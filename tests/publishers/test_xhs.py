"""小红书发布器测试：respx 打桩 xiaohongshu-mcp 全部接口，不产生任何真实网络请求。

样例响应来自 ``tests/fixtures/xhs/*.json``（按上游源码手写，见该目录 README）。
"""

from __future__ import annotations

import base64
import json
import logging
import struct
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select

from core import db
from core.models import Account, ContentItem, PublishRecord
from core.scheduler import RATE_LIMITER, RateLimiter, tick_login_health
from core.state_machine import (
    AccountStatus,
    ContentStatus,
    PublishPhase,
    publish_with_idempotency,
)
from publishers.base import (
    ContentBundle,
    MediaAsset,
    NeedsReloginError,
    PermanentError,
    RetryableError,
)
from publishers.xhs.client import (
    MediaPathMapper,
    XhsMcpClient,
    calc_title_length,
    classify_error,
    note_url,
    parse_count,
    parse_go_duration,
    redact,
)
from publishers.xhs.login import check_and_mark
from publishers.xhs.publisher import (
    KEY_SCHEDULE_AT,
    KEY_SIDECAR_IMAGES,
    SCHEDULED_PREFIX,
    UNRESOLVED_PREFIX,
    XhsPublisher,
    is_placeholder_post_id,
)
from publishers.xhs.stub import StubXhsMcpClient, make_feed
from tests.conftest import make_account

BASE = "http://xhs-sidecar.invalid:18060"
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "xhs"

AUTH_TOKEN = "xhs_token_should_never_be_logged"
NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
NOTE_ID = "66c0f1a2000000001e03d5b1"
TITLE = "3 个通勤包收纳思路"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _clean_rate_limiter() -> None:
    """限频器是进程内单例，跨用例污染会让断言飘。"""
    RATE_LIMITER.reset()
    yield
    RATE_LIMITER.reset()


def png_bytes(width: int = 2, height: int = 2) -> bytes:
    """生成一张合法的最小 PNG（避免往仓库里塞二进制素材）。"""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    raw = bytearray()
    for _ in range(height):
        raw.append(0)
        raw.extend(b"\xff\x00\x00" * width)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def make_images(tmp_path: Path, count: int = 3) -> list[Path]:
    media_dir = tmp_path / "media" / "itm_demo"
    media_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for index in range(count):
        path = media_dir / f"card{index}.png"
        path.write_bytes(png_bytes())
        paths.append(path)
    return paths


def make_client(**kwargs: Any) -> XhsMcpClient:
    kwargs.setdefault("auth_token", AUTH_TOKEN)
    kwargs.setdefault("account_id", "acc-1")
    return XhsMcpClient(BASE, **kwargs)


def make_publisher(
    *, client: XhsMcpClient | None = None, now: datetime = NOW, **kwargs: Any
) -> XhsPublisher:
    kwargs.setdefault("limiter", RateLimiter(min_interval_seconds=0))
    kwargs.setdefault("daily_limit", 50)
    kwargs.setdefault("resolve_attempts", 1)
    kwargs.setdefault("sleeper", lambda _seconds: None)
    return XhsPublisher("acc-1", client=client or make_client(), now=lambda: now, **kwargs)


def make_bundle(paths: list[Path], **overrides: Any) -> ContentBundle:
    data: dict[str, Any] = {
        "id": "itm_demo",
        "account_id": "acc-1",
        "platform": "xhs",
        "title": TITLE,
        "body_markdown": "1. 分区收纳袋\n2. 一物两用\n3. 每周清一次",
        "media": [
            MediaAsset(path=str(p), kind="image", cover=(i == 0)) for i, p in enumerate(paths)
        ],
        "tags": ["#通勤", "通勤", "收纳", "  "],
    }
    data.update(overrides)
    return ContentBundle(**data)


def route_login(mock: respx.MockRouter, fixture_name: str = "login_status_ok") -> None:
    mock.get(f"{BASE}/api/v1/login/status").mock(
        return_value=httpx.Response(200, json=fixture(fixture_name))
    )


def route_user_me(mock: respx.MockRouter, fixture_name: str = "user_me") -> respx.Route:
    return mock.get(f"{BASE}/api/v1/user/me").mock(
        return_value=httpx.Response(200, json=fixture(fixture_name))
    )


# ============================================================ 纯函数 / 工具


def test_calc_title_length_matches_upstream_rule():
    # 上游口径：非 ASCII 记 2、ASCII 记 1，总和向上取整除 2
    assert calc_title_length("") == 0
    assert calc_title_length("ab") == 1
    assert calc_title_length("abc") == 2  # 3 -> ceil(3/2)
    assert calc_title_length("中文") == 2
    assert calc_title_length("中文abcd") == 4
    assert calc_title_length("🎒") == 2  # emoji 是两个 UTF-16 代理码元
    # 20 个汉字刚好到上限，21 个超
    assert calc_title_length("字" * 20) == 20
    assert calc_title_length("字" * 21) == 21


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1234", 1234),
        ("1.2万", 12000),
        ("3.4亿", 340000000),
        ("1,234", 1234),
        (0, 0),
        ("", None),
        ("赞", None),  # 未互动时页面给的是标签文字，不能当 0
        (None, None),
        (True, None),
    ],
)
def test_parse_count(raw, expected):
    assert parse_count(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("4m0s", 240.0), ("0s", 0.0), ("1h30m0s", 5400.0), ("", None), ("nope", None)],
)
def test_parse_go_duration(raw, expected):
    assert parse_go_duration(raw) == expected


def test_redact_hides_token_xsec_and_qrcode():
    text = json.dumps(
        {
            "xsec_token": "SECRET-XSEC",
            "img": "data:image/png;base64," + "A" * 64,
            "note": f"Authorization: Bearer {AUTH_TOKEN}",
        },
        ensure_ascii=False,
    )
    out = redact(text, AUTH_TOKEN)
    assert AUTH_TOKEN not in out
    assert "SECRET-XSEC" not in out
    assert "A" * 64 not in out


@pytest.mark.parametrize(
    ("message", "status", "expected"),
    [
        ("用户未登录，请先扫码登录", 500, NeedsReloginError),
        ("cookies 已失效", 500, NeedsReloginError),
        ("标题长度超过限制", 500, PermanentError),
        ("定时发布时间必须至少在1小时后", 500, PermanentError),
        ("请求参数错误", 400, PermanentError),
        ("未授权", 401, PermanentError),
        # 认不出来的一律按可重试：误判成永久失败会直接进死信
        ("context deadline exceeded", 500, RetryableError),
    ],
)
def test_classify_error(message, status, expected):
    assert classify_error(message, status=status) is expected


def test_note_url_carries_xsec_token():
    assert note_url("abc") == "https://www.xiaohongshu.com/explore/abc"
    assert "xsec_token=t1" in note_url("abc", "t1")


def test_media_path_mapper_translates_into_container(tmp_path):
    mapper = MediaPathMapper(host_dir=str(tmp_path / "media"), container_dir="/app/images")
    target = tmp_path / "media" / "itm_1" / "a.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(png_bytes())
    assert mapper.to_sidecar(str(target)) == "/app/images/itm_1/a.png"
    # URL 原样透传（sidecar 自己下载）
    assert mapper.to_sidecar("https://x.invalid/a.png") == "https://x.invalid/a.png"
    # 挂载点外的素材容器里看不到，早失败好过发出缺图笔记
    outside = tmp_path / "elsewhere.png"
    outside.write_bytes(png_bytes())
    with pytest.raises(PermanentError, match="XHS_MEDIA_HOST_DIR"):
        mapper.to_sidecar(str(outside))


def test_media_path_mapper_passthrough_when_unset(tmp_path):
    """host_dir 置空 = sidecar 与 core 同机，原样给绝对路径。"""
    target = tmp_path / "a.png"
    target.write_bytes(png_bytes())
    assert MediaPathMapper().to_sidecar(str(target)) == str(target.resolve())


# ==================================================================== 客户端


@respx.mock
def test_client_sends_bearer_token_and_unwraps_data():
    route = respx.get(f"{BASE}/api/v1/login/status").mock(
        return_value=httpx.Response(200, json=fixture("login_status_ok"))
    )
    status = make_client().login_status()
    assert status.is_logged_in is True
    assert status.username == "通勤研究所"
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {AUTH_TOKEN}"


@respx.mock
def test_client_qrcode_strips_data_uri_prefix():
    respx.get(f"{BASE}/api/v1/login/qrcode").mock(
        return_value=httpx.Response(200, json=fixture("login_qrcode"))
    )
    info = make_client().get_login_qrcode()
    assert not info.image_base64.startswith("data:")
    assert base64.b64decode(info.image_base64, validate=True)[:8] == b"\x89PNG\r\n\x1a\n"
    assert info.timeout_seconds == 240.0
    assert info.is_logged_in is False


@respx.mock
def test_client_unwraps_double_nested_profile():
    route_user_me(respx)
    notes = make_client().my_notes()
    assert [n["id"] for n in notes] == [NOTE_ID, "66bfe0910000000012034aa2"]


@respx.mock
def test_client_timeout_is_retryable():
    respx.get(f"{BASE}/api/v1/login/status").mock(side_effect=httpx.ConnectTimeout("boom"))
    with pytest.raises(RetryableError, match="超时"):
        make_client().login_status()


@respx.mock
def test_client_connect_error_is_retryable():
    respx.get(f"{BASE}/api/v1/login/status").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(RetryableError, match="连不上 sidecar"):
        make_client().login_status()


@respx.mock
def test_client_401_is_permanent():
    respx.get(f"{BASE}/api/v1/login/status").mock(
        return_value=httpx.Response(401, json=fixture("error_unauthorized"))
    )
    with pytest.raises(PermanentError):
        make_client().login_status()


@respx.mock
def test_client_does_not_log_secrets(caplog):
    respx.get(f"{BASE}/api/v1/user/me").mock(
        return_value=httpx.Response(200, json=fixture("user_me"))
    )
    with caplog.at_level(logging.DEBUG, logger="social_workflow.publishers.xhs"):
        make_client().my_notes()
    text = caplog.text
    assert AUTH_TOKEN not in text
    assert "ABC-xsec-token-should-be-redacted" not in text


# ==================================================================== prepare


def test_prepare_normalises_tags_and_maps_paths(tmp_path):
    paths = make_images(tmp_path, 2)
    client = make_client(media_mapper=MediaPathMapper(host_dir=str(tmp_path / "media")))
    publisher = make_publisher(client=client)
    prepared = publisher.prepare(make_bundle(paths))
    assert prepared.tags == ["通勤", "收纳"]  # 去 # 前缀 + 去重 + 去空
    assert prepared.platform_extra[KEY_SIDECAR_IMAGES] == [
        "/app/images/itm_demo/card0.png",
        "/app/images/itm_demo/card1.png",
    ]
    # 幂等：再跑一次结果完全一致
    assert publisher.prepare(prepared).model_dump() == prepared.model_dump()


def test_prepare_rejects_long_title(tmp_path):
    publisher = make_publisher(client=StubXhsMcpClient())
    bundle = make_bundle(make_images(tmp_path, 1), title="字" * 21)
    with pytest.raises(PermanentError, match="标题最长 20 字"):
        publisher.prepare(bundle)


@pytest.mark.parametrize("count", [0, 19])
def test_prepare_rejects_bad_image_count(tmp_path, count):
    paths = make_images(tmp_path, count) if count else []
    client = make_client(media_mapper=MediaPathMapper(host_dir=str(tmp_path / "media")))
    publisher = make_publisher(client=client)
    with pytest.raises(PermanentError, match="1–18 张图片"):
        publisher.prepare(make_bundle(paths))


def test_prepare_rejects_missing_file(tmp_path):
    client = make_client(media_mapper=MediaPathMapper(host_dir=str(tmp_path / "media")))
    publisher = make_publisher(client=client)
    bundle = make_bundle([tmp_path / "media" / "itm_demo" / "nope.png"])
    with pytest.raises(PermanentError, match="图片文件不存在"):
        publisher.prepare(bundle)


def test_prepare_normalises_schedule_at(tmp_path):
    paths = make_images(tmp_path, 1)
    client = make_client(media_mapper=MediaPathMapper(host_dir=str(tmp_path / "media")))
    publisher = make_publisher(client=client)
    target = NOW + timedelta(days=2)
    bundle = make_bundle(paths, platform_extra={KEY_SCHEDULE_AT: target.isoformat()})
    prepared = publisher.prepare(bundle)
    assert prepared.platform_extra[KEY_SCHEDULE_AT] == target.isoformat(timespec="seconds")


@pytest.mark.parametrize(
    ("delta", "match"),
    [
        (timedelta(minutes=30), "至少要在 1 小时后"),
        (timedelta(days=15), "不能超过 14 天"),
    ],
)
def test_prepare_rejects_out_of_window_schedule(tmp_path, delta, match):
    paths = make_images(tmp_path, 1)
    client = make_client(media_mapper=MediaPathMapper(host_dir=str(tmp_path / "media")))
    publisher = make_publisher(client=client)
    bundle = make_bundle(paths, platform_extra={KEY_SCHEDULE_AT: (NOW + delta).isoformat()})
    with pytest.raises(PermanentError, match=match):
        publisher.prepare(bundle)


def test_prepare_rejects_bad_visibility(tmp_path):
    publisher = make_publisher(client=StubXhsMcpClient())
    bundle = make_bundle(make_images(tmp_path, 1), platform_extra={"visibility": "只给猫看"})
    with pytest.raises(PermanentError, match="visibility 取值非法"):
        publisher.prepare(bundle)


# ==================================================================== publish


@respx.mock
def test_publish_success_resolves_note_id_from_profile(tmp_path):
    publish = respx.post(f"{BASE}/api/v1/publish").mock(
        return_value=httpx.Response(200, json=fixture("publish_ok"))
    )
    route_user_me(respx)
    client = make_client(media_mapper=MediaPathMapper(host_dir=str(tmp_path / "media")))
    publisher = make_publisher(client=client)
    bundle = publisher.prepare(make_bundle(make_images(tmp_path, 3)))

    result = publisher.publish(bundle)

    assert result.ok is True
    # sidecar 的发布响应里没有 note_id，必须靠 user/me 对账拿回来
    assert result.platform_post_id == NOTE_ID
    assert result.url is not None and NOTE_ID in result.url
    assert result.raw["note_id_resolved"] is True
    body = json.loads(publish.calls.last.request.content)
    assert body["title"] == TITLE
    assert body["images"] == [
        "/app/images/itm_demo/card0.png",
        "/app/images/itm_demo/card1.png",
        "/app/images/itm_demo/card2.png",
    ]
    assert body["tags"] == ["通勤", "收纳"]
    assert "schedule_at" not in body


@respx.mock
def test_publish_unresolved_note_id_still_marks_done(tmp_path):
    """对账不到 note_id 时**绝不重发**：宁可少一条指标，也不能重复发真笔记。"""
    respx.post(f"{BASE}/api/v1/publish").mock(
        return_value=httpx.Response(200, json=fixture("publish_ok"))
    )
    route_user_me(respx, "user_me_empty")
    client = make_client(media_mapper=MediaPathMapper(host_dir=str(tmp_path / "media")))
    publisher = make_publisher(client=client)
    bundle = publisher.prepare(make_bundle(make_images(tmp_path, 1)))

    result = publisher.publish(bundle)

    assert result.ok is True
    assert result.platform_post_id.startswith(UNRESOLVED_PREFIX)
    assert is_placeholder_post_id(result.platform_post_id)
    assert result.raw["note_id_resolved"] is False


@respx.mock
def test_publish_scheduled_returns_scheduled_placeholder(tmp_path):
    publish = respx.post(f"{BASE}/api/v1/publish").mock(
        return_value=httpx.Response(200, json=fixture("publish_ok"))
    )
    me = route_user_me(respx)
    client = make_client(media_mapper=MediaPathMapper(host_dir=str(tmp_path / "media")))
    publisher = make_publisher(client=client)
    target = NOW + timedelta(hours=5)
    bundle = publisher.prepare(
        make_bundle(make_images(tmp_path, 1), platform_extra={KEY_SCHEDULE_AT: target.isoformat()})
    )

    result = publisher.publish(bundle)

    assert result.ok is True
    assert result.platform_post_id.startswith(SCHEDULED_PREFIX)
    assert result.raw["stage"] == "scheduled"
    assert json.loads(publish.calls.last.request.content)["schedule_at"] == target.isoformat(
        timespec="seconds"
    )
    # 定时笔记还在"待发布"，主页上查不到，不该白跑一次对账
    assert me.call_count == 0


@respx.mock
def test_publish_not_logged_in_raises_needs_relogin(tmp_path):
    respx.post(f"{BASE}/api/v1/publish").mock(
        return_value=httpx.Response(500, json=fixture("error_not_logged_in"))
    )
    client = make_client(media_mapper=MediaPathMapper(host_dir=str(tmp_path / "media")))
    publisher = make_publisher(client=client)
    bundle = publisher.prepare(make_bundle(make_images(tmp_path, 1)))
    with pytest.raises(NeedsReloginError):
        publisher.publish(bundle)


@respx.mock
def test_publish_content_violation_is_permanent(tmp_path):
    respx.post(f"{BASE}/api/v1/publish").mock(
        return_value=httpx.Response(500, json=fixture("error_title_too_long"))
    )
    client = make_client(media_mapper=MediaPathMapper(host_dir=str(tmp_path / "media")))
    publisher = make_publisher(client=client)
    bundle = publisher.prepare(make_bundle(make_images(tmp_path, 1)))
    with pytest.raises(PermanentError):
        publisher.publish(bundle)


@respx.mock
def test_publish_unknown_5xx_is_retryable(tmp_path):
    respx.post(f"{BASE}/api/v1/publish").mock(
        return_value=httpx.Response(500, json=fixture("error_internal"))
    )
    client = make_client(media_mapper=MediaPathMapper(host_dir=str(tmp_path / "media")))
    publisher = make_publisher(client=client)
    bundle = publisher.prepare(make_bundle(make_images(tmp_path, 1)))
    with pytest.raises(RetryableError):
        publisher.publish(bundle)


@respx.mock
def test_dry_run_never_touches_sidecar(tmp_path):
    publish = respx.post(f"{BASE}/api/v1/publish").mock(
        return_value=httpx.Response(200, json=fixture("publish_ok"))
    )
    client = make_client(
        dry_run=True, media_mapper=MediaPathMapper(host_dir=str(tmp_path / "media"))
    )
    publisher = make_publisher(client=client, dry_run=True)
    bundle = publisher.prepare(make_bundle(make_images(tmp_path, 2)))

    result = publisher.publish(bundle)

    assert result.ok is False  # 契约：dry_run 不得声称发布成功
    assert result.platform_post_id is None
    assert result.raw["dry_run"] is True
    assert publish.call_count == 0
    # dry_run 下 prepare 仍然做本地校验（图片确实被解析并映射了）
    assert bundle.platform_extra[KEY_SIDECAR_IMAGES][0].startswith("/app/images/")
    assert publisher.health().status == "ok"
    assert publisher.reconcile(bundle) is None
    assert publisher.fetch_metrics("x")["available"] is False


# ================================================================= 限频


def test_publish_over_daily_limit_raises_rate_limited(tmp_path):
    limiter = RateLimiter(min_interval_seconds=0)
    limiter.record("acc-1", now=NOW)
    publisher = make_publisher(client=StubXhsMcpClient(), limiter=limiter, daily_limit=1)
    bundle = publisher.prepare(make_bundle(make_images(tmp_path, 1)))
    with pytest.raises(RetryableError, match="rate limited"):
        publisher.publish(bundle)


def test_publish_respects_min_interval(tmp_path):
    limiter = RateLimiter(min_interval_seconds=900)
    publisher = make_publisher(client=StubXhsMcpClient(), limiter=limiter, daily_limit=50)
    bundle = publisher.prepare(make_bundle(make_images(tmp_path, 1)))
    publisher.publish(bundle)
    with pytest.raises(RetryableError) as caught:
        publisher.publish(bundle)
    assert caught.value.retry_after == pytest.approx(900.0)


def test_rate_limit_token_dedupes_publisher_and_scheduler():
    """发布器与调度器都会记账，同一内容项只能计一次，否则日限额被腰斩。"""
    limiter = RateLimiter(min_interval_seconds=0)
    assert limiter.record("acc-1", now=NOW, token="xhs:itm_1") is True
    assert limiter.record("acc-1", now=NOW, token="xhs:itm_1") is False
    assert limiter.used_today("acc-1", now=NOW) == 1
    # 不传 token 保持 P0 行为：无条件计数
    limiter.record("acc-1", now=NOW)
    assert limiter.used_today("acc-1", now=NOW) == 2


# ================================================================= reconcile


@respx.mock
def test_reconcile_hits_by_title(tmp_path):
    route_user_me(respx)
    client = make_client(media_mapper=MediaPathMapper(host_dir=str(tmp_path / "media")))
    publisher = make_publisher(client=client)
    bundle = publisher.prepare(make_bundle(make_images(tmp_path, 1)))

    hit = publisher.reconcile(bundle)

    assert hit is not None and hit.ok
    assert hit.platform_post_id == NOTE_ID
    assert hit.raw["reconciled"] is True


@respx.mock
def test_reconcile_miss_returns_none(tmp_path):
    route_user_me(respx, "user_me_empty")
    client = make_client(media_mapper=MediaPathMapper(host_dir=str(tmp_path / "media")))
    publisher = make_publisher(client=client)
    bundle = publisher.prepare(make_bundle(make_images(tmp_path, 1)))
    assert publisher.reconcile(bundle) is None


@respx.mock
def test_reconcile_unreachable_sidecar_raises_retryable(tmp_path):
    respx.get(f"{BASE}/api/v1/user/me").mock(side_effect=httpx.ConnectError("down"))
    client = make_client(media_mapper=MediaPathMapper(host_dir=str(tmp_path / "media")))
    publisher = make_publisher(client=client)
    bundle = publisher.prepare(make_bundle(make_images(tmp_path, 1)))
    with pytest.raises(RetryableError):
        publisher.reconcile(bundle)


def test_reconcile_picks_newest_when_titles_collide(tmp_path):
    """同名笔记用 feeds/detail 的发布时间挑最新的一条，别认错旧笔记。"""
    old_id, new_id = "note-old", "note-new"
    stub = StubXhsMcpClient(
        notes=[make_feed(old_id, TITLE), make_feed(new_id, TITLE)],
        note_times={old_id: 1_700_000_000_000, new_id: 1_790_000_000_000},
    )
    publisher = make_publisher(client=stub)
    bundle = publisher.prepare(make_bundle(make_images(tmp_path, 1)))
    hit = publisher.reconcile(bundle)
    assert hit is not None and hit.platform_post_id == new_id
    assert stub.calls["note_detail"] == 2


# ================================================================== health


@respx.mock
def test_health_ok_when_logged_in():
    route_login(respx)
    health = make_publisher().health()
    assert health.status == "ok"
    assert "通勤研究所" in health.detail


@respx.mock
def test_health_needs_relogin_when_logged_out():
    route_login(respx, "login_status_logged_out")
    health = make_publisher().health()
    assert health.status == "needs_relogin"
    assert "扫码" in health.detail


@respx.mock
def test_health_degraded_when_sidecar_down():
    """sidecar 挂了只算 degraded：误判成 needs_relogin 会白白挂起排期还催人扫码。"""
    respx.get(f"{BASE}/api/v1/login/status").mock(side_effect=httpx.ConnectError("down"))
    health = make_publisher().health()
    assert health.status == "degraded"


def test_missing_endpoint_is_permanent_error():
    with pytest.raises(PermanentError, match="sidecar 地址"):
        XhsMcpClient("", account_id="acc-1")


# ================================================================== metrics


@respx.mock
def test_fetch_metrics_from_profile_feed():
    route_user_me(respx)
    metrics = make_publisher().fetch_metrics(NOTE_ID)
    assert metrics["available"] is True
    assert metrics["likes"] == 12000  # "1.2万"
    assert metrics["collects"] == 480
    assert metrics["comments"] == 36
    assert metrics["shares"] == 12
    assert metrics["views"] is None  # 小红书不公开阅读量，不能伪造 0
    assert metrics["platform"] == "xhs"
    assert metrics["platform_post_id"] == NOTE_ID


@respx.mock
def test_fetch_metrics_placeholder_is_unavailable():
    metrics = make_publisher().fetch_metrics(UNRESOLVED_PREFIX + "abc")
    assert metrics["available"] is False
    assert all(metrics[k] is None for k in ("views", "likes", "comments", "shares", "collects"))
    assert "占位" in metrics["reason"]


@respx.mock
def test_fetch_metrics_for_title_recovers_post_id():
    route_user_me(respx)
    metrics = make_publisher().fetch_metrics_for_title(TITLE)
    assert metrics["available"] is True
    assert metrics["platform_post_id"] == NOTE_ID


@respx.mock
def test_fetch_metrics_unknown_note_reports_reason():
    route_user_me(respx, "user_me_empty")
    metrics = make_publisher().fetch_metrics("note-not-mine")
    assert metrics["available"] is False
    assert metrics["reason"]


# ============================================================= 扫码登录通道


@respx.mock
def test_get_login_qrcode_returns_png_base64():
    respx.get(f"{BASE}/api/v1/login/qrcode").mock(
        return_value=httpx.Response(200, json=fixture("login_qrcode"))
    )
    publisher = make_publisher()
    raw = base64.b64decode(publisher.get_login_qrcode(), validate=True)
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    detail = publisher.get_login_qrcode_detail()
    assert detail["timeout_seconds"] == 240.0
    assert detail["placeholder"] is False


def test_submit_sms_code_is_refused():
    """红线：小红书只走扫码，不提供任何验证码通道。"""
    with pytest.raises(PermanentError, match="扫码"):
        make_publisher(client=StubXhsMcpClient()).submit_sms_code("123456")


# ============================================================ 登录巡检 + 状态机


@respx.mock
def test_check_and_mark_suspends_scheduled_items_on_logout(notifier):
    route_login(respx, "login_status_logged_out")
    with db.session_scope() as session:
        account = make_account(session, account_id="acc-xhs", platform="xhs")
        item = ContentItem(
            id="itm_sched",
            account_id=account.id,
            status=ContentStatus.SCHEDULED.value,
            bundle_json={},
        )
        session.add(item)
        session.flush()

        health = check_and_mark(session, account, publisher=make_publisher(), notifier=notifier)

        assert health.status == "needs_relogin"
        assert account.status == AccountStatus.NEEDS_RELOGIN
        assert item.status == ContentStatus.SUSPENDED
    assert any("需重登" in title for _lvl, title, _text in notifier.sent)


@respx.mock
def test_check_and_mark_restores_account_after_scan(notifier):
    route_login(respx)
    with db.session_scope() as session:
        account = make_account(
            session, account_id="acc-xhs", platform="xhs", status="needs_relogin"
        )
        item = ContentItem(
            id="itm_susp",
            account_id=account.id,
            status=ContentStatus.SUSPENDED.value,
            prev_status=ContentStatus.SCHEDULED.value,
            bundle_json={},
        )
        session.add(item)
        session.flush()

        check_and_mark(session, account, publisher=make_publisher(), notifier=notifier)

        assert account.status == AccountStatus.OK
        assert item.status == ContentStatus.SCHEDULED


def test_check_and_mark_skips_banned_accounts():
    with db.session_scope() as session:
        account = make_account(session, account_id="acc-banned", platform="xhs", status="banned")
        health = check_and_mark(session, account, publisher=make_publisher())
        assert health.status == "banned"
        assert account.status == AccountStatus.BANNED


@respx.mock
def test_tick_login_health_scans_accounts(notifier):
    route_login(respx, "login_status_logged_out")
    with db.session_scope() as session:
        make_account(session, account_id="acc-xhs", platform="xhs")
    from publishers.registry import register

    register("xhs", lambda account_id, **kw: make_publisher())
    stats = tick_login_health(platforms=["xhs"], notifier=notifier)
    assert stats["checked"] == 1
    assert stats["needs_relogin"] == 1
    with db.session_scope() as session:
        assert session.get(Account, "acc-xhs").status == AccountStatus.NEEDS_RELOGIN


# ======================================================= 与两阶段幂等发布集成


def _seed_item(session, bundle: ContentBundle) -> ContentItem:
    account = session.get(Account, "acc-1")
    if account is None:
        account = make_account(session, account_id="acc-1", platform="xhs")
    item = ContentItem(
        id=bundle.id,
        account_id=account.id,
        status=ContentStatus.SCHEDULED.value,
        bundle_json=bundle.model_dump(mode="json"),
        scheduled_at=None,
    )
    session.add(item)
    session.flush()
    return item


def test_publish_with_idempotency_records_note_id(tmp_path, notifier):
    stub = StubXhsMcpClient()
    publisher = make_publisher(client=stub)
    bundle = make_bundle(make_images(tmp_path, 2))
    with db.session_scope() as session:
        item = _seed_item(session, bundle)
        result = publish_with_idempotency(session, item, publisher, notifier=notifier)
        assert result.ok and result.platform_post_id.startswith("stub-note-")
        assert item.status == ContentStatus.PUBLISHED
        record = session.scalars(select(PublishRecord)).one()
        assert record.phase == PublishPhase.DONE
        assert record.platform_post_id == result.platform_post_id
    assert stub.calls["publish_content"] == 1


def test_retry_reconciles_instead_of_republishing(tmp_path, notifier):
    """回包丢了但笔记其实已发出去：重试前的对账必须命中，绝不能再发一次。"""
    bundle = make_bundle(make_images(tmp_path, 1))
    # sidecar 上已经有同标题笔记（上一次发成功但回包丢了）
    stub = StubXhsMcpClient(notes=[make_feed("note-already-there", TITLE)])
    publisher = make_publisher(client=stub)
    with db.session_scope() as session:
        item = _seed_item(session, bundle)
        prepared = publisher.prepare(bundle)
        from core.state_machine import make_idem_key

        session.add(
            PublishRecord(
                id="pub_prev",
                content_item_id=item.id,
                idem_key=make_idem_key("acc-1", "xhs", prepared.content_hash, ""),
                phase=PublishPhase.FAILED.value,
                attempts=1,
            )
        )
        session.flush()

        result = publish_with_idempotency(session, item, publisher, notifier=notifier)

        assert result.platform_post_id == "note-already-there"
        assert result.raw["reconciled"] is True
        assert item.status == ContentStatus.PUBLISHED
        records = session.scalars(select(PublishRecord)).all()
        assert len(records) == 1 and records[0].phase == PublishPhase.DONE
    assert "publish_content" not in stub.calls, "对账命中后绝不能再发一次"


def test_collector_repairs_placeholder_post_id(tmp_path):
    """发布时没解析出笔记 id，指标兜底命中后要把真 id 回填进发布记录。"""
    from metrics.collector import collect_all

    bundle = make_bundle(make_images(tmp_path, 1))
    # 模拟"发出去了但主页还没刷出来"：发布时只能记占位 id
    stub = StubXhsMcpClient(notes=[], appear_after_publish=False)
    publisher = make_publisher(client=stub)
    with db.session_scope() as session:
        item = _seed_item(session, bundle)
        result = publish_with_idempotency(session, item, publisher)
        assert is_placeholder_post_id(result.platform_post_id)
        published_at = session.scalars(select(PublishRecord)).one().updated_at

    # 笔记随后出现在主页上（定时发布上线 / 页面延迟）
    stub.notes = [make_feed("note-appeared-later", TITLE)]
    from publishers.registry import register

    register("xhs", lambda account_id, **kw: publisher)
    stats = collect_all()

    assert stats["snapshots"] == 1
    with db.session_scope() as session:
        record = session.scalars(select(PublishRecord)).one()
        assert record.platform_post_id == "note-appeared-later"
        # 回填不能顺手把"发布时刻"改掉，否则 24h/7d 窗口整体后移
        assert record.updated_at == published_at
