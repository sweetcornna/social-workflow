"""成本闸门测试。"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from core.budget import BudgetExhausted, BudgetGuard, CostKind, today_key
from core.models import CostLedger
from core.notify import LogNotifier


def test_defaults_come_from_settings(session):
    guard = BudgetGuard(session)
    assert guard.limit_of(CostKind.TOKENS) == 1000.0
    assert guard.limit_of(CostKind.RENDER_SECONDS) == 100.0
    assert guard.day == today_key()


def test_charge_accumulates_and_writes_ledger(session):
    guard = BudgetGuard(session, token_budget=100)
    guard.charge(CostKind.TOKENS, 30, meta={"item": "a"})
    guard.charge(CostKind.TOKENS, 20, meta={"item": "b"})
    session.commit()

    assert guard.used(CostKind.TOKENS) == 50
    assert guard.remaining(CostKind.TOKENS) == 50
    assert guard.is_exhausted(CostKind.TOKENS) is False

    rows = session.scalars(select(CostLedger).order_by(CostLedger.created_at)).all()
    assert [r.amount for r in rows] == [30.0, 20.0]
    assert rows[0].meta == {"item": "a"}
    assert all(r.day == guard.day for r in rows)


def test_charge_over_budget_raises_and_writes_nothing(session):
    guard = BudgetGuard(session, token_budget=100)
    guard.charge(CostKind.TOKENS, 90)
    session.commit()

    with pytest.raises(BudgetExhausted) as exc:
        guard.charge(CostKind.TOKENS, 20)
    assert exc.value.kind == CostKind.TOKENS.value
    assert exc.value.remaining == 10
    assert exc.value.limit == 100

    session.rollback()
    assert guard.used(CostKind.TOKENS) == 90, "超限不得记账"


def test_exact_budget_is_allowed_then_exhausted(session):
    guard = BudgetGuard(session, token_budget=100)
    guard.charge(CostKind.TOKENS, 100)
    session.commit()
    assert guard.is_exhausted(CostKind.TOKENS) is True
    assert guard.remaining(CostKind.TOKENS) == 0
    with pytest.raises(BudgetExhausted):
        guard.charge(CostKind.TOKENS, 1)


def test_kinds_are_independent(session):
    guard = BudgetGuard(session, token_budget=10, render_seconds_budget=60)
    guard.charge(CostKind.TOKENS, 10)
    session.commit()
    assert guard.is_exhausted(CostKind.TOKENS)
    assert not guard.is_exhausted(CostKind.RENDER_SECONDS)
    guard.charge(CostKind.RENDER_SECONDS, 30)
    session.commit()
    assert guard.remaining(CostKind.RENDER_SECONDS) == 30


def test_budget_is_per_day(session):
    yesterday = BudgetGuard(session, token_budget=100, day="2026-08-14")
    today = BudgetGuard(session, token_budget=100, day="2026-08-15")
    yesterday.charge(CostKind.TOKENS, 100)
    session.commit()
    assert yesterday.is_exhausted(CostKind.TOKENS)
    assert today.used(CostKind.TOKENS) == 0
    today.charge(CostKind.TOKENS, 100)  # 不应受昨天影响
    session.commit()


def test_unknown_kind_rejected(session):
    guard = BudgetGuard(session)
    with pytest.raises(ValueError):
        guard.charge("gpu_hours", 1)
    with pytest.raises(ValueError):
        guard.used("gpu_hours")


def test_negative_amount_rejected(session):
    guard = BudgetGuard(session)
    with pytest.raises(ValueError):
        guard.charge(CostKind.TOKENS, -1)


def test_notifies_once_on_exhaustion(session):
    notifier = LogNotifier()
    guard = BudgetGuard(session, token_budget=10, notifier=notifier)
    for _ in range(3):
        with pytest.raises(BudgetExhausted):
            guard.charge(CostKind.TOKENS, 11)
    assert len(notifier.sent) == 1, "同一类型只提醒一次，避免刷屏"
    level, title, _text = notifier.sent[0]
    assert level == "error" and "成本超限" in title


def test_snapshot_shape(session):
    guard = BudgetGuard(session, token_budget=100, render_seconds_budget=60)
    guard.charge(CostKind.TOKENS, 25)
    session.commit()
    snap = guard.snapshot()
    assert set(snap) == {"tokens", "render_seconds", "images"}
    assert snap["tokens"] == {"used": 25.0, "limit": 100.0, "remaining": 75.0}
