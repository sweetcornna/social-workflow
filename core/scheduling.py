"""排期：把 ``approved`` 的内容放进账号的发布时段窗口。

补的是哪个洞
------------
P4 第一版把"限频 + 发布时段窗口"做在了 ``tick_scheduled_publish`` 里，但**没有人
给内容定 ``scheduled_at``**：批准之后内容停在 ``approved``，调度器只扫
``scheduled``，于是 `scanned=0`，整条"无人值守"链断在人点完批准的那一刻。
（唯一做这件事的地方一度是 ``scripts/soak.py`` 里的模拟器作弊。）

现在：``POST /review/{id}/approve`` 成功后立刻算一个槽位并落 ``scheduled_at``。

为什么放在批准时算，而不是再开一个 ``tick_schedule``
----------------------------------------------------
- **人能立刻看到结果**。批准按钮的回显直接写"已排期至 19:30（Asia/Shanghai）"，
  排期时刻也进审核详情页。多一个 tick 的话，人点完批准看到的是"待定"，
  要等最多一个 tick 周期才知道会几点发——这正是运营最想知道的一件事。
- **审计链更短**。排期是"人工确认"这个动作的直接后果，写在同一个请求里，
  ``ReviewLog`` 上 ``approve`` 与 ``schedule`` 两条紧挨着，事后好复盘。
- 不引入"approved 积压"这个新的中间状态需要监控。

代价是"批准时算出的槽位可能过期"（人批准完系统停机两天）。这由
``tick_scheduled_publish`` 兜住：它每次都重新校验窗口与限频，过期的槽位只会
让内容早一点被扫到，不会绕过任何闸门。

算法见 :func:`next_slot`。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.accounts import AccountPolicy, policy_of
from core.models import Account, ContentItem, utcnow
from core.ratelimit import db_usage
from core.state_machine import (
    ContentStatus,
    IllegalTransition,
    SystemAction,
    log_review,
    transition,
)

logger = logging.getLogger("social_workflow.scheduling")

#: 往后找槽位的最长跨度。超过这个还找不到说明窗口/限频配得根本发不出去，
#: 与其偷偷排到两周后，不如报出来让人改配置
DEFAULT_HORIZON_DAYS = 14

#: 算槽位时也要避开的"已占用"状态。``suspended`` 是账号掉线时挂起的排期项，
#: 恢复后会原样放回，所以它占的时间片仍然算数
OCCUPYING_STATUSES: tuple[str, ...] = (
    ContentStatus.SCHEDULED.value,
    ContentStatus.SUSPENDED.value,
)

#: 允许人工改期的状态。``approved`` 顺带补一次排期，``suspended`` 只改时刻不动状态
#: （账号还掉着线，恢复时按 ``prev_status`` 放回）
RESCHEDULABLE_STATUSES: tuple[str, ...] = (
    ContentStatus.APPROVED.value,
    ContentStatus.SCHEDULED.value,
    ContentStatus.SUSPENDED.value,
)


class NoSlotAvailable(RuntimeError):
    """在 horizon 内找不到可用槽位（或人指定的时刻不合法）。携带面向人的原因。"""

    def __init__(self, message: str, *, reason: str = "") -> None:
        super().__init__(message)
        #: 机器可读的被挡原因：``窗口`` / ``最小间隔`` / ``日上限`` / ``已过去``
        self.reason = reason


def _local_day(policy: AccountPolicy, moment: datetime) -> date:
    return moment.astimezone(policy.tzinfo).date()


def _local_midnight(policy: AccountPolicy, day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=policy.tzinfo).astimezone(UTC)


def _window_starts(policy: AccountPolicy, day: date) -> list[datetime]:
    """某个本地日期上所有窗口的开始时刻（UTC）。"""
    return [
        datetime.combine(day, start, tzinfo=policy.tzinfo).astimezone(UTC)
        for start, _end in policy.windows
    ]


@dataclass(frozen=True)
class SlotConstraints:
    """一个账号此刻的排期约束快照。

    :func:`next_slot`（"帮我找一个"）与 :func:`check_slot`（"我挑了这个，行不行"）
    共用同一份判定 —— 人工改期与自动排期必须被**同一套**窗口 / 间隔 / 日上限拦住，
    否则前端就成了绕过闸门的后门。
    """

    policy: AccountPolicy
    now: datetime
    limit: int
    #: 所有已占用时刻（未来已排期 + 上次真实发布），已排序、均带 tzinfo
    occupied: tuple[datetime, ...] = ()
    #: 每个账号本地日已被未来排期占掉的名额
    pending_per_day: Mapping[date, int] = field(default_factory=dict)
    #: 本地"今天"已经发出去的条数。**必须按账号本地日切**（``core.ratelimit.db_usage``
    #: 的口径），否则和下面按本地日分桶的 ``pending_per_day`` 对不上，日上限会漏
    used_today: int = 0

    @property
    def interval(self) -> timedelta:
        return self.policy.min_interval

    @property
    def today(self) -> date:
        return _local_day(self.policy, self.now)

    def quota_left(self, day: date) -> int:
        used = self.used_today if day == self.today else 0
        return self.limit - used - self.pending_per_day.get(day, 0)

    def violation(self, moment: datetime) -> str | None:
        """这个时刻**为什么**不能用。可用则返回 ``None``。"""
        if moment < self.now:
            return "已过去"
        if not self.policy.in_window(moment):
            return "窗口"
        if self.interval and any(abs(moment - m) < self.interval for m in self.occupied):
            return "最小间隔"
        if self.quota_left(_local_day(self.policy, moment)) <= 0:
            return "日上限"
        return None

    def describe(self) -> str:
        return (
            f"窗口 {self.policy.window_text()}，"
            f"最小间隔 {self.interval.total_seconds() / 60:.0f} 分钟，"
            f"日上限 {self.limit}"
        )


def build_constraints(
    policy: AccountPolicy,
    *,
    now: datetime,
    last_published_at: datetime | None = None,
    already_scheduled: Sequence[datetime] = (),
    used_today: int = 0,
    daily_limit: int | None = None,
) -> SlotConstraints:
    """把散装参数归一成 :class:`SlotConstraints`（补 tzinfo、按本地日分桶）。"""
    occupied = sorted(
        m if m.tzinfo else m.replace(tzinfo=UTC)
        for m in ([*already_scheduled] + ([last_published_at] if last_published_at else []))
    )
    # 每个本地日已经占了几个名额（未来已排期项各占一个）
    pending_per_day: dict[date, int] = {}
    for moment in already_scheduled:
        aware = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
        day = _local_day(policy, aware)
        pending_per_day[day] = pending_per_day.get(day, 0) + 1
    return SlotConstraints(
        policy=policy,
        now=now,
        limit=daily_limit if daily_limit is not None else policy.daily_limit,
        occupied=tuple(occupied),
        pending_per_day=pending_per_day,
        used_today=used_today,
    )


def solve_slot(
    constraints: SlotConstraints,
    *,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    include_local_midnight_candidates: bool = False,
) -> datetime:
    """在约束下找最早的可用槽位。找不到抛 :class:`NoSlotAvailable`。

    算法：**只在约束边界上取候选，不做时间步进扫描。**
    可行集是若干区间的并集，它的左端点只可能来自三种地方——``now``、某个窗口的开始、
    某个已占用时刻 ``+ min_interval``。把这些点排序后逐个验，第一个全过的就是最早解。
    步进扫描在 ``min_interval=0`` 时会死循环，粒度取粗了又会错过合法槽位。
    """
    policy = constraints.policy
    now = constraints.now
    today = constraints.today

    candidates: set[datetime] = {now}
    for moment in constraints.occupied:
        if constraints.interval:
            candidates.add(moment + constraints.interval)
    for offset in range(horizon_days + 1):
        day = today + timedelta(days=offset)
        if policy.windows:
            candidates.update(_window_starts(policy, day))
            if include_local_midnight_candidates and offset:
                # 连续查槽时，跨午夜窗口可在本地日额度重置的瞬间重新可用。
                candidates.add(_local_midnight(policy, day))
        elif offset:
            # 没配窗口时，"下一天"的边界就是本地零点（当日额度用完要顺延到那里）
            candidates.add(_local_midnight(policy, day))

    deadline = now + timedelta(days=horizon_days)
    reasons: set[str] = set()
    for moment in sorted(c for c in candidates if c >= now):
        if moment > deadline:
            break
        reason = constraints.violation(moment)
        if reason is None:
            return moment
        reasons.add(reason)

    raise NoSlotAvailable(
        f"账号 {policy.account_id} 在 {horizon_days} 天内没有可用发布槽位"
        f"（{constraints.describe()}）。被挡在：{'、'.join(sorted(reasons)) or '无候选点'}。"
        "多半是窗口太窄或日上限太低，改 accounts.yaml 后重新同步。",
        reason="、".join(sorted(reasons)),
    )


def next_slot(
    policy: AccountPolicy,
    *,
    now: datetime,
    last_published_at: datetime | None = None,
    already_scheduled: Sequence[datetime] = (),
    used_today: int = 0,
    daily_limit: int | None = None,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
) -> datetime:
    """算出这个账号下一条内容该在什么时候发。返回 UTC 时刻。

    约束（全部满足才算可用）：

    1. ``>= now``；
    2. 落在 ``policy.publish_windows`` 里的某个窗口内（没配窗口 = 全天可发）；
    3. 距**每一个**已占用时刻（上次真实发布 + 所有已排期项）都 ``>= min_interval``；
    4. 该槽位所在的**账号本地日**还没把 ``daily_limit`` 用完。

    找不到就抛 :class:`NoSlotAvailable`。算法见 :func:`solve_slot`。

    参数：

    - ``last_published_at``：该账号最近一次**真实发布**的时刻（来自 ``PublishRecord``）。
    - ``already_scheduled``：该账号所有**未来已排期**项的 ``scheduled_at``。
      新槽位必须和它们都隔开——不然一次批准三条会全挤在同一分钟。
    - ``used_today``：本地"今天"已经发出去的条数，用来判断当日额度。
    - ``daily_limit``：缺省取 ``policy.daily_limit``（平台硬顶已夹过）。
      ``daily_target`` 管的是**生成**产能，不参与排期——手工加的稿子不该被它挡住。
    """
    constraints = build_constraints(
        policy,
        now=now,
        last_published_at=last_published_at,
        already_scheduled=already_scheduled,
        used_today=used_today,
        daily_limit=daily_limit,
    )
    return solve_slot(constraints, horizon_days=horizon_days)


def available_slots(
    constraints: SlotConstraints,
    *,
    count: int,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
) -> list[datetime]:
    """只读地连续求取最近的合法槽位。

    每个已返回槽位都会立即计入临时占用和当天额度，确保同一响应不能超过
    ``daily_limit``。原始约束不被修改，调用方可安全地复用其快照。
    """
    if count <= 0:
        return []

    policy = constraints.policy
    deadline = constraints.now + timedelta(days=horizon_days)
    cursor = constraints.now
    occupied = list(constraints.occupied)
    pending = dict(constraints.pending_per_day)
    slots: list[datetime] = []

    while len(slots) < count and cursor <= deadline:
        cursor_day = _local_day(policy, cursor)
        current = SlotConstraints(
            policy=policy,
            now=cursor,
            limit=constraints.limit,
            occupied=tuple(occupied),
            pending_per_day=pending,
            used_today=constraints.used_today if cursor_day == constraints.today else 0,
        )
        try:
            candidate = solve_slot(
                current,
                horizon_days=horizon_days,
                include_local_midnight_candidates=True,
            )
        except NoSlotAvailable:
            break
        if candidate > deadline:
            break

        candidate_minute = candidate.astimezone(UTC).replace(second=0, microsecond=0)
        if any(
            moment.astimezone(UTC).replace(second=0, microsecond=0) == candidate_minute
            for moment in occupied
        ):
            cursor = candidate_minute + timedelta(minutes=1)
            continue

        slots.append(candidate)
        occupied.append(candidate)
        day = _local_day(policy, candidate)
        pending[day] = pending.get(day, 0) + 1
        cursor = candidate_minute + timedelta(minutes=1)

    return slots


# ------------------------------------------------------------------ DB 接线


def scheduled_moments(
    session: Session, account_id: str, *, now: datetime, exclude_item_id: str | None = None
) -> list[datetime]:
    """该账号所有**还没发出去**的排期时刻。

    刻意不过滤掉"已经过期"的排期（``scheduled_at < now``）：它们随时会被下一次
    ``tick_scheduled_publish`` 发出去，仍然要占最小间隔。
    """
    stmt = select(ContentItem.scheduled_at).where(
        ContentItem.account_id == account_id,
        ContentItem.status.in_(OCCUPYING_STATUSES),
        ContentItem.scheduled_at.is_not(None),
    )
    if exclude_item_id:
        stmt = stmt.where(ContentItem.id != exclude_item_id)
    return [
        (m if m.tzinfo else m.replace(tzinfo=UTC))
        for m in session.scalars(stmt)
        # 太久以前的排期多半是卡住的历史项，让它继续占间隔没有意义
        if m is not None and (m if m.tzinfo else m.replace(tzinfo=UTC)) >= now - timedelta(days=1)
    ]


def db_constraints(
    session: Session,
    item: ContentItem,
    account: Account,
    *,
    now: datetime | None = None,
) -> SlotConstraints:
    """查库拼出该内容的排期约束（限频现状 + 该账号其它已排期项）。

    ``exclude_item_id=item.id``：改期时它自己的旧槽位不该把新槽位挡住。

    ``timezone=policy.timezone``（P11.3）：``used_today`` 会被 :meth:`SlotConstraints.quota_left`
    拿去和**账号本地日**的桶相减，所以喂进来的计数也必须按账号本地日切。
    以前这里灌的是 UTC 日计数，"分桶按本地日、计数按 UTC 日"——横跨 UTC 午夜缝的
    窗口（公众号默认 ``07:00-09:00`` 之于 ``Asia/Shanghai``）里日上限直接失效。
    """
    moment = now or utcnow()
    policy = policy_of(account)
    usage = db_usage(session, account.id, now=moment, timezone=policy.timezone)
    return build_constraints(
        policy,
        now=moment,
        last_published_at=usage.last_at,
        already_scheduled=scheduled_moments(
            session, account.id, now=moment, exclude_item_id=item.id
        ),
        used_today=usage.count_today,
    )


def plan_slot(
    session: Session,
    item: ContentItem,
    account: Account,
    *,
    now: datetime | None = None,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
) -> datetime:
    """给一条内容算槽位（查库拿限频与已排期现状）。

    人已经在 ``platform_extra['schedule_at']`` 里指定了时刻的话**以人为准**，
    但仍然要过窗口校验——人指定的时刻是"我想让它几点发"，窗口是"这个号什么时候
    发才有人看"，两者冲突时应该让人知道而不是悄悄改掉他填的值。
    """
    moment = now or utcnow()
    policy = policy_of(account)

    explicit = (item.bundle_json or {}).get("platform_extra", {}).get("schedule_at")
    if explicit:
        chosen = _parse_explicit(explicit)
        if not policy.in_window(chosen):
            raise NoSlotAvailable(
                f"platform_extra.schedule_at={explicit} 不在账号 {account.id} 的发布时段内"
                f"（{policy.window_text()}，时区 {policy.timezone}）。"
                "改时间，或者把窗口放宽后重新同步台账。",
                reason="窗口",
            )
        return chosen

    return solve_slot(db_constraints(session, item, account, now=moment), horizon_days=horizon_days)


def suggest_slot(
    session: Session,
    item: ContentItem,
    account: Account,
    *,
    now: datetime | None = None,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
) -> datetime | None:
    """ "最近一个合法槽位是什么时候"。算不出来返回 ``None``（给人看的提示用，不抛异常）。"""
    try:
        return solve_slot(
            db_constraints(session, item, account, now=now), horizon_days=horizon_days
        )
    except NoSlotAvailable:
        return None


def check_slot(
    session: Session,
    item: ContentItem,
    account: Account,
    when: datetime,
    *,
    now: datetime | None = None,
) -> None:
    """人挑的时刻能不能用。不能就抛 :class:`NoSlotAvailable`，理由具体到"被哪一道挡住"。"""
    moment = now or utcnow()
    constraints = db_constraints(session, item, account, now=moment)
    reason = constraints.violation(when if when.tzinfo else when.replace(tzinfo=UTC))
    if reason is None:
        return
    policy = constraints.policy
    raise NoSlotAvailable(
        f"{format_slot(when, policy)} 不是账号 {account.id} 的合法发布时刻，"
        f"被「{reason}」挡住（{constraints.describe()}，时区 {policy.timezone}）。",
        reason=reason,
    )


def reschedule_item(
    session: Session,
    item: ContentItem,
    account: Account,
    *,
    when: datetime,
    actor: str = "operator",
    now: datetime | None = None,
) -> datetime:
    """人工改期：**走与批准时完全同一套校验**，通过才落 ``scheduled_at``。

    ``approved`` 的内容顺带补上 ``approved → scheduled`` 这一跳；``suspended`` 只改时刻
    （账号还掉着线，恢复时按 ``prev_status`` 放回）。其它状态抛 :class:`IllegalTransition`。
    """
    moment = now or utcnow()
    if item.status not in RESCHEDULABLE_STATUSES:
        raise IllegalTransition("ContentItem", item.status, ContentStatus.SCHEDULED.value)

    target = when if when.tzinfo else when.replace(tzinfo=UTC)
    check_slot(session, item, account, target, now=moment)

    if item.status == ContentStatus.APPROVED.value:
        transition(item, ContentStatus.SCHEDULED)
    item.scheduled_at = target
    log_review(
        session,
        item,
        actor=actor,
        action=SystemAction.SCHEDULE,
        reason=f"人工改期至 {format_slot(target, policy_of(account))}",
        after={"scheduled_at": target.isoformat()},
    )
    logger.info("人工改期 item=%s account=%s at=%s", item.id, account.id, target.isoformat())
    return target


def _parse_explicit(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise NoSlotAvailable(
            f"platform_extra.schedule_at 不是合法时间串：{value!r}（应为 ISO 8601）"
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def schedule_item(
    session: Session,
    item: ContentItem,
    account: Account,
    *,
    actor: str = "system",
    now: datetime | None = None,
) -> datetime:
    """``approved → scheduled`` 并写 ``scheduled_at``。返回排定的时刻。

    找不到槽位时抛 :class:`NoSlotAvailable`，**内容保持 ``approved``**——
    宁可让它停在那里等人改配置，也不要排到一个永远发不出去的时刻。
    """
    slot = plan_slot(session, item, account, now=now)
    transition(item, ContentStatus.SCHEDULED)
    item.scheduled_at = slot
    log_review(
        session,
        item,
        actor=actor,
        action=SystemAction.SCHEDULE,
        reason=f"排期至 {format_slot(slot, policy_of(account))}",
        after={"scheduled_at": slot.isoformat()},
    )
    logger.info("排期 item=%s account=%s at=%s", item.id, account.id, slot.isoformat())
    return slot


def format_slot(slot: datetime, policy: AccountPolicy) -> str:
    """给人看的排期时刻：账号本地时区 + 时区名。"""
    local = slot.astimezone(policy.tzinfo)
    return f"{local.strftime('%m-%d %H:%M')}（{policy.timezone}）"


__all__ = [
    "DEFAULT_HORIZON_DAYS",
    "OCCUPYING_STATUSES",
    "RESCHEDULABLE_STATUSES",
    "NoSlotAvailable",
    "SlotConstraints",
    "available_slots",
    "build_constraints",
    "check_slot",
    "db_constraints",
    "format_slot",
    "next_slot",
    "plan_slot",
    "reschedule_item",
    "schedule_item",
    "scheduled_moments",
    "solve_slot",
    "suggest_slot",
]
