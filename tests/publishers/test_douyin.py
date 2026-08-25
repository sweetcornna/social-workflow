"""抖音发布器测试：respx 打桩宿主机上传器的全部端点，不产生任何真实网络请求。

样例响应来自 ``tests/fixtures/douyin/*.json``（服务契约是本仓库自己定的，见该目录 README）。

另有一个 ``-m browser`` 的冒烟测试：用**真实浏览器**驱动一张本地静态假页面，
验证 identity 闸门、截图落盘、验证码只填不识别这三条逻辑链。缺浏览器自动 skip。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select

from core import db
from core.models import Account, ContentItem, PublishRecord
from core.scheduler import (
    RATE_LIMITER,
    RateLimiter,
    reset_login_health_throttle,
    tick_login_health,
)
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
from publishers.base import is_placeholder_post_id as base_is_placeholder
from publishers.douyin.client import (
    STATE_BUSY,
    STATE_IDENTITY_MISMATCH,
    STATE_LOGGED_OUT,
    STATE_NEEDS_CAPTCHA,
    STATE_NEEDS_SMS,
    STATE_TIMEOUT,
    DouyinServiceClient,
    HostPathMapper,
    clean_hashtags,
    exception_for_state,
    mask_nickname,
    parse_count,
    redact,
)
from publishers.douyin.publisher import (
    DAILY_LIMIT_CEILING,
    KEY_HASHTAGS,
    KEY_HOST_COVER,
    KEY_HOST_VIDEO,
    KEY_SCHEDULE_AT,
    SCHEDULED_PREFIX,
    UNRESOLVED_PREFIX,
    DouyinPublisher,
    MinIntervalGate,
    is_placeholder_post_id,
)
from publishers.douyin.stub import StubDouyinServiceClient
from tests.conftest import make_account

BASE = "http://127.0.0.1:8710"
ACCOUNT = "acc-dy"
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "douyin"

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
TITLE = "三分钟看懂通勤包收纳"
POST_ID = "7412345678901234567"
IDENTITY = "通勤研究所"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _clean_limits() -> None:
    """限频器与最小间隔闸门都是进程内单例，跨用例污染会让断言飘。"""
    from publishers.douyin.publisher import MIN_INTERVAL_GATE

    RATE_LIMITER.reset()
    MIN_INTERVAL_GATE.reset()
    reset_login_health_throttle()
    yield
    RATE_LIMITER.reset()
    MIN_INTERVAL_GATE.reset()
    reset_login_health_throttle()


# ------------------------------------------------------------------ 造数工具


def make_video(tmp_path: Path, name: str = "clip.mp4") -> Path:
    path = tmp_path / "media" / "itm_dy" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    # 内容无所谓：校验只看后缀与存在性，真正的成片校验在 review/ 那边
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42fake")
    return path


def make_cover(tmp_path: Path, name: str = "cover.png") -> Path:
    path = tmp_path / "media" / "itm_dy" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    return path


def make_client(**kwargs: Any) -> DouyinServiceClient:
    kwargs.setdefault("account_id", ACCOUNT)
    return DouyinServiceClient(BASE, **kwargs)


def make_publisher(
    *, client: DouyinServiceClient | None = None, now: Any = NOW, **kwargs: Any
) -> DouyinPublisher:
    """``now`` 可以给固定时刻，也可以给一个返回时刻的函数（对账用真实时间时方便）。"""
    clock = now if callable(now) else (lambda: now)
    kwargs.setdefault("limiter", RateLimiter(min_interval_seconds=0))
    kwargs.setdefault("gate", MinIntervalGate())
    kwargs.setdefault("daily_limit", 10)
    kwargs.setdefault("identity_hint", IDENTITY)
    return DouyinPublisher(ACCOUNT, client=client or make_client(), now=clock, **kwargs)


def make_bundle(video: Path, *, cover: Path | None = None, **overrides: Any) -> ContentBundle:
    media = [MediaAsset(path=str(video), kind="video")]
    if cover is not None:
        media.append(MediaAsset(path=str(cover), kind="image", cover=True))
    data: dict[str, Any] = {
        "id": "itm_dy",
        "account_id": ACCOUNT,
        "platform": "douyin",
        "title": TITLE,
        "body_markdown": "门后挂钩、洞洞板、床底箱，三样东西解决通勤包爆炸。",
        "media": media,
        "tags": ["#通勤", "通勤", "收纳", "  "],
    }
    data.update(overrides)
    return ContentBundle(**data)


def route(method: str, path: str, name: str, status: int = 200) -> respx.Route:
    caller = getattr(respx, method.lower())
    return caller(f"{BASE}{path}").mock(return_value=httpx.Response(status, json=fixture(name)))


# ============================================================ 纯函数 / 工具


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", ""), ("张", "张*"), ("小王", "小*"), ("通勤研究所", "通***所")],
)
def test_mask_nickname(raw, expected):
    assert mask_nickname(raw) == expected


def test_redact_hides_sms_code():
    text = json.dumps({"code": "135790", "note": "验证码 246813 已发送"}, ensure_ascii=False)
    out = redact(text)
    assert "135790" not in out
    assert "246813" not in out


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1234", 1234),
        ("1.2万", 12000),
        ("3.4亿", 340000000),
        ("1,234", 1234),
        (0, 0),
        ("", None),
        ("-", None),  # 没数据时页面给的是横杠，不能当 0
        (None, None),
        (True, None),
    ],
)
def test_parse_count(raw, expected):
    assert parse_count(raw) == expected


def test_clean_hashtags_dedupes_and_truncates():
    tags = ["#a", "a", "b c", "d", "e", "f", "g", "  "]
    # 去 # / 去重 / 去空格 / 截到 5 个
    assert clean_hashtags(tags) == ["a", "bc", "d", "e", "f"]
    # 幂等：再洗一次结果不变
    assert clean_hashtags(clean_hashtags(tags)) == clean_hashtags(tags)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (STATE_LOGGED_OUT, NeedsReloginError),
        (STATE_NEEDS_SMS, NeedsReloginError),
        (STATE_NEEDS_CAPTCHA, NeedsReloginError),
        (STATE_IDENTITY_MISMATCH, PermanentError),
        ("invalid_content", PermanentError),
        (STATE_BUSY, RetryableError),
        (STATE_TIMEOUT, RetryableError),
        # 认不出来的一律按可重试：误判成永久失败会直接进死信
        ("something_new", RetryableError),
    ],
)
def test_exception_for_state(state, expected):
    assert exception_for_state(state) is expected


def test_host_path_mapper_translates_container_path(tmp_path):
    mapper = HostPathMapper(local_dir=str(tmp_path / "app"), host_dir="/Users/me/media")
    target = tmp_path / "app" / "itm_1" / "clip.mp4"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")
    assert mapper.to_host(str(target)) == "/Users/me/media/itm_1/clip.mp4"
    outside = tmp_path / "elsewhere.mp4"
    outside.write_bytes(b"x")
    with pytest.raises(PermanentError, match="DOUYIN_MEDIA_LOCAL_DIR"):
        mapper.to_host(str(outside))


def test_host_path_mapper_passthrough_when_unset(tmp_path):
    """两个目录都留空 = core 与上传器同机，原样给绝对路径。"""
    target = tmp_path / "clip.mp4"
    target.write_bytes(b"x")
    assert HostPathMapper().to_host(str(target)) == str(target.resolve())


def test_placeholder_ids_are_recognised_cross_platform():
    """占位 id 必须被 publishers.base 的通用识别函数认出来，否则指标兜底不会触发。"""
    placeholder = UNRESOLVED_PREFIX + "abcdef"
    assert is_placeholder_post_id(placeholder)
    assert base_is_placeholder(placeholder)
    assert base_is_placeholder(SCHEDULED_PREFIX + "abcdef")
    assert not base_is_placeholder(POST_ID)


# ==================================================================== 客户端


@respx.mock
def test_client_unwraps_envelope():
    route("GET", f"/accounts/{ACCOUNT}/login/status", "login_status_ok")
    state = make_client().login_status()
    assert state.is_logged_in
    assert state.nickname == "通***所"


@respx.mock
def test_client_connect_error_is_retryable():
    respx.get(f"{BASE}/health").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(RetryableError, match="连不上宿主机抖音上传器"):
        make_client().health()


@respx.mock
def test_client_timeout_is_retryable():
    respx.get(f"{BASE}/health").mock(side_effect=httpx.ConnectTimeout("boom"))
    with pytest.raises(RetryableError, match="超时"):
        make_client().health()


@respx.mock
def test_client_non_json_is_retryable():
    respx.get(f"{BASE}/health").mock(return_value=httpx.Response(502, text="<html>bad gateway"))
    with pytest.raises(RetryableError):
        make_client().health()


@respx.mock
def test_client_404_is_permanent():
    respx.get(f"{BASE}/health").mock(return_value=httpx.Response(404, json={"detail": "nope"}))
    with pytest.raises(PermanentError, match="404"):
        make_client().health()


@respx.mock
def test_sms_code_never_reaches_the_log(caplog):
    """红线：验证码不进日志（连请求体都不打）。"""
    route("POST", f"/accounts/{ACCOUNT}/sms_code", "sms_code_ok")
    with caplog.at_level(logging.DEBUG, logger="social_workflow.publishers.douyin"):
        make_client().submit_sms_code("135790")
    assert "135790" not in caplog.text


def test_missing_service_url_is_permanent_error():
    with pytest.raises(PermanentError, match="DOUYIN_SERVICE_URL"):
        DouyinServiceClient("", account_id=ACCOUNT)


# ==================================================================== prepare


def test_prepare_normalises_tags_and_maps_paths(tmp_path):
    video = make_video(tmp_path)
    cover = make_cover(tmp_path)
    publisher = make_publisher()
    prepared = publisher.prepare(make_bundle(video, cover=cover))

    assert prepared.tags == ["通勤", "收纳"]  # 去 # / 去重 / 去空
    assert prepared.platform_extra[KEY_HOST_VIDEO] == str(video.resolve())
    assert prepared.platform_extra[KEY_HOST_COVER] == str(cover.resolve())
    assert prepared.platform_extra[KEY_HASHTAGS] == ["通勤", "收纳"]
    # 幂等：再跑一次结果完全一致
    assert publisher.prepare(prepared).model_dump() == prepared.model_dump()


def test_prepare_cover_is_optional(tmp_path):
    publisher = make_publisher()
    prepared = publisher.prepare(make_bundle(make_video(tmp_path)))
    assert KEY_HOST_COVER not in prepared.platform_extra


def test_prepare_rejects_long_title(tmp_path):
    publisher = make_publisher()
    bundle = make_bundle(make_video(tmp_path), title="字" * 31)
    with pytest.raises(PermanentError, match="标题最长 30 字"):
        publisher.prepare(bundle)


def test_prepare_rejects_empty_title(tmp_path):
    publisher = make_publisher()
    with pytest.raises(PermanentError, match="标题不能为空"):
        publisher.prepare(make_bundle(make_video(tmp_path), title="   "))


@pytest.mark.parametrize("count", [0, 2])
def test_prepare_requires_exactly_one_video(tmp_path, count):
    publisher = make_publisher()
    media = [
        MediaAsset(path=str(make_video(tmp_path, f"c{i}.mp4")), kind="video") for i in range(count)
    ]
    bundle = make_bundle(make_video(tmp_path)).model_copy(update={"media": media})
    with pytest.raises(PermanentError, match="只需要 1 个视频成片"):
        publisher.prepare(bundle)


def test_prepare_rejects_missing_video(tmp_path):
    publisher = make_publisher()
    bundle = make_bundle(tmp_path / "media" / "itm_dy" / "nope.mp4")
    with pytest.raises(PermanentError, match="视频文件不存在"):
        publisher.prepare(bundle)


def test_prepare_rejects_non_mp4(tmp_path):
    publisher = make_publisher()
    bad = tmp_path / "clip.mov"
    bad.write_bytes(b"x")
    with pytest.raises(PermanentError, match="只接受"):
        publisher.prepare(make_bundle(bad))


def test_prepare_truncates_hashtags_to_five(tmp_path):
    publisher = make_publisher()
    bundle = make_bundle(make_video(tmp_path), tags=[f"t{i}" for i in range(9)])
    prepared = publisher.prepare(bundle)
    assert prepared.platform_extra[KEY_HASHTAGS] == ["t0", "t1", "t2", "t3", "t4"]
    assert prepared.tags == prepared.platform_extra[KEY_HASHTAGS]


def test_prepare_normalises_schedule_at(tmp_path):
    publisher = make_publisher()
    target = NOW + timedelta(days=2)
    bundle = make_bundle(make_video(tmp_path), platform_extra={KEY_SCHEDULE_AT: target.isoformat()})
    prepared = publisher.prepare(bundle)
    assert prepared.platform_extra[KEY_SCHEDULE_AT] == target.isoformat(timespec="seconds")


@pytest.mark.parametrize(
    ("delta", "match"),
    [
        (timedelta(minutes=30), "至少要在 2 小时后"),
        (timedelta(days=15), "不能超过 14 天"),
    ],
)
def test_prepare_rejects_out_of_window_schedule(tmp_path, delta, match):
    publisher = make_publisher()
    bundle = make_bundle(
        make_video(tmp_path), platform_extra={KEY_SCHEDULE_AT: (NOW + delta).isoformat()}
    )
    with pytest.raises(PermanentError, match=match):
        publisher.prepare(bundle)


def test_publish_without_prepare_is_permanent(tmp_path):
    publisher = make_publisher()
    with pytest.raises(PermanentError, match="必须先调用 prepare"):
        publisher.publish(make_bundle(make_video(tmp_path)))


# ==================================================================== publish


@respx.mock
def test_publish_success(tmp_path):
    publish = route("POST", f"/accounts/{ACCOUNT}/publish", "publish_ok")
    publisher = make_publisher()
    bundle = publisher.prepare(make_bundle(make_video(tmp_path), cover=make_cover(tmp_path)))

    result = publisher.publish(bundle)

    assert result.ok is True
    assert result.platform_post_id == POST_ID
    assert result.url == f"https://www.douyin.com/video/{POST_ID}"
    assert result.raw["post_id_resolved"] is True
    assert result.raw["screenshot_path"]
    body = json.loads(publish.calls.last.request.content)
    assert body["title"] == TITLE
    assert body["hashtags"] == ["通勤", "收纳"]
    assert body["identity_hint"] == IDENTITY  # 防发错号的依据要真的传下去
    assert body["video_path"].endswith("clip.mp4")
    assert body["cover_path"].endswith("cover.png")


@respx.mock
def test_publish_without_post_id_still_marks_done(tmp_path):
    """拿不到作品 id 时**绝不重发**：宁可少一条指标，也不能重复发一条真视频。"""
    route("POST", f"/accounts/{ACCOUNT}/publish", "publish_no_post_id")
    publisher = make_publisher()
    bundle = publisher.prepare(make_bundle(make_video(tmp_path)))

    result = publisher.publish(bundle)

    assert result.ok is True
    assert result.platform_post_id.startswith(UNRESOLVED_PREFIX)
    assert result.raw["post_id_resolved"] is False


@respx.mock
def test_publish_scheduled_uses_scheduled_placeholder(tmp_path):
    route("POST", f"/accounts/{ACCOUNT}/publish", "publish_no_post_id")
    publisher = make_publisher()
    target = NOW + timedelta(hours=5)
    bundle = publisher.prepare(
        make_bundle(make_video(tmp_path), platform_extra={KEY_SCHEDULE_AT: target.isoformat()})
    )
    result = publisher.publish(bundle)
    assert result.platform_post_id.startswith(SCHEDULED_PREFIX)


@respx.mock
def test_publish_needs_sms_raises_needs_relogin(tmp_path):
    """短信二次验证 = 需要真人，走账号级 needs_relogin 通道，绝不自动打码。"""
    route("POST", f"/accounts/{ACCOUNT}/publish", "publish_needs_sms")
    publisher = make_publisher()
    bundle = publisher.prepare(make_bundle(make_video(tmp_path)))
    with pytest.raises(NeedsReloginError) as caught:
        publisher.publish(bundle)
    assert caught.value.raw["state"] == STATE_NEEDS_SMS
    assert caught.value.raw["screenshot_path"]


@respx.mock
def test_publish_identity_mismatch_is_permanent(tmp_path):
    """页面昵称对不上 = 差点发错号，必须不可重试地停下。"""
    route("POST", f"/accounts/{ACCOUNT}/publish", "publish_identity_mismatch")
    publisher = make_publisher()
    bundle = publisher.prepare(make_bundle(make_video(tmp_path)))
    with pytest.raises(PermanentError) as caught:
        publisher.publish(bundle)
    assert caught.value.raw["state"] == STATE_IDENTITY_MISMATCH


@respx.mock
def test_publish_content_violation_is_permanent(tmp_path):
    route("POST", f"/accounts/{ACCOUNT}/publish", "publish_rejected")
    publisher = make_publisher()
    bundle = publisher.prepare(make_bundle(make_video(tmp_path)))
    with pytest.raises(PermanentError):
        publisher.publish(bundle)


@respx.mock
def test_publish_busy_is_retryable(tmp_path):
    route("POST", f"/accounts/{ACCOUNT}/publish", "publish_busy")
    publisher = make_publisher()
    bundle = publisher.prepare(make_bundle(make_video(tmp_path)))
    with pytest.raises(RetryableError):
        publisher.publish(bundle)


@respx.mock
def test_publish_timeout_is_retryable(tmp_path):
    """点了发布但没等到跳转：**可能已经发出去了**，靠对账兜底，所以只算可重试。"""
    route("POST", f"/accounts/{ACCOUNT}/publish", "publish_timeout")
    publisher = make_publisher()
    bundle = publisher.prepare(make_bundle(make_video(tmp_path)))
    with pytest.raises(RetryableError):
        publisher.publish(bundle)


@respx.mock
def test_dry_run_never_touches_the_service(tmp_path):
    publish = route("POST", f"/accounts/{ACCOUNT}/publish", "publish_ok")
    health = route("GET", "/health", "health")
    client = make_client(dry_run=True)
    publisher = make_publisher(client=client, dry_run=True)
    bundle = publisher.prepare(make_bundle(make_video(tmp_path)))

    result = publisher.publish(bundle)

    assert result.ok is False  # 契约：dry_run 不得声称发布成功
    assert result.platform_post_id is None
    assert result.raw["dry_run"] is True
    assert publish.call_count == 0
    assert health.call_count == 0
    # dry_run 下 prepare 仍然做本地校验（成片确实被解析并映射了）
    assert bundle.platform_extra[KEY_HOST_VIDEO].endswith("clip.mp4")
    assert publisher.health().status == "ok"
    assert publisher.reconcile(bundle) is None
    assert publisher.fetch_metrics("x")["available"] is False
    assert publisher.fetch_metrics_for_title(TITLE)["available"] is False


# ================================================================= 限频


def test_publish_over_daily_limit_raises_rate_limited(tmp_path):
    limiter = RateLimiter(min_interval_seconds=0)
    limiter.record(ACCOUNT, now=NOW)
    publisher = make_publisher(client=StubDouyinServiceClient(), limiter=limiter, daily_limit=1)
    bundle = publisher.prepare(make_bundle(make_video(tmp_path)))
    with pytest.raises(RetryableError, match="rate limited"):
        publisher.publish(bundle)


def test_publish_respects_30min_min_interval(tmp_path):
    """抖音的最小间隔比全局值更严：全局限频器放行也要被抖音闸门挡下。"""
    publisher = make_publisher(
        client=StubDouyinServiceClient(),
        limiter=RateLimiter(min_interval_seconds=0),  # 全局完全不挡
        gate=MinIntervalGate(),
        min_interval_minutes=30,
        daily_limit=10,
    )
    bundle = publisher.prepare(make_bundle(make_video(tmp_path)))
    publisher.publish(bundle)
    with pytest.raises(RetryableError) as caught:
        publisher.publish(bundle)
    assert caught.value.retry_after == pytest.approx(1800.0)


def test_daily_limit_is_capped_by_ceiling(tmp_path):
    """红线：不允许通过 accounts 表把抖音日限额调到离谱的数字。"""
    publisher = make_publisher(client=StubDouyinServiceClient(), daily_limit=999)
    assert publisher.daily_limit == DAILY_LIMIT_CEILING


def test_rate_limit_token_dedupes_publisher_and_scheduler(tmp_path):
    """发布器与调度器都会记账，同一内容项只能计一次。"""
    limiter = RateLimiter(min_interval_seconds=0)
    publisher = make_publisher(
        client=StubDouyinServiceClient(), limiter=limiter, daily_limit=10, min_interval_minutes=0
    )
    bundle = publisher.prepare(make_bundle(make_video(tmp_path)))
    publisher.publish(bundle)
    # 调度器随后用同一个 token 记一次，不该被算成两条
    limiter.record(ACCOUNT, now=NOW, token=f"douyin:{bundle.id}")
    assert limiter.used_today(ACCOUNT, now=NOW) == 1


# ================================================================= reconcile


@respx.mock
def test_reconcile_hits_by_title_and_time(tmp_path):
    route("GET", f"/accounts/{ACCOUNT}/recent_posts", "recent_posts")
    publisher = make_publisher(now=datetime(2026, 8, 16, 12, 0, tzinfo=UTC))
    bundle = publisher.prepare(make_bundle(make_video(tmp_path)))

    hit = publisher.reconcile(bundle)

    assert hit is not None and hit.ok
    assert hit.platform_post_id == POST_ID
    assert hit.raw["reconciled"] is True
    assert hit.raw["matched_by"] == "title+time"


@respx.mock
def test_reconcile_ignores_posts_outside_the_window(tmp_path):
    """同名旧作品不该被当成"这次发的"——差 6 天就超出 24h 对账窗口。"""
    route("GET", f"/accounts/{ACCOUNT}/recent_posts", "recent_posts")
    publisher = make_publisher(now=datetime(2026, 8, 16, 12, 0, tzinfo=UTC))
    bundle = publisher.prepare(make_bundle(make_video(tmp_path), title="上周那条被你们催的开箱"))
    assert publisher.reconcile(bundle) is None


@respx.mock
def test_reconcile_miss_returns_none(tmp_path):
    route("GET", f"/accounts/{ACCOUNT}/recent_posts", "recent_posts_empty")
    publisher = make_publisher()
    bundle = publisher.prepare(make_bundle(make_video(tmp_path)))
    assert publisher.reconcile(bundle) is None


@respx.mock
def test_reconcile_unreachable_service_raises_retryable(tmp_path):
    respx.get(f"{BASE}/accounts/{ACCOUNT}/recent_posts").mock(
        side_effect=httpx.ConnectError("down")
    )
    publisher = make_publisher()
    bundle = publisher.prepare(make_bundle(make_video(tmp_path)))
    with pytest.raises(RetryableError):
        publisher.reconcile(bundle)


# ================================================================== health


@respx.mock
def test_health_ok_when_logged_in_and_identity_matches():
    route("GET", "/health", "health")
    route("GET", f"/accounts/{ACCOUNT}/login/status", "login_status_ok")
    health = make_publisher().health()
    assert health.status == "ok"
    assert "通***所" in health.detail


@respx.mock
def test_health_needs_relogin_when_logged_out():
    route("GET", "/health", "health")
    route("GET", f"/accounts/{ACCOUNT}/login/status", "login_status_logged_out")
    health = make_publisher().health()
    assert health.status == "needs_relogin"
    assert "扫码" in health.detail


@respx.mock
def test_health_needs_relogin_when_stuck_on_sms():
    route("GET", "/health", "health")
    route("GET", f"/accounts/{ACCOUNT}/login/status", "login_status_needs_sms")
    health = make_publisher().health()
    assert health.status == "needs_relogin"


@respx.mock
def test_health_degraded_when_service_down():
    """上传器没起只算 degraded：误判成 needs_relogin 会白白挂起排期还催人扫码。"""
    respx.get(f"{BASE}/health").mock(side_effect=httpx.ConnectError("down"))
    health = make_publisher().health()
    assert health.status == "degraded"
    assert "上传器" in health.detail


@respx.mock
def test_health_identity_mismatch_is_degraded_not_banned():
    """identity 不符要醒目告警，但**不能**自动置 banned——那是不可逆的人工终态。"""
    route("GET", "/health", "health")
    route("GET", f"/accounts/{ACCOUNT}/login/status", "login_status_ok")
    health = make_publisher(identity_hint="别的号").health()
    assert health.status == "degraded"
    assert "identity 不符" in health.detail


@respx.mock
def test_health_warns_when_identity_hint_missing():
    route("GET", "/health", "health")
    route("GET", f"/accounts/{ACCOUNT}/login/status", "login_status_ok")
    health = make_publisher(identity_hint="").health()
    assert health.status == "degraded"
    assert "identity_hint" in health.detail


# ================================================================== metrics


@respx.mock
def test_fetch_metrics_from_data_center():
    route("GET", f"/accounts/{ACCOUNT}/metrics/{POST_ID}", "metrics")
    metrics = make_publisher().fetch_metrics(POST_ID)
    assert metrics["available"] is True
    assert metrics["views"] == 12000
    assert metrics["likes"] == 480
    assert metrics["platform"] == "douyin"
    assert metrics["platform_post_id"] == POST_ID
    # 抖音数据中心不单列收藏，缺就是缺，不能伪造 0
    assert metrics["collects"] is None


@respx.mock
def test_fetch_metrics_placeholder_is_unavailable():
    metrics = make_publisher().fetch_metrics(UNRESOLVED_PREFIX + "abc")
    assert metrics["available"] is False
    assert all(metrics[k] is None for k in ("views", "likes", "comments", "shares"))
    assert "占位" in metrics["reason"]


@respx.mock
def test_fetch_metrics_unavailable_reports_reason():
    route("GET", f"/accounts/{ACCOUNT}/metrics/{POST_ID}", "metrics_unavailable")
    metrics = make_publisher().fetch_metrics(POST_ID)
    assert metrics["available"] is False
    assert metrics["reason"]


@respx.mock
def test_fetch_metrics_for_title_recovers_post_id():
    route("GET", f"/accounts/{ACCOUNT}/recent_posts", "recent_posts")
    route("GET", f"/accounts/{ACCOUNT}/metrics/{POST_ID}", "metrics")
    metrics = make_publisher().fetch_metrics_for_title(TITLE)
    assert metrics["available"] is True
    assert metrics["platform_post_id"] == POST_ID


# ============================================================ 人工介入通道


def test_get_login_qrcode_is_refused():
    """红线 + 体验：抖音二维码在宿主机窗口里，core 不代理，也不给假图骗人扫。"""
    with pytest.raises(PermanentError, match="宿主机"):
        make_publisher(client=StubDouyinServiceClient()).get_login_qrcode()


@respx.mock
def test_start_login_opens_host_window():
    route("POST", f"/accounts/{ACCOUNT}/login/start", "login_start_waiting")
    info = make_publisher().start_login()
    assert info["state"] == "waiting_user"
    assert "扫码" in info["detail"]


@respx.mock
def test_submit_sms_code_forwards_to_service():
    """只转发、只填写，不识别。"""
    sms = route("POST", f"/accounts/{ACCOUNT}/sms_code", "sms_code_ok")
    assert make_publisher().submit_sms_code("135790") is True
    assert json.loads(sms.calls.last.request.content) == {"code": "135790"}


@respx.mock
def test_submit_sms_code_failure_raises_needs_relogin():
    route("POST", f"/accounts/{ACCOUNT}/sms_code", "sms_code_no_input")
    with pytest.raises(NeedsReloginError, match="no_sms_input"):
        make_publisher().submit_sms_code("135790")


# ============================================================ 登录巡检 + 节流


@respx.mock
def test_tick_login_health_covers_douyin(notifier):
    route("GET", "/health", "health")
    route("GET", f"/accounts/{ACCOUNT}/login/status", "login_status_logged_out")
    with db.session_scope() as session:
        make_account(session, account_id=ACCOUNT, platform="douyin")
    from publishers.registry import register

    register("douyin", lambda account_id, **kw: make_publisher())

    stats = tick_login_health(platforms=["douyin"], notifier=notifier)

    assert stats["checked"] == 1
    assert stats["needs_relogin"] == 1
    with db.session_scope() as session:
        assert session.get(Account, ACCOUNT).status == AccountStatus.NEEDS_RELOGIN
    assert any("需重登" in title for _lvl, title, _text in notifier.sent)


def test_tick_login_health_throttles_douyin(monkeypatch, notifier):
    """抖音巡检要开有头浏览器，比小红书贵得多：默认 30 分钟才巡一次。"""
    calls: list[list[str] | None] = []

    def fake_check(session, *, platforms=None, notifier=None):
        calls.append(list(platforms) if platforms else None)
        return {"checked": 0}

    monkeypatch.setattr("publishers.xhs.login.check_accounts", fake_check)

    first = tick_login_health(now=NOW)
    second = tick_login_health(now=NOW + timedelta(minutes=5))
    third = tick_login_health(now=NOW + timedelta(minutes=45))

    assert "douyin" in calls[0]
    assert "douyin" not in calls[1], "5 分钟后不该再开一次浏览器"
    assert second["douyin_throttled"] == 1
    assert "douyin" in calls[2], "过了 30 分钟应当恢复巡检"
    assert "douyin_throttled" not in first
    assert "douyin_throttled" not in third


def test_tick_login_health_force_skips_throttle(monkeypatch):
    calls: list[list[str] | None] = []

    def fake_check(session, *, platforms=None, notifier=None):
        calls.append(list(platforms) if platforms else None)
        return {"checked": 0}

    monkeypatch.setattr("publishers.xhs.login.check_accounts", fake_check)
    tick_login_health(now=NOW)
    tick_login_health(now=NOW + timedelta(minutes=1), force=True)
    assert "douyin" in calls[1]


# ======================================================= 与两阶段幂等发布集成


def _seed_item(session, bundle: ContentBundle) -> ContentItem:
    account = session.get(Account, ACCOUNT)
    if account is None:
        account = make_account(session, account_id=ACCOUNT, platform="douyin")
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


def test_publish_with_idempotency_records_post_id(tmp_path, notifier):
    stub = StubDouyinServiceClient()
    publisher = make_publisher(client=stub)
    bundle = make_bundle(make_video(tmp_path))
    with db.session_scope() as session:
        item = _seed_item(session, bundle)
        result = publish_with_idempotency(session, item, publisher, notifier=notifier)
        assert result.ok and result.platform_post_id.startswith("7")
        assert item.status == ContentStatus.PUBLISHED
        record = session.scalars(select(PublishRecord)).one()
        assert record.phase == PublishPhase.DONE
        assert record.platform_post_id == result.platform_post_id
    assert stub.calls["publish"] == 1


def test_retry_reconciles_instead_of_republishing(tmp_path, notifier):
    """回包丢了但视频其实已发出去：重试前的对账必须命中，绝不能再发一次。"""
    from publishers.douyin.client import RecentPost

    bundle = make_bundle(make_video(tmp_path))
    stub = StubDouyinServiceClient(
        posts=[
            RecentPost(
                title=TITLE,
                post_id="7499999999999999999",
                url="https://www.douyin.com/video/7499999999999999999",
                published_at=datetime.now(UTC) - timedelta(minutes=10),
                raw_time="10分钟前",
            )
        ]
    )
    publisher = make_publisher(client=stub, now=lambda: datetime.now(UTC))
    with db.session_scope() as session:
        item = _seed_item(session, bundle)
        prepared = publisher.prepare(bundle)
        from core.state_machine import make_idem_key

        session.add(
            PublishRecord(
                id="pub_prev",
                content_item_id=item.id,
                idem_key=make_idem_key(ACCOUNT, "douyin", prepared.content_hash, ""),
                phase=PublishPhase.FAILED.value,
                attempts=1,
            )
        )
        session.flush()

        result = publish_with_idempotency(session, item, publisher, notifier=notifier)

        assert result.platform_post_id == "7499999999999999999"
        assert result.raw["reconciled"] is True
        assert item.status == ContentStatus.PUBLISHED
        records = session.scalars(select(PublishRecord)).all()
        assert len(records) == 1 and records[0].phase == PublishPhase.DONE
    assert "publish" not in stub.calls, "对账命中后绝不能再发一次"


def test_collector_repairs_placeholder_post_id(tmp_path):
    """发布时没解析出作品 id，指标兜底命中后要把真 id 回填进发布记录。"""
    from metrics.collector import collect_all
    from publishers.douyin.client import RecentPost

    bundle = make_bundle(make_video(tmp_path))
    # 模拟"发出去了但内容管理页还没刷出来"：发布时只能记占位 id
    stub = StubDouyinServiceClient(resolve_post_id=False, appear_after_publish=False)
    publisher = make_publisher(client=stub, now=lambda: datetime.now(UTC))
    with db.session_scope() as session:
        item = _seed_item(session, bundle)
        result = publish_with_idempotency(session, item, publisher)
        assert is_placeholder_post_id(result.platform_post_id)
        published_at = session.scalars(select(PublishRecord)).one().updated_at

    # 作品随后出现在内容管理页上
    stub.posts = [
        RecentPost(
            title=TITLE,
            post_id="7488888888888888888",
            url="https://www.douyin.com/video/7488888888888888888",
            published_at=datetime.now(UTC) - timedelta(minutes=1),
            raw_time="1分钟前",
        )
    ]
    from publishers.registry import register

    register("douyin", lambda account_id, **kw: publisher)
    stats = collect_all()

    assert stats["snapshots"] == 1
    with db.session_scope() as session:
        record = session.scalars(select(PublishRecord)).one()
        assert record.platform_post_id == "7488888888888888888"
        # 回填不能顺手把"发布时刻"改掉，否则 24h/7d 窗口整体后移
        assert record.updated_at == published_at


# ==================================================== 宿主机上传器：选择器 / 截图


def test_load_selectors_merges_override_file(tmp_path, monkeypatch):
    """平台改版时改 JSON 就行，不用改代码、不用重新发版。"""
    from publishers.douyin.service import SELECTORS, load_selectors

    override = tmp_path / "selectors.json"
    override.write_text(
        json.dumps({"nickname": ["#my-name"], "publish_button": "#go"}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setenv("DOUYIN_SELECTORS_FILE", str(override))
    merged = load_selectors()
    assert merged["nickname"] == ["#my-name"]
    assert merged["publish_button"] == ["#go"]  # 字符串也接受，自动包成单元素列表
    # 没覆盖的项保持内置值，且内置表本身不被改写
    assert merged["title_input"] == SELECTORS["title_input"]


def test_load_selectors_ignores_broken_file(tmp_path, monkeypatch):
    from publishers.douyin.service import SELECTORS, load_selectors

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("DOUYIN_SELECTORS_FILE", str(broken))
    assert load_selectors()["nickname"] == SELECTORS["nickname"]
    monkeypatch.setenv("DOUYIN_SELECTORS_FILE", str(tmp_path / "missing.json"))
    assert load_selectors()["nickname"] == SELECTORS["nickname"]


def test_shooter_path_is_sanitised(tmp_path):
    from publishers.douyin.service import Shooter

    shots = Shooter(tmp_path)
    path = shots.path_for("../evil/acc", "publish done", now=NOW)
    assert path.parent == tmp_path / ".._evil_acc"
    assert path.name.startswith("20260816T120000")
    assert path.name.endswith("-publish_done.png")


def test_pacer_sleeps_within_range():
    from publishers.douyin.service import Pacer

    slept: list[float] = []
    pace = Pacer(sleeper=slept.append)
    for _ in range(20):
        pace()
    assert all(0.8 <= s <= 2.5 for s in slept)


def test_parse_card_time_handles_both_formats():
    from publishers.douyin.client import parse_rfc3339
    from publishers.douyin.service import parse_card_time

    absolute = parse_card_time("2026-08-16 18:30")
    assert absolute
    # 页面写的是宿主机本地时区，折算回本地应当还原成同一个墙上时间
    local = parse_rfc3339(absolute).astimezone()
    assert (local.year, local.month, local.day, local.hour, local.minute) == (
        2026,
        8,
        16,
        18,
        30,
    )

    relative = parse_rfc3339(parse_card_time("3小时前"))
    delta = datetime.now(UTC) - relative
    assert timedelta(hours=2, minutes=55) < delta < timedelta(hours=3, minutes=5)

    assert parse_card_time("说不清什么时候") == ""
    assert parse_card_time("") == ""


# ============================================ 宿主机上传器：浏览器启动参数红线


class _FakeContext:
    def __init__(self) -> None:
        self.pages: list[Any] = []
        self.closed = False

    def new_page(self) -> Any:
        page = object()
        self.pages.append(page)
        return page

    def close(self) -> None:
        self.closed = True


class _FakeChromium:
    def __init__(self, *, fail_on_channel: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_on_channel = fail_on_channel

    def launch_persistent_context(self, **kwargs: Any) -> _FakeContext:
        self.calls.append(dict(kwargs))
        if self.fail_on_channel and "channel" in kwargs:
            raise RuntimeError("Chrome 没装")
        return _FakeContext()


class _FakePlaywright:
    def __init__(self, *, fail_on_channel: bool = False) -> None:
        self.chromium = _FakeChromium(fail_on_channel=fail_on_channel)
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def test_browser_pool_launch_is_headful_and_carries_no_stealth(tmp_path):
    """红线回归测试（docs/POLICY.md）：

    - ``headless`` 必须是 False，且没有任何开关能把它打开；
    - 除 ``user_data_dir`` / ``channel`` 外不传任何启动参数——
      **不设 user_agent、不加 args、不注入脚本、不改指纹**。
    """
    from publishers.douyin.service import BrowserPool

    pool = BrowserPool(profile_root=tmp_path, channel="chrome")
    pool._pw = _FakePlaywright()
    pool.context("acc-1")

    kwargs = pool._pw.chromium.calls[0]
    assert kwargs["headless"] is False
    assert kwargs["channel"] == "chrome"
    assert set(kwargs) == {"user_data_dir", "headless", "channel"}
    forbidden = {"user_agent", "args", "ignore_default_args", "extra_http_headers", "proxy"}
    assert not forbidden & set(kwargs)


def test_browser_pool_one_profile_per_account(tmp_path):
    from publishers.douyin.service import BrowserPool

    pool = BrowserPool(profile_root=tmp_path)
    pool._pw = _FakePlaywright()
    pool.context("acc-1")
    pool.context("acc-2")
    dirs = [call["user_data_dir"] for call in pool._pw.chromium.calls]
    assert dirs == [str(tmp_path / "acc-1"), str(tmp_path / "acc-2")]
    assert len(set(dirs)) == 2, "两个账号绝不能共用 profile（Cookie 池红线）"
    # 同一账号复用同一个 context，不会重复起浏览器
    pool.context("acc-1")
    assert len(pool._pw.chromium.calls) == 2
    assert pool.launched() == ["acc-1", "acc-2"]


def test_browser_pool_falls_back_to_bundled_chromium(tmp_path):
    from publishers.douyin.service import BrowserPool

    pool = BrowserPool(profile_root=tmp_path, channel="chrome")
    pool._pw = _FakePlaywright(fail_on_channel=True)
    pool.context("acc-1")
    calls = pool._pw.chromium.calls
    assert "channel" in calls[0] and "channel" not in calls[1]
    assert calls[1]["headless"] is False  # 回退路径同样必须有头


def test_browser_pool_rejects_weird_account_id(tmp_path):
    from publishers.douyin.service import BrowserPool

    with pytest.raises(ValueError, match="非法 account_id"):
        BrowserPool(profile_root=tmp_path).profile_dir("///")


# ================================================ 宿主机上传器：串行 worker


class _FakePage:
    """只实现服务端点真正会用到的那几个页面方法。"""

    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        self.url = "https://creator.douyin.com/creator-micro/content/upload"
        self.visited: list[str] = []

    def goto(self, url: str, **kwargs: Any) -> None:
        self.visited.append(url)
        self.url = url


class _FakePool:
    """给 BrowserWorker 用的假浏览器池。"""

    def __init__(self, *, has_page: bool = True) -> None:
        self.has_page = has_page
        self.closed = False
        self.channel = "chrome"
        self.profile_root = Path("profiles/douyin")
        self._pages: dict[str, _FakePage] = {}

    def page(self, account_id: str) -> Any:
        return self._pages.setdefault(account_id, _FakePage(account_id))

    def existing_page(self, account_id: str) -> Any | None:
        return self.page(account_id) if self.has_page else None

    def launched(self) -> list[str]:
        return []

    def close(self) -> None:
        self.closed = True


def test_worker_runs_jobs_and_propagates_errors():
    from publishers.douyin.service import BrowserWorker

    worker = BrowserWorker(_FakePool())
    try:
        assert worker.submit(lambda pool: 42, timeout=5) == 42

        def boom(pool: Any) -> None:
            raise ValueError("炸了")

        with pytest.raises(ValueError, match="炸了"):
            worker.submit(boom, timeout=5)
    finally:
        worker.shutdown()


def test_worker_serialises_browser_jobs():
    """同平台串行是红线：两个作业不允许交错执行。"""
    import threading
    import time as _time

    from publishers.douyin.service import BrowserWorker

    events: list[str] = []

    def slow(tag: str):
        def job(pool: Any) -> str:
            events.append(f"{tag}-start")
            _time.sleep(0.05)
            events.append(f"{tag}-end")
            return tag

        return job

    worker = BrowserWorker(_FakePool())
    try:
        threads = [
            threading.Thread(target=lambda t=tag: worker.submit(slow(t), timeout=5))
            for tag in ("a", "b", "c")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
    finally:
        worker.shutdown()

    assert len(events) == 6
    # 每个 start 后面紧跟自己的 end，说明没有交错
    for index in range(0, 6, 2):
        assert events[index].split("-")[0] == events[index + 1].split("-")[0]


def test_worker_rejects_when_busy():
    import threading

    from publishers.douyin.service import BrowserWorker, ServiceBusy

    started = threading.Event()
    release = threading.Event()

    def blocking(pool: Any) -> str:
        started.set()
        release.wait(timeout=5)
        return "done"

    worker = BrowserWorker(_FakePool())
    background = threading.Thread(target=lambda: worker.submit(blocking, timeout=10))
    try:
        background.start()
        assert started.wait(timeout=5)
        with pytest.raises(ServiceBusy):
            worker.submit(lambda pool: "second", timeout=5, reject_if_busy=True)
    finally:
        release.set()
        background.join(timeout=10)
        worker.shutdown()


# ============================================ 宿主机上传器：HTTP 层（无浏览器）


class _FakeAutomation:
    """假页面动作：只回罐头 envelope，用来测服务的 HTTP 外壳与串行调度。"""

    def __init__(self, account_id: str) -> None:
        from publishers.douyin.service import Pacer

        self.account_id = account_id
        self.published: list[Any] = []
        self.codes: list[str] = []
        self.pace = Pacer(sleeper=lambda _s: None)

    def login_state(self, page: Any) -> dict[str, Any]:
        return {"ok": True, "state": "logged_in", "nickname": "通***所", "detail": "已登录"}

    def publish(self, page: Any, req: Any) -> dict[str, Any]:
        self.published.append(req)
        return {
            "ok": True,
            "state": "published",
            "post_id": POST_ID,
            "url": f"https://www.douyin.com/video/{POST_ID}",
            "screenshot_path": "/tmp/shot.png",
        }

    def fill_sms_code(self, page: Any, code: str) -> dict[str, Any]:
        self.codes.append(code)
        return {"ok": True, "state": "ok", "detail": "验证码已填入页面"}

    def read_recent_posts(self, page: Any, *, limit: int = 20) -> list[dict[str, Any]]:
        return [{"title": TITLE, "post_id": POST_ID, "url": "", "raw_time": "刚刚"}][:limit]

    def read_metrics(self, page: Any, post_id: str) -> dict[str, Any]:
        return {"available": True, "views": 1, "likes": 2, "comments": 3, "shares": 4}


def _service_client(tmp_path, *, has_page: bool = True):
    from fastapi.testclient import TestClient

    from publishers.douyin.service import BrowserWorker, create_app

    automations: dict[str, _FakeAutomation] = {}

    def factory(account_id: str) -> _FakeAutomation:
        return automations.setdefault(account_id, _FakeAutomation(account_id))

    app = create_app(
        profile_root=tmp_path / "profiles",
        screenshot_root=tmp_path / "shots",
        worker=BrowserWorker(_FakePool(has_page=has_page)),
        automation_factory=factory,  # type: ignore[arg-type]
    )
    return TestClient(app), automations


def test_service_health_reports_headful(tmp_path):
    client, _ = _service_client(tmp_path)
    with client:
        body = client.get("/health").json()
    assert body["ok"] is True
    assert body["headless"] is False, "红线：上传器永远有头"
    assert body["service"] == "douyin-uploader"


def test_service_publish_endpoint(tmp_path):
    video = make_video(tmp_path)
    client, automations = _service_client(tmp_path)
    with client:
        resp = client.post(
            f"/accounts/{ACCOUNT}/publish",
            json={
                "title": TITLE,
                "description": "正文",
                "video_path": str(video),
                "hashtags": ["通勤"],
                "identity_hint": IDENTITY,
            },
        )
    body = resp.json()
    assert resp.status_code == 200
    assert body["state"] == "published"
    assert body["post_id"] == POST_ID
    req = automations[ACCOUNT].published[0]
    assert req.identity_hint == IDENTITY


def test_service_publish_rejects_missing_video(tmp_path):
    client, _ = _service_client(tmp_path)
    with client:
        body = client.post(
            f"/accounts/{ACCOUNT}/publish",
            json={"title": TITLE, "video_path": str(tmp_path / "nope.mp4")},
        ).json()
    assert body["ok"] is False
    assert body["state"] == "invalid_content"


def test_service_sms_code_needs_an_open_window(tmp_path):
    client, automations = _service_client(tmp_path, has_page=False)
    with client:
        body = client.post(f"/accounts/{ACCOUNT}/sms_code", json={"code": "135790"}).json()
    assert body["state"] == "no_browser"
    assert automations[ACCOUNT].codes == [], "没开窗口时不该在任何页面上填东西"


def test_service_sms_code_fills_the_input(tmp_path):
    client, automations = _service_client(tmp_path)
    with client:
        body = client.post(f"/accounts/{ACCOUNT}/sms_code", json={"code": "135790"}).json()
    assert body["ok"] is True
    assert automations[ACCOUNT].codes == ["135790"]


def test_service_recent_posts_and_metrics(tmp_path):
    client, _ = _service_client(tmp_path)
    with client:
        posts = client.get(f"/accounts/{ACCOUNT}/recent_posts", params={"limit": 5}).json()
        metrics = client.get(f"/accounts/{ACCOUNT}/metrics/{POST_ID}").json()
    assert posts["posts"][0]["title"] == TITLE
    assert metrics["metrics"]["views"] == 1


def test_service_survives_page_exceptions(tmp_path):
    """页面炸了要变成 envelope，绝不能让常驻服务 500 甚至挂掉。"""
    from fastapi.testclient import TestClient

    from publishers.douyin.service import BrowserWorker, Pacer, create_app

    class Boom:
        def __init__(self, account_id: str) -> None:
            self.account_id = account_id
            self.pace = Pacer(sleeper=lambda _s: None)

        def login_state(self, page: Any) -> dict[str, Any]:
            raise RuntimeError("页面没了")

    app = create_app(
        profile_root=tmp_path / "profiles",
        screenshot_root=tmp_path / "shots",
        worker=BrowserWorker(_FakePool()),
        automation_factory=Boom,  # type: ignore[arg-type]
    )
    with TestClient(app) as client:
        body = client.get(f"/accounts/{ACCOUNT}/login/status").json()
    assert body["ok"] is False
    assert body["state"] == "browser_error"
    assert "页面没了" in body["detail"]


# ==================================== 真实浏览器冒烟：假创作者中心页面（-m browser）


def _sync_playwright():
    """优先用 patchright（生产用它），没装就退回 playwright；都没有则跳过。"""
    try:
        from patchright.sync_api import sync_playwright
    except ImportError:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return None
    return sync_playwright


@pytest.mark.browser
def test_automation_smoke_on_fake_creator_center(tmp_path):
    """用**真实浏览器**驱动一张本地假页面，验证三条逻辑链：

    1. 能从页面读出昵称；
    2. identity 闸门：昵称对不上就中止，并把截图落到磁盘；
    3. 验证码**只填写不识别**——输入框没出现时如实报 ``no_sms_input``，
       出现后把人给的那串数字填进去，不做任何图像/模型识别。

    注意：这里是本地静态 HTML，跟抖音无关，所以用无头浏览器跑（CI 里没有显示器）。
    **生产路径完全不同**：``BrowserPool`` 把 ``headless`` 写死 False，见
    ``test_browser_pool_launch_is_headful_and_carries_no_stealth``。
    """
    runner = _sync_playwright()
    if runner is None:
        pytest.skip("未安装 patchright / playwright（uv sync --extra douyin）")

    from publishers.douyin.service import DouyinAutomation, Pacer, Shooter

    page_file = FIXTURES / "fake_creator_center.html"
    shots = Shooter(tmp_path / "shots")
    automation = DouyinAutomation(
        account_id=ACCOUNT,
        pacer=Pacer(sleeper=lambda _s: None),  # 测试里不必真的等
        shooter=shots,
    )

    try:
        playwright = runner().start()
    except Exception as exc:  # pragma: no cover - 取决于本机是否装了浏览器二进制
        pytest.skip(f"浏览器起不来（跑 `uv run patchright install chromium`）：{exc}")
    try:
        try:
            browser = playwright.chromium.launch()
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"chromium 二进制缺失：{exc}")
        page = browser.new_page()
        page.goto(page_file.as_uri())

        # 1. 读昵称
        assert automation.read_nickname(page) == "抖音测试号 01"
        assert automation.login_state(page)["state"] == "logged_in"

        # 2. identity 闸门：对得上放行，对不上中止并截图
        assert automation.check_identity(page, "抖音测试号 01") is None
        blocked = automation.check_identity(page, "别人的号")
        assert blocked is not None
        assert blocked["state"] == STATE_IDENTITY_MISMATCH
        shot = Path(blocked["screenshot_path"])
        assert shot.is_file() and shot.stat().st_size > 0
        assert shot.parent == tmp_path / "shots" / ACCOUNT
        # 截图文件名里不含内容标题，只有账号 / 步骤 / 时间戳
        assert TITLE not in shot.name

        # 3. 验证码通道：没有输入框时如实报，不去"想办法"
        assert automation.fill_sms_code(page, "135790")["state"] == "no_sms_input"
        # 直接改 DOM 而不是调页面里定义的 window.showVerify()：patchright 的
        # page.evaluate 跑在**隔离世界**里（它不开 CDP Runtime.enable），
        # 页面自己定义的全局函数在那里看不见，但 DOM 是共享的。
        page.evaluate("document.getElementById('verify').classList.remove('hidden')")
        result = automation.fill_sms_code(page, "135790")
        assert result["ok"] is True
        assert page.input_value("#sms") == "135790"

        browser.close()
    finally:
        playwright.stop()
