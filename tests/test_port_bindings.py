"""端口发布口径的不变量：宿主机侧一律**只绑回环**。

2026-08-23 之前这条不变量被破坏过**两处**——`docker-compose.yml` 的 `xhs-downloader`
写成 `5556:5556`，`scripts/gen_xhs_sidecars.py` 生成的每账号 sidecar 写成
`18060:18060`——而当时**没有任何测试钉着它**。CI 的 compose job 只跑
`docker compose config`：`5556:5556` 语法完全合法，绑哪个地址它根本不看。
事实链与处置见 `docs/RISKS.md` 第 15 条。

这条不变量还会被无意破坏，所以覆盖面要**按"发布口径的产生方式"分**，一个都不能少：

1. 仓库里静态的 compose 文件（`docker-compose.yml` 及将来新增的同名族文件）；
2. `scripts/gen_xhs_sidecars.py` 的**生成逻辑**——生成物在 `.gitignore` 里，CI 是现场
   生成的，只查文件查不到它；
3. 本机上已经躺着的生成物（如果有）——防止有人留着旧版生成物直接 `up`。

`core/sidecars.py` 的 docker 驱动（`-p 127.0.0.1:<port>:18060`）由
`tests/test_account_lifecycle.py::test_docker_driver_run_args_follow_the_one_account_one_container_rule`
钉着，是同一条不变量的第四个面，不在本文件重复。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.gen_xhs_sidecars import build_compose, render

ROOT = Path(__file__).resolve().parent.parent

# 只认这两个**字面量**前缀。刻意不接受 `${SOME_BIND:-127.0.0.1}` 这类可配形式：
# 那等于留一条"从 .env 静默改回 0.0.0.0"的路，不变量就白钉了。
# 端口本身照旧可配（`${MPT_HOST_PORT:-8080}` / `accounts.yaml` 的 `sidecar.port`），
# 可配的是端口号，不是绑定地址。
LOOPBACK_PREFIXES = ("127.0.0.1:", "[::1]:")
LOOPBACK_HOST_IPS = ("127.0.0.1", "::1")

# 环境本地的覆盖文件（`.gitignore:56`），每台机器内容不同，不进这条门禁
IGNORED_COMPOSE_FILES = {"docker-compose.override.yml"}

_WHY = (
    "宿主机端口必须显式绑到回环。裸 `<host>:<container>` 会绑 0.0.0.0，"
    "生产是合租机器（docs/RISKS.md §8.2，同机跑着无关的同租户容器），"
    "同网段与同机其他容器经 docker 网关就够得着。写法照抄同文件里的 core / mpt / "
    "trendradar：`127.0.0.1:${XXX_HOST_PORT:-<默认>}:<容器端口>`。"
)


def _published_ports(compose: dict[str, Any]) -> list[tuple[str, Any]]:
    """摊平成 (服务名, ports 条目) 列表。没有 ports 的服务不产生条目。"""
    found: list[tuple[str, Any]] = []
    for service_name, service in (compose.get("services") or {}).items():
        for entry in (service or {}).get("ports") or []:
            found.append((service_name, entry))
    return found


def _offenders(compose: dict[str, Any], where: str) -> list[str]:
    bad: list[str] = []
    for service_name, entry in _published_ports(compose):
        if isinstance(entry, dict):  # compose 长语法
            if entry.get("host_ip") not in LOOPBACK_HOST_IPS:
                bad.append(f"{where} :: {service_name} :: {entry!r}（host_ip 不是回环）")
            continue
        text = str(entry)
        if not text.startswith(LOOPBACK_PREFIXES):
            bad.append(f"{where} :: {service_name} :: {text!r}")
    return bad


def _compose_files() -> list[Path]:
    return sorted(
        path for path in ROOT.glob("docker-compose*.yml") if path.name not in IGNORED_COMPOSE_FILES
    )


def _sample_accounts() -> list[dict[str, Any]]:
    """两个账号：一个在台账里写死端口，一个走 DEFAULT_BASE_PORT + index 自动分配。

    两条取值路径都要覆盖——历史上出事的正是自动分配那条。
    """
    return [
        {"id": "xhs-alpha", "platform": "xhs", "sidecar": {"port": 18099}},
        {"id": "xhs-beta", "platform": "xhs"},
    ]


def test_repo_compose_files_publish_only_to_loopback():
    """仓库里静态的 compose 文件：所有 ports 都必须显式绑回环。"""
    files = _compose_files()
    assert any(path.name == "docker-compose.yml" for path in files), (
        "连 docker-compose.yml 都没找到，这条门禁在空跑——先确认它没被改名或挪走"
    )

    bad: list[str] = []
    checked = 0
    for path in files:
        compose = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        checked += len(_published_ports(compose))
        bad.extend(_offenders(compose, path.name))

    assert checked > 0, "一个 ports 条目都没扫到，这条门禁在空跑"
    assert bad == [], "以下端口没有绑回环：\n" + "\n".join(bad) + "\n\n" + _WHY


def test_generated_xhs_sidecars_publish_only_to_loopback():
    """生成器**逻辑**：生成物在 .gitignore 里，只查文件查不到这条。"""
    compose = build_compose(_sample_accounts())
    published = _published_ports(compose)
    assert len(published) == 2, f"两个账号应生成两个发布口，实际 {published!r}"
    assert _offenders(compose, "gen_xhs_sidecars.build_compose") == [], (
        "生成器把 sidecar 端口绑到了回环之外。"
        "sidecar 容器里是这个账号扫码登录后的 cookies，接口能以该账号身份发笔记，"
        "而 AUTH_TOKEN 默认留空（= 不鉴权）。\n" + _WHY
    )
    # 明确钉住完整字面量，避免"前缀对了但端口映射被写反"这类改法悄悄溜过去
    assert {entry for _name, entry in published} == {
        "127.0.0.1:18099:18060",
        "127.0.0.1:18061:18060",
    }


def test_generated_yaml_round_trips_the_binding_as_a_plain_string():
    """`127.0.0.1:18060:18060` 是 YAML 纯量，别被 safe_dump/safe_load 变成别的类型。

    裸 `18060:18060` 曾经也是纯量，改口径后多了两个 `.` 和一段 `:`，这条就是防
    "写进去是字符串、读出来变成 dict/int" 的。
    """
    text = render(build_compose(_sample_accounts()), Path("accounts.yaml"))
    reparsed = yaml.safe_load(text)
    for _name, entry in _published_ports(reparsed):
        assert isinstance(entry, str), f"发布口被解析成了 {type(entry).__name__}：{entry!r}"
    assert _offenders(reparsed, "render() 输出") == []


def test_local_generated_artifact_is_not_a_stale_pre_loopback_copy():
    """本机若躺着生成物，它也必须是收敛后的版本——`docker compose up` 吃的是它。"""
    path = ROOT / "docker-compose.xhs.yml"
    if not path.exists():
        pytest.skip("本机没有生成物（它在 .gitignore 里，CI 上是现场生成的）")
    compose = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    assert _offenders(compose, path.name) == [], (
        f"{path.name} 是收敛前的旧版生成物，重新生成一次："
        "`.venv/bin/python scripts/gen_xhs_sidecars.py`\n" + _WHY
    )
