#!/usr/bin/env python
"""连续运行验证：加速时钟下模拟 3 天无人值守调度。

    uv run python scripts/soak.py              # 临时库，跑完即删
    uv run python scripts/soak.py --json       # 机器可读输出
    uv run python scripts/soak.py --days 7 --step-minutes 15
    uv run python scripts/soak.py --db data/soak.db --keep   # 留现场看表

退出码：0 = 全部断言通过；1 = 有断言失败。回归版本是 ``tests/test_soak.py``（< 30s）。

它验证什么
----------
计划第五节的"连续 3 天无人值守（除审核点击）跑通"。限频与时段窗口在系统里有
**两层防御**，两层各自被真实行使并断言（只验一层的话，另一层废掉了也看不出来）：

1. **无重复发布**——每条内容至多一条 ``phase=done`` 记录；同一时刻重跑一遍
   ``tick_scheduled_publish`` 不会再发；``FakePublisher.publish()`` 的成功次数
   与 done 记录数严格相等。
2. **排期层正确**（``core.scheduling.schedule_item``）——批准即排期，排出来的每个
   ``scheduled_at`` 都落在该账号 ``publish_windows`` 内、同账号相邻槽位间隔
   ``>= min_interval``、单个**账号本地日**的槽位数不超过 ``daily_limit``；
   批准的内容不会积压在 ``approved``。
3. **发布层兜底**（``tick_scheduled_publish``，纵深防御）——刻意注入一批**绕过排期层**
   的脏排期（模拟人工改库 / 未来 UI 手工改期 / 时钟漂移）：窗口外的排期项、
   同账号挤在同一分钟的排期项。它们必须被 ``skipped_window`` / ``skipped_rate``
   挡下：一条都不许在窗口外发出去，被挡住的只会推迟到窗口内才发，且任一账号任一天
   都不超过它的 ``daily_limit``。
4. **needs_relogin 账号被跳过**——它的排期项一条都没发出去。
5. **发布前人工确认**（P12，纵深防御的最后一道）——:data:`ACC_UNCONFIRMED` 的内容
   窗口、限频、账号健康全都不挡它，唯一挡得住它的只有"没人点确认"。断言它一条都
   没发出去、``skipped_unconfirmed`` 真的涨了，并且 TTL 到期后被自动驳回
   （不无限堆积）。
6. **死信触发通知**——一个必然失败的账号在 ``SW_MAX_PUBLISH_ATTEMPTS`` 次后进
   ``dead_letter``，并且通知通道里能看到 ``[死信]``。
7. **指标快照 24h / 7d 各一份**——按 ``metrics.collector`` 的窗口口径分别命中。

它怎么模拟
----------
- **时钟是参数不是全局**：每个 tick 都接受 ``now=``，驱动器按 ``--step-minutes``
  推进。没有 sleep，没有 monkeypatch ``datetime.now``。
- **只有"人工点击"是自动的**：:func:`_approve` 代替运营点批准（排期由生产代码
  ``core.scheduling.schedule_item`` 算，和 `POST /review/{id}/approve` 同一个函数），
  :func:`_confirm` 代替他点"确认发布"。这两下点击就是计划里那句"除审核点击"。
  其余全部走真实的 tick 函数（和 APScheduler 注册的是同一批）。
- **脏数据是显式注入的**：:func:`_inject_tampered` 直接往库里写不合法的
  ``scheduled_at``。这是模拟"排期层被绕过"的手段，不是生产路径——正因为绕过了，
  发布层闸门才有观测对象；否则排期层做对了，发布层的两道闸门整轮都不会被触发，
  "闸门生效"的断言就成了空转。
- **发布器是 FakePublisher，LLM 是 ScriptedLLM**：不联网、不烧 token，但走完
  完整的生成 → 机器审核 → 状态机 → 幂等 → 指标链路。

一个必要的作弊
--------------
``PublishRecord`` / ``ContentItem`` 的 ``created_at`` / ``updated_at`` 是**列默认值**
（``default=utcnow`` 在类定义时就绑死了真实时钟），注入 ``now=`` 影响不到它们。
不修正的话所有行都停在 t0：跨天限频永远不重置、退避永远立刻到期、24h/7d 窗口
永远不到点。所以驱动器在每步之后把**本步动过的行**回填成模拟时刻
（:func:`_retime`）。这是模拟器的手段，生产代码路径里没有这一步。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select

from core import db
from core.accounts import AccountPolicy, policy_of, resolve_timezone
from core.config import reload_settings
from core.confirm import confirm_item
from core.models import (
    Account,
    ContentItem,
    MetricSnapshot,
    PublishRecord,
    Topic,
    new_id,
)
from core.notify import LogNotifier, set_default_notifier
from core.ratelimit import RateLimiter
from core.scheduler import (
    tick_confirm_gate,
    tick_generate,
    tick_metrics,
    tick_retry_sweep,
    tick_scheduled_publish,
)
from core.scheduling import NoSlotAvailable, schedule_item
from core.state_machine import (
    ContentStatus,
    PublishPhase,
    approve,
)
from core.telegram import set_telegram_channel
from metrics.availability import MetricsPayloadKind, classify_metrics_payload
from publishers.base import ContentBundle, FakePublisher, RetryableError
from publishers.registry import register, reset_registry

logger = logging.getLogger("social_workflow.soak")

# ------------------------------------------------------------------ 场景定义

#: 正常账号：有发布时段窗口（本地 09:00–11:00）、日上限 2、最小间隔 60 分钟。
#: 它的内容**全部**由 ``core.scheduling.schedule_item`` 排期——排期层断言只看它。
ACC_OK = "soak-xhs-ok"
#: 掉线账号：``needs_relogin``。它的排期项一条都不该发出去
ACC_OFFLINE = "soak-xhs-offline"
#: 必然失败的账号：发布器一直抛 RetryableError，最终进死信并告警
ACC_DEAD = "soak-xhs-dead"
#: 被"手工改库"污染的账号：排期数据绕过排期层直接写进库，且**故意不合法**。
#: 它存在的唯一理由是给发布层的窗口 / 限频闸门一个观测对象
ACC_TAMPERED = "soak-xhs-tampered"
#: 故意**永远不确认**的账号（P12）：不设窗口、不设间隔、日上限给大，
#: 唯一能挡住它的只有发布前人工确认这一道。它验证"没人点就绝不发"，
#: 以及 TTL 到了会自动驳回、不无限堆积
ACC_UNCONFIRMED = "soak-xhs-unconfirmed"

WINDOW = "09:00-11:00"
TIMEZONE = "Asia/Shanghai"
DAILY_LIMIT = 2
MIN_INTERVAL_MINUTES = 60

#: 各账号的日上限。收尾断言按这张表逐账号核对"任一天都没超"
DAILY_LIMITS: dict[str, int] = {
    ACC_OK: DAILY_LIMIT,
    ACC_TAMPERED: DAILY_LIMIT,
    ACC_OFFLINE: 10,
    ACC_DEAD: 10,
    ACC_UNCONFIRMED: 10,
}

#: 脏排期之一：账号本地 03:00，**铁定在 WINDOW 之外**。它验证的是
#: "排期时刻不在窗口内 → 发布时刻仍被闸门挡住，只能推迟到窗口打开"
DIRTY_OUT_OF_WINDOW = time(3, 0)
#: 脏排期之二：账号本地 09:05（窗口内），同一分钟塞 :data:`DIRTY_DENSE_COUNT` 条。
#: 它验证的是"同账号密集排期 → 日上限 / 最小间隔在发布时刻仍然算数"
DIRTY_IN_WINDOW = time(9, 5)
DIRTY_DENSE_COUNT = 2


@dataclass
class SoakConfig:
    days: int = 3
    step_minutes: int = 30
    #: 每隔几步跑一次 tick_generate（30 分钟一步 → 12 步 = 6 小时）
    generate_every_steps: int = 12
    #: 每隔几步跑一次 tick_metrics
    metrics_every_steps: int = 12
    #: 尾声：把时钟推到 t0 + 这么多天，让 7d 指标窗口到期
    tail_days: int = 8

    @property
    def steps(self) -> int:
        return int(self.days * 24 * 60 / self.step_minutes)

    @property
    def step(self) -> timedelta:
        return timedelta(minutes=self.step_minutes)


@dataclass
class SoakResult:
    config: SoakConfig
    started_at: datetime
    steps: int = 0
    generated: int = 0
    approved: int = 0
    #: 批准后由 core/scheduling.py 排定的槽位：账号 → 该账号的所有 scheduled_at（UTC）
    planned_slots: dict[str, list[datetime]] = field(default_factory=dict)
    #: 排期层的违规明细（都应为空）：窗口外的槽位 / 间隔过近的相邻槽位 / 单日超额的日期
    slots_out_of_window: list[str] = field(default_factory=list)
    slot_interval_violations: list[str] = field(default_factory=list)
    slot_daily_overflow: list[str] = field(default_factory=list)
    #: 绕过排期层注入的脏排期条数；其中真发出去的；其中被推迟到原定时刻之后才发的
    dirty_injected: int = 0
    dirty_published: int = 0
    dirty_deferred: int = 0
    #: 脏数据自检：注入的排期里落在窗口外的条数 / 相邻间隔小于 min_interval 的对数。
    #: 两个都 > 0 才说明"脏数据真的脏"，否则发布层的断言是空转
    dirty_out_of_window: int = 0
    dirty_too_close: int = 0
    #: 发布层的违规明细（都应为空）：在窗口外发出去的 / 相邻发布间隔小于最小间隔的
    published_out_of_window: list[str] = field(default_factory=list)
    publish_interval_violations: list[str] = field(default_factory=list)
    published: int = 0
    duplicate_attempts: int = 0
    skipped_rate: int = 0
    skipped_window: int = 0
    skipped_account: int = 0
    retry_published: int = 0
    dead_letters: int = 0
    snapshots_24h: int = 0
    snapshots_7d: int = 0
    #: 各账号的最大单日发布数
    max_daily: dict[str, int] = field(default_factory=dict)
    offline_published: int = 0
    dead_letter_notices: int = 0
    # -- 发布前人工确认闸门（P12）--
    cards_pushed: int = 0
    confirmed: int = 0
    skipped_unconfirmed: int = 0
    confirm_expired: int = 0
    unconfirmed_published: int = 0
    publish_calls: dict[str, int] = field(default_factory=dict)
    done_records: int = 0
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, bool(ok), detail))

    @property
    def auto_scheduled(self) -> int:
        """批准后被 ``schedule_item`` 排上期的条数。"""
        return sum(len(slots) for slots in self.planned_slots.values())

    @property
    def ok(self) -> bool:
        return all(ok for _, ok, _ in self.checks)

    @property
    def failures(self) -> list[str]:
        return [f"{name}: {detail}" for name, ok, detail in self.checks if not ok]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "days": self.config.days,
            "step_minutes": self.config.step_minutes,
            "steps": self.steps,
            "started_at": self.started_at.isoformat(),
            "generated": self.generated,
            "approved": self.approved,
            "auto_scheduled": self.auto_scheduled,
            "planned_slots": {
                account_id: [slot.isoformat() for slot in sorted(slots)]
                for account_id, slots in self.planned_slots.items()
            },
            "slots_out_of_window": self.slots_out_of_window,
            "slot_interval_violations": self.slot_interval_violations,
            "slot_daily_overflow": self.slot_daily_overflow,
            "dirty_injected": self.dirty_injected,
            "dirty_published": self.dirty_published,
            "dirty_deferred": self.dirty_deferred,
            "dirty_out_of_window": self.dirty_out_of_window,
            "dirty_too_close": self.dirty_too_close,
            "published_out_of_window": self.published_out_of_window,
            "publish_interval_violations": self.publish_interval_violations,
            "published": self.published,
            "done_records": self.done_records,
            "duplicate_attempts": self.duplicate_attempts,
            "skipped_rate": self.skipped_rate,
            "skipped_window": self.skipped_window,
            "skipped_account": self.skipped_account,
            "retry_published": self.retry_published,
            "dead_letters": self.dead_letters,
            "dead_letter_notices": self.dead_letter_notices,
            "snapshots_24h": self.snapshots_24h,
            "snapshots_7d": self.snapshots_7d,
            "max_daily": self.max_daily,
            "offline_published": self.offline_published,
            "cards_pushed": self.cards_pushed,
            "confirmed": self.confirmed,
            "skipped_unconfirmed": self.skipped_unconfirmed,
            "confirm_expired": self.confirm_expired,
            "unconfirmed_published": self.unconfirmed_published,
            "publish_calls": self.publish_calls,
            "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in self.checks],
            "failures": self.failures,
        }


# --------------------------------------------------------------------- 环境


@contextmanager
def soak_env(**overrides: str) -> Iterator[None]:
    """临时改环境变量并重载 settings，退出时还原。"""
    base = {
        # 没 key → 生成链自动用 ScriptedLLM（见 core.dev_flow.resolve_llm）
        "ANTHROPIC_API_KEY": "",
        "SW_USE_FAKE_PUBLISHERS": "true",
        "SW_SYNC_ACCOUNTS_ON_START": "false",
        # 全局最小间隔归零：本次由账号级 min_interval_minutes 说了算
        "SW_MIN_PUBLISH_INTERVAL_SECONDS": "0",
        "SW_MAX_PUBLISH_ATTEMPTS": "3",
        # 无 Playwright 也能跑：只出文案不出卡片
        "SW_GENERATE_MAKE_MEDIA": "false",
        "SW_GENERATE_MAX_PER_TICK": "3",
        # 退避压到 5 / 10 分钟，3 天里能走完 3 次尝试进死信
        "SW_RETRY_BACKOFF_BASE_SECONDS": "300",
        "SW_RETRY_BACKOFF_MAX_SECONDS": "1800",
        "SW_TIMEZONE": TIMEZONE,
        "DAILY_TOKEN_BUDGET": "10000000",
        "FEISHU_WEBHOOK": "",
        "NEWSNOW_BASE_URL": "",
        "TRENDRADAR_BASE_URL": "",
    }
    base.update(overrides)
    saved = {key: os.environ.get(key) for key in base}
    os.environ.update(base)
    reload_settings()
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reload_settings()


# ------------------------------------------------------------------ 造场景


def _account(
    account_id: str,
    *,
    status: str = "ok",
    daily_limit: int = DAILY_LIMIT,
    windows: list[str] | None = None,
    daily_target: int = 0,
    min_interval_minutes: int | None = MIN_INTERVAL_MINUTES,
) -> Account:
    extra: dict[str, Any] = {"timezone": TIMEZONE}
    if windows is not None:
        extra["publish_windows"] = windows
    if daily_target:
        extra["daily_target"] = daily_target
    if min_interval_minutes is not None:
        extra["min_interval_minutes"] = min_interval_minutes
    return Account(
        id=account_id,
        platform="xhs",
        name=f"soak {account_id}",
        status=status,
        sidecar_endpoint="http://localhost:18060",
        daily_limit=daily_limit,
        extra=extra,
    )


def seed_accounts() -> None:
    with db.session_scope() as session:
        session.add(_account(ACC_OK, windows=[WINDOW], daily_target=2, daily_limit=DAILY_LIMIT))
        # 掉线账号：不设窗口、日上限给大，唯一挡住它的应该是账号健康
        session.add(
            _account(
                ACC_OFFLINE,
                status="needs_relogin",
                daily_limit=10,
                windows=[],
                min_interval_minutes=0,
            )
        )
        # 死信账号：不设窗口、不设间隔，让它尽快把重试次数用完
        session.add(_account(ACC_DEAD, daily_limit=10, windows=[], min_interval_minutes=0))
        # 被污染的账号：窗口 / 限频与正常账号同配，差别只在它的排期是手工塞进去的
        session.add(_account(ACC_TAMPERED, windows=[WINDOW], daily_limit=DAILY_LIMIT))
        # 永不确认的账号（P12）：和掉线账号同样的设计——不设窗口、不设间隔、
        # 日上限给大，于是唯一能挡住它的只剩下"没人点确认"
        session.add(_account(ACC_UNCONFIRMED, daily_limit=10, windows=[], min_interval_minutes=0))


def seed_topics(day: int) -> None:
    """给选题池灌几条候选。不联网——``tick_sourcing`` 的网络部分另有单测覆盖。"""
    with db.session_scope() as session:
        for index in range(3):
            session.add(
                Topic(
                    id=new_id("tpc"),
                    source="soak",
                    title=f"第 {day} 天的候选选题 {index + 1}：租房收纳的第 {day}{index} 个坑",
                    url=None,
                    score=1.0 - index * 0.1,
                    raw={"soak_day": day},
                )
            )


def make_item(session: Any, account_id: str, title: str, *, moment: datetime) -> ContentItem:
    """直接造一条已排期的内容（掉线 / 死信两个账号用，不走生成链）。"""
    item_id = new_id("itm")
    bundle = ContentBundle(
        id=item_id,
        account_id=account_id,
        platform="xhs",
        title=title,
        body_markdown=f"{title}\n\n（soak 造的内容，不会真的发出去。）",
        tags=["soak"],
    )
    item = ContentItem(
        id=item_id,
        account_id=account_id,
        status=ContentStatus.SCHEDULED.value,
        bundle_json=bundle.model_dump(mode="json"),
        scheduled_at=moment,
    )
    session.add(item)
    session.flush()
    return item


def _local_moment(moment: datetime, at: time) -> datetime:
    """把"账号本地时区的某个钟点"换算成 UTC，日期取 ``moment`` 所在的本地日。"""
    tz = resolve_timezone(TIMEZONE)
    return datetime.combine(moment.astimezone(tz).date(), at, tzinfo=tz).astimezone(UTC)


def _inject_tampered(moment: datetime, day: int) -> dict[str, datetime]:
    """注入**绕过排期层**的脏排期，返回 ``{item_id: scheduled_at}``。

    模拟的是排期层管不到的三种现实：人工改库、未来审核 UI 上的"手工改期"、
    宿主机时钟漂移。它们共同的后果是库里出现了 ``core.scheduling`` 绝不会排出来的
    ``scheduled_at``：

    - 一条落在**窗口外**（本地 03:00）；
    - :data:`DIRTY_DENSE_COUNT` 条挤在**同一分钟**（本地 09:05，窗口内），
      连最小间隔的边都不沾。

    这些内容直接以 ``scheduled`` 落库，``tick_scheduled_publish`` 会照常扫到它们——
    于是发布层的窗口 / 限频闸门有了观测对象。它们**不该**被放行到窗口外或超额发出，
    收尾断言盯的就是这一点。
    """
    out_of_window = _local_moment(moment, DIRTY_OUT_OF_WINDOW)
    dense_at = _local_moment(moment, DIRTY_IN_WINDOW)
    injected: dict[str, datetime] = {}
    with db.session_scope() as session:
        item = make_item(
            session, ACC_TAMPERED, f"被改到窗口外的第 {day} 天的稿", moment=out_of_window
        )
        injected[item.id] = out_of_window
        for index in range(DIRTY_DENSE_COUNT):
            item = make_item(
                session,
                ACC_TAMPERED,
                f"挤在同一分钟的第 {day} 天的稿 {index + 1}",
                moment=dense_at,
            )
            injected[item.id] = dense_at
    return injected


def register_publishers(calls: dict[str, FakePublisher]) -> None:
    """每个账号一个**共享实例**，这样 ``publish_calls`` 能跨 tick 累加。"""
    reset_registry()

    def factory(account_id: str, **_: Any) -> FakePublisher:
        if account_id not in calls:
            calls[account_id] = FakePublisher(
                account_id,
                platform="xhs",
                # 死信账号：永远抛可重试错误 → attempts 用完进 dead_letter
                raise_exc=RetryableError("soak: 平台 502") if account_id == ACC_DEAD else None,
                post_id_prefix="soak",
            )
        return calls[account_id]

    register("xhs", factory)


# ------------------------------------------------------------------ 时间修正


def _retime(moment: datetime, seen: dict[str, int]) -> None:
    """把本步**动过**的发布记录时间戳改成模拟时刻。

    见模块 docstring "一个必要的作弊"。只动 ``PublishRecord``：限频、退避、
    指标窗口三处都以它为准（``metrics.collector.published_at_of``）。

    判定"动过"用的是 ``attempts``（每发起一次尝试 +1），**不是**比较时间戳。
    早期版本用"updated_at != moment 就改"，结果把已经 done 的记录也一路推着走：
    最小间隔永远处于"刚发过"、7d 窗口永远不到期，整个模拟悄悄失真。
    """
    from sqlalchemy.orm.attributes import flag_modified

    with db.session_scope() as session:
        for record in session.scalars(select(PublishRecord)):
            previous = seen.get(record.id)
            if previous is not None and previous == record.attempts:
                continue  # 这一步没碰它：done 的记录必须停在它真正发布的那一刻
            if previous is None:
                record.created_at = moment
            record.updated_at = moment
            flag_modified(record, "updated_at")
            seen[record.id] = record.attempts


# ------------------------------------------------------------------ 人工卡点


def _approve(moment: datetime) -> tuple[int, list[tuple[str, datetime]]]:
    """替运营点"批准"。返回 ``(批准数, [(账号, 排定时刻)])``。

    这是整个 soak 里**唯一**模拟人的地方——计划里的"连续 3 天无人值守
    （除审核点击）"，就是这一下点击。

    **排期不在这里做**：早期版本在这里直接 ``transition→scheduled`` 并把
    ``scheduled_at`` 设成当前时刻，结果是模拟器自己伪造了一条生产代码里根本不存在的
    路径，掩盖了"批准之后没人排期"这个真缺陷。现在调 ``core.scheduling.schedule_item``，
    和 `POST /review/{id}/approve` 走的是同一个函数。

    返回的槽位列表就是排期层断言的观测对象：它是 ``schedule_item`` **亲口给出**的
    时刻，不是事后从库里捞的（捞出来的分不清哪些是脏数据注入的）。
    """
    approved = 0
    slots: list[tuple[str, datetime]] = []
    with db.session_scope() as session:
        stmt = select(ContentItem).where(
            ContentItem.status.in_([ContentStatus.DRAFT.value, ContentStatus.REVIEWING.value])
        )
        for item in session.scalars(stmt):
            approve(session, item, actor="soak-operator", reason="soak 自动批准")
            approved += 1
            account = session.get(Account, item.account_id)
            if account is None:
                continue
            try:
                slot = schedule_item(session, item, account, actor="soak-operator", now=moment)
            except NoSlotAvailable as exc:
                # 保持 approved，等窗口腾出来。soak 会在收尾断言里检查有没有积压
                logger.info("排不上期 item=%s: %s", item.id, exc)
                continue
            slots.append((item.account_id, slot))
    return approved, slots


class SoakTelegram:
    """模拟 Telegram 通道：不出网，只记下推了几张卡、改了几次。

    没有它的话 ``ensure_confirm_pushed`` 永远推不出去，``confirm_pushed_at`` 一直为空，
    TTL 就退化成"从排期时刻算"——那条路另有单测覆盖，soak 要走的是**推得出去**
    那条主路。
    """

    def __init__(self) -> None:
        self.cards: list[Any] = []
        self.edits: list[tuple[str, str]] = []

    def send(self, title: str, text: str, level: str = "info") -> bool:
        return True

    def send_confirm_card(self, card: Any, *, cover: Any = None) -> str:
        self.cards.append(card)
        return f"soak:{len(self.cards)}:text"

    def edit_card(self, ref: str, text: str) -> bool:
        self.edits.append((ref, text))
        return True

    def answer(self, callback_query_id: str, text: str) -> None:  # pragma: no cover - 用不到
        return None


def _confirm(moment: datetime) -> int:
    """替运营点"确认发布"——soak 里**第二处**模拟人的地方（P12）。

    发布前的人工确认是合规底线（小红书封禁 AI 全托管账号），系统不许有旁路，
    所以模拟器也不能绕过去：它必须像人一样，在卡片推出来之后逐条点确认。

    :data:`ACC_UNCONFIRMED` 的内容**故意一条都不点**——它是"没人点就绝不发"
    这条断言的观测对象。
    """
    confirmed = 0
    with db.session_scope() as session:
        stmt = select(ContentItem).where(
            ContentItem.status == ContentStatus.SCHEDULED.value,
            ContentItem.confirmed_at.is_(None),
            ContentItem.account_id != ACC_UNCONFIRMED,
        )
        for item in session.scalars(stmt):
            confirm_item(session, item, actor="soak-operator", now=moment)
            confirmed += 1
    return confirmed


# ------------------------------------------------------------------ 主循环


def run_soak(config: SoakConfig | None = None) -> SoakResult:
    """跑一遍模拟。调用方需保证 ``core.db`` 已经 configure + init_db。"""
    config = config or SoakConfig()
    notifier = LogNotifier()
    set_default_notifier(notifier)
    telegram = SoakTelegram()
    set_telegram_channel(telegram)  # type: ignore[arg-type]
    limiter = RateLimiter(min_interval_seconds=None, cache_ttl_seconds=0.0)
    publishers: dict[str, FakePublisher] = {}
    register_publishers(publishers)

    # t0 对齐到 UTC 整点，方便按窗口推算；从"现在"开始，这样列默认值写下的
    # 真实时间戳不会跑到模拟时钟前面去
    t0 = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    result = SoakResult(config=config, started_at=t0)

    seed_accounts()
    seen_records: dict[str, int] = {}
    seeded_days: set[int] = set()
    #: 绕过排期层注入的脏排期：item_id → 手工写进去的 scheduled_at
    dirty_slots: dict[str, datetime] = {}

    for step in range(config.steps):
        moment = t0 + config.step * step
        day = step * config.step_minutes // (24 * 60)
        result.steps += 1

        # -- 每天灌一批候选选题 + 给另外两个账号各造一条排期 -------------------
        if day not in seeded_days:
            seeded_days.add(day)
            seed_topics(day)
            with db.session_scope() as session:
                make_item(session, ACC_OFFLINE, f"掉线账号第 {day} 天的稿", moment=moment)
                make_item(session, ACC_DEAD, f"必然失败的第 {day} 天的稿", moment=moment)
                # 这条永远不会有人去点确认（P12）
                make_item(session, ACC_UNCONFIRMED, f"没人确认的第 {day} 天的稿", moment=moment)
            # 每天再塞一批"有人手工改过库"的脏排期，喂给发布层的两道闸门
            dirty_slots.update(_inject_tampered(moment, day))

        # -- 生成（受 daily_target 约束）--------------------------------------
        if step % config.generate_every_steps == 0:
            stats = tick_generate(account_ids=[ACC_OK], now=moment)
            result.generated += stats["generated"]
            # 唯一模拟"人"的一步：替运营点批准。排期交给生产代码（core/scheduling.py）
            approved, slots = _approve(moment)
            result.approved += approved
            for account_id, slot in slots:
                result.planned_slots.setdefault(account_id, []).append(slot)

        # -- 确认闸门：推卡 / 补提醒 / TTL 超时驳回（P12）----------------------
        gate = tick_confirm_gate(now=moment)
        result.cards_pushed += gate["pushed"]
        result.confirm_expired += gate["expired"]
        # 第二处模拟人：卡推出来之后逐条点"确认发布"
        result.confirmed += _confirm(moment)

        # -- 定时发布 ---------------------------------------------------------
        stats = tick_scheduled_publish(limiter=limiter, notifier=notifier, now=moment)
        result.published += stats["published"]
        result.skipped_rate += stats["skipped_rate"]
        result.skipped_window += stats["skipped_window"]
        result.skipped_account += stats["skipped_account"]
        result.skipped_unconfirmed += stats["skipped_unconfirmed"]
        _retime(moment, seen_records)

        if stats["published"]:
            # 同一时刻立刻再跑一遍：幂等键 + 状态机应该让它一条都发不出去
            again = tick_scheduled_publish(limiter=limiter, notifier=notifier, now=moment)
            result.duplicate_attempts += again["published"]
            _retime(moment, seen_records)

        # -- 重试 / 死信 ------------------------------------------------------
        retry = tick_retry_sweep(limiter=limiter, notifier=notifier, now=moment)
        result.retry_published += retry["published"]
        result.dead_letters += retry["dead_letter"]
        _retime(moment, seen_records)

        # -- 指标 -------------------------------------------------------------
        if step % config.metrics_every_steps == 0:
            tick_metrics(now=moment, respect_windows=True)

    # -- 尾声：把时钟推到 t0+8 天，让 7d 窗口到期 -----------------------------
    tail = t0 + timedelta(days=config.tail_days)
    tick_metrics(now=tail, respect_windows=True)

    _collect(result, dirty_slots)
    _assert_all(result, publishers)
    return result


def _collect(result: SoakResult, dirty_slots: dict[str, datetime]) -> None:
    """跑完之后从库里读出所有要断言的事实。"""
    from metrics.collector import WINDOW_ORDER, WINDOWS

    result.dirty_injected = len(dirty_slots)
    with db.session_scope() as session:
        policies = {account.id: policy_of(account) for account in session.scalars(select(Account))}
        done_rows = session.execute(
            select(PublishRecord, ContentItem)
            .join(ContentItem, ContentItem.id == PublishRecord.content_item_id)
            .where(PublishRecord.phase == PublishPhase.DONE.value)
        ).all()
        done = [record for record, _item in done_rows]
        result.done_records = len(done)

        published_at: dict[str, datetime] = {}
        account_of: dict[str, str] = {}
        per_account_day: dict[tuple[str, str], int] = {}
        for record, item in done_rows:
            moment = record.updated_at
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=UTC)
            published_at[record.content_item_id] = moment
            account_of[record.content_item_id] = item.account_id
            key = (item.account_id, moment.date().isoformat())
            per_account_day[key] = per_account_day.get(key, 0) + 1

        for (account_id, _day), count in per_account_day.items():
            result.max_daily[account_id] = max(result.max_daily.get(account_id, 0), count)

        result.offline_published = sum(
            1 for item_id in published_at if account_of.get(item_id) == ACC_OFFLINE
        )
        result.unconfirmed_published = sum(
            1 for item_id in published_at if account_of.get(item_id) == ACC_UNCONFIRMED
        )
        _audit_schedule_layer(result, policies)
        _audit_publish_layer(result, policies, published_at, account_of, dirty_slots)
        result.dead_letters = int(
            session.scalar(
                select(func.count())
                .select_from(ContentItem)
                .where(ContentItem.status == ContentStatus.DEAD_LETTER.value)
            )
            or 0
        )

        # 快照按 metrics.collector 的口径归到 24h / 7d
        for snapshot in session.scalars(select(MetricSnapshot)):
            if classify_metrics_payload(snapshot.metrics_json) is not MetricsPayloadKind.USABLE:
                continue
            start = published_at.get(snapshot.content_item_id)
            if start is None:
                continue
            at = snapshot.snapshot_at
            if at.tzinfo is None:
                at = at.replace(tzinfo=UTC)
            label: str | None = None
            for name in WINDOW_ORDER:
                if at >= start + WINDOWS[name]:
                    label = name
            if label == "24h":
                result.snapshots_24h += 1
            elif label == "7d":
                result.snapshots_7d += 1

    notifier = _current_notifier()
    if notifier is not None:
        result.dead_letter_notices = sum(1 for _l, title, _t in notifier.sent if "[死信]" in title)


def _fmt(moment: datetime, policy: AccountPolicy) -> str:
    """账号本地时区的时刻串（违规明细给人看，UTC 串对不上窗口配置）。"""
    return moment.astimezone(policy.tzinfo).strftime("%m-%d %H:%M")


def _audit_schedule_layer(result: SoakResult, policies: dict[str, AccountPolicy]) -> None:
    """核对**排期层**：``schedule_item`` 给出的槽位本身就该是合法的。

    这是重构后的正确性主张，必须有断言盯着——发布层能兜住不代表排期层可以乱排：
    排期时刻同时是运营看到的"这条几点发"，排错了人会当成事实去规划。

    三条约束各自落到一份违规明细上（空 = 合规）。日上限按**账号本地日**算：
    ``daily_limit`` 是运营口径的"一天几条"，跟着账号时区走。
    """
    for account_id, slots in result.planned_slots.items():
        policy = policies.get(account_id)
        if policy is None:  # pragma: no cover - 账号被删，排期层无从谈起
            continue
        ordered = sorted(slots)
        result.slots_out_of_window += [
            f"{account_id} {_fmt(slot, policy)} 不在 {policy.window_text()}"
            for slot in ordered
            if not policy.in_window(slot)
        ]
        result.slot_interval_violations += _too_close(account_id, ordered, policy)

        per_day: dict[str, int] = {}
        for slot in ordered:
            local_day = slot.astimezone(policy.tzinfo).date().isoformat()
            per_day[local_day] = per_day.get(local_day, 0) + 1
        result.slot_daily_overflow += [
            f"{account_id} {day} 排了 {count} 条 > 日上限 {policy.daily_limit}"
            for day, count in sorted(per_day.items())
            if count > policy.daily_limit
        ]


def _audit_publish_layer(
    result: SoakResult,
    policies: dict[str, AccountPolicy],
    published_at: dict[str, datetime],
    account_of: dict[str, str],
    dirty_slots: dict[str, datetime],
) -> None:
    """核对**发布层**：排期数据再脏，真发出去的那些也必须仍然守规矩。

    观测的是结果而不是计数器：``skipped_window`` 只说明闸门被触发过，
    "一条都没在窗口外发出去"才是它真的挡住了。
    """
    per_account: dict[str, list[datetime]] = {}
    for item_id, moment in published_at.items():
        account_id = account_of.get(item_id, "")
        policy = policies.get(account_id)
        if policy is None:  # pragma: no cover - 有外键
            continue
        if not policy.in_window(moment):
            result.published_out_of_window.append(
                f"{account_id} {item_id} 于 {_fmt(moment, policy)} 发出，"
                f"窗口是 {policy.window_text()}"
            )
        per_account.setdefault(account_id, []).append(moment)

    for account_id, moments in per_account.items():
        result.publish_interval_violations += _too_close(
            account_id, sorted(moments), policies[account_id]
        )

    for item_id, slot in dirty_slots.items():
        moment = published_at.get(item_id)
        if moment is None:
            continue  # 一直被闸门挡在外面——正是想看到的
        result.dirty_published += 1
        if moment > slot:
            # 排期时刻发不了，被推到窗口打开 / 额度腾出来之后才发
            result.dirty_deferred += 1

    # 脏数据自检：注入的排期必须**真的**不合法，否则上面几条断言是空转。
    # （比如以后有人把 WINDOW 改成全天，注入的时刻就会突然变得合法）
    tampered = policies.get(ACC_TAMPERED)
    if tampered is not None:
        slots = sorted(dirty_slots.values())
        result.dirty_out_of_window = sum(1 for slot in slots if not tampered.in_window(slot))
        result.dirty_too_close = len(_too_close(ACC_TAMPERED, slots, tampered))


def _too_close(account_id: str, ordered: list[datetime], policy: AccountPolicy) -> list[str]:
    """相邻时刻里间隔小于 ``min_interval`` 的那些（人可读）。"""
    minutes = policy.min_interval.total_seconds() / 60
    return [
        f"{account_id} {_fmt(previous, policy)} → {_fmt(following, policy)}"
        f"（相隔 {(following - previous).total_seconds() / 60:.0f} 分钟 < {minutes:.0f}）"
        for previous, following in pairwise(ordered)
        if following - previous < policy.min_interval
    ]


def _current_notifier() -> LogNotifier | None:
    from core import notify

    default = notify.get_default_notifier()
    return default if isinstance(default, LogNotifier) else None


def _assert_all(result: SoakResult, publishers: dict[str, FakePublisher]) -> None:
    """六组硬断言（见模块 docstring）。"""
    result.publish_calls = {aid: p.publish_calls for aid, p in publishers.items()}

    # 1 无重复发布
    result.check(
        "无重复发布：同一时刻重跑 tick 不产生新发布",
        result.duplicate_attempts == 0,
        f"重跑产生了 {result.duplicate_attempts} 次额外发布",
    )
    with db.session_scope() as session:
        per_item: dict[str, int] = {}
        keys: list[str] = []
        for record in session.scalars(
            select(PublishRecord).where(PublishRecord.phase == PublishPhase.DONE.value)
        ):
            per_item[record.content_item_id] = per_item.get(record.content_item_id, 0) + 1
            keys.append(record.idem_key)
    worst = max(per_item.values(), default=0)
    result.check(
        "无重复发布：每条内容至多一条 done 记录",
        worst <= 1,
        f"{len(per_item)} 条内容中，单条最多有 {worst} 条 done 记录",
    )
    result.check(
        "无重复发布：idem_key 全局唯一",
        len(keys) == len(set(keys)),
        f"{len(keys)} 条记录，对应 {len(set(keys))} 个不同的 idem_key",
    )
    ok_calls = publishers[ACC_OK].publish_calls if ACC_OK in publishers else 0
    ok_done = sum(1 for item_id in per_item if _account_of(item_id) == ACC_OK)
    result.check(
        "无重复发布：正常账号的 publish() 调用次数等于成功发布数",
        ok_calls == ok_done,
        f"publish() 调了 {ok_calls} 次，对应 {ok_done} 条 done 记录",
    )

    # 2 排期层：schedule_item 排出来的槽位本身就合法
    result.check(
        "排期层：批准后确实排上了期",
        result.auto_scheduled > 0,
        f"批准后排上期 {result.auto_scheduled} 条"
        "（0 说明排期层没被驱动，下面几条排期断言会跟着空转）",
    )
    result.check(
        "排期层：批准的内容没有积压在 approved",
        result.approved == result.auto_scheduled,
        f"批准 {result.approved} 条，排上期 {result.auto_scheduled} 条",
    )
    window_detail = (
        f"在排上期的 {result.auto_scheduled} 个槽位中，窗口外的有 "
        f"{len(result.slots_out_of_window)} 个"
    )
    if result.slots_out_of_window:
        window_detail += "：" + "；".join(result.slots_out_of_window)
    result.check(
        "排期层：所有槽位都落在账号发布时段内",
        not result.slots_out_of_window,
        window_detail,
    )
    interval_detail = (
        f"在排上期的 {result.auto_scheduled} 个槽位中，同账号相邻间隔过近的有 "
        f"{len(result.slot_interval_violations)} 对"
    )
    if result.slot_interval_violations:
        interval_detail += "：" + "；".join(result.slot_interval_violations)
    result.check(
        "排期层：同账号相邻槽位间隔不小于 min_interval",
        not result.slot_interval_violations,
        interval_detail,
    )
    overflow_detail = (
        f"在排上期的 {result.auto_scheduled} 个槽位中，单日超额的账号日有 "
        f"{len(result.slot_daily_overflow)} 个"
    )
    if result.slot_daily_overflow:
        overflow_detail += "：" + "；".join(result.slot_daily_overflow)
    result.check(
        "排期层：同账号单日槽位数不超过日上限",
        not result.slot_daily_overflow,
        overflow_detail,
    )

    # 3 发布层：脏排期绕过了排期层，两道闸门仍要挡住（纵深防御）
    result.check(
        "时段窗口生效：窗口外的排期被挡下过",
        result.skipped_window > 0,
        f"窗口外被跳过 {result.skipped_window} 次（0 说明脏数据没注进去，或时段闸门没生效）",
    )
    result.check(
        "限频生效：被日上限 / 最小间隔挡下过",
        result.skipped_rate > 0,
        f"被限频挡下 {result.skipped_rate} 次（0 说明脏数据没注进去，或限频闸门没生效）",
    )
    result.check(
        "发布层：注入的脏排期确实不合法（窗口外 + 挤在同一分钟都有）",
        result.dirty_out_of_window > 0 and result.dirty_too_close > 0,
        f"窗口外 {result.dirty_out_of_window} 条、间隔过近 {result.dirty_too_close} 对"
        "（都得 > 0，否则下面几条断言没有观测对象）",
    )
    result.check(
        "发布层：脏排期没有被整批放行",
        0 < result.dirty_published < result.dirty_injected,
        f"注入 {result.dirty_injected} 条脏排期，发出 {result.dirty_published} 条"
        "（0 = 闸门之外还有别的东西挡着，等于没验；全发 = 闸门没挡住）",
    )
    result.check(
        "发布层：窗口外的脏排期被推迟到合法时刻才发",
        result.dirty_deferred > 0,
        f"推迟发出 {result.dirty_deferred} 条（共发出 {result.dirty_published} 条脏排期，"
        "0 说明发布层没有把它们顶到窗口内）",
    )
    published_window_detail = (
        f"在已发布的 {result.done_records} 条记录中，发布时段外的有 "
        f"{len(result.published_out_of_window)} 条"
    )
    if result.published_out_of_window:
        published_window_detail += "：" + "；".join(result.published_out_of_window)
    result.check(
        "发布层：没有任何一条内容在发布时段外发出去",
        not result.published_out_of_window,
        published_window_detail,
    )
    published_interval_detail = (
        f"在已发布的 {result.done_records} 条记录中，同账号相邻间隔过近的有 "
        f"{len(result.publish_interval_violations)} 对"
    )
    if result.publish_interval_violations:
        published_interval_detail += "：" + "；".join(result.publish_interval_violations)
    result.check(
        "发布层：同账号相邻发布间隔不小于 min_interval",
        not result.publish_interval_violations,
        published_interval_detail,
    )
    over = {
        aid: count
        for aid, count in result.max_daily.items()
        if count > DAILY_LIMITS.get(aid, DAILY_LIMIT)
    }
    over_detail = f"各账号单日最高发布数：{result.max_daily}"
    if over:
        over_detail += f"；超过日上限：{over}"
    result.check(
        "限频生效：任何账号任何一天都没超过日上限",
        not over,
        over_detail,
    )

    # 4 发布前人工确认（P12）：这一道没有旁路，soak 必须亲眼看到它拦住东西
    result.check(
        "发布前确认：没人点就一条都不发",
        result.unconfirmed_published == 0 and result.skipped_unconfirmed > 0,
        f"没确认的发出去了 {result.unconfirmed_published} 条，"
        f"skipped_unconfirmed={result.skipped_unconfirmed}",
    )
    result.check(
        "发布前确认：卡推得出去、人点得动",
        result.cards_pushed > 0 and result.confirmed > 0,
        f"推了 {result.cards_pushed} 张卡、确认了 {result.confirmed} 条",
    )
    result.check(
        "发布前确认：没人点的到 TTL 会自动驳回，不无限堆积",
        result.confirm_expired > 0,
        f"TTL 超时驳回 {result.confirm_expired} 条（跑了 {result.config.days} 天，"
        "0 说明 TTL 没生效或没有内容真正超时）",
    )

    # 5 needs_relogin
    result.check(
        "needs_relogin 账号被跳过",
        result.offline_published == 0 and result.skipped_account > 0,
        f"掉线账号发出去了 {result.offline_published} 条"
        f"（skipped_account={result.skipped_account}）",
    )

    # 6 死信 + 通知
    result.check(
        "死信：必然失败的内容最终进入 dead_letter",
        result.dead_letters > 0,
        f"进入 dead_letter {result.dead_letters} 条（0 说明重试链没有收敛到死信）",
    )
    result.check(
        "死信：触发了通知",
        result.dead_letter_notices > 0,
        f"通知通道里的 [死信] 消息 {result.dead_letter_notices} 条",
    )

    # 7 指标窗口
    result.check(
        "指标快照：24h 窗口至少一份",
        result.snapshots_24h >= 1,
        f"24h 快照 {result.snapshots_24h} 份",
    )
    result.check(
        "指标快照：7d 窗口至少一份",
        result.snapshots_7d >= 1,
        f"7d 快照 {result.snapshots_7d} 份",
    )


def _account_of(item_id: str) -> str:
    with db.session_scope() as session:
        item = session.get(ContentItem, item_id)
        return item.account_id if item is not None else ""


# ---------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="连续运行验证（加速时钟）")
    parser.add_argument("--days", type=int, default=3, help="模拟几天（默认 3）")
    parser.add_argument("--step-minutes", type=int, default=30, help="每步推进几分钟")
    parser.add_argument("--db", type=Path, default=None, help="用指定的 SQLite 文件（默认临时库）")
    parser.add_argument("--keep", action="store_true", help="跑完保留临时库路径不删")
    parser.add_argument("--json", action="store_true", dest="as_json", help="输出 JSON")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.as_json else logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
    )

    tmpdir: tempfile.TemporaryDirectory[str] | None = None
    if args.db is not None:
        path = args.db
        path.parent.mkdir(parents=True, exist_ok=True)
    else:
        tmpdir = tempfile.TemporaryDirectory(prefix="soak-")
        path = Path(tmpdir.name) / "soak.db"

    config = SoakConfig(days=args.days, step_minutes=args.step_minutes)
    try:
        with soak_env(SW_DATABASE_URL=f"sqlite:///{path}"):
            db.configure(f"sqlite:///{path}")
            db.init_db()
            result = run_soak(config)
    finally:
        set_default_notifier(None)
        reset_registry()
        if tmpdir is not None and not args.keep:
            tmpdir.cleanup()

    if args.as_json:
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    else:
        _render(result, path if args.keep or args.db else None)
    return 0 if result.ok else 1


def _render(result: SoakResult, path: Path | None) -> None:
    print(
        f"\n模拟 {result.config.days} 天 · 每步 {result.config.step_minutes} 分钟 "
        f"· 共 {result.config.steps} 步（起点 {result.started_at.isoformat()}）"
    )
    print(
        f"生成 {result.generated} · 发布 {result.published} · 重试成功 {result.retry_published} "
        f"· 死信 {result.dead_letters}"
    )
    slot_violations = (
        len(result.slots_out_of_window)
        + len(result.slot_interval_violations)
        + len(result.slot_daily_overflow)
    )
    print(
        f"排期层：批准 {result.approved} · 自动排期 {result.auto_scheduled} "
        f"· 槽位违规 {slot_violations}"
    )
    print(
        f"发布层：注入脏排期 {result.dirty_injected} · 其中发出 {result.dirty_published}"
        f"（推迟发出 {result.dirty_deferred}）· 窗口外发出 {len(result.published_out_of_window)}"
    )
    print(
        f"跳过：限频 {result.skipped_rate} · 时段 {result.skipped_window} "
        f"· 账号不健康 {result.skipped_account}"
    )
    print(f"指标快照：24h {result.snapshots_24h} 份 · 7d {result.snapshots_7d} 份")
    print(f"各账号单日最高发布数：{result.max_daily}")
    print(f"publish() 调用次数：{result.publish_calls}\n")
    for name, ok, detail in result.checks:
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}" + (f" —— {detail}" if not ok else ""))
    print()
    if path is not None:
        print(f"库留在 {path}")
    print("全部断言通过" if result.ok else f"{len(result.failures)} 条断言失败")


if __name__ == "__main__":
    raise SystemExit(main())
