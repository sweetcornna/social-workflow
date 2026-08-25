# 第三方组件 License 台账

**策略**（来自计划一·约束 6）：

- **代码级依赖**只允许 MIT / Apache-2.0 / BSD / PSF / MPL-2.0 / WTFPL。
- **GPL / AGPL** 项目只能以**独立进程**出现（Docker sidecar，HTTP 调用），
  绝不 `import`、绝不复制源码、绝不与本仓库代码链接。
- **无 License / 禁商用**项目：只读参考其**行为流程**，不复制任何代码。

新增依赖必须同步更新本文件，否则审计打回。

---

## 1. Python 运行时依赖（`pyproject.toml [project.dependencies]`）

| 包 | License | 用途 |
|---|---|---|
| `fastapi` | MIT | 控制面 HTTP 服务 |
| `uvicorn[standard]` | BSD-3-Clause | ASGI server |
| `sqlalchemy` | MIT | ORM（SQLite） |
| `pydantic` | MIT | 发布契约 DTO |
| `pydantic-settings` | MIT | 环境变量配置 |
| `jinja2` | BSD-3-Clause | 审核 UI 模板 |
| `python-multipart` | Apache-2.0 | 表单解析（审核动作） |
| `httpx` | BSD-3-Clause | 出站 HTTP（sidecar / 官方 API / webhook） |
| `apscheduler` | MIT | 定时调度 |
| `python-dotenv` | BSD-3-Clause | .env 读取 |
| `pyyaml` | MIT | accounts.yaml 解析 |
| `rich` | MIT | preflight 表格输出 |
| `tzdata` | Apache-2.0 | 发布时段窗口按账号时区判定；slim 镜像不保证带系统时区库（P4）|

## 2. 传递依赖（`uv.lock` 锁定，实测 License）

| 包 | License | 备注 |
|---|---|---|
| `annotated-doc`, `annotated-types`, `anyio`, `h11`, `httptools`, `iniconfig`, `markdown-it-py`, `mdurl`, `pluggy`, `pydantic_core`, `typing-inspection`, `tzlocal`, `uvloop`, `watchfiles` | MIT | — |
| `click`, `httpcore`, `idna`, `MarkupSafe`, `starlette`, `websockets` | BSD-3-Clause | — |
| `Jinja2` | BSD-3-Clause | metadata 写作 "BSD License" |
| `Pygments` | BSD-2-Clause | rich 依赖 |
| `packaging` | Apache-2.0 OR BSD-2-Clause | — |
| `certifi` | **MPL-2.0** | 白名单内（文件级 copyleft，不传染本项目源码） |
| `typing_extensions` | PSF-2.0 | — |

**结论：零 GPL / AGPL 代码依赖。** 复核命令见本文末尾。

## 3. 开发依赖

| 包 | License | 用途 |
|---|---|---|
| `pytest` | MIT | 测试 |
| `ruff` | MIT | lint / format |
| `respx` | BSD-3-Clause | mock httpx，公众号官方接口全量打桩（`tests/publishers/`） |

## 4. Sidecar（独立进程，HTTP 调用）

| 组件 | 上游 | License | 集成方式 | 阶段 |
|---|---|---|---|---|
| `xiaohongshu-mcp` | `xpzouying/xiaohongshu-mcp` | **Apache-2.0** | Docker 镜像 `xpzouying/xiaohongshu-mcp:v2.5.0`，一账号一容器，REST `/api/v1/*`（见 4.1） | P2 **已落地** |
| `MoneyPrinterTurbo` | `harry0703/MoneyPrinterTurbo` | **MIT** | Docker（profile `video`），HTTP `POST /api/v1/videos` + 轮询（见 4.2） | P3 **已落地** |
| `XHS-Downloader` | `JoeanAmier/XHS-Downloader` | **GPL-3.0** | **独立进程**（profile `xhs`），仅 HTTP 调用——**但本仓侧至今没有客户端与调用方**：计划中的 `sourcing/xhs_search.py` 文件实际不存在，`xhs_downloader_base_url` 只出现在 `core/config.py` 的默认值与 `scripts/preflight.py` 的连通性探测里 | P2 **未接通**（compose 里有 service，代码侧无消费方；生产上**有意未起**，见 `docs/RISKS.md` 第 9 条） |
| `TrendRadar` | `sansan0/TrendRadar` | **GPL-3.0** | **独立进程**（profile `sourcing`），仅 HTTP 读它产出的数据文件（见 4.4） | P4 **已落地** |
| `douyin-mcp` | `Kuhakucai/douyin-mcp` | **AGPL-3.0** | **独立进程**，仅 HTTP 调用；网络交互条款只约束该进程自身。**当前不接**——抖音指标由宿主机上传器读创作者中心数据页，为几个公开数字再拉一个 copyleft 容器不划算；**路线图保留**（要更细的数据仍按"独立进程 + HTTP"接它）。见 4.5 与 `metrics/README.md`「抖音的口径」 | P3 **当前不接**（未进 compose，见 4.5） |

**上表「阶段」一列的每一行都能当场跑出来**，别只信文字：

```bash
# 「独立进程」这一列：compose 里到底有哪些 sidecar service，各挂哪个 profile
grep -nE '^  [a-z][a-z0-9-]*:$|profiles:' docker-compose.yml

# 「已落地」这一列：本仓侧的客户端在不在（缺的那个会当场报 No such file，这正是答案）
ls -1 publishers/xhs/client.py generation/mpt_client.py sourcing/trendradar.py sourcing/xhs_search.py

# trendradar 是否真的接进了采集链路（不只是有个客户端文件）
sed -n '/^SOURCES/,/^}/p' sourcing/collector.py
```

第一条的输出里**没有** `docker-compose.xhs.yml` 的每账号小红书 sidecar：那个文件是
`scripts/gen_xhs_sidecars.py` 的**生成物**，在 `.gitignore` 里（`git ls-files
docker-compose.xhs.yml` 返回 0 行），起之前先跑生成器。所以 `xiaohongshu-mcp` 的
「已落地」指的是**仓库侧集成已完成**（客户端 + 生成器 + 测试），不是"生产上已经起着"——
各 sidecar 在生产上的实际起停状态以 `docs/RISKS.md` 第 9 / 第 15 条为准。

### 4.1 `xiaohongshu-mcp`（Apache-2.0）——P2 已落地

| 项 | 记录 |
|---|---|
| 上游 | https://github.com/xpzouying/xiaohongshu-mcp |
| License | **Apache-2.0**（允许商用、修改、分发，须保留版权与 License 文件） |
| 形态 | Go 单二进制 + 内置 Chromium，**独立容器**；core 只发 HTTP，不 import、不复制其代码 |
| 镜像 | `xpzouying/xiaohongshu-mcp:v2.5.0`（Docker Hub）；国内源见 `sidecars/xhs/README.md`。**版本号的权威值在代码里**，别信这里抄的：`grep -n DEFAULT_IMAGE scripts/gen_xhs_sidecars.py` |
| 调用方 | `publishers/xhs/client.py`（httpx 直调 REST `/api/v1/*`） |
| 编排 | `scripts/gen_xhs_sidecars.py` → `docker-compose.xhs.yml`，一账号一容器一 volume 一端口。生成物**不进 git**（在 `.gitignore` 里），起之前先跑生成器 |

**从上游取用了什么**（Apache-2.0 下的合规使用，均已注明出处）：

1. **接口形状**：路由表、请求/响应字段来自其 `routes.go` / `types.go` / `service.go`，
   照着写客户端与 `tests/fixtures/xhs/*.json`。这属于接口事实，不是代码复制。
2. **标题长度算法**：`publishers/xhs/client.py:calc_title_length` 与上游
   `pkg/xhsutil/title.go:CalcTitleLength` **同规则**（非 ASCII 记 2、ASCII 记 1、
   向上取整除 2）。按规则重写，未逐行复制；即使复制，Apache-2.0 也允许，这里注明来源。

**没有引入的东西**：

- 不引入 `mcp` Python 客户端库。备选的 MCP 通道（`POST /mcp`）在
  `XhsMcpClient.mcp_call` 里用 httpx 手写最小 JSON-RPC 2.0——为一个 POST 拉进一整套
  依赖不划算，也避免多一份 License 台账。
- 不调用其 `DELETE /api/v1/login/cookies`（破坏性操作），只在运维文档里作为人工手段记录。

### 4.2 `MoneyPrinterTurbo`（MIT）——P3 已落地

| 项 | 记录 |
|---|---|
| 上游 | https://github.com/harry0703/MoneyPrinterTurbo |
| License | **MIT**（`LICENSE` 首行 `MIT License`，Copyright (c) 2024 Harry；GitHub API `spdx_id: MIT`） |
| 形态 | Python + ffmpeg，**独立容器**；core 只发 HTTP，不 import、不复制其代码 |
| 镜像 | `ghcr.io/harry0703/moneyprinterturbo:latest`（compose 用 `command: ["python3","main.py"]` 覆盖镜像自带的 WebUI CMD） |
| 调用方 | `generation/mpt_client.py`（httpx 直调 REST `/api/v1/*`） |
| 编排 | `docker-compose.yml` 的 `mpt` 服务（profile `video`）+ `sidecars/mpt/config.example.toml` |
| 核对基准 | `main` 分支 HEAD `1f9f19c`，2026-08-16 |

**从上游取用了什么**（MIT 下的合规使用，均已注明出处）：

1. **接口事实**：路由表（`app/controllers/v1/video.py`）、`VideoParams` 字段
   （`app/models/schema.py`）、任务状态常量（`app/models/const.py`：`-1/1/4`）、
   响应封装形状（`app/utils/utils.py` + `app/asgi.py`）。照着写客户端与测试打桩，
   属于接口事实，不是代码复制。
2. **compose 形状**：服务命令、端口、挂载点参照其 `docker-compose.release.yml`，
   但按本项目需要改了（不发布 WebUI、端口只绑回环、配置模板自写）。

**没有引入的东西**：

- 不用它内置的 LLM（`/api/v1/scripts`、`/api/v1/terms`）。脚本由 Claude 生成后经
  `video_script` / `video_terms` 灌入，`config.toml` 的 `llm_provider` 留空——
  两套 LLM 配置会导致"改了 prompt 却没生效"。
- 不用它的 Streamlit WebUI（8501），compose 里不起那个服务。
- 不用它的跨平台分发（`cross_post_*`）：本项目的发布走自己的 publisher 契约。
- 不引入任何视频处理 Python 库（moviepy / ffmpeg-python）。成片的时长与分辨率由
  `review/inspect.py:read_video_info` 用**标准库解析 MP4 box**读出，装了 `ffprobe`
  才作为退路调用（外部命令，非代码依赖）。

**已知未核实**（代码里已标注）：上游没有健康检查路由，`MptClient.health()` 用只读的
`GET /api/v1/tasks?page_size=1` 代替；其 API 鉴权默认关闭（`Depends(base.verify_token)`
被注释掉），`MPT_API_KEY` 只在部署方自行打开时才有意义。

### 4.3 素材源条款（Pexels / Pixabay）——**与 MPT 的 License 无关**

MPT 只是**检索并下载**素材，素材本身的授权由各站决定。这是与 License 台账分开的
一类合规义务，运营侧必须知道：

| 素材源 | 许可 | 要点 |
|---|---|---|
| Pexels | [Pexels License](https://www.pexels.com/license/) | 免费商用、无需署名；**禁止**把素材原样重新分发/售卖为素材本身，禁止暗示被拍摄者为你的产品背书，禁止用于对可识别人物有负面暗示的语境 |
| Pixabay | [Pixabay Content License](https://pixabay.com/service/license-summary/) | 免费商用、无需署名；**禁止**原样重新分发，禁止用于含可识别人物/品牌/商标的商业场景而未取得额外授权 |

本项目的用法（把素材作为口播的**背景画面**，叠加自制字幕与配音后作为原创短视频发布）
落在两家许可的允许范围内。但以下三条要靠**人工审核卡点**兜住，代码挡不了：

1. 不要生成"素材本身就是主体"的片子（那接近于原样重分发）。
2. 涉及可识别真人出镜的素材，不要配上对该人物的评价性口播。
3. 医疗、金融、母婴等强监管垂类，不要用素材画面暗示疗效或收益。

`prompts/accounts/<id>/persona.md` 的"不碰的题材"段落是第一道过滤，
审核页的"已完整观看"复选是最后一道。

### 4.4 `TrendRadar`（**GPL-3.0**）——P4 已落地

| 项 | 记录 |
|---|---|
| 上游 | https://github.com/sansan0/TrendRadar |
| License | **GPL-3.0**（`api.github.com/repos/sansan0/TrendRadar` 的 `spdx_id` 实测）|
| 版本 | 仓库 `version` 文件 = `6.10.0`；61.5k★，`pushed_at` 2026-07-17（2026-08-16 核对）|
| 形态 | Docker 镜像 `wantcat/trendradar`（Docker Hub），**独立容器**，profile `sourcing` |
| 集成 | core 只发 HTTP **读它产出的数据文件**；不 import、不复制其任何代码 |
| 客户端 | `sourcing/trendradar.py`；sidecar 说明 `sidecars/trendradar/README.md` |

**为什么"读它产出的文件"也在合规范围内**：GPL 约束的是**程序本身**的分发与链接。
TrendRadar 作为独立进程运行，我们通过它自己开放的 HTTP 端口取用它**生成的数据**
（SQLite / 文本文件），既没有链接它的代码，也没有分发它。这与同为 GPL-3.0 的
`XHS-Downloader` 处理一致。

**它没有 REST API**——这一点最容易搞错，写在这里以免后人重新踩：

- `8080` 是 `python -m http.server` 把 `/app/output` 目录挂出来（上游 `docker/manage.py`），
  无鉴权、无路由、无 JSON 端点；
- `3333` 是另一个镜像 `wantcat/trendradar-mcp` 的 FastMCP Streamable HTTP（路径 `/mcp`，
  JSON-RPC）。**本项目不用**：为了拉个热榜引入一层 MCP 协议栈不划算。

我们读 `output/news/{YYYY-MM-DD}.db`（SQLite，表结构见上游
`trendradar/storage/schema.sql`），退路是 `output/txt/{YYYY-MM-DD}/{HH-MM}.txt`。
注意日期目录是 **ISO 格式**，不是老版本的 `2026年08月16日`。

**上游数据本身来自 newsnow**：`trendradar/crawler/fetcher.py` 里
`DEFAULT_API_URL = "https://newsnow.busiyi.world/api/s"`。所以本源与
`sourcing/newsnow.py` 天然重叠，跨源去重由 `sourcing.base.persist_topics` 兜住；
它的价值在于加工（按天累积、`crawl_count` 持续热度、关键词过滤）而不是覆盖面。

**未核实**：MCP transport 的实际握手报文（没起容器实测，本项目也不走这条路）；
上游可选平台的**总数**（README 只明确"默认监控 11 个主流平台"，总池由 newsnow 决定）。

### 4.5 `douyin-mcp`（**AGPL-3.0**）——P3 **当前不接**，路线图保留

| 项 | 记录 |
|---|---|
| 上游 | https://github.com/Kuhakucai/douyin-mcp |
| License | **AGPL-3.0**（网络交互条款只约束**该进程自身**；本仓只发 HTTP、不 import、不复制源码，不受传染） |
| 现状 | **没有接**：`docker-compose.yml` 里没有这个 service，仓库里也没有它的客户端或任何引用 |
| 这件事现在谁在干 | **宿主机上传器**读创作者中心数据页：`publishers/douyin/service.py:read_metrics` → `GET /accounts/{id}/metrics/{post_id}` → `publishers/douyin/publisher.py:fetch_metrics` |

**为什么当前不接**（口径出处：`metrics/README.md`「抖音的口径」一节）：上传器已经有一个
登录着的浏览器，为了读那几个公开数字（`views` / `likes` / `comments` / `shares`，
`collects` / `follows` 数据页没有单列、恒为 `None`）再拉一个 copyleft sidecar 进来不划算。
这是**成本取舍**，不是"上游不合规不能用"——AGPL 组件在本仓的"独立进程 + HTTP"形态下
本来就是允许的（见本文件开头的策略，以及 4.4 对同为 copyleft 的 TrendRadar 的论述）。

**路线图保留，不是废弃项。** `metrics/README.md` 的原话是"将来要更细的数据（完播率、涨粉）
再按'独立进程 + HTTP'接它们"。真接的那天要做两件事：重新走一遍 AGPL 合规确认（只发 HTTP、
不 import、不复制源码），并把 §4 表里这一行的阶段改掉。所以它留在表里，既不是待办也不是
废弃项。（同一节里被一并否掉的还有 `TikTokDownloader`(GPL)，理由与结论相同；它从未被本仓
集成过，因此不在 §4 表里登记。）

复核（都不联网，当场跑）：

```bash
# compose 里没有 douyin-mcp / douyin-metrics 这个 service
grep -nE '^  [a-z][a-z0-9-]*:$' docker-compose.yml

# 代码里没有任何引用（无输出、退出码 1 才对）
grep -rn "douyin-mcp\|douyin_mcp\|Kuhakucai" --include="*.py" .
```

## 5. Node 工具（子进程调用，不作代码依赖）

| 组件 | 上游 | License | 集成方式 | 阶段 |
|---|---|---|---|---|
| `@wenyan-md/cli` | `caol64/wenyan-cli` | **Apache-2.0** | `subprocess` 调 `wenyan render` / `wenyan publish`（`publishers/wechat_mp/wenyan_backend.py`，凭据经环境变量传子进程）；License 已在上游 README 核实 | P1 |

## 6. 代码级依赖

### 6.1 已落地（P1）

| 组件 | License | 集成方式 | 用途 |
|---|---|---|---|
| `anthropic`（官方 SDK） | MIT | pip 依赖 | Claude API，见 `generation/llm.py` |
| `playwright` | **Apache-2.0** | pip **可选**依赖（`--extra render`） | 截图渲染，见下 |
| `ourongxing/newsnow` | **MIT** | **只调 HTTP API**，不 import（它是 TypeScript/Nitro 项目） | 热榜聚合，见 `sourcing/newsnow.py` |
| `lonnyzhang423/douyin-hot-hub` | **MIT** | **只读其仓库归档 JSON**（raw.githubusercontent.com），不 import | 抖音热榜，见 `sourcing/douyin_hot_hub.py` |
| `konsheng/Sensitive-lexicon` | **MIT** | 运行时下载词表数据到 `data/lexicon/`（**不进 git**），见 `scripts/fetch_lexicon.py` | 敏感词硬过滤 |
| `yuwen-cool/yuwen-publish-precheck` | **MIT** | **vendored 规则数据**（见下） | 平台违禁词预检 |

### 6.2 `playwright`（Apache-2.0）——两处用途，一个依赖

| 用途 | 模块 | 缺它时 |
|---|---|---|
| 公众号封面 900×383 / 900×900（P1） | `generation/cover.py` | 返回 `None`，列表页用默认图，**不阻断** |
| 小红书 3:4 图文卡片 1242×1656（P2） | `generation/xhs_cards.py` | 抛 `ScreenshotUnavailable`，bundle 无图入库并被 `inspect` block |
| 抖音成片封面 9:16 1080×1920（P3） | `generation/cover.py`（`sizes=("vertical",)`） | 返回 `None`，bundle 无封面入库并被 `inspect` block |

仍然是**可选** extra 而不是主依赖：它额外需要 `playwright install chromium`
（约 150MB 浏览器二进制），进主依赖会拖垮 CI 与首次安装。
装法：`uv sync --extra render && uv run playwright install chromium`。

License 实测：`playwright 1.62.0` = Apache-2.0；传递依赖 `greenlet`(MIT AND PSF-2.0)、
`pyee`(MIT)。chromium 二进制由 playwright 在运行期下载，不进本仓库、不随包分发
（BSD-3-Clause + LGPL 组件，作为**独立可执行文件**被子进程调用，不链接本项目代码）。

### 6.3 vendored：`review/vendor/yuwen_precheck/`

上游**没有发布到 PyPI 或 npm**，仓库里没有 `pyproject.toml` / `package.json` / 包目录——
它的形态是一个 Claude Agent Skill（`SKILL.md` + `scripts/scan.py` CLI），无法作为库导入。
按任务书兜底方案 vendored，且**只拷数据**：

- ✅ `scripts/terms.json`（16KB，41 条规则 + 3 条辟谣项）+ `LICENSE`（MIT 全文）
- ❌ 不拷 `SKILL.md` / `references/*.md`——那是写给 AI agent 执行的**指令性文本**，
  放进本仓库等于把上游文档变成对我们自己 agent 的隐式指令，属于提示注入面。
  规则数据（正则 + 严重度）不含指令，安全。
- ❌ 不拷 `scripts/scan.py`——匹配逻辑我们自己写（`review/precheck.py`），
  以接上本项目的 `Finding` 契约。

完整取舍与更新方式见 `review/vendor/yuwen_precheck/PROVENANCE.md`。

### 6.4 `patchright`（Apache-2.0）——P3 已落地

| 项 | 记录 |
|---|---|
| 上游 | https://github.com/Kaliiiiiiiiii-Vinyzu/patchright（Python 包 `patchright`） |
| License | **Apache-2.0**（实测 `importlib.metadata` 的 `License` 字段就是 `Apache-2.0`） |
| 版本 | `patchright==1.61.2`（uv.lock 已锁），API 与 playwright 完全一致 |
| 形态 | **可选 extra**（`uv sync --extra douyin`），只装在**宿主机**上 |
| 调用方 | `publishers/douyin/service.py`（宿主机常驻进程）。core 侧的 `client.py` / `publisher.py` **不 import 它** |
| 传递依赖 | `greenlet`(MIT AND PSF-2.0)、`pyee`(MIT)、`typing_extensions`(PSF-2.0)——与 `playwright` extra 完全重合，无新增许可面 |

**为什么是可选 extra 而不是主依赖**：wheel 约 40MB（自带 driver），且只有跑抖音上传器的
那台宿主机需要它；core 容器与 CI 都不装。装法：
`uv sync --extra douyin && uv run patchright install chromium`。

**用途受限声明（`docs/POLICY.md`）**：patchright 在本项目里的**唯一**用途是
"让真人自己的账号在有头浏览器里不被 headless 误杀"。代码里：

- `BrowserPool.context()` 只传 `user_data_dir` / `channel` / `headless=False`，
  **不设 user_agent、不加 args、不注入 init script、不改 navigator 属性**；
- `headless` 写死 `False`，没有任何开关能打开；
- 一个真人账号一个 `profile_dir`，不提供跨账号复用入口（不是 Cookie 池、不是指纹隔离）。

回归测试 `tests/publishers/test_douyin.py::test_browser_pool_launch_is_headful_and_carries_no_stealth`
断言启动参数集合恰好是那三个，多一个就红。

**未使用其"反检测"周边**：patchright 本身就是 playwright 的 patched fork（不开
CDP `Runtime.enable`、隔离世界执行脚本等），我们**只用它的默认行为**，没有再叠加
任何第三方 stealth 插件或自写规避代码。

### 6.5 `deepseek-harness`（MIT）——P5 已落地

本项目的 **Agent/LLM 基座**可选后端。只换 LLM 层，`core/` 确定性控制面不受影响。

| 项 | 记录 |
|---|---|
| 上游 | https://github.com/deepseek-ai/deepseek-harness |
| License | **MIT**（实测：两个发行包的 metadata `License-Expression` 均为 `MIT`；仓库根 `LICENSE` 亦为 MIT） |
| 发行包 | `deepseek-harness-sdk==0.1.0rc6`（import 名 `deepseek_harness`）+ 同版本平台轮子 `deepseek-harness-runtime-bin==0.1.0rc6`（import 名 `deepseek_harness_runtime`） |
| 版本策略 | **pin 精确版本**。dsh 处于 developer preview，rc 之间随时破坏兼容；不许写 `>=` |
| 形态 | **可选 extra**（`uv sync --extra dsh`）。默认后端仍是 anthropic，不装也能跑 |
| 传递依赖 | 只有 `pydantic>=2.12,<3`（本项目主依赖已有），**零新增许可面** |
| 集成方式 | `deepseek_harness.DeepSeekHarness` 拉起 runtime **子进程**，stdio JSON-RPC 驱动；见 `generation/llm_dsh.py` |
| 组合配置 | `configs/dsh/cordis.yml`（本仓库自带的**受限**组合，零工具） |

**没有 vendor 任何 dsh 源码。** 本仓库只写了一份 `cordis.yml`（我们自己的部署配置，
不是上游代码）和一层 Python 适配（`generation/llm_dsh.py`）。

**为什么是可选 extra 而不是主依赖**：`deepseek-harness-runtime-bin` 的 macOS arm64
轮子解包后约 192MB（单文件 Node 运行时 + 原生 spawn helper）。进主依赖会把 CI、
Docker 镜像和首次安装全部拖垮，而默认后端根本用不到它。

**runtime 二进制的分发形态**：平台轮子里是一个预编译的单文件 Node 可执行文件
（`dsh-jsonrpc-agent-pkg-macos-arm64` 及其 `-spawn-helper` 兄弟），由 pip/uv 从 PyPI 安装，
**不进本仓库、不随本项目分发**；它作为**独立子进程**被调用，不与本项目代码链接。
其内部 Node/V8 及打包进去的 npm 依赖由上游的 `THIRD_PARTY_NOTICES.md` 覆盖。

**红线：模型零工具。** dsh 的默认组合会给模型挂 bash / 文件读写 / subagent 等工具。
本项目的选题标题来自公开热榜（不可信输入），带工具的生成 Agent 等于给提示注入开了
本机执行通道。`configs/dsh/cordis.yml` 因此：既不挂载任何工具/执行器插件，
也把 `agent-spine-demo` 自带的 `toolBash` / `toolJobs` / `skills` 逐项关掉。
两道机械验证见 `docs/OPS.md` §dsh。

### 6.4.1 后续阶段将新增

| 组件 | License | 用途 | 阶段 |
|---|---|---|---|
| `chinese-sensitive-words-mcp` | **MIT** | 平台违禁词 MCP | P2+ |
| `wechatpy` | MIT | **仅作参考实现思路**（错误码分类、token 缓存的做法），未安装、未 import、未复制代码；公众号 API 由 `publishers/wechat_mp/client.py` 用 httpx 直调 | P1 |

### 6.5 上游事实核实记录（2026-08-15）

集成前实测核实过以下两点，与任务书/README 早期描述**不一致**，以实测为准：

| 项 | 早期描述 | 实测结果 |
|---|---|---|
| douyin-hot-hub 归档 | `archives/` 下的当日 JSON | `archives/YYYY-MM-DD.md` 是 **Markdown**；JSON 在 **`raw/YYYY-MM-DD/<board>.json`**，且是抖音上游 API 原样转储（条目无 `url` 字段，需自行拼搜索页） |
| yuwen-publish-precheck | 可 pip / 子模块方式复用 | 未发布到任何包管理器，是 Agent Skill；只能 vendored 数据 |

newsnow 侧核实：`GET /api/s?id=<source>`，响应 `{status,id,updatedTime,items[],info}`；
`items` 服务端固定截断 30 条；非法 source id 返回 **HTTP 500**（不是 4xx）；
`extra.diff` 是前端算的，API 不返回。`weibo/zhihu/baidu/toutiao` 四个 id 均有效。

## 7. 仅参考、不复制（无 License / 禁商用 / 授权受限）

| 项目 | 问题 | 我们的做法 |
|---|---|---|
| `dreammis/social-auto-upload`（14k★） | **无 License**（默认保留所有权利） | 只阅读其 `uploader/douyin_uploader/main.py` 的**流程顺序**（进上传页 → 选文件 → 等转码 → 填标题/话题 → 封面 → 定时 → 点发布 → 等跳内容管理页），选择器、错误分类、状态机、限频、identity 校验全部自写；**未复制任何代码**。逐条比对点见 7.1 |
| `lincwang123-bot/humanized-social-publisher` | MIT（可用） | 仅参考其**多账号安全原则**（独立 profile_dir、identity_hint 昵称校验、内容指纹幂等、同平台串行、导航节流），实现全部自写 |
| `NanmiCoder/MediaCrawler`（62k★） | **禁止商用** | 完全不使用——**禁止商用是硬性的**，这条结论与替代方案接没接通无关。采集侧的既定方案是 `XHS-Downloader` sidecar（GPL-3.0，独立进程），但它目前 **P2 未接通**（见 §4 表：compose 里有 service，代码侧无消费方） |
| `fishaudio/fish-speech` | 商用需单独授权 | 不使用；TTS 用 MPT 内置或 CosyVoice(Apache-2.0) |
| `yikart/AiToEarn` | 闭源中继 + 托管授权 | 不使用 |
| `doocs/md` `packages/core` | private 包，无构建产物 | 仅参考主题与图床**逻辑**，不作依赖 |
| `joeseesun/qiaomu-info-card-designer` | MIT（可用） | 仅参考卡片模板**设计思路**（信息卡片的分栏与留白），`generation/templates/xhs/` 的 HTML/CSS 全部自写，未复制任何代码 |
| `ziguishian/xhs-visual-director-skill` | MIT（可用） | 仅参考视觉编排**提示词思路**，`prompts/xhs/*.md` 全部自写 |
| `Xiangyu-CAS/xiaohongshu-ops-skill` | 方法论文档 | 仅参考运营方法论 |

### 7.1 `social-auto-upload`：参考了什么、没参考什么

无 License 仓库的边界是"事实与流程可以学，表达不能抄"。逐条列出以便审计：

| 维度 | 我们的做法 |
|---|---|
| **流程顺序** | 参考（这是平台页面本身决定的客观顺序，不是上游的创作） |
| 页面 URL | 参考（`creator.douyin.com/creator-micro/content/upload` 等是平台公开地址） |
| CSS 选择器 | **自写**：上游用的是它自己那套定位串；我们写的是"多候选 + 属性包含匹配 + JSON 覆盖表"，结构与写法都不同（`service.py:SELECTORS`） |
| 错误分类 / 重试语义 | **自写**：映射到本项目 P0 冻结的 `RetryableError` / `NeedsReloginError` / `PermanentError`，上游没有这套契约 |
| 登录与验证码 | **自写且方向相反**：上游偏向自动化，我们**刻意停下来等真人**（`docs/POLICY.md`） |
| 限频 / identity / 幂等 / 截图 / 串行 worker | **自写**，上游没有对应实现 |
| 代码片段 | **零复制**。本仓库不含上游任何函数、类名、常量或注释 |

## 8. 复核方法

```bash
# 列出所有已装包及其 License（应为 MIT/Apache/BSD/PSF/MPL，无 GPL/AGPL）
uv run python - <<'PY'
from importlib.metadata import distributions
for d in sorted(distributions(), key=lambda d: d.metadata["Name"].lower()):
    md = d.metadata
    lic = md.get("License-Expression") or md.get("License") or ""
    if not lic or len(lic) > 40:
        cls = [c for c in md.get_all("Classifier") or [] if c.startswith("License ::")]
        lic = "; ".join(c.split("::")[-1].strip() for c in cls) or "?"
    print(f"{md['Name']:<24} {d.version:<12} {lic}")
PY

# 确认没有 GPL 关键字
uv run python -c "
from importlib.metadata import distributions
bad=[d.metadata['Name'] for d in distributions()
     if 'GPL' in ((d.metadata.get('License-Expression') or '') + ' ' +
                  ' '.join(d.metadata.get_all('Classifier') or [])).upper()
        and 'LGPL' not in (d.metadata.get('License-Expression') or '').upper()]
print('GPL 依赖:', bad or '无')"
```

最后复核日期：2026-08-16（P5 交付。**新增两个可选依赖
`deepseek-harness-sdk`(MIT) + `deepseek-harness-runtime-bin`(MIT)**，只进
`[project.optional-dependencies].dsh`；唯一传递依赖 `pydantic` 已是主依赖，
零新增许可面。复跑下面两条命令，`GPL 依赖: 无`。见 6.5。）

历史：2026-08-16（P3-② 交付。**新增一个可选依赖 `patchright`(Apache-2.0)**，
只进 `[project.optional-dependencies].douyin`，主依赖与 CI 不受影响；其传递依赖
（`greenlet` / `pyee` / `typing_extensions`）与已登记的 `playwright` extra 完全重合。
复跑下面两条命令，`GPL 依赖: 无`。见 6.4。）

历史：2026-08-16（P3-① 交付。**本阶段零新增依赖**——MoneyPrinterTurbo 只作
独立容器（MIT，见 4.2），客户端用已登记的 `httpx`；成片时长/分辨率探测用标准库
解析 MP4 box（`struct`）+ 可选的 `ffprobe` 外部命令，**没有**引入 moviepy /
ffmpeg-python / Pillow）；
2026-08-16（P2-① 交付，零新增依赖——小红书卡片复用 P1 已登记的 `playwright`，
模板是自写 HTML/CSS，图片尺寸探测用标准库 `struct` 读文件头而不是引入 Pillow）；
2026-08-15（P0 交付；P1-② 新增 `respx`(BSD-3-Clause)、
P1-① 新增 `anthropic` 与可选 `playwright` 后复核，无 GPL/AGPL）。

### P1-① 新增依赖的 License 实测输出

```
anthropic         0.122.0   MIT
distro            1.9.0     Apache License, Version 2.0
docstring-parser  0.18.0    MIT
jiter             0.16.0    MIT
sniffio           1.3.1     MIT OR Apache-2.0
playwright        1.62.0    Apache-2.0        （可选 extra: render）
greenlet          3.5.5     MIT AND PSF-2.0   （playwright 传递依赖）
pyee              13.0.1    MIT               （playwright 传递依赖）
```

全部落在白名单（MIT / Apache-2.0 / BSD / PSF / MPL-2.0）内，`GPL 依赖: 无`。

### P4 新增依赖的 License 实测输出

```
tzdata            2026.3    Apache-2.0        （主依赖，见第 1 节）
```

### P5 新增依赖的 License 实测输出

```
deepseek-harness-sdk          0.1.0rc6   MIT   （可选 extra: dsh）
deepseek-harness-runtime-bin  0.1.0rc6   MIT   （可选 extra: dsh，平台轮子）
```

两者 metadata 的 `License-Expression` 实测均为 `MIT`。SDK 的唯一非同族传递依赖是
`pydantic<3,>=2.12`（已是本项目主依赖）。`GPL 依赖: 无`。

P4 只新增这一个包。TrendRadar 是 **sidecar**（GPL-3.0，见 4.4），不是代码依赖；
`sqlite3` / `zoneinfo` / `argparse` 都是标准库。全部落在白名单内，`GPL 依赖: 无`。

### P3-② 新增依赖的 License 实测输出

```
patchright        1.61.2    Apache-2.0        （可选 extra: douyin，仅宿主机）
greenlet          3.5.5     MIT AND PSF-2.0   （与 playwright extra 共用）
pyee              13.0.1    MIT               （与 playwright extra 共用）
typing_extensions 4.16.0    PSF-2.0           （既有主依赖的传递依赖）
```

全部落在白名单（MIT / Apache-2.0 / BSD / PSF / MPL-2.0）内，`GPL 依赖: 无`。
