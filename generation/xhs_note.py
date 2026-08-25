"""小红书图文笔记文案链。

::

    切角度 → 卡片脚本(结构化) → 标题/正文/标签(结构化) → 初检(结构化)
    → [至多一次] 整包修订(结构化) → 终检(结构化)

和公众号链（``wechat_article``）同一套思路：每步只处理一件事，中间产物全部留在
:attr:`XhsNoteDraft.trace` 里可审计。差别在于**小红书是图先于文的平台**——
所以先定卡片脚本，正文是在"图上已经说了什么"的前提下补充，而不是先写长文再摘要成图。

平台硬限制（标题 20 字、正文 1000 字、标签数量、页数）在 :mod:`review.inspect`
里定义，本模块 import 过来做**兜底截断**：模型超字数是常态，不能只靠 prompt 约束。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

import prompts
from generation.imagegen import image_prompt_rules, normalize_image_prompts
from generation.llm import SupportsLLM, Usage
from generation.output_budget import budget_for
from generation.textutil import (
    accumulate_usage,
    clean_body,
    clean_tags,
    strip_code_fence,
    strip_zero_width,
)
from generation.textutil import truncate as _truncate
from review.inspect import (
    MAX_XHS_BODY_CHARS,
    MAX_XHS_TITLE_CHARS,
    XHS_PAGE_RANGE,
    XHS_TAG_RANGE,
)

logger = logging.getLogger("social_workflow.generation.xhs_note")

#: 正文目标字数（上限是 1000，写太满在手机上要点"展开"）
DEFAULT_TARGET_BODY_CHARS = 420
#: 兼容旧调用方的评分常量；质量闸门只认 verdict 与 blocking_issues，不能靠分数放行。
REWRITE_THRESHOLD = 8
#: 卡片文字上限。超了模板会截断显示，不如在这里先截干净。
MAX_COVER_HEADLINE_CHARS = 14
MAX_PAGE_HEADLINE_CHARS = 16
MAX_BULLET_CHARS = 28
MAX_FOOTNOTE_CHARS = 24
#: 单页要点条数区间
BULLET_RANGE = (2, 4)
#: 单个话题标签的字数上限（平台侧更宽，这里收紧防止模型写成一句话）
MAX_TAG_CHARS = 12

Verdict = Literal["pass", "revise", "reject"]


# --------------------------------------------------------------- 结构化输出


class PageSpec(BaseModel):
    """一张内页卡片的文字脚本。"""

    model_config = ConfigDict(extra="forbid")

    headline: str = Field(description="这一页的结论，不是标签")
    bullets: list[str] = Field(default_factory=list, description="把 headline 落到地上的动作/数字")
    footnote: str = Field(default="", description="提醒、例外或一句自嘲，可留空")


class XhsCardPlan(BaseModel):
    """封面文案 + 内页脚本。"""

    model_config = ConfigDict(extra="forbid")

    cover_headline: str
    pages: list[PageSpec] = Field(default_factory=list)


class XhsNoteCopy(BaseModel):
    """标题 / 正文 / 话题标签。"""

    model_config = ConfigDict(extra="forbid")

    title: str
    alt_titles: list[str] = Field(default_factory=list)
    body: str
    tags: list[str] = Field(default_factory=list)
    #: 生图模型用的英文 prompt，一条一张（P11）。不配图时是空数组。
    #: 顺手在写正文这一步产出，省一次往返——它要看的上下文和正文完全一样
    image_prompts: list[str] = Field(
        default_factory=list, description="英文生图 prompt，一条一张；不配图时留空数组"
    )


class XhsRevision(BaseModel):
    """初检失败后的整包修订；字段必须一次性保持互相一致。"""

    model_config = ConfigDict(extra="forbid")

    title: str
    alt_titles: list[str] = Field(default_factory=list)
    body: str
    tags: list[str] = Field(default_factory=list)
    cover_headline: str
    pages: list[PageSpec] = Field(default_factory=list)
    image_prompts: list[str] = Field(default_factory=list)


class XhsSelfCheck(BaseModel):
    """质量自评。维度比公众号多两条：卡片适配与合规风险。"""

    model_config = ConfigDict(extra="forbid")

    ai_flavor: int = Field(ge=0, le=10, description="分数越高越不像 AI 写的")
    specificity: int = Field(ge=0, le=10)
    hook: int = Field(ge=0, le=10)
    card_fit: int = Field(ge=0, le=10, description="卡片字量是否适合 3:4 竖图")
    tag_fit: int = Field(ge=0, le=10)
    compliance_risk: int = Field(ge=0, le=10, description="分数越高风险越低")
    overall: int = Field(ge=0, le=10)
    verdict: Verdict
    blocking_issues: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)

    @property
    def needs_rewrite(self) -> bool:
        return self.verdict != "pass" or bool(self.blocking_issues)


class XhsQualityError(RuntimeError):
    """整包修订后的终检仍不合格；下游不得渲染、生图或入库。"""

    def __init__(self, check: XhsSelfCheck) -> None:
        issues = "；".join(check.blocking_issues) or "终检未列出具体 blocking issue"
        super().__init__(
            f"终检 verdict={check.verdict}，blocking={len(check.blocking_issues)}：{issues}"
        )
        self.check = check


# ------------------------------------------------------------------- 产物


@dataclass
class XhsNoteDraft:
    """一条小红书图文笔记的完整文案产物。

    ``pages`` 只是**文字脚本**，渲染成 PNG 是 :mod:`generation.xhs_cards` 的事。
    """

    title: str
    #: 唯一发布正文：已完成标签拼接与 1000 字分配，终检和 ContentBundle 共用此值
    body: str
    tags: list[str]
    cover_headline: str
    pages: list[PageSpec]
    alt_titles: list[str] = field(default_factory=list)
    #: 生图配图的英文 prompt（P11）。空数组 = 这条不配图
    image_prompts: list[str] = field(default_factory=list)
    #: selector 给出的原始切入角度；手工选题为空
    suggested_angle: str = ""
    selfcheck: XhsSelfCheck | None = None
    #: 各步中间产物，供审计与复盘
    trace: dict[str, str] = field(default_factory=dict)
    usage: Usage = field(default_factory=Usage)
    rewritten: bool = False
    #: 兜底截断 / 数量修正留下的痕迹，会汇进 GenerationOutcome.warnings
    warnings: list[str] = field(default_factory=list)

    @property
    def page_count(self) -> int:
        """卡片总数 = 封面 1 张 + 内页 N 张。"""
        return len(self.pages) + 1

    def body_with_tags(self) -> str:
        """返回已经过终检的 canonical publish body。

        标签拼在正文末尾而不是只放 ``platform_extra``：小红书的话题是**正文的一部分**，
        发布时跟着正文一起提交，审核也必须扫到它们（堆无关热词同样会被判违规）。
        长度分配和拼接在初检/终检之前已经完成；这里保留方法名兼容旧调用方，绝不再
        截断或修改内容。
        """
        return self.body

    def as_platform_extra(self) -> dict[str, Any]:
        """写进 ``ContentBundle.platform_extra`` 的生成侧字段。"""
        return {
            "cover_headline": self.cover_headline,
            "alt_titles": list(self.alt_titles),
            # 配图 prompt 也要留痕：出了问题要能回看"当时让模型画的是什么"
            "image_prompts": list(self.image_prompts),
            "suggested_angle": self.suggested_angle,
            "pages": [page.model_dump() for page in self.pages],
            "selfcheck": self.selfcheck.model_dump() if self.selfcheck else None,
            "rewritten": self.rewritten,
        }


# ------------------------------------------------------------------- 归一化


def normalize_tags(raw: list[str], *, limit: int = XHS_TAG_RANGE[1]) -> list[str]:
    """清洗话题标签。规则与抖音共用，实现在 :func:`generation.textutil.clean_tags`。"""
    return clean_tags(raw, limit=limit, max_chars=MAX_TAG_CHARS)


def normalize_pages(pages: list[PageSpec]) -> tuple[list[PageSpec], list[str]]:
    """把内页脚本收进模板放得下的范围，返回 ``(pages, warnings)``。"""
    warnings: list[str] = []
    min_pages, max_pages = XHS_PAGE_RANGE
    min_bullets, max_bullets = BULLET_RANGE

    normalized: list[PageSpec] = []
    for page in pages:
        headline = _truncate(strip_zero_width(page.headline), MAX_PAGE_HEADLINE_CHARS)
        if not headline:
            warnings.append("有一页没有 headline，已丢弃")
            continue
        bullets = [
            _truncate(strip_zero_width(b), MAX_BULLET_CHARS) for b in page.bullets if str(b).strip()
        ][:max_bullets]
        if len(bullets) < min_bullets:
            warnings.append(f"「{headline}」只有 {len(bullets)} 条要点，卡片会显得空")
        normalized.append(
            PageSpec(
                headline=headline,
                bullets=bullets,
                footnote=_truncate(strip_zero_width(page.footnote), MAX_FOOTNOTE_CHARS),
            )
        )

    if len(normalized) > max_pages:
        warnings.append(f"模型给了 {len(normalized)} 页，超过 {max_pages} 页上限，已截断")
        normalized = normalized[:max_pages]
    if len(normalized) < min_pages:
        # 不补空页：宁可少几张图交给人工判断，也不要发一张没内容的卡片出去
        warnings.append(f"只有 {len(normalized)} 页内页，低于建议下限 {min_pages} 页")
    return normalized, warnings


# ------------------------------------------------------------------- 生成链


def generate_xhs_note(
    llm: SupportsLLM,
    *,
    topic_title: str,
    persona: str,
    topic_source: str = "",
    topic_url: str = "",
    topic_context: str = "",
    suggested_angle: str = "",
    target_body_chars: int = DEFAULT_TARGET_BODY_CHARS,
    force_rewrite: bool | None = None,
    image_prompt_count: int = 0,
) -> XhsNoteDraft:
    """跑完有界文案链，产出 :class:`XhsNoteDraft`。

    初检不通过时一定做且只做一次整包修订，再以终检决定是否允许下游继续。
    ``force_rewrite=True`` 会让初检通过的内容也做整包修订；``None`` 与 ``False``
    都是不额外强制，但绝不能跳过失败内容的质量闸门。

    ``image_prompt_count > 0`` 时让模型在写正文那一步**顺手**产出同样多条英文生图
    prompt（P11）。不额外开一次调用：它要看的上下文和写正文完全一样，多一次往返
    只是多花钱多一处会失败的地方。
    """
    system = prompts.load("xhs/system", persona=persona or "（未提供人设）")
    total = Usage()
    trace: dict[str, str] = {}
    warnings: list[str] = []
    min_pages, max_pages = XHS_PAGE_RANGE
    min_tags, max_tags = XHS_TAG_RANGE
    min_bullets, max_bullets = BULLET_RANGE
    suggested_angle = strip_zero_width(suggested_angle).strip()
    trace["suggested_angle"] = suggested_angle

    # 1 切角度 -----------------------------------------------------------
    angle_result = llm.complete(
        prompts.load(
            "xhs/angle",
            topic_title=topic_title,
            topic_source=topic_source or "未标注",
            topic_url=topic_url or "无",
            topic_context=topic_context or "无额外信息，按公开常识处理",
            suggested_angle=suggested_angle or "（选题 Agent 未指定，按题目自行收敛）",
            page_hint=f"{min_pages}–{max_pages}",
        ),
        system=system,
        max_tokens=budget_for("xhs.angle"),
        purpose="xhs.angle",
    )
    angle = strip_code_fence(angle_result.text)
    trace["angle"] = angle
    total = accumulate_usage(total, angle_result.usage)

    # 2 卡片脚本 ---------------------------------------------------------
    plan_result = llm.parse(
        prompts.load(
            "xhs/cards",
            angle=angle,
            suggested_angle=suggested_angle or "（无）",
            max_cover=MAX_COVER_HEADLINE_CHARS,
            min_pages=min_pages,
            max_pages=max_pages,
            max_headline=MAX_PAGE_HEADLINE_CHARS,
            min_bullets=min_bullets,
            max_bullets=max_bullets,
            max_bullet=MAX_BULLET_CHARS,
            max_footnote=MAX_FOOTNOTE_CHARS,
        ),
        XhsCardPlan,
        system=system,
        max_tokens=budget_for("xhs.cards"),
        purpose="xhs.cards",
    )
    plan = plan_result.parsed
    total = accumulate_usage(total, plan_result.usage)

    pages, page_warnings = normalize_pages(plan.pages)
    warnings.extend(page_warnings)
    cover_headline = _truncate(strip_zero_width(plan.cover_headline), MAX_COVER_HEADLINE_CHARS)
    trace["cards"] = _render_cards_text(cover_headline, pages)

    # 3 标题 / 正文 / 标签 ------------------------------------------------
    copy_result = llm.parse(
        prompts.load(
            "xhs/note",
            angle=angle,
            suggested_angle=suggested_angle or "（无）",
            cards=trace["cards"],
            max_title=MAX_XHS_TITLE_CHARS,
            max_body=MAX_XHS_BODY_CHARS,
            target_body=target_body_chars,
            min_tags=min_tags,
            max_tags=max_tags,
            image_rules=image_prompt_rules(image_prompt_count),
        ),
        XhsNoteCopy,
        system=system,
        max_tokens=budget_for("xhs.note"),
        purpose="xhs.note",
    )
    copy = copy_result.parsed
    total = accumulate_usage(total, copy_result.usage)

    title, alt_titles, body, tags, image_prompts = _normalize_copy(
        title=copy.title,
        alt_titles=copy.alt_titles,
        body=copy.body,
        tags=copy.tags,
        image_prompts=copy.image_prompts,
        fallback_title=cover_headline or topic_title,
        min_tags=min_tags,
        max_tags=max_tags,
        image_prompt_count=image_prompt_count,
        warnings=warnings,
    )
    cover_headline = cover_headline or title
    trace["body"] = body

    # 4 初检 --------------------------------------------------------------
    selfcheck, check_usage = _run_selfcheck(
        llm,
        system=system,
        suggested_angle=suggested_angle,
        title=title,
        alt_titles=alt_titles,
        body=body,
        cards=trace["cards"],
        tags=tags,
        image_prompts=image_prompts,
        phase="初检",
    )
    total = accumulate_usage(total, check_usage)

    # 5 整包修订（有界：最多一次）-----------------------------------------
    rewritten = False
    should_rewrite = selfcheck.needs_rewrite or force_rewrite is True
    if should_rewrite:
        issues = selfcheck.blocking_issues + selfcheck.suggestions
        revision_result = llm.parse(
            prompts.load(
                "xhs/dehumanize",
                suggested_angle=suggested_angle or "（无）",
                title=title,
                alt_titles=_render_alt_titles(alt_titles),
                body=body,
                cards=trace["cards"],
                tags="、".join(tags) or "（无）",
                image_prompts=_render_image_prompts(image_prompts),
                issues="\n".join(f"- {issue}" for issue in issues)
                or "（运营强制整包优化，初检未列具体问题）",
                max_title=MAX_XHS_TITLE_CHARS,
                max_body=MAX_XHS_BODY_CHARS,
                max_cover=MAX_COVER_HEADLINE_CHARS,
                min_pages=min_pages,
                max_pages=max_pages,
                max_headline=MAX_PAGE_HEADLINE_CHARS,
                min_bullets=min_bullets,
                max_bullets=max_bullets,
                max_bullet=MAX_BULLET_CHARS,
                max_footnote=MAX_FOOTNOTE_CHARS,
                min_tags=min_tags,
                max_tags=max_tags,
                image_prompt_count=image_prompt_count,
            ),
            XhsRevision,
            system=system,
            max_tokens=budget_for("xhs.dehumanize"),
            purpose="xhs.dehumanize",
        )
        revision = revision_result.parsed
        total = accumulate_usage(total, revision_result.usage)

        pages, page_warnings = normalize_pages(revision.pages)
        warnings.extend(page_warnings)
        cover_headline = _truncate(
            strip_zero_width(revision.cover_headline), MAX_COVER_HEADLINE_CHARS
        )
        title, alt_titles, body, tags, image_prompts = _normalize_copy(
            title=revision.title,
            alt_titles=revision.alt_titles,
            body=revision.body,
            tags=revision.tags,
            image_prompts=revision.image_prompts,
            fallback_title=cover_headline or topic_title,
            min_tags=min_tags,
            max_tags=max_tags,
            image_prompt_count=image_prompt_count,
            warnings=warnings,
        )
        cover_headline = cover_headline or title
        trace["cards"] = _render_cards_text(cover_headline, pages)
        trace["body"] = body
        trace["dehumanized"] = body
        trace["revision"] = revision.model_dump_json()
        rewritten = True

        # 6 终检（有界：整条链最多两次 selfcheck）---------------------------
        selfcheck, check_usage = _run_selfcheck(
            llm,
            system=system,
            suggested_angle=suggested_angle,
            title=title,
            alt_titles=alt_titles,
            body=body,
            cards=trace["cards"],
            tags=tags,
            image_prompts=image_prompts,
            phase="终检",
        )
        total = accumulate_usage(total, check_usage)
        if selfcheck.needs_rewrite:
            raise XhsQualityError(selfcheck)

    return XhsNoteDraft(
        title=title,
        body=body,
        tags=tags,
        cover_headline=cover_headline or title,
        pages=pages,
        alt_titles=alt_titles,
        image_prompts=image_prompts,
        suggested_angle=suggested_angle,
        selfcheck=selfcheck,
        trace=trace,
        usage=total,
        rewritten=rewritten,
        warnings=warnings,
    )


def _normalize_copy(
    *,
    title: str,
    alt_titles: list[str],
    body: str,
    tags: list[str],
    image_prompts: list[str],
    fallback_title: str,
    min_tags: int,
    max_tags: int,
    image_prompt_count: int,
    warnings: list[str],
) -> tuple[str, list[str], str, list[str], list[str]]:
    """把初稿或整包修订统一收进平台硬限制，终检看到的就是最终形态。"""
    raw_title = strip_zero_width(title).strip()
    normalized_title = _truncate(raw_title, MAX_XHS_TITLE_CHARS)
    if not normalized_title:
        normalized_title = _truncate(strip_zero_width(fallback_title).strip(), MAX_XHS_TITLE_CHARS)
        warnings.append("模型没给标题，已回退到封面文案")
    elif len(raw_title) > MAX_XHS_TITLE_CHARS:
        warnings.append(f"标题 {len(raw_title)} 字超过 {MAX_XHS_TITLE_CHARS} 字，已截断")

    normalized_tags = normalize_tags(tags, limit=max_tags)
    if len(normalized_tags) < min_tags:
        warnings.append(f"只有 {len(normalized_tags)} 个话题标签，低于建议下限 {min_tags} 个")

    normalized_body = clean_body(strip_zero_width(body))
    tag_suffix = _render_tag_suffix(normalized_tags)
    publish_body_limit = MAX_XHS_BODY_CHARS - len(tag_suffix)
    if len(normalized_body) > publish_body_limit:
        total_chars = len(normalized_body) + len(tag_suffix)
        warnings.append(
            f"正文与标签合计 {total_chars} 字超过 {MAX_XHS_BODY_CHARS} 字，已在终检前截断"
        )
        normalized_body = (
            normalized_body[: max(publish_body_limit - 1, 0)] + "…"
            if publish_body_limit > 0
            else ""
        )
    canonical_body = f"{normalized_body}{tag_suffix}"

    normalized_images, image_warnings = normalize_image_prompts(
        image_prompts, count=image_prompt_count
    )
    warnings.extend(image_warnings)
    normalized_alts = [
        _truncate(strip_zero_width(value).strip(), MAX_XHS_TITLE_CHARS)
        for value in alt_titles
        if strip_zero_width(value).strip()
    ]
    return (
        normalized_title,
        normalized_alts,
        canonical_body,
        normalized_tags,
        normalized_images,
    )


def _run_selfcheck(
    llm: SupportsLLM,
    *,
    system: str,
    suggested_angle: str,
    title: str,
    alt_titles: list[str],
    body: str,
    cards: str,
    tags: list[str],
    image_prompts: list[str],
    phase: str,
) -> tuple[XhsSelfCheck, Usage]:
    result = llm.parse(
        prompts.load(
            "xhs/selfcheck",
            suggested_angle=suggested_angle or "（无）",
            title=title,
            alt_titles=_render_alt_titles(alt_titles),
            body=body,
            cards=cards,
            tags="、".join(tags) or "（无）",
            image_prompts=_render_image_prompts(image_prompts),
        ),
        XhsSelfCheck,
        system=system,
        max_tokens=budget_for("xhs.selfcheck"),
        purpose="xhs.selfcheck",
    )
    check = result.parsed
    logger.info(
        "小红书%s overall=%d ai_flavor=%d compliance=%d verdict=%s blocking=%d",
        phase,
        check.overall,
        check.ai_flavor,
        check.compliance_risk,
        check.verdict,
        len(check.blocking_issues),
    )
    return check, result.usage


def _render_image_prompts(image_prompts: list[str]) -> str:
    if not image_prompts:
        return "（无配图 prompt）"
    return "\n".join(f"- {value}" for value in image_prompts)


def _render_alt_titles(alt_titles: list[str]) -> str:
    if not alt_titles:
        return "（无备选标题）"
    return "\n".join(f"- {value}" for value in alt_titles)


def _render_tag_suffix(tags: list[str]) -> str:
    if not tags:
        return ""
    return "\n\n" + " ".join(f"#{tag}" for tag in tags)


def _render_cards_text(cover_headline: str, pages: list[PageSpec]) -> str:
    """把卡片脚本转成纯文本，喂给后续 prompt 并留进 trace。"""
    lines = [f"封面：{cover_headline}"]
    for index, page in enumerate(pages, start=1):
        lines.append(f"第 {index} 页：{page.headline}")
        lines.extend(f"  - {bullet}" for bullet in page.bullets)
        if page.footnote:
            lines.append(f"  （{page.footnote}）")
    return "\n".join(lines)


__all__ = [
    "BULLET_RANGE",
    "DEFAULT_TARGET_BODY_CHARS",
    "MAX_BULLET_CHARS",
    "MAX_COVER_HEADLINE_CHARS",
    "MAX_FOOTNOTE_CHARS",
    "MAX_PAGE_HEADLINE_CHARS",
    "MAX_TAG_CHARS",
    "REWRITE_THRESHOLD",
    "PageSpec",
    "XhsCardPlan",
    "XhsNoteCopy",
    "XhsNoteDraft",
    "XhsQualityError",
    "XhsRevision",
    "XhsSelfCheck",
    "generate_xhs_note",
    "normalize_pages",
    "normalize_tags",
]
