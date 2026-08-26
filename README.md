# social_workflow

三平台（小红书 / 抖音 / 微信公众号）自动化运营工作流：
**选题 → 内容生成 → 审核 → 排期 → 人工确认 → 发布 → 数据回流 → 复盘**。

定位是"**高度自动化 + 人工一键确认**"，不是无人值守：

- 小红书 2026-03-10 官方公告，完全 AI 驱动的账号会被直接封禁 → 内容先经人工审核
  `approved`，排期后发布前还要人再点一次确认（Telegram 或工作台，`confirmed_at`
  不为空才真发）——顶在"真的发出去"前面的是后面这道确认，不是审核那道。
- 小红书 / 抖音均无面向个人的官方发布 API → 只能浏览器自动化（持续技术债）。
- 公众号 2025-07 起回收未认证主体的 `freepublish` 权限 → 未认证号只落草稿箱，人工点发布。

当前进度：**P0 骨架 / P1 公众号 / P2 小红书 / P3 抖音 / P4 调度与加固 全部完成**。

---

## 快速开始

**五分钟走通三平台**（不需要任何真实凭据：没有 `ANTHROPIC_API_KEY` 时生成链自动
降级到 `ScriptedLLM`，`SW_USE_FAKE_PUBLISHERS=true` 时发布走 `FakePublisher`）：

```bash
# 1. 依赖（Python 3.12 + uv）
uv sync

# 2. 配置
cp .env.example .env      # 填 ANTHROPIC_API_KEY 等；凭据只放 .env，不入库

# 3. ★ 账号台账入库 ★ 幂等；不做的话所有 dev 端点都会说"账号不存在"
#    （P10 起也可以完全不碰终端：起完控制面在工作台的「账号 → 添加账号」里建，
#     它会回写 accounts.yaml 再同步进库，check 照样通过）
uv run python -m core.accounts sync
uv run python -m core.accounts list

# 4. 门禁自检（--offline 跳过所有网络检查）
#    没填 ANTHROPIC_API_KEY 时这里会有 1 条 FAIL 并说"门禁未通过"——那是**预期的**，
#    它拦的是"真实生成不可用"，不拦这套演练：下面几步照跑，生成链走 ScriptedLLM。
uv run python scripts/preflight.py --offline

# 5. 起控制面（默认同时起 APScheduler，见 SW_SCHEDULER_ENABLED）
uv run uvicorn core.main:app --port 8000 --reload

# 6. 三平台各跑一条到审核队列
#    topic 是中文，必须让 curl 自己编码（-G --data-urlencode）：直接把汉字写进 URL，
#    请求行里就带着裸的非 ASCII 字节，uvicorn 会以 "Invalid HTTP request received" 拒掉。
curl -sX POST -G localhost:8000/dev/run_wechat_pipeline \
  --data-urlencode 'account_id=wechat-demo-01' \
  --data-urlencode 'topic=通勤一小时的隐形成本'
curl -sX POST -G localhost:8000/dev/run_xhs_pipeline \
  --data-urlencode 'account_id=xhs-demo-01' \
  --data-urlencode 'topic=租房不打孔怎么收纳' --data-urlencode 'make_cards=false'
curl -sX POST -G localhost:8000/dev/run_douyin_pipeline \
  --data-urlencode 'account_id=douyin-demo-01' \
  --data-urlencode 'topic=通勤一小时的隐形成本' --data-urlencode 'make_cover=false'

# 7. 人工审核 → 批准
open http://localhost:8000/review

# 8. 让调度器把批准的内容发出去（也可以等定时任务）
curl -sX POST localhost:8000/dev/tick/scheduled_publish
open http://localhost:8000/stats
```

第 3 步的台账同步在 core 启动时也会自动跑一遍（`SW_SYNC_ACCOUNTS_ON_START=true`），
但命令行能看到"新建 / 更新 / 台账外"的明细，建议显式执行。
完整部署清单（含 sidecar 与真实账号登录）见 `docs/OPS.md` 第 1 节。

### 两种 LLM 后端

所有"思考"环节（选题打分 / 文案脚本 / 语义审核 / 复盘）都只依赖
`generation/llm.py` 的 `SupportsLLM` 协议，后端一个开关切换：

| `SW_LLM_BACKEND` | 实现 | 装法 | 凭据 |
|---|---|---|---|
| `anthropic`（默认） | 直连 Claude Messages API | `uv sync` | `ANTHROPIC_API_KEY` |
| `dsh` | 本机 [deepseek-harness](https://github.com/deepseek-ai/deepseek-harness) Agent runtime 子进程（MIT） | `uv sync --extra dsh` | 取决于 `SW_DSH_PROVIDER` 路由，见 `configs/dsh/cordis.yml` |

两者都缺凭据时照旧回落 `ScriptedLLM`，快速开始的六步链路仍然能跑通。

dsh 后端用的是本仓库自带的**受限 Cordis 组合**（`configs/dsh/cordis.yml`）：
**模型可调用的工具为零**——选题标题来自公开热榜，是不可信输入，带 bash 的生成
Agent 等于给提示注入开了本机执行通道。验证命令与运维口径见 `docs/OPS.md` 第 7.5 节，
接缝图见 `docs/ARCHITECTURE.md` 第 10 节。

### 定时调度

九个 tick 默认随 core 一起跑。每一个都能单独手动触发，**走的是同一批函数**：

```bash
curl -s  localhost:8000/dev/tick                    # 列出全部
curl -sX POST localhost:8000/dev/tick/sourcing      # 拉热榜
curl -sX POST localhost:8000/dev/tick/generate      # 按 daily_target 出稿
curl -sX POST localhost:8000/dev/tick/scheduled_publish
curl -sX POST localhost:8000/dev/tick/retry_sweep
curl -sX POST localhost:8000/dev/tick/metrics
curl -sX POST 'localhost:8000/dev/tick/insights?force=true'
uv run python -m core.scheduler                     # 或者单独起一个调度进程
```

### 端到端验收（一条命令）

「这套东西真的能无人值守跑通吗」不必读报告相信，自己跑一条命令：

```bash
uv run python scripts/acceptance_full_chain.py --offline   # 不打网络，ScriptedLLM
uv run python scripts/acceptance_full_chain.py             # 用真实 LLM（要 .env 里的 key）
```

它建两个隔离账号（临时库、临时媒体目录、`FakePublisher`、Telegram 关掉，**一个字节都不会
发到平台上**），然后只调真实的 scheduler tick 走一遍，中途不碰任何一条记录的状态：

- `accept-auto`（`confirm_required=false`）—— 证「**无干预**」：采集 → 出稿 → 机器审核 →
  autopilot 批准 → 排期 → 发布 → 回流 → `measured`。
- `accept-gated`（`confirm_required=true`，**生产的默认形态**）—— 证「红线 R1 还在」：
  同样自动走到 `scheduled` 然后**停住**，`skipped_unconfirmed` 记它一笔。

两半缺一不可：只判前一半的话，哪天谁把确认闸门关了，它照样全绿。

### 连续运行验证

```bash
uv run python scripts/soak.py     # 加速时钟模拟 3 天，1 秒内跑完，22 条硬断言
```

排期（`core/scheduling.py`，批准时算槽位）与发布（`tick_scheduled_publish`，发布时
再校验）是限频 / 时段窗口的两层防御，soak 两层分别验：排期层断言排出来的槽位都合法，
发布层则注入一批绕过排期层的脏排期（窗口外的、挤在同一分钟的），断言闸门仍然挡得住。
细节见 `docs/OPS.md` 5.5。

主要页面：

| 路径 | 说明 |
|---|---|
| `GET /health` | 存活 + DB 自检，DB 异常返回 503 |
| `GET /review` | 审核队列（默认只看 draft/reviewing/rejected，`?status=all` 看全部） |
| `GET /review/{id}` | 详情：正文预览、媒体缩略图占位、改动 diff、审计日志、发布记录 |
| `POST /review/{id}/approve\|reject\|edit` | 人工审核动作，全部写 `ReviewLog` |
| `GET /accounts` | 账号列表与健康状态 |
| `GET /accounts/{id}/login` | 扫码续期页（轮询 `/login/qrcode`）+ 短信验证码人工输入 |
| `GET /stats` · `GET /stats.json` | 按账号的近 7 天看板：发布/失败/死信、配额、指标、成本；`needs_relogin` 醒目提示。`?days=30` 可改窗口 |
| `GET /review/{id}/preview` | 公众号正文 `body_html` 原文，详情页用 sandbox iframe 嵌入 |
| `GET /review/{id}/cover` | 封面图原图（限工作目录内，防目录穿越） |
| `POST /dev/seed` | 开发用：注入 1 个 fake 账号 + 1 条 draft |
| `POST /dev/run_{wechat,xhs,douyin}_pipeline?account_id=` | 开发用：跑通 sourcing→selector→generation→review，产出一条待审内容 |
| `GET /dev/tick` · `POST /dev/tick/{name}` | 手动触发定时任务，与 APScheduler 共用同一批函数 |
| `POST /dev/sync_accounts` | 把 `accounts.yaml` 同步进 DB（等价 `python -m core.accounts sync`）|

## 目录

```
core/         控制面：models(SQLAlchemy) / state_machine(双状态机+幂等) /
              scheduling(批准即排期 + 窗口/限频校验) /
              accounts(台账同步 + 账号级调度策略) / accounts_file(台账保留注释的块级读写) /
              account_admin(账号增删改：先写台账再同步 DB) / sidecars(小红书 sidecar 生命周期) /
              ratelimit(限频，真相在 DB) /
              scheduler(九个 tick) / stats(统计聚合) / budget(成本闸门) /
              notify(飞书 / Telegram / 日志兜底，三路扇出) / sms_inbox(验证码人工通道) / main(FastAPI) /
              review_actions + login_flow + content_view(两套门面共用的业务编排) /
              api/(工作台 JSON API，/api/v1) / review_ui(Jinja2+HTMX) /
              dev_flow(全链路串联)
publishers/   base.py = P0 冻结契约（DTO/异常/ABC/FakePublisher）；registry.py 按平台取实现；
              wechat_mp(P1，官方 API 薄封装 + 双确认闸门 + wenyan 备选后端) /
              xhs(P2) / douyin(P3)
sourcing/     选题采集：newsnow / douyin_hot_hub / trendradar(GPL sidecar) +
              collector(统一入口) + dedupe(simhash+编辑距离+包含度) +
              selector(选题决策 Agent，读复盘结论)
generation/   llm(Anthropic 薄封装+预算记账) / wechat_article(去 AI 味五步 SOP) /
              wechat_render(wenyan-cli 子进程) / cover(Playwright 截图) / pipeline
review/       三级审核：lexicon(Aho-Corasick) → precheck(vendored 规则) →
              llm_semantic(语境判定) → inspect(发布前校验) → 人工卡点(core)
metrics/      collector(24h / 7d 指标快照，只追加) +
              insights(复盘 Agent：指标 → 结论 → 回灌选题，闭环)
prompts/      平台风格 / 去 AI 味 prompt 库 + 账号 persona.md 与 insights.md
sidecars/     各 sidecar 的 Dockerfile / 配置（不含其源码）
scripts/      preflight(门禁) / gen_xhs_sidecars(一账号一容器 compose) /
              soak(连续运行验证) / fetch_lexicon(敏感词库)
tests/        pytest；tests/contract/ 为所有 Publisher 必过的契约测试
docs/         ARCHITECTURE / POLICY(反检测红线) / THIRD_PARTY(License 台账) / OPS /
              WORKBENCH_API(前端工作台的 JSON 契约)
```

## 运行 / 测试命令

```bash
uv sync                                  # 安装依赖
uv run ruff check .                      # lint
uv run ruff format --check .             # 格式（可选）
uv run pytest -q                         # 全部测试（含契约测试）
uv run python scripts/preflight.py       # 门禁；--offline 跳过网络检查；--strict 把 WARN 当失败
uv run python scripts/gen_xhs_sidecars.py --accounts accounts.yaml   # 生成 docker-compose.xhs.yml
uv run python -m core.accounts sync      # 账号台账 → DB（首次部署第一步，幂等）
uv run python -m core.accounts check     # 台账与 DB 不一致时退出码 1
uv run python -m core.scheduler          # 单独起调度进程（--list / --once TICK）
uv run python scripts/soak.py            # 连续运行验证（加速时钟模拟 3 天）
uv run python scripts/fetch_lexicon.py   # 下敏感词库到 data/lexicon（审核第一级需要）
uv sync --extra render && uv run playwright install chromium   # 封面截图（可选）
uv sync --extra dsh                      # 可选：deepseek-harness Agent 后端（含 runtime 平台轮子）
uv run pytest -m live                    # 可选：真调 Anthropic API 的 smoke（会产生费用）
uv run pytest -m dsh_live                # 可选：真起 dsh runtime 的冒烟（零工具红线那条不需要任何 Key）
uv run uvicorn core.main:app --port 8000 # 起控制面（页面 / ；工作台 API /api/v1，见 docs/WORKBENCH_API.md）
docker compose config                    # 校验 compose 语法
docker compose up core                   # 容器方式起控制面
docker compose -f docker-compose.yml -f docker-compose.xhs.yml up    # 带小红书 sidecar
docker compose --profile video    up -d mpt          # 视频合成（先 cp config.toml）
docker compose --profile sourcing up -d trendradar   # 热榜聚合（先 cp config）
```

## 核心设计（P0 冻结，改动需评审）

### 发布契约 `publishers/base.py`

`Publisher.prepare / publish / health / fetch_metrics / reconcile` + `dry_run` 属性；
DTO `ContentBundle`（`content_hash` = sha256(标题+正文+媒体路径)）、`PublishResult`、`AccountHealth`；
异常分类 `PublishError → RetryableError / NeedsReloginError / PermanentError`。
**任何新平台实现都必须通过 `tests/contract/test_publisher_contract.py`**，
扩展只允许走 `platform_extra`，不得改既有字段语义。

### 双状态机 `core/state_machine.py`

```
ContentItem: topic → drafting → draft → reviewing → rejected
                                              ↘ approved → scheduled → publishing → published → measured
             异常:  publishing → publish_failed → retrying → publishing
                                              ↘ dead_letter
             挂起:  scheduled ⇄ suspended（账号 needs_relogin 时）
Account:     ok / degraded / needs_relogin / banned
```

非法迁移抛 `IllegalTransition`。登录过期是**账号级**事件，不挂在内容状态上。

### 幂等与安全阀

`idem_key = sha256(account_id | platform | content_hash | scheduled_slot)`，DB UNIQUE。
两阶段：发起前写 `in_flight` → 成功补 `platform_post_id/url`。
幂等键已存在时先调 `publisher.reconcile()` 做**平台侧对账**，防"发成功但回包丢失"重复发布。
`RetryableError` 计数超过 `SW_MAX_PUBLISH_ATTEMPTS` 进死信；`NeedsReloginError` 触发账号挂起且不计入重试上限。

### 调度与限频（P4，`core/scheduler.py` · `core/ratelimit.py` · `core/accounts.py`）

九个 tick（采集 / 生成 / 定时发布 / 人工确认 / 重试扫描 / 指标 / 登录巡检 / 渲染轮询 / 复盘），
全部幂等、全部可 `POST /dev/tick/{name}` 单独触发，和 APScheduler 共用同一份注册表。

`tick_scheduled_publish` 的六道闸门，顺序不可换：
**账号健康 → 发布时段窗口 → 限频 → 人工确认 → 发布器可用 → 幂等键 + 平台侧对账**。
每道各有一个 `skipped_*` 计数且都计入总 `skipped`，排查时一眼能看出被谁拦下，
`scanned == published + skipped + failed` 恒成立。逐道的触发条件见 `docs/OPS.md` 1.6。

限频的**真相在 DB**（`PublishRecord` 的当日 done 数与最近发布时刻），
进程内计数只是 30 秒 TTL 的缓存，合并策略 `max(DB, 本地)`——重启不清零。

账号级策略写在 `accounts.yaml`，`python -m core.accounts sync` 落进 `Account.extra`：
`daily_target`（每天出几条）、`publish_windows`（如 `["09:00-11:00"]`，按账号时区）、
`min_interval_minutes`（平台缺省值是**下限**，台账只能写得更严）、`timezone`。
同步**不覆盖** `Account.status`——那是登录巡检的地盘。

### 反馈闭环（P4，`metrics/insights.py`）

`MetricSnapshot(7d)` → Claude 结构化复盘 → `prompts/accounts/<id>/insights.md`
（追加，保留最近 N 条）→ `sourcing/selector.py` 选题时读回去。
写文件不写库：复盘结论和 `persona.md` 一样是人要看、要能手改、要进 git 的资产。
**无 API key 时整体跳过**，不回落 `ScriptedLLM`——假结论会持续污染后续每一次选题。

### 公众号发布（P1，`publishers/wechat_mp/`）

```bash
uv run pytest tests/publishers/test_wechat_mp.py -q   # 全程 respx 打桩，不联网
uv run python scripts/preflight.py                    # 有凭据时实际探测 stable_token / 40164
```

**双确认闸门**：`WECHAT_AUTO_PUBLISH`（服务端开关）+ `WECHAT_CERTIFIED`（账号认证）
+ 审核 UI 逐条写入的 `platform_extra.confirm_publish=True`，三者缺一就**只落草稿箱**。
未认证主体本来也只有草稿箱权限（2025-07 官方回收 freepublish）。
详见 `publishers/wechat_mp/README.md`，运维排障见 `docs/OPS.md` 第 3 节。

跑真实公众号前记得把 `SW_USE_FAKE_PUBLISHERS` 改成 `false`，否则会被 `FakePublisher` 顶掉。

## 故障排查

| 现象 | 排查 |
|---|---|
| `/health` 返回 503 | `checks.database` 看错误；确认 `SW_DATABASE_URL` 指向的目录可写 |
| `/accounts/{id}/login/qrcode` 返回 503 | 该平台发布器未注册；P0 需 `SW_USE_FAKE_PUBLISHERS=true` |
| 返回 501 | 该平台不支持扫码（公众号走官方 API，正常） |
| 审核批准报 409 | 状态机不允许该迁移，看返回体里的 `from → to` |
| 公众号 API errcode 40164 | 出口 IP 未加白名单；异常 detail 里直接带出口 IP，见 `docs/OPS.md` 第 3 节 |
| 公众号内容一直停在草稿 | 看 `PublishResult.raw.gate.blocked_by`，指出是三道闸门里的哪一道 |
| 公众号指标一直 `available=false` | 未认证号无 datacube 权限，或 post_id 映射不到 msgid，见 `metrics/README.md` |
| 发布反复进 `retrying` | 看 `/review/{id}` 的发布记录表 `last_error`；`dead_letter` 需人工介入 |
| 审核页样式正常但按钮无局部刷新 | HTMX CDN 不可达；所有操作仍以原生表单 POST 生效 |
| `/dev/run_wechat_pipeline` 返回 409 | 预期内：没配 `NEWSNOW_BASE_URL`（用 `?topic=` 手工指定）、预算耗尽、或选题 Agent 认为今天无题可写 |
| 返回体里 `llm: "scripted"` | 没配 `ANTHROPIC_API_KEY`，走的是预置内容，**不是真实生成** |
| 详情页提示"尚未渲染 body_html" | 没装 Node；`brew install node` 后重跑生成链 |
| 详情页没有封面 | 没装 Playwright：`uv sync --extra render && uv run playwright install chromium` |
| 审核 findings 里有 `lexicon.not_installed` | 敏感词库没下，跑 `uv run python scripts/fetch_lexicon.py` |
| 零凭据演练时小红书/抖音 `review.passed=false`（`blocking=1`） | 没配 `SW_IMAGEGEN_API_KEY`，这两个平台的稿子没有配图。**这是机器审核在正常工作**，稿子照样进了审核队列；配上生图 key（或回落的 `DEEPSEEK_API_KEY`）后就不再 block |
| **dev 端点报"账号不存在"** | 没跑 `uv run python -m core.accounts sync`（快速开始第 3 步）。preflight 的「台账已入库」会直接报 FAIL |
| **排期项一直不发** | `POST /dev/tick/scheduled_publish` 看返回的 `skipped_*`：`unconfirmed`=等人点「确认发布」（默认开启，**最常见**）、`account`=账号不健康、`window`=不在发布时段、`rate`=配额用完或没到间隔、`publisher`=发布器没注册、`not_advanced`=发布器返回了但状态没推进到 published（防御性守卫，正常恒为 0）；`scanned=0`=还没到 `scheduled_at`。详见 `docs/OPS.md` 1.6 |
| **一条稿都不生成** | `python -m core.accounts list` 看 `daily_target` 是不是 0；再看选题池空不空、token 预算是不是耗尽 |
| 复盘一直不出 | `POST /dev/tick/insights?force=true` 看返回：`skipped_no_key`=没配 key；`skipped_sample`=7 天内已发不足 3 条 |
| TrendRadar 报 404 | 容器刚起还没抓第一轮（默认 30 分钟）。设 `IMMEDIATE_RUN=true`，见 `docs/OPS.md` 4.6 |
| 用了 `NODE_BIN` 等老变量名 | 仍然生效，但 P4 已归并为 `WENYAN_NODE_BIN`；preflight 会 WARN 提示 |

## 红线

见 `docs/POLICY.md`。简述：**禁止**验证码自动识别/打码平台、Cookie 池、批量虚拟身份、
一机多号指纹隔离、绕过平台限频；Patchright 仅用于避免真人账号被 headless 误杀。
**生成 Agent 一律零工具**：选题标题来自公开热榜（不可信输入），带 bash/fs/web 的
Agent 等于给提示注入开了本机执行通道——dsh 后端的受限组合与三道验证见
`docs/OPS.md` 第 7.5.3 节。
依赖 License 白名单为 MIT/Apache/BSD/PSF/MPL，GPL/AGPL 只以独立进程（Docker sidecar）出现，
台账见 `docs/THIRD_PARTY.md`。

## 这份公开仓库里没有什么

代码、测试、提示词、工作台前端、运维脚本都是完整的；下面两处是有意留白的：

- `docs/RISKS.md` —— 遗留风险登记册。那是一台**在跑的**部署的运维安全档案（公网地址、
  尚未加固的位置、凭据轮换清单），公开等于交出攻击手册。技术结论已分散落在代码与
  常设文档里，对照表见 `docs/RISKS.md` 的占位说明。
- `docs/briefs/` —— P10–P27 的阶段任务书与验收报告（43 份）。本机绝对路径与一次性
  过程材料，结论已并入代码与 `docs/` 常设文档。见 `docs/briefs/README.md`。

凭据一律不入库：`.env` 在 `.gitignore` 里，模板是 `.env.example`；`accounts.yaml`
只记拓扑与限频，sidecar token 存的是**环境变量名**而不是值。

## License

MIT，见 `LICENSE`。第三方组件的许可证台账与素材使用限制见 `docs/THIRD_PARTY.md`。
