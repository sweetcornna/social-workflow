"""后台任务面：渲染任务、发布记录、死信。

三张表都是"系统在干什么"的一手证据，工作台的运维页直接照着渲染即可。
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
    ok,
    slice_of,
)
from core.api.rows import PublishRecordRow
from core.models import ContentItem, PublishRecord, RenderJob
from core.state_machine import ContentStatus

router = APIRouter(prefix="/jobs", tags=["jobs"])


class RenderJobOut(BaseModel):
    id: str
    content_item_id: str
    title: str = ""
    provider: str = "mpt"
    task_id: str | None = None
    #: pending / running / done / failed / lost
    state: str
    progress: int = 0
    attempts: int = 0
    result_paths: list[str] = Field(default_factory=list)
    last_error: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class DeadLetterOut(BaseModel):
    item_id: str
    account_id: str
    title: str
    at: datetime | None = None
    #: 最后一条 ``dead_letter`` 审计日志的理由（截断到 200 字）
    reason: str = ""


@router.get("/render", summary="渲染任务")
def render_jobs(
    state: str | None = Query(default=None, description="pending / running / done / failed / lost"),
    page: Pagination = PageParams,
    session: Session = DbSession,
) -> Envelope[Page[RenderJobOut]]:
    """MoneyPrinterTurbo 渲染任务与进度。``tick_render_jobs`` 每分钟轮询一次。"""
    stmt = select(RenderJob)
    if state:
        stmt = stmt.where(RenderJob.state == state)
    total = count_of(session, stmt)
    jobs = list(session.scalars(slice_of(stmt.order_by(RenderJob.created_at.desc()), page)))
    titles = {
        item.id: item.title
        for item in session.scalars(
            select(ContentItem).where(
                ContentItem.id.in_([job.content_item_id for job in jobs] or [""])
            )
        )
    }
    return ok(
        Page(
            items=[
                RenderJobOut(
                    id=job.id,
                    content_item_id=job.content_item_id,
                    title=titles.get(job.content_item_id, ""),
                    provider=job.provider,
                    task_id=job.task_id,
                    state=job.state,
                    progress=job.progress,
                    attempts=job.attempts,
                    result_paths=list(job.result_paths or []),
                    last_error=job.last_error,
                    meta=dict(job.meta or {}),
                    created_at=aware(job.created_at),
                    updated_at=aware(job.updated_at),
                )
                for job in jobs
            ],
            total=total,
            limit=page.limit,
            offset=page.offset,
        )
    )


@router.get("/publish_records", summary="发布记录")
def publish_records(
    phase: str | None = Query(default=None, description="in_flight / done / failed"),
    account_id: str | None = None,
    page: Pagination = PageParams,
    session: Session = DbSession,
) -> Envelope[Page[PublishRecordRow]]:
    """两阶段发布记录。``idem_key`` 唯一，是幂等的唯一真相来源。"""
    stmt = select(PublishRecord, ContentItem).join(
        ContentItem, ContentItem.id == PublishRecord.content_item_id
    )
    if phase:
        stmt = stmt.where(PublishRecord.phase == phase)
    if account_id:
        stmt = stmt.where(ContentItem.account_id == account_id)
    total = count_of(session, stmt)
    rows = session.execute(slice_of(stmt.order_by(PublishRecord.updated_at.desc()), page)).all()
    return ok(
        Page(
            items=[
                PublishRecordRow(
                    id=record.id,
                    content_item_id=record.content_item_id,
                    account_id=item.account_id,
                    platform=item.platform,
                    title=item.title,
                    idem_key=record.idem_key,
                    phase=record.phase,
                    platform_post_id=record.platform_post_id,
                    url=record.url,
                    attempts=record.attempts,
                    last_error=record.last_error,
                    created_at=aware(record.created_at),
                    updated_at=aware(record.updated_at),
                )
                for record, item in rows
            ],
            total=total,
            limit=page.limit,
            offset=page.offset,
        )
    )


@router.get("/dead_letters", summary="死信")
def dead_letters(
    page: Pagination = PageParams, session: Session = DbSession
) -> Envelope[Page[DeadLetterOut]]:
    """进了死信的内容 + 最后一条失败理由（复用 ``core.stats.recent_dead_letters``）。

    死信是终态，要重新发只能走 ``POST /api/v1/content/{id}/retry_now`` 复投成新草稿。
    """
    from core.stats import recent_dead_letters

    total = count_of(
        session,
        select(ContentItem).where(ContentItem.status == ContentStatus.DEAD_LETTER.value),
    )
    rows = recent_dead_letters(session, limit=page.offset + page.limit)[page.offset :]
    return ok(
        Page(
            items=[
                DeadLetterOut(
                    item_id=row.item_id,
                    account_id=row.account_id,
                    title=row.title,
                    at=aware(row.at),
                    reason=row.reason,
                )
                for row in rows
            ],
            total=total,
            limit=page.limit,
            offset=page.offset,
        )
    )


__all__ = ["router"]
