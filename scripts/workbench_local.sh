#!/usr/bin/env bash
# 本地工作台启动器 —— 拓扑改了：服务器只跑 core（127.0.0.1:8000，不绑公网口），
# 完整的 Organic 工作台（ui/）与对话台都在本地跑，经 IAP SSH 隧道打数据面。
#
#   服务器（IAP 之后）                本地
#   ┌─────────────────────┐          ┌──────────────────────────────┐
#   │ core :8000（loopback）│◀── ssh ──│ 127.0.0.1:18000（本隧道）      │
#   │ 无 UI、无公网口        │  -L 隧道  │   ↑                            │
#   └─────────────────────┘          │ next dev :3210/workbench      │
#                                     │   （SW_CORE_ORIGIN 指隧道端口） │
#                                     └──────────────────────────────┘
#
#   bash scripts/workbench_local.sh            # up：起隧道 + 起工作台（默认）
#   bash scripts/workbench_local.sh tunnel     # 只起隧道，幂等
#   bash scripts/workbench_local.sh doctor     # 只体检，什么都不起
#   bash scripts/workbench_local.sh down       # 收掉本脚本起的那条隧道
#
# 这个脚本只做三件事：体检、起/收隧道、把 `pnpm dev` 起起来——不装依赖、不改
# ui/ 或 core/ 源码、不碰密钥（ssh 走 ~/.ssh/config 里现成的 IdentityFile，
# gcloud 走它自己的登录态，本脚本从头到尾不读也不打印任何凭据）。
#
# 环境变量：
#   SW_TUNNEL_SSH_ALIAS   ssh 别名（默认 workbench-iap，见 ~/.ssh/config）
#   SW_TUNNEL_PORT        本地转发端口（默认 18000）
#   SW_TUNNEL_REMOTE_PORT 服务器那端 core 的端口（默认 8000，一般不用改）
#   SW_CORE_ORIGIN        工作台打数据面用的 origin（默认跟着 SW_TUNNEL_PORT
#                         联动成 http://127.0.0.1:<port>；显式设了就以它为准，
#                         多用于联调/测试时直接指向别的 core）

set -euo pipefail

MODE="${1:-up}"

SSH_ALIAS="${SW_TUNNEL_SSH_ALIAS:-workbench-iap}"
TUNNEL_PORT="${SW_TUNNEL_PORT:-18000}"
REMOTE_PORT="${SW_TUNNEL_REMOTE_PORT:-8000}"
CORE_ORIGIN="${SW_CORE_ORIGIN:-http://127.0.0.1:${TUNNEL_PORT}}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UI_DIR="$SCRIPT_DIR/../ui"

# 用来起隧道、也用来在 pgrep 里认出「是不是本脚本起的那条」的参数指纹。
# 只要端口/远端口/别名三者都对得上才算数——不误杀别的 ssh 转发。
SSH_FINGERPRINT="-L ${TUNNEL_PORT}:127.0.0.1:${REMOTE_PORT} ${SSH_ALIAS}"

die() { printf '\n✗ %s\n' "$1" >&2; shift; for line in "$@"; do printf '  %s\n' "$line" >&2; done; exit 1; }
note() { printf '  %s\n' "$1"; }
ok() { printf '  ✓ %s\n' "$1"; }
warn() { printf '\n⚠ %s\n' "$1" >&2; shift; for line in "$@"; do printf '  %s\n' "$line" >&2; done; }

# ── 隧道进程查找（永远返回 0，找不到就是空字符串，不当错误处理）───────────
tunnel_pid() {
  pgrep -f "ssh .*${SSH_FINGERPRINT}\$" 2>/dev/null | head -1 || true
}

port_listener_pid() {
  lsof -nP -iTCP:"${TUNNEL_PORT}" -sTCP:LISTEN -t 2>/dev/null | head -1 || true
}

check_core_reachable() {
  curl -sf --max-time 3 "${CORE_ORIGIN%/}/api/v1/system/info" >/dev/null 2>&1
}

# ── 起隧道（幂等）──────────────────────────────────────────────────────────
start_tunnel() {
  local existing
  existing="$(tunnel_pid)"
  if [[ -n "$existing" ]]; then
    ok "隧道已在跑（pid ${existing}）→ 127.0.0.1:${TUNNEL_PORT} → (IAP) → ${SSH_ALIAS}"
    return 0
  fi

  local occupant
  occupant="$(port_listener_pid)"
  if [[ -n "$occupant" ]]; then
    die "端口 ${TUNNEL_PORT} 被别的进程占着（pid ${occupant}，不是本脚本起的隧道）" \
      "换个端口：SW_TUNNEL_PORT=<port> bash scripts/workbench_local.sh $MODE" \
      "或先看看那是什么再决定要不要动它：lsof -nP -iTCP:${TUNNEL_PORT}"
  fi

  command -v ssh >/dev/null 2>&1 || die "本机没有 ssh 命令"
  if ! ssh -G "$SSH_ALIAS" >/dev/null 2>&1; then
    die "ssh 别名 '${SSH_ALIAS}' 解析失败" \
      "检查 ~/.ssh/config 里有没有 Host ${SSH_ALIAS} 那一段" \
      "换别名：SW_TUNNEL_SSH_ALIAS=<alias> bash scripts/workbench_local.sh $MODE"
  fi
  command -v gcloud >/dev/null 2>&1 || die "本机没有 gcloud（IAP 隧道的 ProxyCommand 要用它）" \
    "装：https://cloud.google.com/sdk/docs/install，装完 gcloud auth login"

  note "起隧道 127.0.0.1:${TUNNEL_PORT} → (IAP) → ${SSH_ALIAS} → 127.0.0.1:${REMOTE_PORT}"
  note "首包走 IAP，通常 5-10 秒，稍等…"

  # ssh -f 守护化后子进程会继续握着继承来的 stdout/stderr——若本函数的输出被
  # 管道接走(如 `... tunnel | tail`),读端会永远等不到 EOF 而挂死。所以这里把
  # 守护进程的 stdio 全部摘走,stderr 落临时文件,失败时再放出来。
  local ssh_errlog
  ssh_errlog="$(mktemp)"
  if ! ssh -f -N \
      -o ExitOnForwardFailure=yes \
      -o ServerAliveInterval=30 \
      -o ServerAliveCountMax=3 \
      -o ConnectTimeout=20 \
      -L "${TUNNEL_PORT}:127.0.0.1:${REMOTE_PORT}" \
      "$SSH_ALIAS" </dev/null >/dev/null 2>"$ssh_errlog"; then
    cat "$ssh_errlog" >&2 || true
    rm -f "$ssh_errlog"
    die "隧道没起来（ssh 非零退出）" \
      "常见原因：gcloud 没登录（gcloud auth login）/ 这台 VPS 没有 IAP 权限 / 服务器上 core 还没起" \
      "单独重试看详细报错：ssh -v -N -L ${TUNNEL_PORT}:127.0.0.1:${REMOTE_PORT} ${SSH_ALIAS}"
  fi
  rm -f "$ssh_errlog"

  local new_pid
  new_pid="$(tunnel_pid)"
  if [[ -z "$new_pid" ]]; then
    die "ssh 退出码是 0，但没找到对应进程——环境异常，重跑一遍看看"
  fi
  ok "隧道已起（pid ${new_pid}）→ 127.0.0.1:${TUNNEL_PORT} → (IAP) → ${SSH_ALIAS}"
}

# ── 收隧道（只杀本脚本按指纹认出的那条）───────────────────────────────────
stop_tunnel() {
  local pid
  pid="$(tunnel_pid)"
  if [[ -z "$pid" ]]; then
    note "没有活着的隧道（127.0.0.1:${TUNNEL_PORT} → ${SSH_ALIAS}），无需收"
    return 0
  fi

  kill "$pid" 2>/dev/null || true

  local i=1
  while [[ "$i" -le 5 ]]; do
    if [[ -z "$(tunnel_pid)" ]]; then
      break
    fi
    sleep 1
    i=$((i + 1))
  done

  if [[ -n "$(tunnel_pid)" ]]; then
    warn "隧道进程（pid ${pid}）没能在 5 秒内退出" "手动看看：ps -p ${pid}，必要时 kill -9 $pid"
  else
    ok "隧道已收（原 pid ${pid}）"
  fi
}

# ── doctor：只查不动 ───────────────────────────────────────────────────────
doctor_checks() {
  printf '本地工作台体检\n\n'
  local issues=0

  if ssh -G "$SSH_ALIAS" >/dev/null 2>&1; then
    ok "ssh 别名 ${SSH_ALIAS} 能解析（~/.ssh/config）"
  else
    printf '  ✗ ssh 别名 %s 解析失败\n' "$SSH_ALIAS"
    printf '    检查 ~/.ssh/config 里有没有 Host %s 那一段\n' "$SSH_ALIAS"
    issues=$((issues + 1))
  fi

  if command -v gcloud >/dev/null 2>&1; then
    ok "gcloud 在 PATH 里（$(command -v gcloud)）"
  else
    printf '  ✗ 没有 gcloud —— IAP 隧道的 ProxyCommand 要用它\n'
    printf '    装：https://cloud.google.com/sdk/docs/install\n'
    issues=$((issues + 1))
  fi

  local occ tpid
  occ="$(port_listener_pid)"
  tpid="$(tunnel_pid)"
  if [[ -z "$occ" ]]; then
    note "端口 ${TUNNEL_PORT} 空闲（隧道没起，起：workbench_local.sh tunnel）"
  elif [[ "$occ" == "$tpid" ]]; then
    ok "端口 ${TUNNEL_PORT} 被本脚本的隧道占着（pid ${occ}）"
  else
    printf '  ⚠ 端口 %s 被别的进程占着（pid %s，不是本脚本的隧道）\n' "$TUNNEL_PORT" "$occ"
    issues=$((issues + 1))
  fi

  if check_core_reachable; then
    ok "经 ${CORE_ORIGIN} 能连到 core（GET /api/v1/system/info 200）"
  else
    printf '  ✗ 连不上 %s/api/v1/system/info\n' "${CORE_ORIGIN%/}"
    printf '    没起隧道：bash scripts/workbench_local.sh tunnel\n'
    printf '    起了但连不上：服务器那边 core 可能没起来；也可能是 IAP 首包还没到\n'
    printf '    （偶尔要 5-10 秒），隔几秒重跑一次 doctor 再看\n'
    issues=$((issues + 1))
  fi

  if command -v node >/dev/null 2>&1; then
    ok "node $(node --version)"
  else
    printf '  ✗ 没有 node\n'
    issues=$((issues + 1))
  fi

  if command -v pnpm >/dev/null 2>&1; then
    ok "pnpm $(pnpm --version)"
  else
    printf '  ✗ 没有 pnpm\n'
    printf '    装：corepack enable，或 npm i -g pnpm\n'
    issues=$((issues + 1))
  fi

  printf '\n'
  if [[ "$issues" -eq 0 ]]; then
    printf '体检通过。\n'
  else
    printf '体检发现 %d 项异常（见上面 ✗/⚠）。\n' "$issues"
  fi
  return "$issues"
}

# ── up：起隧道 → 校验 core 可达 → 起工作台 ─────────────────────────────────
cmd_up() {
  printf '本地工作台\n\n'
  start_tunnel

  printf '\n校验 core 可达 %s ...\n' "${CORE_ORIGIN%/}"
  local reached=0 try_n=1
  while [[ "$try_n" -le 3 ]]; do
    if check_core_reachable; then
      reached=1
      break
    fi
    if [[ "$try_n" -lt 3 ]]; then
      sleep 2
    fi
    try_n=$((try_n + 1))
  done

  if [[ "$reached" -eq 1 ]]; then
    ok "core 可达"
  else
    warn "隧道起了，但 ${CORE_ORIGIN%/}/api/v1/system/info 连不上" \
      "服务器那边 core 可能没起来——找主控确认" \
      "也可能是 IAP 首包还没到；工作台照样能开，等会儿刷新页面再看" \
      "工作台会照样起，但数据面没有旁路：页面会一直转圈或报错，直到这条通"
  fi

  [[ -d "$UI_DIR" ]] || die "找不到 ui 目录：$UI_DIR"

  printf '\n起工作台（next dev，basePath /workbench）\n'
  printf '  SW_CORE_ORIGIN  %s\n' "$CORE_ORIGIN"
  printf '  地址            http://127.0.0.1:3210/workbench/\n'
  printf '  停              Ctrl-C（隧道不会跟着收；单独收：workbench_local.sh down）\n\n'

  cd "$UI_DIR"
  exec env SW_CORE_ORIGIN="$CORE_ORIGIN" pnpm dev
}

case "$MODE" in
  up)
    cmd_up
    ;;
  tunnel)
    printf '本地工作台 · 隧道\n\n'
    start_tunnel
    printf '\n数据面：%s\n' "${CORE_ORIGIN%/}"
    ;;
  doctor)
    doctor_checks
    ;;
  down)
    printf '本地工作台 · 收隧道\n\n'
    stop_tunnel
    ;;
  *)
    die "不认识的模式：$MODE" "用法：bash scripts/workbench_local.sh [up|tunnel|doctor|down]"
    ;;
esac
