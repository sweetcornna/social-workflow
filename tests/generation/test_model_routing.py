"""dsh 多模型 purpose 路由的纯函数契约。"""

from __future__ import annotations

import pytest

from generation.model_routing import (
    COMPLEX_PURPOSES,
    LOW_PURPOSES,
    MEDIUM_PURPOSES,
    PURPOSE_COMPLEXITY,
    assert_complete_production_coverage,
    resolve_model_route,
    routing_model_requirements,
)
from generation.output_budget import CALL_SITE_BUDGETS


@pytest.mark.parametrize(
    ("purposes", "model", "effort", "complexity"),
    [
        (COMPLEX_PURPOSES, "sol", "xhigh", "complex"),
        # medium 与 low 同模型同 effort，只有档位标签不同
        (MEDIUM_PURPOSES, "luna", "max", "medium"),
        (LOW_PURPOSES, "luna", "max", "low"),
    ],
)
def test_every_purpose_maps_to_the_exact_tier(purposes, model, effort, complexity) -> None:
    for purpose in purposes:
        route = resolve_model_route(
            purpose,
            enabled=True,
            legacy_model="legacy",
            legacy_effort="medium",
            sol_model="sol",
            luna_model="luna",
        )
        assert (route.model, route.effort, route.complexity, route.purpose) == (
            model,
            effort,
            complexity,
            purpose,
        )


def test_route_table_exactly_covers_every_production_budget_purpose() -> None:
    assert set(PURPOSE_COMPLEXITY) == set(CALL_SITE_BUDGETS)
    assert not (COMPLEX_PURPOSES & MEDIUM_PURPOSES)
    assert not (COMPLEX_PURPOSES & LOW_PURPOSES)
    assert not (MEDIUM_PURPOSES & LOW_PURPOSES)
    assert_complete_production_coverage()


def test_unknown_purpose_falls_back_to_legacy_model_and_effort() -> None:
    route = resolve_model_route(
        "future.call",
        enabled=True,
        legacy_model="legacy-model",
        legacy_effort="high",
    )
    assert (route.model, route.effort, route.complexity) == ("legacy-model", "high", "legacy")


def test_disabled_switch_preserves_legacy_behavior_for_known_purpose() -> None:
    route = resolve_model_route(
        "sourcing.select",
        enabled=False,
        legacy_model="legacy-model",
        legacy_effort="medium",
    )
    assert (route.model, route.effort, route.complexity) == (
        "legacy-model",
        "medium",
        "legacy",
    )


def test_explicit_effort_overrides_the_tier_default_without_changing_model() -> None:
    route = resolve_model_route(
        "xhs.selfcheck",
        enabled=True,
        legacy_model="legacy-model",
        legacy_effort="medium",
        explicit_effort="high",
        luna_model="luna",
    )
    assert (route.model, route.effort, route.complexity) == ("luna", "high", "low")


def test_preflight_requirements_merge_efforts_when_tiers_share_a_model() -> None:
    assert routing_model_requirements(sol_model="shared", luna_model="shared") == {
        "shared": {"xhigh", "max"}
    }


def test_preflight_requirements_fold_medium_and_low_into_one_luna_entry() -> None:
    """medium 与 low 共用 Luna：静态审计表只该出现一条 Luna 要求，且要求是 ``max``。

    合并本身就是 :func:`routing_model_requirements` 的语义（审计的对象是"模型要支持
    哪些 effort"），所以这里钉住的是"不会漏掉 medium 那一档的要求"。
    """
    assert routing_model_requirements(sol_model="sol", luna_model="luna") == {
        "sol": {"xhigh"},
        "luna": {"max"},
    }


def test_medium_and_low_share_a_runtime_config_but_keep_distinct_labels() -> None:
    """两档运行时完全等价；档位标签必须还在——它进账本，也是预算/覆盖审计的锚点。"""
    kwargs = {
        "enabled": True,
        "legacy_model": "legacy",
        "legacy_effort": "medium",
        "sol_model": "sol",
        "luna_model": "luna",
    }
    medium = resolve_model_route("xhs.note", **kwargs)
    low = resolve_model_route("xhs.selfcheck", **kwargs)

    assert (medium.model, medium.effort) == (low.model, low.effort) == ("luna", "max")
    assert (medium.complexity, low.complexity) == ("medium", "low")
