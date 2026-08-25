#!/usr/bin/env bash
# 用途：经 IAP SSH 只读查看生产 core 容器日志，支持尾部行数和跟随。
set -euo pipefail

SSH_ALIAS="${SW_OPS_SSH_ALIAS:-${SW_TUNNEL_SSH_ALIAS:-workbench-iap}}"
LINES=200
FOLLOW=0
LINES_SET=0

die() { printf '\n✗ %s\n' "${1}" >&2; shift; for line in "$@"; do printf '  %s\n' "${line}" >&2; done; exit 1; }
note() { printf '  %s\n' "${1}"; }

while [[ "${#}" -gt 0 ]]; do
  case "${1}" in
    -f)
      FOLLOW=1
      ;;
    *)
      if [[ "${LINES_SET}" -eq 0 && "${1}" =~ ^[1-9][0-9]*$ ]]; then
        LINES="${1}"
        LINES_SET=1
      else
        die "参数无效：${1}" "用法：bash scripts/ops/logs.sh [行数] [-f]"
      fi
      ;;
  esac
  shift
done

command -v ssh >/dev/null 2>&1 || die "本机没有 ssh 命令"

printf '生产 core 日志\n\n'
if [[ "${FOLLOW}" -eq 1 ]]; then
  note "连接 ${SSH_ALIAS}，跟随最近 ${LINES} 行，按 Ctrl-C 结束"
  exec ssh -o ConnectTimeout=25 "${SSH_ALIAS}" \
    "cd \"\${HOME}/social_workflow\" && exec docker compose logs --tail ${LINES} -f core"
fi

note "连接 ${SSH_ALIAS}，读取最近 ${LINES} 行"
ssh -o ConnectTimeout=25 "${SSH_ALIAS}" "bash -s -- ${LINES}" <<'REMOTE'
set -euo pipefail
# 这层 bash 的**脚本正文就是它自己的 stdin**（正上方这个 REMOTE heredoc）。任何读 stdin 的
# 子进程都会把"脚本剩下的部分"吞掉，而脚本仍以 0 收尾。所以本仓的不变量是：远端 heredoc
# 里每一条 docker/curl 都带显式 stdin 来源。真实 `docker compose logs` 不 attach stdin，
# 这里的 `</dev/null` 是防御性的——但不变量不留例外，历史事故正是从"看起来没事"开始的。

lines="${1}"
cd "${HOME}/social_workflow"
docker compose logs --tail "${lines}" core </dev/null
REMOTE
