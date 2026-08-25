"""账号的增删改：台账回写 + DB 同步 + sidecar 接管 + 手动出稿。

为什么要单独一层
----------------
"在工作台里加一个账号"这件事牵三个地方：``accounts.yaml``（台账，人要读）、
``accounts`` 表（调度器要读）、以及小红书的 sidecar 容器（一账号一个）。三者
**必须同时成立**，否则就会出现 P10 之前那种局面：库里有号但台账没有，
``python -m core.accounts check`` 一跑就红，重新部署一次账号就没了。

所以这里的写法是：**永远先写台账，再从台账同步进 DB**，而不是分别写两遍。
这样"台账与 DB 不许漂移"这条红线是**由构造保证的**，不是靠两处代码小心翼翼地对齐。
中途任何一步失败都会把台账文件回滚成动手之前的字节。

红线不变
--------
- 凭据不入库、不走前端：小红书 token 只在台账里留**环境变量名**；
- 一账号一容器一 volume 一端口，端口由 :func:`allocate_port` 分配，冲突直接报错；
- 停用不删账号：历史内容与审计日志还挂在它上面。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core import accounts_file, sidecars
from core.accounts import (
    PLATFORM_DAILY_CEILING,
    AccountsError,
    accounts_file_path,
    default_timezone,
    load_specs,
    parse_spec,
    parse_windows,
    sync_accounts,
)
from core.errors import AppError
from core.models import Account, ContentItem, utcnow
from publishers.base import PLATFORMS

logger = logging.getLogger("social_workflow.account_admin")

#: 账号 id 的平台前缀。沿用台账里既有的写法（``wechat-demo-01`` 而不是 ``wechat_mp-…``）
ID_PREFIX: dict[str, str] = {"xhs": "xhs", "douyin": "douyin", "wechat_mp": "wechat"}

#: 允许的 id 字符集。要同时当 docker 容器名、volume 名与 URL 片段用，从严
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")

#: 一天最多手动出几条稿 = ``max(daily_target, 1) × 这个倍数``。
#: 手动出稿会真的调 LLM（还可能渲染视频），连点两下就是几万 token，必须有闸门
MANUAL_GENERATE_FACTOR = 2

WINDOW_EXAMPLE = '形如 "09:00-11:00"；跨零点写 "22:00-02:00"；留空 = 全天放行'


# --------------------------------------------------------------------- 入参


@dataclass
class AccountDraft:
    """新建 / 修改账号的入参（已经过 API 层的 pydantic 校验）。"""

    platform: str = ""
    name: str = ""
    identity_hint: str | None = None
    publish_windows: list[str] | None = None
    min_interval_minutes: int | None = None
    daily_limit: int | None = None
    daily_target: int | None = None
    timezone: str | None = None
    persona: str | None = None
    # -- P12 --
    #: 机器审核干净的稿子自动批准并排期。默认关，显式打开才生效
    autopilot: bool | None = None
    #: 发布前要不要人点一下。默认开，**没有旁路**（合规底线，见 docs/POLICY.md）
    confirm_required: bool | None = None


# --------------------------------------------------------------------- 校验


def validate_platform(platform: str) -> str:
    if platform not in PLATFORMS:
        raise AppError(
            422,
            "invalid_platform",
            f"platform={platform!r} 不认识，只能是 {'、'.join(sorted(PLATFORMS))}",
        )
    return platform


def validate_windows(windows: list[str] | None) -> list[str]:
    """校验发布时段。非法时把**例子**一起给出去，不要只说"格式错误"。"""
    if not windows:
        return []
    cleaned = [str(w).strip() for w in windows if str(w).strip()]
    try:
        parse_windows(cleaned)
    except AccountsError as exc:
        raise AppError(
            422,
            "invalid_window",
            f"{exc}。{WINDOW_EXAMPLE}",
            detail={"example": ["09:00-11:00", "19:00-22:30"], "got": cleaned},
        ) from exc
    return cleaned


def validate_daily_limit(platform: str, daily_limit: int | None) -> int | None:
    """日上限不许超过平台硬顶。**报错而不是悄悄夹到硬顶**——悄悄改会让人以为配上了。"""
    if daily_limit is None:
        return None
    ceiling = PLATFORM_DAILY_CEILING.get(platform)
    if ceiling is not None and daily_limit > ceiling:
        raise AppError(
            422,
            "limit_above_ceiling",
            f"{platform} 的日上限硬顶是 {ceiling} 条（docs/POLICY.md 的保守限频口径），"
            f"填的 {daily_limit} 超了。",
            detail={"ceiling": ceiling, "got": daily_limit},
        )
    return daily_limit


def validate_timezone(name: str | None) -> str:
    if not name:
        return ""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise AppError(
            422, "invalid_timezone", f"时区 {name!r} 这台机器不认识（例：Asia/Shanghai）"
        ) from exc
    return name


# --------------------------------------------------------------------- id


def slugify(text: str) -> str:
    """名字 → id 片段。中文没有稳妥的音译，直接丢掉，交给序号兜底。"""
    ascii_only = re.sub(r"[^a-zA-Z0-9]+", "-", text or "").strip("-").lower()
    return re.sub(r"-{2,}", "-", ascii_only)


def allocate_id(platform: str, name: str, taken: set[str]) -> str:
    """生成账号 id：``<平台前缀>-<名字 slug 或序号>``，撞车就往后加序号。

    名字是中文时 slug 会是空的（这是常态），退回 ``xhs-01`` 这样的两位序号——
    比音译出一串没人认得的拼音好，也比 uuid 好：这个 id 会出现在容器名、volume 名、
    日志与台账里，人得念得出来。
    """
    prefix = ID_PREFIX.get(platform, platform)
    slug = slugify(name)
    # 名字里已经带了平台前缀就别再套一层，不然会得到 xhs-xhs-主号 这种念不出口的 id
    if slug.startswith(f"{prefix}-"):
        slug = slug[len(prefix) + 1 :]
    if slug and ID_RE.match(f"{prefix}-{slug}"):
        base = f"{prefix}-{slug}"
        if base not in taken:
            return base
        for n in range(2, 100):
            candidate = f"{base}-{n:02d}"
            if candidate not in taken:
                return candidate
    for n in range(1, 1000):
        candidate = f"{prefix}-{n:02d}"
        if candidate not in taken:
            return candidate
    raise AppError(409, "id_exhausted", f"{prefix}-* 的编号用完了，先清理一下台账")


def taken_ids(session: Session, doc: accounts_file.LedgerDocument) -> set[str]:
    """台账里的 + 库里的。两处都要看：库里可能有台账外的历史账号。"""
    return set(doc.ids()) | set(session.scalars(select(Account.id)))


def taken_ports(session: Session, doc: accounts_file.LedgerDocument) -> set[int]:
    ports = accounts_file.declared_ports(doc)
    for endpoint in session.scalars(
        select(Account.sidecar_endpoint).where(Account.sidecar_endpoint.is_not(None))
    ):
        match = accounts_file.PORT_RE.search(str(endpoint))
        if match:
            ports.add(int(match.group(1)))
    return ports


def token_env_name(account_id: str) -> str:
    """该账号 sidecar 的 ``AUTH_TOKEN`` 环境变量名。台账里只写这个名字，不写值。"""
    return "XHS_TOKEN_" + "".join(c if c.isalnum() else "_" for c in account_id).upper()


# --------------------------------------------------------------- 台账条目组装


def build_entry(
    account_id: str,
    draft: AccountDraft,
    *,
    existing: dict[str, Any] | None = None,
    port: int | None = None,
) -> dict[str, Any]:
    """拼一条台账记录。字段顺序 = 人读的顺序，别按字母排。

    ``existing`` 非空表示在改一条已有记录：没传的字段保持原样（PATCH 语义）。
    """
    old = dict(existing or {})
    platform = str(old.get("platform") or draft.platform)
    entry: dict[str, Any] = {
        "id": account_id,
        "platform": platform,
        "name": draft.name or str(old.get("name") or account_id),
    }

    daily_limit = draft.daily_limit if draft.daily_limit is not None else old.get("daily_limit")
    if daily_limit is not None:
        entry["daily_limit"] = int(daily_limit)

    daily_target = draft.daily_target if draft.daily_target is not None else old.get("daily_target")
    if daily_target is not None:
        entry["daily_target"] = int(daily_target)

    windows = (
        draft.publish_windows if draft.publish_windows is not None else old.get("publish_windows")
    )
    if windows:
        entry["publish_windows"] = [str(w) for w in windows]

    interval = (
        draft.min_interval_minutes
        if draft.min_interval_minutes is not None
        else old.get("min_interval_minutes")
    )
    if interval is not None:
        entry["min_interval_minutes"] = int(interval)

    timezone = draft.timezone if draft.timezone is not None else old.get("timezone")
    if timezone:
        entry["timezone"] = str(timezone)

    persona = draft.persona if draft.persona is not None else old.get("persona")
    if persona:
        entry["persona"] = str(persona)

    # P12 的两个开关都是布尔，**不能用真值判断**：``if autopilot:`` 会让"显式关掉"
    # 和"没传"变成同一件事，于是关不掉。一律拿 ``is not None`` 判有没有传
    autopilot = draft.autopilot if draft.autopilot is not None else old.get("autopilot")
    if autopilot is not None:
        entry["autopilot"] = bool(autopilot)

    confirm_required = (
        draft.confirm_required
        if draft.confirm_required is not None
        else old.get("confirm_required")
    )
    if confirm_required is not None:
        entry["confirm_required"] = bool(confirm_required)

    if platform == "xhs":
        sidecar = dict(old.get("sidecar") or {})
        if port is not None:
            sidecar["port"] = int(port)
        sidecar.setdefault("volume", sidecars.volume_name(account_id))
        sidecar.setdefault("token_env", token_env_name(account_id))
        entry["sidecar"] = sidecar

    if platform == "douyin":
        entry["profile_dir"] = str(old.get("profile_dir") or f"./profiles/douyin/{account_id}")

    extra = dict(old.get("extra") or {})
    hint = draft.identity_hint if draft.identity_hint is not None else extra.get("identity_hint")
    if hint:
        extra["identity_hint"] = str(hint)
    if extra:
        entry["extra"] = extra

    return entry


# --------------------------------------------------------------------- 写入


def _commit_ledger(session: Session, doc: accounts_file.LedgerDocument) -> None:
    """写台账 → 从台账同步进 DB。任何一步炸掉都把文件回滚成动手之前的样子。

    顺序刻意是"先文件后库"：文件是唯一真相，库是它的投影。反过来做的话，
    一旦写库成功、写文件失败，下次 ``check`` 就红，而且没人知道是谁改的。
    """
    path = accounts_file_path()
    before = path.read_text(encoding="utf-8") if path.is_file() else None
    accounts_file.write_document(path, doc)
    try:
        specs = load_specs(path)
        sync_accounts(session, specs)
        session.commit()
    except Exception:
        session.rollback()
        if before is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(before, encoding="utf-8")
        raise


def create_account(session: Session, draft: AccountDraft) -> tuple[Account, list[str]]:
    """建账号：分配 id / 端口 → 写台账 → 同步 DB →（小红书）拉起 sidecar。

    返回 ``(账号, 警告列表)``。sidecar 起不来**不算创建失败**——账号已经在台账与库里
    了，容器起不起得来是另一件事，如实报出来让人去看那台机器，比整个回滚有用。
    """
    platform = validate_platform(draft.platform)
    name = (draft.name or "").strip()
    if not name:
        raise AppError(422, "validation_error", "name 不能为空：这是账号在工作台里的显示名")
    if platform == "douyin" and not (draft.identity_hint or "").strip():
        raise AppError(
            422,
            "identity_hint_required",
            "抖音账号必须填 identity_hint（创作者中心显示的昵称）："
            "发布前会读页面比对，这是防发错号的唯一依据。",
        )
    windows = validate_windows(draft.publish_windows)
    validate_daily_limit(platform, draft.daily_limit)
    validate_timezone(draft.timezone)

    doc = accounts_file.read_document(accounts_file_path())
    account_id = allocate_id(platform, name, taken_ids(session, doc))

    port: int | None = None
    if platform == "xhs":
        port = sidecars.allocate_port(taken_ports(session, doc))

    resolved = AccountDraft(
        platform=platform,
        name=name,
        identity_hint=draft.identity_hint,
        publish_windows=windows,
        min_interval_minutes=draft.min_interval_minutes,
        daily_limit=draft.daily_limit,
        daily_target=draft.daily_target if draft.daily_target is not None else 0,
        timezone=draft.timezone or default_timezone(),
        persona=draft.persona,
    )
    entry = build_entry(account_id, resolved, port=port)
    _precheck(entry)

    doc.upsert(entry)
    _commit_ledger(session, doc)

    account = session.get(Account, account_id)
    if account is None:  # pragma: no cover - sync 成功却查不到只可能是库坏了
        raise AppError(500, "internal_error", f"台账已写入但库里查不到 {account_id}")

    warnings: list[str] = []
    if platform == "xhs":
        warnings.extend(_bring_up_sidecar(account))
    return account, warnings


def _precheck(entry: dict[str, Any]) -> None:
    """写文件之前先用台账自己的解析器过一遍，把错误挡在动盘之前。"""
    try:
        parse_spec(entry)
    except AccountsError as exc:
        raise AppError(422, "invalid_account", f"账号配置不合法：{exc}") from exc


def _bring_up_sidecar(account: Account) -> list[str]:
    """建完账号顺手把 sidecar 拉起来。失败只回警告，不回滚账号。"""
    driver = sidecars.get_driver()
    if driver.name == "none":
        return [
            "SW_SIDECAR_DRIVER=none：账号已建好，但 core 不接管容器，"
            "sidecar 未接入。要扫码得先在服务器上把驱动改成 docker，或手工起容器。"
        ]
    try:
        sidecars.act(account, "start", driver=driver)
    except sidecars.SidecarError as exc:
        logger.warning("账号 %s 的 sidecar 没起来：%s", account.id, exc)
        return [f"账号已建好，但 sidecar 没起来：{exc}"]
    return []


def update_account(session: Session, account: Account, draft: AccountDraft) -> Account:
    """改账号：只动台账里那一条，platform 与 id 不许改。"""
    if draft.publish_windows is not None:
        draft.publish_windows = validate_windows(draft.publish_windows)
    validate_daily_limit(account.platform, draft.daily_limit)
    validate_timezone(draft.timezone)
    if draft.name is not None and draft.name.strip() == "":
        raise AppError(422, "validation_error", "name 不能改成空")

    doc = accounts_file.read_document(accounts_file_path())
    existing = doc.find(account.id)
    # 库里有、台账里没有（历史遗留 / dev seed 造的号）：顺手补一条进去，
    # 把 `check` 一直在报的那种漂移修掉
    existing_raw = _entry_from_account(account) if existing is None else dict(existing.raw)

    entry = build_entry(account.id, draft, existing=existing_raw)
    _precheck(entry)
    doc.upsert(entry)
    _commit_ledger(session, doc)

    refreshed = session.get(Account, account.id)
    assert refreshed is not None
    return refreshed


def _entry_from_account(account: Account) -> dict[str, Any]:
    """库里已有的账号 → 台账条目（补台账时用）。"""
    extra = dict(account.extra or {})
    entry: dict[str, Any] = {
        "id": account.id,
        "platform": account.platform,
        "name": account.name,
        "daily_limit": int(account.daily_limit or 0),
    }
    for key in ("daily_target", "publish_windows", "min_interval_minutes", "timezone", "persona"):
        if extra.get(key):
            entry[key] = extra[key]
    # 布尔开关单独一轮：``extra.get(key)`` 为 False 时上面那个循环会当它不存在，
    # 于是"显式关掉"写不回台账
    for key in ("autopilot", "confirm_required"):
        if extra.get(key) is not None:
            entry[key] = bool(extra[key])
    if extra.get("confirm_ttl_hours") is not None:
        entry["confirm_ttl_hours"] = int(extra["confirm_ttl_hours"])
    if account.platform == "xhs":
        sidecar: dict[str, Any] = {
            "volume": sidecars.volume_name(account.id),
            "token_env": token_env_name(account.id),
        }
        match = accounts_file.PORT_RE.search(account.sidecar_endpoint or "")
        if match:
            sidecar["port"] = int(match.group(1))
        entry["sidecar"] = sidecar
    if account.profile_dir:
        entry["profile_dir"] = account.profile_dir
    leftovers = {
        k: v
        for k, v in extra.items()
        if k
        not in {
            "daily_target",
            "publish_windows",
            "min_interval_minutes",
            "timezone",
            "persona",
            "autopilot",
            "confirm_required",
            "confirm_ttl_hours",
            "xhs",
            "seeded",
            "insights_updated_at",
            "insights_error",
        }
    }
    if leftovers:
        entry["extra"] = leftovers
    return entry


# --------------------------------------------------------------- 手动出稿闸门


def generated_today(session: Session, account_id: str, *, now: datetime | None = None) -> int:
    """今天（UTC 日）为这个账号生成过几条内容。预算闸门也按 UTC 日切，口径一致。

    这是本仓库**第三种"今天"**，别把它并到限频那一种去（P11.3）：它挡的是
    "人在工作台连点出稿按钮"——每点一次都真的调模型烧 token，所以它和
    :class:`~core.budget.BudgetGuard` 是一家的（UTC 日），而不是和
    ``core.ratelimit`` 的日上限（账号本地日）一家。三种口径的对照见
    ``core/ratelimit.py`` 模块文档与 ``docs/OPS.md``。
    """
    from core.ratelimit import utc_day_start

    moment = now or utcnow()
    return int(
        session.scalar(
            select(func.count())
            .select_from(ContentItem)
            .where(
                ContentItem.account_id == account_id,
                ContentItem.created_at >= utc_day_start(moment),
            )
        )
        or 0
    )


def manual_generate_cap(account: Account) -> int:
    from core.accounts import policy_of

    return max(policy_of(account).daily_target, 1) * MANUAL_GENERATE_FACTOR


def check_generate_allowed(session: Session, account: Account) -> tuple[int, int]:
    """手动出稿的三道闸门：账号状态、当日条数、token 预算。返回 ``(今日已出, 上限)``。"""
    from core.budget import BudgetGuard, CostKind
    from core.state_machine import AccountStatus

    if account.status == AccountStatus.BANNED:
        raise AppError(409, "account_banned", f"账号 {account.id} 已封禁，不出稿。")
    if account.status == AccountStatus.SUSPENDED:
        raise AppError(409, "account_suspended", f"账号 {account.id} 已停用。先点「启用」再出稿。")

    used = generated_today(session, account.id)
    cap = manual_generate_cap(account)
    if used >= cap:
        raise AppError(
            429,
            "generate_limit",
            f"这个号今天已经出了 {used} 条稿，达到手动上限 {cap} 条"
            f"（= max(daily_target, 1) × {MANUAL_GENERATE_FACTOR}）。"
            "出稿会真的调模型烧 token，明天再来，或先把 daily_target 调高。",
            detail={"used_today": used, "cap": cap},
        )

    guard = BudgetGuard(session)
    if guard.is_exhausted(CostKind.TOKENS):
        raise AppError(
            429,
            "budget_exhausted",
            "今天的 token 预算已经用完了（闸门按 UTC 日重置）。"
            "要现在就出稿，去调 DAILY_TOKEN_BUDGET。",
            detail=guard.snapshot(),
        )
    return used, cap


__all__ = [
    "ID_PREFIX",
    "MANUAL_GENERATE_FACTOR",
    "AccountDraft",
    "allocate_id",
    "build_entry",
    "check_generate_allowed",
    "create_account",
    "generated_today",
    "manual_generate_cap",
    "slugify",
    "taken_ids",
    "taken_ports",
    "token_env_name",
    "update_account",
    "validate_daily_limit",
    "validate_platform",
    "validate_timezone",
    "validate_windows",
]
