"""宿主机抖音上传器：常驻**有头**浏览器进程，由 core 经本地 HTTP 驱动。

启动（必须在有图形界面的宿主机上，**不进 Docker**）::

    uv sync --extra douyin
    uv run patchright install chromium        # 用 channel=chrome 时可跳过
    uv run python -m publishers.douyin serve --port 8710

为什么是独立进程
----------------
抖音会检测 headless 浏览器，发布流程还会触发短信二次验证，两者都要求"真人在场的
有头窗口"。core 可能跑在容器里（无图形界面），所以把浏览器这一层拆成宿主机常驻进程，
core 只发本地 HTTP。见 `docs/POLICY.md` 与实施计划 2.2。

红线（`docs/POLICY.md`，审计逐条核）
-----------------------------------
- 浏览器层**只有 patchright 自带能力**：不设 user_agent、不注入 init script、
  不改 navigator 属性、不加任何 args/指纹伪装。``headless`` **写死 False**，没有开关。
- 一个真人账号一个 ``profile_dir``（``profiles/douyin/<account_id>/``），长期保活，
  **不提供任何跨账号复用 profile 的入口**，不做 Cookie 池。
- 遇到验证码 / 短信二次验证：**暂停并把状态回给 core**，等真人处理。
  ``/sms_code`` 只把人**自己收到**的验证码填进输入框，**绝不识别、绝不对接打码平台**。
- 同平台串行：所有浏览器操作在**同一个 worker 线程**里排队执行，不并发。
- 操作间 0.8–2.5s 随机停顿：这是"别把页面点崩"的人类节奏，不是规避手段。

选择器
------
`SELECTORS` 里的 CSS 依据 **2026-08 抖音创作者中心页面观察**写成，
**未在真实站点验证，平台改版即可能失效**。每个动作都给了多个候选，按顺序取第一个可见的；
线上失配时不用改代码——用 ``DOUYIN_SELECTORS_FILE`` 指向一个 JSON 覆盖表即可
（格式与 :data:`SELECTORS` 相同，按 key 合并）。失败一律截图到
``data/douyin/screenshots/``，照着截图改选择器。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import random
import re
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

from publishers.douyin.client import (
    DATA_CENTER_URL,
    MANAGE_URL,
    STATE_BROWSER_ERROR,
    STATE_BUSY,
    STATE_IDENTITY_MISMATCH,
    STATE_INVALID_CONTENT,
    STATE_LOGGED_IN,
    STATE_LOGGED_OUT,
    STATE_NEEDS_CAPTCHA,
    STATE_NEEDS_SMS,
    STATE_NO_BROWSER,
    STATE_NO_SMS_INPUT,
    STATE_OK,
    STATE_PUBLISHED,
    STATE_SCHEDULED,
    STATE_TIMEOUT,
    STATE_WAITING_USER,
    UPLOAD_URL,
    mask_nickname,
    normalise_title,
    parse_count,
    parse_rfc3339,
    video_url,
)

logger = logging.getLogger("social_workflow.publishers.douyin.service")

SERVICE_NAME = "douyin-uploader"
SERVICE_VERSION = "0.1.0"

DEFAULT_PROFILE_ROOT = "profiles/douyin"
DEFAULT_SCREENSHOT_ROOT = "data/douyin/screenshots"

# 页面等待（秒）
NAV_TIMEOUT = 60.0
UPLOAD_TIMEOUT = 900.0  # 传成片 + 平台转码，几分钟很常见
AFTER_PUBLISH_TIMEOUT = 180.0
ELEMENT_TIMEOUT = 15.0

# 登录页 / 风控页的 URL 特征
LOGIN_URL_MARKERS = ("/login", "passport", "sso", "verify")
MANAGE_URL_MARKER = "/content/manage"

# 作品链接里抽 aweme_id：/video/7412345678901234567 或 ?item_id=...
_POST_ID_IN_URL = re.compile(r"/video/(\d{6,})|[?&](?:item_id|aweme_id|itemId)=(\d{6,})")
# "3小时前" / "2026-08-16 18:00" 两种时间写法都要认
_ABS_TIME = re.compile(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?")


# --------------------------------------------------------------------- 选择器

#: 依据 2026-08 创作者中心页面观察，**未在真实站点验证**，可能随改版失效。
#: 每项是"候选列表"，按顺序取第一个可见的元素。
SELECTORS: dict[str, list[str]] = {
    # 已登录时右上角/侧边栏的账号昵称。抖音的 class 名带构建哈希（如 .name-_lSSDc），
    # 所以一律用属性包含匹配而不是精确 class。
    "nickname": [
        '[data-e2e="user-nickname"]',
        'div[class*="user-info"] [class*="name"]',
        'div[class*="account-info"] [class*="name"]',
        'div[class*="semi-navigation-footer"] [class*="name"]',
        'span[class*="nickname"]',
    ],
    # 未登录时出现的登录入口 / 二维码容器
    "login_marker": [
        'div[class*="login-scan"]',
        'div[class*="qrcode"] canvas',
        'div[class*="login"] img[class*="qrcode"]',
        'button:has-text("登录")',
    ],
    # 上传页的文件输入框（通常隐藏在"点击上传"按钮后面，set_input_files 不需要它可见）
    "upload_input": [
        'input[type="file"][accept*="video"]',
        'input[type="file"][accept*="mp4"]',
        'input[type="file"]',
    ],
    # 成片上传/转码完成的信号
    "upload_done": [
        "text=上传成功",
        "text=上传完成",
        'div[class*="video-info"]',
        'div[class*="preview"] video',
    ],
    "upload_failed": [
        "text=上传失败",
        "text=转码失败",
        "text=视频格式不支持",
    ],
    "title_input": [
        'input[placeholder*="填写作品标题"]',
        'input[placeholder*="作品标题"]',
        'input[placeholder*="标题"]',
        'div[class*="title"] input[type="text"]',
    ],
    # 简介是富文本编辑器（contenteditable），不是 textarea
    "description_editor": [
        'div[data-placeholder*="作品简介"]',
        'div[class*="editor-kit-container"] div[contenteditable="true"]',
        'div[class*="zone-container"][contenteditable="true"]',
        'div[contenteditable="true"]',
    ],
    "cover_entry": [
        'div[class*="cover"] div[class*="upload"]',
        "text=选择封面",
        "text=设置封面",
    ],
    "cover_input": [
        'input[type="file"][accept*="image"]',
    ],
    "cover_confirm": [
        'button:has-text("完成")',
        'button:has-text("确定")',
        'button:has-text("保存")',
    ],
    "schedule_radio": [
        'label:has-text("定时发布")',
        "text=定时发布",
    ],
    "schedule_input": [
        'input[placeholder*="日期"]',
        'div[class*="semi-datepicker"] input',
    ],
    "publish_button": [
        'button:has-text("发布")',
        'button[class*="publish"]',
    ],
    # 发布后落到内容管理页的信号
    "publish_done": [
        "text=发布成功",
        'div[class*="content-card"]',
    ],
    "publish_rejected": [
        "text=内容涉嫌违规",
        "text=不符合社区规范",
        "text=审核不通过",
        "text=含有违规信息",
    ],
    # 需要真人处理的两类拦截
    "sms_input": [
        'input[placeholder*="验证码"]',
        'input[name="code"][maxlength="6"]',
        'div[class*="verify"] input[type="tel"]',
    ],
    "sms_submit": [
        'button:has-text("验证")',
        'button:has-text("确认")',
        'button:has-text("提交")',
    ],
    "captcha_marker": [
        'div[id*="captcha"]',
        'div[class*="captcha"]',
        'iframe[src*="captcha"]',
        "text=拖动滑块",
        "text=请完成安全验证",
    ],
    # 内容管理页的作品卡片
    "post_card": [
        'div[class*="content-card"]',
        'div[class*="work-card"]',
        'li[class*="video-card"]',
    ],
    "post_card_title": [
        'div[class*="title"]',
        'span[class*="title"]',
        'a[class*="title"]',
    ],
    "post_card_time": [
        'div[class*="time"]',
        'span[class*="time"]',
    ],
    # 数据中心里单条作品的指标（顺序：播放 / 点赞 / 评论 / 分享）
    "metric_row": [
        'div[class*="data-item"]',
        'tr[class*="row"]',
    ],
}


def load_selectors(path: str | os.PathLike[str] | None = None) -> dict[str, list[str]]:
    """读取选择器覆盖表并与内置表合并（按 key 覆盖，不做深合并）。

    平台改版时优先改这个 JSON，不用改代码、不用重新发版。
    """
    merged = {key: list(value) for key, value in SELECTORS.items()}
    target = str(path or os.environ.get("DOUYIN_SELECTORS_FILE") or "")
    if not target:
        return merged
    file = Path(target).expanduser()
    if not file.is_file():
        logger.warning("DOUYIN_SELECTORS_FILE 指向的文件不存在，忽略：%s", file)
        return merged
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except ValueError as exc:
        logger.warning("选择器覆盖表不是合法 JSON，忽略：%s（%s）", file, exc)
        return merged
    if not isinstance(data, dict):
        logger.warning("选择器覆盖表必须是对象，忽略：%s", file)
        return merged
    for key, value in data.items():
        if isinstance(value, str):
            merged[str(key)] = [value]
        elif isinstance(value, list):
            merged[str(key)] = [str(v) for v in value]
    logger.info("已加载选择器覆盖表 %s（覆盖 %d 项）", file, len(data))
    return merged


# ----------------------------------------------------------------- 人类节奏


class Pacer:
    """操作之间的随机停顿（0.8–2.5s）。

    这是**节奏**不是规避：抖音创作者中心的编辑器是重前端，连续无间隔地
    fill/click 经常丢事件；给一点间隔既像人也更稳。停顿本身不隐藏任何东西。
    """

    def __init__(
        self,
        *,
        low: float = 0.8,
        high: float = 2.5,
        sleeper: Callable[[float], None] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.low = low
        self.high = high
        self._sleep = sleeper or time.sleep
        self._rng = rng or random.Random()

    def __call__(self, scale: float = 1.0) -> None:
        self._sleep(self._rng.uniform(self.low, self.high) * scale)

    def sleep(self, seconds: float) -> None:
        self._sleep(seconds)


# ------------------------------------------------------------------- 截图


class Shooter:
    """每一步失败都截图，落到 ``data/douyin/screenshots/<account>/``。

    截图是选择器失配时**唯一**能让人快速定位的证据，所以宁可多存。
    文件名只含账号 id / 步骤名 / UTC 时间戳，不含内容标题（避免把内容洒进文件名）。
    """

    def __init__(self, root: str | os.PathLike[str] = DEFAULT_SCREENSHOT_ROOT) -> None:
        self.root = Path(root)

    def path_for(self, account_id: str, step: str, *, now: datetime | None = None) -> Path:
        moment = now or datetime.now(UTC)
        stamp = moment.strftime("%Y%m%dT%H%M%S%f")[:-3]
        safe_account = re.sub(r"[^A-Za-z0-9_.-]", "_", account_id) or "unknown"
        safe_step = re.sub(r"[^A-Za-z0-9_.-]", "_", step) or "step"
        return self.root / safe_account / f"{stamp}-{safe_step}.png"

    def shoot(self, page: Any, account_id: str, step: str) -> str:
        """截图并返回路径；截图本身失败也不该让主流程崩。"""
        target = self.path_for(account_id, step)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(target), full_page=False)
        except Exception as exc:  # pragma: no cover - 只有浏览器已关闭时触发
            logger.warning("截图失败（%s/%s）：%s", account_id, step, exc)
            return ""
        logger.info("已截图 %s", target)
        return str(target)


# --------------------------------------------------------------- 页面小工具


def first_visible(page: Any, selectors: Iterable[str]) -> Any | None:
    """按顺序返回第一个"存在且可见"的元素，全都找不到返回 None。

    刻意吞掉每个候选自身的异常：候选列表里混着不同版本页面的选择器，
    某一条语法在当前页面上报错是正常的，不该中断整轮尝试。
    """
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() > 0 and locator.is_visible():
                return locator
        except Exception:  # 候选失配是预期情况，换下一条
            continue
    return None


def wait_visible(
    page: Any,
    selectors: Iterable[str],
    *,
    timeout: float,
    pacer: Pacer,
    poll: float = 1.0,
) -> Any | None:
    """轮询等待任一候选出现。超时返回 None（由调用方决定怎么报状态）。"""
    deadline = time.monotonic() + timeout
    candidates = list(selectors)
    while True:
        found = first_visible(page, candidates)
        if found is not None:
            return found
        if time.monotonic() >= deadline:
            return None
        pacer.sleep(poll)


def text_of(locator: Any) -> str:
    try:
        return (locator.inner_text() or "").strip()
    except Exception:
        return ""


# ------------------------------------------------------------ 页面动作封装


@dataclass
class PublishRequest:
    """一次发布请求（服务内部使用的归一化形式）。"""

    title: str
    description: str = ""
    video_path: str = ""
    hashtags: list[str] = field(default_factory=list)
    cover_path: str = ""
    schedule_at: str = ""
    identity_hint: str = ""


class DouyinAutomation:
    """创作者中心的页面动作。**只接受 ``page``，不管浏览器怎么来的**，
    所以可以拿本地静态 HTML 假页面做冒烟测试（见 tests/publishers/test_douyin.py）。
    """

    def __init__(
        self,
        *,
        account_id: str,
        selectors: dict[str, list[str]] | None = None,
        pacer: Pacer | None = None,
        shooter: Shooter | None = None,
    ) -> None:
        self.account_id = account_id
        self.selectors = selectors or load_selectors()
        self.pace = pacer or Pacer()
        self.shooter = shooter or Shooter()

    # -- 基础 -------------------------------------------------------------

    def sel(self, key: str) -> list[str]:
        return self.selectors.get(key, [])

    def find(self, page: Any, key: str) -> Any | None:
        return first_visible(page, self.sel(key))

    def shoot(self, page: Any, step: str) -> str:
        return self.shooter.shoot(page, self.account_id, step)

    # -- 登录态 / identity -------------------------------------------------

    def read_nickname(self, page: Any) -> str:
        """读页面上的账号昵称。读不到返回空串（**不是**报错）。"""
        node = self.find(page, "nickname")
        return normalise_title(text_of(node)) if node is not None else ""

    def looks_logged_out(self, page: Any) -> bool:
        url = ""
        try:
            url = str(page.url or "")
        except Exception:
            url = ""
        if any(marker in url for marker in LOGIN_URL_MARKERS):
            return True
        return self.find(page, "login_marker") is not None

    def blocked_state(self, page: Any) -> str | None:
        """页面是不是卡在需要真人处理的拦截上。

        红线：识别到验证码只**上报**，绝不尝试自动通过（docs/POLICY.md）。
        """
        if self.find(page, "captcha_marker") is not None:
            return STATE_NEEDS_CAPTCHA
        if self.find(page, "sms_input") is not None:
            return STATE_NEEDS_SMS
        return None

    def login_state(self, page: Any) -> dict[str, Any]:
        """判断当前登录态，返回 envelope 片段。"""
        blocked = self.blocked_state(page)
        if blocked is not None:
            return {
                "ok": False,
                "state": blocked,
                "detail": (
                    "页面停在需要真人处理的验证上："
                    + ("短信验证码" if blocked == STATE_NEEDS_SMS else "图形/滑块验证")
                    + "。请到宿主机窗口处理，或在 core 的登录页提交验证码。"
                ),
            }
        nickname = self.read_nickname(page)
        if nickname:
            return {
                "ok": True,
                "state": STATE_LOGGED_IN,
                "nickname": mask_nickname(nickname),
                "detail": f"已登录：{mask_nickname(nickname)}",
            }
        if self.looks_logged_out(page):
            return {
                "ok": False,
                "state": STATE_LOGGED_OUT,
                "detail": "创作者中心显示未登录，请在宿主机弹出的浏览器窗口里用抖音 App 扫码",
            }
        return {
            "ok": False,
            "state": STATE_BROWSER_ERROR,
            "detail": "页面既读不到昵称也不像登录页——多半是选择器失配，见截图",
        }

    def check_identity(self, page: Any, identity_hint: str) -> dict[str, Any] | None:
        """发布前的**防发错号**闸门：页面昵称必须与 identity_hint 一致。

        命中不一致返回 envelope（调用方原样回给 core → PermanentError）；一致返回 None。
        没配 hint 时只记一条 warning 放行——但 README 里明确要求生产必须配。
        """
        hint = normalise_title(identity_hint)
        nickname = self.read_nickname(page)
        if not hint:
            logger.warning(
                "账号 %s 未配置 identity_hint，跳过防发错号校验（生产环境必须配，见 README）",
                self.account_id,
            )
            return None
        if not nickname:
            shot = self.shoot(page, "identity-unreadable")
            return {
                "ok": False,
                "state": STATE_IDENTITY_MISMATCH,
                "detail": (
                    "读不到当前登录昵称，无法确认发到哪个号上——为防发错号直接中止。"
                    "多半是 nickname 选择器失配，见截图。"
                ),
                "screenshot_path": shot,
            }
        if nickname != hint:
            shot = self.shoot(page, "identity-mismatch")
            return {
                "ok": False,
                "state": STATE_IDENTITY_MISMATCH,
                "detail": (
                    f"当前浏览器登录的是 {mask_nickname(nickname)}，"
                    f"但账号 {self.account_id} 期望 {mask_nickname(hint)}——已中止，防发错号。"
                ),
                "screenshot_path": shot,
            }
        return None

    # -- 短信验证码（只填写，不识别） ---------------------------------------

    def fill_sms_code(self, page: Any, code: str) -> dict[str, Any]:
        """把真人输入的验证码填进当前页面的验证码输入框并提交。

        红线：本方法**不读取、不识别**任何图片；验证码来自真人的手机短信，
        经 core 的 ``/accounts/{id}/login/code`` 转发过来（docs/POLICY.md）。
        """
        node = self.find(page, "sms_input")
        if node is None:
            return {
                "ok": False,
                "state": STATE_NO_SMS_INPUT,
                "detail": "当前页面上没有验证码输入框——是不是已经过了这一步？",
            }
        try:
            node.fill(code)
        except Exception as exc:
            shot = self.shoot(page, "sms-fill-failed")
            return {
                "ok": False,
                "state": STATE_BROWSER_ERROR,
                "detail": f"填写验证码失败：{type(exc).__name__}",
                "screenshot_path": shot,
            }
        self.pace()
        submit = self.find(page, "sms_submit")
        if submit is not None:
            try:
                submit.click()
            except Exception as exc:  # 点不动就交给人在窗口里点
                logger.warning("验证码提交按钮点击失败（%s），等真人在窗口里点", type(exc).__name__)
        self.pace()
        return {"ok": True, "state": STATE_OK, "detail": "验证码已填入页面"}

    # -- 发布 -------------------------------------------------------------

    def publish(self, page: Any, req: PublishRequest) -> dict[str, Any]:
        """走完整个上传发布流程，返回 envelope。

        流程（参考 ``dreammis/social-auto-upload`` 的**行为**后自行实现，
        未复制其任何代码；该仓库无 License，见 docs/THIRD_PARTY.md）：

        进入上传页 → 选文件 → 等上传/转码完成 → 填标题 → 填简介+话题 →
        （可选）封面 →（可选）定时 → 点发布 → 等跳内容管理页 → 回填作品 id。
        """
        page.goto(UPLOAD_URL, timeout=int(NAV_TIMEOUT * 1000), wait_until="domcontentloaded")
        self.pace()

        state = self.login_state(page)
        if state["state"] != STATE_LOGGED_IN:
            state["screenshot_path"] = self.shoot(page, "publish-not-logged-in")
            return state

        mismatch = self.check_identity(page, req.identity_hint)
        if mismatch is not None:
            return mismatch

        step = self._upload_video(page, req)
        if step is not None:
            return step

        step = self._fill_copy(page, req)
        if step is not None:
            return step

        if req.cover_path:
            self._set_cover(page, req.cover_path)  # 封面失败不阻断发布，只记日志

        scheduled = False
        if req.schedule_at:
            step = self._set_schedule(page, req.schedule_at)
            if step is not None:
                return step
            scheduled = True

        blocked = self.blocked_state(page)
        if blocked is not None:
            return {
                "ok": False,
                "state": blocked,
                "detail": "点发布前页面弹出了需要真人处理的验证，已暂停等待人工",
                "screenshot_path": self.shoot(page, "publish-blocked-before-click"),
            }

        return self._click_publish(page, req, scheduled=scheduled)

    def _upload_video(self, page: Any, req: PublishRequest) -> dict[str, Any] | None:
        node = first_visible(page, self.sel("upload_input"))
        if node is None:
            # 文件输入框常被样式隐藏，set_input_files 对隐藏元素同样有效，
            # 所以可见性检查失败时再退回"不看可见性"的直接定位
            node = self._locate_hidden(page, "upload_input")
        if node is None:
            return {
                "ok": False,
                "state": STATE_BROWSER_ERROR,
                "detail": "上传页找不到 input[type=file]——选择器失配，见截图",
                "screenshot_path": self.shoot(page, "upload-input-missing"),
            }
        try:
            node.set_input_files(req.video_path)
        except Exception as exc:
            return {
                "ok": False,
                "state": STATE_INVALID_CONTENT,
                "detail": f"成片交给浏览器失败（路径 {req.video_path} 在宿主机上存在吗？）：{exc}",
                "screenshot_path": self.shoot(page, "upload-set-file-failed"),
            }
        self.pace()

        deadline = time.monotonic() + UPLOAD_TIMEOUT
        while True:
            if first_visible(page, self.sel("upload_failed")) is not None:
                return {
                    "ok": False,
                    "state": STATE_INVALID_CONTENT,
                    "detail": "平台报上传/转码失败（格式或编码不被接受）",
                    "screenshot_path": self.shoot(page, "upload-failed"),
                }
            blocked = self.blocked_state(page)
            if blocked is not None:
                return {
                    "ok": False,
                    "state": blocked,
                    "detail": "上传过程中弹出需要真人处理的验证，已暂停",
                    "screenshot_path": self.shoot(page, "upload-blocked"),
                }
            # 标题框出现同样说明已经进了编辑页（不同版本的完成信号不一样）
            if (
                first_visible(page, self.sel("upload_done")) is not None
                or first_visible(page, self.sel("title_input")) is not None
            ):
                return None
            if time.monotonic() >= deadline:
                return {
                    "ok": False,
                    "state": STATE_TIMEOUT,
                    "detail": f"等成片上传/转码超过 {UPLOAD_TIMEOUT:.0f}s 仍未完成",
                    "screenshot_path": self.shoot(page, "upload-timeout"),
                }
            self.pace.sleep(2.0)

    def _locate_hidden(self, page: Any, key: str) -> Any | None:
        """只看"存在"不看"可见"，专供被样式藏起来的 file input。"""
        for selector in self.sel(key):
            try:
                locator = page.locator(selector).first
                if locator.count() > 0:
                    return locator
            except Exception:  # 候选失配是预期情况，换下一条
                continue
        return None

    def _fill_copy(self, page: Any, req: PublishRequest) -> dict[str, Any] | None:
        title_node = wait_visible(
            page, self.sel("title_input"), timeout=ELEMENT_TIMEOUT, pacer=self.pace
        )
        if title_node is None:
            return {
                "ok": False,
                "state": STATE_BROWSER_ERROR,
                "detail": "找不到标题输入框——选择器失配，见截图",
                "screenshot_path": self.shoot(page, "title-input-missing"),
            }
        try:
            title_node.fill(req.title)
        except Exception as exc:
            return {
                "ok": False,
                "state": STATE_BROWSER_ERROR,
                "detail": f"填标题失败：{type(exc).__name__}: {exc}",
                "screenshot_path": self.shoot(page, "title-fill-failed"),
            }
        self.pace()

        editor = self.find(page, "description_editor")
        if editor is None:
            logger.warning("找不到简介编辑器，跳过简介与话题（标题已填）")
            return None
        try:
            editor.click()
            self.pace(0.3)
            if req.description:
                editor.type(req.description, delay=12)
                self.pace(0.3)
            for tag in req.hashtags:
                # 抖音的话题是"输入 #xxx 后敲空格"变成话题标签
                editor.type(f"#{tag}", delay=18)
                self.pace(0.2)
                page.keyboard.press("Space")
                self.pace(0.2)
        except Exception as exc:
            logger.warning("填简介/话题失败（标题已填，继续发布）：%s", exc)
        self.pace()
        return None

    def _set_cover(self, page: Any, cover_path: str) -> None:
        """设置自定义封面。失败只记日志：平台会用首帧兜底，不值得为它中止发布。"""
        try:
            entry = self.find(page, "cover_entry")
            if entry is not None:
                entry.click()
                self.pace()
            node = self._locate_hidden(page, "cover_input")
            if node is None:
                logger.warning("找不到封面上传入口，改用平台默认首帧")
                return
            node.set_input_files(cover_path)
            self.pace(1.5)
            confirm = self.find(page, "cover_confirm")
            if confirm is not None:
                confirm.click()
                self.pace()
        except Exception as exc:
            logger.warning("设置封面失败（改用平台默认首帧）：%s", exc)

    def _set_schedule(self, page: Any, schedule_at: str) -> dict[str, Any] | None:
        """勾选定时发布并填时间。时间按**宿主机本地时区**填（页面就是本地时区）。"""
        try:
            moment = parse_rfc3339(schedule_at).astimezone()
        except ValueError:
            return {
                "ok": False,
                "state": STATE_INVALID_CONTENT,
                "detail": f"schedule_at 不是 RFC3339 时间串：{schedule_at!r}",
            }
        radio = self.find(page, "schedule_radio")
        if radio is None:
            return {
                "ok": False,
                "state": STATE_BROWSER_ERROR,
                "detail": "找不到「定时发布」选项——选择器失配，见截图",
                "screenshot_path": self.shoot(page, "schedule-radio-missing"),
            }
        try:
            radio.click()
            self.pace()
            box = wait_visible(
                page, self.sel("schedule_input"), timeout=ELEMENT_TIMEOUT, pacer=self.pace
            )
            if box is None:
                raise RuntimeError("定时时间输入框没出现")
            box.fill(moment.strftime("%Y-%m-%d %H:%M"))
            self.pace(0.4)
            page.keyboard.press("Enter")
        except Exception as exc:
            return {
                "ok": False,
                "state": STATE_BROWSER_ERROR,
                "detail": f"设置定时发布失败：{type(exc).__name__}: {exc}",
                "screenshot_path": self.shoot(page, "schedule-failed"),
            }
        self.pace()
        return None

    def _click_publish(self, page: Any, req: PublishRequest, *, scheduled: bool) -> dict[str, Any]:
        button = self.find(page, "publish_button")
        if button is None:
            return {
                "ok": False,
                "state": STATE_BROWSER_ERROR,
                "detail": "找不到「发布」按钮——选择器失配，见截图",
                "screenshot_path": self.shoot(page, "publish-button-missing"),
            }
        try:
            button.click()
        except Exception as exc:
            return {
                "ok": False,
                "state": STATE_BROWSER_ERROR,
                "detail": f"点发布失败：{type(exc).__name__}: {exc}",
                "screenshot_path": self.shoot(page, "publish-click-failed"),
            }

        deadline = time.monotonic() + AFTER_PUBLISH_TIMEOUT
        while True:
            blocked = self.blocked_state(page)
            if blocked is not None:
                return {
                    "ok": False,
                    "state": blocked,
                    "detail": (
                        "点发布后平台要求二次验证，已暂停等真人处理。"
                        "短信验证码请在 core 的 /accounts/{id}/login 页提交；"
                        "图形验证请直接在宿主机窗口里完成。"
                    ),
                    "screenshot_path": self.shoot(page, "publish-blocked"),
                }
            if first_visible(page, self.sel("publish_rejected")) is not None:
                return {
                    "ok": False,
                    "state": STATE_INVALID_CONTENT,
                    "detail": "平台判定内容违规，已中止（不可重试，请改稿后重新走审核）",
                    "screenshot_path": self.shoot(page, "publish-rejected"),
                }
            url = ""
            try:
                url = str(page.url or "")
            except Exception:
                url = ""
            landed = first_visible(page, self.sel("publish_done")) is not None
            done = MANAGE_URL_MARKER in url or landed
            if done:
                shot = self.shoot(page, "publish-done")
                post_id, post_url = self._resolve_post_id(page, req.title)
                return {
                    "ok": True,
                    "state": STATE_SCHEDULED if scheduled else STATE_PUBLISHED,
                    "post_id": post_id,
                    "url": post_url,
                    "detail": "发布已提交" + ("（定时）" if scheduled else ""),
                    "screenshot_path": shot,
                }
            if time.monotonic() >= deadline:
                return {
                    "ok": False,
                    "state": STATE_TIMEOUT,
                    "detail": (
                        f"点发布后 {AFTER_PUBLISH_TIMEOUT:.0f}s 内没等到内容管理页。"
                        "**可能已经发出去了**——core 侧重试前会先对账，不会重复发。"
                    ),
                    "screenshot_path": self.shoot(page, "publish-timeout"),
                }
            self.pace.sleep(2.0)

    def _resolve_post_id(self, page: Any, title: str) -> tuple[str, str]:
        """发布成功后从内容管理页把作品 id 找回来。找不到返回空串（**绝不重发**）。"""
        wanted = normalise_title(title)
        try:
            for post in self.read_recent_posts(page, limit=10, navigate=False):
                if normalise_title(post.get("title", "")) == wanted and post.get("post_id"):
                    return str(post["post_id"]), str(post.get("url") or "")
        except Exception as exc:
            logger.warning("发布后解析作品 id 失败（不影响发布结果）：%s", exc)
        return "", ""

    # -- 对账 / 指标 --------------------------------------------------------

    def read_recent_posts(
        self, page: Any, *, limit: int = 20, navigate: bool = True
    ) -> list[dict[str, Any]]:
        """读内容管理页最近 N 条作品（标题 + 时间 + 尽力而为的作品 id）。"""
        if navigate:
            page.goto(MANAGE_URL, timeout=int(NAV_TIMEOUT * 1000), wait_until="domcontentloaded")
            self.pace()
        cards: Any = None
        for selector in self.sel("post_card"):
            try:
                locator = page.locator(selector)
                if locator.count() > 0:
                    cards = locator
                    break
            except Exception:  # 候选失配是预期情况，换下一条
                continue
        if cards is None:
            return []
        out: list[dict[str, Any]] = []
        total = min(cards.count(), limit)
        for index in range(total):
            card = cards.nth(index)
            title = self._child_text(card, "post_card_title")
            raw_time = self._child_text(card, "post_card_time")
            post_id, url = self._card_identity(card)
            out.append(
                {
                    "title": title,
                    "post_id": post_id,
                    "url": url,
                    "raw_time": raw_time,
                    "published_at": parse_card_time(raw_time),
                }
            )
        return out

    def _child_text(self, card: Any, key: str) -> str:
        for selector in self.sel(key):
            try:
                node = card.locator(selector).first
                if node.count() > 0:
                    text = normalise_title(node.inner_text() or "")
                    if text:
                        return text
            except Exception:  # 候选失配是预期情况，换下一条
                continue
        return ""

    def _card_identity(self, card: Any) -> tuple[str, str]:
        """从卡片里的链接抽作品 id。抽不到返回空串。"""
        try:
            links = card.locator("a")
            for index in range(min(links.count(), 5)):
                href = links.nth(index).get_attribute("href") or ""
                match = _POST_ID_IN_URL.search(href)
                if match:
                    post_id = match.group(1) or match.group(2)
                    return post_id, video_url(post_id)
        except Exception as exc:
            logger.debug("卡片里没抽到作品 id：%s", exc)
        return "", ""

    def read_metrics(self, page: Any, post_id: str) -> dict[str, Any]:
        """数据中心里单条作品的公开指标（**尽力而为**，读不到就如实报缺失）。"""
        page.goto(DATA_CENTER_URL, timeout=int(NAV_TIMEOUT * 1000), wait_until="domcontentloaded")
        self.pace()
        numbers: list[int | None] = []
        for selector in self.sel("metric_row"):
            try:
                rows = page.locator(selector)
            except Exception:  # 候选失配是预期情况，换下一条
                continue
            for index in range(min(rows.count(), 50)):
                row = rows.nth(index)
                try:
                    html = row.inner_html()
                except Exception:
                    continue
                if post_id and post_id not in html:
                    continue
                numbers = [parse_count(t) for t in _NUMBERS.findall(text_of(row))]
                break
            if numbers:
                break
        if not numbers:
            return {"available": False, "reason": f"数据中心里没找到作品 {post_id} 的指标行"}
        # 页面列顺序：播放 / 点赞 / 评论 / 分享（2026-08 观察，未验证）
        keys = ("views", "likes", "comments", "shares")
        metrics = dict(zip(keys, numbers[: len(keys)], strict=False))
        return {"available": any(v is not None for v in metrics.values()), **metrics}


_NUMBERS = re.compile(r"\d[\d.,]*(?:万|亿|w|k)?")


def parse_card_time(raw: str) -> str:
    """把卡片上的时间文字折算成 RFC3339（UTC）。认不出来返回空串。

    抖音会混用 ``2026-08-16 18:00`` 与 ``3小时前`` 两种写法；相对时间这里只处理
    "分钟/小时/天前"，其余交给调用方按空值处理（对账还有标题这一维）。
    """
    text = (raw or "").strip()
    if not text:
        return ""
    match = _ABS_TIME.search(text)
    if match:
        year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
        hour = int(match.group(4) or 0)
        minute = int(match.group(5) or 0)
        try:
            local = datetime(year, month, day, hour, minute).astimezone()
        except ValueError:
            return ""
        return local.astimezone(UTC).isoformat(timespec="seconds")
    rel = re.search(r"(\d+)\s*(分钟|小时|天)前", text)
    if rel:
        amount = int(rel.group(1))
        unit = rel.group(2)
        seconds = {"分钟": 60, "小时": 3600, "天": 86400}[unit] * amount
        return datetime.fromtimestamp(time.time() - seconds, tz=UTC).isoformat(timespec="seconds")
    return ""


# --------------------------------------------------------- 浏览器（patchright）


class BrowserUnavailable(RuntimeError):
    """patchright 没装 / 浏览器起不来。"""


class BrowserPool:
    """一账号一 ``profile_dir`` 的持久化上下文池。**只在 worker 线程里被访问**。

    红线：``headless`` 写死 ``False`` 且不提供开关；除 ``channel`` 外不传任何
    启动参数——不设 user_agent、不加 args、不注入脚本。patchright 自带能力之外
    的一切"反检测"都不允许（docs/POLICY.md）。
    """

    def __init__(
        self,
        *,
        profile_root: str | os.PathLike[str] = DEFAULT_PROFILE_ROOT,
        channel: str = "chrome",
    ) -> None:
        self.profile_root = Path(profile_root)
        self.channel = channel
        self._pw: Any = None
        self._contexts: dict[str, Any] = {}

    def profile_dir(self, account_id: str) -> Path:
        """``profile_root/<account_id>``。account_id 只做字符白名单，不接受路径穿越。"""
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", account_id)
        # 全是分隔符 / 点的 id（"..", "///"）清洗后仍可能指向上级目录，直接拒
        if not safe or not safe.strip("._-"):
            raise ValueError(f"非法 account_id: {account_id!r}")
        return self.profile_root / safe

    def launched(self) -> list[str]:
        return sorted(self._contexts)

    def _playwright(self) -> Any:
        if self._pw is None:
            try:
                from patchright.sync_api import sync_playwright
            except ImportError as exc:  # pragma: no cover - 取决于本机是否装了 extra
                raise BrowserUnavailable(
                    "未安装 patchright：请在宿主机上跑 `uv sync --extra douyin`"
                    "（patchright 是 Apache-2.0，API 与 playwright 一致）"
                ) from exc
            self._pw = sync_playwright().start()
        return self._pw

    def context(self, account_id: str) -> Any:
        existing = self._contexts.get(account_id)
        if existing is not None:
            return existing
        profile = self.profile_dir(account_id)
        profile.mkdir(parents=True, exist_ok=True)
        pw = self._playwright()
        common = {"user_data_dir": str(profile), "headless": False}
        try:
            context = pw.chromium.launch_persistent_context(channel=self.channel, **common)
            logger.info(
                "已为 %s 启动浏览器（channel=%s, profile=%s）", account_id, self.channel, profile
            )
        except Exception as exc:
            logger.warning(
                "channel=%s 启动失败（%s），回退到 patchright 自带 chromium", self.channel, exc
            )
            try:
                context = pw.chromium.launch_persistent_context(**common)
            except Exception as fallback_exc:
                raise BrowserUnavailable(
                    f"浏览器启动失败：{fallback_exc}。"
                    "宿主机有图形界面吗？装了 Chrome 吗？"
                    "（或跑 `uv run patchright install chromium`）"
                ) from fallback_exc
        self._contexts[account_id] = context
        return context

    def page(self, account_id: str) -> Any:
        context = self.context(account_id)
        pages = [p for p in context.pages if not p.is_closed()]
        return pages[0] if pages else context.new_page()

    def existing_page(self, account_id: str) -> Any | None:
        """只在浏览器**已经开着**时返回页面，不主动拉起（给 /sms_code 用）。"""
        context = self._contexts.get(account_id)
        if context is None:
            return None
        pages = [p for p in context.pages if not p.is_closed()]
        return pages[0] if pages else None

    def close(self) -> None:
        for account_id, context in list(self._contexts.items()):
            try:
                context.close()
            except Exception as exc:  # pragma: no cover
                logger.warning("关闭 %s 的浏览器失败：%s", account_id, exc)
        self._contexts.clear()
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception as exc:  # pragma: no cover
                logger.warning("停止 patchright 失败：%s", exc)
            self._pw = None


# ------------------------------------------------------------- 串行 worker


class ServiceBusy(RuntimeError):
    """已有浏览器作业在跑。同平台串行是红线，不排队硬等。"""


@dataclass
class _Job:
    fn: Callable[[BrowserPool], Any]
    done: threading.Event
    label: str = "job"
    result: Any = None
    error: BaseException | None = None


class BrowserWorker:
    """所有浏览器操作都在**同一个线程**里串行执行。

    两个理由，缺一不可：

    1. patchright / playwright 的 sync API 基于 greenlet，绑定创建它的线程，
       跨线程调用直接崩；FastAPI 的 threadpool 每次可能换线程。
    2. **同平台串行**本身就是红线（不并发、不刷量），一个 worker 线程天然满足。
    """

    def __init__(self, pool: BrowserPool) -> None:
        self.pool = pool
        self._queue: queue.Queue[_Job | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._inflight = 0

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._inflight > 0

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name="douyin-browser", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                self.pool.close()
                return
            try:
                job.result = job.fn(self.pool)
            except BaseException as exc:  # 原样回给提交方，worker 线程不能死
                job.error = exc
            finally:
                job.done.set()

    def submit(
        self,
        fn: Callable[[BrowserPool], Any],
        *,
        timeout: float,
        label: str = "job",
        reject_if_busy: bool = False,
    ) -> Any:
        self.start()
        with self._lock:
            if reject_if_busy and self._inflight > 0:
                raise ServiceBusy(f"已有浏览器作业在跑（{self._inflight} 个），请稍后重试")
            self._inflight += 1
        job = _Job(fn=fn, done=threading.Event(), label=label)
        try:
            self._queue.put(job)
            if not job.done.wait(timeout):
                raise TimeoutError(f"{label} 等待超过 {timeout:.0f}s（作业仍在后台跑）")
            if job.error is not None:
                raise job.error
            return job.result
        finally:
            with self._lock:
                self._inflight -= 1

    def shutdown(self) -> None:
        thread = self._thread
        if thread is None:
            self.pool.close()
            return
        self._queue.put(None)
        thread.join(timeout=30)
        self._thread = None


# ------------------------------------------------------------------ FastAPI


class SmsCodeBody(BaseModel):
    code: str = Field(min_length=1, max_length=16)


class LoginStartBody(BaseModel):
    identity_hint: str = ""


class PublishBody(BaseModel):
    title: str
    description: str = ""
    video_path: str
    hashtags: list[str] = Field(default_factory=list)
    cover_path: str = ""
    schedule_at: str = ""
    identity_hint: str = ""


def _error_envelope(state: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "state": state, "detail": detail, **extra}


def create_app(
    *,
    profile_root: str | os.PathLike[str] | None = None,
    screenshot_root: str | os.PathLike[str] | None = None,
    channel: str | None = None,
    worker: BrowserWorker | None = None,
    automation_factory: Callable[[str], DouyinAutomation] | None = None,
) -> FastAPI:
    """构造服务 app。参数都可注入，便于测试时替换掉真实浏览器。"""
    pool = BrowserPool(
        profile_root=profile_root or os.environ.get("DOUYIN_PROFILE_ROOT") or DEFAULT_PROFILE_ROOT,
        channel=channel or os.environ.get("DOUYIN_BROWSER_CHANNEL") or "chrome",
    )
    runner = worker or BrowserWorker(pool)
    shots = Shooter(
        screenshot_root or os.environ.get("DOUYIN_SCREENSHOT_DIR") or DEFAULT_SCREENSHOT_ROOT
    )
    selectors = load_selectors()

    def make_automation(account_id: str) -> DouyinAutomation:
        if automation_factory is not None:
            return automation_factory(account_id)
        return DouyinAutomation(account_id=account_id, selectors=selectors, shooter=shots)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        # 进程退出时关掉所有浏览器窗口；worker 线程自己负责真正的 close
        runner.shutdown()

    app = FastAPI(title="douyin host uploader", version=SERVICE_VERSION, lifespan=lifespan)

    def run(
        account_id: str,
        step: str,
        fn: Callable[[DouyinAutomation, Any], dict[str, Any]],
        *,
        timeout: float,
        reject_if_busy: bool,
        need_existing_page: bool = False,
    ) -> dict[str, Any]:
        """把一个页面动作丢给 worker 线程，并统一兜底成 envelope。"""
        automation = make_automation(account_id)

        def job(browser: BrowserPool) -> dict[str, Any]:
            if need_existing_page:
                page = browser.existing_page(account_id)
                if page is None:
                    return _error_envelope(
                        STATE_NO_BROWSER,
                        f"账号 {account_id} 现在没有打开的浏览器窗口，"
                        "请先 POST /accounts/{id}/login/start",
                    )
            else:
                page = browser.page(account_id)
            return fn(automation, page)

        try:
            return runner.submit(job, timeout=timeout, label=step, reject_if_busy=reject_if_busy)
        except ServiceBusy as exc:
            return _error_envelope(STATE_BUSY, str(exc))
        except TimeoutError as exc:
            return _error_envelope(STATE_TIMEOUT, str(exc))
        except BrowserUnavailable as exc:
            return _error_envelope(STATE_BROWSER_ERROR, str(exc))
        except Exception as exc:  # 服务不许因为页面异常挂掉
            logger.exception("%s 执行失败（account=%s）", step, account_id)
            return _error_envelope(STATE_BROWSER_ERROR, f"{type(exc).__name__}: {exc}")

    @app.get("/health")
    def health() -> dict[str, Any]:
        """进程存活 + 浏览器概况。**不碰页面**，所以随便调。"""
        return {
            "ok": True,
            "state": STATE_OK,
            "service": SERVICE_NAME,
            "version": SERVICE_VERSION,
            "channel": pool.channel,
            "headless": False,
            "profile_root": str(pool.profile_root),
            "screenshot_root": str(shots.root),
            "launched_accounts": pool.launched(),
            "busy": runner.busy,
            "time": datetime.now(UTC).isoformat(),
        }

    @app.get("/accounts/{account_id}/login/status")
    def login_status(account_id: str) -> dict[str, Any]:
        """打开创作者中心判断登录态（会真的开浏览器，别高频调）。"""

        def action(automation: DouyinAutomation, page: Any) -> dict[str, Any]:
            page.goto(UPLOAD_URL, timeout=int(NAV_TIMEOUT * 1000), wait_until="domcontentloaded")
            automation.pace()
            return automation.login_state(page)

        return run(
            account_id,
            "login/status",
            action,
            timeout=NAV_TIMEOUT * 2,
            reject_if_busy=True,
        )

    @app.post("/accounts/{account_id}/login/start")
    def login_start(account_id: str, body: LoginStartBody | None = None) -> dict[str, Any]:
        """在宿主机弹出登录窗口，让**真人自己**扫码 / 输码。

        红线：本端点只负责把窗口打开并把页面停在登录页，不做任何自动登录。
        """

        def action(automation: DouyinAutomation, page: Any) -> dict[str, Any]:
            page.goto(UPLOAD_URL, timeout=int(NAV_TIMEOUT * 1000), wait_until="domcontentloaded")
            automation.pace()
            state = automation.login_state(page)
            if state.get("state") == STATE_LOGGED_IN:
                return state
            return {
                "ok": True,
                "state": STATE_WAITING_USER,
                "detail": (
                    "已在宿主机打开抖音创作者中心窗口。请用**该账号本人**的抖音 App 扫码；"
                    "若要求短信验证码，请在 core 的 /accounts/{id}/login 页面提交，"
                    "系统会转发到这里自动填入（不做任何识别）。"
                ),
            }

        return run(
            account_id,
            "login/start",
            action,
            timeout=NAV_TIMEOUT * 2,
            reject_if_busy=True,
        )

    @app.post("/accounts/{account_id}/sms_code")
    def sms_code(account_id: str, body: SmsCodeBody) -> dict[str, Any]:
        """把真人收到的短信验证码填进当前页面的输入框。

        红线：**只填写，不识别**。验证码不落盘、不进日志（见 client 侧 ``log_body=False``）。
        """

        def action(automation: DouyinAutomation, page: Any) -> dict[str, Any]:
            return automation.fill_sms_code(page, body.code)

        return run(
            account_id,
            "sms_code",
            action,
            timeout=60.0,
            reject_if_busy=False,
            need_existing_page=True,
        )

    @app.post("/accounts/{account_id}/publish")
    def publish(account_id: str, body: PublishBody) -> dict[str, Any]:
        """完整发布流程。**串行**：已有作业在跑时直接回 busy，由 core 重试。"""
        video = Path(body.video_path)
        if not video.is_file():
            return _error_envelope(
                STATE_INVALID_CONTENT,
                f"宿主机上找不到成片 {body.video_path}"
                "（core 在容器里时要配 DOUYIN_MEDIA_LOCAL_DIR / DOUYIN_MEDIA_HOST_DIR）",
            )
        if body.cover_path and not Path(body.cover_path).is_file():
            return _error_envelope(STATE_INVALID_CONTENT, f"宿主机上找不到封面 {body.cover_path}")
        req = PublishRequest(
            title=body.title,
            description=body.description,
            video_path=str(video),
            hashtags=list(body.hashtags),
            cover_path=body.cover_path,
            schedule_at=body.schedule_at,
            identity_hint=body.identity_hint,
        )

        def action(automation: DouyinAutomation, page: Any) -> dict[str, Any]:
            return automation.publish(page, req)

        return run(
            account_id,
            "publish",
            action,
            timeout=UPLOAD_TIMEOUT + AFTER_PUBLISH_TIMEOUT + NAV_TIMEOUT,
            reject_if_busy=True,
        )

    @app.get("/accounts/{account_id}/recent_posts")
    def recent_posts(account_id: str, limit: int = 20) -> dict[str, Any]:
        """内容管理页最近 N 条，供 core 侧 ``reconcile`` 用（**只读**）。"""

        def action(automation: DouyinAutomation, page: Any) -> dict[str, Any]:
            posts = automation.read_recent_posts(page, limit=max(1, min(limit, 50)))
            return {"ok": True, "state": STATE_OK, "posts": posts}

        return run(
            account_id,
            "recent_posts",
            action,
            timeout=NAV_TIMEOUT * 2,
            reject_if_busy=True,
        )

    @app.get("/accounts/{account_id}/metrics/{post_id}")
    def metrics(account_id: str, post_id: str) -> dict[str, Any]:
        """作品数据页的公开指标（尽力而为，读不到就 ``available=false``）。"""

        def action(automation: DouyinAutomation, page: Any) -> dict[str, Any]:
            data = automation.read_metrics(page, post_id)
            return {"ok": True, "state": STATE_OK, "metrics": data}

        return run(
            account_id,
            "metrics",
            action,
            timeout=NAV_TIMEOUT * 2,
            reject_if_busy=True,
        )

    app.state.pool = pool
    app.state.worker = runner
    return app


# ------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    """``python -m publishers.douyin serve`` 的入口。"""
    parser = argparse.ArgumentParser(
        prog="python -m publishers.douyin",
        description="抖音宿主机上传器（有头 Patchright，一号一 profile，不入 Docker）",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="启动常驻 HTTP 服务")
    serve.add_argument("--host", default="127.0.0.1", help="默认只监听本机回环")
    serve.add_argument("--port", type=int, default=8710)
    serve.add_argument("--profile-root", default=None, help=f"默认 {DEFAULT_PROFILE_ROOT}")
    serve.add_argument("--screenshot-root", default=None, help=f"默认 {DEFAULT_SCREENSHOT_ROOT}")
    serve.add_argument(
        "--channel", default=None, help="浏览器 channel，默认 chrome，失败回退 chromium"
    )
    serve.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    import uvicorn

    app = create_app(
        profile_root=args.profile_root,
        screenshot_root=args.screenshot_root,
        channel=args.channel,
    )
    logger.info(
        "抖音上传器启动：http://%s:%s（有头浏览器，profile 一号一目录）", args.host, args.port
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


__all__ = [
    "DEFAULT_PROFILE_ROOT",
    "DEFAULT_SCREENSHOT_ROOT",
    "SELECTORS",
    "BrowserPool",
    "BrowserUnavailable",
    "BrowserWorker",
    "DouyinAutomation",
    "Pacer",
    "PublishRequest",
    "ServiceBusy",
    "Shooter",
    "create_app",
    "first_visible",
    "load_selectors",
    "main",
    "parse_card_time",
    "wait_visible",
]
