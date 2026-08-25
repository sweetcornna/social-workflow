"""TrendRadar 热榜聚合采集（``sansan0/TrendRadar``，**GPL-3.0**）。

License 边界
------------
TrendRadar 是 GPL-3.0，**只能作独立进程**（Docker sidecar），本模块只发 HTTP
并解析它**产出的数据文件**，不 import 也不复制其任何代码（docs/POLICY.md）。
sidecar 定义见 ``docker-compose.yml`` 的 ``sourcing`` profile 与
``sidecars/trendradar/README.md``。

已核实的上游形态（2026-08-16 读 master 分支源码，版本 6.10.0）
--------------------------------------------------------------
**它没有 REST JSON API。** 这一点和直觉相反，写客户端前必须知道：

- ``wantcat/trendradar`` 容器 = supercronic 定时跑 ``python -m trendradar``
  （默认 ``*/30 * * * *``）+ ``python -m http.server`` 把 ``/app/output``
  整个目录挂在 **8080** 上（``docker/manage.py``）。**无鉴权、无路由**，
  就是静态文件服务。
- ``wantcat/trendradar-mcp`` 容器 = FastMCP 的 Streamable HTTP（**3333/mcp**，
  JSON-RPC，非 REST，需要 MCP 客户端握手）。本模块**不走**这条：为了拉个热榜
  引入一层 MCP 协议栈不划算。

因此我们读它的产出文件，两种模式（``TRENDRADAR_MODE``）：

``db``（默认，推荐）
    ``GET {base}/news/{YYYY-MM-DD}.db`` —— 路径**完全确定**，不需要解析目录列表。
    SQLite，表结构来自上游 ``trendradar/storage/schema.sql``::

        news_items(id, title, platform_id, rank, url, mobile_url,
                   first_crawl_time, last_crawl_time, crawl_count, ...)
        platforms(id, name, is_active, updated_at)

``txt``
    ``GET {base}/txt/{YYYY-MM-DD}/`` 目录列表 → 取最新的 ``{HH-MM}.txt``。
    行格式（上游 ``storage/local.py:save_txt_snapshot``）::

        {source_id} | {source_name}
        1. {标题} [URL:{url}] [MOBILE:{mobile_url}]
        2. {标题} [URL:...]
        <空行>
        ==== 以下ID请求失败 ====
        {failed_id}

``auto``
    先试 ``db``，404 或解析失败再退 ``txt``。

几个坑
------
- 目录名是 **ISO 日期** ``2026-08-16``，不是老版本的 ``2026年08月16日``。
- ``news_items`` 是**整天累积**的（同一标题只有一行，``crawl_count`` 记命中次数），
  不是某一时刻的快照，所以同一平台里 ``rank`` 会重复出现。
- 容器起来前必须挂好 ``config/config.yaml`` 与 ``config/frequency_words.txt``，
  否则上游 ``entrypoint.sh`` 直接 ``exit 1``。
- TrendRadar 自己**不爬平台**，它转手调 newsnow 的 ``/api/s``
  （上游 ``crawler/fetcher.py`` 的 ``DEFAULT_API_URL``）。所以本源与
  ``sourcing/newsnow.py`` 有天然重叠——跨源去重由 ``persist_topics`` 兜住。

**未核实**：MCP transport 的实际握手报文（没起容器实测）；可选平台的总数
（上游 README 只明确"默认监控 11 个主流平台"，总池由 newsnow 决定）。
"""

from __future__ import annotations

import logging
import re
import sqlite3
import tempfile
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

import httpx

from sourcing.base import RawTopic, SourceUnavailable, SourcingError, rank_score

logger = logging.getLogger("social_workflow.sourcing.trendradar")

SOURCE = "trendradar"
UPSTREAM = "https://github.com/sansan0/TrendRadar"
#: **GPL-3.0**：只作 sidecar，绝不 import
UPSTREAM_LICENSE = "GPL-3.0"
UPSTREAM_IMAGE = "wantcat/trendradar"

Mode = Literal["auto", "db", "txt"]

#: 下载体积上限：一天的热榜库通常几百 KB，超过这个数说明拿错了文件
MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024

#: ``http.server`` 目录列表里的文件名
_HREF = re.compile(r'href="([^"?/]+\.txt)"')
#: TXT 快照的条目行：``1. 标题 [URL:...] [MOBILE:...]``
_ITEM_LINE = re.compile(r"^\s*(\d+)\.\s*(.+?)\s*$")
_URL_TAG = re.compile(r"\[URL:([^\]]*)\]")
_MOBILE_TAG = re.compile(r"\[MOBILE:([^\]]*)\]")
#: 失败 id 分节的分隔行
_FAILED_HEADER = "==== 以下ID请求失败 ===="


class TrendRadarError(SourcingError):
    """TrendRadar 调用 / 解析失败。"""


def _base_url(explicit: str | None = None) -> str:
    from core.config import get_settings

    base = (explicit if explicit is not None else get_settings().trendradar_base_url).strip()
    if not base:
        raise SourceUnavailable(
            "TRENDRADAR_BASE_URL 未配置：TrendRadar 数据源不可用。"
            "启动 sidecar：docker compose --profile sourcing up -d trendradar，"
            "然后在 .env 里填 http://localhost:8080（容器内写 http://trendradar:8080）。"
        )
    return base.rstrip("/")


def _client(client: httpx.Client | None, timeout: float) -> tuple[httpx.Client, bool]:
    if client is not None:
        return client, False
    return httpx.Client(timeout=timeout, follow_redirects=True), True


def date_folder(day: date | datetime | str | None = None) -> str:
    """上游的日期目录名：ISO ``YYYY-MM-DD``（``utils/time.py: format_date_folder``）。"""
    if isinstance(day, str):
        return day
    if isinstance(day, datetime):
        return day.astimezone(UTC).date().isoformat()
    return (day or datetime.now(UTC).date()).isoformat()


# ------------------------------------------------------------------ SQLite 模式


def parse_news_db(path: Path, *, limit: int | None = None) -> list[RawTopic]:
    """解析 ``output/news/{date}.db``。表结构见模块 docstring。"""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise TrendRadarError(f"打开 TrendRadar SQLite 失败: {exc}") from exc
    try:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT n.title       AS title,
                       n.platform_id AS platform_id,
                       n.rank        AS rank,
                       n.url         AS url,
                       n.mobile_url  AS mobile_url,
                       n.crawl_count AS crawl_count,
                       n.first_crawl_time AS first_crawl_time,
                       n.last_crawl_time  AS last_crawl_time,
                       p.name        AS platform_name
                FROM news_items n
                LEFT JOIN platforms p ON p.id = n.platform_id
                ORDER BY n.rank ASC, n.crawl_count DESC
                """
            ).fetchall()
        except sqlite3.Error as exc:
            # 上游换 schema 时这里会炸，报错要说清是哪一步而不是一句 "no such table"
            raise TrendRadarError(
                f"TrendRadar 库结构与预期不符（上游 schema.sql 变了？）: {exc}"
            ) from exc
    finally:
        conn.close()

    # 每个平台各自归一化名次：跨平台名次不可比，第 1 名一律 1.0
    per_platform: dict[str, int] = defaultdict(int)
    for row in rows:
        board = str(row["platform_id"] or "")
        per_platform[board] = max(per_platform[board], int(row["rank"] or 0))

    topics: list[RawTopic] = []
    for row in rows:
        title = str(row["title"] or "").strip()
        if not title:
            continue
        board = str(row["platform_id"] or "")
        rank = int(row["rank"] or 0) or 1
        topics.append(
            RawTopic(
                source=SOURCE,
                title=title,
                url=str(row["url"] or "") or str(row["mobile_url"] or "") or None,
                score=rank_score(rank, per_platform.get(board) or rank),
                raw={
                    "board": board,
                    "board_name": row["platform_name"],
                    "rank": rank,
                    "mobile_url": row["mobile_url"],
                    # 同一条热榜在一天里被抓到过几次——比单次名次更能说明"持续热"
                    "crawl_count": row["crawl_count"],
                    "first_crawl_time": row["first_crawl_time"],
                    "last_crawl_time": row["last_crawl_time"],
                    "mode": "db",
                },
            )
        )
        if limit is not None and len(topics) >= limit:
            break
    return topics


def fetch_db(
    *,
    base_url: str | None = None,
    client: httpx.Client | None = None,
    timeout: float | None = None,
    day: date | str | None = None,
    limit: int | None = None,
) -> list[RawTopic]:
    """下载并解析当天的 SQLite 热榜库。"""
    from core.config import get_settings

    base = _base_url(base_url)
    folder = date_folder(day)
    http, owned = _client(client, timeout or get_settings().sourcing_timeout_seconds)
    url = f"{base}/news/{folder}.db"
    try:
        response = http.get(url)
        if response.status_code == 404:
            raise TrendRadarError(
                f"TrendRadar 还没有 {folder} 的热榜库（{url} 404）。"
                "容器刚起时属正常，等一个 CRON_SCHEDULE 周期（默认 30 分钟），"
                "或用 IMMEDIATE_RUN=true 让它立刻跑一次。"
            )
        response.raise_for_status()
        payload = response.content
    except httpx.HTTPError as exc:
        raise TrendRadarError(f"TrendRadar 请求失败（{url}）: {exc}") from exc
    finally:
        if owned:
            http.close()

    if len(payload) > MAX_DOWNLOAD_BYTES:
        raise TrendRadarError(f"{url} 体积异常（{len(payload)} 字节），拒绝解析")
    if not payload.startswith(b"SQLite format 3\x00"):
        raise TrendRadarError(f"{url} 不是 SQLite 文件（拿到的可能是目录列表 HTML）")

    with tempfile.TemporaryDirectory(prefix="trendradar-") as tmpdir:
        local = Path(tmpdir) / "news.db"
        local.write_bytes(payload)
        topics = parse_news_db(local, limit=limit)
    logger.info("TrendRadar(db) %s 拉到 %d 条", folder, len(topics))
    return topics


# --------------------------------------------------------------------- TXT 模式


def parse_txt_snapshot(text: str, *, limit: int | None = None) -> list[RawTopic]:
    """解析 ``output/txt/{date}/{HH-MM}.txt``。行格式见模块 docstring。"""
    topics: list[RawTopic] = []
    board = ""
    board_name = ""
    pending: list[tuple[int, str, str | None, str | None]] = []
    boards: list[tuple[str, str, list[tuple[int, str, str | None, str | None]]]] = []

    def flush() -> None:
        if board and pending:
            boards.append((board, board_name, list(pending)))
        pending.clear()

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(_FAILED_HEADER[:8]) and "请求失败" in stripped:
            break  # 后面全是失败的 source id，不是内容
        if "|" in stripped and not _ITEM_LINE.match(stripped):
            flush()
            head, _, tail = stripped.partition("|")
            board, board_name = head.strip(), tail.strip()
            continue
        match = _ITEM_LINE.match(stripped)
        if not match or not board:
            continue
        rank = int(match.group(1))
        body = match.group(2)
        url_match = _URL_TAG.search(body)
        mobile_match = _MOBILE_TAG.search(body)
        title = _MOBILE_TAG.sub("", _URL_TAG.sub("", body)).strip()
        if not title:
            continue
        pending.append(
            (
                rank,
                title,
                (url_match.group(1).strip() or None) if url_match else None,
                (mobile_match.group(1).strip() or None) if mobile_match else None,
            )
        )
    flush()

    for board_id, name, entries in boards:
        total = max((rank for rank, *_ in entries), default=1)
        for rank, title, url, mobile in entries:
            topics.append(
                RawTopic(
                    source=SOURCE,
                    title=title,
                    url=url or mobile,
                    score=rank_score(rank, total),
                    raw={
                        "board": board_id,
                        "board_name": name,
                        "rank": rank,
                        "mobile_url": mobile,
                        "mode": "txt",
                    },
                )
            )
            if limit is not None and len(topics) >= limit:
                return topics
    return topics


def latest_snapshot_name(index_html: str) -> str | None:
    """从 ``http.server`` 的目录列表里挑最新的 ``{HH-MM}.txt``。"""
    names = sorted(set(_HREF.findall(index_html)))
    return names[-1] if names else None


def fetch_txt(
    *,
    base_url: str | None = None,
    client: httpx.Client | None = None,
    timeout: float | None = None,
    day: date | str | None = None,
    limit: int | None = None,
) -> list[RawTopic]:
    """取当天最新一份 TXT 快照。"""
    from core.config import get_settings

    base = _base_url(base_url)
    folder = date_folder(day)
    http, owned = _client(client, timeout or get_settings().sourcing_timeout_seconds)
    listing_url = f"{base}/txt/{folder}/"
    try:
        listing = http.get(listing_url)
        if listing.status_code == 404:
            raise TrendRadarError(f"TrendRadar 还没有 {folder} 的 TXT 快照（{listing_url} 404）")
        listing.raise_for_status()
        name = latest_snapshot_name(listing.text)
        if not name:
            raise TrendRadarError(f"{listing_url} 目录里没有 .txt 快照")
        snapshot = http.get(f"{listing_url}{name}")
        snapshot.raise_for_status()
        text = snapshot.text
    except httpx.HTTPError as exc:
        raise TrendRadarError(f"TrendRadar 请求失败（{listing_url}）: {exc}") from exc
    finally:
        if owned:
            http.close()

    topics = parse_txt_snapshot(text, limit=limit)
    logger.info("TrendRadar(txt) %s/%s 拉到 %d 条", folder, name, len(topics))
    return topics


# ------------------------------------------------------------------------ 入口


def fetch(
    *,
    base_url: str | None = None,
    client: httpx.Client | None = None,
    timeout: float | None = None,
    day: date | str | None = None,
    limit: int | None = None,
    mode: Mode | None = None,
) -> list[RawTopic]:
    """拉取 TrendRadar 聚合热榜并归一化成 :class:`~sourcing.base.RawTopic`。

    与其它采集器同一契约：未配置抛 :class:`~sourcing.base.SourceUnavailable`，
    拉取 / 解析失败抛 :class:`TrendRadarError`，上层按"单源失败不阻断"处理。
    """
    from core.config import get_settings

    resolved: Mode = mode or get_settings().trendradar_mode  # type: ignore[assignment]
    if resolved not in ("auto", "db", "txt"):
        raise TrendRadarError(f"TRENDRADAR_MODE={resolved!r} 非法，允许 auto/db/txt")

    kwargs: dict[str, Any] = {
        "base_url": base_url,
        "client": client,
        "timeout": timeout,
        "day": day,
        "limit": limit,
    }
    if resolved == "db":
        return fetch_db(**kwargs)
    if resolved == "txt":
        return fetch_txt(**kwargs)
    try:
        return fetch_db(**kwargs)
    except TrendRadarError as exc:
        logger.info("TrendRadar db 模式不可用，退 txt：%s", exc)
        return fetch_txt(**kwargs)


def health(
    *,
    base_url: str | None = None,
    client: httpx.Client | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """探活：静态服务是否在（preflight 用）。不下载热榜库。"""
    base = _base_url(base_url)
    http, owned = _client(client, timeout)
    try:
        response = http.get(f"{base}/")
        return {
            "ok": response.status_code < 500,
            "status_code": response.status_code,
            "base_url": base,
        }
    except httpx.HTTPError as exc:
        raise TrendRadarError(f"TrendRadar 不可达（{base}）: {exc}") from exc
    finally:
        if owned:
            http.close()


__all__ = [
    "MAX_DOWNLOAD_BYTES",
    "SOURCE",
    "UPSTREAM",
    "UPSTREAM_IMAGE",
    "UPSTREAM_LICENSE",
    "Mode",
    "TrendRadarError",
    "date_folder",
    "fetch",
    "fetch_db",
    "fetch_txt",
    "health",
    "latest_snapshot_name",
    "parse_news_db",
    "parse_txt_snapshot",
]
