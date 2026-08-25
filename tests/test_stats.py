"""统计页 `/stats` 与 `/stats.json`（P4 改成按账号的运营看板）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from core import db
from core.budget import BudgetGuard, CostKind
from core.models import ContentItem, MetricSnapshot, PublishRecord, new_id
from core.state_machine import ContentStatus, PublishPhase
from core.stats import build_dashboard
from publishers.base import ContentBundle
from tests.conftest import make_account, make_item

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


def _published(session, account, *, at: datetime, metrics: dict | None = None) -> ContentItem:
    item_id = new_id("itm")
    bundle = ContentBundle(
        id=item_id,
        account_id=account.id,
        platform=account.platform,
        title="已发内容",
        body_markdown="正文",
    )
    session.add(
        ContentItem(
            id=item_id,
            account_id=account.id,
            status=ContentStatus.MEASURED.value,
            bundle_json=bundle.model_dump(mode="json"),
        )
    )
    record = PublishRecord(
        id=new_id("pub"),
        content_item_id=item_id,
        idem_key=new_id("idem"),
        phase=PublishPhase.DONE.value,
        platform_post_id=f"p-{item_id}",
        attempts=1,
        created_at=at,
        updated_at=at,
    )
    session.add(record)
    session.flush()
    record.updated_at = at
    if metrics is not None:
        session.add(
            MetricSnapshot(
                id=new_id("mtr"),
                content_item_id=item_id,
                platform_post_id=record.platform_post_id,
                snapshot_at=at + timedelta(hours=24),
                metrics_json=metrics,
            )
        )
    session.flush()
    return item_id


# ------------------------------------------------------------------ 聚合


def test_dashboard_rolls_up_per_account(session):
    account = make_account(session, account_id="st-1", daily_limit=5)
    account.extra = {"daily_target": 2, "publish_windows": ["09:00-11:00"], "timezone": "UTC"}
    _published(session, account, at=NOW - timedelta(days=1), metrics={"likes": 12, "views": None})
    _published(session, account, at=NOW - timedelta(days=2), metrics={"likes": 8})
    make_item(session, account, status=ContentStatus.DEAD_LETTER.value)
    make_item(session, account, status=ContentStatus.RETRYING.value)
    make_item(session, account, status=ContentStatus.DRAFT.value)
    session.commit()

    dash = build_dashboard(session, now=NOW, window_days=7)
    row = next(r for r in dash.accounts if r.account_id == "st-1")
    assert row.published == 2
    assert row.dead_letter == 1 and row.failed == 1
    assert row.pending_review == 1
    assert row.metric("likes") == 20
    assert row.metric("views") is None, "全员缺失要是 None 而不是 0"
    assert row.snapshots_24h == 2
    assert row.daily_target == 2 and row.windows == "09:00-11:00"


def test_dashboard_uses_latest_snapshot_only(session):
    """快照是只追加的，直接 SUM 会把 24h 和 7d 两张算两遍。"""
    account = make_account(session, account_id="st-dup")
    item_id = _published(session, account, at=NOW - timedelta(days=8), metrics={"likes": 10})
    session.add(
        MetricSnapshot(
            id=new_id("mtr"),
            content_item_id=item_id,
            platform_post_id="p",
            snapshot_at=NOW - timedelta(hours=1),
            metrics_json={"likes": 99},
        )
    )
    session.commit()
    dash = build_dashboard(session, now=NOW, window_days=30)
    row = next(r for r in dash.accounts if r.account_id == "st-dup")
    assert row.metric("likes") == 99, "只算最新那张"


def test_dashboard_counts_both_metric_windows(session):
    account = make_account(session, account_id="st-win")
    item_id = _published(session, account, at=NOW - timedelta(days=8), metrics={"likes": 1})
    session.add(
        MetricSnapshot(
            id=new_id("mtr"),
            content_item_id=item_id,
            platform_post_id="p",
            snapshot_at=NOW - timedelta(hours=2),  # 发布 8 天后 → 7d 窗口
            metrics_json={"likes": 2},
        )
    )
    session.commit()
    dash = build_dashboard(session, now=NOW, window_days=30)
    row = next(r for r in dash.accounts if r.account_id == "st-win")
    assert row.snapshots_24h == 1 and row.snapshots_7d == 1


def test_dashboard_ignores_unavailable_snapshot_history(session):
    account = make_account(session, account_id="st-unavailable")
    item_id = _published(session, account, at=NOW - timedelta(days=8), metrics={"likes": 10})
    session.add(
        MetricSnapshot(
            id=new_id("mtr"),
            content_item_id=item_id,
            platform_post_id="p",
            snapshot_at=NOW - timedelta(hours=1),
            metrics_json={"available": False, "likes": None, "reason": "platform delayed"},
        )
    )
    session.add(
        MetricSnapshot(
            id=new_id("mtr"),
            content_item_id=item_id,
            platform_post_id="p",
            snapshot_at=NOW,
            metrics_json=["malformed history"],
        )
    )
    session.commit()

    dash = build_dashboard(session, now=NOW, window_days=30)
    row = next(r for r in dash.accounts if r.account_id == "st-unavailable")
    assert row.metric("likes") == 10, "较新的不可用行不得遮蔽较早有效指标"
    assert row.measured_posts == 1
    assert row.snapshots_24h == 1 and row.snapshots_7d == 0
    assert dash.snapshot_count == 1


def test_dashboard_attributes_cost_by_account(session):
    make_account(session, account_id="st-cost")
    make_account(session, account_id="st-other")
    BudgetGuard(session, labels={"account_id": "st-cost"}).charge(CostKind.TOKENS, 500)
    BudgetGuard(session).charge(CostKind.TOKENS, 70)  # 没标签 → 未归属
    session.commit()

    dash = build_dashboard(session, now=None)
    row = next(r for r in dash.accounts if r.account_id == "st-cost")
    other = next(r for r in dash.accounts if r.account_id == "st-other")
    assert row.cost["tokens"] == 500
    assert other.cost == {}
    assert dash.unattributed_cost["tokens"] == 70


def test_dashboard_flags_accounts_needing_attention(session):
    make_account(session, account_id="st-ok")
    make_account(session, account_id="st-relogin", status="needs_relogin")
    make_account(session, account_id="st-banned", status="banned")
    session.commit()

    dash = build_dashboard(session, now=NOW)
    assert {r.account_id for r in dash.attention} == {"st-relogin", "st-banned"}
    # 需要人处理的排最上面
    assert dash.accounts[0].needs_attention


def test_dashboard_lists_recent_dead_letters(session):
    account = make_account(session, account_id="st-dl")
    item = make_item(session, account, status=ContentStatus.DEAD_LETTER.value, title="炸了的稿")
    from core.state_machine import SystemAction, log_review

    log_review(session, item, actor="system", action=SystemAction.DEAD_LETTER, reason="重试 3 次")
    session.commit()

    dash = build_dashboard(session, now=NOW)
    assert dash.dead_letters[0].title == "炸了的稿"
    assert "重试 3 次" in dash.dead_letters[0].reason


def test_dashboard_used_today_comes_from_publish_records(session):
    # 时钟必须钉死：``used_today`` 按**账号本地日**切（``core.ratelimit.local_day_start``，
    # P11.3 之前是 UTC 日），而用真实 ``datetime.now(UTC) - 1h`` 造数据时，只要跑在
    # 本地日的头一个小时内，这条记录就落到了前一个本地日，count 变 0。
    # 本文件其余用例都用 NOW，这里跟齐。跨日界本身另有专门的回归用例：
    # tests/test_ratelimit.py 与 tests/test_scheduling.py 的"UTC 午夜缝"那两段。
    account = make_account(session, account_id="st-quota", daily_limit=3)
    _published(session, account, at=NOW - timedelta(hours=1))
    session.commit()
    dash = build_dashboard(session, now=NOW)
    row = next(r for r in dash.accounts if r.account_id == "st-quota")
    assert row.used_today == 1 and row.quota_left == 2


# ------------------------------------------------------------------ 端点


def test_stats_page_renders_with_accounts(client):
    with db.session_scope() as session:
        account = make_account(session, account_id="page-1", daily_limit=4)
        account.extra = {"daily_target": 2, "publish_windows": ["09:00-11:00"]}
        _published(session, account, at=datetime.now(UTC) - timedelta(days=1), metrics={"likes": 3})

    resp = client.get("/stats")
    assert resp.status_code == 200
    body = resp.text
    assert "page-1" in body
    assert "09:00-11:00" in body
    assert "各账号" in body


def test_stats_page_highlights_needs_relogin(client):
    with db.session_scope() as session:
        make_account(session, account_id="page-relogin", status="needs_relogin")
    body = client.get("/stats").text
    assert "需要人处理" in body
    assert "/accounts/page-relogin/login" in body


def test_stats_page_empty_db_tells_you_to_sync(client):
    body = client.get("/stats").text
    assert "core.accounts sync" in body


def test_stats_json_shape(client):
    with db.session_scope() as session:
        make_account(session, account_id="json-1")
    payload = client.get("/stats.json", params={"days": 14}).json()
    assert payload["window_days"] == 14
    assert payload["accounts"][0]["id"] == "json-1"
    assert "totals" in payload and "budget" in payload
    assert "content" in payload


def test_stats_json_window_is_clamped(client):
    assert client.get("/stats.json", params={"days": 9999}).json()["window_days"] == 90
    assert client.get("/stats.json", params={"days": 0}).json()["window_days"] == 1


# ---------------------------------------------------- 输出预算体检（P11.2）


def test_truncated_calls_counts_max_tokens_endings(session) -> None:
    """被 max_tokens 掐停的调用要能按调用点数出来。

    截断不会自己冒头：自愈重试让链路照常出稿，只是白烧一倍 token，
    直到某次思考跑偏、正文一个字都没写才 502。所以要能主动看见。
    """
    from core.budget import today_key
    from core.stats import truncated_calls

    guard = BudgetGuard(session, token_budget=10_000)
    for stop_reason, purpose in (
        ("max_tokens", "xhs.cards"),
        ("max_tokens", "xhs.cards"),
        ("max_tokens", "sourcing.select"),
        ("end_turn", "xhs.note"),
    ):
        guard.charge(CostKind.TOKENS, 10, meta={"purpose": purpose, "stop_reason": stop_reason})

    assert truncated_calls(session, today_key()) == {"xhs.cards": 2, "sourcing.select": 1}


def test_truncated_calls_is_empty_when_nothing_hit_the_ceiling(session) -> None:
    from core.budget import today_key
    from core.stats import truncated_calls

    guard = BudgetGuard(session, token_budget=10_000)
    guard.charge(CostKind.TOKENS, 10, meta={"purpose": "xhs.note", "stop_reason": "end_turn"})
    assert truncated_calls(session, today_key()) == {}
