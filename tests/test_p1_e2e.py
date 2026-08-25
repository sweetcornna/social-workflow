"""P1 端到端 dry-run：fixture 选题 → mock LLM → mock 渲染 → 审核 → ContentItem 状态。

以及新增的 API 端点（/dev/run_wechat_pipeline、正文预览、封面）。
全程不碰网络、不调真 LLM、不需要 Node / Playwright。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from core.budget import BudgetGuard, CostKind
from core.dev_flow import DevFlowError, collect_topics, make_scripted_llm, run_wechat_pipeline
from core.models import Account, ContentItem, Topic
from core.state_machine import ContentStatus
from generation.pipeline import GenerationOptions
from review.pipeline import MACHINE_REVIEW_ACTION
from sourcing import douyin_hot_hub, newsnow
from sourcing.base import persist_topics
from tests.conftest import make_account
from tests.p1_helpers import RecordingRunner, fake_screenshotter, load_fixture


@pytest.fixture
def wechat_account(session) -> Account:
    return make_account(session, account_id="wechat-demo-01", platform="wechat_mp")


@pytest.fixture
def offline_options(tmp_path) -> GenerationOptions:
    """渲染与截图都注入替身，测试不依赖 Node / Playwright。"""
    written: list[tuple[Path, str]] = []
    return GenerationOptions(
        media_root=tmp_path / "media",
        screenshotter=fake_screenshotter(written),
        render_runner=RecordingRunner(
            stdout='<section style="line-height:1.75"><p>正文</p></section>'
        ),
    )


def seed_topics(session) -> None:
    """用 fixture 里的真实榜单数据灌选题池。"""
    weibo = newsnow.parse_source_response(load_fixture("newsnow_weibo.json"), source_id="weibo")
    douyin = douyin_hot_hub.parse_hot_search(
        load_fixture("douyin_hot_search.json"), day=date(2026, 8, 15)
    )
    persist_topics(session, weibo + douyin)


# ------------------------------------------------------------------ 端到端


def test_e2e_dry_run_produces_reviewable_item(session, wechat_account, offline_options) -> None:
    """fixture 选题 → mock LLM → 渲染 mock → 审核 → ContentItem 状态正确。"""
    seed_topics(session)

    result = run_wechat_pipeline(
        session,
        wechat_account,
        llm=make_scripted_llm(),
        skip_sourcing=True,
        options=offline_options,
    )

    assert result.content_item_id is not None
    assert result.candidates > 0
    assert result.selected_topic

    item = session.get(ContentItem, result.content_item_id)
    assert item is not None
    # 机器审核跑完仍回 draft，等人工卡点——不能自己走到 approved
    assert item.status == ContentStatus.DRAFT.value
    assert item.review_notes

    bundle = item.bundle_json
    assert bundle["platform"] == "wechat_mp"
    assert bundle["body_html"], "渲染 mock 应该产出 body_html"
    assert bundle["platform_extra"]["title"]
    assert bundle["platform_extra"]["digest"]
    # 选题决策留痕
    assert bundle["platform_extra"]["selection"]["topic_title"] == result.selected_topic
    # 封面进了 media
    assert any(m["cover"] for m in bundle["media"])

    # 审计日志：机器审核这一步必须留痕
    actions = [log.action for log in item.review_logs]
    assert MACHINE_REVIEW_ACTION in actions

    # 选题被关联上
    assert item.topic_id is not None
    assert session.get(Topic, item.topic_id) is not None


def test_e2e_charges_token_budget(session, wechat_account, offline_options) -> None:
    # conftest 把日预算压到 1000 用于测超限分支；跑完整条链需要放宽
    guard = BudgetGuard(session, token_budget=100_000)
    seed_topics(session)
    run_wechat_pipeline(
        session,
        wechat_account,
        llm=make_scripted_llm(guard),
        skip_sourcing=True,
        options=offline_options,
    )
    assert guard.used(CostKind.TOKENS) > 0


def test_e2e_manual_topic_skips_selector(session, wechat_account, offline_options) -> None:
    llm = make_scripted_llm()
    result = run_wechat_pipeline(
        session,
        wechat_account,
        llm=llm,
        topic_title="手工指定的选题标题",
        skip_sourcing=True,
        options=offline_options,
    )
    assert result.selected_topic == "手工指定的选题标题"
    assert "sourcing.select" not in [c["purpose"] for c in llm.calls]
    item = session.get(ContentItem, result.content_item_id)
    assert item.topic_id is None


def test_e2e_empty_topic_pool_gives_actionable_error(session, wechat_account) -> None:
    with pytest.raises(DevFlowError, match="NEWSNOW_BASE_URL"):
        run_wechat_pipeline(session, wechat_account, llm=make_scripted_llm(), skip_sourcing=True)


def test_e2e_rejects_non_wechat_account(session) -> None:
    account = make_account(session, account_id="xhs-1", platform="xhs")
    with pytest.raises(DevFlowError, match="只跑 wechat_mp"):
        run_wechat_pipeline(session, account, llm=make_scripted_llm())


def test_e2e_budget_exhausted_degrades_to_no_draft(session, wechat_account) -> None:
    """预算耗尽时"只出选题不出稿"，不能产出半成品 ContentItem。"""
    seed_topics(session)
    guard = BudgetGuard(session, token_budget=150)  # 只够一两次调用
    with pytest.raises(DevFlowError, match="预算耗尽"):
        run_wechat_pipeline(
            session,
            wechat_account,
            llm=make_scripted_llm(guard),
            skip_sourcing=True,
        )


def test_collect_topics_tolerates_unconfigured_sources(session, monkeypatch) -> None:
    """newsnow 没配、douyin 拉不到，都不该让整条链挂掉。

    douyin 的默认 base_url 指向真实的 raw.githubusercontent.com，
    这里必须打桩掉——单测不允许出网。
    """
    monkeypatch.setattr(
        douyin_hot_hub,
        "fetch",
        lambda *a, **k: (_ for _ in ()).throw(
            douyin_hot_hub.DouyinHotHubError("归档不可用（测试打桩）")
        ),
    )
    warnings: list[str] = []
    added = collect_topics(session, warnings=warnings)
    assert added == 0
    assert any("newsnow" in w for w in warnings)
    assert any("douyin_hot_hub" in w for w in warnings)


# ------------------------------------------------------------------ API


def test_dev_endpoint_runs_pipeline_without_api_key(client, monkeypatch) -> None:
    """没有 ANTHROPIC_API_KEY 时自动降级到 ScriptedLLM，链路照样跑通。"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    # conftest 把日预算压到 1000（用于测超限分支），整条生成链不够用
    monkeypatch.setenv("DAILY_TOKEN_BUDGET", "100000")
    from core.config import reload_settings

    reload_settings()

    # 先造账号与选题池
    from core import db

    with db.session_scope() as session:
        make_account(session, account_id="wechat-demo-01", platform="wechat_mp")
        seed_topics(session)

    response = client.post(
        "/dev/run_wechat_pipeline",
        params={
            "account_id": "wechat-demo-01",
            "skip_sourcing": True,
            "make_cover": False,
            "render_html": False,
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    assert payload["llm"] == "scripted"
    assert payload["content_item_id"]
    assert payload["review_url"].startswith("/review/")
    assert "lexicon" in payload["review"]["stages_run"]

    detail = client.get(payload["review_url"])
    assert detail.status_code == 200
    assert "正文 Markdown" in detail.text


def test_dev_endpoint_returns_409_when_no_topics(client) -> None:
    from core import db

    with db.session_scope() as session:
        make_account(session, account_id="wechat-demo-01", platform="wechat_mp")

    response = client.post(
        "/dev/run_wechat_pipeline",
        params={"account_id": "wechat-demo-01", "skip_sourcing": True},
    )
    # 没配数据源是预期内的情况，不该是 500
    assert response.status_code == 409
    assert response.json()["ok"] is False


def test_dev_endpoint_404_for_unknown_account(client) -> None:
    response = client.post("/dev/run_wechat_pipeline", params={"account_id": "nope"})
    assert response.status_code == 404


def test_preview_endpoint_serves_body_html(client) -> None:
    from core import db

    with db.session_scope() as session:
        account = make_account(session, account_id="wechat-demo-01", platform="wechat_mp")
        item = ContentItem(
            id="itm_preview",
            account_id=account.id,
            status=ContentStatus.DRAFT.value,
            bundle_json={
                "id": "itm_preview",
                "account_id": account.id,
                "platform": "wechat_mp",
                "title": "预览测试",
                "body_markdown": "正文",
                "body_html": '<section style="color:#333">渲染后的正文</section>',
                "media": [],
                "tags": [],
                "platform_extra": {"title": "预览测试", "digest": "摘要", "author": "作者"},
            },
        )
        session.add(item)

    response = client.get("/review/itm_preview/preview")
    assert response.status_code == 200
    assert "渲染后的正文" in response.text

    detail = client.get("/review/itm_preview")
    # 详情页用 sandbox iframe 隔离不可信的 LLM 产物，不能直接内联进页面
    assert "<iframe" in detail.text
    assert "sandbox" in detail.text
    assert "/review/itm_preview/preview" in detail.text


def test_preview_endpoint_404_without_html(client) -> None:
    from core import db

    with db.session_scope() as session:
        account = make_account(session, account_id="wechat-demo-01", platform="wechat_mp")
        session.add(
            ContentItem(
                id="itm_nohtml",
                account_id=account.id,
                status=ContentStatus.DRAFT.value,
                bundle_json={
                    "id": "itm_nohtml",
                    "account_id": account.id,
                    "platform": "wechat_mp",
                    "title": "无 HTML",
                    "body_markdown": "正文",
                    "media": [],
                    "tags": [],
                    "platform_extra": {},
                },
            )
        )

    assert client.get("/review/itm_nohtml/preview").status_code == 404
    detail = client.get("/review/itm_nohtml")
    assert "尚未渲染" in detail.text


def test_cover_endpoint_serves_local_file(client, tmp_path) -> None:
    from core import db

    cover = Path("data/media/test-cover.png")
    cover.parent.mkdir(parents=True, exist_ok=True)
    cover.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    try:
        with db.session_scope() as session:
            account = make_account(session, account_id="wechat-demo-01", platform="wechat_mp")
            session.add(
                ContentItem(
                    id="itm_cover",
                    account_id=account.id,
                    status=ContentStatus.DRAFT.value,
                    bundle_json={
                        "id": "itm_cover",
                        "account_id": account.id,
                        "platform": "wechat_mp",
                        "title": "封面测试",
                        "body_markdown": "正文",
                        "media": [{"path": str(cover), "kind": "image", "cover": True}],
                        "tags": [],
                        "platform_extra": {},
                    },
                )
            )
        response = client.get("/review/itm_cover/cover")
        assert response.status_code == 200
        assert response.content.startswith(b"\x89PNG")
    finally:
        cover.unlink(missing_ok=True)


def test_cover_endpoint_blocks_path_traversal(client) -> None:
    """bundle_json 里的路径来自生成链，但不该能读到仓库外的文件。"""
    from core import db

    with db.session_scope() as session:
        account = make_account(session, account_id="wechat-demo-01", platform="wechat_mp")
        session.add(
            ContentItem(
                id="itm_evil",
                account_id=account.id,
                status=ContentStatus.DRAFT.value,
                bundle_json={
                    "id": "itm_evil",
                    "account_id": account.id,
                    "platform": "wechat_mp",
                    "title": "越界",
                    "body_markdown": "正文",
                    "media": [{"path": "../../../../etc/passwd", "kind": "image", "cover": True}],
                    "tags": [],
                    "platform_extra": {},
                },
            )
        )
    assert client.get("/review/itm_evil/cover").status_code == 404


def test_approve_marks_confirm_publish_for_wechat(client) -> None:
    """双确认闸门第三道：人工逐条确认，缺它公众号只到草稿箱。"""
    from core import db

    with db.session_scope() as session:
        account = make_account(session, account_id="wechat-demo-01", platform="wechat_mp")
        session.add(
            ContentItem(
                id="itm_confirm",
                account_id=account.id,
                status=ContentStatus.DRAFT.value,
                bundle_json={
                    "id": "itm_confirm",
                    "account_id": account.id,
                    "platform": "wechat_mp",
                    "title": "待批准",
                    "body_markdown": "正文",
                    "media": [],
                    "tags": [],
                    "platform_extra": {"title": "待批准", "digest": "摘要", "author": "作者"},
                },
            )
        )

    response = client.post(
        "/review/itm_confirm/approve", data={"actor": "reviewer-1"}, follow_redirects=False
    )
    assert response.status_code in (200, 303)

    with db.session_scope() as session:
        item = session.get(ContentItem, "itm_confirm")
        # P4 起批准即排期（core/scheduling.py），批准的终点是 scheduled 而不是 approved；
        # 这条用例盯的是"人工逐条确认"这道闸门，排期只是它后面紧跟的一步
        assert item.status == ContentStatus.SCHEDULED.value
        assert item.scheduled_at is not None
        extra = item.bundle_json["platform_extra"]
        assert extra["confirm_publish"] is True
        assert extra["confirm_publish_by"] == "reviewer-1"
        assert extra["confirm_publish_at"]


def test_approve_does_not_mark_confirm_for_other_platforms(client) -> None:
    from core import db

    with db.session_scope() as session:
        account = make_account(session, account_id="xhs-1", platform="xhs")
        session.add(
            ContentItem(
                id="itm_xhs",
                account_id=account.id,
                status=ContentStatus.DRAFT.value,
                bundle_json={
                    "id": "itm_xhs",
                    "account_id": account.id,
                    "platform": "xhs",
                    "title": "小红书笔记",
                    "body_markdown": "正文",
                    "media": [],
                    "tags": [],
                    "platform_extra": {},
                },
            )
        )

    client.post("/review/itm_xhs/approve", data={"actor": "reviewer-1"}, follow_redirects=False)
    with db.session_scope() as session:
        item = session.get(ContentItem, "itm_xhs")
        assert "confirm_publish" not in item.bundle_json["platform_extra"]
