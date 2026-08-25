"""小红书文案链：五步调用顺序、兜底截断、标签归一、prompt 变量对齐。全部离线。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import prompts
from core.budget import BudgetGuard, CostKind
from core.dev_flow import make_xhs_scripted_llm
from generation.llm import LLMError, ScriptedLLM
from generation.textutil import strip_zero_width, truncate
from generation.xhs_note import (
    MAX_BULLET_CHARS,
    MAX_COVER_HEADLINE_CHARS,
    MAX_PAGE_HEADLINE_CHARS,
    PageSpec,
    XhsCardPlan,
    XhsNoteCopy,
    XhsQualityError,
    XhsRevision,
    XhsSelfCheck,
    generate_xhs_note,
    normalize_pages,
    normalize_tags,
)
from review.inspect import MAX_XHS_BODY_CHARS, MAX_XHS_TITLE_CHARS, XHS_PAGE_RANGE, XHS_TAG_RANGE
from tests.p2_helpers import DEMO_PAGES, xhs_llm

# ------------------------------------------------------------------ 归一化


def test_normalize_tags_strips_hash_and_dedupes() -> None:
    tags = normalize_tags(["#租房", " 租房 ", "小户型 收纳", "", "独居#"])
    assert tags == ["租房", "小户型收纳", "独居"]


def test_normalize_tags_respects_limit() -> None:
    assert len(normalize_tags([f"标签{i}" for i in range(20)], limit=5)) == 5


def test_normalize_tags_keeps_case_distinct() -> None:
    """OOTD 与 ootd 在平台上确实是两个话题，不能当成重复合并。"""
    assert normalize_tags(["OOTD", "ootd"]) == ["OOTD", "ootd"]


def test_normalize_pages_truncates_long_text() -> None:
    pages, warnings = normalize_pages(
        [PageSpec(headline="标" * 40, bullets=["点" * 60, "短一点"], footnote="注" * 50)]
    )
    assert len(pages[0].headline) <= MAX_PAGE_HEADLINE_CHARS
    assert all(len(b) <= MAX_BULLET_CHARS for b in pages[0].bullets)
    # 只有一页，低于建议下限要留 warning
    assert any("低于建议下限" in w for w in warnings)


def test_normalize_pages_clamps_page_count() -> None:
    too_many = [PageSpec(headline=f"第{i}页", bullets=["a", "b"]) for i in range(12)]
    pages, warnings = normalize_pages(too_many)
    assert len(pages) == XHS_PAGE_RANGE[1]
    assert any("超过" in w for w in warnings)


def test_normalize_pages_drops_headless_page() -> None:
    pages, warnings = normalize_pages([PageSpec(headline="  ", bullets=["x"]), *DEMO_PAGES])
    assert len(pages) == len(DEMO_PAGES)
    assert any("没有 headline" in w for w in warnings)


def test_strip_zero_width_removes_invisible_chars() -> None:
    assert strip_zero_width("租​房﻿") == "租房"


# ------------------------------------------------------------------ 生成链


def test_generate_xhs_note_runs_five_steps() -> None:
    llm = xhs_llm()
    draft = generate_xhs_note(llm, topic_title="租房收纳", persona="独居号")

    purposes = [c["purpose"] for c in llm.calls]
    assert purposes[:4] == ["xhs.angle", "xhs.cards", "xhs.note", "xhs.selfcheck"]
    # 初检 pass 且无 blocking issue → 不触发整包修订
    assert "xhs.dehumanize" not in purposes
    assert purposes.count("xhs.selfcheck") == 1
    assert draft.rewritten is False
    assert draft.title == "租房不打孔，我多出一面墙"
    assert draft.cover_headline == "不打孔，多出一面墙"
    assert len(draft.pages) == len(DEMO_PAGES)
    assert draft.page_count == len(DEMO_PAGES) + 1  # 封面另算一张
    assert "angle" in draft.trace and "cards" in draft.trace


def test_failed_selfcheck_revises_whole_package_then_passes_final_check() -> None:
    revision_pages = [
        PageSpec(headline="修订页一", bullets=["修订数字 1", "修订数字 2"]),
        PageSpec(headline="修订页二", bullets=["修订动作 1", "修订动作 2"]),
        PageSpec(headline="修订页三", bullets=["修订结果 1", "修订结果 2"]),
    ]
    llm = xhs_llm(
        verdict="revise",
        overall=5,
        revision=XhsRevision(
            title="修订标题",
            alt_titles=["修订备选"],
            body="修订后的正文只认这一版",
            tags=["修订标签一", "修订标签二", "修订标签三"],
            cover_headline="修订封面",
            pages=revision_pages,
            image_prompts=["revised safe image prompt"],
        ),
    )
    draft = generate_xhs_note(
        llm,
        topic_title="租房收纳",
        persona="独居号",
        image_prompt_count=1,
    )
    assert [call["purpose"] for call in llm.calls] == [
        "xhs.angle",
        "xhs.cards",
        "xhs.note",
        "xhs.selfcheck",
        "xhs.dehumanize",
        "xhs.selfcheck",
    ]
    assert draft.rewritten is True
    assert draft.title == "修订标题"
    assert draft.alt_titles == ["修订备选"]
    assert draft.body == "修订后的正文只认这一版\n\n#修订标签一 #修订标签二 #修订标签三"
    assert draft.tags == ["修订标签一", "修订标签二", "修订标签三"]
    assert draft.cover_headline == "修订封面"
    assert draft.pages == revision_pages
    assert draft.image_prompts == ["revised safe image prompt"]
    assert "修订页一" in draft.trace["cards"]
    assert "dehumanized" in draft.trace


def test_blocking_issue_alone_triggers_rewrite() -> None:
    llm = xhs_llm(verdict="pass", overall=10, blocking_issues=["第二段有绝对化用语"])
    draft = generate_xhs_note(llm, topic_title="x", persona="p")
    assert draft.rewritten is True


def test_force_rewrite_overrides_selfcheck() -> None:
    draft = generate_xhs_note(
        xhs_llm(verdict="pass", overall=10), topic_title="x", persona="p", force_rewrite=True
    )
    assert draft.rewritten is True


def test_force_rewrite_false_cannot_bypass_failed_selfcheck() -> None:
    draft = generate_xhs_note(
        xhs_llm(verdict="revise"),
        topic_title="x",
        persona="p",
        force_rewrite=False,
    )
    assert draft.rewritten is True


@pytest.mark.parametrize(
    ("initial_verdict", "final_verdict"), [("revise", "revise"), ("reject", "reject")]
)
def test_failed_final_check_raises_quality_error(initial_verdict: str, final_verdict: str) -> None:
    llm = xhs_llm(
        verdict=initial_verdict,
        final_verdict=final_verdict,
        final_blocking_issues=["终检仍有跨字段冲突"],
    )
    with pytest.raises(XhsQualityError, match=r"终检.*终检仍有跨字段冲突"):
        generate_xhs_note(llm, topic_title="x", persona="p")
    assert [call["purpose"] for call in llm.calls].count("xhs.dehumanize") == 1
    assert [call["purpose"] for call in llm.calls].count("xhs.selfcheck") == 2


def test_revision_reapplies_hard_limits_before_final_check() -> None:
    long_pages = [
        PageSpec(
            headline="页" * 40,
            bullets=["点" * 60, "第二条", "第三条", "第四条", "第五条"],
            footnote="注" * 50,
        )
        for _ in range(XHS_PAGE_RANGE[1] + 2)
    ]
    llm = xhs_llm(
        verdict="revise",
        revision=XhsRevision(
            title="修订标题" * 10,
            alt_titles=["备选" * 20],
            body="修订正文" * 400,
            tags=[f"修订标签{i}" for i in range(XHS_TAG_RANGE[1] + 2)],
            cover_headline="修订封面" * 10,
            pages=long_pages,
            image_prompts=[],
        ),
    )
    draft = generate_xhs_note(llm, topic_title="x", persona="p")
    assert len(draft.title) <= MAX_XHS_TITLE_CHARS
    assert len(draft.body) <= MAX_XHS_BODY_CHARS
    assert len(draft.cover_headline) <= MAX_COVER_HEADLINE_CHARS
    assert len(draft.pages) == XHS_PAGE_RANGE[1]
    assert all(len(page.headline) <= MAX_PAGE_HEADLINE_CHARS for page in draft.pages)
    assert all(len(bullet) <= MAX_BULLET_CHARS for page in draft.pages for bullet in page.bullets)
    final_prompt = [call for call in llm.calls if call["purpose"] == "xhs.selfcheck"][-1]["prompt"]
    assert draft.title in final_prompt and draft.body in final_prompt


def test_failed_final_check_charges_only_the_bounded_six_calls(session) -> None:
    guard = BudgetGuard(session, token_budget=2000)
    with pytest.raises(XhsQualityError):
        generate_xhs_note(
            xhs_llm(
                budget=guard,
                verdict="revise",
                final_verdict="reject",
                final_blocking_issues=["仍未通过"],
            ),
            topic_title="x",
            persona="p",
        )
    assert guard.used(CostKind.TOKENS) == 1200


def test_selector_angle_marker_reaches_every_generation_prompt_and_draft() -> None:
    marker = "ANGLE_MARKER_7f42"
    llm = xhs_llm(verdict="revise")
    draft = generate_xhs_note(
        llm,
        topic_title="x",
        persona="p",
        suggested_angle=marker,
    )
    assert draft.suggested_angle == marker
    assert draft.as_platform_extra()["suggested_angle"] == marker
    assert all(marker in call["prompt"] for call in llm.calls)


def test_alt_title_marker_is_checked_in_initial_revision_and_final_prompts() -> None:
    marker = "ALT_99_MARKER"
    llm = xhs_llm(verdict="revise", alt_titles=[marker])
    draft = generate_xhs_note(llm, topic_title="x", persona="p")
    selfcheck_prompts = [call["prompt"] for call in llm.calls if call["purpose"] == "xhs.selfcheck"]
    revision_prompt = next(
        call["prompt"] for call in llm.calls if call["purpose"] == "xhs.dehumanize"
    )
    assert len(selfcheck_prompts) == 2
    assert all(marker in prompt for prompt in selfcheck_prompts)
    assert marker in revision_prompt
    assert draft.alt_titles == [marker]
    assert draft.as_platform_extra()["alt_titles"] == [marker]


def test_empty_selector_angle_is_backward_compatible() -> None:
    draft = generate_xhs_note(xhs_llm(), topic_title="x", persona="p")
    assert draft.suggested_angle == ""


def test_title_and_body_are_hard_truncated() -> None:
    """模型超字数是常态，长度是平台硬限制，不能只靠 prompt 约束。"""
    draft = generate_xhs_note(
        xhs_llm(title="超长标题" * 10, body="正文" * 900),
        topic_title="x",
        persona="p",
    )
    assert len(draft.title) <= MAX_XHS_TITLE_CHARS
    assert len(draft.body) <= MAX_XHS_BODY_CHARS
    assert any("超过" in w for w in draft.warnings)


def test_cover_headline_is_truncated() -> None:
    draft = generate_xhs_note(xhs_llm(cover_headline="封面" * 20), topic_title="x", persona="p")
    assert len(draft.cover_headline) <= MAX_COVER_HEADLINE_CHARS


def test_empty_title_falls_back_to_cover_headline() -> None:
    draft = generate_xhs_note(xhs_llm(title="   "), topic_title="x", persona="p")
    assert draft.title == "不打孔，多出一面墙"
    assert any("没给标题" in w for w in draft.warnings)


def test_too_few_tags_produces_warning() -> None:
    draft = generate_xhs_note(xhs_llm(tags=["租房"]), topic_title="x", persona="p")
    assert draft.tags == ["租房"]
    assert any("低于建议下限" in w for w in draft.warnings)


def test_body_with_tags_stays_within_platform_limit() -> None:
    """标签也算正文字数——加标签不能把整条笔记撑过 1000 字。"""
    draft = generate_xhs_note(
        xhs_llm(body="正" * MAX_XHS_BODY_CHARS, tags=["租房", "小户型收纳", "独居"]),
        topic_title="x",
        persona="p",
    )
    combined = draft.body_with_tags()
    assert combined == draft.body
    assert len(combined) <= MAX_XHS_BODY_CHARS
    assert combined.endswith("#独居")


def test_system_prompt_carries_persona() -> None:
    llm = xhs_llm()
    generate_xhs_note(llm, topic_title="x", persona="我是一个测试人设标记")
    assert "我是一个测试人设标记" in llm.calls[0]["system"]


def test_generation_charges_tokens(session) -> None:
    guard = BudgetGuard(session)
    generate_xhs_note(xhs_llm(budget=guard), topic_title="x", persona="p")
    # 4 次调用 × (100 输入 + 100 输出)
    assert guard.used(CostKind.TOKENS) == 800


def test_scripted_llm_missing_reply_is_actionable() -> None:
    """预置回复缺类型时要报清楚缺哪个，而不是抛个 IndexError。"""
    llm = ScriptedLLM(replies=["angle"], parsed_replies=[])
    with pytest.raises(LLMError, match="XhsCardPlan"):
        generate_xhs_note(llm, topic_title="x", persona="p")


def test_dev_flow_scripted_llm_drives_whole_chain() -> None:
    """core.dev_flow 的预置替身必须能喂饱整条小红书链。"""
    draft = generate_xhs_note(make_xhs_scripted_llm(), topic_title="租房收纳", persona="独居号")
    assert draft.title
    assert len(draft.pages) >= XHS_PAGE_RANGE[0]
    assert XHS_TAG_RANGE[0] <= len(draft.tags) <= XHS_TAG_RANGE[1]


def test_as_platform_extra_carries_pages_and_selfcheck() -> None:
    draft = generate_xhs_note(xhs_llm(), topic_title="x", persona="p")
    extra = draft.as_platform_extra()
    assert extra["cover_headline"]
    assert len(extra["pages"]) == len(DEMO_PAGES)
    assert extra["selfcheck"]["verdict"] == "pass"
    assert extra["rewritten"] is False


# ------------------------------------------------------------------ 结构化模型


def test_page_spec_rejects_unknown_fields() -> None:
    """``extra="forbid"``：模型多吐字段要炸在解析处，而不是被静默吞掉。"""
    with pytest.raises(ValidationError):
        PageSpec.model_validate({"headline": "x", "unexpected": 1})


def test_selfcheck_score_range_is_enforced() -> None:
    with pytest.raises(ValidationError):
        XhsSelfCheck.model_validate(
            {
                "ai_flavor": 42,
                "specificity": 8,
                "hook": 8,
                "card_fit": 8,
                "tag_fit": 8,
                "compliance_risk": 8,
                "overall": 8,
                "verdict": "pass",
            }
        )


def test_note_copy_defaults_are_empty_lists() -> None:
    copy = XhsNoteCopy(title="t", body="b")
    assert copy.tags == [] and copy.alt_titles == []


def test_card_plan_accepts_empty_pages() -> None:
    """解析层不做数量校验——数量交给 normalize_pages 统一处理并留 warning。"""
    assert XhsCardPlan(cover_headline="x").pages == []


# ------------------------------------------------------------------ prompts


def test_all_xhs_prompts_render() -> None:
    """prompt 与调用方的变量必须对得上，缺变量要在测试里就炸出来。"""
    expected = {
        "xhs/system": {"persona"},
        "xhs/angle": {
            "topic_title",
            "topic_source",
            "topic_url",
            "topic_context",
            "suggested_angle",
            "page_hint",
        },
        "xhs/cards": {
            "angle",
            "suggested_angle",
            "max_cover",
            "min_pages",
            "max_pages",
            "max_headline",
            "min_bullets",
            "max_bullets",
            "max_bullet",
            "max_footnote",
        },
        "xhs/note": {
            "angle",
            "suggested_angle",
            "cards",
            "max_title",
            "max_body",
            "target_body",
            "min_tags",
            "max_tags",
            # P11 起写正文那一步顺手产出配图 prompt
            "image_rules",
        },
        "xhs/selfcheck": {
            "suggested_angle",
            "title",
            "alt_titles",
            "body",
            "cards",
            "tags",
            "image_prompts",
        },
        "xhs/dehumanize": {
            "suggested_angle",
            "title",
            "alt_titles",
            "body",
            "cards",
            "tags",
            "image_prompts",
            "issues",
            "max_title",
            "max_body",
            "max_cover",
            "min_pages",
            "max_pages",
            "max_headline",
            "min_bullets",
            "max_bullets",
            "max_bullet",
            "max_footnote",
            "min_tags",
            "max_tags",
            "image_prompt_count",
        },
    }
    for name, variables in expected.items():
        assert prompts.variables_of(name) == variables, f"{name} 的变量集与调用方不一致"
        rendered = prompts.load(name, **dict.fromkeys(variables, "X"))
        assert "{{" not in rendered


def test_selfcheck_prompt_contains_final_images_consistency_and_safety_rules() -> None:
    text = prompts.load(
        "xhs/selfcheck",
        suggested_angle="唯一角度",
        title="标题",
        alt_titles="备选标题",
        body="正文里的 3 个",
        cards="卡片里的 3 个",
        tags="标签",
        image_prompts="IMAGE_MARKER",
    )
    assert "IMAGE_MARKER" in text
    assert "核心数字" in text and "跨字段矛盾" in text
    for safety_rule in ("液体远离插座", "热源远离易燃物", "清洁剂与食品分开", "绊倒线缆"):
        assert safety_rule in text


def test_xhs_persona_exists() -> None:
    persona = prompts.load_persona("xhs-demo-01")
    assert "独居" in persona
    # 人设里必须写死不碰的题材，否则生成链没有合规兜底
    assert "硬约束" in persona


def test_truncate_is_shared_with_wechat_chain() -> None:
    from generation.wechat_article import truncate as wechat_truncate

    assert truncate is wechat_truncate
