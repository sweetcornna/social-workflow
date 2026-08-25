"""MoneyPrinterTurbo 客户端：任务创建、轮询状态机、404 丢任务、下载。

全部用 respx 打桩，不联网。响应体形状对照上游 ``main`` 分支（见
``generation/mpt_client.py`` 的接口核对段），错误路径**没有 data 键**。
"""

from __future__ import annotations

import httpx
import pytest
import respx

from generation.mpt_client import (
    API_PREFIX,
    ASPECT_PORTRAIT,
    TASK_STATE_COMPLETE,
    TASK_STATE_FAILED,
    TASK_STATE_PROCESSING,
    MptClient,
    MptTaskLost,
    MptVideoParams,
    parse_task,
    redact,
)
from publishers.base import PermanentError, RetryableError
from tests.p3_helpers import error_envelope, ok_envelope, task_payload

BASE = "http://mpt.test:8080"


@pytest.fixture
def client() -> MptClient:
    return MptClient(BASE, timeout=1.0, download_timeout=2.0)


def _params(**kwargs) -> MptVideoParams:
    defaults = {
        "video_subject": "通勤成本",
        "video_script": "通勤一小时的人，一年亏掉十五万。",
        "video_terms": ["crowded morning train"],
    }
    return MptVideoParams(**{**defaults, **kwargs})


# ------------------------------------------------------------------ 参数


def test_params_default_to_portrait_and_drop_none_transition() -> None:
    payload = _params().as_payload()
    assert payload["video_aspect"] == ASPECT_PORTRAIT
    assert payload["subtitle_enabled"] is True
    # None 的过渡模式不发键：上游枚举里 none 是 Python None，发 null 会被 422
    assert "video_transition_mode" not in payload
    assert payload["video_script"].startswith("通勤一小时")


def test_params_reject_bad_values_locally() -> None:
    """本地先挡一道，比让上游回 HTTP 400 的 pydantic 错误列表友好。"""
    for kwargs, needle in (
        ({"video_subject": "  "}, "video_subject"),
        ({"video_aspect": "4:3"}, "video_aspect"),
        ({"video_source": "unsplash"}, "video_source"),
        ({"video_concat_mode": "shuffle"}, "video_concat_mode"),
        ({"video_transition_mode": "fade"}, "video_transition_mode"),
        ({"subtitle_position": "middle"}, "subtitle_position"),
        ({"video_clip_duration": 0}, "video_clip_duration"),
        ({"paragraph_number": 11}, "paragraph_number"),
    ):
        with pytest.raises(PermanentError, match=needle):
            _params(**kwargs).validate()


def test_portrait_resolution_is_1080x1920() -> None:
    assert _params().resolution == (1080, 1920)


# ------------------------------------------------------------------ 创建


@respx.mock
def test_create_video_returns_task_id(client: MptClient) -> None:
    route = respx.post(f"{BASE}{API_PREFIX}/videos").mock(
        return_value=httpx.Response(200, json=ok_envelope({"task_id": "abc-123"}))
    )
    assert client.create_video(_params()) == "abc-123"
    sent = route.calls[0].request
    assert b'"video_aspect":"9:16"' in sent.content.replace(b" ", b"")


@respx.mock
def test_create_video_queue_full_is_retryable(client: MptClient) -> None:
    """429 = 队列满，上游把刚建的任务记录也删了，只能整体重提交。"""
    respx.post(f"{BASE}{API_PREFIX}/videos").mock(
        return_value=httpx.Response(429, json=error_envelope(429, "task queue is full"))
    )
    with pytest.raises(RetryableError, match="队列已满"):
        client.create_video(_params())


@respx.mock
def test_validation_error_is_400_not_422(client: MptClient) -> None:
    """上游把 RequestValidationError 转成 400，客户端不能只认 422。"""
    respx.post(f"{BASE}{API_PREFIX}/videos").mock(
        return_value=httpx.Response(
            400,
            json={
                "status": 400,
                "data": [{"loc": ["body", "video_subject"], "msg": "field required"}],
                "message": "field required",
            },
        )
    )
    with pytest.raises(PermanentError, match="HTTP 400"):
        client.create_video(_params())


@respx.mock
def test_create_video_without_task_id_is_retryable(client: MptClient) -> None:
    respx.post(f"{BASE}{API_PREFIX}/videos").mock(
        return_value=httpx.Response(200, json=ok_envelope({"request_id": "x"}))
    )
    with pytest.raises(RetryableError, match="task_id"):
        client.create_video(_params())


def test_dry_run_does_not_call_network() -> None:
    dry = MptClient(BASE, dry_run=True)
    with respx.mock:  # 没注册任何路由：真发请求会直接抛 AllMockedAssertionError
        assert dry.create_video(_params()) == "dry-run-task"


# ------------------------------------------------------------------ 轮询


@respx.mock
def test_get_task_walks_the_state_machine(client: MptClient) -> None:
    respx.get(f"{BASE}{API_PREFIX}/tasks/t1").mock(
        side_effect=[
            httpx.Response(200, json=ok_envelope(task_payload("t1", state=4, progress=10))),
            httpx.Response(200, json=ok_envelope(task_payload("t1", state=4, progress=50))),
            httpx.Response(
                200,
                json=ok_envelope(
                    task_payload("t1", state=1, progress=100, videos=["/tasks/t1/final-1.mp4"])
                ),
            ),
        ]
    )
    first = client.get_task("t1")
    assert (first.state, first.progress, first.running) == (TASK_STATE_PROCESSING, 10, True)
    assert client.get_task("t1").progress == 50
    done = client.get_task("t1")
    assert done.done and done.outputs == ["/tasks/t1/final-1.mp4"]
    assert "完成" in done.summary


@respx.mock
def test_failed_task_carries_stage_and_error(client: MptClient) -> None:
    respx.get(f"{BASE}{API_PREFIX}/tasks/t2").mock(
        return_value=httpx.Response(
            200,
            json=ok_envelope(
                task_payload(
                    "t2", state=-1, progress=50, failed_stage="materials", error="no material"
                )
            ),
        )
    )
    task = client.get_task("t2")
    assert task.state == TASK_STATE_FAILED
    assert task.failed and not task.running
    assert task.failed_stage == "materials"
    assert "materials" in task.summary


@respx.mock
def test_unknown_task_raises_task_lost(client: MptClient) -> None:
    """sidecar 重启后任务表就没了，这是可重提交的情况，不是内容有问题。"""
    respx.get(f"{BASE}{API_PREFIX}/tasks/gone").mock(
        return_value=httpx.Response(404, json=error_envelope(404, "req-1: task not found"))
    )
    with pytest.raises(MptTaskLost, match="task not found"):
        client.get_task("gone")


@respx.mock
def test_timeout_and_connect_error_are_retryable(client: MptClient) -> None:
    respx.get(f"{BASE}{API_PREFIX}/tasks/t3").mock(side_effect=httpx.ConnectTimeout("slow"))
    with pytest.raises(RetryableError, match="超时"):
        client.get_task("t3")
    respx.get(f"{BASE}{API_PREFIX}/tasks/t4").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(RetryableError, match="连不上"):
        client.get_task("t4")


@respx.mock
def test_server_error_is_retryable(client: MptClient) -> None:
    respx.get(f"{BASE}{API_PREFIX}/tasks/t5").mock(
        return_value=httpx.Response(500, json=error_envelope(500, "boom"))
    )
    with pytest.raises(RetryableError, match="HTTP 500"):
        client.get_task("t5")


def test_parse_task_tolerates_garbage() -> None:
    """状态读不出来时按"处理中"对待：继续轮询比误判成失败安全。"""
    task = parse_task({"task_id": "x", "state": "?", "progress": None})
    assert task.state == TASK_STATE_PROCESSING and task.running
    assert parse_task({}, fallback_id="y").task_id == "y"
    # videos 为 null 时退到 combined_videos
    assert parse_task({"combined_videos": ["a.mp4"], "state": 1}).outputs == ["a.mp4"]


@respx.mock
def test_health_uses_task_listing(client: MptClient) -> None:
    """上游没有 /health 路由，用只读的任务列表探活。"""
    route = respx.get(f"{BASE}{API_PREFIX}/tasks").mock(
        return_value=httpx.Response(200, json=ok_envelope({"tasks": [], "total": 3}))
    )
    assert client.health() == {"ok": True, "endpoint": BASE, "total_tasks": 3}
    assert route.calls[0].request.url.params["page_size"] == "1"


# ------------------------------------------------------------------ 下载


@pytest.mark.parametrize(
    "ref",
    [
        "/tasks/t1/final-1.mp4",
        "t1/final-1.mp4",
        f"{BASE}/tasks/t1/final-1.mp4",
        f"{BASE}{API_PREFIX}/download/t1/final-1.mp4",
    ],
)
def test_download_url_normalizes_every_shape(client: MptClient, ref: str) -> None:
    """endpoint 留空时上游给的是根相对路径，配了才是绝对 URL，两种都要吃下。"""
    assert client.download_url(ref) == f"{BASE}{API_PREFIX}/download/t1/final-1.mp4"


def test_download_url_keeps_foreign_hosts(client: MptClient) -> None:
    assert client.download_url("https://cdn.test/x.mp4") == "https://cdn.test/x.mp4"


def test_download_url_rejects_traversal(client: MptClient) -> None:
    with pytest.raises(PermanentError, match=r"\.\."):
        client.download_url("/tasks/../../etc/passwd")


@respx.mock
def test_download_streams_to_disk(client: MptClient, tmp_path) -> None:
    respx.get(f"{BASE}{API_PREFIX}/download/t1/final-1.mp4").mock(
        return_value=httpx.Response(200, content=b"\x00\x01mp4bytes")
    )
    out = client.download("/tasks/t1/final-1.mp4", tmp_path / "sub" / "video.mp4")
    assert out.read_bytes() == b"\x00\x01mp4bytes"
    # 中间产物不留下
    assert not (tmp_path / "sub" / "video.mp4.part").exists()


@respx.mock
def test_download_empty_file_is_retryable(client: MptClient, tmp_path) -> None:
    respx.get(f"{BASE}{API_PREFIX}/download/t1/final-1.mp4").mock(
        return_value=httpx.Response(200, content=b"")
    )
    with pytest.raises(RetryableError, match="空文件"):
        client.download("/tasks/t1/final-1.mp4", tmp_path / "video.mp4")
    assert not (tmp_path / "video.mp4").exists()


@respx.mock
def test_download_404_is_task_lost(client: MptClient, tmp_path) -> None:
    respx.get(f"{BASE}{API_PREFIX}/download/t1/final-1.mp4").mock(
        return_value=httpx.Response(404, json=error_envelope(404, "file not found"))
    )
    with pytest.raises(MptTaskLost):
        client.download("/tasks/t1/final-1.mp4", tmp_path / "video.mp4")
    assert not (tmp_path / "video.mp4.part").exists()


# ------------------------------------------------------------------ 杂项


def test_missing_base_url_is_permanent() -> None:
    with pytest.raises(PermanentError, match="MPT_BASE_URL"):
        MptClient("")


def test_redact_hides_api_key() -> None:
    assert "sk-secret-value" not in redact('{"api_key": "sk-secret-value"}')
    assert TASK_STATE_COMPLETE == 1  # 常量没被改动（上游 const.py 逐字核对过）
