"""内容与排期：列表 / 时间线、详情、人工改期、重投。

改期这条路**必须**走 ``core.scheduling``：窗口、最小间隔、日上限三道判定与"批准即排期"
用的是同一个 :class:`~core.scheduling.SlotConstraints`。前端能挑一个非法时刻，
但后端一定会把它挡回去，并顺手告诉它最近的合法槽位是什么时候。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.accounts import policy_of
from core.api.common import (
    DbSession,
    Envelope,
    Page,
    PageParams,
    Pagination,
    aware,
    count_of,
    get_account,
    get_item,
    ok,
    slice_of,
)
from core.api.rows import AuditEntry, ContentRow, audit_entries, build_rows
from core.confirm import ConfirmConflict, confirm_item, reject_confirmation
from core.content_view import account_windows, bundle_view
from core.errors import AppError
from core.models import Account, ContentItem, ReviewLog, utcnow
from core.review_actions import DEFAULT_ACTOR, requeue_item
from core.scheduling import (
    NoSlotAvailable,
    available_slots,
    db_constraints,
    format_slot,
    reschedule_item,
    suggest_slot,
)
from core.state_machine import IllegalTransition

router = APIRouter(prefix="/content", tags=["content"])


class ContentDetail(BaseModel):
    item: ContentRow
    bundle: dict[str, Any]
    platform_extra: dict[str, Any] = Field(default_factory=dict)
    logs: list[AuditEntry] = Field(default_factory=list)
    account_windows: str = ""


class RescheduleIn(BaseModel):
    scheduled_at: datetime = Field(description="目标时刻，ISO8601；不带时区按 UTC 解析")
    actor: str = "operator"


class RescheduleResult(BaseModel):
    item: ContentRow
    scheduled_at: datetime
    slot_text: str
    message: str


class AvailableSlot(BaseModel):
    at: datetime
    slot_text: str
    window: str


class AvailableSlotsResult(BaseModel):
    item_id: str
    account_id: str
    timezone: str
    slots: list[AvailableSlot] = Field(default_factory=list)
    note: str


class ConfirmIn(BaseModel):
    actor: str = ""
    #: 只有「不发」用得上；留空按默认文案写进 review_notes
    reason: str = ""


class ConfirmResult(BaseModel):
    item: ContentRow
    message: str


class RetryResult(BaseModel):
    item: ContentRow
    mode: str = Field(description="retry_now = 清除退避；requeued_as_draft = 死信复投成新草稿")
    message: str
    new_item_id: str | None = None


def _slot_now() -> datetime:
    """给可用槽位查询提供可替换的时间锚；生产环境始终取当前 UTC。"""
    return utcnow()


def _slot_window_text(slot: datetime, policy: Any) -> str:
    """返回槽位落入的单个配置窗口；未配置窗口时明确标成“全天”。"""
    if not policy.windows:
        return "00:00-24:00"
    local_time = slot.astimezone(policy.tzinfo).time()
    for start, end in policy.windows:
        if (start <= end and start <= local_time < end) or (
            start > end and (local_time >= start or local_time < end)
        ):
            return f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')}"
    # 合法槽位必在窗口内；保留兜底以免人工改坏账号配置时展示端点 500。
    return policy.window_text()


@router.get("", summary="内容列表 / 时间线")
def list_content(
    status: str | None = Query(default=None, description="内容状态，留空 = 全部"),
    platform: str | None = None,
    account_id: str | None = None,
    from_: datetime | None = Query(default=None, alias="from", description="时间线起点（含）"),
    to: datetime | None = Query(default=None, description="时间线终点（含）"),
    page: Pagination = PageParams,
    session: Session = DbSession,
) -> Envelope[Page[ContentRow]]:
    """时间线数据面。

    ``from`` / ``to`` 过滤的是 **``scheduled_at``，没排期的退回 ``updated_at``**
    （SQL 上就是 ``coalesce(scheduled_at, updated_at)``）——已发布的内容也带着它当初的
    排期时刻，所以过去与未来能画在同一根轴上。响应里的 ``timeline_at`` 是同一口径的
    展示值（已发布优先取真实发布时刻）。
    """
    anchor = func.coalesce(ContentItem.scheduled_at, ContentItem.updated_at)
    stmt = select(ContentItem).join(Account, Account.id == ContentItem.account_id)
    if status:
        stmt = stmt.where(ContentItem.status == status)
    if platform:
        stmt = stmt.where(Account.platform == platform)
    if account_id:
        stmt = stmt.where(ContentItem.account_id == account_id)
    if from_ is not None:
        stmt = stmt.where(anchor >= aware(from_))
    if to is not None:
        stmt = stmt.where(anchor <= aware(to))

    total = count_of(session, stmt)
    items = list(session.scalars(slice_of(stmt.order_by(anchor.desc()), page)))
    return ok(
        Page(
            items=build_rows(session, items),
            total=total,
            limit=page.limit,
            offset=page.offset,
        )
    )


@router.get("/{item_id}", summary="内容详情")
def content_detail(item_id: str, session: Session = DbSession) -> Envelope[ContentDetail]:
    item = get_item(session, item_id)
    logs = list(
        session.scalars(
            select(ReviewLog)
            .where(ReviewLog.content_item_id == item_id)
            .order_by(ReviewLog.at.desc())
        )
    )
    return ok(
        ContentDetail(
            item=build_rows(session, [item], with_machine_review=True)[0],
            bundle=bundle_view(item),
            platform_extra=dict((item.bundle_json or {}).get("platform_extra") or {}),
            logs=audit_entries(logs),
            account_windows=account_windows(session, item),
        )
    )


@router.get("/{item_id}/slots", summary="查询可用改期槽位")
def content_slots(
    item_id: str,
    count: int = Query(default=6, ge=1, le=30, description="返回槽位数，1–30，默认 6"),
    session: Session = DbSession,
) -> Envelope[AvailableSlotsResult]:
    """查询该内容所属账号最近的合法发布槽位，只读不改变内容状态。

    候选严格复用排期模块的窗口、最小间隔、日上限与数据库发布用量；本次响应中
    已返回的候选也会视为占用，避免前端快捷按钮选出彼此冲突的时刻。
    """
    item = get_item(session, item_id)
    account = get_account(session, item.account_id)
    policy = policy_of(account)
    if account.status in {"suspended", "banned"}:
        return ok(
            AvailableSlotsResult(
                item_id=item.id,
                account_id=account.id,
                timezone=policy.timezone,
                note=f"账号当前为 {account.status}，恢复后再查询可用发布时间。",
            )
        )

    slots = available_slots(db_constraints(session, item, account, now=_slot_now()), count=count)
    note = (
        f"已返回最近 {len(slots)} 个合法发布槽位。"
        if len(slots) == count
        else f"未来 14 天内仅找到 {len(slots)} 个合法发布槽位，受窗口、间隔或日上限限制。"
    )
    return ok(
        AvailableSlotsResult(
            item_id=item.id,
            account_id=account.id,
            timezone=policy.timezone,
            slots=[
                AvailableSlot(
                    at=slot,
                    slot_text=format_slot(slot, policy),
                    window=_slot_window_text(slot, policy),
                )
                for slot in slots
            ],
            note=note,
        )
    )


@router.post("/{item_id}/reschedule", summary="人工改期")
def reschedule(
    item_id: str, payload: RescheduleIn, session: Session = DbSession
) -> Envelope[RescheduleResult]:
    """把一条 ``approved`` / ``scheduled`` / ``suspended`` 的内容改到指定时刻。

    非法时刻返回 422 ``invalid_slot``，``error.detail`` 里带：

    - ``reason``：被哪一道挡住（``窗口`` / ``最小间隔`` / ``日上限`` / ``已过去``）
    - ``suggested_slot``：最近一个合法槽位（算不出来时为 null）
    - ``account_windows``：该账号的发布时段，方便前端直接提示

    其它状态（草稿、已发布…）返回 409 ``illegal_transition``。
    """
    item = get_item(session, item_id)
    account = get_account(session, item.account_id)
    try:
        slot = reschedule_item(
            session, item, account, when=payload.scheduled_at, actor=payload.actor
        )
    except NoSlotAvailable as exc:
        suggestion = suggest_slot(session, item, account)
        raise AppError(
            422,
            "invalid_slot",
            str(exc),
            detail={
                "reason": exc.reason,
                "suggested_slot": suggestion.isoformat() if suggestion else None,
                "suggested_slot_text": (
                    format_slot(suggestion, policy_of(account)) if suggestion else ""
                ),
                "account_windows": account_windows(session, item),
            },
        ) from exc
    except IllegalTransition as exc:
        raise AppError(409, "illegal_transition", str(exc)) from exc
    session.commit()
    row = build_rows(session, [item])[0]
    return ok(
        RescheduleResult(
            item=row,
            scheduled_at=slot,
            slot_text=row.slot_text,
            message=f"已改期至 {row.slot_text}",
        )
    )


@router.post("/{item_id}/confirm", summary="确认发布（工作台兜底）")
def confirm(
    item_id: str, payload: ConfirmIn | None = None, session: Session = DbSession
) -> Envelope[ConfirmResult]:
    """人在工作台里点「确认发布」。

    和 Telegram 上那个按钮走的是**同一个函数**（``core.confirm.confirm_item``），
    所以两条路的语义、审计日志、重放保护完全一致。它的存在是为了让 Telegram
    不成为单点：bot 挂了、手机不在身边、群被误删，都还有一条路能把稿子发出去。

    重复确认 / 内容已不在等确认时返回 409 ``confirm_conflict``。
    """
    item = get_item(session, item_id)
    actor = (payload.actor if payload else "") or DEFAULT_ACTOR
    try:
        message = confirm_item(session, item, actor=actor, now=utcnow())
    except ConfirmConflict as exc:
        raise AppError(409, "confirm_conflict", str(exc)) from exc
    session.commit()
    row = build_rows(session, [item])[0]
    return ok(
        ConfirmResult(
            item=row, message=f"{message}（{row.slot_text}）" if row.slot_text else message
        )
    )


@router.post("/{item_id}/reject", summary="不发·驳回（工作台兜底）")
def reject_publish(
    item_id: str, payload: ConfirmIn | None = None, session: Session = DbSession
) -> Envelope[ConfirmResult]:
    """人在工作台里点「不发」。内容退回 ``rejected``，排期槽位让出来。

    理由会写回 ``review_notes`` 给改稿 Agent 当 prompt 输入——这和审核台上的驳回
    是同一套语义（``core.state_machine.reject``）。
    """
    item = get_item(session, item_id)
    actor = (payload.actor if payload else "") or DEFAULT_ACTOR
    reason = (payload.reason if payload else "") or "在工作台点了「不发」"
    try:
        message = reject_confirmation(session, item, actor=actor, reason=reason, now=utcnow())
    except ConfirmConflict as exc:
        raise AppError(409, "confirm_conflict", str(exc)) from exc
    session.commit()
    return ok(ConfirmResult(item=build_rows(session, [item])[0], message=message))


@router.post("/{item_id}/retry_now", summary="立刻重投 / 死信复投")
def retry_now(item_id: str, session: Session = DbSession) -> Envelope[RetryResult]:
    """让卡住的内容重新有机会发出去。

    - ``retrying`` / ``publish_failed``：清掉指数退避，下一轮 ``tick_retry_sweep``
      （默认 5 分钟）立刻重投。**只解退避这一道**——账号健康、限频、48 小时超龄照拦；
    - ``dead_letter``：死信是 P0 冻结的终态，改为**复投一条新的 draft**
      （``new_item_id``），要重新过人工审核；
    - 其它状态返回 409 ``invalid_state``。
    """
    item = get_item(session, item_id)
    outcome = requeue_item(session, item)
    session.commit()
    return ok(
        RetryResult(
            item=build_rows(session, [item])[0],
            mode=outcome.mode,
            message=outcome.message,
            new_item_id=outcome.new_item_id,
        )
    )


__all__ = ["router"]
