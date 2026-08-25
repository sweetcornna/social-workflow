#!/bin/bash
# No network, SSH, or Docker: every boundary command is a local argv-recording fake.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="${ROOT}/scripts/ops/verify.sh"
# 被测脚本路径。默认是仓库里那一份；「stdin 结构性保证」一节会临时指向改写过的副本。
SCRIPT_UNDER_TEST="${SCRIPT}"
HEAD_SHA=2222222222222222222222222222222222222222
OTHER=3333333333333333333333333333333333333333
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
# Depth 0 is the wrapper the *re-tokenized* ssh command string produced, so its argv
# is exactly what survived ssh joining every argument with single spaces. Record it
# with delimiters so an empty positional parameter stays visible.
depth="${SW_FAKE_BASH_DEPTH:-0}"
if [[ "${depth}" -eq 0 ]]; then
  {
    printf 'remote-argc <%s>\n' "$#"
    i=1
    for arg in "$@"; do
      printf 'remote-argv[%s]=[%s]\n' "${i}" "${arg}"
      i=$((i + 1))
    done
  } >>"${TEST_LOG}"
fi
# Depth 1 is the inner verify script. Forcing its status here exercises the wrapper's
# 255 normalization without any real SSH.
if [[ "${depth}" -eq 1 && -n "${TEST_REMOTE_EXIT:-}" ]]; then
  cat >/dev/null
  printf 'remote-forced-exit <%s>\n' "${TEST_REMOTE_EXIT}" >>"${TEST_LOG}"
  exit "${TEST_REMOTE_EXIT}"
fi
export SW_FAKE_BASH_DEPTH=$((depth + 1))
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

# Faithful ssh(1) semantics: ssh does NOT preserve argv boundaries. Per ssh(1) --
#   "If supplied, the arguments will be appended to the command, separated by
#    spaces, before it is sent to the server to be executed."
# so every argument after the host is joined with single spaces into ONE string and
# re-tokenized by the remote login shell; an empty argument leaves only a space and
# vanishes. Passing argv straight through to /bin/bash (as an earlier fake did) has
# the opposite semantics and would hide exactly the bug this models.
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    # ssh options that consume a value
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
shift  # target host
[[ "$#" -gt 0 ]] || exit 98
remote_command="$1"
shift
while [[ "$#" -gt 0 ]]; do
  remote_command="${remote_command} $1"
  shift
done
printf 'ssh-command <%s>\n' "${remote_command}" >>"${TEST_LOG}"
# The remote login shell re-tokenizes; stdin (the heredoc) is inherited verbatim.
/bin/bash -c "${remote_command}"
remote_status=$?
printf 'ssh-remote-status <%s>\n' "${remote_status}" >>"${TEST_LOG}"
exit "${remote_status}"
EOF

cat >"${TMP}/bin/git" <<'EOF'
#!/bin/bash
set -uo pipefail
{
  printf 'git'
  printf ' <%s>' "$@"
  printf '\n'
} >>"${TEST_LOG}"

head=2222222222222222222222222222222222222222
case "$1" in
  symbolic-ref)
    [[ "${TEST_DETACHED:-0}" -eq 1 ]] && exit 1
    printf '%s\n' "${TEST_BRANCH:-p14-organic}"
    ;;
  status)
    [[ "${TEST_STATUS_FAIL:-0}" -eq 1 ]] && exit 1
    [[ "${TEST_DIRTY:-0}" -eq 1 ]] && printf ' M core/main.py\n'
    exit 0
    ;;
  branch)
    # `git branch -r --contains <sha>`：真实 git 每行两格缩进，符号 ref 写成
    # `origin/HEAD -> origin/main`。假件必须两样都还原——被测脚本正是靠"第二个字段非空"
    # 认出符号 ref 并跳过它的，假件要是只吐干净的名字，那条分支就永远没有覆盖。
    [[ "${TEST_CONTAIN_FAIL:-0}" -eq 1 ]] && exit 1
    if [[ "${TEST_CONTAIN_REFS+x}" == x ]]; then
      contain_refs="${TEST_CONTAIN_REFS}"
    else
      contain_refs="origin/p14-organic"
    fi
    for contain_ref in ${contain_refs}; do
      # `~` 是本假件用来表示"这一行是符号 ref"的记号：origin/HEAD~origin/main
      case "${contain_ref}" in
        *"~"*) printf '  %s -> %s\n' "${contain_ref%%~*}" "${contain_ref#*~}" ;;
        *) printf '  %s\n' "${contain_ref}" ;;
      esac
    done
    exit 0
    ;;
  rev-parse)
    case "$*" in
      *"refs/remotes/origin/"*)
        [[ "${TEST_NO_ORIGIN:-0}" -eq 1 ]] && exit 1
        # 每条 origin ref 各有自己的顶端。TEST_ORIGIN_TIPS 形如
        # "origin/p14-organic=<sha> origin/main=<sha>"；没列到的落回 TEST_ORIGIN_SHA。
        rev_target="${!#}"
        rev_short="${rev_target#refs/remotes/}"
        for tip_spec in ${TEST_ORIGIN_TIPS:-}; do
          if [[ "${tip_spec%%=*}" == "${rev_short}" ]]; then
            printf '%s\n' "${tip_spec#*=}"
            exit 0
          fi
        done
        printf '%s\n' "${TEST_ORIGIN_SHA:-${head}}"
        ;;
      *"HEAD"*)
        [[ "${TEST_HEAD_FAIL:-0}" -eq 1 ]] && exit 1
        printf '%s\n' "${TEST_HEAD_SHA:-${head}}"
        ;;
      *)
        exit 97
        ;;
    esac
    ;;
  rev-list)
    # 现在只剩一种用法：`--count <HEAD>..<某条 origin ref 的顶端>`，也就是"HEAD 落后它
    # 几个提交"。顶端等于 HEAD 时被测脚本压根不会问，所以这里只有一个可配的答案。
    printf '%s\n' "${TEST_BEHIND:-4}"
    ;;
  fetch|pull|merge|reset|switch|checkout|clean|remote)
    # A read-only forensic tool must never reach any of these.
    exit 95
    ;;
  *)
    exit 94
    ;;
esac
EOF

cat >"${TMP}/bin/docker" <<'EOF'
#!/bin/bash
set -uo pipefail
{
  printf 'docker'
  printf ' <%s>' "$@"
  printf '\n'
} >>"${TEST_LOG}"

case "${2:-}" in
  build|up|restart|down|stop|start|kill|rm)
    # A read-only forensic tool must never mutate the deployment.
    exit 95
    ;;
esac

if [[ "$1 ${2:-}" == "compose ps" ]]; then
  [[ "${TEST_PS_FAIL:-0}" -eq 1 ]] && exit 1
  printf 'NAME      IMAGE                  SERVICE   STATUS\n'
  printf 'sw-core   social_workflow-core   core      running\n'
  exit 0
fi

if [[ "$1 ${2:-} ${3:-}" == "compose port core" ]]; then
  [[ "${TEST_PORT_COMMAND_FAIL:-0}" -eq 1 ]] && exit 1
  if [[ "${TEST_PORT+x}" == x ]]; then
    mapping="${TEST_PORT}"
  else
    mapping='127.0.0.1:8000'
  fi
  if [[ "${TEST_PORT_NO_FINAL_NEWLINE:-0}" -eq 1 ]]; then
    printf '%s' "${mapping}"
  else
    printf '%s\n' "${mapping}"
  fi
  exit 0
fi

if [[ "$1 ${2:-}" == "compose logs" ]]; then
  [[ "${TEST_LOGS_FAIL:-0}" -eq 1 ]] && exit 1
  printf 'sw-core  | INFO:     Application startup complete.\n'
  case "${TEST_LOGS_MODE:-clean}" in
    ephemeral-port)
      # uvicorn prints the client ephemeral port; 44092 contains "409" but is NOT an HTTP 409.
      printf 'sw-core  | INFO:     127.0.0.1:44092 - "GET /health HTTP/1.1" 200 OK\n'
      printf 'sw-core  | INFO:     127.0.0.1:14090 - "GET /health HTTP/1.1" 200 OK\n'
      ;;
    conflict)
      printf 'sw-core  | INFO:     127.0.0.1:44092 - "GET /health HTTP/1.1" 200 OK\n'
      printf 'sw-core  | WARNING  Telegram 轮询失败（2s 后重试）: getUpdates 失败: error_code=409 Conflict\n'
      printf 'sw-core  | WARNING  Telegram 轮询失败（4s 后重试）: getUpdates 失败: error_code=409 Conflict\n'
      ;;
  esac
  exit 0
fi

if [[ "$1 ${2:-} ${3:-} ${4:-} ${5:-} ${6:-}" == "compose exec -T core python3 -c" ]]; then
  # 真实 `docker compose exec -T` 会把 stdin **转发**给容器内进程（这正是
  # `docker compose exec -T db psql < dump.sql` 能工作的原因）。这里被模拟的容器内
  # 进程自己就 sys.stdin.read()，所以直接 exec 出去即是忠实语义：stdin 被读干净。
  # 调用方必须自带显式 stdin 来源（`printf ... |` 管道），否则它会吃掉调用它的
  # 那份远端脚本正文。
  exec python3 -c "$7"
fi

if [[ "$1 ${2:-} ${3:-} ${4:-} ${5:-} ${6:-}" == "compose exec -T core python3 scripts/preflight.py" ]]; then
  # 同上：`-T` 仍然转发 stdin。preflight.py 自己不读 stdin，但 docker 客户端照样把
  # stdin 泵过去，于是这一步会把"调用者剩下的脚本正文"整个吞掉。旧假件不碰 stdin，
  # 语义与真实命令相反，正是它让这个阻断级缺陷在测试里根本不存在。
  # 这里显式把 stdin 读空，让"没有 </dev/null 保护的调用会丢掉后续脚本"真实发生。
  cat >/dev/null
  exit "${TEST_PREFLIGHT_STATUS:-0}"
fi

exit 93
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

case "${curl_url}" in
  */health)
    # TEST_HEALTH_STATUS 沿用改造前语义：直接指定 curl 退出码（传输层失败，%{http_code}=000）。
    [[ "${TEST_HEALTH_STATUS:-0}" -ne 0 ]] && curl_emit '' '000' "${TEST_HEALTH_STATUS}"
    curl_code="${TEST_HEALTH_HTTP_CODE:-200}"
    [[ "${curl_code}" -ge 400 ]] && curl_emit '' "${curl_code}" 22
    curl_emit '{"ok":true}' "${curl_code}" 0
    ;;
  */api/v1/system/info)
    [[ "${TEST_INFO_CURL_STATUS:-0}" -ne 0 ]] && curl_emit '' '000' "${TEST_INFO_CURL_STATUS}"
    curl_code="${TEST_INFO_HTTP_CODE:-200}"
    [[ "${curl_code}" -ge 400 ]] && curl_emit '' "${curl_code}" 22
    # Keep the default payload out of a ${VAR:-default} expansion: its nested braces
    # would terminate the expansion early and corrupt the JSON.
    if [[ -n "${TEST_INFO_JSON:-}" ]]; then
      curl_emit "${TEST_INFO_JSON}" "${curl_code}" 0
    fi
    curl_emit '{"ok":true,"data":{"version":"0.1.0","env":"prod","time":"2026-08-22T02:00:00Z","timezone":"Asia/Shanghai","scheduler_enabled":true,"generate_enabled":true,"use_fake_publishers":true,"auth_required":false,"publishers":["xhs","douyin"]}}' "${curl_code}" 0
    ;;
  */api/v1/system/telegram)
    [[ "${TEST_TELEGRAM_CURL_STATUS:-0}" -ne 0 ]] && curl_emit '' '000' "${TEST_TELEGRAM_CURL_STATUS}"
    curl_code="${TEST_TELEGRAM_HTTP_CODE:-200}"
    [[ "${curl_code}" -ge 400 ]] && curl_emit '' "${curl_code}" 22
    # Same nested-brace hazard as the info payload: keep the default out of ${VAR:-default}.
    if [[ -n "${TEST_TELEGRAM_JSON:-}" ]]; then
      curl_emit "${TEST_TELEGRAM_JSON}" "${curl_code}" 0
    fi
    curl_emit '{"ok":true,"data":{"enabled":true,"configured":true,"ready":true,"chat_configured":true,"can_sign":true,"polling":true,"username":"sw_ops_bot","sent":3,"failed":0,"stats":{},"detail":"","last_error":""}}' "${curl_code}" 0
    ;;
  */api/v1/dashboard|*/api/v1/dashboard\?*)
    # 形状照着 core/api/dashboard.py 的响应模型抄：Envelope{ok,data,error} 外壳里裹一个
    # DashboardOut{generated_at,window_days,counters,budget,platforms,attention,events}，
    # counters 是 Counters 的**全部** 12 个字段（假件少字段会把"字段缺失"那条降级路径
    # 变得测不出来）。默认 awaiting_confirm=0，与其余用例的既有输出互不干扰。
    [[ "${TEST_DASHBOARD_CURL_STATUS:-0}" -ne 0 ]] && curl_emit '' '000' "${TEST_DASHBOARD_CURL_STATUS}"
    curl_code="${TEST_DASHBOARD_HTTP_CODE:-200}"
    [[ "${curl_code}" -ge 400 ]] && curl_emit '' "${curl_code}" 22
    # 与 info / telegram 同一处陷阱：默认负载里的花括号会提前终止 ${VAR:-default} 展开。
    if [[ -n "${TEST_DASHBOARD_JSON:-}" ]]; then
      curl_emit "${TEST_DASHBOARD_JSON}" "${curl_code}" 0
    fi
    curl_emit '{"ok":true,"data":{"generated_at":"2026-08-22T02:00:00Z","window_days":1,"counters":{"pending_review":0,"published_today":0,"published_7d":0,"failed":0,"dead_letter":0,"scheduled":0,"suspended":0,"awaiting_confirm":0,"rendering":0,"accounts_needing_relogin":0,"accounts_degraded":0,"accounts_suspended":0},"budget":{},"platforms":[],"attention":[],"events":[]},"error":null}' "${curl_code}" 0
    ;;
esac
exit 92
EOF

cat >"${TMP}/bin/sleep" <<'EOF'
#!/bin/bash
exit 0
EOF

chmod +x "${TMP}/bin/"*

failures=0
passed=0
case_name=""
case_failures_at_start=0
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

# A Telegram bot token looks like <8-10 digits>:<35 chars of base64ish>. The forensic
# output must never carry that shape — TelegramOut is contractually token-free, and this
# pins that the new section did not start echoing something that is.
assert_no_token_shape() {
  if grep -Eq '[0-9]{6,}:[A-Za-z0-9_-]{20,}' <<<"$1"; then
    fail_assertion "output carries a bot-token shaped string; value: $1"
  fi
}

assert_log_count() {
  local needle="$1" expected="$2" actual
  actual="$(grep -F -c -- "${needle}" <<<"${LOG}" || true)"
  if [[ "${actual}" -ne "${expected}" ]]; then
    fail_assertion "log count [${needle}]=${actual}, expected=${expected}; log: ${LOG}"
  fi
}

# 「核验结论」段的完整性本身就是被测后果：远端脚本正文被某个读 stdin 的子进程吞掉时，
# 这一整段（连同失败项汇总和 exit 1 判定）会静默消失，而脚本以 0 收尾。只数结论段
# 内部的裁定行——外层 ok() 打的 "✓ 生产部署核验通过" 在段外，不参与计数。
assert_conclusion_verdicts() {
  local want_pass="$1" want_fail="$2" section got_pass got_fail
  section="$(awk '
    /^核验结论$/ { inside = 1; next }
    /^全部核验项通过。/ { inside = 0 }
    /^✗ 生产部署核验失败/ { inside = 0 }
    inside
  ' <<<"${RESULT}")"
  if [[ -z "${section}" ]]; then
    fail_assertion "核验结论 section is missing entirely; output: ${RESULT}"
    return
  fi
  got_pass="$(grep -c '^  ✓ ' <<<"${section}" || true)"
  got_fail="$(grep -c '^  ✗ ' <<<"${section}" || true)"
  if [[ "${got_pass}" -ne "${want_pass}" || "${got_fail}" -ne "${want_fail}" ]]; then
    fail_assertion "核验结论 has ✓=${got_pass} ✗=${got_fail}, expected ✓=${want_pass} ✗=${want_fail}; section: ${section}"
  fi
}

run_case() {
  case_name="$1"
  case_failures_at_start="${failures}"
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
  # 自己 shell 里的导出影响；用例要带 token 时，自己在 env_args 里给一个（-u 先生效，
  # 随后的赋值照样落地，本机 env(1) 实测如此）。
  # TEST_AUTH_LOG 与 TEST_LOG 是**两个文件**：TEST_LOG 记 argv，要被断言"token 出现 0 次"；
  # TEST_AUTH_LOG 记 curl 从 --config - 里真正解析出来的头，要被断言"token 确实送到了"。
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

end_case() {
  if [[ "${failures}" -eq "${case_failures_at_start}" ]]; then
    passed=$((passed + 1))
    printf 'PASS %s\n' "${case_name}"
  fi
}

# Every check the script performs must be side-effect free.
assert_read_only() {
  assert_not_contains "${LOG}" "backup <"
  assert_not_contains "${LOG}" "git <fetch>"
  assert_not_contains "${LOG}" "git <pull>"
  assert_not_contains "${LOG}" "git <merge>"
  assert_not_contains "${LOG}" "git <reset>"
  assert_not_contains "${LOG}" "git <switch>"
  assert_not_contains "${LOG}" "git <checkout>"
  assert_not_contains "${LOG}" "docker <compose> <build>"
  assert_not_contains "${LOG}" "docker <compose> <up>"
  assert_not_contains "${LOG}" "docker <compose> <restart>"
  assert_not_contains "${LOG}" "docker <compose> <down>"
  assert_not_contains "${LOG}" "docker <compose> <stop>"
}

assert_rejected_before_ssh() {
  assert_status 1
  assert_not_contains "${LOG}" "ssh <"
  assert_not_contains "${LOG}" "backup <"
  assert_not_contains "${LOG}" "git <"
  assert_not_contains "${LOG}" "docker <"
  assert_not_contains "${LOG}" "curl <"
}

# ---------------------------------------------------------------- CLI 校验
# All --sha validation is local and must precede any SSH round trip.
run_case "uppercase sha rejected before ssh" --sha ABCDEFABCDEFABCDEFABCDEFABCDEFABCDEFABCD
assert_rejected_before_ssh
assert_contains "${RESULT}" "小写"
end_case

run_case "short sha rejected before ssh" --sha 2222222
assert_rejected_before_ssh
assert_contains "${RESULT}" "40 位"
end_case

run_case "41 char sha rejected before ssh" --sha 22222222222222222222222222222222222222222
assert_rejected_before_ssh
assert_contains "${RESULT}" "40 位"
end_case

run_case "non hex sha rejected before ssh" --sha 222222222222222222222222222222222222222g
assert_rejected_before_ssh
assert_contains "${RESULT}" "40 位"
end_case

run_case "duplicate sha rejected before ssh" --sha "${HEAD_SHA}" --sha "${HEAD_SHA}"
assert_rejected_before_ssh
assert_contains "${RESULT}" "--sha 只能指定一次"
end_case

run_case "missing sha value rejected before ssh" --sha
assert_rejected_before_ssh
assert_contains "${RESULT}" "--sha 缺少"
end_case

run_case "sha value is option rejected before ssh" --sha --preflight
assert_rejected_before_ssh
assert_contains "${RESULT}" "--sha 缺少"
end_case

run_case "unknown flag rejected before ssh" --nope
assert_rejected_before_ssh
assert_contains "${RESULT}" "参数无效"
end_case

run_case "positional argument rejected before ssh" extra
assert_rejected_before_ssh
assert_contains "${RESULT}" "参数无效"
end_case

run_case "help exits zero without ssh" --help
assert_status 0
assert_not_contains "${LOG}" "ssh <"
assert_contains "${RESULT}" "用法：bash scripts/ops/verify.sh"
end_case

# ------------------------------------------------------- 远端参数边界（ssh 拼接）
# ssh joins argv with single spaces, so the script must %q-escape its parameters.
# These cases pin that every parameter — including the empty SHA — survives intact.
run_case "empty sha survives ssh re-tokenization"
assert_status 0
assert_contains "${LOG}" "ssh-command <bash -s -- '' 0"
assert_contains "${LOG}" "remote-argc <4>"
assert_contains "${LOG}" "remote-argv[1]=[-s]"
assert_contains "${LOG}" "remote-argv[2]=[--]"
assert_contains "${LOG}" "remote-argv[3]=[]"
assert_contains "${LOG}" "remote-argv[4]=[0]"
end_case

run_case "explicit sha reaches the remote intact" --sha "${HEAD_SHA}"
assert_status 0
assert_contains "${LOG}" "remote-argc <4>"
assert_contains "${LOG}" "remote-argv[3]=[${HEAD_SHA}]"
assert_contains "${LOG}" "remote-argv[4]=[0]"
end_case

run_case "preflight flag reaches the remote intact" --preflight
assert_status 0
assert_contains "${LOG}" "remote-argc <4>"
assert_contains "${LOG}" "remote-argv[3]=[]"
assert_contains "${LOG}" "remote-argv[4]=[1]"
end_case

run_case "sha and preflight together reach the remote intact" --sha "${HEAD_SHA}" --preflight
assert_status 0
assert_contains "${LOG}" "remote-argc <4>"
assert_contains "${LOG}" "remote-argv[3]=[${HEAD_SHA}]"
assert_contains "${LOG}" "remote-argv[4]=[1]"
end_case

# ---------------------------------------------------------------- 正常路径
run_case "happy path exits zero"
assert_status 0
assert_read_only
assert_log_count "ssh <" 1
assert_contains "${LOG}" "git <symbolic-ref> <--quiet> <--short> <HEAD>"
assert_contains "${LOG}" "git <rev-parse> <--verify> <HEAD>"
assert_contains "${LOG}" "git <status> <--porcelain>"
assert_contains "${LOG}" "docker <compose> <ps>"
assert_contains "${LOG}" "docker <compose> <port> <core> <8000>"
assert_contains "${LOG}" "curl <-q> <-fsS> <--max-time> <10> <-w> <\n%{http_code}> <--config> <-> <http://127.0.0.1:8000/health>"
assert_contains "${LOG}" "curl <-q> <-fsS> <--max-time> <10> <-w> <\n%{http_code}> <--config> <-> <http://127.0.0.1:8000/api/v1/system/info>"
assert_contains "${LOG}" "docker <compose> <logs> <--tail> <2000> <core>"
assert_contains "${RESULT}" "✓ HEAD 可读  ${HEAD_SHA}"
assert_contains "${RESULT}" "✓ 工作树干净"
assert_contains "${RESULT}" "✓ Compose 服务可读"
assert_contains "${RESULT}" "✓ 端口门禁"
assert_contains "${RESULT}" "✓ 健康探针 GET /health 200"
assert_contains "${RESULT}" "✓ 运行环境 env=prod"
assert_contains "${RESULT}" "✓ Telegram error_code=409 计数为 0"
assert_contains "${RESULT}" "全部核验项通过"
assert_contains "${RESULT}" "✓ 生产部署核验通过"
assert_contains "${RESULT}" "未指定 --sha，跳过比对"
end_case

run_case "happy path prints runtime fields verbatim"
assert_status 0
assert_contains "${RESULT}" "版本  0.1.0"
assert_contains "${RESULT}" "环境  prod"
assert_contains "${RESULT}" "服务时间  2026-08-22T02:00:00Z"
assert_contains "${RESULT}" "时区  Asia/Shanghai"
assert_contains "${RESULT}" "调度器  True"
assert_contains "${RESULT}" "生成开关  True"
assert_contains "${RESULT}" "模拟发布器  True"
assert_contains "${RESULT}" "鉴权  False"
assert_contains "${RESULT}" "已注册发布器  xhs, douyin"
assert_contains "${RESULT}" "裁定  模拟发布器=True：如实记录，本身不构成失败项"
assert_contains "${RESULT}" "裁定  鉴权=False：如实记录，本身不构成失败项"
end_case

run_case "fake publishers true and auth false are not failures"
assert_status 0
assert_not_contains "${RESULT}" "✗"
end_case

run_case "matching sha exits zero" --sha "${HEAD_SHA}"
assert_status 0
assert_read_only
assert_contains "${RESULT}" "✓ HEAD 等于期望 SHA  ${HEAD_SHA}"
end_case

# ---------------------------------------------------- 发布线（事实）与部署标记（意图）
# docs/RISKS.md 第 11 条：生产上那个本地分支**名叫 main**，承载的却是 p14-organic 的顶端。
# 从前这里按本地分支名去查 origin/<branch>，于是打出「HEAD 领先 14 个提交」——字面为真，
# 参照系却是错的，读的人会以为生产上跑着 14 个没推上去的提交。下面这一组用例钉死新口径：
# 报告 HEAD 被哪些 origin ref **包含**，命中多个就全列出来，绝不挑一个当参照系。
run_case "the release line is reported by containment, not by branch name"
assert_status 0
assert_read_only
assert_contains "${LOG}" "git <branch> <-r> <--contains> <${HEAD_SHA}>"
assert_contains "${RESULT}" "发布线    HEAD 被下列 origin ref 包含"
assert_contains "${RESULT}" "origin/p14-organic=${HEAD_SHA}  HEAD 正好等于它的顶端"
# 旧口径的措辞一个字都不许再出现——它就是那条错误参照系。
assert_not_contains "${RESULT}" "远端对比"
assert_not_contains "${RESULT}" "HEAD 领先"
end_case

# 这一格是本次要修的那个真实生产形态：本地分支名 main，实际在 p14-organic 线上。
# 关键断言是**两条都被列出来**且各自说清关系，没有任何一条被当成"那条线"。
run_case "every containing ref is listed, none is singled out" \
  TEST_BRANCH=main TEST_CONTAIN_REFS="origin/p14-organic origin/release" \
  TEST_ORIGIN_TIPS="origin/p14-organic=${HEAD_SHA} origin/release=${OTHER}" TEST_BEHIND=3
assert_status 0
assert_read_only
assert_contains "${RESULT}" "当前分支  main"
assert_contains "${RESULT}" "origin/p14-organic=${HEAD_SHA}  HEAD 正好等于它的顶端"
assert_contains "${RESULT}" "origin/release=${OTHER}  HEAD 在这条线上，落后它 3 个提交"
end_case

# `origin/HEAD -> origin/main` 是别名不是发布线，必须被跳过——否则清单里会多出一条
# 看起来像发布线、其实只是符号 ref 的东西。
run_case "a symbolic origin/HEAD alias is skipped" \
  TEST_CONTAIN_REFS="origin/HEAD~origin/p14-organic origin/p14-organic"
assert_status 0
assert_contains "${RESULT}" "origin/p14-organic=${HEAD_SHA}  HEAD 正好等于它的顶端"
assert_not_contains "${RESULT}" "origin/HEAD"
end_case

# 一条都没命中不是失败：本脚本不 fetch，本地 remote-tracking ref 可能陈旧。
run_case "no containing ref is stated plainly and is not a failure" TEST_CONTAIN_REFS=""
assert_status 0
assert_read_only
assert_contains "${RESULT}" "<一条都没命中>"
assert_contains "${RESULT}" "本脚本不 fetch，本地 remote-tracking ref 可能陈旧"
assert_conclusion_verdicts 8 0
end_case

run_case "a failing containment query degrades without a verdict" TEST_CONTAIN_FAIL=1
assert_status 0
assert_contains "${RESULT}" "<git branch -r --contains 读取失败>"
assert_conclusion_verdicts 8 0
end_case

# detached HEAD 现在**照样能报发布线**：--contains 问的是提交，与有没有分支名无关。
# 这是新口径顺带修好的一格——旧口径在这里直接放弃比较。
run_case "detached head still gets a release line" TEST_DETACHED=1
assert_status 0
assert_contains "${RESULT}" "当前分支  <detached HEAD>"
assert_contains "${RESULT}" "origin/p14-organic=${HEAD_SHA}  HEAD 正好等于它的顶端"
assert_not_contains "${RESULT}" "没有对应的 origin 分支"
end_case

run_case "an unreadable ref tip is reported per ref" TEST_NO_ORIGIN=1
assert_status 0
assert_read_only
assert_contains "${RESULT}" "origin/p14-organic  <顶端读取失败>"
end_case

# ---- 部署标记（意图）------------------------------------------------------------
# 标记记的是"上次 update.sh --apply 打算部署哪条线"，与上面那份"事实"并列。
# 它必须落在 git 工作树**外面**（~/sw-deploy-state），否则会让「工作树干净」那道门禁失败。
MARKER_DIR="${TMP}/home dir/sw-deploy-state"
MARKER_FILE="${MARKER_DIR}/last-deploy"
write_marker() {
  mkdir -p "${MARKER_DIR}"
  printf '%s' "$1" >"${MARKER_FILE}"
}
clear_marker() { rm -rf "${MARKER_DIR}"; }

clear_marker
run_case "a missing deploy marker is normal, not a failure"
assert_status 0
assert_contains "${RESULT}" "部署标记  <没有记录>"
assert_contains "${RESULT}" "正常情形，不是失败"
assert_conclusion_verdicts 8 0
end_case

write_marker "schema=1
ref=p14-organic
sha=${HEAD_SHA}
at=2026-08-23T09:15:00Z
"
run_case "a consistent deploy marker is shown next to the fact"
assert_status 0
assert_contains "${RESULT}" "部署标记  ref=p14-organic  sha=${HEAD_SHA}  at=2026-08-23T09:15:00Z"
assert_contains "${RESULT}" "这是**意图**，上面的发布线是**事实**"
assert_contains "${RESULT}" "对照  标记里的 sha 与当前 HEAD 一致"
assert_contains "${RESULT}" "对照  标记里的 origin/p14-organic 与 HEAD 实际所在的发布线一致"
assert_not_contains "${RESULT}" "对照  ⚠"
end_case

# 标记之后有人动过 HEAD：这正是"意图与事实分叉"该被看见的那一刻。
write_marker "schema=1
ref=p14-organic
sha=${OTHER}
at=2026-08-23T09:15:00Z
"
run_case "a stale marker sha is called out as an inconsistency"
assert_status 0
assert_contains "${RESULT}" "对照  ⚠ 标记里的 sha 与当前 HEAD 不一致（HEAD=${HEAD_SHA}）"
assert_contains "${RESULT}" "手工 merge/pull、或走了本工具面之外的部署路径"
# 不一致只是信号，不是门禁——裁定数一个不多一个不少。
assert_conclusion_verdicts 8 0
end_case

# 意图说的那条线压根不在事实清单里：明说以事实为准，并且不下"所以出事了"的结论。
write_marker "schema=1
ref=main
sha=${HEAD_SHA}
at=2026-08-23T09:15:00Z
"
run_case "a marker naming a line HEAD is not on is called out"
assert_status 0
assert_contains "${RESULT}" "对照  ⚠ 标记说部署的是 origin/main，但 HEAD 不在它的历史里"
assert_contains "${RESULT}" "以 --contains 那份事实为准"
end_case

# upstream 形态写进来的是 origin/main 这种带前缀的名字，归一之后照样能比。
write_marker "schema=1
ref=origin/p14-organic
sha=${HEAD_SHA}
at=2026-08-23T09:15:00Z
"
run_case "a marker written by the upstream path normalizes to the same ref"
assert_status 0
assert_contains "${RESULT}" "对照  标记里的 origin/p14-organic 与 HEAD 实际所在的发布线一致"
end_case

# 读不懂就说读不懂：一条猜出来的"上次部署的是 X"比没有记录更坏。
write_marker "ref=p14-organic
sha=${HEAD_SHA}
"
run_case "an unparseable marker is treated as no record, never guessed"
assert_status 0
assert_contains "${RESULT}" "的内容读不懂，按「没有记录」处理；本脚本不猜"
assert_not_contains "${RESULT}" "对照  "
end_case

write_marker "schema=1
ref=p14-organic
sha=not-a-sha
at=2026-08-23T09:15:00Z
"
run_case "a marker with a malformed sha is refused too"
assert_status 0
assert_contains "${RESULT}" "的内容读不懂，按「没有记录」处理"
assert_not_contains "${RESULT}" "not-a-sha"
end_case

# 清单本身没读到时，绝不许对着一份不存在的事实下"不在那条线上"的结论。
# "没读到"与"读到了、里面没有它"是两件事——本任务修的正是这一类"字面为真、指向错误"的话。
write_marker "schema=1
ref=p14-organic
sha=${HEAD_SHA}
at=2026-08-23T09:15:00Z
"
run_case "an unreadable containment list makes the ref comparison say so" TEST_CONTAIN_FAIL=1
assert_status 0
assert_contains "${RESULT}" "<git branch -r --contains 读取失败>"
assert_contains "${RESULT}" "上面那份发布线清单没读到，所以这一格**对不了**"
assert_not_contains "${RESULT}" "但 HEAD 不在它的历史里"
# sha 那一格与清单无关，照样能比。
assert_contains "${RESULT}" "对照  标记里的 sha 与当前 HEAD 一致"
end_case
clear_marker

# ---------------------------------------------------------------- 失败裁定
run_case "sha mismatch fails and names the gate" TEST_HEAD_SHA="${OTHER}" --sha "${HEAD_SHA}"
assert_status 1
assert_read_only
assert_contains "${RESULT}" "✗ HEAD 等于期望 SHA  HEAD=${OTHER}，期望=${HEAD_SHA}"
assert_contains "${RESULT}" "1 项未通过"
assert_contains "${RESULT}" "✗ 生产部署核验失败"
end_case

run_case "dirty worktree fails" TEST_DIRTY=1
assert_status 1
assert_read_only
assert_contains "${RESULT}" "✗ 工作树干净  存在未提交改动"
assert_contains "${RESULT}" "M core/main.py"
end_case

run_case "unreadable git status fails" TEST_STATUS_FAIL=1
assert_status 1
assert_contains "${RESULT}" "✗ 工作树干净  无法读取 git status"
end_case

run_case "unreadable head fails" TEST_HEAD_FAIL=1
assert_status 1
assert_contains "${RESULT}" "✗ HEAD 可读  无法读取当前 HEAD"
assert_contains "${RESULT}" "当前提交  <未知>"
end_case

run_case "compose ps failure fails" TEST_PS_FAIL=1
assert_status 1
assert_contains "${RESULT}" "✗ Compose 服务可读"
end_case

# ---------------------------------------------------------------- 端口门禁
invalid_ports=(
  ''
  '0.0.0.0:8000'
  '127.0.0.1:0'
  '127.0.0.1:08000'
  '127.0.0.1:70000'
  '127.0.0.1:65536'
  '127.0.0.1:99999'
  '127.0.0.1:8x00'
  '127.0.0.1:8000:extra'
  ':::8000'
  '[::]:8000'
  $'127.0.0.1:8000\r'
  $'127.0.0.1:8000\n127.0.0.1:8001'
  $'127.0.0.1:8000\n'
)
for invalid_port in "${invalid_ports[@]}"; do
  run_case "reject port $(printf '%q' "${invalid_port}")" TEST_PORT="${invalid_port}"
  assert_status 1
  assert_read_only
  assert_contains "${RESULT}" "拒绝映射"
  assert_contains "${RESULT}" "✗ 端口门禁"
  assert_contains "${RESULT}" "✗ 健康探针 GET /health 200  未执行（端口门禁未通过）"
  assert_contains "${RESULT}" "✗ 运行环境 env=prod  未执行（端口门禁未通过）"
  assert_not_contains "${LOG}" "curl <"
  assert_not_contains "${LOG}" "docker <compose> <exec>"
  end_case
done

run_case "port command failure fails" TEST_PORT_COMMAND_FAIL=1
assert_status 1
assert_contains "${RESULT}" "无法读取 core 8000 的发布端口"
assert_not_contains "${LOG}" "curl <"
end_case

# 缺尾换行是正常情形（不是截断）：末尾哨兵剥掉后仍是一条合法映射，必须放行。
run_case "port output without a trailing newline is accepted" TEST_PORT_NO_FINAL_NEWLINE=1 TEST_PORT='127.0.0.1:8000'
assert_status 0
assert_contains "${RESULT}" "✓ 端口门禁  core:8000 -> 127.0.0.1:8000"
end_case

run_case "ipv6 loopback port probes the bracketed host" TEST_PORT='[::1]:65535'
assert_status 0
assert_contains "${LOG}" "curl <-q> <-fsS> <--max-time> <10> <-w> <\n%{http_code}> <--config> <-> <http://[::1]:65535/health>"
assert_contains "${LOG}" "curl <-q> <-fsS> <--max-time> <10> <-w> <\n%{http_code}> <--config> <-> <http://[::1]:65535/api/v1/system/info>"
end_case

run_case "port 1 ipv4 is accepted" TEST_PORT='127.0.0.1:1'
assert_status 0
assert_contains "${LOG}" "curl <-q> <-fsS> <--max-time> <10> <-w> <\n%{http_code}> <--config> <-> <http://127.0.0.1:1/health>"
end_case

# ---------------------------------------------------------------- 探针
run_case "health probe failure fails" TEST_HEALTH_STATUS=22
assert_status 1
assert_contains "${RESULT}" "✗ 健康探针 GET /health 200  curl 退出码 22"
end_case

run_case "non prod env fails" TEST_INFO_JSON='{"ok":true,"data":{"version":"0.1.0","env":"dev","time":"t","timezone":"Asia/Shanghai","scheduler_enabled":true,"generate_enabled":true,"use_fake_publishers":true,"auth_required":false,"publishers":["xhs"]}}'
assert_status 1
assert_contains "${RESULT}" "环境  dev"
assert_contains "${RESULT}" "裁定  环境必须是 prod，实际 dev"
assert_contains "${RESULT}" "✗ 运行环境 env=prod  实际 env 不是 prod"
end_case

run_case "failed envelope fails" TEST_INFO_JSON='{"ok":false,"error":"boom"}'
assert_status 1
assert_contains "${RESULT}" "返回失败外壳"
assert_contains "${RESULT}" "✗ 运行环境 env=prod  无法解析"
end_case

run_case "unparsable info fails" TEST_INFO_JSON='not json'
assert_status 1
assert_contains "${RESULT}" "JSON 解析失败"
assert_contains "${RESULT}" "✗ 运行环境 env=prod  无法解析"
end_case

run_case "info curl failure fails" TEST_INFO_CURL_STATUS=7
assert_status 1
assert_contains "${RESULT}" "✗ 运行环境 env=prod  无法获取 /api/v1/system/info"
end_case

# -------------------------------------------------- 人工确认闸门通道（R1 互锁）
# R1：内容上线必须由人点一下才真发（Telegram 闸门消息，或工作台「确认发布」，同一后端
# core.confirm.confirm_item）。Telegram 是主载体。真发布开着而这条通道是死的，后果**不是**
# 内容越权发出去，而是 core/scheduler.py:498-505 里的人工确认闸门（tick_scheduled_publish
# 中的 `confirm_required(policy) and item.confirmed_at is None`；叫名字不叫序数，理由见
# scripts/ops/verify.sh 同段注释）把内容跳过不发、在排期处堆积，再由
# SW_CONFIRM_TTL_HOURS 到点自动驳回——发布链路停摆。所以互锁的松紧完全由
# use_fake_publishers 决定。
REAL_PUBLISHERS_INFO='{"ok":true,"data":{"version":"0.1.0","env":"prod","time":"2026-08-22T02:00:00Z","timezone":"Asia/Shanghai","scheduler_enabled":true,"generate_enabled":true,"use_fake_publishers":false,"auth_required":false,"publishers":["xhs","douyin"]}}'
TELEGRAM_DEAD='{"ok":true,"data":{"enabled":true,"configured":true,"ready":false,"chat_configured":false,"can_sign":true,"polling":false,"username":"","sent":0,"failed":0,"stats":{},"detail":"缺 TELEGRAM_CHAT_ID：先给 bot 发一条 /start，再跑 uv run python -m core.telegram setup 把打印出来的 id 写进 .env"}}'
TELEGRAM_NO_POLLING='{"ok":true,"data":{"enabled":true,"configured":true,"ready":true,"chat_configured":true,"can_sign":true,"polling":false,"username":"sw_ops_bot","sent":0,"failed":0,"stats":{},"detail":""}}'
# 总开关关着：ready/polling 都真也发不出去（build_telegram_notifier() 直接返回 None）。
TELEGRAM_SWITCH_OFF='{"ok":true,"data":{"enabled":false,"configured":true,"ready":true,"chat_configured":true,"can_sign":true,"polling":true,"username":"sw_ops_bot","sent":0,"failed":0,"stats":{},"detail":"SW_TELEGRAM_ENABLED=false，Telegram 通道整体关闭"}}'

run_case "happy path prints every confirm channel field"
assert_status 0
assert_contains "${LOG}" "curl <-q> <-fsS> <--max-time> <10> <-w> <\n%{http_code}> <--config> <-> <http://127.0.0.1:8000/api/v1/system/telegram>"
assert_contains "${RESULT}" "总开关 enabled  True"
assert_contains "${RESULT}" "已配 token configured  True"
assert_contains "${RESULT}" "可推送 ready  True"
assert_contains "${RESULT}" "已知会话 chat_configured  True"
assert_contains "${RESULT}" "可签名 can_sign  True"
assert_contains "${RESULT}" "轮询线程 polling  True"
assert_contains "${RESULT}" "bot 用户名  sw_ops_bot"
assert_contains "${RESULT}" "本进程已推送  3"
assert_contains "${RESULT}" "本进程推送失败  0"
assert_contains "${RESULT}" "指引 detail  <空：通道可用时为空>"
assert_no_token_shape "${RESULT}"
end_case

# 模拟发布器开着 = 什么都不会真发，通道死了也只如实记录。
run_case "fake publishers tolerate a dead confirm channel" TEST_TELEGRAM_JSON="${TELEGRAM_DEAD}"
assert_status 0
assert_read_only
assert_contains "${RESULT}" "可推送 ready  False"
assert_contains "${RESULT}" "轮询线程 polling  False"
assert_contains "${RESULT}" "指引 detail  缺 TELEGRAM_CHAT_ID"
assert_contains "${RESULT}" "裁定  模拟发布器=true：确认通道 ready=false"
assert_contains "${RESULT}" "✓ 人工确认闸门通道 enabled+ready+polling  模拟发布器=true：如实记录"
assert_no_token_shape "${RESULT}"
end_case

run_case "real publishers with a not ready channel fail" \
  TEST_INFO_JSON="${REAL_PUBLISHERS_INFO}" TEST_TELEGRAM_JSON="${TELEGRAM_DEAD}"
assert_status 1
assert_read_only
assert_contains "${RESULT}" "模拟发布器  False"
assert_contains "${RESULT}" "后果  确认卡推不出去或回调收不回来"
assert_contains "${RESULT}" "SW_CONFIRM_TTL_HOURS 到点自动驳回，发布链路停摆"
assert_contains "${RESULT}" "兜底  工作台的「确认发布」按钮不受 Telegram 影响"
assert_contains "${RESULT}" "✗ 人工确认闸门通道 enabled+ready+polling  真发布已开启（模拟发布器=false），但 ready=false"
assert_contains "${RESULT}" "1 项未通过"
assert_no_token_shape "${RESULT}"
end_case

# ready=true 但轮询线程死了：卡片推得出去，人点了没有线程去收，回调永远不回来。
run_case "real publishers with a dead poller fail" \
  TEST_INFO_JSON="${REAL_PUBLISHERS_INFO}" TEST_TELEGRAM_JSON="${TELEGRAM_NO_POLLING}"
assert_status 1
assert_contains "${RESULT}" "可推送 ready  True"
assert_contains "${RESULT}" "轮询线程 polling  False"
assert_contains "${RESULT}" "✗ 人工确认闸门通道 enabled+ready+polling  真发布已开启（模拟发布器=false），但 ready=true 但 polling=false"
end_case

# ready 只看 token+chat_id，不看总开关。总开关关着时 ready/polling 可以双真，
# 但一条都发不出去——所以互锁必须直接判 enabled，不能靠 polling 间接兜。
run_case "real publishers with the telegram master switch off fail" \
  TEST_INFO_JSON="${REAL_PUBLISHERS_INFO}" TEST_TELEGRAM_JSON="${TELEGRAM_SWITCH_OFF}"
assert_status 1
assert_read_only
assert_contains "${RESULT}" "总开关 enabled  False"
assert_contains "${RESULT}" "可推送 ready  True"
assert_contains "${RESULT}" "轮询线程 polling  True"
assert_contains "${RESULT}" "✗ 人工确认闸门通道 enabled+ready+polling  真发布已开启（模拟发布器=false），但 总开关 enabled=false"
assert_contains "${RESULT}" "build_telegram_notifier() 直接返回 None"
assert_contains "${RESULT}" "1 项未通过"
end_case

run_case "fake publishers tolerate the telegram master switch being off" \
  TEST_TELEGRAM_JSON="${TELEGRAM_SWITCH_OFF}"
assert_status 0
assert_read_only
assert_contains "${RESULT}" "总开关 enabled  False"
assert_contains "${RESULT}" "指引 detail  SW_TELEGRAM_ENABLED=false"
assert_contains "${RESULT}" "✓ 人工确认闸门通道 enabled+ready+polling  模拟发布器=true：如实记录 总开关 enabled=false"
assert_not_contains "${RESULT}" "✗"
end_case

run_case "real publishers with a live channel pass" TEST_INFO_JSON="${REAL_PUBLISHERS_INFO}"
assert_status 0
assert_read_only
assert_contains "${RESULT}" "✓ 人工确认闸门通道 enabled+ready+polling  真发布已开启（模拟发布器=false），通道 ready=true polling=true"
assert_not_contains "${RESULT}" "✗"
end_case

# ------------------------------------------------- 轮询假活（polling=true 骗不过去）
# polling 那一格是 core/telegram.py:981 的 bool(poller and poller.alive)，**只看线程活没活**。
# _loop（809-831）里 poll_once() 抛 TelegramError 只记 stats.errors / last_error 再退避重试，
# 线程永不退出。所以 token 被撤销 / 网络长期不通时线程**假活**：polling 照报 true，一条
# callback 都收不到。下面这组用例把"能识破假活"和"绝不因为陈旧错误假红"两头一起钉住。
#
# 假活：线程活着，但本进程启动以来一次 getUpdates 都没成功过（stats.polls=0），且已失败 137 次。
TELEGRAM_FAKE_ALIVE='{"ok":true,"data":{"enabled":true,"configured":true,"ready":true,"chat_configured":true,"can_sign":true,"polling":true,"username":"","sent":0,"failed":0,"stats":{"polls":0,"updates":0,"handled":0,"rejected":0,"errors":137},"detail":"","last_error":"getUpdates 失败: error_code=401 Unauthorized"}}'
# 真活：轮询已成功推进 9412 次，last_error 里躺着一条**早就恢复了**的瞬时抖动，errors 也非零。
# 这是本组最重要的一根钉子：last_error / stats.errors 只增不减（core/telegram.py 里
# poll_once 成功时压根不清 last_error），拿它们判红就是给生产造一个永久假红的闸门。
TELEGRAM_TRUE_ALIVE='{"ok":true,"data":{"enabled":true,"configured":true,"ready":true,"chat_configured":true,"can_sign":true,"polling":true,"username":"sw_ops_bot","sent":3,"failed":0,"stats":{"polls":9412,"updates":58,"handled":21,"rejected":2,"errors":3},"detail":"","last_error":"getUpdates 请求失败: ConnectTimeout: "}}'
# 老版本 core：TelegramOut 里还没有 stats / last_error 这两个字段，整个键都不存在。
TELEGRAM_OLD_CORE='{"ok":true,"data":{"enabled":true,"configured":true,"ready":true,"chat_configured":true,"can_sign":true,"polling":true,"username":"sw_ops_bot","sent":3,"failed":0,"detail":""}}'
# 字段缺失：stats 对象在，但里面没有 polls 这一项。
TELEGRAM_STATS_PARTIAL='{"ok":true,"data":{"enabled":true,"configured":true,"ready":true,"chat_configured":true,"can_sign":true,"polling":true,"username":"sw_ops_bot","sent":0,"failed":0,"stats":{"updates":5,"handled":1,"rejected":0,"errors":9},"detail":"","last_error":""}}'
# 类型不对：polls 是个字符串。既不能当 0，也不能崩。
TELEGRAM_STATS_BADTYPE='{"ok":true,"data":{"enabled":true,"configured":true,"ready":true,"chat_configured":true,"can_sign":true,"polling":true,"username":"sw_ops_bot","sent":0,"failed":0,"stats":{"polls":"many","updates":5,"handled":1,"rejected":0,"errors":9},"detail":"","last_error":null}}'
# 刚起：polls=0 但 errors=0 —— 首次 long polling 还没返回（poll_timeout 默认 30s）。
# 证据不足以判死，必须放行，否则每次重启后的头 30 秒都会误红。
TELEGRAM_JUST_STARTED='{"ok":true,"data":{"enabled":true,"configured":true,"ready":true,"chat_configured":true,"can_sign":true,"polling":true,"username":"sw_ops_bot","sent":0,"failed":0,"stats":{"polls":0,"updates":0,"handled":0,"rejected":0,"errors":0},"detail":"","last_error":""}}'
# last_error 里带着上游 URL，而 URL 里就是 bot token（core/telegram.py:263 把整个 httpx
# 异常插进了错误文案）。渲染这一格绝不许把 token 打出来。
TELEGRAM_TOKEN_IN_ERROR='{"ok":true,"data":{"enabled":true,"configured":true,"ready":true,"chat_configured":true,"can_sign":true,"polling":true,"username":"","sent":0,"failed":0,"stats":{"polls":0,"updates":0,"handled":0,"rejected":0,"errors":4},"detail":"","last_error":"getUpdates 请求失败: ConnectError: [Errno 8] nodename nor servname provided for https://api.telegram.org/bot7263991180:AAHkQwZmXcVbNnMlKjHgFdSaPoIuYtReWq0/getUpdates"}}'

run_case "a fake-alive poller no longer passes as alive under real publishers" \
  TEST_INFO_JSON="${REAL_PUBLISHERS_INFO}" TEST_TELEGRAM_JSON="${TELEGRAM_FAKE_ALIVE}"
assert_status 1
assert_read_only
# polling 这一格照旧报 true —— 这正是旧互锁被骗过去的原因，所以必须还原地打出来。
assert_contains "${RESULT}" "轮询线程 polling  True"
assert_contains "${RESULT}" "轮询成功次数 stats.polls  0"
assert_contains "${RESULT}" "累计错误次数 stats.errors  137"
assert_contains "${RESULT}" "最近一次错误 last_error  getUpdates 失败: error_code=401 Unauthorized"
assert_contains "${RESULT}" "假活判据  **命中**：polling=true 但 stats.polls=0"
assert_contains "${RESULT}" "且已失败 137 次"
# 唯一会误伤的情形（core 刚重建、首次 long polling 还没返回）必须自带零成本排除法，
# 否则这一格就是个只会喊"坏了"、不告诉人怎么分辨的闸门。
assert_contains "${RESULT}" "排除法  刚 up -d --force-recreate 过 core 的话，等一分钟重跑本脚本"
assert_contains "${RESULT}" "先查    bot 用户名那一格空 = 连启动时的 getMe 都没成功"
assert_contains "${RESULT}" "✗ 人工确认闸门通道 enabled+ready+polling  真发布已开启（模拟发布器=false），但 polling=true 但轮询假活（stats.polls=0"
assert_contains "${RESULT}" "后果  确认卡推不出去或回调收不回来"
assert_contains "${RESULT}" "兜底  工作台的「确认发布」按钮不受 Telegram 影响"
assert_contains "${RESULT}" "1 项未通过"
# 裁定条数不许漂：还是 8 项，只是其中一项从 ✓ 变成了 ✗。
assert_conclusion_verdicts 7 1
assert_no_token_shape "${RESULT}"
end_case

# 松紧仍然完全由 use_fake_publishers 决定：假活也走同一条既定裁决。
run_case "a fake-alive poller is only recorded under fake publishers" \
  TEST_TELEGRAM_JSON="${TELEGRAM_FAKE_ALIVE}"
assert_status 0
assert_contains "${RESULT}" "假活判据  **命中**"
assert_contains "${RESULT}" "✓ 人工确认闸门通道 enabled+ready+polling  模拟发布器=true：如实记录 polling=true 但轮询假活"
assert_not_contains "${RESULT}" "✗"
assert_conclusion_verdicts 8 0
end_case

# 防假红的钉子：轮询确实在推进，last_error 非空 + errors 非零也绝不许把裁定拽红。
run_case "a stale last_error never reds a poller that is actually advancing" \
  TEST_INFO_JSON="${REAL_PUBLISHERS_INFO}" TEST_TELEGRAM_JSON="${TELEGRAM_TRUE_ALIVE}"
assert_status 0
assert_read_only
assert_contains "${RESULT}" "轮询成功次数 stats.polls  9412"
assert_contains "${RESULT}" "累计错误次数 stats.errors  3"
assert_contains "${RESULT}" "最近一次错误 last_error  getUpdates 请求失败: ConnectTimeout:"
assert_contains "${RESULT}" "假活判据  未命中：stats.polls=9412 > 0，本进程确实成功轮询过"
# 判据必须把"为什么不判红"和"哪一半测不了"都写在输出里，而不是留给读者猜。
assert_contains "${RESULT}" "口径  last_error 与 stats.errors 都**只增不减**"
assert_contains "${RESULT}" "不假装能测"
assert_contains "${RESULT}" "✓ 人工确认闸门通道 enabled+ready+polling  真发布已开启（模拟发布器=false），通道 ready=true polling=true"
assert_not_contains "${RESULT}" "✗"
assert_conclusion_verdicts 8 0
end_case

# 老版本 core：两个字段整个键都不在。必须如实说"未取到"，绝不许渲染成 0 / 空，也不许判红。
run_case "an old core without stats or last_error degrades honestly instead of reading 0" \
  TEST_INFO_JSON="${REAL_PUBLISHERS_INFO}" TEST_TELEGRAM_JSON="${TELEGRAM_OLD_CORE}"
assert_status 0
assert_contains "${RESULT}" "轮询实况 stats  <未取到：这个 core 没返回 stats 字段（老版本 core）。「未取到」不是「0 次」>"
assert_contains "${RESULT}" "最近一次错误 last_error  <未取到：这个 core 没返回 last_error 字段（老版本 core）。「未取到」不是「没出过错」>"
assert_contains "${RESULT}" "假活判据  未评估：取不到 stats.polls"
assert_not_contains "${RESULT}" "轮询成功次数 stats.polls  0"
assert_not_contains "${RESULT}" "✗"
assert_conclusion_verdicts 8 0
end_case

# stats={} 是 channel_status() 在**没有 poller** 时给的（core/telegram.py:983 的 if poller
# else {}）。那是"没有轮询线程对象"，不是"轮询了 0 次"——两者不许混，也不许因此判红。
run_case "an empty stats object reads as absent, never as zero" \
  TEST_INFO_JSON="${REAL_PUBLISHERS_INFO}"
assert_status 0
assert_contains "${RESULT}" "轮询实况 stats  <无：本进程没有轮询线程对象，channel_status() 给出空 stats。「无」不是「0 次」>"
assert_contains "${RESULT}" "假活判据  未评估：取不到 stats.polls"
assert_not_contains "${RESULT}" "轮询成功次数 stats.polls  0"
assert_conclusion_verdicts 8 0
end_case

run_case "a stats object missing polls degrades per key without reading 0" \
  TEST_INFO_JSON="${REAL_PUBLISHERS_INFO}" TEST_TELEGRAM_JSON="${TELEGRAM_STATS_PARTIAL}"
assert_status 0
assert_contains "${RESULT}" "轮询成功次数 stats.polls  <缺失：这个 core 的 stats 里没有这一项>"
assert_contains "${RESULT}" "累计错误次数 stats.errors  9"
assert_contains "${RESULT}" "假活判据  未评估：取不到 stats.polls"
assert_not_contains "${RESULT}" "✗"
assert_conclusion_verdicts 8 0
end_case

run_case "a wrongly typed polls is reported as such and never judged" \
  TEST_INFO_JSON="${REAL_PUBLISHERS_INFO}" TEST_TELEGRAM_JSON="${TELEGRAM_STATS_BADTYPE}"
assert_status 0
assert_contains "${RESULT}" "轮询成功次数 stats.polls  <类型不对：str>"
assert_contains "${RESULT}" "最近一次错误 last_error  <类型不对：NoneType>"
assert_contains "${RESULT}" "假活判据  未评估：取不到 stats.polls"
assert_not_contains "${RESULT}" "✗"
assert_conclusion_verdicts 8 0
end_case

# polls=0 且 errors=0：进程刚起、首次 long polling 还没返回。证据不足以判死，必须放行。
run_case "a just-started poller with no errors yet is not judged dead" \
  TEST_INFO_JSON="${REAL_PUBLISHERS_INFO}" TEST_TELEGRAM_JSON="${TELEGRAM_JUST_STARTED}"
assert_status 0
assert_contains "${RESULT}" "轮询成功次数 stats.polls  0"
assert_contains "${RESULT}" "假活判据  未命中：stats.polls=0 但 stats.errors 为 0 或取不到"
assert_contains "${RESULT}" "证据不足以判死"
assert_not_contains "${RESULT}" "✗"
assert_conclusion_verdicts 8 0
end_case

# last_error 是 TelegramError 的文案，可能带上游 URL，而 URL 里就是 token
# （core/telegram.py:263 把整个 httpx 异常插了进去）。这一格必须打码后再渲染。
run_case "a bot token inside last_error is redacted before it is printed" \
  TEST_INFO_JSON="${REAL_PUBLISHERS_INFO}" TEST_TELEGRAM_JSON="${TELEGRAM_TOKEN_IN_ERROR}"
assert_status 1
assert_no_token_shape "${RESULT}"
assert_not_contains "${RESULT}" "AAHkQwZmXcVbNnMlKjHgFdSaPoIuYtReWq0"
assert_contains "${RESULT}" "最近一次错误 last_error  getUpdates 请求失败: ConnectError:"
assert_contains "${RESULT}" "bot<已打码的 bot token>/getUpdates"
assert_conclusion_verdicts 7 1
end_case

# 已经 polling=false 的通道不必再问假活：那道判据只用来识破"活着却没在动"。
run_case "a dead poller is not re-litigated as fake-alive" \
  TEST_INFO_JSON="${REAL_PUBLISHERS_INFO}" TEST_TELEGRAM_JSON="${TELEGRAM_NO_POLLING}"
assert_status 1
assert_contains "${RESULT}" "假活判据  未评估：polling 已经是 false"
assert_contains "${RESULT}" "✗ 人工确认闸门通道 enabled+ready+polling  真发布已开启（模拟发布器=false），但 ready=true 但 polling=false"
assert_conclusion_verdicts 7 1
end_case

run_case "telegram probe failure is recorded but not fatal under fake publishers" \
  TEST_TELEGRAM_CURL_STATUS=7
assert_status 0
assert_contains "${RESULT}" "<无法获取 /api/v1/system/telegram>"
assert_contains "${RESULT}" "✓ 人工确认闸门通道 enabled+ready+polling  模拟发布器=true：如实记录 无法获取"
end_case

run_case "telegram probe failure fails under real publishers" \
  TEST_INFO_JSON="${REAL_PUBLISHERS_INFO}" TEST_TELEGRAM_CURL_STATUS=7
assert_status 1
assert_contains "${RESULT}" "<无法获取 /api/v1/system/telegram>"
assert_contains "${RESULT}" "✗ 人工确认闸门通道 enabled+ready+polling  真发布已开启（模拟发布器=false），但 无法获取"
end_case

run_case "unparsable telegram payload is tolerated under fake publishers" \
  TEST_TELEGRAM_JSON='not json'
assert_status 0
assert_contains "${RESULT}" "确认通道  <JSON 解析失败>"
assert_contains "${RESULT}" "✓ 人工确认闸门通道 enabled+ready+polling  模拟发布器=true：如实记录 无法解析"
end_case

run_case "unparsable telegram payload fails under real publishers" \
  TEST_INFO_JSON="${REAL_PUBLISHERS_INFO}" TEST_TELEGRAM_JSON='not json'
assert_status 1
assert_contains "${RESULT}" "✗ 人工确认闸门通道 enabled+ready+polling  真发布已开启（模拟发布器=false），但 无法解析"
end_case

run_case "failed telegram envelope fails under real publishers" \
  TEST_INFO_JSON="${REAL_PUBLISHERS_INFO}" TEST_TELEGRAM_JSON='{"ok":false,"error":"boom"}'
assert_status 1
assert_contains "${RESULT}" "返回失败外壳"
assert_contains "${RESULT}" "✗ 人工确认闸门通道 enabled+ready+polling"
end_case

# use_fake_publishers 取不到时不能给绿灯：无法证明"什么都不会真发"，按真发布从严。
run_case "unknown fake publishers flag is judged strictly" \
  TEST_INFO_CURL_STATUS=7 TEST_TELEGRAM_JSON="${TELEGRAM_DEAD}"
assert_status 1
assert_contains "${RESULT}" "模拟发布器状态取不到（use_fake_publishers=<未知>），按真发布从严裁定"
assert_contains "${RESULT}" "✗ 人工确认闸门通道 enabled+ready+polling"
end_case

run_case "port gate failure marks the confirm channel gate unexecuted" TEST_PORT='0.0.0.0:8000'
assert_status 1
assert_contains "${RESULT}" "✗ 人工确认闸门通道 enabled+ready+polling  未执行（端口门禁未通过）"
assert_not_contains "${LOG}" "curl <"
end_case

# 不短路：确认闸门通道失败与其他失败项必须一起出现在结论里。
run_case "confirm channel failure never short circuits the other gates" \
  TEST_INFO_JSON="${REAL_PUBLISHERS_INFO}" TEST_TELEGRAM_JSON="${TELEGRAM_DEAD}" \
  TEST_DIRTY=1 TEST_LOGS_MODE=conflict
assert_status 1
assert_read_only
assert_contains "${RESULT}" "✗ 工作树干净  存在未提交改动"
assert_contains "${RESULT}" "✗ 人工确认闸门通道 enabled+ready+polling  真发布已开启（模拟发布器=false），但 ready=false"
assert_contains "${RESULT}" "✗ Telegram error_code=409 计数为 0  实测 2 行"
assert_contains "${RESULT}" "3 项未通过"
assert_contains "${LOG}" "docker <compose> <logs> <--tail> <2000> <core>"
end_case

# ------------------------------------------- 待人点的确认卡条数（docs/RISKS.md §8.5 第 0 步）
# §8.5 给 SW_UI_TOKEN 变更定了一条前置：先确认没有待人点的确认卡。生产 .env 里
# SW_TELEGRAM_SIGNING_SECRET 为空时签名密钥回落到 SW_UI_TOKEN（core/telegram.py::load_config），
# 换 token 等于换密钥，已推出去还没人点的卡按下去会 bad_signature。可 scripts/ops 下原本
# 没有任何脚本能回答"现在到底有没有"——system_info 里一个队列计数都没有，本段上面那几格
# 只有通道状态。下面这些用例钉住"把那条前置变成可执行的读数"这件事的四类后果：
#   ① 有卡 / 零卡都如实报，并且口径（上界）必须写在输出里；
#   ② 只报计数，内容标题与账号名一个字节都不许出现；
#   ③ 401 / 404 / 传输失败 / 解析失败 / 字段缺失，一律说"未取到"，**绝不渲染成 0 条**；
#   ④ 它不产生裁定：有人等着点不是故障，「核验结论」的条数一条都不许因此变。
#
# 负载形状照着 core/api/dashboard.py 的响应模型写：Envelope{ok,data,error} 裹
# DashboardOut{generated_at,window_days,counters,budget,platforms,attention,events}。
# events[].title 与 attention[].name 是真端点**真会返回**的字段，这里塞进哨兵串，
# 用来反过来证明脚本没有把它们打出来。
DASHBOARD_AWAITING='{"ok":true,"data":{"generated_at":"2026-08-22T02:00:00Z","window_days":1,"counters":{"pending_review":1,"published_today":2,"published_7d":9,"failed":0,"dead_letter":0,"scheduled":5,"suspended":0,"awaiting_confirm":3,"rendering":0,"accounts_needing_relogin":1,"accounts_degraded":0,"accounts_suspended":0},"budget":{"token":{"used":1.5,"limit":10.0,"remaining":8.5}},"platforms":[{"platform":"xhs","accounts":1,"ok":1,"degraded":0,"needs_relogin":0,"banned":0,"suspended":0,"pending_review":1,"scheduled":5,"published":9,"used_today":2,"daily_limit":3}],"attention":[{"account_id":"acc-1","name":"SENTINEL_ACCOUNT_NAME","platform":"xhs","status":"needs_relogin","suspended":2}],"events":[{"kind":"review_log","at":"2026-08-22T01:00:00Z","actor":"operator","action":"approve","item_id":"itm-1","title":"SENTINEL_ITEM_TITLE","account_id":"acc-1","detail":"SENTINEL_EVENT_DETAIL","url":null}]},"error":null}'
# 老一点的 core：有 counters，但还没有 awaiting_confirm 这个字段。
DASHBOARD_NO_FIELD='{"ok":true,"data":{"generated_at":"2026-08-22T02:00:00Z","window_days":1,"counters":{"pending_review":0,"published_today":0,"published_7d":0,"failed":0,"dead_letter":0,"scheduled":0,"suspended":0,"rendering":0,"accounts_needing_relogin":0,"accounts_degraded":0,"accounts_suspended":0},"budget":{},"platforms":[],"attention":[],"events":[]},"error":null}'
# 字段在但类型不对（契约漂移）：不许拿 0 兜底，也不许把它当数字打出来。
DASHBOARD_BAD_TYPE='{"ok":true,"data":{"generated_at":"2026-08-22T02:00:00Z","window_days":1,"counters":{"awaiting_confirm":"3"},"budget":{},"platforms":[],"attention":[],"events":[]},"error":null}'

# ① 零条：能明确说"0 条"，而且口径必须一起打出来。
run_case "zero pending confirm cards is reported as an explicit zero"
assert_status 0
assert_read_only
assert_contains "${LOG}" "curl <-q> <-fsS> <--max-time> <20> <-w> <\n%{http_code}> <--config> <-> <http://127.0.0.1:8000/api/v1/dashboard?days=1>"
assert_contains "${RESULT}" "待人点的确认卡  0 条"
assert_contains "${RESULT}" "口径  counters.awaiting_confirm（core/api/dashboard.py::_awaiting_confirm）"
assert_contains "${RESULT}" "裁定  待人点的确认卡 0 条：如实记录，本身不构成失败项"
assert_not_contains "${RESULT}" "未取到"
# 0 条时不该出现那段"换 token 会搞坏它们"的提醒——没有卡就没有这回事。
assert_not_contains "${RESULT}" "提醒  改 SW_UI_TOKEN 前先看这一格"
assert_not_contains "${RESULT}" "✗"
end_case

# ① 有卡：报条数，并且必须把"这是上界"这件事写清楚，不许让人读成"这么多条会失效"。
run_case "pending confirm cards are counted and the caliber is stated as an upper bound" \
  TEST_DASHBOARD_JSON="${DASHBOARD_AWAITING}"
assert_status 0
assert_read_only
assert_contains "${RESULT}" "待人点的确认卡  3 条"
assert_contains "${RESULT}" "status=scheduled"
assert_contains "${RESULT}" "confirm_pushed_at，所以对「换签名密钥会搞坏几条」而言这是个**上界**"
assert_contains "${RESULT}" "裁定  待人点的确认卡 3 条：如实记录，本身不构成失败项"
assert_contains "${RESULT}" "提醒  改 SW_UI_TOKEN 前先看这一格（docs/RISKS.md §8.5 第 0 步）"
assert_contains "${RESULT}" "这 3 条里**已经推出卡**的"
assert_contains "${RESULT}" "bad_signature"
# 上界不许被写成精确值：这句话在任何路径下都不许出现。
assert_not_contains "${RESULT}" "3 条会失效"
assert_not_contains "${RESULT}" "未取到"
# 有卡等着人点不是故障：不许因此多出任何失败项。
assert_not_contains "${RESULT}" "✗"
assert_conclusion_verdicts 8 0
end_case

# ② 只报计数：dashboard 的响应真的带 events[].title / attention[].name，
# 运维终端不是内容审阅面，它们一个字节都不许出现在输出里。
run_case "the pending card count never leaks titles or account names" \
  TEST_DASHBOARD_JSON="${DASHBOARD_AWAITING}"
assert_status 0
assert_contains "${RESULT}" "待人点的确认卡  3 条"
assert_not_contains "${RESULT}" "SENTINEL_ITEM_TITLE"
assert_not_contains "${RESULT}" "SENTINEL_ACCOUNT_NAME"
assert_not_contains "${RESULT}" "SENTINEL_EVENT_DETAIL"
assert_no_token_shape "${RESULT}"
end_case

# ② 真发布 + 通道健康 + 有待确认卡：最容易被误做成失败项的组合，必须仍然全绿。
run_case "pending cards never turn a healthy deployment into a failure" \
  TEST_INFO_JSON="${REAL_PUBLISHERS_INFO}" TEST_DASHBOARD_JSON="${DASHBOARD_AWAITING}"
assert_status 0
assert_contains "${RESULT}" "待人点的确认卡  3 条"
assert_contains "${RESULT}" "✓ 人工确认闸门通道 enabled+ready+polling  真发布已开启（模拟发布器=false），通道 ready=true polling=true"
assert_not_contains "${RESULT}" "✗"
assert_conclusion_verdicts 8 0
end_case

# ③ 401：说清是未授权，并复用同一份可行动提示；**绝不能**变成"0 条"。
run_case "a dashboard 401 says not-obtained instead of zero" TEST_DASHBOARD_HTTP_CODE=401
assert_status 0
assert_contains "${RESULT}" "待人点的确认卡  <未取到：GET /api/v1/dashboard 返回 401 未授权。「未取到」不是「0 条」>"
assert_contains "${RESULT}" "core 已启用 SW_UI_TOKEN 鉴权"
assert_not_contains "${RESULT}" "待人点的确认卡  0 条"
end_case

# ③ 生产真开了 token 而本机没配：三条 /api/v1/* 探针一起 401，计数这一格也必须如实降级。
# （UI_TOKEN 在下面的 token 小节才定义，这里自带一个，免得两节的顺序互相绑死。）
DASHBOARD_SECTION_TOKEN='TESTTOKEN_dashboard-section'
run_case "auth-enabled core without a local token cannot obtain the pending card count" \
  TEST_REQUIRE_TOKEN="${DASHBOARD_SECTION_TOKEN}"
assert_status 1
assert_contains "${RESULT}" "待人点的确认卡  <未取到：GET /api/v1/dashboard 返回 401 未授权。「未取到」不是「0 条」>"
assert_not_contains "${RESULT}" "待人点的确认卡  0 条"
end_case

# ③ 端点不存在（老版本 core 还没有 /api/v1/dashboard）：点名说是这一种，不许混进"连不上"。
run_case "a dashboard 404 names the missing endpoint instead of reporting zero" TEST_DASHBOARD_HTTP_CODE=404
assert_status 0
assert_contains "${RESULT}" "待人点的确认卡  <未取到：GET /api/v1/dashboard 返回 404，这版 core 没有这个端点。「未取到」不是「0 条」>"
assert_not_contains "${RESULT}" "待人点的确认卡  0 条"
assert_not_contains "${RESULT}" "core 已启用 SW_UI_TOKEN 鉴权"
end_case

# ③ 传输失败：走通用文案，带上 curl 退出码，绝不误报成鉴权或"没有卡"。
run_case "a dashboard transport failure never claims zero and never claims auth" TEST_DASHBOARD_CURL_STATUS=7
assert_status 0
assert_contains "${RESULT}" "待人点的确认卡  <未取到：GET /api/v1/dashboard 取不到，curl 退出码 7、HTTP 000。「未取到」不是「0 条」>"
assert_not_contains "${RESULT}" "待人点的确认卡  0 条"
assert_not_contains "${RESULT}" "core 已启用 SW_UI_TOKEN 鉴权"
end_case

# ③ 5xx 也走通用文案，且必须能和 401 分开。
run_case "a dashboard 503 is not mistaken for an auth problem" TEST_DASHBOARD_HTTP_CODE=503
assert_status 0
assert_contains "${RESULT}" "curl 退出码 22、HTTP 503"
assert_not_contains "${RESULT}" "待人点的确认卡  0 条"
end_case

# ③ 解析失败：响应不是 JSON。
run_case "an unparsable dashboard payload says not-obtained instead of zero" TEST_DASHBOARD_JSON='not json'
assert_status 0
assert_contains "${RESULT}" "待人点的确认卡  <未取到：/api/v1/dashboard 的响应不是合法 JSON。「未取到」不是「0 条」>"
assert_not_contains "${RESULT}" "待人点的确认卡  0 条"
end_case

# ③ 失败外壳：ok=false 时 data 是 null，不许把它读成"没有卡"。
run_case "a failed dashboard envelope says not-obtained instead of zero" \
  TEST_DASHBOARD_JSON='{"ok":false,"data":null,"error":{"code":"internal","message":"boom","detail":null}}'
assert_status 0
assert_contains "${RESULT}" "待人点的确认卡  <未取到：/api/v1/dashboard 返回失败外壳。「未取到」不是「0 条」>"
assert_not_contains "${RESULT}" "待人点的确认卡  0 条"
end_case

# ③ 字段缺失：老版本 core 有 counters 但没有 awaiting_confirm。这正是"取不到"最像"0 条"
# 的一种——payload 合法、外壳成功、counters 也在，只是那个键不存在。
run_case "a counters block without awaiting_confirm says not-obtained instead of zero" \
  TEST_DASHBOARD_JSON="${DASHBOARD_NO_FIELD}"
assert_status 0
assert_contains "${RESULT}" "待人点的确认卡  <未取到：响应里没有 counters.awaiting_confirm 这个非负整数（字段缺失或类型不对）。「未取到」不是「0 条」>"
assert_not_contains "${RESULT}" "待人点的确认卡  0 条"
end_case

# ③ 类型不对（契约漂移）：字符串 "3" 不是计数，不许被打成 3 条，也不许兜底成 0 条。
run_case "a non integer awaiting_confirm is refused rather than coerced" \
  TEST_DASHBOARD_JSON="${DASHBOARD_BAD_TYPE}"
assert_status 0
assert_contains "${RESULT}" "待人点的确认卡  <未取到：响应里没有 counters.awaiting_confirm 这个非负整数（字段缺失或类型不对）。「未取到」不是「0 条」>"
assert_not_contains "${RESULT}" "待人点的确认卡  3 条"
assert_not_contains "${RESULT}" "待人点的确认卡  0 条"
end_case

# ④ 端口门禁没过时连探测目标都没有：这一格也必须显式说"未取到"，而不是安静消失。
run_case "a failed port gate leaves the pending card count explicitly unobtained" TEST_PORT='0.0.0.0:8000'
assert_status 1
assert_contains "${RESULT}" "待人点的确认卡  <未取到：端口门禁未通过，没有可信的 loopback 探测目标。「未取到」不是「0 条」>"
assert_not_contains "${RESULT}" "待人点的确认卡  0 条"
assert_not_contains "${LOG}" "curl <"
end_case

# ④ 无论取到还是取不到，都不许往「核验结论」里加或减裁定行。
run_case "the pending card count adds no verdict when it cannot be obtained" TEST_DASHBOARD_CURL_STATUS=7
assert_status 0
assert_conclusion_verdicts 8 0
assert_contains "${RESULT}" "全部核验项通过"
end_case

# ---------------------------------------------------------------- 409 计数
# Regression pin: uvicorn access lines print the client ephemeral port, so a naive
# bare-409 match reports a false conflict on ordinary healthy traffic.
run_case "ephemeral port 44092 is not a telegram 409" TEST_LOGS_MODE=ephemeral-port
assert_status 0
assert_read_only
assert_contains "${RESULT}" "近 2000 行中 error_code=409 的日志行数  0"
assert_contains "${RESULT}" "✓ Telegram error_code=409 计数为 0"
assert_contains "${RESULT}" "✓ 生产部署核验通过"
end_case

run_case "real error_code 409 fails" TEST_LOGS_MODE=conflict
assert_status 1
assert_contains "${RESULT}" "近 2000 行中 error_code=409 的日志行数  2"
assert_contains "${RESULT}" "✗ Telegram error_code=409 计数为 0  实测 2 行"
assert_contains "${RESULT}" "两套部署抢同一个 bot token"
end_case

run_case "unreadable logs fail" TEST_LOGS_FAIL=1
assert_status 1
assert_contains "${RESULT}" "✗ Telegram error_code=409 计数为 0  无法读取 core 日志"
end_case

# Raw matching log lines are never echoed: Telegram error text can carry the bot token.
run_case "conflict lines are never echoed" TEST_LOGS_MODE=conflict
assert_status 1
assert_not_contains "${RESULT}" "getUpdates 失败: error_code=409 Conflict"
end_case

# ---------------------------------------------------------------- 多失败项
run_case "all failures are listed without short circuiting" \
  TEST_DIRTY=1 TEST_HEAD_SHA="${OTHER}" TEST_LOGS_MODE=conflict \
  TEST_INFO_JSON='{"ok":true,"data":{"version":"0.1.0","env":"dev","time":"t","timezone":"Asia/Shanghai","scheduler_enabled":true,"generate_enabled":true,"use_fake_publishers":true,"auth_required":false,"publishers":[]}}' \
  --sha "${HEAD_SHA}"
assert_status 1
assert_read_only
assert_contains "${RESULT}" "✗ 工作树干净  存在未提交改动"
assert_contains "${RESULT}" "✗ HEAD 等于期望 SHA  HEAD=${OTHER}，期望=${HEAD_SHA}"
assert_contains "${RESULT}" "✗ 运行环境 env=prod  实际 env 不是 prod"
assert_contains "${RESULT}" "✗ Telegram error_code=409 计数为 0  实测 2 行"
assert_contains "${RESULT}" "4 项未通过"
# Later sections still ran even though the first gate already failed.
assert_contains "${LOG}" "docker <compose> <logs> <--tail> <2000> <core>"
assert_contains "${LOG}" "curl <-q> <-fsS> <--max-time> <10> <-w> <\n%{http_code}> <--config> <-> <http://127.0.0.1:8000/health>"
end_case

# ---------------------------------------------------------------- preflight
run_case "preflight is opt-in and absent by default"
assert_status 0
assert_not_contains "${LOG}" "preflight.py"
assert_contains "${RESULT}" "preflight 未执行（需显式 --preflight；"
end_case

run_case "preflight runs when requested" --preflight
assert_status 0
assert_contains "${LOG}" "docker <compose> <exec> <-T> <core> <python3> <scripts/preflight.py>"
assert_contains "${RESULT}" "preflight 退出码  0"
assert_contains "${RESULT}" "✓ 容器内 preflight（--preflight）"
end_case

run_case "preflight failure is reported truthfully" TEST_PREFLIGHT_STATUS=1 --preflight
assert_status 1
assert_contains "${RESULT}" "preflight 退出码  1"
assert_contains "${RESULT}" "✗ 容器内 preflight（--preflight）  退出码 1"
end_case

# ----------------------------------------- preflight 之后脚本必须继续执行（阻断级回归）
# 生产实证：内层 bash 的**脚本正文就是它自己的 stdin**，而 `docker compose exec -T`
# 会转发 stdin。preflight 那一步少了 `</dev/null`，就会把后面的「核验结论」段、
# 失败项汇总和 exit 1 判定整个吞掉——于是 `--preflight`（最严格的模式）反而
# 不可能失败：工作树脏、HEAD 对不上、确认闸门通道死了，一律报"全部通过"。
# 下面这些用例断言的是**后果**，不是实现：把 `</dev/null` 拿掉，它们必须变红。

# 防回归哨兵：直接断言"preflight 之后的脚本仍在执行"这件事本身。
run_case "preflight does not swallow the rest of the remote script" --preflight
assert_status 0
assert_contains "${RESULT}" "preflight 退出码  0"
assert_contains "${RESULT}" "preflight 之后脚本仍在执行"
assert_contains "${RESULT}" "核验结论"
assert_contains "${RESULT}" "全部核验项通过"
assert_contains "${RESULT}" "✓ 容器内 preflight（--preflight）"
assert_contains "${RESULT}" "✓ Telegram error_code=409 计数为 0"
assert_conclusion_verdicts 9 0
end_case

run_case "preflight with a dirty worktree still fails with a full conclusion" TEST_DIRTY=1 --preflight
assert_status 1
assert_contains "${RESULT}" "preflight 之后脚本仍在执行"
assert_contains "${RESULT}" "核验结论"
assert_contains "${RESULT}" "✗ 工作树干净  存在未提交改动"
assert_contains "${RESULT}" "1 项未通过"
assert_contains "${RESULT}" "✗ 生产部署核验失败"
assert_not_contains "${RESULT}" "全部核验项通过"
assert_conclusion_verdicts 8 1
end_case

run_case "preflight with a sha mismatch still fails with a full conclusion" \
  TEST_HEAD_SHA="${OTHER}" --sha "${HEAD_SHA}" --preflight
assert_status 1
assert_contains "${RESULT}" "preflight 之后脚本仍在执行"
assert_contains "${RESULT}" "核验结论"
assert_contains "${RESULT}" "✗ HEAD 等于期望 SHA  HEAD=${OTHER}，期望=${HEAD_SHA}"
assert_contains "${RESULT}" "1 项未通过"
# 带 --sha 时多出「HEAD 等于期望 SHA」这一格，所以裁定行比默认路径多一条。
assert_conclusion_verdicts 9 1
end_case

# R1 载体死掉 + --preflight：最严格模式下最该报警的组合，绝不能被吞成"全部通过"。
run_case "preflight with a dead confirm channel under real publishers still fails" \
  TEST_INFO_JSON="${REAL_PUBLISHERS_INFO}" TEST_TELEGRAM_JSON="${TELEGRAM_DEAD}" --preflight
assert_status 1
assert_contains "${RESULT}" "preflight 之后脚本仍在执行"
assert_contains "${RESULT}" "核验结论"
assert_contains "${RESULT}" "✗ 人工确认闸门通道 enabled+ready+polling  真发布已开启（模拟发布器=false），但 ready=false"
assert_contains "${RESULT}" "✗ 生产部署核验失败"
assert_conclusion_verdicts 8 1
end_case

# 不短路：preflight 自身失败与其他失败项必须一起出现在结论里。
run_case "preflight failure is listed together with the other failures" \
  TEST_PREFLIGHT_STATUS=1 TEST_DIRTY=1 TEST_LOGS_MODE=conflict --preflight
assert_status 1
assert_contains "${RESULT}" "preflight 之后脚本仍在执行"
assert_contains "${RESULT}" "✗ 工作树干净  存在未提交改动"
assert_contains "${RESULT}" "✗ Telegram error_code=409 计数为 0  实测 2 行"
assert_contains "${RESULT}" "✗ 容器内 preflight（--preflight）  退出码 1"
assert_contains "${RESULT}" "3 项未通过"
assert_conclusion_verdicts 6 3
end_case

# 默认（不带 --preflight）路径的结论段必须原样完整，作为上面计数的对照组。
run_case "default run prints the full conclusion section"
assert_status 0
assert_contains "${RESULT}" "核验结论"
assert_contains "${RESULT}" "全部核验项通过"
assert_not_contains "${RESULT}" "preflight 之后脚本仍在执行"
assert_conclusion_verdicts 8 0
end_case

# ------------------------------------------------- 工作台 API token（docs/RISKS.md 第 8 条）
# 生产 .env 一旦配上非空 SW_UI_TOKEN，/api/v1/* 全部要求 Authorization: Bearer（除
# /auth/login）；/health 挂在 app 根上不过守卫。§8.4 记录的实施前置就是：探针必须能带上
# 这个头，而且 token 一个字符都不能进 argv（生产是合租机器，/proc/*/cmdline 世界可读）。
# 下面这些用例分别钉住：送到了 / 没泄漏 / 没配时行为不变 / 配错时给的是可行动提示。
UI_TOKEN='TESTTOKEN_verify-A1b2+/=.:@'

# 假件把每一次调用的完整 argv 都落进 TEST_LOG（ssh、bash、git、docker、curl 全覆盖，
# 还包括 ssh 真正发出的那一整条远端命令字符串）。token 明文在这份记录里必须是 0 次。
assert_token_absent_from_argv() {
  local token="$1" hits
  hits="$(grep -F -c -- "${token}" <<<"${LOG}" || true)"
  if [[ "${hits}" -ne 0 ]]; then
    fail_assertion "token 明文在 argv 记录里出现了 ${hits} 次，必须是 0；log: ${LOG}"
  fi
}

run_case "ui token reaches every probe header and never appears in argv" SW_OPS_UI_TOKEN="${UI_TOKEN}"
assert_status 0
# ① 确实送到了：四条探针每一条都从 --config - 的配置流里解析出了同一个头。
assert_contains "${AUTH_LOG}" "url <http://127.0.0.1:8000/health> header <Authorization: Bearer ${UI_TOKEN}>"
assert_contains "${AUTH_LOG}" "url <http://127.0.0.1:8000/api/v1/system/info> header <Authorization: Bearer ${UI_TOKEN}>"
assert_contains "${AUTH_LOG}" "url <http://127.0.0.1:8000/api/v1/system/telegram> header <Authorization: Bearer ${UI_TOKEN}>"
assert_contains "${AUTH_LOG}" "url <http://127.0.0.1:8000/api/v1/dashboard?days=1> header <Authorization: Bearer ${UI_TOKEN}>"
# ② 确实没泄漏：argv 记录里 0 次，脚本输出里 0 次。
assert_token_absent_from_argv "${UI_TOKEN}"
assert_not_contains "${RESULT}" "${UI_TOKEN}"
# ③ 只报来源、不报值，也不报长度。
assert_contains "${RESULT}" "已加载工作台 API token（来源：环境变量 SW_OPS_UI_TOKEN）"
assert_contains "${RESULT}" "全部核验项通过"
end_case

# 未配置 token：配置流照样存在（代码路径同构），但里面一个头都没有。
run_case "without a ui token the config stream carries no header"
assert_status 0
assert_contains "${AUTH_LOG}" "url <http://127.0.0.1:8000/api/v1/system/info> header <>"
assert_not_contains "${AUTH_LOG}" "Authorization"
assert_not_contains "${RESULT}" "已加载工作台 API token"
end_case

# 已导出但为空 = 显式声明"本次不带 token"，用来复现未鉴权路径；不回落去读凭据文件。
run_case "an exported but empty token means explicitly no token" SW_OPS_UI_TOKEN=
assert_status 0
assert_not_contains "${AUTH_LOG}" "Authorization"
assert_not_contains "${RESULT}" "已加载工作台 API token"
end_case

# 凭据文件兜底（R5 既定存放约定）：环境变量没导出时才读，只认顶格的 sw_ui_token 键。
CRED_DIR="${TMP}/home dir/.dsh-sw"
CRED_FILE="${CRED_DIR}/.credentials.yaml"
mkdir -p "${CRED_DIR}"
CRED_TOKEN='TESTTOKEN_from-credentials-file'
printf 'dsh_api_key: something-else\nsw_ui_token: "%s"\n' "${CRED_TOKEN}" >"${CRED_FILE}"
chmod 600 "${CRED_FILE}"

run_case "credentials file supplies the token when the env var is unset"
assert_status 0
assert_contains "${AUTH_LOG}" "header <Authorization: Bearer ${CRED_TOKEN}>"
assert_token_absent_from_argv "${CRED_TOKEN}"
assert_contains "${RESULT}" "已加载工作台 API token（来源：${CRED_FILE} 的 sw_ui_token 键）"
end_case

# 优先级：环境变量赢。显式意图优先于长期约定。
run_case "the env var wins over the credentials file" SW_OPS_UI_TOKEN="${UI_TOKEN}"
assert_status 0
assert_contains "${AUTH_LOG}" "header <Authorization: Bearer ${UI_TOKEN}>"
assert_not_contains "${AUTH_LOG}" "${CRED_TOKEN}"
assert_contains "${RESULT}" "来源：环境变量 SW_OPS_UI_TOKEN"
end_case

# 键不存在时当没配，绝不猜——极窄解析的既定取舍。
printf 'dsh_api_key: something-else\n' >"${CRED_FILE}"
run_case "a credentials file without the key falls back to no token"
assert_status 0
assert_not_contains "${AUTH_LOG}" "Authorization"
assert_not_contains "${RESULT}" "已加载工作台 API token"
end_case
rm -f "${CRED_FILE}"

# 字符集校验必须发生在**本机**、在 SSH 之前，而且报错里绝不回显 token 本身。
# curl 配置的 header = "..." 用 \ 转义、用 " 定界，含这两个字符的 token 会静默破坏请求语法。
run_case "a token containing a double quote is rejected before ssh" SW_OPS_UI_TOKEN='TESTTOKEN_bad"quote'
assert_rejected_before_ssh
assert_contains "${RESULT}" "工作台 API token 含有不被允许的字符"
# 报错必须按**白名单**口径说话：列一份"不允许"清单会让 token 里带 % 的人挨个排除后得出
# "我这个应该合法"，再卡在一条看不懂的报错上。
assert_contains "${RESULT}" "这是**白名单**：只允许 A-Z a-z 0-9 以及 . _ - + / = : @；**其余字符一律拒绝**"
# 不许再出现"空白会截断配置行"这句错话：实测空格在 curl 双引号参数里能原样通过，
# 排除空白的真实理由是它不是合法的凭据字符（RFC 6750 的 b64token）。
assert_not_contains "${RESULT}" "空白会截断配置行"
assert_contains "${RESULT}" "空白与控制字符不是合法的凭据字符"
assert_contains "${RESULT}" "curl 在双引号值里把 \\ 当转义、把 \" 当定界符，带上它们会**静默**发出一个语法被破坏的头"
assert_contains "${RESULT}" "Django get_random_secret_key()"
assert_not_contains "${RESULT}" 'TESTTOKEN_bad"quote'
end_case

run_case "a token containing a backslash is rejected before ssh" SW_OPS_UI_TOKEN='TESTTOKEN_bad\back'
assert_rejected_before_ssh
assert_contains "${RESULT}" "工作台 API token 含有不被允许的字符"
assert_not_contains "${RESULT}" 'TESTTOKEN_bad\back'
end_case

run_case "a token containing a space is rejected before ssh" SW_OPS_UI_TOKEN='TESTTOKEN bad space'
assert_rejected_before_ssh
assert_contains "${RESULT}" "工作台 API token 含有不被允许的字符"
end_case

# ---------------------------------------------------------------- 401 是可行动的
# §8.4 点名的误判风险：把"本机没配 token"读成部署故障或运行时故障。
run_case "an info probe 401 gives a root-cause hint instead of a generic failure" TEST_INFO_HTTP_CODE=401
assert_status 1
assert_contains "${RESULT}" "运行时信息  <GET /api/v1/system/info 返回 401 未授权>"
assert_contains "${RESULT}" "core 已启用 SW_UI_TOKEN 鉴权"
assert_contains "${RESULT}" "这不是部署故障，也不是 core 运行时故障——core 正常应答了 401"
assert_contains "${RESULT}" "export SW_OPS_UI_TOKEN=<生产 .env 里 SW_UI_TOKEN 的同一个值>"
# shellcheck disable=SC2088  # 这是要在输出里逐字匹配的文案，不是路径，不需要展开
assert_contains "${RESULT}" "~/.dsh-sw/.credentials.yaml"
assert_contains "${RESULT}" "已经配了还是 401 = 值不匹配"
assert_contains "${RESULT}" "✗ 运行环境 env=prod  /api/v1/system/info 返回 401（core 已启用 SW_UI_TOKEN，本机未提供或不匹配）"
assert_not_contains "${RESULT}" "✗ 运行环境 env=prod  无法获取 /api/v1/system/info"
end_case

# 401 与"连不上"必须分得开：传输失败仍然走通用文案，绝不误报成鉴权问题。
run_case "a transport failure never claims an auth problem" TEST_INFO_CURL_STATUS=7
assert_status 1
assert_contains "${RESULT}" "✗ 运行环境 env=prod  无法获取 /api/v1/system/info"
assert_not_contains "${RESULT}" "core 已启用 SW_UI_TOKEN 鉴权"
end_case

# 401 与其它 HTTP 失败也必须分得开：503 走通用文案。
run_case "a 503 never claims an auth problem" TEST_INFO_HTTP_CODE=503
assert_status 1
assert_contains "${RESULT}" "运行时信息  <无法获取 /api/v1/system/info>"
assert_not_contains "${RESULT}" "core 已启用 SW_UI_TOKEN 鉴权"
end_case

# 健康探针的 HTTP 层失败：-f 让 curl 退 22，文案与改造前逐字一致。
run_case "an http 503 on health reports curl exit 22" TEST_HEALTH_HTTP_CODE=503
assert_status 1
assert_contains "${RESULT}" "✗ 健康探针 GET /health 200  curl 退出码 22"
end_case

# 生产真的开了 token 而本机没配：这正是 §8.4 描述的那一刻，端到端跑一遍。
run_case "auth-enabled core without a local token fails with the auth hint, not a fake deploy failure" \
  TEST_REQUIRE_TOKEN="${UI_TOKEN}"
assert_status 1
assert_contains "${RESULT}" "✓ 健康探针 GET /health 200"
assert_contains "${RESULT}" "✗ 运行环境 env=prod  /api/v1/system/info 返回 401（core 已启用 SW_UI_TOKEN，本机未提供或不匹配）"
assert_contains "${RESULT}" "确认通道  <GET /api/v1/system/telegram 返回 401 未授权>"
assert_contains "${RESULT}" "处置  同上一条 401 的说明。"
assert_contains "${RESULT}" "✗ 人工确认闸门通道 enabled+ready+polling  模拟发布器状态取不到"
assert_contains "${RESULT}" "/api/v1/system/telegram 返回 401（core 已启用 SW_UI_TOKEN，本机未提供或不匹配）"
# 结论段照旧完整：401 不该让任何一道门禁消失，也不该短路。
assert_contains "${RESULT}" "✓ 工作树干净"
assert_contains "${RESULT}" "✓ Telegram error_code=409 计数为 0"
assert_conclusion_verdicts 6 2
end_case

# 同一台 core，本机配对了 token：全部通过，且 token 依然不进 argv。
run_case "auth-enabled core with the matching token passes every gate" \
  TEST_REQUIRE_TOKEN="${UI_TOKEN}" SW_OPS_UI_TOKEN="${UI_TOKEN}"
assert_status 0
assert_contains "${RESULT}" "全部核验项通过"
assert_contains "${RESULT}" "✓ 生产部署核验通过"
assert_conclusion_verdicts 8 0
assert_token_absent_from_argv "${UI_TOKEN}"
assert_contains "${AUTH_LOG}" "url <http://127.0.0.1:8000/api/v1/system/telegram> header <Authorization: Bearer ${UI_TOKEN}>"
end_case

# 配了但值不对：仍然 401，提示必须把"值不匹配"这条路指出来。
run_case "auth-enabled core with a mismatched token still 401s and says so" \
  TEST_REQUIRE_TOKEN="${UI_TOKEN}" SW_OPS_UI_TOKEN='TESTTOKEN_wrong-value'
assert_status 1
assert_contains "${RESULT}" "已加载工作台 API token（来源：环境变量 SW_OPS_UI_TOKEN）"
assert_contains "${RESULT}" "已经配了还是 401 = 值不匹配"
assert_token_absent_from_argv 'TESTTOKEN_wrong-value'
end_case

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
run_case "bash -x never prints the token (env var source)" SW_OPS_UI_TOKEN="${XTRACE_TOKEN}"
assert_status 0
assert_token_absent_from_argv "${XTRACE_TOKEN}"
# RESULT 这次含整份 xtrace 输出（run_case 收 stdout+stderr），token 明文必须 0 次。
assert_not_contains "${RESULT}" "${XTRACE_TOKEN}"
# 反面自检：确认 -x 真的开着（否则这条用例是空转）。
assert_contains "${RESULT}" "+ sw_ops_load_ui_token"
# 而 token 确实送到了——防护不能是"顺手把功能也关了"。
assert_contains "${AUTH_LOG}" "header <Authorization: Bearer ${XTRACE_TOKEN}>"
end_case

printf 'dsh_api_key: something-else\nsw_ui_token: %s\n' "${XTRACE_TOKEN}" >"${CRED_FILE}"
chmod 600 "${CRED_FILE}"
run_case "bash -x never prints the token (credentials file source)"
assert_status 0
assert_not_contains "${RESULT}" "${XTRACE_TOKEN}"
assert_contains "${AUTH_LOG}" "header <Authorization: Bearer ${XTRACE_TOKEN}>"
end_case
rm -f "${CRED_FILE}"

# 非法 token 走 die 路径时同样不许泄漏（报错文案本来就不含 token，这里连 xtrace 也不许带出来）。
run_case "bash -x never prints a rejected token either" SW_OPS_UI_TOKEN='TESTTOKEN_xtrace"bad'
assert_status 1
assert_not_contains "${RESULT}" 'TESTTOKEN_xtrace"bad'
assert_contains "${RESULT}" "工作台 API token 含有不被允许的字符"
end_case
RUN_XTRACE=0

# ------------------------------------ stdin 的结构性保证（把脆弱不变量换成结构，任务 B）
# 远端正文外面那对花括号 + 尾部的 `} </dev/null` 是现在**唯一承重**的那一层：
#   ① `{ ... }` 是一条复合命令，bash 必须整条解析完才开始执行，正文因此在第一条命令跑起来
#      之前就已经离开输入流，任何读 stdin 的子进程都吞不到它；
#   ② `</dev/null` 挂在整个组上，组内所有命令与子进程继承的 fd 0 就是 /dev/null。
# 一正一反两条用例：正的证明逐条 `</dev/null` 标注已经不承重（只是纵深防御），
# 反的证明正例不是空转——把结构删掉，`verify.sh --preflight`「在最严模式下不可能失败」
# 这个生产实证过的阻断级缺陷必须原样重现。
STRUCT_DIR="${TMP}/struct"
mkdir -p "${STRUCT_DIR}"
cp "${ROOT}/scripts/ops/ui_token.sh" "${STRUCT_DIR}/ui_token.sh"
sed -e '/^} <\/dev\/null$/!s| </dev/null||g' "${SCRIPT}" >"${STRUCT_DIR}/annotations_stripped.sh"
sed -e '/^} <\/dev\/null$/d' -e '/^{$/d' -e 's| </dev/null||g' "${SCRIPT}" >"${STRUCT_DIR}/structure_removed.sh"

case_name="structural stdin variants are actually rewritten"
case_failures_at_start="${failures}"
struct_orig_lines="$(grep -c "" "${SCRIPT}")"
struct_stripped_lines="$(grep -c "" "${STRUCT_DIR}/annotations_stripped.sh")"
struct_removed_lines="$(grep -c "" "${STRUCT_DIR}/structure_removed.sh")"
if [[ "${struct_orig_lines}" -ne "${struct_stripped_lines}" ]]; then
  fail_assertion "annotations_stripped 不应改变行数：${struct_orig_lines} -> ${struct_stripped_lines}"
fi
if [[ $((struct_orig_lines - struct_removed_lines)) -ne 4 ]]; then
  fail_assertion "structure_removed 应恰好删掉 4 行（2 个 { 与 2 个 } </dev/null），实际删掉 $((struct_orig_lines - struct_removed_lines)) 行"
fi
struct_annotations_left="$(grep -c -- ' </dev/null' "${STRUCT_DIR}/annotations_stripped.sh" || true)"
if [[ "${struct_annotations_left}" -ne 2 ]]; then
  fail_assertion "annotations_stripped 里应只剩 2 处 </dev/null（两层组重定向），实际 ${struct_annotations_left} 处"
fi
end_case

# 正：逐条标注全删，最严模式下的失败判定一条不少。
SCRIPT_UNDER_TEST="${STRUCT_DIR}/annotations_stripped.sh"
run_case "stripping every per-command </dev/null keeps --preflight able to fail" TEST_DIRTY=1 --preflight
assert_status 1
assert_contains "${LOG}" "docker <compose> <exec> <-T> <core> <python3> <scripts/preflight.py>"
assert_contains "${RESULT}" "preflight 之后脚本仍在执行"
assert_contains "${RESULT}" "核验结论"
assert_contains "${RESULT}" "✗ 工作树干净  存在未提交改动"
assert_contains "${RESULT}" "✗ 生产部署核验失败"
assert_conclusion_verdicts 8 1
end_case

# 反：结构也删掉 = 回到改造前。preflight 把「核验结论」段、失败项汇总和 exit 1 整个吞掉，
# 于是工作树是脏的、脚本却报"全部核验项通过"——取证工具在最严模式下不可能失败。
SCRIPT_UNDER_TEST="${STRUCT_DIR}/structure_removed.sh"
# 注意断言的是**后果**而不是"从哪一行开始被吞"：吞掉多少字节取决于 bash 读脚本时的缓冲
# 方式——heredoc 在 bash 3.2 上是临时文件（可 seek，成块缓冲，前面几行可能已经读进内存），
# 在 bash 5.1+ 上小 heredoc 走管道（不可 seek，逐字节读，吞得更干净）。两种情况下"结论段连同
# exit 1 判定一起消失、脏工作树却报通过"这个后果都成立，所以只钉后果。
run_case "removing the brace group makes --preflight incapable of failing again" TEST_DIRTY=1 --preflight
assert_status 0
assert_contains "${LOG}" "docker <compose> <exec> <-T> <core> <python3> <scripts/preflight.py>"
assert_not_contains "${RESULT}" "核验结论"
assert_not_contains "${RESULT}" "✗ 工作树干净"
assert_contains "${RESULT}" "✓ 生产部署核验通过"
end_case
SCRIPT_UNDER_TEST="${SCRIPT}"

# ---------------------------------------------------------------- 传输重试
run_case "ssh transport failure retries exactly once" TEST_SSH_STATUSES=255,0
assert_status 0
assert_log_count "ssh <" 2
assert_contains "${RESULT}" "IAP 连接中断"
end_case

run_case "ssh transport retry exhausted" TEST_SSH_STATUSES=255,255
assert_status 1
assert_log_count "ssh <" 2
assert_contains "${RESULT}" "IAP 连接中断"
assert_contains "${RESULT}" "✗ 生产部署核验失败"
end_case

run_case "remote 255 is normalized and never retried" TEST_REMOTE_EXIT=255
assert_status 1
assert_log_count "ssh <" 1
assert_contains "${LOG}" "ssh-remote-status <254>"
assert_not_contains "${RESULT}" "IAP 连接中断"
end_case

run_case "remote non-255 failure never retries" TEST_REMOTE_EXIT=42
assert_status 1
assert_log_count "ssh <" 1
assert_contains "${LOG}" "ssh-remote-status <42>"
assert_not_contains "${RESULT}" "IAP 连接中断"
end_case

run_case "remote gate failure never retries" TEST_DIRTY=1
assert_status 1
assert_log_count "ssh <" 1
assert_contains "${LOG}" "ssh-remote-status <1>"
assert_not_contains "${RESULT}" "IAP 连接中断"
end_case

if [[ "${failures}" -ne 0 ]]; then
  printf 'verify.sh mechanical tests failed: %s assertion(s)\n' "${failures}" >&2
  exit 1
fi
printf 'verify.sh mechanical tests passed: %s case(s)\n' "${passed}"
