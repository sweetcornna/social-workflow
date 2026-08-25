"""小红书发布器：实现 ``publishers.base.Publisher`` + ``SupportsInteractiveLogin``。

发布链路
--------
``prepare``（校验 + 素材路径翻译）→ ``publish``（``POST /api/v1/publish``）
→ **对账拿 note_id**（sidecar 的发布响应里没有笔记 id）→ ``fetch_metrics``。

关键设计
--------
1. **note_id 靠对账拿**：上游 ``POST /api/v1/publish`` 只回
   ``{"title","content","images":N,"status":"发布完成"}``，没有笔记 id / 链接。
   发布成功后立刻扫一遍 ``GET /api/v1/user/me`` 的最近笔记按标题命中。
2. **拿不到 id 也绝不重发**：解析失败时仍返回 ``ok=True`` + 占位 id
   （``xhs-unresolved-<hash>``），让两阶段记录落 ``done``。理由：小红书没有幂等接口，
   "重复发一篇真笔记"比"少一条指标"严重得多。占位 id 后续由
   ``metrics/collector.py`` 经 ``fetch_metrics_for_title`` 兜底修复（见 3）。
3. **定时发布**：带 ``schedule_at`` 时笔记进"待发布"，主页上还看不到，
   直接返回占位 id ``xhs-scheduled-<hash>``，等它真发出来后由指标兜底链路补回真 id。
4. **限频**：复用 ``core.scheduler.RateLimiter``（日上限 + 最小间隔）。
   publisher 侧再挡一道，是因为 ``/dev/*`` 与人工触发不一定经过调度器。
   与调度器的重复计数用 ``token`` 去重（同一 ContentItem 只记一次）。
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

from publishers.base import (
    AccountHealth,
    ContentBundle,
    NeedsReloginError,
    PermanentError,
    Publisher,
    PublishError,
    PublishResult,
    RetryableError,
    make_placeholder_qrcode_base64,
)
from publishers.xhs.client import (
    IMAGES_MAX,
    IMAGES_MIN,
    SCHEDULE_MAX_AHEAD,
    SCHEDULE_MIN_AHEAD,
    TITLE_MAX,
    VISIBILITIES,
    MediaPathMapper,
    XhsMcpClient,
    calc_title_length,
    note_url,
    parse_count,
    parse_rfc3339,
    to_rfc3339,
)

if TYPE_CHECKING:  # pragma: no cover - 只为类型标注，避免运行期循环 import
    from core.scheduler import RateLimiter

logger = logging.getLogger("social_workflow.publishers.xhs")

# platform_extra 约定键
KEY_SCHEDULE_AT = "schedule_at"
KEY_SIDECAR_IMAGES = "sidecar_images"
KEY_SIDECAR_VIDEO = "sidecar_video"
KEY_PREPARED = "xhs_prepared"
KEY_VISIBILITY = "visibility"
KEY_IS_ORIGINAL = "is_original"

# 占位 post_id 前缀：说明"发出去了但 id 还没拿到"，不是真实笔记 id
UNRESOLVED_PREFIX = "xhs-unresolved-"
SCHEDULED_PREFIX = "xhs-scheduled-"

# 发布后扫主页找 note_id 的重试次数与间隔（浏览器侧有几秒延迟）
RESOLVE_ATTEMPTS = 3
RESOLVE_INTERVAL = 2.0
# 对账 / 指标扫描的最近笔记条数
DEFAULT_RECONCILE_NOTES = 20
DEFAULT_DAILY_LIMIT = 50


def is_placeholder_post_id(post_id: str) -> bool:
    """``platform_post_id`` 是不是"还没解析出真实笔记 id"的占位值。"""
    return post_id.startswith((UNRESOLVED_PREFIX, SCHEDULED_PREFIX))


def _normalise_title(text: str) -> str:
    """标题比对口径：去首尾空白 + 折叠内部空白（页面回显会做空白归一）。"""
    return " ".join(text.split())


def _clean_tags(tags: list[str]) -> list[str]:
    """去掉 ``#`` 前缀、去空、保序去重。"""
    out: dict[str, None] = {}
    for tag in tags:
        cleaned = tag.strip().lstrip("#").strip()
        if cleaned:
            out.setdefault(cleaned, None)
    return list(out)


class XhsPublisher(Publisher):
    """小红书发布器。``platform = "xhs"``。"""

    platform: ClassVar[str] = "xhs"

    def __init__(
        self,
        account_id: str,
        *,
        dry_run: bool = False,
        client: XhsMcpClient | None = None,
        endpoint: str | None = None,
        auth_token: str | None = None,
        daily_limit: int | None = None,
        limiter: RateLimiter | None = None,
        reconcile_notes: int | None = None,
        resolve_attempts: int = RESOLVE_ATTEMPTS,
        resolve_interval: float = RESOLVE_INTERVAL,
        sleeper: Callable[[float], None] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(account_id, dry_run=dry_run)
        config = load_account_config(account_id)
        if client is None:
            from core.config import get_settings

            settings = get_settings()
            client = XhsMcpClient(
                endpoint if endpoint is not None else config.endpoint,
                auth_token=auth_token if auth_token is not None else config.auth_token,
                dry_run=dry_run,
                timeout=settings.xhs_timeout_seconds,
                publish_timeout=settings.xhs_publish_timeout_seconds,
                media_mapper=MediaPathMapper(
                    host_dir=settings.xhs_media_host_dir,
                    container_dir=settings.xhs_media_container_dir,
                ),
                account_id=account_id,
            )
        self._client = client
        self.daily_limit = daily_limit if daily_limit is not None else config.daily_limit
        self.reconcile_notes = (
            reconcile_notes if reconcile_notes is not None else config.reconcile_notes
        )
        self._limiter = limiter
        self.resolve_attempts = max(1, resolve_attempts)
        self.resolve_interval = resolve_interval
        self._sleep = sleeper or time.sleep
        self._now = now or (lambda: datetime.now(UTC))

    @property
    def client(self) -> XhsMcpClient:
        return self._client

    @property
    def limiter(self) -> RateLimiter:
        """默认复用调度器的进程内限频器，保证两条路径共用同一份计数。"""
        if self._limiter is None:
            from core.scheduler import RATE_LIMITER

            self._limiter = RATE_LIMITER
        return self._limiter

    # ------------------------------------------------------------- prepare

    def prepare(self, bundle: ContentBundle) -> ContentBundle:
        """校验标题 / 图片 / 标签 / 定时时间，并把素材路径翻译成 sidecar 可见路径。

        幂等：所有输出都是输入的确定性函数（标题只 strip、标签保序去重、
        素材路径映射无副作用），``prepare(prepare(b)) == prepare(b)``。
        """
        extra = dict(bundle.platform_extra)

        title = _normalise_title(bundle.title)
        if not title:
            raise PermanentError("小红书笔记标题不能为空", raw={"field": "title"})
        length = calc_title_length(title)
        if length > TITLE_MAX:
            raise PermanentError(
                f"小红书标题最长 {TITLE_MAX} 字（汉字记 1、两个英文字母记 1），"
                f"当前 {length} 字：{title!r}",
                raw={"field": "title", "length": length},
            )
        if not bundle.body_markdown.strip():
            raise PermanentError("小红书笔记正文不能为空", raw={"field": "body_markdown"})

        tags = _clean_tags(bundle.tags)

        videos = [m for m in bundle.media if m.kind == "video"]
        images = [m for m in bundle.media if m.kind == "image"]
        if videos:
            if len(videos) > 1:
                raise PermanentError(
                    f"小红书视频笔记只能有 1 个视频，当前 {len(videos)} 个",
                    raw={"field": "media"},
                )
            extra[KEY_SIDECAR_VIDEO] = self._client.resolve_video(videos[0].path)
            extra.pop(KEY_SIDECAR_IMAGES, None)
        else:
            if not IMAGES_MIN <= len(images) <= IMAGES_MAX:
                raise PermanentError(
                    f"小红书图文笔记需要 {IMAGES_MIN}–{IMAGES_MAX} 张图片，当前 {len(images)} 张",
                    raw={"field": "media", "count": len(images)},
                )
            extra[KEY_SIDECAR_IMAGES] = self._client.resolve_images([m.path for m in images])
            extra.pop(KEY_SIDECAR_VIDEO, None)

        schedule_at = extra.get(KEY_SCHEDULE_AT)
        if schedule_at:
            extra[KEY_SCHEDULE_AT] = to_rfc3339(self._check_schedule(schedule_at))

        visibility = str(extra.get(KEY_VISIBILITY) or "").strip()
        if visibility and visibility not in VISIBILITIES:
            raise PermanentError(
                f"visibility 取值非法: {visibility!r}，允许 {list(VISIBILITIES)}"
                "（取值未在 sidecar 侧核实，来自上游注释）",
                raw={"field": "visibility"},
            )
        if visibility:
            extra[KEY_VISIBILITY] = visibility
        else:
            extra.pop(KEY_VISIBILITY, None)

        extra[KEY_PREPARED] = True
        return bundle.model_copy(update={"title": title, "tags": tags, "platform_extra": extra})

    def _check_schedule(self, value: Any) -> datetime:
        """定时发布时间必须落在 [now+1h, now+14d]（与 sidecar 侧同口径）。"""
        if isinstance(value, datetime):
            moment = value if value.tzinfo else value.replace(tzinfo=UTC)
        else:
            try:
                moment = parse_rfc3339(str(value))
            except ValueError as exc:
                raise PermanentError(
                    f"platform_extra.schedule_at 必须是 RFC3339 时间串，收到 {value!r}",
                    raw={"field": "schedule_at"},
                ) from exc
        now = self._now()
        if moment < now + SCHEDULE_MIN_AHEAD:
            raise PermanentError(
                f"小红书定时发布至少要在 1 小时后，当前设置 {moment.isoformat()}，"
                f"最早可选 {(now + SCHEDULE_MIN_AHEAD).isoformat()}",
                raw={"field": "schedule_at"},
            )
        if moment > now + SCHEDULE_MAX_AHEAD:
            raise PermanentError(
                f"小红书定时发布不能超过 14 天，当前设置 {moment.isoformat()}，"
                f"最晚可选 {(now + SCHEDULE_MAX_AHEAD).isoformat()}",
                raw={"field": "schedule_at"},
            )
        return moment

    # ------------------------------------------------------------- publish

    def publish(self, bundle: ContentBundle) -> PublishResult:
        extra = bundle.platform_extra
        images: list[str] = list(extra.get(KEY_SIDECAR_IMAGES) or [])
        video = str(extra.get(KEY_SIDECAR_VIDEO) or "")
        if not images and not video:
            raise PermanentError(
                "publish 前必须先调用 prepare()（缺少 platform_extra.sidecar_images）",
                raw={"field": KEY_SIDECAR_IMAGES},
            )
        schedule_at = str(extra.get(KEY_SCHEDULE_AT) or "")
        if schedule_at:
            # 从批准到实际发布可能过了很久，重新校验一次窗口（sidecar 侧也会再校验）
            self._check_schedule(schedule_at)

        if self.dry_run:
            logger.info(
                "[dry_run] xhs 不发请求：title=%r 图 %d 张 schedule_at=%s",
                bundle.title,
                len(images),
                schedule_at or "-",
            )
            return PublishResult(
                ok=False,
                raw={
                    "dry_run": True,
                    "images": len(images),
                    "video": bool(video),
                    "schedule_at": schedule_at or None,
                    "hash": bundle.content_hash,
                },
            )

        self._check_rate_limit()

        common = {
            "title": bundle.title,
            "content": bundle.body_markdown,
            "tags": bundle.tags,
            "schedule_at": schedule_at or None,
            "visibility": str(extra.get(KEY_VISIBILITY) or ""),
        }
        if video:
            raw = self._client.publish_video(video=video, **common)
        else:
            raw = self._client.publish_content(
                images=images,
                is_original=bool(extra.get(KEY_IS_ORIGINAL)),
                **common,
            )

        self._record_rate_limit(bundle)

        published_at = self._now()
        if schedule_at:
            # 定时笔记还在"待发布"，主页上查不到，先给占位 id，等它上线后由指标链路补真 id
            post_id = SCHEDULED_PREFIX + bundle.content_hash[:16]
            logger.info("xhs 定时发布已提交：%s at=%s", bundle.title, schedule_at)
            return PublishResult(
                ok=True,
                platform_post_id=post_id,
                url=None,
                raw={
                    "stage": "scheduled",
                    "schedule_at": schedule_at,
                    "note_id_resolved": False,
                    "publish_response": raw,
                },
                published_at=published_at,
            )

        hit = self._resolve_note(bundle, retry=True)
        if hit is None:
            post_id = UNRESOLVED_PREFIX + bundle.content_hash[:16]
            logger.warning(
                "xhs 发布成功但没能在主页最近 %d 条里找到笔记 id（title=%r）；"
                "记为 %s，**不会重发**，指标采集时会按标题兜底解析",
                self.reconcile_notes,
                bundle.title,
                post_id,
            )
            return PublishResult(
                ok=True,
                platform_post_id=post_id,
                url=None,
                raw={
                    "stage": "published",
                    "note_id_resolved": False,
                    "publish_response": raw,
                },
                published_at=published_at,
            )
        note_id, xsec_token = hit
        return PublishResult(
            ok=True,
            platform_post_id=note_id,
            url=note_url(note_id, xsec_token),
            raw={
                "stage": "published",
                "note_id_resolved": True,
                "xsec_token_present": bool(xsec_token),
                "publish_response": raw,
            },
            published_at=published_at,
        )

    # ------------------------------------------------------------ 限频

    def _rate_token(self, bundle: ContentBundle) -> str:
        """限频去重令牌：同一 ContentItem 只计一次。

        ``ContentBundle.id`` 与 ``ContentItem.id`` 在本仓库全链路是同一个值，
        调度器发布成功后用 ``token=item.id`` 记账，这里用 ``token=bundle.id``，
        两边天然去重。万一不一致，最坏结果是**多记一次**（更保守），不会放宽限额。
        """
        return f"{self.platform}:{bundle.id}"

    def _check_rate_limit(self) -> None:
        limiter = self.limiter
        moment = self._now()
        if limiter.allow(self.account_id, self.daily_limit, now=moment):
            return
        used = limiter.used_today(self.account_id, now=moment)
        nxt = limiter.next_available_at(self.account_id)
        retry_after = None
        if nxt is not None:
            retry_after = max((nxt - moment).total_seconds(), 0.0)
        raise RetryableError(
            f"rate limited: 账号 {self.account_id} 今日已发 {used}/{self.daily_limit} 条"
            f"或未到最小发布间隔（下次可发 {nxt.isoformat() if nxt else '未知'}）",
            raw={
                "account_id": self.account_id,
                "daily_limit": self.daily_limit,
                "used_today": used,
            },
            retry_after=retry_after,
        )

    def _record_rate_limit(self, bundle: ContentBundle) -> None:
        self.limiter.record(self.account_id, now=self._now(), token=self._rate_token(bundle))

    # -------------------------------------------------------------- health

    def health(self) -> AccountHealth:
        """登录态巡检。

        - 已登录 → ``ok``
        - 未登录 → ``needs_relogin``（账号级事件，会挂起该账号所有排期项）
        - sidecar 不可达 / 浏览器抖动 → ``degraded``（**不是** needs_relogin：
          误判会白白挂起排期并催人扫码）
        """
        if self.dry_run:
            return AccountHealth(status="ok", detail="dry_run：未联网校验")
        try:
            status = self._client.login_status()
        except NeedsReloginError as exc:
            return AccountHealth(status="needs_relogin", detail=exc.message)
        except PublishError as exc:
            return AccountHealth(status="degraded", detail=f"sidecar 不可用：{exc.message or exc}")
        if not status.is_logged_in:
            return AccountHealth(
                status="needs_relogin",
                detail=(
                    f"sidecar 报告未登录，请到 /accounts/{self.account_id}/login 用小红书 App 扫码"
                ),
            )
        who = status.username or status.user_id or "未知账号"
        return AccountHealth(status="ok", detail=f"已登录：{who}")

    # ----------------------------------------------------------- reconcile

    def reconcile(self, bundle: ContentBundle) -> PublishResult | None:
        """平台侧对账：这条内容是不是其实已经发出去了？

        策略（**只读**，不碰任何写接口）：

        1. 拉 ``GET /api/v1/user/me`` 的最近 N 条笔记（``reconcile_notes``，默认 20）；
        2. 按**归一化标题**精确命中，并要求笔记类型匹配（图文 ``normal`` / 视频 ``video``）；
        3. 命中多条时，用 ``POST /api/v1/feeds/detail`` 取 ``note.time``，
           选发布时间落在对账窗口内且**最新**的一条（标题重复时不至于认错）。

        不用"首图内容 hash"：主页只给 CDN 封面 URL，平台会重编码，
        下下来的字节与本地卡片必然不同，hash 比不上；标题 + 时间窗口是可核实的信号。

        约定：明确未命中返回 ``None``；查不动（网络/5xx）抛 ``RetryableError``。
        """
        if self.dry_run:
            return None
        hit = self._resolve_note(bundle, retry=False)
        if hit is None:
            return None
        note_id, xsec_token = hit
        logger.info("xhs 对账命中：note_id=%s title=%r", note_id, bundle.title)
        return PublishResult(
            ok=True,
            platform_post_id=note_id,
            url=note_url(note_id, xsec_token),
            raw={"stage": "published", "reconciled": True, "matched_by": "title"},
            published_at=self._now(),
        )

    def _resolve_note(self, bundle: ContentBundle, *, retry: bool) -> tuple[str, str] | None:
        """在最近笔记里找这条内容，返回 ``(note_id, xsec_token)``。"""
        wanted = _normalise_title(bundle.title)
        wanted_type = "video" if bundle.platform_extra.get(KEY_SIDECAR_VIDEO) else "normal"
        attempts = self.resolve_attempts if retry else 1
        for attempt in range(1, attempts + 1):
            notes = self._client.my_notes(limit=self.reconcile_notes)
            candidates = [
                note
                for note in notes
                if _normalise_title(str((note.get("noteCard") or {}).get("displayTitle") or ""))
                == wanted
            ]
            typed = [
                note
                for note in candidates
                if str((note.get("noteCard") or {}).get("type") or "") == wanted_type
            ]
            pool = typed or candidates
            if len(pool) == 1:
                return _note_identity(pool[0])
            if len(pool) > 1:
                return _note_identity(self._newest(pool))
            if attempt < attempts:
                self._sleep(self.resolve_interval)
        return None

    def _newest(self, notes: list[dict[str, Any]]) -> dict[str, Any]:
        """同名笔记里挑发布时间最新的一条（多一次 detail 调用，只在重名时发生）。"""
        best: dict[str, Any] = notes[0]
        best_time = -1
        for note in notes:
            note_id, xsec = _note_identity(note)
            if not note_id or not xsec:
                continue
            try:
                detail = self._client.note_detail(note_id, xsec)
            except Exception as exc:  # 详情拿不到不该让对账整体失败
                logger.debug("xhs detail 取发布时间失败 %s: %s", note_id, exc)
                continue
            moment = int((detail.get("note") or {}).get("time") or 0)
            if moment > best_time:
                best_time, best = moment, note
        return best

    # ------------------------------------------------------- fetch_metrics

    def fetch_metrics(self, platform_post_id: str) -> dict[str, Any]:
        """取单条笔记的公开互动指标。

        主路径是 ``GET /api/v1/user/me``：自己主页的 feed 里就带 ``interactInfo``
        （点赞/收藏/评论/分享），比 ``feeds/detail`` 便宜得多（后者要开详情页）。
        主页最近 N 条里没有时，退回 ``feeds/detail``（需要 xsec_token）。

        小红书**不对外暴露阅读量**，``views`` 恒为 ``None``（不是 0）。
        """
        snapshot_at = self._now().isoformat()
        if self.dry_run:
            return _empty_metrics(snapshot_at, "dry_run：不发请求", post_id=platform_post_id)
        if is_placeholder_post_id(platform_post_id):
            return _empty_metrics(
                snapshot_at,
                f"platform_post_id={platform_post_id!r} 是占位值（发布时未解析出笔记 id），"
                "采集器会调 fetch_metrics_for_title 按标题兜底",
                post_id=platform_post_id,
            )
        try:
            notes = self._client.my_notes(limit=self.reconcile_notes)
        except Exception as exc:
            return _empty_metrics(snapshot_at, f"user/me 调用失败：{exc}", post_id=platform_post_id)
        for note in notes:
            if str(note.get("id") or "") == platform_post_id:
                return _metrics_from_feed(note, snapshot_at, source="user/me")
        return self._metrics_via_detail(platform_post_id, snapshot_at)

    def fetch_metrics_for_title(self, title: str) -> dict[str, Any]:
        """按标题兜底取指标（**可选能力**，``metrics/collector.py`` 用 hasattr 探测）。

        用于两种情况：发布时 note_id 没解析出来（占位 id），或笔记 id 变了。
        命中时返回的 dict 里带真实 ``platform_post_id``，采集器据此回填发布记录。
        """
        snapshot_at = self._now().isoformat()
        if self.dry_run:
            return _empty_metrics(snapshot_at, "dry_run：不发请求")
        wanted = _normalise_title(title)
        try:
            notes = self._client.my_notes(limit=self.reconcile_notes)
        except Exception as exc:
            return _empty_metrics(snapshot_at, f"user/me 调用失败：{exc}")
        for note in notes:
            display = str((note.get("noteCard") or {}).get("displayTitle") or "")
            if _normalise_title(display) == wanted:
                return _metrics_from_feed(note, snapshot_at, source="user/me(title)")
        return _empty_metrics(
            snapshot_at, f"主页最近 {self.reconcile_notes} 条里没有标题 {wanted!r} 的笔记"
        )

    def _metrics_via_detail(self, note_id: str, snapshot_at: str) -> dict[str, Any]:
        """主页里找不到时退回详情页。需要 xsec_token，取不到就如实报缺失。"""
        try:
            notes = self._client.my_notes(limit=self.reconcile_notes)
        except Exception as exc:  # pragma: no cover - 上面刚调过，几乎不会走到
            return _empty_metrics(snapshot_at, f"user/me 调用失败：{exc}", post_id=note_id)
        token = ""
        for note in notes:
            if str(note.get("id") or "") == note_id:
                token = str(note.get("xsecToken") or "")
                break
        if not token:
            return _empty_metrics(
                snapshot_at,
                f"主页最近 {self.reconcile_notes} 条里没有 note_id={note_id}，"
                "且没有可用的 xsec_token，无法打开详情页",
                post_id=note_id,
            )
        try:
            detail = self._client.note_detail(note_id, token)
        except Exception as exc:
            return _empty_metrics(snapshot_at, f"feeds/detail 调用失败：{exc}", post_id=note_id)
        note = detail.get("note") or {}
        return _metrics_from_interact(
            note.get("interactInfo") or {},
            snapshot_at,
            post_id=note_id,
            title=str(note.get("title") or ""),
            source="feeds/detail",
        )

    # -------------------------------------------------- 人工介入通道（扫码）

    def get_login_qrcode(self) -> str:
        """返回登录二维码 PNG 的 base64（不含 data URI 前缀）。

        红线：只把二维码呈现给人，人用**自己的**小红书 App 扫。
        本项目不做任何验证码识别 / 自动登录（见 docs/POLICY.md）。
        """
        return self.get_login_qrcode_detail()["image_base64"]

    def get_login_qrcode_detail(self) -> dict[str, Any]:
        """带超时时间的二维码信息，供 ``/accounts/{id}/login/qrcode`` 显示倒计时。

        不属于 P0 冻结契约，是可选能力（``core.main`` 用 hasattr 探测）。
        """
        if self.dry_run:
            return {
                "image_base64": make_placeholder_qrcode_base64(f"xhs:{self.account_id}"),
                "timeout_seconds": 240.0,
                "is_logged_in": False,
                "placeholder": True,
                "detail": "dry_run：返回占位图，未调用 sidecar",
            }
        info = self._client.get_login_qrcode()
        return {
            "image_base64": info.image_base64,
            "timeout_seconds": info.timeout_seconds,
            "is_logged_in": info.is_logged_in,
            "placeholder": False,
            "detail": "请用**该账号本人**的小红书 App 扫码",
        }

    def check_login_status(self) -> AccountHealth:
        return self.health()

    def submit_sms_code(self, code: str) -> bool:
        """小红书走扫码登录，没有短信验证码通道。"""
        raise PermanentError(
            "小红书发布器不接受短信验证码：登录只走 App 扫码（短信通道是抖音用的）",
            raw={"account_id": self.account_id},
        )


def _note_identity(note: dict[str, Any]) -> tuple[str, str]:
    return str(note.get("id") or ""), str(note.get("xsecToken") or "")


# ---------------------------------------------------------------- 指标归一化


def _empty_metrics(snapshot_at: str, reason: str, *, post_id: str | None = None) -> dict[str, Any]:
    """指标缺失时的统一结构：字段一律 ``None``，**不伪造 0**。"""
    return {
        "views": None,
        "likes": None,
        "comments": None,
        "shares": None,
        "collects": None,
        "follows": None,
        "snapshot_at": snapshot_at,
        "available": False,
        "reason": reason,
        "platform": "xhs",
        "source": None,
        "platform_post_id": post_id,
    }


def _metrics_from_feed(note: dict[str, Any], snapshot_at: str, *, source: str) -> dict[str, Any]:
    card = note.get("noteCard") or {}
    return _metrics_from_interact(
        card.get("interactInfo") or {},
        snapshot_at,
        post_id=str(note.get("id") or "") or None,
        title=str(card.get("displayTitle") or ""),
        source=source,
        xsec_token=str(note.get("xsecToken") or ""),
        note_type=str(card.get("type") or ""),
    )


def _metrics_from_interact(
    interact: dict[str, Any],
    snapshot_at: str,
    *,
    post_id: str | None,
    title: str,
    source: str,
    xsec_token: str = "",
    note_type: str = "",
) -> dict[str, Any]:
    """把 ``interactInfo`` 折算成统一指标。

    平台给的是字符串（``"1.2万"``），:func:`publishers.xhs.client.parse_count` 负责换算；
    换不动的返回 ``None``。小红书不公开阅读量，``views`` 恒为 ``None``。
    """
    likes = parse_count(interact.get("likedCount"))
    collects = parse_count(interact.get("collectedCount"))
    comments = parse_count(interact.get("commentCount"))
    shares = parse_count(interact.get("sharedCount"))
    available = any(v is not None for v in (likes, collects, comments, shares))
    return {
        "views": None,  # 小红书不对外暴露阅读量
        "likes": likes,
        "comments": comments,
        "shares": shares,
        "collects": collects,
        "follows": None,  # 关注是账号级指标，见 user/me 的 interactions
        "snapshot_at": snapshot_at,
        "available": available,
        "reason": None if available else "interactInfo 里没有可解析的计数",
        "platform": "xhs",
        "source": source,
        "platform_post_id": post_id,
        "title": title or None,
        "url": note_url(post_id, xsec_token) if post_id else None,
        "xhs": {
            "note_type": note_type or None,
            "liked_count_raw": interact.get("likedCount"),
            "collected_count_raw": interact.get("collectedCount"),
            "comment_count_raw": interact.get("commentCount"),
            "shared_count_raw": interact.get("sharedCount"),
        },
    }


# ------------------------------------------------------------- 账号配置解析


class XhsAccountConfig:
    """一个 xhs 账号的运行期配置：sidecar 地址、鉴权 token、限频。

    - ``sidecar_endpoint`` 优先取 ``XHS_MCP_ENDPOINTS``（运维口径），其次取 DB 的
      ``Account.sidecar_endpoint``（台账口径）。
    - **token 绝不入库**：只从环境变量来。默认读 ``XHS_AUTH_TOKEN``；
      每个 sidecar 用不同 token 时，用 ``XHS_MCP_TOKENS=acc1=t1,acc2=t2``，
      或在 ``Account.extra["xhs"]["auth_token_env"]`` 里写**环境变量名**（不是值）。
    - ``daily_limit`` 取 DB 的 ``Account.daily_limit``，缺省 50（计划 2.3：小红书日 ≤ 50）。
    """

    def __init__(
        self,
        *,
        endpoint: str = "",
        auth_token: str = "",
        daily_limit: int = DEFAULT_DAILY_LIMIT,
        reconcile_notes: int = DEFAULT_RECONCILE_NOTES,
    ) -> None:
        self.endpoint = endpoint
        self.auth_token = auth_token
        self.daily_limit = daily_limit
        self.reconcile_notes = reconcile_notes


def load_account_config(account_id: str) -> XhsAccountConfig:
    """从环境变量 + accounts 表拼出该账号的配置。DB 不可用时静默退回默认值。"""
    import os

    from core.config import get_settings

    settings = get_settings()
    endpoint = settings.xhs_endpoint_map().get(account_id, "")
    token = settings.xhs_token_map().get(account_id, "") or settings.xhs_auth_token
    daily_limit = 0
    extra: dict[str, Any] = {}

    try:
        from core import db
        from core.models import Account

        with db.get_session_factory()() as session:
            account = session.get(Account, account_id)
            if account is not None:
                endpoint = endpoint or (account.sidecar_endpoint or "")
                daily_limit = int(account.daily_limit or 0)
                extra = dict(account.extra or {})
    except Exception as exc:  # DB 未初始化（契约测试 / 纯离线调用）时不该炸
        logger.debug("读取账号 %s 配置失败，使用默认值: %s", account_id, exc)

    xhs_extra = extra.get("xhs") if isinstance(extra.get("xhs"), dict) else {}
    env_name = str(xhs_extra.get("auth_token_env") or "")
    if env_name:
        token = os.environ.get(env_name, "") or token

    return XhsAccountConfig(
        endpoint=endpoint,
        auth_token=token,
        daily_limit=daily_limit or settings.xhs_daily_limit,
        reconcile_notes=settings.xhs_reconcile_notes,
    )


def content_fingerprint(bundle: ContentBundle) -> str:
    """内容指纹（对账日志用）：标题 + 正文首 120 字。"""
    payload = f"{_normalise_title(bundle.title)}|{bundle.body_markdown[:120]}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "DEFAULT_DAILY_LIMIT",
    "KEY_SCHEDULE_AT",
    "KEY_SIDECAR_IMAGES",
    "SCHEDULED_PREFIX",
    "UNRESOLVED_PREFIX",
    "XhsAccountConfig",
    "XhsPublisher",
    "content_fingerprint",
    "is_placeholder_post_id",
    "load_account_config",
]
