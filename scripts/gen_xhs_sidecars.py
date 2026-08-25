#!/usr/bin/env python
"""按 accounts.yaml 生成"一账号一容器"的小红书 sidecar compose 片段。

xiaohongshu-mcp 是**单进程单账号**（cookies 存单一 ``./data``），因此每个账号必须有
独立容器 + 独立 volume + 独立端口，禁止共享（docs/POLICY.md：不做 Cookie 池）。

镜像与启动参数以上游为准（2026-08-15 核对 ``main`` 分支 ``Dockerfile`` /
``docker/docker-compose.yml`` / ``main.go``）：

- 镜像 ``xpzouying/xiaohongshu-mcp``（Docker Hub；国内可换阿里云镜像源，见 ``XHS_IMAGE``）
- 容器内监听 **18060**（``main.go`` 的 ``-port`` 默认 ``:18060``），不是 8080
- ``-headless`` 默认 true，容器里保持默认；``-token`` 留空则读环境变量 ``AUTH_TOKEN``
- 浏览器已在镜像构建期预置（``XDG_CACHE_HOME=/app/cache``），运行时零下载
- 需要的环境变量：``COOKIES_PATH`` / ``HOME`` / ``XDG_CONFIG_HOME`` 都落在 ``/app/data``
- 上游 compose 带 ``tty: true``（浏览器子进程需要）

素材目录：core 生成的卡片在宿主机 ``XHS_MEDIA_HOST_DIR``（默认 ``./data/media``），
这里**只读**挂到容器 ``/app/images``，与 ``core.config`` 的
``xhs_media_container_dir`` 对应，发布时由客户端做路径翻译。

用法::

    uv run python scripts/gen_xhs_sidecars.py
    uv run python scripts/gen_xhs_sidecars.py --accounts accounts.yaml -o docker-compose.xhs.yml
    docker compose -f docker-compose.yml -f docker-compose.xhs.yml config   # 校验
    docker compose -f docker-compose.yml -f docker-compose.xhs.yml up -d
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent

# 上游 Docker Hub 镜像。tag 固定到已发布版本而不是 latest：sidecar 是浏览器自动化，
# 上游一改 DOM/接口就可能断，版本要能复现。国内拉取慢时换阿里云源（见 sidecars/xhs/README.md）。
DEFAULT_IMAGE = "xpzouying/xiaohongshu-mcp:v2.5.0"
# 容器内端口（main.go 的 -port 默认值），宿主机侧从 18060 起逐个 +1
CONTAINER_PORT = 18060
DEFAULT_BASE_PORT = 18060
# 宿主机侧**只绑回环**，与 docker-compose.yml 里 core / mpt / trendradar 三个服务同口径，
# 也与 core/sidecars.py 的 docker 驱动（-p 127.0.0.1:<port>:18060）同口径。
#
# 为什么这条对 xiaohongshu-mcp 尤其要紧：容器里装着这个账号**扫码登录后的 cookies**
# （/app/data/cookies.json），而它的 HTTP 接口就是"以这个身份发笔记/搜索"。鉴权只有一个
# AUTH_TOKEN，而它**默认留空**（见下面 environment 里的 ${...:-}）——留空即不鉴权。
# 绑到 0.0.0.0，同网段任何人都能拿这个号发东西，等于把账号本身交出去；生产还是合租机器
# （docs/RISKS.md §8.2：同机跑着无关的同租户容器），它们经 docker 网关就够得着。
#
# 这里**刻意不做成可配变量**：做成 ${...} 就等于给了一条"从 .env 静默改回 0.0.0.0"的路。
# 端口本身照旧可配——每账号在 accounts.yaml 的 sidecar.port 里写（那个值还要与台账
# sidecar_endpoint、preflight 的 http://localhost:<port> 对齐，不该再多一层 env 间接）。
# 不变量由 tests/test_port_bindings.py 钉住，别把它改回裸 "<port>:<port>"。
HOST_BIND_ADDRESS = "127.0.0.1"
CONTAINER_MEDIA_DIR = "/app/images"
DEFAULT_MEDIA_HOST_DIR = "./data/media"

HEADER = """# 本文件由 scripts/gen_xhs_sidecars.py 自动生成，请勿手工编辑（已在 .gitignore）。
# 生成来源：{source}
# 一账号一容器 · 一独立 volume · 一独立端口（xiaohongshu-mcp 为单进程单账号）
# 上游：https://github.com/xpzouying/xiaohongshu-mcp （Apache-2.0）
#
# 启动：docker compose -f docker-compose.yml -f docker-compose.xhs.yml up -d
# 首次登录流程见 sidecars/xhs/README.md
"""


def load_accounts(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise SystemExit(f"账号台账不存在: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    accounts = data.get("accounts") or []
    if not isinstance(accounts, list):
        raise SystemExit(f"{path} 的 accounts 必须是列表")
    return [a for a in accounts if a.get("platform") == "xhs"]


def service_name_for(account_id: str) -> str:
    safe = str(account_id).replace(".", "-").replace("_", "-").removeprefix("xhs-")
    return f"xhs-{safe}"


def port_for(account: dict[str, Any], index: int) -> int:
    sidecar = account.get("sidecar") or {}
    return int(sidecar.get("port") or DEFAULT_BASE_PORT + index)


def build_compose(
    accounts: list[dict[str, Any]], *, media_host_dir: str = DEFAULT_MEDIA_HOST_DIR
) -> dict[str, Any]:
    services: dict[str, Any] = {}
    volumes: dict[str, Any] = {}
    used_ports: dict[int, str] = {}
    used_volumes: dict[str, str] = {}

    for index, account in enumerate(accounts):
        account_id = account.get("id")
        if not account_id:
            raise SystemExit(f"第 {index + 1} 个 xhs 账号缺少 id")
        sidecar = account.get("sidecar") or {}
        port = port_for(account, index)
        if port in used_ports:
            raise SystemExit(f"端口冲突：{port} 同时被 {used_ports[port]} 和 {account_id} 使用")
        used_ports[port] = account_id

        service_name = service_name_for(account_id)
        volume_name = str(sidecar.get("volume") or f"xhs_data_{str(account_id).replace('-', '_')}")
        if volume_name in used_volumes:
            # 共享 volume = 共享 cookies = Cookie 池，红线（docs/POLICY.md）
            raise SystemExit(
                f"volume 冲突：{volume_name} 同时被 {used_volumes[volume_name]} 和 "
                f"{account_id} 使用；一账号必须一个独立 volume，禁止共享登录态"
            )
        used_volumes[volume_name] = str(account_id)
        volumes[volume_name] = {}

        # 每账号独立的 AUTH_TOKEN 环境变量名，凭据本身只在 .env 里
        token_env = str(sidecar.get("token_env") or f"XHS_TOKEN_{_env_suffix(account_id)}")

        services[service_name] = {
            "image": sidecar.get("image") or "${XHS_IMAGE:-" + DEFAULT_IMAGE + "}",
            "container_name": service_name,
            "restart": "unless-stopped",
            # 上游 compose 带 tty，浏览器子进程需要
            "tty": True,
            # 只绑回环，理由见 HOST_BIND_ADDRESS 上方的注释
            "ports": [f"{HOST_BIND_ADDRESS}:{port}:{CONTAINER_PORT}"],
            "volumes": [
                # 扫码登录后的 cookies 落这里，绝不跨账号共享
                f"{volume_name}:/app/data",
                # core 生成的卡片图，只读挂进来供 POST /api/v1/publish 使用
                f"{media_host_dir}:{CONTAINER_MEDIA_DIR}:ro",
            ],
            "environment": {
                "COOKIES_PATH": "/app/data/cookies.json",
                "HOME": "/app/data/home",
                "XDG_CONFIG_HOME": "/app/data/config",
                # 留空 = 不鉴权（仅限只监听本机时）；生产务必在 .env 里给每个 sidecar 配 token
                "AUTH_TOKEN": "${" + token_env + ":-}",
                "TZ": "Asia/Shanghai",
            },
            "labels": {
                "social_workflow.platform": "xhs",
                "social_workflow.account": account_id,
                "social_workflow.name": str(account.get("name") or account_id),
            },
            "healthcheck": {
                # /health 不走鉴权中间件，可以直接探
                "test": [
                    "CMD-SHELL",
                    f"wget -qO- http://localhost:{CONTAINER_PORT}/health || exit 1",
                ],
                "interval": "30s",
                "timeout": "5s",
                "retries": 3,
                # 镜像内置浏览器，冷启动仍要几十秒
                "start_period": "60s",
            },
            "networks": ["social_workflow"],
        }

    return {
        "services": services,
        "volumes": volumes,
        # 与 docker-compose.yml 里的 core 同网；那边已定义，这里只引用
        "networks": {"social_workflow": {"name": "social_workflow", "external": False}},
    }


def _env_suffix(account_id: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in str(account_id)).upper()


def render(compose: dict[str, Any], source: Path) -> str:
    body = yaml.safe_dump(compose, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return HEADER.format(source=source.name) + body


def env_lines(accounts: list[dict[str, Any]]) -> list[str]:
    """顺带给出 .env 里该怎么填（sidecar 地址与逐账号 token 变量）。"""
    endpoints = []
    tokens = []
    for index, account in enumerate(accounts):
        port = port_for(account, index)
        account_id = account["id"]
        endpoints.append(f"{account_id}=http://localhost:{port}")
        sidecar = account.get("sidecar") or {}
        token_env = str(sidecar.get("token_env") or f"XHS_TOKEN_{_env_suffix(account_id)}")
        tokens.append(f"{token_env}=<给该 sidecar 生成一个随机串>")
    return [
        "XHS_MCP_ENDPOINTS=" + ",".join(endpoints),
        "# 每个 sidecar 一个 AUTH_TOKEN（compose 从这些变量取值）：",
        *tokens,
        "# 并把同样的值按账号填进 XHS_MCP_TOKENS，core 才带得上 Authorization 头：",
        "XHS_MCP_TOKENS=" + ",".join(f"{a['id']}=<同上>" for a in accounts),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成小红书 sidecar compose 片段")
    parser.add_argument("--accounts", type=Path, default=ROOT / "accounts.yaml")
    parser.add_argument("-o", "--output", type=Path, default=ROOT / "docker-compose.xhs.yml")
    parser.add_argument(
        "--media-host-dir",
        default=DEFAULT_MEDIA_HOST_DIR,
        help=f"宿主机素材目录，只读挂到容器 {CONTAINER_MEDIA_DIR}（默认 {DEFAULT_MEDIA_HOST_DIR}）",
    )
    parser.add_argument("--stdout", action="store_true", help="只打印不写文件")
    args = parser.parse_args(argv)

    accounts = load_accounts(args.accounts)
    if not accounts:
        print(f"{args.accounts} 中没有 platform=xhs 的账号，未生成任何内容。", file=sys.stderr)
        return 1

    compose = build_compose(accounts, media_host_dir=args.media_host_dir)
    text = render(compose, args.accounts)
    if args.stdout:
        print(text)
    else:
        args.output.write_text(text, encoding="utf-8")
        print(f"已生成 {args.output}（{len(accounts)} 个小红书账号）")
    for line in env_lines(accounts):
        print(line, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
