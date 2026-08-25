"""``/api/v1`` 的公共件：统一 envelope、分页、可选 token 认证、错误处理器。

契约（前端只要认这三条就够了，详见 docs/WORKBENCH_API.md）：

1. **每个响应都是** ``{"ok": bool, "data": ..., "error": {"code","message"} | null}``；
2. **列表都是** ``{"items": [...], "total": N, "limit": L, "offset": O}``，
   翻页用 ``?limit=&offset=``；
3. **时间都是 UTC ISO8601**（``2026-08-16T09:30:00+00:00``），前端自己转本地时区显示。
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, Header, Query, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from core import db
from core.config import get_settings
from core.errors import AppError, code_for
from core.models import Account, ContentItem

#: 所有工作台端点的前缀。错误处理器靠它区分"该包 envelope"与"HTML 页面原样放行"
API_PREFIX = "/api/v1"

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


# --------------------------------------------------------------------- envelope


class ApiErrorBody(BaseModel):
    """错误体。``code`` 给前端分支，``message`` 直接显示给人，``detail`` 是结构化补充。"""

    code: str
    message: str
    detail: dict[str, Any] | None = None


class Envelope[DataT](BaseModel):
    """统一响应外壳。成功时 ``error`` 为 null，失败时 ``data`` 为 null。"""

    ok: bool = True
    data: DataT | None = None
    error: ApiErrorBody | None = None


class Page[DataT](BaseModel):
    """统一分页体。``total`` 是**过滤后的总数**，与 ``limit/offset`` 无关。"""

    items: list[DataT] = Field(default_factory=list)
    total: int = 0
    limit: int = DEFAULT_LIMIT
    offset: int = 0


def ok[DataT](data: DataT) -> Envelope[DataT]:
    return Envelope(ok=True, data=data, error=None)


def error_body(exc: Exception, status_code: int, message: str) -> dict[str, Any]:
    code = getattr(exc, "code", None) or code_for(status_code)
    return {
        "ok": False,
        "data": None,
        "error": {
            "code": code,
            "message": message,
            "detail": getattr(exc, "error_detail", None),
        },
    }


# ------------------------------------------------------------------- 分页 / 时间


@dataclass(frozen=True)
class Pagination:
    limit: int
    offset: int


def pagination(
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="每页条数，最大 200"),
    offset: int = Query(0, ge=0, description="跳过多少条"),
) -> Pagination:
    return Pagination(limit=limit, offset=offset)


PageParams = Depends(pagination)


def count_of(session: Session, stmt: Select[Any]) -> int:
    """数一条 SELECT 的行数（不动它的 where/join）。"""
    return int(session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)


def slice_of(stmt: Select[Any], page: Pagination) -> Select[Any]:
    return stmt.limit(page.limit).offset(page.offset)


def aware(value: datetime | None) -> datetime | None:
    """DB 里理论上都是 aware UTC，这里兜一道底，免得序列化出没有时区的串。"""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def safe_dt(value: object) -> datetime | None:
    """把"库里存着的时间串"变成 aware datetime。解析不了就当没有，不让展示端点 500。

    用在 ``Account.extra['insights_updated_at']`` / ``Topic.raw['dismissed']['at']``
    这类**人也可能手改**的 JSON 字段上。
    """
    if isinstance(value, datetime):
        return aware(value)
    if not value:
        return None
    try:
        return aware(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


def parse_dt(value: str | datetime | None, *, field: str) -> datetime | None:
    """解析前端传来的 ISO8601 时间。不带时区一律按 UTC。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return aware(value)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise AppError(
            422, "invalid_datetime", f"{field} 不是合法的 ISO8601 时间：{value!r}"
        ) from exc
    return aware(parsed)


# ------------------------------------------------------------------- 常用取数


def get_item(session: Session, item_id: str) -> ContentItem:
    item = session.get(ContentItem, item_id)
    if item is None:
        raise AppError(404, "not_found", f"内容项不存在: {item_id}")
    return item


def get_account(session: Session, account_id: str) -> Account:
    account = session.get(Account, account_id)
    if account is None:
        raise AppError(404, "not_found", f"账号不存在: {account_id}")
    return account


DbSession = Depends(db.get_db)


# ----------------------------------------------------------------------- 认证


def configured_token() -> str:
    """``SW_UI_TOKEN``。空串 = 不鉴权（本机 ops 工具的默认形态）。"""
    return (get_settings().sw_ui_token or "").strip()


def auth_required() -> bool:
    return bool(configured_token())


def token_matches(candidate: str) -> bool:
    """常量时间比较，避免按字符逐位试探。未配置 token 时一律放行。"""
    expected = configured_token()
    if not expected:
        return True
    return secrets.compare_digest(candidate.strip(), expected)


def require_token(authorization: str | None = Header(default=None)) -> None:
    """``/api/v1/*`` 的守卫。``SW_UI_TOKEN`` 为空时直接放行。

    只认 ``Authorization: Bearer <token>``：不接受 ``?token=`` 查询参数——那会被写进
    nginx access log 与浏览器历史。缺头、格式不对、token 不匹配一律 401 ``unauthorized``。
    """
    if not auth_required():
        return
    if not authorization:
        raise AppError(401, "unauthorized", "缺少 Authorization: Bearer <token>")
    scheme, _, candidate = authorization.partition(" ")
    if scheme.lower() != "bearer" or not candidate.strip():
        raise AppError(401, "unauthorized", "Authorization 头必须是 Bearer <token> 形式")
    if not token_matches(candidate):
        raise AppError(401, "unauthorized", "token 不正确")


AuthGuard = Depends(require_token)


# ------------------------------------------------------------------ 错误处理器


async def _http_error(request: Request, exc: Exception) -> Response:
    """把 ``/api/v1`` 下的 HTTPException 包成 envelope；其它路径原样交还 FastAPI。

    HTML 页面（``/review`` 等）与既有测试仍然看到 ``{"detail": "..."}``，一个字节没变。
    """
    assert isinstance(exc, StarletteHTTPException)
    if not request.url.path.startswith(API_PREFIX):
        return await http_exception_handler(request, exc)
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return JSONResponse(
        error_body(exc, exc.status_code, detail),
        status_code=exc.status_code,
        headers=getattr(exc, "headers", None),
    )


async def _validation_error(request: Request, exc: Exception) -> Response:
    """FastAPI 的入参校验失败（422）也要走 envelope，前端才不用分两套解析。"""
    assert isinstance(exc, RequestValidationError)
    if not request.url.path.startswith(API_PREFIX):
        return await request_validation_exception_handler(request, exc)
    errors = exc.errors()
    first = errors[0] if errors else {}
    where = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
    message = f"{where}: {first.get('msg', '入参不合法')}" if where else "入参不合法"
    return JSONResponse(
        {
            "ok": False,
            "data": None,
            "error": {
                "code": "validation_error",
                "message": message,
                # 原始报文留着：前端调试期比一句中文有用得多
                "detail": {"errors": _jsonable_errors(errors)},
            },
        },
        status_code=422,
    )


def _jsonable_errors(errors: list[Any]) -> list[dict[str, Any]]:
    """pydantic 的 ``ctx`` 里可能塞着异常对象，JSON 序列化不了，统一转字符串。"""
    out: list[dict[str, Any]] = []
    for err in errors:
        item = {k: v for k, v in dict(err).items() if k != "ctx"}
        item["loc"] = [str(part) for part in err.get("loc", ())]
        out.append(item)
    return out


def install_error_handlers(app: Any) -> None:
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)


__all__ = [
    "API_PREFIX",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "ApiErrorBody",
    "AuthGuard",
    "DbSession",
    "Envelope",
    "Page",
    "PageParams",
    "Pagination",
    "auth_required",
    "aware",
    "configured_token",
    "count_of",
    "get_account",
    "get_item",
    "install_error_handlers",
    "ok",
    "pagination",
    "parse_dt",
    "require_token",
    "safe_dt",
    "slice_of",
    "token_matches",
]
