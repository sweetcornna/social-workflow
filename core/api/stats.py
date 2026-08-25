"""统计与成本。

``/api/v1/stats`` 直接复用 ``core/stats.py`` 的 :func:`~core.stats.build_dashboard`
（``/stats`` 页面用的同一份聚合），额外补一条**按天的序列**给前端画折线——页面版是
表格，不需要它，所以那边没有。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.api.common import DbSession, Envelope, ok
from core.budget import BudgetGuard, today_key
from core.models import Account, ContentItem, CostLedger, PublishRecord, utcnow
from core.state_machine import PublishPhase

router = APIRouter(tags=["stats"])


class DailyPoint(BaseModel):
    """一天一个点。``day`` 是 UTC 日期（``YYYY-MM-DD``）。"""

    day: str
    published: int = 0
    #: 当天新进死信的条数
    dead_letter: int = 0
    cost: dict[str, float] = Field(default_factory=dict)


class StatsOut(BaseModel):
    window_days: int
    day: str
    generated_at: datetime
    totals: dict[str, int] = Field(default_factory=dict)
    budget: dict[str, dict[str, float]] = Field(default_factory=dict)
    #: 按账号的明细，结构与 ``GET /stats.json`` 完全一致（core/stats.py 的 as_dict）
    accounts: list[dict[str, Any]] = Field(default_factory=list)
    dead_letters: list[dict[str, Any]] = Field(default_factory=list)
    needs_attention: list[str] = Field(default_factory=list)
    unattributed_cost: dict[str, float] = Field(default_factory=dict)
    content_counts: dict[str, int] = Field(default_factory=dict)
    publish_counts: dict[str, int] = Field(default_factory=dict)
    #: 近 N 天的每日序列（含没有数据的日子，值为 0），前端直接画图
    daily: list[DailyPoint] = Field(default_factory=list)


class AccountCost(BaseModel):
    account_id: str
    name: str = ""
    platform: str = ""
    cost: dict[str, float] = Field(default_factory=dict)


class CostsOut(BaseModel):
    days: int
    since_day: str
    today: str
    #: 今天的预算闸门现状：``{"tokens": {"used","limit","remaining"}, ...}``
    budget: dict[str, dict[str, float]] = Field(default_factory=dict)
    by_day: list[DailyPoint] = Field(default_factory=list)
    by_account: list[AccountCost] = Field(default_factory=list)
    unattributed: dict[str, float] = Field(default_factory=dict)
    totals: dict[str, float] = Field(default_factory=dict)


def _day_keys(days: int, *, now: datetime) -> list[str]:
    start = now.date() - timedelta(days=days - 1)
    return [(start + timedelta(days=offset)).isoformat() for offset in range(days)]


def _cost_by_day(session: Session, since_day: str) -> dict[str, dict[str, float]]:
    rows = session.execute(
        select(CostLedger.day, CostLedger.kind, func.sum(CostLedger.amount))
        .where(CostLedger.day >= since_day)
        .group_by(CostLedger.day, CostLedger.kind)
    ).all()
    out: dict[str, dict[str, float]] = {}
    for day, kind, total in rows:
        out.setdefault(str(day), {})[str(kind)] = float(total or 0.0)
    return out


def _published_by_day(session: Session, since: datetime) -> dict[str, int]:
    rows = session.execute(
        select(func.date(PublishRecord.updated_at), func.count())
        .where(
            PublishRecord.phase == PublishPhase.DONE.value,
            PublishRecord.updated_at >= since,
        )
        .group_by(func.date(PublishRecord.updated_at))
    ).all()
    return {str(day): int(count) for day, count in rows}


def _dead_letters_by_day(session: Session, since: datetime) -> dict[str, int]:
    from core.state_machine import ContentStatus

    rows = session.execute(
        select(func.date(ContentItem.updated_at), func.count())
        .where(
            ContentItem.status == ContentStatus.DEAD_LETTER.value,
            ContentItem.updated_at >= since,
        )
        .group_by(func.date(ContentItem.updated_at))
    ).all()
    return {str(day): int(count) for day, count in rows}


def daily_series(session: Session, *, days: int, now: datetime | None = None) -> list[DailyPoint]:
    """近 ``days`` 天的每日序列（UTC 切日，与 ``CostLedger.day`` 同一口径）。"""
    moment = now or utcnow()
    since = moment - timedelta(days=days)
    published = _published_by_day(session, since)
    dead = _dead_letters_by_day(session, since)
    costs = _cost_by_day(session, (moment.date() - timedelta(days=days - 1)).isoformat())
    return [
        DailyPoint(
            day=key,
            published=published.get(key, 0),
            dead_letter=dead.get(key, 0),
            cost=costs.get(key, {}),
        )
        for key in _day_keys(days, now=moment)
    ]


@router.get("/stats", summary="运营统计")
def stats(
    days: int = Query(default=7, ge=1, le=90),
    session: Session = DbSession,
) -> Envelope[StatsOut]:
    """按账号的近 N 天看板 + 每日序列。字段口径见 core/stats.py 的模块 docstring。"""
    from core.stats import build_dashboard

    dash = build_dashboard(session, window_days=days)
    payload = dash.as_dict()
    return ok(
        StatsOut(
            window_days=payload["window_days"],
            day=payload["day"],
            generated_at=payload["generated_at"],
            totals=payload["totals"],
            budget=payload["budget"],
            accounts=payload["accounts"],
            dead_letters=payload["dead_letters"],
            needs_attention=payload["needs_attention"],
            unattributed_cost=payload["unattributed_cost"],
            content_counts=dict(dash.content_counts),
            publish_counts=dict(dash.publish_counts),
            daily=daily_series(session, days=days),
        )
    )


@router.get("/costs", summary="成本与预算")
def costs(
    days: int = Query(default=30, ge=1, le=365),
    session: Session = DbSession,
) -> Envelope[CostsOut]:
    """``CostLedger`` 按日 / 按账号聚合 + 今日预算余量。

    按账号归集靠 ``CostLedger.meta['account_id']``（``BudgetGuard`` 的 labels）；
    没有标签的流水（复盘 Agent、手工脚本）计入 ``unattributed``。
    """
    from core.stats import account_cost, unattributed_cost

    moment = utcnow()
    since_day = (moment.date() - timedelta(days=days - 1)).isoformat()
    series = daily_series(session, days=days, now=moment)

    by_account: list[AccountCost] = []
    totals: dict[str, float] = {}
    for account in session.scalars(select(Account).order_by(Account.platform, Account.id)):
        cost = account_cost(session, account.id, since_day)
        by_account.append(
            AccountCost(
                account_id=account.id,
                name=account.name,
                platform=account.platform,
                cost=cost,
            )
        )
    for point in series:
        for kind, amount in point.cost.items():
            totals[kind] = totals.get(kind, 0.0) + amount

    return ok(
        CostsOut(
            days=days,
            since_day=since_day,
            today=today_key(moment),
            budget=BudgetGuard(session, day=today_key(moment)).snapshot(),
            by_day=series,
            by_account=by_account,
            unattributed=unattributed_cost(session, since_day),
            totals=totals,
        )
    )


__all__ = ["daily_series", "router"]
