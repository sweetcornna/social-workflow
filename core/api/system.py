"""系统：门禁自检、tick 列表与手动触发、生图可用性、提醒渠道状态、运行时信息。

这里的端点都只是**包装既有实现**，本模块不放业务逻辑：``scripts/preflight.py`` 的检查
函数、``core.scheduler.TICKS`` 注册表、``generation.imagegen.imagegen_status`` +
``core.budget``、``core.telegram.channel_status``、``core.config.Settings``。手动触发的
tick 与 APScheduler 定时跑的是同一个函数，所以工作台上看到的行为就是生产行为。
``/imagegen`` 与 ``/telegram`` **都不发网络请求**（只读配置、进程内状态与账本），可以放进
页面加载路径。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.api.common import DbSession, Envelope, auth_required, ok
from core.config import get_settings
from core.errors import AppError
from core.models import utcnow

router = APIRouter(prefix="/system", tags=["system"])


class PreflightCheck(BaseModel):
    name: str
    #: OK / WARN / FAIL / SKIP
    status: str
    detail: str = ""


class PreflightOut(BaseModel):
    offline: bool = True
    passed: bool = True
    counts: dict[str, int] = Field(default_factory=dict)
    checks: list[PreflightCheck] = Field(default_factory=list)
    ran_at: datetime


class TickInfo(BaseModel):
    name: str
    #: 该 tick 支持哪些可选参数（其余参数会被 422 拒掉）
    accepts: list[str] = Field(default_factory=list)


class TicksOut(BaseModel):
    ticks: list[TickInfo] = Field(default_factory=list)
    note: str = "手动触发与定时任务走的是同一批函数（core.scheduler.TICKS）"


class TickRunOut(BaseModel):
    tick: str
    stats: dict[str, int] = Field(default_factory=dict)
    elapsed_s: float = 0.0


class SystemInfo(BaseModel):
    version: str
    env: str
    time: datetime
    timezone: str
    llm_backend: str
    llm_model: str
    database: str
    scheduler_enabled: bool
    use_fake_publishers: bool
    generate_enabled: bool
    publishers: list[str] = Field(default_factory=list)
    ticks: list[str] = Field(default_factory=list)
    #: 前端筛选下拉框的取值域，省得两边各抄一份枚举
    platforms: list[str] = Field(default_factory=list)
    content_statuses: list[str] = Field(default_factory=list)
    review_queue_statuses: list[str] = Field(default_factory=list)
    #: 是否配了 ``SW_UI_TOKEN``（前端据此决定要不要显示登录页）
    auth_required: bool = False
    budget: dict[str, float] = Field(default_factory=dict)


class TelegramOut(BaseModel):
    """提醒渠道（Telegram）的连通状态。**绝不含 token**，脱敏过的也不给。

    ``ready=false`` 时 ``detail`` 一定非空，而且必须是一句能照着做的话——
    前端要把它原样显示出来，不要自己编"未配置"三个字。
    """

    #: 通道总开关（``SW_TELEGRAM_ENABLED``）
    enabled: bool = False
    #: 有 bot token
    configured: bool = False
    #: 能真的推出去（token + chat_id 都有）
    ready: bool = False
    #: 有没有人给 bot 发过 /start（也就是知不知道该推给谁）
    chat_configured: bool = False
    #: 有没有 callback 签名密钥。没有就不发带按钮的卡片
    can_sign: bool = False
    #: 长轮询线程活着没
    polling: bool = False
    #: bot 用户名。**不发网络请求**，只回本进程握手时记下的那个
    username: str = ""
    #: 本进程推了几条、失败几条
    sent: int = 0
    failed: int = 0
    #: 轮询统计（polls / updates / handled / rejected / errors）
    stats: dict[str, int] = Field(default_factory=dict)
    #: 为什么不可用 + 怎么补。ready 时为空串
    detail: str = ""
    last_error: str = ""


class ImagegenOut(BaseModel):
    """生图可用性 + 当日用量。前端据此决定配图开关是否可点、禁用时说什么。"""

    #: 能不能配图。false 时前端必须禁用开关并把 ``reason`` 原样显示出来
    ready: bool
    #: auto / true / false（``SW_IMAGEGEN_ENABLED``）
    enabled: str
    model: str
    base_url: str
    has_api_key: bool
    #: 不 ready 时的人话原因；ready 时为空
    reason: str = ""
    #: 可执行的修复指引（去哪儿开权限、改哪个变量）
    hint: str = ""
    #: 今天已经生成几张 / 每日上限 / 还剩几张
    used_today: float = 0.0
    daily_limit: float = 0.0
    remaining: float = 0.0
    #: 出稿弹层的默认张数（``SW_GENERATE_ILLUSTRATIONS``）
    default_count: int = 0


def _accepts(name: str) -> list[str]:
    """哪些通用参数对这个 tick 有效（与 ``core.scheduler.tick_kwargs`` 的映射一致）。"""
    out: list[str] = []
    if name in ("generate", "insights"):
        out.append("account_id")
    if name in ("generate", "login_health", "sourcing"):
        out.append("platform")
    if name in ("login_health", "insights"):
        out.append("force")
    if name == "metrics":
        out.append("respect_windows")
    return out


@router.get("/preflight", summary="门禁自检")
def preflight(
    offline: bool = Query(default=True, description="true = 跳过所有网络探测（默认）"),
) -> Envelope[PreflightOut]:
    """跑 ``scripts/preflight.py`` 的全部检查并结构化返回。

    ``offline=false`` 会真去探公众号 / MPT / sidecar，**耗时可能十几秒**；即便
    ``offline=true``，docker 探测仍会执行（``docker info`` 最多 15 秒）。前端别把它
    放进轮询，做成"点一下才跑"的按钮。
    """
    from core.accounts import accounts_file_path
    from scripts.preflight import run_checks

    ran_at = utcnow()
    checks = run_checks(offline=offline, accounts_path=accounts_file_path())
    counts: dict[str, int] = {}
    for check in checks:
        counts[check.status] = counts.get(check.status, 0) + 1
    return ok(
        PreflightOut(
            offline=offline,
            passed=counts.get("FAIL", 0) == 0,
            counts=counts,
            checks=[PreflightCheck(name=c.name, status=c.status, detail=c.detail) for c in checks],
            ran_at=ran_at,
        )
    )


@router.get("/ticks", summary="可手动触发的定时任务")
def list_ticks() -> Envelope[TicksOut]:
    from core.scheduler import TICKS

    return ok(
        TicksOut(ticks=[TickInfo(name=name, accepts=_accepts(name)) for name in sorted(TICKS)])
    )


@router.post("/ticks/{name}", summary="手动跑一个 tick")
def run_tick_endpoint(
    name: str,
    account_id: str | None = None,
    platform: str | None = None,
    force: bool = False,
    respect_windows: bool | None = None,
) -> Envelope[TickRunOut]:
    """与 ``POST /dev/tick/{name}`` 完全等价（同一批函数、同一份参数映射）。

    未知 tick 404 ``not_found``；该 tick 不认的参数 422 ``invalid_tick_param``；
    tick 内部炸了 500 ``tick_failed``；metrics 的错误正文固定脱敏。
    """
    from core.scheduler import TICKS, run_tick, tick_kwargs

    if name not in TICKS:
        raise AppError(404, "not_found", f"未知 tick: {name}；可用 {sorted(TICKS)}")
    try:
        kwargs = tick_kwargs(
            name,
            account_id=account_id,
            platform=platform,
            force=force,
            respect_windows=respect_windows,
        )
    except ValueError as exc:
        raise AppError(422, "invalid_tick_param", str(exc)) from exc

    started = utcnow()
    try:
        stats = run_tick(name, **kwargs)
    except Exception as exc:  # tick 内部异常要看得见，不能只留在日志里
        if name == "metrics":
            raise AppError(500, "tick_failed", "metrics tick failed") from None
        raise AppError(500, "tick_failed", f"{type(exc).__name__}: {exc}") from exc
    return ok(
        TickRunOut(
            tick=name,
            stats=stats,
            elapsed_s=round((utcnow() - started).total_seconds(), 3),
        )
    )


@router.get("/imagegen", summary="生图可用性与今日用量")
def imagegen_info(session: Session = DbSession) -> Envelope[ImagegenOut]:
    """**不发任何网络请求**：只看配置 + 本进程内的熔断标记 + 今天的账本。

    所以它可以放进页面加载路径，不会因为探测把出稿的钱花掉（真探要跑
    ``SW_PREFLIGHT_IMAGEGEN=true`` 的 preflight）。

    ``ready=false`` 时 ``reason`` 一定非空——前端要把它原样显示给人看，
    而不是自己编一句"暂不可用"。
    """
    from core.budget import BudgetGuard, CostKind
    from generation.imagegen import imagegen_status

    settings = get_settings()
    status = imagegen_status(settings)
    guard = BudgetGuard(session)
    return ok(
        ImagegenOut(
            **status.as_dict(),
            used_today=guard.used(CostKind.IMAGES),
            daily_limit=guard.limit_of(CostKind.IMAGES),
            remaining=guard.remaining(CostKind.IMAGES),
            default_count=settings.sw_generate_illustrations,
        )
    )


@router.get("/telegram", summary="提醒渠道（Telegram）连通状态")
def telegram_info() -> Envelope[TelegramOut]:
    """**不发任何网络请求**：只看配置 + 本进程内的轮询线程状态（同 ``/system/imagegen``）。

    所以它可以放进页面加载路径。真要探活用 ``uv run python -m core.telegram check``。
    """
    from core.telegram import channel_status, get_telegram_channel

    status = channel_status()
    channel = get_telegram_channel()
    return ok(
        TelegramOut(
            enabled=bool(status["enabled"]),
            configured=bool(status["configured"]),
            ready=bool(status["ready"]),
            chat_configured=bool(status["chat_configured"]),
            can_sign=bool(status["can_sign"]),
            polling=bool(status["polling"]),
            username=str(status.get("username") or ""),
            sent=channel.sent_count if channel else 0,
            failed=channel.failed_count if channel else 0,
            stats=dict(status.get("stats") or {}),
            detail=str(status.get("detail") or ""),
            last_error=str(status.get("last_error") or ""),
        )
    )


@router.get("/info", summary="运行时信息")
def system_info() -> Envelope[SystemInfo]:
    """版本、环境、LLM 后端、调度器开关、已注册发布器。DB 地址里的密码已打码。"""
    from core.accounts import redact_db_url
    from core.scheduler import TICKS
    from core.state_machine import REVIEW_QUEUE_STATUSES, ContentStatus
    from publishers.base import PLATFORMS
    from publishers.registry import registered_platforms

    settings = get_settings()
    return ok(
        SystemInfo(
            version="0.1.0",
            env=settings.sw_env,
            time=utcnow(),
            timezone=settings.sw_timezone,
            llm_backend=settings.sw_llm_backend,
            llm_model=(
                settings.dsh_model if settings.sw_llm_backend == "dsh" else settings.llm_model
            ),
            database=redact_db_url(settings.sw_database_url),
            scheduler_enabled=settings.sw_scheduler_enabled,
            use_fake_publishers=settings.sw_use_fake_publishers,
            generate_enabled=settings.sw_generate_enabled,
            publishers=registered_platforms(),
            ticks=sorted(TICKS),
            platforms=list(PLATFORMS),
            content_statuses=[s.value for s in ContentStatus],
            review_queue_statuses=[s.value for s in REVIEW_QUEUE_STATUSES],
            auth_required=auth_required(),
            budget={
                "daily_token_budget": float(settings.daily_token_budget),
                "daily_render_seconds_budget": float(settings.daily_render_seconds_budget),
                "daily_image_budget": float(settings.daily_image_budget),
            },
        )
    )


__all__ = ["router"]
