"""选题池：看板 + 人工"这条别写了"。

``Topic`` 表没有 ``used`` / ``dismissed`` 列（P0 冻结的模型不动）：

- **used** 是推导出来的——有没有 ``ContentItem.topic_id`` 指向它；
- **dismissed** 写进 ``Topic.raw['dismissed']``（``raw`` 是采集器留的原始 JSON，
  加一个键不影响 ``sourcing/selector.py`` 读的 ``info`` / ``board``）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import exists, select
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
    safe_dt,
    slice_of,
)
from core.errors import AppError
from core.models import ContentItem, Topic, utcnow

router = APIRouter(prefix="/topics", tags=["topics"])

#: ``Topic.raw`` 里放"人工弃用"标记的键
DISMISS_KEY = "dismissed"


class TopicOut(BaseModel):
    id: str
    source: str
    title: str
    url: str | None = None
    score: float = 0.0
    created_at: datetime | None = None
    used: bool = False
    dismissed: bool = False
    dismissed_at: datetime | None = None
    dismissed_by: str = ""
    #: 采集器留的原始字段（热度、榜单名…），前端可直接展示
    raw: dict[str, Any] = Field(default_factory=dict)


class DismissIn(BaseModel):
    actor: str = "operator"
    reason: str = ""
    #: 传 false 可撤销弃用
    dismissed: bool = True


def _used_clause() -> Any:
    return exists().where(ContentItem.topic_id == Topic.id)


def topic_out(topic: Topic, *, used: bool) -> TopicOut:
    raw = dict(topic.raw or {})
    mark = raw.get(DISMISS_KEY) or {}
    return TopicOut(
        id=topic.id,
        source=topic.source,
        title=topic.title,
        url=topic.url,
        score=topic.score,
        created_at=aware(topic.created_at),
        used=used,
        dismissed=bool(mark),
        dismissed_at=safe_dt(mark.get("at")) if isinstance(mark, dict) else None,
        dismissed_by=str(mark.get("by") or "") if isinstance(mark, dict) else "",
        raw={k: v for k, v in raw.items() if k != DISMISS_KEY},
    )


@router.get("", summary="选题池")
def list_topics(
    used: bool | None = Query(default=None, description="true = 已被写成稿子的；false = 还没用过"),
    source: str | None = Query(default=None, description="采集源，如 newsnow / trendradar"),
    page: Pagination = PageParams,
    session: Session = DbSession,
) -> Envelope[Page[TopicOut]]:
    stmt = select(Topic)
    if source:
        stmt = stmt.where(Topic.source == source)
    if used is not None:
        stmt = stmt.where(_used_clause() if used else ~_used_clause())

    total = count_of(session, stmt)
    topics = list(session.scalars(slice_of(stmt.order_by(Topic.created_at.desc(), Topic.id), page)))
    used_ids = set(
        session.scalars(
            select(ContentItem.topic_id).where(
                ContentItem.topic_id.in_([t.id for t in topics] or [""])
            )
        )
    )
    return ok(
        Page(
            items=[topic_out(topic, used=topic.id in used_ids) for topic in topics],
            total=total,
            limit=page.limit,
            offset=page.offset,
        )
    )


@router.post("/{topic_id}/dismiss", summary="弃用 / 恢复一条选题")
def dismiss_topic(
    topic_id: str, payload: DismissIn, session: Session = DbSession
) -> Envelope[TopicOut]:
    """把选题标成"别写了"。标记写在 ``Topic.raw['dismissed']``，选题池列表据此过滤。

    注意：当前 ``sourcing/selector.py`` **还不读**这个标记（P7 接），所以它现在只影响
    工作台的展示，不影响自动选题。
    """
    topic = session.get(Topic, topic_id)
    if topic is None:
        raise AppError(404, "not_found", f"选题不存在: {topic_id}")
    raw = dict(topic.raw or {})
    if payload.dismissed:
        raw[DISMISS_KEY] = {
            "by": payload.actor,
            "at": utcnow().isoformat(),
            "reason": payload.reason,
        }
    else:
        raw.pop(DISMISS_KEY, None)
    topic.raw = raw
    session.commit()
    used = bool(
        session.scalar(select(ContentItem.id).where(ContentItem.topic_id == topic_id).limit(1))
    )
    return ok(topic_out(topic, used=used))


__all__ = ["DISMISS_KEY", "router"]
