"""抖音热榜采集（``lonnyzhang423/douyin-hot-hub``，MIT）。

直接读该仓库的每日归档 JSON，不跑爬虫、不碰抖音私有签名（见 ``docs/POLICY.md``）。

⚠️ **实际仓库结构与任务书描述不同，以下为 2026-08-15 实测核实的结果：**

- ``archives/YYYY-MM-DD.md`` 是 **Markdown**（扁平，没有年月子目录），不是 JSON。
- JSON 在 ``raw/YYYY-MM-DD/<board>.json``，且是**抖音上游 API 的原样转储**，
  不是归一化过的结构。

已核实的 board 文件：

| 文件 | 榜单 | 状态 |
|---|---|---|
| ``hot-search.json`` | 抖音热榜 | 有数据（~49 条） |
| ``hot-music.json`` | 音乐榜 | 有数据（~52 条） |
| ``hot-star.json`` | 明星榜 | 当前为空 ``user_list: []`` |
| ``hot-live.json`` | 直播榜 | 当前 0 字节，榜单已停更 |

``hot-search.json`` 的形状::

    {"status_code": 0, "data": {"word_list": [
        {"word": "日本战败投降81周年", "hot_value": 11639021, "position": 1,
         "view_count": 65297561, "video_count": 14, "event_time": 1786719295,
         "sentence_id": "2610207", "group_id": "6995531927638021384", ...}
    ]}, ...}

**条目里没有 url**，上游仓库自己拼 ``https://www.douyin.com/search/<urlencode(word)>``，
这里照做。
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx

from sourcing.base import RawTopic, SourcingError, rank_score

logger = logging.getLogger("social_workflow.sourcing.douyin_hot_hub")

SOURCE = "douyin_hot_hub"
UPSTREAM = "https://github.com/lonnyzhang423/douyin-hot-hub"
UPSTREAM_LICENSE = "MIT"

#: 抖音搜索页模板，与上游 template/archive.md 一致
SEARCH_URL = "https://www.douyin.com/search/{word}"

#: board 名 → (文件名, 条目所在路径, 标题字段)
BOARDS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "hot-search": ("hot-search.json", ("data", "word_list"), "word"),
    "hot-music": ("hot-music.json", ("music_list",), ""),
}
DEFAULT_BOARD = "hot-search"

#: 当天归档可能还没生成（上游是定时任务），往前回溯几天
DEFAULT_LOOKBACK_DAYS = 3


class DouyinHotHubError(SourcingError):
    """归档拉取或解析失败。"""


def _base_url(explicit: str | None = None) -> str:
    from core.config import get_settings

    base = explicit if explicit is not None else get_settings().douyin_hot_hub_base_url
    return base.rstrip("/")


def archive_url(day: date, board: str = DEFAULT_BOARD, *, base_url: str | None = None) -> str:
    """``{base}/raw/YYYY-MM-DD/<board>.json``。"""
    if board not in BOARDS:
        raise DouyinHotHubError(f"未知 board: {board!r}，可选 {sorted(BOARDS)}")
    filename = BOARDS[board][0]
    return f"{_base_url(base_url)}/raw/{day.isoformat()}/{filename}"


def _dig(payload: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(payload, dict):
            return None
        payload = payload.get(key)
    return payload


def parse_hot_search(payload: dict[str, Any], *, day: date) -> list[RawTopic]:
    """解析 ``hot-search.json``。"""
    entries = _dig(payload, BOARDS["hot-search"][1])
    if not isinstance(entries, list):
        raise DouyinHotHubError(
            f"hot-search 归档缺少 data.word_list（顶层键: {sorted(payload)[:8]}）"
        )
    total = len(entries)
    topics: list[RawTopic] = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            continue
        word = str(entry.get("word") or "").strip()
        if not word:
            continue
        # position 是上游给的名次，缺失时退回遍历序
        position = entry.get("position")
        rank = int(position) if isinstance(position, int) and position > 0 else index
        topics.append(
            RawTopic(
                source=SOURCE,
                title=word,
                url=SEARCH_URL.format(word=quote(word, safe="")),
                score=rank_score(rank, total),
                raw={
                    "board": "hot-search",
                    "archive_date": day.isoformat(),
                    "rank": rank,
                    "hot_value": entry.get("hot_value"),
                    "view_count": entry.get("view_count"),
                    "video_count": entry.get("video_count"),
                    "event_time": entry.get("event_time"),
                    "sentence_id": entry.get("sentence_id"),
                    "group_id": entry.get("group_id"),
                    "label": entry.get("label"),
                },
            )
        )
    return topics


def parse_hot_music(payload: dict[str, Any], *, day: date) -> list[RawTopic]:
    """解析 ``hot-music.json``。标题取 ``music_info.title``。"""
    entries = payload.get("music_list")
    if not isinstance(entries, list):
        raise DouyinHotHubError(f"hot-music 归档缺少 music_list（顶层键: {sorted(payload)[:8]}）")
    total = len(entries)
    topics: list[RawTopic] = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            continue
        info = entry.get("music_info") if isinstance(entry.get("music_info"), dict) else {}
        title = str(info.get("title") or "").strip()
        if not title:
            continue
        author = str(info.get("author") or "").strip()
        topics.append(
            RawTopic(
                source=SOURCE,
                title=f"{title} - {author}" if author else title,
                url=SEARCH_URL.format(word=quote(title, safe="")),
                score=rank_score(index, total),
                raw={
                    "board": "hot-music",
                    "archive_date": day.isoformat(),
                    "rank": index,
                    "heat": entry.get("heat"),
                    "music_id": info.get("id"),
                    "author": author,
                },
            )
        )
    return topics


_PARSERS = {"hot-search": parse_hot_search, "hot-music": parse_hot_music}


def parse_archive(payload: dict[str, Any], *, board: str, day: date) -> list[RawTopic]:
    parser = _PARSERS.get(board)
    if parser is None:
        raise DouyinHotHubError(f"未知 board: {board!r}，可选 {sorted(_PARSERS)}")
    return parser(payload, day=day)


def fetch(
    day: date | None = None,
    *,
    board: str = DEFAULT_BOARD,
    base_url: str | None = None,
    client: httpx.Client | None = None,
    timeout: float | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    limit: int | None = None,
) -> list[RawTopic]:
    """拉取指定日期的归档。当天没有就按天回溯，最多回溯 ``lookback_days`` 天。"""
    from core.config import get_settings

    settings = get_settings()
    target = day or datetime.now(UTC).date()
    base = _base_url(base_url)
    owned = client is None
    http = client or httpx.Client(
        timeout=timeout or settings.sourcing_timeout_seconds, follow_redirects=True
    )

    attempts: list[str] = []
    try:
        for offset in range(max(1, lookback_days)):
            current = target - timedelta(days=offset)
            url = archive_url(current, board, base_url=base)
            try:
                response = http.get(url)
            except httpx.HTTPError as exc:
                attempts.append(f"{current}: 请求失败 {exc}")
                continue
            if response.status_code == 404:
                attempts.append(f"{current}: 404（归档尚未生成）")
                continue
            if response.status_code >= 400:
                attempts.append(f"{current}: HTTP {response.status_code}")
                continue
            if not response.content.strip():
                # hot-live.json 就是 0 字节的，直接当没有
                attempts.append(f"{current}: 空文件")
                continue
            try:
                payload = response.json()
            except ValueError as exc:
                attempts.append(f"{current}: JSON 解析失败 {exc}")
                continue
            if not isinstance(payload, dict):
                attempts.append(f"{current}: 顶层不是对象")
                continue
            topics = parse_archive(payload, board=board, day=current)
            if not topics:
                attempts.append(f"{current}: 归档为空（该榜可能已停更）")
                continue
            logger.info("douyin-hot-hub %s %s 拉到 %d 条", board, current, len(topics))
            return topics[:limit] if limit is not None else topics
    finally:
        if owned:
            http.close()

    raise DouyinHotHubError(f"回溯 {lookback_days} 天都没拿到 {board} 归档：" + "; ".join(attempts))


__all__ = [
    "BOARDS",
    "DEFAULT_BOARD",
    "DEFAULT_LOOKBACK_DAYS",
    "SEARCH_URL",
    "SOURCE",
    "UPSTREAM",
    "UPSTREAM_LICENSE",
    "DouyinHotHubError",
    "archive_url",
    "fetch",
    "parse_archive",
    "parse_hot_music",
    "parse_hot_search",
]
