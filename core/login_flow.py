"""人工介入登录的共享编排：取二维码 / 查状态 / 开宿主机窗口 / 递验证码。

原先这四段逻辑写在 ``core/main.py`` 的路由闭包里。P6 的工作台要用 JSON 走同一条路
（小红书二维码 base64、抖音宿主机窗口状态机原样透传），所以抽到这里，两个门面只做
"取参数 + 包响应"。

红线（docs/POLICY.md）不变：只提供"把二维码显示给人、把人输入的验证码转发给发布器"
的通道，**没有任何自动打码 / 验证码识别**；验证码不落库、不写日志明文。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from core.errors import AppError
from core.models import Account, utcnow
from core.sms_inbox import SMS_INBOX
from core.state_machine import AccountStatus, IllegalTransition, apply_health
from publishers.base import (
    AccountHealth,
    PublisherNotAvailable,
    PublishError,
    SupportsInteractiveLogin,
)
from publishers.registry import get_publisher

logger = logging.getLogger("social_workflow.login")


def supports_interactive_login(account: Account) -> bool:
    """该账号的发布器是否支持扫码登录（登录页据此决定显示什么）。"""
    try:
        publisher = get_publisher(account.platform, account.id)
    except PublisherNotAvailable:
        return False
    return isinstance(publisher, SupportsInteractiveLogin)


def login_publisher(account: Account) -> SupportsInteractiveLogin:
    """取支持扫码登录的发布器。未注册 503，平台不支持 501。"""
    try:
        publisher = get_publisher(account.platform, account.id)
    except PublisherNotAvailable as exc:
        raise AppError(503, "publisher_unavailable", str(exc)) from exc
    if not isinstance(publisher, SupportsInteractiveLogin):
        raise AppError(501, "not_supported", f"平台 {account.platform} 的发布器不支持扫码登录")
    return publisher


def sync_login_health(
    session: Session, account: Account, publisher: SupportsInteractiveLogin
) -> AccountHealth:
    """轮询登录结果并把它落到 Account 状态机。

    扫码成功后账号自动回 ``ok``，被挂起的排期项也会一并放回
    （``apply_health`` → ``restore_account``），不需要人再去手工改状态。
    """
    health = publisher.check_login_status()
    # banned 是人工终态，suspended 是人工停用：两者都不许被巡检结果改写
    if account.status not in (AccountStatus.BANNED, AccountStatus.SUSPENDED):
        try:
            apply_health(session, account, health.status, detail=health.detail)
            session.commit()
        except IllegalTransition as exc:  # pragma: no cover - 状态表已覆盖全部组合
            session.rollback()
            logger.warning("登录巡检状态落库失败 %s: %s", account.id, exc)
    return health


def qrcode_payload(session: Session, account: Account) -> dict[str, Any]:
    """登录二维码 + 当前状态。

    小红书走**真实 sidecar**（``GET /api/v1/login/qrcode``）；其它平台或
    ``SW_USE_FAKE_PUBLISHERS=true`` 时仍是 FakePublisher 的 P0 占位图。
    二维码只呈现给人扫，系统不做任何自动识别（docs/POLICY.md）。
    """
    publisher = login_publisher(account)
    try:
        detail_fn = getattr(publisher, "get_login_qrcode_detail", None)
        if callable(detail_fn):
            # 可选能力：真实 sidecar 会一并给出二维码有效期（Go duration，如 4m0s）
            info = detail_fn()
            image_base64 = str(info.get("image_base64") or "")
            expires_in = int(float(info.get("timeout_seconds") or 120))
            placeholder = bool(info.get("placeholder"))
        else:
            image_base64 = publisher.get_login_qrcode()
            expires_in = 120
            placeholder = True
        health = sync_login_health(session, account, publisher)
    except PublishError as exc:
        raise AppError(502, "upstream_error", f"取二维码失败: {exc}") from exc
    return {
        "account_id": account.id,
        "platform": account.platform,
        "image_base64": image_base64,
        "status": health.status,
        "detail": health.detail,
        "account_status": account.status,
        "placeholder": placeholder,
        "expires_in": expires_in,
        "fetched_at": utcnow().isoformat(),
    }


def status_payload(session: Session, account: Account) -> dict[str, Any]:
    """只查登录状态（不重新取二维码），并把结果落到 Account 状态机。"""
    publisher = login_publisher(account)
    try:
        health = sync_login_health(session, account, publisher)
    except PublishError as exc:
        raise AppError(502, "upstream_error", f"查登录状态失败: {exc}") from exc
    return {
        "account_id": account.id,
        "platform": account.platform,
        "status": health.status,
        "detail": health.detail,
        "account_status": account.status,
        "logged_in": health.status == AccountStatus.OK,
        "checked_at": utcnow().isoformat(),
    }


def start_payload(account: Account) -> dict[str, Any]:
    """让发布器把登录窗口开起来（**可选能力**，用 hasattr 探测）。

    小红书走 core 代理的二维码，不需要这个；抖音的浏览器在宿主机上，
    必须先让那边把创作者中心窗口弹出来，人才有得扫。
    红线：这里只负责"开窗口"，扫码 / 输码全程由真人完成。
    """
    publisher = login_publisher(account)
    start = getattr(publisher, "start_login", None)
    if not callable(start):
        raise AppError(
            501,
            "not_supported",
            f"平台 {account.platform} 的发布器没有「打开登录窗口」这一步",
        )
    try:
        info = start()
    except PublishError as exc:
        raise AppError(502, "upstream_error", f"打开登录窗口失败: {exc}") from exc
    return {
        "ok": True,
        "account_id": account.id,
        "platform": account.platform,
        "state": str(info.get("state") or ""),
        "detail": str(info.get("detail") or ""),
        "started_at": utcnow().isoformat(),
    }


def submit_code(account: Account, code: str) -> dict[str, Any]:
    """把人工输入的短信验证码放进内存队列，并**顺手转发**给发布器。

    两条路都留着：

    - 内存队列（``core/sms_inbox.py``）：发布器随时可以 ``pop`` 取用；
    - 直接转发（``publisher.submit_sms_code``）：抖音上传器要把验证码**填进**
      宿主机浏览器里那个正等着的输入框，晚一步页面就超时了。

    转发失败不算错误（多半是"页面上现在没有验证码框"），只把结果透出来，
    队列里的那份仍然有效。验证码**不落库、不写日志明文**（docs/POLICY.md）。
    """
    try:
        SMS_INBOX.put(account.id, code)
    except ValueError as exc:
        raise AppError(422, "invalid_code", str(exc)) from exc
    pending = SMS_INBOX.pending(account.id)

    forwarded = False
    forward_detail = ""
    try:
        publisher = get_publisher(account.platform, account.id)
    except PublisherNotAvailable as exc:
        forward_detail = f"发布器未注册：{exc}"
        publisher = None
    if publisher is not None:
        submit = getattr(publisher, "submit_sms_code", None)
        if not callable(submit):
            forward_detail = f"平台 {account.platform} 的发布器不接受短信验证码"
        else:
            try:
                forwarded = bool(submit(code))
                forward_detail = "已填入发布器当前页面" if forwarded else "发布器未确认填入"
            except PublishError as exc:
                # 异常文案里绝不能带验证码：submit_sms_code 的实现不回显入参
                forward_detail = f"转发失败：{exc}"
            except Exception as exc:  # pragma: no cover - 发布器实现异常
                logger.warning("转发验证码到 %s 失败：%s", account.platform, type(exc).__name__)
                forward_detail = f"转发失败：{type(exc).__name__}"

    return {
        "ok": True,
        "account_id": account.id,
        "pending": pending,
        "forwarded": forwarded,
        "forward_detail": forward_detail,
    }


__all__ = [
    "login_publisher",
    "qrcode_payload",
    "start_payload",
    "status_payload",
    "submit_code",
    "supports_interactive_login",
    "sync_login_health",
]
