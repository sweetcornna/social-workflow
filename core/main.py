"""FastAPI 控制面：健康检查 + 审核 UI + 账号登录页 + 统计页 + 工作台 JSON API。

启动：``uv run uvicorn core.main:app --port 8000``

两套门面共存：

- **Jinja2 + HTMX 页面**（``/review``、``/accounts``、``/stats``）——运维随手打开就能用，
  无 JS 也能提交表单；
- **JSON API**（``/api/v1/*``，见 ``core/api/`` 与 docs/WORKBENCH_API.md）——前端工作台
  的唯一数据面。

两者共用同一批业务函数（``core/review_actions.py`` / ``core/login_flow.py`` /
``core/content_view.py``），所以不存在"页面拦得住、curl 拦不住"。
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import Scope

from core import db, login_flow
from core.config import get_settings
from core.content_view import (
    account_windows,
    bundle_view,
    cover_asset,
    latest_edit_diff,
    local_media_path,
    needs_watch_confirm,
    platform_extra_json,
    slot_text,
)
from core.models import (
    Account,
    ContentItem,
    CostLedger,
    PublishRecord,
    ReviewLog,
    Topic,
    new_id,
    utcnow,
)
from core.review_actions import DEFAULT_ACTOR, approve_item, edit_item, reject_item
from core.review_ui import TEMPLATES_DIR
from core.sms_inbox import SMS_INBOX
from core.state_machine import REVIEW_QUEUE_STATUSES, ContentStatus
from publishers.base import ContentBundle, MediaAsset
from publishers.registry import registered_platforms, use_fake_publishers

logger = logging.getLogger("social_workflow.api")

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    db.configure(settings.sw_database_url)
    db.init_db()
    if settings.sw_use_fake_publishers:
        # P0：真实发布器尚未实现，注册 FakePublisher 以便本地联调与契约测试
        use_fake_publishers()
        logger.info("已注册 FakePublisher: %s", registered_platforms())
    if settings.sw_sync_accounts_on_start:
        # P4：台账入库。幂等、不碰 status，所以每次启动跑一遍是安全的。
        # 不这么做的话，accounts.yaml 里写了账号但没人执行 sync，
        # 所有 dev 端点与调度器都会说"账号不存在"——这是 P3 之前最常见的踩坑。
        _sync_accounts_on_start()

    scheduler = None
    if settings.sw_scheduler_enabled:
        # P4：全链路定时调度随控制面一起起来，这样 `docker compose up core`
        # 就是"无人值守（除审核点击）"的完整形态。测试里一律关掉（见 conftest）。
        from core.scheduler import create_scheduler

        scheduler = create_scheduler(start=True)
        logger.info("调度器已启动: %s", [job.id for job in scheduler.get_jobs()])

    # P12：Telegram 回调走 long polling（**纯出站**，不需要任何入站暴露，
    # 理由见 core/telegram.py 的模块 docstring）。没配 / SW_TELEGRAM_ENABLED=false
    # 时 start_poller 返回 None，连线程都不会起
    from core.telegram import start_poller, stop_poller

    start_poller(settings)
    try:
        yield
    finally:
        stop_poller()
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        if settings.sw_llm_backend == "dsh":
            # dsh 后端持有 runtime 子进程。进程退出还有 atexit 兜底，
            # 但 uvicorn --reload 只会重建 lifespan，不重建进程，所以这里必须显式关
            from generation.llm_dsh import close_shared_pool

            close_shared_pool()


def _sync_accounts_on_start() -> None:
    """启动时把 ``accounts.yaml`` 同步进 DB。失败只 warn，不挡启动。"""
    from core.accounts import AccountsError, load_specs, sync_accounts

    try:
        specs = load_specs()
        if not specs:
            logger.info("accounts.yaml 为空或不存在，跳过台账同步")
            return
        with db.session_scope() as session:
            report = sync_accounts(session, specs)
        logger.info(
            "台账同步：新建 %d 更新 %d 未变 %d 台账外 %d",
            len(report.created),
            len(report.updated),
            len(report.unchanged),
            len(report.orphans),
        )
    except AccountsError as exc:
        logger.warning("台账不合法，跳过同步（改完跑 python -m core.accounts sync）：%s", exc)
    except Exception as exc:  # pragma: no cover - 启动期兜底，不能因此起不来
        logger.warning("台账同步失败：%s", exc)


def create_app() -> FastAPI:
    app = FastAPI(
        title="social_workflow control plane",
        version="0.1.0",
        lifespan=lifespan,
    )
    _register_routes(app)
    # 工作台 JSON API。挂在 /api/v1 下，与上面的 HTML 端点互不影响
    from core.api import install_api

    install_api(app)
    # 前端工作台（Next.js 静态导出）。挂在最后，前面的路由优先级更高
    _mount_workbench(app)
    return app


# ----------------------------------------------------------------- 工作台静态站

#: 前端产物目录。默认是仓库里的 ``ui/out``（``bash scripts/build_ui.sh`` 生成，
#: 不入库）；容器里可以用 ``SW_UI_DIST`` 指到别处。
_DEFAULT_UI_DIST = Path(__file__).resolve().parent.parent / "ui" / "out"

_WORKBENCH_PLACEHOLDER = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>工作台尚未构建 · social_workflow</title>
<style>
  :root{color-scheme:light dark}
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
       background:#fbf4e8;color:#271810;
       font-family:"PingFang SC","Hiragino Sans GB","Microsoft YaHei",system-ui,sans-serif}
  .card{max-width:34rem;padding:2.25rem 2rem;border-radius:1rem;background:rgba(255,255,255,.7);
        border:1px solid rgba(140,100,60,.16);box-shadow:0 24px 48px -24px rgba(80,45,20,.22)}
  h1{margin:0 0 .75rem;font-family:Songti SC,"Noto Serif SC",serif;
     font-size:1.75rem;font-weight:400}
  em{font-style:italic;color:#b25a1c}
  p{margin:.6rem 0;font-size:.86rem;line-height:1.75;color:#513e2d}
  code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.8rem}
  pre{margin:.9rem 0 0;padding:.75rem .9rem;border-radius:.6rem;background:rgba(255,255,255,.55);
      border:1px solid rgba(140,100,60,.16);overflow-x:auto}
  a{color:#b25a1c}
  @media (prefers-color-scheme:dark){
    body{background:#1a120d;color:#f8f1e5}
    .card{background:rgba(60,40,26,.5);border-color:rgba(255,255,255,.09)}
    p{color:#ccbfa8} em,a{color:#f0a460}
    pre{background:rgba(255,255,255,.04);border-color:rgba(255,255,255,.09)}
  }
</style></head><body><div class="card">
<h1>工作台<em>还没构建</em></h1>
<p>后端已经在跑了，但 <code>ui/out</code> 里没有静态产物——前端产物不入库，需要在本机构建一次：</p>
<pre>bash scripts/build_ui.sh</pre>
<p>构建完刷新本页即可。开发前端时也可以直接 <code>cd ui &amp;&amp; pnpm dev</code>，
它会把 <code>/api</code> 与 <code>/review</code> 代理到这台 core。</p>
<p>数据面不受影响，现在就能用：<a href="/docs">/docs</a> · <a href="/review">/review</a> ·
<a href="/api/v1/dashboard">/api/v1/dashboard</a></p>
</div></body></html>
"""


#: Next.js 的内容寻址产物目录。文件名里带 hash，改一个字节就换一个名字，
#: 所以可以放心地"缓存一年 + immutable"
_IMMUTABLE_PREFIX = "_next/static/"
_IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
#: HTML 一律 no-cache（**不是** no-store：还能走 304，只是每次都要问一下）
_HTML_CACHE = "no-cache"


class _WorkbenchStatic(StaticFiles):
    """``ui/out`` 不存在时返回带构建指引的占位页，而不是 500 / 裸 404。

    产物存在时行为与普通 ``StaticFiles(html=True)`` 完全一致
    （目录补 ``index.html``、未知路径回 Next 导出的 ``404.html``），只多加一件事：
    **缓存头**。

    为什么必须自己加缓存头（修一类真实故障）
    ----------------------------------------
    重新部署一次前端，``_next/static/`` 下的 chunk 全部换名。浏览器如果还拿着上一版
    被缓存的 ``index.html``，它引用的那些 chunk 已经在服务器上不存在了 —— 页面白屏，
    而且用户刷新多少次都一样（缓存的还是那份旧 HTML）。Starlette 的 StaticFiles 默认
    只给 ETag/Last-Modified，不给 Cache-Control，中间的 nginx / CDN 于是各按各的
    启发式缓存，这个故障就变成了"有的人白屏有的人没事"。

    所以：**HTML 每次都回源问一下（no-cache），带 hash 的静态资源缓存一年**。
    """

    async def check_config(self) -> None:
        """Starlette 默认在第一个请求时 assert 目录存在，缺了直接 RuntimeError。

        这里改成不检查——目录缺席是**预期内**的状态（没跑过 build_ui.sh），
        由 ``get_response`` 兜成占位页。
        """
        return None

    async def get_response(self, path: str, scope: Scope):  # type: ignore[override]
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404 and not Path(str(self.directory)).is_dir():
                return _no_cache(HTMLResponse(_WORKBENCH_PLACEHOLDER))
            raise
        return _with_cache_headers(path, response)


def _no_cache(response: Response) -> Response:
    response.headers["cache-control"] = _HTML_CACHE
    return response


def _with_cache_headers(path: str, response: Response) -> Response:
    """按路径给缓存头。判 HTML 用响应的 content-type：目录请求的 ``path`` 不带 .html。"""
    normalized = path.lstrip("/")
    if normalized.startswith(_IMMUTABLE_PREFIX):
        response.headers["cache-control"] = _IMMUTABLE_CACHE
        return response
    content_type = response.headers.get("content-type", "")
    if normalized.endswith(".html") or content_type.startswith("text/html"):
        return _no_cache(response)
    return response


def _mount_workbench(app: FastAPI) -> None:
    dist = Path(os.environ.get("SW_UI_DIST") or _DEFAULT_UI_DIST)
    app.mount(
        "/workbench",
        # check_dir=False：产物可能是起服务之后才构建出来的，不能因此起不来
        _WorkbenchStatic(directory=str(dist), html=True, check_dir=False),
        name="workbench",
    )


# --------------------------------------------------------------------- 工具


def _ctx(request: Request, **kwargs: Any) -> dict[str, Any]:
    base = {"request": request, "env": get_settings().sw_env, "actor": DEFAULT_ACTOR}
    base.update(kwargs)
    return base


def _wants_fragment(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _actions_response(
    request: Request,
    item: ContentItem,
    message: str,
    redirect_to: str,
    *,
    slot_hint: str = "",
) -> HTMLResponse | RedirectResponse:
    """HTMX 请求返回局部片段；普通表单提交返回 303 重定向（无 JS 也能用）。"""
    if _wants_fragment(request):
        return templates.TemplateResponse(
            request,
            "_actions.html",
            _ctx(
                request,
                item=item,
                message=message,
                slot_text=slot_hint,
                needs_watch=needs_watch_confirm(item),
            ),
        )
    return RedirectResponse(redirect_to, status_code=303)


def _get_item(session: Session, item_id: str) -> ContentItem:
    item = session.get(ContentItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"内容项不存在: {item_id}")
    return item


def _get_account(session: Session, account_id: str) -> Account:
    account = session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail=f"账号不存在: {account_id}")
    return account


# ------------------------------------------------------------------- 路由


def _register_routes(app: FastAPI) -> None:
    get_db = db.get_db

    @app.get("/health")
    def health(session: Session = Depends(get_db)) -> JSONResponse:
        """存活 + 依赖自检。DB 不可用返回 503。"""
        checks: dict[str, Any] = {"database": "unknown"}
        status_code = 200
        try:
            session.execute(select(func.count()).select_from(Account))
            checks["database"] = "ok"
        except Exception as exc:  # pragma: no cover - 只在 DB 损坏时触发
            checks["database"] = f"error: {exc}"
            status_code = 503
        checks["publishers"] = registered_platforms()
        return JSONResponse(
            {
                "status": "ok" if status_code == 200 else "degraded",
                "version": app.version,
                "env": get_settings().sw_env,
                "time": datetime.now(UTC).isoformat(),
                "checks": checks,
            },
            status_code=status_code,
        )

    @app.get("/", include_in_schema=False)
    def index() -> RedirectResponse:
        return RedirectResponse("/review", status_code=307)

    # -- 审核 -------------------------------------------------------------

    @app.get("/review", response_class=HTMLResponse)
    def review_list(
        request: Request, status: str | None = None, session: Session = Depends(get_db)
    ) -> HTMLResponse:
        stmt = select(ContentItem).order_by(ContentItem.updated_at.desc())
        if status is None:
            stmt = stmt.where(ContentItem.status.in_([s.value for s in REVIEW_QUEUE_STATUSES]))
        elif status != "all":
            stmt = stmt.where(ContentItem.status == status)
        items = list(session.scalars(stmt))
        return templates.TemplateResponse(
            request,
            "review_list.html",
            _ctx(
                request,
                items=items,
                current_status=status,
                all_statuses=[s.value for s in ContentStatus],
            ),
        )

    @app.get("/review/{item_id}", response_class=HTMLResponse)
    def review_detail(
        request: Request, item_id: str, session: Session = Depends(get_db)
    ) -> HTMLResponse:
        item = _get_item(session, item_id)
        logs = list(
            session.scalars(
                select(ReviewLog)
                .where(ReviewLog.content_item_id == item_id)
                .order_by(ReviewLog.at.desc())
            )
        )
        records = list(
            session.scalars(
                select(PublishRecord)
                .where(PublishRecord.content_item_id == item_id)
                .order_by(PublishRecord.created_at.desc())
            )
        )
        return templates.TemplateResponse(
            request,
            "review_detail.html",
            _ctx(
                request,
                item=item,
                bundle=bundle_view(item),
                platform_extra=platform_extra_json(item),
                diff=latest_edit_diff(session, item_id),
                needs_watch=needs_watch_confirm(item),
                slot_text=slot_text(session, item),
                account_windows=account_windows(session, item),
                logs=logs,
                records=records,
            ),
        )

    @app.get("/review/{item_id}/preview", response_class=HTMLResponse)
    def review_preview(item_id: str, session: Session = Depends(get_db)) -> HTMLResponse:
        """返回 ``body_html`` 原文，供详情页用 sandbox iframe 嵌入预览。

        单独开一个路由而不是把 HTML 塞进模板：wenyan 产出的是**整段内联样式 HTML**，
        直接插进审核页会污染页面样式；放进 sandbox iframe 才能还原公众号里的观感。
        """
        item = _get_item(session, item_id)
        html = (item.bundle_json or {}).get("body_html")
        if not html:
            raise HTTPException(status_code=404, detail="该内容尚未渲染 body_html")
        return HTMLResponse(html)

    @app.get("/review/{item_id}/cover")
    def review_cover(item_id: str, session: Session = Depends(get_db)) -> FileResponse:
        """封面图原图。文件不存在或越界返回 404。"""
        item = _get_item(session, item_id)
        path = cover_asset(item)
        if path is None:
            raise HTTPException(status_code=404, detail="没有可用的本地封面文件")
        return FileResponse(path)

    @app.get("/review/{item_id}/media/{index}")
    def review_media(item_id: str, index: int, session: Session = Depends(get_db)) -> FileResponse:
        """按下标取媒体原图。小红书一条笔记有 4–9 张卡片，详情页要逐张看。

        和 ``/cover`` 走同一套越界防护：只允许读工作目录内的文件。
        """
        item = _get_item(session, item_id)
        path = local_media_path(item, index)
        if path is None:
            raise HTTPException(status_code=404, detail=f"没有可用的本地媒体文件（index={index}）")
        return FileResponse(path)

    @app.post("/review/{item_id}/approve", response_model=None)
    def review_approve(
        request: Request,
        item_id: str,
        actor: str = Form(DEFAULT_ACTOR),
        reason: str = Form(""),
        watched: str = Form(""),
        session: Session = Depends(get_db),
    ) -> HTMLResponse | RedirectResponse:
        item = _get_item(session, item_id)
        outcome = approve_item(session, item, actor=actor, reason=reason, watched=watched)
        session.commit()
        return _actions_response(
            request, item, outcome.message, f"/review/{item_id}", slot_hint=outcome.slot_text
        )

    @app.post("/review/{item_id}/reject", response_model=None)
    def review_reject(
        request: Request,
        item_id: str,
        actor: str = Form(DEFAULT_ACTOR),
        reason: str = Form(...),
        session: Session = Depends(get_db),
    ) -> HTMLResponse | RedirectResponse:
        item = _get_item(session, item_id)
        reject_item(session, item, actor=actor, reason=reason)
        session.commit()
        return _actions_response(request, item, "已驳回，理由已回写", f"/review/{item_id}")

    @app.post("/review/{item_id}/edit", response_model=None)
    def review_edit(
        request: Request,
        item_id: str,
        actor: str = Form(DEFAULT_ACTOR),
        title: str = Form(...),
        body_markdown: str = Form(...),
        tags: str = Form(""),
        reason: str = Form(""),
        session: Session = Depends(get_db),
    ) -> HTMLResponse | RedirectResponse:
        item = _get_item(session, item_id)
        edit_item(
            session,
            item,
            actor=actor,
            title=title,
            body_markdown=body_markdown,
            tags=tags,
            reason=reason,
        )
        session.commit()
        return _actions_response(request, item, "改稿已保存", f"/review/{item_id}")

    # -- 账号 -------------------------------------------------------------

    @app.get("/accounts", response_class=HTMLResponse)
    def accounts_page(request: Request, session: Session = Depends(get_db)) -> HTMLResponse:
        accounts = list(session.scalars(select(Account).order_by(Account.platform, Account.id)))
        return templates.TemplateResponse(
            request, "accounts.html", _ctx(request, accounts=accounts)
        )

    @app.get("/accounts/{account_id}/login", response_class=HTMLResponse)
    def account_login_page(
        request: Request, account_id: str, session: Session = Depends(get_db)
    ) -> HTMLResponse:
        account = _get_account(session, account_id)
        supports = login_flow.supports_interactive_login(account)
        return templates.TemplateResponse(
            request,
            "login.html",
            _ctx(
                request,
                account=account,
                supports_login=supports,
                # 抖音的二维码在**宿主机浏览器窗口**里，core 不代理图片，
                # 页面走另一套文案：点按钮开窗口 + 只轮询状态 + 短信验证码输入
                host_window_login=supports and account.platform == "douyin",
                pending_codes=SMS_INBOX.pending(account_id),
            ),
        )

    @app.get("/accounts/{account_id}/login/qrcode")
    def account_login_qrcode(account_id: str, session: Session = Depends(get_db)) -> JSONResponse:
        """返回登录二维码 base64 供页面轮询渲染。实现见 ``core/login_flow.py``。"""
        account = _get_account(session, account_id)
        return JSONResponse(login_flow.qrcode_payload(session, account))

    @app.get("/accounts/{account_id}/login/status")
    def account_login_status(account_id: str, session: Session = Depends(get_db)) -> JSONResponse:
        """只查登录状态（不重新取二维码），并把结果落到 Account 状态机。

        页面每 3 秒调它一次：扫码成功 → 账号自动回 ``ok`` 并放回挂起的排期项。
        """
        account = _get_account(session, account_id)
        return JSONResponse(login_flow.status_payload(session, account))

    @app.post("/accounts/{account_id}/login/start", response_model=None)
    def account_login_start(account_id: str, session: Session = Depends(get_db)) -> JSONResponse:
        """让发布器把登录窗口开起来（抖音专用，**可选能力**）。"""
        account = _get_account(session, account_id)
        return JSONResponse(login_flow.start_payload(account))

    @app.post("/accounts/{account_id}/login/code", response_model=None)
    def account_login_code(
        request: Request,
        account_id: str,
        code: str = Form(...),
        session: Session = Depends(get_db),
    ) -> HTMLResponse | JSONResponse:
        """把人工输入的短信验证码放进内存队列，并顺手转发给发布器。

        细节见 ``core/login_flow.submit_code``。验证码**不落库、不写日志明文**。
        """
        account = _get_account(session, account_id)
        payload = login_flow.submit_code(account, code)
        if _wants_fragment(request):
            detail = str(payload["forward_detail"])
            tail = f" · {detail}" if detail else ""
            return HTMLResponse(f"已提交（队列 {payload['pending']} 条）{tail}")
        return JSONResponse(payload)

    # -- 统计 -------------------------------------------------------------

    @app.get("/stats", response_class=HTMLResponse)
    def stats_page(
        request: Request, days: int = 7, session: Session = Depends(get_db)
    ) -> HTMLResponse:
        """按账号的近 N 天运营看板。聚合逻辑在 ``core/stats.py``。"""
        from core.stats import build_dashboard

        window = max(1, min(days, 90))
        dashboard = build_dashboard(session, window_days=window)
        return templates.TemplateResponse(
            request,
            "stats.html",
            _ctx(
                request,
                dash=dashboard,
                totals=dashboard.totals(),
                metric_fields=("views", "likes", "comments", "collects", "shares"),
            ),
        )

    @app.get("/stats.json")
    def stats_json(days: int = 7, session: Session = Depends(get_db)) -> dict[str, Any]:
        from core.stats import build_dashboard

        window = max(1, min(days, 90))
        payload = build_dashboard(session, window_days=window).as_dict()
        payload["content"] = dict(
            session.execute(
                select(ContentItem.status, func.count()).group_by(ContentItem.status)
            ).all()
        )
        payload["cost_entries"] = session.scalar(select(func.count()).select_from(CostLedger)) or 0
        return payload

    # -- 调度（P4）--------------------------------------------------------

    @app.get("/dev/tick")
    def dev_tick_list() -> dict[str, Any]:
        """列出可手动触发的 tick 及其当前排期。"""
        from core.scheduler import TICKS

        return {
            "ticks": sorted(TICKS),
            "usage": "POST /dev/tick/{name}",
            "note": "手动触发与定时任务走的是同一批函数（core.scheduler.TICKS）",
        }

    @app.post("/dev/tick/{name}")
    def dev_tick(
        name: str,
        account_id: str | None = None,
        platform: str | None = None,
        force: bool = False,
        respect_windows: bool | None = None,
    ) -> JSONResponse:
        """手动跑一个 tick，返回它的统计。

        和 APScheduler 注册的是**同一个函数**（``core.scheduler.TICKS``），所以
        curl 一下看到的行为就是定时任务的行为，不存在"手动能跑、定时不跑"。

        各 tick 自己开事务（``session_scope``），因此这里不注入 session。

        可选参数按 tick 的签名择优透传：

        - ``account_id`` / ``platform``：``generate`` / ``insights`` / ``login_health``
        - ``force``：``login_health``（跳过抖音节流）、``insights``（跳过 24h 节流）
        - ``respect_windows``：``metrics``。**不传时套用 ``TICK_DEFAULT_KWARGS``**
          （即生产口径 true，只在 24h / 7d 窗口到期时采样）；想立刻采一张传
          ``?respect_windows=false``。
        """
        from core.scheduler import TICKS, run_tick, tick_kwargs

        if name not in TICKS:
            raise HTTPException(status_code=404, detail=f"未知 tick: {name}；可用 {sorted(TICKS)}")
        try:
            # 参数映射与 `POST /api/v1/system/ticks/{name}` 共用一份，见 core.scheduler
            kwargs = tick_kwargs(
                name,
                account_id=account_id,
                platform=platform,
                force=force,
                respect_windows=respect_windows,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        started = datetime.now(UTC)
        try:
            stats = run_tick(name, **kwargs)
        except Exception as exc:  # tick 内部异常要看得见，不能只留在日志里
            if name == "metrics":
                logger.warning("手动触发 metrics tick 失败 category=database_or_internal")
                return JSONResponse(
                    {"ok": False, "tick": name, "error": "metrics tick failed"},
                    status_code=500,
                )
            logger.warning("手动触发 tick %s 失败: %s", name, exc, exc_info=True)
            return JSONResponse(
                {"ok": False, "tick": name, "error": f"{type(exc).__name__}: {exc}"},
                status_code=500,
            )
        return JSONResponse(
            {
                "ok": True,
                "tick": name,
                "stats": stats,
                "elapsed_s": round((datetime.now(UTC) - started).total_seconds(), 3),
            }
        )

    @app.post("/dev/sync_accounts")
    def dev_sync_accounts(
        dry_run: bool = False, session: Session = Depends(get_db)
    ) -> JSONResponse:
        """把 ``accounts.yaml`` 同步进 DB（等价于 ``python -m core.accounts sync``）。

        首次部署的第一步。**不覆盖** ``Account.status``——那是登录巡检的地盘。
        """
        from core.accounts import AccountsError, load_specs, sync_accounts

        try:
            specs = load_specs()
        except AccountsError as exc:
            raise HTTPException(status_code=422, detail=f"台账不合法: {exc}") from exc
        if not specs:
            raise HTTPException(status_code=404, detail="accounts.yaml 不存在或没有 accounts 列表")
        report = sync_accounts(session, specs, dry_run=dry_run)
        if not dry_run:
            session.commit()
        return JSONResponse({"ok": True, "dry_run": dry_run, **report.as_dict()})

    # -- 开发用 -----------------------------------------------------------

    @app.post("/dev/run_wechat_pipeline")
    def dev_run_wechat_pipeline(
        account_id: str,
        topic: str | None = None,
        skip_sourcing: bool = False,
        use_llm_review: bool = True,
        make_cover: bool = True,
        render_html: bool = True,
        session: Session = Depends(get_db),
    ) -> JSONResponse:
        """开发用：跑通 sourcing → selector → generation → review，产出一条待审内容。

        没有 ``ANTHROPIC_API_KEY`` 时自动降级到 ScriptedLLM（返回体 ``llm="scripted"``），
        因此不带凭据也能复现整条链路。

        - ``topic``：手工指定选题标题，跳过采集与选题 Agent
        - ``skip_sourcing``：不拉热榜，直接用库里已有的选题池
        - ``make_cover`` / ``render_html``：无 Playwright / Node 时可关掉
        """
        from core.dev_flow import DevFlowError, run_wechat_pipeline
        from generation.pipeline import GenerationOptions

        account = _get_account(session, account_id)
        try:
            result = run_wechat_pipeline(
                session,
                account,
                topic_title=topic,
                skip_sourcing=skip_sourcing,
                use_llm_review=use_llm_review,
                options=GenerationOptions(make_cover=make_cover, render_html=render_html),
            )
        except DevFlowError as exc:
            # 链路走不下去是预期内的（没配数据源 / 预算耗尽 / 今天没选题），不是 500
            session.rollback()
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
        session.commit()
        return JSONResponse(result.as_dict())

    @app.post("/dev/run_xhs_pipeline")
    def dev_run_xhs_pipeline(
        account_id: str,
        topic: str | None = None,
        # 字面量而不是 import generation.xhs_cards.DEFAULT_THEME：
        # 那条 import 链会把 anthropic SDK 拉进控制面的启动路径。
        # tests/generation/test_xhs_e2e.py 有一条断言盯着这两个值不许漂移。
        theme: str = "editorial",
        skip_sourcing: bool = False,
        use_llm_review: bool = True,
        make_cards: bool = True,
        commercial: bool = True,
        session: Session = Depends(get_db),
    ) -> JSONResponse:
        """开发用：跑通 sourcing → selector → 小红书图文生成 → review。

        和公众号端点同一套降级策略：没有 ``ANTHROPIC_API_KEY`` 时用 ScriptedLLM。

        - ``theme``：卡片主题，见 ``generation.xhs_cards.THEMES``（editorial/swiss/warm）
        - ``make_cards``：无 Playwright / chromium 时关掉，只出文案
        - ``commercial``：启用极限词等商业场景规则，小红书默认开
        """
        from core.dev_flow import DevFlowError, run_xhs_pipeline
        from generation.pipeline import XhsGenerationOptions
        from generation.xhs_cards import get_theme

        account = _get_account(session, account_id)
        # 提前校验主题名：不然只有在真去渲染卡片时才炸，make_cards=False 时还会静默
        try:
            get_theme(theme)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        options = XhsGenerationOptions(theme=theme, make_cards=make_cards)
        try:
            result = run_xhs_pipeline(
                session,
                account,
                topic_title=topic,
                skip_sourcing=skip_sourcing,
                use_llm_review=use_llm_review,
                commercial=commercial,
                options=options,
            )
        except DevFlowError as exc:
            session.rollback()
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
        session.commit()
        return JSONResponse(result.as_dict())

    @app.post("/dev/run_douyin_pipeline")
    def dev_run_douyin_pipeline(
        account_id: str,
        topic: str | None = None,
        skip_sourcing: bool = False,
        use_llm_review: bool = True,
        make_cover: bool = True,
        commercial: bool = True,
        skip_render: bool = True,
        session: Session = Depends(get_db),
    ) -> JSONResponse:
        """开发用：跑通 sourcing → selector → 口播脚本 → 渲染 → review。

        和另外两个端点同一套降级策略：没有 ``ANTHROPIC_API_KEY`` 时用 ScriptedLLM。

        - ``skip_render``：**默认 true**。不调 MoneyPrinterTurbo，直接挂
          ``tests/fixtures/video/sample.mp4`` 样本片，几秒钟跑完全链路。
          置 false 才会真的提交渲染任务（需要 ``docker compose --profile video up``
          且 sidecar 的 ``config.toml`` 里配好素材源 key），一条片子几分钟起步。
        - ``make_cover``：无 Playwright / chromium 时关掉，只出脚本
        """
        from core.dev_flow import DevFlowError, run_douyin_pipeline
        from generation.video_pipeline import VideoGenerationOptions

        account = _get_account(session, account_id)
        options = VideoGenerationOptions(make_cover=make_cover, skip_render=skip_render)
        try:
            result = run_douyin_pipeline(
                session,
                account,
                topic_title=topic,
                skip_sourcing=skip_sourcing,
                use_llm_review=use_llm_review,
                commercial=commercial,
                options=options,
            )
        except DevFlowError as exc:
            session.rollback()
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
        session.commit()
        return JSONResponse(result.as_dict())

    @app.post("/dev/seed")
    def dev_seed(include_topic: bool = False, session: Session = Depends(get_db)) -> dict[str, Any]:
        """注入 1 个 fake 账号 + 1 条 draft 内容，便于本地联调。"""
        account = session.get(Account, "acc_demo_xhs")
        if account is None:
            account = Account(
                id="acc_demo_xhs",
                platform="xhs",
                name="小红书 Demo 账号",
                status="ok",
                sidecar_endpoint="http://localhost:18060",
                daily_limit=5,
                profile_dir=None,
                extra={"seeded": True},
            )
            session.add(account)
            session.flush()

        if include_topic and session.get(Topic, "tpc_e2e_seed") is None:
            session.add(
                Topic(
                    id="tpc_e2e_seed",
                    source="e2e",
                    title="通勤路上如何减少无效负担",
                    score=0.8,
                    raw={"seeded": True},
                )
            )

        item_id = new_id("itm")
        bundle = ContentBundle(
            id=item_id,
            account_id=account.id,
            platform="xhs",
            title="3 个让通勤包变轻的收纳思路",
            body_markdown=(
                "# 3 个让通勤包变轻的收纳思路\n\n"
                "1. 分区收纳袋：把充电线、耳机、钥匙各归各位，翻找时间从 30 秒降到 3 秒。\n"
                "2. 一物两用：水杯选带挂扣的，省掉一个侧袋。\n"
                "3. 每周清一次：把上周没用到的东西拿出来，包会轻 200g。\n\n"
                "（这是 /dev/seed 注入的示例草稿，仅供本地联调。）"
            ),
            media=[
                MediaAsset(path="data/demo/cover.png", kind="image", cover=True),
                MediaAsset(path="data/demo/page2.png", kind="image"),
            ],
            tags=["通勤", "收纳", "好物分享"],
            platform_extra={"seeded": True},
        )
        item = ContentItem(
            id=item_id,
            account_id=account.id,
            topic_id=None,
            status=ContentStatus.DRAFT.value,
            bundle_json=bundle.model_dump(mode="json"),
            scheduled_at=utcnow() + timedelta(hours=1),
        )
        session.add(item)
        session.commit()
        return {
            "ok": True,
            "account_id": account.id,
            "content_item_id": item.id,
            "status": item.status,
            "review_url": f"/review/{item.id}",
        }


app = create_app()

__all__ = ["app", "create_app"]
