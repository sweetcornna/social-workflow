"""compose 服务起来之后**真的在提供服务**——不只是"容器 running"。

这条不变量是被一次真实失败逼出来的：`scripts/ops/sidecar.sh --up xhs-downloader` 在生产上
容器起来了，端口回环闸门也过了，但 5556 上永远没人监听。日志里是一片
``Q Quit / U Update / S Settings / R Record``——镜像的默认 CMD 起的是 Textual **交互界面**，
不是 API server。上游 README 给的 API 模式命令是 ``python main.py api``。

为什么现有的护栏一条都没拦住它：

* CI 的 compose job 只跑 ``docker compose config``——没有 ``command:`` 语法完全合法；
* ``tests/test_port_bindings.py`` 钉的是"绑哪个地址"，绑对了地址不代表有人在听；
* 门禁只会说一句「不可达（未启动则正常）」——而它确实"启动"了，所以这句话读起来像常态。

于是 `sidecar.sh` 对外宣称支持 ``--up xhs-downloader``，而那个动作**结构上不可能成功**。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _services() -> dict[str, Any]:
    data = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    return data["services"]


def test_xhs_downloader_starts_the_api_server_not_the_interactive_tui() -> None:
    """没有这条 ``command``，容器起的是 TUI，5556 上永远没人监听。"""
    service = _services()["xhs-downloader"]
    command = service.get("command")
    assert command, (
        "xhs-downloader 没有 command：镜像默认 CMD 是 Textual 交互界面，"
        "容器会 running 但 5556 上没人监听，sidecar.sh --up 结构上不可能成功"
    )
    parts = command.split() if isinstance(command, str) else list(command)
    assert parts[-1] == "api", f"要跑 API 模式（上游 README：python main.py api），实际 {parts}"


def test_every_ops_managed_sidecar_can_actually_serve() -> None:
    """`sidecar.sh` 管辖的每个服务，要么镜像默认就起服务，要么必须显式给 command。

    名单来自 `scripts/ops/sidecar.sh` 的白名单，**从脚本里读**而不是在这里再抄一份——
    抄一份的话，哪天名单加了第三个服务，这条用例会安静地不覆盖它。
    """
    whitelist_line = next(
        line
        for line in (ROOT / "scripts/ops/sidecar.sh").read_text(encoding="utf-8").splitlines()
        if line.startswith("SW_SIDECAR_WHITELIST=")
    )
    names = whitelist_line.split("=", 1)[1].strip().strip('"').split()
    assert names, "白名单读空了——这条用例会变成永真"

    services = _services()
    # trendradar 的 8080 是上游 entrypoint.sh 起的 `python -m http.server`，镜像默认就服务，
    # 不需要 command。它同时是这条用例的"反例锚"：如果哪天所有服务都被要求写 command，
    # 说明这条规则被写成了机械要求而不是"起来之后真的在服务"。
    serves_by_default = {"trendradar"}
    for name in names:
        assert name in services, f"白名单里的 {name} 在 docker-compose.yml 里没有对应服务"
        if name in serves_by_default:
            continue
        assert services[name].get("command"), (
            f"{name} 既不在'镜像默认就服务'名单里，也没有显式 command——"
            "那 sidecar.sh --up 起来的到底是什么？"
        )
