# 运维手册

| 节 | 内容 |
|---|---|
| [1](#1-首次部署清单) | 首次部署清单（**从这里开始**）与日常门禁 |
| [1.5](#15-账号台账与调度策略) | 账号台账 `accounts.yaml` → DB，账号级调度策略 |
| [1.6](#16-调度与定时任务) | 九个 tick、手动触发、限频与发布时段窗口 |
| [2](#2-登录续期) | 登录续期（小红书 / 抖音 / 公众号） |
| [3](#3-ip-白名单errcode-40164) | 公众号 IP 白名单（errcode 40164） |
| [3.5](#35-真实发布的回滚--撤稿手册) | 真实发布的回滚 / 撤稿手册（按平台 × 阶段） |
| [4](#4-验证码通道) | 验证码通道 |
| [4.5](#45-视频渲染moneyprinterturbop3) | 视频渲染 sidecar（MoneyPrinterTurbo） |
| [4.6](#46-热榜采集-sidecartrendradarp4) | 热榜采集 sidecar（TrendRadar） |
| [4.7](#47-复盘-agent-与统计页p4) | 复盘 Agent 与统计页 |
| [5](#5-故障处置) | 故障处置速查表 |
| [5.1](#51-指标采集-future-bucket-时钟异常) | 指标采集 future bucket 时钟异常：诊断、受控恢复与回滚 |
| [5.5](#55-连续运行验证soak) | 连续运行验证（soak） |
| [6](#6-备份) | 备份 |
| [6.5](#65-sidecar-升级) | sidecar 升级 |
| [6.6](#66-只读部署核验verifysh) | 只读部署核验（verify.sh）：HEAD / 端口 / 健康 / 确认闸门 / 409 |
| [6.7](#67-生产运维命令与改生产-env) | 生产运维命令一览；**改生产 `.env` 为什么"重启"不够**（env_set.sh） |
| [7](#7-成本闸门调整) | 成本闸门调整 |
| [7.2](#72-生图配图p11) | 生图配图：开通、预算、尺寸不可信 |
| [7.7](#77-对话台已移除2026-08-27) | 对话台（hermes desktop）：**已移除**，去向与保留项 |
| [8](#8-本地工作台与隧道控制生产p17c) | 本地工作台与隧道控制生产：拓扑、日常三步、安全边界 |
| [9](#9-待补) | 待补 |

## 1. 首次部署清单

按顺序做，每一步都能单独验证。**第 3 步是最容易漏的**——不做的话所有
`/dev/*` 端点与调度器都会说"账号不存在"，看起来像代码坏了。

```bash
# 1) 依赖与配置
uv sync                                  # core（CI / 服务器）
uv sync --extra render                   # 需要出封面 / 小红书卡片时
uv sync --extra douyin                   # **只在有图形界面的宿主机上**装抖音上传器
cp .env.example .env && $EDITOR .env     # 填 ANTHROPIC_API_KEY 等凭据

# 2) 建库（也可以直接起 core，lifespan 里会 create_all）
uv run python -c "from core import db; db.configure(); db.init_db()"

# 3) ★ 账号台账入库 ★（幂等；不覆盖运行时 status）
uv run python -m core.accounts sync
uv run python -m core.accounts list      # 确认 4 个 demo 账号都在

# 4) 门禁
uv run python scripts/preflight.py       # FAIL 必须修
uv run python scripts/preflight.py --strict   # 上线前：WARN 也当失败

# 5) 起控制面
uv run uvicorn core.main:app --port 8000
#   或 docker compose up -d core

# 6) 按需起 sidecar
uv run python scripts/gen_xhs_sidecars.py            # 一账号一容器的 compose 片段
docker compose -f docker-compose.yml -f docker-compose.xhs.yml up -d
docker compose --profile video    up -d mpt          # 抖音成片（先 cp config.toml，见 4.5）
docker compose --profile sourcing up -d trendradar   # 热榜聚合（先 cp config，见 4.6）

# 7) 抖音上传器（宿主机常驻，有头浏览器，**不进 Docker**）
uv run python -m publishers.douyin serve --port 8710

# 8) 各账号首次扫码登录
open http://localhost:8000/accounts       # 逐个点进 /accounts/{id}/login

# 9) 冒烟：手动跑一遍全链路
curl -sX POST 'http://localhost:8000/dev/tick/sourcing'
curl -sX POST 'http://localhost:8000/dev/tick/generate'
open http://localhost:8000/review         # 人工批准
curl -sX POST 'http://localhost:8000/dev/tick/scheduled_publish'
open http://localhost:8000/stats
```

`SW_SYNC_ACCOUNTS_ON_START=true`（默认）时第 3 步在 core 启动时会自动跑一遍，
但**仍然建议显式执行**：命令行能看到"新建 / 更新 / 台账外"的明细，启动日志里容易划过去。

### 日常门禁

```bash
uv run python scripts/preflight.py           # 每次开工 / 每次发布前
uv run python scripts/preflight.py --offline # 无网环境
uv run python scripts/preflight.py --strict  # 上线前，WARN 也当失败
```

FAIL 必须修；WARN 不阻塞开发，但阻塞对应平台的**真实发布**。P4 起它还会检查：

| 检查项 | FAIL 的含义 |
|---|---|
| 台账已入库 | `accounts.yaml` 里有账号没 sync 进 DB → 调度器看不见它们 |
| 调度参数 | 退避上限小于下限、批量为 0、时区不可用（缺 tzdata） |
| 抖音上传器 headless | 上传器跑在无头模式 → 抖音当场识破（`docs/POLICY.md` 红线） |
| 环境变量命名 | 只 WARN：仍在用 `NODE_BIN` / `ANTHROPIC_MODEL` / `DOUYIN_AGENT_BASE_URL` 等历史别名 |

## 1.5 账号台账与调度策略

**`accounts.yaml` 是台账的唯一真相，但调度器只认 DB。** 两者靠 `core.accounts` 同步：

```bash
uv run python -m core.accounts sync             # 幂等 upsert
uv run python -m core.accounts sync --dry-run   # 只看差异
uv run python -m core.accounts check            # 不一致时退出码 1（CI / preflight 用）
uv run python -m core.accounts list             # 看 DB 里的账号与生效的策略
curl -sX POST localhost:8000/dev/sync_accounts  # 等价的 HTTP 入口
```

同步规则（有回归测试盯着）：

- 只写台账字段：`platform / name / daily_limit / profile_dir / sidecar_endpoint / extra`；
- **绝不覆盖 `Account.status`**——那是登录巡检的地盘。被 YAML 冲掉会把
  `needs_relogin` 的账号悄悄改回 `ok`，排期就发到一个掉线的号上去了；
- `extra` 合并时只保留运行时键（`insights_updated_at` / `insights_error` / `seeded`），
  其余以 YAML 为准。所以从 YAML 里删掉 `publish_windows` 才能真的把它删掉；
- DB 里有、台账里没有的账号**只报告不删除**（历史内容还挂在它上面）。

### 账号级调度策略

以下字段写在 `accounts.yaml` 顶层，同步时落进 `Account.extra`：

| 字段 | 作用 | 缺省 |
|---|---|---|
| `daily_target` | `tick_generate` 每天给这个账号出几条稿 | `0` = 不自动生成 |
| `publish_windows` | 发布时段窗口，如 `["09:00-11:00", "19:00-22:30"]` | 空 = 全天 |
| `min_interval_minutes` | 两次发布的最小间隔 | 平台缺省（抖音 30，其余 `SW_MIN_PUBLISH_INTERVAL_SECONDS`）|
| `timezone` | 窗口用的时区 | `SW_TIMEZONE`（`Asia/Shanghai`）|
| `autopilot`（P12） | 机器审核干净（block=0 且 warn=0）的稿子自动批准并排期 | `false` |
| `confirm_required`（P12） | 发布前要不要人点一下确认；**没有旁路**，见 `docs/POLICY.md` | `true` |
| `confirm_ttl_hours`（P12） | 推了确认卡这么多小时没人点就自动驳回、释放排期槽位 | `24` |

`autopilot` / `confirm_required` 也能通过工作台 `PATCH /api/v1/accounts/{id}` 改，改完同样是
"先台账、后库"；`confirm_ttl_hours` 目前只能直接改这个文件再 `sync`。详见
[7.6 发布前确认（Telegram，P12）](#76-发布前确认telegramp12)。

几条必须知道的：

- **窗口按账号时区判定**，不是 UTC。`09:00-11:00` + `Asia/Shanghai` = UTC 01:00–03:00。
- 支持跨零点：`22:00-02:00` 合法。起止相同（`09:00-09:00`）会被当成配置错误报出来。
- **平台缺省的最小间隔是下限**：抖音写 `min_interval_minutes: 5` 也仍然按 30 分钟走
  （`docs/POLICY.md` 的保守限频不允许被台账放宽）。
- `daily_limit` 会被平台硬顶夹住：抖音 ≤ 10（`DAILY_LIMIT_CEILING`）、小红书 ≤ 50。
- 台账写错不会让调度器崩，只会退化成缺省并留 WARN 日志——但**不会有人看到**，
  所以改完台账一定要 `python -m core.accounts list` 确认策略真的生效了。

### 限频的真相在 DB

P4 起 `core/ratelimit.py` 的日计数与"最近发布时刻"来自
`PublishRecord`（`phase='done'`），进程内计数只是 30 秒 TTL 的缓存，
合并策略是 `max(DB, 本地)`——宁可少发不可多发。含义：

- **重启不会把配额清零**（P0–P3 的进程内实现会）；
- `/dev/*` 与人工触发的发布同样占配额；
- 想知道某个账号现在还剩多少，看 `/stats` 的"今日配额"列，或直接查库：

```sql
-- 注意日界：限频按**账号本地日**切（下面的 ±8 小时是 Asia/Shanghai，换号要换偏移）。
-- 直接写 date('now') 是 UTC 日，本地 00:00–08:00 发的那几条会被漏掉
select count(*) from publish_records r
  join content_items i on i.id = r.content_item_id
 where i.account_id = 'xhs-demo-01'
   and r.phase = 'done'
   and r.updated_at >= datetime('now', '+8 hours', 'start of day', '-8 hours');
```

### 三个"今天"

系统里有三种"今天"，**看着重复，其实各有各的理由，谁也不许去统一它们**
（P11.3 修的就是"槽位按本地日分桶、计数按 UTC 日"这种半统一状态）：

| 计量 | 日界 | 在哪 | 为什么是这个日界 |
|---|---|---|---|
| **限频**：日上限 / `used_today` / `quota_left` | **账号本地日**（`extra.timezone`） | `core/ratelimit.py`（`db_usage`、`RateLimiter`）、`core/scheduling.py`、发布 tick、`/api/v1/accounts`、`/stats` | 它护的是"这个号在**它自己的作息**里一天发了几条"。发布窗口本来就按账号本地日排，计数不跟齐就会漏 |
| **成本闸门**：token / 渲染秒 / 生图张数 | **UTC 日** | `core/budget.py`（`BudgetGuard`、`today_key`）、`cost_ledger.day` | 这是**一台 core 服务**的开销，与账号无关。一台服务上可以同时跑 `Asia/Shanghai` 与 `America/Los_Angeles` 的号，按谁的本地日重置都说不通 |
| **手动出稿草稿计数**：`generate` 的 `used_today` / `cap` | **UTC 日** | `core/account_admin.py`（`generated_today`） | 它挡的是"人在工作台连点出稿按钮"——每点一次都真烧 token，和成本闸门是一家的，不是限频 |

具体到 `Asia/Shanghai`（UTC+8）：限频的日界在**本地 00:00**（= UTC 前一天 16:00），
成本闸门与草稿计数的日界在 **UTC 00:00**（= 本地早上 8 点）。所以"公众号一天只发一条"
从本地零点算，而"今天的 token 烧完了"是在本地早上 8 点回血——这不是 bug。

⚠️ 曾经的真实缺陷：公众号默认窗口 `07:00-09:00` 正好横跨本地 08:00 这条 UTC 午夜缝，
限频却按 UTC 日计数，于是本地 07:30 与 08:30 各发一条会被算成两天，`daily_limit: 1`
**完全不生效**。回归用例钉在 `tests/test_scheduling.py` 与 `tests/test_scheduler_p4.py`
的"UTC 午夜缝"两段，改限频前先跑它们。

`tick_generate` 的 `daily_target`（每天出几条稿）也按账号本地日算
（`core/scheduler.py:local_day_start`），与限频同侧——它同样是"人的一天"。

## 1.6 调度与定时任务

`core/scheduler.py` 注册九个 job。**手动触发与定时跑的是同一批函数**
（`core.scheduler.TICKS`），所以 curl 出来的行为就是生产行为。下表按
`core.scheduler.TICKS` 字典的实际顺序排列（`create_scheduler` 的注册顺序与此一致）。

| tick | 频率（可配） | 做什么 |
|---|---|---|
| `sourcing` | `SW_SOURCING_INTERVAL_HOURS`=6 | 拉 newsnow / douyin-hot-hub / TrendRadar → 去重入库 |
| `generate` | `SW_GENERATE_INTERVAL_MINUTES`=30 | 按 `daily_target` 选题 → 生成 → 机器审核 → 进人工队列 |
| `scheduled_publish` | 1 分钟（固定） | 发到期的 `scheduled` 项，六道闸门见下 |
| `confirm_gate` | 1 分钟（固定） | 发布前人工确认闸门巡检（P12）：推确认卡 / 槽位前补提醒（`SW_CONFIRM_REMIND_MINUTES`=30 分钟）/ 到点未确认按账号 `confirm_ttl_hours`（默认 24，见 `SW_CONFIRM_TTL_HOURS`）自动驳回，详见 7.6.4 |
| `retry_sweep` | `SW_RETRY_SWEEP_INTERVAL_MINUTES`=5 | `retrying` 项按指数退避重投；超龄进死信并告警 |
| `metrics` | 6 小时（固定） | 24h / 7d 指标快照（只追加） |
| `login_health` | `XHS_LOGIN_HEALTH_INTERVAL_MINUTES`=10 | 登录态巡检（抖音内部另有 30 分钟节流） |
| `render_jobs` | 1 分钟（固定） | 轮询 MPT 渲染任务，成片补挂回内容包 |
| `insights` | 6 小时（固定） | 复盘 Agent（每账号内部按 `INSIGHTS_INTERVAL_HOURS`=24 小时节流） |

### 手动触发

```bash
curl -s localhost:8000/dev/tick                     # 列出全部 tick
curl -sX POST localhost:8000/dev/tick/sourcing
curl -sX POST localhost:8000/dev/tick/generate
curl -sX POST 'localhost:8000/dev/tick/generate?account_id=xhs-demo-01'
curl -sX POST 'localhost:8000/dev/tick/generate?platform=xhs'
curl -sX POST localhost:8000/dev/tick/scheduled_publish
curl -sX POST localhost:8000/dev/tick/confirm_gate
curl -sX POST localhost:8000/dev/tick/retry_sweep
curl -sX POST 'localhost:8000/dev/tick/metrics?respect_windows=false'   # 默认 true（生产口径），想立刻采一张才传 false
curl -sX POST 'localhost:8000/dev/tick/login_health?platform=douyin&force=true'
curl -sX POST localhost:8000/dev/tick/render_jobs
curl -sX POST 'localhost:8000/dev/tick/insights?force=true'
```

返回体里的 `stats` 就是 job 的统计字典。

`metrics` 的 `max_items` 限制成功 claim 并开始处理的**内容数**，不是 HTTP 请求数；一条内容
可能执行主 fetch、标题 fallback 和 health。每条内容先用独立短事务原子 claim 当前 UTC 六小时
桶，提交后才调用 publisher；并发 tick 争到同一内容时只有胜者调用，败者继续寻找其它候选。
claim 只允许数据库中桶号严格小于目标桶号时升级；同桶、旧桶和乱序手动 tick 都不能倒退或重复
调用。网络期间不持有 DB 事务，每项结果独立提交，所以后项 DB 失败不会回滚前项。

该 claim 是 **at-most-once per bucket** 租约：claim 后进程崩溃会令本桶跳过该内容，下一桶窗口
仍欠账时才重新进入公平队列，且不保证紧邻下一 tick 一定选中。结果事务也会用
`item_id + bucket + claimed` 做首尾 CAS；迟到的旧桶结果或重复结果不会写快照、post id、状态或
health。SQLite 锁等待遵从数据库 URL 的连接 `timeout`；超时会作为脱敏数据库失败停止该 tick，
不会在响应或日志中回显 SQL 参数。需要排障时查看 `metric_collection_attempts.last_outcome`：
`claimed|success|unavailable|malformed|error`。

publisher 返回值必须是严格可由 stdlib JSON 编码的 Mapping；set、对象、循环引用、NaN 和
Infinity 都按 `malformed` 记录 attempt，不写 payload、不推进内容、不回填 post id。默认每 tick
最多成功 claim 50 条，可用 `max_items` 显式缩小；真实平台的并发与限流参数仍未验证。

也可以直接在 Python 里跑：

```bash
uv run python -c "from core.scheduler import run_tick; print(run_tick('scheduled_publish'))"
```

### `tick_scheduled_publish` 的六道闸门

六道 = 循环里六个"这条这一轮不发"的分支，顺序不能换（越便宜的越先判）。每道在
`stats` 里各有一个计数、且都计入总 `skipped`，排查时先看是哪一道拦的。

| 闸门 | 统计键 | 触发条件 |
|---|---|---|
| 账号健康 | `skipped_account` | 账号不是 `ok`（`needs_relogin` / `banned` / `degraded`）|
| 发布时段 | `skipped_window` | 当前不在 `publish_windows` 内 |
| 限频 | `skipped_rate` | 日上限用完，或距上次发布不足最小间隔 |
| **人工确认** | `skipped_unconfirmed` | 账号 `confirm_required`（**默认开启**）而 `confirmed_at` 为空。拦它的是本 tick 自己，不是 `tick_confirm_gate`——后者只推确认卡 / 槽位前提醒 / 到点按 `confirm_ttl_hours` 自动驳回（见 1.6 的 `confirm_gate` 行与 7.6.4）。没人点就一直不发，直到 TTL 把它驳回 |
| 发布器 | `skipped_publisher` | 该平台的发布器没注册（`PublisherNotAvailable`）|
| 状态未推进 | `skipped_not_advanced` | `publish_with_idempotency` 没抛异常，但状态也没推进到 `published`。**防御性守卫，两条成因当前都不可达**：dry-run 发布器（工厂都不传 `dry_run`）；publisher 违反 `publishers/base.py` 的 publish 契约返回 `ok=False`（现有发布器的 `ok=False` 全在各自 dry-run 分支里，而 dry-run 在 `publish_with_idempotency` 里提前 return，到不了那道兜底）。**正常恒为 0**；非零说明有发布器破了契约。2026-08-23 起第一现场还会有一条 `[契约违规] <item_id>` 的 error 级通知 + 一条 `social_workflow.state_machine` 的 warning（措辞点明这是发布器把契约改坏了、不是一次普通发布失败，并带发布器类名），再配合 `publish_records.last_error` 与日志里的 `status=` 定位。见 `docs/RISKS.md` 第 13 条 |

> `scanned == published + skipped + failed` **恒成立**（六道全部计入 `skipped`），
> 对不上就是有 bug。发布器抛 `PublishError` 不算闸门，是失败，计 `failed` 并走重试（见下节）。
>
> 全是 `skipped_window` 说明窗口配窄了或时区配错了；全是 `skipped_account` 先去
> `/accounts` 看谁红了；全是 `skipped_unconfirmed` 是**预期行为**，去催人点确认。
>
> **别被"第五道"这个叫法带偏**：`core/confirm.py:19`/`:322` 把人工确认称作"第五道闸门"
> （`core/models.py:189-190` 的字段注释里也提了一句"代码里若干处沿用的'第五道'"），指的是
> 它是第 5 个**被加进来**的闸门（P12，见 `docs/briefs/p12_brief_autopilot_telegram.md`：
> 当时已有的四道是账号健康 / 窗口 / 限频 / **发布器**），**不是执行顺序里的位次**。执行
> 顺序里它排第 4——插在限频之后、发布器之前，`core/scheduler.py` 内联注释的 `# ④` 才是
> 位次。`core/confirm.py` 是红线 R4 冻结件，措辞不动；`tests/ops/test_verify.sh` 本轮已经
> 改口叫「人工确认闸门」，不再用"第五道"这个叫法。碰到这个叫法时按本表的位次读。

### 重试与死信

- 退避：`SW_RETRY_BACKOFF_BASE_SECONDS * 2^(attempts-1)`，封顶
  `SW_RETRY_BACKOFF_MAX_SECONDS`（默认 300s → 4h）。从**最后一次尝试**算起。
- `attempts >= SW_MAX_PUBLISH_ATTEMPTS`（默认 3）→ `dead_letter` + 通知。
- **`NeedsReloginError` 不计 attempts**（等人续期不该被重试次数烧掉），
  代价是没人处理时它会永远 `retrying`。出口是
  `SW_RETRY_MAX_AGE_HOURS`（默认 48h）：超龄直接进死信并告警。
- `retry_sweep` 会**按账号健康过滤**：掉线的号一次都不会再调发布器。
  （这修的是 P3 的一个洞：`mark_account_needs_relogin` 只挂起 `scheduled` 的项，
  `retrying` 的那条不在挂起范围内。）

### 调度器在哪跑

默认**随 core 进程一起启动**（`SW_SCHEDULER_ENABLED=true`），所以
`docker compose up -d core` 就是完整的"无人值守（除审核点击）"形态。
启动日志里会打印注册了哪些 job：

```
INFO social_workflow.api 调度器已启动: ['tick_sourcing', 'tick_generate', ...]
```

想把控制面和调度分开部署（或者只想临时让定时任务跑起来）：

```bash
SW_SCHEDULER_ENABLED=false uv run uvicorn core.main:app --port 8000   # 只有 UI
uv run python -m core.scheduler                                       # 只有调度
uv run python -m core.scheduler --list                                # 看有哪些 tick
uv run python -m core.scheduler --once scheduled_publish              # 跑一次就退出
```

**多进程部署时只许有一个调度器实例**：APScheduler 的 `max_instances=1` 只在进程内生效，
两个进程会各跑一套 job。限频与幂等能兜住重复发布，但会白白多打一倍 HTTP。

## 2. 登录续期

### 小红书（P2，已落地）

**巡检**：`core.scheduler.tick_login_health` 每 **10 分钟**跑一次
（`XHS_LOGIN_HEALTH_INTERVAL_MINUTES`），实现在 `publishers/xhs/login.py`。
它对每个账号调 `Publisher.health()` → `GET /api/v1/login/status`，再经
`core.state_machine.apply_health` 落到 Account 状态机。

| health 结果 | 触发条件 | 系统动作 |
|---|---|---|
| `ok` | `is_logged_in=true` | 账号回 `ok`；若原先是 `needs_relogin`，**自动放回**挂起的排期项 |
| `needs_relogin` | `is_logged_in=false`，或发布/接口回文含"未登录/cookie" | 账号 → `needs_relogin`；该账号所有 `scheduled` → `suspended`（记 `prev_status`）；发通知 |
| `degraded` | sidecar 连不上 / 5xx / 浏览器抖动 | **只改账号状态，不挂起排期**（误判成掉线会白白催人扫码） |
| — | 账号已是 `banned` | 巡检直接跳过（人工终态） |

**续期步骤**（预计数周一次）：

1. 收到 `[需重登]` 通知，或在 `/accounts` 看到账号变红；
2. 打开 `/accounts/{id}/login`，用**该账号本人**的小红书 App 扫码
   （App → 右上角「+」→ 扫一扫）；
3. 页面每 3 秒轮询 `/accounts/{id}/login/status`，扫上就变 `ok`，挂起项自动放回；
4. 不放心可跑一次 `uv run python -c "from core.scheduler import tick_login_health as t; print(t())"`。

**必须知道的几件事**：

- 页面**不会**每 3 秒重取二维码。上游每取一次二维码就新开一个浏览器会话并**关掉上一个
  正在等扫码的会话**，频繁重取会导致永远扫不上。二维码只在自身过期（默认 4 分钟）后重取。
  同理：**别同时开多个登录页**。
- 同一账号**不允许多网页端同时登录**。你在浏览器里另开小红书网页版，就会把 sidecar
  顶下线，下一轮巡检立刻变 `needs_relogin`。
- 一账号 = 一 sidecar 容器 + 一 volume + 一端口，**禁止共享 `/app/data`**
  （共享 = Cookie 池，见 `docs/POLICY.md`）。生成脚本会在两个账号共用 volume 时直接报错。
- sidecar 的 `401 UNAUTHORIZED` 是 `AUTH_TOKEN` 与 `XHS_MCP_TOKENS` 不一致，**不是**掉线，
  发布器会按 `PermanentError` 处理，不会误挂账号。
- 备份 `xhs_data_*` volume 就是备份登录态；丢了要重新扫码。

容器编排、首次登录、镜像升级见 `sidecars/xhs/README.md`。

### 小红书发布的两个特有现象

**1. 笔记 id 是"占位值"（`xhs-unresolved-*` / `xhs-scheduled-*`）**

上游 `POST /api/v1/publish` 的响应里没有笔记 id，我们靠发布后扫主页对账拿。
拿不到时仍记 `done` + 占位 id——因为小红书没有幂等接口，重试就是真的再发一篇。

- 影响：该条内容暂时没有链接、指标拿不到；
- 自愈：下一次指标采集（`tick_metrics`）会用 `fetch_metrics_for_title` 按标题兜底解析，
  命中后把真实 id 回填进 `publish_records`（**不会**改 `updated_at`，24h/7d 窗口不受影响）；
- 定时发布必然先记 `xhs-scheduled-*`（笔记还在"待发布"，主页上看不到），
  等它真上线后同样由指标链路补回。

**2. 限频记两处但只算一次**

调度器与发布器各挡一道限频（`/dev/*` 与人工触发不经过调度器），
两边用 `token=<内容项 id>` 去重。看 `/stats` 时若发现日计数比实际发布数多，
说明某条路径没传 token——查 `RateLimiter.record` 的调用点。

### 抖音（P3，已落地）

上传器是**宿主机上的有头浏览器常驻进程**，不在 docker-compose 里，也永远不会加进去
（容器没有图形界面，抖音当场识破）。完整手册见 `publishers/douyin/README.md`，
这里只列运维要点。

**每天开工前**（宿主机上）：

```bash
uv sync --extra douyin                              # 只在宿主机装浏览器层
uv run python -m publishers.douyin serve --port 8710
curl -s http://127.0.0.1:8710/health | python3 -m json.tool   # headless 必须是 false
```

core 在容器里时 `DOUYIN_SERVICE_URL=http://host.docker.internal:8710`（compose 已默认）。

**巡检**：`tick_login_health` 里有一条**抖音专属节流分支**——它每次巡检都会真的开一个
有头浏览器去看创作者中心，比小红书的一次 HTTP 贵一个量级，所以按
`DOUYIN_LOGIN_HEALTH_INTERVAL_MINUTES`（默认 **30 分钟**）单独限速，
没到点就整体跳过 douyin，统计里出现 `douyin_throttled: 1`。
要强制巡一次：

```bash
uv run python -c "from core.scheduler import tick_login_health as t; print(t(platforms=['douyin']))"
```

| health 结果 | 触发条件 | 系统动作 |
|---|---|---|
| `ok` | 已登录且页面昵称与 `identity_hint` 一致 | 正常；原先 `needs_relogin` 的会自动放回排期 |
| `needs_relogin` | 未登录 / 卡在短信验证 / 卡在图形验证 | 挂起该账号排期 + 通知去 `/accounts/{id}/login` |
| `degraded` | 上传器进程不可达；或 identity 不符；或没配 `identity_hint` | **只改账号状态，不挂起排期** |

`identity` 不符**刻意只报 `degraded` 而不是 `banned`**：`banned` 是人工终态、状态机
不允许自动恢复，靠一次昵称读取就把账号钉死代价太大。真正的硬闸门在发布前
（不符 → `PermanentError` → 直接进死信，不会发出去）。

**续期步骤**：

1. 收到 `[需重登]` 通知，或 `/accounts` 里看到账号变红；
2. 打开 `/accounts/{id}/login` → 点「打开宿主机登录窗口」；
3. **去宿主机那台机器前面**，用该账号本人的抖音 App 扫弹出窗口里的码；
4. 要短信验证码就在同一页面的输入框里填（core 会同时进队列 + 转发给上传器填入窗口）；
5. 页面每 5 秒轮询一次，登录成功后账号自动回 `ok`，挂起项自动放回。

**必须知道的几件事**：

- 二维码**不经过 core**。抖音的登录页在宿主机浏览器窗口里，页面上没有二维码图片，
  也刻意不给占位图（给了人会对着扫半天）。
- **图形/滑块验证只能人自己在宿主机窗口里拖**。系统检测到就暂停上报，不碰它。
- `profile_dir` 一号一目录（`profiles/douyin/<account_id>/`），**禁止跨账号复制**
  （那就是 Cookie 池）。备份 `profiles/` 就是备份登录态。
- 限频三道：日 ≤ 2（硬顶 10）、两次发布间隔 ≥ 30 分钟、同时只允许 1 个浏览器作业。

### 抖音发布的三个特有现象

**1. 作品 id 是"占位值"（`douyin-unresolved-*` / `douyin-scheduled-*`）**

和小红书同因同解：抖音没有幂等发布接口，发布后要从内容管理页把 `aweme_id` 找回来，
找不到就记占位 id 并落 `done`——**绝不重发**。下一次 `tick_metrics` 会用
`fetch_metrics_for_title` 按标题兜底解析并回填真 id（不改 `updated_at`）。

**2. `state=timeout` 不等于没发出去**

点了发布但没等到跳转内容管理页，上传器回 `timeout`（可重试）。
**不要手动重发**：core 重试前必走 `reconcile`（内容管理页最近 20 条，标题 + 24h 时间窗），
命中就直接补记录，不会再点一次发布。

**3. 选择器随时会失效**

`publishers/douyin/service.py:SELECTORS` 的 CSS 依据 2026-08 页面观察写成，
**未在真实站点验证**。失配时上传器会回 `browser_error` 并截图到
`data/douyin/screenshots/<account>/<时间戳>-<步骤>.png`；照着截图改，
用 `DOUYIN_SELECTORS_FILE` 指一个 JSON 覆盖表即可，**不用改代码、不用重新发版**。

### 公众号（P1）

- 无扫码流程，凭 AppID/AppSecret 走官方 API；`health()` **永不返回** `needs_relogin`。
- `stable_token` 有效期 7200s，**必须缓存复用**，不要每次调用都换新 token（会互相顶掉）。
- 自动发布要同时满足三道闸门（缺一只落草稿箱）：

  | 闸门 | 配置项 | 谁改 |
  |---|---|---|
  | 服务端开关 | `WECHAT_AUTO_PUBLISH=true` | 生产上走 `bash scripts/ops/env_set.sh --key WECHAT_AUTO_PUBLISH --value true`（备份 → 原子写入 → **重建容器**，见 6.7.1）。它有事前闸门：`WECHAT_CERTIFIED` 不是 `true` 时**拒绝**——那样改是个不会生效的空操作。`WECHAT_CERTIFIED` 现在也在白名单里，走 `--key WECHAT_CERTIFIED --value true`（**先记事实，再开开关**，反过来会被上面那道闸门拦住） |
  | 账号认证 | `WECHAT_CERTIFIED=true` | 运维走 `bash scripts/ops/env_set.sh --key WECHAT_CERTIFIED --value true`。**必须与公众号实际认证状态一致**——这个值记的是微信那边的事实，工具面核实不了，写错就在 `freepublish` 报 48001 |
  | 逐条人工确认 | 审核 UI 批准时写入 `platform_extra.confirm_publish=True` | 审核人，**每条内容各写一次** |

  想临时"全停自动发布"，把 `WECHAT_AUTO_PUBLISH` 改回 `false` 再让新值生效即可（同上：
  本地重启进程，Compose 生产要重建容器），已批准的内容会退化为只落草稿箱，不会丢。
  生产上这个键**现在有工具面**（`bash scripts/ops/env_set.sh --key WECHAT_AUTO_PUBLISH
  --value false`——回到安全方向，不设闸门）。作用域更大的那条急停是
  `bash scripts/ops/env_set.sh --key SW_USE_FAKE_PUBLISHERS --value true`——但那是**三个平台
  一起**停掉真发布，不只是公众号的自动发布，范围大得多，别当成等价替代。
- 未认证号：内容到草稿箱后仍需人到「公众号后台 → 草稿箱」点「发表」。

## 3. IP 白名单（errcode 40164）

公众号 API 要求调用方出口 IP 在后台白名单内。

**排查**：`uv run python scripts/preflight.py` 的"公众号 IP 白名单(40164)"行会直接给出结论。

**三种解法**：

| 方案 | 做法 | 适用 |
|---|---|---|
| 固定出口 IP | 部署到有固定公网 IP 的机器，把 IP 加进公众号后台"IP 白名单" | 生产 |
| 中转代理 | 部署 wenyan Server（`WENYAN_SERVER_URL`），由它统一出网 | 本机开发 / 动态 IP |
| 只用草稿箱 | 未认证号本来就只能落草稿，但**草稿接口同样受 IP 白名单约束** | 不能规避，仍需上面二选一 |

查当前出口 IP：`curl -s https://api.ipify.org`。家宽 IP 会变，改了要回后台同步。

### 3.1 40164 的现场特征

发布器把 40164 映射成 `PermanentError`，异常 detail 里**直接带上平台回报的出口 IP**
（从 `errmsg` 的 `invalid ip <IP>, not in whitelist` 解析），所以不用另外去查：

```
PermanentError: IP not in whitelist: 当前出口 IP 203.0.113.42 不在公众号后台白名单内（errcode=40164）。
请到「公众号后台 → 开发 → 基本配置 → IP 白名单」添加；本机出口 IP 可用 `curl -s https://api.ipify.org` 查询；
动态 IP 环境请改走 WENYAN_SERVER_URL 中转。详见 docs/OPS.md 第 3 节。
```

- `health()` 会把它映射成账号 `degraded`（**不是** `needs_relogin`——公众号没有登录态，
  误判会白白挂起该账号的全部排期项）。
- 它是 `PermanentError`，**不会重试**：白名单是配置问题，重试只会烧掉发布次数。
- 平台侧回报的 IP 可能是 IPv6 映射形式（`::ffff:1.2.3.4`），后台要按它实际显示的那个加。
- 白名单最多 200 条，公网出口是 IP 段的话逐个加；每次改动约 5 分钟生效。

### 3.2 公众号相关的其它高频错误码

| errcode | 含义 | 本项目行为 | 处置 |
|---|---|---|---|
| 40001 / 42001 | access_token 失效 / 过期 | 自动刷新并**重试一次** | 无需人工 |
| 40013 / 40125 | AppID / AppSecret 无效 | `PermanentError`，`health()` → `degraded` | 后台重置 secret 后更新 `.env` |
| 45009 / 45011 | 达到日调用上限 / 频率过高 | `RetryableError` | 等下一轮 tick；持续出现要降调度频率 |
| 48001 | 无接口权限 | `PermanentError` | 未认证号调了 freepublish，把 `WECHAT_AUTO_PUBLISH` 关掉 |
| 53503/53504/53505 | 草稿未通过发布校验 | `PermanentError` | 到后台手工检查该草稿 |
| 61500 / 61501 | datacube 日期格式 / 范围错 | 客户端**在发请求前**就拦下来 | 检查 `begin_date`/`end_date` 跨度 |

### 3.3 access_token 的运维注意

- `stable_token` 有效期 7200s，进程内缓存（`publishers/wechat_mp/client.py` 的
  `TOKEN_CACHE`，提前 300s 刷新），**不要**每次调用都换新。
- `force_refresh` 官方限制每日 20 次、两次间隔 ≥30s；客户端内置了 30s 保护，
  间隔不足时自动退化为普通获取。
- 多进程部署时每个进程有各自的缓存，日调用量会翻倍——单机 MVP 无碍，扩容前要换共享缓存。

## 3.5 真实发布的回滚 / 撤稿手册

内容一旦真的发到平台上，"撤稿"目前**全是人工动作**——三个平台里没有一个被本仓库
封装了"删除已发布内容"的接口（详见 3.5.2 每个平台的代码依据）。这份手册按"发出去了
没有"分两段，再按平台展开；末尾单独列"根本撤不回来"的情况和"本地状态怎么收敛"，
后者是最容易被忽略但最容易出乱子的一步。

**先分清两件事**：还没真正发出去，走 3.5.1 的现成路径；已经真正发出去了，没有按钮，
走 3.5.2 的人工步骤。中间状态判断不清时，先用 `content_get(item_id)` /
`jobs_query("publish_records")` 看 `PublishRecord.phase` 是不是 `done`——`done` 就是
已经交付，不管平台侧最终是否真上线（占位 id 的情形见 2 节"两个特有现象"）。

### 3.5.1 还没真正发出去：现成路径（优先用这条）

- **适用范围很窄**：只覆盖"已排期、正在等人工确认发布"这一段，即
  `ContentItem.status == scheduled` 且 `confirmed_at` 还是空（工作台 / `content_list`
  里 `awaiting_confirm=true`）。`core/confirm.py:66` 把这个范围定义为
  `CONFIRMABLE_STATUSES = (ContentStatus.SCHEDULED,)`，`_assert_decidable`
  （`core/confirm.py:307-312`）对状态不在这个集合里或已经 `confirmed_at` 不为空的
  内容一律拒绝（`ConfirmConflict`）。
- **工具**：`content_reject(item_id, reason, operator)`（`scripts/workbench_mcp.py:761`）
  → `POST /content/{item_id}/reject`（`core/api/content.py:309`）→
  `core.confirm.reject_confirmation`（`core/confirm.py:339`）。内部**逐跳走状态机**
  `scheduled → approved → draft → reviewing → rejected`，落地状态是 `rejected`，
  同时把 `scheduled_at` 清空让出排期槽位，理由写回 `review_notes`。
- 前提 - 动作 - 验证 - 失败：
  - 前提：`content_list(status="scheduled")` 能看到该条，`awaiting_confirm=true`，
    `confirm_deadline` 还没过。
  - 动作：`content_reject(item_id, reason="人工撤回，原因……")`。
  - 验证：调用返回的 `message`；再 `content_get(item_id)` 确认 `status=rejected`、
    `scheduled_at=null`；`content_list(status="scheduled")` 里这条应该消失。
  - 失败：状态已经越过 `scheduled`（进了 `publishing`/`published` 等）或
    `confirmed_at` 已经写入 → 后端返回 409 `confirm_conflict`，说明来晚了，改走 3.5.2。
- **`confirmed_at` 写入之后、调度器真正调用 `publisher.publish()` 之前**有一段
  没有工具能拦的窗口，这段时间内**没有取消手段**，只能等结果出来后走 3.5.2。
  窗口的**调度延迟上界是可知的、且很短**，不是"未知时长"：
  - `tick_scheduled_publish` 是 1 分钟固定周期，硬编码在
    `core/scheduler.py:994`（`add("scheduled_publish", minutes=1)`），不经过任何
    `SW_*` 配置项——所以从人点确认到**下一轮 tick 开始扫描**这条，最多等 1 分钟，
    这是调度层面的硬上界。
  - 但"1 分钟"只是**轮询延迟的上界，不等于"1 分钟内一定发完"**：
    `tick_scheduled_publish` 在同一轮里对到期项是**串行处理**——`core/scheduler.py`
    第 454 行起是一个普通 `for item in session.scalars(stmt):` 循环，单个数据库
    session、单线程，逐条调用 `publish_with_idempotency`，全文件没有
    `ThreadPoolExecutor` / `asyncio` / 任何并发派发（已确认 grep 不到）；
    查询按 `scheduled_at` 升序取，单轮最多处理 `sw_publish_batch_size` 条
    （默认 20，`core/config.py:77`）。也就是说，这条内容真正被调用
    `publisher.publish()` 的时刻 = 排在它之前进入这一批、且同样到期待发的项
    （账号健康/时段/限频/确认闸门未拦下的）逐条处理完的时间，**加上**这些项各自
    发布器的执行耗时——三个平台的发布器耗时数量级不同：公众号/小红书是几次
    HTTP 调用（通常秒级），抖音走有头浏览器自动化外加可能的短信验证等待，
    耗时明显更长（数十秒到分钟级，无法给出精确数字，见 2 节"抖音专属"的巡检成本
    描述）。批量积压严重或前面排到一条抖音发布时，这条内容的实际发出时刻可能
    明显晚于"确认后 1 分钟"，但仍然是**分钟级**，不是无限期悬空。
  - 真正**未查到**、需要真实环境验证的只有一件事：生产环境典型的"同一轮到期队列
    积压量级"（即确认之后平均会排在几条内容后面），这个数字没有实测数据，见
    §9 待办"多账号并发调度的限频参数实测值"。

### 3.5.2 已经真正发出去：分平台

#### 公众号（`publishers/wechat_mp/`）

本地留痕：`PublishRecord.platform_post_id` = 草稿 `media_id`（认证号发布成功后**仍然是
这个值**，见 `publishers/wechat_mp/publisher.py:299`/`310` 两处 `platform_post_id=media_id`）；
`url` 只有认证号走完 `freepublish` 才有值，取自 `_first_article_url`
（`publishers/wechat_mp/publisher.py:355`）；`raw` 里另外记了 `publish_id`
（`freepublish/submit` 返回）与 `article_id`（`freepublish/get` 返回）。

- **认证号（已跑完 `freepublish/submit`，真正群发出去）**
  - 官方对应接口是 `cgi-bin/freepublish/delete`（公开文档，非本仓库自测）：
    `POST` 参数 `article_id`（必填）+ `index`（可选，不传删除整组图文，传了只删组内
    某一篇）。**本项目 `publishers/wechat_mp/client.py` 没有封装它**——该文件里所有
    `freepublish_*` 方法只有 `freepublish_submit`（605 行）、`freepublish_get`
    （623 行）、`freepublish_batchget`（629 行）、`freepublish_getarticle`
    （640 行），逐一确认过，不存在 `freepublish_delete`。
  - 因此撤稿只能人工操作：登录公众号后台 → 图文消息（已群发）→ 找到该篇 →
    删除。`article_id` 可以从 `PublishRecord.raw.article_id` 或
    `jobs_query("publish_records")` 里取，仅供人工核对是哪一篇，**本仓库不提供任何
    代码路径去调用删除接口**。
  - 前提 - 动作 - 验证 - 失败：
    - 前提：`content_get(item_id)` 确认 `status=published`，平台是认证号
      （`certified=true`，见 1.5 节账号策略）。
    - 动作：人工登录公众号后台完成删除。
    - 验证：`freepublish_get(publish_id)`（工程师手动跑，或等下一次
      `reconcile()`/`fetch_metrics` 触发）应能看到 `publish_status` 变为 5
      （成功后用户删除所有文章）；参见 `PUBLISH_STATUS_TEXT`
      （`publishers/wechat_mp/publisher.py`）。
    - 失败：后台找不到该篇——先核对 `article_id`/标题是否对应，警惕同标题多次投稿
      的情形（`reconcile()` 用标题+摘要双字段匹配，见 `publisher.py` 的 `_scan`）。
  - **删除不等于收回**：已经被读者转发/收藏/截图的内容不会因为后台删除而消失，见
    3.5.4。
- **未认证号（停在草稿箱：`platform_post_id` 是草稿 `media_id`，`url=None`，
  `raw.stage="draft"`）**
  - 人还没去后台点"发表"：直接去公众号后台 → 草稿箱 → 删除该草稿即可，**不用**动
    本地任何记录——`PublishRecord.phase` 已经是 `done`（系统认为这是"已交付到草稿箱"
    这个既定动作，不代表已经群发），删草稿不影响 `ContentItem.status`（已是
    `published`，见 `publishers/wechat_mp/__init__.py:10` 的前提说明）。
  - 人已经在后台点了"发表"：这条内容就从"系统看不见的草稿"变成了"系统看不见的
    已发布"——`health()`/`reconcile()` 都不扫描未认证号在后台的手动发表历史
    （`reconcile()` 只在 `self.auto_publish and self.certified` 时才去扫
    `freepublish/batchget`，见 `publisher.py` 的 `reconcile`），只能去后台按认证号的
    路径手工删，本系统全程不知情、也不会补记录。
  - 48001（无接口权限，见 §3.2）本身就是"未认证号硬调 freepublish"的产物：撤稿时如果
    误对未认证号走 freepublish 系列接口会先撞见这个不可重试错误，再次印证未认证号唯一
    合规路径是后台手工操作。

#### 小红书（`publishers/xhs/`，走 sidecar）

本地留痕：`PublishRecord.platform_post_id` = 笔记 `note_id`，解析不出来时是占位值
`xhs-unresolved-*` / `xhs-scheduled-*`（见 2 节"小红书发布的两个特有现象"）；
`url` 在拿到真实 `note_id` 时会写成 `note_url(note_id, xsec_token)`
（`publishers/xhs/publisher.py:355`/`454`，模板见 `client.py:249`
`https://www.xiaohongshu.com/explore/{note_id}?xsec_token=...`），占位 id 阶段
`url=None`。

- **平台侧删除**：`publishers/xhs/client.py`（`XhsMcpClient`）封装的接口只有
  `health` / `login_status` / `get_login_qrcode` / `my_profile` / `my_notes` /
  `user_profile` / `note_detail` / `resolve_images` / `resolve_video` /
  `publish_content` / `publish_video` / `mcp_call`——**没有任何删除笔记的接口**。
  `sidecars/xhs/README.md:135` 提到的 `DELETE /api/v1/login/cookies`
  只是重置登录态，与删内容无关，且"本项目代码不调它"。
  - **结论：小红书撤稿只能人工在 App 或网页版操作**（笔记详情页 → 更多 → 删除）。
    上游 sidecar（`xiaohongshu-mcp`）本身是否有更完整的删除相关能力
    **未查到，需要在有真实 sidecar 实例时翻它自己的 API 文档 / 源码确认**——本项目
    `client.py` 没有封装不代表上游一定没有，只能确认"本仓库没有这条路径"。
  - 前提 - 动作 - 验证 - 失败：
    - 前提：`content_get(item_id)` 拿到 `platform_post_id`；若是占位值先看
      `url` 是否为空——为空说明发布时没解析出 id，人工去 App 自己的"笔记管理"里
      按标题找，不能直接拼链接打开。
    - 动作：人工在 App/网页版删除该笔记。
    - 验证：`account_sidecar`/`accounts_list` 无法验证内容删除与否（那是账号健康，
      不是内容状态）；只能靠人工在 App 里确认笔记已消失，或等下一次
      `tick_metrics` 触发 `fetch_metrics` 后看 `available=False` 且原因是"主页最近
      N 条里没有该 note_id"（`publishers/xhs/publisher.py:568-574`）间接印证——
      **但这不是即时验证，隔一个采集周期才会体现**。
    - 失败：占位 id 阶段无法区分"平台还没上线"还是"已上线只是没对账上"，见下条。
- **定时笔记**（`xhs-scheduled-*`）：如果笔记还在小红书自己的"定时发布"队列里、还没
  真正上线，需要在 App 侧的"草稿/定时"箱里取消，而不是等它上线后再删——这个窗口
  本系统看不见，`platform_post_id` 停在占位值期间无法分辨究竟是哪种情况。

#### 抖音（`publishers/douyin/`，走 Patchright 有头浏览器）

本地留痕：`PublishRecord.platform_post_id` = `aweme_id`，解析不出来是占位值
`douyin-unresolved-*` / `douyin-scheduled-*`（见 2 节"抖音发布的三个特有现象"）；
`url` 取 `outcome.url` 或退化为 `video_url(post_id)`
（`publishers/douyin/publisher.py:354`/`515`，模板函数见 `client.py:192`），
占位 id 阶段 `url=None`。

- **平台侧删除**：`publishers/douyin/client.py`（`DouyinServiceClient`）封装的接口只有
  `health` / `login_status` / `start_login` / `submit_sms_code` / `publish` /
  `recent_posts` / `metrics`——**没有删除作品的接口**；`service.py` 的浏览器自动化
  动作与 `SELECTORS` 里也没有对应步骤（对 `publishers/douyin/` 全目录搜索
  `delete|删除|下架|撤` 无命中）。
  - **结论：抖音撤稿只能人工操作**——去宿主机那台常驻有头浏览器窗口（或抖音 App）：
    创作者中心 → 内容管理 → 找到该作品 → 删除。
  - 前提 - 动作 - 验证 - 失败：
    - 前提：先确认这条**真的发出去了**，不要被 `state=timeout` 误导——按 2 节
      "抖音发布的三个特有现象"第 2 条，`timeout` 不等于没发出去，重试前 core 会先走
      `reconcile()`（内容管理页最近 20 条核对），人工要撤稿前同样应该先去内容管理页
      核实这条是否存在，而不是凭 `jobs_query` 里的 `last_error` 字面意思判断。
    - 动作：宿主机人工在创作者中心删除该作品。
    - 验证：人工在内容管理页确认作品已消失；或等下一次 `tick_metrics` 调
      `GET /accounts/{id}/metrics/{post_id}` 后 `available=False`
      （`publishers/douyin/service.py:1406` 起的 `metrics` 动作"读不到就
      `available=false`"）间接印证，同样有采集周期延迟。
    - 失败：如果作品还处于刚发布的审核期就去删，是否会触发额外的平台侧风控标记
      **未查到**，建议先等审核状态明确（内容管理页会显示"审核中/已发布"）再决定要不
      要删。

### 3.5.3 删完之后，本地状态怎么收敛

这是最容易被忽略却最容易出乱子的一步。**系统里没有"已撤稿/已下架"这个状态**——
`core/state_machine.py` 的 `CONTENT_TRANSITIONS`（116 行起）里，`PUBLISHED` 只能到
`MEASURED`，`MEASURED` 只能到自身，两者都到不了 `DEAD_LETTER` 或任何"作废"态；
`content_reject` 只认 `scheduled`（3.5.1），`content_retry_now` 只认
`retrying`/`publish_failed`/`dead_letter`（`core/api/content.py:330` 的
`retry_now`），同样覆盖不到 `published`/`measured`。也就是说，**平台侧删掉之后，
本地没有任何一个现成工具能把这条内容标记成"已撤稿"**。

会不会因此产生噪音或错误，取决于指标采集当时的进度（`metrics/collector.py`）：

1. **24h、7d 两个窗口都已经拿到过至少一次可用快照**（`ContentItem.status` 已经是
   `measured`）：删稿后不会再触发任何新的采集——候选集合虽然仍会扫到 `measured`
   状态的内容（`MEASURABLE_STATUSES = (PUBLISHED, MEASURED)`，
   `metrics/collector.py:65`），但 `due_window`（`metrics/collector.py:218`）判定两个
   窗口都已覆盖后就不会再选中它。**不需要做任何事**。
2. **还没拿到过可用快照就被删了**（常见于发布后 24 小时内就撤稿）：三个平台的
   `fetch_metrics` 实现对"内容已经不存在"都不会抛异常，而是兜底成
   `available=False` 的空指标——
   公众号在 `datacube` 里查不到对应 `msgid`（`publisher.py` 的
   `fetch_metrics`/`_empty_metrics` 分支）、
   小红书在主页最近 N 条里找不到就退到 `_metrics_via_detail`，拿不到 `xsec_token`
   照样如实报缺失而不是报错（`publishers/xhs/publisher.py:568-578`）、
   抖音的 `metrics` 动作本身就是"尽力而为，读不到就 `available=false`"
   （`publishers/douyin/service.py:1406`）。因为 `metrics/README.md`
   明确"只有非 `available=False` 的成功结果才落 `MetricSnapshot`"，
   所以**不会写脏快照，不会抛异常，也不会触发任何告警**。
   - 代价：`_persist_result`（`metrics/collector.py:505`）只有在拿到可用指标时才把
     `ContentItem.status` 从 `PUBLISHED` 推进到 `MEASURED`，这条内容会**永远卡在
     `published`**；`metric_collection_attempts` 里对应行会持续记
     `last_outcome=unavailable`，采集器每个 UTC 6 小时桶都会重新把它选进候选并再打
     一次空调用——不是错误、不产生告警，但会长期占一条"名义上还在被追踪"的记录，
     `dashboard()` / `content_list(status="published")` 里会一直看到它，`insights`
     复盘统计也会把它当"还没测完"排除在 7d 结论之外。
   - **目前没有工具或接口能把这条内容标成"已撤稿、不用再追"**。如果要处理，以下是
     可选做法，均不是本系统提供的功能，按代价从小到大排：
     a. **什么都不做**（推荐）：代价只是采集器每 6 小时白跑一次这一条，不影响任何
        业务判断，也不产生错误噪音；
     b. 在运营台账 / 工单里外部记一笔"已撤稿"，不动数据库；
     c. **直接改数据库**把 `content_items.status` 强制置为某个终态——**不建议**。
        `content_items` / `publish_records` 是运行中的活表，绕开状态机直接写会破坏
        `CONTENT_TRANSITIONS` 的不变式，且没有任何测试覆盖这条路径；真要做需要参照
        5.1 节的规格（先停服、先备份、二人复核）单独走一次运维操作，不是这份手册能
        当场拍板的事，也不在本次改动范围内。
3. **24h 快照已经拿到（状态已经是 `measured`），但 7d 快照之前内容被删**：`due_window`
   只看"该窗口是否已经被一次可用快照覆盖"，不会因为内容被删就要求重新覆盖 24h；
   但 7d 窗口如果还没到期，会按第 2 条的机制反复尝试直到 7 天期满——期满后
   `due_window` 对这条返回 `None`，不会再被选中，`status` 仍停在 `measured` 不变，
   同样不产生错误噪音，只是 7d 口径的指标会一直缺失（`insights` 复盘时会看到
   7d 窗口没有数据，这是预期行为，不是采集故障）。

### 3.5.4 根本撤不回来的情况（如实列出，不代表"可以不用管"）

- **已经被转发 / 截图 / 二次转载**：三个平台都没有机制能追溯并撤回已经流出的副本，
  删除只影响平台自身是否继续展示，**历史触达无法收回**。
- **公众号 `freepublish/get` 的 `publish_status=5/6`**
  （`PUBLISH_STATUS_TEXT`，`publishers/wechat_mp/publisher.py`）：分别是"成功后用户
  删除所有文章" / "成功后系统封禁所有文章"——平台侧的删除/封禁本身就是官方 API 认可
  的既成状态，不是可撤销操作。
- **平台侧生效延迟**：三个平台删除/下架操作后多久对外真正不可见，均**未查到**官方
  SLA，需要在真实账号上操作一次后补记录，这里不编造数字。
- **未认证公众号在后台手动"发表"之后**：这条内容从"系统看不见的草稿"变成"系统看不见
  的已发布"，见 3.5.2；本系统不会提醒、也不会在 `publish_records` 里补一条对应记录，
  撤稿与否全靠运营自己记得住并手工去后台处理。
- **抖音 `state=timeout` 之后没先 `reconcile` 就去撤**：可能找错作品，或误判"没发成功"
  而放弃排查，导致真正上线的那条无人知晓，见 3.5.2 的"前提"一栏。

### 3.5.5 后续（建议，不在本次实施范围）

以下两点只是建议，**不改动 `publishers/`**（P0 冻结契约，改它要单独立项评审）：

- 若要封装 `freepublish/delete`：建议接口形状是
  `WechatMpClient.freepublish_delete(article_id: str, index: int | None = None) -> None`，
  错误码复用既有的 `_raise_for_errcode` 分类；`WechatMpPublisher` 层面再包一个显式的
  `retract(...)` 方法（不属于 `Publisher` 抽象基类当前的方法集，需要评估是作为新的
  冻结契约方法还是像 `fetch_metrics_for_title` 一样做成 `hasattr` 探测的可选能力）。
- 若要让"已撤稿但还没等到可用快照"的内容收敛：建议在 `core/state_machine.py` 里补一个
  显式终态（例如 `RETRACTED`），允许 `PUBLISHED`/`MEASURED` → `RETRACTED` 的合法迁移，
  并配一个新的工作台工具（类比 `content_reject`，可以叫 `content_retract`）。这涉及
  P0 冻结的迁移表与状态机测试，需要单独立项。
- 在以上任何一项落地之前，运维只能用 3.5.3 第 2 条"外部记录、不动数据库"作为过渡方案。

## 4. 验证码通道

- 唯一入口：`POST /accounts/{id}/login/code`，表单字段 `code`。
- 存储：`core/sms_inbox.py` 的进程内队列，默认保留 5 条 / 单条 300 秒过期。
- **不落库、不写日志明文、进程重启即丢失**——这是刻意设计，不是缺陷。
- 发布器用 `SMS_INBOX.pop(account_id)` 取用，取走即消费。
- **抖音额外多一步转发（P3）**：同一个端点在入队之后，还会立刻调
  `publisher.submit_sms_code(code)` → 宿主机上传器 `POST /accounts/{id}/sms_code`，
  把验证码 fill 进那个正等着的输入框（页面等不了几十秒，光靠轮询队列会超时）。
  转发失败**不算错误**（多半是"页面上现在没有验证码框"），返回体里的
  `forwarded` / `forward_detail` 会说明情况，队列里那份仍然有效。
- 红线：任何自动识别验证码的实现都会被审计打回。填写 ≠ 识别——
  验证码始终来自真人自己的手机短信。

## 4.5 视频渲染（MoneyPrinterTurbo，P3）

完整的 sidecar 说明见 `sidecars/mpt/README.md`，这里只列运维要点。

### 4.5.1 起停与配置

```bash
cp sidecars/mpt/config.example.toml sidecars/mpt/config.toml   # 必须先做
$EDITOR sidecars/mpt/config.toml                                # 填素材源 key
docker compose --profile video up -d mpt
uv run python scripts/preflight.py        # 会检查 config.toml + key + API 存活
```

`config.toml` 在 `.gitignore` 里（含素材源 key）。**忘了 cp 就起容器**，Docker 会在
`sidecars/mpt/config.toml` 建一个**目录**，MPT 读配置失败——preflight 会先 WARN 提醒。

配置分两处，别搞混：

| 配在哪 | 谁读 | 内容 |
|---|---|---|
| `sidecars/mpt/config.toml` | **MPT 容器** | 素材源 key、字幕后端、队列上限、`endpoint` |
| `.env`（`MPT_*`） | **core** | sidecar 地址、超时、轮询间隔、默认音色 / 素材源 / 字幕位置 |

`.env` 里的 `PEXELS_API_KEY` / `PIXABAY_API_KEY` **只被 preflight 用来验 key 是否有效**，
MPT 自己不读它们。两边不同步是"key 明明配了却还是 `materials` 阶段失败"的头号原因。

### 4.5.2 渲染时长预算

- 闸门：`DAILY_RENDER_SECONDS_BUDGET`（默认 3600 秒/天），账本在 `cost_ledger`
  的 `kind="render_seconds"`，`/stats` 能看当日用量。
- **提交前**按估算值（成片时长 × 4，下限 60 秒）查余额，不够就**不提交**——
  MPT 一开跑就是几分钟 CPU 与素材源配额，提交完再发现超预算已经晚了。
- **完成后**按真实墙钟耗时记账；超出剩余额度时记满剩余并留 warning，
  **不会**把已经渲好的片子扔掉。
- 一条 45 秒的片子实测量级是几分钟，默认 3600 秒/天大约够 10–20 条。

### 4.5.3 任务丢失（MPT 重启）

MPT 的任务表默认在**进程内存**里，容器一重启 `GET /api/v1/tasks/{id}` 就变 404。

- core 侧把 `task_id` 落在 **`render_jobs`** 表（P3 新增，`create_all` 自动建表）。
- 404 → 任务标 `lost`；生成链**原样重提交一次**，再丢就报错让人看。
- `tick_render_jobs`（每分钟）轮询 `pending`/`running` 的任务，完成后把成片下载到
  `data/media/<content_item_id>/video.mp4` 并挂回内容包。
- **已批准的内容不再补挂**：那会让"人看过的"和"发出去的"不是一份，审计链就断了。
  这种情况要人工重跑生成链。

查现场：

```sql
-- 卡住的渲染任务
select id, content_item_id, task_id, state, progress, attempts, last_error
from render_jobs where state in ('pending','running') order by created_at;
```

### 4.5.4 成片放行闸门

含视频的内容在审核页上，**勾选「已完整观看」之前批准按钮是灰的**；后端还会再校验一次
（`watched` 表单字段，缺了返回 422），所以 `curl` 也绕不过去。勾选后
`platform_extra.watched_by / watched_at` 会写进内容包作为合规证据。

### 4.5.5 常见失败

| `failed_stage` | 含义与处置 |
|---|---|
| `preflight` | MPT 自检失败，多半是 config.toml 缺项。看 `docker compose logs mpt` |
| `script` / `terms` | 本项目不该出现——脚本由 Claude 灌入。出现说明 `video_script` 空了 |
| `audio` | edge-tts 出网失败。配 `[proxy]` 或换 `voice_name` |
| `materials` | **最常见**：素材源 key 没配 / 配额用完 / 检索词太抽象。检查 `platform_extra.search_terms` 是不是具体可拍的英文名词 |
| `video` / `pipeline` | ffmpeg 阶段出错，看容器日志 |

注意上游**没有** `subtitle` 这个失败阶段——字幕出问题不致命，只会进 `warnings`。

## 4.6 热榜采集 sidecar（TrendRadar，P4）

完整说明见 `sidecars/trendradar/README.md`，这里只列运维要点。

### 4.6.1 起停

```bash
mkdir -p sidecars/trendradar/config
cp sidecars/trendradar/config.example.yaml         sidecars/trendradar/config/config.yaml
cp sidecars/trendradar/frequency_words.example.txt sidecars/trendradar/config/frequency_words.txt
docker compose --profile sourcing up -d trendradar
```

**两个配置文件缺一个，上游 `entrypoint.sh` 就直接 `exit 1`**（容器起不来但也不报错到
core 侧）。preflight 会先 WARN 提醒。

`.env`：

```bash
TRENDRADAR_BASE_URL=http://localhost:8081     # core 在宿主机上（compose 把 8080 映到 8081）
# TRENDRADAR_BASE_URL=http://trendradar:8080  # core 在 compose 里
```

### 4.6.2 它没有 REST API

这是最容易踩的一条。上游 8080 端口是 **`python -m http.server` 挂出来的 `output/` 目录**，
不是 API。我们读它产出的文件：

| 模式 | 读什么 | 何时用 |
|---|---|---|
| `db`（默认） | `GET {base}/news/{YYYY-MM-DD}.db` → SQLite | 路径确定，推荐 |
| `txt` | `GET {base}/txt/{YYYY-MM-DD}/` 目录列表 → 最新 `{HH-MM}.txt` | db 模式解析不了时 |
| `auto` | 先 db，失败退 txt | 默认 |

排查：

```bash
curl -s  http://localhost:8081/                          # 目录列表
curl -sI http://localhost:8081/news/$(date -u +%F).db    # 200 = 今天的库已生成
docker compose logs trendradar | tail -30
uv run python -c "from sourcing import trendradar as t; print(len(t.fetch(limit=5)))"
```

### 4.6.3 常见现象

| 现象 | 原因与处置 |
|---|---|
| `还没有 YYYY-MM-DD 的热榜库（404）` | 容器刚起，还没到第一个 `CRON_SCHEDULE`（默认 30 分钟）。设 `IMMEDIATE_RUN=true` 让它起来就抓一次 |
| `不是 SQLite 文件` | 路径拿到的是目录列表 HTML，多半是 `TRENDRADAR_BASE_URL` 少了/多了路径段 |
| `库结构与预期不符` | 上游改了 `storage/schema.sql`。临时把 `TRENDRADAR_MODE=txt` 顶一下，然后对照 README 里的 schema 更新 `sourcing/trendradar.py` |
| 日期目录对不上 | 上游按容器的 `TZ` 切日期。compose 里的 `TZ` 要和 `SW_TIMEZONE` 一致 |
| 选题和 newsnow 大量重复 | **正常**：TrendRadar 自己不爬平台，它转手调 newsnow。跨源去重由 `persist_topics` 兜住 |

## 4.7 复盘 Agent 与统计页（P4）

### 4.7.1 复盘 Agent

`metrics/insights.py`：7 天指标 → Claude 结构化复盘 → 写
`prompts/accounts/<account_id>/insights.md` → `sourcing/selector.py` 选题时读回去。
这是"指标 → 选题权重"闭环的最后一环。

```bash
curl -sX POST 'localhost:8000/dev/tick/insights?force=true'
curl -sX POST 'localhost:8000/dev/tick/insights?account_id=xhs-demo-01&force=true'
cat prompts/accounts/xhs-demo-01/insights.md
```

行为约定：

- **无 `ANTHROPIC_API_KEY` 时整体跳过**（统计里 `skipped_no_key`），
  刻意**不**回落 ScriptedLLM——复盘是会持续影响后续选题的长期资产，
  用预置假文本污染它比不写更糟。
- 7 天内已发且采到指标的内容少于 `INSIGHTS_MIN_POSTS`（默认 3）就不跑：
  三条数据推不出规律，只会生成一段像模像样的噪声（统计里 `skipped_sample`）。
- 每个账号内部按 `INSIGHTS_INTERVAL_HOURS`（默认 24h）节流，`force=true` 可跳过。
- 文件是**追加**的，只保留最近 `INSIGHTS_KEEP`（默认 6）条；条目之间用
  `<!-- insight -->` 分隔。**人可以直接改这个文件**，和 `persona.md` 一样进 git。
- 模型调用失败会把原因写进 `Account.extra['insights_error']` 并**推进时间戳**，
  否则每轮 tick 都会对同一个坏账号重试烧 token。

### 4.7.2 统计页

`/stats`（HTML）与 `/stats.json`（同一份数据），默认近 7 天，`?days=30` 可改（1–90）。

| 看什么 | 在哪 |
|---|---|
| 谁需要人处理 | 顶部红框：`needs_relogin` / `banned` 的账号，直接给扫码链接 |
| 各账号发布 / 失败 / 死信 | 账号表，按平台排序，需要处理的排最上面 |
| 今日配额还剩多少 | 「今日配额」列（`已用/上限`，来自 `publish_records`，不是内存计数）|
| 指标汇总 | 每条内容取**最新一张**快照再求和；`—` 表示**没有数据**而不是 0 |
| 24h / 7d 快照各几份 | 总览行。两个都为 0 说明指标链没跑起来 |
| 成本按账号 | 靠 `CostLedger.meta['account_id']`（`BudgetGuard(labels=...)` 写入）。复盘 Agent 等不带标签的计入"未归属" |
| 最近的死信 | 页面底部，带 `ReviewLog` 里的原因 |

## 5. 故障处置

| 现象 | 处置 |
|---|---|
| 小红书 sidecar 连不上 | `docker compose ps` 看容器；`curl -s http://localhost:18060/health`；核对 `XHS_MCP_ENDPOINTS` 端口。账号会是 `degraded`，排期不会被挂起 |
| 小红书发布报"图片文件不存在" | 素材不在 `XHS_MEDIA_HOST_DIR`（默认 `data/media`）下，容器里看不到；见 `sidecars/xhs/README.md` |
| 小红书内容有 `platform_post_id` 但没链接 | 占位 id，等下一次 `tick_metrics` 兜底解析回填（见上节） |
| 内容卡在 `retrying` | 看 `/review/{id}` 的发布记录表 `last_error`；修因后等下一轮 tick |
| 内容进 `dead_letter` | 终态。确认平台侧确实没发出去后，复制内容重新走一遍审核流程 |
| 账号 `needs_relogin` | 见第 2 节；挂起项会自动放回，不要手工改状态 |
| 账号 `banned` | 人工终态，状态机不允许自动恢复。先停掉该账号所有任务，人工申诉 |
| 成本超限 | `/stats` 看当日用量；调 `DAILY_TOKEN_BUDGET` / `DAILY_RENDER_SECONDS_BUDGET` 或等次日 UTC 0 点重置 |
| 抖音内容"缺成片" | 渲染没跟上或失败。看 `render_jobs`（第 4.5.3 节）；`running` 的等 `tick_render_jobs` 补挂，`failed` 的看 `last_error` 与 `meta.failed_stage` |
| 渲染任务全变 `lost` | MPT 容器在反复重启（`docker compose logs mpt`）。任务表在内存里，重启即丢 |
| 成片是横屏 / 时长超限 | `review.inspect` 会以 `douyin.video.aspect` / `douyin.video.too_long` block 掉，改参数重渲 |
| 重复发布 | 不应发生。若发生，查 `publish_records` 是否有两条不同 `idem_key` 指向同一内容（说明 `scheduled_slot` 抖动） |
| **排期项一直不发** | 按顺序排除（六道闸门全表见 1.6）：① `/stats` 看账号是不是 `ok`；② `POST /dev/tick/scheduled_publish` 看返回的 `skipped_*` 是哪一项；③ `skipped_unconfirmed` → 等人点「确认发布」，**最常见**，不是故障（7.6.4）；④ `skipped_window` → 窗口/时区配错；⑤ `skipped_rate` → 配额用完或没到最小间隔；⑥ `skipped_publisher` → 该平台发布器没注册；⑦ `skipped_not_advanced` → **正常恒为 0**，非零说明发布器破了 publish 契约，查 `publish_records.last_error`；⑧ `scanned=0` → `scheduled_at` 还没到 |
| **一条稿都不生成** | ① `python -m core.accounts list` 看 `daily_target` 是不是 0；② `POST /dev/tick/sourcing` 看选题池空不空；③ `/stats` 看 token 预算是不是耗尽；④ `SW_GENERATE_ENABLED` 是不是被关了 |
| **调度器像没在跑** | 看 core 启动日志有没有「调度器已启动」。`SW_SCHEDULER_ENABLED=false` 时只剩 `/dev/tick/*`。也可能是两个进程各起了一套（见 1.6） |
| 账号在台账里但系统说不存在 | 没跑 `python -m core.accounts sync`（第 1 节第 3 步）。preflight 的「台账已入库」会直接报 FAIL |
| 选题池空 | `POST /dev/tick/sourcing` 看返回：`sources_ok=0` 说明所有源都不可用。newsnow 要配 `NEWSNOW_BASE_URL`，TrendRadar 见 4.6 |
| 复盘一直不出 | `POST /dev/tick/insights?force=true` 看返回：`skipped_no_key` = 没配 key；`skipped_sample` = 7 天内已发不足 `INSIGHTS_MIN_POSTS` 条 |
| 内容永远卡在 `retrying` | 多半是账号 `needs_relogin`（`NeedsReloginError` 不计 attempts）。续期后自动恢复；48 小时（`SW_RETRY_MAX_AGE_HOURS`）后兜底进死信并告警 |
| 限频计数看起来不对 | P4 起真相在 `publish_records`（见 1.5 的 SQL）。内存计数只是缓存，重启会重新从库里读 |

## 5.1 指标采集 future bucket 时钟异常

**仅生产值班人员执行。** 此处只处理 `MetricCollectionAttempt` 的辅助尝试记录；不会修改真实
payload、`metric_snapshots` 或任何发布记录。不要把它当成应用的自动修复：指标 claim 必须保持
单调，只有人工确认的未来 bucket 才能进入本 runbook。

`metric_collection_attempts` 的字段是 `content_item_id`、`last_attempt_at`、
`last_attempt_bucket`、`last_outcome`、`updated_at`。bucket 固定为
`floor(UTC Unix timestamp / 21600)`。正常情况下，同一内容在同一个 UTC 六小时桶内最多开始
一次（at-most-once），这不是故障；故障证据是 `last_attempt_bucket > current_bucket`。

症状常见为 metrics tick 反复 `attempted=0`，或少量已经到 24h/7d 窗口的内容长期没有新的
attempt。它通常意味着宿主机或容器曾以错误的未来时间运行。以下命令除特别标为“修复”外都只读。
不要直接复制示例到未确认的环境，也不要在时钟仍有问题时修改数据库。

下面标为“生产机”的命令在生产服务器的仓库目录执行：

```bash
cd "${HOME}/social_workflow"
```

标为“值班工作站”的命令在有本仓库与 IAP SSH 配置的值班机执行；现有
`scripts/ops/*.sh` 会连接生产机。

### 1. 先修复时间，再做只读诊断

先确认宿主机和运行中 core 容器的 UTC 时间一致，并与可信 NTP 时间源一致。若有漂移，按该环境
批准的 NTP/宿主机时钟流程修正并重新检查；**未修好之前在此停止，不运行任何 DB 修复命令。**

生产机：

```bash
printf 'host_utc      '; date -u '+%Y-%m-%dT%H:%M:%SZ\n'
printf 'container_utc '; docker compose exec -T core date -u '+%Y-%m-%dT%H:%M:%SZ\n'
```

时钟已修正且 core 仍在运行后，用这个只读连接诊断。它只输出允许查看的六列：
`content_item_id`、`last_attempt_at`、`last_attempt_bucket`、`last_outcome`、
`current_bucket`、`ahead_buckets`；不会读取 title、payload、账号或凭据，也不会写库或打印 SQL
参数。

```bash
docker compose exec -T core python3 - <<'PY'
from datetime import UTC, datetime
from pathlib import Path
import sqlite3

db_path = Path('/app/data/social_workflow.db')
if not db_path.is_file():
    raise SystemExit('readonly diagnostics failed')

current_bucket = int(datetime.now(UTC).timestamp() // 21600)
columns = (
    'content_item_id',
    'last_attempt_at',
    'last_attempt_bucket',
    'last_outcome',
    'current_bucket',
    'ahead_buckets',
)
with sqlite3.connect(f'file:{db_path}?mode=ro', uri=True) as connection:
    connection.execute('PRAGMA query_only = ON')
    rows = connection.execute(
        '''
        SELECT content_item_id,
               last_attempt_at,
               last_attempt_bucket,
               last_outcome,
               ? AS current_bucket,
               last_attempt_bucket - ? AS ahead_buckets
          FROM metric_collection_attempts
         WHERE last_attempt_bucket > ?
         ORDER BY last_attempt_bucket DESC, content_item_id
        ''',
        (current_bucket, current_bucket, current_bucket),
    ).fetchall()

print('\t'.join(columns))
for row in rows:
    print('\t'.join(str(value) for value in row))
PY
```

只有表头、没有数据行表示 0 个未来 bucket：停止，不修复。若有行，记录精确的
`content_item_id` 集合与数量，且先执行下一步备份；不要因为同桶 at-most-once、`max_items` 或
backlog 造成的正常延迟而误判。

### 2. 在停服前创建并验证一致性备份

先在值班工作站运行现有备份脚本：

```bash
bash scripts/ops/backup.sh
```

该脚本需要 core **仍在运行**，因为它通过 `docker compose exec -T core` 调用 SQLite 在线备份 API。
它在卷内保留 `/app/data/backups/sw-<UTC时间戳>.db`，并把同一一致性快照拷到值班工作站的
`${SW_SERVER_BACKUP_DIR:-${HOME}/sw-server-backups}/<UTC时间戳>/social_workflow.db`。备份输出中的
时间戳就是本次事件的 `BACKUP_STAMP`；保留卷内与本地原文件，直到事件关闭。

值班工作站：把输出中的**精确**本地目录和 UTC 时间戳抄入下面变量。先用白名单校验时间戳，再验证
本次而不是“最新的某份”本地备份：

```bash
BACKUP_DIR='/absolute/path/printed-by-backup.sh'
BACKUP_STAMP='YYYYMMDDTHHMMSSZ'  # 仅替换为本次 backup.sh 输出中的实际 UTC 时间戳
export BACKUP_STAMP

if ! [[ "${BACKUP_STAMP}" =~ ^[0-9]{8}T[0-9]{6}Z$ ]]; then
  printf 'invalid backup stamp; stop\n' >&2
  exit 1
fi
test -s "${BACKUP_DIR}/social_workflow.db" || {
  printf 'backup missing or empty; stop\n' >&2
  exit 1
}
```

随后、**仍在停 core 之前**，在生产机将同一个精确时间戳写入该 shell，并通过运行中的 core 只读验证
卷内备份。两处都通过才可以停服：

```bash
BACKUP_STAMP='YYYYMMDDTHHMMSSZ'  # 使用值班工作站刚验证过的同一实际 UTC 时间戳
export BACKUP_STAMP
if ! [[ "${BACKUP_STAMP}" =~ ^[0-9]{8}T[0-9]{6}Z$ ]]; then
  printf 'invalid backup stamp; stop\n' >&2
  exit 1
fi
if ! docker compose exec -T -e "BACKUP_STAMP=${BACKUP_STAMP}" core python3 - <<'PY'
import os
import re
from pathlib import Path

stamp = os.environ.get('BACKUP_STAMP', '')
if not re.fullmatch(r'\d{8}T\d{6}Z', stamp):
    raise SystemExit('invalid backup stamp; stop')
backup_path = Path('/app/data/backups') / f'sw-{stamp}.db'
if not backup_path.is_file() or backup_path.stat().st_size == 0:
    raise SystemExit('backup missing or empty; stop')
print('backup_verified')
PY
then
  printf 'volume backup verification failed; stop\n' >&2
  exit 1
fi
```

若备份脚本、时间戳、本地验证或卷内验证任一失败，停止；不能以常规文件复制替代此处的在线一致性备份。

### 3. 停掉所有 DB 写者并复核受影响行

回到生产机。`BACKUP_STAMP` 已在步骤 2 由实际备份输出确认；它仅用于验证已存在的备份文件，
不进入任何 SQL。先停服务；停服务、服务状态检查或写者检查任何一步失败都必须停止。

```bash
if ! docker compose stop core; then
  printf 'could not stop core; stop\n' >&2
  exit 1
fi

ps_output=''
if ps_output="$(docker compose ps --status running --services)"; then
  :
else
  printf 'could not verify compose service status; stop\n' >&2
  exit 1
fi
grep_rc=0
grep -Fxq 'core' <<<"${ps_output}" || grep_rc=$?
case "${grep_rc}" in
  0)
    printf 'core is still running; stop\n' >&2
    exit 1
    ;;
  1)
    ;;
  *)
    printf 'could not inspect compose service status; stop\n' >&2
    exit 1
    ;;
esac

pgrep_rc=0
pgrep -f 'core\.main:app' >/dev/null 2>&1 || pgrep_rc=$?
case "${pgrep_rc}" in
  0)
    printf 'a manually started core process is still running; stop\n' >&2
    exit 1
    ;;
  1)
    ;;
  *)
    printf 'could not check for manually started core processes; stop\n' >&2
    exit 1
    ;;
esac
```

此后不得另起 uvicorn、手动 tick 或其他会写 `/app/data/social_workflow.db` 的维护进程。上面的
`docker compose run --no-deps core python3` 只会启动一个一次性 Python 容器，不启动 core 服务或
scheduler；它是停服后安全访问 `core_data` volume 的方式。卷内备份已在停服前验证；以下是停服后的
第二次只读复核，不替代步骤 2 的首次验证：

```bash
docker compose run --rm --no-deps -T -e BACKUP_STAMP core python3 - <<'PY'
import os
import re
from pathlib import Path

stamp = os.environ.get('BACKUP_STAMP', '')
if not re.fullmatch(r'\d{8}T\d{6}Z', stamp):
    raise SystemExit('invalid backup stamp; stop')
backup_path = Path('/app/data/backups') / f'sw-{stamp}.db'
if not backup_path.is_file() or backup_path.stat().st_size == 0:
    raise SystemExit('backup missing or empty; stop')
print('backup_verified')
PY
```

再次以只读方式列出精确 ID 和数量。这里仍不写库；`affected_count` 必须与步骤 1 的确认结果一致，
否则保持 core 停止并重新调查。

```bash
docker compose run --rm --no-deps -T core python3 - <<'PY'
from datetime import UTC, datetime
from pathlib import Path
import sqlite3

db_path = Path('/app/data/social_workflow.db')
if not db_path.is_file():
    raise SystemExit('readonly inventory failed')

current_bucket = int(datetime.now(UTC).timestamp() // 21600)
columns = (
    'content_item_id',
    'last_attempt_at',
    'last_attempt_bucket',
    'last_outcome',
    'current_bucket',
    'ahead_buckets',
)
with sqlite3.connect(f'file:{db_path}?mode=ro', uri=True) as connection:
    connection.execute('PRAGMA query_only = ON')
    rows = connection.execute(
        '''
        SELECT content_item_id,
               last_attempt_at,
               last_attempt_bucket,
               last_outcome,
               ? AS current_bucket,
               last_attempt_bucket - ? AS ahead_buckets
          FROM metric_collection_attempts
         WHERE last_attempt_bucket > ?
         ORDER BY last_attempt_bucket DESC, content_item_id
        ''',
        (current_bucket, current_bucket, current_bucket),
    ).fetchall()

print('\t'.join(columns))
for row in rows:
    print('\t'.join(str(value) for value in row))
print(f'affected_count\t{len(rows)}')
PY
```

若 `affected_count` 为 0，直接启动 core（见步骤 6），不做修复。若 ID 或数量不符合预期，也不做
修复：保留现场、复查时钟和可能的其它写者。

### 4. 受保护的定向修复

最保守的恢复是**只删除**经两次诊断确认的未来 attempt 辅助行；下一次合格的 tick 会以正确的当前
bucket 重新 claim。它不改写 bucket、不降低单调 claim，也不会触碰真实 payload 或快照。

把上一步输出的精确 ID 填入 `EXPECTED_IDS`（默认空集必然失败关闭），并由第二位值班人员对照
ID 与数量。ID 只在 Python 内存中比较，绝不拼接到 SQL。脚本会在一个 `BEGIN IMMEDIATE` 事务里重新
计算 bucket、先打印将删除的行，再以 `WHERE last_attempt_bucket > recomputed_current_bucket` 删除；
行集或行数任何不符都会 rollback 并退出。

```bash
docker compose run --rm --no-deps -T -e BACKUP_STAMP core python3 - <<'PY'
from datetime import UTC, datetime
import os
from pathlib import Path
import re
import sqlite3

# Replace only after the stopped-service read-only inventory and a second-person check.
EXPECTED_IDS = frozenset({
    # 'itm_exact_id_from_step_3',
})

stamp = os.environ.get('BACKUP_STAMP', '')
if not re.fullmatch(r'\d{8}T\d{6}Z', stamp):
    raise SystemExit('invalid backup stamp; stop')
backup_path = Path('/app/data/backups') / f'sw-{stamp}.db'
db_path = Path('/app/data/social_workflow.db')
if not EXPECTED_IDS:
    raise SystemExit('EXPECTED_IDS is empty; refusing repair')
if not backup_path.is_file() or backup_path.stat().st_size == 0:
    raise SystemExit('backup missing or empty; refusing repair')
if not db_path.is_file():
    raise SystemExit('database missing; refusing repair')

connection = sqlite3.connect(
    f'file:{db_path}?mode=rw', uri=True, isolation_level=None
)
try:
    connection.execute('PRAGMA foreign_keys = ON')
    connection.execute('BEGIN IMMEDIATE')
    current_bucket = int(datetime.now(UTC).timestamp() // 21600)
    rows = connection.execute(
        '''
        SELECT content_item_id,
               last_attempt_at,
               last_attempt_bucket,
               last_outcome,
               ? AS current_bucket,
               last_attempt_bucket - ? AS ahead_buckets
          FROM metric_collection_attempts
         WHERE last_attempt_bucket > ?
         ORDER BY last_attempt_bucket DESC, content_item_id
        ''',
        (current_bucket, current_bucket, current_bucket),
    ).fetchall()
    actual_ids = frozenset(row[0] for row in rows)
    if not actual_ids:
        raise RuntimeError('no future rows remain; refusing repair')
    if actual_ids != EXPECTED_IDS:
        raise RuntimeError(
            f'future ID set changed: expected {len(EXPECTED_IDS)}, found {len(actual_ids)}'
        )

    print('content_item_id\tlast_attempt_at\tlast_attempt_bucket\tlast_outcome\tcurrent_bucket\tahead_buckets')
    for row in rows:
        print('\t'.join(str(value) for value in row))

    deleted = connection.execute(
        '''
        DELETE FROM metric_collection_attempts
         WHERE last_attempt_bucket > ?
        ''',
        (current_bucket,),
    ).rowcount
    if deleted != len(rows):
        raise RuntimeError(f'delete count mismatch: expected {len(rows)}, got {deleted}')
except BaseException:
    connection.rollback()
    raise
else:
    connection.commit()
    print(f'repaired_attempt_rows\t{deleted}')
finally:
    connection.close()
PY
```

禁止把此操作改成全表 `UPDATE`/`DELETE`，禁止用用户输入拼 SQL，也不要试图手工把 bucket 写回较小
数值。异常、事务冲突、ID 集变化或删除数不符时脚本会回滚；保持 core 停止并转入回滚/升级处置。

### 5. 只读复检

修复命令成功后、启动 core 前，用相同的一次性容器只读复检。表头之后必须没有数据行；有任何行就
不要启动 core，也不要重复执行修复。

```bash
docker compose run --rm --no-deps -T core python3 - <<'PY'
from datetime import UTC, datetime
from pathlib import Path
import sqlite3

db_path = Path('/app/data/social_workflow.db')
if not db_path.is_file():
    raise SystemExit('readonly verification failed')

current_bucket = int(datetime.now(UTC).timestamp() // 21600)
with sqlite3.connect(f'file:{db_path}?mode=ro', uri=True) as connection:
    connection.execute('PRAGMA query_only = ON')
    rows = connection.execute(
        '''
        SELECT content_item_id,
               last_attempt_at,
               last_attempt_bucket,
               last_outcome,
               ? AS current_bucket,
               last_attempt_bucket - ? AS ahead_buckets
          FROM metric_collection_attempts
         WHERE last_attempt_bucket > ?
         ORDER BY last_attempt_bucket DESC, content_item_id
        ''',
        (current_bucket, current_bucket, current_bucket),
    ).fetchall()

print('content_item_id\tlast_attempt_at\tlast_attempt_bucket\tlast_outcome\tcurrent_bucket\tahead_buckets')
for row in rows:
    print('\t'.join(str(value) for value in row))
if rows:
    raise SystemExit('future rows remain; core must stay stopped')
PY
```

### 6. 恢复服务并观察一次生产口径 tick

生产机启动 core：

```bash
docker compose start core
```

随后在值班工作站运行现有只读状态检查：

```bash
bash scripts/ops/status.sh
```

只有其输出显示 `/api/v1/system/info` 正常且“调度器”为 `True` 才继续；否则停止并调查启动配置。然后在
生产机手动跑一次**生产口径** metrics tick，并查看返回的 `stats`：

```bash
curl -fsS -X POST 'http://127.0.0.1:8000/dev/tick/metrics?respect_windows=true'
```

即使恢复成功，本桶的 at-most-once 语义仍然有效，且 `max_items` 与 backlog 会限制一轮服务的内容
数量；因此不保证所有内容立即得到新快照。下一轮在窗口仍未覆盖时会继续按正常公平队列处理。

### 7. 异常时回滚本次修复

若修复后发现异常，先按步骤 3 再次停止并确认 core 没有运行；**绝不能在服务运行时覆盖 DB**。保留
当前文件，并从本次 `backup.sh` 创建的卷内快照恢复。以下命令先把现有 DB 保留为
`/app/data/social_workflow.db.pre-rollback-<BACKUP_STAMP>`，以 SQLite backup API 复制快照到临时
文件、做 integrity check，最后才原子替换；core 使用 WAL 模式，所以若存在也会保留并移走旧的
`-wal` / `-shm` sidecar。不会删除本次原始备份文件。

```bash
docker compose run --rm --no-deps -T -e BACKUP_STAMP core python3 - <<'PY'
import os
from pathlib import Path
import re
import sqlite3

stamp = os.environ.get('BACKUP_STAMP', '')
if not re.fullmatch(r'\d{8}T\d{6}Z', stamp):
    raise SystemExit('invalid backup stamp; stop')
data_dir = Path('/app/data')
backup_path = data_dir / 'backups' / f'sw-{stamp}.db'
live_path = data_dir / 'social_workflow.db'
preserved_path = data_dir / f'social_workflow.db.pre-rollback-{stamp}'
temporary_path = data_dir / f'.social_workflow.db.rollback-{stamp}.tmp'
if not backup_path.is_file() or backup_path.stat().st_size == 0:
    raise SystemExit('backup missing or empty; stop')
if not live_path.is_file():
    raise SystemExit('live database missing; stop')
if preserved_path.exists() or temporary_path.exists():
    raise SystemExit('rollback target already exists; refusing overwrite')

# Use SQLite's backup API here too: core runs SQLite in WAL mode, so a plain file
# copy would not be a transaction-consistent preserved copy of the current state.
with sqlite3.connect(f'file:{live_path}?mode=ro', uri=True) as source:
    with sqlite3.connect(str(preserved_path)) as target:
        source.backup(target)
if not preserved_path.is_file() or preserved_path.stat().st_size == 0:
    raise SystemExit('could not preserve current database; stop')

with sqlite3.connect(f'file:{backup_path}?mode=ro', uri=True) as source:
    with sqlite3.connect(str(temporary_path)) as target:
        source.backup(target)
with sqlite3.connect(f'file:{temporary_path}?mode=ro', uri=True) as check:
    if check.execute('PRAGMA integrity_check').fetchone() != ('ok',):
        raise SystemExit('backup integrity check failed; stop')

# A restored main DB must not be paired with the stopped instance's old WAL files.
# Preflight all names, then preserve rather than delete sidecars. If a rename fails,
# put back any sidecar already moved and leave the live main DB untouched.
sidecars = []
for suffix in ('-wal', '-shm'):
    sidecar_path = Path(f'{live_path}{suffix}')
    if sidecar_path.exists():
        preserved_sidecar = data_dir / f'{sidecar_path.name}.pre-rollback-{stamp}'
        if preserved_sidecar.exists():
            raise SystemExit('preserved WAL sidecar already exists; refusing overwrite')
        sidecars.append((sidecar_path, preserved_sidecar))

moved_sidecars = []
try:
    for sidecar_path, preserved_sidecar in sidecars:
        os.replace(sidecar_path, preserved_sidecar)
        moved_sidecars.append((sidecar_path, preserved_sidecar))
    os.replace(temporary_path, live_path)
except BaseException:
    for sidecar_path, preserved_sidecar in reversed(moved_sidecars):
        if preserved_sidecar.exists() and not sidecar_path.exists():
            os.replace(preserved_sidecar, sidecar_path)
    raise

print('rollback_complete')
PY
```

接着重复步骤 6 的启动、`status.sh` 探针/调度器检查和生产口径 tick。若回滚也无法恢复，保持 core
停止，保留本地与卷内备份、`pre-rollback` 文件和命令输出，升级给平台/数据库值班人员。

## 5.5 连续运行验证（soak）

改完调度相关的任何东西，跑一遍这个再提交：

```bash
uv run python scripts/soak.py                    # 默认模拟 3 天，1 秒内跑完
uv run python scripts/soak.py --json             # 机器可读（CI 用这个）
uv run python scripts/soak.py --days 7 --step-minutes 15
uv run python scripts/soak.py --db data/soak.db --keep   # 留现场，跑完自己去翻表
uv run pytest tests/test_soak.py -q              # 同一个 run_soak 的回归版本
```

它在加速时钟下驱动真实的 tick 函数（FakePublisher + ScriptedLLM，不联网不烧 token），
断言六件事：**无重复发布**、**排期层排出来的槽位都合法**、**发布层的窗口 / 限频闸门
挡得住脏排期**、**`needs_relogin` 账号被跳过**、**死信触发通知**、
**24h / 7d 指标快照各至少一份**。任何一条挂了退出码是 1。

限频与时段窗口有**两层防御**，soak 两层分别验：

- **排期层**（`core/scheduling.py`，批准时算槽位）：断言 `schedule_item` 排出来的每个
  `scheduled_at` 都在账号 `publish_windows` 内、同账号相邻槽位 ≥ `min_interval`、
  单个本地日不超日上限。
- **发布层**（`tick_scheduled_publish`，发布时再校验一遍）：soak 每天往库里注入一批
  **绕过排期层**的脏排期（窗口外的、挤在同一分钟的），模拟人工改库 / 时钟漂移，
  断言它们一条都没能在窗口外或超额发出去——只会被推迟到合法时刻。
  改库改出来的排期能不能发，看的就是这一层。

输出长这样：

```
模拟 3 天 · 每步 30 分钟 · 共 144 步（起点 2026-08-16T13:00:00+00:00）
生成 12 · 发布 12 · 重试成功 0 · 死信 3
排期层：批准 12 · 自动排期 12 · 槽位违规 0
发布层：注入脏排期 9 · 其中发出 6（推迟发出 6）· 窗口外发出 0
跳过：限频 30 · 时段 408 · 账号不健康 288
指标快照：24h 12 份 · 7d 4 份
各账号单日最高发布数：{'soak-xhs-tampered': 2, 'soak-xhs-ok': 2}
  [PASS] 无重复发布：同一时刻重跑 tick 不产生新发布
  ...
```

**它不能替代真实发布验证**：FakePublisher 不会碰任何平台。真号验证仍然是
"每个平台人工目视确认一次"（计划第五节）。

### 本地 CI 复现

GitHub Actions 因账单停用期间，用 `bash scripts/ci_local.sh` 在本机复现五个 CI job
（`test`/`compose`/`soak`/`render-smoke`/`ops`），或用
`bash scripts/ci_local.sh test` 只跑指定 job；结尾会给出 `PASS/FAIL/WARN/SKIP` 汇总，
其中 render-smoke 的失败与 CI 一样只记 WARN，不拖红整体。`ops` 不依赖
uv/venv/Docker/网络，按文件名排序遍历 `tests/ops/test_*.sh` 并逐个执行，覆盖
`scripts/ops/` 下生产运维脚本的安全校验；一个测试都找不到时记为失败，避免
"测试消失"被静默放过。

### 5.6 e2e 走查截图的时间锚点

`SW_E2E_TIME_ANCHOR` 用于把 Playwright 走查中的浏览器和三台隔离 core 固定到同一
个 ISO 8601 时刻；`ui/e2e/serve.sh` 未显式设置时默认
`2026-08-19T11:00:00.000Z`，因此入库的 `ui/screenshots/` 不会因墙钟推进而漂移。

互锁规则是：变量未设置时，core 仍逐次使用真实 UTC 时钟；变量已设置且
`SW_USE_FAKE_PUBLISHERS=true` 时，core 在导入时记录 WARNING 后使用该固定时刻；变量
已设置但假发布器不为真时，core 会拒绝启动。生产环境绝不能携带这个变量：冻结时钟会让
调度、限频、预算闸门和确认截止时间按错误的“现在”工作，并可能影响真实平台发布。

临时验证跨日期鲁棒性可在 `ui/` 下运行：

```bash
SW_E2E_TIME_ANCHOR=2026-09-25T11:00:00.000Z pnpm exec playwright test
```

跑完后不带该变量再跑一次，即会回到入库基线使用的默认锚点。

## 6. 备份

```bash
# 值班工作站：SQLite 在线一致性备份 + 生产台账拷回本机
bash scripts/ops/backup.sh
```

生产备份使用现有 `backup.sh`：它在运行中的 core 容器内通过 Python 的 SQLite backup API 生成
卷内快照，并拷回本机，不依赖镜像中不存在的 `sqlite3` 命令行工具。该脚本需要 core 处于运行状态；
若处置 future bucket 时钟异常，必须遵守 [§5.1](#51-指标采集-future-bucket-时钟异常) 的“先在线备份、
验证，再停服”顺序。

需要备份的是 `core_data` volume（数据库）与各 `xhs_data_*` volume（登录态）。
`profiles/douyin/<account_id>/`（抖音 profile = 登录态）也要备份，丢了要重新扫码登录；**禁止跨账号复制**（那就是 Cookie 池，见 `docs/POLICY.md`）。`data/douyin/screenshots/` 只是排障截图，可随时删。

**不需要**备份 `mpt_storage`：成片在渲染完成后已经下载到 `data/media/<item_id>/`，
那个卷里剩的是可重建的中间产物。反过来说，`data/media/` **要**备份——
审核过的成片丢了只能重渲。

`trendradar_output` 也**不需要**备份：热榜是可重抓的公开数据。

**进 git 的那部分也是资产**：`accounts.yaml`（台账）、`prompts/accounts/<id>/persona.md`
（人设）、`prompts/accounts/<id>/insights.md`（复盘结论）。它们不在 volume 里，
靠版本库保管——所以改完记得提交。

## 6.5 sidecar 升级

三个 sidecar 都用固定 tag，**不要用 `:latest` 直接升**。

| sidecar | 镜像 | 升级前必看 |
|---|---|---|
| 小红书 | `xpzouying/xiaohongshu-mcp`（本项目钉 `:v2.5.0`）| `/api/v1/*` 的路径与返回结构；升级后先 `curl /health` 再发一条测试笔记 |
| MoneyPrinterTurbo | `ghcr.io/harry0703/moneyprinterturbo` | 任务状态码（-1/1/4）与 `config.toml` 字段；成片下载路径 |
| TrendRadar | `wantcat/trendradar` | **`storage/schema.sql` 与 `output/` 布局**——我们直接读它的文件，这两处一变解析就崩 |

通用步骤：

```bash
# 1) 先在测试环境拉新版本，别动生产的 tag
docker pull xpzouying/xiaohongshu-mcp:v2.6.0

# 2) 改 accounts.yaml 里的 sidecar.image（小红书）或 .env 里的 *_IMAGE
uv run python scripts/gen_xhs_sidecars.py       # 重新生成 compose 片段
docker compose -f docker-compose.yml -f docker-compose.xhs.yml up -d

# 3) 跑门禁 + 冒烟
uv run python scripts/preflight.py
curl -sX POST localhost:8000/dev/tick/login_health
curl -sX POST localhost:8000/dev/tick/sourcing

# 4) 回滚就是把 tag 改回去再 up -d
```

**小红书升级不会丢登录态**：cookies 在 `xhs_data_*` volume 里，容器换了 volume 还在。
但**换 volume 名字就等于重新扫码**——生成脚本会在两个账号共用 volume 时直接报错
（那是 Cookie 池）。

## 6.6 只读部署核验（verify.sh）

值班工作站上一次 SSH 往返只读采集生产 core 的部署证据（git HEAD、端口门禁、健康探针、
人工确认闸门通道、Telegram 409 计数），不备份、不 fetch、不构建、不重启，可重复运行：

```bash
bash scripts/ops/verify.sh                             # 只读打印生产 HEAD，不核对具体 SHA
bash scripts/ops/verify.sh --sha <40位小写SHA>         # 部署后核验：要求 HEAD 严格等于该提交
bash scripts/ops/verify.sh --sha <SHA> --preflight     # 额外在容器内跑 preflight（默认不跑）
```

`--sha` 给了就要求生产 HEAD 逐字符等于该 40 位小写完整提交，不给只如实打印 HEAD 不比对；
`--preflight` 默认不跑，给了才在容器内执行 `scripts/preflight.py`（外部连通性探测可能耗时，
且不再只读——会读 `.env`、发真实外部请求、打印密钥脱敏指纹）。失败项清单、人工确认闸门通道
的互锁规则与完整输出样例见 `scripts/ops/README.md`。

## 6.7 生产运维命令与改生产 .env

`scripts/ops/` 下的可执行命令都经 SSH 别名 `workbench-iap` 打生产（红线 R3：**生产只经这个
目录**，不直连、不手敲远程命令）。这里每条只给一行用途；参数、输出样例与互锁规则的**单一
真相源**是 `scripts/ops/README.md`，不要照着下表推断行为：

| 命令 | 一行用途 |
|---|---|
| `bash scripts/ops/status.sh` | 只读看 Compose 服务、core 信息探针、磁盘水位与数据卷文件。 |
| `bash scripts/ops/logs.sh [行数] [-f]` | 只读看 core 最近日志，默认 200 行。 |
| `bash scripts/ops/backup.sh` | SQLite 在线一致性备份 + 生产台账拷回本机（第 6 节）。 |
| `bash scripts/ops/restart.sh` | 备份后**重启**同一个 core 容器，再核验探针与 R1 确认闸门通道。 |
| `bash scripts/ops/update.sh [--dry-run\|--apply] [--ref <分支> --sha <SHA>]` | 备份后演练或执行受限纯快进更新，可把 `origin/<分支>` 钉死到完整 SHA。 |
| `bash scripts/ops/verify.sh [--sha <SHA>] [--preflight]` | 只读部署核验，零副作用（6.6 节）。 |
| `bash scripts/ops/env_set.sh --show \| --key <白名单键> ...` | 按一份写死的**白名单**（键与闸门见 6.7.1 那张表）查看 / 变更生产 `.env`：逐键校验 → **逐键的事前闸门** → 备份 → 原子写入 → **重建容器** → 走 R1 闸门（见下）。 |
| `bash scripts/ops/sidecar.sh --status \| --materialize \| --up \| --down <sidecar>` | 按一份写死的**白名单**（`trendradar` / `xhs-downloader`；`mpt` **有意排除**）在生产上**从已部署的模板就地生成** sidecar 配置、按 profile 起停**单个** sidecar。`--up` 之前先用**远端 `docker compose config` 解析后的 `host_ip`** 确认端口只绑回环，判不了即 fail-closed 拒绝启动；一律不碰 core。细节见 `scripts/ops/README.md`「sidecar 启用」。 |

`ui_token.sh` 不是命令，是被其中若干个（以 `grep -l 'ui_token\.sh' scripts/ops/*.sh`
为准）`source` 的库，不单独执行。

### 6.7.1 改了 `.env`：本地重启进程，Compose 生产必须重建容器

**本手册里凡是写「改 `.env` 后重启 core」的地方，都只对本地开发成立。** 本地
`uvicorn --reload` 是一个普通进程，`.env` 在进程启动时读一次，重启进程就会重读，那句话没错。

**Compose 生产不是这样。** `docker-compose.yml` 的 `core` 服务是用 `env_file`（`- path: .env`）
挂进去的——compose 在**创建容器**时就把 `.env` 的值烘进容器环境，
而 `docker compose restart core` 重启的是**同一个容器**，环境原封不动，因此拿不到新值。
7.5.5 那条「改了 `.env` 但行为没变」正是这个现象。`scripts/ops/restart.sh` 内部用的就是
`docker compose restart core`，所以**它同样不能用来让 `.env` 变更生效**。真要生效只有重建容器：

```bash
docker compose up -d --force-recreate --no-build core
```

**但这一行不要手敲**（红线 R3）。生产上按键分三种情况：

| 键 | 生产上怎么改 |
|---|---|
| `SW_UI_TOKEN` | `bash scripts/ops/env_set.sh --key SW_UI_TOKEN --generate`（本机生成一个新的）或 `--from-credentials`（把本机已持有的那个推上去）。值全程不回显、不进 argv。**有事前闸门**（`signing_secret`）：生产 `.env` 里 `SW_TELEGRAM_SIGNING_SECRET` 已显式设且非空时直接放行（那时换它动不到确认卡的签名密钥），否则去读待人点的确认卡条数——0 条放行、有卡拒绝（退 `45`）、读不出来 fail-closed（退 `46`）。 |
| `SW_TELEGRAM_SIGNING_SECRET` | `--generate` 或 `--from-credentials`，同样零回显。它**就是**确认卡 `callback_data` 的 HMAC 签名密钥（`core/telegram.py:151-154` 三级回落的第一级），所以**没有免检方向**：每次都读待人点的确认卡条数。显式设上它之后，`SW_UI_TOKEN` 与 `TELEGRAM_BOT_TOKEN` 再怎么换都不会动签名密钥——**挑一个 0 条的时刻做这一次，那条耦合就永久解开了**。 |
| `TELEGRAM_BOT_TOKEN` | **只走 `--from-credentials`**：值由 BotFather 签发，本机造不出来，对它 `--generate` 会被**明确拒绝**并告诉你正确做法（去 `@BotFather` 取，自己写进 `~/.dsh-sw/.credentials.yaml` 的对应键）。它是三级回落的**第三级**，上面两级任一非空就放行；两级都空时换它就是换签名密钥，走同一道闸门。另注两条：旧 token **当场作废**，而 `/api/v1/system/telegram` 的 `polling` 那一格只看轮询线程活没活、照样报 `true`，要判通道真活着得看同一份响应里的 `last_error` 与 `stats.errors`；换完再跑一次 `bash scripts/ops/verify.sh` 看「Telegram 轮询冲突（`error_code=409`）」那一格——**同一个新 token 被两个部署同时轮询**时 Telegram 只喂一个、另一个持续 409。409 这一格**刻意只提示、不设闸门**（日志计数两个方向都会错，理由见 `docs/RISKS.md` §14.1），真撞上了处置是让另一个部署停下来或给它换一个 bot，**不是**再换一次 token。 |
| `DEEPSEEK_API_KEY` | **只走 `--from-credentials`**：值由**模型网关**签发，本机造不出来，`--generate` 会被明确拒绝并告诉你去网关控制台取（写进 `~/.dsh-sw/.credentials.yaml` 的 `deepseek_api_key` 键，0600）。**它有自己的事前闸门 `llm_key_live`，而且是唯一一道跑在本机、`ssh` 之前的**：拿这把新值真的去 `GET <baseURL>/models` 问一次网关，网关认（2xx）才写，`401/403` 拒绝（退 `47`），探不到（连不上 / 超时 / 不知道网关地址）也拒绝（退 `48`）。探哪个网关：`SW_OPS_DEEPSEEK_BASE_URL` > `SW_DSH_DEEPSEEK_BASE_URL` > 本仓 `.env` 的同名键 > `https://api.deepseek.com`。**边界要自己看一眼**：生产 `.env` 的 `SW_DSH_DEEPSEEK_BASE_URL` 不在白名单上、工具面读不到，所以闸门探的是**值班机**认识的那个网关——它会把 `host=` 打出来，两边不是同一台时这道闸门就问错了地方。它**不在**签名密钥回落链上，改它不会让任何一张确认卡失效。 |
| `SW_USE_FAKE_PUBLISHERS` | `--value <true\|false>`。`false` 方向有事前闸门：先探人工确认闸门通道。 |
| `SW_LLM_BACKEND` | `--value <anthropic\|dsh>`。**两个方向都有事前闸门**：目标后端的凭据必须在生产 `.env` 里存在且非空，否则"回退"会把 core 换到一个起不来的后端上。 |
| `SW_GENERATE_ENABLED` | `--value <true\|false>`。无闸门（但它只停出稿、不停发布）。 |
| `SW_TELEGRAM_ENABLED` | `--value <true\|false>`。`false` 方向有事前闸门：真发布正开着时拒绝（别拆掉确认卡载体）。 |
| `WECHAT_AUTO_PUBLISH` | `--value <true\|false>`。`true` 方向有事前闸门：`WECHAT_CERTIFIED` 必须已经是 `true`。 |
| `WECHAT_CERTIFIED` | `--value <true\|false>`。记的是**微信那边的事实**，本工具面核实不了，所以 `true` 方向那道闸门**从不拒绝**——它只把当场后果讲清（`WECHAT_AUTO_PUBLISH` 已是 `true` 时这一个写入就让平台级自动发布成立）并禁掉 `--write-only`。填错的代价是 `freepublish` 报 48001，响、按条报，不会越权发出去。 |
| `DAILY_TOKEN_BUDGET` / `DAILY_RENDER_SECONDS_BUDGET` / `DAILY_IMAGE_BUDGET` | `--value <非负整数>`。无闸门。**注意 `0` 是"当天全停"，不是"不限"**；本仓没有"不限"这个语义。 |
| 其余所有键（`TELEGRAM_CHAT_ID`、`SW_IMAGEGEN_*`、`SW_DSH_*`、`ANTHROPIC_API_KEY` 等） | **仍然没有合规路径**。白名单**写死在 `env_set.sh` 里、不接受运行时扩展**；红线 R3 又不允许手工 ssh 上去改。**这里刻意不写键数**（数字写在这、被数的东西在脚本里，扩容时必然对不上），当前名单以 `sed -n 's/^SW_ENV_WHITELIST="\(.*\)"$/\1/p' scripts/ops/env_set.sh` 为准。凭据类键不再是一律不加——签名密钥回落链那三级（`SW_UI_TOKEN` / `SW_TELEGRAM_SIGNING_SECRET` / `TELEGRAM_BOT_TOKEN`）已经在上面各占一行，2026-08-26 又加了 `DEEPSEEK_API_KEY`（它不在那条链上，靠自己那道 `llm_key_live` 闸门够格：一把 key 能不能用，`GET /models` 就问得出来）；**`TELEGRAM_CHAT_ID` 是有理由地不加**：它的闸门要问"新会话真的收得到卡吗"，不真发一条 Telegram 消息就验不了，而它的值又是**从生产流出来**的（要在服务器上跑 `core.telegram setup` 才知道），现有的按键表模型不了这个方向。要改这些键，得先给它们做工具面——如实记在这里，不要私自绕过。这条缺口仍登记为 `docs/RISKS.md` 第 14 条（含出路与复核方式）。 |

`env_set.sh` 那条路一次做完：本机校验 → 一次 SSH（远端再校验 → **备份 `.env`** →
**原子写入** → `docker compose up -d --force-recreate --no-build core`）→ 调
`scripts/ops/restart.sh` 走 **R1 红线闸门**；闸门不过则整条命令 fail-closed 收尾。
`--write-only` 只改 `.env`、不重建容器，也就是**变更不会生效**；**任何会触发事前闸门的方向
都禁用它**（上表里写了"有事前闸门"的那几格）。没有闸门的方向照旧允许。
**三个凭据类键因此一律不能 `--write-only`**——`signing_secret` 那道闸门没有方向可言、无条件
点亮。这条在 `TELEGRAM_BOT_TOKEN` 上尤其要紧：签发方一发新值、**旧 token 当场作废**，
"`.env` 已改、容器没重建"在它身上不是"上了膛没击发"，是**当场哑火**——运行中的 core 攥着一个
已经失效的凭据，一张卡都推不出去，而 `polling` 那一格还报 `true`。

**闸门现在是两道，不是一道**：事前预防在**写 `.env` 之前**判（判红时 `.env` 一个字节都没动、
连备份都没建），事后检测在重建容器之后由 `restart.sh` 判。事前拦住的是可预见的那一半，
事后兜住的是"写入到生效之间"发生变化的那一半，**谁也不替换谁**。各自的退出码与判据见
`scripts/ops/README.md`「改生产 `.env`」一节。

**两条必须知道的局限**（详见 `docs/RISKS.md` §12.3）：事后那道仍然是**事后检测**——它触发时，
带着新值的 core 已经在跑了；**手工绕过的路一个字节都没被堵**——直接 ssh 上去改 `.env` 再
`docker compose up -d`，不会触发任何闸门。`env_set.sh` 给的是一条合规的路，不是一道强制手段。

## 7. 成本闸门调整

三个闸门，都按 **UTC 自然日**统计（`Asia/Shanghai` 的号看到的是"早上 8 点回血"），
`/stats` 能看当日用量。**这是有意的**：成本是一台 core 服务的开销，与账号时区无关——
别把它改成账号本地日，理由与其它两种"今天"的分工见 [1.5 三个"今天"](#三个今天)。

| 闸门 | 环境变量 | 默认 | 超限时 |
|---|---|---|---|
| Claude token | `DAILY_TOKEN_BUDGET` | 2,000,000 | 生成链抛 `BudgetExhausted` → 降级为"只出选题不出稿"并通知；`tick_generate` 记 `skipped_budget` |
| 视频渲染秒数 | `DAILY_RENDER_SECONDS_BUDGET` | 3600 | 提交前按估算值查余额，不够就**不提交**（MPT 一开跑就是几分钟 CPU + 素材源配额）|
| 生图张数 | `DAILY_IMAGE_BUDGET` | 40 | 请求发出**之前**查余额，不够就不发（一分钱不花）；这条稿降级成没有配图 + warning，**不阻塞出稿** |

生图刻意按**张**而不是按 token 计：它走的是独立的 key 与计价口径，混进 `tokens` 会让写稿的
预算被配图悄悄吃掉。模型上报的 token 用量作为观测字段留在 `cost_ledger.meta` 里
（`input_tokens` / `output_tokens` / `total_tokens`），要按 token 对账时从那里取。

怎么调：

1. 先看 `/stats` 的「今日成本」和账号表的 `成本(tokens)` 列——**按账号归集**能看出是谁在烧；
2. 算清楚一条稿大概多少 token：`select kind, sum(amount) from cost_ledger where day='YYYY-MM-DD' group by kind;`
   除以当天生成条数；
3. 目标产能 × 单条成本 × 1.5（重试与改稿的余量）= 新的 `DAILY_TOKEN_BUDGET`；
4. 改 `.env` 让新值生效（settings 是进程内缓存的）：**本地重启进程即可，Compose 生产必须
   重建容器**。生产上走
   `bash scripts/ops/env_set.sh --key DAILY_TOKEN_BUDGET --value <非负整数>`——它会备份、
   原子写入、重建容器让新值生效。**`0` 是"当天全停"，不是"不限"**（`core/budget.py:118-122`
   的 `max(limit - used, 0)`），负数同理，取值校验会直接拒掉。见 6.7.1。

想临时刹车而不改预算：把 `SW_GENERATE_ENABLED=false`，生成停但发布照常
（已经在队列里的内容还会正常发出去）。

账本是**只追加**的 `cost_ledger` 表，不会自动清理。想看趋势：

```sql
select day, kind, round(sum(amount)) from cost_ledger group by day, kind order by day desc limit 30;
```

## 7.2 生图配图（P11）

内容配图走生图模型（默认 `gpt-image-2`），产出真实照片质感的图。三个平台的用法不同：

| 平台 | 用途 | 请求尺寸 | 最终尺寸 |
|---|---|---|---|
| 小红书 | 文字卡**之后**追加配图（封面仍是标题卡） | 1024×1536 | 比例合规就原样用，不合规才居中裁到 1242×1656 |
| 公众号 | 题图底图，标题仍由模板排版 | 1536×1024 | 900×383 / 900×900（模板 `background-size: cover` 保证） |
| 抖音 | 竖版封面底图 | 1024×1536 | 1080×1920（同上） |

### 7.2.1 配置

```dotenv
SW_IMAGEGEN_API_KEY=sk-…      # 生图**专用** key，与聊天 key 分开（网关按 key 分组授权）
SW_IMAGEGEN_MODEL=gpt-image-2 # 代码默认就是它；gpt-image-1 / 1.5 可作回退
SW_IMAGEGEN_BASE_URL=         # 留空 = 复用 SW_DSH_DEEPSEEK_BASE_URL 那条网关
SW_IMAGEGEN_ENABLED=auto      # auto（默认）/ true / false
SW_GENERATE_ILLUSTRATIONS=2   # 自动出稿每条配几张；0 = 自动出稿一律不配图
DAILY_IMAGE_BUDGET=40         # 每日张数上限
```

`auto` 的含义：**启动不探测**（探一次就是一张图的钱），首次调用失败就在**本进程内**标记不可用，
后续调用直接跳过，不反复烧钱试。改完配置**重启 core** 才会重新尝试（Compose 生产上光
`restart` 读不到新的 `.env`，要重建容器，见 6.7.1）。

### 7.2.2 权限没开怎么办

这把 key 能聊天不等于能生图——网关上图像生成是**单独的开关**。没开时调用返回
`permission_error`，本项目把它单独识别成 `ImagegenNotEnabled`：

> 在 Sub2API 后台给这把 key 所在的分组开启「图像生成」权限（与聊天权限是两个开关）

开完之后重启 core（清掉进程内的熔断标记），再跑一次带真探的门禁确认：

```console
$ SW_PREFLIGHT_IMAGEGEN=true uv run python scripts/preflight.py    # 会真生成一张图，要花钱
```

默认 `SW_PREFLIGHT_IMAGEGEN=false`：门禁在 CI 和每次开工都会跑，不能每次都烧一张图。

### 7.2.3 `size` 参数这台网关不认，形状要写进 prompt（实测）

**`size` 发了也白发**。2026-08-24 复验（真调上游，量 PNG IHDR）：

| 送出去的 | 实返 | 比例 |
|---|---|---|
| 裸 prompt + `size=1024x1536` | 1122×1402 / 1254×1254（两次不一样） | 0.800 / 1.000 |
| 裸 prompt + `size=1536x1024` | 1254×1254 | 1.000 |
| prompt 前置 `…3:4 aspect ratio (taller than wide). ` | 1086×1448 | 0.750 |
| prompt 前置 `…3:2 aspect ratio (wider than tall). ` | 1536×1024 | 1.500 |
| prompt 前置 `…9:16 aspect ratio (much taller than wide). ` | 941×1672 | 0.563 |

结论两条：`size` 不生效**而且不稳定**（同一条请求两次给不同形状）；
把画幅要求写进 prompt 前缀则几乎精确照做。所以生成侧的目标画幅统一走
`generation.imagegen.AspectSpec`——它同时发 `size`（换一台认它的网关时才不用改代码）
和 prompt 指令（对**这台**网关唯一有效的杠杆）。现有三档：

| 常量 | 用途 | 目标比例 |
|---|---|---|
| `ASPECT_PORTRAIT_3_4` | 小红书内页配图 | 0.750 |
| `ASPECT_LANDSCAPE_3_2` | 公众号题图（同时喂 900×383 与 900×900） | 1.500 |
| `ASPECT_VERTICAL_9_16` | 抖音封面 1080×1920 | 0.563 |

即便如此，客户端仍然一律读 **PNG IHDR 里的真实宽高**
（`review.inspect.read_image_size`）再决定裁不裁，绝不假设尺寸——指令是"很灵"，不是"保证"。
`cost_ledger.meta.actual_size` 与 `platform_extra.illustrations[].actual_size`
记的都是量出来的值，请求值另存在 `requested_size` 里做对照；
`platform_extra.illustrations[].prompt` 记的是**带指令的那条**，照着能复现同一张图。

指令失效是静默的（图只是形状不对，不报错），所以带真探的门禁会把实返比例和目标比例
一起打出来，偏差超过 0.15 就报 WARN。

### 7.2.4 常见现象

| 现象 | 原因 | 处置 |
|---|---|---|
| 出稿成功但 `illustrations: 0` | 权限没开 / 额度用完 / 网关抖动 | 看返回体的 `warnings`，里面是人话原因；**这是设计好的降级，不是故障** |
| 工作台配图开关是灰的 | `GET /api/v1/system/imagegen` 的 `ready=false` | 界面上直接写着 `reason` 与 `hint`，照着做 |
| 配图比例被裁 | 模型没照画幅指令出图，比例落在 `generation.pipeline.XHS_ILLUSTRATION_ASPECT_RANGE` 之外 | 正常：裁切是为了不让平台自己裁。warning 里写了裁成什么尺寸。这道闸门比审核侧的 `XHS_ASPECT_RANGE` 紧，因为竖版笔记不该配横图 |
| 日志里 `生图实际尺寸 X 与请求的 Y 不一致` | 网关行为，见 7.2.3 | 只是 info，不必处理 |

## 7.5 LLM 后端：deepseek-harness（dsh，P5）

默认后端仍是 Anthropic。dsh 是**可选**后端，把"思考"环节换成本机的 deepseek-harness
Agent runtime 子进程。`core/` 确定性控制面（状态机 / 调度 / 发布 / 审核 UI）不受影响。

### 7.5.1 安装与切换

```bash
# 1) 装依赖（会连带下 ~50MB 平台轮子，解包后约 192MB）
uv sync --extra render --extra douyin --extra dsh

# 2) 配 .env
SW_LLM_BACKEND=dsh
SW_DSH_PROVIDER=deepseek-official   # 必须是 configs/dsh/cordis.yml 里注册过的路由名
SW_DSH_MODEL=deepseek-v4-flash
DEEPSEEK_API_KEY=...                # 由该路由的 apiKeyEnv 决定，见下表

# 3) 门禁自检（会真起一次 runtime 完成握手）
uv run python scripts/preflight.py

# 4) 重启 core（settings 是进程内缓存的）
```

四条 provider 路由（都在 `configs/dsh/cordis.yml` 的 `dsh-llm-pi-ai` 段，文件里**只有**
`apiKeyEnv` 引用，没有任何密钥字面量）：

| `SW_DSH_PROVIDER` | 凭据环境变量 | 端点 | 备注 |
|---|---|---|---|
| `deepseek-official` | `DEEPSEEK_API_KEY` | `SW_DSH_DEEPSEEK_BASE_URL`（默认 `https://api.deepseek.com`） | 手工声明路由，模型 `deepseek-v4-flash` / `deepseek-v4-pro`。**只适合真官方端点** |
| `deepseek` | `DEEPSEEK_API_KEY` | 同上 | **`.env.example` 里的默认值**。与上一条同凭据同端点，区别在路由名让 pi-ai 认出 DeepSeek 方言（system 角色而非 `developer`、`max_tokens` 字段、`reasoning_content` 回填）；**挂私有网关（Sub2API 等）必须用这条**，用 `deepseek-official` 会被当成标准 OpenAI 端点发 `developer` 角色而 400（实测踩过） |
| `gateway` | `SW_DSH_GATEWAY_API_KEY` | `SW_DSH_GATEWAY_BASE_URL` | OpenAI 兼容自建中转；模型与容量由 `SW_DSH_GATEWAY_MODEL` / `_CONTEXT_WINDOW` / `_MAX_TOKENS` 声明 |
| `anthropic` | `ANTHROPIC_API_KEY` | pi-ai catalog 自带 | 回 Claude 的路，`SW_DSH_MODEL=claude-opus-5` 可直接用 |

**容器部署**：`Dockerfile` 默认只装主依赖（`uv sync --frozen --no-dev`），
镜像里**没有** dsh runtime。要在容器里用 dsh，把那行改成
`uv sync --frozen --no-dev --extra dsh` 并重建镜像；`configs/` 已经在 `COPY . .` 范围内，
会话目录记得挂到 `/app/data` 那个 volume 上（默认路径就在其中）。

其它可调项：`SW_DSH_CORDIS_PATH`（默认 `configs/dsh/cordis.yml`）、
`SW_DSH_SESSION_ROOT`（默认 `data/dsh_sessions`）、`SW_DSH_MAX_TOKENS`（0 = 沿用
`LLM_MAX_TOKENS` / `LLM_ARTICLE_MAX_TOKENS`；置成小于分档表某一档的值会让**截断自愈
加不动码**，见 `generation/output_budget.py`）、`SW_DSH_MAX_LIVE_RUNTIMES`（默认 4）、
`SW_DSH_PERSONA`（runtime 的进程级 persona）。

#### 7.5.1.1 按任务复杂度路由 GPT 模型

路由默认关闭；关闭时行为与旧版本完全相同，所有调用仍使用 `SW_DSH_MODEL` 和
`LLM_EFFORT`。启用后只有预算表已登记的生产 `purpose` 会分档，未知 purpose 仍回落旧配置：

| 复杂度 | 模型 / effort | purpose |
|---|---|---|
| complex | `SW_DSH_SOL_MODEL`（默认 `gpt-5.6-sol`）/ `xhigh` | `sourcing.select`、`review.semantic`、`metrics.insights` |
| medium | `SW_DSH_LUNA_MODEL`（默认 `gpt-5.6-luna`）/ `max` | 三个平台的角度、正文/脚本、润色与去 AI 味 |
| low | `SW_DSH_LUNA_MODEL`（默认 `gpt-5.6-luna`）/ `max` | 三个平台自检与公众号 meta |

**`medium` 与 `low` 现在是同一份运行时配置**（同模型、同 effort），只有档位标签不同：
标签仍然进账本的 `complexity` 字段，也仍是预算表（`generation/output_budget.py`）与
覆盖审计的锚点，所以两档没有被合并掉。**曾经的 `SW_DSH_TERRA_MODEL` 已移除**——
旧 `.env` 里留着它不会报错，但那一行已经是静默无效的键，请删掉。

```bash
SW_LLM_BACKEND=dsh
SW_DSH_MODEL_ROUTING=true
SW_DSH_PROVIDER=gateway
SW_DSH_SOL_MODEL=gpt-5.6-sol
SW_DSH_LUNA_MODEL=gpt-5.6-luna
SW_DSH_CORDIS_PATH=/absolute/path/to/zero-tool-gpt-cordis.yml
```

OpenAI 兼容 endpoint 的 base URL 必须以 `/v1/` 结尾。上表用到的模型必须全部声明在所选
provider 的外部 Cordis 组合里，且各自支持上表里那一档的 effort（Sol 要 `xhigh`，
Luna 要 `max`；两档共用 Luna 时要求自动合并成一条）；仓库自带组合不为
特定私有网关扩写模型表，也不要为此放松零工具设置。密钥只通过该 provider 的
`apiKeyEnv` 指向的环境变量注入，禁止写进 Cordis、`.env.example` 或日志。

启用后先跑 `uv run python scripts/preflight.py --offline`。即使机器没有 dsh SDK，静态的
零工具、provider、路由模型和 effort 审计也会执行；只有 runtime 握手会跳过。

#### 7.5.1.2 变量改名：`DSH_*` → `SW_DSH_*`（P15）

**症状**：对话台（sw-harness / `scripts/sw-web.sh`）在本仓库目录下起不来，报

```
dsh: /path/to/social_workflow/.env sets "DSH_PROVIDER", which only the launching
environment may set (…); export DSH_PROVIDER instead of putting it in a .env file
```

**原因**：dsh 的产品 CLI 启动时逐行读项目 `.env`，凡是名字落在 `DSH_` / `XDG_` /
`DYLD_` / `BASH_FUNC_` 前缀（或 `PATH` / `HTTP_PROXY` / `DEEPSEEK_BASE_URL` 等一张
精确名单）上的**一律抛错拒绝启动**——这些名字决定进程怎么起、代码与指令从哪儿加载、
怎么出网，所以只准由启动环境提供。这是供应链防护，**没有开关可关**
（`packages/boot/app-boot/src/index.ts` 的 `BOOTSTRAP_PREFIXES` / `isBootstrapOnly`）。

本项目那六个变量是自己的配置项，只是名字撞进了这道保留前缀，于是全部改名：

| 旧名（仍可用） | 现行名 |
|---|---|
| `DSH_PROVIDER` | `SW_DSH_PROVIDER` |
| `DSH_MODEL` | `SW_DSH_MODEL` |
| `DSH_CORDIS_PATH` | `SW_DSH_CORDIS_PATH` |
| `DSH_SESSION_ROOT` | `SW_DSH_SESSION_ROOT` |
| `DSH_MAX_TOKENS` | `SW_DSH_MAX_TOKENS` |
| `DSH_MAX_LIVE_RUNTIMES` | `SW_DSH_MAX_LIVE_RUNTIMES` |

**迁移**：旧名作为回退别名**仍然生效**（`core/config.py` 的 `AliasChoices`），读到旧名
会打一条 WARNING 日志并让 `preflight` 报 WARN。所以升级代码不必同步改 `.env`——
下次部署时把 `.env` 里那几行改名即可，改完 WARN 自动消失。**但只要 `.env` 里还留着任何
一行 `DSH_*`，对话台就仍然起不来**：那道墙看的是文件内容，不是本项目读不读得懂。

**不要改的两处**：`configs/dsh/cordis.yml` 里的 `process.env.DSH_SESSION_ROOT` 是 **dsh SDK
注入给 runtime 子进程的**变量（`deepseek_harness/api.py` 写的），不是本项目的 `.env` 变量，
改了会让会话日志落错地方。

**只影响对话台，不影响出稿**：`generation/llm_dsh.py` 起的 runtime 子进程走的是 SDK 的
`loadEnv`（不做任何名字校验），不是产品 CLI 的 `loadLayeredEnv`。所以 `SW_LLM_BACKEND=dsh`
的出稿链路在改名前后都能正常跑，这次改名对它是纯粹的无害重命名。

### 7.5.2 回退到 anthropic

改一行 `SW_LLM_BACKEND=anthropic`，再让 core 重读 `.env`（本地重启进程；Compose 生产要
重建容器，见 6.7.1）。**不需要卸载 dsh extra**，
也不需要改任何调用方代码——四个消费方只认 `SupportsLLM` 协议。
只要 `ANTHROPIC_API_KEY` 还在，回退是即时的。

生产上走工具面，不要手敲（红线 R3）：

```bash
bash scripts/ops/env_set.sh --key SW_LLM_BACKEND --value anthropic
```

它在写 `.env` **之前**会先核对**目标后端**的凭据在生产 `.env` 里存在且非空
（`anthropic` → `ANTHROPIC_API_KEY`；切回 `dsh` 则按 `.env` 的 `SW_DSH_PROVIDER` 经
`configs/dsh/cordis.yml` 的 `apiKeyEnv` 决定要哪一个），缺了就拒绝写入、生产一个字节不动。
这道闸门不是洁癖：`generation/llm.py:271-278` 是**懒加载**，缺 key 时 core 照常起来、直到
第一次真出稿才抛 `LLMUnavailable`——一次没凭据的"回退"不会当场报错，只会把故障推迟到排期里，
**比不回退更糟**。真被它拦下时，得先把那个 API key 写进生产 `.env`，而**那一步本工具面做不到**
（凭据类键不在白名单上，见 `docs/RISKS.md` 第 14 条）。

两个后端凭据都缺时，`/dev/*` 联调链路照旧回落 `ScriptedLLM`（非真实生成），
而复盘 Agent 依然**整体跳过**（假结论会污染后续每一次选题决策）。

### 7.5.3 零工具红线怎么验

模型能调用的工具必须为**零**。选题标题来自公开热榜，是不可信输入；
带 bash 的生成 Agent 等于给提示注入开了本机执行通道。三道验证，都能机械复现：

```bash
# ① 静态：审计仓库自带的受限组合（不起进程，跑在默认测试集里）
uv run pytest tests/generation/test_llm_dsh.py -q -k "zero_tools or audit or secret"

# ② 运行期 · 无需任何 API Key：真起 runtime，dump request/header 看工具清单
uv run pytest -m dsh_live -q -k "no_tools"

# ③ 整轮 · 无需任何 API Key：把 gateway 路由指到进程内假 OpenAI 端点，
#    检查真正发出去的 HTTP 请求体里连 tools 字段都不存在，并核对 usage 折算
uv run pytest -m dsh_live -q -k "local_gateway"
```

想亲手看一眼组合里挂了什么：

```bash
uv run python -c "
from generation.llm_dsh import load_composition, audit_composition
entries = load_composition('configs/dsh/cordis.yml')
for e in entries: print(f\"{e['id']:<20} {e['name']}\")
print('红线审计:', audit_composition(entries) or '通过（零工具）')"
```

`preflight` 也会把这条报成一项（`dsh 后端 零工具红线`）。

### 7.5.4 会话目录清理

每次 LLM 调用都会在 `data/dsh_sessions/` 下留一份 JSONL 会话日志（含完整 prompt 与
回复，排障和审计靠它回放）。目录**不会自动清理**，也**不进 git**（`data/` 已在
`.gitignore` 里）。日常维护：

```bash
du -sh data/dsh_sessions                              # 看体量
find data/dsh_sessions -type f -mtime +14 -delete     # 清 14 天前的
find data/dsh_sessions -type d -empty -delete
```

会话日志里**有原始 prompt 和模型输出**，和 `data/media` 一样按内部资料对待，
备份时要么一起加密，要么显式排除（见第 6 节）。

### 7.5.5 常见现象

| 现象 | 原因 | 处置 |
|---|---|---|
| `LLMUnavailable: dsh runtime 启动失败` | 没装 extra，或平台轮子与本机架构不匹配 | `uv sync --extra dsh`；macOS 需 arm64 轮子（含 `-spawn-helper` 兄弟文件，缺了是硬启动错误） |
| `LLMUnavailable: dsh 请求失败[MISSING_CREDENTIAL]` | 所选路由的 `apiKeyEnv` 环境变量为空 | 按上表补齐；`preflight` 会提前报 FAIL |
| `LLMUnavailable: ... [UNKNOWN_MODEL]` | `SW_DSH_MODEL` 不在该路由声明的模型表里 | 改 `SW_DSH_MODEL`，或在 `cordis.yml` 的该路由 `models` 里补一条 |
| `LLMAPIError: dsh 结构化输出两次都不合格` | 模型没按注入的 JSON Schema 输出 | dsh 侧没有原生结构化输出通道，本就有失败概率；换更强的模型或提高 `LLM_EFFORT` |
| 成本流水 meta 里出现 `estimated: true` | provider 这轮没上报 usage，已按字符**保守高估**记账 | 不影响闸门生效；持续出现说明该 provider 不报 usage，要按估算值重新标定 `DAILY_TOKEN_BUDGET` |
| runtime 进程数比预期多 | `(model, effort, max_tokens)` 任一项不同就会分桶；分档路由会扩大组合数 | 正常。预算维度只有三档（`generation/output_budget.py`），但完整键的组合数可能超过默认池上限；由 `SW_DSH_MAX_LIVE_RUNTIMES` 封顶，超出按 LRU 关掉最久没用的。关闭路由时保持旧的单模型分桶语义 |
| `LLMAPIError: dsh 输出两次都被 max_tokens 截断且没有任何内容` | 抬一档预算重试后仍然零输出：思考阶段就把预算烧穿了 | 给该调用点提档（`CALL_SITE_BUDGETS`）或降 `LLM_EFFORT`；先跑 `preflight` 的"输出预算"一项看是哪些调用点长期贴边 |
| 日志出现 `输出预算不够，已加码重试` | 该调用点的预算偏紧，自愈生效但白烧了一次 token | 不阻断出稿。频繁出现就把这个 purpose 提到下一档 |
| 改了 `.env` 但行为没变 | settings 与 runtime 子进程都是进程内长驻的；**Compose 生产还多一层**：`.env` 是在容器**创建**时烘进环境的 | **本地开发**：重启进程即可（`uvicorn --reload` 只重建 lifespan，lifespan 收尾会关掉 runtime 池）。**Compose 生产**：`docker compose restart`（含 `scripts/ops/restart.sh`）**不生效**，必须重建容器——白名单里的键走 `bash scripts/ops/env_set.sh --key <键> ...`（**这里不写键数**，当前名单以 `sed -n 's/^SW_ENV_WHITELIST="\(.*\)"$/\1/p' scripts/ops/env_set.sh` 为准），名单外的键当前无合规路径。全文见 6.7.1「改了 `.env`：本地重启进程，Compose 生产必须重建容器」 |

## 7.6 发布前确认（Telegram，P12）

系统能全自动跑到"就等发"，但**发布前必须推消息给人、人点一下才真发**——这不是产品偏好，
是合规底线：小红书 2026-03-10 公告直接封禁"完全 AI 驱动、无人值守"的账号。见
`docs/POLICY.md`《为什么保留人工确认》。Telegram 是这道确认的**推送通道**，不是这道闸门本身：
就算完全不配 Telegram，工作台排期页上的「确认发布」按钮照样能用（走同一个后端函数
`core.confirm.confirm_item`），只是少了"手机上弹一条消息"的便利。

### 7.6.1 建 bot 与接通

1. 找 [@BotFather](https://t.me/BotFather) 发 `/newbot`，按提示起名字，拿到一串
   `123456:ABC-DEF…` 的 token。写进服务器的 `.env`：

   ```dotenv
   TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
   ```

   `.env` 必须是 600 权限，token **不入库、不进前端、不写日志明文**
   （日志与错误信息里一律经 `core.telegram.mask_token` 打码）。

2. 用你自己的 Telegram 给这个 bot 发一句 `/start`（私聊或把它拉进群都行），然后跑：

   ```bash
   uv run python -m core.telegram setup      # 默认等 60 秒，--wait 可调
   ```

   它会把收到消息的会话打出来，形如：

   ```
   把下面这行写进 .env：
   TELEGRAM_CHAT_ID=123456789    # your_name（private）
   ```

   复制那一行进 `.env`。这一步刻意**不确认游标**——不会吃掉一条更新，long polling
   照样能拿到它。

3. 打开总开关（改完怎么让它生效见本节末段）：

   ```dotenv
   SW_TELEGRAM_ENABLED=true
   ```

4. 验一遍（不发消息，纯探活）：

   ```bash
   uv run python -m core.telegram check
   ```

   最后一行"结论：可用"才算真的接通了；"只能收不能发"通常是缺 `chat_id`，"不可用"通常是缺 token。

5.（可选，但**强烈建议**）配一把签名密钥，确认卡才会带「确认发布」/「不发」按钮：

   ```dotenv
   SW_TELEGRAM_SIGNING_SECRET=<openssl rand -hex 24 的输出>
   ```

   没配也不是不能用——`sw_telegram_signing_secret` 留空时会依次回落到 `SW_UI_TOKEN`、
   bot token 本身；三个都空的话系统**只发纯文字提醒，不带按钮**，因为没有签名就没法
   验证"是真的点了那个按钮"，宁可不发按钮也不能让人伪造确认。

改完要让它生效：`.env` 与 runtime 子进程一样，都是进程内长驻读一次。**本地重启进程即可；
但本节这几行写的是服务器的 `.env`，Compose 生产 `restart`（含 `scripts/ops/restart.sh`）
不生效、必须重建容器**——而 `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` / `SW_TELEGRAM_*`
里，`SW_TELEGRAM_ENABLED` 在 `env_set.sh` 的白名单上（`--key SW_TELEGRAM_ENABLED
--value <true|false>`，关掉方向有事前闸门）；`TELEGRAM_BOT_TOKEN` 与
`SW_TELEGRAM_SIGNING_SECRET` **2026-08-23 也进了名单**（凭据类，值全程零回显、只走
`--from-credentials`，其中签名密钥还能 `--generate`、bot token 不能——它的值由 BotFather 签发；
两者与 `SW_UI_TOKEN` 共用同一道签名密钥轮换闸门）。**只剩 `TELEGRAM_CHAT_ID` 仍然没有合规
路径**：它的值是从生产流出来的（要在服务器上跑 `core.telegram setup` 才知道），而"新会话真的
收得到卡吗"这道闸门不真发一条 Telegram 消息就验不了。见 6.7.1「改了 `.env`：
本地重启进程，Compose 生产必须重建容器」。

### 7.6.2 每账号怎么开关

`autopilot` 与 `confirm_required` 是**账号级**策略（见 1.5 节表格），两个开关管两件不同的事：

- `autopilot`（默认 `false`）：机器审核干净的稿子自动批准并排期，省的是"人工审核"这一步。
- `confirm_required`（默认 `true`）：批准之后、真发之前还要不要人点一下，**独立于 `autopilot`**、
  没有旁路。哪怕某个账号打开了 `autopilot`，只要 `confirm_required` 还是 `true`，稿子照样会停在
  "等你确认"，`tick_scheduled_publish` 不会替它点这一下。

改的两条路，效果等价（都是"先台账、后库"）：

```yaml
# accounts.yaml
accounts:
  - id: xhs-demo-01
    platform: xhs
    autopilot: true          # 机审干净的稿子自动批准 + 排期 + 推确认卡
    confirm_required: true   # 但发布前仍然要人点一下（改成 false 才是真全自动，会撞合规红线）
    confirm_ttl_hours: 12    # 这个号缩短到 12 小时没人点就自动驳回
```

```console
$ curl -sX PATCH http://127.0.0.1:8000/api/v1/accounts/xhs-demo-01 \
    -H 'content-type: application/json' -d '{"autopilot": true}'
```

`confirm_ttl_hours` 只能写 `accounts.yaml`（PATCH 接口不支持，见 `docs/WORKBENCH_API.md` 6.8 节）。

### 7.6.3 群聊里用要多配一项

把 bot 拉进群比私聊多一层风险：群里谁都能看到确认卡、谁都能点按钮。`chat_id` 只能锁定
"推给哪个会话"，锁不住"这个会话里谁能点"——群成员的 `user_id` 与群本身的 `chat_id`是两个数。
必须再配：

```dotenv
SW_TELEGRAM_ALLOWED_USER_IDS=111111111,222222222   # 逗号分隔，留空 = 只认 chat_id 本人（私聊场景）
```

没配这个而 `chat_id` 又指向一个群时，`core/telegram.py` 的回调处理会**拒绝所有群成员的点击**
（宁可无人能点，也不能谁都能点）；对应的回归测试见
`tests/test_telegram.py::test_allowlisted_user_may_press_the_button_in_a_group` 与
`test_callback_from_another_user_in_the_right_chat_is_ignored`。

### 7.6.4 没人点会怎样：TTL 自动驳回

推了确认卡没人点不许无限堆积占着排期槽位。`tick_confirm_gate`（每分钟跑一次）按
`confirm_ttl_hours`（账号级，默认 24）巡检：

- 到期还没确认 → 自动驳回（`ContentItem.status → rejected`，`scheduled_at` 清空、槽位让出来）+
  发一条 `[确认超时]` 通知，Telegram 卡片本身也会被编辑成"已超时自动驳回"；
- 快到发布时刻前会**补推一次提醒**（`SW_CONFIRM_REMIND_MINUTES`，默认槽位前 30 分钟，只补一次）；
- TTL 从"第一次成功推送确认卡"起算；如果 Telegram 完全没配、一次都没推成功过，就从
  `scheduled_at`（排期时刻）起算——两条路都必须有出口，不能因为没配 Telegram 就永远堆着。

驳回后要重发，去工作台把内容改好重新走一遍人工审核（`review_link` 指向的地址），
不会自动重排。

### 7.6.5 常见现象

| 现象 | 原因 | 处置 |
|---|---|---|
| 系统页"提醒渠道"面板显示"还没有接提醒渠道" | 缺 `TELEGRAM_BOT_TOKEN` | 按 7.6.1 第 1 步配好 |
| 显示"提醒渠道关着" | `TELEGRAM_BOT_TOKEN` 配了但 `SW_TELEGRAM_ENABLED=false` | 改成 `true`。生产上走 `bash scripts/ops/env_set.sh --key SW_TELEGRAM_ENABLED --value true`（装回载体是安全方向，不设闸门；它会重建容器让新值生效） |
| 显示"不知道该推给谁" | 有 token 但没有人给 bot 发过 `/start` | 发一句 `/start`，跑 `core.telegram setup` 补 `TELEGRAM_CHAT_ID` |
| 显示"能推，但不带按钮" | 三个签名密钥来源都是空 | 配 `SW_TELEGRAM_SIGNING_SECRET`（或 `SW_UI_TOKEN`），再按 6.7.1 让新值生效：本地重启进程；Compose 生产要重建容器——`SW_UI_TOKEN` 这条走 `bash scripts/ops/env_set.sh --key SW_UI_TOKEN --from-credentials`（或 `--generate`），`SW_TELEGRAM_SIGNING_SECRET` **2026-08-23 起也有路了**：`--key SW_TELEGRAM_SIGNING_SECRET --generate`。它**没有免检方向**（它就是回落链第一级），每次都去读待人点的确认卡条数，有卡就拒绝。**在这一格上它偏保守**：判据 `counters.awaiting_confirm` 数的是"等人确认的**内容条数**"，不看这些条目的卡推出去没有；而三个来源都空时 `push_confirm_card` 根本不推卡（`core/telegram.py:583-588` 直接 `return None` 并记一行 error），也就没有任何一张已推出去的卡会因为这次设值而失效。所以真被拦住时，`--accept-breaking-pending-confirm-cards` 在这一格是诚实的答案；否则照常挑一个 0 条的时刻做。 |
| 显示"推得出去，但收不回来" | long polling 线程没起来 / 挂了 | 看 core 启动日志；这段时间工作台的兜底确认按钮仍可用 |
| 确认卡推送成功，但点按钮没反应 | 同上，或点的人不在 `SW_TELEGRAM_ALLOWED_USER_IDS` 里（群聊场景） | 检查轮询是否活着；群聊必配 7.6.3 那条 |
| 稿子一直卡在"等你确认"，`scheduled_publish` 报 `skipped_unconfirmed` | 预期行为，不是故障——confirm 闸门在生效 | 去工作台点「确认发布」，或等 TTL 自动驳回 |
| 以为打开 `autopilot` 就是全自动发布了 | `autopilot` 与 `confirm_required` 是两个独立开关（7.6.2） | 要真全自动只能账号级显式 `confirm_required: false`，这本身是一次要留痕的决定 |

## 7.7 对话台（已移除，2026-08-27）

「对话台」（hermes-agent 0.20.4 fork + Electron，`sw-hermes-desktop`）**已删除**，用户裁决"没用了"。
连同它一起去掉的：`scripts/chat_console.sh`、`tests/ops/test_chat_console.sh`、`.hermes.md`、
`ui/screenshots/chat/`、本机 profile `~/.hermes/profiles/sw/`。

代码没丢：fork 早已推到一个私有仓库（默认分支 `sw-desktop`），删除前核实过远端 HEAD 与
本地逐字符一致。删的东西另有一份本机备份，路径记在删除那次提交的说明里。

**操作工作台现在只有两条路**：浏览器工作台（§8）与工作台桌面版（`desktop/`，见其 README）。
MCP 工具面 `scripts/workbench_mcp.py` **保留** —— 它不属于对话台，任何 MCP 客户端都能接。

## 8. 本地工作台与隧道控制生产（P17.C）

拓扑改了：服务器只跑 core（`127.0.0.1:8000`，不绑公网口），完整的 Organic 工作台
（`ui/`）在本地跑，经 IAP SSH 隧道打服务器的数据面。UI 静态
产物不再往服务器上部署——本地就是 `next dev`，不需要 `pnpm build` / 挂
`/workbench`。

```
服务器（GCE，只有 IAP 能进）              本地（这台 Mac）
┌───────────────────────┐               ┌───────────────────────────────┐
│ core :8000（loopback）  │               │  scripts/workbench_local.sh    │
│ 不装 UI、无公网监听口     │   ssh -L      │    ├─ 隧道 127.0.0.1:18000 ──┐ │
│ SQLite / accounts.yaml │◀── (IAP) ─────│    │   → workbench-iap        │ │
└───────────────────────┘   隧道         │    │     → 服务器 :8000       │ │
                                          │    └─ cd ui && next dev :3210 │
                                          │       SW_CORE_ORIGIN=         │
                                          │       http://127.0.0.1:18000  │
                                          │       （/api/* /review/* 走   │
                                          │        next.config.ts 的 dev  │
                                          │        rewrites 转发）        │
                                          └───────────────────────────────┘
```

### 8.1 日常三步

```bash
# 1) 起隧道（幂等；已经起着就直接复用，不会重复起）
bash scripts/workbench_local.sh tunnel

# 2) 起工作台（内部会自己先做第 1 步，再校验 core 可达，再 cd ui && pnpm dev）
bash scripts/workbench_local.sh up
#    等价于：cd ui && SW_CORE_ORIGIN=http://127.0.0.1:18000 pnpm dev
#    打开 http://127.0.0.1:3210/workbench/
```

体检（不起任何进程，只查）：

```bash
bash scripts/workbench_local.sh doctor
```

收隧道（只杀本脚本自己起的那一条，按参数指纹认，不会误杀别的 ssh 转发）：

```bash
bash scripts/workbench_local.sh down
```

`up` 模式退出（Ctrl-C）只停 `pnpm dev`，**不会**顺手收隧道——工作台和别的隧道用户
（步骤 2、3）共用同一条隧道，任何一边退出都不该动它；隧道的生死只由
`tunnel` / `down` 显式控制。

端口可换：`SW_TUNNEL_PORT=<port>` 覆盖默认的 `18000`（`SW_CORE_ORIGIN` 会跟着
联动到新端口，不用手动同步）；ssh 别名可换：`SW_TUNNEL_SSH_ALIAS=<alias>`
（默认 `workbench-iap`，定义见 `~/.ssh/config`）。

### 8.2 MCP 客户端接生产：地址一处 + token 一处

对话台已于 2026-08-27 移除（见 7.7）。工具面 `scripts/workbench_mcp.py` 保留——它是个
stdio MCP server，任何 MCP 客户端都能接。要让它打生产（经隧道），客户端那边配两样：

1. **地址**：`SW_MCP_BASE_URL=http://127.0.0.1:18000`（隧道端口）。`workbench_mcp.py`
   读的是**它自己进程的环境变量**，不是起它那个 shell 的变量——客户端怎么把 env 传给
   子进程，看客户端自己的配置格式。
2. **（仅当生产 core 开了 `SW_UI_TOKEN`）token**：同样经环境变量传给该子进程。
   值从 `~/.dsh-sw/.credentials.yaml`（0600）取，**不写进任何配置文件**（红线 R5）。

漏第 1 条的症状是"工具面连不上工作台"，漏第 2 条是"工具面全 401"——两者长得像，根因不同。

### 8.3 安全边界

- **服务器 core 只绑 `127.0.0.1:8000`**，没有任何公网监听口；从公网直接连
  这台机器的 8000 端口应该连不通（连通了就是回归，得查）。
- **进服务器唯一路径是 IAP**：`~/.ssh/config` 里 `workbench-iap` 的
  `ProxyCommand` 用 `gcloud compute start-iap-tunnel`，走 GCP 身份认证，不是
  裸 SSH 公网暴露 22 端口。本地隧道只是把这条 IAP 转发的本地端接到
  `127.0.0.1:18000`，隧道断了/进程退出，这条路就彻底断了，没有旁路。
- **审计在 IAP 那一层**：谁在什么时候连过这台服务器，看的是 GCP 的 IAP 访问
  日志，不是应用层日志；本脚本不额外记录，也不打印/缓存任何凭据（ssh 走
  `~/.ssh/config` 里现成的 `IdentityFile`，gcloud 走它自己的登录态）。
- 工作台在本地是**普通用户进程**，没有比登录这台 Mac 更高的权限
  要求；真正的写操作（审核通过、发布、排期）仍然只经 core 的 `/api/v1`，
  该有的业务闸门（Telegram 确认、MCP elicitation）不因为"本地起的"而绕过。

### 8.4 故障速查

| 现象 | 原因 | 处置 |
|---|---|---|
| `workbench_local.sh tunnel` 卡住不返回 | IAP 首包慢，正常情况 5-10 秒；也可能是 `gcloud auth login` 过期 | 等到 20 秒（脚本的 `ConnectTimeout`）；超时会明确报错，再看是不是要重新 `gcloud auth login` |
| `doctor` 里 ssh 别名解析失败 | `~/.ssh/config` 没有 `workbench-iap`（或改过别名没同步） | 核对 `~/.ssh/config` 的 `Host workbench-iap` 段；换别名用 `SW_TUNNEL_SSH_ALIAS` |
| `doctor` / `up` 里连不上 `core`（`/api/v1/system/info` 超时或拒绝） | 隧道没起；或隧道起了但服务器上 core 没跑 | 先 `bash scripts/workbench_local.sh doctor` 看端口是不是本脚本的隧道占着；core 有没有起要找主控确认（服务器侧不归本脚本管） |
| 端口 `18000` 被占，`tunnel` 报"不是本脚本的隧道" | 端口被别的进程（可能是上一次没收干净的隧道，或别的 SSH 转发）占了 | `lsof -nP -iTCP:18000` 看清是谁；确认是自己的旧隧道就 `down`，否则换端口：`SW_TUNNEL_PORT=<port>` |
| 工作台页面一直转圈，`/api/*` 请求超时 | `SW_CORE_ORIGIN` 没指对（不是 `up` 起的，比如手动 `pnpm dev` 忘了带环境变量） | 用 `workbench_local.sh up`（会自动把 `SW_CORE_ORIGIN` 钉到隧道端口），不要绕过脚本手动起 |
| 本地 `pnpm dev` 报 node/pnpm 版本相关的诡异报错 | 版本漂移（这台机器装的 node/pnpm 和 CI/其他机器不一致） | `doctor` 会打印当前 `node --version` / `pnpm --version`；无 `engines` 字段锁版本时以团队约定为准，出问题先对齐版本再排查别的 |
| `down` 之后隧道端口还占着 | 进程没能在 5 秒内退出（少见，通常是 ssh 卡在等待网络） | 脚本会打印原 pid 并提示 `ps -p <pid>`；确认后手动 `kill -9` |

## 9. 待补

- [x] 公众号 `health()` 映射与 40164 排障（第 2/3 节）
- [x] 小红书 `health()` 巡检的实际调用与频率（第 2 节，`tick_login_health` 每 10 分钟）
- [x] 视频渲染 sidecar 运维：config.toml、素材源 key、时长预算、任务丢失（第 4.5 节）
- [x] 抖音 `health()` 巡检与登录续期（第 2 节，`tick_login_health` 抖音分支每 30 分钟）
- [x] 首次部署清单与账号台账同步（第 1 / 1.5 节，P4）
- [x] 全链路定时调度、限频与发布时段窗口、重试与死信（第 1.6 节，P4）
- [x] 热榜采集 sidecar、复盘 Agent、统计页（第 4.6 / 4.7 节，P4）
- [x] 连续运行验证、sidecar 升级、成本闸门调整（第 5.5 / 6.5 / 7 节，P4）
- [x] dsh 后端安装 / 切换 / 回退、零工具验证、会话目录清理（第 7.5 节，P5）
- [x] Telegram 提醒渠道接通、每账号 autopilot / confirm_required 开关、TTL 自动驳回（第 7.6 节，P12）
- [x] 真实发布的回滚 / 撤稿手册（第 3.5 节；三平台均无封装的撤稿接口，撤稿是人工动作，
      本地状态如何收敛见 3.5.3）
- [x] 指标采集失败的公平补采策略（P22：明确不可用/格式错误不落快照、不覆盖窗口；
      记录最近内容尝试并按最久未尝试排序，同一 UTC 桶不重复尝试，见 `metrics/README.md`）
- [ ] 多账号并发调度的限频参数**实测**值（现在的窗口 / 间隔是拍脑袋的保守值，
      要用真号跑满一周才知道平台侧会不会限流）
- [ ] 日志采集与告警分级（通知通道现在是"飞书 + Telegram + 日志兜底，有几个用几个"，
      见 `core/notify.py:build_notifier`（195 行起）与 `scripts/preflight.py:check_notifier`
      （711 行起，两条通道都会体检）——不是只有飞书一个；级别仍然只有
      `LEVELS = ("info", "warning", "error")`（`core/notify.py:27`）三档，
      没有更细的分级或采集聚合，这部分待办依旧成立）
- [ ] 多进程部署时的调度器选主（**已有护栏**：`scripts/ops/verify.sh` 第九道门禁固定匹配
      `error_code=409`（`grep -c -F 'error_code=409'`），能发现"多个进程/多套部署争抢同一个
      Telegram bot token"这类冲突——`docs/RISKS.md` 第 1 条的双轮询事故就是靠这个信号定位
      的；但**这是事后发现，不是预防**，且 `verify.sh` 没有自动调用方（见
      `docs/RISKS.md` 第 12 条事实链第 1 条），只在人手动跑时才生效。**这道护栏覆盖不到的
      情形**：同一台机器、同一个数据库上起多个 core 进程互抢调度——这不是当前部署形态
      （生产是单容器，`docker-compose.yml` 的 `core` 服务没有多副本配置），而且基于数据库
      的选主/单例锁本来就查不出跨部署冲突：第 1 条那次事故的两套部署（`workbench-iap` 上的
      当前生产与另一台自有主机上的 P8 世代旧部署）**各有各的数据库**——`docs/RISKS.md` §5.2b
      记着 p3 的 `cost_ledger` 共 12 条，是那台机器本机独立的库，与生产不共享——连库都不
      共享就抢不到同一把锁，一把基于 DB 的锁救不了那次冲突。现在仍然靠"只起一个"的人肉
      约定，见 1.6）
- 遗留风险的单一真相登记册见 `docs/RISKS.md`（Windows 安装器实机验证、`SW_UI_TOKEN`
      鉴权加固、S0 密钥轮换等，含定性/状态/处置/复核方式；条目清单以该文件顶部总表为准）

### 8.5 生产台账与 demo 数据边界(2026-08-19 清理后的现状)

- **生产台账**在服务器数据卷:`/app/data/accounts.yaml`(`.env` 里 `SW_ACCOUNTS_FILE` 指过去),初始为合法空台账。仓库根的 `accounts.yaml` 从此只是 **demo/开发台账**,不再进入生产——compose 里那行 `./accounts.yaml:/app/accounts.yaml` 挂载仍在但已被 env 越过。
- **上真实账号**:直接用工作台「建号向导」——它会把账号写进生产台账并同步入库;或手动改 `/app/data/accounts.yaml` 后 `docker compose exec core python -m core.accounts sync`。
- **demo 数据备份**(删除于 2026-08-19,如需回看):服务器 `~/sw-demo-backup.db` 与卷内 `/app/data/backup-demo-*.db`;本机 `~/sw-demo-data-backup-20260819.tar.gz`(含本地 data/ 与 worktree data/)。
- 真发布前还差:把 `SW_USE_FAKE_PUBLISHERS` 从 `true` 翻成 `false`、接 sidecar 与账号登录(见 §1 与 §2)。
  翻开关这一步现在有合规且带闸门的执行路径:

  ```bash
  bash scripts/ops/env_set.sh --key SW_USE_FAKE_PUBLISHERS --value false
  ```

  它一次做完:**先探人工确认闸门通道(事前预防)** → 备份 `.env` → 原子写入 →
  `docker compose up -d --force-recreate --no-build core` 重建容器让新值生效(`restart` 不行,
  见 6.7.1) → 调 `scripts/ops/restart.sh` 走 R1 红线闸门(事后检测),闸门不过则整条命令
  fail-closed 收尾。**闸门是两道,不是一道**:事前那道在**写 `.env` 之前**判,判红时 `.env`
  一个字节都不动、连备份都不建;事后那道兜住"写入到生效之间"发生变化的那一半。
  **两条局限**(`docs/RISKS.md` §12.3 已写明):事后那道仍然是**事后检测**——它触发时,带着
  `false` 的 core 已经在跑了;**手工绕过的路一个字节都没被堵**——直接 ssh 上去改 `.env` 再
  `docker compose up -d`,不会触发任何闸门。无论走哪条路,做完都仍应跑一次
  `bash scripts/ops/verify.sh` 取证。
