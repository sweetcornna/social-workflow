"""短信验证码人工输入通道（内存队列）。

红线：这里只做"人在 UI 里输入 → 发布器取用"的**转发**，
**绝不**做任何验证码自动识别 / 打码平台对接（见 docs/POLICY.md）。
验证码不落库、不写日志明文，进程重启即丢失，这是刻意设计。
"""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from datetime import UTC, datetime


class SmsInbox:
    """按账号维护一个短小的验证码队列，线程安全。"""

    def __init__(self, maxlen: int = 5) -> None:
        self._lock = threading.Lock()
        self._queues: dict[str, deque[tuple[str, datetime]]] = defaultdict(
            lambda: deque(maxlen=maxlen)
        )

    def put(self, account_id: str, code: str) -> None:
        code = code.strip()
        if not code:
            raise ValueError("验证码不能为空")
        with self._lock:
            self._queues[account_id].append((code, datetime.now(UTC)))

    def pop(self, account_id: str, *, max_age_seconds: float = 300.0) -> str | None:
        """发布器取用：返回最早的未过期验证码，无则 None。"""
        now = datetime.now(UTC)
        with self._lock:
            queue = self._queues.get(account_id)
            while queue:
                code, at = queue.popleft()
                if (now - at).total_seconds() <= max_age_seconds:
                    return code
        return None

    def pending(self, account_id: str) -> int:
        with self._lock:
            return len(self._queues.get(account_id, ()))

    def clear(self, account_id: str | None = None) -> None:
        with self._lock:
            if account_id is None:
                self._queues.clear()
            else:
                self._queues.pop(account_id, None)


SMS_INBOX = SmsInbox()

__all__ = ["SMS_INBOX", "SmsInbox"]
