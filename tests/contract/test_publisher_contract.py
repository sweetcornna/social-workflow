"""发布契约测试（参数化）。

**任何 Publisher 实现都必须通过本文件**。P1/P2/P3 完成后，把新实现的工厂追加到
``PUBLISHER_FACTORIES`` 即可，工厂必须构造出"不触发真实平台写操作"的实例
（dry_run=True 或指向 VCR/本地 stub）。
"""

from __future__ import annotations

import base64
import inspect
from collections.abc import Callable
from datetime import datetime

import pytest
from pydantic import ValidationError

from metrics.availability import MetricsPayloadKind, normalize_metrics_payload
from publishers.base import (
    ACCOUNT_STATUSES,
    AccountHealth,
    ContentBundle,
    FakePublisher,
    MediaAsset,
    NeedsReloginError,
    PermanentError,
    Publisher,
    PublishError,
    PublishResult,
    RetryableError,
    SupportsInteractiveLogin,
)
from publishers.douyin.publisher import DouyinPublisher, MinIntervalGate
from publishers.douyin.stub import StubDouyinServiceClient
from publishers.wechat_mp.publisher import WechatMpPublisher
from publishers.wechat_mp.stub import StubWechatMpClient
from publishers.wechat_mp.wenyan_backend import WenyanBackend, WenyanWechatMpPublisher
from publishers.xhs.publisher import XhsPublisher
from publishers.xhs.stub import StubXhsMcpClient

PublisherFactory = Callable[[], Publisher]


def _wenyan_publisher() -> Publisher:
    """wenyan 后端：dry_run 保证既不起子进程也不联网。"""
    return WenyanWechatMpPublisher(
        "acc-1",
        dry_run=True,
        client=StubWechatMpClient(),
        backend=WenyanBackend(app_id="a", app_secret="b", dry_run=True),
    )


def _xhs_publisher(*, dry_run: bool = False) -> Publisher:
    """小红书：stub 客户端不联网、不碰文件系统。

    限频器传一个**独立实例**（而不是调度器的进程内单例），否则契约用例里的多次
    publish 会消耗真实账号的日配额计数，跨用例互相干扰。
    """
    from core.scheduler import RateLimiter

    return XhsPublisher(
        "acc-1",
        dry_run=dry_run,
        client=StubXhsMcpClient(dry_run=dry_run),
        limiter=RateLimiter(min_interval_seconds=0),
        daily_limit=1000,
        resolve_attempts=1,
        sleeper=lambda _seconds: None,
    )


def _douyin_publisher(*, dry_run: bool = False) -> Publisher:
    """抖音：stub 客户端不联网、不起浏览器、不碰文件系统。

    限频器与最小间隔闸门都传**独立实例**：默认那两个是进程内单例，
    契约用例里的多次 publish 会吃掉真实账号的日配额（抖音只有 2 条/天）。
    """
    from core.scheduler import RateLimiter

    return DouyinPublisher(
        "acc-1",
        dry_run=dry_run,
        client=StubDouyinServiceClient(dry_run=dry_run),
        limiter=RateLimiter(min_interval_seconds=0),
        gate=MinIntervalGate(),
        min_interval_minutes=0,
        daily_limit=1000,  # 会被 DAILY_LIMIT_CEILING 夹到 10，足够契约用例跑
        identity_hint="抖音 Stub 账号",
    )


PUBLISHER_FACTORIES: list[pytest.param] = [
    pytest.param(lambda: FakePublisher("acc-1", platform="xhs"), id="fake-xhs"),
    pytest.param(lambda: FakePublisher("acc-1", platform="wechat_mp"), id="fake-wechat_mp"),
    pytest.param(lambda: FakePublisher("acc-1", platform="douyin"), id="fake-douyin"),
    pytest.param(
        lambda: FakePublisher("acc-1", platform="xhs", dry_run=True), id="fake-xhs-dryrun"
    ),
    # P1 真实实现：stub 客户端走完整成功路径，dry_run 走 ok=False 路径
    pytest.param(
        lambda: WechatMpPublisher("acc-1", client=StubWechatMpClient()), id="wechat_mp-stub"
    ),
    pytest.param(
        lambda: WechatMpPublisher("acc-1", dry_run=True, client=StubWechatMpClient()),
        id="wechat_mp-dryrun",
    ),
    pytest.param(_wenyan_publisher, id="wechat_mp-wenyan-dryrun"),
    # P2 真实实现：stub 客户端走完整成功路径（含"发布后对账取 note_id"），
    # dry_run 走 ok=False 路径
    pytest.param(_xhs_publisher, id="xhs-stub"),
    pytest.param(lambda: _xhs_publisher(dry_run=True), id="xhs-dryrun"),
    # P3 真实实现：stub 客户端不联网、不起浏览器、不碰文件系统
    pytest.param(_douyin_publisher, id="douyin-stub"),
    pytest.param(lambda: _douyin_publisher(dry_run=True), id="douyin-dryrun"),
]


def sample_bundle(platform: str = "xhs") -> ContentBundle:
    media = [
        MediaAsset(path="data/demo/cover.png", kind="image", cover=True),
        MediaAsset(path="data/demo/p2.png", kind="image"),
    ]
    if platform == "douyin":
        # 抖音是视频平台：内容包必须带且只带一个成片，封面可选
        media = [
            MediaAsset(path="data/demo/clip.mp4", kind="video"),
            MediaAsset(path="data/demo/cover.png", kind="image", cover=True),
        ]
    return ContentBundle(
        id="itm-contract",
        account_id="acc-1",
        platform=platform,  # type: ignore[arg-type]
        title="  契约测试标题  ",
        body_markdown="正文内容\n第二行",
        media=media,
        tags=["#标签A", "标签A", "标签B", " "],
        platform_extra={"foo": "bar"},
    )


# ------------------------------------------------------------------ 结构契约


@pytest.mark.parametrize("factory", PUBLISHER_FACTORIES)
def test_implements_abstract_interface(factory: PublisherFactory) -> None:
    publisher = factory()
    assert isinstance(publisher, Publisher)
    for name in ("prepare", "publish", "health", "fetch_metrics", "reconcile"):
        assert callable(getattr(publisher, name)), f"缺少方法 {name}"
    assert isinstance(publisher.dry_run, bool), "dry_run 必须是 bool 属性"
    assert isinstance(publisher.platform, str) and publisher.platform


@pytest.mark.parametrize("factory", PUBLISHER_FACTORIES)
def test_signatures_match_contract(factory: PublisherFactory) -> None:
    """签名不得私自扩展必填参数，否则调度器无法通用调用。"""
    publisher = factory()
    assert list(inspect.signature(publisher.prepare).parameters) == ["bundle"]
    assert list(inspect.signature(publisher.publish).parameters) == ["bundle"]
    assert list(inspect.signature(publisher.health).parameters) == []
    assert list(inspect.signature(publisher.fetch_metrics).parameters) == ["platform_post_id"]
    assert list(inspect.signature(publisher.reconcile).parameters) == ["bundle"]


# ------------------------------------------------------------------- prepare


@pytest.mark.parametrize("factory", PUBLISHER_FACTORIES)
def test_prepare_is_idempotent(factory: PublisherFactory) -> None:
    publisher = factory()
    bundle = sample_bundle(publisher.platform)
    once = publisher.prepare(bundle)
    twice = publisher.prepare(once)
    assert once.model_dump() == twice.model_dump(), "prepare 必须幂等"


@pytest.mark.parametrize("factory", PUBLISHER_FACTORIES)
def test_prepare_preserves_identity(factory: PublisherFactory) -> None:
    publisher = factory()
    bundle = sample_bundle(publisher.platform)
    prepared = publisher.prepare(bundle)
    assert isinstance(prepared, ContentBundle)
    assert prepared.id == bundle.id
    assert prepared.account_id == bundle.account_id
    assert prepared.platform == bundle.platform
    assert prepared.body_markdown == bundle.body_markdown


@pytest.mark.parametrize("factory", PUBLISHER_FACTORIES)
def test_prepare_does_not_mutate_input(factory: PublisherFactory) -> None:
    publisher = factory()
    bundle = sample_bundle(publisher.platform)
    before = bundle.model_dump()
    publisher.prepare(bundle)
    assert bundle.model_dump() == before, "prepare 不得原地修改入参"


# ------------------------------------------------------------------- publish


@pytest.mark.parametrize("factory", PUBLISHER_FACTORIES)
def test_publish_returns_publish_result(factory: PublisherFactory) -> None:
    publisher = factory()
    bundle = publisher.prepare(sample_bundle(publisher.platform))
    result = publisher.publish(bundle)
    assert isinstance(result, PublishResult)
    assert isinstance(result.ok, bool)
    assert isinstance(result.raw, dict)
    if publisher.dry_run:
        assert result.ok is False, "dry_run 下不得声称发布成功"
        assert result.platform_post_id is None
    else:
        assert result.ok is True
        assert result.platform_post_id, "发布成功必须回传 platform_post_id"
        assert isinstance(result.published_at, datetime)


@pytest.mark.parametrize("factory", PUBLISHER_FACTORIES)
def test_publish_is_deterministic_for_same_bundle(factory: PublisherFactory) -> None:
    """同一内容包重复调用得到同一个 post_id（便于对账），或至少结构一致。"""
    publisher = factory()
    bundle = publisher.prepare(sample_bundle(publisher.platform))
    first = publisher.publish(bundle)
    second = publisher.publish(bundle)
    assert first.ok == second.ok


# -------------------------------------------------------------------- health


@pytest.mark.parametrize("factory", PUBLISHER_FACTORIES)
def test_health_returns_enum_status(factory: PublisherFactory) -> None:
    publisher = factory()
    health = publisher.health()
    assert isinstance(health, AccountHealth)
    assert health.status in ACCOUNT_STATUSES
    assert isinstance(health.detail, str)


# -------------------------------------------------------------- fetch_metrics


@pytest.mark.parametrize("factory", PUBLISHER_FACTORIES)
def test_fetch_metrics_returns_dict(factory: PublisherFactory) -> None:
    publisher = factory()
    metrics = publisher.fetch_metrics("post-123")
    assert isinstance(metrics, dict)
    assert all(type(key) is str for key in metrics)
    kind, copied = normalize_metrics_payload(metrics)
    assert kind is not MetricsPayloadKind.MALFORMED
    assert copied is not None


# ----------------------------------------------------------------- reconcile


@pytest.mark.parametrize("factory", PUBLISHER_FACTORIES)
def test_reconcile_returns_none_or_result(factory: PublisherFactory) -> None:
    publisher = factory()
    bundle = publisher.prepare(sample_bundle(publisher.platform))
    out = publisher.reconcile(bundle)
    assert out is None or isinstance(out, PublishResult)


def test_reconcile_hit_reports_post_id() -> None:
    """对账命中必须带 platform_post_id，否则调用方无法补记录。"""
    hit = PublishResult(ok=True, platform_post_id="p-9", url="https://example.invalid/p-9")
    publisher = FakePublisher("acc-1", reconcile_result=hit)
    out = publisher.reconcile(sample_bundle())
    assert out is not None and out.ok and out.platform_post_id == "p-9"


def test_reconcile_miss_returns_none() -> None:
    publisher = FakePublisher("acc-1")
    assert publisher.reconcile(sample_bundle()) is None


# ------------------------------------------------------------------ 异常分类


def test_exception_hierarchy() -> None:
    for exc in (RetryableError, NeedsReloginError, PermanentError):
        assert issubclass(exc, PublishError)
    assert not issubclass(PublishError, RetryableError)


@pytest.mark.parametrize(
    "exc",
    [RetryableError("502"), NeedsReloginError("cookie 过期"), PermanentError("内容违规")],
    ids=["retryable", "needs_relogin", "permanent"],
)
def test_publish_failures_raise_publish_error(exc: PublishError) -> None:
    """失败必须抛 PublishError 子类，不允许用 ok=False 表达异常。"""
    publisher = FakePublisher("acc-1", raise_exc=exc)
    with pytest.raises(PublishError) as caught:
        publisher.publish(sample_bundle())
    assert type(caught.value) is type(exc)


def test_retryable_error_carries_retry_after() -> None:
    exc = RetryableError("限频", retry_after=30.0)
    assert exc.retry_after == 30.0
    assert exc.raw == {}


# ------------------------------------------------------------------- DTO 契约


def test_content_hash_covers_title_body_and_media() -> None:
    base = sample_bundle()
    assert base.content_hash == sample_bundle().content_hash
    assert base.content_hash != base.model_copy(update={"title": "别的标题"}).content_hash
    assert base.content_hash != base.model_copy(update={"body_markdown": "别的正文"}).content_hash
    assert (
        base.content_hash
        != base.model_copy(update={"media": [MediaAsset(path="other.png")]}).content_hash
    )


def test_content_hash_ignores_tags_and_platform_extra() -> None:
    """改标签/平台字段不算新内容，避免幂等键抖动导致重复发布。"""
    base = sample_bundle()
    assert base.content_hash == base.model_copy(update={"tags": ["完全不同"]}).content_hash
    assert base.content_hash == base.model_copy(update={"platform_extra": {"x": 1}}).content_hash


def test_bundle_rejects_unknown_fields() -> None:
    """契约冻结：扩展只能走 platform_extra。"""
    with pytest.raises(ValidationError):
        ContentBundle.model_validate(
            {
                "id": "i",
                "account_id": "a",
                "platform": "xhs",
                "title": "t",
                "body_markdown": "b",
                "私自加的字段": 1,
            }
        )


def test_bundle_rejects_unknown_platform() -> None:
    with pytest.raises(ValidationError):
        ContentBundle.model_validate(
            {
                "id": "i",
                "account_id": "a",
                "platform": "weibo",
                "title": "t",
                "body_markdown": "b",
            }
        )


def test_cover_selection() -> None:
    bundle = sample_bundle()
    assert bundle.cover is not None and bundle.cover.path == "data/demo/cover.png"
    no_cover = bundle.model_copy(update={"media": [MediaAsset(path="a.png", cover=False)]})
    assert no_cover.cover is not None and no_cover.cover.path == "a.png"
    assert bundle.model_copy(update={"media": []}).cover is None


# ------------------------------------------------------- 人工介入通道（可选能力）


def test_interactive_login_returns_png_base64() -> None:
    publisher = FakePublisher("acc-1", platform="xhs")
    assert isinstance(publisher, SupportsInteractiveLogin)
    raw = base64.b64decode(publisher.get_login_qrcode(), validate=True)
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", "必须是合法 PNG"
    assert publisher.check_login_status().status in ACCOUNT_STATUSES


def test_sms_code_is_forwarded_not_recognised() -> None:
    """只做转发通道，不做识别。"""
    publisher = FakePublisher("acc-1")
    assert publisher.submit_sms_code("123456") is True
    assert publisher.sms_codes == ["123456"]
