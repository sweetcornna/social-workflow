"""generation/llm.py：走**真实 SDK**，但 HTTP 层用 httpx.MockTransport 拦掉。

这样能验证我们实际发出去的请求体（模型串、effort、fallbacks、没有 temperature），
而不是只验证一层自己写的 mock。
"""

from __future__ import annotations

import json
from typing import Any

import anthropic
import httpx
import pytest
from pydantic import BaseModel

from core.budget import BudgetExhausted, BudgetGuard, CostKind
from generation.llm import (
    FALLBACK_BETA,
    GenerationRefused,
    LLMAPIError,
    LLMClient,
    LLMConnectionError,
    LLMError,
    LLMRateLimited,
    LLMUnavailable,
    ScriptedLLM,
    Usage,
    charge_usage,
    join_prompt,
)


def message_payload(
    text: str = "hello",
    *,
    stop_reason: str = "end_turn",
    input_tokens: int = 120,
    output_tokens: int = 30,
    cache_read: int = 0,
    model: str = "claude-opus-5",
) -> dict[str, Any]:
    """一个最小但结构正确的 Messages API 响应。"""
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": 0,
        },
    }


def sse_stream(payload: dict[str, Any]) -> bytes:
    """把一个完整 message 拆成最小可用的 SSE 事件序列。"""
    text = payload["content"][0]["text"]
    start = {
        **payload,
        "content": [],
        "usage": {"input_tokens": payload["usage"]["input_tokens"], "output_tokens": 0},
    }
    events = [
        ("message_start", {"type": "message_start", "message": start}),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": payload["stop_reason"], "stop_sequence": None},
                "usage": {"output_tokens": payload["usage"]["output_tokens"]},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    body = "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events)
    return body.encode("utf-8")


class Recorder:
    """记录发出去的请求，并按路径返回预置响应。"""

    def __init__(self, response: Any) -> None:
        self.response = response
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        raw = request.content
        self.bodies.append(json.loads(raw) if raw else {})
        if callable(self.response):
            return self.response(request)
        return self.response

    @property
    def last_body(self) -> dict[str, Any]:
        return self.bodies[-1]

    @property
    def last_headers(self) -> httpx.Headers:
        return self.requests[-1].headers


def make_client(recorder: Recorder) -> anthropic.Anthropic:
    return anthropic.Anthropic(
        api_key="sk-ant-test",
        http_client=httpx.Client(transport=httpx.MockTransport(recorder)),
        max_retries=0,
    )


# ------------------------------------------------------------------ complete


def test_complete_sends_expected_request_shape() -> None:
    recorder = Recorder(httpx.Response(200, json=message_payload("大纲内容")))
    llm = LLMClient(client=make_client(recorder), model="claude-opus-5", effort="medium")

    result = llm.complete("写个大纲", system="你是编辑", purpose="test.outline")

    assert result.text == "大纲内容"
    body = recorder.last_body
    assert body["model"] == "claude-opus-5"
    assert body["output_config"] == {"effort": "medium"}
    # Opus 5 上这些参数会 400，必须不出现
    for forbidden in ("temperature", "top_p", "top_k", "budget_tokens"):
        assert forbidden not in body, f"{forbidden} 不该出现在请求里"
    # thinking 省略即 adaptive，不显式传
    assert "thinking" not in body
    # server-side fallback 默认开
    assert body["fallbacks"] == "default"
    assert FALLBACK_BETA in recorder.last_headers.get("anthropic-beta", "")
    # 最后一条消息必须是 user——不做 assistant prefill
    assert body["messages"][-1]["role"] == "user"
    # 系统提示词带缓存断点
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_complete_can_disable_fallbacks() -> None:
    recorder = Recorder(httpx.Response(200, json=message_payload()))
    llm = LLMClient(client=make_client(recorder), use_fallbacks=False)
    llm.complete("hi")
    assert "fallbacks" not in recorder.last_body


def test_effort_override_per_call() -> None:
    recorder = Recorder(httpx.Response(200, json=message_payload()))
    llm = LLMClient(client=make_client(recorder), effort="medium")
    llm.complete("hi", effort="high")
    assert recorder.last_body["output_config"] == {"effort": "high"}


# ------------------------------------------------------------------ 拒答


def test_refusal_raises_before_reading_content() -> None:
    """拒答是 HTTP 200，content 可能为空——必须先看 stop_reason。"""
    payload = message_payload(stop_reason="refusal")
    payload["content"] = []
    payload["stop_details"] = {"type": "refusal", "category": "cyber", "explanation": "declined"}
    recorder = Recorder(httpx.Response(200, json=payload))
    llm = LLMClient(client=make_client(recorder))

    with pytest.raises(GenerationRefused) as excinfo:
        llm.complete("...")
    assert excinfo.value.category == "cyber"


def test_refusal_still_charges_streamed_tokens(session) -> None:
    """流式中途拒答已经产生的 token 要计费，不能白漏。"""
    payload = message_payload(stop_reason="refusal", input_tokens=50, output_tokens=17)
    recorder = Recorder(httpx.Response(200, json=payload))
    guard = BudgetGuard(session)
    llm = LLMClient(client=make_client(recorder), budget=guard)

    with pytest.raises(GenerationRefused):
        llm.complete("...")
    assert guard.used(CostKind.TOKENS) == 67


# ------------------------------------------------------------------ 异常链


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (429, LLMRateLimited),
        (400, LLMAPIError),
        (500, LLMAPIError),
    ],
)
def test_http_errors_translate_to_typed_exceptions(status: int, expected: type) -> None:
    recorder = Recorder(httpx.Response(status, json={"error": {"message": "boom"}}))
    llm = LLMClient(client=make_client(recorder))
    with pytest.raises(expected):
        llm.complete("hi")


def test_rate_limit_carries_retry_after() -> None:
    recorder = Recorder(
        httpx.Response(429, json={"error": {"message": "slow down"}}, headers={"retry-after": "12"})
    )
    llm = LLMClient(client=make_client(recorder))
    with pytest.raises(LLMRateLimited) as excinfo:
        llm.complete("hi")
    assert excinfo.value.retry_after == 12.0


def test_connection_error_translates() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    llm = LLMClient(client=make_client(Recorder(boom)))
    with pytest.raises(LLMConnectionError):
        llm.complete("hi")


def test_missing_api_key_raises_unavailable(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    from core.config import reload_settings

    reload_settings()
    llm = LLMClient()
    with pytest.raises(LLMUnavailable, match="ANTHROPIC_API_KEY"):
        llm.complete("hi")


# ------------------------------------------------------------------ 流式长输出


def test_complete_long_uses_streaming() -> None:
    payload = message_payload("很长的正文", output_tokens=9000)
    recorder = Recorder(
        httpx.Response(
            200,
            content=sse_stream(payload),
            headers={"content-type": "text/event-stream"},
        )
    )
    llm = LLMClient(client=make_client(recorder))

    result = llm.complete_long("写正文", max_tokens=16000)

    assert result.text == "很长的正文"
    body = recorder.last_body
    assert body["stream"] is True, "长输出必须走流式，否则大 max_tokens 会撞 HTTP 超时"
    assert body["max_tokens"] == 16000
    assert result.usage.output_tokens == 9000


# ------------------------------------------------------------------ 结构化输出


class Extracted(BaseModel):
    name: str
    score: int


def test_parse_returns_pydantic_instance() -> None:
    payload = message_payload(json.dumps({"name": "台风", "score": 7}))
    recorder = Recorder(httpx.Response(200, json=payload))
    llm = LLMClient(client=make_client(recorder))

    result = llm.parse("抽取", Extracted)

    assert isinstance(result.parsed, Extracted)
    assert result.parsed.name == "台风"
    assert result.parsed.score == 7
    # 结构化输出走 output_config.format，而不是已废弃的 output_format 顶层参数
    assert "json_schema" in json.dumps(recorder.last_body["output_config"])


# ------------------------------------------------------------------ 预算


def test_usage_is_charged_to_budget(session) -> None:
    recorder = Recorder(
        httpx.Response(200, json=message_payload(input_tokens=200, output_tokens=50))
    )
    guard = BudgetGuard(session)
    llm = LLMClient(client=make_client(recorder), budget=guard)

    llm.complete("hi", purpose="test.charge", max_tokens=8192)

    assert guard.used(CostKind.TOKENS) == 250
    from core.models import CostLedger

    entry = session.query(CostLedger).one()
    assert entry.meta["purpose"] == "test.charge"
    assert entry.meta["input_tokens"] == 200
    # 两个后端口径一致：预算是不是贴边了必须事后可查（P11.2）
    assert entry.meta["stop_reason"] == "end_turn"
    assert entry.meta["max_tokens"] == 8192


def test_cache_read_tokens_recorded_but_not_billed(session) -> None:
    payload = message_payload(input_tokens=10, output_tokens=5, cache_read=9000)
    recorder = Recorder(httpx.Response(200, json=payload))
    guard = BudgetGuard(session)
    llm = LLMClient(client=make_client(recorder), budget=guard)

    result = llm.complete("hi")

    assert result.usage.cache_read_input_tokens == 9000
    assert guard.used(CostKind.TOKENS) == 15  # 缓存命中不重复计费
    from core.models import CostLedger

    assert session.query(CostLedger).one().meta["cache_read_input_tokens"] == 9000


def test_budget_exhausted_still_records_remaining(session) -> None:
    """token 已经花掉了，账本必须反映耗尽，否则下一次调用还会放行。"""
    guard = BudgetGuard(session, token_budget=100)
    with pytest.raises(BudgetExhausted):
        charge_usage(
            guard,
            Usage(input_tokens=300, output_tokens=0),
            purpose="test",
            model="claude-opus-5",
        )
    assert guard.remaining(CostKind.TOKENS) == 0
    assert guard.is_exhausted(CostKind.TOKENS)


def test_charge_usage_without_guard_is_noop() -> None:
    charge_usage(None, Usage(input_tokens=10), purpose="x", model="m")


# ------------------------------------------------------------------ ScriptedLLM


def test_scripted_llm_matches_by_type_not_order() -> None:
    """调用顺序变化（例如跳过选题 Agent）不该让预置回复错位。"""

    class A(BaseModel):
        a: int

    class B(BaseModel):
        b: int

    llm = ScriptedLLM(parsed_replies=[A(a=1), B(b=2)])
    assert llm.parse("p", B).parsed.b == 2  # 先要 B
    assert llm.parse("p", A).parsed.a == 1  # 再要 A


def test_scripted_llm_reports_missing_type() -> None:
    class A(BaseModel):
        a: int

    class C(BaseModel):
        c: int

    llm = ScriptedLLM(parsed_replies=[A(a=1)])
    llm.parse("p", A)
    with pytest.raises(LLMError, match="没有可用的"):
        llm.parse("p", C)


def test_scripted_llm_charges_budget(session) -> None:
    guard = BudgetGuard(session)
    llm = ScriptedLLM(replies=["ok"], budget=guard, tokens_per_call=25)
    llm.complete("hi")
    llm.complete_long("hi")
    assert guard.used(CostKind.TOKENS) == 100  # (25+25) * 2


def test_scripted_llm_records_calls() -> None:
    llm = ScriptedLLM(replies=["one", "two"])
    llm.complete("first", system="sys", purpose="p1")
    llm.complete_long("second", purpose="p2")
    assert [c["purpose"] for c in llm.calls] == ["p1", "p2"]
    assert llm.calls[0]["system"] == "sys"


def test_join_prompt_drops_blanks() -> None:
    assert join_prompt("a", None, "  ", ["b", ""], "c") == "a\n\nb\n\nc"


# ------------------------------------------------------------------ 真实 smoke


@pytest.mark.live
def test_live_smoke_real_api() -> None:
    """可选：有 ANTHROPIC_API_KEY 时打一次真实 API。``pytest -m live`` 才跑。"""
    import os

    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("未配置 ANTHROPIC_API_KEY")
    llm = LLMClient(effort="low", max_tokens=64)
    result = llm.complete("只回复两个字：收到", purpose="live.smoke")
    assert result.text
    assert result.usage.output_tokens > 0
