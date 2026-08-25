#!/usr/bin/env bash
# 用途：探测自建模型网关的配额恢复状态，并在恢复后提示补验对话台的活体 LLM 路径。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE=""
# 自建网关地址不入库：用 SW_GATEWAY_URL 指定 chat/completions 端点，
# 或用 SW_DSH_GATEWAY_BASE_URL（与 configs/dsh/cordis.yml 的 gateway 路由同一个变量）。
GATEWAY_URL="${SW_GATEWAY_URL:-${SW_DSH_GATEWAY_BASE_URL:+${SW_DSH_GATEWAY_BASE_URL%/}/chat/completions}}"
WATCH=0
WATCH_INTERVAL=900
MAX_WATCH_SECONDS=$((48 * 60 * 60))

die() { printf '\n失败：%s\n' "${1}" >&2; shift; for line in "$@"; do printf '  %s\n' "${line}" >&2; done; exit 2; }
note() { printf '  %s\n' "${1}"; }
ok() { printf '  通过：%s\n' "${1}"; }

usage() {
  printf '用法：bash scripts/gateway_ready.sh [--watch [间隔秒]]\n'
}

resolve_env_file() {
  local worktree_env
  local common_git_dir
  local common_checkout
  local common_env

  if [[ -n "${SW_ENV_FILE:-}" ]]; then
    if [[ -f "${SW_ENV_FILE}" ]]; then
      ENV_FILE="${SW_ENV_FILE}"
      return 0
    fi
    die "SW_ENV_FILE 指向的文件不存在" "${SW_ENV_FILE}"
  fi

  worktree_env="${REPO_ROOT}/.env"
  if [[ -f "${worktree_env}" ]]; then
    ENV_FILE="${worktree_env}"
    return 0
  fi

  if common_git_dir="$(git -C "${REPO_ROOT}" rev-parse --git-common-dir 2>/dev/null)"; then
    case "${common_git_dir}" in
      /*) ;;
      *) common_git_dir="${REPO_ROOT}/${common_git_dir}" ;;
    esac
    common_checkout="$(cd "$(dirname "${common_git_dir}")" && pwd)"
    common_env="${common_checkout}/.env"
    if [[ -f "${common_env}" ]]; then
      ENV_FILE="${common_env}"
      return 0
    fi
  else
    common_env="<无法通过 git 公共目录定位>"
  fi

  die "找不到 .env" "已查找：" "1. ${worktree_env}" "2. ${common_env}"
}

utc_now() {
  date -u '+%Y-%m-%dT%H:%M:%SZ'
}

utc_from_epoch() {
  local epoch="$1"

  if date -u -r "${epoch}" '+%Y-%m-%dT%H:%M:%SZ' >/dev/null 2>&1; then
    date -u -r "${epoch}" '+%Y-%m-%dT%H:%M:%SZ'
  else
    date -u -d "@${epoch}" '+%Y-%m-%dT%H:%M:%SZ'
  fi
}

format_remaining() {
  local remaining="$1"
  local days=$((remaining / 86400))
  local hours=$(((remaining % 86400) / 3600))
  local minutes=$(((remaining % 3600) / 60))

  printf '%s天%s时%s分' "${days}" "${hours}" "${minutes}"
}

print_next_steps() {
  printf '  接下来补验（docs/OPS.md 第 7.7.6 节）：\n'
  printf '  1. persona 与红线问答\n'
  printf '%s\n' '     bash ui/e2e/serve.sh 8000'
  printf '%s\n' "     ${SW_DESKTOP_ROOT:-$HOME/project/social_workflow/sw-hermes-desktop}/.venv/bin/hermes --profile sw"
  printf '  2. LLM 主动调工具\n'
  printf '%s\n' "     ${SW_DESKTOP_ROOT:-$HOME/project/social_workflow/sw-hermes-desktop}/.venv/bin/hermes --profile sw"
}

retry_after_from_headers() {
  awk 'tolower($1) == "retry-after:" { sub(/\r$/, "", $2); print $2; exit }' "${1}"
}

probe_once() {
  local output_mode="$1"
  local headers
  local curl_error
  local status
  local retry_after
  local now_epoch
  local recovery_epoch
  local now_utc
  local curl_rc

  headers="$(mktemp)"
  curl_error="$(mktemp)"
  status=""

  if status="$(curl --silent --show-error --connect-timeout 15 --max-time 30 \
      --output /dev/null --dump-header "${headers}" --write-out '%{http_code}' \
      --request POST "${GATEWAY_URL}" \
      --header 'Content-Type: application/json' \
      --header "Authorization: Bearer ${DEEPSEEK_API_KEY}" \
      --data '{"model":"deepseek-v4-flash","max_tokens":1,"messages":[{"role":"user","content":"hi"}]}' \
      2>"${curl_error}")"; then
    :
  else
    curl_rc=$?
    if [[ "${output_mode}" == "watch" ]]; then
      printf '%s 网络失败（curl 退出码 %s）\n' "$(utc_now)" "${curl_rc}" >&2
    else
      printf '网关探测失败\n' >&2
      note "网络失败，curl 退出码：${curl_rc}，未返回 HTTP 状态"
    fi
    sed -n '1p' "${curl_error}" >&2 || true
    rm -f "${headers}" "${curl_error}"
    return 3
  fi

  rm -f "${curl_error}"
  case "${status}" in
    200)
      now_utc="$(utc_now)"
      if [[ "${output_mode}" == "watch" ]]; then
        printf '%s 网关已恢复\n' "${now_utc}"
      else
        printf '网关已恢复\n'
        note "当前 UTC：${now_utc}"
      fi
      rm -f "${headers}"
      print_next_steps
      return 0
      ;;
    429)
      retry_after="$(retry_after_from_headers "${headers}")"
      rm -f "${headers}"
      if [[ -z "${retry_after}" || "${retry_after}" == *[!0-9]* ]]; then
        if [[ "${output_mode}" == "watch" ]]; then
          printf '%s 网关仍受限（HTTP 429，缺少可解析的 retry-after）\n' "$(utc_now)"
        else
          printf '网关仍受限\n'
          note "HTTP 429，但响应头缺少可解析的 retry-after"
        fi
        return 1
      fi

      now_epoch="$(date +%s)"
      recovery_epoch=$((now_epoch + retry_after))
      now_utc="$(utc_from_epoch "${now_epoch}")"
      if [[ "${output_mode}" == "watch" ]]; then
        printf '%s 网关仍受限（HTTP 429，剩余 %s，预计恢复 %s）\n' \
          "${now_utc}" "$(format_remaining "${retry_after}")" "$(utc_from_epoch "${recovery_epoch}")"
      else
        printf '网关仍受限\n'
        note "当前 UTC：${now_utc}"
        note "HTTP 429，retry-after：${retry_after} 秒"
        note "剩余：$(format_remaining "${retry_after}")"
        note "预计恢复 UTC：$(utc_from_epoch "${recovery_epoch}")"
      fi
      return 1
      ;;
    *)
      rm -f "${headers}"
      if [[ "${output_mode}" == "watch" ]]; then
        printf '%s 网关异常（HTTP %s）\n' "$(utc_now)" "${status}" >&2
      else
        printf '网关探测异常\n' >&2
        note "HTTP 状态码：${status}"
        note "请检查网关服务、路由和凭据权限"
      fi
      return 3
      ;;
  esac
}

if [[ "$#" -eq 0 ]]; then
  :
elif [[ "${1}" == "--watch" && "$#" -le 2 ]]; then
  WATCH=1
  if [[ "$#" -eq 2 ]]; then
    WATCH_INTERVAL="${2}"
  fi
else
  usage >&2
  exit 2
fi

case "${WATCH_INTERVAL}" in
  ''|*[!0-9]*) die "轮询间隔必须是正整数秒" ;;
esac
if [[ "${WATCH_INTERVAL}" -eq 0 ]]; then
  die "轮询间隔必须大于 0"
fi

resolve_env_file

set -a
# shellcheck disable=SC1090
. "${ENV_FILE}"
set +a

[[ -n "${GATEWAY_URL}" ]] || die "没有网关地址" \
  "自建网关的域名不入库。二选一：" \
  "  SW_GATEWAY_URL=https://<网关>/v1/chat/completions bash scripts/gateway_ready.sh" \
  "  SW_DSH_GATEWAY_BASE_URL=https://<网关>/v1 bash scripts/gateway_ready.sh"
[[ -n "${DEEPSEEK_API_KEY:-}" ]] || die ".env 缺少 DEEPSEEK_API_KEY"
command -v curl >/dev/null 2>&1 || die "本机没有 curl 命令"

cd "${REPO_ROOT}"

if [[ "${WATCH}" -eq 0 ]]; then
  if probe_once single; then
    exit 0
  else
    exit "$?"
  fi
fi

trap 'printf "\n%s 已收到终止信号，停止轮询\n" "$(utc_now)"; exit 130' INT TERM

watch_started="$(date +%s)"
while true; do
  if probe_once watch; then
    exit 0
  else
    probe_rc=$?
  fi
  if [[ "${probe_rc}" -eq 3 ]]; then
    exit 3
  fi
  if [[ $(( $(date +%s) - watch_started )) -ge "${MAX_WATCH_SECONDS}" ]]; then
    printf '%s 已达到 48 小时轮询上限，网关仍未恢复\n' "$(utc_now)" >&2
    exit 1
  fi
  sleep "${WATCH_INTERVAL}"
done
