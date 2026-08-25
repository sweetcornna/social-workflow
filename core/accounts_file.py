"""``accounts.yaml`` 的**保留注释**读写（工作台"添加账号"回写台账用）。

为什么不直接 ``yaml.safe_dump`` 整个文件
----------------------------------------
台账是给人看的：文件头那段"这几个字段什么意思"的说明、每个账号旁边"为什么是这个
值"的旁注，是运维排障时唯一的说明书。``safe_dump`` 一轮下来全没了，而台账文件恰恰
是最不该被机器改花的那一份。

所以这里做**块级编辑**：把文件按顶层 ``accounts:`` 列表切成一块一块，只重写被改动
的那一块，其余字节原样保留。被改的那一块自己的旁注会丢——那是"改过它"的合理代价，
其余账号与文件头一个字符都不动。

红线：台账里只写拓扑、限频与调度策略。**任何凭据都不写这里**（小红书的 token 只写
环境变量名，见 ``core/accounts.py`` 的 ``parse_spec``）。
"""

from __future__ import annotations

import contextlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

#: 顶层 ``accounts:`` 键。文件里必须有这一行，否则 ``load_specs`` 也读不出东西
ACCOUNTS_KEY_RE = re.compile(r"^accounts:\s*(#.*)?$")
#: 列表项的起始行，如 ``  - id: xhs-demo-01``
ITEM_RE = re.compile(r"^(\s*)-\s")
#: 空行或纯注释行
FILLER_RE = re.compile(r"^\s*(#.*)?$")

DEFAULT_INDENT = "  "

#: 台账不存在时新建用的文件头。写得像人写的，因为接下来它就是人要读的那份
NEW_FILE_HEADER = """\
# 账号台账。凭据不写在这里，只写拓扑、限频与调度策略。
#
# 这份文件既可以手工编辑，也会被工作台的「添加账号」向导回写（只重写被改动的条目，
# 其余注释原样保留）。改完记得让台账与 DB 保持一致：
#     uv run python -m core.accounts sync
#     uv run python -m core.accounts check
accounts:
"""


class LedgerError(RuntimeError):
    """台账文件结构不支持编辑（缺 ``accounts:`` 顶层键等）。"""


@dataclass
class LedgerEntry:
    """台账里的一条账号：YAML 正文行 + 它后面那段空行/注释。"""

    #: 解析出来的映射（``yaml.safe_load`` 的结果）
    raw: dict[str, Any]
    #: 该条目的 YAML 正文行（含 ``  - `` 前缀，不含结尾换行）
    lines: list[str] = field(default_factory=list)
    #: 条目之后、下一条目之前的空行与注释行。重写条目时原样留着
    gap: list[str] = field(default_factory=list)

    @property
    def account_id(self) -> str:
        return str(self.raw.get("id") or "")


@dataclass
class LedgerDocument:
    """整份台账：文件头（含 ``accounts:`` 行）+ 若干条目。"""

    header: list[str] = field(default_factory=list)
    entries: list[LedgerEntry] = field(default_factory=list)
    indent: str = DEFAULT_INDENT
    #: 原文件是否以换行结尾（写回时保持一致，免得 diff 里多一行"\\ No newline"）
    trailing_newline: bool = True

    def find(self, account_id: str) -> LedgerEntry | None:
        return next((e for e in self.entries if e.account_id == account_id), None)

    def ids(self) -> list[str]:
        return [e.account_id for e in self.entries if e.account_id]

    def upsert(self, entry: dict[str, Any]) -> None:
        """按 ``id`` 覆盖或追加一条。只重写这一条，别的字节不动。"""
        account_id = str(entry.get("id") or "")
        if not account_id:
            raise LedgerError("台账条目缺少 id")
        lines = render_entry(entry, indent=self.indent)
        existing = self.find(account_id)
        if existing is not None:
            existing.raw = dict(entry)
            existing.lines = lines
            return
        # 追加：让新条目与前一条之间空一行，读起来才是"一条一条"而不是一堵墙
        if self.entries and not any(line.strip() == "" for line in self.entries[-1].gap):
            self.entries[-1].gap.append("")
        self.entries.append(LedgerEntry(raw=dict(entry), lines=lines))

    def remove(self, account_id: str) -> bool:
        before = len(self.entries)
        self.entries = [e for e in self.entries if e.account_id != account_id]
        return len(self.entries) != before

    def render(self) -> str:
        out: list[str] = list(self.header)
        for entry in self.entries:
            out.extend(entry.lines)
            out.extend(entry.gap)
        text = "\n".join(out)
        return f"{text}\n" if self.trailing_newline and not text.endswith("\n") else text

    def specs_payload(self) -> dict[str, Any]:
        """等价于 ``yaml.safe_load(render())``，用于写回前先自检一遍。"""
        return {"accounts": [dict(e.raw) for e in self.entries]}


# --------------------------------------------------------------------- 解析


def parse_document(text: str) -> LedgerDocument:
    """把台账文本切成"文件头 + 条目块"。

    条目的边界就是行首的 ``- ``（在 ``accounts:`` 之后、同一缩进层级上）。
    条目之后紧跟的空行与注释归入该条目的 ``gap``——这样删条目时不会留下孤儿注释，
    重写条目时又不会把"下一条为什么这么配"的注释一起吃掉。
    """
    lines = text.split("\n")
    if text.endswith("\n"):
        lines = lines[:-1]
        trailing_newline = True
    else:
        trailing_newline = text != ""

    key_index = next((i for i, line in enumerate(lines) if ACCOUNTS_KEY_RE.match(line)), None)
    if key_index is None:
        raise LedgerError("台账缺少顶层 accounts: 列表，无法安全地自动编辑")

    first_item: int | None = None
    indent = DEFAULT_INDENT
    for i in range(key_index + 1, len(lines)):
        match = ITEM_RE.match(lines[i])
        if match:
            first_item = i
            indent = match.group(1) or ""
            break
        if not FILLER_RE.match(lines[i]):
            # accounts: 下面第一个非空非注释行不是列表项 —— 多半是 `accounts: []`
            # 或者别的写法，不冒险动它
            raise LedgerError(f"accounts: 之后第 {i + 1} 行不是列表项，无法安全地自动编辑")

    if first_item is None:
        return LedgerDocument(
            header=lines, entries=[], indent=DEFAULT_INDENT, trailing_newline=trailing_newline
        )

    starts = [
        i
        for i in range(first_item, len(lines))
        if (m := ITEM_RE.match(lines[i])) and (m.group(1) or "") == indent
    ]
    entries: list[LedgerEntry] = []
    for pos, start in enumerate(starts):
        end = starts[pos + 1] if pos + 1 < len(starts) else len(lines)
        block = lines[start:end]
        # 块尾的空行 / 注释行划给 gap
        split = len(block)
        while split > 0 and FILLER_RE.match(block[split - 1]):
            split -= 1
        body, gap = block[:split], block[split:]
        entries.append(LedgerEntry(raw=parse_entry(body), lines=body, gap=gap))

    return LedgerDocument(
        header=lines[:first_item],
        entries=entries,
        indent=indent or DEFAULT_INDENT,
        trailing_newline=trailing_newline,
    )


def parse_entry(lines: list[str]) -> dict[str, Any]:
    """解析单个条目块。解析不出映射就抛，免得后面用一个空 dict 把条目改没了。"""
    try:
        parsed = yaml.safe_load("\n".join(lines))
    except yaml.YAMLError as exc:
        raise LedgerError(f"台账条目不是合法 YAML：{exc}") from exc
    if not isinstance(parsed, list) or len(parsed) != 1 or not isinstance(parsed[0], dict):
        raise LedgerError(f"台账条目解析异常：{lines[:1]}")
    return parsed[0]


def render_entry(entry: dict[str, Any], *, indent: str = DEFAULT_INDENT) -> list[str]:
    """一条账号 → YAML 行。字段顺序按传入的 dict 顺序（人读起来有主次）。"""
    dumped = yaml.safe_dump(
        [entry],
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=100,
    ).rstrip("\n")
    return [f"{indent}{line}" if line.strip() else line for line in dumped.split("\n")]


# --------------------------------------------------------------------- 读写


def read_document(path: Path | str) -> LedgerDocument:
    """读台账。文件不存在时返回一份只有文件头的空文档（第一次添加账号就是这样）。"""
    target = Path(path)
    if not target.is_file():
        return parse_document(NEW_FILE_HEADER)
    return parse_document(target.read_text(encoding="utf-8"))


def write_document(path: Path | str, doc: LedgerDocument) -> None:
    """原子写回（临时文件 + ``os.replace``）。

    写前先把渲染结果重新解析一遍：宁可在这里抛，也不要把一份读不回来的台账落到盘上
    —— 那会让 core 下次启动时同步失败。
    """
    target = Path(path)
    text = doc.render()
    reparsed = yaml.safe_load(text) or {}
    if not isinstance(reparsed, dict) or "accounts" not in reparsed:
        raise LedgerError("写回前自检失败：渲染结果里没有 accounts 列表")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)


# --------------------------------------------------------------------- 端口


PORT_RE = re.compile(r":(\d{2,5})(?:/|$)")


def declared_ports(doc: LedgerDocument) -> set[int]:
    """台账里已经被占用的宿主机端口（``sidecar.port`` 与 ``sidecar_endpoint`` 两处）。"""
    ports: set[int] = set()
    for entry in doc.entries:
        sidecar = entry.raw.get("sidecar")
        if isinstance(sidecar, dict) and sidecar.get("port"):
            with contextlib.suppress(TypeError, ValueError):
                ports.add(int(sidecar["port"]))
        endpoint = str(entry.raw.get("sidecar_endpoint") or "")
        match = PORT_RE.search(endpoint)
        if match:
            ports.add(int(match.group(1)))
    return ports


__all__ = [
    "NEW_FILE_HEADER",
    "LedgerDocument",
    "LedgerEntry",
    "LedgerError",
    "declared_ports",
    "parse_document",
    "parse_entry",
    "read_document",
    "render_entry",
    "write_document",
]
