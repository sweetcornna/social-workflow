# tests/fixtures/douyin

宿主机抖音上传器（`publishers/douyin/service.py`）的 HTTP 响应样例，供
`tests/publishers/test_douyin.py` 用 respx 打桩。

**这些是我们自己定义的服务契约**，不是抖音官方接口——上传器是本仓库写的，
envelope 形状（`{"ok","state","detail",...}`）与 `state` 取值都定义在
`publishers/douyin/client.py` 顶部。改服务端返回结构时这里要同步改。

| 文件 | 对应端点 / 场景 |
|---|---|
| `health.json` | `GET /health` |
| `login_status_ok.json` | `GET /accounts/{id}/login/status`，已登录 |
| `login_status_logged_out.json` | 同上，未登录（→ `NeedsReloginError` / `needs_relogin`） |
| `login_status_needs_sms.json` | 同上，卡在短信二次验证（→ 需真人处理） |
| `login_start_waiting.json` | `POST /accounts/{id}/login/start` |
| `publish_ok.json` | `POST /accounts/{id}/publish` 成功并拿到作品 id |
| `publish_no_post_id.json` | 发布成功但没解析出作品 id（→ 占位 id，**不重发**） |
| `publish_needs_sms.json` | 发布中要求短信验证（→ `NeedsReloginError`） |
| `publish_identity_mismatch.json` | 页面昵称与 `identity_hint` 不符（→ `PermanentError`） |
| `publish_rejected.json` | 平台判违规（→ `PermanentError`） |
| `publish_busy.json` | 已有作业在跑（→ `RetryableError`） |
| `recent_posts.json` | `GET /accounts/{id}/recent_posts`，供对账 |
| `metrics.json` | `GET /accounts/{id}/metrics/{post_id}` |
| `fake_creator_center.html` | 假创作者中心页面，给 `-m browser` 的真实浏览器冒烟测试用 |

`fake_creator_center.html` 里的元素刻意只覆盖 `SELECTORS` 中**结构性**的几项
（昵称、file input、标题框、简介编辑器、发布按钮、验证码框），
用来验证"页面动作 + identity 闸门 + 截图落盘"这条逻辑链，
**不代表真实抖音页面的 DOM**——真实选择器未在真实站点验证。
