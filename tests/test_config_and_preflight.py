"""配置归并（wenyan 键 / 弃用别名）与 P4 新增的门禁检查项。"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from core import db
from core.config import (
    DEPRECATED_ENV_ALIASES,
    Settings,
    deprecated_env_aliases,
    log_deprecated_env_aliases,
    reload_settings,
)
from tests.conftest import make_account

# ------------------------------------------------------------ wenyan 配置归并


def test_wenyan_node_bin_is_the_canonical_name(monkeypatch):
    monkeypatch.setenv("WENYAN_NODE_BIN", "/opt/node/bin/npx")
    settings = reload_settings()
    assert settings.wenyan_node_bin == "/opt/node/bin/npx"
    # node_bin 是只读别名属性，老代码继续可用
    assert settings.node_bin == "/opt/node/bin/npx"


def test_legacy_node_bin_still_works(monkeypatch):
    """归并不能把已有部署打崩：NODE_BIN 仍然生效。"""
    monkeypatch.delenv("WENYAN_NODE_BIN", raising=False)
    monkeypatch.setenv("NODE_BIN", "/legacy/npx")
    settings = reload_settings()
    assert settings.wenyan_node_bin == "/legacy/npx"


def test_new_name_wins_over_legacy(monkeypatch):
    monkeypatch.setenv("NODE_BIN", "/legacy/npx")
    monkeypatch.setenv("WENYAN_NODE_BIN", "/new/npx")
    assert reload_settings().wenyan_node_bin == "/new/npx"


def test_both_wenyan_consumers_read_the_same_config(monkeypatch):
    """P4 归并的目的：渲染侧与发布侧不能再各读一半配置。"""
    monkeypatch.setenv("WENYAN_NODE_BIN", "/shared/npx")
    monkeypatch.setenv("WENYAN_NPM_SPEC", "@wenyan-md/cli@2.0.11")
    reload_settings()

    from generation.wechat_render import build_command
    from publishers.wechat_mp.wenyan_backend import WenyanBackend

    render_cmd = build_command(
        Path("/tmp/a.md"),
        theme="default",
        node_bin=reload_settings().wenyan_node_bin,
        npm_spec=reload_settings().wenyan_npm_spec,
    )
    publish_cmd = WenyanBackend.from_settings().build_command(Path("/tmp/a.md"))
    assert render_cmd[0] == publish_cmd[0] == "/shared/npx"
    assert "@wenyan-md/cli@2.0.11" in render_cmd
    assert "@wenyan-md/cli@2.0.11" in publish_cmd


# ------------------------------------------------------------ 弃用别名


def test_deprecated_aliases_detected():
    stale = deprecated_env_aliases({"NODE_BIN": "/x"})
    assert stale == [("NODE_BIN", "WENYAN_NODE_BIN")]


def test_deprecated_alias_silent_when_new_name_also_set():
    """两个都写了说明已经在迁移，不必再唠叨。"""
    assert deprecated_env_aliases({"NODE_BIN": "/x", "WENYAN_NODE_BIN": "/y"}) == []


def test_every_deprecated_alias_actually_still_works():
    """台账里登记的别名必须真的还能用，否则这张表是在骗人。"""
    for old, _new in DEPRECATED_ENV_ALIASES.items():
        sentinel: object = "sentinel-value"
        try:
            settings = Settings(_env_file=None, **{old.lower(): sentinel})
        except ValidationError:
            # 数值字段收不下字符串哨兵。换一个不会与任何默认值撞车的数字，
            # 否则"别名生效"会被默认值伪装成通过。
            sentinel = 4321
            settings = Settings(_env_file=None, **{old.lower(): sentinel})
        assert str(sentinel) in settings.model_dump_json(), f"{old} 别名已失效"


def test_every_deprecated_alias_has_the_new_name_too():
    """新名必须也真的能用——只留回退别名等于改名没做。"""
    for old, new in DEPRECATED_ENV_ALIASES.items():
        sentinel: object = "sentinel-value"
        try:
            settings = Settings(_env_file=None, **{new.lower(): sentinel})
        except ValidationError:
            sentinel = 4321
            settings = Settings(_env_file=None, **{new.lower(): sentinel})
        assert str(sentinel) in settings.model_dump_json(), f"{new}（{old} 的新名）不生效"


# ------------------------------------------------------------ DSH_ → SW_DSH_ 改名（P15）


def test_no_dsh_reserved_prefix_left_in_env_example():
    """`.env.example` 里出现任何 dsh 保留前缀的名字，都会让对话台在本仓库目录下起不来。

    dsh 产品 CLI 读项目 `.env` 时对这些前缀一律抛错拒绝启动（无开关），
    所以这是一条**会真的把对话台打死**的回归，值得单独钉住。
    """
    reserved_prefixes = ("DSH_", "XDG_", "DYLD_", "BASH_FUNC_")
    text = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
    offenders = [
        line.split("=", 1)[0].strip()
        for line in text.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
        if line.split("=", 1)[0].strip().upper().startswith(reserved_prefixes)
    ]
    assert offenders == [], f".env.example 声明了 dsh 保留前缀的变量：{offenders}"


def test_sw_dsh_provider_is_the_canonical_name(monkeypatch):
    monkeypatch.setenv("SW_DSH_PROVIDER", "gateway")
    monkeypatch.delenv("DSH_PROVIDER", raising=False)
    assert reload_settings().dsh_provider == "gateway"


def test_legacy_dsh_provider_still_read(monkeypatch):
    """生产 .env 下次部署才改名，旧名断了就是一次线上事故。"""
    monkeypatch.delenv("SW_DSH_PROVIDER", raising=False)
    monkeypatch.setenv("DSH_PROVIDER", "deepseek-official")
    assert reload_settings().dsh_provider == "deepseek-official"


def test_new_dsh_name_wins_over_legacy(monkeypatch):
    """两个都写着时以新名为准——迁移期里旧名不该反过来盖掉新名。"""
    monkeypatch.setenv("DSH_PROVIDER", "旧名")
    monkeypatch.setenv("SW_DSH_PROVIDER", "新名")
    assert reload_settings().dsh_provider == "新名"


def test_dsh_model_routing_defaults_off_and_reads_only_sw_names(monkeypatch):
    monkeypatch.delenv("SW_DSH_MODEL_ROUTING", raising=False)
    assert Settings(_env_file=None).dsh_model_routing is False

    monkeypatch.setenv("SW_DSH_MODEL_ROUTING", "true")
    monkeypatch.setenv("SW_DSH_SOL_MODEL", "sol-custom")
    monkeypatch.setenv("SW_DSH_LUNA_MODEL", "luna-custom")
    settings = Settings(_env_file=None)
    assert settings.dsh_model_routing is True
    assert (settings.dsh_sol_model, settings.dsh_luna_model) == ("sol-custom", "luna-custom")


def test_removed_terra_key_is_gone_from_settings_and_example(monkeypatch):
    """``SW_DSH_TERRA_MODEL`` 已移除：字段没了，`.env.example` 里也不许再出现。

    Settings 是 ``extra="ignore"``，所以旧 .env 里留着这一行**不会**报错——它只是
    静默无效。迁移说明因此必须留在 `.env.example` 里，不然没人知道它失效了。
    """
    monkeypatch.setenv("SW_DSH_TERRA_MODEL", "terra-custom")
    settings = Settings(_env_file=None)
    assert not hasattr(settings, "dsh_terra_model")

    example = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")
    assert "SW_DSH_TERRA_MODEL=" not in example
    assert "SW_DSH_TERRA_MODEL" in example, "迁移说明要点名这个已移除的键"


def test_legacy_dsh_names_are_reported_as_deprecated():
    stale = dict(deprecated_env_aliases({"DSH_PROVIDER": "deepseek"}))
    assert stale == {"DSH_PROVIDER": "SW_DSH_PROVIDER"}


def test_deprecated_aliases_see_dotenv_not_only_process_env(tmp_path, monkeypatch):
    """只写在 `.env` 里的旧名也要能被发现。

    pydantic-settings 读 `.env` **不会**把值写进 ``os.environ``，只看 ``os.environ``
    就会漏掉最常见的那种形态——而那正是 ``DSH_PROVIDER`` 撞名的那一份。
    """
    (tmp_path / ".env").write_text("DSH_PROVIDER=deepseek\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DSH_PROVIDER", raising=False)
    monkeypatch.delenv("SW_DSH_PROVIDER", raising=False)
    assert ("DSH_PROVIDER", "SW_DSH_PROVIDER") in deprecated_env_aliases()


def test_log_deprecated_env_aliases_never_prints_values(caplog, monkeypatch, tmp_path):
    """提示只报名字。变量值里可能是凭据，日志里出现一次就是泄漏。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DSH_PROVIDER", "super-secret-route-name")
    monkeypatch.delenv("SW_DSH_PROVIDER", raising=False)
    with caplog.at_level(logging.WARNING, logger="core.config"):
        stale = log_deprecated_env_aliases()
    assert ("DSH_PROVIDER", "SW_DSH_PROVIDER") in stale
    assert "super-secret-route-name" not in caplog.text
    assert "DSH_PROVIDER → SW_DSH_PROVIDER" in caplog.text


def test_explicit_config_env_file_isolates_settings_and_preflight(tmp_path, monkeypatch):
    """显式 E2E 配置不能被工作目录的 `.env` 或旧别名污染。"""
    from scripts.preflight import check_env_file, run_checks

    fake_repo = tmp_path / "fake-repo"
    fake_repo.mkdir()
    (fake_repo / ".env").write_text(
        "\n".join(
            [
                "DSH_PROVIDER=root-legacy-marker",
                "ANTHROPIC_API_KEY=sk-ant-root-marker-should-never-be-used",
                "SW_DATABASE_URL=sqlite:///./root-marker.db",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    e2e_env = tmp_path / "e2e.env"
    e2e_db = tmp_path / "e2e.db"
    e2e_env.write_text(
        "\n".join(
            [
                f"SW_DATABASE_URL=sqlite:///{e2e_db}",
                "SW_DSH_PROVIDER=e2e-provider",
                "ANTHROPIC_API_KEY=sk-ant-e2e-isolated-000000000000000000000000",
                "SW_LLM_BACKEND=anthropic",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    accounts_path = tmp_path / "accounts.yaml"
    accounts_path.write_text(yaml.safe_dump({"accounts": []}), encoding="utf-8")

    monkeypatch.chdir(fake_repo)
    monkeypatch.setenv("SW_CONFIG_ENV_FILE", str(e2e_env))
    for name in (
        "SW_DATABASE_URL",
        "DSH_PROVIDER",
        "SW_DSH_PROVIDER",
        "ANTHROPIC_API_KEY",
        "SW_LLM_BACKEND",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings()
    assert settings.sw_database_url == f"sqlite:///{e2e_db}"
    assert settings.dsh_provider == "e2e-provider"
    assert settings.anthropic_api_key.endswith("000000000000")
    assert deprecated_env_aliases() == []
    assert log_deprecated_env_aliases() == []

    source_check = check_env_file()
    assert source_check.status == "OK"
    assert source_check.detail == "E2E 专用配置已存在"
    assert str(e2e_env) not in source_check.detail

    checks = run_checks(offline=True, accounts_path=accounts_path)
    rendered = "\n".join(check.detail for check in checks)
    by_name = {check.name: check for check in checks}
    assert by_name["数据库"].detail.startswith(f"sqlite:///{e2e_db}")
    assert by_name["环境变量命名"].status == "OK"
    assert "root-legacy-marker" not in rendered
    assert "root-marker.db" not in rendered


# ------------------------------------------------------------ preflight


def test_preflight_flags_unsynced_accounts(tmp_path):
    from scripts.preflight import check_accounts_synced

    path = tmp_path / "accounts.yaml"
    path.write_text(
        yaml.safe_dump({"accounts": [{"id": "pf-1", "platform": "xhs"}]}), encoding="utf-8"
    )
    checks = {c.name: c for c in check_accounts_synced(path)}
    assert checks["账号台账"].status == "OK"
    assert checks["台账已入库"].status == "FAIL"
    assert "core.accounts sync" in checks["台账已入库"].detail


def test_preflight_passes_after_sync(tmp_path):
    from core.accounts import load_specs, sync_accounts
    from scripts.preflight import check_accounts_synced

    path = tmp_path / "accounts.yaml"
    path.write_text(
        yaml.safe_dump({"accounts": [{"id": "pf-2", "platform": "xhs"}]}), encoding="utf-8"
    )
    with db.session_scope() as session:
        sync_accounts(session, load_specs(path))

    checks = {c.name: c for c in check_accounts_synced(path)}
    assert checks["台账已入库"].status == "OK"


def test_preflight_reports_orphan_accounts(tmp_path):
    from scripts.preflight import check_accounts_synced

    path = tmp_path / "accounts.yaml"
    path.write_text(yaml.safe_dump({"accounts": []}), encoding="utf-8")
    with db.session_scope() as session:
        make_account(session, account_id="手工建的")
    checks = {c.name: c for c in check_accounts_synced(path)}
    # 空台账本身是 WARN，不该因为 DB 里有账号就 FAIL
    assert checks["账号台账"].status == "WARN"


def test_preflight_rejects_broken_yaml(tmp_path):
    from scripts.preflight import check_accounts_synced

    path = tmp_path / "accounts.yaml"
    path.write_text(yaml.safe_dump({"accounts": [{"id": "x", "platform": "weibo"}]}), "utf-8")
    checks = {c.name: c for c in check_accounts_synced(path)}
    assert checks["账号台账"].status == "FAIL"


def test_preflight_schedule_params(monkeypatch):
    from scripts.preflight import check_schedule

    assert check_schedule(reload_settings()).status == "OK"

    monkeypatch.setenv("SW_RETRY_BACKOFF_BASE_SECONDS", "3600")
    monkeypatch.setenv("SW_RETRY_BACKOFF_MAX_SECONDS", "60")
    check = check_schedule(reload_settings())
    assert check.status in ("WARN", "FAIL") and "退避" in check.detail


def test_preflight_trendradar_unconfigured(monkeypatch):
    from scripts.preflight import check_trendradar

    monkeypatch.setenv("TRENDRADAR_BASE_URL", "")
    checks = check_trendradar(reload_settings(), True)
    assert checks[0].status == "WARN" and "TRENDRADAR_BASE_URL" in checks[0].detail


def test_preflight_douyin_headless_is_a_hard_fail(monkeypatch):
    """docs/POLICY.md：抖音上传器必须有头。无头就是红线，直接 FAIL。"""
    import httpx

    from scripts.preflight import check_douyin_service

    class _Resp:
        @staticmethod
        def json() -> dict:
            return {"headless": True}

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _Resp())
    check = check_douyin_service(reload_settings(), False)
    assert check.status == "FAIL" and "headless" in check.detail

    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **kw: type("R", (), {"json": staticmethod(lambda: {"headless": False})})(),
    )
    assert check_douyin_service(reload_settings(), False).status == "OK"


def test_preflight_runs_end_to_end_offline(tmp_path, monkeypatch):
    """整个门禁必须能在无网、无凭据的机器上跑完而不抛异常。"""
    from scripts.preflight import run_checks

    path = tmp_path / "accounts.yaml"
    path.write_text(yaml.safe_dump({"accounts": []}), encoding="utf-8")
    checks = run_checks(offline=True, accounts_path=path)
    assert len(checks) > 10
    assert all(c.status in ("OK", "WARN", "FAIL", "SKIP") for c in checks)
    names = {c.name for c in checks}
    assert {"调度参数", "环境变量命名", "TrendRadar", "账号台账"} <= names


# ------------------------------------------------------------ dsh 后端门禁（P5）


def test_dsh_checks_are_skipped_on_the_default_backend(monkeypatch):
    from scripts.preflight import check_dsh

    monkeypatch.setenv("SW_LLM_BACKEND", "anthropic")
    checks = check_dsh(reload_settings(), True)
    assert [c.status for c in checks] == ["SKIP"]


def test_dsh_checks_report_zero_tools_and_missing_credential(monkeypatch):
    """切到 dsh 后：零工具红线必须 OK，缺凭据必须 FAIL（而不是静默放行）。"""
    from scripts.preflight import check_dsh

    monkeypatch.setenv("SW_LLM_BACKEND", "dsh")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    checks = {c.name: c for c in check_dsh(reload_settings(), True)}
    assert checks["dsh 后端 零工具红线"].status == "OK"
    assert checks["dsh 后端 provider"].status == "FAIL"
    assert "DEEPSEEK_API_KEY" in checks["dsh 后端 provider"].detail
    # --offline 下不拉子进程
    assert checks["dsh 后端 握手"].status == "SKIP"

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-test")
    checks = {c.name: c for c in check_dsh(reload_settings(), True)}
    assert checks["dsh 后端 provider"].status == "OK"


def test_missing_anthropic_key_is_not_fatal_under_dsh(monkeypatch):
    from scripts.preflight import check_anthropic

    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("SW_LLM_BACKEND", "anthropic")
    assert check_anthropic(reload_settings()).status == "FAIL"
    monkeypatch.setenv("SW_LLM_BACKEND", "dsh")
    assert check_anthropic(reload_settings()).status == "WARN"


def test_dsh_provider_typo_is_reported(monkeypatch):
    from scripts.preflight import check_dsh

    monkeypatch.setenv("SW_LLM_BACKEND", "dsh")
    monkeypatch.setenv("SW_DSH_PROVIDER", "打错的路由名")
    checks = {c.name: c for c in check_dsh(reload_settings(), True)}
    assert checks["dsh 后端 provider"].status == "FAIL"
    assert "打错的路由名" in checks["dsh 后端 provider"].detail


def _write_routed_cordis(
    path: Path,
    *,
    missing_model: str = "",
    effort_override: tuple[str, str] | None = None,
) -> None:
    models = []
    for model, effort in (
        ("gpt-5.6-sol", "xhigh"),
        ("gpt-5.6-luna", "max"),
    ):
        if model == missing_model:
            continue
        efforts = {effort: effort}
        if effort_override is not None and model == effort_override[0]:
            efforts = {effort: effort_override[1]}
        models.append({"id": model, "reasoningEfforts": efforts})
    path.write_text(
        yaml.safe_dump(
            [
                {
                    "id": "llm",
                    "name": "@deepseek-ai/dsh-llm-pi-ai",
                    "config": {
                        "providers": {
                            "gateway": {
                                "apiKeyEnv": "SW_TEST_GATEWAY_API_KEY",
                                "models": models,
                            }
                        }
                    },
                }
            ]
        ),
        encoding="utf-8",
    )


def _routed_preflight(monkeypatch, path: Path):
    from scripts.preflight import check_dsh

    monkeypatch.setenv("SW_LLM_BACKEND", "dsh")
    monkeypatch.setenv("SW_DSH_MODEL_ROUTING", "true")
    monkeypatch.setenv("SW_DSH_PROVIDER", "gateway")
    monkeypatch.setenv("SW_DSH_CORDIS_PATH", str(path))
    monkeypatch.setenv("SW_TEST_GATEWAY_API_KEY", "configured-for-test")
    return {check.name: check for check in check_dsh(reload_settings(), True)}


def test_preflight_static_audits_all_routed_models_and_efforts(tmp_path, monkeypatch):
    path = tmp_path / "cordis.yml"
    _write_routed_cordis(path)
    checks = _routed_preflight(monkeypatch, path)
    assert checks["dsh 后端 模型路由"].status == "OK"


def test_preflight_fails_when_a_routed_model_is_missing(tmp_path, monkeypatch):
    path = tmp_path / "cordis.yml"
    _write_routed_cordis(path, missing_model="gpt-5.6-luna")
    checks = _routed_preflight(monkeypatch, path)
    assert checks["dsh 后端 模型路由"].status == "FAIL"
    assert "gpt-5.6-luna" in checks["dsh 后端 模型路由"].detail


@pytest.mark.parametrize(
    ("model", "mapped_effort"),
    [
        ("gpt-5.6-sol", "high"),
        ("gpt-5.6-luna", "low"),
    ],
)
def test_preflight_fails_when_a_routed_effort_is_downgraded(
    tmp_path, monkeypatch, model, mapped_effort
):
    path = tmp_path / "cordis.yml"
    _write_routed_cordis(path, effort_override=(model, mapped_effort))
    checks = _routed_preflight(monkeypatch, path)
    assert checks["dsh 后端 模型路由"].status == "FAIL"
    expected_effort = "max" if model == "gpt-5.6-luna" else "xhigh"
    detail = checks["dsh 后端 模型路由"].detail
    assert f"{expected_effort}:{expected_effort}" in detail
    assert f"{expected_effort}:{mapped_effort}" in detail


def test_preflight_still_audits_routes_when_dsh_sdk_is_missing(tmp_path, monkeypatch):
    import sys

    path = tmp_path / "cordis.yml"
    _write_routed_cordis(path)
    monkeypatch.setitem(sys.modules, "deepseek_harness", None)
    checks = _routed_preflight(monkeypatch, path)
    assert checks["dsh 后端 SDK"].status == "FAIL"
    assert checks["dsh 后端 模型路由"].status == "OK"
    assert checks["dsh 后端 零工具红线"].status == "OK"
    assert checks["dsh 后端 握手"].status == "SKIP"


def test_daily_image_budget_accepts_both_spellings(monkeypatch) -> None:
    """两种拼法都要认：认错一个的后果是预算配置**静默失效**。"""
    from core.config import Settings

    monkeypatch.delenv("DAILY_IMAGE_BUDGET", raising=False)
    monkeypatch.setenv("SW_DAILY_IMAGE_BUDGET", "7")
    assert Settings(_env_file=None).daily_image_budget == 7

    # 主名优先（与 DAILY_TOKEN_BUDGET / DAILY_RENDER_SECONDS_BUDGET 对齐）
    monkeypatch.setenv("DAILY_IMAGE_BUDGET", "12")
    assert Settings(_env_file=None).daily_image_budget == 12


# ------------------------------------------------------ 输出预算体检（P11.2）


def test_preflight_reports_calls_that_hit_the_output_ceiling(session):
    """预算贴边要在变成 502 之前被人看到，不能只留在账本里等人去翻。"""
    from core.budget import BudgetGuard, CostKind
    from scripts.preflight import check_output_budget

    guard = BudgetGuard(session, token_budget=10_000)
    guard.charge(CostKind.TOKENS, 10, meta={"purpose": "xhs.cards", "stop_reason": "max_tokens"})
    session.commit()

    check = check_output_budget(reload_settings())

    assert check.status == "WARN"
    assert "xhs.cards" in check.detail


def test_preflight_output_budget_is_ok_on_a_clean_day(session):
    from core.budget import BudgetGuard, CostKind
    from scripts.preflight import check_output_budget

    guard = BudgetGuard(session, token_budget=10_000)
    guard.charge(CostKind.TOKENS, 10, meta={"purpose": "xhs.note", "stop_reason": "end_turn"})
    session.commit()

    assert check_output_budget(reload_settings()).status == "OK"


def test_preflight_output_budget_skips_when_there_is_no_database(monkeypatch, tmp_path):
    """全新机器上还没有库：这是 SKIP，不是 FAIL——门禁不该因为它红。"""
    from scripts.preflight import check_output_budget

    monkeypatch.setenv("SW_DATABASE_URL", f"sqlite:///{tmp_path / 'nope.db'}")
    check = check_output_budget(reload_settings())
    assert check.status == "SKIP"


# ------------------------------------- 通知通道 = 人工确认闸门通道（R1，P14）

#: 形状照着真 bot token 编的假凭据。门禁输出里出现它的任何一段都是泄露。
FAKE_BOT_TOKEN = "8123456789:AAHnotarealtokennotarealtoken12345"
FAKE_CHAT_ID = "424242424"


def _notifier_check(monkeypatch, **env: str):
    """按给定环境变量算一次「通知通道」体检。"""
    from scripts.preflight import check_notifier

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return check_notifier(reload_settings())


def test_notifier_ok_when_the_telegram_confirm_gate_is_ready(monkeypatch):
    """R1 的载体活着才算 OK——这条门禁过去只看飞书，对 Telegram 一无所知。"""
    check = _notifier_check(
        monkeypatch,
        SW_TELEGRAM_ENABLED="true",
        TELEGRAM_BOT_TOKEN=FAKE_BOT_TOKEN,
        TELEGRAM_CHAT_ID=FAKE_CHAT_ID,
        FEISHU_WEBHOOK="",
    )
    assert check.status == "OK"
    assert "确认闸门" in check.detail
    assert FAKE_BOT_TOKEN not in check.detail
    assert FAKE_CHAT_ID not in check.detail


def test_notifier_warns_when_telegram_has_no_chat_id(monkeypatch):
    """配了 bot 却没人给它发过 /start：卡片不知道推给谁，指引要原样带出来。"""
    check = _notifier_check(
        monkeypatch,
        SW_TELEGRAM_ENABLED="true",
        TELEGRAM_BOT_TOKEN=FAKE_BOT_TOKEN,
        TELEGRAM_CHAT_ID="",
    )
    assert check.status == "WARN"
    assert "TELEGRAM_CHAT_ID" in check.detail
    assert FAKE_BOT_TOKEN not in check.detail


def test_notifier_warns_when_the_telegram_master_switch_is_off(monkeypatch):
    """``ready`` 不看总开关，但关掉开关就一条都发不出去，所以这里必须是 WARN。"""
    check = _notifier_check(
        monkeypatch,
        SW_TELEGRAM_ENABLED="false",
        TELEGRAM_BOT_TOKEN=FAKE_BOT_TOKEN,
        TELEGRAM_CHAT_ID=FAKE_CHAT_ID,
    )
    assert check.status == "WARN"
    assert "SW_TELEGRAM_ENABLED" in check.detail


def test_notifier_warns_that_feishu_cannot_carry_the_confirm_gate(monkeypatch):
    """飞书只能收通知，接不了确认闸门的回调按钮——这是两回事，不能互相顶替。"""
    check = _notifier_check(
        monkeypatch,
        FEISHU_WEBHOOK="https://open.feishu.cn/open-apis/bot/v2/hook/whatever",
        TELEGRAM_BOT_TOKEN="",
        TELEGRAM_CHAT_ID="",
    )
    assert check.status == "WARN"
    assert "回调按钮" in check.detail
    assert "TELEGRAM_BOT_TOKEN" in check.detail


def test_notifier_warns_when_nothing_is_configured(monkeypatch):
    check = _notifier_check(
        monkeypatch,
        FEISHU_WEBHOOK="",
        TELEGRAM_BOT_TOKEN="",
        TELEGRAM_CHAT_ID="",
    )
    assert check.status == "WARN"
    assert "退化为日志通知" in check.detail


@pytest.mark.parametrize(
    ("enabled", "token", "chat_id", "webhook"),
    [
        ("true", FAKE_BOT_TOKEN, FAKE_CHAT_ID, ""),
        ("true", FAKE_BOT_TOKEN, FAKE_CHAT_ID, "https://feishu.test/hook"),
        ("true", FAKE_BOT_TOKEN, "", ""),
        ("true", FAKE_BOT_TOKEN, "", "https://feishu.test/hook"),
        ("false", FAKE_BOT_TOKEN, FAKE_CHAT_ID, ""),
        ("false", "", "", ""),
        ("true", "", "", "https://feishu.test/hook"),
        ("true", "", "", ""),
    ],
)
def test_notifier_never_fails_the_deployment(monkeypatch, enabled, token, chat_id, webhook):
    """preflight 跑在 `scripts/ops/update.sh --apply` 的部署流程里：这一项出一个 FAIL
    就直接卡死生产部署。通道降级要被看见，但不该有那种杀伤力——硬互锁在
    `scripts/ops/verify.sh`（真发布开启时通道必须 ready+polling）。
    """
    check = _notifier_check(
        monkeypatch,
        SW_TELEGRAM_ENABLED=enabled,
        TELEGRAM_BOT_TOKEN=token,
        TELEGRAM_CHAT_ID=chat_id,
        FEISHU_WEBHOOK=webhook,
    )
    assert check.status in ("OK", "WARN")
    assert check.status != "FAIL"
    # 任何组合下都不许把凭据写进门禁输出
    assert FAKE_BOT_TOKEN not in check.detail
    assert FAKE_CHAT_ID not in check.detail
