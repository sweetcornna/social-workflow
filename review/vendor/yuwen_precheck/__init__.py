"""yuwen-publish-precheck 的规则数据（MIT）。来源与取舍见 PROVENANCE.md。

这里只暴露数据文件路径，匹配逻辑在 :mod:`review.precheck`。
"""

from __future__ import annotations

from pathlib import Path

VENDOR_DIR = Path(__file__).resolve().parent
TERMS_PATH = VENDOR_DIR / "terms.json"
LICENSE_PATH = VENDOR_DIR / "LICENSE"

UPSTREAM = "https://github.com/yuwen-cool/yuwen-publish-precheck"
UPSTREAM_LICENSE = "MIT"
UPSTREAM_BRANCH = "master"
UPSTREAM_TERMS_URL = (
    "https://raw.githubusercontent.com/yuwen-cool/yuwen-publish-precheck/master/scripts/terms.json"
)

__all__ = [
    "LICENSE_PATH",
    "TERMS_PATH",
    "UPSTREAM",
    "UPSTREAM_BRANCH",
    "UPSTREAM_LICENSE",
    "UPSTREAM_TERMS_URL",
    "VENDOR_DIR",
]
