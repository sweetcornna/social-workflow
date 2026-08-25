"""TrendRadar 采集（GPL sidecar，只走 HTTP 读它产出的文件）。

上游形态见 ``sourcing/trendradar.py`` 的模块 docstring 与
``sidecars/trendradar/README.md``：它**没有 REST API**，8080 上是
``python -m http.server`` 挂出来的 ``output/`` 目录。
"""

from __future__ import annotations

import sqlite3
from datetime import date

import httpx
import pytest
import respx

from sourcing import trendradar
from sourcing.base import SourceUnavailable
from sourcing.trendradar import (
    TrendRadarError,
    date_folder,
    latest_snapshot_name,
    parse_news_db,
    parse_txt_snapshot,
)

BASE = "http://trendradar.test:8080"
DAY = "2026-08-16"

#: 上游 trendradar/storage/schema.sql 的相关部分（只建我们查的两张表）
SCHEMA = """
CREATE TABLE news_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    platform_id TEXT NOT NULL,
    rank INTEGER NOT NULL,
    url TEXT DEFAULT '',
    mobile_url TEXT DEFAULT '',
    first_crawl_time TEXT NOT NULL,
    last_crawl_time TEXT NOT NULL,
    crawl_count INTEGER DEFAULT 1,
    created_at TIMESTAMP,
    updated_at TIMESTAMP
);
CREATE TABLE platforms (
    id TEXT PRIMARY KEY,
    name TEXT,
    is_active INTEGER DEFAULT 1,
    updated_at TIMESTAMP
);
"""

TXT_SNAPSHOT = """weibo | 微博
1. 某地暴雨红色预警 [URL:https://weibo.com/1] [MOBILE:https://m.weibo.cn/1]
2. 某明星官宣 [URL:https://weibo.com/2]

zhihu | 知乎
1. 如何看待通勤时间成本 [URL:https://zhihu.com/1]

==== 以下ID请求失败 ====
douyin
"""


@pytest.fixture
def news_db(tmp_path):
    """造一份和上游 schema 一致的热榜库，返回它的字节。"""
    path = tmp_path / f"{DAY}.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO platforms (id, name) VALUES (?, ?)",
        [("weibo", "微博"), ("zhihu", "知乎")],
    )
    conn.executemany(
        "INSERT INTO news_items "
        "(title, platform_id, rank, url, mobile_url, first_crawl_time, last_crawl_time, crawl_count)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "某地暴雨红色预警",
                "weibo",
                1,
                "https://weibo.com/1",
                "https://m.weibo.cn/1",
                "t",
                "t",
                7,
            ),
            ("某明星官宣", "weibo", 2, "https://weibo.com/2", "", "t", "t", 2),
            ("如何看待通勤时间成本", "zhihu", 1, "https://zhihu.com/1", "", "t", "t", 3),
        ],
    )
    conn.commit()
    conn.close()
    return path


# ---------------------------------------------------------------- 纯解析


def test_parse_news_db(news_db):
    topics = parse_news_db(news_db)
    assert [t.title for t in topics] == [
        "某地暴雨红色预警",
        "如何看待通勤时间成本",
        "某明星官宣",
    ], "按 rank 升序、crawl_count 降序"
    top = topics[0]
    assert top.source == "trendradar"
    assert top.url == "https://weibo.com/1"
    assert top.raw["board"] == "weibo" and top.raw["board_name"] == "微博"
    assert top.raw["crawl_count"] == 7
    # 名次按**每个平台各自**归一化：两个平台的第 1 名都是 1.0
    assert top.score == 1.0
    assert topics[1].score == 1.0
    assert topics[2].score < 1.0


def test_parse_news_db_rejects_foreign_schema(tmp_path):
    path = tmp_path / "wrong.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE something_else (x INTEGER)")
    conn.commit()
    conn.close()
    with pytest.raises(TrendRadarError, match="库结构与预期不符"):
        parse_news_db(path)


def test_parse_txt_snapshot():
    topics = parse_txt_snapshot(TXT_SNAPSHOT)
    assert len(topics) == 3, "失败 id 分节之后的内容不算数据"
    first = topics[0]
    assert first.title == "某地暴雨红色预警"
    assert first.url == "https://weibo.com/1"
    assert first.raw["mobile_url"] == "https://m.weibo.cn/1"
    assert first.raw["board"] == "weibo" and first.raw["board_name"] == "微博"
    assert first.score == 1.0 and topics[1].score == 0.5
    assert topics[2].raw["board"] == "zhihu"
    assert all(t.raw["board"] != "douyin" for t in topics)


def test_parse_txt_snapshot_tolerates_missing_url_tags():
    topics = parse_txt_snapshot("weibo | 微博\n1. 没有链接的条目\n")
    assert topics[0].title == "没有链接的条目" and topics[0].url is None


def test_latest_snapshot_name_picks_the_last():
    html = (
        '<ul><li><a href="09-30.txt">09-30.txt</a></li>'
        '<li><a href="15-00.txt">15-00.txt</a></li>'
        '<li><a href="12-00.txt">12-00.txt</a></li></ul>'
    )
    assert latest_snapshot_name(html) == "15-00.txt"
    assert latest_snapshot_name("<ul></ul>") is None


def test_date_folder_is_iso_not_chinese():
    """老版本是 ``2026年08月16日``，现在是 ISO。写错了就一路 404。"""
    assert date_folder(date(2026, 8, 16)) == "2026-08-16"


# ------------------------------------------------------------------- HTTP


@respx.mock
def test_fetch_db_mode(news_db, monkeypatch):
    monkeypatch.setenv("TRENDRADAR_BASE_URL", BASE)
    monkeypatch.setenv("TRENDRADAR_MODE", "db")
    from core.config import reload_settings

    reload_settings()
    respx.get(f"{BASE}/news/{DAY}.db").mock(
        return_value=httpx.Response(200, content=news_db.read_bytes())
    )
    topics = trendradar.fetch(day=DAY)
    assert len(topics) == 3 and topics[0].raw["mode"] == "db"


@respx.mock
def test_fetch_txt_mode(monkeypatch):
    monkeypatch.setenv("TRENDRADAR_BASE_URL", BASE)
    monkeypatch.setenv("TRENDRADAR_MODE", "txt")
    from core.config import reload_settings

    reload_settings()
    respx.get(f"{BASE}/txt/{DAY}/").mock(
        return_value=httpx.Response(200, text='<a href="09-30.txt">09-30.txt</a>')
    )
    respx.get(f"{BASE}/txt/{DAY}/09-30.txt").mock(
        return_value=httpx.Response(200, text=TXT_SNAPSHOT)
    )
    topics = trendradar.fetch(day=DAY)
    assert len(topics) == 3 and topics[0].raw["mode"] == "txt"


@respx.mock
def test_fetch_auto_falls_back_to_txt(monkeypatch):
    """容器刚起时 .db 还没生成，auto 模式要退到 txt 而不是整源阵亡。"""
    monkeypatch.setenv("TRENDRADAR_BASE_URL", BASE)
    monkeypatch.setenv("TRENDRADAR_MODE", "auto")
    from core.config import reload_settings

    reload_settings()
    respx.get(f"{BASE}/news/{DAY}.db").mock(return_value=httpx.Response(404))
    respx.get(f"{BASE}/txt/{DAY}/").mock(
        return_value=httpx.Response(200, text='<a href="09-30.txt">x</a>')
    )
    respx.get(f"{BASE}/txt/{DAY}/09-30.txt").mock(
        return_value=httpx.Response(200, text=TXT_SNAPSHOT)
    )
    assert len(trendradar.fetch(day=DAY)) == 3


@respx.mock
def test_fetch_db_404_message_is_actionable(monkeypatch):
    monkeypatch.setenv("TRENDRADAR_BASE_URL", BASE)
    from core.config import reload_settings

    reload_settings()
    respx.get(f"{BASE}/news/{DAY}.db").mock(return_value=httpx.Response(404))
    with pytest.raises(TrendRadarError, match="CRON_SCHEDULE"):
        trendradar.fetch_db(day=DAY)


@respx.mock
def test_fetch_db_rejects_html_directory_listing(monkeypatch):
    """路径写错时拿到的是目录列表 HTML，要报得看得懂而不是 sqlite 乱码。"""
    monkeypatch.setenv("TRENDRADAR_BASE_URL", BASE)
    from core.config import reload_settings

    reload_settings()
    respx.get(f"{BASE}/news/{DAY}.db").mock(
        return_value=httpx.Response(200, text="<html>Index of /news</html>")
    )
    with pytest.raises(TrendRadarError, match="不是 SQLite"):
        trendradar.fetch_db(day=DAY)


def test_fetch_without_base_url_is_source_unavailable(monkeypatch):
    """未配置 ≠ 故障。上层按"单源不可用"处理，不阻断其它源。"""
    monkeypatch.setenv("TRENDRADAR_BASE_URL", "")
    from core.config import reload_settings

    reload_settings()
    with pytest.raises(SourceUnavailable, match="TRENDRADAR_BASE_URL"):
        trendradar.fetch()


@respx.mock
def test_health_probe(monkeypatch):
    monkeypatch.setenv("TRENDRADAR_BASE_URL", BASE)
    from core.config import reload_settings

    reload_settings()
    respx.get(f"{BASE}/").mock(return_value=httpx.Response(200, text="Index"))
    assert trendradar.health()["ok"] is True


def test_license_is_declared_as_gpl():
    """红线自查：它必须被标成 GPL 且只作 sidecar。"""
    assert trendradar.UPSTREAM_LICENSE == "GPL-3.0"
