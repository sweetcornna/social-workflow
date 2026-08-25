"""抖音发布器：实现 ``publishers.base.Publisher`` + ``SupportsInteractiveLogin``。

发布链路
--------
``prepare``（成片/标题/话题校验 + 宿主机路径翻译）→ ``publish``（调宿主机上传器
``POST /accounts/{id}/publish``）→ 上传器回作品 id（拿不到则占位）→ ``fetch_metrics``。

关键设计
--------
1. **浏览器不在 core 里**：core 只发本地 HTTP，浏览器跑在宿主机常驻进程
   （`python -m publishers.douyin serve`）。本模块不 import patchright。
2. **identity 校验防发错号**：``Account.extra["identity_hint"]`` 写该账号昵称，
   上传器发布前会读页面昵称比对，不一致直接 ``PermanentError``。
   ``health()`` 也顺带核一次，但**只报 degraded 不报 banned**——``banned`` 是人工终态，
   状态机不允许自动恢复，靠一次昵称读取就把账号钉死代价太大。
3. **拿不到作品 id 也绝不重发**：抖音没有幂等接口，"重复发一条真视频"比"少一条指标"
   严重得多。解析不出 id 时返回占位 ``douyin-unresolved-<hash>``，两阶段记录照样落
   ``done``，随后由 ``metrics/collector.py`` 经 ``fetch_metrics_for_title`` 兜底修复。
4. **限频比其它平台更保守**：日上限走共享 ``RATE_LIMITER``（与调度器共用计数，
   ``token`` 去重），**最小间隔另设 30 分钟**（``MIN_INTERVAL_GATE``）——全局的
   ``SW_MIN_PUBLISH_INTERVAL_SECONDS`` 默认才 15 分钟，对抖音太松。
5. **需要真人的一切都抛 NeedsReloginError**：未登录 / 短信二次验证 / 图形验证，
   统一走"挂起该账号排期 + 通知去 /accounts/{id}/login"这条既有通道，
   **绝不自动打码**（docs/POLICY.md）。
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta
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
)
from publishers.douyin.client import (
    DESCRIPTION_MAX,
    HASHTAG_MAX,
    SCHEDULE_MAX_AHEAD,
    SCHEDULE_MIN_AHEAD,
    STATE_LOGGED_IN,
    STATE_LOGGED_OUT,
    STATE_NEEDS_CAPTCHA,
    STATE_NEEDS_SMS,
    STATE_SCHEDULED,
    TITLE_MAX,
    DouyinServiceClient,
    HostPathMapper,
    clean_hashtags,
    mask_nickname,
    normalise_title,
    parse_rfc3339,
    to_rfc3339,
    video_url,
)

if TYPE_CHECKING:  # pragma: no cover - 只为类型标注，避免运行期循环 import
    from core.scheduler import RateLimiter

logger = logging.getLogger("social_workflow.publishers.douyin")

# platform_extra 约定键
KEY_HOST_VIDEO = "douyin_host_video"
KEY_HOST_COVER = "douyin_host_cover"
KEY_HASHTAGS = "douyin_hashtags"
KEY_DESCRIPTION = "douyin_description"
KEY_SCHEDULE_AT = "schedule_at"
KEY_PREPARED = "douyin_prepared"

# 占位 post_id 前缀：说明"发出去了但作品 id 还没拿到"，不是真实 aweme_id
UNRESOLVED_PREFIX = "douyin-unresolved-"
SCHEDULED_PREFIX = "douyin-scheduled-"

# 计划 2.3 / docs/POLICY.md：抖音日 <= 2（测试期口径）。任务书给的上限是 10，
# 两者取严：默认 2，且无论 accounts 表怎么填都不超过 DAILY_LIMIT_CEILING。
DEFAULT_DAILY_LIMIT = 2
DAILY_LIMIT_CEILING = 10
DEFAULT_MIN_INTERVAL_MINUTES = 30
DEFAULT_RECENT_POSTS = 20
# 对账时"多久以内的作品才算这次发的"
DEFAULT_RECONCILE_WINDOW_HOURS = 24


def is_placeholder_post_id(post_id: str) -> bool:
    """``platform_post_id`` 是不是"还没解析出真实作品 id"的占位值。"""
    return post_id.startswith((UNRESOLVED_PREFIX, SCHEDULED_PREFIX))


# --------------------------------------------------------------- 最小间隔闸门


class MinIntervalGate:
    """抖音专属的"两次发布最小间隔"闸门（默认 30 分钟）。

    为什么不直接用 ``RateLimiter.min_interval``：那是全局值
    （``SW_MIN_PUBLISH_INTERVAL_SECONDS``，默认 15 分钟），三个平台共用。
    抖音要更保守，但**日计数仍必须与调度器共享**（否则限额会被算两遍），
    所以只把"最小间隔"这一维单拎出来，日上限照旧走 ``RATE_LIMITER``。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: dict[str, datetime] = {}

    def wait_seconds(self, account_id: str, interval: timedelta, now: datetime) -> float:
        """还需要等多少秒才允许发下一条。0 表示可以发。"""
        with self._lock:
            last = self._last.get(account_id)
        if last is None:
            return 0.0
        remaining = (last + interval - now).total_seconds()
        return max(remaining, 0.0)

    def record(self, account_id: str, now: datetime) -> None:
        with self._lock:
            self._last[account_id] = now

    def reset(self) -> None:
        with self._lock:
            self._last.clear()


MIN_INTERVAL_GATE = MinIntervalGate()


# ------------------------------------------------------------------ 发布器


class DouyinPublisher(Publisher):
    """抖音发布器。``platform = "douyin"``。"""

    platform: ClassVar[str] = "douyin"

    def __init__(
        self,
        account_id: str,
        *,
        dry_run: bool = False,
        client: DouyinServiceClient | None = None,
        service_url: str | None = None,
        identity_hint: str | None = None,
        daily_limit: int | None = None,
        min_interval_minutes: int | None = None,
        limiter: RateLimiter | None = None,
        gate: MinIntervalGate | None = None,
        recent_posts: int | None = None,
        reconcile_window_hours: int | None = None,
        now: Any = None,
    ) -> None:
        super().__init__(account_id, dry_run=dry_run)
        config = load_account_config(account_id)
        if client is None:
            from core.config import get_settings

            settings = get_settings()
            client = DouyinServiceClient(
                service_url if service_url is not None else config.service_url,
                dry_run=dry_run,
                timeout=settings.douyin_timeout_seconds,
                publish_timeout=settings.douyin_publish_timeout_seconds,
                account_id=account_id,
                path_mapper=HostPathMapper(
                    local_dir=settings.douyin_media_local_dir,
                    host_dir=settings.douyin_media_host_dir,
                ),
            )
        self._client = client
        self.identity_hint = identity_hint if identity_hint is not None else config.identity_hint
        limit = daily_limit if daily_limit is not None else config.daily_limit
        self.daily_limit = min(max(int(limit), 0), DAILY_LIMIT_CEILING)
        minutes = (
            min_interval_minutes
            if min_interval_minutes is not None
            else config.min_interval_minutes
        )
        self.min_interval = timedelta(minutes=max(int(minutes), 0))
        self.recent_posts = recent_posts if recent_posts is not None else config.recent_posts
        self.reconcile_window = timedelta(
            hours=(
                reconcile_window_hours
                if reconcile_window_hours is not None
                else config.reconcile_window_hours
            )
        )
        self._limiter = limiter
        self._gate = gate or MIN_INTERVAL_GATE
        self._now = now or (lambda: datetime.now(UTC))

    @property
    def client(self) -> DouyinServiceClient:
        return self._client

    @property
    def limiter(self) -> RateLimiter:
        """默认复用调度器的进程内限频器，保证两条路径共用同一份日计数。"""
        if self._limiter is None:
            from core.scheduler import RATE_LIMITER

            self._limiter = RATE_LIMITER
        return self._limiter

    # ------------------------------------------------------------- prepare

    def prepare(self, bundle: ContentBundle) -> ContentBundle:
        """校验标题 / 成片 / 封面 / 话题 / 定时，并把素材路径翻译成宿主机可见路径。

        幂等：所有输出都是输入的确定性函数（标题只做空白归一、话题保序去重后截断、
        路径映射无副作用），``prepare(prepare(b)) == prepare(b)``。
        """
        extra = dict(bundle.platform_extra)

        title = normalise_title(bundle.title)
        if not title:
            raise PermanentError("抖音作品标题不能为空", raw={"field": "title"})
        if len(title) > TITLE_MAX:
            raise PermanentError(
                f"抖音标题最长 {TITLE_MAX} 字，当前 {len(title)} 字：{title!r}"
                "（上限依据 2026-08 创作者中心页面观察，未在真实站点验证）",
                raw={"field": "title", "length": len(title)},
            )

        videos = [m for m in bundle.media if m.kind == "video"]
        if len(videos) != 1:
            raise PermanentError(
                f"抖音发布需要且只需要 1 个视频成片，当前 {len(videos)} 个",
                raw={"field": "media", "count": len(videos)},
            )
        extra[KEY_HOST_VIDEO] = self._client.resolve_video(videos[0].path)

        cover = next(
            (m for m in bundle.media if m.kind == "image" and m.cover),
            next((m for m in bundle.media if m.kind == "image"), None),
        )
        if cover is not None:
            extra[KEY_HOST_COVER] = self._client.resolve_cover(cover.path)
        else:
            # 封面可选：不给就用平台抽的首帧
            extra.pop(KEY_HOST_COVER, None)

        hashtags = clean_hashtags(bundle.tags, limit=HASHTAG_MAX)
        dropped = len(clean_hashtags(bundle.tags, limit=len(bundle.tags) + 1)) - len(hashtags)
        if dropped > 0:
            logger.warning(
                "抖音话题最多 %d 个，已截掉 %d 个（内容项 %s）", HASHTAG_MAX, dropped, bundle.id
            )
        extra[KEY_HASHTAGS] = hashtags

        description = bundle.body_markdown.strip()
        if len(description) > DESCRIPTION_MAX:
            description = description[:DESCRIPTION_MAX]
            logger.warning(
                "抖音简介超过 %d 字已截断（内容项 %s）；上限未在真实站点验证",
                DESCRIPTION_MAX,
                bundle.id,
            )
        extra[KEY_DESCRIPTION] = description

        schedule_at = extra.get(KEY_SCHEDULE_AT)
        if schedule_at:
            extra[KEY_SCHEDULE_AT] = to_rfc3339(self._check_schedule(schedule_at))

        extra[KEY_PREPARED] = True
        return bundle.model_copy(update={"title": title, "tags": hashtags, "platform_extra": extra})

    def _check_schedule(self, value: Any) -> datetime:
        """定时发布时间必须落在 [now+2h, now+14d]（未在真实站点验证）。"""
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
                f"抖音定时发布至少要在 2 小时后，当前设置 {moment.isoformat()}，"
                f"最早可选 {(now + SCHEDULE_MIN_AHEAD).isoformat()}",
                raw={"field": "schedule_at"},
            )
        if moment > now + SCHEDULE_MAX_AHEAD:
            raise PermanentError(
                f"抖音定时发布不能超过 14 天，当前设置 {moment.isoformat()}，"
                f"最晚可选 {(now + SCHEDULE_MAX_AHEAD).isoformat()}",
                raw={"field": "schedule_at"},
            )
        return moment

    # ------------------------------------------------------------- publish

    def publish(self, bundle: ContentBundle) -> PublishResult:
        extra = bundle.platform_extra
        video = str(extra.get(KEY_HOST_VIDEO) or "")
        if not video:
            raise PermanentError(
                "publish 前必须先调用 prepare()（缺少 platform_extra.douyin_host_video）",
                raw={"field": KEY_HOST_VIDEO},
            )
        schedule_at = str(extra.get(KEY_SCHEDULE_AT) or "")
        if schedule_at:
            # 从批准到实际发布可能过了很久，重新校验一次窗口
            self._check_schedule(schedule_at)

        if self.dry_run:
            logger.info(
                "[dry_run] douyin 不发请求：title=%r video=%s schedule_at=%s",
                bundle.title,
                video,
                schedule_at or "-",
            )
            return PublishResult(
                ok=False,
                raw={
                    "dry_run": True,
                    "video": video,
                    "cover": bool(extra.get(KEY_HOST_COVER)),
                    "hashtags": list(extra.get(KEY_HASHTAGS) or []),
                    "schedule_at": schedule_at or None,
                    "hash": bundle.content_hash,
                },
            )

        self._check_rate_limit()

        outcome = self._client.publish(
            title=bundle.title,
            description=str(extra.get(KEY_DESCRIPTION) or bundle.body_markdown.strip()),
            video_path=video,
            hashtags=list(extra.get(KEY_HASHTAGS) or []),
            cover_path=str(extra.get(KEY_HOST_COVER) or ""),
            schedule_at=schedule_at,
            identity_hint=self.identity_hint,
        )

        self._record_rate_limit(bundle)
        published_at = self._now()
        scheduled = outcome.state == STATE_SCHEDULED or bool(schedule_at)

        if outcome.post_id:
            return PublishResult(
                ok=True,
                platform_post_id=outcome.post_id,
                url=outcome.url or video_url(outcome.post_id),
                raw={
                    "stage": outcome.state,
                    "post_id_resolved": True,
                    "screenshot_path": outcome.screenshot_path,
                },
                published_at=published_at,
            )

        prefix = SCHEDULED_PREFIX if scheduled else UNRESOLVED_PREFIX
        post_id = prefix + bundle.content_hash[:16]
        logger.warning(
            "抖音发布已提交但没拿到作品 id（title=%r）；记为 %s，**不会重发**，"
            "指标采集时会按标题兜底解析",
            bundle.title,
            post_id,
        )
        return PublishResult(
            ok=True,
            platform_post_id=post_id,
            url=None,
            raw={
                "stage": outcome.state,
                "post_id_resolved": False,
                "screenshot_path": outcome.screenshot_path,
            },
            published_at=published_at,
        )

    # ------------------------------------------------------------ 限频

    def _rate_token(self, bundle: ContentBundle) -> str:
        """限频去重令牌：同一 ContentItem 只计一次（与调度器共用口径）。"""
        return f"{self.platform}:{bundle.id}"

    def _check_rate_limit(self) -> None:
        moment = self._now()
        waiting = self._gate.wait_seconds(self.account_id, self.min_interval, moment)
        if waiting > 0:
            raise RetryableError(
                f"rate limited: 账号 {self.account_id} 距上次抖音发布不足 "
                f"{self.min_interval.total_seconds() / 60:.0f} 分钟，还需等 {waiting:.0f}s",
                raw={"account_id": self.account_id, "min_interval_s": self.min_interval.seconds},
                retry_after=waiting,
            )
        limiter = self.limiter
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
        moment = self._now()
        self.limiter.record(self.account_id, now=moment, token=self._rate_token(bundle))
        self._gate.record(self.account_id, moment)

    # -------------------------------------------------------------- health

    def health(self) -> AccountHealth:
        """账号健康巡检。

        - 已登录且 identity 对得上 → ``ok``
        - 未登录 / 卡在短信或图形验证 → ``needs_relogin``（挂起排期 + 通知人处理）
        - 上传器进程不可达 / 浏览器抖动 → ``degraded``（**不是** needs_relogin：
          误判会白白挂起排期并催人扫码）
        - identity 对不上 → ``degraded`` + 醒目 detail（**刻意不置 banned**：
          banned 是人工终态，状态机不允许自动恢复）
        """
        if self.dry_run:
            return AccountHealth(status="ok", detail="dry_run：未联网校验")
        try:
            self._client.health()
        except PublishError as exc:
            return AccountHealth(
                status="degraded",
                detail=(
                    f"宿主机抖音上传器不可用：{exc.message or exc}"
                    "（`python -m publishers.douyin serve` 起着吗？）"
                ),
            )
        try:
            state = self._client.login_status()
        except PublishError as exc:
            return AccountHealth(status="degraded", detail=f"查登录态失败：{exc.message or exc}")

        if state.state in (STATE_NEEDS_SMS, STATE_NEEDS_CAPTCHA):
            return AccountHealth(
                status="needs_relogin",
                detail=(
                    f"需要真人处理二次验证（{state.state}）："
                    f"{state.detail or ''} 请到 /accounts/{self.account_id}/login"
                ),
            )
        if state.state == STATE_LOGGED_OUT:
            return AccountHealth(
                status="needs_relogin",
                detail=(
                    "抖音创作者中心未登录，请在宿主机弹出的浏览器窗口用抖音 App 扫码"
                    f"（入口 /accounts/{self.account_id}/login）"
                ),
            )
        if state.state != STATE_LOGGED_IN:
            return AccountHealth(status="degraded", detail=state.detail or state.state)

        hint = normalise_title(self.identity_hint)
        if hint and state.nickname and state.nickname != mask_nickname(hint):
            # 上传器回的是打码昵称，这里只能做打码后的比对；真正的硬闸门在发布前
            return AccountHealth(
                status="degraded",
                detail=(
                    f"⚠️ identity 不符：浏览器里登录的是 {state.nickname}，"
                    f"账号 {self.account_id} 期望 {mask_nickname(hint)}。"
                    "发布会被 identity 闸门拦下（PermanentError）。"
                    "请核对 Account.extra.identity_hint 或换回正确账号登录。"
                ),
            )
        if not hint:
            return AccountHealth(
                status="degraded",
                detail=(
                    f"已登录（{state.nickname or '昵称未读到'}），但未配置 "
                    "Account.extra.identity_hint —— 防发错号闸门形同虚设，请尽快补上。"
                ),
            )
        return AccountHealth(status="ok", detail=f"已登录：{state.nickname}")

    # ----------------------------------------------------------- reconcile

    def reconcile(self, bundle: ContentBundle) -> PublishResult | None:
        """平台侧对账：这条内容是不是其实已经发出去了？

        策略（**只读**，不碰任何写接口）：拉内容管理页最近 N 条作品，
        按**归一化标题**精确命中，且发布时间落在对账窗口内（默认 24h）。
        时间读不出来时只按标题命中——抖音卡片上的时间写法多变（"3小时前"），
        宁可保守地认为"已发过"也不要重复发一条真视频。

        约定：明确未命中返回 ``None``；查不动（上传器挂了）抛 ``RetryableError``。
        """
        if self.dry_run:
            return None
        hit = self._find_post(bundle.title)
        if hit is None:
            return None
        post_id, url, matched_time = hit
        logger.info("抖音对账命中：post_id=%s title=%r", post_id or "(未解析)", bundle.title)
        return PublishResult(
            ok=True,
            platform_post_id=post_id or (UNRESOLVED_PREFIX + bundle.content_hash[:16]),
            url=url or (video_url(post_id) if post_id else None),
            raw={
                "stage": "published",
                "reconciled": True,
                "matched_by": "title+time" if matched_time else "title",
            },
            published_at=self._now(),
        )

    def _find_post(self, title: str) -> tuple[str, str, bool] | None:
        """在最近作品里找这条内容，返回 ``(post_id, url, 时间是否也匹配上)``。"""
        wanted = normalise_title(title)
        posts = self._client.recent_posts(limit=self.recent_posts)
        now = self._now()
        best: tuple[str, str, bool] | None = None
        best_at: datetime | None = None
        for post in posts:
            if normalise_title(post.title) != wanted:
                continue
            in_window = False
            if post.published_at is not None:
                age = now - post.published_at
                # 未来时间（定时作品）也算命中：它确实已经提交过了
                in_window = age <= self.reconcile_window
                if not in_window:
                    continue
            if best_at is None or (post.published_at is not None and post.published_at > best_at):
                best = (post.post_id, post.url, in_window)
                best_at = post.published_at
        return best

    # ------------------------------------------------------- fetch_metrics

    def fetch_metrics(self, platform_post_id: str) -> dict[str, Any]:
        """取单条作品的公开指标（数据中心页，**尽力而为**）。"""
        snapshot_at = self._now().isoformat()
        if self.dry_run:
            return _empty_metrics(snapshot_at, "dry_run：不发请求", post_id=platform_post_id)
        if is_placeholder_post_id(platform_post_id):
            return _empty_metrics(
                snapshot_at,
                f"platform_post_id={platform_post_id!r} 是占位值（发布时未解析出作品 id），"
                "采集器会调 fetch_metrics_for_title 按标题兜底",
                post_id=platform_post_id,
            )
        try:
            env = self._client.metrics(platform_post_id)
        except Exception as exc:
            return _empty_metrics(snapshot_at, f"metrics 调用失败：{exc}", post_id=platform_post_id)
        data = env.get("metrics") if isinstance(env.get("metrics"), dict) else {}
        return _metrics_from_service(data, snapshot_at, post_id=platform_post_id)

    def fetch_metrics_for_title(self, title: str) -> dict[str, Any]:
        """按标题兜底取指标（**可选能力**，``metrics/collector.py`` 用 hasattr 探测）。

        命中时返回的 dict 里带真实 ``platform_post_id``，采集器据此回填发布记录。
        """
        snapshot_at = self._now().isoformat()
        if self.dry_run:
            return _empty_metrics(snapshot_at, "dry_run：不发请求")
        try:
            hit = self._find_post(title)
        except Exception as exc:
            return _empty_metrics(snapshot_at, f"recent_posts 调用失败：{exc}")
        if hit is None or not hit[0]:
            return _empty_metrics(
                snapshot_at, f"内容管理页最近 {self.recent_posts} 条里没有标题 {title!r} 的作品"
            )
        return self.fetch_metrics(hit[0])

    # -------------------------------------------------- 人工介入通道（登录）

    def get_login_qrcode(self) -> str:
        """抖音的登录二维码在**宿主机浏览器窗口里**，core 不代理。

        故意抛 ``PermanentError`` 而不是返回占位图：给一张假二维码会让人对着扫半天。
        core 的登录页对 douyin 账号走另一套文案（"请到宿主机窗口扫码"）。
        """
        raise PermanentError(
            "抖音不通过 core 代理二维码：请点「打开宿主机登录窗口」，"
            "然后在宿主机弹出的浏览器里用抖音 App 扫码。",
            raw={"account_id": self.account_id, "platform": self.platform},
        )

    def start_login(self) -> dict[str, Any]:
        """在宿主机弹出登录窗口（**可选能力**，``core.main`` 用 hasattr 探测）。"""
        env = self._client.start_login(identity_hint=self.identity_hint)
        return {
            "state": str(env.get("state") or ""),
            "detail": str(env.get("detail") or ""),
            "nickname": str(env.get("nickname") or ""),
        }

    def check_login_status(self) -> AccountHealth:
        return self.health()

    def submit_sms_code(self, code: str) -> bool:
        """把真人输入的短信验证码转发给宿主机上传器填入页面。

        红线：只转发、只填写，**绝不识别**（docs/POLICY.md）。
        验证码不落库、不进日志（客户端对该请求关掉了请求体日志）。
        """
        env = self._client.submit_sms_code(code)
        if env.get("ok"):
            return True
        detail = str(env.get("detail") or "")
        state = str(env.get("state") or "")
        raise NeedsReloginError(
            f"验证码没能填进页面（state={state}）：{detail}",
            raw={"account_id": self.account_id, "state": state},
        )


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
        "platform": "douyin",
        "source": None,
        "platform_post_id": post_id,
    }


def _metrics_from_service(
    data: dict[str, Any], snapshot_at: str, *, post_id: str
) -> dict[str, Any]:
    if not data.get("available"):
        return _empty_metrics(
            snapshot_at,
            str(data.get("reason") or "上传器没能从数据中心读到该作品的指标"),
            post_id=post_id,
        )
    return {
        "views": data.get("views"),
        "likes": data.get("likes"),
        "comments": data.get("comments"),
        "shares": data.get("shares"),
        # 抖音数据中心不单列收藏 / 涨粉，缺就是缺，不伪造 0
        "collects": data.get("collects"),
        "follows": data.get("follows"),
        "snapshot_at": snapshot_at,
        "available": True,
        "reason": None,
        "platform": "douyin",
        "source": "creator-data-center",
        "platform_post_id": post_id,
        "url": video_url(post_id),
    }


# ------------------------------------------------------------- 账号配置解析


class DouyinAccountConfig:
    """一个抖音账号的运行期配置。

    - ``service_url``：优先 ``DOUYIN_SERVICE_URL``（运维口径），其次 DB 的
      ``Account.sidecar_endpoint``（台账口径）。
    - ``identity_hint``：``Account.extra["identity_hint"]``（或 ``extra["douyin"]``
      下同名键）。**防发错号的唯一依据**，生产必须配。
    - ``daily_limit``：DB 的 ``Account.daily_limit``，缺省取 ``DOUYIN_DAILY_LIMIT``；
      无论怎么配都不会超过 :data:`DAILY_LIMIT_CEILING`。
    """

    def __init__(
        self,
        *,
        service_url: str = "",
        identity_hint: str = "",
        daily_limit: int = DEFAULT_DAILY_LIMIT,
        min_interval_minutes: int = DEFAULT_MIN_INTERVAL_MINUTES,
        recent_posts: int = DEFAULT_RECENT_POSTS,
        reconcile_window_hours: int = DEFAULT_RECONCILE_WINDOW_HOURS,
    ) -> None:
        self.service_url = service_url
        self.identity_hint = identity_hint
        self.daily_limit = daily_limit
        self.min_interval_minutes = min_interval_minutes
        self.recent_posts = recent_posts
        self.reconcile_window_hours = reconcile_window_hours


def load_account_config(account_id: str) -> DouyinAccountConfig:
    """从环境变量 + accounts 表拼出该账号的配置。DB 不可用时静默退回默认值。"""
    from core.config import get_settings

    settings = get_settings()
    service_url = settings.douyin_service_url
    identity_hint = ""
    daily_limit = 0

    try:
        from core import db
        from core.models import Account

        with db.get_session_factory()() as session:
            account = session.get(Account, account_id)
            if account is not None:
                service_url = service_url or (account.sidecar_endpoint or "")
                daily_limit = int(account.daily_limit or 0)
                extra = dict(account.extra or {})
                nested = extra.get("douyin") if isinstance(extra.get("douyin"), dict) else {}
                identity_hint = str(extra.get("identity_hint") or nested.get("identity_hint") or "")
    except Exception as exc:  # DB 未初始化（契约测试 / 纯离线调用）时不该炸
        logger.debug("读取账号 %s 配置失败，使用默认值: %s", account_id, exc)

    return DouyinAccountConfig(
        service_url=service_url,
        identity_hint=identity_hint,
        daily_limit=daily_limit or settings.douyin_daily_limit,
        min_interval_minutes=settings.douyin_min_interval_minutes,
        recent_posts=settings.douyin_recent_posts,
        reconcile_window_hours=settings.douyin_reconcile_window_hours,
    )


__all__ = [
    "DAILY_LIMIT_CEILING",
    "DEFAULT_DAILY_LIMIT",
    "DEFAULT_MIN_INTERVAL_MINUTES",
    "KEY_DESCRIPTION",
    "KEY_HASHTAGS",
    "KEY_HOST_COVER",
    "KEY_HOST_VIDEO",
    "KEY_SCHEDULE_AT",
    "MIN_INTERVAL_GATE",
    "SCHEDULED_PREFIX",
    "UNRESOLVED_PREFIX",
    "DouyinAccountConfig",
    "DouyinPublisher",
    "MinIntervalGate",
    "is_placeholder_post_id",
    "load_account_config",
]
