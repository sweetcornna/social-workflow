"""生图客户端（P11）：成功 / url 回退 / 权限未开 / 预算拒绝 / 尺寸合成。

**一律用 respx 打桩，绝不真调网关**——真图冒烟由人工在验收时跑一次（见交付报告）。
"""

from __future__ import annotations

import base64
import struct
import zlib

import httpx
import pytest
import respx

from core.budget import BudgetExhausted, BudgetGuard, CostKind
from core.config import reload_settings
from generation import imagegen
from generation.imagegen import (
    ASPECT_LANDSCAPE_3_2,
    ASPECT_PORTRAIT_3_4,
    ASPECT_SQUARE_1_1,
    ASPECT_VERTICAL_9_16,
    ENABLE_HINT,
    SIZE_PORTRAIT,
    GeneratedImage,
    ImagegenAPIError,
    ImagegenClient,
    ImagegenConnectionError,
    ImagegenNotEnabled,
    ImagegenRateLimited,
    ImagegenUnavailable,
    fit_to_canvas,
    generate_batch,
    image_prompt_rules,
    imagegen_status,
    measure,
    normalize_image_prompts,
    plan_illustrations,
)

BASE = "https://imagegen.test/v1"
URL = f"{BASE}/images/generations"


# ------------------------------------------------------------------ 造图工具


def png_bytes(width: int, height: int) -> bytes:
    """造一张真 PNG。**尺寸写进 IHDR**，这样 read_image_size 量到的就是它。"""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + b"\x80" * (width * 3) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def ok_payload(width: int = 1086, height: int = 1448) -> dict:
    """网关的成功响应。默认尺寸是 gpt-image-2 实测返回的那个非标准尺寸。"""
    return {
        "model": "gpt-image-2",
        "data": [
            {
                "b64_json": base64.b64encode(png_bytes(width, height)).decode("ascii"),
                "revised_prompt": "a linen tablecloth still life, soft window light",
            }
        ],
        "usage": {"input_tokens": 42, "output_tokens": 1580, "total_tokens": 1622},
    }


@pytest.fixture
def enabled(monkeypatch):
    """把生图开关打开（conftest 默认关掉，防止误调真网关）。"""
    monkeypatch.setenv("SW_IMAGEGEN_ENABLED", "auto")
    reload_settings()
    imagegen.reset_availability()
    yield
    imagegen.reset_availability()


def client(**kwargs) -> ImagegenClient:
    return ImagegenClient(base_url=BASE, api_key="sk-test", model="gpt-image-2", **kwargs)


# ------------------------------------------------------------------ 成功路径


@respx.mock
def test_generate_reads_actual_png_size_not_requested_size(tmp_path, enabled) -> None:
    """核心不变量：尺寸以 **PNG IHDR 实测值** 为准，不信请求里的 size。

    实测过的行为：请求 1024x1536，gpt-image-2 返回 1086x1448。
    """
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=ok_payload()))

    with client() as c:
        image = c.generate("still life", tmp_path / "a.png", size=SIZE_PORTRAIT)

    assert route.called
    body = route.calls[0].request
    assert body.headers["authorization"] == "Bearer sk-test"
    sent = httpx.Response(200, content=body.content).json()
    # 显式给足参数：n / size / response_format 一个都不能省（P10.1 的教训）
    assert sent == {
        "model": "gpt-image-2",
        "prompt": "still life",
        "n": 1,
        "size": "1024x1536",
        "response_format": "b64_json",
    }

    assert image.requested_size == "1024x1536"
    assert (image.width, image.height) == (1086, 1448)
    assert measure(image.path) == (1086, 1448)
    assert image.revised_prompt.startswith("a linen tablecloth")
    assert image.usage.as_meta()["total_tokens"] == 1622
    assert image.path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


@respx.mock
def test_generate_falls_back_to_url_download(tmp_path, enabled) -> None:
    """没有 b64_json 时按 url 回退下载。"""
    respx.post(URL).mock(
        return_value=httpx.Response(
            200, json={"data": [{"url": "https://cdn.test/i.png"}], "usage": {}}
        )
    )
    download = respx.get("https://cdn.test/i.png").mock(
        return_value=httpx.Response(200, content=png_bytes(1024, 1024))
    )

    with client() as c:
        image = c.generate("x", tmp_path / "b.png")

    assert download.called
    assert (image.width, image.height) == (1024, 1024)


@respx.mock
def test_generate_charges_one_image_with_tokens_in_meta(session, tmp_path, enabled) -> None:
    """记账按**张**计，token 用量只作为观测字段落进 meta。"""
    respx.post(URL).mock(return_value=httpx.Response(200, json=ok_payload()))
    guard = BudgetGuard(session, labels={"account_id": "acc-1"})

    with client(budget=guard) as c:
        c.generate(
            "x",
            tmp_path / "c.png",
            purpose="xhs.illustration",
            account_id="acc-1",
            platform="xhs",
        )

    assert guard.used(CostKind.IMAGES) == 1.0
    # token 预算一分没动：生图有独立的计价口径，不该吃写稿的额度
    assert guard.used(CostKind.TOKENS) == 0.0
    entry = session.query(type(guard.charge(CostKind.IMAGES, 0))).all()[0]
    meta = entry.meta
    assert meta["purpose"] == "xhs.illustration"
    assert meta["platform"] == "xhs"
    assert meta["account_id"] == "acc-1"
    assert meta["size"] == "1024x1536"
    # 请求的和实返的都留痕，方便日后回看网关行为
    assert meta["actual_size"] == "1086x1448"
    assert meta["output_tokens"] == 1580


# ------------------------------------------------------------------ 失败降级


@respx.mock
def test_permission_error_becomes_not_enabled_with_actionable_hint(tmp_path, enabled) -> None:
    """permission_error 单独识别，并给出"去哪儿开开关"的人话。"""
    respx.post(URL).mock(
        return_value=httpx.Response(
            403,
            json={"error": {"type": "permission_error", "message": "no image permission"}},
        )
    )

    with client() as c, pytest.raises(ImagegenNotEnabled) as excinfo:
        c.generate("x", tmp_path / "d.png")

    assert ENABLE_HINT in str(excinfo.value)
    # 会话级熔断：同一进程内不再反复试，省钱也省时间
    assert imagegen.unavailable_reason() is not None
    assert imagegen_status().ready is False


@respx.mock
def test_second_call_short_circuits_after_permission_error(tmp_path, enabled) -> None:
    """熔断之后连请求都不发。"""
    route = respx.post(URL).mock(
        return_value=httpx.Response(403, json={"error": {"type": "permission_error"}})
    )
    with client() as c:
        with pytest.raises(ImagegenNotEnabled):
            c.generate("x", tmp_path / "e.png")
        with pytest.raises(ImagegenUnavailable):
            c.generate("x", tmp_path / "f.png")
    assert route.call_count == 1


@respx.mock
def test_rate_limit_and_server_error_classification(tmp_path, enabled) -> None:
    respx.post(URL).mock(return_value=httpx.Response(429, headers={"retry-after": "12"}))
    with client() as c, pytest.raises(ImagegenRateLimited) as rate:
        c.generate("x", tmp_path / "g.png")
    assert rate.value.retry_after == 12.0
    # 429 不熔断：它是抖动，等一会儿还能用
    assert imagegen.unavailable_reason() is None

    respx.post(URL).mock(return_value=httpx.Response(500, text="boom"))
    with client() as c, pytest.raises(ImagegenAPIError) as api:
        c.generate("x", tmp_path / "h.png")
    assert api.value.status_code == 500


@respx.mock
def test_connection_error_is_translated(tmp_path, enabled) -> None:
    respx.post(URL).mock(side_effect=httpx.ConnectError("dns"))
    with client() as c, pytest.raises(ImagegenConnectionError):
        c.generate("x", tmp_path / "i.png")


@respx.mock
def test_budget_exhausted_rejects_before_spending_money(session, tmp_path, enabled) -> None:
    """超预算时**一个请求都不发**——先问后花。"""
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=ok_payload()))
    guard = BudgetGuard(session, image_budget=1)
    guard.charge(CostKind.IMAGES, 1, meta={})

    with client(budget=guard) as c, pytest.raises(BudgetExhausted):
        c.generate("x", tmp_path / "j.png")
    assert not route.called


def test_disabled_switch_blocks_without_network(tmp_path) -> None:
    """``SW_IMAGEGEN_ENABLED=false``（conftest 的默认）直接不可用。"""
    status = imagegen_status()
    assert status.ready is False
    assert "false" in status.reason
    with ImagegenClient(base_url=BASE, api_key="k") as c, pytest.raises(ImagegenUnavailable):
        c.generate("x", tmp_path / "k.png")


def test_missing_key_is_reported_with_fix_hint(monkeypatch) -> None:
    monkeypatch.setenv("SW_IMAGEGEN_ENABLED", "auto")
    monkeypatch.setenv("SW_IMAGEGEN_API_KEY", "")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    reload_settings()
    status = imagegen_status()
    assert status.ready is False
    assert status.has_api_key is False
    assert "SW_IMAGEGEN_API_KEY" in status.hint


def test_base_url_falls_back_to_dsh_gateway(monkeypatch) -> None:
    """没单独配生图端点时复用 dsh 那条网关地址。"""
    monkeypatch.setenv("SW_IMAGEGEN_BASE_URL", "")
    monkeypatch.setenv("SW_DSH_DEEPSEEK_BASE_URL", "https://gw.test/v1")
    reload_settings()
    assert imagegen.resolve_base_url() == "https://gw.test/v1"


def test_default_model_is_gpt_image_2(monkeypatch) -> None:
    """用户拍板：代码默认就是 image2，不靠 .env 兜。"""
    monkeypatch.delenv("SW_IMAGEGEN_MODEL", raising=False)
    reload_settings()
    from core.config import Settings

    assert Settings(_env_file=None).sw_imagegen_model == "gpt-image-2"


# ------------------------------------------------------------------ 目标画幅
#
# 实测（2026-08-24，真调上游）：``size`` 参数被本网关吞掉，同一条裸 prompt
# 请求 1024x1536 两次分别实返 1122×1402（0.800）与 1254×1254（1.000），
# 请求 1536x1024 实返 1254×1254（1.000）——形状是模型自己挑的。
# 把画幅写进 prompt 前缀才管用：3:4→1086×1448（0.750）、9:16→941×1672（0.563）、
# 16:9→1672×941（1.777）。所以下面这些断言全部落在**送出去的 prompt 文本**上。


@respx.mock
def test_aspect_prefixes_the_directive_onto_the_wire_prompt(tmp_path, enabled) -> None:
    """核心：画幅指令必须出现在**真正发给网关的那条 prompt** 里，不是只存在于常量里。"""
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=ok_payload()))

    with client() as c:
        image = c.generate("still life", tmp_path / "a.png", aspect=ASPECT_PORTRAIT_3_4)

    sent = httpx.Response(200, content=route.calls[0].request.content).json()
    assert sent["prompt"] == (
        "vertical portrait orientation, 3:4 aspect ratio (taller than wide). still life"
    )
    assert sent["prompt"].endswith("still life"), "原 prompt 必须原样保留在后面"
    # size 仍然照发（换一台认它的网关时还得靠它），只是本网关不认
    assert sent["size"] == "1024x1536"
    # 留痕记的是发出去的那条，出了问题能原样复现
    assert image.prompt == sent["prompt"]
    assert image.as_meta()["prompt"] == sent["prompt"]


@respx.mock
def test_each_purpose_sends_a_different_directive(tmp_path, enabled) -> None:
    """不同用途拿到**不同**指令：小红书竖 3:4、公众号横 3:2、抖音竖 9:16。"""
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=ok_payload()))

    with client() as c:
        for index, spec in enumerate(
            (ASPECT_PORTRAIT_3_4, ASPECT_LANDSCAPE_3_2, ASPECT_VERTICAL_9_16)
        ):
            c.generate("a woven basket", tmp_path / f"{index}.png", aspect=spec)

    prompts_sent = [
        httpx.Response(200, content=call.request.content).json()["prompt"] for call in route.calls
    ]
    assert len(set(prompts_sent)) == 3, f"三个用途送出了重复的 prompt：{prompts_sent}"
    assert "3:4 aspect ratio (taller than wide)" in prompts_sent[0]
    assert "3:2 aspect ratio (wider than tall)" in prompts_sent[1]
    assert "9:16 aspect ratio (much taller than wide)" in prompts_sent[2]
    # 竖版指令不能跑到横版用途上去，反之亦然
    assert "taller than wide" not in prompts_sent[1]
    assert "wider than tall" not in prompts_sent[0]

    sizes = [httpx.Response(200, content=c_.request.content).json()["size"] for c_ in route.calls]
    assert sizes == ["1024x1536", "1536x1024", "1024x1536"]


@respx.mock
def test_no_aspect_leaves_the_prompt_untouched(tmp_path, enabled) -> None:
    """不给画幅就一个字都不加——preflight 之类的调用方不该被塞进构图要求。"""
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=ok_payload()))

    with client() as c:
        c.generate("still life", tmp_path / "a.png")

    sent = httpx.Response(200, content=route.calls[0].request.content).json()
    assert sent["prompt"] == "still life"
    assert sent["size"] == "1024x1536"


@respx.mock
def test_explicit_size_still_wins_over_the_aspect_size(tmp_path, enabled) -> None:
    """``size`` 没被废：显式给了就照发，指令照旧前置。"""
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=ok_payload()))

    with client() as c:
        c.generate("still life", tmp_path / "a.png", aspect=ASPECT_VERTICAL_9_16, size="1024x1024")

    sent = httpx.Response(200, content=route.calls[0].request.content).json()
    assert sent["size"] == "1024x1024"
    assert sent["prompt"].startswith("vertical portrait orientation, 9:16")


def test_aspect_directive_is_idempotent() -> None:
    """重试时不许把指令叠成两遍。"""
    once = ASPECT_SQUARE_1_1.apply("a bowl")
    assert ASPECT_SQUARE_1_1.apply(once) == once
    assert once.count("1:1 aspect ratio") == 1


def test_every_aspect_declares_the_ratio_it_asks_for() -> None:
    """``ratio`` 是给调用方判断"拿到的图偏了多少"的，必须和指令说的是同一件事。"""
    assert round(ASPECT_PORTRAIT_3_4.ratio, 4) == 0.75
    assert round(ASPECT_LANDSCAPE_3_2.ratio, 4) == 1.5
    assert round(ASPECT_VERTICAL_9_16.ratio, 4) == 0.5625
    assert round(ASPECT_SQUARE_1_1.ratio, 4) == 1.0
    for spec in (ASPECT_PORTRAIT_3_4, ASPECT_LANDSCAPE_3_2, ASPECT_VERTICAL_9_16):
        assert spec.directive.endswith(" "), "指令要自带尾空格，否则会和原 prompt 粘在一起"
        assert (spec.ratio < 1) == ("taller than wide" in spec.directive)


@respx.mock
def test_generate_batch_forwards_the_aspect_to_every_image(tmp_path, enabled) -> None:
    """批量生成时**每一张**都要带指令，不能只有第一张。"""
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=ok_payload()))

    with client() as c:
        generate_batch(c, ["a", "b"], tmp_path, aspect=ASPECT_PORTRAIT_3_4)

    assert route.call_count == 2
    for call in route.calls:
        sent = httpx.Response(200, content=call.request.content).json()
        assert sent["prompt"].startswith("vertical portrait orientation, 3:4")
        assert sent["size"] == "1024x1536"


# --------------------------------------------------------------- 批量与规划


@respx.mock
def test_generate_batch_stops_after_first_failure(tmp_path, enabled) -> None:
    """第一张失败就停：同一个原因对后面几张同样成立，继续试只是白烧钱。"""
    route = respx.post(URL).mock(
        return_value=httpx.Response(500, json={"error": {"message": "gateway down"}})
    )
    warnings: list[str] = []
    with client() as c:
        produced = generate_batch(c, ["a", "b", "c"], tmp_path, warnings=warnings)
    assert produced == []
    assert route.call_count == 1
    assert any("gateway down" in w for w in warnings)


@respx.mock
def test_generate_batch_numbers_files_in_order(tmp_path, enabled) -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, json=ok_payload()))
    with client() as c:
        produced = generate_batch(c, ["a", "b"], tmp_path, stem="illustration")
    assert [p.path.name for p in produced] == ["illustration-1.png", "illustration-2.png"]


def test_plan_illustrations_degrades_with_reason() -> None:
    """开关关掉时返回 0 张并给出人话原因，调用方据此连 prompt 都不写。"""
    warnings: list[str] = []
    assert plan_illustrations(2, warnings=warnings) == 0
    assert warnings and "没有生成配图" in warnings[0]


def test_plan_illustrations_prefers_injected_client() -> None:
    """注入的替身比环境变量更具体，即便开关关着也认它。"""
    assert plan_illustrations(3, injected=object()) == 3  # type: ignore[arg-type]


def test_plan_illustrations_zero_is_a_noop() -> None:
    assert plan_illustrations(0) == 0


def test_plan_illustrations_does_not_build_a_client(monkeypatch, enabled) -> None:
    """规划阶段不许造客户端：写稿链一抛异常就会漏一个没关的连接池。"""
    monkeypatch.setattr(
        imagegen, "build_imagegen", lambda **kw: pytest.fail("规划阶段不该造客户端")
    )
    assert plan_illustrations(2) == 2


@respx.mock
def test_illustrator_closes_the_client_it_built(enabled) -> None:
    """自己造的要自己关；注入的替身由调用方负责，不许替它关。"""
    from generation.imagegen import illustrator

    with illustrator(budget=None) as built:
        inner = built.client
    assert inner.is_closed

    injected = client()
    borrowed_transport = injected.client  # 先用一下，让它真的持有连接池
    with illustrator(injected) as borrowed:
        assert borrowed is injected
    assert not borrowed_transport.is_closed, "注入的替身由调用方负责生命周期，不许替它关"


# ------------------------------------------------------------------ prompt


def test_image_prompt_rules_carries_hard_constraints() -> None:
    text = image_prompt_rules(2)
    assert "2 张" in text
    for redline in ("no text", "no recognizable faces", "logo"):
        assert redline in text
    for safety_rule in (
        "液体、杯具和潮湿物品远离插座",
        "热源远离",
        "清洁剂、药剂等化学品与食品",
        "不遮挡通风口",
        "绊倒线缆",
        "不稳定堆叠",
        "安全替代构图",
    ):
        assert safety_rule in text
    assert image_prompt_rules(0).startswith("本次**不需要**配图")


def test_normalize_image_prompts_dedupes_and_truncates() -> None:
    prompts, warnings = normalize_image_prompts(["  a  b ", "A B", "x" * 900], count=3)
    assert prompts[0] == "a b"  # 空白折叠
    assert len(prompts) == 2  # 大小写不同但内容一样，去重
    assert len(prompts[1]) == 600
    assert any("去重" in w for w in warnings)
    assert any("少于" in w for w in warnings)


def test_normalize_image_prompts_handles_garbage() -> None:
    assert normalize_image_prompts(None, count=2) == (
        [],
        ["模型没给 image_prompts（拿到 NoneType）"],
    )
    assert normalize_image_prompts(["a"], count=0) == ([], [])


# ------------------------------------------------------------------ 尺寸合成


def test_fit_to_canvas_produces_exact_size(tmp_path) -> None:
    """裁切结果必须是精确尺寸——截图用注入的替身，不起真浏览器。"""
    source = tmp_path / "src.png"
    source.write_bytes(png_bytes(1086, 1448))
    captured: dict = {}

    def fake_shot(html: str, path, width: int, height: int) -> None:
        captured.update(html=html, width=width, height=height)
        path.write_bytes(png_bytes(width, height))

    out = fit_to_canvas(source, tmp_path / "out.png", 1242, 1656, screenshotter=fake_shot)
    assert out is not None
    assert measure(out) == (1242, 1656)
    # 图必须内嵌成 data: URI：截图页面没有 base URL，外链一律加载不到
    assert "data:image/png;base64," in captured["html"]
    assert "background-size:cover" in captured["html"]


def test_fit_to_canvas_returns_none_when_screenshot_unavailable(tmp_path, monkeypatch) -> None:
    from generation import cover

    source = tmp_path / "src.png"
    source.write_bytes(png_bytes(100, 100))

    def boom(*args, **kwargs):
        raise cover.ScreenshotUnavailable("没装 chromium")

    monkeypatch.setattr(cover, "screenshot_html", boom)
    assert fit_to_canvas(source, tmp_path / "o.png", 10, 10) is None


def test_fit_to_canvas_missing_source(tmp_path) -> None:
    assert fit_to_canvas(tmp_path / "nope.png", tmp_path / "o.png", 10, 10) is None


def test_generated_image_meta_records_measured_size(tmp_path) -> None:
    path = tmp_path / "x.png"
    path.write_bytes(png_bytes(1086, 1448))
    image = GeneratedImage(
        path=path,
        requested_size="1024x1536",
        model="gpt-image-2",
        prompt="p",
        width=1086,
        height=1448,
    )
    meta = image.as_meta()
    assert meta["requested_size"] == "1024x1536"
    assert meta["actual_size"] == [1086, 1448]
    assert round(image.aspect or 0, 3) == 0.75


def test_unmeasured_image_reports_honestly() -> None:
    image = GeneratedImage(
        path=__import__("pathlib").Path("x"), requested_size="", model="", prompt=""
    )
    assert image.measured is False
    assert image.aspect is None
    assert image.size_text == "未量到"
    assert image.as_meta()["actual_size"] is None
