"""选题决策 Agent：按账号人设 + 历史发文，给候选热点打分并推荐。

输入：账号 ``persona.md`` + 候选 :class:`~sourcing.base.RawTopic` 列表 + 最近已发选题。
输出：结构化打分（Pydantic），带推荐理由与切入角度。

设计取舍：

- 用 ``client.messages.parse`` 拿结构化输出，不让模型自由写 JSON。
- 候选用**短 id**（``c1``、``c2``）而不是数据库 id 参与对话——省 token，
  也避免模型把长 id 抄错。返回后再映射回真实 Topic。
- 模型给空 ``recommended`` 是**合法结果**（"今天没有值得写的"），
  上层必须能接受"今天不出稿"，不能强行取 top-1。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

import prompts
from core.models import ContentItem, Topic, utcnow
from generation.llm import SupportsLLM
from generation.output_budget import budget_for
from sourcing.base import RawTopic

logger = logging.getLogger("social_workflow.sourcing.selector")

DEFAULT_MAX_PICKS = 3
DEFAULT_RECENT_DAYS = 14
#: 送进 prompt 的候选上限。再多模型也分不清，而且线性烧 token
DEFAULT_MAX_CANDIDATES = 30
#: 送进 prompt 的复盘条数。只看最近两轮：更早的结论多半已经被后面的推翻
DEFAULT_INSIGHTS_ENTRIES = 2
#: 选题调用的输出预算。**必须显式传**，不能吃 ``llm_max_tokens`` 的兜底值。
#:
#: 两笔账加起来就超了原来的 4096：
#:
#: 1. prompt 要求给**每条**候选打分（``reason`` + ``angle`` 各一句话），
#:    ``DEFAULT_MAX_CANDIDATES`` 条候选光 JSON 正文就是 3000+ token
#:    （成本账本实测 ``sourcing.select`` 一次 9047 output token）；
#: 2. 现在的默认模型是 reasoning 模型（deepseek-v4-flash），思考阶段与正文共用这份预算，
#:    思考几千 token 起步——正文一个字都没写就先被 max_tokens 掐了。
#:
#: 2026-08-17 生产事故正是这样：选题调用要么被截断成空回复，要么"回复里找不到 JSON 对象"。
#: P11.2 起取值收编进 :mod:`generation.output_budget` 的统一分档表（大输出档 16000，
#: 与 ``llm_article_max_tokens`` 同值，dsh 后端不用为它多开一个 runtime 桶）；
#: 名字保留，它已经进了单测与 P10.1 的交付记录。
SELECT_MAX_TOKENS = budget_for("sourcing.select")


class TopicScore(BaseModel):
    """单条候选的打分。"""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(description="候选编号，形如 c1")
    fit: int = Field(ge=0, le=10, description="与账号人设的匹配度")
    freshness: int = Field(ge=0, le=10, description="时效性")
    depth: int = Field(ge=0, le=10, description="能否写出独家视角")
    risk: int = Field(ge=0, le=10, description="分数越高越安全")
    overall: int = Field(ge=0, le=10)
    reason: str
    angle: str = Field(default="", description="切入角度")


class SelectionResult(BaseModel):
    """选题 Agent 的完整输出。"""

    model_config = ConfigDict(extra="forbid")

    scores: list[TopicScore] = Field(default_factory=list)
    recommended: list[str] = Field(
        default_factory=list, description="推荐的 candidate_id，按推荐顺序"
    )
    note: str = Field(default="", description="整体说明；无推荐时必须写明原因")


@dataclass
class Selection:
    """把 LLM 输出映射回真实选题后的结果。"""

    #: 按推荐顺序排列的 ``(topic, score)``
    picks: list[tuple[RawTopic, TopicScore]]
    #: 全部候选的打分，未被推荐的也在
    scores: dict[str, TopicScore]
    note: str
    raw: SelectionResult

    @property
    def top(self) -> RawTopic | None:
        return self.picks[0][0] if self.picks else None

    def __bool__(self) -> bool:
        return bool(self.picks)


def format_candidates(topics: list[RawTopic]) -> tuple[str, dict[str, RawTopic]]:
    """渲染候选列表，返回 ``(prompt 文本, candidate_id → topic)``。"""
    mapping: dict[str, RawTopic] = {}
    lines: list[str] = []
    for index, topic in enumerate(topics, start=1):
        cid = f"c{index}"
        mapping[cid] = topic
        info = topic.raw.get("info") or topic.raw.get("hot_value") or ""
        board = topic.raw.get("board", "")
        detail = " · ".join(str(x) for x in (topic.source, board, info) if x)
        lines.append(f"- `{cid}` {topic.title}" + (f"（{detail}）" if detail else ""))
    return "\n".join(lines), mapping


def recent_titles(
    session: Session,
    account_id: str,
    *,
    days: int = DEFAULT_RECENT_DAYS,
    limit: int = 30,
) -> list[str]:
    """该账号最近发过 / 正在走流程的选题标题。

    刻意不只看 ``published``——正在审核和已排期的也算"占用了这个选题"，
    否则同一天会生成两篇同题稿子。
    """
    since = utcnow() - timedelta(days=days)
    rows = session.scalars(
        select(ContentItem)
        .where(ContentItem.account_id == account_id, ContentItem.created_at >= since)
        .order_by(ContentItem.created_at.desc())
        .limit(limit)
    )
    titles: list[str] = []
    for item in rows:
        title = item.title
        if title:
            titles.append(title)
    return titles


def load_insights(account_id: str, *, limit: int = DEFAULT_INSIGHTS_ENTRIES) -> str:
    """读该账号最近的复盘结论（``prompts/accounts/<id>/insights.md``，P4 写入）。

    由 ``metrics/insights.py`` 产出。没有就返回空串——选题照常跑，只是少一层上下文。
    """
    return prompts.load_insights(account_id, limit=limit)


def select_topics(
    candidates: list[RawTopic],
    llm: SupportsLLM,
    *,
    persona: str,
    recent: list[str] | None = None,
    insights: str = "",
    max_picks: int = DEFAULT_MAX_PICKS,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    today: str | None = None,
) -> Selection:
    """调用选题 Agent。候选为空时直接返回空结果，不调用 LLM。

    ``insights``：复盘 Agent 的历史结论（见 :func:`load_insights`）。
    它是**闭环的输入端**——指标 → 复盘 → 选题权重，缺了这一段整条链只是单向的。
    """
    pool = candidates[:max_candidates]
    if not pool:
        empty = SelectionResult(note="没有候选选题")
        return Selection(picks=[], scores={}, note=empty.note, raw=empty)

    rendered, mapping = format_candidates(pool)
    prompt = prompts.load(
        "sourcing/select",
        today=today or utcnow().date().isoformat(),
        persona=persona or "（未提供人设，按通用资讯号处理）",
        recent="\n".join(f"- {t}" for t in (recent or [])) or "（暂无历史发文）",
        insights=insights.strip() or "（还没有复盘数据，这是这个账号的早期阶段）",
        candidates=rendered,
        max_picks=max_picks,
    )
    # effort 保持后端默认（medium）：这里缺的是**写出来**的预算，不是想得更久
    result = llm.parse(
        prompt, SelectionResult, max_tokens=SELECT_MAX_TOKENS, purpose="sourcing.select"
    ).parsed

    scores = {s.candidate_id: s for s in result.scores}
    picks: list[tuple[RawTopic, TopicScore]] = []
    for cid in result.recommended[:max_picks]:
        topic = mapping.get(cid)
        if topic is None:
            logger.warning("选题 Agent 返回了不存在的 candidate_id: %s", cid)
            continue
        score = scores.get(cid) or TopicScore(
            candidate_id=cid, fit=0, freshness=0, depth=0, risk=0, overall=0, reason="模型未给分"
        )
        picks.append((topic, score))

    logger.info(
        "选题 Agent：候选 %d 条，推荐 %d 条%s",
        len(pool),
        len(picks),
        f"（{result.note}）" if result.note else "",
    )
    return Selection(picks=picks, scores=scores, note=result.note, raw=result)


def topics_from_rows(rows: list[Topic]) -> list[RawTopic]:
    """把库里的 ``Topic`` 行转回 :class:`RawTopic`，供选题 Agent 使用。"""
    return [
        RawTopic(
            source=row.source,
            title=row.title,
            url=row.url,
            score=row.score,
            raw=dict(row.raw or {}),
        )
        for row in rows
    ]


def load_candidates(
    session: Session,
    *,
    hours: int = 24,
    limit: int = DEFAULT_MAX_CANDIDATES,
    sources: list[str] | None = None,
) -> tuple[list[RawTopic], list[Topic]]:
    """从库里取最近入池的选题，按热度分降序。返回 ``(RawTopic 列表, 原始行)``。"""
    since = utcnow() - timedelta(hours=hours)
    stmt = select(Topic).where(Topic.created_at >= since)
    if sources:
        stmt = stmt.where(Topic.source.in_(sources))
    rows = list(session.scalars(stmt.order_by(Topic.score.desc()).limit(limit)))
    return topics_from_rows(rows), rows


def find_row(rows: list[Topic], topic: RawTopic) -> Topic | None:
    """把选中的 :class:`RawTopic` 映射回它的数据库行（用于填 ``ContentItem.topic_id``）。"""
    for row in rows:
        if row.title == topic.title and row.source == topic.source:
            return row
    return None


def selection_meta(selection: Selection) -> dict[str, Any]:
    """写进 ``ContentBundle.platform_extra`` 的选题决策留痕。"""
    if not selection.picks:
        return {"note": selection.note}
    topic, score = selection.picks[0]
    return {
        "topic_title": topic.title,
        "topic_source": topic.source,
        "topic_url": topic.url,
        "score_overall": score.overall,
        "score_fit": score.fit,
        "score_risk": score.risk,
        "reason": score.reason,
        "angle": score.angle,
    }


__all__ = [
    "DEFAULT_INSIGHTS_ENTRIES",
    "DEFAULT_MAX_CANDIDATES",
    "DEFAULT_MAX_PICKS",
    "DEFAULT_RECENT_DAYS",
    "SELECT_MAX_TOKENS",
    "Selection",
    "SelectionResult",
    "TopicScore",
    "find_row",
    "format_candidates",
    "load_candidates",
    "load_insights",
    "recent_titles",
    "select_topics",
    "selection_meta",
    "topics_from_rows",
]
