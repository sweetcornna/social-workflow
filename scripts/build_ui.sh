#!/usr/bin/env bash
# 构建前端工作台（Next.js 静态导出）。
#
#   bash scripts/build_ui.sh
#
# 产物落在 ui/out/，由 core/main.py 用 StaticFiles 挂在 /workbench。
# ui/out/ 不入库（见 .gitignore）——每台机器自己构建一次。
#
# 环境要求：node ≥ 20、pnpm ≥ 9。安装依赖需要网络，构建本身不需要
# （字体、图标、样式全部自托管在 node_modules 与源码里）。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UI_DIR="$ROOT/ui"

if ! command -v pnpm >/dev/null 2>&1; then
  echo "找不到 pnpm。装一个：npm i -g pnpm（或 corepack enable）" >&2
  exit 1
fi

# 注意：变量展开一律加花括号。macOS 自带的 bash 3.2 在 UTF-8 下会把紧跟其后的
# 全角字符（如 `）`）当成标识符的一部分，`$UI_DIR）` 会在 `set -u` 下报
# "UI_DIR�: unbound variable" 而构建根本跑不起来。
echo "==> 安装依赖（${UI_DIR}）"
cd "$UI_DIR"
# --frozen-lockfile：lockfile 入了库，CI 与本机必须装出同一棵依赖树
pnpm install --frozen-lockfile

echo "==> 类型检查"
pnpm run typecheck

echo "==> 构建静态导出"
pnpm run build

if [ ! -f "$UI_DIR/out/index.html" ]; then
  echo "构建似乎没产出 ui/out/index.html，检查上面的日志" >&2
  exit 1
fi

echo
echo "==> 完成。产物在 ui/out/（$(find "$UI_DIR/out" -type f | wc -l | tr -d ' ') 个文件）"
echo "    起 core 后打开 http://127.0.0.1:8000/workbench/"
