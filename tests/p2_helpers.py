"""P2（小红书）测试公用工具。

和 ``tests/p1_helpers.py`` 一样单独成模块，避免多人同时改 ``conftest.py``。
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import Any

from generation.llm import ScriptedLLM
from generation.xhs_cards import CARD_HEIGHT, CARD_WIDTH
from generation.xhs_note import PageSpec, XhsCardPlan, XhsNoteCopy, XhsRevision, XhsSelfCheck

DEMO_BODY = (
    "搬进这间 30㎡ 之前，我以为收纳就是买柜子。买完发现房间更小了。\n\n"
    "真正管用的是三件事：门后挂钩、免钉洞洞板、带轮的床底箱。\n\n"
    "门后那面墙我空了半年才反应过来。19 块的挂钩承重 3kg，挂了包和外套，地上立刻空出一块。\n\n"
    "洞洞板 89 块买的 60×80cm，贴在书桌上方，两年没掉过。乳胶漆掉粉的墙别贴，我翻过车。\n\n"
    "床底箱一定要带轮。没轮的那个我第三次就懒得拉出来了。\n\n"
    "你们租的房子有哪面墙是完全空着的？"
)

DEMO_PAGES = [
    PageSpec(
        headline="门后是最被浪费的墙",
        bullets=["19 块的挂钩承重 3kg", "挂包和外套，地上空一块"],
        footnote="空心门先看承重标注",
    ),
    PageSpec(
        headline="免钉洞洞板比柜子便宜",
        bullets=["60×80cm 一块 89 元", "贴书桌上方，两年没掉"],
        footnote="掉粉的乳胶漆墙别贴",
    ),
    PageSpec(
        headline="床底箱一定要带轮",
        bullets=["没轮的第三次就懒得拉", "冬装在床底躺了一整年"],
        footnote="",
    ),
]


def xhs_llm(
    *,
    budget: Any = None,
    verdict: str = "pass",
    overall: int = 9,
    title: str = "租房不打孔，我多出一面墙",
    alt_titles: list[str] | None = None,
    body: str = DEMO_BODY,
    tags: list[str] | None = None,
    cover_headline: str = "不打孔，多出一面墙",
    pages: list[PageSpec] | None = None,
    blocking_issues: list[str] | None = None,
    image_prompts: list[str] | None = None,
    revision: XhsRevision | None = None,
    final_verdict: str = "pass",
    final_blocking_issues: list[str] | None = None,
) -> ScriptedLLM:
    """喂饱小红书有界文案链的假 LLM。

    修订与第二次 selfcheck 总是预置；初检通过且未强制修订时不会消费它们。
    """
    return ScriptedLLM(
        budget=budget,
        replies=["### 3 我的答案\n\n不打孔也能收纳。"],
        parsed_replies=[
            XhsCardPlan(
                cover_headline=cover_headline,
                pages=list(pages if pages is not None else DEMO_PAGES),
            ),
            XhsNoteCopy(
                title=title,
                alt_titles=list(
                    alt_titles if alt_titles is not None else ["30㎡ 收纳，三件东西就够"]
                ),
                body=body,
                tags=list(tags if tags is not None else ["租房", "小户型收纳", "独居", "免打孔"]),
                image_prompts=list(image_prompts or []),
            ),
            XhsSelfCheck(
                ai_flavor=overall,
                specificity=overall,
                hook=overall,
                card_fit=overall,
                tag_fit=overall,
                compliance_risk=9,
                overall=overall,
                verdict=verdict,  # type: ignore[arg-type]
                blocking_issues=list(blocking_issues or []),
            ),
            revision
            or XhsRevision(
                title=title,
                alt_titles=list(
                    alt_titles if alt_titles is not None else ["30㎡ 收纳，三件东西就够"]
                ),
                body=body,
                tags=list(tags if tags is not None else ["租房", "小户型收纳", "独居", "免打孔"]),
                cover_headline=cover_headline,
                pages=list(pages if pages is not None else DEMO_PAGES),
                image_prompts=list(image_prompts or []),
            ),
            XhsSelfCheck(
                ai_flavor=9,
                specificity=9,
                hook=9,
                card_fit=9,
                tag_fit=9,
                compliance_risk=9,
                overall=9,
                verdict=final_verdict,  # type: ignore[arg-type]
                blocking_issues=list(final_blocking_issues or []),
            ),
        ],
    )


def png_bytes(width: int, height: int) -> bytes:
    """造一张真实可解析的纯色 PNG（灰度 8bit），只为让尺寸校验有东西可量。"""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    raw = bytearray()
    for _ in range(height):
        raw.append(0)  # 每行 filter type = 0
        raw.extend(b"\xd0" * width)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )


def card_screenshotter(
    written: list[tuple[Path, str]],
    *,
    width: int = CARD_WIDTH,
    height: int = CARD_HEIGHT,
) -> Any:
    """假截图器：记下 HTML 并写一张**尺寸真实**的 PNG。

    尺寸真实很重要——``review.inspect`` 会去量图片长宽比与长边，
    写 1×1 占位图的话这条校验等于没测。
    """
    payload = png_bytes(width, height)

    def _shot(html: str, path: Path, w: int, h: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload if (w, h) == (width, height) else png_bytes(w, h))
        written.append((path, html))

    return _shot


__all__ = [
    "DEMO_BODY",
    "DEMO_PAGES",
    "card_screenshotter",
    "png_bytes",
    "xhs_llm",
]
