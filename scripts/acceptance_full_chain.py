#!/usr/bin/env python
"""端到端验收：一条内容**零干预**从热榜走到 ``measured``，同时钉住 R1 闸门仍然拦得住。

    uv run python scripts/acceptance_full_chain.py            # 用真实 LLM（要 .env 里的 key）
    uv run python scripts/acceptance_full_chain.py --offline  # 降级到 ScriptedLLM，不打网络
    uv run python scripts/acceptance_full_chain.py --lane xhs # 走图文赛道（要渲染链）

**渲染链是硬前置，两条赛道都一样。** 这句话是被一次 CI 红和随后的实测逼出来的：

  最早两个账号都钉死在 ``xhs``，于是这个脚本在装了 Playwright 的开发机上绿、在没装的
  CI test job 上红。第一版修法是"没有渲染链就换 wechat 纯文赛道"——实测当场推翻了它：
  autopilot 的自动批准条件是 ``block == 0 且 warn == 0``（core/scheduler.py:235），而
  封面缺失虽然对公众号只是 **warn**，一条 warn 就足以让 autopilot 不批。

  所以真实结论比"换个平台"硬得多：**没有 Playwright + chromium，任何平台都跑不出
  「无干预」**。缺渲染链时正确的行为是当场退出并说清怎么装，而不是换条路假装走通了。

  ``--lane xhs``（默认）  图文笔记。``xhs.image.missing`` 是 **block**，最严。
  ``--lane wechat``      公众号纯文。封面缺失只是 warn，但 warn 一样卡住 autopilot；
                         另外账号必须配 ``extra["author"]``，否则
                         ``inspect.platform_extra.missing`` 再补一条 warn。

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

#: 要求了 ``--lane xhs``、这台机器却没有渲染链。**不是**验收失败，是环境不满足前提，
#: 所以用一个和"验收未通过"（1）分得开的码，脚本化调用能一眼看出该装东西还是该查代码。
EXIT_RENDER_CHAIN_MISSING = 3

#: 沙盒自检没过——隔离保险掉了一条，拒跑。
EXIT_SANDBOX_UNPROVEN = 40


def _assert_sandbox(tmp: str) -> None:
    """跑之前**当场验**这五道保险还在，不在就拒跑。

    为什么这道检查非得长在脚本自己身上，而不是由调用方（scripts/ops/acceptance.sh）
    在外面 grep 一遍源码：生产是容器部署，而 docker-compose.yml 里 core **没有**把源码
    bind mount 进去（只挂 core_data 与 accounts.yaml）。真正会跑的是**镜像里烤进去的
    那一份**。在宿主机检出上 grep 等于检查了另一个文件——正常情况下两份一样，而
    "正常情况下一样"恰恰是这类护栏最常见的失效方式。

    长在进程内就没有这个缝：验的就是马上要跑的这份，验的是**环境变量的实际取值**，
    不是源码里长得像那么回事的几行字。
    """
    proofs = [
        (
            "SW_DATABASE_URL",
            lambda v: v.startswith("sqlite:///") and tmp in v,
            "库必须在临时目录里",
        ),
        ("SW_MEDIA_ROOT", lambda v: v.startswith(tmp), "媒体目录必须在临时目录里"),
        ("SW_ACCOUNTS_FILE", lambda v: v.startswith(tmp), "台账必须指向临时文件"),
        ("SW_USE_FAKE_PUBLISHERS", lambda v: v == "true", "发布必须走 FakePublisher"),
        ("SW_TELEGRAM_ENABLED", lambda v: v == "false", "不许往真人手机推确认卡"),
        ("SW_SYNC_ACCOUNTS_ON_START", lambda v: v == "false", "不许读、更不许写真实台账"),
    ]
    broken = [
        f"{name}（{why}），实际 = {os.environ.get(name, '<未设置>')!r}"
        for name, ok, why in proofs
        if not ok(os.environ.get(name, ""))
    ]
    if broken:
        print("沙盒自检没过，拒跑。以下保险不成立：", flush=True)
        for line in broken:
            print(f"  · {line}", flush=True)
        print(
            "这几条是本脚本碰不到真实数据的**全部**依据。少一条就意味着它会在真实库上"
            "跑一遍采集与发布——所以宁可不跑。",
            flush=True,
        )
        raise SystemExit(EXIT_SANDBOX_UNPROVEN)
    print(f"沙盒自检: 六道保险都成立（沙盒目录 {tmp}）", flush=True)


#: 每条赛道：账号用哪个平台、这条赛道证明了什么。**两条都要渲染链**，见模块 docstring。
LANES = {
    "xhs": ("xhs", "图文笔记链（卡片渲染，xhs.image.missing 是 block，最严）"),
    "wechat": ("wechat_mp", "公众号纯文链（封面只是 warn，但 warn 一样卡住 autopilot）"),
}

#: 公众号账号必须带的 ``platform_extra``。少了 ``author`` 只是 warn——但 autopilot 要求
#: warn 也为 0，所以对本脚本而言"只是 warn"和"发不出去"是同一件事。
LANE_ACCOUNT_EXTRA = {
    "wechat_mp": {"author": "验收机器人"},
}


def _render_chain_ready() -> tuple[bool, str]:
    """渲染链到底能不能用——**装没装库**和**浏览器起不起得来**是两回事。

    只查 ``playwright_available()`` 会漏掉最常见的那种坏法：库装了、chromium 没下载。
    那种机器上卡片照样渲不出来，而这个脚本会一路跑到判定处才报"没走到 measured"，
    把一个装机问题伪装成一个代码问题。所以这里**真起一次浏览器**。
    """
    from generation.cover import INSTALL_HINT, playwright_available

    if not playwright_available():
        return False, f"Playwright 库没装。装：{INSTALL_HINT}"
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            p.chromium.launch().close()
    except Exception as exc:  # 起不来的方式很多，一律当作"不可用"
        return (
            False,
            f"Playwright 装了，但 chromium 起不来"
            f"（{type(exc).__name__}: {exc}）。装：{INSTALL_HINT}",
        )
    return True, "Playwright + chromium 就绪"


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
    _assert_sandbox(tmp)
    return tmp


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="清掉所有 LLM key，生成链降级到 ScriptedLLM；不打任何外部网络",
    )
    parser.add_argument(
        "--lane",
        choices=sorted(LANES),
        default="xhs",
        help="走哪条平台赛道。两条都要求 Playwright + chromium",
    )
    args = parser.parse_args()

    platform, lane_desc = LANES[args.lane]
    tmp = _bootstrap(args.offline)

    print(f"赛道: --lane {args.lane} —— {lane_desc}", flush=True)
    # 【前置检查，不是降级点】没有渲染链就当场退出。换赛道躲开它只会让这个脚本在缺件的
    # 机器上照样打印"验收: 通过"，而 autopilot 在那台机器上一条稿都批不了。
    ready, detail = _render_chain_ready()
    print(f"渲染链前置检查: {detail}", flush=True)
    if not ready:
        print(
            "验收: 未跑 —— 渲染链缺件，这台机器上 autopilot 批不了任何稿子"
            "（自动批准要求 block==0 且 warn==0，封面缺失就是一条 warn）。"
            "**没有换赛道绕过去**，因为那只会把缺件伪装成通过。",
            flush=True,
        )
        return EXIT_RENDER_CHAIN_MISSING

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
                    platform=platform,
                    name="验收·无干预",
                    status=AccountStatus.OK,
                    daily_limit=10,
                    extra={
                        "daily_target": 1,
                        "autopilot": True,
                        "confirm_required": False,
                        **LANE_ACCOUNT_EXTRA.get(platform, {}),
                    },
                )
            )
            session.add(
                Account(
                    id="accept-gated",
                    platform=platform,
                    name="验收·闸门开着",
                    status=AccountStatus.OK,
                    daily_limit=10,
                    extra={
                        "daily_target": 1,
                        "autopilot": True,
                        "confirm_required": True,
                        **LANE_ACCOUNT_EXTRA.get(platform, {}),
                    },
                )
            )
        return f"accept-auto（闸门关）+ accept-gated（闸门开，生产默认形态），platform={platform}"

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
        f"验收: 通过（--lane {args.lane}, platform={platform}）"
        " —— 闸门关时一条内容零干预走到 measured；闸门开时停在 scheduled，R1 拦得住",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
