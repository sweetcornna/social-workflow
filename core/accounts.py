"""账号台账：``accounts.yaml`` → DB 幂等同步，以及账号级调度策略。

为什么需要这个模块（P4 之前的缺口）
------------------------------------
``accounts.yaml`` 之前只被 ``scripts/gen_xhs_sidecars.py`` 用来生成 compose 片段，
**没有任何入库入口**：`/dev/seed` 只建一个 ``acc_demo_xhs``，而文档让人用
``douyin-demo-01`` / ``wechat-demo-01``，于是所有 dev 端点一律 404。
现在：

    uv run python -m core.accounts sync      # 幂等 upsert，首次部署第一步
    uv run python -m core.accounts check     # 校验 YAML 与 DB 是否一致（preflight 用）
    uv run python -m core.accounts list      # 看 DB 里现在有什么

同步只写**台账字段**（``platform/name/daily_limit/profile_dir/sidecar_endpoint/extra``），
绝不覆盖运行时状态（``Account.status``）——那是登录巡检的地盘，被 YAML 冲掉会把
``needs_relogin`` 的账号悄悄改回 ``ok``，排期就发到一个掉线的号上去了。

``extra`` 的合并规则
--------------------
``extra`` 里既有台账管的键（人设、发布时段…），也有运行时写的键（复盘时间戳…）。
同步时**只保留** :data:`RUNTIME_EXTRA_KEYS` 列出的运行时键，其余一律以 YAML 为准。
这样从 YAML 里删掉 ``publish_windows`` 才能真的把它删掉，而不是永远留在库里。

``Account.extra`` 里都有什么
----------------------------
以 :func:`parse_spec` 的实际写入点为准，按用途分组（都可省，缺省见
:class:`AccountPolicy`）。**这里刻意不写总数**：加键时要回到这份清单里补一行，
写个数字只会在下次加键时悄悄失真——``accounts.yaml`` 顶部那份注释同样按这个口径分块。

调度策略：

- ``daily_target``：每天产出几条稿（``tick_generate``）。0 / 缺省 = 不自动生成
- ``publish_windows``：发布时段窗口，如 ``["09:00-11:00", "19:00-22:00"]``
- ``min_interval_minutes``：该账号两次发布的最小间隔
- ``timezone``：窗口用的时区，缺省取 ``SW_TIMEZONE``

人设：

- ``persona``：行内人设（留空则读 ``prompts/accounts/<id>/persona.md``）

确认与托管（P12）：

- ``autopilot``：机器审核干净的稿子自动批准并排期。**默认关**
- ``confirm_required``：发布前要不要人点一下。**默认开且没有旁路**（红线 R1）
- ``confirm_ttl_hours``：推了确认卡多少小时还没人点就自动驳回，释放排期槽位

平台侧配置（嵌套一层，键名即平台）：

- ``xhs``：目前只有 ``auth_token_env``——sidecar 的 Bearer token
  **只写环境变量名，值永远不入库**（``docs/POLICY.md``）

公众号署名：

- ``author``：文章署名，被 ``generation.pipeline.generate_wechat_bundle`` 读走填进
  ``platform_extra['author']``。**不填这条，每篇公众号稿都会挂一条
  ``inspect.platform_extra.missing`` warn，autopilot 因此不自动批准**——
  它以前没写在这份清单里，于是没人知道要设。YAML 里放进 ``extra:`` 块即可。

运行时写进 ``extra`` 的键不属于上面任何一组、也不由 YAML 管，见上一节的
:data:`RUNTIME_EXTRA_KEYS`。
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.models import Account, utcnow

logger = logging.getLogger("social_workflow.accounts")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ACCOUNTS_FILE = ROOT / "accounts.yaml"

#: 发布时段窗口的默认时区。窗口是"人的作息"，用 UTC 写会反直觉。
#: 部署可用 ``SW_TIMEZONE`` 覆盖，单个账号可用 ``extra['timezone']`` 覆盖
DEFAULT_TIMEZONE = "Asia/Shanghai"


def default_timezone() -> str:
    from core.config import get_settings

    return get_settings().sw_timezone or DEFAULT_TIMEZONE


def accounts_file_path() -> Path:
    """当前生效的台账路径：``SW_ACCOUNTS_FILE`` 覆盖仓库根的 ``accounts.yaml``。

    P10 起工作台会**回写**这个文件，所以它必须可配：测试与 e2e 指到临时副本上，
    不能让一次用例把仓库里那份台账改了。
    """
    from core.config import get_settings

    configured = (get_settings().sw_accounts_file or "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_ACCOUNTS_FILE


#: 运行时写进 ``Account.extra`` 的键，同步时保留不动
RUNTIME_EXTRA_KEYS: frozenset[str] = frozenset(
    {
        "seeded",  # /dev/seed 注入的标记
        "insights_updated_at",  # 复盘 Agent 上次写 insights.md 的时刻
        "insights_error",  # 复盘 Agent 上次失败原因
    }
)

#: 台账里直接支持的顶层键（其余顶层键会被忽略并 warn，防止拼错静默失效）
KNOWN_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "platform",
        "name",
        "daily_limit",
        "daily_target",
        "publish_windows",
        "min_interval_minutes",
        "timezone",
        "persona",
        "profile_dir",
        "sidecar",
        "sidecar_endpoint",
        "extra",
        # P12：自动批准 / 发布前人工确认。**必须列在这里**，否则 parse_spec 只会
        # warn 一句"未识别的字段（已忽略）"，配置静默失效——而这两个键一个管钱
        # （自动出稿）一个管合规（人工确认），静默失效的代价太大
        "autopilot",
        "confirm_required",
        "confirm_ttl_hours",
    }
)

#: 平台日上限硬顶。与发布器里的 ceiling 保持一致（有回归测试盯着，见 tests/test_accounts.py）
#: 抖音：``publishers.douyin.publisher.DAILY_LIMIT_CEILING``
#: 小红书：计划 2.3「小红书日 ≤ 50」
PLATFORM_DAILY_CEILING: dict[str, int] = {"douyin": 10, "xhs": 50}

#: 平台默认最小发布间隔（分钟）。抖音比全局更保守，见 docs/POLICY.md
PLATFORM_MIN_INTERVAL_MINUTES: dict[str, int] = {"douyin": 30}


class AccountsError(RuntimeError):
    """台账文件不合法。"""


# ------------------------------------------------------------------ 时段窗口


def resolve_timezone(name: str) -> ZoneInfo:
    """取时区，缺 tzdata 时退回 UTC 而不是崩掉（容器里可能没装时区库）。"""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        logger.warning("时区 %r 不可用（缺 tzdata？），退回 UTC", name)
        return ZoneInfo("UTC")


def parse_window(raw: str) -> tuple[time, time]:
    """``"09:00-11:00"`` → ``(time(9,0), time(11,0))``。跨零点（``22:00-02:00``）合法。"""
    text = str(raw).strip()
    if "-" not in text:
        raise AccountsError(f"发布时段格式错误 {raw!r}，应形如 09:00-11:00")
    start_s, _, end_s = text.partition("-")
    try:
        start = time.fromisoformat(start_s.strip())
        end = time.fromisoformat(end_s.strip())
    except ValueError as exc:
        raise AccountsError(f"发布时段格式错误 {raw!r}: {exc}") from exc
    if start == end:
        raise AccountsError(f"发布时段 {raw!r} 起止相同，等于永不放行；要全天放行请留空")
    return start, end


def parse_windows(raw: Any) -> list[tuple[time, time]]:
    """解析 ``publish_windows``。空 / None = 全天放行。"""
    if not raw:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list | tuple):
        raise AccountsError(f"publish_windows 应为字符串列表，实际 {type(raw).__name__}")
    return [parse_window(item) for item in raw]


#: YAML / JSON 里"假"的各种写法。其余一律按缺省值处理
FALSEY: frozenset[str] = frozenset({"false", "0", "no", "off", "否", ""})
TRUTHY: frozenset[str] = frozenset({"true", "1", "yes", "on", "是"})


def _as_bool(value: object, *, default: bool) -> bool:
    """宽松解析布尔开关。认不出来就用 ``default``——安全侧的默认值不该被拼错关掉。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in TRUTHY:
        return True
    if text in FALSEY:
        return False
    return default


def _within(start: time, end: time, moment: time) -> bool:
    if start <= end:
        return start <= moment < end
    # 跨零点：22:00-02:00
    return moment >= start or moment < end


# ------------------------------------------------------------------ 调度策略


@dataclass(frozen=True)
class AccountPolicy:
    """一个账号的调度策略（全部来自 ``Account.extra``，都有缺省值）。"""

    account_id: str
    platform: str
    #: 每天产出几条稿。0 = 不自动生成（只接受人工 / dev 端点触发）
    daily_target: int = 0
    #: 发布时段窗口。空 = 全天放行
    windows: tuple[tuple[time, time], ...] = ()
    timezone: str = DEFAULT_TIMEZONE
    #: 两次发布最小间隔
    min_interval: timedelta = timedelta(0)
    #: 行内人设。空串表示回落到 prompts/accounts/<id>/persona.md
    persona: str = ""
    #: 日上限（已按平台硬顶夹过）
    daily_limit: int = 0
    #: 机器审核干净（block=0 且 warn=0）的稿子自动批准并排期（P12）。
    #: **默认关**：要让一个号自动出稿到排期，必须显式打开
    autopilot: bool = False
    #: 发布前要不要人点一下确认（P12）。**默认开，而且没有旁路**——
    #: 小红书 2026-03 公告封禁 AI 全托管账号，这是合规底线，不是偏好
    confirm_required: bool = True
    #: 推了确认卡这么多小时还没人点就自动驳回，释放排期槽位
    confirm_ttl_hours: int = 24

    @property
    def tzinfo(self) -> ZoneInfo:
        return resolve_timezone(self.timezone)

    def in_window(self, now: datetime) -> bool:
        """此刻是否落在发布时段内。没配窗口 = 永远为真。"""
        if not self.windows:
            return True
        local = now.astimezone(self.tzinfo).timetz()
        return any(_within(start, end, local.replace(tzinfo=None)) for start, end in self.windows)

    def next_window_start(self, now: datetime) -> datetime | None:
        """下一个窗口的开始时刻（UTC）。没配窗口返回 ``None``。"""
        if not self.windows:
            return None
        tz = self.tzinfo
        local = now.astimezone(tz)
        candidates: list[datetime] = []
        for offset in (0, 1):
            day = (local + timedelta(days=offset)).date()
            for start, _end in self.windows:
                moment = datetime.combine(day, start, tzinfo=tz)
                if moment > local:
                    candidates.append(moment)
        return min(candidates).astimezone(UTC) if candidates else None

    def window_text(self) -> str:
        if not self.windows:
            return "全天"
        return "、".join(f"{s.strftime('%H:%M')}-{e.strftime('%H:%M')}" for s, e in self.windows)


def policy_of(account: Account) -> AccountPolicy:
    """从 ``Account``（含 ``extra``）解析调度策略。非法配置降级为缺省并 warn。"""
    extra = dict(account.extra or {})
    platform = account.platform

    try:
        windows = parse_windows(extra.get("publish_windows"))
    except AccountsError as exc:
        logger.warning("账号 %s 的 publish_windows 非法，按全天放行：%s", account.id, exc)
        windows = []

    default_interval = PLATFORM_MIN_INTERVAL_MINUTES.get(platform)
    raw_interval = extra.get("min_interval_minutes", default_interval)
    if raw_interval is None:
        from core.config import get_settings

        interval = timedelta(seconds=get_settings().sw_min_publish_interval_seconds)
    else:
        try:
            interval = timedelta(minutes=max(int(raw_interval), 0))
        except (TypeError, ValueError):
            logger.warning("账号 %s 的 min_interval_minutes 非法：%r", account.id, raw_interval)
            interval = timedelta(minutes=default_interval or 0)
    # 平台缺省是**下限**：台账写得更松也不放行得更快（docs/POLICY.md 保守限频）
    if default_interval is not None:
        interval = max(interval, timedelta(minutes=default_interval))

    try:
        target = max(int(extra.get("daily_target") or 0), 0)
    except (TypeError, ValueError):
        logger.warning("账号 %s 的 daily_target 非法：%r", account.id, extra.get("daily_target"))
        target = 0

    ceiling = PLATFORM_DAILY_CEILING.get(platform)
    limit = int(account.daily_limit or 0)
    if ceiling is not None:
        limit = min(limit, ceiling)

    from core.config import get_settings

    settings = get_settings()
    try:
        ttl_hours = max(int(extra.get("confirm_ttl_hours") or settings.sw_confirm_ttl_hours), 0)
    except (TypeError, ValueError):
        logger.warning(
            "账号 %s 的 confirm_ttl_hours 非法：%r", account.id, extra.get("confirm_ttl_hours")
        )
        ttl_hours = settings.sw_confirm_ttl_hours

    return AccountPolicy(
        account_id=account.id,
        platform=platform,
        daily_target=target,
        windows=tuple(windows),
        timezone=str(extra.get("timezone") or default_timezone()),
        min_interval=interval,
        persona=str(extra.get("persona") or ""),
        daily_limit=limit,
        autopilot=_as_bool(extra.get("autopilot"), default=False),
        # 缺省为 True，且只有**显式**写 false 才关得掉：拼错一个字母就绕过合规闸门
        # 是绝对不能有的失败模式
        confirm_required=_as_bool(extra.get("confirm_required"), default=True),
        confirm_ttl_hours=ttl_hours,
    )


# ------------------------------------------------------------------ 台账解析


@dataclass
class AccountSpec:
    """``accounts.yaml`` 里的一条账号台账。"""

    id: str
    platform: str
    name: str
    daily_limit: int
    sidecar_endpoint: str | None = None
    profile_dir: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_columns(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "name": self.name,
            "daily_limit": self.daily_limit,
            "sidecar_endpoint": self.sidecar_endpoint,
            "profile_dir": self.profile_dir,
            "extra": self.extra,
        }


def _default_daily_limit(platform: str) -> int:
    from core.config import get_settings

    settings = get_settings()
    return {
        "xhs": settings.xhs_daily_limit,
        "douyin": settings.douyin_daily_limit,
        "wechat_mp": 1,
    }.get(platform, 1)


def parse_spec(raw: dict[str, Any]) -> AccountSpec:
    """把 YAML 的一条记录转成 :class:`AccountSpec`，顺带校验。"""
    from publishers.base import PLATFORMS

    if not isinstance(raw, dict):
        raise AccountsError(f"账号条目应为映射，实际 {type(raw).__name__}")
    account_id = str(raw.get("id") or "").strip()
    if not account_id:
        raise AccountsError("账号缺少 id")
    platform = str(raw.get("platform") or "").strip()
    if platform not in PLATFORMS:
        raise AccountsError(f"账号 {account_id} 的 platform={platform!r} 非法，允许 {PLATFORMS}")

    unknown = set(raw) - KNOWN_TOP_LEVEL_KEYS
    if unknown:
        logger.warning("账号 %s 有未识别的字段（已忽略）：%s", account_id, sorted(unknown))

    sidecar = raw.get("sidecar") or {}
    if not isinstance(sidecar, dict):
        raise AccountsError(f"账号 {account_id} 的 sidecar 应为映射")

    endpoint = str(raw.get("sidecar_endpoint") or sidecar.get("endpoint") or "").strip()
    if not endpoint and sidecar.get("port"):
        endpoint = f"http://localhost:{sidecar['port']}"

    # -- extra：台账管的键 + 显式 extra 块 ---------------------------------
    extra: dict[str, Any] = dict(raw.get("extra") or {})
    if raw.get("daily_target") is not None:
        extra["daily_target"] = int(raw["daily_target"])
    if raw.get("publish_windows") is not None:
        # 解析一次纯为校验；库里存原始字符串列表，人看得懂
        parse_windows(raw["publish_windows"])
        extra["publish_windows"] = [str(w) for w in raw["publish_windows"]]
    if raw.get("min_interval_minutes") is not None:
        extra["min_interval_minutes"] = int(raw["min_interval_minutes"])
    if raw.get("timezone"):
        extra["timezone"] = str(raw["timezone"])
    if raw.get("persona"):
        extra["persona"] = str(raw["persona"])
    if raw.get("autopilot") is not None:
        extra["autopilot"] = _as_bool(raw["autopilot"], default=False)
    if raw.get("confirm_required") is not None:
        extra["confirm_required"] = _as_bool(raw["confirm_required"], default=True)
    if raw.get("confirm_ttl_hours") is not None:
        extra["confirm_ttl_hours"] = max(int(raw["confirm_ttl_hours"]), 0)
    if platform == "xhs" and sidecar.get("token_env"):
        # token **只写环境变量名**，值永远不入库（docs/POLICY.md）
        xhs_extra = dict(extra.get("xhs") or {})
        xhs_extra["auth_token_env"] = str(sidecar["token_env"])
        extra["xhs"] = xhs_extra

    limit = raw.get("daily_limit")
    daily_limit = int(limit) if limit is not None else _default_daily_limit(platform)
    ceiling = PLATFORM_DAILY_CEILING.get(platform)
    if ceiling is not None and daily_limit > ceiling:
        logger.warning(
            "账号 %s 的 daily_limit=%d 超过平台硬顶 %d，已夹到硬顶",
            account_id,
            daily_limit,
            ceiling,
        )
        daily_limit = ceiling

    return AccountSpec(
        id=account_id,
        platform=platform,
        name=str(raw.get("name") or account_id),
        daily_limit=daily_limit,
        sidecar_endpoint=endpoint or None,
        profile_dir=str(raw["profile_dir"]) if raw.get("profile_dir") else None,
        extra=extra,
    )


def load_specs(path: Path | str | None = None) -> list[AccountSpec]:
    """读 ``accounts.yaml``。文件不存在返回空列表（不是错误：可以纯用 DB）。"""
    target = Path(path) if path is not None else accounts_file_path()
    if not target.is_file():
        logger.info("台账文件不存在，跳过：%s", target)
        return []
    data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    if "accounts" not in data:
        raise AccountsError(f"{target} 缺少顶层 accounts 列表")
    # ``accounts:`` 有键没值是**合法的空台账**（刚部署、还没加过号，工作台新建的
    # 第一个账号就是从这个状态开始的），不是错误
    rows = data.get("accounts") or []
    specs = [parse_spec(row) for row in rows]
    seen: set[str] = set()
    for spec in specs:
        if spec.id in seen:
            raise AccountsError(f"台账里 id 重复：{spec.id}")
        seen.add(spec.id)
    return specs


# ------------------------------------------------------------------ DB 同步


@dataclass
class SyncReport:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    #: DB 里有但台账里没有的账号（只报告，不删——历史内容还挂在它上面）
    orphans: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return len(self.created) + len(self.updated)

    def as_dict(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "orphans": self.orphans,
        }


def merge_extra(existing: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
    """台账 extra 覆盖库里的，只保留 :data:`RUNTIME_EXTRA_KEYS` 里的运行时键。"""
    kept = {k: v for k, v in (existing or {}).items() if k in RUNTIME_EXTRA_KEYS}
    return {**kept, **incoming}


def sync_accounts(
    session: Session,
    specs: list[AccountSpec],
    *,
    dry_run: bool = False,
) -> SyncReport:
    """幂等 upsert。**不碰** ``Account.status``。"""
    report = SyncReport()
    known = {spec.id for spec in specs}

    for spec in specs:
        account = session.get(Account, spec.id)
        columns = spec.as_columns()
        if account is None:
            report.created.append(spec.id)
            if not dry_run:
                session.add(
                    Account(
                        id=spec.id,
                        status="ok",  # 只在**新建**时设，之后一律由巡检维护
                        **{**columns, "extra": merge_extra(None, spec.extra)},
                    )
                )
            continue

        columns["extra"] = merge_extra(account.extra, spec.extra)
        diff = {k: v for k, v in columns.items() if getattr(account, k) != v}
        if not diff:
            report.unchanged.append(spec.id)
            continue
        report.updated.append(spec.id)
        if not dry_run:
            for key, value in diff.items():
                setattr(account, key, value)

    for account_id in session.scalars(select(Account.id).order_by(Account.id)):
        if account_id not in known:
            report.orphans.append(account_id)

    if not dry_run:
        session.flush()
    return report


def diff_report(session: Session, specs: list[AccountSpec]) -> SyncReport:
    """只算差异不落库（``check`` 子命令 / preflight 用）。"""
    return sync_accounts(session, specs, dry_run=True)


# ---------------------------------------------------------------------- CLI


def _cmd_sync(args: argparse.Namespace) -> int:
    from core import db

    db.configure()
    db.init_db()
    specs = load_specs(args.file)
    if not specs:
        print(f"台账为空或不存在：{args.file or accounts_file_path()}")
        return 1
    with db.session_scope() as session:
        report = sync_accounts(session, specs, dry_run=args.dry_run)
    prefix = "[dry-run] " if args.dry_run else ""
    print(f"DB: {redact_db_url(db.current_url() or '(未配置)')}")
    print(
        f"{prefix}新建 {len(report.created)} · 更新 {len(report.updated)} · "
        f"未变 {len(report.unchanged)} · 台账外账号 {len(report.orphans)}"
    )
    for label, ids in (
        ("新建", report.created),
        ("更新", report.updated),
        ("台账外（未删除）", report.orphans),
    ):
        for account_id in ids:
            print(f"  {label}: {account_id}")
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    from core import db

    db.configure()
    db.init_db()
    specs = load_specs(args.file)
    with db.session_scope() as session:
        report = diff_report(session, specs)
    print(f"DB: {redact_db_url(db.current_url() or '(未配置)')}")
    if report.changed == 0:
        print(f"台账与 DB 一致（{len(report.unchanged)} 个账号）")
        return 0
    print(
        f"台账与 DB 不一致：待新建 {report.created}，待更新 {report.updated}。"
        "请先跑 `uv run python -m core.accounts sync`"
    )
    return 1


def redact_db_url(url: str) -> str:
    """把 DB URL 里的密码打掉再打印（SQLite 没有凭据，Postgres 有）。"""
    if "@" not in url or "://" not in url:
        return url
    scheme, _, rest = url.partition("://")
    creds, _, host = rest.rpartition("@")
    user = creds.partition(":")[0]
    return f"{scheme}://{user}:***@{host}"


def _cmd_list(args: argparse.Namespace) -> int:
    from core import db

    db.configure()
    db.init_db()
    # 先把库路径打出来：不带 SW_DATABASE_URL 跑时读的是默认库，
    # 很容易让人以为"明明 sync 过了怎么是空的"
    print(f"DB: {redact_db_url(db.current_url() or '(未配置)')}")
    with db.session_scope() as session:
        accounts = list(session.scalars(select(Account).order_by(Account.platform, Account.id)))
        if not accounts:
            print("DB 里没有任何账号。先跑 `uv run python -m core.accounts sync`")
            return 1
        for account in accounts:
            policy = policy_of(account)
            print(
                f"{account.id:<20} {account.platform:<10} {account.status:<14} "
                f"日上限 {policy.daily_limit:<3} 目标 {policy.daily_target} "
                f"窗口 {policy.window_text()} 间隔 {policy.min_interval.total_seconds() / 60:.0f}m"
            )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m core.accounts", description="账号台账（accounts.yaml）与 DB 的同步工具"
    )
    parser.add_argument(
        "--file", type=Path, default=None, help="台账路径，默认 SW_ACCOUNTS_FILE 或 accounts.yaml"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", help="幂等 upsert 到 DB（不覆盖运行时 status）")
    p_sync.add_argument("--dry-run", action="store_true", help="只报差异不落库")
    p_sync.set_defaults(func=_cmd_sync)

    sub.add_parser("check", help="校验台账与 DB 是否一致（不一致退出码 1）").set_defaults(
        func=_cmd_check
    )
    sub.add_parser("list", help="列出 DB 里的账号与调度策略").set_defaults(func=_cmd_list)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except AccountsError as exc:
        print(f"台账错误：{exc}", file=sys.stderr)
        return 2


def today_in(tz_name: str, now: datetime | None = None) -> date:
    """某时区下的"今天"。统计页按人的日历分组时用。"""
    moment = now or utcnow()
    return moment.astimezone(resolve_timezone(tz_name)).date()


__all__ = [
    "DEFAULT_ACCOUNTS_FILE",
    "DEFAULT_TIMEZONE",
    "FALSEY",
    "PLATFORM_DAILY_CEILING",
    "PLATFORM_MIN_INTERVAL_MINUTES",
    "RUNTIME_EXTRA_KEYS",
    "TRUTHY",
    "AccountPolicy",
    "AccountSpec",
    "AccountsError",
    "SyncReport",
    "accounts_file_path",
    "build_parser",
    "default_timezone",
    "diff_report",
    "load_specs",
    "main",
    "merge_extra",
    "parse_spec",
    "parse_window",
    "parse_windows",
    "policy_of",
    "redact_db_url",
    "resolve_timezone",
    "sync_accounts",
    "today_in",
]

if __name__ == "__main__":
    raise SystemExit(main())
