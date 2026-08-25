"""三级审核管线 + 状态机接线。

```
词面硬过滤(lexicon) → 平台违禁(precheck) → LLM 语境判定(llm_semantic) → 发布前校验(inspect)
                                                                     ↓
                                                       人工卡点（已在 core/main.py）
```

状态流转（``review_item``）::

    draft → reviewing → draft_reviewed

"draft_reviewed" 不是新状态——P0 冻结的状态机里没有它，也**不允许**新增。
落地方式是：跑完机器审核后把内容**放回 draft**，并把结论写进 ``review_notes``，
由 ``ReviewLog(actor="system", action="machine_review")`` 留痕。
人工批准仍走原有的 ``/review/{id}/approve``。

有 ``block`` 时同样回到 ``draft``，但 ``review_notes`` 里写明被挡的原因——
按 ``review/README.md`` 的约定，这类内容不该出现在人工队列顶部等人点驳回。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from core.models import ContentItem
from core.state_machine import ContentStatus, log_review, transition
from generation.llm import SupportsLLM
from publishers.base import ContentBundle
from review import inspect as inspect_mod
from review import lexicon as lexicon_mod
from review import llm_semantic, precheck
from review.base import Finding, ReviewResult, sort_findings

logger = logging.getLogger("social_workflow.review.pipeline")

#: 系统写 ReviewLog 时用的动作名。不复用 core.state_machine.SystemAction，
#: 那个枚举是发布相关事件的，机器审核是另一类事件。
MACHINE_REVIEW_ACTION = "machine_review"

#: 话题标签会随内容一起公开展示的平台。这些平台的 ``tags`` 必须送审——
#: 蹭违规话题、堆无关热词同样会被判违规。公众号的 tags 是内部检索关键词，不外显，
#: 扫它只会平白多出误报。
TAGGED_PLATFORMS = frozenset({"xhs", "douyin"})


def _xhs_selfcheck_finding(bundle: ContentBundle) -> Finding | None:
    """挡住旧链路遗留的不合格自检；缺失自检仍兼容人工或历史稿。"""
    if bundle.platform != "xhs":
        return None
    raw = (bundle.platform_extra or {}).get("selfcheck")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return Finding(
            level="block",
            rule="generation.xhs.selfcheck.invalid",
            suggestion="生成自检记录格式无效，请重新生成并通过终检。",
            stage="inspect",
        )

    verdict = str(raw.get("verdict") or "").strip().lower()
    issues_raw = raw.get("blocking_issues")
    issues = issues_raw if isinstance(issues_raw, list) else ([issues_raw] if issues_raw else [])
    if verdict == "pass" and not issues:
        return None
    issue_text = "；".join(str(issue).strip() for issue in issues if str(issue).strip())
    detail = f"verdict={verdict or 'missing'}"
    if issue_text:
        detail += f"；blocking_issues={issue_text}"
    return Finding(
        level="block",
        rule="generation.xhs.selfcheck.failed",
        suggestion=f"生成质量终检未通过（{detail}），请整包修订并重新终检。",
        stage="inspect",
        extra={"verdict": verdict, "blocking_issues": issues},
    )


@dataclass
class ReviewOptions:
    """管线开关。"""

    #: 带货 / 商业场景（启用极限词等商业规则）
    commercial: bool = False
    #: 行业标签，见 review.precheck.KNOWN_INDUSTRIES
    industries: frozenset[str] = frozenset()
    #: 关掉可跳过 LLM 语境判定（离线测试 / 省 token）
    use_llm: bool = True
    #: 词库目录，留空取配置
    lexicon_dir: str | None = None
    #: 发布前结构校验的媒体根目录
    media_root: str | None = None


def review_text(bundle: ContentBundle) -> str:
    """拼出送审文本：标题 + 正文 + （标签外显的平台）尚未出现在正文里的话题标签。

    标题违规和正文违规一样致命，而且标题更显眼，所以两者一起审。

    小红书的生成链已经把标签拼进了 ``body_markdown``，这里补的是"标签只存在于
    ``tags`` 字段"的情况——例如人工改稿时只动了标签输入框。
    """
    parts = [bundle.title or "", bundle.body_markdown or ""]
    if bundle.platform in TAGGED_PLATFORMS:
        body = bundle.body_markdown or ""
        missing = [str(tag) for tag in bundle.tags if str(tag) and str(tag) not in body]
        if missing:
            parts.append(" ".join(f"#{tag}" for tag in missing))
    return "\n\n".join(part for part in parts if part)


def review(
    bundle: ContentBundle,
    *,
    llm: SupportsLLM | None = None,
    options: ReviewOptions | None = None,
) -> ReviewResult:
    """跑完三级机器审核 + 发布前校验，返回 :class:`ReviewResult`。

    本函数**不碰数据库**，纯函数，便于单测与 ``inspect --json`` 复用。
    """
    opts = options or ReviewOptions()
    text = review_text(bundle)

    findings: list[Finding] = []
    stages_run: list[str] = []
    stages_skipped: dict[str, str] = {}
    edits: dict[str, str] = {}

    # -- 1 词面硬过滤 -------------------------------------------------------
    lex = lexicon_mod.get_lexicon(opts.lexicon_dir)
    findings.extend(lexicon_mod.scan(text, lex))
    stages_run.append("lexicon")

    # -- 1b 配图 prompt 的词面过滤（P11）-------------------------------------
    # 生成图和文字同权送审，prompt 本身也不许踩红线：让模型去画一个违规词描述的画面，
    # 画出来的东西一样会被平台判违规，而且事后从图上看不出是哪句 prompt 惹的祸。
    # 单独扫而不是拼进 review_text：命中位置的下标是相对 prompt 的，混进正文会指错地方，
    # 而且 LLM 语境判定不该把英文 prompt 当成"要发出去的内容"来改写。
    prompt_text = "\n".join(inspect_mod.iter_image_prompts(bundle))
    if prompt_text:
        for hit in lexicon_mod.scan(prompt_text, lex):
            findings.append(
                hit.model_copy(
                    update={
                        "suggestion": f"配图 prompt 命中词库，改掉再重出：{hit.suggestion}",
                        # 下标是相对 prompt 文本的，留着会让审核页在正文里高亮错位置
                        "start": None,
                        "end": None,
                        "extra": {**hit.extra, "source": "image_prompt"},
                    }
                )
            )
        stages_run.append("lexicon.image_prompts")

    # -- 2 平台违禁 ---------------------------------------------------------
    try:
        findings.extend(precheck.scan(text, commercial=opts.commercial, industries=opts.industries))
        stages_run.append("precheck")
    except precheck.PrecheckDataMissing as exc:
        stages_skipped["precheck"] = str(exc)
        logger.warning("precheck 规则数据缺失，已跳过: %s", exc)

    # -- 3 LLM 语境判定 -----------------------------------------------------
    if not opts.use_llm:
        stages_skipped["llm_semantic"] = "options.use_llm=False"
    elif llm is None:
        stages_skipped["llm_semantic"] = "未注入 LLM 客户端"
    else:
        try:
            semantic = llm_semantic.judge(text, findings, llm)
            findings, edits = llm_semantic.apply(findings, semantic)
            stages_run.append("llm_semantic")
        except llm_semantic.SemanticSkipped as exc:
            stages_skipped["llm_semantic"] = exc.reason
            logger.info("跳过 LLM 语境判定: %s", exc.reason)

    # -- 3b 生成质量闸门兜底 -------------------------------------------------
    # 放在 LLM 语境判定之后：这是生成元数据里的硬失败，不能被语义审核降级。
    selfcheck_finding = _xhs_selfcheck_finding(bundle)
    if selfcheck_finding is not None:
        findings.append(selfcheck_finding)

    # -- 4 发布前结构校验 ---------------------------------------------------
    report = inspect_mod.inspect(bundle, media_root=opts.media_root)
    findings.extend(report.findings)
    stages_run.append("inspect")

    findings = sort_findings(findings)
    passed = not any(f.level == "block" for f in findings)
    return ReviewResult(
        passed=passed,
        findings=findings,
        suggested_edits=edits,
        stages_run=stages_run,
        stages_skipped=stages_skipped,
    )


def review_item(
    session: Session,
    item: ContentItem,
    *,
    llm: SupportsLLM | None = None,
    options: ReviewOptions | None = None,
    actor: str = "system",
) -> ReviewResult:
    """对一个 ``ContentItem`` 跑机器审核，写 ``review_notes`` 与审计日志。

    状态：``draft → reviewing → draft``。要求进来时是 ``draft``——
    已经是 ``approved`` 的内容不该被机器审核悄悄打回，那要走人工撤回。
    """
    if item.status != ContentStatus.DRAFT.value:
        raise ValueError(f"机器审核只接受 draft 状态的内容，当前 {item.status}（item={item.id}）")

    bundle = ContentBundle.model_validate(item.bundle_json)
    transition(item, ContentStatus.REVIEWING)
    result = review(bundle, llm=llm, options=options)

    item.review_notes = result.summary()
    # 回到 draft 等人工处置；机器审核不代替人工卡点
    transition(item, ContentStatus.DRAFT)
    log_review(
        session,
        item,
        actor=actor,
        action=MACHINE_REVIEW_ACTION,
        reason=result.summary(),
        after={
            "passed": result.passed,
            "blocking": len(result.blocking),
            "warnings": len(result.warnings),
            "stages_run": result.stages_run,
            "stages_skipped": result.stages_skipped,
            "suggested_edits": result.suggested_edits,
        },
    )
    session.flush()
    logger.info(
        "机器审核完成 item=%s passed=%s block=%d warn=%d",
        item.id,
        result.passed,
        len(result.blocking),
        len(result.warnings),
    )
    return result


__all__ = [
    "MACHINE_REVIEW_ACTION",
    "TAGGED_PLATFORMS",
    "ReviewOptions",
    "ReviewResult",
    "review",
    "review_item",
    "review_text",
]
