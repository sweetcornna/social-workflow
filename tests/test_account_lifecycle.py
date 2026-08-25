"""P10：账号全生命周期（建 / 改 / 停 / sidecar / 手动出稿）。

三条主线：

1. **台账与 DB 不许漂移**——每个写操作之后 ``python -m core.accounts check`` 都必须过；
2. **docker 一律 mock**——本机与 CI 都没有 docker daemon，真实运行路径由部署时实测，
   这里只钉住"命令拼成什么样"（一账号一容器一 volume 一端口、volume 挂 /app/data）；
3. **失败态要说人话**——窗口写错、抖音缺 identity_hint、sidecar 起不来、出稿超额，
   每一种都得有明确的 ``code`` 和一句能直接贴给运营看的中文。
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta

import pytest
import yaml

from core import accounts_file, db, sidecars
from core.accounts import accounts_file_path, diff_report, load_specs
from core.models import Account, ContentItem, new_id, utcnow
from core.state_machine import AccountStatus, ContentStatus
from tests.conftest import make_account, make_item

LEDGER_WITH_COMMENTS = """\
# 账号台账（示例）。凭据不写在这里。
#
# 入库：uv run python -m core.accounts sync
accounts:
  - id: xhs-demo-01
    platform: xhs
    name: 小红书测试号 01
    # 平台侧限频保守值
    daily_limit: 10
    sidecar:
      port: 18060
      volume: xhs_data_demo01
      token_env: XHS_TOKEN_XHS_DEMO_01

  - id: wechat-demo-01
    platform: wechat_mp
    name: 公众号测试号 01
    daily_limit: 1
"""


# ------------------------------------------------------------------ 工具


def _data(resp) -> dict:
    body = resp.json()
    assert body["ok"] is True, body
    return body["data"]


def _error(resp) -> dict:
    body = resp.json()
    assert body["ok"] is False, body
    return body["error"]


def _ledger_text() -> str:
    return accounts_file_path().read_text(encoding="utf-8")


def _write_ledger(text: str) -> None:
    accounts_file_path().write_text(text, encoding="utf-8")


def _assert_no_drift() -> None:
    """等价于 ``python -m core.accounts check`` 退出码 0。"""
    specs = load_specs()
    with db.session_scope() as session:
        report = diff_report(session, specs)
    assert report.changed == 0, (
        f"台账与 DB 漂移了：待新建 {report.created}，待更新 {report.updated}"
    )


def _create_xhs(client, **overrides) -> dict:
    payload = {
        "platform": "xhs",
        "name": "小红书测试号 03",
        "publish_windows": ["12:00-14:00", "19:00-22:30"],
        "min_interval_minutes": 90,
        "daily_limit": 10,
        "daily_target": 1,
    }
    payload.update(overrides)
    resp = client.post("/api/v1/accounts", json=payload)
    assert resp.status_code == 201, resp.text
    return _data(resp)


# ------------------------------------------------------------ 台账文件读写


def test_ledger_round_trip_keeps_every_byte():
    doc = accounts_file.parse_document(LEDGER_WITH_COMMENTS)
    assert doc.ids() == ["xhs-demo-01", "wechat-demo-01"]
    assert doc.render() == LEDGER_WITH_COMMENTS


def test_ledger_upsert_only_rewrites_the_touched_entry():
    doc = accounts_file.parse_document(LEDGER_WITH_COMMENTS)
    entry = dict(doc.find("wechat-demo-01").raw)
    entry["daily_target"] = 1
    doc.upsert(entry)
    text = doc.render()
    # 别人的旁注一个字都不能少
    assert "# 平台侧限频保守值" in text
    assert "# 账号台账（示例）。凭据不写在这里。" in text
    assert yaml.safe_load(text)["accounts"][1]["daily_target"] == 1


def test_ledger_append_keeps_a_blank_line_between_entries():
    doc = accounts_file.parse_document(LEDGER_WITH_COMMENTS)
    doc.upsert({"id": "xhs-99", "platform": "xhs", "name": "新号", "daily_limit": 5})
    text = doc.render()
    assert "\n\n  - id: xhs-99\n" in text
    assert len(yaml.safe_load(text)["accounts"]) == 3


def test_ledger_without_accounts_key_is_refused():
    with pytest.raises(accounts_file.LedgerError):
        accounts_file.parse_document("# 只有注释，没有 accounts:\n")


def test_allocate_id_does_not_double_the_platform_prefix():
    """名字里已经带了平台前缀就别再套一层：xhs-xhs-主号 这种 id 没人念得出来。"""
    from core.account_admin import allocate_id

    assert allocate_id("xhs", "xhs-main", set()) == "xhs-main"
    assert allocate_id("xhs", "main", set()) == "xhs-main"
    # 撞车往后加序号，而不是覆盖别人
    assert allocate_id("xhs", "main", {"xhs-main"}) == "xhs-main-02"
    # 中文名 slug 是空的（常态）→ 回落到两位序号
    assert allocate_id("douyin", "抖音主号", set()) == "douyin-01"
    assert allocate_id("wechat_mp", "公众号", set()) == "wechat-01"


def test_ledger_declared_ports():
    doc = accounts_file.parse_document(LEDGER_WITH_COMMENTS)
    assert accounts_file.declared_ports(doc) == {18060}


# ------------------------------------------------------------------ 新建


def test_create_writes_ledger_then_db_without_drift(client):
    _write_ledger(LEDGER_WITH_COMMENTS)
    data = _create_xhs(client)

    account_id = data["account"]["id"]
    assert account_id == "xhs-03", "中文名 slug 不出来，应回落到平台前缀 + 序号"
    assert data["account"]["platform"] == "xhs"
    assert data["account"]["policy"]["publish_windows"] == "12:00-14:00、19:00-22:30"
    assert data["account"]["policy"]["min_interval_minutes"] == 90

    # 台账里真的多了一条，而且别人的注释没被改花
    text = _ledger_text()
    assert "# 平台侧限频保守值" in text
    assert f"id: {account_id}" in text

    # DB 里也有，且两边一致（= `python -m core.accounts check` 通过）
    with db.session_scope() as session:
        row = session.get(Account, account_id)
        assert row is not None and row.status == AccountStatus.OK
        assert row.sidecar_endpoint == "http://localhost:18061"
    _assert_no_drift()


def test_create_allocates_the_next_free_port(client):
    """一账号一端口：台账里占了 18060，新号必须往后挪。"""
    _write_ledger(LEDGER_WITH_COMMENTS)
    first = _create_xhs(client, name="a")
    second = _create_xhs(client, name="b")
    ports = {
        first["account"]["sidecar_endpoint"],
        second["account"]["sidecar_endpoint"],
    }
    assert len(ports) == 2, "两个账号不许共用端口"
    assert "http://localhost:18060" not in ports, "18060 已被台账占用"


def test_create_reports_sidecar_not_attached_under_none_driver(client):
    data = _create_xhs(client)
    assert any("sidecar 未接入" in w for w in data["warnings"]), data["warnings"]

    state = _data(client.get(f"/api/v1/accounts/{data['account']['id']}/sidecar"))
    assert state["driver"] == "none"
    assert state["state"] == "none-driver"
    assert "SW_SIDECAR_DRIVER=none" in state["detail"]


def test_create_rejects_a_broken_window_with_an_example(client):
    resp = client.post(
        "/api/v1/accounts",
        json={"platform": "xhs", "name": "坏窗口", "publish_windows": ["09:00~11:00"]},
    )
    assert resp.status_code == 422
    err = _error(resp)
    assert err["code"] == "invalid_window"
    assert "09:00-11:00" in err["message"], "报错必须带上正确写法的例子"
    assert err["detail"]["example"] == ["09:00-11:00", "19:00-22:30"]
    assert load_specs() == [], "校验失败时不许留下半条台账"


def test_create_douyin_requires_identity_hint(client):
    resp = client.post("/api/v1/accounts", json={"platform": "douyin", "name": "抖音号"})
    assert resp.status_code == 422
    assert _error(resp)["code"] == "identity_hint_required"

    data = _data(
        client.post(
            "/api/v1/accounts",
            json={"platform": "douyin", "name": "抖音号", "identity_hint": "抖音测试号 09"},
        )
    )
    assert data["account"]["extra"]["identity_hint"] == "抖音测试号 09"
    assert data["account"]["id"].startswith("douyin-")
    _assert_no_drift()


def test_create_refuses_a_daily_limit_above_the_platform_ceiling(client):
    resp = client.post(
        "/api/v1/accounts",
        json={"platform": "douyin", "name": "x", "identity_hint": "x", "daily_limit": 99},
    )
    assert resp.status_code == 422
    err = _error(resp)
    assert err["code"] == "limit_above_ceiling"
    assert err["detail"]["ceiling"] == 10


def test_create_rejects_an_unknown_platform(client):
    resp = client.post("/api/v1/accounts", json={"platform": "weibo", "name": "x"})
    assert resp.status_code == 422
    assert _error(resp)["code"] == "invalid_platform"


def test_create_rejects_an_unknown_timezone(client):
    resp = client.post(
        "/api/v1/accounts", json={"platform": "xhs", "name": "x", "timezone": "Mars/Olympus"}
    )
    assert resp.status_code == 422
    assert _error(resp)["code"] == "invalid_timezone"


def test_ledger_is_rolled_back_when_the_db_sync_blows_up(client, monkeypatch):
    """写文件成功、写库失败 → 台账必须回到动手之前的字节，否则下次 check 就红。"""
    _write_ledger(LEDGER_WITH_COMMENTS)
    before = _ledger_text()

    def boom(*_args, **_kwargs):
        raise RuntimeError("模拟同步失败")

    monkeypatch.setattr("core.account_admin.sync_accounts", boom)
    with pytest.raises(RuntimeError):
        client.post("/api/v1/accounts", json={"platform": "xhs", "name": "会失败的号"})
    assert _ledger_text() == before


# ------------------------------------------------------------------ 修改


def test_patch_updates_both_sides(client):
    _write_ledger(LEDGER_WITH_COMMENTS)
    account_id = _create_xhs(client)["account"]["id"]

    data = _data(
        client.patch(
            f"/api/v1/accounts/{account_id}",
            json={"name": "改过名的号", "daily_target": 3, "publish_windows": ["08:00-09:00"]},
        )
    )
    assert data["account"]["name"] == "改过名的号"
    assert data["account"]["policy"]["daily_target"] == 3
    assert data["account"]["policy"]["publish_windows"] == "08:00-09:00"

    entry = accounts_file.read_document(accounts_file_path()).find(account_id).raw
    assert entry["name"] == "改过名的号"
    assert entry["publish_windows"] == ["08:00-09:00"]
    _assert_no_drift()


def test_patch_leaves_untouched_fields_alone(client):
    account_id = _create_xhs(client)["account"]["id"]
    data = _data(client.patch(f"/api/v1/accounts/{account_id}", json={"daily_target": 2}))
    assert data["account"]["name"] == "小红书测试号 03"
    assert data["account"]["policy"]["min_interval_minutes"] == 90
    assert data["account"]["policy"]["publish_windows"] == "12:00-14:00、19:00-22:30"


def test_patch_cannot_change_platform_or_id(client):
    account_id = _create_xhs(client)["account"]["id"]
    data = _data(
        client.patch(
            f"/api/v1/accounts/{account_id}",
            # 这两个字段不在 schema 里，pydantic 会直接忽略——但结果必须没变
            json={"platform": "douyin", "id": "hacked", "name": "还是我"},
        )
    )
    assert data["account"]["id"] == account_id
    assert data["account"]["platform"] == "xhs"


def test_patch_backfills_an_account_that_was_never_in_the_ledger(client):
    """库里有、台账里没有（老库 / dev seed）：改一次就顺手把漂移修掉。"""
    _write_ledger(LEDGER_WITH_COMMENTS)
    with db.session_scope() as session:
        make_account(session, account_id="acc-legacy", platform="xhs", daily_limit=5)

    _data(client.patch("/api/v1/accounts/acc-legacy", json={"daily_target": 1}))
    assert "acc-legacy" in accounts_file.read_document(accounts_file_path()).ids()
    _assert_no_drift()


def test_patch_404_for_unknown_account(client):
    assert client.patch("/api/v1/accounts/nope", json={"daily_target": 1}).status_code == 404


# ------------------------------------------------------------- 停用 / 启用


def test_deactivate_suspends_scheduled_items_and_reactivate_puts_them_back(client):
    with db.session_scope() as session:
        account = make_account(session, account_id="acc-off", platform="xhs")
        item = make_item(session, account, status=ContentStatus.SCHEDULED.value)
        item_id = item.id

    data = _data(client.post("/api/v1/accounts/acc-off/deactivate", json={"reason": "先关一阵"}))
    assert data["account"]["status"] == "suspended"
    assert "挂起" in data["message"]
    with db.session_scope() as session:
        assert session.get(ContentItem, item_id).status == ContentStatus.SUSPENDED

    data = _data(client.post("/api/v1/accounts/acc-off/reactivate", json={}))
    assert data["account"]["status"] == "ok"
    with db.session_scope() as session:
        assert session.get(ContentItem, item_id).status == ContentStatus.SCHEDULED


def test_deactivate_is_idempotent_and_keeps_history(client):
    with db.session_scope() as session:
        account = make_account(session, account_id="acc-off2", platform="xhs")
        make_item(session, account, status=ContentStatus.PUBLISHED.value)

    client.post("/api/v1/accounts/acc-off2/deactivate", json={})
    again = _data(client.post("/api/v1/accounts/acc-off2/deactivate", json={}))
    assert again["account"]["status"] == "suspended"
    # 不硬删：历史内容还挂在这个账号上
    with db.session_scope() as session:
        assert session.get(Account, "acc-off2") is not None
        assert session.query(ContentItem).filter(ContentItem.account_id == "acc-off2").count() == 1


def test_banned_account_cannot_be_deactivated(client):
    with db.session_scope() as session:
        make_account(session, account_id="acc-banned", platform="xhs", status="banned")
    resp = client.post("/api/v1/accounts/acc-banned/deactivate", json={})
    assert resp.status_code == 409
    assert _error(resp)["code"] == "illegal_transition"


def test_login_health_skips_a_suspended_account():
    """人明确关掉的号，巡检不许偷偷把它打开。"""
    from publishers.registry import use_fake_publishers
    from publishers.xhs.login import check_and_mark

    use_fake_publishers()
    with db.session_scope() as session:
        account = make_account(session, account_id="acc-sus", platform="xhs", status="suspended")
        # 停用的号根本不巡检：返回 None 而不是编一个假的健康结果
        assert check_and_mark(session, account) is None
        assert account.status == "suspended"


# ------------------------------------------------------------------ sidecar


class _FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def docker_calls(monkeypatch):
    """把 ``docker`` CLI 换成录音机。本机与 CI 都没有 daemon，绝不真调。"""
    calls: list[list[str]] = []
    replies: list[_FakeProc] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return replies.pop(0) if replies else _FakeProc()

    monkeypatch.setattr(sidecars.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(sidecars.subprocess, "run", fake_run)
    return calls, replies


def _xhs_account(account_id: str = "acc-xhs") -> Account:
    with db.session_scope() as session:
        account = make_account(session, account_id=account_id, platform="xhs")
        account.sidecar_endpoint = "http://localhost:18060"
        account.extra = {"xhs": {"auth_token_env": "XHS_TOKEN_ACC_XHS"}}
        session.flush()
        session.expunge(account)
    return account


def test_docker_driver_run_args_follow_the_one_account_one_container_rule(docker_calls):
    calls, replies = docker_calls
    replies.append(_FakeProc(1, stderr="Error: No such object: sw-xhs-acc-xhs"))  # inspect
    replies.append(_FakeProc(0, stdout="deadbeef"))  # run

    account = _xhs_account()
    message = sidecars.DockerDriver().start(account)
    assert "已创建并启动容器" in message

    run_args = calls[-1]
    assert run_args[:3] == ["docker", "run", "-d"]
    joined = " ".join(run_args)
    assert "--name sw-xhs-acc-xhs" in joined
    assert "--restart unless-stopped" in joined
    # 只绑回环：sidecar 不对外暴露
    assert "127.0.0.1:18060:18060" in joined
    # volume 必须挂到 /app/data —— cookies 在那儿，挂错地方每次重启都要重扫码
    assert "swxhs_acc-xhs:/app/data" in joined
    assert "COOKIES_PATH=/app/data/cookies.json" in joined
    assert run_args[-1] == "xpzouying/xiaohongshu-mcp:v2.5.0"


def test_docker_driver_passes_the_token_by_name_never_by_value(docker_calls, monkeypatch):
    calls, replies = docker_calls
    replies.append(_FakeProc(1, stderr="No such object"))
    replies.append(_FakeProc(0))
    monkeypatch.setenv("XHS_TOKEN_ACC_XHS", "s3cr3t-token")

    sidecars.DockerDriver().start(_xhs_account())
    joined = " ".join(calls[-1])
    assert "-e AUTH_TOKEN" in joined
    assert "s3cr3t-token" not in joined, "token 绝不能出现在命令行参数里"


def test_docker_driver_starts_an_existing_stopped_container(docker_calls):
    calls, replies = docker_calls
    replies.append(_FakeProc(0, stdout="exited"))
    replies.append(_FakeProc(0))

    message = sidecars.DockerDriver().start(_xhs_account())
    assert "不用重新扫码" in message
    assert calls[-1][1:] == ["start", "sw-xhs-acc-xhs"]


def test_docker_driver_recreate_keeps_the_volume(docker_calls):
    calls, replies = docker_calls
    replies.append(_FakeProc(0))  # rm -f
    replies.append(_FakeProc(0))  # run

    message = sidecars.DockerDriver().recreate(_xhs_account())
    assert "volume 未动" in message
    assert calls[0][1:3] == ["rm", "-f"]
    assert not any("volume" in arg and "rm" in call for call in calls for arg in call[:2]), (
        "重建绝不许删 volume"
    )


def test_docker_driver_missing_binary_says_so(monkeypatch):
    monkeypatch.setattr(sidecars.shutil, "which", lambda _name: None)
    with pytest.raises(sidecars.SidecarError, match="找不到"):
        sidecars.DockerDriver().probe(_xhs_account())


def test_docker_driver_timeout_says_so(monkeypatch):
    monkeypatch.setattr(sidecars.shutil, "which", lambda _name: "/usr/bin/docker")

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="docker", timeout=1)

    monkeypatch.setattr(sidecars.subprocess, "run", timeout)
    with pytest.raises(sidecars.SidecarError, match="秒没返回"):
        sidecars.DockerDriver().probe(_xhs_account())


def test_health_probe_never_goes_through_a_proxy(monkeypatch):
    """sidecar 永远在回环地址上，而部署机上常配着 HTTP_PROXY。

    走代理去连 127.0.0.1 会拿回代理自己的 502，把"容器没起来"和"代理拦了"
    混成同一句话——这条断言就是防那个的（本机实测踩过）。
    """
    import httpx

    seen: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def get(self, url):
            raise httpx.ConnectError(f"refused: {url}")

    monkeypatch.setattr(httpx, "Client", FakeClient)
    payload, why = sidecars.probe_health("http://127.0.0.1:18060")
    assert seen["trust_env"] is False
    assert payload is None
    assert "连不上 http://127.0.0.1:18060/health" in why


def test_sidecar_endpoint_is_501_for_non_xhs(client):
    with db.session_scope() as session:
        make_account(session, account_id="acc-dy", platform="douyin")
    resp = client.get("/api/v1/accounts/acc-dy/sidecar")
    assert resp.status_code == 501
    assert _error(resp)["code"] == "not_supported"


def test_sidecar_action_under_none_driver_is_a_502_with_a_reason(client):
    with db.session_scope() as session:
        make_account(session, account_id="acc-x", platform="xhs")
    resp = client.post("/api/v1/accounts/acc-x/sidecar/start")
    assert resp.status_code == 502
    err = _error(resp)
    assert err["code"] == "sidecar_error"
    assert "SW_SIDECAR_DRIVER=none" in err["message"]


def test_sidecar_unknown_action_is_422(client):
    with db.session_scope() as session:
        make_account(session, account_id="acc-x2", platform="xhs")
    resp = client.post("/api/v1/accounts/acc-x2/sidecar/nuke")
    assert resp.status_code == 422
    assert _error(resp)["code"] == "unknown_action"


def test_port_allocator_skips_taken_ports():
    assert sidecars.allocate_port({18060, 18061}, base=18060) >= 18062


# ------------------------------------------------------------------ 出一条稿


def test_generate_produces_a_review_item(client):
    account_id = _create_xhs(client)["account"]["id"]
    data = _data(client.post(f"/api/v1/accounts/{account_id}/generate", json={"topic": "租房收纳"}))
    assert data["content_item_id"]
    assert data["status"] == ContentStatus.DRAFT.value
    assert data["selected_topic"] == "租房收纳"
    assert data["llm"] == "scripted", "没配凭据时必须如实标明是预置文案"
    assert "ScriptedLLM" in data["message"]
    assert data["used_today"] == 1

    with db.session_scope() as session:
        item = session.get(ContentItem, data["content_item_id"])
        assert item is not None and item.account_id == account_id


def test_generate_is_capped_per_day(client):
    """连点两下就是几万 token：daily_target=1 时最多手动出 2 条。"""
    account_id = _create_xhs(client, daily_target=1)["account"]["id"]
    with db.session_scope() as session:
        for _ in range(2):
            session.add(
                ContentItem(
                    id=new_id("itm"),
                    account_id=account_id,
                    status=ContentStatus.DRAFT.value,
                    bundle_json={},
                    created_at=utcnow(),
                )
            )

    resp = client.post(f"/api/v1/accounts/{account_id}/generate", json={})
    assert resp.status_code == 429
    err = _error(resp)
    assert err["code"] == "generate_limit"
    assert err["detail"] == {"used_today": 2, "cap": 2}


def test_generate_ignores_yesterdays_items(client):
    """闸门按 UTC 日切，昨天的稿子不该占今天的额度。"""
    account_id = _create_xhs(client, daily_target=1)["account"]["id"]
    with db.session_scope() as session:
        session.add(
            ContentItem(
                id=new_id("itm"),
                account_id=account_id,
                status=ContentStatus.DRAFT.value,
                bundle_json={},
                created_at=datetime.now(UTC) - timedelta(days=2),
            )
        )
    data = _data(client.post(f"/api/v1/accounts/{account_id}/generate", json={"topic": "x"}))
    assert data["used_today"] == 1


def test_generate_with_an_empty_topic_pool_says_it_can_be_fixed_by_naming_one(client, monkeypatch):
    """选题池空是**可救回来**的失败：报错里必须说清楚"直接指定一个选题标题"这条路。

    前端据此把输入框推到人面前（见 ui/components/accounts/account-actions.tsx），
    所以这句话的措辞是契约的一部分，不能随手改。
    """
    account_id = _create_xhs(client)["account"]["id"]
    # 采集器在测试机上会真去拉 douyin 归档（拉得到就不空了），这里显式清空
    monkeypatch.setattr("core.dev_flow.collect_topics", lambda *_a, **_k: 0)
    monkeypatch.setattr("core.dev_flow.load_candidates", lambda *_a, **_k: ([], []))

    resp = client.post(f"/api/v1/accounts/{account_id}/generate", json={})
    assert resp.status_code == 409
    err = _error(resp)
    assert err["code"] == "generation_failed"
    assert "选题池" in err["message"]
    assert "指定一个选题标题" in err["message"]

    # 给了题目就能出来
    data = _data(client.post(f"/api/v1/accounts/{account_id}/generate", json={"topic": "租房收纳"}))
    assert data["content_item_id"]


def test_generate_maps_llm_failures_to_a_502_envelope(client, monkeypatch):
    """模型 / 网关炸了必须是带 envelope 的 502，不是裸 500。

    2026-08-17 生产事故：选题调用被 max_tokens 掐断抛 ``LLMAPIError``，而 ``_run_pipeline``
    只 catch ``DevFlowError``，异常一路穿透成 FastAPI 的裸 500——前端拿不到 ``code``，
    只能吐一句通用错误。异常原文要留在 message 里，否则运营没法判断是网关还是模型。
    """
    from generation.llm import LLMAPIError

    account_id = _create_xhs(client)["account"]["id"]

    def boom(*_a, **_k):
        raise LLMAPIError("dsh 输出被 max_tokens 截断且没有任何内容")

    monkeypatch.setattr("core.dev_flow.run_xhs_pipeline", boom)
    resp = client.post(f"/api/v1/accounts/{account_id}/generate", json={"topic": "x"})
    assert resp.status_code == 502
    err = _error(resp)
    assert err["code"] == "llm_failed"
    assert "重试" in err["message"] and "preflight" in err["message"]
    assert "max_tokens 截断" in err["message"], "异常原文不能被吞掉"


def test_generate_still_maps_pipeline_failures_to_409(client, monkeypatch):
    """新增的 LLM 分支不许把 ``DevFlowError`` 的既有契约抢走（前端按 409 做就地补救）。"""
    account_id = _create_xhs(client)["account"]["id"]

    def boom(*_a, **_k):
        from core.dev_flow import DevFlowError

        raise DevFlowError("选题池是空的：这次直接指定一个选题标题。")

    monkeypatch.setattr("core.dev_flow.run_xhs_pipeline", boom)
    resp = client.post(f"/api/v1/accounts/{account_id}/generate", json={})
    assert resp.status_code == 409
    assert _error(resp)["code"] == "generation_failed"


def test_generate_refuses_a_suspended_account(client):
    with db.session_scope() as session:
        make_account(session, account_id="acc-sus2", platform="xhs", status="suspended")
    resp = client.post("/api/v1/accounts/acc-sus2/generate", json={"topic": "x"})
    assert resp.status_code == 409
    assert _error(resp)["code"] == "account_suspended"


def test_generate_stops_when_the_token_budget_is_gone(client, monkeypatch):
    account_id = _create_xhs(client)["account"]["id"]
    monkeypatch.setattr("core.budget.BudgetGuard.is_exhausted", lambda *_a, **_k: True)
    resp = client.post(f"/api/v1/accounts/{account_id}/generate", json={"topic": "x"})
    assert resp.status_code == 429
    assert _error(resp)["code"] == "budget_exhausted"


def test_generate_wechat_without_credentials_says_what_is_missing(client):
    data = _create_xhs(client, platform="wechat_mp", name="公众号", daily_limit=1)
    resp = client.post(f"/api/v1/accounts/{data['account']['id']}/generate", json={"topic": "x"})
    assert resp.status_code == 503
    err = _error(resp)
    assert err["code"] == "credentials_missing"
    assert "WECHAT_APPID" in err["message"]
    assert "40164" in err["message"]


def test_generate_douyin_without_a_render_service_says_so(client, monkeypatch):
    data = _data(
        client.post(
            "/api/v1/accounts",
            json={"platform": "douyin", "name": "抖音号", "identity_hint": "抖音号"},
        )
    )
    monkeypatch.setenv("MPT_BASE_URL", "")
    from core.config import reload_settings

    reload_settings()
    resp = client.post(f"/api/v1/accounts/{data['account']['id']}/generate", json={"topic": "x"})
    assert resp.status_code == 503
    err = _error(resp)
    assert err["code"] == "render_unavailable"
    assert "MPT_BASE_URL" in err["message"]


def test_generate_404_for_unknown_account(client):
    assert client.post("/api/v1/accounts/nope/generate", json={}).status_code == 404


# ------------------------------------------------------------- 静态资源缓存头


def test_workbench_html_is_no_cache_and_hashed_assets_are_immutable(client, tmp_path, monkeypatch):
    """重部署后旧 HTML 引用已删 chunk 会把页面挂死，缓存头是那道防线。"""
    dist = tmp_path / "dist"
    (dist / "_next" / "static" / "chunks").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>x</title>", encoding="utf-8")
    (dist / "_next" / "static" / "chunks" / "main-abc123.js").write_text("//", encoding="utf-8")

    monkeypatch.setenv("SW_UI_DIST", str(dist))
    from fastapi.testclient import TestClient

    from core.main import create_app

    with TestClient(create_app()) as fresh:
        html = fresh.get("/workbench/")
        assert html.status_code == 200
        assert html.headers["cache-control"] == "no-cache"

        asset = fresh.get("/workbench/_next/static/chunks/main-abc123.js")
        assert asset.status_code == 200
        assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_workbench_placeholder_is_also_no_cache(client, tmp_path, monkeypatch):
    monkeypatch.setenv("SW_UI_DIST", str(tmp_path / "never-built"))
    from fastapi.testclient import TestClient

    from core.main import create_app

    with TestClient(create_app()) as fresh:
        resp = fresh.get("/workbench/")
        assert resp.status_code == 200
        assert "工作台" in resp.text
        assert resp.headers["cache-control"] == "no-cache"


# --------------------------------------------------------------- 配图（P11）


def test_generate_accepts_illustration_count(client):
    """``illustrations`` 进 body；生图没接通时如实降级为 0 张 + warning。"""
    account_id = _create_xhs(client)["account"]["id"]
    data = _data(
        client.post(
            f"/api/v1/accounts/{account_id}/generate",
            json={"topic": "租房收纳", "illustrations": 2},
        )
    )
    # conftest 里 SW_IMAGEGEN_ENABLED=false，所以这里必然走降级路径
    assert data["illustrations"] == 0
    assert any("没有生成配图" in w for w in data["warnings"])
    # 红线：配图配不上不许阻塞出稿
    assert data["content_item_id"]


def test_generate_rejects_absurd_illustration_counts(client):
    """手滑传个 999 会把当日生图预算一次烧光，必须在入口挡住。"""
    account_id = _create_xhs(client)["account"]["id"]
    resp = client.post(
        f"/api/v1/accounts/{account_id}/generate", json={"topic": "x", "illustrations": 999}
    )
    assert resp.status_code == 422
    resp = client.post(
        f"/api/v1/accounts/{account_id}/generate", json={"topic": "x", "illustrations": -1}
    )
    assert resp.status_code == 422


def test_system_imagegen_reports_honest_reason_when_off(client):
    """开关关掉时 ``ready=false`` 且 ``reason`` 非空——前端要原样显示它。"""
    data = _data(client.get("/api/v1/system/imagegen"))
    assert data["ready"] is False
    assert "SW_IMAGEGEN_ENABLED=false" in data["reason"]
    assert data["model"] == "gpt-image-2"
    assert data["daily_limit"] == 10.0
    assert data["used_today"] == 0.0
    assert data["remaining"] == 10.0


def test_system_imagegen_reports_ready_and_usage(client, monkeypatch):
    """开着的时候要报出今日已用张数，前端拿它显示"今天还能配几张"。"""
    from core import db
    from core.budget import BudgetGuard, CostKind
    from core.config import reload_settings

    monkeypatch.setenv("SW_IMAGEGEN_ENABLED", "auto")
    reload_settings()
    with db.session_scope() as session:
        BudgetGuard(session).charge(CostKind.IMAGES, 3, meta={"purpose": "test"})

    data = _data(client.get("/api/v1/system/imagegen"))
    assert data["ready"] is True
    assert data["reason"] == ""
    assert data["has_api_key"] is True
    assert (data["used_today"], data["remaining"]) == (3.0, 7.0)
    assert data["default_count"] == 2


def test_system_imagegen_never_spends_money(client, monkeypatch):
    """这个端点在页面加载路径上，绝不能因为探测把出稿的钱花掉。

    所以它连生图客户端都不该造出来——只看配置、熔断标记和今天的账本。
    """
    from core.config import reload_settings
    from generation import imagegen as imagegen_mod

    monkeypatch.setenv("SW_IMAGEGEN_ENABLED", "auto")
    reload_settings()

    def explode(*args, **kwargs):
        raise AssertionError("system/imagegen 不该真调生图")

    monkeypatch.setattr(imagegen_mod.ImagegenClient, "generate", explode)
    monkeypatch.setattr(imagegen_mod, "build_imagegen", explode)
    assert _data(client.get("/api/v1/system/imagegen"))["ready"] is True
