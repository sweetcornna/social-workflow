"""词面硬过滤：``konsheng/Sensitive-lexicon``（MIT）词库 + Aho-Corasick 多模匹配。

为什么自己写 Aho-Corasick 而不是 `pip install pyahocorasick`：
后者是 C 扩展，装起来要编译，而我们的规模（十万级词、万字级文本）用纯 Python
自动机完全够——建机一次 O(总词长)，扫描 O(文本长)，不随词数增长。
换成朴素 `for word in words: if word in text` 会是 O(词数 × 文本长)，
十万词时单篇文章要几秒，接受不了。

词库不进 git（`零时-Tencent.txt` 一个就 700KB+），由 ``scripts/fetch_lexicon.py``
下载到 ``data/lexicon/``。**没下载时不报错**，退化到内置的极小兜底词表，
保证离线单测与首次运行不炸——但会在 findings 里留一条 info 说明词库缺失。
"""

from __future__ import annotations

import logging
import re
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from review.base import Finding, excerpt_around

logger = logging.getLogger("social_workflow.review.lexicon")

#: 上游词库
LEXICON_UPSTREAM = "https://github.com/konsheng/Sensitive-lexicon"
LEXICON_LICENSE = "MIT"
#: 词表所在子目录（上游仓库结构）
LEXICON_SUBDIR = "Vocabulary"

#: 词表文件 → 审核等级。上游只给词不给等级，这里按类目映射。
#: 政治 / 反动 / 暴恐 / 涉枪涉爆 / 色情属于硬红线；广告、民生等更适合人工判断。
CATEGORY_LEVELS: dict[str, str] = {
    "政治类型": "block",
    "反动词库": "block",
    "暴恐词库": "block",
    "涉枪涉爆": "block",
    "色情类型": "block",
    "色情词库": "block",
    "贪腐词库": "warn",
    "民生词库": "warn",
    "广告类型": "warn",
    "新思想启蒙": "warn",
    "COVID-19词库": "warn",
    "其他词库": "warn",
    "补充词库": "warn",
    "GFW补充词库": "warn",
    "网易前端过滤敏感词库": "warn",
    "零时-Tencent": "warn",
}

#: 明确排除的文件：这个是**域名**清单，拿去做子串匹配会把正常文本里的
#: "com"、"cn" 之类误伤，必须单独按 URL 匹配（本模块暂不处理）。
EXCLUDED_FILES = frozenset({"非法网址"})

#: 词库缺失时的兜底：只保留最不可能误伤、又确实不能发的几类。
#: 这不是"审核能力"，只是让链路在无词库时仍然连通。
FALLBACK_WORDS: dict[str, tuple[str, ...]] = {
    "政治类型": ("颠覆国家政权", "煽动分裂国家"),
    "暴恐词库": ("制造炸弹", "恐怖袭击"),
    "涉枪涉爆": ("出售枪支", "买卖军火"),
    "色情类型": ("裸聊", "招嫖"),
}

#: 匹配前做的归一化：全角→半角由调用方决定，这里只处理零宽字符与重复空白。
#: 常见规避手法是往词里插零宽空格 / 软连字符。
_INVISIBLE = re.compile(r"[​-‏‪-‮⁠﻿\xad]")


def normalize_for_match(text: str) -> tuple[str, list[int]]:
    """删掉零宽字符并返回 ``(净化文本, 每个字符在原文中的下标)``。

    保留下标映射，命中位置才能回指到**原文**——否则给人工看的 excerpt 会错位。
    """
    cleaned: list[str] = []
    offsets: list[int] = []
    for index, char in enumerate(text):
        if _INVISIBLE.match(char):
            continue
        cleaned.append(char)
        offsets.append(index)
    return "".join(cleaned), offsets


@dataclass
class _Node:
    children: dict[str, int] = field(default_factory=dict)
    fail: int = 0
    #: 在本节点结束的词（词, 类目）
    outputs: list[tuple[str, str]] = field(default_factory=list)


class AhoCorasick:
    """多模式串匹配自动机。大小写不敏感（统一转小写后建机与匹配）。"""

    def __init__(self) -> None:
        self._nodes: list[_Node] = [_Node()]
        self._built = False
        self._word_count = 0

    def __len__(self) -> int:
        return self._word_count

    def add(self, word: str, category: str) -> None:
        word = word.strip().lower()
        if not word:
            return
        node = 0
        for char in word:
            nxt = self._nodes[node].children.get(char)
            if nxt is None:
                self._nodes.append(_Node())
                nxt = len(self._nodes) - 1
                self._nodes[node].children[char] = nxt
            node = nxt
        self._nodes[node].outputs.append((word, category))
        self._word_count += 1
        self._built = False

    def build(self) -> None:
        """构建 fail 指针（BFS）。"""
        queue: deque[int] = deque()
        root = self._nodes[0]
        for child in root.children.values():
            self._nodes[child].fail = 0
            queue.append(child)
        while queue:
            current = queue.popleft()
            node = self._nodes[current]
            for char, child in node.children.items():
                fail = node.fail
                while fail and char not in self._nodes[fail].children:
                    fail = self._nodes[fail].fail
                target = self._nodes[fail].children.get(char, 0)
                self._nodes[child].fail = target if target != child else 0
                # 合并 fail 链上的输出，扫描时就不必再回溯
                self._nodes[child].outputs.extend(self._nodes[self._nodes[child].fail].outputs)
                queue.append(child)
        self._built = True

    def iter_matches(self, text: str) -> Iterator[tuple[int, int, str, str]]:
        """产出 ``(start, end, word, category)``，下标是**传入文本**的下标。"""
        if not self._built:
            self.build()
        node = 0
        lowered = text.lower()
        for index, char in enumerate(lowered):
            while node and char not in self._nodes[node].children:
                node = self._nodes[node].fail
            node = self._nodes[node].children.get(char, 0)
            if node == 0:
                continue
            for word, category in self._nodes[node].outputs:
                start = index - len(word) + 1
                yield start, index + 1, word, category


@dataclass
class Lexicon:
    """加载好的词库 + 自动机。"""

    automaton: AhoCorasick
    categories: dict[str, int]
    #: True 表示用的是内置兜底词表，不是真词库
    is_fallback: bool = False
    source_dir: Path | None = None

    @property
    def word_count(self) -> int:
        return len(self.automaton)


def _iter_word_files(directory: Path) -> Iterable[Path]:
    if not directory.is_dir():
        return []
    # 上游把词表放在 Vocabulary/ 下；也兼容"直接把 txt 放在根目录"的手工布局
    sub = directory / LEXICON_SUBDIR
    root = sub if sub.is_dir() else directory
    return sorted(p for p in root.glob("*.txt") if p.stem not in EXCLUDED_FILES)


def _read_words(path: Path) -> Iterator[str]:
    """一行一词，UTF-8。上游存在空行与重复词，这里过滤空行，重复交给自动机。"""
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            word = line.strip()
            # 单字词误伤率极高（"死"、"枪"），跳过
            if len(word) >= 2:
                yield word


def load_lexicon(
    directory: str | Path | None = None,
    *,
    categories: Iterable[str] | None = None,
    max_words_per_file: int | None = None,
) -> Lexicon:
    """加载词库。目录不存在或为空时退化到 :data:`FALLBACK_WORDS`。

    ``max_words_per_file`` 供测试限制规模；生产留空。
    """
    from core.config import get_settings

    path = Path(directory) if directory is not None else Path(get_settings().lexicon_dir)
    wanted = set(categories) if categories is not None else None

    automaton = AhoCorasick()
    counts: dict[str, int] = {}
    files = list(_iter_word_files(path))
    for file in files:
        category = file.stem
        if wanted is not None and category not in wanted:
            continue
        loaded = 0
        for word in _read_words(file):
            if max_words_per_file is not None and loaded >= max_words_per_file:
                break
            automaton.add(word, category)
            loaded += 1
        if loaded:
            counts[category] = counts.get(category, 0) + loaded

    if counts:
        automaton.build()
        logger.info("词库加载完成: %d 类 %d 词（%s）", len(counts), len(automaton), path)
        return Lexicon(automaton=automaton, categories=counts, source_dir=path)

    logger.warning("词库目录 %s 为空，退化到内置兜底词表（覆盖极窄）", path)
    for category, words in FALLBACK_WORDS.items():
        if wanted is not None and category not in wanted:
            continue
        for word in words:
            automaton.add(word, category)
            counts[category] = counts.get(category, 0) + 1
    automaton.build()
    return Lexicon(automaton=automaton, categories=counts, is_fallback=True, source_dir=path)


def level_for(category: str) -> str:
    return CATEGORY_LEVELS.get(category, "warn")


def _is_ascii_alnum(ch: str) -> bool:
    return ch.isascii() and ch.isalnum()


def _boundary_ok(text: str, start: int, end: int, word: str) -> bool:
    """拉丁词条必须落在词边界上；中日韩词条不受此限。

    **为什么只管拉丁**：中文没有分词边界，"赌博"藏在"这是一个赌博网站"里就该报，
    子串匹配是对的。拉丁不是——上游词库里有 76 条纯 ASCII 短词（``ma`` ``64``
    ``AV`` ``BJ`` ``CBD`` ``CCTV``…），拿它们做子串匹配必然误报：``ma`` 藏在
    ``minimalist`` / ``format`` / ``many`` 里，``av`` 藏在 ``available`` 里。而本仓的
    配图 prompt 一律是英文，2026-08-24 装上完整词库后**每条稿子**都因此挂一条 warn，
    autopilot（判据是 block 0 且 warn 0）于是静默停在 draft。

    判据只看**紧邻的那一个字符**：词首是 ASCII 字母数字时，它前面不许还是 ASCII
    字母数字；词尾同理。等价于 ``\b``，但只在该用的那一侧用。

    它**治不了语义误报**：``在 CBD 上班`` 里 CBD 确实独立成词，照样命中——
    "这个词在语境里其实无害"归 ``llm_semantic`` 那一档管，不是这里。
    """
    if not word:
        return True
    if _is_ascii_alnum(word[0]) and start > 0 and _is_ascii_alnum(text[start - 1]):
        return False
    return not (_is_ascii_alnum(word[-1]) and end < len(text) and _is_ascii_alnum(text[end]))


def scan(text: str, lexicon: Lexicon) -> list[Finding]:
    """扫描文本，产出 findings。同一个词只报一次（取首次命中位置）。"""
    if not text:
        return []
    cleaned, offsets = normalize_for_match(text)
    seen: set[str] = set()
    findings: list[Finding] = []
    for start, end, word, category in lexicon.automaton.iter_matches(cleaned):
        if not _boundary_ok(cleaned, start, end, word):
            continue
        if word in seen:
            continue
        seen.add(word)
        # 映射回原文下标
        raw_start = offsets[start] if start < len(offsets) else start
        raw_end = offsets[end - 1] + 1 if end - 1 < len(offsets) else end
        findings.append(
            Finding(
                level=level_for(category),  # type: ignore[arg-type]
                rule=f"lexicon.{category}",
                excerpt=excerpt_around(text, raw_start, raw_end),
                suggestion=f"删除或改写命中词「{word}」",
                stage="lexicon",
                start=raw_start,
                end=raw_end,
                extra={"word": word, "category": category},
            )
        )
    if lexicon.is_fallback:
        findings.append(
            Finding(
                level="info",
                rule="lexicon.not_installed",
                excerpt="",
                suggestion=(
                    "敏感词库未安装，当前只用了内置兜底词表（覆盖极窄）。"
                    "执行 `uv run python scripts/fetch_lexicon.py` 下载完整词库。"
                ),
                stage="lexicon",
            )
        )
    return findings


_CACHE: dict[tuple[str, str | None], Lexicon] = {}


def get_lexicon(directory: str | Path | None = None) -> Lexicon:
    """带进程内缓存的加载（词库十万词级，别每篇文章重建一次自动机）。"""
    from core.config import get_settings

    path = str(directory) if directory is not None else get_settings().lexicon_dir
    key = (path, None)
    if key not in _CACHE:
        _CACHE[key] = load_lexicon(path)
    return _CACHE[key]


def clear_cache() -> None:
    _CACHE.clear()


__all__ = [
    "CATEGORY_LEVELS",
    "EXCLUDED_FILES",
    "FALLBACK_WORDS",
    "LEXICON_LICENSE",
    "LEXICON_SUBDIR",
    "LEXICON_UPSTREAM",
    "AhoCorasick",
    "Lexicon",
    "clear_cache",
    "get_lexicon",
    "level_for",
    "load_lexicon",
    "normalize_for_match",
    "scan",
]
