"""发布前人工确认闸门 + autopilot 自动批准（P12）。

用户的裁决（2026-08-17）
-----------------------
"自动到排期，发布前推消息给你点一下"——系统全自动跑到"就等发"，**发布前推消息给人，
人点一下才真发**。这不是产品偏好，是合规底线：小红书 2026-03-10 公告直接封禁完全
AI 驱动的无人值守账号（见 ``docs/POLICY.md``）。所以：

- ``autopilot`` 打开**只影响"自动批准"**（机器审核干净的稿子不用人再点一次批准），
  它**不影响"发布前要人点"**。这条闸门没有旁路，也不许有。
- 确认动作全部写进 ``ReviewLog``——那是"人参与了"的唯一证据链。

为什么用字段而不是新状态
------------------------
确认在语义上和"限频""发布时段窗口"是同一类东西：``scheduled`` 之后、真发之前的一道
闸门，而不是内容生命周期里的新阶段。新增状态要动 P0 冻结的迁移表和一大票测试，
换来的只是把三个字段挪个地方放。所以落在 ``ContentItem.confirmed_at`` /
``confirm_ref`` / ``confirm_pushed_at`` 上，闸门本身做成
``tick_scheduled_publish`` 的**第五道**，形状与前四道完全一致。

超时不许无限堆积
----------------
推了卡没人点的内容会一直占着排期槽位。``SW_CONFIRM_TTL_HOURS``（默认 24）到了就
**自动驳回并通知**——内容回到审核台等人处理，槽位释放出来。TTL 从"第一次成功推送"
起算，没推成功过（比如 Telegram 没配）则从 ``scheduled_at`` 起算，两条路都有出口。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.accounts import AccountPolicy, policy_of
from core.models import Account, ContentItem, utcnow
from core.notify import Notifier, notify_event, public_url
from core.state_machine import (
    ContentStatus,
    IllegalTransition,
    ReviewAction,
    SystemAction,
    log_review,
    reject,
    transition,
)
from core.telegram import ACTION_CONFIRM, ACTION_REJECT, ConfirmCard, TelegramNotifier

logger = logging.getLogger("social_workflow.confirm")

#: autopilot 自动批准时写进 ``ReviewLog.actor``。审计日志必须能看出**是谁批的**——
#: 事后追责时"operator 批的"和"机器批的"是两件完全不同的事
AUTOPILOT_ACTOR = "autopilot"

#: 提醒过一次的标记落在 ``platform_extra`` 里。刻意不再加一列：它是纯展示/去重用的
#: 时间戳，而 ``platform_extra`` 本来就承载 ``watched_at`` / ``confirm_publish_at``
#: 这类闸门痕迹（见 ``core/review_actions.py`` 的 ``GATE_KEYS``）
REMINDED_KEY = "confirm_reminded_at"

#: 能进确认闸门的状态。只有 ``scheduled`` —— ``suspended`` 是账号掉线挂起的，
#: 这时候催人确认没有意义（号都发不出去）
CONFIRMABLE_STATUSES: tuple[str, ...] = (ContentStatus.SCHEDULED.value,)


class ConfirmConflict(RuntimeError):
    """这条内容已经被处理过了（重放 / 双击 / 两个门面同时点）。"""


@dataclass(frozen=True)
class ConfirmView:
    """一条内容的确认状态，给两套门面共用。

    ``deadline`` 是**决定期限**（TTL 到点会自动驳回）。它和排期时刻是两个不同的时刻，
    工作台上那处"双时刻读数"要的正是这两个数：``19:00 发`` 与 ``还有 3 小时 40 分决定``。
    """

    required: bool = False
    awaiting: bool = False
    confirmed_at: datetime | None = None
    pushed_at: datetime | None = None
    deadline: datetime | None = None


def confirm_view(item: ContentItem, account: Account | None) -> ConfirmView:
    """把确认状态算成一份给前端直接用的读数。"""
    if account is None:
        return ConfirmView()
    policy = policy_of(account)
    required = confirm_required(policy)
    ttl = confirm_ttl(policy)
    started = item.confirm_pushed_at or item.scheduled_at
    return ConfirmView(
        required=required,
        awaiting=required and item.confirmed_at is None and item.status in CONFIRMABLE_STATUSES,
        confirmed_at=item.confirmed_at,
        pushed_at=item.confirm_pushed_at,
        deadline=(started + ttl) if (required and ttl and started is not None) else None,
    )


# ------------------------------------------------------------------ 策略读数


def confirm_required(policy: AccountPolicy) -> bool:
    return policy.confirm_required


def autopilot_enabled(policy: AccountPolicy) -> bool:
    return policy.autopilot


def confirm_ttl(policy: AccountPolicy) -> timedelta:
    return timedelta(hours=max(policy.confirm_ttl_hours, 0))


def remind_window() -> timedelta:
    from core.config import get_settings

    return timedelta(minutes=max(get_settings().sw_confirm_remind_minutes, 0))


# ------------------------------------------------------------------ 通道


def confirm_channel(channel: TelegramNotifier | None = None) -> TelegramNotifier | None:
    """拿到能发确认卡的通道。``None`` 表示没配 Telegram（工作台兜底按钮仍可用）。"""
    if channel is not None:
        return channel
    from core.telegram import get_telegram_channel

    return get_telegram_channel()


def review_link(item_id: str) -> str:
    return public_url(f"/workbench/review?id={item_id}")


def schedule_link(item_id: str) -> str:
    return public_url(f"/workbench/schedule?id={item_id}")


# ------------------------------------------------------------------ 卡片组装


def _account_label(account: Account | None, item: ContentItem) -> str:
    if account is None:  # pragma: no cover - 有外键
        return item.account_id
    return f"{account.name}({account.id})"


#: ``review.pipeline.MACHINE_REVIEW_ACTION`` 的字面量副本。理由与 ``core/api/rows.py``
#: 里那份完全一样：那条 import 链（review.pipeline → generation.llm）会把 anthropic SDK
#: 拉进调度器与控制面的启动路径。有回归测试盯着三者不许漂移
MACHINE_REVIEW_ACTION = "machine_review"


def machine_counts(session: Session, item: ContentItem) -> tuple[int, int]:
    """该内容最近一次机器审核的 ``(block 数, warn 数)``。查不到按 ``(0, 0)``。"""
    from core.models import ReviewLog

    log = session.scalars(
        select(ReviewLog)
        .where(
            ReviewLog.content_item_id == item.id,
            ReviewLog.action == MACHINE_REVIEW_ACTION,
        )
        .order_by(ReviewLog.at.desc())
        .limit(1)
    ).first()
    after = dict((log.after_json if log is not None else None) or {})
    try:
        return int(after.get("blocking") or 0), int(after.get("warnings") or 0)
    except (TypeError, ValueError):  # pragma: no cover - 日志字段被手工改过
        return 0, 0


def humanize_delta(delta: timedelta) -> str:
    """``3:40:00`` → ``3 小时 40 分``。给"还有多久"这类读数用，不带秒。"""
    total = max(int(delta.total_seconds()), 0)
    hours, minutes = divmod(total // 60, 60)
    if hours and minutes:
        return f"{hours} 小时 {minutes} 分"
    if hours:
        return f"{hours} 小时"
    return f"{minutes} 分钟"


def build_card(
    session: Session, item: ContentItem, *, reminder: bool = False, now: datetime | None = None
) -> ConfirmCard:
    """把一条内容渲染成确认卡的数据。"""
    from core.content_view import media_summary, slot_text

    moment = now or datetime.now(UTC)
    account = session.get(Account, item.account_id)
    bundle = item.bundle_json or {}
    blocking, warnings = machine_counts(session, item)
    countdown = ""
    if reminder and item.scheduled_at is not None:
        countdown = humanize_delta(item.scheduled_at - moment)
    return ConfirmCard(
        item_id=item.id,
        title=item.title or "（无标题）",
        body=str(bundle.get("body_markdown") or ""),
        slot_text=slot_text(session, item),
        account_label=_account_label(account, item),
        platform=item.platform,
        review_url=schedule_link(item.id),
        blocking=blocking,
        warnings=warnings,
        media_count=int(media_summary(item).get("images") or 0),
        reminder=reminder,
        countdown=countdown,
    )


def card_cover(item: ContentItem) -> Path | None:
    from core.content_view import cover_asset

    try:
        return cover_asset(item)
    except Exception:  # pragma: no cover - 封面读不出来不该挡住推送
        return None


# ------------------------------------------------------------------ 推送


def ensure_confirm_pushed(
    session: Session,
    item: ContentItem,
    *,
    channel: TelegramNotifier | None = None,
    notifier: Notifier | None = None,
    now: datetime | None = None,
    reminder: bool = False,
) -> bool:
    """推一次确认卡。已经推过（且不是提醒）就直接返回 ``False``，不重复打扰。

    推送失败**不写** ``confirm_pushed_at``：下一轮 ``tick_confirm_gate`` 还会再试，
    而 TTL 也就不会从一次没送达的推送开始计时。
    """
    moment = now or datetime.now(UTC)
    if item.confirmed_at is not None:
        return False
    if item.confirm_pushed_at is not None and not reminder:
        return False

    target = confirm_channel(channel)
    if target is None:
        # 没有 Telegram 不是错误：工作台里的兜底确认按钮照样能用。
        # 但要让人知道"系统在等确认"，否则内容会静悄悄停住
        notify_event(
            "[待确认] 有内容在等发布确认",
            f"{item.title or item.id}\nTelegram 未配置，请到工作台确认：{schedule_link(item.id)}",
            kind="confirm_no_channel",
            account_id=item.account_id,
            level="warning",
            notifier=notifier,
            now=moment,
        )
        return False

    card = build_card(session, item, reminder=reminder, now=moment)
    ref = target.send_confirm_card(card, cover=card_cover(item))
    if ref is None:
        logger.warning("确认卡未能送达 item=%s（下一轮会重试）", item.id)
        return False

    item.confirm_ref = ref
    if reminder:
        _mark_reminded(item, moment)
    else:
        item.confirm_pushed_at = moment
    item.updated_at = utcnow()
    log_review(
        session,
        item,
        actor="system",
        action=SystemAction.CONFIRM_PUSHED,
        reason="补推确认提醒（快到发布时间）" if reminder else "已推送发布确认卡，等待人工确认",
        after={"confirm_ref": ref, "reminder": reminder},
    )
    logger.info("已推确认卡 item=%s ref=%s reminder=%s", item.id, ref, reminder)
    return True


def _mark_reminded(item: ContentItem, moment: datetime) -> None:
    bundle = dict(item.bundle_json or {})
    extra = dict(bundle.get("platform_extra") or {})
    extra[REMINDED_KEY] = moment.isoformat()
    bundle["platform_extra"] = extra
    item.bundle_json = bundle


def was_reminded(item: ContentItem) -> bool:
    return bool(((item.bundle_json or {}).get("platform_extra") or {}).get(REMINDED_KEY))


# ------------------------------------------------------------------ 决定


def _assert_decidable(item: ContentItem) -> None:
    """一条内容只认第一次有效点击——重放 / 双击 / 两个门面同时点都撞这里。"""
    if item.confirmed_at is not None:
        raise ConfirmConflict(f"这条已经在 {item.confirmed_at:%m-%d %H:%M} UTC 确认过了")
    if item.status not in CONFIRMABLE_STATUSES:
        raise ConfirmConflict(f"这条当前是 {item.status}，不在等确认")


def confirm_item(
    session: Session,
    item: ContentItem,
    *,
    actor: str,
    now: datetime | None = None,
) -> str:
    """人点了「确认发布」。放行第五道闸门，其余闸门（窗口 / 限频）照常拦。"""
    _assert_decidable(item)
    moment = now or datetime.now(UTC)
    item.confirmed_at = moment
    item.updated_at = utcnow()
    log_review(
        session,
        item,
        actor=actor,
        action=ReviewAction.CONFIRM,
        reason="人工确认发布",
        after={"confirmed_at": moment.isoformat()},
    )
    logger.info("已确认发布 item=%s actor=%s", item.id, actor)
    return "已确认，到点就发"


def reject_confirmation(
    session: Session,
    item: ContentItem,
    *,
    actor: str,
    reason: str,
    now: datetime | None = None,
) -> str:
    """人点了「不发·驳回」。内容退回 ``rejected``，槽位释放。

    退回时**逐跳走状态机**（``scheduled → approved → draft → reviewing → rejected``），
    每一步都是迁移表里合法的边——不绕过状态机，审计链也就没有断点。
    """
    _assert_decidable(item)
    text = reason.strip() or "人工在确认环节驳回"
    try:
        if item.status == ContentStatus.SCHEDULED.value:
            transition(item, ContentStatus.APPROVED)
        if item.status == ContentStatus.APPROVED.value:
            transition(item, ContentStatus.DRAFT)
        reject(session, item, actor=actor, reason=text)
    except IllegalTransition as exc:  # pragma: no cover - 上面刚判过状态
        raise ConfirmConflict(str(exc)) from exc
    # 槽位让出来：排期约束按 scheduled/suspended 算占用，留着会挡住后面的稿子
    item.scheduled_at = None
    item.updated_at = utcnow()
    logger.info("确认环节驳回 item=%s actor=%s", item.id, actor)
    return "已驳回，不会发出去"


def expire_confirmation(
    session: Session,
    item: ContentItem,
    *,
    notifier: Notifier | None = None,
    channel: TelegramNotifier | None = None,
    now: datetime | None = None,
    ttl_hours: int = 24,
) -> None:
    """TTL 到了还没人点：自动驳回 + 通知，绝不让它无限堆积。"""
    moment = now or datetime.now(UTC)
    reason = f"确认超时：推送后 {ttl_hours} 小时内无人确认，已自动驳回并释放排期槽位"
    card = build_card(session, item, now=moment)
    reject_confirmation(session, item, actor="system", reason=reason, now=moment)
    log_review(
        session,
        item,
        actor="system",
        action=SystemAction.CONFIRM_EXPIRED,
        reason=reason,
    )
    notify_event(
        f"[确认超时] {item.title or item.id}",
        f"{reason}\n要重新发就去工作台改稿再批：{review_link(item.id)}",
        kind="confirm_expired",
        account_id=item.account_id,
        level="warning",
        notifier=notifier,
        now=moment,
    )
    target = confirm_channel(channel)
    if target is not None and item.confirm_ref:
        target.edit_card(
            item.confirm_ref,
            card.render_decided(f"已超时自动驳回 · {ttl_hours} 小时内没人确认"),
        )


# ------------------------------------------------------------------ Telegram 回调


def handle_telegram_decision(action: str, item_id: str, actor: str) -> tuple[str, str]:
    """``TelegramPoller`` 的业务钩子：``(action, item_id, actor)`` → ``(回执, 卡片新文案)``。

    来源与签名已经由 poller 校验过（见 ``core/telegram.py`` 的安全模型），
    这里只管"这条内容现在能不能被这么处理"。
    """
    from core.db import session_scope

    with session_scope() as session:
        item = session.get(ContentItem, item_id)
        if item is None:
            return "找不到这条内容（可能已被删除）", ""
        # 先按"还没决定"的样子把卡片数据取出来：决定之后 scheduled_at 会被清掉，
        # 那时再取就没有"19:00 发"这一行了
        card = build_card(session, item)
        slot = card.slot_text
        try:
            if action == ACTION_CONFIRM:
                answer = confirm_item(session, item, actor=actor)
                status = f"已确认 · {slot}" if slot else "已确认"
            elif action == ACTION_REJECT:
                answer = reject_confirmation(
                    session, item, actor=actor, reason="在 Telegram 上点了「不发」"
                )
                status = "已驳回 · 已回写给改稿 Agent"
            else:  # pragma: no cover - poller 已经校验过动作码
                return f"未知动作 {action}", ""
        except ConfirmConflict as exc:
            # 重放 / 双击：明确告诉人"已经处理过了"，不要静默吞掉
            logger.info("确认回调被拒（已处理过）item=%s actor=%s: %s", item_id, actor, exc)
            return str(exc), ""
    return answer, card.render_decided(status)


# ------------------------------------------------------------------ autopilot


@dataclass(frozen=True)
class AutopilotOutcome:
    """一次自动批准的结果（给 ``tick_generate`` 记统计与写日志）。"""

    approved: bool
    scheduled: bool
    reason: str = ""
    slot: datetime | None = None


def autopilot_approve(
    session: Session,
    item: ContentItem,
    *,
    policy: AccountPolicy,
    blocking: int,
    warnings: int,
    notifier: Notifier | None = None,
    channel: TelegramNotifier | None = None,
    now: datetime | None = None,
) -> AutopilotOutcome:
    """机器审核**干净**的稿子自动批准 → 自动排期 → 推确认卡。

    "干净"= ``block == 0 且 warn == 0``。只要有一条 block 或 warn 就**不自动批准**：
    这些恰恰是需要人判断的地方，机器替人放行等于把审核这一层白做了。留在审核台，
    并推一条"这条有问题，去工作台看"（走节流层，不会刷屏）。

    复用 ``core.review_actions.approve_item`` 与人工批准**同一批函数**——
    两条路分叉的话，闸门迟早只在其中一条上生效。
    """
    from core.errors import AppError
    from core.review_actions import approve_item

    moment = now or datetime.now(UTC)
    if not autopilot_enabled(policy):
        return AutopilotOutcome(False, False, "autopilot 未开启")
    if blocking or warnings:
        notify_event(
            f"[待人工审核] {item.title or item.id}",
            (
                f"机器审核命中 block {blocking} / warn {warnings} 条，autopilot 不自动批准。\n"
                f"去工作台看：{review_link(item.id)}"
            ),
            kind="autopilot_needs_human",
            account_id=item.account_id,
            level="warning",
            notifier=notifier,
            now=moment,
        )
        return AutopilotOutcome(False, False, f"机器审核 block={blocking} warn={warnings}")

    try:
        outcome = approve_item(session, item, actor=AUTOPILOT_ACTOR, reason="autopilot 自动批准")
    except AppError as exc:
        logger.warning("autopilot 批准失败 item=%s: %s", item.id, exc.detail)
        return AutopilotOutcome(False, False, f"批准失败：{exc.detail}")

    if outcome.slot is None:
        # 排不上不是错误：内容停在 approved 等人改配置，与人工批准的语义一致
        logger.info("autopilot 已批准但未排期 item=%s: %s", item.id, outcome.message)
        return AutopilotOutcome(True, False, outcome.message)

    if confirm_required(policy):
        ensure_confirm_pushed(session, item, channel=channel, notifier=notifier, now=moment)
    return AutopilotOutcome(True, True, outcome.message, slot=outcome.slot)


# ------------------------------------------------------------------ 巡检 tick


def run_confirm_gate(
    *,
    account_ids: Sequence[str] | None = None,
    notifier: Notifier | None = None,
    channel: TelegramNotifier | None = None,
    now: datetime | None = None,
    max_items: int = 100,
) -> dict[str, int]:
    """确认闸门巡检：该推的推、该提醒的提醒、该超时的超时。

    统计键：``scanned`` / ``pushed`` / ``reminded`` / ``expired`` / ``waiting``
    / ``skipped_account`` / ``skipped_not_required``。
    """
    from core.db import session_scope
    from core.scheduler import PUBLISHABLE_ACCOUNT_STATUSES

    moment = now or datetime.now(UTC)
    window = remind_window()
    stats = {
        "scanned": 0,
        "pushed": 0,
        "reminded": 0,
        "expired": 0,
        "waiting": 0,
        "skipped_account": 0,
        "skipped_not_required": 0,
    }

    with session_scope() as session:
        stmt = (
            select(ContentItem)
            .where(
                ContentItem.status.in_(CONFIRMABLE_STATUSES),
                ContentItem.confirmed_at.is_(None),
            )
            .order_by(ContentItem.scheduled_at)
            .limit(max_items)
        )
        if account_ids:
            stmt = stmt.where(ContentItem.account_id.in_(list(account_ids)))
        for item in session.scalars(stmt):
            stats["scanned"] += 1
            account = session.get(Account, item.account_id)
            if account is None or account.status not in PUBLISHABLE_ACCOUNT_STATUSES:
                # 号掉线 / 停用时催人确认没有意义：确认了也发不出去
                stats["skipped_account"] += 1
                continue
            policy = policy_of(account)
            if not confirm_required(policy):
                stats["skipped_not_required"] += 1
                continue

            # TTL 从"第一次成功推送"起算；一次都没推成功过则从排期时刻起算——
            # 两条路都要有出口，否则 Telegram 没配时内容会永远堆在那里
            ttl = confirm_ttl(policy)
            started = item.confirm_pushed_at or item.scheduled_at
            if ttl and started is not None and moment - started >= ttl:
                expire_confirmation(
                    session,
                    item,
                    notifier=notifier,
                    channel=channel,
                    now=moment,
                    ttl_hours=policy.confirm_ttl_hours,
                )
                stats["expired"] += 1
                continue

            if item.confirm_pushed_at is None:
                if ensure_confirm_pushed(
                    session, item, channel=channel, notifier=notifier, now=moment
                ):
                    stats["pushed"] += 1
                else:
                    stats["waiting"] += 1
                continue

            due = item.scheduled_at
            if (
                window
                and due is not None
                and due - moment <= window
                and not was_reminded(item)
                and ensure_confirm_pushed(
                    session, item, channel=channel, notifier=notifier, now=moment, reminder=True
                )
            ):
                stats["reminded"] += 1
                continue
            stats["waiting"] += 1
    return stats


__all__ = [
    "AUTOPILOT_ACTOR",
    "CONFIRMABLE_STATUSES",
    "MACHINE_REVIEW_ACTION",
    "REMINDED_KEY",
    "AutopilotOutcome",
    "ConfirmConflict",
    "ConfirmView",
    "autopilot_approve",
    "autopilot_enabled",
    "build_card",
    "card_cover",
    "confirm_channel",
    "confirm_item",
    "confirm_required",
    "confirm_ttl",
    "confirm_view",
    "ensure_confirm_pushed",
    "expire_confirmation",
    "handle_telegram_decision",
    "humanize_delta",
    "machine_counts",
    "reject_confirmation",
    "remind_window",
    "review_link",
    "run_confirm_gate",
    "schedule_link",
    "was_reminded",
]
