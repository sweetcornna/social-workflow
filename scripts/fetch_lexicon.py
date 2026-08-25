#!/usr/bin/env python
"""下载审核所需的第三方词库 / 规则数据。

两个来源，都是 MIT：

- ``konsheng/Sensitive-lexicon`` → ``data/lexicon/Vocabulary/*.txt``（**不进 git**，
  单文件最大 700KB+；由 ``review/lexicon.py`` 加载）
- ``yuwen-cool/yuwen-publish-precheck`` → ``review/vendor/yuwen_precheck/terms.json``
  （**进 git**，16KB 规则数据；来源与取舍见同目录 PROVENANCE.md）

用法::

    uv run python scripts/fetch_lexicon.py                 # 下载常用类目
    uv run python scripts/fetch_lexicon.py --all           # 全量（含 700KB 的腾讯词库）
    uv run python scripts/fetch_lexicon.py --precheck-only # 只刷新 precheck 规则
    uv run python scripts/fetch_lexicon.py --list          # 只看远端有哪些词表
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from review.lexicon import EXCLUDED_FILES, LEXICON_SUBDIR  # noqa: E402
from review.vendor.yuwen_precheck import TERMS_PATH, UPSTREAM_TERMS_URL  # noqa: E402

LEXICON_OWNER = "konsheng"
LEXICON_REPO = "Sensitive-lexicon"
LEXICON_BRANCH = "main"
CONTENTS_API = (
    f"https://api.github.com/repos/{LEXICON_OWNER}/{LEXICON_REPO}/contents/{LEXICON_SUBDIR}"
    f"?ref={LEXICON_BRANCH}"
)
RAW_BASE = (
    f"https://raw.githubusercontent.com/{LEXICON_OWNER}/{LEXICON_REPO}/{LEXICON_BRANCH}/"
    f"{LEXICON_SUBDIR}"
)

#: 默认下载的类目。刻意不含 `零时-Tencent`（716KB）与 `网易前端过滤敏感词库`（114KB）——
#: 它们误伤率高且体积大，需要时用 --all 或 --categories 显式要。
DEFAULT_CATEGORIES = (
    "政治类型",
    "反动词库",
    "暴恐词库",
    "涉枪涉爆",
    "色情类型",
    "色情词库",
    "贪腐词库",
    "民生词库",
    "广告类型",
    "其他词库",
    "补充词库",
)

TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def list_remote_files(client: httpx.Client) -> list[dict[str, object]]:
    response = client.get(CONTENTS_API)
    response.raise_for_status()
    payload = response.json()
    return [
        entry
        for entry in payload
        if entry.get("type") == "file" and str(entry.get("name", "")).endswith(".txt")
    ]


def fetch_lexicon(target_dir: Path, *, categories: tuple[str, ...] | None, dry_run: bool) -> int:
    out_dir = target_dir / LEXICON_SUBDIR
    written = 0
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
        try:
            remote = list_remote_files(client)
        except httpx.HTTPError as exc:
            print(f"[FAIL] 无法列出远端词表: {exc}", file=sys.stderr)
            return 1
        print(f"远端共 {len(remote)} 个词表文件")

        for entry in remote:
            name = str(entry["name"])
            stem = name[:-4]
            if stem in EXCLUDED_FILES:
                print(f"[skip] {name}（域名清单，不参与子串匹配）")
                continue
            if categories is not None and stem not in categories:
                print(f"[skip] {name}（不在选定类目）")
                continue
            size = int(entry.get("size", 0) or 0)
            if dry_run:
                print(f"[dry-run] {name} ({size} bytes)")
                continue
            url = f"{RAW_BASE}/{name}"
            try:
                response = client.get(url)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                print(f"[FAIL] {name}: {exc}", file=sys.stderr)
                continue
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / name
            path.write_text(response.text, encoding="utf-8")
            lines = sum(1 for line in response.text.splitlines() if line.strip())
            print(f"[ok] {name} → {path} （{lines} 词）")
            written += 1

    if not dry_run and written:
        print(f"\n完成：{written} 个词表写入 {out_dir}")
        print("提示：data/ 已在 .gitignore 中，词库不会进版本库。")
    return 0


def fetch_precheck(dry_run: bool) -> int:
    if dry_run:
        print(f"[dry-run] {UPSTREAM_TERMS_URL} → {TERMS_PATH}")
        return 0
    try:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
            response = client.get(UPSTREAM_TERMS_URL)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"[FAIL] 拉取 precheck 规则失败: {exc}", file=sys.stderr)
        return 1
    TERMS_PATH.parent.mkdir(parents=True, exist_ok=True)
    TERMS_PATH.write_text(response.text, encoding="utf-8")
    print(f"[ok] precheck 规则 → {TERMS_PATH}")
    print("提示：这个文件**进 git**，请同步更新 PROVENANCE.md 里的取用日期。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dir", default=None, help="词库输出目录，默认取 LEXICON_DIR 配置")
    parser.add_argument("--all", action="store_true", help="下载全部类目（含 700KB 大词库）")
    parser.add_argument("--categories", default=None, help="逗号分隔的类目名，覆盖默认集合")
    parser.add_argument("--precheck-only", action="store_true", help="只刷新 precheck 规则数据")
    parser.add_argument("--skip-precheck", action="store_true", help="不刷新 precheck 规则数据")
    parser.add_argument("--list", action="store_true", help="只列出远端词表，不下载")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要做什么")
    args = parser.parse_args(argv)

    if args.list:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
            for entry in list_remote_files(client):
                print(f"{entry['name']:<32} {entry.get('size', 0)} bytes")
        return 0

    if args.precheck_only:
        return fetch_precheck(args.dry_run)

    from core.config import get_settings

    target = Path(args.dir) if args.dir else Path(get_settings().lexicon_dir)
    if args.categories:
        categories: tuple[str, ...] | None = tuple(
            c.strip() for c in args.categories.split(",") if c.strip()
        )
    elif args.all:
        categories = None
    else:
        categories = DEFAULT_CATEGORIES

    code = fetch_lexicon(target, categories=categories, dry_run=args.dry_run)
    if not args.skip_precheck:
        code = fetch_precheck(args.dry_run) or code
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
