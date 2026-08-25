"""``XhsMcpClient`` 的进程内假实现（不联网），供契约测试与本地联调使用。

和 ``publishers.wechat_mp.stub.StubWechatMpClient`` 同一定位：随代码发布的测试替身，
让 ``tests/contract/`` 在**非 dry_run** 模式下也能跑通 ``XhsPublisher`` 的完整成功路径。

它刻意模拟了真实 sidecar 的两个关键行为：

1. ``publish_content`` 的返回体里**没有** note_id；
2. 发布后的笔记会出现在 ``my_notes()`` 里（带 ``id`` / ``xsecToken`` / ``interactInfo``），
   所以 ``XhsPublisher`` 的"发布 → 对账取 id"链路能被真正验证到。
"""

from __future__ import annotations

import hashlib
from typing import Any

from publishers.xhs.client import LoginStatus, QrcodeInfo, XhsMcpClient

STUB_BASE_URL = "http://xhs-sidecar.invalid:18060"

# 一张 1x1 的 PNG（base64），当作二维码占位；契约测试要求 base64 解得出合法 PNG
TINY_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _fake_id(prefix: str, seed: str) -> str:
    return f"{prefix}{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:20]}"


def make_feed(
    note_id: str,
    title: str,
    *,
    note_type: str = "normal",
    liked: str = "12",
    collected: str = "3",
    comments: str = "1",
    shared: str = "0",
    xsec_token: str = "stub-xsec-token",
) -> dict[str, Any]:
    """按上游 ``xiaohongshu.Feed`` 的形状造一条主页笔记。"""
    return {
        "id": note_id,
        "xsecToken": xsec_token,
        "modelType": "note",
        "noteCard": {
            "type": note_type,
            "displayTitle": title,
            "user": {"userId": "stub-user", "nickname": "小红书 Stub 账号"},
            "interactInfo": {
                "liked": False,
                "likedCount": liked,
                "collectedCount": collected,
                "commentCount": comments,
                "sharedCount": shared,
            },
            "cover": {"url": f"https://sns-img.invalid/{note_id}.jpg"},
        },
    }


class StubXhsMcpClient(XhsMcpClient):
    """所有网络方法都返回确定性假数据，并记录调用次数。"""

    def __init__(
        self,
        *,
        logged_in: bool = True,
        notes: list[dict[str, Any]] | None = None,
        note_times: dict[str, int] | None = None,
        dry_run: bool = False,
        appear_after_publish: bool = True,
    ) -> None:
        super().__init__(STUB_BASE_URL, dry_run=dry_run, account_id="acc-stub")
        self.logged_in = logged_in
        # False 模拟"发出去了但主页上还没刷出来"，用来验证占位 id + 指标兜底回填链路
        self.appear_after_publish = appear_after_publish
        self.notes: list[dict[str, Any]] = list(notes or [])
        # note_id -> 发布时间（毫秒时间戳），供 _newest() 在标题重名时挑最新的
        self.note_times: dict[str, int] = dict(note_times or {})
        self.calls: dict[str, int] = {}
        self.published: list[dict[str, Any]] = []

    # -- 记账 -------------------------------------------------------------

    def _note(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    # -- 覆写网络方法 ------------------------------------------------------

    def health(self) -> dict[str, Any]:
        self._note("health")
        return {"status": "healthy", "service": "xiaohongshu-mcp", "version": "stub"}

    def login_status(self) -> LoginStatus:
        self._note("login_status")
        return LoginStatus(
            is_logged_in=self.logged_in,
            username="小红书 Stub 账号" if self.logged_in else "",
            user_id="stub-user" if self.logged_in else "",
            raw={"is_logged_in": self.logged_in},
        )

    def get_login_qrcode(self) -> QrcodeInfo:
        self._note("get_login_qrcode")
        return QrcodeInfo(
            image_base64=TINY_PNG_BASE64,
            timeout_seconds=240.0,
            is_logged_in=self.logged_in,
            raw={"timeout": "4m0s"},
        )

    # 测试替身整体绕过文件系统：契约测试的素材路径是不存在的占位路径
    def resolve_images(self, paths: list[str]) -> list[str]:
        self._note("resolve_images")
        return list(paths)

    def resolve_video(self, path: str) -> str:
        self._note("resolve_video")
        return path

    def publish_content(
        self,
        *,
        title: str,
        content: str,
        images: list[str],
        tags: list[str] | None = None,
        schedule_at: str | None = None,
        is_original: bool = False,
        visibility: str = "",
        products: list[str] | None = None,
    ) -> dict[str, Any]:
        self._note("publish_content")
        request = {
            "title": title,
            "content": content,
            "images": list(images),
            "tags": list(tags or []),
            "schedule_at": schedule_at,
            "is_original": is_original,
            "visibility": visibility,
        }
        self.published.append(request)
        if self.dry_run:
            return {"dry_run": True, "title": title, "images": len(images), "status": "dry_run"}
        if not schedule_at and self.appear_after_publish:
            # 真实 sidecar 发布后笔记会出现在主页；定时笔记则还在"待发布"，主页看不到
            note_id = _fake_id("stub-note-", f"{title}|{len(self.notes)}")
            self.notes.insert(0, make_feed(note_id, title))
            self.note_times[note_id] = 1_760_000_000_000 + len(self.notes)
        return {"title": title, "content": content, "images": len(images), "status": "发布完成"}

    def publish_video(
        self,
        *,
        title: str,
        content: str,
        video: str,
        tags: list[str] | None = None,
        schedule_at: str | None = None,
        visibility: str = "",
        products: list[str] | None = None,
    ) -> dict[str, Any]:
        self._note("publish_video")
        self.published.append({"title": title, "video": video, "schedule_at": schedule_at})
        if self.dry_run:
            return {"dry_run": True, "title": title, "video": video, "status": "dry_run"}
        if not schedule_at and self.appear_after_publish:
            note_id = _fake_id("stub-video-", f"{title}|{len(self.notes)}")
            self.notes.insert(0, make_feed(note_id, title, note_type="video"))
            self.note_times[note_id] = 1_760_000_000_000 + len(self.notes)
        return {"title": title, "content": content, "video": video, "status": "发布完成"}

    def my_profile(self, *, tab: str = "") -> dict[str, Any]:
        self._note("my_profile")
        return {
            "userBasicInfo": {"nickname": "小红书 Stub 账号", "redId": "stub-red-id"},
            "interactions": [
                {"type": "follows", "name": "关注", "count": "12"},
                {"type": "fans", "name": "粉丝", "count": "345"},
            ],
            "feeds": list(self.notes),
        }

    def user_profile(self, user_id: str, xsec_token: str, *, tab: str = "") -> dict[str, Any]:
        self._note("user_profile")
        return self.my_profile()

    def note_detail(
        self, feed_id: str, xsec_token: str, *, load_all_comments: bool = False
    ) -> dict[str, Any]:
        self._note("note_detail")
        for note in self.notes:
            if note.get("id") == feed_id:
                card = note["noteCard"]
                return {
                    "note": {
                        "noteId": feed_id,
                        "xsecToken": xsec_token,
                        "title": card.get("displayTitle", ""),
                        "desc": "stub 正文",
                        "type": card.get("type", "normal"),
                        "time": self.note_times.get(feed_id, 0),
                        "interactInfo": dict(card.get("interactInfo") or {}),
                    },
                    "comments": {"list": [], "cursor": "", "hasMore": False},
                }
        return {"note": {}, "comments": {"list": []}}


__all__ = ["STUB_BASE_URL", "TINY_PNG_BASE64", "StubXhsMcpClient", "make_feed"]
