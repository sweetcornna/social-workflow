"""``SupportsLLM`` 双后端契约：anthropic 与 dsh 对上层必须同形。

这组用例刻意**不**碰任何后端专属细节（请求体形状、事件字段），只压一件事：
四个消费方（选题 / 文案 / 审核 / 复盘）看得见的那层行为，两个后端一模一样。
换后端时如果这里绿，消费方就不需要改一行。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from core.budget import BudgetExhausted, BudgetGuard, CostKind
from generation.llm import LLMClient, LLMResult, ParsedResult, ScriptedLLM, SupportsLLM
from generation.llm_dsh import DshLLM, DshRuntimeOptions, DshRuntimePool
from tests.generation.test_llm_dsh import FakeHarness, FakeRunResult, ok_events
from tests.test_generation_llm import Recorder, make_client, message_payload, sse_stream


class Extracted(BaseModel):
    name: str
    score: int


PAYLOAD = {"name": "台风", "score": 7}
#: 两个后端都必须能从这段回复里拿到同一个结构体（dsh 侧要穿过"最后一个 JSON"提取）
TEXT_REPLY = "正文内容"
JSON_REPLY = json.dumps(PAYLOAD, ensure_ascii=False)

#: 两个后端各自的"一次调用记多少 token"，用于断言记账真的落了账
BILLABLE = {"anthropic": 120 + 30, "dsh": 234 + 56}

BackendFactory = Callable[[BudgetGuard | None], SupportsLLM]


def build_anthropic(budget: BudgetGuard | None) -> SupportsLLM:
    def respond(request: httpx.Request) -> httpx.Response:
        raw = request.content
        payload = message_payload(JSON_REPLY if b"json_schema" in raw else TEXT_REPLY)
        # complete_long 走流式，得回 SSE 而不是整包 JSON
        if json.loads(raw or b"{}").get("stream"):
            return httpx.Response(
                200, content=sse_stream(payload), headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(200, json=payload)

    return LLMClient(client=make_client(Recorder(respond)), budget=budget)


def build_dsh(budget: BudgetGuard | None) -> SupportsLLM:
    class ScriptedHarness(FakeHarness):
        def run(self, input: str, *, session_id: str) -> FakeRunResult:
            self.prompts.append(input)
            self.session_ids.append(session_id)
            reply = JSON_REPLY if "JSON Schema" in input else TEXT_REPLY
            return FakeRunResult(reply, "completed", ok_events(reply))

    options = DshRuntimeOptions(
        provider="deepseek-official",
        model="deepseek-v4-flash",
        cordis_path=Path("configs/dsh/cordis.yml"),
        session_root=Path("data/dsh_sessions"),
        cwd=Path("."),
        request_timeout_seconds=60.0,
        stream_idle_timeout_ms=60_000,
    )
    harness = ScriptedHarness()
    pool = DshRuntimePool(options, factory=lambda key, opts: harness)
    return DshLLM(pool=pool, budget=budget)


BACKENDS: dict[str, BackendFactory] = {"anthropic": build_anthropic, "dsh": build_dsh}


@pytest.fixture(params=sorted(BACKENDS))
def backend(request: Any) -> str:
    return request.param


def test_backend_implements_the_whole_protocol(backend: str) -> None:
    llm = BACKENDS[backend](None)
    for method in ("complete", "complete_long", "parse"):
        assert callable(getattr(llm, method)), f"{backend} 缺少 {method}"


@pytest.mark.parametrize("method", ["complete", "complete_long"])
def test_text_calls_return_llm_result(backend: str, method: str) -> None:
    llm = BACKENDS[backend](None)
    result = getattr(llm, method)("写点东西", system="你是编辑", purpose="contract.text")
    assert isinstance(result, LLMResult)
    assert result.text == TEXT_REPLY
    assert result.model
    # 两个后端的正常收尾都归一到同一个词
    assert result.stop_reason == "end_turn"
    assert result.usage.billable == BILLABLE[backend]


def test_parse_returns_the_pydantic_model(backend: str) -> None:
    llm = BACKENDS[backend](None)
    result = llm.parse("抽取", Extracted, purpose="contract.parse")
    assert isinstance(result, ParsedResult)
    assert isinstance(result.parsed, Extracted)
    assert result.parsed.name == "台风"
    assert result.parsed.score == 7


def test_usage_lands_in_the_cost_ledger(backend: str, session) -> None:
    guard = BudgetGuard(session, token_budget=10_000)
    llm = BACKENDS[backend](guard)
    llm.complete("hi", purpose="contract.budget")
    assert guard.used(CostKind.TOKENS) == BILLABLE[backend]


def test_budget_exhaustion_raises_the_same_exception(backend: str, session) -> None:
    guard = BudgetGuard(session, token_budget=10)
    llm = BACKENDS[backend](guard)
    with pytest.raises(BudgetExhausted):
        llm.complete("hi", purpose="contract.budget")
    # 闸门关上前先把剩余额度记满，账本不会显示"还有余额"
    assert guard.remaining(CostKind.TOKENS) == 0


def test_scripted_stand_in_still_satisfies_the_same_shape(session) -> None:
    """ScriptedLLM 是第三种实现：无凭据机器上跑 e2e 靠它，形状不能跑偏。"""
    guard = BudgetGuard(session, token_budget=10_000)
    llm = ScriptedLLM(
        replies=[TEXT_REPLY],
        parsed_replies=[Extracted(**PAYLOAD)],
        budget=guard,
    )
    assert llm.complete("hi").text == TEXT_REPLY
    assert llm.parse("hi", Extracted).parsed.score == 7
    assert guard.used(CostKind.TOKENS) > 0
