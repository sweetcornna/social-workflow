"""小红书登录态巡检：把 sidecar 的登录状态落到 Account 状态机上。

小红书 cookies 大约数周过期一次，且**同一账号不允许多网页端同时登录**
（另一处登录会把 sidecar 顶下线）。等到发布那一刻才发现掉线，排期就已经错过了，
所以由 ``core.scheduler.tick_login_health`` 每 10 分钟主动巡检一次。

状态流转全部交给 ``core.state_machine.apply_health``：

- ``needs_relogin`` → 账号置 ``needs_relogin`` + 挂起该账号所有 ``scheduled`` 项 + 通知
- 从 ``needs_relogin`` 回到 ``ok`` → 恢复账号 + 把挂起项放回原状态 + 通知
- ``degraded``（sidecar 挂了 / 浏览器抖动）→ 只改账号状态，**不挂起**排期项

红线：这里只做"检测 + 通知人去扫码"，不含任何自动登录 / 验证码识别（docs/POLICY.md）。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.models import Account
from core.notify import Notifier
from core.state_machine import AccountStatus, apply_health
from publishers.base import AccountHealth, Publisher, PublisherNotAvailable, PublishError
from publishers.registry import get_publisher

logger = logging.getLogger("social_workflow.publishers.xhs")


def check_and_mark(
    session: Session,
    account: Account,
    *,
    publisher: Publisher | None = None,
    notifier: Notifier | None = None,
) -> AccountHealth | None:
    """巡检一个账号的登录态并落库。返回本次巡检结果。

    调用方负责 commit（``tick_login_health`` 用 ``session_scope`` 统一提交）。

    两种情况**不巡检**：``banned`` 是人工终态（状态机不允许自动改写），
    ``suspended`` 是人工停用（P10）——人明确关掉的号，巡检不能悄悄把它打开。
    停用返回 ``None`` 而不是编一个 ``AccountHealth``：``AccountHealth.status``
    是发布器契约里的四值 Literal，"被人停用"不是发布器能观察到的事实，
    不该往那个枚举里塞。
    """
    if account.status == AccountStatus.SUSPENDED:
        return None
    if account.status == AccountStatus.BANNED:
        return AccountHealth(status="banned", detail="人工终态，巡检跳过")

    if publisher is None:
        try:
            publisher = get_publisher(account.platform, account.id)
        except PublisherNotAvailable as exc:
            return AccountHealth(status="degraded", detail=str(exc))

    try:
        health = publisher.health()
    except PublishError as exc:
        # health() 本不该抛，抛了也按"暂时不可用"处理，绝不据此挂起排期
        health = AccountHealth(status="degraded", detail=f"health() 异常：{exc}")

    before = account.status
    apply_health(session, account, health.status, detail=health.detail, notifier=notifier)
    if before != account.status:
        logger.info(
            "账号 %s(%s) 登录态巡检：%s -> %s（%s）",
            account.id,
            account.platform,
            before,
            account.status,
            health.detail,
        )
    return health


def check_accounts(
    session: Session,
    *,
    platforms: Sequence[str] | None = None,
    notifier: Notifier | None = None,
) -> dict[str, int]:
    """批量巡检。``platforms=None`` 表示所有平台。返回按结果状态计数。

    人工停用的账号只计 ``suspended_skipped``，不进 ``checked``——它压根没被巡检，
    算进去会让"巡了几个号"这个数字骗人。
    """
    stmt = select(Account).order_by(Account.platform, Account.id)
    if platforms:
        stmt = stmt.where(Account.platform.in_(list(platforms)))
    stats: dict[str, int] = {"checked": 0}
    for account in session.scalars(stmt):
        health = check_and_mark(session, account, notifier=notifier)
        if health is None:
            stats["suspended_skipped"] = stats.get("suspended_skipped", 0) + 1
            continue
        stats["checked"] += 1
        stats[health.status] = stats.get(health.status, 0) + 1
    return stats


__all__ = ["check_accounts", "check_and_mark"]
