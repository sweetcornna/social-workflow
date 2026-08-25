"""连续运行验证的回归版本（``scripts/soak.py``）。

和脚本跑的是**同一个** :func:`scripts.soak.run_soak`——不是"测试里另写一遍"，
所以这条用例绿了就等于 `uv run python scripts/soak.py` 会绿。

预算：整文件应在几秒内跑完（远低于任务书要求的 30 秒）。真跑三天要 144 步，
每步几个 SQL，全在 tmp_path 的 SQLite 上。

两层防御分别对应 :func:`test_soak_schedule_layer_slots_are_legal`（排期层）与
:func:`test_soak_publish_layer_blocks_tampered_schedules`（发布层）。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from core import db
from core.models import MetricSnapshot, new_id
from core.notify import set_default_notifier
from publishers.registry import reset_registry
from scripts.soak import SoakConfig, SoakResult, _collect, run_soak, soak_env
from tests.conftest import make_account, make_publish_record

#: 任务书要求 < 30s；给一半的余量当回归阈值，慢下来要能立刻发现
TIME_BUDGET_SECONDS = 15.0


@pytest.fixture(scope="module")
def soak_result(tmp_path_factory):
    """整模块只跑一次模拟，各条断言分开看结果（失败时定位更清楚）。"""
    path = tmp_path_factory.mktemp("soak") / "soak.db"
    url = f"sqlite:///{path}"
    started = time.monotonic()
    try:
        with soak_env(SW_DATABASE_URL=url):
            db.configure(url)
            db.init_db()
            result = run_soak(SoakConfig(days=3, step_minutes=30))
    finally:
        set_default_notifier(None)
        reset_registry()
        db.configure()  # 还原成 conftest 那套，别污染后面的用例
    result_elapsed = time.monotonic() - started
    return result, result_elapsed


def test_soak_covers_both_gate_layers(soak_result):
    """限频 / 时段窗口有两层防御，两层都要被真实行使过。

    只验一层的话另一层废了也看不出来：排期层排得再对，人手工改一次库就绕过了；
    发布层拦得再稳，排期层排到窗口外只会让内容一直延后。
    """
    result, _ = soak_result
    names = [name for name, _ok, _detail in result.checks]
    assert [n for n in names if n.startswith("排期层")], "缺排期层断言"
    assert [n for n in names if n.startswith("发布层")], "缺发布层断言"


def test_soak_all_checks_pass(soak_result):
    """六组硬断言全绿。失败时把具体哪条打出来。"""
    result, _ = soak_result
    assert result.ok, "soak 断言失败：\n  " + "\n  ".join(result.failures)
    # 断言本身必须真的跑过——空列表也会让 ok 为 True
    assert len(result.checks) >= 20


def test_soak_runs_three_simulated_days(soak_result):
    result, _ = soak_result
    assert result.steps == 144, "3 天 × 每步 30 分钟 = 144 步"
    assert result.generated > 0, "一条稿都没生成，生成链没被真正驱动"
    assert result.published > 0, "一条都没发出去，发布链没被真正驱动"


def test_soak_no_duplicate_publish(soak_result):
    result, _ = soak_result
    assert result.duplicate_attempts == 0
    assert result.done_records == result.published + result.retry_published


def test_soak_schedule_layer_slots_are_legal(soak_result):
    """第一层：``schedule_item`` 排出来的槽位本身就合法。

    重构后"批准即排期"，槽位同时是运营看到的"这条几点发"——排错了不只是发得不对，
    人还会拿它当事实去规划。
    """
    result, _ = soak_result
    assert result.auto_scheduled > 0, "一条都没排上期，排期层没被驱动"
    assert result.approved == result.auto_scheduled, "有内容积压在 approved"
    assert result.slots_out_of_window == []
    assert result.slot_interval_violations == []
    assert result.slot_daily_overflow == []


def test_soak_publish_layer_blocks_tampered_schedules(soak_result):
    """第二层：绕过排期层塞进来的脏排期，发布时刻仍然被闸门挡住。

    脏数据由 ``_inject_tampered`` 直接写库（窗口外 + 同一分钟挤一堆），
    模拟人工改库 / 手工改期 / 时钟漂移。
    """
    result, _ = soak_result
    assert result.skipped_window > 0, "发布时段窗口闸门整轮没被触发"
    assert result.skipped_rate > 0, "限频闸门整轮没被触发"
    assert result.dirty_injected > 0, "脏数据没注进去，上面两条等于空转"
    # 注入的排期得**真的**不合法（比如以后有人把窗口改成全天，就不脏了）
    assert result.dirty_out_of_window > 0
    assert result.dirty_too_close > 0
    assert 0 < result.dirty_published < result.dirty_injected, (
        "脏排期要么全被放行，要么全被别的东西挡着"
    )
    assert result.dirty_deferred > 0, "没有一条脏排期是被推迟到合法时刻才发的"
    # 闸门挡住没挡住，看的是结果：一条都不许在窗口外或超额发出去
    assert result.published_out_of_window == []
    assert result.publish_interval_violations == []
    assert result.max_daily.get("soak-xhs-ok", 0) <= 2
    assert result.max_daily.get("soak-xhs-tampered", 0) <= 2


def test_soak_skips_needs_relogin_account(soak_result):
    result, _ = soak_result
    assert result.offline_published == 0
    assert result.skipped_account > 0


def test_soak_dead_letter_notifies(soak_result):
    result, _ = soak_result
    assert result.dead_letters > 0
    assert result.dead_letter_notices > 0


def test_soak_metric_windows_both_covered(soak_result):
    result, _ = soak_result
    assert result.snapshots_24h >= 1
    assert result.snapshots_7d >= 1


def test_soak_collect_ignores_explicitly_unavailable_snapshot():
    started_at = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    with db.session_scope() as session:
        account = make_account(session, account_id="soak-unavailable")
        record = make_publish_record(session, account.id, at=started_at)
        record.platform_post_id = "post-unavailable"
        session.add(
            MetricSnapshot(
                id=new_id("mtr"),
                content_item_id=record.content_item_id,
                platform_post_id=record.platform_post_id,
                snapshot_at=started_at + timedelta(days=8),
                metrics_json={"available": False, "reason": "not ready"},
            )
        )
        session.add(
            MetricSnapshot(
                id=new_id("mtr"),
                content_item_id=record.content_item_id,
                platform_post_id=record.platform_post_id,
                snapshot_at=started_at + timedelta(days=8),
                metrics_json=["malformed history"],
            )
        )
    result = SoakResult(config=SoakConfig(), started_at=started_at)

    _collect(result, {})

    assert result.snapshots_24h == result.snapshots_7d == 0


def test_soak_is_fast_enough(soak_result):
    """任务书要求 tests/test_soak.py < 30s。"""
    _, elapsed = soak_result
    assert elapsed < TIME_BUDGET_SECONDS, f"soak 跑了 {elapsed:.1f}s，超出回归预算"
