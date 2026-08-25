"""deepseek-harness（dsh）后端：把本地 Agent runtime 接到 ``SupportsLLM`` 接缝上。

设计取向（与 :mod:`generation.llm` 的 Anthropic 后端对齐，差异都在这里写清楚）：

- **进程模型**：``deepseek_harness.DeepSeekHarness`` 拉起一个 Node 单文件 runtime 子进程，
  经 stdio JSON-RPC 驱动。子进程懒启动、跨调用复用，由 :class:`DshRuntimePool` 持有；
  ``close()`` 幂等释放，FastAPI 生命周期结束与 ``atexit`` 各兜一次。
- **一次调用 = 一个新会话**：``harness.run()`` 每次开一个新 session id，所以语义等价于
  Anthropic 的一次无状态 Messages 调用，不会串上一轮的历史。
- **前缀缓存**：上游网关**不做**隐式前缀缓存，请求体里带了 ``prompt_cache_key`` 才会
  缓存/命中。这条链上该字段的取值被 runtime 写死成"session id 的前 64 个字符"
  （pi-ai ``clampOpenAIPromptCacheKey``），所以本模块把两件事叠进同一个串：前 64 字节
  是**整组调用共享**的缓存键（:func:`prompt_cache_key`），后面挂一段唯一后缀保证会话
  仍然一次一新。开关在 ``configs/dsh/cordis.yml`` 的 ``cacheRetention: long``。
- **进程级参数只能开新进程**：``provider`` / ``model`` / ``maxTokens`` / 思考档
  （``reasoning``）都在 ``initialize`` 握手或 cordis 配置里定死，SDK 线协议没有
  "每次调用改一档"的通道。所以按 ``(model, effort, max_tokens)`` 分桶起进程，池子按
  LRU 封顶。关闭模型路由时，桶数仍等于输出预算档数；开启后不同职责使用不同模型，
  超过池上限的组合照常按 LRU 回收。全部调用点的预算已收敛到
  :data:`generation.output_budget.OUTPUT_TIERS` 的**三档**——标准档 8192
  （= ``llm_max_tokens`` 兜底值）、大输出档 16000（= ``llm_article_max_tokens``；
  选题 / 卡片脚本 / 长文正文都用它）、天花板档 32000（**只**由截断自愈加码到达）。
  自愈加码刻意"抬到下一档"而不是简单 ×2，就是为了不造出梯子之外的取值。
  路由、格式重试和截断自愈都复用同一份调用级模型决策，不会在重试中途换模型。
- **截断自愈**：``max-tokens`` 收尾且一个字都没吐时，:meth:`DshLLM._invoke` 抬一档预算
  重试一次——reasoning 模型的思考量有突发性，一次跑偏不该让整条生成链 502。
  两次的 usage 都进账本与返回值，两次都截断才抛 :class:`generation.llm.LLMAPIError`。
- **调用串行化**：一把进程内锁串起所有 run。生成链本身就是顺序的，串行换来的是
  "runtime 进程数可预测"，以及池子淘汰不会踩到正在跑的会话。
- **system 提示词**：SDK 线协议没有 per-call system 通道（persona 在 cordis 里，是进程级的），
  所以 ``system=`` 被拼进用户消息的开头。这是与 Anthropic 后端最大的行为差异。
- **usage**：从 session events 里取 provider 上报的真实 token 数，实测结论见
  :func:`usage_from_events` 的 docstring。取不到才退化成字符估算，并在流水 meta 里
  标 ``estimated=true``——预算闸门不允许失效。

红线：受限 cordis 组合（``configs/dsh/cordis.yml``）不给模型挂任何工具。
"""

from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import BaseModel, ValidationError

from core.budget import BudgetGuard
from generation.llm import (
    Effort,
    GenerationRefused,
    LLMAPIError,
    LLMConnectionError,
    LLMRateLimited,
    LLMResult,
    LLMUnavailable,
    ParsedResult,
    Usage,
    charge_usage,
)
from generation.model_routing import ModelRoute, resolve_model_route
from generation.output_budget import escalate
from generation.textutil import accumulate_usage

logger = logging.getLogger("social_workflow.generation.llm_dsh")

#: 没装 dsh extra 时的统一提示。工厂与 preflight 都用它，避免两处说法不一致。
INSTALL_HINT = (
    "deepseek-harness SDK 未安装：`uv sync --extra dsh`"
    "（会连带装 deepseek-harness-runtime-bin 平台轮子）。"
    "或把 SW_LLM_BACKEND 改回 anthropic。"
)

# dsh 的 LlmError code 分类。route on code，绝不 parse message（dsh 自己的告诫）。
#: 前置条件缺失：改配置才有用，重试没意义。
UNAVAILABLE_CODES = frozenset(
    {
        "MISSING_CREDENTIAL",
        "INVALID_CREDENTIAL",
        "AUTH",
        "NO_ADAPTER",
        "UNKNOWN_MODEL",
        "UNSUPPORTED_REASONING_EFFORT",
        "UNSUPPORTED_OPTION",
    }
)
#: 可退避重试。
RATE_LIMIT_CODES = frozenset({"RATE_LIMIT"})
#: 网络层没拿到响应。
CONNECTION_CODES = frozenset({"TRANSPORT", "TIMEOUT"})

#: dsh 的 turn/end kind → Anthropic 的 stop_reason 词汇。两个后端对上层同形。
STOP_REASON_ALIASES = {"completed": "end_turn", "max-tokens": "max_tokens"}


# ------------------------------------------------------------------ 组合审计


class _CordisLoader(yaml.SafeLoader):
    """能读 dsh cordis.yml 的 SafeLoader。

    dsh 用 ``!!js <表达式>`` 让配置读环境变量。Python 侧不执行它，只把表达式原样
    当字符串收下——我们只需要看清"挂了哪些插件、引用了哪个 apiKeyEnv"。

    安全性：基类是 ``SafeLoader``，这里只多注册了一个**返回字符串**的标量构造器，
    没有放开任何 ``!!python/*`` 标签，所以不存在 ``yaml.load`` 的任意对象构造风险。
    """


def _construct_js(loader: yaml.Loader, node: yaml.Node) -> str:
    return str(loader.construct_scalar(node))  # type: ignore[arg-type]


_CordisLoader.add_constructor("tag:yaml.org,2002:js", _construct_js)


def load_composition(path: Path | str) -> list[dict[str, Any]]:
    """读受限 cordis 组合，返回插件条目列表。"""
    raw = yaml.load(Path(path).read_text(encoding="utf-8"), Loader=_CordisLoader)
    if not isinstance(raw, list):
        raise ValueError(f"cordis 组合必须是插件条目列表: {path}")
    return [entry for entry in raw if isinstance(entry, dict)]


#: 组合里出现这些插件名片段，就意味着模型**可能**拿到工具或执行器——红线，一个都不许有。
#: 既拦模型可见的工具（``dsh-tool-*``、``str-replace-editor``、``web-*``），
#: 也拦它们依赖的执行器（bash / terminal / fs / sandbox / subprocess / subagent），
#: 因为"挂了执行器只是暂时没挂工具"这种状态一次改配置就会翻车。
FORBIDDEN_PLUGIN_MARKERS = (
    "dsh-tool-",
    "dsh-bash",
    "dsh-shell",
    "dsh-terminal",
    "dsh-fs-",
    "dsh-sandbox",
    "dsh-subprocess",
    "dsh-subagent",
    "dsh-web",
    "dsh-mcp",
    "dsh-acp",
    "dsh-skill",
    "dsh-jobs",
    "dsh-code-runtime",
    "dsh-workflow",
    "dsh-plan-mode",
    "dsh-commands",
    "dsh-hooks",
    "str-replace-editor",
)

#: ``agent-spine-demo`` 自带的三组模型可见工具，必须逐项关掉（它们默认是开的）。
SPINE_REQUIRED_CONFIG: dict[str, Any] = {
    "toolBash": False,
    "toolJobs": False,
    "workspaceContext": False,
}


def audit_composition(entries: list[dict[str, Any]]) -> list[str]:
    """静态审计"零工具"红线，返回违规说明列表；**空列表 = 合规**。

    这是可机械验证的第一道：不起进程就能跑，任何人改了 cordis.yml 立刻被测试打回。
    第二道是真起 runtime 看 ``request/header.header.tools``，见 :func:`tool_schemas_in`。
    """
    findings: list[str] = []
    for entry in entries:
        name = str(entry.get("name") or "")
        for marker in FORBIDDEN_PLUGIN_MARKERS:
            if marker in name:
                findings.append(f"组合挂载了工具/执行器插件 {name}（命中 {marker}）")
                break
        if "agent-spine-demo" not in name:
            continue
        config = entry.get("config")
        config = config if isinstance(config, dict) else {}
        for key, expected in SPINE_REQUIRED_CONFIG.items():
            if config.get(key) is not expected:
                findings.append(f"agent-spine-demo 的 {key} 必须显式为 {expected!r}")
        skills = config.get("skills")
        if not isinstance(skills, dict) or skills.get("enabled") is not False:
            findings.append("agent-spine-demo 的 skills.enabled 必须显式为 False")
        if config.get("goals") not in (None, False):
            findings.append("agent-spine-demo 不得启用 goals（会带出 goal 工具）")
    return findings


def provider_api_key_env(entries: list[dict[str, Any]], provider: str) -> str | None:
    """取某条 provider 路由声明的 ``apiKeyEnv``（凭据引用，不是密钥本身）。"""
    for entry in entries:
        if "llm-pi-ai" not in str(entry.get("name") or ""):
            continue
        config = entry.get("config")
        providers = config.get("providers") if isinstance(config, dict) else None
        profile = providers.get(provider) if isinstance(providers, dict) else None
        if isinstance(profile, dict):
            value = profile.get("apiKeyEnv")
            return str(value) if value else None
    return None


def provider_cache_retention(entries: list[dict[str, Any]], provider: str) -> str | None:
    """取某条 provider 路由声明的 ``cacheRetention``；没声明返回 ``None``。

    这是**前缀缓存的开关**：pi-ai 的 ``openai-completions`` 只在 baseURL 含
    ``api.openai.com``、或本字段是 ``long`` 时，才把 ``prompt_cache_key`` 放进请求体。
    本项目走私有网关，URL 那条对不上，所以漏了这一行 = 一个键都不发 = 命中率恒为 0。
    """
    for entry in entries:
        if "llm-pi-ai" not in str(entry.get("name") or ""):
            continue
        config = entry.get("config")
        providers = config.get("providers") if isinstance(config, dict) else None
        profile = providers.get(provider) if isinstance(providers, dict) else None
        if isinstance(profile, dict):
            value = profile.get("cacheRetention")
            return str(value) if value else None
    return None


def audit_provider_models(
    entries: list[dict[str, Any]],
    provider: str,
    required: dict[str, set[str]],
) -> list[str]:
    """静态确认所选 provider 声明了路由模型与所需 reasoning effort。

    只检查组合声明，不启动 SDK。catalog 隐式模型表无法机械核对，因此在启用多模型
    路由时必须改用显式列出 ``models`` 的外部零工具 Cordis 组合。
    """
    profile: dict[str, Any] | None = None
    for entry in entries:
        if "llm-pi-ai" not in str(entry.get("name") or ""):
            continue
        config = entry.get("config")
        providers = config.get("providers") if isinstance(config, dict) else None
        candidate = providers.get(provider) if isinstance(providers, dict) else None
        if isinstance(candidate, dict):
            profile = candidate
            break
    if profile is None:
        return [f"组合里没有名为 {provider!r} 的 provider 路由"]

    declared = profile.get("models")
    if not isinstance(declared, list):
        return [f"provider {provider!r} 没有显式 models 表，无法静态审计模型路由"]

    profiles: dict[str, list[dict[str, Any]]] = {}
    for item in declared:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if isinstance(model_id, str) and model_id:
            profiles.setdefault(model_id, []).append(item)

    findings: list[str] = []
    for model, efforts in required.items():
        candidates = profiles.get(model, [])
        if not candidates:
            findings.append(f"provider {provider!r} 缺少模型 {model!r}")
            continue
        supported = False
        for candidate in candidates:
            mapping = candidate.get("reasoningEfforts")
            if isinstance(mapping, dict) and all(
                mapping.get(effort) == effort for effort in efforts
            ):
                supported = True
                break
        if not supported:
            expected = ", ".join(f"{effort}:{effort}" for effort in sorted(efforts))
            actual: list[str] = []
            for candidate in candidates:
                mapping = candidate.get("reasoningEfforts")
                for effort in sorted(efforts):
                    value = mapping.get(effort) if isinstance(mapping, dict) else None
                    actual.append(f"{effort}:{value}" if value is not None else f"{effort}:<缺失>")
            actual_text = ", ".join(actual)
            requirement = f"模型 {model!r} reasoning effort 必须精确映射 {expected}"
            findings.append(f"{requirement}；实际 {actual_text}")
    return findings


def dsh_credentials_ready(settings: Any = None) -> bool:
    """所选路由的 ``apiKeyEnv`` 是否已经在环境里有值。读不到组合文件按"没准备好"。"""
    if settings is None:
        from core.config import get_settings

        settings = get_settings()
    try:
        entries = load_composition(Path.cwd() / settings.dsh_cordis_path)
    except (OSError, ValueError):
        return False
    env_name = provider_api_key_env(entries, settings.dsh_provider)
    return bool(env_name and os.environ.get(env_name))


# ------------------------------------------------------------------ runtime 接缝


class HarnessRunResult(Protocol):
    """``deepseek_harness.RunResult`` 里本模块真正会读的三个字段。"""

    final_response: str
    finish_reason: str | None
    events: list[dict[str, Any]]


class SupportsHarness(Protocol):
    """``DeepSeekHarness`` 的最小面。测试注入假实现，不起真 runtime。"""

    # 形参名 input 与 SDK 一致，方便直接把 DeepSeekHarness 当实现塞进来
    def run(self, input: str, *, session_id: str) -> HarnessRunResult: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class RuntimeKey:
    """一个 runtime 子进程的身份：模型 + 思考档 + 输出上限。"""

    model: str
    effort: str
    max_tokens: int


@dataclass(frozen=True)
class DshRuntimeOptions:
    """起 runtime 需要的全部配置，来自 :class:`core.config.Settings`。"""

    provider: str
    model: str
    cordis_path: Path
    session_root: Path
    cwd: Path
    request_timeout_seconds: float
    stream_idle_timeout_ms: int
    max_live_runtimes: int = 4

    @classmethod
    def from_settings(cls, settings: Any = None) -> DshRuntimeOptions:
        if settings is None:
            from core.config import get_settings

            settings = get_settings()
        root = Path.cwd()
        return cls(
            provider=settings.dsh_provider,
            model=settings.dsh_model,
            cordis_path=(root / settings.dsh_cordis_path).resolve(),
            session_root=(root / settings.dsh_session_root).resolve(),
            cwd=root,
            request_timeout_seconds=settings.llm_timeout_seconds,
            # runtime 侧的"单次 provider 读空闲上限"，与本地 HTTP 超时同一个量级
            stream_idle_timeout_ms=int(settings.llm_timeout_seconds * 1000),
            max_live_runtimes=settings.dsh_max_live_runtimes,
        )


HarnessFactory = Callable[[RuntimeKey, DshRuntimeOptions], SupportsHarness]


def default_harness_factory(key: RuntimeKey, options: DshRuntimeOptions) -> SupportsHarness:
    """真正拉起 bundled runtime。任何前置条件缺失都归一成 :class:`LLMUnavailable`。"""
    try:
        from deepseek_harness import DeepSeekHarness
    except ImportError as exc:  # pragma: no cover - 装了 extra 就走不到
        raise LLMUnavailable(INSTALL_HINT) from exc

    if not options.cordis_path.is_file():
        raise LLMUnavailable(f"dsh cordis 组合文件不存在: {options.cordis_path}")
    options.session_root.mkdir(parents=True, exist_ok=True)

    try:
        harness = DeepSeekHarness(
            provider=options.provider,
            model=key.model,
            max_tokens=key.max_tokens,
            cordis=str(options.cordis_path),
            session_root=str(options.session_root),
            cwd=str(options.cwd),
            env={
                "SW_DSH_REASONING": key.effort,
                "SW_DSH_STREAM_IDLE_TIMEOUT_MS": str(options.stream_idle_timeout_ms),
            },
            request_timeout_seconds=options.request_timeout_seconds,
        )
        # 握手在这里做掉：runtime 起不来 / 组合配错要在第一次调用就炸，而不是等到 run()
        harness.start()
    except LLMUnavailable:
        raise
    except FileNotFoundError as exc:
        raise LLMUnavailable(f"dsh runtime 二进制缺失（平台轮子没装全？）: {exc}") from exc
    except Exception as exc:
        raise LLMUnavailable(f"dsh runtime 启动失败: {exc}") from exc
    return harness


class DshRuntimePool:
    """按 :class:`RuntimeKey` 分桶的 runtime 进程池，LRU 封顶，全部调用串行。"""

    def __init__(
        self,
        options: DshRuntimeOptions | None = None,
        *,
        factory: HarnessFactory | None = None,
    ) -> None:
        self._options = options
        self._factory = factory or default_harness_factory
        self._harnesses: OrderedDict[RuntimeKey, SupportsHarness] = OrderedDict()
        self._lock = threading.RLock()
        self._closed = False

    @property
    def options(self) -> DshRuntimeOptions:
        if self._options is None:
            self._options = DshRuntimeOptions.from_settings()
        return self._options

    def _acquire(self, key: RuntimeKey) -> SupportsHarness:
        harness = self._harnesses.get(key)
        if harness is not None:
            self._harnesses.move_to_end(key)
            return harness
        harness = self._factory(key, self.options)
        self._harnesses[key] = harness
        while len(self._harnesses) > max(1, self.options.max_live_runtimes):
            evicted_key, evicted = self._harnesses.popitem(last=False)
            logger.info("dsh runtime 池超限，淘汰 %s", evicted_key)
            _close_quietly(evicted)
        return harness

    def run(
        self,
        prompt: str,
        *,
        model: str,
        effort: str,
        max_tokens: int,
        cache_key: str,
    ) -> HarnessRunResult:
        """跑一轮，返回 ``RunResult``。子进程死掉当作 :class:`LLMUnavailable`。

        ``cache_key`` 铺进这次调用 session id 的**前 64 个字节**（:func:`session_id_for`），
        runtime 把它原样截成 ``prompt_cache_key``。会话本身仍是一次一个新的——后缀唯一，
        所以同一个缓存键下的多次调用不会互相串历史。

        缓存键**刻意不进** :class:`RuntimeKey`：它是逐调用参数，不是进程级参数，
        并进去只会把同一个子进程按前缀劈成好几份，白白多起进程。
        """
        key = RuntimeKey(model=model, effort=effort, max_tokens=int(max_tokens))
        session_id = session_id_for(cache_key)
        with self._lock:
            if self._closed:
                raise LLMUnavailable("dsh runtime 池已关闭")
            harness = self._acquire(key)
            try:
                return harness.run(prompt, session_id=session_id)
            except Exception as exc:
                # 传输断了就把这个桶丢掉，下次调用重开一个干净的子进程
                self._harnesses.pop(key, None)
                _close_quietly(harness)
                raise LLMUnavailable(f"dsh runtime 调用失败（子进程已回收）: {exc}") from exc

    def close(self) -> None:
        with self._lock:
            self._closed = True
            harnesses = list(self._harnesses.values())
            self._harnesses.clear()
        for harness in harnesses:
            _close_quietly(harness)

    @property
    def live_keys(self) -> list[RuntimeKey]:
        with self._lock:
            return list(self._harnesses)


def _close_quietly(harness: SupportsHarness) -> None:
    try:
        harness.close()
    except Exception:  # pragma: no cover - 关闭失败不该冒泡
        logger.warning("dsh runtime 关闭异常", exc_info=True)


# 进程级共享池：``build_llm`` 每次都新建 ``DshLLM``（budget 不同），但子进程只能有一份。
_shared_pool: DshRuntimePool | None = None
_shared_pool_lock = threading.Lock()


def shared_pool() -> DshRuntimePool:
    global _shared_pool
    with _shared_pool_lock:
        if _shared_pool is None:
            _shared_pool = DshRuntimePool()
            atexit.register(close_shared_pool)
        return _shared_pool


def close_shared_pool() -> None:
    """释放共享 runtime 子进程。幂等，FastAPI lifespan 与 atexit 都会调。"""
    global _shared_pool
    with _shared_pool_lock:
        pool, _shared_pool = _shared_pool, None
    if pool is not None:
        pool.close()


# ------------------------------------------------------------------ 事件解析


def usage_from_events(events: list[dict[str, Any]]) -> Usage | None:
    """从 session events 提取 provider 上报的真实 token 用量；没有就返回 ``None``。

    实测结论（受限组合 + OpenAI 兼容端点，见交付报告"usage 实测"）：

    - 两处携带用量：``assistant/chunk`` 里 ``chunk.type == "usage"``，以及该步收尾的
      ``assistant/message`` 的 ``data.usage``。同一个 ``(turn, step)`` 两者一致，
      后者是终值，所以按 ``(turn, step)`` 去重、``assistant/message`` 覆盖 chunk。
    - 字段名 ``inputTokens`` / ``outputTokens`` / ``cacheReadTokens`` / ``cacheWriteTokens``
      （后两个可缺席），四个桶**互不重叠**：上游返回 ``prompt_tokens=1234`` +
      ``cached_tokens=1000`` 时，dsh 上报 ``inputTokens=234`` + ``cacheReadTokens=1000``。
      这与 :class:`generation.llm.Usage` 的 ``billable = input + output`` 语义天然对齐。
    - ``reasoningTokens`` 已经折进 ``outputTokens``，不再单独加（dsh/pi-ai 明文如此）。
    - dsh 的 retry 会开新的 ``step``，每次 provider 尝试都可能计费，所以跨 step 求和。
    """
    samples: dict[tuple[int, int], dict[str, Any]] = {}
    finalized: set[tuple[int, int]] = set()
    for event in events:
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        turn, step = data.get("turn"), data.get("step")
        if not isinstance(turn, int) or not isinstance(step, int):
            continue
        slot = (turn, step)
        if event.get("type") == "assistant/message":
            usage = data.get("usage")
            if isinstance(usage, dict):
                samples[slot] = usage
                finalized.add(slot)
            continue
        if event.get("type") != "assistant/chunk" or slot in finalized:
            continue
        chunk = data.get("chunk")
        if isinstance(chunk, dict) and chunk.get("type") == "usage":
            usage = chunk.get("usage")
            if isinstance(usage, dict):
                samples[slot] = usage
    if not samples:
        return None

    def total(name: str) -> int:
        return sum(int(u.get(name) or 0) for u in samples.values())

    return Usage(
        input_tokens=total("inputTokens"),
        output_tokens=total("outputTokens"),
        cache_read_input_tokens=total("cacheReadTokens"),
        cache_creation_input_tokens=total("cacheWriteTokens"),
    )


#: 中日韩统一表意文字区间；CJK 一字≈一 token，拉丁按 4 字符一 token。
_CJK_RANGES = ((0x3400, 0x9FFF), (0xF900, 0xFAFF), (0x20000, 0x2FA1F))


def estimate_tokens(text: str) -> int:
    """字符级保守估算。宁可高估——预算闸门宁可早关也不能漏计。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if any(lo <= ord(ch) <= hi for lo, hi in _CJK_RANGES))
    latin = len(text) - cjk
    return cjk + -(-latin // 4)


def resolved_model(events: list[dict[str, Any]], fallback: str) -> str:
    """从 ``request/header`` 拿 runtime 实际用的模型串。"""
    for event in reversed(events):
        if event.get("type") != "request/header":
            continue
        data = event.get("data")
        header = data.get("header") if isinstance(data, dict) else None
        config = header.get("config") if isinstance(header, dict) else None
        model = config.get("model") if isinstance(config, dict) else None
        if isinstance(model, str) and model:
            return model
    return fallback


def tool_schemas_in(events: list[dict[str, Any]]) -> list[Any]:
    """列出 runtime 真正发给模型的工具 schema。受限组合下必须恒为空。

    ``request/header`` 是"下一次请求的完整快照，在派发前追加进 session log"，
    无工具时 ``header.tools`` 整个字段缺席——这就是"零工具"的机械证据。
    """
    tools: list[Any] = []
    for event in events:
        if event.get("type") != "request/header":
            continue
        data = event.get("data")
        header = data.get("header") if isinstance(data, dict) else None
        declared = header.get("tools") if isinstance(header, dict) else None
        if isinstance(declared, list):
            tools.extend(declared)
    return tools


def failure_of(events: list[dict[str, Any]]) -> tuple[str, str]:
    """取最后一次 ``turn/end`` 的结构化失败 ``(code, message)``。"""
    for event in reversed(events):
        if event.get("type") != "turn/end":
            continue
        data = event.get("data")
        reason = data.get("reason") if isinstance(data, dict) else None
        error = reason.get("error") if isinstance(reason, dict) else None
        if isinstance(error, dict):
            return str(error.get("code") or "UNKNOWN"), str(error.get("message") or "")
        break
    return "UNKNOWN", ""


# ------------------------------------------------------------------ JSON 提取


def iter_json_objects(text: str) -> list[str]:
    """扫出文本里所有**顶层**平衡的 ``{...}`` 片段，按出现顺序返回。

    只认顶层：嵌套对象不会被单独切出来，所以"取最后一个"拿到的是最后一个完整对象，
    而不是它内部的某个子对象。字符串字面量里的花括号与转义都跳过。
    """
    spans: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                spans.append(text[start : index + 1])
                start = -1
    return spans


def parse_last_json(text: str, output_format: type[BaseModel]) -> BaseModel:
    """取最后一个能 ``json.loads`` 成功的顶层对象，交给 Pydantic 校验。

    失败一律抛 :class:`pydantic.ValidationError` 或 :class:`ValueError`，
    由 :meth:`DshLLM.parse` 决定要不要带着错误信息重试。
    """
    candidates = iter_json_objects(text)
    if not candidates:
        raise ValueError("回复里找不到任何 JSON 对象")
    last_error: Exception | None = None
    for raw in reversed(candidates):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        return output_format.model_validate(payload)
    raise ValueError(f"回复里的 JSON 片段都解析失败: {last_error}")


def schema_instruction(output_format: type[BaseModel]) -> str:
    """给 prompt 尾部加的结构化输出指令。"""
    schema = json.dumps(output_format.model_json_schema(), ensure_ascii=False)
    return (
        "【输出格式】\n"
        "只输出一个 JSON 对象，不要加解释、不要加 Markdown 代码块围栏。"
        "该对象必须满足下面的 JSON Schema：\n"
        f"{schema}"
    )


def compose_prompt(prompt: str, system: str | None) -> str:
    """dsh 的 SDK 线协议没有 per-call system 通道，只能把它拼进用户消息。"""
    if not system or not system.strip():
        return prompt
    return f"【系统设定】\n{system.strip()}\n\n【任务】\n{prompt}"


# ------------------------------------------------------------------ 前缀缓存


#: pi-ai 的 ``clampOpenAIPromptCacheKey`` 把 ``prompt_cache_key`` 截到前 64 个字符
#: （它的 ``OPENAI_PROMPT_CACHE_KEY_MAX_LENGTH``）。这个常数是本文件全部长度算术的锚点：
#: 只有**恰好** 64 个字符的键，才等于"session id 被截断后剩下的东西"。
PROMPT_CACHE_KEY_LENGTH = 64

#: 键尾的摘要位数（blake2b 16 字节 → 32 个十六进制字符）。真正保证"不同前缀不同键"的是它。
_CACHE_KEY_DIGEST_CHARS = 32
#: 键头留给可读标签的位数。截断只影响可读性，不影响区分度——区分度全在摘要里。
_CACHE_KEY_HEAD_CHARS = PROMPT_CACHE_KEY_LENGTH - 1 - _CACHE_KEY_DIGEST_CHARS


def stable_prefix(system: str | None) -> str:
    """同一组调用**逐字节相同**的那段请求开头。

    :func:`compose_prompt` 把 system 折在用户消息最前面，所以"``【系统设定】`` + system 段
    + ``【任务】`` 标记"就是同一个 system 的多次调用共享的字面前缀，跟 ``prompt`` 无关。
    ``system`` 为空时返回空串——那种调用之间除了进程级 persona 没有共享前缀，如实表达。
    """
    return compose_prompt("", system)


def _slug(text: str) -> str:
    """把模型名压成键头能放的字符集。只为可读，不承担区分度。"""
    kept = "".join(ch if ch.isascii() and ch.isalnum() else "-" for ch in text.lower())
    return kept.strip("-") or "model"


def prompt_cache_key(*, model: str, system: str | None) -> str:
    """一组共享前缀的调用共用的缓存键；长度**恒为** :data:`PROMPT_CACHE_KEY_LENGTH`。

    取值按"**哪些调用共享同一段字面前缀**"分组，而不是按 purpose 或账号：
    ``(model, stable_prefix(system))`` 相同 ⇔ 请求开头逐字节相同 ⇔ 该共用一个键。
    这条等价关系就是 codex ``prompt_caching`` 那两条契约在本仓的落法——键稳定（①）
    正是因为被哈希的那段前缀逐字节稳定（②）。

    model 进哈希是因为缓存本来就按模型分家；purpose **不**进：同一条流水线里
    多个 purpose 共享同一个 system 段，按 purpose 再切一刀只会把它们拆成互不复用的
    单例，命中率退回 0。

    注意分组**改变不了**共享前缀的长度，而长度才是上游肯不肯缓存的门槛，且门槛按路由
    分家：本仓的稳定前缀只有 system 段那 ~500 token，在 deepseek 路由（``~300`` token
    就缓存）之上、在 gpt-5.6 路由（门槛在 1561 与 1921 token 之间）之下。所以开着
    ``SW_DSH_MODEL_ROUTING`` 时，真正吃到缓存的只剩"原样重发同一条 prompt"的重试
    路径。两条路由的实测数字见 ``generation/README.md``。
    """
    payload = f"{model}\x00{stable_prefix(system)}".encode()
    digest = hashlib.blake2b(payload, digest_size=_CACHE_KEY_DIGEST_CHARS // 2).hexdigest()
    head = f"sw-{_slug(model)}"[:_CACHE_KEY_HEAD_CHARS].ljust(_CACHE_KEY_HEAD_CHARS, "-")
    return f"{head}-{digest}"


def session_id_for(cache_key: str) -> str:
    """把缓存键铺成一次调用专用的 session id。

    前 :data:`PROMPT_CACHE_KEY_LENGTH` 个字符原样是缓存键（runtime 截到这里当
    ``prompt_cache_key``），后缀每次都换，于是 runtime 侧每次都是一个**新会话**：
    同一个缓存键下的多次调用共享缓存，但绝不共享对话历史。
    """
    if len(cache_key) != PROMPT_CACHE_KEY_LENGTH:
        raise ValueError(
            f"缓存键必须恰好 {PROMPT_CACHE_KEY_LENGTH} 个字符才能被原样截出，实际 {len(cache_key)}"
        )
    return f"{cache_key}-{uuid.uuid4().hex}"


# ------------------------------------------------------------------ 客户端


@dataclass(frozen=True)
class _Attempt:
    """一次 ``harness.run`` 的结果。``truncated_empty`` 是"该加码重试"的唯一信号。

    ``max_tokens`` 是这一轮**实际用掉**的预算：自愈加码之后它和调用方最初给的值不一样，
    而 :meth:`DshLLM.parse` 要从"真正用过的那一档"继续往上抬，否则会抬回一个
    刚刚已经被截断过的预算。
    """

    text: str
    usage: Usage
    model: str
    stop_reason: str | None
    truncated_empty: bool
    max_tokens: int


class DshLLM:
    """``SupportsLLM`` 的 dsh 实现。签名与 :class:`generation.llm.LLMClient` 完全一致。"""

    def __init__(
        self,
        *,
        pool: DshRuntimePool | None = None,
        budget: BudgetGuard | None = None,
        model: str | None = None,
        effort: Effort | None = None,
        max_tokens: int | None = None,
        article_max_tokens: int | None = None,
    ) -> None:
        from core.config import get_settings

        settings = get_settings()
        self._pool = pool
        self.budget = budget
        self.model = model or settings.dsh_model
        self.effort: Effort = effort or settings.llm_effort
        self.model_routing = settings.sw_llm_backend == "dsh" and bool(settings.dsh_model_routing)
        self.sol_model = settings.dsh_sol_model
        self.luna_model = settings.dsh_luna_model
        #: ``SW_DSH_MAX_TOKENS > 0`` 时给本后端再压一道统一上限（0 = 沿用 LLM_* 的两档）
        self.hard_max_tokens = int(settings.dsh_max_tokens or 0)
        self.max_tokens = self._cap(max_tokens or settings.llm_max_tokens)
        self.article_max_tokens = self._cap(article_max_tokens or settings.llm_article_max_tokens)

    def _cap(self, value: int) -> int:
        return min(value, self.hard_max_tokens) if self.hard_max_tokens > 0 else value

    @property
    def pool(self) -> DshRuntimePool:
        if self._pool is None:
            self._pool = shared_pool()
        return self._pool

    def close(self) -> None:
        """释放 runtime 子进程。幂等；注入过 pool 时只关自己那个。"""
        if self._pool is None:
            close_shared_pool()
            return
        self._pool.close()

    # -- 内部 --------------------------------------------------------------

    def _bigger_budget(self, current: int) -> int | None:
        """加码后的预算；已经到顶（或被 ``SW_DSH_MAX_TOKENS`` 压回原值）返回 ``None``。"""
        nxt = escalate(current)
        if nxt is None:
            return None
        capped = self._cap(nxt)
        return capped if capped > current else None

    def _route(self, purpose: str, effort: Effort | None) -> ModelRoute:
        return resolve_model_route(
            purpose,
            enabled=self.model_routing,
            legacy_model=self.model,
            legacy_effort=self.effort,
            explicit_effort=effort,
            sol_model=self.sol_model,
            luna_model=self.luna_model,
        )

    def _attempt(
        self,
        prompt: str,
        *,
        system: str | None,
        max_tokens: int,
        route: ModelRoute,
    ) -> _Attempt:
        """跑一轮并记账。除"空截断"外的失败都在这里就地抛掉。"""
        sent = compose_prompt(prompt, system)
        cache_key = prompt_cache_key(model=route.model, system=system)
        result = self.pool.run(
            sent,
            model=route.model,
            effort=route.effort,
            max_tokens=max_tokens,
            cache_key=cache_key,
        )
        events = list(result.events or [])
        text = (result.final_response or "").strip()
        model = resolved_model(events, route.model)
        finish = result.finish_reason
        stop_reason = STOP_REASON_ALIASES.get(str(finish), finish)

        usage = usage_from_events(events)
        estimated = usage is None
        if usage is None:
            usage = Usage(
                input_tokens=estimate_tokens(sent),
                output_tokens=estimate_tokens(text),
            )
            logger.warning(
                "dsh 未上报 usage，按字符保守估算记账: purpose=%s model=%s billable=%d",
                route.purpose,
                model,
                usage.billable,
            )
        # 先记账再判失败：token 已经真实花掉了，账本不能因为这轮失败就漏记。
        # ``stop_reason`` / ``max_tokens`` 一起落进 meta：事后要能查"这次是不是贴着
        # 天花板收尾的"，只看 output_tokens 只能猜——这本身就是个观测缺口。
        meta: dict[str, Any] = {
            "backend": "dsh",
            "stop_reason": stop_reason,
            "max_tokens": max_tokens,
            "effort": route.effort,
            "complexity": route.complexity,
            # 事后能按键聚合命中率：账本里已经有 cache_read_input_tokens，配上键才知道
            # 是"哪一组前缀"在命中或没命中，不必再去翻 session log。
            "cache_key": cache_key,
        }
        if estimated:
            meta["estimated"] = True
        charge_usage(self.budget, usage, purpose=route.purpose, model=model, extra_meta=meta)

        truncated_empty = self._check_finish(finish, events, model=model, text=text)
        return _Attempt(
            text=text,
            usage=usage,
            model=model,
            stop_reason=stop_reason,
            truncated_empty=truncated_empty,
            max_tokens=max_tokens,
        )

    def _invoke(
        self,
        prompt: str,
        *,
        system: str | None,
        max_tokens: int,
        route: ModelRoute,
    ) -> _Attempt:
        """跑一轮并记账，必要时**加码重试一次**。

        输出被 ``max_tokens`` 截断且**一个字都没吐**时不直接抛死，而是抬一档预算重试
        （:func:`generation.output_budget.escalate`）。这条路径以前是就地 raise，
        而它发生在 :meth:`parse` 的重试循环**内部**，所以连一次重试都没有——
        2026-08-17 生产事故里 ``xhs.angle`` 就是这样让整条生成链 502 的。

        返回的 ``Usage`` 是**两次之和**：第一次烧掉的 token 是真花了的，账本不能漏。
        """
        first = self._attempt(prompt, system=system, max_tokens=max_tokens, route=route)
        if not first.truncated_empty:
            return first

        bigger = self._bigger_budget(max_tokens)
        if bigger is None:
            raise LLMAPIError(
                f"dsh 输出被 max_tokens 截断且没有任何内容"
                f"（预算 {max_tokens} 已是上限，无法加码重试）"
            )
        logger.warning(
            "输出预算不够，已加码重试: purpose=%s max_tokens %d → %d",
            route.purpose,
            max_tokens,
            bigger,
        )
        second = self._attempt(prompt, system=system, max_tokens=bigger, route=route)
        if second.truncated_empty:
            raise LLMAPIError(
                f"dsh 输出两次都被 max_tokens 截断且没有任何内容（预算 {max_tokens} → {bigger}）"
            )
        return replace(second, usage=accumulate_usage(first.usage, second.usage))

    @staticmethod
    def _check_finish(
        finish: str | None,
        events: list[dict[str, Any]],
        *,
        model: str,
        text: str,
    ) -> bool:
        """``turn/end`` 的 kind → 本项目异常。

        返回 ``True`` 表示"被 ``max-tokens`` 截断且没有任何内容"——**只有这一种不抛**，
        交给 :meth:`_invoke` 加码重试。其余失败照旧就地抛死。
        """
        if finish == "completed":
            return False
        if finish == "max-tokens":
            # Anthropic 后端在 stop_reason=max_tokens 时也是返回截断文本交给调用方
            return not text
        if finish == "blocked":
            raise GenerationRefused("blocked", "dsh guard 拦截了这次生成", model=model)
        if finish is None:
            raise LLMAPIError("dsh runtime 没有产出 turn/end，无法判定本轮结果")
        if finish != "error":
            raise LLMAPIError(f"dsh 异常结束: finish_reason={finish}")

        code, message = failure_of(events)
        detail = f"dsh 请求失败[{code}]: {message}"
        if code in UNAVAILABLE_CODES:
            raise LLMUnavailable(detail)
        if code in RATE_LIMIT_CODES:
            raise LLMRateLimited(detail)
        if code in CONNECTION_CODES:
            raise LLMConnectionError(detail)
        raise LLMAPIError(detail)

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
        route = self._route(purpose, effort)
        done = self._invoke(
            prompt,
            system=system,
            max_tokens=self._cap(max_tokens or self.max_tokens),
            route=route,
        )
        return LLMResult(
            text=done.text, usage=done.usage, model=done.model, stop_reason=done.stop_reason
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
        route = self._route(purpose, effort)
        done = self._invoke(
            prompt,
            system=system,
            max_tokens=self._cap(max_tokens or self.article_max_tokens),
            route=route,
        )
        return LLMResult(
            text=done.text, usage=done.usage, model=done.model, stop_reason=done.stop_reason
        )

    def parse[T: BaseModel](
        self,
        prompt: str,
        output_format: type[T],
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        effort: Effort | None = None,
        purpose: str = "parse",
    ) -> ParsedResult[T]:
        """结构化输出：prompt 尾部注入 schema，回复里取最后一个 JSON 对象校验。

        失败**分两种**重试（这是 dsh 侧没有原生 structured output 的代价）：

        - **格式错**：模型自由发挥写歪了 → 带着校验错误信息重发，预算不变。
        - **被截断**（``stop_reason == "max_tokens"``，JSON 写到一半断了）→ 抬一档预算
          重发**原始** prompt。这种情况把"JSON 不完整"回喂给模型毫无意义：它不是不会写，
          是没地方写；回喂还会把 prompt 撑得更长、更容易再截一次。

        两次都不合格抛 :class:`generation.llm.LLMAPIError`。``usage`` 返回两次之和。
        """
        base = f"{prompt}\n\n{schema_instruction(output_format)}"
        attempt_prompt = base
        budget = self._cap(max_tokens or self.max_tokens)
        totals = Usage()
        model = self.model
        stop_reason: str | None = None
        last_error: Exception | None = None
        route = self._route(purpose, effort)

        for attempt in range(2):
            done = self._invoke(
                attempt_prompt,
                system=system,
                max_tokens=budget,
                route=route,
            )
            text, model, stop_reason = done.text, done.model, done.stop_reason
            totals = accumulate_usage(totals, done.usage)
            # _invoke 内部可能已经自愈加过码了：要从**真正用过**的那一档继续往上抬，
            # 否则会抬回一个刚刚已经被截断过的预算
            budget = done.max_tokens
            try:
                parsed = parse_last_json(text, output_format)
            except (ValidationError, ValueError) as exc:
                last_error = exc
                if attempt == 0:
                    bigger = self._bigger_budget(budget) if stop_reason == "max_tokens" else None
                    if bigger is not None:
                        logger.warning(
                            "dsh 结构化输出被截断（不是格式错），加码重试: "
                            "purpose=%s max_tokens %d → %d",
                            purpose,
                            budget,
                            bigger,
                        )
                        budget = bigger
                    else:
                        logger.warning("dsh 结构化输出校验失败，带错误信息重试一次: %s", exc)
                        attempt_prompt = (
                            f"{base}\n\n【上一次的回复不合格】\n{text[:2000]}\n\n"
                            f"【校验错误】\n{exc}\n\n请只输出修正后的 JSON 对象。"
                        )
                continue
            return ParsedResult(
                parsed=parsed,  # type: ignore[arg-type]
                usage=totals,
                model=model,
                stop_reason=stop_reason,
            )

        raise LLMAPIError(f"dsh 结构化输出两次都不合格（{output_format.__name__}）: {last_error}")


__all__ = [
    "CONNECTION_CODES",
    "FORBIDDEN_PLUGIN_MARKERS",
    "INSTALL_HINT",
    "PROMPT_CACHE_KEY_LENGTH",
    "RATE_LIMIT_CODES",
    "SPINE_REQUIRED_CONFIG",
    "STOP_REASON_ALIASES",
    "UNAVAILABLE_CODES",
    "DshLLM",
    "DshRuntimeOptions",
    "DshRuntimePool",
    "HarnessRunResult",
    "RuntimeKey",
    "SupportsHarness",
    "audit_composition",
    "audit_provider_models",
    "close_shared_pool",
    "compose_prompt",
    "default_harness_factory",
    "dsh_credentials_ready",
    "estimate_tokens",
    "failure_of",
    "iter_json_objects",
    "load_composition",
    "parse_last_json",
    "prompt_cache_key",
    "provider_api_key_env",
    "provider_cache_retention",
    "resolved_model",
    "schema_instruction",
    "session_id_for",
    "shared_pool",
    "stable_prefix",
    "tool_schemas_in",
    "usage_from_events",
]
