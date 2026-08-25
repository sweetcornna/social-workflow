"""generation/llm_dsh.py：注入假 HarnessClient，不起真 runtime。

事件负载全部照抄真 runtime 的 dump（见 tests/generation/test_llm_dsh_live.py 里
的实测口径），所以这里断言的字段路径就是线上会走的那条。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from core.budget import BudgetExhausted, BudgetGuard, CostKind
from core.models import CostLedger
from generation.llm import LLMAPIError, LLMConnectionError, LLMRateLimited, LLMUnavailable
from generation.llm_dsh import (
    PROMPT_CACHE_KEY_LENGTH,
    DshLLM,
    DshRuntimeOptions,
    DshRuntimePool,
    RuntimeKey,
    Usage,
    audit_composition,
    audit_provider_models,
    compose_prompt,
    estimate_tokens,
    iter_json_objects,
    load_composition,
    parse_last_json,
    prompt_cache_key,
    provider_api_key_env,
    provider_cache_retention,
    schema_instruction,
    session_id_for,
    stable_prefix,
    tool_schemas_in,
    usage_from_events,
)
from generation.output_budget import (
    CEILING_OUTPUT_TOKENS,
    LARGE_OUTPUT_TOKENS,
    OUTPUT_TIERS,
    STANDARD_OUTPUT_TOKENS,
)

CORDIS_PATH = "configs/dsh/cordis.yml"

#: 只关心"池子怎么分桶"的用例用这个占位键；取值本身由前缀缓存那一组用例钉。
ANY_KEY = prompt_cache_key(model="placeholder", system=None)


class Score(BaseModel):
    score: int
    reason: str


# ------------------------------------------------------------------ 事件构造


def usage_chunk(turn: int = 1, step: int = 1, **counts: int) -> dict[str, Any]:
    payload = {"inputTokens": 234, "outputTokens": 56, "cacheReadTokens": 1000, **counts}
    return {
        "type": "assistant/chunk",
        "data": {"turn": turn, "step": step, "chunk": {"type": "usage", "usage": payload}},
    }


def assistant_message(text: str, *, turn: int = 1, step: int = 1, usage: dict | None = None):
    data: dict[str, Any] = {
        "turn": turn,
        "step": step,
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }
    if usage is not None:
        data["usage"] = usage
    return {"type": "assistant/message", "data": data}


def request_header(model: str = "deepseek-v4-flash", tools: list | None = None):
    header: dict[str, Any] = {"config": {"provider": "deepseek-official", "model": model}}
    if tools is not None:
        header["tools"] = tools
    return {"type": "request/header", "data": {"header": header, "reason": "initial"}}


def turn_end(kind: str = "completed", *, code: str | None = None, message: str = ""):
    reason: dict[str, Any] = {"kind": kind}
    if code is not None:
        reason["error"] = {"code": code, "message": message}
    return {"type": "turn/end", "data": {"turn": 1, "reason": reason}}


def ok_events(text: str = "hello", *, model: str = "deepseek-v4-flash") -> list[dict[str, Any]]:
    return [
        request_header(model),
        usage_chunk(),
        assistant_message(
            text, usage={"inputTokens": 234, "outputTokens": 56, "cacheReadTokens": 1000}
        ),
        turn_end("completed"),
    ]


class FakeRunResult:
    def __init__(self, text: str, finish_reason: str | None, events: list[dict[str, Any]]) -> None:
        self.final_response = text
        self.finish_reason = finish_reason
        self.events = events
        self.session_id = "session-fake"
        self.notifications: list[Any] = []


class FakeHarness:
    """按脚本吐 RunResult 的假 runtime。记录 prompt，方便断言 schema 注入与重试。"""

    def __init__(self, script: list[FakeRunResult] | None = None) -> None:
        self.script = list(script or [])
        self.prompts: list[str] = []
        self.session_ids: list[str] = []
        self.closed = 0
        self.raise_on_run: Exception | None = None

    # 形参名 input 与 SDK 的 Session.run 一致
    def run(self, input: str, *, session_id: str) -> FakeRunResult:
        self.prompts.append(input)
        self.session_ids.append(session_id)
        if self.raise_on_run is not None:
            raise self.raise_on_run
        if not self.script:
            return FakeRunResult("hello", "completed", ok_events())
        return self.script.pop(0)

    def close(self) -> None:
        self.closed += 1


def make_llm(
    script: list[FakeRunResult] | None = None,
    *,
    budget: BudgetGuard | None = None,
    **kwargs: Any,
) -> tuple[DshLLM, FakeHarness, DshRuntimePool]:
    harness = FakeHarness(script)
    options = DshRuntimeOptions(
        provider="deepseek-official",
        model="deepseek-v4-flash",
        cordis_path=Path(CORDIS_PATH),
        session_root=Path("data/dsh_sessions"),
        cwd=Path("."),
        request_timeout_seconds=60.0,
        stream_idle_timeout_ms=60000,
        max_live_runtimes=2,
    )
    pool = DshRuntimePool(options, factory=lambda key, opts: harness)
    return DshLLM(pool=pool, budget=budget, **kwargs), harness, pool


def enable_test_routing(llm: DshLLM) -> None:
    llm.model_routing = True
    llm.sol_model = "gpt-5.6-sol"
    llm.luna_model = "gpt-5.6-luna"


# ------------------------------------------------------------------ 零工具红线


def test_shipped_cordis_composition_has_zero_tools() -> None:
    """红线的静态那一半：仓库自带的受限组合里不许有任何工具/执行器插件。"""
    entries = load_composition(CORDIS_PATH)
    assert audit_composition(entries) == []


def test_audit_catches_a_reintroduced_tool() -> None:
    """审计本身得有效：随便加回一个工具插件必须被拦。"""
    entries = load_composition(CORDIS_PATH)
    entries.append({"id": "bash", "name": "@deepseek-ai/dsh-bash-local"})
    findings = audit_composition(entries)
    assert findings and "dsh-bash-local" in findings[0]


def test_audit_catches_spine_tool_switch_flipped() -> None:
    entries = load_composition(CORDIS_PATH)
    for entry in entries:
        if "agent-spine-demo" in entry["name"]:
            entry["config"]["toolBash"] = {}
    assert any("toolBash" in f for f in audit_composition(entries))


def test_cordis_holds_no_secret_literals() -> None:
    """凭据只能以 apiKeyEnv 引用出现，文件里不许有密钥字面量。"""
    raw = Path(CORDIS_PATH).read_text(encoding="utf-8")
    for marker in ("sk-", "Bearer ", "api_key:", "apiKey:"):
        assert marker not in raw, f"cordis.yml 里出现了疑似密钥字面量: {marker}"
    entries = load_composition(CORDIS_PATH)
    assert provider_api_key_env(entries, "deepseek-official") == "DEEPSEEK_API_KEY"
    assert provider_api_key_env(entries, "anthropic") == "ANTHROPIC_API_KEY"
    assert provider_api_key_env(entries, "gateway") == "SW_DSH_GATEWAY_API_KEY"
    assert provider_api_key_env(entries, "不存在的路由") is None


def test_tool_schemas_in_reads_request_header() -> None:
    assert tool_schemas_in(ok_events()) == []
    leaked = [request_header(tools=[{"name": "bash"}])]
    assert tool_schemas_in(leaked) == [{"name": "bash"}]


def _provider_with_effort_mapping(required_effort: str, mapped_effort: str) -> list[dict[str, Any]]:
    return [
        {
            "name": "@deepseek-ai/dsh-llm-pi-ai",
            "config": {
                "providers": {
                    "gateway": {
                        "models": [
                            {
                                "id": "model-under-test",
                                "reasoningEfforts": {required_effort: mapped_effort},
                            }
                        ]
                    }
                }
            },
        }
    ]


@pytest.mark.parametrize(
    ("effort", "mapped"), [("xhigh", "high"), ("xhigh", "low"), ("max", "low")]
)
def test_provider_model_audit_rejects_reasoning_effort_downgrades(effort, mapped) -> None:
    findings = audit_provider_models(
        _provider_with_effort_mapping(effort, mapped),
        "gateway",
        {"model-under-test": {effort}},
    )
    assert len(findings) == 1
    assert f"{effort}:{effort}" in findings[0]
    assert f"{effort}:{mapped}" in findings[0]


@pytest.mark.parametrize("effort", ["xhigh", "max"])
def test_provider_model_audit_accepts_only_exact_reasoning_effort_mapping(effort) -> None:
    assert (
        audit_provider_models(
            _provider_with_effort_mapping(effort, effort),
            "gateway",
            {"model-under-test": {effort}},
        )
        == []
    )


# ------------------------------------------------------------------ complete


def test_complete_returns_text_and_charges_real_usage(session) -> None:
    guard = BudgetGuard(session, token_budget=10_000)
    llm, harness, _ = make_llm(budget=guard)

    result = llm.complete("写个大纲", system="你是编辑", purpose="test.outline")

    assert result.text == "hello"
    assert result.model == "deepseek-v4-flash"
    # dsh 的 completed 归一到 Anthropic 词汇，两个后端对上层同形
    assert result.stop_reason == "end_turn"
    # 四个桶互不重叠：billable 只算新增 input + output
    assert result.usage == Usage(input_tokens=234, output_tokens=56, cache_read_input_tokens=1000)
    assert guard.used(CostKind.TOKENS) == 290
    entry = session.query(CostLedger).one()
    assert entry.meta["backend"] == "dsh"
    assert "estimated" not in entry.meta
    # system 没有 per-call 通道，只能拼进用户消息
    assert harness.prompts[0].startswith("【系统设定】\n你是编辑")


@pytest.mark.parametrize(
    ("method", "purpose", "expected_model", "expected_effort", "response"),
    [
        ("complete", "sourcing.select", "gpt-5.6-sol", "xhigh", "完成"),
        # medium 档（wechat.body）与 low 档（xhs.selfcheck）都落在 luna @ max
        ("complete_long", "wechat.body", "gpt-5.6-luna", "max", "长文"),
        ("parse", "xhs.selfcheck", "gpt-5.6-luna", "max", '{"score": 8, "reason": "好"}'),
    ],
)
def test_every_entry_point_routes_model_and_effort(
    method, purpose, expected_model, expected_effort, response
) -> None:
    llm, _, pool = make_llm(
        [FakeRunResult(response, "completed", ok_events(response, model=expected_model))]
    )
    enable_test_routing(llm)

    if method == "parse":
        result = llm.parse("打分", Score, purpose=purpose)
    else:
        result = getattr(llm, method)("写作", purpose=purpose)

    key = pool.live_keys[0]
    assert (key.model, key.effort) == (expected_model, expected_effort)
    assert result.model == expected_model


def test_dsh_client_reads_the_routing_switch_and_models_from_settings(monkeypatch) -> None:
    from core.config import reload_settings

    monkeypatch.setenv("SW_LLM_BACKEND", "dsh")
    monkeypatch.setenv("SW_DSH_MODEL_ROUTING", "true")
    monkeypatch.setenv("SW_DSH_LUNA_MODEL", "luna-from-settings")
    reload_settings()
    llm, _, pool = make_llm(
        [FakeRunResult("完成", "completed", ok_events("完成", model="luna-from-settings"))]
    )

    # xhs.note 是 medium 档：模型跟着 SW_DSH_LUNA_MODEL 走，effort 是 max
    llm.complete("hi", purpose="xhs.note")

    assert pool.live_keys[0].model == "luna-from-settings"
    assert pool.live_keys[0].effort == "max"


def test_explicit_effort_wins_but_keeps_the_routed_model() -> None:
    llm, _, pool = make_llm()
    enable_test_routing(llm)

    llm.complete("hi", purpose="xhs.selfcheck", effort="high")

    assert pool.live_keys == [
        RuntimeKey(model="gpt-5.6-luna", effort="high", max_tokens=STANDARD_OUTPUT_TOKENS)
    ]


def test_same_effort_and_budget_do_not_share_a_runtime_across_models() -> None:
    created: list[RuntimeKey] = []

    def factory(key: RuntimeKey, options: DshRuntimeOptions) -> FakeHarness:
        created.append(key)
        return FakeHarness()

    options = DshRuntimeOptions(
        provider="p",
        model="legacy",
        cordis_path=Path(CORDIS_PATH),
        session_root=Path("data/dsh_sessions"),
        cwd=Path("."),
        request_timeout_seconds=1.0,
        stream_idle_timeout_ms=1000,
        max_live_runtimes=4,
    )
    llm = DshLLM(pool=DshRuntimePool(options, factory=factory))
    enable_test_routing(llm)

    # 显式 effort 把三次调用拉到同一档，只剩"模型"这一维不同——正是本用例要证的那一维。
    llm.complete("a", purpose="review.semantic", effort="max")
    llm.complete("b", purpose="xhs.angle", effort="max")
    llm.complete("c", purpose="xhs.note", effort="max")

    assert [key.model for key in created] == ["gpt-5.6-sol", "gpt-5.6-luna"]
    assert len(llm.pool.live_keys) == 2, "同一个 Luna 桶必须复用"


def test_routed_runtime_lru_evicts_by_full_model_key() -> None:
    created: list[FakeHarness] = []

    def factory(key: RuntimeKey, options: DshRuntimeOptions) -> FakeHarness:
        harness = FakeHarness()
        created.append(harness)
        return harness

    options = DshRuntimeOptions(
        provider="p",
        model="legacy",
        cordis_path=Path(CORDIS_PATH),
        session_root=Path("data/dsh_sessions"),
        cwd=Path("."),
        request_timeout_seconds=1.0,
        stream_idle_timeout_ms=1000,
        max_live_runtimes=2,
    )
    pool = DshRuntimePool(options, factory=factory)
    for model in ("sol", "luna", "legacy"):
        pool.run("hi", model=model, effort="xhigh", max_tokens=100, cache_key=ANY_KEY)

    assert [key.model for key in pool.live_keys] == ["luna", "legacy"]
    assert created[0].closed == 1


def test_routed_ledger_records_actual_model_effort_and_complexity(session) -> None:
    guard = BudgetGuard(session, token_budget=10_000)
    model = "gpt-5.6-luna"
    llm, _, _ = make_llm(
        [FakeRunResult("完成", "completed", ok_events("完成", model=model))], budget=guard
    )
    enable_test_routing(llm)

    result = llm.complete("写", purpose="xhs.note")

    entry = session.query(CostLedger).one()
    assert result.model == model
    assert entry.meta["model"] == model
    assert entry.meta["purpose"] == "xhs.note"
    assert entry.meta["effort"] == "max"
    # 模型与 low 档相同，但账本记的仍是 medium——档位标签没有被合并掉
    assert entry.meta["complexity"] == "medium"
    assert "endpoint" not in entry.meta and "api_key" not in entry.meta


def test_routed_truncation_retry_sticks_to_the_original_model_and_effort() -> None:
    llm, _, pool = make_llm(
        [
            FakeRunResult("", "max-tokens", ok_events("", model="gpt-5.6-luna")),
            FakeRunResult("完成", "completed", ok_events("完成", model="gpt-5.6-luna")),
        ]
    )
    enable_test_routing(llm)

    llm.complete("hi", purpose="xhs.angle")

    assert {key.model for key in pool.live_keys} == {"gpt-5.6-luna"}
    assert {key.effort for key in pool.live_keys} == {"max"}


def test_routed_parse_format_retry_sticks_to_the_original_route() -> None:
    good = '{"score": 8, "reason": "扎实"}'
    llm, harness, pool = make_llm(
        [
            FakeRunResult("不是 JSON", "completed", ok_events(model="gpt-5.6-luna")),
            FakeRunResult(good, "completed", ok_events(good, model="gpt-5.6-luna")),
        ]
    )
    enable_test_routing(llm)

    llm.parse("打分", Score, purpose="xhs.selfcheck")

    assert len(harness.prompts) == 2
    assert pool.live_keys == [
        RuntimeKey(model="gpt-5.6-luna", effort="max", max_tokens=STANDARD_OUTPUT_TOKENS)
    ]


def test_complete_long_uses_article_budget() -> None:
    llm, _, pool = make_llm(article_max_tokens=16000, max_tokens=4096)
    llm.complete("hi")
    llm.complete_long("hi")
    assert {key.max_tokens for key in pool.live_keys} == {4096, 16000}


def test_effort_and_max_tokens_split_runtimes() -> None:
    llm, _, pool = make_llm(max_tokens=4096)
    llm.complete("hi", effort="high")
    llm.complete("hi", effort="max")
    assert {key.effort for key in pool.live_keys} == {"high", "max"}


def test_pool_evicts_least_recently_used() -> None:
    created: list[FakeHarness] = []

    def factory(key: RuntimeKey, options: DshRuntimeOptions) -> FakeHarness:
        harness = FakeHarness()
        created.append(harness)
        return harness

    options = DshRuntimeOptions(
        provider="p",
        model="m",
        cordis_path=Path(CORDIS_PATH),
        session_root=Path("data/dsh_sessions"),
        cwd=Path("."),
        request_timeout_seconds=1.0,
        stream_idle_timeout_ms=1000,
        max_live_runtimes=2,
    )
    pool = DshRuntimePool(options, factory=factory)
    for effort in ("low", "medium", "high"):
        pool.run("hi", model="m", effort=effort, max_tokens=100, cache_key=ANY_KEY)
    assert len(pool.live_keys) == 2
    assert created[0].closed == 1  # 最久没用的那个被关掉了


def test_dead_subprocess_becomes_llm_unavailable_and_is_recycled() -> None:
    llm, harness, pool = make_llm()
    harness.raise_on_run = RuntimeError("runtime stdout closed")
    with pytest.raises(LLMUnavailable):
        llm.complete("hi")
    assert harness.closed == 1
    assert pool.live_keys == []


def test_close_is_idempotent() -> None:
    llm, harness, _ = make_llm()
    llm.complete("hi")
    llm.close()
    llm.close()
    assert harness.closed == 1  # 池子清空后第二次 close 不会重复关


# ------------------------------------------------------------------ finish_reason 分支


def test_max_tokens_keeps_truncated_text() -> None:
    """与 Anthropic 后端同语义：截断不抛异常，把已有文本交给调用方。"""
    events = [*ok_events("半句话"), turn_end("max-tokens")]
    llm, _, _ = make_llm([FakeRunResult("半句话", "max-tokens", events)])
    result = llm.complete("hi")
    assert result.text == "半句话"
    assert result.stop_reason == "max_tokens"


# ------------------------------------------------------------------ 截断自愈（P11.2）
#
# 2026-08-17 生产事故：xhs.angle 被 max_tokens 掐停且零输出，_raise_for_finish 在
# _invoke **内部**直接抛死，parse() 的重试循环只 try 住了 parse_last_json——
# 这条路径连一次重试都没有，整条生成链 502。


def test_empty_truncation_retries_with_a_bigger_budget(session) -> None:
    guard = BudgetGuard(session, token_budget=10_000)
    llm, harness, pool = make_llm(
        [
            FakeRunResult("", "max-tokens", ok_events("")),
            FakeRunResult("补完了", "completed", ok_events("补完了")),
        ],
        budget=guard,
    )

    result = llm.complete("hi", purpose="xhs.angle")

    assert result.text == "补完了"
    assert len(harness.prompts) == 2
    assert harness.prompts[0] == harness.prompts[1], "加码重试发的是同一个 prompt"
    # 加码落在梯子的下一档，不是凭空 ×2（×2 会造出 16384 这种梯子外的桶）
    assert {key.max_tokens for key in pool.live_keys} == {
        STANDARD_OUTPUT_TOKENS,
        LARGE_OUTPUT_TOKENS,
    }
    # 两次的 usage 都要算：第一次烧掉的 token 是真花了的，账本要诚实
    assert result.usage.billable == 290 * 2
    assert guard.used(CostKind.TOKENS) == 290 * 2
    assert session.query(CostLedger).count() == 2


def test_two_truncations_in_a_row_raise_with_both_budgets(session) -> None:
    """两次都截断才抛，且文案要带上两次的预算——运维一眼看出是预算不够。"""
    guard = BudgetGuard(session, token_budget=10_000)
    llm, harness, _ = make_llm(
        [
            FakeRunResult("", "max-tokens", ok_events("")),
            FakeRunResult("", "max-tokens", ok_events("")),
        ],
        budget=guard,
    )

    with pytest.raises(LLMAPIError) as excinfo:
        llm.complete("hi")

    message = str(excinfo.value)
    assert "两次都被 max_tokens 截断" in message
    assert str(STANDARD_OUTPUT_TOKENS) in message
    assert str(LARGE_OUTPUT_TOKENS) in message
    assert len(harness.prompts) == 2
    assert guard.used(CostKind.TOKENS) == 290 * 2, "抛错也要把两次烧掉的 token 记满"


def test_truncation_at_the_ceiling_raises_without_a_pointless_retry() -> None:
    """已经在最高档还被截断：重试也是同样的预算，不白烧一遍。"""
    llm, harness, _ = make_llm(
        [FakeRunResult("", "max-tokens", ok_events(""))],
        max_tokens=CEILING_OUTPUT_TOKENS,
    )
    with pytest.raises(LLMAPIError, match="已是上限"):
        llm.complete("hi")
    assert len(harness.prompts) == 1


def test_hard_cap_that_flattens_the_escalation_skips_the_retry(monkeypatch) -> None:
    """``SW_DSH_MAX_TOKENS`` 把加码压回原值时同样不重试——同样的预算重来一次没有意义。"""
    from core.config import reload_settings

    monkeypatch.setenv("SW_DSH_MAX_TOKENS", str(STANDARD_OUTPUT_TOKENS))
    reload_settings()
    llm, harness, _ = make_llm([FakeRunResult("", "max-tokens", ok_events(""))])
    with pytest.raises(LLMAPIError, match="已是上限"):
        llm.complete("hi")
    assert len(harness.prompts) == 1


def test_self_healing_only_ever_asks_for_ladder_buckets() -> None:
    """自愈加码走同一张梯子，所以 runtime 桶数上界还是档位数，装得进 LRU。"""
    created: list[RuntimeKey] = []
    harness = FakeHarness(
        [
            FakeRunResult("", "max-tokens", ok_events("")),  # 标准档截断 → 抬到大档
            FakeRunResult("ok", "completed", ok_events("ok")),
            FakeRunResult("", "max-tokens", ok_events("")),  # 大档截断 → 抬到天花板
            FakeRunResult("ok", "completed", ok_events("ok")),
        ]
    )

    def factory(key: RuntimeKey, options: DshRuntimeOptions) -> FakeHarness:
        created.append(key)
        return harness

    options = DshRuntimeOptions(
        provider="p",
        model="m",
        cordis_path=Path(CORDIS_PATH),
        session_root=Path("data/dsh_sessions"),
        cwd=Path("."),
        request_timeout_seconds=1.0,
        stream_idle_timeout_ms=1000,
        max_live_runtimes=4,
    )
    llm = DshLLM(pool=DshRuntimePool(options, factory=factory), budget=None)
    llm.complete("hi")
    llm.complete("hi", max_tokens=LARGE_OUTPUT_TOKENS)

    budgets = {key.max_tokens for key in created}
    assert budgets <= set(OUTPUT_TIERS)
    assert len(budgets) <= options.max_live_runtimes


def test_parse_escalates_from_the_budget_actually_used_not_the_original() -> None:
    """``_invoke`` 内部已经加过码时，``parse`` 要接着往上抬，而不是抬回刚被截断的那一档。

    这是最坏情况的完整链路：空截断（标准档）→ 自愈到大档 → 大档吐了半截 JSON →
    parse 再抬到天花板档才写完。三档全用上了，正好是桶数上界。
    """
    half = '{"score": 8, "reason": "扎'
    good = '{"score": 8, "reason": "扎实"}'
    created: list[RuntimeKey] = []
    harness = FakeHarness(
        [
            FakeRunResult("", "max-tokens", ok_events("")),
            FakeRunResult(half, "max-tokens", ok_events(half)),
            FakeRunResult(good, "completed", ok_events(good)),
        ]
    )

    def factory(key: RuntimeKey, options: DshRuntimeOptions) -> FakeHarness:
        created.append(key)
        return harness

    options = DshRuntimeOptions(
        provider="p",
        model="m",
        cordis_path=Path(CORDIS_PATH),
        session_root=Path("data/dsh_sessions"),
        cwd=Path("."),
        request_timeout_seconds=1.0,
        stream_idle_timeout_ms=1000,
        max_live_runtimes=4,
    )
    llm = DshLLM(pool=DshRuntimePool(options, factory=factory), budget=None)

    result = llm.parse("打分", Score, purpose="xhs.cards")

    assert result.parsed.score == 8
    assert [key.max_tokens for key in created] == [
        STANDARD_OUTPUT_TOKENS,
        LARGE_OUTPUT_TOKENS,
        CEILING_OUTPUT_TOKENS,
    ]
    # 三次尝试的用量一次都不能漏
    assert result.usage.billable == 290 * 3


def test_ledger_records_stop_reason_and_the_budget(session) -> None:
    """截断要能事后查：P11.2 之前 meta 里没有 stop_reason，只能靠 output_tokens 猜。"""
    guard = BudgetGuard(session, token_budget=10_000)
    llm, _, _ = make_llm([FakeRunResult("半句话", "max-tokens", ok_events("半句话"))], budget=guard)

    llm.complete("hi", max_tokens=4096, purpose="xhs.cards")

    entry = session.query(CostLedger).one()
    assert entry.meta["stop_reason"] == "max_tokens"
    assert entry.meta["max_tokens"] == 4096
    assert entry.meta["purpose"] == "xhs.cards"


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("MISSING_CREDENTIAL", LLMUnavailable),
        ("INVALID_CREDENTIAL", LLMUnavailable),
        ("UNKNOWN_MODEL", LLMUnavailable),
        ("RATE_LIMIT", LLMRateLimited),
        ("TIMEOUT", LLMConnectionError),
        ("TRANSPORT", LLMConnectionError),
        ("QUOTA", LLMAPIError),
        ("SERVER", LLMAPIError),
        ("UNKNOWN", LLMAPIError),
    ],
)
def test_error_code_maps_to_exception(code: str, expected: type[Exception]) -> None:
    events = [request_header(), turn_end("error", code=code, message="boom")]
    llm, _, _ = make_llm([FakeRunResult("", "error", events)])
    with pytest.raises(expected) as excinfo:
        llm.complete("hi")
    assert code in str(excinfo.value)


def test_missing_turn_end_is_api_error() -> None:
    llm, _, _ = make_llm([FakeRunResult("", None, [request_header()])])
    with pytest.raises(LLMAPIError, match="turn/end"):
        llm.complete("hi")


def test_blocked_turn_is_refusal() -> None:
    from generation.llm import GenerationRefused

    llm, _, _ = make_llm([FakeRunResult("", "blocked", [request_header(), turn_end("blocked")])])
    with pytest.raises(GenerationRefused):
        llm.complete("hi")


def test_failed_turn_still_charges_tokens_already_spent(session) -> None:
    """失败也要记账：token 已经真实花掉了，账本不能漏。"""
    guard = BudgetGuard(session, token_budget=10_000)
    events = [request_header(), usage_chunk(), turn_end("error", code="SERVER", message="500")]
    llm, _, _ = make_llm([FakeRunResult("", "error", events)], budget=guard)
    with pytest.raises(LLMAPIError):
        llm.complete("hi")
    assert guard.used(CostKind.TOKENS) == 290


def test_budget_exhaustion_propagates(session) -> None:
    guard = BudgetGuard(session, token_budget=10)
    llm, _, _ = make_llm(budget=guard)
    with pytest.raises(BudgetExhausted):
        llm.complete("hi")


# ------------------------------------------------------------------ usage 提取


def test_usage_prefers_final_message_and_sums_across_steps() -> None:
    events = [
        # step 1：chunk 与 message 报同一笔，不能重复计
        usage_chunk(step=1, inputTokens=10, outputTokens=1, cacheReadTokens=0),
        assistant_message(
            "a", step=1, usage={"inputTokens": 10, "outputTokens": 2, "cacheReadTokens": 0}
        ),
        # step 2：dsh 的 retry 会开新 step，每次尝试都可能计费，要累加
        usage_chunk(step=2, inputTokens=100, outputTokens=5, cacheReadTokens=7),
    ]
    assert usage_from_events(events) == Usage(
        input_tokens=110, output_tokens=7, cache_read_input_tokens=7
    )


def test_usage_reads_cache_write_bucket() -> None:
    events = [
        assistant_message("a", usage={"inputTokens": 1, "outputTokens": 2, "cacheWriteTokens": 3})
    ]
    assert usage_from_events(events).cache_creation_input_tokens == 3


def test_usage_absent_returns_none() -> None:
    assert usage_from_events([request_header(), turn_end()]) is None


def test_estimated_usage_marks_ledger(session) -> None:
    """provider 没报 usage 时按字符保守估算，并在流水里标 estimated。"""
    guard = BudgetGuard(session, token_budget=10_000)
    events = [request_header(), assistant_message("你好世界"), turn_end("completed")]
    llm, _, _ = make_llm([FakeRunResult("你好世界", "completed", events)], budget=guard)

    result = llm.complete("写四个字")

    assert result.usage.billable > 0
    entry = session.query(CostLedger).one()
    assert entry.meta["estimated"] is True
    assert entry.meta["backend"] == "dsh"


def test_estimate_tokens_counts_cjk_conservatively() -> None:
    assert estimate_tokens("") == 0
    # CJK 一字一 token，拉丁四字符一 token——宁可高估
    assert estimate_tokens("你好") == 2
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("你好abcd") == 3


# ------------------------------------------------------------------ JSON 提取


def test_iter_json_objects_takes_top_level_spans_only() -> None:
    text = '开场白 {"a": {"b": 1}} 中间 {"c": 2} 结尾'
    assert iter_json_objects(text) == ['{"a": {"b": 1}}', '{"c": 2}']


def test_iter_json_objects_ignores_braces_inside_strings() -> None:
    text = '{"a": "}{ 不是括号", "b": "带\\"转义\\""}'
    assert iter_json_objects(text) == [text]


def test_parse_last_json_picks_the_last_object() -> None:
    text = '先给个例子 {"score": 1, "reason": "示例"} 真正答案：{"score": 9, "reason": "好"}'
    assert parse_last_json(text, Score) == Score(score=9, reason="好")


def test_parse_last_json_survives_markdown_fences() -> None:
    text = '说明\n```json\n{"score": 3, "reason": "行"}\n```\n'
    assert parse_last_json(text, Score).score == 3


def test_parse_injects_schema_and_returns_model() -> None:
    reply = '好的。{"score": 8, "reason": "扎实"}'
    llm, harness, _ = make_llm([FakeRunResult(reply, "completed", ok_events(reply))])

    result = llm.parse("给这条选题打分", Score, purpose="test.parse")

    assert result.parsed == Score(score=8, reason="扎实")
    assert "JSON Schema" in harness.prompts[0]
    assert "score" in harness.prompts[0]


def test_parse_retries_once_with_the_validation_error() -> None:
    bad = "我觉得挺好的，没有 JSON。"
    good = '{"score": 5, "reason": "改好了"}'
    llm, harness, pool = make_llm(
        [
            FakeRunResult(bad, "completed", ok_events(bad)),
            FakeRunResult(good, "completed", ok_events(good)),
        ]
    )

    result = llm.parse("打分", Score)

    assert result.parsed.score == 5
    assert len(harness.prompts) == 2
    assert "上一次的回复不合格" in harness.prompts[1]
    # 格式错（不是截断）不加码：预算够，多开一个 runtime 桶纯属浪费
    assert {key.max_tokens for key in pool.live_keys} == {STANDARD_OUTPUT_TOKENS}
    # 两次尝试的用量都要算进去
    assert result.usage.billable == 290 * 2


def test_parse_escalates_the_budget_when_the_json_is_truncated() -> None:
    """结构化输出因**截断**失败时加码重发原 prompt，而不是回喂"JSON 不完整"。

    回喂在这种情况下毫无意义：模型不是不会写，是没地方写；
    回喂反而把 prompt 撑得更长，更容易再截一次。
    """
    half = '{"score": 8, "reason": "扎'
    good = '{"score": 8, "reason": "扎实"}'
    llm, harness, pool = make_llm(
        [
            FakeRunResult(half, "max-tokens", ok_events(half)),
            FakeRunResult(good, "completed", ok_events(good)),
        ]
    )

    result = llm.parse("打分", Score, purpose="xhs.cards")

    assert result.parsed.score == 8
    assert len(harness.prompts) == 2
    assert harness.prompts[0] == harness.prompts[1], "截断重试发原 prompt，不回喂错误"
    assert "上一次的回复不合格" not in harness.prompts[1]
    assert {key.max_tokens for key in pool.live_keys} == {
        STANDARD_OUTPUT_TOKENS,
        LARGE_OUTPUT_TOKENS,
    }
    assert result.usage.billable == 290 * 2


def test_parse_gives_up_after_the_retry() -> None:
    bad = "还是没有 JSON"
    llm, harness, _ = make_llm(
        [
            FakeRunResult(bad, "completed", ok_events(bad)),
            FakeRunResult(bad, "completed", ok_events(bad)),
        ]
    )
    with pytest.raises(LLMAPIError, match="两次都不合格"):
        llm.parse("打分", Score)
    assert len(harness.prompts) == 2


def test_parse_rejects_schema_violating_json() -> None:
    wrong = '{"score": "不是数字", "reason": "x"}'
    llm, _, _ = make_llm(
        [
            FakeRunResult(wrong, "completed", ok_events(wrong)),
            FakeRunResult(wrong, "completed", ok_events(wrong)),
        ]
    )
    with pytest.raises(LLMAPIError):
        llm.parse("打分", Score)


def test_schema_instruction_is_self_describing() -> None:
    text = schema_instruction(Score)
    assert "只输出一个 JSON 对象" in text
    assert '"score"' in text


def test_compose_prompt_without_system_is_verbatim() -> None:
    assert compose_prompt("正文", None) == "正文"
    assert compose_prompt("正文", "   ") == "正文"


# ------------------------------------------------------------------ 工厂开关


def test_build_llm_switches_backend(monkeypatch) -> None:
    from core.config import reload_settings
    from generation.llm import LLMClient, build_llm

    monkeypatch.setenv("SW_LLM_BACKEND", "anthropic")
    reload_settings()
    assert isinstance(build_llm(), LLMClient)

    monkeypatch.setenv("SW_LLM_BACKEND", "dsh")
    reload_settings()
    built = build_llm()
    assert isinstance(built, DshLLM)
    # 只是构造，不该碰子进程
    assert built._pool is None


def test_build_llm_honours_injected_anthropic_client(monkeypatch) -> None:
    """注入的东西比环境变量更具体，不该被开关翻盘。"""
    import anthropic

    from core.config import reload_settings
    from generation.llm import LLMClient, build_llm

    monkeypatch.setenv("SW_LLM_BACKEND", "dsh")
    reload_settings()
    client = anthropic.Anthropic(api_key="sk-ant-test")
    assert isinstance(build_llm(client=client), LLMClient)


def test_credentials_ready_follows_backend(monkeypatch) -> None:
    from core.config import reload_settings
    from generation.llm import llm_credentials_ready

    monkeypatch.setenv("SW_LLM_BACKEND", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert llm_credentials_ready(reload_settings()) is True

    monkeypatch.setenv("SW_LLM_BACKEND", "dsh")
    assert llm_credentials_ready(reload_settings()) is False
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-x")
    assert llm_credentials_ready(reload_settings()) is True


# ------------------------------------------------------------------ 前缀缓存


def test_cache_key_is_stable_across_calls_that_share_a_prefix() -> None:
    """codex 契约①：同一组调用必须拿到**同一个**键，prompt 不同也不许变。"""
    system = "你是小红书文案助手。人设：城市漫游。"
    first = prompt_cache_key(model="gpt-5.6-luna", system=system)
    second = prompt_cache_key(model="gpt-5.6-luna", system=system)
    assert first == second
    # system 前后的空白被 compose_prompt strip 掉，落在线上的字节一样，键也必须一样
    assert prompt_cache_key(model="gpt-5.6-luna", system=f"  {system}\n") == first


def test_cache_key_splits_when_the_prefix_or_model_differs() -> None:
    """太粗会让不同前缀共用一个键：model 与 system 任一变化都必须换键。"""
    base = prompt_cache_key(model="gpt-5.6-luna", system="A 人设")
    assert prompt_cache_key(model="gpt-5.6-sol", system="A 人设") != base
    assert prompt_cache_key(model="gpt-5.6-luna", system="B 人设") != base
    assert prompt_cache_key(model="gpt-5.6-luna", system=None) != base


@pytest.mark.parametrize(
    "model",
    ["m", "gpt-5.6-luna", "deepseek-v4-flash", "a" * 200, "Model/With:Weird_Chars.v2"],
)
def test_cache_key_length_matches_the_upstream_clamp(model: str) -> None:
    """键必须**恰好** 64 个字符：短了会被 session 后缀污染，长了会被截掉尾部摘要。"""
    key = prompt_cache_key(model=model, system="任意 system")
    assert len(key) == PROMPT_CACHE_KEY_LENGTH


def test_hashed_prefix_is_a_literal_prefix_of_what_gets_sent() -> None:
    """codex 契约②：被哈希的那段就是真正发出去的字节前缀，逐字节相同。"""
    system = "你是公众号写手。"
    prefix = stable_prefix(system)
    assert prefix  # 有 system 就一定有共享前缀
    for prompt in ("写一段开头", "换一个完全不同的任务", ""):
        assert compose_prompt(prompt, system).startswith(prefix)
    # 没有 system 时如实表达"没有共享前缀"，不许编一个出来
    assert stable_prefix(None) == ""
    assert stable_prefix("   ") == ""


def test_session_id_carries_the_key_and_still_opens_a_new_session() -> None:
    key = prompt_cache_key(model="gpt-5.6-luna", system="人设")
    ids = {session_id_for(key) for _ in range(8)}
    assert len(ids) == 8  # 会话不复用：绝不能把上一轮历史串进来
    for session_id in ids:
        assert session_id[:PROMPT_CACHE_KEY_LENGTH] == key
        assert len(session_id) > PROMPT_CACHE_KEY_LENGTH


def test_session_id_refuses_a_key_that_would_not_survive_the_clamp() -> None:
    with pytest.raises(ValueError):
        session_id_for("太短了")
    with pytest.raises(ValueError):
        session_id_for("x" * (PROMPT_CACHE_KEY_LENGTH + 1))


def test_pool_opens_a_fresh_session_per_call_under_one_cache_key() -> None:
    llm, harness, pool = make_llm(
        [
            FakeRunResult("一", "completed", ok_events("一")),
            FakeRunResult("二", "completed", ok_events("二")),
        ]
    )
    llm.complete("第一次", system="同一个人设")
    llm.complete("第二次的任务完全不同", system="同一个人设")

    first, second = harness.session_ids
    assert first != second
    assert first[:PROMPT_CACHE_KEY_LENGTH] == second[:PROMPT_CACHE_KEY_LENGTH]
    # 缓存键不进 RuntimeKey：同一档参数仍然只有一个子进程
    assert len(pool.live_keys) == 1


def test_routed_calls_share_a_key_within_a_model_and_split_across_models() -> None:
    """同一条流水线里多个 purpose 共享 system 段，就该共享键；跨模型必须分开。"""
    llm, harness, _ = make_llm(
        [FakeRunResult("ok", "completed", ok_events("ok")) for _ in range(3)]
    )
    enable_test_routing(llm)
    system = "你是小红书文案助手。"
    llm.complete("切角度", system=system, purpose="xhs.angle")  # luna
    llm.complete("写正文", system=system, purpose="xhs.note")  # luna
    llm.complete("选题", system=system, purpose="sourcing.select")  # sol

    keys = [sid[:PROMPT_CACHE_KEY_LENGTH] for sid in harness.session_ids]
    assert keys[0] == keys[1]
    assert keys[2] != keys[0]


def test_ledger_records_the_cache_key(session) -> None:
    guard = BudgetGuard(session, token_budget=10_000)
    llm, harness, _ = make_llm([FakeRunResult("ok", "completed", ok_events("ok"))], budget=guard)
    llm.complete("写点什么", system="人设")
    row = session.query(CostLedger).filter_by(kind=CostKind.TOKENS.value).one()
    assert row.meta["cache_key"] == harness.session_ids[0][:PROMPT_CACHE_KEY_LENGTH]


def test_shipped_cordis_keeps_prefix_caching_switched_on() -> None:
    """开关漂了，键就一个都发不出去——静态钉住仓库自带组合里在用的两条路由。"""
    entries = load_composition(CORDIS_PATH)
    for provider in ("deepseek", "deepseek-official"):
        assert provider_cache_retention(entries, provider) == "long", provider
