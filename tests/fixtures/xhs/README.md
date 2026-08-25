# tests/fixtures/xhs — xiaohongshu-mcp 响应样例

按上游 `xpzouying/xiaohongshu-mcp`（Apache-2.0）**main 分支源码**手写，
不是抓包录制（sidecar 要真账号才跑得起来，录制会带上真实 cookies/xsec_token）。

字段出处（2026-08-15 核对）：

| 文件 | 对应接口 | 结构出处 |
|---|---|---|
| `health.json` | `GET /health` | `handlers_api.go:healthHandler` |
| `login_status_ok.json` / `login_status_logged_out.json` | `GET /api/v1/login/status` | `service.go:LoginStatusResponse` |
| `login_qrcode.json` | `GET /api/v1/login/qrcode` | `service.go:LoginQrcodeResponse`（`img` 是页面 `src` 原文，带 data URI 前缀） |
| `publish_ok.json` | `POST /api/v1/publish` | `service.go:PublishResponse`（**注意没有 note_id**） |
| `user_me.json` / `user_me_empty.json` | `GET /api/v1/user/me` | `service.go:UserProfileResponse` + `xiaohongshu/types.go:Feed`（双层 `data`） |
| `feed_detail.json` | `POST /api/v1/feeds/detail` | `xiaohongshu/types.go:FeedDetailResponse`（双层 `data`） |
| `error_*.json` | 任意接口的失败分支 | `types.go:ErrorResponse` + `middleware.go:respondError` |

外层统一是 `{"success": bool, "data": ..., "message": str}`（`types.go:SuccessResponse`），
失败时是 `{"error": str, "code": str, "details": any}` 且 HTTP 4xx/5xx。

计数字段一律是**字符串**（`"1.2万"`），由 `publishers/xhs/client.py:parse_count` 折算。
`xsecToken` 在这里是假值，日志里会被 `redact()` 抹掉。
