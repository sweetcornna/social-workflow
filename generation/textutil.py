"""生成链共用的纯文本工具。

这些函数被公众号链（``wechat_article``）与小红书链（``xhs_note``）共用。
单独成模块而不是让一条链去 import 另一条链——两条链是平级的，
互相 import 会让"改公众号的东西顺手弄坏小红书"变得容易。
"""

from __future__ import annotations

import re

from generation.llm import Usage

_CODE_FENCE = re.compile(r"^\s*```[a-zA-Z]*\s*\n(.*?)\n\s*```\s*$", re.DOTALL)
_H1 = re.compile(r"^#\s+.*$", re.MULTILINE)
#: 零宽字符：模型偶尔会吐出来，会让字数统计与平台侧不一致
_ZERO_WIDTH = re.compile("[\\u200b-\\u200f\\u2060\\ufeff]")
#: 话题标签里不允许出现的字符（小红书与抖音都不支持，带上去话题会创建失败）
_TAG_BANNED = re.compile(r"[#＃\s,，、;；/\\|]+")


def strip_code_fence(text: str) -> str:
    """模型偶尔会把整段产出包在 ``` 里，剥掉。"""
    match = _CODE_FENCE.match(text.strip())
    return match.group(1).strip() if match else text.strip()


def clean_body(text: str) -> str:
    """正文清洗：剥代码围栏、去掉一级标题（标题单独产出，正文里重复是冗余）。"""
    body = strip_code_fence(text)
    body = _H1.sub("", body).strip()
    # 连续三个以上空行压成两个
    return re.sub(r"\n{3,}", "\n\n", body)


def truncate(text: str, limit: int) -> str:
    """按字符数截断并压掉多余空白。中文按字算，和平台一致。"""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def strip_zero_width(text: str) -> str:
    """删零宽字符。留着会让本地字数与平台侧对不上，也是常见的过审规避手法。"""
    return _ZERO_WIDTH.sub("", text)


def clean_tags(raw: list[str], *, limit: int, max_chars: int) -> list[str]:
    """清洗话题标签：去 ``#``、去空白与分隔符、截断、去重、限量。

    小红书与抖音共用。去重按**原样**比较而不是忽略大小写：中文标签不存在大小写问题，
    而英文标签 ``OOTD`` 与 ``ootd`` 在平台上确实是两个话题。
    """
    cleaned: list[str] = []
    for item in raw:
        tag = _TAG_BANNED.sub("", strip_zero_width(str(item))).strip()
        if not tag:
            continue
        tag = tag[:max_chars]
        if tag not in cleaned:
            cleaned.append(tag)
    return cleaned[:limit]


def accumulate_usage(total: Usage, add: Usage) -> Usage:
    """累加多次调用的 token 用量。"""
    return Usage(
        input_tokens=total.input_tokens + add.input_tokens,
        output_tokens=total.output_tokens + add.output_tokens,
        cache_read_input_tokens=total.cache_read_input_tokens + add.cache_read_input_tokens,
        cache_creation_input_tokens=(
            total.cache_creation_input_tokens + add.cache_creation_input_tokens
        ),
    )


__all__ = [
    "accumulate_usage",
    "clean_body",
    "clean_tags",
    "strip_code_fence",
    "strip_zero_width",
    "truncate",
]
