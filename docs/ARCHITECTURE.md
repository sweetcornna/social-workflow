# 架构

## 1. 总体分层（三层分离，浏览器自动化必须隔离）

```
┌─────────────────────────────────────────────────────────────┐
│  控制面 core/  (Python 3.12 · FastAPI · SQLite)               │
│  · 双状态机（见第 2 节）: ContentItem 状态 + Account 健康状态   │
│  · APScheduler 九个 tick（见第 8 节）· 重试/死信/幂等            │
│  · 按账号限频（DB 为准）+ 发布时段窗口 + 账号台账同步            │
│  · 审核队列 Web UI（预览图文/视频/文章，批准/驳回/改稿，diff， │
│    驳回理由回写 prompt，审核操作审计日志）                      │
│  · 人工介入页 /accounts/{id}/login（扫码二维码轮询、验证码输入）│
│  · 飞书/企微/邮件 通知（待审核、需重登、发布失败、成本超限）     │
└──────┬───────────────┬────────────────┬─────────────────────┘
       ▼               ▼                ▼
  sourcing/        generation/       review/
  选题采集          内容生成 Agent    审核
  (newsnow,        (LLM 接缝 +       (yuwen-precheck
   douyin-hot-hub,  模板渲染,          + 词库硬过滤
   XHS-Downloader   MPT sidecar,       + LLM 语义
   sidecar,         wenyan-cli)        + 人工卡点)
   TrendRadar)
       │               │                │
       │               │  ┌─────────────┴──────────────────────────┐
       │               │  │ generation/llm.py::SupportsLLM（见 10） │
       │               │  │  complete / complete_long / parse       │
       │               │  └────────────────────────────────────────┘
       └───────────────┴────────────────┘
                       ▼
             publishers/  发布执行层（每平台独立进程/容器，独立发版，共用 P0 冻结的契约）
             · wechat_mp: 官方 API 薄封装（HTTP 直调 + wechatpy 参考）draft/add → [certified] freepublish/submit
             · xhs:       xiaohongshu-mcp（Go sidecar, Apache-2.0）一账号一容器，走其 REST /api/v1/*（或 MCP）
             · douyin:    自研 Patchright 适配器（一号一 profile，有头，常驻宿主机进程
                          由 core 经本地 HTTP 驱动；参考 SAU 流程重写）
                       ▼
             metrics/   数据回流（发布后 24h/7d 双次快照，只追加）
                        └─ insights.py 复盘 Agent ──► prompts/accounts/<id>/insights.md
                                                        └──► sourcing/selector.py（闭环）
```

**编排选型**：代码优先的 Python 编排（FastAPI + APScheduler + 状态表），不引入
n8n / Temporal。理由：审核卡点需要富预览 UI（多图笔记 / 视频 / 文章），
n8n 的 Send-and-Wait 无法提供；代码优先便于单测与审计。

## 2. 双状态机

### ContentItem

```
            ┌──────────────── rejected ◄──────────┐
            │                    │                │
            ▼                    ▼                │
topic ──► drafting ──► draft ──► reviewing ──► approved ──► scheduled
                         ▲          │                            │  ▲
                         └──────────┘                            │  │
                                                       ┌─────────┘  │
                                                       ▼            │
                                                   publishing    suspended
                                                    │      │        ▲
                                       ┌────────────┘      └────────┘
                                       ▼                （账号 needs_relogin）
                                   published ──► measured ⟲
                                       ▲
                                       │
publishing ──► publish_failed ──► retrying ──┘
                     │                 │
                     └──► dead_letter ◄┘
```

迁移表是唯一真相：`core/state_machine.py:CONTENT_TRANSITIONS`。非法迁移抛 `IllegalTransition`。

关键不变量：
- **发布必经人工卡点**：只有 `approved → scheduled` 之后才能进 `publishing`。
- **`approved → scheduled` 由批准动作本身完成**：`POST /review/{id}/approve` 成功后立刻调
  `core/scheduling.py:schedule_item`，按账号 `publish_windows` / `min_interval` / 日上限算出槽位
  写进 `scheduled_at`，页面上直接回显"已排期至 …"。算不出槽位就**停在 `approved`** 并把原因
  告诉审核人（改 `accounts.yaml` 或改 `schedule_at`），不排一个永远发不出去的时刻。
  这两个约束在 `tick_scheduled_publish` 发布时**还会再校验一遍**（纵深防御：手工改库、
  时钟漂移都可能绕过排期层），两层都由 `scripts/soak.py` 盯着。
- `dead_letter` 是终态，需人工处理。
- `measured` 自环，容纳 24h / 7d 两次快照。
- `suspended` 只由账号级事件产生，`prev_status` 记录挂起前状态，恢复时放回。

### Account

```
        ┌──────────────┐
        ▼              │
      ok ⇄ degraded ⇄ needs_relogin
        │        │            │
        └────────┴────────────┴──► banned（人工终态）
```

登录过期是**账号级**事件，不挂在内容状态上。进入 `needs_relogin` 时：
1. 该账号所有 `scheduled` 项 → `suspended`（记 `prev_status`）；
2. 发通知，附 `/accounts/{id}/login` 链接；
3. 人工扫码续期后 `restore_account` 把挂起项放回。

## 3. 幂等与两阶段发布

```
idem_key = sha256(account_id | platform | content_hash | scheduled_slot)   # DB UNIQUE
content_hash = sha256(title + body_markdown + media 路径序列)
```

```
publish_with_idempotency(session, item, publisher)
  │
  ├─ prepare(bundle)                      归一化（幂等）
  ├─ 计算 idem_key
  ├─ dry_run? ──► publish() 后直接返回，不写记录、不改状态
  ├─ 查 PublishRecord
  │    ├─ 不存在 ─► SAVEPOINT 内 INSERT(phase=in_flight)
  │    │              └─ UNIQUE 冲突（并发）─► 转「已存在」分支
  │    └─ 已存在
  │         ├─ phase=done ─► 直接返回既有 post_id，**绝不重发**
  │         └─ 否则 ─► publisher.reconcile(bundle) 平台侧对账
  │                      └─ 命中 ─► 补记 post_id/url，phase=done，内容→published
  ├─ attempts += 1，内容 → publishing
  ├─ publish(bundle)
  │    ├─ 成功 ─► phase=done，补 post_id/url，内容 → published，写 ReviewLog + 通知
  │    ├─ RetryableError    ─► publish_failed → retrying；attempts ≥ 上限则 → dead_letter
  │    ├─ NeedsReloginError ─► publish_failed → retrying；账号 → needs_relogin（挂起排期项）
  │    │                        **不计入重试上限**（等人工续期，不该被重试烧掉）
  │    └─ PermanentError / 其它 ─► publish_failed → dead_letter
```

`reconcile` 的存在是为了解决"发成功但回包丢失"：平台侧已经有笔记/草稿了，
本地却以为失败，重试会造成重复发布。各平台的对账依据：
XHS 拉用户主页最近笔记比对标题+首图；公众号拉 `draft/batchget` 比对。

## 4. 数据模型

| 表 | 作用 | 特别约定 |
|---|---|---|
| `accounts` | 账号台账 + 健康状态 | `sidecar_endpoint`(xhs/douyin)、`profile_dir`(douyin)、`daily_limit` |
| `topics` | 选题池 | `raw` 保留源站原始 JSON |
| `content_items` | 一条待发布内容 | `bundle_json` = 序列化的 `ContentBundle`；`prev_status` 供挂起恢复 |
| `publish_records` | 两阶段发布记录 | `idem_key` **UNIQUE**；`phase ∈ {in_flight, done, failed}` |
| `metric_snapshots` | 指标快照 | **只追加**，永不更新删除 |
| `review_logs` | 审核审计日志 | 人工 `approve/reject/edit` + 系统事件；合规证据链 |
| `render_jobs` | 异步渲染任务（P3）| **刻意无外键**：渲染先于 ContentItem 落库 |
| `cost_ledger` | 成本流水 | 按 UTC 日 + kind(`tokens`/`render_seconds`) 累计；`meta['account_id']` 供按账号归集 |

时间统一 UTC：`UTCDateTime` 类型装饰器存 naive UTC、读出带 tzinfo，避免混用。

**P4 没有新增表也没有改既有字段**：调度策略全部落在 `Account.extra`（JSON），
限频与退避由 `publish_records` 推导，复盘结论写文件。这样老库直接启动即可，
不需要任何手工 DDL。

## 5. 发布契约（P0 冻结）

```python
class Publisher(ABC):
    platform: ClassVar[str]
    dry_run: bool                                  # 属性
    def prepare(self, bundle) -> ContentBundle     # 必须幂等
    def publish(self, bundle) -> PublishResult     # 失败必须抛 PublishError 子类
    def health(self) -> AccountHealth              # ok|degraded|needs_relogin|banned
    def fetch_metrics(self, platform_post_id) -> dict
    def reconcile(self, bundle) -> PublishResult | None   # 平台侧对账
```

异常分类 `PublishError → RetryableError / NeedsReloginError / PermanentError`
是调度器唯一的分流依据；实现方不得抛未分类异常表达业务失败。

DTO 扩展点只有 `ContentBundle.platform_extra`；`extra="forbid"` 保证私自加字段会在
校验期直接失败。契约测试 `tests/contract/test_publisher_contract.py` 参数化跑所有实现。

## 6. 人工介入通道

| 场景 | 通道 | 红线 |
|---|---|---|
| 小红书扫码登录 | `/accounts/{id}/login` 轮询 `/login/qrcode` 拿 base64 PNG | 只显示给人扫，不自动登录 |
| 抖音短信验证码 | `/accounts/{id}/login/code` → `core/sms_inbox.py` 内存队列 → 发布器取用 | **不做任何自动识别**；不落库不写日志 |
| 内容审核 | `/review/{id}` 批准 / 驳回 / 改稿 → `ReviewLog` | 发布前必经，构成合规证据链 |

## 7. 部署形态（MVP 单机）

```
宿主机 (macOS)
├── docker compose
│   ├── core            :8000   控制面 + 审核 UI + APScheduler（SW_SCHEDULER_ENABLED）
│   ├── mpt             :8080   profile=video     （MIT，视频合成）
│   ├── xhs-downloader  :5556   profile=xhs       （**GPL**，独立进程）
│   ├── trendradar      :8081   profile=sourcing  （**GPL**，静态文件服务，非 API）
│   └── xhs-<account>   :1806N  一账号一容器（由 gen_xhs_sidecars.py 生成）
└── 抖音发布器（有头 Patchright，常驻，**不进 Docker**） :8710
```

数据库 SQLite（`core_data` volume）。Postgres 迁移仍未评估——单机 MVP 够用，
真正的瓶颈会先出现在"多进程调度器需要选主"上（见 `docs/OPS.md` 1.6）。

## 8. 调度（P4）

九个 tick 注册在 `core/scheduler.py:TICKS`，**手动触发（`POST /dev/tick/{name}`）
与定时执行共用同一份注册表**——不存在"手动能跑、定时不跑"的分叉，有回归测试
断言 `create_scheduler()` 注册的 job 集合恰好等于 `TICKS`。

```
              tick_confirm_gate ─► 推卡 / 提醒 / TTL 驳回 ──┐
                  (1min)                                    ▼
tick_sourcing ──► topics ──► tick_generate ──► [人工审核] ──► tick_scheduled_publish
   (6h)                          (30min)         /review          (1min)
                                                                     │
              tick_retry_sweep ◄── retrying ◄──────────┬─────────────┤
                  (5min)              │                │             ▼
                                      └──► dead_letter │        published
                                                       │             │
              tick_login_health ─► Account 健康 ───────┘             ▼
                  (10min)                                        tick_metrics
              tick_render_jobs ─► 成片补挂回内容包                    (6h)
                  (1min)                                             │
              tick_insights ◄──────── 7d 指标汇总 ◄──────────────────┘
                  (6h)  └─► insights.md ─► 下一轮选题
```

三条贯穿所有 tick 的规则：

1. **只认 DB 里的账号**。`accounts.yaml` 是台账的唯一真相，但要先
   `python -m core.accounts sync` 才进 DB；同步不覆盖 `Account.status`。
2. **按账号健康过滤**。发布与重试两条路都只放行 `ok` 的账号。
   这修掉了 P3 的一个洞：`NeedsReloginError` 把内容打到 `retrying`，而
   `mark_account_needs_relogin` 只挂起 `scheduled` 的项。
3. **限频真相在 DB**（`core/ratelimit.py`）。当日已发数与最近发布时刻来自
   `PublishRecord(phase='done')`，进程内计数只是 30 秒 TTL 的缓存，
   合并策略 `max(DB, 本地)`——宁可少发不可多发，重启不清零。

`tick_scheduled_publish` 的六道闸门（顺序不可换，越便宜的越先判）：
账号健康 → 发布时段窗口 → 限频 → 人工确认 → 发布器可用 → 幂等键 + 平台侧对账。
每道各有一个 `skipped_*` 计数且都计入总 `skipped`，排查时一眼能看出是被谁拦的，
`scanned == published + skipped + failed` 恒成立。逐道的触发条件见 `docs/OPS.md` 1.6。

人工确认（P12，`core/confirm.py`）默认开启（`AccountPolicy.confirm_required` 缺省
`true`）。拦它的不是"确认闸门放行"，而是 `tick_scheduled_publish` 自己发现
`confirmed_at` 为空就跳过（计入 `skipped_unconfirmed`）；`tick_confirm_gate` 只负责
在这之前把确认卡推出去、槽位前补一次提醒、以及 `SW_CONFIRM_TTL_HOURS` 到点后自动
驳回并释放排期槽位。确认动作有两条独立通道——Telegram 卡片按钮与工作台「确认
发布」——最终都调用同一个 `core.confirm.confirm_item`，没有 Telegram 也不影响
工作台这条路。

账号级策略来自 `Account.extra`（`daily_target` / `publish_windows` /
`min_interval_minutes` / `timezone`），解析在 `core/accounts.py:AccountPolicy`。
平台缺省值是**下限**：台账把抖音的间隔写成 5 分钟，实际仍按 30 分钟走。

## 9. 反馈闭环（P4）

P3 之前 `metrics/` 是只写不读的：指标存下来没人用，选题 Agent 每天从零开始。
P4 补上回路：

```
MetricSnapshot(7d) ──► metrics/insights.py ──► Claude 结构化复盘（InsightsReport）
                                                        │
                          prompts/accounts/<id>/insights.md（追加，保留最近 N 条）
                                                        │
                          sourcing/selector.py ◄────────┘  作为选题 prompt 的一段
```

两个刻意的设计：

- **写文件不写库**。复盘结论是人也要看、要能手改的资产，和 `persona.md` 一样
  应该躺在 git 里出 diff，而不是埋在 SQLite 的某个 JSON 列里。
- **无 API key 时整体跳过**，不像生成链那样回落 ScriptedLLM。生成的稿子人会审，
  假的复盘结论却会**持续**污染后续每一次选题决策。

## 10. LLM 后端接缝（P5）

所有"思考"环节——选题打分、文案/脚本、语义审核、复盘——都只依赖
`generation/llm.py` 的 `SupportsLLM` 协议（`complete` / `complete_long` / `parse`）。
P5 在这条接缝下面加了第二个实现，`core/` 确定性控制面一行没动。

```
        sourcing/selector.py   generation/*.py   review/llm_semantic.py   metrics/insights.py
                    │                │                    │                      │
                    └────────────────┴────────────────────┴──────────────────────┘
                                             ▼
                          generation/llm.py :: SupportsLLM（协议，P1 冻结）
                          complete(prompt, system, max_tokens, effort, purpose) -> LLMResult
                          complete_long(...)                                    -> LLMResult
                          parse(prompt, PydanticModel, ...)                     -> ParsedResult
                                             │
              build_llm(budget=...)  按 SW_LLM_BACKEND 选实现，凭据都缺时回落 ScriptedLLM
                    ┌────────────────────────┼────────────────────────┐
                    ▼                        ▼                        ▼
            LLMClient（默认）           DshLLM（P5 新增）          ScriptedLLM
            anthropic SDK              deepseek-harness           预置回复
            HTTPS → Messages API       runtime 子进程             （离线单测 / 无凭据联调）
            · server-side fallback     · stdio JSON-RPC
            · messages.parse 原生      · schema 注入 + 取最后一个 JSON
            · usage 来自响应体          · usage 来自 session events
                                       · 受限 cordis 组合：模型零工具
                                             │
                                   configs/dsh/cordis.yml
                                   ├─ dsh-sdk-jsonrpc-server（stdio 协议入口）
                                   ├─ dsh-llm-pi-ai（provider 路由，凭据只写 apiKeyEnv）
                                   ├─ dsh-agent-spine-demo（bash/skill/jobs 全部关闭）
                                   └─ dsh-session-persistence-jsonl（data/dsh_sessions/）
```

**两个实现的行为差异**（上层不需要感知，但排障时要知道）：

| 维度 | `LLMClient`（anthropic） | `DshLLM`（dsh） |
|---|---|---|
| 传输 | HTTPS 直连 | 本机子进程 + stdio JSON-RPC |
| 结构化输出 | SDK 原生 `messages.parse` | prompt 注入 JSON Schema，回复里取**最后一个**顶层 JSON 对象；格式错回喂错误信息重试一次，被截断则抬一档预算重发原 prompt |
| `system` | 独立 system 字段（带缓存断点） | 线协议无 per-call system 通道，拼进用户消息开头 |
| `model` / `effort` / `max_tokens` | 每次调用可变 | 进程级；按 `(model, effort, max_tokens)` 分桶起 runtime，池子 LRU 封顶。预算维度收敛到 `generation/output_budget.py` 的三档；关闭路由时是旧的单模型语义，分档路由开启后组合数可能超过默认池上限并由 LRU 回收 |
| 拒答 | `stop_reason == "refusal"` → `GenerationRefused` | `turn/end.kind == "blocked"` → `GenerationRefused` |
| 截断 | `stop_reason == "max_tokens"`，返回截断文本 | `turn/end.kind == "max-tokens"`：有文本时同样返回（stop_reason 归一成 `max_tokens`）；**零文本**时抬一档预算自愈重试一次，两次都截断才抛 |
| 前缀缓存 | system 段挂 `cache_control: ephemeral`（显式断点） | 上游网关不做隐式缓存，靠请求体的 `prompt_cache_key`：开关是 cordis provider 段的 `cacheRetention: long`，取值被 runtime 写死成 session id 的前 64 字符，于是本仓把「组键 + 唯一后缀」叠进同一个 session id（`generation/llm_dsh.py::prompt_cache_key`），组按 `(model, system 段)` 划 |
| usage | 响应体 `usage` | session events 里 `assistant/message.data.usage`（四桶互不重叠），跨 step 求和 |
| 并发 | SDK 自身线程安全 | 进程内一把锁串行 |

两边对上层的同形由 `tests/generation/test_llm_backend_contract.py` 逐条压住。
运维口径（安装、切换、回退、会话目录清理、两道零工具验证）见 `docs/OPS.md` §dsh。
