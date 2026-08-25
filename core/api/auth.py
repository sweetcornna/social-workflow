"""可选 token 认证的登录探针。

``SW_UI_TOKEN`` 为空（默认）时整个 ``/api/v1`` 不鉴权——它本来就是跑在本机 / 内网的
ops 工具。要暴露到公网就配上 token，前端拿它换一张"能用"的确认后，把
``Authorization: Bearer <token>`` 带在之后每个请求上。

本端点**不挂**认证守卫（否则登录页永远进不来），但错的 token 一样 401。
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from core.api.common import Envelope, auth_required, ok, token_matches
from core.errors import AppError

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginIn(BaseModel):
    token: str = Field(default="", description="SW_UI_TOKEN 的值；未开启鉴权时随便填")


class LoginOut(BaseModel):
    ok: bool = True
    #: 后端是否要求鉴权（``SW_UI_TOKEN`` 非空）。false 时前端可以直接进主界面
    auth_required: bool = False
    message: str = ""


@router.post("/login", summary="校验 token")
def login(payload: LoginIn) -> Envelope[LoginOut]:
    """token 正确回 ``ok=true``，错的回 401 ``unauthorized``。

    没配 ``SW_UI_TOKEN`` 时一律 ``ok=true`` 且 ``auth_required=false``——
    前端据此跳过登录页。
    """
    required = auth_required()
    if not token_matches(payload.token):
        raise AppError(401, "unauthorized", "token 不正确")
    return ok(
        LoginOut(
            ok=True,
            auth_required=required,
            message="已认证" if required else "本实例未开启 token 认证",
        )
    )


__all__ = ["router"]
