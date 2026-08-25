"""数据库引擎与会话管理（SQLAlchemy 2.x + SQLite）。"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from core.config import get_settings

logger = logging.getLogger("social_workflow.db")

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None
_configured_url: str | None = None

#: SQLite 写者的等锁时长（毫秒）。**per-connection**，所以设在 ``connect``
#: 钩子里，每条新连接都要重设一遍。
#:
#: 先说清一件容易搞错的事：**裸 SQLite** 的默认 ``busy_timeout`` 是 0（写锁被
#: 占时当场返回 ``SQLITE_BUSY`` / "database is locked"），但本仓走的是 pysqlite，
#: CPython ``sqlite3.connect(timeout=5.0)`` 的默认参数会被翻成
#: ``busy_timeout=5000``。也就是说 **这条 pragma 不改变今天的行为**——它把一个
#: 只活在 CPython 默认参数里、仓里一处没写下来也没有任何用例盯着的值，变成显式
#: 的、有用例钉住的值。换驱动（aiosqlite）、有人往 ``connect_args`` 里塞
#: ``timeout``、或 CPython 改默认，都不会再静默地把等锁时间抹成 0。
#: **别当死代码删掉**：删了值不变，但保证没了。
#:
#: 取 5 秒的依据（两头都有锚，不是随手写的数字）：
#: - 下界：实测本仓的**纯 DB 写事务**——单条 ``INSERT`` + ``COMMIT`` p99≈0.09ms，
#:   1000 行一个事务≈0.8ms。5 秒是它的四个数量级，"两个写者都只做 DB"这种正常
#:   争用必然在超时内拿到锁。
#: - 上界：``core/scheduler.py`` 的 ``create_scheduler`` 里最密的 tick
#:   （``scheduled_publish`` / ``confirm_gate`` / ``render_jobs``）是 1 分钟一轮
#:   且 ``max_instances=1``。等锁时间是直接加在 tick 耗时上的，逼近 60 秒就会让
#:   下一轮被静默跳过。5 秒≈一轮间隔的 1/12，等锁对排期不可见。
#:
#: **它解决不了什么**（有了这条不等于并发安全了）：``busy_timeout`` 只处理"等一
#: 下就能拿到锁"。把网络调用夹在**已经握着写锁**的事务里，撞上它的写者等满 5 秒
#: 照样 ``database is locked``——这类事务的时长上界是 ``llm_timeout_seconds=600``
#: / ``sw_imagegen_timeout_seconds=180`` / ``douyin_publish_timeout_seconds=1200``
#: 这一档，比任何合理的 busy_timeout 大一到两个数量级。修法是把网络调用挪出写事务，
#: 不是把这个数调大——调大只会把"快速失败"换成"卡住几分钟再失败"（假死），顺带让
#: 1 分钟一轮的 tick 漏跑。
#:
#: **四处都挪完了**，由 tests/test_write_lock_boundaries.py 逐处盯着（探针是一条
#: ``timeout=0`` 的独立连接，锁被占着就当场报错，不靠 sleep 猜时序）：
#:
#: - P16.2 ``BudgetGuard.charge()`` 改走独立连接并当场 commit；
#: - P16.2 ``publish_with_idempotency`` 在 ``reconcile`` / ``publish`` 之前 commit；
#: - P16.3 ``generation/video_pipeline.py`` 的 ``RenderJob`` 每次状态写入当场 commit
#:   （见那边的 ``_settle``），管线等待循环与 ``tick_render_jobs`` 的轮询、下载都在锁外；
#: - P16.3 ``core/dev_flow.py`` 的 ``_persist_and_review`` 先把 draft commit 再跑机器审核。
#:
#: 实测口径（同一条用例，只把对应文件换回旧版）：修复前 ``tick_generate`` 一轮 6 次
#: LLM 调用有 5 次在锁里、MPT 轮询 2/2 在锁里、``tick_render_jobs`` 从第 2 个 job 起
#: 在锁里；修复后全部为 0。
#:
#: 这四处**每一处都改了事务边界的语义**（"这笔账 / 这条声明 / 这个远端任务 / 这份草稿
#: 要不要先落盘"），不是纯粹的搬移。后两处的崩溃残留各有回收路径：渲染侧走现成的
#: ``tick_render_jobs``；发布侧与审核侧是新加的 ``recover_stale_publishing`` /
#: ``recover_stale_drafts``（都挂在 ``tick_retry_sweep`` 上）。**再往这几条链里加
#: flush 之前先读那几个函数**——放锁和"崩了之后谁来收"是一件事的两面。
#:
#: 要按部署覆盖就用 ``SW_DATABASE_URL`` 的 ``?timeout=<秒>``：pysqlite 直接把它
#: 变成本连接的 busy_timeout，下面的钩子会让路。**不另开配置键**——同一个数两个
#: 入口一定会漂。那条路由 tests/test_metric_availability.py 的
#: ``test_sqlite_url_timeout_is_not_overridden_globally`` 盯着。
SQLITE_BUSY_TIMEOUT_MS = 5_000


def _ensure_sqlite_dir(url: str) -> None:
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return
    path = url[len(prefix) :]
    if path in ("", ":memory:") or path.startswith(":memory:"):
        return
    parent = Path(path).expanduser().resolve().parent
    parent.mkdir(parents=True, exist_ok=True)


def configure(database_url: str | None = None) -> Engine:
    """（重新）创建引擎。测试用临时库时显式调用。"""
    global _engine, _session_factory, _configured_url
    url = database_url or get_settings().sw_database_url
    if _engine is not None:
        _engine.dispose()
    _ensure_sqlite_dir(url)
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, future=True, connect_args=connect_args)
    if url.startswith("sqlite"):
        # URL 自带 ``?timeout=<秒>`` 时 pysqlite 已按它设好 busy_timeout，这里让路：
        # 部署侧的显式覆盖不该被一个默认值悄悄吃掉
        url_sets_timeout = any(key.lower() == "timeout" for key in engine.url.query)

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            if not url_sets_timeout:
                # per-connection，必须每条新连接都设，见 SQLITE_BUSY_TIMEOUT_MS
                cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
            cursor.close()

    _engine = engine
    _session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    _configured_url = url
    return engine


def get_engine() -> Engine:
    if _engine is None:
        configure()
    assert _engine is not None
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    if _session_factory is None:
        configure()
    assert _session_factory is not None
    return _session_factory


def current_url() -> str | None:
    return _configured_url


#: 老库补列清单：``表名 → [(列名, DDL 类型)]``。
#: ``create_all`` 只建**不存在的表**，对已存在的表加字段是 no-op——老库启动后第一次
#: 查询就会 ``no such column``。P0 决定不引入 Alembic，所以这里用最小的幂等补丁：
#: ``PRAGMA table_info`` 看一眼，缺了才 ``ALTER TABLE ADD COLUMN``。
#:
#: 只允许**加可空列**。改类型 / 删列 / 加约束一律不走这条路——SQLite 那几个
#: 需要重建表的操作放在这里太危险，真要做就单开一个带备份的运维脚本。
ADDITIVE_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "content_items": [
        # P12 发布前人工确认闸门
        ("confirmed_at", "DATETIME"),
        ("confirm_ref", "VARCHAR(128)"),
        ("confirm_pushed_at", "DATETIME"),
    ],
}


def _existing_columns(conn: Any, table: str) -> set[str]:
    from sqlalchemy import inspect

    inspector = inspect(conn)
    if table not in inspector.get_table_names():
        return set()
    return {col["name"] for col in inspector.get_columns(table)}


def ensure_columns(engine: Engine | None = None) -> list[str]:
    """给老库补上 :data:`ADDITIVE_COLUMNS` 里缺失的列。返回实际补了哪些。

    幂等：已经有的列不动。新库走 ``create_all`` 就已经带上了，这里什么都不做。
    """
    from sqlalchemy import text

    target = engine or get_engine()
    added: list[str] = []
    with target.begin() as conn:
        for table, columns in ADDITIVE_COLUMNS.items():
            present = _existing_columns(conn, table)
            if not present:  # 表还不存在（create_all 会建），没什么可补的
                continue
            for name, ddl_type in columns:
                if name in present:
                    continue
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type}"))
                added.append(f"{table}.{name}")
    if added:
        logger.info("已给老库补列：%s", ", ".join(added))
    return added


def init_db() -> None:
    """建表（P0 不引入 Alembic，SQLite 直接 create_all）+ 给老库补新增的可空列。"""
    from core import models  # 延迟导入，确保所有表已注册

    models.Base.metadata.create_all(get_engine())
    ensure_columns()


def drop_db() -> None:
    from core import models

    models.Base.metadata.drop_all(get_engine())


@contextmanager
def session_scope() -> Iterator[Session]:
    """事务边界：正常提交，异常回滚。"""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI 依赖。"""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
