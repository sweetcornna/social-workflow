"""`scripts/workbench_mcp.py` 的单测：信封错误映射、无 confirm 的注册表、鉴权头传递。

全部用 httpx mock（respx），**不需要 core 起着**。
"""

from __future__ import annotations

import ast
import asyncio
import base64
import json
from pathlib import Path

import httpx
import pytest
import respx

pytest.importorskip("mcp", reason="需要 `uv sync --extra mcp`")

from mcp.server.elicitation import (
    AcceptedElicitation,
    CancelledElicitation,
    DeclinedElicitation,
)
from mcp.server.mcpserver import Image
from mcp.server.mcpserver.exceptions import ToolError

from scripts import workbench_mcp as wb

BASE = "http://127.0.0.1:9999"
API = f"{BASE}/api/v1"

#: 1×1 PNG，够小又是合法 base64。
TINY_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


@pytest.fixture(autouse=True)
def _point_at_the_mock(monkeypatch):
    monkeypatch.setenv("SW_MCP_BASE_URL", BASE)
    monkeypatch.delenv("SW_UI_TOKEN", raising=False)


def ok(data):
    return httpx.Response(200, json={"ok": True, "data": data, "error": None})


class FakeSession:
    """`ctx.session` 的替身：闸门只问它一句「客户端声明了 elicitation 没有」。"""

    def __init__(self, *, supports: bool):
        self.supports = supports
        self.asked = []

    def check_client_capability(self, capability):
        self.asked.append(capability)
        return self.supports


class FakeContext:
    """够用的 `Context` 替身。

    真 Context 要一整套 request_context / session 才能建起来，而闸门只碰两处：
    `ctx.session.check_client_capability` 与 `ctx.elicit`。鸭子类型到此为止。
    """

    def __init__(self, *, answer: str = "accept", supports: bool = True, raises=None):
        self.session = FakeSession(supports=supports)
        self.answer = answer
        self.raises = raises
        self.messages: list[str] = []

    async def elicit(self, message, schema):
        self.messages.append(message)
        if self.raises is not None:
            raise self.raises
        if self.answer == "accept":
            return AcceptedElicitation(data=schema())
        if self.answer == "decline":
            return DeclinedElicitation()
        return CancelledElicitation()

    @property
    def only_message(self) -> str:
        assert len(self.messages) == 1, f"应当只弹一次确认，实际 {len(self.messages)} 次"
        return self.messages[0]


def brief_route(item_id="itm_1", **item):
    """确认文案里那句稿件摘要要读一次 `/review/{id}`（只读，且失败不挡确认）。"""
    row = {"id": item_id, "title": "秋天第一杯奶茶", "platform": "xhs", "status": "draft"}
    row.update(item)
    return respx.get(f"{API}/review/{item_id}").mock(return_value=ok({"item": row}))


def fail(status, code, message, detail=None):
    return httpx.Response(
        status,
        json={
            "ok": False,
            "data": None,
            "error": {"code": code, "message": message, "detail": detail},
        },
    )


# --------------------------------------------------------------- 无 confirm


def test_no_tool_name_mentions_confirm():
    """遍历注册表：任何工具名里都不许出现 confirm。

    发布确认只属于人（Telegram 闸门 / 工作台按钮）。这里**没有这个函数**，
    不是"有但被拒绝"——见 `docs/POLICY.md` 与模块头注释。
    """
    names = [tool.name for tool in asyncio.run(wb.mcp.list_tools())]
    assert names, "注册表是空的，这个断言就没意义了"
    assert [n for n in names if "confirm" in n.lower()] == []


def test_no_executable_code_can_build_the_confirm_endpoint():
    """源码里不许有任何**可执行**字符串拼出 `/confirm` 端点。

    文档与注释可以（也应该）解释"为什么这里没有它"，所以只查非 docstring 的字符串
    常量——f-string 的字面段同样会被 ast 拆成 Constant，照样查得到。
    """
    source = Path(wb.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstrings.add(id(first.value))

    offenders = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        and "/confirm" in node.value
    ]
    assert offenders == [], f"可执行代码里出现了 confirm 端点：{offenders}"


def test_account_update_cannot_flip_the_confirm_gate():
    """`account_update` 不许暴露 `confirm_required`——那道闸门没有旁路。"""
    schema = {tool.name: tool.input_schema for tool in asyncio.run(wb.mcp.list_tools())}[
        "account_update"
    ]
    assert "confirm_required" not in schema.get("properties", {})


def test_tool_names_the_chat_card_renderer_depends_on_exist():
    """P15-S5 的专用卡按这些名字注册 slot，改名会让卡片静默回落成通用 JSON 树。"""
    names = {tool.name for tool in asyncio.run(wb.mcp.list_tools())}
    for required in (
        "dashboard",
        "review_list",
        "content_list",
        "accounts_list",
        "account_login_status",
        "account_login_start",
        "account_login_qrcode",
        "costs",
        "stats",
    ):
        assert required in names, f"S5 的渲染器依赖 {required}，不能改名"


# --------------------------------------------------------------- 鉴权头


@respx.mock
def test_bearer_header_is_sent_when_token_configured(monkeypatch):
    monkeypatch.setenv("SW_UI_TOKEN", "tok-secret-123")
    route = respx.get(f"{API}/dashboard").mock(return_value=ok({"counters": {}}))

    asyncio.run(wb.dashboard())

    assert route.calls.last.request.headers["authorization"] == "Bearer tok-secret-123"


@respx.mock
def test_no_authorization_header_when_token_absent():
    """core 默认不鉴权。空 token 时**不发**这个头，别让 core 看到一个空 Bearer。"""
    route = respx.get(f"{API}/dashboard").mock(return_value=ok({}))

    asyncio.run(wb.dashboard())

    assert "authorization" not in route.calls.last.request.headers


@respx.mock
def test_base_url_comes_from_env(monkeypatch):
    monkeypatch.setenv("SW_MCP_BASE_URL", "http://10.0.0.5:8000/")
    route = respx.get("http://10.0.0.5:8000/api/v1/dashboard").mock(return_value=ok({}))

    asyncio.run(wb.dashboard())

    assert route.called


def test_default_base_url_is_localhost_8000(monkeypatch):
    monkeypatch.delenv("SW_MCP_BASE_URL", raising=False)
    assert wb.WorkbenchClient().base_url == "http://127.0.0.1:8000"


# --------------------------------------------------------------- 代理绕行


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://[::1]:8000",
        "http://192.168.1.20:8000",
        "http://10.0.0.5:8000",
        "http://core.local:8000",
    ],
)
def test_local_targets_bypass_the_proxy(url):
    """本机 / 内网一律 `trust_env=False`。

    macOS 上 httpx 的 `trust_env=True` 会读**系统代理设置**（不只是 HTTP_PROXY），
    开发机上挂着 Clash / Surge 时连 127.0.0.1 都会被塞进隧道——core 停着时拿到的
    就是代理的 502 空 body，而不是 ConnectError，「core 未启动」那句话永远出不来。
    实测过，不是假想。
    """
    assert wb.is_local_host(url) is True
    assert wb.WorkbenchClient(base_url=url).trust_env is False


@pytest.mark.parametrize("url", ["http://core.example.com:8000", "https://8.8.8.8"])
def test_remote_targets_still_honour_the_environment_proxy(url):
    """真部署在外网、中间隔着公司代理的 core 不能被误伤。"""
    assert wb.is_local_host(url) is False
    assert wb.WorkbenchClient(base_url=url).trust_env is True


# --------------------------------------------------------------- 信封错误映射


@respx.mock
def test_envelope_error_becomes_a_readable_tool_error():
    respx.get(f"{API}/review/itm_nope").mock(
        return_value=fail(404, "not_found", "内容项不存在: itm_nope")
    )

    with pytest.raises(ToolError) as excinfo:
        asyncio.run(wb.review_get("itm_nope"))

    text = str(excinfo.value)
    assert "not_found" in text
    assert "内容项不存在: itm_nope" in text  # 后端的中文原文照抄
    assert wb.ERROR_HINTS["not_found"] in text  # 外加"该怎么办"


@respx.mock
def test_error_detail_is_carried_through():
    """`invalid_slot` 的 detail 里有最近一个合法槽位——丢了它人就只能瞎猜。"""
    respx.post(f"{API}/content/itm_1/reschedule").mock(
        return_value=fail(
            422,
            "invalid_slot",
            "不是合法发布时刻",
            {"reason": "窗口", "suggested_slot_text": "08-17 09:00（Asia/Shanghai）"},
        )
    )

    with pytest.raises(ToolError) as excinfo:
        asyncio.run(wb.content_reschedule("itm_1", "2026-08-18T03:00:00Z"))

    assert "08-17 09:00（Asia/Shanghai）" in str(excinfo.value)


@respx.mock
def test_content_slots_forwards_read_only_query():
    route = respx.get(f"{API}/content/itm_1/slots", params={"count": "3"}).mock(
        return_value=ok({"item_id": "itm_1", "slots": []})
    )

    data = asyncio.run(wb.content_slots("itm_1", count=3))

    assert data == {"item_id": "itm_1", "slots": []}
    assert route.called


@respx.mock
def test_unknown_error_code_is_reported_as_contract_drift():
    respx.get(f"{API}/dashboard").mock(return_value=fail(500, "some_new_code", "出事了"))

    with pytest.raises(ToolError) as excinfo:
        asyncio.run(wb.dashboard())

    assert "契约漂移" in str(excinfo.value)


def test_every_documented_error_code_has_a_hint():
    """错误码表来自 docs/WORKBENCH_API.md 第 3 节；漏一个就会退化成"契约漂移"提示。"""
    documented = {
        line.split("`")[1]
        for line in (Path(wb.__file__).parent.parent / "docs" / "WORKBENCH_API.md")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.startswith("| ") and line.count("`") >= 2 and line.split("|")[1].strip().isdigit()
    }
    missing = documented - set(wb.ERROR_HINTS) - {"confirm_conflict"}
    assert missing == set(), f"这些错误码没有可读提示：{sorted(missing)}"


@respx.mock
def test_non_envelope_response_is_called_out():
    respx.get(f"{API}/dashboard").mock(return_value=httpx.Response(200, json={"hello": "world"}))

    with pytest.raises(ToolError) as excinfo:
        asyncio.run(wb.dashboard())

    assert "SW_MCP_BASE_URL" in str(excinfo.value)


# --------------------------------------------------------------- core 不在线


@respx.mock
def test_connection_refused_says_core_is_not_running():
    respx.get(f"{API}/dashboard").mock(side_effect=httpx.ConnectError("connection refused"))

    with pytest.raises(ToolError) as excinfo:
        asyncio.run(wb.dashboard())

    text = str(excinfo.value)
    assert "core 未启动" in text
    assert BASE in text  # 把它当前指向哪儿说清楚
    assert "SW_MCP_BASE_URL" in text


@respx.mock
def test_timeout_is_distinguished_from_being_offline():
    respx.get(f"{API}/dashboard").mock(side_effect=httpx.ReadTimeout("too slow"))

    with pytest.raises(ToolError) as excinfo:
        asyncio.run(wb.dashboard())

    assert "超时" in str(excinfo.value)
    assert "core 未启动" not in str(excinfo.value)


# ------------------------------------------------- 审核动作的人工确认（elicitation）
#
# 三条路：人接受 → 执行；人拒绝 / 取消 → 不执行；客户端不支持确认交互 → fail-closed。
# 这道闸门把 SW-AGENT.md R2（审核动作只在人明确指示后提交）从提示词级降到协议级。


@respx.mock
def test_accepted_elicitation_lets_the_approve_through():
    brief_route()
    route = respx.post(f"{API}/review/itm_1/approve").mock(return_value=ok({"item": {}}))
    ctx = FakeContext(answer="accept")

    result = asyncio.run(wb.review_approve("itm_1", reason="标题已收敛", ctx=ctx))

    assert route.called
    assert result == {"item": {}}
    # 确认文案得让人看清是哪一条、要干什么、后果是什么
    message = ctx.only_message
    assert "itm_1" in message
    assert "秋天第一杯奶茶" in message
    assert "review_approve" in message
    assert "批准 ≠ 发布" in message  # confirm_required 的账号仍要人点确认发布


@pytest.mark.parametrize(
    ("answer", "tool", "call"),
    [
        ("decline", "review_approve", lambda ctx: wb.review_approve("itm_1", ctx=ctx)),
        ("cancel", "review_approve", lambda ctx: wb.review_approve("itm_1", ctx=ctx)),
        ("decline", "review_reject", lambda ctx: wb.review_reject("itm_1", "太夸张", ctx=ctx)),
        (
            "decline",
            "review_edit",
            lambda ctx: wb.review_edit("itm_1", "新标题", "新正文", ctx=ctx),
        ),
    ],
)
@respx.mock
def test_declined_elicitation_executes_nothing(answer, tool, call):
    """人没点同意 = 一个写请求都不发，返回一句人话而不是抛错。"""
    brief_route()
    writes = [
        respx.post(f"{API}/review/itm_1/{verb}").mock(return_value=ok({"item": {}}))
        for verb in ("approve", "reject", "edit")
    ]
    ctx = FakeContext(answer=answer)

    result = asyncio.run(call(ctx))

    assert not any(route.called for route in writes), "拒绝之后仍然发了写请求"
    assert result["cancelled"] is True
    assert result["ok"] is False
    assert result["tool"] == tool
    assert result["elicitation"] == answer
    assert "操作已取消，未执行" in result["message"]


@pytest.mark.parametrize(
    ("ctx_factory", "declares_capability"),
    [
        pytest.param(lambda: None, False, id="没有 Context"),
        pytest.param(lambda: FakeContext(supports=False), False, id="客户端没声明能力"),
        pytest.param(
            lambda: FakeContext(raises=RuntimeError("no back-channel")),
            True,
            id="声明了却答不上来",
        ),
    ],
)
@respx.mock
def test_missing_elicitation_support_fails_closed(ctx_factory, declares_capability):
    """拿不到人的确认就**不执行**——不是静默放行。

    静默放行等于把 R2 退回提示词级约束，那还不如让人回工作台点一下。
    """
    brief = brief_route()
    writes = [
        respx.post(f"{API}/review/itm_1/{verb}").mock(return_value=ok({"item": {}}))
        for verb in ("approve", "reject", "edit")
    ]

    for call in (
        lambda ctx: wb.review_approve("itm_1", ctx=ctx),
        lambda ctx: wb.review_reject("itm_1", "太夸张", ctx=ctx),
        lambda ctx: wb.review_edit("itm_1", "新标题", "新正文", ctx=ctx),
    ):
        with pytest.raises(ToolError) as excinfo:
            asyncio.run(call(ctx_factory()))
        assert "不支持确认交互" in str(excinfo.value)
        assert "工作台" in str(excinfo.value)  # 告诉人去哪儿做这件事

    assert not any(route.called for route in writes), "不支持确认交互时仍然发了写请求"
    # 能力检查要早于摘要那次读：客户端根本弹不出确认，就别去 core 白跑一趟往返。
    # （声明了能力才失败的那一路例外——那时确认本来就要弹，摘要该读。）
    assert brief.called is declares_capability


def test_hermes_shaped_accept_survives_schema_validation():
    """hermes 的"同意"回的是 `ElicitResult(action="accept", content={})`。

    锚点：`hermes-agent/tools/mcp_tool.py:2302`——它的审批面是二选一按钮，不收表单
    字段。而 SDK 拿到 accept 之后会用确认表单去 `model_validate(content)`
    （`mcp/server/elicitation.py:133`），所以 `ReviewConfirmation` 里**多一个必填
    字段**就会让这一路抛 ValidationError，把"人已经点了同意"变成一次失败。
    这个断言就是那道栏杆。
    """
    from mcp.server.elicitation import elicit_with_validation
    from mcp.types import ElicitResult

    class _HermesShapedSession:
        async def elicit_form(self, message, requested_schema, related_request_id=None):
            self.requested_schema = requested_schema
            return ElicitResult(action="accept", content={})

    session = _HermesShapedSession()
    result = asyncio.run(elicit_with_validation(session, "确认？", wb.ReviewConfirmation))

    assert result.action == "accept"
    # 顺带钉住"零字段"：有了字段，hermes 那边的空 content 就分不清同意与漏填
    assert session.requested_schema["properties"] == {}


@respx.mock
def test_read_only_review_tools_have_no_confirmation_gate():
    """查队列 / 读稿不该弹确认——闸门只拦写动作，别把看一眼也变成打扰。"""
    respx.get(f"{API}/review").mock(return_value=ok({"items": [], "total": 0}))
    brief_route()
    respx.get(f"{API}/review/itm_1/records").mock(return_value=ok([]))

    asyncio.run(wb.review_list())
    asyncio.run(wb.review_get("itm_1"))


def test_only_the_three_review_mutations_are_gated():
    """闸门的适用面就是这三个，别悄悄扩大或缩小。"""
    import inspect

    gated = {
        name
        for name, fn in vars(wb).items()
        if not name.startswith("_")  # 闸门本身是私有函数，不算工具
        and inspect.iscoroutinefunction(fn)
        and "_require_human_confirmation" in (inspect.getsource(fn) or "")
    }
    assert gated == {"review_approve", "review_reject", "review_edit"}


# --------------------------------------------------------------- via sw-agent


@respx.mock
def test_write_tools_stamp_via_sw_agent_into_the_audit_actor():
    brief_route()
    route = respx.post(f"{API}/review/itm_1/approve").mock(return_value=ok({"item": {}}))

    asyncio.run(wb.review_approve("itm_1", reason="标题已收敛", ctx=FakeContext()))

    body = json.loads(route.calls.last.request.content)
    assert body["actor"].endswith(wb.AGENT_MARK)
    # 驳回理由会被改稿 Agent 当 prompt 读走，不许掺流程噪音
    assert body["reason"] == "标题已收敛"


@respx.mock
def test_operator_name_is_kept_alongside_the_marker():
    brief_route()
    route = respx.post(f"{API}/review/itm_1/reject").mock(return_value=ok({"item": {}}))

    asyncio.run(wb.review_reject("itm_1", reason="标题夸张", operator="张三", ctx=FakeContext()))

    body = json.loads(route.calls.last.request.content)
    assert body["actor"] == f"张三 {wb.AGENT_MARK}"


@respx.mock
def test_unreadable_item_does_not_block_the_confirmation():
    """core 抽风读不到摘要时，确认照弹（人至少知道要对哪个 id 动手）。"""
    respx.get(f"{API}/review/itm_1").mock(side_effect=httpx.ConnectError("refused"))
    route = respx.post(f"{API}/review/itm_1/approve").mock(return_value=ok({"item": {}}))
    ctx = FakeContext(answer="accept")

    asyncio.run(wb.review_approve("itm_1", ctx=ctx))

    assert "itm_1" in ctx.only_message
    assert route.called


@respx.mock
def test_topic_dismiss_also_carries_the_marker():
    route = respx.post(f"{API}/topics/top-1/dismiss").mock(return_value=ok({"id": "top-1"}))

    asyncio.run(wb.topic_dismiss("top-1", reason="旧闻"))

    assert wb.AGENT_MARK in json.loads(route.calls.last.request.content)["actor"]


# --------------------------------------------------------------- 载荷形状


@respx.mock
def test_list_payload_is_passed_through_verbatim():
    """列表工具返回结构化 JSON，字段名与契约一致——S5 的卡片按这些名字取值。"""
    payload = {
        "items": [{"id": "itm_1", "title": "标题", "status": "draft", "platform": "xhs"}],
        "total": 1,
        "limit": 50,
        "offset": 0,
    }
    respx.get(f"{API}/review").mock(return_value=ok(payload))

    assert asyncio.run(wb.review_list()) == payload


@respx.mock
def test_bare_array_endpoints_are_normalised_to_items():
    """`/accounts` 契约上回裸数组，这里统一包成 `{items,total}`，让列表形状只有一种。"""
    respx.get(f"{API}/accounts").mock(return_value=ok([{"id": "xhs-demo-01"}, {"id": "xhs-02"}]))

    result = asyncio.run(wb.accounts_list())

    assert result == {"items": [{"id": "xhs-demo-01"}, {"id": "xhs-02"}], "total": 2}


@respx.mock
def test_review_get_folds_in_the_publish_records():
    respx.get(f"{API}/review/itm_1").mock(return_value=ok({"item": {"id": "itm_1"}}))
    respx.get(f"{API}/review/itm_1/records").mock(return_value=ok([{"id": "pub_1"}]))

    result = asyncio.run(wb.review_get("itm_1"))

    assert result["item"]["id"] == "itm_1"
    assert result["records"] == [{"id": "pub_1"}]


@respx.mock
def test_review_get_can_skip_the_records_round_trip():
    respx.get(f"{API}/review/itm_1").mock(return_value=ok({"item": {"id": "itm_1"}}))
    records = respx.get(f"{API}/review/itm_1/records").mock(return_value=ok([]))

    result = asyncio.run(wb.review_get("itm_1", include_records=False))

    assert "records" not in result
    assert not records.called


@respx.mock
def test_jobs_query_routes_to_the_right_table_and_labels_it():
    respx.get(f"{API}/jobs/dead_letters").mock(return_value=ok({"items": [], "total": 0}))

    result = asyncio.run(wb.jobs_query("dead_letters"))

    assert result["kind"] == "dead_letters"


@respx.mock
def test_system_info_degrades_one_block_without_failing_the_call():
    """四块里有一块探不到就只降级那一块——不能因为 telegram 没配就问不出 core 版本。"""
    respx.get(f"{API}/system/info").mock(return_value=ok({"version": "0.1.0"}))
    respx.get(f"{API}/system/imagegen").mock(return_value=ok({"ready": False}))
    respx.get(f"{API}/system/telegram").mock(return_value=fail(500, "tick_failed", "炸了"))
    respx.get(f"{API}/system/ticks").mock(return_value=ok({"ticks": []}))

    result = asyncio.run(wb.system_info())

    assert result["info"]["version"] == "0.1.0"
    assert "error" in result["telegram"]


@respx.mock
def test_system_info_still_fails_loudly_when_core_is_down():
    respx.get(f"{API}/system/info").mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(ToolError) as excinfo:
        asyncio.run(wb.system_info())

    assert "core 未启动" in str(excinfo.value)


# --------------------------------------------------------------- 二维码 = 图片块


@respx.mock
def test_login_qrcode_returns_an_mcp_image_block():
    respx.get(f"{API}/accounts/xhs-demo-01/login/qrcode").mock(
        return_value=ok(
            {
                "account_id": "xhs-demo-01",
                "platform": "xhs",
                "image_base64": TINY_PNG_B64,
                "status": "ok",
                "account_status": "ok",
                "placeholder": False,
                "expires_in": 120,
            }
        )
    )

    blocks = asyncio.run(wb.account_login_qrcode("xhs-demo-01"))

    image, text = blocks
    assert isinstance(image, Image)
    # 断言**线上形状**而不是 python 属性名：S5 的渲染器读到的是这一份
    # （`{type:'image', data, mimeType}` 原样到达 ToolResultNode.content）。
    wire = image.to_image_content().model_dump(by_alias=True, exclude_none=True)
    assert wire["type"] == "image"
    assert wire["mimeType"] == "image/png"
    assert base64.b64decode(wire["data"]) == base64.b64decode(TINY_PNG_B64)

    meta = json.loads(text)
    # 几 KB 的 base64 不该在文本块里再来一份
    assert "image_base64" not in meta
    assert meta["account_id"] == "xhs-demo-01"
    assert "120" in meta["hint"]  # 过期倒计时说明


@respx.mock
def test_placeholder_qrcode_is_flagged_as_unscannable():
    respx.get(f"{API}/accounts/xhs-demo-01/login/qrcode").mock(
        return_value=ok({"image_base64": TINY_PNG_B64, "placeholder": True, "expires_in": 120})
    )

    _image, text = asyncio.run(wb.account_login_qrcode("xhs-demo-01"))

    assert "扫不了" in json.loads(text)["hint"]


@respx.mock
def test_missing_qrcode_image_is_a_readable_error():
    respx.get(f"{API}/accounts/xhs-demo-01/login/qrcode").mock(
        return_value=ok({"image_base64": "", "detail": "sidecar 没就绪"})
    )

    with pytest.raises(ToolError) as excinfo:
        asyncio.run(wb.account_login_qrcode("xhs-demo-01"))

    assert "sidecar 没就绪" in str(excinfo.value)
