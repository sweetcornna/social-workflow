"""Telegram 通道：签名 / 卡片文案 / long polling / 回调安全（P12）。

**这里一条真实网络请求都不许有。** ``api.telegram.org`` 全部由 respx 打桩——真跑验收
是主 agent 拿真 bot 做的事，测试要能在没有 token 的机器上、离线状态下跑通。
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from core.telegram import (
    ACTION_CONFIRM,
    ACTION_REJECT,
    CallbackRejected,
    ConfirmCard,
    OffsetStore,
    TelegramClient,
    TelegramConfig,
    TelegramError,
    TelegramNotifier,
    TelegramPoller,
    build_callback_data,
    build_keyboard,
    load_config,
    mask_token,
    parse_callback_data,
    parse_ref,
    review_verdict,
    summarize,
)

TOKEN = "4242424242:TESTONLYtoken0000000000000000000"
CHAT = "12345"
SECRET = "test-signing-secret"
API = "https://api.telegram.org"


def make_config(**overrides) -> TelegramConfig:
    base = {
        "bot_token": TOKEN,
        "chat_id": CHAT,
        "signing_secret": SECRET,
        "api_base": API,
        "timeout": 1.0,
        "poll_timeout": 0,
    }
    base.update(overrides)
    return TelegramConfig(**base)


def method_url(method: str) -> str:
    return f"{API}/bot{TOKEN}/{method}"


def ok_response(result):
    return httpx.Response(200, json={"ok": True, "result": result})


# --------------------------------------------------------------------- 凭据


def test_mask_token_never_leaks_the_secret_half():
    masked = mask_token(TOKEN)
    assert "TESTONLYtoken" not in masked
    assert masked.startswith("4242424242:")
    assert mask_token("") == "(未配置)"


def test_telegram_errors_never_carry_the_token():
    """异常文案会进日志。带上 URL 就等于把 token 写进日志了。"""
    config = make_config(api_base="http://127.0.0.1:1")  # 必然连不上
    client = TelegramClient(config)
    with pytest.raises(TelegramError) as exc:
        client.get_me()
    assert TOKEN not in str(exc.value)


def test_settings_fall_back_to_ui_token_for_signing(monkeypatch):
    """没配专用签名密钥时回落 SW_UI_TOKEN，再回落 bot token。"""
    from core.config import reload_settings

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("SW_UI_TOKEN", "ui-token-abc")
    monkeypatch.setenv("SW_TELEGRAM_SIGNING_SECRET", "")
    assert load_config(reload_settings()).signing_secret == "ui-token-abc"

    monkeypatch.setenv("SW_UI_TOKEN", "")
    assert load_config(reload_settings()).signing_secret == TOKEN


# --------------------------------------------------------------------- 签名


def test_callback_data_fits_telegram_64_byte_limit():
    data = build_callback_data(SECRET, ACTION_CONFIRM, "itm_0123456789ab")
    assert len(data.encode("utf-8")) <= 64
    assert parse_callback_data(SECRET, data) == (ACTION_CONFIRM, "itm_0123456789ab")


def test_forged_callback_data_is_rejected():
    """签名对不上 = 伪造。任何人都能给 bot 发东西，这一道不能松。"""
    data = build_callback_data(SECRET, ACTION_CONFIRM, "itm_aaa")
    tampered = data.rsplit(":", 1)[0] + ":deadbeef00"
    with pytest.raises(CallbackRejected) as exc:
        parse_callback_data(SECRET, tampered)
    assert exc.value.reason == "bad_signature"


def test_callback_data_signed_with_another_secret_is_rejected():
    data = build_callback_data("someone-elses-secret", ACTION_CONFIRM, "itm_aaa")
    with pytest.raises(CallbackRejected):
        parse_callback_data(SECRET, data)


def test_callback_data_for_another_item_is_rejected():
    """把签名搬到另一条内容上也不行——item_id 参与签名。"""
    data = build_callback_data(SECRET, ACTION_CONFIRM, "itm_aaa")
    moved = data.replace("itm_aaa", "itm_bbb")
    with pytest.raises(CallbackRejected):
        parse_callback_data(SECRET, moved)


@pytest.mark.parametrize(
    "raw,reason",
    [
        ("", "malformed"),
        ("v9:ok:itm_a:0000000000", "bad_version"),
        ("v1:zz:itm_a:0000000000", "bad_action"),
        ("v1:ok::0000000000", "bad_item"),
    ],
)
def test_malformed_callback_data_is_rejected(raw, reason):
    with pytest.raises(CallbackRejected) as exc:
        parse_callback_data(SECRET, raw)
    assert exc.value.reason == reason


def test_no_secret_means_no_verification_possible():
    with pytest.raises(CallbackRejected) as exc:
        parse_callback_data("", "v1:ok:itm_a:0000000000")
    assert exc.value.reason == "no_secret"


# --------------------------------------------------------------------- 卡片


def test_summary_breaks_at_a_sentence_boundary_not_mid_clause():
    """中文没有词间空格，硬按字数切会切在半句话中间。要退到最近的收束点。"""
    text = "第一句话说完了。第二句话正在展开，它比较长一点，一直写到超过上限为止，后面还有很多。"
    out = summarize(text, limit=30)
    assert out.endswith("…")
    assert len(out) <= 31
    body = out[:-1]
    assert text.startswith(body), "摘要必须是原文的前缀，不能改字"
    # 断点后面紧跟的是标点 = 断在句读上，而不是切进了半句话
    assert text[len(body)] in "。！？；，、"


def test_summary_leaves_short_text_alone():
    assert summarize("短文案") == "短文案"


def test_verdict_reads_like_a_sentence_not_a_field_dump():
    """卡片是给人在手机上一眼决定用的，不该出现 block=0 warn=0 这种机器口径。"""
    for text in (review_verdict(0, 0), review_verdict(0, 2), review_verdict(1, 1)):
        assert "block" not in text and "warn" not in text
    assert review_verdict(0, 0) == "机审通过 · 无阻断"
    assert "2 处提醒" in review_verdict(0, 2)


def _card(**overrides) -> ConfirmCard:
    base = {
        "item_id": "itm_abc123456789",
        "title": "租房收纳的第三个坑",
        "body": "把收纳箱买回来之前，先量一次柜子内壁。" * 6,
        "slot_text": "08-18 19:00（Asia/Shanghai）",
        "account_label": "甜玉米(acc_xhs_01)",
        "media_count": 3,
    }
    base.update(overrides)
    return ConfirmCard(**base)


def test_card_puts_title_slot_body_verdict_in_that_order():
    lines = [line for line in _card().render().splitlines() if line.strip()]
    assert lines[0] == "<b>租房收纳的第三个坑</b>"
    assert lines[1].startswith("08-18 19:00（Asia/Shanghai）")
    assert "甜玉米(acc_xhs_01)" in lines[1]
    assert lines[-1] == "机审通过 · 无阻断"


def test_card_keeps_the_title_when_it_is_edited_after_a_decision():
    """点完只换状态行，标题不变——同一条内容在对话里始终是同一张卡。"""
    card = _card()
    decided = card.render_decided("已确认 · 08-18 19:00（Asia/Shanghai）")
    assert decided.splitlines()[0] == card.render().splitlines()[0]
    assert decided.endswith("已确认 · 08-18 19:00（Asia/Shanghai）")


def test_buttons_say_what_pressing_them_does():
    markup = build_keyboard(SECRET, "itm_abc123456789", "https://ops.test/w", "看全部 3 张")
    labels = [b["text"] for row in markup["inline_keyboard"] for b in row]
    assert labels == ["确认发布", "不发", "看全部 3 张"]


def test_relative_review_url_is_dropped_instead_of_breaking_the_message():
    """Telegram 会拒收带相对路径的 url 按钮，整条消息都发不出去。宁可少一个按钮。"""
    markup = build_keyboard(SECRET, "itm_a", "/workbench/schedule?id=itm_a")
    assert len(markup["inline_keyboard"]) == 1


def test_single_image_says_see_full_text_instead_of_see_all_1():
    assert _card(media_count=1).media_button_text() == "看全文"
    assert _card(media_count=4).media_button_text() == "看全部 4 张"


# --------------------------------------------------------------- Notifier


@respx.mock
def test_notifier_sends_a_plain_message():
    route = respx.post(method_url("sendMessage")).mock(return_value=ok_response({"message_id": 7}))
    assert TelegramNotifier(make_config()).send("标题", "正文", "warning") is True
    body = json.loads(route.calls[0].request.content)
    assert body["chat_id"] == CHAT and "标题" in body["text"]


@respx.mock
def test_notifier_swallows_upstream_failures():
    """``Notifier`` 协议约定：通知失败绝不能拖垮主流程。"""
    respx.post(method_url("sendMessage")).mock(
        return_value=httpx.Response(200, json={"ok": False, "error_code": 403, "description": "x"})
    )
    notifier = TelegramNotifier(make_config())
    assert notifier.send("标题", "正文") is False
    assert notifier.failed_count == 1


def test_notifier_refuses_to_send_without_a_chat_id_and_says_why():
    """静默不发是最糟的失败方式：拒发要留下一句能照着做的话。"""
    config = make_config(chat_id="")
    assert TelegramNotifier(config).send("标题", "正文") is False
    assert "/start" in config.why_not_ready()
    assert "core.telegram setup" in config.why_not_ready()


@respx.mock
def test_confirm_card_goes_out_as_a_photo_when_there_is_a_cover(tmp_path):
    """配图就是主体：有封面就走 sendPhoto，而不是发一条纯文字再附链接。"""
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"\x89PNG\r\n\x1a\n fake")
    route = respx.post(method_url("sendPhoto")).mock(return_value=ok_response({"message_id": 42}))
    ref = TelegramNotifier(make_config()).send_confirm_card(_card(), cover=cover)
    assert ref == f"{CHAT}:42:photo"
    assert route.called


@respx.mock
def test_confirm_card_falls_back_to_text_without_a_cover():
    respx.post(method_url("sendMessage")).mock(return_value=ok_response({"message_id": 43}))
    assert TelegramNotifier(make_config()).send_confirm_card(_card()) == f"{CHAT}:43:text"


def test_confirm_card_is_not_sent_without_a_signing_secret():
    """没有防伪造能力时宁可不发按钮：一张点了不算数的卡比没有卡更糟。"""
    assert TelegramNotifier(make_config(signing_secret="")).send_confirm_card(_card()) is None


@respx.mock
def test_edit_card_drops_the_buttons_so_it_cannot_be_pressed_again():
    route = respx.post(method_url("editMessageText")).mock(return_value=ok_response({}))
    assert TelegramNotifier(make_config()).edit_card(f"{CHAT}:9:text", "已确认") is True
    body = json.loads(route.calls[0].request.content)
    assert body["reply_markup"] == {"inline_keyboard": []}


@respx.mock
def test_edit_card_uses_caption_for_photo_messages():
    route = respx.post(method_url("editMessageCaption")).mock(return_value=ok_response({}))
    TelegramNotifier(make_config()).edit_card(f"{CHAT}:9:photo", "已驳回")
    assert route.called


def test_parse_ref_defaults_to_text_for_legacy_refs():
    assert parse_ref("12345:9") == ("12345", 9, "text")


# ------------------------------------------------------------- offset 持久化


def test_offset_store_survives_a_restart(tmp_path):
    """进程重启不许重复消费已处理过的回调。"""
    path = tmp_path / "state.json"
    OffsetStore(path).save(1024)
    assert OffsetStore(path).load() == 1024


def test_offset_store_returns_none_on_a_missing_or_broken_file(tmp_path):
    assert OffsetStore(tmp_path / "nope.json").load() is None
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert OffsetStore(broken).load() is None


@respx.mock
def test_poller_advances_and_persists_the_offset(tmp_path):
    store = OffsetStore(tmp_path / "state.json")
    respx.post(method_url("getUpdates")).mock(
        return_value=ok_response(
            [{"update_id": 77, "message": {"chat": {"id": 999, "type": "private"}}}]
        )
    )
    poller = TelegramPoller(make_config(), handler=lambda *a: ("", ""), store=store)
    poller.poll_once()
    assert poller.offset == 78
    assert store.load() == 78


# --------------------------------------------------------------- 回调安全


def callback(*, chat_id=CHAT, user_id=int(CHAT), action=ACTION_CONFIRM, item_id="itm_aaa"):
    return {
        "id": "cb-1",
        "from": {"id": user_id, "is_bot": False},
        "message": {"message_id": 5, "chat": {"id": int(chat_id), "type": "private"}},
        "data": build_callback_data(SECRET, action, item_id),
    }


def make_poller(config=None, handler=None):
    calls: list[tuple[str, str, str]] = []

    def default_handler(action: str, item_id: str, actor: str) -> tuple[str, str]:
        calls.append((action, item_id, actor))
        return "好了", "卡片新文案"

    poller = TelegramPoller(config or make_config(), handler=handler or default_handler)
    return poller, calls


@respx.mock
def test_callback_from_a_stranger_chat_is_ignored_without_any_reply():
    """任何人拿到 bot 用户名都能找到它。不认识的 chat 一律忽略，连回执都不给
    ——不给探测者"这个 bot 认识我"的信号。"""
    answered = respx.post(method_url("answerCallbackQuery")).mock(return_value=ok_response(True))
    edited = respx.post(method_url("editMessageText")).mock(return_value=ok_response({}))
    poller, calls = make_poller()
    poller.handle_callback(callback(chat_id="999999", user_id=999999))
    assert calls == []
    assert poller.stats["rejected"] == 1
    assert not answered.called and not edited.called


@respx.mock
def test_callback_from_another_user_in_the_right_chat_is_ignored():
    """群里 chat_id 对得上，但点按钮的不是本人。没配白名单就一律拒——往关的方向失败。"""
    poller, calls = make_poller()
    poller.handle_callback(callback(user_id=777))
    assert calls == [] and poller.stats["rejected"] == 1


@respx.mock
def test_allowlisted_user_may_press_the_button_in_a_group():
    poller, calls = make_poller(make_config(allowed_user_ids=frozenset({777})))
    respx.post(method_url("answerCallbackQuery")).mock(return_value=ok_response(True))
    respx.post(method_url("editMessageText")).mock(return_value=ok_response({}))
    poller.handle_callback(callback(user_id=777))
    assert calls == [(ACTION_CONFIRM, "itm_aaa", "tg:777")]


@respx.mock
def test_forged_signature_is_ignored_even_from_the_right_chat():
    poller, calls = make_poller()
    payload = callback()
    payload["data"] = payload["data"].rsplit(":", 1)[0] + ":0000000000"
    poller.handle_callback(payload)
    assert calls == [] and poller.stats["rejected"] == 1


@respx.mock
def test_valid_callback_answers_and_rewrites_the_card():
    answered = respx.post(method_url("answerCallbackQuery")).mock(return_value=ok_response(True))
    edited = respx.post(method_url("editMessageText")).mock(return_value=ok_response({}))
    poller, calls = make_poller()
    poller.handle_callback(callback(action=ACTION_REJECT))
    assert calls == [(ACTION_REJECT, "itm_aaa", f"tg:{CHAT}")]
    assert answered.called and edited.called
    assert poller.stats["handled"] == 1


@respx.mock
def test_a_failing_handler_never_kills_the_polling_thread():
    respx.post(method_url("answerCallbackQuery")).mock(return_value=ok_response(True))

    def boom(*_args):
        raise RuntimeError("业务炸了")

    poller, _ = make_poller(handler=boom)
    poller.handle_callback(callback())  # 不抛
    assert poller.stats["errors"] == 1


@respx.mock
def test_a_stale_card_from_the_real_user_gets_an_answer_instead_of_a_spinner():
    """**本人**点了一张签名对不上的卡：必须回执，否则手机上那个圈永远转下去。

    2026-08-24 真出过：本机 dev 用生产 bot token 推了张卡，签名是本机密钥，
    轮询的却是生产进程 → ``reason=bad_signature``。拒绝路径只 ``+1`` 计数就
    ``return``，从没调 ``answerCallbackQuery``，于是点的人对着转圈干等、
    没有任何提示，也不知道该去工作台。

    和 :func:`test_callback_from_a_stranger_chat_is_ignored_without_any_reply`
    是**两回事**，别合并：那条讲的是来源不认识，沉默是刻意的（不给探测者
    "这个 bot 认识我"的信号）；这条的来源**已经通过授权**，只是签名过期，
    回执一句话既不泄露任何东西，也是唯一能把圈停掉的办法。
    """
    answered = respx.post(method_url("answerCallbackQuery")).mock(return_value=ok_response(True))
    edited = respx.post(method_url("editMessageText")).mock(return_value=ok_response({}))
    poller, calls = make_poller()

    payload = callback()
    payload["data"] = payload["data"].rsplit(":", 1)[0] + ":0000000000"
    poller.handle_callback(payload)

    assert calls == [], "签名不对还是绝对不许应用任何决定"
    assert poller.stats["rejected"] == 1
    assert answered.called, "本人点的失效卡没有回执 → 手机上永远 loading"
    assert not edited.called, "卡片正文不许改：这条回调没通过校验"
    body = json.loads(answered.calls.last.request.content)
    assert body["callback_query_id"] == "cb-1"
    assert body["text"], "回执得说句人话，不能是空串"
    assert "itm_" not in body["text"], "回执里不许带 item id"


@respx.mock
def test_an_unauthorized_sender_still_gets_total_silence():
    """回归护栏：上面那条回执**不许**扩散到来源就不认识的回调上。"""
    answered = respx.post(method_url("answerCallbackQuery")).mock(return_value=ok_response(True))
    poller, calls = make_poller()
    poller.handle_callback(callback(chat_id="999999", user_id=999999))
    assert calls == [] and poller.stats["rejected"] == 1
    assert not answered.called, "对陌生来源开口了 —— 等于告诉探测者这个 bot 认识谁"
