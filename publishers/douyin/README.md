# publishers/douyin（P3）

抖音发布器。分成两半：**core 侧的薄客户端** + **宿主机上的有头浏览器常驻进程**。

```
core（可能在 Docker 里）                    宿主机（macOS 本机，有图形界面）
┌────────────────────────┐   本地 HTTP   ┌──────────────────────────────────┐
│ publisher.py  client.py│ ────────────▶ │ python -m publishers.douyin serve│
│ 实现 P0 冻结契约        │  :8710        │ service.py + Patchright 有头浏览器│
│ 不 import patchright   │               │ 一号一 profile，长期保活          │
└────────────────────────┘               └──────────────────────────────────┘
```

**为什么必须拆**：抖音没有面向个人/小团队的官方发布 API（`video.create.bind` 仅限
党政/事业单位），只能走浏览器自动化；而抖音会检测 headless，发布时还会触发短信二次验证，
两者都要求"真人在场的有头窗口"。容器里没有图形界面，所以浏览器这一层必须留在宿主机。

---

## 🚨 红线声明（`docs/POLICY.md`，审计逐条核）

| 做了什么 | 没做什么 |
|---|---|
| 用 Patchright（Apache-2.0）起**有头**浏览器，仅为让真人自己的账号不被 headless 误杀 | ❌ 任何指纹伪装：不设 `user_agent`、不加 `args`、不注入 init script、不改 navigator 属性 |
| 一个真人账号一个 `profile_dir`（`profiles/douyin/<account_id>/`），长期保活 | ❌ Cookie 池 / 账号池 / 多账号共用 profile。代码里**没有**复用他人 profile 的入口 |
| 遇到验证码就**停下来**，把状态回给 core，等真人在宿主机窗口里处理 | ❌ 打码平台、OCR、模型识别——仓库里没有任何验证码识别代码或依赖 |
| `/sms_code` 把**真人自己手机收到**的验证码填进输入框 | ❌ 自动获取验证码、自动过风控 |
| 日 ≤ 2 条（`DAILY_LIMIT_CEILING=10` 硬顶）+ 两次发布间隔 ≥ 30 分钟 | ❌ 绕过限频、并发发布。所有浏览器操作在**一个 worker 线程**里串行 |
| 发布前读页面昵称与 `identity_hint` 比对，不符直接中止 | ❌ 不做任何"猜是哪个号"的兜底 |
| 参考 `dreammis/social-auto-upload` 的**流程**后自行实现 | ❌ 未复制其任何代码（该仓库无 License，见 `docs/THIRD_PARTY.md`） |

代码里可被审计验证的点：

- `service.py:BrowserPool.context()` 只传 `user_data_dir` / `channel` / `headless=False`，
  且 `headless` **写死**没有开关；有回归测试
  `test_browser_pool_launch_is_headful_and_carries_no_stealth` 断言参数集合。
- `grep -rn "ddddocr\|captcha_solver\|2captcha\|anti_captcha\|user_agent=" publishers/douyin/` 为空。
  `打码` 只出现在注释里（要么是"绝不打码"的声明，要么指昵称**脱敏**打码）；
  `captcha` 只出现在"识别到拦截就上报给人"的检测逻辑里，没有任何求解代码。
- 限频：`publisher.py:_check_rate_limit`，日计数走共享 `RATE_LIMITER`（与调度器同一份），
  最小间隔走 `MIN_INTERVAL_GATE`。

---

## 1. 宿主机启动

```bash
# 只在宿主机上装浏览器层；core 容器 / CI 都不需要
uv sync --extra douyin
uv run patchright install chromium      # 用系统 Chrome（默认 channel）时可跳过

uv run python -m publishers.douyin serve --port 8710
```

默认只监听 `127.0.0.1`。core 也跑在容器里时，把 `DOUYIN_SERVICE_URL` 写成
`http://host.docker.internal:8710`（`docker-compose.yml` 已默认这么配）。

**不要**把它加进 docker-compose——容器里没有图形界面，抖音会当场识破。

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `DOUYIN_SERVICE_URL` | `http://127.0.0.1:8710` | core 侧调用地址（旧名 `DOUYIN_AGENT_BASE_URL` 仍兼容） |
| `DOUYIN_BROWSER_CHANNEL` | `chrome` | 优先系统 Chrome，起不来自动回退 patchright 自带 chromium |
| `DOUYIN_PROFILE_ROOT` | `profiles/douyin` | 一号一目录 |
| `DOUYIN_SCREENSHOT_DIR` | `data/douyin/screenshots` | 失败截图 |
| `DOUYIN_SELECTORS_FILE` | 空 | 选择器覆盖表（见第 6 节） |
| `DOUYIN_DAILY_LIMIT` | `2` | 日上限兜底；硬顶 10 |
| `DOUYIN_MIN_INTERVAL_MINUTES` | `30` | 两次发布最小间隔 |
| `DOUYIN_MEDIA_LOCAL_DIR` / `DOUYIN_MEDIA_HOST_DIR` | 空 / 空 | core 在容器里时的路径映射；同机时留空 |

## 2. 首次登录

1. 在 `accounts.yaml` / DB 里给该账号配 **`extra.identity_hint`**（创作者中心显示的昵称）；
2. 宿主机上启动上传器；
3. 打开 `http://localhost:8000/accounts/<id>/login`，点「打开宿主机登录窗口」；
4. **宿主机上会弹出一个真实的浏览器窗口**，用该账号本人的抖音 App 扫码；
5. 页面每 5 秒轮询一次状态，登录成功后账号自动回 `ok`，挂起的排期项自动放回。

profile 长期保活，正常几周才需要重来一次。

## 3. 短信验证码流程

```
抖音 →（短信）→ 你的手机
你 → core 的 /accounts/{id}/login 页面输入 → POST /accounts/{id}/login/code
core →（1）放进内存队列 core/sms_inbox.py
     →（2）转发 POST http://127.0.0.1:8710/accounts/{id}/sms_code
上传器 → 把那串数字 fill 进宿主机窗口里的输入框 → 点确认
```

- 验证码**不落库、不进日志**（客户端对该请求整体关掉了请求体日志，
  `redact()` 还会兜底抹掉日志里的裸数字串）；内存队列进程重启即丢，这是刻意设计。
- 转发失败（例如页面上现在根本没有验证码框）**不算错误**：接口照样返回 `ok`，
  `forward_detail` 里说明原因，队列里那份仍然有效。
- 发布过程中撞上验证码：上传器返回 `needs_sms` / `needs_captcha_by_human`，
  发布器抛 `NeedsReloginError` → 账号进 `needs_relogin` → 挂起该账号排期 → 通知人处理。
  **图形/滑块验证只能人在宿主机窗口里自己拖**，系统不碰。

## 4. 契约实现

| 方法 | 行为 |
|---|---|
| `prepare` | 标题 ≤ 30 字；**恰好 1 个** `.mp4` 成片且文件存在；封面可选（jpg/png）；话题去 `#`、去空格、保序去重、截到 5 个；简介截到 1000 字；`schedule_at` 必须在 2h–14d 内。全部是确定性变换，**幂等** |
| `publish` | 调上传器 → 按 `state` 分流：`published/scheduled` 成功；`needs_sms`/`needs_captcha_by_human`/`logged_out` → `NeedsReloginError`；`identity_mismatch`/`invalid_content`/`rejected` → `PermanentError`；`busy`/`timeout`/`browser_error` → `RetryableError` |
| `reconcile` | 拉内容管理页最近 20 条，按**归一化标题 + 24h 时间窗**命中 |
| `health` | 上传器可达 + 登录态 + identity 核对 |
| `fetch_metrics` | 数据中心的播放/点赞/评论/分享（**尽力而为**，读不到就 `available=false`，绝不伪造 0） |
| `fetch_metrics_for_title` | 可选能力：按标题兜底找回作品 id（见下） |
| `dry_run` | 一个请求都不发，只做本地校验 |

### 拿不到作品 id 怎么办

抖音**没有幂等发布接口**。发布成功但没能从内容管理页解析出 `aweme_id` 时，
发布器记一个占位 id `douyin-unresolved-<hash>`（定时的是 `douyin-scheduled-<hash>`），
两阶段记录照样落 `done`——**绝不重发**，因为"重复发一条真视频"比"少一条指标"严重得多。
下一轮 `tick_metrics` 会用 `fetch_metrics_for_title` 按标题兜底解析，命中后把真 id
回填进 `publish_records`（且不改 `updated_at`，24h/7d 窗口不受影响）。
占位 id 由 `publishers.base.is_placeholder_post_id` 跨平台识别。

### identity 校验（防发错号）

两道：

1. **硬闸门**（上传器侧）：发布前读页面昵称，与请求里的 `identity_hint` 不一致
   → 立刻截图 + 返回 `identity_mismatch` → core 侧 `PermanentError`，直接进死信。
   读不到昵称也算不一致（宁可停下也不能瞎发）。
2. **软告警**（`health()`）：巡检时发现对不上 → 账号置 `degraded` + 醒目 detail。
   **刻意不置 `banned`**：`banned` 是人工终态，状态机不允许自动恢复，
   靠一次昵称读取就把账号钉死代价太大。

没配 `identity_hint` 时：发布放行但记 warning，`health()` 返回 `degraded` 催你去配。

## 5. 限频

| 维度 | 值 | 在哪 |
|---|---|---|
| 日上限 | `Account.daily_limit`，兜底 `DOUYIN_DAILY_LIMIT=2`，**硬顶 10** | 共享 `core.scheduler.RATE_LIMITER`（与调度器同一份计数，`token=douyin:<内容项 id>` 去重） |
| 最小间隔 | 30 分钟（`DOUYIN_MIN_INTERVAL_MINUTES`） | `publisher.MIN_INTERVAL_GATE`（全局的 `SW_MIN_PUBLISH_INTERVAL_SECONDS` 默认才 15 分钟，对抖音太松） |
| 并发 | **1**（进程内 worker 线程串行） | `service.BrowserWorker`，忙时直接回 `busy` 让 core 重试 |
| 登录巡检 | 30 分钟（`DOUYIN_LOGIN_HEALTH_INTERVAL_MINUTES`） | `core.scheduler.tick_login_health` 里的抖音分支 |

`docs/POLICY.md` 的口径是"抖音日 ≤ 2"，任务书写的是"默认 ≤ 10"，**取严**：默认 2、硬顶 10。

## 6. 选择器与失败截图

`service.py:SELECTORS` 里的 CSS 依据 **2026-08 抖音创作者中心页面观察**写成，
**未在真实站点验证，平台改版即失效**。每个动作给了多个候选，按顺序取第一个可见的。

线上失配时**不用改代码**：

```bash
cat > selectors.json <<'JSON'
{ "title_input": ["input[placeholder*='新的占位文案']"] }
JSON
DOUYIN_SELECTORS_FILE=selectors.json uv run python -m publishers.douyin serve
```

怎么知道该改哪个？看截图：

```
data/douyin/screenshots/<account_id>/<UTC时间戳>-<步骤>.png
```

步骤名就是失配点：`upload-input-missing` / `title-input-missing` /
`publish-button-missing` / `identity-mismatch` / `publish-blocked` / `publish-timeout` …
文件名里只有账号、步骤、时间戳，**不含内容标题**。

## 7. 目录与备份

| 路径 | 内容 | 进 git？ |
|---|---|---|
| `profiles/douyin/<account_id>/` | 浏览器 profile = 登录态 | ❌（`.gitignore`）**要备份**，丢了要重新扫码 |
| `data/douyin/screenshots/` | 失败截图 | ❌ 可随时删 |

**禁止**跨账号复制 profile 目录——那就是 Cookie 池。

## 8. 故障排查

| 现象 | 处置 |
|---|---|
| `连不上宿主机抖音上传器` | 上传器没起，或 core 在容器里却把地址写成了 `127.0.0.1`（要写 `host.docker.internal`） |
| 账号一直 `degraded`，detail 说"上传器不可用" | 同上。degraded **不会**挂起排期，修好即可 |
| 账号 `needs_relogin` | 去 `/accounts/{id}/login` 点按钮 → 宿主机窗口扫码 |
| `identity 不符` | 宿主机浏览器里登录的不是这个号，或 `identity_hint` 填错了。**先核对再改** |
| `state=browser_error`，detail 说选择器失配 | 看截图，改 `DOUYIN_SELECTORS_FILE` |
| `state=timeout`，"可能已经发出去了" | 别手动重发。core 重试前会先 `reconcile`，命中就不会重复发 |
| 内容有 `douyin-unresolved-*` 却没链接 | 占位 id，等下一次 `tick_metrics` 兜底回填 |
| 浏览器起不来 | `uv run patchright install chromium`，或把 `DOUYIN_BROWSER_CHANNEL` 置空用自带 chromium |

## 9. 测试

```bash
uv run pytest tests/publishers/test_douyin.py -q     # respx 打桩，不联网
uv run pytest tests/publishers/test_douyin.py -m browser -q   # 真实浏览器 + 本地假页面
uv run pytest tests/contract -q                      # 契约测试（douyin-stub / douyin-dryrun）
```

`-m browser` 那条用**真实 patchright/playwright** 驱动
`tests/fixtures/douyin/fake_creator_center.html`，验证 identity 闸门、截图落盘、
验证码只填不识别。缺浏览器自动 skip。它是**无头**跑的——那是一张本地静态 HTML，
与抖音无关；生产路径的 `headless=False` 由单独的红线回归测试守着。
