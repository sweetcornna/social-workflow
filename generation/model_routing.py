"""dsh 后端按调用职责选择模型与思考档。

路由只认已经登记的生产 ``purpose``。未知 purpose 必须回落到旧的
``SW_DSH_MODEL`` / ``LLM_EFFORT``，这样新增调用点不会被静默分到一个错误档位。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from generation.output_budget import CALL_SITE_BUDGETS

Complexity = Literal["complex", "medium", "low", "legacy"]
RouteEffort = Literal["low", "medium", "high", "xhigh", "max"]

DEFAULT_SOL_MODEL = "gpt-5.6-sol"
DEFAULT_LUNA_MODEL = "gpt-5.6-luna"

COMPLEX_PURPOSES = frozenset({"sourcing.select", "review.semantic", "metrics.insights"})
MEDIUM_PURPOSES = frozenset(
    {
        "xhs.angle",
        "xhs.cards",
        "xhs.note",
        "xhs.dehumanize",
        "wechat.outline",
        "wechat.body",
        "wechat.polish",
        "wechat.dehumanize",
        "douyin.angle",
        "douyin.script",
        "douyin.dehumanize",
    }
)
LOW_PURPOSES = frozenset({"xhs.selfcheck", "wechat.selfcheck", "wechat.meta", "douyin.selfcheck"})

PURPOSE_COMPLEXITY: dict[str, Literal["complex", "medium", "low"]] = {
    **dict.fromkeys(COMPLEX_PURPOSES, "complex"),
    **dict.fromkeys(MEDIUM_PURPOSES, "medium"),
    **dict.fromkeys(LOW_PURPOSES, "low"),
}

COMPLEXITY_EFFORT: dict[Literal["complex", "medium", "low"], RouteEffort] = {
    "complex": "xhigh",
    "medium": "max",
    "low": "max",
}


def tier_models(
    *, sol_model: str, luna_model: str
) -> dict[Literal["complex", "medium", "low"], str]:
    """档位 → 模型。**唯一**一张映射表，路由、preflight 审计与握手都从这里取。

    ``medium`` 与 ``low`` 现在共用 Luna，且 :data:`COMPLEXITY_EFFORT` 给两档的
    effort 也相同——运行时它们完全等价。档位标签本身仍然保留：它进
    :class:`ModelRoute` 的 ``complexity`` 写账本，也是预算表
    （:data:`generation.output_budget.CALL_SITE_BUDGETS`）与覆盖审计的锚点。
    """
    return {"complex": sol_model, "medium": luna_model, "low": luna_model}


@dataclass(frozen=True)
class ModelRoute:
    model: str
    effort: RouteEffort
    complexity: Complexity
    purpose: str


def resolve_model_route(
    purpose: str,
    *,
    enabled: bool,
    legacy_model: str,
    legacy_effort: RouteEffort,
    explicit_effort: RouteEffort | None = None,
    sol_model: str = DEFAULT_SOL_MODEL,
    luna_model: str = DEFAULT_LUNA_MODEL,
) -> ModelRoute:
    """解析一次调用的实际模型与 effort；显式 effort 始终优先。"""
    complexity = PURPOSE_COMPLEXITY.get(purpose)
    if not enabled or complexity is None:
        return ModelRoute(
            model=legacy_model,
            effort=explicit_effort or legacy_effort,
            complexity="legacy",
            purpose=purpose,
        )

    models = tier_models(sol_model=sol_model, luna_model=luna_model)
    return ModelRoute(
        model=models[complexity],
        effort=explicit_effort or COMPLEXITY_EFFORT[complexity],
        complexity=complexity,
        purpose=purpose,
    )


def routing_model_requirements(*, sol_model: str, luna_model: str) -> dict[str, set[str]]:
    """给 preflight 派生模型 → effort 静态审计表；同名模型自动合并要求。

    ``medium`` 与 ``low`` 共用 Luna，于是这里天然合并成一条要求——审计的对象本来
    就是"这个模型要支持哪些 effort"，档位有几个不影响结论。
    """
    requirements: dict[str, set[str]] = {}
    for complexity, model in tier_models(sol_model=sol_model, luna_model=luna_model).items():
        requirements.setdefault(model, set()).add(COMPLEXITY_EFFORT[complexity])
    return requirements


def assert_complete_production_coverage() -> None:
    """预算表与路由表必须精确覆盖同一组生产调用。"""
    purposes = set(CALL_SITE_BUDGETS)
    routed = set(PURPOSE_COMPLEXITY)
    if purposes != routed:
        missing = sorted(purposes - routed)
        extra = sorted(routed - purposes)
        raise ValueError(f"模型路由 purpose 漂移：missing={missing}, extra={extra}")


__all__ = [
    "COMPLEXITY_EFFORT",
    "COMPLEX_PURPOSES",
    "DEFAULT_LUNA_MODEL",
    "DEFAULT_SOL_MODEL",
    "LOW_PURPOSES",
    "MEDIUM_PURPOSES",
    "PURPOSE_COMPLEXITY",
    "ModelRoute",
    "assert_complete_production_coverage",
    "resolve_model_route",
    "routing_model_requirements",
    "tier_models",
]
