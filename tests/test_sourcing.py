"""选题采集与去重：全部离线，用 fixture JSON + httpx.MockTransport。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from core.models import Topic, new_id, utcnow
from sourcing import douyin_hot_hub, newsnow
from sourcing.base import RawTopic, SourceUnavailable, normalize_batch, persist_topics, rank_score
from sourcing.dedupe import (
    Deduper,
    containment,
    edit_distance,
    hamming,
    is_duplicate,
    normalize_title,
    simhash,
    similarity,
    title_key,
)
from tests.p1_helpers import load_fixture, mock_client

# --------------------------------------------------------------------- 去重


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  台风登陆广东沿海  ", "台风登陆广东沿海"),
        ("1. 台风登陆广东沿海", "台风登陆广东沿海"),
        ("#台风登陆广东沿海# 热", "台风登陆广东沿海"),
        ("ＡＢＣ 123", "abc123"),
        ("台风登陆广东沿海 沸", "台风登陆广东沿海"),
        ("No.3、某事件", "某事件"),
    ],
)
def test_normalize_title(raw: str, expected: str) -> None:
    assert normalize_title(raw) == expected


def test_title_key_is_stable_across_processes() -> None:
    """去重键必须是 hashlib 而不是内置 hash()——后者带进程级随机盐。"""
    assert title_key("台风登陆广东") == title_key(" #台风登陆广东# ")
    assert len(title_key("x")) == 64


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        # 语序重排：编辑距离救不了，靠字符集包含度
        ("某明星回应争议事件", "某明星就争议事件作出回应", True),
        # 加后缀
        ("台风登陆广东沿海", "台风登陆广东沿海地区", True),
        ("国足输球", "国足输球了", True),
        # 同义改写
        ("新能源车销量创新高", "新能源汽车销量创下新高", True),
        # 一字之差但意思相反，必须判为不同
        ("A股放量大涨", "A股放量大跌", False),
        ("国足输球", "国足赢球", False),
        ("北京今日暴雨", "上海今日暴雨", False),
        # 完全无关
        ("某明星回应争议事件", "国足输球球迷失望", False),
        # 短标题不能被包含度误伤
        ("苹果", "苹果发布会新品汇总", False),
    ],
)
def test_is_duplicate(a: str, b: str, expected: bool) -> None:
    assert is_duplicate(a, b) is expected
    # 判定必须对称
    assert is_duplicate(b, a) is expected


def test_similarity_and_edit_distance() -> None:
    assert edit_distance("abc", "abc") == 0
    assert edit_distance("abc", "abd") == 1
    assert edit_distance("", "abc") == 3
    assert similarity("台风登陆", "台风登陆") == 1.0
    assert 0.0 <= similarity("台风登陆", "地震发生") < 0.3


def test_simhash_is_deterministic() -> None:
    first = simhash("台风登陆广东沿海")
    assert first == simhash("台风登陆广东沿海")
    assert hamming(first, first) == 0
    assert simhash("") == 0


def test_containment_ignores_word_order() -> None:
    assert containment("某明星回应争议事件", "某明星就争议事件作出回应") == 1.0
    assert containment("", "abc") == 0.0


def test_deduper_add_and_len() -> None:
    deduper = Deduper()
    assert deduper.add("台风登陆广东沿海") is True
    assert deduper.add("台风登陆广东沿海地区") is False  # 重复
    assert deduper.add("") is False  # 空标题不入库
    assert deduper.add("完全不相干的另一个热点话题") is True
    assert len(deduper) == 2
    assert deduper.seen("1. #台风登陆广东沿海# 热") is True


def _disjoint_titles(count: int, length: int = 8, stride: int = 12) -> list[str]:
    """造 ``count`` 条**字符集互不相交**的标题（stride > length 保证不重叠）。"""
    return ["".join(chr(0x4E00 + i * stride + k) for k in range(length)) for i in range(count)]


def test_deduper_keeps_all_disjoint_titles() -> None:
    """字符集完全不相交的标题一条都不该被误判成重复。"""
    titles = _disjoint_titles(300)
    deduper = Deduper()
    assert deduper.extend(titles) == 300
    assert len(deduper) == 300


def test_deduper_still_catches_duplicates_in_a_large_pool() -> None:
    """大池子里也要抓得住重复——倒排索引不能为了快而漏召回。"""
    titles = _disjoint_titles(300)
    deduper = Deduper()
    deduper.extend(titles)
    # 给其中一条加后缀，应判为重复
    assert deduper.add(titles[150] + "后续进展") is False
    assert len(deduper) == 300


# ----------------------------------------------------------------- base


def test_rank_score_monotonic() -> None:
    assert rank_score(1, 10) == 1.0
    assert rank_score(10, 10) == pytest.approx(0.1)
    assert rank_score(5, 10) > rank_score(6, 10)
    assert rank_score(1, 0) == 0.0
    # 越界名次被夹住而不是抛异常
    assert rank_score(99, 10) == rank_score(10, 10)


def test_raw_topic_validation() -> None:
    topic = RawTopic(source="newsnow", title="  标题  ", score=5.0)
    assert topic.title == "标题"
    assert topic.score == 1.0  # 夹到 [0,1]
    with pytest.raises(ValueError, match="title 不能为空"):
        RawTopic(source="x", title="   ")


def test_normalize_batch_keeps_first_occurrence() -> None:
    topics = [
        RawTopic(source="a", title="台风登陆广东沿海"),
        RawTopic(source="b", title="台风登陆广东沿海地区"),
        RawTopic(source="c", title="另一个独立的热点事件话题"),
    ]
    result = normalize_batch(topics)
    assert [t.source for t in result] == ["a", "c"]


def test_persist_topics_dedupes_against_db(session) -> None:
    session.add(
        Topic(id=new_id("tpc"), source="newsnow", title="台风登陆广东沿海", score=0.9, raw={})
    )
    session.flush()

    created = persist_topics(
        session,
        [
            RawTopic(source="newsnow", title="台风登陆广东沿海地区", score=0.8),
            RawTopic(source="douyin_hot_hub", title="一个全新的独立热点事件", score=0.7),
        ],
    )
    assert len(created) == 1
    assert created[0].title == "一个全新的独立热点事件"


def test_persist_topics_ignores_rows_outside_window(session) -> None:
    old = Topic(id=new_id("tpc"), source="newsnow", title="台风登陆广东沿海", score=0.9, raw={})
    old.created_at = utcnow() - timedelta(days=30)
    session.add(old)
    session.flush()

    created = persist_topics(session, [RawTopic(source="newsnow", title="台风登陆广东沿海")])
    assert len(created) == 1  # 30 天前的不参与去重


def test_load_candidates_uses_shared_clock_at_24_hour_boundary(session, monkeypatch) -> None:
    """候选窗口必须跟 Topic 默认时间用同一时钟，不能偷偷退回真实墙钟。"""
    from sourcing.selector import load_candidates

    anchor = datetime(2031, 4, 5, 6, 7, 8, tzinfo=UTC)
    monkeypatch.setattr("sourcing.selector.utcnow", lambda: anchor)
    session.add_all(
        [
            Topic(
                id=new_id("tpc"),
                source="boundary",
                title="恰好二十四小时仍在窗口内",
                score=0.9,
                raw={},
                created_at=anchor - timedelta(hours=24),
            ),
            Topic(
                id=new_id("tpc"),
                source="boundary",
                title="超过二十四小时一微秒应淘汰",
                score=1.0,
                raw={},
                created_at=anchor - timedelta(hours=24, microseconds=1),
            ),
        ]
    )
    session.flush()

    candidates, rows = load_candidates(session, sources=["boundary"])

    assert [topic.title for topic in candidates] == ["恰好二十四小时仍在窗口内"]
    assert [row.title for row in rows] == ["恰好二十四小时仍在窗口内"]


# -------------------------------------------------------------- newsnow


def test_newsnow_parse_fixture() -> None:
    payload = load_fixture("newsnow_weibo.json")
    topics = newsnow.parse_source_response(payload, source_id="weibo")

    assert len(topics) == 3  # 空标题那条被丢掉
    first = topics[0]
    assert first.source == "newsnow"
    assert first.title == "台风登陆广东沿海"
    assert first.score == 1.0
    assert first.raw["board"] == "weibo"
    assert first.raw["rank"] == 1
    assert first.raw["mobile_url"] == "https://m.weibo.cn/search?containerid=1"
    assert first.raw["status"] == "success"
    # 第二条带 extra.info（字符串热度，原样保留）
    assert topics[1].raw["info"] == "312 万热度"
    assert topics[2].raw["hover"] == "当事人今日发布长文回应"
    # 名次靠后分数更低
    assert topics[0].score > topics[1].score > topics[2].score


def test_newsnow_fetch_board_uses_expected_endpoint() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["query"] = str(request.url.query.decode())
        return httpx.Response(200, json=load_fixture("newsnow_weibo.json"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        topics = newsnow.fetch_board("weibo", base_url="http://newsnow.test", client=client)

    assert captured["path"] == "/api/s"
    assert "id=weibo" in captured["query"]
    assert len(topics) == 3


def test_newsnow_invalid_source_id_gives_readable_error() -> None:
    """上游用 HTTP 500 表达"非法 id"，不能原样冒泡成看不懂的 500。"""
    routes = {"/api/s": (500, {"error": True, "message": "Invalid source id"})}
    with (
        mock_client(routes) as client,
        pytest.raises(newsnow.NewsNowError, match="不在已知榜单列表里"),
    ):
        newsnow.fetch_board("nonexistent-board", base_url="http://x.test", client=client)


def test_newsnow_requires_base_url(monkeypatch) -> None:
    monkeypatch.setenv("NEWSNOW_BASE_URL", "")
    from core.config import reload_settings

    reload_settings()
    with pytest.raises(SourceUnavailable, match="NEWSNOW_BASE_URL"):
        newsnow.fetch(["weibo"])


def test_newsnow_fetch_survives_one_bad_board() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "zhihu" in request.url.query.decode():
            return httpx.Response(500, json={"message": "Invalid source id"})
        return httpx.Response(200, json=load_fixture("newsnow_weibo.json"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        topics = newsnow.fetch(["weibo", "zhihu"], base_url="http://x.test", client=client)
    assert len(topics) == 3  # weibo 的照常返回


def test_newsnow_fetch_all_boards_failing_raises() -> None:
    routes = {"/api/s": (500, {"message": "Invalid source id"})}
    with (
        mock_client(routes) as client,
        pytest.raises(newsnow.NewsNowError, match="所有 newsnow 榜单都拉取失败"),
    ):
        newsnow.fetch(["weibo", "zhihu"], base_url="http://x.test", client=client)


def test_newsnow_cross_board_dedupe() -> None:
    """微博和知乎的同一件事只应入库一条。"""
    weibo = newsnow.parse_source_response(load_fixture("newsnow_weibo.json"), source_id="weibo")
    zhihu = newsnow.parse_source_response(load_fixture("newsnow_zhihu.json"), source_id="zhihu")
    merged = normalize_batch(weibo + zhihu)
    titles = [t.title for t in merged]
    assert "某明星回应争议事件" in titles
    assert "某明星就争议事件作出回应" not in titles


# -------------------------------------------------- douyin-hot-hub


def test_douyin_archive_url_shape() -> None:
    """实测：JSON 在 raw/YYYY-MM-DD/，不是 archives/（archives 是 Markdown）。"""
    url = douyin_hot_hub.archive_url(
        date(2026, 8, 15), "hot-search", base_url="https://raw.test/repo/main"
    )
    assert url == "https://raw.test/repo/main/raw/2026-08-15/hot-search.json"


def test_douyin_unknown_board() -> None:
    with pytest.raises(douyin_hot_hub.DouyinHotHubError, match="未知 board"):
        douyin_hot_hub.archive_url(date(2026, 8, 15), "hot-live")


def test_douyin_parse_fixture() -> None:
    payload = load_fixture("douyin_hot_search.json")
    topics = douyin_hot_hub.parse_hot_search(payload, day=date(2026, 8, 15))

    assert len(topics) == 2  # 空 word 那条被丢掉
    first = topics[0]
    assert first.title == "台风登陆广东沿海地区"
    assert first.source == "douyin_hot_hub"
    # 条目里没有 url，按上游做法拼搜索页
    assert first.url is not None and first.url.startswith("https://www.douyin.com/search/")
    assert "%E5%8F%B0%E9%A3%8E" in first.url  # 标题被 urlencode
    assert first.raw["hot_value"] == 11639021
    assert first.raw["rank"] == 1
    assert first.raw["archive_date"] == "2026-08-15"
    assert first.score > topics[1].score


def test_douyin_fetch_falls_back_to_previous_day() -> None:
    """当天归档还没生成时应回溯，而不是直接失败。"""
    today = date(2026, 8, 15)
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if "2026-08-15" in request.url.path:
            return httpx.Response(404)
        return httpx.Response(200, json=load_fixture("douyin_hot_search.json"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        topics = douyin_hot_hub.fetch(today, base_url="https://raw.test/repo/main", client=client)
    assert len(topics) == 2
    assert any("2026-08-14" in path for path in requested)


def test_douyin_fetch_gives_up_after_lookback() -> None:
    routes: dict[str, object] = {}
    with (
        mock_client(routes) as client,
        pytest.raises(douyin_hot_hub.DouyinHotHubError, match="回溯"),
    ):
        douyin_hot_hub.fetch(
            date(2026, 8, 15),
            base_url="https://raw.test/repo/main",
            client=client,
            lookback_days=2,
        )


def test_douyin_empty_file_is_treated_as_missing() -> None:
    """hot-live.json 实测是 0 字节，不能当成 JSON 去解析。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(douyin_hot_hub.DouyinHotHubError),
    ):
        douyin_hot_hub.fetch(
            date(2026, 8, 15),
            base_url="https://raw.test/x",
            client=client,
            lookback_days=1,
        )


# -------------------------------------------------------------- 选题 Agent 预算


class _KwargsRecordingLLM:
    """只实现 ``parse`` 的假 LLM：把调用 kwargs 原样记下来。

    刻意不用 :class:`generation.llm.ScriptedLLM`：这里还要断言 ``effort`` **没被传**，
    而 ScriptedLLM 记的是"形参值"（没传就是 ``None``），分不出"没传"和"传了 None"。
    """

    def __init__(self, parsed: object) -> None:
        self.parsed = parsed
        self.calls: list[dict[str, object]] = []

    def parse(self, prompt: str, output_format: type, **kwargs: object):
        from generation.llm import ParsedResult, Usage

        self.calls.append(dict(kwargs))
        return ParsedResult(parsed=self.parsed, usage=Usage(), model="fake", stop_reason="end_turn")


def test_select_topics_asks_for_its_own_output_budget() -> None:
    """选题调用必须显式给足 ``max_tokens``，不能吃 ``llm_max_tokens`` 的兜底值。

    2026-08-17 生产事故：prompt 要求给**每条**候选打分（30 条候选光 JSON 正文就 3000+
    token，成本账本实测一次 9047 output token），而默认模型是 reasoning 模型、
    思考与正文共用输出预算 → 正文被 max_tokens 掐断，上层看到的是"回复里找不到任何
    JSON 对象"。P11.2 起取值来自 :mod:`generation.output_budget` 的统一分档表。
    """
    from sourcing.selector import SELECT_MAX_TOKENS, SelectionResult, select_topics

    llm = _KwargsRecordingLLM(SelectionResult(recommended=[], note="不选"))
    select_topics([RawTopic(source="t", title="候选一")], llm, persona="人设")

    assert len(llm.calls) == 1
    kwargs = llm.calls[0]
    assert kwargs["max_tokens"] == SELECT_MAX_TOKENS
    assert SELECT_MAX_TOKENS >= 8192, "低于这个量级，光是全部候选的打分就写不完"
    assert kwargs["purpose"] == "sourcing.select"
    assert "effort" not in kwargs, "缺的是写出来的预算，不是想得更久：effort 保持后端默认"
