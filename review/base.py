"""审核层共用契约。

``Finding`` 的三个等级来自 ``review/README.md``：

- ``block``——不能发。内容留在 ``draft``，findings 写进 ``ContentItem.review_notes``，
  **不进人工队列**（不让人去点一个必然要驳回的东西）。
- ``warn``——可疑。进人工队列，在详情页高亮，由人决定。
- ``info``——提示。不阻断，仅供参考（如"这里不必做谐音规避"）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Level = Literal["block", "warn", "info"]
LEVELS: tuple[Level, ...] = ("block", "warn", "info")
#: 严重度从高到低，便于排序
LEVEL_ORDER: dict[str, int] = {"block": 0, "warn": 1, "info": 2}

Stage = Literal["lexicon", "precheck", "llm_semantic", "inspect"]


class Finding(BaseModel):
    """一条审核发现。"""

    model_config = ConfigDict(extra="forbid")

    level: Level
    #: 规则 id，形如 ``lexicon.政治类型`` / ``precheck.general-community-safety.G01``
    rule: str
    #: 命中的原文片段（带上下文），**必须**是原文里真实存在的内容，便于人工复核
    excerpt: str = ""
    #: 可落地的修改建议
    suggestion: str = ""
    stage: Stage = "lexicon"
    #: 命中位置（字符下标），没有就留空
    start: int | None = None
    end: int | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.level == "block"


class ReviewResult(BaseModel):
    """三级管线的输出。"""

    model_config = ConfigDict(extra="forbid")

    passed: bool
    findings: list[Finding] = Field(default_factory=list)
    #: 建议改写：``{原文片段: 建议替换}``，供改稿 Agent / 人工参考
    suggested_edits: dict[str, str] = Field(default_factory=dict)
    #: 各级是否实际执行（LLM 语义级可能因缺 key / 预算耗尽被跳过）
    stages_run: list[str] = Field(default_factory=list)
    stages_skipped: dict[str, str] = Field(default_factory=dict)

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "block"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "warn"]

    def summary(self) -> str:
        """写进 ``ContentItem.review_notes`` 的人类可读摘要。"""
        if not self.findings:
            return "机器审核通过，无发现。"
        lines = [
            f"机器审核{'通过' if self.passed else '未通过'}："
            f"block {len(self.blocking)} / warn {len(self.warnings)} / "
            f"info {len(self.findings) - len(self.blocking) - len(self.warnings)}"
        ]
        for finding in sort_findings(self.findings):
            excerpt = finding.excerpt.replace("\n", " ")
            if len(excerpt) > 60:
                excerpt = excerpt[:60] + "…"
            line = f"[{finding.level}] {finding.rule}"
            if excerpt:
                line += f" · 命中「{excerpt}」"
            if finding.suggestion:
                line += f" · 建议：{finding.suggestion}"
            lines.append(line)
        for stage, why in self.stages_skipped.items():
            lines.append(f"[skip] {stage}：{why}")
        return "\n".join(lines)


def sort_findings(findings: list[Finding]) -> list[Finding]:
    """按严重度、再按出现位置排序。"""
    return sorted(
        findings,
        key=lambda f: (LEVEL_ORDER.get(f.level, 9), f.start if f.start is not None else 1 << 30),
    )


def excerpt_around(text: str, start: int, end: int, *, radius: int = 24) -> str:
    """取命中点前后各 ``radius`` 个字符，越界处加省略号。"""
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    chunk = text[left:right].replace("\n", " ")
    return ("…" if left > 0 else "") + chunk + ("…" if right < len(text) else "")


__all__ = [
    "LEVELS",
    "LEVEL_ORDER",
    "Finding",
    "Level",
    "ReviewResult",
    "Stage",
    "excerpt_around",
    "sort_findings",
]
