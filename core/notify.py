"""通知通道：待审核 / 需重登 / 发布失败 / 成本超限。

节流层（P12）
------------
长期无人值守的通知有个致命的失效模式：登录巡检每 10 分钟跑一次，掉线后每一轮都推
一条"你的号掉线了"，一天上百条。用户的第一反应不是去扫码，而是**把这个 bot 静音**
——通知通道就此整体失效，比没有通知更糟。

所以除了一次性事件（死信、发布失败），凡是"会被定时任务反复触发"的通知都必须走
:func:`notify_event`：同一 ``(account_id, event_kind)`` 在
``SW_NOTIFY_THROTTLE_MINUTES``（默认 120）内只推一次。节流状态是**进程内**的
（:data:`NOTIFY_THROTTLE`），重启后会重推一次——这是刻意的：重启多半意味着人正在
处理，那一条提醒是有用的，而把它持久化的代价（又一张表）不成比例。
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

import httpx

logger = logging.getLogger("social_workflow.notify")

LEVELS = ("info", "warning", "error")


@runtime_checkable
class Notifier(Protocol):
    """通知协议。实现必须吞掉自身异常，通知失败不能拖垮主流程。"""

    def send(self, title: str, text: str, level: str = "info") -> bool: ...


class LogNotifier:
    """无 webhook 时的兜底实现：只写日志。"""

    def __init__(self, logger_: logging.Logger | None = None) -> None:
        self._logger = logger_ or logger
        self.sent: list[tuple[str, str, str]] = []

    def send(self, title: str, text: str, level: str = "info") -> bool:
        self.sent.append((level, title, text))
        log_fn = {
            "error": self._logger.error,
            "warning": self._logger.warning,
        }.get(level, self._logger.info)
        log_fn("[%s] %s | %s", level, title, text.replace("\n", " ⏎ "))
        return True


class FeishuWebhookNotifier:
    """飞书自定义机器人（text 消息）。企微 webhook 结构类似，P4 再抽象。"""

    def __init__(self, webhook_url: str, *, timeout: float = 5.0) -> None:
        if not webhook_url:
            raise ValueError("webhook_url 不能为空")
        self.webhook_url = webhook_url
        self.timeout = timeout

    def send(self, title: str, text: str, level: str = "info") -> bool:
        prefix = {"error": "🔴", "warning": "🟠"}.get(level, "🔵")
        payload = {
            "msg_type": "text",
            "content": {"text": f"{prefix} {title}\n{text}"},
        }
        try:
            resp = httpx.post(self.webhook_url, json=payload, timeout=self.timeout)
            resp.raise_for_status()
        except Exception as exc:  # 通知失败不影响主流程
            logger.warning("飞书通知发送失败: %s", exc)
            return False
        return True


class MultiNotifier:
    """扇出到多个通道。"""

    def __init__(self, *notifiers: Notifier) -> None:
        self.notifiers = list(notifiers)

    def send(self, title: str, text: str, level: str = "info") -> bool:
        results = [n.send(title, text, level) for n in self.notifiers]
        return all(results) if results else False


# --------------------------------------------------------------------- 节流层


class NotifyThrottle:
    """按 key 的"这么久内只放一条"闸门。线程安全（调度器是多线程的）。

    刻意做成**通用**的 key→时刻表而不是绑死在账号上：登录掉线按
    ``(account_id, needs_relogin)`` 节流，成本闸门按 ``(全局, budget_tokens)`` 节流，
    形状一样，没必要写两遍。
    """

    def __init__(self) -> None:
        self._last: dict[str, datetime] = {}
        self._lock = threading.Lock()

    @staticmethod
    def key_of(account_id: str | None, kind: str) -> str:
        return f"{account_id or '-'}|{kind}"

    def allow(self, key: str, *, window: timedelta, now: datetime | None = None) -> bool:
        """这条能不能推。放行时**顺手记账**，所以同一个 key 并发只会有一个拿到 True。"""
        moment = now or datetime.now(UTC)
        if window <= timedelta(0):
            return True
        with self._lock:
            last = self._last.get(key)
            if last is not None and moment - last < window:
                return False
            self._last[key] = moment
            return True

    def mark(self, key: str, *, now: datetime | None = None) -> None:
        """手工记一次（推送不走 :meth:`allow` 时用，例如确认卡自己有去重逻辑）。"""
        with self._lock:
            self._last[key] = now or datetime.now(UTC)

    def reset(self, key: str | None = None) -> None:
        """清节流状态。``key=None`` 清全部（测试 / 人工强制重推）。"""
        with self._lock:
            if key is None:
                self._last.clear()
            else:
                self._last.pop(key, None)

    def snapshot(self) -> dict[str, datetime]:
        with self._lock:
            return dict(self._last)


#: 进程内共享的节流器。与 ``RATE_LIMITER`` 一样是模块级单例，
#: 测试要在夹具里 ``reset()``，否则会跨用例串味
NOTIFY_THROTTLE = NotifyThrottle()


def throttle_window() -> timedelta:
    from core.config import get_settings

    return timedelta(minutes=max(get_settings().sw_notify_throttle_minutes, 0))


def notify_event(
    title: str,
    text: str,
    *,
    kind: str,
    account_id: str | None = None,
    level: str = "info",
    notifier: Notifier | None = None,
    now: datetime | None = None,
    window: timedelta | None = None,
    throttle: NotifyThrottle | None = None,
) -> bool:
    """带节流的通知。被节流时返回 ``False`` 并只留一条 debug 日志。

    ``kind`` 是事件种类（``needs_relogin`` / ``budget_tokens`` / ``review_blocked`` …），
    与 ``account_id`` 一起构成节流 key。一次性事件（死信、发布失败）**不要**走这里，
    它们每条都值得看见。
    """
    gate = throttle or NOTIFY_THROTTLE
    key = NotifyThrottle.key_of(account_id, kind)
    if not gate.allow(key, window=window if window is not None else throttle_window(), now=now):
        logger.debug("通知被节流 key=%s title=%s", key, title)
        return False
    target = notifier or get_default_notifier()
    return target.send(title, text, level)


def public_url(path: str) -> str:
    """把 ``/review/xxx`` 拼成对外可点的绝对地址。

    没配 ``SW_PUBLIC_BASE_URL`` 时原样返回相对路径——在 Telegram 里点不动，
    但比拼出一个错的域名诚实。
    """
    from core.config import get_settings

    base = (get_settings().sw_public_base_url or "").strip().rstrip("/")
    if not base:
        return path
    return f"{base}/{path.lstrip('/')}"


# ------------------------------------------------------------------ 默认通道

_default: Notifier | None = None


def build_notifier(webhook_url: str | None = None) -> Notifier:
    """按配置拼出通知通道：飞书 / Telegram / 日志，有几个用几个。

    日志通道**永远在**：它是审计与排障的落点，也是所有远端通道都挂掉时的兜底。
    """
    from core.telegram import get_telegram_channel

    channels: list[Notifier] = []
    if webhook_url:
        channels.append(FeishuWebhookNotifier(webhook_url))
    # 与确认卡共用同一个实例：分成两份的话"今日推了几条"谁也不对
    telegram = get_telegram_channel()
    if telegram is not None:
        channels.append(telegram)
    channels.append(LogNotifier())
    return channels[0] if len(channels) == 1 else MultiNotifier(*channels)


def get_default_notifier() -> Notifier:
    global _default
    if _default is None:
        from core.config import get_settings

        _default = build_notifier(get_settings().feishu_webhook or None)
    return _default


def set_default_notifier(notifier: Notifier | None) -> None:
    """测试 / 启动时替换默认通知器。"""
    global _default
    _default = notifier


__all__ = [
    "NOTIFY_THROTTLE",
    "FeishuWebhookNotifier",
    "LogNotifier",
    "MultiNotifier",
    "Notifier",
    "NotifyThrottle",
    "build_notifier",
    "get_default_notifier",
    "notify_event",
    "public_url",
    "set_default_notifier",
    "throttle_window",
]
