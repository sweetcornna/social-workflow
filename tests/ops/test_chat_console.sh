#!/bin/bash
# scripts/chat_console.sh 的工作台 API token 接线。
# 零网络、零 Electron、零 hermes：curl / hermes / python / npx 全是本地的 argv 记录假件。
#
# 【为什么这份测试放在 tests/ops/ 而不是 tests/】它测的是 scripts/ops/ui_token.sh 这条凭据
# 通路的第五个消费方（前四个是 verify/update/restart/status）。断言体例、假件写法、
# 「token 明文在 argv 记录里必须 0 次」这条硬断言都与那四份一致，放在一起才好一起改。
# scripts/ci_local.sh 的 ops job 会自动发现 tests/ops/test_*.sh，不需要登记。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="${ROOT}/scripts/chat_console.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

# ── 假件 ───────────────────────────────────────────────────────────────────
# TEST_LOG   记录每一次边界调用的**完整 argv**。token 明文在这份记录里必须是 0 次。
# TEST_ENV_LOG 分开记录子进程**环境**里的 token。只证"没泄漏"不证"送到了"不算数，
#              所以两份记录必须分开，断言也分开。
mkdir -p "${TMP}/bin"

cat >"${TMP}/bin/curl" <<'EOF'
#!/bin/bash
{
  printf 'curl'
  printf ' <%s>' "$@"
  printf '\n'
} >>"${TEST_LOG}"
# `--config -` 时把配置流读干净并单独记档：这是 Authorization 头真正走的那条路。
for a in "$@"; do
  if [[ "${a}" == "-" ]]; then
    while IFS= read -r line; do
      printf 'curl-config <%s>\n' "${line}" >>"${TEST_ENV_LOG}"
    done
    break
  fi
done
printf '%s' "${TEST_HTTP_CODE:-200}"
[[ "${TEST_CURL_STATUS:-0}" -eq 0 ]] || exit "${TEST_CURL_STATUS}"
EOF

cat >"${TMP}/bin/hermes" <<'EOF'
#!/bin/bash
if [[ "${1:-}" == "--version" ]]; then
  printf 'Hermes Agent v0.20.4 (fake)\n'
  exit 0
fi
{
  printf 'hermes'
  printf ' <%s>' "$@"
  printf '\n'
} >>"${TEST_LOG}"
# 子进程环境里 SW_UI_TOKEN 到底是什么。分开记档（见文件头）。
printf 'child-env SW_UI_TOKEN=<%s> set=<%s>\n' "${SW_UI_TOKEN:-}" "${SW_UI_TOKEN+yes}" >>"${TEST_ENV_LOG}"
EOF

cat >"${TMP}/bin/python" <<'EOF'
#!/bin/bash
# 只被用来跑 `-c "import mcp"`。默认装了。
[[ "${TEST_NO_MCP:-0}" -eq 0 ]] || exit 1
exit 0
EOF

chmod +x "${TMP}/bin/"*

# 假的 sw-hermes-desktop 检出：只要脚本体检要看的那几个文件在就够了。
FAKE_DESKTOP="${TMP}/desktop"
mkdir -p "${FAKE_DESKTOP}/.venv/bin" "${FAKE_DESKTOP}/apps/desktop/dist" "${FAKE_DESKTOP}/node_modules"
cp "${TMP}/bin/hermes" "${FAKE_DESKTOP}/.venv/bin/hermes"
cp "${TMP}/bin/python" "${FAKE_DESKTOP}/.venv/bin/python"
chmod +x "${FAKE_DESKTOP}/.venv/bin/"*
: >"${FAKE_DESKTOP}/apps/desktop/dist/index.html"

FAKE_HERMES_HOME="${TMP}/hermes-home"
PROFILE_CFG="${FAKE_HERMES_HOME}/profiles/sw/config.yaml"
mkdir -p "$(dirname "${PROFILE_CFG}")"

FAKE_HOME="${TMP}/home"
CRED="${FAKE_HOME}/.dsh-sw/.credentials.yaml"
mkdir -p "${FAKE_HOME}/.dsh-sw"

# 三个形状各异但都在白名单内的假 token（A-Za-z0-9 . _ - + / = : @）。
TOKEN_ENV_UI='FAKEuiTOKEN-0001'
TOKEN_ENV_OPS='FAKEopsTOKEN-0002'
TOKEN_CRED='FAKEcredTOKEN-0003'

# config.yaml 的两种形态。转发那一份写的是**字面占位符**，不是值。
write_cfg_pinned() {
  cat >"${PROFILE_CFG}" <<'YAML'
mcp_servers:
  workbench:
    command: uv
    env:
      SW_MCP_BASE_URL: http://127.0.0.1:8000
      SW_UI_TOKEN: ''
YAML
}
write_cfg_forward() {
  cat >"${PROFILE_CFG}" <<'YAML'
mcp_servers:
  workbench:
    command: uv
    env:
      SW_MCP_BASE_URL: http://127.0.0.1:8000
      SW_UI_TOKEN: '${SW_UI_TOKEN}'
YAML
}
write_cred() {
  printf 'sw_ui_token: %s\n' "$1" >"${CRED}"
  chmod 600 "${CRED}"
}
clear_cred() { rm -f "${CRED}"; }

# ── 断言 ───────────────────────────────────────────────────────────────────
failures=0
cases=0
case_name=""
RESULT=""
STATUS=0
LOG=""
ENV_LOG=""

fail_assertion() {
  printf 'FAIL [%s] %s\n' "${case_name}" "$1" >&2
  failures=$((failures + 1))
}

assert_status() {
  local expected="$1"
  [[ "${STATUS}" -eq "${expected}" ]] \
    || fail_assertion "status=${STATUS}, expected=${expected}; output: ${RESULT}"
}

assert_contains() {
  [[ "$1" == *"$2"* ]] || fail_assertion "missing [$2]; value: $1"
}

assert_not_contains() {
  [[ "$1" != *"$2"* ]] || fail_assertion "unexpected [$2]; value: $1"
}

# token 明文在 argv 记录与脚本输出里都必须是 0 次。
assert_token_never_shown() {
  local token="$1" hits
  hits="$(grep -F -c -- "${token}" <<<"${LOG}" || true)"
  [[ "${hits}" -eq 0 ]] \
    || fail_assertion "token 明文在 argv 记录里出现了 ${hits} 次，必须是 0；log: ${LOG}"
  hits="$(grep -F -c -- "${token}" <<<"${RESULT}" || true)"
  [[ "${hits}" -eq 0 ]] \
    || fail_assertion "token 明文在脚本输出里出现了 ${hits} 次，必须是 0；output: ${RESULT}"
}

# 用法：run_case "名字" [mode] [VAR=VAL ...]
# 默认 doctor 模式（只体检，什么都不起）。
run_case() {
  case_name="$1"; shift
  local mode="doctor"
  if [[ "${1:-}" != *=* && -n "${1:-}" ]]; then mode="$1"; shift; fi
  cases=$((cases + 1))
  : >"${TMP}/log"
  : >"${TMP}/env-log"
  set +e
  RESULT="$(
    env -i \
      PATH="${TMP}/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
      HOME="${FAKE_HOME}" \
      TEST_LOG="${TMP}/log" \
      TEST_ENV_LOG="${TMP}/env-log" \
      SW_DESKTOP_ROOT="${FAKE_DESKTOP}" \
      SW_HERMES_HOME="${FAKE_HERMES_HOME}" \
      SW_CHAT_USER_DATA="${TMP}/userdata" \
      "$@" \
      /bin/bash "${SCRIPT}" "${mode}" 2>&1
  )"
  STATUS=$?
  set -e
  LOG="$(cat "${TMP}/log")"
  ENV_LOG="$(cat "${TMP}/env-log")"
}

# ── 用例 ───────────────────────────────────────────────────────────────────

write_cfg_pinned
clear_cred

# 1. 三个来源都没有 = core 未开鉴权的常态形态：输出里一个 token 字都不许有。
run_case "no token anywhere prints nothing about tokens"
assert_status 0
assert_not_contains "${RESULT}" "token"
assert_not_contains "${RESULT}" "已加载"
assert_not_contains "${ENV_LOG}" "Authorization"

# 2. 三种取值路径，各自报对来源。
run_case "env SW_UI_TOKEN is picked up and named" SW_UI_TOKEN="${TOKEN_ENV_UI}"
assert_status 0
assert_contains "${RESULT}" "已加载（来源：环境变量 SW_UI_TOKEN）"
assert_token_never_shown "${TOKEN_ENV_UI}"

run_case "env SW_OPS_UI_TOKEN is picked up and named" SW_OPS_UI_TOKEN="${TOKEN_ENV_OPS}"
assert_status 0
assert_contains "${RESULT}" "已加载（来源：环境变量 SW_OPS_UI_TOKEN）"
assert_token_never_shown "${TOKEN_ENV_OPS}"

write_cred "${TOKEN_CRED}"
run_case "credentials file sw_ui_token is picked up and named"
assert_status 0
assert_contains "${RESULT}" "的 sw_ui_token 键）"
assert_token_never_shown "${TOKEN_CRED}"

# 3. 优先级：SW_UI_TOKEN > SW_OPS_UI_TOKEN > 凭据文件。凭据文件此刻仍有值。
run_case "SW_UI_TOKEN wins over SW_OPS_UI_TOKEN" \
  SW_UI_TOKEN="${TOKEN_ENV_UI}" SW_OPS_UI_TOKEN="${TOKEN_ENV_OPS}"
assert_status 0
assert_contains "${RESULT}" "来源：环境变量 SW_UI_TOKEN）"
assert_token_never_shown "${TOKEN_ENV_UI}"
assert_token_never_shown "${TOKEN_ENV_OPS}"

run_case "SW_OPS_UI_TOKEN wins over the credentials file" SW_OPS_UI_TOKEN="${TOKEN_ENV_OPS}"
assert_status 0
assert_contains "${RESULT}" "来源：环境变量 SW_OPS_UI_TOKEN）"

# 4. 空串 = 本次显式不带 token，**不回落**去读凭据文件（与 ops 侧逐字同一语义）。
run_case "an exported empty SW_UI_TOKEN means 'no token this run'" SW_UI_TOKEN=""
assert_status 0
assert_not_contains "${RESULT}" "已加载"
assert_token_never_shown "${TOKEN_CRED}"

run_case "an exported empty SW_OPS_UI_TOKEN means 'no token this run'" SW_OPS_UI_TOKEN=""
assert_status 0
assert_not_contains "${RESULT}" "已加载"
assert_token_never_shown "${TOKEN_CRED}"

clear_cred

# 5. 字符集白名单：不合法就在任何网络动作之前报错退出，且不回显值。
run_case "an out-of-whitelist token dies before any network call" SW_UI_TOKEN='bad token'
assert_status 1
assert_contains "${RESULT}" "不被允许的字符"
assert_contains "${RESULT}" "此处不回显 token 值"
assert_not_contains "${LOG}" "curl <"

run_case "a backslash token is rejected too" SW_UI_TOKEN='back\slash'
assert_status 1
assert_contains "${RESULT}" "不被允许的字符"

# 6. config.yaml 的转发接线：两种错配都要明说，配对了不许吵。
run_case "token loaded but config.yaml does not forward it warns" SW_UI_TOKEN="${TOKEN_ENV_UI}"
assert_status 0
assert_contains "${RESULT}" "取到了 token，但 MCP 子进程收不到它"
assert_contains "${RESULT}" "_build_safe_env"
assert_token_never_shown "${TOKEN_ENV_UI}"

write_cfg_forward
run_case "token loaded and config.yaml forwards it is quiet" SW_UI_TOKEN="${TOKEN_ENV_UI}"
assert_status 0
assert_contains "${RESULT}" "config.yaml 已把它转发给 MCP 子进程"
assert_not_contains "${RESULT}" "收不到它"
assert_token_never_shown "${TOKEN_ENV_UI}"

# 反向错配：config.yaml 写了转发但没取到 token。告警必须把**两种成因**分开说——
# 它们在 core 那边的 401 文案不同（本机实测），那是排查时唯一的区分线索：
#   · 变量未设置 → hermes 第一层插值保留字面量 → 子进程拿 ${SW_UI_TOKEN} 当 token 发
#     → core 答「token 不正确」
#   · 变量是空串 → 拿到 ''（不是字面量）→ 不发头 → core 答「缺少 Authorization」
# 这条断言钉住的就是"别把空串那种说成保留字面量"（那是本文案改过的一版真错误）。
run_case "config.yaml forwards but no token is configured warns"
assert_status 0
assert_contains "${RESULT}" "但本机没取到 token"
assert_contains "${RESULT}" "未设置"
assert_contains "${RESULT}" "保留字面量"
assert_contains "${RESULT}" "token 不正确"
assert_contains "${RESULT}" "缺少 Authorization"
# 归因不许再退回旧版：空串那条必须明说**不是**字面量。
assert_contains "${RESULT}" "不是**字面量"
write_cfg_pinned

# 7. core 探针：401 与"连不上"必须分得开。401 不是"core 没起来"。
run_case "a 401 from core is reported as auth, not as a dead core" \
  SW_UI_TOKEN="${TOKEN_ENV_UI}" TEST_HTTP_CODE=401
assert_status 0
assert_contains "${RESULT}" "core 活着，但拒绝了这次探活"
assert_not_contains "${RESULT}" "core 没起来"
assert_token_never_shown "${TOKEN_ENV_UI}"

run_case "an unreachable core still reads as unreachable" TEST_HTTP_CODE=000 TEST_CURL_STATUS=7
assert_status 0
assert_contains "${RESULT}" "core 没起来"
assert_not_contains "${RESULT}" "拒绝了这次探活"

run_case "a 500 is not misreported as a bare connection failure" TEST_HTTP_CODE=500
assert_status 0
assert_contains "${RESULT}" "实际拿到 HTTP 500"

# 8. 探针带头：token 只走 `curl --config -` 的配置流，绝不进 curl 的 argv。
run_case "the probe sends Bearer via the curl config stream, never argv" SW_UI_TOKEN="${TOKEN_ENV_UI}"
assert_status 0
assert_contains "${ENV_LOG}" "Authorization: Bearer ${TOKEN_ENV_UI}"
assert_contains "${LOG}" "<--config> <->"
assert_token_never_shown "${TOKEN_ENV_UI}"

# `-q` 必须是 curl 的第一个参数才生效（挡掉 ~/.curlrc 把 Authorization 头 trace 落盘）。
case_name="curl -q is the first argument"
cases=$((cases + 1))
if ! grep -q '^curl <-q>' "${TMP}/log"; then
  fail_assertion "curl 的第一个参数必须是 -q；log: ${LOG}"
fi

run_case "no token means no Authorization header at all"
assert_status 0
assert_not_contains "${ENV_LOG}" "Authorization"
assert_not_contains "${LOG}" "--config"

# 9. token 真的进了后端子进程的**环境**（不是 argv）。serve 模式 exec 的是 hermes。
write_cfg_forward
run_case "serve mode hands the token to the backend via the environment" serve \
  SW_UI_TOKEN="${TOKEN_ENV_UI}"
assert_status 0
assert_contains "${ENV_LOG}" "child-env SW_UI_TOKEN=<${TOKEN_ENV_UI}> set=<yes>"
assert_contains "${LOG}" "hermes <--profile> <sw> <serve>"
assert_token_never_shown "${TOKEN_ENV_UI}"

# 取不到 token 时子进程拿到的是空串——与"未设置"对下游完全等价
# （workbench_mcp.py:179 是 `os.environ.get(...) or ""`），但报的来源与实际一致。
run_case "serve mode with no token hands the backend an empty value" serve
assert_status 0
assert_contains "${ENV_LOG}" "child-env SW_UI_TOKEN=<> set=<yes>"
write_cfg_pinned

# 10. `bash -x` 不许打出 token。这是红线 R5 里最容易被踩的一条：401 修不好就开 -x
#     看看、再把输出贴进工单，是最自然的排查动作。
case_name="bash -x never prints the token"
cases=$((cases + 1))
: >"${TMP}/log"
: >"${TMP}/env-log"
write_cfg_forward
set +e
xtrace_out="$(
  env -i \
    PATH="${TMP}/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
    HOME="${FAKE_HOME}" \
    TEST_LOG="${TMP}/log" \
    TEST_ENV_LOG="${TMP}/env-log" \
    SW_DESKTOP_ROOT="${FAKE_DESKTOP}" \
    SW_HERMES_HOME="${FAKE_HERMES_HOME}" \
    SW_CHAT_USER_DATA="${TMP}/userdata" \
    SW_UI_TOKEN="${TOKEN_ENV_UI}" \
    /bin/bash -x "${SCRIPT}" serve 2>&1
)"
xtrace_status=$?
set -e
[[ "${xtrace_status}" -eq 0 ]] || fail_assertion "bash -x 跑不通，status=${xtrace_status}"
xtrace_hits="$(grep -F -c -- "${TOKEN_ENV_UI}" <<<"${xtrace_out}" || true)"
[[ "${xtrace_hits}" -eq 0 ]] \
  || fail_assertion "bash -x 的输出里 token 出现了 ${xtrace_hits} 次，必须是 0"
# 反向确认这一趟确实走到了带 token 的那条路（否则 0 次是因为压根没加载）。
assert_contains "$(cat "${TMP}/env-log")" "child-env SW_UI_TOKEN=<${TOKEN_ENV_UI}>"
assert_contains "${xtrace_out}" "已加载（来源：环境变量 SW_UI_TOKEN）"

# 凭据文件那条路也过一遍 -x：它多走一段 grep + 正则匹配，泄漏点与环境变量那条不同。
case_name="bash -x never prints the token read from the credentials file"
cases=$((cases + 1))
write_cred "${TOKEN_CRED}"
: >"${TMP}/log"
: >"${TMP}/env-log"
set +e
xtrace_out="$(
  env -i \
    PATH="${TMP}/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
    HOME="${FAKE_HOME}" \
    TEST_LOG="${TMP}/log" \
    TEST_ENV_LOG="${TMP}/env-log" \
    SW_DESKTOP_ROOT="${FAKE_DESKTOP}" \
    SW_HERMES_HOME="${FAKE_HERMES_HOME}" \
    SW_CHAT_USER_DATA="${TMP}/userdata" \
    /bin/bash -x "${SCRIPT}" serve 2>&1
)"
xtrace_status=$?
set -e
[[ "${xtrace_status}" -eq 0 ]] || fail_assertion "bash -x 跑不通，status=${xtrace_status}"
xtrace_hits="$(grep -F -c -- "${TOKEN_CRED}" <<<"${xtrace_out}" || true)"
[[ "${xtrace_hits}" -eq 0 ]] \
  || fail_assertion "bash -x 的输出里凭据文件的 token 出现了 ${xtrace_hits} 次，必须是 0"
assert_contains "$(cat "${TMP}/env-log")" "child-env SW_UI_TOKEN=<${TOKEN_CRED}>"
clear_cred
write_cfg_pinned

# 11. 静态断言：本脚本必须复用 scripts/ops/ui_token.sh，不许另起一套取值逻辑。
#     改成自己写一份 grep 凭据文件 / 自己写字符集正则时，这条会红。
case_name="chat_console.sh reuses scripts/ops/ui_token.sh"
cases=$((cases + 1))
grep -q 'ops/ui_token.sh' "${SCRIPT}" \
  || fail_assertion "chat_console.sh 不再 source scripts/ops/ui_token.sh"
grep -q 'sw_ops_load_ui_token' "${SCRIPT}" \
  || fail_assertion "chat_console.sh 不再调用 sw_ops_load_ui_token"
if grep -q 'credentials.yaml"' "${SCRIPT}"; then
  fail_assertion "chat_console.sh 里出现了自己读凭据文件的代码；取值应全权交给 ui_token.sh"
fi

# 12. 静态断言：红线 R4——工具面里不许出现「确认发布」。本脚本只接鉴权，不加工具。
case_name="no confirm tool sneaks into the MCP surface"
cases=$((cases + 1))
if grep -qE '^@mcp\.tool.*$' "${ROOT}/scripts/workbench_mcp.py" \
   && grep -qiE 'def (confirm|publish_confirm|approve_publish)' "${ROOT}/scripts/workbench_mcp.py"; then
  fail_assertion "workbench_mcp.py 里出现了确认发布类工具（红线 R4）"
fi

if [[ "${failures}" -ne 0 ]]; then
  printf 'chat_console.sh token wiring tests failed: %s assertion(s)\n' "${failures}" >&2
  exit 1
fi
printf 'chat_console.sh token wiring tests passed: %s case(s)\n' "${cases}"
