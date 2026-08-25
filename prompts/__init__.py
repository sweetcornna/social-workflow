"""Prompt 库：从 ``.md`` 文件加载并做 ``{{变量}}`` 替换。

**Prompt 是版本化资产**：改动走 git diff，禁止在代码里拼长 prompt 字符串。
渲染刻意不用 Jinja2——prompt 里大量出现 ``{`` ``}``（JSON 示例、正则），
Jinja 的 ``{{ }}`` 之外还会解析 ``{% %}`` 与过滤器，误伤概率高。
这里只做最朴素的 ``{{name}}`` 全字符串替换，行为可预测。
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent
#: 账号人设目录：prompts/accounts/<account_id>/persona.md
ACCOUNTS_DIR = PROMPTS_DIR / "accounts"

_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


class PromptNotFound(FileNotFoundError):
    """请求的 prompt 文件不存在。"""


class PromptRenderError(ValueError):
    """渲染时缺变量。缺变量必须报错——静默留下 ``{{x}}`` 会被当成正文发出去。"""


def prompt_path(name: str) -> Path:
    """``"wechat/outline"`` → ``prompts/wechat/outline.md``。"""
    relative = name if name.endswith(".md") else f"{name}.md"
    path = (PROMPTS_DIR / relative).resolve()
    # 防止 ``../`` 越界读到仓库外的文件
    if not path.is_relative_to(PROMPTS_DIR):
        raise PromptNotFound(f"非法 prompt 名: {name!r}")
    return path


@lru_cache(maxsize=128)
def read(name: str) -> str:
    """读取 prompt 原文（带缓存）。"""
    path = prompt_path(name)
    if not path.is_file():
        raise PromptNotFound(f"prompt 不存在: {path}")
    return path.read_text(encoding="utf-8")


def render(template: str, /, **variables: object) -> str:
    """替换 ``{{name}}``。缺变量抛 :class:`PromptRenderError`。"""
    missing: list[str] = []

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in variables:
            missing.append(key)
            return match.group(0)
        value = variables[key]
        return "" if value is None else str(value)

    result = _PLACEHOLDER.sub(_sub, template)
    if missing:
        raise PromptRenderError(f"prompt 缺少变量: {sorted(set(missing))}")
    return result


def load(name: str, /, **variables: object) -> str:
    """读取 + 渲染。"""
    return render(read(name), **variables)


def variables_of(name: str) -> set[str]:
    """列出某个 prompt 用到的变量名，便于测试断言 prompt 与调用方没有脱节。"""
    return {m.group(1) for m in _PLACEHOLDER.finditer(read(name))}


def load_persona(account_id: str, *, default: str = "") -> str:
    """读取账号人设 ``prompts/accounts/<account_id>/persona.md``，不存在则返回默认值。"""
    path = ACCOUNTS_DIR / account_id / "persona.md"
    if not path.is_file():
        return default
    return path.read_text(encoding="utf-8").strip()


# --------------------------------------------------------------- 复盘（P4）

#: 复盘文件名。由 ``metrics/insights.py`` 追加写，``sourcing/selector.py`` 读
INSIGHTS_FILENAME = "insights.md"
#: 条目分隔符。刻意用 HTML 注释而不是 ``---``——正文里 ``---`` 太常见，切不干净
INSIGHT_SEPARATOR = "<!-- insight -->"


def insights_path(account_id: str) -> Path:
    """``prompts/accounts/<account_id>/insights.md``。"""
    path = (ACCOUNTS_DIR / account_id / INSIGHTS_FILENAME).resolve()
    if not path.is_relative_to(ACCOUNTS_DIR):
        raise PromptNotFound(f"非法账号 id: {account_id!r}")
    return path


def split_insights(text: str) -> list[str]:
    """把 insights.md 拆成条目列表（旧 → 新）。"""
    return [chunk.strip() for chunk in text.split(INSIGHT_SEPARATOR) if chunk.strip()]


def load_insights(account_id: str, *, limit: int = 2, default: str = "") -> str:
    """读最近 ``limit`` 条复盘，拼成一段可直接塞进 prompt 的文本。

    读的是文件而不是 DB：复盘结论是**人也要看、要能手改**的资产，和 persona 一样
    应该躺在 git 里出 diff，而不是埋在 SQLite 的某个 JSON 列里。
    """
    path = insights_path(account_id)
    if not path.is_file():
        return default
    entries = split_insights(path.read_text(encoding="utf-8"))
    if not entries:
        return default
    return f"\n\n{INSIGHT_SEPARATOR}\n\n".join(entries[-limit:]) if limit > 0 else default


def append_insight(account_id: str, entry: str, *, keep: int = 6) -> Path:
    """追加一条复盘，只保留最近 ``keep`` 条。返回写入的路径。"""
    path = insights_path(account_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = split_insights(path.read_text(encoding="utf-8")) if path.is_file() else []
    entries.append(entry.strip())
    if keep > 0:
        entries = entries[-keep:]
    body = f"\n\n{INSIGHT_SEPARATOR}\n\n".join(entries)
    path.write_text(f"{body}\n", encoding="utf-8")
    return path


def clear_cache() -> None:
    """测试里改了 prompt 文件后调用。"""
    read.cache_clear()


__all__ = [
    "ACCOUNTS_DIR",
    "INSIGHTS_FILENAME",
    "INSIGHT_SEPARATOR",
    "PROMPTS_DIR",
    "PromptNotFound",
    "PromptRenderError",
    "append_insight",
    "clear_cache",
    "insights_path",
    "load",
    "load_insights",
    "load_persona",
    "prompt_path",
    "read",
    "render",
    "split_insights",
    "variables_of",
]
