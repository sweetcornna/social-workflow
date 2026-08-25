"""生图客户端：OpenAI Images API 兼容端点（``POST {base}/images/generations``）。

设计取向（与 :mod:`generation.llm` 的异常分类、:mod:`generation.cover` 的
"不可用就降级不抛异常"对齐，差异都在这里写清楚）：

- **provider 无关**：只依赖 OpenAI Images API 的线协议，换网关只改
  ``SW_IMAGEGEN_BASE_URL``。默认模型 ``gpt-image-2``（用户拍板），
  ``gpt-image-1`` / ``gpt-image-1.5`` 作为可配回退。
- **独立 key 与独立预算**：生图走 ``SW_IMAGEGEN_API_KEY``（网关按 key 分组授权，
  图像生成是单独开关），记账走 :attr:`core.budget.CostKind.IMAGES`——按**张**计，
  不混进写稿的 token 预算。模型上报的 token 用量作为观测字段留在流水 ``meta`` 里。
- **size 参数本网关不认**（2026-08-24 复验，别绕过这条）：同一条裸 prompt 请求
  ``1024x1536`` 两次分别实返 ``1122x1402``（0.800）与 ``1254x1254``（1.000），
  请求 ``1536x1024`` 实返 ``1254x1254``（1.000）——**画幅是模型自己挑的，而且不稳定**。
  所以 :class:`GeneratedImage` 的 ``width/height`` 一律来自
  :func:`review.inspect.read_image_size` 读出来的 **PNG IHDR 实际值**，
  请求值只留在 ``requested_size`` 里做对照。下游要精确尺寸仍然得自己合成，
  见 :func:`fit_to_canvas`。
- **要什么形状就写进 prompt**：唯一有效的杠杆是提示词前缀，而且非常有效
  （同一条 prompt 加 3:4 指令实返 ``1086x1448``=0.750，9:16 实返 ``941x1672``=0.563，
  16:9 实返 ``1672x941``=1.777）。这层翻译是 :class:`AspectSpec`——**生出来就是对的
  形状**，比生完再 :func:`fit_to_canvas` 裁掉半张画面强。
- **失败一律降级**：权限未开（``ImagegenNotEnabled``）、预算拒绝、网关抖动
  都不该阻塞出稿主链。调用方 catch :class:`ImagegenError` +
  :class:`~core.budget.BudgetExhausted`，记 warning 后继续出无配图的稿。
- **auto 模式不做启动探测**：探一次就是一张图的钱。首次调用失败会在**本进程内**
  标记不可用（:func:`mark_unavailable`），后续调用直接跳过，不反复烧钱试。

红线：prompt 里不写真人姓名 / 名人 / 品牌 logo，见 ``prompts/imagegen.md``。
"""

from __future__ import annotations

import base64
import binascii
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from core.budget import BudgetExhausted, BudgetGuard, CostKind

logger = logging.getLogger("social_workflow.generation.imagegen")

#: 上游路径。base_url 已经含 ``/v1`` 时不再重复拼
IMAGES_PATH = "/images/generations"

#: 权限没开时给人看的修复指引。客户端与 preflight 共用，避免两处说法不一致
ENABLE_HINT = "在 Sub2API 后台给这把 key 所在的分组开启「图像生成」权限（与聊天权限是两个开关）"

#: 各用途的**请求**尺寸。
#:
#: **本网关实测不认这个参数**（见模块 docstring 的复验数字）——它既不照办也不报错，
#: 画幅完全由模型自己挑。保留它有两个理由：换一台照办的网关时它仍然正确，
#: 而且它是"我们想要什么"这条信息的唯一声明处，删了就丢了。
#: 想让**本网关**出对形状，靠的是 :class:`AspectSpec` 那句提示词指令，不是这里。
SIZE_PORTRAIT = "1024x1536"
SIZE_LANDSCAPE = "1536x1024"
SIZE_SQUARE = "1024x1024"


@dataclass(frozen=True)
class AspectSpec:
    """一个目标画幅：同一件事说两遍——``size`` 给协议，``directive`` 给模型。

    为什么要说两遍：``size`` 是 OpenAI Images 线协议里的正规字段，换网关时它是对的；
    但本网关吞掉它，只有写进 prompt 的画幅要求才生效（实测数字见模块 docstring）。
    两条都发，哪台网关都不亏。

    指令**前置**（不是追加）：写稿链给的 prompt 常以句号结尾，构图要求跟在后面
    容易被当成又一句画面描述。前置那版是实测过的，追加那版没测——别顺手改成追加。
    指令用英文，和 ``prompts/imagegen.md`` 里配图 prompt 一律英文是同一个约定。
    """

    #: 审计与日志里的短名，也是测试里认人的凭据
    key: str
    #: 目标 宽/高。调用方据此判断"拿到的图偏了多少"
    ratio: float
    #: 请求体里的 size（本网关不认，见上）
    size: str
    #: 前置到 prompt 最前面的画幅指令。**自带尾空格**，否则会和原 prompt 粘在一起
    directive: str

    def apply(self, prompt: str) -> str:
        """把画幅指令前置到 ``prompt`` 前面。已经带了就原样返回。

        幂等是给重试留的：同一条 prompt 被重新 apply 一次不该叠出两句指令。
        """
        if prompt.lstrip().startswith(self.directive.strip()):
            return prompt
        return f"{self.directive}{prompt}"


#: 小红书内页配图。笔记本体是 3:4 竖版文字卡（``generation.xhs_cards.CARD_SIZE``），
#: 配图跟着竖，否则一条竖版笔记里混进横图——而横图的比例往往还落在平台容忍区间内，
#: 谁都不会报错，只是难看
ASPECT_PORTRAIT_3_4 = AspectSpec(
    key="portrait_3_4",
    ratio=3 / 4,
    size=SIZE_PORTRAIT,
    directive="vertical portrait orientation, 3:4 aspect ratio (taller than wide). ",
)

#: 公众号题图。一张底图要同时喂 900×383（2.35:1）与 900×900（1:1）两张封面，
#: 两边都是 ``background-size: cover`` 居中裁。3:2 是这两个目标的几何中项
#: （√2.35≈1.53），两张封面裁掉的画面最平均——偏 16:9 会把方图版裁得太狠
ASPECT_LANDSCAPE_3_2 = AspectSpec(
    key="landscape_3_2",
    ratio=3 / 2,
    size=SIZE_LANDSCAPE,
    directive="horizontal landscape orientation, 3:2 aspect ratio (wider than tall). ",
)

#: 抖音封面 1080×1920 全出血竖屏。横图喂进去只剩 37.5% 的画面
ASPECT_VERTICAL_9_16 = AspectSpec(
    key="vertical_9_16",
    ratio=9 / 16,
    size=SIZE_PORTRAIT,
    directive="vertical portrait orientation, 9:16 aspect ratio (much taller than wide). ",
)

#: 方图。preflight 探测这种"只是想看看能不能出图"的场合用
ASPECT_SQUARE_1_1 = AspectSpec(
    key="square_1_1",
    ratio=1.0,
    size=SIZE_SQUARE,
    directive="square composition, 1:1 aspect ratio (equal width and height). ",
)


def apply_aspect(prompt: str, aspect: AspectSpec | None) -> str:
    """按画幅改写 prompt；``aspect=None`` 时原样返回。

    单独给个函数是为了让测试替身能复用**同一份**实现——替身自己拼指令，
    早晚会和真客户端漂开，那种测试是假的。
    """
    return prompt if aspect is None else aspect.apply(prompt)


def resolve_request_size(size: str | None, aspect: AspectSpec | None) -> str:
    """请求体里的 ``size``：显式给的优先，其次画幅规格自带的，最后竖版兜底。"""
    if size:
        return size
    return aspect.size if aspect is not None else SIZE_PORTRAIT


# --------------------------------------------------------------------- 异常


class ImagegenError(Exception):
    """生图层异常基类。上层 catch 这个就够了。"""


class ImagegenUnavailable(ImagegenError):
    """前置条件缺失：没配 key、开关关掉、或本会话已被标记不可用。"""


class ImagegenNotEnabled(ImagegenUnavailable):
    """网关返回 ``permission_error``：key 能用，但分组没开图像生成。

    单独分出来是因为**处置方式不同**——这不是抖动，重试一万次也没用，
    要人去后台点一个开关。所以它会触发会话级熔断，并把 :data:`ENABLE_HINT` 带出去。
    """


class ImagegenRateLimited(ImagegenError):
    """429，可退避重试。"""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ImagegenAPIError(ImagegenError):
    """其它非 2xx，或 2xx 但响应体读不出图。"""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ImagegenConnectionError(ImagegenError):
    """网络层失败，没拿到响应。"""


# ------------------------------------------------------------- 会话级可用性

_state_lock = threading.Lock()
_unavailable_reason: str | None = None


def mark_unavailable(reason: str) -> None:
    """标记本进程内生图不可用。幂等，只记第一条原因（第一条才是根因）。"""
    global _unavailable_reason
    with _state_lock:
        if _unavailable_reason is None:
            _unavailable_reason = reason
            logger.warning("生图已在本会话内标记为不可用：%s", reason)


def unavailable_reason() -> str | None:
    """本会话被熔断的原因；``None`` = 没被熔断。"""
    with _state_lock:
        return _unavailable_reason


def reset_availability() -> None:
    """清掉熔断标记。测试与"改完配置重试"用。"""
    global _unavailable_reason
    with _state_lock:
        _unavailable_reason = None


@dataclass(frozen=True)
class ImagegenStatus:
    """生图可用性快照，给 ``GET /api/v1/system/imagegen`` 与 preflight 共用。"""

    #: 配置层面允许尝试（开关没关 + 有 key + 没被熔断）
    ready: bool
    enabled: str
    model: str
    base_url: str
    has_api_key: bool
    #: 不 ready 时的人话原因；ready 时为空
    reason: str = ""
    hint: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "enabled": self.enabled,
            "model": self.model,
            "base_url": self.base_url,
            "has_api_key": self.has_api_key,
            "reason": self.reason,
            "hint": self.hint,
        }


def resolve_base_url(settings: Any = None) -> str:
    """生图端点。留空则复用 dsh 那条网关地址（同一个网关，只是换一把 key）。"""
    if settings is None:
        from core.config import get_settings

        settings = get_settings()
    base = (settings.sw_imagegen_base_url or "").strip()
    return (base or (settings.sw_dsh_deepseek_base_url or "").strip()).rstrip("/")


def resolve_api_key(settings: Any = None) -> str:
    """生图 key。留空回落到聊天 key——多半没图像权限，首调就会如实报出来。"""
    if settings is None:
        from core.config import get_settings

        settings = get_settings()
    return (settings.sw_imagegen_api_key or "").strip() or (settings.deepseek_api_key or "").strip()


def imagegen_status(settings: Any = None) -> ImagegenStatus:
    """算一次可用性。**不发任何网络请求**（auto 模式刻意不做启动探测）。"""
    if settings is None:
        from core.config import get_settings

        settings = get_settings()
    enabled = settings.sw_imagegen_enabled
    base_url = resolve_base_url(settings)
    key = resolve_api_key(settings)
    common = {
        "enabled": enabled,
        "model": settings.sw_imagegen_model,
        "base_url": base_url,
        "has_api_key": bool(key),
    }
    if enabled == "false":
        return ImagegenStatus(
            ready=False,
            reason="SW_IMAGEGEN_ENABLED=false：配图功能被显式关掉了",
            **common,  # type: ignore[arg-type]
        )
    if not key:
        return ImagegenStatus(
            ready=False,
            reason="没配 SW_IMAGEGEN_API_KEY（也没有可回落的 DEEPSEEK_API_KEY）",
            hint="把生图专用 key 写进 core 那台机器的 .env：SW_IMAGEGEN_API_KEY=sk-…",
            **common,  # type: ignore[arg-type]
        )
    if not base_url:
        return ImagegenStatus(
            ready=False,
            reason="没配生图端点（SW_IMAGEGEN_BASE_URL / SW_DSH_DEEPSEEK_BASE_URL 都是空的）",
            **common,  # type: ignore[arg-type]
        )
    reason = unavailable_reason()
    if reason and enabled == "auto":
        return ImagegenStatus(
            ready=False,
            reason=f"本次运行里已经失败过一次，已停止重试：{reason}",
            hint=ENABLE_HINT if ENABLE_HINT in reason else "改完配置重启 core，或跑一次 preflight",
            **common,  # type: ignore[arg-type]
        )
    return ImagegenStatus(ready=True, **common)  # type: ignore[arg-type]


def imagegen_ready(settings: Any = None) -> bool:
    """配置齐备且没被熔断。管线用它决定要不要走生图这一步。"""
    return imagegen_status(settings).ready


# ----------------------------------------------------------------------- DTO


@dataclass(frozen=True)
class ImageUsage:
    """一次生图调用的 token 用量。

    网关按 input/output 分桶上报，output 侧就是 image_tokens。这些数字**不进**
    token 预算（那是写稿的额度），只作为观测字段落进 ``CostLedger.meta``。
    """

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    def as_meta(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens or (self.input_tokens + self.output_tokens),
        }


@dataclass
class GeneratedImage:
    """一张生成好的图。

    ``width`` / ``height`` 是**读文件头量出来的真实尺寸**，不是请求的 size；
    量不出来（非 PNG/JPEG）时为 ``None``，调用方按"没量到"处理。
    """

    path: Path
    requested_size: str
    model: str
    prompt: str
    width: int | None = None
    height: int | None = None
    revised_prompt: str = ""
    usage: ImageUsage = field(default_factory=ImageUsage)
    bytes_len: int = 0

    @property
    def measured(self) -> bool:
        return self.width is not None and self.height is not None

    @property
    def aspect(self) -> float | None:
        """宽 / 高。量不出来或高为 0 时返回 ``None``。"""
        if self.width is None or not self.height:
            return None
        return self.width / self.height

    @property
    def size_text(self) -> str:
        return f"{self.width}×{self.height}" if self.measured else "未量到"

    def as_meta(self) -> dict[str, Any]:
        """写进 ``platform_extra.illustrations`` 的审计字段。"""
        return {
            "path": str(self.path),
            "prompt": self.prompt,
            "revised_prompt": self.revised_prompt,
            "model": self.model,
            "requested_size": self.requested_size,
            "actual_size": [self.width, self.height] if self.measured else None,
            "bytes": self.bytes_len,
            **self.usage.as_meta(),
        }


# ------------------------------------------------------------------- 响应解析


def _error_payload(response: httpx.Response) -> dict[str, Any]:
    """把错误响应体解析成 ``{"type","code","message"}``，解析不了就用原文兜底。"""
    try:
        body = response.json()
    except ValueError:
        return {"type": "", "code": "", "message": response.text[:300]}
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return {"type": "", "code": "", "message": str(body)[:300]}
    return {
        "type": str(error.get("type") or ""),
        "code": str(error.get("code") or ""),
        "message": str(error.get("message") or "")[:300],
    }


def _is_permission_error(payload: dict[str, Any], status_code: int) -> bool:
    """权限未开。网关的措辞不统一，所以 type / code 都认，403 也算。"""
    marker = f"{payload.get('type', '')}|{payload.get('code', '')}".lower()
    if "permission" in marker or "insufficient_permission" in marker:
        return True
    # 403 且没给出别的分类时按权限问题处理：重试没意义，让人去开开关
    return status_code == 403


def _usage_from(raw: Any) -> ImageUsage:
    """网关的 usage 字段名不统一（prompt_tokens / input_tokens 都见过），都认。"""
    if not isinstance(raw, dict):
        return ImageUsage()

    def pick(*names: str) -> int:
        for name in names:
            value = raw.get(name)
            if isinstance(value, int):
                return value
        return 0

    return ImageUsage(
        input_tokens=pick("input_tokens", "prompt_tokens"),
        output_tokens=pick("output_tokens", "completion_tokens", "image_tokens"),
        total_tokens=pick("total_tokens"),
    )


# --------------------------------------------------------------- prompt 规范

#: 单条生图 prompt 的字数上限。超了多半是模型把正文抄了进去，截断即可
MAX_IMAGE_PROMPT_CHARS = 600
#: 不需要配图时塞给写稿 prompt 的那句话。仍然要给，否则模型看不懂 ``image_prompts`` 是什么
NO_IMAGE_RULES = "本次**不需要**配图：`image_prompts` 直接给空数组 `[]`，不要写任何内容。"


def image_prompt_rules(count: int) -> str:
    """写稿 prompt 里 ``{{image_rules}}`` 那一段。``count <= 0`` 时是"不要配图"。

    规范正文放在 ``prompts/imagegen.md``——prompt 是版本化资产，改动要出 git diff，
    不在代码里拼长字符串（见 ``prompts/__init__.py`` 的模块 docstring）。
    """
    if count <= 0:
        return NO_IMAGE_RULES
    import prompts

    return prompts.load("imagegen", count=count)


def normalize_image_prompts(raw: Any, *, count: int) -> tuple[list[str], list[str]]:
    """清洗模型给的配图 prompt，返回 ``(prompts, warnings)``。

    模型少给、多给、给重复、给成一整段正文都是常态，所以这里做兜底而不是只靠 prompt 约束
    （和 :func:`generation.xhs_note.normalize_pages` 同一个态度）。
    """
    warnings: list[str] = []
    if count <= 0:
        return [], warnings
    if not isinstance(raw, list):
        return [], [f"模型没给 image_prompts（拿到 {type(raw).__name__}）"]

    cleaned: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = " ".join(str(item).split())
        if not text:
            continue
        if len(text) > MAX_IMAGE_PROMPT_CHARS:
            warnings.append(
                f"有一条配图 prompt {len(text)} 字符，已截断到 {MAX_IMAGE_PROMPT_CHARS}"
            )
            text = text[:MAX_IMAGE_PROMPT_CHARS]
        key = text.lower()
        if key in seen:
            warnings.append("模型给了重复的配图 prompt，已去重")
            continue
        seen.add(key)
        cleaned.append(text)

    if len(cleaned) > count:
        cleaned = cleaned[:count]
    elif len(cleaned) < count:
        warnings.append(f"模型只给了 {len(cleaned)} 条配图 prompt，少于需要的 {count} 条")
    return cleaned, warnings


# ----------------------------------------------------------------- 尺寸合成


def measure(path: str | Path) -> tuple[int, int] | None:
    """读图片真实尺寸。复用 :func:`review.inspect.read_image_size`（PNG IHDR / JPEG SOF）。

    单独包一层是为了让"必须读实际尺寸"这条规则在生图这一侧有个明确的名字，
    并且下游不必知道它来自 review 层。
    """
    from review.inspect import read_image_size

    return read_image_size(path)


def _canvas_html(data_uri: str, width: int, height: int) -> str:
    """全出血背景图 HTML。``background-size: cover`` = 等比放大后**居中裁切**。

    刻意用 CSS 而不是 Pillow：项目已经有 Playwright 截图这条成熟通路（封面与小红书
    卡片都走它），裁切逻辑交给浏览器意味着**不必新增带 C 扩展的图像依赖**，
    也不必进 THIRD_PARTY 台账。图片以 data: URI 内嵌，HTML 自包含、截图时不联网。
    """
    return (
        "<!doctype html><html><head><meta charset='utf-8'><style>"
        "*{margin:0;padding:0;box-sizing:border-box}"
        f"html,body{{width:{width}px;height:{height}px;overflow:hidden;background:#000}}"
        f".p{{width:{width}px;height:{height}px;"
        f"background-image:url('{data_uri}');"
        "background-size:cover;background-position:center;background-repeat:no-repeat}"
        "</style></head><body><div class='p'></div></body></html>"
    )


def fit_to_canvas(
    source: str | Path,
    target: str | Path,
    width: int,
    height: int,
    *,
    screenshotter: Any | None = None,
) -> Path | None:
    """把生成图**居中裁切**成精确的 ``width×height`` PNG。失败返回 ``None``。

    存在的理由就是"size 参数不可信"：网关给什么尺寸都不奇怪，而平台对图片比例
    是有硬要求的（小红书 3:4~4:3、抖音封面 9:16），所以落库前必须自己合成一次。

    截图不可用（没装 Playwright / chromium）时返回 ``None`` 而不是抛异常——
    调用方会退回用原图并记 warning，缺一次裁切不该让整条稿子作废。
    """
    from generation.cover import ScreenshotUnavailable, screenshot_html

    src = Path(source)
    dest = Path(target)
    try:
        payload = src.read_bytes()
    except OSError as exc:
        logger.warning("读不到待裁切的图 %s：%s", src, exc)
        return None

    suffix = src.suffix.lower()
    mime = "image/jpeg" if suffix in (".jpg", ".jpeg") else "image/png"
    data_uri = f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"
    html = _canvas_html(data_uri, width, height)

    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        if screenshotter is not None:
            screenshotter(html, dest, width, height)
        else:
            screenshot_html(html, dest, width, height)
    except ScreenshotUnavailable as exc:
        logger.warning("跳过尺寸合成（%dx%d）：%s", width, height, exc)
        return None
    except Exception as exc:  # pragma: no cover - 兜底，配图不该拖垮生成链
        logger.warning("尺寸合成异常，跳过：%s", exc)
        return None

    # 截完再量一次：模板写死了 viewport，但"我以为它是这个尺寸"正是本模块要根除的毛病
    actual = measure(dest)
    if actual is not None and actual != (width, height):
        logger.warning("合成结果 %s 不是期望的 %dx%d，仍然采用", actual, width, height)
    return dest


# ------------------------------------------------------------------- 客户端


class ImagegenClient:
    """OpenAI Images API 兼容客户端。

    ``client`` 可注入（测试用 respx 挂 mock transport），生产环境留空由本类按
    配置构造。用完记得 :meth:`close`，或者当上下文管理器用。
    """

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        budget: BudgetGuard | None = None,
        settings: Any = None,
    ) -> None:
        if settings is None:
            from core.config import get_settings

            settings = get_settings()
        self.base_url = (base_url if base_url is not None else resolve_base_url(settings)).rstrip(
            "/"
        )
        self._api_key = api_key if api_key is not None else resolve_api_key(settings)
        self.model = model or settings.sw_imagegen_model
        self.timeout = timeout if timeout is not None else settings.sw_imagegen_timeout_seconds
        self.enabled = settings.sw_imagegen_enabled
        self.budget = budget
        self._client = client
        self._owns_client = client is None

    # -- 生命周期 ----------------------------------------------------------

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
        self._client = None

    def __enter__(self) -> ImagegenClient:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- 内部 --------------------------------------------------------------

    def _url(self) -> str:
        return f"{self.base_url}{IMAGES_PATH}"

    def _headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self._api_key}",
            "content-type": "application/json",
        }

    def _preflight(self) -> None:
        """调用前的本地闸门。失败一律是 :class:`ImagegenUnavailable` 家族。"""
        if self.enabled == "false":
            raise ImagegenUnavailable("SW_IMAGEGEN_ENABLED=false：配图功能被显式关掉了")
        if not self._api_key:
            raise ImagegenUnavailable(
                "没配 SW_IMAGEGEN_API_KEY（也没有可回落的 DEEPSEEK_API_KEY），生图不可用"
            )
        if not self.base_url:
            raise ImagegenUnavailable("没配生图端点 SW_IMAGEGEN_BASE_URL")
        reason = unavailable_reason()
        if reason and self.enabled == "auto":
            raise ImagegenUnavailable(f"本次运行里生图已经失败过，不再重试：{reason}")

    def _raise_for_status(self, response: httpx.Response) -> None:
        """非 2xx → 分类异常。权限问题额外触发会话级熔断。"""
        if response.status_code < 400:
            return
        payload = _error_payload(response)
        detail = payload["message"] or f"HTTP {response.status_code}"
        if _is_permission_error(payload, response.status_code):
            message = f"这把 key 的分组没有图像生成权限（{detail}）。{ENABLE_HINT}"
            mark_unavailable(message)
            raise ImagegenNotEnabled(message)
        if response.status_code == 401:
            message = f"生图 key 被拒（{detail}）。检查 SW_IMAGEGEN_API_KEY 是否写错或已失效"
            mark_unavailable(message)
            raise ImagegenUnavailable(message)
        if response.status_code == 429:
            retry_after: float | None = None
            raw = response.headers.get("retry-after")
            if raw:
                try:
                    retry_after = float(raw)
                except ValueError:
                    retry_after = None
            raise ImagegenRateLimited(f"生图限流：{detail}", retry_after=retry_after)
        raise ImagegenAPIError(
            f"生图返回 {response.status_code}：{detail}", status_code=response.status_code
        )

    def _image_bytes(self, entry: dict[str, Any]) -> bytes:
        """优先 ``b64_json``，没有就按 ``url`` 回落下载。"""
        raw = entry.get("b64_json")
        if isinstance(raw, str) and raw:
            try:
                return base64.b64decode(raw, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ImagegenAPIError(f"b64_json 解码失败：{exc}") from exc
        url = entry.get("url")
        if not isinstance(url, str) or not url:
            raise ImagegenAPIError("响应里既没有 b64_json 也没有 url，拿不到图")
        try:
            response = self.client.get(url, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise ImagegenConnectionError(f"下载生成图失败（{url}）：{exc}") from exc
        if response.status_code >= 400:
            raise ImagegenAPIError(
                f"下载生成图返回 {response.status_code}", status_code=response.status_code
            )
        return response.content

    # -- 对外 API ----------------------------------------------------------

    def generate(
        self,
        prompt: str,
        out_path: str | Path,
        *,
        aspect: AspectSpec | None = None,
        size: str | None = None,
        purpose: str = "illustration",
        account_id: str = "",
        platform: str = "",
    ) -> GeneratedImage:
        """生成一张图并落盘。

        ``aspect`` 是"我要什么形状"的唯一入口：它既决定请求里的 ``size``，
        也把画幅指令前置进 prompt。**只有后者对本网关有效**（见模块 docstring），
        但两条都发——换一台认 ``size`` 的网关时不必再改这里。
        显式传 ``size`` 会盖过画幅规格自带的那个，指令照旧前置。

        预算是"先问后花"：:meth:`core.budget.BudgetGuard.ensure` 在请求发出**之前**
        查额度，超了直接抛 :class:`~core.budget.BudgetExhausted`，一分钱不花；
        拿到图之后才 :meth:`~core.budget.BudgetGuard.charge` 记一张。
        """
        self._preflight()
        if self.budget is not None:
            self.budget.ensure(CostKind.IMAGES, 1)

        target = Path(out_path)
        # 从这里往下，prompt / size 一律用"真正发出去的那两个值"——留痕也记它们，
        # 否则事后拿着台账里的 prompt 复现不出同一张图
        prompt = apply_aspect(prompt, aspect)
        size = resolve_request_size(size, aspect)
        body = {
            "model": self.model,
            "prompt": prompt,
            "n": 1,
            "size": size,
            # 优先要 base64：网关给的临时 url 有有效期，多一跳就多一处会失败的地方
            "response_format": "b64_json",
        }
        try:
            response = self.client.post(
                self._url(), json=body, headers=self._headers(), timeout=self.timeout
            )
        except httpx.HTTPError as exc:
            raise ImagegenConnectionError(f"连不上生图端点 {self._url()}：{exc}") from exc
        self._raise_for_status(response)

        try:
            payload = response.json()
        except ValueError as exc:
            raise ImagegenAPIError(f"生图响应不是 JSON：{exc}") from exc
        entries = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(entries, list) or not entries:
            raise ImagegenAPIError("生图响应里 data 是空的")
        entry = entries[0] if isinstance(entries[0], dict) else {}
        blob = self._image_bytes(entry)
        if not blob:
            raise ImagegenAPIError("生图返回了 0 字节")

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)

        # 实测尺寸：请求的 size 只是个愿望，落盘后量出来的才作数
        actual = measure(target)
        image = GeneratedImage(
            path=target,
            requested_size=size,
            model=str(payload.get("model") or self.model),
            prompt=prompt,
            width=actual[0] if actual else None,
            height=actual[1] if actual else None,
            revised_prompt=str(entry.get("revised_prompt") or ""),
            usage=_usage_from(payload.get("usage")),
            bytes_len=len(blob),
        )
        if actual is not None and f"{actual[0]}x{actual[1]}" != size:
            logger.info(
                "生图实际尺寸 %s 与请求的 %s 不一致（网关行为，已按实际值处理）",
                image.size_text,
                size,
            )

        if self.budget is not None:
            self.budget.charge(
                CostKind.IMAGES,
                1,
                meta={
                    "purpose": purpose,
                    "model": image.model,
                    "account_id": account_id,
                    "platform": platform,
                    "size": size,
                    "actual_size": f"{actual[0]}x{actual[1]}" if actual else "",
                    **image.usage.as_meta(),
                },
            )
        logger.info(
            "生图完成 purpose=%s model=%s 请求=%s 实际=%s %d 字节 tokens=%d",
            purpose,
            image.model,
            size,
            image.size_text,
            image.bytes_len,
            image.usage.as_meta()["total_tokens"],
        )
        return image


def plan_illustrations(
    count: int,
    *,
    injected: ImagegenClient | None = None,
    warnings: list[str] | None = None,
) -> int:
    """决定这次到底能配几张图。配不上就返回 0 并写一句人话进 ``warnings``。

    这是三条管线**共用的降级入口**：配置不全 / 开关关掉 / 本会话已熔断时返回 0，
    调用方据此连"让模型写配图 prompt"这一步都跳过，既不烧 token 也不阻塞出稿。

    **刻意不在这里造客户端**：这个决定要在写稿之前做（prompt 里得写清楚要几张），
    而客户端要到写完稿才用得上。提前造出来，中间的文案链一抛异常就漏一个没关的
    连接池。造客户端交给 :func:`illustrator`。

    注入了客户端（测试替身）就一定认它：注入的东西比环境变量更具体。
    """
    wanted = max(int(count or 0), 0)
    if wanted <= 0:
        return 0
    if injected is not None:
        return wanted
    status = imagegen_status()
    if not status.ready:
        if warnings is not None:
            hint = f"（{status.hint}）" if status.hint else ""
            warnings.append(f"这条内容没有生成配图：{status.reason}{hint}")
        logger.info("跳过生图：%s", status.reason)
        return 0
    return wanted


@contextmanager
def illustrator(
    injected: ImagegenClient | None = None, *, budget: BudgetGuard | None = None
) -> Iterator[ImagegenClient]:
    """借一个生图客户端用，用完必关。注入的替身由调用方自己负责生命周期。"""
    client = injected if injected is not None else build_imagegen(budget=budget)
    try:
        yield client
    finally:
        if injected is None:
            client.close()


def generate_batch(
    client: ImagegenClient,
    prompt_list: list[str],
    out_dir: str | Path,
    *,
    aspect: AspectSpec | None = None,
    size: str | None = None,
    purpose: str = "illustration",
    account_id: str = "",
    platform: str = "",
    stem: str = "illustration",
    warnings: list[str] | None = None,
) -> list[GeneratedImage]:
    """按顺序生成一批图，返回**实际成功**的那些。任何失败都不抛异常。

    ``aspect`` 原样转交给 :meth:`ImagegenClient.generate`：一批图是同一条笔记
    / 同一张封面的素材，形状必须一致，所以画幅是**整批**的属性而不是每张的。

    第一张失败就**停止**后续尝试：能让第一张失败的原因（权限、预算、网关挂了）
    对后面几张同样成立，继续试只是把钱和时间烧在同一个坑里。
    """
    produced: list[GeneratedImage] = []
    directory = Path(out_dir)
    for index, prompt in enumerate(prompt_list, start=1):
        target = directory / f"{stem}-{index}.png"
        try:
            produced.append(
                client.generate(
                    prompt,
                    target,
                    aspect=aspect,
                    size=size,
                    purpose=purpose,
                    account_id=account_id,
                    platform=platform,
                )
            )
        except BudgetExhausted as exc:
            # 红线：预算耗尽不阻塞出稿主链，只是这条没有配图
            if warnings is not None:
                warnings.append(f"配图生成到第 {index} 张时当日生图预算用完了：{exc}")
            logger.warning("生图预算耗尽 purpose=%s: %s", purpose, exc)
            break
        except ImagegenError as exc:
            if warnings is not None:
                warnings.append(f"第 {index} 张配图没生成出来（后面几张已跳过）：{exc}")
            logger.warning("生图失败 purpose=%s: %s", purpose, exc)
            break
    return produced


def build_imagegen(
    *,
    budget: BudgetGuard | None = None,
    client: httpx.Client | None = None,
    **kwargs: Any,
) -> ImagegenClient:
    """按配置造一个客户端。与 :func:`generation.llm.build_llm` 同形。"""
    return ImagegenClient(budget=budget, client=client, **kwargs)


__all__ = [
    "ASPECT_LANDSCAPE_3_2",
    "ASPECT_PORTRAIT_3_4",
    "ASPECT_SQUARE_1_1",
    "ASPECT_VERTICAL_9_16",
    "ENABLE_HINT",
    "IMAGES_PATH",
    "MAX_IMAGE_PROMPT_CHARS",
    "NO_IMAGE_RULES",
    "SIZE_LANDSCAPE",
    "SIZE_PORTRAIT",
    "SIZE_SQUARE",
    "AspectSpec",
    "GeneratedImage",
    "ImageUsage",
    "ImagegenAPIError",
    "ImagegenClient",
    "ImagegenConnectionError",
    "ImagegenError",
    "ImagegenNotEnabled",
    "ImagegenRateLimited",
    "ImagegenStatus",
    "ImagegenUnavailable",
    "apply_aspect",
    "build_imagegen",
    "fit_to_canvas",
    "generate_batch",
    "illustrator",
    "image_prompt_rules",
    "imagegen_ready",
    "imagegen_status",
    "mark_unavailable",
    "measure",
    "normalize_image_prompts",
    "plan_illustrations",
    "reset_availability",
    "resolve_api_key",
    "resolve_base_url",
    "resolve_request_size",
    "unavailable_reason",
]
