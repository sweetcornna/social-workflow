"""共享的指标 payload 分类契约。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import StrEnum
from typing import Any


class MetricsPayloadKind(StrEnum):
    """发布器或历史 JSON 的指标 payload 分类。"""

    USABLE = "usable"
    EXPLICITLY_UNAVAILABLE = "explicitly_unavailable"
    MALFORMED = "malformed"


_JSON_SCALAR_TYPES = (type(None), str, bool, int, float)


def _copy_json_value(value: object, active: set[int]) -> object:
    """把受信任边界外的 JSON 值复制成内建对象。

    此函数有意只以 ``items()`` 读取 Mapping，且在递归前将每层容器完整物化。
    ``active`` 是当前递归路径而非全局 seen 集合，因此共享引用合法，只有环被拒绝。
    """

    if isinstance(value, Mapping):
        value_id = id(value)
        if value_id in active:
            raise ValueError("cyclic metrics payload")
        active.add(value_id)
        try:
            # 只调用一次源 Mapping.items()；list 同时完整快照 view/iterator 和 pair。
            raw_pairs = list(value.items())
            stable_pairs: list[tuple[str, object]] = []
            seen_keys: set[str] = set()
            for pair in raw_pairs:
                # 不解包源 pair，以免多次迭代自定义 pair；list 也会触发其 length hint。
                pair_values = list(pair)
                if len(pair_values) != 2:
                    raise ValueError("metrics mapping pair must contain two values")
                key, child = pair_values
                if type(key) is not str or key in seen_keys:
                    raise ValueError("metrics mapping keys must be unique exact strings")
                seen_keys.add(key)
                stable_pairs.append((key, child))

            copied: dict[str, Any] = {}
            for key, child in stable_pairs:
                copied[key] = _copy_json_value(child, active)
            return copied
        finally:
            active.remove(value_id)

    if isinstance(value, (list, tuple)):
        value_id = id(value)
        if value_id in active:
            raise ValueError("cyclic metrics payload")
        active.add(value_id)
        try:
            # tuple 在安全副本中统一变成 JSON array/list。
            children = list(value)
            return [_copy_json_value(child, active) for child in children]
        finally:
            active.remove(value_id)

    if type(value) in _JSON_SCALAR_TYPES:
        return value
    raise TypeError("metrics payload scalar is not an exact JSON builtin")


def normalize_metrics_payload(
    metrics: object,
) -> tuple[MetricsPayloadKind, dict[str, Any] | None]:
    """严格验证并归一化 publisher payload，且不把内容带进异常。

    只接收顶层 Mapping。所有容器先复制到普通 ``dict``/``list``，再让 JSON 编码器
    接触副本；因此攻击性 Mapping、key、迭代器或 scalar 对象不会进入 serializer、
    SQLAlchemy 或后续的 ``get``。协议操作的普通异常统一折为 malformed；
    :class:`BaseException`（例如 Ctrl-C）仍按原样传播。
    """

    try:
        if not isinstance(metrics, Mapping):
            return MetricsPayloadKind.MALFORMED, None
        copied = _copy_json_value(metrics, set())
        # 顶层分支已确认是 Mapping；这里保留内建检查，防止未来 helper 被错误改动。
        if type(copied) is not dict:
            return MetricsPayloadKind.MALFORMED, None
        encoded = json.dumps(copied, allow_nan=False)
        normalized = json.loads(encoded)
        if type(normalized) is not dict:
            return MetricsPayloadKind.MALFORMED, None
        # 此时是内建 dict，唯一允许调用 get 的位置。
        if normalized.get("available") is False:
            return MetricsPayloadKind.EXPLICITLY_UNAVAILABLE, normalized
        return MetricsPayloadKind.USABLE, normalized
    except Exception:
        return MetricsPayloadKind.MALFORMED, None


def classify_metrics_payload(metrics: object) -> MetricsPayloadKind:
    """按 P22 契约分类，不让坏历史 JSON 或 publisher 对象破坏整批读取。"""

    kind, _normalized = normalize_metrics_payload(metrics)
    return kind


def is_metrics_usable(metrics: object) -> bool:
    """兼容读模型使用的可用性断言。"""

    return classify_metrics_payload(metrics) is MetricsPayloadKind.USABLE


__all__ = [
    "MetricsPayloadKind",
    "classify_metrics_payload",
    "is_metrics_usable",
    "normalize_metrics_payload",
]
