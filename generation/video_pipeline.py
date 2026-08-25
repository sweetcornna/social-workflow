"""抖音短视频生成管线：选题 → 口播脚本 → MPT 渲染 → ContentBundle。

``generate_douyin_bundle(topic, account, ...)`` 是入口，和
``generate_wechat_bundle`` / ``generate_xhs_bundle`` 同构（Options / Outcome 两个
dataclass + 一个函数），但多了一件别的平台没有的事：**渲染是跨进程的长任务**。

    Claude 写脚本 ──► MPT POST /videos ──► RenderJob(task_id) 落库
                                              │
                          轮询 GET /tasks/{id}│（管线内等，或调度器接手）
                                              ▼
                                     下载成片 → 挂进 bundle.media

由此带来三条与其它平台不同的处理：

1. **task_id 必须落库**（``core.models.RenderJob``）。MPT 的任务表在它自己的进程
   内存里，容器一重启就没了；没有这行记录，重启后既拿不到成片也不知道该不该重提交。
2. **404 = 任务丢了**，不是"内容有问题"。允许**原样重提交一次**
   （:data:`MAX_RENDER_ATTEMPTS`），再丢就报错让人看。
3. **等不到不等于失败**。超过 ``MPT_RENDER_TIMEOUT_SECONDS`` 时管线**不抛异常**：
   产出一个没有视频的 bundle 照常入库（``review.inspect`` 会以
   ``douyin.video.missing`` block 掉它），RenderJob 留在 ``running``，由
   ``core.scheduler.tick_render_jobs`` 继续跟，渲染完再把成片挂回去。
   ——和小红书"没有 chromium 也要让内容进人工队列"是同一个态度。

成本：渲染时长计入 ``core.budget`` 的 ``render_seconds``。**提交前**用估算值查一次
余额，不够就直接不提交（计划里的"超预算不提交"）；**完成后**按真实墙钟耗时记账，
超出剩余额度时记满剩余并留 warning，而不是抛异常——片子已经渲出来了，
为了记账把产物扔掉是亏的。
"""

from __future__ import annotations

import logging
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

import prompts
from core.budget import BudgetExhausted, BudgetGuard, CostKind
from core.models import Account, ContentItem, RenderJob, RenderJobState, Topic, new_id
from generation.cover import ScreenshotUnavailable, render_cover_set
from generation.imagegen import (
    ASPECT_VERTICAL_9_16,
    GeneratedImage,
    generate_batch,
    illustrator,
    plan_illustrations,
)
from generation.llm import SupportsLLM, Usage
from generation.mpt_client import (
    ASPECT_PORTRAIT,
    MptClient,
    MptTask,
    MptTaskLost,
    MptVideoParams,
    build_client,
)
from generation.video_script import (
    DEFAULT_TARGET_SCRIPT_CHARS,
    VideoScriptDraft,
    generate_video_script,
)
from publishers.base import (
    ContentBundle,
    MediaAsset,
    PermanentError,
    PublishError,
    RetryableError,
)
from review.inspect import read_video_info
from sourcing.base import RawTopic

logger = logging.getLogger("social_workflow.generation.video_pipeline")

DOUYIN_PLATFORM = "douyin"
#: 生成物落盘根目录，与图文链共用（``data/media/<item_id>/``）
DEFAULT_MEDIA_ROOT = Path("data/media")
#: ``skip_render=True`` 时挂上去的样本片：2 秒 720×1280 纯色，3KB。
#: 放在 tests/fixtures 下而不是 data/：它是**测试与联调资产**，要跟着 git 走。
DEFAULT_SAMPLE_VIDEO = Path("tests/fixtures/video/sample.mp4")
#: 成片在 item 目录下的文件名
VIDEO_FILENAME = "video.mp4"
#: 封面底图用几张生图（P11）。竖版封面只有一张，多生没有用处
DEFAULT_DOUYIN_ILLUSTRATIONS = 1

#: 丢任务后允许重提交的总次数（首次提交也算一次）
MAX_RENDER_ATTEMPTS = 2
#: 渲染耗时估算：成片每 1 秒大约要渲这么多秒（含素材下载与 TTS）
RENDER_TIME_FACTOR = 4.0
#: 估算下限：起容器、拉素材的固定开销
MIN_RENDER_ESTIMATE_SECONDS = 60.0

#: 允许被"补挂成片"的内容状态。**已批准之后不再动内容**——
#: 那会让人工审核过的东西和实际发出去的东西不是一份，审计链就断了
ATTACHABLE_STATUSES = frozenset({"topic", "drafting", "draft", "reviewing", "rejected"})


class RenderTimeout(RetryableError):
    """在管线里等不到渲染结果。任务仍在 MPT 侧跑，由调度器继续跟。"""


class RenderFailed(PermanentError):
    """MPT 明确报告任务失败（``state == -1``）。"""


# ------------------------------------------------------------------ Options


@dataclass
class VideoGenerationOptions:
    """抖音生成管线开关。留 ``None`` 的字段表示"取配置默认值"。"""

    media_root: Path = DEFAULT_MEDIA_ROOT
    #: 关掉可跳过封面（无 Playwright 环境）；关掉后 inspect 会报 douyin.cover.missing
    make_cover: bool = True
    #: 不调 MPT，直接挂 :data:`DEFAULT_SAMPLE_VIDEO`。无 sidecar 环境跑通全链路用
    skip_render: bool = False
    sample_video: Path = DEFAULT_SAMPLE_VIDEO
    #: 成片比例。抖音只做竖屏，改它基本等于配错
    aspect: str = ASPECT_PORTRAIT
    video_source: str | None = None
    voice_name: str | None = None
    subtitle_enabled: bool = True
    subtitle_position: str | None = None
    clip_duration: int | None = None
    bgm_type: str | None = None
    concat_mode: str = "random"
    transition_mode: str | None = None
    target_script_chars: int = DEFAULT_TARGET_SCRIPT_CHARS
    #: 强制/禁止去 AI 味改写，None = 按自评决定
    force_rewrite: bool | None = None
    #: 封面底图用几张生图（P11）。只用第一张；0 = 老的纯色版式
    illustrations: int = DEFAULT_DOUYIN_ILLUSTRATIONS
    poll_interval: float | None = None
    render_timeout: float | None = None
    #: 注入测试替身 / 复用连接
    client: MptClient | None = None
    screenshotter: Any | None = None
    #: 注入生图客户端（测试替身）。留空则按配置构造，配置不全时静默降级
    imagegen: Any | None = None
    #: 注入假 sleep 与假时钟，让轮询在测试里瞬间跑完
    sleeper: Callable[[float], None] | None = None
    clock: Callable[[], float] | None = None


@dataclass
class VideoGenerationOutcome:
    """比 ContentBundle 更完整的产物，供审计与日志。"""

    bundle: ContentBundle
    draft: VideoScriptDraft
    usage: Usage
    warnings: list[str] = field(default_factory=list)
    video_path: Path | None = None
    cover_paths: dict[str, Path] = field(default_factory=dict)
    render_job_id: str | None = None
    task_id: str | None = None
    #: 实际墙钟渲染耗时（秒），计入 render_seconds 预算
    render_seconds: float = 0.0
    #: 成片真实时长（秒），量不出来为 None
    duration_s: float | None = None
    #: 封面底图（生图产物）。``None`` = 这次走的是纯色版式
    hero_image: GeneratedImage | None = None


# ------------------------------------------------------------------- 预算


def estimate_render_seconds(draft: VideoScriptDraft) -> float:
    """提交前的渲染耗时估算。宁可估高——估低会让预算闸门形同虚设。"""
    return max(MIN_RENDER_ESTIMATE_SECONDS, draft.estimated_seconds * RENDER_TIME_FACTOR)


def ensure_render_budget(guard: BudgetGuard | None, estimate: float) -> None:
    """余额不够就**不提交**（抛 :class:`~core.budget.BudgetExhausted`）。

    刻意在提交**之前**查：MPT 一旦开跑就是几分钟的 CPU 与素材源配额，
    提交完再发现超预算已经晚了。
    """
    if guard is None:
        return
    remaining = guard.remaining(CostKind.RENDER_SECONDS)
    if estimate > remaining:
        raise BudgetExhausted(
            CostKind.RENDER_SECONDS.value,
            estimate,
            remaining,
            guard.limit_of(CostKind.RENDER_SECONDS),
        )


def charge_render_seconds(
    guard: BudgetGuard | None,
    seconds: float,
    *,
    meta: dict[str, Any],
    warnings: list[str],
) -> None:
    """按真实耗时记账。超额时记满剩余并 warn，**不抛异常**。

    和 ``generation.llm.charge_usage`` 的差别是刻意的：token 超额时抛异常能阻止
    下一次调用，而这里片子已经渲完落盘了，抛异常只会把产物丢掉。账本仍要反映
    "已耗尽"，否则下一条内容还会被放行。
    """
    if guard is None or seconds <= 0:
        return
    remaining = guard.remaining(CostKind.RENDER_SECONDS)
    amount = min(seconds, remaining)
    if amount > 0:
        clamped = {"clamped_from": seconds} if seconds > remaining else {}
        guard.charge(CostKind.RENDER_SECONDS, amount, meta={**meta, **clamped})
    if seconds > remaining:
        warnings.append(
            f"渲染耗时 {seconds:.0f} 秒超出当日 render_seconds 剩余额度"
            f"（{remaining:.0f} 秒），已记满剩余；后续渲染会被闸门挡下"
        )


# --------------------------------------------------------------- RenderJob


def map_task_state(task: MptTask) -> str:
    """MPT 的数字状态 → 我们的 :class:`~core.models.RenderJobState`。

    映射放在这里而不是模型里：数字状态码是 MPT 的实现细节，换渲染后端时
    只该改这一个函数。
    """
    if task.done:
        return RenderJobState.DONE
    if task.failed:
        return RenderJobState.FAILED
    return RenderJobState.RUNNING


def create_render_job(
    session: Session,
    *,
    content_item_id: str,
    task_id: str,
    params: MptVideoParams,
    provider: str = "mpt",
) -> RenderJob:
    """落一行 RenderJob。``meta`` 里只放观测字段，**不放任何凭据**。"""
    job = RenderJob(
        id=new_id("rj"),
        content_item_id=content_item_id,
        provider=provider,
        task_id=task_id,
        state=RenderJobState.RUNNING,
        progress=0,
        result_paths=[],
        attempts=1,
        meta={
            "subject": params.video_subject,
            "aspect": params.video_aspect,
            "video_source": params.video_source,
            "terms": list(params.video_terms),
            "script_chars": len(params.video_script),
        },
    )
    session.add(job)
    _settle(session)
    return job


def _settle(session: Session) -> None:
    """把 ``RenderJob`` 的这一次状态写入**当场 commit**（P16.3）。两件事，缺一不可。

    1. **交出写锁**。SQLite 的写锁是整库一把，``flush`` 只把语句发出去、锁一直握到
       ``commit``。紧跟在这些写后面的是"轮询 MPT / 下载成片"——管线内的等待循环上界
       是 ``mpt_render_timeout_seconds``（默认 1800 秒），下载还另有
       ``mpt_download_timeout_seconds``（600 秒）。整段跑在锁里，同期任何别的写者
       等满 5 秒就 ``database is locked``（见 ``core/db.py``）。
    2. **让远端任务真的有主**。这张表存在的唯一理由就是"MPT 的任务表在它自己的进程
       内存里，容器一重启就没了，``task_id`` 必须由我们持久化"（见
       ``core/models.py`` 的 ``RenderJob`` 注释）。只 ``flush`` 不落盘的话，进程在轮询
       中途没了这行就跟着没了——而 MPT 那边的任务还在**真实消耗算力**，变成一个没人
       认领的孤儿：既拿不到成片，也不知道该不该重提交。

    这一处**不会造出新的卡死状态**（对比 ``publishing`` 那次）：``running`` 早就是一个
    被回收的状态，``core.scheduler.tick_render_jobs`` 每分钟扫
    ``ACTIVE_RENDER_STATES``，把完成的成片补挂回内容。崩溃残留正好落进这条现成的路。
    ``tests/test_write_lock_boundaries.py::test_c_a_submitted_render_job_survives_a_crash_and_is_picked_up_again``
    把"崩了之后真能被捞回来并挂上成片"整条钉住了。
    """
    session.commit()


def sync_render_job(session: Session, job: RenderJob, client: MptClient) -> MptTask | None:
    """轮询一次并把结果写回 ``job``，并**当场落盘**（见 :func:`_settle`）。

    被管线内的等待循环与 ``core.scheduler.tick_render_jobs`` 共用——两处的状态
    机语义必须一致，否则会出现"调度器认为在跑、管线认为失败"这种分裂。落盘时机
    也是共用的一部分：两处都靠它在下一次网络调用之前把写锁交出去。
    """
    if not job.task_id:
        job.state = RenderJobState.LOST
        job.last_error = "没有 task_id（提交时就失败了）"
        _settle(session)
        return None
    try:
        task = client.get_task(job.task_id)
    except MptTaskLost as exc:
        job.state = RenderJobState.LOST
        job.last_error = f"MPT 侧查不到该任务（sidecar 重启？）: {exc}"
        _settle(session)
        logger.warning("渲染任务丢失 job=%s task=%s", job.id, job.task_id)
        return None
    job.state = map_task_state(task)
    job.progress = task.progress
    if task.failed:
        job.last_error = task.summary
        job.meta = {**(job.meta or {}), "failed_stage": task.failed_stage}
    _settle(session)
    return task


def download_outputs(client: MptClient, task: MptTask, dest: Path) -> Path:
    """下载成片。多个成片时只取第一个（本项目固定 ``video_count=1``）。"""
    outputs = task.outputs
    if not outputs:
        raise RenderFailed(
            f"任务 {task.task_id} 报告完成但没有成片引用（videos / combined_videos 都是空）",
            raw={"task_id": task.task_id},
        )
    return client.download(outputs[0], dest)


def wait_for_render(
    session: Session,
    job: RenderJob,
    client: MptClient,
    params: MptVideoParams,
    *,
    timeout: float,
    interval: float,
    sleeper: Callable[[float], None],
    clock: Callable[[], float],
) -> MptTask:
    """轮询到完成 / 失败 / 超时。丢任务时**原样重提交一次**。

    超时抛 :class:`RenderTimeout` 而不是标记失败：任务还在 MPT 侧跑着，
    ``job`` 保持 ``running``，调度器会接着跟。
    """
    started = clock()
    while True:
        task = sync_render_job(session, job, client)
        if task is None:  # 丢了
            if job.attempts >= MAX_RENDER_ATTEMPTS:
                raise RenderFailed(
                    f"渲染任务丢失且已重提交 {job.attempts} 次，不再重试。"
                    "先确认 MPT sidecar 是否在反复重启（docker logs sw-mpt）",
                    raw={"job_id": job.id},
                )
            job.task_id = client.create_video(params)
            job.attempts += 1
            job.state = RenderJobState.RUNNING
            job.progress = 0
            _settle(session)  # 又提交了一个真实远端任务，先落盘再接着等
            logger.info("渲染任务丢失后已重提交 job=%s task=%s", job.id, job.task_id)
            sleeper(interval)
            continue
        if task.done:
            return task
        if task.failed:
            raise RenderFailed(
                f"MPT 渲染失败（阶段 {task.failed_stage or '未知'}）：{task.error or '未给出原因'}",
                raw={"task_id": task.task_id, "failed_stage": task.failed_stage},
            )
        if clock() - started >= timeout:
            job.last_error = f"管线等待超时（{timeout:.0f}s），任务仍在渲染，交给调度器继续跟"
            _settle(session)  # "交给调度器继续跟"要成立，这行必须真的在库里
            raise RenderTimeout(
                f"渲染超过 {timeout:.0f} 秒仍未完成（当前 {task.progress}%）。"
                "内容已入库但没有成片，渲染完成后由 tick_render_jobs 自动补挂",
                raw={"task_id": task.task_id, "job_id": job.id},
            )
        sleeper(interval)


def attach_video_to_item(session: Session, item: ContentItem, video: Path) -> bool:
    """把成片补挂到一条 ContentItem 的 bundle 上。返回是否真的改了。

    只在内容还没被人工批准时才动（:data:`ATTACHABLE_STATUSES`）：批准之后改内容
    会让"人看过的"和"发出去的"不是一份，``content_hash`` 也会变，幂等键跟着变。
    """
    if item.status not in ATTACHABLE_STATUSES:
        logger.warning("item=%s 状态 %s，不再补挂成片", item.id, item.status)
        return False
    raw = dict(item.bundle_json or {})
    media = list(raw.get("media") or [])
    path_str = str(video)
    if any(entry.get("kind") == "video" and entry.get("path") == path_str for entry in media):
        return False
    # 成片排在最前，封面留在原位（inspect 与审核 UI 都按 kind 取，不依赖顺序）
    media.insert(0, MediaAsset(path=path_str, kind="video").model_dump(mode="json"))
    raw["media"] = media
    extra = dict(raw.get("platform_extra") or {})
    info = read_video_info(video)
    if info is not None:
        extra["duration_s"] = info.duration_s
        extra["resolution"] = [info.width, info.height]
    raw["platform_extra"] = extra
    # 过一遍契约校验：宁可这里抛，也不要写进去一个发布器读不出来的 bundle
    item.bundle_json = ContentBundle.model_validate(raw).model_dump(mode="json")
    session.flush()
    logger.info("成片已补挂 item=%s video=%s", item.id, video)
    # 这里刻意只 flush 不 commit：唯一的调用方 ``tick_render_jobs`` 在每个 job 开头
    # 统一 commit 一次（见那边的注释），落盘时机归它管，两处都 commit 只是白跑一趟
    return True


# ------------------------------------------------------------------ 生成链


def _account_persona(account: Account | None, account_id: str) -> str:
    """人设优先级：``Account.extra['persona']`` > ``prompts/accounts/<id>/persona.md``。"""
    if account is not None:
        inline = (account.extra or {}).get("persona")
        if isinstance(inline, str) and inline.strip():
            return inline.strip()
    return prompts.load_persona(account_id)


def _topic_fields(topic: Topic | RawTopic | str) -> tuple[str, str, str, str]:
    """归一化选题输入 → ``(title, source, url, context)``。"""
    if isinstance(topic, str):
        return topic, "", "", ""
    raw = dict(getattr(topic, "raw", {}) or {})
    context_bits = [
        f"{key}: {value}"
        for key, value in (
            ("热度", raw.get("info") or raw.get("hot_value")),
            ("榜单", raw.get("board")),
            ("名次", raw.get("rank")),
            ("角度建议", raw.get("angle")),
        )
        if value not in (None, "")
    ]
    return topic.title, topic.source or "", topic.url or "", "；".join(context_bits)


def build_video_params(draft: VideoScriptDraft, options: VideoGenerationOptions) -> MptVideoParams:
    """把脚本产物翻译成 MPT 请求参数。

    ``video_script`` 给了非空值，MPT 就**跳过自己的 LLM**——这正是"不用两套 LLM"
    的落点。``video_subject`` 仍要给：上游拿它做日志与素材兜底检索。
    """
    from core.config import get_settings

    settings = get_settings()
    return MptVideoParams(
        video_subject=draft.title,
        video_script=draft.script,
        video_terms=list(draft.search_terms),
        video_aspect=options.aspect,
        video_concat_mode=options.concat_mode,
        video_transition_mode=options.transition_mode,
        video_clip_duration=(
            options.clip_duration
            if options.clip_duration is not None
            else settings.mpt_clip_duration
        ),
        video_count=1,
        video_source=options.video_source or settings.mpt_video_source,
        voice_name=options.voice_name
        if options.voice_name is not None
        else settings.mpt_voice_name,
        bgm_type=options.bgm_type or settings.mpt_bgm_type,
        subtitle_enabled=options.subtitle_enabled,
        subtitle_position=options.subtitle_position or settings.mpt_subtitle_position,
    )


def _render_sample(
    options: VideoGenerationOptions, target: Path, warnings: list[str]
) -> Path | None:
    """``skip_render`` 分支：把样本片复制到成片位置。"""
    source = Path(options.sample_video)
    if not source.is_file():
        warnings.append(f"skip_render=true 但样本片不存在：{source}")
        logger.warning("样本片缺失: %s", source)
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    warnings.append(f"skip_render=true：挂的是样本片 {source}，**不是**真实成片")
    return target


def generate_douyin_bundle(
    topic: Topic | RawTopic | str,
    account: Account | None = None,
    *,
    llm: SupportsLLM,
    session: Session | None = None,
    account_id: str | None = None,
    options: VideoGenerationOptions | None = None,
    content_id: str | None = None,
    budget: BudgetGuard | None = None,
) -> VideoGenerationOutcome:
    """跑完抖音生成链，产出可入库的 :class:`~publishers.base.ContentBundle`。

    ``session`` 为 ``None`` 时不落 ``RenderJob``（纯离线测试用）；真实链路必须给，
    否则 sidecar 重启后 task_id 就丢了。

    渲染失败 / 超时 / 无 sidecar 都**不抛异常**：产出一个没有视频的 bundle 照常
    入库，``review.inspect`` 会以 ``douyin.video.missing`` block 掉它，人在审核页
    看到的是"缺成片"而不是一个 500。
    """
    from core.config import get_settings

    opts = options or VideoGenerationOptions()
    settings = get_settings()
    resolved_account_id = account_id or (account.id if account else None)
    if not resolved_account_id:
        raise ValueError("必须提供 account 或 account_id")

    title_hint, source, url, context = _topic_fields(topic)
    persona = _account_persona(account, resolved_account_id)
    item_id = content_id or new_id("itm")
    warnings: list[str] = []
    item_dir = Path(opts.media_root) / item_id

    # 渲染时长与生图张数共用同一个闸门实例（同一个 session，同一天的账本）
    guard = budget if budget is not None else (BudgetGuard(session) if session else None)

    # 不做封面就没必要生图：底图无处可用
    want_images = plan_illustrations(
        opts.illustrations if opts.make_cover else 0,
        injected=opts.imagegen,
        warnings=warnings,
    )

    # -- 1 脚本链 -----------------------------------------------------------
    draft = generate_video_script(
        llm,
        topic_title=title_hint,
        persona=persona,
        topic_source=source,
        topic_url=url,
        topic_context=context,
        target_script_chars=opts.target_script_chars,
        force_rewrite=opts.force_rewrite,
        image_prompt_count=want_images,
    )
    warnings.extend(draft.warnings)

    # -- 2 渲染 -------------------------------------------------------------
    video_path: Path | None = None
    render_job_id: str | None = None
    task_id: str | None = None
    render_seconds = 0.0
    clock = opts.clock or time.monotonic
    sleeper = opts.sleeper or time.sleep

    if opts.skip_render:
        video_path = _render_sample(opts, item_dir / VIDEO_FILENAME, warnings)
    else:
        params = build_video_params(draft, opts)
        estimate = estimate_render_seconds(draft)
        # 超预算直接不提交，往上抛给 dev_flow / 调度器降级成"只出选题不出稿"
        ensure_render_budget(guard, estimate)
        client = opts.client or build_client()
        owns_client = opts.client is None
        started = clock()
        try:
            task_id = client.create_video(params)
            if session is not None:
                job = create_render_job(
                    session, content_item_id=item_id, task_id=task_id, params=params
                )
                render_job_id = job.id
                task = wait_for_render(
                    session,
                    job,
                    client,
                    params,
                    timeout=(
                        opts.render_timeout
                        if opts.render_timeout is not None
                        else settings.mpt_render_timeout_seconds
                    ),
                    interval=(
                        opts.poll_interval
                        if opts.poll_interval is not None
                        else settings.mpt_poll_interval_seconds
                    ),
                    sleeper=sleeper,
                    clock=clock,
                )
                task_id = job.task_id
                video_path = download_outputs(client, task, item_dir / VIDEO_FILENAME)
                job.state = RenderJobState.DONE
                job.progress = 100
                job.result_paths = [str(video_path)]
                _settle(session)
            else:
                warnings.append("未提供 session：task_id 没有落库，sidecar 重启后无法恢复")
        except PublishError as exc:
            # 渲染出问题不该让文案也白写：降级成"没有成片的 bundle"进人工队列。
            # PublishError 覆盖了这一段能抛的全部四类：RenderTimeout / RenderFailed /
            # MptTaskLost / 客户端的 Retryable+Permanent。**BudgetExhausted 不在其中**，
            # 它会继续往上抛给 dev_flow 降级成"只出选题不出稿"。
            warnings.append(f"未拿到成片：{exc}")
            logger.warning("抖音渲染未完成 item=%s: %s", item_id, exc)
        finally:
            render_seconds = clock() - started
            charge_render_seconds(
                guard,
                render_seconds,
                meta={
                    "purpose": "douyin.render",
                    "content_item_id": item_id,
                    "task_id": task_id or "",
                },
                warnings=warnings,
            )
            if owns_client:
                client.close()

    duration_s: float | None = None
    if video_path is not None:
        info = read_video_info(video_path)
        if info is not None:
            duration_s = info.duration_s

    # -- 3 封面（9:16 竖版，P11 起可用生图当底图）-----------------------------
    hero: GeneratedImage | None = None
    if want_images and not draft.image_prompts:
        warnings.append("模型没给配图 prompt，封面回落纯色版式")
    elif want_images:
        with illustrator(opts.imagegen, budget=guard) as client:
            produced = generate_batch(
                client,
                draft.image_prompts[:1],
                item_dir,
                # 封面模板 background-size: cover 到 1080×1920（0.562）。
                # 网关不认 size，只认 prompt 里的画幅指令——不给指令拿回来的
                # 是方图或横图，居中裁进 9:16 只剩三成多画面。
                # 注意别和上面从 mpt_client 导的 ASPECT_PORTRAIT 看串：那个是
                # 成片比例字符串 "9:16"，和生图画幅是两回事。
                aspect=ASPECT_VERTICAL_9_16,
                purpose="douyin.cover",
                account_id=resolved_account_id,
                platform=DOUYIN_PLATFORM,
                stem="hero",
                warnings=warnings,
            )
        hero = produced[0] if produced else None
        if hero is None:
            warnings.append("封面底图没生成出来，回落纯色版式")

    cover_paths: dict[str, Path] = {}
    if opts.make_cover:
        try:
            cover_paths = render_cover_set(
                draft.cover_text or draft.title,
                item_dir,
                kicker=source or "SHORT VIDEO",
                footer=(account.name if account else resolved_account_id),
                stem="cover",
                sizes=("vertical",),
                screenshotter=opts.screenshotter,
                # 底图交给模板 background-size: cover 居中裁切，所以生图返回什么
                # 尺寸都不影响最终的 1080×1920（inspect 的比例校验因此照常通过）
                background=str(hero.path) if hero is not None else "",
            )
        except ScreenshotUnavailable as exc:  # pragma: no cover - render_cover 已吞掉
            warnings.append(f"未渲染封面：{exc}")
        if not cover_paths:
            warnings.append("未生成封面（缺 Playwright 或浏览器），inspect 会报缺封面")
    else:
        warnings.append("按配置跳过封面渲染")

    # -- 4 组装 bundle ------------------------------------------------------
    media: list[MediaAsset] = []
    if video_path is not None:
        media.append(MediaAsset(path=str(video_path), kind="video"))
    for path in cover_paths.values():
        media.append(MediaAsset(path=str(path), kind="image", cover=True))

    platform_extra: dict[str, Any] = {
        "title": draft.title,
        "hashtags": list(draft.hashtags),
        "duration_s": duration_s,
        # 生图审计：prompt、模型、请求尺寸与**实测**尺寸都留痕
        "illustrations": [{**hero.as_meta(), "role": "cover"}] if hero is not None else [],
        **draft.as_platform_extra(),
        "render": {
            "provider": "mpt",
            "job_id": render_job_id,
            "task_id": task_id,
            "skip_render": opts.skip_render,
            "seconds": round(render_seconds, 1),
        },
        "topic_source": source,
        "topic_url": url,
        "generated_by": "generation.video_pipeline.generate_douyin_bundle",
    }

    bundle = ContentBundle(
        id=item_id,
        account_id=resolved_account_id,
        platform=DOUYIN_PLATFORM,
        title=draft.title,
        body_markdown=draft.caption(),
        body_html=None,  # 抖音没有富文本正文
        media=media,
        tags=list(draft.hashtags),
        platform_extra=platform_extra,
    )

    logger.info(
        "抖音生成完成 item=%s 标题=%r 口播 %d 字(约 %.0fs) 成片=%s 封面=%d tokens=%d",
        item_id,
        draft.title,
        len(draft.script),
        draft.estimated_seconds,
        video_path or "无",
        len(cover_paths),
        hero.size_text if hero is not None else "纯色",
        draft.usage.billable,
    )
    return VideoGenerationOutcome(
        bundle=bundle,
        draft=draft,
        usage=draft.usage,
        warnings=warnings,
        video_path=video_path,
        cover_paths=cover_paths,
        render_job_id=render_job_id,
        task_id=task_id,
        render_seconds=round(render_seconds, 1),
        duration_s=duration_s,
        hero_image=hero,
    )


__all__ = [
    "ATTACHABLE_STATUSES",
    "DEFAULT_DOUYIN_ILLUSTRATIONS",
    "DEFAULT_MEDIA_ROOT",
    "DEFAULT_SAMPLE_VIDEO",
    "DOUYIN_PLATFORM",
    "MAX_RENDER_ATTEMPTS",
    "MIN_RENDER_ESTIMATE_SECONDS",
    "RENDER_TIME_FACTOR",
    "VIDEO_FILENAME",
    "RenderFailed",
    "RenderTimeout",
    "VideoGenerationOptions",
    "VideoGenerationOutcome",
    "attach_video_to_item",
    "build_video_params",
    "charge_render_seconds",
    "create_render_job",
    "download_outputs",
    "ensure_render_budget",
    "estimate_render_seconds",
    "generate_douyin_bundle",
    "map_task_state",
    "sync_render_job",
    "wait_for_render",
]
