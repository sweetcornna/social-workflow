"""选题采集层共用 DTO 与落库工具。

各采集器（newsnow / douyin_hot_hub / …）只负责把源站数据归一化成 :class:`RawTopic`，
入库与去重统一走 :func:`persist_topics`，避免每个采集器各写一份。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.models import Topic, new_id, utcnow

logger = logging.getLogger("social_workflow.sourcing")

#: 同源多少天内的选题参与去重
DEFAULT_DEDUPE_WINDOW_DAYS = 7


class SourcingError(Exception):
    """采集层异常基类。"""


class SourceUnavailable(SourcingError):
    """数据源未配置或不可达。"""


class RawTopic(BaseModel):
    """采集器的统一输出。落库前不写 DB，纯值对象。"""

    model_config = ConfigDict(extra="forbid")

    #: 与模块名一致：newsnow / douyin_hot_hub / xhs_search / trendradar
    source: str
    title: str
    url: str | None = None
    #: 归一化到 [0, 1] 的热度分，跨源可比
    score: float = 0.0
    #: 源站原始 JSON，不裁剪，便于事后复盘
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title 不能为空")
        return value

    @field_validator("score")
    @classmethod
    def _score_in_range(cls, value: float) -> float:
        return min(max(float(value), 0.0), 1.0)


def rank_score(rank: int, total: int) -> float:
    """把榜单名次线性映射到 ``[0, 1]``：第 1 名得 1.0，末名得 ``1/total``。

    热榜大多只给名次不给可比的热度值，名次是唯一跨源可比的信号。
    """
    if total <= 0:
        return 0.0
    rank = max(1, min(rank, total))
    return round((total - rank + 1) / total, 6)


def persist_topics(
    session: Session,
    topics: Sequence[RawTopic],
    *,
    window_days: int = DEFAULT_DEDUPE_WINDOW_DAYS,
    cross_source: bool = True,
) -> list[Topic]:
    """去重后写入 ``core.models.Topic``，返回**新增**的行。

    去重对象包含两部分：本批次内部，以及最近 ``window_days`` 天已入库的选题。
    ``cross_source=True`` 时跨源去重（微博和知乎的同一热点只留一条）。
    """
    from sourcing.dedupe import Deduper

    if not topics:
        return []

    since = utcnow() - timedelta(days=window_days)
    stmt = select(Topic).where(Topic.created_at >= since)
    if not cross_source:
        stmt = stmt.where(Topic.source.in_({t.source for t in topics}))
    existing = list(session.scalars(stmt))

    deduper = Deduper()
    for row in existing:
        deduper.add(row.title)

    created: list[Topic] = []
    for item in topics:
        if not deduper.add(item.title):
            logger.debug("选题去重命中，跳过: %s", item.title)
            continue
        row = Topic(
            id=new_id("tpc"),
            source=item.source,
            title=item.title[:512],
            url=item.url,
            score=item.score,
            raw=item.raw,
        )
        session.add(row)
        created.append(row)
    if created:
        session.flush()
    logger.info("选题入库 %d/%d 条（其余为重复）", len(created), len(topics))
    return created


def normalize_batch(topics: Iterable[RawTopic]) -> list[RawTopic]:
    """批内去重（不查库），保序保留首次出现的那条。"""
    from sourcing.dedupe import Deduper

    deduper = Deduper()
    return [item for item in topics if deduper.add(item.title)]


__all__ = [
    "DEFAULT_DEDUPE_WINDOW_DAYS",
    "RawTopic",
    "SourceUnavailable",
    "SourcingError",
    "normalize_batch",
    "persist_topics",
    "rank_score",
]
