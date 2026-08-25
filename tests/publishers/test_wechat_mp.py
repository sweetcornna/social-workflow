"""公众号发布器测试：respx 打桩全部官方接口，不产生任何真实网络请求。

样例响应来自 ``tests/fixtures/wechat/*.json``（按官方文档字段手写，见该目录 README）。
"""

from __future__ import annotations

import json
import struct
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from core import db
from core.models import PublishRecord, new_id
from core.state_machine import (
    ContentStatus,
    PublishPhase,
    make_idem_key,
    publish_with_idempotency,
    slot_of,
)
from publishers.base import (
    ContentBundle,
    MediaAsset,
    PermanentError,
    RetryableError,
)
from publishers.wechat_mp.client import (
    TOKEN_CACHE,
    TokenCache,
    WechatMpClient,
    redact,
)
from publishers.wechat_mp.publisher import (
    CONFIRM_KEY,
    WechatMpPublisher,
    mark_confirm_publish,
)
from publishers.wechat_mp.stub import StubWechatMpClient
from tests.conftest import make_account, make_item

BASE = "https://api.weixin.qq.com"
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "wechat"

APP_ID = "wx_test_appid"
APP_SECRET = "test_app_secret_should_never_be_logged"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _clean_token_cache() -> None:
    """模块级 token 缓存跨用例污染会让断言飘，每个用例前后都清干净。"""
    TOKEN_CACHE.clear()
    yield
    TOKEN_CACHE.clear()


def make_client(**kwargs: Any) -> WechatMpClient:
    kwargs.setdefault("token_cache", TokenCache())
    return WechatMpClient(APP_ID, APP_SECRET, base_url=BASE, **kwargs)


def png_bytes(width: int = 2, height: int = 2) -> bytes:
    """生成一张合法的最小 PNG（避免往仓库里塞二进制素材）。"""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + b"\xff" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


@pytest.fixture
def cover_png(tmp_path: Path) -> Path:
    path = tmp_path / "cover.png"
    path.write_bytes(png_bytes())
    return path


def make_bundle(
    *,
    cover: str = "data/demo/cover.png",
    title: str = "公众号测试标题",
    body_html: str | None = None,
    extra: dict[str, Any] | None = None,
) -> ContentBundle:
    return ContentBundle(
        id="itm-wx-1",
        account_id="acc-wx",
        platform="wechat_mp",
        title=title,
        body_markdown="第一段正文\n\n第二段正文",
        body_html=body_html,
        media=[MediaAsset(path=cover, kind="image", cover=True)],
        tags=["测试"],
        platform_extra=extra or {},
    )


# =========================================================== client: token


def test_token_is_cached_and_refreshed_on_expiry() -> None:
    cache = TokenCache()
    client = WechatMpClient(APP_ID, APP_SECRET, base_url=BASE, token_cache=cache)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(f"{BASE}/cgi-bin/stable_token").mock(
            side_effect=[
                httpx.Response(200, json=fixture("stable_token")),
                httpx.Response(200, json={"access_token": "second-token", "expires_in": 7200}),
            ]
        )
        first = client.get_access_token()
        second = client.get_access_token()
        assert first == second, "缓存内应复用同一个 token"
        assert route.call_count == 1

        # 手动让缓存过期（模拟 7200s 后）
        cache.clear()
        third = client.get_access_token()
        assert third == "second-token"
        assert route.call_count == 2


def test_token_force_refresh_respects_30s_interval() -> None:
    cache = TokenCache()
    client = WechatMpClient(APP_ID, APP_SECRET, base_url=BASE, token_cache=cache)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(f"{BASE}/cgi-bin/stable_token").mock(
            return_value=httpx.Response(200, json=fixture("stable_token"))
        )
        client.get_access_token(force_refresh=True)
        assert json.loads(route.calls[0].request.content)["force_refresh"] is True
        # 30s 内再次 force_refresh 会退化为普通获取，避免撞官方硬限制
        client.get_access_token(force_refresh=True)
        assert json.loads(route.calls[1].request.content)["force_refresh"] is False


def test_missing_credentials_is_permanent_error() -> None:
    client = WechatMpClient("", "", base_url=BASE, token_cache=TokenCache())
    with pytest.raises(PermanentError, match="WECHAT_APP_ID"):
        client.get_access_token()


# ==================================================== client: 错误码映射


def test_40164_maps_to_permanent_error_with_egress_ip() -> None:
    client = make_client()
    with respx.mock as mock:
        mock.post(f"{BASE}/cgi-bin/stable_token").mock(
            return_value=httpx.Response(200, json=fixture("stable_token_40164"))
        )
        with pytest.raises(PermanentError) as caught:
            client.get_access_token()
    exc = caught.value
    assert exc.message.startswith("IP not in whitelist")
    assert "203.0.113.42" in exc.message, "detail 必须带上当前出口 IP"
    assert "api.ipify.org" in exc.message, "detail 必须给出查出口 IP 的办法"
    assert exc.raw["errcode"] == 40164
    assert exc.raw["egress_ip"] == "203.0.113.42"


def test_invalid_secret_maps_to_permanent_error() -> None:
    client = make_client()
    with respx.mock as mock:
        mock.post(f"{BASE}/cgi-bin/stable_token").mock(
            return_value=httpx.Response(200, json=fixture("stable_token_40125"))
        )
        with pytest.raises(PermanentError, match="AppSecret 无效"):
            client.get_access_token()


def test_45009_maps_to_retryable() -> None:
    client = make_client()
    with respx.mock as mock:
        mock.post(f"{BASE}/cgi-bin/stable_token").mock(
            return_value=httpx.Response(200, json=fixture("stable_token"))
        )
        mock.post(f"{BASE}/cgi-bin/draft/add").mock(
            return_value=httpx.Response(200, json=fixture("errcode_45009"))
        )
        with pytest.raises(RetryableError, match="45009"):
            client.draft_add([{"title": "t", "content": "c", "thumb_media_id": "x"}])


def test_40001_refreshes_token_and_retries_once() -> None:
    client = make_client()
    with respx.mock as mock:
        token = mock.post(f"{BASE}/cgi-bin/stable_token").mock(
            side_effect=[
                httpx.Response(200, json={"access_token": "stale", "expires_in": 7200}),
                httpx.Response(200, json={"access_token": "fresh", "expires_in": 7200}),
            ]
        )
        draft = mock.post(f"{BASE}/cgi-bin/draft/add").mock(
            side_effect=[
                httpx.Response(200, json=fixture("errcode_40001")),
                httpx.Response(200, json=fixture("draft_add")),
            ]
        )
        media_id = client.draft_add([{"title": "t", "content": "c", "thumb_media_id": "x"}])
    assert media_id == "FIXTURE-draft-media-id-0001"
    assert token.call_count == 2, "40001 应触发一次 token 刷新"
    assert draft.call_count == 2, "刷新后只重试一次"
    assert "access_token=fresh" in str(draft.calls[1].request.url)


def test_http_5xx_is_retryable_and_4xx_is_permanent() -> None:
    client = make_client()
    with respx.mock as mock:
        mock.post(f"{BASE}/cgi-bin/stable_token").mock(return_value=httpx.Response(503))
        with pytest.raises(RetryableError, match="5xx"):
            client.get_access_token()
    client2 = make_client()
    with respx.mock as mock:
        mock.post(f"{BASE}/cgi-bin/stable_token").mock(
            return_value=httpx.Response(404, text="nope")
        )
        with pytest.raises(PermanentError, match="404"):
            client2.get_access_token()


def test_redact_hides_secret_and_token() -> None:
    text = f'{{"secret":"{APP_SECRET}","access_token":"89_abcdef"}} ?access_token=89_abcdef'
    out = redact(text, APP_SECRET)
    assert APP_SECRET not in out
    assert "89_abcdef" not in out


# ======================================================= client: 图片上传


def test_upload_image_for_article_returns_mmbiz_url(cover_png: Path) -> None:
    client = make_client()
    with respx.mock as mock:
        mock.post(f"{BASE}/cgi-bin/stable_token").mock(
            return_value=httpx.Response(200, json=fixture("stable_token"))
        )
        route = mock.post(f"{BASE}/cgi-bin/media/uploadimg").mock(
            return_value=httpx.Response(200, json=fixture("media_uploadimg"))
        )
        url = client.upload_image_for_article(cover_png)
    assert url.startswith("https://mmbiz.qpic.cn/")
    assert route.call_count == 1


def test_upload_rejects_oversized_and_bad_format(tmp_path: Path) -> None:
    client = make_client()
    gif = tmp_path / "x.gif"
    gif.write_bytes(b"GIF89a")
    with pytest.raises(PermanentError, match="不支持的图片格式"):
        client.upload_image_for_article(gif)

    big = tmp_path / "big.png"
    big.write_bytes(b"\x89PNG" + b"0" * (1024 * 1024 + 10))
    with pytest.raises(PermanentError, match="超过上限"):
        client.upload_image_for_article(big)

    with pytest.raises(PermanentError, match="不存在"):
        client.upload_image_for_article(tmp_path / "missing.png")


def test_add_material_image_returns_media_id(cover_png: Path) -> None:
    client = make_client()
    with respx.mock as mock:
        mock.post(f"{BASE}/cgi-bin/stable_token").mock(
            return_value=httpx.Response(200, json=fixture("stable_token"))
        )
        route = mock.post(f"{BASE}/cgi-bin/material/add_material").mock(
            return_value=httpx.Response(200, json=fixture("material_add_material_image"))
        )
        media_id = client.add_material_image(cover_png)
    assert media_id == "FIXTURE-thumb-media-id-0001"
    assert "type=image" in str(route.calls[0].request.url)


# =================================================== client: 正文图片替换


def test_replace_external_images_uploads_only_non_mmbiz(tmp_path: Path) -> None:
    local = tmp_path / "inline.png"
    local.write_bytes(png_bytes())
    html = (
        '<p><img src="https://mmbiz.qpic.cn/keep/0" /></p>'
        f'<p><img src="{local.name}"></p>'
        f'<p><img src="{local.name}"></p>'  # 同一张图只上传一次
        '<p><img src="https://cdn.example.com/remote.png"></p>'
        '<p><img src="data:image/png;base64,AAAA"></p>'
    )
    client = make_client()
    with respx.mock as mock:
        mock.post(f"{BASE}/cgi-bin/stable_token").mock(
            return_value=httpx.Response(200, json=fixture("stable_token"))
        )
        upload = mock.post(f"{BASE}/cgi-bin/media/uploadimg").mock(
            return_value=httpx.Response(200, json=fixture("media_uploadimg"))
        )
        download = mock.get("https://cdn.example.com/remote.png").mock(
            return_value=httpx.Response(
                200, content=png_bytes(), headers={"content-type": "image/png"}
            )
        )
        out = client.replace_external_images(html, base_dir=tmp_path)

    assert upload.call_count == 2, "本地图去重后 1 次 + 外链 1 次"
    assert download.call_count == 1
    assert "cdn.example.com" not in out
    assert local.name not in out
    assert out.count("mmbiz.qpic.cn") == 4  # 原有 1 张 + 换掉的 3 处
    assert "data:image/png;base64,AAAA" in out, "内联图保持原样，不静默改写"


def test_replace_external_images_raises_on_missing_local_file(tmp_path: Path) -> None:
    client = make_client()
    with pytest.raises(PermanentError, match="不存在"):
        client.replace_external_images('<img src="no-such.png">', base_dir=tmp_path)


# ============================================================ client: datacube


def test_datacube_enforces_date_span() -> None:
    client = make_client()
    with pytest.raises(PermanentError, match="时间跨度超限"):
        client.datacube_getarticletotal("2026-08-01", "2026-08-02")
    with pytest.raises(PermanentError, match="时间跨度超限"):
        client.datacube_getusersummary("2026-08-01", "2026-08-08")
    with pytest.raises(PermanentError, match="日期格式"):
        client.datacube_getusersummary("20260801", "20260801")


def test_datacube_getusersummary_parses_list() -> None:
    client = make_client()
    with respx.mock as mock:
        mock.post(f"{BASE}/cgi-bin/stable_token").mock(
            return_value=httpx.Response(200, json=fixture("stable_token"))
        )
        mock.post(f"{BASE}/datacube/getusersummary").mock(
            return_value=httpx.Response(200, json=fixture("datacube_getusersummary"))
        )
        rows = client.datacube_getusersummary("2026-08-13", "2026-08-14")
    assert [r["new_user"] for r in rows] == [12, 25]


# ============================================================ publisher: prepare


def test_prepare_uploads_cover_and_is_idempotent(cover_png: Path) -> None:
    publisher = WechatMpPublisher("acc-wx", client=StubWechatMpClient())
    bundle = make_bundle(cover=str(cover_png))
    once = publisher.prepare(bundle)
    twice = publisher.prepare(once)
    assert once.model_dump() == twice.model_dump()
    assert once.platform_extra["thumb_media_id"].startswith("thumb-")
    assert once.platform_extra["digest"] == "第一段正文 第二段正文"
    assert once.body_html == "<p>第一段正文</p><p>第二段正文</p>"
    assert publisher.client.calls["add_material_image"] == 1, "第二次 prepare 不应重复上传封面"


def test_prepare_validates_field_lengths(cover_png: Path) -> None:
    publisher = WechatMpPublisher("acc-wx", client=StubWechatMpClient())
    with pytest.raises(PermanentError, match="标题最长 32"):
        publisher.prepare(make_bundle(cover=str(cover_png), title="标" * 33))
    with pytest.raises(PermanentError, match="作者名最长 16"):
        publisher.prepare(make_bundle(cover=str(cover_png), extra={"author": "作" * 17}))
    with pytest.raises(PermanentError, match="摘要最长 120"):
        publisher.prepare(make_bundle(cover=str(cover_png), extra={"digest": "摘" * 121}))


def test_prepare_requires_cover() -> None:
    publisher = WechatMpPublisher("acc-wx", client=StubWechatMpClient())
    bundle = make_bundle().model_copy(update={"media": []})
    with pytest.raises(PermanentError, match="必须有封面图"):
        publisher.prepare(bundle)


# ============================================================ publisher: 闸门


GATE_CASES = [
    pytest.param(True, True, True, True, id="三者齐备-放行"),
    pytest.param(False, True, True, False, id="服务端开关关-挡住"),
    pytest.param(True, False, True, False, id="未认证号-挡住"),
    pytest.param(True, True, False, False, id="本次未人工确认-挡住"),
]


@pytest.mark.parametrize(("auto", "certified", "confirmed", "expected"), GATE_CASES)
def test_double_confirmation_gate(
    auto: bool, certified: bool, confirmed: bool, expected: bool
) -> None:
    stub = StubWechatMpClient()
    publisher = WechatMpPublisher(
        "acc-wx", client=stub, certified=certified, auto_publish=auto, sleeper=lambda _s: None
    )
    extra = {"thumb_media_id": "t-1"}
    if confirmed:
        extra[CONFIRM_KEY] = True
    bundle = make_bundle(extra=extra, body_html="<p>正文</p>")
    result = publisher.publish(bundle)

    assert result.ok is True
    assert result.platform_post_id, "无论是否放行都必须回传 draft media_id"
    if expected:
        assert result.raw["stage"] == "published"
        assert result.url and result.url.startswith("https://mp.weixin.qq.com/s/")
        assert stub.calls.get("freepublish_submit") == 1
    else:
        assert result.raw["stage"] == "draft"
        assert result.url is None
        assert "freepublish_submit" not in stub.calls, "闸门未全开时绝不能触发发布"
        assert result.raw["gate"]["blocked_by"]


def test_confirm_publish_only_accepts_real_true() -> None:
    """字符串 "true" / 1 都不算人工确认，避免表单值被误当放行。"""
    stub = StubWechatMpClient()
    publisher = WechatMpPublisher("acc-wx", client=stub, certified=True, auto_publish=True)
    for value in ("true", 1, "on", "True"):
        bundle = make_bundle(extra={"thumb_media_id": "t", CONFIRM_KEY: value})
        allowed, detail = publisher.gate(bundle)
        assert allowed is False
        assert detail["confirm_publish"] is False


def test_mark_confirm_publish_writes_audit_fields() -> None:
    before = make_bundle().model_dump(mode="json")
    after = mark_confirm_publish(before, actor="operator", at=datetime(2026, 8, 15, tzinfo=UTC))
    assert before["platform_extra"] == {}, "不得原地修改"
    assert after["platform_extra"][CONFIRM_KEY] is True
    assert after["platform_extra"]["confirm_publish_by"] == "operator"
    assert after["platform_extra"]["confirm_publish_at"].startswith("2026-08-15")


# ==================================================== publisher: 发布流程


def test_publish_polls_until_success_over_http(cover_png: Path) -> None:
    """完整走一遍 HTTP：draft/add → freepublish/submit → get(发布中) → get(成功)。"""
    client = make_client()
    publisher = WechatMpPublisher(
        "acc-wx",
        client=client,
        certified=True,
        auto_publish=True,
        poll_interval=0.0,
        sleeper=lambda _s: None,
    )
    with respx.mock as mock:
        mock.post(f"{BASE}/cgi-bin/stable_token").mock(
            return_value=httpx.Response(200, json=fixture("stable_token"))
        )
        mock.post(f"{BASE}/cgi-bin/material/add_material").mock(
            return_value=httpx.Response(200, json=fixture("material_add_material_image"))
        )
        draft = mock.post(f"{BASE}/cgi-bin/draft/add").mock(
            return_value=httpx.Response(200, json=fixture("draft_add"))
        )
        submit = mock.post(f"{BASE}/cgi-bin/freepublish/submit").mock(
            return_value=httpx.Response(200, json=fixture("freepublish_submit"))
        )
        get = mock.post(f"{BASE}/cgi-bin/freepublish/get").mock(
            side_effect=[
                httpx.Response(200, json=fixture("freepublish_get_publishing")),
                httpx.Response(200, json=fixture("freepublish_get_success")),
            ]
        )
        bundle = publisher.prepare(make_bundle(cover=str(cover_png), extra={CONFIRM_KEY: True}))
        result = publisher.publish(bundle)

    assert draft.call_count == 1
    assert submit.call_count == 1
    assert get.call_count == 2, "publish_status=1 时必须继续轮询"
    assert result.ok and result.raw["stage"] == "published"
    assert result.platform_post_id == "FIXTURE-draft-media-id-0001"
    assert "mid=2247483661" in (result.url or "")
    payload = json.loads(draft.calls[0].request.content.decode("utf-8"))
    article = payload["articles"][0]
    assert set(article) >= {"title", "content", "thumb_media_id", "digest", "article_type"}
    assert article["need_open_comment"] == 0
    assert "公众号测试标题" in draft.calls[0].request.content.decode("utf-8")


def test_publish_failure_status_is_permanent() -> None:
    stub = StubWechatMpClient(publish_statuses=[3])
    publisher = WechatMpPublisher(
        "acc-wx", client=stub, certified=True, auto_publish=True, sleeper=lambda _s: None
    )
    bundle = make_bundle(extra={"thumb_media_id": "t", CONFIRM_KEY: True}, body_html="<p>正文</p>")
    with pytest.raises(PermanentError, match="常规失败"):
        publisher.publish(bundle)


def test_publish_poll_timeout_is_retryable() -> None:
    stub = StubWechatMpClient(publish_statuses=[1])
    publisher = WechatMpPublisher(
        "acc-wx",
        client=stub,
        certified=True,
        auto_publish=True,
        poll_timeout=0.0,
        sleeper=lambda _s: None,
    )
    bundle = make_bundle(extra={"thumb_media_id": "t", CONFIRM_KEY: True}, body_html="<p>正文</p>")
    with pytest.raises(RetryableError, match="轮询超时"):
        publisher.publish(bundle)


def test_publish_without_prepare_is_permanent() -> None:
    publisher = WechatMpPublisher("acc-wx", client=StubWechatMpClient())
    with pytest.raises(PermanentError, match="thumb_media_id"):
        publisher.publish(make_bundle())


# ============================================================ dry_run


def test_dry_run_makes_no_http_call(cover_png: Path) -> None:
    client = WechatMpClient(
        APP_ID, APP_SECRET, base_url=BASE, dry_run=True, token_cache=TokenCache()
    )
    publisher = WechatMpPublisher(
        "acc-wx", dry_run=True, client=client, certified=True, auto_publish=True
    )
    with respx.mock(assert_all_called=False) as mock:
        catch_all = mock.route().mock(return_value=httpx.Response(500))
        bundle = publisher.prepare(make_bundle(cover=str(cover_png), extra={CONFIRM_KEY: True}))
        result = publisher.publish(bundle)
        assert publisher.health().status == "ok"
        assert publisher.reconcile(bundle) is None
        assert publisher.fetch_metrics("whatever")["available"] is False
        assert catch_all.call_count == 0, "dry_run 下不得发出任何请求"
    assert result.ok is False, "dry_run 不得声称发布成功"
    assert result.platform_post_id is None
    assert result.raw["dry_run"] is True
    assert bundle.platform_extra["thumb_media_id"].startswith("dryrun-thumb-")


# ============================================================ health


def test_health_maps_40164_to_degraded() -> None:
    publisher = WechatMpPublisher("acc-wx", client=make_client())
    with respx.mock as mock:
        mock.post(f"{BASE}/cgi-bin/stable_token").mock(
            return_value=httpx.Response(200, json=fixture("stable_token_40164"))
        )
        health = publisher.health()
    assert health.status == "degraded"
    assert "203.0.113.42" in health.detail


def test_health_ok_and_missing_credentials() -> None:
    publisher = WechatMpPublisher("acc-wx", client=make_client(), certified=True)
    with respx.mock as mock:
        mock.post(f"{BASE}/cgi-bin/stable_token").mock(
            return_value=httpx.Response(200, json=fixture("stable_token"))
        )
        assert publisher.health().status == "ok"

    blank = WechatMpPublisher(
        "acc-wx", client=WechatMpClient("", "", base_url=BASE, token_cache=TokenCache())
    )
    health = blank.health()
    assert health.status == "degraded" and "WECHAT_APP_ID" in health.detail


def test_health_never_returns_needs_relogin() -> None:
    """公众号没有登录态，误判成 needs_relogin 会白白挂起排期项。"""
    publisher = WechatMpPublisher("acc-wx", client=make_client())
    with respx.mock as mock:
        mock.post(f"{BASE}/cgi-bin/stable_token").mock(
            return_value=httpx.Response(200, json=fixture("stable_token_40125"))
        )
        assert publisher.health().status == "degraded"


# ============================================================ reconcile


def test_reconcile_matches_draft_by_title_and_digest() -> None:
    drafts = fixture("draft_batchget")["item"]
    stub = StubWechatMpClient(drafts=drafts)
    publisher = WechatMpPublisher("acc-wx", client=stub)
    bundle = make_bundle(
        title="已经建过的草稿标题", extra={"digest": "这是摘要", "thumb_media_id": "t"}
    )
    hit = publisher.reconcile(bundle)
    assert hit is not None and hit.ok
    assert hit.platform_post_id == "FIXTURE-draft-media-id-0001"
    assert hit.raw["matched_by"] == "title+digest"


def test_reconcile_returns_none_when_digest_differs() -> None:
    stub = StubWechatMpClient(drafts=fixture("draft_batchget")["item"])
    publisher = WechatMpPublisher("acc-wx", client=stub)
    bundle = make_bundle(title="已经建过的草稿标题", extra={"digest": "换了个摘要"})
    assert publisher.reconcile(bundle) is None


def test_reconcile_checks_published_list_when_auto_publish_on() -> None:
    stub = StubWechatMpClient(published=fixture("freepublish_batchget")["item"])
    publisher = WechatMpPublisher("acc-wx", client=stub, certified=True, auto_publish=True)
    bundle = make_bundle(title="已经发布过的文章标题", extra={"digest": "这是摘要"})
    hit = publisher.reconcile(bundle)
    assert hit is not None and hit.raw["stage"] == "published"
    assert hit.platform_post_id == "FIXTURE-article-id-0001"


def test_idempotent_conflict_goes_through_reconcile(session) -> None:
    """幂等键已存在（上次结果未知）时，必须先对账，不能再建一份草稿。"""
    account = make_account(session, account_id="acc-wx", platform="wechat_mp")
    item = make_item(session, account, title="幂等测试标题", scheduled_in_minutes=-1)
    session.commit()

    stub = StubWechatMpClient()
    publisher = WechatMpPublisher("acc-wx", client=stub, certified=False, auto_publish=False)

    first = publish_with_idempotency(session, item, publisher)
    session.commit()
    assert first.ok and stub.calls["draft_add"] == 1
    assert item.status == ContentStatus.PUBLISHED.value

    # 模拟"发起过但回包丢失"：记录退回 in_flight，内容退回 scheduled
    record = session.query(PublishRecord).filter_by(content_item_id=item.id).one()
    record.phase = PublishPhase.IN_FLIGHT.value
    item.status = ContentStatus.SCHEDULED.value
    session.flush()

    second = publish_with_idempotency(session, item, publisher)
    session.commit()

    assert second.ok and second.raw.get("reconciled") is True
    assert stub.calls["draft_add"] == 1, "对账命中后绝不能重复建草稿"
    assert stub.calls["draft_batchget"] >= 1
    assert item.status == ContentStatus.PUBLISHED.value


def test_unique_idem_key_blocks_second_record(session) -> None:
    account = make_account(session, account_id="acc-wx2", platform="wechat_mp")
    item = make_item(session, account, title="唯一键测试", scheduled_in_minutes=-1)
    bundle = ContentBundle.model_validate(item.bundle_json)
    key = make_idem_key(account.id, "wechat_mp", bundle.content_hash, slot_of(item))
    session.add(
        PublishRecord(
            id=new_id("pub"),
            content_item_id=item.id,
            idem_key=key,
            phase=PublishPhase.DONE.value,
            platform_post_id="already-there",
        )
    )
    session.commit()

    stub = StubWechatMpClient()
    publisher = WechatMpPublisher("acc-wx2", client=stub)
    result = publish_with_idempotency(session, item, publisher)
    assert result.raw["idempotent_hit"] is True
    assert result.platform_post_id == "already-there"
    assert "draft_add" not in stub.calls


# ============================================================ fetch_metrics


def test_fetch_metrics_unavailable_for_uncertified_account() -> None:
    publisher = WechatMpPublisher("acc-wx", client=StubWechatMpClient(), certified=False)
    metrics = publisher.fetch_metrics("FIXTURE-draft-media-id-0001")
    assert metrics["available"] is False
    assert "未认证号" in metrics["reason"]
    # 契约：缺失填 None，不伪造 0
    assert all(metrics[k] is None for k in ("views", "likes", "shares", "collects", "follows"))


def test_fetch_metrics_matches_by_title_via_draft_get() -> None:
    rows = fixture("datacube_getarticletotal")["list"]
    stub = StubWechatMpClient(
        article_rows=rows,
        drafts=[
            {
                "media_id": "FIXTURE-draft-media-id-0001",
                "content": {"news_item": [{"title": "契约测试标题"}]},
            }
        ],
    )
    publisher = WechatMpPublisher("acc-wx", client=stub, certified=True)
    metrics = publisher.fetch_metrics("FIXTURE-draft-media-id-0001")
    assert metrics["available"] is True
    assert metrics["views"] == 1080, "details 取 stat_date 最大的一条"
    assert metrics["read"] == 1080
    assert metrics["shares"] == 88 and metrics["collects"] == 40
    assert metrics["likes"] is None and metrics["comments"] is None
    assert metrics["stat_date"] == "2026-08-14"


def test_fetch_metrics_matches_by_msgid() -> None:
    stub = StubWechatMpClient(article_rows=fixture("datacube_getarticletotal")["list"])
    publisher = WechatMpPublisher("acc-wx", client=stub, certified=True)
    metrics = publisher.fetch_metrics("12003_3")
    assert metrics["available"] is True
    assert stub.calls.get("draft_get") is None, "msgid 直配时不该再查草稿"


def test_fetch_metrics_reports_unmatched_reason() -> None:
    stub = StubWechatMpClient(article_rows=[])
    publisher = WechatMpPublisher("acc-wx", client=stub, certified=True)
    metrics = publisher.fetch_metrics("some-unknown-id")
    assert metrics["available"] is False
    assert "映射到 datacube msgid" in metrics["reason"]


# ============================================================ registry


def test_registry_serves_wechat_publisher(monkeypatch) -> None:
    from publishers.registry import get_publisher, register_builtin_publishers

    register_builtin_publishers()
    publisher = get_publisher("wechat_mp", "acc-wx", dry_run=True)
    assert isinstance(publisher, WechatMpPublisher)
    assert publisher.platform == "wechat_mp"


def test_registry_switches_to_wenyan_backend(monkeypatch) -> None:
    from core.config import reload_settings
    from publishers.registry import get_publisher, register_builtin_publishers
    from publishers.wechat_mp.wenyan_backend import WenyanWechatMpPublisher

    monkeypatch.setenv("WECHAT_BACKEND", "wenyan")
    reload_settings()
    register_builtin_publishers()
    publisher = get_publisher("wechat_mp", "acc-wx", dry_run=True)
    assert isinstance(publisher, WenyanWechatMpPublisher)


# ============================================================ wenyan 后端


def test_wenyan_command_and_env_hide_secret() -> None:
    from publishers.wechat_mp.wenyan_backend import WenyanBackend

    backend = WenyanBackend(app_id=APP_ID, app_secret=APP_SECRET, node_bin="npx")
    cmd = backend.build_command(Path("/tmp/article.md"))
    assert cmd[:4] == ["npx", "-y", "@wenyan-md/cli", "publish"]
    assert "-f" in cmd and "/tmp/article.md" in cmd
    assert "--app-id" in cmd
    assert APP_SECRET not in cmd, "AppSecret 绝不能出现在 argv（ps 可见）"
    env = backend.build_env()
    assert env["WECHAT_APP_ID"] == APP_ID and env["WECHAT_APP_SECRET"] == APP_SECRET


def test_wenyan_server_mode_flags() -> None:
    from publishers.wechat_mp.wenyan_backend import WenyanBackend

    backend = WenyanBackend(
        app_id=APP_ID,
        app_secret=APP_SECRET,
        server_url="https://wenyan.example.com",
        api_key_file="/etc/wenyan/key",
    )
    cmd = backend.build_command(Path("a.md"))
    assert "--server" in cmd and "https://wenyan.example.com" in cmd
    assert "--api-key-file" in cmd and "/etc/wenyan/key" in cmd


def test_wenyan_publish_parses_media_id(monkeypatch) -> None:
    import subprocess

    from publishers.wechat_mp.wenyan_backend import WenyanBackend, WenyanWechatMpPublisher

    seen: dict[str, Any] = {}

    def fake_runner(cmd, env, timeout, cwd):
        seen["cmd"] = cmd
        seen["env_secret"] = env["WECHAT_APP_SECRET"]
        seen["markdown"] = (Path(cmd[cmd.index("-f") + 1])).read_text(encoding="utf-8")
        return subprocess.CompletedProcess(
            cmd, 0, stdout="publish ok media_id: FIXTUREwenyanMediaId123456\n", stderr=""
        )

    backend = WenyanBackend(
        app_id=APP_ID, app_secret=APP_SECRET, runner=fake_runner, node_bin="python3"
    )
    publisher = WenyanWechatMpPublisher(
        "acc-wx", backend=backend, client=StubWechatMpClient(), certified=True, auto_publish=True
    )
    bundle = publisher.prepare(make_bundle(extra={CONFIRM_KEY: True}))
    result = publisher.publish(bundle)

    assert result.ok and result.platform_post_id == "FIXTUREwenyanMediaId123456"
    assert result.raw["stage"] == "draft", "wenyan 后端只到草稿箱"
    assert seen["env_secret"] == APP_SECRET
    assert "第一段正文" in seen["markdown"]


def test_wenyan_publish_failure_is_permanent() -> None:
    import subprocess

    from publishers.wechat_mp.wenyan_backend import WenyanBackend

    def failing(cmd, env, timeout, cwd):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    backend = WenyanBackend(
        app_id=APP_ID, app_secret=APP_SECRET, runner=failing, node_bin="python3"
    )
    with pytest.raises(PermanentError, match="rc=1"):
        backend.publish_draft("正文", title="标题")


def test_wenyan_dry_run_does_not_spawn_process() -> None:
    from publishers.wechat_mp.wenyan_backend import WenyanBackend

    def boom(*args, **kwargs):  # pragma: no cover - 不应被调用
        raise AssertionError("dry_run 不得启动子进程")

    backend = WenyanBackend(app_id=APP_ID, app_secret=APP_SECRET, runner=boom, dry_run=True)
    out = backend.publish_draft("正文", title="标题")
    assert out["media_id"].startswith("dryrun-wenyan-")


# ============================================================ metrics 采集


def test_collector_respects_24h_and_7d_windows(session) -> None:
    from metrics.collector import collect_all, due_window

    published_at = datetime(2026, 8, 1, tzinfo=UTC)
    assert due_window(published_at, [], published_at + timedelta(hours=1)) is None
    assert due_window(published_at, [], published_at + timedelta(hours=25)) == "24h"
    taken = [published_at + timedelta(hours=25)]
    assert due_window(published_at, taken, published_at + timedelta(hours=26)) is None
    assert due_window(published_at, taken, published_at + timedelta(days=8)) == "7d"

    from publishers.registry import use_fake_publishers

    use_fake_publishers()
    account = make_account(session, account_id="acc-m", platform="xhs")
    item = make_item(session, account, title="指标窗口", scheduled_in_minutes=-1)
    session.commit()
    with db.session_scope() as s2:
        fresh = s2.get(type(item), item.id)
        from publishers.base import FakePublisher

        publish_with_idempotency(s2, fresh, FakePublisher("acc-m", platform="xhs"))

    now = datetime.now(UTC)
    assert collect_all(respect_windows=True, now=now)["snapshots"] == 0, "刚发布，窗口未到"
    assert collect_all(respect_windows=True, now=now + timedelta(hours=25))["snapshots"] == 1
    assert collect_all(respect_windows=True, now=now + timedelta(hours=26))["snapshots"] == 0, (
        "24h 窗口已覆盖"
    )
    assert collect_all(respect_windows=True, now=now + timedelta(days=8))["snapshots"] == 1


def test_collector_falls_back_to_title_metrics(session) -> None:
    """公众号 post_id 映射不上时，用标题兜底（可选能力，不破坏契约）。"""
    from metrics.collector import collect_all
    from publishers.registry import register

    stub = StubWechatMpClient(article_rows=fixture("datacube_getarticletotal")["list"])
    publisher = WechatMpPublisher("acc-wxm", client=stub, certified=True)
    register("wechat_mp", lambda account_id, **kw: publisher)

    account = make_account(session, account_id="acc-wxm", platform="wechat_mp")
    item = make_item(session, account, title="契约测试标题", scheduled_in_minutes=-1)
    session.commit()
    with db.session_scope() as s2:
        fresh = s2.get(type(item), item.id)
        result = publish_with_idempotency(s2, fresh, publisher)
    post_id = result.platform_post_id or ""

    # 模拟"草稿已被 freepublish 消费掉"：draft/get 取不到标题，post_id 也不是 msgid
    stub.drafts.clear()
    assert publisher.fetch_metrics(post_id)["available"] is False

    stats = collect_all()
    assert stats["snapshots"] == 1
    with db.session_scope() as s3:
        from core.models import MetricSnapshot

        snap = s3.query(MetricSnapshot).one()
    assert snap.metrics_json["available"] is True
    assert snap.metrics_json["views"] == 1080


# ============================================================ preflight 接线


def test_preflight_reports_40164(monkeypatch) -> None:
    from core.config import Settings
    from scripts.preflight import check_wechat

    settings = Settings(
        wechat_app_id=APP_ID,
        wechat_app_secret=APP_SECRET,
        wechat_api_base=BASE,
        wechat_certified=False,
        wechat_auto_publish=False,
    )
    with respx.mock as mock:
        mock.post(f"{BASE}/cgi-bin/stable_token").mock(
            return_value=httpx.Response(200, json=fixture("stable_token_40164"))
        )
        checks = {c.name: c for c in check_wechat(settings, offline=False)}
    assert checks["公众号 IP 白名单(40164)"].status == "FAIL"
    assert "203.0.113.42" in checks["公众号 IP 白名单(40164)"].detail
    assert checks["公众号自动发布闸门"].status == "OK"


def test_preflight_flags_auto_publish_without_certification() -> None:
    from core.config import Settings
    from scripts.preflight import check_wechat

    settings = Settings(
        wechat_app_id=APP_ID,
        wechat_app_secret=APP_SECRET,
        wechat_api_base=BASE,
        wechat_certified=False,
        wechat_auto_publish=True,
    )
    checks = {c.name: c for c in check_wechat(settings, offline=True)}
    assert checks["公众号自动发布闸门"].status == "FAIL"
    assert checks["公众号 IP 白名单(40164)"].status == "SKIP"


def test_preflight_token_ok() -> None:
    from core.config import Settings
    from scripts.preflight import check_wechat

    settings = Settings(
        wechat_app_id=APP_ID,
        wechat_app_secret=APP_SECRET,
        wechat_api_base=BASE,
        wechat_certified=True,
        wechat_auto_publish=True,
    )
    with respx.mock as mock:
        mock.post(f"{BASE}/cgi-bin/stable_token").mock(
            return_value=httpx.Response(200, json=fixture("stable_token"))
        )
        checks = {c.name: c for c in check_wechat(settings, offline=False)}
    assert checks["公众号 IP 白名单(40164)"].status == "OK"
    assert checks["公众号自动发布闸门"].status == "WARN", "两道开关全开时必须提醒仍需逐条确认"
