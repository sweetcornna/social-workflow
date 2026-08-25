# 反检测红线（POLICY）

本文件是**硬约束**，优先级高于任何功能需求。子代理交付若违反本文任一条，主 agent 直接打回。

原文（来自实施计划 2.2 "关键设计细节（P0 冻结）"）：

> **反检测红线** `docs/POLICY.md`（解除 Patchright 与审计标准的冲突）：允许 Patchright
> 仅用于"真人账号避免被 headless 误杀"；**禁止**打码平台/验证码自动识别、批量虚拟身份
> 或 Cookie 池、一机多号指纹隔离、绕过平台限频。

## 允许 / 禁止

| ✅ 允许 | ❌ 禁止 |
|---|---|
| Patchright 有头浏览器，仅为让**真人自己的账号**不被 headless 检测误杀 | 任何指纹伪装 / 指纹隔离，用来让平台**无法区分**多个身份 |
| 一个真人账号一个 `profile_dir`，长期保活、长期复用 | Cookie 池、账号池、批量虚拟身份、一机多号指纹隔离 |
| 把二维码显示给人，让人**自己**扫码登录 | 自动登录绕过扫码、破解登录风控 |
| 把人**自己**收到的短信验证码，通过 UI 输入后转发给发布器 | 打码平台对接、OCR / 模型识别验证码、任何形式的自动过验证码 |
| 按平台公开规则**保守**限频（小红书日 ≤ 50，测试期 ≤ 10；抖音日 ≤ 2） | 绕过平台限频、并发刷量、多容器规避频控 |
| 采集平台**公开**页面上的公开指标 | 破解私有签名（a_bogus / x-s）、逆向鉴权、抓取非公开数据 |
| 生成内容后**人工逐条确认**再发布 | 完全 AI 驱动的无人值守发布（小红书 2026-03-10 公告：直接封禁） |
| GPL/AGPL 项目作为独立进程（Docker sidecar），HTTP 调用 | 把 GPL/AGPL 代码 import 进本仓库，或复制其源码片段 |
| 阅读无 License 仓库理解**行为流程**后自己重写 | 复制 social-auto-upload / MediaCrawler 等无 License、禁商用仓库的任何代码 |
| 凭据只放环境变量 | AppSecret / Cookie / Token 明文入库，或写进日志、写进 git |

## 为什么这些红线不能松

1. **合规**：小红书官方已明确对"完全 AI 驱动"的账号执行封禁。人工确认卡点既是产品设计，
   也是唯一能留下"人参与了"证据链的方式（`ReviewLog` 就是这条证据链）。
2. **法律**：验证码自动识别、指纹伪装、Cookie 池指向的是**规避身份核验**，
   与"帮真人管理自己的账号"是完全不同性质的行为。
3. **License**：无 License 的代码默认"保留所有权利"，复制即侵权；禁商用条款同理。

## 为什么保留人工确认（P12）

小红书 2026-03-10 公告直接对"完全 AI 驱动、无人值守"的账号执行封禁——这不是猜测的风险，
是已经发生的平台行为。用户的裁决（2026-08-17）是"自动到排期，发布前推消息给你点一下"：
系统可以全自动跑到"就等发"，**但发布前必须推消息给人、人点一下才真发**。这不是产品偏好，
是这条红线落地成具体机制的方式。

- `autopilot`（账号策略，默认 `false`）打开后，**只影响"自动批准"**：机器审核干净
  （block=0 且 warn=0）的稿子不用人再点一次"批准"，自动进入排期。
- `confirm_required`（账号策略，默认 `true`）是发布前的人工确认闸门，**独立于 `autopilot`**：
  不管 `autopilot` 开没开，只要 `confirm_required` 是 `true`，`tick_scheduled_publish` 在
  真正调用发布器之前都会再检查一次"有没有人点过确认"，没点就跳过（统计里的
  `skipped_unconfirmed`），绝不静默发出去。
- **这条闸门没有旁路，也不许有。** 打开 `autopilot` 不等于打开"全自动发布"——两个开关分别管
  两件不同的事：`autopilot` 省的是"人工审核"这一步，`confirm_required` 守的是"人工参与发布"
  这条合规底线，谁都不能把后者当成前者的副作用关掉。唯一的合法出口是账号级显式设
  `confirm_required: false`（`accounts.yaml` 或 `PATCH /api/v1/accounts/{id}`），这本身就是
  一次留痕的人工决定，不是代码路径上的旁路。
- 确认动作（工作台按钮或 Telegram 卡片，走同一个 `core.confirm.confirm_item`）全部写进
  `ReviewLog`——那是"人参与了"在系统里**唯一**的证据链，出事时靠它自证。

见 `core/confirm.py` 模块顶部文档与 `tests/test_confirm_gate.py::test_autopilot_does_not_open_the_publish_gate`
（红线用例：`autopilot` 打开后仍然 `skipped_unconfirmed`）。

## 实现约束（可被代码审计验证）

- 仓库内**不得出现**任何验证码识别 / 打码平台 SDK 依赖；
  `core/sms_inbox.py` 只做内存转发，验证码不落库、不写日志明文。
- `Account.profile_dir` 与账号一一对应；不提供任何"复用他人 profile"的入口。
- 限频由 `core/scheduler.py:RateLimiter` 统一实施（日上限 + 最小间隔），
  发布路径**只能**经 `publish_with_idempotency`，不允许旁路。
- 发布前必须处于 `approved`→`scheduled` 状态；`publish_with_idempotency` 会拒绝其它状态。
- 抖音发布器必须有头运行在真人的宿主机上，不入 Docker。**P3 落地后的可核验点**：
  `publishers/douyin/service.py:BrowserPool.context()` 只向 patchright 传
  `user_data_dir` / `channel` / `headless=False` 三个参数，`headless` 写死无开关，
  不设 user_agent、不加 args、不注入 init script；
  回归测试 `tests/publishers/test_douyin.py::test_browser_pool_launch_is_headful_and_carries_no_stealth`
  断言参数集合恰好是这三个，多一个就红。所有浏览器操作在单个 worker 线程里串行。
- 发布前人工确认（P12）：`tick_scheduled_publish` 对 `confirm_required` 的账号，**只认**
  `ContentItem.confirmed_at` 非空才放行；`autopilot_approve`（`core/confirm.py`）自动批准时
  只会推确认卡，从不代人点确认。凭据 / Telegram token 只走 `.env`，`core/telegram.py` 的
  `channel_status()` 明确不把 token（哪怕脱敏）吐给前端。

## 违规自查清单（提交前逐条确认）

- [ ] 没有新增 GPL/AGPL 的**代码依赖**（sidecar 不算）
- [ ] 没有复制任何无 License / 禁商用仓库的代码
- [ ] 没有验证码识别、指纹伪装、Cookie 池相关代码或依赖
- [ ] 没有凭据入库或进日志
- [ ] 新增的发布路径仍然强制经过人工 `approved` 与限频
- [ ] `autopilot` 相关改动没有绕开或削弱 `confirm_required` 这道发布前确认闸门
