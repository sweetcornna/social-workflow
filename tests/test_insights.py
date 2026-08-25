"""复盘 Agent（``metrics/insights.py``）：汇总 → 结构化结论 → 回灌选题。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import prompts
from core import db
from core.models import ContentItem, MetricSnapshot, PublishRecord, new_id
from core.state_machine import ContentStatus, PublishPhase
from generation.llm import LLMError, ScriptedLLM
from metrics import insights
from metrics.insights import InsightsReport, collect_summary, generate_for_account, run
from publishers.base import ContentBundle
from tests.conftest import make_account

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


def _report(**kwargs) -> InsightsReport:
    base = {
        "headline": "带具体价格的标题表现明显更好",
        "what_worked": ["标题里出现「89 元」这类具体金额的两条，点赞是其余的 3 倍"],
        "what_failed": ["设问式标题（「你们都怎么…」）三条全部低于均值"],
        "topic_guidance": ["继续做「租房不打孔」这一族"],
        "title_patterns": ["数字 + 具体物件 + 结果"],
        "best_slots": ["19:00-22:00 好于午间"],
        "next_actions": ["下一轮三条标题里至少两条带价格"],
        "confidence": "medium",
        "note": "",
    }
    base.update(kwargs)
    return InsightsReport(**base)


def _publish(session, account, *, title: str, at: datetime, metrics: dict) -> ContentItem:
    """造一条"已发布且采到指标"的现场。"""
    item_id = new_id("itm")
    bundle = ContentBundle(
        id=item_id,
        account_id=account.id,
        platform=account.platform,
        title=title,
        body_markdown="正文",
        tags=["测试"],
        platform_extra={"selection": {"topic_source": "newsnow", "angle": "从价格切入"}},
    )
    item = ContentItem(
        id=item_id,
        account_id=account.id,
        status=ContentStatus.MEASURED.value,
        bundle_json=bundle.model_dump(mode="json"),
    )
    session.add(item)
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
    record.updated_at = at  # onupdate=utcnow 会把它推到现在，按需求值写回
    session.flush()
    return item


# --------------------------------------------------------------------- 汇总


def test_collect_summary_gathers_posts_and_failures(session):
    account = make_account(session, account_id="ins-1")
    _publish(session, account, title="甲", at=NOW - timedelta(days=2), metrics={"likes": 30})
    _publish(session, account, title="乙", at=NOW - timedelta(days=1), metrics={"likes": 10})
    # 窗口外的不算
    _publish(session, account, title="太老了", at=NOW - timedelta(days=30), metrics={"likes": 999})

    summary = collect_summary(session, account, now=NOW, window_days=7)
    assert summary.published == 2
    assert [p.title for p in summary.posts] == ["甲", "乙"]
    assert summary.totals()["likes"] == 40
    # 没有任何一条给出 views → 汇总是 None 而不是 0
    assert summary.totals()["views"] is None


def test_collect_summary_counts_dead_letters(session):
    from tests.conftest import make_item

    account = make_account(session, account_id="ins-2")
    make_item(session, account, status=ContentStatus.DEAD_LETTER.value)
    make_item(session, account, status=ContentStatus.RETRYING.value)
    summary = collect_summary(session, account, now=NOW)
    assert summary.dead_letter == 1 and summary.failed == 1


def test_collect_summary_ignores_unavailable_history_and_counts_only_unavailable_as_unmeasured(
    session,
):
    account = make_account(session, account_id="ins-unavailable")
    earlier = _publish(
        session,
        account,
        title="有效历史",
        at=NOW - timedelta(days=2),
        metrics={"likes": 30},
    )
    session.add(
        MetricSnapshot(
            id=new_id("mtr"),
            content_item_id=earlier.id,
            platform_post_id="p-history",
            snapshot_at=NOW - timedelta(hours=1),
            metrics_json={"available": False, "likes": None},
        )
    )
    session.add(
        MetricSnapshot(
            id=new_id("mtr"),
            content_item_id=earlier.id,
            platform_post_id="p-history",
            snapshot_at=NOW,
            metrics_json=["malformed history"],
        )
    )
    _publish(
        session,
        account,
        title="只有不可用",
        at=NOW - timedelta(days=1),
        metrics={"available": False, "likes": None},
    )
    session.commit()

    summary = collect_summary(session, account, now=NOW)
    assert [post.title for post in summary.posts] == ["有效历史"]
    assert summary.totals()["likes"] == 30
    assert summary.unmeasured == 1


def test_render_stats_table_marks_missing_metrics_as_dash(session):
    from core.accounts import policy_of

    account = make_account(session, account_id="ins-3")
    _publish(session, account, title="甲", at=NOW - timedelta(days=1), metrics={"likes": 5})
    summary = collect_summary(session, account, now=NOW)
    table = insights.render_stats_table(summary, policy_of(account))
    assert "甲" in table and "—" in table, "缺失字段要显式画成 —，不能填 0"


# ---------------------------------------------------------------- 生成复盘


def test_generate_writes_insights_file(session, monkeypatch, tmp_path):
    monkeypatch.setattr(prompts, "ACCOUNTS_DIR", tmp_path)
    account = make_account(session, account_id="ins-write")
    for index in range(3):
        _publish(
            session,
            account,
            title=f"第 {index} 条",
            at=NOW - timedelta(days=index + 1),
            metrics={"likes": 10 * (index + 1)},
        )

    llm = ScriptedLLM(parsed_replies=[_report()])
    report = generate_for_account(session, account, llm, now=NOW)

    assert report is not None and report.confidence == "medium"
    path = tmp_path / "ins-write" / "insights.md"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "带具体价格的标题" in text
    assert "近 7 天复盘" in text
    # 写完要记时间戳，下一轮才知道该不该再跑
    assert account.extra["insights_updated_at"].startswith("2026-08-16")


def test_generate_skips_when_sample_too_small(session, monkeypatch, tmp_path):
    """三条数据推不出规律，只会生成一段像模像样的噪声。"""
    monkeypatch.setattr(prompts, "ACCOUNTS_DIR", tmp_path)
    account = make_account(session, account_id="ins-small")
    _publish(session, account, title="就一条", at=NOW - timedelta(days=1), metrics={"likes": 1})

    llm = ScriptedLLM(parsed_replies=[_report()])
    assert generate_for_account(session, account, llm, now=NOW) is None
    assert not (tmp_path / "ins-small").exists()
    assert llm.calls == [], "样本不足时一次 LLM 都不该调"


def test_insights_file_keeps_only_recent_entries(monkeypatch, tmp_path):
    monkeypatch.setattr(prompts, "ACCOUNTS_DIR", tmp_path)
    for index in range(5):
        prompts.append_insight("acc", f"## 第 {index} 条复盘", keep=3)
    entries = prompts.split_insights((tmp_path / "acc" / "insights.md").read_text("utf-8"))
    assert len(entries) == 3
    assert "第 4 条" in entries[-1] and "第 2 条" in entries[0]


def test_load_insights_returns_most_recent(monkeypatch, tmp_path):
    monkeypatch.setattr(prompts, "ACCOUNTS_DIR", tmp_path)
    prompts.append_insight("acc", "## 老的")
    prompts.append_insight("acc", "## 新的")
    text = prompts.load_insights("acc", limit=1)
    assert "新的" in text and "老的" not in text


def test_load_insights_missing_file_is_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(prompts, "ACCOUNTS_DIR", tmp_path)
    assert prompts.load_insights("nobody") == ""


# ------------------------------------------------------------------- run()


def test_run_skips_without_api_key(session, monkeypatch):
    """无 key 时整体跳过——**不**用 ScriptedLLM 顶替，复盘是长期资产。"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    from core.config import reload_settings

    reload_settings()
    make_account(session, account_id="ins-nokey")
    stats = run(session, now=NOW)
    assert stats["skipped_no_key"] == 1 and stats["written"] == 0


def test_run_disabled_by_setting(session, monkeypatch):
    monkeypatch.setenv("INSIGHTS_ENABLED", "false")
    from core.config import reload_settings

    reload_settings()
    make_account(session, account_id="ins-off")
    assert run(session, now=NOW)["scanned"] == 0


def test_run_throttles_per_account(session, monkeypatch, tmp_path):
    monkeypatch.setattr(prompts, "ACCOUNTS_DIR", tmp_path)
    account = make_account(session, account_id="ins-throttle")
    for index in range(3):
        _publish(
            session, account, title=f"{index}", at=NOW - timedelta(days=1), metrics={"likes": 1}
        )

    first = run(session, llm=ScriptedLLM(parsed_replies=[_report()]), now=NOW)
    assert first["written"] == 1

    # 24 小时内不重复跑
    second = run(session, llm=ScriptedLLM(parsed_replies=[_report()]), now=NOW + timedelta(hours=3))
    assert second["skipped_not_due"] == 1 and second["written"] == 0

    # 到点了再跑
    third = run(session, llm=ScriptedLLM(parsed_replies=[_report()]), now=NOW + timedelta(hours=25))
    assert third["written"] == 1


def test_run_force_ignores_throttle(session, monkeypatch, tmp_path):
    monkeypatch.setattr(prompts, "ACCOUNTS_DIR", tmp_path)
    account = make_account(session, account_id="ins-force")
    for index in range(3):
        _publish(
            session, account, title=f"{index}", at=NOW - timedelta(days=1), metrics={"likes": 1}
        )
    run(session, llm=ScriptedLLM(parsed_replies=[_report()]), now=NOW)
    again = run(session, llm=ScriptedLLM(parsed_replies=[_report()]), now=NOW, force=True)
    assert again["written"] == 1


def test_run_records_llm_failure_and_moves_on(session, monkeypatch, tmp_path):
    """一个账号的模型调用炸了，不能拖住其它账号，也不能每轮都重试烧 token。"""
    monkeypatch.setattr(prompts, "ACCOUNTS_DIR", tmp_path)
    account = make_account(session, account_id="ins-boom")
    for index in range(3):
        _publish(
            session, account, title=f"{index}", at=NOW - timedelta(days=1), metrics={"likes": 1}
        )

    llm = ScriptedLLM(parsed_replies=[_report()], raise_exc=LLMError("模型挂了"))
    stats = run(session, llm=llm, now=NOW)
    assert stats["failed"] == 1 and stats["written"] == 0
    assert "模型挂了" in account.extra["insights_error"]
    assert account.extra["insights_updated_at"], "失败也要推进时间戳，否则每轮都重试"


# ------------------------------------------------------- 回灌到选题 Agent


def test_selector_prompt_includes_insights(monkeypatch, tmp_path):
    """闭环的最后一段：复盘结论必须真的出现在选题 prompt 里。"""
    monkeypatch.setattr(prompts, "ACCOUNTS_DIR", tmp_path)
    prompts.append_insight("acc-loop", "## 复盘\n\n- 带价格的标题更好")

    from sourcing.base import RawTopic
    from sourcing.selector import SelectionResult, load_insights, select_topics

    llm = ScriptedLLM(parsed_replies=[SelectionResult(recommended=[], note="不选")])
    select_topics(
        [RawTopic(source="t", title="候选一")],
        llm,
        persona="人设",
        insights=load_insights("acc-loop"),
    )
    prompt = llm.calls[0]["prompt"]
    assert "带价格的标题更好" in prompt
    assert "上一轮复盘的结论" in prompt


def test_selector_prompt_without_insights_says_so():
    from sourcing.base import RawTopic
    from sourcing.selector import SelectionResult, select_topics

    llm = ScriptedLLM(parsed_replies=[SelectionResult(recommended=[], note="不选")])
    select_topics([RawTopic(source="t", title="候选一")], llm, persona="人设")
    assert "还没有复盘数据" in llm.calls[0]["prompt"]


def test_tick_insights_wires_through(monkeypatch, tmp_path):
    from core.scheduler import tick_insights

    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    from core.config import reload_settings

    reload_settings()
    with db.session_scope() as session:
        make_account(session, account_id="ins-tick")
    assert tick_insights()["skipped_no_key"] == 1
