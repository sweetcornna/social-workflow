"""微信公众号官方 API 的 httpx 薄封装。

设计原则
--------
- **不深绑 SDK**：只以 ``wechatpy``(MIT) 为参考实现思路，所有请求用 httpx 直调，
  依赖面最小、错误码映射对本项目的状态机友好。
- **错误码 → 契约异常**：统一映射到 ``publishers.base`` 的 ``RetryableError`` /
  ``PermanentError``，调度器只认这两类（公众号没有登录态，永远不产生 ``NeedsReloginError``）。
- **凭据只从环境变量来**，日志全程脱敏（secret / access_token 一律打码）。
- ``dry_run=True`` 时**所有写操作只记日志不发请求**，返回确定性的假 id，
  保证 ``prepare`` 幂等、契约测试可离线跑。

字段核对
--------
下列字段与限制已对照官方文档核实（2026-08-15）：

- ``cgi-bin/stable_token``：``grant_type`` / ``appid`` / ``secret`` / ``force_refresh``；
  返回 ``access_token`` / ``expires_in``（7200s）；force_refresh 每日 20 次、间隔 30s。
  错误码 40013(AppID 无效) / 40125(AppSecret 无效) / 43002(必须 POST) /
  45009(达到日调用上限) / 45011(调用频率过高)。
- ``cgi-bin/draft/add``：``articles[]`` 支持 ``article_type`` / ``title``(≤32) /
  ``author``(≤16) / ``digest``(≤120) / ``content``(≤20k 字符, 1M) /
  ``content_source_url``(≤1k) / ``thumb_media_id``(news 必填) /
  ``need_open_comment`` / ``only_fans_can_comment`` / ``image_info`` /
  ``cover_info`` / ``product_info``；返回 ``media_id``。
- ``cgi-bin/draft/batchget``：``offset`` / ``count``(1–20) / ``no_content``；
  返回 ``total_count`` / ``item_count`` / ``item[].media_id`` /
  ``item[].content.news_item[]`` / ``item[].update_time``。
- ``cgi-bin/freepublish/submit``：body ``{"media_id": ...}``，返回 ``publish_id``；
  errcode=0 **只代表任务提交成功**，不代表已发布。48001 无接口权限、
  53503/53504/53505 草稿校验类错误。
- ``cgi-bin/freepublish/get``：body ``{"publish_id": ...}``，返回 ``publish_status``
  0=成功 1=发布中 2=原创失败 3=常规失败 4=平台审核不通过
  5=成功后用户删除所有文章 6=成功后系统封禁所有文章；
  以及 ``article_id`` / ``article_detail.count`` /
  ``article_detail.item[].idx`` / ``item[].article_url`` / ``fail_idx[]``。
- ``cgi-bin/material/add_material?type=image``：multipart 字段 ``media``，
  图片 ≤10M，支持 bmp/png/jpeg/jpg/gif；返回 ``media_id`` / ``url``。
- ``cgi-bin/media/uploadimg``：multipart 字段 ``media``，仅 jpg/png 且 ≤1M，
  返回 ``url``（``mmbiz.qpic.cn`` 域名），不占用素材库配额。
- ``datacube/getusersummary``：``begin_date`` / ``end_date``（YYYY-MM-DD，
  end_date 最大为昨天），**最大时间跨度 7**。

**未核实**（官方页面未明示，取社区共识，代码中标注）：

- ``datacube/getarticletotal`` / ``datacube/getarticlesummary`` 的最大时间跨度
  （本实现按 1 天处理，即 begin_date 必须等于 end_date）；
  ``getarticlesummary`` 官方页已标注"本接口已停止维护"。
- ``media/uploadimg`` 每日 1000 次调用上限（社区口径）。
- freepublish 成功后原草稿是否仍可 ``draft/get`` 到（影响 fetch_metrics 的标题解析链路）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from publishers.base import PermanentError, RetryableError

logger = logging.getLogger("social_workflow.publishers.wechat_mp")

API_BASE = "https://api.weixin.qq.com"

# 公众号图床域名：正文里的图片必须是这些域名，否则会被平台吞掉
MMBIZ_HOSTS: tuple[str, ...] = ("mmbiz.qpic.cn", "mmbiz.qlogo.cn", "mmecoa.qpic.cn")

# 官方限制
TITLE_MAX = 32
AUTHOR_MAX = 16
DIGEST_MAX = 120
CONTENT_SOURCE_URL_MAX = 1024
UPLOADIMG_MAX_BYTES = 1 * 1024 * 1024  # 1M
UPLOADIMG_EXTS = frozenset({".jpg", ".jpeg", ".png"})
MATERIAL_IMAGE_MAX_BYTES = 10 * 1024 * 1024  # 10M
MATERIAL_IMAGE_EXTS = frozenset({".bmp", ".png", ".jpeg", ".jpg", ".gif"})
DRAFT_BATCHGET_MAX_COUNT = 20

# access_token 提前刷新的安全边界（秒）
TOKEN_SAFETY_MARGIN = 300
# stable_token 的 force_refresh 官方要求两次间隔 ≥30s、每日 ≤20 次
FORCE_REFRESH_MIN_INTERVAL = 30.0

# --- 错误码分类 -------------------------------------------------------------

# access_token 失效：刷新后重试一次
TOKEN_INVALID_ERRCODES = frozenset({40001, 40014, 42001})
# 可重试：系统繁忙 / 频率限制 / 服务不可用
RETRYABLE_ERRCODES = frozenset({-1, 45009, 45011, 45016, 45017, 61501})
# IP 白名单
ERRCODE_IP_NOT_IN_WHITELIST = 40164
# 凭据配置错误
ERRCODE_INVALID_APPID = 40013
ERRCODE_INVALID_SECRET = 40125
CREDENTIAL_ERRCODES = frozenset({ERRCODE_INVALID_APPID, ERRCODE_INVALID_SECRET})

_IP_IN_ERRMSG = re.compile(r"invalid ip ([0-9a-fA-F:.]+)")
_IMG_SRC = re.compile(r"""(<img\b[^>]*?\bsrc\s*=\s*)(["'])(.*?)\2""", re.IGNORECASE | re.DOTALL)
_MSGID = re.compile(r"^\d+(_\d+)?$")

# 北京时间：公众号后台的"昨天"以东八区为准
CST = timezone(timedelta(hours=8))


# --------------------------------------------------------------- token 缓存


@dataclass
class _CachedToken:
    value: str
    expires_at: float


class TokenCache:
    """进程内 access_token 缓存。

    公众号同一 AppID 的 token 是**全局共享**的，重复获取会互相顶掉（stable_token
    虽然与 getAccessToken 隔离，但仍有日调用上限），所以缓存必须跨 Publisher 实例，
    做成模块级单例 :data:`TOKEN_CACHE`。测试可以自己 new 一个注入。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: dict[str, _CachedToken] = {}
        self._last_force_refresh: dict[str, float] = {}

    def get(self, key: str, *, now: float | None = None) -> str | None:
        moment = now if now is not None else time.time()
        with self._lock:
            cached = self._tokens.get(key)
            if cached is None or cached.expires_at <= moment:
                return None
            return cached.value

    def set(self, key: str, value: str, expires_in: float, *, now: float | None = None) -> None:
        moment = now if now is not None else time.time()
        with self._lock:
            self._tokens[key] = _CachedToken(
                value=value, expires_at=moment + max(expires_in - TOKEN_SAFETY_MARGIN, 0.0)
            )

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._tokens.pop(key, None)

    def may_force_refresh(self, key: str, *, now: float | None = None) -> bool:
        """force_refresh 距上次不足 30s 时返回 False（官方硬限制，避免白白报错）。"""
        moment = now if now is not None else time.time()
        with self._lock:
            last = self._last_force_refresh.get(key)
            return last is None or moment - last >= FORCE_REFRESH_MIN_INTERVAL

    def note_force_refresh(self, key: str, *, now: float | None = None) -> None:
        moment = now if now is not None else time.time()
        with self._lock:
            self._last_force_refresh[key] = moment

    def clear(self) -> None:
        with self._lock:
            self._tokens.clear()
            self._last_force_refresh.clear()


TOKEN_CACHE = TokenCache()


# ------------------------------------------------------------------ 脱敏


def redact(text: str, *secrets: str) -> str:
    """把 secret / access_token 从日志文本里抹掉。"""
    out = text
    for secret in secrets:
        if secret and len(secret) >= 6:
            out = out.replace(secret, f"{secret[:4]}***{secret[-2:]}")
    # 兜底：URL query 里的 access_token
    out = re.sub(r"(access_token=)[^&\s\"']+", r"\1***", out)
    # 兜底：JSON 里的 secret / access_token 字段
    out = re.sub(r'("(?:secret|access_token)"\s*:\s*")[^"]*(")', r"\1***\2", out)
    return out


def _digest_of(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------------------ 客户端


class WechatMpClient:
    """公众号官方 API 客户端。线程安全依赖 :class:`TokenCache`，其余方法无共享状态。"""

    def __init__(
        self,
        app_id: str = "",
        app_secret: str = "",
        *,
        base_url: str = API_BASE,
        http: httpx.Client | None = None,
        timeout: float = 15.0,
        dry_run: bool = False,
        token_cache: TokenCache | None = None,
        cache_key: str | None = None,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.dry_run = dry_run
        self._token_cache = token_cache if token_cache is not None else TOKEN_CACHE
        self._cache_key = cache_key or f"{self.base_url}|{app_id}"
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

    def __enter__(self) -> WechatMpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def has_credentials(self) -> bool:
        return bool(self.app_id and self.app_secret)

    # -- 底层请求 ---------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _log_request(self, method: str, url: str, body: Any = None) -> None:
        payload = ""
        if body is not None:
            try:
                payload = json.dumps(body, ensure_ascii=False)[:2000]
            except (TypeError, ValueError):  # pragma: no cover - 非 JSON 体
                payload = repr(body)[:2000]
        logger.debug(
            "wechat_mp -> %s %s %s",
            method,
            redact(url, self.app_secret),
            redact(payload, self.app_secret),
        )

    def _log_response(self, api: str, data: Any) -> None:
        try:
            text = json.dumps(data, ensure_ascii=False)[:2000]
        except (TypeError, ValueError):  # pragma: no cover
            text = repr(data)[:2000]
        logger.debug("wechat_mp <- %s %s", api, redact(text, self.app_secret))

    def _send(self, request: httpx.Request, *, api: str) -> dict[str, Any]:
        try:
            resp = self.http.send(request)
        except httpx.TimeoutException as exc:
            raise RetryableError(f"{api} 请求超时: {exc}", raw={"api": api}) from exc
        except httpx.HTTPError as exc:
            raise RetryableError(f"{api} 网络错误: {exc}", raw={"api": api}) from exc
        if resp.status_code >= 500:
            raise RetryableError(
                f"{api} 平台 5xx: HTTP {resp.status_code}",
                raw={"api": api, "status": resp.status_code},
            )
        if resp.status_code >= 400:
            raise PermanentError(
                f"{api} HTTP {resp.status_code}: {redact(resp.text[:200], self.app_secret)}",
                raw={"api": api, "status": resp.status_code},
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise RetryableError(
                f"{api} 返回非 JSON: {redact(resp.text[:200], self.app_secret)}",
                raw={"api": api},
            ) from exc
        self._log_response(api, data)
        if not isinstance(data, dict):  # pragma: no cover - 官方 API 均返回对象
            raise PermanentError(f"{api} 返回结构异常: {type(data).__name__}", raw={"api": api})
        return data

    def _raise_for_errcode(self, data: dict[str, Any], *, api: str) -> None:
        errcode = data.get("errcode", 0)
        if not errcode:
            return
        errmsg = str(data.get("errmsg", ""))
        raw = {"api": api, "errcode": errcode, "errmsg": errmsg}

        if errcode == ERRCODE_IP_NOT_IN_WHITELIST:
            ip_match = _IP_IN_ERRMSG.search(errmsg)
            ip = ip_match.group(1) if ip_match else "未知"
            raw["egress_ip"] = ip
            raise PermanentError(
                "IP not in whitelist: "
                f"当前出口 IP {ip} 不在公众号后台白名单内（errcode=40164）。"
                "请到「公众号后台 → 开发 → 基本配置 → IP 白名单」添加；"
                "本机出口 IP 可用 `curl -s https://api.ipify.org` 查询；"
                "动态 IP 环境请改走 WENYAN_SERVER_URL 中转。详见 docs/OPS.md 第 3 节。"
                f" errmsg={errmsg}",
                raw=raw,
            )
        if errcode in CREDENTIAL_ERRCODES:
            name = "AppID" if errcode == ERRCODE_INVALID_APPID else "AppSecret"
            raise PermanentError(
                f"{name} 无效（errcode={errcode}）：请检查环境变量 "
                f"WECHAT_APP_ID / WECHAT_APP_SECRET。errmsg={errmsg}",
                raw=raw,
            )
        if errcode in RETRYABLE_ERRCODES:
            raise RetryableError(f"{api} 可重试错误 errcode={errcode} errmsg={errmsg}", raw=raw)
        if errcode in TOKEN_INVALID_ERRCODES:
            # 交给 _post_json 的刷新重试逻辑；重试后仍失败才会冒泡到这里
            raise PermanentError(
                f"{api} access_token 失效且刷新后仍失败 errcode={errcode} errmsg={errmsg}", raw=raw
            )
        raise PermanentError(f"{api} 调用失败 errcode={errcode} errmsg={errmsg}", raw=raw)

    def _post_json(self, path: str, payload: dict[str, Any], *, api: str) -> dict[str, Any]:
        """带 access_token 的 POST JSON；40001/42001 自动刷新 token 重试一次。"""
        for attempt in (1, 2):
            token = self.get_access_token(force_refresh=attempt == 2)
            url = f"{self._url(path)}?access_token={token}"
            self._log_request("POST", url, payload)
            # 微信要求 UTF-8 原文，不能被 ensure_ascii 转义
            request = self.http.build_request(
                "POST",
                url,
                content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            data = self._send(request, api=api)
            if attempt == 1 and data.get("errcode") in TOKEN_INVALID_ERRCODES:
                logger.warning("%s access_token 失效(errcode=%s)，刷新后重试", api, data["errcode"])
                self._token_cache.invalidate(self._cache_key)
                continue
            self._raise_for_errcode(data, api=api)
            return data
        raise AssertionError("unreachable")  # pragma: no cover

    def _post_file(
        self, path: str, file_path: Path, *, api: str, params: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """带 access_token 的 multipart 上传；同样支持一次 token 刷新重试。"""
        content = file_path.read_bytes()
        mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        for attempt in (1, 2):
            token = self.get_access_token(force_refresh=attempt == 2)
            query = f"access_token={token}"
            for key, value in (params or {}).items():
                query += f"&{key}={value}"
            url = f"{self._url(path)}?{query}"
            self._log_request("POST", url, {"media": f"<{file_path.name} {len(content)}B>"})
            request = self.http.build_request(
                "POST", url, files={"media": (file_path.name, content, mime)}
            )
            data = self._send(request, api=api)
            if attempt == 1 and data.get("errcode") in TOKEN_INVALID_ERRCODES:
                self._token_cache.invalidate(self._cache_key)
                continue
            self._raise_for_errcode(data, api=api)
            return data
        raise AssertionError("unreachable")  # pragma: no cover

    # -- access_token -----------------------------------------------------

    def get_access_token(self, *, force_refresh: bool = False) -> str:
        """取 access_token（``POST /cgi-bin/stable_token``），带内存缓存与过期刷新。

        - ``dry_run`` 下不联网，返回占位 token。
        - 缓存命中且未过期直接返回；``force_refresh=True`` 会强制换新，
          但受官方 30s 间隔限制保护（间隔不足时退化为普通获取）。
        """
        if self.dry_run:
            return "DRY-RUN-ACCESS-TOKEN"
        if not self.has_credentials:
            raise PermanentError(
                "未配置 WECHAT_APP_ID / WECHAT_APP_SECRET，公众号接口不可用",
                raw={"api": "stable_token", "errcode": None},
            )
        if not force_refresh:
            cached = self._token_cache.get(self._cache_key)
            if cached is not None:
                return cached

        really_force = force_refresh and self._token_cache.may_force_refresh(self._cache_key)
        if force_refresh and not really_force:
            logger.warning("stable_token force_refresh 距上次不足 30s，退化为普通获取")
        payload = {
            "grant_type": "client_credential",
            "appid": self.app_id,
            "secret": self.app_secret,
            "force_refresh": really_force,
        }
        url = self._url("/cgi-bin/stable_token")
        self._log_request("POST", url, payload)
        request = self.http.build_request(
            "POST",
            url,
            content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        data = self._send(request, api="stable_token")
        self._raise_for_errcode(data, api="stable_token")
        token = str(data.get("access_token") or "")
        if not token:
            raise RetryableError("stable_token 返回缺少 access_token", raw={"api": "stable_token"})
        expires_in = float(data.get("expires_in") or 7200)
        self._token_cache.set(self._cache_key, token, expires_in)
        if really_force:
            self._token_cache.note_force_refresh(self._cache_key)
        return token

    # -- 素材 / 图床 -------------------------------------------------------

    @staticmethod
    def _validate_image(path: Path, *, exts: frozenset[str], max_bytes: int, api: str) -> None:
        if not path.is_file():
            raise PermanentError(
                f"{api}: 图片文件不存在 {path}", raw={"api": api, "path": str(path)}
            )
        if path.suffix.lower() not in exts:
            raise PermanentError(
                f"{api}: 不支持的图片格式 {path.suffix}，允许 {sorted(exts)}",
                raw={"api": api, "path": str(path)},
            )
        size = path.stat().st_size
        if size > max_bytes:
            raise PermanentError(
                f"{api}: 图片 {path.name} 大小 {size}B 超过上限 {max_bytes}B",
                raw={"api": api, "path": str(path), "size": size},
            )

    def upload_image_for_article(self, path: str | Path) -> str:
        """``POST /cgi-bin/media/uploadimg``：正文配图上传，返回 mmbiz.qpic.cn URL。

        仅支持 jpg/png 且 ≤1M；不占用素材库 10 万张配额。
        """
        target = Path(path)
        if self.dry_run:
            logger.info("[dry_run] media/uploadimg 跳过：%s", target)
            return f"https://mmbiz.qpic.cn/dry-run/{_digest_of(str(target))}/640"
        self._validate_image(
            target, exts=UPLOADIMG_EXTS, max_bytes=UPLOADIMG_MAX_BYTES, api="media/uploadimg"
        )
        data = self._post_file("/cgi-bin/media/uploadimg", target, api="media/uploadimg")
        url = str(data.get("url") or "")
        if not url:
            raise RetryableError("media/uploadimg 返回缺少 url", raw={"api": "media/uploadimg"})
        return url

    def add_material_image(self, path: str | Path) -> str:
        """``POST /cgi-bin/material/add_material?type=image``：永久素材，返回 media_id。

        封面 ``thumb_media_id`` 必须是**永久素材**的 media_id，不能用 uploadimg 的 URL。
        """
        target = Path(path)
        if self.dry_run:
            logger.info("[dry_run] material/add_material 跳过：%s", target)
            return f"dryrun-thumb-{_digest_of(str(target))}"
        self._validate_image(
            target,
            exts=MATERIAL_IMAGE_EXTS,
            max_bytes=MATERIAL_IMAGE_MAX_BYTES,
            api="material/add_material",
        )
        data = self._post_file(
            "/cgi-bin/material/add_material",
            target,
            api="material/add_material",
            params={"type": "image"},
        )
        media_id = str(data.get("media_id") or "")
        if not media_id:
            raise RetryableError(
                "material/add_material 返回缺少 media_id", raw={"api": "material/add_material"}
            )
        return media_id

    @staticmethod
    def is_mmbiz_url(src: str) -> bool:
        host = (urlparse(src).hostname or "").lower()
        return any(host == h or host.endswith("." + h) for h in MMBIZ_HOSTS)

    def replace_external_images(self, html: str, *, base_dir: str | Path | None = None) -> str:
        """把正文里所有非 mmbiz 域名的 ``<img src>`` 上传到公众号图床后替换。

        - 本地路径（相对路径按 ``base_dir`` 解析）→ ``media/uploadimg``
        - 外链 http(s) → 先下载到临时文件再上传
        - ``data:`` 内联图保持原样（公众号会吞掉，由 review 环节拦，不在这里静默改写）
        - 同一个 src 在一次调用内只上传一次

        失败语义：本地文件缺失/格式不合法 → ``PermanentError``；
        外链下载失败 → ``RetryableError``。**不静默跳过**，否则会发出缺图的文章。
        """
        if not html:
            return html
        root = Path(base_dir) if base_dir else Path.cwd()
        cache: dict[str, str] = {}

        def _resolve(src: str) -> str:
            if src in cache:
                return cache[src]
            new_src = self._upload_one_image(src, root)
            cache[src] = new_src
            return new_src

        def _sub(match: re.Match[str]) -> str:
            prefix, quote, src = match.group(1), match.group(2), match.group(3)
            stripped = src.strip()
            if not stripped or stripped.startswith("data:") or self.is_mmbiz_url(stripped):
                return match.group(0)
            return f"{prefix}{quote}{_resolve(stripped)}{quote}"

        return _IMG_SRC.sub(_sub, html)

    def _upload_one_image(self, src: str, root: Path) -> str:
        parsed = urlparse(src)
        if parsed.scheme in ("http", "https"):
            return self.upload_image_for_article(self._download_to_temp(src))
        if parsed.scheme == "file":
            return self.upload_image_for_article(Path(parsed.path))
        local = Path(src)
        if not local.is_absolute():
            local = root / local
        return self.upload_image_for_article(local)

    def _download_to_temp(self, url: str) -> Path:
        """下载外链图片到临时文件（只读操作，dry_run 下不会走到这里）。"""
        import tempfile

        try:
            resp = self.http.get(url, timeout=self.timeout, follow_redirects=True)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise RetryableError(f"下载正文外链图片失败 {url}: {exc}", raw={"src": url}) from exc
        if len(resp.content) > UPLOADIMG_MAX_BYTES:
            raise PermanentError(
                f"外链图片 {url} 大小 {len(resp.content)}B 超过 media/uploadimg 的 1M 上限",
                raw={"src": url},
            )
        ext = Path(urlparse(url).path).suffix.lower()
        if ext not in UPLOADIMG_EXTS:
            guessed = mimetypes.guess_extension(resp.headers.get("content-type", "").split(";")[0])
            ext = guessed if guessed in UPLOADIMG_EXTS else ".jpg"
        handle = tempfile.NamedTemporaryFile(suffix=ext, delete=False)  # noqa: SIM115
        with handle:
            handle.write(resp.content)
        return Path(handle.name)

    # -- 草稿箱 -----------------------------------------------------------

    def draft_add(self, articles: list[dict[str, Any]]) -> str:
        """``POST /cgi-bin/draft/add``，返回草稿 media_id。"""
        if not articles:
            raise PermanentError("draft/add: articles 不能为空", raw={"api": "draft/add"})
        if self.dry_run:
            payload = json.dumps(articles, ensure_ascii=False, sort_keys=True)
            logger.info("[dry_run] draft/add 跳过，articles=%s", payload[:500])
            return f"dryrun-draft-{_digest_of(payload)}"
        data = self._post_json("/cgi-bin/draft/add", {"articles": articles}, api="draft/add")
        media_id = str(data.get("media_id") or "")
        if not media_id:
            raise RetryableError("draft/add 返回缺少 media_id", raw={"api": "draft/add"})
        return media_id

    def draft_get(self, media_id: str) -> dict[str, Any]:
        """``POST /cgi-bin/draft/get``，返回 ``{"news_item": [...]}``。"""
        return self._post_json("/cgi-bin/draft/get", {"media_id": media_id}, api="draft/get")

    def draft_batchget(
        self, *, offset: int = 0, count: int = DRAFT_BATCHGET_MAX_COUNT, no_content: int = 0
    ) -> dict[str, Any]:
        """``POST /cgi-bin/draft/batchget``，用于对账。count 官方限制 1–20。"""
        count = max(1, min(int(count), DRAFT_BATCHGET_MAX_COUNT))
        return self._post_json(
            "/cgi-bin/draft/batchget",
            {"offset": int(offset), "count": count, "no_content": int(no_content)},
            api="draft/batchget",
        )

    # -- 发布 -------------------------------------------------------------

    def freepublish_submit(self, media_id: str) -> str:
        """``POST /cgi-bin/freepublish/submit``，返回 publish_id。

        errcode=0 **只代表任务提交成功**，必须再轮询 :meth:`freepublish_get`。
        """
        if self.dry_run:
            logger.info("[dry_run] freepublish/submit 跳过，media_id=%s", media_id)
            return f"dryrun-publish-{_digest_of(media_id)}"
        data = self._post_json(
            "/cgi-bin/freepublish/submit", {"media_id": media_id}, api="freepublish/submit"
        )
        publish_id = str(data.get("publish_id") or "")
        if not publish_id:
            raise RetryableError(
                "freepublish/submit 返回缺少 publish_id", raw={"api": "freepublish/submit"}
            )
        return publish_id

    def freepublish_get(self, publish_id: str) -> dict[str, Any]:
        """``POST /cgi-bin/freepublish/get``，轮询发布任务状态。"""
        return self._post_json(
            "/cgi-bin/freepublish/get", {"publish_id": publish_id}, api="freepublish/get"
        )

    def freepublish_batchget(
        self, *, offset: int = 0, count: int = DRAFT_BATCHGET_MAX_COUNT, no_content: int = 0
    ) -> dict[str, Any]:
        """``POST /cgi-bin/freepublish/batchget``，已发布列表，用于对账。"""
        count = max(1, min(int(count), DRAFT_BATCHGET_MAX_COUNT))
        return self._post_json(
            "/cgi-bin/freepublish/batchget",
            {"offset": int(offset), "count": count, "no_content": int(no_content)},
            api="freepublish/batchget",
        )

    def freepublish_getarticle(self, article_id: str) -> dict[str, Any]:
        """``POST /cgi-bin/freepublish/getarticle``，取已发布图文详情。"""
        return self._post_json(
            "/cgi-bin/freepublish/getarticle",
            {"article_id": article_id},
            api="freepublish/getarticle",
        )

    # -- datacube ---------------------------------------------------------

    @staticmethod
    def yesterday(*, now: datetime | None = None) -> str:
        """datacube 的 end_date 上限是"昨天"（东八区）。"""
        moment = (now or datetime.now(UTC)).astimezone(CST)
        return (moment - timedelta(days=1)).strftime("%Y-%m-%d")

    @staticmethod
    def _check_date_range(begin_date: str, end_date: str, max_span: int, api: str) -> None:
        """官方语义：``end_date - begin_date`` 的天数差必须**小于**最大时间跨度。"""
        try:
            begin = datetime.strptime(begin_date, "%Y-%m-%d")
            end = datetime.strptime(end_date, "%Y-%m-%d")
        except ValueError as exc:
            raise PermanentError(
                f"{api}: 日期格式必须为 YYYY-MM-DD（begin={begin_date} end={end_date}）",
                raw={"api": api},
            ) from exc
        if end < begin:
            raise PermanentError(f"{api}: end_date 早于 begin_date", raw={"api": api})
        if (end - begin).days >= max_span:
            raise PermanentError(
                f"{api}: 时间跨度超限，最大时间跨度 {max_span}（差值须 < {max_span} 天），"
                f"当前 begin={begin_date} end={end_date}",
                raw={"api": api},
            )

    def _datacube(
        self, path: str, begin_date: str, end_date: str, *, max_span: int, api: str
    ) -> list[dict[str, Any]]:
        self._check_date_range(begin_date, end_date, max_span, api)
        data = self._post_json(path, {"begin_date": begin_date, "end_date": end_date}, api=api)
        items = data.get("list")
        return list(items) if isinstance(items, list) else []

    def datacube_getarticletotal(self, begin_date: str, end_date: str) -> list[dict[str, Any]]:
        """``POST /datacube/getarticletotal``：图文群发**总数据**（累计量）。

        最大时间跨度 **未核实**（官方页未明示），按社区共识取 1 天处理。
        """
        return self._datacube(
            "/datacube/getarticletotal",
            begin_date,
            end_date,
            max_span=1,  # 未核实
            api="datacube/getarticletotal",
        )

    def datacube_getarticlesummary(self, begin_date: str, end_date: str) -> list[dict[str, Any]]:
        """``POST /datacube/getarticlesummary``：图文群发每日数据。

        官方页已标注"本接口已停止维护"；最大时间跨度 **未核实**，按 1 天处理。
        """
        return self._datacube(
            "/datacube/getarticlesummary",
            begin_date,
            end_date,
            max_span=1,  # 未核实
            api="datacube/getarticlesummary",
        )

    def datacube_getusersummary(self, begin_date: str, end_date: str) -> list[dict[str, Any]]:
        """``POST /datacube/getusersummary``：用户增减数据。最大时间跨度 7（已核实）。"""
        return self._datacube(
            "/datacube/getusersummary",
            begin_date,
            end_date,
            max_span=7,
            api="datacube/getusersummary",
        )


def looks_like_msgid(value: str) -> bool:
    """datacube 的 ``msgid`` 形如 ``12003_3``（msgid_index）或纯数字。"""
    return bool(_MSGID.match(value or ""))


def client_from_settings(
    *, dry_run: bool = False, http: httpx.Client | None = None
) -> WechatMpClient:
    """按环境变量构造客户端（凭据只来自 env，绝不入库）。"""
    from core.config import get_settings

    settings = get_settings()
    return WechatMpClient(
        settings.wechat_app_id,
        settings.wechat_app_secret,
        base_url=settings.wechat_api_base,
        dry_run=dry_run,
        http=http,
    )


__all__ = [
    "API_BASE",
    "AUTHOR_MAX",
    "DIGEST_MAX",
    "ERRCODE_IP_NOT_IN_WHITELIST",
    "MMBIZ_HOSTS",
    "TITLE_MAX",
    "TOKEN_CACHE",
    "TokenCache",
    "WechatMpClient",
    "client_from_settings",
    "looks_like_msgid",
    "redact",
]
