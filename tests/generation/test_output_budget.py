"""输出预算分档表：桶数上界、兜底值同步、以及每个调用点真的给足了预算。

2026-08-17 生产事故的根因之一是"生成链一个调用点都没显式传 max_tokens"，
全部落到 4096 的兜底值上。所以这里断言的不是常量长什么样，而是**跑一遍生成链、
看它实际递给 LLM 的 max_tokens 是多少**——常量改对了但忘了传，一样要被打回。
"""

from __future__ import annotations

import pytest

from core.config import Settings
from core.dev_flow import make_douyin_scripted_llm, make_scripted_llm, make_xhs_scripted_llm
from generation.output_budget import (
    CALL_SITE_BUDGETS,
    CEILING_OUTPUT_TOKENS,
    LARGE_OUTPUT_TOKENS,
    OUTPUT_TIERS,
    STANDARD_OUTPUT_TOKENS,
    budget_for,
    escalate,
)
from generation.video_script import generate_video_script
from generation.wechat_article import generate_article
from generation.xhs_note import generate_xhs_note

# --------------------------------------------------------------- 分档表本身


def test_every_call_site_lands_on_a_declared_tier() -> None:
    """调用点只许用梯子上的档位——每多一个取值，dsh 就多起一个 runtime 子进程。"""
    strays = {p: v for p, v in CALL_SITE_BUDGETS.items() if v not in OUTPUT_TIERS}
    assert strays == {}


def test_runtime_bucket_count_stays_within_the_pool_limit() -> None:
    """桶数核算：dsh 按 (effort, max_tokens) 分桶，池子 LRU 上限默认 4。

    没有任何调用点显式传 ``effort``（全部落默认档），所以桶数上界就是档位数。
    真要加第四档，先想清楚它会不会把常驻子进程顶出 LRU。
    """
    assert len(OUTPUT_TIERS) <= 3
    assert len(OUTPUT_TIERS) <= Settings().dsh_max_live_runtimes
    assert sorted(OUTPUT_TIERS) == list(OUTPUT_TIERS), "档位必须从小到大，escalate 依赖这个顺序"


def test_declared_call_sites_only_use_two_of_the_three_tiers() -> None:
    """天花板档是**自愈专用**的：没有调用点显式声明它。

    这条锁的是"正常路径最多两个桶"——第三个桶只在真被截断时才会出现。
    """
    assert set(CALL_SITE_BUDGETS.values()) == {STANDARD_OUTPUT_TOKENS, LARGE_OUTPUT_TOKENS}
    assert CEILING_OUTPUT_TOKENS not in set(CALL_SITE_BUDGETS.values())


def test_fallback_budget_in_settings_matches_the_standard_tier() -> None:
    """``llm_max_tokens`` 是兜底值，必须和标准档同值，否则凭空多一个桶。"""
    settings = Settings()
    assert settings.llm_max_tokens == STANDARD_OUTPUT_TOKENS
    assert settings.llm_article_max_tokens == LARGE_OUTPUT_TOKENS
    # 4096 是事故当天的值：reasoning 模型光思考就能烧光它
    assert settings.llm_max_tokens > 4096


def test_escalate_walks_up_the_ladder_and_stops_at_the_top() -> None:
    assert escalate(STANDARD_OUTPUT_TOKENS) == LARGE_OUTPUT_TOKENS
    assert escalate(LARGE_OUTPUT_TOKENS) == CEILING_OUTPUT_TOKENS
    assert escalate(CEILING_OUTPUT_TOKENS) is None
    # 梯子之外的取值（比如被 SW_DSH_MAX_TOKENS 压过的）也要能抬到下一档
    assert escalate(4096) == STANDARD_OUTPUT_TOKENS
    assert escalate(999_999) is None


def test_escalate_never_invents_a_value_outside_the_ladder() -> None:
    """加码不能用"简单 ×2"：8192×2=16384 不在梯子上，等于给 dsh 多开一个桶。"""
    for tier in OUTPUT_TIERS:
        nxt = escalate(tier)
        assert nxt is None or nxt in OUTPUT_TIERS


def test_unknown_purpose_falls_back_instead_of_exploding() -> None:
    """没登记的 purpose 回落标准档：不能为了一个观测字符串把生成链炸掉。"""
    assert budget_for("某个还没登记的调用点") == STANDARD_OUTPUT_TOKENS


# ------------------------------------------------------- 调用点真的传了预算


def budgets_of(llm) -> dict[str, int | None]:
    """把 ScriptedLLM 记录的调用摊平成 ``{purpose: max_tokens}``。"""
    return {call["purpose"]: call["max_tokens"] for call in llm.calls}


@pytest.mark.parametrize(
    ("make_llm", "run_chain", "expected_purposes"),
    [
        pytest.param(
            make_xhs_scripted_llm,
            lambda llm: generate_xhs_note(
                llm, topic_title="小户型收纳", persona="独居博主", force_rewrite=True
            ),
            ["xhs.angle", "xhs.cards", "xhs.note", "xhs.selfcheck", "xhs.dehumanize"],
            id="xhs",
        ),
        pytest.param(
            make_scripted_llm,
            lambda llm: generate_article(
                llm, topic_title="远程办公", persona="职场作者", force_rewrite=True
            ),
            [
                "wechat.outline",
                "wechat.body",
                "wechat.polish",
                "wechat.selfcheck",
                "wechat.dehumanize",
                "wechat.meta",
            ],
            id="wechat",
        ),
        pytest.param(
            make_douyin_scripted_llm,
            lambda llm: generate_video_script(
                llm, topic_title="通勤成本", persona="生活博主", force_rewrite=True
            ),
            ["douyin.angle", "douyin.script", "douyin.selfcheck", "douyin.dehumanize"],
            id="douyin",
        ),
    ],
)
def test_chain_passes_an_explicit_budget_at_every_call_site(
    make_llm, run_chain, expected_purposes
) -> None:
    """三条生成链的每一步都必须显式传 max_tokens，且取值来自分档表。

    ``None`` 意味着这个调用点在吃 ``llm_max_tokens`` 兜底——事故当天就是全链如此。
    """
    llm = make_llm()
    run_chain(llm)
    budgets = budgets_of(llm)
    assert list(budgets) == expected_purposes, "调用顺序或 purpose 变了，预算表要跟着改"
    for purpose, value in budgets.items():
        assert value is not None, f"{purpose} 没有显式传 max_tokens，会吃兜底值"
        assert value == budget_for(purpose)
        assert value in OUTPUT_TIERS


def test_selector_budget_comes_from_the_same_ladder() -> None:
    """选题 Agent 的 P10.1 常量已经收编进分档表，不是另一套数字。"""
    from sourcing.selector import SELECT_MAX_TOKENS

    assert SELECT_MAX_TOKENS == budget_for("sourcing.select") == LARGE_OUTPUT_TOKENS
