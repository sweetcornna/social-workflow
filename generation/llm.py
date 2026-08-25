"""Anthropic SDK 薄封装：统一模型、预算记账、拒答处理与异常分类。

调用约定（与官方 SDK 0.122 对齐，勿凭记忆改动）：

- 默认模型 ``claude-opus-5``，可用环境变量 ``LLM_MODEL`` 覆盖。
- **不传** ``temperature/top_p/top_k``（Opus 5 会 400），**不传** ``budget_tokens``，
  **不做** assistant prefill。``thinking`` 省略即 adaptive。
- 深度用 ``output_config={"effort": ...}`` 控制，默认 ``medium``。
- 默认调用路径是 ``client.beta.messages.create(betas=["server-side-fallback-2026-07-01"],
  fallbacks="default")``：拒答时服务端按类别自动回落到备用模型，少写一层客户端重试。
- 长输出（文章正文）走 ``client.beta.messages.stream()`` + ``get_final_message()``，
  避免大 ``max_tokens`` 撞 HTTP 超时。
- 结构化输出走 ``client.messages.parse(output_format=PydanticModel)`` → ``parsed_output``。
  该路径在非 beta 命名空间上，因此**没有** server-side fallback。

每次调用把 ``usage`` 计入 :class:`core.budget.BudgetGuard`（``kind="tokens"``），
预算耗尽抛 :class:`core.budget.BudgetExhausted`，上层降级为"只出选题不出稿"。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypeVar

import anthropic
from pydantic import BaseModel

from core.budget import BudgetExhausted, BudgetGuard, CostKind

logger = logging.getLogger("social_workflow.generation.llm")

Effort = Literal["low", "medium", "high", "xhigh", "max"]

#: 服务端拒答回落。用 ``"default"`` 而非写死备用模型，避免备用模型下线时还要改代码。
FALLBACK_BETA = "server-side-fallback-2026-07-01"

ModelT = TypeVar("ModelT", bound=BaseModel)


# --------------------------------------------------------------------- 异常


class LLMError(Exception):
    """生成层异常基类。"""


class LLMUnavailable(LLMError):
    """缺少 API Key 等前置条件，无法调用。"""


class GenerationRefused(LLMError):
    """模型拒答（``stop_reason == "refusal"``）。

    这是 HTTP 200 的正常返回，不是 API 错误；``content`` 可能为空或只有部分内容，
    调用方**不得**直接读 ``content[0]``。
    """

    def __init__(self, category: str | None, explanation: str | None, *, model: str) -> None:
        super().__init__(f"模型拒答（model={model}, category={category}）: {explanation or ''}")
        self.category = category
        self.explanation = explanation
        self.model = model


class LLMRateLimited(LLMError):
    """429，调用方可退避重试。"""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class LLMAPIError(LLMError):
    """其它非 2xx 响应。"""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class LLMConnectionError(LLMError):
    """网络层失败，未拿到响应。"""


# --------------------------------------------------------------------- DTO


@dataclass(frozen=True)
class Usage:
    """一次调用的 token 用量。``cache_read`` 只用于观测，不重复计费。"""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    @property
    def billable(self) -> int:
        """计入日预算的 token 数：新增输入 + 输出（缓存命中部分另计）。"""
        return self.input_tokens + self.output_tokens

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_input_tokens
            + self.cache_creation_input_tokens
        )

    def as_meta(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
        }


@dataclass
class LLMResult:
    """一次文本调用的结果。"""

    text: str
    usage: Usage
    model: str
    stop_reason: str | None = None

    def __str__(self) -> str:  # pragma: no cover - 便于日志
        return self.text


@dataclass
class ParsedResult[ModelT: BaseModel]:
    """一次结构化调用的结果。"""

    parsed: ModelT
    usage: Usage
    model: str
    stop_reason: str | None = None


# ------------------------------------------------------------------ 协议


class SupportsLLM(Protocol):
    """生成链只依赖这个协议，便于测试注入 :class:`ScriptedLLM`。"""

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = ...,
        max_tokens: int | None = ...,
        effort: Effort | None = ...,
        purpose: str = ...,
    ) -> LLMResult: ...

    def complete_long(
        self,
        prompt: str,
        *,
        system: str | None = ...,
        max_tokens: int | None = ...,
        effort: Effort | None = ...,
        purpose: str = ...,
    ) -> LLMResult: ...

    def parse(
        self,
        prompt: str,
        output_format: type[ModelT],
        *,
        system: str | None = ...,
        max_tokens: int | None = ...,
        effort: Effort | None = ...,
        purpose: str = ...,
    ) -> ParsedResult[ModelT]: ...


# ------------------------------------------------------------------ 预算


def _usage_from(raw: Any) -> Usage:
    """把 SDK 的 usage 对象转成本地 DTO；字段缺失或为 None 都按 0 处理。"""

    def pick(name: str) -> int:
        value = getattr(raw, name, None)
        return int(value) if isinstance(value, int) else 0

    if raw is None:
        return Usage()
    return Usage(
        input_tokens=pick("input_tokens"),
        output_tokens=pick("output_tokens"),
        cache_read_input_tokens=pick("cache_read_input_tokens"),
        cache_creation_input_tokens=pick("cache_creation_input_tokens"),
    )


def charge_usage(
    guard: BudgetGuard | None,
    usage: Usage,
    *,
    purpose: str,
    model: str,
    extra_meta: dict[str, Any] | None = None,
) -> None:
    """把用量写进成本流水。超出剩余额度时**先记满剩余**再抛，保证账本不丢数。

    ``BudgetGuard.charge`` 在超额时不写流水（这是它的正确行为：拒绝这笔支出）。
    但 token 已经真实花掉了，账本必须反映"已耗尽"，否则下次调用还会放行。

    ``extra_meta`` 给后端补充观测字段：两个后端都记 ``stop_reason`` 与本次的
    ``max_tokens``（"预算是不是贴边了"必须事后可查），dsh 后端另外标
    ``backend`` 与 ``estimated``。
    """
    if guard is None:
        return
    amount = float(usage.billable)
    meta = {"purpose": purpose, "model": model, **usage.as_meta(), **(extra_meta or {})}
    remaining = guard.remaining(CostKind.TOKENS)
    if amount <= remaining:
        guard.charge(CostKind.TOKENS, amount, meta=meta)
        return
    if remaining > 0:
        guard.charge(CostKind.TOKENS, remaining, meta={**meta, "clamped_from": amount})
    raise BudgetExhausted(CostKind.TOKENS.value, amount, remaining, guard.limit_of(CostKind.TOKENS))


# ------------------------------------------------------------------ 客户端


class LLMClient:
    """Anthropic Messages API 薄封装。

    ``client`` 可注入（测试时传一个挂了 ``httpx.MockTransport`` 的
    ``anthropic.Anthropic``），生产环境留空由本类按环境变量构造。
    """

    def __init__(
        self,
        *,
        client: anthropic.Anthropic | None = None,
        model: str | None = None,
        effort: Effort | None = None,
        budget: BudgetGuard | None = None,
        max_tokens: int | None = None,
        article_max_tokens: int | None = None,
        api_key: str | None = None,
        use_fallbacks: bool = True,
    ) -> None:
        from core.config import get_settings

        settings = get_settings()
        self.model = model or os.environ.get("LLM_MODEL") or settings.llm_model
        self.effort: Effort = effort or settings.llm_effort
        self.budget = budget
        self.max_tokens = max_tokens or settings.llm_max_tokens
        self.article_max_tokens = article_max_tokens or settings.llm_article_max_tokens
        self.use_fallbacks = use_fallbacks
        self._api_key = api_key if api_key is not None else settings.anthropic_api_key
        self._timeout = settings.llm_timeout_seconds
        self._client = client

    # -- 底层 --------------------------------------------------------------

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            if not self._api_key:
                raise LLMUnavailable(
                    "ANTHROPIC_API_KEY 未配置：生成链不可用。"
                    "本地联调请改用 generation.llm.ScriptedLLM。"
                )
            self._client = anthropic.Anthropic(api_key=self._api_key, timeout=self._timeout)
        return self._client

    def _beta_kwargs(self) -> dict[str, Any]:
        """server-side fallback 参数。关掉时返回空 dict。"""
        if not self.use_fallbacks:
            return {}
        return {"betas": [FALLBACK_BETA], "fallbacks": "default"}

    def _messages(self, prompt: str) -> list[dict[str, Any]]:
        return [{"role": "user", "content": prompt}]

    def _system(self, system: str | None) -> Any:
        # 系统提示词整段做缓存断点：同一账号的 persona 在一次生成链里会被复用多次
        if not system:
            return anthropic.NOT_GIVEN
        return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]

    @staticmethod
    def _check_refusal(response: Any, model: str) -> None:
        """必须在读 ``content`` 之前调用。"""
        if getattr(response, "stop_reason", None) != "refusal":
            return
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None)
        explanation = getattr(details, "explanation", None)
        logger.warning("模型拒答: model=%s category=%s", model, category)
        raise GenerationRefused(category, explanation, model=model)

    @staticmethod
    def _text_of(response: Any) -> str:
        parts = [
            block.text
            for block in getattr(response, "content", []) or []
            if getattr(block, "type", None) == "text"
        ]
        return "\n".join(parts).strip()

    def _settle(self, response: Any, *, purpose: str, max_tokens: int) -> Usage:
        """拒答检查 + 预算记账，返回用量。"""
        model = str(getattr(response, "model", self.model) or self.model)
        usage = _usage_from(getattr(response, "usage", None))
        # 先记账再判拒答：拒答前置分类不计费，但流式中途拒答已产生的 token 要记。
        # ``stop_reason`` / ``max_tokens`` 一起落进 meta：预算长期贴边是要能事后查出来的，
        # 只看 output_tokens 只能猜（两个后端在这件事上口径一致）。
        charge_usage(
            self.budget,
            usage,
            purpose=purpose,
            model=model,
            extra_meta={
                "stop_reason": getattr(response, "stop_reason", None),
                "max_tokens": max_tokens,
            },
        )
        self._check_refusal(response, model)
        return usage

    # -- 对外 API ----------------------------------------------------------

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        effort: Effort | None = None,
        purpose: str = "complete",
    ) -> LLMResult:
        """短输出（大纲、标题、摘要、单段改写）。"""
        budget = max_tokens or self.max_tokens
        with _translate_errors():
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=budget,
                system=self._system(system),
                messages=self._messages(prompt),
                output_config={"effort": effort or self.effort},
                **self._beta_kwargs(),
            )
        usage = self._settle(response, purpose=purpose, max_tokens=budget)
        return LLMResult(
            text=self._text_of(response),
            usage=usage,
            model=str(getattr(response, "model", self.model)),
            stop_reason=getattr(response, "stop_reason", None),
        )

    def complete_long(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        effort: Effort | None = None,
        purpose: str = "complete_long",
    ) -> LLMResult:
        """长输出（文章正文）。走流式，避免大 max_tokens 撞 HTTP 超时。"""
        budget = max_tokens or self.article_max_tokens
        with (
            _translate_errors(),
            self.client.beta.messages.stream(
                model=self.model,
                max_tokens=budget,
                system=self._system(system),
                messages=self._messages(prompt),
                output_config={"effort": effort or self.effort},
                **self._beta_kwargs(),
            ) as stream,
        ):
            response = stream.get_final_message()
        usage = self._settle(response, purpose=purpose, max_tokens=budget)
        return LLMResult(
            text=self._text_of(response),
            usage=usage,
            model=str(getattr(response, "model", self.model)),
            stop_reason=getattr(response, "stop_reason", None),
        )

    def parse(
        self,
        prompt: str,
        output_format: type[ModelT],
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        effort: Effort | None = None,
        purpose: str = "parse",
    ) -> ParsedResult[ModelT]:
        """结构化输出。``output_format`` 是 Pydantic 模型，返回其实例。

        走非 beta 的 ``client.messages.parse``，因此**没有** server-side fallback：
        拒答时直接抛 :class:`GenerationRefused`，由调用方决定降级策略。
        """
        budget = max_tokens or self.max_tokens
        with _translate_errors():
            response = self.client.messages.parse(
                model=self.model,
                max_tokens=budget,
                system=self._system(system),
                messages=self._messages(prompt),
                output_config={"effort": effort or self.effort},
                output_format=output_format,
            )
        usage = self._settle(response, purpose=purpose, max_tokens=budget)
        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            raise LLMAPIError(
                f"结构化输出为空（stop_reason={getattr(response, 'stop_reason', None)}）"
            )
        return ParsedResult(
            parsed=parsed,
            usage=usage,
            model=str(getattr(response, "model", self.model)),
            stop_reason=getattr(response, "stop_reason", None),
        )


class _translate_errors:
    """把 SDK 异常链翻译成本模块的异常。顺序：RateLimit → APIStatus → APIConnection。"""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> bool:
        if exc is None:
            return False
        if isinstance(exc, anthropic.RateLimitError):
            retry_after = None
            response = getattr(exc, "response", None)
            if response is not None:
                raw = response.headers.get("retry-after")
                if raw:
                    try:
                        retry_after = float(raw)
                    except ValueError:
                        retry_after = None
            raise LLMRateLimited(f"Anthropic 限流: {exc}", retry_after=retry_after) from exc
        if isinstance(exc, anthropic.APIStatusError):
            raise LLMAPIError(
                f"Anthropic 返回 {exc.status_code}: {exc}", status_code=exc.status_code
            ) from exc
        if isinstance(exc, anthropic.APIConnectionError):
            raise LLMConnectionError(f"Anthropic 连接失败: {exc}") from exc
        return False


# ------------------------------------------------------------------ 测试替身


@dataclass
class ScriptedLLM:
    """按顺序吐出预置回复的假客户端，用于离线单测与 ``/dev/*`` 联调。

    与 :class:`publishers.base.FakePublisher` 同一思路：不碰网络，但走完全部调用路径
    （含预算记账），这样 e2e dry-run 能验证状态机与账本而不烧真实 token。
    """

    replies: list[str] = field(default_factory=list)
    parsed_replies: list[BaseModel] = field(default_factory=list)
    model: str = "scripted-llm"
    budget: BudgetGuard | None = None
    tokens_per_call: int = 100
    #: 设成异常实例则每次调用都抛（测试拒答 / 预算耗尽分支）
    raise_exc: Exception | None = None

    calls: list[dict[str, Any]] = field(default_factory=list)
    _reply_cursor: int = 0
    _consumed_parsed: set[int] = field(default_factory=set)

    # -- 内部 --------------------------------------------------------------

    def _record(
        self,
        kind: str,
        prompt: str,
        system: str | None,
        purpose: str,
        *,
        max_tokens: int | None = None,
        effort: Effort | None = None,
    ) -> None:
        # ``max_tokens`` / ``effort`` 也记：调用点有没有给足输出预算是可断言的事实
        # （见 generation/output_budget.py），不记就只能靠 review 眼睛看
        self.calls.append(
            {
                "kind": kind,
                "prompt": prompt,
                "system": system,
                "purpose": purpose,
                "max_tokens": max_tokens,
                "effort": effort,
            }
        )
        if self.raise_exc is not None:
            raise self.raise_exc

    def _usage(self, purpose: str) -> Usage:
        usage = Usage(input_tokens=self.tokens_per_call, output_tokens=self.tokens_per_call)
        charge_usage(self.budget, usage, purpose=purpose, model=self.model)
        return usage

    def _next_reply(self) -> str:
        if not self.replies:
            return "（ScriptedLLM 未配置回复）"
        reply = self.replies[min(self._reply_cursor, len(self.replies) - 1)]
        self._reply_cursor += 1
        return reply

    # -- 协议实现 ----------------------------------------------------------

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        effort: Effort | None = None,
        purpose: str = "complete",
    ) -> LLMResult:
        self._record("complete", prompt, system, purpose, max_tokens=max_tokens, effort=effort)
        usage = self._usage(purpose)
        return LLMResult(
            text=self._next_reply(), usage=usage, model=self.model, stop_reason="end_turn"
        )

    def complete_long(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        effort: Effort | None = None,
        purpose: str = "complete_long",
    ) -> LLMResult:
        self._record("complete_long", prompt, system, purpose, max_tokens=max_tokens, effort=effort)
        usage = self._usage(purpose)
        return LLMResult(
            text=self._next_reply(), usage=usage, model=self.model, stop_reason="end_turn"
        )

    def parse(
        self,
        prompt: str,
        output_format: type[ModelT],
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        effort: Effort | None = None,
        purpose: str = "parse",
    ) -> ParsedResult[ModelT]:
        """按**类型**而不是顺序取预置回复。

        按顺序取会让调用序列一变（例如手工指定选题时跳过了选题 Agent）就全盘错位，
        测试挂在无关的地方。按类型取更贴近"这次调用要的是哪种结构"，也允许
        ``parsed_replies`` 随便排。同类型多条时按先后消费。
        """
        self._record("parse", prompt, system, purpose, max_tokens=max_tokens, effort=effort)
        usage = self._usage(purpose)
        for index, value in enumerate(self.parsed_replies):
            if index in self._consumed_parsed or not isinstance(value, output_format):
                continue
            self._consumed_parsed.add(index)
            return ParsedResult(parsed=value, usage=usage, model=self.model, stop_reason="end_turn")
        available = sorted({type(v).__name__ for v in self.parsed_replies}) or ["（空）"]
        raise LLMError(
            f"ScriptedLLM 没有可用的 {output_format.__name__} 预置回复"
            f"（已配置类型：{', '.join(available)}）"
        )


def build_llm(
    *,
    budget: BudgetGuard | None = None,
    client: anthropic.Anthropic | None = None,
    backend: Literal["anthropic", "dsh"] | None = None,
    **kwargs: Any,
) -> SupportsLLM:
    """按 ``SW_LLM_BACKEND`` 选后端构造客户端。默认 ``anthropic``，现有部署零影响。

    显式传了 ``client``（注入的 Anthropic 客户端）就一定走 Anthropic 后端——
    注入的东西比开关更具体，不该被环境变量翻盘。
    """
    from core.config import get_settings

    chosen = backend or get_settings().sw_llm_backend
    if chosen == "dsh" and client is None:
        from generation.llm_dsh import DshLLM

        return DshLLM(budget=budget, **kwargs)
    return LLMClient(budget=budget, client=client, **kwargs)


def llm_credentials_ready(settings: Any = None) -> bool:
    """当前后端的凭据是否齐备。两个后端都缺时上层照旧回落 :class:`ScriptedLLM`。"""
    from core.config import get_settings

    settings = settings or get_settings()
    if settings.sw_llm_backend != "dsh":
        return bool(settings.anthropic_api_key)
    from generation.llm_dsh import dsh_credentials_ready

    return dsh_credentials_ready(settings)


def join_prompt(*parts: str | Sequence[str] | None) -> str:
    """拼接 prompt 片段，丢掉空段落。"""
    chunks: list[str] = []
    for part in parts:
        if part is None:
            continue
        if isinstance(part, str):
            if part.strip():
                chunks.append(part.strip())
        else:
            for sub in part:
                if sub and sub.strip():
                    chunks.append(sub.strip())
    return "\n\n".join(chunks)


__all__ = [
    "FALLBACK_BETA",
    "Effort",
    "GenerationRefused",
    "LLMAPIError",
    "LLMClient",
    "LLMConnectionError",
    "LLMError",
    "LLMRateLimited",
    "LLMResult",
    "LLMUnavailable",
    "ParsedResult",
    "ScriptedLLM",
    "SupportsLLM",
    "Usage",
    "build_llm",
    "charge_usage",
    "join_prompt",
    "llm_credentials_ready",
]
