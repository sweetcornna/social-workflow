# 控制面 core 镜像：python:3.12-slim + uv
FROM python:3.12-slim AS base

# 官方 uv 二进制（Apache-2.0 / MIT 双许可）
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /usr/local/bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

WORKDIR /app

# 先只拷依赖清单，最大化 layer 缓存
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev --extra dsh --extra render

# 【render extra 与 chromium 不是可选的，尽管名字叫 optional-dependencies】
#
# 只装 dsh 的镜像跑不出「无干预」，而且失败得很安静。链路是这样的：没有 chromium →
# 小红书卡片 / 抖音封面渲不出来 → review.inspect 记一条 cover.missing（小红书那条
# xhs.image.missing 甚至是 block）→ autopilot 的自动批准条件是 block == 0 且 warn == 0
# （core/scheduler.py:235），**一条 warn 就够让它不批** → 每条稿子都退回人工审核台。
# 于是这台机器上「全自动」这三个字对任何平台都不成立，而门禁在 P17 之前一声不吭。
#
# 换个平台躲不开：公众号封面缺失虽然只是 warn，warn 一样卡住 autopilot。
#
# 装在 /opt/ms-playwright（见上面的 PLAYWRIGHT_BROWSERS_PATH）而不是默认的 ~/.cache：
# 这一步以 root 跑，运行时是 appuser，默认路径装完 appuser 根本找不到。
RUN playwright install --with-deps chromium && chmod -R a+rX /opt/ms-playwright

COPY . .
RUN uv sync --frozen --no-dev --extra dsh --extra render

# 数据目录（compose 里挂 volume）
RUN mkdir -p /app/data && \
    useradd --create-home --uid 10001 appuser && \
    chown -R appuser:appuser /app /opt/venv
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"

CMD ["uvicorn", "core.main:app", "--host", "0.0.0.0", "--port", "8000"]
