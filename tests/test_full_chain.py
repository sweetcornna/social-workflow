"""一条内容走完**整条链**：选题 → 出稿 → 机器审核 → autopilot → 排期 → 人确认 → 发布 → 指标。

**为什么单独要这一条**：每一道闸门都各自有测试（`test_confirm_gate.py` 盯确认、
`test_p1_e2e.py` 盯到"可审核的 item"为止、`test_scheduling.py` 盯窗口与限频），
但没有任何一条把它们**串起来**走一遍。而这条链最典型的坏法恰恰不会让任何单元测试
变红——它只是**不再往前走**：

2026-08-24 真出过一次。`inspect` 对公众号 / 抖音的题图误报了一条
``illustration.orphan``（题图是烤进封面的底图，本来就不该出现在 media），于是
每条稿子都带着一条 warn；autopilot 的判据是"block 0 **且** warn 0"，于是它一条都
不批准。所有测试全绿，流水线静默停在 draft，没有任何东西报错。

所以这条测试的断言分两类，缺一不可：

- **该走的每一步真的走了**（集成）。
- **不该自己走的那一步真的没走**：没人点确认时，链条必须停在 ``scheduled``。
  这是红线 R1 的机器化表达——小红书 2026-03 封禁完全无人值守的 AI 账号，
  "发布前要人点一下"不许有旁路。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from core import db
from core.accounts import policy_of
from core.confirm import autopilot_approve, confirm_item
from core.dev_flow import (
    make_scripted_llm,
    make_xhs_scripted_llm,
    run_wechat_pipeline,
    run_xhs_pipeline,
)
from core.models import Account, ContentItem
from core.scheduler import tick_metrics, tick_scheduled_publish
from core.state_machine import ContentStatus
from core.telegram import set_telegram_channel
from generation.pipeline import GenerationOptions, XhsGenerationOptions
from publishers.registry import use_fake_publishers
from sourcing import douyin_hot_hub, newsnow
from sourcing.base import persist_topics
from tests.conftest import make_account
from tests.generation.test_illustrations import FakeImagegen
from tests.p1_helpers import RecordingRunner, fake_screenshotter, load_fixture
from tests.p2_helpers import card_screenshotter
from tests.test_confirm_gate import FakeChannel

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


@pytest.fixture
def channel() -> FakeChannel:
    ch = FakeChannel()
    set_telegram_channel(ch)  # type: ignore[arg-type]
    return ch


def _notes(item_id: str) -> str:
    with db.session_scope() as session:
        return session.get(ContentItem, item_id).review_notes or ""


def _status_of(item_id: str) -> str:
    with db.session_scope() as session:
        item = session.get(ContentItem, item_id)
        assert item is not None
        return item.status


def test_the_whole_chain_walks_from_a_topic_to_measured(session, tmp_path, channel) -> None:
    use_fake_publishers()

    # -- 1 选题 + 出稿 + 机器审核 -----------------------------------------
    account = make_account(
        session,
        account_id="xhs-fullchain",
        platform="xhs",
        # autopilot 开着才轮得到第 2 步；confirm_required 不写 = 默认 True（R1）
        extra={"autopilot": True},
    )
    # 灌选题池用 fixture 里的真实榜单快照，不出网（同 test_p1_e2e）
    persist_topics(
        session,
        newsnow.parse_source_response(load_fixture("newsnow_weibo.json"), source_id="weibo")
        + douyin_hot_hub.parse_hot_search(
            load_fixture("douyin_hot_search.json"), day=date(2026, 8, 15)
        ),
    )
    session.commit()

    result = run_xhs_pipeline(
        session,
        account,
        llm=make_xhs_scripted_llm(),
        options=XhsGenerationOptions(
            media_root=tmp_path / "media",
            screenshotter=card_screenshotter([]),
            # **配图必须打开**：2026-08-24 那次静默停摆就出在题图那段检查上，
            # illustrations=0 的话它整段不执行，这条测试就守不住那个 bug。
            # 生图走替身（不出网），但 inspect 的题图/孤儿判定照常跑。
            illustrations=1,
            imagegen=FakeImagegen(),
        ),
    )
    session.commit()
    item_id = result.content_item_id
    assert item_id, result.error or "没出稿"
    assert _status_of(item_id) == ContentStatus.DRAFT.value

    # 机器审核必须是**干净**的，否则 autopilot 不会批 —— 这正是 2026-08-24 那次
    # 静默停摆的判据。把它钉在这里：将来哪一档开始误报，这条会红，而不是流水线默默不动。
    assert result.review_blocking == 0, result.review_findings
    assert result.review_warning == 0, "机器审核挂了 warn，autopilot 不会批准，链条会静默停在 draft"

    # -- 2 autopilot：批准 + 排期 -----------------------------------------
    with db.session_scope() as s2:
        item = s2.get(ContentItem, item_id)
        outcome = autopilot_approve(
            s2, item, policy=policy_of(s2.get(Account, account.id)), blocking=0, warnings=0, now=NOW
        )
    assert outcome.approved and outcome.scheduled, outcome.reason
    assert _status_of(item_id) == ContentStatus.SCHEDULED.value

    # -- 3 ★ 链条必须停在这里 ---------------------------------------------
    # 没人点确认，到点也不许发。这一条断言就是 R1 本身。
    with db.session_scope() as s3:
        s3.get(ContentItem, item_id).scheduled_at = NOW - timedelta(minutes=5)
    stats = tick_scheduled_publish(now=NOW)
    assert stats["published"] == 0, "没人确认就发出去了 —— 红线 R1 被旁路"
    assert stats["skipped_unconfirmed"] == 1
    assert _status_of(item_id) == ContentStatus.SCHEDULED.value

    # -- 4 人点确认（这一步在真实系统里只能由人做）-------------------------
    with db.session_scope() as s4:
        confirm_item(s4, s4.get(ContentItem, item_id), actor="operator", now=NOW)

    # -- 5 发布 -------------------------------------------------------------
    stats = tick_scheduled_publish(now=NOW)
    assert stats["published"] == 1, stats
    assert _status_of(item_id) == ContentStatus.PUBLISHED.value

    # -- 6 指标回流 ---------------------------------------------------------
    tick_metrics(now=NOW + timedelta(hours=25))
    assert _status_of(item_id) == ContentStatus.MEASURED.value


def test_the_wechat_chain_also_walks_all_the_way(session, tmp_path, channel) -> None:
    """公众号版的同一条链。

    **不是重复**：2026-08-24 那次静默停摆只发生在公众号 / 抖音上——它们的题图带
    ``role``、被烤进封面当底图，所以旧的 ``illustration.orphan`` 判据对它们必然误报；
    小红书的配图带 ``final_path``、本来就挂在 ``media`` 里，那段检查天然通过。
    我第一版只写了小红书那条，把假阳性放回去它照样全绿——守不住的测试比没有更糟，
    所以这条必须存在。
    """
    use_fake_publishers()

    account = make_account(
        session,
        account_id="wechat-fullchain",
        platform="wechat_mp",
        extra={"autopilot": True, "author": "公众号测试号"},
    )
    persist_topics(
        session,
        newsnow.parse_source_response(load_fixture("newsnow_weibo.json"), source_id="weibo"),
    )
    session.commit()

    written: list[tuple[Path, str]] = []
    result = run_wechat_pipeline(
        session,
        account,
        llm=make_scripted_llm(),
        options=GenerationOptions(
            media_root=tmp_path / "media",
            screenshotter=fake_screenshotter(written),
            render_runner=RecordingRunner(stdout="<section><p>正文</p></section>"),
            illustrations=1,
            imagegen=FakeImagegen(),
        ),
    )
    session.commit()
    item_id = result.content_item_id
    assert item_id, result.error or "没出稿"
    assert result.review_blocking == 0, result.review_findings
    assert result.review_warning == 0, f"机器审核挂了 warn，autopilot 不会批准：{_notes(item_id)}"

    with db.session_scope() as s2:
        outcome = autopilot_approve(
            s2,
            s2.get(ContentItem, item_id),
            policy=policy_of(s2.get(Account, account.id)),
            blocking=0,
            warnings=0,
            now=NOW,
        )
    assert outcome.approved and outcome.scheduled, outcome.reason

    with db.session_scope() as s3:
        s3.get(ContentItem, item_id).scheduled_at = NOW - timedelta(minutes=5)
    assert tick_scheduled_publish(now=NOW)["published"] == 0, "没人确认就发出去了"

    with db.session_scope() as s4:
        confirm_item(s4, s4.get(ContentItem, item_id), actor="operator", now=NOW)
    assert tick_scheduled_publish(now=NOW)["published"] == 1
    tick_metrics(now=NOW + timedelta(hours=25))
    assert _status_of(item_id) == ContentStatus.MEASURED.value
