"""小红书发布器（P2）。

调用 `xpzouying/xiaohongshu-mcp`（Apache-2.0，Go 单二进制）sidecar 的 REST `/api/v1/*`。
**一账号一容器一 volume 一端口**（sidecar 为单进程单账号，cookies 存单一 `./data`），
compose 片段由 `scripts/gen_xhs_sidecars.py` 生成。

- :mod:`~publishers.xhs.client`：REST 薄封装 + 错误分类 + 宿主机→容器 素材路径映射
- :mod:`~publishers.xhs.publisher`：``XhsPublisher``，实现 P0 冻结契约 + 扫码登录通道
- :mod:`~publishers.xhs.login`：登录态巡检，落到 Account 状态机
- :mod:`~publishers.xhs.stub`：不联网的测试替身客户端
"""

from publishers.xhs.client import XhsMcpClient
from publishers.xhs.login import check_accounts, check_and_mark
from publishers.xhs.publisher import XhsPublisher, is_placeholder_post_id

__all__ = [
    "XhsMcpClient",
    "XhsPublisher",
    "check_accounts",
    "check_and_mark",
    "is_placeholder_post_id",
]
