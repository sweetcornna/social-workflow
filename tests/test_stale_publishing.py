"""崩溃残留在 ``publishing`` 的内容必须有出口（P16.2）。

背景：``publish_with_idempotency`` 在碰平台之前会把 ``PublishRecord(phase=in_flight)``
与 ``item.status=publishing`` **commit** 掉，好让幂等声明真的落盘、崩溃后不重复发布
（见 ``core/state_machine.py`` 的 ``_commit_before_platform``）。代价是进程真的在
publish 中途没了的话，这条内容会停在 ``publishing``——而全仓**没有任何 tick 扫这个
状态**。那是一次静默的流水线停摆：稿子不见了，日志里没有失败，也没人会发现。

``recover_stale_publishing`` 是这条状态的唯一出口。本文件钉三件事，缺一不可：

1. 卡住的内容**捞得回来**（没有它就是永久卡死）；
2. 捞回来**不会重复发布**——重投链先做平台侧对账（这是本次改动的核心风险）；
3. 阈值内**正在正常发布**的内容一根手指都不许碰（误扫比卡住严重得多）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from core import db
from core.models import ContentItem, PublishRecord, ReviewLog
from core.scheduler import (
    STALE_PUBLISHING_MIN_SECONDS,
    recover_stale_publishing,
    stale_publishing_after,
    tick_retry_sweep,
    tick_scheduled_publish,
)
from core.state_machine import ContentStatus, PublishPhase, SystemAction
from publishers.base import ContentBundle, FakePublisher, PermanentError, PublishResult
from publishers.registry import register
from tests.conftest import NO_CONFIRM, make_account, make_item

NOW = datetime.now(UTC).replace(microsecond=0)


class DyingPublisher(FakePublisher):
    """``publish`` 进行到一半进程就没了。

    用 ``SystemExit``（``BaseException``，不是 ``Exception``）演：它绕开
    ``publish_with_idempotency`` 的三个 ``except`` 和 ``tick_scheduled_publish``
    的 ``except PublishError``，谁都来不及收拾——被 ``kill`` / OOM / 断电就是这样。
    """

    def publish(self, bundle: ContentBundle) -> PublishResult:
        self.publish_calls += 1
        raise SystemExit("进程在发布中途没了")


def _register(publisher: FakePublisher) -> FakePublisher:
    register("xhs", lambda account_id, **_: publisher)
    return publisher


def _crash_mid_publish(notifier, *, account_id: str = "acc-stale") -> str:
    """真跑一遍 ``tick_scheduled_publish`` 并在 publish 中途"崩掉"，返回 item_id。

    不手工造状态：手工 ``UPDATE`` 出来的 ``publishing`` 证明不了"真实崩溃会留下这个
    形状"，而这正是整个文件要证的前提。
    """
    _register(DyingPublisher(account_id, platform="xhs"))
    with db.session_scope() as session:
        account = make_account(session, account_id=account_id, daily_limit=9)
        account.extra = {"min_interval_minutes": 0, **NO_CONFIRM}
        item = make_item(session, account, scheduled_in_minutes=-10, now=NOW)
        item_id = item.id

    with pytest.raises(SystemExit):
        tick_scheduled_publish(now=NOW, notifier=notifier)

    with db.session_scope() as session:
        status = session.scalar(select(ContentItem.status).where(ContentItem.id == item_id))
        phase = session.scalar(
            select(PublishRecord.phase).where(PublishRecord.content_item_id == item_id)
        )
    assert status == ContentStatus.PUBLISHING.value, "没造出崩溃残留，后面全白测"
    assert phase == PublishPhase.IN_FLIGHT.value

    # 残留的**形状**是真崩出来的（上面那段），但它的**时刻**必须钉在 NOW 上。
    # 不钉的话：`updated_at` 走的是 models.utcnow() 的真实墙钟，而下面每条用例都拿
    # `NOW ± 阈值 ± 1 分钟` 去卡边界——NOW 是模块导入时刻。整套测试从收集到跑到这个
    # 文件只要超过 1 分钟，墙钟就漂出那 1 分钟的余量，扫描器**正确地**认为"还没到
    # 阈值"，五条用例一起红。本机 53 秒跑完所以一直是绿的，CI 上 2 分 22 秒，
    # 2026-08-25 当场炸了。这不是产品的时区/时钟问题，是夹具把两个时钟混着用。
    with db.session_scope() as session:
        item = session.get(ContentItem, item_id)
        assert item is not None
        item.updated_at = NOW
        record = session.scalar(
            select(PublishRecord).where(PublishRecord.content_item_id == item_id)
        )
        assert record is not None
        record.created_at = NOW
        record.updated_at = NOW

    with db.session_scope() as session:
        pinned = session.scalar(
            select(PublishRecord.updated_at).where(PublishRecord.content_item_id == item_id)
        )
    # 显式赋值必须真的盖过 onupdate=utcnow，否则上面那段等于没写。
    assert pinned == NOW, "残留时刻没钉住，边界用例又会跟着墙钟漂"
    return item_id


def _status_of(item_id: str) -> str | None:
    with db.session_scope() as session:
        return session.scalar(select(ContentItem.status).where(ContentItem.id == item_id))


# --------------------------------------------------------------------- 阈值


def test_threshold_is_larger_than_every_configured_publish_timeout():
    """阈值唯一不能犯的错是**取小**：取小会把正在正常发布的内容扫成失败。

    逐条比对配置里真实存在的发布超时，而不是钉一个魔法数字——钉数字挡不住"有人把
    抖音超时从 1200 调到 7200 却忘了同步这里"。
    """
    from core.config import get_settings

    settings = get_settings()
    threshold = stale_publishing_after().total_seconds()
    for name in (
        "douyin_publish_timeout_seconds",
        "xhs_publish_timeout_seconds",
        "wechat_publish_poll_timeout",
        "wenyan_timeout_seconds",
    ):
        configured = float(getattr(settings, name))
        assert threshold > configured, (
            f"阈值 {threshold:.0f}s 没有超过 {name}={configured:.0f}s："
            "正常发布还没跑完就会被扫成崩溃残留，然后重发一次"
        )
    assert threshold >= STALE_PUBLISHING_MIN_SECONDS, "下限被绕过了"


def test_threshold_follows_the_slowest_platform_timeout(monkeypatch):
    """调大最慢平台的发布超时，阈值必须跟着涨——不许漂成两个数。"""
    from core.config import reload_settings

    monkeypatch.setenv("DOUYIN_PUBLISH_TIMEOUT_SECONDS", "7200")
    reload_settings()
    assert stale_publishing_after() >= timedelta(seconds=7200)


def test_threshold_keeps_a_real_multiple_of_the_slowest_timeout(monkeypatch):
    """余量必须来自**倍数**，不能靠下限凑巧顶上。

    这条和 :func:`test_threshold_is_larger_than_every_configured_publish_timeout`
    是两件事：默认配置下 ``max(1200*3, 1800)`` 的两项谁都能单独盖过 1200，所以那条
    用例对"倍数被人改成 1"是瞎的。这里把平台超时抬到下限完全够不着的量级，逼出倍数
    本身。

    ×2 的依据同 ``STALE_PUBLISHING_TIMEOUT_FACTOR``：一次 ``publish()`` 是"传素材 →
    提交 → 等跳转"若干个各自受那条超时约束的请求，跑到单条超时的两倍完全合法。
    """
    from core.config import reload_settings

    monkeypatch.setenv("DOUYIN_PUBLISH_TIMEOUT_SECONDS", "7200")
    reload_settings()
    assert stale_publishing_after() >= timedelta(seconds=7200 * 2), (
        "阈值只比单条发布超时高一点点：一次跑了两三个请求的正常发布会被扫成崩溃残留"
    )


def test_threshold_has_a_floor_when_every_platform_timeout_is_tiny(monkeypatch):
    """所有平台超时都被调得很小时，下限必须顶住。

    进程崩溃与重启的时间尺度跟平台超时没关系：超时调到 5 秒不代表"卡了 15 秒就是
    崩溃残留"。这条对"倍数"是瞎的（倍数再大也乘不出东西来），专钉下限。
    """
    from core.config import reload_settings

    for name in (
        "DOUYIN_PUBLISH_TIMEOUT_SECONDS",
        "XHS_PUBLISH_TIMEOUT_SECONDS",
        "WECHAT_PUBLISH_POLL_TIMEOUT",
        "WENYAN_TIMEOUT_SECONDS",
    ):
        monkeypatch.setenv(name, "5")
    reload_settings()
    assert stale_publishing_after() >= timedelta(minutes=30), "下限没顶住，一次重启就会误扫"


# ----------------------------------------------------------- 捞得回来吗


def test_a_crashed_publish_has_no_other_way_out(notifier):
    """反证：除了这个 sweeper，卡在 ``publishing`` 的内容谁都不管。

    这条绿了，下一条"捞回来了"才有意义——否则可能只是别的 tick 顺手救的。
    """
    item_id = _crash_mid_publish(notifier)

    later = NOW + timedelta(hours=6)
    published_tick = tick_scheduled_publish(now=later, notifier=notifier)
    assert published_tick["scanned"] == 0, "定时发布只扫 scheduled，不该看见它"

    with db.session_scope() as session:
        stuck = session.scalars(
            select(ContentItem).where(ContentItem.status == ContentStatus.RETRYING.value)
        ).all()
    assert stuck == [], "重投只扫 retrying，这条根本进不了那个集合"
    assert _status_of(item_id) == ContentStatus.PUBLISHING.value


def test_stale_publishing_is_pushed_back_into_the_retry_chain(notifier):
    """超过阈值就推回 ``publish_failed → retrying``，交给现成的重投链。"""
    item_id = _crash_mid_publish(notifier)
    later = NOW + stale_publishing_after() + timedelta(minutes=1)

    stats = tick_retry_sweep(now=later, notifier=notifier)

    assert stats["recovered_stale"] == 1
    assert _status_of(item_id) == ContentStatus.RETRYING.value
    with db.session_scope() as session:
        record = session.scalars(
            select(PublishRecord).where(PublishRecord.content_item_id == item_id)
        ).one()
        assert record.phase == PublishPhase.FAILED.value, "别把孤儿声明永远留在 in_flight"
        assert record.attempts == 1, "这次尝试从来没有得到过结果，不该额外烧掉一次重试额度"


def test_recovery_is_audited_as_crash_residue_not_platform_failure(notifier):
    """审计日志必须说清"这条是崩溃残留"，别和真的发布失败混成一种原因。

    两者的排查方向完全不同：一个查进程为什么死，一个查平台与内容。
    """
    item_id = _crash_mid_publish(notifier)
    tick_retry_sweep(now=NOW + stale_publishing_after() + timedelta(minutes=1), notifier=notifier)

    with db.session_scope() as session:
        logs = session.scalars(select(ReviewLog).where(ReviewLog.content_item_id == item_id)).all()
    actions = [entry.action for entry in logs]
    assert SystemAction.PUBLISH_STALE.value in actions
    assert SystemAction.PUBLISH_FAILED.value not in actions, (
        "崩溃残留被记成了 publish_failed，等于把证据抹平"
    )
    residue = next(e for e in logs if e.action == SystemAction.PUBLISH_STALE.value)
    assert "publishing" in (residue.reason or "")
    assert "对账" in (residue.reason or ""), "得让读日志的人知道重投不会重复发"


def test_recovery_notifies_through_the_throttle(notifier):
    """卡住的稿子是运维信号，该让人知道；但走节流层，一批捞回来不刷屏。"""
    _crash_mid_publish(notifier, account_id="acc-n1")
    _crash_mid_publish(notifier, account_id="acc-n2")

    stats = tick_retry_sweep(now=NOW + stale_publishing_after() + timedelta(minutes=1))
    assert stats["recovered_stale"] == 2

    from core.notify import get_default_notifier

    sent = [t for _lvl, t, _x in get_default_notifier().sent if "崩溃残留" in t]
    assert len(sent) == 1, f"两条内容应合成一条通知，实际 {sent}"


# --------------------------------------------- 核心风险：会不会重复发布


def test_recovered_item_reconciles_instead_of_republishing(monkeypatch, notifier):
    """**这次改动的核心风险**：捞回来之后不许再发一次。

    重投链看到幂等键已存在会先 ``reconcile``。这里让对账说"平台其实已经发成功了"，
    同时把 ``publish`` 设成一调就抛——所以"``publish_calls == 0``"不是单一断言：
    真被调了的话内容会落到 ``dead_letter``，状态断言也会跟着红。
    """
    monkeypatch.setenv("SW_RETRY_BACKOFF_BASE_SECONDS", "60")
    from core.config import reload_settings

    reload_settings()

    item_id = _crash_mid_publish(notifier)
    swept_at = NOW + stale_publishing_after() + timedelta(minutes=1)
    assert tick_retry_sweep(now=swept_at, notifier=notifier)["recovered_stale"] == 1

    survivor = _register(
        FakePublisher(
            "acc-stale",
            platform="xhs",
            raise_exc=PermanentError("publish 不该被再调用一次"),
            reconcile_result=PublishResult(
                ok=True,
                platform_post_id="already-live",
                url="https://example.invalid/xhs/already-live",
            ),
        )
    )
    stats = tick_retry_sweep(now=swept_at + timedelta(minutes=30), notifier=notifier)

    assert survivor.publish_calls == 0, "重复发布了——这正是幂等声明要防的事故"
    assert survivor.reconcile_calls == 1, "根本没做平台侧对账"
    assert stats["published"] == 1
    assert _status_of(item_id) == ContentStatus.PUBLISHED.value
    with db.session_scope() as session:
        record = session.scalars(
            select(PublishRecord).where(PublishRecord.content_item_id == item_id)
        ).one()
        assert record.phase == PublishPhase.DONE.value
        assert record.platform_post_id == "already-live"


# ------------------------------------------------- 阈值内的正常发布别碰


def test_a_publish_still_within_the_threshold_is_left_alone(notifier):
    """正在正常发布的内容一根手指都不许碰。

    误扫比卡住严重得多：被扫成失败后重投链会去 ``reconcile``，而平台那一刻多半还
    没有结果可查（内容正在上传/转码），对不上就会**真的重发一次**。
    """
    item_id = _crash_mid_publish(notifier)

    from core.config import get_settings

    # 第二项刻意**从配置推**而不是从阈值推：从阈值推的话，阈值被改小时这条用例会
    # 跟着一起缩，等于自己把自己蒙上眼睛。一次 publish() 跑到单条超时的两倍完全合法
    slowest = get_settings().douyin_publish_timeout_seconds
    for elapsed in (
        timedelta(seconds=1),
        timedelta(seconds=slowest * 2),
        stale_publishing_after() - timedelta(minutes=1),
    ):
        stats = tick_retry_sweep(now=NOW + elapsed, notifier=notifier)
        assert stats["recovered_stale"] == 0, f"发布才过了 {elapsed}，就被扫成崩溃残留了"
        assert _status_of(item_id) == ContentStatus.PUBLISHING.value


def test_the_sweeper_is_exactly_on_the_threshold_boundary(notifier):
    """边界本身：差一分钟不碰，过一分钟就捞。钉住"阈值真的被用上了"。"""
    item_id = _crash_mid_publish(notifier)
    window = stale_publishing_after()

    assert recover_stale_publishing(now=NOW + window - timedelta(minutes=1)) == 0
    assert _status_of(item_id) == ContentStatus.PUBLISHING.value

    assert recover_stale_publishing(now=NOW + window + timedelta(minutes=1)) == 1
    assert _status_of(item_id) == ContentStatus.RETRYING.value


def test_sweeper_does_not_touch_healthy_states(notifier):
    """只认 ``publishing``：``scheduled`` / ``published`` / ``retrying`` 全不许动。"""
    with db.session_scope() as session:
        account = make_account(session, account_id="acc-healthy", daily_limit=9)
        account.extra = {**NO_CONFIRM}
        ids = {
            status: make_item(session, account, status=status, now=NOW).id
            for status in (
                ContentStatus.SCHEDULED.value,
                ContentStatus.PUBLISHED.value,
                ContentStatus.RETRYING.value,
                ContentStatus.DRAFT.value,
            )
        }

    assert recover_stale_publishing(now=NOW + timedelta(days=7), notifier=notifier) == 0
    for status, item_id in ids.items():
        assert _status_of(item_id) == status
