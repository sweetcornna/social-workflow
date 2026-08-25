# prompts/ — Prompt 库

## 契约

Prompt 以 `.md` 存放，用 `{{变量}}` 占位，由 `prompts.load(name, **vars)` 渲染。
**Prompt 是版本化资产**：改动走 git diff，禁止在代码里拼长 prompt 字符串。

```python
import prompts
prompts.load("wechat/outline", topic_title="…", target_words=1500, ...)
prompts.load_persona("wechat-demo-01")      # 账号人设，不存在返回默认值
prompts.variables_of("wechat/outline")      # 该 prompt 用到的变量名集合
```

渲染刻意**不用 Jinja2**：prompt 里大量出现 `{` `}`（JSON 示例、正则），
Jinja 除 `{{ }}` 外还会解析 `{% %}` 与过滤器，误伤概率高。
这里只做最朴素的 `{{name}}` 全字符串替换，行为可预测。

**缺变量必须报错**（`PromptRenderError`）——静默留下 `{{x}}` 会被当成正文发出去。

## 当前布局

```
prompts/
├── wechat/                     # 公众号去 AI 味 SOP（P1 ✅）
│   ├── system.md               #   共用 system prompt（含人设、禁用表达）
│   ├── outline.md              #   1 大纲
│   ├── body.md                 #   2 正文
│   ├── polish.md               #   3 风格润色
│   ├── selfcheck.md            #   4 质量自评（结构化输出）
│   ├── dehumanize.md           #   5 去 AI 味改写（条件触发）
│   └── meta.md                 #   标题 / 摘要 / 封面提示词（结构化输出）
├── sourcing/select.md          # 选题打分（P1 ✅）
├── review/semantic.md          # LLM 语境审核（P1 ✅）
└── accounts/<account_id>/persona.md   # 账号人设
```

P2/P3 再加 `xhs/`（小红书笔记）与 `douyin/`（视频脚本）。

## 账号人设

优先级：`Account.extra["persona"]` > `prompts/accounts/<account_id>/persona.md`。
放进 `extra` 是为了让运营能在 UI 上临时改人设而不必改文件并重启。

`prompts/accounts/wechat-demo-01/persona.md` 是**示例**，用于本地联调与测试，
真实账号请新建目录，不要直接拿去发号。

persona 建议写清楚：定位、读者画像、语气、常写题材、**不碰的题材（硬约束）**、
惯用结构、禁用表达。"不碰的题材"这一节会直接影响选题 Agent 的 `risk` 打分。

## 写 prompt 的几条经验

- **说清楚要什么，别喊口号。** 当前模型对 system prompt 的遵循度很高，
  `CRITICAL: You MUST` 这类为了对抗老模型"不听话"而写的强调，
  现在会**过度触发**。用平实的祈使句。
- **给理由。** "不要写'在当今这个时代'"不如"这类铺垫和正文无关，读者会划走"。
- **正例比反例管用。** 与其列一堆"不要写 X"，不如给一个写对了的样子。
- **结构化输出别在 prompt 里描述 JSON 格式**——schema 由 Pydantic 模型给，
  prompt 只说明每个字段的判断标准。
- 长度、字数这类**平台硬限制不能只靠 prompt**，代码侧必须兜底截断
  （见 `generation/wechat_article.py` 的 `truncate`）。

## 反馈闭环

`ContentItem.review_notes`（人工驳回理由 + 机器审核结论）会作为改稿 prompt 的输入变量；
P4 的复盘 Agent 会把指标结果回灌到 `accounts/<name>/persona.md`。

## 测试

`tests/test_generation_wechat.py::test_all_wechat_prompts_render` 断言
**每个 prompt 的变量集与调用方完全一致**——改 prompt 忘了改调用方（或反之）
会在这里炸出来，而不是等到线上渲染出 `{{topic_title}}`。
