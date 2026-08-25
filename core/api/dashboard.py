"""首页看板：一屏之内回答"现在要我做什么、系统有没有事"。

聚合逻辑复用 ``core/stats.py`` 的 :func:`~core.stats.build_dashboard`（``/stats`` 页面
用的同一份），这里只做"挑出前端要的那几块 + 混排最近事件"。
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.api.common import DbSession, Envelope, aware, ok
from core.models import (
    ACTIVE_RENDER_STATES,
    ContentItem,
    PublishRecord,
    RenderJob,
    ReviewLog,
    utcnow,
)
from core.state_machine import AccountStatus

router = APIRouter(tags=["dashboard"])

#: 事件流默认给多少条
EVENT_LIMIT = 20


class BudgetLine(BaseModel):
    used: float = 0.0
    limit: float = 0.0
    remaining: float = 0.0


class PlatformHealth(BaseModel):
    platform: str
    accounts: int = 0
    ok: int = 0
    degraded: int = 0
    needs_relogin: int = 0
    banned: int = 0
    #: 人工停用（P10）。既不出稿也不发布，但不是"故障"，前端别把它算进要处理的事
    suspended: int = 0
    pending_review: int = 0
    scheduled: int = 0
    published: int = 0
    used_today: int = 0
    daily_limit: int = 0


class AttentionAccount(BaseModel):
    account_id: str
    name: str = ""
    platform: str
    status: str
    #: 该账号有多少条排期项被挂起（``suspended``），扫码恢复后会自动放回
    suspended: int = 0


class Event(BaseModel):
    """最近发生的事。``kind`` = ``review_log``（审计日志）| ``publish``（发布记录）。"""

    kind: str
    at: datetime | None = None
    actor: str = "system"
    action: str
    item_id: str
    title: str = ""
    account_id: str = ""
    detail: str = ""
    url: str | None = None


class Counters(BaseModel):
    pending_review: int = 0
    #: "今日已发"：各账号**按自己本地日**算的当日发布数之和（P11.3）。多时区共存时
    #: 没有唯一的"今天"，这个口径至少和每个账号行里的 used_today 对得上
    published_today: int = 0
    published_7d: int = 0
    failed: int = 0
    dead_letter: int = 0
    scheduled: int = 0
    suspended: int = 0
    #: 已排期但还等着人点「确认发布」的条数（P12）。这是"无人值守"链路上
    #: **唯一还需要人**的一步，首页把它排在最前面
    awaiting_confirm: int = 0
    rendering: int = 0
    accounts_needing_relogin: int = 0
    #: sidecar / 上传器连不上导致降级的账号数（P10）。和"要扫码"是两回事：
    #: 扫码是人去掏手机，降级是去看那台机器上的容器活没活着
    accounts_degraded: int = 0
    #: 人工停用的账号数。不是故障，只是不该被算进"账号都在线"
    accounts_suspended: int = 0


class DashboardOut(BaseModel):
    generated_at: datetime
    window_days: int = 7
    counters: Counters
    budget: dict[str, BudgetLine] = Field(default_factory=dict)
    platforms: list[PlatformHealth] = Field(default_factory=list)
    attention: list[AttentionAccount] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)


def _awaiting_confirm(session: Session) -> int:
    """还等着人点「确认发布」的条数。

    逐条按账号策略判定（``confirm_required`` 在 ``Account.extra`` 里，SQL 查不了），
    但只扫 ``scheduled`` 且 ``confirmed_at is null`` 的那一小撮，量很小。
    """
    from core.accounts import policy_of
    from core.models import Account, ContentItem
    from core.state_machine import ContentStatus

    rows = session.scalars(
        select(ContentItem).where(
            ContentItem.status == ContentStatus.SCHEDULED.value,
            ContentItem.confirmed_at.is_(None),
        )
    ).all()
    cache: dict[str, bool] = {}
    count = 0
    for item in rows:
        required = cache.get(item.account_id)
        if required is None:
            account = session.get(Account, item.account_id)
            required = bool(account is not None and policy_of(account).confirm_required)
            cache[item.account_id] = required
        count += 1 if required else 0
    return count


def _rendering(session: Session) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(RenderJob)
            .where(RenderJob.state.in_(list(ACTIVE_RENDER_STATES)))
        )
        or 0
    )


def recent_events(session: Session, *, limit: int = EVENT_LIMIT) -> list[Event]:
    """``ReviewLog`` 与 ``PublishRecord`` 混排的最近事件。

    两张表各取 ``limit`` 条再按时间归并 —— 比 UNION 一条 SQL 好读，量也就几十行。
    """
    logs = list(session.scalars(select(ReviewLog).order_by(ReviewLog.at.desc()).limit(limit)).all())
    records = list(
        session.scalars(
            select(PublishRecord).order_by(PublishRecord.updated_at.desc()).limit(limit)
        ).all()
    )
    item_ids = {log.content_item_id for log in logs} | {r.content_item_id for r in records}
    items = {
        item.id: item
        for item in session.scalars(
            select(ContentItem).where(ContentItem.id.in_(list(item_ids)))
        ).all()
    }

    events: list[Event] = []
    for log in logs:
        item = items.get(log.content_item_id)
        events.append(
            Event(
                kind="review_log",
                at=aware(log.at),
                actor=log.actor,
                action=log.action,
                item_id=log.content_item_id,
                title=item.title if item else "",
                account_id=item.account_id if item else "",
                detail=(log.reason or "")[:280],
            )
        )
    for record in records:
        item = items.get(record.content_item_id)
        events.append(
            Event(
                kind="publish",
                at=aware(record.updated_at),
                actor="system",
                action=record.phase,
                item_id=record.content_item_id,
                title=item.title if item else "",
                account_id=item.account_id if item else "",
                detail=(record.last_error or record.platform_post_id or "")[:280],
                url=record.url,
            )
        )
    epoch = datetime.min.replace(tzinfo=UTC)
    events.sort(key=lambda e: e.at or epoch, reverse=True)
    return events[:limit]


@router.get("/dashboard", summary="首页看板")
def dashboard(
    days: int = Query(default=7, ge=1, le=90, description="统计窗口天数"),
    session: Session = DbSession,
) -> Envelope[DashboardOut]:
    """待审 / 今日与 7 天发布 / 失败与死信 / 需重登账号 / 渲染中 / 今日成本 vs 预算 /
    各平台账号健康 / 最近 20 条事件。首页 5 秒轮询它一个就够。"""
    from core.stats import build_dashboard

    dash = build_dashboard(session, window_days=days)
    moment = aware(dash.generated_at) or utcnow()
    totals = dash.totals()

    platforms: dict[str, PlatformHealth] = {}
    attention: list[AttentionAccount] = []
    for row in dash.accounts:
        bucket = platforms.setdefault(row.platform, PlatformHealth(platform=row.platform))
        bucket.accounts += 1
        if row.status in ("ok", "degraded", "needs_relogin", "banned", "suspended"):
            setattr(bucket, row.status, getattr(bucket, row.status) + 1)
        bucket.pending_review += row.pending_review
        bucket.scheduled += row.scheduled
        bucket.published += row.published
        bucket.used_today += row.used_today
        bucket.daily_limit += row.daily_limit
        if row.needs_attention:
            attention.append(
                AttentionAccount(
                    account_id=row.account_id,
                    name=row.name,
                    platform=row.platform,
                    status=row.status,
                    suspended=row.suspended,
                )
            )

    counters = Counters(
        pending_review=totals["pending_review"],
        # 各账号"自己那个今天"之和（P11.3）：每行的 used_today 已按账号本地日切，
        # 这里再按 UTC 日单独查一次库只会得到一个和账号行对不上的数
        published_today=sum(r.used_today for r in dash.accounts),
        published_7d=totals["published"],
        failed=totals["failed"],
        dead_letter=totals["dead_letter"],
        scheduled=totals["scheduled"],
        suspended=sum(r.suspended for r in dash.accounts),
        awaiting_confirm=_awaiting_confirm(session),
        rendering=_rendering(session),
        accounts_needing_relogin=sum(
            1 for r in dash.accounts if r.status == AccountStatus.NEEDS_RELOGIN.value
        ),
        accounts_degraded=sum(1 for r in dash.accounts if r.status == AccountStatus.DEGRADED.value),
        accounts_suspended=sum(
            1 for r in dash.accounts if r.status == AccountStatus.SUSPENDED.value
        ),
    )
    return ok(
        DashboardOut(
            generated_at=moment,
            window_days=days,
            counters=counters,
            budget={k: BudgetLine(**v) for k, v in dash.budget.items()},
            platforms=[platforms[key] for key in sorted(platforms)],
            attention=attention,
            events=recent_events(session),
        )
    )


__all__ = ["router"]
