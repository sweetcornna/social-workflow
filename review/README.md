# review/ — 审核管线

**P1 已落地。** 人工卡点在 P0 就有了（`core/main.py` 的 `/review/{id}/approve|reject|edit`
→ `ReviewLog`），本目录补的是它前面的三级机器审核 + 发布前结构校验。

```
词面硬过滤 → 平台违禁 → LLM 语境判定 → 发布前校验 → 人工卡点
 lexicon      precheck    llm_semantic     inspect      (core)
```

## 模块

| 模块 | 作用 | 复用（License） |
|---|---|---|
| `base.py` | `Finding` / `ReviewResult` 契约 | — |
| `lexicon.py` | 词库加载 + Aho-Corasick 多模匹配 | `konsheng/Sensitive-lexicon`(MIT) 词库数据 |
| `precheck.py` | 平台违禁规则匹配 | `yuwen-publish-precheck`(MIT) **vendored 规则数据** |
| `llm_semantic.py` | LLM 语境判定，结构化输出 | `anthropic`(MIT) |
| `inspect.py` | 发布前结构校验，`inspect --json` 形态 | — |
| `pipeline.py` | 串起四级 + 接状态机 | — |

## Finding 三个等级

| level | 含义 | 后果 |
|---|---|---|
| `block` | 不能发 | 内容留在 `draft`，findings 写进 `review_notes` |
| `warn` | 可疑，人来拍板 | 进人工队列，详情页高亮 |
| `info` | 提示 | 不阻断（如"这里不必做谐音规避"） |

## 各级说明

### 1 词面硬过滤 `lexicon.py`

自己写的 Aho-Corasick，不装 `pyahocorasick`（C 扩展要编译）。规模够用：
建机 O(总词长)，扫描 O(文本长)，**不随词数增长**。朴素 `for word in words` 在
十万词时单篇文章要几秒，接受不了。

- 词库**不进 git**（单文件最大 700KB+），`scripts/fetch_lexicon.py` 下到 `data/lexicon/`。
- **没下载不报错**，退化到内置兜底词表 + 一条 `lexicon.not_installed` 的 info，
  保证离线单测与首次运行不炸，但不会让人误以为"审核过了"。
- 匹配前删零宽字符（最常见的规避手法），但保留下标映射，
  命中位置回指**原文**，人工复核的 excerpt 才不会错位。
- `非法网址.txt` 被排除：那是域名清单，拿去做子串匹配会把正文里的 `com`/`cn` 误伤。
- 单字词跳过，误伤率太高。

类目 → 等级映射见 `CATEGORY_LEVELS`（政治/反动/暴恐/涉枪涉爆/色情 = block，其余 warn）。

### 2 平台违禁 `precheck.py`

上游不是 pip/npm 包（是 Claude Agent Skill），按兜底方案 vendored **只拷规则数据**，
不拷面向 agent 的指令性文档——理由见 `review/vendor/yuwen_precheck/PROVENANCE.md`。
匹配逻辑本项目自己实现，接本项目的 `Finding` 契约。

- `commercial=True` 才启用带货/极限词规则；`industries={"medical", ...}` 才启用行业规则。
- `debunked_myths` 是**反向**规则：命中说明作者在做没必要的谐音规避（"赚米"），
  输出 `info` 建议改回正常表达，不是违规。

### 3 LLM 语境判定 `llm_semantic.py`

前两级只看字面，误报率高——"扫黄打非取得成效"会命中涉黄词表。这一级把命中片段
连同全文交给 Claude 判断**当前语境**。

- **只在前两级有 block/warn 命中时才调**，没命中不烧 token。
- LLM 判 `safe` 时把 finding **降级为 info 而不是删掉**——审核链路宁可留噪声，
  也不要静默丢证据。
- 模型抄错 `rule` id 时保留原判定，不让一条 block 悄悄消失。
- 任何异常（缺 key / 限流 / 预算耗尽）都**不阻断管线**：跳过本级，
  在 `stages_skipped` 记原因，前两级结论直接生效（更严格，方向安全）。

### 4 发布前校验 `inspect.py`

确定性规则，不调 LLM，可以在发布路径上同步跑。管的是"平台会不会直接拒收"：

标题 ≤32 字 · 摘要 ≤120 字 · 正文 ≥200 字 · `body_html` 存在 ·
媒体文件本地存在 · 正文无外链图片（必须是 `mmbiz.qpic.cn`）· `platform_extra` 字段完整。

`InspectReport.model_dump_json()` 就是 `inspect --json` 的输出。

## 状态机接线

`review_item(session, item)`：`draft → reviewing → draft`。

"draft_reviewed" **不是新状态**——P0 冻结的状态机里没有它，也不允许新增。
落地方式是跑完机器审核后放回 `draft`，结论写 `review_notes`，
`ReviewLog(actor="system", action="machine_review")` 留痕。人工批准仍走原有 `/approve`。

只接受 `draft` 状态进来：已经 `approved` 的内容不该被机器审核悄悄打回，那要走人工撤回。

## 运行

```bash
uv run python scripts/fetch_lexicon.py          # 下词库（首次必须）
uv run python scripts/fetch_lexicon.py --list   # 只看远端有哪些词表
uv run python scripts/fetch_lexicon.py --precheck-only  # 只刷新 precheck 规则
uv run pytest tests/test_review.py -q
```

## 故障排查

| 现象 | 排查 |
|---|---|
| findings 里有 `lexicon.not_installed` | 词库没下，跑 `scripts/fetch_lexicon.py` |
| `PrecheckDataMissing` | `review/vendor/yuwen_precheck/terms.json` 丢了，`--precheck-only` 重拉 |
| `stages_skipped` 里有 `llm_semantic` | 看原因字段：缺 key / 无命中 / 预算耗尽，都是预期行为 |
| 正常内容被判 block | 看 `Finding.rule`：`lexicon.*` 调 `CATEGORY_LEVELS`；误报应由第三级降级 |
| `inspect.image.external_host` | 正文有外链图，公众号会过滤掉，必须先过素材库 |

## 红线

审核只做内容合规，**不做**任何"绕过平台检测"的改写——
`prompts/review/semantic.md` 里明确禁止模型建议谐音/拆字/同音替换。
