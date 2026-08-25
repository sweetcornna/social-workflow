"""指标采集器：给已发布内容按 24h / 7d 打快照，只追加。

成功快照的窗口判定不额外建表也不污染 ``metrics_json``——完全由
``MetricSnapshot.snapshot_at`` 与发布时刻推导：

- 窗口 ``24h`` 已覆盖 ⟺ 存在 ``snapshot_at >= published_at + 24h`` 的可用快照
- 窗口 ``7d``  已覆盖 ⟺ 存在 ``snapshot_at >= published_at + 7d`` 的可用快照

一次 tick 只打**一张**快照：若两个窗口同时到期（例如中间停机了几天），
这张快照同时满足两者，不需要补打两次。

``respect_windows=False``（默认）保留 P0 的行为：每次调用都尝试给所有已发布内容采样，
成功时追加一张快照，便于本地联调与手动触发；生产调度由 ``core.scheduler.create_scheduler``
以 ``respect_windows=True`` 注册。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from core.db import get_session_factory
from core.models import (
    Account,
    ContentItem,
    MetricCollectionAttempt,
    MetricSnapshot,
    PublishRecord,
    new_id,
    utcnow,
)
from core.state_machine import ContentStatus, PublishPhase, apply_health
from metrics.availability import (
    MetricsPayloadKind,
    classify_metrics_payload,
    normalize_metrics_payload,
)
from publishers.base import (
    Publisher,
    is_placeholder_post_id,
)
from publishers.registry import get_publisher

logger = logging.getLogger("social_workflow.metrics")

_DATABASE_OPERATIONS = frozenset({"candidate_query", "claim", "result"})
_INVALID_DATABASE_CONTEXT_VALUE = "<invalid>"

# 采样节奏：发布后 24h 与 7d 各一次（计划 2.1 / metrics/README.md）
WINDOWS: dict[str, timedelta] = {"24h": timedelta(hours=24), "7d": timedelta(days=7)}
# 从早到晚，判定时取"最晚到期的那个"作为本次快照的窗口标签
WINDOW_ORDER: tuple[str, ...] = ("24h", "7d")

MEASURABLE_STATUSES = (ContentStatus.PUBLISHED.value, ContentStatus.MEASURED.value)
DEFAULT_MAX_ITEMS = 50

# 生产 tick 每 6 小时运行（见 core.scheduler.create_scheduler）。尝试状态把该桶写入
# 数据库，因此重复触发不会重复打同一内容，而是继续服务同桶尚未尝试的候选。
FAIRNESS_BUCKET = timedelta(hours=6)


@dataclass
class MetricCandidate:
    """已经通过本地校验、可尝试原子认领的脱管候选。"""

    item_id: str
    title: str
    record_id: str
    platform_post_id: str
    account_id: str
    platform: str
    window: str | None
    due_at: datetime | None
    last_attempt_at: datetime | None
    last_attempt_bucket: int | None


class AttemptOutcome(StrEnum):
    """``MetricCollectionAttempt.last_outcome`` 的稳定持久化值。"""

    CLAIMED = "claimed"
    SUCCESS = "success"
    UNAVAILABLE = "unavailable"
    MALFORMED = "malformed"
    ERROR = "error"


class _PublisherCallFailed(Exception):
    """丢弃发布器异常正文，只把稳定阶段名带回采集循环。"""

    def __init__(self, stage: str) -> None:
        self.stage = stage
        super().__init__(stage)


class MetricDatabaseError(RuntimeError):
    """P22 数据库边界的稳定脱敏错误；不得附带 SQL、参数或 payload。"""

    def __init__(
        self,
        message: str,
        *,
        operation: str,
        candidate: MetricCandidate | None = None,
    ) -> None:
        if type(operation) is not str or operation not in _DATABASE_OPERATIONS:
            raise ValueError("unsupported metric database operation")
        self._context = _safe_database_context(operation, candidate)
        super().__init__(message)

    @property
    def operation(self) -> str:
        """受控的数据库边界名称。"""

        return self._context["operation"] or ""

    @property
    def context(self) -> Mapping[str, str | None]:
        """只读、白名单化的运维上下文。"""

        return self._context


def _safe_database_context(
    operation: str, candidate: MetricCandidate | None
) -> Mapping[str, str | None]:
    """固化数据库错误中允许出现的候选字段，绝不保留原对象。"""

    context: dict[str, str | None] = {"operation": operation}
    if candidate is not None:
        context.update(
            item_id=_safe_database_context_string(candidate.item_id),
            platform=_safe_database_context_string(candidate.platform),
            account_id=_safe_database_context_string(candidate.account_id),
            window=_safe_database_context_window(candidate.window),
        )
    return MappingProxyType(context)


def _safe_database_context_string(value: object) -> str:
    """只接受 exact builtin str，避免执行不可信对象协议。"""

    return value if type(value) is str else _INVALID_DATABASE_CONTEXT_VALUE


def _safe_database_context_window(value: object) -> str | None:
    """window 的安全版本允许 exact builtin str 或 None。"""

    if type(value) is str or value is None:
        return value
    return _INVALID_DATABASE_CONTEXT_VALUE


def _log_metric_database_error(error: MetricDatabaseError) -> None:
    """仅记录已固化的白名单 context，不能接触原始 candidate。"""

    context = error.context
    if error.operation == "candidate_query":
        logger.error("指标数据库操作失败 operation=candidate_query category=database")
        return
    logger.error(
        "指标数据库操作失败 item_id=%s platform=%s account_id=%s window=%s "
        "operation=%s category=database",
        context["item_id"],
        context["platform"],
        context["account_id"],
        context["window"],
        error.operation,
    )


class MetricResultInvariantError(RuntimeError):
    """结果事务内部 CAS 未满足时使用的稳定错误。"""


def done_record(session: Session, item: ContentItem) -> PublishRecord | None:
    """取该内容已完成的发布记录（两阶段记录里 phase=done 的那条）。"""
    return session.scalars(
        select(PublishRecord)
        .where(
            PublishRecord.content_item_id == item.id,
            PublishRecord.phase == PublishPhase.DONE.value,
        )
        .limit(1)
    ).one_or_none()


def published_at_of(record: PublishRecord) -> datetime:
    """发布时刻：phase 置 done 时写的 ``updated_at``，兜底用 ``created_at``。"""
    moment = record.updated_at or record.created_at
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def taken_snapshot_times(session: Session, item: ContentItem) -> list[datetime]:
    rows = session.scalars(
        select(MetricSnapshot).where(MetricSnapshot.content_item_id == item.id)
    ).all()
    return [
        snapshot.snapshot_at
        if snapshot.snapshot_at.tzinfo
        else snapshot.snapshot_at.replace(tzinfo=UTC)
        for snapshot in rows
        if classify_metrics_payload(snapshot.metrics_json) is MetricsPayloadKind.USABLE
    ]


def due_window(published_at: datetime, taken: list[datetime], now: datetime) -> str | None:
    """返回本次应打的窗口标签；都不到期或都已覆盖则 ``None``。"""
    label: str | None = None
    for name in WINDOW_ORDER:
        deadline = published_at + WINDOWS[name]
        if now < deadline:
            continue
        if any(t >= deadline for t in taken):
            continue
        label = name
    return label


def earliest_due_at(
    published_at: datetime, taken: list[datetime], now: datetime
) -> datetime | None:
    """返回最早尚未覆盖且已经到期的窗口截止时刻。

    选择顺序按它排队；真正写入时仍由 :func:`due_window` 选择最晚的已到期
    标签，所以一次成功快照在两个窗口都欠账时仍会同时覆盖两者。
    """

    due = [
        published_at + WINDOWS[name]
        for name in WINDOW_ORDER
        if now >= published_at + WINDOWS[name]
        and not any(taken_at >= published_at + WINDOWS[name] for taken_at in taken)
    ]
    return min(due, default=None)


def _fetch_metrics(
    publisher: Publisher, title: str, post_id: str
) -> tuple[MetricsPayloadKind, dict[str, Any] | None]:
    """调 ``fetch_metrics``；post_id 映射不上时用标题兜底（**可选能力**）。

    两个平台都会走到兜底：

    - 公众号：回写的是草稿 ``media_id``，datacube 用的是 ``msgid``，没有官方映射；
    - 小红书：``POST /api/v1/publish`` 的响应里根本没有笔记 id，发布当时若没能从主页
      对账出来，记的是占位 id（``xhs-unresolved-*`` / ``xhs-scheduled-*``）。

    兜底命中时返回的 dict 里可能带真实 ``platform_post_id``，由
    :func:`_repair_post_id` 回填到发布记录，下次就能走正常路径。
    """
    try:
        main_raw = publisher.fetch_metrics(post_id)
    except Exception:
        raise _PublisherCallFailed("main_fetch") from None
    main_kind, main_metrics = normalize_metrics_payload(main_raw)
    if main_kind is not MetricsPayloadKind.EXPLICITLY_UNAVAILABLE or not title:
        return main_kind, main_metrics

    try:
        fallback_fetch = publisher.fetch_metrics_for_title  # type: ignore[attr-defined]
    except AttributeError:
        return main_kind, main_metrics
    except Exception:
        raise _PublisherCallFailed("fallback_fetch") from None
    try:
        fallback_raw = fallback_fetch(title)
    except Exception:
        raise _PublisherCallFailed("fallback_fetch") from None
    fallback_kind, fallback_metrics = normalize_metrics_payload(fallback_raw)
    if fallback_kind is MetricsPayloadKind.EXPLICITLY_UNAVAILABLE:
        return main_kind, main_metrics
    return fallback_kind, fallback_metrics


def _repair_post_id(record: PublishRecord, metrics: dict[str, Any]) -> str | None:
    """兜底链路解析出真实 post_id 时回填发布记录。返回回填后的 id（没变则 None）。

    只在原值是**占位 id** 时回填：真实 id 是幂等链路的凭据，不该被指标采集改写。
    """
    resolved = metrics.get("platform_post_id")
    if not resolved or not isinstance(resolved, str):
        return None
    current = record.platform_post_id or ""
    if resolved == current or not is_placeholder_post_id(current):
        return None
    published_at = record.updated_at  # 见下方注释，必须原样写回
    record.platform_post_id = resolved
    url = metrics.get("url")
    if isinstance(url, str) and url and not record.url:
        record.url = url
    # PublishRecord.updated_at 被 published_at_of() 当作"发布时刻"用来算 24h/7d 窗口，
    # 而这列带 onupdate=utcnow。原样赋同一个值不会产生净变更，flush 时反而会让 onupdate
    # 生效把时间推后；必须 flag_modified 强制把原值写进 UPDATE 的 SET 里。
    record.updated_at = published_at
    flag_modified(record, "updated_at")
    return resolved


def _collect_candidates(
    session: Session,
    *,
    moment: datetime,
    respect_windows: bool,
    stats: dict[str, int],
) -> list[MetricCandidate]:
    """先完成所有本地判定，再返回可尝试的候选。

    不在 SQL 上先 ``LIMIT``，否则最近更新但还没到期或长期不可用的内容会饿死
    已到期项。这里没有发布器调用。
    """

    latest_record_id = (
        select(PublishRecord.id)
        .where(
            PublishRecord.content_item_id == ContentItem.id,
            PublishRecord.phase == PublishPhase.DONE.value,
        )
        .order_by(PublishRecord.updated_at.desc(), PublishRecord.id.desc())
        .limit(1)
        .correlate(ContentItem)
        .scalar_subquery()
    )
    rows = session.execute(
        select(ContentItem, Account, PublishRecord, MetricCollectionAttempt)
        .outerjoin(Account, Account.id == ContentItem.account_id)
        .outerjoin(PublishRecord, PublishRecord.id == latest_record_id)
        .outerjoin(
            MetricCollectionAttempt,
            MetricCollectionAttempt.content_item_id == ContentItem.id,
        )
        .where(ContentItem.status.in_(MEASURABLE_STATUSES))
        .order_by(ContentItem.id)
    ).all()
    stats["scanned"] += len(rows)
    if not rows:
        return []

    # 历史快照可远多于内容。流式扫完后每条内容只保留一个最新可用时刻，内存上界 O(N item)，
    # 不随历史快照数增长；查询也不展开 Python ID，避开 SQLite 变量上限。
    latest_taken_by_item: dict[str, datetime] = {}
    snapshot_rows = session.execute(
        select(
            MetricSnapshot.content_item_id,
            MetricSnapshot.snapshot_at,
            MetricSnapshot.metrics_json,
        )
        .join(ContentItem, ContentItem.id == MetricSnapshot.content_item_id)
        .where(ContentItem.status.in_(MEASURABLE_STATUSES))
        .execution_options(yield_per=500)
    )
    for item_id, snapshot_at, metrics_json in snapshot_rows:
        kind = classify_metrics_payload(metrics_json)
        if kind is MetricsPayloadKind.USABLE:
            aware_at = snapshot_at if snapshot_at.tzinfo else snapshot_at.replace(tzinfo=UTC)
            previous = latest_taken_by_item.get(item_id)
            if previous is None or aware_at > previous:
                latest_taken_by_item[item_id] = aware_at
        elif kind is MetricsPayloadKind.EXPLICITLY_UNAVAILABLE:
            stats["history_unavailable"] += 1
        else:
            stats["history_malformed"] += 1

    candidates: list[MetricCandidate] = []
    for item, account, record, attempt in rows:
        if record is None or not record.platform_post_id:
            stats["skipped"] += 1
            continue
        if account is None:
            stats["skipped"] += 1
            continue

        window: str | None = None
        due_at: datetime | None = None
        if respect_windows:
            published_at = published_at_of(record)
            latest_taken = latest_taken_by_item.get(item.id)
            taken = [latest_taken] if latest_taken is not None else []
            window = due_window(published_at, taken, moment)
            if window is None:
                stats["not_due"] += 1
                continue
            due_at = earliest_due_at(published_at, taken, moment)
            assert due_at is not None  # due_window 已确认至少有一个窗口到期未覆盖
        candidates.append(
            MetricCandidate(
                item_id=item.id,
                title=item.title,
                record_id=record.id,
                platform_post_id=record.platform_post_id,
                account_id=account.id,
                platform=account.platform,
                window=window,
                due_at=due_at,
                last_attempt_at=attempt.last_attempt_at if attempt is not None else None,
                last_attempt_bucket=(attempt.last_attempt_bucket if attempt is not None else None),
            )
        )
    return candidates


def _bucket_number(moment: datetime) -> int:
    """UTC 六小时桶号；同一桶内重跑必须命中同一批候选。"""

    utc_moment = moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
    return int(utc_moment.timestamp() // FAIRNESS_BUCKET.total_seconds())


def _ordered_candidates(
    candidates: list[MetricCandidate], *, moment: datetime
) -> list[MetricCandidate]:
    """按持久化的最近尝试时间选择最久未服务的候选。

    同一 UTC 桶里已经开始过的内容不再候选；重复调用会按同一排序处理该桶其余内容。
    下一桶重新放行所有未覆盖窗口的内容，持续失败也会因 ``last_attempt_at`` 后移而让出
    前排。这个顺序与当前候选数量无关。
    """

    if not candidates:
        return []
    bucket = _bucket_number(moment)
    eligible = [
        candidate
        for candidate in candidates
        if candidate.last_attempt_bucket is None or candidate.last_attempt_bucket < bucket
    ]
    ordered = sorted(
        eligible,
        key=lambda candidate: (
            (
                candidate.last_attempt_at
                if candidate.last_attempt_at is not None
                else candidate.due_at
            )
            or datetime.min.replace(tzinfo=UTC),
            candidate.due_at or datetime.min.replace(tzinfo=UTC),
            candidate.item_id,
        ),
    )
    return ordered


def _claim_attempt(candidate: MetricCandidate, *, moment: datetime) -> bool:
    """以独立短事务原子认领 ``(content_item_id, UTC 6h bucket)``。

    SQLite 单语句 UPSERT 的冲突分支只允许 ``stored_bucket < target_bucket``；同桶、
    更旧 target 以及任意乱序调用都得到 rowcount=0。SQLite 单写者串行化该语句，
    因此无需先读后写或 IntegrityError 重试，也不可能把 bucket 倒退。
    """

    bucket = _bucket_number(moment)
    claim_session = get_session_factory()()
    try:
        inserted = sqlite_insert(MetricCollectionAttempt).values(
            content_item_id=candidate.item_id,
            last_attempt_at=moment,
            last_attempt_bucket=bucket,
            last_outcome=AttemptOutcome.CLAIMED.value,
            updated_at=utcnow(),
        )
        claimed = claim_session.execute(
            inserted.on_conflict_do_update(
                index_elements=[MetricCollectionAttempt.content_item_id],
                set_={
                    "last_attempt_at": inserted.excluded.last_attempt_at,
                    "last_attempt_bucket": inserted.excluded.last_attempt_bucket,
                    "last_outcome": inserted.excluded.last_outcome,
                    "updated_at": inserted.excluded.updated_at,
                },
                where=(
                    MetricCollectionAttempt.last_attempt_bucket
                    < inserted.excluded.last_attempt_bucket
                ),
            )
        )
        claim_session.commit()
        return claimed.rowcount == 1
    except SQLAlchemyError:
        claim_session.rollback()
        error = MetricDatabaseError(
            "metric claim database operation failed",
            operation="claim",
            candidate=candidate,
        )
        _log_metric_database_error(error)
        raise error from None
    except Exception:
        claim_session.rollback()
        raise
    finally:
        claim_session.close()


def _persist_result(
    candidate: MetricCandidate,
    *,
    moment: datetime,
    outcome: AttemptOutcome,
    metrics: dict[str, Any] | None = None,
    health: object | None = None,
) -> bool:
    """把单条结果写入独立短事务，返回本事务是否赢得结果 CAS。

    第一条条件写同时验证 item、bucket 和 ``last_outcome=claimed`` 并取得 SQLite
    写锁。未命中表示迟到/重复结果，必须在读取候选和写业务表之前退出。最终 outcome
    仍以同一条件 CAS 并断言恰好一行；任何异常都会回滚整项结果。
    """

    if not isinstance(outcome, AttemptOutcome):
        raise ValueError("outcome must be an AttemptOutcome")
    bucket = _bucket_number(moment)
    session = get_session_factory()()
    try:
        locked_claim = session.execute(
            update(MetricCollectionAttempt)
            .where(
                MetricCollectionAttempt.content_item_id == candidate.item_id,
                MetricCollectionAttempt.last_attempt_bucket == bucket,
                MetricCollectionAttempt.last_outcome == AttemptOutcome.CLAIMED.value,
            )
            .values(last_outcome=AttemptOutcome.CLAIMED.value, updated_at=utcnow())
        )
        if locked_claim.rowcount != 1:
            session.rollback()
            return False

        item = session.get(ContentItem, candidate.item_id)
        record = session.get(PublishRecord, candidate.record_id)
        account = session.get(Account, candidate.account_id)
        if item is None or record is None or account is None:
            raise RuntimeError("metric collection candidate disappeared after claim")

        # 只有 publisher.health() 本身属于可降级的外部调用。状态机及其 DB 写入必须在
        # catch 外执行，失败时本事务回滚并明确传播，attempt 保持 claimed。
        if health is not None:
            apply_health(session, account, health.status, detail=health.detail)

        if metrics is not None:
            if type(metrics) is not dict:
                raise TypeError("metrics must be a normalized builtin dict")
            if _repair_post_id(record, metrics):
                logger.info(
                    "指标 post id 回填 item_id=%s platform=%s account_id=%s window=%s",
                    candidate.item_id,
                    candidate.platform,
                    candidate.account_id,
                    candidate.window,
                )
            session.add(
                MetricSnapshot(
                    id=new_id("mtr"),
                    content_item_id=item.id,
                    platform_post_id=record.platform_post_id,
                    snapshot_at=moment,
                    metrics_json=metrics,
                )
            )
            if item.status == ContentStatus.PUBLISHED.value:
                item.status = ContentStatus.MEASURED.value
                item.updated_at = utcnow()

        # 若更晚的桶已经认领同一内容，旧结果不能覆盖新桶的 outcome。
        finalized = session.execute(
            update(MetricCollectionAttempt)
            .where(
                MetricCollectionAttempt.content_item_id == candidate.item_id,
                MetricCollectionAttempt.last_attempt_bucket == bucket,
                MetricCollectionAttempt.last_outcome == AttemptOutcome.CLAIMED.value,
            )
            .values(last_outcome=outcome.value, updated_at=utcnow())
        )
        if finalized.rowcount != 1:
            raise MetricResultInvariantError("metric result finalization conflict")
        session.commit()
        return True
    except SQLAlchemyError:
        session.rollback()
        error = MetricDatabaseError(
            "metric result database operation failed",
            operation="result",
            candidate=candidate,
        )
        _log_metric_database_error(error)
        raise error from None
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _normalized_max_items(max_items: int | None) -> int:
    """``None`` 取默认上限，0 为合法空批，负数和 bool 明确拒绝。"""

    if max_items is None:
        return DEFAULT_MAX_ITEMS
    if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items < 0:
        raise ValueError("max_items 必须是非负整数或 None")
    return max_items


def collect_all(
    *,
    max_items: int | None = DEFAULT_MAX_ITEMS,
    now: datetime | None = None,
    respect_windows: bool = False,
) -> dict[str, int]:
    """扫描已发布内容并追加可用指标快照。

    ``max_items`` 是一次调用最多处理的内容候选数（``None`` = 默认 50，0 = 空批）。
    统计键还区分 publisher 的 ``unavailable`` / ``malformed`` / ``errors`` 以及历史
    坏快照的 ``history_unavailable`` / ``history_malformed``。所有新尝试记录、快照和
    claim 在 publisher 构造前以独立短事务提交；网络调用不持有数据库事务；每条结果再
    独立提交。因此一项数据库失败会传播并停止本 tick，但不会回滚此前已提交的项目。
    """
    cap = _normalized_max_items(max_items)
    moment = now or utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    stats = {
        "scanned": 0,
        "attempted": 0,
        "snapshots": 0,
        "unavailable": 0,
        "malformed": 0,
        "errors": 0,
        "health_errors": 0,
        "history_unavailable": 0,
        "history_malformed": 0,
        "skipped": 0,
        "not_due": 0,
    }
    if cap == 0:
        return stats
    read_session = get_session_factory()()
    try:
        candidates = _collect_candidates(
            read_session, moment=moment, respect_windows=respect_windows, stats=stats
        )
    except SQLAlchemyError:
        read_session.rollback()
        error = MetricDatabaseError(
            "metric candidate database operation failed",
            operation="candidate_query",
        )
        _log_metric_database_error(error)
        raise error from None
    finally:
        read_session.close()

    for candidate in _ordered_candidates(candidates, moment=moment):
        if stats["attempted"] >= cap:
            break
        if not _claim_attempt(candidate, moment=moment):
            continue
        # claim 已提交；从这里开始才允许构造 publisher / 发网络请求。
        stats["attempted"] += 1
        try:
            publisher = get_publisher(candidate.platform, candidate.account_id)
        except Exception:
            logger.warning(
                "指标发布器不可用 item_id=%s platform=%s account_id=%s window=%s category=init",
                candidate.item_id,
                candidate.platform,
                candidate.account_id,
                candidate.window,
            )
            if _persist_result(candidate, moment=moment, outcome=AttemptOutcome.ERROR):
                stats["errors"] += 1
            continue

        fetch_failed = False
        kind = MetricsPayloadKind.MALFORMED
        normalized_metrics: dict[str, Any] | None = None
        try:
            kind, normalized_metrics = _fetch_metrics(
                publisher,
                candidate.title,
                candidate.platform_post_id,
            )
        except _PublisherCallFailed as exc:
            logger.warning(
                "指标采集失败 item_id=%s platform=%s account_id=%s window=%s category=%s",
                candidate.item_id,
                candidate.platform,
                candidate.account_id,
                candidate.window,
                exc.stage,
            )
            fetch_failed = True

        health = None
        try:
            health = publisher.health()
        except Exception:
            logger.warning(
                "指标健康检查失败 item_id=%s platform=%s account_id=%s "
                "window=%s category=health_exception",
                candidate.item_id,
                candidate.platform,
                candidate.account_id,
                candidate.window,
            )
            stats["health_errors"] += 1

        if fetch_failed:
            persisted = _persist_result(
                candidate,
                moment=moment,
                outcome=AttemptOutcome.ERROR,
                health=health,
            )
            if persisted:
                stats["errors"] += 1
            continue

        if kind is MetricsPayloadKind.EXPLICITLY_UNAVAILABLE:
            logger.warning(
                "指标不可用 item_id=%s platform=%s account_id=%s window=%s",
                candidate.item_id,
                candidate.platform,
                candidate.account_id,
                candidate.window,
            )
            persisted = _persist_result(
                candidate,
                moment=moment,
                outcome=AttemptOutcome.UNAVAILABLE,
                health=health,
            )
            if persisted:
                stats["unavailable"] += 1
            continue
        if kind is MetricsPayloadKind.MALFORMED:
            logger.warning(
                "指标格式错误 item_id=%s platform=%s account_id=%s window=%s category=malformed",
                candidate.item_id,
                candidate.platform,
                candidate.account_id,
                candidate.window,
            )
            persisted = _persist_result(
                candidate,
                moment=moment,
                outcome=AttemptOutcome.MALFORMED,
                health=health,
            )
            if persisted:
                stats["malformed"] += 1
            continue
        assert normalized_metrics is not None
        persisted = _persist_result(
            candidate,
            moment=moment,
            outcome=AttemptOutcome.SUCCESS,
            metrics=normalized_metrics,
            health=health,
        )
        if persisted:
            stats["snapshots"] += 1
        if persisted and candidate.window:
            logger.info(
                "指标快照 item_id=%s platform=%s account_id=%s window=%s",
                candidate.item_id,
                candidate.platform,
                candidate.account_id,
                candidate.window,
            )
    return stats


__all__ = [
    "WINDOWS",
    "WINDOW_ORDER",
    "collect_all",
    "done_record",
    "due_window",
    "earliest_due_at",
    "published_at_of",
    "taken_snapshot_times",
]

# 说明：xhs 与 douyin 走的都是这条通用链路（fetch_metrics → 不可用则 fetch_metrics_for_title
# → _repair_post_id 回填内容 id），不需要平台分支。占位 id 的识别用
# publishers.base.is_placeholder_post_id（跨平台，认 "-unresolved-"/"-scheduled-" 标记）。
# 各平台字段口径见 metrics/README.md。
