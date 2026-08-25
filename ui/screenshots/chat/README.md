# 对话台截图（P15.H5，2026-08-19）

对话台 = hermes desktop fork（`sw-hermes-desktop`，分支 `sw-desktop`），Organic 换肤 +
运营特化。运维说明见 `docs/OPS.md` 第 7.7 节。

## 这些图是怎么来的

全部由 `apps/desktop/e2e/sw-elicitation-live.spec.ts` 现拍，链路如下：

```
electron → 真 hermes serve（profile sw 的 config）
         → 真 stdio MCP server（scripts/workbench_mcp.py，带 elicitation 闸门）
         → 真 core（ui/e2e/serve.sh 8000 起的隔离实例：独立 SQLite + fake 发布器）
```

**只有推理端是替身。** 上游 LLM 网关当时 429（至约 2026-08-23），活体回合做不了，
所以「模型」是 e2e 的脚本化 Mock Model——它只负责把工具叫起来。被取证的对象
（确认闸门、审批面板、工具卡、core 的读写行为）全都在模型下游，不受影响。
图里模型选择器显示 `Mock Model · Med`，就是这个替身，没有藏。

卡片里的数字是隔离 core 现算的真数据，不是 fixture。

复跑：

```bash
bash ui/e2e/serve.sh 8000
cd $HOME/project/social_workflow/sw-hermes-desktop/apps/desktop
SW_ELICIT_OUT=/abs/out npx playwright test e2e/sw-elicitation-live.spec.ts
```

## 图单

| 文件 | 画面 |
|---|---|
| `01-empty-*` | 空态起手：运营开场白 + 四枚 composer 药丸 |
| `02-tool-cards-*` | 一轮叫起五个只读工具，五张专用卡同框（真 core 数据） |
| `03-approval-*` | **审批面板**：`review_approve` 触发 MCP elicitation，Run / Reject 待人点 |
| `03b-approval-panel-*` | 同上，面板特写（展开 Command 可见确认正文原文） |
| `04-cancelled-*` | 点 Reject 之后的会话全貌 |
| `04b-card-cancelled-*` | 取消卡特写——**中性呈现，不是红色故障**（人的决定不是故障） |
| `05-approved-*` | 点 Run 之后：稿件 draft → scheduled，卡上写明排期时刻 |
| `06-card-*` | 五张只读卡各自特写：概览 / 审核队列 / 排期 / 账号健康 / 用量 |

亮暗两态各一张。

## 实测结论（红线）

- **拒绝路**：core 零写请求（access log 只有闸门取摘要那条 GET），稿件停在 `draft`，
  `updated_at` 一字未变，审计日志为空。
- **同意路**：`POST /api/v1/review/<id>/approve 200`，稿件 `draft → scheduled`，
  审计日志 `approve` 且 `actor="operator via sw-agent"`。

证据原文见 `docs/OPS.md` 7.7.4。
