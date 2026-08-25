"""选题去重：标题归一化 + simhash + 归一化编辑距离。

热榜的同一件事在不同平台标题往往只差几个字（"XX 回应 YY" / "XX 就 YY 作出回应"），
纯 hash 去重挡不住，纯编辑距离又是 O(n²) 全量比。这里两级：

1. **归一化精确键**——挡住纯标点/全半角/emoji 差异，O(1)。
2. **simhash 分桶 + 汉明距离**——挡住少量增删改；桶内再用归一化编辑距离复核，
   避免 simhash 在短文本上的误判。

所有哈希都走 :mod:`hashlib`，**不用内置 ``hash()``**：后者对 str 加了进程级随机盐，
跨进程结果不一致，会让"上次跑过的去重"在重启后失效。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable

#: simhash 位宽
SIMHASH_BITS = 64
#: 汉明距离 > 该值直接判为不重复（**粗筛**，短中文标题上 simhash 噪声大，阈值必须放宽）
DEFAULT_HAMMING_THRESHOLD = 26
#: 归一化编辑距离相似度 >= 该值判为重复
DEFAULT_SIMILARITY_THRESHOLD = 0.78
#: 短标题（归一化后字符数 < 该值）改用更严的相似度阈值，防止"大涨/大跌"被判成同一条
SHORT_TITLE_CHARS = 8
SHORT_SIMILARITY_THRESHOLD = 0.9
#: 字符集包含度 >= 该值判为重复：中文热榜同一事件常见语序重排 + 加后缀，
#: 编辑距离对语序重排无能为力，包含度能补上（"XX回应YY" vs "XX就YY作出回应"）。
DEFAULT_CONTAINMENT_THRESHOLD = 0.9
#: 包含度规则的最小标题长度，太短的标题（如"苹果"）用包含度会大面积误判
CONTAINMENT_MIN_CHARS = 5
#: 子串规则的最小标题长度，同理防止"苹果"命中一切含"苹果"的标题
SUBSTRING_MIN_CHARS = 4

# 热榜常见的噪声后缀/前缀：名次、"热"/"新"/"沸"/"爆" 角标、话题井号
_RANK_PREFIX = re.compile(r"^\s*(?:no\.?\s*)?\d{1,3}\s*[.、,:：)）]\s*", re.IGNORECASE)
_HASH_TAG = re.compile(r"[#＃]")
_NOISE_SUFFIX = re.compile(r"(?:热|新|沸|爆|荐|置顶)$")
# 只保留中日韩统一表意文字 + 拉丁字母 + 数字
_KEEP = re.compile(r"[^0-9a-z一-鿿぀-ヿ]")


def normalize_title(title: str) -> str:
    """标题归一化：NFKC → 去名次前缀 → 去井号 → 只留字母数字与 CJK → 小写 → 去角标。"""
    text = unicodedata.normalize("NFKC", title or "").strip().lower()
    text = _RANK_PREFIX.sub("", text)
    text = _HASH_TAG.sub("", text)
    text = _KEEP.sub("", text)
    # 角标可能叠加（"…热沸"），循环剥离
    while True:
        stripped = _NOISE_SUFFIX.sub("", text)
        if stripped == text:
            break
        text = stripped
    return text


def title_key(title: str) -> str:
    """归一化标题的 sha256，与 ``sourcing/README.md`` 约定的去重键一致。"""
    return hashlib.sha256(normalize_title(title).encode("utf-8")).hexdigest()


def _shingles(text: str, size: int = 2) -> list[str]:
    """字符 n-gram。中文没有空格分词，字符 bigram 是最稳的廉价特征。"""
    if len(text) <= size:
        return [text] if text else []
    return [text[i : i + size] for i in range(len(text) - size + 1)]


def _feature_hash(token: str) -> int:
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def simhash(text: str, *, bits: int = SIMHASH_BITS) -> int:
    """对归一化后的标题算 simhash。空串返回 0。"""
    normalized = normalize_title(text)
    tokens = _shingles(normalized)
    if not tokens:
        return 0
    vector = [0] * bits
    for token in tokens:
        h = _feature_hash(token)
        for i in range(bits):
            vector[i] += 1 if (h >> i) & 1 else -1
    value = 0
    for i in range(bits):
        if vector[i] > 0:
            value |= 1 << i
    return value


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def edit_distance(a: str, b: str) -> int:
    """Levenshtein 距离。标题很短（≤ 64 字），两行滚动数组足够。"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(
                min(
                    previous[j] + 1,  # 删除
                    current[j - 1] + 1,  # 插入
                    previous[j - 1] + (ca != cb),  # 替换
                )
            )
        previous = current
    return previous[-1]


def similarity(a: str, b: str) -> float:
    """基于归一化标题的编辑距离相似度，范围 ``[0, 1]``。"""
    na, nb = normalize_title(a), normalize_title(b)
    if not na and not nb:
        return 1.0
    longest = max(len(na), len(nb))
    if longest == 0:
        return 1.0
    return 1.0 - edit_distance(na, nb) / longest


def containment(a: str, b: str) -> float:
    """字符集包含度 ``|A∩B| / min(|A|,|B|)``，对语序重排与加后缀不敏感。"""
    sa, sb = set(normalize_title(a)), set(normalize_title(b))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / min(len(sa), len(sb))


def _similarity_threshold_for(na: str, nb: str, base: float) -> float:
    """短标题收紧阈值：8 字以内一字之差就可能是相反的意思（大涨/大跌）。"""
    if min(len(na), len(nb)) < SHORT_TITLE_CHARS:
        return max(base, SHORT_SIMILARITY_THRESHOLD)
    return base


def is_duplicate(
    a: str,
    b: str,
    *,
    hamming_threshold: int = DEFAULT_HAMMING_THRESHOLD,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    containment_threshold: float = DEFAULT_CONTAINMENT_THRESHOLD,
) -> bool:
    """两个标题是否指向同一个热点。

    判定顺序：归一化精确相等 → simhash 粗筛 → 编辑距离相似度 → 字符集包含度。
    """
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return na == nb
    if na == nb:
        return True
    # 一方是另一方的子串：热榜同一事件加后缀的典型形态（"国足输球" / "国足输球了"）
    if min(len(na), len(nb)) >= SUBSTRING_MIN_CHARS and (na in nb or nb in na):
        return True
    # 粗筛：汉明距离过大直接否掉，省去后面两次 O(n·m) 比对
    if hamming(simhash(a), simhash(b)) > hamming_threshold:
        return False
    if similarity(a, b) >= _similarity_threshold_for(na, nb, similarity_threshold):
        return True
    if min(len(na), len(nb)) >= CONTAINMENT_MIN_CHARS:
        return containment(a, b) >= containment_threshold
    return False


class Deduper:
    """增量去重器。``add`` 返回 ``True`` 表示这是个新选题。

    候选集用**字符倒排索引**生成（共享 ≥2 个字符才参与比对），而不是 simhash 分桶：
    短中文标题上 simhash 的位分布不稳，分桶会漏检；倒排索引召回高、代价低，
    simhash 退化为候选集内部的廉价粗筛。
    """

    #: 进入精确比对所需的最少共享字符数
    MIN_SHARED_CHARS = 2

    def __init__(
        self,
        *,
        hamming_threshold: int = DEFAULT_HAMMING_THRESHOLD,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        containment_threshold: float = DEFAULT_CONTAINMENT_THRESHOLD,
    ) -> None:
        self.hamming_threshold = hamming_threshold
        self.similarity_threshold = similarity_threshold
        self.containment_threshold = containment_threshold
        self._keys: set[str] = set()
        self._titles: list[str] = []
        self._index: dict[str, list[int]] = {}

    def __len__(self) -> int:
        return len(self._titles)

    def _candidates(self, normalized: str) -> list[int]:
        counts: dict[int, int] = {}
        chars = set(normalized)
        for char in chars:
            for idx in self._index.get(char, ()):
                counts[idx] = counts.get(idx, 0) + 1
        need = min(self.MIN_SHARED_CHARS, len(chars))
        return [idx for idx, hits in counts.items() if hits >= need]

    def seen(self, title: str) -> bool:
        """只查不写。空标题一律视为已见（不入库）。"""
        normalized = normalize_title(title)
        if not normalized:
            return True
        if normalized in self._keys:
            return True
        return any(
            is_duplicate(
                normalized,
                self._titles[idx],
                hamming_threshold=self.hamming_threshold,
                similarity_threshold=self.similarity_threshold,
                containment_threshold=self.containment_threshold,
            )
            for idx in self._candidates(normalized)
        )

    def add(self, title: str) -> bool:
        """登记标题。返回 ``True`` 表示新增，``False`` 表示与已有选题重复。"""
        normalized = normalize_title(title)
        if not normalized or self.seen(title):
            return False
        index = len(self._titles)
        self._titles.append(normalized)
        self._keys.add(normalized)
        for char in set(normalized):
            self._index.setdefault(char, []).append(index)
        return True

    def extend(self, titles: Iterable[str]) -> int:
        """批量登记，返回新增条数。"""
        return sum(1 for title in titles if self.add(title))


__all__ = [
    "CONTAINMENT_MIN_CHARS",
    "DEFAULT_CONTAINMENT_THRESHOLD",
    "DEFAULT_HAMMING_THRESHOLD",
    "DEFAULT_SIMILARITY_THRESHOLD",
    "SHORT_SIMILARITY_THRESHOLD",
    "SHORT_TITLE_CHARS",
    "SIMHASH_BITS",
    "Deduper",
    "containment",
    "edit_distance",
    "hamming",
    "is_duplicate",
    "normalize_title",
    "simhash",
    "similarity",
    "title_key",
]
