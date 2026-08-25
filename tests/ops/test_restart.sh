#!/bin/bash
# No network, SSH, or Docker: every boundary command is a local argv-recording fake.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="${ROOT}/scripts/ops/restart.sh"
# 被测脚本路径。默认是仓库里那一份；「stdin 结构性保证」一节会临时指向改写过的副本。
SCRIPT_UNDER_TEST="${SCRIPT}"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT
mkdir -p "${TMP}/bin" "${TMP}/home dir"
ln -s "${ROOT}" "${TMP}/home dir/social_workflow"

cat >"${TMP}/bin/bash" <<'EOF'
#!/bin/bash
if [[ "${1:-}" == */backup.sh ]]; then
  printf 'backup <%s>\n' "$1" >>"${TEST_LOG}"
  exit 0
fi
depth="${SW_FAKE_BASH_DEPTH:-0}"
if [[ "${1:-}" == "-s" ]]; then
  printf 'remote-bash <-s> depth=%s\n' "${depth}" >>"${TEST_LOG}"
  # depth=0 是 ssh 送达远端的那层状态规范化包装；depth=1 是它转交的内层重启脚本。
  # 在 depth=1 上强制退出码，就能在零 SSH 的前提下考察包装层的 255→254 规范化。
  if [[ "${depth}" -eq 1 && -n "${TEST_REMOTE_EXIT:-}" ]]; then
    cat >/dev/null
    printf 'remote-forced-exit <%s>\n' "${TEST_REMOTE_EXIT}" >>"${TEST_LOG}"
    exit "${TEST_REMOTE_EXIT}"
  fi
  export SW_FAKE_BASH_DEPTH=$((depth + 1))
fi
exec /bin/bash "$@"
EOF

cat >"${TMP}/bin/ssh" <<'EOF'
#!/bin/bash
{
  printf 'ssh'
  printf ' <%s>' "$@"
  printf '\n'
} >>"${TEST_LOG}"
ssh_attempt="$(grep -c '^ssh <' "${TEST_LOG}" || true)"
IFS=',' read -r -a ssh_statuses <<<"${TEST_SSH_STATUSES:-0}"
ssh_status="${ssh_statuses[$((ssh_attempt - 1))]:-${ssh_statuses[${#ssh_statuses[@]} - 1]}}"
[[ "${ssh_status}" -eq 0 ]] || exit "${ssh_status}"

# 忠实模拟真实 ssh(1)：ssh 不保留 argv 边界。host 之后的全部参数被用单个空格拼成一个
# 字符串发给远端，由远端登录 shell 重新分词；stdin（heredoc）原样继承。
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    -B|-b|-c|-D|-E|-e|-F|-I|-i|-J|-L|-l|-m|-O|-o|-p|-Q|-R|-S|-W|-w)
      shift 2 || exit 98
      ;;
    -*)
      shift
      ;;
    *)
      break
      ;;
  esac
done
[[ "$#" -gt 0 ]] || exit 98
shift  # 目标主机
[[ "$#" -gt 0 ]] || exit 98
remote_command="$1"
shift
while [[ "$#" -gt 0 ]]; do
  remote_command="${remote_command} $1"
  shift
done
printf 'ssh-command <%s>\n' "${remote_command}" >>"${TEST_LOG}"
/bin/bash -c "${remote_command}"
remote_status=$?
printf 'ssh-remote-status <%s>\n' "${remote_status}" >>"${TEST_LOG}"
exit "${remote_status}"
EOF

cat >"${TMP}/bin/docker" <<'EOF'
#!/bin/bash
set -uo pipefail
{
  printf 'docker'
  printf ' <%s>' "$@"
  printf '\n'
} >>"${TEST_LOG}"

if [[ "$1 ${2:-} ${3:-} ${4:-} ${5:-} ${6:-}" == "compose exec -T core python3 -c" ]]; then
  # R1 闸门用的内联解析。真实 `docker compose exec -T` 转发 stdin，而被模拟的容器内进程
  # 自己就 sys.stdin.read()，所以直接 exec 出去即是忠实语义：stdin 被读干净。调用方必须
  # 自带显式 stdin 来源（`printf ... |` 管道），否则它会吃掉调用它的那份远端脚本正文。
  # 两条解析各有独立开关，刻意不与"重启/探针"的开关共用——否则"重启正常、只是闸门那条
  # docker exec 打嗝"这条真实路径根本构造不出来。按脚本正文分派。
  if [[ "$7" == *use_fake_publishers* ]]; then
    gate_exec_status="${TEST_FAKE_EXEC_STATUS:-0}"
  else
    gate_exec_status="${TEST_TELEGRAM_EXEC_STATUS:-0}"
  fi
  if [[ "${gate_exec_status}" -ne 0 ]]; then
    cat >/dev/null
    exit "${gate_exec_status}"
  fi
  exec python3 -c "$7"
fi

if [[ "$1 ${2:-} ${3:-}" == "compose restart core" ]]; then
  exit "${TEST_RESTART_STATUS:-0}"
fi

exit 0
EOF

cat >"${TMP}/bin/curl" <<'EOF'
#!/bin/bash
set -uo pipefail
{
  printf 'curl'
  printf ' <%s>' "$@"
  printf '\n'
} >>"${TEST_LOG}"

# 忠实模拟本仓真正用到的那几个 curl 语义（curl 8.7.1 本机实测，证据见
# docs/briefs/p25/p25_ops_ui_token_and_stdin.md §「假件保真度」）：
#   -f          HTTP >= 400 时**不输出响应体**，退出码 22。
#   -w <fmt>    传输结束后把 fmt 追加到 stdout；**即便 -f 判失败也照样输出**，
#               连不上时 %{http_code} 是 000。
#   -sS         失败时仍往 stderr 写一行错误说明。
#   --config -  从 **stdin** 读配置文件并读到 EOF。本仓用它注入 Authorization 头，
#               所以这里必须真的读 stdin 并解析出 `header = "..."`，不能假装读——
#               本项目已经两次被"假件语义与真命令不符"坑到（假 ssh 透传 argv、
#               假 docker 不读 stdin），不再重蹈。
# 解析出来的头写进 TEST_AUTH_LOG（**与 argv 日志分开的文件**）：argv 日志要能被断言
# "token 明文出现 0 次"，而"头确实送到了"又必须能被断言，两者不能混在一个文件里。
curl_url=""
curl_write_out=""
curl_config_stdin=0
curl_auth_header=""
curl_i=1
while [[ "${curl_i}" -le "$#" ]]; do
  curl_arg="${!curl_i}"
  case "${curl_arg}" in
    -K|--config)
      curl_i=$((curl_i + 1))
      if [[ "${curl_i}" -le "$#" && "${!curl_i}" == "-" ]]; then curl_config_stdin=1; fi
      ;;
    -w|--write-out)
      curl_i=$((curl_i + 1))
      if [[ "${curl_i}" -le "$#" ]]; then curl_write_out="${!curl_i}"; fi
      ;;
    --max-time)
      curl_i=$((curl_i + 1))
      ;;
    -*) ;;
    *) curl_url="${curl_arg}" ;;
  esac
  curl_i=$((curl_i + 1))
done

if [[ "${curl_config_stdin}" -eq 1 ]]; then
  while IFS= read -r curl_cfg || [[ -n "${curl_cfg}" ]]; do
    case "${curl_cfg}" in
      'header = "'*'"')
        curl_cfg_value="${curl_cfg#header = \"}"
        curl_auth_header="${curl_cfg_value%\"}"
        ;;
    esac
  done
  printf 'curl-config url <%s> header <%s>\n' "${curl_url}" "${curl_auth_header}" >>"${TEST_AUTH_LOG}"
fi

curl_emit() {
  # $1 = 响应体（HTTP >= 400 时按真实 -f 语义丢弃）；$2 = HTTP 状态码；$3 = curl 退出码
  local body="$1" code="$2" status="$3"
  if [[ "${status}" -eq 0 ]]; then
    printf '%s' "${body}"
  elif [[ "${status}" -eq 22 ]]; then
    printf 'curl: (22) The requested URL returned error: %s\n' "${code}" >&2
  else
    printf 'curl: (%s) fake transport failure\n' "${status}" >&2
  fi
  if [[ -n "${curl_write_out}" ]]; then
    # 假件只实现本仓用到的这一种 -w 格式。格式一变就**大声失败**，绝不静默给出错误状态码。
    if [[ "${curl_write_out}" != '\n%{http_code}' ]]; then
      printf 'curl-fake-unsupported-write-out <%s>\n' "${curl_write_out}" >>"${TEST_LOG}"
      exit 91
    fi
    printf '\n%s' "${code}"
  fi
  exit "${status}"
}

# 模拟"生产 .env 已配 SW_UI_TOKEN"之后 core 的真实行为：除 /health 外的 /api/v1/*，
# 缺头 / 非 Bearer / 值不匹配一律 401（core/api/common.py::require_token）。
# 有了它，"生产启用 token 而本机没配"这条真实路径可以在零网络下端到端跑通。
if [[ -n "${TEST_REQUIRE_TOKEN:-}" && "${curl_url}" == */api/v1/* ]]; then
  if [[ "${curl_auth_header}" != "Authorization: Bearer ${TEST_REQUIRE_TOKEN}" ]]; then
    curl_emit '' '401' 22
  fi
fi

if [[ "${curl_url}" == */api/v1/system/telegram ]]; then
  # 确认通道探针有自己的失败开关：TEST_CURL_STATUS 只管 /api/v1/system/info 那条探针，
  # 否则"探针活着但确认通道取不到"这个组合根本没法构造。
  [[ "${TEST_TELEGRAM_CURL_STATUS:-0}" -ne 0 ]] && curl_emit '' '000' "${TEST_TELEGRAM_CURL_STATUS}"
  curl_code="${TEST_TELEGRAM_HTTP_CODE:-200}"
  [[ "${curl_code}" -ge 400 ]] && curl_emit '' "${curl_code}" 22
  # 默认载荷绝不能塞进 ${VAR:-默认} 展开：JSON 里的花括号会提前终止展开、把载荷弄坏。
  if [[ "${TEST_TELEGRAM_JSON+x}" == x ]]; then
    curl_emit "${TEST_TELEGRAM_JSON}" "${curl_code}" 0
  fi
  curl_emit '{"ok":true,"data":{"enabled":true,"configured":true,"ready":true,"chat_configured":true,"can_sign":true,"polling":true,"username":"sw_ops_bot","sent":3,"failed":0,"stats":{},"detail":"","last_error":""}}' "${curl_code}" 0
fi
[[ "${TEST_CURL_STATUS:-0}" -ne 0 ]] && curl_emit '' '000' "${TEST_CURL_STATUS}"
curl_code="${TEST_INFO_HTTP_CODE:-200}"
[[ "${curl_code}" -ge 400 ]] && curl_emit '' "${curl_code}" 22
# /api/v1/system/info 的响应体：默认按 2026-08-22 生产实测形态（模拟发布器挂着）。
# 用 +x 判存在而不是 :- 判非空，好让用例能显式喂一个空 body 去构造"解析失败"。
if [[ "${TEST_INFO_JSON+x}" == x ]]; then
  curl_emit "${TEST_INFO_JSON}" "${curl_code}" 0
fi
curl_emit '{"ok":true,"data":{"version":"0.1.0","env":"prod","time":"2026-08-22T02:00:00Z","timezone":"Asia/Shanghai","scheduler_enabled":true,"generate_enabled":true,"use_fake_publishers":true,"auth_required":false,"publishers":["xhs","douyin"]}}' "${curl_code}" 0
EOF

cat >"${TMP}/bin/sleep" <<'EOF'
#!/bin/bash
exit 0
EOF

chmod +x "${TMP}/bin/"*

failures=0
cases=0
case_name=""
RESULT=""
STATUS=0
LOG=""
AUTH_LOG=""

fail_assertion() {
  printf 'FAIL [%s] %s\n' "${case_name}" "$1" >&2
  failures=$((failures + 1))
}

assert_status() {
  local expected="$1"
  if [[ "${STATUS}" -ne "${expected}" ]]; then
    fail_assertion "status=${STATUS}, expected=${expected}; output: ${RESULT}"
  fi
}

assert_contains() {
  local haystack="$1" needle="$2"
  [[ "${haystack}" == *"${needle}"* ]] \
    || fail_assertion "missing [${needle}]; value: ${haystack}"
}

assert_not_contains() {
  local haystack="$1" needle="$2"
  [[ "${haystack}" != *"${needle}"* ]] \
    || fail_assertion "unexpected [${needle}]; value: ${haystack}"
}

assert_log_count() {
  local needle="$1" expected="$2" actual
  actual="$(grep -F -c -- "${needle}" <<<"${LOG}" || true)"
  if [[ "${actual}" -ne "${expected}" ]]; then
    fail_assertion "log count [${needle}]=${actual}, expected=${expected}; log: ${LOG}"
  fi
}

run_case() {
  case_name="$1"
  cases=$((cases + 1))
  shift
  local -a env_args=()
  while [[ "$#" -gt 0 && "$1" == *=* && "$1" != --* ]]; do
    env_args+=("$1")
    shift
  done
  : >"${TMP}/log"
  : >"${TMP}/auth"
  set +e
  # `env -u SW_OPS_UI_TOKEN` 让每个用例从"本机没配 token"这个确定起点开始，不受跑测试的人
  # 自己 shell 里的导出影响；用例要带 token 时自己在 env_args 里给一个（-u 先生效，随后的
  # 赋值照样落地）。TEST_AUTH_LOG 与 TEST_LOG 是**两个文件**：TEST_LOG 记 argv，要被断言
  # "token 出现 0 次"；TEST_AUTH_LOG 记 curl 从 --config - 里真正解析出的头，要被断言
  # "token 确实送到了"。混在一起就没法同时证明这两件事。
  # RUN_XTRACE=1 时用 `bash -x` 跑被测脚本，专门考察"凭据会不会被 xtrace 打出来"。
  # xtrace 不会跨进程传递（假件有自己的 shebang，远端 bash 也没有 -x），所以这只影响本机那一层
  # ——正好是 token 经手的那一层。
  local -a bash_opts=()
  [[ "${RUN_XTRACE:-0}" -eq 0 ]] || bash_opts=(-x)
  RESULT="$(env -u SW_OPS_UI_TOKEN ${env_args[@]+"${env_args[@]}"} \
    PATH="${TMP}/bin:${PATH}" \
    HOME="${TMP}/home dir" \
    TEST_LOG="${TMP}/log" \
    TEST_AUTH_LOG="${TMP}/auth" \
    /bin/bash ${bash_opts[@]+"${bash_opts[@]}"} "${SCRIPT_UNDER_TEST}" "$@" 2>&1)"
  STATUS=$?
  set -e
  LOG="$(<"${TMP}/log")"
  AUTH_LOG="$(<"${TMP}/auth")"
}

# R1 闸门失败必须走"报错 + 不重试"，而不是再打断一次生产 core。
assert_gate_failed() {
  assert_status 1
  assert_contains "${RESULT}" "R1 红线闸门未通过"
  assert_contains "${RESULT}" "R1 确认闸门未通过：core 已重启，但不应就此收工"
  assert_contains "${RESULT}" "后果不是内容会越权发出去"
  assert_contains "${RESULT}" "SW_CONFIRM_TTL_HOURS（默认 24 小时）到点被自动驳回"
  assert_contains "${RESULT}" "兜底：工作台的「确认发布」按钮不受 Telegram 影响"
  assert_contains "${RESULT}" "这里没有「回滚」可言"
  # polling 那一格不是 .env 决定的（core/main.py:104 lifespan 起线程、
  # core/telegram.py:981 按 poller.alive 判活），文案必须把这条出路指出来。
  assert_contains "${RESULT}" "但请注意 polling 那一格不是 .env 决定的"
  assert_contains "${RESULT}" "core/main.py:104 的 lifespan 起、core/telegram.py:981 判活"
  assert_contains "${RESULT}" "要走 update.sh 回滚，不是本脚本"
  assert_not_contains "${RESULT}" "✓ core 已重启"
  # 闸门失败重试没有意义：再重启一次也修不好 .env，只会白白多打断一次生产 core。
  assert_log_count "ssh <" 1
  assert_log_count "docker <compose> <restart> <core>" 1
  assert_not_contains "${RESULT}" "IAP 连接中断"
}

INFO_REAL_PUBLISH='{"ok":true,"data":{"version":"0.1.0","env":"prod","time":"2026-08-22T02:00:00Z","timezone":"Asia/Shanghai","scheduler_enabled":true,"generate_enabled":true,"use_fake_publishers":false,"auth_required":false,"publishers":["xhs","douyin"]}}'
TG_LIVE='{"ok":true,"data":{"enabled":true,"configured":true,"ready":true,"chat_configured":true,"can_sign":true,"polling":true,"username":"sw_ops_bot","sent":3,"failed":0,"stats":{},"detail":"","last_error":""}}'
TG_NOT_READY='{"ok":true,"data":{"enabled":true,"configured":true,"ready":false,"chat_configured":false,"can_sign":true,"polling":false,"username":"sw_ops_bot","sent":0,"failed":0,"stats":{},"detail":"未配置 chat_id","last_error":""}}'
TG_NOT_POLLING='{"ok":true,"data":{"enabled":true,"configured":true,"ready":true,"chat_configured":true,"can_sign":true,"polling":false,"username":"sw_ops_bot","sent":0,"failed":0,"stats":{},"detail":"","last_error":""}}'
# 总开关关着、ready 与 polling 都真：靠 polling 间接兜必然漏判，所以必须直判 enabled。
TG_DISABLED='{"ok":true,"data":{"enabled":false,"configured":true,"ready":true,"chat_configured":true,"can_sign":true,"polling":true,"username":"sw_ops_bot","sent":0,"failed":0,"stats":{},"detail":"","last_error":""}}'

# ---------------------------------------------------------------- 既有行为不回归
# 收尾行必须**按分支**说话。模拟发布器挂着时闸门根本没探测通道，此时宣称"确认闸门通道
# 已核验"是假话——而 use_fake_publishers=true 正是当前生产状态，等于每次重启都说一次。
# 下面两条用例分别钉死两句收尾，任何一句被改成"两条路径都能过"的笼统话都会红。
run_case "restart backs up first and restarts core"
assert_status 0
assert_contains "${LOG}" "backup <"
assert_contains "${LOG}" "docker <compose> <restart> <core>"
assert_contains "${LOG}" "curl <-q> <-fsS> <--max-time> <5> <-w> <\n%{http_code}> <--config> <-> <http://127.0.0.1:8000/api/v1/system/info>"
assert_contains "${RESULT}" "探针  GET /api/v1/system/info 200（第 1 次）"
assert_contains "${RESULT}" "✓ core 已重启、探针恢复 200，R1 闸门已记录（模拟发布器=true，未探测通道）"
assert_not_contains "${RESULT}" "已核验"

# 模拟发布器挂着（当前生产状态）：通道死了也不阻断，且**不多打一个请求**。
run_case "fake publishers tolerate a dead confirm channel" TEST_TELEGRAM_JSON="${TG_NOT_READY}"
assert_status 0
assert_contains "${RESULT}" "R1 闸门  模拟发布器=true：本次重启后什么都不会真发"
assert_contains "${RESULT}" "✓ core 已重启、探针恢复 200，R1 闸门已记录（模拟发布器=true，未探测通道）"
# 未探测就不许说"已核验"——这条断言是必修 1 的正面钉子。
assert_not_contains "${RESULT}" "已核验"
assert_log_count "curl <" 1
assert_not_contains "${LOG}" "/api/v1/system/telegram"

# 探针一直不回 200：15 次重试后失败，压根走不到闸门。
run_case "probe never returns fails before the gate" TEST_CURL_STATUS=7
assert_status 1
assert_contains "${RESULT}" "core 在 30 秒内未恢复 /api/v1/system/info 200"
assert_not_contains "${RESULT}" "R1 闸门"
assert_not_contains "${LOG}" "docker <compose> <exec>"
assert_not_contains "${RESULT}" "✓ core 已重启"

# 传输失败仍然重试一次（既有行为）。
run_case "transport failure still retries once" TEST_SSH_STATUSES=255,0
assert_status 0
assert_log_count "ssh <" 2
assert_contains "${RESULT}" "IAP 连接中断"
assert_contains "${RESULT}" "✓ core 已重启、探针恢复 200，R1 闸门已记录（模拟发布器=true，未探测通道）"

# 远端脚本**自身**退出 255 必须被包装层规范化成 254，否则会与 ssh 的传输失败混淆——
# 那会让"远端因故 255"被当成 IAP 断链而**再重启一次生产 core**。update.sh / verify.sh
# 都有这层，restart.sh 现在有了退出码协议，更不能少。
run_case "remote 255 is normalized to 254" TEST_REMOTE_EXIT=255
assert_status 1
assert_contains "${LOG}" "remote-forced-exit <255>"
assert_contains "${LOG}" "ssh-remote-status <254>"
assert_not_contains "${LOG}" "ssh-remote-status <255>"
assert_contains "${RESULT}" "core 重启或探针确认失败"
assert_not_contains "${RESULT}" "✓ core 已重启"

# 远端协议码不会被规范化掉：20/40 必须原样穿过包装层。
run_case "remote gate protocol codes pass through the wrapper" TEST_REMOTE_EXIT=20
assert_status 0
assert_contains "${LOG}" "ssh-remote-status <20>"
assert_contains "${RESULT}" "✓ core 已重启、探针恢复 200，R1 闸门已记录（模拟发布器=true，未探测通道）"
assert_log_count "ssh <" 1

# ------------------------------------------------------------------- R1 红线闸门
# use_fake_publishers 与 SW_TELEGRAM_* 都在服务器 .env 里；把假发布器关掉只需要改 .env
# 再重启，这条路不经过 update.sh --apply，所以同一道互锁必须在 restart.sh 里也有一份。
run_case "gate passes real publishing with a live confirm channel" \
  TEST_INFO_JSON="${INFO_REAL_PUBLISH}" TEST_TELEGRAM_JSON="${TG_LIVE}"
assert_status 0
assert_contains "${LOG}" "curl <-q> <-fsS> <--max-time> <5> <-w> <\n%{http_code}> <--config> <-> <http://127.0.0.1:8000/api/v1/system/telegram>"
assert_contains "${RESULT}" "R1 闸门  真发布已开启（模拟发布器=false）：人工确认闸门通道 enabled=true ready=true polling=true"
# 从严路径是**真的探测过**通道，这里才配说"已核验"——与上面 fake 路径那句互为对照。
assert_contains "${RESULT}" "✓ core 已重启、探针恢复 200，R1 确认闸门通道已核验"
assert_not_contains "${RESULT}" "R1 闸门已记录"
# stdin 哨兵：两条内联解析都由管道显式喂 stdin，谁少一层就会把放行行连同收尾整段吞掉。
assert_log_count "docker <compose> <exec> <-T> <core> <python3> <-c>" 2

run_case "gate aborts when the channel is not ready" \
  TEST_INFO_JSON="${INFO_REAL_PUBLISH}" TEST_TELEGRAM_JSON="${TG_NOT_READY}"
assert_gate_failed
assert_contains "${RESULT}" "真发布已开启（模拟发布器=false）"
assert_contains "${RESULT}" "ready=false（确认卡根本推不出去）"
assert_contains "${RESULT}" "把 .env 里的 SW_USE_FAKE_PUBLISHERS 设回 true"

run_case "gate aborts when polling is dead" \
  TEST_INFO_JSON="${INFO_REAL_PUBLISH}" TEST_TELEGRAM_JSON="${TG_NOT_POLLING}"
assert_gate_failed
assert_contains "${RESULT}" "ready=true 但 polling=false"

run_case "gate aborts when the master switch is off" \
  TEST_INFO_JSON="${INFO_REAL_PUBLISH}" TEST_TELEGRAM_JSON="${TG_DISABLED}"
assert_gate_failed
assert_contains "${RESULT}" "总开关 enabled=false"

run_case "gate aborts when the telegram probe fails" \
  TEST_INFO_JSON="${INFO_REAL_PUBLISH}" TEST_TELEGRAM_CURL_STATUS=7
assert_gate_failed
assert_contains "${RESULT}" "无法获取 /api/v1/system/telegram"

run_case "gate is strict when the info body is not json" \
  TEST_INFO_JSON='<html>502</html>' TEST_TELEGRAM_JSON="${TG_NOT_READY}"
assert_gate_failed
assert_contains "${RESULT}" "模拟发布器状态取不到（use_fake_publishers=<未知>），按真发布从严裁定"
# 取不到时不许再建议"设回 true"——它可能已经成立、因而不可执行。
assert_contains "${RESULT}" "先别改 SW_USE_FAKE_PUBLISHERS（它可能已经是 true）"
assert_not_contains "${RESULT}" "把 .env 里的 SW_USE_FAKE_PUBLISHERS 设回 true"

# 闸门自己那条 docker exec 打嗝（独立于重启与探针）：升级从严，但通道活着仍然放行。
run_case "gate exec hiccup escalates to strict but still passes a live channel" \
  TEST_FAKE_EXEC_STATUS=125 TEST_TELEGRAM_JSON="${TG_LIVE}"
assert_status 0
assert_contains "${RESULT}" "R1 闸门  模拟发布器状态取不到（use_fake_publishers=<未知>），按真发布从严裁定：人工确认闸门通道 enabled=true ready=true polling=true"
assert_contains "${RESULT}" "✓ core 已重启、探针恢复 200，R1 确认闸门通道已核验"

run_case "gate exec hiccup with a dead channel aborts with actionable advice" \
  TEST_FAKE_EXEC_STATUS=125 TEST_TELEGRAM_JSON="${TG_NOT_POLLING}"
assert_gate_failed
assert_contains "${RESULT}" "先别改 SW_USE_FAKE_PUBLISHERS（它可能已经是 true）"
assert_contains "${RESULT}" "docker compose logs core"

# 确认通道那条内联解析自己出错（与上面那条是不同的开关）：退出码落进兜底分支，
# 依然阻断。这条用例的存在也是 TEST_TELEGRAM_EXEC_STATUS 不是备而不用的脚手架的证明。
run_case "telegram parse exec hiccup still aborts" \
  TEST_INFO_JSON="${INFO_REAL_PUBLISH}" TEST_TELEGRAM_EXEC_STATUS=125
assert_gate_failed
assert_contains "${RESULT}" "真发布已开启（模拟发布器=false）"
assert_contains "${RESULT}" "/api/v1/system/telegram 解析异常（退出码 125）"

# ------------------------------------------- 工作台 API token（docs/RISKS.md 第 8 条 §8.4）
# 生产 .env 一旦配上非空 SW_UI_TOKEN，/api/v1/* 全部要求 Authorization: Bearer。
# restart.sh 的探针与 R1 闸门都打 /api/v1/*，所以它同样必须能带上这个头，而且 token
# 一个字符都不能进 argv（生产是合租机器，/proc/*/cmdline 世界可读）。
UI_TOKEN='TESTTOKEN_restart-A1b2+/=.:@'

assert_token_absent_from_argv() {
  local token="$1" hits
  hits="$(grep -F -c -- "${token}" <<<"${LOG}" || true)"
  if [[ "${hits}" -ne 0 ]]; then
    fail_assertion "token 明文在 argv 记录里出现了 ${hits} 次，必须是 0；log: ${LOG}"
  fi
}

run_case "ui token reaches both probe headers and never appears in argv" \
  SW_OPS_UI_TOKEN="${UI_TOKEN}" TEST_INFO_JSON="${INFO_REAL_PUBLISH}" TEST_TELEGRAM_JSON="${TG_LIVE}"
assert_status 0
assert_contains "${AUTH_LOG}" "url <http://127.0.0.1:8000/api/v1/system/info> header <Authorization: Bearer ${UI_TOKEN}>"
assert_contains "${AUTH_LOG}" "url <http://127.0.0.1:8000/api/v1/system/telegram> header <Authorization: Bearer ${UI_TOKEN}>"
assert_token_absent_from_argv "${UI_TOKEN}"
assert_not_contains "${RESULT}" "${UI_TOKEN}"
assert_contains "${RESULT}" "已加载工作台 API token（来源：环境变量 SW_OPS_UI_TOKEN）"
assert_contains "${RESULT}" "✓ core 已重启、探针恢复 200，R1 确认闸门通道已核验"

run_case "without a ui token the config stream carries no header"
assert_status 0
assert_contains "${AUTH_LOG}" "url <http://127.0.0.1:8000/api/v1/system/info> header <>"
assert_not_contains "${AUTH_LOG}" "Authorization"
assert_not_contains "${RESULT}" "已加载工作台 API token"
assert_contains "${RESULT}" "✓ core 已重启、探针恢复 200，R1 闸门已记录（模拟发布器=true，未探测通道）"

# 字符集校验在**本机**、在备份与 SSH 之前完成，报错里绝不回显 token 本身。
run_case "a token containing a double quote is rejected before backup and ssh" \
  SW_OPS_UI_TOKEN='TESTTOKEN_bad"quote'
assert_status 1
assert_contains "${RESULT}" "工作台 API token 含有不被允许的字符"
# 报错必须按**白名单**口径说话：列一份"不允许"清单会让 token 里带 % 的人挨个排除后得出
# "我这个应该合法"，再卡在一条看不懂的报错上。
assert_contains "${RESULT}" "这是**白名单**：只允许 A-Z a-z 0-9 以及 . _ - + / = : @；**其余字符一律拒绝**"
# 不许再出现"空白会截断配置行"这句错话：实测空格在 curl 双引号参数里能原样通过，
# 排除空白的真实理由是它不是合法的凭据字符（RFC 6750 的 b64token）。
assert_not_contains "${RESULT}" "空白会截断配置行"
assert_contains "${RESULT}" "空白与控制字符不是合法的凭据字符"
assert_not_contains "${RESULT}" 'TESTTOKEN_bad"quote'
assert_not_contains "${LOG}" "backup <"
assert_not_contains "${LOG}" "ssh <"
assert_not_contains "${LOG}" "docker <"

# 探针拿到 401：core 已经重启完成并在应答，重试第二次纯属多打断一次生产，所以不重试。
run_case "a 401 probe stops without a second restart and explains itself" TEST_INFO_HTTP_CODE=401
assert_status 1
assert_contains "${RESULT}" "探针拿到 401：core 已启用 SW_UI_TOKEN 鉴权，而本机没带上匹配的 token。"
assert_contains "${RESULT}" "注意这不是重启失败"
assert_contains "${RESULT}" "401 恰恰证明 core 已经重启完成并在正常应答"
assert_contains "${RESULT}" "这不是重启失败——401 证明 core 已经重启完成并在正常应答；缺的是运维侧凭据。"
assert_contains "${RESULT}" "export SW_OPS_UI_TOKEN=<生产 .env 里 SW_UI_TOKEN 的同一个值>"
assert_not_contains "${RESULT}" "✓ core 已重启"
assert_not_contains "${RESULT}" "core 在 30 秒内未恢复"
assert_log_count "ssh <" 1
assert_log_count "docker <compose> <restart> <core>" 1
assert_log_count "curl <" 1
assert_not_contains "${RESULT}" "IAP 连接中断"

# 401 与"连不上"必须分得开：连不上仍然重试满 15 次探针 + 一次整体重试，文案照旧。
run_case "a transport failure keeps the old probe-failure path" TEST_CURL_STATUS=7
assert_status 1
assert_contains "${RESULT}" "core 在 30 秒内未恢复 /api/v1/system/info 200"
assert_not_contains "${RESULT}" "探针拿到 401"

# 生产真的开了 token 而本机没配：端到端跑一遍。
run_case "auth-enabled core without a local token stops with the auth message" \
  TEST_REQUIRE_TOKEN="${UI_TOKEN}"
assert_status 1
assert_contains "${RESULT}" "探针拿到 401：core 已启用 SW_UI_TOKEN 鉴权"
assert_not_contains "${RESULT}" "✓ core 已重启"

# 同一台 core，本机配对了 token：重启与 R1 闸门照常，token 依然不进 argv。
run_case "auth-enabled core with the matching token restarts and clears the gate" \
  TEST_REQUIRE_TOKEN="${UI_TOKEN}" SW_OPS_UI_TOKEN="${UI_TOKEN}" \
  TEST_INFO_JSON="${INFO_REAL_PUBLISH}" TEST_TELEGRAM_JSON="${TG_LIVE}"
assert_status 0
assert_contains "${RESULT}" "探针  GET /api/v1/system/info 200（第 1 次）"
assert_contains "${RESULT}" "R1 闸门  真发布已开启（模拟发布器=false）：人工确认闸门通道 enabled=true ready=true polling=true"
assert_contains "${RESULT}" "✓ core 已重启、探针恢复 200，R1 确认闸门通道已核验"
assert_token_absent_from_argv "${UI_TOKEN}"

# 确认通道探针的 401 落进 R1 闸门裁定文案。
run_case "a telegram 401 lands in the R1 gate verdict" \
  TEST_INFO_JSON="${INFO_REAL_PUBLISH}" TEST_TELEGRAM_HTTP_CODE=401
assert_gate_failed
assert_contains "${RESULT}" "/api/v1/system/telegram 返回 401（core 已启用 SW_UI_TOKEN，本机未提供或不匹配）"

# --------------------------------------------- bash -x 下 token 必须零出现（红线 R5）
# 现实场景：401 老是修不好 → 值班的人做最自然的一步 `bash -x scripts/ops/XXX.sh 2>&1 |
# tee /tmp/x.log` → 把这段贴进工单或对话求助。xtrace 会把每条命令**展开后的参数**打到
# stderr，没有防护时 token 明文会在赋值、[[ -n ]]、正则匹配、printf 这几行里出现五次。
# scripts/ops/ui_token.sh 的 sw_ops_xtrace_guard 在经手 token 的三个函数入口关掉 xtrace、
# 出口恢复（用 `case "$-"` 而不是 bash 4.4+ 才有的 `local -`，因为工作站是 bash 3.2）。
# 这条用例断言的是**后果**：整份 -x 输出里 token 明文 0 次。
# 注意假件只记录 argv，模拟不了 xtrace——所以这条缺口只能靠这个用例守。
XTRACE_TOKEN='TESTTOKEN_xtrace-must-not-leak'
RUN_XTRACE=1
run_case "bash -x never prints the token" \
  SW_OPS_UI_TOKEN="${XTRACE_TOKEN}" TEST_INFO_JSON="${INFO_REAL_PUBLISH}" TEST_TELEGRAM_JSON="${TG_LIVE}"
assert_status 0
assert_token_absent_from_argv "${XTRACE_TOKEN}"
assert_not_contains "${RESULT}" "${XTRACE_TOKEN}"
assert_contains "${RESULT}" "+ sw_ops_load_ui_token"
assert_contains "${AUTH_LOG}" "header <Authorization: Bearer ${XTRACE_TOKEN}>"

run_case "bash -x never prints a rejected token either" SW_OPS_UI_TOKEN='TESTTOKEN_xtrace"bad'
assert_status 1
assert_not_contains "${RESULT}" 'TESTTOKEN_xtrace"bad'
assert_contains "${RESULT}" "工作台 API token 含有不被允许的字符"
RUN_XTRACE=0

# ------------------------------------ stdin 的结构性保证（把脆弱不变量换成结构，任务 B）
# 远端正文外面那对花括号 + 尾部的 `} </dev/null` 是现在唯一承重的那一层（理由见
# scripts/ops/restart.sh 里那段说明）。这里只放**正例**：把逐条命令上的 `</dev/null` 标注
# 全部删掉，行为必须逐字不变，证明那些标注已经降级成纵深防御。
# 反例（把结构也删掉、让历史缺陷重现）放在 tests/ops/test_update.sh 与 test_verify.sh 里，
# 不在这里重复：restart.sh 的边界命令里没有一条会在本套假件下真的消费 stdin
# （`docker compose restart` 的假件不读 stdin，两条内联解析都由管道喂），
# 所以在这个文件里构造不出"被吞掉"的后果——如实写明，不假装覆盖。
STRUCT_DIR="${TMP}/struct"
mkdir -p "${STRUCT_DIR}"
cp "${ROOT}/scripts/ops/ui_token.sh" "${STRUCT_DIR}/ui_token.sh"
sed -e '/^} <\/dev\/null$/!s| </dev/null||g' "${SCRIPT}" >"${STRUCT_DIR}/annotations_stripped.sh"

case_name="structural stdin variant is actually rewritten"
struct_orig_lines="$(grep -c "" "${SCRIPT}")"
struct_stripped_lines="$(grep -c "" "${STRUCT_DIR}/annotations_stripped.sh")"
struct_annotations_left="$(grep -c -- ' </dev/null' "${STRUCT_DIR}/annotations_stripped.sh" || true)"
if [[ "${struct_orig_lines}" -ne "${struct_stripped_lines}" ]]; then
  fail_assertion "annotations_stripped 不应改变行数：${struct_orig_lines} -> ${struct_stripped_lines}"
fi
if [[ "${struct_annotations_left}" -ne 2 ]]; then
  fail_assertion "annotations_stripped 里应只剩 2 处 </dev/null（两层组重定向），实际 ${struct_annotations_left} 处"
fi

SCRIPT_UNDER_TEST="${STRUCT_DIR}/annotations_stripped.sh"
run_case "stripping every per-command </dev/null changes nothing" \
  TEST_INFO_JSON="${INFO_REAL_PUBLISH}" TEST_TELEGRAM_JSON="${TG_LIVE}"
assert_status 0
assert_contains "${LOG}" "docker <compose> <restart> <core>"
assert_contains "${RESULT}" "探针  GET /api/v1/system/info 200（第 1 次）"
assert_contains "${RESULT}" "R1 闸门  真发布已开启（模拟发布器=false）：人工确认闸门通道 enabled=true ready=true polling=true"
assert_contains "${RESULT}" "✓ core 已重启、探针恢复 200，R1 确认闸门通道已核验"
SCRIPT_UNDER_TEST="${SCRIPT}"

if [[ "${failures}" -ne 0 ]]; then
  printf 'restart.sh mechanical tests failed: %s assertion(s)\n' "${failures}" >&2
  exit 1
fi
printf 'restart.sh mechanical tests passed: %s case(s)\n' "${cases}"
