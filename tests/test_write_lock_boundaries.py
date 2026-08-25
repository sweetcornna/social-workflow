"""网络调用不许夹在**被持有的 SQLite 写事务**里。

SQLite 的写锁是整库一把：一个事务 flush 出第一条写就拿到 RESERVED，直到 commit
才放。本文件钉的是两条真实链路上"锁的持有区间里有没有网络调用"——

- ``BudgetGuard.charge()``：``tick_generate`` 一轮里第 1 次记账之后，剩下的 LLM
  （``llm_timeout_seconds=600``）与生图（180s）全在锁里跑；
- ``publish_with_idempotency``：``in_flight`` 声明只 ``flush`` 不 ``commit``，
  紧接着就是最长 1200 秒的平台发布。

写法上有两条自我约束（这仓被"看起来在测并发、其实没有争用"的假测试坑过）：

1. **真的两条连接、真的锁**。探针是一条独立的 ``sqlite3`` 连接，
   ``timeout=0``——锁被占着就当场 ``database is locked``，不靠 sleep 猜时序。
2. **每条正向断言都配一条反证**。先证明探针在锁真的被握着时会红，
   否则"探针绿了"可能只是因为这一轮压根没发生争用。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import func, select

from core import db
from core.budget import BudgetExhausted, BudgetGuard, CostKind, today_key
from core.models import Account, ContentItem, CostLedger, PublishRecord, Topic, new_id
from core.scheduler import tick_generate
from core.state_machine import ContentStatus, PublishPhase, publish_with_idempotency
from generation.llm import ScriptedLLM
from generation.mpt_client import API_PREFIX
from publishers.base import (
    ContentBundle,
    FakePublisher,
    PermanentError,
    PublishResult,
    RetryableError,
)
from tests.conftest import make_account, make_bundle, make_item
from tests.p3_helpers import cover_screenshotter, douyin_llm, ok_envelope, task_payload

MPT_BASE = "http://mpt.test:8080"
SAMPLE_VIDEO = Path("tests/fixtures/video/sample.mp4")

# --------------------------------------------------------------------- 探针


def _db_path() -> str:
    url = db.current_url() or ""
    prefix = "sqlite:///"
    assert url.startswith(prefix), f"这些用例只对文件型 SQLite 有意义，当前 {url!r}"
    return url[len(prefix) :]


def _make_probe_table() -> None:
    conn = sqlite3.connect(_db_path())
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS lock_probe (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()


def _independent_write(marker: int) -> str | None:
    """一条**完全独立**的连接尝试写库。写进去返回 ``None``，被写锁挡住返回错误文本。

    ``timeout=0``：等锁时长为零，锁被别人握着就当场报错。这样"能不能写"是即时
    的事实判断，既不需要 sleep，也不会因为机器快慢而漂。
    """
    conn = sqlite3.connect(_db_path(), timeout=0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO lock_probe VALUES (?)", (marker,))
        conn.commit()
        return None
    except sqlite3.OperationalError as exc:
        conn.rollback()
        return str(exc)
    finally:
        conn.close()


def _fresh_session():
    """另开一条连接读库——只看得见**已提交**的数据，等价于"重启后再看一眼"。"""
    return db.get_session_factory()()


def test_the_probe_really_detects_a_held_write_lock(session):
    """反证：ORM flush 出去还没 commit 时，探针必须报 ``database is locked``。

    这条是上面所有正向断言的地基。它绿了才说明探针有分辨力——否则后面那些
    "另一个线程能写库"可能只是因为探针根本测不出锁。
    """
    _make_probe_table()
    assert _independent_write(1) is None, "没人持锁时探针本身就该能写"

    session.add(CostLedger(id=new_id("cost"), day=today_key(), kind="tokens", amount=1.0, meta={}))
    session.flush()  # 写锁到手，不 commit
    blocked = _independent_write(2)
    session.rollback()

    assert blocked is not None and "locked" in blocked, (
        "flush 之后探针居然写进去了 = 探针测不出写锁，本文件其余用例全部失去意义"
    )


# ------------------------------------------------------------ A：预算记账


def test_a_charged_generation_round_does_not_hold_the_write_lock(session):
    """一个线程记完账后卡在"网络调用"位置，另一个线程此时必须能写库。

    复现的是 ``tick_generate``：第 1 次 ``charge`` 之后还有第 2..N 次 LLM 与生图，
    整轮跑完才 commit。``release`` 事件就站在那些网络调用的位置上。
    """
    _make_probe_table()
    charged = threading.Event()
    release = threading.Event()
    failures: list[BaseException] = []

    def generation_round() -> None:
        s = _fresh_session()
        try:
            guard = BudgetGuard(s, token_budget=10_000, labels={"account_id": "acc-probe"})
            guard.charge(CostKind.TOKENS, 120, meta={"purpose": "probe.first"})
            charged.set()
            # ↓ 真实链路上这里是几十秒到几分钟的 LLM / 生图 / 渲染
            release.wait(timeout=30)
            s.commit()
        except BaseException as exc:
            failures.append(exc)
            charged.set()
        finally:
            s.close()

    worker = threading.Thread(target=generation_round, name="generation-round")
    worker.start()
    try:
        assert charged.wait(timeout=30), "生成线程没能记上账"
        blocked = _independent_write(11)
    finally:
        release.set()
        worker.join(timeout=30)

    assert not failures, f"生成线程炸了：{failures[0]!r}"
    assert blocked is None, f"charge 之后写锁仍被生成事务握着：{blocked}"


def test_a_charge_survives_a_rollback_of_the_generation_transaction(session):
    """token 是真花掉了：外层事务回滚**不该**把这笔记账一起回滚。

    这既是记账正确性，也是闸门正确性——流水没了，下一轮就会拿着一个偏小的
    ``used`` 继续放行支出。
    """
    guard = BudgetGuard(session, token_budget=10_000, labels={"account_id": "acc-probe"})
    guard.charge(CostKind.TOKENS, 250, meta={"purpose": "probe.rollback"})

    session.rollback()  # 模拟生成链后半段失败，外层整体回滚

    fresh = _fresh_session()
    try:
        total = fresh.scalar(
            select(func.coalesce(func.sum(CostLedger.amount), 0.0)).where(
                CostLedger.day == today_key(), CostLedger.kind == CostKind.TOKENS.value
            )
        )
    finally:
        fresh.close()
    assert total == 250.0, "外层一回滚，已经花掉的 token 就从账本上消失了"


def test_a_gate_still_accumulates_across_charges_in_one_round(session):
    """闸门是金额闸门：换了记账连接之后，同一轮里的累计与超限判定不许松。"""
    guard = BudgetGuard(session, token_budget=100)
    guard.charge(CostKind.TOKENS, 40)
    assert guard.used(CostKind.TOKENS) == 40.0
    assert guard.remaining(CostKind.TOKENS) == 60.0

    guard.charge(CostKind.TOKENS, 40)
    assert guard.used(CostKind.TOKENS) == 80.0
    assert guard.remaining(CostKind.TOKENS) == 20.0

    with pytest.raises(BudgetExhausted):
        guard.charge(CostKind.TOKENS, 40)
    assert guard.used(CostKind.TOKENS) == 80.0, "被拦下的支出不许写流水"


def test_a_gate_is_not_fooled_by_a_stale_snapshot(session):
    """调用方自己进了写事务之后，闸门仍必须看得见此前已经记满的账。

    换连接记账最危险的失手方式是读到旧快照：``used`` 偏小 → 本该拦下的支出被放行。
    这里让调用方的连接先进入写事务（快照定格），再问闸门。
    """
    guard = BudgetGuard(session, token_budget=100)
    guard.charge(CostKind.TOKENS, 90, meta={"purpose": "probe.snapshot"})

    make_account(session, account_id="acc-snapshot")  # add + flush → 调用方连接进写事务
    assert session.in_transaction()

    assert guard.used(CostKind.TOKENS) == 90.0
    with pytest.raises(BudgetExhausted):
        guard.charge(CostKind.TOKENS, 20)
    session.rollback()


def test_a_charge_still_lands_when_the_caller_already_holds_the_write_lock(session):
    """调用方已经握着写锁时，记账不许失败，也不许干等到 ``database is locked``。

    这是真实存在的形状：``generation/video_pipeline.py`` 的 ``charge_render_seconds``
    就在 ``RenderJob`` flush 之后的 ``finally`` 里。独立连接在这种局面下拿不到写锁
    （对方要等本次调用返回才 commit，是真死锁），所以只能退回调用方的连接记账——
    退回后语义与修复前一致，但**账一定要记上**：丢一笔就是闸门被放松。
    """
    _make_probe_table()
    make_account(session, account_id="acc-holds-lock")  # 调用方拿住写锁
    assert _independent_write(21) is not None, "调用方没握住锁，这条用例没测到东西"

    guard = BudgetGuard(session, token_budget=1_000)
    started = time.perf_counter()
    guard.charge(CostKind.TOKENS, 30, meta={"purpose": "probe.fallback"})
    elapsed = time.perf_counter() - started
    assert guard.used(CostKind.TOKENS) == 30.0

    # 死锁必须**判出来**，不能靠等到 busy_timeout 再兜底：等满 5 秒是直接加在
    # 1 分钟一轮的 tick 上的，逼近一轮间隔就会让下一轮被静默跳过（见 core/db.py）。
    # 1 秒对 5 秒有五倍余量，机器慢一点也不会漂
    assert elapsed < 1.0, f"记账干等了 {elapsed:.2f} 秒才落库 = 没有提前判出调用方持锁"

    session.commit()
    fresh = _fresh_session()
    try:
        total = fresh.scalar(
            select(func.coalesce(func.sum(CostLedger.amount), 0.0)).where(
                CostLedger.day == today_key(), CostLedger.kind == CostKind.TOKENS.value
            )
        )
    finally:
        fresh.close()
    assert total == 30.0


def test_a_tick_generate_runs_its_whole_llm_chain_outside_the_write_lock(monkeypatch, notifier):
    """端到端：真跑一轮 ``tick_generate``，在**每一次** LLM 调用处探一下写锁。

    这是缺陷本体的形状。修复前实测（同一条用例、只把 ``core/budget.py`` 换回旧版）::

        LLM #1 sourcing.select  写锁被持有=False   ← 第 1 次 charge 之前
        LLM #2 xhs.angle        写锁被持有=True    ← 第 1 次 charge 已经把锁拿走
        LLM #3 xhs.cards        写锁被持有=True
        LLM #4 xhs.note         写锁被持有=True
        LLM #5 xhs.selfcheck    写锁被持有=True

    也就是说这一轮里除了第一次调用，全部 LLM（``llm_timeout_seconds=600``）都跑在
    锁里，整轮才 commit。

    断言覆盖这一轮里**每一次** LLM 调用，不豁免 ``review.*``：机器审核的调用一度是
    个例外（``core/dev_flow.py`` 的 ``_persist_and_review`` 把 ``ContentItem``
    flush 出去之后才审），那道锁已经在 P16.3 一并交出去了，见下面
    ``test_a_machine_review_llm_runs_outside_the_write_lock``。
    """
    monkeypatch.setenv("SW_GENERATE_MAKE_MEDIA", "false")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")  # 没 key → 走 ScriptedLLM，不出网
    monkeypatch.setenv("DAILY_TOKEN_BUDGET", "1000000")
    from core.config import reload_settings

    reload_settings()
    _make_probe_table()

    with db.session_scope() as setup:
        account = make_account(setup, account_id="gen-lock-probe", platform="xhs")
        account.extra = {"daily_target": 1}
        for index in range(5):
            setup.add(
                Topic(
                    id=new_id("tp"),
                    source="newsnow",
                    title=f"候选选题 {index}",
                    score=1.0 - index * 0.1,
                    raw={},
                )
            )

    probes: list[tuple[str, str | None]] = []
    original = ScriptedLLM._record

    def probing_record(self, kind, prompt, system, purpose, **kwargs):
        probes.append((purpose, _independent_write(60 + len(probes))))
        return original(self, kind, prompt, system, purpose, **kwargs)

    monkeypatch.setattr(ScriptedLLM, "_record", probing_record)
    stats = tick_generate(account_ids=["gen-lock-probe"], notifier=notifier)

    assert stats["generated"] == 1, f"这一轮压根没生成内容，等于没测：{stats}"
    assert len(probes) >= 4, f"这一轮只调了 {len(probes)} 次 LLM，太少"
    assert [(p, b) for p, b in probes if b is not None] == [], (
        f"这一轮里还有 LLM 调用跑在写锁里：{probes}"
    )


def test_a_machine_review_llm_runs_outside_the_write_lock(monkeypatch, notifier):
    """机器审核的 ``review.semantic`` 调用也不许在锁里跑。

    它一度是生成链上唯一的例外：``_persist_and_review`` 先把 ``ContentItem``
    flush 出去（写锁到手）再跑审核，于是那一次 LLM 调用（上界同样是
    ``llm_timeout_seconds=600``）整段在锁里。修复前实测 ``写锁被持有=True``。

    单独一条用例是因为它在正常内容上**根本不会被调到**——``llm_semantic`` 只在有
    block/warn 命中时才真的问 LLM。这里直接把那一级换成探针，逼它必然发生。
    """
    monkeypatch.setenv("SW_GENERATE_MAKE_MEDIA", "false")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("DAILY_TOKEN_BUDGET", "1000000")
    from core.config import reload_settings

    reload_settings()
    _make_probe_table()

    with db.session_scope() as setup:
        account = make_account(setup, account_id="gen-review-probe", platform="xhs")
        account.extra = {"daily_target": 1}
        for index in range(5):
            setup.add(
                Topic(
                    id=new_id("tp"),
                    source="newsnow",
                    title=f"候选选题 {index}",
                    score=1.0 - index * 0.1,
                    raw={},
                )
            )

    from review import llm_semantic

    seen: list[str | None] = []

    def probing_judge(content, findings, llm, **kwargs):
        seen.append(_independent_write(70 + len(seen)))
        raise llm_semantic.SemanticSkipped("测试探针：只量锁，不真判定")

    monkeypatch.setattr("review.pipeline.llm_semantic.judge", probing_judge)
    stats = tick_generate(account_ids=["gen-review-probe"], notifier=notifier)

    assert stats["generated"] == 1, f"这一轮压根没生成内容，等于没测：{stats}"
    assert seen == [None], f"机器审核的 LLM 调用是在写锁里跑的：{seen}"


# ------------------------------------------------------ C：视频渲染轮询


def _mock_mpt(states: list[dict], *, video: bytes | None = None) -> None:
    respx.post(f"{MPT_BASE}{API_PREFIX}/videos").mock(
        return_value=httpx.Response(200, json=ok_envelope({"task_id": "t1"}))
    )
    respx.get(f"{MPT_BASE}{API_PREFIX}/tasks/t1").mock(
        side_effect=[httpx.Response(200, json=ok_envelope(state)) for state in states]
    )
    if video is not None:
        respx.get(f"{MPT_BASE}{API_PREFIX}/download/t1/final-1.mp4").mock(
            return_value=httpx.Response(200, content=video)
        )


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _mpt_options(tmp_path, clock: _FakeClock):
    from generation.mpt_client import MptClient
    from generation.video_pipeline import VideoGenerationOptions

    return VideoGenerationOptions(
        media_root=tmp_path / "media",
        screenshotter=cover_screenshotter([]),
        client=MptClient(MPT_BASE, timeout=1.0, download_timeout=1.0),
        poll_interval=5.0,
        render_timeout=60.0,
        sleeper=clock.sleep,
        clock=clock,
    )


@respx.mock
def test_c_the_pipeline_poll_loop_does_not_hold_the_write_lock(session, tmp_path):
    """管线内等 MPT 的那个循环：每一次轮询开始时写锁都必须是空的。

    修复前实测两次轮询 ``写锁被持有=True``——``create_render_job`` 的 flush 拿走锁，
    然后整个 ``mpt_render_timeout_seconds``（默认 1800 秒）都握着它轮询。
    """
    from generation import video_pipeline
    from generation.video_pipeline import generate_douyin_bundle

    _make_probe_table()
    clock = _FakeClock()
    _mock_mpt(
        [
            task_payload("t1", state=4, progress=20),
            task_payload("t1", state=1, progress=100, videos=["/tasks/t1/final-1.mp4"]),
        ],
        video=SAMPLE_VIDEO.read_bytes(),
    )
    make_account(session, account_id="douyin-lock", platform="douyin")
    session.commit()

    polls: list[str | None] = []
    original = video_pipeline.sync_render_job

    def probing_sync(s, job, client):
        polls.append(_independent_write(80 + len(polls)))
        return original(s, job, client)

    charged_under_lock: list[str | None] = []
    original_charge = video_pipeline.charge_render_seconds

    def probing_charge(guard, seconds, **kwargs):
        charged_under_lock.append(_independent_write(90))
        return original_charge(guard, seconds, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(video_pipeline, "sync_render_job", probing_sync)
        mp.setattr(video_pipeline, "charge_render_seconds", probing_charge)
        generate_douyin_bundle(
            "通勤成本",
            None,
            llm=douyin_llm(),
            session=session,
            account_id="douyin-lock",
            content_id="itm_lock_probe",
            options=_mpt_options(tmp_path, clock),
            budget=BudgetGuard(session, render_seconds_budget=3600),
        )

    assert len(polls) >= 2, "没真的轮询，等于没测"
    assert [p for p in polls if p is not None] == [], f"轮询是在写锁里跑的：{polls}"
    assert charged_under_lock == [None], (
        f"渲染秒数记账时写锁还在调用方手里（会退回调用方事务）：{charged_under_lock}"
    )


@respx.mock
def test_c_the_scheduler_poll_tick_does_not_hold_the_write_lock(session):
    """``tick_render_jobs`` 一轮扫 20 个 job：第 2 个开始不许在前一个的锁里轮询。

    修复前实测 ``job #1=False, job #2=True``——第 1 个 job 的 flush 拿走锁，之后
    每个 job 的 HTTP 轮询和**成片下载**都在这把锁里，一轮最多 20 次。
    """
    from core.models import RenderJob, RenderJobState
    from core.scheduler import tick_render_jobs
    from generation import video_pipeline
    from generation.mpt_client import MptClient

    _make_probe_table()
    # 第 1 个 job 走**完整**的"轮询 → 下载成片 → 补挂进内容"路径：这条路才会在
    # 本轮留下待写的数据。第 2、3 个还在渲染，负责当探针
    respx.get(f"{MPT_BASE}{API_PREFIX}/tasks/done-1").mock(
        return_value=httpx.Response(
            200,
            json=ok_envelope(
                task_payload("done-1", state=1, progress=100, videos=["/tasks/done-1/final-1.mp4"])
            ),
        )
    )
    respx.get(f"{MPT_BASE}{API_PREFIX}/download/done-1/final-1.mp4").mock(
        return_value=httpx.Response(200, content=SAMPLE_VIDEO.read_bytes())
    )
    respx.get(f"{MPT_BASE}{API_PREFIX}/tasks/running-1").mock(
        return_value=httpx.Response(
            200, json=ok_envelope(task_payload("running-1", state=4, progress=30))
        )
    )
    account = make_account(session, account_id="douyin-tick", platform="douyin")
    attachable = make_item(session, account, status=ContentStatus.DRAFT.value)
    session.add(
        RenderJob(
            id="rj_probe_1",
            content_item_id=attachable.id,
            provider="mpt",
            task_id="done-1",
            state=RenderJobState.RUNNING,
            progress=0,
            result_paths=[],
            attempts=1,
            meta={},
        )
    )
    for index in (2, 3):
        session.add(
            RenderJob(
                id=f"rj_probe_{index}",
                content_item_id=f"itm_probe_{index}",
                provider="mpt",
                task_id="running-1",
                state=RenderJobState.RUNNING,
                progress=0,
                result_paths=[],
                attempts=1,
                meta={},
            )
        )
    session.commit()

    polls: list[str | None] = []
    original = video_pipeline.sync_render_job

    def probing_sync(s, job, client):
        polls.append(_independent_write(100 + len(polls)))
        return original(s, job, client)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("generation.video_pipeline.sync_render_job", probing_sync)
        stats = tick_render_jobs(client=MptClient(MPT_BASE, timeout=1.0, download_timeout=1.0))

    assert len(polls) == 3, f"没扫到三个 job：{polls}"
    assert stats["attached"] == 1, "第 1 个 job 没走成'下载 + 补挂'那条路，探针就白设了"
    assert [p for p in polls if p is not None] == [], f"调度器轮询是在写锁里跑的：{polls}"


@respx.mock
def test_c_a_submitted_render_job_survives_a_crash_and_is_picked_up_again(session, tmp_path):
    """提交给 MPT 的任务必须先落盘再轮询：崩了也不能变成没人认领的远端孤儿。

    这条同时把**回收路径**钉住：崩溃残留的 ``running`` job 由现成的
    ``tick_render_jobs`` 捞回来，下载成片并挂回内容。渲染这一侧**不需要**新的
    sweeper，就是因为这条路早就通着（对比 ``publishing`` 那次：那个状态谁都不扫）。
    """
    from core.models import RenderJob, RenderJobState
    from core.scheduler import tick_render_jobs
    from generation.mpt_client import MptClient
    from generation.video_pipeline import generate_douyin_bundle

    clock = _FakeClock()
    # 第一次轮询就超时（render_timeout=0）→ 管线放弃，但任务还在 MPT 侧跑着
    _mock_mpt(
        [
            task_payload("t1", state=4, progress=10),
            task_payload("t1", state=1, progress=100, videos=["/tasks/t1/final-1.mp4"]),
        ],
        video=SAMPLE_VIDEO.read_bytes(),
    )
    account = make_account(session, account_id="douyin-crash", platform="douyin")
    session.commit()

    options = _mpt_options(tmp_path, clock)
    options.render_timeout = 0.0  # 提交完立刻判超时，模拟"没等到就走了"
    outcome = generate_douyin_bundle(
        "通勤成本",
        None,
        llm=douyin_llm(),
        session=session,
        account_id=account.id,
        content_id="itm_crash_probe",
        options=options,
        budget=BudgetGuard(session, render_seconds_budget=3600),
    )
    assert outcome.video_path is None, "这条用例要的是'没拿到成片'的那条路"

    # ← 进程在这里没了：这一半事务永远不会提交
    item = make_item(session, account, status=ContentStatus.DRAFT.value)
    item.id = "itm_crash_probe"
    session.rollback()

    survivor = _fresh_session()
    try:
        jobs = survivor.scalars(select(RenderJob)).all()
        assert len(jobs) == 1, "提交给 MPT 的任务随崩溃一起没了 → 远端孤儿，没人认领"
        assert jobs[0].state == RenderJobState.RUNNING
        assert jobs[0].task_id == "t1"
        # "交给调度器继续跟"这句话要成立，超时那一笔也必须真的在库里，
        # 否则重启后没人知道管线为什么放手
        assert "超时" in (jobs[0].last_error or ""), "管线放手的原因没落盘"
        # 内容项没来得及入库，回收时不该炸
        survivor.add(
            ContentItem(
                id="itm_crash_probe",
                account_id=account.id,
                status=ContentStatus.DRAFT.value,
                bundle_json=make_bundle(
                    item_id="itm_crash_probe", account_id=account.id, platform="douyin"
                ).model_dump(mode="json"),
            )
        )
        survivor.commit()
    finally:
        survivor.close()

    stats = tick_render_jobs(client=MptClient(MPT_BASE, timeout=1.0, download_timeout=1.0))
    assert stats["done"] == 1 and stats["attached"] == 1, f"现成的回收路径没接住它：{stats}"

    checker = _fresh_session()
    try:
        job = checker.scalars(select(RenderJob)).one()
        assert job.state == RenderJobState.DONE
        assert job.result_paths
    finally:
        checker.close()


# ------------------------------------------------------------ B：发布幂等


class BlockingPublisher(FakePublisher):
    """``publish`` 停在事件上不返回——站在"抖音发布最长 1200 秒"的位置。"""

    def __init__(self, account_id: str, *, platform: str = "xhs") -> None:
        super().__init__(account_id, platform=platform)
        self.entered = threading.Event()
        self.release = threading.Event()

    def publish(self, bundle: ContentBundle) -> PublishResult:
        self.entered.set()
        self.release.wait(timeout=30)
        return super().publish(bundle)


def test_b_publish_runs_outside_the_write_lock_and_after_the_claim_is_durable(session):
    """发布器跑起来的那一刻：写锁必须已经放掉，``in_flight`` 声明必须已经落盘。

    两件事要一起断言。只放锁不落盘的话，进程在 publish 中途崩掉，这条幂等声明
    就没了——重启后会**重复发布**。
    """
    _make_probe_table()
    account = make_account(session, account_id="acc-pub-lock")
    item = make_item(session, account)
    session.commit()
    item_id = item.id

    item_account_id = account.id
    publisher = BlockingPublisher(account.id, platform=account.platform)
    failures: list[BaseException] = []

    def publish_round() -> None:
        s = _fresh_session()
        try:
            fresh_item = s.get(ContentItem, item_id)
            assert fresh_item is not None
            publish_with_idempotency(
                s, fresh_item, publisher, account=s.get(Account, item_account_id)
            )
            s.commit()
        except BaseException as exc:
            failures.append(exc)
            publisher.entered.set()
        finally:
            s.close()

    worker = threading.Thread(target=publish_round, name="publish-round")
    worker.start()
    try:
        assert publisher.entered.wait(timeout=30), "发布器没被调用"
        blocked = _independent_write(31)
        observer = _fresh_session()
        try:
            claimed = observer.scalars(
                select(PublishRecord).where(PublishRecord.content_item_id == item_id)
            ).all()
            seen_status = observer.scalar(
                select(ContentItem.status).where(ContentItem.id == item_id)
            )
        finally:
            observer.close()
    finally:
        publisher.release.set()
        worker.join(timeout=30)

    assert not failures, f"发布线程炸了：{failures[0]!r}"
    assert blocked is None, f"publish 期间写锁仍被握着：{blocked}"
    assert len(claimed) == 1, "publish 开跑时幂等声明还没落盘"
    assert claimed[0].phase == PublishPhase.IN_FLIGHT.value
    assert seen_status == ContentStatus.PUBLISHING.value


class DyingPublisher(FakePublisher):
    """``publish`` 进行到一半进程就没了。

    用 ``SystemExit``（``BaseException``，不是 ``Exception``）来演：它会绕开
    ``publish_with_idempotency`` 里那三个 ``except``，谁都来不及收拾——这正是被
    ``kill`` / OOM / 断电时的样子。用普通异常演不出来，那几个分支会把结局写好。
    """

    def publish(self, bundle: ContentBundle) -> PublishResult:
        self.publish_calls += 1
        raise SystemExit("进程在发布中途没了")


def test_b_the_claim_survives_a_crash_in_the_middle_of_publishing(session):
    """进程在 publish 中途崩掉：声明必须还在，重启后不会当成"没发过"再发一次。

    崩溃之后那一半事务永远不会提交——只 ``flush`` 的话，幂等声明与
    ``item.status`` 一起回到"从没发起过"，下一轮 ``tick_scheduled_publish``
    会把同一条内容**再发一次**，而且因为幂等记录也没了，连 ``reconcile``
    这道兜底都不会被调用。
    """
    account = make_account(session, account_id="acc-pub-crash")
    item = make_item(session, account)
    session.commit()
    item_id = item.id

    publisher = DyingPublisher(account.id, platform="xhs")
    crashing = _fresh_session()
    try:
        fresh_item = crashing.get(ContentItem, item_id)
        assert fresh_item is not None
        with pytest.raises(SystemExit):
            publish_with_idempotency(crashing, fresh_item, publisher)
    finally:
        crashing.rollback()  # ← 进程没了，这一半事务永远不会提交
        crashing.close()

    survivor = _fresh_session()
    try:
        records = survivor.scalars(
            select(PublishRecord).where(PublishRecord.content_item_id == item_id)
        ).all()
        status = survivor.scalar(select(ContentItem.status).where(ContentItem.id == item_id))
    finally:
        survivor.close()

    assert publisher.publish_calls == 1
    assert len(records) == 1, "幂等声明随崩溃一起没了 → 重启后会重复发布"
    assert records[0].phase == PublishPhase.IN_FLIGHT.value
    assert status == ContentStatus.PUBLISHING.value, (
        "内容退回了 scheduled，下一轮 tick 会把同一条再发一次"
    )


def test_b_failure_state_is_durable_even_if_the_caller_rolls_back(session):
    """异常路径改的 ``phase=failed`` / ``retrying`` 也必须落盘。

    声明是在 publish 之前就 commit 掉的，那么"这条声明最后怎么了"就必须同样持久——
    否则崩溃或外层回滚会留下一条永远 ``in_flight`` 的孤儿声明，比修复前更糟。
    """
    account = make_account(session, account_id="acc-pub-fail")
    item = make_item(session, account)
    session.commit()
    item_id = item.id

    failing = _fresh_session()
    try:
        fresh_item = failing.get(ContentItem, item_id)
        assert fresh_item is not None
        with pytest.raises(RetryableError):
            publish_with_idempotency(
                failing,
                fresh_item,
                FakePublisher(account.id, platform="xhs", raise_exc=RetryableError("502")),
                max_attempts=3,
            )
    finally:
        failing.rollback()  # 外层（调度器 session_scope）遇到别的异常整体回滚
        failing.close()

    survivor = _fresh_session()
    try:
        record = survivor.scalars(
            select(PublishRecord).where(PublishRecord.content_item_id == item_id)
        ).one()
        status = survivor.scalar(select(ContentItem.status).where(ContentItem.id == item_id))
    finally:
        survivor.close()

    assert record.phase == PublishPhase.FAILED.value
    assert record.attempts == 1
    assert status == ContentStatus.RETRYING.value


def test_b_dead_letter_is_durable_even_if_the_caller_rolls_back(session):
    """兜底分支（``PermanentError`` 及一切未分类异常）的死信结局同样要落盘。

    和可重试分支分开钉：那一支归 ``RetryableError``，这一支是另一段代码，漏一个
    就会留下一条永远 ``in_flight`` 的孤儿声明。
    """
    account = make_account(session, account_id="acc-pub-dead")
    item = make_item(session, account)
    session.commit()
    item_id = item.id

    failing = _fresh_session()
    try:
        fresh_item = failing.get(ContentItem, item_id)
        assert fresh_item is not None
        with pytest.raises(PermanentError):
            publish_with_idempotency(
                failing,
                fresh_item,
                FakePublisher(account.id, platform="xhs", raise_exc=PermanentError("标题违规")),
            )
    finally:
        failing.rollback()
        failing.close()

    survivor = _fresh_session()
    try:
        record = survivor.scalars(
            select(PublishRecord).where(PublishRecord.content_item_id == item_id)
        ).one()
        status = survivor.scalar(select(ContentItem.status).where(ContentItem.id == item_id))
    finally:
        survivor.close()

    assert record.phase == PublishPhase.FAILED.value
    assert status == ContentStatus.DEAD_LETTER.value


def test_b_success_is_durable_even_if_the_caller_rolls_back(session):
    """发出去了就得记牢：``published`` 与 ``platform_post_id`` 不能被外层回滚吃掉。

    声明是在碰平台之前 commit 的，结局却跟着外层事务走的话，一次回滚会把一条
    **已经在平台上活着**的内容退回成 ``in_flight`` 孤儿声明——比修复前更糟。
    """
    account = make_account(session, account_id="acc-pub-ok")
    item = make_item(session, account)
    session.commit()
    item_id = item.id

    publishing = _fresh_session()
    try:
        fresh_item = publishing.get(ContentItem, item_id)
        assert fresh_item is not None
        result = publish_with_idempotency(
            publishing, fresh_item, FakePublisher(account.id, platform="xhs")
        )
        assert result.ok
    finally:
        publishing.rollback()  # 同一轮里别的内容炸了，外层整体回滚
        publishing.close()

    survivor = _fresh_session()
    try:
        record = survivor.scalars(
            select(PublishRecord).where(PublishRecord.content_item_id == item_id)
        ).one()
        status = survivor.scalar(select(ContentItem.status).where(ContentItem.id == item_id))
    finally:
        survivor.close()

    assert record.phase == PublishPhase.DONE.value
    assert record.platform_post_id
    assert status == ContentStatus.PUBLISHED.value


def test_b_reconcile_also_runs_outside_the_write_lock(session):
    """重投前的平台侧对账同样是网络调用，同样不许在锁里跑。

    ``reconcile`` 走的是"幂等键已存在"的分支，比 ``publish`` 更早，很容易被漏掉。

    这里刻意复现调度器的真实形状：``tick_scheduled_publish`` / ``tick_retry_sweep``
    是**一个 session_scope 扫一批内容**，同一轮里前面那条内容已经写过库（限频记录、
    ``confirm_pushed_at``、上一条的发布记录……）。所以轮到这一条时，写锁早就在这条
    session 手里了——除非在碰平台之前把它交出去。
    """
    _make_probe_table()
    account = make_account(session, account_id="acc-reconcile")
    item = make_item(session, account, status=ContentStatus.RETRYING.value)
    session.commit()
    item_id = item.id

    seen: list[str | None] = []

    class ReconcileProbe(FakePublisher):
        def reconcile(self, bundle: ContentBundle) -> PublishResult | None:
            seen.append(_independent_write(41))
            return None

    first = _fresh_session()
    try:
        fresh_item = first.get(ContentItem, item_id)
        assert fresh_item is not None
        # 先让它留下一条 in_flight 声明（publish 抛错 → phase=failed，键还在）
        with pytest.raises(RetryableError):
            publish_with_idempotency(
                first,
                fresh_item,
                FakePublisher(account.id, platform="xhs", raise_exc=RetryableError("502")),
                max_attempts=5,
            )
        first.commit()
    finally:
        first.close()

    second = _fresh_session()
    try:
        retry_item = second.get(ContentItem, item_id)
        assert retry_item is not None
        # 同一轮里"前面那条内容"留下的写：写锁此刻在 second 手里
        make_account(second, account_id="acc-earlier-in-the-same-round")
        assert _independent_write(42) is not None, "前置写没拿住锁，这条用例没测到东西"

        publish_with_idempotency(second, retry_item, ReconcileProbe(account.id, platform="xhs"))
        second.commit()
    finally:
        second.close()

    assert seen == [None], f"reconcile 是在写锁里跑的：{seen}"


# ------------------------------------------------- 死信通知：循环里的累积效应


def test_d_dead_letter_notifications_do_not_hold_the_write_lock() -> None:
    """``tick_retry_sweep`` 的死信分支：**第 2 条起**的通知不许在写锁里发。

    这是"第一条写攥住整库锁、之后每一条都在锁里"的同一个形状——和
    ``tick_render_jobs`` 那处一样，只不过这里锁住的是 ``Notifier.send`` 的
    HTTP（``core/notify.py`` 默认 ``timeout=5.0``）。单条 5 秒看着不起眼，但
    ``max_items`` 默认 20：webhook 一挂，最坏就是 ~95 秒握着整库写锁，和公众号
    发布轮询超时同一量级。而 webhook 超时与"大批内容进死信"高度相关——真出事
    的时候两者同时发生。

    探针放在 ``send`` 里，量的是**发通知那一刻**能不能写库，不靠 sleep 猜时序。
    """
    from datetime import UTC, datetime, timedelta

    from core.config import get_settings
    from core.scheduler import tick_retry_sweep

    _make_probe_table()
    held: list[str | None] = []

    class ProbingNotifier:
        def send(self, title: str = "", text: str = "", level: str = "info", **kw: object) -> bool:
            held.append(_independent_write(len(held)))
            return True

    old = datetime.now(UTC) - timedelta(hours=get_settings().sw_retry_max_age_hours + 24)
    with db.session_scope() as session:
        account = make_account(session, account_id="acc-deadletter")
        for i in range(3):
            item = make_item(session, account, status=ContentStatus.RETRYING.value)
            item.created_at = old
            session.add(
                PublishRecord(
                    id=new_id("pub"),
                    content_item_id=item.id,
                    idem_key=f"idem-deadletter-{i}",
                    phase=PublishPhase.FAILED.value,
                    attempts=3,
                    created_at=old,
                    updated_at=old,
                )
            )

    stats = tick_retry_sweep(notifier=ProbingNotifier(), now=datetime.now(UTC))

    assert stats["dead_letter"] == 3, stats
    assert held == [None, None, None], f"死信通知是在写锁里发的：{held}"
