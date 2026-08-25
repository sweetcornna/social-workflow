"""复盘：读 ``prompts/accounts/<id>/insights.md``，以及手动触发一次复盘 Agent。

复盘结论刻意**写文件不写库**（和 persona 一样是人要看、要能手改、要出 git diff 的资产），
所以这里读的是文件；``metrics/insights.py`` 负责写。
"""

from __future__ import annotations

import re
from datetime import datetime

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

import prompts
from core.api.common import DbSession, Envelope, ok, safe_dt
from core.models import Account, utcnow

router = APIRouter(tags=["insights"])

#: 每条复盘的首行形如 ``## 2026-08-16 · 近 7 天复盘（acc_demo_xhs）``
_HEADING = re.compile(r"^##\s*(?P<date>\d{4}-\d{2}-\d{2})\s*·\s*(?P<title>.+?)\s*$")
#: 紧随其后的一行加粗结论
_HEADLINE = re.compile(r"^\*\*(?P<text>.+)\*\*$")


class InsightEntry(BaseModel):
    account_id: str
    #: 从标题里解析出的日期（``YYYY-MM-DD``），解析不出则空串
    date: str = ""
    title: str = ""
    #: 那句加粗的一句话结论
    headline: str = ""
    #: 条目原文（Markdown），前端直接渲染
    markdown: str


class AccountInsights(BaseModel):
    account_id: str
    name: str = ""
    platform: str = ""
    #: ``Account.extra['insights_updated_at']``，复盘 Agent 上次写盘的时刻
    updated_at: datetime | None = None
    error: str = ""
    path: str = ""
    exists: bool = False
    entries: list[InsightEntry] = Field(default_factory=list)


class RunIn(BaseModel):
    account_id: str | None = None
    #: 跳过"每账号 24 小时"的节流
    force: bool = False


class RunOut(BaseModel):
    tick: str = "insights"
    stats: dict[str, int] = Field(default_factory=dict)
    elapsed_s: float = 0.0
    message: str = ""


def parse_entries(account_id: str, text: str) -> list[InsightEntry]:
    """把 insights.md 拆成条目（新的在前）。"""
    entries: list[InsightEntry] = []
    for chunk in prompts.split_insights(text):
        lines = chunk.splitlines()
        date = title = headline = ""
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            heading = _HEADING.match(stripped)
            if heading and not title:
                date = heading.group("date")
                title = heading.group("title")
                continue
            bold = _HEADLINE.match(stripped)
            if bold and not headline:
                headline = bold.group("text")
        entries.append(
            InsightEntry(
                account_id=account_id,
                date=date,
                title=title,
                headline=headline,
                markdown=chunk,
            )
        )
    entries.reverse()  # 文件里是旧 → 新，界面上要新的在最上面
    return entries


def _for_account(account: Account) -> AccountInsights:
    path = prompts.insights_path(account.id)
    extra = dict(account.extra or {})
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    return AccountInsights(
        account_id=account.id,
        name=account.name,
        platform=account.platform,
        updated_at=safe_dt(extra.get("insights_updated_at")),
        error=str(extra.get("insights_error") or ""),
        path=str(path),
        exists=path.is_file(),
        entries=parse_entries(account.id, text) if text else [],
    )


@router.get("/insights", summary="复盘结论")
def list_insights(
    account_id: str | None = Query(default=None, description="留空 = 所有账号"),
    session: Session = DbSession,
) -> Envelope[list[AccountInsights]]:
    """读 ``prompts/accounts/<id>/insights.md`` 并拆成条目。没有文件时 ``exists=false``。"""
    stmt = select(Account).order_by(Account.platform, Account.id)
    if account_id:
        stmt = stmt.where(Account.id == account_id)
    return ok([_for_account(account) for account in session.scalars(stmt)])


@router.post("/insights/run", summary="立刻跑一次复盘")
def run_insights(payload: RunIn | None = None) -> Envelope[RunOut]:
    """触发 ``tick_insights``（与定时任务**同一个函数**）。

    没配 LLM 凭据时不会回落到假模型，整体跳过并在 ``stats.skipped_no_key`` 里记数——
    复盘是会持续影响后续选题的长期资产，宁可空着也不要用预置假文本污染它。
    这一跑可能要几十秒（真调 LLM），前端记得给它一个 loading 态。
    """
    from core.scheduler import run_tick

    body = payload or RunIn()
    started = utcnow()
    kwargs: dict[str, object] = {}
    if body.account_id:
        kwargs["account_ids"] = [body.account_id]
    if body.force:
        kwargs["force"] = True
    stats = run_tick("insights", **kwargs)
    elapsed = round((utcnow() - started).total_seconds(), 3)
    return ok(
        RunOut(
            stats=stats,
            elapsed_s=elapsed,
            message=(
                "未配置 LLM 凭据，本轮整体跳过"
                if stats.get("skipped_no_key")
                else f"复盘完成：写入 {stats.get('written', 0)} 份"
            ),
        )
    )


__all__ = ["parse_entries", "router"]
