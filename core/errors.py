"""带机器可读 ``code`` 的 HTTP 错误。

为什么不直接用 ``HTTPException``
--------------------------------
``/api/v1`` 的契约要求错误体是 ``{"ok": false, "data": null, "error": {"code", "message"}}``：
前端要按 ``code`` 分支（``watch_required`` 弹观看提示、``invalid_slot`` 高亮时间输入框），
而 ``detail`` 那句中文只适合直接显示给人。

:class:`AppError` 仍然是 ``HTTPException`` 的子类，因此：

- 既有的 Jinja2/HTMX 端点抛它时行为**完全不变**（同样的 status_code + ``detail`` 文案）；
- ``/api/v1`` 的异常处理器（``core/api/common.py``）额外读出 ``code`` 与 ``error_detail``。

这样审核动作（``core/review_actions.py``）等共享逻辑只抛一种异常，两套门面各取所需。
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

#: HTTP 状态码 → 缺省错误码。没有显式 ``code`` 的异常（含 FastAPI 自己抛的）按这张表兜底
DEFAULT_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "unprocessable",
    500: "internal_error",
    501: "not_supported",
    502: "upstream_error",
    503: "unavailable",
}


def code_for(status_code: int) -> str:
    return DEFAULT_CODES.get(status_code, f"http_{status_code}")


class AppError(HTTPException):
    """带 ``code`` 的 HTTP 错误。``detail`` 是给人看的中文，``code`` 是给前端分支用的。"""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=message)
        self.code = code
        self.message = message
        #: 结构化补充信息（如 reschedule 失败时的"最近合法槽位"），进 envelope 的 ``error.detail``
        self.error_detail = detail


__all__ = ["DEFAULT_CODES", "AppError", "code_for"]
