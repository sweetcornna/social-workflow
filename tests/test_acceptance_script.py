"""``scripts/acceptance_full_chain.py`` 的护栏。

这个脚本是"无干预跑完全流程"这句话的**唯一可执行凭证**：谁都能跑一条命令自己看，
而不是读一段报告相信它。所以它自己也要有护栏——护栏盯的是**它还证不证得动那件事**，
不是它的输出长什么样。

两条用例分工：
  · 端到端那条真的把脚本跑起来（``--offline``，不打网络），确认两个账号各自停在该停的地方；
  · 源码那条钉住"两个方向都判"——只判一半的验收脚本会在最要命的方向上变成永真：
    闸门被谁关掉之后，"零干预跑通了"照样绿。
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "acceptance_full_chain.py"


def test_the_acceptance_script_walks_the_chain_and_still_honours_the_gate() -> None:
    """一次跑完证两件事：闸门关时走到 measured，闸门开时停在 scheduled。"""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--offline"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"验收脚本自己红了：\n{out[-3000:]}"
    # 前一半：零干预真的走完了
    assert "accept-auto  最终状态: {'measured': 1}" in out, out[-2000:]
    # 后一半：红线还在。少了它，前一半的绿色可能只是"闸门被关了"
    assert "accept-gated 最终状态: {'scheduled': 1}" in out, out[-2000:]
    assert "'skipped_unconfirmed': 1" in out, out[-2000:]
    assert "失败项: 无" in out
    assert "验收: 通过" in out


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
