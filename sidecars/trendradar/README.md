# sidecars/trendradar — 热榜聚合 sidecar（TrendRadar）

上游：[`sansan0/TrendRadar`](https://github.com/sansan0/TrendRadar)，**GPL-3.0**。
本目录**不放其源码**，只用官方镜像 `wantcat/trendradar`（Docker Hub）。

> **License 红线**：GPL 项目只能作独立进程，core 通过 HTTP 读它**产出的数据文件**，
> 绝不 import、绝不复制其代码（`docs/POLICY.md`、`docs/THIRD_PARTY.md`）。
> 客户端在 `sourcing/trendradar.py`。

## 先知道这一件事：它没有 REST API

写客户端前最容易踩的坑。上游 2026-08-16（master，版本 `6.10.0`）核对结果：

| 端口 | 是什么 | 能不能当 API 用 |
|---|---|---|
| **8080** | `python -m http.server` 把 `/app/output` 目录挂出来（`docker/manage.py`） | 只能 GET 静态文件；无鉴权、无路由、无 JSON 端点 |
| **3333** | `wantcat/trendradar-mcp` 的 FastMCP Streamable HTTP，路径 `/mcp` | MCP JSON-RPC，需要 MCP 客户端握手；**本项目不用** |

抓取本身是 **supercronic 定时任务**（默认 `*/30 * * * *`）跑 `python -m trendradar`，
结果写进 `output/`。所以我们的做法是：**HTTP GET 它产出的文件**。

### 输出布局（已核实，注意不是老版本的中文日期目录）

```
output/news/{YYYY-MM-DD}.db        SQLite，热榜主数据 ← 我们读这个
output/rss/{YYYY-MM-DD}.db         RSS
output/txt/{YYYY-MM-DD}/{HH-MM}.txt  文本快照 ← 退路
output/html/latest/{daily|incremental|current}.html
output/index.html
```

`news_items` 表结构（上游 `trendradar/storage/schema.sql`）：

```sql
news_items(id, title, platform_id, rank, url, mobile_url,
           first_crawl_time, last_crawl_time, crawl_count, created_at, updated_at)
platforms(id, name, is_active, updated_at)
```

`sourcing/trendradar.py` 有两种模式，由 `TRENDRADAR_MODE` 控制：

- `db`（默认，`auto` 也先试它）：`GET {base}/news/{date}.db`，路径**完全确定**，
  不用解析目录列表；下载到临时文件后用标准库 `sqlite3` 读。
- `txt`：`GET {base}/txt/{date}/` 取目录列表 → 拿最新的 `{HH-MM}.txt` → 按行解析。

## 起停

```bash
cp sidecars/trendradar/config.example.yaml sidecars/trendradar/config/config.yaml
cp sidecars/trendradar/frequency_words.example.txt sidecars/trendradar/config/frequency_words.txt
docker compose --profile sourcing up -d trendradar
```

**两个配置文件都必须存在**，否则上游 `entrypoint.sh` 直接 `exit 1`：

```sh
if [ ! -f "/app/config/config.yaml" ] || [ ! -f "/app/config/frequency_words.txt" ]; then
    echo "❌ 配置文件缺失"; exit 1; fi
```

然后在 `.env` 里：

```bash
TRENDRADAR_BASE_URL=http://localhost:8080      # core 在宿主机上
# TRENDRADAR_BASE_URL=http://trendradar:8080   # core 在 compose 里
```

验证：

```bash
curl -s http://localhost:8080/                       # 目录列表
curl -sI http://localhost:8080/news/$(date -u +%F).db  # 200 = 今天的库已经生成
uv run python -c "from sourcing import trendradar as t; print(len(t.fetch(limit=5)))"
```

刚起的容器 `output/` 是空的，`GET .../news/{date}.db` 会 404。这是**正常**的：
等一个 `CRON_SCHEDULE` 周期，或把 `IMMEDIATE_RUN=true` 打开让它立刻跑一次。
`sourcing.trendradar` 会把 404 报成看得懂的错误，`tick_sourcing` 只把它记进
warnings，不阻断其它数据源。

## 环境变量（只列我们关心的）

| 变量 | 默认 | 说明 |
|---|---|---|
| `TZ` | `Asia/Shanghai` | 决定 `output/` 里的日期目录名 |
| `WEBSERVER_PORT` | 8080 | 静态文件服务端口 |
| `RUN_MODE` | `cron` | `once` = 跑一次就退出 |
| `CRON_SCHEDULE` | `*/30 * * * *` | 抓取频率 |
| `IMMEDIATE_RUN` | `true` | 启动时立刻抓一次 |

其余全是通知渠道（飞书 / TG / 钉钉 / 企微 / 邮件 / Bark / Slack）、AI 摘要
（`AI_API_KEY` 等）与 S3 存储——**本项目一个都不配**：通知走 core 自己的
`core/notify.py`，摘要由我们的 Claude 链做，避免两套 LLM 配置。

## 它的数据从哪来（重要）

TrendRadar **自己不爬任何平台**，它转手调 newsnow 的公开 API
（上游 `trendradar/crawler/fetcher.py`：`DEFAULT_API_URL = "https://newsnow.busiyi.world/api/s"`）。

所以：

- 本源与 `sourcing/newsnow.py` **天然重叠**。跨源去重由 `sourcing.base.persist_topics` 兜住。
- 它的价值在于**加工**（按天累积、`crawl_count` 持续热度、关键词过滤），不是覆盖面。
- 只想要原始热榜的话，直接配 `NEWSNOW_BASE_URL` 更省一个容器。

默认监控 **11** 个平台（上游 `config/config.yaml`）：今日头条、百度热搜、华尔街见闻、
澎湃新闻、B 站热搜、财联社热门、凤凰网、贴吧、微博、抖音、知乎。

## 未核实

- MCP transport（3333）的实际握手报文——只读了源码里的 `mcp.run(transport='http', path='/mcp')`，
  没起容器实测。本项目不走这条路，所以没有进一步核。
- 可选平台的**总数**（上游 README 指向 issue #95 的汇总表，未读）。总池由 newsnow 决定。
