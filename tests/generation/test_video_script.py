"""抖音口播脚本链：清洗、截断、钩子兜底、检索词归一化。全部离线。"""

from __future__ import annotations

import pytest

from generation.video_script import (
    CHARS_PER_SECOND,
    MAX_COVER_TEXT_CHARS,
    MAX_HOOK_CHARS,
    MAX_SCRIPT_CHARS,
    SEARCH_TERMS_RANGE,
    generate_video_script,
    normalize_hashtags,
    normalize_terms,
    strip_unspeakable,
    truncate_spoken,
)
from review.inspect import MAX_DOUYIN_TITLE_CHARS
from tests.p3_helpers import DEMO_HOOK, DEMO_SCRIPT, douyin_llm


def _run(**kwargs):
    return generate_video_script(douyin_llm(**kwargs), topic_title="通勤成本", persona="示例人设")


# ------------------------------------------------------------------ 清洗


def test_strip_unspeakable_keeps_chinese() -> None:
    """踩过的坑：网上流传的 emoji 大区间会把整个 CJK 区删光。"""
    text = "通勤一小时，一年亏掉十五万。价格 89 元，尺寸 60×80cm。"
    assert strip_unspeakable(text) == text


def test_strip_unspeakable_removes_markup_and_emoji() -> None:
    raw = "## 小标题\n- **加粗** #话题 🎉\n1. 编号项"
    out = strip_unspeakable(raw)
    assert "#" not in out and "*" not in out and "🎉" not in out
    assert "小标题" in out and "加粗" in out and "编号项" in out


def test_truncate_spoken_cuts_at_sentence_end() -> None:
    text = "第一句话。第二句话。第三句很长很长很长的话。"
    out = truncate_spoken(text, 12)
    assert out.endswith("。") and len(out) <= 12
    # 没有句读时硬截，但补一个句号收尾（不补省略号，TTS 会读出来）
    assert truncate_spoken("啊" * 30, 10).endswith("。")
    assert truncate_spoken("短", 10) == "短"


# ------------------------------------------------------------------ 检索词


def test_normalize_terms_drops_chinese() -> None:
    terms, warnings = normalize_terms(
        ["Crowded Morning Train", "地铁早高峰", "empty  subway   platform!!"]
    )
    assert terms == ["crowded morning train", "empty subway platform"]
    assert any("非英文" in w for w in warnings)


def test_normalize_terms_dedupes_and_caps_length() -> None:
    terms, _ = normalize_terms(["a b", "A B", "one two three four five six"])
    assert terms[0] == "a b"
    assert terms[1].count(" ") == 3  # 最多 4 个单词


def test_normalize_terms_warns_when_too_few() -> None:
    _, warnings = normalize_terms(["only one"])
    assert any(str(SEARCH_TERMS_RANGE[0]) in w for w in warnings)


def test_normalize_hashtags_strips_hash() -> None:
    assert normalize_hashtags(["#通勤", "通 勤", "通勤"]) == ["通勤"]


# ------------------------------------------------------------------ 生成链


def test_draft_carries_every_field() -> None:
    draft = _run()
    assert draft.title and len(draft.title) <= MAX_DOUYIN_TITLE_CHARS
    assert draft.hook == DEMO_HOOK and len(draft.hook) <= MAX_HOOK_CHARS
    assert draft.script.startswith(DEMO_HOOK)
    assert len(draft.script) <= MAX_SCRIPT_CHARS
    assert len(draft.search_terms) >= SEARCH_TERMS_RANGE[0]
    assert all(t.isascii() for t in draft.search_terms)
    assert len(draft.cover_text) <= MAX_COVER_TEXT_CHARS
    assert draft.hashtags and all(not t.startswith("#") for t in draft.hashtags)
    assert draft.selfcheck is not None and draft.selfcheck.verdict == "pass"
    assert draft.trace["angle"] and draft.trace["script"]
    assert draft.rewritten is False
    assert draft.warnings == []


def test_estimated_seconds_tracks_script_length() -> None:
    draft = _run()
    assert draft.estimated_seconds == pytest.approx(len(draft.script) / CHARS_PER_SECOND, abs=0.1)


def test_caption_appends_hashtags() -> None:
    draft = _run()
    caption = draft.caption()
    assert caption.startswith(draft.hook)
    assert caption.endswith("#" + draft.hashtags[-1])


def test_low_score_triggers_rewrite() -> None:
    draft = generate_video_script(
        douyin_llm(overall=5, verdict="revise", rewritten_script=DEMO_SCRIPT),
        topic_title="通勤成本",
        persona="",
    )
    assert draft.rewritten is True
    assert draft.trace["dehumanized"]


def test_rewrite_that_drops_the_hook_gets_it_back() -> None:
    """钩子是前 3 秒的全部，改写最容易把它冲掉，所以有确定性兜底。"""
    draft = generate_video_script(
        douyin_llm(
            overall=5,
            verdict="revise",
            rewritten_script="我算过一笔账。单程六十分钟，一周五天。",
        ),
        topic_title="通勤成本",
        persona="",
    )
    assert draft.script.startswith(DEMO_HOOK)
    assert any("钩子" in w for w in draft.warnings)


def test_overlong_script_is_truncated_by_sentence() -> None:
    long_script = DEMO_HOOK + "".join(f"这是第{i}句用来撑长度的话。" for i in range(60))
    draft = generate_video_script(
        douyin_llm(script=long_script, verdict="pass"), topic_title="x", persona=""
    )
    assert len(draft.script) <= MAX_SCRIPT_CHARS
    assert draft.script.endswith("。")
    assert any("已按句截断" in w for w in draft.warnings)


def test_missing_title_falls_back_to_hook() -> None:
    draft = generate_video_script(douyin_llm(title="   "), topic_title="通勤成本", persona="")
    assert draft.title == DEMO_HOOK[:MAX_DOUYIN_TITLE_CHARS]
    assert any("没给标题" in w for w in draft.warnings)


def test_markup_in_model_output_is_cleaned() -> None:
    draft = generate_video_script(
        douyin_llm(script="**" + DEMO_HOOK + "**\n\n- 一个要点 #话题"),
        topic_title="x",
        persona="",
    )
    assert "*" not in draft.script and "#" not in draft.script
