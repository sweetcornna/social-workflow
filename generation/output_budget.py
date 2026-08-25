"""生成链的输出预算（``max_tokens``）分档表。

为什么单独一张表
----------------
默认模型是 **reasoning 模型**（``deepseek-v4-flash``）：思考与正文**共用**同一份输出
预算。所以 ``max_tokens`` 不是"正文最多写多长"，而是"思考 + 正文一共最多多长"——
按正文长度估出来的数字必然偏小，模型某次多想几步就把预算全烧在思考上、正文一个字
都没有。2026-08-17 的生产事故正是如此：``xhs.angle`` 正常只写 853 token 的正文，
那一次却烧光 4096 且**零输出**，整条生成链 502。

分档而不是逐点调参
------------------
dsh 后端按 ``(model, effort, max_tokens)`` **分桶起 runtime 子进程**（见
:mod:`generation.llm_dsh` 的模块 docstring），池子 LRU 上限默认 4。这里收敛的是
``max_tokens`` 这一维：所有调用点都必须落在
:data:`OUTPUT_TIERS` 上，截断自愈的加码（:func:`escalate`）也走同一张梯子，
不会在预算维度凭空造出第四档。关闭模型路由时仍保持旧的单模型分桶语义；开启分档
路由后完整 RuntimeKey 的组合数可能超过默认池上限，超出的进程由 LRU 回收。

取值口径：**账本实测用量 × 安全系数**，实测数据来自 2026-08-16 UTC 04:55–04:57
那次成功运行的 ``CostLedger``（逐条见 :data:`CALL_SITE_BUDGETS` 的注释）。
安全系数不是拍脑袋的余量——它买的是"思考阶段的突发量"，而思考量与正文长度无关，
所以短调用同样需要一份不小的余量。
"""

from __future__ import annotations

#: 标准档。绝大多数调用点用它。
#:
#: 实测正文最长的一个是 ``xhs.note`` 的 1183 token（P11 之后还要多带 ``image_prompts``），
#: 其余短调用都在 1000 以内。定 8192 不是为了正文，而是为了思考：``xhs.angle`` 实测
#: 正文 853，却有一次把 4096 单独烧在思考上。8192 ≈ 实测正文上限的 7 倍，
#: 留给思考的净余量在 7000 token 量级。
STANDARD_OUTPUT_TOKENS = 8192

#: 大输出档。给"正文本身就长"或"实测已经顶满过天花板"的调用点。
#:
#: 两条硬数据：``sourcing.select`` 实测 9047（P10.1 已按 16000 定过）；
#: ``xhs.cards`` 实测**正好 4096 顶满被截断**，靠重试的 3565 才活下来。
#: 取值与 ``Settings.llm_article_max_tokens`` 的默认值一致——公众号长文链本来就在
#: 用这个数，同值就不用多开一个 dsh 桶。
LARGE_OUTPUT_TOKENS = 16000

#: 天花板档。**没有任何调用点显式声明它**，只由截断自愈从大输出档加码上来。
#:
#: 走到这一档说明 16000 的预算都被烧穿了，正常内容不该有这种体量——所以它是
#: "最后一次机会"而不是常规档位。运维想再压一道可以配 ``SW_DSH_MAX_TOKENS``。
CEILING_OUTPUT_TOKENS = 32000

#: 全部合法的**预算档位**，从小到大。完整 runtime 桶还包含 model 与 effort；开启分档
#: 路由后组合数可能超过默认 LRU 上限，不能把 ``len(OUTPUT_TIERS)`` 当作池总桶数上界。
OUTPUT_TIERS: tuple[int, ...] = (
    STANDARD_OUTPUT_TOKENS,
    LARGE_OUTPUT_TOKENS,
    CEILING_OUTPUT_TOKENS,
)

#: 每个调用点的输出预算。key 就是调用时传的 ``purpose``，两者写在相邻两行，
#: 方便 review 时一眼对上。改这里等于改预算，不需要翻三个生成链模块。
#:
#: "实测"列是 2026-08-16 那次成功运行的 ``CostLedger.output_tokens``；
#: 没有实测数据的调用点按"同类调用里最重的那个"归档。
CALL_SITE_BUDGETS: dict[str, int] = {
    # -- 选题 ----------------------------------------------------------------
    # 实测 9047：prompt 要求给每条候选（最多 30 条）各写一句理由 + 一句角度。
    # P10.1 已经按这个数定过，这里只是收编进统一梯子。
    "sourcing.select": LARGE_OUTPUT_TOKENS,
    # -- 小红书 --------------------------------------------------------------
    # 实测 853。事故当天同一个调用烧光 4096 且零输出 → 正文短不等于预算可以小。
    "xhs.angle": STANDARD_OUTPUT_TOKENS,
    # 实测 4096（顶满被截断）+ 重试 3565。全链最吃预算的一个：封面 + 最多 6 页
    # 内页脚本一次性产出，且实测证明 4096 不够。
    "xhs.cards": LARGE_OUTPUT_TOKENS,
    # 实测 1183，P11 之后这一步还要顺手产出 image_prompts，只会更长。
    "xhs.note": STANDARD_OUTPUT_TOKENS,
    # 实测 541。纯打分 + 几条建议。
    "xhs.selfcheck": STANDARD_OUTPUT_TOKENS,
    # 没有实测（当天没触发修订）。现在产出标题/正文/卡片/标签/配图 prompt 整包；
    # 结构虽比旧正文改写更宽，但正文仍受 1000 字硬上限，标准档保留充足思考余量。
    "xhs.dehumanize": STANDARD_OUTPUT_TOKENS,
    # -- 公众号 --------------------------------------------------------------
    # 大纲是短输出，但公众号长文的大纲本身就有十几条，按标准档。
    "wechat.outline": STANDARD_OUTPUT_TOKENS,
    # 正文/润色/改写都是整篇长文，本来就走 complete_long 的 16000。
    "wechat.body": LARGE_OUTPUT_TOKENS,
    "wechat.polish": LARGE_OUTPUT_TOKENS,
    "wechat.dehumanize": LARGE_OUTPUT_TOKENS,
    # 自评与配 meta 都是结构化短输出，对标 xhs.selfcheck。
    "wechat.selfcheck": STANDARD_OUTPUT_TOKENS,
    "wechat.meta": STANDARD_OUTPUT_TOKENS,
    # -- 抖音 ----------------------------------------------------------------
    "douyin.angle": STANDARD_OUTPUT_TOKENS,
    # 抖音链里结构最复杂的一次结构化输出：标题 + 钩子 + 口播稿 + 搜索词 + 话题 +
    # 封面文案 + image_prompts 一次产出。对标 xhs.cards 给大档。
    "douyin.script": LARGE_OUTPUT_TOKENS,
    "douyin.selfcheck": STANDARD_OUTPUT_TOKENS,
    "douyin.dehumanize": STANDARD_OUTPUT_TOKENS,
    # -- 生成链之外的结构化调用 ------------------------------------------------
    # 语义审核：结构化打分 + 几条问题说明。
    "review.semantic": STANDARD_OUTPUT_TOKENS,
    # 复盘报告：条目比自评多，但仍是结构化短文。
    "metrics.insights": STANDARD_OUTPUT_TOKENS,
}


def budget_for(purpose: str) -> int:
    """取某个调用点的输出预算。

    没登记过的 ``purpose`` 回落到标准档而不是抛异常——**这条路径在生产上跑生成链**，
    为了一个没登记的观测字符串把整条链炸掉不划算。漏登记由
    ``tests/generation/test_output_budget.py`` 兜住（它逐个核对已知调用点）。
    """
    return CALL_SITE_BUDGETS.get(purpose, STANDARD_OUTPUT_TOKENS)


def escalate(current: int) -> int | None:
    """截断自愈用：给出比 ``current`` 高的下一档；已经到顶返回 ``None``。

    刻意"抬到下一档"而不是直接 ×2：×2 会造出梯子之外的取值（8192×2 = 16384），
    等于给 dsh 多开一个 runtime 桶。抬到下一档既满足"预算翻一个量级"的诉求，
    又让加码后的那个桶**大概率已经活着**（大调用点本来就在用它），
    自愈这件事因此几乎不额外拉子进程。
    """
    for tier in OUTPUT_TIERS:
        if tier > current:
            return tier
    return None


__all__ = [
    "CALL_SITE_BUDGETS",
    "CEILING_OUTPUT_TOKENS",
    "LARGE_OUTPUT_TOKENS",
    "OUTPUT_TIERS",
    "STANDARD_OUTPUT_TOKENS",
    "budget_for",
    "escalate",
]
