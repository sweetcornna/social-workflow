"""P4 新增的四个 tick：采集 / 生成 / 重试扫描 / 复盘，以及 `/dev/tick/{name}`。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from core import db
from core.models import Account, ContentItem, PublishRecord, ReviewLog, Topic, new_id
from core.ratelimit import RateLimiter
from core.scheduler import (
    TICKS,
    backoff_for,
    local_day_start,
    produced_today,
    run_tick,
    tick_generate,
    tick_retry_sweep,
    tick_scheduled_publish,
    tick_sourcing,
)
from core.state_machine import ContentStatus, PublishPhase, SystemAction
from publishers.base import FakePublisher, RetryableError
from publishers.registry import register, use_fake_publishers
from sourcing.base import RawTopic
from tests.conftest import NO_CONFIRM, make_account, make_item, make_publish_record

#: 时间基准取**真实当下**。原因：``PublishRecord`` / ``ContentItem`` 的时间戳是列默认值
#: （``default=utcnow`` 绑死真实时钟），注入 ``now=`` 影响不到它们。用一个写死的 2026-08-16
#: 会让"注入时刻"和"行里的时刻"差出几小时，退避与限频的断言全都变成碰运气。
NOW = datetime.now(UTC).replace(microsecond=0)


# ------------------------------------------------------------------ 采集


def test_tick_sourcing_persists_and_dedupes(monkeypatch, notifier):
    from sourcing import collector

    batch = [
        RawTopic(source="fake", title="选题甲", score=1.0),
        RawTopic(source="fake", title="选题乙", score=0.9),
    ]
    monkeypatch.setitem(collector.SOURCES, "fake", lambda: list(batch))

    stats = tick_sourcing(sources=["fake"], notifier=notifier)
    assert stats == {"fetched": 2, "created": 2, "sources_ok": 1, "sources_failed": 0}

    # 再跑一遍：同样的标题应该被去重，一条都不新增（tick 必须幂等）
    again = tick_sourcing(sources=["fake"], notifier=notifier)
    assert again["fetched"] == 2 and again["created"] == 0

    with db.session_scope() as session:
        assert len(session.scalars(select(Topic)).all()) == 2


def test_tick_sourcing_one_bad_source_does_not_block_others(monkeypatch, notifier):
    from sourcing import collector
    from sourcing.base import SourceUnavailable

    def boom() -> list[RawTopic]:
        raise SourceUnavailable("没配 BASE_URL")

    monkeypatch.setitem(collector.SOURCES, "bad", boom)
    monkeypatch.setitem(
        collector.SOURCES, "good", lambda: [RawTopic(source="good", title="活着的选题")]
    )

    stats = tick_sourcing(sources=["bad", "good"], notifier=notifier)
    assert stats["created"] == 1 and stats["sources_failed"] == 1


def test_tick_sourcing_notifies_when_all_sources_down(monkeypatch, notifier):
    from sourcing import collector
    from sourcing.base import SourceUnavailable

    def boom() -> list[RawTopic]:
        raise SourceUnavailable("挂了")

    monkeypatch.setitem(collector.SOURCES, "bad", boom)
    tick_sourcing(sources=["bad"], notifier=notifier)
    assert any("选题采集" in title for _lvl, title, _t in notifier.sent)


def test_trendradar_is_registered_as_a_source():
    """P4 的交付项：TrendRadar 必须真的挂进采集链，不只是有个模块。"""
    from sourcing.collector import SOURCES

    assert "trendradar" in SOURCES


def test_dev_flow_and_tick_share_the_same_collector(monkeypatch):
    """dev 端点与定时任务必须跑同一批采集器，否则加了新源只有一边生效。"""
    from core.dev_flow import collect_topics
    from sourcing import collector

    seen: list[str] = []

    def spy(session, **kwargs):
        seen.append("called")
        return collector.CollectResult(created=0)

    monkeypatch.setattr("sourcing.collector.collect", spy)
    with db.session_scope() as session:
        collect_topics(session, warnings=[])
    assert seen == ["called"]


# ------------------------------------------------------------------ 生成


def _seed_topics(session, count: int = 3) -> None:
    for index in range(count):
        session.add(
            Topic(
                id=new_id("tpc"),
                source="test",
                title=f"候选选题 {index}",
                score=1.0 - index * 0.1,
                raw={},
            )
        )


def test_tick_generate_respects_daily_target(monkeypatch, notifier):
    monkeypatch.setenv("SW_GENERATE_MAKE_MEDIA", "false")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    from core.config import reload_settings

    reload_settings()

    with db.session_scope() as session:
        account = make_account(session, account_id="gen-xhs", platform="xhs")
        account.extra = {"daily_target": 1}
        _seed_topics(session)

    first = tick_generate(account_ids=["gen-xhs"], notifier=notifier)
    assert first["generated"] == 1

    second = tick_generate(account_ids=["gen-xhs"], notifier=notifier)
    assert second["generated"] == 0
    assert second["skipped_target_met"] == 1


def test_tick_generate_skips_accounts_without_target(notifier):
    with db.session_scope() as session:
        make_account(session, account_id="no-target", platform="xhs")
    stats = tick_generate(account_ids=["no-target"], notifier=notifier)
    assert stats["skipped_no_target"] == 1 and stats["generated"] == 0


def test_tick_generate_skips_needs_relogin(notifier):
    """发不出去的号别烧 token。"""
    with db.session_scope() as session:
        account = make_account(
            session, account_id="offline", platform="xhs", status="needs_relogin"
        )
        account.extra = {"daily_target": 3}
    stats = tick_generate(account_ids=["offline"], notifier=notifier)
    assert stats["skipped_account"] == 1 and stats["generated"] == 0


def test_tick_generate_stops_when_budget_exhausted(monkeypatch, notifier):
    monkeypatch.setenv("DAILY_TOKEN_BUDGET", "10")
    from core.budget import BudgetGuard, CostKind
    from core.config import reload_settings

    reload_settings()
    with db.session_scope() as session:
        account = make_account(session, account_id="broke", platform="xhs")
        account.extra = {"daily_target": 2}
        BudgetGuard(session).charge(CostKind.TOKENS, 10)

    stats = tick_generate(account_ids=["broke"], notifier=notifier)
    assert stats["skipped_budget"] == 1 and stats["generated"] == 0
    assert any("成本闸门" in title for _lvl, title, _t in notifier.sent)


def test_tick_generate_disabled_by_setting(monkeypatch, notifier):
    monkeypatch.setenv("SW_GENERATE_ENABLED", "false")
    from core.config import reload_settings

    reload_settings()
    with db.session_scope() as session:
        account = make_account(session, account_id="off", platform="xhs")
        account.extra = {"daily_target": 5}
    assert tick_generate(notifier=notifier)["scanned"] == 0


def test_produced_today_uses_account_timezone():
    from core.accounts import policy_of

    with db.session_scope() as session:
        account = make_account(session, account_id="tz-acc", platform="xhs")
        account.extra = {"timezone": "Asia/Shanghai"}
        policy = policy_of(account)
        # 北京时间 2026-08-16 07:00 = UTC 前一天 23:00
        moment = datetime(2026, 8, 15, 23, 0, tzinfo=UTC)
        assert local_day_start(policy, moment) == datetime(2026, 8, 15, 16, 0, tzinfo=UTC)
        assert produced_today(session, account, policy, moment) == 0


# ------------------------------------------------------------------ 时段窗口


def test_publish_tick_respects_publish_windows():
    """窗口按账号时区判定；窗口外一条都不发，窗口内立刻放行。"""
    use_fake_publishers()
    # 以"真实当下的第二天"为基准，保证排期一定已到期，同时两个时刻都在同一天
    day = (NOW + timedelta(days=1)).date()
    outside_at = datetime.combine(day, datetime.min.time(), tzinfo=UTC).replace(hour=15)
    inside_at = datetime.combine(day, datetime.min.time(), tzinfo=UTC).replace(hour=9, minute=30)

    with db.session_scope() as session:
        account = make_account(session, account_id="win-acc", daily_limit=5)
        account.extra = {"publish_windows": ["09:00-11:00"], "timezone": "UTC", **NO_CONFIRM}
        item = make_item(session, account, scheduled_in_minutes=-10)
        item.scheduled_at = NOW - timedelta(hours=1)

    limiter = RateLimiter(min_interval_seconds=0, cache_ttl_seconds=0.0)
    outside = tick_scheduled_publish(limiter=limiter, now=outside_at)
    assert outside["published"] == 0 and outside["skipped_window"] == 1

    inside = tick_scheduled_publish(limiter=limiter, now=inside_at)
    assert inside["published"] == 1


# 公众号默认窗口 07:00-09:00 横跨 ``Asia/Shanghai`` 的 UTC 午夜缝（本地 08:00 = 00:00Z）：
# 本地 07:30 与 08:30 落在两个不同的 UTC 日里。这两条时刻是 P11.3 回归的锚点。
SEAM_TZ = "Asia/Shanghai"
SEAM_0730 = datetime(2026, 8, 16, 23, 30, tzinfo=UTC)  # 本地 08-17 07:30
SEAM_0830 = datetime(2026, 8, 17, 0, 30, tzinfo=UTC)  # 本地 08-17 08:30


def _seam_account(session, *, daily_limit: int):
    account = make_account(
        session,
        account_id="seam-acc",
        platform="wechat_mp",
        daily_limit=daily_limit,
        extra={
            "timezone": SEAM_TZ,
            "publish_windows": ["07:00-09:00"],
            "min_interval_minutes": 0,  # 只验日上限，别让最小间隔抢答
            **NO_CONFIRM,  # 同理，别让确认闸门抢答（P12）
        },
    )
    # 本地 07:30 已经发过一条（时刻写死，不能用真实时钟：日界用例靠它定生死）
    make_publish_record(session, account.id, at=SEAM_0730, platform="wechat_mp")
    item = make_item(session, account, title="本地 08:30 的第二条", scheduled_in_minutes=None)
    item.scheduled_at = SEAM_0830
    return account


def test_publish_tick_daily_limit_holds_across_the_utc_midnight_seam():
    """``daily_limit: 1`` 必须挡住本地同一天的第二条——哪怕它落在下一个 UTC 日。

    这是 P11.3 的核心回归：按 UTC 日计数时，本地 07:30 那条压根不算"今天"，
    08:30 这条就被放行了，公众号一天群发两次。
    """
    use_fake_publishers()
    with db.session_scope() as session:
        _seam_account(session, daily_limit=1)

    limiter = RateLimiter(min_interval_seconds=0, cache_ttl_seconds=0.0)
    stats = tick_scheduled_publish(limiter=limiter, now=SEAM_0830)
    assert stats["published"] == 0
    assert stats["skipped_rate"] == 1, "被挡住的必须是限频这一道，不是窗口/账号状态"

    with db.session_scope() as session:
        reason = limiter.deny_reason(
            "seam-acc", 1, now=SEAM_0830, session=session, timezone=SEAM_TZ
        )
    assert reason is not None and reason.startswith("daily_limit: 今日已发 1/1")


def test_publish_tick_still_publishes_when_the_local_day_has_quota_left():
    """同一副牌，只把 ``daily_limit`` 放到 2：证明上面那条是被日上限挡的，
    而不是窗口、账号状态或发布器缺席。"""
    use_fake_publishers()
    with db.session_scope() as session:
        _seam_account(session, daily_limit=2)

    limiter = RateLimiter(min_interval_seconds=0, cache_ttl_seconds=0.0)
    stats = tick_scheduled_publish(limiter=limiter, now=SEAM_0830)
    assert stats["published"] == 1 and stats["skipped_rate"] == 0


def test_publish_tick_uses_account_min_interval():
    """账号级 min_interval_minutes 覆盖全局值。"""
    use_fake_publishers()
    with db.session_scope() as session:
        account = make_account(session, account_id="interval-acc", daily_limit=5)
        account.extra = {"min_interval_minutes": 120, **NO_CONFIRM}
        make_item(session, account, title="第一条", scheduled_in_minutes=-30)
        make_item(session, account, title="第二条", scheduled_in_minutes=-20)

    limiter = RateLimiter(min_interval_seconds=0, cache_ttl_seconds=0.0)
    first = tick_scheduled_publish(limiter=limiter, now=NOW)
    assert first["published"] == 1 and first["skipped_rate"] == 1

    later = tick_scheduled_publish(limiter=limiter, now=NOW + timedelta(minutes=60))
    assert later["published"] == 0, "间隔 120 分钟，60 分钟后还不能发"

    much_later = tick_scheduled_publish(limiter=limiter, now=NOW + timedelta(minutes=150))
    assert much_later["published"] == 1


# ---------------------------------------------------- 幂等命中已 done 记录（P16.1）

#: 显式时间锚点，不挂真实墙钟——这两条测的是"同一分钟槽位撞车"，靠的是两条内容
#: scheduled_at 落进同一分钟；秒级漂移会让锚点漂到另一分钟，幂等键就撞不上了。
#: 惯例见 tests/conftest.py 的 ``make_item(now=...)`` 与 tests/test_confirm_gate.py 的 NOW。
IDEM_ANCHOR = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)


def _seed_idempotent_collision(session) -> tuple[str, str]:
    """造两条同账号、同内容、同一分钟槽位的稿子。

    ``make_idem_key`` = sha256(account_id | platform | content_hash | slot)，不含
    content_item_id；``make_item`` 默认标题/正文对所有稿子都相同，所以两条稿子的
    content_hash 天然一致——只要账号、槽位也一致，幂等键必然相同。
    """
    account = make_account(session, extra=NO_CONFIRM)
    first = make_item(session, account, scheduled_in_minutes=-5, now=IDEM_ANCHOR)
    second = make_item(session, account, scheduled_in_minutes=-5, now=IDEM_ANCHOR)
    return first.id, second.id


def test_publish_tick_reconciles_idempotent_hit_instead_of_stalling():
    """P16.1 根因复现：第二条内容命中第一条已 done 的幂等记录时，修复前
    ``publish_with_idempotency`` 直接 return、不推进状态机——这条内容会永远卡在
    scheduled，而 tick 却仍把它计入 published。修复后：同一轮 tick 里两条各自真实
    推进到 published，各计一次（不是"记一次账、两条都不发"，也不是"两条都白算"）。
    """
    use_fake_publishers()
    with db.session_scope() as session:
        first_id, second_id = _seed_idempotent_collision(session)

    stats = tick_scheduled_publish(now=IDEM_ANCHOR)

    assert stats["scanned"] == 2
    assert stats["published"] == 2, "两条都真的推进了状态机，一条都不能少算也不能多算"
    assert stats["skipped"] == 0 and stats["failed"] == 0

    with db.session_scope() as session:
        assert session.get(ContentItem, first_id).status == ContentStatus.PUBLISHED.value
        assert session.get(ContentItem, second_id).status == ContentStatus.PUBLISHED.value, (
            "命中已有 done 记录的一方此前会永远卡在 scheduled（P16.1 缺陷）"
        )
        records = session.scalars(select(PublishRecord)).all()
        assert len(records) == 1, "同一个幂等键只能有一条 PublishRecord，绝不重复发到平台"

        logs = session.scalars(
            select(ReviewLog).where(ReviewLog.content_item_id.in_([first_id, second_id]))
        ).all()
        actions_by_item: dict[str, list[str]] = {}
        for log in logs:
            actions_by_item.setdefault(log.content_item_id, []).append(log.action)
        publish_owner = records[0].content_item_id
        reconciled_owner = second_id if publish_owner == first_id else first_id
        assert SystemAction.PUBLISH.value in actions_by_item[publish_owner]
        assert SystemAction.RECONCILED.value in actions_by_item[reconciled_owner], (
            "命中方要在审计日志里留痕，写明是靠对账推进的，不是真的又发了一次"
        )


def test_publish_tick_is_a_no_op_the_next_round_after_reconciling():
    """P16.1 第二重危害的回归：撞车后两条都已经离开 scheduled 池，再跑一轮 tick
    必须扫描 0、发布 0——不能像修复前那样，卡住的那条每轮都被重新扫到、白算一次
    published。"""
    use_fake_publishers()
    with db.session_scope() as session:
        _seed_idempotent_collision(session)

    first_round = tick_scheduled_publish(now=IDEM_ANCHOR)
    assert first_round["published"] == 2

    second_round = tick_scheduled_publish(now=IDEM_ANCHOR + timedelta(minutes=1))

    assert second_round == {
        "scanned": 0,
        "published": 0,
        "skipped": 0,
        "failed": 0,
        "skipped_account": 0,
        "skipped_window": 0,
        "skipped_rate": 0,
        "skipped_unconfirmed": 0,
        "skipped_publisher": 0,
        "skipped_not_advanced": 0,
    }


# ------------------------------------------------------------------ 重试扫描


def test_backoff_is_exponential_and_capped(monkeypatch):
    monkeypatch.setenv("SW_RETRY_BACKOFF_BASE_SECONDS", "300")
    monkeypatch.setenv("SW_RETRY_BACKOFF_MAX_SECONDS", "3600")
    from core.config import reload_settings

    reload_settings()
    assert backoff_for(1) == timedelta(seconds=300)
    assert backoff_for(2) == timedelta(seconds=600)
    assert backoff_for(3) == timedelta(seconds=1200)
    assert backoff_for(99) == timedelta(seconds=3600), "必须封顶，且不能算出天文数字"


def _flaky_registry(**kwargs) -> FakePublisher:
    publisher = FakePublisher("acc-1", platform="xhs", **kwargs)
    register("xhs", lambda account_id, **_: publisher)
    return publisher


def test_retry_sweep_waits_for_backoff(notifier):
    publisher = _flaky_registry(raise_exc=RetryableError("502"))
    with db.session_scope() as session:
        account = make_account(session, account_id="acc-1", daily_limit=9)
        account.extra = {"min_interval_minutes": 0, **NO_CONFIRM}
        make_item(session, account, scheduled_in_minutes=-10)

    tick_scheduled_publish(now=NOW, notifier=notifier)
    assert publisher.publish_calls == 1
    with db.session_scope() as session:
        assert session.scalars(select(ContentItem.status)).one() == ContentStatus.RETRYING

    # 退避 5 分钟内不重投
    early = tick_retry_sweep(now=NOW + timedelta(minutes=1), notifier=notifier)
    assert early["skipped_backoff"] == 1
    assert publisher.publish_calls == 1


def test_retry_sweep_eventually_dead_letters_and_notifies(monkeypatch, notifier):
    monkeypatch.setenv("SW_MAX_PUBLISH_ATTEMPTS", "2")
    monkeypatch.setenv("SW_RETRY_BACKOFF_BASE_SECONDS", "60")
    from core.config import reload_settings

    reload_settings()
    _flaky_registry(raise_exc=RetryableError("一直 502"))
    with db.session_scope() as session:
        account = make_account(session, account_id="acc-1", daily_limit=9)
        account.extra = {"min_interval_minutes": 0, **NO_CONFIRM}
        make_item(session, account, scheduled_in_minutes=-10)

    tick_scheduled_publish(now=NOW, notifier=notifier)
    stats = tick_retry_sweep(now=NOW + timedelta(minutes=10), notifier=notifier)
    assert stats["failed"] == 1 and stats["dead_letter"] == 1

    with db.session_scope() as session:
        assert session.scalars(select(ContentItem.status)).one() == ContentStatus.DEAD_LETTER
    assert any("[死信]" in title for _lvl, title, _t in notifier.sent)


def test_retry_sweep_skips_needs_relogin_accounts(notifier):
    """P3 遗留问题①：NeedsRelogin 时内容在 retrying，不在挂起范围内。

    挂起只覆盖 ``scheduled``，所以重试扫描必须自己按账号健康过滤，
    否则会一遍遍去撞一个掉线的号。
    """
    publisher = _flaky_registry(raise_exc=RetryableError("502"))
    with db.session_scope() as session:
        account = make_account(session, account_id="acc-1", daily_limit=9)
        account.extra = {"min_interval_minutes": 0, **NO_CONFIRM}
        make_item(session, account, scheduled_in_minutes=-10)

    tick_scheduled_publish(now=NOW, notifier=notifier)
    calls_before = publisher.publish_calls

    with db.session_scope() as session:
        session.get(Account, "acc-1").status = "needs_relogin"

    stats = tick_retry_sweep(now=NOW + timedelta(hours=1), notifier=notifier)
    assert stats["skipped_account"] == 1 and stats["published"] == 0
    assert publisher.publish_calls == calls_before, "掉线的号一次都不该再调发布器"


def test_retry_sweep_recovers_when_publisher_heals(notifier):
    """前一次失败、后一次成功：重试扫描要能把它推到 published。"""
    _flaky_registry(raise_exc=RetryableError("暂时 502"), raise_times=1)
    with db.session_scope() as session:
        account = make_account(session, account_id="acc-1", daily_limit=9)
        account.extra = {"min_interval_minutes": 0, **NO_CONFIRM}
        make_item(session, account, scheduled_in_minutes=-10)

    tick_scheduled_publish(now=NOW, notifier=notifier)
    stats = tick_retry_sweep(now=NOW + timedelta(hours=1), notifier=notifier)
    assert stats["published"] == 1
    with db.session_scope() as session:
        assert session.scalars(select(ContentItem.status)).one() == ContentStatus.PUBLISHED
        done = session.scalars(
            select(PublishRecord).where(PublishRecord.phase == PublishPhase.DONE.value)
        ).all()
        assert len(done) == 1, "重试成功只能留一条 done 记录"


def test_retry_sweep_dead_letters_stale_items(monkeypatch, notifier):
    """NeedsRelogin 不计 attempts，没人处理时靠超龄兜底进死信。"""
    monkeypatch.setenv("SW_RETRY_MAX_AGE_HOURS", "24")
    from core.config import reload_settings

    reload_settings()
    _flaky_registry(raise_exc=RetryableError("502"))
    with db.session_scope() as session:
        account = make_account(session, account_id="acc-1", daily_limit=9)
        account.extra = {"min_interval_minutes": 0, **NO_CONFIRM}
        make_item(session, account, scheduled_in_minutes=-10)

    tick_scheduled_publish(now=NOW, notifier=notifier)
    stats = tick_retry_sweep(now=NOW + timedelta(hours=48), notifier=notifier)
    assert stats["dead_letter"] == 1
    with db.session_scope() as session:
        assert session.scalars(select(ContentItem.status)).one() == ContentStatus.DEAD_LETTER
    assert any("[死信]" in title for _lvl, title, _t in notifier.sent)


# ------------------------------------------------------------------ 注册表


def test_run_tick_rejects_unknown_name():
    with pytest.raises(KeyError, match="未知 tick"):
        run_tick("nope")


def test_all_ticks_are_callable_with_no_arguments():
    """`POST /dev/tick/{name}` 不带参数也要能跑通每一个 tick。"""
    use_fake_publishers()
    for name in TICKS:
        if name == "render_jobs":
            continue  # 需要 MPT sidecar，另有 tests/test_scheduler.py 覆盖
        stats = run_tick(name)
        assert isinstance(stats, dict)


# ------------------------------------------------------------------ dev 端点


def test_dev_tick_endpoint_lists_and_runs(client):
    listing = client.get("/dev/tick").json()
    assert set(listing["ticks"]) == set(TICKS)

    resp = client.post("/dev/tick/scheduled_publish")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] and body["tick"] == "scheduled_publish"
    assert "scanned" in body["stats"]


def test_dev_tick_endpoint_404_on_unknown(client):
    assert client.post("/dev/tick/nope").status_code == 404


def test_dev_tick_endpoint_rejects_irrelevant_params(client):
    assert client.post("/dev/tick/metrics", params={"account_id": "a"}).status_code == 422


def test_dev_tick_endpoint_scopes_by_account(client):
    with db.session_scope() as session:
        make_account(session, account_id="scoped", platform="xhs")
    body = client.post("/dev/tick/generate", params={"account_id": "scoped"}).json()
    assert body["ok"] and body["stats"]["scanned"] == 1


def test_dev_sync_accounts_endpoint(client, monkeypatch):
    # conftest 默认把台账指到临时空文件（P10 起工作台会回写台账，不能让用例改仓库里
    # 那份）。这条用例要验的恰恰是"读仓库真台账"，所以显式指回去
    from core.accounts import DEFAULT_ACCOUNTS_FILE
    from core.config import reload_settings

    monkeypatch.setenv("SW_ACCOUNTS_FILE", str(DEFAULT_ACCOUNTS_FILE))
    reload_settings()

    body = client.post("/dev/sync_accounts").json()
    assert body["ok"]
    assert "xhs-demo-01" in body["created"]
    with db.session_scope() as session:
        assert session.get(Account, "douyin-demo-01") is not None
    # 幂等
    again = client.post("/dev/sync_accounts").json()
    assert not again["created"] and not again["updated"]


def test_scheduler_only_sees_accounts_in_db(notifier):
    """台账里写了但没 sync 的账号，调度器一律看不见（DB 空 → 什么都不做）。"""
    from core.accounts import load_specs

    assert load_specs("accounts.yaml"), "台账不该是空的"
    assert tick_generate(notifier=notifier)["scanned"] == 0
    assert tick_scheduled_publish(notifier=notifier)["scanned"] == 0


def test_an_empty_topic_pool_is_not_counted_as_a_generation_failure(monkeypatch) -> None:
    """「今天没素材可写」和「生成器坏了」不许记在同一个数上。

    ``tick_generate`` 原先把两者都记进 ``stats["failed"]``——而 ``DevFlowError``
    那一支的注释自己写着"预期内的，不是故障"，日志也只记 INFO。看板
    （``core/api/dashboard.py``）把这个数原样当 failed 报出去，于是"选题池空了"
    和"生成链抛异常"在界面上长得一模一样：看到 failed=3 的人无法判断该去配
    NEWSNOW_BASE_URL，还是该去看 traceback。

    这是「不知道」和「没有」被压成同一个数的老毛病，2026-08-24 验收run 里撞出来的。
    """
    from core import dev_flow

    def _no_topics(*a, **kw):
        raise dev_flow.DevFlowError("选题池是空的")

    monkeypatch.setattr(dev_flow, "run_xhs_pipeline", _no_topics)
    with db.session_scope() as session:
        account = make_account(session, account_id="gen-empty", platform="xhs")
        account.extra = {"daily_target": 1}

    stats = tick_generate(account_ids=["gen-empty"])

    assert stats["skipped_no_material"] == 1, stats
    assert stats["failed"] == 0, "选题池空被记成了生成失败，看板分不清该修什么"
    assert stats["scanned"] == 1


def test_a_real_exception_is_still_counted_as_a_failure(monkeypatch) -> None:
    """护栏：上一条**不是**把 failed 变成永远 0。真异常照记。"""
    from core import dev_flow

    def _boom(*a, **kw):
        raise RuntimeError("生成链炸了")

    monkeypatch.setattr(dev_flow, "run_xhs_pipeline", _boom)
    with db.session_scope() as session:
        account = make_account(session, account_id="gen-boom", platform="xhs")
        account.extra = {"daily_target": 1}

    stats = tick_generate(account_ids=["gen-boom"])

    assert stats["failed"] == 1, stats
    assert stats["skipped_no_material"] == 0


def test_one_misconfigured_account_does_not_abort_the_whole_publish_tick() -> None:
    """一个账号缺 sidecar 配置，不许把整批发布打断。

    2026-08-24 验收run 撞出来的：``get_publisher`` 只把 ``KeyError`` 包成
    ``PublisherNotAvailable``，工厂本身抛的异常直接往外逃。小红书工厂在账号没配
    ``sidecar_endpoint`` 时抛 ``PermanentError``，而 ``tick_scheduled_publish``
    只接 ``PublisherNotAvailable`` —— 于是**一个**账号配错，这一轮里**所有**账号
    的发布全停，而且 stats 一个数都拿不到，看板上什么都看不出来。

    工厂在**构造阶段**抛 ``PermanentError`` 只可能是"这个账号这么配根本发不了"，
    语义上就是 ``PublisherNotAvailable``，所以在 ``get_publisher`` 里就地归一。
    """
    from publishers.base import PermanentError, PublisherNotAvailable
    from publishers.registry import get_publisher, register, unregister

    def _broken_factory(account_id: str, **kwargs):
        raise PermanentError("未配置该账号的 sidecar 地址")

    register("xhs", _broken_factory)
    try:
        with pytest.raises(PublisherNotAvailable):
            get_publisher("xhs", "any-account")
    finally:
        unregister("xhs")
