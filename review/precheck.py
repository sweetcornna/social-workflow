"""平台违禁预检：复用 ``yuwen-cool/yuwen-publish-precheck``（MIT）的规则数据。

上游不是 pip / npm 包（它是一个 Claude Agent Skill：``SKILL.md`` + ``scripts/scan.py``），
所以按任务书的兜底方案 vendored 拷入 ``review/vendor/yuwen_precheck/``——
**只拷规则数据 `terms.json` 与 LICENSE，不拷面向 agent 的指令性文档**，
理由见 ``review/vendor/yuwen_precheck/PROVENANCE.md``。

匹配逻辑是本项目自己实现的，接的是本项目的 :class:`review.base.Finding` 契约。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from review.base import Finding, excerpt_around
from review.vendor.yuwen_precheck import TERMS_PATH, UPSTREAM, UPSTREAM_LICENSE

logger = logging.getLogger("social_workflow.review.precheck")

#: 上游 severity → 本项目 Finding.level
SEVERITY_LEVELS: dict[str, str] = {
    "critical": "block",
    "high": "block",
    "medium": "warn",
    "low": "info",
}

#: 上游 industries 取值
KNOWN_INDUSTRIES = frozenset(
    {"education", "finance", "health", "health_food", "medical", "medical_beauty", "pharma"}
)


class PrecheckDataMissing(FileNotFoundError):
    """vendored 规则数据缺失。"""


@dataclass(frozen=True)
class Term:
    """一条平台违禁规则。"""

    rule: str
    title: str
    severity: str
    pattern: re.Pattern[str]
    commercial_only: bool = False
    industries: frozenset[str] = frozenset()
    doc: str = ""

    @property
    def level(self) -> str:
        return SEVERITY_LEVELS.get(self.severity, "warn")


@dataclass(frozen=True)
class Myth:
    """被平台辟谣的"错误规避认知"。命中说明作者在做没必要的谐音替换。"""

    pattern: re.Pattern[str]
    myth: str
    note: str
    suggestion: str


@dataclass
class PrecheckRules:
    terms: list[Term] = field(default_factory=list)
    myths: list[Myth] = field(default_factory=list)
    generated_at: str = ""

    def __len__(self) -> int:
        return len(self.terms)


def _compile(pattern: str, *, rule: str) -> re.Pattern[str] | None:
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:  # pragma: no cover - 上游数据损坏时才触发
        logger.warning("规则 %s 的正则无法编译，已跳过: %s", rule, exc)
        return None


def load_rules(path: str | Path | None = None) -> PrecheckRules:
    """读取 vendored ``terms.json``。"""
    target = Path(path) if path is not None else TERMS_PATH
    if not target.is_file():
        raise PrecheckDataMissing(
            f"vendored 规则数据缺失: {target}。"
            f"执行 `uv run python scripts/fetch_lexicon.py --precheck-only` 重新拉取"
            f"（上游 {UPSTREAM}，{UPSTREAM_LICENSE}）"
        )
    data: dict[str, Any] = json.loads(target.read_text(encoding="utf-8"))

    terms: list[Term] = []
    for entry in data.get("terms", []):
        rule = str(entry.get("rule", "")) or "unknown"
        compiled = _compile(str(entry.get("pattern", "")), rule=rule)
        if compiled is None:
            continue
        terms.append(
            Term(
                rule=rule,
                title=str(entry.get("title", "")),
                severity=str(entry.get("severity", "medium")),
                pattern=compiled,
                commercial_only=bool(entry.get("commercial_only", False)),
                industries=frozenset(entry.get("industries") or ()),
                doc=str(entry.get("doc", "")),
            )
        )

    myths: list[Myth] = []
    for entry in data.get("debunked_myths", []):
        compiled = _compile(str(entry.get("pattern", "")), rule="debunked_myth")
        if compiled is None:
            continue
        myths.append(
            Myth(
                pattern=compiled,
                myth=str(entry.get("myth", "")),
                note=str(entry.get("note", "")),
                suggestion=str(entry.get("suggestion", "")),
            )
        )

    return PrecheckRules(terms=terms, myths=myths, generated_at=str(data.get("generated_at", "")))


@lru_cache(maxsize=4)
def get_rules(path: str | None = None) -> PrecheckRules:
    return load_rules(path)


def clear_cache() -> None:
    get_rules.cache_clear()


def rule_applies(term: Term, *, commercial: bool, industries: frozenset[str]) -> bool:
    """商业场景 / 行业开关。与上游 ``rule_applies`` 语义一致。"""
    if term.commercial_only and not commercial:
        return False
    return not (term.industries and not (term.industries & industries))


def scan(
    text: str,
    *,
    commercial: bool = False,
    industries: frozenset[str] | set[str] | None = None,
    rules: PrecheckRules | None = None,
) -> list[Finding]:
    """扫描文本。``commercial=True`` 时启用带货 / 商业场景规则。"""
    if not text:
        return []
    active = rules if rules is not None else get_rules()
    industry_set = frozenset(industries or ())
    unknown = industry_set - KNOWN_INDUSTRIES
    if unknown:
        logger.warning("未知行业标签，规则不会命中: %s", sorted(unknown))

    findings: list[Finding] = []
    for term in active.terms:
        if not rule_applies(term, commercial=commercial, industries=industry_set):
            continue
        match = term.pattern.search(text)
        if match is None:
            continue
        findings.append(
            Finding(
                level=term.level,  # type: ignore[arg-type]
                rule=f"precheck.{term.rule}",
                excerpt=excerpt_around(text, match.start(), match.end()),
                suggestion=f"{term.title}：删除或改写命中表述「{match.group(0)}」",
                stage="precheck",
                start=match.start(),
                end=match.end(),
                extra={
                    "severity": term.severity,
                    "title": term.title,
                    "doc": term.doc,
                    "matched": match.group(0),
                    "commercial_only": term.commercial_only,
                    "industries": sorted(term.industries),
                },
            )
        )

    # 反向规则：命中说明作者做了没必要的谐音规避，提示改回正常表达
    for myth in active.myths:
        match = myth.pattern.search(text)
        if match is None:
            continue
        findings.append(
            Finding(
                level="info",
                rule="precheck.debunked_myth",
                excerpt=excerpt_around(text, match.start(), match.end()),
                suggestion=myth.suggestion or myth.note,
                stage="precheck",
                start=match.start(),
                end=match.end(),
                extra={"myth": myth.myth, "note": myth.note, "matched": match.group(0)},
            )
        )
    return findings


__all__ = [
    "KNOWN_INDUSTRIES",
    "SEVERITY_LEVELS",
    "Myth",
    "PrecheckDataMissing",
    "PrecheckRules",
    "Term",
    "clear_cache",
    "get_rules",
    "load_rules",
    "rule_applies",
    "scan",
]
