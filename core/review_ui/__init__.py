"""审核 UI（Jinja2 + HTMX）。模板在 ``templates/``，路由在 ``core/main.py``。"""

from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

__all__ = ["TEMPLATES_DIR"]
