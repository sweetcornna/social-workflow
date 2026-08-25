"""内容项在列表里的统一行结构（审核队列 / 内容与排期 / 死信共用一张表）。

前端只需要认识**一种**内容行：审核页看 ``needs_watch`` 与 ``machine_review``，
时间线看 ``scheduled_at`` / ``published_at``，用不到的字段是 ``null``。
少一种结构就少一处两边对不上的地方。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.api.common import aware
from core.confirm import confirm_view
from core.content_view import cover_url, media_summary, needs_watch_confirm, slot_text
from core.models import Account, ContentItem, PublishRecord, ReviewLog
from core.review_actions import REQUEUE_ACTION
from core.state_machine import PublishPhase

#: ``review.pipeline.MACHINE_REVIEW_ACTION`` 的字面量副本。
#: 刻意不 import：那条 import 链（review.pipeline → generation.llm）会把 anthropic SDK
#: 拉进控制面的启动路径。``tests/test_api_v1.py`` 有一条断言盯着两者不许漂移。
MACHINE_REVIEW_ACTION = "machine_review"


class MediaSummary(BaseModel):
    """媒体摘要：几张图、几段视频、封面在第几个。"""

    total: int = 0
    images: int = 0
    videos: int = 0
    kinds: list[str] = Field(default_factory=list)
    cover_index: int | None = None


class MachineReview(BaseModel):
    """机器审核（``review/pipeline.py``）的结论摘要。

    逐条 finding 目前**只以文本形式**存在 ``ContentItem.review_notes`` 里
    （``ReviewLog.after_json`` 只记了计数），所以这里给的是计数 + 原文行。
    """

    at: datetime | None = None
    passed: bool | None = None
    blocking: int = 0
    warnings: int = 0
    stages_run: list[str] = Field(default_factory=list)
    stages_skipped: dict[str, str] = Field(default_factory=dict)
    suggested_edits: dict[str, str] = Field(default_factory=dict)
    #: ``review_notes`` 按行拆开，第一行是"机器审核通过/未通过：block x / warn y / info z"
    notes: list[str] = Field(default_factory=list)


class ContentRow(BaseModel):
    """一条内容在列表里的样子。"""

    id: str
    account_id: str
    account_name: str = ""
    platform: str = ""
    title: str = ""
    status: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
    scheduled_at: datetime | None = None
    #: 账号本地时区的排期文案，如 ``08-16 19:30（Asia/Shanghai）``
    slot_text: str = ""
    published_at: datetime | None = None
    platform_post_id: str | None = None
    url: str | None = None
    #: 发布记录的阶段：``in_flight`` / ``done`` / ``failed``；从未发起过则为 null
    publish_phase: str | None = None
    attempts: int = 0
    last_error: str | None = None
    needs_watch: bool = False
    cover_url: str | None = None
    media: MediaSummary = Field(default_factory=MediaSummary)
    tags: list[str] = Field(default_factory=list)
    review_notes: str | None = None
    machine_review: MachineReview | None = None
    #: 时间线锚点：优先已发布时刻，其次排期时刻，最后更新时刻
    timeline_at: datetime | None = None
    # -- 发布前人工确认（P12）--------------------------------------------
    #: 这个账号发布前要不要人点一下
    confirm_required: bool = False
    #: 现在正卡在"等你确认"上
    awaiting_confirm: bool = False
    confirmed_at: datetime | None = None
    confirm_pushed_at: datetime | None = None
    #: **决定期限**：到这个时刻还没人点就自动驳回。和 ``scheduled_at`` 是两个不同的
    #: 时刻，工作台上那处双时刻读数（``19:00 发`` / ``还有 3 小时 40 分决定``）要的
    #: 就是这两个数
    confirm_deadline: datetime | None = None


def _latest_records(session: Session, item_ids: Sequence[str]) -> dict[str, PublishRecord]:
    """每条内容**最近一条**发布记录（done 优先，其次最新的一条）。"""
    if not item_ids:
        return {}
    rows = session.scalars(
        select(PublishRecord)
        .where(PublishRecord.content_item_id.in_(list(item_ids)))
        .order_by(PublishRecord.created_at)
    ).all()
    out: dict[str, PublishRecord] = {}
    for record in rows:
        current = out.get(record.content_item_id)
        if current is None or current.phase != PublishPhase.DONE.value:
            out[record.content_item_id] = record
    return out


def _latest_machine_reviews(session: Session, item_ids: Sequence[str]) -> dict[str, ReviewLog]:
    if not item_ids:
        return {}
    rows = session.scalars(
        select(ReviewLog)
        .where(
            ReviewLog.content_item_id.in_(list(item_ids)),
            ReviewLog.action == MACHINE_REVIEW_ACTION,
        )
        .order_by(ReviewLog.at)
    ).all()
    return {log.content_item_id: log for log in rows}


def machine_review_of(log: ReviewLog | None, notes: str | None) -> MachineReview | None:
    """把 ``machine_review`` 审计日志 + ``review_notes`` 拼成结论摘要。"""
    lines = [line for line in (notes or "").splitlines() if line.strip()]
    if log is None:
        return MachineReview(notes=lines) if lines else None
    after: dict[str, Any] = dict(log.after_json or {})
    return MachineReview(
        at=aware(log.at),
        passed=after.get("passed"),
        blocking=int(after.get("blocking") or 0),
        warnings=int(after.get("warnings") or 0),
        stages_run=list(after.get("stages_run") or []),
        stages_skipped=dict(after.get("stages_skipped") or {}),
        suggested_edits=dict(after.get("suggested_edits") or {}),
        notes=lines,
    )


def build_rows(
    session: Session, items: Sequence[ContentItem], *, with_machine_review: bool = False
) -> list[ContentRow]:
    """批量组行：发布记录与机器审核日志各查一次，避免 N+1。"""
    item_ids = [item.id for item in items]
    records = _latest_records(session, item_ids)
    reviews = _latest_machine_reviews(session, item_ids) if with_machine_review else {}
    accounts: dict[str, Account] = {}
    rows: list[ContentRow] = []
    for item in items:
        account = accounts.get(item.account_id)
        if account is None:
            account = session.get(Account, item.account_id)
            if account is not None:
                accounts[item.account_id] = account
        view = confirm_view(item, account)
        record = records.get(item.id)
        published_at = (
            aware(record.updated_at)
            if record is not None and record.phase == PublishPhase.DONE.value
            else None
        )
        rows.append(
            ContentRow(
                id=item.id,
                account_id=item.account_id,
                account_name=account.name if account else "",
                platform=item.platform or (account.platform if account else ""),
                title=item.title,
                status=item.status,
                created_at=aware(item.created_at),
                updated_at=aware(item.updated_at),
                scheduled_at=aware(item.scheduled_at),
                slot_text=slot_text(session, item),
                published_at=published_at,
                platform_post_id=record.platform_post_id if record else None,
                url=record.url if record else None,
                publish_phase=record.phase if record else None,
                attempts=record.attempts if record else 0,
                last_error=record.last_error if record else None,
                needs_watch=needs_watch_confirm(item),
                cover_url=cover_url(item),
                media=MediaSummary(**media_summary(item)),
                tags=list((item.bundle_json or {}).get("tags") or []),
                review_notes=item.review_notes,
                machine_review=(
                    machine_review_of(reviews.get(item.id), item.review_notes)
                    if with_machine_review
                    else None
                ),
                timeline_at=published_at or aware(item.scheduled_at) or aware(item.updated_at),
                confirm_required=view.required,
                awaiting_confirm=view.awaiting,
                confirmed_at=aware(view.confirmed_at),
                confirm_pushed_at=aware(view.pushed_at),
                confirm_deadline=aware(view.deadline),
            )
        )
    return rows


#: 审计日志里"人做的事"，前端可据此把系统事件折叠起来
HUMAN_ACTIONS = frozenset({"approve", "reject", "edit", "confirm", REQUEUE_ACTION})


class AuditEntry(BaseModel):
    """一条审计日志。"""

    id: str
    actor: str
    action: str
    reason: str | None = None
    at: datetime | None = None
    is_human: bool = False
    has_diff: bool = False


def audit_entries(logs: Sequence[ReviewLog]) -> list[AuditEntry]:
    return [
        AuditEntry(
            id=log.id,
            actor=log.actor,
            action=log.action,
            reason=log.reason,
            at=aware(log.at),
            is_human=log.actor != "system" and log.action in HUMAN_ACTIONS,
            has_diff=bool(log.before_json or log.after_json),
        )
        for log in logs
    ]


class PublishRecordRow(BaseModel):
    """一条两阶段发布记录。"""

    id: str
    content_item_id: str
    account_id: str = ""
    platform: str = ""
    title: str = ""
    idem_key: str
    phase: str
    platform_post_id: str | None = None
    url: str | None = None
    attempts: int = 0
    last_error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


__all__ = [
    "HUMAN_ACTIONS",
    "AuditEntry",
    "ContentRow",
    "MachineReview",
    "MediaSummary",
    "PublishRecordRow",
    "audit_entries",
    "build_rows",
    "machine_review_of",
]
