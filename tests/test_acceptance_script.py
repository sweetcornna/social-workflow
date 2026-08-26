"""``scripts/acceptance_full_chain.py`` 的护栏。

这个脚本是"无干预跑完全流程"这句话的**唯一可执行凭证**：谁都能跑一条命令自己看，
而不是读一段报告相信它。所以它自己也要有护栏——护栏盯的是**它还证不证得动那件事**，
不是它的输出长什么样。

三条用例分工：

  · 端到端那条真的把脚本跑起来（``--offline``，不打网络），两条赛道各跑一遍，
    确认两个账号各自停在该停的地方。它要求渲染链，所以打 ``render`` 标记：
    CI 的 test job 没有 chromium 会自动 skip，render-smoke job 才真跑。
  · 缺件那条把 Playwright 从 ``sys.path`` 上藏掉，钉住脚本**拒跑**而不是换条路假装跑通。
    这条不需要渲染链，任何机器上都跑——它盯的正是"没有渲染链时会发生什么"。
  · 源码那条钉住"两个方向都判"——只判一半的验收脚本会在最要命的方向上变成永真：
    闸门被谁关掉之后，"零干预跑通了"照样绿。

第二条用例的由来值得记一笔。这个脚本第一版把两个账号钉死在 ``xhs``，于是它在装了
Playwright 的开发机上绿、在没装的 CI 上红。第一次修它时我写的是"没有渲染链就换 wechat
纯文赛道"——实测当场推翻：autopilot 自动批准要求 ``block == 0 且 warn == 0``，而封面
缺失对公众号虽然只是 warn，一条 warn 就够让它不批。换赛道躲不开渲染链，只会把
"这台机器批不了任何稿子"伪装成"验收通过"。
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "acceptance_full_chain.py"

sys.path.insert(0, str(REPO_ROOT))
from scripts.acceptance_full_chain import (  # noqa: E402
    EXIT_RENDER_CHAIN_MISSING,
    EXIT_SANDBOX_UNPROVEN,
    _assert_sandbox,
    _render_chain_ready,
)


def _run(*extra: str, env_path: str | None = None) -> subprocess.CompletedProcess[str]:
    import os

    env = dict(os.environ)
    if env_path is not None:
        env["PYTHONPATH"] = env_path
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--offline", *extra],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=900,
        env=env,
    )


@pytest.mark.render
@pytest.mark.parametrize("lane", ["xhs", "wechat"])
def test_the_acceptance_script_walks_the_chain_and_still_honours_the_gate(lane: str) -> None:
    """一次跑完证两件事：闸门关时走到 measured，闸门开时停在 scheduled。"""
    ready, detail = _render_chain_ready()
    if not ready:
        pytest.skip(f"本机没有可用的渲染链：{detail}")
    proc = _run("--lane", lane)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"验收脚本自己红了：\n{out[-3000:]}"
    # 前一半：零干预真的走完了
    assert "accept-auto  最终状态: {'measured': 1}" in out, out[-2000:]
    # 后一半：红线还在。少了它，前一半的绿色可能只是"闸门被关了"
    assert "accept-gated 最终状态: {'scheduled': 1}" in out, out[-2000:]
    assert "'skipped_unconfirmed': 1" in out, out[-2000:]
    assert "失败项: 无" in out
    assert f"验收: 通过（--lane {lane}" in out


def test_missing_render_chain_refuses_instead_of_quietly_switching_lanes(tmp_path) -> None:
    """渲染链缺件时**拒跑**，而且拒得让人一眼知道该装什么。

    伪造缺件的办法：往 ``PYTHONPATH`` 前面放一个名叫 ``playwright`` 的**模块**（不是包），
    于是 ``import playwright.sync_api`` 抛 ImportError，正好走 ``playwright_available()``
    的 False 分支。不给生产代码开任何"测试用"后门——后门本身就会变成下一个假绿的入口。
    """
    (tmp_path / "playwright.py").write_text("# 冒充成非 package，令子模块 import 失败\n")
    proc = _run(env_path=str(tmp_path))
    out = proc.stdout + proc.stderr
    assert proc.returncode == EXIT_RENDER_CHAIN_MISSING, (
        f"缺渲染链应当以 {EXIT_RENDER_CHAIN_MISSING} 退出（和「验收未通过」的 1 分得开），"
        f"实际 {proc.returncode}：\n{out[-2000:]}"
    )
    assert "验收: 通过" not in out, "缺件却打印了「通过」——这正是要拦的那句谎话"
    assert "playwright install chromium" in out, "拒跑要顺手说清怎么装，否则等于把人晾在这"
    # 换赛道也一样拒：躲不开渲染链这件事对两条赛道都成立
    gated = _run("--lane", "wechat", env_path=str(tmp_path))
    assert gated.returncode == EXIT_RENDER_CHAIN_MISSING, (
        "wechat 赛道也必须拒跑：封面缺失虽然只是 warn，但 autopilot 要求 warn 也为 0"
    )


def test_the_script_would_fail_loudly_if_the_gate_were_bypassed() -> None:
    """判定必须**双向**，而且"闸门被绕过"要单独判一条。

    只判 ``measured >= 1`` 的脚本有一个致命的退化方向：谁把 confirm_required 关了，
    它照样全绿——而那恰恰是这份验收最该拦住的事。这里在源码层面钉住那三条判定都在。
    """
    src = SCRIPT.read_text(encoding="utf-8")
    assert 'auto.get("measured", 0) < 1' in src, "少了「零干预真的走完」这一判"
    assert 'gated.get("scheduled", 0) < 1' in src, "少了「闸门开着要停住」这一判"
    assert "红线 R1 被绕过了" in src, "少了「闸门开着却发出去了」这一判——最要命的那条"
    assert 'publish.get("skipped_unconfirmed", 0) < 1' in src, "少了「闸门真的记了账」这一判"
    # 三道保险一条都不许掉：它们保证这个脚本碰不到真实世界
    assert 'os.environ["SW_USE_FAKE_PUBLISHERS"] = "true"' in src
    assert 'os.environ["SW_TELEGRAM_ENABLED"] = "false"' in src
    assert 'os.environ["SW_SYNC_ACCOUNTS_ON_START"] = "false"' in src
    # 前置检查必须在**所有**赛道之前无条件跑一次。写成 if needs_render / if lane == ... 的
    # 那一刻，"某条赛道不用渲染链"这个已经被实测推翻的说法就又回来了。
    assert "if needs_render" not in src, "渲染链前置检查不许挂在某条赛道上——两条都要"
    assert "ready, detail = _render_chain_ready()" in src, "少了渲染链前置检查"
    # 沙盒自检必须在 _bootstrap 里无条件跑。它是这个脚本碰不到真实数据的唯一保证，
    # 而且必须长在进程内——运维通路在宿主机上 grep 的是另一个文件（镜像没 bind mount）。
    assert "_assert_sandbox(tmp)" in src, "少了进程内沙盒自检"


@pytest.mark.parametrize(
    "broken",
    [
        {"SW_USE_FAKE_PUBLISHERS": "false"},
        {"SW_TELEGRAM_ENABLED": "true"},
        {"SW_SYNC_ACCOUNTS_ON_START": "true"},
        {"SW_DATABASE_URL": "sqlite:////app/data/social_workflow.db"},
        {"SW_MEDIA_ROOT": "/app/data/media"},
        {"SW_ACCOUNTS_FILE": "/app/accounts.yaml"},
    ],
)
def test_sandbox_self_check_refuses_when_any_single_guard_is_broken(
    monkeypatch, tmp_path, broken: dict[str, str]
) -> None:
    """六道保险**任意一条**不成立就拒跑，而且要说出是哪一条。

    这道自检长在脚本进程里而不是长在调用方，是有具体原因的：生产是容器部署，
    docker-compose.yml 里 core 没有把源码 bind mount 进去，真正跑的是镜像里烤进去的
    那一份。运维脚本在宿主机检出上 grep 等于检查了另一个文件——两份正常情况下一样，
    而"正常情况下一样"正是这类护栏最常见的失效方式。

    参数化到每一条，是因为"六条一起掉"从来不是真实的失效方式；真实的是有人顺手删了
    其中一行。一条都不许漏网。
    """
    tmp = str(tmp_path)
    good = {
        "SW_DATABASE_URL": f"sqlite:///{tmp}/acceptance.db",
        "SW_MEDIA_ROOT": f"{tmp}/media",
        "SW_ACCOUNTS_FILE": f"{tmp}/accounts.yaml",
        "SW_USE_FAKE_PUBLISHERS": "true",
        "SW_TELEGRAM_ENABLED": "false",
        "SW_SYNC_ACCOUNTS_ON_START": "false",
    }
    for name, value in {**good, **broken}.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(SystemExit) as excinfo:
        _assert_sandbox(tmp)
    assert excinfo.value.code == EXIT_SANDBOX_UNPROVEN, "拒跑的码要和「验收未通过」分得开"


def test_sandbox_self_check_passes_when_every_guard_holds(monkeypatch, tmp_path) -> None:
    """全都成立时**不能**拦——否则上一条用例可以靠"永远拒跑"作弊过关。"""
    tmp = str(tmp_path)
    for name, value in {
        "SW_DATABASE_URL": f"sqlite:///{tmp}/acceptance.db",
        "SW_MEDIA_ROOT": f"{tmp}/media",
        "SW_ACCOUNTS_FILE": f"{tmp}/accounts.yaml",
        "SW_USE_FAKE_PUBLISHERS": "true",
        "SW_TELEGRAM_ENABLED": "false",
        "SW_SYNC_ACCOUNTS_ON_START": "false",
    }.items():
        monkeypatch.setenv(name, value)
    _assert_sandbox(tmp)
