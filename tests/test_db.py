"""``core/db.py`` 的连接级 pragma 与写锁等待行为。

这里只钉"连接真的处于什么状态"——读回 ``PRAGMA busy_timeout`` 的实际值、以及被
写锁挡住的写者到底是等还是当场炸。断言"执行过某个 pragma 字符串"挡不住任何东西
（字符串写错、只在第一条连接上设、被 URL 参数覆盖，三种都照样绿）。
"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest
from sqlalchemy import text

from core import db


def test_every_new_sqlite_connection_reads_back_the_configured_busy_timeout(tmp_path):
    """两条独立连接各读一次真实 pragma 值。

    ``busy_timeout`` 是 per-connection 的：设在 ``configure()`` 里只跑一次的地方
    时第一条连接会是对的、第二条是驱动默认值——所以必须同时持有两条再读。
    """
    engine = db.configure(f"sqlite:///{tmp_path / 'busy.db'}")
    with engine.connect() as first, engine.connect() as second:
        assert first.connection.dbapi_connection is not second.connection.dbapi_connection
        assert first.scalar(text("PRAGMA busy_timeout")) == db.SQLITE_BUSY_TIMEOUT_MS
        assert second.scalar(text("PRAGMA busy_timeout")) == db.SQLITE_BUSY_TIMEOUT_MS


def test_busy_timeout_stays_between_its_two_anchors():
    """光钉"读回来等于常量"拦不住有人把常量改成 0 或十分钟，这里钉取值区间。

    下界 1 秒：实测本仓纯 DB 写事务 p99 < 0.1ms，再低就没有余量可言。
    上界 30 秒：最密的 tick 是 1 分钟一轮且 ``max_instances=1``（见
    ``core/scheduler.py`` 的 ``create_scheduler``），等锁逼近一轮间隔就会让下一轮
    被静默跳过——那是把快速失败换成假死，比报错更难查。
    """
    assert 1_000 <= db.SQLITE_BUSY_TIMEOUT_MS <= 30_000


def test_blocked_writer_waits_for_the_held_write_lock_instead_of_failing(tmp_path):
    """真实争用：写锁被另一个连接占着时，本引擎的写者要等，不能当场 ``database is locked``。

    不用 sleep 猜时序——锁的持有与释放都由本用例自己控制：
    ``BEGIN IMMEDIATE`` 拿锁、后台线程固定 0.2 秒后 commit 放锁，而等锁窗口是 5 秒，
    两者差 25 倍，不存在"机器慢一点就红"的余地。
    """
    path = tmp_path / "contended.db"
    engine = db.configure(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE probe (id INTEGER PRIMARY KEY)"))

    hold_seconds = 0.2
    holder = sqlite3.connect(path, check_same_thread=False)
    try:
        holder.execute("BEGIN IMMEDIATE")  # WAL 下这一句就拿到了唯一的写锁
        holder.execute("INSERT INTO probe VALUES (1)")

        # 反证：等锁时长为 0 的连接当场炸。证明锁**真的**被占住了，下面那条写成功
        # 不是因为压根没冲突——少了这一段，整个用例可能在无争用的情况下绿着
        impatient = sqlite3.connect(path, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                impatient.execute("INSERT INTO probe VALUES (2)")
        finally:
            impatient.close()

        releaser = threading.Thread(target=_commit_after, args=(holder, hold_seconds))
        started = time.perf_counter()
        releaser.start()
        with engine.begin() as conn:  # 阻塞在这里，直到 holder 放锁
            conn.execute(text("INSERT INTO probe VALUES (3)"))
        waited = time.perf_counter() - started
        releaser.join(timeout=5)
    finally:
        holder.close()

    assert waited >= hold_seconds, "没等就写进去了 = 这一轮压根没发生争用，用例没测到东西"
    assert waited < db.SQLITE_BUSY_TIMEOUT_MS / 1000
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM probe")) == 2


def _commit_after(conn: sqlite3.Connection, seconds: float) -> None:
    time.sleep(seconds)
    conn.commit()
