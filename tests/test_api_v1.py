"""工作台 JSON API（``/api/v1``）契约测试。

盯三件事：

1. **envelope 一致性**——每个端点都是 ``{"ok", "data", "error"}``，错误也一样；
2. **闸门没被绕过**——JSON 这条路必须和表单端点一样拦住"没看完的成片"、
   "窗口外的排期"、"死信直接复活"；
3. **token 两态**——``SW_UI_TOKEN`` 空 / 非空的行为。
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

import prompts
from core import db
from core.api import content as content_api
from core.config import reload_settings
from core.models import (
    Account,
    ContentItem,
    CostLedger,
    PublishRecord,
    RenderJob,
    ReviewLog,
    Topic,
    new_id,
    utcnow,
)
from core.ratelimit import local_day_start
from core.state_machine import ContentStatus
from publishers.base import ContentBundle, MediaAsset
from tests.conftest import make_account, make_item, make_publish_record

SHANGHAI = ZoneInfo("Asia/Shanghai")

#: 无副作用、任何时候都该 200 的 GET 端点，用于 envelope 参数化
READ_ENDPOINTS = [
    "/api/v1/dashboard",
    "/api/v1/review",
    "/api/v1/accounts",
    "/api/v1/content",
    "/api/v1/topics",
    "/api/v1/jobs/render",
    "/api/v1/jobs/publish_records",
    "/api/v1/jobs/dead_letters",
    "/api/v1/stats",
    "/api/v1/costs",
    "/api/v1/insights",
    "/api/v1/system/ticks",
    "/api/v1/system/info",
]


# ------------------------------------------------------------------ 造数工具


def _seed(client) -> dict:
    resp = client.post("/dev/seed")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _data(resp) -> dict:
    body = resp.json()
    assert body["ok"] is True, body
    assert body["error"] is None
    return body["data"]


def _error(resp) -> dict:
    body = resp.json()
    assert body["ok"] is False, body
    assert body["data"] is None
    return body["error"]


def _windowed_account(account_id: str = "acc-win", *, daily_limit: int = 5) -> str:
    """带发布时段窗口的账号（09:00-11:00 上海时间，最小间隔 60 分钟）。"""
    with db.session_scope() as session:
        account = make_account(session, account_id=account_id, daily_limit=daily_limit)
        account.extra = {
            "publish_windows": ["09:00-11:00"],
            "timezone": "Asia/Shanghai",
            "min_interval_minutes": 60,
        }
    return account_id


def _video_item(client) -> str:
    """一条带视频的内容（触发"必须看完整片"闸门）。"""
    with db.session_scope() as session:
        account = make_account(session, account_id="acc-dy", platform="douyin", daily_limit=2)
        item_id = new_id("itm")
        bundle = ContentBundle(
            id=item_id,
            account_id=account.id,
            platform="douyin",
            title="成片标题",
            body_markdown="口播稿",
            media=[MediaAsset(path="tests/fixtures/video/sample.mp4", kind="video")],
        )
        session.add(
            ContentItem(
                id=item_id,
                account_id=account.id,
                status=ContentStatus.DRAFT.value,
                bundle_json=bundle.model_dump(mode="json"),
            )
        )
    return item_id


def _next_window_slot(*, days_ahead: int = 1, hour: int = 9, minute: int = 30) -> datetime:
    local = datetime.now(SHANGHAI) + timedelta(days=days_ahead)
    return local.replace(hour=hour, minute=minute, second=0, microsecond=0).astimezone(UTC)


# ------------------------------------------------------------------ envelope


@pytest.mark.parametrize("url", READ_ENDPOINTS)
def test_read_endpoints_share_the_envelope(client, url):
    _seed(client)
    resp = client.get(url)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"ok", "data", "error"}
    assert body["ok"] is True
    assert body["error"] is None
    assert body["data"] is not None


@pytest.mark.parametrize(
    "url",
    [
        "/api/v1/review",
        "/api/v1/content",
        "/api/v1/topics",
        "/api/v1/jobs/render",
        "/api/v1/jobs/publish_records",
        "/api/v1/jobs/dead_letters",
    ],
)
def test_list_endpoints_share_the_page_shape(client, url):
    _seed(client)
    data = _data(client.get(url))
    assert set(data) == {"items", "total", "limit", "offset"}
    assert isinstance(data["items"], list)
    assert data["total"] >= len(data["items"])


def test_errors_share_the_envelope(client):
    resp = client.get("/api/v1/review/does-not-exist")
    assert resp.status_code == 404
    error = _error(resp)
    assert error["code"] == "not_found"
    assert "does-not-exist" in error["message"]


def test_html_endpoints_keep_the_old_error_shape(client):
    """既有页面端点的错误体一个字节没变（HTMX 与老测试都指着它）。"""
    resp = client.get("/review/does-not-exist")
    assert resp.status_code == 404
    assert set(resp.json()) == {"detail"}


def test_validation_error_is_wrapped(client):
    resp = client.get("/api/v1/review", params={"limit": 9999})
    assert resp.status_code == 422
    error = _error(resp)
    assert error["code"] == "validation_error"
    assert "limit" in error["message"]


def test_pagination_slices(client):
    account_id = _seed(client)["account_id"]
    with db.session_scope() as session:
        account = session.get(Account, account_id)
        for _ in range(3):
            make_item(session, account, status=ContentStatus.DRAFT.value)

    first = _data(client.get("/api/v1/review", params={"limit": 2, "offset": 0}))
    second = _data(client.get("/api/v1/review", params={"limit": 2, "offset": 2}))
    assert first["total"] == 4  # seed 的 1 条 + 上面 3 条
    assert len(first["items"]) == 2
    assert len(second["items"]) == 2
    assert {i["id"] for i in first["items"]} & {i["id"] for i in second["items"]} == set()


# ---------------------------------------------------------------------- 认证


def _enable_token(monkeypatch, token: str = "s3cret-token") -> str:
    monkeypatch.setenv("SW_UI_TOKEN", token)
    reload_settings()
    return token


def test_no_token_configured_means_open(client):
    assert client.get("/api/v1/system/info").status_code == 200
    assert (
        _data(client.post("/api/v1/auth/login", json={"token": "随便填"}))["auth_required"] is False
    )


def test_token_required_when_configured(client, monkeypatch):
    token = _enable_token(monkeypatch)

    anonymous = client.get("/api/v1/system/info")
    assert anonymous.status_code == 401
    assert _error(anonymous)["code"] == "unauthorized"

    wrong = client.get("/api/v1/system/info", headers={"Authorization": f"Bearer {token}x"})
    assert wrong.status_code == 401

    malformed = client.get("/api/v1/system/info", headers={"Authorization": token})
    assert malformed.status_code == 401

    good = client.get("/api/v1/system/info", headers={"Authorization": f"Bearer {token}"})
    assert good.status_code == 200
    assert _data(good)["auth_required"] is True


def test_token_guard_covers_writes_too(client, monkeypatch):
    """权限绕过测试：写端点不能因为"没挂守卫"而漏出去。"""
    item_id = _seed(client)["content_item_id"]
    _enable_token(monkeypatch)
    resp = client.post(f"/api/v1/review/{item_id}/approve", json={"actor": "attacker"})
    assert resp.status_code == 401
    with db.session_scope() as session:
        assert session.get(ContentItem, item_id).status == ContentStatus.DRAFT


def test_every_api_route_is_guarded(client, monkeypatch):
    """逐路由的权限绕过测试：从 OpenAPI 里枚举全部 ``/api/v1`` 端点，一个都不许漏。

    只有 ``/auth/login`` 例外（登录探针，token 在 body 里），它自己会校验。
    """
    schema = client.get("/openapi.json").json()
    _enable_token(monkeypatch)

    checked = 0
    for path, methods in schema["paths"].items():
        if not path.startswith("/api/v1") or path == "/api/v1/auth/login":
            continue
        url = path.replace("{item_id}", "itm-x").replace("{account_id}", "acc-x")
        url = url.replace("{topic_id}", "top-x").replace("{name}", "metrics")
        for method in methods:
            resp = client.request(method.upper(), url, json={})
            assert resp.status_code == 401, f"{method.upper()} {url} 没有被 token 拦住"
            assert _error(resp)["code"] == "unauthorized"
            checked += 1
    assert checked == 42, f"路由数变了（现在 {checked}），确认新端点也挂了守卫"


def test_auth_login_probe(client, monkeypatch):
    token = _enable_token(monkeypatch)
    assert client.post("/api/v1/auth/login", json={"token": "nope"}).status_code == 401
    body = _data(client.post("/api/v1/auth/login", json={"token": token}))
    assert body == {"ok": True, "auth_required": True, "message": "已认证"}


def test_html_pages_are_never_token_guarded(client, monkeypatch):
    """token 只管 /api/v1；页面与既有 JSON 端点不受影响（并行期还要用）。"""
    _seed(client)
    _enable_token(monkeypatch)
    assert client.get("/review").status_code == 200
    assert client.get("/health").status_code == 200


# ---------------------------------------------------------------------- 审核


def test_review_queue_and_detail(client):
    seeded = _seed(client)
    item_id = seeded["content_item_id"]

    page = _data(client.get("/api/v1/review"))
    assert page["total"] == 1
    row = page["items"][0]
    assert row["id"] == item_id
    assert row["platform"] == "xhs"
    assert row["status"] == "draft"
    assert row["media"]["images"] == 2
    assert row["needs_watch"] is False
    # seed 的示例图不在磁盘上 → 没有可用封面，前端据此显示占位图
    assert row["cover_url"] is None

    detail = _data(client.get(f"/api/v1/review/{item_id}"))
    assert detail["item"]["id"] == item_id
    assert detail["bundle"]["title"].startswith("3 个让通勤包")
    assert detail["bundle"]["media"][0]["index"] == 0
    assert detail["slot"]["account_windows"] == "全天"
    assert detail["media_url_template"] == "/review/{item_id}/media/{index}"


def test_review_queue_filters(client):
    _seed(client)
    assert _data(client.get("/api/v1/review", params={"platform": "douyin"}))["total"] == 0
    assert _data(client.get("/api/v1/review", params={"platform": "xhs"}))["total"] == 1
    assert _data(client.get("/api/v1/review", params={"account_id": "nope"}))["total"] == 0
    assert _data(client.get("/api/v1/review", params={"status": "all"}))["total"] == 1


def test_review_detail_exposes_machine_review(client):
    item_id = _seed(client)["content_item_id"]
    with db.session_scope() as session:
        item = session.get(ContentItem, item_id)
        item.review_notes = (
            "机器审核通过：block 0 / warn 1 / info 0\n[warn] lexicon.测试 · 命中「x」"
        )
        session.add(
            ReviewLog(
                id=new_id("rvl"),
                content_item_id=item_id,
                actor="system",
                action="machine_review",
                reason="机器审核通过",
                after_json={
                    "passed": True,
                    "blocking": 0,
                    "warnings": 1,
                    "stages_run": ["lexicon", "inspect"],
                    "stages_skipped": {"llm_semantic": "未注入 LLM 客户端"},
                    "suggested_edits": {},
                },
                at=utcnow(),
            )
        )
    detail = _data(client.get(f"/api/v1/review/{item_id}"))
    review = detail["machine_review"]
    assert review["passed"] is True
    assert review["warnings"] == 1
    assert review["stages_skipped"]["llm_semantic"] == "未注入 LLM 客户端"
    assert len(review["notes"]) == 2


def test_machine_review_action_name_does_not_drift():
    """``core/api/rows.py`` 抄了字面量（不 import review.pipeline，那条链会拉进 anthropic）。"""
    from core.api.rows import MACHINE_REVIEW_ACTION
    from review.pipeline import MACHINE_REVIEW_ACTION as canonical

    assert canonical == MACHINE_REVIEW_ACTION


def test_approve_schedules_and_writes_audit_log(client):
    item_id = _seed(client)["content_item_id"]
    data = _data(client.post(f"/api/v1/review/{item_id}/approve", json={"actor": "auditor"}))

    assert data["scheduled"] is True
    assert data["scheduled_at"] is not None
    assert "已批准，已排期至" in data["message"]
    assert data["item"]["status"] == "scheduled"

    with db.session_scope() as session:
        item = session.get(ContentItem, item_id)
        assert item.status == ContentStatus.SCHEDULED
        actions = list(
            session.scalars(
                select(ReviewLog.action)
                .where(ReviewLog.content_item_id == item_id)
                .order_by(ReviewLog.at)
            )
        )
    assert actions == ["approve", "schedule"]


def test_approve_without_slot_keeps_approved(client):
    """排不上期不是 500：内容停在 approved，原因原样告诉人。"""
    item_id = _seed(client)["content_item_id"]
    with db.session_scope() as session:
        session.get(Account, "acc_demo_xhs").daily_limit = 0

    data = _data(client.post(f"/api/v1/review/{item_id}/approve", json={"actor": "auditor"}))
    assert data["scheduled"] is False
    assert "未能排期" in data["message"] and "日上限" in data["message"]
    assert data["item"]["status"] == "approved"


def test_approve_twice_conflicts(client):
    item_id = _seed(client)["content_item_id"]
    assert client.post(f"/api/v1/review/{item_id}/approve", json={}).status_code == 200
    second = client.post(f"/api/v1/review/{item_id}/approve", json={})
    assert second.status_code == 409
    assert _error(second)["code"] == "invalid_state"


def test_video_content_needs_watch_confirmation(client):
    """计划 2.2 的硬约束在 JSON 这条路上同样成立（前端 disabled 挡不住 curl）。"""
    item_id = _video_item(client)
    row = _data(client.get(f"/api/v1/review/{item_id}"))["item"]
    assert row["needs_watch"] is True

    refused = client.post(f"/api/v1/review/{item_id}/approve", json={"actor": "auditor"})
    assert refused.status_code == 422
    assert _error(refused)["code"] == "watch_required"
    with db.session_scope() as session:
        assert session.get(ContentItem, item_id).status == ContentStatus.DRAFT

    ok = client.post(
        f"/api/v1/review/{item_id}/approve", json={"actor": "auditor", "watched": True}
    )
    assert ok.status_code == 200
    with db.session_scope() as session:
        extra = session.get(ContentItem, item_id).bundle_json["platform_extra"]
    assert extra["watched_by"] == "auditor"
    assert extra["watched_at"]


def test_reject_requires_reason(client):
    item_id = _seed(client)["content_item_id"]
    blank = client.post(f"/api/v1/review/{item_id}/reject", json={"reason": "   "})
    assert blank.status_code == 422
    assert _error(blank)["code"] == "reason_required"
    missing = client.post(f"/api/v1/review/{item_id}/reject", json={})
    assert missing.status_code == 422  # pydantic 缺字段
    assert _error(missing)["code"] == "validation_error"

    data = _data(client.post(f"/api/v1/review/{item_id}/reject", json={"reason": "标题夸张"}))
    assert data["item"]["status"] == "rejected"
    assert data["item"]["review_notes"] == "标题夸张"


def test_edit_updates_bundle_and_diff(client):
    item_id = _seed(client)["content_item_id"]
    data = _data(
        client.post(
            f"/api/v1/review/{item_id}/edit",
            json={
                "actor": "editor",
                "title": "改过的标题",
                "body_markdown": "改过的正文",
                "tags": ["通勤", "收纳"],
                "reason": "更克制",
            },
        )
    )
    assert data["item"]["title"] == "改过的标题"
    assert data["item"]["tags"] == ["通勤", "收纳"]
    detail = _data(client.get(f"/api/v1/review/{item_id}"))
    assert "改前" in detail["diff"] and "改后" in detail["diff"]
    assert detail["logs"][0]["actor"] == "editor"
    assert detail["logs"][0]["is_human"] is True


def test_edit_rejects_broken_bundle(client):
    item_id = _seed(client)["content_item_id"]
    with db.session_scope() as session:
        session.get(ContentItem, item_id).bundle_json = {"平台": "乱写的"}
    resp = client.post(f"/api/v1/review/{item_id}/edit", json={"title": "t", "body_markdown": "b"})
    assert resp.status_code == 422
    assert _error(resp)["code"] == "invalid_bundle"


# ---------------------------------------------------------------- 内容与排期


def test_content_list_and_timeline_filters(client):
    account_id = _windowed_account()
    with db.session_scope() as session:
        account = session.get(Account, account_id)
        make_item(session, account, status=ContentStatus.SCHEDULED.value, title="未来那条")

    page = _data(client.get("/api/v1/content", params={"account_id": account_id}))
    assert page["total"] == 1
    row = page["items"][0]
    assert row["timeline_at"] is not None
    assert row["slot_text"].endswith("（Asia/Shanghai）")

    future = (utcnow() + timedelta(days=3)).isoformat()
    assert _data(client.get("/api/v1/content", params={"from": future}))["total"] == 0
    assert _data(client.get("/api/v1/content", params={"to": future}))["total"] == 1
    assert _data(client.get("/api/v1/content", params={"platform": "douyin"}))["total"] == 0


def test_content_detail_and_404(client):
    item_id = _seed(client)["content_item_id"]
    detail = _data(client.get(f"/api/v1/content/{item_id}"))
    assert detail["item"]["id"] == item_id
    assert detail["bundle"]["tags"]
    assert client.get("/api/v1/content/nope").status_code == 404


def test_content_slots_returns_ordered_windowed_slots(client, monkeypatch):
    """槽位查询按账号窗口连续取值，时间锚不得依赖墙钟或同分钟重复。"""
    now = datetime(2026, 8, 20, 8, 30, tzinfo=UTC)
    monkeypatch.setattr(content_api, "_slot_now", lambda: now)
    with db.session_scope() as session:
        account = make_account(
            session,
            daily_limit=10,
            extra={"timezone": "UTC", "publish_windows": ["09:00-11:00"]},
        )
        item_id = make_item(session, account, now=now).id

    data = _data(client.get(f"/api/v1/content/{item_id}/slots", params={"count": 3}))
    assert data["timezone"] == "UTC"
    assert [datetime.fromisoformat(slot["at"]) for slot in data["slots"]] == [
        datetime(2026, 8, 20, 9, 0, tzinfo=UTC),
        datetime(2026, 8, 20, 9, 1, tzinfo=UTC),
        datetime(2026, 8, 20, 9, 2, tzinfo=UTC),
    ]
    assert [slot["window"] for slot in data["slots"]] == ["09:00-11:00"] * 3
    assert all(slot["slot_text"].endswith("（UTC）") for slot in data["slots"])


def test_content_slots_counts_returned_slots_toward_daily_limit(client, monkeypatch):
    """变异防护：同一响应内返回的槽位也必须累计占用日上限。"""
    now = datetime(2026, 8, 20, 8, 30, tzinfo=UTC)
    monkeypatch.setattr(content_api, "_slot_now", lambda: now)
    with db.session_scope() as session:
        account = make_account(
            session,
            daily_limit=2,
            extra={"timezone": "UTC", "publish_windows": ["09:00-12:00"]},
        )
        item_id = make_item(session, account, now=now).id

    slots = _data(client.get(f"/api/v1/content/{item_id}/slots", params={"count": 3}))["slots"]
    moments = [datetime.fromisoformat(slot["at"]) for slot in slots]
    assert [moment.date() for moment in moments[:2]] == [now.date(), now.date()]
    assert moments[2] == datetime(2026, 8, 21, 9, 0, tzinfo=UTC)


def test_content_slots_respects_existing_scheduled_interval(client, monkeypatch):
    now = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)
    monkeypatch.setattr(content_api, "_slot_now", lambda: now)
    with db.session_scope() as session:
        account = make_account(
            session,
            extra={
                "timezone": "UTC",
                "publish_windows": ["09:00-11:00"],
                "min_interval_minutes": 30,
            },
        )
        item_id = make_item(session, account, now=now).id
        make_item(session, account, scheduled_in_minutes=15, now=now)

    data = _data(client.get(f"/api/v1/content/{item_id}/slots", params={"count": 1}))
    assert datetime.fromisoformat(data["slots"][0]["at"]) == datetime(
        2026, 8, 20, 9, 45, tzinfo=UTC
    )


def test_content_slots_skips_to_next_day_when_daily_limit_is_full(client, monkeypatch):
    now = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)
    monkeypatch.setattr(content_api, "_slot_now", lambda: now)
    with db.session_scope() as session:
        account = make_account(
            session,
            daily_limit=1,
            extra={"timezone": "UTC", "publish_windows": ["08:00-10:00"]},
        )
        item_id = make_item(session, account, now=now).id
        make_item(session, account, scheduled_in_minutes=60, now=now)

    data = _data(client.get(f"/api/v1/content/{item_id}/slots", params={"count": 1}))
    assert datetime.fromisoformat(data["slots"][0]["at"]) == datetime(2026, 8, 21, 8, 0, tzinfo=UTC)


def test_content_slots_uses_local_midnight_for_overnight_window_limit(client, monkeypatch):
    """跨零点窗口内，日额度在账号本地零点重置后应立即重新可用。"""
    now = datetime(2026, 8, 20, 15, 30, tzinfo=UTC)  # Asia/Shanghai 23:30
    monkeypatch.setattr(content_api, "_slot_now", lambda: now)
    with db.session_scope() as session:
        account = make_account(
            session,
            daily_limit=1,
            extra={"timezone": "Asia/Shanghai", "publish_windows": ["22:00-02:00"]},
        )
        item_id = make_item(session, account, now=now).id
        make_publish_record(session, account.id, at=datetime(2026, 8, 20, 15, 0, tzinfo=UTC))

    data = _data(client.get(f"/api/v1/content/{item_id}/slots", params={"count": 1}))
    assert datetime.fromisoformat(data["slots"][0]["at"]) == datetime(
        2026, 8, 20, 16, 0, tzinfo=UTC
    )
    assert data["slots"][0]["window"] == "22:00-02:00"


def test_content_slots_formats_all_day_window_as_time_range(client, monkeypatch):
    now = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)
    monkeypatch.setattr(content_api, "_slot_now", lambda: now)
    with db.session_scope() as session:
        account = make_account(session, extra={"timezone": "UTC"})
        item_id = make_item(session, account, now=now).id

    data = _data(client.get(f"/api/v1/content/{item_id}/slots", params={"count": 1}))
    assert data["slots"][0]["window"] == "00:00-24:00"


def test_content_slots_for_suspended_account_is_empty(client, monkeypatch):
    now = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)
    monkeypatch.setattr(content_api, "_slot_now", lambda: now)
    with db.session_scope() as session:
        account = make_account(session, status="suspended", extra={"timezone": "UTC"})
        item_id = make_item(session, account, now=now).id

    data = _data(client.get(f"/api/v1/content/{item_id}/slots"))
    assert data["slots"] == []
    assert "suspended" in data["note"]


def test_content_slots_missing_item_is_not_found(client):
    response = client.get("/api/v1/content/nope/slots")
    assert response.status_code == 404
    assert _error(response)["code"] == "not_found"


def test_reschedule_accepts_a_slot_inside_the_window(client):
    account_id = _windowed_account()
    with db.session_scope() as session:
        account = session.get(Account, account_id)
        item = make_item(session, account, status=ContentStatus.SCHEDULED.value)
        item_id = item.id

    target = _next_window_slot()
    data = _data(
        client.post(
            f"/api/v1/content/{item_id}/reschedule",
            json={"scheduled_at": target.isoformat()},
        )
    )
    assert data["item"]["status"] == "scheduled"
    assert datetime.fromisoformat(data["scheduled_at"]) == target
    with db.session_scope() as session:
        assert session.get(ContentItem, item_id).scheduled_at == target
        actions = list(
            session.scalars(select(ReviewLog.action).where(ReviewLog.content_item_id == item_id))
        )
    assert actions == ["schedule"]


def test_reschedule_outside_window_is_rejected_with_a_suggestion(client):
    """前端能挑一个非法时刻，后端一定挡回去，并给出最近的合法槽位。"""
    account_id = _windowed_account()
    with db.session_scope() as session:
        account = session.get(Account, account_id)
        item = make_item(session, account, status=ContentStatus.SCHEDULED.value)
        item_id = item.id
        before = item.scheduled_at

    bad = _next_window_slot(hour=3)  # 凌晨 3 点，窗口是 09:00-11:00
    resp = client.post(
        f"/api/v1/content/{item_id}/reschedule", json={"scheduled_at": bad.isoformat()}
    )
    assert resp.status_code == 422
    error = _error(resp)
    assert error["code"] == "invalid_slot"
    assert error["detail"]["reason"] == "窗口"
    assert error["detail"]["account_windows"] == "09:00-11:00"
    suggested = datetime.fromisoformat(error["detail"]["suggested_slot"])
    assert suggested.astimezone(SHANGHAI).hour in (9, 10)
    with db.session_scope() as session:
        assert session.get(ContentItem, item_id).scheduled_at == before, "非法改期不得落库"


def test_reschedule_into_the_past_is_rejected(client):
    account_id = _windowed_account()
    with db.session_scope() as session:
        item = make_item(
            session, session.get(Account, account_id), status=ContentStatus.SCHEDULED.value
        )
        item_id = item.id
    resp = client.post(
        f"/api/v1/content/{item_id}/reschedule",
        json={"scheduled_at": (utcnow() - timedelta(hours=1)).isoformat()},
    )
    assert resp.status_code == 422
    assert _error(resp)["detail"]["reason"] == "已过去"


def test_reschedule_wrong_state_conflicts(client):
    item_id = _seed(client)["content_item_id"]  # draft
    resp = client.post(
        f"/api/v1/content/{item_id}/reschedule",
        json={"scheduled_at": _next_window_slot().isoformat()},
    )
    assert resp.status_code == 409
    assert _error(resp)["code"] == "illegal_transition"


def test_reschedule_bad_datetime_is_422(client):
    account_id = _windowed_account()
    with db.session_scope() as session:
        item_id = make_item(
            session, session.get(Account, account_id), status=ContentStatus.SCHEDULED.value
        ).id
    resp = client.post(f"/api/v1/content/{item_id}/reschedule", json={"scheduled_at": "明天早上"})
    assert resp.status_code == 422
    assert _error(resp)["code"] == "validation_error"


def _retrying_item(client) -> tuple[str, str]:
    account_id = _windowed_account("acc-retry")
    with db.session_scope() as session:
        account = session.get(Account, account_id)
        item = make_item(session, account, status=ContentStatus.RETRYING.value)
        session.add(
            PublishRecord(
                id=new_id("pub"),
                content_item_id=item.id,
                idem_key=new_id("idem"),
                phase="failed",
                attempts=2,
                last_error="RetryableError: 502",
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
        return item.id, account_id


def test_retry_now_clears_the_backoff(client):
    item_id, _ = _retrying_item(client)
    data = _data(client.post(f"/api/v1/content/{item_id}/retry_now"))
    assert data["mode"] == "retry_now"
    assert data["item"]["status"] == "retrying"

    from core.scheduler import backoff_for

    with db.session_scope() as session:
        record = session.scalars(
            select(PublishRecord).where(PublishRecord.content_item_id == item_id)
        ).one()
        assert utcnow() - record.updated_at >= backoff_for(record.attempts)
        actions = list(
            session.scalars(select(ReviewLog.action).where(ReviewLog.content_item_id == item_id))
        )
    assert actions == ["requeue"]


def test_retry_now_requeues_a_dead_letter_as_a_new_draft(client):
    """死信是 P0 冻结的终态，只能复投成新草稿，绝不能原地复活。"""
    account_id = _windowed_account("acc-dead")
    with db.session_scope() as session:
        account = session.get(Account, account_id)
        item = make_item(
            session, account, status=ContentStatus.DEAD_LETTER.value, title="炸掉的那条"
        )
        item.bundle_json = {
            **item.bundle_json,
            "platform_extra": {"confirm_publish": True, "watched_by": "auditor"},
        }
        item_id = item.id

    data = _data(client.post(f"/api/v1/content/{item_id}/retry_now"))
    assert data["mode"] == "requeued_as_draft"
    new_id_ = data["new_item_id"]
    assert new_id_ and new_id_ != item_id

    with db.session_scope() as session:
        old = session.get(ContentItem, item_id)
        clone = session.get(ContentItem, new_id_)
        assert old.status == ContentStatus.DEAD_LETTER, "原死信保持终态"
        assert clone.status == ContentStatus.DRAFT
        assert clone.bundle_json["id"] == new_id_
        assert clone.bundle_json["title"] == "炸掉的那条"
        # 闸门痕迹必须清掉：新稿子要重新过一遍人工确认
        assert clone.bundle_json["platform_extra"] == {}


def test_retry_now_rejects_other_states(client):
    item_id = _seed(client)["content_item_id"]  # draft
    resp = client.post(f"/api/v1/content/{item_id}/retry_now")
    assert resp.status_code == 409
    assert _error(resp)["code"] == "invalid_state"


# ---------------------------------------------------------------------- 账号


def test_accounts_list_and_detail(client):
    account_id = _seed(client)["account_id"]
    rows = _data(client.get("/api/v1/accounts"))
    assert [r["id"] for r in rows] == [account_id]
    row = rows[0]
    assert row["platform"] == "xhs"
    assert row["policy"]["daily_limit"] == 5
    assert row["policy"]["publish_windows"] == "全天"
    assert row["used_today"] == 0
    assert row["quota_left"] == 5
    assert row["supports_login"] is True

    detail = _data(client.get(f"/api/v1/accounts/{account_id}"))
    assert detail["pending_review"] == 1
    assert detail["extra"] == {"seeded": True}
    assert client.get("/api/v1/accounts/nope").status_code == 404


def test_accounts_used_today_counts_the_account_local_day(client):
    """``used_today`` / ``quota_left`` 按**账号本地日**切（P11.3）。

    端点内部读真实时钟，注入不了 ``now``，所以把两条发布记录**骑在账号本地零点两侧**：
    不管这条用例在一天里的哪一刻跑，只有本地零点之后那条算数。按 UTC 日切时这个断言
    必然落空——``Asia/Shanghai`` 的本地零点与 UTC 零点差 8 小时，两条记录要么都算（2）、
    要么都不算（0），**永远不会是 1**。这正是"本地 00:00–08:00 打开工作台，今日已发里
    混着昨晚那几条"的那个 bug。
    """
    with db.session_scope() as session:
        account = make_account(
            session, account_id="tz-quota", daily_limit=3, extra={"timezone": "Asia/Shanghai"}
        )
        midnight = local_day_start("Asia/Shanghai")  # 账号本地"今天"的零点（UTC 时刻）
        make_publish_record(session, account.id, at=midnight - timedelta(minutes=1))  # 本地昨天
        make_publish_record(session, account.id, at=midnight + timedelta(minutes=1))  # 本地今天

    row = _data(client.get("/api/v1/accounts/tz-quota"))
    assert row["policy"]["timezone"] == "Asia/Shanghai"
    assert row["used_today"] == 1
    assert row["quota_left"] == 2


def test_accounts_filters(client):
    _seed(client)
    assert len(_data(client.get("/api/v1/accounts", params={"platform": "xhs"}))) == 1
    assert _data(client.get("/api/v1/accounts", params={"platform": "douyin"})) == []
    assert len(_data(client.get("/api/v1/accounts", params={"status": "ok"}))) == 1


def test_login_qrcode_and_status(client):
    account_id = _seed(client)["account_id"]
    qrcode = _data(client.get(f"/api/v1/accounts/{account_id}/login/qrcode"))
    assert qrcode["placeholder"] is True  # FakePublisher 的占位图
    raw = base64.b64decode(qrcode["image_base64"], validate=True)
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"

    status = _data(client.get(f"/api/v1/accounts/{account_id}/login/status"))
    assert status["logged_in"] is True
    assert status["account_status"] == "ok"


def test_login_start_is_501_when_unsupported(client):
    account_id = _seed(client)["account_id"]  # FakePublisher 没有 start_login
    resp = client.post(f"/api/v1/accounts/{account_id}/login/start")
    assert resp.status_code == 501
    assert _error(resp)["code"] == "not_supported"


def test_login_code_queues_and_forwards(client):
    from core.sms_inbox import SMS_INBOX

    account_id = _seed(client)["account_id"]
    data = _data(client.post(f"/api/v1/accounts/{account_id}/login/code", json={"code": "135790"}))
    assert data["pending"] == 1
    assert data["forwarded"] is True
    assert SMS_INBOX.pop(account_id) == "135790"

    blank = client.post(f"/api/v1/accounts/{account_id}/login/code", json={"code": "   "})
    assert blank.status_code == 422
    assert _error(blank)["code"] == "invalid_code"


# ---------------------------------------------------------------------- 选题


def _topic(session, *, topic_id: str = "top-1", source: str = "newsnow") -> Topic:
    topic = Topic(
        id=topic_id, source=source, title=f"选题 {topic_id}", score=1.0, raw={"info": "热"}
    )
    session.add(topic)
    session.flush()
    return topic


def test_topics_list_used_and_source_filters(client):
    account_id = _seed(client)["account_id"]
    with db.session_scope() as session:
        _topic(session, topic_id="top-used")
        _topic(session, topic_id="top-free", source="trendradar")
        account = session.get(Account, account_id)
        item = make_item(session, account, status=ContentStatus.DRAFT.value)
        item.topic_id = "top-used"

    assert _data(client.get("/api/v1/topics"))["total"] == 2
    used = _data(client.get("/api/v1/topics", params={"used": True}))
    assert [t["id"] for t in used["items"]] == ["top-used"]
    assert used["items"][0]["used"] is True
    unused = _data(client.get("/api/v1/topics", params={"used": False}))
    assert [t["id"] for t in unused["items"]] == ["top-free"]
    assert _data(client.get("/api/v1/topics", params={"source": "trendradar"}))["total"] == 1


def test_topic_dismiss_and_restore(client):
    with db.session_scope() as session:
        _topic(session)

    data = _data(
        client.post("/api/v1/topics/top-1/dismiss", json={"actor": "me", "reason": "旧闻"})
    )
    assert data["dismissed"] is True
    assert data["dismissed_by"] == "me"
    assert data["raw"] == {"info": "热"}, "raw 里的采集字段不受影响"

    restored = _data(client.post("/api/v1/topics/top-1/dismiss", json={"dismissed": False}))
    assert restored["dismissed"] is False
    assert client.post("/api/v1/topics/nope/dismiss", json={}).status_code == 404


# ---------------------------------------------------------------------- 任务


def test_render_jobs_and_publish_records_and_dead_letters(client):
    account_id = _windowed_account("acc-jobs")
    with db.session_scope() as session:
        account = session.get(Account, account_id)
        item = make_item(session, account, status=ContentStatus.PUBLISHED.value)
        session.add(
            RenderJob(
                id=new_id("rjb"),
                content_item_id=item.id,
                provider="mpt",
                task_id="t-1",
                state="running",
                progress=42,
                meta={"stage": "materials"},
            )
        )
        session.add(
            PublishRecord(
                id=new_id("pub"),
                content_item_id=item.id,
                idem_key=new_id("idem"),
                phase="done",
                platform_post_id="post-1",
                url="https://example.invalid/p/1",
                attempts=1,
            )
        )
        dead = make_item(session, account, status=ContentStatus.DEAD_LETTER.value, title="死了")
        session.add(
            ReviewLog(
                id=new_id("rvl"),
                content_item_id=dead.id,
                actor="system",
                action="dead_letter",
                reason="重试 3 次仍失败",
                at=utcnow(),
            )
        )

    jobs = _data(client.get("/api/v1/jobs/render"))
    assert jobs["items"][0]["progress"] == 42
    assert jobs["items"][0]["state"] == "running"
    assert _data(client.get("/api/v1/jobs/render", params={"state": "done"}))["total"] == 0

    records = _data(client.get("/api/v1/jobs/publish_records", params={"phase": "done"}))
    assert records["items"][0]["platform_post_id"] == "post-1"
    assert records["items"][0]["account_id"] == account_id
    assert (
        _data(client.get("/api/v1/jobs/publish_records", params={"account_id": "nope"}))["total"]
        == 0
    )

    letters = _data(client.get("/api/v1/jobs/dead_letters"))
    assert letters["total"] == 1
    assert letters["items"][0]["reason"] == "重试 3 次仍失败"


def test_content_row_carries_publish_result(client):
    account_id = _windowed_account("acc-pub")
    with db.session_scope() as session:
        account = session.get(Account, account_id)
        item = make_item(session, account, status=ContentStatus.PUBLISHED.value)
        session.add(
            PublishRecord(
                id=new_id("pub"),
                content_item_id=item.id,
                idem_key=new_id("idem"),
                phase="done",
                platform_post_id="post-9",
                url="https://example.invalid/p/9",
                attempts=1,
            )
        )
    row = _data(client.get("/api/v1/content", params={"account_id": account_id}))["items"][0]
    assert row["platform_post_id"] == "post-9"
    assert row["url"] == "https://example.invalid/p/9"
    assert row["published_at"] is not None
    assert row["publish_phase"] == "done"


# ------------------------------------------------------------------ 统计成本


def test_stats_has_a_daily_series(client):
    _seed(client)
    data = _data(client.get("/api/v1/stats", params={"days": 7}))
    assert data["window_days"] == 7
    assert len(data["daily"]) == 7
    assert data["daily"][-1]["day"] == datetime.now(UTC).date().isoformat()
    assert data["content_counts"] == {"draft": 1}
    assert data["budget"]["tokens"]["limit"] == 1000.0


def test_costs_aggregates_by_day_and_account(client):
    account_id = _seed(client)["account_id"]
    with db.session_scope() as session:
        session.add(
            CostLedger(
                id=new_id("cost"),
                day=datetime.now(UTC).date().isoformat(),
                kind="tokens",
                amount=120.0,
                meta={"account_id": account_id},
            )
        )
        session.add(
            CostLedger(
                id=new_id("cost"),
                day=datetime.now(UTC).date().isoformat(),
                kind="tokens",
                amount=30.0,
                meta={},
            )
        )

    data = _data(client.get("/api/v1/costs", params={"days": 3}))
    assert len(data["by_day"]) == 3
    assert data["by_day"][-1]["cost"]["tokens"] == 150.0
    assert data["totals"]["tokens"] == 150.0
    assert data["by_account"][0]["cost"] == {"tokens": 120.0}
    assert data["unattributed"] == {"tokens": 30.0}
    assert data["budget"]["tokens"]["remaining"] == 850.0


# ---------------------------------------------------------------------- 复盘


def test_insights_reads_the_markdown_file(client, monkeypatch, tmp_path):
    monkeypatch.setattr(prompts, "ACCOUNTS_DIR", tmp_path)
    account_id = _seed(client)["account_id"]
    prompts.append_insight(
        account_id,
        "## 2026-08-15 · 近 7 天复盘（demo）\n\n**通勤话题最稳**\n\n- 置信度：`medium`",
    )
    prompts.append_insight(account_id, "## 2026-08-16 · 近 7 天复盘（demo）\n\n**收纳其次**")

    data = _data(client.get("/api/v1/insights", params={"account_id": account_id}))
    assert len(data) == 1
    entries = data[0]["entries"]
    assert data[0]["exists"] is True
    assert [e["date"] for e in entries] == ["2026-08-16", "2026-08-15"], "新的在最上面"
    assert entries[0]["headline"] == "收纳其次"
    assert "近 7 天复盘" in entries[0]["title"]


def test_insights_missing_file_is_not_an_error(client, monkeypatch, tmp_path):
    monkeypatch.setattr(prompts, "ACCOUNTS_DIR", tmp_path)
    _seed(client)
    data = _data(client.get("/api/v1/insights"))
    assert data[0]["exists"] is False
    assert data[0]["entries"] == []


def test_insights_run_skips_without_credentials(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    reload_settings()
    _seed(client)
    data = _data(client.post("/api/v1/insights/run", json={"force": True}))
    assert data["tick"] == "insights"
    assert data["stats"]["skipped_no_key"] == 1
    assert "未配置 LLM 凭据" in data["message"]


# ---------------------------------------------------------------------- 系统


def test_system_info(client):
    data = _data(client.get("/api/v1/system/info"))
    assert data["env"] == "test"
    assert data["scheduler_enabled"] is False
    assert data["use_fake_publishers"] is True
    assert data["auth_required"] is False
    assert set(data["publishers"]) == {"wechat_mp", "xhs", "douyin"}
    assert "scheduled_publish" in data["ticks"]
    assert "test.db" in data["database"]
    # 前端筛选下拉框的取值域，别让两边各抄一份枚举
    assert data["platforms"] == ["wechat_mp", "xhs", "douyin"]
    assert data["review_queue_statuses"] == ["draft", "reviewing", "rejected"]
    assert {"draft", "scheduled", "published", "dead_letter"} <= set(data["content_statuses"])


def test_system_ticks_listing_matches_the_registry(client):
    from core.scheduler import TICKS

    data = _data(client.get("/api/v1/system/ticks"))
    assert {t["name"] for t in data["ticks"]} == set(TICKS)
    accepts = {t["name"]: t["accepts"] for t in data["ticks"]}
    assert accepts["metrics"] == ["respect_windows"]
    assert accepts["generate"] == ["account_id", "platform"]


def test_system_tick_run_and_errors(client):
    from publishers.registry import use_fake_publishers

    use_fake_publishers()
    data = _data(client.post("/api/v1/system/ticks/scheduled_publish"))
    assert data["tick"] == "scheduled_publish"
    assert "scanned" in data["stats"]

    unknown = client.post("/api/v1/system/ticks/nope")
    assert unknown.status_code == 404
    assert _error(unknown)["code"] == "not_found"

    bad_param = client.post("/api/v1/system/ticks/metrics", params={"account_id": "a"})
    assert bad_param.status_code == 422
    assert _error(bad_param)["code"] == "invalid_tick_param"


def test_system_preflight(client, monkeypatch):
    """门禁复用 scripts/preflight 的检查函数。docker 探测在测试里换成常量，免得等 15 秒。"""
    import scripts.preflight as preflight_mod

    monkeypatch.setattr(
        preflight_mod,
        "check_docker",
        lambda: [preflight_mod.Check("docker", "SKIP", "测试里跳过")],
    )
    data = _data(client.get("/api/v1/system/preflight", params={"offline": True}))
    assert data["offline"] is True
    names = {c["name"] for c in data["checks"]}
    assert {"调度参数", "环境变量命名", "数据库"} <= names
    assert set(data["counts"]) <= {"OK", "WARN", "FAIL", "SKIP"}
    assert all(c["status"] in ("OK", "WARN", "FAIL", "SKIP") for c in data["checks"])


# -------------------------------------------------------------------- 看板


def test_dashboard_counters_and_events(client):
    item_id = _seed(client)["content_item_id"]
    data = _data(client.get("/api/v1/dashboard"))
    assert data["counters"]["pending_review"] == 1
    assert data["counters"]["published_today"] == 0
    assert data["counters"]["rendering"] == 0
    assert data["budget"]["tokens"]["limit"] == 1000.0
    assert [p["platform"] for p in data["platforms"]] == ["xhs"]
    assert data["platforms"][0]["pending_review"] == 1
    assert data["events"] == []

    client.post(f"/api/v1/review/{item_id}/approve", json={"actor": "auditor"})
    after = _data(client.get("/api/v1/dashboard"))
    assert after["counters"]["pending_review"] == 0
    assert after["counters"]["scheduled"] == 1
    actions = [e["action"] for e in after["events"]]
    assert "approve" in actions and "schedule" in actions
    assert after["events"][0]["title"].startswith("3 个让通勤包")


def test_dashboard_flags_accounts_that_need_relogin(client):
    account_id = _seed(client)["account_id"]
    with db.session_scope() as session:
        session.get(Account, account_id).status = "needs_relogin"
    data = _data(client.get("/api/v1/dashboard"))
    assert data["counters"]["accounts_needing_relogin"] == 1
    assert data["attention"][0]["account_id"] == account_id
    assert data["platforms"][0]["needs_relogin"] == 1


# ------------------------------------------------------------------ OpenAPI


def test_openapi_covers_every_api_route(client):
    schema = client.get("/openapi.json").json()
    api_paths = [p for p in schema["paths"] if p.startswith("/api/v1")]
    assert len(api_paths) == 41, sorted(api_paths)
    detail = schema["paths"]["/api/v1/review/{item_id}"]["get"]
    ref = detail["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    assert "Envelope" in ref
