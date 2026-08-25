"""MVP 封面：HTML 模板 → Playwright 截图 PNG。

公众号需要两个尺寸：

- **900×383**（2.35:1）——图文消息头图，列表页与正文顶部用。
- **900×900**（1:1）——分享卡片缩略图。

抖音（P3）另需一个 **1080×1920**（9:16）竖版，作为成片封面（``sizes=("vertical",)``）。
同一套模板 / 配色，只换画布尺寸——封面不是本项目的差异化点，多一套模板只会多一处要维护。

Playwright 是**可选依赖**（``uv sync --extra render`` + ``playwright install chromium``）。
没装库、或装了库但没装浏览器时，:func:`render_cover` 返回 ``None`` 而不是抛异常——
封面缺失只会让列表页显示默认图，不该阻断整条生成链。
:func:`build_html` 不依赖 Playwright，任何环境都能跑，所以模板本身是可单测的。
"""

from __future__ import annotations

import base64
import html as html_escape
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger("social_workflow.generation.cover")

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
COVER_TEMPLATE = TEMPLATES_DIR / "wechat_cover.html"

CoverSize = Literal["banner", "square", "vertical"]

#: 封面尺寸。前两个是公众号（P1），``vertical`` 是抖音成片封面（P3，9:16 竖屏）
SIZES: dict[str, tuple[int, int]] = {
    "banner": (900, 383),
    "square": (900, 900),
    "vertical": (1080, 1920),
}

#: 配色方案。按标题 hash 取，同一篇文章每次生成的封面一致（便于比对与重跑）
PALETTES: tuple[dict[str, str], ...] = (
    {"bg": "#11304a", "fg": "#f5f7fa", "accent": "#f0a202"},
    {"bg": "#1d1d21", "fg": "#f2f2f0", "accent": "#e2574c"},
    {"bg": "#f4f1ea", "fg": "#23201c", "accent": "#c1553a"},
    {"bg": "#0f3d3e", "fg": "#eef6f4", "accent": "#7ac6a4"},
    {"bg": "#2b2350", "fg": "#f0edff", "accent": "#9d8df1"},
)


@dataclass(frozen=True)
class CoverSpec:
    """一张封面的全部可变量。"""

    title: str
    kicker: str = ""
    footer: str = ""
    size: CoverSize = "banner"
    palette_index: int | None = None
    #: 生图底图的本地路径（P11）。留空 = 老的纯色 + 斜纹版式。
    #: 图会被内嵌成 data: URI 并以 ``background-size: cover`` 居中裁切，所以
    #: **模型返回什么尺寸都不影响**最终截图的精确尺寸
    background: str = ""

    @property
    def dimensions(self) -> tuple[int, int]:
        return SIZES[self.size]

    @property
    def has_background(self) -> bool:
        return bool(self.background) and Path(self.background).is_file()

    def palette(self) -> dict[str, str]:
        if self.palette_index is not None:
            return PALETTES[self.palette_index % len(PALETTES)]
        # 用标题的稳定哈希选色：同一标题永远同一配色
        digest = sum(ord(c) for c in self.title) if self.title else 0
        return PALETTES[digest % len(PALETTES)]


def _metrics(width: int, height: int, title: str) -> dict[str, int]:
    """按尺寸与标题长度算字号与留白。标题越长字号越小，避免溢出。"""
    base = min(width, height)
    length = max(len(title), 1)
    if length <= 8:
        title_size = int(base * 0.155)
    elif length <= 14:
        title_size = int(base * 0.125)
    else:
        title_size = int(base * 0.095)
    return {
        "title_size": max(28, title_size),
        "kicker_size": max(13, int(base * 0.033)),
        "pad": int(width * 0.075),
        "gap": max(10, int(base * 0.035)),
        "rule": int(width * 0.11),
    }


def _data_uri(path: str | Path) -> str:
    """把本地图片读成 data: URI。读不出来返回空串（调用方退回纯色版式）。

    必须内嵌而不是写 ``file://``：:func:`screenshot_batch` 用 ``set_content`` 喂 HTML，
    页面没有 base URL，外链一律加载不到。
    """
    target = Path(path)
    try:
        payload = target.read_bytes()
    except OSError as exc:
        logger.warning("封面底图读不出来，回落纯色版式：%s（%s）", target, exc)
        return ""
    mime = "image/jpeg" if target.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    return f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"


def build_html(spec: CoverSpec) -> str:
    """渲染封面 HTML。纯字符串替换，不依赖 Playwright。"""
    if not COVER_TEMPLATE.is_file():  # pragma: no cover - 打包缺文件才触发
        raise FileNotFoundError(f"封面模板缺失: {COVER_TEMPLATE}")
    width, height = spec.dimensions
    palette = spec.palette()
    metrics = _metrics(width, height, spec.title)
    photo = _data_uri(spec.background) if spec.has_background else ""

    replacements = {
        "__WIDTH__": str(width),
        "__HEIGHT__": str(height),
        "__BG__": palette["bg"],
        "__FG__": palette["fg"],
        "__ACCENT__": palette["accent"],
        "__TITLE_SIZE__": str(metrics["title_size"]),
        "__KICKER_SIZE__": str(metrics["kicker_size"]),
        "__PAD__": str(metrics["pad"]),
        "__GAP__": str(metrics["gap"]),
        "__RULE__": str(metrics["rule"]),
        "__PHOTO__": photo,
        "__PHOTO_DISPLAY__": "block" if photo else "none",
        # 有照片就不要斜纹了：两层纹理叠在一起只会显得脏
        "__TEXTURE_DISPLAY__": "none" if photo else "block",
        # 文案必须转义：标题里出现 < & " 会破坏 HTML 结构
        "__TITLE__": html_escape.escape(spec.title),
        "__KICKER__": html_escape.escape(spec.kicker),
        "__FOOTER__": html_escape.escape(spec.footer),
    }
    content = COVER_TEMPLATE.read_text(encoding="utf-8")
    for token, value in replacements.items():
        content = content.replace(token, value)
    return content


def playwright_available() -> bool:
    """Playwright 库是否已安装（不检查浏览器是否已下载）。"""
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


# ------------------------------------------------------------ 通用截图能力
#
# 下面三样被公众号封面（本模块）与小红书卡片（generation/xhs_cards.py）共用。
# 两边对失败的态度不同：封面缺了只是列表页显示默认图，卡片缺了这条笔记根本发不出去。
# 所以底层统一**抛异常**，由各自的调用方决定是吞掉还是往上抛。


#: 浏览器缺失时给出的可执行修复命令
INSTALL_HINT = "uv sync --extra render && uv run playwright install chromium"


class ScreenshotUnavailable(RuntimeError):
    """Playwright 库未安装、chromium 未下载，或渲染进程起不来。"""


@dataclass(frozen=True)
class ScreenshotJob:
    """一次截图任务。``html`` 必须自包含（不外链任何资源）。"""

    html: str
    path: Path
    width: int
    height: int


def screenshot_batch(jobs: Sequence[ScreenshotJob], *, scale: float = 1.0) -> None:
    """批量截图，**一次**启动 chromium 截完所有任务。

    分批而不是一张一次：起一次浏览器要几百毫秒，小红书一条笔记有 4–9 张卡片，
    逐张启动会把渲染时间拖到十几秒（还要计进渲染时长预算）。

    ``scale`` 是 ``deviceScaleFactor``：``1.0`` 出原始像素，``0.3`` 出缩略图
    （生成文档预览图用，避免把 1242×1656 的大图塞进 git）。
    """
    if not jobs:
        return
    if not playwright_available():
        raise ScreenshotUnavailable(f"未安装 Playwright。安装：{INSTALL_HINT}")

    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    for job in jobs:
        job.path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                first = jobs[0]
                context = browser.new_context(
                    viewport={"width": first.width, "height": first.height},
                    device_scale_factor=scale,
                )
                page = context.new_page()
                for job in jobs:
                    page.set_viewport_size({"width": job.width, "height": job.height})
                    # set_content 直接喂 HTML，不落磁盘、不需要 file:// 协议
                    page.set_content(job.html, wait_until="load")
                    # 模板可能内嵌 data: URI 字体，字体没就绪就截图会拿到回退字形
                    page.evaluate("() => document.fonts.ready")
                    page.screenshot(path=str(job.path), type="png")
            finally:
                browser.close()
    except PlaywrightError as exc:
        # 最常见的是"浏览器未下载"，给出可执行的修复建议
        raise ScreenshotUnavailable(f"Playwright 截图失败：{exc}。修复：{INSTALL_HINT}") from exc


def screenshot_html(html: str, path: str | Path, width: int, height: int) -> Path:
    """截一张图。失败抛 :class:`ScreenshotUnavailable`。"""
    target = Path(path)
    screenshot_batch([ScreenshotJob(html=html, path=target, width=width, height=height)])
    return target


def render_cover(
    spec: CoverSpec,
    output_path: str | Path,
    *,
    screenshotter: Any | None = None,
) -> Path | None:
    """把封面截成 PNG。不可用时返回 ``None`` 并记 warning，**不抛异常**。

    ``screenshotter`` 供测试注入，签名为
    ``(html: str, path: Path, width: int, height: int) -> None``。
    """
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    width, height = spec.dimensions
    content = build_html(spec)

    if screenshotter is not None:
        screenshotter(content, target, width, height)
        return target

    try:
        screenshot_html(content, target, width, height)
    except ScreenshotUnavailable as exc:
        logger.warning("跳过封面生成：%s", exc)
        return None
    except Exception as exc:  # pragma: no cover - 兜底，封面不该拖垮生成链
        logger.warning("封面生成异常，跳过：%s", exc)
        return None

    logger.info("封面已生成: %s (%dx%d)", target, width, height)
    return target


def render_cover_set(
    title: str,
    output_dir: str | Path,
    *,
    kicker: str = "",
    footer: str = "",
    stem: str = "cover",
    sizes: tuple[CoverSize, ...] = ("banner", "square"),
    screenshotter: Any | None = None,
    background: str = "",
) -> dict[str, Path]:
    """生成一组封面（默认 banner + square）。返回**实际生成成功**的那些。

    ``background`` 是生图产出的底图路径（P11），留空则是老的纯色版式。
    """
    out = Path(output_dir)
    produced: dict[str, Path] = {}
    for size in sizes:
        spec = CoverSpec(
            title=title, kicker=kicker, footer=footer, size=size, background=background
        )
        path = render_cover(spec, out / f"{stem}-{size}.png", screenshotter=screenshotter)
        if path is not None:
            produced[size] = path
    return produced


__all__ = [
    "COVER_TEMPLATE",
    "INSTALL_HINT",
    "PALETTES",
    "SIZES",
    "TEMPLATES_DIR",
    "CoverSize",
    "CoverSpec",
    "ScreenshotJob",
    "ScreenshotUnavailable",
    "build_html",
    "playwright_available",
    "render_cover",
    "render_cover_set",
    "screenshot_batch",
    "screenshot_html",
]
