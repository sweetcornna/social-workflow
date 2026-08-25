"""MoneyPrinterTurbo（MIT）sidecar 的 httpx 薄封装。

设计原则
--------
- **sidecar 只作独立进程**：本模块只发 HTTP，绝不 import / 复制上游代码。
  MPT 自带 ffmpeg、素材下载、TTS 与字幕烧录，我们只用它的"合成"能力。
- **不用 MPT 内置 LLM**：脚本由 Claude 生成后经 ``video_script`` / ``video_terms``
  灌入（见 ``generation/video_script.py``），避免两套 LLM 配置与两份提示词。
- **错误 → 契约异常**：统一映射到 ``publishers.base`` 的 ``RetryableError`` /
  ``PermanentError``，调用方只认这两类（sidecar 没有"账号登录态"概念，
  所以不会产生 ``NeedsReloginError``）。
- **task 状态易失**：MPT 的任务表在进程内存里（可选 Redis），容器重启就没了。
  所以 ``task_id`` 必须由 core 侧持久化（``core.models.RenderJob``），
  查不到时本模块抛 :class:`MptTaskLost`，由上层决定重提交还是放弃。

接口核对（对照上游 ``main`` 分支源码，2026-08-16，HEAD ``1f9f19c``）
--------------------------------------------------------------------
路由前缀在 ``app/controllers/v1/base.py:new_router`` 里写死为 ``/api/v1``：

- ``POST /api/v1/videos``  body = ``VideoParams``（``TaskVideoRequest`` 就是它本身）
  → HTTP **200**，``{"status":200,"message":"success","data":{"task_id":"<uuid4>"}}``。
  handler 里塞进 data 的 ``request_id`` / ``params`` 会被 ``TaskResponseData`` 丢掉。
  队列满时返回 **429** 且任务记录被删除（不要去轮询那个 id，直接重提交）。
- ``GET /api/v1/tasks/{task_id}`` → ``data`` 为 ``TaskStatusData``
  （``extra="allow"``，除下列字段外还会透传 ``script`` / ``terms`` / ``audio_duration``
  / ``materials`` / ``warnings``）::

      task_id, state, progress, videos, combined_videos, failed_stage, error

  未知 id → **404**，body ``{"status":404,"message":"<request_id>: task not found"}``。
- ``GET /api/v1/download/{file_path:path}``：``file_path`` 相对 ``storage/tasks``，
  形如 ``<task_id>/final-1.mp4``；返回 ``FileResponse`` + attachment 头。
- ``GET /api/v1/stream/{file_path:path}``：**永远返回 206**（无 Range 头也是），
  本模块不用它——落盘走 ``download`` 更直白。

任务状态常量来自 ``app/models/const.py``：``-1`` 失败 / ``1`` 完成 / ``4`` 处理中。
**没有排队态**：入队即 ``4``。因此轮询要看 ``state``，不能看 ``progress``
（失败时 progress 停在失败点而不是归零）。

响应封装的两个坑（``app/utils/utils.py`` + ``app/asgi.py``）：

1. 成功路径经 ``response_model`` 序列化，一定有 ``status`` / ``message`` / ``data``；
   **错误路径绕过 response_model**，body 里**没有 ``data`` 键**。
2. 请求体校验失败返回 **HTTP 400**（不是 FastAPI 默认的 422），
   body 为 ``{"status":400,"data":[pydantic errors],"message":"field required"}``。

**未核实**：

- 上游**没有**健康检查路由。:meth:`MptClient.health` 用只读的
  ``GET /api/v1/tasks?page=1&page_size=1`` 代替（不产生任务、不占队列）。
- 鉴权默认关闭（``video.py`` 里 ``Depends(base.verify_token)`` 被注释掉了）。
  若部署方自行打开，凭据走 ``x-api-key`` 头，本模块支持传 ``api_key``。
- 生成的视频路径受 sidecar 侧 ``[app] endpoint`` 影响：留空时 ``videos`` 是
  **根相对路径** ``/tasks/<id>/final-1.mp4``（由 StaticFiles 挂载提供，不带 ``/api/v1``
  前缀）；配了 endpoint 才是绝对 URL。:meth:`MptClient.download` 两种都吃。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from publishers.base import PermanentError, RetryableError

logger = logging.getLogger("social_workflow.generation.mpt")

#: 上游 API 前缀（``app/controllers/v1/base.py:new_router``）
API_PREFIX = "/api/v1"
#: 上游默认监听端口（``config.example.toml`` 顶层 ``listen_port``）
DEFAULT_PORT = 8080

# --- 任务状态（app/models/const.py，逐字核对）---------------------------------
TASK_STATE_FAILED = -1
TASK_STATE_COMPLETE = 1
TASK_STATE_PROCESSING = 4
#: ``0/2/3`` 上游未使用；这里只是把"已知值"列全，便于日志里区分未知态
KNOWN_TASK_STATES = (TASK_STATE_FAILED, TASK_STATE_COMPLETE, TASK_STATE_PROCESSING)

#: 上游会写进 ``failed_stage`` 的全部取值（``app/services/task.py``）。
#: 注意没有 ``subtitle``——字幕出问题不致命，只会进 ``warnings``。
FAILED_STAGES = ("preflight", "script", "terms", "audio", "materials", "video", "pipeline")

# --- 参数取值（app/models/schema.py 的枚举）-----------------------------------
ASPECT_PORTRAIT = "9:16"
ASPECT_LANDSCAPE = "16:9"
ASPECT_SQUARE = "1:1"
#: 各比例对应的输出分辨率（上游 ``VideoAspect.to_resolution``）
ASPECT_RESOLUTIONS: dict[str, tuple[int, int]] = {
    ASPECT_LANDSCAPE: (1920, 1080),
    ASPECT_PORTRAIT: (1080, 1920),
    ASPECT_SQUARE: (1080, 1080),
}
CONCAT_MODES = ("random", "sequential")
#: ``VideoTransitionMode`` 的字符串取值（``none`` 是 Python ``None``，不是字符串）
TRANSITION_MODES = (
    "Shuffle",
    "FadeIn",
    "FadeOut",
    "SlideIn",
    "SlideOut",
    "ZoomIn",
    "ZoomOut",
)
#: 素材源。``local`` 需要 sidecar 侧配 ``material_directory``，本项目不用
VIDEO_SOURCES = ("pexels", "pixabay", "coverr", "local")
SUBTITLE_POSITIONS = ("top", "bottom", "center", "custom")

#: 上游 ``storage/tasks`` 在 URL 空间里的挂载点
TASKS_URL_PREFIX = "/tasks/"

_API_KEY_HEADER = "x-api-key"
_HEX_KEY = re.compile(r"(?i)(\"?(?:api_?key|token)\"?\s*[:=]\s*\"?)[A-Za-z0-9._\-]{8,}")


class MptTaskLost(PermanentError):
    """``GET /tasks/{id}`` 返回 404：sidecar 重启后任务表丢了。

    分成独立异常是因为它的处置方式和别的永久失败不同——**可以原样重提交一次**
    （内容与参数都还在我们这边），而不是直接进死信。
    """


def redact(text: str) -> str:
    """日志脱敏：把可能出现的 api_key / token 抹掉。"""
    return _HEX_KEY.sub(r"\1***", text)


# ------------------------------------------------------------------ 请求参数


@dataclass
class MptVideoParams:
    """``POST /api/v1/videos`` 的请求体（上游 ``VideoParams`` 的**子集**）。

    刻意只覆盖本项目会设的字段：上游还有两个默认值是**从 sidecar 的 config.toml
    读出来的**（``subtitle_position`` / ``custom_position``），把它们当客户端常量
    会在换部署时静默漂移。没列出的字段一律不发，用 sidecar 侧默认值。

    ``video_script`` 非空时上游**跳过内置 LLM 写稿**，直接用它——这正是
    "脚本由 Claude 生成后灌入"的落点（见模块 docstring）。
    """

    #: 唯一必填字段。即便给了 script 也要给它（上游用它做日志与素材兜底检索）
    video_subject: str
    video_script: str = ""
    #: 素材检索词，英文效果最好（Pexels/Pixabay 的中文召回很差）
    video_terms: list[str] = field(default_factory=list)
    video_aspect: str = ASPECT_PORTRAIT
    video_concat_mode: str = "random"
    video_transition_mode: str | None = None
    video_clip_duration: int = 3
    video_count: int = 1
    video_source: str = "pexels"
    video_language: str = ""
    #: 留空 = 用 sidecar 侧默认音色（edge-tts 的中文音色）
    voice_name: str = ""
    voice_rate: float = 1.0
    voice_volume: float = 1.0
    bgm_type: str = "random"
    bgm_volume: float = 0.2
    subtitle_enabled: bool = True
    subtitle_position: str = "bottom"
    font_size: int = 60
    text_fore_color: str = "#FFFFFF"
    stroke_color: str = "#000000"
    stroke_width: float = 1.5
    n_threads: int = 2
    paragraph_number: int = 1

    def validate(self) -> None:
        """本地先挡一道：上游校验失败回的是 HTTP 400，错误文案对人不友好。"""
        if not self.video_subject.strip():
            raise PermanentError("video_subject 不能为空（MPT 唯一必填字段）")
        if self.video_aspect not in ASPECT_RESOLUTIONS:
            raise PermanentError(
                f"video_aspect={self.video_aspect!r} 非法，允许 {sorted(ASPECT_RESOLUTIONS)}"
            )
        if self.video_concat_mode not in CONCAT_MODES:
            raise PermanentError(
                f"video_concat_mode={self.video_concat_mode!r} 非法，允许 {list(CONCAT_MODES)}"
            )
        if self.video_transition_mode is not None and (
            self.video_transition_mode not in TRANSITION_MODES
        ):
            raise PermanentError(
                f"video_transition_mode={self.video_transition_mode!r} 非法，"
                f"允许 {list(TRANSITION_MODES)} 或 None"
            )
        if self.video_source not in VIDEO_SOURCES:
            raise PermanentError(
                f"video_source={self.video_source!r} 非法，允许 {list(VIDEO_SOURCES)}"
            )
        if self.subtitle_position not in SUBTITLE_POSITIONS:
            raise PermanentError(
                f"subtitle_position={self.subtitle_position!r} 非法，"
                f"允许 {list(SUBTITLE_POSITIONS)}"
            )
        if self.video_clip_duration < 1:
            raise PermanentError("video_clip_duration 至少 1 秒")
        if self.video_count < 1:
            raise PermanentError("video_count 至少 1")
        if not 1 <= self.paragraph_number <= 10:
            raise PermanentError("paragraph_number 取值 1–10")

    @property
    def resolution(self) -> tuple[int, int]:
        return ASPECT_RESOLUTIONS[self.video_aspect]

    def as_payload(self) -> dict[str, Any]:
        """转成请求体。``video_transition_mode=None`` 时不发该键。"""
        payload = asdict(self)
        if payload.get("video_transition_mode") is None:
            payload.pop("video_transition_mode")
        payload["video_terms"] = list(self.video_terms)
        return payload


# ------------------------------------------------------------------ 任务状态


@dataclass(frozen=True)
class MptTask:
    """``GET /api/v1/tasks/{id}`` 的归一化结果。"""

    task_id: str
    state: int
    progress: int = 0
    videos: list[str] = field(default_factory=list)
    combined_videos: list[str] = field(default_factory=list)
    failed_stage: str = ""
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def done(self) -> bool:
        return self.state == TASK_STATE_COMPLETE

    @property
    def failed(self) -> bool:
        return self.state == TASK_STATE_FAILED

    @property
    def running(self) -> bool:
        """处理中。**未知状态也按处理中对待**——继续轮询比误判成失败安全。"""
        return not self.done and not self.failed

    @property
    def outputs(self) -> list[str]:
        """成片引用。优先 ``videos``（每条一个独立成片），退到 ``combined_videos``。

        ``video_count=1`` 时 ``videos`` 就是那一条最终片；上游把拼接前的中间产物
        放 ``combined_videos``，两者都给出时以 ``videos`` 为准。
        """
        return list(self.videos or self.combined_videos or [])

    @property
    def summary(self) -> str:
        if self.done:
            return f"完成（{len(self.outputs)} 个成片）"
        if self.failed:
            stage = f"，失败于 {self.failed_stage}" if self.failed_stage else ""
            return f"失败{stage}：{self.error or '（未给出原因）'}"
        return f"处理中 {self.progress}%"


def parse_task(payload: dict[str, Any], *, fallback_id: str = "") -> MptTask:
    """把 ``TaskStatusData`` 转成 :class:`MptTask`。字段缺失一律按空处理。"""

    def as_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item) for item in value if item]

    state_raw = payload.get("state")
    try:
        state = int(state_raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        # 读不出状态时按"处理中"对待：轮询会继续，超时兜底，不会卡死
        state = TASK_STATE_PROCESSING
    try:
        progress = int(payload.get("progress") or 0)
    except (TypeError, ValueError):
        progress = 0
    return MptTask(
        task_id=str(payload.get("task_id") or fallback_id),
        state=state,
        progress=progress,
        videos=as_list(payload.get("videos")),
        combined_videos=as_list(payload.get("combined_videos")),
        failed_stage=str(payload.get("failed_stage") or ""),
        error=str(payload.get("error") or ""),
        raw=payload,
    )


# ------------------------------------------------------------------- 客户端


class MptClient:
    """MoneyPrinterTurbo REST 客户端。

    ``dry_run=True`` 时 :meth:`create_video` 只记日志并返回一个假 task_id，
    不发请求——用于无 sidecar 环境下把上层链路跑通。
    """

    def __init__(
        self,
        base_url: str,
        *,
        http: httpx.Client | None = None,
        timeout: float = 30.0,
        download_timeout: float = 600.0,
        api_key: str = "",
        dry_run: bool = False,
    ) -> None:
        if not base_url:
            raise PermanentError(
                "未配置 MoneyPrinterTurbo 地址：请设置 MPT_BASE_URL（默认 http://localhost:8080）"
            )
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # 成片可能有几十 MB，下载单独给更长的超时
        self.download_timeout = download_timeout
        self.api_key = api_key
        self.dry_run = dry_run
        self._http = http
        self._own_http = http is None

    # -- 生命周期 ---------------------------------------------------------

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=self.timeout, follow_redirects=True)
        return self._http

    def close(self) -> None:
        if self._own_http and self._http is not None:
            self._http.close()
            self._http = None

    def __enter__(self) -> MptClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- 底层请求 ---------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers[_API_KEY_HEADER] = self.api_key
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        api: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """发一次请求，取出 ``{"status","message","data"}`` 里的 ``data``。"""
        url = self._url(path)
        logger.debug("mpt -> %s %s", method, url)
        try:
            resp = self.http.request(
                method,
                url,
                json=json_body,
                params=params,
                headers=self._headers(),
                timeout=timeout if timeout is not None else self.timeout,
            )
        except httpx.TimeoutException as exc:
            raise RetryableError(
                f"{api} 请求超时（{timeout or self.timeout}s）: {exc}",
                raw={"api": api, "endpoint": self.base_url},
            ) from exc
        except httpx.HTTPError as exc:
            raise RetryableError(
                f"{api} 连不上 MoneyPrinterTurbo {self.base_url}: {exc}"
                "（容器起了吗？`docker compose --profile video up` / MPT_BASE_URL 对吗？）",
                raw={"api": api, "endpoint": self.base_url},
            ) from exc

        body: Any = None
        try:
            body = resp.json()
        except ValueError:
            body = None
        logger.debug(
            "mpt <- %s %s %s",
            api,
            resp.status_code,
            redact(json.dumps(body, ensure_ascii=False)[:800])
            if body is not None
            else redact(resp.text[:300]),
        )
        if resp.status_code >= 400:
            self._raise_api_error(resp, body, api=api)
        if not isinstance(body, dict):
            raise RetryableError(
                f"{api} 返回的不是 JSON 对象：{redact(resp.text[:200])}", raw={"api": api}
            )
        return body.get("data")

    def _raise_api_error(self, resp: httpx.Response, body: Any, *, api: str) -> None:
        """按 HTTP 状态码分流。

        错误 body 没有 ``data`` 键（绕过了 response_model），信息只在 ``message`` 里；
        请求体校验失败是 **400**（不是 422），此时 ``data`` 反而有 pydantic 错误列表。
        """
        message = ""
        if isinstance(body, dict):
            message = str(body.get("message") or "")
            detail = body.get("data")
            if detail:
                message = f"{message} | {json.dumps(detail, ensure_ascii=False)[:300]}"
        if not message:
            message = redact(resp.text[:300]) or f"HTTP {resp.status_code}"
        raw = {"api": api, "status": resp.status_code, "endpoint": self.base_url}
        status = resp.status_code
        if status == 404:
            raise MptTaskLost(f"{api}: {redact(message)}", raw=raw)
        if status == 429:
            # 队列满：上游把刚建的任务记录也删了，不能去轮询那个 id，只能整体重提交
            raise RetryableError(
                f"{api}: MPT 任务队列已满（{redact(message)}）。"
                "调高 sidecar 的 max_queued_tasks，或稍后重试",
                raw=raw,
                retry_after=60.0,
            )
        if status in (400, 401, 403, 409, 416, 422):
            raise PermanentError(f"{api} 失败（HTTP {status}）: {redact(message)}", raw=raw)
        raise RetryableError(f"{api} 失败（HTTP {status}）: {redact(message)}", raw=raw)

    # -- 只读接口 ---------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """存活探测。

        上游**没有** ``/health`` 路由，这里用只读的任务列表代替：不建任务、
        不占队列，能通就说明 API 进程活着且路由前缀正确。
        """
        data = self._request(
            "GET",
            f"{API_PREFIX}/tasks",
            api="tasks.list",
            params={"page": 1, "page_size": 1},
        )
        payload = data if isinstance(data, dict) else {}
        return {
            "ok": True,
            "endpoint": self.base_url,
            "total_tasks": payload.get("total"),
        }

    def get_task(self, task_id: str) -> MptTask:
        """``GET /api/v1/tasks/{task_id}``。任务不存在抛 :class:`MptTaskLost`。"""
        if not task_id:
            raise PermanentError("task_id 为空")
        data = self._request("GET", f"{API_PREFIX}/tasks/{task_id}", api="tasks.get")
        payload = data if isinstance(data, dict) else {}
        return parse_task(payload, fallback_id=task_id)

    # -- 写操作 -----------------------------------------------------------

    def create_video(self, params: MptVideoParams) -> str:
        """``POST /api/v1/videos``：提交渲染任务，返回 ``task_id``。

        返回即入队（``state=4``），**不等渲染完成**——由调用方轮询
        :meth:`get_task`，并把 ``task_id`` 落到 ``core.models.RenderJob``。
        """
        params.validate()
        payload = params.as_payload()
        if self.dry_run:
            logger.info(
                "[dry_run] mpt create_video 跳过：subject=%r script=%d 字 terms=%s",
                params.video_subject,
                len(params.video_script),
                params.video_terms,
            )
            return "dry-run-task"
        data = self._request("POST", f"{API_PREFIX}/videos", api="videos.create", json_body=payload)
        task_id = str((data or {}).get("task_id") or "") if isinstance(data, dict) else ""
        if not task_id:
            raise RetryableError(
                "MPT 未返回 task_id（响应体形状与上游 TaskResponseData 不符）",
                raw={"api": "videos.create", "data": data},
            )
        logger.info(
            "MPT 任务已提交 task=%s subject=%r aspect=%s source=%s terms=%s",
            task_id,
            params.video_subject,
            params.video_aspect,
            params.video_source,
            params.video_terms,
        )
        return task_id

    # -- 下载 -------------------------------------------------------------

    def download_url(self, ref: str) -> str:
        """把 ``videos`` / ``combined_videos`` 里的引用翻译成可下载的绝对 URL。

        三种形态都要吃下：

        - ``http(s)://host/tasks/<id>/final-1.mp4`` —— sidecar 配了 ``[app] endpoint``
        - ``/tasks/<id>/final-1.mp4`` —— endpoint 留空（默认），根相对路径
        - ``<id>/final-1.mp4`` —— 调用方自己拼的相对路径

        统一走 ``GET /api/v1/download/<相对路径>``：这是文档化的 API 路由，
        比 StaticFiles 挂载点更稳（后者是上游的实现细节，改了不算破坏 API）。
        外部主机的绝对 URL 原样返回。
        """
        if not ref:
            raise PermanentError("下载引用为空")
        relative = ref
        if ref.startswith(("http://", "https://")):
            mine = urlsplit(self.base_url)
            theirs = urlsplit(ref)
            if (theirs.scheme, theirs.netloc) != (mine.scheme, mine.netloc):
                return ref  # 别人家的 CDN，原样走
            relative = theirs.path
        relative = relative.lstrip("/")
        if relative.startswith("tasks/"):
            relative = relative[len("tasks/") :]
        if relative.startswith("api/v1/download/"):
            relative = relative[len("api/v1/download/") :]
        if ".." in Path(relative).parts:
            raise PermanentError(f"下载路径非法（含 ..）：{ref}")
        return self._url(f"{API_PREFIX}/download/{relative}")

    def download(self, ref: str, dest: str | Path) -> Path:
        """把成片下载到 ``dest``。返回落盘路径。

        流式写入而不是整块读进内存：成片几十 MB 是常态，一条链路里还可能同时
        跑好几个账号。先写 ``.part`` 再改名，避免下载中断留下一个"看起来完整"的坏文件。
        """
        url = self.download_url(ref)
        target = Path(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + ".part")
        logger.info("下载成片 %s → %s", url, target)
        try:
            with self.http.stream(
                "GET", url, headers=self._headers(), timeout=self.download_timeout
            ) as resp:
                if resp.status_code >= 400:
                    resp.read()
                    body: Any = None
                    try:
                        body = resp.json()
                    except ValueError:
                        body = None
                    self._raise_api_error(resp, body, api="download")
                with temp.open("wb") as fh:
                    for chunk in resp.iter_bytes():
                        fh.write(chunk)
        except httpx.TimeoutException as exc:
            temp.unlink(missing_ok=True)
            raise RetryableError(
                f"下载成片超时（{self.download_timeout}s）: {exc}", raw={"url": url}
            ) from exc
        except httpx.HTTPError as exc:
            temp.unlink(missing_ok=True)
            raise RetryableError(f"下载成片失败: {exc}", raw={"url": url}) from exc
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        if temp.stat().st_size == 0:
            temp.unlink(missing_ok=True)
            raise RetryableError(f"下载到的成片是空文件: {url}", raw={"url": url})
        temp.replace(target)
        logger.info("成片已落盘 %s（%.1f MB）", target, target.stat().st_size / 1024 / 1024)
        return target


def build_client(**kwargs: Any) -> MptClient:
    """按环境变量构造默认客户端（``MPT_BASE_URL`` / ``MPT_API_KEY`` / 超时）。"""
    from core.config import get_settings

    settings = get_settings()
    kwargs.setdefault("base_url", settings.mpt_base_url)
    kwargs.setdefault("timeout", settings.mpt_timeout_seconds)
    kwargs.setdefault("download_timeout", settings.mpt_download_timeout_seconds)
    kwargs.setdefault("api_key", settings.mpt_api_key)
    return MptClient(**kwargs)


__all__ = [
    "API_PREFIX",
    "ASPECT_LANDSCAPE",
    "ASPECT_PORTRAIT",
    "ASPECT_RESOLUTIONS",
    "ASPECT_SQUARE",
    "CONCAT_MODES",
    "DEFAULT_PORT",
    "FAILED_STAGES",
    "KNOWN_TASK_STATES",
    "SUBTITLE_POSITIONS",
    "TASKS_URL_PREFIX",
    "TASK_STATE_COMPLETE",
    "TASK_STATE_FAILED",
    "TASK_STATE_PROCESSING",
    "TRANSITION_MODES",
    "VIDEO_SOURCES",
    "MptClient",
    "MptTask",
    "MptTaskLost",
    "MptVideoParams",
    "build_client",
    "parse_task",
    "redact",
]
