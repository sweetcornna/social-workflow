"""Markdown → 公众号内联样式 HTML，走 ``@wenyan-md/cli``（Apache-2.0）子进程。

为什么是子进程而不是 Python 库：公众号编辑器只认**内联样式**，
所有 CSS 必须 inline 到每个标签的 ``style`` 属性上。这套主题 + 内联化逻辑在
``caol64/wenyan-cli`` 里已经很成熟，重写一遍没有价值。它是 Node 包，
所以只能 ``npx`` 调用。

**明确不用** ``@doocs/md-cli``（它是 Web 编辑器服务，不是渲染 CLI）
与 ``@md/core``（private 包，npm 上没有构建产物）——见 ``docs/THIRD_PARTY.md``。

没有 Node 时抛 :class:`NodeNotAvailable`，错误信息里给出安装方式，
不静默降级——公众号没有 ``body_html`` 就发不出去，静默返回原文只会让问题推迟暴露。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("social_workflow.generation.wechat_render")

UPSTREAM = "https://github.com/caol64/wenyan-cli"
UPSTREAM_LICENSE = "Apache-2.0"
NPM_PACKAGE = "@wenyan-md/cli"

#: wenyan 内置主题。``wenyan render --theme`` 的取值
KNOWN_THEMES = ("default", "orangeheart", "rainbow", "lapis", "pie", "maize", "purple", "phycat")


class RenderError(RuntimeError):
    """渲染失败。"""


class NodeNotAvailable(RenderError):
    """找不到 Node / npx。"""

    def __init__(self, executable: str) -> None:
        super().__init__(
            f"找不到可执行文件 {executable!r}：公众号渲染需要 Node.js（>=18）。\n"
            f"  macOS: brew install node\n"
            f"  或用 nvm 安装后确保 PATH 里有 npx\n"
            f"渲染用的是 {NPM_PACKAGE}（{UPSTREAM_LICENSE}, {UPSTREAM}），"
            f"首次执行 npx 会自动下载。"
        )
        self.executable = executable


@dataclass(frozen=True)
class RenderResult:
    html: str
    theme: str
    command: list[str]

    @property
    def chars(self) -> int:
        return len(self.html)


def _resolve_executable(node_bin: str) -> str:
    """确认 npx / node 可用，返回绝对路径。"""
    resolved = shutil.which(node_bin)
    if resolved is None:
        raise NodeNotAvailable(node_bin)
    return resolved


def node_available(node_bin: str | None = None) -> bool:
    """探测 Node 是否可用，供 preflight 与测试跳过判断。"""
    from core.config import get_settings

    binary = node_bin or get_settings().node_bin
    return shutil.which(binary) is not None


def build_command(
    markdown_path: Path,
    *,
    theme: str,
    node_bin: str,
    npm_spec: str,
) -> list[str]:
    """构造 ``npx -y @wenyan-md/cli render <file> --theme <theme>``。

    ``-y`` 让 npx 首次执行时不交互确认，否则会挂在 CI 里等输入。
    """
    return [node_bin, "-y", npm_spec, "render", str(markdown_path), "--theme", theme]


def render_markdown(
    markdown: str,
    *,
    theme: str | None = None,
    node_bin: str | None = None,
    npm_spec: str | None = None,
    timeout: float | None = None,
    runner: object | None = None,
) -> RenderResult:
    """把 Markdown 渲染成内联样式 HTML。

    ``runner`` 供测试注入（签名同 ``subprocess.run``），生产留空。
    """
    from core.config import get_settings

    settings = get_settings()
    theme = theme or settings.wenyan_theme
    node_bin = node_bin or settings.node_bin
    npm_spec = npm_spec or settings.wenyan_npm_spec
    timeout = timeout or settings.wenyan_timeout_seconds

    if not markdown.strip():
        raise RenderError("Markdown 为空，没有可渲染的内容")
    if theme not in KNOWN_THEMES:
        # 不硬拦：上游可能加了新主题。但要留痕，方便排查"主题没生效"
        logger.warning("主题 %r 不在已知列表 %s 中，仍按原样传给 wenyan", theme, KNOWN_THEMES)

    run = runner if runner is not None else subprocess.run
    executable = node_bin if runner is not None else _resolve_executable(node_bin)

    # wenyan render 只接受文件路径，写到临时文件
    with tempfile.TemporaryDirectory(prefix="wenyan-") as tmpdir:
        source = Path(tmpdir) / "article.md"
        source.write_text(markdown, encoding="utf-8")
        command = build_command(source, theme=theme, node_bin=executable, npm_spec=npm_spec)
        logger.debug("执行渲染命令: %s", " ".join(command))
        try:
            completed = run(  # type: ignore[operator]
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                cwd=tmpdir,
            )
        except FileNotFoundError as exc:
            raise NodeNotAvailable(node_bin) from exc
        except subprocess.TimeoutExpired as exc:
            raise RenderError(f"wenyan 渲染超时（{timeout}s）：{exc}") from exc

        returncode = getattr(completed, "returncode", 1)
        stdout = getattr(completed, "stdout", "") or ""
        stderr = getattr(completed, "stderr", "") or ""
        if returncode != 0:
            raise RenderError(
                f"wenyan 渲染失败（exit={returncode}）：{stderr.strip() or stdout.strip()}"
            )

        html = stdout.strip()
        if not html:
            # 部分版本把结果写成同目录的 .html 文件而不是 stdout，兜底读一次
            produced = sorted(Path(tmpdir).glob("*.html"))
            if produced:
                html = produced[0].read_text(encoding="utf-8").strip()

    if not html:
        raise RenderError(
            "wenyan 没有产出 HTML：stdout 为空且临时目录里没有 .html 文件。"
            f"stderr={stderr.strip()[:300]}"
        )
    if "<" not in html:
        raise RenderError(f"wenyan 输出不像 HTML：{html[:200]}")

    logger.info("公众号 HTML 渲染完成：theme=%s, %d 字符", theme, len(html))
    return RenderResult(html=html, theme=theme, command=command)


__all__ = [
    "KNOWN_THEMES",
    "NPM_PACKAGE",
    "UPSTREAM",
    "UPSTREAM_LICENSE",
    "NodeNotAvailable",
    "RenderError",
    "RenderResult",
    "build_command",
    "node_available",
    "render_markdown",
]
