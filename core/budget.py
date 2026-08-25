"""成本闸门：每日 Claude token 上限 + 视频渲染时长上限。

超限抛 :class:`BudgetExhausted`，上层据此降级为"只出选题不出稿"并通知。

**流水写在自己的连接上，每笔当场 commit**（P16.2）。两条独立的理由：

1. **并发**。SQLite 的写锁是整库一把，事务里 flush 出第一条写就拿到 RESERVED、
   直到 commit 才放。``tick_generate`` 一轮里第 1 次 ``charge`` 之后还有第 2..N 次
   LLM（``llm_timeout_seconds=600``）与生图（180s×N），旧写法让这些网络调用全部
   跑在写锁里，整轮才 commit——同期任何别的写者等满 ``busy_timeout`` 照样
   ``database is locked``（见 ``core/db.py`` 的 :data:`~core.db.SQLITE_BUSY_TIMEOUT_MS`）。
2. **记账**。``CostLedger`` 是只追加的账本，和内容事务没有原子性需求；更重要的是
   外层回滚时**不该**把 charge 一起回滚——token 已经真的花出去了。流水一丢，下一轮
   就拿着一个偏小的 ``used`` 继续放行支出，闸门等于被悄悄放松。

读（``used`` / ``remaining``）仍然走调用方的 session，这不是遗漏，见
:meth:`BudgetGuard.used` 的说明。
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from core.models import CostLedger, new_id, utcnow
from core.notify import Notifier

logger = logging.getLogger("social_workflow.budget")


class CostKind(StrEnum):
    TOKENS = "tokens"
    RENDER_SECONDS = "render_seconds"
    #: 生图张数（P11）。刻意按**张**而不是按 token 计：生图走的是独立的 key 与
    #: 计价口径，混进 tokens 会让"写稿的预算"被配图悄悄吃掉；模型上报的 token
    #: 用量作为观测字段留在流水 meta 里
    IMAGES = "images"


class BudgetExhausted(Exception):
    """当日预算不足。"""

    def __init__(self, kind: str, requested: float, remaining: float, limit: float) -> None:
        super().__init__(
            f"当日 {kind} 预算不足：请求 {requested:g}，剩余 {remaining:g}，上限 {limit:g}"
        )
        self.kind = kind
        self.requested = requested
        self.remaining = remaining
        self.limit = limit


def today_key(now: datetime | None = None) -> str:
    """UTC 日期键 YYYY-MM-DD。

    **成本闸门刻意按 UTC 日重置，不要"顺手"改成账号本地日**（P11.3）：
    token / 渲染秒 / 生图张数是**一台 core 服务**的开销，账单也不认账号；
    同一台服务上可以同时跑 ``Asia/Shanghai`` 与 ``America/Los_Angeles`` 的号，
    按谁的本地日重置都说不通。

    与之相对，**限频**（``core.ratelimit``）按**账号本地日**——那是"这个号在它
    自己的作息里一天发了几条"，必须跟发布窗口的分桶对齐。三种"今天"的完整对照
    见 ``core/ratelimit.py`` 模块文档与 ``docs/OPS.md``。
    """
    moment = now or utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).date().isoformat()


class BudgetGuard:
    """按天累计的成本闸门。所有流水写入 ``CostLedger``（只追加）。"""

    def __init__(
        self,
        session: Session,
        *,
        token_budget: int | None = None,
        render_seconds_budget: int | None = None,
        image_budget: int | None = None,
        day: str | date | None = None,
        notifier: Notifier | None = None,
        labels: dict[str, Any] | None = None,
    ) -> None:
        from core.config import get_settings

        settings = get_settings()
        self.session = session
        self.limits: dict[str, float] = {
            CostKind.TOKENS.value: float(
                token_budget if token_budget is not None else settings.daily_token_budget
            ),
            CostKind.RENDER_SECONDS.value: float(
                render_seconds_budget
                if render_seconds_budget is not None
                else settings.daily_render_seconds_budget
            ),
            CostKind.IMAGES.value: float(
                image_budget if image_budget is not None else settings.daily_image_budget
            ),
        }
        if isinstance(day, date):
            self.day = day.isoformat()
        else:
            self.day = day or today_key()
        self.notifier = notifier
        #: 每笔流水都会带上的标签（如 ``{"account_id": ...}``），供 ``/stats`` 按账号归集成本。
        #: ``CostLedger`` 刻意不加列：账本是只追加的宽表，成本归属属于观测维度而非主键
        self.labels: dict[str, Any] = dict(labels or {})
        self._notified: set[str] = set()

    # -- 查询 --------------------------------------------------------------

    def limit_of(self, kind: str | CostKind) -> float:
        key = str(kind)
        if key not in self.limits:
            raise ValueError(f"未知成本类型: {key}，允许 {sorted(self.limits)}")
        return self.limits[key]

    def used(self, kind: str | CostKind) -> float:
        """当日该类型的累计支出。

        **读刻意留在调用方的 session 上**，即使写已经搬到了独立连接。这样它同时
        看得见两种流水，一条都不漏：

        - :meth:`charge` 在独立连接上提交的（绝大多数）。看得见是因为 pysqlite 只在
          DML 前发 ``BEGIN``、``SELECT`` 走 autocommit——调用方的连接不会攥着一个陈旧
          的读快照。唯一会定格快照的情况是调用方自己进了写事务，而那时整库写锁在它
          手里，别的连接**根本提交不了**，也就不存在"漏掉别人刚提交的流水"。
        - 调用方握着写锁时退回它自己的连接写的那几笔（见 :meth:`_write_entry`）——
          那几笔还没提交，只有这条 session 看得见。

        反过来把读也搬到独立连接，第二种流水就会漏，``used`` 偏小 → 本该拦下的支出
        被放行。这是金额闸门，宁可多绕一句注释也不能松。
        """
        key = str(kind)
        self.limit_of(key)
        total = self.session.scalar(
            select(func.coalesce(func.sum(CostLedger.amount), 0.0)).where(
                CostLedger.day == self.day, CostLedger.kind == key
            )
        )
        return float(total or 0.0)

    def remaining(self, kind: str | CostKind) -> float:
        return max(self.limit_of(kind) - self.used(kind), 0.0)

    def is_exhausted(self, kind: str | CostKind) -> bool:
        return self.used(kind) >= self.limit_of(kind)

    def snapshot(self) -> dict[str, dict[str, float]]:
        """给统计页用。"""
        return {
            kind: {
                "used": self.used(kind),
                "limit": self.limits[kind],
                "remaining": self.remaining(kind),
            }
            for kind in self.limits
        }

    # -- 记账 --------------------------------------------------------------

    def ensure(self, kind: str | CostKind, amount: float) -> None:
        """余额不够就抛 :class:`BudgetExhausted`，**不写流水**。

        给"先付费后拿货"的外部调用用（生图一次请求就是真金白银）：请求发出去之前
        先问一句还有没有额度，够了再发，发成功了才 :meth:`charge`。这样超预算时
        一分钱都不会花出去，失败时也不会白占一格配额。

        与 :meth:`charge` 的超限分支共用同一套通知，口径一致。
        """
        key = str(kind)
        limit = self.limit_of(key)
        remaining = self.remaining(key)
        if amount > remaining:
            self._notify_exhausted(key, amount, remaining, limit)
            raise BudgetExhausted(key, amount, remaining, limit)

    def charge(
        self,
        kind: str | CostKind,
        amount: float,
        *,
        meta: dict[str, Any] | None = None,
    ) -> CostLedger:
        """扣减预算并落流水。超限抛 :class:`BudgetExhausted`，且**不**写流水。"""
        key = str(kind)
        limit = self.limit_of(key)
        if amount < 0:
            raise ValueError("amount 不能为负")
        remaining = self.remaining(key)
        if amount > remaining:
            self._notify_exhausted(key, amount, remaining, limit)
            raise BudgetExhausted(key, amount, remaining, limit)
        return self._write_entry(
            {
                "id": new_id("cost"),
                "day": self.day,
                "kind": key,
                "amount": float(amount),
                # 调用方给的 meta 优先：labels 只是"没写就补上"的默认归属
                "meta": {**self.labels, **(meta or {})},
            }
        )

    # -- 落库 --------------------------------------------------------------

    def _caller_holds_write_lock(self) -> bool:
        """调用方那条连接是不是**已经**握着 SQLite 的整库写锁。

        判据是 DBAPI 连接的 ``in_transaction``，不是 ``Session.in_transaction()``：
        后者在第一次 ``SELECT`` 触发 autobegin 时就为真，而 pysqlite 只在 DML 之前
        才真的发 ``BEGIN``。所以前者恰好等价于"这条连接拿到写锁了"，后者会把"只读
        过几张表"误判成持锁。

        非 SQLite 后端没有整库写锁（行级锁不会造成这里说的死等），一律当作没握着。
        """
        session = self.session
        if not session.in_transaction():
            return False
        try:
            if session.get_bind().dialect.name != "sqlite":
                return False
            raw = session.connection().connection.dbapi_connection
        except Exception:  # pragma: no cover - 拿不到底层连接就按最保守的走
            return True
        return bool(getattr(raw, "in_transaction", False))

    def _write_entry(self, fields: dict[str, Any]) -> CostLedger:
        """把一笔流水落库。优先走独立连接并当场 commit。

        **退路是必须的，不是防御性代码**：调用方已经握着写锁时，独立连接拿不到锁，
        而对方要等本次调用返回才 commit——这是真死锁，干等只会在 ``busy_timeout``
        之后抛 ``database is locked``，把一笔**已经真实发生的**开销整个丢掉。真实
        存在的形状是 ``generation/video_pipeline.py`` 的 ``charge_render_seconds``：
        它在 ``RenderJob`` flush 之后的 ``finally`` 里记账。

        退回调用方的 session 之后语义与修复前完全一致（跟着外层事务走），不会更差；
        丢账才会更差。走了退路就记一条 warning，免得"某条链路其实没享受到修复"
        只能靠读代码发现。
        """
        if not self._caller_holds_write_lock():
            from core.db import get_session_factory

            ledger = get_session_factory()()
            try:
                entry = CostLedger(**fields)
                ledger.add(entry)
                ledger.commit()
                return entry
            except OperationalError as exc:
                # 到这里说明锁在**别人**手里（同进程另一个 tick / 另一个进程），
                # 已经等满 busy_timeout。退回调用方的连接，至少账不丢
                ledger.rollback()
                logger.warning("成本流水独立提交失败，退回调用方事务：%s", exc)
            finally:
                ledger.close()

        entry = CostLedger(**fields)
        self.session.add(entry)
        self.session.flush()
        logger.debug(
            "成本流水跟随调用方事务落库（调用方正握着写锁）kind=%s amount=%s",
            fields["kind"],
            fields["amount"],
        )
        return entry

    def _notify_exhausted(
        self, kind: str, requested: float, remaining: float, limit: float
    ) -> None:
        if self.notifier is None or kind in self._notified:
            return
        self._notified.add(kind)
        self.notifier.send(
            title=f"[成本超限] {kind}",
            text=(
                f"日期 {self.day}：请求 {requested:g}，剩余 {remaining:g}，上限 {limit:g}。"
                "已降级为只出选题不出稿。"
            ),
            level="error",
        )


__all__ = ["BudgetExhausted", "BudgetGuard", "CostKind", "today_key"]
