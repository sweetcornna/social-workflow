"""Telegram 通道：通知 + 发布前人工确认闸门的交互面（P12）。

为什么是 long polling 而不是 webhook
------------------------------------
这台服务器的 443 是一个 **nginx stream ``ssl_preread`` SNI 分流器**，同时承载 6 个站
和 VLESS / Hysteria 中继。给一个回调去动那份分流配置，风险与收益完全不成比例：改错
一行，6 个站和代理一起掉。long polling 是**纯出站**的——不需要任何入站暴露、不需要
证书、不需要公网可达，服务器实测能直连 ``api.telegram.org``（``getMe`` 0.5s 返回，
不需要代理）。代价只是一个常驻线程和 30 秒一次的空转请求，这个价我们付得起。

安全模型（这是硬红线）
----------------------
任何人拿到 bot 用户名都能找到它并给它发消息。所以按钮回调必须过三道：

1. **来源**：``callback_query.message.chat.id`` 必须等于配置里的 ``TELEGRAM_CHAT_ID``；
   点按钮的 ``from.id`` 必须在白名单里（私聊时白名单缺省就是 chat 本人）。
   不符的一律**忽略并记 warning**，连 ``answerCallbackQuery`` 都不回——不给探测者
   任何"这个 bot 活着且认识我"的信号。
2. **完整性**：``callback_data`` 里带 HMAC 短签名（见 :func:`build_callback_data`）。
   没有签名密钥就**根本不发带按钮的卡片**——没有防伪造能力时宁可不给这个按钮。
3. **重放**：一条内容只认第一次有效点击。真相在 DB 的状态与
   ``ContentItem.confirmed_at`` 上（见 ``core/confirm.py``），不靠内存去重——
   进程重启后内存里的"点过了"就没了，而合规上"这条到底谁点的"必须可追溯。

凭据红线
--------
``TELEGRAM_BOT_TOKEN`` 只走 ``.env``：不入库、不进前端、不写日志明文。任何要把 token
放进日志 / 异常文案的地方都必须过 :func:`mask_token`。
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("social_workflow.telegram")

#: callback_data 的版本前缀。Telegram 限制 callback_data ≤ 64 字节，所以每一段都要短
CALLBACK_VERSION = "v1"
#: 动作码。故意用两字母：``item_id`` 已经占了 16 字节，签名占 10，预算很紧
ACTION_CONFIRM = "ok"
ACTION_REJECT = "no"
ACTIONS: frozenset[str] = frozenset({ACTION_CONFIRM, ACTION_REJECT})
#: 签名取 HMAC-SHA256 十六进制的前这么多位。10 位 = 40 bit，
#: 配合"只认一个 chat + 一次性生效"足够；再长就撞 64 字节上限了
SIGNATURE_LENGTH = 10

#: getUpdates 要哪些更新。``message`` 收进来只为一件事：把发消息那个人的 chat_id
#: 打进日志，省掉"服务已经在跑、又想拿 chat_id"时的来回折腾（见 OPS.md）
ALLOWED_UPDATES: tuple[str, ...] = ("callback_query", "message")

#: 轮询出错后的退避区间（秒）
POLL_BACKOFF_MIN = 2.0
POLL_BACKOFF_MAX = 120.0

LEVEL_PREFIX: dict[str, str] = {"error": "🔴", "warning": "🟠", "info": "🔵"}


class TelegramError(RuntimeError):
    """调用 Bot API 失败（网络异常或 ``ok=false``）。文案里绝不带 token。"""


#: 本人点了一张签名对不上的卡时的回执。刻意不带 item id、不解释签名细节——
#: 它出现在手机上，作用是"把圈停掉 + 告诉人下一步去哪"，不是排错输出。
STALE_CARD_ANSWER = "这张确认卡已失效，请到工作台确认"


class CallbackRejected(Exception):
    """回调没通过来源 / 签名校验。``reason`` 用于日志与统计。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


# --------------------------------------------------------------------- 配置


def mask_token(token: str) -> str:
    """``123456:ABCdefGhi...`` → ``123456:***fGhi``。日志里只留够认人的部分。"""
    if not token:
        return "(未配置)"
    head, _, tail = token.partition(":")
    if not tail:
        return f"{token[:3]}***"
    return f"{head}:***{tail[-4:]}" if len(tail) > 4 else f"{head}:***"


@dataclass(frozen=True)
class TelegramConfig:
    """Telegram 通道的运行期配置（全部来自环境变量）。"""

    bot_token: str = ""
    chat_id: str = ""
    allowed_user_ids: frozenset[int] = frozenset()
    api_base: str = "https://api.telegram.org"
    timeout: float = 15.0
    poll_timeout: int = 30
    signing_secret: str = ""
    enabled: bool = True
    state_file: str = "data/telegram_state.json"

    @property
    def configured(self) -> bool:
        """有 token = 通道存在（能 ``getMe``、能被 setup 工具用）。"""
        return bool(self.bot_token)

    @property
    def ready(self) -> bool:
        """能不能真的发出去。缺 chat_id 时为假——这时必须**明确拒绝**并提示人。"""
        return bool(self.bot_token and self.chat_id)

    @property
    def can_sign(self) -> bool:
        return bool(self.signing_secret)

    def why_not_ready(self) -> str:
        """给人看的一句话。静默不发是最糟的失败方式，任何拒绝都要能解释。"""
        if not self.enabled:
            return "SW_TELEGRAM_ENABLED=false，Telegram 通道整体关闭"
        if not self.bot_token:
            return "缺 TELEGRAM_BOT_TOKEN（只写 .env，不入库）"
        if not self.chat_id:
            return (
                "缺 TELEGRAM_CHAT_ID：先给 bot 发一条 /start，"
                "再跑 `uv run python -m core.telegram setup` 把打印出来的 id 写进 .env"
            )
        return ""

    def method_url(self, method: str) -> str:
        return f"{self.api_base.rstrip('/')}/bot{self.bot_token}/{method}"


def load_config(settings: Any | None = None) -> TelegramConfig:
    """从 ``Settings`` 读配置。签名密钥按 专用 → SW_UI_TOKEN → bot token 回落。

    回落到 bot token 是可接受的：它本来就是这套系统里最高等级的秘密，
    而且从不出现在 callback_data 里（只作为 HMAC 的 key）。
    """
    if settings is None:
        from core.config import get_settings

        settings = get_settings()
    secret = (
        (settings.sw_telegram_signing_secret or "").strip()
        or (settings.sw_ui_token or "").strip()
        or (settings.telegram_bot_token or "").strip()
    )
    return TelegramConfig(
        bot_token=(settings.telegram_bot_token or "").strip(),
        chat_id=str(settings.telegram_chat_id or "").strip(),
        allowed_user_ids=settings.telegram_allowed_user_ids(),
        api_base=settings.sw_telegram_api_base,
        timeout=settings.sw_telegram_timeout_seconds,
        poll_timeout=max(int(settings.sw_telegram_poll_timeout_seconds), 0),
        signing_secret=secret,
        enabled=bool(settings.sw_telegram_enabled),
        state_file=settings.sw_telegram_state_file,
    )


# ------------------------------------------------------------------ 签名


def sign_callback(secret: str, action: str, item_id: str) -> str:
    """``HMAC-SHA256(secret, "action:item_id")`` 的前 :data:`SIGNATURE_LENGTH` 位十六进制。"""
    if not secret:
        raise ValueError("缺少签名密钥，不能生成 callback_data")
    digest = hmac.new(
        secret.encode("utf-8"), f"{action}:{item_id}".encode(), hashlib.sha256
    ).hexdigest()
    return digest[:SIGNATURE_LENGTH]


def build_callback_data(secret: str, action: str, item_id: str) -> str:
    """``v1:ok:itm_xxxxxxxxxxxx:0123456789``（34 字节，远在 64 字节上限内）。"""
    if action not in ACTIONS:
        raise ValueError(f"未知回调动作 {action!r}，允许 {sorted(ACTIONS)}")
    data = f"{CALLBACK_VERSION}:{action}:{item_id}:{sign_callback(secret, action, item_id)}"
    if len(data.encode("utf-8")) > 64:
        raise ValueError(f"callback_data 超过 Telegram 的 64 字节上限：{len(data)}")
    return data


def parse_callback_data(secret: str, data: str) -> tuple[str, str]:
    """校验并解析 ``callback_data``，返回 ``(action, item_id)``。

    不合法一律抛 :class:`CallbackRejected` —— 伪造者只会看到"这条被忽略了"。
    """
    parts = (data or "").split(":")
    if len(parts) != 4:
        raise CallbackRejected("malformed", data or "(空)")
    version, action, item_id, signature = parts
    if version != CALLBACK_VERSION:
        raise CallbackRejected("bad_version", version)
    if action not in ACTIONS:
        raise CallbackRejected("bad_action", action)
    if not item_id:
        raise CallbackRejected("bad_item", item_id)
    if not secret:
        raise CallbackRejected("no_secret", "未配置签名密钥，无法校验回调")
    # compare_digest：签名比对必须是常数时间，否则可以按字节爆破
    if not hmac.compare_digest(signature, sign_callback(secret, action, item_id)):
        raise CallbackRejected("bad_signature", item_id)
    return action, item_id


# ------------------------------------------------------------------ HTTP 客户端


class TelegramClient:
    """Bot API 的薄封装。只做"发请求 + 解 ``ok`` 信封"，不含任何业务判断。"""

    def __init__(self, config: TelegramConfig, *, client: httpx.Client | None = None) -> None:
        self.config = config
        self._client = client
        self._owns_client = client is None

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.config.timeout)
        return self._client

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    def call(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
        files: dict[str, Any] | None = None,
    ) -> Any:
        """调一个 Bot API 方法，返回 ``result``。失败抛 :class:`TelegramError`。"""
        if not self.config.bot_token:
            raise TelegramError("缺 TELEGRAM_BOT_TOKEN")
        url = self.config.method_url(method)
        body = {k: v for k, v in (payload or {}).items() if v is not None}
        try:
            if files:
                # 多部分表单：值里的 dict/list 要先序列化成 JSON 串
                data = {
                    k: (json.dumps(v, ensure_ascii=False) if isinstance(v, dict | list) else str(v))
                    for k, v in body.items()
                }
                resp = self._http().post(
                    url, data=data, files=files, timeout=timeout or self.config.timeout
                )
            else:
                resp = self._http().post(url, json=body, timeout=timeout or self.config.timeout)
        except httpx.HTTPError as exc:
            # 异常文案里带 URL 就等于泄露 token，只报方法名
            raise TelegramError(f"{method} 请求失败: {type(exc).__name__}: {exc}") from exc
        try:
            envelope = resp.json()
        except ValueError as exc:
            raise TelegramError(f"{method} 返回的不是 JSON（HTTP {resp.status_code}）") from exc
        if not envelope.get("ok"):
            raise TelegramError(
                f"{method} 失败: error_code={envelope.get('error_code')} "
                f"{envelope.get('description') or ''}".strip()
            )
        return envelope.get("result")

    # -- 常用方法 -------------------------------------------------------

    def get_me(self) -> dict[str, Any]:
        return dict(self.call("getMe") or {})

    def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        disable_preview: bool = True,
    ) -> dict[str, Any]:
        return dict(
            self.call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "link_preview_options": {"is_disabled": disable_preview},
                    "reply_markup": reply_markup,
                },
            )
            or {}
        )

    def send_photo(
        self,
        chat_id: str,
        photo_path: Path,
        caption: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with photo_path.open("rb") as handle:
            return dict(
                self.call(
                    "sendPhoto",
                    {
                        "chat_id": chat_id,
                        "caption": caption,
                        "parse_mode": "HTML",
                        "reply_markup": reply_markup,
                    },
                    files={"photo": (photo_path.name, handle)},
                )
                or {}
            )

    def edit_message_text(self, chat_id: str, message_id: int, text: str) -> dict[str, Any]:
        return dict(
            self.call(
                "editMessageText",
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": text,
                    "parse_mode": "HTML",
                    # 传空的 inline_keyboard = 把按钮撤掉，点过的卡片不该还能再点
                    "reply_markup": {"inline_keyboard": []},
                },
            )
            or {}
        )

    def edit_message_caption(self, chat_id: str, message_id: int, caption: str) -> dict[str, Any]:
        return dict(
            self.call(
                "editMessageCaption",
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "caption": caption,
                    "parse_mode": "HTML",
                    "reply_markup": {"inline_keyboard": []},
                },
            )
            or {}
        )

    def answer_callback_query(
        self, callback_query_id: str, text: str = "", *, show_alert: bool = False
    ) -> None:
        self.call(
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id, "text": text, "show_alert": show_alert},
        )

    def get_updates(
        self,
        *,
        offset: int | None = None,
        timeout: int = 30,
        allowed: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        result = self.call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": list(allowed if allowed is not None else ALLOWED_UPDATES),
            },
            # HTTP 超时必须比 long polling 的 timeout 长，否则每次都在自己这边超时
            timeout=self.config.timeout + timeout,
        )
        return list(result or [])


# ------------------------------------------------------------------ 卡片渲染


def _esc(text: str) -> str:
    return html.escape(str(text or ""), quote=False)


#: 中文没有词间空格，硬按字数切会切在半句话中间。在上限往回找这些收束点，
#: 让摘要停在一个读得完的地方
BREAK_CHARS = "。！？；，、,.;!?　 "

#: 正文摘要长度。手机通知栏里能一眼读完的量；再长人就只会划过去
SUMMARY_LIMIT = 80


def summarize(text: str, limit: int = SUMMARY_LIMIT) -> str:
    """正文摘要：压掉空白 → 到上限 → 往回退到最近的收束点 → 补省略号。"""
    body = " ".join(str(text or "").split())
    if len(body) <= limit:
        return body
    head = body[:limit]
    # 只在最后 1/4 里找收束点：退太多会把摘要砍成一句没信息量的话
    cut = max(head.rfind(ch) for ch in BREAK_CHARS)
    if cut >= limit * 3 // 4:
        head = head[: cut + 1] if head[cut] not in "　 " else head[:cut]
    return head.rstrip("　 ,，、") + "…"


def review_verdict(blocking: int, warnings: int) -> str:
    """机审结论的**一句人话**。

    刻意不倒机器字段（``block=0 warn=0`` 那种）：这张卡是给人在手机上一眼决定用的，
    读它的人关心的是"要不要多看一眼"，不是我们内部的计数口径。
    """
    if blocking:
        return f"机审有 {blocking} 处阻断 · 已由人工放行，建议看全文再决定"
    if warnings:
        return f"机审有 {warnings} 处提醒 · 建议看全文再决定"
    return "机审通过 · 无阻断"


@dataclass(frozen=True)
class ConfirmCard:
    """一张确认卡要展示的东西（纯数据，便于单测断言文案）。

    版式（配图是主体，文字是它的说明）::

        [封面图]
        <b>标题</b>
        19:00 发（Asia/Shanghai） · 甜玉米(acc_xhs_01)

        正文前 80 字…

        机审通过 · 无阻断
    """

    item_id: str
    title: str
    body: str
    slot_text: str
    account_label: str
    platform: str = ""
    review_url: str = ""
    blocking: int = 0
    warnings: int = 0
    #: 媒体张数，决定第三个按钮是「看全部 N 张」还是「看全文」
    media_count: int = 0
    #: 提醒版：槽位前补推的那一次，多一行"还有多久"
    reminder: bool = False
    #: 提醒版的剩余时间文案，如 ``28 分钟``
    countdown: str = ""

    def _head(self) -> list[str]:
        lines: list[str] = []
        if self.reminder:
            lines.append(
                f"还有 {_esc(self.countdown)}就到发布时刻，这条还没确认"
                if self.countdown
                else "快到发布时刻了，这条还没确认"
            )
            lines.append("")
        lines.append(f"<b>{_esc(self.title)}</b>")
        return lines

    def _meta(self) -> str:
        slot = self.slot_text or "未排期"
        return f"{_esc(slot)} · {_esc(self.account_label)}"

    def render(self) -> str:
        """待确认的样子。"""
        lines = [*self._head(), self._meta()]
        body = summarize(self.body)
        if body:
            lines += ["", _esc(body)]
        lines += ["", _esc(review_verdict(self.blocking, self.warnings))]
        return "\n".join(lines)

    def render_decided(self, status_line: str) -> str:
        """点完之后的样子：**标题不变**，只把状态行换掉，按钮撤掉。

        改原消息而不是再发一条：一条内容在对话里只占一格，划回去还能看到它现在
        是什么状态。刷屏是长期无人值守里最容易把人逼到静音的东西。
        """
        lines = [f"<b>{_esc(self.title)}</b>", self._meta()]
        body = summarize(self.body)
        if body:
            lines += ["", _esc(body)]
        lines += ["", _esc(status_line)]
        return "\n".join(lines)

    def media_button_text(self) -> str:
        return f"看全部 {self.media_count} 张" if self.media_count > 1 else "看全文"


def build_keyboard(
    secret: str, item_id: str, review_url: str = "", media_text: str = "看全文"
) -> dict[str, Any]:
    """确认卡的 inline keyboard。

    按钮文案说清"按下去会发生什么"，而且**同一个词根贯穿全流程**：卡片上写
    「确认发布」，点完状态行写「已确认」，工作台上那个兜底按钮也叫「确认发布」。
    换词等于让人每一处都重新判断一次这是不是同一件事。

    ``review_url`` 只有在是**绝对 http(s) 地址**时才放进去：Telegram 会拒收带相对
    路径的 url 按钮，整条消息都发不出去。没配 ``SW_PUBLIC_BASE_URL`` 时就少一个按钮，
    但确认与不发照常可用。
    """
    keyboard = [
        [
            {
                "text": "确认发布",
                "callback_data": build_callback_data(secret, ACTION_CONFIRM, item_id),
            },
            {"text": "不发", "callback_data": build_callback_data(secret, ACTION_REJECT, item_id)},
        ]
    ]
    if review_url.startswith(("http://", "https://")):
        keyboard.append([{"text": media_text, "url": review_url}])
    return {"inline_keyboard": keyboard}


def parse_ref(ref: str) -> tuple[str, int, str]:
    """``"<chat_id>:<message_id>:<kind>"`` → ``(chat_id, message_id, kind)``。"""
    parts = str(ref or "").split(":")
    if len(parts) < 2:
        raise ValueError(f"confirm_ref 格式非法: {ref!r}")
    kind = parts[2] if len(parts) > 2 else "text"
    return parts[0], int(parts[1]), kind


# ------------------------------------------------------------------ Notifier


class TelegramNotifier:
    """实现 :class:`core.notify.Notifier`，另带确认卡的发/改能力。

    协议约定：``send`` **必须吞掉自己的异常**，通知失败不能拖垮主流程。
    交互方法（``send_confirm_card`` / ``edit_card``）同样返回"成不成"而不抛。
    """

    def __init__(self, config: TelegramConfig, *, client: TelegramClient | None = None) -> None:
        self.config = config
        self.client = client or TelegramClient(config)
        #: 观测用：本进程发出去几条（系统页显示"今日推了几条"）
        self.sent_count = 0
        self.failed_count = 0

    # -- Notifier 协议 ---------------------------------------------------

    def send(self, title: str, text: str, level: str = "info") -> bool:
        if not self.config.ready:
            logger.warning("Telegram 未就绪，跳过通知：%s", self.config.why_not_ready())
            return False
        prefix = LEVEL_PREFIX.get(level, LEVEL_PREFIX["info"])
        body = f"{prefix} <b>{_esc(title)}</b>\n{_esc(text)}"
        try:
            self.client.send_message(self.config.chat_id, body)
        except TelegramError as exc:
            self.failed_count += 1
            logger.warning("Telegram 通知发送失败: %s", exc)
            return False
        except Exception as exc:  # pragma: no cover - 兜底，绝不外抛
            self.failed_count += 1
            logger.warning("Telegram 通知异常: %s: %s", type(exc).__name__, exc)
            return False
        self.sent_count += 1
        return True

    # -- 确认卡 ----------------------------------------------------------

    def send_confirm_card(self, card: ConfirmCard, *, cover: Path | None = None) -> str | None:
        """发一张带按钮的确认卡。成功返回 ``confirm_ref``，失败返回 ``None``。

        没有签名密钥时**拒发**：一张点了不算数的卡片比没有卡片更糟——人会以为
        自己确认过了。
        """
        if not self.config.ready:
            logger.warning("Telegram 未就绪，未推确认卡：%s", self.config.why_not_ready())
            return None
        if not self.config.can_sign:
            logger.error(
                "缺少 callback 签名密钥（SW_TELEGRAM_SIGNING_SECRET / SW_UI_TOKEN），"
                "拒绝发送带按钮的确认卡"
            )
            return None
        try:
            markup = build_keyboard(
                self.config.signing_secret,
                card.item_id,
                card.review_url,
                card.media_button_text(),
            )
            text = card.render()
            if cover is not None and cover.is_file():
                message = self.client.send_photo(
                    self.config.chat_id, cover, text, reply_markup=markup
                )
                kind = "photo"
            else:
                message = self.client.send_message(self.config.chat_id, text, reply_markup=markup)
                kind = "text"
        except TelegramError as exc:
            self.failed_count += 1
            logger.warning("推确认卡失败 item=%s: %s", card.item_id, exc)
            return None
        except Exception as exc:  # pragma: no cover - 兜底
            self.failed_count += 1
            logger.warning("推确认卡异常 item=%s: %s: %s", card.item_id, type(exc).__name__, exc)
            return None
        message_id = message.get("message_id")
        if message_id is None:
            return None
        self.sent_count += 1
        return f"{self.config.chat_id}:{int(message_id)}:{kind}"

    def edit_card(self, ref: str, text: str) -> bool:
        """把已发出的卡片改成"已确认 / 已驳回"并撤掉按钮，避免重复点。"""
        if not self.config.configured:
            return False
        try:
            chat_id, message_id, kind = parse_ref(ref)
        except ValueError as exc:
            logger.warning("改写确认卡失败: %s", exc)
            return False
        try:
            if kind == "photo":
                self.client.edit_message_caption(chat_id, message_id, text)
            else:
                self.client.edit_message_text(chat_id, message_id, text)
        except TelegramError as exc:
            # 改写失败不是业务失败：决定已经落库了，卡片只是显示层
            logger.warning("改写确认卡失败 ref=%s: %s", ref, exc)
            return False
        except Exception as exc:  # pragma: no cover - 兜底
            logger.warning("改写确认卡异常 ref=%s: %s", ref, type(exc).__name__)
            return False
        return True

    def answer(self, callback_query_id: str, text: str) -> None:
        """回一次 ``answerCallbackQuery``，把手机上那个转圈停掉。失败只记日志。"""
        try:
            self.client.answer_callback_query(callback_query_id, text)
        except Exception as exc:  # pragma: no cover - 兜底
            logger.debug("answerCallbackQuery 失败: %s", exc)


def build_telegram_notifier(settings: Any | None = None) -> TelegramNotifier | None:
    """按配置造一个通道实例，没配 / 关掉时返回 ``None``。"""
    config = load_config(settings)
    if not config.enabled or not config.configured:
        return None
    if not config.chat_id:
        # 有 token 没 chat_id：仍然建出来，让每次拒发都留下那句人话提示，
        # 而不是"配了 token 却什么都没发生"
        logger.warning("Telegram: %s", config.why_not_ready())
    return TelegramNotifier(config)


#: 进程内共享的通道实例。通知扇出与确认卡必须用**同一个**，
#: 否则"今日推了几条"会分裂成两份谁也不对
_channel: TelegramNotifier | None = None
_channel_built = False
_channel_lock = threading.Lock()


def get_telegram_channel(settings: Any | None = None) -> TelegramNotifier | None:
    """取共享通道（首次调用时按配置构造）。``None`` = 没配 / 关掉了。"""
    global _channel, _channel_built
    with _channel_lock:
        if not _channel_built:
            _channel = build_telegram_notifier(settings)
            _channel_built = True
        return _channel


def set_telegram_channel(channel: TelegramNotifier | None) -> None:
    """显式指定通道（测试注入 / 显式关闭）。``None`` = 明确没有通道。"""
    global _channel, _channel_built
    with _channel_lock:
        _channel = channel
        _channel_built = True


def reset_telegram_channel() -> None:
    """丢掉缓存，下次按当前配置重新构造（测试夹具 / 改配置后调用）。"""
    global _channel, _channel_built
    with _channel_lock:
        _channel = None
        _channel_built = False


# ------------------------------------------------------------------ 游标持久化


class OffsetStore:
    """``getUpdates`` 游标的落盘。进程重启后不重复消费已处理过的回调。

    用一个小 JSON 文件而不是新开一张表：它是**单进程**的运行期状态，
    和内容 / 审计链没有关系，塞进 DB 只会让迁移变复杂。
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def load(self) -> int | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        value = data.get("offset")
        return int(value) if isinstance(value, int | float | str) and str(value).isdigit() else None

    def save(self, offset: int) -> None:
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps({"offset": int(offset)}), encoding="utf-8")
                tmp.replace(self.path)
            except OSError as exc:  # 落盘失败只意味着重启会重放一次，不该拖垮轮询
                logger.warning("保存 Telegram 游标失败: %s", exc)


# ------------------------------------------------------------------ long polling

#: 处理一次回调的业务钩子：``(action, item_id, actor) -> (提示语, 卡片新文案)``。
#: 由 ``core/confirm.py`` 注入，telegram.py 只管"谁点的、真不真"
DecisionHandler = Callable[[str, str, str], tuple[str, str]]


class TelegramPoller:
    """后台 long polling 线程：拉 ``getUpdates`` → 校验 → 应用决定 → 改卡片。

    随 FastAPI lifespan 起停；``SW_TELEGRAM_ENABLED=false`` 时压根不构造它。
    """

    def __init__(
        self,
        config: TelegramConfig,
        *,
        handler: DecisionHandler,
        notifier: TelegramNotifier | None = None,
        client: TelegramClient | None = None,
        store: OffsetStore | None = None,
    ) -> None:
        self.config = config
        self.handler = handler
        self.client = client or TelegramClient(config)
        self.notifier = notifier or TelegramNotifier(config, client=self.client)
        self.store = store or OffsetStore(config.state_file)
        self.offset = self.store.load()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: 观测统计（系统页 / 测试断言用）
        self.stats: dict[str, int] = {
            "polls": 0,
            "updates": 0,
            "handled": 0,
            "rejected": 0,
            "errors": 0,
        }
        self.last_error: str = ""
        self.started_at: float | None = None
        #: 启动时握一次手记下的 bot 用户名。系统页要显示"这是哪个 bot"，
        #: 但那一页不该为了拿个名字去发网络请求，所以在这里记着
        self.bot_username: str = ""

    # -- 线程生命周期 ----------------------------------------------------

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self.started_at = time.time()
        try:
            self.bot_username = str(self.client.get_me().get("username") or "")
        except TelegramError as exc:
            # 握手失败不挡启动：轮询循环自带退避重连，网络恢复后自然会好
            self.last_error = str(exc)
            logger.warning("Telegram getMe 失败（仍会起轮询并退避重连）: %s", exc)
        self._thread = threading.Thread(target=self._loop, name="telegram-poller", daemon=True)
        self._thread.start()
        logger.info(
            "Telegram long polling 已启动 bot=%s chat=%s offset=%s",
            mask_token(self.config.bot_token),
            self.config.chat_id or "(未配置)",
            self.offset,
        )

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            # 线程多半正卡在 30 秒 long polling 上，join 不必等它自然返回：
            # daemon 线程 + 无状态循环，进程退出时丢掉它是安全的
            thread.join(timeout=timeout)
        self.client.close()
        logger.info("Telegram long polling 已停止 统计=%s", self.stats)

    def _loop(self) -> None:
        backoff = POLL_BACKOFF_MIN
        while not self._stop.is_set():
            try:
                self.poll_once()
            except TelegramError as exc:
                self.stats["errors"] += 1
                self.last_error = str(exc)
                logger.warning("Telegram 轮询失败（%.0fs 后重试）: %s", backoff, exc)
                # 退避等待也要能被 stop() 立刻打断
                if self._stop.wait(backoff):
                    break
                backoff = min(backoff * 2, POLL_BACKOFF_MAX)
                continue
            except Exception as exc:  # pragma: no cover - 线程里任何异常都不许逃逸
                self.stats["errors"] += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("Telegram 轮询线程异常")
                if self._stop.wait(backoff):
                    break
                backoff = min(backoff * 2, POLL_BACKOFF_MAX)
                continue
            backoff = POLL_BACKOFF_MIN

    # -- 一轮 ------------------------------------------------------------

    def poll_once(self) -> int:
        """拉一轮并处理。返回本轮处理的更新条数（测试直接调它，不起线程）。"""
        updates = self.client.get_updates(offset=self.offset, timeout=self.config.poll_timeout)
        self.stats["polls"] += 1
        handled = 0
        for update in updates:
            self.stats["updates"] += 1
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                # 先推进游标再处理：一条处理不了的更新不能把整个轮询卡死在原地
                self.offset = update_id + 1
                self.store.save(self.offset)
            if "callback_query" in update:
                self.handle_callback(update["callback_query"])
                handled += 1
            elif "message" in update:
                self._log_message(update["message"])
        return handled

    def _log_message(self, message: dict[str, Any]) -> None:
        """普通消息只做一件事：把 chat_id 打进日志，方便配置时对号入座。

        **不回显消息正文**——这个 bot 谁都能发消息，正文进日志等于给了外人一条
        往运维日志里写东西的路。
        """
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        known = str(chat_id) == self.config.chat_id
        logger.info(
            "Telegram 收到消息 chat_id=%s type=%s %s",
            chat_id,
            chat.get("type"),
            "（已配置）" if known else "（未配置：要用它就把这个 id 写进 TELEGRAM_CHAT_ID）",
        )

    # -- 回调处理 --------------------------------------------------------

    def authorize(self, callback: dict[str, Any]) -> str:
        """三道来源校验，通过则返回操作者标识（``tg:<user_id>``）。

        不通过抛 :class:`CallbackRejected`。**不回 answerCallbackQuery**：
        不给探测者任何"这个 bot 认识我"的反馈。
        """
        message = callback.get("message") or {}
        chat_id = str(((message.get("chat") or {}).get("id")) or "")
        if not self.config.chat_id or chat_id != self.config.chat_id:
            raise CallbackRejected("bad_chat", chat_id or "(空)")
        sender = callback.get("from") or {}
        user_id = sender.get("id")
        if not isinstance(user_id, int):
            raise CallbackRejected("bad_user", str(user_id))
        allowed = self.config.allowed_user_ids
        if allowed:
            if user_id not in allowed:
                raise CallbackRejected("user_not_allowed", str(user_id))
        elif str(user_id) != self.config.chat_id:
            # 没配白名单时只认"私聊里的本人"（私聊 chat_id 就是 user id）。
            # 群里用必须显式配 SW_TELEGRAM_ALLOWED_USER_IDS —— 失败要往关的方向失败
            raise CallbackRejected("user_not_allowed", str(user_id))
        return f"tg:{user_id}"

    def _log_rejected(self, exc: CallbackRejected) -> None:
        self.stats["rejected"] += 1
        logger.warning(
            "拒绝 Telegram 回调 reason=%s detail=%s（任何人都能找到这个 bot，这是预期内的）",
            exc.reason,
            exc.detail,
        )

    def handle_callback(self, callback: dict[str, Any]) -> None:
        """校验 → 应用决定 → 改卡片 + 回执。任何异常都不外抛（线程要活着）。"""
        # 两段刻意分开，因为**该不该开口是两回事**：
        #
        # - ``authorize`` 没过 = 来源就不认识。一律沉默，连回执都不给——回执等于
        #   告诉探测者"这个 bot 认识你说的这个 chat"。见
        #   ``test_callback_from_a_stranger_chat_is_ignored_without_any_reply``。
        # - ``authorize`` 过了、只是签名对不上 = **本人**点了一张失效的卡（换过
        #   签名密钥、换过部署、卡片过期）。这时回执一句话不泄露任何东西，而且是
        #   唯一能把手机上那个转圈停掉的办法：Telegram 客户端在收到
        #   ``answerCallbackQuery`` 之前会一直转，没有超时提示。2026-08-24 真踩过。
        #
        # 两段都**不改变裁决**：签名不对照旧一条决定都不应用，卡片正文也不动。
        try:
            actor = self.authorize(callback)
        except CallbackRejected as exc:
            self._log_rejected(exc)
            return

        try:
            action, item_id = parse_callback_data(
                self.config.signing_secret, str(callback.get("data") or "")
            )
        except CallbackRejected as exc:
            self._log_rejected(exc)
            query_id = str(callback.get("id") or "")
            if query_id:
                # 不带 item id：回执是给人看的，不该把内部标识说出去
                self.notifier.answer(query_id, STALE_CARD_ANSWER)
            return

        try:
            answer_text, card_text = self.handler(action, item_id, actor)
        except Exception as exc:  # pragma: no cover - 业务异常兜底
            self.stats["errors"] += 1
            logger.exception("处理 Telegram 回调失败 item=%s", item_id)
            answer_text, card_text = f"处理失败：{type(exc).__name__}", ""

        self.stats["handled"] += 1
        query_id = str(callback.get("id") or "")
        if query_id:
            self.notifier.answer(query_id, answer_text)
        if card_text:
            message = callback.get("message") or {}
            message_id = message.get("message_id")
            kind = "photo" if message.get("photo") else "text"
            if isinstance(message_id, int):
                self.notifier.edit_card(f"{self.config.chat_id}:{message_id}:{kind}", card_text)


# ------------------------------------------------------------------ 进程内单例

_poller: TelegramPoller | None = None
_poller_lock = threading.Lock()


def get_poller() -> TelegramPoller | None:
    return _poller


def start_poller(settings: Any | None = None) -> TelegramPoller | None:
    """随 FastAPI lifespan 启动 long polling。没配 / 关掉时返回 ``None``。"""
    global _poller
    config = load_config(settings)
    if not config.enabled:
        logger.info("SW_TELEGRAM_ENABLED=false，不启动 Telegram long polling")
        return None
    if not config.ready:
        logger.warning("不启动 Telegram long polling：%s", config.why_not_ready())
        return None
    with _poller_lock:
        if _poller is not None:
            return _poller
        from core.confirm import handle_telegram_decision

        poller = TelegramPoller(config, handler=handle_telegram_decision)
        poller.start()
        _poller = poller
        return poller


def stop_poller() -> None:
    global _poller
    with _poller_lock:
        poller, _poller = _poller, None
    if poller is not None:
        poller.stop()


def channel_status(settings: Any | None = None) -> dict[str, Any]:
    """系统页要的通道状态。**不含 token**（脱敏后的指纹也不给前端）。"""
    config = load_config(settings)
    poller = get_poller()
    return {
        "enabled": config.enabled,
        "configured": config.configured,
        "ready": config.ready,
        "chat_configured": bool(config.chat_id),
        "can_sign": config.can_sign,
        "detail": config.why_not_ready(),
        "polling": bool(poller and poller.alive),
        "username": poller.bot_username if poller else "",
        "stats": dict(poller.stats) if poller else {},
        "last_error": poller.last_error if poller else "",
    }


# ---------------------------------------------------------------------- CLI


def _cmd_setup(args: Any) -> int:
    """`python -m core.telegram setup`：把给 bot 发过消息的 chat_id 打出来。

    刻意**不确认游标**（不传 offset）：确认掉的更新长驻服务再也拿不到，
    这个小工具不该有那种副作用。
    """
    config = load_config()
    if not config.configured:
        print("缺 TELEGRAM_BOT_TOKEN：先在 .env 里配好（600 权限，不要提交进仓库）")
        return 2
    client = TelegramClient(config)
    try:
        me = client.get_me()
        print(
            f"bot: @{me.get('username')} (id={me.get('id')}) token={mask_token(config.bot_token)}"
        )
        print(
            f"请现在用你的 Telegram 给 @{me.get('username')} 发一条 /start，最多等 {args.wait} 秒…"
        )
        deadline = time.time() + args.wait
        seen: dict[str, str] = {}
        while time.time() < deadline:
            updates = client.get_updates(timeout=min(args.wait, 10), allowed=("message",))
            for update in updates:
                message = update.get("message") or update.get("edited_message") or {}
                chat = message.get("chat") or {}
                chat_id = chat.get("id")
                if chat_id is None:
                    continue
                sender = message.get("from") or {}
                label = chat.get("username") or chat.get("title") or sender.get("username") or "?"
                seen[str(chat_id)] = f"{label}（{chat.get('type')}）"
            if seen:
                break
        if not seen:
            print("没收到任何消息。确认你发的是给这个 bot，且它没被别的进程抢着轮询。")
            return 1
        print("\n把下面这行写进 .env：")
        for chat_id, label in seen.items():
            print(f"TELEGRAM_CHAT_ID={chat_id}    # {label}")
        if len(seen) > 1:
            print("（收到多个来源，挑你自己那个）")
        return 0
    except TelegramError as exc:
        print(f"调用失败：{exc}")
        return 2
    finally:
        client.close()


def _cmd_check(args: Any) -> int:
    """`python -m core.telegram check`：探活 + 报当前配置是否可用（不发消息）。"""
    config = load_config()
    print(f"token: {mask_token(config.bot_token)}")
    print(f"chat_id: {config.chat_id or '(未配置)'}")
    print(f"签名密钥: {'已配置' if config.can_sign else '缺失（不会发带按钮的卡片）'}")
    print(f"通道开关: SW_TELEGRAM_ENABLED={'true' if config.enabled else 'false'}")
    if not config.configured:
        print("结论：不可用 —— 缺 TELEGRAM_BOT_TOKEN")
        return 2
    client = TelegramClient(config)
    try:
        me = client.get_me()
    except TelegramError as exc:
        print(f"结论：不可用 —— getMe 失败：{exc}")
        return 2
    finally:
        client.close()
    print(f"getMe: @{me.get('username')} (id={me.get('id')})")
    if not config.ready:
        print(f"结论：只能收不能发 —— {config.why_not_ready()}")
        return 1
    print("结论：可用")
    return 0


def build_parser() -> Any:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m core.telegram", description="Telegram 通道运维小工具"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_setup = sub.add_parser("setup", help="抓取给 bot 发 /start 的 chat_id")
    p_setup.add_argument("--wait", type=int, default=60, help="最多等几秒（默认 60）")
    p_setup.set_defaults(func=_cmd_setup)
    sub.add_parser("check", help="探活并报告配置是否可用（不发消息）").set_defaults(func=_cmd_check)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    return int(args.func(args))


__all__ = [
    "ACTIONS",
    "ACTION_CONFIRM",
    "ACTION_REJECT",
    "CALLBACK_VERSION",
    "CallbackRejected",
    "ConfirmCard",
    "OffsetStore",
    "TelegramClient",
    "TelegramConfig",
    "TelegramError",
    "TelegramNotifier",
    "TelegramPoller",
    "build_callback_data",
    "build_keyboard",
    "build_telegram_notifier",
    "channel_status",
    "get_poller",
    "get_telegram_channel",
    "load_config",
    "main",
    "mask_token",
    "parse_callback_data",
    "parse_ref",
    "reset_telegram_channel",
    "review_verdict",
    "set_telegram_channel",
    "sign_callback",
    "start_poller",
    "stop_poller",
    "summarize",
]


if __name__ == "__main__":
    raise SystemExit(main())
