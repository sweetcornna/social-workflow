# sidecars/xhs — 小红书发布 sidecar（xiaohongshu-mcp）

上游：[`xpzouying/xiaohongshu-mcp`](https://github.com/xpzouying/xiaohongshu-mcp)，
**Apache-2.0**，Go 单二进制 + 内置 Chromium。本目录**不放其源码**，只用官方镜像。

core 只通过 HTTP 调它（`publishers/xhs/client.py`），不 import、不复制其代码。

## 为什么必须一账号一容器

`xiaohongshu-mcp` 是**单进程单账号**：cookies 固定落在 `COOKIES_PATH`（默认
`/app/data/cookies.json`）。多个账号共用一个容器 = 共用一份登录态 = Cookie 池，
违反 `docs/POLICY.md` 的红线。所以：

**一账号 = 一容器 + 一独立 volume + 一独立宿主机端口**，由
`scripts/gen_xhs_sidecars.py` 从 `accounts.yaml` 生成 compose 片段。

另外：同一小红书账号**不允许多网页端同时登录**。你在浏览器里另开一次小红书网页版，
就会把 sidecar 里的会话顶下线（下一轮巡检会变 `needs_relogin`）。

## 上游关键事实（2026-08-15 对照 main 分支源码核对）

| 项 | 值 | 出处 |
|---|---|---|
| 镜像 | `xpzouying/xiaohongshu-mcp`（Docker Hub），本项目固定 `:v2.5.0` | `.github/workflows/docker-release.yml` |
| 国内镜像源 | `crpi-hocnvtkomt7w9v8t.cn-beijing.personal.cr.aliyuncs.com/xpzouying/xiaohongshu-mcp` | 上游 `docker/docker-compose.yml` |
| 容器内端口 | **18060**（`-port` 默认 `:18060`） | `main.go` |
| 无头 | `-headless` 默认 **true**，容器里保持默认 | `main.go` |
| 鉴权 | `-token` 或环境变量 `AUTH_TOKEN`；留空 = **不鉴权** | `main.go` / `middleware.go` |
| 鉴权范围 | `/mcp` 与 `/api/v1/*` 需 `Authorization: Bearer <token>`；`/health` 不需要 | `routes.go` |
| 数据目录 | `COOKIES_PATH` / `HOME` / `XDG_CONFIG_HOME` 都指向 `/app/data` | 上游 `docker/docker-compose.yml` |
| 浏览器 | 构建期预置在 `/app/cache`（`XDG_CACHE_HOME`），运行时零下载 | `Dockerfile` |
| 代理 | 环境变量 `XHS_PROXY`（http/https/socks5） | README |
| 素材 | `images` 支持 http(s) URL 与**容器内**本地路径 | `pkg/downloader/processor.go` |

> `AUTH_TOKEN` 留空只在"sidecar 仅监听 127.0.0.1"时可接受。生成的 compose 已经是
> `ports: "127.0.0.1:<host>:18060"`（`scripts/gen_xhs_sidecars.py` 的 `HOST_BIND_ADDRESS`，
> 与 `core/sidecars.py` 的 docker 驱动同口径，由 `tests/test_port_bindings.py` 钉住），
> 所以默认不对同网段暴露。即便如此**仍然务必配 token**（每个 sidecar 一个）：回环绑定
> 挡的是同网段的邻居，挡不住同一台机器上的其他进程/容器；而这个接口能以该账号的身份发笔记。
> 历史提醒：2026-08-23 之前这里生成的是裸 `"<host>:18060"`，确实绑在 0.0.0.0 上（见
> `docs/RISKS.md` 第 15 条）。

## 两种管法：compose 手工 vs 工作台接管（P10）

| | `SW_SIDECAR_DRIVER=none`（默认） | `SW_SIDECAR_DRIVER=docker` |
|---|---|---|
| 谁起容器 | 人，用 `docker-compose.xhs.yml` | 工作台账号页上的「启动 / 停止 / 重建」 |
| 端口 | `accounts.yaml` 里手写 | 建号时自动分配（18060 起找空闲） |
| 账号页显示 | 「sidecar 未接入」——**如实说**，不假装在起 | 真实容器状态 + `/health` 透传 |
| 适用 | 本机开发、CI、Playwright | 生产 |

`docker` 驱动拼出来的命令等价于（`core/sidecars.py`，有单测钉着参数）：

```bash
docker run -d --name sw-xhs-<account_id> --restart unless-stopped -t \
  -p 127.0.0.1:<port>:18060 \
  -v swxhs_<account_id>:/app/data \
  -e COOKIES_PATH=/app/data/cookies.json -e HOME=/app/data/home \
  -e XDG_CONFIG_HOME=/app/data/config -e TZ=Asia/Shanghai \
  -v <XHS_MEDIA_HOST_DIR 绝对路径>:/app/images:ro \
  --label social_workflow.platform=xhs --label social_workflow.account=<account_id> \
  "$SW_XHS_MCP_IMAGE"
```

两处要留意：

- **volume 挂 `/app/data`**，不是 `/data`。cookies 就在这个目录里（见上面的上游事实表），
  挂错地方的后果是"每次重启容器都要重新扫码"，而且不会报任何错。
- **只绑 `127.0.0.1`**。sidecar 不对外暴露，所以 `AUTH_TOKEN` 留空也不至于裸奔；
  配了 token 的账号用 `-e AUTH_TOKEN`（不带值）从 core 进程环境透传，token 不进命令行参数。

`recreate` 只删容器**不删 volume**，所以换镜像不用重新扫码。想彻底清登录态得手工
`docker volume rm swxhs_<account_id>` —— 那是破坏性动作，工作台上刻意不给按钮。

## aarch64 服务器：镜像要自己构建

上游 `xpzouying/xiaohongshu-mcp` 的 Docker Hub 镜像**只有 amd64**，且**源码原样
构建也不行**——上游 Dockerfile 写死 `GOARCH=amd64`，内置浏览器 CDN 也没有
linux-arm64 包（2026-08-17 生产实测：原样构建的容器 crash-loop，
`./app: 1: ELF: not found`）。

必须用 [`arm64/`](arm64/) 里的补丁 + 专用 Dockerfile 构建：给 `EnsureBrowser()`
加 `XHS_BROWSER_BIN` 环境变量覆盖，运行段用 Debian 的 arm64 Chromium。
步骤见 `arm64/README.md`；构建完把 `SW_XHS_MCP_IMAGE=xiaohongshu-mcp:v2.5.0-arm64`
填进 core 的 `.env`，已有容器在账号页点「重建」即可（volume 不动，登录态保留）。

## 首次登录流程

```bash
# 1. 生成 compose 片段（读 accounts.yaml 里 platform=xhs 的账号）
uv run python scripts/gen_xhs_sidecars.py
#    命令会把要填进 .env 的 XHS_MCP_ENDPOINTS / XHS_TOKEN_* / XHS_MCP_TOKENS 打到 stderr

# 2. 填 .env：每个 sidecar 一个随机 token（两处要一致）
#    XHS_TOKEN_XHS_DEMO_01=<openssl rand -hex 24>
#    XHS_MCP_TOKENS=xhs-demo-01=<同一个值>
#    XHS_MCP_ENDPOINTS=xhs-demo-01=http://localhost:18060
#    容器内访问时地址要写成 http://xhs-demo-01:18060（同一 compose 网络内用服务名）

# 3. 起容器（首次拉镜像 ~1GB，内含 Chromium）
docker compose -f docker-compose.yml -f docker-compose.xhs.yml config    # 先校验
docker compose -f docker-compose.yml -f docker-compose.xhs.yml up -d

# 4. 确认 sidecar 活着（/health 不需要 token）
curl -s http://localhost:18060/health

# 5. 打开 core 的登录页，用**该账号本人**的小红书 App 扫码
open http://localhost:8000/accounts/xhs-demo-01/login
```

扫码成功后：

- 页面轮询的 `/accounts/{id}/login/status` 会变成 `ok`；
- `core.state_machine.apply_health` 自动把账号从 `needs_relogin` 恢复为 `ok`，
  并把之前挂起的 `scheduled` 内容放回排期；
- 之后每 10 分钟由 `core.scheduler.tick_login_health` 巡检一次。

> 页面**不会**每 3 秒重取二维码：上游每取一次二维码就会关掉上一个正在等扫码的会话，
> 频繁重取会导致永远扫不上。二维码只在自身过期（默认 4 分钟）后才重取。

## 素材路径

core 生成的卡片图在宿主机 `data/media/<item_id>/*.png`，compose 把
`./data/media` 只读挂到容器 `/app/images`；发布时
`publishers/xhs/client.py:MediaPathMapper` 把宿主机绝对路径改写成 `/app/images/...`。

- 改挂载点要同步改 `XHS_MEDIA_CONTAINER_DIR`；
- sidecar 与 core 同机跑二进制（不用 Docker）时，把 `XHS_MEDIA_HOST_DIR` 置空，路径原样透传；
- 素材放到 `data/media` 之外会直接报 `PermanentError`（容器里看不到，不如早失败）。

## 排障

| 现象 | 处置 |
|---|---|
| `连不上 sidecar` | `docker compose ps` 看容器状态；确认 `XHS_MCP_ENDPOINTS` 端口与 compose 一致；容器内互访要用服务名而非 localhost |
| `HTTP 401 UNAUTHORIZED` | `AUTH_TOKEN` 与 `XHS_MCP_TOKENS` 两处值不一致 |
| 账号一直 `needs_relogin` | 有没有人在别处登了同一账号把它顶下线？重新扫码 |
| 扫码总是超时 | 别开多个登录页；每开一个新页面就会作废上一个二维码会话 |
| 发布报"图片文件不存在" | 素材不在 `data/media` 下，或没挂载 `/app/images` |
| 想彻底重置登录态 | `docker compose ... exec` 不必要，直接 `DELETE /api/v1/login/cookies`（**本项目代码不调它**，只作人工手段），或删掉该账号的 volume 后重扫 |
| 拉镜像慢 | `XHS_IMAGE=crpi-hocnvtkomt7w9v8t.cn-beijing.personal.cr.aliyuncs.com/xpzouying/xiaohongshu-mcp:v2.5.0` |

## 升级

上游是浏览器自动化，小红书改版就可能断。升级步骤：

1. 看上游 release note，改 `accounts.yaml` 里的 `sidecar.image` tag（或设 `XHS_IMAGE`）；
2. `uv run python scripts/gen_xhs_sidecars.py` 重新生成；
3. `docker compose ... up -d`（volume 不动，登录态保留）；
4. 跑一次 `uv run pytest tests/publishers/test_xhs.py -q` 确认契约没变；
5. `curl -s http://localhost:18060/health` + 登录页看状态。
