"""审核：队列、详情、批准 / 驳回 / 改稿。

三个写端点走的是 ``core/review_actions.py`` —— 与 ``/review/{id}/approve`` 表单端点
**同一批函数**，所以"成片必须看完整片"、"公众号逐条确认"、"批准即排期"这三道闸门
在 JSON 这条路上一个都不少。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.api.common import (
    DbSession,
    Envelope,
    Page,
    PageParams,
    Pagination,
    aware,
    count_of,
    get_item,
    ok,
    slice_of,
)
from core.api.rows import AuditEntry, ContentRow, MachineReview, audit_entries, build_rows
from core.content_view import account_windows, bundle_view, latest_edit_diff
from core.models import Account, ContentItem, PublishRecord, ReviewLog
from core.review_actions import approve_item, edit_item, reject_item
from core.state_machine import REVIEW_QUEUE_STATUSES

router = APIRouter(prefix="/review", tags=["review"])


# ------------------------------------------------------------------ 请求 / 响应


class ApproveIn(BaseModel):
    actor: str = Field(default="operator", description="操作人，会写进审计日志")
    reason: str | None = Field(default=None, description="批准备注，可空")
    watched: bool = Field(
        default=False,
        description="内容包里有视频时必须为 true（人已完整看过成片），否则 422 watch_required",
    )


class RejectIn(BaseModel):
    actor: str = "operator"
    reason: str = Field(description="驳回理由，必填；会回写 review_notes 供改稿 Agent 使用")


class EditIn(BaseModel):
    actor: str = "operator"
    title: str
    body_markdown: str
    tags: list[str] = Field(default_factory=list)
    reason: str | None = None


class ActionResult(BaseModel):
    """写操作的统一返回：改完之后这条内容长什么样 + 一句给人看的话。"""

    item: ContentRow
    message: str
    scheduled: bool = False
    scheduled_at: datetime | None = None
    slot_text: str = ""


class SlotInfo(BaseModel):
    scheduled_at: datetime | None = None
    slot_text: str = ""
    #: 该账号的发布时段窗口文案，解释"为什么排到这个点"
    account_windows: str = ""


class ReviewDetail(BaseModel):
    item: ContentRow
    #: 归一化后的内容包（媒体带下标与 exists 标记，直接对应 /review/{id}/media/{i}）
    bundle: dict[str, Any]
    platform_extra: dict[str, Any] = Field(default_factory=dict)
    machine_review: MachineReview | None = None
    logs: list[AuditEntry] = Field(default_factory=list)
    slot: SlotInfo = Field(default_factory=SlotInfo)
    #: 最近一次人工改稿的 unified diff（没改过则空串）
    diff: str = ""
    #: 媒体直接走既有端点：/review/{id}/media/{index} 与 /review/{id}/cover
    media_url_template: str = "/review/{item_id}/media/{index}"


# ---------------------------------------------------------------------- 端点


def _one_row(session: Session, item: ContentItem) -> ContentRow:
    return build_rows(session, [item], with_machine_review=True)[0]


@router.get("", summary="审核队列")
def list_review(
    status: str | None = Query(
        default=None,
        description="内容状态；留空 = 人工队列（draft/reviewing/rejected），all = 全部",
    ),
    platform: str | None = Query(default=None, description="wechat_mp / xhs / douyin"),
    account_id: str | None = None,
    page: Pagination = PageParams,
    session: Session = DbSession,
) -> Envelope[Page[ContentRow]]:
    """待人工处置的内容。默认只给 ``draft`` / ``reviewing`` / ``rejected`` 三档。"""
    stmt = select(ContentItem).join(Account, Account.id == ContentItem.account_id)
    if status is None:
        stmt = stmt.where(ContentItem.status.in_([s.value for s in REVIEW_QUEUE_STATUSES]))
    elif status != "all":
        stmt = stmt.where(ContentItem.status == status)
    if platform:
        stmt = stmt.where(Account.platform == platform)
    if account_id:
        stmt = stmt.where(ContentItem.account_id == account_id)

    total = count_of(session, stmt)
    items = list(session.scalars(slice_of(stmt.order_by(ContentItem.updated_at.desc()), page)))
    return ok(
        Page(
            items=build_rows(session, items, with_machine_review=True),
            total=total,
            limit=page.limit,
            offset=page.offset,
        )
    )


@router.get("/{item_id}", summary="审核详情（全量）")
def review_detail(item_id: str, session: Session = DbSession) -> Envelope[ReviewDetail]:
    item = get_item(session, item_id)
    logs = list(
        session.scalars(
            select(ReviewLog)
            .where(ReviewLog.content_item_id == item_id)
            .order_by(ReviewLog.at.desc())
        )
    )
    row = _one_row(session, item)
    return ok(
        ReviewDetail(
            item=row,
            bundle=bundle_view(item),
            platform_extra=dict((item.bundle_json or {}).get("platform_extra") or {}),
            machine_review=row.machine_review,
            logs=audit_entries(logs),
            slot=SlotInfo(
                scheduled_at=aware(item.scheduled_at),
                slot_text=row.slot_text,
                account_windows=account_windows(session, item),
            ),
            diff=latest_edit_diff(session, item_id),
        )
    )


@router.get("/{item_id}/records", summary="该内容的发布记录")
def review_records(item_id: str, session: Session = DbSession) -> Envelope[list[dict[str, Any]]]:
    """详情页右栏的发布尝试历史（``/api/v1/jobs/publish_records`` 的单条版）。"""
    get_item(session, item_id)
    records = session.scalars(
        select(PublishRecord)
        .where(PublishRecord.content_item_id == item_id)
        .order_by(PublishRecord.created_at.desc())
    ).all()
    return ok(
        [
            {
                "id": r.id,
                "phase": r.phase,
                "attempts": r.attempts,
                "platform_post_id": r.platform_post_id,
                "url": r.url,
                "last_error": r.last_error,
                "created_at": aware(r.created_at),
                "updated_at": aware(r.updated_at),
            }
            for r in records
        ]
    )


@router.post("/{item_id}/approve", summary="批准（人工卡点）")
def approve(
    item_id: str, payload: ApproveIn, session: Session = DbSession
) -> Envelope[ActionResult]:
    """语义与 ``POST /review/{id}/approve`` 表单端点完全一致。

    - 只有 ``draft`` / ``reviewing`` 能批准，否则 409 ``invalid_state``；
    - 内容包里有视频时必须 ``watched=true``，否则 422 ``watch_required``；
    - 公众号会写入本条内容的 ``confirm_publish``（双确认闸门第三道）；
    - 批准后立刻排期：排上了 ``scheduled=true`` 并带 ``scheduled_at``；
      排不上**不报错**，内容停在 ``approved``，``message`` 里写明被哪一道挡住。
    """
    item = get_item(session, item_id)
    outcome = approve_item(
        session, item, actor=payload.actor, reason=payload.reason, watched=payload.watched
    )
    session.commit()
    return ok(
        ActionResult(
            item=_one_row(session, item),
            message=outcome.message,
            scheduled=outcome.scheduled,
            scheduled_at=aware(outcome.slot),
            slot_text=outcome.slot_text,
        )
    )


@router.post("/{item_id}/reject", summary="驳回")
def reject(item_id: str, payload: RejectIn, session: Session = DbSession) -> Envelope[ActionResult]:
    """理由必填（空白 422 ``reason_required``），会回写 ``review_notes``。"""
    item = get_item(session, item_id)
    reject_item(session, item, actor=payload.actor, reason=payload.reason)
    session.commit()
    return ok(ActionResult(item=_one_row(session, item), message="已驳回，理由已回写"))


@router.post("/{item_id}/edit", summary="人工改稿")
def edit(item_id: str, payload: EditIn, session: Session = DbSession) -> Envelope[ActionResult]:
    """改标题 / 正文 / 标签。改完状态回到 ``draft``，before/after 进审计日志出 diff。"""
    item = get_item(session, item_id)
    edit_item(
        session,
        item,
        actor=payload.actor,
        title=payload.title,
        body_markdown=payload.body_markdown,
        tags=payload.tags,
        reason=payload.reason,
    )
    session.commit()
    return ok(ActionResult(item=_one_row(session, item), message="改稿已保存"))


__all__ = ["router"]
