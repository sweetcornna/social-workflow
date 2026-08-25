"""公众号文案生成链（去 AI 味 SOP）。

::

    大纲 → 正文 → 风格润色 → 质量自评 → 去 AI 味改写

五步各是一次独立调用，而不是一个大 prompt 让模型"一次写好"：
每一步的输入都是上一步的**成品**，模型每次只处理一件事，
比单轮长 prompt 稳定得多，也让中间产物可审计（都留在 :class:`ArticleDraft.trace` 里）。

质量自评那一步走结构化输出，拿到分数后**有条件**地决定要不要做第五步——
自评已经很好就不改了，避免越改越平。
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
from generation.textutil import accumulate_usage as _accumulate
from generation.textutil import clean_body, strip_code_fence, truncate
from review.inspect import MAX_DIGEST_CHARS, MAX_TITLE_CHARS

logger = logging.getLogger("social_workflow.generation.wechat_article")

DEFAULT_TARGET_WORDS = 1500
#: 自评总分低于该值才触发第五步去 AI 味改写
REWRITE_THRESHOLD = 8
#: 封面文案上限，和 prompt 里保持一致
MAX_COVER_TITLE_CHARS = 14

Verdict = Literal["pass", "revise", "reject"]


class SelfCheck(BaseModel):
    """质量自评的结构化输出。"""

    model_config = ConfigDict(extra="forbid")

    ai_flavor: int = Field(ge=0, le=10, description="分数越高越不像 AI 写的")
    specificity: int = Field(ge=0, le=10)
    hook: int = Field(ge=0, le=10)
    structure: int = Field(ge=0, le=10)
    fact_risk: int = Field(ge=0, le=10, description="分数越高风险越低")
    overall: int = Field(ge=0, le=10)
    verdict: Verdict
    blocking_issues: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)

    @property
    def needs_rewrite(self) -> bool:
        return (
            self.verdict != "pass"
            or self.overall < REWRITE_THRESHOLD
            or self.ai_flavor < REWRITE_THRESHOLD
            or bool(self.blocking_issues)
        )


class ArticleMeta(BaseModel):
    """标题 / 摘要 / 封面提示词。"""

    model_config = ConfigDict(extra="forbid")

    title: str
    alt_titles: list[str] = Field(default_factory=list)
    digest: str
    cover_prompt: str = ""
    cover_title: str = ""
    keywords: list[str] = Field(default_factory=list)
    #: 生图模型用的英文 prompt（P11）。题图只用第一条；没给时回落到 ``cover_prompt``
    image_prompts: list[str] = Field(
        default_factory=list, description="英文生图 prompt，一条一张；不配图时留空数组"
    )


@dataclass
class ArticleDraft:
    """生成链的完整产物。"""

    title: str
    digest: str
    body_markdown: str
    meta: ArticleMeta
    selfcheck: SelfCheck | None = None
    #: 各步中间产物，供审计与复盘
    trace: dict[str, str] = field(default_factory=dict)
    usage: Usage = field(default_factory=Usage)
    rewritten: bool = False

    @property
    def image_prompts(self) -> list[str]:
        """题图用的生图 prompt。模型没给 ``image_prompts`` 时回落到老字段 ``cover_prompt``。

        回落是有意义的：``cover_prompt`` 本来就是"一句英文的图像生成提示词"，
        只是没带 P11 的硬约束；有总比没有强，缺的约束由 ``prompts/imagegen.md``
        在下一次生成时补上。
        """
        if self.meta.image_prompts:
            return list(self.meta.image_prompts)
        prompt = (self.meta.cover_prompt or "").strip()
        return [prompt] if prompt else []

    def as_platform_extra(self) -> dict[str, Any]:
        """写进 ``ContentBundle.platform_extra`` 的生成侧字段。"""
        return {
            "digest": self.digest,
            "alt_titles": self.meta.alt_titles,
            "keywords": self.meta.keywords,
            "cover_prompt": self.meta.cover_prompt,
            "cover_title": self.meta.cover_title,
            # 配图 prompt 留痕：出了问题要能回看"当时让模型画的是什么"
            "image_prompts": list(self.image_prompts),
            "selfcheck": self.selfcheck.model_dump() if self.selfcheck else None,
            "rewritten": self.rewritten,
        }


# ------------------------------------------------------------------ 生成链
#
# ``strip_code_fence`` / ``clean_body`` / ``truncate`` 已挪到 generation/textutil.py
# 与小红书链共用，这里保留 re-export（见 __all__），旧的 import 路径不受影响。


def generate_article(
    llm: SupportsLLM,
    *,
    topic_title: str,
    persona: str,
    topic_source: str = "",
    topic_url: str = "",
    topic_context: str = "",
    target_words: int = DEFAULT_TARGET_WORDS,
    force_rewrite: bool | None = None,
    image_prompt_count: int = 0,
) -> ArticleDraft:
    """跑完五步生成链。

    ``force_rewrite`` 为 ``None`` 时按自评结果决定是否做第五步；
    显式 ``True``/``False`` 可强制或跳过（测试与省 token 用）。

    ``image_prompt_count > 0`` 时让配 meta 那一步顺手产出英文生图 prompt（P11），
    题图用第一条。不额外开调用，理由同 :func:`generation.xhs_note.generate_xhs_note`。
    """
    system = prompts.load("wechat/system", persona=persona or "（未提供人设）")
    total = Usage()
    trace: dict[str, str] = {}

    # 1 大纲
    outline_result = llm.complete(
        prompts.load(
            "wechat/outline",
            topic_title=topic_title,
            topic_source=topic_source or "未标注",
            topic_url=topic_url or "无",
            topic_context=topic_context or "无额外信息，按公开常识处理",
            target_words=target_words,
        ),
        system=system,
        max_tokens=budget_for("wechat.outline"),
        purpose="wechat.outline",
    )
    outline = strip_code_fence(outline_result.text)
    trace["outline"] = outline
    total = _accumulate(total, outline_result.usage)

    # 2 正文
    body_result = llm.complete_long(
        prompts.load("wechat/body", outline=outline, target_words=target_words),
        system=system,
        max_tokens=budget_for("wechat.body"),
        purpose="wechat.body",
    )
    body = clean_body(body_result.text)
    trace["body"] = body
    total = _accumulate(total, body_result.usage)

    # 3 风格润色
    polish_result = llm.complete_long(
        prompts.load("wechat/polish", draft=body),
        system=system,
        max_tokens=budget_for("wechat.polish"),
        purpose="wechat.polish",
    )
    polished = clean_body(polish_result.text)
    trace["polished"] = polished
    total = _accumulate(total, polish_result.usage)

    # 4 质量自评
    check_result = llm.parse(
        prompts.load("wechat/selfcheck", draft=polished),
        SelfCheck,
        system=system,
        max_tokens=budget_for("wechat.selfcheck"),
        purpose="wechat.selfcheck",
    )
    selfcheck = check_result.parsed
    total = _accumulate(total, check_result.usage)
    logger.info(
        "自评 overall=%d ai_flavor=%d verdict=%s blocking=%d",
        selfcheck.overall,
        selfcheck.ai_flavor,
        selfcheck.verdict,
        len(selfcheck.blocking_issues),
    )

    # 5 去 AI 味改写（有条件）
    final = polished
    rewritten = False
    should_rewrite = selfcheck.needs_rewrite if force_rewrite is None else force_rewrite
    if should_rewrite:
        issues = selfcheck.blocking_issues + selfcheck.suggestions
        rewrite_result = llm.complete_long(
            prompts.load(
                "wechat/dehumanize",
                draft=polished,
                issues="\n".join(f"- {i}" for i in issues) or "（自评未列出具体问题）",
            ),
            system=system,
            max_tokens=budget_for("wechat.dehumanize"),
            purpose="wechat.dehumanize",
        )
        final = clean_body(rewrite_result.text)
        trace["dehumanized"] = final
        total = _accumulate(total, rewrite_result.usage)
        rewritten = True

    # 标题 / 摘要 / 封面提示词
    meta_result = llm.parse(
        prompts.load(
            "wechat/meta",
            body=final,
            persona=persona or "（未提供人设）",
            max_title=MAX_TITLE_CHARS,
            max_digest=MAX_DIGEST_CHARS,
            image_rules=image_prompt_rules(image_prompt_count),
        ),
        ArticleMeta,
        system=system,
        max_tokens=budget_for("wechat.meta"),
        purpose="wechat.meta",
    )
    meta = meta_result.parsed
    total = _accumulate(total, meta_result.usage)

    # 模型有时会超字数，这里兜底截断——长度是平台硬限制，不能只靠 prompt 约束
    title = truncate(meta.title, MAX_TITLE_CHARS) or truncate(topic_title, MAX_TITLE_CHARS)
    digest = truncate(meta.digest, MAX_DIGEST_CHARS)
    if len(meta.title) > MAX_TITLE_CHARS:
        logger.warning("模型给的标题 %d 字，超限已截断: %s", len(meta.title), meta.title)
    image_prompts, _ = normalize_image_prompts(meta.image_prompts, count=image_prompt_count)
    meta = meta.model_copy(
        update={
            "title": title,
            "digest": digest,
            "cover_title": truncate(meta.cover_title, MAX_COVER_TITLE_CHARS),
            "image_prompts": image_prompts,
        }
    )

    return ArticleDraft(
        title=title,
        digest=digest,
        body_markdown=final,
        meta=meta,
        selfcheck=selfcheck,
        trace=trace,
        usage=total,
        rewritten=rewritten,
    )


__all__ = [
    "DEFAULT_TARGET_WORDS",
    "MAX_COVER_TITLE_CHARS",
    "REWRITE_THRESHOLD",
    "ArticleDraft",
    "ArticleMeta",
    "SelfCheck",
    "clean_body",
    "generate_article",
    "strip_code_fence",
    "truncate",
]
