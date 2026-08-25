"""测试夹具：每个用例一套独立的临时 SQLite 库 + 干净的注册表与通知器。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from core import db
from core.config import reload_settings
from core.models import Account, ContentItem, PublishRecord, new_id, utcnow
from core.notify import NOTIFY_THROTTLE, LogNotifier, set_default_notifier
from core.ratelimit import RATE_LIMITER
from core.sms_inbox import SMS_INBOX
from core.telegram import reset_telegram_channel
from generation import imagegen
from publishers.base import ContentBundle, MediaAsset
from publishers.registry import reset_registry, use_fake_publishers


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch) -> Iterator[None]:
    monkeypatch.setenv("SW_DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("SW_ENV", "test")
    monkeypatch.setenv("SW_USE_FAKE_PUBLISHERS", "true")
    # 测试自己造账号，不要让启动钩子把 accounts.yaml 灌进来（id 会撞车）。
    # 同步本身另有 tests/test_accounts.py 专门覆盖
    monkeypatch.setenv("SW_SYNC_ACCOUNTS_ON_START", "false")
    # 台账指到临时副本。P10 起「新建账号」会**回写**台账，绝不能让一条用例
    # 把仓库里那份 accounts.yaml 改了
    ledger = tmp_path / "accounts.yaml"
    ledger.write_text("# 测试用临时台账（tests/conftest.py 生成）\naccounts:\n", encoding="utf-8")
    monkeypatch.setenv("SW_ACCOUNTS_FILE", str(ledger))
    # sidecar 驱动一律 none：单测里绝不真调 docker（docker 分支全部 mock）
    monkeypatch.setenv("SW_SIDECAR_DRIVER", "none")
    # 别让后台线程在用例中间偷偷改数据库
    monkeypatch.setenv("SW_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("SW_MAX_PUBLISH_ATTEMPTS", "3")
    monkeypatch.setenv("SW_MIN_PUBLISH_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("FEISHU_WEBHOOK", "")
    # Telegram **全局关掉**，理由和生图那条一样：仓库根的 .env 里有真 token，
    # pydantic-settings 会读它。不关的话某条用例一不小心就会真发消息 / 真起轮询线程。
    # 要测 Telegram 的用例自己 monkeypatch 打开并用 respx 挡住出网
    monkeypatch.setenv("SW_TELEGRAM_ENABLED", "false")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    monkeypatch.setenv("SW_TELEGRAM_ALLOWED_USER_IDS", "")
    monkeypatch.setenv("SW_TELEGRAM_SIGNING_SECRET", "")
    monkeypatch.setenv("SW_PUBLIC_BASE_URL", "")
    monkeypatch.setenv("SW_TELEGRAM_STATE_FILE", str(tmp_path / "telegram_state.json"))
    monkeypatch.setenv("DAILY_TOKEN_BUDGET", "1000")
    monkeypatch.setenv("DAILY_RENDER_SECONDS_BUDGET", "100")
    monkeypatch.setenv("DAILY_IMAGE_BUDGET", "10")
    # 生图默认**全局关掉**：仓库根的 .env 里有真 key，pydantic-settings 会读它，
    # 不关的话某条用例一不小心就会真调网关烧钱。要测生图的用例自己
    # monkeypatch 打开并用 respx 挡住出网（见 tests/generation/test_imagegen.py）
    monkeypatch.setenv("SW_IMAGEGEN_ENABLED", "false")
    monkeypatch.setenv("SW_IMAGEGEN_API_KEY", "sk-imagegen-test")
    monkeypatch.setenv("SW_IMAGEGEN_BASE_URL", "https://imagegen.test/v1")
    reload_settings()
    # 会话级熔断标记是模块级单例，会跨用例串味
    imagegen.reset_availability()
    db.configure()
    db.init_db()
    set_default_notifier(LogNotifier())
    # 通知节流与 Telegram 通道都是模块级单例，会跨用例串味
    NOTIFY_THROTTLE.reset()
    reset_telegram_channel()
    reset_registry()
    SMS_INBOX.clear()
    # 共享限频器是模块级单例，进程内计数会跨用例串味（上一条用例发过的账号，
    # 下一条用例一上来就"今天已发 N 条"）
    RATE_LIMITER.reset()
    yield
    RATE_LIMITER.reset()
    NOTIFY_THROTTLE.reset()
    set_default_notifier(None)
    reset_telegram_channel()
    reset_registry()
    imagegen.reset_availability()
    reload_settings()


@pytest.fixture
def session() -> Iterator[Session]:
    s = db.get_session_factory()()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def notifier() -> LogNotifier:
    n = LogNotifier()
    set_default_notifier(n)
    return n


@pytest.fixture
def client() -> Iterator[TestClient]:
    from core.main import create_app

    with TestClient(create_app()) as c:
        yield c


# ------------------------------------------------------------------ 造数工具

#: 关掉「发布前人工确认」的账号 extra。
#:
#: P12 起 ``confirm_required`` 默认**开**（合规底线，见 docs/POLICY.md），于是所有
#: "排期到点就该发出去"的老用例都会被人工确认闸门拦下。那些用例考的是限频 / 时段窗口 /
#: 重试退避，不是确认闸门——显式关掉这个开关，比给每条内容偷偷打一个 ``confirmed_at``
#: 更贴近它们真正想验的东西，也不会把闸门在测试里整体架空
#: （闸门本身由 tests/test_confirm_gate.py 专门盯着）。
NO_CONFIRM: dict[str, Any] = {"confirm_required": False}


def make_account(
    session: Session,
    *,
    account_id: str = "acc-1",
    platform: str = "xhs",
    status: str = "ok",
    daily_limit: int = 10,
    extra: dict | None = None,
) -> Account:
    """造一个账号。``extra`` 走 ``Account.extra``（时区、发布窗口、最小间隔…），
    时区相关的用例（限频按本地日切）靠它注入 ``{"timezone": ...}``。"""
    account = Account(
        id=account_id,
        platform=platform,
        name=f"测试账号 {account_id}",
        status=status,
        sidecar_endpoint="http://localhost:18060",
        daily_limit=daily_limit,
        extra=dict(extra or {}),
    )
    session.add(account)
    session.flush()
    return account


def make_bundle(
    *,
    item_id: str,
    account_id: str = "acc-1",
    platform: str = "xhs",
    title: str = "测试标题",
    body: str = "测试正文",
) -> ContentBundle:
    return ContentBundle(
        id=item_id,
        account_id=account_id,
        platform=platform,
        title=title,
        body_markdown=body,
        media=[MediaAsset(path="data/demo/cover.png", kind="image", cover=True)],
        tags=["测试"],
    )


def make_item(
    session: Session,
    account: Account,
    *,
    status: str = "scheduled",
    title: str = "测试标题",
    scheduled_in_minutes: int | None = -1,
    now: datetime | None = None,
) -> ContentItem:
    """``scheduled_in_minutes`` 默认相对真实墙钟（``utcnow()``）计算 ``scheduled_at``。

    用例如果自己钉了一个固定的 ``NOW`` 锚点去跟 ``tick_scheduled_publish(now=NOW)``
    这类显式传入的时刻比较，就必须把同一个锚点通过 ``now=`` 传进来——否则
    ``scheduled_at`` 会漂在真实的"此刻"上，日历一过锚点就跟断言脱节
    （P12.1 教训：见 tests/test_confirm_gate.py 顶部的 NOW）。
    """
    item_id = new_id("itm")
    bundle = make_bundle(
        item_id=item_id, account_id=account.id, platform=account.platform, title=title
    )
    anchor = now if now is not None else utcnow()
    item = ContentItem(
        id=item_id,
        account_id=account.id,
        status=status,
        bundle_json=bundle.model_dump(mode="json"),
        scheduled_at=(
            None
            if scheduled_in_minutes is None
            else anchor + timedelta(minutes=scheduled_in_minutes)
        ),
    )
    session.add(item)
    session.flush()
    return item


def make_publish_record(
    session: Session,
    account_id: str,
    *,
    at: datetime,
    phase: str = "done",
    platform: str = "xhs",
) -> PublishRecord:
    """造一条"已发出去"的记录（连带它的内容项），时刻精确落在 ``at``。

    ``PublishRecord.updated_at`` 上挂着 ``onupdate=utcnow``，flush 之后必须按需求值
    写回，否则时间戳会跳到"真实当下"——跨日界的用例就全变成碰运气了。
    """
    from core.state_machine import ContentStatus

    item_id = new_id("itm")
    bundle = make_bundle(item_id=item_id, account_id=account_id, platform=platform)
    session.add(
        ContentItem(
            id=item_id,
            account_id=account_id,
            status=ContentStatus.PUBLISHED.value,
            bundle_json=bundle.model_dump(mode="json"),
        )
    )
    record = PublishRecord(
        id=new_id("pub"),
        content_item_id=item_id,
        idem_key=new_id("idem"),
        phase=phase,
        attempts=1,
        created_at=at,
        updated_at=at,
    )
    session.add(record)
    session.flush()
    record.updated_at = at
    session.flush()
    return record


@pytest.fixture
def fake_registry() -> None:
    use_fake_publishers()
