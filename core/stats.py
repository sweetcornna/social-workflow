"""统计页的数据聚合（``/stats`` 与 ``/stats.json`` 共用）。

刻意**不 import** ``metrics.insights`` / ``generation.*``：那条 import 链会把
anthropic SDK 拉进控制面的请求路径。这里只查库，纯 SQL + 一点 Python 归并；
只引入无外部依赖的指标可用性 helper。

口径
----
- **7 天窗口**按 ``PublishRecord.updated_at``（phase=done 时写入 = 发布时刻），
  与 ``metrics.collector.published_at_of`` 一致。
- **指标汇总**取每条内容**最新一张可用**快照再求和。快照是只追加的，直接 SUM 会把
  24h 和 7d 两张算两遍。
- **``None`` 不是 0**：某个字段所有内容都没数据时汇总也是 ``None``
  （小红书的 ``views`` 永远如此），页面上显示 ``—``。
- **成本按账号归集**靠 ``CostLedger.meta['account_id']``（见
  :class:`core.budget.BudgetGuard` 的 ``labels``）。没有标签的流水计入"未归属"。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.accounts import AccountPolicy, policy_of
from core.budget import BudgetGuard, today_key
from core.models import (
    Account,
    ContentItem,
    CostLedger,
    MetricSnapshot,
    PublishRecord,
    ReviewLog,
    utcnow,
)
from core.ratelimit import db_usage
from core.state_machine import REVIEW_QUEUE_STATUSES, AccountStatus, ContentStatus, PublishPhase
from metrics.availability import MetricsPayloadKind, classify_metrics_payload

DEFAULT_WINDOW_DAYS = 7
#: 统一指标字段（契约见 metrics/README.md）
METRIC_FIELDS: tuple[str, ...] = ("views", "likes", "comments", "shares", "collects", "follows")
#: 需要人立刻处理的账号状态，页面上要醒目
ATTENTION_STATUSES = (AccountStatus.NEEDS_RELOGIN.value, AccountStatus.BANNED.value)


@dataclass
class AccountRow:
    """统计页里的一行账号。"""

    account_id: str
    name: str
    platform: str
    status: str
    # -- 策略（来自 accounts.yaml → Account.extra）
    daily_limit: int
    daily_target: int
    windows: str
    min_interval_minutes: int
    # -- 近 N 天
    published: int = 0
    failed: int = 0
    dead_letter: int = 0
    # -- 当前队列
    pending_review: int = 0
    scheduled: int = 0
    suspended: int = 0
    # -- 限频现状（``used_today`` 按**账号本地日**，与限频闸门同一口径）
    used_today: int = 0
    last_published_at: datetime | None = None
    # -- 指标
    metrics: dict[str, int | None] = field(default_factory=dict)
    measured_posts: int = 0
    snapshots_24h: int = 0
    snapshots_7d: int = 0
    # -- 成本
    cost: dict[str, float] = field(default_factory=dict)
    # -- 复盘
    insights_at: str = ""

    @property
    def needs_attention(self) -> bool:
        return self.status in ATTENTION_STATUSES

    @property
    def quota_left(self) -> int:
        return max(self.daily_limit - self.used_today, 0)

    def metric(self, name: str) -> int | None:
        return self.metrics.get(name)


@dataclass
class DeadLetterRow:
    item_id: str
    account_id: str
    title: str
    at: datetime
    reason: str


@dataclass
class Dashboard:
    window_days: int
    day: str
    generated_at: datetime
    accounts: list[AccountRow] = field(default_factory=list)
    content_counts: list[tuple[str, int]] = field(default_factory=list)
    publish_counts: list[tuple[str, int]] = field(default_factory=list)
    budget: dict[str, dict[str, float]] = field(default_factory=dict)
    dead_letters: list[DeadLetterRow] = field(default_factory=list)
    snapshot_count: int = 0
    review_log_count: int = 0
    #: 没带 account_id 标签的成本（复盘 Agent、手工脚本等）
    unattributed_cost: dict[str, float] = field(default_factory=dict)

    @property
    def attention(self) -> list[AccountRow]:
        return [row for row in self.accounts if row.needs_attention]

    def totals(self) -> dict[str, int]:
        return {
            "published": sum(r.published for r in self.accounts),
            "failed": sum(r.failed for r in self.accounts),
            "dead_letter": sum(r.dead_letter for r in self.accounts),
            "pending_review": sum(r.pending_review for r in self.accounts),
            "scheduled": sum(r.scheduled for r in self.accounts),
            "snapshots_24h": sum(r.snapshots_24h for r in self.accounts),
            "snapshots_7d": sum(r.snapshots_7d for r in self.accounts),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "window_days": self.window_days,
            "day": self.day,
            "generated_at": self.generated_at.isoformat(),
            "totals": self.totals(),
            "budget": self.budget,
            "unattributed_cost": self.unattributed_cost,
            "needs_attention": [r.account_id for r in self.attention],
            "accounts": [
                {
                    "id": r.account_id,
                    "platform": r.platform,
                    "status": r.status,
                    "daily_limit": r.daily_limit,
                    "daily_target": r.daily_target,
                    "publish_windows": r.windows,
                    "min_interval_minutes": r.min_interval_minutes,
                    "published": r.published,
                    "failed": r.failed,
                    "dead_letter": r.dead_letter,
                    "pending_review": r.pending_review,
                    "scheduled": r.scheduled,
                    "suspended": r.suspended,
                    "used_today": r.used_today,
                    "last_published_at": (
                        r.last_published_at.isoformat() if r.last_published_at else None
                    ),
                    "metrics": r.metrics,
                    "measured_posts": r.measured_posts,
                    "snapshots_24h": r.snapshots_24h,
                    "snapshots_7d": r.snapshots_7d,
                    "cost": r.cost,
                    "insights_at": r.insights_at,
                }
                for r in self.accounts
            ],
            "dead_letters": [
                {
                    "item_id": d.item_id,
                    "account_id": d.account_id,
                    "title": d.title,
                    "at": d.at.isoformat(),
                    "reason": d.reason,
                }
                for d in self.dead_letters
            ],
        }


# ------------------------------------------------------------------ 分项查询


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _count_items(session: Session, account_id: str, statuses: Sequence[str]) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(ContentItem)
            .where(ContentItem.account_id == account_id, ContentItem.status.in_(list(statuses)))
        )
        or 0
    )


def _count_items_since(
    session: Session, account_id: str, statuses: Sequence[str], since: datetime
) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(ContentItem)
            .where(
                ContentItem.account_id == account_id,
                ContentItem.status.in_(list(statuses)),
                ContentItem.updated_at >= since,
            )
        )
        or 0
    )


def latest_snapshots(
    session: Session, account_id: str, since: datetime
) -> list[tuple[MetricSnapshot, datetime]]:
    """该账号窗口内每条已发内容的**最新一张可用**快照 + 发布时刻。"""
    rows = session.execute(
        select(PublishRecord.content_item_id, func.max(PublishRecord.updated_at))
        .join(ContentItem, ContentItem.id == PublishRecord.content_item_id)
        .where(
            ContentItem.account_id == account_id,
            PublishRecord.phase == PublishPhase.DONE.value,
            PublishRecord.updated_at >= since,
        )
        .group_by(PublishRecord.content_item_id)
    ).all()
    published_at = {item_id: _aware(at) for item_id, at in rows}
    if not published_at:
        return []

    published_item_ids = (
        select(PublishRecord.content_item_id.label("content_item_id"))
        .join(ContentItem, ContentItem.id == PublishRecord.content_item_id)
        .where(
            ContentItem.account_id == account_id,
            PublishRecord.phase == PublishPhase.DONE.value,
            PublishRecord.updated_at >= since,
        )
        .group_by(PublishRecord.content_item_id)
        .subquery()
    )
    newest_usable: dict[str, MetricSnapshot] = {}
    for snapshot in session.scalars(
        select(MetricSnapshot)
        .join(
            published_item_ids,
            published_item_ids.c.content_item_id == MetricSnapshot.content_item_id,
        )
        .order_by(MetricSnapshot.content_item_id, MetricSnapshot.snapshot_at.desc())
        .execution_options(yield_per=500)
    ):
        if classify_metrics_payload(snapshot.metrics_json) is not MetricsPayloadKind.USABLE:
            continue
        newest_usable.setdefault(snapshot.content_item_id, snapshot)

    out: list[tuple[MetricSnapshot, datetime]] = []
    for item_id, at in published_at.items():
        snapshot = newest_usable.get(item_id)
        if snapshot is not None and at is not None:
            out.append((snapshot, at))
    return out


def count_snapshot_windows(session: Session, account_id: str, since: datetime) -> tuple[int, int]:
    """该账号窗口内 24h / 7d 两类快照各有几张。

    窗口不是存出来的，是由 ``snapshot_at`` 与发布时刻推导的
    （见 ``metrics.collector.due_window`` 的同一套口径）。
    """
    from metrics.collector import WINDOW_ORDER, WINDOWS

    rows = session.execute(
        select(PublishRecord.content_item_id, func.max(PublishRecord.updated_at))
        .join(ContentItem, ContentItem.id == PublishRecord.content_item_id)
        .where(
            ContentItem.account_id == account_id,
            PublishRecord.phase == PublishPhase.DONE.value,
            PublishRecord.updated_at >= since,
        )
        .group_by(PublishRecord.content_item_id)
    ).all()
    published_at = {item_id: _aware(at) for item_id, at in rows}
    counts = {"24h": 0, "7d": 0}
    if not published_at:
        return counts["24h"], counts["7d"]
    published_item_ids = (
        select(PublishRecord.content_item_id.label("content_item_id"))
        .join(ContentItem, ContentItem.id == PublishRecord.content_item_id)
        .where(
            ContentItem.account_id == account_id,
            PublishRecord.phase == PublishPhase.DONE.value,
            PublishRecord.updated_at >= since,
        )
        .group_by(PublishRecord.content_item_id)
        .subquery()
    )
    for snapshot in session.scalars(
        select(MetricSnapshot)
        .join(
            published_item_ids,
            published_item_ids.c.content_item_id == MetricSnapshot.content_item_id,
        )
        .execution_options(yield_per=500)
    ):
        if classify_metrics_payload(snapshot.metrics_json) is not MetricsPayloadKind.USABLE:
            continue
        published = published_at.get(snapshot.content_item_id)
        if published is None:
            continue
        moment = _aware(snapshot.snapshot_at)
        if moment is None:
            continue
        label: str | None = None
        for name in WINDOW_ORDER:
            if moment >= published + WINDOWS[name]:
                label = name
        if label:
            counts[label] += 1
    return counts["24h"], counts["7d"]


def count_usable_snapshots(session: Session) -> int:
    """全局累计只计可用 Mapping，坏历史 JSON 不得打断看板请求。"""

    return sum(
        1
        for snapshot in session.scalars(select(MetricSnapshot).execution_options(yield_per=500))
        if classify_metrics_payload(snapshot.metrics_json) is MetricsPayloadKind.USABLE
    )


def account_cost(session: Session, account_id: str, since_day: str) -> dict[str, float]:
    """按账号归集的成本（靠 ``CostLedger.meta['account_id']``）。"""
    rows = session.execute(
        select(CostLedger.kind, func.sum(CostLedger.amount))
        .where(
            CostLedger.day >= since_day,
            func.json_extract(CostLedger.meta, "$.account_id") == account_id,
        )
        .group_by(CostLedger.kind)
    ).all()
    return {kind: float(total or 0.0) for kind, total in rows}


def unattributed_cost(session: Session, since_day: str) -> dict[str, float]:
    rows = session.execute(
        select(CostLedger.kind, func.sum(CostLedger.amount))
        .where(
            CostLedger.day >= since_day,
            func.json_extract(CostLedger.meta, "$.account_id").is_(None),
        )
        .group_by(CostLedger.kind)
    ).all()
    return {kind: float(total or 0.0) for kind, total in rows}


def truncated_calls(session: Session, since_day: str) -> dict[str, int]:
    """哪些调用点是被 ``max_tokens`` 掐停收尾的，按 ``purpose`` 计数。

    输出预算长期贴边不会自己报警：截断自愈让链路照常出稿（只是白烧一倍 token），
    直到某次思考跑偏、正文一个字都没写出来才 502。所以要能主动看见"最近有几次
    是顶着天花板结束的"，趁它还只是浪费的时候把预算调上去。

    数据来源是 ``CostLedger.meta['stop_reason']``（两个后端都记，见
    :func:`generation.llm.charge_usage`）。P11.2 之前这个字段根本没落库，
    只能靠 ``output_tokens`` 是不是正好等于某个整数去猜——那本身就是个观测缺口。
    """
    rows = session.execute(
        select(
            func.json_extract(CostLedger.meta, "$.purpose"),
            func.count(),
        )
        .where(
            CostLedger.day >= since_day,
            func.json_extract(CostLedger.meta, "$.stop_reason") == "max_tokens",
        )
        .group_by(func.json_extract(CostLedger.meta, "$.purpose"))
    ).all()
    return {str(purpose or "（未标注）"): int(count) for purpose, count in rows}


def recent_dead_letters(session: Session, *, limit: int = 10) -> list[DeadLetterRow]:
    items = list(
        session.scalars(
            select(ContentItem)
            .where(ContentItem.status == ContentStatus.DEAD_LETTER.value)
            .order_by(ContentItem.updated_at.desc())
            .limit(limit)
        )
    )
    out: list[DeadLetterRow] = []
    for item in items:
        log = session.scalars(
            select(ReviewLog)
            .where(ReviewLog.content_item_id == item.id, ReviewLog.action == "dead_letter")
            .order_by(ReviewLog.at.desc())
            .limit(1)
        ).first()
        out.append(
            DeadLetterRow(
                item_id=item.id,
                account_id=item.account_id,
                title=item.title or "（无标题）",
                at=_aware(item.updated_at) or utcnow(),
                reason=(log.reason if log and log.reason else "")[:200],
            )
        )
    return out


# ------------------------------------------------------------------------ 入口


def build_account_row(
    session: Session,
    account: Account,
    policy: AccountPolicy,
    *,
    now: datetime,
    since: datetime,
    since_day: str,
) -> AccountRow:
    # 本地日口径（P11.3）：这一行的 used_today / quota_left 就是限频闸门看到的数
    usage = db_usage(session, account.id, now=now, timezone=policy.timezone)
    row = AccountRow(
        account_id=account.id,
        name=account.name,
        platform=account.platform,
        status=account.status,
        daily_limit=policy.daily_limit,
        daily_target=policy.daily_target,
        windows=policy.window_text(),
        min_interval_minutes=int(policy.min_interval.total_seconds() // 60),
        used_today=usage.count_today,
        last_published_at=usage.last_at,
        insights_at=str((account.extra or {}).get("insights_updated_at") or ""),
    )
    row.published = int(
        session.scalar(
            select(func.count(PublishRecord.id))
            .join(ContentItem, ContentItem.id == PublishRecord.content_item_id)
            .where(
                ContentItem.account_id == account.id,
                PublishRecord.phase == PublishPhase.DONE.value,
                PublishRecord.updated_at >= since,
            )
        )
        or 0
    )
    row.failed = _count_items_since(
        session,
        account.id,
        (ContentStatus.PUBLISH_FAILED.value, ContentStatus.RETRYING.value),
        since,
    )
    row.dead_letter = _count_items_since(
        session, account.id, (ContentStatus.DEAD_LETTER.value,), since
    )
    row.pending_review = _count_items(session, account.id, [s.value for s in REVIEW_QUEUE_STATUSES])
    row.scheduled = _count_items(session, account.id, (ContentStatus.SCHEDULED.value,))
    row.suspended = _count_items(session, account.id, (ContentStatus.SUSPENDED.value,))

    snapshots = latest_snapshots(session, account.id, since)
    row.measured_posts = len(snapshots)
    for name in METRIC_FIELDS:
        values = [
            snapshot.metrics_json.get(name)
            for snapshot, _ in snapshots
            if isinstance(snapshot.metrics_json, dict)
        ]
        present = [v for v in values if isinstance(v, int)]
        row.metrics[name] = sum(present) if present else None
    row.snapshots_24h, row.snapshots_7d = count_snapshot_windows(session, account.id, since)
    row.cost = account_cost(session, account.id, since_day)
    return row


def build_dashboard(
    session: Session,
    *,
    now: datetime | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> Dashboard:
    """把 ``/stats`` 需要的一切算出来。"""
    moment = now or utcnow()
    since = moment - timedelta(days=window_days)
    since_day = (moment - timedelta(days=window_days)).date().isoformat()

    dashboard = Dashboard(
        window_days=window_days,
        day=today_key(moment),
        generated_at=moment,
        budget=BudgetGuard(session, day=today_key(moment)).snapshot(),
        unattributed_cost=unattributed_cost(session, since_day),
        dead_letters=recent_dead_letters(session),
        snapshot_count=count_usable_snapshots(session),
        review_log_count=int(session.scalar(select(func.count()).select_from(ReviewLog)) or 0),
    )
    dashboard.content_counts = [
        (str(status), int(count))
        for status, count in session.execute(
            select(ContentItem.status, func.count())
            .group_by(ContentItem.status)
            .order_by(ContentItem.status)
        ).all()
    ]
    dashboard.publish_counts = [
        (str(phase), int(count))
        for phase, count in session.execute(
            select(PublishRecord.phase, func.count())
            .group_by(PublishRecord.phase)
            .order_by(PublishRecord.phase)
        ).all()
    ]

    accounts = list(session.scalars(select(Account).order_by(Account.platform, Account.id)))
    for account in accounts:
        dashboard.accounts.append(
            build_account_row(
                session,
                account,
                policy_of(account),
                now=moment,
                since=since,
                since_day=since_day,
            )
        )
    # 需要人处理的排最上面
    dashboard.accounts.sort(key=lambda r: (not r.needs_attention, r.platform, r.account_id))
    return dashboard


__all__ = [
    "ATTENTION_STATUSES",
    "DEFAULT_WINDOW_DAYS",
    "METRIC_FIELDS",
    "AccountRow",
    "Dashboard",
    "DeadLetterRow",
    "account_cost",
    "build_account_row",
    "build_dashboard",
    "count_snapshot_windows",
    "latest_snapshots",
    "recent_dead_letters",
    "truncated_calls",
    "unattributed_cost",
]
