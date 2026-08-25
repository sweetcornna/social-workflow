"""APScheduler 全链路定时任务（P4 补齐，P12 加确认闸门）。

九个 tick，全部**幂等**、全部可单独触发（``POST /dev/tick/{name}``）：

===================== ======== ==========================================
job                   频率     职责
===================== ======== ==========================================
``tick_sourcing``     6 小时   拉热榜（newsnow / douyin-hot-hub / TrendRadar）→ 去重入库
``tick_generate``     30 分钟  按账号 ``daily_target`` 选题 → 生成 → 机器审核 →
                               （``autopilot`` 开时）自动批准排期，否则进人工队列
``tick_scheduled_publish`` 1 分钟 只发 ``scheduled`` 且账号 ``ok`` 的项，受限频、
                               时段窗口与**人工确认**约束
``tick_confirm_gate`` 1 分钟   推确认卡 / 槽位前补一次提醒 / TTL 超时自动驳回
``tick_retry_sweep``  5 分钟   ``retrying`` 项按指数退避重投；超龄进死信并告警；
                               顺带回收两类崩溃残留（卡在 ``publishing`` 的、
                               入库了却没审过的 draft）
``tick_metrics``      6 小时   24h / 7d 指标快照（只追加）
``tick_login_health`` 10 分钟  登录态巡检（抖音在函数内另有 30 分钟节流）
``tick_render_jobs``  1 分钟   轮询 MPT 渲染任务，成片补挂回内容包
``tick_insights``     6 小时   复盘 Agent（内部按账号 24 小时节流）
===================== ======== ==========================================

三条贯穿所有 tick 的规则
------------------------
1. **只认 DB 里的账号**。``accounts.yaml`` 要先 ``python -m core.accounts sync``
   才会生效——否则调度器看不见它。
2. **按账号健康过滤**。``needs_relogin`` / ``banned`` 的账号在发布与重试两条路上
   都被跳过。这修掉了 P3 遗留的一个洞：``NeedsReloginError`` 会把内容项打到
   ``retrying``，而 ``mark_account_needs_relogin`` 只挂起 ``scheduled`` 的项，
   ``retrying`` 的那条不在挂起范围内，重试扫描会一遍遍去撞一个掉线的号。
3. **限频真相在 DB**（``core/ratelimit.py``）。进程内计数只是缓存，重启不会清零。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.accounts import AccountPolicy, policy_of
from core.db import session_scope
from core.models import Account, ContentItem, PublishRecord, utcnow
from core.notify import Notifier, get_default_notifier, notify_event
from core.ratelimit import RATE_LIMITER, RateLimiter
from core.ratelimit import local_day_start as account_local_day_start
from core.state_machine import (
    AccountStatus,
    ContentStatus,
    SystemAction,
    log_review,
    publish_with_idempotency,
    transition,
)
from publishers.base import PublisherNotAvailable, PublishError
from publishers.registry import get_publisher

if TYPE_CHECKING:  # 只为类型标注；运行期不把 generation（连带 anthropic SDK）拉进调度器
    from generation.mpt_client import MptClient

logger = logging.getLogger("social_workflow.scheduler")

#: 能自动发布的账号状态。``degraded`` 也放行——它多半只是 sidecar 抖了一下，
#: 真发不出去会走异常分类，比"因为一次探活失败就全线停摆"好
PUBLISHABLE_ACCOUNT_STATUSES = frozenset({AccountStatus.OK.value})
#: 能自动生成内容的账号状态。``needs_relogin`` 排除在外：发不出去就别烧 token
GENERATABLE_ACCOUNT_STATUSES = frozenset({AccountStatus.OK.value, AccountStatus.DEGRADED.value})


# --------------------------------------------------------------------- 小工具


def _now(now: datetime | None) -> datetime:
    return now or utcnow()


def local_day_start(policy: AccountPolicy, now: datetime) -> datetime:
    """账号所在时区的"今天零点"（返回 UTC）。

    ``daily_target`` 是"一天出几条"这种人的概念，按 UTC 切会在北京时间早上 8 点
    换日，运营看着莫名其妙。

    实现落在 ``core.ratelimit``：那里还有限频的日界，两处各写一遍迟早会漂
    （P11.3 修的正是"分桶按本地日、计数按 UTC 日"这种漂移）。
    """
    return account_local_day_start(policy.timezone, now)


def latest_record(session: Session, item_id: str) -> PublishRecord | None:
    """该内容最近一条发布记录（重试用它算退避与年龄）。"""
    return session.scalars(
        select(PublishRecord)
        .where(PublishRecord.content_item_id == item_id)
        .order_by(PublishRecord.created_at.desc())
        .limit(1)
    ).first()


def backoff_for(attempts: int) -> timedelta:
    """指数退避 ``base * 2^(attempts-1)``，封顶 ``SW_RETRY_BACKOFF_MAX_SECONDS``。"""
    from core.config import get_settings

    settings = get_settings()
    base = max(settings.sw_retry_backoff_base_seconds, 0)
    cap = max(settings.sw_retry_backoff_max_seconds, base)
    if attempts <= 1:
        return timedelta(seconds=base)
    # 2**30 就已经溢出任何合理的 cap，先夹指数免得算大数
    shift = min(attempts - 1, 30)
    return timedelta(seconds=min(base * (2**shift), cap))


def _publishable(account: Account | None) -> bool:
    return account is not None and account.status in PUBLISHABLE_ACCOUNT_STATUSES


# ------------------------------------------------------------------ 选题采集


def tick_sourcing(
    *,
    sources: Sequence[str] | None = None,
    notifier: Notifier | None = None,
) -> dict[str, int]:
    """拉热榜并去重入库。单个源不可用不阻断，全部不可用才告警。

    统计键：``fetched`` / ``created`` / ``sources_ok`` / ``sources_failed``。
    """
    from sourcing.collector import SOURCES, collect

    notifier = notifier or get_default_notifier()
    with session_scope() as session:
        result = collect(session, sources=sources)

    stats = {
        "fetched": result.total_fetched,
        "created": result.created,
        "sources_ok": len(result.fetched),
        "sources_failed": len(result.warnings),
    }
    if not result.fetched:
        # 一条都没拉到 = 选题池会枯竭 = 明天没稿可发，必须让人知道。
        # 走节流层：源挂了通常一挂几小时，每轮推一次没有新信息
        notify_event(
            "[选题采集] 所有数据源都不可用",
            "\n".join(result.warnings) or f"已注册的源：{sorted(SOURCES)}",
            kind="sourcing_down",
            level="warning",
            notifier=notifier,
        )
    return stats


# ------------------------------------------------------------------ 内容生成


#: platform → (pipeline 函数名, 构造 options 的函数)。延迟 import，别把 anthropic
#: 拉进控制面的启动路径
_PIPELINES: dict[str, str] = {
    "wechat_mp": "run_wechat_pipeline",
    "xhs": "run_xhs_pipeline",
    "douyin": "run_douyin_pipeline",
}


def _generation_options(platform: str) -> Any:
    """按平台构造生成参数（无 Playwright / Node / MPT 的机器上可整体关掉）。"""
    from core.config import get_settings

    settings = get_settings()
    media = settings.sw_generate_make_media
    # 不出图就别生图：底图/配图都要落进 media，关掉 media 时生成了也没处挂
    illustrations = settings.sw_generate_illustrations if media else 0
    if platform == "wechat_mp":
        from generation.pipeline import GenerationOptions

        return GenerationOptions(
            make_cover=media, render_html=media, illustrations=min(illustrations, 1)
        )
    if platform == "xhs":
        from generation.pipeline import XhsGenerationOptions

        return XhsGenerationOptions(make_cards=media, illustrations=illustrations)
    from generation.video_pipeline import VideoGenerationOptions

    return VideoGenerationOptions(
        make_cover=media,
        skip_render=settings.sw_generate_skip_render,
        illustrations=min(illustrations, 1),
    )


def produced_today(session: Session, account: Account, policy: AccountPolicy, now: datetime) -> int:
    """该账号今天（本地时区）已经产出几条稿。

    统计**创建**而不是"已发布"：``daily_target`` 管的是生成侧的产能，
    发布节奏另有限频与时段窗口在管。死信不计——那条稿等于没产出。
    """
    day_start = local_day_start(policy, now)
    return int(
        session.scalar(
            select(func.count())
            .select_from(ContentItem)
            .where(
                ContentItem.account_id == account.id,
                ContentItem.created_at >= day_start,
                ContentItem.status != ContentStatus.DEAD_LETTER.value,
            )
        )
        or 0
    )


def tick_generate(
    *,
    account_ids: Sequence[str] | None = None,
    platforms: Sequence[str] | None = None,
    llm: Any | None = None,
    now: datetime | None = None,
    max_items: int | None = None,
    notifier: Notifier | None = None,
) -> dict[str, int]:
    """按账号的 ``daily_target`` 补齐当天产出。

    流程与 ``/dev/run_*_pipeline`` **完全同一批函数**（``core.dev_flow``），
    区别只在于这里跳过采集（``tick_sourcing`` 已经拉过）、受 ``daily_target``
    与成本闸门约束、并且逐账号独立事务。

    **autopilot（P12）**：账号策略里 ``autopilot: true`` 时，机器审核干净
    （``block == 0 且 warn == 0``）的稿子会自动批准并排期，随后推一张确认卡等人点。
    有任何 block/warn 命中的一律**不自动批准**，留在审核台并推一条（限流的）提醒。
    自动批准复用与人工完全同一批函数，``ReviewLog.actor`` 记 ``autopilot``。

    统计键：``scanned`` / ``generated`` / ``failed`` / ``skipped_no_target``
    / ``skipped_target_met`` / ``skipped_account`` / ``skipped_budget``
    / ``autopilot_approved`` / ``autopilot_scheduled`` / ``autopilot_held``。
    """
    from core.budget import BudgetGuard, CostKind
    from core.config import get_settings
    from core.confirm import autopilot_approve, autopilot_enabled
    from core.db import get_session_factory
    from core.dev_flow import DevFlowError
    from core.models import ContentItem as _ContentItem

    settings = get_settings()
    notifier = notifier or get_default_notifier()
    moment = _now(now)
    budget_cap = max_items if max_items is not None else settings.sw_generate_max_per_tick
    stats = {
        "scanned": 0,
        "generated": 0,
        "failed": 0,
        "skipped_no_material": 0,
        "skipped_no_target": 0,
        "skipped_target_met": 0,
        "skipped_account": 0,
        "skipped_budget": 0,
        "autopilot_approved": 0,
        "autopilot_scheduled": 0,
        "autopilot_held": 0,
    }
    if not settings.sw_generate_enabled:
        logger.info("SW_GENERATE_ENABLED=false，tick_generate 空转")
        return stats

    # 先在一个只读事务里挑出该做的账号，别把长事务跨到生成链上（LLM 要跑几十秒）
    with session_scope() as session:
        stmt = select(Account).order_by(Account.platform, Account.id)
        if account_ids:
            stmt = stmt.where(Account.id.in_(list(account_ids)))
        if platforms:
            stmt = stmt.where(Account.platform.in_(list(platforms)))
        targets = [(a.id, a.platform) for a in session.scalars(stmt)]

    budget_notified = False
    produced = 0
    for account_id, platform in targets:
        if produced >= budget_cap:
            break
        stats["scanned"] += 1
        if platform not in _PIPELINES:
            stats["skipped_account"] += 1
            continue

        session = get_session_factory()()
        try:
            account = session.get(Account, account_id)
            if account is None or account.status not in GENERATABLE_ACCOUNT_STATUSES:
                stats["skipped_account"] += 1
                continue
            policy = policy_of(account)
            if policy.daily_target <= 0:
                stats["skipped_no_target"] += 1
                continue
            done = produced_today(session, account, policy, moment)
            if done >= policy.daily_target:
                stats["skipped_target_met"] += 1
                continue
            if BudgetGuard(session).is_exhausted(CostKind.TOKENS):
                # 计划 2.2 的降级口径：只出选题不出稿
                stats["skipped_budget"] += 1
                if not budget_notified:
                    budget_notified = True
                    # 本轮内用 budget_notified 去重，跨轮由节流层管（预算耗尽会持续到次日）
                    notify_event(
                        "[成本闸门] 当日 token 预算已耗尽",
                        "tick_generate 本轮不再出稿；调 DAILY_TOKEN_BUDGET 或等 UTC 次日重置。",
                        kind="budget_tokens",
                        level="warning",
                        notifier=notifier,
                    )
                continue

            from core import dev_flow

            runner = getattr(dev_flow, _PIPELINES[platform])
            try:
                result = runner(
                    session,
                    account,
                    llm=llm,
                    skip_sourcing=True,  # 采集是 tick_sourcing 的事
                    options=_generation_options(platform),
                )
            except DevFlowError as exc:
                # 选题池空 / 今天没值得写的 / 预算耗尽都是预期内的，不是故障。
                # **刻意不记进 failed**：它和"生成链抛异常"是两件事，压成一个数
                # 之后看板上长得一模一样，看到 failed=3 的人无法判断该去配
                # NEWSNOW_BASE_URL 还是该去看 traceback。日志级别（INFO vs
                # WARNING）早就分开了，计数口径也得跟上。
                session.rollback()
                logger.info("生成跳过 account=%s: %s", account_id, exc)
                stats["skipped_no_material"] += 1
                continue
            except Exception as exc:  # 生成链内部异常不该拖垮整个 tick
                session.rollback()
                logger.warning("生成失败 account=%s: %s", account_id, exc, exc_info=True)
                stats["failed"] += 1
                notify_event(
                    f"[生成失败] {account_id}",
                    f"{type(exc).__name__}: {exc}",
                    kind="generate_failed",
                    account_id=account_id,
                    level="error",
                    notifier=notifier,
                )
                continue

            logger.info(
                "生成完成 account=%s item=%s 选题=%r 审核 passed=%s（今日 %d/%d）",
                account_id,
                result.content_item_id,
                result.selected_topic,
                result.review_passed,
                done + 1,
                policy.daily_target,
            )

            # -- autopilot：机器审核干净就自动批准 + 排期 + 推确认卡 ----------
            outcome = None
            if autopilot_enabled(policy) and result.content_item_id:
                item = session.get(_ContentItem, result.content_item_id)
                if item is not None:
                    outcome = autopilot_approve(
                        session,
                        item,
                        policy=policy,
                        blocking=result.review_blocking,
                        warnings=result.review_warning,
                        notifier=notifier,
                        now=moment,
                    )
                    if outcome.approved:
                        stats["autopilot_approved"] += 1
                        if outcome.scheduled:
                            stats["autopilot_scheduled"] += 1
                    else:
                        stats["autopilot_held"] += 1

            session.commit()
            produced += 1
            stats["generated"] += 1
            if outcome is None or not outcome.approved:
                # 只有还需要人去审核台点的时候才推"待审核"——autopilot 批过的
                # 会另有一张确认卡，两条都推等于同一条内容打扰两次
                notifier.send(
                    title=f"[待审核] {account_id}",
                    text=(
                        f"{result.selected_topic or '（未知选题）'}\n"
                        f"/review/{result.content_item_id}"
                    ),
                    level="info",
                )
        finally:
            session.close()
    return stats


# ------------------------------------------------------------------ 定时发布


def tick_scheduled_publish(
    *,
    limiter: RateLimiter | None = None,
    notifier: Notifier | None = None,
    now: datetime | None = None,
    batch_size: int | None = None,
) -> dict[str, int]:
    """扫到期的 ``scheduled`` 项并发布。

    六道闸门 = 循环里六个"这条这一轮不发"的分支，顺序不能换（越便宜的越先判）：

    1. 账号健康：只发 ``ok`` 的号；
    2. 发布时段窗口 ``Account.extra['publish_windows']``；
    3. 按账号限频（日上限 + 最小间隔，真相在 DB）；
    4. **人工确认**（P12）：账号策略要求确认而 ``confirmed_at is None`` 就不发，
       顺手补推一次确认卡（``ensure_confirm_pushed`` 自带去重）。
       这一道**没有旁路**——``autopilot`` 打开也只影响"自动批准"，不影响"发布前
       要人点"（小红书 2026-03 公告封禁 AI 全托管账号，见 docs/POLICY.md）；
    5. 发布器可用：该平台的发布器没注册（``PublisherNotAvailable``）就不发；
    6. ``publish_with_idempotency`` 返回了但状态没推进到 ``published``——dry-run
       发布器，或 publisher 违反契约返回 ``ok=False``。**防御性守卫，两条成因
       当前都不可达**，理由见该分支注释。

    统计键：``scanned`` / ``published`` / ``skipped`` / ``failed``，外加六个
    ``skipped_*`` 明细（``account`` / ``window`` / ``rate`` / ``unconfirmed``
    / ``publisher`` / ``not_advanced``），与六道闸门一一对应，每道都加
    ``skipped``。所以 ``scanned == published + skipped + failed`` **恒成立**。
    ``PublishError`` 不是闸门，是失败，计 ``failed`` 并走重试。
    """
    from core.config import get_settings
    from core.confirm import confirm_required, ensure_confirm_pushed

    limiter = limiter or RATE_LIMITER
    notifier = notifier or get_default_notifier()
    moment = _now(now)
    limit = batch_size if batch_size is not None else get_settings().sw_publish_batch_size
    stats = {
        "scanned": 0,
        "published": 0,
        "skipped": 0,
        "failed": 0,
        "skipped_account": 0,
        "skipped_window": 0,
        "skipped_rate": 0,
        "skipped_unconfirmed": 0,
        "skipped_publisher": 0,
        "skipped_not_advanced": 0,
    }

    with session_scope() as session:
        stmt = (
            select(ContentItem)
            .where(
                ContentItem.status == ContentStatus.SCHEDULED.value,
                ContentItem.scheduled_at.is_not(None),
                ContentItem.scheduled_at <= moment,
            )
            .order_by(ContentItem.scheduled_at)
            .limit(limit)
        )
        for item in session.scalars(stmt):
            stats["scanned"] += 1
            account = session.get(Account, item.account_id)
            if not _publishable(account):
                # ①：needs_relogin / banned 的号一条都不发
                stats["skipped"] += 1
                stats["skipped_account"] += 1
                continue
            assert account is not None
            policy = policy_of(account)

            if not policy.in_window(moment):
                stats["skipped"] += 1
                stats["skipped_window"] += 1
                logger.debug(
                    "不在发布时段 item=%s account=%s 窗口=%s",
                    item.id,
                    account.id,
                    policy.window_text(),
                )
                continue

            reason = limiter.deny_reason(
                account.id,
                policy.daily_limit,
                now=moment,
                session=session,  # 用本事务的 session：同一 tick 里刚发的也算数
                min_interval=policy.min_interval,
                # 日上限按**账号本地日**计（P11.3）：窗口按本地日排，计数就得跟齐，
                # 否则横跨 UTC 午夜缝的窗口里 daily_limit 拦不住第二条
                timezone=policy.timezone,
            )
            if reason is not None:
                stats["skipped"] += 1
                stats["skipped_rate"] += 1
                logger.info("限频跳过 item=%s account=%s %s", item.id, account.id, reason)
                continue

            if confirm_required(policy) and item.confirmed_at is None:
                # ④：人这一票还没投。到点了还没推过卡就补推一次——不然内容会
                # 在这里静默停住，人根本不知道系统在等他
                stats["skipped"] += 1
                stats["skipped_unconfirmed"] += 1
                ensure_confirm_pushed(session, item, notifier=notifier, now=moment)
                logger.info("等人工确认，跳过 item=%s account=%s", item.id, account.id)
                continue

            try:
                publisher = get_publisher(account.platform, account.id)
            except PublisherNotAvailable as exc:
                logger.warning("跳过 %s：%s", item.id, exc)
                stats["skipped"] += 1
                stats["skipped_publisher"] += 1
                continue
            try:
                publish_with_idempotency(
                    session, item, publisher, notifier=notifier, account=account
                )
            except PublishError as exc:
                logger.warning("发布失败 %s: %s", item.id, exc)
                stats["failed"] += 1
                continue
            if item.status != ContentStatus.PUBLISHED.value:
                # ⑥：没抛异常，但状态机也没推进到 published。这是一道**防御性守卫**，
                # 两条成因**当前都不可达**：
                #   · dry-run 发布器提前返回（publish_with_idempotency 的 dry_run
                #     分支有意不碰状态机）。不可达是因为没人把 dry_run 打开：
                #     register_builtin_publishers 的三个工厂与 _fake_factory 都不传
                #     它，Publisher.__init__ 默认 False；
                #   · publisher 违反 publishers/base.py 的 publish 契约（"失败必须抛
                #     PublishError，不允许返回 ok=False"），被 state_machine 的
                #     `if not result.ok` 兜底打到 retrying / dead_letter 后正常 return。
                #     它不可达要由两条事实**合起来**给出，缺一不可（不是各自独立的两条
                #     证明）：(1) 现有五个发布器里所有 PublishResult(ok=False) 都在各自的
                #     `if self.dry_run:` 分支内，非 dry-run 路径上一条都没有 →「拿到
                #     ok=False」蕴含「dry_run 为真」；(2) publish_with_idempotency 在
                #     `if publisher.dry_run:` 处提前 return →「dry_run 为真」蕴含「走不到
                #     那道兜底」。合起来才有：走到兜底 → 非 dry-run → 只可能 ok=True，即
                #     "返回 ok=False"与"能走到兜底"互斥。2026-08-23 更正：此处原写作"两个
                #     各自独立的理由"，那个说法不成立——只有 (1) 时，一个 dry-run 发布器
                #     照样能把 ok=False 送到那一行；只有 (2) 时，它自己就只覆盖 dry-run
                #     路径，对非 dry-run 一句话没说。见 docs/RISKS.md 第 13 条。
                # 那为什么还要计数：将来新增或第三方发布器破了契约，这里至少不会
                # 静默。没有真的发生状态推进，不能算一次发布，也不占用限频 token
                # （P16.1：计数只认状态推进）；但必须计入 skipped，否则 scanned 与
                # published + skipped + failed 对不上，一条内容会凭空消失在 stats 里。
                # status 进日志，用来区分上面两条成因。见 docs/RISKS.md 第 13 条。
                stats["skipped"] += 1
                stats["skipped_not_advanced"] += 1
                logger.info(
                    "状态未推进到 published，跳过 item=%s account=%s status=%s",
                    item.id,
                    account.id,
                    item.status,
                )
                continue
            # token 与发布器内部记账去重（见 RateLimiter.record），一次发布只计一次
            limiter.record(
                account.id,
                now=moment,
                token=f"{account.platform}:{item.id}",
                timezone=policy.timezone,  # 进程内计数与 DB 用同一条本地日界
            )
            stats["published"] += 1
    return stats


# ------------------------------------------------------------------ 重试 / 死信

#: "卡在 ``publishing`` 多久算没人管了"= **最慢平台的单次发布超时 × 这个倍数**。
#:
#: 倍数而不是一个常数，因为这个阈值唯一不能犯的错是**取小**：取小会把**正在正常
#: 发布**的内容扫成失败，而重试链的第一步是 ``publisher.reconcile``——平台侧那一刻
#: 多半还没有结果可查，于是会真的重发一次。取大只是多卡一会儿。
#:
#: 取 3 的依据：一次 ``publish()`` 不是一个请求，是"传素材 → 提交 → 等页面跳转/轮询
#: 状态"若干个请求，每个各自受那条超时约束（抖音上传成片尤其明显，见
#: ``douyin_publish_timeout_seconds`` 的注释）。单条超时只是**其中一步**的上界，不是
#: 整个 ``publish()`` 的上界，所以必须留倍数余量。3 之后再由下面的下限兜一道。
STALE_PUBLISHING_TIMEOUT_FACTOR = 3

#: 阈值下限（秒）。防的是"有人把所有平台超时都调到很小"——那时倍数算出来的阈值会
#: 跟着变小，而进程崩溃与重启的时间尺度跟平台超时没关系。30 分钟 = 最密的 tick
#: （1 分钟一轮）的 30 倍，也远大于一次进程重启
STALE_PUBLISHING_MIN_SECONDS = 1_800


def stale_publishing_after(now: datetime | None = None) -> timedelta:
    """``publishing`` 停留超过这个时长就按"崩溃残留"处理。

    从**配置里真实存在的那几条发布超时**推出来，不另开配置键：新增/调大某个平台的
    发布超时，这个阈值自动跟着涨，不会漂成两个数。默认值下 =
    ``max(1200, 300, 120, 120) * 3 = 3600`` 秒。

    ``tests/test_stale_publishing.py`` 有一条用例钉住"阈值必须大于每一条已配置的发布
    超时"——这是它唯一不能犯的错。
    """
    from core.config import get_settings

    settings = get_settings()
    slowest = max(
        settings.douyin_publish_timeout_seconds,  # 1200：上传成片 + 平台转码
        settings.xhs_publish_timeout_seconds,  # 300：浏览器自动化传图
        settings.wechat_publish_poll_timeout,  # 120：freepublish 轮询
        settings.wenyan_timeout_seconds,  # 120：wenyan CLI 子进程
    )
    return timedelta(
        seconds=max(slowest * STALE_PUBLISHING_TIMEOUT_FACTOR, STALE_PUBLISHING_MIN_SECONDS)
    )


#: 机器审核里"要不要开商业规则（极限词 / 效果承诺等）"的平台口径。与
#: ``core/dev_flow.py`` 的三条生成链保持一致：小红书与抖音传 ``commercial=True``，
#: 公众号用 ``ReviewOptions`` 的默认值。补跑的那一遍必须和正常那一遍同口径，
#: 否则同一条内容在审核台上会因为"谁审的"而结论不同
COMMERCIAL_REVIEW_PLATFORMS = frozenset({"xhs", "douyin"})

#: "入库了却一直没审"多久算没人管了 = **一次机器审核里最慢的那步 × 这个倍数**。
#: 那一步是 ``review.semantic`` 的 LLM 调用（``llm_timeout_seconds``，默认 600 秒；
#: 回落是**服务端**做的，仍在同一个请求的超时内，不存在客户端重试把时间翻几倍）。
#: 倍数留给 precheck 规则加载与 ``inspect`` 读媒体文件那些本地开销。
STALE_REVIEW_TIMEOUT_FACTOR = 3

#: 阈值下限（秒）。30 分钟 = ``tick_generate`` 的默认间隔：比它还短的话，一轮生成
#: 还没跑完就可能被判成"没人管了"。也远大于一次进程重启
STALE_REVIEW_MIN_SECONDS = 1_800


def stale_review_after() -> timedelta:
    """``draft`` 且从没审过，停留超过这个时长就补跑一遍机器审核。

    和 :func:`stale_publishing_after` 同一套写法但**不共用常数**：那个的输入是各平台
    的发布超时，这个的输入是 LLM 超时，两个数没有任何理由绑在一起。默认值下 =
    ``max(600 * 3, 1800) = 1800`` 秒。
    """
    from core.config import get_settings

    slowest = float(get_settings().llm_timeout_seconds)
    return timedelta(seconds=max(slowest * STALE_REVIEW_TIMEOUT_FACTOR, STALE_REVIEW_MIN_SECONDS))


def recover_stale_drafts(
    *,
    notifier: Notifier | None = None,
    now: datetime | None = None,
    max_items: int = 20,
) -> int:
    """给"入库了却从来没跑过机器审核"的 draft 补一遍审核。返回补了几条。

    为什么需要它（P16.3）：``core/dev_flow.py`` 的 ``_persist_and_review`` 在跑机器
    审核**之前**会把 draft commit 掉——为了交出写锁，也为了别把已经烧过 token 的稿子
    跟着审核异常一起回滚。代价是审核中途崩掉会留下一条 ``review_notes`` 为空的 draft。
    它**不是完全看不见**（``REVIEW_QUEUE_STATUSES`` 含 ``draft``，审核台和
    ``pending_review`` 计数都会带上它），但：没有任何机器结论可看、autopilot 只在
    ``tick_generate`` 那一轮里动它所以永远不会再碰、也没有任何东西报警。这个函数是它
    的出口。

    **补跑刻意关掉 LLM 语境判定**（``use_llm=False``）：这个 sweeper 跑在一个 DB 事务
    里，往里面塞一次 600 秒的 LLM 调用等于把这一整轮改动修掉的 bug 原样种回来。离线
    那几级（precheck 规则 + ``inspect`` 结构校验）不花钱、不出网、跑完就把内容送回正常
    的人工审核流。缺掉的那一档记在审计日志的 ``stages_skipped`` 里，通知与审计原因也都
    明说了，让人知道要多看一眼。

    判据：``status=draft`` **且** ``review_notes`` 为空 **且** ``updated_at`` 早于
    :func:`stale_review_after`。第二条是关键——审过的 draft（含被驳回的、机器审出
    block 的）``review_notes`` 都不为空，不会被反复补跑；补完一遍之后它自己也就不再
    命中这个判据了（幂等）。

    **不是所有命中的都一定是崩溃残留**：``/dev/seed`` 这类直接塞一条 draft 进来的路径
    同样没有 ``review_notes``，也会被补跑。所以审计原因与通知的措辞只陈述可验证的事实
    ——"这条 draft 从来没跑过机器审核"——而不是断言"进程崩过"。
    """
    from review.pipeline import ReviewOptions, review_item

    notifier = notifier or get_default_notifier()
    moment = _now(now)
    deadline = moment - stale_review_after()
    recovered_ids: list[str] = []

    with session_scope() as session:
        stmt = (
            select(ContentItem)
            .where(
                ContentItem.status == ContentStatus.DRAFT.value,
                # 空串和 NULL 都算"没审过"：审过的一定写得出一行摘要
                func.coalesce(ContentItem.review_notes, "") == "",
            )
            .order_by(ContentItem.updated_at)
            .limit(max_items)
        )
        for item in session.scalars(stmt):
            touched = item.updated_at
            if touched.tzinfo is None:
                touched = touched.replace(tzinfo=UTC)
            if touched > deadline:
                continue  # 还在阈值内，八成正审着呢

            platform = str((item.bundle_json or {}).get("platform") or "")
            try:
                result = review_item(
                    session,
                    item,
                    llm=None,
                    options=ReviewOptions(
                        use_llm=False,
                        commercial=platform in COMMERCIAL_REVIEW_PLATFORMS,
                    ),
                    actor="system",
                )
            except Exception as exc:  # 一条坏内容不该把整个 tick 拖垮
                session.rollback()
                logger.warning("补跑机器审核失败 item=%s: %s", item.id, exc, exc_info=True)
                continue

            log_review(
                session,
                item,
                actor="system",
                action=SystemAction.REVIEW_MISSING,
                reason=(
                    "这条 draft 入库后从来没跑过机器审核（review_notes 为空），已补跑一遍。"
                    "**缺 LLM 语境判定那一档**（补跑刻意不出网，见 recover_stale_drafts），"
                    "所以机器结论比正常那一遍弱，请人工多看一眼。"
                    "常见成因是生成链在机器审核中途退出（被杀 / OOM / 重启）；"
                    f"也可能是 /dev/seed 之类直接塞进来的草稿。本次 block={len(result.blocking)} "
                    f"warn={len(result.warnings)}"
                ),
            )
            # 逐条落定：一条一条 commit，坏内容的回滚不会牵连已经补好的
            session.commit()
            logger.warning("已补跑机器审核 item=%s passed=%s", item.id, result.passed)
            recovered_ids.append(item.id)

    if recovered_ids:
        notify_event(
            f"[漏审] {len(recovered_ids)} 条 draft 从没跑过机器审核，已离线补跑",
            (
                f"{', '.join(recovered_ids[:5])}{' …' if len(recovered_ids) > 5 else ''}\n"
                "补跑**不含 LLM 语境判定**，机器结论比正常那一遍弱，审核台上请多看一眼。"
                "常见成因是生成链在机器审核中途退出（被杀 / OOM / 重启）。"
                "反复出现就去查进程为什么退出。"
            ),
            kind="stale_review",
            level="warning",
            notifier=notifier,
            now=moment,
        )
    return len(recovered_ids)


def recover_stale_publishing(
    *,
    notifier: Notifier | None = None,
    now: datetime | None = None,
    max_items: int = 20,
) -> int:
    """把**崩溃残留**在 ``publishing`` 的内容推回重试链。返回捞回来几条。

    为什么需要它（P16.2）：``publish_with_idempotency`` 在碰平台之前会把
    ``PublishRecord(phase=in_flight)`` 与 ``item.status=publishing`` **commit** 掉——
    这是为了让幂等声明真的落盘，崩溃后不至于重复发布（见
    ``core/state_machine.py`` 的 ``_commit_before_platform``）。代价是进程真的在
    publish 中途没了的话，这条内容会停在 ``publishing``，而**没有任何 tick 扫这个
    状态**：``tick_scheduled_publish`` 只扫 ``scheduled``、下面的重投只扫
    ``retrying``。那就是一次**静默的流水线停摆**——没人会发现。这个函数是那条状态
    的唯一出口。

    **不会造成重复发布**，这一点是本函数存在的前提，不是顺带的性质：推回
    ``retrying`` 之后走的是现成的重投链，而重投链看到幂等键已存在（``is_retry``）
    会**先**调 ``publisher.reconcile`` 做平台侧对账——"其实发成功了但回包丢了"正是
    它的设计用途。对上了就直接认已发布，``publish`` 一次都不会再调。
    ``tests/test_stale_publishing.py::test_recovered_item_reconciles_instead_of_republishing``
    钉住这条。

    判据只用**时间**：``publishing`` 且发布记录（没有记录就退回内容本身）的
    ``updated_at`` 早于 :func:`stale_publishing_after` 给的阈值。不用别的信号，因为
    "这个进程还活着吗"在单库多进程下没有可靠的本地答案，而时间是所有部署形态都认的。

    单开一个 ``session_scope`` 跑在重投扫描**之后**：捞回来的这条的
    ``record.updated_at`` 刚被改写，本轮扫到也只会 ``skipped_backoff``，白让
    ``scanned`` 虚增一格。下一轮（5 分钟后）退避到点自然接上。
    """
    from core.state_machine import PublishPhase

    notifier = notifier or get_default_notifier()
    moment = _now(now)
    deadline = moment - stale_publishing_after()
    recovered = 0
    recovered_ids: list[str] = []

    with session_scope() as session:
        stmt = (
            select(ContentItem)
            .where(ContentItem.status == ContentStatus.PUBLISHING.value)
            .order_by(ContentItem.updated_at)
            .limit(max_items)
        )
        for item in session.scalars(stmt):
            record = latest_record(session, item.id)
            started = record.updated_at or record.created_at if record is not None else None
            if started is None:
                started = item.updated_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            if started > deadline:
                continue  # 还在阈值内，八成正发着呢，一根手指都不许碰

            stuck_for = moment - started
            detail = (
                f"内容在 publishing 停留 {stuck_for.total_seconds() / 60:.0f} 分钟"
                f"（阈值 {stale_publishing_after().total_seconds() / 60:.0f} 分钟）。"
                "这**不是**平台把发布拒了——是进程在调用发布器的中途没了（被杀 / OOM / "
                "重启），幂等声明留在了库里而没人来收尾。已推回重试链；重投会先做平台侧"
                "对账（reconcile），平台那边其实成功了就直接认已发布，不会重复发。"
            )
            if record is not None and record.phase == PublishPhase.IN_FLIGHT.value:
                # 声明落到一个确定的结局上，别留成永远 in_flight 的孤儿。
                # attempts **不加**：这一次尝试从来没有得到过结果，不该烧掉一次重试额度
                record.phase = PublishPhase.FAILED.value
                record.last_error = f"stale_publishing: {detail}"
            transition(item, ContentStatus.PUBLISH_FAILED)
            transition(item, ContentStatus.RETRYING)
            log_review(
                session,
                item,
                actor="system",
                action=SystemAction.PUBLISH_STALE,
                reason=detail,
            )
            logger.warning("崩溃残留已推回重试链 item=%s account=%s", item.id, item.account_id)
            recovered += 1
            recovered_ids.append(item.id)

    if recovered:
        # 走节流层：卡住的稿子是**运维信号**（多半意味着进程最近被杀过），该让人知道，
        # 但一批捞回来十条不该刷十条消息。一次性事件（死信 / 发布失败）才逐条推
        notify_event(
            f"[崩溃残留] {recovered} 条内容卡在 publishing，已推回重试",
            (
                f"{', '.join(recovered_ids[:5])}{' …' if recovered > 5 else ''}\n"
                "含义是 core 进程在调用发布器的中途退出过（被杀 / OOM / 重启），"
                "不是平台发布失败。重投会先做平台侧对账，不会重复发。"
                "反复出现就去查进程为什么退出。"
            ),
            kind="stale_publishing",
            level="warning",
            notifier=notifier,
            now=moment,
        )
    return recovered


def tick_retry_sweep(
    *,
    limiter: RateLimiter | None = None,
    notifier: Notifier | None = None,
    now: datetime | None = None,
    max_items: int = 20,
) -> dict[str, int]:
    """把 ``retrying`` 的内容按指数退避重新投递；超龄的进死信。

    退避从**最后一次尝试**算起：``base * 2^(attempts-1)``，封顶
    ``SW_RETRY_BACKOFF_MAX_SECONDS``。

    两个必须有的特例：

    - **账号不健康就不重试**。``NeedsReloginError`` 把内容打到 ``retrying`` 而
      ``mark_account_needs_relogin`` 只挂起 ``scheduled`` 的项，所以这里必须自己
      按账号健康过滤，否则会一直去撞一个掉线的号（P3 遗留问题 ①）。
    - **超龄进死信**。``NeedsReloginError`` 刻意不计 ``attempts``（等人续期不该
      被重试次数烧掉），代价是没人处理时它会永远 ``retrying``。
      ``SW_RETRY_MAX_AGE_HOURS``（默认 48h）是这条路的唯一出口。

    扫完之后还会跑两趟**崩溃残留回收**，它们和重投不是一回事，但共用这个 5 分钟一轮、
    无条件执行的 tick（不新起 job，也不挂在 ``tick_generate`` 上——生成被关掉时残留
    照样得有人捞）：

    - :func:`recover_stale_publishing`：卡在 ``publishing`` 的推回本函数的重试链。
      那是全仓唯一扫这个状态的地方。
    - :func:`recover_stale_drafts`：入库了却从没跑过机器审核的 draft，离线补一遍。

    统计键：``scanned`` / ``published`` / ``failed`` / ``dead_letter``
    / ``skipped_backoff`` / ``skipped_account`` / ``skipped_rate``
    / ``recovered_stale`` / ``recovered_drafts``。
    """
    from core.config import get_settings

    settings = get_settings()
    limiter = limiter or RATE_LIMITER
    notifier = notifier or get_default_notifier()
    moment = _now(now)
    max_age = timedelta(hours=settings.sw_retry_max_age_hours)
    stats = {
        "scanned": 0,
        "published": 0,
        "failed": 0,
        "dead_letter": 0,
        "skipped_backoff": 0,
        "skipped_account": 0,
        "skipped_rate": 0,
        "recovered_stale": 0,
        "recovered_drafts": 0,
    }

    with session_scope() as session:
        stmt = (
            select(ContentItem)
            .where(ContentItem.status == ContentStatus.RETRYING.value)
            .order_by(ContentItem.updated_at)
            .limit(max_items)
        )
        for item in session.scalars(stmt):
            stats["scanned"] += 1
            record = latest_record(session, item.id)
            if record is None:
                # 没有发布记录却在 retrying：状态被手工改过。回 scheduled 让主链接管
                logger.warning("内容 %s 处于 retrying 但没有发布记录，交还定时发布", item.id)
                stats["skipped_backoff"] += 1
                continue

            first_at = record.created_at
            if first_at.tzinfo is None:
                first_at = first_at.replace(tzinfo=UTC)
            if moment - first_at >= max_age:
                transition(item, ContentStatus.DEAD_LETTER)
                detail = (
                    f"重试超过 {settings.sw_retry_max_age_hours} 小时仍未成功"
                    f"（尝试 {record.attempts} 次，最后错误：{record.last_error or '未知'}）"
                )
                log_review(
                    session, item, actor="system", action=SystemAction.DEAD_LETTER, reason=detail
                )
                # 先落定再通知。死信是终态，早提交没有坏处；而不提交就发通知的话，
                # 第 1 条的写攥住整库写锁，**第 2 条起**的 Notifier.send（HTTP，
                # core/notify.py 默认 timeout=5.0）全在锁里跑——max_items 默认 20，
                # webhook 一挂最坏 ~95 秒握着整库写锁。而"webhook 超时"与"大批
                # 内容进死信"高度相关，真出事时两者同时发生。
                # 这里保持一条内容一条通知（聚合会让人看不出是哪条死的），
                # 所以是逐条 commit，不是把通知挪到循环外。
                session.commit()
                notifier.send(title=f"[死信] {item.id}", text=detail, level="error")
                stats["dead_letter"] += 1
                continue

            last_at = record.updated_at or record.created_at
            if last_at.tzinfo is None:
                last_at = last_at.replace(tzinfo=UTC)
            wait = backoff_for(record.attempts)
            if moment < last_at + wait:
                stats["skipped_backoff"] += 1
                continue

            account = session.get(Account, item.account_id)
            if not _publishable(account):
                stats["skipped_account"] += 1
                continue
            assert account is not None
            policy = policy_of(account)

            if limiter.deny_reason(
                account.id,
                policy.daily_limit,
                now=moment,
                session=session,
                min_interval=policy.min_interval,
                timezone=policy.timezone,  # 日上限按账号本地日（P11.3）
            ):
                stats["skipped_rate"] += 1
                continue

            try:
                publisher = get_publisher(account.platform, account.id)
            except PublisherNotAvailable as exc:
                logger.warning("重试跳过 %s：%s", item.id, exc)
                stats["skipped_account"] += 1
                continue
            try:
                publish_with_idempotency(
                    session, item, publisher, notifier=notifier, account=account
                )
            except PublishError as exc:
                logger.info("重试仍失败 %s: %s", item.id, exc)
                stats["failed"] += 1
                if item.status == ContentStatus.DEAD_LETTER.value:
                    stats["dead_letter"] += 1
                continue
            limiter.record(
                account.id,
                now=moment,
                token=f"{account.platform}:{item.id}",
                timezone=policy.timezone,  # 进程内计数与 DB 用同一条本地日界
            )
            stats["published"] += 1

    # 放在扫描**之后**、而且各自单开一个事务：见 recover_stale_publishing 的说明
    stats["recovered_stale"] = recover_stale_publishing(
        notifier=notifier, now=moment, max_items=max_items
    )
    stats["recovered_drafts"] = recover_stale_drafts(
        notifier=notifier, now=moment, max_items=max_items
    )
    return stats


# ------------------------------------------------------------------ 登录巡检

#: 抖音巡检上次真正执行的时刻（进程内）。见 :func:`tick_login_health` 的节流分支。
_LAST_DOUYIN_HEALTH_AT: datetime | None = None
_DOUYIN_HEALTH_LOCK = threading.Lock()


def reset_login_health_throttle() -> None:
    """清掉抖音巡检节流状态（测试 / 手工强制巡检用）。"""
    global _LAST_DOUYIN_HEALTH_AT
    with _DOUYIN_HEALTH_LOCK:
        _LAST_DOUYIN_HEALTH_AT = None


def _douyin_health_due(now: datetime) -> bool:
    """抖音巡检是否到点。到点则**顺手记账**（同一 tick 内不会重复放行）。"""
    global _LAST_DOUYIN_HEALTH_AT
    from core.config import get_settings

    interval = timedelta(minutes=get_settings().douyin_login_health_interval_minutes)
    with _DOUYIN_HEALTH_LOCK:
        last = _LAST_DOUYIN_HEALTH_AT
        if last is not None and now - last < interval:
            return False
        _LAST_DOUYIN_HEALTH_AT = now
        return True


def tick_login_health(
    *,
    platforms: Sequence[str] | None = None,
    notifier: Notifier | None = None,
    now: datetime | None = None,
    force: bool = False,
) -> dict[str, int]:
    """登录态巡检：把各账号 ``Publisher.health()`` 的结果落到 Account 状态机。

    小红书 cookies 数周过期一次，且同一账号不允许多网页端同时登录；等发布时才发现
    掉线就已经错过排期，所以每 10 分钟主动巡一次。``needs_relogin`` 会挂起该账号的
    排期项并通知人去 ``/accounts/{id}/login`` 扫码，扫完自动恢复。

    实现在 ``publishers/xhs/login.py``（对所有平台通用，不只小红书）。

    **抖音分支（P3）**：抖音的 ``health()`` 会真的在宿主机上开一个有头浏览器去看
    创作者中心，成本比小红书的一次 HTTP 高一个量级，而且频繁导航本身就不像人。
    所以它按 ``DOUYIN_LOGIN_HEALTH_INTERVAL_MINUTES``（默认 30 分钟）**单独节流**：
    没到点就整体跳过 douyin 平台，统计里记 ``douyin_throttled``。
    ``force=True`` 或显式只传 ``platforms=["douyin"]`` 时不节流（人工强制巡检）。
    """
    from publishers.base import PLATFORMS
    from publishers.xhs.login import check_accounts

    moment = _now(now)
    targets = list(platforms) if platforms else list(PLATFORMS)
    throttled = 0
    explicit_douyin = platforms is not None and list(platforms) == ["douyin"]
    if "douyin" in targets and not (force or explicit_douyin) and not _douyin_health_due(moment):
        targets = [p for p in targets if p != "douyin"]
        throttled = 1

    stats: dict[str, int] = {"checked": 0}
    if targets:
        with session_scope() as session:
            stats = check_accounts(session, platforms=targets, notifier=notifier)
    if throttled:
        # 只在真的跳过时才出现这个键，免得干扰既有断言
        stats["douyin_throttled"] = throttled
    return stats


# -------------------------------------------------------------------- 指标 / 复盘


def tick_confirm_gate(
    *,
    account_ids: Sequence[str] | None = None,
    notifier: Notifier | None = None,
    now: datetime | None = None,
    max_items: int = 100,
) -> dict[str, int]:
    """发布前人工确认闸门的巡检（P12）。实现在 ``core/confirm.py``。

    为什么单开一个 tick 而不是塞进 ``tick_scheduled_publish``：后者只扫**已到点**的
    内容（``scheduled_at <= now``），而确认卡要在**离槽位还有几小时**时就推出去
    （人才有充足时间点），槽位前 30 分钟还要补一次提醒。这两件事发生在到点之前，
    ``tick_scheduled_publish`` 根本扫不到它们。

    统计键见 ``core.confirm.run_confirm_gate``。
    """
    from core.confirm import run_confirm_gate

    return run_confirm_gate(
        account_ids=account_ids, notifier=notifier, now=_now(now), max_items=max_items
    )


def tick_metrics(
    *, max_items: int = 50, now: datetime | None = None, respect_windows: bool = False
) -> dict[str, int]:
    """给 published/measured 的内容拉指标快照（只追加）。

    实现在 ``metrics/collector.py``。``respect_windows=True`` 时只在发布后
    24h / 7d 两个窗口到期且尚未覆盖时才打快照（生产调度用）；默认 False 表示
    每次调用都打一张，便于手动触发与本地联调。
    """
    from metrics.collector import collect_all

    return collect_all(max_items=max_items, now=now, respect_windows=respect_windows)


def tick_insights(
    *,
    account_ids: Sequence[str] | None = None,
    llm: Any | None = None,
    now: datetime | None = None,
    force: bool = False,
) -> dict[str, int]:
    """复盘 Agent：7 天指标 → 结论 → ``prompts/accounts/<id>/insights.md``。

    实现在 ``metrics/insights.py``。job 按 6 小时跑，但每个账号内部按
    ``INSIGHTS_INTERVAL_HOURS``（默认 24 小时）各自节流，所以多跑几次不会重复烧
    token。无 ``ANTHROPIC_API_KEY`` 时整体跳过并留日志。
    """
    from metrics import insights

    with session_scope() as session:
        return insights.run(
            session,
            account_ids=list(account_ids) if account_ids else None,
            llm=llm,
            now=now,
            force=force,
        )


# ------------------------------------------------------------------ 渲染任务


def tick_render_jobs(*, max_jobs: int = 20, client: MptClient | None = None) -> dict[str, int]:
    """轮询进行中的视频渲染任务（P3）。

    为什么需要它：MPT 渲染一条片子要几分钟到几十分钟，生成链的 HTTP 请求早就返回
    了。管线等超时后会把 ``RenderJob`` 留在 ``running`` 并产出一个**没有成片**的
    内容进人工队列；这个 job 负责后续把成片补挂回去，让人不用重跑整条生成链。

    处置：

    - ``state=1`` 完成 → 下载成片到 ``data/media/<item_id>/`` 并挂进 bundle
      （仅限内容还没被人工批准，见 ``ATTACHABLE_STATUSES``）
    - ``state=-1`` 失败 → 标 ``failed``，错误写进 ``last_error``，等人处置
    - 404 → 标 ``lost``（sidecar 重启）。**这里不重提交**：重提交要重新过预算闸门，
      属于生成链的职责，调度器只负责观测
    """
    from sqlalchemy import select as sa_select

    from core.models import ACTIVE_RENDER_STATES, RenderJob, RenderJobState
    from generation.mpt_client import build_client
    from generation.video_pipeline import (
        DEFAULT_MEDIA_ROOT,
        VIDEO_FILENAME,
        attach_video_to_item,
        download_outputs,
        sync_render_job,
    )

    stats = {"scanned": 0, "done": 0, "failed": 0, "lost": 0, "running": 0, "attached": 0}
    mpt = client if client is not None else build_client()
    owns_client = client is None
    try:
        with session_scope() as session:
            jobs = list(
                session.scalars(
                    sa_select(RenderJob)
                    .where(RenderJob.state.in_(ACTIVE_RENDER_STATES))
                    .order_by(RenderJob.created_at)
                    .limit(max_jobs)
                )
            )
            for job in jobs:
                stats["scanned"] += 1
                # 上一个 job 的处置（下载路径、state、补挂进内容的 bundle）先落定，
                # 再去碰网络。不这么做的话第 1 个 job 的写就把整库写锁攥住了，之后
                # 每个 job 的轮询 HTTP 与**成片下载**（mpt_download_timeout_seconds
                # 默认 600 秒）都在这把锁里，一轮最多 20 个（P16.3）。
                # ``sync_render_job`` 自己也 commit，但那是在它的 HTTP **之后**，
                # 挡不住上一轮遗留的脏数据，所以这一句不是重复
                session.commit()
                try:
                    task = sync_render_job(session, job, mpt)
                except PublishError as exc:
                    logger.warning("轮询渲染任务失败 job=%s: %s", job.id, exc)
                    continue
                if task is None:
                    stats["lost"] += 1
                    continue
                if task.failed:
                    stats["failed"] += 1
                    continue
                if not task.done:
                    stats["running"] += 1
                    continue
                stats["done"] += 1
                item = session.get(ContentItem, job.content_item_id)
                if item is None:
                    # 内容项被删了（或生成链在入库前就崩了）。成片下下来也没地方挂，
                    # 直接收尾——render_jobs 没有外键，本来就允许这种孤儿行
                    job.state = RenderJobState.DONE
                    job.progress = 100
                    job.last_error = "对应的 ContentItem 已不存在，跳过下载"
                    continue
                try:
                    video = download_outputs(
                        mpt, task, DEFAULT_MEDIA_ROOT / job.content_item_id / VIDEO_FILENAME
                    )
                except PublishError as exc:
                    job.last_error = f"下载成片失败: {exc}"
                    logger.warning("下载成片失败 job=%s: %s", job.id, exc)
                    continue
                job.result_paths = [str(video)]
                job.progress = 100
                job.state = RenderJobState.DONE
                if attach_video_to_item(session, item, video):
                    stats["attached"] += 1
    finally:
        if owns_client:
            mpt.close()
    return stats


# ---------------------------------------------------------------- tick 注册表

#: 名字 → tick 函数。``POST /dev/tick/{name}`` 与 ``create_scheduler`` 共用这一份，
#: 保证"手动触发的"和"定时跑的"永远是同一个函数（审计时不用担心两条路会分叉）
TICKS: dict[str, Callable[..., dict[str, int]]] = {
    "sourcing": tick_sourcing,
    "generate": tick_generate,
    "scheduled_publish": tick_scheduled_publish,
    "confirm_gate": tick_confirm_gate,
    "retry_sweep": tick_retry_sweep,
    "metrics": tick_metrics,
    "login_health": tick_login_health,
    "render_jobs": tick_render_jobs,
    "insights": tick_insights,
}


#: 各 tick 的**默认参数**。``create_scheduler`` 与 ``run_tick`` 共用同一份，
#: 于是 `POST /dev/tick/metrics` 与定时跑的那次行为完全一致（早期版本这里对不上：
#: 定时是 ``respect_windows=True``、手动是 ``False`` —— 手动触发看到的不是生产行为）。
#: 调用方显式传参可以覆盖，例如 `?respect_windows=false` 强制采一张。
TICK_DEFAULT_KWARGS: dict[str, dict[str, Any]] = {
    # 生产只在 24h / 7d 窗口到期且未覆盖时采样，否则会把只追加的快照表刷爆
    "metrics": {"respect_windows": True},
}


def tick_kwargs(
    name: str,
    *,
    account_id: str | None = None,
    platform: str | None = None,
    force: bool = False,
    respect_windows: bool | None = None,
) -> dict[str, Any]:
    """把"运维面板上那几个通用开关"翻译成某个 tick 的关键字参数。

    ``POST /dev/tick/{name}`` 与 ``POST /api/v1/system/ticks/{name}`` 共用这一份映射，
    两条路才不会一个接受 ``account_id`` 另一个不接受。tick 不认的参数抛 ``ValueError``
    （调用方转 422）——静默忽略比报错糟得多：人以为限定了账号，其实全量跑了一遍。
    """
    kwargs: dict[str, Any] = {}
    if account_id:
        if name in ("generate", "insights", "confirm_gate"):
            kwargs["account_ids"] = [account_id]
        else:
            raise ValueError(f"tick {name} 不接受 account_id")
    if platform:
        if name == "generate" or name == "login_health":
            kwargs["platforms"] = [platform]
        elif name == "sourcing":
            kwargs["sources"] = [platform]
        else:
            raise ValueError(f"tick {name} 不接受 platform")
    if force and name in ("login_health", "insights"):
        kwargs["force"] = True
    if respect_windows is not None:
        if name != "metrics":
            raise ValueError(f"tick {name} 不接受 respect_windows")
        kwargs["respect_windows"] = respect_windows
    return kwargs


def run_tick(name: str, **kwargs: Any) -> dict[str, int]:
    """按名字跑一个 tick（套用 :data:`TICK_DEFAULT_KWARGS`）。未知名字抛 :class:`KeyError`。"""
    try:
        fn = TICKS[name]
    except KeyError:
        raise KeyError(f"未知 tick: {name!r}，可用 {sorted(TICKS)}") from None
    merged = {**TICK_DEFAULT_KWARGS.get(name, {}), **kwargs}
    started = datetime.now(UTC)
    stats = fn(**merged)
    logger.info(
        "tick %s 完成 耗时=%.2fs 统计=%s",
        name,
        (datetime.now(UTC) - started).total_seconds(),
        stats,
    )
    return stats


def create_scheduler(*, start: bool = False) -> BackgroundScheduler:
    """构造调度器并注册全部 job。``start=True`` 时立即启动。"""
    from core.config import get_settings

    settings = get_settings()
    scheduler = BackgroundScheduler(timezone="UTC")

    def add(name: str, *, jitter: int | None = None, **trigger: Any) -> None:
        scheduler.add_job(
            TICKS[name],
            trigger="interval",
            id=f"tick_{name}",
            # 与 run_tick 共用同一份默认参数，两条路不会分叉
            kwargs=dict(TICK_DEFAULT_KWARGS.get(name, {})),
            max_instances=1,
            coalesce=True,  # 停机补跑时只跑最后一次，不把积压的 tick 全放出来
            replace_existing=True,
            jitter=jitter,
            **trigger,
        )

    add("sourcing", hours=settings.sw_sourcing_interval_hours)
    # 出稿时刻加抖动（P12）：不加的话每天整点同一秒出稿，太机器。
    # ± GENERATE_JITTER_SECONDS 内随机偏移，配合排期本身的窗口/间隔约束，
    # 落到平台侧就是一条不规整的时间线
    add(
        "generate",
        minutes=settings.sw_generate_interval_minutes,
        jitter=max(settings.sw_generate_jitter_seconds, 0) or None,
    )
    add("scheduled_publish", minutes=1)
    # 确认闸门要在"离槽位还有几小时"时就把卡推出去，1 分钟一轮足够灵敏，
    # 而真正的去重靠 confirm_pushed_at，跑再密也不会重复打扰
    add("confirm_gate", minutes=1)
    add("retry_sweep", minutes=settings.sw_retry_sweep_interval_minutes)
    add("metrics", hours=6)  # respect_windows 见 TICK_DEFAULT_KWARGS
    # job 频率按小红书口径（默认 10 分钟）；抖音在函数内部另有 30 分钟节流，
    # 所以这里一个 job 就够，不需要为抖音单开一个
    add("login_health", minutes=settings.xhs_login_health_interval_minutes)
    # 渲染任务动辄十几分钟，1 分钟一轮足够；MPT 没起来时每轮只是一次失败的 HTTP
    add("render_jobs", minutes=1)
    # 每个账号内部还有 24 小时节流，这里跑密一点只是为了让重启后能尽快补上
    add("insights", hours=6)

    if start:
        scheduler.start()
    return scheduler


def main(argv: list[str] | None = None) -> int:
    """独立进程跑调度器：``uv run python -m core.scheduler``。

    常规形态是随 core 一起启动（``SW_SCHEDULER_ENABLED=true``）。这个入口是给
    "控制面与调度分开部署"或"临时只想让定时任务跑起来"用的。
    """
    import argparse
    import signal
    import threading

    from core import db
    from core.config import get_settings

    parser = argparse.ArgumentParser(prog="python -m core.scheduler", description="定时调度器")
    parser.add_argument("--once", metavar="TICK", help="只跑一个 tick 就退出（调试用）")
    parser.add_argument("--list", action="store_true", help="列出全部 tick 名字")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.list:
        print("\n".join(sorted(TICKS)))
        return 0

    db.configure(get_settings().sw_database_url)
    db.init_db()
    if args.once:
        print(run_tick(args.once))
        return 0

    scheduler = create_scheduler(start=True)
    logger.info("调度器已启动: %s", [job.id for job in scheduler.get_jobs()])
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()
    scheduler.shutdown(wait=False)
    logger.info("调度器已停止")
    return 0


__all__ = [
    "GENERATABLE_ACCOUNT_STATUSES",
    "PUBLISHABLE_ACCOUNT_STATUSES",
    "RATE_LIMITER",
    "TICKS",
    "TICK_DEFAULT_KWARGS",
    "RateLimiter",
    "backoff_for",
    "create_scheduler",
    "latest_record",
    "local_day_start",
    "main",
    "produced_today",
    "recover_stale_drafts",
    "recover_stale_publishing",
    "reset_login_health_throttle",
    "run_tick",
    "stale_publishing_after",
    "stale_review_after",
    "tick_confirm_gate",
    "tick_generate",
    "tick_insights",
    "tick_kwargs",
    "tick_login_health",
    "tick_metrics",
    "tick_render_jobs",
    "tick_retry_sweep",
    "tick_scheduled_publish",
    "tick_sourcing",
]


if __name__ == "__main__":
    raise SystemExit(main())
