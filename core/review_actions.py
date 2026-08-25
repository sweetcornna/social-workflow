"""人工审核动作的**共享编排**：批准 / 驳回 / 改稿 / 复投。

为什么单独一层
--------------
``core/state_machine.py`` 的 ``approve``/``reject``/``edit`` 只管状态与审计日志；真正的
业务闸门（成片必须看完、公众号逐条确认、批准后立刻排期）原先写在 ``core/main.py`` 的
表单端点里。P6 的 JSON API 要求"语义与现有表单端点完全一致"——把这段编排抽到这里，
两个门面调同一个函数，就不会出现"HTMX 那条路拦了、curl 那条路没拦"。

错误一律抛 :class:`core.errors.AppError`（``HTTPException`` 的子类）：HTML 端点看到的
status_code 与 detail 文案与 P5 逐字一致，``/api/v1`` 另外读它的 ``code``。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from core.content_view import needs_watch_confirm
from core.errors import AppError
from core.models import Account, ContentItem, ReviewLog, new_id, utcnow
from core.state_machine import (
    ContentStatus,
    IllegalTransition,
    approve,
    edit,
    log_review,
    reject,
    transition,
)
from publishers.base import ContentBundle
from publishers.wechat_mp import mark_confirm_publish

logger = logging.getLogger("social_workflow.review_actions")

#: 审核页与 API 的缺省操作人。批准后自动排期那条系统日志固定用它记账
DEFAULT_ACTOR = "operator"

#: "已完整观看"复选框认可的真值。前端只是提示，后端才是闸门
TRUTHY = frozenset({"1", "true", "on", "yes"})

#: 复投（死信重新进队）写进 ``ReviewLog.action`` 的动作名
REQUEUE_ACTION = "requeue"

#: 复投时必须从 ``platform_extra`` 里抹掉的闸门痕迹——新的一条稿子要重新过一遍人工确认
GATE_KEYS: tuple[str, ...] = (
    "confirm_publish",
    "confirm_publish_by",
    "confirm_publish_at",
    "watched_by",
    "watched_at",
)


def is_watched(value: object) -> bool:
    """把表单字符串 / JSON 布尔统一判成"看过了"。空、``maybe`` 之类一律为假。"""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in TRUTHY


@dataclass
class ScheduleOutcome:
    """批准后自动排期的结果。``slot=None`` 表示没排上（内容仍在 approved）。"""

    slot: datetime | None
    message: str


@dataclass
class ApproveOutcome:
    """批准的结果，给两套门面共用。"""

    item: ContentItem
    slot: datetime | None
    message: str
    slot_text: str

    @property
    def scheduled(self) -> bool:
        return self.slot is not None


@dataclass
class RequeueOutcome:
    """复投的结果。

    - ``mode="retry_now"``：``retrying`` 项清掉退避，下一次 ``tick_retry_sweep`` 立刻重投
    - ``mode="requeued_as_draft"``：死信是终态，复投的是**新的一条** draft（``new_item_id``）
    """

    mode: str
    message: str
    status: str
    new_item_id: str | None = None
    due_at: datetime | None = None


def schedule_after_approve(session: Session, item: ContentItem) -> ScheduleOutcome:
    """批准后自动排期，返回给人看的一句话。

    排不上（窗口太窄 / 日上限用完 / 人指定的时刻不在窗口内）时**不报 500**：
    内容保留在 ``approved``，把原因原样告诉审核人，让他去改 `accounts.yaml`
    或改 `schedule_at`。硬拦下来比悄悄排到一个发不出去的时刻好。
    """
    from core.accounts import policy_of
    from core.scheduling import NoSlotAvailable, format_slot, schedule_item

    account = session.get(Account, item.account_id)
    if account is None:  # pragma: no cover - 有外键，正常不会发生
        return ScheduleOutcome(None, "已批准，但找不到对应账号，未能排期")
    try:
        slot = schedule_item(session, item, account, actor=DEFAULT_ACTOR)
    except NoSlotAvailable as exc:
        # 模板是转义输出的纯文本，别在这里写 Markdown 星号——页面上会原样显示 `**`
        logger.warning("批准后排期失败 item=%s: %s", item.id, exc)
        return ScheduleOutcome(None, f"已批准，但未能排期：{exc}")
    except IllegalTransition as exc:  # pragma: no cover - approve 刚把它置成 approved
        logger.warning("批准后排期状态迁移失败 item=%s: %s", item.id, exc)
        return ScheduleOutcome(None, f"已批准，但未能排期：{exc}")
    return ScheduleOutcome(slot, f"已批准，已排期至 {format_slot(slot, policy_of(account))}")


def approve_item(
    session: Session,
    item: ContentItem,
    *,
    actor: str = DEFAULT_ACTOR,
    reason: str | None = None,
    watched: object = "",
) -> ApproveOutcome:
    """人工批准 → 三道闸门 → 状态机 → 自动排期。**不 commit**（由调用方决定事务边界）。"""
    from core.content_view import slot_text as _slot_text

    # 显式守卫，不依赖状态迁移表的副作用：``scheduled → approved`` 在表里是**合法**的
    # （批准后又想改稿要能撤回），所以重复点批准原本会静默地重新排一次期。
    if item.status not in (ContentStatus.DRAFT.value, ContentStatus.REVIEWING.value):
        raise AppError(
            409,
            "invalid_state",
            f"当前状态 {item.status} 不能批准（只有 draft / reviewing 可以）",
        )
    if needs_watch_confirm(item):
        # 计划 2.2：成片必须**人工看完整片**才放行。前端把复选框绑在批准按钮上，
        # 后端再校验一次——前端约束只是提示，绕过它太容易（curl 一行的事）。
        if not is_watched(watched):
            raise AppError(
                422,
                "watch_required",
                "含视频的内容必须先完整观看成片，并勾选「已完整观看」才能批准",
            )
        # 把"看过了"写进审计日志：这是合规证据链的一部分，不能只留在前端
        item.bundle_json = {
            **(item.bundle_json or {}),
            "platform_extra": {
                **((item.bundle_json or {}).get("platform_extra") or {}),
                "watched_by": actor,
                "watched_at": utcnow().isoformat(),
            },
        }
    if item.platform == "wechat_mp":
        # 双确认闸门的第三道：逐条人工确认。不写 confirm_publish 的内容只到草稿箱，
        # 前两道是 WECHAT_CERTIFIED（账号资质）与 WECHAT_AUTO_PUBLISH（服务端总开关）。
        item.bundle_json = mark_confirm_publish(item.bundle_json, actor=actor)
    try:
        approve(session, item, actor=actor, reason=reason or None)
    except IllegalTransition as exc:
        raise AppError(409, "illegal_transition", str(exc)) from exc

    # 批准后**立刻排期**：这是"无人值守（除审核点击）"链路的接续点。
    # 不这么做的话内容会停在 approved，而调度器只扫 scheduled，整条链就断在这里。
    outcome = schedule_after_approve(session, item)
    return ApproveOutcome(
        item=item,
        slot=outcome.slot,
        message=outcome.message,
        slot_text=_slot_text(session, item),
    )


def reject_item(session: Session, item: ContentItem, *, actor: str, reason: str) -> None:
    """人工驳回。理由回写 ``review_notes``，供改稿 Agent 当 prompt 输入。"""
    if not reason.strip():
        raise AppError(422, "reason_required", "驳回必须填写理由")
    try:
        reject(session, item, actor=actor, reason=reason)
    except IllegalTransition as exc:
        raise AppError(409, "illegal_transition", str(exc)) from exc


def normalize_tags(tags: Sequence[str] | str | None) -> list[str]:
    """表单是 ``"a, b"``，JSON 是 ``["a", "b"]``，统一成去空的列表。"""
    if tags is None:
        return []
    values = tags.split(",") if isinstance(tags, str) else list(tags)
    return [str(tag).strip() for tag in values if str(tag).strip()]


def edit_item(
    session: Session,
    item: ContentItem,
    *,
    actor: str,
    title: str,
    body_markdown: str,
    tags: Sequence[str] | str | None = None,
    reason: str | None = None,
) -> ContentBundle:
    """人工改稿：校验内容包 → 写 before/after 审计日志 → 状态回 ``draft``。"""
    raw = dict(item.bundle_json or {})
    raw["title"] = title
    raw["body_markdown"] = body_markdown
    raw["tags"] = normalize_tags(tags)
    try:
        bundle = ContentBundle.model_validate(raw)
    except Exception as exc:
        raise AppError(422, "invalid_bundle", f"内容包非法: {exc}") from exc
    try:
        edit(
            session,
            item,
            actor=actor,
            new_bundle=bundle.model_dump(mode="json"),
            reason=reason or None,
        )
    except IllegalTransition as exc:
        raise AppError(409, "illegal_transition", str(exc)) from exc
    return bundle


# --------------------------------------------------------------------- 复投


def _clone_for_requeue(session: Session, item: ContentItem, *, actor: str) -> ContentItem:
    """把死信的内容包复制成一条新的 ``draft``。

    死信是 P0 冻结的**终态**（``CONTENT_TRANSITIONS[DEAD_LETTER]`` 是空集，
    ``tests/test_state_machine.py`` 有断言盯着"dead_letter → retrying 非法"）。
    所以 API 不去改迁移表，而是复投一条新稿子：它要重新过人工审核与所有闸门，
    原死信保持终态、审计链完整。
    """
    new_item_id = new_id("itm")
    raw = dict(item.bundle_json or {})
    raw["id"] = new_item_id
    extra = {k: v for k, v in (raw.get("platform_extra") or {}).items() if k not in GATE_KEYS}
    raw["platform_extra"] = extra
    clone = ContentItem(
        id=new_item_id,
        account_id=item.account_id,
        topic_id=item.topic_id,
        status=ContentStatus.DRAFT.value,
        bundle_json=raw,
        review_notes=f"由死信 {item.id} 复投，需重新人工审核。",
    )
    session.add(clone)
    session.flush()
    log_review(
        session,
        clone,
        actor=actor,
        action=REQUEUE_ACTION,
        reason=f"由死信 {item.id} 复投",
        after={"source_item_id": item.id},
    )
    log_review(
        session,
        item,
        actor=actor,
        action=REQUEUE_ACTION,
        reason=f"已复投为新内容 {new_item_id}（死信本身保持终态）",
        after={"new_item_id": new_item_id},
    )
    return clone


def requeue_item(
    session: Session,
    item: ContentItem,
    *,
    actor: str = DEFAULT_ACTOR,
    now: datetime | None = None,
) -> RequeueOutcome:
    """让一条卡住的内容重新有机会发出去。**不 commit**。

    - ``publish_failed`` → 先按状态机推进到 ``retrying``；
    - ``retrying`` → 把最近一条发布记录的时间戳往前拨到退避窗口之外，
      下一次 ``tick_retry_sweep``（默认 5 分钟一轮）就会立刻重投。
      **只解退避这一道**：账号健康、限频、48h 超龄仍然照常拦，绕过它们才是危险的；
    - ``dead_letter`` → 终态不可逆，复投一条新的 ``draft``（见 :func:`_clone_for_requeue`）。
    """
    from core.scheduler import backoff_for, latest_record

    moment = now or utcnow()
    status = item.status

    if status == ContentStatus.DEAD_LETTER.value:
        clone = _clone_for_requeue(session, item, actor=actor)
        return RequeueOutcome(
            mode="requeued_as_draft",
            message=f"死信是终态，已复投为新的待审内容 {clone.id}，请重新走人工审核。",
            status=item.status,
            new_item_id=clone.id,
        )

    if status == ContentStatus.PUBLISH_FAILED.value:
        try:
            transition(item, ContentStatus.RETRYING)
        except IllegalTransition as exc:  # pragma: no cover - 上面刚判过状态
            raise AppError(409, "illegal_transition", str(exc)) from exc

    if item.status != ContentStatus.RETRYING.value:
        raise AppError(
            409,
            "invalid_state",
            f"当前状态 {status} 不支持重投（只有 retrying / publish_failed / dead_letter 可以）",
        )

    record = latest_record(session, item.id)
    if record is None:
        # 没有发布记录却在 retrying：状态被手工改过。tick_retry_sweep 会原样跳过它
        return RequeueOutcome(
            mode="retry_now",
            message="该内容没有发布记录，重试扫描会跳过它；请检查它为什么处于 retrying。",
            status=item.status,
        )
    # 把"上次尝试时刻"拨到退避窗口之外 = 下一轮扫描立刻到点。多减 1 秒避开边界相等
    record.updated_at = moment - backoff_for(record.attempts) - timedelta(seconds=1)
    item.updated_at = utcnow()
    log_review(
        session,
        item,
        actor=actor,
        action=REQUEUE_ACTION,
        reason=f"人工重投：已清除退避（已尝试 {record.attempts} 次）",
        after={"attempts": record.attempts},
    )
    return RequeueOutcome(
        mode="retry_now",
        message="已清除退避，下一轮重试扫描（默认 5 分钟内）会重新投递。",
        status=item.status,
        due_at=moment,
    )


def audit_entry(log: ReviewLog) -> dict[str, object]:
    """``ReviewLog`` → JSON 友好的一条审计记录（API 与将来的导出共用）。"""
    return {
        "id": log.id,
        "actor": log.actor,
        "action": log.action,
        "reason": log.reason,
        "at": log.at,
        "has_diff": bool(log.before_json or log.after_json),
    }


__all__ = [
    "DEFAULT_ACTOR",
    "GATE_KEYS",
    "REQUEUE_ACTION",
    "ApproveOutcome",
    "RequeueOutcome",
    "ScheduleOutcome",
    "approve_item",
    "audit_entry",
    "edit_item",
    "is_watched",
    "normalize_tags",
    "reject_item",
    "requeue_item",
    "schedule_after_approve",
]
