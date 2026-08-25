"""小红书端到端 dry-run：fixture 选题 → mock LLM → 卡片 mock → 审核 → 进审核队列。

以及 ``POST /dev/run_xhs_pipeline`` 与审核 UI 的多图展示。
全程不碰网络、不调真 LLM、不需要 Playwright。
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest

from core.budget import BudgetGuard, CostKind
from core.dev_flow import DevFlowError, make_xhs_scripted_llm, run_xhs_pipeline
from core.models import Account, ContentItem, Topic
from core.state_machine import ContentStatus
from generation.pipeline import XhsGenerationOptions
from generation.xhs_cards import DEFAULT_THEME
from review.inspect import MAX_XHS_TITLE_CHARS
from review.pipeline import MACHINE_REVIEW_ACTION
from sourcing import douyin_hot_hub, newsnow
from sourcing.base import persist_topics
from tests.conftest import make_account
from tests.p1_helpers import load_fixture
from tests.p2_helpers import card_screenshotter, xhs_llm


@pytest.fixture
def xhs_account(session) -> Account:
    return make_account(session, account_id="xhs-demo-01", platform="xhs")


@pytest.fixture
def offline_options(tmp_path) -> XhsGenerationOptions:
    """卡片截图注入替身，测试不依赖 Playwright。"""
    written: list[tuple[Path, str]] = []
    return XhsGenerationOptions(
        media_root=tmp_path / "media",
        screenshotter=card_screenshotter(written),
    )


def seed_topics(session) -> None:
    weibo = newsnow.parse_source_response(load_fixture("newsnow_weibo.json"), source_id="weibo")
    douyin = douyin_hot_hub.parse_hot_search(
        load_fixture("douyin_hot_search.json"), day=date(2026, 8, 15)
    )
    persist_topics(session, weibo + douyin)


# ------------------------------------------------------------------ 端到端


def test_e2e_dry_run_produces_reviewable_xhs_item(session, xhs_account, offline_options) -> None:
    seed_topics(session)
    llm = make_xhs_scripted_llm()

    result = run_xhs_pipeline(
        session,
        xhs_account,
        llm=llm,
        skip_sourcing=True,
        options=offline_options,
    )

    assert result.content_item_id is not None
    assert result.candidates > 0
    assert result.selected_topic
    assert result.cards == 4  # 封面 + 3 张内页
    assert result.theme == DEFAULT_THEME

    item = session.get(ContentItem, result.content_item_id)
    assert item is not None
    # 机器审核跑完仍回 draft，等人工卡点——绝不能自己走到 approved
    # （小红书 2026-03-10 公告：完全 AI 驱动的账号直接封禁）
    assert item.status == ContentStatus.DRAFT.value
    assert item.review_notes

    bundle = item.bundle_json
    assert bundle["platform"] == "xhs"
    assert bundle["body_html"] is None
    assert len(bundle["title"]) <= MAX_XHS_TITLE_CHARS
    assert bundle["tags"]
    assert bundle["platform_extra"]["tags"] == bundle["tags"]
    assert bundle["platform_extra"]["is_original"] is True
    assert bundle["platform_extra"]["schedule_at"] is None
    # 卡片进了 media，第一张是封面，文件真的落了盘
    assert len(bundle["media"]) == 4
    assert bundle["media"][0]["cover"] is True
    assert all(Path(m["path"]).is_file() for m in bundle["media"])
    # 选题决策留痕
    assert bundle["platform_extra"]["selection"]["topic_title"] == result.selected_topic
    selected_angle = bundle["platform_extra"]["selection"]["angle"]
    assert bundle["platform_extra"]["suggested_angle"] == selected_angle
    generation_calls = [call for call in llm.calls if call["purpose"].startswith("xhs.")]
    assert generation_calls and all(selected_angle in call["prompt"] for call in generation_calls)

    # 审计日志：机器审核这一步必须留痕
    assert MACHINE_REVIEW_ACTION in [log.action for log in item.review_logs]

    # 选题被关联上
    assert item.topic_id is not None
    assert session.get(Topic, item.topic_id) is not None


def test_e2e_review_finds_no_blocking_issue(session, xhs_account, offline_options) -> None:
    """预置内容是干净的：三级审核 + inspect 都不该报 block。"""
    result = run_xhs_pipeline(
        session,
        xhs_account,
        llm=make_xhs_scripted_llm(),
        topic_title="租房收纳",
        skip_sourcing=True,
        options=offline_options,
    )
    item = session.get(ContentItem, result.content_item_id)
    assert result.review_blocking == 0, item.review_notes
    assert result.review_passed is True
    assert "inspect" in result.stages_run


def test_e2e_without_cards_is_blocked_by_inspect(session, xhs_account, tmp_path) -> None:
    """没图的图文笔记必须被挡在人工卡点之前，而不是等发布时才失败。"""
    result = run_xhs_pipeline(
        session,
        xhs_account,
        llm=make_xhs_scripted_llm(),
        topic_title="租房收纳",
        skip_sourcing=True,
        options=XhsGenerationOptions(media_root=tmp_path, make_cards=False),
    )
    assert result.review_passed is False
    item = session.get(ContentItem, result.content_item_id)
    assert "xhs.image.missing" in item.review_notes


def test_e2e_charges_token_budget(session, xhs_account, offline_options) -> None:
    guard = BudgetGuard(session, token_budget=100_000)
    seed_topics(session)
    run_xhs_pipeline(
        session,
        xhs_account,
        llm=make_xhs_scripted_llm(guard),
        skip_sourcing=True,
        options=offline_options,
    )
    assert guard.used(CostKind.TOKENS) > 0


def test_e2e_manual_topic_skips_selector(session, xhs_account, offline_options) -> None:
    llm = make_xhs_scripted_llm()
    result = run_xhs_pipeline(
        session,
        xhs_account,
        llm=llm,
        topic_title="手工指定的小红书选题",
        skip_sourcing=True,
        options=offline_options,
    )
    assert result.selected_topic == "手工指定的小红书选题"
    assert "sourcing.select" not in [c["purpose"] for c in llm.calls]
    assert session.get(ContentItem, result.content_item_id).topic_id is None


def test_e2e_rejects_non_xhs_account(session) -> None:
    account = make_account(session, account_id="wechat-1", platform="wechat_mp")
    with pytest.raises(DevFlowError, match="只跑 xhs"):
        run_xhs_pipeline(session, account, llm=make_xhs_scripted_llm())


def test_e2e_empty_topic_pool_gives_actionable_error(session, xhs_account) -> None:
    with pytest.raises(DevFlowError, match="NEWSNOW_BASE_URL"):
        run_xhs_pipeline(session, xhs_account, llm=make_xhs_scripted_llm(), skip_sourcing=True)


def test_e2e_budget_exhausted_degrades_to_no_draft(session, xhs_account) -> None:
    seed_topics(session)
    guard = BudgetGuard(session, token_budget=150)  # 只够一两次调用
    with pytest.raises(DevFlowError, match="预算耗尽"):
        run_xhs_pipeline(
            session,
            xhs_account,
            llm=make_xhs_scripted_llm(guard),
            skip_sourcing=True,
        )


def test_quality_failure_is_diagnostic_and_never_persists_item(
    session, xhs_account, tmp_path
) -> None:
    before = session.query(ContentItem).count()
    with pytest.raises(DevFlowError, match=r"质量终检未通过.*未入库"):
        run_xhs_pipeline(
            session,
            xhs_account,
            llm=xhs_llm(
                verdict="revise",
                final_verdict="reject",
                final_blocking_issues=["物理构图仍不安全"],
            ),
            topic_title="租房收纳",
            skip_sourcing=True,
            options=XhsGenerationOptions(media_root=tmp_path, make_cards=False),
        )
    assert session.query(ContentItem).count() == before


# ------------------------------------------------------------------ API


def _prepare(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    # conftest 把日预算压到 1000（用于测超限分支），整条生成链不够用
    monkeypatch.setenv("DAILY_TOKEN_BUDGET", "100000")
    from core.config import reload_settings

    reload_settings()


def test_dev_endpoint_runs_xhs_pipeline_without_api_key(client, monkeypatch) -> None:
    _prepare(monkeypatch)
    from core import db

    with db.session_scope() as session:
        make_account(session, account_id="xhs-demo-01", platform="xhs")

    response = client.post(
        "/dev/run_xhs_pipeline",
        params={
            "account_id": "xhs-demo-01",
            "topic": "30㎡ 租房收纳",
            "skip_sourcing": True,
            "make_cards": False,
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    assert payload["llm"] == "scripted"
    assert payload["theme"] == DEFAULT_THEME
    assert payload["cards"] == 0
    assert payload["review_url"].startswith("/review/")
    assert "lexicon" in payload["review"]["stages_run"]

    detail = client.get(payload["review_url"])
    assert detail.status_code == 200
    assert "图文卡片" in detail.text
    # 小红书没有 body_html，不该再劝人去装 Node
    assert "公众号预览" not in detail.text


def test_dev_endpoint_default_theme_matches_card_module() -> None:
    """端点默认值是字面量（避免把 anthropic SDK 拉进启动路径），盯着它别漂移。"""
    import inspect as py_inspect

    from core.main import create_app

    app = create_app()
    route = next(r for r in app.routes if getattr(r, "path", "") == "/dev/run_xhs_pipeline")
    default = py_inspect.signature(route.endpoint).parameters["theme"].default
    assert default == DEFAULT_THEME


def test_dev_endpoint_rejects_unknown_theme(client, monkeypatch) -> None:
    _prepare(monkeypatch)
    from core import db

    with db.session_scope() as session:
        make_account(session, account_id="xhs-demo-01", platform="xhs")

    response = client.post(
        "/dev/run_xhs_pipeline",
        params={"account_id": "xhs-demo-01", "topic": "x", "theme": "nope"},
    )
    assert response.status_code == 422
    assert "editorial" in response.json()["detail"]


def test_dev_endpoint_returns_409_when_no_topics(client, monkeypatch) -> None:
    _prepare(monkeypatch)
    from core import db

    with db.session_scope() as session:
        make_account(session, account_id="xhs-demo-01", platform="xhs")

    response = client.post(
        "/dev/run_xhs_pipeline",
        params={"account_id": "xhs-demo-01", "skip_sourcing": True},
    )
    assert response.status_code == 409
    assert response.json()["ok"] is False


def test_dev_endpoint_404_for_unknown_account(client) -> None:
    assert client.post("/dev/run_xhs_pipeline", params={"account_id": "nope"}).status_code == 404


def test_dev_endpoint_rejects_wechat_account(client, monkeypatch) -> None:
    _prepare(monkeypatch)
    from core import db

    with db.session_scope() as session:
        make_account(session, account_id="wechat-demo-01", platform="wechat_mp")

    response = client.post(
        "/dev/run_xhs_pipeline",
        params={"account_id": "wechat-demo-01", "topic": "x", "skip_sourcing": True},
    )
    assert response.status_code == 409
    assert "只跑 xhs" in response.json()["error"]


# ------------------------------------------------------------------ 审核 UI


@pytest.fixture
def item_with_cards() -> Iterator[str]:
    """一条带 3 张真实 PNG 的 xhs 内容。

    文件必须落在 **cwd 之内**：``/review/{id}/media/{i}`` 有目录穿越防护，
    只允许读工作目录里的文件，所以这里不能用 pytest 的 ``tmp_path``。
    """
    from core import db
    from tests.p2_helpers import png_bytes

    root = Path.cwd() / "data" / "media" / f"test_xhs_e2e_{uuid4().hex[:8]}"
    root.mkdir(parents=True, exist_ok=True)
    media = []
    for index in range(3):
        path = root / f"card-{index:02d}.png"
        path.write_bytes(png_bytes(1242, 1656))
        media.append({"path": str(path), "kind": "image", "cover": index == 0})

    with db.session_scope() as session:
        account = make_account(session, account_id="xhs-demo-01", platform="xhs")
        session.add(
            ContentItem(
                id="itm_cards",
                account_id=account.id,
                status=ContentStatus.DRAFT.value,
                bundle_json={
                    "id": "itm_cards",
                    "account_id": account.id,
                    "platform": "xhs",
                    "title": "租房不打孔",
                    "body_markdown": "正文" * 40 + "\n\n#租房 #独居",
                    "media": media,
                    "tags": ["租房", "独居"],
                    "platform_extra": {
                        "tags": ["租房", "独居"],
                        "schedule_at": "2026-08-20T10:00:00+08:00",
                        "is_original": True,
                    },
                },
            )
        )
    try:
        yield "itm_cards"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_review_detail_renders_all_cards(client, item_with_cards) -> None:
    detail = client.get(f"/review/{item_with_cards}")
    assert detail.status_code == 200
    assert "图文卡片（3 张）" in detail.text
    for index in range(3):
        assert f"/review/{item_with_cards}/media/{index}" in detail.text
    assert "原创声明：是" in detail.text
    assert "2026-08-20T10:00:00+08:00" in detail.text


def test_media_endpoint_serves_each_card(client, item_with_cards) -> None:
    for index in range(3):
        response = client.get(f"/review/{item_with_cards}/media/{index}")
        assert response.status_code == 200
        assert response.content.startswith(b"\x89PNG")
    assert client.get(f"/review/{item_with_cards}/media/99").status_code == 404
    assert client.get(f"/review/{item_with_cards}/media/-1").status_code == 404


def test_media_endpoint_blocks_path_traversal(client) -> None:
    """bundle_json 里的路径来自生成链，但不该能读到仓库外的文件。"""
    from core import db

    with db.session_scope() as session:
        account = make_account(session, account_id="xhs-demo-01", platform="xhs")
        session.add(
            ContentItem(
                id="itm_evil_media",
                account_id=account.id,
                status=ContentStatus.DRAFT.value,
                bundle_json={
                    "id": "itm_evil_media",
                    "account_id": account.id,
                    "platform": "xhs",
                    "title": "越界",
                    "body_markdown": "正文",
                    "media": [{"path": "../../../../etc/passwd", "kind": "image", "cover": True}],
                    "tags": [],
                    "platform_extra": {},
                },
            )
        )
    assert client.get("/review/itm_evil_media/media/0").status_code == 404


def test_review_detail_shows_missing_cards_hint(client) -> None:
    from core import db

    with db.session_scope() as session:
        account = make_account(session, account_id="xhs-demo-01", platform="xhs")
        session.add(
            ContentItem(
                id="itm_nocards",
                account_id=account.id,
                status=ContentStatus.DRAFT.value,
                bundle_json={
                    "id": "itm_nocards",
                    "account_id": account.id,
                    "platform": "xhs",
                    "title": "缺图",
                    "body_markdown": "正文",
                    "media": [],
                    "tags": [],
                    "platform_extra": {},
                },
            )
        )
    detail = client.get("/review/itm_nocards")
    assert "playwright install chromium" in detail.text
