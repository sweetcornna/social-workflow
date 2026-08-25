# Workbench API（`/api/v1`）契约

前端工作台的**唯一数据面**。本文档是前后端之间的契约：写前端只看这一份就够，不需要读后端代码。

- 基址：`http://<core-host>:8000/api/v1`（本机默认 `http://127.0.0.1:8000/api/v1`）
- 交互式 schema：`/docs`（Swagger UI）、`/openapi.json`（FastAPI 自动生成，全部端点带 `response_model`）
- 实现：`core/api/`。业务逻辑全部复用既有模块，API 层只做"取参数 → 调函数 → 包 envelope"
- 本文档所有响应示例都是**真机 curl 的原始输出**（fake publishers + 样例数据），只对二维码 base64 做了截断

> 既有的 Jinja2 + HTMX 页面（`/review`、`/accounts`、`/stats`）**原样保留**，不受本 API 影响，
> 也不受 token 认证影响。两套门面调的是同一批业务函数，所以不存在"页面拦得住、curl 拦不住"。

---

## 目录

1. [通用约定](#1-通用约定)
2. [认证](#2-认证)
3. [错误码表](#3-错误码表)
4. [Dashboard](#4-dashboard)
5. [审核](#5-审核)
6. [账号与登录](#6-账号与登录)
   - P10 新增：[6.7 建号](#67-post-apiv1accounts) · [6.8 改配置](#68-patch-apiv1accountsid) ·
     [6.9 停用](#69-post-apiv1accountsiddeactivate) · [6.10 启用](#610-post-apiv1accountsidreactivate) ·
     [6.11 看 sidecar](#611-get-apiv1accountsidsidecar) · [6.12 起停 sidecar](#612-post-apiv1accountsidsidecaraction) ·
     [6.13 手动出稿](#613-post-apiv1accountsidgenerate)
7. [内容与排期](#7-内容与排期)
   - P12 新增：[7.5 确认发布](#75-post-apiv1contentidconfirm) ·
     [7.6 不发·驳回](#76-post-apiv1contentidreject)
8. [选题](#8-选题)
9. [任务](#9-任务)
10. [统计与成本](#10-统计与成本)
11. [复盘](#11-复盘)
12. [系统](#12-系统)
    - P12 新增：[12.5 提醒渠道状态](#125-get-apiv1systemtelegram)
13. [媒体（复用既有端点）](#13-媒体复用既有端点)
14. [轮询建议](#14-轮询建议)
15. [已知限制](#15-已知限制)

---

## 1. 通用约定

### 1.1 Envelope

**每一个**响应（成功与失败）都是这个形状：

```json
{ "ok": true,  "data": { ... }, "error": null }
{ "ok": false, "data": null,    "error": { "code": "not_found", "message": "内容项不存在: nope", "detail": null } }
```

- `ok`：布尔。前端唯一需要分支的字段
- `data`：成功时的载荷；失败时恒为 `null`
- `error.code`：机器可读，见[错误码表](#3-错误码表)。**按它分支，别按 message**
- `error.message`：中文，可直接显示给运营
- `error.detail`：可选的结构化补充（如改期失败时的"最近合法槽位"）。多数情况是 `null`

HTTP 状态码与 `error.code` 一致（404 → `not_found`，401 → `unauthorized`…），两者都能用。

### 1.2 分页

所有列表端点接受 `?limit=&offset=`，返回：

```json
{ "items": [ ... ], "total": 128, "limit": 50, "offset": 0 }
```

- `limit` 默认 50，**最大 200**（超了返回 422 `validation_error`）
- `offset` 默认 0
- `total` 是**过滤后的总数**，与 limit/offset 无关，可直接算总页数

例外：`GET /accounts` 与 `GET /insights` 返回**裸数组**（账号数是个位数，分页只会碍事）。

### 1.3 时间

- 一律 **UTC ISO8601**，形如 `2026-08-16T15:24:51.928368Z`（JS 里 `new Date(s)` 直接能解析）
- 请求里传时间也用 ISO8601；**不带时区一律按 UTC 解析**，所以前端请显式带 `Z` 或 `+08:00`
- 例外：`day` / `since_day` / `date` 这类**日期**字段是 `YYYY-MM-DD`（UTC 切日），`slot_text` 是给人看的
  账号本地时区文案（如 `08-17 19:00（Asia/Shanghai）`），别拿去解析

### 1.4 内容行（ContentRow）

审核队列、内容列表、写操作的返回里都是**同一种**内容行，字段固定：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` / `account_id` / `account_name` / `platform` / `title` / `status` | string | 基本信息，`platform` ∈ `wechat_mp` / `xhs` / `douyin` |
| `created_at` / `updated_at` / `scheduled_at` / `published_at` | datetime \| null | UTC |
| `slot_text` | string | 排期时刻的本地时区文案，没排期是空串 |
| `platform_post_id` / `url` / `publish_phase` / `attempts` / `last_error` | | 最近一条发布记录的结果，`publish_phase` ∈ `in_flight` / `done` / `failed` |
| `needs_watch` | bool | **内容包里有视频** → 批准前必须 `watched=true` |
| `cover_url` | string \| null | 封面图地址（见[媒体](#13-媒体复用既有端点)）。本地没有可读文件时是 `null`，前端显示占位图 |
| `media` | object | `{total, images, videos, kinds[], cover_index}` |
| `tags` | string[] | |
| `review_notes` | string \| null | 机器审核摘要 / 驳回理由（**纯文本**） |
| `machine_review` | object \| null | 机器审核结论，只有审核相关端点会填 |
| `timeline_at` | datetime \| null | 时间线锚点：已发布时刻 → 排期时刻 → 更新时刻 |
| `confirm_required` | bool | 这个账号发布前要不要人点一下确认（P12，`AccountPolicy.confirm_required`） |
| `awaiting_confirm` | bool | 现在正卡在"等你确认"——`confirm_required` 且未确认且 `status=scheduled` 才为真 |
| `confirmed_at` | datetime \| null | 人点了「确认发布」的时刻；`null` = 还没点 |
| `confirm_pushed_at` | datetime \| null | 确认卡第一次成功推送的时刻（Telegram 没配则一直是 `null`，工作台兜底按钮不受影响） |
| `confirm_deadline` | datetime \| null | **决定期限**：到点还没人点就被 `confirm_gate` 自动驳回。和 `scheduled_at` 是两个不同的时刻，工作台的双时刻读数（`19:00 发` / `还有 3 小时 40 分决定`）要的就是这两个数 |

### 1.5 内容状态机（`status`）

```
topic → drafting → draft → reviewing → rejected
                                    ↘ approved → scheduled → publishing → published → measured
异常：publishing → publish_failed → retrying → publishing ↘ dead_letter
账号掉线：scheduled ⇄ suspended
```

`dead_letter` 是**终态**，不可逆（见 [`retry_now`](#74-post-contentidretry_now)）。

---

## 2. 认证

**默认不鉴权**——这是跑在本机 / 内网的 ops 工具。要暴露出去就设环境变量：

```bash
SW_UI_TOKEN=一串足够长的随机串
```

设了之后：

- `/api/v1/*` **全部**要求 `Authorization: Bearer <token>`（常量时间比较，只认请求头，
  不接受 `?token=`——那会被写进 nginx 日志和浏览器历史）
- 唯一例外是 `POST /api/v1/auth/login`（登录页的探针，token 在 body 里）
- HTML 页面（`/review`、`/accounts`、`/stats`）与 `/health`、`/stats.json`、`/dev/*` **不受影响**

### `POST /api/v1/auth/login`

Body：`{"token": "..."}`。校验通过回 `ok`，错的回 401。

```console
$ curl -s -X POST http://127.0.0.1:8125/api/v1/auth/login \
    -H 'content-type: application/json' -d '{"token":"demo-token-abc123"}'
{
    "ok": true,
    "data": { "ok": true, "auth_required": true, "message": "已认证" },
    "error": null
}
```

未配置 `SW_UI_TOKEN` 时，任何 token 都返回 `ok`，且 `auth_required: false` —— 前端据此**跳过登录页**：

```json
{ "ok": true, "data": { "ok": true, "auth_required": false, "message": "本实例未开启 token 认证" }, "error": null }
```

缺 token 时的 401（token 模式实测）：

```console
$ curl -s http://127.0.0.1:8125/api/v1/dashboard
{
    "ok": false,
    "data": null,
    "error": { "code": "unauthorized", "message": "缺少 Authorization: Bearer <token>", "detail": null }
}
```

`GET /api/v1/system/info` 的 `auth_required` 字段也能查到当前实例是否开了鉴权，但它本身要鉴权，
所以**登录页应该用 `/auth/login` 探测**。

---

## 3. 错误码表

| HTTP | `error.code` | 出现在哪 | 前端该怎么办 |
|---|---|---|---|
| 401 | `unauthorized` | 全部（token 模式） | 跳登录页 |
| 404 | `not_found` | 各详情 / 写端点、未知 tick | 提示"已不存在"，刷新列表 |
| 409 | `invalid_state` | 批准非 draft/reviewing 的内容；重投状态不对 | 提示当前状态，刷新该行 |
| 409 | `illegal_transition` | 状态机拒绝（改期一条 draft；停用 / 启用一个 `banned` 账号） | 同上 |
| 409 | `account_banned` | 给已封禁账号出稿 | 禁用"出一条稿"按钮，引导去处理封禁 |
| 409 | `account_suspended` | 给已停用账号出稿 | 引导先点"启用" |
| 409 | `generation_failed` | 生成链跑不下去（选题池空、渲染失败…） | 显示 message，这是预期内的失败不是 500 |
| 409 | `id_exhausted` | 该平台前缀的账号编号用完了 | 提示去清理台账 |
| 409 | `confirm_conflict` | 发布前确认 / 驳回：内容已经被确认过、或已不在等确认状态（重放 / 双击 / 两个门面同时点） | 提示"已经处理过了"，刷新该行（工作台按钮与 Telegram 卡片走的是同一把闸门） |
| 422 | `watch_required` | 批准含视频内容但 `watched` 不为真 | 弹"请完整观看成片"，勾选后重试 |
| 422 | `reason_required` | 驳回没填理由 | 聚焦理由输入框 |
| 422 | `invalid_bundle` | 改稿后内容包不合法 | 显示 message（里面有 pydantic 的具体报错） |
| 422 | `invalid_slot` | 改期到非法时刻 | 用 `error.detail.suggested_slot` 提示最近合法槽位 |
| 422 | `invalid_code` | 提交空验证码 | |
| 422 | `invalid_tick_param` | tick 不接受该参数 | |
| 422 | `invalid_platform` | 建号时 `platform` 不认识 | message 里带了合法取值，做成下拉框就不会撞上 |
| 422 | `invalid_window` | `publish_windows` 写法不对 | `error.detail.example` 是正确写法的例子，直接显示 |
| 422 | `invalid_timezone` | `timezone` 这台机器不认识 | 聚焦时区输入框（例：`Asia/Shanghai`） |
| 422 | `limit_above_ceiling` | `daily_limit` 超过平台硬顶 | 用 `error.detail.ceiling` 把上限写进输入框提示 |
| 422 | `identity_hint_required` | 建抖音号没填 `identity_hint` | 聚焦该字段，说明它是防发错号的依据 |
| 422 | `invalid_account` | 组装出的台账条目过不了台账自己的解析器 | 显示 message（里面有台账层的具体报错） |
| 422 | `unknown_action` | sidecar 动作不是 `start`/`stop`/`recreate` | 只可能是前端拼错了 URL |
| 422 | `validation_error` | 入参不合法（类型 / 范围 / 缺字段），FastAPI 校验层 | `error.detail.errors` 里有逐字段原文 |
| 429 | `generate_limit` | 手动出稿超当日条数闸门 | 用 `error.detail` 的 `used_today` / `cap` 显示"今天 2/2" |
| 429 | `budget_exhausted` | 今天的 token 预算已用完 | `error.detail` 是预算快照，直接画进度条 |
| 500 | `tick_failed` | 手动跑 tick 内部炸了 | 显示 message（`类型: 描述`） |
| 501 | `not_supported` | 平台不支持该登录动作；非小红书账号问 sidecar；平台还没有生成链 | 隐藏对应按钮 |
| 502 | `upstream_error` | sidecar / 上传器出错 | 提示"稍后重试"，附 message |
| 502 | `sidecar_error` | 起 / 停 / 重建容器失败（含 `SW_SIDECAR_DRIVER=none` 时的拒绝） | 原样显示 message，它写清了该去改哪个环境变量 |
| 502 | `llm_failed` | 出稿时模型调用失败（网关 5xx、限流、拒答、结构化输出兜不住） | 提示"稍等重试"并附 message（末尾括号里是异常原文）；反复出现引导去系统页跑 preflight |
| 503 | `publisher_unavailable` | 发布器未注册 | 提示去看 `/system/preflight` |
| 503 | `credentials_missing` | 公众号出稿但没配 `WECHAT_APPID` / `WECHAT_APPSECRET` | 原样显示 message，**别在前端收凭据** |
| 503 | `render_unavailable` | 抖音出稿但渲染服务（MPT）不可达 | 原样显示 message，引导去起 MoneyPrinterTurbo |

`validation_error` 实例：

```console
$ curl -s 'http://127.0.0.1:8123/api/v1/review?limit=9999'
{
    "ok": false,
    "data": null,
    "error": {
        "code": "validation_error",
        "message": "query.limit: Input should be less than or equal to 200",
        "detail": {
            "errors": [
                { "type": "less_than_equal", "loc": ["query", "limit"],
                  "msg": "Input should be less than or equal to 200", "input": "9999" }
            ]
        }
    }
}
```

---

## 4. Dashboard

### `GET /api/v1/dashboard`

首页一屏。参数：`days`（统计窗口，默认 7，1–90）。

```console
$ curl -s http://127.0.0.1:8123/api/v1/dashboard
{
    "ok": true,
    "data": {
        "generated_at": "2026-08-16T15:24:51.928368Z",
        "window_days": 7,
        "counters": {
            "pending_review": 2,
            "published_today": 1,
            "published_7d": 1,
            "failed": 0,
            "dead_letter": 0,
            "scheduled": 1,
            "suspended": 0,
            "rendering": 1,
            "accounts_needing_relogin": 1,
            "accounts_degraded": 0,
            "accounts_suspended": 0
        },
        "budget": {
            "tokens":         { "used": 20532.0, "limit": 2000000.0, "remaining": 1979468.0 },
            "render_seconds": { "used": 42.0,    "limit": 3600.0,    "remaining": 3558.0 }
        },
        "platforms": [
            {
                "platform": "douyin", "accounts": 1,
                "ok": 0, "degraded": 0, "needs_relogin": 1, "banned": 0, "suspended": 0,
                "pending_review": 1, "scheduled": 0, "published": 0,
                "used_today": 0, "daily_limit": 2
            },
            {
                "platform": "xhs", "accounts": 1,
                "ok": 1, "degraded": 0, "needs_relogin": 0, "banned": 0, "suspended": 0,
                "pending_review": 1, "scheduled": 1, "published": 1,
                "used_today": 1, "daily_limit": 5
            }
        ],
        "attention": [
            { "account_id": "douyin-demo-01", "name": "抖音 Demo 01",
              "platform": "douyin", "status": "needs_relogin", "suspended": 0 }
        ],
        "events": [
            {
                "kind": "publish", "at": "2026-08-16T15:24:25.075648Z", "actor": "system",
                "action": "done", "item_id": "itm_demo_done",
                "title": "地铁通勤 30 分钟能做什么", "account_id": "xhs-demo-01",
                "detail": "65f0c0de000000001203a1b2",
                "url": "https://www.xiaohongshu.com/explore/65f0c0de000000001203a1b2"
            },
            {
                "kind": "review_log", "at": "2026-08-16T15:24:25.074178Z", "actor": "system",
                "action": "machine_review", "item_id": "itm_demo_draft",
                "title": "3 个让通勤包变轻的收纳思路", "account_id": "xhs-demo-01",
                "detail": "机器审核通过", "url": null
            }
        ]
    },
    "error": null
}
```

要点：

- `counters.published_today` 按 **UTC 日**切；`published_7d` 是 `days` 窗口内的
- `budget` 是**今天**的成本闸门（`tokens` / `render_seconds`）
- **`suspended` 这个名字在两处含义不同**：`counters.suspended` 与 `attention[].suspended` 数的是
  **被挂起的排期内容条数**（账号掉线时 `scheduled → suspended`）；`counters.accounts_suspended` 与
  `platforms[].suspended` 数的是**人工停用的账号个数**（见 [6.9](#69-post-apiv1accountsiddeactivate)）。
  界面上别把两者放进同一个计数器
- `counters.accounts_degraded` 是 sidecar / 上传器连不上导致降级的账号数。它和"要扫码"是两回事：
  扫码是人去掏手机，降级是去看那台机器上的容器活没活着（[6.11](#611-get-apiv1accountsidsidecar)）
- `events` 是 `ReviewLog` 与 `PublishRecord` 混排的最近 20 条，`kind` 区分来源；
  `action` 对 `review_log` 是动作名（`approve` / `reject` / `edit` / `schedule` / `machine_review` /
  `publish` / `publish_failed` / `dead_letter` / `suspend` / `resume` / `reconciled` / `requeue`），
  对 `publish` 是发布阶段（`in_flight` / `done` / `failed`）
- `attention` 是需要人立刻处理的账号（`needs_relogin` / `banned`），点进去就是登录页。
  **人工停用（`suspended`）的账号不在里面**——那是人自己按下去的，不是待办事项

---

## 5. 审核

### 5.1 `GET /api/v1/review`

审核队列。参数：`status`（留空 = `draft`+`reviewing`+`rejected`；`all` = 全部；也可传单个状态）、
`platform`、`account_id`、`limit`、`offset`。

```console
$ curl -s http://127.0.0.1:8123/api/v1/review
{
    "ok": true,
    "data": {
        "items": [
            {
                "id": "itm_demo_video",
                "account_id": "douyin-demo-01",
                "account_name": "抖音 Demo 01",
                "platform": "douyin",
                "title": "通勤成本上涨怎么省 · 口播",
                "status": "draft",
                "created_at": "2026-08-16T15:24:25.075954Z",
                "updated_at": "2026-08-16T15:24:25.075954Z",
                "scheduled_at": null,
                "slot_text": "",
                "published_at": null,
                "platform_post_id": null,
                "url": null,
                "publish_phase": null,
                "attempts": 0,
                "last_error": null,
                "needs_watch": true,
                "cover_url": null,
                "media": { "total": 1, "images": 0, "videos": 1, "kinds": ["video"], "cover_index": null },
                "tags": ["通勤", "收纳"],
                "review_notes": null,
                "machine_review": null,
                "timeline_at": "2026-08-16T15:24:25.075954Z"
            },
            {
                "id": "itm_demo_draft",
                "account_id": "xhs-demo-01",
                "account_name": "小红书 Demo 01",
                "platform": "xhs",
                "title": "3 个让通勤包变轻的收纳思路",
                "status": "draft",
                "needs_watch": false,
                "cover_url": null,
                "media": { "total": 2, "images": 2, "videos": 0, "kinds": ["image", "image"], "cover_index": 0 },
                "tags": ["通勤", "收纳"],
                "review_notes": "机器审核通过：block 0 / warn 1 / info 0\n[warn] lexicon.绝对化用语 · 命中「最轻」 · 建议：改成「更轻」",
                "machine_review": {
                    "at": "2026-08-16T15:24:25.074178Z",
                    "passed": true,
                    "blocking": 0,
                    "warnings": 1,
                    "stages_run": ["lexicon", "precheck", "inspect"],
                    "stages_skipped": { "llm_semantic": "未注入 LLM 客户端" },
                    "suggested_edits": { "最轻": "更轻" },
                    "notes": [
                        "机器审核通过：block 0 / warn 1 / info 0",
                        "[warn] lexicon.绝对化用语 · 命中「最轻」 · 建议：改成「更轻」"
                    ]
                },
                "timeline_at": "2026-08-16T15:24:25.074488Z"
            }
        ],
        "total": 2,
        "limit": 50,
        "offset": 0
    },
    "error": null
}
```

（第二条为省篇幅省略了与第一条同名的空字段，实际响应里字段是全的。）

### 5.2 `GET /api/v1/review/{id}`

审核详情（全量）。`data` 的结构：

| 字段 | 说明 |
|---|---|
| `item` | 上面的 ContentRow（含 `machine_review`） |
| `bundle` | 归一化内容包：`platform` / `title` / `body_markdown` / `body_html` / `tags` / `media[]` / `images[]` / `videos[]` / `cover` / `digest` / `author` / `schedule_at` / `is_original` / `duration_s` / `hook` / `script` / `render` |
| `platform_extra` | 平台特有字段原文 |
| `machine_review` | 同 `item.machine_review`，放外层方便直接取 |
| `logs` | 审计日志（新 → 旧）：`{id, actor, action, reason, at, is_human, has_diff}` |
| `slot` | `{scheduled_at, slot_text, account_windows}`，`account_windows` 解释"为什么排到这个点" |
| `diff` | 最近一次人工改稿的 unified diff（纯文本，没改过是空串） |
| `media_url_template` | 固定 `"/review/{item_id}/media/{index}"` |

`bundle.media[]` 每项是 `{index, path, kind, cover, exists}`：`exists=false` 表示本地文件不在了
（生成机与展示机不同盘时会出现），前端应显示占位图而不是打一个必然 404 的请求。

```console
$ curl -s http://127.0.0.1:8123/api/v1/review/itm_demo_draft
{
    "ok": true,
    "data": {
        "item": { "id": "itm_demo_draft", "...": "同上" },
        "bundle": {
            "platform": "xhs",
            "title": "3 个让通勤包变轻的收纳思路",
            "body_markdown": "正文示例：三个把通勤包变轻的收纳思路。",
            "body_html": null,
            "tags": ["通勤", "收纳"],
            "media": [
                { "index": 0, "path": "data/demo/cover.png", "kind": "image", "cover": true,  "exists": false },
                { "index": 1, "path": "data/demo/page2.png", "kind": "image", "cover": false, "exists": false }
            ],
            "images": [ "…同 media 里的图片项…" ],
            "videos": [],
            "cover": { "index": 0, "path": "data/demo/cover.png", "kind": "image", "cover": true, "exists": false },
            "digest": "", "author": "",
            "schedule_at": null, "is_original": null,
            "duration_s": null, "hook": "", "script": "", "render": {}
        },
        "platform_extra": {},
        "machine_review": { "…同 item.machine_review…": null },
        "logs": [
            { "id": "rvl_…", "actor": "system", "action": "machine_review",
              "reason": "机器审核通过", "at": "2026-08-16T15:24:25.074178Z",
              "is_human": false, "has_diff": true }
        ],
        "slot": { "scheduled_at": null, "slot_text": "", "account_windows": "09:00-11:00、19:00-22:00" },
        "diff": "",
        "media_url_template": "/review/{item_id}/media/{index}"
    },
    "error": null
}
```

### 5.3 `GET /api/v1/review/{id}/records`

该内容的发布尝试历史（详情页右栏）。`data` 是数组：

```console
$ curl -s http://127.0.0.1:8123/api/v1/review/itm_demo_done/records
{
    "ok": true,
    "data": [
        {
            "id": "pub_demo_1",
            "phase": "done",
            "attempts": 1,
            "platform_post_id": "65f0c0de000000001203a1b2",
            "url": "https://www.xiaohongshu.com/explore/65f0c0de000000001203a1b2",
            "last_error": null,
            "created_at": "2026-08-16T15:24:25.075647Z",
            "updated_at": "2026-08-16T15:24:25.075648Z"
        }
    ],
    "error": null
}
```

### 5.4 `POST /api/v1/review/{id}/approve`

Body：`{"actor": "operator", "reason": null, "watched": false}`（全部可选）。

语义与表单端点 `POST /review/{id}/approve` **完全一致**：

1. 只有 `draft` / `reviewing` 能批准 → 否则 409 `invalid_state`
2. `needs_watch=true` 的内容必须 `watched: true` → 否则 422 `watch_required`
   （"看过了"会写进 `platform_extra.watched_by/at`，是合规证据链的一部分）
3. 公众号会写入本条内容的 `confirm_publish`（双确认闸门第三道）
4. 批准后**立刻排期**。排上了 `scheduled=true` + `scheduled_at`；
   **排不上不报错**：内容停在 `approved`，`message` 里写明被哪一道挡住（窗口 / 最小间隔 / 日上限）

```console
$ curl -s -X POST http://127.0.0.1:8123/api/v1/review/itm_demo_draft/approve \
    -H 'content-type: application/json' -d '{"actor":"auditor","reason":"标题已收敛"}'
{
    "ok": true,
    "data": {
        "item": {
            "id": "itm_demo_draft",
            "status": "scheduled",
            "scheduled_at": "2026-08-17T11:00:00Z",
            "slot_text": "08-17 19:00（Asia/Shanghai）",
            "…": "其余 ContentRow 字段"
        },
        "message": "已批准，已排期至 08-17 19:00（Asia/Shanghai）",
        "scheduled": true,
        "scheduled_at": "2026-08-17T11:00:00Z",
        "slot_text": "08-17 19:00（Asia/Shanghai）"
    },
    "error": null
}
```

没看完成片就批准（实测）：

```console
$ curl -s -X POST http://127.0.0.1:8123/api/v1/review/itm_demo_video/approve \
    -H 'content-type: application/json' -d '{"actor":"auditor"}'      # HTTP 422
{
    "ok": false,
    "data": null,
    "error": {
        "code": "watch_required",
        "message": "含视频的内容必须先完整观看成片，并勾选「已完整观看」才能批准",
        "detail": null
    }
}
```

排不上期时（`scheduled: false`）的 message 形如：

```
已批准，但未能排期：账号 xhs-demo-01 在 14 天内没有可用发布槽位（窗口 09:00-11:00，最小间隔 120 分钟，日上限 5）。被挡在：日上限。多半是窗口太窄或日上限太低，改 accounts.yaml 后重新同步。
```

### 5.5 `POST /api/v1/review/{id}/reject`

Body：`{"actor": "operator", "reason": "必填"}`。理由会回写 `review_notes`，供改稿 Agent 当输入。
空白理由 → 422 `reason_required`；缺字段 → 422 `validation_error`。

返回体同 approve（`{item, message, scheduled, scheduled_at, slot_text}`，后三个是默认值）。

### 5.6 `POST /api/v1/review/{id}/edit`

Body：`{"actor": "operator", "title": "…", "body_markdown": "…", "tags": ["a","b"], "reason": null}`。
`title` / `body_markdown` 必填，`tags` 是**数组**（表单端点那边是逗号串）。

改完状态回到 `draft`，before/after 进审计日志，详情页的 `diff` 立刻能看到。内容包非法 → 422 `invalid_bundle`。

---

## 6. 账号与登录

### 6.1 `GET /api/v1/accounts`

不分页，`data` 是数组。参数：`platform`、`status`。

```console
$ curl -s http://127.0.0.1:8123/api/v1/accounts
{
    "ok": true,
    "data": [
        {
            "id": "douyin-demo-01",
            "name": "抖音 Demo 01",
            "platform": "douyin",
            "status": "needs_relogin",
            "needs_attention": true,
            "policy": {
                "daily_limit": 2, "daily_target": 0,
                "publish_windows": "全天", "timezone": "Asia/Shanghai",
                "min_interval_minutes": 30, "has_persona": false,
                "autopilot": false, "confirm_required": true, "confirm_ttl_hours": 24
            },
            "used_today": 0,
            "quota_left": 2,
            "last_published_at": null,
            "sidecar_endpoint": null,
            "supports_login": true,
            "insights_updated_at": null,
            "insights_error": "",
            "created_at": "2026-08-16T15:24:25.072891Z",
            "updated_at": "2026-08-16T15:24:25.072892Z"
        },
        {
            "id": "xhs-demo-01",
            "name": "小红书 Demo 01",
            "platform": "xhs",
            "status": "ok",
            "needs_attention": false,
            "policy": {
                "daily_limit": 5, "daily_target": 1,
                "publish_windows": "09:00-11:00、19:00-22:00", "timezone": "Asia/Shanghai",
                "min_interval_minutes": 120, "has_persona": false,
                "autopilot": false, "confirm_required": true, "confirm_ttl_hours": 24
            },
            "used_today": 1,
            "quota_left": 4,
            "last_published_at": "2026-08-16T15:24:25.075648Z",
            "sidecar_endpoint": "http://localhost:18060",
            "supports_login": true,
            "insights_updated_at": null,
            "insights_error": "",
            "created_at": "2026-08-16T15:24:25.072532Z",
            "updated_at": "2026-08-16T15:24:25.072539Z"
        }
    ],
    "error": null
}
```

- `status` ∈ `ok` / `degraded` / `needs_relogin` / `banned` / `suspended`
  （`banned` 需人工确认才能解除；`suspended` 是**人工停用**，只能人工进、人工出——
  登录巡检碰到它一律跳过，不会偷偷把它打开，见 [6.9](#69-post-apiv1accountsiddeactivate)）
- `policy` 来自 `accounts.yaml`（日上限已按平台硬顶夹过：抖音 ≤ 10、小红书 ≤ 50）
- `policy.autopilot` / `policy.confirm_required` / `policy.confirm_ttl_hours`（P12）：
  机器审核干净的稿子是否自动批准并排期（`autopilot`，默认 `false`）、发布前要不要人点一下确认
  （`confirm_required`，默认 `true`，**没有旁路**——小红书 2026-03-10 公告封禁 AI 全托管账号，
  这是合规底线，见 `docs/POLICY.md`）、推了确认卡多少小时没人点就自动驳回
  （`confirm_ttl_hours`，默认 `24`）。前两个可以在 [6.7](#67-post-apiv1accounts) /
  [6.8](#68-patch-apiv1accountsid) 里改，`confirm_ttl_hours` 目前只能写 `accounts.yaml`
- `used_today` / `quota_left` 是**限频现状**，真相在 `PublishRecord`，不是内存计数
- `used_today` 按**账号本地日**计（`policy.timezone`，对照表见 `OPS.md`《三个"今天"》）：
  `Asia/Shanghai` 的号在本地 00:00 归零，不是 UTC 00:00（本地 08:00）。
  所以本地凌晨打开工作台时，"今日已发"不会还挂着昨晚发的那几条。
  `quota_left = max(daily_limit - used_today, 0)`，与发布 tick 的限频闸门同一口径——
  这里显示 0，那边就一定发不出去。
  ⚠️ 手动出稿返回体里的 `used_today`（[6.13](#613-post-apiv1accountsidgenerate)）**不是**这个：
  那是当日**草稿**计数，按 UTC 日切，与成本闸门同源

### 6.2 `GET /api/v1/accounts/{id}`

在上面的基础上多出：`pending_review`、`scheduled`、`suspended`、`dead_letter`、`extra`
（`Account.extra` 原样透出；里面**没有任何凭据**，小红书只存 token 的环境变量名）。

### 6.3 `GET /api/v1/accounts/{id}/login/qrcode`

```console
$ curl -s http://127.0.0.1:8123/api/v1/accounts/xhs-demo-01/login/qrcode
{
    "ok": true,
    "data": {
        "account_id": "xhs-demo-01",
        "platform": "xhs",
        "image_base64": "iVBORw0KGgoAAAANSUhEUgAAAMgAAADICAAAAACI…（此处截断，实际约 3KB）",
        "status": "ok",
        "detail": "",
        "account_status": "ok",
        "placeholder": true,
        "expires_in": 120,
        "fetched_at": "2026-08-16T15:24:52.640262Z"
    },
    "error": null
}
```

- `image_base64` 是 PNG 的 base64（不含 data URI 前缀），前端拼 `data:image/png;base64,` 显示
- `placeholder: true` 表示这是 FakePublisher 的**占位图，不是能扫的真二维码**（`SW_USE_FAKE_PUBLISHERS=true` 时）
- `expires_in` 是二维码有效期（秒），真 sidecar 会给真值，做倒计时用
- 每次调用会**顺带巡一次登录态**并落 Account 状态机，所以 `account_status` 可能就此变化
- 平台不支持扫码 → 501 `not_supported`；发布器没注册 → 503；sidecar 报错 → 502

### 6.4 `GET /api/v1/accounts/{id}/login/status`

只查状态、不重取二维码。扫码成功后账号自动回 `ok`，**被挂起的排期项一并放回**。

```json
{ "ok": true, "data": {
    "account_id": "xhs-demo-01", "platform": "xhs",
    "status": "ok", "detail": "", "account_status": "ok",
    "logged_in": true, "checked_at": "2026-08-16T15:24:52.6Z"
}, "error": null }
```

### 6.5 `POST /api/v1/accounts/{id}/login/start`

抖音专用：二维码在**宿主机浏览器窗口**里，core 不代理图片，这个端点只负责把窗口弹出来。
成功返回 `{ok, account_id, platform, state, detail, started_at}`，`state` 是上传器的登录状态机
（如 `waiting_user`）。不支持的平台（含 fake 发布器）返回 501：

```console
$ curl -s -X POST http://127.0.0.1:8123/api/v1/accounts/douyin-demo-01/login/start   # HTTP 501
{
    "ok": false,
    "data": null,
    "error": { "code": "not_supported", "message": "平台 douyin 的发布器没有「打开登录窗口」这一步", "detail": null }
}
```

### 6.6 `POST /api/v1/accounts/{id}/login/code`

Body：`{"code": "135790"}`。验证码进内存队列，并**顺手转发**给发布器（抖音要填进宿主机页面里
那个正等着的输入框）。转发失败不算错误。

```console
$ curl -s -X POST http://127.0.0.1:8123/api/v1/accounts/xhs-demo-01/login/code \
    -H 'content-type: application/json' -d '{"code":"135790"}'
{
    "ok": true,
    "data": { "ok": true, "account_id": "xhs-demo-01", "pending": 1,
              "forwarded": true, "forward_detail": "已填入发布器当前页面" },
    "error": null
}
```

> 红线（docs/POLICY.md）：系统**不做任何自动打码 / 验证码识别**，二维码只呈现给人扫。
> 验证码**不落库、不写日志明文**——前端也别把它塞进任何本地存储。

### 6.7 `POST /api/v1/accounts`

新建账号。成功是 **HTTP 201**（不是 200，注意别把状态码写死成 2xx 判断之外的等值比较）。

Body（只有 `platform` / `name` 必填）：

```json
{
    "platform": "xhs",
    "name": "小红书测试号 03",
    "identity_hint": null,
    "publish_windows": ["12:00-14:00", "19:00-22:30"],
    "min_interval_minutes": 90,
    "daily_limit": 10,
    "daily_target": 1,
    "timezone": "Asia/Shanghai",
    "persona": null,
    "autopilot": false,
    "confirm_required": true
}
```

- **`id` 由服务端生成，不接受前端指定**：`<平台前缀>-<名字 slug>`（前缀是 `xhs` / `douyin` / `wechat`）。
  中文名 slug 化后是空的（这是常态），回落成 `xhs-03` 这样的两位序号——这个 id 会当 docker 容器名、
  volume 名与 URL 片段用，人得念得出来，所以不用 uuid、也不做音译
- 抖音的 `identity_hint` **必填**（创作者中心显示的昵称）：发布前会读页面比对，这是防发错号的唯一依据。
  缺了 422 `identity_hint_required`
- `publish_windows` 是**数组**，每项形如 `"09:00-11:00"`，跨零点写 `"22:00-02:00"`，留空 = 全天放行；
  起止相同不算合法（等于永不放行）
- 数值范围（pydantic 层，越界是 422 `validation_error`）：`min_interval_minutes` 0–1440、
  `daily_limit` 0–100、`daily_target` 0–50、`name` ≤ 64 字、`persona` ≤ 4000 字
- `daily_limit` 还要过**平台硬顶**（抖音 10、小红书 50）：超了直接 422 `limit_above_ceiling`，
  **不悄悄夹到硬顶**——悄悄改会让人以为配上了
- 小红书会顺手分配一个宿主机端口（从 18060 起，找第一个既没被台账占用、本机也没人在听的），
  并按"一账号一容器一 volume 一端口"的规矩落进台账；`sidecar.token_env` 里只写**环境变量名**，
  token 的值永远不入库、不经过工作台
- `autopilot`（P12，默认 `false`）：机器审核干净（block=0 且 warn=0）的稿子自动批准并排期，
  不用人再点一次"批准"。`confirm_required`（P12，默认 `true`）：批准之后、真发之前还要不要人点一下
  "确认发布"。**`autopilot` 只影响自动批准，不影响 `confirm_required` 这道闸门**——两个开关分别开
  才等于全自动，误以为打开 `autopilot` 就不用管确认，会当场撞上 409 `confirm_conflict` 或稿子卡在
  "等你确认"不发。详见 `docs/POLICY.md`《为什么保留人工确认》与 `docs/OPS.md`《发布前确认（Telegram）》

写入顺序刻意是**先台账、后库**：先写 `accounts.yaml`，再从这个文件同步进 DB。台账是唯一真相，
库是它的投影，所以建完 `python -m core.accounts check` 立刻就是通过的，不会漂移；同步中途炸掉会把
台账文件**回滚成动手之前的字节**（不会留下半条记录）。

```json
{ "ok": true, "data": {
    "account": {
        "id": "xhs-03",
        "name": "小红书测试号 03",
        "platform": "xhs",
        "status": "ok",
        "needs_attention": false,
        "policy": {
            "daily_limit": 10, "daily_target": 1,
            "publish_windows": "12:00-14:00、19:00-22:30", "timezone": "Asia/Shanghai",
            "min_interval_minutes": 90, "has_persona": false,
            "autopilot": false, "confirm_required": true, "confirm_ttl_hours": 24
        },
        "used_today": 0,
        "quota_left": 10,
        "last_published_at": null,
        "sidecar_endpoint": "http://localhost:18061",
        "supports_login": true,
        "insights_updated_at": null,
        "insights_error": "",
        "created_at": "2026-08-16T15:31:02.113402Z",
        "updated_at": "2026-08-16T15:31:02.113404Z",
        "pending_review": 0, "scheduled": 0, "suspended": 0, "dead_letter": 0,
        "extra": {
            "daily_target": 1,
            "publish_windows": ["12:00-14:00", "19:00-22:30"],
            "min_interval_minutes": 90,
            "timezone": "Asia/Shanghai",
            "xhs": { "auth_token_env": "XHS_TOKEN_XHS_03" }
        }
    },
    "message": "账号 xhs-03 已建好，台账与库都写了。",
    "warnings": [
        "SW_SIDECAR_DRIVER=none：账号已建好，但 core 不接管容器，sidecar 未接入。要扫码得先在服务器上把驱动改成 docker，或手工起容器。"
    ]
}, "error": null }
```

`data.account` 就是[账号详情](#62-get-apiv1accountsid)那一份（`AccountOut` + 四个计数 + `extra`），
直接拿去替换本地状态即可。6.7–6.10 四个写端点返回的都是**同一种** `{account, message, warnings}`。

`warnings` 是**非致命**的问题，一条条显示给人看：小红书建号会按 `SW_SIDECAR_DRIVER` 顺手拉容器，
容器起不来**不算创建失败**（账号已经在台账与库里了），如实报出来让人去看那台机器，比整个回滚有用。
`SW_SIDECAR_DRIVER=none` 时就直说"sidecar 未接入"，界面别转个圈假装它在起。

错误：`invalid_platform` / `invalid_window`（`detail.example` 里有正确写法）/ `invalid_timezone` /
`limit_above_ceiling`（`detail.ceiling` 是硬顶）/ `identity_hint_required` / `invalid_account` 都是 422；
编号用尽是 409 `id_exhausted`。

### 6.8 `PATCH /api/v1/accounts/{id}`

改配置。可改：`name`、`identity_hint`、`publish_windows`、`min_interval_minutes`、`daily_limit`、
`daily_target`、`timezone`、`persona`、`autopilot`、`confirm_required`（后两个 P12 新增）。
**没传的字段保持原样**（真正的 PATCH 语义，不是整体覆盖）。`confirm_ttl_hours` 不在这个列表里，
目前只能直接改 `accounts.yaml` 再同步。

**`platform` 与 `id` 不可改**——改了等于换了个号，历史内容、发布记录与审计日志全对不上。
这两个字段不在 schema 里，传了会被 pydantic 直接忽略，不报错也不生效。

```json
{ "name": "改过名的号", "daily_target": 3, "publish_windows": ["08:00-09:00"] }
```

同样是"改台账再同步"，所以改完 `check` 依然通过。库里有、台账里没有的历史账号（老库 / dev seed 造的），
改一次会**顺手补一条台账进去**，把一直在报的那种漂移修掉。

```json
{ "ok": true, "data": {
    "account": { "id": "xhs-03", "name": "改过名的号",
                 "policy": { "daily_target": 3, "publish_windows": "08:00-09:00", "…": "其余策略字段" },
                 "…": "其余 AccountDetail 字段" },
    "message": "配置已更新，台账同步写了。",
    "warnings": []
}, "error": null }
```

错误：账号不存在 404；校验类错误与 6.7 完全同一套（`invalid_window` / `invalid_timezone` /
`limit_above_ceiling` / `invalid_account`），另外 `name` 不许改成空串（422 `validation_error`）。

### 6.9 `POST /api/v1/accounts/{id}/deactivate`

人工停用。Body 可选：`{"reason": "先关一阵", "actor": "operator"}`（`reason` ≤ 280 字，会进审计日志）。

做两件事：账号状态置 `suspended`，**名下 `scheduled` 的内容一并挂起**（`scheduled → suspended`）。
停用期间调度器既不给它出稿也不给它发布，登录巡检也会跳过它——人明确关掉的号，巡检不许偷偷再打开。

**刻意不硬删账号**：历史内容、发布记录、审计日志都还挂在它上面，删了证据链就断了。所以界面上
这个按钮该叫"停用"，不该叫"删除"。

```json
{ "ok": true, "data": {
    "account": { "id": "xhs-03", "status": "suspended", "needs_attention": false,
                 "scheduled": 0, "suspended": 1,
                 "…": "其余 AccountDetail 字段" },
    "message": "已停用，顺手挂起了 1 条排期。重新启用后挂起的内容会自动放回。",
    "warnings": []
}, "error": null }
```

（`account.suspended` 是**被挂起的内容条数**，不是账号状态——账号状态看 `account.status`。）

**幂等**：已经停用的号再停一次照样 200，状态不变。唯一挡回去的是 `banned`：封禁是要人工确认才能
解除的终态，停不掉，409 `illegal_transition`。

### 6.10 `POST /api/v1/accounts/{id}/reactivate`

启用。Body 同 6.9（可选）。账号回 `ok`，被挂起的内容放回原状态（`suspended → scheduled`）。

回的是 `ok` 而**不是"停用前那个状态"**：停用期间登录态早就可能过期了，真实健康由下一次登录巡检
写回来，这里不假装知道。所以启用之后界面上该顺手提示"去看看还需不需要重新扫码"。

```json
{ "ok": true, "data": {
    "account": { "id": "xhs-03", "status": "ok", "scheduled": 1, "suspended": 0,
                 "…": "其余 AccountDetail 字段" },
    "message": "已启用，放回了 1 条排期。登录态是否还在，下一次巡检（或点「重新扫码」）说了算。",
    "warnings": []
}, "error": null }
```

`banned` 的号同样启用不了（409 `illegal_transition`）——封禁只能人工确认后解除，不走这个端点。

### 6.11 `GET /api/v1/accounts/{id}/sidecar`

**小红书专属**：容器状态 + `GET {endpoint}/health` 的原样透传。其它平台 501 `not_supported`
（抖音的上传器常驻宿主机，公众号走官方 API，都没有 sidecar 这回事），前端据此隐藏整块面板。

```json
{ "ok": true, "data": {
    "account_id": "xhs-03",
    "driver": "none",
    "state": "none-driver",
    "detail": "SW_SIDECAR_DRIVER=none：core 不接管容器。sidecar 请用 docker-compose.xhs.yml 自行起，或把 SW_SIDECAR_DRIVER 改成 docker。",
    "container": "sw-xhs-xhs-03",
    "volume": "swxhs_xhs-03",
    "image": "xpzouying/xiaohongshu-mcp:v2.5.0",
    "port": 18061,
    "endpoint": "http://localhost:18061",
    "health": null,
    "healthy": false,
    "health_detail": "连不上 http://localhost:18061/health：ConnectError",
    "checked_at": "2026-08-16T15:31:20.884413Z"
}, "error": null }
```

| 字段 | 说明 |
|---|---|
| `driver` | `docker`（core 接管容器）/ `none`（**默认**，只记账不起容器） |
| `state` | `running` / `stopped` / `absent`（容器还没建）/ `none-driver` / `error`（探测本身失败） |
| `detail` | 这一档状态的人话解释，**原样显示**（里面写清了该去改哪个环境变量） |
| `container` / `volume` / `image` / `port` / `endpoint` | 这个账号独占的那一套。一账号一容器一 volume 一端口，永不共享 |
| `health` | `GET {endpoint}/health` 的**原样透传**（上游这个路由不走鉴权）；探不通是 `null` |
| `healthy` / `health_detail` | 探通没有 + 探不通的原因（`连不上 …：ConnectError` 之类） |

`state` 与 `healthy` 是**两件事**：容器在跑不代表里面那个浏览器已经就绪，首次启动要几十秒。
界面上两个都显示，别只显示一个。

### 6.12 `POST /api/v1/accounts/{id}/sidecar/{action}`

`action` ∈ `start` / `stop` / `recreate`，无 body。

| 动作 | 行为 |
|---|---|
| `start` | 在跑就什么都不做；容器存在但没跑就 `docker start`（**登录态在 volume 里，不用重新扫码**）；不存在就按一账号一容器的规矩创建 |
| `stop` | `docker stop`。volume 还在，登录态不会丢 |
| `recreate` | 删容器按当前镜像重建。**只删容器不删 volume**，所以扫过的码不用重扫；真想清登录态得手工删 volume——那是个破坏性动作，不给按钮 |

```json
{ "ok": true, "data": {
    "sidecar": { "state": "running", "healthy": false, "…": "同 6.11 的 SidecarOut" },
    "message": "已创建并启动容器。首次启动要拉起内置浏览器，几十秒后再看健康探测。"
}, "error": null }
```

返回里的 `sidecar` 是**动作之后重新探的**状态，直接替换本地状态即可，别再调一次 6.11。

错误：非小红书账号 501 `not_supported`；`action` 拼错 422 `unknown_action`；容器操作失败
502 `sidecar_error`（`SW_SIDECAR_DRIVER=none` 时的拒绝也走这条，message 里写清了要改哪个变量）。

### 6.13 `POST /api/v1/accounts/{id}/generate`

给这个号手动出一条稿。Body 两个字段都可选：

| 字段 | 取值 | 说明 |
|---|---|---|
| `topic` | ≤ 200 字 | 手工指定选题标题；留空则跑选题 Agent 自己挑 |
| `illustrations` | 0–6 整数 | 配几张生图（P11）。留空取 `SW_GENERATE_ILLUSTRATIONS`（默认 2）；`0` = 不配图。**公众号题图与抖音封面只用第一张**。超出 0–6 直接 422 —— 手滑传个 999 会把当日生图额度一次烧光 |

跑的是**完整生成链**：选题 → 生成 → 机器审核 → 进审核队列，产出一条待审内容。和调度器的
`tick_generate` 走**同一批函数**，所以界面上点出来的东西就是自动出稿的东西，不存在"手点能出、
自动出不来"。

**三道闸门**，都在真调模型之前就挡住（每一道都给人话）：

| 闸门 | 触发 | 错误码 |
|---|---|---|
| 账号状态 | `banned` / `suspended` 不出稿 | 409 `account_banned` / `account_suspended` |
| 当日条数 | `max(daily_target, 1) × 2`。出稿会真的调 LLM（抖音还要渲染视频），连点两下就是几万 token | 429 `generate_limit`（`detail` 里有 `used_today` / `cap`） |
| token 预算 | 今天的预算用完（按 UTC 日重置） | 429 `budget_exhausted`（`detail` 是预算快照） |

平台各自还有前置条件：公众号要 `WECHAT_APPID` / `WECHAT_APPSECRET` 已配（503 `credentials_missing`），
抖音要渲染服务 MoneyPrinterTurbo 可达（503 `render_unavailable`）——缺了就直说缺什么，**不出半成品**。
抖音手动出稿默认**真渲染**：人点这个按钮就是想看成片。

```json
{ "ok": true, "data": {
    "account_id": "xhs-03",
    "content_item_id": "itm_7c1d…",
    "status": "draft",
    "title": "3 个让通勤包变轻的收纳思路",
    "llm": "real",
    "selected_topic": "租房收纳",
    "tokens_used": 18432,
    "elapsed_s": 42.15,
    "review_passed": true,
    "review_blocking": 0,
    "illustrations": 2,
    "warnings": [],
    "used_today": 1,
    "cap": 2,
    "message": "出好了：《3 个让通勤包变轻的收纳思路》已进审核队列。"
}, "error": null }
```

- `llm` ∈ `real` / `scripted` / `injected`。**`scripted` 表示这台 core 没配模型凭据**，内容是
  ScriptedLLM 的预置文案、不是真生成——此时 `message` 末尾会带上这句话，`warnings` 里也有
  `"ANTHROPIC_API_KEY 未配置，已用 ScriptedLLM 预置内容跑通链路（非真实生成）"`。
  界面上必须显眼地标出来，别让人把预置文案当成模型写的
- `review_passed` / `review_blocking` 是机器审核结论（`null` = 没跑）。**机器审核通过不等于可以发**，
  内容照样停在 `draft` 等人工审核
- `used_today` 是**含这一条**的当日**草稿**计数，配合 `cap` 直接显示成"今天 1/2"。
  它按 **UTC 日**切（与成本闸门同源：点一次就烧 token），**不是**账号列表里那个
  按账号本地日切的已发布数——两者同名不同义，前端别把它们混在一个组件里用
  （对照表见 `OPS.md`《三个"今天"》）
- `illustrations` 是**实际配上的张数**。它比请求的少（甚至是 0）是**正常的降级**，不是错误：
  权限没开、当日生图额度用完、网关抖动都只会让这条稿没有照片，绝不阻塞出稿主链。
  原因在 `warnings` 里（例如 `"这条内容没有生成配图：SW_IMAGEGEN_ENABLED=false…"`）。
  界面上开了开关却拿到 `0` 时**必须如实提示**，别让人以为开关没生效
- `content_item_id` 拿到就能跳[审核详情](#52-get-apiv1reviewid)

**这个端点很慢**：真 LLM 几十秒，抖音带渲染可能几分钟。前端要给 loading 态并**禁用按钮**，
别让人连点（闸门会挡住，但那时候钱已经烧了一半）。

错误：账号不存在 404；三道闸门见上表；生成链跑不下去（选题池空、渲染失败…）是 409
`generation_failed`——这是**预期内**的失败，不是 500，显示 message 即可；模型 / 网关这一侧
炸了（限流、5xx、拒答、结构化输出兜不住）是 502 `llm_failed`，message 末尾括号里带异常原文，
按"稍等重试"提示即可；平台还没有生成链 501 `not_supported`。

---

## 7. 内容与排期

### 7.1 `GET /api/v1/content`

参数：`status`、`platform`、`account_id`、`from`、`to`、`limit`、`offset`。

`from` / `to` 过滤的是 **`coalesce(scheduled_at, updated_at)`**：已发布的内容也带着它当初的排期时刻，
所以过去与未来能画在同一根时间轴上。响应里的 `timeline_at` 是同一口径的展示值
（已发布时优先取真实发布时刻）。

`items[]` 就是 ContentRow，已发布的那条长这样：

```json
{
    "id": "itm_demo_done",
    "account_id": "xhs-demo-01",
    "platform": "xhs",
    "title": "地铁通勤 30 分钟能做什么",
    "status": "published",
    "scheduled_at": "2026-08-16T05:24:25.075350Z",
    "slot_text": "08-16 13:24（Asia/Shanghai）",
    "published_at": "2026-08-16T15:24:25.075648Z",
    "platform_post_id": "65f0c0de000000001203a1b2",
    "url": "https://www.xiaohongshu.com/explore/65f0c0de000000001203a1b2",
    "publish_phase": "done",
    "attempts": 1,
    "last_error": null,
    "timeline_at": "2026-08-16T15:24:25.075648Z"
}
```

### 7.2 `GET /api/v1/content/{id}`

`{item, bundle, platform_extra, logs, account_windows}`，字段含义同[审核详情](#52-get-apiv1reviewid)。

### 7.3 `GET /api/v1/content/{id}/slots`

参数：`count`（1–30，默认 6）。只读查询，不改变内容或排期状态。

从当前时刻起按账号时区返回最近的合法发布槽位。每个候选均复用排期约束：必须落在
`publish_windows` 内、与已排期及最近已发布内容满足 `min_interval_minutes`、不超过
`daily_limit`，也不会与已占用时刻同一分钟冲突；同一响应前一个候选也会占用后一个候选。
未配置 `publish_windows` 时 `window` 为 `"00:00-24:00"`。

成功响应的 `data`：

```json
{
    "item_id": "itm_demo_sched",
    "account_id": "xhs-demo-01",
    "timezone": "Asia/Shanghai",
    "slots": [{
        "at": "2026-08-17T01:30:00Z",
        "slot_text": "08-17 09:30（Asia/Shanghai）",
        "window": "09:00-11:00"
    }],
    "note": "已返回最近 6 个合法发布槽位。"
}
```

`suspended` 或 `banned` 账号返回 HTTP 200、空 `slots` 和原因说明；内容不存在返回 HTTP 404
`not_found` 信封。若未来 14 天内的合法槽位不足 `count`，照常返回已找到的部分，`note` 会说明限制。

### 7.4 `POST /api/v1/content/{id}/reschedule`

Body：`{"scheduled_at": "2026-08-17T01:30:00Z", "actor": "operator"}`。

**这个端点走的是与"批准即排期"完全同一套校验**（`core/scheduling.py` 的 `SlotConstraints`）：
窗口、最小间隔、日上限、不能排到过去。前端能挑一个非法时刻，后端一定挡回去。

可改期的状态：`approved`（顺带补 `approved → scheduled`）、`scheduled`、`suspended`。其它状态 409。

成功：

```console
$ curl -s -X POST http://127.0.0.1:8123/api/v1/content/itm_demo_sched/reschedule \
    -H 'content-type: application/json' -d '{"scheduled_at":"2026-08-17T01:30:00Z","actor":"operator"}'
{
    "ok": true,
    "data": {
        "item": { "id": "itm_demo_sched", "status": "scheduled",
                  "scheduled_at": "2026-08-17T01:30:00Z",
                  "slot_text": "08-17 09:30（Asia/Shanghai）", "…": "其余 ContentRow 字段" },
        "scheduled_at": "2026-08-17T01:30:00Z",
        "slot_text": "08-17 09:30（Asia/Shanghai）",
        "message": "已改期至 08-17 09:30（Asia/Shanghai）"
    },
    "error": null
}
```

非法时刻（窗口是 09:00-11:00，这里挑了 11:00 整——右端点开区间）：

```console
$ curl -s -X POST http://127.0.0.1:8123/api/v1/content/itm_demo_sched/reschedule \
    -H 'content-type: application/json' -d '{"scheduled_at":"2026-08-18T03:00:00Z"}'   # HTTP 422
{
    "ok": false,
    "data": null,
    "error": {
        "code": "invalid_slot",
        "message": "08-18 11:00（Asia/Shanghai） 不是账号 xhs-demo-01 的合法发布时刻，被「窗口」挡住（窗口 09:00-11:00、19:00-22:00，最小间隔 120 分钟，日上限 5，时区 Asia/Shanghai）。",
        "detail": {
            "reason": "窗口",
            "suggested_slot": "2026-08-17T01:00:00+00:00",
            "suggested_slot_text": "08-17 09:00（Asia/Shanghai）",
            "account_windows": "09:00-11:00、19:00-22:00"
        }
    }
}
```

`detail.reason` ∈ `窗口` / `最小间隔` / `日上限` / `已过去`；`detail.suggested_slot` 是**最近一个合法槽位**
（14 天内算不出来时为 `null`）。前端应该把它做成"改用这个时间"的一键按钮。

### 7.5 `POST /api/v1/content/{id}/retry_now`

无 body。让卡住的内容重新有机会发出去，两种模式：

| 当前状态 | `mode` | 行为 |
|---|---|---|
| `retrying` / `publish_failed` | `retry_now` | 清掉指数退避 → 下一轮 `tick_retry_sweep`（默认 5 分钟）立刻重投。**只解退避这一道**，账号健康、限频、48 小时超龄照拦 |
| `dead_letter` | `requeued_as_draft` | 死信是 P0 冻结的**终态**，不能原地复活；改为把内容包复投成**新的一条 `draft`**（`new_item_id`），要重新过人工审核。原死信保持终态，两边都写审计日志 |
| 其它 | — | 409 `invalid_state` |

复投时会抹掉 `platform_extra` 里的闸门痕迹（`confirm_publish*` / `watched_*`），新稿子必须重新过确认。

```json
{ "ok": true, "data": {
    "item": { "id": "itm_dead_1", "status": "dead_letter", "…": "" },
    "mode": "requeued_as_draft",
    "message": "死信是终态，已复投为新的待审内容 itm_9f3a…，请重新走人工审核。",
    "new_item_id": "itm_9f3a…"
}, "error": null }
```

### 7.6 `POST /api/v1/content/{id}/confirm`

发布前人工确认闸门的**工作台兜底**（P12）。Body 可选：`{"actor": "operator"}`。

只有 `confirm_required` 的账号会走到这道闸门；确认之前 `tick_scheduled_publish` 一条都不会发
（统计里的 `skipped_unconfirmed`）。和 Telegram 确认卡上「确认发布」按钮走的是**同一个后端函数**
（`core.confirm.confirm_item`），语义、审计日志（`ReviewLog.action=confirm`）、重放保护完全一致——
Telegram 不是单点：bot 挂了、手机不在身边，这里照样能把稿子放出去。

只能对 `status=scheduled` 且未确认的内容调用；重复确认 / 已经不在等确认状态返回 409
`confirm_conflict`（一条内容只认第一次有效点击，防重放 / 双击 / 两个门面同时点）。

```console
$ curl -s -X POST http://127.0.0.1:8123/api/v1/content/itm_demo_sched/confirm \
    -H 'content-type: application/json' -d '{"actor":"operator"}'
{
    "ok": true,
    "data": {
        "item": { "id": "itm_demo_sched", "status": "scheduled",
                  "awaiting_confirm": false, "confirmed_at": "2026-08-17T10:50:20.572579Z",
                  "confirm_deadline": "2026-08-18T10:49:13.986453Z", "…": "其余 ContentRow 字段" },
        "message": "已确认，到点就发（08-17 18:49（Asia/Shanghai））"
    },
    "error": null
}
```

重复点第二次：

```console
$ curl -s -X POST http://127.0.0.1:8123/api/v1/content/itm_demo_sched/confirm -d '{}'   # HTTP 409
{ "ok": false, "data": null,
  "error": { "code": "confirm_conflict",
             "message": "这条已经在 08-17 10:50 UTC 确认过了" } }
```

⚠️ Body 是**原始 JSON 对象**，不是预先 `JSON.stringify` 过的字符串——工作台的 `apiFetch` helper
自己会做序列化，调用方传字符串会被再包一层引号，后端按"不是合法字典"拒收（422 `validation_error`）。

### 7.7 `POST /api/v1/content/{id}/reject`

发布前确认环节的「不发」（P12）。Body 可选：`{"reason": "...", "actor": "operator"}`，
`reason` 留空则落一句默认文案。内容**逐跳走状态机**退回 `rejected`
（`scheduled → approved → draft → reviewing → rejected`），排期槽位让出来，
理由写回 `review_notes` 给改稿 Agent 当 prompt 输入——和审核台上的驳回是同一套语义。

同样只能对还在等确认的内容调用；已处理过返回 409 `confirm_conflict`。

```console
$ curl -s -X POST http://127.0.0.1:8123/api/v1/content/itm_demo_sched/reject \
    -H 'content-type: application/json' -d '{"reason":"标题起夸张了，改稿再走一遍"}'
{ "ok": true, "data": {
    "item": { "id": "itm_demo_sched", "status": "rejected", "scheduled_at": null,
              "awaiting_confirm": false, "…": "其余 ContentRow 字段" },
    "message": "已驳回，不会发出去"
}, "error": null }
```

---

## 8. 选题

### `GET /api/v1/topics`

参数：`used`（`true` = 已经写成稿子的 / `false` = 还没用过）、`source`、`limit`、`offset`。

```console
$ curl -s http://127.0.0.1:8123/api/v1/topics
{
    "ok": true,
    "data": {
        "items": [
            {
                "id": "top-demo-1",
                "source": "newsnow",
                "title": "通勤成本上涨怎么省",
                "url": "https://example.invalid/t/1",
                "score": 8.5,
                "created_at": "2026-08-16T15:24:25.073389Z",
                "used": true,
                "dismissed": false,
                "dismissed_at": null,
                "dismissed_by": "",
                "raw": { "info": "12.3万热度" }
            }
        ],
        "total": 1, "limit": 50, "offset": 0
    },
    "error": null
}
```

- `used` 是**推导**出来的（有没有 `ContentItem.topic_id` 指向它），`Topic` 表没有这一列
- `raw` 是采集器留的原始字段（热度、榜单名…），可直接展示

### `POST /api/v1/topics/{id}/dismiss`

Body：`{"actor": "operator", "reason": "两周前的旧闻", "dismissed": true}`（传 `dismissed: false` 撤销）。

```console
$ curl -s -X POST http://127.0.0.1:8123/api/v1/topics/top-demo-1/dismiss \
    -H 'content-type: application/json' -d '{"actor":"operator","reason":"两周前的旧闻"}'
{
    "ok": true,
    "data": {
        "id": "top-demo-1", "source": "newsnow", "title": "通勤成本上涨怎么省",
        "url": "https://example.invalid/t/1", "score": 8.5,
        "created_at": "2026-08-16T15:24:25.073389Z",
        "used": true, "dismissed": true,
        "dismissed_at": "2026-08-16T15:24:53.104Z", "dismissed_by": "operator",
        "raw": { "info": "12.3万热度" }
    },
    "error": null
}
```

> ⚠️ 标记写在 `Topic.raw['dismissed']`（模型不加列）。**当前选题 Agent 还不读这个标记**，
> 所以它现在只影响工作台展示，不影响自动选题——界面上别把它写成"以后不会再选它"。

---

## 9. 任务

### 9.1 `GET /api/v1/jobs/render`

参数：`state`（`pending` / `running` / `done` / `failed` / `lost`）、`limit`、`offset`。

```console
$ curl -s http://127.0.0.1:8123/api/v1/jobs/render
{
    "ok": true,
    "data": {
        "items": [
            {
                "id": "rjb_demo_1",
                "content_item_id": "itm_demo_video",
                "title": "通勤成本上涨怎么省 · 口播",
                "provider": "mpt",
                "task_id": "9f2c1a7b-2e11-4a1d-9f0a-2b6b1c0d3e4f",
                "state": "running",
                "progress": 45,
                "attempts": 0,
                "result_paths": [],
                "last_error": null,
                "meta": { "stage": "materials", "duration_s": 42 },
                "created_at": "2026-08-16T15:24:25.076637Z",
                "updated_at": "2026-08-16T15:24:25.076638Z"
            }
        ],
        "total": 1, "limit": 50, "offset": 0
    },
    "error": null
}
```

`state=lost` 表示渲染 sidecar 重启把任务表丢了（这是它的已知行为），需要人决定要不要重跑生成链。

### 9.2 `GET /api/v1/jobs/publish_records`

参数：`phase`（`in_flight` / `done` / `failed`）、`account_id`、`limit`、`offset`。

```console
$ curl -s http://127.0.0.1:8123/api/v1/jobs/publish_records
{
    "ok": true,
    "data": {
        "items": [
            {
                "id": "pub_demo_1",
                "content_item_id": "itm_demo_done",
                "account_id": "xhs-demo-01",
                "platform": "xhs",
                "title": "地铁通勤 30 分钟能做什么",
                "idem_key": "a1b2c3d4e5",
                "phase": "done",
                "platform_post_id": "65f0c0de000000001203a1b2",
                "url": "https://www.xiaohongshu.com/explore/65f0c0de000000001203a1b2",
                "attempts": 1,
                "last_error": null,
                "created_at": "2026-08-16T15:24:25.075647Z",
                "updated_at": "2026-08-16T15:24:25.075648Z"
            }
        ],
        "total": 1, "limit": 50, "offset": 0
    },
    "error": null
}
```

`idem_key` 唯一，是幂等的唯一真相来源；`phase=in_flight` 表示"发起了但还不知道结果"。

### 9.3 `GET /api/v1/jobs/dead_letters`

```json
{ "ok": true, "data": {
    "items": [
        { "item_id": "itm_…", "account_id": "xhs-demo-01", "title": "…",
          "at": "2026-08-16T…Z", "reason": "重试 3 次仍失败: 502" }
    ],
    "total": 1, "limit": 50, "offset": 0
}, "error": null }
```

`reason` 取最后一条 `dead_letter` 审计日志（截断到 200 字）。要复投走 `retry_now`。

---

## 10. 统计与成本

### 10.1 `GET /api/v1/stats?days=7`

复用 `/stats.json` 的同一份聚合，另加 `daily` 序列供画图。

```console
$ curl -s 'http://127.0.0.1:8123/api/v1/stats?days=3'
{
    "ok": true,
    "data": {
        "window_days": 3,
        "day": "2026-08-16",
        "generated_at": "2026-08-16T15:24:52.258408Z",
        "totals": { "published": 1, "failed": 0, "dead_letter": 0,
                    "pending_review": 2, "scheduled": 1,
                    "snapshots_24h": 0, "snapshots_7d": 0 },
        "budget": { "tokens": { "used": 20532.0, "limit": 2000000.0, "remaining": 1979468.0 },
                    "render_seconds": { "used": 42.0, "limit": 3600.0, "remaining": 3558.0 } },
        "accounts": [
            {
                "id": "xhs-demo-01", "platform": "xhs", "status": "ok",
                "daily_limit": 5, "daily_target": 1,
                "publish_windows": "09:00-11:00、19:00-22:00", "min_interval_minutes": 120,
                "published": 1, "failed": 0, "dead_letter": 0,
                "pending_review": 1, "scheduled": 1, "suspended": 0,
                "used_today": 1, "last_published_at": "2026-08-16T15:24:25.075648+00:00",
                "metrics": { "views": null, "likes": null, "comments": null,
                             "shares": null, "collects": null, "follows": null },
                "measured_posts": 0, "snapshots_24h": 0, "snapshots_7d": 0,
                "cost": { "tokens": 18432.0 }, "insights_at": ""
            }
        ],
        "dead_letters": [],
        "needs_attention": ["douyin-demo-01"],
        "unattributed_cost": { "tokens": 2100.0 },
        "content_counts": { "draft": 2, "published": 1, "scheduled": 1 },
        "publish_counts": { "done": 1 },
        "daily": [
            { "day": "2026-08-14", "published": 0, "dead_letter": 0, "cost": {} },
            { "day": "2026-08-15", "published": 0, "dead_letter": 0, "cost": {} },
            { "day": "2026-08-16", "published": 1, "dead_letter": 0,
              "cost": { "render_seconds": 42.0, "tokens": 20532.0 } }
        ]
    },
    "error": null
}
```

口径（与 `core/stats.py` 一致，别自己换算）：

- 窗口按 `PublishRecord.updated_at`（phase 置 done 的时刻）
- `metrics` 取每条内容**最新一张**快照再求和；**`null` 不是 0**，是"这个平台/这条内容没有该字段"
  （小红书的 `views` 永远是 `null`），界面上显示 `—`
- `accounts[].cost` 靠 `CostLedger.meta['account_id']` 归集，没标签的进 `unattributed_cost`
- `accounts[].last_published_at` 这几个字段来自既有 `as_dict()`，时区写法是 `+00:00`（同样是 UTC）

### 10.2 `GET /api/v1/costs?days=30`

```console
$ curl -s 'http://127.0.0.1:8123/api/v1/costs?days=3'
{
    "ok": true,
    "data": {
        "days": 3,
        "since_day": "2026-08-14",
        "today": "2026-08-16",
        "budget": { "tokens": { "used": 20532.0, "limit": 2000000.0, "remaining": 1979468.0 },
                    "render_seconds": { "used": 42.0, "limit": 3600.0, "remaining": 3558.0 } },
        "by_day": [
            { "day": "2026-08-14", "published": 0, "dead_letter": 0, "cost": {} },
            { "day": "2026-08-15", "published": 0, "dead_letter": 0, "cost": {} },
            { "day": "2026-08-16", "published": 1, "dead_letter": 0,
              "cost": { "render_seconds": 42.0, "tokens": 20532.0 } }
        ],
        "by_account": [
            { "account_id": "douyin-demo-01", "name": "抖音 Demo 01", "platform": "douyin",
              "cost": { "render_seconds": 42.0 } },
            { "account_id": "xhs-demo-01", "name": "小红书 Demo 01", "platform": "xhs",
              "cost": { "tokens": 18432.0 } }
        ],
        "unattributed": { "tokens": 2100.0 },
        "totals": { "render_seconds": 42.0, "tokens": 20532.0 }
    },
    "error": null
}
```

`budget` 只反映**今天**（闸门按 UTC 日重置）；`by_day` / `by_account` 覆盖 `days` 窗口。
成本单位：`tokens` 是 token 数，`render_seconds` 是渲染秒数——不是钱。

---

## 11. 复盘

### `GET /api/v1/insights?account_id=`

读 `prompts/accounts/<id>/insights.md` 并拆成条目（**新的在最上面**）。

```console
$ curl -s http://127.0.0.1:8123/api/v1/insights
{
    "ok": true,
    "data": [
        {
            "account_id": "xhs-demo-01",
            "name": "小红书 Demo 01",
            "platform": "xhs",
            "updated_at": null,
            "error": "",
            "path": "/…/prompts/accounts/xhs-demo-01/insights.md",
            "exists": false,
            "entries": []
        }
    ],
    "error": null
}
```

有内容时每个 `entries[]` 是：

```json
{
    "account_id": "xhs-demo-01",
    "date": "2026-08-16",
    "title": "近 7 天复盘（xhs-demo-01）",
    "headline": "通勤话题最稳，周三晚 19:30 的表现明显更好",
    "markdown": "## 2026-08-16 · 近 7 天复盘（xhs-demo-01）\n\n**通勤话题最稳…**\n\n- 置信度：`medium`\n…"
}
```

`markdown` 是原文，前端直接渲染即可（`date` 是日期串不是时间戳）。`updated_at` / `error` 来自
`Account.extra`（复盘 Agent 上次写盘时刻 / 上次失败原因）。

### `POST /api/v1/insights/run`

Body（可选）：`{"account_id": null, "force": false}`。触发 `tick_insights`（与定时任务同一个函数）。

```json
{ "ok": true, "data": {
    "tick": "insights",
    "stats": { "scanned": 2, "written": 0, "skipped_sample": 0,
               "skipped_not_due": 0, "skipped_no_key": 2, "failed": 0 },
    "elapsed_s": 0.004,
    "message": "未配置 LLM 凭据，本轮整体跳过"
}, "error": null }
```

- 每个账号内部有 **24 小时节流**，`force: true` 跳过它
- 没配 LLM 凭据时**不会**回落到假模型，整体跳过（`skipped_no_key`）——复盘是长期资产，
  宁可空着也不要被预置假文本污染
- 真跑起来会调 LLM，**可能几十秒**，前端要给 loading 态

---

## 12. 系统

### 12.1 `GET /api/v1/system/info`

```console
$ curl -s http://127.0.0.1:8123/api/v1/system/info
{
    "ok": true,
    "data": {
        "version": "0.1.0",
        "env": "dev",
        "time": "2026-08-16T15:24:52.802245Z",
        "timezone": "Asia/Shanghai",
        "llm_backend": "anthropic",
        "llm_model": "claude-opus-5",
        "database": "sqlite:////…/demo.db",
        "scheduler_enabled": false,
        "use_fake_publishers": true,
        "generate_enabled": true,
        "publishers": ["douyin", "wechat_mp", "xhs"],
        "ticks": ["generate", "insights", "login_health", "metrics",
                  "render_jobs", "retry_sweep", "scheduled_publish", "sourcing"],
        "platforms": ["wechat_mp", "xhs", "douyin"],
        "content_statuses": ["topic", "drafting", "draft", "reviewing", "rejected", "approved",
                             "scheduled", "suspended", "publishing", "published", "measured",
                             "publish_failed", "retrying", "dead_letter"],
        "review_queue_statuses": ["draft", "reviewing", "rejected"],
        "auth_required": false,
        "budget": { "daily_token_budget": 2000000.0, "daily_render_seconds_budget": 3600.0,
                    "daily_image_budget": 40.0 }
    },
    "error": null
}
```

- `use_fake_publishers: true` 时**什么都不会真的发出去**——界面上建议挂个显眼的角标
- `platforms` / `content_statuses` / `review_queue_statuses` 是**筛选下拉框的取值域**，
  前端从这里取，别自己抄一份枚举（后端加状态时你不用改代码）
- `publishers` 是**已注册**的发布器（可能被 fake 覆盖）；`database` 里的密码已打码

### 12.2 `GET /api/v1/system/imagegen`

生图（P11）能不能用 + 今天用了几张。**不发任何网络请求**：只看配置、本进程内的熔断标记
和今天的账本，所以可以放进页面加载路径，不会因为探测把出稿的钱花掉。

```console
$ curl -s http://127.0.0.1:8123/api/v1/system/imagegen
{
    "ok": true,
    "data": {
        "ready": true,
        "enabled": "auto",
        "model": "gpt-image-2",
        "base_url": "https://<私有网关域名>/v1",
        "has_api_key": true,
        "reason": "",
        "hint": "",
        "used_today": 4.0,
        "daily_limit": 40.0,
        "remaining": 36.0,
        "default_count": 2
    },
    "error": null
}
```

- `ready: false` 时 `reason` **一定非空**。界面要把它**原样显示**给人看，不许自己编一句
  "暂不可用"——人得知道是开关关了、key 没配、还是分组没开图像生成权限。
  `hint` 是可执行的修复指引（去哪儿开、改哪个变量），非空就一起显示
- `enabled` ∈ `auto` / `true` / `false`（`SW_IMAGEGEN_ENABLED`）。`auto` 是默认：
  **启动不探测**，首次调用失败就在本进程内熔断，此时 `ready` 转 `false` 且 `reason` 会写明
  "本次运行里已经失败过一次"。改完配置重启 core 即可恢复
- `used_today` / `daily_limit` / `remaining` 按**张**计（`DAILY_IMAGE_BUDGET`，默认 40）。
  `remaining <= 0` 时前端应禁用配图开关并说清楚是额度问题
- `default_count` 是出稿弹层里配图张数的默认值（`SW_GENERATE_ILLUSTRATIONS`）

### 12.3 `GET /api/v1/system/ticks` / `POST /api/v1/system/ticks/{name}`

```console
$ curl -s http://127.0.0.1:8123/api/v1/system/ticks
{
    "ok": true,
    "data": {
        "ticks": [
            { "name": "generate",          "accepts": ["account_id", "platform"] },
            { "name": "insights",          "accepts": ["account_id", "force"] },
            { "name": "login_health",      "accepts": ["platform", "force"] },
            { "name": "metrics",           "accepts": ["respect_windows"] },
            { "name": "render_jobs",       "accepts": [] },
            { "name": "retry_sweep",       "accepts": [] },
            { "name": "scheduled_publish", "accepts": [] },
            { "name": "sourcing",          "accepts": ["platform"] }
        ],
        "note": "手动触发与定时任务走的是同一批函数（core.scheduler.TICKS）"
    },
    "error": null
}
```

触发（参数走 **query string**，不是 body）：

```console
$ curl -s -X POST http://127.0.0.1:8123/api/v1/system/ticks/scheduled_publish
{
    "ok": true,
    "data": {
        "tick": "scheduled_publish",
        "stats": { "scanned": 1, "published": 0, "skipped": 1, "failed": 0,
                   "skipped_account": 1, "skipped_window": 0, "skipped_rate": 0,
                   "skipped_unconfirmed": 0, "skipped_publisher": 0,
                   "skipped_not_advanced": 0 },
        "elapsed_s": 0.001
    },
    "error": null
}
```

`stats` 的键**因 tick 而异**（上面是发布 tick 的全部十个键：四个总计 + 六个 `skipped_*` 明细，
与六道闸门一一对应，`scanned == published + skipped + failed` 恒成立。逐道见 `docs/OPS.md` 1.6），
前端按 key/value 表格渲染即可。
传了该 tick 不认的参数 → 422 `invalid_tick_param`；未知 tick → 404；内部异常 → 500 `tick_failed`。

各 tick 的耗时差别很大：`scheduled_publish` / `retry_sweep` 毫秒级，`generate` / `insights`
要调 LLM（几十秒），`sourcing` 要拉外网热榜。

### 12.4 `GET /api/v1/system/preflight?offline=true`

跑 `scripts/preflight.py` 的全部检查并结构化返回。

```console
$ curl -s 'http://127.0.0.1:8123/api/v1/system/preflight?offline=true'
{
    "ok": true,
    "data": {
        "offline": true,
        "passed": false,
        "counts": { "WARN": 8, "OK": 5, "FAIL": 2, "SKIP": 7 },
        "checks": [
            { "name": ".env 文件", "status": "WARN",
              "detail": "不存在，将只从进程环境变量读取；可 cp .env.example .env" },
            { "name": "数据库", "status": "OK", "detail": "sqlite:////…/demo.db（目录可写）" },
            { "name": "Anthropic API Key", "status": "FAIL",
              "detail": "ANTHROPIC_API_KEY 未配置，内容生成不可用" },
            { "name": "dsh 后端", "status": "SKIP", "detail": "SW_LLM_BACKEND=anthropic，未启用 dsh" }
        ],
        "ran_at": "2026-08-16T15:24:53.9Z"
    },
    "error": null
}
```

- `status` ∈ `OK` / `WARN` / `FAIL` / `SKIP`；`passed` = 没有任何 `FAIL`
- `offline=false` 会真去探公众号 / MPT / sidecar，**可能十几秒**
- 即便 `offline=true`，docker 探测仍会执行（`docker info` 最多 15 秒）
- **别放进轮询**，做成"点一下才跑"的按钮

### 12.5 `GET /api/v1/system/telegram`

提醒渠道（Telegram，P12）的连通状态。**不发任何网络请求**：只看配置 + 本进程内的长轮询线程状态
（同 [`/system/imagegen`](#122-get-apiv1systemimagegen)），所以可以放进页面加载路径。
真要探活用 `uv run python -m core.telegram check`（见 `docs/OPS.md`）。

```console
$ curl -s http://127.0.0.1:8123/api/v1/system/telegram
{
    "ok": true,
    "data": {
        "enabled": false,
        "configured": false,
        "ready": false,
        "chat_configured": false,
        "can_sign": false,
        "polling": false,
        "username": "",
        "sent": 0,
        "failed": 0,
        "stats": {},
        "detail": "SW_TELEGRAM_ENABLED=false，Telegram 通道整体关闭",
        "last_error": ""
    },
    "error": null
}
```

- **绝不含 token**，脱敏过的指纹也不给
- `ready=false` 时 `detail` **一定非空**，而且是一句能照着做的话——前端把它原样显示，
  不要自己编"未配置"三个字。工作台的"提醒渠道"面板（`ui/components/system/reminder-channel.tsx`）
  按 `configured`（有没有 token）→ `enabled`（总开关）→ `chat_configured`（知不知道推给谁）→
  `can_sign`（有没有签名密钥）→ `polling`（长轮询活着没）的顺序给出下一步；先看 token 是因为
  建 bot、拿 token 是接入 Telegram 天然的第一步（见 `docs/OPS.md`），没 token 时单说"把开关打开"
  是一句瘸腿的指引
- `configured` = 有 bot token；`ready` = token 与 `chat_id` 都有，能真的推出去
- `chat_configured` = 有没有人给 bot 发过 `/start`（系统才知道该推到哪个会话）
- `can_sign` = 有没有 callback 签名密钥；没有就只发纯文字提醒，不带按钮
- `stats` 是长轮询统计：`polls` / `updates` / `handled` / `rejected` / `errors`
- 这一块**没有对应的写端点**：改配置只能改 `.env` 再重启 core，工作台不提供"在线配置 Telegram"的表单
  （凭据只走环境变量，不入库、不进前端，见 `docs/POLICY.md`）

---

## 13. 媒体（复用既有端点）

JSON API **不另开**媒体端点，直接用既有的两个（它们返回的是文件流，不是 envelope）：

| 端点 | 用途 |
|---|---|
| `GET /review/{item_id}/cover` | 封面原图。等价于 ContentRow 的 `cover_url` |
| `GET /review/{item_id}/media/{index}` | 按下标取媒体原文件（小红书一条笔记 4–9 张卡片；抖音成片是 mp4，`Content-Type: video/mp4`，`<video controls>` 能直接播） |
| `GET /review/{item_id}/preview` | 公众号 `body_html` 原文，**必须放进 sandbox iframe**（wenyan 产出的是整段内联样式 HTML，直接插进页面会污染样式） |

三点注意：

1. 这三个端点**不在 `/api/v1` 下，因此不受 token 认证保护**——`<img src>` / `<video src>` 没法带
   Authorization 头，这是刻意的取舍。真要暴露公网，请在反向代理上给它们单独加保护
2. 文件不存在 / 越界返回 404（响应体是 `{"detail": "..."}` 而不是 envelope）
3. 只允许读工作目录内的文件（防目录穿越），`bundle.media[].exists=false` 时不要发请求

---

## 14. 轮询建议

| 页面 | 端点 | 建议 |
|---|---|---|
| 首页看板 | `GET /dashboard` | **5 秒**轮询。一个请求覆盖全部计数与事件流，别再并发拉别的 |
| 审核队列 | `GET /review` | **10 秒**轮询（或只在窗口聚焦时）。人正在看列表时刷新会打断操作，建议只更新计数、由用户点"有 N 条新内容"再刷 |
| 审核详情 | `GET /review/{id}` | **不轮询**。写操作的响应里已经带了最新的 `item` |
| 内容 / 时间线 | `GET /content` | **不轮询**，切换筛选时拉 |
| 登录页 | `GET /accounts/{id}/login/status` | **3 秒**轮询，直到 `logged_in: true`；二维码按 `expires_in` 到期再取新的 |
| 渲染任务 | `GET /jobs/render?state=running` | **5 秒**轮询，只在有进行中的任务时开；全部结束就停 |
| 发布记录 / 死信 | `GET /jobs/*` | **不轮询** |
| 统计 / 成本 | `GET /stats`、`GET /costs` | **不轮询**（数据以小时计变化），给个手动刷新按钮 |
| 复盘 | `GET /insights` | **不轮询** |
| 系统门禁 | `GET /system/preflight` | **绝不轮询**，点击才跑 |
| 账号列表 | `GET /accounts` | 30 秒或跟随看板 |

通用建议：写操作（approve / reject / edit / reschedule / retry_now / dismiss）的响应里都带着**更新后的行**，
直接用它替换本地状态，不要马上再拉一次列表。

---

## 15. 已知限制

1. **机器审核的逐条 finding 没有结构化持久化。** `review/pipeline.py` 只把摘要写进
   `ContentItem.review_notes`（文本）、把计数写进 `ReviewLog.after_json`。所以
   `machine_review.notes` 是**按行拆开的文本**，不是 `{level, rule, excerpt, suggestion}` 对象数组。
   要结构化得改写入侧（P7）。
2. **选题的 `dismissed` 标记只影响展示**，`sourcing/selector.py` 还不读它。
3. **`retry_now` 对 `retrying` 只解退避**，不能绕过账号健康 / 限频 / 48 小时超龄；账号掉线时点它没用，
   要先去扫码。
4. **死信不可原地复活**（P0 冻结的状态机），只能复投成新 draft，`id` 会变。
5. **成本单位是 token 数与渲染秒数，不是金额**——没有汇率表，界面别写 "¥"。
6. **`/system/preflight` 慢**（docker 探测最多 15 秒，联网模式更久），且它会重新初始化一次 DB 引擎；
   不要并发调用。
7. **媒体端点不受 token 保护**（见上一节）。
8. **没有 WebSocket / SSE**，实时性靠轮询。
9. **没有跨域配置**：默认没开 CORS，前端要么同源部署（把静态导出的产物挂在同一个反代下），
   要么自己在反代上加 CORS 头。
10. **`limit` 上限 200**，超过要自己翻页。
11. **sidecar 的 docker 驱动要显式打开。** `SW_SIDECAR_DRIVER` 默认是 `none`（只记账、不起容器），
    此时 [6.12](#612-post-apiv1accountsidsidecaraction) 的三个动作一律 502 `sidecar_error`。
    要让工作台真的接管容器，得在 core 那台机器上设 `SW_SIDECAR_DRIVER=docker` **并且**配好
    `SW_XHS_MCP_IMAGE`。上游镜像**只发 amd64**，aarch64 服务器要先从上游源码本地 `docker build`
    再把镜像名填进去。
12. **`POST /accounts` 会回写 `accounts.yaml`**（路径可用 `SW_ACCOUNTS_FILE` 覆盖），
    所以跑 core 的进程需要对这个文件有写权限，且这个文件应该是**可版本管理**的——
    它是账号台账的唯一真相，库只是它的投影。只读挂载或多实例并发写会让写入失败
    （失败时台账会回滚成动手之前的字节，不会留下半条记录）。
