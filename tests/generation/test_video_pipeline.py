"""抖音 bundle 组装 + RenderJob 持久化 + 预算 + 超时/丢任务。全部离线。

MPT 用 respx 打桩；轮询注入假 sleep 与假时钟，所以整组用例是瞬时的。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select

from core.budget import BudgetExhausted, BudgetGuard, CostKind
from core.models import ContentItem, RenderJob, RenderJobState
from generation.mpt_client import API_PREFIX, MptClient
from generation.video_pipeline import (
    MAX_RENDER_ATTEMPTS,
    VIDEO_FILENAME,
    RenderFailed,
    VideoGenerationOptions,
    attach_video_to_item,
    build_video_params,
    estimate_render_seconds,
    generate_douyin_bundle,
)
from publishers.base import ContentBundle, MediaAsset
from review.inspect import inspect
from sourcing.base import RawTopic
from tests.conftest import make_account
from tests.p3_helpers import cover_screenshotter, douyin_llm, ok_envelope, task_payload

BASE = "http://mpt.test:8080"
SAMPLE = Path("tests/fixtures/video/sample.mp4")


class FakeClock:
    """单调时钟替身：每次 ``sleep`` 直接把时间往前推，轮询循环瞬间跑完。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def offline_options(tmp_path) -> VideoGenerationOptions:
    """不碰 MPT、不碰 chromium 的一套选项。"""
    return VideoGenerationOptions(
        media_root=tmp_path / "media",
        skip_render=True,
        screenshotter=cover_screenshotter([]),
    )


@pytest.fixture
def render_budget(session) -> BudgetGuard:
    """conftest 把 DAILY_RENDER_SECONDS_BUDGET 压到 100 秒（够测闸门，不够测正常路径）。

    正常渲染路径的用例显式给一个宽松的闸门；专门测闸门的用例自己再收紧。
    """
    return BudgetGuard(session, render_seconds_budget=3600)


def _mpt_options(tmp_path, clock: FakeClock, **kwargs) -> VideoGenerationOptions:
    defaults: dict = {
        "media_root": tmp_path / "media",
        "screenshotter": cover_screenshotter([]),
        "client": MptClient(BASE, timeout=1.0, download_timeout=1.0),
        "poll_interval": 5.0,
        "render_timeout": 60.0,
        "sleeper": clock.sleep,
        "clock": clock,
    }
    return VideoGenerationOptions(**{**defaults, **kwargs})


def _mock_render(*, states: list[dict], video: bytes | None = None) -> None:
    respx.post(f"{BASE}{API_PREFIX}/videos").mock(
        return_value=httpx.Response(200, json=ok_envelope({"task_id": "t1"}))
    )
    respx.get(f"{BASE}{API_PREFIX}/tasks/t1").mock(
        side_effect=[httpx.Response(200, json=ok_envelope(s)) for s in states]
    )
    if video is not None:
        respx.get(f"{BASE}{API_PREFIX}/download/t1/final-1.mp4").mock(
            return_value=httpx.Response(200, content=video)
        )


# ------------------------------------------------------------------ bundle


def test_bundle_satisfies_frozen_contract(offline_options) -> None:
    outcome = generate_douyin_bundle(
        RawTopic(source="douyin_hot_hub", title="通勤成本", url="https://x.test/1", score=0.8),
        None,
        llm=douyin_llm(),
        account_id="douyin-demo-01",
        options=offline_options,
    )
    bundle = outcome.bundle

    assert bundle.platform == "douyin"
    assert bundle.title and len(bundle.title) <= 30
    assert bundle.body_html is None  # 抖音没有富文本正文
    # 媒体：1 个成片 + 1 张 9:16 封面，封面标了 cover
    kinds = [(m.kind, m.cover) for m in bundle.media]
    assert kinds == [("video", False), ("image", True)]
    assert all(Path(m.path).is_file() for m in bundle.media)
    assert bundle.cover is not None and bundle.cover.kind == "image"
    # 话题同时进 tags 与正文（平台把话题算作文案的一部分）
    assert bundle.tags == outcome.draft.hashtags
    assert bundle.body_markdown.endswith("#" + bundle.tags[-1])

    extra = bundle.platform_extra
    assert extra["hashtags"] == bundle.tags
    assert extra["script"] == outcome.draft.script
    assert extra["hook"] == outcome.draft.hook
    assert extra["search_terms"] and all(t.isascii() for t in extra["search_terms"])
    assert extra["duration_s"] == pytest.approx(2.0)
    assert extra["render"]["skip_render"] is True
    assert extra["generated_by"].endswith("generate_douyin_bundle")


def test_bundle_roundtrips_through_json(offline_options) -> None:
    outcome = generate_douyin_bundle(
        "通勤成本", None, llm=douyin_llm(), account_id="douyin-demo-01", options=offline_options
    )
    restored = ContentBundle.model_validate(outcome.bundle.model_dump(mode="json"))
    assert restored == outcome.bundle


def test_skip_render_uses_sample_and_says_so(offline_options, tmp_path) -> None:
    outcome = generate_douyin_bundle(
        "通勤成本",
        None,
        llm=douyin_llm(),
        account_id="douyin-demo-01",
        content_id="itm_fixed",
        options=offline_options,
    )
    assert outcome.video_path == tmp_path / "media" / "itm_fixed" / VIDEO_FILENAME
    assert outcome.video_path.read_bytes() == SAMPLE.read_bytes()
    # 不能让人把样本片当成真实产出
    assert any("不是**真实成片" in w for w in outcome.warnings)


def test_missing_sample_degrades_instead_of_crashing(tmp_path) -> None:
    outcome = generate_douyin_bundle(
        "通勤成本",
        None,
        llm=douyin_llm(),
        account_id="douyin-demo-01",
        options=VideoGenerationOptions(
            media_root=tmp_path,
            skip_render=True,
            sample_video=tmp_path / "nope.mp4",
            make_cover=False,
        ),
    )
    assert outcome.video_path is None
    report = inspect(outcome.bundle)
    assert {f.rule for f in report.blocking} >= {
        "inspect.douyin.video.missing",
        "inspect.douyin.cover.missing",
    }


def test_pipeline_requires_account_id() -> None:
    with pytest.raises(ValueError, match="account 或 account_id"):
        generate_douyin_bundle("x", None, llm=douyin_llm())


def test_build_video_params_feeds_claude_script() -> None:
    """不用 MPT 内置 LLM：script 非空时上游跳过自己的写稿步骤。"""
    outcome = generate_douyin_bundle(
        "通勤成本",
        None,
        llm=douyin_llm(),
        account_id="douyin-demo-01",
        options=VideoGenerationOptions(skip_render=True, make_cover=False),
    )
    params = build_video_params(outcome.draft, VideoGenerationOptions())
    assert params.video_script == outcome.draft.script
    assert params.video_terms == outcome.draft.search_terms
    assert params.video_aspect == "9:16"
    assert params.subtitle_enabled is True
    assert params.video_count == 1


# --------------------------------------------------------- 真渲染（打桩）


@respx.mock
def test_render_persists_job_and_downloads(session, tmp_path, render_budget) -> None:
    clock = FakeClock()
    _mock_render(
        states=[
            task_payload("t1", state=4, progress=20),
            task_payload("t1", state=1, progress=100, videos=["/tasks/t1/final-1.mp4"]),
        ],
        video=SAMPLE.read_bytes(),
    )
    make_account(session, account_id="douyin-demo-01", platform="douyin")
    outcome = generate_douyin_bundle(
        "通勤成本",
        None,
        llm=douyin_llm(),
        session=session,
        account_id="douyin-demo-01",
        content_id="itm_r1",
        options=_mpt_options(tmp_path, clock),
        budget=render_budget,
    )

    assert outcome.task_id == "t1"
    assert outcome.video_path is not None and outcome.video_path.is_file()
    assert outcome.duration_s == pytest.approx(2.0)

    job = session.get(RenderJob, outcome.render_job_id)
    assert job is not None
    assert job.state == RenderJobState.DONE
    assert job.progress == 100
    assert job.task_id == "t1"
    assert job.attempts == 1
    assert job.result_paths == [str(outcome.video_path)]
    assert job.content_item_id == "itm_r1"
    # meta 只放观测字段，不放凭据
    assert job.meta["aspect"] == "9:16"
    assert "api_key" not in job.meta


@respx.mock
def test_render_failure_degrades_to_bundle_without_video(session, tmp_path, render_budget) -> None:
    clock = FakeClock()
    _mock_render(
        states=[
            task_payload("t1", state=-1, progress=50, failed_stage="materials", error="no clips")
        ]
    )
    outcome = generate_douyin_bundle(
        "通勤成本",
        None,
        llm=douyin_llm(),
        session=session,
        account_id="douyin-demo-01",
        options=_mpt_options(tmp_path, clock),
        budget=render_budget,
    )
    assert outcome.video_path is None
    assert any("materials" in w for w in outcome.warnings)
    job = session.get(RenderJob, outcome.render_job_id)
    assert job.state == RenderJobState.FAILED
    assert job.meta["failed_stage"] == "materials"
    # 文案还在，人工队列里看到的是"缺成片"而不是 500
    assert outcome.bundle.title
    assert any(f.rule == "inspect.douyin.video.missing" for f in inspect(outcome.bundle).blocking)


@respx.mock
def test_render_timeout_keeps_job_running_for_scheduler(session, tmp_path, render_budget) -> None:
    clock = FakeClock()
    respx.post(f"{BASE}{API_PREFIX}/videos").mock(
        return_value=httpx.Response(200, json=ok_envelope({"task_id": "t1"}))
    )
    respx.get(f"{BASE}{API_PREFIX}/tasks/t1").mock(
        return_value=httpx.Response(200, json=ok_envelope(task_payload("t1", state=4, progress=40)))
    )
    outcome = generate_douyin_bundle(
        "通勤成本",
        None,
        llm=douyin_llm(),
        session=session,
        account_id="douyin-demo-01",
        options=_mpt_options(tmp_path, clock, render_timeout=20.0),
        budget=render_budget,
    )
    assert outcome.video_path is None
    assert any("渲染超过" in w for w in outcome.warnings)
    job = session.get(RenderJob, outcome.render_job_id)
    # 关键：留在 running，tick_render_jobs 才会继续跟
    assert job.state == RenderJobState.RUNNING
    assert job.progress == 40
    assert "超时" in (job.last_error or "")


@respx.mock
def test_lost_task_is_resubmitted_once(session, tmp_path, render_budget) -> None:
    """sidecar 重启导致 404：原样重提交一次，第二次成功。"""
    clock = FakeClock()
    respx.post(f"{BASE}{API_PREFIX}/videos").mock(
        side_effect=[
            httpx.Response(200, json=ok_envelope({"task_id": "t1"})),
            httpx.Response(200, json=ok_envelope({"task_id": "t2"})),
        ]
    )
    respx.get(f"{BASE}{API_PREFIX}/tasks/t1").mock(
        return_value=httpx.Response(404, json={"status": 404, "message": "task not found"})
    )
    respx.get(f"{BASE}{API_PREFIX}/tasks/t2").mock(
        return_value=httpx.Response(
            200,
            json=ok_envelope(
                task_payload("t2", state=1, progress=100, videos=["/tasks/t2/final-1.mp4"])
            ),
        )
    )
    respx.get(f"{BASE}{API_PREFIX}/download/t2/final-1.mp4").mock(
        return_value=httpx.Response(200, content=SAMPLE.read_bytes())
    )
    outcome = generate_douyin_bundle(
        "通勤成本",
        None,
        llm=douyin_llm(),
        session=session,
        account_id="douyin-demo-01",
        options=_mpt_options(tmp_path, clock),
        budget=render_budget,
    )
    job = session.get(RenderJob, outcome.render_job_id)
    assert job.task_id == "t2"
    assert job.attempts == MAX_RENDER_ATTEMPTS
    assert job.state == RenderJobState.DONE
    assert outcome.video_path is not None


@respx.mock
def test_lost_twice_gives_up(session, tmp_path, render_budget) -> None:
    clock = FakeClock()
    respx.post(f"{BASE}{API_PREFIX}/videos").mock(
        return_value=httpx.Response(200, json=ok_envelope({"task_id": "t1"}))
    )
    respx.get(f"{BASE}{API_PREFIX}/tasks/t1").mock(
        return_value=httpx.Response(404, json={"status": 404, "message": "task not found"})
    )
    outcome = generate_douyin_bundle(
        "通勤成本",
        None,
        llm=douyin_llm(),
        session=session,
        account_id="douyin-demo-01",
        options=_mpt_options(tmp_path, clock),
        budget=render_budget,
    )
    assert outcome.video_path is None
    assert any("重提交" in w for w in outcome.warnings)
    job = session.get(RenderJob, outcome.render_job_id)
    assert job.attempts == MAX_RENDER_ATTEMPTS


@respx.mock
def test_complete_without_outputs_is_a_failure(session, tmp_path, render_budget) -> None:
    clock = FakeClock()
    _mock_render(states=[task_payload("t1", state=1, progress=100)])
    outcome = generate_douyin_bundle(
        "通勤成本",
        None,
        llm=douyin_llm(),
        session=session,
        account_id="douyin-demo-01",
        options=_mpt_options(tmp_path, clock),
        budget=render_budget,
    )
    assert outcome.video_path is None
    assert any("没有成片引用" in w for w in outcome.warnings)


# ------------------------------------------------------------------ 预算


def test_estimate_has_a_floor() -> None:
    class _Draft:
        estimated_seconds = 1.0

    assert estimate_render_seconds(_Draft()) >= 60.0  # type: ignore[arg-type]


@respx.mock
def test_render_seconds_are_charged(session, tmp_path, render_budget) -> None:
    clock = FakeClock()
    _mock_render(
        states=[task_payload("t1", state=1, progress=100, videos=["/tasks/t1/final-1.mp4"])],
        video=SAMPLE.read_bytes(),
    )
    guard = render_budget
    before = guard.used(CostKind.RENDER_SECONDS)
    options = _mpt_options(tmp_path, clock)
    # 让假时钟在轮询里走掉一段时间，好让记账有非零值
    options.poll_interval = 7.0
    respx.get(f"{BASE}{API_PREFIX}/tasks/t1").mock(
        side_effect=[
            httpx.Response(200, json=ok_envelope(task_payload("t1", state=4, progress=10))),
            httpx.Response(
                200,
                json=ok_envelope(
                    task_payload("t1", state=1, progress=100, videos=["/tasks/t1/final-1.mp4"])
                ),
            ),
        ]
    )
    generate_douyin_bundle(
        "通勤成本",
        None,
        llm=douyin_llm(),
        session=session,
        account_id="douyin-demo-01",
        options=options,
        budget=guard,
    )
    assert guard.used(CostKind.RENDER_SECONDS) == pytest.approx(before + 7.0)


@respx.mock
def test_over_budget_does_not_submit(session, tmp_path) -> None:
    """超预算就**不提交**：MPT 一开跑就是几分钟 CPU 与素材源配额。"""
    respx.post(f"{BASE}{API_PREFIX}/videos").mock(
        return_value=httpx.Response(200, json=ok_envelope({"task_id": "never"}))
    )
    clock = FakeClock()
    guard = BudgetGuard(session, render_seconds_budget=5)
    with pytest.raises(BudgetExhausted, match="render_seconds"):
        generate_douyin_bundle(
            "通勤成本",
            None,
            llm=douyin_llm(),
            session=session,
            account_id="douyin-demo-01",
            options=_mpt_options(tmp_path, clock),
            budget=guard,
        )
    assert not respx.routes[0].called
    assert session.scalars(select(RenderJob)).all() == []


# --------------------------------------------------------------- 补挂成片


def _draft_item(session, *, status: str = "draft") -> ContentItem:
    account = make_account(session, account_id="douyin-demo-01", platform="douyin")
    bundle = ContentBundle(
        id="itm_attach",
        account_id=account.id,
        platform="douyin",
        title="通勤一小时，一年亏掉十五万",
        body_markdown="正文" * 20,
        media=[MediaAsset(path="data/demo/cover.png", kind="image", cover=True)],
        tags=["通勤"],
    )
    item = ContentItem(
        id="itm_attach",
        account_id=account.id,
        status=status,
        bundle_json=bundle.model_dump(mode="json"),
    )
    session.add(item)
    session.flush()
    return item


def test_attach_video_writes_media_and_duration(session) -> None:
    item = _draft_item(session)
    assert attach_video_to_item(session, item, SAMPLE) is True
    media = item.bundle_json["media"]
    assert media[0] == {"path": str(SAMPLE), "kind": "video", "cover": False}
    assert item.bundle_json["platform_extra"]["duration_s"] == pytest.approx(2.0)
    assert item.bundle_json["platform_extra"]["resolution"] == [720, 1280]
    # 幂等：再挂一次不会出现两个视频
    assert attach_video_to_item(session, item, SAMPLE) is False
    assert len(item.bundle_json["media"]) == 2


def test_attach_video_refuses_after_human_approval(session) -> None:
    """批准之后改内容会让"人看过的"和"发出去的"不是一份，审计链就断了。"""
    item = _draft_item(session, status="approved")
    assert attach_video_to_item(session, item, SAMPLE) is False
    assert all(m["kind"] != "video" for m in item.bundle_json["media"])


def test_render_failed_is_permanent() -> None:
    from publishers.base import PermanentError

    assert issubclass(RenderFailed, PermanentError)
