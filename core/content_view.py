"""内容项的**呈现层**：把 ``ContentItem.bundle_json`` 变成模板 / JSON 都能直接用的结构。

这些函数原先是 ``core/main.py`` 里的私有助手（``_bundle_view`` 等）。P6 把工作台的
JSON API（``core/api/``）挂上来之后，两套门面要显示的是**同一份**东西——封面走哪个
下标、哪条内容需要"看完整片"、槽位文案怎么写。抽到这里是为了让它们只有一份实现，
而不是在 API 层复制一遍再慢慢漂移。

行为与 P5 完全一致（纯搬运），Jinja2 模板拿到的键一个都没动。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.accounts import policy_of
from core.models import Account, ContentItem, ReviewLog
from core.state_machine import diff_bundles
from publishers.base import MediaAsset


def _generated_paths(extra: dict[str, Any]) -> set[str]:
    """``platform_extra.illustrations`` 里记的生图落盘路径（P11）。

    同时收 ``path``（模型原图）与 ``final_path``（裁切后的成品）：挂进 media 的
    是后者，但两条都收下，将来改了裁切策略也不会让角标失灵。
    """
    entries = extra.get("illustrations")
    if not isinstance(entries, list):
        return set()
    paths: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for key in ("final_path", "path"):
            value = entry.get(key)
            if isinstance(value, str) and value:
                paths.add(value)
    return paths


def bundle_view(item: ContentItem) -> dict[str, Any]:
    """把 bundle_json 变成模板友好的结构（媒体带 index / exists，供缩略图与占位用）。"""
    raw = dict(item.bundle_json or {})
    extra = raw.get("platform_extra", {}) or {}
    generated = _generated_paths(extra)
    media = []
    for index, entry in enumerate(raw.get("media", []) or []):
        asset = MediaAsset.model_validate(entry)
        media.append(
            {
                "index": index,
                "path": asset.path,
                "kind": asset.kind,
                "cover": asset.cover,
                "exists": Path(asset.path).exists(),
                # 出处：imagegen = 生图模型画的照片，render = 本地 HTML 模板截的图。
                # 刻意不给 MediaAsset 加字段（P0 冻结契约），而是按 platform_extra
                # 里的生图流水反查路径——出处属于观测维度，不该动发布器的输入契约
                "source": "imagegen" if asset.path in generated else "render",
            }
        )
    cover = next((m for m in media if m["cover"]), None) or next(
        (m for m in media if m["kind"] == "image"), None
    )
    images = [m for m in media if m["kind"] == "image"]
    videos = [m for m in media if m["kind"] == "video"]
    render = extra.get("render") or {}
    return {
        "platform": raw.get("platform", ""),
        "title": raw.get("title", ""),
        "body_markdown": raw.get("body_markdown", ""),
        "body_html": raw.get("body_html"),
        "tags": raw.get("tags", []) or [],
        "media": media,
        "images": images,
        "videos": videos,
        "cover": cover,
        "digest": extra.get("digest", ""),
        "author": extra.get("author", ""),
        # 小红书专属：定时槽位与原创声明，人工审核时要能一眼看到
        "schedule_at": extra.get("schedule_at"),
        "is_original": extra.get("is_original"),
        # 抖音专属：成片时长与渲染任务，人工审核时要能一眼看到
        "duration_s": extra.get("duration_s"),
        "hook": extra.get("hook", ""),
        "script": extra.get("script", ""),
        "render": render,
    }


def needs_watch_confirm(item: ContentItem) -> bool:
    """这条内容在批准前是否必须勾选"已完整观看"。

    计划 2.2 的硬约束："成片必须人工看完整片才放行"。判定条件是**内容包里有视频**
    而不是"平台是抖音"——将来小红书发视频笔记时同样该拦。
    """
    for entry in (item.bundle_json or {}).get("media", []) or []:
        if str(entry.get("kind")) == "video":
            return True
    return False


def local_media_path(item: ContentItem, index: int) -> Path | None:
    """按下标取媒体文件的本地路径。只允许读工作目录内的文件。

    防目录穿越：``bundle_json`` 里的路径来自生成链，但它不该能读到仓库外的文件。
    """
    raw = dict(item.bundle_json or {})
    entries = raw.get("media", []) or []
    if not 0 <= index < len(entries):
        return None
    asset = MediaAsset.model_validate(entries[index])
    if asset.path.startswith(("http://", "https://")):
        return None
    root = Path.cwd().resolve()
    path = Path(asset.path)
    resolved = (path if path.is_absolute() else root / path).resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        return None
    return resolved


def cover_index(item: ContentItem) -> int | None:
    """封面在 media 列表里的下标：显式 cover 优先，否则第一张图。"""
    entries = (item.bundle_json or {}).get("media", []) or []
    assets = [MediaAsset.model_validate(entry) for entry in entries]
    for index, asset in enumerate(assets):
        if asset.cover:
            return index
    for index, asset in enumerate(assets):
        if asset.kind == "image":
            return index
    return None


def cover_asset(item: ContentItem) -> Path | None:
    """详情页封面缩略图的本地路径。"""
    index = cover_index(item)
    return None if index is None else local_media_path(item, index)


def cover_url(item: ContentItem) -> str | None:
    """封面的 HTTP 地址（复用既有的 ``GET /review/{id}/cover``）。

    没有可读的本地封面文件时返回 ``None``——前端据此显示占位图，而不是打一个必然 404
    的请求。JSON API **不另开**一套媒体端点，见 docs/WORKBENCH_API.md 的"媒体"一节。
    """
    return f"/review/{item.id}/cover" if cover_asset(item) is not None else None


def media_summary(item: ContentItem) -> dict[str, Any]:
    """列表页要的媒体摘要：几张图、几段视频、封面在哪。"""
    entries = (item.bundle_json or {}).get("media", []) or []
    kinds = [str(entry.get("kind") or "image") for entry in entries]
    return {
        "total": len(entries),
        "images": sum(1 for kind in kinds if kind == "image"),
        "videos": sum(1 for kind in kinds if kind == "video"),
        "kinds": kinds,
        "cover_index": cover_index(item),
    }


def slot_text(session: Session, item: ContentItem) -> str:
    """内容当前排期时刻的人类可读串（账号本地时区）。没排期返回空串。"""
    if item.scheduled_at is None:
        return ""
    from core.scheduling import format_slot

    account = session.get(Account, item.account_id)
    if account is None:  # pragma: no cover - 有外键
        return item.scheduled_at.strftime("%m-%d %H:%M UTC")
    return format_slot(item.scheduled_at, policy_of(account))


def account_windows(session: Session, item: ContentItem) -> str:
    """该账号的发布时段窗口文案（详情页解释"为什么排到这个点"）。"""
    account = session.get(Account, item.account_id)
    return "" if account is None else policy_of(account).window_text()


def latest_edit_diff(session: Session, item_id: str) -> str:
    """最近一次人工改稿的文本 diff（没有改过则空串）。"""
    log = session.scalars(
        select(ReviewLog)
        .where(ReviewLog.content_item_id == item_id, ReviewLog.action == "edit")
        .order_by(ReviewLog.at.desc())
        .limit(1)
    ).one_or_none()
    if log is None:
        return ""
    return diff_bundles(log.before_json, log.after_json)


def platform_extra_json(item: ContentItem) -> str:
    """``platform_extra`` 的美化 JSON（详情页原样展示）。"""
    return json.dumps(
        (item.bundle_json or {}).get("platform_extra", {}), ensure_ascii=False, indent=2
    )


__all__ = [
    "account_windows",
    "bundle_view",
    "cover_asset",
    "cover_index",
    "cover_url",
    "latest_edit_diff",
    "local_media_path",
    "media_summary",
    "needs_watch_confirm",
    "platform_extra_json",
    "slot_text",
]
