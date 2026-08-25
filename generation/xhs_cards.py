"""小红书图文卡片：HTML 模板 → Playwright 截图 → 3:4 PNG。

沿用 ``generation/cover.py`` 的范式：

- :func:`build_deck` 只做字符串替换，**不依赖 Playwright**，任何环境都能跑，
  所以模板本身可单测（HTML 快照断言）。
- 截图走 ``cover.screenshot_batch``（一次启动 chromium 截完整套卡片），
  或由调用方注入 ``screenshotter`` 替身。

与封面的关键差别是**失败态度**：公众号封面缺了只是列表页显示默认图，
小红书卡片缺了这条笔记根本发不出去（图文笔记至少 1 张图）。
所以这里不吞异常，直接抛 :class:`~generation.cover.ScreenshotUnavailable`，
由 :func:`generation.pipeline.generate_xhs_bundle` 决定是降级入队还是中断。

模板集在 ``generation/templates/xhs/<theme>/{cover,page}.html``，三套主题：

============ ==========================================================
``editorial`` 杂志风：深底、衬线、大留白、细网格
``swiss``     网格风：白底、粗横线、单一强调色、严格左对齐
``warm``      手账风：纸张底、虚线框、胶带与荧光笔
============ ==========================================================

字体一律走系统字体栈，不外链。可选地把 ``generation/templates/xhs/fonts/``
（或环境变量 ``XHS_FONT_DIR`` 指向的目录）里的字体以 ``data:`` URI 内嵌——
仍然不产生任何网络请求。
"""

from __future__ import annotations

import base64
import html as html_escape
import logging
import os
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

from generation.cover import ScreenshotJob, ScreenshotUnavailable, screenshot_batch
from generation.xhs_note import PageSpec, XhsNoteDraft

logger = logging.getLogger("social_workflow.generation.xhs_cards")

XHS_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates" / "xhs"

#: 小红书竖版 3:4。1242px 宽是 iPhone Plus 系列的物理像素宽度，
#: 传这个尺寸上去平台不会再放大，是社区里最稳的一档。
CARD_WIDTH = 1242
CARD_HEIGHT = 1656
CARD_SIZE = (CARD_WIDTH, CARD_HEIGHT)

#: 本地字体目录（可选）。放进去的字体会被 base64 内嵌，**不外链**。
FONT_DIR_ENV = "XHS_FONT_DIR"
DEFAULT_FONT_DIR = XHS_TEMPLATES_DIR / "fonts"
#: 内嵌字体总体积上限：超了截图会明显变慢，不如让它退回系统字体
MAX_EMBED_FONT_BYTES = 8 * 1024 * 1024
_FONT_FORMATS = {".woff2": "woff2", ".woff": "woff", ".ttf": "truetype", ".otf": "opentype"}

#: 各主题的系统中文字体栈。全部是 macOS / Windows / 常见 Linux 上能直接命中的字体名。
_SANS = (
    '"PingFang SC", "Hiragino Sans GB", "Source Han Sans SC", '
    '"Noto Sans CJK SC", "Microsoft YaHei", "Heiti SC", sans-serif'
)
_SERIF = (
    '"Songti SC", "STSong", "Source Han Serif SC", "Noto Serif CJK SC", '
    '"SimSun", "PingFang SC", serif'
)
_KAI = '"Kaiti SC", "STKaiti", "KaiTi", "Songti SC", "PingFang SC", serif'


@dataclass(frozen=True)
class CardTheme:
    """一套卡片主题：模板目录 + 配色 + 字体栈。"""

    name: str
    label: str
    font_stack: str
    palette: dict[str, str]
    #: 封面默认小标（模板里的 kicker / 胶带文字），调用方没给时用它
    default_kicker: str = "NOTES"
    #: 标题字号整体缩放，用于抵消不同字体的视觉大小差异
    headline_scale: float = 1.0

    @property
    def dir(self) -> Path:
        return XHS_TEMPLATES_DIR / self.name


THEMES: dict[str, CardTheme] = {
    "editorial": CardTheme(
        name="editorial",
        label="杂志风",
        font_stack=_SERIF,
        default_kicker="FIELD NOTES",
        palette={
            "bg": "#14161b",
            "fg": "#f4f1ea",
            "accent": "#e3a008",
            "muted": "#a9a49a",
            "surface": "#14161b",
            "line": "#2b2e35",
            "highlight": "#e3a00833",
        },
    ),
    "swiss": CardTheme(
        name="swiss",
        label="网格风",
        font_stack=_SANS,
        default_kicker="NOTES",
        headline_scale=0.96,
        palette={
            "bg": "#ffffff",
            "fg": "#111111",
            "accent": "#e2251b",
            "muted": "#6b6b6b",
            "surface": "#f2f2f0",
            "line": "#e3e3e0",
            "highlight": "#e2251b22",
        },
    ),
    "warm": CardTheme(
        name="warm",
        label="手账风",
        font_stack=_KAI,
        default_kicker="手帐",
        headline_scale=0.94,
        palette={
            "bg": "#efe6d6",
            "fg": "#3b3229",
            "accent": "#c9705a",
            "muted": "#8a7c6b",
            "surface": "#fbf7ef",
            "line": "#ddd0bb",
            "highlight": "#f2d9a733",
        },
    ),
}

DEFAULT_THEME = "editorial"


def available_themes() -> list[str]:
    return sorted(THEMES)


def get_theme(name: str | None) -> CardTheme:
    """按名字取主题。未知主题**报错**而不是静默回退——静默回退会让运营
    以为换了风格其实没换，等发出去才发现。"""
    key = (name or DEFAULT_THEME).strip()
    if key not in THEMES:
        raise ValueError(f"未知卡片主题 {key!r}，可用：{', '.join(available_themes())}")
    return THEMES[key]


# --------------------------------------------------------------------- 字体


@lru_cache(maxsize=4)
def _font_faces(font_dir: str) -> tuple[str, str]:
    """把目录里的字体读成 ``@font-face`` CSS，返回 ``(css, 字体族前缀)``。

    用 ``data:`` URI 内嵌而不是 ``file://``：``page.set_content`` 的文档源是
    ``about:blank``，从这个源去取 ``file://`` 子资源会被 chromium 拦掉。
    """
    directory = Path(font_dir)
    if not directory.is_dir():
        return "", ""
    files = sorted(p for p in directory.iterdir() if p.suffix.lower() in _FONT_FORMATS)
    if not files:
        return "", ""
    total = sum(p.stat().st_size for p in files)
    if total > MAX_EMBED_FONT_BYTES:
        logger.warning(
            "本地字体目录 %s 共 %.1fMB，超过 %dMB 上限，已忽略并退回系统字体栈",
            directory,
            total / 1024 / 1024,
            MAX_EMBED_FONT_BYTES // 1024 // 1024,
        )
        return "", ""

    blocks: list[str] = []
    families: list[str] = []
    for path in files:
        fmt = _FONT_FORMATS[path.suffix.lower()]
        family = path.stem
        payload = base64.b64encode(path.read_bytes()).decode("ascii")
        blocks.append(
            f"@font-face{{font-family:'{family}';"
            f"src:url(data:font/{fmt};base64,{payload}) format('{fmt}');"
            "font-display:block;}"
        )
        families.append(f'"{family}"')
    logger.info("已内嵌 %d 个本地字体（%.1fKB）", len(files), total / 1024)
    return "\n".join(blocks), ", ".join(families)


def font_dir() -> Path:
    """本地字体目录：环境变量优先。"""
    override = os.environ.get(FONT_DIR_ENV)
    return Path(override) if override else DEFAULT_FONT_DIR


def clear_font_cache() -> None:
    """测试里换了字体目录之后调用。"""
    _font_faces.cache_clear()


# ------------------------------------------------------------------- 字号


def _cover_headline_size(text: str, theme: CardTheme) -> int:
    """封面主文案字号：字越多越小，保证 4 行内放得下。"""
    length = max(len(text), 1)
    if length <= 6:
        size = 154
    elif length <= 9:
        size = 128
    elif length <= 12:
        size = 108
    elif length <= 16:
        size = 92
    else:
        size = 78
    return max(56, int(size * theme.headline_scale))


def _page_headline_size(text: str, theme: CardTheme) -> int:
    length = max(len(text), 1)
    if length <= 6:
        size = 90
    elif length <= 10:
        size = 78
    elif length <= 14:
        size = 68
    else:
        size = 58
    return max(44, int(size * theme.headline_scale))


def _bullet_size(bullets: list[str], theme: CardTheme) -> int:
    """要点字号：条数多或单条长就缩小，避免撑出画面。"""
    if not bullets:
        return int(46 * theme.headline_scale)
    longest = max(len(b) for b in bullets)
    size = 46
    if longest > 18 or len(bullets) >= 4:
        size = 42
    if longest > 24 or len(bullets) >= 5:
        size = 38
    return max(30, int(size * theme.headline_scale))


# ------------------------------------------------------------------- 渲染


@dataclass(frozen=True)
class RenderedCard:
    """一张卡片的渲染输入。``index`` 为 0 表示封面。"""

    index: int
    filename: str
    html: str
    is_cover: bool = False


@dataclass
class CardOptions:
    """卡片渲染的可调项。"""

    theme: str = DEFAULT_THEME
    #: 印在每张卡片角上的账号水印
    watermark: str = ""
    #: 封面小标；留空用主题默认值
    kicker: str = ""
    #: 封面副标题；留空用笔记标题
    subline: str = ""
    stem: str = "card"
    #: 本地字体目录覆盖（测试用）
    fonts_dir: Path | None = None
    extra: dict[str, str] = field(default_factory=dict)


def _read_template(theme: CardTheme, kind: str) -> str:
    path = theme.dir / f"{kind}.html"
    if not path.is_file():  # pragma: no cover - 打包缺文件才触发
        raise FileNotFoundError(f"卡片模板缺失: {path}")
    return path.read_text(encoding="utf-8")


def _fill(template: str, replacements: dict[str, str]) -> str:
    for token, value in replacements.items():
        template = template.replace(token, value)
    return template


def _base_replacements(theme: CardTheme, options: CardOptions) -> dict[str, str]:
    face_css, families = _font_faces(str(options.fonts_dir or font_dir()))
    stack = f"{families}, {theme.font_stack}" if families else theme.font_stack
    palette = theme.palette
    return {
        "__WIDTH__": str(CARD_WIDTH),
        "__HEIGHT__": str(CARD_HEIGHT),
        "__FONT_FACE__": face_css,
        "__FONT_STACK__": stack,
        "__BG__": palette["bg"],
        "__FG__": palette["fg"],
        "__ACCENT__": palette["accent"],
        "__MUTED__": palette["muted"],
        "__SURFACE__": palette["surface"],
        "__LINE__": palette["line"],
        "__HIGHLIGHT__": palette["highlight"],
        "__WATERMARK__": html_escape.escape(options.watermark),
    }


def _page_no(index: int, total: int) -> str:
    return f"{index + 1:02d} / {total:02d}"


def _bullets_html(bullets: list[str]) -> str:
    """要点列表。文案必须转义——正文来自 LLM，出现 ``<`` 会破坏模板结构。"""
    return "".join(
        f'<li class="bullet"><i class="marker"></i><span>{html_escape.escape(b)}</span></li>'
        for b in bullets
        if b.strip()
    )


def build_cover_html(
    headline: str,
    *,
    theme: CardTheme,
    options: CardOptions,
    total: int = 1,
) -> str:
    """渲染封面 HTML。纯字符串替换，不依赖 Playwright。"""
    replacements = _base_replacements(theme, options)
    replacements.update(
        {
            "__KICKER__": html_escape.escape(options.kicker or theme.default_kicker),
            "__HEADLINE__": html_escape.escape(headline),
            "__SUBLINE__": html_escape.escape(options.subline),
            "__HEADLINE_SIZE__": str(_cover_headline_size(headline, theme)),
            "__PAGE_NO__": _page_no(0, total),
            "__INDEX__": "01",
        }
    )
    return _fill(_read_template(theme, "cover"), replacements)


def build_page_html(
    page: PageSpec,
    *,
    index: int,
    theme: CardTheme,
    options: CardOptions,
    total: int = 1,
) -> str:
    """渲染内页 HTML。``index`` 从 1 开始（0 是封面）。"""
    replacements = _base_replacements(theme, options)
    replacements.update(
        {
            "__KICKER__": html_escape.escape(options.kicker or theme.default_kicker),
            "__INDEX__": f"{index:02d}",
            "__HEADLINE__": html_escape.escape(page.headline),
            "__HEADLINE_SIZE__": str(_page_headline_size(page.headline, theme)),
            "__BULLETS__": _bullets_html(page.bullets),
            "__BULLET_SIZE__": str(_bullet_size(page.bullets, theme)),
            "__FOOTNOTE__": html_escape.escape(page.footnote),
            "__PAGE_NO__": _page_no(index, total),
        }
    )
    return _fill(_read_template(theme, "page"), replacements)


def build_deck(draft: XhsNoteDraft, *, options: CardOptions | None = None) -> list[RenderedCard]:
    """把文案草稿变成一整套卡片 HTML（封面在前）。不截图，纯函数。"""
    opts = options or CardOptions()
    theme = get_theme(opts.theme)
    if not opts.subline:
        opts = replace(opts, subline=draft.title)
    total = draft.page_count

    deck = [
        RenderedCard(
            index=0,
            filename=f"{opts.stem}-00-cover.png",
            html=build_cover_html(
                draft.cover_headline or draft.title, theme=theme, options=opts, total=total
            ),
            is_cover=True,
        )
    ]
    for offset, page in enumerate(draft.pages, start=1):
        deck.append(
            RenderedCard(
                index=offset,
                filename=f"{opts.stem}-{offset:02d}.png",
                html=build_page_html(page, index=offset, theme=theme, options=opts, total=total),
            )
        )
    return deck


def render_cards(
    draft: XhsNoteDraft,
    output_dir: str | Path,
    *,
    theme: str = DEFAULT_THEME,
    watermark: str = "",
    kicker: str = "",
    subline: str = "",
    stem: str = "card",
    fonts_dir: Path | None = None,
    screenshotter: Any | None = None,
) -> list[Path]:
    """把一条笔记渲染成 3:4 PNG 卡片，返回**按顺序**的文件路径（封面在前）。

    没有 Playwright / chromium 时抛 :class:`~generation.cover.ScreenshotUnavailable`，
    错误信息里带安装命令。``screenshotter`` 供测试注入，签名与
    ``generation.cover.render_cover`` 一致：``(html, path, width, height) -> None``。
    """
    options = CardOptions(
        theme=theme,
        watermark=watermark,
        kicker=kicker,
        subline=subline,
        stem=stem,
        fonts_dir=fonts_dir,
    )
    deck = build_deck(draft, options=options)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = [out / card.filename for card in deck]

    if screenshotter is not None:
        for card, path in zip(deck, paths, strict=True):
            screenshotter(card.html, path, CARD_WIDTH, CARD_HEIGHT)
    else:
        # 一次启动浏览器截完整套：一条笔记 4–9 张，逐张启动会慢十几秒
        screenshot_batch(
            [
                ScreenshotJob(html=card.html, path=path, width=CARD_WIDTH, height=CARD_HEIGHT)
                for card, path in zip(deck, paths, strict=True)
            ]
        )

    logger.info("卡片渲染完成 theme=%s 共 %d 张 → %s", theme, len(paths), out)
    return paths


__all__ = [
    "CARD_HEIGHT",
    "CARD_SIZE",
    "CARD_WIDTH",
    "DEFAULT_FONT_DIR",
    "DEFAULT_THEME",
    "FONT_DIR_ENV",
    "MAX_EMBED_FONT_BYTES",
    "THEMES",
    "XHS_TEMPLATES_DIR",
    "CardOptions",
    "CardTheme",
    "RenderedCard",
    "ScreenshotUnavailable",
    "available_themes",
    "build_cover_html",
    "build_deck",
    "build_page_html",
    "clear_font_cache",
    "font_dir",
    "get_theme",
    "render_cards",
]
