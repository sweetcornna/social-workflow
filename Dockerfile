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

# 【Node 同样不是可选的，尽管门禁那句话曾经这么写】body_html 只有一条产出路径：
# generation/wechat_render.py 的 `npx -y @wenyan-md/cli render`。`WECHAT_BACKEND` 说的是
# **发布器**走 API 还是走 wenyan，跟渲染器是两码事——不管它取什么值，没有 Node 就没有
# body_html，而 review/inspect.py 里 `body_html.missing` 对 wechat_mp 是 **block**。
# 也就是说：缺 Node 的机器上，公众号这条链一条稿都出不去。
#
# 生产上就是这么撞上的：门禁写着「WECHAT_BACKEND=wenyan 时必需」，我据此判断默认的
# api 后端用不上 Node，于是这层没装；随后 --lane wechat 的验收在生产上红了，
# block 1 / warn 0，正是 body_html.missing。
#
# 从官方 node 镜像抄二进制而不是 apt install：base 同为 bookworm，版本可控且不引入 apt 源。
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-bookworm-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \
    ln -sf /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx && \
    npm install -g @wenyan-md/cli && \
    node --version && npx --version

COPY . .
RUN uv sync --frozen --no-dev --extra dsh --extra render

# 数据目录（compose 里挂 volume）
RUN mkdir -p /app/data /home/appuser/.npm && \
    useradd --create-home --uid 10001 appuser && \
    chown -R appuser:appuser /app /opt/venv /home/appuser
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"

CMD ["uvicorn", "core.main:app", "--host", "0.0.0.0", "--port", "8000"]
