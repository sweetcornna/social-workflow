"""账号台账：accounts.yaml 解析、DB 幂等同步、账号级调度策略（P4）。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import yaml

from core import db
from core.accounts import (
    PLATFORM_DAILY_CEILING,
    AccountsError,
    diff_report,
    load_specs,
    merge_extra,
    parse_spec,
    parse_window,
    parse_windows,
    policy_of,
    sync_accounts,
)
from core.models import Account
from tests.conftest import make_account

REPO_ACCOUNTS = "accounts.yaml"


# ------------------------------------------------------------------ 台账解析


def test_repo_accounts_yaml_is_valid():
    """仓库自带的台账必须能解析——它是首次部署清单的第一步。"""
    specs = load_specs(REPO_ACCOUNTS)
    ids = {spec.id for spec in specs}
    # 文档（README / docs/OPS.md）里用的就是这三个 id，缺一 dev 端点就 404
    assert {"xhs-demo-01", "wechat-demo-01", "douyin-demo-01"} <= ids
    by_id = {spec.id: spec for spec in specs}
    # sidecar.port → sidecar_endpoint
    assert by_id["xhs-demo-01"].sidecar_endpoint == "http://localhost:18060"
    # token 只存**环境变量名**，绝不存值（docs/POLICY.md）
    assert by_id["xhs-demo-01"].extra["xhs"]["auth_token_env"] == "XHS_TOKEN_XHS_DEMO_01"
    assert "XHS_TOKEN" not in str(by_id["xhs-demo-01"].extra).replace("XHS_TOKEN_XHS_DEMO_01", "")
    # P4 的调度字段落进 extra
    assert by_id["douyin-demo-01"].extra["daily_target"] == 1
    assert by_id["douyin-demo-01"].extra["publish_windows"]
    # 既有的 identity_hint 不能被 P4 的字段挤掉
    assert by_id["douyin-demo-01"].extra["identity_hint"] == "抖音测试号 01"


def test_parse_spec_rejects_unknown_platform():
    with pytest.raises(AccountsError, match="platform"):
        parse_spec({"id": "a", "platform": "weibo"})


def test_parse_spec_requires_id():
    with pytest.raises(AccountsError, match="缺少 id"):
        parse_spec({"platform": "xhs"})


def test_parse_spec_clamps_daily_limit_to_platform_ceiling():
    spec = parse_spec({"id": "d1", "platform": "douyin", "daily_limit": 999})
    assert spec.daily_limit == PLATFORM_DAILY_CEILING["douyin"]


def test_platform_ceiling_matches_publisher_constant():
    """台账侧的硬顶必须和发布器里的 ceiling 一致，否则两边会打架。"""
    from publishers.douyin.publisher import DAILY_LIMIT_CEILING

    assert PLATFORM_DAILY_CEILING["douyin"] == DAILY_LIMIT_CEILING


def test_load_specs_rejects_duplicate_ids(tmp_path):
    path = tmp_path / "accounts.yaml"
    path.write_text(
        yaml.safe_dump(
            {"accounts": [{"id": "a", "platform": "xhs"}, {"id": "a", "platform": "xhs"}]}
        ),
        encoding="utf-8",
    )
    with pytest.raises(AccountsError, match="id 重复"):
        load_specs(path)


def test_load_specs_missing_file_is_not_an_error(tmp_path):
    assert load_specs(tmp_path / "nope.yaml") == []


# ------------------------------------------------------------------ 时段窗口


def test_parse_window_basic():
    start, end = parse_window("09:00-11:30")
    assert (start.hour, start.minute) == (9, 0)
    assert (end.hour, end.minute) == (11, 30)


@pytest.mark.parametrize("bad", ["09:00", "9-11", "25:00-26:00", "09:00-09:00", "上午-中午"])
def test_parse_window_rejects_garbage(bad):
    with pytest.raises(AccountsError):
        parse_window(bad)


def test_parse_window_also_accepts_iso_basic_form():
    """``time.fromisoformat`` 认 ISO 基本格式，``0900-1100`` 等价于 ``09:00-11:00``。

    留个用例说明这不是漏网之鱼，是标准库行为。
    """
    assert parse_window("0900-1100") == parse_window("09:00-11:00")


def test_parse_windows_empty_means_all_day():
    assert parse_windows(None) == []
    assert parse_windows([]) == []


def test_policy_window_uses_account_timezone(session):
    account = make_account(session, account_id="acc-tz")
    account.extra = {"publish_windows": ["09:00-11:00"], "timezone": "Asia/Shanghai"}
    policy = policy_of(account)

    # 09:30 北京 = 01:30 UTC
    assert policy.in_window(datetime(2026, 8, 16, 1, 30, tzinfo=UTC))
    # 09:30 UTC = 17:30 北京，不在窗口内
    assert not policy.in_window(datetime(2026, 8, 16, 9, 30, tzinfo=UTC))


def test_policy_window_wraps_midnight(session):
    account = make_account(session, account_id="acc-night")
    account.extra = {"publish_windows": ["22:00-02:00"], "timezone": "UTC"}
    policy = policy_of(account)
    assert policy.in_window(datetime(2026, 8, 16, 23, 0, tzinfo=UTC))
    assert policy.in_window(datetime(2026, 8, 16, 1, 0, tzinfo=UTC))
    assert not policy.in_window(datetime(2026, 8, 16, 12, 0, tzinfo=UTC))


def test_policy_next_window_start(session):
    account = make_account(session, account_id="acc-next")
    account.extra = {"publish_windows": ["09:00-11:00"], "timezone": "UTC"}
    policy = policy_of(account)
    nxt = policy.next_window_start(datetime(2026, 8, 16, 12, 0, tzinfo=UTC))
    assert nxt == datetime(2026, 8, 17, 9, 0, tzinfo=UTC)


def test_policy_no_window_means_always(session):
    policy = policy_of(make_account(session, account_id="acc-any"))
    assert policy.in_window(datetime(2026, 8, 16, 3, 33, tzinfo=UTC))
    assert policy.next_window_start(datetime(2026, 8, 16, 3, 33, tzinfo=UTC)) is None
    assert policy.window_text() == "全天"


def test_policy_bad_window_degrades_to_all_day(session):
    """台账写错不能让调度器崩，只能退化成全天并留日志。"""
    account = make_account(session, account_id="acc-bad")
    account.extra = {"publish_windows": ["九点到十一点"]}
    policy = policy_of(account)
    assert policy.windows == ()
    assert policy.in_window(datetime(2026, 8, 16, 3, 0, tzinfo=UTC))


def test_policy_min_interval_platform_default_is_a_floor(session):
    """抖音的 30 分钟是下限：台账写更松也不放行得更快。"""
    account = make_account(session, account_id="acc-dy", platform="douyin")
    account.extra = {"min_interval_minutes": 5}
    assert policy_of(account).min_interval.total_seconds() == 30 * 60

    account.extra = {"min_interval_minutes": 120}
    assert policy_of(account).min_interval.total_seconds() == 120 * 60


def test_policy_clamps_daily_limit_by_platform(session):
    account = make_account(session, account_id="acc-dy2", platform="douyin", daily_limit=99)
    assert policy_of(account).daily_limit == PLATFORM_DAILY_CEILING["douyin"]


# -------------------------------------------------------------------- 同步


def _write(tmp_path, accounts: list[dict]) -> str:
    path = tmp_path / "accounts.yaml"
    path.write_text(yaml.safe_dump({"accounts": accounts}, allow_unicode=True), encoding="utf-8")
    return str(path)


def test_sync_creates_then_is_idempotent(tmp_path, session):
    path = _write(
        tmp_path,
        [{"id": "a1", "platform": "xhs", "name": "甲", "daily_limit": 7, "daily_target": 2}],
    )
    specs = load_specs(path)

    first = sync_accounts(session, specs)
    session.commit()
    assert first.created == ["a1"] and not first.updated

    second = sync_accounts(session, specs)
    session.commit()
    assert second.unchanged == ["a1"], "第二次同步必须什么都不改"
    assert second.changed == 0

    account = session.get(Account, "a1")
    assert account.daily_limit == 7
    assert account.extra["daily_target"] == 2


def test_sync_never_overwrites_runtime_status(tmp_path, session):
    """登录巡检把账号打成 needs_relogin 之后，同步不许把它改回 ok。"""
    path = _write(tmp_path, [{"id": "a1", "platform": "xhs", "name": "甲"}])
    specs = load_specs(path)
    sync_accounts(session, specs)
    session.commit()

    session.get(Account, "a1").status = "needs_relogin"
    session.commit()

    sync_accounts(session, specs)
    session.commit()
    assert session.get(Account, "a1").status == "needs_relogin"


def test_sync_updates_changed_fields(tmp_path, session):
    sync_accounts(session, load_specs(_write(tmp_path, [{"id": "a1", "platform": "xhs"}])))
    session.commit()
    report = sync_accounts(
        session,
        load_specs(_write(tmp_path, [{"id": "a1", "platform": "xhs", "name": "改了名"}])),
    )
    session.commit()
    assert report.updated == ["a1"]
    assert session.get(Account, "a1").name == "改了名"


def test_sync_reports_orphans_without_deleting(tmp_path, session):
    make_account(session, account_id="手工建的")
    session.commit()
    report = sync_accounts(session, load_specs(_write(tmp_path, [{"id": "a1", "platform": "xhs"}])))
    session.commit()
    assert report.orphans == ["手工建的"]
    assert session.get(Account, "手工建的") is not None, "台账外的账号只报告不删除"


def test_sync_dry_run_changes_nothing(tmp_path, session):
    specs = load_specs(_write(tmp_path, [{"id": "a1", "platform": "xhs"}]))
    report = sync_accounts(session, specs, dry_run=True)
    session.commit()
    assert report.created == ["a1"]
    assert session.get(Account, "a1") is None


def test_merge_extra_keeps_runtime_keys_and_drops_stale_ledger_keys():
    existing = {
        "insights_updated_at": "2026-08-15T00:00:00+00:00",  # 运行时写的，保留
        "publish_windows": ["09:00-11:00"],  # 台账管的，YAML 里删掉就该消失
        "identity_hint": "老昵称",
    }
    merged = merge_extra(existing, {"daily_target": 3})
    assert merged == {"insights_updated_at": "2026-08-15T00:00:00+00:00", "daily_target": 3}


def test_diff_report_detects_drift(tmp_path, session):
    specs = load_specs(_write(tmp_path, [{"id": "a1", "platform": "xhs", "daily_limit": 3}]))
    assert diff_report(session, specs).created == ["a1"]
    sync_accounts(session, specs)
    session.commit()
    assert diff_report(session, specs).changed == 0


# --------------------------------------------------------------------- CLI


def test_cli_sync_then_check(tmp_path, monkeypatch, capsys):
    from core.accounts import main

    path = _write(tmp_path, [{"id": "cli-1", "platform": "xhs", "name": "CLI"}])
    assert main(["--file", path, "sync"]) == 0
    assert "新建 1" in capsys.readouterr().out
    assert main(["--file", path, "check"]) == 0
    assert main(["--file", path, "list"]) == 0
    assert "cli-1" in capsys.readouterr().out

    with db.session_scope() as s:
        assert s.get(Account, "cli-1") is not None


def test_cli_prints_which_database_it_touched(tmp_path, capsys):
    """CLI 要先把库路径打出来——"明明 sync 过了 list 却是空的"十次有九次是读错了库。

    Postgres 的 URL 里带密码，打印前必须打码：这行日志会被贴进工单和聊天窗口。
    """
    from core.accounts import main, redact_db_url

    path = _write(tmp_path, [{"id": "cli-3", "platform": "xhs"}])
    assert main(["--file", path, "sync"]) == 0
    assert "DB: sqlite:///" in capsys.readouterr().out

    assert redact_db_url("postgresql://sw:s3cret@db:5432/social") == (
        "postgresql://sw:***@db:5432/social"
    )
    assert redact_db_url("sqlite:///data/social_workflow.db") == "sqlite:///data/social_workflow.db"


def test_cli_check_fails_when_out_of_sync(tmp_path, capsys):
    from core.accounts import main

    path = _write(tmp_path, [{"id": "cli-2", "platform": "xhs"}])
    assert main(["--file", path, "check"]) == 1, "台账里有 DB 没有的账号必须退出码 1"
    assert "不一致" in capsys.readouterr().out
