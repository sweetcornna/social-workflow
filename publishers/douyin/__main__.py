"""``python -m publishers.douyin serve`` 入口。

刻意放在 ``__main__`` 而不是 ``__init__``：``publishers/douyin/service.py`` 会 import
patchright（只在宿主机上装，``uv sync --extra douyin`` 才有），
而 core 侧只需要 ``client`` / ``publisher``，不该被浏览器依赖拖住。
"""

from __future__ import annotations

from publishers.douyin.service import main

if __name__ == "__main__":  # pragma: no cover - 进程入口
    raise SystemExit(main())
