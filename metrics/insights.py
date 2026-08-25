"""复盘 Agent：7 天指标 → Claude 结论 → 写回选题上下文（P4）。

闭环的最后一环
--------------
``metrics/collector.py`` 只负责把指标**存下来**，没人读它。本模块把它接回选题：

    MetricSnapshot(7d) ──汇总──► Claude 结构化复盘 ──► prompts/accounts/<id>/insights.md
                                                              │
                            sourcing/selector.py ◄────读────┘

写文件而不是写库，理由和 ``persona.md`` 一样：复盘结论是**人也要看、要能手改**的
资产，应该躺在 git 里出 diff。

约束
----
- 结构化输出走 ``SupportsLLM.parse``（Pydantic），不让模型自由写 JSON。
  后端由 ``SW_LLM_BACKEND`` 决定（anthropic / dsh），本模块不关心是哪一个。
- 预算受 :class:`~core.budget.BudgetGuard` 管；耗尽就跳过并留日志，不抛到调度器。
- **当前后端的凭据缺失时整体跳过**（统计里记 ``skipped_no_key``），
  不像生成链那样回落到 ScriptedLLM——复盘产出的是会持续影响后续选题的长期资产，
  用预置假文本污染它比不写更糟。测试用 ``llm=`` 显式注入 ScriptedLLM。
- 样本太小（7 天内已发 < ``INSIGHTS_MIN_POSTS``）不复盘：三条数据推不出规律，
  只会生成一段像模像样的噪声。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

import prompts
from core.accounts import AccountPolicy, policy_of
from core.budget import BudgetExhausted, BudgetGuard
from core.models import Account, ContentItem, MetricSnapshot, PublishRecord, Topic, utcnow
from core.state_machine import ContentStatus, PublishPhase
from generation.llm import LLMError, SupportsLLM
from generation.output_budget import budget_for
from metrics.availability import MetricsPayloadKind, classify_metrics_payload

logger = logging.getLogger("social_workflow.metrics.insights")

DEFAULT_WINDOW_DAYS = 7
#: 统一指标字段（见 metrics/README.md 的契约）。``None`` 表示"没数据"，不是 0
METRIC_FIELDS: tuple[str, ...] = ("views", "likes", "comments", "shares", "collects", "follows")


# --------------------------------------------------------------------- 结构化输出


class InsightsReport(BaseModel):
    """复盘 Agent 的结构化输出。"""

    model_config = ConfigDict(extra="forbid")

    headline: str = Field(description="一句话概括这段时间")
    what_worked: list[str] = Field(default_factory=list, description="奏效的模式，每条附数据依据")
    what_failed: list[str] = Field(default_factory=list)
    topic_guidance: list[str] = Field(default_factory=list, description="下一轮的选题方向")
    title_patterns: list[str] = Field(default_factory=list, description="可复用的标题结构")
    best_slots: list[str] = Field(default_factory=list, description="表现好的发布时段")
    next_actions: list[str] = Field(default_factory=list)
    confidence: Literal["low", "medium", "high"] = "low"
    note: str = ""

    def to_markdown(self, *, account_id: str, window_days: int, at: datetime) -> str:
        """渲染成 insights.md 里的一条。格式要让人一眼看懂，也要让模型好读。"""
        lines = [
            f"## {at.date().isoformat()} · 近 {window_days} 天复盘（{account_id}）",
            "",
            f"**{self.headline.strip()}**",
            "",
            f"- 置信度：`{self.confidence}`",
        ]
        for label, items in (
            ("奏效", self.what_worked),
            ("没奏效", self.what_failed),
            ("选题方向", self.topic_guidance),
            ("标题结构", self.title_patterns),
            ("发布时段", self.best_slots),
            ("下一步", self.next_actions),
        ):
            if not items:
                continue
            lines.append(f"- {label}：")
            lines.extend(f"  - {item.strip()}" for item in items if item.strip())
        if self.note.strip():
            lines += ["", f"> {self.note.strip()}"]
        return "\n".join(lines)

    def as_context(self) -> str:
        """喂给选题 Agent 的精简版（只留能改变下一次决策的部分）。"""
        chunks: list[str] = [self.headline.strip()]
        for label, items in (
            ("继续做", self.what_worked),
            ("别再做", self.what_failed),
            ("选题方向", self.topic_guidance),
            ("标题结构", self.title_patterns),
        ):
            if items:
                chunks.append(f"{label}：" + "；".join(i.strip() for i in items if i.strip()))
        return "\n".join(c for c in chunks if c)


# ------------------------------------------------------------------------ 汇总


@dataclass(frozen=True)
class PostStat:
    """一条已发内容在窗口内的表现。"""

    item_id: str
    title: str
    published_at: datetime
    url: str | None
    topic_source: str
    metrics: dict[str, Any]
    tags: list[str]
    angle: str

    def local_slot(self, policy: AccountPolicy) -> str:
        local = self.published_at.astimezone(policy.tzinfo)
        return local.strftime("%m-%d %a %H:%M")

    def metric(self, name: str) -> int | None:
        value = self.metrics.get(name)
        return value if isinstance(value, int) else None

    @property
    def engagement(self) -> int:
        """互动总量（缺失的字段按缺失处理，不补 0 再求和会更误导）。"""
        return sum(v for v in (self.metric(n) for n in ("likes", "comments", "shares")) if v)


@dataclass
class AccountSummary:
    """一个账号在窗口内的全部素材。"""

    account_id: str
    platform: str
    window_days: int
    posts: list[PostStat] = field(default_factory=list)
    failed: int = 0
    dead_letter: int = 0
    #: 有发布记录但一次指标都没采到的条数
    unmeasured: int = 0

    @property
    def published(self) -> int:
        return len(self.posts)

    def totals(self) -> dict[str, int | None]:
        out: dict[str, int | None] = {}
        for name in METRIC_FIELDS:
            values = [p.metric(name) for p in self.posts]
            present = [v for v in values if v is not None]
            out[name] = sum(present) if present else None
        return out


def _latest_usable_snapshots(
    session: Session,
    *,
    account_id: str,
    since: datetime,
) -> dict[str, MetricSnapshot]:
    """批量取每条内容最新可用快照，坏历史 JSON 不参与复盘。"""

    newest: dict[str, MetricSnapshot] = {}
    for snapshot in session.scalars(
        select(MetricSnapshot)
        .join(ContentItem, ContentItem.id == MetricSnapshot.content_item_id)
        .where(ContentItem.account_id == account_id, ContentItem.updated_at >= since)
        .order_by(MetricSnapshot.content_item_id, MetricSnapshot.snapshot_at.desc())
        .execution_options(yield_per=500)
    ):
        if classify_metrics_payload(snapshot.metrics_json) is not MetricsPayloadKind.USABLE:
            continue
        newest.setdefault(snapshot.content_item_id, snapshot)
    return newest


def collect_summary(
    session: Session,
    account: Account,
    *,
    now: datetime | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> AccountSummary:
    """汇总某账号最近 ``window_days`` 天的表现。"""
    moment = now or utcnow()
    since = moment - timedelta(days=window_days)
    summary = AccountSummary(
        account_id=account.id, platform=account.platform, window_days=window_days
    )

    rows = session.scalars(
        select(ContentItem)
        .where(ContentItem.account_id == account.id, ContentItem.updated_at >= since)
        .order_by(ContentItem.updated_at.desc())
    ).all()
    records_by_item: dict[str, PublishRecord] = {}
    for record in session.scalars(
        select(PublishRecord)
        .join(ContentItem, ContentItem.id == PublishRecord.content_item_id)
        .where(
            ContentItem.account_id == account.id,
            ContentItem.updated_at >= since,
            PublishRecord.phase == PublishPhase.DONE.value,
        )
        .order_by(PublishRecord.content_item_id, PublishRecord.updated_at.desc())
    ):
        records_by_item.setdefault(record.content_item_id, record)
    snapshots_by_item = _latest_usable_snapshots(
        session,
        account_id=account.id,
        since=since,
    )
    topics_by_id = {
        topic.id: topic
        for topic in session.scalars(
            select(Topic)
            .join(ContentItem, ContentItem.topic_id == Topic.id)
            .where(ContentItem.account_id == account.id, ContentItem.updated_at >= since)
        )
    }

    for item in rows:
        if item.status == ContentStatus.DEAD_LETTER.value:
            summary.dead_letter += 1
            continue
        if item.status in (
            ContentStatus.PUBLISH_FAILED.value,
            ContentStatus.RETRYING.value,
        ):
            summary.failed += 1
            continue
        if item.status not in (ContentStatus.PUBLISHED.value, ContentStatus.MEASURED.value):
            continue

        record = records_by_item.get(item.id)
        if record is None:
            continue
        published_at = record.updated_at or record.created_at
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=UTC)
        if published_at < since:
            continue

        snapshot = snapshots_by_item.get(item.id)
        if snapshot is None:
            summary.unmeasured += 1
            continue

        extra = (item.bundle_json or {}).get("platform_extra") or {}
        selection = extra.get("selection") or {}
        topic_source = str(selection.get("topic_source") or "")
        if not topic_source and item.topic_id:
            topic = topics_by_id.get(item.topic_id)
            topic_source = topic.source if topic is not None else ""

        summary.posts.append(
            PostStat(
                item_id=item.id,
                title=item.title,
                published_at=published_at,
                url=record.url,
                topic_source=topic_source or "未知",
                metrics=dict(snapshot.metrics_json or {}),
                tags=list((item.bundle_json or {}).get("tags") or []),
                angle=str(selection.get("angle") or ""),
            )
        )

    summary.posts.sort(key=lambda p: p.published_at)
    return summary


def render_stats_table(summary: AccountSummary, policy: AccountPolicy) -> str:
    """把 :class:`AccountSummary` 渲染成 Markdown 表（送进 prompt）。"""
    if not summary.posts:
        return "（窗口内没有已发布且已采到指标的内容）"
    header = "| 发布时刻 | 标题 | 选题来源 | 切入角度 | 阅读 | 点赞 | 评论 | 收藏 | 分享 |"
    sep = "|---|---|---|---|---|---|---|---|---|"
    rows = [header, sep]
    for post in summary.posts:

        def cell(name: str, p: PostStat = post) -> str:
            value = p.metric(name)
            return "—" if value is None else str(value)

        rows.append(
            "| {slot} | {title} | {source} | {angle} | {views} | {likes} | "
            "{comments} | {collects} | {shares} |".format(
                slot=post.local_slot(policy),
                title=post.title.replace("|", "/")[:48] or "（无标题）",
                source=post.topic_source,
                angle=(post.angle.replace("|", "/")[:28] or "—"),
                views=cell("views"),
                likes=cell("likes"),
                comments=cell("comments"),
                collects=cell("collects"),
                shares=cell("shares"),
            )
        )
    return "\n".join(rows)


def render_totals(summary: AccountSummary) -> str:
    totals = summary.totals()
    parts = [f"已发布 {summary.published} 条"]
    parts += [f"{name} 合计 {v}" for name, v in totals.items() if v is not None]
    missing = [name for name, v in totals.items() if v is None]
    if missing:
        parts.append(f"无数据字段：{'、'.join(missing)}（是缺失，不是 0）")
    return "；".join(parts)


def render_failures(summary: AccountSummary) -> str:
    bits: list[str] = []
    if summary.failed:
        bits.append(f"发布失败 / 重试中 {summary.failed} 条")
    if summary.dead_letter:
        bits.append(f"死信 {summary.dead_letter} 条")
    if summary.unmeasured:
        bits.append(f"已发布但还没采到指标 {summary.unmeasured} 条")
    return "；".join(bits) if bits else "无"


# ------------------------------------------------------------------------ 主流程


def build_prompt(summary: AccountSummary, policy: AccountPolicy, *, now: datetime) -> str:
    persona = policy.persona or prompts.load_persona(
        summary.account_id, default="（未提供人设，按通用资讯号处理）"
    )
    return prompts.load(
        "metrics/insights",
        today=now.date().isoformat(),
        account_id=summary.account_id,
        platform=summary.platform,
        persona=persona,
        window_days=summary.window_days,
        windows=policy.window_text(),
        timezone=policy.timezone,
        stats_table=render_stats_table(summary, policy),
        totals=render_totals(summary),
        failures=render_failures(summary),
    )


def generate_for_account(
    session: Session,
    account: Account,
    llm: SupportsLLM,
    *,
    now: datetime | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    keep: int | None = None,
    min_posts: int | None = None,
) -> InsightsReport | None:
    """给单个账号跑一次复盘并写盘。样本不足返回 ``None``。"""
    from core.config import get_settings

    settings = get_settings()
    moment = now or utcnow()
    keep = keep if keep is not None else settings.insights_keep
    threshold = min_posts if min_posts is not None else settings.insights_min_posts

    summary = collect_summary(session, account, now=moment, window_days=window_days)
    if summary.published < threshold:
        logger.info(
            "账号 %s 近 %d 天只有 %d 条已发且有指标的内容（阈值 %d），跳过复盘",
            account.id,
            window_days,
            summary.published,
            threshold,
        )
        return None

    policy = policy_of(account)
    prompt = build_prompt(summary, policy, now=moment)
    report = llm.parse(
        prompt,
        InsightsReport,
        max_tokens=budget_for("metrics.insights"),
        purpose="metrics.insights",
    ).parsed

    path = prompts.append_insight(
        account.id,
        report.to_markdown(account_id=account.id, window_days=window_days, at=moment),
        keep=keep,
    )
    account.extra = {**(account.extra or {}), "insights_updated_at": moment.isoformat()}
    logger.info(
        "复盘完成 account=%s 样本=%d 置信度=%s → %s",
        account.id,
        summary.published,
        report.confidence,
        path,
    )
    return report


def _due(account: Account, now: datetime, interval: timedelta) -> bool:
    raw = (account.extra or {}).get("insights_updated_at")
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(str(raw))
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return now - last >= interval


def run(
    session: Session,
    *,
    account_ids: list[str] | None = None,
    llm: SupportsLLM | None = None,
    now: datetime | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    force: bool = False,
) -> dict[str, int]:
    """给所有（或指定）账号跑复盘。返回统计，不抛异常到调度器。

    统计键：``scanned`` / ``written`` / ``skipped_sample`` / ``skipped_not_due``
    / ``skipped_no_key`` / ``failed``。
    """
    from core.config import get_settings

    settings = get_settings()
    moment = now or utcnow()
    stats = {
        "scanned": 0,
        "written": 0,
        "skipped_sample": 0,
        "skipped_not_due": 0,
        "skipped_no_key": 0,
        "failed": 0,
    }
    if not settings.insights_enabled:
        logger.info("INSIGHTS_ENABLED=false，跳过复盘")
        return stats

    stmt = select(Account).order_by(Account.id)
    if account_ids:
        stmt = stmt.where(Account.id.in_(account_ids))
    accounts = list(session.scalars(stmt))
    if not accounts:
        return stats

    client = llm
    if client is None:
        from generation.llm import build_llm, llm_credentials_ready

        if not llm_credentials_ready(settings):
            # 刻意不回落 ScriptedLLM：复盘是长期资产，宁可空着
            logger.info("LLM 凭据未配置（后端 %s），跳过复盘", settings.sw_llm_backend)
            stats["skipped_no_key"] = len(accounts)
            return stats

        client = build_llm(budget=BudgetGuard(session, notifier=None))

    interval = timedelta(hours=settings.insights_interval_hours)
    for account in accounts:
        stats["scanned"] += 1
        if not force and not _due(account, moment, interval):
            stats["skipped_not_due"] += 1
            continue
        try:
            report = generate_for_account(
                session, account, client, now=moment, window_days=window_days
            )
        except BudgetExhausted as exc:
            logger.warning("复盘预算耗尽，本轮停止：%s", exc)
            stats["failed"] += 1
            break
        except LLMError as exc:
            logger.warning("复盘失败 account=%s: %s", account.id, exc)
            account.extra = {
                **(account.extra or {}),
                "insights_error": f"{type(exc).__name__}: {exc}"[:300],
                # 失败也推进时间戳，否则每轮 tick 都会对同一个坏账号重试烧 token
                "insights_updated_at": moment.isoformat(),
            }
            stats["failed"] += 1
            continue
        if report is None:
            stats["skipped_sample"] += 1
            continue
        stats["written"] += 1
    session.flush()
    return stats


def last_run_at(account: Account) -> datetime | None:
    raw = (account.extra or {}).get("insights_updated_at")
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "METRIC_FIELDS",
    "AccountSummary",
    "InsightsReport",
    "PostStat",
    "build_prompt",
    "collect_summary",
    "generate_for_account",
    "last_run_at",
    "render_failures",
    "render_stats_table",
    "render_totals",
    "run",
]
