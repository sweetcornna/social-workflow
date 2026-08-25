"""P1 测试公用工具。

单独一个模块而不是塞进 ``conftest.py``：conftest 是 P0 与并行开发的其它阶段共用的，
减少改动面能降低多人同时改同一文件的冲突概率。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    """读取 ``tests/fixtures/<name>``（JSON）。"""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def json_transport(routes: dict[str, Any], *, default_status: int = 404) -> httpx.MockTransport:
    """按 URL path 分发的假 transport。

    ``routes`` 的值可以是：
    - dict / list → 200 + JSON
    - ``(status_code, payload)`` → 指定状态码
    - ``httpx.Response`` → 原样返回
    """

    def handler(request: httpx.Request) -> httpx.Response:
        entry = routes.get(request.url.path)
        if entry is None:
            return httpx.Response(default_status, json={"error": f"no route {request.url.path}"})
        if isinstance(entry, httpx.Response):
            return entry
        if isinstance(entry, tuple):
            status, payload = entry
            if isinstance(payload, str):
                return httpx.Response(status, text=payload)
            return httpx.Response(status, json=payload)
        return httpx.Response(200, json=entry)

    return httpx.MockTransport(handler)


def mock_client(routes: dict[str, Any], **kwargs: Any) -> httpx.Client:
    """挂了假 transport 的 httpx.Client。"""
    return httpx.Client(transport=json_transport(routes), **kwargs)


class RecordingRunner:
    """假的 ``subprocess.run``：记录命令并返回预置结果。"""

    def __init__(self, *, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.calls: list[list[str]] = []
        self.kwargs: list[dict[str, Any]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> Any:
        self.calls.append(list(command))
        self.kwargs.append(kwargs)

        class _Completed:
            pass

        completed = _Completed()
        completed.returncode = self.returncode  # type: ignore[attr-defined]
        completed.stdout = self.stdout  # type: ignore[attr-defined]
        completed.stderr = self.stderr  # type: ignore[attr-defined]
        return completed

    @property
    def last_command(self) -> list[str]:
        return self.calls[-1]


def fake_screenshotter(written: list[tuple[Path, str]]) -> Any:
    """假截图器：把 HTML 记下来并写一个最小 PNG，签名同 cover.render_cover 要求。"""
    # 1x1 透明 PNG，避免测试依赖 Playwright / 浏览器
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c6300010000050001"
        "0d0a2db40000000049454e44ae426082"
    )

    def _shot(html: str, path: Path, width: int, height: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png)
        written.append((path, html))

    return _shot
