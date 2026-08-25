#!/bin/bash
# No network, SSH, or Docker: every boundary command is a local argv-recording fake.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="${ROOT}/scripts/ops/update.sh"
# 被测脚本路径。默认是仓库里那一份；「stdin 结构性保证」一节会临时指向改写过的副本。
SCRIPT_UNDER_TEST="${SCRIPT}"
COMPOSE_FILE="${ROOT}/docker-compose.yml"
OLD=1111111111111111111111111111111111111111
NEW=2222222222222222222222222222222222222222
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
if [[ "${1:-}" == "-s" ]]; then
  # 记录远端 shell 重新分词后真正交到脚本手里的位置参数。
  # depth=1 是 ssh 送达远端外层包装的那一份（即"线上真实收到什么"）；
  # depth=2 是外层再转交给内层更新脚本的那一份。空串用 [] 定界，必须看得见。
  sw_depth=$(( ${SW_TEST_REMOTE_DEPTH:-0} + 1 ))
  sw_argv=("$@")
  sw_start=1
  [[ "${sw_argv[1]:-}" == "--" ]] && sw_start=2
  sw_positional=("${sw_argv[@]:sw_start}")
  {
    printf 'remote-args depth=%s argc=%s' "${sw_depth}" "${#sw_positional[@]}"
    printf ' [%s]' ${sw_positional[@]+"${sw_positional[@]}"}
    printf '\n'
  } >>"${TEST_LOG}"
  # 【测试专用的脚本流捕获模式】设了 SW_TEST_DUMP_REMOTE 时，depth=1 那一层（也就是 ssh
  # 真正送达远端的那条脚本流）把 stdin 原样落盘并以 0 收尾，一行远端正文都不执行。
  # 它服务的是"发射进去的 sw_probe 到底落在花括号里面还是外面"这个问题——那件事没法靠
  # 行为断言分辨（两种放法在单层 bash 上都能跑通），只能直接看线上真正发出的那串字节。
  # 只在显式设置该变量时生效；其余用例一律走正常路径，不受影响。
  if [[ -n "${SW_TEST_DUMP_REMOTE:-}" && "${sw_depth}" -eq 1 ]]; then
    cat >"${SW_TEST_DUMP_REMOTE}"
    exit 0
  fi
  export SW_TEST_REMOTE_DEPTH="${sw_depth}"
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

# 忠实模拟真实 ssh(1)：ssh 不保留 argv 边界。ssh(1) 手册原文——
#   "If supplied, the arguments will be appended to the command, separated by
#    spaces, before it is sent to the server to be executed."
# 即 host 之后的全部参数被用单个空格拼成一个字符串发给远端，由远端登录 shell
# 重新分词。空参数在拼接里只留下一个空格，重新分词后彻底消失。
# 之前的假件直接把 argv 透传给 /bin/bash，语义与真实 ssh 相反，会给出虚假信心。
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    # 需要吃掉一个取值的 ssh 选项
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
# 用单个空格拼接（不依赖 IFS 现场值），得到真正上线的那一个字符串
remote_command="$1"
shift
while [[ "$#" -gt 0 ]]; do
  remote_command="${remote_command} $1"
  shift
done
printf 'ssh-command <%s>\n' "${remote_command}" >>"${TEST_LOG}"
# 远端登录 shell 重新分词；stdin（heredoc）原样继承
/bin/bash -c "${remote_command}"
remote_status=$?
printf 'ssh-remote-status <%s>\n' "${remote_status}" >>"${TEST_LOG}"
exit "${remote_status}"
EOF

cat >"${TMP}/bin/git" <<'EOF'
#!/bin/bash
set -euo pipefail
{
  printf 'git'
  printf ' <%s>' "$@"
  printf '\n'
} >>"${TEST_LOG}"

old=1111111111111111111111111111111111111111
new=2222222222222222222222222222222222222222
case "$1" in
  check-ref-format)
    exit "${TEST_CHECK_REF_STATUS:-0}"
    ;;
  fetch)
    exit "${TEST_GIT_FETCH_STATUS:-0}"
    ;;
  symbolic-ref)
    [[ "${TEST_DETACHED:-0}" -eq 1 ]] && exit 1
    printf '%s\n' "${TEST_BRANCH:-main}"
    ;;
  status)
    [[ "${TEST_STATUS_FAIL:-0}" -eq 1 ]] && exit 1
    [[ "${TEST_DIRTY:-0}" -eq 1 ]] && printf ' M dirty\n'
    exit 0
    ;;
  rev-parse)
    case "$*" in
      *"@{upstream}"*)
        [[ "${TEST_NO_UPSTREAM:-0}" -eq 1 ]] && exit 1
        printf '%s\n' "${TEST_UPSTREAM:-origin/main}"
        ;;
      *"refs/remotes/origin/"*"^{commit}"*)
        [[ "${TEST_NONCOMMIT:-0}" -eq 1 ]] && exit 1
        printf '%s\n' "${TEST_PEELED_SHA:-${new}}"
        ;;
      *"refs/remotes/origin/"*)
        printf '%s\n' "${TEST_RAW_SHA:-${TEST_PEELED_SHA:-${new}}}"
        ;;
      *"origin/main"*)
        printf '%s\n' "${TEST_UPSTREAM_SHA:-${new}}"
        ;;
      *"HEAD"*)
        if [[ -f "${TEST_CHANGED}" ]]; then
          printf '%s\n' "${TEST_DEPLOYED_SHA:-${TEST_PEELED_SHA:-${TEST_UPSTREAM_SHA:-${new}}}}"
        else
          printf '%s\n' "${old}"
        fi
        ;;
      *)
        exit 97
        ;;
    esac
    ;;
  rev-list)
    relation="${TEST_RELATION:-behind}"
    case "${relation}:$3" in
      behind:1111111111111111111111111111111111111111..*) printf '1\n' ;;
      behind:*) printf '0\n' ;;
      same:*) printf '0\n' ;;
      ahead:*..1111111111111111111111111111111111111111) printf '1\n' ;;
      ahead:*) printf '0\n' ;;
      diverged:*) printf '1\n' ;;
      not-ancestor:1111111111111111111111111111111111111111..*) printf '1\n' ;;
      not-ancestor:*) printf '0\n' ;;
      *) exit 96 ;;
    esac
    ;;
  merge-base)
    case "${TEST_RELATION:-behind}" in
      behind|same) exit 0 ;;
      ahead|diverged|not-ancestor) exit 1 ;;
    esac
    ;;
  --no-pager)
    printf '2222222 test commit\n'
    ;;
  merge)
    : >"${TEST_CHANGED}"
    ;;
  pull)
    : >"${TEST_CHANGED}"
    ;;
  reset|switch)
    # Recovery instructions must never execute automatically.
    exit 95
    ;;
  *)
    exit 94
    ;;
esac
EOF

cat >"${TMP}/bin/docker" <<'EOF'
#!/bin/bash
set -euo pipefail
{
  printf 'docker'
  printf ' <%s>' "$@"
  printf '\n'
} >>"${TEST_LOG}"
if [[ "$1 ${2:-} ${3:-} ${4:-} ${5:-} ${6:-}" == "compose exec -T core python3 -c" ]]; then
  # R1 闸门用的内联解析。真实 `docker compose exec -T` 转发 stdin，而被模拟的容器内
  # 进程自己就 sys.stdin.read()，所以直接 exec 出去即是忠实语义：stdin 被读干净。
  # 调用方必须自带显式 stdin 来源（`printf ... |` 管道），否则它照样会吃掉调用它的
  # 那份远端脚本正文——这正是下面「R1 闸门」哨兵用例要压住的后果。
  #
  # 两条内联解析各有**独立于 preflight** 的失败开关。刻意不复用 TEST_PREFLIGHT_STATUS /
  # TEST_DOCKER_FAIL_ON=exec：共用一个开关会同时打挂 preflight，于是"preflight 过了、
  # 但闸门的 docker exec 自己打嗝"这条真实路径根本构造不出来，也就永远没有测试覆盖。
  # 按脚本正文分派（只有 use_fake_publishers 那条会提到这个标识符）。
  if [[ "$7" == *use_fake_publishers* ]]; then
    gate_exec_status="${TEST_FAKE_EXEC_STATUS:-0}"
  else
    gate_exec_status="${TEST_TELEGRAM_EXEC_STATUS:-0}"
  fi
  if [[ "${gate_exec_status}" -ne 0 ]]; then
    # 真实 docker 无论最终成败都已经把 stdin 泵走了。
    cat >/dev/null
    exit "${gate_exec_status}"
  fi
  exec python3 -c "$7"
fi
if [[ "$1 ${2:-} ${3:-}" == "compose exec -T" ]]; then
  # 忠实模拟真实 `docker compose exec -T`：它会把 stdin **转发**给容器内进程
  # （`docker compose exec -T db psql < dump.sql` 能工作就是靠这个）。旧假件完全
  # 不碰 stdin，语义与真实命令相反 —— 于是"远端脚本被 preflight 一步吞掉"这个
  # 生产实证过的阻断级缺陷在测试里根本不存在。这里显式把 stdin 读空，让缺乏
  # `</dev/null` 保护的调用真的丢掉后续脚本正文。
  # 排在 TEST_DOCKER_FAIL_ON 之前：真实 docker 无论最终成败都已经把 stdin 泵走了。
  cat >/dev/null
  if [[ "${TEST_DOCKER_FAIL_ON:-}" == "exec" ]]; then
    exit 1
  fi
  exit "${TEST_PREFLIGHT_STATUS:-0}"
fi
if [[ "${TEST_DOCKER_FAIL_ON:-}" == "${2:-}" ]]; then
  exit 1
fi
if [[ "$1 $2 $3" == "compose port core" ]]; then
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

# 假 mv：默认透传给真 mv。TEST_MV_FAIL_SUBSTR 命中目标路径时失败——用来构造"部署成功了，
# 但那条部署标记写不进去"这条真实路径：部署本身**不许**因此失败，而一条过期的旧标记**必须**
# 被删掉（错的记录比没有记录坏）。
cat >"${TMP}/bin/mv" <<'EOF'
#!/bin/bash
if [[ -n "${TEST_MV_FAIL_SUBSTR:-}" ]]; then
  for mv_arg in "$@"; do
    if [[ "${mv_arg}" == *"${TEST_MV_FAIL_SUBSTR}"* ]]; then
      printf 'mv-forced-failure <%s>\n' "${mv_arg}" >>"${TEST_LOG}"
      exit 1
    fi
  done
fi
exec /bin/mv "$@"
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
  rm -f "${TMP}/changed"
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
    TEST_CHANGED="${TMP}/changed" \
    /bin/bash ${bash_opts[@]+"${bash_opts[@]}"} "${SCRIPT_UNDER_TEST}" "$@" 2>&1)"
  STATUS=$?
  set -e
  LOG="$(<"${TMP}/log")"
  AUTH_LOG="$(<"${TMP}/auth")"
}

assert_rejected_before_backup() {
  assert_status 1
  assert_not_contains "${LOG}" "backup <"
  assert_not_contains "${LOG}" "ssh <"
  assert_not_contains "${LOG}" "git <merge>"
  assert_not_contains "${LOG}" "git <pull>"
  assert_not_contains "${LOG}" "docker <compose> <build>"
}

assert_remote_rejected_before_change() {
  assert_status 1
  assert_contains "${LOG}" "backup <"
  assert_contains "${LOG}" "ssh <"
  assert_log_count "ssh <" 1
  assert_not_contains "${RESULT}" "IAP 连接中断"
  assert_not_contains "${LOG}" "git <merge>"
  assert_not_contains "${LOG}" "git <pull>"
  assert_log_count "docker <compose> <build>" 0
}

# The production-facing core mapping is fixed to IPv4 loopback in Compose.
case_name="compose core loopback binding"
COMPOSE_TEXT="$(<"${COMPOSE_FILE}")"
assert_contains "${COMPOSE_TEXT}" '      - "127.0.0.1:8000:8000"'
assert_not_contains "${COMPOSE_TEXT}" '      - "8000:8000"'

# ------------------------------------- sw_probe 是单一真相源（源码级 + 真实流两道断言）
# 【不变量换过了，先读这一段】从前：verify.sh / update.sh / restart.sh / status.sh 里各内联
# 一份**逐字相同**的 sw_probe，本节的职责是逐字比对那四份。现在定义只剩**一处**——
# scripts/ops/ui_token.sh 里的 sw_ops_emit_sw_probe_definition——各远端脚本在拼 ssh stdin
# 流的时候把它**发射**进去。于是新的不变量是三条：
#   ① scripts/ops/ 下只有一处定义，且就在 ui_token.sh 里；没有任何脚本自己内联；
#   ② 每个需要 sw_probe 的远端脚本都**调用**那个发射函数，恰好一次；
#   ③ 发射进去的字节必须落在远端正文那对花括号的**里面**。这一条不是洁癖：落在外面时
#      `{ ... } </dev/null` 的结构性保证盖不到它，而且两层 bash 的脚本里内层 `bash -s`
#      是另一个进程，根本拿不到外层定义的函数——远端会以 "sw_probe: command not found"
#      收场。①② 是源码级；③ 用**真实捕获的那条 ssh stdin 流**验，见本文件下方
#      「the remote stream carries sw_probe inside the brace group」。
# 抽取器自检的思路原样保留：正则一旦失配就必须**变红**，绝不许退化成"空 == 空"而全绿。
cat >"${TMP}/extract_sw_probe.awk" <<'AWKEOF'
/^sw_probe_code=''$/ { on = 1 }
on { print; if ($0 == "}") { n++; if (n == 2) exit } }
AWKEOF

case_name="sw_probe has exactly one definition and it lives in ui_token.sh"
probe_body="$(awk -f "${TMP}/extract_sw_probe.awk" "${ROOT}/scripts/ops/ui_token.sh")"
# 抽取器自检：正则一旦失配（比如以后有人给函数加了缩进），下面几条会退化成"空==空"而全绿。
probe_lines="$(printf '%s\n' "${probe_body}" | grep -c "" || true)"
if [[ "${probe_lines}" -lt 15 ]]; then
  fail_assertion "从 ui_token.sh 抽到的 sw_probe 只有 ${probe_lines} 行，抽取器多半失配了"
fi
# shellcheck disable=SC2016  # 这里要的就是被扫文件里的字面文本，不能展开
if [[ "${probe_body}" != *'curl -q -fsS --max-time "${max_time}" -w '* ]]; then
  fail_assertion "从 ui_token.sh 抽到的 sw_probe 里没有那条 curl，抽取器多半失配了"
fi
probe_owners="$(grep -l "^sw_probe_code=''$" "${ROOT}"/scripts/ops/*.sh || true)"
probe_owner_count="$(printf '%s\n' "${probe_owners}" | grep -c . || true)"
if [[ "${probe_owner_count}" -ne 1 || "${probe_owners}" != "${ROOT}/scripts/ops/ui_token.sh" ]]; then
  fail_assertion "sw_probe 的定义必须只有一处、且在 scripts/ops/ui_token.sh 里；实测持有者：${probe_owners:-<无>}（${probe_owner_count} 个）"
fi

case_name="every remote script takes sw_probe from that one emitter"
# 覆盖度自检放在最前面：下面那个循环写死了文件名，新增调用方时这里必须先红一次，
# 逼着改动的人回来把它加进列表——而不是悄悄多出一个没被任何断言看着的调用方。
probe_callers="$(grep -l '^  sw_ops_emit_sw_probe_definition$' "${ROOT}"/scripts/ops/*.sh \
  | while IFS= read -r f; do basename "${f}"; done | LC_ALL=C sort | tr '\n' ' ')"
PROBE_EXPECTED_CALLERS="env_set.sh restart.sh sidecar.sh status.sh update.sh verify.sh "
if [[ "${probe_callers}" != "${PROBE_EXPECTED_CALLERS}" ]]; then
  fail_assertion "调用 sw_ops_emit_sw_probe_definition 的脚本是 [${probe_callers}]，期望 [${PROBE_EXPECTED_CALLERS}]——新增/删减调用方请同步改这条断言"
fi
for probe_script in ${PROBE_EXPECTED_CALLERS}; do
  probe_src="${ROOT}/scripts/ops/${probe_script}"
  probe_emit_count="$(grep -c '^  sw_ops_emit_sw_probe_definition$' "${probe_src}" || true)"
  if [[ "${probe_emit_count}" -ne 1 ]]; then
    fail_assertion "${probe_script} 里 sw_ops_emit_sw_probe_definition 的调用有 ${probe_emit_count} 处，应当恰好 1 处"
  fi
  # 内联一份"顺手改一改"的拷贝，正是本次重构要消灭的东西。任何一个 sw_probe 的招牌行
  # 出现在脚本自己身上都判红——不只是那条 `sw_probe_code=''`。
  if grep -q '^sw_probe_curl_config() {$' "${probe_src}" || grep -q '^sw_probe() {$' "${probe_src}"; then
    fail_assertion "${probe_script} 里又出现了内联的 sw_probe 定义——定义只许有一处（scripts/ops/ui_token.sh）"
  fi
done

# stdin 的源码级护栏（收窄版，扫 scripts/ops/*.sh 全部）。
#
# 【它现在的定位变了，先看这一段】远端脚本正文已经不再是那层 bash 的 stdin：三个远端脚本
#   的正文都被 `{ ... } </dev/null` 包住了（① 复合命令必须整条解析完才开始执行，正文因此
#   在第一条命令跑起来之前就离开了输入流；② 组重定向让组内所有命令与子进程的 fd 0 是
#   /dev/null）。承重的是那一层，本扫描器与被它扫的那些 `</dev/null` 都已降级为**纵深防御**。
#   保留它们的理由：读代码时 stdin 来源自明；花括号一旦被人拆掉它们就是第二道防线；
#   新增调用点时有一道自动提醒。本文件下方「stdin 的结构性保证」一节用一正一反两条用例
#   分别钉住"标注全删也不影响行为"与"结构删掉缺陷立刻重现"。
#
# 【为什么只查 docker compose exec】历史上远端脚本正文就是那层 bash 自己的 stdin。
#   `docker compose exec -T` **真的会转发 stdin**（`-T` 只关 TTY），所以它一旦少了显式
#   stdin 来源就会把"脚本剩下的部分"整段吞掉——这是生产实证过的阻断级缺陷。而真实
#   `curl` 做 GET 时**压根不读 stdin**，那是一个不存在的失败模式；上一版连 `curl` 一起扫，
#   守住的是零风险项，换来的却是实打实的误伤：把 `</dev/null` 写在反斜杠续行上会误报，
#   往错误文案里写一句带命令名的话也会误报（本轮实测踩到：`strict_hint="…（读取方式：
#   docker compose exec -T core python3 解析 …）"` 被直接判红）。故收窄到 exec 一格。
#
# 【判定过程】① 整行注释不参与；② 反斜杠续行与管道/&&/|| 续行先拼成逻辑行（status.sh 的
#   `curl … |` 换行接 `docker compose exec` 就是后者，不拼就会误判）；③ 只认**命令位置**上
#   的匹配（前面是行首，或 | ; & ( { ` ! then else do elif），字符串文案里出现命令名不算；
#   ④ 该逻辑行必须给出显式 stdin 来源：`</dev/null`、heredoc（`<<`）、或前置管道。
#
# 【局限，必须写明】这是**词法匹配，不是安全边界**：
#   ① `E=docker; $E compose exec …` 这类间接调用绕得过去；
#   ② **文案里某一行以命令名顶格开头**仍会被当成命令位置而误报——运维文案里放一行可直接
#      粘贴的命令很常见，遇到时把那行改成不顶格（或在前面加一句说明）即可，**不是真问题**，
#      别去改被扫的生产脚本来迁就它。当前仓内无此形态（文案里的命令名都在句中，前面是
#      「查 」「与 」这类中文，不构成命令位置）。
#   ③ 只查 `docker compose exec`。`docker compose restart/logs/ps` 等在本仓也一律写了
#      `</dev/null`（远端 heredoc 的既定不变量），但**不在静态扫描范围内**——理由与当初
#      把 `curl` 摘出去一样：真实命令不读 stdin，扫它是守一个不存在的失败模式，代价是误伤。
#      那几格靠行为测试与 code review 兜。
#   它的定位是**给新增调用点一道自动提醒**——真正的保证来自 `{ ... } </dev/null` 这层结构，
#   以及行为测试（假件忠实消费 stdin，加上闸门放行行 / preflight 哨兵行的存在性断言）。
cat >"${TMP}/scan_stdin.awk" <<'AWKEOF'
function is_cmd_pos(pre,   t) {
  t = pre
  sub(/[[:space:]]+$/, "", t)
  if (t == "") return 1
  if (t ~ /(\||;|&|\(|\{|`|!)$/) return 1
  if (t ~ /(^|[[:space:]])(then|else|do|elif)$/) return 1
  return 0
}
/^[[:space:]]*#/ { next }
{
  buf = buf $0
  if ($0 ~ /\\[[:space:]]*$/) { sub(/\\[[:space:]]*$/, " ", buf); next }
  if ($0 ~ /(\||&&|\|\|)[[:space:]]*$/) { buf = buf " "; next }
  line = buf; buf = ""
  start = 1
  while (1) {
    rest = substr(line, start)
    i = index(rest, needle)
    if (i == 0) break
    abs = start + i - 1
    if (is_cmd_pos(substr(line, 1, abs - 1))) { print base ":" FNR ": " line; break }
    start = abs + length(needle)
  }
}
AWKEOF

case_name="scripts/ops/*.sh give every docker compose exec an explicit stdin"
: >"${TMP}/scan_out"
for ops_script in "${ROOT}"/scripts/ops/*.sh; do
  awk -v needle='docker compose exec' -v base="$(basename "${ops_script}")" \
    -f "${TMP}/scan_stdin.awk" "${ops_script}" >>"${TMP}/scan_out"
done
while IFS= read -r line; do
  [[ -n "${line}" ]] || continue
  # 前置管道要按"匹配点之前有没有 |"判，不能按字面 `| docker compose exec` 判：
  # 拼过续行的逻辑行里管道和命令之间会留下不定长空白（status.sh 就是 `curl … |` 换行）。
  scan_prefix="${line%%docker compose exec*}"
  [[ "${line}" == *"</dev/null"* || "${line}" == *"<<"* || "${scan_prefix}" == *"|"* ]] \
    || fail_assertion "docker compose exec 缺少显式 stdin 来源：${line}"
done <"${TMP}/scan_out"
# 覆盖度自检：扫描器一旦写坏（比如正则失配导致一条都不命中），上面的循环会静默全绿。
# 这里钉死每个已知调用方至少被看到几条，让"扫了个寂寞"这种失效也变红。
# （bash 3.2 没有关联数组，所以按文件名重数一遍，不用 declare -A。）
for expect in "backup.sh:2" "status.sh:2" "update.sh:3" "verify.sh:4" "restart.sh:2" "sidecar.sh:2"; do
  expect_file="${expect%%:*}"
  expect_min="${expect##*:}"
  scanned_count="$(awk -F: -v f="${expect_file}" '$1 == f { n++ } END { print n + 0 }' "${TMP}/scan_out")"
  if [[ "${scanned_count}" -lt "${expect_min}" ]]; then
    fail_assertion "stdin 扫描器只在 ${expect_file} 里看到 ${scanned_count} 条 docker compose exec，至少应有 ${expect_min} 条"
  fi
done

# CLI validation is entirely local and must precede backup and SSH.
run_case "only sha" --sha "${NEW}"
assert_rejected_before_backup
assert_contains "${RESULT}" "同时提供"

run_case "only ref" --ref p14-organic
assert_rejected_before_backup
assert_contains "${RESULT}" "同时提供"

run_case "uppercase sha" --ref p14-organic --sha ABCDEFABCDEFABCDEFABCDEFABCDEFABCDEFABCD
assert_rejected_before_backup
assert_contains "${RESULT}" "小写"

run_case "short sha" --ref p14-organic --sha 2222222
assert_rejected_before_backup
assert_contains "${RESULT}" "40 位"

run_case "mutually exclusive modes" --dry-run --apply
assert_rejected_before_backup
assert_contains "${RESULT}" "只能指定一次"

run_case "duplicate mode" --dry-run --dry-run
assert_rejected_before_backup
assert_contains "${RESULT}" "只能指定一次"

run_case "duplicate ref" --ref p14 --ref other --sha "${NEW}"
assert_rejected_before_backup
assert_contains "${RESULT}" "--ref 只能指定一次"

run_case "duplicate sha" --ref p14 --sha "${NEW}" --sha "${NEW}"
assert_rejected_before_backup
assert_contains "${RESULT}" "--sha 只能指定一次"

run_case "missing ref value" --ref
assert_rejected_before_backup
assert_contains "${RESULT}" "--ref 缺少"

run_case "ref value is option" --ref --sha "${NEW}"
assert_rejected_before_backup
assert_contains "${RESULT}" "--ref 缺少"

run_case "missing sha value" --ref p14 --sha
assert_rejected_before_backup
assert_contains "${RESULT}" "--sha 缺少"

run_case "leading option ref" --ref -pwn --sha "${NEW}"
assert_rejected_before_backup
assert_contains "${RESULT}" "ref 格式非法"

run_case "shell injection ref" --ref 'p14;id' --sha "${NEW}"
assert_rejected_before_backup
assert_contains "${RESULT}" "ref 格式非法"

run_case "invalid git ref" --ref 'topic..bad' --sha "${NEW}"
assert_rejected_before_backup
assert_contains "${RESULT}" "ref 格式非法"

run_case "extra positional argument" --dry-run extra
assert_rejected_before_backup
assert_contains "${RESULT}" "参数无效"

# ssh argv 边界：ssh 把 host 之后的参数用单个空格拼成一个字符串交给远端登录 shell
# 重新分词。以下用例断言的是"远端实际收到的位置参数"，不是退出码。
# depth=1 = ssh 送达远端外层包装的那一份；depth=2 = 外层转交给内层更新脚本的那一份。
run_case "argv default dry-run reaches remote intact"
assert_status 0
assert_contains "${LOG}" "remote-args depth=1 argc=3 [--dry-run] [] []"
assert_contains "${LOG}" "remote-args depth=2 argc=3 [--dry-run] [] []"
assert_not_contains "${RESULT}" "unbound variable"
assert_not_contains "${RESULT}" "更新--dry-run失败"

run_case "argv default apply reaches remote intact" --apply
assert_status 0
assert_contains "${LOG}" "remote-args depth=1 argc=3 [--apply] [] []"
assert_contains "${LOG}" "remote-args depth=2 argc=3 [--apply] [] []"
assert_not_contains "${RESULT}" "unbound variable"
# 空 ref 必须真的走到 upstream 快进路径，而不是半路死在 set -u 上
assert_contains "${LOG}" "git <rev-parse> <--abbrev-ref> <--symbolic-full-name> <@{upstream}>"
assert_contains "${LOG}" "git <pull> <--ff-only>"
assert_not_contains "${LOG}" "git <merge>"

run_case "argv pinned reaches remote verbatim" --apply --ref p14-organic --sha "${NEW}"
assert_status 0
assert_contains "${LOG}" "remote-args depth=1 argc=3 [--apply] [p14-organic] [${NEW}]"
assert_contains "${LOG}" "remote-args depth=2 argc=3 [--apply] [p14-organic] [${NEW}]"
# %q 不得改写已通过校验的 ref/sha 字面量
assert_contains "${LOG}" "ssh-command <bash -s -- --apply p14-organic ${NEW} >"

# 防回归哨兵：直接断言"空参数不会在 ssh 拼接/远端重新分词中消失"这件事本身。
run_case "argv empty arguments survive ssh word splitting"
assert_status 0
# 线上真正发出的那一个字符串里，空参数必须以 '' 的形式带引号存活
assert_contains "${LOG}" "ssh-command <bash -s -- --dry-run '' '' >"
# 裸 argv 形态拼出的坏字符串（空参数塌成空格）必须不再出现
assert_not_contains "${LOG}" "ssh-command <bash -s -- --dry-run  >"
# 远端两层都必须恰好看到 3 个位置参数，塌成 1 个即为回归
assert_log_count "remote-args depth=1 argc=3" 1
assert_log_count "remote-args depth=2 argc=3" 1
assert_not_contains "${LOG}" "argc=1"
assert_not_contains "${LOG}" "argc=2"

# Default compatibility path: configured upstream, fetch --prune, and pull --ff-only only.
run_case "default dry-run"
assert_status 0
assert_contains "${LOG}" "backup <"
assert_contains "${LOG}" "git <rev-parse> <--abbrev-ref> <--symbolic-full-name> <@{upstream}>"
assert_contains "${LOG}" "git <fetch> <--prune>"
assert_not_contains "${LOG}" "git <pull>"
assert_not_contains "${LOG}" "git <merge>"
assert_not_contains "${LOG}" "docker <compose> <build>"

run_case "default apply" --apply
assert_status 0
assert_contains "${LOG}" "git <pull> <--ff-only>"
assert_not_contains "${LOG}" "git <merge>"
assert_contains "${LOG}" "docker <compose> <build> <core>"
assert_contains "${LOG}" "docker <compose> <exec> <-T> <core> <python3> <scripts/preflight.py>"

run_case "default missing upstream" TEST_NO_UPSTREAM=1 --apply
assert_remote_rejected_before_change
assert_contains "${RESULT}" "没有 upstream"
assert_contains "${RESULT}" "未自动回滚"

run_case "default dirty" TEST_DIRTY=1 --apply
assert_remote_rejected_before_change
assert_contains "${RESULT}" "未提交改动"
assert_contains "${RESULT}" "未自动回滚"
assert_log_count "git <fetch>" 1

run_case "default status failure" TEST_STATUS_FAIL=1 --apply
assert_remote_rejected_before_change
assert_contains "${RESULT}" "无法确认工作树状态"
assert_contains "${RESULT}" "未自动回滚"

run_case "default ahead" TEST_RELATION=ahead --apply
assert_remote_rejected_before_change
assert_contains "${RESULT}" "领先目标"
assert_contains "${RESULT}" "未自动回滚"
assert_log_count "git <fetch>" 1

run_case "default diverged" TEST_RELATION=diverged --apply
assert_remote_rejected_before_change
assert_contains "${RESULT}" "分叉"
assert_contains "${RESULT}" "未自动回滚"
assert_log_count "git <fetch>" 1

run_case "default deployed head drift" TEST_DEPLOYED_SHA="${OTHER}" --apply
assert_status 1
assert_contains "${LOG}" "git <pull> <--ff-only>"
assert_contains "${RESULT}" "部署后 HEAD"
assert_not_contains "${LOG}" "docker <compose> <build>"

# Pinned mode fetches one exact heads refspec and verifies raw/direct/peeled object identity.
run_case "pinned dry-run" --dry-run --ref p14-organic --sha "${NEW}"
assert_status 0
assert_contains "${LOG}" "backup <"
assert_contains "${LOG}" "git <check-ref-format> <--branch> <p14-organic>"
assert_contains "${LOG}" "git <fetch> <--no-tags> <--prune> <origin> <+refs/heads/p14-organic:refs/remotes/origin/p14-organic>"
assert_contains "${LOG}" "git <rev-parse> <--verify> <refs/remotes/origin/p14-organic>"
assert_contains "${LOG}" "git <rev-parse> <--verify> <refs/remotes/origin/p14-organic^{commit}>"
assert_not_contains "${LOG}" "git <merge>"
assert_not_contains "${LOG}" "git <pull>"
assert_not_contains "${LOG}" "docker <compose> <build>"

run_case "pinned remote noncommit" TEST_NONCOMMIT=1 --dry-run --ref p14-organic --sha "${NEW}"
assert_remote_rejected_before_change
assert_contains "${RESULT}" "不是 commit"
assert_log_count "git <fetch>" 1

run_case "pinned raw differs from peeled" TEST_RAW_SHA="${OTHER}" --dry-run --ref p14-organic --sha "${NEW}"
assert_remote_rejected_before_change
assert_contains "${RESULT}" "直接指向 commit"

run_case "pinned sha mismatch" TEST_PEELED_SHA="${OTHER}" --dry-run --ref p14-organic --sha "${NEW}"
assert_remote_rejected_before_change
assert_contains "${RESULT}" "期望 SHA 不一致"
assert_log_count "git <fetch>" 1

run_case "pinned dirty" TEST_DIRTY=1 --apply --ref p14-organic --sha "${NEW}"
assert_remote_rejected_before_change
assert_contains "${RESULT}" "未提交改动"
assert_log_count "git <fetch>" 1

run_case "pinned ahead" TEST_RELATION=ahead --apply --ref p14-organic --sha "${NEW}"
assert_remote_rejected_before_change
assert_contains "${RESULT}" "领先目标"
assert_log_count "git <fetch>" 1

run_case "pinned diverged" TEST_RELATION=diverged --apply --ref p14-organic --sha "${NEW}"
assert_remote_rejected_before_change
assert_contains "${RESULT}" "分叉"
assert_log_count "git <fetch>" 1

run_case "pinned not ancestor" TEST_RELATION=not-ancestor --apply --ref p14-organic --sha "${NEW}"
assert_remote_rejected_before_change
assert_contains "${RESULT}" "不是目标的祖先"

run_case "pinned deployed head drift" TEST_DEPLOYED_SHA="${OTHER}" --apply --ref p14-organic --sha "${NEW}"
assert_status 1
assert_contains "${LOG}" "git <merge> <--ff-only> <${NEW}>"
assert_not_contains "${LOG}" "git <pull>"
assert_contains "${RESULT}" "部署后 HEAD"
assert_not_contains "${LOG}" "docker <compose> <build>"

run_case "pinned apply" --apply --ref p14-organic --sha "${NEW}"
assert_status 0
assert_contains "${LOG}" "git <merge> <--ff-only> <${NEW}>"
assert_not_contains "${LOG}" "git <pull>"
assert_contains "${LOG}" "docker <compose> <build> <core>"
assert_contains "${LOG}" "docker <compose> <up> <-d> <core>"
assert_contains "${LOG}" "docker <compose> <port> <core> <8000>"
assert_contains "${LOG}" "docker <compose> <exec> <-T> <core> <python3> <scripts/preflight.py>"
assert_contains "${LOG}" "curl <-q> <-fsS> <--max-time> <5> <-w> <\n%{http_code}> <--config> <-> <http://127.0.0.1:8000/api/v1/system/info>"

# Retry classification: only a local ssh 255 in dry-run mode gets one retry.
run_case "dry-run dirty does not retry" TEST_DIRTY=1 --dry-run --ref p14-organic --sha "${NEW}"
assert_remote_rejected_before_change
assert_contains "${RESULT}" "未提交改动"
assert_log_count "git <fetch>" 1

run_case "dry-run ahead does not retry" TEST_RELATION=ahead --dry-run --ref p14-organic --sha "${NEW}"
assert_remote_rejected_before_change
assert_contains "${RESULT}" "领先目标"
assert_log_count "git <fetch>" 1

run_case "dry-run diverged does not retry" TEST_RELATION=diverged --dry-run --ref p14-organic --sha "${NEW}"
assert_remote_rejected_before_change
assert_contains "${RESULT}" "分叉"
assert_log_count "git <fetch>" 1

run_case "dry-run transport retry succeeds" TEST_SSH_STATUSES=255,0 --dry-run --ref p14-organic --sha "${NEW}"
assert_status 0
assert_log_count "ssh <" 2
assert_log_count "git <fetch>" 1
assert_log_count "docker <compose> <build>" 0
assert_contains "${RESULT}" "IAP 连接中断"

run_case "dry-run transport retry exhausted" TEST_SSH_STATUSES=255,255 --dry-run --ref p14-organic --sha "${NEW}"
assert_status 1
assert_log_count "ssh <" 2
assert_log_count "git <fetch>" 0
assert_log_count "docker <compose> <build>" 0
assert_contains "${RESULT}" "IAP 连接中断"

run_case "dry-run non-255 remote failure does not retry" TEST_GIT_FETCH_STATUS=42 --dry-run --ref p14-organic --sha "${NEW}"
assert_status 1
assert_log_count "ssh <" 1
assert_contains "${LOG}" "ssh-remote-status <42>"
assert_log_count "git <fetch>" 1
assert_log_count "docker <compose> <build>" 0
assert_not_contains "${RESULT}" "IAP 连接中断"

run_case "remote 255 is normalized and does not retry" TEST_GIT_FETCH_STATUS=255 --dry-run --ref p14-organic --sha "${NEW}"
assert_status 1
assert_log_count "ssh <" 1
assert_contains "${LOG}" "ssh-remote-status <254>"
assert_log_count "git <fetch>" 1
assert_log_count "docker <compose> <build>" 0
assert_not_contains "${RESULT}" "IAP 连接中断"

run_case "apply transport failure does not retry" TEST_SSH_STATUSES=255 --apply --ref p14-organic --sha "${NEW}"
assert_status 1
assert_log_count "ssh <" 1
assert_log_count "git <fetch>" 0
assert_log_count "docker <compose> <build>" 0
assert_not_contains "${RESULT}" "IAP 连接中断"

# Recovery output is copy/paste-safe and remains instructions only.
printf -v escaped_repo '%q' "${TMP}/home dir/social_workflow"
run_case "recovery branch shell metachar" TEST_BRANCH='safe;id' TEST_PORT='0.0.0.0:8000' --apply --ref p14-organic --sha "${NEW}"
assert_status 1
assert_contains "${RESULT}" "更新前 branch=safe;id, HEAD=${OLD}"
assert_contains "${RESULT}" "cd ${escaped_repo}"
assert_contains "${RESULT}" 'git switch -- safe\;id'
assert_contains "${RESULT}" "git reset --hard ${OLD}"
assert_contains "${RESULT}" "未自动回滚"
assert_not_contains "${LOG}" "git <reset>"
assert_not_contains "${LOG}" "git <switch>"

run_case "recovery detached" TEST_DETACHED=1 TEST_PORT='0.0.0.0:8000' --apply --ref p14-organic --sha "${NEW}"
assert_status 1
assert_contains "${RESULT}" "更新前 branch=<detached HEAD>, HEAD=${OLD}"
assert_contains "${RESULT}" "git switch --detach ${OLD}"
assert_not_contains "${RESULT}" "reset --hard"
assert_not_contains "${LOG}" "git <reset>"
assert_not_contains "${LOG}" "git <switch>"

run_case "unexpected build failure recovery" TEST_DOCKER_FAIL_ON=build --apply --ref p14-organic --sha "${NEW}"
assert_status 1
assert_contains "${LOG}" "docker <compose> <build> <core>"
assert_contains "${RESULT}" "未自动回滚"
assert_contains "${RESULT}" "HEAD=${OLD}"
assert_not_contains "${LOG}" "docker <compose> <up>"
assert_not_contains "${LOG}" "git <reset>"

# Valid decimal edge ports reach both preflight and the host-specific probe.
run_case "port 1 ipv4" TEST_PORT='127.0.0.1:1' --apply --ref p14-organic --sha "${NEW}"
assert_status 0
assert_contains "${LOG}" "docker <compose> <exec> <-T> <core> <python3> <scripts/preflight.py>"
assert_contains "${LOG}" "curl <-q> <-fsS> <--max-time> <5> <-w> <\n%{http_code}> <--config> <-> <http://127.0.0.1:1/api/v1/system/info>"

run_case "port 65535 ipv6" TEST_PORT='[::1]:65535' --apply --ref p14-organic --sha "${NEW}"
assert_status 0
assert_contains "${LOG}" "docker <compose> <exec> <-T> <core> <python3> <scripts/preflight.py>"
assert_contains "${LOG}" "curl <-q> <-fsS> <--max-time> <5> <-w> <\n%{http_code}> <--config> <-> <http://[::1]:65535/api/v1/system/info>"

invalid_ports=(
  ''
  '127.0.0.1:0'
  '127.0.0.1:00000'
  '127.0.0.1:08000'
  '127.0.0.1:65536'
  '127.0.0.1:99999'
  '127.0.0.1:123456789012345678901234567890'
  '127.0.0.1:8x00'
  '127.0.0.1:8000:extra'
  '0.0.0.0:8000'
  ':::8000'
  '[::]:8000'
  '[::]'
  $'127.0.0.1:8000\r'
  $'127.0.0.1:8000\n127.0.0.1:8001'
  $'127.0.0.1:8000\n'
)
for invalid_port in "${invalid_ports[@]}"; do
  run_case "reject port $(printf '%q' "${invalid_port}")" TEST_PORT="${invalid_port}" --apply --ref p14-organic --sha "${NEW}"
  assert_status 1
  assert_contains "${RESULT}" "拒绝映射"
  assert_contains "${RESULT}" "未自动回滚"
  assert_not_contains "${LOG}" "docker <compose> <exec>"
  assert_not_contains "${LOG}" "curl <"
done

run_case "port command failure" TEST_PORT_COMMAND_FAIL=1 --apply --ref p14-organic --sha "${NEW}"
assert_status 1
assert_contains "${RESULT}" "无法读取"
assert_contains "${RESULT}" "未自动回滚"
assert_not_contains "${LOG}" "docker <compose> <exec>"
assert_not_contains "${LOG}" "curl <"

# ------------------------------------------- preflight 之后脚本必须继续执行到探针
# 生产实证的阻断级缺陷：内层 bash 的**脚本正文就是它的 stdin**，而 `docker compose
# exec -T` 会转发 stdin。preflight 那一步少了 `</dev/null`，就会把它后面整段
# /api/v1/system/info 探针循环连同 abort_update 一起吞掉，脚本以 0 收尾，
# 外层照样打印"✓ 更新完成……探针均通过"——而 core 是否真的活过来从未被证明。
# 下面这些用例断言的是**后果**：把 `</dev/null` 拿掉，它们必须变红。

run_case "apply reaches the system info probe after preflight" --apply --ref p14-organic --sha "${NEW}"
assert_status 0
assert_contains "${LOG}" "docker <compose> <exec> <-T> <core> <python3> <scripts/preflight.py>"
# 防回归哨兵：preflight 之后紧跟的这条标记必须出现，直接证明脚本正文没有被吞掉。
assert_contains "${RESULT}" "容器内门禁  preflight 通过，继续做探针"
assert_contains "${LOG}" "curl <-q> <-fsS> <--max-time> <5> <-w> <\n%{http_code}> <--config> <-> <http://127.0.0.1:8000/api/v1/system/info>"
assert_log_count "curl <" 1
assert_contains "${RESULT}" "探针  GET /api/v1/system/info 200（第 1 次）"
assert_contains "${RESULT}" "✓ 更新完成，端口门禁、容器内门禁和探针均通过"

run_case "default apply reaches the system info probe after preflight" --apply
assert_status 0
assert_contains "${LOG}" "docker <compose> <exec> <-T> <core> <python3> <scripts/preflight.py>"
assert_contains "${RESULT}" "容器内门禁  preflight 通过，继续做探针"
assert_contains "${LOG}" "curl <-q> <-fsS> <--max-time> <5> <-w> <\n%{http_code}> <--config> <-> <http://127.0.0.1:8000/api/v1/system/info>"
assert_contains "${RESULT}" "探针  GET /api/v1/system/info 200（第 1 次）"
assert_contains "${RESULT}" "✓ 更新完成，端口门禁、容器内门禁和探针均通过"

# 探针一直不回 200 时必须真的重试满 15 次再 abort —— 这整段循环正是被吞掉的那段。
run_case "apply fails when the system info probe never returns" TEST_CURL_STATUS=7 --apply --ref p14-organic --sha "${NEW}"
assert_status 1
assert_contains "${RESULT}" "容器内门禁  preflight 通过，继续做探针"
assert_log_count "curl <" 15
assert_contains "${RESULT}" "core 在 30 秒内未恢复 /api/v1/system/info 200"
assert_contains "${RESULT}" "未自动回滚"
assert_not_contains "${RESULT}" "✓ 更新完成"

run_case "apply aborts when preflight fails" TEST_PREFLIGHT_STATUS=1 --apply --ref p14-organic --sha "${NEW}"
assert_status 1
assert_contains "${LOG}" "docker <compose> <exec> <-T> <core> <python3> <scripts/preflight.py>"
assert_not_contains "${RESULT}" "容器内门禁  preflight 通过"
assert_not_contains "${LOG}" "curl <"
assert_contains "${RESULT}" "未自动回滚"
assert_contains "${RESULT}" "HEAD=${OLD}"
assert_not_contains "${RESULT}" "✓ 更新完成"

# 演练路径压根不该碰 preflight 或探针（`--dry-run` 在 build/up 之前就 exit 0）。
run_case "dry-run never reaches preflight or the probe" --dry-run --ref p14-organic --sha "${NEW}"
assert_status 0
assert_not_contains "${LOG}" "docker <compose> <exec>"
assert_not_contains "${LOG}" "curl <"
assert_not_contains "${RESULT}" "容器内门禁  preflight 通过"
assert_contains "${RESULT}" "演练完成：目标 SHA 已核验"

# --------------------------------------------------- R1 红线闸门（真发布 + 死通道）
# docs/RISKS.md §12：「真发布已开启（use_fake_publishers=false）+ 人工确认闸门通道是死
# 的」这个组合，在闸门落地前没有任何自动流程能拦住 —— verify.sh 里那道同款互锁只在人手
# 动敲的时候才跑，而 preflight 的 check_notifier 被刻意设计成永不 FAIL。闸门现在坐在
# `--apply` 的 /api/v1/system/info 探针**成功之后**：那一刻读到的才是刚部署这一版的配置。
INFO_REAL_PUBLISH='{"ok":true,"data":{"version":"0.1.0","env":"prod","time":"2026-08-22T02:00:00Z","timezone":"Asia/Shanghai","scheduler_enabled":true,"generate_enabled":true,"use_fake_publishers":false,"auth_required":false,"publishers":["xhs","douyin"]}}'
INFO_NO_FIELD='{"ok":true,"data":{"version":"0.1.0","env":"prod"}}'
TG_LIVE='{"ok":true,"data":{"enabled":true,"configured":true,"ready":true,"chat_configured":true,"can_sign":true,"polling":true,"username":"sw_ops_bot","sent":3,"failed":0,"stats":{},"detail":"","last_error":""}}'
TG_NOT_READY='{"ok":true,"data":{"enabled":true,"configured":true,"ready":false,"chat_configured":false,"can_sign":true,"polling":false,"username":"sw_ops_bot","sent":0,"failed":0,"stats":{},"detail":"未配置 chat_id","last_error":""}}'
TG_NOT_POLLING='{"ok":true,"data":{"enabled":true,"configured":true,"ready":true,"chat_configured":true,"can_sign":true,"polling":false,"username":"sw_ops_bot","sent":0,"failed":0,"stats":{},"detail":"","last_error":""}}'
# 总开关关着、但 ready 与 polling 都是 true：靠 polling 去间接兜住 enabled 的写法在这一格
# 上必然漏判，所以闸门必须直判 enabled。
TG_DISABLED='{"ok":true,"data":{"enabled":false,"configured":true,"ready":true,"chat_configured":true,"can_sign":true,"polling":true,"username":"sw_ops_bot","sent":0,"failed":0,"stats":{},"detail":"","last_error":""}}'

assert_r1_gate_aborted() {
  assert_status 1
  assert_contains "${RESULT}" "R1 红线闸门未通过"
  assert_contains "${RESULT}" "未自动回滚"
  assert_contains "${RESULT}" "HEAD=${OLD}"
  assert_not_contains "${RESULT}" "✓ 更新完成"
  # 闸门坐在探针之后，此刻新版早已 build+up 过，所以 rollback_hint 的人工恢复提示是
  # 必需的契约；但恢复命令永远只是文本，绝不自动执行。
  assert_contains "${LOG}" "docker <compose> <up> <-d> <core>"
  assert_contains "${LOG}" "curl <-q> <-fsS> <--max-time> <5> <-w> <\n%{http_code}> <--config> <-> <http://127.0.0.1:8000/api/v1/system/telegram>"
  assert_not_contains "${LOG}" "git <reset>"
  assert_not_contains "${LOG}" "git <switch>"
}

# 1) 模拟发布器挂着：什么都不会真发，死通道不构成阻断，部署照常成功。
#    这条路径的行为必须与闸门落地前逐字一致 —— 只多一行记录，绝不多打一个请求。
run_case "R1 gate records fake publishers and never probes telegram" \
  TEST_TELEGRAM_JSON="${TG_NOT_READY}" --apply --ref p14-organic --sha "${NEW}"
assert_status 0
assert_contains "${RESULT}" "R1 闸门  模拟发布器=true：本版什么都不会真发"
assert_contains "${RESULT}" "探针  GET /api/v1/system/info 200（第 1 次）"
assert_contains "${RESULT}" "✓ 更新完成，端口门禁、容器内门禁和探针均通过"
assert_log_count "curl <" 1
assert_not_contains "${LOG}" "/api/v1/system/telegram"

# 2) 真发布开启 + 通道 enabled/ready/polling 三真：放行。
run_case "R1 gate passes real publishing with a live confirm channel" \
  TEST_INFO_JSON="${INFO_REAL_PUBLISH}" TEST_TELEGRAM_JSON="${TG_LIVE}" --apply --ref p14-organic --sha "${NEW}"
assert_status 0
assert_contains "${LOG}" "curl <-q> <-fsS> <--max-time> <5> <-w> <\n%{http_code}> <--config> <-> <http://127.0.0.1:8000/api/v1/system/telegram>"
assert_contains "${RESULT}" "R1 闸门  真发布已开启（模拟发布器=false）：人工确认闸门通道 enabled=true ready=true polling=true"
assert_contains "${RESULT}" "✓ 更新完成，端口门禁、容器内门禁和探针均通过"

# 3) 真发布 + ready=false：那张要点的卡片根本推不出去。
run_case "R1 gate aborts real publishing when the channel is not ready" \
  TEST_INFO_JSON="${INFO_REAL_PUBLISH}" TEST_TELEGRAM_JSON="${TG_NOT_READY}" --apply --ref p14-organic --sha "${NEW}"
assert_r1_gate_aborted
assert_contains "${RESULT}" "真发布已开启（模拟发布器=false）"
assert_contains "${RESULT}" "ready=false（那张要点的卡片根本推不出去）"
assert_contains "${RESULT}" "后果不是内容会越权发出去"
assert_contains "${RESULT}" "SW_CONFIRM_TTL_HOURS（默认 24 小时）到点被自动驳回"
assert_contains "${RESULT}" "兜底：工作台的「确认发布」按钮不受 Telegram 影响"
# 回滚有没有用按格分：enabled/ready 是 .env（没用），polling 是正在跑的那份代码
# （core/main.py:104 lifespan 起线程、core/telegram.py:981 判活），回滚有用。
# 先前那句"与代码版本无关"对 polling 是错的，会把人从正确解法上引开。
assert_contains "${RESULT}" "enabled 与 ready 取决于服务器 .env 里的 SW_TELEGRAM_*，回滚代码修不好"
assert_contains "${RESULT}" "polling 取决于**正在跑的这份代码**"
assert_contains "${RESULT}" "core/main.py:104 的 lifespan 起、core/telegram.py:981 按 poller.alive 判活"
assert_contains "${RESULT}" "回滚到上一版是有效解法"
assert_not_contains "${RESULT}" "与代码版本无关"

# 4) 真发布 + polling=false：卡片能推出去，人点了没有线程去收。
run_case "R1 gate aborts real publishing when polling is dead" \
  TEST_INFO_JSON="${INFO_REAL_PUBLISH}" TEST_TELEGRAM_JSON="${TG_NOT_POLLING}" --apply --ref p14-organic --sha "${NEW}"
assert_r1_gate_aborted
assert_contains "${RESULT}" "ready=true 但 polling=false"

# 5) 真发布 + enabled=false（ready/polling 都还是 true）：必须直判总开关，不能靠 polling 兜。
run_case "R1 gate aborts real publishing when the master switch is off" \
  TEST_INFO_JSON="${INFO_REAL_PUBLISH}" TEST_TELEGRAM_JSON="${TG_DISABLED}" --apply --ref p14-organic --sha "${NEW}"
assert_r1_gate_aborted
assert_contains "${RESULT}" "总开关 enabled=false"

# 6) use_fake_publishers 取不到 / 不可解析 → 从严按真发布裁定。
run_case "R1 gate is strict when the info body is empty" \
  TEST_INFO_JSON= TEST_TELEGRAM_JSON="${TG_NOT_READY}" --apply --ref p14-organic --sha "${NEW}"
assert_r1_gate_aborted
assert_contains "${RESULT}" "模拟发布器状态取不到（use_fake_publishers=<未知>），按真发布从严裁定"

run_case "R1 gate is strict when the info body is not json" \
  TEST_INFO_JSON='<html>502 Bad Gateway</html>' TEST_TELEGRAM_JSON="${TG_NOT_POLLING}" --apply --ref p14-organic --sha "${NEW}"
assert_r1_gate_aborted
assert_contains "${RESULT}" "按真发布从严裁定"

run_case "R1 gate is strict when use_fake_publishers is missing" \
  TEST_INFO_JSON="${INFO_NO_FIELD}" TEST_TELEGRAM_JSON="${TG_DISABLED}" --apply --ref p14-organic --sha "${NEW}"
assert_r1_gate_aborted
assert_contains "${RESULT}" "按真发布从严裁定"
assert_contains "${RESULT}" "总开关 enabled=false"

# 从严不等于必然阻断：取不到 use_fake_publishers 但通道是活的时，仍然放行，
# 只是裁定理由必须如实写明是"按真发布从严"，不能被静默当成模拟发布器放过去。
run_case "R1 gate strict mode still passes a live channel" \
  TEST_INFO_JSON='not json at all' TEST_TELEGRAM_JSON="${TG_LIVE}" --apply --ref p14-organic --sha "${NEW}"
assert_status 0
assert_contains "${RESULT}" "R1 闸门  模拟发布器状态取不到（use_fake_publishers=<未知>），按真发布从严裁定：人工确认闸门通道 enabled=true ready=true polling=true"
assert_contains "${RESULT}" "✓ 更新完成"

# 6b) 闸门自己那条内联解析打嗝（独立于 preflight）：升级从严，但通道活着仍然放行。
#     用独立开关而不是 TEST_PREFLIGHT_STATUS / TEST_DOCKER_FAIL_ON=exec —— 共用开关会同时
#     打挂 preflight，于是"preflight 过了、只是闸门的 docker exec 自己出错"这条真实路径
#     根本构造不出来，也就永远没有覆盖。断言里同时钉住 preflight 哨兵仍在，证明两者独立。
run_case "R1 gate exec hiccup escalates to strict but still passes a live channel" \
  TEST_FAKE_EXEC_STATUS=125 TEST_TELEGRAM_JSON="${TG_LIVE}" --apply --ref p14-organic --sha "${NEW}"
assert_status 0
assert_contains "${RESULT}" "容器内门禁  preflight 通过，继续做探针"
assert_contains "${RESULT}" "R1 闸门  模拟发布器状态取不到（use_fake_publishers=<未知>），按真发布从严裁定：人工确认闸门通道 enabled=true ready=true polling=true"
assert_contains "${RESULT}" "✓ 更新完成"

# 6c) 同一条打嗝 + 通道是死的：阻断，但建议必须可执行——不许再劝"把 SW_USE_FAKE_PUBLISHERS
#     设回 true"，因为连这个值都没读出来，它很可能已经是 true 了，改了也没用。
run_case "R1 gate exec hiccup with a dead channel gives actionable advice" \
  TEST_FAKE_EXEC_STATUS=125 TEST_TELEGRAM_JSON="${TG_NOT_POLLING}" --apply --ref p14-organic --sha "${NEW}"
assert_r1_gate_aborted
assert_contains "${RESULT}" "容器内门禁  preflight 通过，继续做探针"
assert_contains "${RESULT}" "按真发布从严裁定"
assert_contains "${RESULT}" "所以先别急着改 SW_USE_FAKE_PUBLISHERS——它可能已经是 true 了"
assert_contains "${RESULT}" "docker compose logs core"
assert_not_contains "${RESULT}" "或把 SW_USE_FAKE_PUBLISHERS 设回 true 后重新部署"

# 反过来：真发布确凿（读得到 false）时，"设回 true"是真能执行的建议，必须给。
run_case "R1 gate offers the fake-publishers escape hatch only when it is actionable" \
  TEST_INFO_JSON="${INFO_REAL_PUBLISH}" TEST_TELEGRAM_JSON="${TG_NOT_READY}" --apply --ref p14-organic --sha "${NEW}"
assert_r1_gate_aborted
assert_contains "${RESULT}" "或把 SW_USE_FAKE_PUBLISHERS 设回 true 后重新部署"
assert_not_contains "${RESULT}" "所以先别急着改 SW_USE_FAKE_PUBLISHERS"

# 7) 确认通道探针本身失败（curl 非零）+ 真发布开启：同样阻断。
run_case "R1 gate aborts when the telegram probe fails" \
  TEST_INFO_JSON="${INFO_REAL_PUBLISH}" TEST_TELEGRAM_CURL_STATUS=7 --apply --ref p14-organic --sha "${NEW}"
assert_r1_gate_aborted
assert_contains "${RESULT}" "无法获取 /api/v1/system/telegram"

# 确认通道返回的不是可解析 JSON / 是失败外壳时，同样从严阻断。
run_case "R1 gate aborts when the telegram body is not json" \
  TEST_INFO_JSON="${INFO_REAL_PUBLISH}" TEST_TELEGRAM_JSON='<html>502</html>' --apply --ref p14-organic --sha "${NEW}"
assert_r1_gate_aborted
assert_contains "${RESULT}" "无法解析 /api/v1/system/telegram"

run_case "R1 gate aborts when the telegram envelope reports failure" \
  TEST_INFO_JSON="${INFO_REAL_PUBLISH}" TEST_TELEGRAM_JSON='{"ok":false,"data":null}' --apply --ref p14-organic --sha "${NEW}"
assert_r1_gate_aborted
assert_contains "${RESULT}" "无法解析 /api/v1/system/telegram"

# 7b) 确认通道那条内联解析自己出错（与 use_fake_publishers 那条是不同的开关）：退出码落进
#     兜底分支，依然阻断。这条用例也是 TEST_TELEGRAM_EXEC_STATUS 不是备而不用脚手架的证明。
run_case "R1 gate aborts when the telegram parse exec hiccups" \
  TEST_INFO_JSON="${INFO_REAL_PUBLISH}" TEST_TELEGRAM_EXEC_STATUS=125 --apply --ref p14-organic --sha "${NEW}"
assert_r1_gate_aborted
assert_contains "${RESULT}" "真发布已开启（模拟发布器=false）"
assert_contains "${RESULT}" "/api/v1/system/telegram 解析异常（退出码 125）"

# 8) stdin 哨兵：闸门里两条 `docker compose exec -T` 都由 `printf ... |` 管道显式喂 stdin。
#    少了这层显式来源，它们会把闸门之后的脚本正文（含放行行与 exit 0）整段吞掉，而脚本
#    仍以 0 收尾、外层照样打印"✓ 更新完成"——闸门等于没跑。直接断言放行行仍然出现。
run_case "R1 gate does not swallow the rest of the remote script" \
  TEST_INFO_JSON="${INFO_REAL_PUBLISH}" TEST_TELEGRAM_JSON="${TG_LIVE}" --apply
assert_status 0
assert_contains "${LOG}" "git <pull> <--ff-only>"
assert_contains "${RESULT}" "容器内门禁  preflight 通过，继续做探针"
assert_contains "${RESULT}" "探针  GET /api/v1/system/info 200（第 1 次）"
assert_contains "${RESULT}" "R1 闸门  真发布已开启（模拟发布器=false）：人工确认闸门通道 enabled=true ready=true polling=true"
assert_contains "${RESULT}" "✓ 更新完成，端口门禁、容器内门禁和探针均通过"
# 闸门恰好用掉两条内联解析（use_fake_publishers 一条、确认通道一条），一条都不多。
assert_log_count "docker <compose> <exec> <-T> <core> <python3> <-c>" 2
assert_log_count "docker <compose> <exec> <-T> <core> <python3> <scripts/preflight.py>" 1

# 9) 演练路径完全不受影响：`--dry-run` 在 merge 之前就 exit 0，压根不会去取通道状态。
run_case "dry-run never reaches the R1 gate" \
  TEST_INFO_JSON="${INFO_REAL_PUBLISH}" TEST_TELEGRAM_JSON="${TG_NOT_READY}" --dry-run --ref p14-organic --sha "${NEW}"
assert_status 0
assert_not_contains "${LOG}" "curl <"
assert_not_contains "${LOG}" "/api/v1/system/telegram"
assert_not_contains "${LOG}" "docker <compose> <exec>"
assert_not_contains "${RESULT}" "R1 闸门"
assert_contains "${RESULT}" "演练完成：目标 SHA 已核验"

# ------------------------------------------- 工作台 API token（docs/RISKS.md 第 8 条 §8.4）
# 生产 .env 一旦配上非空 SW_UI_TOKEN，/api/v1/* 全部要求 Authorization: Bearer。§8.4 记录的
# 实施前置就是：探针必须能带上这个头，而且 token 一个字符都不能进 argv（生产是合租机器，
# /proc/*/cmdline 世界可读）。下面这些用例分别钉住：送到了 / 没泄漏 / 没配时行为不变 /
# 配错时给的是可行动提示而不是一句"部署失败"。
UI_TOKEN='TESTTOKEN_update-A1b2+/=.:@'

# 假件把每一次调用的完整 argv 都落进 TEST_LOG（ssh、bash、git、docker、curl 全覆盖，还包括
# ssh 真正发出的那一整条远端命令字符串）。token 明文在这份记录里必须是 0 次。
assert_token_absent_from_argv() {
  local token="$1" hits
  hits="$(grep -F -c -- "${token}" <<<"${LOG}" || true)"
  if [[ "${hits}" -ne 0 ]]; then
    fail_assertion "token 明文在 argv 记录里出现了 ${hits} 次，必须是 0；log: ${LOG}"
  fi
}

run_case "ui token reaches both probe headers and never appears in argv" \
  SW_OPS_UI_TOKEN="${UI_TOKEN}" TEST_INFO_JSON="${INFO_REAL_PUBLISH}" TEST_TELEGRAM_JSON="${TG_LIVE}" \
  --apply --ref p14-organic --sha "${NEW}"
assert_status 0
assert_contains "${AUTH_LOG}" "url <http://127.0.0.1:8000/api/v1/system/info> header <Authorization: Bearer ${UI_TOKEN}>"
assert_contains "${AUTH_LOG}" "url <http://127.0.0.1:8000/api/v1/system/telegram> header <Authorization: Bearer ${UI_TOKEN}>"
assert_token_absent_from_argv "${UI_TOKEN}"
assert_not_contains "${RESULT}" "${UI_TOKEN}"
assert_contains "${RESULT}" "已加载工作台 API token（来源：环境变量 SW_OPS_UI_TOKEN）"
assert_contains "${RESULT}" "✓ 更新完成，端口门禁、容器内门禁和探针均通过"

# 未配置 token：配置流照样存在（代码路径同构），但里面一个头都没有；输出里也不提 token。
run_case "without a ui token the config stream carries no header" --apply --ref p14-organic --sha "${NEW}"
assert_status 0
assert_contains "${AUTH_LOG}" "url <http://127.0.0.1:8000/api/v1/system/info> header <>"
assert_not_contains "${AUTH_LOG}" "Authorization"
assert_not_contains "${RESULT}" "已加载工作台 API token"
assert_contains "${RESULT}" "✓ 更新完成，端口门禁、容器内门禁和探针均通过"

# 字符集校验在**本机**、在备份与 SSH 之前完成，且报错里绝不回显 token 本身。
run_case "a token containing a double quote is rejected before backup and ssh" \
  SW_OPS_UI_TOKEN='TESTTOKEN_bad"quote' --apply --ref p14-organic --sha "${NEW}"
assert_rejected_before_backup
assert_contains "${RESULT}" "工作台 API token 含有不被允许的字符"
# 报错必须按**白名单**口径说话：列一份"不允许"清单会让 token 里带 % 的人挨个排除后得出
# "我这个应该合法"，再卡在一条看不懂的报错上。
assert_contains "${RESULT}" "这是**白名单**：只允许 A-Z a-z 0-9 以及 . _ - + / = : @；**其余字符一律拒绝**"
# 不许再出现"空白会截断配置行"这句错话：实测空格在 curl 双引号参数里能原样通过，
# 排除空白的真实理由是它不是合法的凭据字符（RFC 6750 的 b64token）。
assert_not_contains "${RESULT}" "空白会截断配置行"
assert_contains "${RESULT}" "空白与控制字符不是合法的凭据字符"
assert_not_contains "${RESULT}" 'TESTTOKEN_bad"quote'

run_case "a token containing a backslash is rejected before backup and ssh" \
  SW_OPS_UI_TOKEN='TESTTOKEN_bad\back' --apply --ref p14-organic --sha "${NEW}"
assert_rejected_before_backup
assert_contains "${RESULT}" "工作台 API token 含有不被允许的字符"

# 探针拿到 401：立刻停手（不再空等 30 秒），并且必须说清"这不是部署失败、先别回滚"。
# §8.4 点名的误判风险就是把这一刻读成部署故障。
run_case "a 401 probe aborts immediately with an actionable auth message" \
  TEST_INFO_HTTP_CODE=401 --apply --ref p14-organic --sha "${NEW}"
assert_status 1
assert_contains "${RESULT}" "容器内门禁  preflight 通过，继续做探针"
assert_contains "${RESULT}" "探针拿到 401：core 已启用 SW_UI_TOKEN 鉴权，而本次部署没带上匹配的 token。"
assert_contains "${RESULT}" "先别按下面的人工恢复指引回滚"
assert_contains "${RESULT}" "401 恰恰证明新版 core 已经起来并在正常应答"
assert_contains "${RESULT}" "export SW_OPS_UI_TOKEN=<生产 .env 里 SW_UI_TOKEN 的同一个值>"
assert_contains "${RESULT}" "bash scripts/ops/verify.sh --sha ${NEW}"
assert_contains "${RESULT}" "已经配了还是 401 = 值不匹配"
assert_not_contains "${RESULT}" "core 在 30 秒内未恢复"
assert_not_contains "${RESULT}" "✓ 更新完成"
# 401 说明 core 已经在应答，重试 15 次毫无意义：只打一次。
assert_log_count "curl <" 1
# 它仍然是 abort_update，所以人工恢复指引照旧打印，但恢复命令永远只是文本。
assert_contains "${RESULT}" "未自动回滚"
assert_not_contains "${LOG}" "git <reset>"
assert_not_contains "${LOG}" "git <switch>"

# 401 与"连不上"必须分得开：连不上仍然重试满 15 次、仍然走原来的文案。
run_case "a transport failure still retries fifteen times with the old message" \
  TEST_CURL_STATUS=7 --apply --ref p14-organic --sha "${NEW}"
assert_status 1
assert_log_count "curl <" 15
assert_contains "${RESULT}" "core 在 30 秒内未恢复 /api/v1/system/info 200"
assert_not_contains "${RESULT}" "探针拿到 401"

# 401 与其它 HTTP 失败也必须分得开：503 走原来的等待与文案。
run_case "an http 503 still retries fifteen times with the old message" \
  TEST_INFO_HTTP_CODE=503 --apply --ref p14-organic --sha "${NEW}"
assert_status 1
assert_log_count "curl <" 15
assert_contains "${RESULT}" "core 在 30 秒内未恢复 /api/v1/system/info 200"
assert_not_contains "${RESULT}" "探针拿到 401"

# 生产真的开了 token 而本机没配：端到端跑一遍 §8.4 描述的那一刻。
run_case "auth-enabled core without a local token aborts with the auth message" \
  TEST_REQUIRE_TOKEN="${UI_TOKEN}" --apply --ref p14-organic --sha "${NEW}"
assert_status 1
assert_contains "${RESULT}" "探针拿到 401：core 已启用 SW_UI_TOKEN 鉴权"
assert_not_contains "${RESULT}" "✓ 更新完成"

# 同一台 core，本机配对了 token：部署照常完成，R1 闸门照常跑，token 依然不进 argv。
run_case "auth-enabled core with the matching token completes the deploy" \
  TEST_REQUIRE_TOKEN="${UI_TOKEN}" SW_OPS_UI_TOKEN="${UI_TOKEN}" \
  TEST_INFO_JSON="${INFO_REAL_PUBLISH}" TEST_TELEGRAM_JSON="${TG_LIVE}" \
  --apply --ref p14-organic --sha "${NEW}"
assert_status 0
assert_contains "${RESULT}" "探针  GET /api/v1/system/info 200（第 1 次）"
assert_contains "${RESULT}" "R1 闸门  真发布已开启（模拟发布器=false）：人工确认闸门通道 enabled=true ready=true polling=true"
assert_contains "${RESULT}" "✓ 更新完成，端口门禁、容器内门禁和探针均通过"
assert_token_absent_from_argv "${UI_TOKEN}"

# 确认通道探针的 401 也要落进 R1 闸门的裁定文案里（真发布 + 拿不到通道状态 = 阻断）。
run_case "a telegram 401 lands in the R1 gate verdict" \
  TEST_INFO_JSON="${INFO_REAL_PUBLISH}" TEST_TELEGRAM_HTTP_CODE=401 \
  --apply --ref p14-organic --sha "${NEW}"
assert_r1_gate_aborted
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
run_case "bash -x never prints the token" SW_OPS_UI_TOKEN="${XTRACE_TOKEN}" --apply --ref p14-organic --sha "${NEW}"
assert_status 0
assert_token_absent_from_argv "${XTRACE_TOKEN}"
assert_not_contains "${RESULT}" "${XTRACE_TOKEN}"
assert_contains "${RESULT}" "+ sw_ops_load_ui_token"
assert_contains "${AUTH_LOG}" "header <Authorization: Bearer ${XTRACE_TOKEN}>"

run_case "bash -x never prints a rejected token either" SW_OPS_UI_TOKEN='TESTTOKEN_xtrace"bad' --apply --ref p14-organic --sha "${NEW}"
assert_status 1
assert_not_contains "${RESULT}" 'TESTTOKEN_xtrace"bad'
assert_contains "${RESULT}" "工作台 API token 含有不被允许的字符"
RUN_XTRACE=0

# ----------------------------------------- 部署标记：路径字面量必须两边逐字一致
# 标记由 update.sh 写、由 verify.sh 读。路径一旦两边漂开，写的人和读的人各看各的文件，
# 而**两边都不会报错**——verify.sh 只会永远说"没有记录"，看起来像"还没部署过"。
# 这种失效没有任何行为测试会红（两个脚本各自都能跑通），只能靠源码级断言钉死。
case_name="update.sh and verify.sh agree on the deploy marker path"
# shellcheck disable=SC2016  # 要的就是脚本里的字面量文本，不能展开
MARKER_LITERAL='deploy_marker_file="${HOME}/sw-deploy-state/last-deploy"'
for marker_script in update verify; do
  marker_hits="$(grep -c -F -- "${MARKER_LITERAL}" "${ROOT}/scripts/ops/${marker_script}.sh" || true)"
  if [[ "${marker_hits}" -ne 1 ]]; then
    fail_assertion "${marker_script}.sh 里 ${MARKER_LITERAL} 出现了 ${marker_hits} 次，应恰好 1 次——两边的路径字面量必须逐字一致"
  fi
done
# 标记必须落在 git 工作树**外面**：放进 ~/social_workflow 会让 verify.sh 的「工作树干净」
# 判失败、让 update.sh 下一次自己拒绝部署。
# shellcheck disable=SC2016  # 同上：这里比对的是被扫脚本里的字面量
if grep -q 'deploy_marker_file="\${HOME}/social_workflow' "${ROOT}/scripts/ops/update.sh"; then
  fail_assertion "部署标记被放进了 git 工作树里——它会让 verify.sh 判「工作树不干净」"
fi

# ----------------------------------------- 部署标记：写不写、写什么、写不成怎么办
MARKER_DIR="${TMP}/home dir/sw-deploy-state"
MARKER_FILE="${MARKER_DIR}/last-deploy"
clear_marker() { rm -rf "${MARKER_DIR}"; }
read_marker() { [[ -f "${MARKER_FILE}" ]] && cat "${MARKER_FILE}"; }

clear_marker
run_case "a pinned apply records the release line it deployed" --apply --ref p14-organic --sha "${NEW}"
assert_status 0
assert_contains "${RESULT}" "部署标记  已记录 ref=p14-organic sha=${NEW} at="
MARKER_TEXT="$(read_marker || true)"
assert_contains "${MARKER_TEXT}" "schema=1"
assert_contains "${MARKER_TEXT}" "ref=p14-organic"
assert_contains "${MARKER_TEXT}" "sha=${NEW}"
case_name="a pinned apply records the release line it deployed"
# 时间戳必须是规范的 UTC 形状——verify.sh 的极窄解析只认这一种，形状漂了它会判"读不懂"。
printf '%s\n' "${MARKER_TEXT}" | grep -qE '^at=[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$' \
  || fail_assertion "标记里的 at= 不是规范 UTC 时间戳；实际内容：${MARKER_TEXT}"

clear_marker
run_case "the upstream path records the upstream name it followed" --apply
assert_status 0
assert_contains "${RESULT}" "部署标记  已记录 ref=origin/main sha=${NEW} at="
MARKER_TEXT="$(read_marker || true)"
assert_contains "${MARKER_TEXT}" "ref=origin/main"

# 演练不部署，所以绝不许留下"部署过"的记录。
clear_marker
run_case "a dry run leaves no deploy marker at all" --dry-run --ref p14-organic --sha "${NEW}"
assert_status 0
assert_not_contains "${RESULT}" "部署标记"
case_name="a dry run leaves no deploy marker at all"
[[ ! -e "${MARKER_FILE}" ]] || fail_assertion "演练不该留下部署标记，但 ${MARKER_FILE} 存在了"

# 写不成时：① 部署本身**不**失败（它已经成功了，为一条簿记文件判失败是错的）；
#           ② 那条过期的旧标记**必须**被删掉——错的记录比没有记录坏。
clear_marker
mkdir -p "${MARKER_DIR}"
printf 'schema=1\nref=some-old-line\nsha=%s\nat=2026-01-01T00:00:00Z\n' "${OLD}" >"${MARKER_FILE}"
run_case "a failed marker write never fails the deploy but drops the stale record" \
  TEST_MV_FAIL_SUBSTR=last-deploy --apply --ref p14-organic --sha "${NEW}"
assert_status 0
assert_contains "${RESULT}" "✓ 更新完成，端口门禁、容器内门禁和探针均通过"
assert_contains "${RESULT}" "部署标记  ⚠ 写入失败，已把可能存在的旧标记一并删掉"
case_name="a failed marker write never fails the deploy but drops the stale record"
[[ ! -e "${MARKER_FILE}" ]] || fail_assertion "写入失败时必须把过期的旧标记删掉，但它还在：$(cat "${MARKER_FILE}")"
# 临时文件也不许留下。
marker_residue="$(find "${MARKER_DIR}" -maxdepth 1 -name 'last-deploy.tmp.*' 2>/dev/null || true)"
[[ -z "${marker_residue}" ]] || fail_assertion "标记写入路径留下了临时文件：${marker_residue}"
clear_marker

# --------------------------- 发射进去的 sw_probe 必须落在花括号里面（看真实的那条流）
# 上面两条源码级断言管得住"定义只有一处、每个脚本都从那一处取"，但管不住**取到之后放在
# 哪儿**。放错地方的后果是实打实的：
#   · 落在外层 `{` 之前 → `{ ... } </dev/null` 那层结构性保证盖不到它；
#   · 落在外层花括号里、内层 heredoc 外 → 内层 `bash -s` 是另一个进程，拿不到外层定义的
#     函数，远端会以 "sw_probe: command not found" 收场。
# 这两种错法都不是源码文本能一眼看出来的，所以这里直接把 ssh 真正送出去的那串字节捕获
# 下来数花括号深度。捕获靠假 bash 的 SW_TEST_DUMP_REMOTE 模式（见文件顶部那段说明）。
cat >"${TMP}/probe_depth.awk" <<'AWKEOF'
$0 == "{" { depth++ }
$0 == "} </dev/null" { depth-- }
!seen && $0 == "sw_probe_code=''" { print depth; seen = 1 }
END { if (!seen) print "none" }
AWKEOF

run_case "the remote stream carries sw_probe inside the brace group" \
  SW_TEST_DUMP_REMOTE="${TMP}/remote_stream" --dry-run --ref p14-organic --sha "${NEW}"
assert_status 0
STREAM="$(<"${TMP}/remote_stream")"
# 捕获自检：流是空的 / 短得离谱时下面每一条都会退化成无意义的通过，所以先把这一格钉死。
stream_lines="$(printf '%s\n' "${STREAM}" | grep -c "" || true)"
if [[ "${stream_lines}" -lt 100 ]]; then
  fail_assertion "捕获到的远端脚本流只有 ${stream_lines} 行，捕获多半没生效"
fi
# 前言在花括号**外面**：它是流的第一行，且必须仍然只是一条 export（ui_token.sh 顶部那段
# "只许放不读 stdin 的内建命令"的警告说的就是这一行）。
assert_contains "$(printf '%s\n' "${STREAM}" | head -n 1)" "export SW_OPS_UI_TOKEN="
# 定义确实进了流。
assert_contains "${STREAM}" "sw_probe_code=''"
# shellcheck disable=SC2016  # 要的就是流里的字面文本，不能展开
assert_contains "${STREAM}" 'raw="$(sw_probe_curl_config | curl -q -fsS --max-time'
# 深度必须是 2：update.sh 的远端是两层——外层状态规范化包装一层 `{`，内层更新脚本再一层。
probe_depth="$(awk -f "${TMP}/probe_depth.awk" "${TMP}/remote_stream")"
if [[ "${probe_depth}" != "2" ]]; then
  fail_assertion "sw_probe 定义出现在花括号深度 ${probe_depth} 处，应当是 2（外层包装 + 内层更新脚本）"
fi
# 定义必须排在第一次调用之前，否则远端读到调用时函数还不存在。
# `|| true`：定义或调用真的不见了时，这里必须**报断言失败**，而不是让 set -e 把整个测试
# 文件在半路打死——那会让下面的用例连跑都没跑，输出里看不出到底坏在哪。
def_line="$(grep -n "^sw_probe_code=''$" "${TMP}/remote_stream" | head -n 1 | cut -d: -f1 || true)"
call_line="$(grep -n '^  sw_probe "http' "${TMP}/remote_stream" | head -n 1 | cut -d: -f1 || true)"
if [[ -z "${def_line}" || -z "${call_line}" || "${def_line}" -ge "${call_line}" ]]; then
  fail_assertion "sw_probe 的定义在第 ${def_line:-<无>} 行、第一次调用在第 ${call_line:-<无>} 行，定义必须在前"
fi

# ------------------------------------ stdin 的结构性保证（把脆弱不变量换成结构，任务 B）
# 远端正文外面那对花括号 + 尾部的 `} </dev/null` 是现在**唯一承重**的那一层：
#   ① `{ ... }` 是一条复合命令，bash 必须整条解析完才开始执行，正文因此在第一条命令跑起来
#      之前就已经离开输入流，任何读 stdin 的子进程都吞不到它；
#   ② `</dev/null` 挂在整个组上，组内所有命令与子进程继承的 fd 0 就是 /dev/null。
# 下面两条用例一正一反：
#   正：把逐条命令上的 `</dev/null` 标注**全部删掉**，脚本必须照样跑完、哨兵照样在——
#       证明那些标注确实已经不承重，只是纵深防御。
#   反：把结构本身（那两行 `{` 与两行 `} </dev/null`）也删掉，历史缺陷必须**重新出现**——
#       证明上面那条正例不是空转，也证明假件确实忠实地转发/消费 stdin。
STRUCT_DIR="${TMP}/struct"
mkdir -p "${STRUCT_DIR}"
cp "${ROOT}/scripts/ops/ui_token.sh" "${STRUCT_DIR}/ui_token.sh"
# 只删逐条标注，保留 `} </dev/null` 这层组重定向。
sed -e '/^} <\/dev\/null$/!s| </dev/null||g' "${SCRIPT}" >"${STRUCT_DIR}/annotations_stripped.sh"
# 再把结构本身也删掉：两行单独的 `{` 与两行 `} </dev/null`，回到改造前的形态。
sed -e '/^} <\/dev\/null$/d' -e '/^{$/d' -e 's| </dev/null||g' "${SCRIPT}" >"${STRUCT_DIR}/structure_removed.sh"

# 改写器自检：sed 一旦失配（比如以后有人给花括号加了缩进），下面两条用例会静默失去意义。
case_name="structural stdin variants are actually rewritten"
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

# 正：逐条标注全删，行为与仓库里那一份逐字一致。
SCRIPT_UNDER_TEST="${STRUCT_DIR}/annotations_stripped.sh"
run_case "stripping every per-command </dev/null changes nothing" \
  TEST_INFO_JSON="${INFO_REAL_PUBLISH}" TEST_TELEGRAM_JSON="${TG_LIVE}" --apply --ref p14-organic --sha "${NEW}"
assert_status 0
assert_contains "${LOG}" "docker <compose> <exec> <-T> <core> <python3> <scripts/preflight.py>"
assert_contains "${RESULT}" "容器内门禁  preflight 通过，继续做探针"
assert_contains "${RESULT}" "探针  GET /api/v1/system/info 200（第 1 次）"
assert_contains "${RESULT}" "R1 闸门  真发布已开启（模拟发布器=false）：人工确认闸门通道 enabled=true ready=true polling=true"
assert_contains "${RESULT}" "✓ 更新完成，端口门禁、容器内门禁和探针均通过"

# 反：结构也删掉 = 回到改造前。preflight 那一步会把后面整段吞掉，脚本却以 0 收尾，
# 外层照样打印"✓ 更新完成"——这正是生产实证过的那个阻断级缺陷。
SCRIPT_UNDER_TEST="${STRUCT_DIR}/structure_removed.sh"
run_case "removing the brace group brings the historical swallow back" \
  TEST_INFO_JSON="${INFO_REAL_PUBLISH}" TEST_TELEGRAM_JSON="${TG_LIVE}" --apply --ref p14-organic --sha "${NEW}"
assert_status 0
assert_contains "${LOG}" "docker <compose> <exec> <-T> <core> <python3> <scripts/preflight.py>"
# 断言的是**后果**而不是"从哪一行开始被吞"：吞掉多少字节取决于 bash 读脚本时的缓冲方式
# （bash 3.2 的 heredoc 是临时文件、可 seek、成块缓冲；bash 5.1+ 的小 heredoc 走管道、
# 逐字节读、吞得更干净）。两种情况下探针循环连同 R1 闸门一起消失、脚本仍以 0 收尾这个
# 后果都成立，所以只钉后果。
assert_not_contains "${RESULT}" "探针  GET /api/v1/system/info 200"
assert_not_contains "${RESULT}" "R1 闸门"
assert_log_count "curl <" 0
# 而外层照样宣布成功——这就是"测试全绿而真实路径是坏的"长什么样
assert_contains "${RESULT}" "✓ 更新完成，端口门禁、容器内门禁和探针均通过"
SCRIPT_UNDER_TEST="${SCRIPT}"

if [[ "${failures}" -ne 0 ]]; then
  printf 'update.sh mechanical tests failed: %s assertion(s)\n' "${failures}" >&2
  exit 1
fi
printf 'update.sh mechanical tests passed: %s case(s)\n' "${cases}"
