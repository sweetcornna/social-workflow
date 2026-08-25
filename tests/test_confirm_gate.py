"""发布前人工确认闸门 + autopilot + 通知节流（P12）。

这一层是**合规底线**：小红书 2026-03 公告封禁完全 AI 驱动的无人值守账号，所以
"发布前要人点一下"这道闸门不许有旁路。这个文件盯的就是"旁路一个都没有"。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from core import db
from core.accounts import policy_of
from core.confirm import (
    AUTOPILOT_ACTOR,
    MACHINE_REVIEW_ACTION,
    ConfirmConflict,
    autopilot_approve,
    confirm_item,
    confirm_view,
    handle_telegram_decision,
    humanize_delta,
    reject_confirmation,
    run_confirm_gate,
)
from core.models import Account, ContentItem, ReviewLog
from core.notify import NOTIFY_THROTTLE, LogNotifier, notify_event
from core.scheduler import tick_scheduled_publish
from core.state_machine import ContentStatus
from core.telegram import ACTION_CONFIRM, ACTION_REJECT, set_telegram_channel
from publishers.registry import use_fake_publishers
from tests.conftest import make_account, make_item

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


class FakeChannel:
    """替身 Telegram 通道：不出网，只记下推了什么。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.cards: list = []
        self.edits: list[tuple[str, str]] = []
        self.messages: list[tuple[str, str, str]] = []
        self.fail = fail

    def send(self, title: str, text: str, level: str = "info") -> bool:
        self.messages.append((level, title, text))
        return True

    def send_confirm_card(self, card, *, cover=None):
        if self.fail:
            return None
        self.cards.append(card)
        return f"12345:{len(self.cards)}:text"

    def edit_card(self, ref: str, text: str) -> bool:
        self.edits.append((ref, text))
        return True

    def answer(self, callback_query_id: str, text: str) -> None:  # pragma: no cover
        return None


@pytest.fixture
def channel() -> FakeChannel:
    ch = FakeChannel()
    set_telegram_channel(ch)  # type: ignore[arg-type]
    return ch


def seed(*, extra: dict | None = None, status: str = "scheduled", minutes: int = -5):
    """造一个"到点待发"的现场，返回 ``(account_id, item_id)``。"""
    with db.session_scope() as session:
        account = make_account(session, extra=extra or {})
        item = make_item(session, account, status=status, scheduled_in_minutes=minutes, now=NOW)
        return account.id, item.id


def load(item_id: str) -> ContentItem:
    with db.session_scope() as session:
        item = session.get(ContentItem, item_id)
        assert item is not None
        session.expunge(item)
        return item


# ----------------------------------------------------------- 人工确认闸门


def test_scheduled_item_is_not_published_until_someone_confirms(channel):
    """默认 confirm_required=true：到点了也不发，统计里记在 skipped_unconfirmed。"""
    use_fake_publishers()
    _, item_id = seed()

    stats = tick_scheduled_publish(now=NOW)

    assert stats["published"] == 0
    assert stats["skipped"] == 1 and stats["skipped_unconfirmed"] == 1
    assert load(item_id).status == ContentStatus.SCHEDULED.value


def test_the_fifth_gate_pushes_a_card_when_the_slot_arrives_unconfirmed(channel):
    """内容不该静默停住：到点还没推过卡就补推一次，人才知道系统在等他。"""
    use_fake_publishers()
    _, item_id = seed()

    tick_scheduled_publish(now=NOW)

    assert len(channel.cards) == 1
    assert load(item_id).confirm_pushed_at is not None


def test_the_fifth_gate_does_not_push_twice_for_the_same_item(channel):
    use_fake_publishers()
    seed()
    tick_scheduled_publish(now=NOW)
    tick_scheduled_publish(now=NOW + timedelta(minutes=1))
    assert len(channel.cards) == 1


def test_confirmed_item_goes_out(channel):
    use_fake_publishers()
    _, item_id = seed()
    with db.session_scope() as session:
        confirm_item(session, session.get(ContentItem, item_id), actor="operator", now=NOW)

    stats = tick_scheduled_publish(now=NOW)

    assert stats["published"] == 1 and stats["skipped_unconfirmed"] == 0
    assert load(item_id).status == ContentStatus.PUBLISHED.value


def test_autopilot_does_not_open_the_publish_gate(channel):
    """红线：``autopilot`` 只影响"自动批准"，**不影响"发布前要人点"**。"""
    use_fake_publishers()
    _, _item_id = seed(extra={"autopilot": True})

    stats = tick_scheduled_publish(now=NOW)

    assert stats["published"] == 0 and stats["skipped_unconfirmed"] == 1


def test_confirm_required_false_lets_the_account_publish_unattended(channel):
    use_fake_publishers()
    seed(extra={"confirm_required": False})
    assert tick_scheduled_publish(now=NOW)["published"] == 1


def test_a_typo_in_confirm_required_keeps_the_gate_closed(channel):
    """安全侧的默认值不该被拼错关掉：认不出来的值一律按"要确认"处理。"""
    use_fake_publishers()
    seed(extra={"confirm_required": "flase"})
    assert tick_scheduled_publish(now=NOW)["skipped_unconfirmed"] == 1


def test_the_gate_sits_after_the_window_gate(channel):
    """闸门顺序不能换：窗口这种更便宜的判定要先跑，别为发不出去的内容推卡打扰人。"""
    use_fake_publishers()
    with db.session_scope() as session:
        account = make_account(
            session, extra={"publish_windows": ["09:00-11:00"], "timezone": "UTC"}
        )
        make_item(session, account, scheduled_in_minutes=-5, now=NOW)

    stats = tick_scheduled_publish(now=NOW)  # 12:00 UTC，窗口外

    assert stats["skipped_window"] == 1 and stats["skipped_unconfirmed"] == 0
    assert channel.cards == []


# ------------------------------------------------------------------ 确认动作


def test_confirming_twice_is_refused(channel):
    """一条内容只认第一次有效点击（重放 / 双击 / 两个门面同时点）。"""
    _, item_id = seed()
    with db.session_scope() as session:
        confirm_item(session, session.get(ContentItem, item_id), actor="tg:1", now=NOW)
    with db.session_scope() as session, pytest.raises(ConfirmConflict):
        confirm_item(session, session.get(ContentItem, item_id), actor="tg:1", now=NOW)


def test_confirm_writes_an_audit_trail_naming_who_pressed_it(channel):
    """合规上"这条到底谁点的"必须可追溯。"""
    _, item_id = seed()
    with db.session_scope() as session:
        confirm_item(session, session.get(ContentItem, item_id), actor="tg:889", now=NOW)
    with db.session_scope() as session:
        log = session.scalars(
            select(ReviewLog).where(
                ReviewLog.content_item_id == item_id, ReviewLog.action == "confirm"
            )
        ).one()
        assert log.actor == "tg:889"


def test_rejecting_releases_the_slot_and_feeds_the_rewrite_agent(channel):
    _, item_id = seed()
    with db.session_scope() as session:
        reject_confirmation(
            session, session.get(ContentItem, item_id), actor="tg:1", reason="标题太标题党"
        )
    item = load(item_id)
    assert item.status == ContentStatus.REJECTED.value
    assert item.scheduled_at is None, "驳回要把排期槽位让出来"
    assert item.review_notes == "标题太标题党"


# ------------------------------------------------------- Telegram 回调闭环


def test_telegram_confirm_callback_confirms_and_rewrites_the_card(channel):
    _, item_id = seed()
    answer, card_text = handle_telegram_decision(ACTION_CONFIRM, item_id, "tg:12345")
    assert "已确认" in answer or "到点就发" in answer
    assert card_text.startswith("<b>测试标题</b>"), "标题保持不变，只换状态行"
    assert "已确认" in card_text
    assert load(item_id).confirmed_at is not None


def test_telegram_reject_callback_says_it_went_back_to_the_rewrite_agent(channel):
    _, item_id = seed()
    _answer, card_text = handle_telegram_decision(ACTION_REJECT, item_id, "tg:12345")
    assert "已驳回 · 已回写给改稿 Agent" in card_text
    assert load(item_id).status == ContentStatus.REJECTED.value


def test_replaying_the_same_callback_changes_nothing(channel):
    """重放：第二次点只会拿到一句"已经处理过了"，不会再动内容。"""
    _, item_id = seed()
    handle_telegram_decision(ACTION_CONFIRM, item_id, "tg:12345")
    first = load(item_id).confirmed_at

    answer, card_text = handle_telegram_decision(ACTION_CONFIRM, item_id, "tg:12345")

    assert "确认过了" in answer
    assert card_text == "", "重放不该再改一次卡片"
    assert load(item_id).confirmed_at == first


def test_rejecting_after_confirming_is_refused(channel):
    _, item_id = seed()
    handle_telegram_decision(ACTION_CONFIRM, item_id, "tg:12345")
    answer, _ = handle_telegram_decision(ACTION_REJECT, item_id, "tg:12345")
    assert "确认过了" in answer
    assert load(item_id).status == ContentStatus.SCHEDULED.value


def test_callback_for_a_deleted_item_is_answered_not_crashed(channel):
    answer, card = handle_telegram_decision(ACTION_CONFIRM, "itm_gone", "tg:12345")
    assert "找不到" in answer and card == ""


# ------------------------------------------------------------ 巡检 / 提醒 / TTL


def test_confirm_gate_pushes_a_card_well_before_the_slot(channel):
    """离槽位还有几小时就推，人才有充足时间点。"""
    _, _item_id = seed(minutes=240)
    stats = run_confirm_gate(now=NOW, channel=channel)
    assert stats["pushed"] == 1
    assert channel.cards[0].reminder is False


def test_confirm_gate_reminds_once_near_the_slot(channel):
    _, _item_id = seed(minutes=240)
    run_confirm_gate(now=NOW, channel=channel)

    near = NOW + timedelta(minutes=225)  # 槽位前 15 分钟
    assert run_confirm_gate(now=near, channel=channel)["reminded"] == 1
    assert channel.cards[-1].reminder is True
    # 只提醒一次
    assert run_confirm_gate(now=near + timedelta(minutes=1), channel=channel)["reminded"] == 0


def test_confirm_gate_auto_rejects_after_the_ttl(channel):
    """推了卡没人点不许无限堆积：TTL 到了自动驳回，槽位让出来。"""
    _, item_id = seed(minutes=60)
    run_confirm_gate(now=NOW, channel=channel)

    stats = run_confirm_gate(now=NOW + timedelta(hours=25), channel=channel)

    assert stats["expired"] == 1
    item = load(item_id)
    assert item.status == ContentStatus.REJECTED.value and item.scheduled_at is None
    assert "已超时自动驳回" in channel.edits[-1][1]


def test_ttl_is_configurable_per_account(channel):
    _, _item_id = seed(minutes=30, extra={"confirm_ttl_hours": 2})
    run_confirm_gate(now=NOW, channel=channel)
    assert run_confirm_gate(now=NOW + timedelta(hours=1), channel=channel)["expired"] == 0
    assert run_confirm_gate(now=NOW + timedelta(hours=3), channel=channel)["expired"] == 1


def test_items_pile_up_no_longer_than_the_ttl_even_without_a_channel(notifier):
    """Telegram 没配也要有出口：TTL 从排期时刻起算，不会永远堆着。"""
    set_telegram_channel(None)
    _, _item_id = seed(minutes=0)
    assert run_confirm_gate(now=NOW, notifier=notifier)["waiting"] == 1
    assert run_confirm_gate(now=NOW + timedelta(hours=25), notifier=notifier)["expired"] == 1


def test_confirm_gate_leaves_offline_accounts_alone(channel):
    """号掉线时催人确认没有意义——确认了也发不出去。"""
    with db.session_scope() as session:
        account = make_account(session, status="needs_relogin")
        make_item(session, account, scheduled_in_minutes=60, now=NOW)
    assert run_confirm_gate(now=NOW, channel=channel)["skipped_account"] == 1
    assert channel.cards == []


def test_confirm_gate_skips_accounts_that_do_not_require_confirmation(channel):
    seed(minutes=60, extra={"confirm_required": False})
    assert run_confirm_gate(now=NOW, channel=channel)["skipped_not_required"] == 1


def test_a_failed_push_is_retried_next_round(channel):
    """推送失败不写 confirm_pushed_at：下一轮还会再试，TTL 也不从一次没送达的推送开始算。"""
    failing = FakeChannel(fail=True)
    _, item_id = seed(minutes=60)
    assert run_confirm_gate(now=NOW, channel=failing)["waiting"] == 1
    assert load(item_id).confirm_pushed_at is None
    assert run_confirm_gate(now=NOW + timedelta(minutes=1), channel=channel)["pushed"] == 1


def test_workbench_fallback_confirm_works_without_telegram(client):
    """Telegram 不能是单点：工作台里点确认走的是同一个函数。"""
    set_telegram_channel(None)
    _, item_id = seed()

    resp = client.post(f"/api/v1/content/{item_id}/confirm", json={"actor": "operator"})

    assert resp.status_code == 200
    assert load(item_id).confirmed_at is not None
    again = client.post(f"/api/v1/content/{item_id}/confirm", json={})
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "confirm_conflict"


def test_workbench_fallback_reject_releases_the_slot(client):
    set_telegram_channel(None)
    _, item_id = seed()
    resp = client.post(f"/api/v1/content/{item_id}/reject", json={"reason": "不想发这条"})
    assert resp.status_code == 200
    assert load(item_id).status == ContentStatus.REJECTED.value


def test_content_row_carries_both_moments_for_the_dual_readout(client, channel):
    """工作台那处双时刻读数要的两个数：发布槽位与决定期限。"""
    _, item_id = seed(minutes=120)
    run_confirm_gate(now=NOW, channel=channel)

    row = client.get(f"/api/v1/content/{item_id}").json()["data"]["item"]

    assert row["confirm_required"] is True and row["awaiting_confirm"] is True
    assert row["scheduled_at"] and row["confirm_deadline"]
    assert row["confirm_deadline"] != row["scheduled_at"], "决定期限和发布时刻是两个时刻"


def test_confirm_view_has_no_deadline_when_confirmation_is_off():
    with db.session_scope() as session:
        account = make_account(session, extra={"confirm_required": False})
        item = make_item(session, account, scheduled_in_minutes=60, now=NOW)
        view = confirm_view(item, account)
    assert view.required is False and view.awaiting is False and view.deadline is None


@pytest.mark.parametrize(
    "delta,text",
    [
        (timedelta(hours=3, minutes=40), "3 小时 40 分"),
        (timedelta(hours=2), "2 小时"),
        (timedelta(minutes=28), "28 分钟"),
        (timedelta(seconds=-10), "0 分钟"),
    ],
)
def test_humanize_delta_reads_like_a_person_would_say_it(delta, text):
    assert humanize_delta(delta) == text


# ------------------------------------------------------------------ autopilot


def _clean_item(session, account, *, blocking: int = 0, warnings: int = 0):
    item = make_item(session, account, status="draft", scheduled_in_minutes=None)
    session.add(
        ReviewLog(
            id=f"rvl_{item.id}",
            content_item_id=item.id,
            actor="system",
            action=MACHINE_REVIEW_ACTION,
            after_json={"passed": not blocking, "blocking": blocking, "warnings": warnings},
        )
    )
    session.flush()
    return item


def test_autopilot_approves_and_schedules_clean_content(channel):
    with db.session_scope() as session:
        account = make_account(session, extra={"autopilot": True})
        item = _clean_item(session, account)
        outcome = autopilot_approve(
            session, item, policy=policy_of(account), blocking=0, warnings=0, now=NOW
        )
    assert outcome.approved and outcome.scheduled
    assert load(item.id).status == ContentStatus.SCHEDULED.value
    assert len(channel.cards) == 1, "自动批准 + 排期成功要推一次确认卡"


def test_autopilot_records_who_approved_it(channel):
    """审计日志必须能看出是机器批的，不是人批的。"""
    with db.session_scope() as session:
        account = make_account(session, extra={"autopilot": True})
        item = _clean_item(session, account)
        autopilot_approve(session, item, policy=policy_of(account), blocking=0, warnings=0, now=NOW)
        item_id = item.id
    with db.session_scope() as session:
        approve_log = session.scalars(
            select(ReviewLog).where(
                ReviewLog.content_item_id == item_id, ReviewLog.action == "approve"
            )
        ).one()
        assert approve_log.actor == AUTOPILOT_ACTOR


@pytest.mark.parametrize("blocking,warnings", [(1, 0), (0, 1), (2, 3)])
def test_autopilot_never_approves_content_with_findings(blocking, warnings, channel, notifier):
    """block / warn 恰恰是需要人判断的地方，机器替人放行等于把审核这层白做了。"""
    with db.session_scope() as session:
        account = make_account(session, extra={"autopilot": True})
        item = _clean_item(session, account, blocking=blocking, warnings=warnings)
        outcome = autopilot_approve(
            session,
            item,
            policy=policy_of(account),
            blocking=blocking,
            warnings=warnings,
            notifier=notifier,
            now=NOW,
        )
        item_id = item.id
    assert not outcome.approved
    assert load(item_id).status == ContentStatus.DRAFT.value
    assert any("待人工审核" in title for _l, title, _t in notifier.sent)


def test_autopilot_off_means_nothing_is_auto_approved(channel):
    with db.session_scope() as session:
        account = make_account(session)  # 默认 autopilot=false
        item = _clean_item(session, account)
        outcome = autopilot_approve(
            session, item, policy=policy_of(account), blocking=0, warnings=0, now=NOW
        )
    assert not outcome.approved


def test_machine_review_action_literal_does_not_drift():
    """三处字面量副本（review.pipeline / core.api.rows / core.confirm）必须一致。"""
    from core.api.rows import MACHINE_REVIEW_ACTION as ROWS_ACTION
    from review.pipeline import MACHINE_REVIEW_ACTION as SOURCE_ACTION

    assert MACHINE_REVIEW_ACTION == ROWS_ACTION == SOURCE_ACTION


# ------------------------------------------------------------------ 通知节流


def test_the_same_event_is_pushed_once_per_window():
    """登录巡检每 10 分钟一轮，不节流的话一天上百条——用户会直接把 bot 静音。"""
    sink = LogNotifier()
    for _ in range(5):
        notify_event(
            "[需重登] acc-1", "去扫码", kind="needs_relogin", account_id="acc-1", notifier=sink
        )
    assert len(sink.sent) == 1


def test_the_window_eventually_reopens():
    sink = LogNotifier()
    kwargs = {"kind": "needs_relogin", "account_id": "acc-1", "notifier": sink}
    notify_event("标题", "正文", now=NOW, **kwargs)
    notify_event("标题", "正文", now=NOW + timedelta(minutes=119), **kwargs)
    notify_event("标题", "正文", now=NOW + timedelta(minutes=121), **kwargs)
    assert len(sink.sent) == 2


def test_throttling_is_per_account_and_per_event_kind():
    """acc-1 掉线不该把 acc-2 的掉线提醒也吞掉。"""
    sink = LogNotifier()
    notify_event("a", "x", kind="needs_relogin", account_id="acc-1", notifier=sink)
    notify_event("b", "x", kind="needs_relogin", account_id="acc-2", notifier=sink)
    notify_event("c", "x", kind="budget_tokens", account_id="acc-1", notifier=sink)
    assert len(sink.sent) == 3


def test_login_health_does_not_renotify_every_round(notifier):
    """掉线巡检的实战形态：连跑几轮只推一条。"""
    from core.state_machine import mark_account_needs_relogin

    with db.session_scope() as session:
        account = make_account(session, status="ok")
        for _ in range(3):
            account.status = "ok"
            mark_account_needs_relogin(session, account, detail="cookie 过期", notifier=notifier)
    relogin = [t for _l, t, _x in notifier.sent if "需重登" in t]
    assert len(relogin) == 1


def test_reset_clears_the_throttle_state():
    sink = LogNotifier()
    notify_event("a", "x", kind="k", account_id="acc-1", notifier=sink)
    NOTIFY_THROTTLE.reset()
    notify_event("a", "x", kind="k", account_id="acc-1", notifier=sink)
    assert len(sink.sent) == 2


# ------------------------------------------------------------------ 老库迁移


def test_a_legacy_database_gets_the_new_columns(tmp_path):
    """``create_all`` 对已存在的表是 no-op：老库不补列，启动后第一次查询就崩。"""
    import sqlite3

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE content_items (id VARCHAR(64) PRIMARY KEY, account_id VARCHAR(64), "
        "status VARCHAR(24), bundle_json JSON, created_at DATETIME, updated_at DATETIME)"
    )
    conn.commit()
    conn.close()

    db.configure(f"sqlite:///{path}")
    try:
        added = db.ensure_columns()
        assert set(added) == {
            "content_items.confirmed_at",
            "content_items.confirm_ref",
            "content_items.confirm_pushed_at",
        }
        assert db.ensure_columns() == [], "补列必须幂等"
    finally:
        db.configure()


# ------------------------------------------------------------------ 台账开关


def test_the_two_switches_round_trip_through_accounts_yaml(tmp_path, monkeypatch):
    from core.accounts import load_specs, sync_accounts

    ledger = tmp_path / "accounts.yaml"
    ledger.write_text(
        "accounts:\n"
        "  - id: acc-auto\n"
        "    platform: xhs\n"
        "    name: 自动号\n"
        "    autopilot: true\n"
        "    confirm_required: false\n"
        "    confirm_ttl_hours: 6\n",
        encoding="utf-8",
    )
    with db.session_scope() as session:
        sync_accounts(session, load_specs(ledger))
    with db.session_scope() as session:
        policy = policy_of(session.get(Account, "acc-auto"))
    assert policy.autopilot is True
    assert policy.confirm_required is False
    assert policy.confirm_ttl_hours == 6


def test_patching_an_account_can_turn_autopilot_on_and_confirm_off(client, tmp_path):
    created = client.post(
        "/api/v1/accounts",
        json={"platform": "xhs", "name": "自动号", "daily_target": 1},
    )
    assert created.status_code == 201, created.text
    account_id = created.json()["data"]["account"]["id"]

    resp = client.patch(
        f"/api/v1/accounts/{account_id}",
        json={"autopilot": True, "confirm_required": False},
    )

    assert resp.status_code == 200, resp.text
    policy = resp.json()["data"]["account"]["policy"]
    assert policy["autopilot"] is True and policy["confirm_required"] is False
    # 关掉之后还要能再打开——布尔开关最容易在"没传"与"传了 false"之间出错
    again = client.patch(f"/api/v1/accounts/{account_id}", json={"confirm_required": True})
    assert again.json()["data"]["account"]["policy"]["confirm_required"] is True


def test_patching_something_else_does_not_silently_reset_the_switches(client):
    created = client.post("/api/v1/accounts", json={"platform": "xhs", "name": "自动号"})
    account_id = created.json()["data"]["account"]["id"]
    client.patch(f"/api/v1/accounts/{account_id}", json={"autopilot": True})

    resp = client.patch(f"/api/v1/accounts/{account_id}", json={"daily_target": 2})

    assert resp.json()["data"]["account"]["policy"]["autopilot"] is True


# --------------------------------------------------------------- 系统页状态


def test_system_telegram_endpoint_explains_what_to_do_when_unconfigured(client):
    body = client.get("/api/v1/system/telegram").json()["data"]
    assert body["ready"] is False
    assert body["detail"], "不可用时必须给一句能照着做的话，不能只说「未配置」"
    assert "token" not in json_dump(body).lower() or "TELEGRAM_BOT_TOKEN" in body["detail"]


def json_dump(payload: dict) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False)


def test_system_telegram_endpoint_never_leaks_the_token(client, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "4242424242:SUPERSECRETVALUE")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
    from core.config import reload_settings

    reload_settings()

    body = client.get("/api/v1/system/telegram").json()["data"]

    assert "SUPERSECRETVALUE" not in json_dump(body)
    assert body["configured"] is True and body["ready"] is True
