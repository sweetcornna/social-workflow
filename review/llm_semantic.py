"""第三级审核：LLM 语境判定。

前两级（词面 + 平台违禁）只看字面，误报率高——"扫黄打非取得成效"会命中涉黄词表。
这一级把命中片段连同全文交给 Claude，判断在**当前语境**下是否真的违规，
并给出可直接替换的句子。

设计取舍：

- 只在前两级**有命中**时才调用。没有命中就不烧 token。
- 用结构化输出（``client.messages.parse``）而不是让模型自由写 JSON，
  省掉一层容易出错的解析。
- LLM 只能**降级或维持**前两级的判定，不能把 ``block`` 升成 ``pass`` 以外的东西：
  判 ``safe`` 时降为 ``info`` 保留痕迹，而不是直接删掉这条 finding。
  审核链路宁可留噪声，也不要静默丢证据。
- 任何异常（缺 key / 限流 / 预算耗尽）都**不阻断管线**：跳过本级并在
  ``stages_skipped`` 里记原因，让前两级的结论直接生效（更严格，是安全的方向）。
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

import prompts
from core.budget import BudgetExhausted
from generation.llm import LLMError, SupportsLLM
from generation.output_budget import budget_for
from review.base import Finding

logger = logging.getLogger("social_workflow.review.llm_semantic")

Verdict = Literal["violation", "suspicious", "safe"]

#: LLM 判定 → Finding.level
VERDICT_LEVELS: dict[str, str] = {
    "violation": "block",
    "suspicious": "warn",
    "safe": "info",
}


class HitJudgement(BaseModel):
    """对单条命中的语境判定。"""

    model_config = ConfigDict(extra="forbid")

    rule: str = Field(description="原样抄回的规则 id")
    verdict: Verdict
    reason: str = Field(description="针对该段语境的一句话理由")
    replacement: str = Field(default="", description="可直接替换原文的句子；safe 时为空")


class ExtraRisk(BaseModel):
    """词表查不出、但模型认为有风险的内容。"""

    model_config = ConfigDict(extra="forbid")

    excerpt: str = Field(description="原文片段")
    risk: str = Field(description="风险描述")
    suggestion: str = Field(default="")


class SemanticReview(BaseModel):
    """LLM 语义审核的结构化输出。"""

    model_config = ConfigDict(extra="forbid")

    judgements: list[HitJudgement] = Field(default_factory=list)
    extra_risks: list[ExtraRisk] = Field(default_factory=list)


class SemanticSkipped(Exception):
    """本级被跳过（缺前置条件或调用失败），携带原因。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def format_hits(findings: list[Finding]) -> str:
    """把待判定的命中渲染成 prompt 里的列表。"""
    lines: list[str] = []
    for finding in findings:
        excerpt = finding.excerpt.replace("\n", " ")
        lines.append(
            f"- rule: `{finding.rule}`\n"
            f"  - 机器初判: {finding.level}\n"
            f"  - 命中上下文: {excerpt}\n"
            f"  - 命中内容: {finding.extra.get('word') or finding.extra.get('matched') or ''}"
        )
    return "\n".join(lines)


def judge(
    content: str,
    findings: list[Finding],
    llm: SupportsLLM,
    *,
    max_hits: int = 30,
) -> SemanticReview:
    """调用 LLM 做语境判定。失败抛 :class:`SemanticSkipped`。"""
    candidates = [f for f in findings if f.level in ("block", "warn")][:max_hits]
    if not candidates:
        raise SemanticSkipped("前两级无 block/warn 命中，无需语境判定")

    prompt = prompts.load(
        "review/semantic",
        content=content,
        hits=format_hits(candidates),
    )
    try:
        result = llm.parse(
            prompt,
            SemanticReview,
            max_tokens=budget_for("review.semantic"),
            purpose="review.semantic",
        )
    except BudgetExhausted as exc:
        raise SemanticSkipped(f"token 预算耗尽: {exc}") from exc
    except LLMError as exc:
        raise SemanticSkipped(f"LLM 调用失败: {exc}") from exc
    return result.parsed


def apply(
    findings: list[Finding],
    review: SemanticReview,
) -> tuple[list[Finding], dict[str, str]]:
    """把语义判定合并回 findings，返回 ``(新 findings, suggested_edits)``。

    规则 id 对不上时保留原判定——模型抄错 id 不应该让一条 block 悄悄消失。
    """
    by_rule: dict[str, HitJudgement] = {j.rule: j for j in review.judgements}
    edits: dict[str, str] = {}
    merged: list[Finding] = []

    for finding in findings:
        judgement = by_rule.get(finding.rule)
        if judgement is None or finding.level == "info":
            merged.append(finding)
            continue
        level = VERDICT_LEVELS.get(judgement.verdict, finding.level)
        suggestion = judgement.replacement or judgement.reason or finding.suggestion
        merged.append(
            finding.model_copy(
                update={
                    "level": level,
                    "suggestion": suggestion,
                    "extra": {
                        **finding.extra,
                        "llm_verdict": judgement.verdict,
                        "llm_reason": judgement.reason,
                        "machine_level": finding.level,
                    },
                }
            )
        )
        if judgement.replacement and finding.excerpt:
            edits[finding.excerpt] = judgement.replacement

    for risk in review.extra_risks:
        merged.append(
            Finding(
                level="warn",
                rule="llm_semantic.extra_risk",
                excerpt=risk.excerpt,
                suggestion=risk.suggestion or risk.risk,
                stage="llm_semantic",
                extra={"risk": risk.risk},
            )
        )
        if risk.suggestion and risk.excerpt:
            edits[risk.excerpt] = risk.suggestion

    return merged, edits


__all__ = [
    "VERDICT_LEVELS",
    "ExtraRisk",
    "HitJudgement",
    "SemanticReview",
    "SemanticSkipped",
    "Verdict",
    "apply",
    "format_hits",
    "judge",
]
