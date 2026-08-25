"""内容生成管线：选题 → ContentBundle。

- ``generate_wechat_bundle(topic, account)``：公众号长文 + 封面。
- ``generate_xhs_bundle(topic, account)``：小红书图文笔记 + 3:4 卡片。

产出的 bundle 满足 P0 冻结契约：公众号是 ``platform="wechat_mp"`` + ``body_html``
+ ``platform_extra`` 含 ``title/digest/author``；小红书是 ``platform="xhs"``
+ ``media`` 为封面与内页 PNG + ``platform_extra`` 含 ``tags/schedule_at/is_original``。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import prompts
from core.budget import BudgetGuard
from core.models import Account, Topic, new_id
from generation.cover import ScreenshotUnavailable, render_cover_set
from generation.imagegen import (
    ASPECT_LANDSCAPE_3_2,
    ASPECT_PORTRAIT_3_4,
    GeneratedImage,
    generate_batch,
    illustrator,
    plan_illustrations,
)
from generation.llm import SupportsLLM, Usage
from generation.wechat_article import ArticleDraft, generate_article
from generation.wechat_render import NodeNotAvailable, RenderError, render_markdown
from generation.xhs_cards import CARD_SIZE, DEFAULT_THEME, render_cards
from generation.xhs_note import DEFAULT_TARGET_BODY_CHARS, XhsNoteDraft, generate_xhs_note
from publishers.base import ContentBundle, MediaAsset
from review.inspect import MAX_XHS_IMAGE_EDGE
from sourcing.base import RawTopic

logger = logging.getLogger("social_workflow.generation.pipeline")

PLATFORM = "wechat_mp"
XHS_PLATFORM = "xhs"
#: 生成物落盘根目录。封面等媒体文件放这里，路径写进 MediaAsset.path
DEFAULT_MEDIA_ROOT = Path("data/media")

#: 小红书默认配几张生图（P11）。文字卡讲逻辑、配图给质感，2 张是"看得出用心"
#: 又不至于把笔记撑成图册的量。0 = 只出文字卡
DEFAULT_XHS_ILLUSTRATIONS = 2
#: 公众号题图只用一张生图当底
DEFAULT_WECHAT_ILLUSTRATIONS = 1

#: **生成侧**的小红书配图比例闸门，比 :data:`review.inspect.XHS_ASPECT_RANGE`
#: 那个 (0.74, 1.34) 紧得多，因为两者回答的不是同一个问题：
#:
#: - review 侧问的是"平台会不会裁"，是审核语义，对**别处来的**图也要成立，放宽是对的；
#: - 这里问的是"我们自己出的 3:4 竖版笔记该不该配这张图"。笔记本体是
#:   :data:`generation.xhs_cards.CARD_SIZE` 的 0.75 竖版卡，配一张 1.000 的方图
#:   平台一句话都不会说（它在容忍区间内），出来的就是一条竖版笔记里混着方图。
#:
#: 区间取 3:4 上下约一档：0.750 正命中（画幅指令生效时的实测值），
#: 0.800 这种小偏差也放过（裁掉 6% 的宽不值得启一次浏览器），
#: 1.000 / 1.250 这些"模型没听指令"的产物则老实裁成 3:4。
XHS_ILLUSTRATION_ASPECT_RANGE = (0.70, 0.82)


def normalize_illustration(
    image: GeneratedImage,
    *,
    target: tuple[int, int],
    aspect_range: tuple[float, float],
    max_edge: int,
    screenshotter: Any | None,
    warnings: list[str],
) -> Path:
    """比例/尺寸不合规就居中裁切成 ``target``，否则原样用。返回最终落盘路径。

    **量了才知道要不要裁**：网关根本不认请求里的 size，画幅是模型自己挑的
    （见 :mod:`generation.imagegen` 的模块 docstring），所以这里读的是
    :attr:`GeneratedImage.width` / ``height`` 这两个从 PNG IHDR 量出来的真值。

    ``aspect_range`` 是**调用方**的比例主张，不一定等于平台的容忍区间——小红书那条
    传的是 :data:`XHS_ILLUSTRATION_ASPECT_RANGE`，比审核侧更紧，理由见那里。
    落在区间内就直接放过，省掉一次浏览器启动；画幅指令生效时走的正是这条快路径。
    """
    from generation.imagegen import fit_to_canvas

    if not image.measured:
        warnings.append(f"量不出配图尺寸（{image.path.name}），按原样使用，人工确认一下比例")
        return image.path

    ratio = image.aspect or 0.0
    low, high = aspect_range
    too_big = max(image.width or 0, image.height or 0) > max_edge
    if low <= ratio <= high and not too_big:
        return image.path

    why = f"{image.size_text} 超过长边上限" if too_big else f"{image.size_text}（比例 {ratio:.3f}）"
    fitted = fit_to_canvas(
        image.path,
        image.path.with_name(f"{image.path.stem}-fit.png"),
        target[0],
        target[1],
        screenshotter=screenshotter,
    )
    if fitted is None:
        warnings.append(f"配图 {why}，但没能裁切（缺 Playwright），按原样使用")
        return image.path
    warnings.append(f"配图 {why} 不合平台比例，已居中裁切成 {target[0]}×{target[1]}")
    return fitted


@dataclass
class GenerationOptions:
    """生成管线开关。"""

    target_words: int = 1500
    theme: str | None = None
    #: 关掉可跳过封面（无 Playwright 环境 / 测试）
    make_cover: bool = True
    #: 关掉则 body_html 留空，由后续步骤补（inspect 会因此报 block）
    render_html: bool = True
    media_root: Path = DEFAULT_MEDIA_ROOT
    #: 强制/禁止第五步去 AI 味改写，None = 按自评决定
    force_rewrite: bool | None = None
    #: 题图底图用几张生图（P11）。只用第一张，> 1 没有意义；0 = 老的纯色版式
    illustrations: int = DEFAULT_WECHAT_ILLUSTRATIONS
    #: 注入测试替身
    screenshotter: Any | None = None
    render_runner: Any | None = None
    #: 注入生图客户端（测试替身）。留空则按配置构造，配置不全时静默降级
    imagegen: Any | None = None


@dataclass
class GenerationOutcome:
    """比 ContentBundle 更完整的产物，供审计与日志。"""

    bundle: ContentBundle
    draft: ArticleDraft
    usage: Usage
    warnings: list[str] = field(default_factory=list)
    cover_paths: dict[str, Path] = field(default_factory=dict)
    #: 题图底图（生图产物）。``None`` = 这次走的是纯色版式
    hero_image: GeneratedImage | None = None


def _account_persona(account: Account | None, account_id: str) -> str:
    """人设优先级：Account.extra['persona'] > prompts/accounts/<id>/persona.md。

    放进 ``extra`` 是为了让运营能在 UI 上临时改人设而不必改文件。
    """
    if account is not None:
        inline = (account.extra or {}).get("persona")
        if isinstance(inline, str) and inline.strip():
            return inline.strip()
    return prompts.load_persona(account_id)


def _topic_fields(topic: Topic | RawTopic | str) -> tuple[str, str, str, str]:
    """归一化选题输入 → ``(title, source, url, context)``。"""
    if isinstance(topic, str):
        return topic, "", "", ""
    raw = dict(getattr(topic, "raw", {}) or {})
    context_bits = [
        f"{key}: {value}"
        for key, value in (
            ("热度", raw.get("info") or raw.get("hot_value")),
            ("榜单", raw.get("board")),
            ("名次", raw.get("rank")),
            ("角度建议", raw.get("angle")),
        )
        if value not in (None, "")
    ]
    return (
        topic.title,
        topic.source or "",
        topic.url or "",
        "；".join(context_bits),
    )


def generate_wechat_bundle(
    topic: Topic | RawTopic | str,
    account: Account | None = None,
    *,
    llm: SupportsLLM,
    account_id: str | None = None,
    author: str | None = None,
    options: GenerationOptions | None = None,
    content_id: str | None = None,
    budget: BudgetGuard | None = None,
) -> GenerationOutcome:
    """跑完生成链，产出可入库的 :class:`~publishers.base.ContentBundle`。

    ``account`` 可以为 ``None``（此时必须给 ``account_id``），便于纯离线测试。

    题图（P11）：生图产出的照片作为封面模板的**底图图层**，标题仍由模板排版，
    最终截图尺寸精确为 900×383 / 900×900。生图不可用时回落纯色版式，只记 warning。
    """
    opts = options or GenerationOptions()
    resolved_account_id = account_id or (account.id if account else None)
    if not resolved_account_id:
        raise ValueError("必须提供 account 或 account_id")

    title_hint, source, url, context = _topic_fields(topic)
    persona = _account_persona(account, resolved_account_id)
    item_id = content_id or new_id("itm")
    warnings: list[str] = []

    # 不做封面就没必要生图：底图无处可用
    want_images = plan_illustrations(
        opts.illustrations if opts.make_cover else 0,
        injected=opts.imagegen,
        warnings=warnings,
    )

    # -- 1 文案链 -----------------------------------------------------------
    draft = generate_article(
        llm,
        topic_title=title_hint,
        persona=persona,
        topic_source=source,
        topic_url=url,
        topic_context=context,
        target_words=opts.target_words,
        force_rewrite=opts.force_rewrite,
        image_prompt_count=want_images,
    )

    # -- 2 渲染内联样式 HTML ------------------------------------------------
    body_html: str | None = None
    if opts.render_html:
        try:
            body_html = render_markdown(
                draft.body_markdown, theme=opts.theme, runner=opts.render_runner
            ).html
        except NodeNotAvailable as exc:
            # 不抛：让 bundle 仍然入库进人工队列，inspect 会把缺 body_html 标成 block
            warnings.append(f"未渲染 HTML（无 Node）：{exc}")
            logger.warning("跳过 HTML 渲染：%s", exc)
        except RenderError as exc:
            warnings.append(f"HTML 渲染失败：{exc}")
            logger.warning("HTML 渲染失败：%s", exc)
    else:
        warnings.append("按配置跳过 HTML 渲染")

    # -- 3 题图底图（P11）+ 封面 --------------------------------------------
    cover_dir = Path(opts.media_root) / item_id
    hero: GeneratedImage | None = None
    if want_images and not draft.image_prompts:
        warnings.append("模型没给配图 prompt，封面回落纯色版式")
    elif want_images:
        with illustrator(opts.imagegen, budget=budget) as client:
            produced = generate_batch(
                client,
                draft.image_prompts[:1],
                cover_dir,
                # 题图要喂 900×383（2.35:1）与 900×900（1:1）两张封面。光请求横版没用
                # ——网关不认 size，实测请求 1536x1024 照样返方图。得把横版写进 prompt
                aspect=ASPECT_LANDSCAPE_3_2,
                purpose="wechat.hero",
                account_id=resolved_account_id,
                platform=PLATFORM,
                stem="hero",
                warnings=warnings,
            )
        hero = produced[0] if produced else None
        if hero is None:
            warnings.append("题图没生成出来，封面回落纯色版式")

    cover_paths: dict[str, Path] = {}
    media: list[MediaAsset] = []
    if opts.make_cover:
        cover_paths = render_cover_set(
            draft.meta.cover_title or draft.title,
            cover_dir,
            kicker=source or "HOT TOPIC",
            footer=(account.name if account else resolved_account_id),
            screenshotter=opts.screenshotter,
            # 底图交给模板做 background-size: cover，所以生图返回什么尺寸都不影响
            # 最终的 900×383 / 900×900——这就是"不信任 size 参数"的落点
            background=str(hero.path) if hero is not None else "",
        )
        if not cover_paths:
            warnings.append("未生成封面（缺 Playwright 或浏览器），列表页会用默认图")
        for index, (_size, path) in enumerate(sorted(cover_paths.items())):
            media.append(
                MediaAsset(
                    path=str(path),
                    kind="image",
                    # banner 排序在 square 前面，作为 cover
                    cover=(index == 0),
                )
            )

    # -- 4 组装 bundle ------------------------------------------------------
    platform_extra: dict[str, Any] = {
        "title": draft.title,
        "digest": draft.digest,
        "author": author or (account.extra or {}).get("author") if account else author,
        **draft.as_platform_extra(),
        # 题图审计：prompt、模型、请求尺寸与**实测**尺寸都留痕
        "illustrations": [{**hero.as_meta(), "role": "hero"}] if hero is not None else [],
        "topic_source": source,
        "topic_url": url,
        "generated_by": "generation.pipeline.generate_wechat_bundle",
    }
    # author 可能被上面的表达式算成 None，统一成空串，避免 inspect 报类型问题
    platform_extra["author"] = str(platform_extra.get("author") or "")

    bundle = ContentBundle(
        id=item_id,
        account_id=resolved_account_id,
        platform=PLATFORM,
        title=draft.title,
        body_markdown=draft.body_markdown,
        body_html=body_html,
        media=media,
        tags=list(draft.meta.keywords),
        platform_extra=platform_extra,
    )

    logger.info(
        "生成完成 item=%s 标题=%r 正文 %d 字 HTML=%s 封面=%d 张 题图=%s tokens=%d",
        item_id,
        draft.title,
        len(draft.body_markdown),
        "有" if body_html else "无",
        len(cover_paths),
        hero.size_text if hero is not None else "纯色",
        draft.usage.billable,
    )
    return GenerationOutcome(
        bundle=bundle,
        draft=draft,
        usage=draft.usage,
        warnings=warnings,
        cover_paths=cover_paths,
        hero_image=hero,
    )


# ============================================================== 小红书图文


@dataclass
class XhsGenerationOptions:
    """小红书生成管线开关。"""

    #: 卡片主题，见 generation.xhs_cards.THEMES
    theme: str = DEFAULT_THEME
    #: 关掉可跳过卡片渲染（无 Playwright 环境 / 只想看文案）
    make_cards: bool = True
    media_root: Path = DEFAULT_MEDIA_ROOT
    #: 印在卡片角上的水印，留空取账号名
    watermark: str | None = None
    #: 封面小标，留空用主题默认值
    kicker: str = ""
    target_body_chars: int = DEFAULT_TARGET_BODY_CHARS
    #: True = 初检通过也整包修订；False/None = 不额外强制（失败闸门始终生效）
    force_rewrite: bool | None = None
    #: 平台侧定时发布（ISO 8601 字符串）。发布器负责校验 1h–14d 的窗口
    schedule_at: str | None = None
    #: 是否声明原创
    is_original: bool = True
    #: 生图配图张数（P11）。0 = 只出文字卡，不调生图模型也不让 LLM 写配图 prompt
    illustrations: int = DEFAULT_XHS_ILLUSTRATIONS
    #: 注入测试替身，签名 (html, path, width, height) -> None
    screenshotter: Any | None = None
    #: 注入生图客户端（测试替身）。留空则按配置构造，配置不全时静默降级为不配图
    imagegen: Any | None = None


@dataclass
class XhsGenerationOutcome:
    """比 ContentBundle 更完整的产物，供审计与日志。"""

    bundle: ContentBundle
    draft: XhsNoteDraft
    usage: Usage
    warnings: list[str] = field(default_factory=list)
    card_paths: list[Path] = field(default_factory=list)
    #: 生图配图落盘路径（已按平台比例裁切过），顺序与 media 里一致
    illustration_paths: list[Path] = field(default_factory=list)


def generate_xhs_bundle(
    topic: Topic | RawTopic | str,
    account: Account | None = None,
    *,
    llm: SupportsLLM,
    account_id: str | None = None,
    suggested_angle: str = "",
    options: XhsGenerationOptions | None = None,
    content_id: str | None = None,
    budget: BudgetGuard | None = None,
) -> XhsGenerationOutcome:
    """跑完小红书生成链，产出可入库的 :class:`~publishers.base.ContentBundle`。

    卡片渲染不可用（没装 Playwright / chromium）时**不抛异常**：记 warning，
    产出一个没有 media 的 bundle 照常入库。``review.inspect`` 会以
    ``xhs.image.missing`` block 掉它，人在审核页看到的是"缺图"而不是一个 500。

    生图配图（P11）同样是"能配就配、配不上就算了"：权限没开、当日生图预算用完、
    网关抖动都只记 warning，媒体里就只剩文字卡。``budget`` 用来记生图张数，
    留空则不记账（纯离线测试）。
    """
    opts = options or XhsGenerationOptions()
    resolved_account_id = account_id or (account.id if account else None)
    if not resolved_account_id:
        raise ValueError("必须提供 account 或 account_id")

    title_hint, source, url, context = _topic_fields(topic)
    persona = _account_persona(account, resolved_account_id)
    item_id = content_id or new_id("itm")
    warnings: list[str] = []
    item_dir = Path(opts.media_root) / item_id

    # 先问清楚这次能不能配图：配不上就连"让模型写配图 prompt"都省掉，不白花 token
    want_images = plan_illustrations(opts.illustrations, injected=opts.imagegen, warnings=warnings)

    # -- 1 文案链 -----------------------------------------------------------
    draft = generate_xhs_note(
        llm,
        topic_title=title_hint,
        persona=persona,
        topic_source=source,
        topic_url=url,
        topic_context=context,
        suggested_angle=suggested_angle,
        target_body_chars=opts.target_body_chars,
        force_rewrite=opts.force_rewrite,
        image_prompt_count=want_images,
    )
    warnings.extend(draft.warnings)

    # -- 2 卡片渲染 ---------------------------------------------------------
    card_paths: list[Path] = []
    if opts.make_cards:
        watermark = opts.watermark
        if watermark is None:
            watermark = f"@{account.name}" if account else f"@{resolved_account_id}"
        try:
            card_paths = render_cards(
                draft,
                item_dir,
                theme=opts.theme,
                watermark=watermark,
                kicker=opts.kicker or source,
                screenshotter=opts.screenshotter,
            )
        except ScreenshotUnavailable as exc:
            warnings.append(f"未渲染卡片：{exc}")
            logger.warning("跳过小红书卡片渲染：%s", exc)
    else:
        warnings.append("按配置跳过卡片渲染")

    # -- 3 生图配图（P11）---------------------------------------------------
    # 排在文字卡**之后**：封面必须是版式可控的标题卡，生图只当补充质感的内页。
    # 这一段任何失败都不往上抛，最坏结果就是"这条笔记只有文字卡"
    illustrations: list[GeneratedImage] = []
    illustration_paths: list[Path] = []
    if want_images and not draft.image_prompts:
        warnings.append("模型没给配图 prompt，这条只有文字卡")
    elif want_images:
        with illustrator(opts.imagegen, budget=budget) as client:
            illustrations = generate_batch(
                client,
                draft.image_prompts[:want_images],
                item_dir,
                # 笔记本体是 3:4 竖版卡，配图必须跟着竖。同样得靠 prompt 指令：
                # 只请求 size=1024x1536 时实测返过 1122×1402 甚至 1254×1254
                aspect=ASPECT_PORTRAIT_3_4,
                purpose="xhs.illustration",
                account_id=resolved_account_id,
                platform=XHS_PLATFORM,
                stem="illustration",
                warnings=warnings,
            )
        illustration_paths = [
            normalize_illustration(
                image,
                target=CARD_SIZE,
                aspect_range=XHS_ILLUSTRATION_ASPECT_RANGE,
                max_edge=MAX_XHS_IMAGE_EDGE,
                screenshotter=opts.screenshotter,
                warnings=warnings,
            )
            for image in illustrations
        ]

    media = [
        MediaAsset(path=str(path), kind="image", cover=(index == 0))
        for index, path in enumerate(card_paths)
    ]
    media.extend(
        # 配图一律 cover=False：封面是第一张文字卡，生图不抢这个位置
        MediaAsset(path=str(path), kind="image", cover=False)
        for path in illustration_paths
    )

    # -- 4 组装 bundle ------------------------------------------------------
    platform_extra: dict[str, Any] = {
        "title": draft.title,
        # tags 在 ContentBundle.tags 里已有一份；这里再放一份是给发布器用的——
        # 契约冻结后平台特有字段只能走 platform_extra，发布器不该依赖通用字段的语义
        "tags": list(draft.tags),
        "schedule_at": opts.schedule_at,
        "is_original": opts.is_original,
        "theme": opts.theme,
        # 生图审计：prompt、模型、请求尺寸与**实测**尺寸都留痕，出了问题能复现
        "illustrations": [
            {**image.as_meta(), "final_path": str(path)}
            for image, path in zip(illustrations, illustration_paths, strict=True)
        ],
        **draft.as_platform_extra(),
        "topic_source": source,
        "topic_url": url,
        "generated_by": "generation.pipeline.generate_xhs_bundle",
    }

    bundle = ContentBundle(
        id=item_id,
        account_id=resolved_account_id,
        platform=XHS_PLATFORM,
        title=draft.title,
        # draft.body 已在终检前完成标签拼接与长度分配，闸门后不得再改正文。
        body_markdown=draft.body,
        body_html=None,  # 小红书不需要 HTML：正文是纯文本，信息在图上
        media=media,
        tags=list(draft.tags),
        platform_extra=platform_extra,
    )

    logger.info(
        "小红书生成完成 item=%s 标题=%r 正文 %d 字 卡片 %d 张 配图 %d 张 标签 %d 个 tokens=%d",
        item_id,
        draft.title,
        len(bundle.body_markdown),
        len(card_paths),
        len(illustration_paths),
        len(draft.tags),
        draft.usage.billable,
    )
    return XhsGenerationOutcome(
        bundle=bundle,
        draft=draft,
        usage=draft.usage,
        warnings=warnings,
        card_paths=card_paths,
        illustration_paths=illustration_paths,
    )


__all__ = [
    "DEFAULT_MEDIA_ROOT",
    "DEFAULT_WECHAT_ILLUSTRATIONS",
    "DEFAULT_XHS_ILLUSTRATIONS",
    "PLATFORM",
    "XHS_ILLUSTRATION_ASPECT_RANGE",
    "XHS_PLATFORM",
    "GenerationOptions",
    "GenerationOutcome",
    "XhsGenerationOptions",
    "XhsGenerationOutcome",
    "generate_wechat_bundle",
    "generate_xhs_bundle",
    "normalize_illustration",
]
