"""``core.models`` 的 e2e 时间锚点互锁。

锚点在模块导入时解析，因此每种环境组合都必须由全新解释器验证。不能在 pytest
主进程里 reload ``core.models``：该模块同时声明 SQLAlchemy ORM 类，reload 会替换
类对象并污染共享 mapper registry，让后续测试产生与导入顺序有关的失败。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ANCHOR = "2026-08-19T11:00:00.000Z"

_SUCCESS_PROBE = """
import json
from datetime import UTC, datetime

before = datetime.now(UTC)
import core.models as models
first_now = models.utcnow()
second_now = models.utcnow()
after = datetime.now(UTC)

print(json.dumps({
    "anchor": models._E2E_TIME_ANCHOR.isoformat() if models._E2E_TIME_ANCHOR else None,
    "before": before.isoformat(),
    "first_now": first_now.isoformat(),
    "second_now": second_now.isoformat(),
    "after": after.isoformat(),
    "ids": [models.new_id("itm"), models.new_id("pub"), models.new_id("itm")],
}))
"""


def _run_probe(
    *, anchor: str | None, fake_publishers: str | None
) -> subprocess.CompletedProcess[str]:
    """在隔离解释器里导入一次 models，不触碰 pytest 进程的 ORM registry。"""
    env = os.environ.copy()
    env.pop("SW_E2E_TIME_ANCHOR", None)
    env.pop("SW_USE_FAKE_PUBLISHERS", None)
    if anchor is not None:
        env["SW_E2E_TIME_ANCHOR"] = anchor
    if fake_publishers is not None:
        env["SW_USE_FAKE_PUBLISHERS"] = fake_publishers
    return subprocess.run(
        [sys.executable, "-c", _SUCCESS_PROBE],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def _successful_probe(*, anchor: str | None, fake_publishers: str | None) -> dict[str, object]:
    completed = _run_probe(anchor=anchor, fake_publishers=fake_publishers)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_utcnow_uses_real_clock_when_anchor_unset() -> None:
    """未设锚点：行为与改动前相同，仍逐次读取 ``datetime.now(UTC)``。"""
    result = _successful_probe(anchor=None, fake_publishers=None)

    assert result["anchor"] is None
    before = datetime.fromisoformat(str(result["before"]))
    got = datetime.fromisoformat(str(result["first_now"]))
    after = datetime.fromisoformat(str(result["after"]))
    assert before <= got <= after
    assert got != datetime(2026, 8, 19, 11, 0, tzinfo=UTC)


def test_utcnow_pinned_when_anchor_set_and_fake_publishers_on() -> None:
    """锚点 + 假发布器为真：时钟钉死，且模块导入时打印 WARNING。"""
    completed = _run_probe(anchor=ANCHOR, fake_publishers="true")
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)

    expected = "2026-08-19T11:00:00+00:00"
    assert result["anchor"] == expected
    assert result["first_now"] == expected
    assert result["second_now"] == expected
    assert "SW_E2E_TIME_ANCHOR" in completed.stderr
    assert "生产" in completed.stderr


def test_new_id_is_stable_across_fresh_e2e_imports() -> None:
    """两次全新 e2e 进程（临时库重建）必须产生相同的确定序列。"""
    first = _successful_probe(anchor=ANCHOR, fake_publishers="true")
    second = _successful_probe(anchor=ANCHOR, fake_publishers="true")

    expected = [
        "itm_e2e_000000000001",
        "pub_e2e_000000000002",
        "itm_e2e_000000000003",
    ]
    assert first["ids"] == expected
    assert second["ids"] == expected


def test_new_id_keeps_random_uuid_path_without_anchor() -> None:
    """生产路径未设锚点时仍用随机 UUID 后缀，不受 e2e 序列影响。"""
    result = _successful_probe(anchor=None, fake_publishers=None)
    ids = result["ids"]
    assert isinstance(ids, list)
    assert ids[0] != ids[2]
    for value in ids:
        assert isinstance(value, str)
        prefix, suffix = value.split("_", maxsplit=1)
        assert prefix in {"itm", "pub"}
        assert len(suffix) == 12
        int(suffix, 16)


@pytest.mark.parametrize("fake_publishers_value", ["false", "0", "off"])
def test_utcnow_anchor_rejected_when_fake_publishers_off(fake_publishers_value: str) -> None:
    """锚点已设但假发布器不为真时，模块导入必须直接拒绝启动。"""
    completed = _run_probe(anchor=ANCHOR, fake_publishers=fake_publishers_value)

    assert completed.returncode != 0
    assert "RuntimeError" in completed.stderr
    assert "SW_USE_FAKE_PUBLISHERS" in completed.stderr


def test_utcnow_anchor_rejected_when_fake_publishers_unset() -> None:
    """锚点已设但未配置假发布器时，同样拒绝启动。"""
    completed = _run_probe(anchor=ANCHOR, fake_publishers=None)

    assert completed.returncode != 0
    assert "RuntimeError" in completed.stderr
    assert "SW_USE_FAKE_PUBLISHERS" in completed.stderr
