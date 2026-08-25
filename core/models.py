"""数据模型（SQLAlchemy 2.x, SQLite）。

设计要点：
- 时间统一存 UTC naive，读出来自动补 tzinfo（见 :class:`UTCDateTime`）。
- ``PublishRecord.idem_key`` UNIQUE，是幂等的唯一真相来源。
- ``MetricSnapshot`` 只追加，不更新不删除。
- 任何凭据（AppSecret / Cookie / Token）都不落库，只在环境变量里。
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from itertools import count
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

logger = logging.getLogger("social_workflow.models")

# 与 core/review_actions.py::TRUTHY 同一套真值判定，避免同一个环境变量在不同
# 模块里被解析出两种结果。这里不 import core.config：core/models.py 是底层
# 模块，被 core.config 间接依赖，import 回去会成环。
_TRUTHY = frozenset({"1", "true", "on", "yes"})


def _resolve_e2e_time_anchor() -> datetime | None:
    """模块导入时解析一次时间锚点，返回 ``None`` 表示未启用（正常生产路径）。

    互锁：``SW_E2E_TIME_ANCHOR`` 只有在 ``SW_USE_FAKE_PUBLISHERS`` 为真时才生效。
    冻结时钟真正的风险不是"截图里的时间文本没变"，而是调度器会拿一个错误的
    "现在"去判断该不该发布，把内容真的发到平台上；假发布器打开时这条风险路径
    被封死，所以只在这个前提下放行。若锚点已设但假发布器为假（=真发布模式），
    直接拒绝启动，不允许静默忽略——否则线上一旦不小心带了这个变量，整条链路
    （调度器 / 限频器 / 预算闸门 / 确认截止时间）会在无任何告警的情况下集体失准。
    """
    anchor_raw = os.environ.get("SW_E2E_TIME_ANCHOR")
    if not anchor_raw:
        return None

    fake_publishers_raw = os.environ.get("SW_USE_FAKE_PUBLISHERS", "")
    fake_publishers_on = fake_publishers_raw.strip().lower() in _TRUTHY
    if not fake_publishers_on:
        raise RuntimeError(
            "SW_E2E_TIME_ANCHOR 已设置，但 SW_USE_FAKE_PUBLISHERS 不为真："
            '真发布模式下不允许钉死系统时钟（调度器会拿错误的"现在"去决定要不要'
            "真的把内容发到平台上）。请去掉 SW_E2E_TIME_ANCHOR，或者如果你其实是要"
            "跑 e2e，把 SW_USE_FAKE_PUBLISHERS=true 一起设上。"
        )

    parsed = datetime.fromisoformat(anchor_raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("SW_E2E_TIME_ANCHOR 必须带时区，例如 2026-08-19T11:00:00Z")
    resolved = parsed.astimezone(UTC)
    logger.warning(
        "SW_E2E_TIME_ANCHOR 已启用：系统时钟钉死在 %s。"
        "这个配置只应出现在 e2e 环境，绝不能出现在生产环境。",
        resolved.isoformat(),
    )
    return resolved


# 只在模块导入时解析一次：utcnow() 是热路径（每次插库、每次限频检查都会调），
# 不能每次调用都重新读环境变量 + fromisoformat。
_E2E_TIME_ANCHOR: datetime | None = _resolve_e2e_time_anchor()
# 临时 e2e 库每次启动都会重建；固定序号让审核结果里的媒体路径也能逐次复现。
_E2E_ID_SEQUENCE = count(1) if _E2E_TIME_ANCHOR is not None else None


class UTCDateTime(TypeDecorator):
    """存 naive UTC，取回带 tzinfo=UTC，避免 SQLite 上 naive/aware 混用。"""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


def utcnow() -> datetime:
    """返回 UTC 当前时刻；e2e 下可能被 ``SW_E2E_TIME_ANCHOR`` 钉死（见互锁说明）。"""
    if _E2E_TIME_ANCHOR is not None:
        return _E2E_TIME_ANCHOR
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    if _E2E_ID_SEQUENCE is not None:
        return f"{prefix}_e2e_{next(_E2E_ID_SEQUENCE):012x}"
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Base(DeclarativeBase):
    pass


class Account(Base):
    """运营账号。状态机见 core.state_machine.AccountStatus。"""

    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    platform: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="ok", index=True)
    # 小红书 = 该账号专属 xiaohongshu-mcp 容器地址；抖音 = 宿主机上传器地址；公众号为空
    sidecar_endpoint: Mapped[str | None] = mapped_column(String(255), nullable=True)
    daily_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    # 抖音一号一 profile，长期保活
    profile_dir: Mapped[str | None] = mapped_column(String(255), nullable=True)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    items: Mapped[list[ContentItem]] = relationship(back_populates="account")


class Topic(Base):
    """选题池条目（P1 由 sourcing/ 写入）。"""

    __tablename__ = "topics"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, index=True
    )


class ContentItem(Base):
    """一条待发布内容。状态机见 core.state_machine.ContentStatus。"""

    __tablename__ = "content_items"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    topic_id: Mapped[str | None] = mapped_column(
        ForeignKey("topics.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="topic", index=True)
    # 账号进入 needs_relogin 时挂起排期项，这里记录挂起前的状态，恢复后放回
    prev_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    # 序列化后的 publishers.base.ContentBundle
    bundle_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)

    # -- 发布前人工确认闸门（P12）------------------------------------------
    # 刻意**不新增状态**：这本质上和"限频""发布时段窗口"一样，是 scheduled 之后、
    # 真发之前的一道闸门，而不是内容生命周期里的新阶段。加状态要动 P0 冻结的迁移表
    # 和一大票测试，收益却只是把三个字段换个地方放。
    #: 人点了「确认发布」的时刻。``None`` = 还没人点，人工确认闸门会拦住它
    #:（``tick_scheduled_publish`` 执行顺序里的第 4 道；代码里若干处沿用的"第五道"
    #: 说的是它是第 5 个被加进来的闸门，不是位次，见 docs/OPS.md 1.6）
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)
    #: 确认卡的位置 ``"<chat_id>:<message_id>:<text|photo>"``，点完回来改这条消息用
    confirm_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: 确认卡推送时刻。防重复推送，同时也是 TTL（``SW_CONFIRM_TTL_HOURS``）的起点
    confirm_pushed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    account: Mapped[Account] = relationship(back_populates="items")
    publish_records: Mapped[list[PublishRecord]] = relationship(
        back_populates="content_item", cascade="all, delete-orphan"
    )
    review_logs: Mapped[list[ReviewLog]] = relationship(
        back_populates="content_item", cascade="all, delete-orphan"
    )

    @property
    def platform(self) -> str:
        return str(self.bundle_json.get("platform", "")) if self.bundle_json else ""

    @property
    def title(self) -> str:
        return str(self.bundle_json.get("title", "")) if self.bundle_json else ""


class PublishRecord(Base):
    """两阶段发布记录：发起前写 in_flight，成功后补 platform_post_id/url。

    ``idem_key`` UNIQUE 是并发下的唯一防重复真相；冲突时走 ``publisher.reconcile`` 对账。
    """

    __tablename__ = "publish_records"
    __table_args__ = (UniqueConstraint("idem_key", name="uq_publish_records_idem_key"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    content_item_id: Mapped[str] = mapped_column(
        ForeignKey("content_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    idem_key: Mapped[str] = mapped_column(String(64), nullable=False)
    # in_flight | done | failed
    phase: Mapped[str] = mapped_column(String(16), nullable=False, default="in_flight", index=True)
    platform_post_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    content_item: Mapped[ContentItem] = relationship(back_populates="publish_records")


class MetricSnapshot(Base):
    """指标快照（发布后 24h / 7d）。**只追加**，永不更新。"""

    __tablename__ = "metric_snapshots"
    __table_args__ = (Index("ix_metric_snapshots_item_at", "content_item_id", "snapshot_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    content_item_id: Mapped[str] = mapped_column(
        ForeignKey("content_items.id", ondelete="CASCADE"), nullable=False
    )
    platform_post_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    snapshot_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class MetricCollectionAttempt(Base):
    """每条内容最近一次指标采集尝试（P22）。

    这不是指标快照：无论成功、明确不可用、格式错误或发布器异常，都只覆盖这行的
    最近尝试信息。它让候选排序不依赖当前 backlog 大小，并限制同一 UTC 六小时桶内
    每条内容只开始一次采集。

    迁移：新表由 ``core.db.init_db()`` 的 ``create_all`` 幂等创建；既有表不变，老库
    无需手工 DDL。
    """

    __tablename__ = "metric_collection_attempts"
    __table_args__ = (Index("ix_metric_attempts_at_item", "last_attempt_at", "content_item_id"),)

    content_item_id: Mapped[str] = mapped_column(
        ForeignKey("content_items.id", ondelete="CASCADE"), primary_key=True
    )
    last_attempt_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    #: ``floor(UTC epoch / 6h)``，防止同一生产 tick 桶重复打同一内容。
    last_attempt_bucket: Mapped[int] = mapped_column(Integer, nullable=False)
    #: claimed | success | unavailable | malformed | error
    last_outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


class ReviewLog(Base):
    """审核审计日志：谁/何时/做了什么/理由/前后内容。合规证据链，不可删改。

    ``action``：人工审核为 ``approve|reject|edit``；系统事件（actor='system'）另见
    ``core.state_machine.SystemAction``。
    """

    __tablename__ = "review_logs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    content_item_id: Mapped[str] = mapped_column(
        ForeignKey("content_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    before_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    after_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow, index=True)

    content_item: Mapped[ContentItem] = relationship(back_populates="review_logs")


class RenderJobState(StrEnum):
    """``RenderJob.state``。

    刻意**不复用** MPT 的数字状态码（-1/1/4）：那是上游的实现细节，换渲染后端
    （P4 可能接 CosyVoice / 自研合成）时不该跟着变。映射在
    ``generation.video_pipeline.map_task_state``。
    """

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    #: sidecar 重启导致任务表丢失（``GET /tasks/{id}`` → 404）。可重提交一次
    LOST = "lost"


#: 还需要继续轮询的状态，供 ``core.scheduler.tick_render_jobs`` 扫描
ACTIVE_RENDER_STATES: tuple[str, ...] = (RenderJobState.PENDING, RenderJobState.RUNNING)


class RenderJob(Base):
    """异步渲染任务（P3 新增表）。

    为什么要一张新表而不是塞进 ``CostLedger`` 或 ``ContentItem.bundle_json``：

    - MPT 的任务表在**它自己的进程内存**里，容器一重启就没了。``task_id`` 必须
      由我们持久化，否则重启后既拿不到成片也不知道该不该重提交。
    - 渲染是**跨进程的长任务**（几分钟到几十分钟），生成链的 HTTP 请求早就返回了，
      后续进度只能靠调度器轮询——需要一个可扫描的行，而不是 JSON 里的一个字段。
    - ``CostLedger`` 是只追加的账本，语义上不允许更新 ``progress``。

    迁移：SQLite 上由 ``core.db.init_db()`` 的 ``create_all`` 自动建表，
    既有表的字段一律没动，因此老库直接启动即可，不需要手工 DDL。
    """

    __tablename__ = "render_jobs"
    __table_args__ = (Index("ix_render_jobs_state_updated", "state", "updated_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: 目标内容项。**刻意不加外键**：渲染任务在 ContentItem 落库**之前**就产生了
    #: （先提交渲染、等几分钟、拿到成片才组装出完整 bundle 入库），加 FK 会让
    #: 同一事务里的插入顺序变成硬约束。``tick_render_jobs`` 已经能容忍查不到 item。
    content_item_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: 渲染后端。当前只有 ``mpt``
    provider: Mapped[str] = mapped_column(String(24), nullable=False, default="mpt")
    #: 后端侧任务 id。丢任务重提交后会被覆盖成新 id
    task_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default=RenderJobState.PENDING, index=True
    )
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 成片落盘路径列表（下载完成后写入）
    result_paths: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    #: 提交次数。丢任务只允许重提交一次，见 generation.video_pipeline
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 提交参数摘要 / failed_stage / 渲染耗时等观测字段（**不放任何凭据**）
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


class CostLedger(Base):
    """成本流水（按天）。kind = tokens | render_seconds。"""

    __tablename__ = "cost_ledger"
    __table_args__ = (Index("ix_cost_ledger_day_kind", "day", "kind"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # ISO 日期字符串 YYYY-MM-DD（UTC）
    day: Mapped[str] = mapped_column(String(10), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)


__all__ = [
    "ACTIVE_RENDER_STATES",
    "Account",
    "Base",
    "ContentItem",
    "CostLedger",
    "MetricCollectionAttempt",
    "MetricSnapshot",
    "PublishRecord",
    "RenderJob",
    "RenderJobState",
    "ReviewLog",
    "Topic",
    "UTCDateTime",
    "new_id",
    "utcnow",
]
