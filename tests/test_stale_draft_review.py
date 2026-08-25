"""入库了却从来没跑过机器审核的 draft 必须有出口（P16.3）。

背景：``core/dev_flow.py`` 的 ``_persist_and_review`` 在跑机器审核**之前**把 draft
commit 掉——为了把写锁交出去（审核里有一次 ``llm_timeout_seconds=600`` 的 LLM 调用），
也为了别把已经烧过 token 的稿子跟着审核异常一起回滚。代价是审核中途崩掉会留下一条
``review_notes`` 为空的 draft。

它和上一轮的 ``publishing`` 残留**不是同一种严重程度**，这里如实写清楚：
``REVIEW_QUEUE_STATUSES`` 含 ``draft``，所以它**会**出现在审核台、也会计进
``pending_review``；但它没有任何机器结论可看，autopilot 只在 ``tick_generate`` 那一轮
里动它、之后永远不会再碰，也没有任何东西报警。``recover_stale_drafts`` 是它的出口。

本文件钉四件事：

1. 崩溃残留**确实会产生**，而且除了这个 sweeper 没人来填（反证）；
2. 补跑之后内容回到正常的人工审核流，且**幂等**（补过的不会被反复补）；
3. 补跑**不出网**——这个 sweeper 跑在一个 DB 事务里，往里塞 LLM 调用等于把这一整轮
   改动修掉的 bug 原样种回来；
4. 阈值内正在审的、以及审过的，一根手指都不许碰。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from core import db
from core.models import ContentItem, ReviewLog, Topic, new_id
from core.scheduler import (
    STALE_REVIEW_MIN_SECONDS,
    produced_today,
    recover_stale_drafts,
    stale_review_after,
    tick_generate,
    tick_retry_sweep,
)
from core.state_machine import ContentStatus, SystemAction
from tests.conftest import make_account, make_item

MACHINE_REVIEW = "machine_review"


def _seed_account(account_id: str, *, platform: str = "xhs") -> None:
    with db.session_scope() as session:
        account = make_account(session, account_id=account_id, platform=platform)
        account.extra = {"daily_target": 1}
        for index in range(5):
            session.add(
                Topic(
                    id=new_id("tp"),
                    source="newsnow",
                    title=f"候选选题 {index}",
                    score=1.0 - index * 0.1,
                    raw={},
                )
            )


def _crash_during_review(monkeypatch, notifier, *, account_id: str = "acc-draft") -> str:
    """真跑一轮 ``tick_generate`` 并在机器审核中途"崩掉"，返回残留 draft 的 id。

    崩溃用 ``SystemExit``（``BaseException``）演：它绕开 ``tick_generate`` 的
    ``except Exception``，谁都来不及收拾——被 ``kill`` / OOM / 断电就是这样。
    不手工造状态：手工塞一条 ``review_notes`` 为空的 draft 证明不了"真实崩溃会留下
    这个形状"，而这正是整个文件要证的前提。
    """
    from core.config import reload_settings

    monkeypatch.setenv("SW_GENERATE_MAKE_MEDIA", "false")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")  # 没 key → ScriptedLLM，不出网
    monkeypatch.setenv("DAILY_TOKEN_BUDGET", "1000000")
    reload_settings()
    _seed_account(account_id)

    def dying_review(bundle, **kwargs):
        raise SystemExit("进程在机器审核中途没了")

    monkeypatch.setattr("review.pipeline.review", dying_review)
    with pytest.raises(SystemExit):
        tick_generate(account_ids=[account_id], notifier=notifier)
    monkeypatch.undo()
    reload_settings()

    with db.session_scope() as session:
        items = session.scalars(
            select(ContentItem).where(ContentItem.account_id == account_id)
        ).all()
        assert len(items) == 1, f"没造出崩溃残留（{len(items)} 条），后面全白测"
        item = items[0]
        assert item.status == ContentStatus.DRAFT.value
        assert not item.review_notes, "残留应该是'没有任何机器结论'的那种 draft"
        return item.id


def _reload(item_id: str) -> ContentItem:
    with db.session_scope() as session:
        item = session.get(ContentItem, item_id)
        assert item is not None
        session.expunge(item)
        return item


# --------------------------------------------------------------------- 阈值


def test_threshold_is_larger_than_the_review_llm_timeout():
    """阈值不能小于机器审核里最慢的那一步，否则会把**正在审**的稿子判成漏审。"""
    from core.config import get_settings

    configured = float(get_settings().llm_timeout_seconds)
    assert stale_review_after().total_seconds() > configured
    assert stale_review_after().total_seconds() >= STALE_REVIEW_MIN_SECONDS


def test_threshold_keeps_a_real_multiple_of_the_llm_timeout(monkeypatch):
    """余量必须来自**倍数**，不能靠下限凑巧顶上。

    上一轮在发布侧踩过这个坑：``max(x * factor, floor)`` 的两项谁都能单独盖过 x，
    于是"倍数被人改成 1"没有任何用例看得见。这里把 LLM 超时抬到下限完全够不着的
    量级，逼出倍数本身。
    """
    from core.config import reload_settings

    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "3600")
    reload_settings()
    assert stale_review_after() >= timedelta(seconds=3600 * 2), (
        "阈值只比 LLM 超时高一点点：一次跑满超时的正常审核会被判成漏审"
    )


def test_threshold_has_a_floor_when_the_llm_timeout_is_tiny(monkeypatch):
    """LLM 超时被调得很小时，下限必须顶住（对倍数是瞎的，专钉下限）。

    下限锚在 ``tick_generate`` 的默认间隔 30 分钟上：比它还短的话，一轮生成还没跑完
    就可能被判成没人管了。
    """
    from core.config import reload_settings

    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "5")
    reload_settings()
    assert stale_review_after() >= timedelta(minutes=30)


# ----------------------------------------------------------- 残留与出口


def test_a_crashed_review_leaves_a_draft_that_nobody_else_fills(monkeypatch, notifier):
    """反证：除了这个 sweeper，没人会把这条 draft 的机器结论补上。

    这条绿了，下一条"补上了"才有意义。同时如实钉住它**不是全盲**：审核台看得见它。
    """
    item_id = _crash_during_review(monkeypatch, notifier)

    # 再跑几轮别的 tick，谁都不会碰它
    tick_generate(account_ids=["acc-draft"], notifier=notifier)
    item = _reload(item_id)
    assert item.status == ContentStatus.DRAFT.value
    assert not item.review_notes, "有别的东西把结论补上了，那这个 sweeper 就是多余的"

    with db.session_scope() as session:
        logs = session.scalars(select(ReviewLog).where(ReviewLog.content_item_id == item_id)).all()
    assert [entry.action for entry in logs if entry.action == MACHINE_REVIEW] == [], (
        "机器审核根本没跑完，不该留下 machine_review 记录"
    )

    from core.state_machine import REVIEW_QUEUE_STATUSES

    assert ContentStatus.DRAFT in REVIEW_QUEUE_STATUSES, (
        "如实记账：这条残留会出现在审核台，不是全盲；它缺的是机器结论与告警"
    )


def test_stale_draft_gets_its_machine_review_backfilled(monkeypatch, notifier):
    """超过阈值就补跑一遍离线审核，内容回到正常的人工审核流。"""
    item_id = _crash_during_review(monkeypatch, notifier)
    later = datetime.now(UTC) + stale_review_after() + timedelta(minutes=1)

    stats = tick_retry_sweep(now=later, notifier=notifier)

    assert stats["recovered_drafts"] == 1
    item = _reload(item_id)
    assert item.status == ContentStatus.DRAFT.value, "补跑完还是 draft，等人工处置"
    assert item.review_notes, "机器结论没补上，等于没补"


def test_backfill_is_idempotent(monkeypatch, notifier):
    """补过的不会被反复补：``review_notes`` 一填上就不再命中判据。"""
    _crash_during_review(monkeypatch, notifier)
    later = datetime.now(UTC) + stale_review_after() + timedelta(minutes=1)

    assert tick_retry_sweep(now=later, notifier=notifier)["recovered_drafts"] == 1
    again = tick_retry_sweep(now=later + timedelta(hours=1), notifier=notifier)
    assert again["recovered_drafts"] == 0, "同一条被补了两次"


def test_backfill_never_calls_the_llm(monkeypatch, notifier):
    """**这次改动的核心风险**：补跑绝不许出网。

    这个 sweeper 跑在一个 DB 事务里。往里面塞一次 600 秒的 LLM 调用，就是把整轮
    改动刚修掉的"网络调用夹在被持有的写事务里"原样种回来。
    """
    item_id = _crash_during_review(monkeypatch, notifier)

    from review import llm_semantic

    called: list[int] = []

    def exploding_judge(content, findings, llm, **kwargs):
        called.append(1)
        raise AssertionError("补跑机器审核时调了 LLM——这正是要防的事")

    monkeypatch.setattr("review.pipeline.llm_semantic.judge", exploding_judge)
    later = datetime.now(UTC) + stale_review_after() + timedelta(minutes=1)
    assert recover_stale_drafts(now=later, notifier=notifier) == 1
    assert called == []
    assert llm_semantic is not None  # 保证上面那次 patch 打在真模块上

    with db.session_scope() as session:
        entry = session.scalars(
            select(ReviewLog).where(
                ReviewLog.content_item_id == item_id,
                ReviewLog.action == MACHINE_REVIEW,
            )
        ).one()
    assert (entry.after_json or {}).get("stages_skipped", {}).get("llm_semantic") == (
        "options.use_llm=False"
    ), "缺掉的那一档必须留在审计里，否则没人知道这遍审核是弱的"


def test_backfill_is_audited_apart_from_a_normal_machine_review(monkeypatch, notifier):
    """审计要分得清"审过了，结论如下"和"本该审的那遍没跑成，这是事后补的"。"""
    item_id = _crash_during_review(monkeypatch, notifier)
    later = datetime.now(UTC) + stale_review_after() + timedelta(minutes=1)
    recover_stale_drafts(now=later, notifier=notifier)

    with db.session_scope() as session:
        logs = session.scalars(select(ReviewLog).where(ReviewLog.content_item_id == item_id)).all()
    actions = [entry.action for entry in logs]
    assert SystemAction.REVIEW_MISSING.value in actions
    residue = next(e for e in logs if e.action == SystemAction.REVIEW_MISSING.value)
    reason = residue.reason or ""
    assert "从来没跑过机器审核" in reason
    assert "LLM 语境判定" in reason, "得让人知道这遍审核缺了哪一档"


def test_backfill_notifies_through_the_throttle(monkeypatch, notifier):
    """漏审是运维信号，该让人知道；但一批补回来不刷屏。"""
    _crash_during_review(monkeypatch, notifier, account_id="acc-d1")
    _crash_during_review(monkeypatch, notifier, account_id="acc-d2")

    later = datetime.now(UTC) + stale_review_after() + timedelta(minutes=1)
    assert tick_retry_sweep(now=later)["recovered_drafts"] == 2

    from core.notify import get_default_notifier

    sent = [t for _lvl, t, _x in get_default_notifier().sent if "漏审" in t]
    assert len(sent) == 1, f"两条内容应合成一条通知，实际 {sent}"


def _draft_with_body(account_id: str, platform: str, body: str) -> str:
    with db.session_scope() as session:
        account = make_account(session, account_id=account_id, platform=platform)
        item = make_item(session, account, status=ContentStatus.DRAFT.value)
        bundle = dict(item.bundle_json)
        bundle["platform"] = platform
        bundle["body_markdown"] = body
        item.bundle_json = bundle
        item.review_notes = None
        return item.id


COMMERCIAL_RULE = "precheck.commercial-expression"


def test_backfill_uses_the_same_commercial_ruleset_as_the_normal_pipeline(notifier):
    """补跑的那一遍必须和正常那一遍**同口径**，两个方向都钉。

    ``core/dev_flow.py`` 的小红书 / 抖音链传 ``commercial=True``（极限词、效果承诺是
    这两个平台被判违规最多的一类），公众号用 ``ReviewOptions`` 的默认值。

    - 漏了这个开关：小红书的补跑比正常那一遍**松**，审核台上的"机器审核通过"是假的；
    - 一律打开：公众号会多出一堆正常链路不会报的 warn，同一条内容的结论跟着"谁审的"变。

    所以正反两条一起断言——只钉一边的话，"改成 ``frozenset()``"和"改成所有平台"里
    总有一个溜得掉。
    """
    body = "本店全网最低价，欢迎选购"
    xhs_id = _draft_with_body("acc-comm-xhs", "xhs", body)
    mp_id = _draft_with_body("acc-comm-mp", "wechat_mp", body)

    assert recover_stale_drafts(now=datetime.now(UTC) + timedelta(days=7), notifier=notifier) == 2

    xhs_notes = _reload(xhs_id).review_notes or ""
    mp_notes = _reload(mp_id).review_notes or ""
    assert COMMERCIAL_RULE in xhs_notes, f"小红书补跑没开商业规则，比正常那一遍松：{xhs_notes!r}"
    assert COMMERCIAL_RULE not in mp_notes, f"公众号补跑开了商业规则，比正常那一遍严：{mp_notes!r}"


# ------------------------------------------------------- 别碰不该碰的


def test_a_review_still_within_the_threshold_is_left_alone(monkeypatch, notifier):
    """正在审的稿子一根手指都不许碰。

    误判的代价：正常那一遍审到一半被抢先写上一份**缺 LLM 语境判定**的弱结论。
    """
    from core.config import get_settings

    item_id = _crash_during_review(monkeypatch, notifier)
    started = datetime.now(UTC)
    # 第二项刻意从配置推而不是从阈值推：从阈值推的话阈值被改小时用例会跟着缩
    slowest = get_settings().llm_timeout_seconds
    for elapsed in (
        timedelta(seconds=1),
        timedelta(seconds=slowest * 2),
        stale_review_after() - timedelta(minutes=1),
    ):
        assert recover_stale_drafts(now=started + elapsed, notifier=notifier) == 0, (
            f"入库才过了 {elapsed}，就被判成漏审了"
        )
        assert not _reload(item_id).review_notes


def test_the_sweeper_is_exactly_on_the_threshold_boundary(monkeypatch, notifier):
    """边界本身：差一分钟不碰，过一分钟就补。钉住"阈值真的被用上了"。"""
    item_id = _crash_during_review(monkeypatch, notifier)
    started = datetime.now(UTC)
    window = stale_review_after()

    assert recover_stale_drafts(now=started + window - timedelta(minutes=1)) == 0
    assert not _reload(item_id).review_notes

    assert recover_stale_drafts(now=started + window + timedelta(minutes=1)) == 1
    assert _reload(item_id).review_notes


def test_sweeper_ignores_drafts_that_were_already_reviewed(notifier):
    """审过的 draft（含被驳回的、机器审出 block 的）都有 ``review_notes``，不许重审。

    重审会用一份**弱结论**盖掉正常那一遍的结论，是实打实的信息损失。
    """
    with db.session_scope() as session:
        account = make_account(session, account_id="acc-reviewed")
        item = make_item(session, account, status=ContentStatus.DRAFT.value)
        item.review_notes = "机器审核通过：block 0 / warn 0 / info 1"
        item_id = item.id
        original = item.review_notes

    assert recover_stale_drafts(now=datetime.now(UTC) + timedelta(days=7), notifier=notifier) == 0
    assert _reload(item_id).review_notes == original


def test_sweeper_does_not_touch_other_statuses(notifier, caplog):
    """只认 ``draft``：``approved`` / ``scheduled`` / ``published`` 全不许动。

    尤其 ``approved``——机器审核会把内容打回 ``reviewing`` 再回 ``draft``，
    对一条人已经批准的内容做这件事等于把人工卡点抹掉。

    断言不止"状态没变"：``review_item`` 自己也拦非 draft，所以光看状态的话，
    "SQL 里根本没查它"和"查了、试了、被 review_item 拒了"是同一个结果——后者会让
    每 5 分钟一轮的 tick 对着每条内容刷一条 warning。所以连**有没有去试**一起钉。
    """
    import logging

    caplog.set_level(logging.WARNING, logger="social_workflow.scheduler")
    with db.session_scope() as session:
        account = make_account(session, account_id="acc-other")
        ids = {
            status: make_item(session, account, status=status).id
            for status in (
                ContentStatus.APPROVED.value,
                ContentStatus.SCHEDULED.value,
                ContentStatus.PUBLISHED.value,
                ContentStatus.REJECTED.value,
            )
        }

    assert recover_stale_drafts(now=datetime.now(UTC) + timedelta(days=7), notifier=notifier) == 0
    for status, item_id in ids.items():
        assert _reload(item_id).status == status
    tried = [r.getMessage() for r in caplog.records if "补跑机器审核失败" in r.getMessage()]
    assert tried == [], f"SQL 判据没拦住，靠 review_item 兜底了（每轮都会刷日志）：{tried}"


# ---------------------------------------------- produced_today 会不会失真


def test_crash_residue_counts_toward_the_daily_target(monkeypatch, notifier):
    """残留会计进当日产出——**这是对的**，不是失真。

    它确实被生产出来了，token 也确实花了。计进去的效果是这一轮不会再生成一条几乎
    一样的稿（旧写法把 draft 一起回滚，下一轮 tick_generate 会重新烧一遍 token 产出
    重复内容）。而 sweeper 会把它补成一条正常可审的稿，名额没有被浪费。
    """
    from core.accounts import policy_of
    from core.models import Account

    item_id = _crash_during_review(monkeypatch, notifier)
    with db.session_scope() as session:
        account = session.get(Account, "acc-draft")
        assert account is not None
        assert produced_today(session, account, policy_of(account), datetime.now(UTC)) == 1

    # 补完之后它是一条正常的待审稿，名额没白占
    recover_stale_drafts(now=datetime.now(UTC) + stale_review_after() + timedelta(minutes=1))
    item = _reload(item_id)
    assert item.status == ContentStatus.DRAFT.value and item.review_notes
