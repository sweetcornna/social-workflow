"""xiaohongshu-mcp（Apache-2.0）sidecar 的 httpx 薄封装。

设计原则
--------
- **sidecar 只作独立进程**：本模块只发 HTTP，绝不 import / 复制上游 Go 代码。
  一账号一容器一 volume 一端口（上游是单进程单账号，cookies 存单一 ``./data``）。
- **错误 → 契约异常**：统一映射到 ``publishers.base`` 的
  ``RetryableError`` / ``NeedsReloginError`` / ``PermanentError``，调度器只认这三类。
- **日志全程脱敏**：``AUTH_TOKEN``、``xsec_token``、二维码 base64 一律打码。
- ``dry_run=True`` 时所有**写操作**只记日志不发请求；只读校验（图片存在性）照常做。

接口核对（对照上游 ``main`` 分支源码，2026-08-15，仓库 v2.5.0）
--------------------------------------------------------------
路由表来自 ``routes.go``，请求/响应字段来自 ``types.go`` / ``service.go``：

- ``GET  /health``（**不鉴权**）→ ``{"success":true,"data":{"status":"healthy",...}}``
- ``GET  /api/v1/login/status`` → ``data: {"is_logged_in":bool,"username":str,"user_id":str}``
- ``GET  /api/v1/login/qrcode`` → ``data: {"timeout":"4m0s","is_logged_in":bool,"img":str}``
  ``img`` 是页面 ``.login-container .qrcode-img`` 的 ``src`` 原文，通常是
  ``data:image/png;base64,...`` 形式，本客户端负责剥掉 data URI 前缀。
- ``DELETE /api/v1/login/cookies``（**本项目不调用**：删 cookies 属于破坏性操作，
  只在 sidecars/xhs/README.md 里作为人工排障手段记录）
- ``POST /api/v1/publish`` body
  ``{"title","content","images":[],"tags":[],"schedule_at","is_original","visibility","products"}``
  → ``data: {"title","content","images":int,"status":"发布完成"}``
  **注意：返回体里没有 note_id / url**，笔记 id 必须靠 ``/api/v1/user/me`` 对账拿到。
- ``POST /api/v1/publish_video`` body ``{"title","content","video","tags","schedule_at",...}``
- ``GET  /api/v1/user/me?tab=`` → ``data: {"data":{"userBasicInfo","interactions","feeds"}}``
  （**两层 data**：handler 又包了一层 ``map{"data": result}``）
- ``POST /api/v1/user/profile`` body ``{"user_id","xsec_token","tab"}`` → 同上结构
- ``POST /api/v1/feeds/detail`` body ``{"feed_id","xsec_token","load_all_comments"}``
  → ``data: {"feed_id":str,"data":{"note":{...},"comments":{...}}}``

平台限制（上游 ``service.go`` / ``pkg/xhsutil/title.go`` 已核实）：

- 标题长度上限 20，算法为"非 ASCII 字符按 2 计、ASCII 按 1 计，总和向上取整除 2"
  （见 :func:`calc_title_length`，按规则重写，未复制上游代码）。
- ``schedule_at`` 必须是 RFC3339，且在 **1 小时后 ~ 14 天内**，由 sidecar 侧再校验一次。
- ``images`` 支持 http(s) URL（sidecar 自行下载）与**sidecar 容器内**的本地路径。

**未核实**（上游源码未给出显式约束，代码中按此处口径处理并标注）：

- 图文笔记的图片数量上限 18（上游只校验 ``min=1``；18 是小红书前端的公开限制）。
- 错误分类：sidecar 把所有 handler 失败都返回 ``HTTP 500 + code``，
  是否可重试只能靠 ``details`` 文案关键字判断（见 :func:`classify_error`）。
- ``visibility`` 取值只在注释里出现（"公开可见"/"仅自己可见"/"仅互关好友可见"）。
- MCP 备选路径：同一进程另有 ``POST /mcp``（Streamable HTTP，JSON-RPC 2.0，
  ``Stateless: true`` 可跳过 initialize 握手）。若上游哪天改了 REST 形状，
  可用 :meth:`XhsMcpClient.mcp_call` 走 MCP 工具调用兜底 —— 它用 httpx 手写最小
  JSON-RPC，**不引入 mcp 客户端库**（避免为一个 POST 拉进一整套依赖）。
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from publishers.base import (
    NeedsReloginError,
    PermanentError,
    RetryableError,
)

logger = logging.getLogger("social_workflow.publishers.xhs")

# --- 平台 / 上游限制 ---------------------------------------------------------

TITLE_MAX = 20
IMAGES_MIN = 1
IMAGES_MAX = 18  # 未核实：上游只校验 min=1，18 取自小红书前端公开限制
SCHEDULE_MIN_AHEAD = timedelta(hours=1)
SCHEDULE_MAX_AHEAD = timedelta(days=14)
IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp"})
VIDEO_EXTS = frozenset({".mp4", ".mov", ".m4v"})
VISIBILITIES = ("公开可见", "仅自己可见", "仅互关好友可见")  # 未核实：取自上游注释

DEFAULT_PORT = 18060
DEFAULT_CONTAINER_MEDIA_DIR = "/app/images"

# 笔记正文链接。xsec_token 是平台自己在分享链接里带的公开票据，缺它页面打不开
NOTE_URL_TEMPLATE = "https://www.xiaohongshu.com/explore/{note_id}"

# --- 错误文案分类（sidecar 一律回 HTTP 500，只能看 details） -------------------

# 登录态失效：账号级事件，触发 needs_relogin
NEEDS_RELOGIN_MARKERS: tuple[str, ...] = (
    "未登录",
    "请登录",
    "登录状态",
    "登录已过期",
    "登录失效",
    "登录超时",
    "cookie",
    "not logged in",
    "not login",
    "need login",
    "login required",
    # 刻意**不**收 "unauthorized"：sidecar 的 401 是 AUTH_TOKEN 配错（运维问题），
    # 不是小红书账号掉线。误判成 needs_relogin 会白白挂起排期并催人去扫码。
)
# 不可重试：内容/参数问题，重试多少次都一样
PERMANENT_MARKERS: tuple[str, ...] = (
    "标题长度超过限制",
    "定时发布时间",
    "格式错误",
    "文件不存在",
    "不可访问",
    "no valid images",
    "invalid image",
    "违规",
    "违反",
    "敏感词",
    "审核不通过",
    "内容不合规",
    "参数错误",
    "必须提供",
)


def classify_error(message: str, *, status: int, code: str = "") -> type[Exception]:
    """按 sidecar 回文选异常类型。

    上游把 handler 失败一律映射成 ``HTTP 500 + code + details``，HTTP 状态码本身
    带不了多少信息，所以这里按 ``code`` 与 ``details`` 文案分流。**默认可重试**：
    误判成永久失败会直接进死信，代价比多重试一次大。
    """
    if status in (401, 403):
        # sidecar 自己的 Bearer 鉴权失败 = AUTH_TOKEN / XHS_MCP_TOKENS 配错，
        # 与小红书账号登录态无关，先于关键字判断短路，别误判成 needs_relogin
        return PermanentError
    haystack = f"{code} {message}".lower()
    if any(marker.lower() in haystack for marker in NEEDS_RELOGIN_MARKERS):
        return NeedsReloginError
    if any(marker.lower() in haystack for marker in PERMANENT_MARKERS):
        return PermanentError
    if status in (400, 404, 405, 422):
        # 400 INVALID_REQUEST 之类都是调用方传错，重试没意义
        return PermanentError
    return RetryableError


# ------------------------------------------------------------------ 脱敏


_XSEC_JSON = re.compile(r'("(?:xsec_token|xsecToken)"\s*:\s*")[^"]*(")')
_XSEC_QUERY = re.compile(r"(xsec_token=)[^&\s\"']+")
_IMG_JSON = re.compile(r'("(?:img|image_base64)"\s*:\s*")[^"]{16,}(")')
_DATA_URI = re.compile(r"data:image/[0-9a-zA-Z.+-]+;base64,[A-Za-z0-9+/=]{16,}")
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+")


def redact(text: str, *secrets: str) -> str:
    """把 AUTH_TOKEN / xsec_token / 二维码 base64 从日志文本里抹掉。"""
    out = text
    for secret in secrets:
        if secret and len(secret) >= 6:
            out = out.replace(secret, f"{secret[:3]}***{secret[-2:]}")
    out = _BEARER.sub(r"\1***", out)
    out = _XSEC_JSON.sub(r"\1***\2", out)
    out = _XSEC_QUERY.sub(r"\1***", out)
    out = _IMG_JSON.sub(r"\1<base64 已省略>\2", out)
    out = _DATA_URI.sub("data:image/*;base64,<已省略>", out)
    return out


# ------------------------------------------------------------------ 小工具


def calc_title_length(text: str) -> int:
    """小红书标题长度口径：非 ASCII 记 2、ASCII 记 1，总和向上取整除 2。

    与上游 ``pkg/xhsutil/title.go:CalcTitleLength`` 同规则（按规则重写，未复制代码）。
    直觉解释：一个汉字算 1 个字，两个英文字母合起来算 1 个字。
    """
    units = 0
    for ch in text:
        code = ord(ch)
        if code > 0xFFFF:
            # 星际字符（emoji）在 UTF-16 里是两个代理码元，各自 > 127
            units += 4
        elif code > 127:
            units += 2
        else:
            units += 1
    return math.ceil(units / 2)


def parse_go_duration(text: str) -> float | None:
    """解析 Go 的 ``time.Duration.String()``，如 ``4m0s`` / ``1h30m0s`` / ``0s``。"""
    if not text:
        return None
    matches = re.findall(r"(\d+(?:\.\d+)?)(h|m|s|ms|us|µs|ns)", text)
    if not matches:
        return None
    factors = {
        "h": 3600.0,
        "m": 60.0,
        "s": 1.0,
        "ms": 1e-3,
        "us": 1e-6,
        "µs": 1e-6,
        "ns": 1e-9,
    }
    return sum(float(value) * factors[unit] for value, unit in matches)


_COUNT_UNITS = (("亿", 100_000_000), ("万", 10_000), ("w", 10_000), ("k", 1_000))


def parse_count(value: Any) -> int | None:
    """把小红书的计数字符串折算成整数。

    页面给的是 ``"1234"`` / ``"1.2万"`` / ``"3.4亿"``，未互动时也可能直接给标签文字
    （``"赞"`` / ``"收藏"``）。**不认识的一律返回 None，绝不伪造 0**
    （见 metrics/README.md：``available=False`` 时字段是"没数据"而不是 0）。
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
    if not text:
        return None
    for suffix, factor in _COUNT_UNITS:
        if text.lower().endswith(suffix):
            head = text[: -len(suffix)]
            try:
                return round(float(head) * factor)
            except ValueError:
                return None
    try:
        return int(text)
    except ValueError:
        return None


def note_url(note_id: str, xsec_token: str = "") -> str:
    """笔记链接。带 xsec_token 才打得开（平台自己的分享链接同样带它）。"""
    url = NOTE_URL_TEMPLATE.format(note_id=note_id)
    if xsec_token:
        return f"{url}?xsec_token={xsec_token}&xsec_source=pc_user"
    return url


def to_rfc3339(moment: datetime) -> str:
    """RFC3339（Go ``time.RFC3339`` 可解析）。naive 视为 UTC。"""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.isoformat(timespec="seconds")


def parse_rfc3339(text: str) -> datetime:
    value = text.strip()
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# ------------------------------------------------------- 宿主机 → 容器 路径映射


@dataclass(frozen=True)
class MediaPathMapper:
    """把宿主机上的素材路径翻译成 sidecar 容器内的路径。

    sidecar 跑在容器里，拿到的路径必须是**容器内**可见的。compose 里把
    ``${XHS_MEDIA_HOST_DIR}`` 只读挂到 ``/app/images``，这里做对应的路径改写。

    ``host_dir`` 留空表示 sidecar 与 core 共享文件系统（例如 macOS 上直接跑二进制），
    此时原样透传绝对路径。
    """

    host_dir: str = ""
    container_dir: str = DEFAULT_CONTAINER_MEDIA_DIR

    def to_sidecar(self, path: str) -> str:
        if is_remote_url(path):
            return path
        local = Path(path).expanduser()
        absolute = local if local.is_absolute() else (Path.cwd() / local)
        absolute = absolute.resolve()
        if not self.host_dir:
            return str(absolute)
        root = Path(self.host_dir).expanduser()
        root = (root if root.is_absolute() else Path.cwd() / root).resolve()
        try:
            relative = absolute.relative_to(root)
        except ValueError as exc:
            raise PermanentError(
                f"素材 {path} 不在 XHS_MEDIA_HOST_DIR({root}) 下，sidecar 容器看不到它。"
                "请把素材放进该目录，或把 XHS_MEDIA_HOST_DIR 置空（sidecar 与 core 同机时）。",
                raw={"path": str(absolute), "host_dir": str(root)},
            ) from exc
        return str(Path(self.container_dir) / relative)


def is_remote_url(path: str) -> bool:
    return path.startswith(("http://", "https://"))


@dataclass
class QrcodeInfo:
    """``GET /api/v1/login/qrcode`` 的归一化结果。"""

    image_base64: str
    timeout_seconds: float
    is_logged_in: bool
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class LoginStatus:
    is_logged_in: bool
    username: str = ""
    user_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------ 客户端


class XhsMcpClient:
    """xiaohongshu-mcp REST 客户端。一个实例对应**一个账号的一个 sidecar**。"""

    def __init__(
        self,
        base_url: str,
        *,
        auth_token: str = "",
        http: httpx.Client | None = None,
        timeout: float = 30.0,
        publish_timeout: float = 300.0,
        dry_run: bool = False,
        media_mapper: MediaPathMapper | None = None,
        account_id: str = "",
    ) -> None:
        if not base_url:
            raise PermanentError(
                "未配置该账号的 xiaohongshu-mcp sidecar 地址："
                "请在 accounts 表的 sidecar_endpoint 或 XHS_MCP_ENDPOINTS 里填写",
                raw={"account_id": account_id},
            )
        self.base_url = base_url.rstrip("/")
        self.auth_token = auth_token
        # 浏览器自动化很慢：发布要传图 + 等页面跳转，单独给一个更长的超时
        self.timeout = timeout
        self.publish_timeout = publish_timeout
        self.dry_run = dry_run
        self.media = media_mapper or MediaPathMapper()
        self.account_id = account_id
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

    def __enter__(self) -> XhsMcpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- 底层请求 ---------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        return headers

    def _scrub(self, text: str) -> str:
        return redact(text, self.auth_token)

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
        """发一次请求并把 ``{"success","data","message"}`` 里的 ``data`` 取出来。"""
        url = self._url(path)
        logger.debug(
            "xhs -> %s %s %s",
            method,
            self._scrub(url),
            self._scrub(json.dumps(json_body, ensure_ascii=False)[:1500]) if json_body else "",
        )
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
                f"{api} 连不上 sidecar {self.base_url}: {exc}"
                "（容器是否起着？端口是否与 accounts 里的 sidecar_endpoint 一致？）",
                raw={"api": api, "endpoint": self.base_url},
            ) from exc

        body: Any = None
        try:
            body = resp.json()
        except ValueError:
            body = None
        logger.debug(
            "xhs <- %s %s %s",
            api,
            resp.status_code,
            self._scrub(json.dumps(body, ensure_ascii=False)[:1500])
            if body is not None
            else self._scrub(resp.text[:300]),
        )

        if resp.status_code >= 400 or not isinstance(body, dict) or not body.get("success"):
            self._raise_api_error(resp, body, api=api)
        return body.get("data")

    def _raise_api_error(self, resp: httpx.Response, body: Any, *, api: str) -> None:
        code = ""
        message = ""
        details: Any = None
        if isinstance(body, dict):
            code = str(body.get("code") or "")
            message = str(body.get("error") or body.get("message") or "")
            details = body.get("details")
        text = " ".join(
            part for part in (message, details if isinstance(details, str) else "") if part
        )
        if not text:
            text = self._scrub(resp.text[:300]) or f"HTTP {resp.status_code}"
        exc_type = classify_error(text, status=resp.status_code, code=code)
        raw = {
            "api": api,
            "status": resp.status_code,
            "code": code,
            "endpoint": self.base_url,
        }
        raise exc_type(
            f"{api} 失败（HTTP {resp.status_code} {code}）: {self._scrub(text)}", raw=raw
        )

    # -- 只读接口 ---------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """``GET /health``（sidecar 自身存活，不鉴权、不碰浏览器）。"""
        data = self._request("GET", "/health", api="health")
        return data if isinstance(data, dict) else {}

    def login_status(self) -> LoginStatus:
        """``GET /api/v1/login/status``。"""
        data = self._request("GET", "/api/v1/login/status", api="login/status")
        payload = data if isinstance(data, dict) else {}
        return LoginStatus(
            is_logged_in=bool(payload.get("is_logged_in")),
            username=str(payload.get("username") or ""),
            user_id=str(payload.get("user_id") or ""),
            raw=payload,
        )

    def get_login_qrcode(self) -> QrcodeInfo:
        """``GET /api/v1/login/qrcode``：返回二维码 PNG 的 base64（无 data URI 前缀）。

        红线：只把图给人看，人自己用小红书 App 扫。**不做任何自动识别 / 打码**
        （见 docs/POLICY.md）。
        """
        data = self._request("GET", "/api/v1/login/qrcode", api="login/qrcode")
        payload = data if isinstance(data, dict) else {}
        raw_img = str(payload.get("img") or "")
        image_base64 = raw_img.split(",", 1)[1] if raw_img.startswith("data:") else raw_img
        timeout = parse_go_duration(str(payload.get("timeout") or "")) or 240.0
        return QrcodeInfo(
            image_base64=image_base64,
            timeout_seconds=timeout,
            is_logged_in=bool(payload.get("is_logged_in")),
            raw={k: v for k, v in payload.items() if k != "img"},
        )

    def my_profile(self, *, tab: str = "") -> dict[str, Any]:
        """``GET /api/v1/user/me``：当前登录账号的主页（含最近笔记）。

        响应是**两层 data**（handler 又包了一层），这里剥掉。
        """
        params = {"tab": tab} if tab else None
        data = self._request("GET", "/api/v1/user/me", api="user/me", params=params)
        return _unwrap_profile(data)

    def my_notes(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        """当前账号最近发布的笔记（``user/me`` 的 ``feeds``），用于对账与指标。"""
        feeds = self.my_profile().get("feeds") or []
        notes = [f for f in feeds if isinstance(f, dict)]
        return notes[:limit] if limit else notes

    def user_profile(self, user_id: str, xsec_token: str, *, tab: str = "") -> dict[str, Any]:
        """``POST /api/v1/user/profile``：他人主页（需要 xsec_token）。"""
        data = self._request(
            "POST",
            "/api/v1/user/profile",
            api="user/profile",
            json_body={"user_id": user_id, "xsec_token": xsec_token, "tab": tab},
        )
        return _unwrap_profile(data)

    def note_detail(
        self, feed_id: str, xsec_token: str, *, load_all_comments: bool = False
    ) -> dict[str, Any]:
        """``POST /api/v1/feeds/detail``：笔记详情（互动数、正文、发布时间）。

        返回**剥掉两层包装后**的 ``{"note": {...}, "comments": {...}}``。
        """
        data = self._request(
            "POST",
            "/api/v1/feeds/detail",
            api="feeds/detail",
            json_body={
                "feed_id": feed_id,
                "xsec_token": xsec_token,
                "load_all_comments": bool(load_all_comments),
            },
        )
        payload = data if isinstance(data, dict) else {}
        inner = payload.get("data")
        return inner if isinstance(inner, dict) else payload

    # -- 素材 -------------------------------------------------------------

    def resolve_images(self, paths: list[str]) -> list[str]:
        """校验图片存在性 / 格式 / 数量，并翻译成 sidecar 容器内路径。

        放在 client 而不是 publisher，是为了让测试替身（:mod:`publishers.xhs.stub`）
        能整体绕过文件系统，同时真实客户端在 ``dry_run`` 下也照样做本地校验。
        """
        if not IMAGES_MIN <= len(paths) <= IMAGES_MAX:
            raise PermanentError(
                f"小红书图文笔记需要 {IMAGES_MIN}–{IMAGES_MAX} 张图片，当前 {len(paths)} 张"
                "（18 张为前端公开限制，未在 sidecar 侧核实）",
                raw={"count": len(paths)},
            )
        resolved: list[str] = []
        for path in paths:
            if is_remote_url(path):
                resolved.append(path)
                continue
            local = Path(path).expanduser()
            absolute = local if local.is_absolute() else Path.cwd() / local
            if not absolute.is_file():
                raise PermanentError(
                    f"图片文件不存在: {path}（解析为 {absolute}）", raw={"path": str(absolute)}
                )
            if absolute.suffix.lower() not in IMAGE_EXTS:
                raise PermanentError(
                    f"不支持的图片格式 {absolute.suffix}，允许 {sorted(IMAGE_EXTS)}",
                    raw={"path": str(absolute)},
                )
            resolved.append(self.media.to_sidecar(str(absolute)))
        return resolved

    def resolve_video(self, path: str) -> str:
        """校验视频文件并翻译路径。上游只接受**本地单个视频文件**，不接受 URL。"""
        if is_remote_url(path):
            raise PermanentError(
                "小红书视频笔记只接受本地视频文件，不支持 URL（上游 publish_video 限制）",
                raw={"path": path},
            )
        local = Path(path).expanduser()
        absolute = local if local.is_absolute() else Path.cwd() / local
        if not absolute.is_file():
            raise PermanentError(
                f"视频文件不存在: {path}（解析为 {absolute}）", raw={"path": str(absolute)}
            )
        if absolute.suffix.lower() not in VIDEO_EXTS:
            raise PermanentError(
                f"不支持的视频格式 {absolute.suffix}，允许 {sorted(VIDEO_EXTS)}",
                raw={"path": str(absolute)},
            )
        return self.media.to_sidecar(str(absolute))

    # -- 写操作 -----------------------------------------------------------

    def publish_content(
        self,
        *,
        title: str,
        content: str,
        images: list[str],
        tags: list[str] | None = None,
        schedule_at: str | None = None,
        is_original: bool = False,
        visibility: str = "",
        products: list[str] | None = None,
    ) -> dict[str, Any]:
        """``POST /api/v1/publish``：发布图文笔记。

        ``images`` 必须已经是 sidecar 可见的路径（先过 :meth:`resolve_images`）。
        返回体**不含 note_id**，由调用方用 :meth:`my_notes` 对账取回。
        """
        payload: dict[str, Any] = {
            "title": title,
            "content": content,
            "images": list(images),
        }
        if tags:
            payload["tags"] = list(tags)
        if schedule_at:
            payload["schedule_at"] = schedule_at
        if is_original:
            payload["is_original"] = True
        if visibility:
            payload["visibility"] = visibility
        if products:
            payload["products"] = list(products)
        if self.dry_run:
            logger.info(
                "[dry_run] xhs publish 跳过：title=%r images=%d schedule_at=%s",
                title,
                len(images),
                schedule_at,
            )
            return {"dry_run": True, "title": title, "images": len(images), "status": "dry_run"}
        data = self._request(
            "POST",
            "/api/v1/publish",
            api="publish",
            json_body=payload,
            timeout=self.publish_timeout,
        )
        return data if isinstance(data, dict) else {"status": str(data)}

    def publish_video(
        self,
        *,
        title: str,
        content: str,
        video: str,
        tags: list[str] | None = None,
        schedule_at: str | None = None,
        visibility: str = "",
        products: list[str] | None = None,
    ) -> dict[str, Any]:
        """``POST /api/v1/publish_video``：发布视频笔记（本地单文件）。"""
        payload: dict[str, Any] = {"title": title, "content": content, "video": video}
        if tags:
            payload["tags"] = list(tags)
        if schedule_at:
            payload["schedule_at"] = schedule_at
        if visibility:
            payload["visibility"] = visibility
        if products:
            payload["products"] = list(products)
        if self.dry_run:
            logger.info("[dry_run] xhs publish_video 跳过：title=%r video=%s", title, video)
            return {"dry_run": True, "title": title, "video": video, "status": "dry_run"}
        data = self._request(
            "POST",
            "/api/v1/publish_video",
            api="publish_video",
            json_body=payload,
            timeout=self.publish_timeout,
        )
        return data if isinstance(data, dict) else {"status": str(data)}

    # -- MCP 备选通道 ------------------------------------------------------

    def mcp_call(self, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """MCP 兜底：``POST /mcp`` 走 JSON-RPC 2.0 的 ``tools/call``。

        REST 与 MCP 由同一进程提供、共用同一套 service，正常路径**只用 REST**。
        这里保留最小实现是为了上游万一调整 REST 形状时有条退路 —— 手写 JSON-RPC，
        不引入 ``mcp`` 客户端库（依赖收益不成正比）。

        **未核实**：具体工具名与参数 schema 以运行期 ``tools/list`` 为准。
        """
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments or {}},
        }
        url = self._url("/mcp")
        headers = {**self._headers(), "Content-Type": "application/json"}
        # Streamable HTTP 要求同时接受 JSON 与 SSE；上游开了 JSONResponse，实际回 JSON
        headers["Accept"] = "application/json, text/event-stream"
        try:
            resp = self.http.post(url, json=body, headers=headers, timeout=self.publish_timeout)
        except httpx.HTTPError as exc:
            raise RetryableError(f"mcp/{tool} 调用失败: {exc}", raw={"api": f"mcp/{tool}"}) from exc
        if resp.status_code >= 400:
            self._raise_api_error(resp, None, api=f"mcp/{tool}")
        payload = resp.json()
        if isinstance(payload, dict) and payload.get("error"):
            error = payload["error"]
            message = str(error.get("message", error))
            exc_type = classify_error(message, status=resp.status_code)
            raise exc_type(f"mcp/{tool} 返回错误: {self._scrub(message)}", raw={"api": tool})
        return payload if isinstance(payload, dict) else {}


def _unwrap_profile(data: Any) -> dict[str, Any]:
    """``user/me`` 与 ``user/profile`` 都是 ``{"data": {...}}`` 双层包装。"""
    payload = data if isinstance(data, dict) else {}
    inner = payload.get("data")
    return inner if isinstance(inner, dict) else payload


__all__ = [
    "DEFAULT_CONTAINER_MEDIA_DIR",
    "DEFAULT_PORT",
    "IMAGES_MAX",
    "IMAGES_MIN",
    "SCHEDULE_MAX_AHEAD",
    "SCHEDULE_MIN_AHEAD",
    "TITLE_MAX",
    "VISIBILITIES",
    "LoginStatus",
    "MediaPathMapper",
    "QrcodeInfo",
    "XhsMcpClient",
    "calc_title_length",
    "classify_error",
    "is_remote_url",
    "note_url",
    "parse_count",
    "parse_go_duration",
    "parse_rfc3339",
    "redact",
    "to_rfc3339",
]
