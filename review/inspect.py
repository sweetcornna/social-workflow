"""发布前校验：``inspect(bundle) -> InspectReport``。

这是最后一道**结构性**校验，和三级内容审核是两回事：内容审核管"能不能说"，
这里管"平台会不会直接拒收"。全部是确定性规则，不调 LLM，可以在发布路径上同步跑。

公众号侧的硬约束（``docs/OPS.md`` / 官方文档）：

- 标题 ≤ 64 字，实际展示会截断，本项目自己收紧到 32 字。
- 摘要 ≤ 120 字（留空则平台自动取正文前 54 字）。
- 正文里的图片必须是 ``mmbiz.qpic.cn`` 域名——外链图片会被直接过滤掉，
  所以草稿里出现外链 ``<img>`` 一律 block。
- 封面 ``thumb_media_id`` 必须来自素材库；本阶段只校验本地封面文件存在。

小红书侧的硬约束（P2）：

- 标题 ≤ 20 字（超了发布器会截断，标题被截断等于白写）。
- 正文（含话题标签，标签算正文字数）≤ 1000 字。
- 图片 1–18 张；单图长边与体积有上限；比例落在 3:4 ~ 4:3 之间才不会被裁。
- 话题标签 3–8 个为宜，超过 10 个平台不再收录。

抖音侧的硬约束（P3）：

- 有且只有 1 个视频成片，时长 ≤ 15 分钟、比例 9:16。
- 必须有封面图（发布器要拿它当作品封面）。
- 标题 ≤ 30 字；文案（含话题）≤ 1000 字。

**视频探测不引入新依赖**：:func:`read_video_info` 先用标准库解析 MP4 box
（``moov/mvhd`` 拿时长、``moov/trak/tkhd`` 拿分辨率），失败再退到 ``ffprobe``
（装了才用）。两条路都读不出时返回 ``None``，调用方按"没量到"处理（warn），
**绝不因为量不出来就 block** —— 那会把"本机没装 ffmpeg"变成"内容不合规"。
"""

from __future__ import annotations

import json
import re
import shutil
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from publishers.base import ContentBundle, MediaAsset
from review.base import Finding, sort_findings

#: 本项目自定的标题上限（平台是 64，收紧是为了移动端不折行）
MAX_TITLE_CHARS = 32
#: 公众号 digest 上限
MAX_DIGEST_CHARS = 120
#: 单篇图文的图片数量上限（超过会明显拖慢加载）
MAX_IMAGES = 20
#: 正文长度下限：低于这个数基本是生成失败
MIN_BODY_CHARS = 200

# -------------------------------------------------------------------- 小红书
#: 小红书标题上限。平台侧就是 20 字，不是本项目收紧的
MAX_XHS_TITLE_CHARS = 20
#: 正文上限。话题标签算在正文字数里，所以校验的是 body_markdown 全文
MAX_XHS_BODY_CHARS = 1000
#: 正文下限。比公众号低很多——图文笔记的信息主要在图上，正文短是正常的
MIN_XHS_BODY_CHARS = 60
#: 图片张数区间（平台上限 18 张）
XHS_IMAGE_RANGE = (1, 18)
#: 话题标签的**建议**区间；超出只 warn
XHS_TAG_RANGE = (3, 8)
#: 话题标签硬上限，超过平台不再收录，直接 block
MAX_XHS_TAGS = 10
#: 卡片脚本页数区间（封面另计），供 generation 侧共用
XHS_PAGE_RANGE = (3, 8)
#: 单图长边上限（px）与体积上限（bytes）
MAX_XHS_IMAGE_EDGE = 4096
MAX_XHS_IMAGE_BYTES = 20 * 1024 * 1024
#: 宽高比可接受区间：3:4 (0.75) ~ 4:3 (1.333)，超出会被平台裁切
XHS_ASPECT_RANGE = (0.74, 1.34)

# --------------------------------------------------------------------- 抖音
#: 抖音标题上限。**未核实**：官方未公开该数字，30 取自创作者中心输入框的实测计数，
#: 且比"标题被折叠"的经验阈值更紧，超了先挡下来让人精简
MAX_DOUYIN_TITLE_CHARS = 30
#: 文案（含话题标签）上限。**未核实**：取自社区共识的 1000 字
MAX_DOUYIN_BODY_CHARS = 1000
#: 文案下限：短于这个数基本是生成失败（视频的信息在片子里，文案短是正常的）
MIN_DOUYIN_BODY_CHARS = 20
#: 成片时长上限（秒）。15 分钟是普通创作者可发布的最长时长
MAX_DOUYIN_VIDEO_SECONDS = 15 * 60
#: 成片时长下限（秒）：短于这个数基本是渲染失败或素材没拼上
MIN_DOUYIN_VIDEO_SECONDS = 3.0
#: 竖屏 9:16 = 0.5625。容差覆盖 1080×1920 / 720×1280 / 1088×1920 一类的近似尺寸
DOUYIN_ASPECT = 9 / 16
DOUYIN_ASPECT_TOLERANCE = 0.02
#: 单个成片体积上限（bytes）。**未核实**：4GB 取自上传页提示
MAX_DOUYIN_VIDEO_BYTES = 4 * 1024 * 1024 * 1024
#: 话题标签**建议**区间；超出只 warn
DOUYIN_TAG_RANGE = (2, 5)
#: 话题标签硬上限
MAX_DOUYIN_TAGS = 10
#: 发布器接受的视频容器格式
VIDEO_EXTS = frozenset({".mp4", ".mov", ".m4v"})

#: 各平台的标题上限
MAX_TITLE_CHARS_BY_PLATFORM: dict[str, int] = {
    "wechat_mp": MAX_TITLE_CHARS,
    "xhs": MAX_XHS_TITLE_CHARS,
    "douyin": MAX_DOUYIN_TITLE_CHARS,
}
#: 各平台的正文下限
MIN_BODY_CHARS_BY_PLATFORM: dict[str, int] = {
    "wechat_mp": MIN_BODY_CHARS,
    "xhs": MIN_XHS_BODY_CHARS,
    "douyin": MIN_DOUYIN_BODY_CHARS,
}

#: 微信素材库图片域名，正文里只有这个域名的图能正常显示
WECHAT_IMAGE_HOST = "mmbiz.qpic.cn"

_IMG_SRC = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE)
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)")
_HTTP_URL = re.compile(r"^https?://", re.IGNORECASE)
#: 话题标签里不该出现的字符（发布器会原样提交，带上去平台创建话题会失败）
_BAD_TAG_CHARS = re.compile(r"[#＃\s,，、;；/\\|]")
#: 配图 prompt 里的高危要素（P11）。只认**肯定句**——``prompts/imagegen.md`` 里
#: 全是 ``no text`` / ``no recognizable faces`` 这类否定式约束，把它们也报出来
#: 就成了纯噪音。所以否定词开头的一律放过。
_RISKY_PROMPT = re.compile(
    r"(?<!no )(?<!without )(?<!avoid )"
    r"(recognizable faces?|celebrity|celebrities|portrait of a real|brand logos?"
    r"|trademark|watermarks?|in the style of [A-Z])",
    re.IGNORECASE,
)

#: 各平台在 ``platform_extra`` 里的必填字段
REQUIRED_EXTRA: dict[str, tuple[str, ...]] = {
    "wechat_mp": ("title", "digest", "author"),
    "xhs": (),
    "douyin": (),
}


class InspectReport(BaseModel):
    """结构化校验报告，可直接 ``model_dump_json()`` 当作 ``inspect --json`` 的输出。"""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    platform: str
    content_id: str
    findings: list[Finding] = Field(default_factory=list)
    #: 便于人工/日志快速看数：标题长度、图片数等
    metrics: dict[str, Any] = Field(default_factory=dict)

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "block"]


def _finding(level: str, rule: str, suggestion: str, *, excerpt: str = "") -> Finding:
    return Finding(
        level=level,  # type: ignore[arg-type]
        rule=f"inspect.{rule}",
        excerpt=excerpt,
        suggestion=suggestion,
        stage="inspect",
    )


def read_image_size(path: str | Path) -> tuple[int, int] | None:
    """读 PNG / JPEG 文件头拿宽高，读不出来返回 ``None``。

    刻意不引入 Pillow：这里只需要文件头里的两个整数，为此多一个带 C 扩展的依赖
    （还要进 License 台账）不划算。认不出的格式返回 ``None``，调用方按"没量到"处理，
    不会把校验变成 block。
    """
    target = Path(path)
    try:
        with target.open("rb") as fh:
            head = fh.read(32)
            if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
                width, height = struct.unpack(">II", head[16:24])
                return int(width), int(height)
            if head[:2] != b"\xff\xd8":  # 不是 JPEG
                return None
            # JPEG：顺着 marker 链找 SOFn（不含 SOF4/8/12 —— 那几个不是帧头）
            fh.seek(2)
            while True:
                marker = fh.read(2)
                if len(marker) < 2 or marker[0] != 0xFF:
                    return None
                kind = marker[1]
                size_bytes = fh.read(2)
                if len(size_bytes) < 2:
                    return None
                (segment_len,) = struct.unpack(">H", size_bytes)
                if 0xC0 <= kind <= 0xCF and kind not in (0xC4, 0xC8, 0xCC):
                    payload = fh.read(5)
                    if len(payload) < 5:
                        return None
                    height, width = struct.unpack(">HH", payload[1:5])
                    return int(width), int(height)
                fh.seek(segment_len - 2, 1)
    except OSError:
        return None


@dataclass(frozen=True)
class VideoInfo:
    """一段视频的可见尺寸与时长。``duration_s`` 读不出时为 ``None``。"""

    width: int
    height: int
    duration_s: float | None = None
    #: 来源：``mp4`` = 标准库解析 box；``ffprobe`` = 外部命令
    source: str = "mp4"

    @property
    def aspect(self) -> float | None:
        return self.width / self.height if self.height else None


def _iter_boxes(data: bytes, start: int, end: int) -> list[tuple[bytes, int, int]]:
    """遍历一层 MP4 box，返回 ``[(类型, 载荷起点, 载荷终点)]``。"""
    boxes: list[tuple[bytes, int, int]] = []
    offset = start
    while offset + 8 <= end:
        (size,) = struct.unpack(">I", data[offset : offset + 4])
        kind = data[offset + 4 : offset + 8]
        body = offset + 8
        if size == 1:  # 64 位 largesize
            if body + 8 > end:
                break
            (size,) = struct.unpack(">Q", data[body : body + 8])
            body += 8
        elif size == 0:  # 一直到文件末尾
            size = end - offset
        if size < 8:
            break
        stop = min(offset + size, end)
        if body > stop:
            break
        boxes.append((kind, body, stop))
        offset += size
    return boxes


def _find_box(data: bytes, path: tuple[bytes, ...], start: int, end: int) -> tuple[int, int] | None:
    """按 box 路径逐层下钻，返回最内层的载荷区间。"""
    head, rest = path[0], path[1:]
    for kind, body, stop in _iter_boxes(data, start, end):
        if kind != head:
            continue
        if not rest:
            return body, stop
        found = _find_box(data, rest, body, stop)
        if found is not None:
            return found
    return None


def _parse_mvhd(payload: bytes) -> float | None:
    """``mvhd`` → 时长（秒）。"""
    if len(payload) < 4:
        return None
    version = payload[0]
    try:
        if version == 1:
            timescale, duration = struct.unpack(">IQ", payload[20:32])
        else:
            timescale, duration = struct.unpack(">II", payload[12:20])
    except struct.error:
        return None
    if not timescale:
        return None
    return duration / timescale


def _parse_tkhd(payload: bytes) -> tuple[int, int] | None:
    """``tkhd`` → ``(width, height)``，已按变换矩阵处理 90°/270° 旋转。"""
    if len(payload) < 4:
        return None
    version = payload[0]
    # 载荷布局：v0 共 84 字节（matrix 在 40..75，width/height 在 76..83），
    # v1 共 96 字节（matrix 在 52..87，width/height 在 88..95）
    matrix_at = 88 if version == 1 else 76
    if len(payload) < matrix_at + 8:
        return None
    matrix = struct.unpack(">9i", payload[matrix_at - 36 : matrix_at])
    width, height = struct.unpack(">II", payload[matrix_at : matrix_at + 8])
    # 16.16 定点数
    w, h = width / 65536.0, height / 65536.0
    if w <= 0 or h <= 0:
        return None
    # 手机竖拍的成片常见 a=d=0、b/c 非零（旋转 ±90°），此时 tkhd 里的宽高是旋转前的
    if matrix[0] == 0 and matrix[4] == 0 and (matrix[1] or matrix[3]):
        w, h = h, w
    return round(w), round(h)


def _read_mp4_info(path: Path) -> VideoInfo | None:
    """纯标准库解析 MP4/MOV 的 ``moov`` box。非 MP4 系容器返回 ``None``。"""
    try:
        # moov 可能在文件尾（未 faststart），但整片读进内存不划算：
        # 先读头部 4MB 找 moov，找不到再读尾部 4MB
        with path.open("rb") as fh:
            head = fh.read(4 * 1024 * 1024)
            size = path.stat().st_size
            tail = b""
            if size > len(head):
                fh.seek(max(size - 4 * 1024 * 1024, len(head)))
                tail = fh.read()
    except OSError:
        return None
    if head[4:8] not in (b"ftyp", b"moov", b"free", b"mdat", b"skip", b"wide"):
        return None
    for blob in (head, tail):
        if not blob:
            continue
        # tail 是从文件中间切下来的，box 边界不一定对齐；用 moov 标记定位
        offset = 0 if blob is head else max(blob.find(b"moov") - 4, 0)
        moov = _find_box(blob, (b"moov",), offset, len(blob))
        if moov is None:
            continue
        m_start, m_end = moov
        duration: float | None = None
        mvhd = _find_box(blob, (b"mvhd",), m_start, m_end)
        if mvhd is not None:
            duration = _parse_mvhd(blob[mvhd[0] : mvhd[1]])
        for kind, body, stop in _iter_boxes(blob, m_start, m_end):
            if kind != b"trak":
                continue
            tkhd = _find_box(blob, (b"tkhd",), body, stop)
            if tkhd is None:
                continue
            size_pair = _parse_tkhd(blob[tkhd[0] : tkhd[1]])
            if size_pair is None:  # 音轨的 tkhd 宽高为 0，跳过
                continue
            return VideoInfo(size_pair[0], size_pair[1], duration, source="mp4")
    return None


def _read_ffprobe_info(path: Path) -> VideoInfo | None:
    """退路：调 ``ffprobe``。没装就返回 ``None``，不当作错误。"""
    binary = shutil.which("ffprobe")
    if binary is None:
        return None
    try:
        # 参数全是常量 + 一个本地路径，不经 shell，不存在注入面
        proc = subprocess.run(
            [
                binary,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height:format=duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout or "{}")
    except ValueError:
        return None
    streams = payload.get("streams") or []
    if not streams:
        return None
    stream = streams[0]
    try:
        width, height = int(stream["width"]), int(stream["height"])
    except (KeyError, TypeError, ValueError):
        return None
    duration: float | None = None
    raw_duration = (payload.get("format") or {}).get("duration")
    try:
        duration = float(raw_duration) if raw_duration is not None else None
    except (TypeError, ValueError):
        duration = None
    return VideoInfo(width, height, duration, source="ffprobe")


def read_video_info(path: str | Path) -> VideoInfo | None:
    """读视频的分辨率与时长，读不出返回 ``None``。

    刻意不引入 ffmpeg-python / moviepy 一类的依赖：**标准库先行**（MP4 box 解析），
    装了 ``ffprobe`` 才用它兜底。和 :func:`read_image_size` 同一个态度——
    量不出来就是"没量到"，由调用方降级成 warn，不会变成 block。
    """
    target = Path(path)
    if not target.is_file():
        return None
    return _read_mp4_info(target) or _read_ffprobe_info(target)


def iter_image_prompts(bundle: ContentBundle) -> list[str]:
    """取这条内容的配图 prompt（P11）。

    两处来源合并去重：写稿链留在 ``platform_extra.image_prompts`` 的原始 prompt，
    以及生图流水 ``platform_extra.illustrations[].prompt``（真正发出去的那条）。
    机器审核要扫的是**真正发出去的那条**，但两处都扫更保险——prompt 本身也不许踩红线。
    """
    extra = bundle.platform_extra or {}
    found: list[str] = []
    seen: set[str] = set()
    sources: list[Any] = [extra.get("image_prompts")]
    illustrations = extra.get("illustrations")
    if isinstance(illustrations, list):
        for entry in illustrations:
            if isinstance(entry, dict):
                sources.append([entry.get("prompt"), entry.get("revised_prompt")])
    for source in sources:
        if not isinstance(source, list):
            continue
        for item in source:
            text = str(item or "").strip()
            if text and text not in seen:
                seen.add(text)
                found.append(text)
    return found


def _inspect_illustrations(
    bundle: ContentBundle, images: list[MediaAsset], root: Path
) -> tuple[list[Finding], dict[str, Any]]:
    """生图配图专属校验（P11）：文件在不在、尺寸量不量得到、prompt 有没有踩明面红线。

    文件缺失已由通用规则 block，这里只补生图特有的三件事，且**一律不 block**：
    配图是锦上添花，缺一张不该让整条内容发不出去（缺到一张图都没有时，
    平台侧的 ``xhs.image.missing`` / ``douyin.cover.missing`` 会接手）。
    """
    findings: list[Finding] = []
    extra = bundle.platform_extra or {}
    entries = extra.get("illustrations")
    entries = [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
    metrics: dict[str, Any] = {"illustration_count": len(entries)}
    if not entries:
        return findings, metrics

    known = {asset.path for asset in images}
    # 题图（``role`` 非空）是**底图**，由 render_cover_set 烤进封面里，本来就不该自己
    # 出现在 media——公众号 `role="hero"`、抖音 `role="cover"` 都是这么用的（见
    # generation/pipeline.py 与 generation/video_pipeline.py 里传给 `background=` 的那一路）。
    # 小红书的配图没有 role、带 final_path，是真交付物，仍按"必须在 media 里"判。
    # 但底图**不是无条件豁免**：封面没渲出来时（缺 Playwright，cover_paths 为空）
    # 这张图就是白生成的，那时照报——所以豁免挂在"确实有封面落地"这个前提上。
    cover_shipped = any(asset.cover for asset in images)
    missing: list[str] = []
    dropped: list[str] = []
    unmeasured: list[str] = []
    for entry in entries:
        path = str(entry.get("final_path") or entry.get("path") or "")
        if not path:
            continue
        if entry.get("role"):
            if not cover_shipped:
                dropped.append(path)
            continue
        if path not in known:
            # 生成了但没挂进 media：多半是裁切失败后路径变了，人要能看见
            missing.append(path)
            continue
        candidate = Path(path)
        target = candidate if candidate.is_absolute() else root / candidate
        if target.is_file() and read_image_size(target) is None:
            unmeasured.append(path)
    if missing:
        findings.append(
            _finding(
                "warn",
                "illustration.orphan",
                f"有 {len(missing)} 张配图记录没有出现在 media 里：{', '.join(missing)}",
                excerpt=missing[0],
            )
        )
    if dropped:
        findings.append(
            _finding(
                "warn",
                "illustration.dropped",
                f"题图生成了但封面没渲出来，这 {len(dropped)} 张图不会出现在成品里："
                f"{', '.join(dropped)}",
                excerpt=dropped[0],
            )
        )
    if unmeasured:
        findings.append(
            _finding(
                "warn",
                "illustration.unreadable",
                f"读不出这些配图的尺寸，人工确认一下是不是坏文件：{', '.join(unmeasured)}",
                excerpt=unmeasured[0],
            )
        )
    metrics["illustration_orphans"] = len(missing)
    metrics["illustration_dropped"] = len(dropped)

    # prompt 里明面上的红线：真人 / 品牌 / 文字水印。词库级扫描在 review.pipeline 里另有一道
    risky: list[str] = []
    for prompt in iter_image_prompts(bundle):
        hit = _RISKY_PROMPT.search(prompt)
        if hit:
            risky.append(f"{hit.group(0)}（{prompt[:40]}…）")
    if risky:
        findings.append(
            _finding(
                "warn",
                "illustration.prompt_risky",
                (
                    "配图 prompt 里出现了容易踩合规红线的要素"
                    f"（真人面孔 / 品牌 logo / 画面文字）：{'; '.join(risky)}"
                ),
                excerpt=risky[0],
            )
        )
    metrics["illustration_risky_prompts"] = len(risky)
    return findings, metrics


def iter_image_urls(bundle: ContentBundle) -> list[str]:
    """正文（Markdown 与 HTML）里引用的所有图片地址。"""
    urls: list[str] = []
    for pattern, text in (
        (_MD_IMAGE, bundle.body_markdown or ""),
        (_IMG_SRC, bundle.body_html or ""),
    ):
        urls.extend(match.group(1) for match in pattern.finditer(text))
    return urls


def _inspect_xhs(
    bundle: ContentBundle,
    images: list[MediaAsset],
    root: Path,
) -> tuple[list[Finding], dict[str, Any]]:
    """小红书专属校验：正文上限、图片张数与尺寸、话题标签。

    这些都是**平台会直接拒收或截断**的硬规则，和"能不能说"的内容审核无关。
    """
    findings: list[Finding] = []
    body = bundle.body_markdown or ""

    # -- 正文上限（话题标签算正文字数）--------------------------------------
    if len(body.strip()) > MAX_XHS_BODY_CHARS:
        findings.append(
            _finding(
                "block",
                "xhs.body.too_long",
                f"正文（含话题标签）{len(body.strip())} 字，超过 {MAX_XHS_BODY_CHARS} 字上限",
            )
        )

    # -- 图片张数 -----------------------------------------------------------
    min_images, max_images = XHS_IMAGE_RANGE
    if len(images) < min_images:
        findings.append(
            _finding(
                "block",
                "xhs.image.missing",
                "图文笔记至少要有 1 张图。先跑 generation.xhs_cards.render_cards 生成卡片",
            )
        )
    elif len(images) > max_images:
        findings.append(
            _finding(
                "block",
                "xhs.image.too_many",
                f"{len(images)} 张图，超过平台上限 {max_images} 张",
            )
        )
    videos = [asset for asset in bundle.media if asset.kind == "video"]
    if videos and images:
        findings.append(
            _finding(
                "warn",
                "xhs.media.mixed",
                "同一条笔记里既有图片又有视频，平台只会按其中一种类型发布",
            )
        )

    # -- 单图尺寸与体积 -----------------------------------------------------
    low, high = XHS_ASPECT_RANGE
    oversize: list[str] = []
    off_ratio: list[str] = []
    measured = 0
    for asset in images:
        if _HTTP_URL.match(asset.path):
            continue  # 已上传到平台，量不了也不需要量
        path = Path(asset.path)
        candidate = path if path.is_absolute() else root / path
        if not candidate.is_file():
            continue  # 缺文件已由通用规则 block，不重复报
        if candidate.stat().st_size > MAX_XHS_IMAGE_BYTES:
            oversize.append(f"{asset.path}（{candidate.stat().st_size // 1024 // 1024}MB）")
            continue
        size = read_image_size(candidate)
        if size is None:
            continue
        measured += 1
        width, height = size
        if max(width, height) > MAX_XHS_IMAGE_EDGE:
            oversize.append(f"{asset.path}（{width}×{height}）")
        elif height and not (low <= width / height <= high):
            off_ratio.append(f"{asset.path}（{width}×{height}）")
    if oversize:
        findings.append(
            _finding(
                "block",
                "xhs.image.oversize",
                (
                    f"以下图片超过平台上限（长边 {MAX_XHS_IMAGE_EDGE}px / "
                    f"{MAX_XHS_IMAGE_BYTES // 1024 // 1024}MB）：{', '.join(oversize)}"
                ),
                excerpt=oversize[0],
            )
        )
    if off_ratio:
        findings.append(
            _finding(
                "warn",
                "xhs.image.aspect",
                f"以下图片不在 3:4 ~ 4:3 区间，会被平台裁切：{', '.join(off_ratio)}",
                excerpt=off_ratio[0],
            )
        )

    # -- 话题标签 -----------------------------------------------------------
    tags = [str(t).strip() for t in bundle.tags if str(t).strip()]
    min_tags, max_tags = XHS_TAG_RANGE
    if len(tags) > MAX_XHS_TAGS:
        findings.append(
            _finding(
                "block",
                "xhs.tags.too_many",
                f"{len(tags)} 个话题标签，超过平台收录上限 {MAX_XHS_TAGS} 个",
            )
        )
    elif not (min_tags <= len(tags) <= max_tags):
        findings.append(
            _finding(
                "warn",
                "xhs.tags.count",
                f"话题标签 {len(tags)} 个，建议 {min_tags}–{max_tags} 个",
            )
        )
    malformed = [t for t in tags if _BAD_TAG_CHARS.search(t)]
    if malformed:
        findings.append(
            _finding(
                "warn",
                "xhs.tags.malformed",
                f"话题标签不能带 # 或空格/分隔符：{', '.join(malformed)}",
                excerpt=malformed[0],
            )
        )
    if len(set(tags)) != len(tags):
        findings.append(_finding("warn", "xhs.tags.duplicate", "话题标签有重复，去重后再发"))

    metrics = {
        "xhs_tag_count": len(tags),
        "xhs_measured_images": measured,
        "xhs_oversize_images": len(oversize),
    }
    return findings, metrics


def _inspect_douyin(
    bundle: ContentBundle,
    images: list[MediaAsset],
    root: Path,
) -> tuple[list[Finding], dict[str, Any]]:
    """抖音专属校验：成片存在 / 时长 / 9:16 / 封面 / 文案与话题。

    时长与分辨率量不出来时只 warn，不 block —— 见 :func:`read_video_info` 的注释。
    """
    findings: list[Finding] = []
    body = (bundle.body_markdown or "").strip()
    videos = [asset for asset in bundle.media if asset.kind == "video"]
    metrics: dict[str, Any] = {"douyin_video_count": len(videos)}

    # -- 成片 ---------------------------------------------------------------
    if not videos:
        findings.append(
            _finding(
                "block",
                "douyin.video.missing",
                "没有成片：抖音只能发视频。先跑 generation.video_pipeline 渲染，"
                "或用 skip_render=true 挂一个样本片走联调",
            )
        )
    elif len(videos) > 1:
        findings.append(
            _finding(
                "block",
                "douyin.video.too_many",
                f"有 {len(videos)} 个视频，一条作品只能有 1 个成片",
            )
        )

    for asset in videos[:1]:
        if _HTTP_URL.match(asset.path):
            continue  # 已在平台侧，量不了也不需要量
        path = Path(asset.path)
        candidate = path if path.is_absolute() else root / path
        if not candidate.is_file():
            continue  # 缺文件已由通用规则 block，不重复报
        if candidate.suffix.lower() not in VIDEO_EXTS:
            findings.append(
                _finding(
                    "block",
                    "douyin.video.format",
                    f"不支持的视频格式 {candidate.suffix}，允许 {sorted(VIDEO_EXTS)}",
                    excerpt=asset.path,
                )
            )
        size_bytes = candidate.stat().st_size
        metrics["douyin_video_bytes"] = size_bytes
        if size_bytes > MAX_DOUYIN_VIDEO_BYTES:
            findings.append(
                _finding(
                    "block",
                    "douyin.video.oversize",
                    f"成片 {size_bytes // 1024 // 1024}MB，超过 "
                    f"{MAX_DOUYIN_VIDEO_BYTES // 1024 // 1024 // 1024}GB 上限",
                    excerpt=asset.path,
                )
            )
        info = read_video_info(candidate)
        if info is None:
            findings.append(
                _finding(
                    "warn",
                    "douyin.video.unreadable",
                    "读不出成片的时长与分辨率（非 MP4 容器且本机没有 ffprobe）。"
                    "人工确认它是 9:16、时长在 15 分钟内",
                    excerpt=asset.path,
                )
            )
            return findings, metrics
        metrics["douyin_video_width"] = info.width
        metrics["douyin_video_height"] = info.height
        metrics["douyin_video_seconds"] = info.duration_s
        metrics["douyin_probe"] = info.source
        if info.duration_s is None:
            findings.append(
                _finding("warn", "douyin.video.duration_unknown", "读不出成片时长，人工确认")
            )
        elif info.duration_s > MAX_DOUYIN_VIDEO_SECONDS:
            findings.append(
                _finding(
                    "block",
                    "douyin.video.too_long",
                    f"成片 {info.duration_s:.0f} 秒，超过 "
                    f"{MAX_DOUYIN_VIDEO_SECONDS // 60} 分钟上限",
                    excerpt=asset.path,
                )
            )
        elif info.duration_s < MIN_DOUYIN_VIDEO_SECONDS:
            findings.append(
                _finding(
                    "warn",
                    "douyin.video.too_short",
                    f"成片只有 {info.duration_s:.1f} 秒，疑似渲染失败或素材没拼上",
                    excerpt=asset.path,
                )
            )
        aspect = info.aspect
        if aspect is not None and abs(aspect - DOUYIN_ASPECT) > DOUYIN_ASPECT_TOLERANCE:
            findings.append(
                _finding(
                    "block",
                    "douyin.video.aspect",
                    f"成片 {info.width}×{info.height}（比例 {aspect:.3f}），"
                    f"抖音要 9:16（{DOUYIN_ASPECT:.3f}）竖屏。重渲染时把 video_aspect 设成 9:16",
                    excerpt=asset.path,
                )
            )

    # -- 封面 ---------------------------------------------------------------
    if not images:
        findings.append(
            _finding(
                "block",
                "douyin.cover.missing",
                "没有封面图：发布器要拿它当作品封面。"
                "装好 Playwright 后重跑生成链，或人工补一张 9:16 的图",
            )
        )

    # -- 文案 ---------------------------------------------------------------
    if len(body) > MAX_DOUYIN_BODY_CHARS:
        findings.append(
            _finding(
                "block",
                "douyin.body.too_long",
                f"文案（含话题）{len(body)} 字，超过 {MAX_DOUYIN_BODY_CHARS} 字上限",
            )
        )

    # -- 话题标签 -----------------------------------------------------------
    tags = [str(t).strip() for t in bundle.tags if str(t).strip()]
    metrics["douyin_tag_count"] = len(tags)
    min_tags, max_tags = DOUYIN_TAG_RANGE
    if len(tags) > MAX_DOUYIN_TAGS:
        findings.append(
            _finding(
                "block",
                "douyin.tags.too_many",
                f"{len(tags)} 个话题，超过 {MAX_DOUYIN_TAGS} 个上限（堆无关热词会被限流）",
            )
        )
    elif not (min_tags <= len(tags) <= max_tags):
        findings.append(
            _finding(
                "warn",
                "douyin.tags.count",
                f"话题 {len(tags)} 个，建议 {min_tags}–{max_tags} 个",
            )
        )
    malformed = [t for t in tags if _BAD_TAG_CHARS.search(t)]
    if malformed:
        findings.append(
            _finding(
                "warn",
                "douyin.tags.malformed",
                f"话题不能带 # 或空格/分隔符：{', '.join(malformed)}",
                excerpt=malformed[0],
            )
        )
    return findings, metrics


def inspect(bundle: ContentBundle, *, media_root: str | Path | None = None) -> InspectReport:
    """发布前校验。不改 bundle，只报告。"""
    findings: list[Finding] = []
    root = Path(media_root) if media_root is not None else Path.cwd()
    extra = bundle.platform_extra or {}

    # -- 标题 ---------------------------------------------------------------
    title_limit = MAX_TITLE_CHARS_BY_PLATFORM.get(bundle.platform, MAX_TITLE_CHARS)
    title = str(extra.get("title") or bundle.title or "").strip()
    if not title:
        findings.append(
            _finding("block", "title.empty", f"标题为空，补一个 {title_limit} 字以内的标题")
        )
    elif len(title) > title_limit:
        findings.append(
            _finding(
                "block",
                "title.too_long",
                f"标题 {len(title)} 字，超过 {title_limit} 字上限，请精简",
                excerpt=title,
            )
        )
    if "\n" in title:
        findings.append(_finding("block", "title.newline", "标题不能包含换行", excerpt=title))

    # -- 摘要 ---------------------------------------------------------------
    digest = str(extra.get("digest") or "").strip()
    if bundle.platform == "wechat_mp":
        if not digest:
            findings.append(
                _finding("warn", "digest.empty", "摘要为空，平台会自动截取正文前 54 字")
            )
        elif len(digest) > MAX_DIGEST_CHARS:
            findings.append(
                _finding(
                    "block",
                    "digest.too_long",
                    f"摘要 {len(digest)} 字，超过 {MAX_DIGEST_CHARS} 字上限",
                    excerpt=digest,
                )
            )

    # -- 正文 ---------------------------------------------------------------
    body = bundle.body_markdown or ""
    min_body = MIN_BODY_CHARS_BY_PLATFORM.get(bundle.platform, MIN_BODY_CHARS)
    if len(body.strip()) < min_body:
        findings.append(
            _finding(
                "block",
                "body.too_short",
                f"正文只有 {len(body.strip())} 字，低于 {min_body} 字，疑似生成失败",
            )
        )
    if bundle.platform == "wechat_mp" and not (bundle.body_html or "").strip():
        findings.append(
            _finding(
                "block",
                "body_html.missing",
                "缺少 body_html：公众号需要内联样式 HTML，先跑 generation.wechat_render",
            )
        )

    # -- 媒体 ---------------------------------------------------------------
    images = [asset for asset in bundle.media if asset.kind == "image"]
    if len(bundle.media) > MAX_IMAGES:
        findings.append(
            _finding(
                "warn",
                "media.too_many",
                f"媒体 {len(bundle.media)} 个，超过建议上限 {MAX_IMAGES}",
            )
        )
    missing_local: list[str] = []
    for asset in bundle.media:
        if _HTTP_URL.match(asset.path):
            continue  # 已上传到平台，路径是 URL
        path = Path(asset.path)
        candidate = path if path.is_absolute() else root / path
        if not candidate.is_file():
            missing_local.append(asset.path)
    if missing_local:
        findings.append(
            _finding(
                "block",
                "media.missing_file",
                f"以下媒体文件在本地不存在，发布会失败：{', '.join(missing_local)}",
                excerpt=missing_local[0],
            )
        )

    cover = bundle.cover
    if bundle.platform == "wechat_mp" and cover is None:
        findings.append(_finding("warn", "cover.missing", "没有封面图，公众号列表页会显示默认图"))

    # -- 正文外链图片 -------------------------------------------------------
    external: list[str] = []
    for url in iter_image_urls(bundle):
        if not _HTTP_URL.match(url):
            continue
        if WECHAT_IMAGE_HOST not in url:
            external.append(url)
    if external and bundle.platform == "wechat_mp":
        findings.append(
            _finding(
                "block",
                "image.external_host",
                (
                    f"正文含 {len(external)} 张外链图片，公众号会过滤掉。"
                    f"必须先经素材库换成 {WECHAT_IMAGE_HOST} 域名"
                ),
                excerpt=external[0],
            )
        )

    # -- 平台字段完整性 -----------------------------------------------------
    required = REQUIRED_EXTRA.get(bundle.platform, ())
    missing_fields = [name for name in required if not str(extra.get(name) or "").strip()]
    if missing_fields:
        level = "warn" if missing_fields == ["author"] else "block"
        findings.append(
            _finding(
                level,
                "platform_extra.missing",
                f"platform_extra 缺字段：{', '.join(missing_fields)}",
            )
        )

    # -- 各平台专属 ---------------------------------------------------------
    platform_metrics: dict[str, Any] = {}
    if bundle.platform == "xhs":
        xhs_findings, platform_metrics = _inspect_xhs(bundle, images, root)
        findings.extend(xhs_findings)
    elif bundle.platform == "douyin":
        douyin_findings, platform_metrics = _inspect_douyin(bundle, images, root)
        findings.extend(douyin_findings)

    # -- 生图配图（P11，三平台共用）------------------------------------------
    illustration_findings, illustration_metrics = _inspect_illustrations(bundle, images, root)
    findings.extend(illustration_findings)
    platform_metrics.update(illustration_metrics)

    findings = sort_findings(findings)
    metrics = {
        "title_chars": len(title),
        "digest_chars": len(digest),
        "body_chars": len(body.strip()),
        "body_html_chars": len(bundle.body_html or ""),
        "media_count": len(bundle.media),
        "image_count": len(images),
        "has_cover": cover is not None,
        "external_images": len(external),
        "missing_media": len(missing_local),
        **platform_metrics,
    }
    return InspectReport(
        ok=not any(f.level == "block" for f in findings),
        platform=bundle.platform,
        content_id=bundle.id,
        findings=findings,
        metrics=metrics,
    )


__all__ = [
    "DOUYIN_ASPECT",
    "DOUYIN_ASPECT_TOLERANCE",
    "DOUYIN_TAG_RANGE",
    "MAX_DIGEST_CHARS",
    "MAX_DOUYIN_BODY_CHARS",
    "MAX_DOUYIN_TAGS",
    "MAX_DOUYIN_TITLE_CHARS",
    "MAX_DOUYIN_VIDEO_BYTES",
    "MAX_DOUYIN_VIDEO_SECONDS",
    "MAX_IMAGES",
    "MAX_TITLE_CHARS",
    "MAX_TITLE_CHARS_BY_PLATFORM",
    "MAX_XHS_BODY_CHARS",
    "MAX_XHS_IMAGE_BYTES",
    "MAX_XHS_IMAGE_EDGE",
    "MAX_XHS_TAGS",
    "MAX_XHS_TITLE_CHARS",
    "MIN_BODY_CHARS",
    "MIN_BODY_CHARS_BY_PLATFORM",
    "MIN_DOUYIN_BODY_CHARS",
    "MIN_DOUYIN_VIDEO_SECONDS",
    "MIN_XHS_BODY_CHARS",
    "REQUIRED_EXTRA",
    "VIDEO_EXTS",
    "WECHAT_IMAGE_HOST",
    "XHS_ASPECT_RANGE",
    "XHS_IMAGE_RANGE",
    "XHS_PAGE_RANGE",
    "XHS_TAG_RANGE",
    "InspectReport",
    "VideoInfo",
    "inspect",
    "iter_image_prompts",
    "iter_image_urls",
    "read_image_size",
    "read_video_info",
]
