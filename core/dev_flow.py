"""开发用全链路串联：sourcing → selector → generation → review → 审核队列。

给 ``POST /dev/run_wechat_pipeline``（公众号）、``POST /dev/run_xhs_pipeline``
（小红书图文）与 ``POST /dev/run_douyin_pipeline``（抖音短视频）用。生产链路由
P4 的 APScheduler 驱动，不会走这里——但两者调用的是同一批函数，所以这几个端点
也是**可复现证据**：审计时 `curl` 一下就能看到一条 ContentItem 真的走完了全流程。

没有 ``ANTHROPIC_API_KEY`` 时自动降级到 :class:`~generation.llm.ScriptedLLM`，
链路照样跑通（返回体里 ``llm="scripted"`` 标明）。这样审计不需要真 key、不烧 token。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

import prompts
from core.budget import BudgetExhausted, BudgetGuard
from core.models import Account, ContentItem, new_id, utcnow
from core.state_machine import ContentStatus, transition
from generation.llm import ScriptedLLM, SupportsLLM, build_llm
from generation.pipeline import (
    GenerationOptions,
    XhsGenerationOptions,
    generate_wechat_bundle,
    generate_xhs_bundle,
)
from generation.wechat_article import ArticleMeta, SelfCheck
from generation.xhs_note import (
    PageSpec,
    XhsCardPlan,
    XhsNoteCopy,
    XhsQualityError,
    XhsRevision,
    XhsSelfCheck,
)
from publishers.base import ContentBundle
from review.pipeline import ReviewOptions, review_item
from sourcing.base import RawTopic
from sourcing.selector import (
    SelectionResult,
    TopicScore,
    find_row,
    load_candidates,
    load_insights,
    recent_titles,
    select_topics,
    selection_meta,
)

logger = logging.getLogger("social_workflow.dev_flow")

PLATFORM = "wechat_mp"
XHS_PLATFORM = "xhs"
DOUYIN_PLATFORM = "douyin"


# --------------------------------------------------------------- 离线替身


DEMO_BODY = """## 先说结论

通勤时间超过 45 分钟的人，换房子比换工作更划算。这个判断来自我自己算的一笔账，
以及三个同事真实搬家后的反馈。

## 那笔账是怎么算的

单程 60 分钟、每周 5 天，一年就是 500 小时。按照 300 元的时薪估——这是一线城市
中位数偏上的水平——一年的通勤成本大约是 15 万。这还没算上到家之后什么都不想干
的那两个小时。

搬到离公司 20 分钟的地方，房租每月多 2000 元，一年多花 2.4 万。省下的时间是
330 小时。这笔交换在纯经济账上是明显划算的。

## 为什么大多数人不换

我问过身边七个通勤超过一小时的人，六个人给的理由是"合同还没到期"或者"懒得搬"。
只有一个人真的算过账。这说明问题不在于经济上不划算，而在于搬家这件事的启动成本
太高——它需要连续两个周末，而通勤长的人恰恰最缺完整的周末。

## 可以先做的两件事

第一，把这一周的通勤时间记下来，不要估算，用手机自动记录。多数人会低估 20% 左右。

第二，在租房软件上设一个「20 分钟通勤圈」的提醒。不用现在就搬，但当合适的房子
出现时，你得先知道它存在。

那么，如果时间账这么清楚，为什么公司还在把办公室开在租金最贵的地方？
"""


def make_scripted_llm(budget: BudgetGuard | None = None) -> ScriptedLLM:
    """离线联调用的假 LLM，按生成链的调用顺序排好预置回复。

    顺序必须与实际调用顺序一致：
    ``complete/complete_long`` 取 ``replies``，``parse`` 取 ``parsed_replies``。
    """
    return ScriptedLLM(
        budget=budget,
        replies=[
            "## 大纲\n\n切入角度：通勤时间的真实成本。\n核心观点：换房比换工作划算。",
            DEMO_BODY,
            DEMO_BODY,
            DEMO_BODY,  # 若触发第五步去 AI 味改写
        ],
        parsed_replies=[
            # 1) 选题 Agent
            SelectionResult(
                scores=[
                    TopicScore(
                        candidate_id="c1",
                        fit=8,
                        freshness=7,
                        depth=8,
                        risk=9,
                        overall=8,
                        reason="（ScriptedLLM 预置）与账号效率垂类匹配，风险低",
                        angle="从通勤时间的经济账切入",
                    )
                ],
                recommended=["c1"],
                note="ScriptedLLM 预置结果",
            ),
            # 2) 质量自评
            SelfCheck(
                ai_flavor=8,
                specificity=8,
                hook=8,
                structure=8,
                fact_risk=9,
                overall=8,
                verdict="pass",
                blocking_issues=[],
                suggestions=[],
            ),
            # 3) 标题 / 摘要 / 封面
            ArticleMeta(
                title="通勤一小时，一年亏掉十五万",
                alt_titles=["你的通勤时间值多少钱", "该换工作还是该换房子"],
                digest=(
                    "按 300 元时薪算，单程一小时的通勤一年成本约 15 万。"
                    "这笔账算清楚之后，搬家的优先级会变。"
                ),
                cover_prompt="minimalist illustration of an empty subway platform at dawn, no text",
                cover_title="通勤的隐形账单",
                keywords=["通勤", "时间成本", "租房"],
            ),
        ],
    )


DEMO_XHS_ANGLE = """### 1 具体是给谁看的

刚搬进 30㎡ 一居室、押一付三之后手里只剩两千块的人。

### 2 她的问题

「东西都堆在地上，但我不敢往墙上打孔。」

### 3 我的答案

不打孔也能收纳，关键是把垂直空间让出来，而不是再买柜子。

### 4 支撑要点

- 门后是最被浪费的一面墙 —— 挂钩承重 3kg，一个 19 块
- 免钉洞洞板比柜子便宜十倍 —— 60×80cm 的一块 89 元，装了两年没掉
- 床底箱子要带轮 —— 没轮的第三次就懒得拉出来了

### 5 封面

不打孔，多出一面墙
"""

DEMO_XHS_BODY = """搬进这间 30㎡ 之前，我以为收纳就是买柜子。买完发现房间更小了。

真正管用的是三件事：门后挂钩、免钉洞洞板、带轮的床底箱。

门后那面墙我空了半年才反应过来。19 块的挂钩承重 3kg，挂了包和外套，
地上立刻空出一块。

洞洞板是 89 块买的 60×80cm，贴在书桌上方，装了两年没掉过。
前提是墙面别是那种一擦就掉粉的乳胶漆，我第一次贴在厨房就翻车了。

床底箱一定要带轮。我买的第一个没轮，第三次就懒得拉出来，
后来那箱冬装在床底躺了一整年。

你们租的房子有哪面墙是完全空着的？
"""


def make_xhs_scripted_llm(budget: BudgetGuard | None = None) -> ScriptedLLM:
    """小红书链的离线替身。

    ``parse`` 按**类型**取预置回复，所以这里的顺序无所谓。整包修订与第二次 selfcheck
    总是预置；初检通过且未强制修订时不会消费它们。
    """
    return ScriptedLLM(
        budget=budget,
        replies=[DEMO_XHS_ANGLE],
        parsed_replies=[
            # 1) 选题 Agent
            SelectionResult(
                scores=[
                    TopicScore(
                        candidate_id="c1",
                        fit=8,
                        freshness=7,
                        depth=7,
                        risk=9,
                        overall=8,
                        reason="（ScriptedLLM 预置）与租房收纳垂类匹配，风险低",
                        angle="从不打孔的垂直收纳切入",
                    )
                ],
                recommended=["c1"],
                note="ScriptedLLM 预置结果",
            ),
            # 2) 卡片脚本
            XhsCardPlan(
                cover_headline="不打孔，多出一面墙",
                pages=[
                    PageSpec(
                        headline="门后是最被浪费的一面墙",
                        bullets=["19 块的挂钩承重 3kg", "挂包和外套，地上立刻空一块"],
                        footnote="空心门先看承重标注",
                    ),
                    PageSpec(
                        headline="免钉洞洞板比柜子便宜十倍",
                        bullets=["60×80cm 一块 89 元", "贴书桌上方，两年没掉"],
                        footnote="掉粉的乳胶漆墙别贴",
                    ),
                    PageSpec(
                        headline="床底箱一定要带轮",
                        bullets=["没轮的第三次就懒得拉", "冬装在床底躺了一整年"],
                        footnote="",
                    ),
                ],
            ),
            # 3) 标题 / 正文 / 标签
            XhsNoteCopy(
                title="租房不打孔，我多出一面墙",
                alt_titles=["30㎡ 收纳，三件东西就够", "押一付三之后的两千块怎么花"],
                body=DEMO_XHS_BODY,
                tags=["租房", "小户型收纳", "独居", "免打孔", "居家好物"],
            ),
            # 4) 质量自评
            XhsSelfCheck(
                ai_flavor=8,
                specificity=9,
                hook=8,
                card_fit=9,
                tag_fit=8,
                compliance_risk=9,
                overall=8,
                verdict="pass",
                blocking_issues=[],
                suggestions=[],
            ),
            # 5) 初检失败或 force_rewrite=True 时的整包修订
            XhsRevision(
                title="租房不打孔，我多出一面墙",
                alt_titles=["30㎡ 收纳，三件东西就够", "押一付三之后的两千块怎么花"],
                body=DEMO_XHS_BODY,
                tags=["租房", "小户型收纳", "独居", "免打孔", "居家好物"],
                cover_headline="不打孔，多出一面墙",
                pages=[
                    PageSpec(
                        headline="门后是最被浪费的一面墙",
                        bullets=["19 块的挂钩承重 3kg", "挂包和外套，地上立刻空一块"],
                        footnote="空心门先看承重标注",
                    ),
                    PageSpec(
                        headline="免钉洞洞板比柜子便宜十倍",
                        bullets=["60×80cm 一块 89 元", "贴书桌上方，两年没掉"],
                        footnote="掉粉的乳胶漆墙别贴",
                    ),
                    PageSpec(
                        headline="床底箱一定要带轮",
                        bullets=["没轮的第三次就懒得拉", "冬装在床底躺了一整年"],
                        footnote="",
                    ),
                ],
                image_prompts=[],
            ),
            # 6) 整包修订后的终检
            XhsSelfCheck(
                ai_flavor=9,
                specificity=9,
                hook=9,
                card_fit=9,
                tag_fit=9,
                compliance_risk=9,
                overall=9,
                verdict="pass",
                blocking_issues=[],
                suggestions=[],
            ),
        ],
    )


DEMO_DOUYIN_ANGLE = """### 1 具体是给谁看的

单程通勤超过一小时、每天在地铁上刷手机的上班族。

### 2 他凭什么停下来

"你每天多花的那一小时，一年是十五万。"

### 3 我要给的答案

换房比换工作划算。

### 4 支撑要点

- 单程 60 分钟 × 5 天 × 52 周 = 500 小时
- 按 300 元时薪折 = 15 万
- 搬到 20 分钟通勤圈：一年多花 2.4 万，换回 330 小时

### 5 时长

45 秒。一笔账 + 一个反例 + 一个动作，再长就有人划走。

### 6 画面

crowded morning train, empty subway platform, office worker walking,
hands counting coins
"""

DEMO_DOUYIN_SCRIPT = """通勤一小时的人，一年亏掉十五万。

我算过一笔账。单程六十分钟，一周五天，一年就是五百个小时。

按三百块的时薪折，这是十五万。还没算到家之后什么都不想干的那两个小时。

搬到离公司二十分钟的地方，房租一个月多两千，一年多花两万四，换回三百三十个小时。

我问过身边七个通勤超过一小时的人，六个说的是合同没到期，或者懒得搬。

只有一个人真的算过。

今晚别估算，用手机自动记一次你的通勤时间。多数人会少算两成。

你的通勤时间，值多少钱？"""

DEMO_DOUYIN_HOOK = "通勤一小时的人，一年亏掉十五万。"


def make_douyin_scripted_llm(budget: BudgetGuard | None = None) -> ScriptedLLM:
    """抖音链的离线替身。

    ``parse`` 按**类型**取预置回复，顺序无所谓；``complete`` 按顺序取，
    对应"切角度"与（可能触发的）"去 AI 味改写"两步。
    """
    from generation.video_script import VideoScriptCopy, VideoSelfCheck

    return ScriptedLLM(
        budget=budget,
        replies=[DEMO_DOUYIN_ANGLE, DEMO_DOUYIN_SCRIPT],
        parsed_replies=[
            # 1) 选题 Agent
            SelectionResult(
                scores=[
                    TopicScore(
                        candidate_id="c1",
                        fit=8,
                        freshness=7,
                        depth=7,
                        risk=9,
                        overall=8,
                        reason="（ScriptedLLM 预置）与通勤垂类匹配，风险低",
                        angle="从通勤时间的经济账切入",
                    )
                ],
                recommended=["c1"],
                note="ScriptedLLM 预置结果",
            ),
            # 2) 脚本
            VideoScriptCopy(
                title="通勤一小时，一年亏掉十五万",
                hook=DEMO_DOUYIN_HOOK,
                script=DEMO_DOUYIN_SCRIPT,
                search_terms=[
                    "crowded morning train",
                    "empty subway platform",
                    "office worker walking",
                    "hands counting coins",
                ],
                cover_text="通勤的隐形账单",
                hashtags=["通勤", "时间管理", "上班族", "租房"],
            ),
            # 3) 质量自评
            VideoSelfCheck(
                ai_flavor=8,
                hook_strength=9,
                specificity=9,
                spoken_fit=9,
                pacing=8,
                term_fit=8,
                compliance_risk=9,
                overall=8,
                verdict="pass",
                blocking_issues=[],
                suggestions=[],
            ),
        ],
    )


# --------------------------------------------------------------- 结果 DTO


@dataclass
class DevFlowResult:
    content_item_id: str | None = None
    status: str = ""
    llm: str = "real"
    topics_ingested: int = 0
    candidates: int = 0
    selected_topic: str | None = None
    selection_note: str = ""
    review_passed: bool | None = None
    review_findings: int = 0
    review_blocking: int = 0
    #: warn 级发现数。autopilot 的自动批准条件是 ``blocking == 0 且 warning == 0``，
    #: 光有 blocking 算不出"干净"——warn 恰恰是需要人判断的那一类
    review_warning: int = 0
    stages_run: list[str] = field(default_factory=list)
    stages_skipped: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    tokens_used: int = 0
    review_url: str | None = None
    #: 小红书专属：卡片张数与主题
    cards: int = 0
    theme: str = ""
    #: 生图配图张数（P11）。0 可能是没开、没权限、或预算用完了——原因在 warnings 里
    illustrations: int = 0
    #: 抖音专属：成片与渲染任务
    video: str | None = None
    duration_s: float | None = None
    render_job_id: str | None = None
    render_task_id: str | None = None
    render_seconds: float = 0.0
    skip_render: bool = False

    def as_dict(self) -> dict[str, Any]:
        data = {
            "ok": self.content_item_id is not None,
            "content_item_id": self.content_item_id,
            "status": self.status,
            "llm": self.llm,
            "topics_ingested": self.topics_ingested,
            "candidates": self.candidates,
            "selected_topic": self.selected_topic,
            "selection_note": self.selection_note,
            "review": {
                "passed": self.review_passed,
                "findings": self.review_findings,
                "blocking": self.review_blocking,
                "warnings": self.review_warning,
                "stages_run": self.stages_run,
                "stages_skipped": self.stages_skipped,
            },
            "warnings": self.warnings,
            "tokens_used": self.tokens_used,
            "review_url": self.review_url,
        }
        if self.theme or self.cards:
            data["cards"] = self.cards
            data["theme"] = self.theme
        if self.render_job_id or self.video or self.skip_render:
            data["render"] = {
                "video": self.video,
                "duration_s": self.duration_s,
                "job_id": self.render_job_id,
                "task_id": self.render_task_id,
                "seconds": self.render_seconds,
                "skip_render": self.skip_render,
            }
        return data


class DevFlowError(RuntimeError):
    """链路无法继续，携带面向人的原因。"""


# --------------------------------------------------------------- 各步骤


def collect_topics(session: Session, *, warnings: list[str]) -> int:
    """跑一遍采集器，返回**新增**入库条数。单个源不可用不阻断。

    实现在 ``sourcing/collector.py``——``tick_sourcing`` 走的是同一批采集器，
    两处各写一份的话，加了新源只有一边生效。
    """
    from sourcing.collector import collect

    result = collect(session)
    warnings.extend(result.warnings)
    return result.created


def resolve_llm(
    llm: SupportsLLM | None,
    result: DevFlowResult,
    guard: BudgetGuard,
    *,
    scripted: Callable[[BudgetGuard], ScriptedLLM],
) -> SupportsLLM:
    """挑一个 LLM 客户端：注入的 > 真实的 > ScriptedLLM。

    没 key 就用替身而不是报错，是为了让审计在无凭据的机器上也能复现整条链路。
    """
    if llm is not None:
        result.llm = "injected"
        return llm

    from core.config import get_settings
    from generation.llm import llm_credentials_ready

    settings = get_settings()
    if llm_credentials_ready(settings):
        result.llm = "real"
        return build_llm(budget=guard)
    result.llm = "scripted"
    missing = (
        f"dsh 路由 {settings.dsh_provider} 的凭据（见 configs/dsh/cordis.yml 的 apiKeyEnv）"
        if settings.sw_llm_backend == "dsh"
        else "ANTHROPIC_API_KEY"
    )
    result.warnings.append(f"{missing} 未配置，已用 ScriptedLLM 预置内容跑通链路（非真实生成）")
    return scripted(guard)


def pick_topic(
    session: Session,
    account: Account,
    llm: SupportsLLM,
    result: DevFlowResult,
    *,
    topic_title: str | None,
    skip_sourcing: bool,
) -> tuple[RawTopic, Any, dict[str, Any]]:
    """采集 + 选题，返回 ``(选中的选题, Topic 行, 选题决策留痕)``。"""
    if not skip_sourcing:
        result.topics_ingested = collect_topics(session, warnings=result.warnings)

    if topic_title:
        result.selection_note = "手工指定选题，跳过选题 Agent"
        chosen = RawTopic(source="manual", title=topic_title, score=1.0, raw={"manual": True})
        result.selected_topic = chosen.title
        return chosen, None, {}

    candidates, rows = load_candidates(session)
    result.candidates = len(candidates)
    if not candidates:
        raise DevFlowError(
            "选题池是空的：newsnow 未配置，douyin 归档也拉不到。"
            "要么配上 NEWSNOW_BASE_URL 让它自己拉热榜，要么这次直接指定一个选题标题。"
        )
    try:
        selection = select_topics(
            candidates,
            llm,
            # P4 起把人设与复盘结论真的喂进去（P1–P3 这里一直是空串，选题 Agent
            # 其实在"裸奔"）。人设优先级与生成链一致：extra['persona'] > persona.md
            persona=(account.extra or {}).get("persona") or prompts.load_persona(account.id),
            recent=recent_titles(session, account.id),
            insights=load_insights(account.id),
        )
    except BudgetExhausted as exc:
        raise DevFlowError(f"token 预算耗尽，只出选题不出稿：{exc}") from exc
    result.selection_note = selection.note
    if not selection.picks:
        raise DevFlowError(f"选题 Agent 认为今天没有值得写的选题：{selection.note or '（未说明）'}")
    chosen = selection.top
    assert chosen is not None
    result.selected_topic = chosen.title
    return chosen, find_row(rows, chosen), selection_meta(selection)


def _persist_and_review(
    session: Session,
    account: Account,
    result: DevFlowResult,
    *,
    item_id: str,
    bundle: ContentBundle,
    topic_row: Any,
    llm: SupportsLLM,
    review_options: ReviewOptions,
) -> ContentItem:
    """入库（topic → drafting → draft）+ 机器审核（draft → reviewing → draft）。"""
    item = ContentItem(
        id=item_id,
        account_id=account.id,
        topic_id=topic_row.id if topic_row is not None else None,
        status=ContentStatus.TOPIC.value,
        bundle_json=bundle.model_dump(mode="json"),
        created_at=utcnow(),
    )
    session.add(item)
    session.flush()
    transition(item, ContentStatus.DRAFTING)
    transition(item, ContentStatus.DRAFT)
    # 先落盘再审（P16.3）。两件事，缺一不可：
    # 1. **交出写锁**。SQLite 的写锁是整库一把，flush 只发语句、锁握到 commit。紧跟着的
    #    机器审核里有 `review.semantic` 那一次 LLM 调用（上界 llm_timeout_seconds=600），
    #    整段跑在锁里，同期任何别的写者等满 5 秒就 database is locked（见 core/db.py）。
    # 2. **别把已经付过钱的稿子扔掉**。这条 draft 是真烧了 token 生成出来的；审核崩了
    #    连它一起回滚，等于把花掉的钱扔了，下一轮 tick_generate 还得重新生成一遍。
    #    重审很便宜（review_item 只接受 draft，重跑一次就行）。
    # 代价：审核中途崩掉会留下一条"入库了但从没审过"的 draft（review_notes 为空）。
    # 它会出现在审核台（REVIEW_QUEUE_STATUSES 含 draft），但没有任何机器结论、也没人
    # 报警。出口是 core/scheduler.py 的 recover_stale_drafts——**改这里之前先看那个函数**。
    session.commit()

    review_result = review_item(
        session,
        item,
        llm=llm if review_options.use_llm else None,
        options=review_options,
    )
    result.review_passed = review_result.passed
    result.review_findings = len(review_result.findings)
    result.review_blocking = len(review_result.blocking)
    result.review_warning = len(review_result.warnings)
    result.stages_run = review_result.stages_run
    result.stages_skipped = review_result.stages_skipped
    result.content_item_id = item.id
    result.status = item.status
    result.review_url = f"/review/{item.id}"
    return item


def run_wechat_pipeline(
    session: Session,
    account: Account,
    *,
    llm: SupportsLLM | None = None,
    topic_title: str | None = None,
    skip_sourcing: bool = False,
    use_llm_review: bool = True,
    options: GenerationOptions | None = None,
) -> DevFlowResult:
    """跑完 sourcing → selector → generation → review，产出一条待审 ContentItem。"""
    if account.platform != PLATFORM:
        raise DevFlowError(f"账号 {account.id} 是 {account.platform} 平台，本端点只跑 wechat_mp")

    result = DevFlowResult()
    # labels 让每笔 token 流水带上账号，`/stats` 才能按账号归集成本
    guard = BudgetGuard(session, labels={"account_id": account.id, "platform": account.platform})
    llm = resolve_llm(llm, result, guard, scripted=make_scripted_llm)
    chosen, topic_row, extra_meta = pick_topic(
        session,
        account,
        llm,
        result,
        topic_title=topic_title,
        skip_sourcing=skip_sourcing,
    )

    # -- 3 生成 -------------------------------------------------------------
    item_id = new_id("itm")
    try:
        outcome = generate_wechat_bundle(
            chosen,
            account,
            llm=llm,
            content_id=item_id,
            options=options or GenerationOptions(),
            # 生图张数记进同一个账本（labels 让流水带上账号）
            budget=guard,
        )
    except BudgetExhausted as exc:
        raise DevFlowError(f"token 预算耗尽，未出稿：{exc}") from exc

    result.warnings.extend(outcome.warnings)
    result.tokens_used = outcome.usage.billable
    result.illustrations = 1 if outcome.hero_image is not None else 0

    bundle = outcome.bundle
    if extra_meta:
        merged = {**bundle.platform_extra, "selection": extra_meta}
        bundle = bundle.model_copy(update={"platform_extra": merged})

    # -- 4/5 入库 + 机器审核 -------------------------------------------------
    item = _persist_and_review(
        session,
        account,
        result,
        item_id=item_id,
        bundle=bundle,
        topic_row=topic_row,
        llm=llm,
        review_options=ReviewOptions(use_llm=use_llm_review),
    )
    logger.info(
        "dev 链路完成 item=%s 选题=%r 审核 passed=%s",
        item.id,
        chosen.title,
        result.review_passed,
    )
    return result


def run_xhs_pipeline(
    session: Session,
    account: Account,
    *,
    llm: SupportsLLM | None = None,
    topic_title: str | None = None,
    skip_sourcing: bool = False,
    use_llm_review: bool = True,
    commercial: bool = True,
    options: XhsGenerationOptions | None = None,
) -> DevFlowResult:
    """小红书版：sourcing → selector → 图文生成（文案 + 3:4 卡片）→ review。

    ``commercial`` 默认开着：小红书笔记天然处在"推荐一个东西"的语境里，
    极限词与效果承诺是这个平台被判违规最多的一类，宁可多报几条 warn 让人看一眼。
    """
    if account.platform != XHS_PLATFORM:
        raise DevFlowError(f"账号 {account.id} 是 {account.platform} 平台，本端点只跑 xhs")

    result = DevFlowResult()
    # labels 让每笔 token 流水带上账号，`/stats` 才能按账号归集成本
    guard = BudgetGuard(session, labels={"account_id": account.id, "platform": account.platform})
    llm = resolve_llm(llm, result, guard, scripted=make_xhs_scripted_llm)
    chosen, topic_row, extra_meta = pick_topic(
        session,
        account,
        llm,
        result,
        topic_title=topic_title,
        skip_sourcing=skip_sourcing,
    )

    # -- 3 生成（文案 + 卡片）------------------------------------------------
    item_id = new_id("itm")
    opts = options or XhsGenerationOptions()
    try:
        outcome = generate_xhs_bundle(
            chosen,
            account,
            llm=llm,
            content_id=item_id,
            suggested_angle=str(extra_meta.get("angle") or ""),
            options=opts,
            # 生图张数记进同一个账本（labels 让流水带上账号）
            budget=guard,
        )
    except BudgetExhausted as exc:
        raise DevFlowError(f"token 预算耗尽，未出稿：{exc}") from exc
    except XhsQualityError as exc:
        raise DevFlowError(f"小红书质量终检未通过，未渲染、未生图、未入库：{exc}") from exc

    result.warnings.extend(outcome.warnings)
    result.tokens_used = outcome.usage.billable
    result.cards = len(outcome.card_paths)
    result.illustrations = len(outcome.illustration_paths)
    result.theme = opts.theme

    bundle = outcome.bundle
    if extra_meta:
        merged = {**bundle.platform_extra, "selection": extra_meta}
        bundle = bundle.model_copy(update={"platform_extra": merged})

    # -- 4/5 入库 + 机器审核 -------------------------------------------------
    item = _persist_and_review(
        session,
        account,
        result,
        item_id=item_id,
        bundle=bundle,
        topic_row=topic_row,
        llm=llm,
        review_options=ReviewOptions(use_llm=use_llm_review, commercial=commercial),
    )
    logger.info(
        "dev 小红书链路完成 item=%s 选题=%r 卡片=%d 审核 passed=%s",
        item.id,
        chosen.title,
        result.cards,
        result.review_passed,
    )
    return result


def run_douyin_pipeline(
    session: Session,
    account: Account,
    *,
    llm: SupportsLLM | None = None,
    topic_title: str | None = None,
    skip_sourcing: bool = False,
    use_llm_review: bool = True,
    commercial: bool = True,
    options: Any | None = None,
) -> DevFlowResult:
    """抖音版：sourcing → selector → 口播脚本 + MPT 渲染 → review。

    和另外两条链的差别只有一处：**渲染是跨进程长任务**。没有 MPT sidecar 时用
    ``options.skip_render=True`` 挂样本片，整条链照样跑通（返回体的
    ``render.skip_render=true`` 会显式标出来，避免把样本片误当成真实产出）。

    ``commercial`` 默认开着：短视频带货语境里极限词与效果承诺同样是高频违规项。
    """
    from generation.video_pipeline import VideoGenerationOptions, generate_douyin_bundle

    if account.platform != DOUYIN_PLATFORM:
        raise DevFlowError(f"账号 {account.id} 是 {account.platform} 平台，本端点只跑 douyin")

    result = DevFlowResult()
    # labels 让每笔 token 流水带上账号，`/stats` 才能按账号归集成本
    guard = BudgetGuard(session, labels={"account_id": account.id, "platform": account.platform})
    llm = resolve_llm(llm, result, guard, scripted=make_douyin_scripted_llm)
    chosen, topic_row, extra_meta = pick_topic(
        session,
        account,
        llm,
        result,
        topic_title=topic_title,
        skip_sourcing=skip_sourcing,
    )

    # -- 3 生成（脚本 + 成片 + 封面）----------------------------------------
    item_id = new_id("itm")
    opts = options or VideoGenerationOptions()
    try:
        outcome = generate_douyin_bundle(
            chosen,
            account,
            llm=llm,
            session=session,
            content_id=item_id,
            options=opts,
            budget=guard,
        )
    except BudgetExhausted as exc:
        # token 或 render_seconds 任一耗尽都走这里：只出选题不出稿
        raise DevFlowError(f"预算耗尽，未出稿：{exc}") from exc

    result.warnings.extend(outcome.warnings)
    result.tokens_used = outcome.usage.billable
    result.video = str(outcome.video_path) if outcome.video_path else None
    result.duration_s = outcome.duration_s
    result.render_job_id = outcome.render_job_id
    result.render_task_id = outcome.task_id
    result.render_seconds = outcome.render_seconds
    result.skip_render = opts.skip_render
    result.illustrations = 1 if outcome.hero_image is not None else 0

    bundle = outcome.bundle
    if extra_meta:
        merged = {**bundle.platform_extra, "selection": extra_meta}
        bundle = bundle.model_copy(update={"platform_extra": merged})

    # -- 4/5 入库 + 机器审核 -------------------------------------------------
    item = _persist_and_review(
        session,
        account,
        result,
        item_id=item_id,
        bundle=bundle,
        topic_row=topic_row,
        llm=llm,
        review_options=ReviewOptions(use_llm=use_llm_review, commercial=commercial),
    )
    logger.info(
        "dev 抖音链路完成 item=%s 选题=%r 成片=%s 审核 passed=%s",
        item.id,
        chosen.title,
        result.video or "无",
        result.review_passed,
    )
    return result


__all__ = [
    "DEMO_BODY",
    "DEMO_DOUYIN_ANGLE",
    "DEMO_DOUYIN_HOOK",
    "DEMO_DOUYIN_SCRIPT",
    "DEMO_XHS_ANGLE",
    "DEMO_XHS_BODY",
    "DOUYIN_PLATFORM",
    "PLATFORM",
    "XHS_PLATFORM",
    "DevFlowError",
    "DevFlowResult",
    "collect_topics",
    "make_douyin_scripted_llm",
    "make_scripted_llm",
    "make_xhs_scripted_llm",
    "pick_topic",
    "resolve_llm",
    "run_douyin_pipeline",
    "run_wechat_pipeline",
    "run_xhs_pipeline",
]
