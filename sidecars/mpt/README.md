# sidecars/mpt — 视频合成 sidecar（MoneyPrinterTurbo）

上游：[`harry0703/MoneyPrinterTurbo`](https://github.com/harry0703/MoneyPrinterTurbo)，
**MIT**，Python + ffmpeg。本目录**不放其源码**，只用官方镜像 + 一份配置模板。

core 只通过 HTTP 调它（`generation/mpt_client.py`），不 import、不复制其代码。

## 我们只用它的什么

| 用 | 不用 |
|---|---|
| 素材检索（Pexels / Pixabay）+ 剪辑拼接 | 它内置的 LLM 写稿（`/api/v1/scripts`、`/api/v1/terms`） |
| TTS 配音（edge-tts） | 它的 Streamlit WebUI（8501，本项目不启这个服务） |
| 字幕生成与烧录 | 它的跨平台分发（`cross_post_*`） |
| 9:16 竖版输出（1080×1920） | 它的任务持久化——**任务表在进程内存里，重启就没** |

**脚本由 Claude 生成后经 `video_script` / `video_terms` 灌入**（计划 2.2 的"MPT 边界"）。
`video_script` 非空时上游会跳过自己的 LLM。两套 LLM 配置会导致"改了 prompt 却没生效"
这类查半天的问题，所以 `config.toml` 里的 `llm_provider` 一律留空。

## 上游关键事实（2026-08-16 对照 `main` 分支源码核对，HEAD `1f9f19c`）

| 项 | 值 | 出处 |
|---|---|---|
| 镜像 | `ghcr.io/harry0703/moneyprinterturbo:latest` | `docker-compose.release.yml` |
| API 进程 | `python3 main.py`（镜像的 `CMD` 是 **WebUI**，必须覆盖 `command`） | `Dockerfile` / compose |
| 端口 | **8080**（`listen_port`，顶层键） | `app/config/config.py` |
| 路由前缀 | `/api/v1`（写死在 `new_router`） | `app/controllers/v1/base.py` |
| 鉴权 | **默认关闭**（`Depends(base.verify_token)` 被注释掉了）；打开后走 `x-api-key` | `app/controllers/v1/video.py` |
| 任务状态 | `-1` 失败 / `1` 完成 / `4` 处理中。**没有排队态**，入队即 `4` | `app/models/const.py` |
| 未知 task | `GET /api/v1/tasks/{id}` → **404** `{"status":404,"message":"...: task not found"}` | `video.py` |
| 队列满 | `POST /api/v1/videos` → **429**，且刚建的任务记录被删（别去轮询那个 id） | `video.py` |
| 参数校验失败 | **HTTP 400**（不是 FastAPI 默认的 422），`data` 里是 pydantic 错误列表 | `app/asgi.py` |
| 成片链接 | `[app] endpoint` 留空时是**根相对路径** `/tasks/<id>/final-1.mp4` | `video.py:_task_file_to_uri` |
| 下载 | `GET /api/v1/download/<task_id>/final-1.mp4`（路径相对 `storage/tasks`） | `video.py` |
| 挂载 | `./config.toml:/MoneyPrinterTurbo/config.toml`、`./storage:/MoneyPrinterTurbo/storage` | `docker-compose.release.yml` |
| 健康检查 | **没有** `/health` 路由。我们用只读的 `GET /api/v1/tasks?page_size=1` 探活 | 路由表 |

> 上游 compose 把端口绑在 `127.0.0.1`。本项目的 `mpt` 服务**不发布端口到宿主机**，
> 只在 `social_workflow` 网络内可达（core 用 `http://mpt:8080` 访问）。要用 WebUI
> 或 `/docs` 调试时临时加一个 compose override 映射端口，别长期开着。

## 起停

```bash
# 1) 准备配置（config.toml 在 .gitignore 里，不会进 git）
cp sidecars/mpt/config.example.toml sidecars/mpt/config.toml
$EDITOR sidecars/mpt/config.toml     # 填 pexels_api_keys / pixabay_api_keys

# 2) 起容器（带 profile 的服务默认不启动）
docker compose --profile video up -d mpt
docker compose logs -f mpt

# 3) 门禁自检：会探 MPT 存活 + 素材源 key
uv run python scripts/preflight.py

# 4) 停
docker compose --profile video down
```

素材源 key 申请：[Pexels](https://www.pexels.com/api/) /
[Pixabay](https://pixabay.com/api/docs/)，都是免费自助。
**没配 key 渲染会在 `materials` 阶段失败**（`failed_stage="materials"`）。

## 联调

```bash
# 不起 sidecar 也能跑通全链路（挂 tests/fixtures/video/sample.mp4 样本片）
curl -X POST 'http://localhost:8000/dev/run_douyin_pipeline?account_id=douyin-demo-01&topic=通勤成本&skip_render=true'

# 真渲染（要 sidecar 起着 + 素材源 key）。一条片子几分钟起步
curl -X POST 'http://localhost:8000/dev/run_douyin_pipeline?account_id=douyin-demo-01&topic=通勤成本&skip_render=false'
```

返回体里的 `render.skip_render=true` 表示挂的是**样本片，不是真实成片**——审核页上也会
显式标出来，别把它当成可发布的产出。

## 任务丢失与重启

MPT 的任务表默认在**进程内存**里（`enable_redis = false`）。容器一重启，
`GET /api/v1/tasks/{id}` 就变 404。因此：

- core 侧把 `task_id` 落在 **`render_jobs`** 表（`core/models.py:RenderJob`）。
- 客户端把 404 映射成 `MptTaskLost`；生成链**原样重提交一次**，再丢就报错让人看。
- `core.scheduler.tick_render_jobs` 每分钟轮询进行中的任务，渲染完成后把成片下载到
  `data/media/<content_item_id>/video.mp4` 并挂回内容包（**仅限内容还没被人工批准**）。

开 Redis 只能让丢任务变少，**不能替代持久化**。

## 排障

| 现象 | 原因 / 处理 |
|---|---|
| `连不上 MoneyPrinterTurbo` | 容器没起（`docker compose --profile video ps`），或 `MPT_BASE_URL` 指错。容器内互访用 `http://mpt:8080`，宿主机直连要自己映射端口 |
| `failed_stage="materials"` | 素材源 key 没配 / 配额用完 / 检索词太抽象。检查 `config.toml` 的 `pexels_api_keys`，以及 `platform_extra.search_terms` 是不是具体可拍的英文名词 |
| `failed_stage="audio"` | edge-tts 出网失败。配 `[proxy]` 或换 `voice_name` |
| `MPT 任务队列已满`（429） | `max_concurrent_tasks` / `max_queued_tasks` 太小，或上一批任务卡住。`docker compose restart mpt` 会**清空任务表**，进行中的任务会变 `lost` |
| 成片是横屏 | 提交时 `video_aspect` 不是 `9:16`。`review.inspect` 会以 `douyin.video.aspect` block 掉它 |
| 字幕没烧上 | `subtitle_provider` 留空了。抖音静音播放占多数，字幕必须有 |
| 渲染很慢 | 正常，几分钟起步。`MPT_RENDER_TIMEOUT_SECONDS`（默认 1800）只是**管线等待上限**，超时不算失败，`tick_render_jobs` 会继续跟 |

## License

MIT，允许商用与修改。本项目只以**独立容器**使用它，不 import、不复制其源码；
从上游取用的只有"接口事实"（路由、字段名、状态码），已在
`generation/mpt_client.py` 的模块 docstring 与 `docs/THIRD_PARTY.md` 里注明出处。

**素材本身的授权与 MPT 的 License 无关**：Pexels / Pixabay 各有自己的内容许可，
见 `docs/THIRD_PARTY.md` 的"素材源条款"。
