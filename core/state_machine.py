"""双状态机 + 幂等键 + 两阶段发布记录（P0 冻结）。

ContentItem::

    topic → drafting → draft → reviewing → rejected
                                        ↘ approved → scheduled → publishing → published → measured
    异常分支：publishing → publish_failed → retrying → publishing
                                        ↘ dead_letter
    账号级挂起：scheduled ⇄ suspended（Account 进入 needs_relogin 时挂起，恢复后放回）

Account::

    ok / degraded / needs_relogin / banned

登录过期是**账号级**事件，不挂在内容状态上。
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.models import Account, ContentItem, PublishRecord, ReviewLog, new_id, utcnow
from core.notify import Notifier, get_default_notifier
from publishers.base import (
    ContentBundle,
    NeedsReloginError,
    PermanentError,
    Publisher,
    PublishError,
    PublishResult,
    RetryableError,
)

logger = logging.getLogger("social_workflow.state_machine")


class ContentStatus(StrEnum):
    TOPIC = "topic"
    DRAFTING = "drafting"
    DRAFT = "draft"
    REVIEWING = "reviewing"
    REJECTED = "rejected"
    APPROVED = "approved"
    SCHEDULED = "scheduled"
    SUSPENDED = "suspended"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    MEASURED = "measured"
    PUBLISH_FAILED = "publish_failed"
    RETRYING = "retrying"
    DEAD_LETTER = "dead_letter"


class AccountStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    NEEDS_RELOGIN = "needs_relogin"
    BANNED = "banned"
    #: 人工停用（P10）。账号还在、历史内容还在，只是不再生成也不再发布。
    #: **不是巡检能得出的状态**——只有人点「停用」才会到这里，巡检遇到它一律跳过
    SUSPENDED = "suspended"


class ReviewAction(StrEnum):
    """人工审核动作（合规证据链）。"""

    APPROVE = "approve"
    REJECT = "reject"
    EDIT = "edit"
    #: 发布前的人工确认（P12）。批准是"这稿子能发"，确认是"现在真的发出去"——
    #: 小红书封禁 AI 全托管账号，这一跳是"人参与了"最直接的证据
    CONFIRM = "confirm"


class SystemAction(StrEnum):
    """系统写入 ReviewLog 的事件（actor='system'），与人工动作区分。"""

    PUBLISH = "publish"
    PUBLISH_FAILED = "publish_failed"
    RECONCILED = "reconciled"
    SUSPEND = "suspend"
    RESUME = "resume"
    DEAD_LETTER = "dead_letter"
    #: 批准后自动排进账号的发布时段窗口，见 core/scheduling.py
    SCHEDULE = "schedule"
    #: 已把确认卡推给人，等他点（P12）
    CONFIRM_PUSHED = "confirm_pushed"
    #: TTL 内没人确认，自动驳回并释放槽位（P12）
    CONFIRM_EXPIRED = "confirm_expired"
    #: 崩溃残留：内容卡在 ``publishing`` 太久，被 ``tick_retry_sweep`` 推回重试链（P16.2）。
    #: **刻意不复用** ``PUBLISH_FAILED``：那个的含义是"平台把这次发布拒了/发不出去"，
    #: 而这个的含义是"我们不知道平台那边到底怎么样了，进程在半路没的"。两者的排查方向
    #: 完全不同（一个查平台与内容，一个查进程为什么死），混成一种原因等于把证据抹平
    PUBLISH_STALE = "publish_stale"
    #: 入库了却从来没跑过机器审核的 draft，被 ``tick_retry_sweep`` 补跑了一遍（P16.3）。
    #: **刻意不复用** ``machine_review``：那个的含义是"审过了，结论如下"，而这个的含义是
    #: "本该在生成链里审的那一遍没跑成，这是事后补的、而且缺 LLM 语境判定那一档"。
    #: 混成一种，审核台上就分不出"机器说没问题"和"机器根本没看全"
    REVIEW_MISSING = "review_missing"


class PublishPhase(StrEnum):
    IN_FLIGHT = "in_flight"
    DONE = "done"
    FAILED = "failed"


class IllegalTransition(Exception):
    """非法状态迁移。"""

    def __init__(self, entity: str, from_status: str, to_status: str) -> None:
        super().__init__(f"{entity} 非法状态迁移: {from_status} -> {to_status}")
        self.entity = entity
        self.from_status = from_status
        self.to_status = to_status


# 内容状态迁移表（唯一真相）
CONTENT_TRANSITIONS: dict[ContentStatus, frozenset[ContentStatus]] = {
    ContentStatus.TOPIC: frozenset({ContentStatus.DRAFTING, ContentStatus.DEAD_LETTER}),
    ContentStatus.DRAFTING: frozenset({ContentStatus.DRAFT, ContentStatus.DEAD_LETTER}),
    ContentStatus.DRAFT: frozenset({ContentStatus.REVIEWING, ContentStatus.DRAFTING}),
    ContentStatus.REVIEWING: frozenset(
        {ContentStatus.APPROVED, ContentStatus.REJECTED, ContentStatus.DRAFT}
    ),
    ContentStatus.REJECTED: frozenset(
        {ContentStatus.DRAFTING, ContentStatus.DRAFT, ContentStatus.DEAD_LETTER}
    ),
    # approved -> draft：批准后又想改稿，撤回重审
    ContentStatus.APPROVED: frozenset({ContentStatus.SCHEDULED, ContentStatus.DRAFT}),
    ContentStatus.SCHEDULED: frozenset(
        {ContentStatus.PUBLISHING, ContentStatus.SUSPENDED, ContentStatus.APPROVED}
    ),
    ContentStatus.SUSPENDED: frozenset({ContentStatus.SCHEDULED, ContentStatus.DEAD_LETTER}),
    ContentStatus.PUBLISHING: frozenset({ContentStatus.PUBLISHED, ContentStatus.PUBLISH_FAILED}),
    ContentStatus.PUBLISHED: frozenset({ContentStatus.MEASURED}),
    # measured 自环：24h / 7d 两次快照都停留在 measured
    ContentStatus.MEASURED: frozenset({ContentStatus.MEASURED}),
    ContentStatus.PUBLISH_FAILED: frozenset({ContentStatus.RETRYING, ContentStatus.DEAD_LETTER}),
    ContentStatus.RETRYING: frozenset({ContentStatus.PUBLISHING, ContentStatus.DEAD_LETTER}),
    ContentStatus.DEAD_LETTER: frozenset(),
}

# 账号状态迁移表：任何状态都可以直接被巡检结果改写，除 banned 需人工处理。
# suspended 是**人工停用**，只能人工进、人工出：巡检碰到它直接跳过（见
# publishers/xhs/login.py 与 core/login_flow.py），所以它没有"被巡检改写"这条边。
ACCOUNT_TRANSITIONS: dict[AccountStatus, frozenset[AccountStatus]] = {
    AccountStatus.OK: frozenset(
        {
            AccountStatus.DEGRADED,
            AccountStatus.NEEDS_RELOGIN,
            AccountStatus.BANNED,
            AccountStatus.SUSPENDED,
            AccountStatus.OK,
        }
    ),
    AccountStatus.DEGRADED: frozenset(
        {
            AccountStatus.OK,
            AccountStatus.NEEDS_RELOGIN,
            AccountStatus.BANNED,
            AccountStatus.SUSPENDED,
            AccountStatus.DEGRADED,
        }
    ),
    AccountStatus.NEEDS_RELOGIN: frozenset(
        {
            AccountStatus.OK,
            AccountStatus.BANNED,
            AccountStatus.SUSPENDED,
            AccountStatus.NEEDS_RELOGIN,
        }
    ),
    # banned 是人工确认后才能解除的终态
    AccountStatus.BANNED: frozenset({AccountStatus.BANNED}),
    # 停用之后只能人工启用（回 ok，随后第一次巡检会把真实健康写回来）
    AccountStatus.SUSPENDED: frozenset({AccountStatus.OK, AccountStatus.SUSPENDED}),
}

TERMINAL_CONTENT_STATUSES = frozenset({ContentStatus.DEAD_LETTER})
# 审核队列展示的状态
REVIEW_QUEUE_STATUSES = (ContentStatus.DRAFT, ContentStatus.REVIEWING, ContentStatus.REJECTED)


# ------------------------------------------------------------------ 迁移原语


def can_transition(from_status: str, to_status: str) -> bool:
    try:
        src = ContentStatus(from_status)
        dst = ContentStatus(to_status)
    except ValueError:
        return False
    return dst in CONTENT_TRANSITIONS[src]


def transition(item: ContentItem, to_status: ContentStatus | str) -> ContentItem:
    """迁移 ContentItem 状态，非法则抛 :class:`IllegalTransition`。"""
    try:
        dst = ContentStatus(to_status)
    except ValueError as exc:
        raise IllegalTransition("ContentItem", item.status, str(to_status)) from exc
    try:
        src = ContentStatus(item.status)
    except ValueError as exc:
        raise IllegalTransition("ContentItem", item.status, dst.value) from exc
    if dst not in CONTENT_TRANSITIONS[src]:
        raise IllegalTransition("ContentItem", src.value, dst.value)
    item.status = dst.value
    item.updated_at = utcnow()
    return item


def transition_account(account: Account, to_status: AccountStatus | str) -> Account:
    try:
        dst = AccountStatus(to_status)
    except ValueError as exc:
        raise IllegalTransition("Account", account.status, str(to_status)) from exc
    try:
        src = AccountStatus(account.status)
    except ValueError as exc:
        raise IllegalTransition("Account", account.status, dst.value) from exc
    if dst not in ACCOUNT_TRANSITIONS[src]:
        raise IllegalTransition("Account", src.value, dst.value)
    account.status = dst.value
    account.updated_at = utcnow()
    return account


def log_review(
    session: Session,
    item: ContentItem,
    *,
    actor: str,
    action: ReviewAction | SystemAction | str,
    reason: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> ReviewLog:
    """写审计日志。人工审核动作与系统事件都走这里。"""
    entry = ReviewLog(
        id=new_id("rvl"),
        content_item_id=item.id,
        actor=actor,
        action=str(action),
        reason=reason,
        before_json=before,
        after_json=after,
        at=utcnow(),
    )
    session.add(entry)
    return entry


# ------------------------------------------------------------ 人工审核动作


def approve(session: Session, item: ContentItem, *, actor: str, reason: str | None = None) -> None:
    """批准：draft/reviewing → approved，并写审计日志。发布前必经此卡点。"""
    if item.status == ContentStatus.DRAFT:
        transition(item, ContentStatus.REVIEWING)
    transition(item, ContentStatus.APPROVED)
    log_review(session, item, actor=actor, action=ReviewAction.APPROVE, reason=reason)


def reject(session: Session, item: ContentItem, *, actor: str, reason: str) -> None:
    """驳回：理由写回 review_notes，供改稿 Agent 作为 prompt 输入。"""
    if not reason.strip():
        raise ValueError("驳回必须填写理由")
    if item.status == ContentStatus.DRAFT:
        transition(item, ContentStatus.REVIEWING)
    transition(item, ContentStatus.REJECTED)
    item.review_notes = reason
    log_review(session, item, actor=actor, action=ReviewAction.REJECT, reason=reason)


def edit(
    session: Session,
    item: ContentItem,
    *,
    actor: str,
    new_bundle: dict[str, Any],
    reason: str | None = None,
) -> None:
    """人工改稿：记录 before/after 供 UI 出 diff，状态回到 draft。"""
    before = dict(item.bundle_json or {})
    if item.status == ContentStatus.REVIEWING or item.status == ContentStatus.REJECTED:
        transition(item, ContentStatus.DRAFT)
    elif item.status not in (ContentStatus.DRAFT, ContentStatus.APPROVED):
        raise IllegalTransition("ContentItem", item.status, ContentStatus.DRAFT.value)
    item.bundle_json = new_bundle
    item.updated_at = utcnow()
    log_review(
        session,
        item,
        actor=actor,
        action=ReviewAction.EDIT,
        reason=reason,
        before=before,
        after=new_bundle,
    )


# ----------------------------------------------------------------- 幂等键


def make_idem_key(
    account_id: str,
    platform: str,
    content_hash: str,
    scheduled_slot: str | datetime | None = None,
) -> str:
    """``sha256(account_id | platform | content_hash | scheduled_slot)``。

    ``scheduled_slot`` 参与哈希：同一内容改期重发是**新的**发布意图，不应被幂等挡掉；
    而同一槽位的重试必须命中同一个键。
    """
    if isinstance(scheduled_slot, datetime):
        slot = scheduled_slot.astimezone().isoformat(timespec="minutes")
    else:
        slot = scheduled_slot or ""
    payload = "|".join([account_id, platform, content_hash, slot])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def slot_of(item: ContentItem) -> str:
    """内容项的排期槽位字符串。未排期则为空串（表示"立即发一次"）。"""
    if item.scheduled_at is None:
        return ""
    return item.scheduled_at.isoformat(timespec="minutes")


# --------------------------------------------------------- 账号级挂起 / 恢复


def suspend_scheduled_items(
    session: Session,
    account: Account,
    *,
    reason: str,
    actor: str = "system",
) -> list[ContentItem]:
    """把该账号所有 ``scheduled`` 项挂起，原状态记进 ``prev_status`` 供恢复时放回。

    掉线（``needs_relogin``）与人工停用（``suspended``）共用这一段：区别只在账号
    状态与理由文案，"排期项怎么处理"必须是同一套，否则两条路会各挂各的。
    """
    stmt = select(ContentItem).where(
        ContentItem.account_id == account.id,
        ContentItem.status == ContentStatus.SCHEDULED.value,
    )
    suspended: list[ContentItem] = []
    for item in session.scalars(stmt):
        item.prev_status = item.status
        transition(item, ContentStatus.SUSPENDED)
        log_review(session, item, actor=actor, action=SystemAction.SUSPEND, reason=reason)
        suspended.append(item)
    return suspended


def restore_suspended_items(
    session: Session, account: Account, *, actor: str = "system"
) -> list[ContentItem]:
    """把挂起的排期项放回它挂起前的状态。"""
    stmt = select(ContentItem).where(
        ContentItem.account_id == account.id,
        ContentItem.status == ContentStatus.SUSPENDED.value,
    )
    restored: list[ContentItem] = []
    for item in session.scalars(stmt):
        target = item.prev_status or ContentStatus.SCHEDULED.value
        transition(item, target)
        item.prev_status = None
        log_review(session, item, actor=actor, action=SystemAction.RESUME)
        restored.append(item)
    return restored


def mark_account_needs_relogin(
    session: Session,
    account: Account,
    *,
    detail: str = "",
    notifier: Notifier | None = None,
) -> list[ContentItem]:
    """账号登录态失效：置 needs_relogin，并挂起该账号所有 scheduled 项。

    返回被挂起的内容项。挂起时把原状态记在 ``prev_status``，恢复时放回。
    """
    from core.notify import notify_event, public_url

    if account.status != AccountStatus.NEEDS_RELOGIN:
        transition_account(account, AccountStatus.NEEDS_RELOGIN)
    suspended = suspend_scheduled_items(
        session, account, reason=f"账号 {account.id} 需重新登录：{detail}", actor="system"
    )
    # **必须走节流层**：登录巡检默认 10 分钟一轮，每轮都推的话一天上百条
    # "你的号掉线了"。用户的第一反应不是去扫码，而是把这个 bot 静音——
    # 通知通道就此整体失效，比没有通知更糟（见 core/notify.py 的节流层）
    notify_event(
        f"[需重登] {account.name}({account.id})",
        (
            f"{detail}\n已挂起排期内容 {len(suspended)} 条。\n"
            f"去扫码续期：{public_url(f'/workbench/accounts?id={account.id}')}\n"
            "（二维码有效期只有几分钟，过期了就在那一页再点一次「重新取码」）"
        ),
        kind="needs_relogin",
        account_id=account.id,
        level="warning",
        notifier=notifier,
    )
    return suspended


def restore_account(
    session: Session,
    account: Account,
    *,
    notifier: Notifier | None = None,
) -> list[ContentItem]:
    """登录续期成功：账号回 ok，挂起项放回原状态。"""
    notifier = notifier or get_default_notifier()
    transition_account(account, AccountStatus.OK)
    restored = restore_suspended_items(session, account)
    notifier.send(
        title=f"[已恢复] {account.name}({account.id})",
        text=f"登录态已恢复，放回排期内容 {len(restored)} 条。",
        level="info",
    )
    return restored


def deactivate_account(
    session: Session,
    account: Account,
    *,
    actor: str = "operator",
    reason: str = "",
    notifier: Notifier | None = None,
) -> list[ContentItem]:
    """人工停用：账号置 ``suspended``，名下 ``scheduled`` 项一并挂起。

    刻意**不删账号**——历史内容、发布记录、审计日志都还挂在它上面，删了就断了证据链。
    停用期间调度器既不给它出稿也不给它发布（``PUBLISHABLE`` / ``GENERATABLE``
    两个集合都不含 ``suspended``），巡检也会跳过它。
    """
    notifier = notifier or get_default_notifier()
    if account.status == AccountStatus.BANNED:
        raise IllegalTransition("Account", account.status, AccountStatus.SUSPENDED.value)
    if account.status != AccountStatus.SUSPENDED:
        transition_account(account, AccountStatus.SUSPENDED)
    text = reason or "人工停用"
    suspended = suspend_scheduled_items(
        session, account, reason=f"账号 {account.id} 已停用：{text}", actor=actor
    )
    notifier.send(
        title=f"[已停用] {account.name}({account.id})",
        text=f"{text}\n已挂起排期内容 {len(suspended)} 条；重新启用后会自动放回。",
        level="info",
    )
    return suspended


def reactivate_account(
    session: Session,
    account: Account,
    *,
    actor: str = "operator",
    notifier: Notifier | None = None,
) -> list[ContentItem]:
    """人工启用：账号回 ``ok``，挂起项放回原状态。

    回的是 ``ok`` 而不是"停用前那个状态"——停用期间登录态早就可能过期了，
    真实健康由下一次巡检写回来，这里不假装知道。
    """
    notifier = notifier or get_default_notifier()
    transition_account(account, AccountStatus.OK)
    restored = restore_suspended_items(session, account, actor=actor)
    notifier.send(
        title=f"[已启用] {account.name}({account.id})",
        text=f"已放回排期内容 {len(restored)} 条；下一次登录巡检会写回真实健康状态。",
        level="info",
    )
    return restored


def apply_health(
    session: Session,
    account: Account,
    status: str,
    *,
    detail: str = "",
    notifier: Notifier | None = None,
) -> None:
    """把 ``Publisher.health()`` 的结果落到 Account 状态机上。"""
    if status == AccountStatus.NEEDS_RELOGIN:
        mark_account_needs_relogin(session, account, detail=detail, notifier=notifier)
        return
    if status == AccountStatus.OK and account.status == AccountStatus.NEEDS_RELOGIN:
        restore_account(session, account, notifier=notifier)
        return
    transition_account(account, status)


# ------------------------------------------------------- 两阶段幂等发布


def _record_for(session: Session, idem_key: str) -> PublishRecord | None:
    return session.scalars(
        select(PublishRecord).where(PublishRecord.idem_key == idem_key)
    ).one_or_none()


def _commit_before_platform(session: Session) -> None:
    """碰平台之前把已经写下的东西 commit 掉。**两件事，缺一不可**（P16.2）。

    1. **交出写锁**。SQLite 的写锁是整库一把，``flush`` 只把语句发出去、锁一直握到
       ``commit``。这之后紧跟的是 ``publisher.reconcile`` / ``publisher.publish``——
       抖音那条最长 1200 秒（``douyin_publish_timeout_seconds``）。整段跑在锁里，同期
       任何别的写者（另一个 tick、控制面的一次请求）等满 5 秒就
       ``database is locked``，见 ``core/db.py`` 的 ``SQLITE_BUSY_TIMEOUT_MS``。
    2. **让幂等声明真的落盘**。``PublishRecord(phase=in_flight)`` 是"这一条我要发了"
       的声明，只 ``flush`` 不落盘的话，进程在 publish 中途没了这条声明就跟着没了：
       重启后 ``item`` 退回 ``scheduled``、幂等键查不到、``reconcile`` 也就不会被调用，
       于是**同一条内容被重复发布**。commit 在这里不只是放锁，是把幂等承诺兑现。

    代价是"已经在发了"这个状态先落盘：崩溃后 ``item`` 会停在 ``publishing``，而
    ``tick_scheduled_publish`` 只扫 ``scheduled``、``tick_retry_sweep`` 只扫
    ``retrying``，两边都看不见它。这条状态的出口是
    ``core/scheduler.py`` 的 ``recover_stale_publishing``：停留超过
    ``stale_publishing_after()``（默认 1 小时，= 最慢平台发布超时 ×3）就推回
    ``publish_failed → retrying``，由重投链接管——而重投链看到幂等键已存在会**先**
    ``reconcile``，所以捞回来也不会重复发。**改这里的 commit 边界之前先看那个函数**：
    没有它，这里的取舍就从"崩溃即重复发布"变成"崩溃即永久卡死"。

    调用方不依赖"整轮失败就整轮回滚"：``tick_scheduled_publish`` 与
    ``tick_retry_sweep`` 都是 ``except PublishError: ... continue``，循环里每条内容各
    算各的，最后由 ``session_scope`` 统一 commit；它们的统计口径也只看
    ``item.status``，不看事务边界。
    """
    session.commit()


def _settle_after_platform(session: Session, item: ContentItem) -> None:
    """把发布的**结局**落盘。

    声明是在碰平台之前就 commit 掉的，那么"这条声明最后怎么了"必须同样持久——否则
    一次崩溃或外层回滚会留下一条永远 ``in_flight`` 的孤儿声明，比旧的只 ``flush``
    还糟（旧写法至少声明和结局一起没）。异常分支尤其要落：``phase=failed`` 丢了，
    重试计数与死信判定就全错。

    提交失败**不往上抛**：这时调用方手里多半正拿着一个发布异常，那个异常比这里的
    落盘失败重要得多，不能被顶掉。吞掉之后记 error 日志，异常原样继续往上走。
    """
    try:
        session.commit()
    except Exception:  # pragma: no cover - 落盘失败本身已经是异常路径
        logger.exception("发布结局落盘失败 item=%s，状态可能停在 in_flight", item.id)
        session.rollback()


def _finish_done(
    session: Session,
    item: ContentItem,
    record: PublishRecord,
    result: PublishResult,
    *,
    action: SystemAction,
) -> PublishResult:
    record.phase = PublishPhase.DONE.value
    record.platform_post_id = result.platform_post_id
    record.url = result.url
    record.last_error = None
    record.updated_at = utcnow()
    if item.status != ContentStatus.PUBLISHED:
        transition(item, ContentStatus.PUBLISHED)
    log_review(
        session,
        item,
        actor="system",
        action=action,
        reason=result.url or result.platform_post_id,
        after={"platform_post_id": result.platform_post_id, "url": result.url},
    )
    return result


def publish_with_idempotency(
    session: Session,
    item: ContentItem,
    publisher: Publisher,
    *,
    scheduled_slot: str | None = None,
    max_attempts: int | None = None,
    notifier: Notifier | None = None,
    account: Account | None = None,
) -> PublishResult:
    """两阶段幂等发布。

    流程：
    1. ``prepare`` 归一化内容包，算 ``idem_key``。
    2. 尝试插入 ``PublishRecord(phase=in_flight)``；UNIQUE 冲突说明之前发起过，
       调 ``publisher.reconcile`` 做**平台侧对账**：命中即认为已发布（不重复发）。
    3. ``publish`` 成功补 ``platform_post_id/url``，phase=done，内容 → published。
    4. 异常按类型分流：
       - ``RetryableError`` → publish_failed → retrying；attempts 超上限 → dead_letter
       - ``NeedsReloginError`` → 账号 needs_relogin（挂起该账号排期项），内容 → retrying，
         **不计入** max_attempts（等人工续期，不该被重试次数烧掉）
       - ``PermanentError`` / 其它异常 → publish_failed → dead_letter
    """
    from core.config import get_settings

    notifier = notifier or get_default_notifier()
    if max_attempts is None:
        max_attempts = get_settings().sw_max_publish_attempts

    if item.status not in (ContentStatus.SCHEDULED, ContentStatus.RETRYING):
        raise IllegalTransition("ContentItem", item.status, ContentStatus.PUBLISHING.value)

    bundle = ContentBundle.model_validate(item.bundle_json)
    bundle = publisher.prepare(bundle)
    item.bundle_json = bundle.model_dump(mode="json")

    slot = scheduled_slot if scheduled_slot is not None else slot_of(item)
    idem_key = make_idem_key(item.account_id, bundle.platform, bundle.content_hash, slot)

    # --- 第一阶段：抢占幂等记录 -------------------------------------------
    record = _record_for(session, idem_key)
    is_retry = record is not None

    if publisher.dry_run:
        # dry-run：走完 prepare 与幂等查重，但既不写记录也不改状态机，更不碰平台
        if record is not None and record.phase == PublishPhase.DONE:
            return PublishResult(
                ok=True,
                platform_post_id=record.platform_post_id,
                url=record.url,
                raw={"idempotent_hit": True, "dry_run": True, "idem_key": idem_key},
            )
        return publisher.publish(bundle)

    if record is None:
        record = PublishRecord(
            id=new_id("pub"),
            content_item_id=item.id,
            idem_key=idem_key,
            phase=PublishPhase.IN_FLIGHT.value,
            attempts=0,
        )
        try:
            with session.begin_nested():  # SAVEPOINT：冲突只回滚这条 INSERT
                session.add(record)
        except IntegrityError:
            # 并发下另一个 worker 抢先写入了同一个 idem_key → 转对账路径
            record = _record_for(session, idem_key)
            is_retry = True
            if record is None:  # pragma: no cover - 理论上不可达
                raise RuntimeError(f"幂等记录丢失: {idem_key}") from None

    if record.phase == PublishPhase.DONE:
        # 已经发过：绝不再次触发平台写操作。但眼下这条 item 自己的状态机必须跟着
        # 推进——常见成因是"同账号 + 同一分钟槽位 + 内容哈希相同"两条内容撞了同一个
        # idem_key（幂等键不含 content_item_id，见 make_idem_key），record.content_item_id
        # 归先抢到记录的那条内容，可能不是眼下这条 item。此前这里直接 return，item
        # 永远卡在 scheduled/retrying 出不来，tick_scheduled_publish 却仍把它计入
        # published（P16.1 修复：内容卡死 + 计数虚增）。
        #
        # 不改 `record` 本身（phase/platform_post_id/updated_at 都不动）：它可能属于
        # 另一条内容，动了会让那条内容的"刚刚发布"时间被这次对账悄悄顶掉。
        result = PublishResult(
            ok=True,
            platform_post_id=record.platform_post_id,
            url=record.url,
            raw={"idempotent_hit": True, "idem_key": idem_key},
        )
        if item.status != ContentStatus.PUBLISHED:
            transition(item, ContentStatus.PUBLISHING)
            transition(item, ContentStatus.PUBLISHED)
            log_review(
                session,
                item,
                actor="system",
                action=SystemAction.RECONCILED,
                reason=record.url or record.platform_post_id or f"复用幂等记录 {record.id}",
                after={
                    "platform_post_id": record.platform_post_id,
                    "url": record.url,
                    "idem_key": idem_key,
                    "reused_record_id": record.id,
                },
            )
        return result

    if is_retry:
        # 幂等键已存在 → 上一次发布结果未知，先做平台侧对账，防"发成功但回包丢失"重复发。
        # ``reconcile`` 一样是网络调用，一样要在放掉写锁之后跑：调度器是"一个
        # session_scope 扫一批"，轮到这一条时锁多半已经被同一轮里前面那条内容的写
        # 攥在手里了
        _commit_before_platform(session)
        reconciled = publisher.reconcile(bundle)
        if reconciled is not None and reconciled.ok:
            transition(item, ContentStatus.PUBLISHING)
            return _finish_done(session, item, record, reconciled, action=SystemAction.RECONCILED)

    record.attempts += 1
    record.phase = PublishPhase.IN_FLIGHT.value
    record.updated_at = utcnow()
    transition(item, ContentStatus.PUBLISHING)
    _commit_before_platform(session)

    try:
        result = publisher.publish(bundle)
    except NeedsReloginError as exc:
        record.phase = PublishPhase.FAILED.value
        record.last_error = f"NeedsReloginError: {exc}"
        record.updated_at = utcnow()
        transition(item, ContentStatus.PUBLISH_FAILED)
        transition(item, ContentStatus.RETRYING)
        acc = account or session.get(Account, item.account_id)
        if acc is not None:
            mark_account_needs_relogin(session, acc, detail=str(exc), notifier=notifier)
        log_review(
            session,
            item,
            actor="system",
            action=SystemAction.PUBLISH_FAILED,
            reason=f"needs_relogin: {exc}",
        )
        _settle_after_platform(session, item)
        raise
    except RetryableError as exc:
        record.phase = PublishPhase.FAILED.value
        record.last_error = f"RetryableError: {exc}"
        record.updated_at = utcnow()
        transition(item, ContentStatus.PUBLISH_FAILED)
        if record.attempts >= max_attempts:
            transition(item, ContentStatus.DEAD_LETTER)
            log_review(
                session,
                item,
                actor="system",
                action=SystemAction.DEAD_LETTER,
                reason=f"重试 {record.attempts} 次仍失败: {exc}",
            )
            notifier.send(
                title=f"[死信] {item.id}",
                text=f"重试 {record.attempts} 次仍失败：{exc}",
                level="error",
            )
        else:
            transition(item, ContentStatus.RETRYING)
            log_review(
                session,
                item,
                actor="system",
                action=SystemAction.PUBLISH_FAILED,
                reason=f"第 {record.attempts}/{max_attempts} 次失败: {exc}",
            )
        _settle_after_platform(session, item)
        raise
    except Exception as exc:  # PermanentError 及一切未分类异常都按不可重试处理
        record.phase = PublishPhase.FAILED.value
        kind = "PermanentError" if isinstance(exc, PermanentError) else type(exc).__name__
        record.last_error = f"{kind}: {exc}"
        record.updated_at = utcnow()
        transition(item, ContentStatus.PUBLISH_FAILED)
        transition(item, ContentStatus.DEAD_LETTER)
        log_review(
            session,
            item,
            actor="system",
            action=SystemAction.DEAD_LETTER,
            reason=record.last_error,
        )
        _settle_after_platform(session, item)
        notifier.send(title=f"[发布失败] {item.id}", text=record.last_error, level="error")
        raise

    if not result.ok:
        # 非 dry-run 却返回 ok=False：契约要求失败必须抛异常，这里按可重试兜底
        record.phase = PublishPhase.FAILED.value
        record.last_error = "publisher 返回 ok=False（违反契约，按可重试处理）"
        record.updated_at = utcnow()
        transition(item, ContentStatus.PUBLISH_FAILED)
        transition(
            item,
            ContentStatus.DEAD_LETTER
            if record.attempts >= max_attempts
            else ContentStatus.RETRYING,
        )
        _settle_after_platform(session, item)
        # 下面这条 warning + 通知是**防御性**的，因为这道兜底**当前不可达**——
        # 由两条事实**合起来**（缺一不可，不是各自独立的两条证明）：
        #   (1) 现有五个发布器（FakePublisher 与 xhs / wechat_mp 官方 / wenyan /
        #       douyin）里所有 `PublishResult(ok=False)` 无一例外都在各自的
        #       `if self.dry_run:` 分支内，非 dry-run 路径上一条都没有
        #       → 「拿到 ok=False」蕴含「publisher.dry_run 为真」；
        #   (2) 本函数上面的 `if publisher.dry_run:` 已经提前 return
        #       → 「dry_run 为真」蕴含「根本走不到这一行」。
        #   合起来：走到这里 → 非 dry-run → 只可能拿到 ok=True。即「返回 ok=False」
        #   与「能走到这道兜底」在现有代码里**互斥**。两条里任何一条被将来的改动
        #   破掉（尤其第 (1) 条），这道兜底立刻变成活路径。
        # 那为什么还要告警：它一旦真的响了，含义是**某个发布器把 publishers/base.py
        # 的 publish 契约改坏了**（契约：失败必须抛 PublishError 的子类，不允许用
        # ok=False 表达异常），**不是**「某一次发布失败了」——普通发布失败一定抛异常、
        # 由上面三个 except 分流，通知与日志里必带异常类名。措辞必须让读日志的人
        # 一眼分清这两件事，否则这条告警等于没有。见 docs/RISKS.md 第 13 条。
        # 只加可观测性：上面的状态流转、record 字段与下面的 return 值一字未动。
        logger.warning(
            "发布器违反 publish 契约：%s.publish() 返回 ok=False 而不是抛 PublishError。"
            "这不是一次普通发布失败（普通失败会抛异常、由上面三个 except 分流、带异常类名），"
            "是这个发布器的实现坏了；本条内容已按可重试兜底处理。"
            "item=%s account=%s platform=%s attempts=%s/%s status=%s "
            "post_id=%r url=%r raw_keys=%s",
            type(publisher).__name__,
            item.id,
            item.account_id,
            bundle.platform,
            record.attempts,
            max_attempts,
            item.status,
            result.platform_post_id,
            result.url,
            sorted(result.raw),
        )
        notifier.send(
            title=f"[契约违规] {item.id}",
            text=(
                f"{type(publisher).__name__}.publish() 返回 ok=False 而不是抛 PublishError，"
                f"违反 publishers/base.py 的 publish 契约。这不是一次普通发布失败，"
                f"是这个发布器的实现坏了——去查最近一次改它的改动。"
                f"内容已按可重试兜底：第 {record.attempts}/{max_attempts} 次，落到 {item.status}。"
                f"platform={bundle.platform} account={item.account_id}"
            ),
            level="error",
        )
        return result

    finished = _finish_done(session, item, record, result, action=SystemAction.PUBLISH)
    # 平台侧已经发出去了，"我们知道它发出去了"必须先落盘再去发通知：这一步之后
    # 就算调用方整轮回滚，也不会退回一条 in_flight 的孤儿声明
    _settle_after_platform(session, item)
    notifier.send(
        title=f"[已发布] {bundle.title}",
        text=result.url or result.platform_post_id or "",
        level="info",
    )
    return finished


def diff_bundles(before: dict[str, Any] | None, after: dict[str, Any] | None) -> str:
    """给审核 UI 用的文本 diff。"""
    left = json.dumps(before or {}, ensure_ascii=False, indent=2, sort_keys=True).splitlines()
    right = json.dumps(after or {}, ensure_ascii=False, indent=2, sort_keys=True).splitlines()
    return "\n".join(
        difflib.unified_diff(left, right, fromfile="改前", tofile="改后", lineterm="", n=2)
    )


__all__ = [
    "ACCOUNT_TRANSITIONS",
    "CONTENT_TRANSITIONS",
    "REVIEW_QUEUE_STATUSES",
    "AccountStatus",
    "ContentStatus",
    "IllegalTransition",
    "PublishError",
    "PublishPhase",
    "ReviewAction",
    "SystemAction",
    "apply_health",
    "approve",
    "can_transition",
    "deactivate_account",
    "diff_bundles",
    "edit",
    "log_review",
    "make_idem_key",
    "mark_account_needs_relogin",
    "publish_with_idempotency",
    "reactivate_account",
    "reject",
    "restore_account",
    "restore_suspended_items",
    "slot_of",
    "suspend_scheduled_items",
    "transition",
    "transition_account",
]
