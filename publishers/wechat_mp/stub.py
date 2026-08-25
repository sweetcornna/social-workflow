"""``WechatMpClient`` 的进程内假实现（不联网），供契约测试与本地联调使用。

和 ``publishers.base.FakePublisher`` 同一定位：随代码发布的测试替身，
让 ``tests/contract/`` 能在**非 dry_run** 模式下也跑通 ``WechatMpPublisher``
的完整成功路径（dry_run 只覆盖 ok=False 分支）。
"""

from __future__ import annotations

import hashlib
from typing import Any

from publishers.wechat_mp.client import WechatMpClient


def _fake_id(prefix: str, seed: str) -> str:
    return f"{prefix}-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:16]}"


class StubWechatMpClient(WechatMpClient):
    """所有网络方法都返回确定性假数据，并记录调用次数。"""

    def __init__(
        self,
        *,
        drafts: list[dict[str, Any]] | None = None,
        published: list[dict[str, Any]] | None = None,
        article_rows: list[dict[str, Any]] | None = None,
        publish_statuses: list[int] | None = None,
    ) -> None:
        super().__init__("stub-appid", "stub-secret", base_url="https://stub.invalid")
        self.drafts = drafts if drafts is not None else []
        self.published = published if published is not None else []
        self.article_rows = article_rows if article_rows is not None else []
        # 依次返回的 publish_status，用尽后固定返回最后一个
        self.publish_statuses = publish_statuses or [0]
        self._status_cursor = 0
        self.calls: dict[str, int] = {}

    # -- 记账 -------------------------------------------------------------

    def _note(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    # -- 覆写网络方法 ------------------------------------------------------

    @property
    def has_credentials(self) -> bool:
        return True

    def get_access_token(self, *, force_refresh: bool = False) -> str:
        self._note("get_access_token")
        return "STUB-ACCESS-TOKEN"

    def upload_image_for_article(self, path: Any) -> str:
        self._note("upload_image_for_article")
        return f"https://mmbiz.qpic.cn/stub/{_fake_id('img', str(path))}/640"

    def add_material_image(self, path: Any) -> str:
        self._note("add_material_image")
        return _fake_id("thumb", str(path))

    def draft_add(self, articles: list[dict[str, Any]]) -> str:
        self._note("draft_add")
        media_id = _fake_id("draft", articles[0].get("title", ""))
        self.drafts.append({"media_id": media_id, "content": {"news_item": list(articles)}})
        return media_id

    def draft_get(self, media_id: str) -> dict[str, Any]:
        self._note("draft_get")
        for entry in self.drafts:
            if entry["media_id"] == media_id:
                return {"news_item": entry["content"]["news_item"]}
        return {"news_item": []}

    def draft_batchget(
        self, *, offset: int = 0, count: int = 20, no_content: int = 0
    ) -> dict[str, Any]:
        self._note("draft_batchget")
        page = self.drafts[offset : offset + count]
        return {"total_count": len(self.drafts), "item_count": len(page), "item": page}

    def freepublish_submit(self, media_id: str) -> str:
        self._note("freepublish_submit")
        return _fake_id("publish", media_id)

    def freepublish_get(self, publish_id: str) -> dict[str, Any]:
        self._note("freepublish_get")
        index = min(self._status_cursor, len(self.publish_statuses) - 1)
        status = self.publish_statuses[index]
        self._status_cursor += 1
        return {
            "publish_id": publish_id,
            "publish_status": status,
            "article_id": _fake_id("article", publish_id),
            "article_detail": {
                "count": 1,
                "item": [{"idx": 1, "article_url": f"https://mp.weixin.qq.com/s/{publish_id}"}],
            },
        }

    def freepublish_batchget(
        self, *, offset: int = 0, count: int = 20, no_content: int = 0
    ) -> dict[str, Any]:
        self._note("freepublish_batchget")
        page = self.published[offset : offset + count]
        return {"total_count": len(self.published), "item_count": len(page), "item": page}

    def freepublish_getarticle(self, article_id: str) -> dict[str, Any]:
        self._note("freepublish_getarticle")
        return {"news_item": []}

    def datacube_getarticletotal(self, begin_date: str, end_date: str) -> list[dict[str, Any]]:
        self._note("datacube_getarticletotal")
        return list(self.article_rows)

    def datacube_getarticlesummary(self, begin_date: str, end_date: str) -> list[dict[str, Any]]:
        self._note("datacube_getarticlesummary")
        return []

    def datacube_getusersummary(self, begin_date: str, end_date: str) -> list[dict[str, Any]]:
        self._note("datacube_getusersummary")
        return []


__all__ = ["StubWechatMpClient"]
