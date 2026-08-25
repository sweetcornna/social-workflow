"""统一的选题采集入口：跑一遍所有采集器 → 去重 → 落库。

抽出来的原因：``core.dev_flow``（dev 端点）与 ``core.scheduler.tick_sourcing``
（定时任务）要跑的是**同一批采集器**。两处各写一份的话，P4 加了 TrendRadar
只有一边生效，审计时会看到"dev 端点有 trendradar、定时任务没有"这种鬼现象。

契约：单个源不可用（未配置 / 网络抖动 / 归档缺失）**不阻断**其它源，
只把原因收进 ``warnings``——热榜采集本来就是尽力而为。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from sourcing.base import RawTopic, SourceUnavailable, persist_topics

logger = logging.getLogger("social_workflow.sourcing.collector")

Fetcher = Callable[[], list[RawTopic]]


def _newsnow() -> list[RawTopic]:
    from sourcing import newsnow

    return newsnow.fetch(limit_per_board=15)


def _douyin_hot_hub() -> list[RawTopic]:
    from sourcing import douyin_hot_hub

    return douyin_hot_hub.fetch(limit=15)


def _trendradar() -> list[RawTopic]:
    from sourcing import trendradar

    return trendradar.fetch(limit=60)


#: 采集器注册表。名字与模块名一致，也就是 ``Topic.source`` 的取值
SOURCES: dict[str, Fetcher] = {
    "newsnow": _newsnow,
    "douyin_hot_hub": _douyin_hot_hub,
    # P4 接入。GPL sidecar，只走 HTTP，见 sourcing/trendradar.py
    "trendradar": _trendradar,
}


@dataclass
class CollectResult:
    """一次采集的结果。"""

    #: 各源实际拉到的条数（含重复）
    fetched: dict[str, int] = field(default_factory=dict)
    #: 去重后**新增**入库的条数
    created: int = 0
    #: 完全不可用的源及原因
    warnings: list[str] = field(default_factory=list)

    @property
    def total_fetched(self) -> int:
        return sum(self.fetched.values())

    def as_dict(self) -> dict[str, object]:
        return {
            "sources": self.fetched,
            "fetched": self.total_fetched,
            "created": self.created,
            "warnings": self.warnings,
        }


def collect(
    session: Session,
    *,
    sources: Sequence[str] | None = None,
    persist: bool = True,
) -> CollectResult:
    """跑一遍采集器并落库。返回 :class:`CollectResult`。"""
    names = list(sources) if sources is not None else list(SOURCES)
    result = CollectResult()
    collected: list[RawTopic] = []

    for name in names:
        fetcher = SOURCES.get(name)
        if fetcher is None:
            result.warnings.append(f"未知采集源：{name}")
            continue
        try:
            topics = fetcher()
        except SourceUnavailable as exc:
            result.warnings.append(f"{name} 未配置：{exc}")
            logger.info("采集源 %s 未配置，跳过: %s", name, exc)
            continue
        except Exception as exc:  # 网络抖动 / 归档缺失都不该让整条链挂掉
            result.warnings.append(f"{name} 采集失败：{exc}")
            logger.warning("采集源 %s 失败，跳过: %s", name, exc)
            continue
        result.fetched[name] = len(topics)
        collected.extend(topics)

    if collected and persist:
        result.created = len(persist_topics(session, collected))
    logger.info(
        "选题采集：%d 条 → 新增 %d 条（源 %s）",
        result.total_fetched,
        result.created,
        result.fetched,
    )
    return result


__all__ = ["SOURCES", "CollectResult", "Fetcher", "collect"]
