#!/usr/bin/env bash
# 用途：Actions 账单停用期间在本地复现 CI 回归门禁；账单修好后 CI 仍是权威，本脚本只是补充。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CI_VENV=""

die() { printf '\n失败：%s\n' "${1}" >&2; shift; for line in "$@"; do printf '  %s\n' "${line}" >&2; done; exit 2; }
note() { printf '  %s\n' "${1}"; }
ok() { printf '  通过：%s\n' "${1}"; }

resolve_ci_venv() {
  local requested="$1"
  local target
  local parent
  local name
  local existing_path

  case "${requested}" in
    /*) target="${requested}" ;;
    *) target="${REPO_ROOT}/${requested}" ;;
  esac

  name="$(basename "${target}")"
  case "${name}" in
    ''|.|..) die "SW_CI_VENV 必须指向具体目录" "当前值：${requested}" ;;
  esac

  parent="$(dirname "${target}")"
  mkdir -p "${parent}" || die "无法创建隔离 venv 的父目录" "${parent}"
  parent="$(cd -P "${parent}" && pwd)" || die "无法解析隔离 venv 的父目录" "${parent}"
  CI_VENV="${parent}/${name}"

  if [[ -d "${CI_VENV}" ]]; then
    existing_path="$(cd -P "${CI_VENV}" && pwd)" || die "无法访问隔离 venv" "${CI_VENV}"
    if [[ "${existing_path}" == "${REPO_ROOT}/.venv" ]]; then
      die "SW_CI_VENV 不可指向仓库开发环境 .venv" "请改用仓库外目录或 .venv-ci"
    fi
  elif [[ "${CI_VENV}" == "${REPO_ROOT}/.venv" ]]; then
    die "SW_CI_VENV 不可指向仓库开发环境 .venv" "请改用仓库外目录或 .venv-ci"
  fi

  # 所有 uv 调用共享这一固定环境，绝不复用仓库默认 .venv。
  export UV_PROJECT_ENVIRONMENT="${CI_VENV}"
}

TEST_STATUS="SKIP"
TEST_DURATION="0s"
TEST_REASON="未选择"
COMPOSE_STATUS="SKIP"
COMPOSE_DURATION="0s"
COMPOSE_REASON="未选择"
SOAK_STATUS="SKIP"
SOAK_DURATION="0s"
SOAK_REASON="未选择"
RENDER_STATUS="SKIP"
RENDER_DURATION="0s"
RENDER_REASON="未选择"
OPS_STATUS="SKIP"
OPS_DURATION="0s"
OPS_REASON="未选择"

RUN_TEST=0
RUN_COMPOSE=0
RUN_SOAK=0
RUN_RENDER=0
RUN_OPS=0

list_jobs() {
  printf '%s\n' test compose soak render-smoke ops
}

usage() {
  printf '用法：bash scripts/ci_local.sh [test] [compose] [soak] [render-smoke] [ops]\n'
  printf '      bash scripts/ci_local.sh --list\n'
}

require_uv() {
  if command -v uv >/dev/null 2>&1; then
    return 0
  fi
  note "本机没有 uv，无法复现该必需 job"
  return 1
}

run_required() {
  local label="$1"
  shift

  note "${label}"
  if "$@"; then
    ok "${label}"
    return 0
  fi

  printf '  失败：%s\n' "${label}" >&2
  return 1
}

run_required_quiet() {
  local label="$1"
  shift

  note "${label}"
  if "$@" >/dev/null; then
    ok "${label}"
    return 0
  fi

  printf '  失败：%s\n' "${label}" >&2
  return 1
}

check_chromium() {
  uv run --no-sync python - <<'PY'
from pathlib import Path

from playwright.sync_api import sync_playwright

with sync_playwright() as playwright:
    executable = Path(playwright.chromium.executable_path)
    if not executable.is_file():
        raise SystemExit(f"chromium 不存在：{executable}")
PY
}

run_test() {
  printf '\ntest\n\n'

  if ! require_uv; then
    return 1
  fi
  if ! run_required "同步依赖（uv sync --frozen）" uv sync --frozen; then
    return 1
  fi
  if ! run_required "Lint（ruff check）" uv run ruff check --output-format=github .; then
    return 1
  fi
  if ! run_required "格式检查（ruff format --check）" uv run ruff format --check .; then
    return 1
  fi
  if ! run_required "测试（pytest）" uv run pytest -q; then
    return 1
  fi

  note "Preflight（offline，退出码按 CI 忽略）"
  if uv run python scripts/preflight.py --offline; then
    ok "Preflight（offline）"
  else
    note "Preflight（offline）已执行，退出码按 CI 语义忽略"
  fi

  note "License 台账复核（禁止 GPL 代码依赖）"
  if uv run python - <<'PY'
import sys
from importlib.metadata import distributions
bad = []
for d in distributions():
    md = d.metadata
    text = (md.get("License-Expression") or "") + " " + " ".join(md.get_all("Classifier") or [])
    if "GPL" in text.upper():
        bad.append(f"{md['Name']} :: {text.strip()}")
if bad:
    print("发现 GPL/AGPL 代码依赖，违反 docs/THIRD_PARTY.md 策略：")
    print("\n".join(bad))
    sys.exit(1)
print("License 复核通过：无 GPL/AGPL 代码依赖")
PY
  then
    ok "License 台账复核"
  else
    printf '  失败：License 台账复核\n' >&2
    return 1
  fi
}

run_compose() {
  printf '\ncompose\n\n'

  if ! command -v docker >/dev/null 2>&1; then
    COMPOSE_REASON="本机没有 docker"
    return 20
  fi
  if ! docker compose version >/dev/null 2>&1; then
    COMPOSE_REASON="docker 未提供 compose 插件"
    return 20
  fi
  if ! require_uv; then
    return 1
  fi
  if ! run_required "同步依赖（uv sync --frozen）" uv sync --frozen; then
    return 1
  fi
  if ! run_required "生成小红书 sidecar compose 片段" uv run python scripts/gen_xhs_sidecars.py; then
    return 1
  fi

  note "准备 sidecar 配置占位"
  if mkdir -p sidecars/trendradar/config &&
      cp sidecars/trendradar/config.example.yaml sidecars/trendradar/config/config.yaml &&
      cp sidecars/trendradar/frequency_words.example.txt sidecars/trendradar/config/frequency_words.txt; then
    ok "准备 sidecar 配置占位"
  else
    printf '  失败：准备 sidecar 配置占位\n' >&2
    return 1
  fi

  if ! run_required_quiet "校验 compose 语法（默认组合）" docker compose config; then
    return 1
  fi
  if ! run_required_quiet "校验 compose 语法（含 xhs）" docker compose -f docker-compose.yml -f docker-compose.xhs.yml config; then
    return 1
  fi
  if ! run_required_quiet "校验 compose 语法（含 profile）" docker compose --profile video --profile xhs --profile sourcing config; then
    return 1
  fi
}

run_soak() {
  printf '\nsoak\n\n'

  if ! require_uv; then
    return 1
  fi
  if ! run_required "同步依赖（uv sync --frozen）" uv sync --frozen; then
    return 1
  fi
  run_required "连续运行验证（soak）" uv run python scripts/soak.py --json
}

run_render() {
  local failed=0

  printf '\nrender-smoke\n\n'

  if ! command -v uv >/dev/null 2>&1; then
    RENDER_REASON="本机没有 uv，无法检查 render 环境"
    return 20
  fi
  note "同步依赖（uv sync --frozen --extra render）"
  if ! uv sync --frozen --extra render; then
    RENDER_REASON="render 依赖同步失败"
    return 1
  fi
  if ! check_chromium; then
    RENDER_REASON="未安装 chromium（运行 UV_PROJECT_ENVIRONMENT=${CI_VENV} uv run --no-sync playwright install chromium）"
    return 20
  fi

  if ! run_required "卡片 / 封面真实截图 smoke" uv run pytest -q -m render; then
    failed=1
  fi
  if ! run_required "公众号 wenyan 渲染 smoke（需要 Node）" uv run pytest -q -m node; then
    failed=1
  fi

  if [[ "${failed}" -ne 0 ]]; then
    return 1
  fi
}

run_ops() {
  printf '\nops\n\n'

  local test_dir="${REPO_ROOT}/tests/ops"
  local -a test_files=()
  local f
  local rc
  local passed=0
  local -a fail_details=()

  while IFS= read -r f; do
    test_files+=("${f}")
  done < <(LC_ALL=C find "${test_dir}" -maxdepth 1 -type f -name 'test_*.sh' 2>/dev/null | LC_ALL=C sort)

  if [[ "${#test_files[@]}" -eq 0 ]]; then
    OPS_REASON="tests/ops 下没有可执行的测试"
    return 1
  fi

  # 与 .github/workflows/ci.yml 的「Shell 静态检查」同一条命令。本机跑不到它，
  # 就只能在 CI 上才发现——2026-08-25 真栽过一次（info 级也让 shellcheck 退 1）。
  if command -v shellcheck >/dev/null 2>&1; then
    note "shellcheck"
    if shellcheck "${REPO_ROOT}"/scripts/ops/*.sh "${REPO_ROOT}"/tests/ops/*.sh; then
      ok "shellcheck"
    else
      rc=$?
      printf '  失败：shellcheck（退出码 %s）\n' "${rc}" >&2
      fail_details+=("shellcheck（退出码 ${rc}）")
    fi
  else
    note "跳过 shellcheck（本机没装；CI 上是必过项）"
  fi

  for f in "${test_files[@]}"; do
    note "$(basename "${f}")"
    if bash "${f}"; then
      ok "$(basename "${f}")"
      passed=$((passed + 1))
    else
      rc=$?
      printf '  失败：%s（退出码 %s）\n' "$(basename "${f}")" "${rc}" >&2
      fail_details+=("$(basename "${f}")（退出码 ${rc}）")
    fi
  done

  if [[ "${#fail_details[@]}" -gt 0 ]]; then
    local joined=""
    local item
    for item in "${fail_details[@]}"; do
      if [[ -z "${joined}" ]]; then
        joined="${item}"
      else
        joined="${joined}, ${item}"
      fi
    done
    OPS_REASON="失败：${joined}"
    return 1
  fi

  OPS_REASON="全部通过（${passed} 个）"
}

record_result() {
  local job="$1"
  local started="$2"
  local rc="$3"
  local elapsed=$(( $(date +%s) - started ))

  case "${job}" in
    test)
      TEST_DURATION="${elapsed}s"
      if [[ "${rc}" -eq 0 ]]; then TEST_STATUS="PASS"; TEST_REASON=""; else TEST_STATUS="FAIL"; TEST_REASON="必需检查失败"; fi
      ;;
    compose)
      COMPOSE_DURATION="${elapsed}s"
      if [[ "${rc}" -eq 0 ]]; then COMPOSE_STATUS="PASS"; COMPOSE_REASON="";
      elif [[ "${rc}" -eq 20 ]]; then COMPOSE_STATUS="SKIP";
      else COMPOSE_STATUS="FAIL"; COMPOSE_REASON="必需检查失败"; fi
      ;;
    soak)
      SOAK_DURATION="${elapsed}s"
      if [[ "${rc}" -eq 0 ]]; then SOAK_STATUS="PASS"; SOAK_REASON=""; else SOAK_STATUS="FAIL"; SOAK_REASON="必需检查失败"; fi
      ;;
    render-smoke)
      RENDER_DURATION="${elapsed}s"
      if [[ "${rc}" -eq 0 ]]; then RENDER_STATUS="PASS"; RENDER_REASON="";
      elif [[ "${rc}" -eq 20 ]]; then RENDER_STATUS="SKIP";
      else
        RENDER_STATUS="WARN"
        if [[ -z "${RENDER_REASON}" ]]; then RENDER_REASON="CI 中 continue-on-error"; fi
      fi
      ;;
    ops)
      OPS_DURATION="${elapsed}s"
      if [[ "${rc}" -eq 0 ]]; then OPS_STATUS="PASS"; else OPS_STATUS="FAIL"; fi
      ;;
  esac
}

run_selected_job() {
  local job="$1"
  local started
  local rc=0

  started="$(date +%s)"
  case "${job}" in
    test) if run_test; then rc=0; else rc=$?; fi ;;
    compose) if run_compose; then rc=0; else rc=$?; fi ;;
    soak) if run_soak; then rc=0; else rc=$?; fi ;;
    render-smoke) if run_render; then rc=0; else rc=$?; fi ;;
    ops) if run_ops; then rc=0; else rc=$?; fi ;;
  esac
  record_result "${job}" "${started}" "${rc}"
}

print_summary() {
  printf '\n本地 CI 汇总\n\n'
  printf '  %-14s %-6s %-8s %s\n' "job" "结果" "耗时" "说明"
  printf '  %-14s %-6s %-8s %s\n' "test" "${TEST_STATUS}" "${TEST_DURATION}" "${TEST_REASON}"
  printf '  %-14s %-6s %-8s %s\n' "compose" "${COMPOSE_STATUS}" "${COMPOSE_DURATION}" "${COMPOSE_REASON}"
  printf '  %-14s %-6s %-8s %s\n' "soak" "${SOAK_STATUS}" "${SOAK_DURATION}" "${SOAK_REASON}"
  printf '  %-14s %-6s %-8s %s\n' "render-smoke" "${RENDER_STATUS}" "${RENDER_DURATION}" "${RENDER_REASON}"
  printf '  %-14s %-6s %-8s %s\n' "ops" "${OPS_STATUS}" "${OPS_DURATION}" "${OPS_REASON}"
}

if [[ "${1:-}" == "--list" ]]; then
  [[ "$#" -eq 1 ]] || die "--list 不接受其他参数"
  list_jobs
  exit 0
fi

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

if [[ "$#" -eq 0 ]]; then
  RUN_TEST=1
  RUN_COMPOSE=1
  RUN_SOAK=1
  RUN_RENDER=1
  RUN_OPS=1
else
  for job in "$@"; do
    case "${job}" in
      test) RUN_TEST=1; TEST_REASON="" ;;
      compose) RUN_COMPOSE=1; COMPOSE_REASON="" ;;
      soak) RUN_SOAK=1; SOAK_REASON="" ;;
      render-smoke) RUN_RENDER=1; RENDER_REASON="" ;;
      ops) RUN_OPS=1; OPS_REASON="" ;;
      *) die "未知 job：${job}" "可用 job：test、compose、soak、render-smoke、ops" ;;
    esac
  done
fi

resolve_ci_venv "${SW_CI_VENV:-.venv-ci}"

cd "${REPO_ROOT}"
printf '本地 CI 复现\n'
note "仓库：${REPO_ROOT}"
note "隔离 venv：${CI_VENV}（不会修改 ${REPO_ROOT}/.venv）"
if [[ ! -d "${CI_VENV}" ]]; then
  note "隔离 venv 首次同步会较慢，请稍候"
fi

if [[ "${RUN_TEST}" -eq 1 ]]; then run_selected_job test; fi
if [[ "${RUN_COMPOSE}" -eq 1 ]]; then run_selected_job compose; fi
if [[ "${RUN_SOAK}" -eq 1 ]]; then run_selected_job soak; fi
if [[ "${RUN_RENDER}" -eq 1 ]]; then run_selected_job render-smoke; fi
if [[ "${RUN_OPS}" -eq 1 ]]; then run_selected_job ops; fi

print_summary

if [[ "${TEST_STATUS}" == "FAIL" || "${COMPOSE_STATUS}" == "FAIL" || "${SOAK_STATUS}" == "FAIL" || "${OPS_STATUS}" == "FAIL" ]]; then
  exit 1
fi
exit 0
