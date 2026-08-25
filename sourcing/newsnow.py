"""newsnow 热榜采集（``ourongxing/newsnow``，MIT）。

只调它的公开 HTTP API，不 import 其代码（它是 TypeScript / Nitro 项目）。
实例地址由 ``NEWSNOW_BASE_URL`` 配置——**默认为空**，必须显式指定自部署实例或
公开实例，避免默认去打别人的服务器。

已核实的接口形态（2026-08-15）::

    GET  {base}/api/s?id=<source>       单个榜单
    POST {base}/api/s/entire            批量，body {"sources": ["weibo", ...]}

单榜响应::

    {"status": "success" | "cache", "id": "weibo", "updatedTime": 1755...,
     "items": [{"id": ..., "title": "...", "url": "...",
                "mobileUrl": "...", "pubDate": ...,
                "extra": {"hover": "...", "info": "669 万热度", "icon": ...}}],
     "info": {...}}

几个坑：

- ``items`` 服务端固定截断到 **30** 条。
- 非法 source id 返回 **HTTP 500**（不是 400/404），报错要看得懂。
- ``extra.diff`` 是前端算的排名变化，**API 不会返回**，别指望。
- 批量接口**只读缓存**，冷启动的源会被整个略掉，所以默认走逐个 GET。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from sourcing.base import RawTopic, SourceUnavailable, SourcingError, rank_score

logger = logging.getLogger("social_workflow.sourcing.newsnow")

SOURCE = "newsnow"
UPSTREAM = "https://github.com/ourongxing/newsnow"
UPSTREAM_LICENSE = "MIT"

#: 服务端对每个榜单的固定截断
SERVER_ITEM_CAP = 30

#: 已核实存在的榜单 id（节选，完整 66 个见上游 shared/sources.json）。
#: 只用来在配置写错时给出可读提示，不做强校验——上游随时会加新源。
KNOWN_SOURCE_IDS = frozenset(
    {
        "weibo",
        "zhihu",
        "baidu",
        "toutiao",
        "douyin",
        "bilibili",
        "bilibili-hot-search",
        "v2ex",
        "hupu",
        "tieba",
        "36kr",
        "ithome",
        "thepaper",
        "sspai",
        "juejin",
        "github",
        "github-trending-today",
        "hackernews",
        "producthunt",
        "solidot",
        "xueqiu",
        "cls",
        "cls-hot",
        "wallstreetcn",
        "wallstreetcn-hot",
        "jin10",
        "gelonghui",
        "douban",
        "kuaishou",
        "ifeng",
        "zaobao",
        "coolapk",
        "steam",
        "tencent",
        "freebuf",
        "nowcoder",
    }
)


class NewsNowError(SourcingError):
    """newsnow 调用失败。"""


def _base_url(explicit: str | None = None) -> str:
    from core.config import get_settings

    base = (explicit if explicit is not None else get_settings().newsnow_base_url).strip()
    if not base:
        raise SourceUnavailable(
            "NEWSNOW_BASE_URL 未配置：newsnow 数据源不可用。"
            "请在 .env 里填自部署实例地址（如 http://localhost:4444）或公开实例地址。"
        )
    return base.rstrip("/")


def _client(client: httpx.Client | None, timeout: float) -> tuple[httpx.Client, bool]:
    """返回 ``(client, 是否需要由调用方关闭)``。"""
    if client is not None:
        return client, False
    return httpx.Client(timeout=timeout, follow_redirects=True), True


def parse_source_response(payload: dict[str, Any], *, source_id: str) -> list[RawTopic]:
    """把单个榜单的响应转成 :class:`RawTopic` 列表。"""
    items = payload.get("items")
    if not isinstance(items, list):
        raise NewsNowError(f"榜单 {source_id} 响应缺少 items 字段: {list(payload)[:8]}")

    total = len(items)
    topics: list[RawTopic] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        topics.append(
            RawTopic(
                source=SOURCE,
                title=title,
                url=item.get("url") or item.get("mobileUrl"),
                score=rank_score(index, total),
                raw={
                    "board": source_id,
                    "rank": index,
                    "item_id": item.get("id"),
                    "mobile_url": item.get("mobileUrl"),
                    "pub_date": item.get("pubDate"),
                    # extra.info 常见形如 "669 万热度"，是字符串不是数字，原样保留
                    "info": extra.get("info"),
                    "hover": extra.get("hover"),
                    "updated_time": payload.get("updatedTime"),
                    "status": payload.get("status"),
                },
            )
        )
    return topics


def fetch_board(
    source_id: str,
    *,
    base_url: str | None = None,
    client: httpx.Client | None = None,
    timeout: float | None = None,
    latest: bool = False,
) -> list[RawTopic]:
    """拉取单个榜单。``latest=True`` 让服务端跳过缓存强制刷新。"""
    from core.config import get_settings

    settings = get_settings()
    base = _base_url(base_url)
    http, owned = _client(client, timeout or settings.sourcing_timeout_seconds)
    params: dict[str, Any] = {"id": source_id}
    if latest:
        params["latest"] = ""
    try:
        response = http.get(f"{base}/api/s", params=params)
        if response.status_code == 500:
            # 上游用 500 表达"非法 source id"，翻译成看得懂的错误
            hint = "" if source_id in KNOWN_SOURCE_IDS else f"（{source_id!r} 不在已知榜单列表里）"
            raise NewsNowError(f"newsnow 拒绝了榜单 id {source_id!r}{hint}：{response.text[:200]}")
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        raise NewsNowError(f"newsnow 请求失败（{source_id}）: {exc}") from exc
    finally:
        if owned:
            http.close()

    if not isinstance(payload, dict):
        raise NewsNowError(f"newsnow 返回了非对象响应（{source_id}）: {type(payload).__name__}")
    topics = parse_source_response(payload, source_id=source_id)
    logger.info(
        "newsnow 榜单 %s 拉到 %d 条（status=%s）", source_id, len(topics), payload.get("status")
    )
    return topics


def fetch(
    source_ids: list[str] | None = None,
    *,
    base_url: str | None = None,
    client: httpx.Client | None = None,
    timeout: float | None = None,
    limit_per_board: int | None = None,
    latest: bool = False,
) -> list[RawTopic]:
    """拉取多个榜单并合并。单个榜单失败不影响其它榜单。

    刻意逐个 GET 而不用 ``POST /api/s/entire``：批量接口只读缓存，
    冷启动的源会被整个略掉，静默少数据比慢几百毫秒糟糕得多。
    """
    from core.config import get_settings

    settings = get_settings()
    boards = source_ids if source_ids is not None else settings.newsnow_source_ids()
    if not boards:
        raise SourceUnavailable("NEWSNOW_SOURCES 为空，没有要拉的榜单")

    base = _base_url(base_url)
    http, owned = _client(client, timeout or settings.sourcing_timeout_seconds)
    collected: list[RawTopic] = []
    failures: list[str] = []
    try:
        for board in boards:
            try:
                topics = fetch_board(board, base_url=base, client=http, latest=latest)
            except NewsNowError as exc:
                failures.append(f"{board}: {exc}")
                logger.warning("newsnow 榜单 %s 拉取失败，跳过: %s", board, exc)
                continue
            if limit_per_board is not None:
                topics = topics[:limit_per_board]
            collected.extend(topics)
    finally:
        if owned:
            http.close()

    if not collected and failures:
        raise NewsNowError("所有 newsnow 榜单都拉取失败：" + "; ".join(failures))
    return collected


__all__ = [
    "KNOWN_SOURCE_IDS",
    "SERVER_ITEM_CAP",
    "SOURCE",
    "UPSTREAM",
    "UPSTREAM_LICENSE",
    "NewsNowError",
    "fetch",
    "fetch_board",
    "parse_source_response",
]
