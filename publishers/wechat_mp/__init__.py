"""微信公众号发布器（P1）。

- :mod:`~publishers.wechat_mp.client`：官方 API 的 httpx 薄封装
  （``stable_token`` / ``material/add_material`` / ``media/uploadimg`` /
  ``draft/*`` / ``freepublish/*`` / ``datacube/*``），以 ``wechatpy``(MIT) 为参考实现。
- :mod:`~publishers.wechat_mp.publisher`：``WechatMpPublisher``，实现 P0 冻结契约。
- :mod:`~publishers.wechat_mp.wenyan_backend`：``@wenyan-md/cli`` 备选后端。
- :mod:`~publishers.wechat_mp.stub`：不联网的测试替身客户端。

未认证主体只落草稿箱，由人工在公众号后台点发表（2025-07 官方回收 freepublish 权限）。
"""

from publishers.wechat_mp.client import TOKEN_CACHE, WechatMpClient
from publishers.wechat_mp.publisher import WechatMpPublisher, mark_confirm_publish

__all__ = [
    "TOKEN_CACHE",
    "WechatMpClient",
    "WechatMpPublisher",
    "mark_confirm_publish",
]
