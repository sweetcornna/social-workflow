"""宿主机抖音上传器（``publishers/douyin/service.py``）的 httpx 薄封装。

形态说明
--------
抖音**没有面向个人/小团队的发布 API**（`video.create.bind` 仅限党政/事业单位），
只能走浏览器自动化；而抖音会检测 headless，所以上传器必须是**有头**进程，
跑在真人的宿主机上（不入 Docker）。本模块就是 core 侧对那个本地服务的客户端：

    core（可能在容器里） --HTTP--> 宿主机 python -m publishers.douyin serve

设计原则（与 ``publishers/xhs/client.py`` 同一套）
------------------------------------------------
- **只发 HTTP**：本模块绝不 import patchright，core 侧不需要装浏览器。
- **业务结果走 envelope，不走 HTTP 状态码**：服务一律返回
  ``{"ok": bool, "state": str, "detail": str, ...}``，HTTP 5xx 只表示服务自身崩了。
  ``state`` → 契约异常的映射见 :data:`STATE_EXCEPTIONS`。
- **日志脱敏**：短信验证码永不进日志（连请求体都不打），昵称一律打码。
- ``dry_run=True`` 时所有**写操作**只记日志不发请求；只读的本地校验（文件存在性）照常做。

服务端接口（本仓库自己定义，见 ``publishers/douyin/service.py``）
---------------------------------------------------------------
- ``GET  /health``
- ``GET  /accounts/{id}/login/status``
- ``POST /accounts/{id}/login/start``
- ``POST /accounts/{id}/sms_code``（**只填写，不识别**，见 docs/POLICY.md）
- ``POST /accounts/{id}/publish``
- ``GET  /accounts/{id}/recent_posts``
- ``GET  /accounts/{id}/metrics/{post_id}``
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from publishers.base import (
    NeedsReloginError,
    PermanentError,
    PublishError,
    RetryableError,
)

logger = logging.getLogger("social_workflow.publishers.douyin")

# --- 平台限制 ---------------------------------------------------------------
#
# 以下数值依据 2026-08 抖音创作者中心（https://creator.douyin.com/creator-micro/content/upload）
# 页面观察写入，**未在真实站点验证**，平台改版即可能失效。改了这里也要改 README。

TITLE_MAX = 30
DESCRIPTION_MAX = 1000
HASHTAG_MAX = 5
VIDEO_EXTS = frozenset({".mp4"})
COVER_EXTS = frozenset({".jpg", ".jpeg", ".png"})
# 定时发布窗口：2 小时后 ~ 14 天内（未验证）
SCHEDULE_MIN_AHEAD = timedelta(hours=2)
SCHEDULE_MAX_AHEAD = timedelta(days=14)

DEFAULT_PORT = 8710
CREATOR_HOME = "https://creator.douyin.com"
UPLOAD_URL = f"{CREATOR_HOME}/creator-micro/content/upload"
MANAGE_URL = f"{CREATOR_HOME}/creator-micro/content/manage"
DATA_CENTER_URL = f"{CREATOR_HOME}/creator-micro/data-center/content"
VIDEO_URL_TEMPLATE = "https://www.douyin.com/video/{post_id}"


# --- 服务端 state 取值（唯一真相，服务与客户端共用） -------------------------

STATE_OK = "ok"
STATE_PUBLISHED = "published"
STATE_SCHEDULED = "scheduled"
STATE_LOGGED_IN = "logged_in"
STATE_LOGGED_OUT = "logged_out"
STATE_WAITING_USER = "waiting_user"
STATE_NEEDS_SMS = "needs_sms"
STATE_NEEDS_CAPTCHA = "needs_captcha_by_human"
STATE_IDENTITY_MISMATCH = "identity_mismatch"
STATE_INVALID_CONTENT = "invalid_content"
STATE_REJECTED = "rejected"
STATE_TIMEOUT = "timeout"
STATE_BROWSER_ERROR = "browser_error"
STATE_BUSY = "busy"
STATE_NO_BROWSER = "no_browser"
STATE_NO_SMS_INPUT = "no_sms_input"

#: ``state`` → 契约异常。**没列出来的一律按可重试**：误判成永久失败会直接进死信。
STATE_EXCEPTIONS: dict[str, type[PublishError]] = {
    # 需要真人介入：账号级事件，挂起该账号排期并通知去 /accounts/{id}/login
    STATE_LOGGED_OUT: NeedsReloginError,
    STATE_WAITING_USER: NeedsReloginError,
    STATE_NEEDS_SMS: NeedsReloginError,
    STATE_NEEDS_CAPTCHA: NeedsReloginError,
    STATE_NO_BROWSER: NeedsReloginError,
    # 不可重试：发错号 / 内容或参数非法 / 平台判违规
    STATE_IDENTITY_MISMATCH: PermanentError,
    STATE_INVALID_CONTENT: PermanentError,
    STATE_REJECTED: PermanentError,
    # 可重试：服务忙、浏览器抖动、页面等超时
    STATE_BUSY: RetryableError,
    STATE_BROWSER_ERROR: RetryableError,
    STATE_TIMEOUT: RetryableError,
}

SUCCESS_STATES = frozenset(
    {STATE_OK, STATE_PUBLISHED, STATE_SCHEDULED, STATE_LOGGED_IN, STATE_WAITING_USER}
)


def exception_for_state(state: str) -> type[PublishError]:
    """按 ``state`` 选异常类型。未知 state 按可重试处理（保守）。"""
    return STATE_EXCEPTIONS.get(state, RetryableError)


# ------------------------------------------------------------------ 脱敏

_CODE_JSON = re.compile(r'("(?:code|sms_code)"\s*:\s*")[^"]*(")')
_DIGITS = re.compile(r"(?<!\d)\d{4,8}(?!\d)")


def mask_nickname(name: str) -> str:
    """昵称打码：只保留首尾各一个字符，用于日志与错误文案。

    identity 校验要让人看得出"发到哪个号上了"，又不该把完整昵称洒进日志。
    """
    text = (name or "").strip()
    if not text:
        return ""
    if len(text) <= 2:
        return text[0] + "*"
    return f"{text[0]}***{text[-1]}"


def redact(text: str) -> str:
    """抹掉日志文本里的验证码（JSON 字段与裸数字串）。"""
    out = _CODE_JSON.sub(r"\1***\2", text)
    return _DIGITS.sub("******", out)


# ------------------------------------------------------------------ 小工具


def to_rfc3339(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.isoformat(timespec="seconds")


def parse_rfc3339(text: str) -> datetime:
    value = text.strip()
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_count(value: Any) -> int | None:
    """把页面上的计数折算成整数。

    抖音数据中心给的是 ``"1234"`` / ``"1.2万"`` / ``"3.4亿"``，没数据时可能是 ``"-"``。
    **不认识的一律 None，绝不伪造 0**（见 metrics/README.md）。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if not isinstance(value, str):
        return None
    text = value.strip().replace(",", "").replace(" ", "")
    if not text or text in {"-", "--"}:
        return None
    for suffix, factor in (("亿", 100_000_000), ("万", 10_000), ("w", 10_000), ("k", 1_000)):
        if text.lower().endswith(suffix):
            try:
                return round(float(text[: -len(suffix)]) * factor)
            except ValueError:
                return None
    try:
        return int(text)
    except ValueError:
        return None


def video_url(post_id: str) -> str:
    return VIDEO_URL_TEMPLATE.format(post_id=post_id)


def normalise_title(text: str) -> str:
    """标题比对口径：去首尾空白 + 折叠内部空白（页面回显会做空白归一）。"""
    return " ".join(text.split())


def clean_hashtags(tags: list[str], *, limit: int = HASHTAG_MAX) -> list[str]:
    """去 ``#`` 前缀、去空、保序去重，并截断到 ``limit`` 个。

    截断而不是报错：话题不属于内容主体，为多打了一个标签把整条内容打进死信不划算。
    截断是确定性的，所以 ``prepare`` 仍然幂等。
    """
    out: dict[str, None] = {}
    for tag in tags:
        cleaned = tag.strip().lstrip("#").strip()
        # 抖音话题里不能有空格，页面会把空格当成"结束话题输入"
        cleaned = cleaned.replace(" ", "")
        if cleaned:
            out.setdefault(cleaned, None)
    return list(out)[:limit]


# ---------------------------------------------------- core → 宿主机 路径映射


@dataclass(frozen=True)
class HostPathMapper:
    """把 core 侧看到的素材路径翻译成**宿主机上传器**看得到的路径。

    core 跑在 Docker 里、上传器跑在宿主机时，同一个文件两边路径不同：
    compose 把宿主机的 ``${DOUYIN_MEDIA_HOST_DIR}`` 挂到容器的 ``local_dir``，
    这里做反向改写。两个值都留空 = core 与上传器同机（macOS 上的常规 MVP 形态），
    原样透传绝对路径。
    """

    local_dir: str = ""
    host_dir: str = ""

    def to_host(self, path: str) -> str:
        local = Path(path).expanduser()
        absolute = local if local.is_absolute() else (Path.cwd() / local)
        absolute = absolute.resolve()
        if not self.local_dir or not self.host_dir:
            return str(absolute)
        root = Path(self.local_dir).expanduser()
        root = (root if root.is_absolute() else Path.cwd() / root).resolve()
        try:
            relative = absolute.relative_to(root)
        except ValueError as exc:
            raise PermanentError(
                f"素材 {path} 不在 DOUYIN_MEDIA_LOCAL_DIR({root}) 下，"
                "宿主机上传器看不到它。请把成片放进该目录，或把两个 DOUYIN_MEDIA_*_DIR 都置空"
                "（core 与上传器同机时）。",
                raw={"path": str(absolute), "local_dir": str(root)},
            ) from exc
        return str(Path(self.host_dir) / relative)


# ------------------------------------------------------------------ 返回结构


@dataclass
class LoginState:
    """``GET /accounts/{id}/login/status`` 的归一化结果。"""

    state: str
    nickname: str = ""
    detail: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_logged_in(self) -> bool:
        return self.state == STATE_LOGGED_IN


@dataclass
class PublishOutcome:
    """``POST /accounts/{id}/publish`` 成功时的归一化结果。"""

    state: str
    post_id: str = ""
    url: str = ""
    screenshot_path: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class RecentPost:
    """内容管理页上的一条作品。"""

    title: str
    post_id: str = ""
    url: str = ""
    published_at: datetime | None = None
    raw_time: str = ""


# ------------------------------------------------------------------ 客户端


class DouyinServiceClient:
    """宿主机上传器的 REST 客户端。一个实例对应**一个账号**。"""

    def __init__(
        self,
        base_url: str,
        *,
        http: httpx.Client | None = None,
        timeout: float = 30.0,
        publish_timeout: float = 900.0,
        dry_run: bool = False,
        account_id: str = "",
        path_mapper: HostPathMapper | None = None,
    ) -> None:
        if not base_url:
            raise PermanentError(
                "未配置抖音上传器地址：请设置 DOUYIN_SERVICE_URL"
                "（或在 accounts 表的 sidecar_endpoint 里填写）",
                raw={"account_id": account_id},
            )
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # 上传成片 + 等平台转码 + 等页面跳转，比小红书还慢，单独给一个很长的超时
        self.publish_timeout = publish_timeout
        self.dry_run = dry_run
        self.account_id = account_id
        self.media = path_mapper or HostPathMapper()
        self._http = http
        self._own_http = http is None

    # -- 生命周期 ---------------------------------------------------------

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=self.timeout, follow_redirects=False)
        return self._http

    def close(self) -> None:
        if self._own_http and self._http is not None:
            self._http.close()
            self._http = None

    def __enter__(self) -> DouyinServiceClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- 底层请求 ---------------------------------------------------------

    def _url(self, path: str) -> str:
        account = self.account_id
        return f"{self.base_url}/{path.lstrip('/')}".replace("{id}", account)

    def _request(
        self,
        method: str,
        path: str,
        *,
        api: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
        log_body: bool = True,
    ) -> dict[str, Any]:
        """发一次请求并返回 envelope ``{"ok","state","detail",...}``。

        传输层失败一律 :class:`RetryableError`（上传器没起 / 被人关了窗口都属于
        "等会儿再试"，不该把内容打进死信）。
        """
        url = self._url(path)
        if log_body and json_body:
            logger.debug("douyin -> %s %s %s", method, url, redact(json.dumps(json_body)[:800]))
        else:
            logger.debug("douyin -> %s %s", method, url)
        try:
            resp = self.http.request(
                method,
                url,
                json=json_body,
                params=params,
                headers={"Accept": "application/json"},
                timeout=timeout if timeout is not None else self.timeout,
            )
        except httpx.TimeoutException as exc:
            raise RetryableError(
                f"{api} 请求超时（{timeout or self.timeout}s）: {exc}",
                raw={"api": api, "endpoint": self.base_url},
            ) from exc
        except httpx.HTTPError as exc:
            raise RetryableError(
                f"{api} 连不上宿主机抖音上传器 {self.base_url}: {exc}"
                "（`python -m publishers.douyin serve` 起着吗？"
                "core 在容器里时 DOUYIN_SERVICE_URL 要写 host.docker.internal）",
                raw={"api": api, "endpoint": self.base_url},
            ) from exc

        body: Any = None
        try:
            body = resp.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise RetryableError(
                f"{api} 返回的不是 JSON（HTTP {resp.status_code}）: {redact(resp.text[:300])}",
                raw={"api": api, "status": resp.status_code},
            )
        logger.debug(
            "douyin <- %s %s %s",
            api,
            resp.status_code,
            redact(json.dumps(body, ensure_ascii=False)[:800]),
        )
        if resp.status_code == 404:
            raise PermanentError(
                f"{api} 路径不存在（HTTP 404）：上传器版本与 core 不匹配？"
                f" detail={body.get('detail')}",
                raw={"api": api, "status": 404},
            )
        if resp.status_code >= 400:
            raise RetryableError(
                f"{api} 失败（HTTP {resp.status_code}）: {redact(str(body)[:300])}",
                raw={"api": api, "status": resp.status_code},
            )
        return body

    def raise_for_state(self, env: dict[str, Any], *, api: str) -> dict[str, Any]:
        """envelope 不 ok 时按 ``state`` 抛对应契约异常，并把截图路径带进 ``raw``。"""
        state = str(env.get("state") or "")
        if env.get("ok") and state in SUCCESS_STATES:
            return env
        detail = str(env.get("detail") or "")
        exc_type = exception_for_state(state)
        raw = {
            "api": api,
            "state": state,
            "account_id": self.account_id,
            "screenshot_path": env.get("screenshot_path") or "",
        }
        raise exc_type(f"{api} 未完成（state={state}）: {detail}", raw=raw)

    # -- 只读接口 ---------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """``GET /health``：上传器进程自身是否活着（不碰浏览器）。"""
        return self._request("GET", "/health", api="health")

    def login_status(self) -> LoginState:
        """``GET /accounts/{id}/login/status``：打开创作者中心判断登录态。"""
        env = self._request("GET", "/accounts/{id}/login/status", api="login/status")
        return LoginState(
            state=str(env.get("state") or STATE_BROWSER_ERROR),
            nickname=str(env.get("nickname") or ""),
            detail=str(env.get("detail") or ""),
            raw=env,
        )

    def recent_posts(self, *, limit: int = 20) -> list[RecentPost]:
        """``GET /accounts/{id}/recent_posts``：内容管理页最近 N 条，用于对账。"""
        env = self._request(
            "GET", "/accounts/{id}/recent_posts", api="recent_posts", params={"limit": limit}
        )
        self.raise_for_state(env, api="recent_posts")
        posts: list[RecentPost] = []
        for entry in env.get("posts") or []:
            if not isinstance(entry, dict):
                continue
            moment: datetime | None = None
            iso = str(entry.get("published_at") or "")
            if iso:
                try:
                    moment = parse_rfc3339(iso)
                except ValueError:
                    moment = None
            posts.append(
                RecentPost(
                    title=str(entry.get("title") or ""),
                    post_id=str(entry.get("post_id") or ""),
                    url=str(entry.get("url") or ""),
                    published_at=moment,
                    raw_time=str(entry.get("raw_time") or ""),
                )
            )
        return posts

    def metrics(self, post_id: str) -> dict[str, Any]:
        """``GET /accounts/{id}/metrics/{post_id}``：作品数据页的公开指标（尽力而为）。"""
        env = self._request(
            "GET", f"/accounts/{{id}}/metrics/{post_id}", api="metrics", timeout=self.timeout * 3
        )
        return env

    # -- 人工介入 ---------------------------------------------------------

    def start_login(self, *, identity_hint: str = "") -> dict[str, Any]:
        """``POST /accounts/{id}/login/start``：在宿主机弹出登录窗口让人扫码。

        红线：只负责**把窗口打开**，扫码/输码全部由真人完成，不做任何自动登录。
        """
        if self.dry_run:
            logger.info("[dry_run] douyin login/start 跳过：account=%s", self.account_id)
            return {"ok": True, "state": STATE_WAITING_USER, "detail": "dry_run：未打开浏览器"}
        return self._request(
            "POST",
            "/accounts/{id}/login/start",
            api="login/start",
            json_body={"identity_hint": identity_hint},
            timeout=self.timeout * 3,
        )

    def submit_sms_code(self, code: str) -> dict[str, Any]:
        """``POST /accounts/{id}/sms_code``：把**人自己收到**的验证码填进页面输入框。

        红线：只做转发与填写，**绝不识别**（docs/POLICY.md）。
        请求体不进日志（``log_body=False``），验证码也不落库。
        """
        if self.dry_run:
            logger.info("[dry_run] douyin sms_code 跳过：account=%s", self.account_id)
            return {"ok": True, "state": STATE_OK, "detail": "dry_run：未填写"}
        return self._request(
            "POST",
            "/accounts/{id}/sms_code",
            api="sms_code",
            json_body={"code": code},
            log_body=False,
            timeout=self.timeout * 2,
        )

    # -- 素材 -------------------------------------------------------------

    def resolve_video(self, path: str) -> str:
        """校验成片存在性 / 格式，并翻译成宿主机可见路径。

        放在 client 而不是 publisher，是为了让测试替身（:mod:`publishers.douyin.stub`）
        能整体绕过文件系统，同时真实客户端在 ``dry_run`` 下也照样做本地校验。
        """
        local = Path(path).expanduser()
        absolute = local if local.is_absolute() else Path.cwd() / local
        if not absolute.is_file():
            raise PermanentError(
                f"视频文件不存在: {path}（解析为 {absolute}）", raw={"path": str(absolute)}
            )
        if absolute.suffix.lower() not in VIDEO_EXTS:
            raise PermanentError(
                f"抖音成片只接受 {sorted(VIDEO_EXTS)}，收到 {absolute.suffix}",
                raw={"path": str(absolute)},
            )
        return self.media.to_host(str(absolute))

    def resolve_cover(self, path: str) -> str:
        """封面是可选的：给了就必须存在且是 jpg/png。"""
        local = Path(path).expanduser()
        absolute = local if local.is_absolute() else Path.cwd() / local
        if not absolute.is_file():
            raise PermanentError(
                f"封面文件不存在: {path}（解析为 {absolute}）", raw={"path": str(absolute)}
            )
        if absolute.suffix.lower() not in COVER_EXTS:
            raise PermanentError(
                f"封面只接受 {sorted(COVER_EXTS)}，收到 {absolute.suffix}",
                raw={"path": str(absolute)},
            )
        return self.media.to_host(str(absolute))

    # -- 写操作 -----------------------------------------------------------

    def publish(
        self,
        *,
        title: str,
        description: str,
        video_path: str,
        hashtags: list[str] | None = None,
        cover_path: str = "",
        schedule_at: str = "",
        identity_hint: str = "",
    ) -> PublishOutcome:
        """``POST /accounts/{id}/publish``：驱动上传器走完整个发布流程。

        ``video_path`` / ``cover_path`` 必须已经过 :meth:`resolve_video` /
        :meth:`resolve_cover`（即宿主机可见的绝对路径）。
        """
        payload: dict[str, Any] = {
            "title": title,
            "description": description,
            "video_path": video_path,
            "hashtags": list(hashtags or []),
            "cover_path": cover_path,
            "schedule_at": schedule_at,
            "identity_hint": identity_hint,
        }
        if self.dry_run:
            logger.info(
                "[dry_run] douyin publish 跳过：title=%r video=%s schedule_at=%s",
                title,
                video_path,
                schedule_at or "-",
            )
            return PublishOutcome(state="dry_run", raw={"dry_run": True, **payload})
        env = self._request(
            "POST",
            "/accounts/{id}/publish",
            api="publish",
            json_body=payload,
            timeout=self.publish_timeout,
        )
        self.raise_for_state(env, api="publish")
        return PublishOutcome(
            state=str(env.get("state") or STATE_PUBLISHED),
            post_id=str(env.get("post_id") or ""),
            url=str(env.get("url") or ""),
            screenshot_path=str(env.get("screenshot_path") or ""),
            raw=env,
        )


__all__ = [
    "COVER_EXTS",
    "CREATOR_HOME",
    "DATA_CENTER_URL",
    "DEFAULT_PORT",
    "DESCRIPTION_MAX",
    "HASHTAG_MAX",
    "MANAGE_URL",
    "SCHEDULE_MAX_AHEAD",
    "SCHEDULE_MIN_AHEAD",
    "STATE_BROWSER_ERROR",
    "STATE_BUSY",
    "STATE_EXCEPTIONS",
    "STATE_IDENTITY_MISMATCH",
    "STATE_INVALID_CONTENT",
    "STATE_LOGGED_IN",
    "STATE_LOGGED_OUT",
    "STATE_NEEDS_CAPTCHA",
    "STATE_NEEDS_SMS",
    "STATE_NO_BROWSER",
    "STATE_NO_SMS_INPUT",
    "STATE_OK",
    "STATE_PUBLISHED",
    "STATE_REJECTED",
    "STATE_SCHEDULED",
    "STATE_TIMEOUT",
    "STATE_WAITING_USER",
    "SUCCESS_STATES",
    "TITLE_MAX",
    "UPLOAD_URL",
    "VIDEO_EXTS",
    "DouyinServiceClient",
    "HostPathMapper",
    "LoginState",
    "PublishOutcome",
    "RecentPost",
    "clean_hashtags",
    "exception_for_state",
    "mask_nickname",
    "normalise_title",
    "parse_count",
    "parse_rfc3339",
    "redact",
    "to_rfc3339",
    "video_url",
]
