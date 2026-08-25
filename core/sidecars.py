"""小红书 sidecar（``xiaohongshu-mcp``）的生命周期：起 / 停 / 重建 / 看状态。

红线（docs/POLICY.md）
--------------------
``xiaohongshu-mcp`` 是**单进程单账号**——cookies 固定落在 ``COOKIES_PATH``。所以
**一账号 = 一容器 + 一独立 volume + 一独立宿主机端口**，永远不共享登录态。这个模块
里没有任何"复用容器""共享 volume"的分支，端口冲突与 volume 冲突都会直接报错。

两个驱动（``SW_SIDECAR_DRIVER``）
--------------------------------
- ``none``（**默认**）：只记账，不起容器。账号照样能建、能出稿，账号页会如实显示
  "sidecar 未接入"，而不是假装它在跑。本机开发、CI、Playwright 全用它。
- ``docker``：调宿主机的 ``docker`` CLI（不引 docker SDK：部署方装的是 docker 本体，
  不一定装 Python 客户端库，而且 CLI 的行为与运维手敲的一模一样，排障时可复现）。

上游事实（2026-08-15 核对 main 分支，另见 ``sidecars/xhs/README.md``）
--------------------------------------------------------------------
- 容器内监听 **18060**；``-headless`` 默认 true
- 数据目录 ``/app/data``：``COOKIES_PATH`` / ``HOME`` / ``XDG_CONFIG_HOME`` 都指向它。
  **volume 必须挂在 ``/app/data``**，挂错地方容器一重启登录态就没了，人得反复扫码
- ``/health`` 不走鉴权中间件，可以直接探
- ``AUTH_TOKEN`` 留空 = 不鉴权。这里的容器只绑 ``127.0.0.1``，所以留空可接受；
  配了 token 的账号会用 ``-e AUTH_TOKEN``（**不带值**）从 core 进程环境透传过去，
  token 不会出现在 ``docker inspect`` 之外的地方，也绝不入库

镜像：上游只发 **amd64**。aarch64 服务器要先从上游源码本地 ``docker build``，
再把镜像名填进 ``SW_XHS_MCP_IMAGE``。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import socket
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from core.config import get_settings
from core.models import Account, utcnow

logger = logging.getLogger("social_workflow.sidecars")

#: 容器内监听端口（上游 ``main.go`` 的 ``-port`` 默认值）
CONTAINER_PORT = 18060
#: 容器内数据目录。cookies 落在这里，volume 必须挂到这个路径
CONTAINER_DATA_DIR = "/app/data"
#: 容器内素材目录（发布时读卡片图），与 ``XHS_MEDIA_CONTAINER_DIR`` 对应
CONTAINER_MEDIA_DIR = "/app/images"

CONTAINER_PREFIX = "sw-xhs-"
VOLUME_PREFIX = "swxhs_"

#: 一次性拉走的状态，前端据此分支
STATE_RUNNING = "running"
STATE_STOPPED = "stopped"
STATE_ABSENT = "absent"
STATE_NONE_DRIVER = "none-driver"
STATE_ERROR = "error"

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]")
_PORT_RE = re.compile(r":(\d{2,5})(?:/|$)")


class SidecarError(RuntimeError):
    """sidecar 操作失败，携带面向人的原因（会原样显示在账号页上）。"""


class SidecarNotSupported(SidecarError):
    """这个平台没有 sidecar 这回事（抖音在宿主机、公众号走官方 API）。"""


def container_name(account_id: str) -> str:
    return f"{CONTAINER_PREFIX}{_SAFE_NAME_RE.sub('-', account_id)}"


def volume_name(account_id: str) -> str:
    return f"{VOLUME_PREFIX}{_SAFE_NAME_RE.sub('_', account_id)}"


def endpoint_of(account: Account) -> str:
    """该账号的 sidecar 地址。``XHS_MCP_ENDPOINTS`` 优先（与发布器同一口径）。"""
    override = get_settings().xhs_endpoint_map().get(account.id, "")
    return override or (account.sidecar_endpoint or "")


def port_of(account: Account) -> int | None:
    match = _PORT_RE.search(endpoint_of(account))
    return int(match.group(1)) if match else None


def auth_token_env_of(account: Account) -> str:
    """该账号 ``AUTH_TOKEN`` 取自哪个环境变量名（库里只存**变量名**）。"""
    extra = account.extra or {}
    xhs = extra.get("xhs") if isinstance(extra.get("xhs"), dict) else {}
    return str(xhs.get("auth_token_env") or "")


def resolve_auth_token(account: Account) -> str:
    """解析该账号的 sidecar token。只从环境变量取，绝不读库里的值。"""
    settings = get_settings()
    env_name = auth_token_env_of(account)
    if env_name and os.environ.get(env_name):
        return os.environ[env_name]
    return settings.xhs_token_map().get(account.id, "") or settings.xhs_auth_token


# --------------------------------------------------------------------- 状态


@dataclass
class SidecarState:
    """一次状态查询的结果。前端只认这个结构，不需要懂 docker。"""

    account_id: str
    driver: str
    #: running / stopped / absent / none-driver / error
    state: str
    detail: str = ""
    container: str = ""
    volume: str = ""
    image: str = ""
    port: int | None = None
    endpoint: str = ""
    #: ``GET {endpoint}/health`` 的原样透传。探不通时为 None
    health: dict[str, Any] | None = None
    healthy: bool = False
    health_detail: str = ""
    checked_at: datetime = field(default_factory=utcnow)

    def as_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "driver": self.driver,
            "state": self.state,
            "detail": self.detail,
            "container": self.container,
            "volume": self.volume,
            "image": self.image,
            "port": self.port,
            "endpoint": self.endpoint,
            "health": self.health,
            "healthy": self.healthy,
            "health_detail": self.health_detail,
            "checked_at": self.checked_at,
        }


# --------------------------------------------------------------------- 驱动


class NoneDriver:
    """不接管容器。账号建得出来，sidecar 由人自己起（compose / 手工 docker run）。"""

    name = "none"

    def probe(self, account: Account) -> tuple[str, str]:
        return (
            STATE_NONE_DRIVER,
            "SW_SIDECAR_DRIVER=none：core 不接管容器。"
            "sidecar 请用 docker-compose.xhs.yml 自行起，或把 SW_SIDECAR_DRIVER 改成 docker。",
        )

    def _refuse(self, verb: str) -> str:
        raise SidecarError(
            f"当前实例的 SW_SIDECAR_DRIVER=none，core 不{verb}容器。"
            "要让工作台接管，请在服务器上把它改成 docker（并配好 SW_XHS_MCP_IMAGE）后重启 core；"
            "也可以继续用 docker-compose.xhs.yml 手工管理。"
        )

    def start(self, account: Account) -> str:
        return self._refuse("负责起")

    def stop(self, account: Account) -> str:
        return self._refuse("负责停")

    def recreate(self, account: Account) -> str:
        return self._refuse("负责重建")


class DockerDriver:
    """调宿主机 ``docker`` CLI。一账号一容器一 volume 一端口。"""

    name = "docker"

    def __init__(self, *, binary: str | None = None, timeout: float | None = None) -> None:
        settings = get_settings()
        self.binary = binary or settings.sw_docker_bin
        self.timeout = timeout if timeout is not None else settings.sw_docker_timeout_seconds

    # -- 底层 ------------------------------------------------------------

    def _run(
        self, args: list[str], *, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        if shutil.which(self.binary) is None:
            raise SidecarError(
                f"这台机器上找不到 {self.binary!r}。装好 docker 或把 SW_DOCKER_BIN 指到正确路径。"
            )
        try:
            return subprocess.run(
                [self.binary, *args],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env={**os.environ, **(env or {})},
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SidecarError(
                f"docker {' '.join(args[:2])} 超过 {self.timeout:.0f} 秒没返回"
            ) from exc
        except OSError as exc:
            raise SidecarError(f"调用 docker 失败：{exc}") from exc

    def _checked(self, args: list[str], *, what: str, env: dict[str, str] | None = None) -> str:
        proc = self._run(args, env=env)
        if proc.returncode != 0:
            raise SidecarError(f"{what}失败：{(proc.stderr or proc.stdout or '').strip()[:400]}")
        return proc.stdout.strip()

    # -- 生命周期 --------------------------------------------------------

    def probe(self, account: Account) -> tuple[str, str]:
        proc = self._run(["inspect", "-f", "{{.State.Status}}", container_name(account.id)])
        if proc.returncode != 0:
            text = (proc.stderr or "").strip()
            if "No such object" in text or "no such object" in text.lower():
                return STATE_ABSENT, "容器还没建。点「启动 sidecar」会按一账号一容器的规矩创建。"
            return STATE_ERROR, f"docker inspect 失败：{text[:400]}"
        status = proc.stdout.strip()
        if status == "running":
            return STATE_RUNNING, "容器在跑。"
        return STATE_STOPPED, f"容器存在但没跑（docker 状态 {status or '未知'}）。"

    def start(self, account: Account) -> str:
        state, detail = self.probe(account)
        if state == STATE_RUNNING:
            return "容器已经在跑，什么都没做。"
        if state == STATE_STOPPED:
            self._checked(["start", container_name(account.id)], what="启动容器")
            return "已启动已有容器（登录态在 volume 里，不用重新扫码）。"
        if state == STATE_ERROR:
            raise SidecarError(detail)
        self._create(account)
        return "已创建并启动容器。首次启动要拉起内置浏览器，几十秒后再看健康探测。"

    def stop(self, account: Account) -> str:
        state, _detail = self.probe(account)
        if state == STATE_ABSENT:
            return "容器本来就不存在。"
        if state == STATE_STOPPED:
            return "容器本来就没跑。"
        self._checked(["stop", container_name(account.id)], what="停止容器")
        return "已停止容器。volume 还在，登录态不会丢。"

    def recreate(self, account: Account) -> str:
        """删容器重建。**volume 不动**，所以扫过的码不用重扫。"""
        name = container_name(account.id)
        proc = self._run(["rm", "-f", name])
        if proc.returncode != 0 and "No such container" not in (proc.stderr or ""):
            raise SidecarError(f"删除旧容器失败：{(proc.stderr or '').strip()[:400]}")
        self._create(account)
        return "已按当前镜像重建容器（volume 未动，登录态保留）。"

    # -- 创建 ------------------------------------------------------------

    def _create(self, account: Account) -> None:
        settings = get_settings()
        port = port_of(account)
        if not port:
            raise SidecarError(
                f"账号 {account.id} 没有 sidecar 端口"
                f"（sidecar_endpoint={account.sidecar_endpoint!r}）。"
                "先在账号页上补一个端口，或重新走一次添加账号向导。"
            )
        image = (settings.sw_xhs_mcp_image or "").strip()
        if not image:
            raise SidecarError("SW_XHS_MCP_IMAGE 没配。aarch64 服务器要先本地构建镜像再填这里。")

        args: list[str] = [
            "run",
            "-d",
            "--name",
            container_name(account.id),
            "--restart",
            "unless-stopped",
            # 上游 compose 带 tty：内置浏览器子进程需要
            "-t",
            # 只绑本机回环：sidecar 不对外暴露，AUTH_TOKEN 留空也不至于裸奔
            "-p",
            f"127.0.0.1:{port}:{CONTAINER_PORT}",
            # 一账号一 volume。挂在 /app/data —— cookies 就在这里，挂错地方等于每次
            # 重启都要重新扫码
            "-v",
            f"{volume_name(account.id)}:{CONTAINER_DATA_DIR}",
            "-e",
            f"COOKIES_PATH={CONTAINER_DATA_DIR}/cookies.json",
            "-e",
            f"HOME={CONTAINER_DATA_DIR}/home",
            "-e",
            f"XDG_CONFIG_HOME={CONTAINER_DATA_DIR}/config",
            "-e",
            f"TZ={settings.sw_timezone or 'Asia/Shanghai'}",
            "--label",
            "social_workflow.platform=xhs",
            "--label",
            f"social_workflow.account={account.id}",
        ]

        media_dir = _media_host_dir(settings.xhs_media_host_dir)
        if media_dir is not None:
            # core 生成的卡片图，只读挂进去供 POST /api/v1/publish 使用
            args += ["-v", f"{media_dir}:{CONTAINER_MEDIA_DIR}:ro"]

        env: dict[str, str] = {}
        token = resolve_auth_token(account)
        if token:
            # 不带值的 -e 让 docker 从 **本进程环境**取，token 不进命令行参数
            env["AUTH_TOKEN"] = token
            args += ["-e", "AUTH_TOKEN"]

        args.append(image)
        self._checked(args, what=f"创建容器（镜像 {image}）", env=env)


def _media_host_dir(raw: str) -> str | None:
    """素材目录 → 绝对路径。留空表示 core 与 sidecar 共享文件系统，不挂载。"""
    text = (raw or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = (Path(__file__).resolve().parent.parent / path).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def get_driver(name: str | None = None) -> NoneDriver | DockerDriver:
    driver = (name or get_settings().sw_sidecar_driver or "none").strip().lower()
    if driver == "docker":
        return DockerDriver()
    return NoneDriver()


# --------------------------------------------------------------------- 对外


def require_sidecar_platform(account: Account) -> None:
    if account.platform != "xhs":
        raise SidecarNotSupported(
            f"{account.platform} 没有 sidecar 这回事：抖音的上传器常驻宿主机，公众号走官方 API。"
        )


def probe_health(endpoint: str, *, timeout: float = 4.0) -> tuple[dict[str, Any] | None, str]:
    """探 ``GET {endpoint}/health``（上游这个路由不走鉴权）。原样透传，不做解释。

    ``trust_env=False`` 是**必须的**：sidecar 永远在回环地址上，而部署机上常配着
    ``HTTP_PROXY``。走代理去连 ``127.0.0.1`` 会拿回代理自己的 502，把"容器没起来"
    和"代理拦了"混成同一句话——那就是在骗人。
    """
    if not endpoint:
        return None, "这个账号还没有 sidecar 地址。"
    import httpx

    url = f"{endpoint.rstrip('/')}/health"
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            resp = client.get(url)
    except httpx.HTTPError as exc:
        return None, f"连不上 {url}：{type(exc).__name__}"
    if resp.status_code >= 400:
        return None, f"{url} 返回 HTTP {resp.status_code}"
    try:
        payload = resp.json()
    except ValueError:
        return {"raw": resp.text[:200]}, ""
    return (payload if isinstance(payload, dict) else {"raw": payload}), ""


def describe(account: Account, *, driver: Any | None = None, health: bool = True) -> SidecarState:
    """当前状态 + 健康探测。任何一步失败都如实写进 ``detail``，不吞。"""
    require_sidecar_platform(account)
    drv = driver or get_driver()
    settings = get_settings()
    endpoint = endpoint_of(account)

    try:
        state, detail = drv.probe(account)
    except SidecarError as exc:
        state, detail = STATE_ERROR, str(exc)

    result = SidecarState(
        account_id=account.id,
        driver=drv.name,
        state=state,
        detail=detail,
        container=container_name(account.id),
        volume=volume_name(account.id),
        image=settings.sw_xhs_mcp_image,
        port=port_of(account),
        endpoint=endpoint,
    )
    if health:
        payload, why = probe_health(endpoint)
        result.health = payload
        result.healthy = payload is not None
        result.health_detail = why or ("sidecar 应答正常。" if payload is not None else "")
    return result


def act(account: Account, action: str, *, driver: Any | None = None) -> tuple[SidecarState, str]:
    """``start`` / ``stop`` / ``recreate``。返回（动作之后的状态, 一句给人看的话）。"""
    require_sidecar_platform(account)
    drv = driver or get_driver()
    handler = {"start": drv.start, "stop": drv.stop, "recreate": drv.recreate}.get(action)
    if handler is None:
        raise SidecarError(f"未知动作 {action!r}，只支持 start / stop / recreate")
    message = handler(account)
    logger.info("sidecar %s(%s) %s：%s", account.id, drv.name, action, message)
    return describe(account, driver=drv), message


# --------------------------------------------------------------------- 端口


def port_is_free(port: int, *, host: str = "127.0.0.1") -> bool:
    """本机这个端口现在有没有人在听。绑一下试试，比解析 ``netstat`` 靠谱。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def allocate_port(taken: set[int], *, base: int | None = None, span: int = 200) -> int:
    """从 ``base``（默认 18060）起找第一个既没被台账占用、也没被本机占用的端口。"""
    start = base if base is not None else get_settings().sw_sidecar_base_port
    for port in range(start, start + span):
        if port in taken:
            continue
        if not port_is_free(port):
            continue
        return port
    raise SidecarError(f"{start}–{start + span} 之间找不到空闲端口，先清理一下再建账号。")


__all__ = [
    "CONTAINER_DATA_DIR",
    "CONTAINER_MEDIA_DIR",
    "CONTAINER_PORT",
    "STATE_ABSENT",
    "STATE_ERROR",
    "STATE_NONE_DRIVER",
    "STATE_RUNNING",
    "STATE_STOPPED",
    "DockerDriver",
    "NoneDriver",
    "SidecarError",
    "SidecarNotSupported",
    "SidecarState",
    "act",
    "allocate_port",
    "auth_token_env_of",
    "container_name",
    "describe",
    "endpoint_of",
    "get_driver",
    "port_is_free",
    "port_of",
    "probe_health",
    "require_sidecar_platform",
    "resolve_auth_token",
    "volume_name",
]
