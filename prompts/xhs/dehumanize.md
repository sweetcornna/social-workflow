对下面这条小红书笔记做一次**整包修订**。这是唯一一次修订机会；必须同时修正所有字段，
让标题、正文、卡片、标签和生图 prompts 互相一致。

## 选题 Agent 承诺的切入角度

{{suggested_angle}}

修订后的所有字段必须兑现这个角度，不能为了解决局部问题悄悄换题。值为“（无）”时表示
没有上游承诺，本项不构成额外限制。

## 当前标题

{{title}}

## 当前备选标题

{{alt_titles}}

## 当前最终发布正文（已包含末尾话题标签）

{{body}}

## 当前卡片脚本

{{cards}}

## 当前话题标签

{{tags}}

## 当前生图 prompts

{{image_prompts}}

## 需要解决的问题

{{issues}}

## 输出 schema

- 必须输出与结构化模型完全一致的一整个对象，字段只能是：`title`、`alt_titles`、`body`、
  `tags`、`cover_headline`、`pages`、`image_prompts`。
- `pages` 每项字段只能是 `headline`、`bullets`、`footnote`。
- 不要省略未修改字段，不要输出解释或代码围栏。

## 修订要求

- `title` 不超过 {{max_title}} 字；`body` 不超过 {{max_body}} 字；`cover_headline`
  不超过 {{max_cover}} 字。
- `pages` 为 {{min_pages}} 到 {{max_pages}} 页。每页 `headline` 不超过
  {{max_headline}} 字，`bullets` 为 {{min_bullets}} 到 {{max_bullets}} 条且每条不超过
  {{max_bullet}} 字，`footnote` 不超过 {{max_footnote}} 字。
- `tags` 为 {{min_tags}} 到 {{max_tags}} 个，不带 `#`。
- 输出对象里的 `body` 只写正文主体，不要重复末尾的 `#话题标签`；标签只放进 `tags`，
  生成链会在终检前生成唯一的最终发布正文。
- `image_prompts` 必须正好 {{image_prompt_count}} 条；若数量是 0，必须给空数组。
- 信息不能变少：原来有的数字、步骤、踩坑细节一个都不能丢。
  改不动的地方就保持原样，不要为了"改过"而换同义词。
- 打散节奏：句子长短不一，允许一句话独立成段。
- 删掉所有过渡词、总结句、以及任何"希望对你有帮助"式的结尾。
- 至少保留一处**只有真人才会写**的东西：一个具体时间、一个具体价格、
  一次失败、一个不完美的细节。原文里如果没有，从原文已有的信息里挖，**不要编**。
- emoji 全篇不超过 3 个。
- 对照所有字段统一核心数字、金额、尺寸、数量、时长与结论。
- 生图构图必须满足通用物理安全：液体远离插座/通电线材，热源远离易燃物，清洁剂与
  食品分开，不遮挡通风/逃生，没有绊倒线缆或不稳定堆叠。发现风险时直接改成安全的
  替代摆放方式，不能只堆 negative words。
