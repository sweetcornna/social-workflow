"""微信公众号发布器：实现 ``publishers.base.Publisher`` 契约。

形态 A（认证号）
    ``draft/add`` → **双确认闸门**全通过 → ``freepublish/submit`` → 轮询
    ``freepublish/get`` 到 ``publish_status=0``，回传文章永久链接。

形态 B（个人号 / 未认证号）
    ``draft/add`` 之后**停在草稿箱**，返回 ``PublishResult(ok=True,
    platform_post_id=<draft media_id>, url=None, raw={"stage": "draft"})``，
    由人工在公众号后台点"发表"。这是 2025-07 官方回收 freepublish 权限后的唯一合规路径。

双确认闸门（三者缺一即只到草稿箱）
    1. 服务端开关 ``settings.WECHAT_AUTO_PUBLISH``（运维侧，改环境变量才生效）
    2. 账号认证状态 ``certified``（默认取 ``settings.WECHAT_CERTIFIED``）
    3. **本次内容**的显式确认：``bundle.platform_extra["confirm_publish"] is True``，
       由审核 UI 在人工批准时写入（见 :func:`mark_confirm_publish`）。
       它落在 ``ContentItem.bundle_json`` 里，配合 ``ReviewLog`` 构成"人确认过"的证据链。

    第 3 条严格判 ``is True``：字符串 "true"、1、"on" 都**不算**确认，
    避免表单值被意外当成放行。
"""

from __future__ import annotations

import html as html_lib
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, ClassVar

from publishers.base import (
    AccountHealth,
    ContentBundle,
    PermanentError,
    Publisher,
    PublishResult,
    RetryableError,
)
from publishers.wechat_mp.client import (
    AUTHOR_MAX,
    DIGEST_MAX,
    ERRCODE_IP_NOT_IN_WHITELIST,
    TITLE_MAX,
    WechatMpClient,
    looks_like_msgid,
)

logger = logging.getLogger("social_workflow.publishers.wechat_mp")

# freepublish/get 的 publish_status 语义（官方文档已核实）
PUBLISH_STATUS_SUCCESS = 0
PUBLISH_STATUS_PUBLISHING = 1
PUBLISH_STATUS_TEXT: dict[int, str] = {
    0: "发布成功",
    1: "发布中",
    2: "原创失败",
    3: "常规失败",
    4: "平台审核不通过",
    5: "成功后用户删除所有文章",
    6: "成功后系统封禁所有文章",
}

# 对账时扫描的草稿 / 已发布条数上限（每页 20）
RECONCILE_MAX_ITEMS = 60

CONFIRM_KEY = "confirm_publish"


def mark_confirm_publish(
    bundle_json: dict[str, Any], *, actor: str, at: datetime | None = None
) -> dict[str, Any]:
    """审核 UI 批准公众号内容时调用：把本次的显式发布确认写进 ``platform_extra``。

    返回**新的** bundle dict（不原地改），调用方负责回写 ``ContentItem.bundle_json``
    并写 ``ReviewLog``。不写 ``confirm_publish`` 的内容永远只到草稿箱。
    """
    updated = dict(bundle_json)
    extra = dict(updated.get("platform_extra") or {})
    extra[CONFIRM_KEY] = True
    extra["confirm_publish_by"] = actor
    extra["confirm_publish_at"] = (at or datetime.now(UTC)).isoformat()
    updated["platform_extra"] = extra
    return updated


def _plain_text(markdown: str) -> str:
    """把 markdown 粗暴压成一行纯文本，只用于自动生成 digest。"""
    text = markdown.replace("\r", "")
    out: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip().lstrip("#>-*+ ").strip()
        if stripped:
            out.append(stripped)
    return " ".join(out)


def _markdown_to_minimal_html(markdown: str) -> str:
    """兜底渲染：正文只做转义 + 分段。

    正式渲染由 ``generation/`` 走 ``@wenyan-md/cli render`` 产出内联样式 HTML 并写进
    ``bundle.body_html``；这里只保证 body_html 缺失时仍能发出结构正确的文章，
    且**确定性**（保证 ``prepare`` 幂等）。
    """
    blocks = [b.strip() for b in markdown.replace("\r", "").split("\n\n")]
    paragraphs = ["<p>" + html_lib.escape(b).replace("\n", "<br/>") + "</p>" for b in blocks if b]
    return "".join(paragraphs)


class WechatMpPublisher(Publisher):
    """公众号发布器。``platform = "wechat_mp"``。"""

    platform: ClassVar[str] = "wechat_mp"

    def __init__(
        self,
        account_id: str,
        *,
        dry_run: bool = False,
        client: WechatMpClient | None = None,
        certified: bool | None = None,
        auto_publish: bool | None = None,
        base_dir: str | None = None,
        poll_interval: float | None = None,
        poll_timeout: float | None = None,
        sleeper: Callable[[float], None] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(account_id, dry_run=dry_run)
        from core.config import get_settings

        settings = get_settings()
        if client is None:
            client = WechatMpClient(
                settings.wechat_app_id,
                settings.wechat_app_secret,
                base_url=settings.wechat_api_base,
                dry_run=dry_run,
            )
        self._client = client
        self.certified = settings.wechat_certified if certified is None else bool(certified)
        self.auto_publish = (
            settings.wechat_auto_publish if auto_publish is None else bool(auto_publish)
        )
        self.base_dir = (
            base_dir if base_dir is not None else (settings.wechat_media_base_dir or None)
        )
        self.poll_interval = (
            settings.wechat_publish_poll_interval if poll_interval is None else poll_interval
        )
        self.poll_timeout = (
            settings.wechat_publish_poll_timeout if poll_timeout is None else poll_timeout
        )
        self._sleep = sleeper or time.sleep
        self._now = now or (lambda: datetime.now(UTC))

    @property
    def client(self) -> WechatMpClient:
        return self._client

    # ------------------------------------------------------------- prepare

    def normalise_fields(self, bundle: ContentBundle) -> tuple[str, dict[str, Any]]:
        """只做字段校验与归一化（标题/作者/摘要/原文链接），**不做任何上传**。

        抽出来是为了让 wenyan 后端复用同一套长度校验（它自己负责渲染与传图）。
        返回 ``(归一化标题, 新的 platform_extra)``。
        """
        extra = dict(bundle.platform_extra)

        title = bundle.title.strip()
        if not title:
            raise PermanentError("公众号文章标题不能为空", raw={"field": "title"})
        if len(title) > TITLE_MAX:
            raise PermanentError(
                f"公众号标题最长 {TITLE_MAX} 字，当前 {len(title)} 字：{title!r}",
                raw={"field": "title", "length": len(title)},
            )

        author = str(extra.get("author") or "").strip()
        if len(author) > AUTHOR_MAX:
            raise PermanentError(
                f"公众号作者名最长 {AUTHOR_MAX} 字，当前 {len(author)} 字",
                raw={"field": "author", "length": len(author)},
            )
        if author:
            extra["author"] = author
        else:
            extra.pop("author", None)

        digest = str(extra.get("digest") or "").strip()
        if digest:
            if len(digest) > DIGEST_MAX:
                raise PermanentError(
                    f"公众号摘要最长 {DIGEST_MAX} 字，当前 {len(digest)} 字",
                    raw={"field": "digest", "length": len(digest)},
                )
        else:
            digest = _plain_text(bundle.body_markdown)[:DIGEST_MAX]
        if digest:
            extra["digest"] = digest

        source_url = str(extra.get("content_source_url") or "").strip()
        if source_url:
            extra["content_source_url"] = source_url
        return title, extra

    def prepare(self, bundle: ContentBundle) -> ContentBundle:
        """校验字段长度、上传封面与正文图片，填充 ``platform_extra``。

        幂等保证：标题只 strip；``digest`` / ``author`` / ``thumb_media_id`` 一旦落进
        ``platform_extra`` 就不再重算；正文图片替换后已全是 mmbiz 域名，第二次是 no-op。
        """
        title, extra = self.normalise_fields(bundle)

        # 封面：thumb_media_id 必须是永久素材 media_id
        if not extra.get("thumb_media_id"):
            cover = bundle.cover
            if cover is None or cover.kind != "image":
                raise PermanentError(
                    "公众号文章必须有封面图（ContentBundle.media 中标记 cover=True 的图片）",
                    raw={"field": "thumb_media_id"},
                )
            extra["thumb_media_id"] = self._client.add_material_image(cover.path)

        # 正文：缺 body_html 时兜底渲染；把非 mmbiz 图片换成公众号图床 URL
        html = bundle.body_html or _markdown_to_minimal_html(bundle.body_markdown)
        html = self._client.replace_external_images(html, base_dir=self.base_dir)

        extra["wechat_prepared"] = True
        return bundle.model_copy(
            update={"title": title, "body_html": html, "platform_extra": extra}
        )

    # ------------------------------------------------------------- publish

    def gate(self, bundle: ContentBundle) -> tuple[bool, dict[str, Any]]:
        """双确认闸门判定。返回 ``(是否允许 freepublish, 判定明细)``。"""
        confirmed = bundle.platform_extra.get(CONFIRM_KEY) is True
        detail = {
            "server_switch": bool(self.auto_publish),  # WECHAT_AUTO_PUBLISH
            "account_certified": bool(self.certified),  # WECHAT_CERTIFIED / 账号台账
            "confirm_publish": confirmed,  # 审核 UI 批准时写入
        }
        allowed = all(detail.values())
        detail["allowed"] = allowed
        if not allowed:
            detail["blocked_by"] = [k for k, v in detail.items() if k != "allowed" and not v]
        return allowed, detail

    def _build_articles(self, bundle: ContentBundle) -> list[dict[str, Any]]:
        extra = bundle.platform_extra
        thumb = str(extra.get("thumb_media_id") or "")
        if not thumb:
            raise PermanentError(
                "缺少 thumb_media_id：publish 前必须先调用 prepare()",
                raw={"field": "thumb_media_id"},
            )
        article: dict[str, Any] = {
            "article_type": "news",
            "title": bundle.title.strip(),
            "content": bundle.body_html or "",
            "thumb_media_id": thumb,
            "need_open_comment": 1 if extra.get("need_open_comment") else 0,
            "only_fans_can_comment": 1 if extra.get("only_fans_can_comment") else 0,
        }
        if extra.get("author"):
            article["author"] = str(extra["author"])
        if extra.get("digest"):
            article["digest"] = str(extra["digest"])
        if extra.get("content_source_url"):
            article["content_source_url"] = str(extra["content_source_url"])
        if not article["content"]:
            raise PermanentError("公众号文章正文为空", raw={"field": "content"})
        return [article]

    def publish(self, bundle: ContentBundle) -> PublishResult:
        articles = self._build_articles(bundle)
        allowed, gate = self.gate(bundle)

        if self.dry_run:
            logger.info("[dry_run] wechat_mp 不发请求：title=%r 闸门=%s", bundle.title, gate)
            return PublishResult(
                ok=False,
                raw={
                    "dry_run": True,
                    "stage": "draft" if not allowed else "freepublish",
                    "gate": gate,
                    "article_keys": sorted(articles[0]),
                },
            )

        media_id = self._client.draft_add(articles)

        if not allowed:
            logger.info("wechat_mp 停在草稿箱（闸门未全开 %s），media_id=%s", gate, media_id)
            return PublishResult(
                ok=True,
                platform_post_id=media_id,
                url=None,
                raw={"stage": "draft", "gate": gate},
                published_at=self._now(),
            )

        publish_id = self._client.freepublish_submit(media_id)
        status = self._poll_publish(publish_id)
        article_url = self._first_article_url(status)
        return PublishResult(
            ok=True,
            platform_post_id=media_id,
            url=article_url,
            raw={
                "stage": "published",
                "gate": gate,
                "publish_id": publish_id,
                "article_id": status.get("article_id"),
                "publish_status": status.get("publish_status"),
                "draft_media_id": media_id,
            },
            published_at=self._now(),
        )

    def _poll_publish(self, publish_id: str) -> dict[str, Any]:
        """轮询 ``freepublish/get`` 直到终态。

        - 0 成功 → 返回响应
        - 1 发布中 → 继续轮询；超时抛 ``RetryableError``
        - 2/3/4/5/6 → ``PermanentError``（原创失败 / 常规失败 / 平台审核不通过 / 已被删除或封禁）
        """
        deadline = time.monotonic() + self.poll_timeout
        last: dict[str, Any] = {}
        while True:
            last = self._client.freepublish_get(publish_id)
            status = int(last.get("publish_status", PUBLISH_STATUS_PUBLISHING))
            if status == PUBLISH_STATUS_SUCCESS:
                return last
            if status != PUBLISH_STATUS_PUBLISHING:
                raise PermanentError(
                    f"freepublish 失败：publish_status={status}"
                    f"（{PUBLISH_STATUS_TEXT.get(status, '未知状态')}）"
                    f" fail_idx={last.get('fail_idx')}",
                    raw={"publish_id": publish_id, **last},
                )
            if time.monotonic() >= deadline:
                # 注意：任务可能仍会成功，重试前必经 reconcile() 对账，避免重复建草稿
                raise RetryableError(
                    f"freepublish 轮询超时（{self.poll_timeout}s）仍处于发布中，"
                    f"publish_id={publish_id}；下次重试前会先做平台侧对账",
                    raw={"publish_id": publish_id, **last},
                    retry_after=self.poll_interval,
                )
            self._sleep(self.poll_interval)

    @staticmethod
    def _first_article_url(status: dict[str, Any]) -> str | None:
        detail = status.get("article_detail") or {}
        items = detail.get("item") or []
        for item in items:
            url = item.get("article_url")
            if url:
                return str(url)
        return None

    # -------------------------------------------------------------- health

    def health(self) -> AccountHealth:
        """公众号没有登录态，只有凭据与 IP 白名单，因此**永远不返回** ``needs_relogin``。

        - token 可取 → ``ok``
        - 40164（IP 未白名单）→ ``degraded`` + 出口 IP 排障提示
        - AppID/AppSecret 无效 → ``degraded``（这是配置问题，不是封号；``banned`` 是
          状态机的人工终态，一旦置入就无法自动恢复，误判代价太大）
        - 限频 / 网络问题 → ``degraded``
        """
        if self.dry_run:
            return AccountHealth(status="ok", detail="dry_run：未联网校验")
        if not self._client.has_credentials:
            return AccountHealth(
                status="degraded", detail="未配置 WECHAT_APP_ID / WECHAT_APP_SECRET"
            )
        try:
            self._client.get_access_token()
        except PermanentError as exc:
            errcode = exc.raw.get("errcode")
            if errcode == ERRCODE_IP_NOT_IN_WHITELIST:
                return AccountHealth(status="degraded", detail=exc.message)
            return AccountHealth(status="degraded", detail=f"凭据/权限异常：{exc.message}")
        except RetryableError as exc:
            return AccountHealth(status="degraded", detail=f"暂时不可用：{exc.message}")
        mode = "认证号(可自动发布)" if self.certified else "未认证号(只落草稿箱)"
        return AccountHealth(status="ok", detail=f"stable_token 正常；{mode}")

    # ----------------------------------------------------------- reconcile

    def reconcile(self, bundle: ContentBundle) -> PublishResult | None:
        """平台侧对账：这条内容是不是其实已经建过草稿 / 已经发布了？

        策略（**不污染任何用户可见字段**，不往 digest/content_source_url 塞隐藏标记）：

        1. DB 侧的唯一真相仍是 ``PublishRecord.idem_key``，由
           ``core.state_machine.publish_with_idempotency`` 负责；本方法只补平台侧。
        2. 闸门配置为自动发布时，先扫 ``freepublish/batchget`` 已发布列表，标题命中即认为已发。
        3. 再扫 ``draft/batchget`` 草稿箱，用**标题 + 摘要**双字段命中判定已建草稿
           （摘要缺失时退化为仅标题）。命中返回 ``stage=draft``。

        约定：明确未命中返回 ``None``；查不动（网络/5xx）抛 ``RetryableError``。
        """
        if self.dry_run:
            return None
        title = bundle.title.strip()
        if not title:
            return None
        digest = str(bundle.platform_extra.get("digest") or "").strip()

        if self.auto_publish and self.certified:
            hit = self._scan(
                self._client.freepublish_batchget,
                title,
                digest,
                stage="published",
                key="article_id",
            )
            if hit is not None:
                return hit
        return self._scan(self._client.draft_batchget, title, digest, stage="draft", key="media_id")

    def _scan(
        self,
        fetch: Callable[..., dict[str, Any]],
        title: str,
        digest: str,
        *,
        stage: str,
        key: str,
    ) -> PublishResult | None:
        offset = 0
        page = 20
        while offset < RECONCILE_MAX_ITEMS:
            data = fetch(offset=offset, count=page, no_content=1)
            items = data.get("item") or []
            if not items:
                return None
            for entry in items:
                news = (entry.get("content") or {}).get("news_item") or []
                for article in news:
                    if str(article.get("title", "")).strip() != title:
                        continue
                    if digest and str(article.get("digest", "")).strip() != digest:
                        continue
                    post_id = str(entry.get(key) or entry.get("media_id") or "")
                    if not post_id:
                        continue
                    logger.info("wechat_mp 对账命中 %s：%s title=%r", stage, post_id, title)
                    return PublishResult(
                        ok=True,
                        platform_post_id=post_id,
                        url=article.get("url") or None,
                        raw={
                            "stage": stage,
                            "reconciled": True,
                            "matched_by": "title+digest" if digest else "title",
                            "update_time": entry.get("update_time"),
                        },
                        published_at=self._now(),
                    )
            total = int(data.get("total_count") or 0)
            offset += len(items)
            if offset >= total:
                return None
        return None

    # ------------------------------------------------------- fetch_metrics

    def fetch_metrics(self, platform_post_id: str) -> dict[str, Any]:
        """拉取单篇文章的 datacube 指标。

        未认证号没有 datacube 权限：返回统一结构但 ``available=False`` + 原因说明，
        **不伪造 0**（见 metrics/README.md 契约）。

        ``platform_post_id`` 是我们回写的草稿 ``media_id``，而 datacube 用的是
        ``msgid``，两者没有官方映射，所以走一条解析链：
        msgid 直配 → ``draft/get`` 取标题后按标题配 → 都不行则如实报 unavailable。
        """
        snapshot_at = self._now().isoformat()
        if self.dry_run:
            return _empty_metrics(snapshot_at, "dry_run：不发请求", post_id=platform_post_id)
        if not self.certified:
            return _empty_metrics(
                snapshot_at,
                "未认证号无 datacube 权限（2025-07 起权限回收），指标需人工在公众号后台查看",
                post_id=platform_post_id,
            )
        if not self._client.has_credentials:
            return _empty_metrics(
                snapshot_at, "未配置 WECHAT_APP_ID / WECHAT_APP_SECRET", post_id=platform_post_id
            )

        day = self._client.yesterday(now=self._now())
        try:
            rows = self._client.datacube_getarticletotal(day, day)
        except (PermanentError, RetryableError) as exc:
            return _empty_metrics(
                snapshot_at, f"datacube 调用失败：{exc}", post_id=platform_post_id, day=day
            )

        row = self._match_row(rows, platform_post_id)
        if row is None:
            return _empty_metrics(
                snapshot_at,
                f"未能把 platform_post_id={platform_post_id!r} 映射到 datacube msgid"
                f"（{day} 共 {len(rows)} 条群发记录）",
                post_id=platform_post_id,
                day=day,
            )
        return _metrics_from_row(row, snapshot_at, post_id=platform_post_id, day=day)

    def fetch_metrics_for_title(self, title: str, *, day: str | None = None) -> dict[str, Any]:
        """按标题取指标：供 ``metrics/collector.py`` 在 post_id 映射失败时兜底调用。

        不属于冻结契约，是可选能力（collector 用 ``hasattr`` 探测）。
        """
        snapshot_at = self._now().isoformat()
        if self.dry_run or not self.certified or not self._client.has_credentials:
            return self.fetch_metrics(title)
        target = day or self._client.yesterday(now=self._now())
        try:
            rows = self._client.datacube_getarticletotal(target, target)
        except (PermanentError, RetryableError) as exc:
            return _empty_metrics(snapshot_at, f"datacube 调用失败：{exc}", day=target)
        wanted = title.strip()
        for row in rows:
            if str(row.get("title", "")).strip() == wanted:
                return _metrics_from_row(
                    row, snapshot_at, post_id=str(row.get("msgid")), day=target
                )
        return _empty_metrics(snapshot_at, f"{target} 的群发记录里没有标题 {wanted!r}", day=target)

    def _match_row(self, rows: list[dict[str, Any]], post_id: str) -> dict[str, Any] | None:
        if looks_like_msgid(post_id):
            for row in rows:
                msgid = str(row.get("msgid", ""))
                if msgid == post_id or msgid.split("_")[0] == post_id:
                    return row
        title = self._resolve_title(post_id)
        if title:
            for row in rows:
                if str(row.get("title", "")).strip() == title:
                    return row
        return None

    def _resolve_title(self, post_id: str) -> str | None:
        """用 draft/get 把草稿 media_id 换成标题。

        **未核实**：freepublish 成功后原草稿是否仍能 draft/get 到。取不到就返回 None，
        由调用方 fallback 到 :meth:`fetch_metrics_for_title`。
        """
        try:
            data = self._client.draft_get(post_id)
        except (PermanentError, RetryableError) as exc:
            logger.debug("draft/get 解析标题失败 %s: %s", post_id, exc)
            return None
        news = data.get("news_item") or []
        if news:
            return str(news[0].get("title", "")).strip() or None
        return None


# ---------------------------------------------------------------- 指标归一化


def _empty_metrics(
    snapshot_at: str, reason: str, *, post_id: str | None = None, day: str | None = None
) -> dict[str, Any]:
    """指标缺失时的统一结构：字段一律 ``None``，**不伪造 0**。"""
    return {
        # metrics/README.md 的跨平台统一字段
        "views": None,
        "likes": None,
        "comments": None,
        "shares": None,
        "collects": None,
        "follows": None,
        # 公众号原生口径别名
        "read": None,
        "like": None,
        "share": None,
        "comment": None,
        "collect": None,
        "snapshot_at": snapshot_at,
        "available": False,
        "reason": reason,
        "platform": "wechat_mp",
        "source": "datacube/getarticletotal",
        "platform_post_id": post_id,
        "stat_date": day,
    }


def _metrics_from_row(
    row: dict[str, Any], snapshot_at: str, *, post_id: str | None, day: str | None
) -> dict[str, Any]:
    """把 ``getarticletotal`` 的一行折算成统一指标。

    ``details`` 是"到该日为止的累计量"数组，取 ``stat_date`` 最大的一条。
    公众号 datacube **不提供点赞（在看）与评论数**，这两个字段保持 ``None``。
    """
    details = row.get("details") or []
    latest: dict[str, Any] = {}
    if details:
        latest = max(details, key=lambda d: str(d.get("stat_date", "")))

    def _num(key: str) -> int | None:
        value = latest.get(key)
        return int(value) if isinstance(value, int | float) else None

    read = _num("int_page_read_count")
    share = _num("share_count")
    collect = _num("add_to_fav_count")
    return {
        "views": read,
        "likes": None,  # datacube 无点赞/在看接口
        "comments": None,  # datacube 无评论数接口
        "shares": share,
        "collects": collect,
        "follows": None,  # 关注数是账号级指标，见 datacube_getusersummary
        "read": read,
        "like": None,
        "share": share,
        "comment": None,
        "collect": collect,
        "snapshot_at": snapshot_at,
        "available": True,
        "platform": "wechat_mp",
        "source": "datacube/getarticletotal",
        "platform_post_id": post_id,
        "msgid": row.get("msgid"),
        "title": row.get("title"),
        "stat_date": latest.get("stat_date") or day,
        "wechat": {
            "int_page_read_user": _num("int_page_read_user"),
            "int_page_read_count": read,
            "ori_page_read_user": _num("ori_page_read_user"),
            "ori_page_read_count": _num("ori_page_read_count"),
            "share_user": _num("share_user"),
            "share_count": share,
            "add_to_fav_user": _num("add_to_fav_user"),
            "add_to_fav_count": collect,
            "target_user": _num("target_user"),
        },
    }


__all__ = [
    "CONFIRM_KEY",
    "PUBLISH_STATUS_TEXT",
    "WechatMpPublisher",
    "mark_confirm_publish",
]
