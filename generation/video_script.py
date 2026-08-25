"""抖音短视频口播脚本链。

::

    切角度 → 脚本(结构化) → 质量自评(结构化) → [条件] 去 AI 味改写

和小红书链（``xhs_note``）、公众号链（``wechat_article``）同一套思路：每步只处理
一件事，中间产物全部留在 :attr:`VideoScriptDraft.trace` 里可审计。差别在于
**短视频的载体是"被念出来的话"**：

- 产物必须是纯口语文本，任何念不出来的东西（markdown 标记、项目符号、emoji、
  ``#话题``）都会被 TTS 原样读出来或读成噪音，所以 :func:`strip_unspeakable`
  做**兜底清洗**，不只靠 prompt 约束。
- 字数就是时长。中文 TTS 约 :data:`CHARS_PER_SECOND` 字/秒，300 字 ≈ 60 秒——
  这也是脚本上限的来源。
- 另外要产出 ``search_terms``：**英文**画面检索词，直接送进 MoneyPrinterTurbo 的
  素材源（Pexels / Pixabay）。中文词在这两个库里几乎召回不到东西，所以非 ASCII
  的词会被丢掉并记 warning。

平台硬限制（标题 30 字、文案 1000 字、话题数量）在 :mod:`review.inspect` 里定义，
本模块 import 过来做兜底截断——模型超字数是常态。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

import prompts
from generation.imagegen import image_prompt_rules, normalize_image_prompts
from generation.llm import SupportsLLM, Usage
from generation.output_budget import budget_for
from generation.textutil import accumulate_usage, clean_tags, strip_code_fence, strip_zero_width
from generation.textutil import truncate as _truncate
from review.inspect import (
    DOUYIN_TAG_RANGE,
    MAX_DOUYIN_BODY_CHARS,
    MAX_DOUYIN_TITLE_CHARS,
    MAX_DOUYIN_VIDEO_SECONDS,
)

logger = logging.getLogger("social_workflow.generation.video_script")

#: 中文 TTS 的经验语速（字/秒）。用来把"字数"折算成"时长"，估算而已，不追求精确
CHARS_PER_SECOND = 5.0
#: 口播稿字数上限 ≈ 60 秒。抖音允许 15 分钟，但完播率决定一切，MVP 只做短片
MAX_SCRIPT_CHARS = 300
#: 目标字数（约 40 秒）。留出余量，模型写超一点也不至于被截
DEFAULT_TARGET_SCRIPT_CHARS = 220
#: 成片时长的建议区间（秒），只用于提示词，不做硬校验
VIDEO_SECONDS_RANGE = (20, 60)
#: 前 3 秒钩子的字数上限（3 秒 × 语速，向上取整留点余量）
MAX_HOOK_CHARS = 20
#: 封面大字上限
MAX_COVER_TEXT_CHARS = 12
#: 素材检索词个数区间
SEARCH_TERMS_RANGE = (3, 6)
#: 单个检索词的单词数上限（Pexels 的长查询召回很差）
MAX_TERM_WORDS = 4
#: 单个话题标签的字数上限（平台侧更宽，这里收紧防止模型写成一句话）
MAX_TAG_CHARS = 12
#: 自评总分低于该值才触发去 AI 味改写
REWRITE_THRESHOLD = 8

Verdict = Literal["pass", "revise", "reject"]

#: 念不出来的东西：markdown 标记、项目符号、行内代码、话题标签、emoji 与装饰符号
_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_MD_BULLET = re.compile(r"^\s{0,3}(?:[-*+•]|\d+[.、)])\s+", re.MULTILINE)
_MD_EMPHASIS = re.compile(r"(\*{1,3}|_{1,3}|`+|~~)")
_HASHTAG = re.compile(r"[#＃][^\s#＃]{1,30}")
#: 常见 emoji 区段 + 变体选择符。刻意不做全 Unicode 分类扫描（那要拉 regex 库），
#: 也刻意**逐块列举**而不是写一个大区间——网上流传的 ``Ⓜ-\U0001F251`` 写法
#: 把整个 CJK 统一表意文字区（U+4E00–U+9FFF）也圈进去了，会把中文正文删光。
_EMOJI = re.compile(
    "["
    "™ℹ"  # ™ ℹ
    "←-⇿"  # 箭头
    "⌀-⏿"  # 杂项技术符号（⌚⏰…）
    "①-⓿"  # 带圈数字 / 字母
    "☀-➿"  # 杂项符号 + 装饰符号（☀…➿）
    "⬀-⯿"  # 杂项符号与箭头
    "〰〽㊗㊙"  # 〰 〽 ㊗ ㊙（其余 CJK 标点不能碰）
    "️⃣"  # 变体选择符 / 组合键帽
    "\U0001f000-\U0001faff"  # 麻将 → 扩展象形文字（含 emoji 主体）
    "]"
)
#: 检索词里允许的字符（ASCII 字母、数字、空格、连字符）
_TERM_ALLOWED = re.compile(r"[^a-zA-Z0-9 \-]")
#: 句子结束标点，供按句截断用
_SENTENCE_END = "。！？!?…\n"


# --------------------------------------------------------------- 结构化输出


class VideoScriptCopy(BaseModel):
    """一条短视频的全部文案产物。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(description="作品标题，不是口播第一句")
    hook: str = Field(description="前 3 秒钩子，必须是口播稿的第一句")
    script: str = Field(description="完整口播稿，纯口语文本，不带任何标记")
    search_terms: list[str] = Field(
        default_factory=list, description="英文画面检索词，2–4 个单词一条"
    )
    cover_text: str = Field(default="", description="印在竖版封面上的一行大字")
    hashtags: list[str] = Field(default_factory=list, description="话题标签，不带 # 号")
    #: 生图模型用的英文 prompt（P11）。封面底图只用第一条
    image_prompts: list[str] = Field(
        default_factory=list, description="英文生图 prompt，一条一张；不配图时留空数组"
    )


class VideoSelfCheck(BaseModel):
    """质量自评。维度按短视频重排：钩子、口语度、节奏、检索词可用性。"""

    model_config = ConfigDict(extra="forbid")

    ai_flavor: int = Field(ge=0, le=10, description="分数越高越不像 AI 写的")
    hook_strength: int = Field(ge=0, le=10, description="前 3 秒能不能让人停下来")
    specificity: int = Field(ge=0, le=10)
    spoken_fit: int = Field(ge=0, le=10, description="念出来顺不顺")
    pacing: int = Field(ge=0, le=10, description="有没有可以整句删掉的话")
    term_fit: int = Field(ge=0, le=10, description="英文检索词能不能召回画面")
    compliance_risk: int = Field(ge=0, le=10, description="分数越高风险越低")
    overall: int = Field(ge=0, le=10)
    verdict: Verdict
    blocking_issues: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)

    @property
    def needs_rewrite(self) -> bool:
        return (
            self.verdict != "pass"
            or self.overall < REWRITE_THRESHOLD
            or self.ai_flavor < REWRITE_THRESHOLD
            or self.spoken_fit < REWRITE_THRESHOLD
            or bool(self.blocking_issues)
        )


# ------------------------------------------------------------------- 产物


@dataclass
class VideoScriptDraft:
    """一条短视频的完整文案产物。

    ``script`` 是**要被念出来的那段字**，会原样灌进 MPT 的 ``video_script``；
    ``caption()`` 是发布到抖音的文案（和口播稿不是一回事）。
    """

    title: str
    hook: str
    script: str
    search_terms: list[str]
    cover_text: str
    hashtags: list[str]
    #: 封面底图用的英文生图 prompt（P11）。空数组 = 这条不用生图做底
    image_prompts: list[str] = field(default_factory=list)
    selfcheck: VideoSelfCheck | None = None
    #: 各步中间产物，供审计与复盘
    trace: dict[str, str] = field(default_factory=dict)
    usage: Usage = field(default_factory=Usage)
    rewritten: bool = False
    #: 兜底截断 / 清洗留下的痕迹，会汇进 VideoGenerationOutcome.warnings
    warnings: list[str] = field(default_factory=list)

    @property
    def estimated_seconds(self) -> float:
        """按语速估的成片时长。真实时长以 ``review.inspect.read_video_info`` 量到的为准。"""
        return round(len(self.script) / CHARS_PER_SECOND, 1)

    def caption(self) -> str:
        """写进 ``ContentBundle.body_markdown`` 的作品文案 = 钩子/摘要 + 话题行。

        和小红书一样把话题拼进正文：抖音的话题也是**文案的一部分**，
        跟着作品一起提交，审核必须扫到它们（蹭违规话题同样会被判违规）。
        """
        base = self.hook.strip() or self.title.strip()
        if not self.hashtags:
            return base[:MAX_DOUYIN_BODY_CHARS]
        suffix = "\n\n" + " ".join(f"#{tag}" for tag in self.hashtags)
        room = MAX_DOUYIN_BODY_CHARS - len(suffix)
        body = base if len(base) <= room else base[: max(room - 1, 0)] + "…"
        return f"{body}{suffix}"

    def as_platform_extra(self) -> dict[str, Any]:
        """写进 ``ContentBundle.platform_extra`` 的生成侧字段。"""
        return {
            "hook": self.hook,
            "script": self.script,
            "search_terms": list(self.search_terms),
            "cover_text": self.cover_text,
            # 配图 prompt 留痕：出了问题要能回看"当时让模型画的是什么"
            "image_prompts": list(self.image_prompts),
            "estimated_seconds": self.estimated_seconds,
            "selfcheck": self.selfcheck.model_dump() if self.selfcheck else None,
            "rewritten": self.rewritten,
        }


# ------------------------------------------------------------------- 归一化


def strip_unspeakable(text: str) -> str:
    """把念不出来的东西从口播稿里清掉。

    TTS 会把 ``**`` 读成"星星星星"、把 ``#通勤`` 读成"井号通勤"。prompt 已经写了
    不要这些，但模型偶发是常态，兜底清洗比事后人工重跑便宜。
    """
    out = strip_code_fence(strip_zero_width(text))
    out = _MD_HEADING.sub("", out)
    out = _MD_BULLET.sub("", out)
    out = _MD_EMPHASIS.sub("", out)
    out = _HASHTAG.sub("", out)
    out = _EMOJI.sub("", out)
    # 逐行清掉行尾空白，再把三个以上空行压成两个
    out = "\n".join(line.rstrip() for line in out.splitlines())
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def truncate_spoken(text: str, limit: int) -> str:
    """按字数截断口播稿，**优先切在句号处**。

    直接硬截会让成片以半句话结尾，听感上是明显的事故。找不到句读时才硬截，
    并补一个句号收尾（不补省略号——TTS 会把它读成停顿或干脆读出来）。
    """
    if len(text) <= limit:
        return text
    window = text[:limit]
    cut = max(window.rfind(ch) for ch in _SENTENCE_END)
    if cut >= limit // 2:
        return window[: cut + 1].rstrip()
    return window.rstrip().rstrip("，,、;；") + "。"


def normalize_terms(raw: list[str]) -> tuple[list[str], list[str]]:
    """清洗素材检索词，返回 ``(terms, warnings)``。

    规则：只留 ASCII 字母数字与连字符、压空白、限词数、去重、限量。
    含中文的词整条丢掉——Pexels / Pixabay 对中文查询基本零召回，留着只会让
    MPT 拿不到素材然后在 ``materials`` 阶段失败。
    """
    warnings: list[str] = []
    min_terms, max_terms = SEARCH_TERMS_RANGE
    cleaned: list[str] = []
    dropped: list[str] = []
    for item in raw:
        text = strip_zero_width(str(item)).strip()
        if not text:
            continue
        if not text.isascii():
            dropped.append(text)
            continue
        words = _TERM_ALLOWED.sub(" ", text).split()
        if not words:
            dropped.append(text)
            continue
        term = " ".join(words[:MAX_TERM_WORDS]).lower()
        if term not in cleaned:
            cleaned.append(term)
    if dropped:
        warnings.append(
            f"丢弃 {len(dropped)} 个非英文检索词（素材源召回不到）：{'、'.join(dropped)}"
        )
    if len(cleaned) > max_terms:
        warnings.append(f"检索词 {len(cleaned)} 个，超过 {max_terms} 个，已截断")
        cleaned = cleaned[:max_terms]
    if len(cleaned) < min_terms:
        warnings.append(f"只有 {len(cleaned)} 个可用检索词，低于建议下限 {min_terms} 个")
    return cleaned, warnings


def normalize_hashtags(raw: list[str], *, limit: int = DOUYIN_TAG_RANGE[1]) -> list[str]:
    """清洗话题标签。规则与小红书共用，实现在 :func:`generation.textutil.clean_tags`。"""
    return clean_tags(raw, limit=limit, max_chars=MAX_TAG_CHARS)


def _ensure_hook_first(script: str, hook: str) -> tuple[str, bool]:
    """保证口播稿以钩子开头。返回 ``(script, 是否补写过)``。

    钩子是前 3 秒的全部——它不在开头，这条片子的完播率就没了。改写步骤最容易
    把它冲掉，所以补一道确定性检查而不是再问一次模型。
    """
    hook = hook.strip()
    if not hook:
        return script, False
    head = script.lstrip()
    if head.startswith(hook):
        return script, False
    # 允许模型在钩子里加尾标点：比对时去掉两端标点再看
    bare = hook.rstrip("。！？!?，,")
    if bare and head.startswith(bare):
        return script, False
    return f"{hook}\n\n{script.lstrip()}", True


# ------------------------------------------------------------------- 生成链


def generate_video_script(
    llm: SupportsLLM,
    *,
    topic_title: str,
    persona: str,
    topic_source: str = "",
    topic_url: str = "",
    topic_context: str = "",
    target_script_chars: int = DEFAULT_TARGET_SCRIPT_CHARS,
    force_rewrite: bool | None = None,
    image_prompt_count: int = 0,
) -> VideoScriptDraft:
    """跑完四步脚本链，产出 :class:`VideoScriptDraft`。

    ``force_rewrite`` 为 ``None`` 时按自评结果决定要不要做第四步；
    显式 ``True``/``False`` 可强制或跳过（测试与省 token 用）。

    ``image_prompt_count > 0`` 时让写脚本那一步顺手产出英文生图 prompt（P11），
    封面底图用第一条。不额外开调用，理由同 :func:`generation.xhs_note.generate_xhs_note`。
    """
    system = prompts.load("douyin/system", persona=persona or "（未提供人设）")
    total = Usage()
    trace: dict[str, str] = {}
    warnings: list[str] = []
    min_seconds, max_seconds = VIDEO_SECONDS_RANGE
    min_terms, max_terms = SEARCH_TERMS_RANGE
    min_tags, max_tags = DOUYIN_TAG_RANGE

    # 1 切角度 -----------------------------------------------------------
    angle_result = llm.complete(
        prompts.load(
            "douyin/angle",
            topic_title=topic_title,
            topic_source=topic_source or "未标注",
            topic_url=topic_url or "无",
            topic_context=topic_context or "无额外信息，按公开常识处理",
            min_seconds=min_seconds,
            max_seconds=max_seconds,
        ),
        system=system,
        max_tokens=budget_for("douyin.angle"),
        purpose="douyin.angle",
    )
    angle = strip_code_fence(angle_result.text)
    trace["angle"] = angle
    total = accumulate_usage(total, angle_result.usage)

    # 2 脚本 ---------------------------------------------------------------
    copy_result = llm.parse(
        prompts.load(
            "douyin/script",
            angle=angle,
            max_title=MAX_DOUYIN_TITLE_CHARS,
            max_hook=MAX_HOOK_CHARS,
            max_script=MAX_SCRIPT_CHARS,
            target_seconds=int(target_script_chars / CHARS_PER_SECOND),
            min_terms=min_terms,
            max_terms=max_terms,
            max_cover=MAX_COVER_TEXT_CHARS,
            min_tags=min_tags,
            max_tags=max_tags,
            image_rules=image_prompt_rules(image_prompt_count),
        ),
        VideoScriptCopy,
        system=system,
        max_tokens=budget_for("douyin.script"),
        purpose="douyin.script",
    )
    copy = copy_result.parsed
    total = accumulate_usage(total, copy_result.usage)

    hook = _truncate(strip_unspeakable(copy.hook), MAX_HOOK_CHARS)
    script = strip_unspeakable(copy.script)
    trace["script"] = script
    search_terms, term_warnings = normalize_terms(copy.search_terms)
    warnings.extend(term_warnings)
    hashtags = normalize_hashtags(copy.hashtags, limit=max_tags)
    image_prompts, image_warnings = normalize_image_prompts(
        copy.image_prompts, count=image_prompt_count
    )
    warnings.extend(image_warnings)
    if len(hashtags) < min_tags:
        warnings.append(f"只有 {len(hashtags)} 个话题，低于建议下限 {min_tags} 个")

    # 3 质量自评 ----------------------------------------------------------
    check_result = llm.parse(
        prompts.load(
            "douyin/selfcheck",
            title=copy.title,
            hook=hook,
            script=script,
            search_terms="、".join(search_terms) or "（无）",
            tags="、".join(hashtags) or "（无）",
        ),
        VideoSelfCheck,
        system=system,
        max_tokens=budget_for("douyin.selfcheck"),
        purpose="douyin.selfcheck",
    )
    selfcheck = check_result.parsed
    total = accumulate_usage(total, check_result.usage)
    logger.info(
        "抖音自评 overall=%d hook=%d spoken=%d compliance=%d verdict=%s blocking=%d",
        selfcheck.overall,
        selfcheck.hook_strength,
        selfcheck.spoken_fit,
        selfcheck.compliance_risk,
        selfcheck.verdict,
        len(selfcheck.blocking_issues),
    )

    # 4 去 AI 味改写（有条件）---------------------------------------------
    rewritten = False
    should_rewrite = selfcheck.needs_rewrite if force_rewrite is None else force_rewrite
    if should_rewrite:
        issues = selfcheck.blocking_issues + selfcheck.suggestions
        rewrite_result = llm.complete(
            prompts.load(
                "douyin/dehumanize",
                script=script,
                hook=hook or "（原稿没有明确钩子，用第一句）",
                issues="\n".join(f"- {i}" for i in issues) or "（自评未列出具体问题）",
                max_script=MAX_SCRIPT_CHARS,
            ),
            system=system,
            max_tokens=budget_for("douyin.dehumanize"),
            purpose="douyin.dehumanize",
        )
        script = strip_unspeakable(rewrite_result.text)
        trace["dehumanized"] = script
        total = accumulate_usage(total, rewrite_result.usage)
        rewritten = True

    # 兜底：钩子必须在开头，长度是硬限制 ------------------------------------
    script, hook_restored = _ensure_hook_first(script, hook)
    if hook_restored:
        warnings.append("口播稿没有以钩子开头（改写最容易把它冲掉），已把钩子补回第一句")
    if len(script) > MAX_SCRIPT_CHARS:
        warnings.append(
            f"口播稿 {len(script)} 字（约 {len(script) / CHARS_PER_SECOND:.0f} 秒），"
            f"超过 {MAX_SCRIPT_CHARS} 字，已按句截断"
        )
        script = truncate_spoken(script, MAX_SCRIPT_CHARS)

    title = _truncate(strip_unspeakable(copy.title), MAX_DOUYIN_TITLE_CHARS)
    if not title:
        title = _truncate(hook or topic_title, MAX_DOUYIN_TITLE_CHARS)
        warnings.append("模型没给标题，已回退到钩子")
    elif len(strip_unspeakable(copy.title)) > MAX_DOUYIN_TITLE_CHARS:
        warnings.append(f"标题 {len(copy.title)} 字超过 {MAX_DOUYIN_TITLE_CHARS} 字，已截断")

    if not hook:
        hook = _truncate(script.splitlines()[0] if script else title, MAX_HOOK_CHARS)
        warnings.append("模型没给钩子，已取口播稿第一行")

    cover_text = _truncate(strip_unspeakable(copy.cover_text), MAX_COVER_TEXT_CHARS)
    if not cover_text:
        cover_text = _truncate(title, MAX_COVER_TEXT_CHARS)

    estimated = len(script) / CHARS_PER_SECOND
    if estimated > MAX_DOUYIN_VIDEO_SECONDS:  # pragma: no cover - 300 字上限决定了走不到
        warnings.append(f"估算时长 {estimated:.0f} 秒超过平台上限，人工确认")

    return VideoScriptDraft(
        title=title,
        hook=hook,
        script=script,
        search_terms=search_terms,
        cover_text=cover_text,
        hashtags=hashtags,
        image_prompts=image_prompts,
        selfcheck=selfcheck,
        trace=trace,
        usage=total,
        rewritten=rewritten,
        warnings=warnings,
    )


__all__ = [
    "CHARS_PER_SECOND",
    "DEFAULT_TARGET_SCRIPT_CHARS",
    "MAX_COVER_TEXT_CHARS",
    "MAX_HOOK_CHARS",
    "MAX_SCRIPT_CHARS",
    "MAX_TAG_CHARS",
    "MAX_TERM_WORDS",
    "REWRITE_THRESHOLD",
    "SEARCH_TERMS_RANGE",
    "VIDEO_SECONDS_RANGE",
    "VideoScriptCopy",
    "VideoScriptDraft",
    "VideoSelfCheck",
    "generate_video_script",
    "normalize_hashtags",
    "normalize_terms",
    "strip_unspeakable",
    "truncate_spoken",
]
