"""工作台 JSON API（``/api/v1``）—— 前端的唯一数据面。

设计取舍
--------
- **不碰既有页面**。Jinja2/HTMX 那套原样保留（并行期还要用），本包只新增路由；
  错误处理器按路径前缀区分，HTML 端点的 ``{"detail": ...}`` 一个字节没变。
- **不复制业务逻辑**。批准 / 驳回 / 改稿走 ``core/review_actions.py``，登录走
  ``core/login_flow.py``，排期走 ``core/scheduling.py``，统计走 ``core/stats.py``，
  门禁走 ``scripts/preflight.py``，tick 走 ``core.scheduler.TICKS``。
  API 层只做"取参数 → 调函数 → 包 envelope"。
- **认证可选**。``SW_UI_TOKEN`` 非空时全量要求 Bearer token（``/auth/login`` 除外）。

契约文档：docs/WORKBENCH_API.md（前端只看那一份就够）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from core.api import (
    accounts,
    auth,
    content,
    dashboard,
    insights,
    jobs,
    review,
    stats,
    system,
    topics,
)
from core.api.common import API_PREFIX, AuthGuard, install_error_handlers


def build_router() -> APIRouter:
    """组装 ``/api/v1``。除 ``/auth/login`` 外全部挂 :func:`~core.api.common.require_token`。"""
    api = APIRouter(prefix=API_PREFIX)
    # 登录探针不能挂守卫，否则前端永远没机会试探 token
    api.include_router(auth.router)

    guarded = APIRouter(dependencies=[AuthGuard])
    for module in (dashboard, review, accounts, content, topics, jobs, stats, insights, system):
        guarded.include_router(module.router)
    api.include_router(guarded)
    return api


def install_api(app: Any) -> None:
    """把 API 挂到 FastAPI 应用上（``core.main.create_app`` 调用）。"""
    install_error_handlers(app)
    app.include_router(build_router())


__all__ = ["API_PREFIX", "build_router", "install_api"]
