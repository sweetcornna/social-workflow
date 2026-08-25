"""调度骨架与按账号限频测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import respx
from sqlalchemy import select

from core import db
from core.models import Account, ContentItem, MetricSnapshot, utcnow
from core.scheduler import (
    RateLimiter,
    create_scheduler,
    tick_login_health,
    tick_metrics,
    tick_render_jobs,
    tick_scheduled_publish,
)
from core.state_machine import ContentStatus
from publishers.base import FakePublisher, PublishResult
from publishers.registry import register, use_fake_publishers
from tests.conftest import NO_CONFIRM, make_account, make_item

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def test_rate_limiter_daily_limit():
    limiter = RateLimiter(min_interval_seconds=0)
    assert limiter.allow("acc-1", 2, now=NOW)
    limiter.record("acc-1", now=NOW)
    limiter.record("acc-1", now=NOW)
    assert limiter.used_today("acc-1", now=NOW) == 2
    assert not limiter.allow("acc-1", 2, now=NOW)
    # 跨天重置
    assert limiter.allow("acc-1", 2, now=NOW + timedelta(days=1))
    # 账号之间互不影响
    assert limiter.allow("acc-2", 1, now=NOW)


def test_rate_limiter_min_interval():
    limiter = RateLimiter(min_interval_seconds=900)
    limiter.record("acc-1", now=NOW)
    assert not limiter.allow("acc-1", 99, now=NOW + timedelta(minutes=5))
    assert limiter.allow("acc-1", 99, now=NOW + timedelta(minutes=16))
    assert limiter.next_available_at("acc-1") == NOW + timedelta(minutes=15)
    limiter.reset()
    assert limiter.next_available_at("acc-1") is None


def test_tick_publishes_due_items_and_respects_limit():
    use_fake_publishers()
    with db.session_scope() as session:
        # 这条考的是日上限，不是确认闸门（P12）
        account = make_account(session, daily_limit=1, extra=NO_CONFIRM)
        make_item(session, account, title="到期 1", scheduled_in_minutes=-10)
        make_item(session, account, title="到期 2", scheduled_in_minutes=-5)
        make_item(session, account, title="未到期", scheduled_in_minutes=600)

    limiter = RateLimiter(min_interval_seconds=0)
    stats = tick_scheduled_publish(limiter=limiter)

    assert stats["scanned"] == 2, "未到期的不该被扫到"
    assert stats["published"] == 1
    assert stats["skipped"] == 1, "日上限 1，第二条应被限频跳过"

    with db.session_scope() as session:
        statuses = sorted(s for s in session.scalars(select(ContentItem.status)))
    assert statuses == ["published", "scheduled", "scheduled"]


def test_tick_skips_unhealthy_accounts():
    use_fake_publishers()
    with db.session_scope() as session:
        account = make_account(session, status="needs_relogin")
        make_item(session, account, scheduled_in_minutes=-10)

    stats = tick_scheduled_publish(limiter=RateLimiter(min_interval_seconds=0))
    assert stats["scanned"] == 1
    assert stats["published"] == 0
    assert stats["failed"] == 0
    # P4 起 skipped 拆出明细，方便运维一眼看出是被哪道闸门挡的
    assert stats["skipped"] == 1 and stats["skipped_account"] == 1


class _OkFalsePublisher(FakePublisher):
    """违反契约的发布器：失败时返回 ``ok=False`` 而不抛异常。

    ``publish_with_idempotency`` 的 ``if not result.ok`` 分支会兜底把 item 打到
    retrying / dead_letter 然后**正常 return**——不抛异常，状态也不是 published。
    """

    def publish(self, bundle):
        self.publish_calls += 1
        return PublishResult(ok=False, raw={"reason": "契约违规：失败必须抛异常"})


def test_publish_tick_counts_items_whose_state_never_advanced():
    """第六道闸门（``publish_with_idempotency`` 返回了但状态没推进到 published）
    必须有自己的计数键，并计入总 ``skipped``。

    修复前这一支静默 ``continue``，一个计数都不加：``scanned`` 与
    ``published + skipped + failed`` 对不上，排查的人拿到一张全零 stats，
    看不出这条被谁拦下。这里走 dry-run 发布器命中已 done 幂等记录那条路。
    """
    use_fake_publishers()
    with db.session_scope() as session:
        account = make_account(session, extra=NO_CONFIRM)
        make_item(session, account, scheduled_in_minutes=-5, now=NOW)

    assert tick_scheduled_publish(now=NOW)["published"] == 1, "先让幂等记录落到 done"

    # 同账号 + 同标题 + 同槽位 → 同一个 idem_key（make_idem_key 不含 content_item_id）
    with db.session_scope() as session:
        account = session.get(Account, "acc-1")
        make_item(session, account, scheduled_in_minutes=-5, now=NOW)

    register(
        "xhs", lambda account_id, **kw: FakePublisher(account_id, platform="xhs", dry_run=True)
    )

    stats = tick_scheduled_publish(now=NOW)

    assert stats["scanned"] == 1
    assert stats["published"] == 0 and stats["failed"] == 0
    assert stats["skipped_not_advanced"] == 1, "第六道要有自己的 skipped_* 明细"
    assert stats["skipped"] == 1, "第六道也要计入总 skipped，否则 scanned 对不上"
    assert stats["scanned"] == stats["published"] + stats["skipped"] + stats["failed"], (
        "scanned == published + skipped + failed 必须恒成立"
    )


def test_publish_tick_counts_contract_breaking_ok_false_result():
    """同一道闸门的另一条成因：publisher 违反契约返回 ``ok=False`` 而不抛异常。

    state_machine 的 ``if not result.ok`` 兜底把 item 打到 retrying 后正常 return，
    于是既不计 ``failed``（没抛异常）也不计 ``published``。修复前它在本 tick 的
    stats 里毫无痕迹。

    这条路和 dry-run 那条**当前都不可达**（现有发布器的 ``ok=False`` 全在 dry-run
    分支里，而 dry-run 在 ``publish_with_idempotency`` 里提前 return，到不了那道
    兜底）——所以这里得自己造一个破契约的发布器。守的是"万一将来真有"，
    见 docs/RISKS.md 第 13 条。
    """
    use_fake_publishers()
    with db.session_scope() as session:
        account = make_account(session, extra=NO_CONFIRM)
        item_id = make_item(session, account, scheduled_in_minutes=-5, now=NOW).id

    register("xhs", lambda account_id, **kw: _OkFalsePublisher(account_id, platform="xhs"))

    stats = tick_scheduled_publish(now=NOW)

    assert stats["scanned"] == 1
    assert stats["published"] == 0
    assert stats["failed"] == 0, "没抛异常，走不到 except PublishError"
    assert stats["skipped"] == 1 and stats["skipped_not_advanced"] == 1
    assert stats["scanned"] == stats["published"] + stats["skipped"] + stats["failed"]

    with db.session_scope() as session:
        assert session.get(ContentItem, item_id).status == ContentStatus.RETRYING.value


def test_tick_metrics_appends_snapshot():
    use_fake_publishers()
    with db.session_scope() as session:
        account = make_account(session, extra=NO_CONFIRM)
        make_item(session, account, scheduled_in_minutes=-10)
    tick_scheduled_publish(limiter=RateLimiter(min_interval_seconds=0))

    moment = utcnow()
    first = tick_metrics(now=moment)
    assert first["snapshots"] == first["attempted"] == 1
    assert first["unavailable"] == 0
    second = tick_metrics(now=moment + timedelta(hours=6))
    assert second["snapshots"] == second["attempted"] == 1, "只追加，不更新"

    with db.session_scope() as session:
        snapshots = session.scalars(select(MetricSnapshot)).all()
        assert len(snapshots) == 2
        assert snapshots[0].metrics_json["views"] == 100
        item = session.scalars(select(ContentItem)).one()
        assert item.status == ContentStatus.MEASURED


def test_create_scheduler_registers_jobs():
    from core.scheduler import TICKS

    scheduler = create_scheduler()
    ids = {job.id for job in scheduler.get_jobs()}
    # P2 起多了登录态巡检（默认每 10 分钟，见 XHS_LOGIN_HEALTH_INTERVAL_MINUTES）；
    # P3 起多了视频渲染任务轮询（每分钟）；P4 补齐了采集 / 生成 / 重试 / 复盘四条；
    # P12 多了确认闸门巡检（每分钟，推确认卡 / 补提醒 / TTL 超时驳回）
    assert ids == {
        "tick_scheduled_publish",
        "tick_confirm_gate",
        "tick_metrics",
        "tick_login_health",
        "tick_render_jobs",
        "tick_sourcing",
        "tick_generate",
        "tick_retry_sweep",
        "tick_insights",
    }
    # 注册表与实际注册的 job 必须一一对应：`POST /dev/tick/{name}` 走的是同一份表，
    # 少注册一个就会出现"手动能跑、定时不跑"（或反过来）的鬼现象
    assert ids == {f"tick_{name}" for name in TICKS}


def test_tick_login_health_marks_accounts():
    """巡检把 FakePublisher 的 health() 结果落到 Account 状态机上。"""
    from publishers.base import FakePublisher
    from publishers.registry import register

    with db.session_scope() as session:
        make_account(session, account_id="acc-xhs", platform="xhs")

    register("xhs", lambda account_id, **kw: FakePublisher(account_id, health_status="degraded"))
    stats = tick_login_health(platforms=["xhs"])

    assert stats == {"checked": 1, "degraded": 1}
    with db.session_scope() as session:
        assert session.scalars(select(Account.status)).one() == "degraded"


def test_scheduler_can_actually_start():
    scheduler = create_scheduler(start=True)
    try:
        assert scheduler.running
        # interval 触发器首次执行在 now+interval，启动本身不会立刻跑 job
        assert all(job.next_run_time is not None for job in scheduler.get_jobs())
    finally:
        scheduler.shutdown(wait=False)


# --------------------------------------------------- 视频渲染任务轮询 (P3)

MPT_BASE = "http://mpt.test:8080"
SAMPLE_VIDEO = Path("tests/fixtures/video/sample.mp4")


def _seed_render_job(*, state: str = "running", item_status: str = "draft") -> tuple[str, str]:
    """造一条"内容已入库但还缺成片"的现场，返回 ``(job_id, item_id)``。"""
    from core.models import RenderJob, new_id
    from publishers.base import ContentBundle, MediaAsset

    with db.session_scope() as session:
        account = make_account(session, account_id="douyin-demo-01", platform="douyin")
        item_id = new_id("itm")
        bundle = ContentBundle(
            id=item_id,
            account_id=account.id,
            platform="douyin",
            title="通勤一小时，一年亏掉十五万",
            body_markdown="通勤一小时的人，一年亏掉十五万。\n\n#通勤 #时间管理",
            media=[MediaAsset(path="data/demo/cover.png", kind="image", cover=True)],
            tags=["通勤", "时间管理"],
        )
        session.add(
            ContentItem(
                id=item_id,
                account_id=account.id,
                status=item_status,
                bundle_json=bundle.model_dump(mode="json"),
            )
        )
        job = RenderJob(
            id=new_id("rj"),
            content_item_id=item_id,
            provider="mpt",
            task_id="t1",
            state=state,
            progress=40,
            result_paths=[],
            attempts=1,
            meta={},
        )
        session.add(job)
        session.flush()
        return job.id, item_id


def _mpt_task(**kwargs):
    from tests.p3_helpers import ok_envelope, task_payload

    return httpx.Response(200, json=ok_envelope(task_payload("t1", **kwargs)))


@respx.mock
def test_tick_render_jobs_attaches_finished_video(tmp_path, monkeypatch):
    """渲染完成后把成片补挂回内容包——人不用重跑整条生成链。"""
    from generation import video_pipeline
    from generation.mpt_client import API_PREFIX, MptClient

    monkeypatch.setattr(video_pipeline, "DEFAULT_MEDIA_ROOT", tmp_path)
    job_id, item_id = _seed_render_job()
    respx.get(f"{MPT_BASE}{API_PREFIX}/tasks/t1").mock(
        return_value=_mpt_task(state=1, progress=100, videos=["/tasks/t1/final-1.mp4"])
    )
    respx.get(f"{MPT_BASE}{API_PREFIX}/download/t1/final-1.mp4").mock(
        return_value=httpx.Response(200, content=SAMPLE_VIDEO.read_bytes())
    )

    stats = tick_render_jobs(client=MptClient(MPT_BASE, timeout=1.0))
    assert stats == {
        "scanned": 1,
        "done": 1,
        "failed": 0,
        "lost": 0,
        "running": 0,
        "attached": 1,
    }

    from core.models import RenderJob

    with db.session_scope() as session:
        job = session.get(RenderJob, job_id)
        assert job.state == "done" and job.progress == 100
        assert job.result_paths == [str(tmp_path / item_id / "video.mp4")]
        item = session.get(ContentItem, item_id)
        media = item.bundle_json["media"]
        assert media[0]["kind"] == "video"
        assert item.bundle_json["platform_extra"]["duration_s"] == 2.0


@respx.mock
def test_tick_render_jobs_marks_failure_and_keeps_running(tmp_path, monkeypatch):
    from core.models import RenderJob
    from generation import video_pipeline
    from generation.mpt_client import API_PREFIX, MptClient

    monkeypatch.setattr(video_pipeline, "DEFAULT_MEDIA_ROOT", tmp_path)
    job_id, _ = _seed_render_job()
    respx.get(f"{MPT_BASE}{API_PREFIX}/tasks/t1").mock(
        return_value=_mpt_task(state=-1, progress=55, failed_stage="audio", error="tts down")
    )
    stats = tick_render_jobs(client=MptClient(MPT_BASE, timeout=1.0))
    assert stats["failed"] == 1 and stats["attached"] == 0
    with db.session_scope() as session:
        job = session.get(RenderJob, job_id)
        assert job.state == "failed"
        assert job.meta["failed_stage"] == "audio"
        assert "tts down" in job.last_error


@respx.mock
def test_tick_render_jobs_marks_lost_task(tmp_path, monkeypatch):
    """sidecar 重启后任务表就没了：标 lost，重提交交给生成链（要过预算闸门）。"""
    from core.models import RenderJob
    from generation import video_pipeline
    from generation.mpt_client import API_PREFIX, MptClient

    monkeypatch.setattr(video_pipeline, "DEFAULT_MEDIA_ROOT", tmp_path)
    job_id, _ = _seed_render_job()
    respx.get(f"{MPT_BASE}{API_PREFIX}/tasks/t1").mock(
        return_value=httpx.Response(404, json={"status": 404, "message": "task not found"})
    )
    assert tick_render_jobs(client=MptClient(MPT_BASE, timeout=1.0))["lost"] == 1
    with db.session_scope() as session:
        assert session.get(RenderJob, job_id).state == "lost"


@respx.mock
def test_tick_render_jobs_does_not_touch_approved_content(tmp_path, monkeypatch):
    """批准之后再改内容，人看过的和发出去的就不是一份了。"""
    from generation import video_pipeline
    from generation.mpt_client import API_PREFIX, MptClient

    monkeypatch.setattr(video_pipeline, "DEFAULT_MEDIA_ROOT", tmp_path)
    _, item_id = _seed_render_job(item_status="approved")
    respx.get(f"{MPT_BASE}{API_PREFIX}/tasks/t1").mock(
        return_value=_mpt_task(state=1, progress=100, videos=["/tasks/t1/final-1.mp4"])
    )
    respx.get(f"{MPT_BASE}{API_PREFIX}/download/t1/final-1.mp4").mock(
        return_value=httpx.Response(200, content=SAMPLE_VIDEO.read_bytes())
    )
    stats = tick_render_jobs(client=MptClient(MPT_BASE, timeout=1.0))
    assert stats["done"] == 1 and stats["attached"] == 0
    with db.session_scope() as session:
        item = session.get(ContentItem, item_id)
        assert all(m["kind"] != "video" for m in item.bundle_json["media"])


@respx.mock
def test_tick_render_jobs_skips_settled_jobs(tmp_path, monkeypatch):
    """done / failed / lost 的任务不再轮询，免得白打 HTTP。"""
    from generation import video_pipeline
    from generation.mpt_client import MptClient

    monkeypatch.setattr(video_pipeline, "DEFAULT_MEDIA_ROOT", tmp_path)
    _seed_render_job(state="done")
    # 没注册任何路由：真发请求会直接失败
    assert tick_render_jobs(client=MptClient(MPT_BASE, timeout=1.0))["scanned"] == 0
