"""账号：台账 + 调度策略 + 限频现状 + 人工介入登录 + 全生命周期（P10）。

登录那四个端点是 ``core/login_flow.py`` 的 JSON 包装，与 ``/accounts/{id}/login/*``
页面端点共用同一份实现：小红书回二维码 base64，抖音回宿主机窗口的状态机，
core 侧一律**不做任何自动识别**（docs/POLICY.md）。

P10 新增的增删改、sidecar 生命周期与手动出稿同样只做"取参数 → 调函数 → 包 envelope"：
台账回写在 ``core/account_admin.py``，容器在 ``core/sidecars.py``，出稿链路直接复用
``core/dev_flow.py``（与调度器 ``tick_generate`` 是同一批函数，不存在"手点能出、
自动出不来"）。
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core import account_admin, login_flow, sidecars
from core.accounts import AccountPolicy, policy_of
from core.api.common import DbSession, Envelope, aware, get_account, ok, safe_dt
from core.errors import AppError
from core.models import Account, ContentItem, utcnow
from core.ratelimit import db_usage
from core.state_machine import (
    REVIEW_QUEUE_STATUSES,
    ContentStatus,
    IllegalTransition,
    deactivate_account,
    reactivate_account,
)

logger = logging.getLogger("social_workflow.api.accounts")

router = APIRouter(prefix="/accounts", tags=["accounts"])


class PolicySummary(BaseModel):
    """账号级调度策略（来自 ``accounts.yaml`` → ``Account.extra``）。"""

    daily_limit: int = 0
    daily_target: int = 0
    publish_windows: str = "全天"
    timezone: str = "UTC"
    min_interval_minutes: int = 0
    has_persona: bool = False
    #: 机器审核干净的稿子自动批准并排期（P12）。默认关
    autopilot: bool = False
    #: 发布前要不要人点一下（P12）。默认开，且**没有旁路**
    confirm_required: bool = True
    confirm_ttl_hours: int = 24


class AccountOut(BaseModel):
    id: str
    name: str
    platform: str
    status: str
    #: ``needs_relogin`` / ``banned`` —— 需要人立刻处理
    needs_attention: bool = False
    policy: PolicySummary
    used_today: int = 0
    quota_left: int = 0
    last_published_at: datetime | None = None
    sidecar_endpoint: str | None = None
    supports_login: bool = False
    #: 复盘 Agent 上次写盘的时刻（``Account.extra['insights_updated_at']``）
    insights_updated_at: datetime | None = None
    insights_error: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AccountDetail(AccountOut):
    pending_review: int = 0
    scheduled: int = 0
    suspended: int = 0
    dead_letter: int = 0
    #: ``Account.extra`` 原样透出。里面只有台账配置与运行时标记，**没有任何凭据**
    #: （小红书只存 token 的**环境变量名**，见 core/accounts.py）
    extra: dict[str, object] = Field(default_factory=dict)


class LoginQrcode(BaseModel):
    account_id: str
    platform: str
    #: PNG 的 base64（不含 data URI 前缀）。前端拼 ``data:image/png;base64,``
    image_base64: str = ""
    status: str
    detail: str = ""
    account_status: str
    #: true = FakePublisher 的占位图，**不是**能扫的真二维码
    placeholder: bool = True
    expires_in: int = 120
    fetched_at: datetime


class LoginStatus(BaseModel):
    account_id: str
    platform: str
    status: str
    detail: str = ""
    account_status: str
    logged_in: bool = False
    checked_at: datetime


class LoginStart(BaseModel):
    ok: bool = True
    account_id: str
    platform: str
    #: 宿主机上传器的登录状态机，如 ``waiting_user``
    state: str = ""
    detail: str = ""
    started_at: datetime


class LoginCodeIn(BaseModel):
    code: str = Field(description="人工输入的短信验证码。不落库、不写日志明文")


class LoginCodeOut(BaseModel):
    ok: bool = True
    account_id: str
    #: 内存队列里还有几条待取
    pending: int = 0
    forwarded: bool = False
    forward_detail: str = ""


class AccountCreateIn(BaseModel):
    """新建账号。id 由服务端生成（平台前缀 + 名字 slug 或序号），不接受前端指定。"""

    platform: str = Field(description="xhs / douyin / wechat_mp")
    name: str = Field(min_length=1, max_length=64, description="工作台里显示的名字")
    identity_hint: str | None = Field(
        default=None,
        max_length=64,
        description="抖音必填：创作者中心显示的昵称。发布前读页面比对，防发错号",
    )
    publish_windows: list[str] | None = Field(
        default=None, description='发布时段，如 ["12:00-14:00","19:00-22:30"]；留空 = 全天'
    )
    min_interval_minutes: int | None = Field(default=None, ge=0, le=1440)
    daily_limit: int | None = Field(default=None, ge=0, le=100, description="日发上限")
    daily_target: int | None = Field(default=None, ge=0, le=50, description="每天自动出几条稿")
    timezone: str | None = Field(default=None, description="窗口用的时区，如 Asia/Shanghai")
    persona: str | None = Field(default=None, max_length=4000, description="行内人设，可留空")
    autopilot: bool | None = Field(
        default=None,
        description="机器审核干净的稿子自动批准并排期；默认 false，显式打开才生效",
    )
    confirm_required: bool | None = Field(
        default=None,
        description="发布前要不要人点一下确认；默认 true，关掉等于让这个号全自动发布",
    )


class AccountPatchIn(BaseModel):
    """改账号。platform 与 id **不可改**：改了等于换了个号，历史内容会对不上。"""

    name: str | None = Field(default=None, max_length=64)
    identity_hint: str | None = Field(default=None, max_length=64)
    publish_windows: list[str] | None = None
    min_interval_minutes: int | None = Field(default=None, ge=0, le=1440)
    daily_limit: int | None = Field(default=None, ge=0, le=100)
    daily_target: int | None = Field(default=None, ge=0, le=50)
    timezone: str | None = None
    persona: str | None = Field(default=None, max_length=4000)
    autopilot: bool | None = Field(
        default=None,
        description="机器审核干净的稿子自动批准并排期；默认 false，显式打开才生效",
    )
    confirm_required: bool | None = Field(
        default=None,
        description="发布前要不要人点一下确认；默认 true，关掉等于让这个号全自动发布",
    )


class AccountWriteOut(BaseModel):
    """写操作的统一返回：改完之后这个账号长什么样 + 一句给人看的话。"""

    account: AccountDetail
    message: str = ""
    #: 非致命的问题（sidecar 没起来、驱动是 none…）。有就一条条显示给人
    warnings: list[str] = Field(default_factory=list)


class DeactivateIn(BaseModel):
    reason: str = Field(default="", max_length=280, description="停用理由，会进审计日志")
    actor: str = Field(default="operator")


class SidecarOut(BaseModel):
    """小红书 sidecar 的当前状态。前端只认这个结构，不需要懂 docker。"""

    account_id: str
    #: docker / none
    driver: str
    #: running / stopped / absent / none-driver / error
    state: str
    detail: str = ""
    container: str = ""
    volume: str = ""
    image: str = ""
    port: int | None = None
    endpoint: str = ""
    #: ``GET {endpoint}/health`` 的原样透传（探不通为 null）
    health: dict[str, object] | None = None
    healthy: bool = False
    health_detail: str = ""
    checked_at: datetime


class SidecarActionOut(BaseModel):
    sidecar: SidecarOut
    message: str = ""


#: 一条稿最多配几张生图。上限卡在这里而不是只靠前端：手滑（或脚本）传个 999
#: 会把当日生图预算一次烧光
MAX_ILLUSTRATIONS = 6


class GenerateIn(BaseModel):
    topic: str | None = Field(
        default=None, max_length=200, description="手工指定选题标题；留空则跑选题 Agent"
    )
    illustrations: int | None = Field(
        default=None,
        ge=0,
        le=MAX_ILLUSTRATIONS,
        description=(
            "这条稿配几张生图（P11）。留空取 SW_GENERATE_ILLUSTRATIONS；0 = 不配图。"
            "公众号题图与抖音封面只会用第一张。生图不可用时静默降级为不配图，"
            "原因写在 warnings 里"
        ),
    )


class GenerateOut(BaseModel):
    """手动出稿的结果。链路与调度器 ``tick_generate`` 完全一致。"""

    account_id: str
    content_item_id: str | None = None
    status: str = ""
    title: str = ""
    #: real / scripted / injected —— scripted 表示没配模型凭据，走的是预置文案
    llm: str = "real"
    selected_topic: str | None = None
    tokens_used: int = 0
    elapsed_s: float = 0.0
    review_passed: bool | None = None
    review_blocking: int = 0
    #: 实际生成了几张配图。比请求的少（甚至 0）是正常的降级，原因在 warnings 里
    illustrations: int = 0
    warnings: list[str] = Field(default_factory=list)
    #: 今天这个号已经出了几条**草稿** / 手动上限多少。
    #: 注意这个 ``used_today`` 与账号列表里的**不是**一个口径：这里是
    #: ``core.account_admin.generated_today``（防手滑的草稿计数，按 UTC 日，
    #: 与成本闸门同源），账号列表那个是限频的已发布数（按账号本地日）
    used_today: int = 0
    cap: int = 0
    message: str = ""


ATTENTION = ("needs_relogin", "banned")


def _policy_summary(policy: AccountPolicy) -> PolicySummary:
    return PolicySummary(
        daily_limit=policy.daily_limit,
        daily_target=policy.daily_target,
        publish_windows=policy.window_text(),
        timezone=policy.timezone,
        min_interval_minutes=int(policy.min_interval.total_seconds() // 60),
        has_persona=bool(policy.persona),
        autopilot=policy.autopilot,
        confirm_required=policy.confirm_required,
        confirm_ttl_hours=policy.confirm_ttl_hours,
    )


def account_out(session: Session, account: Account) -> AccountOut:
    policy = policy_of(account)
    # ``used_today`` / ``quota_left`` 按**账号本地日**计（P11.3），与限频闸门同一口径：
    # 本地 00:00–08:00 打开工作台时，"今日已发"不该还混着昨晚发的那几条
    usage = db_usage(session, account.id, timezone=policy.timezone)
    extra = dict(account.extra or {})
    return AccountOut(
        id=account.id,
        name=account.name,
        platform=account.platform,
        status=account.status,
        needs_attention=account.status in ATTENTION,
        policy=_policy_summary(policy),
        used_today=usage.count_today,
        quota_left=max(policy.daily_limit - usage.count_today, 0),
        last_published_at=aware(usage.last_at),
        sidecar_endpoint=account.sidecar_endpoint,
        supports_login=login_flow.supports_interactive_login(account),
        insights_updated_at=safe_dt(extra.get("insights_updated_at")),
        insights_error=str(extra.get("insights_error") or ""),
        created_at=aware(account.created_at),
        updated_at=aware(account.updated_at),
    )


def _count(session: Session, account_id: str, statuses: tuple[str, ...]) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(ContentItem)
            .where(
                ContentItem.account_id == account_id,
                ContentItem.status.in_(list(statuses)),
            )
        )
        or 0
    )


@router.get("", summary="账号列表")
def list_accounts(
    platform: str | None = Query(default=None),
    status: str | None = Query(
        default=None, description="ok / degraded / needs_relogin / banned / suspended"
    ),
    session: Session = DbSession,
) -> Envelope[list[AccountOut]]:
    """账号台账 + 健康 + 今日已发/日上限。账号总数是个位数，这里**不分页**。"""
    stmt = select(Account).order_by(Account.platform, Account.id)
    if platform:
        stmt = stmt.where(Account.platform == platform)
    if status:
        stmt = stmt.where(Account.status == status)
    accounts = list(session.scalars(stmt))
    return ok([account_out(session, account) for account in accounts])


def account_detail_of(session: Session, account: Account) -> AccountDetail:
    base = account_out(session, account).model_dump()
    return AccountDetail(
        **base,
        pending_review=_count(session, account.id, tuple(s.value for s in REVIEW_QUEUE_STATUSES)),
        scheduled=_count(session, account.id, (ContentStatus.SCHEDULED.value,)),
        suspended=_count(session, account.id, (ContentStatus.SUSPENDED.value,)),
        dead_letter=_count(session, account.id, (ContentStatus.DEAD_LETTER.value,)),
        extra=dict(account.extra or {}),
    )


@router.get("/{account_id}", summary="账号详情")
def account_detail(account_id: str, session: Session = DbSession) -> Envelope[AccountDetail]:
    return ok(account_detail_of(session, get_account(session, account_id)))


# ------------------------------------------------------------------ 增 / 改 / 停


@router.post("", summary="新建账号", status_code=201)
def create_account(
    payload: AccountCreateIn, session: Session = DbSession
) -> Envelope[AccountWriteOut]:
    """建一个账号：分配 id →（小红书）分配端口 → **回写 accounts.yaml** → 同步进 DB。

    台账是唯一真相，库是它的投影：这里永远"先写文件、再从文件同步"，所以
    ``python -m core.accounts check`` 建完立刻就是通过的，不会漂移。写库失败会把
    台账文件回滚成动手之前的字节。

    小红书还会顺手按 ``SW_SIDECAR_DRIVER`` 起容器（``none`` 时不起，
    在 ``warnings`` 里如实说"sidecar 未接入"，**不假装它在跑**）。
    """
    draft = account_admin.AccountDraft(**payload.model_dump())
    account, warnings = account_admin.create_account(session, draft)
    message = f"账号 {account.id} 已建好，台账与库都写了。"
    return ok(
        AccountWriteOut(
            account=account_detail_of(session, account), message=message, warnings=warnings
        )
    )


@router.patch("/{account_id}", summary="改账号配置")
def patch_account(
    account_id: str, payload: AccountPatchIn, session: Session = DbSession
) -> Envelope[AccountWriteOut]:
    """改名字 / 发布窗口 / 限频 / daily_target / 人设。**platform 与 id 不可改**。

    同样是"改台账再同步"，所以改完 ``check`` 依然通过。没传的字段保持原样。
    """
    account = get_account(session, account_id)
    draft = account_admin.AccountDraft(platform=account.platform, **payload.model_dump())
    updated = account_admin.update_account(session, account, draft)
    return ok(
        AccountWriteOut(
            account=account_detail_of(session, updated), message="配置已更新，台账同步写了。"
        )
    )


@router.post("/{account_id}/deactivate", summary="停用账号")
def deactivate(
    account_id: str, payload: DeactivateIn | None = None, session: Session = DbSession
) -> Envelope[AccountWriteOut]:
    """停用：置 ``suspended``，名下已排期的内容一并挂起。

    **不硬删**——历史内容、发布记录、审计日志都还挂在这个账号上，删了证据链就断了。
    停用期间调度器既不给它出稿也不给它发布，登录巡检也会跳过它。
    """
    account = get_account(session, account_id)
    body = payload or DeactivateIn()
    try:
        suspended = deactivate_account(session, account, actor=body.actor, reason=body.reason)
    except IllegalTransition as exc:
        raise AppError(409, "illegal_transition", f"这个账号停不掉：{exc}") from exc
    session.commit()
    tail = f"，顺手挂起了 {len(suspended)} 条排期" if suspended else ""
    return ok(
        AccountWriteOut(
            account=account_detail_of(session, account),
            message=f"已停用{tail}。重新启用后挂起的内容会自动放回。",
        )
    )


@router.post("/{account_id}/reactivate", summary="启用账号")
def reactivate(
    account_id: str, payload: DeactivateIn | None = None, session: Session = DbSession
) -> Envelope[AccountWriteOut]:
    """启用：回 ``ok`` 并把挂起的排期项放回。真实健康由下一次登录巡检写回。"""
    account = get_account(session, account_id)
    body = payload or DeactivateIn()
    try:
        restored = reactivate_account(session, account, actor=body.actor)
    except IllegalTransition as exc:
        raise AppError(409, "illegal_transition", f"这个账号启用不了：{exc}") from exc
    session.commit()
    tail = f"，放回了 {len(restored)} 条排期" if restored else ""
    return ok(
        AccountWriteOut(
            account=account_detail_of(session, account),
            message=f"已启用{tail}。登录态是否还在，下一次巡检（或点「重新扫码」）说了算。",
        )
    )


# ---------------------------------------------------------------------- sidecar


def _sidecar_out(state: sidecars.SidecarState) -> SidecarOut:
    return SidecarOut(**state.as_dict())


def _sidecar_account(session: Session, account_id: str) -> Account:
    account = get_account(session, account_id)
    try:
        sidecars.require_sidecar_platform(account)
    except sidecars.SidecarNotSupported as exc:
        raise AppError(501, "not_supported", str(exc)) from exc
    return account


@router.get("/{account_id}/sidecar", summary="看 sidecar 状态")
def sidecar_state(account_id: str, session: Session = DbSession) -> Envelope[SidecarOut]:
    """小红书专属：容器状态 + ``GET /health`` 透传。

    ``state`` 五档：``running`` / ``stopped`` / ``absent`` / ``none-driver`` /
    ``error``。``none-driver`` 表示这台 core 的 ``SW_SIDECAR_DRIVER=none``——
    界面要如实显示"sidecar 未接入"，而不是转个圈假装在起。
    """
    account = _sidecar_account(session, account_id)
    return ok(_sidecar_out(sidecars.describe(account)))


@router.post("/{account_id}/sidecar/{action}", summary="起 / 停 / 重建 sidecar")
def sidecar_action(
    account_id: str, action: str, session: Session = DbSession
) -> Envelope[SidecarActionOut]:
    """``start`` / ``stop`` / ``recreate``。

    ``recreate`` 只删容器**不删 volume**，所以扫过的码不用重扫；真想清登录态得手工
    删 volume（那是个破坏性动作，不给按钮）。
    """
    account = _sidecar_account(session, account_id)
    if action not in ("start", "stop", "recreate"):
        raise AppError(422, "unknown_action", f"未知动作 {action!r}，只支持 start/stop/recreate")
    try:
        state, message = sidecars.act(account, action)
    except sidecars.SidecarError as exc:
        raise AppError(502, "sidecar_error", str(exc)) from exc
    return ok(SidecarActionOut(sidecar=_sidecar_out(state), message=message))


# ---------------------------------------------------------------------- 出一条稿


@router.post("/{account_id}/generate", summary="给这个号出一条稿")
def generate(
    account_id: str, payload: GenerateIn | None = None, session: Session = DbSession
) -> Envelope[GenerateOut]:
    """跑一次完整生成链（真 LLM → 机器审核 → 进审核队列），产出一条待审内容。

    和调度器的 ``tick_generate`` 走**同一批函数**（``core/dev_flow.py``），所以界面上
    点出来的东西就是自动出稿的东西。

    三道闸门（都会给出人话）：账号状态（封禁 / 停用不出）、当日手动条数
    ``max(daily_target,1)×2``（防手滑连点烧钱）、token 预算。
    抖音还要求渲染服务可达，公众号要求凭据已配——缺了就直说缺什么，不出半成品。

    失败分两类：链路自己走不下去（选题池空、渲染失败）是 409 ``generation_failed``；
    模型 / 网关炸了是 502 ``llm_failed``——两者都带 envelope，前端按 ``code`` 分支。
    """
    account = get_account(session, account_id)
    used, cap = account_admin.check_generate_allowed(session, account)
    topic = (payload.topic if payload else None) or None
    illustrations = payload.illustrations if payload else None

    started = utcnow()
    result = _run_pipeline(session, account, topic=topic, illustrations=illustrations)
    session.commit()

    elapsed = round((utcnow() - started).total_seconds(), 2)
    title = ""
    if result.content_item_id:
        item = session.get(ContentItem, result.content_item_id)
        title = item.title if item else ""
    message = (
        f"出好了：《{title}》已进审核队列。"
        if result.content_item_id
        else "链路跑完了但没产出内容。"
    )
    if result.llm == "scripted":
        message += "（这台 core 没配模型凭据，内容是 ScriptedLLM 的预置文案，不是真生成。）"
    if result.illustrations:
        message += f"（配了 {result.illustrations} 张生图。）"
    return ok(
        GenerateOut(
            account_id=account.id,
            content_item_id=result.content_item_id,
            status=result.status,
            title=title,
            llm=result.llm,
            selected_topic=result.selected_topic,
            tokens_used=result.tokens_used,
            elapsed_s=elapsed,
            review_passed=result.review_passed,
            review_blocking=result.review_blocking,
            illustrations=result.illustrations,
            warnings=result.warnings,
            used_today=used + 1,
            cap=cap,
            message=message,
        )
    )


def _run_pipeline(
    session: Session, account: Account, *, topic: str | None, illustrations: int | None = None
):
    """按平台分发到 ``core/dev_flow.py``，并把"缺依赖"翻译成人话。

    ``illustrations`` 为 ``None`` 时取 ``SW_GENERATE_ILLUSTRATIONS``——工作台上不选
    就是"按这台机器的默认来"，和自动出稿保持一致。
    """
    from core.config import get_settings
    from core.dev_flow import (
        DevFlowError,
        run_douyin_pipeline,
        run_wechat_pipeline,
        run_xhs_pipeline,
    )
    from generation.llm import LLMError

    wanted = (
        get_settings().sw_generate_illustrations if illustrations is None else int(illustrations)
    )
    wanted = max(0, min(wanted, MAX_ILLUSTRATIONS))

    try:
        if account.platform == "xhs":
            from generation.pipeline import XhsGenerationOptions

            return run_xhs_pipeline(
                session,
                account,
                topic_title=topic,
                options=XhsGenerationOptions(illustrations=wanted),
            )
        if account.platform == "wechat_mp":
            _require_wechat_credentials()
            from generation.pipeline import GenerationOptions

            return run_wechat_pipeline(
                session,
                account,
                topic_title=topic,
                # 题图只用一张，多要没有意义
                options=GenerationOptions(illustrations=min(wanted, 1)),
            )
        if account.platform == "douyin":
            from generation.video_pipeline import VideoGenerationOptions

            _require_render_service()
            return run_douyin_pipeline(
                session,
                account,
                topic_title=topic,
                # 手动出稿默认**真渲染**：人点这个按钮就是想看成片。
                # 渲染服务不可达时上面已经拦下并说清楚了
                options=VideoGenerationOptions(skip_render=False, illustrations=min(wanted, 1)),
            )
    except DevFlowError as exc:
        # 链路走不下去是预期内的（选题池空、预算耗尽、渲染失败），不是 500
        session.rollback()
        raise AppError(409, "generation_failed", str(exc)) from exc
    except LLMError as exc:
        # 模型这一侧炸了（网关 5xx、限流、拒答、结构化输出兜不住）。
        # catch 基类而不是逐个子类：对界面来说这些都是同一件事——"这次没出稿，可以重试"，
        # 分得再细前端也只会显示同一段话；漏 catch 的代价则是一个没有 envelope 的裸 500。
        # 原文异常拼进 message：出事时人要凭它判断是网关还是模型，日志里翻不方便。
        session.rollback()
        raise AppError(
            502,
            "llm_failed",
            f"模型这次没走通，多半是网关或模型抽风，稍等重试；"
            f"反复出现就去系统页跑一次 preflight 看看凭据和后端。（{exc}）",
        ) from exc
    raise AppError(501, "not_supported", f"平台 {account.platform} 还没有生成链")


def _require_wechat_credentials() -> None:
    from core.config import get_settings

    settings = get_settings()
    if settings.wechat_app_id and settings.wechat_app_secret:
        return
    raise AppError(
        503,
        "credentials_missing",
        "公众号还没配凭据：稿子出得来也发不出去。请在 core 那台机器的 .env 里配好 "
        "WECHAT_APPID / WECHAT_APPSECRET（并把服务器出口 IP 加进公众号后台白名单，"
        "否则调用会报 errcode 40164），再回来出稿。凭据不经过工作台。",
    )


def _require_render_service() -> None:
    """抖音手动出稿要真渲染，先探一下 MoneyPrinterTurbo 活没活着。"""
    from core.config import get_settings
    from generation.mpt_client import MptClient
    from publishers.base import PublishError

    settings = get_settings()
    if not (settings.mpt_base_url or "").strip():
        raise AppError(
            503,
            "render_unavailable",
            "抖音出稿要渲染成片，但 MPT_BASE_URL 是空的。"
            "先起 MoneyPrinterTurbo（docker compose --profile video up -d）并配好素材源 key。",
        )
    try:
        with MptClient(
            base_url=settings.mpt_base_url, timeout=5.0, api_key=settings.mpt_api_key
        ) as client:
            client.health()
    except (PublishError, OSError) as exc:
        raise AppError(
            503,
            "render_unavailable",
            f"连不上渲染服务 {settings.mpt_base_url}：{exc}。"
            "抖音出稿必须先有它，否则只能出脚本出不了片。",
        ) from exc


@router.get("/{account_id}/login/qrcode", summary="取登录二维码")
def login_qrcode(account_id: str, session: Session = DbSession) -> Envelope[LoginQrcode]:
    """小红书走真实 sidecar；``SW_USE_FAKE_PUBLISHERS=true`` 时是占位图（``placeholder=true``）。

    顺带巡一次登录态并落 Account 状态机。平台不支持扫码返回 501 ``not_supported``，
    发布器未注册 503 ``publisher_unavailable``，sidecar 出错 502 ``upstream_error``。
    """
    account = get_account(session, account_id)
    return ok(LoginQrcode(**login_flow.qrcode_payload(session, account)))


@router.get("/{account_id}/login/status", summary="查登录状态")
def login_status(account_id: str, session: Session = DbSession) -> Envelope[LoginStatus]:
    """登录页每 3 秒调一次。扫码成功 → 账号自动回 ``ok`` 并放回被挂起的排期项。"""
    account = get_account(session, account_id)
    return ok(LoginStatus(**login_flow.status_payload(session, account)))


@router.post("/{account_id}/login/start", summary="打开宿主机登录窗口（抖音）")
def login_start(account_id: str, session: Session = DbSession) -> Envelope[LoginStart]:
    """抖音的二维码在**宿主机浏览器窗口**里，core 不代理图片，只负责把窗口弹出来。

    小红书没有这一步，返回 501 ``not_supported``。
    """
    account = get_account(session, account_id)
    return ok(LoginStart(**login_flow.start_payload(account)))


@router.post("/{account_id}/login/code", summary="提交短信验证码")
def login_code(
    account_id: str, payload: LoginCodeIn, session: Session = DbSession
) -> Envelope[LoginCodeOut]:
    """验证码进内存队列并顺手转发给发布器。转发失败不算错误（``forwarded=false`` + 原因）。"""
    account = get_account(session, account_id)
    return ok(LoginCodeOut(**login_flow.submit_code(account, payload.code)))


__all__ = ["AccountDetail", "AccountOut", "account_detail_of", "account_out", "router"]
