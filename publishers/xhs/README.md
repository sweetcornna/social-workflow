# publishers/xhs（P2）

小红书发布器。走 [`xpzouying/xiaohongshu-mcp`](https://github.com/xpzouying/xiaohongshu-mcp)
（Apache-2.0）sidecar 的 REST `/api/v1/*`，**一账号一容器**。
容器编排与首次登录见 [`sidecars/xhs/README.md`](../../sidecars/xhs/README.md)。

```
client.py     REST 薄封装 + 错误分类 + 宿主机→容器 素材路径映射 + 日志脱敏
publisher.py  XhsPublisher：P0 冻结契约 + 扫码登录通道 + 按账号限频
login.py      登录态巡检，把 health() 落到 Account 状态机
stub.py       不联网的测试替身客户端（契约测试用）
```

## 契约实现

`platform = "xhs"`，实现 `publishers.base.Publisher` + `SupportsInteractiveLogin`。

| 方法 | sidecar 接口 | 说明 |
|---|---|---|
| `prepare` | — | 标题 ≤20 字、图片 1–18 张且**文件存在**、标签去 `#` 去重、`schedule_at` 落在 1h–14d；把宿主机路径翻译成容器路径写进 `platform_extra.sidecar_images` |
| `publish` | `POST /api/v1/publish`（视频走 `/publish_video`） | 发完**立刻对账取 note_id**（见下） |
| `reconcile` | `GET /api/v1/user/me` | 最近 N 条按标题命中；重名时用 `POST /api/v1/feeds/detail` 的 `note.time` 挑最新 |
| `health` | `GET /api/v1/login/status` | 未登录 → `needs_relogin`；sidecar 不可达 → `degraded` |
| `fetch_metrics` | `GET /api/v1/user/me`（退回 `feeds/detail`） | 点赞/收藏/评论/分享；小红书不公开阅读量，`views` 恒 `None` |
| `fetch_metrics_for_title` | 同上 | 可选能力，占位 id 的兜底解析（`metrics/collector.py` 用 `hasattr` 探测） |
| `get_login_qrcode` | `GET /api/v1/login/qrcode` | base64 PNG，交给 `/accounts/{id}/login` 渲染 |

## 三个必须知道的设计取舍

### 1. 发布响应里没有 note_id

上游 `POST /api/v1/publish` 只回 `{"title","content","images":N,"status":"发布完成"}`。
所以发布成功后要立刻扫 `GET /api/v1/user/me` 的最近笔记按标题把 id 找回来。

**找不到时仍然返回 `ok=True`**，`platform_post_id` 记成占位值
`xhs-unresolved-<hash>`（定时发布是 `xhs-scheduled-<hash>`）。
理由：小红书没有幂等接口，重试就是**真的再发一篇**。宁可少一条指标，也不能重复发。
占位 id 后续由 `metrics/collector.py` 经 `fetch_metrics_for_title` 兜底解析并回填。

### 2. reconcile 用「标题 + 类型（+ 重名时比发布时间）」，不用首图 hash

主页只给 CDN 封面 URL，平台会重编码，下下来的字节和本地卡片必然对不上，hash 比不了。
标题在 `prepare` 里已经归一化过（≤20 字、空白折叠），是稳定可核实的信号；
图文/视频用 `noteCard.type` 再筛一道；同标题多条时才多花一次 `feeds/detail` 取
`note.time` 选最新的。

### 3. 限频在 publisher 侧再挡一道

`core.scheduler.RateLimiter`（日上限 + 最小间隔）。调度器发布前会查一次，但
`/dev/*` 和人工触发不一定经过调度器，所以 `publish()` 里再查一次，超限抛
`RetryableError("rate limited")` 让调度器延后。两处记账用 `token=<内容项 id>` 去重，
否则一次发布会被记成两次、日限额被腰斩。

日限额取 `Account.daily_limit`，缺省 `XHS_DAILY_LIMIT`（50）。计划 2.3 的口径是
小红书日 ≤ 50，测试期建议 ≤ 10。

## 错误分类

sidecar 把所有 handler 失败都返回 `HTTP 500 + code + details`，状态码本身没信息量，
只能按文案分流（`client.classify_error`）：

| 情形 | 异常 | 后果 |
|---|---|---|
| `未登录` / `cookie` 等 | `NeedsReloginError` | 账号 → `needs_relogin`，挂起排期，通知扫码 |
| `标题长度超过限制` / `定时发布时间` / `违规` / HTTP 400 等 | `PermanentError` | 直接进死信 |
| sidecar 401 | `PermanentError` | **不是**掉线，是 `AUTH_TOKEN` 配错 |
| 超时 / 连不上 / 其它 5xx | `RetryableError` | 重试 |

默认落在 `RetryableError`：误判成永久失败会直接进死信，代价比多重试一次大。

## 配置

| 环境变量 | 说明 |
|---|---|
| `XHS_MCP_ENDPOINTS` | `acc1=http://localhost:18060,acc2=...`；缺省回落到 `Account.sidecar_endpoint` |
| `XHS_MCP_TOKENS` | `acc1=token1,...`，逐账号 Bearer token |
| `XHS_AUTH_TOKEN` | 所有 sidecar 共用一个 token 时用它 |
| `XHS_DAILY_LIMIT` | `Account.daily_limit` 为 0 时的兜底（默认 50） |
| `XHS_RECONCILE_NOTES` | 对账 / 指标扫描主页最近多少条（默认 20） |
| `XHS_MEDIA_HOST_DIR` / `XHS_MEDIA_CONTAINER_DIR` | 素材目录映射，默认 `data/media` → `/app/images` |
| `XHS_TIMEOUT_SECONDS` / `XHS_PUBLISH_TIMEOUT_SECONDS` | 30 / 300（浏览器自动化很慢） |
| `XHS_LOGIN_HEALTH_INTERVAL_MINUTES` | 登录巡检间隔，默认 10 |

**凭据只走环境变量，绝不入库。** 想按账号指定 token 变量名，可在
`Account.extra["xhs"]["auth_token_env"]` 里写**变量名**（不是值）——
`Account` 表没有为此新增列。

## 红线（docs/POLICY.md）

- 二维码只呈现给人扫，**不做任何验证码识别 / 自动登录**；`submit_sms_code` 直接拒绝
  （小红书没有短信通道，那是抖音的）。
- 一账号一 sidecar 一 volume，**不做 Cookie 池**；生成脚本会拒绝两个账号共用一个 volume。
- 同一账号不允许多网页端同时登录——别在浏览器里另开小红书网页版把 sidecar 顶下线。
- 2026-03-10 官方公告：完全 AI 驱动的账号直接封禁 → 发布前必经人工 `approved` 卡点，
  发布路径只能走 `publish_with_idempotency`。

## 排障

```bash
uv run pytest tests/publishers/test_xhs.py -q     # 全程 respx 打桩，不联网
curl -s http://localhost:18060/health             # sidecar 活着吗（/health 不鉴权）
```

其余见 `sidecars/xhs/README.md` 的排障表与 `docs/OPS.md` 第 2 节。
