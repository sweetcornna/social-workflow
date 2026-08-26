#!/usr/bin/env python
"""端到端验收：一条内容**零干预**从热榜走到 ``measured``，同时钉住 R1 闸门仍然拦得住。

    uv run python scripts/acceptance_full_chain.py            # 用真实 LLM（要 .env 里的 key）
    uv run python scripts/acceptance_full_chain.py --offline  # 降级到 ScriptedLLM，不打网络

为什么这个脚本要存在，而不是"跑一遍 tests/test_full_chain.py 就够了"：那两条用例跑在
pytest 的夹具里，LLM、发布器、指标全是假的，证的是**代码接得上**。这里证的是另一件事——
按一份接近部署形态的配置（真实 settings、真实 scheduler tick 顺序、真实生成链）从头走一遍，
中途**不碰任何一条记录的状态**。手工 ``UPDATE`` 出来的 ``published`` 证明不了链路走得通，
而那正是本脚本唯一要证的事。

两个账号，缺一不可：

  accept-auto   ``confirm_required=False`` —— 它证明"**无干预**跑完全流程"：
                采集 → 出稿 → 机器审核 → autopilot 批准 → 排期 → 发布 → 回流 → measured。
  accept-gated  ``confirm_required=True``（生产的默认形态）—— 它证明红线 R1 **还在**：
                同样一路自动走到 ``scheduled``，然后**停住**，``skipped_unconfirmed`` 记它一笔。
                少了这一半，"无干预跑通"就可能是"闸门被谁悄悄关了"的同义词。

隔离与安全：临时库、临时媒体目录、``FakePublisher``、``SW_TELEGRAM_ENABLED=false``。
**一个字节都不会发到平台上，也不会往任何人手机上推确认卡。**
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
import tempfile
import traceback

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _bootstrap(offline: bool) -> str:
    """在 import core.* **之前**把环境钉死——settings 是 import 期读的，晚了就不生效。"""
    tmp = tempfile.mkdtemp(prefix="sw-acceptance-")
    os.environ["SW_DATABASE_URL"] = f"sqlite:///{tmp}/acceptance.db"
    os.environ["SW_MEDIA_ROOT"] = f"{tmp}/media"
    # 三道保险，任何一道都足以让本脚本碰不到真实世界：
    os.environ["SW_USE_FAKE_PUBLISHERS"] = "true"  # 发布走 FakePublisher
    os.environ["SW_TELEGRAM_ENABLED"] = "false"  # 不往真人手机推确认卡
    os.environ["SW_SYNC_ACCOUNTS_ON_START"] = "false"  # 不读、更不写真实台账
    os.environ["SW_SCHEDULER_ENABLED"] = "false"  # tick 由本脚本按顺序驱动，不并发
    # 台账指向临时文件：就算上面那行哪天被改掉，也碰不到真实的 accounts.yaml。
    os.environ["SW_ACCOUNTS_FILE"] = f"{tmp}/accounts.yaml"
    pathlib.Path(f"{tmp}/accounts.yaml").write_text("accounts: []\n", encoding="utf-8")
    # 【预算必须自己钉死，不能跟着环境走】这个脚本会被从各种 shell 里跑起来，其中一种是
    # pytest 的子进程——而 tests/conftest.py 把 DAILY_TOKEN_BUDGET 设成 1000（够一条稿、
    # 不够两条）。跟着环境走的后果是第二个账号被 skipped_budget 挡掉，于是"闸门开着要停住"
    # 那一半退化成"它根本没生成过内容"——一次假绿，而且是往最要命的方向假。
    # 取产品默认值（core/config.py:370 起那三行）。
    os.environ["DAILY_TOKEN_BUDGET"] = "2000000"
    os.environ["DAILY_RENDER_SECONDS_BUDGET"] = "3600"
    os.environ["DAILY_IMAGE_BUDGET"] = "40"
    if offline:
        # 没有 key 时生成链本来就会降级到 ScriptedLLM；这里显式清掉，免得本机 .env 里
        # 恰好有 key 时 --offline 变成一句谎话。
        for key in ("ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "SW_DSH_GATEWAY_API_KEY"):
            os.environ.pop(key, None)
    sys.path.insert(0, str(REPO_ROOT))
    return tmp


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="清掉所有 LLM key，生成链降级到 ScriptedLLM；不打任何外部网络",
    )
    args = parser.parse_args()

    tmp = _bootstrap(args.offline)

    from sqlalchemy import select

    from core import db, scheduler
    from core.models import Account, ContentItem
    from core.state_machine import AccountStatus
    from publishers.registry import use_fake_publishers

    failures: list[str] = []

    def step(title: str, fn):
        print(f"=== {title} ===", flush=True)
        try:
            out = fn()
        except Exception as exc:
            failures.append(f"{title}: {type(exc).__name__}: {exc}")
            print(f"  ✗ {title}: {type(exc).__name__}: {exc}", flush=True)
            traceback.print_exc()
            return None
        print(f"  ✓ {title}: {out}", flush=True)
        return out

    def status_of(account_id: str) -> dict[str, int]:
        with db.session_scope() as session:
            rows = session.scalars(
                select(ContentItem).where(ContentItem.account_id == account_id)
            ).all()
            out: dict[str, int] = {}
            for row in rows:
                out[row.status] = out.get(row.status, 0) + 1
        return out

    db.init_db()
    use_fake_publishers()

    def seed_accounts() -> str:
        with db.session_scope() as session:
            session.add(
                Account(
                    id="accept-auto",
                    platform="xhs",
                    name="验收·无干预",
                    status=AccountStatus.OK,
                    daily_limit=10,
                    extra={"daily_target": 1, "autopilot": True, "confirm_required": False},
                )
            )
            session.add(
                Account(
                    id="accept-gated",
                    platform="xhs",
                    name="验收·闸门开着",
                    status=AccountStatus.OK,
                    daily_limit=10,
                    extra={"daily_target": 1, "autopilot": True, "confirm_required": True},
                )
            )
        return "accept-auto（闸门关）+ accept-gated（闸门开，生产默认形态）"

    step("0 建两个隔离账号（临时库，真实台账一个字节没碰）", seed_accounts)
    step("1 tick_sourcing 采集热榜填选题池", scheduler.tick_sourcing)
    step("2 tick_generate 出稿 → 机器审核 → autopilot 批准 → 排期", scheduler.tick_generate)
    publish = step(
        "3 tick_scheduled_publish 发布（闸门在这一步说话）",
        scheduler.tick_scheduled_publish,
    )
    step("4 tick_metrics 数据回流", scheduler.tick_metrics)
    step("5 tick_retry_sweep 死信 / 崩溃残留回收", scheduler.tick_retry_sweep)
    step("6 tick_render_jobs 渲染任务轮询", scheduler.tick_render_jobs)
    step("7 tick_login_health 登录态体检", scheduler.tick_login_health)
    step("8 tick_insights 复盘", scheduler.tick_insights)

    auto = status_of("accept-auto")
    gated = status_of("accept-gated")
    print(f"\naccept-auto  最终状态: {auto}", flush=True)
    print(f"accept-gated 最终状态: {gated}", flush=True)

    # ---- 判定 -------------------------------------------------------------
    # 【两条都要过，少一条这份验收就没有意义】
    #   ① 闸门关掉时**真的**能零干预走完 —— 否则"全自动"是假的；
    #   ② 闸门开着时**真的**拦得住 —— 否则 ① 的绿色只说明红线被谁关了。
    if auto.get("measured", 0) < 1:
        failures.append(f"accept-auto 没走到 measured：{auto}")
    if gated.get("scheduled", 0) < 1:
        failures.append(f"accept-gated 应当停在 scheduled 等人确认，实际：{gated}")
    if gated.get("published", 0) or gated.get("measured", 0):
        failures.append(
            f"**红线 R1 被绕过了**：confirm_required=true 的账号不该发出去，实际：{gated}"
        )
    if publish is not None and publish.get("skipped_unconfirmed", 0) < 1:
        failures.append(
            "tick_scheduled_publish 没有记下 skipped_unconfirmed —— 闸门要么没跑、要么记错了账"
        )

    print(f"\n临时目录: {tmp}", flush=True)
    if failures:
        print("失败项:", flush=True)
        for line in failures:
            print(f"  · {line}", flush=True)
        print("验收: 未通过", flush=True)
        return 1
    print("失败项: 无", flush=True)
    print(
        "验收: 通过 —— 闸门关时一条内容零干预走到 measured；闸门开时停在 scheduled，R1 拦得住",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
