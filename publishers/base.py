"""发布层冻结契约（P0）。

本模块定义所有平台发布器共用的 DTO、异常分类与抽象基类。
**契约冻结**：P1 之后新增平台只允许通过 ``platform_extra`` 携带平台特有字段，
不得改动 :class:`ContentBundle` / :class:`PublishResult` 的既有字段语义，
任何实现都必须通过 ``tests/contract/test_publisher_contract.py``。
"""

from __future__ import annotations

import base64
import hashlib
import struct
import zlib
from abc import ABC, abstractmethod
from collections.abc import Iterable
from datetime import datetime
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from core.models import utcnow

Platform = Literal["wechat_mp", "xhs", "douyin"]
PLATFORMS: tuple[str, ...] = ("wechat_mp", "xhs", "douyin")

MediaKind = Literal["image", "video"]

AccountStatus = Literal["ok", "degraded", "needs_relogin", "banned"]
ACCOUNT_STATUSES: tuple[str, ...] = ("ok", "degraded", "needs_relogin", "banned")


# --------------------------------------------------------------------------- DTO


class MediaAsset(BaseModel):
    """单个媒体资源。``path`` 为本地文件路径或已上传后的平台 URL。"""

    model_config = ConfigDict(frozen=True)

    path: str
    kind: MediaKind = "image"
    cover: bool = False


class ContentBundle(BaseModel):
    """跨平台内容包：文本 + 媒体 + 平台特有字段。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    account_id: str
    platform: Platform
    title: str
    body_markdown: str
    body_html: str | None = None
    media: list[MediaAsset] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    # 平台特有字段容器：契约冻结后的唯一扩展点（如 xhs 的 schedule_at、公众号的 thumb_media_id）
    platform_extra: dict[str, Any] = Field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        """内容指纹 = sha256(标题 + 正文 + 媒体路径序列)，用于幂等键。

        故意不纳入 ``platform_extra`` 与 ``tags``：改标签或改定时槽位不应视为新内容，
        而定时槽位由 ``make_idem_key`` 单独参与哈希。
        """
        h = hashlib.sha256()
        h.update(self.title.encode("utf-8"))
        h.update(b"\x00")
        h.update(self.body_markdown.encode("utf-8"))
        for asset in self.media:
            h.update(b"\x00")
            h.update(asset.path.encode("utf-8"))
        return h.hexdigest()

    @property
    def cover(self) -> MediaAsset | None:
        """封面：显式标记 cover 的第一个资源，否则第一张图。"""
        for asset in self.media:
            if asset.cover:
                return asset
        for asset in self.media:
            if asset.kind == "image":
                return asset
        return None


class PublishResult(BaseModel):
    """一次发布尝试的结果。``ok=False`` 只用于 dry-run 或明确的"未发布但非异常"。"""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    platform_post_id: str | None = None
    url: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)
    published_at: datetime | None = None


class AccountHealth(BaseModel):
    """账号健康状态，对应 Account 状态机。"""

    model_config = ConfigDict(extra="forbid")

    status: AccountStatus
    detail: str = ""


# ----------------------------------------------------------------------- 异常分类


class PublishError(Exception):
    """发布层异常基类。调度器只按下面三个子类分流，不识别其它异常。"""

    def __init__(self, message: str = "", *, raw: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.raw = raw or {}


class RetryableError(PublishError):
    """可重试：网络抖动、平台 5xx、限频。计入 attempts，超过上限进死信。"""

    def __init__(
        self,
        message: str = "",
        *,
        raw: dict[str, Any] | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, raw=raw)
        self.retry_after = retry_after


class NeedsReloginError(PublishError):
    """登录态失效：账号级事件，触发 Account -> needs_relogin 并挂起该账号排期项。"""


class PermanentError(PublishError):
    """不可重试：内容违规、参数非法、权限不足（如未认证号调 freepublish）。直接进死信。"""


class PublisherNotAvailable(LookupError):
    """请求的平台发布器尚未实现或未注册。"""


# --------------------------------------------------------------- 占位 post_id

# 小红书 / 抖音都没有幂等发布接口，发布响应里也不一定带内容 id。拿不到 id 时**绝不重发**
# （重复发一篇真笔记 / 一条真视频，比少一条指标严重得多），改记一个占位 id，
# 让两阶段记录照常落 done，随后由 metrics/collector.py 按标题兜底解析并回填真 id。
# 各平台自己拼 ``<platform>-unresolved-<hash>`` / ``<platform>-scheduled-<hash>``，
# 这里只提供跨平台的识别函数（不属于 DTO，不影响 P0 冻结的 ContentBundle/PublishResult）。
UNRESOLVED_MARKER = "-unresolved-"
SCHEDULED_MARKER = "-scheduled-"


def is_placeholder_post_id(post_id: str) -> bool:
    """``platform_post_id`` 是不是"发布时没解析出真实内容 id"的占位值。"""
    return UNRESOLVED_MARKER in post_id or SCHEDULED_MARKER in post_id


# ------------------------------------------------------------------ 人工介入通道


@runtime_checkable
class SupportsInteractiveLogin(Protocol):
    """支持人工介入登录的发布器（扫码 / 短信验证码）。

    红线：只提供"把二维码显示给人、把人输入的验证码转发给发布器"的通道，
    **禁止**任何自动打码 / 验证码识别，见 docs/POLICY.md。
    """

    def get_login_qrcode(self) -> str:
        """返回 PNG 图片的 base64（不含 data URI 前缀）。"""
        ...

    def check_login_status(self) -> AccountHealth:
        """轮询登录结果。"""
        ...

    def submit_sms_code(self, code: str) -> bool:
        """把人工输入的短信验证码交给发布器。"""
        ...


# ------------------------------------------------------------------------- 基类


class Publisher(ABC):
    """平台发布器抽象基类。

    生命周期：``prepare`` -> （幂等记录 in_flight）-> ``publish`` -> 成功补 post_id/url。
    重试前调用 ``reconcile`` 做平台侧对账，防"发成功但回包丢失"导致重复发布。
    """

    platform: ClassVar[str] = ""

    def __init__(self, account_id: str, *, dry_run: bool = False) -> None:
        self.account_id = account_id
        self._dry_run = dry_run

    @property
    def dry_run(self) -> bool:
        """dry-run 模式下 ``publish`` 必须走完整校验但不触发平台侧写操作。"""
        return self._dry_run

    @abstractmethod
    def prepare(self, bundle: ContentBundle) -> ContentBundle:
        """归一化 / 校验内容包（上传素材、渲染 HTML、裁剪标题等）。

        契约要求**幂等**：``prepare(prepare(b)) == prepare(b)``。
        """

    @abstractmethod
    def publish(self, bundle: ContentBundle) -> PublishResult:
        """执行发布。失败必须抛 :class:`PublishError` 的子类，不允许返回 ok=False 表达异常。"""

    @abstractmethod
    def health(self) -> AccountHealth:
        """账号健康巡检（登录态、限流、封禁）。"""

    @abstractmethod
    def fetch_metrics(self, platform_post_id: str) -> dict[str, Any]:
        """拉取单条内容的公开指标。

        真实实现返回只含合法 JSON 值的普通 ``dict``，所有 key 必须是 exact ``str``，
        scalar 必须是 exact ``None``/``str``/``bool``/``int``/``float``，嵌套容器只能是
        dict/list（tuple 会由采集器复制成 list）。可选诊断字段 ``available`` 只有明确为
        ``False`` 时表示本次没有数据；字段缺失仍是成功，兼容旧实现与
        :class:`FakePublisher`。采集器只归一化一次：先把任意 Mapping 复制成内建 JSON
        值，再交给 serializer、SQLAlchemy 和 post-id 修复；非 Mapping、环、非 exact key/
        scalar 或协议异常会隔离该内容而不落快照。这里的返回注解和 ABC 签名保持冻结。
        """

    def reconcile(self, bundle: ContentBundle) -> PublishResult | None:
        """平台侧对账：查该内容是否其实已发布成功。

        约定：命中返回 ``PublishResult(ok=True, platform_post_id=...)``；
        明确未发布返回 ``None``；查不动（网络失败）抛 :class:`RetryableError`。
        默认实现返回 ``None``（保守：不认为已发布），实现方应尽量覆写。
        """
        return None

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<{type(self).__name__} platform={self.platform} account={self.account_id}>"


# -------------------------------------------------------------- 占位二维码 PNG


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def make_placeholder_qrcode_png(seed: str, *, modules: int = 21, scale: int = 8) -> bytes:
    """生成一张确定性的黑白方块占位图（**不是**可扫描的真二维码）。

    P0 用它把 ``/accounts/{id}/login`` 的轮询链路跑通；
    P2 接入 xiaohongshu-mcp 的 ``get_login_qrcode`` 后替换为平台真图。
    """
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    bits: list[list[int]] = []
    for y in range(modules):
        row = []
        for x in range(modules):
            # 三个定位角画实心方框，其余按 hash 铺点，视觉上像二维码
            in_finder = (
                (x < 7 and y < 7) or (x >= modules - 7 and y < 7) or (x < 7 and y >= modules - 7)
            )
            if in_finder:
                fx = x if x < 7 else x - (modules - 7)
                fy = y if y < 7 else y - (modules - 7)
                on = fx in (0, 6) or fy in (0, 6) or (2 <= fx <= 4 and 2 <= fy <= 4)
            else:
                on = bool(digest[(y * modules + x) % len(digest)] >> (x % 8) & 1)
            row.append(0 if on else 1)
        bits.append(row)

    quiet = 2
    side = (modules + quiet * 2) * scale
    raw = bytearray()
    for y in range(side):
        raw.append(0)  # 每行 filter type = 0
        my = y // scale - quiet
        for x in range(side):
            mx = x // scale - quiet
            inside = 0 <= mx < modules and 0 <= my < modules
            raw.append((255 if bits[my][mx] else 0) if inside else 255)

    ihdr = struct.pack(">IIBBBBB", side, side, 8, 0, 0, 0, 0)  # 8bit 灰度
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _png_chunk(b"IEND", b"")
    )


def make_placeholder_qrcode_base64(seed: str) -> str:
    return base64.b64encode(make_placeholder_qrcode_png(seed)).decode("ascii")


# ------------------------------------------------------------------ FakePublisher


class FakePublisher(Publisher):
    """可配置的假发布器：契约测试基准 + 本地联调用。

    典型用法::

        FakePublisher("acc-1", platform="xhs")                       # 一直成功
        FakePublisher("acc-1", raise_exc=RetryableError("502"))      # 一直抛可重试
        FakePublisher("acc-1", raise_exc=RetryableError(), raise_times=2)  # 前两次失败
        FakePublisher("acc-1", raise_exc=RetryableError(),
                      reconcile_result=PublishResult(ok=True, platform_post_id="p1"))
    """

    platform: ClassVar[str] = "fake"

    def __init__(
        self,
        account_id: str = "acc-fake",
        *,
        platform: str = "xhs",
        dry_run: bool = False,
        raise_exc: PublishError | None = None,
        raise_times: int | None = None,
        reconcile_result: PublishResult | None = None,
        health_status: AccountStatus = "ok",
        health_detail: str = "",
        metrics: dict[str, Any] | None = None,
        post_id_prefix: str = "fake",
    ) -> None:
        super().__init__(account_id, dry_run=dry_run)
        self.platform = platform  # 实例级覆盖，便于按平台注册
        self.raise_exc = raise_exc
        # None 表示一直抛；整数 N 表示前 N 次抛，之后成功
        self.raise_times = raise_times
        self.reconcile_result = reconcile_result
        self.health_status: AccountStatus = health_status
        self.health_detail = health_detail
        self.metrics = metrics or {"views": 100, "likes": 10, "comments": 1, "shares": 0}
        self.post_id_prefix = post_id_prefix
        # 调用计数，便于测试断言
        self.publish_calls = 0
        self.prepare_calls = 0
        self.reconcile_calls = 0
        self.sms_codes: list[str] = []

    # -- 契约实现 ----------------------------------------------------------

    def prepare(self, bundle: ContentBundle) -> ContentBundle:
        self.prepare_calls += 1
        title = bundle.title.strip()[:64]
        tags = list(dict.fromkeys(t.strip().lstrip("#") for t in bundle.tags if t.strip()))
        extra = dict(bundle.platform_extra)
        extra["prepared"] = True
        return bundle.model_copy(update={"title": title, "tags": tags, "platform_extra": extra})

    def publish(self, bundle: ContentBundle) -> PublishResult:
        self.publish_calls += 1
        if self._should_raise():
            assert self.raise_exc is not None
            raise self.raise_exc
        if self.dry_run:
            return PublishResult(ok=False, raw={"dry_run": True, "hash": bundle.content_hash})
        post_id = f"{self.post_id_prefix}-{bundle.content_hash[:12]}"
        return PublishResult(
            ok=True,
            platform_post_id=post_id,
            url=f"https://example.invalid/{self.platform}/{post_id}",
            raw={"echo": bundle.title},
            published_at=utcnow(),
        )

    def health(self) -> AccountHealth:
        return AccountHealth(status=self.health_status, detail=self.health_detail)

    def fetch_metrics(self, platform_post_id: str) -> dict[str, Any]:
        return {"platform_post_id": platform_post_id, **self.metrics}

    def reconcile(self, bundle: ContentBundle) -> PublishResult | None:
        self.reconcile_calls += 1
        return self.reconcile_result

    # -- 人工介入通道 ------------------------------------------------------

    def get_login_qrcode(self) -> str:
        return make_placeholder_qrcode_base64(f"{self.platform}:{self.account_id}")

    def check_login_status(self) -> AccountHealth:
        return self.health()

    def submit_sms_code(self, code: str) -> bool:
        self.sms_codes.append(code)
        return True

    # -- 内部 --------------------------------------------------------------

    def _should_raise(self) -> bool:
        if self.raise_exc is None:
            return False
        if self.raise_times is None:
            return True
        return self.publish_calls <= self.raise_times


def bundle_from_dict(data: dict[str, Any]) -> ContentBundle:
    """从 ContentItem.bundle_json 还原内容包。"""
    return ContentBundle.model_validate(data)


def bundles_equal(left: ContentBundle, right: ContentBundle) -> bool:
    return left.model_dump() == right.model_dump()


def iter_media_paths(bundle: ContentBundle) -> Iterable[str]:
    return (asset.path for asset in bundle.media)


__all__ = [
    "ACCOUNT_STATUSES",
    "PLATFORMS",
    "SCHEDULED_MARKER",
    "UNRESOLVED_MARKER",
    "AccountHealth",
    "ContentBundle",
    "FakePublisher",
    "MediaAsset",
    "NeedsReloginError",
    "PermanentError",
    "Platform",
    "PublishError",
    "PublishResult",
    "Publisher",
    "PublisherNotAvailable",
    "RetryableError",
    "SupportsInteractiveLogin",
    "bundle_from_dict",
    "bundles_equal",
    "is_placeholder_post_id",
    "make_placeholder_qrcode_base64",
    "make_placeholder_qrcode_png",
]
