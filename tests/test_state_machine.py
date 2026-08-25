"""双状态机 + 幂等 + 两阶段发布记录测试。"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.models import Account, ContentItem, PublishRecord, ReviewLog, new_id
from core.state_machine import (
    AccountStatus,
    ContentStatus,
    IllegalTransition,
    PublishPhase,
    ReviewAction,
    SystemAction,
    approve,
    can_transition,
    diff_bundles,
    edit,
    make_idem_key,
    mark_account_needs_relogin,
    publish_with_idempotency,
    reject,
    restore_account,
    slot_of,
    transition,
    transition_account,
)
from publishers.base import (
    ContentBundle,
    FakePublisher,
    NeedsReloginError,
    PermanentError,
    PublishResult,
    RetryableError,
)
from tests.conftest import make_account, make_item

# ------------------------------------------------------------------ 迁移合法性

LEGAL = [
    ("topic", "drafting"),
    ("drafting", "draft"),
    ("draft", "reviewing"),
    ("reviewing", "approved"),
    ("reviewing", "rejected"),
    ("rejected", "drafting"),
    ("approved", "scheduled"),
    ("scheduled", "publishing"),
    ("scheduled", "suspended"),
    ("suspended", "scheduled"),
    ("publishing", "published"),
    ("publishing", "publish_failed"),
    ("published", "measured"),
    ("publish_failed", "retrying"),
    ("publish_failed", "dead_letter"),
    ("retrying", "publishing"),
]

ILLEGAL = [
    ("draft", "published"),  # 跳过审核直接发布
    ("draft", "approved"),  # 必须先进 reviewing
    ("topic", "published"),
    ("approved", "published"),  # 必须先排期再进 publishing
    ("rejected", "approved"),  # 驳回后必须重新改稿走审核
    ("dead_letter", "retrying"),  # 死信是终态
    ("dead_letter", "scheduled"),
    ("published", "publishing"),  # 不允许重复发布
    ("measured", "published"),
    ("scheduled", "dead_letter"),
]


@pytest.mark.parametrize(("src", "dst"), LEGAL)
def test_legal_transitions(session, src, dst):
    account = make_account(session)
    item = make_item(session, account, status=src)
    transition(item, dst)
    assert item.status == dst
    assert can_transition(src, dst)


@pytest.mark.parametrize(("src", "dst"), ILLEGAL)
def test_illegal_transitions(session, src, dst):
    account = make_account(session)
    item = make_item(session, account, status=src)
    with pytest.raises(IllegalTransition) as exc:
        transition(item, dst)
    assert exc.value.from_status == src
    assert exc.value.to_status == dst
    assert item.status == src, "非法迁移不得改变状态"
    assert not can_transition(src, dst)


def test_unknown_status_is_illegal(session):
    account = make_account(session)
    item = make_item(session, account, status="draft")
    with pytest.raises(IllegalTransition):
        transition(item, "不存在的状态")
    assert not can_transition("draft", "不存在的状态")


def test_account_transitions(session):
    account = make_account(session)
    transition_account(account, AccountStatus.DEGRADED)
    assert account.status == "degraded"
    transition_account(account, AccountStatus.NEEDS_RELOGIN)
    transition_account(account, AccountStatus.OK)
    transition_account(account, AccountStatus.BANNED)
    with pytest.raises(IllegalTransition):
        transition_account(account, AccountStatus.OK)  # banned 是人工终态


# ------------------------------------------------------------------- 人工审核


def test_approve_writes_review_log(session):
    account = make_account(session)
    item = make_item(session, account, status="draft")
    approve(session, item, actor="alice", reason="内容 OK")
    session.commit()
    assert item.status == ContentStatus.APPROVED
    logs = session.scalars(select(ReviewLog).where(ReviewLog.content_item_id == item.id)).all()
    assert [(log.actor, log.action) for log in logs] == [("alice", ReviewAction.APPROVE.value)]


def test_reject_stores_reason_in_review_notes(session):
    account = make_account(session)
    item = make_item(session, account, status="draft")
    reject(session, item, actor="bob", reason="标题太夸张")
    session.commit()
    assert item.status == ContentStatus.REJECTED
    assert item.review_notes == "标题太夸张"


def test_reject_requires_reason(session):
    account = make_account(session)
    item = make_item(session, account, status="draft")
    with pytest.raises(ValueError):
        reject(session, item, actor="bob", reason="   ")


def test_edit_records_before_after_diff(session):
    account = make_account(session)
    item = make_item(session, account, status="draft", title="旧标题")
    new_bundle = dict(item.bundle_json)
    new_bundle["title"] = "新标题"
    edit(session, item, actor="carol", new_bundle=new_bundle, reason="标题优化")
    session.commit()
    log = session.scalars(
        select(ReviewLog).where(ReviewLog.action == ReviewAction.EDIT.value)
    ).one()
    assert log.before_json["title"] == "旧标题"
    assert log.after_json["title"] == "新标题"
    text = diff_bundles(log.before_json, log.after_json)
    assert "旧标题" in text and "新标题" in text


# --------------------------------------------------------------------- 幂等键


def test_idem_key_is_stable_and_slot_sensitive():
    a = make_idem_key("acc-1", "xhs", "hash1", "2026-08-15T10:00")
    b = make_idem_key("acc-1", "xhs", "hash1", "2026-08-15T10:00")
    assert a == b and len(a) == 64
    assert a != make_idem_key("acc-1", "xhs", "hash1", "2026-08-15T11:00")
    assert a != make_idem_key("acc-2", "xhs", "hash1", "2026-08-15T10:00")
    assert a != make_idem_key("acc-1", "douyin", "hash1", "2026-08-15T10:00")
    assert a != make_idem_key("acc-1", "xhs", "hash2", "2026-08-15T10:00")


def test_idem_key_unique_constraint(session):
    account = make_account(session)
    item = make_item(session, account)
    session.add(
        PublishRecord(id=new_id("pub"), content_item_id=item.id, idem_key="dup", phase="in_flight")
    )
    session.commit()
    session.add(
        PublishRecord(id=new_id("pub"), content_item_id=item.id, idem_key="dup", phase="in_flight")
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_slot_of_uses_scheduled_at(session):
    account = make_account(session)
    scheduled = make_item(session, account, scheduled_in_minutes=60)
    assert slot_of(scheduled) == scheduled.scheduled_at.isoformat(timespec="minutes")
    immediate = make_item(session, account, scheduled_in_minutes=None)
    assert slot_of(immediate) == ""


# ---------------------------------------------------------------- 幂等发布流程


def test_publish_success_two_phase_record(session, notifier):
    account = make_account(session)
    item = make_item(session, account)
    publisher = FakePublisher(account.id, platform="xhs")

    result = publish_with_idempotency(session, item, publisher, notifier=notifier)
    session.commit()

    assert result.ok and result.platform_post_id
    assert item.status == ContentStatus.PUBLISHED
    record = session.scalars(select(PublishRecord)).one()
    assert record.phase == PublishPhase.DONE
    assert record.platform_post_id == result.platform_post_id
    assert record.url == result.url
    assert record.attempts == 1
    assert len(record.idem_key) == 64
    actions = [
        log.action
        for log in session.scalars(select(ReviewLog).where(ReviewLog.content_item_id == item.id))
    ]
    assert SystemAction.PUBLISH.value in actions


def test_publish_requires_scheduled_or_retrying(session):
    account = make_account(session)
    item = make_item(session, account, status="draft")
    with pytest.raises(IllegalTransition):
        publish_with_idempotency(session, item, FakePublisher(account.id))


def test_dry_run_does_not_touch_state_or_records(session):
    account = make_account(session)
    item = make_item(session, account)
    publisher = FakePublisher(account.id, dry_run=True)

    result = publish_with_idempotency(session, item, publisher)
    session.commit()

    assert result.ok is False
    assert item.status == ContentStatus.SCHEDULED
    assert session.scalars(select(PublishRecord)).all() == []


def test_idem_conflict_goes_through_reconcile(session, notifier):
    """第一次发布回包丢失（抛可重试），重试前对账命中 → 不重复发布。"""
    account = make_account(session)
    item = make_item(session, account)
    hit = PublishResult(ok=True, platform_post_id="p-42", url="https://example.invalid/p-42")
    publisher = FakePublisher(
        account.id,
        raise_exc=RetryableError("网络超时，回包丢失"),
        raise_times=1,
        reconcile_result=hit,
    )

    with pytest.raises(RetryableError):
        publish_with_idempotency(session, item, publisher, notifier=notifier)
    session.commit()
    assert item.status == ContentStatus.RETRYING
    assert publisher.reconcile_calls == 0

    result = publish_with_idempotency(session, item, publisher, notifier=notifier)
    session.commit()

    assert publisher.reconcile_calls == 1
    assert publisher.publish_calls == 1, "对账命中后不得再次调用 publish"
    assert result.platform_post_id == "p-42"
    assert item.status == ContentStatus.PUBLISHED
    records = session.scalars(select(PublishRecord)).all()
    assert len(records) == 1, "同一槽位只能有一条幂等记录"
    assert records[0].phase == PublishPhase.DONE
    assert records[0].platform_post_id == "p-42"
    actions = [log.action for log in session.scalars(select(ReviewLog))]
    assert SystemAction.RECONCILED.value in actions


def test_done_record_short_circuits_without_republish(session):
    """幂等记录已 done 时直接返回，绝不再次触发平台写操作。"""
    account = make_account(session)
    item = make_item(session, account)
    publisher = FakePublisher(account.id)
    first = publish_with_idempotency(session, item, publisher)
    session.commit()

    # 模拟人工/崩溃恢复把状态改回 scheduled，幂等键必须仍然挡住重复发布
    item.status = ContentStatus.SCHEDULED.value
    session.commit()
    again = publish_with_idempotency(session, item, publisher)

    assert publisher.publish_calls == 1
    assert again.ok and again.platform_post_id == first.platform_post_id
    assert again.raw["idempotent_hit"] is True


def test_retryable_counts_up_to_dead_letter(session, notifier):
    account = make_account(session)
    item = make_item(session, account)
    publisher = FakePublisher(account.id, raise_exc=RetryableError("平台 502"))

    for expected_attempt in (1, 2):
        with pytest.raises(RetryableError):
            publish_with_idempotency(session, item, publisher, max_attempts=3, notifier=notifier)
        session.commit()
        record = session.scalars(select(PublishRecord)).one()
        assert record.attempts == expected_attempt
        assert record.phase == PublishPhase.FAILED
        assert item.status == ContentStatus.RETRYING

    with pytest.raises(RetryableError):
        publish_with_idempotency(session, item, publisher, max_attempts=3, notifier=notifier)
    session.commit()

    record = session.scalars(select(PublishRecord)).one()
    assert record.attempts == 3
    assert item.status == ContentStatus.DEAD_LETTER
    assert "RetryableError" in record.last_error
    assert any("死信" in title for _lvl, title, _txt in notifier.sent)
    # 死信后不允许再次发起
    with pytest.raises(IllegalTransition):
        publish_with_idempotency(session, item, publisher, max_attempts=3)


def test_permanent_error_goes_straight_to_dead_letter(session, notifier):
    account = make_account(session)
    item = make_item(session, account)
    publisher = FakePublisher(account.id, raise_exc=PermanentError("内容违规"))

    with pytest.raises(PermanentError):
        publish_with_idempotency(session, item, publisher, max_attempts=5, notifier=notifier)
    session.commit()

    assert item.status == ContentStatus.DEAD_LETTER
    record = session.scalars(select(PublishRecord)).one()
    assert record.phase == PublishPhase.FAILED
    assert record.attempts == 1
    assert "PermanentError" in record.last_error


def test_unknown_exception_is_treated_as_permanent(session):
    account = make_account(session)
    item = make_item(session, account)
    publisher = FakePublisher(account.id, raise_exc=None)
    publisher.raise_exc = RuntimeError("驱动崩了")  # type: ignore[assignment]

    with pytest.raises(RuntimeError):
        publish_with_idempotency(session, item, publisher)
    session.commit()

    assert item.status == ContentStatus.DEAD_LETTER
    assert "RuntimeError" in session.scalars(select(PublishRecord)).one().last_error


# ------------------------------------------ 契约违规兜底（见 docs/RISKS.md 第 13 条）


class _ContractBreakingPublisher(FakePublisher):
    """人为构造的破契约发布器：**非 dry-run 却返回 ok=False**。

    仓库里现成的发布器造不出这种情况，所以那道兜底分支当前不可达——五个发布器
    （FakePublisher 与 xhs / wechat_mp 官方 / wenyan / douyin）所有
    ``PublishResult(ok=False)`` 都在各自的 ``if self.dry_run:`` 里，而
    ``publish_with_idempotency`` 在 ``if publisher.dry_run:`` 处就提前 return 了，
    「返回 ok=False」与「能走到兜底」互斥。要验那道**防御性**告警，只能在测试里
    人为造一个破契约的替身；这个类**只活在测试里**，不许挪进 publishers/。
    """

    def publish(self, bundle: ContentBundle) -> PublishResult:
        self.publish_calls += 1
        assert not self.dry_run, "必须走非 dry-run 路径，否则根本到不了那道兜底"
        return PublishResult(
            ok=False,
            raw={"breach": "returned ok=False instead of raising", "hash": bundle.content_hash},
        )


@pytest.mark.parametrize(
    ("max_attempts", "expected_status"),
    [(3, ContentStatus.RETRYING), (1, ContentStatus.DEAD_LETTER)],
)
def test_contract_violation_is_visible_and_changes_nothing_else(
    session, notifier, caplog, max_attempts, expected_status
):
    """破契约返回 ok=False：必须打 warning + 发通知，且原有行为一字未变。"""
    account = make_account(session)
    item = make_item(session, account)
    publisher = _ContractBreakingPublisher(account.id, platform="xhs")

    with caplog.at_level(logging.WARNING, logger="social_workflow.state_machine"):
        result = publish_with_idempotency(
            session, item, publisher, max_attempts=max_attempts, notifier=notifier
        )
    session.commit()

    # --- 原有行为：状态流转 / 记录 / 返回值一字不动 --------------------------
    assert result.ok is False, "兜底必须原样返回发布器给的 result"
    assert result.raw["breach"] == "returned ok=False instead of raising"
    assert item.status == expected_status
    record = session.scalars(select(PublishRecord)).one()
    assert record.phase == PublishPhase.FAILED
    assert record.attempts == 1
    assert record.last_error == "publisher 返回 ok=False（违反契约，按可重试处理）"
    # 隔壁三个 except 都写审计日志，这道兜底历来不写；本次只补可观测性，不动这一点
    logs = session.scalars(select(ReviewLog).where(ReviewLog.content_item_id == item.id)).all()
    assert logs == []
    assert publisher.publish_calls == 1

    # --- 新增可观测性之一：warning ------------------------------------------
    warnings = [
        rec
        for rec in caplog.records
        if rec.name == "social_workflow.state_machine" and rec.levelno == logging.WARNING
    ]
    assert len(warnings) == 1, "契约违规必须打且只打一条 state_machine warning"
    msg = warnings[0].getMessage()
    # 措辞要让读日志的人一眼分清「契约被改坏」与「一次普通发布失败」
    assert "违反 publish 契约" in msg
    assert "不是一次普通发布失败" in msg
    assert "ok=False" in msg and "PublishError" in msg
    # 定位信息：哪个发布器、哪条内容、哪个号、哪个平台、落到了哪个状态
    assert "_ContractBreakingPublisher" in msg
    assert item.id in msg and f"account={account.id}" in msg and "platform=xhs" in msg
    assert f"attempts=1/{max_attempts}" in msg
    assert f"status={expected_status.value}" in msg

    # --- 新增可观测性之二：通知 ---------------------------------------------
    breaches = [(lvl, title, text) for lvl, title, text in notifier.sent if "契约违规" in title]
    assert len(breaches) == 1, "契约违规必须发且只发一条通知"
    level, title, text = breaches[0]
    assert level == "error", "与隔壁死信 / 发布失败两条通知同级"
    assert item.id in title
    assert "_ContractBreakingPublisher" in text
    assert "不是一次普通发布失败" in text
    assert expected_status.value in text


def test_contract_violation_warning_is_distinguishable_from_ordinary_failure(session, notifier):
    """同一条内容：普通发布失败与契约违规必须给出**不同**的告警面貌。

    普通失败抛异常、通知标题是 ``[死信]`` / ``[发布失败]``、正文带异常类名；
    契约违规不抛异常、标题是 ``[契约违规]``、正文点名发布器实现坏了。
    两者混在一起时，值班的人靠标题就能分开。
    """
    account = make_account(session)
    ordinary = make_item(session, account, title="普通失败")
    with pytest.raises(PermanentError):
        publish_with_idempotency(
            session,
            ordinary,
            FakePublisher(account.id, raise_exc=PermanentError("内容违规")),
            notifier=notifier,
        )
    session.commit()

    breaking = make_item(session, account, title="破契约")
    publish_with_idempotency(
        session, breaking, _ContractBreakingPublisher(account.id), notifier=notifier
    )
    session.commit()

    titles = [title for _lvl, title, _txt in notifier.sent]
    assert any(t.startswith("[发布失败]") for t in titles)
    assert any(t.startswith("[契约违规]") for t in titles)
    ordinary_text = next(txt for _lvl, t, txt in notifier.sent if t.startswith("[发布失败]"))
    breach_text = next(txt for _lvl, t, txt in notifier.sent if t.startswith("[契约违规]"))
    assert "PermanentError" in ordinary_text and "契约" not in ordinary_text
    assert "契约" in breach_text and "PermanentError" not in breach_text


# ------------------------------------------------------- 账号级挂起 / 恢复


def test_needs_relogin_suspends_scheduled_items(session, notifier):
    account = make_account(session)
    failing = make_item(session, account, title="触发失效的那条")
    other = make_item(session, account, title="同账号另一条排期")
    another_account = make_account(session, account_id="acc-2")
    unaffected = make_item(session, another_account, title="别的账号，不该被挂起")
    session.commit()

    publisher = FakePublisher(account.id, raise_exc=NeedsReloginError("cookie 失效"))
    with pytest.raises(NeedsReloginError):
        publish_with_idempotency(session, failing, publisher, notifier=notifier)
    session.commit()

    assert account.status == AccountStatus.NEEDS_RELOGIN
    assert failing.status == ContentStatus.RETRYING, "登录失效不该把内容打成死信"
    assert other.status == ContentStatus.SUSPENDED
    assert other.prev_status == ContentStatus.SCHEDULED.value
    assert unaffected.status == ContentStatus.SCHEDULED
    assert any("需重登" in title for _lvl, title, _txt in notifier.sent)

    restored = restore_account(session, account, notifier=notifier)
    session.commit()
    assert account.status == AccountStatus.OK
    assert [i.id for i in restored] == [other.id]
    assert other.status == ContentStatus.SCHEDULED
    assert other.prev_status is None


def test_needs_relogin_does_not_burn_retry_budget(session):
    account = make_account(session)
    item = make_item(session, account)
    publisher = FakePublisher(account.id, raise_exc=NeedsReloginError("cookie 失效"))
    for _ in range(4):
        with pytest.raises(NeedsReloginError):
            publish_with_idempotency(session, item, publisher, max_attempts=3)
        session.commit()
        # 账号已 needs_relogin，重复调用不应再迁移账号状态
        assert account.status == AccountStatus.NEEDS_RELOGIN
        item.status = ContentStatus.RETRYING.value
    assert item.status != ContentStatus.DEAD_LETTER


def test_mark_needs_relogin_is_idempotent(session, notifier):
    account = make_account(session)
    make_item(session, account)
    session.commit()
    first = mark_account_needs_relogin(session, account, detail="第一次", notifier=notifier)
    second = mark_account_needs_relogin(session, account, detail="第二次", notifier=notifier)
    assert len(first) == 1 and second == []
    assert account.status == AccountStatus.NEEDS_RELOGIN


def test_suspended_items_are_invisible_to_publish(session):
    account = make_account(session)
    item = make_item(session, account)
    session.commit()
    mark_account_needs_relogin(session, account, detail="失效")
    session.commit()
    assert item.status == ContentStatus.SUSPENDED
    with pytest.raises(IllegalTransition):
        publish_with_idempotency(session, item, FakePublisher(account.id))


def test_records_survive_across_sessions(session):
    """两阶段记录必须真的落库，而不是只活在内存对象里。"""
    account = make_account(session)
    item = make_item(session, account)
    publisher = FakePublisher(account.id)
    publish_with_idempotency(session, item, publisher)
    session.commit()
    item_id = item.id
    session.close()

    from core import db

    with db.session_scope() as fresh:
        reloaded = fresh.get(ContentItem, item_id)
        assert reloaded is not None
        assert reloaded.status == ContentStatus.PUBLISHED
        record = fresh.scalars(
            select(PublishRecord).where(PublishRecord.content_item_id == item_id)
        ).one()
        assert record.phase == PublishPhase.DONE
        assert fresh.get(Account, account.id).status == AccountStatus.OK
