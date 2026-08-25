"""按账号限频：日上限 + 两次发布最小间隔。

P4 改造要点（P0–P3 是纯进程内实现）：

- **真相在 DB**：当日已发数与最近发布时刻来自 ``PublishRecord.phase='done'``，
  重启、多进程、`/dev/*` 旁路都不会把计数清零。
- 进程内计数只作**缓存**：默认 30 秒 TTL，避免每次 ``allow()`` 都打一次库；
  合并策略是 ``max(DB, 本地)``，宁可少发不可多发。
- 调度器把**自己那个事务的 session** 传进来，于是同一个 tick 里刚 flush 但还没
  commit 的发布记录也算数——否则一个 tick 会把同一账号的日上限连发穿。
- 发布器（xhs / douyin）不持有 session，仍按老签名调用；它们读到的是调度器刚
  同步过的缓存，冷启动时会自己开一次只读 session 兜底。

历史位置在 ``core/scheduler.py``，为避免发布器 import 调度器（连带 APScheduler）
而下沉到本模块；``core.scheduler`` 仍原样再导出 :class:`RateLimiter` 与
:data:`RATE_LIMITER`，既有调用点无需改动。

日界口径（P11.3）
-----------------
本仓库有**三种"今天"**，看着像重复其实各有各的理由，**不要把它们统一掉**——
统一任何一处都会悄悄放宽另一处的闸门：

1. **限频 = 账号本地日**（本模块 + ``core.scheduling``）。日上限护的是"这个号在
   它自己的作息里一天发了几条"；窗口与槽位分配本来就按本地日分桶
   （``core.scheduling._local_day``），计数必须跟齐。
   P11.3 之前这里按 UTC 日切：公众号默认窗口 ``07:00-09:00`` 正好横跨 ``Asia/Shanghai``
   的 UTC 午夜缝（本地 08:00 = 00:00Z），本地 07:30 与 08:30 各发一条会落进两个
   UTC 日，``daily_limit: 1`` 形同虚设。
2. **成本闸门 = UTC 日**（``core.budget.today_key`` / :class:`~core.budget.BudgetGuard`）。
   token / 渲染秒 / 生图张数是**一台 core 服务**的开销，与账号无关；同一台服务上
   可以跑多个时区的号，按谁的本地日重置都说不通，UTC 日是唯一有定义的口径。
   **本模块的改动不涉及它。**
3. **手动出稿草稿计数 = UTC 日**（``core.account_admin.generated_today``）。那是
   "防止人连点出稿按钮烧 token"的闸门，与第 2 类同源，不是限频，同样不改。

只有第 1 类是"平台会不会觉得这个号像机器"的安全带，所以只有它跟着账号时区走。
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.models import utcnow

logger = logging.getLogger("social_workflow.ratelimit")

#: 进程内缓存的默认有效期（秒）。调度器每分钟一 tick，30 秒足够挡住同一 tick 内的重复查询
DEFAULT_CACHE_TTL_SECONDS = 30.0


@dataclass(frozen=True)
class DbUsage:
    """某账号的发布用量（来自 ``PublishRecord``）。"""

    #: **账号本地日**已完成的发布数（日界见模块文档第 1 类）
    count_today: int
    #: 最近一次完成发布的时刻（跨天，用于最小间隔判定）
    last_at: datetime | None
    #: 算 ``count_today`` 时用的时区名。:class:`RateLimiter` 拿它给进程内计数分桶，
    #: 免得缓存按一个日界、DB 按另一个日界。空串 = 没解析出来（DB 没这个号）
    timezone: str = ""


def _aware(now: datetime | None = None) -> datetime:
    """补 tzinfo：naive 一律当 UTC（DB 里存的就是 UTC naive）。"""
    moment = now or utcnow()
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment


def utc_day_start(now: datetime | None = None) -> datetime:
    """当前 UTC 日的零点。与 :func:`core.budget.today_key` 同一口径。

    **限频已经不用它了**（见模块文档）：留着是给第 3 类日界用的
    ——``core.account_admin.generated_today`` 的手动出稿草稿计数。
    """
    return _aware(now).astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def local_day_start(tz_name: str, now: datetime | None = None) -> datetime:
    """``now`` 所在的**账号本地日**的零点，返回 UTC 时刻。

    返回 UTC 是因为 ``PublishRecord.updated_at`` 存的是 UTC，比较必须在同一系。
    夏令时把本地零点整个跳过去的时区（如 America/Santiago）里，``ZoneInfo`` 会
    落在跳变前的偏移上，等于把日界往前挪一小时——方向是**少放行**，安全。
    """
    from core.accounts import resolve_timezone, today_in

    moment = _aware(now)
    tz = resolve_timezone(tz_name)
    return datetime.combine(today_in(tz_name, moment), time.min, tzinfo=tz).astimezone(UTC)


def local_day_key(tz_name: str, now: datetime | None = None) -> str:
    """账号本地日期键 ``YYYY-MM-DD``。进程内计数按它分桶。

    与 :func:`core.budget.today_key` 刻意不共用：那个是成本闸门的 UTC 日。
    """
    from core.accounts import today_in

    return today_in(tz_name, _aware(now)).isoformat()


def account_timezone(session: Session, account_id: str) -> str:
    """该账号的时区名。解析规则与 :func:`core.accounts.policy_of` 完全一致
    （``extra['timezone']`` → 部署默认），库里没这个号时回落部署默认时区。"""
    from core.accounts import default_timezone
    from core.models import Account

    account = session.get(Account, account_id)
    extra = dict(account.extra or {}) if account is not None else {}
    return str(extra.get("timezone") or default_timezone())


def db_usage(
    session: Session,
    account_id: str,
    *,
    now: datetime | None = None,
    timezone: str | None = None,
) -> DbUsage:
    """查该账号**本地今天**的发布数与最近发布时刻。

    只认 ``phase='done'``：``in_flight`` 代表"正在发但还不知道结果"，
    把它计进日上限会在一次失败后永久占掉配额。

    发布时刻取 ``PublishRecord.updated_at``（phase 置 done 时写入），
    与 ``metrics.collector.published_at_of`` 保持同一口径。

    ``timezone`` 缺省 = 从 ``Account.extra`` 解析（:func:`account_timezone`）。
    手里已经有 ``policy`` 的调用方直接传进来，省一次 ``session.get``。
    **刻意不留 UTC 分支**：所有调用方问的都是"这个号今天发了几条"，多一个口径
    只会让下一个人挑错——成本闸门那类"今天"在 ``core.budget``，与本函数无关。
    """
    from core.models import ContentItem, PublishRecord
    from core.state_machine import PublishPhase

    moment = _aware(now)
    tz_name = timezone or account_timezone(session, account_id)
    day_start = local_day_start(tz_name, moment)
    done = (
        ContentItem.account_id == account_id,
        PublishRecord.phase == PublishPhase.DONE.value,
    )
    join_on = ContentItem.id == PublishRecord.content_item_id

    count = session.scalar(
        select(func.count(PublishRecord.id))
        .join(ContentItem, join_on)
        .where(*done, PublishRecord.updated_at >= day_start)
    )
    # 最近发布时刻**不限当天**：最小间隔要能跨零点生效
    last = session.scalar(
        select(func.max(PublishRecord.updated_at)).join(ContentItem, join_on).where(*done)
    )
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return DbUsage(count_today=int(count or 0), last_at=last, timezone=tz_name)


class RateLimiter:
    """按账号限频：日上限 + 两次发布最小间隔。

    线程安全。``use_db=False`` 退化为 P0 的纯进程内实现（纯单测用）。
    """

    def __init__(
        self,
        *,
        min_interval_seconds: int | None = None,
        cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
        use_db: bool = True,
    ) -> None:
        # None = 跟随 settings（每次读取，测试改环境变量后立即生效）
        self._min_interval_seconds = min_interval_seconds
        self.cache_ttl = timedelta(seconds=max(cache_ttl_seconds, 0.0))
        self.use_db = use_db
        self._lock = threading.Lock()
        self._last: dict[str, datetime] = {}
        self._counts: dict[tuple[str, str], int] = defaultdict(int)
        self._tokens: set[str] = set()
        #: 每个账号上次从 DB 同步的时刻（按传入的 ``now`` 计，soak 的加速时钟同样适用）
        self._synced_at: dict[str, datetime] = {}
        #: 每个账号的时区名（``_counts`` 按它分本地日的桶）。由 :meth:`sync` 从 DB 学到，
        #: 或由调用方显式传 ``timezone=``。没有 session 的发布器就是靠这份缓存分对桶的
        self._timezones: dict[str, str] = {}

    # ------------------------------------------------------------------ 配置

    @property
    def min_interval(self) -> timedelta:
        """全局最小间隔。构造时未指定则每次从 settings 读取。"""
        if self._min_interval_seconds is not None:
            return timedelta(seconds=self._min_interval_seconds)
        from core.config import get_settings

        return timedelta(seconds=get_settings().sw_min_publish_interval_seconds)

    def _interval(self, override: timedelta | None) -> timedelta:
        return override if override is not None else self.min_interval

    # -------------------------------------------------------------- 日界分桶

    def _day_key(self, account_id: str, moment: datetime, timezone: str | None = None) -> str:
        """该账号在 ``moment`` 这一刻的**本地日**桶键（见模块文档第 1 类日界）。

        时区来源优先级：显式传入 > :meth:`sync` 从 DB 学到的 > 部署默认时区。
        回落到部署默认只会发生在"这个号从没同步过就直接 :meth:`record`"的路径上
        （发布器都是先 :meth:`allow` 再 :meth:`record`，那时时区已经学到了）；
        真相在 DB，下一次 :meth:`sync` 会把桶纠正回来。

        不加锁：``dict`` 的读写在 GIL 下是原子的，而调用方多半正握着 ``self._lock``
        （``threading.Lock`` 不可重入，这里再去抢一次就死锁了）。
        """
        if timezone:
            self._timezones[account_id] = timezone
        else:
            timezone = self._timezones.get(account_id)
        if not timezone:
            from core.accounts import default_timezone

            timezone = default_timezone()
        return local_day_key(timezone, moment)

    # ---------------------------------------------------------------- DB 同步

    def sync(
        self,
        account_id: str,
        *,
        session: Session | None = None,
        now: datetime | None = None,
        force: bool = False,
        timezone: str | None = None,
    ) -> DbUsage | None:
        """从 DB 刷新该账号的用量并并入进程内缓存（取大者）。

        ``session`` 为空时自己开一个只读 session——**看不到**别处未提交的事务，
        所以调度器一定要把自己的 session 传进来。
        """
        if not self.use_db:
            return None
        moment = _aware(now)
        if not force:
            with self._lock:
                last_sync = self._synced_at.get(account_id)
            if last_sync is not None and moment - last_sync < self.cache_ttl:
                return None

        try:
            if session is not None:
                usage = db_usage(session, account_id, now=moment, timezone=timezone)
            else:
                from core import db

                with db.get_session_factory()() as own:
                    usage = db_usage(own, account_id, now=moment, timezone=timezone)
        except Exception as exc:  # DB 未初始化 / 表不存在：退回纯进程内计数
            logger.debug("限频同步失败（退回进程内计数）account=%s: %s", account_id, exc)
            return None

        # DB 用哪个时区切的日，进程内缓存就用哪个分桶，两边口径必须一致
        key = (account_id, self._day_key(account_id, moment, usage.timezone or timezone))
        with self._lock:
            self._counts[key] = max(self._counts[key], usage.count_today)
            if usage.last_at is not None:
                current = self._last.get(account_id)
                if current is None or usage.last_at > current:
                    self._last[account_id] = usage.last_at
            self._synced_at[account_id] = moment
        return usage

    # ------------------------------------------------------------------ 判定

    def allow(
        self,
        account_id: str,
        daily_limit: int,
        *,
        now: datetime | None = None,
        session: Session | None = None,
        min_interval: timedelta | None = None,
        timezone: str | None = None,
    ) -> bool:
        """该账号此刻是否允许再发一条。日上限按**账号本地日**计。"""
        moment = _aware(now)
        self.sync(account_id, session=session, now=moment, timezone=timezone)
        interval = self._interval(min_interval)
        day = self._day_key(account_id, moment, timezone)
        with self._lock:
            if self._counts[(account_id, day)] >= daily_limit:
                return False
            last = self._last.get(account_id)
            return not (last is not None and moment - last < interval)

    def deny_reason(
        self,
        account_id: str,
        daily_limit: int,
        *,
        now: datetime | None = None,
        session: Session | None = None,
        min_interval: timedelta | None = None,
        timezone: str | None = None,
    ) -> str | None:
        """不允许发布的原因（结构化日志用）。允许则返回 ``None``。"""
        moment = _aware(now)
        self.sync(account_id, session=session, now=moment, timezone=timezone)
        interval = self._interval(min_interval)
        day = self._day_key(account_id, moment, timezone)
        with self._lock:
            used = self._counts[(account_id, day)]
            last = self._last.get(account_id)
        if used >= daily_limit:
            return f"daily_limit: 今日已发 {used}/{daily_limit}"
        if last is not None and moment - last < interval:
            wait = (last + interval - moment).total_seconds()
            minutes = interval.total_seconds() / 60
            return f"min_interval: 距上次发布不足 {minutes:.0f} 分钟（还需 {wait:.0f}s）"
        return None

    def used_today(
        self,
        account_id: str,
        *,
        now: datetime | None = None,
        session: Session | None = None,
        timezone: str | None = None,
    ) -> int:
        """该账号**本地今天**已发出去的条数。"""
        moment = _aware(now)
        self.sync(account_id, session=session, now=moment, timezone=timezone)
        day = self._day_key(account_id, moment, timezone)
        with self._lock:
            return self._counts[(account_id, day)]

    def last_published_at(self, account_id: str) -> datetime | None:
        with self._lock:
            return self._last.get(account_id)

    def next_available_at(
        self, account_id: str, *, min_interval: timedelta | None = None
    ) -> datetime | None:
        with self._lock:
            last = self._last.get(account_id)
        return None if last is None else last + self._interval(min_interval)

    # ------------------------------------------------------------------ 记账

    def record(
        self,
        account_id: str,
        *,
        now: datetime | None = None,
        token: str | None = None,
        timezone: str | None = None,
    ) -> bool:
        """记一次发布。返回是否真的计了数。

        ``token`` 用于**跨调用方去重**：发布器自己会挡一道限频（``/dev/*`` 与人工触发
        不一定经过调度器），调度器成功后也会记一次，两边传同一个 token（内容项 id）
        才不会把一次发布记成两次。token 为空 = 无条件计数（保持 P0 行为）。

        注：DB 才是真相，这里的计数只是让"下一次 allow() 在 TTL 到期前"也准。
        """
        moment = _aware(now)
        day = self._day_key(account_id, moment, timezone)
        with self._lock:
            if token is not None:
                if token in self._tokens:
                    return False
                self._tokens.add(token)
            self._last[account_id] = moment
            self._counts[(account_id, day)] += 1
            return True

    def reset(self) -> None:
        with self._lock:
            self._last.clear()
            self._counts.clear()
            self._tokens.clear()
            self._synced_at.clear()
            self._timezones.clear()


#: 全进程共享的限频器。发布器与调度器共用同一个实例，日计数才不会算两遍
RATE_LIMITER = RateLimiter()


__all__ = [
    "DEFAULT_CACHE_TTL_SECONDS",
    "RATE_LIMITER",
    "DbUsage",
    "RateLimiter",
    "account_timezone",
    "db_usage",
    "local_day_key",
    "local_day_start",
    "utc_day_start",
]
