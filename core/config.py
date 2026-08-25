"""运行期配置：只从环境变量 / .env 读取，凭据不入库。"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from functools import lru_cache
from typing import Any, Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _parse_pairs(raw: str) -> dict[str, str]:
    """解析 ``k1=v1,k2=v2`` 形式的环境变量（按账号分发 sidecar 地址 / token）。"""
    result: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, value = part.partition("=")
        result[key.strip()] = value.strip()
    return result


def config_env_file() -> str:
    """返回当前进程应读取的配置文件。

    正常部署继续从工作目录的 ``.env`` 读取。E2E 等隔离进程可显式指定
    ``SW_CONFIG_ENV_FILE``，避免当前工作树的真实配置参与解析。
    """
    return os.environ.get("SW_CONFIG_ENV_FILE", ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    def __init__(self, **values: Any) -> None:
        # 保留 ``Settings(_env_file=None)`` 这条测试/调用方的明确禁用路径。
        # 未明确指定时，统一走可注入的配置文件来源。
        values.setdefault("_env_file", config_env_file())
        super().__init__(**values)

    # 运行时
    sw_env: str = "dev"
    sw_database_url: str = "sqlite:///./data/social_workflow.db"
    sw_use_fake_publishers: bool = True
    # 启动时把 accounts.yaml 同步进 DB（幂等、不碰 status）。关掉就必须手动
    # `python -m core.accounts sync`，否则调度器看不见台账里的账号
    sw_sync_accounts_on_start: bool = True
    # 随 core 进程启动 APScheduler。关掉的话定时任务不会跑，只剩 `POST /dev/tick/{name}`
    # 与外部 cron 两条路（测试里一律关掉，见 tests/conftest.py）
    sw_scheduler_enabled: bool = True
    sw_min_publish_interval_seconds: int = 900
    sw_max_publish_attempts: int = 3
    # 工作台 JSON API（/api/v1）的访问 token。**留空 = 不鉴权**（本机 ops 工具的默认形态）；
    # 非空时 /api/v1/* 一律要求 `Authorization: Bearer <token>`，见 core/api/。
    # 现有 Jinja2 页面（/review、/accounts、/stats）不受影响
    sw_ui_token: str = ""
    # 发布时段窗口（Account.extra['publish_windows']）的默认时区。窗口是"人的作息"
    sw_timezone: str = "Asia/Shanghai"
    # 账号台账文件。留空 = 仓库根的 accounts.yaml。
    # 工作台的「添加账号」会**回写**这个文件（台账与 DB 不许漂移），所以测试 / e2e
    # 一律指到临时副本上，绝不碰仓库里那份
    sw_accounts_file: str = ""

    # 调度（P4）：各 tick 的间隔与批量上限
    sw_sourcing_interval_hours: int = 6  # 每天 4 次热榜采集
    sw_generate_interval_minutes: int = 30
    sw_retry_sweep_interval_minutes: int = 5
    sw_publish_batch_size: int = 20
    sw_generate_max_per_tick: int = 3
    # 出稿时刻的随机抖动（秒，±）。每天整点同一秒出稿太机器；0 = 不抖
    sw_generate_jitter_seconds: int = 600
    # 关掉后 tick_generate 只统计不生成（`/dev/tick/generate` 仍可手动触发）
    sw_generate_enabled: bool = True
    # 自动生成时是否出图/出封面（无 Playwright/Node 的机器上置 false）
    sw_generate_make_media: bool = True
    # 自动生成时每条配几张生图（P11）。公众号题图与抖音封面只取 1 张。
    # 0 = 自动出稿一律不配图（手动出稿仍可在弹层里单独打开）
    sw_generate_illustrations: int = 2
    # 抖音自动生成默认**不**真渲染：一条片子几分钟起步且烧素材源配额
    sw_generate_skip_render: bool = True
    # 重试退避：base * 2^(attempts-1)，上限 max
    sw_retry_backoff_base_seconds: int = 300
    sw_retry_backoff_max_seconds: int = 14400
    # 卡在 retrying 超过这个时长直接进死信（NeedsRelogin 长期不处理的兜底）
    sw_retry_max_age_hours: int = 48

    # LLM
    # 后端开关：anthropic = 直连 Claude Messages API；dsh = 本地 deepseek-harness
    # Agent runtime 子进程（见 generation/llm_dsh.py）。默认不变，现有部署零影响。
    sw_llm_backend: Literal["anthropic", "dsh"] = "anthropic"
    anthropic_api_key: str = ""
    # 模型串：优先读 LLM_MODEL，兼容历史的 ANTHROPIC_MODEL
    llm_model: str = Field(
        default="claude-opus-5",
        validation_alias=AliasChoices("llm_model", "anthropic_model"),
    )
    # 思考深度 / 输出预算档位，见 generation/llm.py
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    # **兜底**上限：只作用于没有显式声明 max_tokens 的调用。正经调用点应该按
    # generation/output_budget.py 的分档表显式声明自己的预算，别吃这个默认值。
    # 为什么不是 4096：默认模型是 reasoning 模型，思考与正文共用输出预算，
    # 4096 会被思考单独吃光（2026-08-17 生产事故），所以兜底也要给到"标准档"。
    # 取值与 output_budget.STANDARD_OUTPUT_TOKENS 保持一致（有单测锁定）——
    # dsh 后端按 (model, effort, max_tokens) 分桶起 runtime；路由开启后组合数可能超过
    # 默认池上限，超出的桶由 LRU 回收。路由关闭时仍是旧的单模型分桶语义。
    llm_max_tokens: int = 8192
    # 长文正文档，与 output_budget.LARGE_OUTPUT_TOKENS 一致（理由同上）
    llm_article_max_tokens: int = 16000
    llm_timeout_seconds: float = 600.0

    # deepseek-harness（dsh）后端。只有 SW_LLM_BACKEND=dsh 时才生效。
    #
    # ⚠️ 这一组变量一律以 ``SW_DSH_`` 打头，**不是** ``DSH_``。这不是风格洁癖：
    # dsh 的产品 CLI 启动时会逐行读项目 `.env`，凡是名字落在 ``DSH_`` / ``XDG_`` /
    # ``DYLD_`` / ``BASH_FUNC_`` 前缀上的**一律抛错拒绝启动**（供应链防护：这些名字
    # 决定进程怎么起、代码与指令从哪儿加载、怎么出网，所以只准由启动环境提供）。
    # 判定见 deepseek-harness `packages/boot/app-boot/src/index.ts` 的
    # ``BOOTSTRAP_PREFIXES`` / ``isBootstrapOnly``，**没有开关可关**。
    #
    # 这些是本项目自有的配置项，只是名字撞进了那道保留前缀。撞名的代价很实在：
    # `.env` 里留着一行 ``DSH_PROVIDER=``，对话台（sw-harness）在本仓库目录下就起不来，
    # 报 `dsh: …/.env sets "DSH_PROVIDER", which only the launching environment may set`。
    #
    # 旧名全部保留为回退别名（见文件末尾的 ``DEPRECATED_ENV_ALIASES``），读到旧名会打一条
    # 弃用日志 + preflight WARN——生产 .env 下次部署才改名，不能因为改名断掉正在跑的部署。
    #
    # 注：走 SDK 的那条路（generation/llm_dsh.py 起的 runtime 子进程）**不受影响**——
    # 它调的是不做校验的 `loadEnv`，不是产品 CLI 的 `loadLayeredEnv`。两条路真的不同。
    #
    # provider 必须是 configs/dsh/cordis.yml 里注册过的路由名，否则 runtime 握手就失败
    dsh_provider: str = Field(
        default="deepseek-official",
        validation_alias=AliasChoices("sw_dsh_provider", "dsh_provider"),
    )
    dsh_model: str = Field(
        default="deepseek-v4-flash",
        validation_alias=AliasChoices("sw_dsh_model", "dsh_model"),
    )
    # 显式打开后，dsh 按每次调用的 purpose 分档：complex 走 Sol，medium 与 low 都走
    # Luna（映射表在 generation/model_routing.py 的 tier_models）；默认保持旧部署。
    # 曾经的 SW_DSH_TERRA_MODEL 已移除，medium 档不再有独立模型旋钮。
    dsh_model_routing: bool = Field(default=False, validation_alias="sw_dsh_model_routing")
    dsh_sol_model: str = Field(default="gpt-5.6-sol", validation_alias="sw_dsh_sol_model")
    dsh_luna_model: str = Field(default="gpt-5.6-luna", validation_alias="sw_dsh_luna_model")
    # 受限 Cordis 组合（零工具）。相对项目根解析
    dsh_cordis_path: str = Field(
        default="configs/dsh/cordis.yml",
        validation_alias=AliasChoices("sw_dsh_cordis_path", "dsh_cordis_path"),
    )
    # JSONL 会话日志目录。在 data/ 之下，已被 .gitignore 覆盖
    dsh_session_root: str = Field(
        default="data/dsh_sessions",
        validation_alias=AliasChoices("sw_dsh_session_root", "dsh_session_root"),
    )
    # 0 = 沿用 LLM_MAX_TOKENS / LLM_ARTICLE_MAX_TOKENS；> 0 则再压一道统一上限
    dsh_max_tokens: int = Field(
        default=0,
        validation_alias=AliasChoices("sw_dsh_max_tokens", "dsh_max_tokens"),
    )
    # runtime 子进程按 (模型, 思考档, 输出上限) 分桶。分档路由可能超过默认池上限，
    # 超出时按 LRU 关最久未用的；关闭路由仍保持旧的单模型分桶语义
    dsh_max_live_runtimes: int = Field(
        default=4,
        validation_alias=AliasChoices("sw_dsh_max_live_runtimes", "dsh_max_live_runtimes"),
    )

    # 生图（P11，generation/imagegen.py）
    # OpenAI Images API 兼容端点。留空 = 复用 dsh 那条网关地址（同一个私有网关，
    # 只是换一把 key），这样只配一处就能同时跑文字与生图
    sw_imagegen_base_url: str = ""
    # 生图**专用** key，与聊天 key 分开（网关按 key 分组授权，图像生成是单独的开关）。
    # 留空则回落到 DEEPSEEK_API_KEY——多半没有图像权限，首调会报 permission_error
    sw_imagegen_api_key: str = ""
    # 代码默认就是 gpt-image-2（用户拍板"以后都是 image2"），不靠 .env 兜底。
    # gpt-image-1 / gpt-image-1.5 作为可配回退
    sw_imagegen_model: str = "gpt-image-2"
    # auto = 启动不探测，首次调用失败就在本进程内标记不可用（不再反复烧钱试）；
    # true = 强制启用（失败仍降级，只是不会被会话级标记永久关掉）；false = 完全关闭
    sw_imagegen_enabled: Literal["auto", "true", "false"] = "auto"
    # 生图比文字慢得多，单独给更长的超时
    sw_imagegen_timeout_seconds: float = 180.0
    # dsh deepseek 路由的网关地址与 key。两者都由 cordis.yml 经环境变量取用
    # （见 configs/dsh/cordis.yml 的 apiKeyEnv / baseURL），这里声明出来是为了让
    # Settings 也能读到——生图默认复用同一条网关，只是换一把 key。
    # 取值必须与 cordis.yml 一致，否则文字与生图会打到两个地方
    sw_dsh_deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_api_key: str = ""

    # 选题采集
    # newsnow 自部署或公开实例地址（如 http://localhost:4444）；留空则该数据源不可用
    newsnow_base_url: str = ""
    # 逗号分隔的榜单 id，见 sourcing/newsnow.py
    newsnow_sources: str = "weibo,zhihu,baidu,toutiao"
    douyin_hot_hub_base_url: str = (
        "https://raw.githubusercontent.com/lonnyzhang423/douyin-hot-hub/main"
    )
    sourcing_timeout_seconds: float = 15.0
    # TrendRadar sidecar（GPL-3.0，只走 HTTP）。它的 8080 是静态文件服务而不是 REST API，
    # 见 sourcing/trendradar.py 的模块 docstring。留空则该数据源不可用
    trendradar_base_url: str = ""
    # db = 读 output/news/{date}.db（路径确定，推荐）；txt = 读最新 TXT 快照；auto = 先 db 后 txt
    trendradar_mode: Literal["auto", "db", "txt"] = "auto"

    # 审核词库（scripts/fetch_lexicon.py 下载到此目录）
    lexicon_dir: str = "data/lexicon"

    # 公众号渲染 / 发布（@wenyan-md/cli）。
    # P4 归并：渲染侧（generation/wechat_render.py）与发布侧
    # （publishers/wechat_mp/wenyan_backend.py）此前各读一半配置，现在统一 WENYAN_* 前缀。
    # 唯一的历史别名是 NODE_BIN → WENYAN_NODE_BIN（仍可用，preflight 会提示弃用）。
    wenyan_theme: str = "default"
    wenyan_npm_spec: str = "@wenyan-md/cli"
    wenyan_node_bin: str = Field(
        default="npx",
        validation_alias=AliasChoices("wenyan_node_bin", "node_bin"),
    )
    wenyan_timeout_seconds: float = 120.0

    # 公众号
    wechat_app_id: str = ""
    wechat_app_secret: str = ""
    # 账号是否已认证：2025-07 起未认证主体的 freepublish 权限被回收，草稿箱不受限
    wechat_certified: bool = False
    # 服务端总开关：双确认闸门的第一道。即便账号已认证，这里为 false 也只落草稿箱
    wechat_auto_publish: bool = False
    # 发布后端：api = 官方 HTTP API；wenyan = @wenyan-md/cli 子进程直传草稿箱
    wechat_backend: str = "api"
    # 官方 API 基址（测试 / 自建中转可覆盖）
    wechat_api_base: str = "https://api.weixin.qq.com"
    # 正文相对图片路径的解析根目录，留空表示进程当前目录
    wechat_media_base_dir: str = ""
    # freepublish 轮询参数
    wechat_publish_poll_interval: float = 3.0
    wechat_publish_poll_timeout: float = 120.0
    # 可选：wenyan Server 中转地址（绕开固定出口 IP 白名单，errcode 40164）
    wenyan_server_url: str = ""
    # wenyan Server 模式的 API key 文件路径（凭据不入代码、不入库）
    wenyan_api_key_file: str = ""

    # 素材源
    pexels_api_key: str = ""
    pixabay_api_key: str = ""

    # sidecar
    mpt_base_url: str = "http://localhost:8080"
    # MPT 默认不鉴权（上游把 verify_token 注释掉了）；部署方打开时填这里，走 x-api-key 头
    mpt_api_key: str = ""
    mpt_timeout_seconds: float = 30.0
    # 成片几十 MB，下载单独给更长的超时
    mpt_download_timeout_seconds: float = 600.0
    # 单个渲染任务的最长等待。超时不算失败：RenderJob 留着，由 tick_render_jobs 继续跟
    mpt_render_timeout_seconds: float = 1800.0
    mpt_poll_interval_seconds: float = 5.0
    # 素材源：pexels / pixabay / coverr（key 配在 sidecar 的 config.toml 里，core 只探测）
    mpt_video_source: str = "pexels"
    # 留空 = 用 sidecar 侧默认中文音色（edge-tts）
    mpt_voice_name: str = ""
    mpt_subtitle_position: str = "bottom"
    # 单个素材片段时长（秒），越短剪辑感越强
    mpt_clip_duration: int = 3
    mpt_bgm_type: str = "random"
    xhs_downloader_base_url: str = "http://localhost:5556"
    xhs_mcp_endpoints: str = ""

    # 抖音宿主机上传器（有头 Patchright 常驻进程，不入 Docker，见 publishers/douyin/）
    # 历史别名 DOUYIN_AGENT_BASE_URL 仍可用
    douyin_service_url: str = Field(
        default="http://127.0.0.1:8710",
        validation_alias=AliasChoices("douyin_service_url", "douyin_agent_base_url"),
    )
    # 浏览器 channel：优先系统 Chrome，失败自动回退 patchright 自带 chromium
    douyin_browser_channel: str = "chrome"
    # accounts 表没填 daily_limit 时的兜底。docs/POLICY.md：抖音日 <= 2（测试期口径），
    # 无论怎么配都不会超过 publishers.douyin.publisher.DAILY_LIMIT_CEILING
    douyin_daily_limit: int = 2
    # 两次发布最小间隔（分钟）。比全局 SW_MIN_PUBLISH_INTERVAL_SECONDS 更保守
    douyin_min_interval_minutes: int = 30
    # 对账 / 指标扫描内容管理页最近多少条作品，以及"多久以内算这次发的"
    douyin_recent_posts: int = 20
    douyin_reconcile_window_hours: int = 24
    douyin_timeout_seconds: float = 30.0
    # 上传成片 + 平台转码 + 等页面跳转，比小红书还慢
    douyin_publish_timeout_seconds: float = 1200.0
    # 素材目录映射：core 侧路径 -> 宿主机路径。两个都留空 = core 与上传器同机（常规形态）
    douyin_media_local_dir: str = ""
    douyin_media_host_dir: str = ""
    # 抖音登录巡检间隔（分钟）：每次巡检都会真开浏览器，比小红书贵得多，节流要更狠
    douyin_login_health_interval_minutes: int = 30

    # 小红书 sidecar 生命周期（core/sidecars.py）
    # docker = 由 core 直接起/停容器（生产）；none = 只建账号不起容器，
    # 账号页如实显示"sidecar 未接入"（本机开发、CI、e2e 一律用 none）
    sw_sidecar_driver: Literal["docker", "none"] = "none"
    # sidecar 镜像。上游 xpzouying/xiaohongshu-mcp **只发 amd64**；aarch64 服务器要先
    # 本地 docker build 一个 arm64 镜像，再把镜像名填这里（见 sidecars/xhs/README.md）
    sw_xhs_mcp_image: str = "xpzouying/xiaohongshu-mcp:v2.5.0"
    # docker 可执行文件。容器内跑 core 时可能是挂进来的别的路径
    sw_docker_bin: str = "docker"
    # 单次 docker CLI 调用超时（秒）。docker run 要拉镜像时会久，给宽一点
    sw_docker_timeout_seconds: float = 120.0
    # 新账号自动分配宿主机端口的起点，逐个 +1 找空闲
    sw_sidecar_base_port: int = 18060

    # 小红书 sidecar（xiaohongshu-mcp，一账号一容器）
    # 所有 sidecar 共用的 AUTH_TOKEN；每个容器用不同 token 时改用 XHS_MCP_TOKENS
    xhs_auth_token: str = ""
    # 逐账号 token：acc1=token1,acc2=token2（凭据只走环境变量，绝不入库）
    xhs_mcp_tokens: str = ""
    # accounts 表没填 daily_limit 时的兜底值。计划 2.3：小红书日 <= 50，测试期建议 <= 10
    xhs_daily_limit: int = 50
    # 对账 / 指标扫描主页最近多少条笔记
    xhs_reconcile_notes: int = 20
    xhs_timeout_seconds: float = 30.0
    # 发布要传图 + 等页面跳转，浏览器自动化很慢，单独给更长的超时
    xhs_publish_timeout_seconds: float = 300.0
    # 素材目录：宿主机路径 -> sidecar 容器内路径（compose 里只读挂载）。
    # 置空表示 sidecar 与 core 共享文件系统（如 macOS 上直接跑二进制），路径原样透传
    xhs_media_host_dir: str = "data/media"
    xhs_media_container_dir: str = "/app/images"
    # 登录态巡检间隔（分钟），见 core.scheduler.tick_login_health
    xhs_login_health_interval_minutes: int = 10

    # 通知
    feishu_webhook: str = ""
    # 同一 (account_id, event_kind) 在这么多分钟内只推一次（core/notify.py 的节流层）。
    # 登录巡检默认 10 分钟一轮，不节流的话"你的号掉线了"会一天推上百条，
    # 用户第一反应是把 bot 静音——那等于把通知通道整个废掉
    sw_notify_throttle_minutes: int = 120
    # 通知里那些 `/review/xxx` 链接的对外基址（如 https://ops.example.com）。
    # 留空则只给相对路径——Telegram 里点不动，但至少不会拼出一个错的域名
    sw_public_base_url: str = ""

    # Telegram（P12：通知 + 发布前人工确认闸门）
    # 凭据只走 .env，不入库、不进前端、不写日志明文（打日志一律过 mask_token）
    telegram_bot_token: str = ""
    # 目标会话 id。**没有它就拒绝发送**并给出人话提示，
    # 用 `uv run python -m core.telegram setup` 抓
    telegram_chat_id: str = ""
    # 额外允许点按钮的 user id（逗号分隔）。留空 = 只认 chat_id 本人。
    # 群组里 chat_id 是群、点按钮的是群成员，这时才需要它
    sw_telegram_allowed_user_ids: str = ""
    # false = 连 long polling 线程都不起（CI / 本机开发的默认形态由 .env 决定）
    sw_telegram_enabled: bool = True
    sw_telegram_api_base: str = "https://api.telegram.org"
    sw_telegram_timeout_seconds: float = 15.0
    # long polling 的 `timeout` 参数：服务端 hold 住这么久没消息才返回空
    sw_telegram_poll_timeout_seconds: int = 30
    # getUpdates 游标落盘位置，进程重启后不重复消费已处理的回调
    sw_telegram_state_file: str = "data/telegram_state.json"
    # callback_data 的 HMAC 密钥。留空按 SW_UI_TOKEN → bot token 顺序回落，
    # 三个都空则**不发带按钮的卡片**（没有密钥就没有防伪造，宁可不发）
    sw_telegram_signing_secret: str = ""

    # 人工确认闸门（P12）。autopilot 只影响"自动批准"，**不影响"发布前要人点"**
    # ——小红书 2026-03 公告封禁 AI 全托管账号，这一环是合规底线
    sw_confirm_ttl_hours: int = 24
    # 槽位前这么多分钟仍未确认，补推一次提醒（只补一次）
    sw_confirm_remind_minutes: int = 30

    # 成本闸门
    daily_token_budget: int = 2_000_000
    daily_render_seconds_budget: int = 3600
    # 生图按**张**计（见 core.budget.CostKind.IMAGES）。40 张 ≈ 一天 13 条三图笔记，
    # 够用且撞墙时损失可控。
    # 主名跟上面两个闸门对齐（无 SW_ 前缀）；``SW_DAILY_IMAGE_BUDGET`` 也认——
    # 两种拼法都有人会写，认错一个的后果是配置**静默失效**，那比多一行别名贵得多
    daily_image_budget: int = Field(
        default=40,
        validation_alias=AliasChoices("daily_image_budget", "sw_daily_image_budget"),
    )

    # preflight 是否真发一张图去探生图权限。默认关：探一次就是一张图的钱，
    # 而门禁自检在 CI / 每次开工都会跑
    sw_preflight_imagegen: bool = False

    # 复盘 Agent（metrics/insights.py）
    insights_enabled: bool = True
    # 每个账号多久复盘一次（小时）。指标是 24h/7d 快照，比这更密没有新信息
    insights_interval_hours: int = 24
    # insights.md 里保留最近几条复盘（追加写，超出的从头砍掉）
    insights_keep: int = 6
    # 7 天内已发少于这么多条就不复盘（样本太小，结论全是噪声）
    insights_min_posts: int = 3

    @property
    def node_bin(self) -> str:
        """历史别名，等价于 :attr:`wenyan_node_bin`。新代码请直接用后者。"""
        return self.wenyan_node_bin

    def xhs_endpoint_map(self) -> dict[str, str]:
        """解析 ``XHS_MCP_ENDPOINTS``：``acc1=http://h:1,acc2=http://h:2``。"""
        return _parse_pairs(self.xhs_mcp_endpoints)

    def xhs_token_map(self) -> dict[str, str]:
        """解析 ``XHS_MCP_TOKENS``：``acc1=token1,acc2=token2``。"""
        return _parse_pairs(self.xhs_mcp_tokens)

    def telegram_allowed_user_ids(self) -> frozenset[int]:
        """解析 ``SW_TELEGRAM_ALLOWED_USER_IDS``：``123,456``。非数字项忽略。"""
        ids: set[int] = set()
        for part in self.sw_telegram_allowed_user_ids.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                ids.add(int(part))
            except ValueError:
                continue
        return frozenset(ids)

    def newsnow_source_ids(self) -> list[str]:
        """解析 ``NEWSNOW_SOURCES``：``weibo,zhihu,baidu``（保序去重）。"""
        seen: dict[str, None] = {}
        for part in self.newsnow_sources.split(","):
            part = part.strip()
            if part:
                seen.setdefault(part, None)
        return list(seen)


#: 历史环境变量别名 → 现行名。别名仍然生效（见各字段的 ``AliasChoices``），
#: 但 ``scripts/preflight.py`` 会把它们报成 WARN，提示改名。
#: 刻意**不用** ``warnings.warn``：pyproject 把 core.* 的 DeprecationWarning 提成了
#: error，会把只是沿用老变量名的部署直接打崩。
DEPRECATED_ENV_ALIASES: dict[str, str] = {
    "NODE_BIN": "WENYAN_NODE_BIN",
    "ANTHROPIC_MODEL": "LLM_MODEL",
    "DOUYIN_AGENT_BASE_URL": "DOUYIN_SERVICE_URL",
    # P15 S2：``DSH_`` 是 dsh 产品 CLI 的保留启动前缀，项目 `.env` 里出现任何一个
    # 都会让对话台在本仓库目录下拒绝启动（无开关）。改名成 ``SW_DSH_*``，旧名回退。
    "DSH_PROVIDER": "SW_DSH_PROVIDER",
    "DSH_MODEL": "SW_DSH_MODEL",
    "DSH_CORDIS_PATH": "SW_DSH_CORDIS_PATH",
    "DSH_SESSION_ROOT": "SW_DSH_SESSION_ROOT",
    "DSH_MAX_TOKENS": "SW_DSH_MAX_TOKENS",
    "DSH_MAX_LIVE_RUNTIMES": "SW_DSH_MAX_LIVE_RUNTIMES",
}


def _env_file_values() -> Mapping[str, str]:
    """读一遍当前配置文件的**变量名与值**，读不到就当空。

    为什么需要它：pydantic-settings 读 `.env` 时**不会**把值写进 ``os.environ``，
    所以只看 ``os.environ`` 会漏掉"只写在 .env 里"的旧名——而那恰恰是最常见的形态
    （也正是 ``DSH_PROVIDER`` 撞名的那一份）。漏掉就等于这套弃用提示对真实部署失灵。
    """
    try:
        from dotenv import dotenv_values

        return {k: v for k, v in dotenv_values(config_env_file()).items() if v is not None}
    except Exception:
        return {}


def deprecated_env_aliases(env: Mapping[str, str] | None = None) -> list[tuple[str, str]]:
    """列出当前环境里仍在用的历史别名 ``(旧名, 新名)``。

    只有"旧名有值且新名没值"才算——两个都写了说明部署方已经在迁移，不必再唠叨。
    不传 ``env`` 时看的是 ``.env`` 与进程环境的并集（进程环境优先，与 pydantic 同序）。
    """
    if env is not None:
        source: Mapping[str, str] = env
    else:
        source = {**_env_file_values(), **os.environ}
    return [
        (old, new)
        for old, new in DEPRECATED_ENV_ALIASES.items()
        if source.get(old) and not source.get(new)
    ]


def log_deprecated_env_aliases() -> list[tuple[str, str]]:
    """把仍在用的旧变量名打成一条 WARNING 日志。**只记名字，绝不记值。**"""
    stale = deprecated_env_aliases()
    if stale:
        logging.getLogger(__name__).warning(
            "检测到 %d 个已弃用的环境变量名，仍然生效但请尽快改名：%s",
            len(stale),
            "；".join(f"{old} → {new}" for old, new in stale),
        )
    return stale


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    # 进程生命周期里只会跑一次（lru_cache），所以这条提示不会刷屏。
    log_deprecated_env_aliases()
    return settings


def reload_settings() -> Settings:
    """测试 / 改环境变量后重新读取。"""
    get_settings.cache_clear()
    return get_settings()
