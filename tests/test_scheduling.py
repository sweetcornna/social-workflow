"""批准时排期（``core/scheduling.py``）。

soak（``scripts/soak.py``）验的是"跑三天下来槽位都合法"，这里补的是它够不着的边界：
窗口右端点开闭、日上限用完后顺延、排不上期时**保持 approved** 不落 ``scheduled_at``、
以及人手填的 ``platform_extra.schedule_at`` 与窗口冲突时的处理。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from core.accounts import AccountPolicy, policy_of
from core.models import Account, ReviewLog
from core.scheduling import (
    NoSlotAvailable,
    check_slot,
    db_constraints,
    next_slot,
    plan_slot,
    schedule_item,
)
from core.state_machine import ContentStatus
from tests.conftest import make_account, make_item, make_publish_record

TIMEZONE = "Asia/Shanghai"
SHANGHAI = ZoneInfo(TIMEZONE)
WINDOW = "09:00-11:00"


def _local(day: int, hour: int, minute: int = 0) -> datetime:
    """账号本地时刻 → UTC（2026-08 的某天）。"""
    return datetime(2026, 8, day, hour, minute, tzinfo=SHANGHAI).astimezone(UTC)


def _policy(
    *, daily_limit: int = 2, windows: list[str] | None = None, interval: int = 60
) -> AccountPolicy:
    account = Account(
        id="acc-plan",
        platform="xhs",
        name="排期测试号",
        status="ok",
        sidecar_endpoint="http://localhost:18060",
        daily_limit=daily_limit,
        extra={
            "timezone": TIMEZONE,
            "publish_windows": [WINDOW] if windows is None else windows,
            "min_interval_minutes": interval,
        },
    )
    return policy_of(account)


# ------------------------------------------------------------------ next_slot


def test_next_slot_waits_for_window_to_open():
    """窗口外提出的排期请求，落到下一个窗口的开始，而不是"现在"。"""
    assert next_slot(_policy(), now=_local(17, 3)) == _local(17, 9)


def test_next_slot_uses_now_when_already_inside_window():
    assert next_slot(_policy(), now=_local(17, 9, 30)) == _local(17, 9, 30)


def test_next_slot_window_end_is_exclusive():
    """窗口右端点不算数（``09:00-11:00`` 是 ``[09:00, 11:00)``）。

    11:00 整提出的请求要顺延到**次日**窗口开始，不能贴着右端点发出去。
    """
    assert next_slot(_policy(), now=_local(17, 10, 59)) == _local(17, 10, 59)
    assert next_slot(_policy(), now=_local(17, 11)) == _local(18, 9)


def test_next_slot_keeps_min_interval_from_scheduled_and_published():
    """已排期项与上次真实发布**都**占间隔，否则一次批准三条会挤在同一分钟。"""
    slot = next_slot(
        _policy(),
        now=_local(17, 9),
        already_scheduled=[_local(17, 9)],
    )
    assert slot == _local(17, 10)

    slot = next_slot(
        _policy(),
        now=_local(17, 9),
        last_published_at=_local(17, 9),
    )
    assert slot == _local(17, 10)


def test_next_slot_rolls_over_when_daily_quota_is_used_up():
    """当日额度用完 → 顺延到次日窗口，而不是塞进今天的窗口尾巴。"""
    slot = next_slot(_policy(), now=_local(17, 9), used_today=2)
    assert slot == _local(18, 9)


def test_next_slot_counts_pending_items_against_the_daily_limit():
    slot = next_slot(
        _policy(),
        now=_local(17, 9),
        already_scheduled=[_local(17, 9), _local(17, 10)],
    )
    assert slot == _local(18, 9)


def test_next_slot_raises_with_actionable_reason():
    """排不上期要**报出来**：静悄悄排到两周后没人会发现配置错了。"""
    with pytest.raises(NoSlotAvailable) as excinfo:
        next_slot(_policy(daily_limit=0), now=_local(17, 3))
    message = str(excinfo.value)
    assert "日上限" in message
    assert WINDOW in message  # 附带当前窗口配置，人才知道去改哪儿


def test_next_slot_without_windows_is_all_day():
    assert next_slot(_policy(windows=[]), now=_local(17, 3)) == _local(17, 3)


# ------------------------------------------------- plan_slot / schedule_item


def _approved_item(session, **extra_account):
    account = make_account(session, account_id="acc-sched")
    account.daily_limit = extra_account.pop("daily_limit", 2)
    account.extra = {
        "timezone": TIMEZONE,
        "publish_windows": [WINDOW],
        "min_interval_minutes": 60,
        **extra_account,
    }
    item = make_item(
        session, account, status=ContentStatus.APPROVED.value, scheduled_in_minutes=None
    )
    session.flush()
    return account, item


def test_plan_slot_honours_explicit_schedule_at(session):
    """人在 ``platform_extra.schedule_at`` 里填了时刻就以人为准（前提是在窗口内）。"""
    account, item = _approved_item(session)
    wanted = _local(17, 10, 30)
    item.bundle_json = {**item.bundle_json, "platform_extra": {"schedule_at": wanted.isoformat()}}

    assert plan_slot(session, item, account, now=_local(17, 9)) == wanted


def test_plan_slot_rejects_explicit_schedule_at_outside_window(session):
    """人填的时刻和窗口冲突时报出来，不悄悄改成合法时刻。"""
    account, item = _approved_item(session)
    item.bundle_json = {
        **item.bundle_json,
        "platform_extra": {"schedule_at": _local(17, 3).isoformat()},
    }

    with pytest.raises(NoSlotAvailable) as excinfo:
        plan_slot(session, item, account, now=_local(17, 1))
    assert "发布时段" in str(excinfo.value)


def test_schedule_item_transitions_and_writes_audit_log(session):
    account, item = _approved_item(session)
    slot = schedule_item(session, item, account, actor="auditor", now=_local(17, 3))

    assert slot == _local(17, 9)
    assert item.status == ContentStatus.SCHEDULED
    assert item.scheduled_at == slot
    log = session.scalars(select(ReviewLog).where(ReviewLog.content_item_id == item.id)).one()
    assert log.action == "schedule"
    assert log.after_json == {"scheduled_at": slot.isoformat()}


def test_schedule_item_keeps_approved_when_no_slot(session):
    """排不上期时内容**留在 approved**：宁可停下等人改配置，也不排到发不出去的时刻。"""
    account, item = _approved_item(session, daily_limit=0)

    with pytest.raises(NoSlotAvailable):
        schedule_item(session, item, account, now=_local(17, 3))

    assert item.status == ContentStatus.APPROVED
    assert item.scheduled_at is None
    assert (
        session.scalars(select(ReviewLog).where(ReviewLog.content_item_id == item.id)).all() == []
    )


def test_schedule_item_spaces_out_a_batch_of_approvals(session):
    """连着批准三条：槽位互相错开，不会全挤在同一分钟。"""
    account, first = _approved_item(session)
    slots = [schedule_item(session, first, account, now=_local(17, 3))]
    for _ in range(2):
        item = make_item(
            session, account, status=ContentStatus.APPROVED.value, scheduled_in_minutes=None
        )
        session.flush()
        slots.append(schedule_item(session, item, account, now=_local(17, 3)))

    assert slots == [_local(17, 9), _local(17, 10), _local(18, 9)]
    assert all(b - a >= timedelta(minutes=60) for a, b in pairwise(slots))


# ------------------------------------- 日上限跨 UTC 午夜缝（P11.3 回归，真实路径）
#
# 这一段走的是 ``db_constraints``——批准排期、人工改期、发布 tick 真正用的那条路。
# 此前只有"显式传 used_today"的用例，于是"槽位按账号本地日分桶、计数按 UTC 日"
# 这个自相矛盾两个方向都没人测到。
#
# 公众号默认台账（accounts.yaml 的 wechat-demo-01）：窗口 07:00-09:00、daily_limit 1。
# ``Asia/Shanghai`` 是 UTC+8，本地 08:00 正是 UTC 的午夜缝：本地 07:30 与 08:30
# 落在两个 UTC 日里，按 UTC 日计数时 daily_limit=1 拦不住第二条。

WECHAT_WINDOW = "07:00-09:00"
#: 夏令时 UTC-7，缝在本地 17:00
LA_TIMEZONE = "America/Los_Angeles"
LOS_ANGELES = ZoneInfo(LA_TIMEZONE)


def _la(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, day, hour, minute, tzinfo=LOS_ANGELES).astimezone(UTC)


def _wechat_account(session, *, daily_limit: int = 1, timezone: str = TIMEZONE, window: str):
    account = make_account(
        session,
        account_id="acc-mp",
        platform="wechat_mp",
        daily_limit=daily_limit,
        extra={
            "timezone": timezone,
            "publish_windows": [window],
            "min_interval_minutes": 0,  # 单测只想验日上限，别让最小间隔抢答
        },
    )
    item = make_item(
        session, account, status=ContentStatus.APPROVED.value, scheduled_in_minutes=None
    )
    session.flush()
    return account, item


def test_db_constraints_counts_both_sides_of_the_utc_midnight_seam(session):
    """本地同一天、跨 UTC 午夜缝发的两条，``used_today`` 必须是 2（旧实现给 1）。"""
    account, item = _wechat_account(session, daily_limit=3, window=WECHAT_WINDOW)
    make_publish_record(session, account.id, at=_local(17, 7, 30), platform="wechat_mp")
    make_publish_record(session, account.id, at=_local(17, 8, 30), platform="wechat_mp")

    constraints = db_constraints(session, item, account, now=_local(17, 8, 45))
    assert constraints.today == date(2026, 8, 17)
    assert constraints.used_today == 2
    assert constraints.quota_left(constraints.today) == 1


def test_daily_limit_blocks_the_second_wechat_post_across_the_seam(session):
    """公众号 ``07:00-09:00`` + ``daily_limit: 1``：本地 07:30 发过之后，08:30 发不出去。"""
    account, item = _wechat_account(session, daily_limit=1, window=WECHAT_WINDOW)
    make_publish_record(session, account.id, at=_local(17, 7, 30), platform="wechat_mp")
    now = _local(17, 8)

    # 本地 08:30 还在窗口里，只是落到了下一个 UTC 日——挡住它的必须是「日上限」
    with pytest.raises(NoSlotAvailable) as excinfo:
        check_slot(session, item, account, _local(17, 8, 30), now=now)
    assert excinfo.value.reason == "日上限"

    # 自动排期同样不许把它塞进今天，只能顺延到本地明天的窗口开始
    assert plan_slot(session, item, account, now=now) == _local(18, 7)


def test_daily_limit_still_allows_the_first_post_of_the_local_day(session):
    """反向对照：本地昨晚（同一个 UTC 日）发过，不该占掉今天的名额。"""
    account, item = _wechat_account(session, daily_limit=1, window=WECHAT_WINDOW)
    # 本地 08-16 20:00 = 08-16 12:00Z，与本地 08-17 07:30 同属 UTC 08-16
    make_publish_record(session, account.id, at=_local(16, 20), platform="wechat_mp")

    now = _local(17, 7, 30)
    assert db_constraints(session, item, account, now=now).used_today == 0
    assert plan_slot(session, item, account, now=now) == now


def test_daily_limit_holds_for_negative_offset_timezones(session):
    """负偏移时区（``America/Los_Angeles``）：缝在本地 17:00，同样按本地日算。"""
    account, item = _wechat_account(
        session, daily_limit=2, timezone=LA_TIMEZONE, window="10:00-21:00"
    )
    make_publish_record(session, account.id, at=_la(16, 10), platform="wechat_mp")
    make_publish_record(session, account.id, at=_la(16, 20), platform="wechat_mp")  # 已跨 UTC 日

    now = _la(16, 20, 30)
    constraints = db_constraints(session, item, account, now=now)
    assert constraints.today == date(2026, 8, 16)
    assert constraints.used_today == 2
    assert constraints.quota_left(constraints.today) == 0

    with pytest.raises(NoSlotAvailable) as excinfo:
        check_slot(session, item, account, _la(16, 20, 45), now=now)
    assert excinfo.value.reason == "日上限"
    assert plan_slot(session, item, account, now=now) == _la(17, 10)
