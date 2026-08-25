"""发布器注册表：按 platform 取实现。

P0 阶段真实发布器尚未实现，默认注册 :class:`FakePublisher`（由 ``SW_USE_FAKE_PUBLISHERS``
控制）。P1/P2/P3 各自实现完成后调用 :func:`register` 覆盖对应平台即可，
调用方（scheduler / API）无需改动。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from publishers.base import (
    PLATFORMS,
    FakePublisher,
    PermanentError,
    Publisher,
    PublisherNotAvailable,
)

PublisherFactory = Callable[..., Publisher]

_REGISTRY: dict[str, PublisherFactory] = {}


def register(platform: str, factory: PublisherFactory) -> None:
    """注册（或覆盖）某平台的发布器工厂。"""
    if platform not in PLATFORMS:
        raise ValueError(f"未知平台: {platform}，允许值 {PLATFORMS}")
    _REGISTRY[platform] = factory


def unregister(platform: str) -> None:
    _REGISTRY.pop(platform, None)


def registered_platforms() -> list[str]:
    return sorted(_REGISTRY)


def get_publisher(platform: str, account_id: str, **kwargs: Any) -> Publisher:
    """取某平台某账号的发布器实例。**拿不到就抛** :class:`PublisherNotAvailable`。

    "拿不到"有两种，两种都归一到同一个异常：

    - 平台压根没注册（``KeyError``）。
    - 注册了，但**这个账号**构造不出来——小红书没配 ``sidecar_endpoint``、抖音没配
      profile 之类。工厂在构造阶段抛 :class:`PermanentError` 只可能是这一类
      （真正的发布失败要等 ``publish()`` 才谈得上），语义上就是"这个发布器对这个
      账号不可用"。

    **为什么必须归一**：``tick_scheduled_publish`` 只接 ``PublisherNotAvailable``
    并跳过该条内容。工厂的异常直接往外逃的话，**一个**账号配错就会打断整批发布，
    这一轮里所有账号都发不出去，而且 stats 一个数都拿不到——2026-08-24 验收run
    实测撞到过。
    """
    try:
        factory = _REGISTRY[platform]
    except KeyError as exc:
        raise PublisherNotAvailable(
            f"平台 {platform!r} 的发布器尚未注册（已注册: {registered_platforms()}）"
        ) from exc
    try:
        return factory(account_id, **kwargs)
    except PermanentError as exc:
        raise PublisherNotAvailable(
            f"平台 {platform!r} 的发布器对账号 {account_id!r} 不可用：{exc}"
        ) from exc


def _wechat_mp_factory(account_id: str, **kwargs: Any) -> Publisher:
    """公众号发布器工厂：按 ``WECHAT_BACKEND`` 选 官方 API / wenyan CLI 后端。

    延迟 import，避免 registry 被导入时就拉起 httpx / subprocess 相关模块。
    """
    from core.config import get_settings

    backend = kwargs.pop("backend", None) or get_settings().wechat_backend
    if backend == "wenyan":
        from publishers.wechat_mp.wenyan_backend import WenyanWechatMpPublisher

        return WenyanWechatMpPublisher(account_id, **kwargs)
    from publishers.wechat_mp.publisher import WechatMpPublisher

    return WechatMpPublisher(account_id, **kwargs)


def _xhs_factory(account_id: str, **kwargs: Any) -> Publisher:
    """小红书发布器工厂。

    sidecar 地址 / 鉴权 token / 日限频由 ``publishers.xhs.publisher.load_account_config``
    从 ``XHS_MCP_ENDPOINTS`` 与 accounts 表解析（token 只从环境变量来，绝不入库）。
    延迟 import，避免 registry 被导入时就拉起 httpx。
    """
    from publishers.xhs.publisher import XhsPublisher

    return XhsPublisher(account_id, **kwargs)


def _douyin_factory(account_id: str, **kwargs: Any) -> Publisher:
    """抖音发布器工厂。

    上传器地址 / identity_hint / 日限频由 ``publishers.douyin.publisher.load_account_config``
    从 ``DOUYIN_SERVICE_URL`` 与 accounts 表解析。延迟 import：抖音的浏览器层
    （patchright）只装在宿主机上，core 侧只 import ``client``/``publisher``，不碰 ``service``。
    """
    from publishers.douyin.publisher import DouyinPublisher

    return DouyinPublisher(account_id, **kwargs)


def register_builtin_publishers() -> None:
    """注册已实现的真实发布器。P1 公众号、P2 小红书、P3 抖音。

    模块导入时自动执行一次；``SW_USE_FAKE_PUBLISHERS=true`` 时
    :func:`use_fake_publishers` 会把它覆盖成 FakePublisher（本地联调 / 测试）。
    """
    register("wechat_mp", _wechat_mp_factory)
    register("xhs", _xhs_factory)
    register("douyin", _douyin_factory)


def _fake_factory(platform: str) -> PublisherFactory:
    def factory(account_id: str, **kwargs: Any) -> Publisher:
        kwargs.setdefault("platform", platform)
        return FakePublisher(account_id, **kwargs)

    return factory


def use_fake_publishers() -> None:
    """把三个平台全部指向 FakePublisher（P0 联调 / 测试用）。"""
    for platform in PLATFORMS:
        register(platform, _fake_factory(platform))


def reset_registry() -> None:
    _REGISTRY.clear()


__all__ = [
    "PublisherFactory",
    "get_publisher",
    "register",
    "register_builtin_publishers",
    "registered_platforms",
    "reset_registry",
    "unregister",
    "use_fake_publishers",
]

# 导入即注册真实发布器；FakePublisher 由 core.main / 测试显式覆盖
register_builtin_publishers()
