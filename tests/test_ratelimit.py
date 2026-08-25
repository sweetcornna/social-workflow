"""DB 支撑的按账号限频（P4 把进程内计数降级成缓存）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from core import db
from core.models import PublishRecord
from core.ratelimit import (
    RateLimiter,
    db_usage,
    local_day_key,
    local_day_start,
    utc_day_start,
)
from core.state_machine import PublishPhase
from tests.conftest import make_account, make_publish_record

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


def _record(session, account_id: str, *, phase: str, at: datetime) -> PublishRecord:
    """造一条发布记录（连带它的内容项）。实现在 conftest，几个跨日界的用例共用。"""
    return make_publish_record(session, account_id, at=at, phase=phase)


# ------------------------------------------------------------------ db_usage


def test_db_usage_counts_only_done_records(session):
    account = make_account(session, account_id="acc-db")
    _record(session, account.id, phase=PublishPhase.DONE.value, at=NOW - timedelta(hours=1))
    _record(session, account.id, phase=PublishPhase.DONE.value, at=NOW - timedelta(hours=3))
    # in_flight 不该占配额：一次失败的发布不能永久吃掉一个名额
    _record(session, account.id, phase=PublishPhase.IN_FLIGHT.value, at=NOW)
    _record(session, account.id, phase=PublishPhase.FAILED.value, at=NOW)

    usage = db_usage(session, account.id, now=NOW)
    assert usage.count_today == 2
    assert usage.last_at == NOW - timedelta(hours=1)


def test_db_usage_daily_count_resets_at_local_midnight(session):
    """日计数在**账号本地**零点归零（P11.3 之前切的是 UTC 零点）。"""
    account = make_account(session, account_id="acc-day")
    _record(session, account.id, phase=PublishPhase.DONE.value, at=NOW - timedelta(days=1))
    usage = db_usage(session, account.id, now=NOW)
    assert usage.count_today == 0, "昨天的不计入今天"
    # 但最近发布时刻要跨天保留，否则最小间隔在零点前后会失效
    assert usage.last_at == NOW - timedelta(days=1)

    # 日界左闭：本地零点整那一条算今天。NOW 是本地 08-16 20:00，本地零点 = 08-15 16:00Z
    midnight = local_day_start(usage.timezone, NOW)
    assert midnight == datetime(2026, 8, 15, 16, 0, tzinfo=UTC)
    _record(session, account.id, phase=PublishPhase.DONE.value, at=midnight)
    assert db_usage(session, account.id, now=NOW).count_today == 1


def test_db_usage_is_per_account(session):
    a = make_account(session, account_id="acc-a")
    make_account(session, account_id="acc-b")
    _record(session, a.id, phase=PublishPhase.DONE.value, at=NOW)
    assert db_usage(session, "acc-b", now=NOW).count_today == 0


def test_utc_day_start():
    assert utc_day_start(NOW) == datetime(2026, 8, 16, 0, 0, tzinfo=UTC)


# ------------------------------------------------- 日界：账号本地日（P11.3 回归）
#
# ``Asia/Shanghai`` 是 UTC+8，本地日 D 横跨 ``D-1 16:00Z → D 15:59Z``；换句话说
# **本地 08:00 正好压在 UTC 的午夜缝上**。公众号默认窗口 ``07:00-09:00`` 横跨这条缝，
# 本地 07:30 与 08:30 各发一条会落进两个不同的 UTC 日 —— 按 UTC 日计数时
# ``daily_limit: 1`` 根本拦不住第二条。以下用例全部钉在这条缝上。

SHANGHAI = "Asia/Shanghai"
#: 夏令时 UTC-7：本地日 08-16 横跨 ``08-16 07:00Z → 08-17 06:59Z``，缝在本地 17:00
LOS_ANGELES = "America/Los_Angeles"

SEAM_0730 = datetime(2026, 8, 16, 23, 30, tzinfo=UTC)  # 上海本地 08-17 07:30
SEAM_0830 = datetime(2026, 8, 17, 0, 30, tzinfo=UTC)  # 上海本地 08-17 08:30
SEAM_0845 = datetime(2026, 8, 17, 0, 45, tzinfo=UTC)  # 上海本地 08-17 08:45


def test_local_day_start_and_key_follow_the_account_timezone():
    assert local_day_start(SHANGHAI, SEAM_0730) == datetime(2026, 8, 16, 16, 0, tzinfo=UTC)
    assert local_day_start(SHANGHAI, SEAM_0830) == datetime(2026, 8, 16, 16, 0, tzinfo=UTC)
    assert local_day_key(SHANGHAI, SEAM_0730) == local_day_key(SHANGHAI, SEAM_0830) == "2026-08-17"
    # 同一刻在 UTC 口径下是两天——这正是旧实现漏计的原因
    assert utc_day_start(SEAM_0730) != utc_day_start(SEAM_0830)


def test_db_usage_counts_both_sides_of_the_utc_midnight_seam(session):
    """本地同一天、跨 UTC 午夜缝的两条发布必须算成 2（旧实现算成 1）。"""
    account = make_account(session, account_id="acc-seam", extra={"timezone": SHANGHAI})
    _record(session, account.id, phase=PublishPhase.DONE.value, at=SEAM_0730)
    _record(session, account.id, phase=PublishPhase.DONE.value, at=SEAM_0830)

    usage = db_usage(session, account.id, now=SEAM_0845)
    assert usage.count_today == 2
    assert usage.timezone == SHANGHAI, "算出来的口径要自报家门，缓存才好按同一日界分桶"


def test_db_usage_excludes_yesterday_local_inside_the_same_utc_day(session):
    """反方向：UTC 同一天但本地已经是昨天的，不该算进今天（旧实现会多算）。"""
    account = make_account(session, account_id="acc-seam-back", extra={"timezone": SHANGHAI})
    # 本地 08-16 20:00 = 08-16 12:00Z：与"本地 08-17 07:00"同属 UTC 08-16
    _record(
        session,
        account.id,
        phase=PublishPhase.DONE.value,
        at=datetime(2026, 8, 16, 12, 0, tzinfo=UTC),
    )

    usage = db_usage(session, account.id, now=datetime(2026, 8, 16, 23, 0, tzinfo=UTC))
    assert usage.count_today == 0, "本地 00:00–08:00 打开工作台，不该混进昨晚发的"
    assert usage.last_at == datetime(2026, 8, 16, 12, 0, tzinfo=UTC), "最近发布时刻仍要跨天保留"


def test_db_usage_local_day_holds_for_negative_offset_timezone(session):
    """负偏移时区同理，缝在本地 17:00（UTC-7）。"""
    account = make_account(session, account_id="acc-la", extra={"timezone": LOS_ANGELES})
    _record(  # 本地 08-16 10:00
        session,
        account.id,
        phase=PublishPhase.DONE.value,
        at=datetime(2026, 8, 16, 17, 0, tzinfo=UTC),
    )
    _record(  # 本地 08-16 20:00 —— 已经是下一个 UTC 日
        session,
        account.id,
        phase=PublishPhase.DONE.value,
        at=datetime(2026, 8, 17, 3, 0, tzinfo=UTC),
    )

    usage = db_usage(session, account.id, now=datetime(2026, 8, 17, 4, 0, tzinfo=UTC))
    assert usage.count_today == 2
    assert usage.timezone == LOS_ANGELES


def test_db_usage_timezone_argument_wins_over_the_account_row(session):
    """调用方手里有 policy 时可以直接传时区，省一次 ``session.get``。"""
    account = make_account(session, account_id="acc-tz-arg", extra={"timezone": SHANGHAI})
    _record(session, account.id, phase=PublishPhase.DONE.value, at=SEAM_0730)
    _record(session, account.id, phase=PublishPhase.DONE.value, at=SEAM_0830)

    assert db_usage(session, account.id, now=SEAM_0845, timezone=SHANGHAI).count_today == 2
    assert db_usage(session, account.id, now=SEAM_0845, timezone="UTC").count_today == 1


def test_limiter_buckets_process_counts_by_account_local_day(session):
    """进程内缓存也按本地日分桶，否则 DB 一个日界、缓存另一个日界。"""
    account = make_account(session, account_id="acc-bucket", extra={"timezone": SHANGHAI})
    session.commit()

    limiter = RateLimiter(min_interval_seconds=0, cache_ttl_seconds=0.0)
    limiter.record(account.id, now=SEAM_0730, timezone=SHANGHAI)
    assert limiter.used_today(account.id, now=SEAM_0830, session=session) == 1
    assert not limiter.allow(account.id, 1, now=SEAM_0830, session=session), (
        "本地同一天的第二条必须被 daily_limit=1 挡住"
    )
    # 本地次日零点（= 08-17 16:00Z）之后配额才重置
    tomorrow = datetime(2026, 8, 17, 16, 30, tzinfo=UTC)
    assert limiter.used_today(account.id, now=tomorrow, session=session) == 0
    assert limiter.allow(account.id, 1, now=tomorrow, session=session)


def test_limiter_learns_the_timezone_from_db_without_being_told(session):
    """发布器不持有 policy，只能靠 :meth:`sync` 从库里学到时区。"""
    account = make_account(session, account_id="acc-learn", extra={"timezone": LOS_ANGELES})
    _record(  # 本地 08-16 10:00
        session,
        account.id,
        phase=PublishPhase.DONE.value,
        at=datetime(2026, 8, 16, 17, 0, tzinfo=UTC),
    )
    session.commit()

    limiter = RateLimiter(min_interval_seconds=0, cache_ttl_seconds=0.0)
    later = datetime(2026, 8, 17, 3, 0, tzinfo=UTC)  # 本地 08-16 20:00，已跨 UTC 日
    assert limiter.used_today(account.id, now=later, session=session) == 1
    # 学到时区之后，不传 timezone 的 record() 也落进同一个本地日的桶
    limiter.record(account.id, now=later)
    assert limiter.used_today(account.id, now=later, session=session) == 2


# --------------------------------------------------------------- RateLimiter


def test_limiter_reads_daily_count_from_db(session):
    """重启后进程内计数是空的，配额必须还认得出来。"""
    account = make_account(session, account_id="acc-restart")
    for _ in range(3):
        _record(session, account.id, phase=PublishPhase.DONE.value, at=NOW - timedelta(hours=2))
    session.commit()

    fresh = RateLimiter(min_interval_seconds=0, cache_ttl_seconds=0.0)
    assert fresh.used_today(account.id, now=NOW, session=session) == 3
    assert not fresh.allow(account.id, 3, now=NOW, session=session)
    assert fresh.allow(account.id, 4, now=NOW, session=session)


def test_limiter_min_interval_from_db_last_publish(session):
    account = make_account(session, account_id="acc-interval")
    _record(session, account.id, phase=PublishPhase.DONE.value, at=NOW - timedelta(minutes=20))
    session.commit()

    limiter = RateLimiter(cache_ttl_seconds=0.0)
    hour = timedelta(hours=1)
    assert not limiter.allow(account.id, 99, now=NOW, session=session, min_interval=hour)
    assert limiter.allow(
        account.id, 99, now=NOW + timedelta(minutes=45), session=session, min_interval=hour
    )


def test_limiter_takes_max_of_db_and_local(session):
    """本地刚记了一次但还没 commit 到 DB 时，宁可少发不可多发。"""
    account = make_account(session, account_id="acc-max")
    session.commit()
    limiter = RateLimiter(min_interval_seconds=0, cache_ttl_seconds=0.0)
    limiter.record(account.id, now=NOW)
    assert limiter.used_today(account.id, now=NOW, session=session) == 1


def test_limiter_cache_ttl_avoids_requery(session, monkeypatch):
    account = make_account(session, account_id="acc-ttl")
    session.commit()

    limiter = RateLimiter(min_interval_seconds=0, cache_ttl_seconds=60.0)
    calls = {"n": 0}
    real = db_usage

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr("core.ratelimit.db_usage", counting)
    limiter.allow(account.id, 5, now=NOW, session=session)
    limiter.allow(account.id, 5, now=NOW + timedelta(seconds=10), session=session)
    assert calls["n"] == 1, "TTL 内不该重复打库"
    limiter.allow(account.id, 5, now=NOW + timedelta(seconds=120), session=session)
    assert calls["n"] == 2


def test_limiter_min_interval_follows_settings(monkeypatch):
    """构造时不指定就跟随 settings，改环境变量立刻生效（P0 是构造时定死的）。"""
    from core.config import reload_settings

    limiter = RateLimiter(use_db=False)
    monkeypatch.setenv("SW_MIN_PUBLISH_INTERVAL_SECONDS", "1800")
    reload_settings()
    assert limiter.min_interval == timedelta(minutes=30)


def test_limiter_record_token_dedupes():
    """发布器与调度器各记一次，同一 token 只能算一次。"""
    limiter = RateLimiter(min_interval_seconds=0, use_db=False)
    assert limiter.record("acc-1", now=NOW, token="xhs:itm-1")
    assert not limiter.record("acc-1", now=NOW, token="xhs:itm-1")
    assert limiter.used_today("acc-1", now=NOW) == 1


def test_limiter_deny_reason_explains_which_gate(session):
    account = make_account(session, account_id="acc-why")
    _record(session, account.id, phase=PublishPhase.DONE.value, at=NOW - timedelta(minutes=5))
    session.commit()

    limiter = RateLimiter(cache_ttl_seconds=0.0)
    reason = limiter.deny_reason(account.id, 1, now=NOW, session=session)
    assert reason is not None and "daily_limit" in reason

    reason = limiter.deny_reason(
        account.id, 9, now=NOW, session=session, min_interval=timedelta(hours=1)
    )
    assert reason is not None and "min_interval" in reason

    assert (
        limiter.deny_reason(account.id, 9, now=NOW, session=session, min_interval=timedelta(0))
        is None
    )


def test_limiter_survives_broken_db(monkeypatch):
    """DB 挂了不能让限频整体报错——退回进程内计数继续挡。"""

    def boom(*_args, **_kwargs):
        raise RuntimeError("no such table")

    monkeypatch.setattr("core.ratelimit.db_usage", boom)
    limiter = RateLimiter(min_interval_seconds=0, cache_ttl_seconds=0.0)
    limiter.record("acc-x", now=NOW)
    assert limiter.used_today("acc-x", now=NOW) == 1
    assert not limiter.allow("acc-x", 1, now=NOW)


def test_limiter_sees_uncommitted_rows_in_same_session():
    """调度器把自己的 session 传进来，同一 tick 里刚 flush 的发布也要算数。"""
    with db.session_scope() as session:
        account = make_account(session, account_id="acc-same-tx")
        _record(session, account.id, phase=PublishPhase.DONE.value, at=NOW)
        limiter = RateLimiter(min_interval_seconds=0, cache_ttl_seconds=0.0)
        # 传 session：看得到未提交的行
        assert limiter.used_today(account.id, now=NOW, session=session) == 1
        # 不传 session：另开连接，看不到 → 这正是必须把 session 传进去的原因
        other = RateLimiter(min_interval_seconds=0, cache_ttl_seconds=0.0)
        assert other.used_today(account.id, now=NOW) == 0
