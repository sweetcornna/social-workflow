"""``DouyinServiceClient`` 的进程内假实现（不联网、不碰文件系统、不起浏览器）。

和 ``publishers/xhs/stub.py`` 同一定位：随代码发布的测试替身，让
``tests/contract/`` 在**非 dry_run** 模式下也能跑通 ``DouyinPublisher`` 的完整成功路径。

刻意模拟了真实上传器的两个关键行为：

1. ``publish`` **可能拿不到作品 id**（``resolve_post_id=False`` 时），
   于是发布器要落占位 id 并靠指标兜底修复；
2. 发布成功的作品会出现在 ``recent_posts()`` 里，
   所以"发布 → 对账"与"重试前先对账，命中就不重发"两条链路能被真正验证到。
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Any

from core.models import utcnow
from publishers.douyin.client import (
    STATE_OK,
    STATE_PUBLISHED,
    STATE_SCHEDULED,
    DouyinServiceClient,
    LoginState,
    PublishOutcome,
    RecentPost,
    mask_nickname,
    video_url,
)

STUB_BASE_URL = "http://127.0.0.1:8710"
STUB_NICKNAME = "抖音 Stub 账号"


def _fake_post_id(seed: str) -> str:
    """造一个像 aweme_id 的 19 位数字串。"""
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return "7" + str(int(digest[:24], 16))[:18]


class StubDouyinServiceClient(DouyinServiceClient):
    """所有网络方法都返回确定性假数据，并记录调用次数。"""

    def __init__(
        self,
        *,
        logged_in: bool = True,
        nickname: str = STUB_NICKNAME,
        posts: list[RecentPost] | None = None,
        dry_run: bool = False,
        resolve_post_id: bool = True,
        appear_after_publish: bool = True,
        metrics: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(STUB_BASE_URL, dry_run=dry_run, account_id="acc-stub")
        self.logged_in = logged_in
        self.nickname = nickname
        self.posts: list[RecentPost] = list(posts or [])
        # False 模拟"发出去了但内容管理页还没刷出来"，用来验证占位 id + 指标兜底链路
        self.resolve_post_id = resolve_post_id
        self.appear_after_publish = appear_after_publish
        self.metrics_payload = metrics or {
            "available": True,
            "views": 1234,
            "likes": 56,
            "comments": 7,
            "shares": 8,
        }
        self.calls: dict[str, int] = {}
        self.published: list[dict[str, Any]] = []
        self.sms_codes: list[str] = []

    # -- 记账 -------------------------------------------------------------

    def _note(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    # -- 覆写网络方法 ------------------------------------------------------

    def health(self) -> dict[str, Any]:
        self._note("health")
        return {"ok": True, "state": STATE_OK, "service": "douyin-uploader", "version": "stub"}

    def login_status(self) -> LoginState:
        self._note("login_status")
        if not self.logged_in:
            return LoginState(state="logged_out", detail="stub：未登录")
        return LoginState(
            state="logged_in",
            nickname=mask_nickname(self.nickname),
            detail=f"已登录：{mask_nickname(self.nickname)}",
        )

    def start_login(self, *, identity_hint: str = "") -> dict[str, Any]:
        self._note("start_login")
        if self.logged_in:
            return {"ok": True, "state": "logged_in", "nickname": mask_nickname(self.nickname)}
        return {"ok": True, "state": "waiting_user", "detail": "stub：已打开登录窗口"}

    def submit_sms_code(self, code: str) -> dict[str, Any]:
        self._note("submit_sms_code")
        self.sms_codes.append(code)
        return {"ok": True, "state": STATE_OK, "detail": "stub：验证码已填入"}

    # 测试替身整体绕过文件系统：契约测试的素材路径是不存在的占位路径
    def resolve_video(self, path: str) -> str:
        self._note("resolve_video")
        return path

    def resolve_cover(self, path: str) -> str:
        self._note("resolve_cover")
        return path

    def publish(
        self,
        *,
        title: str,
        description: str,
        video_path: str,
        hashtags: list[str] | None = None,
        cover_path: str = "",
        schedule_at: str = "",
        identity_hint: str = "",
    ) -> PublishOutcome:
        self._note("publish")
        request = {
            "title": title,
            "description": description,
            "video_path": video_path,
            "hashtags": list(hashtags or []),
            "cover_path": cover_path,
            "schedule_at": schedule_at,
            "identity_hint": identity_hint,
        }
        self.published.append(request)
        if self.dry_run:
            return PublishOutcome(state="dry_run", raw={"dry_run": True, **request})
        post_id = _fake_post_id(f"{title}|{len(self.published)}") if self.resolve_post_id else ""
        if self.appear_after_publish:
            self.posts.insert(
                0,
                RecentPost(
                    title=title,
                    post_id=post_id,
                    url=video_url(post_id) if post_id else "",
                    published_at=utcnow() - timedelta(seconds=5),
                    raw_time="刚刚",
                ),
            )
        return PublishOutcome(
            state=STATE_SCHEDULED if schedule_at else STATE_PUBLISHED,
            post_id=post_id,
            url=video_url(post_id) if post_id else "",
            screenshot_path="/stub/screenshots/publish-done.png",
        )

    def recent_posts(self, *, limit: int = 20) -> list[RecentPost]:
        self._note("recent_posts")
        return list(self.posts[:limit])

    def metrics(self, post_id: str) -> dict[str, Any]:
        self._note("metrics")
        known = {p.post_id for p in self.posts if p.post_id}
        if post_id not in known:
            return {
                "ok": True,
                "state": STATE_OK,
                "metrics": {"available": False, "reason": f"stub：不认识作品 {post_id}"},
            }
        return {"ok": True, "state": STATE_OK, "metrics": dict(self.metrics_payload)}


__all__ = ["STUB_BASE_URL", "STUB_NICKNAME", "StubDouyinServiceClient"]
