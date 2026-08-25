"""FastAPI 控制面 / 审核 UI 集成测试。"""

from __future__ import annotations

import base64

from sqlalchemy import select

from core import db
from core.models import Account, ContentItem, ReviewLog
from core.sms_inbox import SMS_INBOX
from core.state_machine import ContentStatus, ReviewAction


def _seed(client) -> dict:
    resp = client.post("/dev/seed")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _load(item_id: str) -> ContentItem:
    with db.session_scope() as session:
        item = session.get(ContentItem, item_id)
        assert item is not None
        session.expunge(item)
        return item


def test_health_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["checks"]["database"] == "ok"
    assert set(body["checks"]["publishers"]) == {"wechat_mp", "xhs", "douyin"}


def test_root_redirects_to_review(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "审核队列" in resp.text


def test_seed_then_review_then_approve(client):
    seeded = _seed(client)
    item_id = seeded["content_item_id"]

    listing = client.get("/review")
    assert listing.status_code == 200
    assert item_id in listing.text
    assert "通勤包" in listing.text

    detail = client.get(f"/review/{item_id}")
    assert detail.status_code == 200
    assert "批准（人工确认发布）" in detail.text
    assert "data/demo/cover.png" in detail.text  # 媒体缩略图占位

    resp = client.post(f"/review/{item_id}/approve", data={"actor": "auditor"})
    assert resp.status_code == 200

    # P4：批准后**自动排期**，否则调度器只扫 scheduled，整条链断在这里
    item = _load(item_id)
    assert item.status == ContentStatus.SCHEDULED
    assert item.scheduled_at is not None

    with db.session_scope() as session:
        logs = session.scalars(
            select(ReviewLog).where(ReviewLog.content_item_id == item_id).order_by(ReviewLog.at)
        ).all()
    # 人工 approve + 系统 schedule 两条，紧挨着，事后好复盘
    assert [log.action for log in logs] == [ReviewAction.APPROVE.value, "schedule"]
    assert logs[0].actor == "auditor"
    assert logs[1].actor == "operator"

    # 批准后审核队列里不再出现
    assert item_id not in client.get("/review").text
    assert item_id in client.get("/review?status=all").text


def test_approve_via_htmx_returns_fragment(client):
    item_id = _seed(client)["content_item_id"]
    resp = client.post(
        f"/review/{item_id}/approve",
        data={"actor": "auditor"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert "<html" not in resp.text.lower()
    assert "scheduled" in resp.text
    # 排期时刻要直接回显给人看（账号本地时区），这是运营最想知道的一件事
    assert "已排期至" in resp.text


def test_approve_without_available_slot_keeps_approved(client):
    """排不上期时：不报 500，内容停在 ``approved``，把原因原样告诉审核人。

    把账号日上限压到 0（"这个号先别发了"）就没有任何合法槽位。此时**不能**把内容
    推进到 ``scheduled``——排一个永远发不出去的时刻，比停下来等人改配置糟得多。
    """
    item_id = _seed(client)["content_item_id"]
    with db.session_scope() as session:
        account = session.get(Account, "acc_demo_xhs")
        assert account is not None
        account.daily_limit = 0

    resp = client.post(
        f"/review/{item_id}/approve",
        data={"actor": "auditor"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert "未能排期" in resp.text
    assert "日上限" in resp.text  # 原因要具体到"改哪儿"
    assert "**" not in resp.text  # 模板是转义输出，提示里不能混 Markdown 星号

    item = _load(item_id)
    assert item.status == ContentStatus.APPROVED
    with db.session_scope() as session:
        actions = session.scalars(
            select(ReviewLog.action).where(ReviewLog.content_item_id == item_id)
        ).all()
    assert list(actions) == [ReviewAction.APPROVE.value]  # 没排上就不该有 schedule 那条


def test_reject_stores_reason(client):
    item_id = _seed(client)["content_item_id"]
    resp = client.post(
        f"/review/{item_id}/reject", data={"actor": "auditor", "reason": "标题过于夸张"}
    )
    assert resp.status_code == 200
    item = _load(item_id)
    assert item.status == ContentStatus.REJECTED
    assert item.review_notes == "标题过于夸张"
    assert "标题过于夸张" in client.get(f"/review/{item_id}").text


def test_reject_without_reason_is_rejected(client):
    item_id = _seed(client)["content_item_id"]
    assert client.post(f"/review/{item_id}/reject", data={"actor": "a"}).status_code == 422
    assert (
        client.post(f"/review/{item_id}/reject", data={"actor": "a", "reason": "  "}).status_code
        == 422
    )
    assert _load(item_id).status == ContentStatus.DRAFT


def test_edit_produces_diff_and_audit_log(client):
    item_id = _seed(client)["content_item_id"]
    resp = client.post(
        f"/review/{item_id}/edit",
        data={
            "actor": "editor",
            "title": "改过的标题",
            "body_markdown": "改过的正文",
            "tags": "通勤, 收纳",
            "reason": "标题更克制",
        },
    )
    assert resp.status_code == 200
    item = _load(item_id)
    assert item.bundle_json["title"] == "改过的标题"
    assert item.bundle_json["tags"] == ["通勤", "收纳"]

    detail = client.get(f"/review/{item_id}")
    assert "改过的标题" in detail.text
    assert "改前" in detail.text and "改后" in detail.text  # diff 区
    assert "editor" in detail.text


def test_approve_twice_conflicts(client):
    """重复点批准必须 409。

    注意 ``scheduled → approved`` 在状态迁移表里是**合法**的（批准后想改稿要能撤回），
    所以这里靠端点的显式守卫拦，不能指望状态机报错。
    """
    item_id = _seed(client)["content_item_id"]
    assert client.post(f"/review/{item_id}/approve", data={"actor": "a"}).status_code == 200
    second = client.post(f"/review/{item_id}/approve", data={"actor": "a"})
    assert second.status_code == 409
    assert "不能批准" in second.json()["detail"]


def test_missing_item_404(client):
    assert client.get("/review/does-not-exist").status_code == 404
    assert client.post("/review/nope/approve", data={"actor": "a"}).status_code == 404


def test_accounts_page_and_login_qrcode(client):
    seeded = _seed(client)
    account_id = seeded["account_id"]

    accounts = client.get("/accounts")
    assert accounts.status_code == 200
    assert account_id in accounts.text

    page = client.get(f"/accounts/{account_id}/login")
    assert page.status_code == 200
    assert "扫码登录" in page.text

    resp = client.get(f"/accounts/{account_id}/login/qrcode")
    assert resp.status_code == 200
    body = resp.json()
    assert body["account_id"] == account_id
    assert body["status"] == "ok"
    raw = base64.b64decode(body["image_base64"], validate=True)
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(raw) > 100


def test_login_qrcode_unknown_account_404(client):
    assert client.get("/accounts/nope/login/qrcode").status_code == 404


def test_sms_code_goes_to_memory_queue(client):
    account_id = _seed(client)["account_id"]
    assert SMS_INBOX.pending(account_id) == 0

    resp = client.post(f"/accounts/{account_id}/login/code", data={"code": "135790"})
    assert resp.status_code == 200
    assert resp.json()["pending"] == 1
    assert SMS_INBOX.pending(account_id) == 1
    assert SMS_INBOX.pop(account_id) == "135790"
    assert SMS_INBOX.pop(account_id) is None

    bad = client.post(f"/accounts/{account_id}/login/code", data={"code": "   "})
    assert bad.status_code == 422


def test_sms_code_is_not_persisted(client):
    """验证码不入库：全表扫一遍确认没有明文。"""
    account_id = _seed(client)["account_id"]
    client.post(f"/accounts/{account_id}/login/code", data={"code": "246813"})
    with db.session_scope() as session:
        rows = session.execute(
            select(ReviewLog.reason, ReviewLog.before_json, ReviewLog.after_json)
        ).all()
    assert all("246813" not in str(row) for row in rows)


def test_stats_pages(client):
    _seed(client)
    page = client.get("/stats")
    assert page.status_code == 200
    assert "内容状态分布" in page.text
    assert "今日成本" in page.text

    data = client.get("/stats.json").json()
    assert data["content"] == {"draft": 1}
    assert data["budget"]["tokens"]["limit"] == 1000.0
    assert data["budget"]["tokens"]["used"] == 0.0


def test_seed_is_reusable(client):
    first = _seed(client)
    second = _seed(client)
    assert first["account_id"] == second["account_id"]
    assert first["content_item_id"] != second["content_item_id"]
    assert client.get("/stats.json").json()["content"]["draft"] == 2


# ---------------------------------------------------- P2：小红书真二维码代理


def _register_xhs_publisher(**stub_kwargs):
    """把 xhs 平台指向真实 ``XhsPublisher`` + 不联网的 stub 客户端。

    默认注册表在测试里是 FakePublisher（P0 占位二维码），这里覆盖成真实实现，
    验证 ``/accounts/{id}/login/*`` 走的是 sidecar 那条路。
    """
    from core.scheduler import RateLimiter
    from publishers.registry import register
    from publishers.xhs.publisher import XhsPublisher
    from publishers.xhs.stub import StubXhsMcpClient

    stub = StubXhsMcpClient(**stub_kwargs)

    def factory(account_id: str, **kwargs):
        return XhsPublisher(
            account_id,
            client=stub,
            limiter=RateLimiter(min_interval_seconds=0),
            daily_limit=50,
            resolve_attempts=1,
            sleeper=lambda _seconds: None,
            **kwargs,
        )

    register("xhs", factory)
    return stub


def test_login_qrcode_proxies_real_sidecar(client):
    account_id = _seed(client)["account_id"]
    stub = _register_xhs_publisher(logged_in=False)

    resp = client.get(f"/accounts/{account_id}/login/qrcode")

    assert resp.status_code == 200
    body = resp.json()
    assert body["platform"] == "xhs"
    assert body["placeholder"] is False, "应当来自真实 sidecar，而不是 P0 占位图"
    # sidecar 的二维码有效期（Go duration "4m0s"）要透出来给页面做倒计时
    assert body["expires_in"] == 240
    raw = base64.b64decode(body["image_base64"], validate=True)
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    assert stub.calls["get_login_qrcode"] == 1
    # 未登录 -> 账号被置为 needs_relogin
    assert body["status"] == "needs_relogin"
    assert body["account_status"] == "needs_relogin"


def test_login_status_endpoint_marks_account_ok_after_scan(client):
    account_id = _seed(client)["account_id"]
    stub = _register_xhs_publisher(logged_in=False)

    first = client.get(f"/accounts/{account_id}/login/status").json()
    assert first["logged_in"] is False
    assert first["account_status"] == "needs_relogin"

    # 人在手机上扫了码
    stub.logged_in = True
    second = client.get(f"/accounts/{account_id}/login/status").json()

    assert second["logged_in"] is True
    assert second["status"] == "ok"
    assert second["account_status"] == "ok", "扫码成功后账号要自动回 ok"
    assert stub.calls["login_status"] >= 2


def test_login_status_restores_suspended_items(client):
    """扫码成功要顺带把 needs_relogin 期间挂起的排期项放回去。"""
    from core.models import Account, ContentItem, new_id
    from core.state_machine import ContentStatus

    account_id = _seed(client)["account_id"]
    stub = _register_xhs_publisher(logged_in=False)
    client.get(f"/accounts/{account_id}/login/status")

    item_id = new_id("itm")
    with db.session_scope() as session:
        assert session.get(Account, account_id).status == "needs_relogin"
        session.add(
            ContentItem(
                id=item_id,
                account_id=account_id,
                status=ContentStatus.SUSPENDED.value,
                prev_status=ContentStatus.SCHEDULED.value,
                bundle_json={},
            )
        )

    stub.logged_in = True
    assert client.get(f"/accounts/{account_id}/login/status").json()["account_status"] == "ok"

    with db.session_scope() as session:
        assert session.get(ContentItem, item_id).status == ContentStatus.SCHEDULED


def test_login_page_tells_user_to_scan_with_xhs_app(client):
    account_id = _seed(client)["account_id"]
    _register_xhs_publisher()
    page = client.get(f"/accounts/{account_id}/login")
    assert page.status_code == 200
    assert "小红书 App" in page.text
    assert "不允许多网页端同时登录" in page.text


def test_login_status_unsupported_platform_501(client):
    """公众号没有扫码流程，端点要明确回 501 而不是假装成功。"""
    from core.models import Account
    from publishers.registry import register_builtin_publishers

    register_builtin_publishers()
    with db.session_scope() as session:
        session.add(
            Account(id="acc_mp", platform="wechat_mp", name="公众号", status="ok", extra={})
        )
    assert client.get("/accounts/acc_mp/login/status").status_code == 501


# ---------------------------------------------- 抖音成片：看完整片才能批准 (P3)


def _seed_douyin(client) -> str:
    """跑一遍 dev 抖音链路（样本片 + 不渲染封面），返回内容项 id。"""
    from core.models import Account

    with db.session_scope() as session:
        if session.get(Account, "douyin-demo-01") is None:
            session.add(
                Account(
                    id="douyin-demo-01",
                    platform="douyin",
                    name="抖音测试号 01",
                    status="ok",
                    daily_limit=2,
                    extra={},
                )
            )
    resp = client.post(
        "/dev/run_douyin_pipeline",
        params={
            "account_id": "douyin-demo-01",
            "topic": "通勤成本",
            "skip_sourcing": True,
            "use_llm_review": False,
            "make_cover": False,
            "skip_render": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["llm"] == "scripted"
    assert body["render"]["skip_render"] is True
    assert body["render"]["video"].endswith("video.mp4")
    return body["content_item_id"]


def test_douyin_pipeline_produces_reviewable_item(client):
    item_id = _seed_douyin(client)
    item = _load(item_id)
    assert item.platform == "douyin"
    assert item.status == ContentStatus.DRAFT
    kinds = [m["kind"] for m in item.bundle_json["media"]]
    assert kinds == ["video"]

    detail = client.get(f"/review/{item_id}")
    assert detail.status_code == 200
    assert "<video controls" in detail.text
    assert "已<b>完整观看</b>" in detail.text
    assert "口播稿" in detail.text
    # 样本片必须被明确标出来，免得被当成真实产出
    assert "样本片（skip_render=true）" in detail.text


def test_douyin_video_cannot_be_approved_without_watching(client):
    """计划 2.2 的硬约束：成片必须人工看完整片才放行。前端 disabled 挡不住 curl。"""
    item_id = _seed_douyin(client)

    resp = client.post(f"/review/{item_id}/approve", data={"actor": "auditor"})
    assert resp.status_code == 422
    assert "完整观看" in resp.json()["detail"]
    assert _load(item_id).status == ContentStatus.DRAFT

    # 随便填个值也不行，必须是明确的真值
    assert (
        client.post(
            f"/review/{item_id}/approve", data={"actor": "auditor", "watched": "maybe"}
        ).status_code
        == 422
    )

    ok = client.post(f"/review/{item_id}/approve", data={"actor": "auditor", "watched": "true"})
    assert ok.status_code == 200
    item = _load(item_id)
    assert item.status == ContentStatus.SCHEDULED
    # "看过了"是合规证据链的一部分，要能查得到
    assert item.bundle_json["platform_extra"]["watched_by"] == "auditor"
    assert item.bundle_json["platform_extra"]["watched_at"]


def test_watch_gate_does_not_apply_to_image_only_content(client):
    """图文内容不该被这道闸门拦住。"""
    item_id = _seed(client)["content_item_id"]
    assert client.post(f"/review/{item_id}/approve", data={"actor": "a"}).status_code == 200


def test_douyin_media_endpoint_serves_the_video(client):
    item_id = _seed_douyin(client)
    resp = client.get(f"/review/{item_id}/media/0")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "video/mp4"
    assert resp.content[4:8] == b"ftyp"


# ------------------------------------------------- P3：抖音登录页与验证码转发


def _seed_douyin_account(account_id: str = "acc_dy") -> str:
    from core.models import Account

    with db.session_scope() as session:
        session.add(
            Account(
                id=account_id,
                platform="douyin",
                name="抖音测试号",
                status="ok",
                daily_limit=2,
                profile_dir=f"./profiles/douyin/{account_id}",
                extra={"identity_hint": "抖音 Stub 账号"},
            )
        )
    return account_id


def _register_douyin_publisher(**stub_kwargs):
    """把 douyin 平台指向真实 ``DouyinPublisher`` + 不联网的 stub 客户端。"""
    from core.scheduler import RateLimiter
    from publishers.douyin.publisher import DouyinPublisher, MinIntervalGate
    from publishers.douyin.stub import StubDouyinServiceClient
    from publishers.registry import register

    stub = StubDouyinServiceClient(**stub_kwargs)

    def factory(account_id: str, **kwargs):
        return DouyinPublisher(
            account_id,
            client=stub,
            limiter=RateLimiter(min_interval_seconds=0),
            gate=MinIntervalGate(),
            daily_limit=2,
            identity_hint="抖音 Stub 账号",
            **kwargs,
        )

    register("douyin", factory)
    return stub


def test_douyin_login_page_points_at_host_window(client):
    """抖音的二维码在宿主机窗口里，页面不能渲染一张假二维码骗人扫。"""
    account_id = _seed_douyin_account()
    _register_douyin_publisher()

    page = client.get(f"/accounts/{account_id}/login")

    assert page.status_code == 200
    assert "宿主机" in page.text
    assert "打开宿主机登录窗口" in page.text
    assert "不做任何验证码自动识别" in page.text
    # 不该出现小红书那套二维码 img 与二维码轮询
    assert 'id="qr"' not in page.text
    assert "/login/qrcode" not in page.text


def test_douyin_login_start_opens_host_window(client):
    account_id = _seed_douyin_account()
    stub = _register_douyin_publisher(logged_in=False)

    resp = client.post(f"/accounts/{account_id}/login/start")

    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "waiting_user"
    assert stub.calls["start_login"] == 1


def test_login_start_is_501_for_platforms_without_it(client):
    """小红书走 core 代理的二维码，没有"开宿主机窗口"这一步。"""
    account_id = _seed(client)["account_id"]
    _register_xhs_publisher()
    assert client.post(f"/accounts/{account_id}/login/start").status_code == 501


def test_sms_code_is_forwarded_to_douyin_publisher(client):
    """验证码既进内存队列，也立刻转发给上传器填进宿主机窗口。"""
    account_id = _seed_douyin_account()
    stub = _register_douyin_publisher()

    resp = client.post(f"/accounts/{account_id}/login/code", data={"code": "135790"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["forwarded"] is True
    assert body["pending"] == 1  # 队列那份仍然留着，发布器也可以自己 pop
    assert stub.sms_codes == ["135790"]
    assert SMS_INBOX.pop(account_id) == "135790"


def test_sms_code_forward_failure_does_not_break_the_queue(client):
    """上传器那边没有验证码输入框时，转发失败不该让人白填一次。"""
    from publishers.base import NeedsReloginError
    from publishers.registry import register

    account_id = _seed_douyin_account()

    class Refusing:
        platform = "douyin"

        def __init__(self, account_id: str, **kwargs) -> None:
            self.account_id = account_id

        def submit_sms_code(self, code: str) -> bool:
            raise NeedsReloginError("当前页面上没有验证码输入框")

    register("douyin", Refusing)

    body = client.post(f"/accounts/{account_id}/login/code", data={"code": "246813"}).json()

    assert body["ok"] is True
    assert body["forwarded"] is False
    assert "没有验证码输入框" in body["forward_detail"]
    assert SMS_INBOX.pop(account_id) == "246813"


def test_sms_code_stays_out_of_logs_and_db(client, caplog):
    """红线：验证码不落库、不进日志明文。"""
    import logging

    account_id = _seed_douyin_account()
    _register_douyin_publisher()
    with caplog.at_level(logging.DEBUG):
        client.post(f"/accounts/{account_id}/login/code", data={"code": "864209"})
    assert "864209" not in caplog.text
    with db.session_scope() as session:
        rows = session.execute(
            select(ReviewLog.reason, ReviewLog.before_json, ReviewLog.after_json)
        ).all()
    assert all("864209" not in str(row) for row in rows)


# --------------------------------------------------------------- 工作台静态站


def _workbench_client(monkeypatch, dist):
    """用指定的产物目录起一份 app（挂载点在 create_app 里定，所以要重建）。"""
    from fastapi.testclient import TestClient

    from core.main import create_app

    monkeypatch.setenv("SW_UI_DIST", str(dist))
    return TestClient(create_app())


def test_workbench_serves_static_export(tmp_path, monkeypatch):
    """有产物时 /workbench/ 直出 index.html，子路由走目录里的 index.html。"""
    dist = tmp_path / "out"
    (dist / "review").mkdir(parents=True)
    (dist / "index.html").write_text("<h1>工作台</h1>", encoding="utf-8")
    (dist / "review" / "index.html").write_text("<h1>审核队列</h1>", encoding="utf-8")
    (dist / "404.html").write_text("<h1>没有这一页</h1>", encoding="utf-8")

    with _workbench_client(monkeypatch, dist) as client:
        root = client.get("/workbench/")
        assert root.status_code == 200
        assert "工作台" in root.text

        # 不带尾斜杠也要能到（StaticFiles 会 307 到带斜杠的形式）
        sub = client.get("/workbench/review")
        assert sub.status_code == 200
        assert "审核队列" in sub.text

        missing = client.get("/workbench/nope/")
        assert missing.status_code == 404
        assert "没有这一页" in missing.text


def test_workbench_without_build_shows_instructions(tmp_path, monkeypatch):
    """没构建过时给一页构建指引，**不能** 500，也不能是裸 404。"""
    with _workbench_client(monkeypatch, tmp_path / "does-not-exist") as client:
        resp = client.get("/workbench/")
        assert resp.status_code == 200
        assert "scripts/build_ui.sh" in resp.text
        # 任意子路径也落到同一页，免得刷新一个深链接就白屏
        assert client.get("/workbench/review/").status_code == 200


def test_workbench_mount_does_not_shadow_existing_routes(tmp_path, monkeypatch):
    """挂载点不许把既有的 HTML 页面 / JSON API 遮掉。"""
    dist = tmp_path / "out"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<h1>工作台</h1>", encoding="utf-8")

    with _workbench_client(monkeypatch, dist) as client:
        assert client.get("/review").status_code == 200
        assert client.get("/api/v1/system/info").json()["ok"] is True
        assert client.get("/health").json()["status"] == "ok"
