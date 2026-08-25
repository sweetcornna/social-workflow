#!/bin/bash
# No network, SSH, or Docker: every boundary command is a local argv-recording fake.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="${ROOT}/scripts/ops/status.sh"
# 被测脚本路径。默认是仓库里那一份；「stdin 结构性保证」一节会临时指向改写过的副本。
SCRIPT_UNDER_TEST="${SCRIPT}"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT
mkdir -p "${TMP}/bin" "${TMP}/home dir"
ln -s "${ROOT}" "${TMP}/home dir/social_workflow"

cat >"${TMP}/bin/bash" <<'EOF'
#!/bin/bash
depth="${SW_FAKE_BASH_DEPTH:-0}"
if [[ "${1:-}" == "-s" ]]; then
  # status.sh 只有一层远端 bash（不像 update/verify/restart 有状态规范化包装）。
  # depth=0 就是 ssh 送达远端、被登录 shell 重新分词之后真正收到的那一份 argv。
  sw_argv=("$@")
  sw_start=1
  [[ "${sw_argv[1]:-}" == "--" ]] && sw_start=2
  sw_positional=("${sw_argv[@]:sw_start}")
  {
    printf 'remote-args depth=%s argc=%s' "${depth}" "${#sw_positional[@]}"
    printf ' [%s]' ${sw_positional[@]+"${sw_positional[@]}"}
    printf '\n'
  } >>"${TEST_LOG}"
  if [[ -n "${TEST_REMOTE_EXIT:-}" ]]; then
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
[[ "${TEST_SSH_STATUS:-0}" -eq 0 ]] || exit "${TEST_SSH_STATUS}"

# 忠实模拟真实 ssh(1)：ssh 不保留 argv 边界。host 之后的全部参数被用单个空格拼成一个
# 字符串发给远端，由远端登录 shell 重新分词；stdin 原样继承。
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
  # 探针那段的内联解析。真实 `docker compose exec -T` 转发 stdin，而被模拟的容器内进程
  # 自己就 json.load(sys.stdin)，所以直接 exec 出去即是忠实语义：stdin 被读干净。
  # 调用方必须自带显式 stdin 来源（`printf … |` 管道），否则它会吃掉调用它的远端脚本正文。
  exec python3 -c "$7"
fi

if [[ "$1 ${2:-} ${3:-} ${4:-} ${5:-} ${6:-}" == "compose exec -T core python3 -" ]]; then
  # 数据卷清单那段：`docker compose exec -T core python3 - <<'PY'`。真实 docker 把 heredoc
  # 转发进容器、容器内 python 从 stdin 读脚本并读到 EOF，所以这里必须把 stdin 读干净。
  # 不去真的跑那段 python：它读的是容器内的 /app/data，本机没有；跑它只会引入一个与被测
  # 行为无关的失败。忠实的地方在于"stdin 被消费"这一条，那才是能吞掉脚本正文的性质。
  cat >/dev/null
  [[ "${TEST_VOLUME_STATUS:-0}" -ne 0 ]] && exit "${TEST_VOLUME_STATUS}"
  printf '  accounts.yaml  128 bytes\n'
  printf '  sw.db  4096 bytes\n'
  exit 0
fi

case "${2:-}" in
  build|up|restart|down|stop|start|kill|rm|exec)
    # 只读查看工具永远不该改动部署，也不该有别的 exec 形态。
    exit 95
    ;;
esac

if [[ "$1 ${2:-}" == "compose ps" ]]; then
  [[ "${TEST_PS_FAIL:-0}" -eq 1 ]] && exit 1
  printf 'NAME      IMAGE                  SERVICE   STATUS\n'
  printf 'sw-core   social_workflow-core   core      running\n'
  exit 0
fi

exit 93
EOF

cat >"${TMP}/bin/df" <<'EOF'
#!/bin/bash
{
  printf 'df'
  printf ' <%s>' "$@"
  printf '\n'
} >>"${TEST_LOG}"
[[ "${TEST_DF_STATUS:-0}" -ne 0 ]] && exit "${TEST_DF_STATUS}"
printf 'Filesystem      Size  Used Avail Use%% Mounted on\n'
printf '/dev/sda1        49G   21G   26G  45%% /\n'
exit 0
EOF

cat >"${TMP}/bin/sleep" <<'EOF'
#!/bin/bash
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

# 假件把每一次调用的完整 argv 都落进 TEST_LOG（ssh、bash、docker、curl、df 全覆盖，还包括
# ssh 真正发出的那一整条远端命令字符串）。token 明文在这份记录里必须是 0 次。
assert_token_absent_from_argv() {
  local token="$1" hits
  hits="$(grep -F -c -- "${token}" <<<"${LOG}" || true)"
  if [[ "${hits}" -ne 0 ]]; then
    fail_assertion "token 明文在 argv 记录里出现了 ${hits} 次，必须是 0；log: ${LOG}"
  fi
}

# 只读查看工具永远不该改动部署。
assert_read_only() {
  assert_not_contains "${LOG}" "docker <compose> <build>"
  assert_not_contains "${LOG}" "docker <compose> <up>"
  assert_not_contains "${LOG}" "docker <compose> <restart>"
  assert_not_contains "${LOG}" "docker <compose> <down>"
  assert_not_contains "${LOG}" "backup <"
  assert_not_contains "${LOG}" "git <"
}

# 与鉴权无关的三段（Compose 服务 / 磁盘水位 / 数据卷文件）都完整打印过。
# 401 降级路径要的就是"这三段一个都不能少"，所以单独抽出来。
assert_three_unauthed_sections() {
  assert_contains "${RESULT}" "Compose 服务"
  assert_contains "${RESULT}" "sw-core   social_workflow-core   core      running"
  assert_contains "${RESULT}" "磁盘水位"
  assert_contains "${RESULT}" "/dev/sda1        49G   21G   26G  45% /"
  assert_contains "${RESULT}" "数据卷文件"
  assert_contains "${RESULT}" "accounts.yaml  128 bytes"
}

# 正常路径：四段齐全 + 哨兵说"四段"。哨兵排在所有读 stdin 的边界命令之后，一旦消失就说明
# 某条命令又把远端脚本正文吞了。
assert_all_four_sections() {
  assert_three_unauthed_sections
  assert_contains "${RESULT}" "Core 探针"
  assert_contains "${RESULT}" "四段读取完毕"
  assert_not_contains "${RESULT}" "三段读取完毕"
}

# 401 降级路径：三段齐全，哨兵必须说"三段"。哨兵仍然要打（它的技术目的是证明正文没被吞），
# 但**措辞必须与实际发生的事一致**——那一段一个字节都没产出，说"四段读取完毕"就是假话。
assert_three_sections_after_401() {
  assert_three_unauthed_sections
  assert_contains "${RESULT}" "三段读取完毕（Core 探针段因 401 未取到，见上）"
  assert_not_contains "${RESULT}" "四段读取完毕"
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

# ---------------------------------------------------------------- 既有行为不回归
# 未配置 token 时，输出与退出码必须与改造前逐字一致。
run_case "status prints all four sections and exits zero"
assert_status 0
assert_read_only
assert_log_count "ssh <" 1
assert_contains "${LOG}" "docker <compose> <ps>"
assert_contains "${LOG}" "curl <-q> <-fsS> <--max-time> <10> <-w> <\n%{http_code}> <--config> <-> <http://127.0.0.1:8000/api/v1/system/info>"
assert_contains "${LOG}" "docker <compose> <exec> <-T> <core> <python3> <-c>"
assert_contains "${LOG}" "df <-h> <${TMP}/home dir/social_workflow>"
assert_contains "${LOG}" "docker <compose> <exec> <-T> <core> <python3> <->"
assert_all_four_sections
assert_contains "${RESULT}" "版本  0.1.0"
assert_contains "${RESULT}" "环境  prod"
assert_contains "${RESULT}" "模拟发布器  True"
assert_contains "${RESULT}" "鉴权  False"
assert_contains "${RESULT}" "已注册发布器  xhs, douyin"
assert_contains "${RESULT}" "✓ 状态读取完成"
assert_not_contains "${RESULT}" "已加载工作台 API token"

# ssh 的 argv 边界：远端只收到一个位置参数（401 协议码），空参数塌陷问题不存在但仍钉死。
run_case "the protocol code reaches the remote intact"
assert_status 0
assert_contains "${LOG}" "ssh-command <bash -s -- 41 >"
assert_contains "${LOG}" "remote-args depth=0 argc=1 [41]"

# 传输失败：退出码原样传出，不加解释也不改码（改造前语义）。
run_case "an ssh transport failure passes its status through" TEST_SSH_STATUS=255
assert_status 255
assert_not_contains "${RESULT}" "✓ 状态读取完成"
assert_not_contains "${RESULT}" "401"

# compose ps 失败：set -e 在第一段就中止，后面三段不打印（改造前语义）。
run_case "a compose ps failure aborts at the first section" TEST_PS_FAIL=1
assert_status 1
assert_not_contains "${RESULT}" "Core 探针"
assert_not_contains "${RESULT}" "四段读取完毕"
assert_not_contains "${RESULT}" "✓ 状态读取完成"

# ------------------------------------------- 工作台 API token（docs/RISKS.md 第 8 条 §8.4）
UI_TOKEN='TESTTOKEN_status-A1b2+/=.:@'

run_case "ui token reaches the probe header and never appears in argv" SW_OPS_UI_TOKEN="${UI_TOKEN}"
assert_status 0
# ① 确实送到了。
assert_contains "${AUTH_LOG}" "url <http://127.0.0.1:8000/api/v1/system/info> header <Authorization: Bearer ${UI_TOKEN}>"
# ② 确实没泄漏：argv 记录里 0 次，脚本输出里 0 次。
assert_token_absent_from_argv "${UI_TOKEN}"
assert_not_contains "${RESULT}" "${UI_TOKEN}"
# ③ 只报来源、不报值。
assert_contains "${RESULT}" "已加载工作台 API token（来源：环境变量 SW_OPS_UI_TOKEN）"
assert_all_four_sections
assert_contains "${RESULT}" "✓ 状态读取完成"

run_case "without a ui token the config stream carries no header"
assert_status 0
assert_contains "${AUTH_LOG}" "url <http://127.0.0.1:8000/api/v1/system/info> header <>"
assert_not_contains "${AUTH_LOG}" "Authorization"
assert_not_contains "${RESULT}" "已加载工作台 API token"

run_case "an exported but empty token means explicitly no token" SW_OPS_UI_TOKEN=
assert_status 0
assert_not_contains "${AUTH_LOG}" "Authorization"
assert_not_contains "${RESULT}" "已加载工作台 API token"

# 凭据文件兜底（R5 既定存放约定）。
CRED_DIR="${TMP}/home dir/.dsh-sw"
CRED_FILE="${CRED_DIR}/.credentials.yaml"
mkdir -p "${CRED_DIR}"
CRED_TOKEN='TESTTOKEN_status-from-credentials-file'
printf 'dsh_api_key: something-else\nsw_ui_token: "%s"\n' "${CRED_TOKEN}" >"${CRED_FILE}"
chmod 600 "${CRED_FILE}"

run_case "credentials file supplies the token when the env var is unset"
assert_status 0
assert_contains "${AUTH_LOG}" "header <Authorization: Bearer ${CRED_TOKEN}>"
assert_token_absent_from_argv "${CRED_TOKEN}"
assert_contains "${RESULT}" "已加载工作台 API token（来源：${CRED_FILE} 的 sw_ui_token 键）"
rm -f "${CRED_FILE}"

# 字符集校验在**本机**、在 SSH 之前完成，报错里绝不回显 token 本身。
run_case "a token containing a double quote is rejected before ssh" SW_OPS_UI_TOKEN='TESTTOKEN_bad"quote'
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
assert_not_contains "${LOG}" "ssh <"
assert_not_contains "${LOG}" "docker <"
assert_not_contains "${LOG}" "curl <"

run_case "a token containing a backslash is rejected before ssh" SW_OPS_UI_TOKEN='TESTTOKEN_bad\back'
assert_status 1
assert_contains "${RESULT}" "工作台 API token 含有不被允许的字符"
assert_not_contains "${LOG}" "ssh <"

# ------------------------------------------------- 401：只降级这一段，其余三段照常打印
# 这条语义与 verify/update/restart 刻意不同，理由写在 scripts/ops/status.sh 的注释里：
# status.sh 是纯只读查看工具，磁盘水位 / compose ps / 数据卷清单与鉴权无关，不该被一条
# 401 连坐——值班时"磁盘满了把 core 撑挂"恰恰是最需要看到那三段的时候。
run_case "a 401 degrades only the probe section and keeps the other three" TEST_INFO_HTTP_CODE=401
assert_status 1
# 探针段降级为明确告警，不是静默跳过。
assert_contains "${RESULT}" "<GET /api/v1/system/info 返回 401 未授权：本段跳过，其余段落照常>"
assert_contains "${RESULT}" "core 已启用 SW_UI_TOKEN 鉴权"
assert_contains "${RESULT}" "这不是部署故障，也不是 core 运行时故障——core 正常应答了 401"
assert_contains "${RESULT}" "export SW_OPS_UI_TOKEN=<生产 .env 里 SW_UI_TOKEN 的同一个值>"
assert_contains "${RESULT}" "已经配了还是 401 = 值不匹配"
# 其余三段一个都不能少（这正是"降级而不是整体崩掉"的全部意义），而哨兵必须说"三段"。
assert_three_sections_after_401
# 降级路径不会去跑探针那段的容器内解析（没有 body 可解析）。
assert_log_count "docker <compose> <exec> <-T> <core> <python3> <-c>" 0
assert_log_count "docker <compose> <exec> <-T> <core> <python3> <->" 1
# 收尾必须说清根因，且不能宣称"状态读取完成"。
assert_contains "${RESULT}" "Core 探针段未取到：401 未授权"
assert_contains "${RESULT}" "其余三段（Compose 服务 / 磁盘水位 / 数据卷文件）已在上面完整打印"
assert_contains "${RESULT}" "这不是生产故障：core 正常应答了 401，缺的是运维侧凭据"
assert_not_contains "${RESULT}" "✓ 状态读取完成"

# 401 与"连不上"必须分得开：非 401 失败保持改造前的语义——在探针段原地中止，后两段不打印。
run_case "a transport failure keeps the old abort-in-place semantics" TEST_CURL_STATUS=7
assert_status 1
assert_contains "${RESULT}" "Core 探针"
assert_not_contains "${RESULT}" "401"
assert_not_contains "${RESULT}" "磁盘水位"
assert_not_contains "${RESULT}" "四段读取完毕"
assert_not_contains "${RESULT}" "三段读取完毕"
assert_not_contains "${RESULT}" "✓ 状态读取完成"

# 401 与其它 HTTP 失败也必须分得开：503 同样走"原地中止"，不给鉴权提示。
run_case "an http 503 keeps the old abort-in-place semantics" TEST_INFO_HTTP_CODE=503
assert_status 1
assert_not_contains "${RESULT}" "core 已启用 SW_UI_TOKEN 鉴权"
assert_not_contains "${RESULT}" "磁盘水位"
assert_not_contains "${RESULT}" "✓ 状态读取完成"

# 生产真的开了 token 而本机没配：端到端跑一遍（假 curl 按 require_token 的真实行为应答）。
run_case "auth-enabled core without a local token degrades the probe section" TEST_REQUIRE_TOKEN="${UI_TOKEN}"
assert_status 1
assert_contains "${RESULT}" "<GET /api/v1/system/info 返回 401 未授权：本段跳过，其余段落照常>"
assert_three_sections_after_401
assert_contains "${RESULT}" "Core 探针段未取到：401 未授权"

# 同一台 core，本机配对了 token：四段全出，token 依然不进 argv。
run_case "auth-enabled core with the matching token prints every section" \
  TEST_REQUIRE_TOKEN="${UI_TOKEN}" SW_OPS_UI_TOKEN="${UI_TOKEN}"
assert_status 0
assert_all_four_sections
assert_contains "${RESULT}" "版本  0.1.0"
assert_contains "${RESULT}" "✓ 状态读取完成"
assert_token_absent_from_argv "${UI_TOKEN}"

# 配了但值不对：仍然 401，仍然只降级这一段。
run_case "auth-enabled core with a mismatched token still degrades only the probe section" \
  TEST_REQUIRE_TOKEN="${UI_TOKEN}" SW_OPS_UI_TOKEN='TESTTOKEN_wrong-value'
assert_status 1
assert_contains "${RESULT}" "已加载工作台 API token（来源：环境变量 SW_OPS_UI_TOKEN）"
assert_contains "${RESULT}" "已经配了还是 401 = 值不匹配"
assert_three_sections_after_401
assert_token_absent_from_argv 'TESTTOKEN_wrong-value'

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
run_case "bash -x never prints the token" SW_OPS_UI_TOKEN="${XTRACE_TOKEN}"
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
# scripts/ops/status.sh 里那段说明）。这里只放**正例**：把逐条命令上的 `</dev/null` 标注
# 全部删掉，行为必须逐字不变，证明那些标注已经降级成纵深防御。
# 反例（把结构也删掉、让历史缺陷重现）放在 tests/ops/test_update.sh 与 test_verify.sh 里，
# 不在这里重复：status.sh 里真正会消费 stdin 的只有两条 `docker compose exec -T`，而它们
# 本来就由管道与 heredoc 显式喂；`docker compose ps` / `df` 不读 stdin，要让假件去读才能
# 造出"被吞掉"，那就不忠实了。如实写明，不假装覆盖。
STRUCT_DIR="${TMP}/struct"
mkdir -p "${STRUCT_DIR}"
cp "${ROOT}/scripts/ops/ui_token.sh" "${STRUCT_DIR}/ui_token.sh"
sed -e '/^} <\/dev\/null$/!s| </dev/null||g' "${SCRIPT}" >"${STRUCT_DIR}/annotations_stripped.sh"

case_name="structural stdin variant is actually rewritten"
cases=$((cases + 1))
struct_orig_lines="$(grep -c "" "${SCRIPT}")"
struct_stripped_lines="$(grep -c "" "${STRUCT_DIR}/annotations_stripped.sh")"
struct_annotations_left="$(grep -c -- ' </dev/null' "${STRUCT_DIR}/annotations_stripped.sh" || true)"
if [[ "${struct_orig_lines}" -ne "${struct_stripped_lines}" ]]; then
  fail_assertion "annotations_stripped 不应改变行数：${struct_orig_lines} -> ${struct_stripped_lines}"
fi
if [[ "${struct_annotations_left}" -ne 1 ]]; then
  fail_assertion "annotations_stripped 里应只剩 1 处 </dev/null（那一层组重定向），实际 ${struct_annotations_left} 处"
fi

SCRIPT_UNDER_TEST="${STRUCT_DIR}/annotations_stripped.sh"
run_case "stripping every per-command </dev/null changes nothing"
assert_status 0
assert_all_four_sections
assert_contains "${RESULT}" "✓ 状态读取完成"
SCRIPT_UNDER_TEST="${SCRIPT}"

if [[ "${failures}" -ne 0 ]]; then
  printf 'status.sh mechanical tests failed: %s assertion(s)\n' "${failures}" >&2
  exit 1
fi
printf 'status.sh mechanical tests passed: %s case(s)\n' "${cases}"
