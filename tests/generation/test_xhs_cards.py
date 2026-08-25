"""小红书卡片：模板渲染（HTML 快照断言）+ 有浏览器时的真实截图 smoke。"""

from __future__ import annotations

from pathlib import Path

import pytest

from generation import cover as cover_mod
from generation import xhs_cards
from generation.xhs_cards import (
    CARD_HEIGHT,
    CARD_WIDTH,
    THEMES,
    CardOptions,
    ScreenshotUnavailable,
    available_themes,
    build_cover_html,
    build_deck,
    build_page_html,
    get_theme,
    render_cards,
)
from generation.xhs_note import PageSpec, generate_xhs_note
from review.inspect import read_image_size
from tests.p2_helpers import card_screenshotter, xhs_llm


@pytest.fixture
def draft():
    return generate_xhs_note(xhs_llm(), topic_title="租房收纳", persona="独居号")


# ------------------------------------------------------------------ 主题


def test_three_themes_are_shipped() -> None:
    assert set(available_themes()) >= {"editorial", "swiss", "warm"}


def test_every_theme_has_both_templates() -> None:
    for theme in THEMES.values():
        assert (theme.dir / "cover.html").is_file(), f"{theme.name} 缺封面模板"
        assert (theme.dir / "page.html").is_file(), f"{theme.name} 缺内页模板"


def test_unknown_theme_raises_with_choices() -> None:
    with pytest.raises(ValueError, match="editorial"):
        get_theme("nope")


def test_theme_palettes_are_complete() -> None:
    needed = {"bg", "fg", "accent", "muted", "surface", "line", "highlight"}
    for theme in THEMES.values():
        assert needed <= set(theme.palette), f"{theme.name} 配色缺 {needed - set(theme.palette)}"


# ------------------------------------------------------------------ HTML


@pytest.mark.parametrize("theme_name", ["editorial", "swiss", "warm"])
def test_cover_html_is_selfcontained(theme_name: str) -> None:
    html = build_cover_html(
        "不打孔，多出一面墙",
        theme=get_theme(theme_name),
        options=CardOptions(theme=theme_name, watermark="@测试号", subline="副标题"),
    )
    assert f"{CARD_WIDTH}px" in html and f"{CARD_HEIGHT}px" in html
    # 不能有外链资源，否则截图时要联网
    assert "http://" not in html and "https://" not in html
    # 占位符必须全部替换掉
    assert "__" not in html.replace("-webkit-", ""), "还有未替换的 __TOKEN__"
    assert "@测试号" in html
    assert "不打孔，多出一面墙" in html


@pytest.mark.parametrize("theme_name", ["editorial", "swiss", "warm"])
def test_page_html_renders_bullets_and_page_number(theme_name: str) -> None:
    page = PageSpec(
        headline="门后是最被浪费的墙", bullets=["19 块挂钩", "承重 3kg"], footnote="注意承重"
    )
    html = build_page_html(
        page,
        index=2,
        theme=get_theme(theme_name),
        options=CardOptions(theme=theme_name, watermark="@测试号"),
        total=6,
    )
    assert "19 块挂钩" in html and "承重 3kg" in html
    assert html.count('class="bullet"') == 2
    assert "注意承重" in html
    assert "03 / 06" in html  # 页码从 1 开始展示，index=2 是第 3 张
    assert "__" not in html.replace("-webkit-", "")


def test_html_escapes_untrusted_text() -> None:
    """卡片文案来自 LLM，出现 < & " 会破坏模板结构。"""
    page = PageSpec(headline="标题 & <b>粗</b>", bullets=['要点 "引号" <i>'], footnote="<script>")
    html = build_page_html(page, index=1, theme=get_theme("swiss"), options=CardOptions(), total=2)
    assert "&amp;" in html and "&lt;b&gt;" in html
    assert "<b>粗</b>" not in html
    assert "<script>" not in html


def test_headline_font_shrinks_as_text_grows() -> None:
    theme = get_theme("editorial")
    short = build_cover_html("四个字", theme=theme, options=CardOptions())
    long = build_cover_html("这是一个非常长的封面文案标题", theme=theme, options=CardOptions())

    def size(html: str) -> int:
        marker = "font-size: "
        chunk = html.split(".headline {")[1]
        return int(chunk.split(marker)[1].split("px")[0])

    assert size(short) > size(long), "长标题必须自动缩小字号，否则会溢出"


def test_bullet_font_shrinks_with_more_and_longer_items() -> None:
    theme = get_theme("swiss")
    few = build_page_html(
        PageSpec(headline="h", bullets=["短"]), index=1, theme=theme, options=CardOptions()
    )
    many = build_page_html(
        PageSpec(
            headline="h", bullets=["很长的一条要点内容写到二十八个字上限左右" for _ in range(5)]
        ),
        index=1,
        theme=theme,
        options=CardOptions(),
    )

    def size(html: str) -> int:
        return int(html.split(".bullet {")[1].split("font-size: ")[1].split("px")[0])

    assert size(few) > size(many)


def test_build_deck_puts_cover_first(draft) -> None:
    deck = build_deck(draft, options=CardOptions(theme="warm", watermark="@x"))
    assert len(deck) == draft.page_count
    assert deck[0].is_cover and deck[0].filename.endswith("00-cover.png")
    assert not any(card.is_cover for card in deck[1:])
    # 每张都印了页码，人工审核时能确认顺序没错
    for index, card in enumerate(deck):
        assert f"{index + 1:02d} / {len(deck):02d}" in card.html


def test_build_deck_defaults_subline_to_title(draft) -> None:
    deck = build_deck(draft)
    assert draft.title in deck[0].html


# ------------------------------------------------------------------ 字体


def test_local_fonts_are_embedded_as_data_uri(tmp_path) -> None:
    """本地字体走 data: URI 内嵌，绝不外链。"""
    fonts = tmp_path / "fonts"
    fonts.mkdir()
    (fonts / "DemoFont.woff2").write_bytes(b"not-a-real-font")
    xhs_cards.clear_font_cache()
    try:
        html = build_cover_html(
            "标题", theme=get_theme("swiss"), options=CardOptions(fonts_dir=fonts)
        )
    finally:
        xhs_cards.clear_font_cache()
    assert "@font-face" in html
    assert "data:font/woff2;base64," in html
    assert '"DemoFont"' in html
    assert "http" not in html


def test_missing_font_dir_falls_back_to_system_stack(tmp_path) -> None:
    xhs_cards.clear_font_cache()
    html = build_cover_html(
        "标题", theme=get_theme("swiss"), options=CardOptions(fonts_dir=tmp_path / "nope")
    )
    assert "@font-face" not in html
    assert "PingFang SC" in html


def test_oversize_font_dir_is_ignored(tmp_path, monkeypatch) -> None:
    fonts = tmp_path / "fonts"
    fonts.mkdir()
    (fonts / "Huge.ttf").write_bytes(b"x" * 1024)
    monkeypatch.setattr(xhs_cards, "MAX_EMBED_FONT_BYTES", 10)
    xhs_cards.clear_font_cache()
    try:
        html = build_cover_html(
            "标题", theme=get_theme("editorial"), options=CardOptions(fonts_dir=fonts)
        )
    finally:
        xhs_cards.clear_font_cache()
    assert "@font-face" not in html


# ------------------------------------------------------------------ 渲染


def test_render_cards_with_injected_screenshotter(draft, tmp_path) -> None:
    written: list[tuple[Path, str]] = []
    paths = render_cards(
        draft,
        tmp_path,
        theme="editorial",
        watermark="@测试号",
        screenshotter=card_screenshotter(written),
    )
    assert len(paths) == draft.page_count
    assert all(path.is_file() for path in paths)
    assert paths[0].name.endswith("00-cover.png")
    # 每张都是 3:4
    for path in paths:
        assert read_image_size(path) == (CARD_WIDTH, CARD_HEIGHT)
    assert len(written) == len(paths)
    assert "@测试号" in written[0][1]


def test_render_cards_rejects_unknown_theme(draft, tmp_path) -> None:
    with pytest.raises(ValueError, match="未知卡片主题"):
        render_cards(draft, tmp_path, theme="nope", screenshotter=lambda *a: None)


def test_render_cards_raises_without_playwright(draft, tmp_path, monkeypatch) -> None:
    """卡片缺了这条笔记根本发不出去，所以这里必须抛错，不能像封面那样静默跳过。"""
    monkeypatch.setattr(cover_mod, "playwright_available", lambda: False)
    with pytest.raises(ScreenshotUnavailable, match="playwright install chromium"):
        render_cards(draft, tmp_path)


def test_cover_still_degrades_to_none_without_playwright(monkeypatch, tmp_path) -> None:
    """对照组：公众号封面缺了只是列表页显示默认图，不该抛。"""
    monkeypatch.setattr(cover_mod, "playwright_available", lambda: False)
    assert cover_mod.render_cover(cover_mod.CoverSpec(title="x"), tmp_path / "c.png") is None


# ------------------------------------------------------------------ 真实截图


def _chromium_ready() -> bool:
    if not cover_mod.playwright_available():
        return False
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            p.chromium.launch().close()
    except Exception:
        return False
    return True


@pytest.mark.render
@pytest.mark.parametrize("theme_name", ["editorial", "swiss", "warm"])
def test_real_screenshot_smoke(draft, tmp_path, theme_name: str) -> None:
    """装了 chromium 才跑：真的截一张，断言出图尺寸就是 1242×1656。"""
    if not _chromium_ready():
        pytest.skip("本机没有可用的 chromium（uv run playwright install chromium）")
    paths = render_cards(draft, tmp_path / theme_name, theme=theme_name, watermark="@测试号")
    assert len(paths) == draft.page_count
    for path in paths:
        assert path.is_file() and path.stat().st_size > 1024
        assert read_image_size(path) == (CARD_WIDTH, CARD_HEIGHT)
