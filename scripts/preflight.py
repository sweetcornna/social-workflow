#!/usr/bin/env python
"""门禁自检：每阶段开工与真实发布前先跑这个，缺失的前置条件显式报出。

用法::

    uv run python scripts/preflight.py            # 全部检查
    uv run python scripts/preflight.py --offline  # 跳过所有网络检查
    uv run python scripts/preflight.py --strict   # 把 WARN 也当作失败

退出码：0 = 全通过；1 = 有 FAIL（--strict 下 WARN 也算）。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# 允许直接 `python scripts/preflight.py` 运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import yaml
from rich.console import Console
from rich.table import Table

from core.config import Settings, config_env_file

Status = Literal["OK", "WARN", "FAIL", "SKIP"]

ROOT = Path(__file__).resolve().parent.parent
console = Console()


@dataclass
class Check:
    name: str
    status: Status
    detail: str


def check_env_file() -> Check:
    source = Path(config_env_file())
    explicit_source = "SW_CONFIG_ENV_FILE" in os.environ
    if source.is_file():
        if explicit_source:
            return Check(".env 文件", "OK", "E2E 专用配置已存在")
        return Check(".env 文件", "OK", "已存在")
    if explicit_source:
        return Check(".env 文件", "WARN", "E2E 专用配置不存在，将只从进程环境变量读取")
    return Check(".env 文件", "WARN", "不存在，将只从进程环境变量读取；可 cp .env.example .env")


def check_anthropic(settings: Settings) -> Check:
    key = settings.anthropic_api_key
    if not key:
        if settings.sw_llm_backend == "dsh":
            # 后端已切到 dsh：Anthropic key 只是"回退到 Claude"的备用路，缺了不阻塞
            return Check(
                "Anthropic API Key",
                "WARN",
                "未配置。当前后端是 dsh，不影响生成；但 SW_LLM_BACKEND=anthropic 的回退路不可用",
            )
        return Check("Anthropic API Key", "FAIL", "ANTHROPIC_API_KEY 未配置，内容生成不可用")
    if not key.startswith("sk-ant-"):
        return Check(
            "Anthropic API Key", "FAIL", f"格式可疑：应以 sk-ant- 开头，实际前缀 {key[:7]!r}"
        )
    if len(key) < 40:
        return Check("Anthropic API Key", "WARN", f"长度仅 {len(key)}，看起来被截断")
    return Check("Anthropic API Key", "OK", f"sk-ant-…{key[-4:]}（未做联网校验）")


def check_wechat(settings: Settings, offline: bool) -> list[Check]:
    checks: list[Check] = []
    if not settings.wechat_app_id or not settings.wechat_app_secret:
        checks.append(
            Check("公众号 AppID/Secret", "WARN", "未配置，公众号发布不可用（不阻塞其它平台）")
        )
        return checks
    checks.append(Check("公众号 AppID/Secret", "OK", f"AppID {settings.wechat_app_id[:8]}…"))
    checks.append(
        Check(
            "公众号认证状态",
            "OK" if settings.wechat_certified else "WARN",
            "已认证，可 freepublish/submit"
            if settings.wechat_certified
            else "未认证：只能落草稿箱，需人工在后台点发布（2025-07 权限回收）",
        )
    )
    checks.append(_check_wechat_gate(settings))
    checks.append(
        Check(
            "公众号发布后端",
            "OK" if settings.wechat_backend in ("api", "wenyan") else "FAIL",
            f"WECHAT_BACKEND={settings.wechat_backend}"
            + (
                "（@wenyan-md/cli 子进程，需要 Node）"
                if settings.wechat_backend == "wenyan"
                else ""
            ),
        )
    )
    if offline:
        checks.append(Check("公众号 IP 白名单(40164)", "SKIP", "--offline"))
        return checks
    checks.append(_probe_wechat_token(settings))
    return checks


def _check_wechat_gate(settings: Settings) -> Check:
    """双确认闸门的前两道（服务端开关 + 账号认证）在门禁里显式报出。

    第三道（本次内容的 ``confirm_publish``）由审核 UI 在人工批准时写入，
    不是环境配置，无法在这里检查。
    """
    if not settings.wechat_auto_publish:
        return Check(
            "公众号自动发布闸门",
            "OK",
            "WECHAT_AUTO_PUBLISH=false：只落草稿箱（最安全的默认值）",
        )
    if not settings.wechat_certified:
        return Check(
            "公众号自动发布闸门",
            "FAIL",
            "WECHAT_AUTO_PUBLISH=true 但 WECHAT_CERTIFIED=false："
            "未认证号没有 freepublish 权限，会在发布时报 48001",
        )
    return Check(
        "公众号自动发布闸门",
        "WARN",
        "服务端开关 + 认证状态都已打开；仍需审核 UI 对**每一条**内容写入 "
        "confirm_publish=True 才会真正 freepublish",
    )


def _probe_wechat_token(settings: Settings) -> Check:
    """用 publishers/wechat_mp 的客户端探测 stable_token 与 40164。

    走真实客户端而不是裸 httpx，保证门禁与发布路径的错误码映射完全一致。
    """
    from publishers.base import PermanentError, PublishError
    from publishers.wechat_mp.client import ERRCODE_IP_NOT_IN_WHITELIST, TokenCache, WechatMpClient

    client = WechatMpClient(
        settings.wechat_app_id,
        settings.wechat_app_secret,
        base_url=settings.wechat_api_base,
        timeout=8.0,
        # 用独立缓存，避免门禁把 token 写进长驻进程的共享缓存
        token_cache=TokenCache(),
    )
    try:
        client.get_access_token()
    except PermanentError as exc:
        if exc.raw.get("errcode") == ERRCODE_IP_NOT_IN_WHITELIST:
            return Check("公众号 IP 白名单(40164)", "FAIL", f"{exc.message}")
        return Check("公众号 IP 白名单(40164)", "FAIL", exc.message)
    except PublishError as exc:
        return Check("公众号 IP 白名单(40164)", "WARN", f"探测失败（可重试）：{exc}")
    except Exception as exc:  # 门禁不该因为意外异常整体崩掉
        return Check("公众号 IP 白名单(40164)", "WARN", f"探测失败：{type(exc).__name__}: {exc}")
    finally:
        client.close()
    return Check("公众号 IP 白名单(40164)", "OK", "stable_token 获取成功，出口 IP 已在白名单")


def check_imagegen(settings: Settings, offline: bool) -> list[Check]:
    """生图（P11）。**默认不真发图**：探一次就是一张图的钱。

    要真探就置 ``SW_PREFLIGHT_IMAGEGEN=true`` 且不带 ``--offline``。权限没开时
    给出可执行的开通指引，而不是丢一个 permission_error 让人自己猜。
    """
    from generation.imagegen import imagegen_status

    status = imagegen_status(settings)
    if not status.ready:
        # 配图缺席只是"内容没有真实照片"，不影响出稿，所以是 WARN 不是 FAIL
        detail = status.reason + (f"；{status.hint}" if status.hint else "")
        return [Check("生图配置", "WARN", detail)]

    checks = [
        Check(
            "生图配置",
            "OK",
            f"model={status.model} base={status.base_url} enabled={status.enabled}（未做联网校验）",
        )
    ]
    if offline:
        checks.append(Check("生图权限", "SKIP", "--offline"))
        return checks
    if not settings.sw_preflight_imagegen:
        checks.append(
            Check(
                "生图权限",
                "SKIP",
                "SW_PREFLIGHT_IMAGEGEN=false（默认）：探测会真生成一张图，要花钱。"
                "确实要探就临时置 true 再跑一次",
            )
        )
        return checks
    checks.append(_probe_imagegen(settings))
    return checks


def _probe_imagegen(settings: Settings) -> Check:
    """真发一张方图探权限。上游不支持更小的尺寸，所以省不了这笔钱。

    顺带探的第二件事：**提示词画幅指令还灵不灵**。生成侧靠它出对形状（``size``
    参数本网关不认），换模型或换网关都可能让它失效，而失效是静默的——出的图
    只是形状不对，不会报错。所以这里把实返比例和目标比例一起打出来。
    """
    import tempfile

    from generation.imagegen import (
        ASPECT_SQUARE_1_1,
        ENABLE_HINT,
        ImagegenClient,
        ImagegenError,
        ImagegenNotEnabled,
    )

    with tempfile.TemporaryDirectory() as tmp:
        try:
            with ImagegenClient(settings=settings, timeout=120.0) as client:
                image = client.generate(
                    "a plain matte ceramic bowl on a linen cloth, soft window light,"
                    " minimal still life, no text, no logo, no people",
                    Path(tmp) / "probe.png",
                    aspect=ASPECT_SQUARE_1_1,
                    purpose="preflight",
                )
        except ImagegenNotEnabled as exc:
            return Check("生图权限", "FAIL", f"{exc}。{ENABLE_HINT}")
        except ImagegenError as exc:
            return Check("生图权限", "FAIL", str(exc))
        except Exception as exc:  # pragma: no cover - 兜底，探测不该炸门禁
            return Check("生图权限", "WARN", f"探测失败：{type(exc).__name__}: {exc}")
    ratio = image.aspect
    shape = f"比例 {ratio:.3f}（目标 {ASPECT_SQUARE_1_1.ratio:.3f}）" if ratio else "比例未量到"
    if ratio is not None and abs(ratio - ASPECT_SQUARE_1_1.ratio) > 0.15:
        return Check(
            "生图权限",
            "WARN",
            f"能出图但形状不对：请求 {ASPECT_SQUARE_1_1.key} 实返 {image.size_text} {shape}。"
            "提示词画幅指令可能已对这台网关失效，配图会变成生完再裁",
        )
    return Check(
        "生图权限",
        "OK",
        f"真出图成功：model={image.model} 实返 {image.size_text} {shape}",
    )


def check_pexels(settings: Settings, offline: bool) -> Check:
    if not settings.pexels_api_key:
        return Check("Pexels API Key", "WARN", "未配置，MoneyPrinterTurbo 无素材源（P3 才需要）")
    if offline:
        return Check("Pexels API Key", "SKIP", "--offline")
    try:
        resp = httpx.get(
            "https://api.pexels.com/v1/search",
            params={"query": "city", "per_page": 1},
            headers={"Authorization": settings.pexels_api_key},
            timeout=8.0,
        )
    except Exception as exc:
        return Check("Pexels API Key", "WARN", f"探测失败（网络问题）：{exc}")
    if resp.status_code == 200:
        return Check("Pexels API Key", "OK", "可用")
    return Check("Pexels API Key", "FAIL", f"HTTP {resp.status_code}: {resp.text[:80]}")


def check_pixabay(settings: Settings, offline: bool) -> Check:
    """Pixabay 是 Pexels 的备选素材源，两个都没有则 MPT 渲染必然失败。"""
    if not settings.pixabay_api_key:
        return Check("Pixabay API Key", "WARN", "未配置（Pexels 可用时不影响）")
    if offline:
        return Check("Pixabay API Key", "SKIP", "--offline")
    try:
        resp = httpx.get(
            "https://pixabay.com/api/videos/",
            params={"key": settings.pixabay_api_key, "q": "city", "per_page": 3},
            timeout=8.0,
        )
    except Exception as exc:
        return Check("Pixabay API Key", "WARN", f"探测失败（网络问题）：{exc}")
    if resp.status_code == 200:
        return Check("Pixabay API Key", "OK", "可用")
    return Check("Pixabay API Key", "FAIL", f"HTTP {resp.status_code}: {resp.text[:80]}")


#: MPT sidecar 的实际配置文件（素材源 key 在里面，不进 git）
MPT_CONFIG = ROOT / "sidecars" / "mpt" / "config.toml"


def check_mpt(settings: Settings, offline: bool) -> list[Check]:
    """MoneyPrinterTurbo：配置文件 + 素材源 key + API 存活。

    素材源 key 有两处：``.env``（本脚本能读，用于探测 key 本身是否有效）与
    **sidecar 的 config.toml**（MPT 真正读的地方）。两边不同步是最常见的
    "key 明明配了却还是 materials 阶段失败"，所以这里两边都看。
    """
    checks: list[Check] = []

    if not MPT_CONFIG.is_file():
        checks.append(
            Check(
                "MPT config.toml",
                "WARN",
                f"{MPT_CONFIG.relative_to(ROOT)} 不存在。"
                "先 cp sidecars/mpt/config.example.toml sidecars/mpt/config.toml，"
                "否则 docker 会在该路径建一个**目录**，容器起不来",
            )
        )
    else:
        checks.append(_check_mpt_config_keys())

    if offline:
        checks.append(Check("MoneyPrinterTurbo API", "SKIP", "--offline"))
        return checks
    checks.append(_probe_mpt(settings))
    return checks


def _check_mpt_config_keys() -> Check:
    """看 config.toml 里的素材源 key 列表是不是空的（不打印 key 本身）。"""
    import tomllib

    try:
        data = tomllib.loads(MPT_CONFIG.read_text(encoding="utf-8"))
    except Exception as exc:
        return Check("MPT config.toml", "FAIL", f"解析失败：{exc}")
    app = data.get("app") or {}
    counts = {name: len(app.get(f"{name}_api_keys") or []) for name in ("pexels", "pixabay")}
    if not any(counts.values()):
        return Check(
            "MPT config.toml",
            "FAIL",
            "pexels_api_keys / pixabay_api_keys 都是空的：渲染会在 materials 阶段失败",
        )
    detail = "、".join(f"{name} {n} 个 key" for name, n in counts.items() if n)
    if app.get("llm_provider"):
        detail += "；⚠ llm_provider 非空——本项目由 Claude 写稿灌入，这里应留空"
    if not app.get("subtitle_provider"):
        detail += "；⚠ subtitle_provider 为空，成片不会有字幕"
    return Check("MPT config.toml", "OK", detail)


def _probe_mpt(settings: Settings) -> Check:
    """用真实客户端探活，保证门禁与生成路径的错误映射完全一致。

    上游没有 ``/health`` 路由，客户端用只读的任务列表代替（不建任务、不占队列）。
    """
    from generation.mpt_client import MptClient
    from publishers.base import PermanentError, PublishError

    client = MptClient(
        settings.mpt_base_url,
        timeout=5.0,
        api_key=settings.mpt_api_key,
    )
    try:
        info = client.health()
    except PermanentError as exc:
        return Check("MoneyPrinterTurbo API", "FAIL", exc.message)
    except PublishError as exc:
        return Check(
            "MoneyPrinterTurbo API",
            "WARN",
            f"不可达（未启动则正常）：{exc}。启动：docker compose --profile video up -d mpt",
        )
    except Exception as exc:  # 门禁不该因为意外异常整体崩掉
        return Check("MoneyPrinterTurbo API", "WARN", f"探测失败：{type(exc).__name__}: {exc}")
    finally:
        client.close()
    return Check(
        "MoneyPrinterTurbo API",
        "OK",
        f"{settings.mpt_base_url} 可用（历史任务 {info.get('total_tasks')} 个）",
    )


def load_accounts(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(data.get("accounts") or [])


def check_accounts_synced(path: Path) -> list[Check]:
    """台账 ↔ DB 一致性（P4）。

    这是**首次部署最容易漏的一步**：``accounts.yaml`` 里写了账号但没人跑 sync，
    于是 `/dev/*` 与调度器一律说"账号不存在"，看起来像是代码坏了。
    """
    from core.accounts import AccountsError, diff_report, load_specs

    try:
        specs = load_specs(path)
    except AccountsError as exc:
        return [Check("账号台账", "FAIL", f"{path.name} 不合法：{exc}")]
    if not specs:
        return [Check("账号台账", "WARN", f"{path} 不存在或为空")]

    checks = [Check("账号台账", "OK", f"{path.name}: {len(specs)} 个账号，格式合法")]
    try:
        from core import db

        db.configure()
        db.init_db()
        with db.session_scope() as session:
            report = diff_report(session, specs)
    except Exception as exc:
        checks.append(Check("台账已入库", "WARN", f"读 DB 失败：{type(exc).__name__}: {exc}"))
        return checks

    if report.created:
        checks.append(
            Check(
                "台账已入库",
                "FAIL",
                f"这些账号还没入库：{report.created}。"
                "先跑 `uv run python -m core.accounts sync`，否则调度器看不见它们",
            )
        )
    elif report.updated:
        checks.append(
            Check(
                "台账已入库",
                "WARN",
                f"台账改过但没同步：{report.updated}。跑 `python -m core.accounts sync`",
            )
        )
    else:
        detail = f"{len(report.unchanged)} 个账号与 DB 一致"
        if report.orphans:
            detail += f"；DB 里还有台账外的账号 {report.orphans}（只提示，不会删）"
        checks.append(Check("台账已入库", "OK", detail))
    return checks


def check_deprecated_env() -> Check:
    """历史环境变量别名（仍然生效，但该改名了）。"""
    from core.config import deprecated_env_aliases

    stale = deprecated_env_aliases()
    if not stale:
        return Check("环境变量命名", "OK", "没有使用已弃用的别名")
    pairs = "、".join(f"{old} → {new}" for old, new in stale)
    return Check("环境变量命名", "WARN", f"仍在用弃用别名（当前仍生效）：{pairs}")


def check_trendradar(settings: Settings, offline: bool) -> list[Check]:
    """TrendRadar sidecar（P4，GPL，只走 HTTP 读它产出的文件）。"""
    from sourcing import trendradar

    if not settings.trendradar_base_url:
        return [
            Check(
                "TrendRadar",
                "WARN",
                "TRENDRADAR_BASE_URL 未配置，该选题源不可用（其它源不受影响）。"
                "启动：docker compose --profile sourcing up -d trendradar",
            )
        ]
    config_dir = ROOT / "sidecars" / "trendradar" / "config"
    required = [config_dir / "config.yaml", config_dir / "frequency_words.txt"]
    missing = [p.name for p in required if not p.is_file()]
    checks: list[Check] = []
    if missing:
        checks.append(
            Check(
                "TrendRadar 配置",
                "WARN",
                f"缺 {missing}：上游 entrypoint.sh 会直接 exit 1。"
                "先 cp sidecars/trendradar/*.example.* 到 sidecars/trendradar/config/",
            )
        )
    else:
        checks.append(Check("TrendRadar 配置", "OK", "config.yaml + frequency_words.txt 就位"))

    if offline:
        checks.append(Check("TrendRadar API", "SKIP", "--offline"))
        return checks
    try:
        info = trendradar.health(timeout=3.0)
    except Exception as exc:
        checks.append(
            Check("TrendRadar API", "WARN", f"不可达（未启动则正常）：{type(exc).__name__}: {exc}")
        )
        return checks
    checks.append(
        Check(
            "TrendRadar API", "OK", f"{info['base_url']} HTTP {info['status_code']}（静态文件服务）"
        )
    )
    return checks


def check_douyin_service(settings: Settings, offline: bool) -> Check:
    """抖音宿主机上传器：不仅要活着，还必须**有头**（docs/POLICY.md）。"""
    if offline:
        return Check("抖音上传器 headless", "SKIP", "--offline")
    try:
        resp = httpx.get(f"{settings.douyin_service_url.rstrip('/')}/health", timeout=3.0)
        payload = resp.json()
    except Exception as exc:
        return Check(
            "抖音上传器 headless",
            "WARN",
            f"不可达（未启动则正常）：{type(exc).__name__}。"
            "启动：uv run python -m publishers.douyin serve --port 8710",
        )
    headless = payload.get("headless")
    if headless is True:
        return Check(
            "抖音上传器 headless",
            "FAIL",
            "上传器跑在 headless 模式下：抖音会当场识破。必须在有图形界面的宿主机上跑",
        )
    return Check("抖音上传器 headless", "OK", f"headless={headless}（有头，符合 POLICY）")


def check_sidecars(settings: Settings, accounts: list[dict], offline: bool) -> list[Check]:
    """各账号 sidecar 端点可达性。"""
    targets: list[tuple[str, str]] = []
    for account in accounts:
        sidecar = account.get("sidecar") or {}
        port = sidecar.get("port")
        if port:
            targets.append((f"sidecar {account['id']}", f"http://localhost:{port}"))
    for account_id, url in settings.xhs_endpoint_map().items():
        targets.append((f"sidecar {account_id}(env)", url))
    # MoneyPrinterTurbo 不在这里裸探：它没有 / 路由，裸 GET 只会 404。
    # 走 check_mpt() 的真实客户端（GET /api/v1/tasks?page_size=1）。
    targets.append(("XHS-Downloader", settings.xhs_downloader_base_url))
    targets.append(("抖音上传器(宿主机)", settings.douyin_service_url))

    checks: list[Check] = []
    seen: set[str] = set()
    for name, url in targets:
        if url in seen:
            continue
        seen.add(url)
        if offline:
            checks.append(Check(name, "SKIP", f"--offline（{url}）"))
            continue
        checks.append(_probe_http(name, url))
    return checks


def _probe_http(name: str, url: str) -> Check:
    try:
        resp = httpx.get(url, timeout=3.0)
    except Exception as exc:
        return Check(name, "WARN", f"不可达 {url}：{type(exc).__name__}（未启动则正常）")
    return Check(name, "OK", f"{url} HTTP {resp.status_code}")


def check_docker() -> list[Check]:
    binary = shutil.which("docker")
    if binary is None:
        return [Check("docker", "WARN", "未安装；sidecar 与 core 容器化部署不可用")]
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception as exc:
        return [Check("docker", "WARN", f"docker info 执行失败：{exc}")]
    if proc.returncode != 0:
        return [Check("docker", "WARN", "docker 已安装但守护进程未运行")]
    return [Check("docker", "OK", f"daemon {proc.stdout.strip()}")]


def check_dsh(settings: Settings, offline: bool) -> list[Check]:
    """deepseek-harness 后端探测：SDK / runtime 二进制 / 零工具组合 / 凭据 / 握手。"""
    name = "dsh 后端"
    if settings.sw_llm_backend != "dsh":
        return [Check(name, "SKIP", "SW_LLM_BACKEND=anthropic，未启用 dsh")]

    checks: list[Check] = []
    try:
        from generation import llm_dsh
    except ImportError as exc:  # pragma: no cover - 本仓库自带该模块
        return [Check(name, "FAIL", f"generation.llm_dsh 导入失败：{exc}")]

    # 1) SDK + runtime 二进制
    runtime_available = False
    try:
        import deepseek_harness  # noqa: F401
        from deepseek_harness_runtime import bundled_runtime_path
    except ImportError:
        checks.append(Check(f"{name} SDK", "FAIL", llm_dsh.INSTALL_HINT))
    else:
        try:
            exe = bundled_runtime_path()
        except Exception as exc:
            checks.append(Check(f"{name} runtime", "FAIL", f"runtime 二进制不可用：{exc}"))
        else:
            runtime_available = True
            checks.append(Check(f"{name} runtime", "OK", f"{exe.name}"))

    # 2) 受限组合：文件在不在 + 零工具红线
    cordis = ROOT / settings.dsh_cordis_path
    entries = None
    if not cordis.is_file():
        checks.append(Check(f"{name} cordis 组合", "FAIL", f"文件不存在：{cordis}"))
    else:
        try:
            entries = llm_dsh.load_composition(cordis)
        except Exception as exc:
            checks.append(Check(f"{name} cordis 组合", "FAIL", f"解析失败：{exc}"))
        else:
            findings = llm_dsh.audit_composition(entries)
            checks.append(
                Check(
                    f"{name} 零工具红线",
                    "FAIL" if findings else "OK",
                    "；".join(findings) or "组合内无任何工具/执行器插件",
                )
            )

            if settings.dsh_model_routing:
                from generation.model_routing import (
                    assert_complete_production_coverage,
                    routing_model_requirements,
                )

                requirements = routing_model_requirements(
                    sol_model=settings.dsh_sol_model,
                    luna_model=settings.dsh_luna_model,
                )
                try:
                    assert_complete_production_coverage()
                    route_findings = llm_dsh.audit_provider_models(
                        entries, settings.dsh_provider, requirements
                    )
                except ValueError as exc:
                    route_findings = [str(exc)]
                checks.append(
                    Check(
                        f"{name} 模型路由",
                        "FAIL" if route_findings else "OK",
                        "；".join(route_findings)
                        or f"{len(requirements)} 个路由模型与各自的 reasoning effort 均已声明",
                    )
                )
            else:
                checks.append(
                    Check(f"{name} 模型路由", "SKIP", "SW_DSH_MODEL_ROUTING=false，沿用单模型")
                )

            # 3) 前缀缓存开关：漏了这一行，prompt_cache_key 根本不上线
            retention = llm_dsh.provider_cache_retention(entries, settings.dsh_provider)
            checks.append(
                Check(
                    f"{name} 前缀缓存",
                    "OK" if retention == "long" else "FAIL",
                    f"{settings.dsh_provider} · cacheRetention={retention or '<未声明>'}"
                    + (
                        ""
                        if retention == "long"
                        else "；pi-ai 只在 cacheRetention=long 时才发 prompt_cache_key，"
                        "私有网关下缺了它命中率恒为 0"
                    ),
                )
            )

            # 4) 所选路由的凭据引用
            env_name = llm_dsh.provider_api_key_env(entries, settings.dsh_provider)
            if env_name is None:
                checks.append(
                    Check(
                        f"{name} provider",
                        "FAIL",
                        f"组合里没有名为 {settings.dsh_provider!r} 的 provider 路由"
                        "（SW_DSH_PROVIDER 写错？）",
                    )
                )
            elif os.environ.get(env_name):
                checks.append(
                    Check(f"{name} provider", "OK", f"{settings.dsh_provider} · {env_name} 已配置")
                )
            else:
                checks.append(
                    Check(
                        f"{name} provider",
                        "FAIL",
                        f"{settings.dsh_provider} 需要 {env_name}，当前为空",
                    )
                )

    if entries is None:
        checks.extend(
            [
                Check(f"{name} 零工具红线", "FAIL", "cordis 组合不可读，无法审计工具/执行器插件"),
                Check(f"{name} 前缀缓存", "FAIL", "cordis 组合不可读，无法检查 cacheRetention"),
                Check(f"{name} provider", "FAIL", "cordis 组合不可读，无法检查 provider 凭据引用"),
            ]
        )
        if settings.dsh_model_routing:
            checks.append(Check(f"{name} 模型路由", "FAIL", "cordis 组合不可读，无法审计路由模型"))

    # 5) 真起一次 runtime 完成握手（联网检查开关下才做：它要拉子进程）
    if not runtime_available:
        checks.append(Check(f"{name} 握手", "SKIP", "SDK/runtime 不可用，跳过 runtime 拉起"))
        return checks
    if offline:
        checks.append(Check(f"{name} 握手", "SKIP", "--offline 跳过 runtime 拉起"))
        return checks
    try:
        options = llm_dsh.DshRuntimeOptions.from_settings(settings)
        # 路由开启时拿 medium 档那一组真起一次握手：它是生产里调用最密集的一档。
        # 模型与 effort 都从路由表取，不在这里复写一份会漂的常量。
        if settings.dsh_model_routing:
            from generation.model_routing import COMPLEXITY_EFFORT, tier_models

            model = tier_models(
                sol_model=settings.dsh_sol_model, luna_model=settings.dsh_luna_model
            )["medium"]
            effort = COMPLEXITY_EFFORT["medium"]
        else:
            model = settings.dsh_model
            effort = settings.llm_effort
        key = llm_dsh.RuntimeKey(
            model=model,
            effort=effort,
            max_tokens=settings.llm_max_tokens,
        )
        harness = llm_dsh.default_harness_factory(key, options)
    except Exception as exc:
        checks.append(Check(f"{name} 握手", "FAIL", f"runtime 起不来：{exc}"))
        return checks
    try:
        checks.append(Check(f"{name} 握手", "OK", "initialize 成功，会话可创建"))
    finally:
        harness.close()
    return checks


def check_notifier(settings: Settings) -> Check:
    """通知通道 **+ 人工确认闸门通道**（R1）。

    这一项过去只看飞书，于是门禁对"人工确认闸门的载体是死是活"一无所知——而 R1
    （内容上线必须由人在 Telegram 闸门消息上点一下）靠的正是 Telegram：飞书只能**收通知**，
    接不了确认闸门的回调按钮，两者不能互相顶替。

    **只返回 OK / WARN，永不 FAIL**：preflight 会在 ``scripts/ops/update.sh --apply``
    的部署流程里跑，这里出一个 FAIL 就直接卡死生产部署。通道降级要被看见，
    但不该有那种杀伤力——真正的硬互锁在 ``scripts/ops/verify.sh``
    （真发布开启时通道必须 ready+polling），那里才是裁定的地方。

    刻意**不看 polling**：preflight 是一次性进程（容器里 ``python3 scripts/preflight.py``），
    它自己从来不起长轮询线程，``polling`` 在这里恒为 false，拿来判定只会天天误报。

    只取 :func:`core.telegram.channel_status` 的布尔量与 ``detail``——该契约保证
    不含 token / chat_id，脱敏指纹也不给。
    """
    name = "通知通道"
    feishu = "飞书 webhook 已配置" if settings.feishu_webhook else "未配置 FEISHU_WEBHOOK"
    try:
        # 导入必须留在 try 里：本函数的不变量是"永不 FAIL、永不抛未捕获异常"
        # （preflight 会在 scripts/ops/update.sh --apply 的部署流程里跑）。
        # 现实中 core.telegram 只依赖 stdlib + httpx、不会导入失败，但把 import
        # 放在保护范围外，等于让这条不变量依赖一个未被强制的前提。
        from core.telegram import channel_status

        status = channel_status(settings)
    except Exception as exc:  # 体检本身绝不能把部署门禁打红
        # 只报异常类型：异常消息可能把它读到的配置片段带出来，而这条通道的配置全是凭据。
        return Check(
            name,
            "WARN",
            f"读不到 Telegram 通道状态（{type(exc).__name__}）："
            f"跑 uv run python -m core.telegram check 看细节；{feishu}",
        )

    detail = str(status.get("detail") or "").strip()
    # ``ready`` 只说"token + chat_id 都有"，**不看总开关**：SW_TELEGRAM_ENABLED=false 时
    # build_telegram_notifier 直接返回 None，一条都发不出去。所以两个都要看。
    if status.get("enabled") and status.get("ready"):
        bot = str(status.get("username") or "").strip()
        who = f"（bot @{bot}）" if bot else ""
        if not status.get("can_sign"):
            # 没有签名密钥就不发带按钮的卡片（core/telegram.py），确认闸门等于没有载体。
            return Check(
                name,
                "WARN",
                f"Telegram 能推消息{who}，但没有 callback 签名密钥，不会发带按钮的确认卡片："
                f"在 .env 配 SW_TELEGRAM_SIGNING_SECRET（或 SW_UI_TOKEN）；{feishu}",
            )
        return Check(name, "OK", f"Telegram 人工确认闸门通道可用{who}；{feishu}")

    if status.get("configured"):
        return Check(
            name,
            "WARN",
            f"Telegram 已配 bot 但确认闸门通道不可用：{detail or '通道未就绪'}；{feishu}",
        )

    if settings.feishu_webhook:
        return Check(
            name,
            "WARN",
            "只有飞书 webhook：飞书只能收通知，接不了确认闸门的回调按钮，"
            "顶不了 R1（内容上线必须由人在 Telegram 闸门消息上点一下）。"
            f"补 TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID：{detail or '先配 Telegram bot'}",
        )

    return Check(
        name,
        "WARN",
        "未配置 FEISHU_WEBHOOK，退化为日志通知；Telegram 确认闸门通道也没配，"
        f"人收不到要点的确认卡片：{detail or '先配 Telegram bot'}",
    )


def check_budget(settings: Settings) -> Check:
    if settings.daily_image_budget < 0:
        return Check("成本闸门", "FAIL", "DAILY_IMAGE_BUDGET 不能为负")
    if settings.daily_token_budget <= 0 or settings.daily_render_seconds_budget <= 0:
        return Check("成本闸门", "FAIL", "日预算必须为正数，否则闸门形同虚设")
    return Check(
        "成本闸门",
        "OK",
        f"tokens={settings.daily_token_budget} "
        f"render_seconds={settings.daily_render_seconds_budget} "
        f"images={settings.daily_image_budget}",
    )


def check_output_budget(settings: Settings) -> Check:
    """输出预算体检：今天有几次调用是被 ``max_tokens`` 掐停收尾的（P11.2）。

    截断不会自己冒头——自愈重试让链路照常出稿，只是白烧一倍 token；
    直到某次思考跑偏、正文一个字都没写出来才 502。这里把它显式报出来，
    预算长期贴边时人能在它变成事故之前先看到，去调
    ``generation/output_budget.py`` 的分档。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session as OrmSession

    from core.budget import today_key
    from core.stats import truncated_calls

    name = "输出预算"
    url = settings.sw_database_url
    if url.startswith("sqlite:///"):
        path = Path(url[len("sqlite:///") :]).expanduser()
        if not path.exists():
            return Check(name, "SKIP", "数据库还没建，跑起来之后再看")
    # 刻意不用 core.db.configure：那会改掉进程级的 session 工厂，
    # 一个只读体检不该有这种副作用
    engine = create_engine(url, future=True)
    try:
        with OrmSession(engine) as session:
            counts = truncated_calls(session, today_key())
    except Exception as exc:
        return Check(name, "SKIP", f"读不到成本账本：{exc}")
    finally:
        engine.dispose()

    if not counts:
        return Check(name, "OK", "今天没有调用顶到输出上限")
    detail = "、".join(
        f"{purpose} × {n}" for purpose, n in sorted(counts.items(), key=lambda kv: -kv[1])
    )
    return Check(name, "WARN", f"今天有调用被 max_tokens 掐停（{detail}），考虑给这些调用点提档")


def check_database(settings: Settings) -> Check:
    url = settings.sw_database_url
    if not url.startswith("sqlite:///"):
        return Check("数据库", "OK", url)
    path = Path(url[len("sqlite:///") :]).expanduser()
    parent = path.parent if path.parent.as_posix() else Path(".")
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return Check("数据库", "FAIL", f"目录不可创建 {parent}：{exc}")
    return Check("数据库", "OK", f"{url}（目录可写）")


def check_render_chain(settings: Settings) -> list[Check]:
    """渲染链在不在：Playwright/chromium（封面与小红书卡片）+ Node（公众号正文）。

    【为什么必须有这两格 —— 2026-08-26 在生产上被这件事咬过】这两样都是**可选依赖**，
    缺了不会让 core 起不来、``/health`` 与 ``/api/v1/system/info`` 一格都不会红。表现要等到
    某条稿走完生成链、在机器审核里挂 ``inspect.douyin.cover.missing`` 才看得见——那时它已经
    在审核队列里了，而门禁刚刚才对着同一台机器说过"通过"。**一道号称回答"这台机器能不能干活"
    的门禁，对一整个模块的缺失保持沉默，比没有这道门禁更糟。**

    定成 WARN 不是 FAIL：三平台里只有小红书与抖音**必须**有图，公众号纯文也能发，所以缺渲染
    链不该把整台机器判死。它要做的是把"你这台机器出的稿子会在审核那一关被拦下"提前说出来。
    """
    from generation.cover import INSTALL_HINT, playwright_available
    from generation.wechat_render import node_available

    checks: list[Check] = []
    if playwright_available():
        checks.append(Check("渲染链 Playwright", "OK", "已安装：封面与小红书卡片能渲"))
    else:
        checks.append(
            Check(
                "渲染链 Playwright",
                "WARN",
                "未安装：**出不了封面与卡片**。小红书/抖音的稿子会在机器审核挂 "
                f"inspect.*.cover.missing 并被拦下（公众号纯文不受影响）。装：{INSTALL_HINT}",
            )
        )

    node_bin = settings.wenyan_node_bin
    if node_available(node_bin):
        checks.append(Check("渲染链 Node", "OK", f"{node_bin} 可用：公众号正文能渲"))
    else:
        checks.append(
            Check(
                "渲染链 Node",
                "WARN",
                f"找不到 {node_bin}：公众号正文渲不出 body_html（WECHAT_BACKEND=wenyan 时必需）。"
                "装 Node 或改 WENYAN_NODE_BIN",
            )
        )
    return checks


def run_checks(*, offline: bool, accounts_path: Path) -> list[Check]:
    settings = Settings()
    accounts = load_accounts(accounts_path)
    checks: list[Check] = [
        check_env_file(),
        check_database(settings),
        check_anthropic(settings),
        *check_dsh(settings, offline),
        *check_imagegen(settings, offline),
        *check_render_chain(settings),
        *check_wechat(settings, offline),
        check_pexels(settings, offline),
        check_pixabay(settings, offline),
        *check_mpt(settings, offline),
        *check_trendradar(settings, offline),
        *check_accounts_synced(accounts_path),
        *check_sidecars(settings, accounts, offline),
        check_douyin_service(settings, offline),
        *check_docker(),
        check_notifier(settings),
        check_budget(settings),
        check_output_budget(settings),
        check_deprecated_env(),
        check_schedule(settings),
    ]
    return checks


def check_schedule(settings: Settings) -> Check:
    """调度参数是否自洽（P4）。"""
    from core.accounts import resolve_timezone

    problems: list[str] = []
    if settings.sw_publish_batch_size <= 0:
        problems.append("SW_PUBLISH_BATCH_SIZE 必须为正")
    if settings.sw_retry_backoff_max_seconds < settings.sw_retry_backoff_base_seconds:
        problems.append("SW_RETRY_BACKOFF_MAX_SECONDS 小于 BASE，退避会被夹成常数")
    if settings.sw_max_publish_attempts < 1:
        problems.append("SW_MAX_PUBLISH_ATTEMPTS 必须 >= 1")
    tz = resolve_timezone(settings.sw_timezone)
    if str(tz) != settings.sw_timezone:
        problems.append(
            f"时区 {settings.sw_timezone!r} 不可用（缺 tzdata？），发布时段窗口会按 UTC 判定"
        )
    if problems:
        return Check("调度参数", "FAIL" if len(problems) > 1 else "WARN", "；".join(problems))
    return Check(
        "调度参数",
        "OK",
        f"时区 {settings.sw_timezone} · 批量 {settings.sw_publish_batch_size} "
        f"· 重试上限 {settings.sw_max_publish_attempts} 次 "
        f"· 退避 {settings.sw_retry_backoff_base_seconds}–"
        f"{settings.sw_retry_backoff_max_seconds}s",
    )


STYLES = {"OK": "green", "WARN": "yellow", "FAIL": "bold red", "SKIP": "dim"}


def render(checks: list[Check]) -> None:
    table = Table(title="social_workflow preflight", show_lines=False)
    table.add_column("检查项", style="bold")
    table.add_column("结果", justify="center")
    table.add_column("说明")
    for check in checks:
        table.add_row(check.name, f"[{STYLES[check.status]}]{check.status}[/]", check.detail)
    console.print(table)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="social_workflow 门禁自检")
    parser.add_argument("--offline", action="store_true", help="跳过所有网络检查")
    parser.add_argument("--strict", action="store_true", help="把 WARN 也当作失败")
    parser.add_argument(
        "--accounts",
        type=Path,
        default=None,
        help="账号台账 YAML 路径，默认 SW_ACCOUNTS_FILE 或 accounts.yaml",
    )
    args = parser.parse_args(argv)

    from core.accounts import accounts_file_path

    checks = run_checks(offline=args.offline, accounts_path=args.accounts or accounts_file_path())
    render(checks)

    fails = [c for c in checks if c.status == "FAIL"]
    warns = [c for c in checks if c.status == "WARN"]
    console.print(
        f"合计 {len(checks)} 项：[green]OK {sum(c.status == 'OK' for c in checks)}[/] · "
        f"[yellow]WARN {len(warns)}[/] · [bold red]FAIL {len(fails)}[/] · "
        f"[dim]SKIP {sum(c.status == 'SKIP' for c in checks)}[/]"
    )
    if fails:
        console.print("[bold red]门禁未通过[/]，请先修复上面的 FAIL 项。")
        return 1
    if warns and args.strict:
        console.print("[bold red]--strict 下 WARN 视为失败[/]")
        return 1
    if warns:
        console.print("[yellow]存在 WARN：不阻塞开发，但会阻塞对应平台的真实发布。[/]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
