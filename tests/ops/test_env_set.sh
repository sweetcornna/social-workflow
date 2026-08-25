#!/bin/bash
# No network, SSH, or Docker: every boundary command is a local argv-recording fake.
#
# 与 tests/ops/ 下另外四份同规格：假件把每次调用的完整 argv 落盘，凭据只出现在**另一份**
# 日志里（TEST_AUTH_LOG），于是"值没进 argv"与"值确实送到了"这两件事能被分开断言。
#
# 【本文件多出来的一层假件：容器环境快照】
# 这是本批次最关键的一条真实语义，假件必须忠实：**容器的环境变量在创建时定型**。
# compose 把 `env_file: .env` 解析进服务配置、写进容器的 Config.Env，之后再没有任何 API
# 能改它；`docker compose restart` 只是 restart 那个已存在的容器，**读不到新的 .env**。
# 所以假 docker 里：
#   `compose up -d --force-recreate ... core` → 把当前 .env 快照到 TEST_CONTAINER_ENV
#   `compose restart core`                    → **刻意不动那份快照**
# 而假 curl 的 /api/v1/system/info 从快照（不是从 .env）里读 use_fake_publishers，
# 鉴权也按快照里的 SW_UI_TOKEN 判。少了这一层，"改完 .env 没重建容器也能看到新值"这种
# 假绿会直接骗过整套测试——本项目已经栽过两次假件语义与真命令不符（假 ssh 透传 argv、
# 假 docker compose exec 不读 stdin），不再重蹈。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="${ROOT}/scripts/ops/env_set.sh"
# 被测脚本路径。默认是仓库里那一份；「纵深防御」与「结构性保证」两节会临时指向改写过的副本。
SCRIPT_UNDER_TEST="${SCRIPT}"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT
mkdir -p "${TMP}/bin" "${TMP}/home dir"
# **刻意不软链到仓库根**（另外四份测试那样做是安全的，它们只读）。本脚本会真的写
# ${HOME}/social_workflow/.env——软链过去就等于在测试里改开发者本机的真 .env。
ENV_DIR="${TMP}/home dir/social_workflow"
CRED_FILE="${TMP}/home dir/.dsh-sw/.credentials.yaml"
BACKUP_DIR="${TMP}/home dir/sw-env-backups"
CONTAINER_ENV="${TMP}/container-env"

cat >"${TMP}/bin/bash" <<'EOF'
#!/bin/bash
if [[ "${1:-}" == */backup.sh ]]; then
  printf 'backup <%s>\n' "$1" >>"${TEST_LOG}"
  exit 0
fi
if [[ "${1:-}" == */restart.sh ]]; then
  # 不短路：restart.sh 必须**真的跑**，本文件的核心断言之一就是"R1 闸门由它来判"。
  printf 'restart-sh <%s>\n' "$1" >>"${TEST_LOG}"
fi
depth="${SW_FAKE_BASH_DEPTH:-0}"
if [[ "${1:-}" == "-s" ]]; then
  printf 'remote-bash <-s> depth=%s\n' "${depth}" >>"${TEST_LOG}"
  # 【测试专用的脚本流捕获模式，与 tests/ops/test_update.sh、test_sidecar.sh 同款】
  # 设了 SW_TEST_DUMP_REMOTE 时，depth=0 那一层（也就是 ssh 真正送达远端的那条脚本流）
  # 把 stdin 原样落盘并以 0 收尾，一行远端正文都不执行。它服务的是"发射进来的两个共享片段
  # 到底落在花括号里面还是外面"这个问题——那件事没法靠行为断言分辨（两种放法在单层 bash 上
  # 都能跑通），只能直接看线上真正发出的那串字节。只在显式设置该变量时生效。
  # 只捕获**第一条**流。本脚本一次调用会经 ssh 送出两条：自己那条，以及收尾时 restart.sh
  # 送出的那条。不加这个条件的话后者会把前者覆盖掉，而断言看的是一份根本不含
  # sw_awaiting_confirm 的流——那正是"测试全绿但测的是别的东西"。
  if [[ -n "${SW_TEST_DUMP_REMOTE:-}" && "${depth}" -eq 0 && ! -s "${SW_TEST_DUMP_REMOTE}" ]]; then
    cat >"${SW_TEST_DUMP_REMOTE}"
    exit 0
  fi
  # depth=0 是 ssh 送达远端的那层状态规范化包装；depth=1 是它转交的内层脚本。
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
# 字符串发给远端，由远端登录 shell 重新分词；stdin（进程替换）原样继承。
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
  # restart.sh 的 R1 闸门与 env_set.sh 的事前预防闸门都走这里。真实 `docker compose exec -T`
  # 把 stdin **转发**给容器内进程，而那个进程自己 sys.stdin.read()。这里先整个收下来、再原样
  # 喂给 python：转发语义与 exec 出去完全一致，但多出一个可断言的观测点。
  #
  # 【为什么要这个观测点】"那条 exec 的 fd 0 到底接的是什么"是本文件唯一能直接证明
  # `{ ... } </dev/null` 承重的信号，而它在别的地方看不出来：不管收到的是空还是一段 shell
  # 脚本正文，python 都会解析失败、退同一个码。所以这里只记一个**分类**——绝不记内容，
  # TEST_LOG 是要被断言"凭据出现 0 次"的那份日志。
  gate_stdin="$(cat)"
  if [[ -z "${gate_stdin}" ]]; then
    printf 'exec-stdin <empty>\n' >>"${TEST_LOG}"
  elif [[ "${gate_stdin}" == '{'* ]]; then
    printf 'exec-stdin <json>\n' >>"${TEST_LOG}"
  else
    printf 'exec-stdin <not-json>\n' >>"${TEST_LOG}"
  fi
  # 三类容器内解析各有各的开关。**按 python 源码里的特征串分辨，不按调用顺序**：
  # 顺序会随脚本改动漂，而"这段 python 在解析哪个端点"是稳定的。
  if [[ "$7" == *use_fake_publishers* ]]; then
    gate_exec_status="${TEST_FAKE_EXEC_STATUS:-0}"
  elif [[ "$7" == *awaiting_confirm* ]]; then
    gate_exec_status="${TEST_AWAITING_EXEC_STATUS:-0}"
  else
    gate_exec_status="${TEST_TELEGRAM_EXEC_STATUS:-0}"
  fi
  if [[ "${gate_exec_status}" -ne 0 ]]; then
    exit "${gate_exec_status}"
  fi
  printf '%s' "${gate_stdin}" | python3 -c "$7"
  exit $?
fi

if [[ "$1 ${2:-} ${3:-} ${4:-} ${5:-} ${6:-}" == "compose up -d --force-recreate --no-build core" ]]; then
  [[ "${TEST_RECREATE_STATUS:-0}" -eq 0 ]] || exit "${TEST_RECREATE_STATUS}"
  # 容器**重建**：环境在这一刻从 .env 定型进容器。这是假件里最要紧的一格。
  if [[ -f "${HOME}/social_workflow/.env" ]]; then
    cp "${HOME}/social_workflow/.env" "${TEST_CONTAINER_ENV}"
  fi
  printf 'container-recreated\n' >>"${TEST_LOG}"
  exit 0
fi

if [[ "$1 ${2:-} ${3:-}" == "compose restart core" ]]; then
  # **刻意不刷新容器环境快照**：真实 `docker compose restart` 不重建容器，也就读不到新的
  # .env（compose 官方文档：配置改动不会被 restart 反映出来）。这条"不做什么"是本文件
  # 里最重要的假件语义之一——它一旦被"顺手补上"，env_set.sh 里那条 up -d 是不是承重的
  # 就再也测不出来了。
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
#   -w <fmt>    传输结束后把 fmt 追加到 stdout；**即便 -f 判失败也照样输出**。
#   -sS         失败时仍往 stderr 写一行错误说明。
#   --config -  从 **stdin** 读配置文件并读到 EOF，解析出 `header = "..."` 行。
# 解析出来的头写进 TEST_AUTH_LOG（**与 argv 日志分开的文件**）。
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
  local body="$1" code="$2" status="$3"
  if [[ "${status}" -eq 0 ]]; then
    printf '%s' "${body}"
  elif [[ "${status}" -eq 22 ]]; then
    printf 'curl: (22) The requested URL returned error: %s\n' "${code}" >&2
  else
    printf 'curl: (%s) fake transport failure\n' "${status}" >&2
  fi
  if [[ -n "${curl_write_out}" ]]; then
    if [[ "${curl_write_out}" != '\n%{http_code}' ]]; then
      printf 'curl-fake-unsupported-write-out <%s>\n' "${curl_write_out}" >>"${TEST_LOG}"
      exit 91
    fi
    printf '\n%s' "${code}"
  fi
  exit "${status}"
}

# ---- core 的运行时配置来自**容器环境快照**，不是来自当前 .env ---------------------
# 见本文件头部说明：容器环境在创建时定型。快照只由假 docker 的 `up -d --force-recreate`
# 刷新，`restart` 刻意不刷新。
read_container_key() {
  local key="$1" line prefix
  prefix="${key}="
  [[ -f "${TEST_CONTAINER_ENV}" ]] || return 1
  while IFS= read -r line || [[ -n "${line}" ]]; do
    case "${line}" in
      "${key}="*) printf '%s' "${line:${#prefix}}"; return 0 ;;
    esac
  done <"${TEST_CONTAINER_ENV}"
  return 1
}
container_fake_pub="$(read_container_key SW_USE_FAKE_PUBLISHERS)" || container_fake_pub="true"
case "${container_fake_pub}" in
  true|false) ;;
  # 不是布尔字面量时用 JSON null：core 那边 pydantic 会直接起不来，取不到布尔量正是
  # R1 闸门"从严裁定"要覆盖的情形。这里刻意不悄悄纠正成 true。
  *) container_fake_pub="null" ;;
esac
container_token="$(read_container_key SW_UI_TOKEN)" || container_token=""

# 模拟 core/api/common.py::require_token：SW_UI_TOKEN 非空时，除 /auth/login 外的
# /api/v1/* 缺头 / 非 Bearer / 值不匹配一律 401。
if [[ -n "${container_token}" && "${curl_url}" == */api/v1/* ]]; then
  if [[ "${curl_auth_header}" != "Authorization: Bearer ${container_token}" ]]; then
    curl_emit '' '401' 22
  fi
fi

if [[ "${curl_url}" == */api/v1/system/telegram ]]; then
  # 第几次探这条 URL（含本次）。TEST_TELEGRAM_DIE_FROM=<n> 让**第 n 次起**返回"轮询线程已死"
  # 的载荷。它构造的是本文件里最要紧的一条真实竞态：事前闸门探的时候通道还活着，等写完
  # .env、重建完容器、restart.sh 那道事后闸门再探时它掉线了。两道闸门之所以都要留着，
  # 靠的就是这一格；没有这个开关，"事前拦可预见的、事后兜写入到生效之间变化的"这句话
  # 在测试里根本无法证明，只能靠读代码相信。
  tg_seen="$(grep -c -F 'system/telegram' "${TEST_LOG}" || true)"
  if [[ -n "${TEST_TELEGRAM_DIE_FROM:-}" && "${tg_seen}" -ge "${TEST_TELEGRAM_DIE_FROM}" ]]; then
    curl_emit '{"ok":true,"data":{"enabled":true,"configured":true,"ready":true,"chat_configured":true,"can_sign":true,"polling":false,"username":"sw_ops_bot","sent":0,"failed":0,"stats":{},"detail":"","last_error":""}}' '200' 0
  fi
  [[ "${TEST_TELEGRAM_CURL_STATUS:-0}" -ne 0 ]] && curl_emit '' '000' "${TEST_TELEGRAM_CURL_STATUS}"
  curl_code="${TEST_TELEGRAM_HTTP_CODE:-200}"
  [[ "${curl_code}" -ge 400 ]] && curl_emit '' "${curl_code}" 22
  # 默认载荷绝不能塞进 ${VAR:-默认} 展开：JSON 里的花括号会提前终止展开、把载荷弄坏。
  if [[ "${TEST_TELEGRAM_JSON+x}" == x ]]; then
    curl_emit "${TEST_TELEGRAM_JSON}" "${curl_code}" 0
  fi
  # 【本轮补的一层保真度：enabled 跟着容器快照走】SW_TELEGRAM_ENABLED 进白名单之后，
  # "关掉它会怎样"成了可测的东西——但只有假 core 真的按快照回答 enabled，才测得出来。
  # 真实语义：core/config.py:351 的 sw_telegram_enabled → core/telegram.py:650-654
  # build_telegram_notifier() 返回 None，长轮询线程也不会起。所以 enabled=false 时
  # polling 必然也是 false；ready 只看 token+chat_id，不受总开关影响，保持 true。
  # 少了这一层，"拆掉载体之后事后闸门必然判红"就只能靠读代码相信——而那正是
  # confirm_carrier 这道事前闸门存在的全部理由。
  container_tg="$(read_container_key SW_TELEGRAM_ENABLED)" || container_tg="true"
  if [[ "${container_tg}" == "false" ]]; then
    curl_emit '{"ok":true,"data":{"enabled":false,"configured":true,"ready":true,"chat_configured":true,"can_sign":true,"polling":false,"username":"sw_ops_bot","sent":0,"failed":0,"stats":{},"detail":"SW_TELEGRAM_ENABLED=false","last_error":""}}' "${curl_code}" 0
  fi
  curl_emit '{"ok":true,"data":{"enabled":true,"configured":true,"ready":true,"chat_configured":true,"can_sign":true,"polling":true,"username":"sw_ops_bot","sent":3,"failed":0,"stats":{},"detail":"","last_error":""}}' "${curl_code}" 0
fi
# ---- /api/v1/dashboard：签名密钥轮换闸门读"待人点的确认卡条数"的那条 -----------------
# 【形状照着 core/api/dashboard.py 的响应模型抄，不是随手编的】Envelope{ok,data,error} 外壳
# 裹一个 DashboardOut{generated_at,window_days,counters,budget,platforms,attention,events}，
# counters 是 Counters 的**全部** 12 个字段。少字段会让"字段缺失"那条降级路径变得测不出来；
# 而 events[].title / attention[].name 是真端点**真会返回**的自由文本，这里塞进哨兵串，
# 用来反过来证明闸门没有把它们打出来。tests/ops/test_verify.sh 用的是同一份形状。
if [[ "${curl_url}" == */api/v1/dashboard || "${curl_url}" == */api/v1/dashboard\?* ]]; then
  [[ "${TEST_DASHBOARD_CURL_STATUS:-0}" -ne 0 ]] && curl_emit '' '000' "${TEST_DASHBOARD_CURL_STATUS}"
  curl_code="${TEST_DASHBOARD_HTTP_CODE:-200}"
  [[ "${curl_code}" -ge 400 ]] && curl_emit '' "${curl_code}" 22
  # 默认载荷绝不能塞进 ${VAR:-默认} 展开：JSON 里的花括号会提前终止展开、把载荷弄坏。
  if [[ "${TEST_DASHBOARD_JSON+x}" == x ]]; then
    curl_emit "${TEST_DASHBOARD_JSON}" "${curl_code}" 0
  fi
  dashboard_awaiting="${TEST_AWAITING_CONFIRM:-0}"
  curl_emit '{"ok":true,"data":{"generated_at":"2026-08-22T02:00:00Z","window_days":1,"counters":{"pending_review":1,"published_today":2,"published_7d":9,"failed":0,"dead_letter":0,"scheduled":5,"suspended":0,"awaiting_confirm":'"${dashboard_awaiting}"',"rendering":0,"accounts_needing_relogin":0,"accounts_degraded":0,"accounts_suspended":0},"budget":{"token":{"used":1.5,"limit":10.0,"remaining":8.5}},"platforms":[{"platform":"xhs","accounts":1,"ok":1,"degraded":0,"needs_relogin":0,"banned":0,"suspended":0,"pending_review":1,"scheduled":5,"published":9,"used_today":2,"daily_limit":3}],"attention":[{"account_id":"acc-1","name":"SENTINEL_ACCOUNT_NAME","platform":"xhs","status":"needs_relogin","suspended":2}],"events":[{"kind":"review_log","at":"2026-08-22T01:00:00Z","actor":"operator","action":"approve","item_id":"itm-1","title":"SENTINEL_ITEM_TITLE","account_id":"acc-1","detail":"SENTINEL_EVENT_DETAIL","url":null}]},"error":null}' "${curl_code}" 0
fi
[[ "${TEST_CURL_STATUS:-0}" -ne 0 ]] && curl_emit '' '000' "${TEST_CURL_STATUS}"
curl_code="${TEST_INFO_HTTP_CODE:-200}"
[[ "${curl_code}" -ge 400 ]] && curl_emit '' "${curl_code}" 22
curl_emit '{"ok":true,"data":{"version":"0.1.0","env":"prod","time":"2026-08-22T02:00:00Z","timezone":"Asia/Shanghai","scheduler_enabled":true,"generate_enabled":true,"use_fake_publishers":'"${container_fake_pub}"',"auth_required":false,"publishers":["xhs","douyin"]}}' "${curl_code}" 0
EOF

cat >"${TMP}/bin/sleep" <<'EOF'
#!/bin/bash
exit 0
EOF

# 假 mv：默认透传给真 mv。TEST_MV_FAIL_SUBSTR 命中目标路径时失败——用来在零网络下考察
# "写入是原子的"：mv 失败后 .env 必须仍是**旧的完整内容**，而不是被就地改了一半。
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

# 假 openssl / od：只在需要考察"熵源不可用"时被 TEST_NO_ENTROPY 打开，否则透传给真命令。
# 假 openssl / od：默认透传给真命令。两个开关分别构造两种**不同**的熵源故障：
#   TEST_NO_ENTROPY     命令直接失败（装都没装 / 跑不起来）
#   TEST_SHORT_ENTROPY  命令**成功**但吐出一个短得离谱的值——这一格只有生成端的自检拦得住，
#                       而"熵源静默降级"恰恰是最危险的那种：一个 3 位的 token 与没有 token
#                       的区别只在纸面上，却不会有任何一条命令报错。
cat >"${TMP}/bin/openssl" <<'EOF'
#!/bin/bash
[[ -z "${TEST_NO_ENTROPY:-}" ]] || exit 1
if [[ -n "${TEST_SHORT_ENTROPY:-}" ]]; then printf 'abc\n'; exit 0; fi
for candidate in /usr/bin/openssl /bin/openssl /usr/local/bin/openssl /opt/homebrew/bin/openssl; do
  [[ -x "${candidate}" ]] && exec "${candidate}" "$@"
done
exit 127
EOF
cat >"${TMP}/bin/od" <<'EOF'
#!/bin/bash
[[ -z "${TEST_NO_ENTROPY:-}" ]] || exit 1
if [[ -n "${TEST_SHORT_ENTROPY:-}" ]]; then printf ' ab cd\n'; exit 0; fi
for candidate in /usr/bin/od /bin/od; do
  [[ -x "${candidate}" ]] && exec "${candidate}" "$@"
done
exit 127
EOF

chmod +x "${TMP}/bin/"*

failures=0
cases=0
case_name=""
RESULT=""
STATUS=0
LOG=""
AUTH_LOG=""
ENV_AFTER=""

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

# 输出里"A 必须排在 B 前面"。备份先于写入这条断言靠它。
assert_order() {
  local haystack="$1" first="$2" second="$3" head_part
  if [[ "${haystack}" != *"${first}"* ]]; then
    fail_assertion "missing [${first}]; value: ${haystack}"
    return
  fi
  head_part="${haystack%%"${second}"*}"
  if [[ "${head_part}" == "${haystack}" ]]; then
    fail_assertion "missing [${second}]; value: ${haystack}"
    return
  fi
  [[ "${head_part}" == *"${first}"* ]] \
    || fail_assertion "[${first}] 没有排在 [${second}] 前面; value: ${haystack}"
}

file_mode() {
  # macOS 与 GNU 的 stat 参数不同，两条都试——但**不能靠退出码分辨**：
  # BSD 的 `-f` 是「格式串」，GNU 的 `-f` 是「看文件系统状态」。GNU stat 拿到
  # `-f '%Lp'` 会**成功**并打出一段 `  File: "..."` 的文件系统信息，`||` 那条退路
  # 根本轮不到执行，assert_mode 于是拿着这段文字去比 0600。2026-08-25 在 Linux CI
  # 上实测炸了 8 条。所以判据换成「输出像不像一个八进制权限」，而不是「命令有没有失败」。
  local mode
  mode="$(stat -c '%a' "$1" 2>/dev/null)"   # GNU coreutils
  if [[ ! "${mode}" =~ ^[0-7]{3,4}$ ]]; then
    mode="$(stat -f '%Lp' "$1" 2>/dev/null)"  # BSD / macOS
  fi
  if [[ ! "${mode}" =~ ^[0-7]{3,4}$ ]]; then
    printf 'file_mode: 两种 stat 都没给出权限位（%s）\n' "$1" >&2
    return 1
  fi
  printf '%s' "${mode}"
}

assert_mode() {
  local path="$1" expected="$2" actual
  actual="$(file_mode "${path}")"
  [[ "${actual}" == "${expected}" ]] \
    || fail_assertion "权限 ${path}=${actual}，期望 ${expected}"
}

# 一份接近生产形态的 .env 夹具：里面有凭据行，用来钉死"本脚本只碰白名单键、其余逐字保留"。
ENV_DEFAULT='SW_LLM_BACKEND=dsh
DEEPSEEK_API_KEY=sk-TESTFAKE-not-a-real-key
SW_ENV=prod

# P12 Telegram 无人值守确认通道
TELEGRAM_BOT_TOKEN=123456:TESTFAKE
SW_USE_FAKE_PUBLISHERS=true
'
ENV_CONTENT="${ENV_DEFAULT}"
ENV_MISSING=0
CRED_CONTENT=""
CRED_MISSING=1
# 夹具建好之后、被测脚本跑起来之前执行的钩子，用来摆放障碍物（例如把备份目录做成一个文件）。
RUN_CASE_HOOK=""

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
  rm -rf "${ENV_DIR}" "${BACKUP_DIR}" "${TMP}/home dir/.dsh-sw"
  mkdir -p "${ENV_DIR}"
  if [[ "${ENV_MISSING}" -eq 0 ]]; then
    printf '%s' "${ENV_CONTENT}" >"${ENV_DIR}/.env"
    chmod 600 "${ENV_DIR}/.env"
    # 当前"正在跑的容器"是按这份 .env 创建的。
    cp "${ENV_DIR}/.env" "${CONTAINER_ENV}"
  else
    : >"${CONTAINER_ENV}"
  fi
  if [[ "${CRED_MISSING}" -eq 0 ]]; then
    mkdir -p "${TMP}/home dir/.dsh-sw"
    printf '%s' "${CRED_CONTENT}" >"${CRED_FILE}"
    chmod 600 "${CRED_FILE}"
  fi
  [[ -z "${RUN_CASE_HOOK}" ]] || "${RUN_CASE_HOOK}"
  set +e
  # `env -u SW_OPS_UI_TOKEN` 让每个用例从"本机没配 token"这个确定起点开始。
  # TEST_AUTH_LOG 与 TEST_LOG 是**两个文件**：TEST_LOG 记 argv，要被断言"值出现 0 次"；
  # TEST_AUTH_LOG 记 curl 从 --config - 里真正解析出的头，要被断言"确实送到了"。
  local -a bash_opts=()
  [[ "${RUN_XTRACE:-0}" -eq 0 ]] || bash_opts=(-x)
  RESULT="$(env -u SW_OPS_UI_TOKEN ${env_args[@]+"${env_args[@]}"} \
    PATH="${TMP}/bin:${PATH}" \
    HOME="${TMP}/home dir" \
    TEST_LOG="${TMP}/log" \
    TEST_AUTH_LOG="${TMP}/auth" \
    TEST_CONTAINER_ENV="${CONTAINER_ENV}" \
    /bin/bash ${bash_opts[@]+"${bash_opts[@]}"} "${SCRIPT_UNDER_TEST}" "$@" 2>&1)"
  STATUS=$?
  set -e
  LOG="$(<"${TMP}/log")"
  AUTH_LOG="$(<"${TMP}/auth")"
  if [[ -f "${ENV_DIR}/.env" ]]; then
    ENV_AFTER="$(<"${ENV_DIR}/.env")"
  else
    ENV_AFTER=""
  fi
}

# 从一份 bash 源码里抽出某个函数的完整函数体（含函数头与收尾的 `}`）。
# **用逐字比较函数头，不用正则**：正则要穿过 shell → awk 两层转义，上一版就是在这里被
# 吃掉反斜杠、抽出空串的。抽不出来时返回空串，由调用点判红——绝不让"空 == 空"变成绿。
extract_fn_body() {
  local file="$1" fn="$2"
  awk -v hdr="${fn}() {" '$0 == hdr {on = 1} on {print; if ($0 == "}") exit}' "${file}"
}

# 从一段函数体里取出所有 case 标签上的键名（把 `A|B|C)` 拆成三行）。
extract_case_keys() {
  sed -n 's/^    \([A-Z][A-Z0-9_|]*\))[[:space:]]*.*$/\1/p' | tr '|' '\n' | sed '/^$/d'
}

# 写入路径上不许留下临时文件：`.env` 就在 git 工作树里，一个残留的临时文件会让
# verify.sh 判"工作树不干净"、让 update.sh 拒绝部署。
assert_no_tmp_residue() {
  local leftovers
  leftovers="$(find "${ENV_DIR}" -maxdepth 1 -name '.env.sw-ops-tmp.*' 2>/dev/null || true)"
  [[ -z "${leftovers}" ]] || fail_assertion "写入路径留下了临时文件：${leftovers}"
  leftovers="$(find "${TMP}/home dir/.dsh-sw" -maxdepth 1 -name '*.sw-ops-tmp.*' 2>/dev/null || true)"
  [[ -z "${leftovers}" ]] || fail_assertion "凭据写入路径留下了临时文件：${leftovers}"
}

assert_value_absent_from_argv() {
  local value="$1" hits
  hits="$(grep -F -c -- "${value}" <<<"${LOG}" || true)"
  if [[ "${hits}" -ne 0 ]]; then
    fail_assertion "值的明文在 argv 记录里出现了 ${hits} 次，必须是 0；log: ${LOG}"
  fi
}

# 通道活着的载荷就是假 curl 的默认值（见上面那份假件），所以这里只列需要显式构造的三种坏情形。
TG_NOT_POLLING='{"ok":true,"data":{"enabled":true,"configured":true,"ready":true,"chat_configured":true,"can_sign":true,"polling":false,"username":"sw_ops_bot","sent":0,"failed":0,"stats":{},"detail":"","last_error":""}}'
TG_DISABLED='{"ok":true,"data":{"enabled":false,"configured":true,"ready":true,"chat_configured":true,"can_sign":true,"polling":true,"username":"sw_ops_bot","sent":0,"failed":0,"stats":{},"detail":"","last_error":""}}'

UI_TOKEN='TESTTOKEN_envset-A1b2+/=.:@'

# =============================================================== 参数与白名单
# 白名单写死在脚本里。这里钉死的不只是"拒绝"，还有**在碰生产之前就拒绝**：
# 备份、ssh、docker 一律不许发生。
assert_rejected_before_remote() {
  assert_status 1
  assert_not_contains "${LOG}" "ssh <"
  assert_not_contains "${LOG}" "docker <"
  assert_not_contains "${LOG}" "backup <"
  assert_not_contains "${LOG}" "restart-sh <"
}

run_case "a key outside the whitelist is refused" --key SW_DATABASE_URL --value x
assert_rejected_before_remote
assert_contains "${RESULT}" "键 SW_DATABASE_URL 不在白名单里，拒绝执行"
assert_contains "${RESULT}" "白名单**写死在脚本里，不接受运行时扩展**，当前是："
assert_contains "${RESULT}" "而不是把这里改成通用编辑器"
# 扩容之后这条提示还得如实说清"凭据类键仍然没有路径"——第 14 条不因本轮而关闭。
# 扩容之后这条提示要如实说清两件事：哪些凭据类键**已经**有了路径，哪些**仍然**没有。
# 这份凭据类键名单是从 SW_ENV_WHITELIST **派生**的（sw_env_keys_where），不是手写的第二份。
# 派生出来的顺序就是白名单里的顺序，所以这里可以逐字断言。
assert_contains "${RESULT}" "名单上的凭据类键是：SW_UI_TOKEN、SW_TELEGRAM_SIGNING_SECRET、TELEGRAM_BOT_TOKEN"
assert_contains "${RESULT}" "其余凭据类键（TELEGRAM_CHAT_ID / 各种 API key）**刻意仍不在名单上**"
# TELEGRAM_CHAT_ID 的"为什么还不能加"必须**说得出理由**，不是一句"还没做"。
assert_contains "${RESULT}" "它的值是**从生产流出来**的"

run_case "a telegram key is refused too（白名单不是「看起来危险才拦」）" --key TELEGRAM_CHAT_ID --value x
assert_rejected_before_remote
assert_contains "${RESULT}" "不在白名单里"

# 名字只差一截、处境天差地别：bot token 本轮进了白名单，chat_id 没有。拒绝理由必须**点名**
# 后者缺的到底是什么，而不是含糊地说"凭据类键都不加"——那句话现在已经不成立了。
# 【为什么 chat_id 这一轮刻意不加】它的闸门本质上验证不了：没有真发一条 Telegram 消息，
# 就确认不了新会话可达；而且它的值是**从生产流出来**的（要在服务器上跑 core.telegram setup
# 才知道），现有的表模型不了"值从生产往本机流"这个方向。那是另一件事，不是顺手能补的一格。
run_case "the bot token is in now but TELEGRAM_CHAT_ID still is not" \
  --key TELEGRAM_CHAT_ID --value 123456789
assert_rejected_before_remote
assert_contains "${RESULT}" "键 TELEGRAM_CHAT_ID 不在白名单里"
assert_contains "${RESULT}" "TELEGRAM_CHAT_ID 尤其别顺手加"
assert_contains "${RESULT}" "不真发一条 Telegram 消息就验不了"
# 反过来钉住：bot token 现在**不该**再出现在"仍不在名单上"那一句里。
assert_not_contains "${RESULT}" "其余凭据类键（TELEGRAM_BOT_TOKEN"

run_case "SW_UI_TOKEN refuses --value（凭据不进 argv）" --key SW_UI_TOKEN --value hunter2
assert_rejected_before_remote
assert_contains "${RESULT}" "SW_UI_TOKEN 不接受 --value"
assert_contains "${RESULT}" "/proc/*/cmdline 世界可读"
assert_contains "${RESULT}" "改用 --generate"

run_case "SW_USE_FAKE_PUBLISHERS rejects a non-boolean" --key SW_USE_FAKE_PUBLISHERS --value TRUE
assert_rejected_before_remote
assert_contains "${RESULT}" "SW_USE_FAKE_PUBLISHERS 的值不合法（当前给的是：TRUE）"
assert_contains "${RESULT}" "true 或 false —— 只认这两个**小写单词**"
# 文案必须**明说**这是主动收紧，不能再说"写了不生效"——pydantic 其实认 TRUE，
# 旧文案那句话是错的，本轮已更正；这条断言把更正后的口径钉住。
assert_contains "${RESULT}" "**拒绝不等于「写了不生效」**"

run_case "SW_USE_FAKE_PUBLISHERS rejects yes" --key SW_USE_FAKE_PUBLISHERS --value yes
assert_rejected_before_remote

run_case "SW_UI_TOKEN needs a value source" --key SW_UI_TOKEN
assert_rejected_before_remote
assert_contains "${RESULT}" "需要 --generate 或 --from-credentials"

run_case "--generate and --from-credentials are mutually exclusive" \
  --key SW_UI_TOKEN --generate --from-credentials
assert_rejected_before_remote
assert_contains "${RESULT}" "只能选一个"

run_case "no arguments prints usage"
assert_status 2
assert_contains "${RESULT}" "白名单（十二个键，写死在脚本里，**不接受运行时扩展**）："
# usage 里那份清单是**手写**的，而它旁边那个键数之所以允许留着，理由是"枚举就在紧下方、
# 数字自证"。那个理由只有在**枚举本身也被钉住**时才成立——否则手写清单漏一个键，数字与
# 清单一起错，谁也发现不了。所以这里逐键断言它出现在 usage 输出里。
usage_expected_keys="$(sed -n 's/^SW_ENV_WHITELIST="\(.*\)"$/\1/p' "${SCRIPT}")"
if [[ -z "${usage_expected_keys}" ]]; then
  fail_assertion "抽不出 SW_ENV_WHITELIST——抽取器失配了，下面的逐键断言会退化成空转"
fi
usage_seen=0
for usage_key in ${usage_expected_keys}; do
  usage_seen=$((usage_seen + 1))
  assert_contains "${RESULT}" "  ${usage_key} "
done
if [[ "${usage_seen}" -ne 12 ]]; then
  fail_assertion "白名单里有 ${usage_seen} 个键，usage 那行写着「十二个键」——两处必须一起改"
fi
assert_contains "${RESULT}" "SW_LLM_BACKEND               anthropic|dsh"
assert_contains "${RESULT}" "SW_TELEGRAM_SIGNING_SECRET   凭据"
# bot token 那一行必须把它与另外两个凭据类键的**差别**说出来，否则人会照着 --generate 去试。
assert_contains "${RESULT}" "TELEGRAM_BOT_TOKEN           凭据          **只走 --from-credentials**"
assert_contains "${RESULT}" "本机造不出来，--generate 会被拒绝并告诉你怎么办"
assert_contains "${RESULT}" "**不是每个凭据类键都能这样**"
# override 必须在 usage 里露面，而且那一行本身就要说出后果——名字是它唯一的说明书。
assert_contains "${RESULT}" "--accept-breaking-pending-confirm-cards"
assert_contains "${RESULT}" "明知**已推出去还没人点的确认卡会因此失效**"
assert_contains "${RESULT}" "DAILY_TOKEN_BUDGET           非负整数      0 = 当天全停，**不是**「不限」"

run_case "an unknown flag prints usage" --wat
assert_status 2
assert_contains "${RESULT}" "无法识别的参数：--wat"

# ==================================================================== --show
run_case "--show reports presence without echoing the credential" --show
assert_status 0
assert_contains "${RESULT}" "$(printf '  %-28s 已设置  %s' SW_USE_FAKE_PUBLISHERS true)"
assert_contains "${RESULT}" "$(printf '  %-28s 未设置' SW_UI_TOKEN)"
assert_contains "${RESULT}" "只读：不备份、不写入、不重建容器、不重启"
# 只读就是只读：不许出现备份、写入、重建、重启。
assert_not_contains "${LOG}" "docker <"
assert_not_contains "${LOG}" "backup <"
assert_not_contains "${LOG}" "restart-sh <"
# .env 里其余的行（含两处凭据）一个字节都不许进输出。
# 【见证行本轮换过】原来用 SW_LLM_BACKEND 当"不该出现的非白名单键"，而它这一轮进了白名单，
# 必须换一个仍在白名单外的见证，否则这条断言会悄悄退化成永真。
assert_not_contains "${RESULT}" "sk-TESTFAKE-not-a-real-key"
assert_not_contains "${RESULT}" "123456:TESTFAKE"
assert_not_contains "${RESULT}" "SW_ENV=prod"
assert_not_contains "${RESULT}" "P12 Telegram 无人值守确认通道"
# TELEGRAM_BOT_TOKEN 本轮进了白名单，所以**键名会出现**（这是对的：--show 的职责就是逐键
# 回答"设了没有"）。要钉的是它按 secret 策略走：只报"已设置"，值一个字节都不出来。
# 【这条断言换过一次，理由记一笔】上一版这里断言的是"键名不出现"，那时它借着"不在白名单上"
# 顺带当了一次见证行；键一进白名单，那条断言就会红——而它红得对，只是原来的意思没了。
# 见证行改由 SW_ENV=prod / sk-TESTFAKE-... / 那行注释承担，它们仍然在白名单之外。
assert_contains "${RESULT}" "$(printf '  %-28s 已设置（凭据，值不回显：红线 R5）' TELEGRAM_BOT_TOKEN)"

# ---- --show 覆盖**全部**十一个白名单键，而且是从 SW_ENV_WHITELIST 派生的 ----------
# 「白名单加了键但 --show 看不见」是一种无声的漏法：人跑一次 --show，看不到那个键，
# 就以为它没被这套工具管着。这里逐键断言它出现在输出里，一个都不许少。
case_name="--show covers every whitelisted key"
show_expected_keys="$(sed -n 's/^SW_ENV_WHITELIST="\(.*\)"$/\1/p' "${SCRIPT}")"
if [[ -z "${show_expected_keys}" ]]; then
  fail_assertion "抽不出 SW_ENV_WHITELIST——抽取器失配了，下面的逐键断言会退化成空转"
fi
show_seen=0
for show_key in ${show_expected_keys}; do
  show_seen=$((show_seen + 1))
  assert_contains "${RESULT}" "$(printf '  %-28s ' "${show_key}")"
done
if [[ "${show_seen}" -ne 12 ]]; then
  fail_assertion "白名单里有 ${show_seen} 个键，本轮预期 12 个；改了键数就回来同步这条断言"
fi

# ---- 等价别名：主名没设、别名设了，绝不能只答"未设置" --------------------------
# core/config.py:375-378 的 AliasChoices 让 SW_DAILY_IMAGE_BUDGET 与 DAILY_IMAGE_BUDGET
# 是同一个字段。只看主名会答"未设置"，人据此以为出厂默认值 40 在生效——那是一次很确定的错答。
ENV_CONTENT="${ENV_DEFAULT}SW_DAILY_IMAGE_BUDGET=5
"
run_case "--show does not call an alias-only key unset" --show
assert_status 0
assert_contains "${RESULT}" "主名未设置，但等价别名 SW_DAILY_IMAGE_BUDGET=5 在 .env 里"
assert_contains "${RESULT}" "**生效的是它**，不是出厂默认值"

ENV_CONTENT="${ENV_DEFAULT}DAILY_IMAGE_BUDGET=40
SW_DAILY_IMAGE_BUDGET=5
"
run_case "--show says which one wins when both the key and its alias are set" --show
assert_status 0
assert_contains "${RESULT}" "$(printf '  %-28s 已设置  %s' DAILY_IMAGE_BUDGET 40)"
assert_contains "${RESULT}" "两者都在时**主名赢**，别名那行是死配置"
ENV_CONTENT="${ENV_DEFAULT}"

ENV_CONTENT="${ENV_DEFAULT}SW_UI_TOKEN=${UI_TOKEN}
"
run_case "--show never prints the token value" --show
assert_status 0
assert_contains "${RESULT}" "$(printf '  %-28s 已设置（凭据，值不回显：红线 R5）' SW_UI_TOKEN)"
assert_not_contains "${RESULT}" "${UI_TOKEN}"
assert_value_absent_from_argv "${UI_TOKEN}"

ENV_CONTENT="${ENV_DEFAULT}SW_USE_FAKE_PUBLISHERS=false
"
run_case "--show flags a duplicated key" --show
assert_status 0
assert_contains "${RESULT}" "同名键出现 2 次"
ENV_CONTENT="${ENV_DEFAULT}"

ENV_MISSING=1
run_case "--show refuses when the remote .env is absent" --show
assert_status 30
assert_contains "${RESULT}" "不存在，无法回答任何一个键的状态"
ENV_MISSING=0

# ================================================= SW_USE_FAKE_PUBLISHERS=false
# 本批次的核心：这一次变更此前完全在工具面之外手工进行，也就完全绕过了 R1 闸门。
run_case "flipping to false backs up, writes, recreates and clears the R1 gate" \
  --key SW_USE_FAKE_PUBLISHERS --value false
assert_status 0
# ① 备份先于写入——顺序本身就是断言。
assert_order "${RESULT}" ".env 备份" ".env 写入"
assert_contains "${RESULT}" ".env 备份  ${TMP}/home dir/sw-env-backups/env-"
assert_contains "${RESULT}" ".env 写入  SW_USE_FAKE_PUBLISHERS 已就地替换（原子：临时文件 + mv）"
# ② 值真的落到 .env 上了，且是**替换**不是追加。
assert_contains "${ENV_AFTER}" "SW_USE_FAKE_PUBLISHERS=false"
assert_not_contains "${ENV_AFTER}" "SW_USE_FAKE_PUBLISHERS=true"
# ③ .env 里其余每一行逐字保留。
assert_contains "${ENV_AFTER}" "DEEPSEEK_API_KEY=sk-TESTFAKE-not-a-real-key"
assert_contains "${ENV_AFTER}" "# P12 Telegram 无人值守确认通道"
assert_mode "${ENV_DIR}/.env" 600
# ④ 让它生效的是 up -d --force-recreate，不是 restart。
assert_log_count "container-recreated" 1
# ⑤ R1 闸门由 restart.sh 判，而且它这次真的看到了 false（容器已按新 .env 重建）。
assert_contains "${LOG}" "restart-sh <"
assert_contains "${RESULT}" "R1 闸门  真发布已开启（模拟发布器=false）：人工确认闸门通道 enabled=true ready=true polling=true"
assert_contains "${RESULT}" "✓ core 已重启、探针恢复 200，R1 确认闸门通道已核验"
assert_contains "${RESULT}" "✓ 生产 .env 已变更、已生效，restart.sh 与 R1 闸门均已跑完"
# ⑥ 事前把真发布这件事说清楚，并如实承认闸门是事后检测。
assert_contains "${RESULT}" "SW_USE_FAKE_PUBLISHERS=false 表示**真发布开启**"
assert_contains "${RESULT}" "事后那道仍然是**事后检测**"
# 事前那道闸门也真的跑过了，而且它的放行话术只声称"此刻"，不声称"以后也会活着"。
assert_contains "${RESULT}" "事前闸门  人工确认闸门通道 enabled=true ready=true polling=true，允许写入"
assert_contains "${RESULT}" "只代表此刻；写入到生效之间仍可能变化"
# 顺序本身就是断言：探完才备份，备份完才写。
assert_order "${RESULT}" "事前闸门" ".env 备份"
assert_no_tmp_residue

# ================================================== 事前预防闸门（写 .env 之前）
# docs/RISKS.md §12.3：这道闸门本轮才补上，它拦的是**可预见**的那一半——动手时通道就已经
# 死了。判定通过时 .env 一个字节都不动、连备份都不建，生产完全不知道有人来过。
# 下面每一条都同时钉住"拒绝了"和"什么都没做"这两件事：只断言退出码的话，一个"写完了才
# 拒绝"的实现照样能全绿，而那正是事前预防要消灭的东西。
assert_precheck_refused() {
  local want_status="$1"
  assert_status "${want_status}"
  # ① 值没落到 .env 上——这是"事前"与"事后"最本质的区别。
  assert_contains "${ENV_AFTER}" "SW_USE_FAKE_PUBLISHERS=true"
  assert_not_contains "${ENV_AFTER}" "SW_USE_FAKE_PUBLISHERS=false"
  # ② 连备份都没建（本脚本的规矩是备份先于写入；既然没写，就不该有备份）。
  assert_not_contains "${RESULT}" ".env 备份  "
  [[ ! -d "${BACKUP_DIR}" ]] || fail_assertion "事前拒绝时不该生成备份目录"
  # ③ 容器没被重建、restart.sh 没被调起——整条后半程根本没开始。
  assert_log_count "container-recreated" 0
  assert_not_contains "${LOG}" "restart-sh <"
  # ④ 绝不打印成功行，也绝不打印那些只在"已经写了"的世界里才成立的话。
  assert_not_contains "${RESULT}" "✓ 生产 .env 已变更、已生效"
  assert_not_contains "${RESULT}" "真发布**已经开启**"
  assert_no_tmp_residue
}

run_case "a dead polling thread is refused before anything is written" \
  TEST_TELEGRAM_JSON="${TG_NOT_POLLING}" --key SW_USE_FAKE_PUBLISHERS --value false
assert_precheck_refused 37
assert_contains "${RESULT}" "事前预防闸门：人工确认闸门通道不可用"
assert_contains "${RESULT}" "ready=true 但 polling=false"
assert_contains "${RESULT}" '这是"探到了，不行"'
assert_contains "${RESULT}" "**.env 一个字节都没动**"
assert_contains "${RESULT}" "真发布**没有**被打开"

run_case "the telegram master switch being off is refused before writing too" \
  TEST_TELEGRAM_JSON="${TG_DISABLED}" --key SW_USE_FAKE_PUBLISHERS --value false
assert_precheck_refused 37
assert_contains "${RESULT}" "总开关 enabled=false"

# "探不到"与"探到了不行"必须分开：不同的退出码、不同的文案。混成一种的话，运维会拿着
# "去修 Telegram 配置"的指引去查一个其实是网络/凭据的问题。
run_case "an unreachable probe is could-not-tell, not a channel verdict" \
  TEST_TELEGRAM_CURL_STATUS=7 --key SW_USE_FAKE_PUBLISHERS --value false
assert_precheck_refused 38
assert_contains "${RESULT}" "事前预防闸门**探不到**人工确认闸门通道"
assert_contains "${RESULT}" '这是"没探到"，不是"探到了不行"'
assert_contains "${RESULT}" "fail-closed"
# 不许把"不知道"说成"通道不活"：那句只在"探到了"的世界里才成立的裁定不能出现。
# （"polling"这个词本身会出现在开头那几行说明里，所以钉的是裁定句，不是词。）
assert_not_contains "${RESULT}" "人工确认闸门通道不可用"
assert_not_contains "${RESULT}" '这是"探到了，不行"'

# 401 也属于"没探到"，但处置完全不同（缺的是运维侧凭据），所以要单独给指引。
ENV_CONTENT="${ENV_DEFAULT}SW_UI_TOKEN=deadbeefcafe
"
run_case "a 401 on the pre-check is could-not-tell with the credential hint" \
  --key SW_USE_FAKE_PUBLISHERS --value false
assert_precheck_refused 38
assert_contains "${RESULT}" "返回 401 未授权"
assert_contains "${RESULT}" "core 正常应答了，缺的是运维侧凭据"
assert_contains "${RESULT}" "export SW_OPS_UI_TOKEN="

# 反过来：**带着匹配的 token 时事前闸门必须能探通**。这一格钉的是一条真实的自锁——闸门要打
# /api/v1，而 core 启用 SW_UI_TOKEN 之后不带头的探针一律 401；如果本脚本忘了把本机持有的
# token 也送进远端脚本流，那么在一台已经启用鉴权的生产上，这道闸门会**永远**停在 401，
# 而它给出的"export SW_OPS_UI_TOKEN=…"指引又根本不生效（本机导出了也送不到远端）——
# 人被锁死在关不掉假发布器的状态里，还拿着一句错的指引。少了这一格，那个缺陷全绿。
run_case "a matching token lets the pre-check actually reach the channel" \
  SW_OPS_UI_TOKEN=deadbeefcafe --key SW_USE_FAKE_PUBLISHERS --value false
assert_status 0
assert_contains "${RESULT}" "事前闸门  人工确认闸门通道 enabled=true ready=true polling=true，允许写入"
assert_contains "${RESULT}" "✓ 生产 .env 已变更、已生效"
assert_contains "${ENV_AFTER}" "SW_USE_FAKE_PUBLISHERS=false"
# token 确实是经 stdin 流送到远端、再由 curl 从 `--config -` 里读走的，而不是进了 argv。
assert_contains "${AUTH_LOG}" "url <http://127.0.0.1:8000/api/v1/system/telegram> header <Authorization: Bearer deadbeefcafe>"
assert_value_absent_from_argv "deadbeefcafe"
ENV_CONTENT="${ENV_DEFAULT}"

# 响应拿到了却解析不了，本质仍是"不知道通道活不活"，归到"没探到"那一档，而不是判它不活。
run_case "an unparseable telegram payload is could-not-tell too" \
  TEST_TELEGRAM_JSON='not json at all' --key SW_USE_FAKE_PUBLISHERS --value false
assert_precheck_refused 38
assert_contains "${RESULT}" "无法解析 /api/v1/system/telegram 的响应"

# ============================ 事后那道闸门必须还在（事前不是用来替换它的）
# 构造两道闸门之间的真实竞态：事前探的时候通道活着（第 1 次），写完 .env、重建完容器之后
# restart.sh 再探（第 2 次）时它掉线了。事前拦不住这一半——只有事后那道拦得住。
# 这一格红了就说明有人"用事前替换掉了事后"，而那会让这半类风险彻底失去防线。
run_case "the after-the-fact gate still catches a channel that dies mid-flight" \
  TEST_TELEGRAM_DIE_FROM=2 --key SW_USE_FAKE_PUBLISHERS --value false
assert_status 1
# 事前放行了。
assert_contains "${RESULT}" "事前闸门  人工确认闸门通道 enabled=true ready=true polling=true，允许写入"
# 写入与重建都真的发生了——所以这一次是"已经开启"的世界。
assert_contains "${ENV_AFTER}" "SW_USE_FAKE_PUBLISHERS=false"
assert_log_count "container-recreated" 1
assert_contains "${LOG}" "restart-sh <"
# 事后闸门接住了它，并且 fail-closed。
assert_contains "${RESULT}" "R1 红线闸门未通过"
assert_contains "${RESULT}" "ready=true 但 polling=false"
assert_contains "${RESULT}" "真发布**已经开启**"
assert_contains "${RESULT}" "bash scripts/ops/env_set.sh --key SW_USE_FAKE_PUBLISHERS --value true"
assert_contains "${RESULT}" "本脚本刻意**不自动执行**任何恢复动作"
assert_contains "${RESULT}" "回退用的 .env 备份：远端 ~/sw-env-backups/env-"
assert_not_contains "${RESULT}" "✓ 生产 .env 已变更、已生效"

# --write-only + false 会留下"上了膛没击发"的状态，必须拒绝。
run_case "--write-only is refused for the dangerous direction" \
  --key SW_USE_FAKE_PUBLISHERS --value false --write-only
assert_rejected_before_remote
assert_contains "${RESULT}" "--write-only 不能与 --key SW_USE_FAKE_PUBLISHERS --value false 一起用"
assert_contains "${RESULT}" "上了膛没击发"

# ================================================== SW_USE_FAKE_PUBLISHERS=true
# 安全方向：不触发 R1 从严裁定，也不许打那条"真发布开启"的警告。
ENV_CONTENT="${ENV_DEFAULT//SW_USE_FAKE_PUBLISHERS=true/SW_USE_FAKE_PUBLISHERS=false}"
run_case "flipping back to true needs no strict verdict" --key SW_USE_FAKE_PUBLISHERS --value true
assert_status 0
assert_contains "${ENV_AFTER}" "SW_USE_FAKE_PUBLISHERS=true"
assert_not_contains "${ENV_AFTER}" "SW_USE_FAKE_PUBLISHERS=false"
assert_not_contains "${RESULT}" "真发布开启"
assert_contains "${RESULT}" "R1 闸门  模拟发布器=true：本次重启后什么都不会真发"
assert_contains "${RESULT}" "✓ core 已重启、探针恢复 200，R1 闸门已记录（模拟发布器=true，未探测通道）"
assert_contains "${RESULT}" "✓ 生产 .env 已变更、已生效"
# 未探测就不许说"已核验"。
assert_not_contains "${RESULT}" "R1 确认闸门通道已核验"
assert_no_tmp_residue

# 真发布开着时**回退**这条路，即使确认通道是死的也必须能走通——否则出事时人被锁死在
# 危险状态里。这一格由 restart.sh 的闸门语义保证（fake=true 直接放行），这里钉死它。
run_case "rolling back to true works even with a dead confirm channel" \
  TEST_TELEGRAM_JSON="${TG_NOT_POLLING}" --key SW_USE_FAKE_PUBLISHERS --value true
assert_status 0
assert_contains "${ENV_AFTER}" "SW_USE_FAKE_PUBLISHERS=true"
assert_contains "${RESULT}" "✓ 生产 .env 已变更、已生效"
ENV_CONTENT="${ENV_DEFAULT}"

# 值未变化：跳过备份与写入，但**仍然重建**（契约是"让它生效"，不是"编辑一个文件"）。
run_case "a no-op value skips backup and write but still converges" \
  --key SW_USE_FAKE_PUBLISHERS --value true
assert_status 0
assert_contains "${RESULT}" "SW_USE_FAKE_PUBLISHERS 的值与本次要写入的值相同，跳过备份与写入"
# 没有备份就一个字都不许说"备份了"：收尾话术不能在没发生的事情上打勾。
assert_not_contains "${RESULT}" ".env 备份  "
case_name="a no-op value skips backup and write but still converges"
[[ ! -d "${BACKUP_DIR}" ]] || fail_assertion "值未变化时不该生成备份目录"
assert_log_count "container-recreated" 1
assert_contains "${RESULT}" "✓ 生产 .env 已变更、已生效"

# ============================================== .env 结构：替换 / 新增 / 末尾换行
run_case "an absent key is appended rather than replaced" SW_OPS_UI_TOKEN="${UI_TOKEN}" \
  --key SW_UI_TOKEN --from-credentials
assert_status 0
assert_contains "${RESULT}" ".env 写入  SW_UI_TOKEN 已新增（原子：临时文件 + mv）"
assert_contains "${ENV_AFTER}" "SW_USE_FAKE_PUBLISHERS=true
SW_UI_TOKEN=${UI_TOKEN}"
# 只多了一行。
case_name="an absent key is appended rather than replaced"
before_lines="$(printf '%s' "${ENV_CONTENT}" | grep -c "" || true)"
after_lines="$(printf '%s\n' "${ENV_AFTER}" | grep -c "" || true)"
[[ "${after_lines}" -eq $((before_lines + 1)) ]] \
  || fail_assertion "追加应当只多一行：${before_lines} -> ${after_lines}"

# 【这条用例本轮从 SW_UI_TOKEN 换到 SW_TELEGRAM_SIGNING_SECRET，理由要写下来】
# 它考察的是 .env 的**结构**（就地替换、绝不重复），与是哪个键无关。而 SW_UI_TOKEN 那条路
# 现在必然先撞上签名密钥闸门：本机 token 与容器里那个不一致 → 探针 401 → fail-closed。
# 那正是本轮**刻意**的行为，单独有一组用例钉它（见下方「签名密钥轮换闸门」一节）；
# 让这条结构用例去顺带扛那件事，只会让它在闸门改动时红在无关的地方。
CRED_MISSING=0
CRED_CONTENT="sw_telegram_signing_secret: ${UI_TOKEN}
"
ENV_CONTENT="${ENV_DEFAULT}SW_TELEGRAM_SIGNING_SECRET=TESTSECRET_old-value
"
run_case "an existing key is replaced in place, never duplicated" \
  --key SW_TELEGRAM_SIGNING_SECRET --from-credentials
assert_status 0
assert_contains "${RESULT}" "已就地替换"
assert_contains "${ENV_AFTER}" "SW_TELEGRAM_SIGNING_SECRET=${UI_TOKEN}"
assert_not_contains "${ENV_AFTER}" "TESTSECRET_old-value"
assert_value_absent_from_argv "${UI_TOKEN}"
assert_not_contains "${RESULT}" "${UI_TOKEN}"
case_name="an existing key is replaced in place, never duplicated"
key_lines="$(grep -c '^SW_TELEGRAM_SIGNING_SECRET=' <<<"${ENV_AFTER}" || true)"
[[ "${key_lines}" -eq 1 ]] || fail_assertion "SW_TELEGRAM_SIGNING_SECRET 应当恰好一行，实际 ${key_lines} 行"
CRED_MISSING=1
CRED_CONTENT=""

# 末尾没有换行的 .env：新键绝不能被粘到上一行尾巴后面，最后一行也绝不能被吞掉。
ENV_CONTENT='SW_ENV=prod
SW_USE_FAKE_PUBLISHERS=true'
run_case "a .env without a trailing newline does not glue the new key onto the last line" \
  SW_OPS_UI_TOKEN="${UI_TOKEN}" --key SW_UI_TOKEN --from-credentials
assert_status 0
assert_contains "${ENV_AFTER}" "SW_USE_FAKE_PUBLISHERS=true
SW_UI_TOKEN=${UI_TOKEN}"
assert_not_contains "${ENV_AFTER}" "SW_USE_FAKE_PUBLISHERS=trueSW_UI_TOKEN"
case_name="a .env without a trailing newline does not glue the new key onto the last line"
[[ "$(grep -c '^SW_USE_FAKE_PUBLISHERS=true$' <<<"${ENV_AFTER}" || true)" -eq 1 ]] \
  || fail_assertion "末尾无换行的最后一行被吞掉或改写了：${ENV_AFTER}"

# 同一份夹具，改的正好是**最后那一行**：它必须被替换，而不是被丢掉或复制一份。
run_case "the last line of a newline-less .env is replaced, not dropped" \
  --key SW_USE_FAKE_PUBLISHERS --value false
assert_status 0
assert_contains "${ENV_AFTER}" "SW_ENV=prod
SW_USE_FAKE_PUBLISHERS=false"
case_name="the last line of a newline-less .env is replaced, not dropped"
[[ "$(printf '%s\n' "${ENV_AFTER}" | grep -c "" || true)" -eq 2 ]] \
  || fail_assertion "行数应当仍是 2：${ENV_AFTER}"
ENV_CONTENT="${ENV_DEFAULT}"

# 重复键：拒绝执行，什么都不动。
ENV_CONTENT="${ENV_DEFAULT}SW_USE_FAKE_PUBLISHERS=false
"
run_case "a duplicated key is refused instead of guessed" --key SW_USE_FAKE_PUBLISHERS --value false
assert_status 1
assert_contains "${RESULT}" "出现了 2 次"
assert_contains "${RESULT}" "本脚本拒绝猜哪一条生效"
assert_contains "${RESULT}" "dotenv 语义下后一条覆盖前一条"
assert_contains "${ENV_AFTER}" "SW_USE_FAKE_PUBLISHERS=true"
assert_log_count "container-recreated" 0
assert_not_contains "${LOG}" "restart-sh <"
ENV_CONTENT="${ENV_DEFAULT}"

ENV_MISSING=1
run_case "an absent remote .env is refused, never created" --key SW_USE_FAKE_PUBLISHERS --value true
assert_status 1
assert_contains "${RESULT}" "不存在，什么都没做"
assert_contains "${RESULT}" "不凭空造一个"
assert_log_count "container-recreated" 0
ENV_MISSING=0

# ===================================================== 备份先于写入 / 原子写入
# 备份拿不到就绝不写：把备份目录做成一个**文件**，mkdir -p 必然失败。
prep_backup_obstacle() { rm -rf "${BACKUP_DIR}"; : >"${BACKUP_DIR}"; }
RUN_CASE_HOOK=prep_backup_obstacle
run_case "backup failure keeps .env untouched" --key SW_USE_FAKE_PUBLISHERS --value false
RUN_CASE_HOOK=""
assert_status 1
assert_contains "${RESULT}" "备份生产 .env 失败，**没有动 .env**"
assert_contains "${RESULT}" "拿不到备份就绝不写"
assert_contains "${ENV_AFTER}" "SW_USE_FAKE_PUBLISHERS=true"
assert_not_contains "${ENV_AFTER}" "SW_USE_FAKE_PUBLISHERS=false"
assert_log_count "container-recreated" 0
assert_no_tmp_residue
rm -f "${BACKUP_DIR}"

# 原子写入的行为证据：让 mv 失败，.env 必须仍是**旧的完整内容**——就地编辑做不到这一点。
run_case "a failing mv leaves .env as the old complete file（原子写入的行为证据）" \
  TEST_MV_FAIL_SUBSTR="social_workflow/.env" --key SW_USE_FAKE_PUBLISHERS --value false
assert_status 1
assert_contains "${LOG}" "mv-forced-failure <"
assert_contains "${RESULT}" "写入生产 .env 失败"
assert_contains "${RESULT}" "要么还是旧的完整内容、要么已是新的完整内容，不会是半截"
assert_contains "${ENV_AFTER}" "SW_USE_FAKE_PUBLISHERS=true"
assert_contains "${ENV_AFTER}" "DEEPSEEK_API_KEY=sk-TESTFAKE-not-a-real-key"
case_name="a failing mv leaves .env as the old complete file（原子写入的行为证据）"
[[ "${ENV_AFTER}" == "${ENV_CONTENT%$'\n'}" ]] || fail_assertion ".env 不是逐字未改：${ENV_AFTER}"
# 备份已经生成（备份先于写入），且写入失败后不留临时文件残渣。
assert_contains "${RESULT}" ".env 备份"
assert_no_tmp_residue
assert_log_count "container-recreated" 0

# 备份文件本身必须是 0600、目录 0700。
run_case "the .env backup is 0600 in a 0700 directory" --key SW_USE_FAKE_PUBLISHERS --value false
assert_status 0
case_name="the .env backup is 0600 in a 0700 directory"
backup_path="$(find "${BACKUP_DIR}" -maxdepth 1 -type f -name 'env-*' | head -1)"
[[ -n "${backup_path}" ]] || fail_assertion "没有生成 .env 备份"
if [[ -n "${backup_path}" ]]; then
  assert_mode "${backup_path}" 600
  assert_mode "${BACKUP_DIR}" 700
  # 备份是**改动前**的那一份。
  assert_contains "$(<"${backup_path}")" "SW_USE_FAKE_PUBLISHERS=true"
fi
# 备份**不能**落在 git 工作树里：那会让 verify.sh 判"工作树不干净"、让 update.sh 拒绝部署。
case_name="the backup never lands inside the git worktree"
stray="$(find "${ENV_DIR}" -maxdepth 1 -name '.env*' ! -name '.env' 2>/dev/null || true)"
[[ -z "${stray}" ]] || fail_assertion "备份或临时文件落进了 ~/social_workflow：${stray}"

# ================================================ 容器重建是承重的（不是 restart）
# `docker compose restart` 不重建容器，也就读不到新的 .env。这条真实语义写在假 docker 里，
# 这里正面钉住它的后果：让 .env 生效的是 up -d --force-recreate。
run_case "the recreate is what applies .env（restart alone would not）" \
  --key SW_USE_FAKE_PUBLISHERS --value false
assert_status 0
assert_order "${RESULT}" "容器已按新 .env 重建" "core 已重启"
assert_log_count "docker <compose> <up> <-d> <--force-recreate> <--no-build> <core>" 1
assert_log_count "docker <compose> <restart> <core>" 1
# 重建之后 restart.sh 的闸门才看得到 false。
assert_contains "${RESULT}" "真发布已开启（模拟发布器=false）"

run_case "a failed recreate reports the half-applied state honestly" \
  TEST_RECREATE_STATUS=1 --key SW_USE_FAKE_PUBLISHERS --value false
assert_status 1
assert_contains "${RESULT}" "但容器重建失败——变更**没有生效**"
assert_contains "${RESULT}" "这是一个半截状态"
assert_contains "${ENV_AFTER}" "SW_USE_FAKE_PUBLISHERS=false"
assert_not_contains "${LOG}" "restart-sh <"
assert_not_contains "${RESULT}" "✓ 生产 .env 已变更、已生效"

# --write-only：只改文件，不碰运行中的容器。
# 【本轮从 SW_UI_TOKEN 换成一个无闸门的键，这不是绕开失败，是行为真的变了】
# 两个凭据类键现在都有 signing_secret 闸门，而"任何会触发事前闸门的方向都禁用 --write-only"
# 是本文件既有的规矩，它们因此落进了禁令。下面紧跟着一条用例正面钉住那个**拒绝**。
run_case "--write-only edits .env and stops there" \
  --key DAILY_TOKEN_BUDGET --value 4242 --write-only
assert_status 0
assert_contains "${ENV_AFTER}" "DAILY_TOKEN_BUDGET=4242"
assert_log_count "container-recreated" 0
assert_not_contains "${LOG}" "restart-sh <"
assert_contains "${RESULT}" "docker compose restart **不会**让 .env 变更生效"

# 凭据类键 + --write-only = 拒绝。报错里那句"用什么命令"必须按取值方式写对：
# 凭据类键没有 --value，照抄 `--value ${VALUE}` 会打出一个空的、不存在的用法。
run_case "--write-only is refused for a credential key, and names the right invocation" \
  SW_OPS_UI_TOKEN="${UI_TOKEN}" --key SW_UI_TOKEN --from-credentials --write-only
assert_rejected_before_remote
assert_contains "${RESULT}" "--write-only 不能与 --key SW_UI_TOKEN --from-credentials 一起用"
assert_not_contains "${RESULT}" "--key SW_UI_TOKEN --value "
assert_contains "${RESULT}" "这一格没有事后闸门兜底"
assert_contains "${ENV_AFTER}" "SW_USE_FAKE_PUBLISHERS=true"
assert_not_contains "${ENV_AFTER}" "SW_UI_TOKEN="

# ================================================== SW_UI_TOKEN：取值与零暴露
run_case "--from-credentials pushes the local value without leaking it" \
  SW_OPS_UI_TOKEN="${UI_TOKEN}" --key SW_UI_TOKEN --from-credentials
assert_status 0
assert_contains "${RESULT}" "已加载本机 token（来源：环境变量 SW_OPS_UI_TOKEN）；值不打印、不进 argv"
assert_contains "${RESULT}" "目标值    <不回显：凭据，红线 R5>"
assert_contains "${ENV_AFTER}" "SW_UI_TOKEN=${UI_TOKEN}"
# ① 值不在任何一条 argv 里（ssh / docker / curl 全部记录在 TEST_LOG）。
assert_value_absent_from_argv "${UI_TOKEN}"
# ② 值不在任何一行输出里（stdout + stderr 都收进 RESULT）。
assert_not_contains "${RESULT}" "${UI_TOKEN}"
# ③ 但它**确实送到了**：容器重建后 core 要求这个 token，restart.sh 的探针带对了头才 200。
assert_contains "${AUTH_LOG}" "url <http://127.0.0.1:8000/api/v1/system/info> header <Authorization: Bearer ${UI_TOKEN}>"
assert_contains "${RESULT}" "✓ 生产 .env 已变更、已生效"
# ④ 换 token 会换掉 Telegram 的签名密钥——这条副作用必须事前说。
assert_contains "${RESULT}" "Telegram 确认卡的 HMAC 签名密钥"
assert_contains "${RESULT}" "**此前已推出去、还没人点的确认卡按下去会验签失败**"
assert_contains "${RESULT}" "core/telegram.py:151-154"

CRED_MISSING=0
CRED_CONTENT="sw_ui_token: ${UI_TOKEN}
"
run_case "--from-credentials also reads the credentials file" --key SW_UI_TOKEN --from-credentials
assert_status 0
assert_contains "${RESULT}" ".dsh-sw/.credentials.yaml 的 sw_ui_token 键"
assert_contains "${ENV_AFTER}" "SW_UI_TOKEN=${UI_TOKEN}"
assert_value_absent_from_argv "${UI_TOKEN}"

run_case "--generate refuses to overwrite an existing sw_ui_token" --key SW_UI_TOKEN --generate
assert_rejected_before_remote
assert_contains "${RESULT}" "已经有 sw_ui_token 键，--generate 拒绝覆盖它"
assert_contains "${RESULT}" "旧值被盖掉就再也拿不回来"
assert_contains "${RESULT}" "--from-credentials"
case_name="--generate refuses to overwrite an existing sw_ui_token"
[[ "$(<"${CRED_FILE}")" == "${CRED_CONTENT%$'\n'}" ]] || fail_assertion "凭据文件被改动了"

# 凭据文件存在、但没有 sw_ui_token 键，且**末尾没有换行**：只追加，不粘连，不动原有内容。
CRED_CONTENT='some_other_key: keep-me
another: also-keep'
run_case "--generate appends to an existing credentials file without gluing" --key SW_UI_TOKEN --generate
assert_status 0
case_name="--generate appends to an existing credentials file without gluing"
CRED_AFTER="$(<"${CRED_FILE}")"
assert_contains "${CRED_AFTER}" "some_other_key: keep-me"
assert_contains "${CRED_AFTER}" "another: also-keep"
assert_not_contains "${CRED_AFTER}" "also-keepsw_ui_token"
[[ "$(grep -c '^sw_ui_token: ' <<<"${CRED_AFTER}" || true)" -eq 1 ]] \
  || fail_assertion "凭据文件里应当恰好一行 sw_ui_token：${CRED_AFTER}"
GENERATED="$(sed -n 's/^sw_ui_token: //p' "${CRED_FILE}")"
[[ "${GENERATED}" =~ ^[0-9a-f]{64}$ ]] \
  || fail_assertion "生成的 token 不是 64 位十六进制（此处只校验形状，不打印值）"
# 生成的值同样一个字符都不许进 argv 或输出。
assert_value_absent_from_argv "${GENERATED}"
assert_not_contains "${RESULT}" "${GENERATED}"
assert_contains "${RESULT}" "已在本机生成新值并写入"
# 凭据文件的键名现在由 sw_env_policy 的第四格给，报错 / 提示里必须**点名**是哪一个键——
# 硬编码回 sw_ui_token 的话，第二个凭据类键会静默地去写别人那一行。
assert_contains "${RESULT}" "的 sw_ui_token 键（0600）"
assert_contains "${RESULT}" "人要用它的时候自己读那个文件"
assert_contains "${ENV_AFTER}" "SW_UI_TOKEN=${GENERATED}"
assert_mode "${CRED_FILE}" 600
assert_mode "${TMP}/home dir/.dsh-sw" 700
assert_no_tmp_residue

CRED_MISSING=1
CRED_CONTENT=""
run_case "--generate creates the credentials file when it is absent" --key SW_UI_TOKEN --generate
assert_status 0
case_name="--generate creates the credentials file when it is absent"
GENERATED="$(sed -n 's/^sw_ui_token: //p' "${CRED_FILE}")"
[[ "${GENERATED}" =~ ^[0-9a-f]{64}$ ]] || fail_assertion "生成的 token 形状不对"
assert_value_absent_from_argv "${GENERATED}"
assert_not_contains "${RESULT}" "${GENERATED}"
assert_mode "${CRED_FILE}" 600
assert_mode "${TMP}/home dir/.dsh-sw" 700
# 端到端：写进 .env → 重建容器 → core 要求这个 token → restart.sh 从凭据文件读到同一个值 → 200。
assert_contains "${AUTH_LOG}" "url <http://127.0.0.1:8000/api/v1/system/info> header <Authorization: Bearer ${GENERATED}>"
assert_contains "${RESULT}" "✓ 生产 .env 已变更、已生效"

# 熵源都不可用时**拒绝生成**，绝不退化到可预测的随机数；且此时不许碰生产、不许写凭据文件。
run_case "--generate refuses when no CSPRNG is available" TEST_NO_ENTROPY=1 --key SW_UI_TOKEN --generate
assert_rejected_before_remote
assert_contains "${RESULT}" "本机拿不到密码学安全的随机源，拒绝生成 SW_UI_TOKEN 的值"
assert_contains "${RESULT}" "绝不退化到 \$RANDOM"
case_name="--generate refuses when no CSPRNG is available"
[[ ! -f "${CRED_FILE}" ]] || fail_assertion "拒绝生成时不该写凭据文件"

# 熵源**成功但降级**（吐出一个短值）：只有生成端自检拦得住，必须同样拒绝。
run_case "--generate refuses a silently degraded entropy source" TEST_SHORT_ENTROPY=1 --key SW_UI_TOKEN --generate
assert_rejected_before_remote
assert_contains "${RESULT}" "本机拿不到密码学安全的随机源，拒绝生成 SW_UI_TOKEN 的值"
case_name="--generate refuses a silently degraded entropy source"
[[ ! -f "${CRED_FILE}" ]] || fail_assertion "熵源降级时不该写凭据文件"

# 本机 token 字符集非法：在碰生产之前就报错，且报错里绝不回显 token。
run_case "an illegal token is rejected before touching production" \
  SW_OPS_UI_TOKEN='TESTTOKEN_bad"quote' --key SW_UI_TOKEN --from-credentials
assert_rejected_before_remote
assert_contains "${RESULT}" "工作台 API token 含有不被允许的字符"
assert_not_contains "${RESULT}" 'TESTTOKEN_bad"quote'

run_case "--from-credentials with nothing configured explains where to put it" \
  SW_OPS_UI_TOKEN= --key SW_UI_TOKEN --from-credentials
assert_rejected_before_remote
assert_contains "${RESULT}" "本机没有可用的工作台 API token"
assert_contains "${RESULT}" "--generate"

# ------------------------------------------ bash -x 下值必须零出现（红线 R5）
# 现实场景：401 修不好 → `bash -x scripts/ops/env_set.sh … 2>&1 | tee /tmp/x.log` → 贴进工单。
# 假件只记录 argv，模拟不了 xtrace，所以这条缺口只能靠这个用例守。
XTRACE_TOKEN='TESTTOKEN_xtrace-envset-must-not-leak'
RUN_XTRACE=1
run_case "bash -x never prints the token" \
  SW_OPS_UI_TOKEN="${XTRACE_TOKEN}" --key SW_UI_TOKEN --from-credentials
assert_status 0
assert_value_absent_from_argv "${XTRACE_TOKEN}"
assert_not_contains "${RESULT}" "${XTRACE_TOKEN}"
assert_contains "${RESULT}" "+ sw_ops_load_ui_token"
assert_contains "${AUTH_LOG}" "header <Authorization: Bearer ${XTRACE_TOKEN}>"

run_case "bash -x never prints a generated token either" --key SW_UI_TOKEN --generate
assert_status 0
case_name="bash -x never prints a generated token either"
GENERATED="$(sed -n 's/^sw_ui_token: //p' "${CRED_FILE}")"
[[ "${GENERATED}" =~ ^[0-9a-f]{64}$ ]] || fail_assertion "生成的 token 形状不对"
assert_value_absent_from_argv "${GENERATED}"
assert_not_contains "${RESULT}" "${GENERATED}"
RUN_XTRACE=0

# ==================== 凭据文件三个函数的键名参数化（直接驱动 scripts/ops/ui_token.sh）
#
# 【为什么必须单独有这一节，先读这段】读 / 判存在 / 写 这三个函数刚从"键名 sw_ui_token 写死
# 在实现里"改成"键名走参数"。而 env_set.sh 只会拿 sw_ui_token 这**一个**键去调它们——于是
# 上面所有用例覆盖到的仍然只有旧键名：把三个实现原样退回硬编码 'sw_ui_token'，它们一条都
# 不会红。要让"参数化"本身成为被测过的代码，只能在这里直接 source ui_token.sh，用一个
# **不是** sw_ui_token 的键名把三条路各驱动一遍。
#
# 【为什么放这份文件里而不是新开一个 test_ui_token.sh】env_set.sh 是这三个函数**唯一**的
# 取用方（`grep -rn '<函数名>' scripts/`），凭据文件的行为一直由本文件负责钉；再开一份会
# 让同一块行为分散在两处。
#
# 这一节出现的所有"凭据"都是 TESTVALUE_ 打头的本地假值，只在 ${TMP} 里存活。
UT_LIB="${ROOT}/scripts/ops/ui_token.sh"
UT_DIR="${TMP}/ui-token-unit"
UT_HOME="${UT_DIR}/home"
mkdir -p "${UT_DIR}" "${UT_HOME}"
UT_CRED="${UT_DIR}/creds.yaml"
UT_KEY="ops_probe_key"
UT_FAKE_A="TESTVALUE_alpha-0123456789"
UT_FAKE_B="TESTVALUE_beta-9876543210"
UT_XTRACE=""
UT_OUT=""
UT_STATUS=0

ut_case() { case_name="$1"; cases=$((cases + 1)); }

# 在一个干净的子进程里 source ui_token.sh，再执行 stdin 上给的脚本片段。
# die() / note() 由这里提供——那是 source 本文件的前置契约（ui_token.sh 自己写着）。
# 片段用**引号 heredoc** 传，一个字都不在外层展开；要用的值全部经环境变量送进去，
# 于是片段里不需要任何转义，也不会出现"外层先展开了一次"这类看不出来的失配。
ut_drive() {
  {
    printf '%s\n' 'set -euo pipefail'
    # 下面两行往临时脚本里写的是**字面 shell 源码**，$1/$@ 要留到那个脚本自己跑起来
    # 的时候才展开——这里展开就等于把桩函数写死成空串。
    # shellcheck disable=SC2016
    printf '%s\n' 'die() { printf "DIE: %s\n" "$1" >&2; shift; for ut_l in "$@"; do printf "  %s\n" "${ut_l}" >&2; done; exit 3; }'
    # shellcheck disable=SC2016
    printf '%s\n' 'note() { printf "  %s\n" "$1"; }'
    printf 'source %q\n' "${UT_LIB}"
    cat
  } >"${UT_DIR}/case.sh"
  set +e
  UT_OUT="$(env -u SW_OPS_UI_TOKEN \
    HOME="${UT_HOME}" \
    UT_CRED="${UT_CRED}" UT_KEY="${UT_KEY}" \
    UT_FAKE_A="${UT_FAKE_A}" UT_FAKE_B="${UT_FAKE_B}" \
    /bin/bash ${UT_XTRACE:+-x} "${UT_DIR}/case.sh" 2>&1)"
  UT_STATUS=$?
  set -e
}
ut_assert_status() {
  [[ "${UT_STATUS}" -eq "$1" ]] \
    || fail_assertion "驱动退出码应为 $1，实际 ${UT_STATUS}；输出：${UT_OUT}"
}
ut_assert_contains() {
  [[ "${UT_OUT}" == *"$1"* ]] || fail_assertion "驱动输出里缺少 [$1]；输出：${UT_OUT}"
}
ut_assert_not_contains() {
  [[ "${UT_OUT}" != *"$1"* ]] || fail_assertion "驱动输出里不该出现 [$1]；输出：${UT_OUT}"
}

# ① 三个函数真的按传进来的键名工作，而不是永远盯着 sw_ui_token。
#    同一个文件里放两个键，交叉验一遍：拿 A 键读到 A 值、拿 B 键读到 B 值、
#    拿一个没写过的键判存在必须为假。任何一个函数退回硬编码，这条都会红。
ut_case "the credentials helpers key off the argument, not a hardcoded sw_ui_token"
rm -f "${UT_CRED}"
ut_drive <<'UTEOF'
SW_OPS_ENV_VALUE="${UT_FAKE_A}"
sw_ops_write_credentials_key "${UT_CRED}" "${UT_KEY}"
SW_OPS_ENV_VALUE="${UT_FAKE_B}"
sw_ops_write_credentials_key "${UT_CRED}" sw_ui_token
sw_ops_credentials_has_key "${UT_CRED}" "${UT_KEY}" && echo HAS_PROBE || echo NO_PROBE
sw_ops_credentials_has_key "${UT_CRED}" sw_ui_token && echo HAS_UI || echo NO_UI
sw_ops_credentials_has_key "${UT_CRED}" never_written_key && echo HAS_NEVER || echo NO_NEVER
printf 'READ_PROBE=[%s]\n' "$(sw_ops_read_credentials_key "${UT_CRED}" "${UT_KEY}")"
printf 'READ_UI=[%s]\n' "$(sw_ops_read_credentials_key "${UT_CRED}" sw_ui_token)"
UTEOF
ut_assert_status 0
ut_assert_contains "HAS_PROBE"
ut_assert_contains "HAS_UI"
ut_assert_contains "NO_NEVER"
ut_assert_contains "READ_PROBE=[${UT_FAKE_A}]"
ut_assert_contains "READ_UI=[${UT_FAKE_B}]"
# 写入的形状也要对：两行、各自顶格、键名就是传进去的那个。
UT_CRED_TEXT="$(<"${UT_CRED}")"
[[ "${UT_CRED_TEXT}" == "${UT_KEY}: ${UT_FAKE_A}"$'\n'"sw_ui_token: ${UT_FAKE_B}" ]] \
  || fail_assertion "凭据文件内容不对：[${UT_CRED_TEXT}]"

# ② 极窄解析的语义在任意键名下逐字不变——参数化不许顺手"改好"任何一条。
#    只认顶格；裸值 / 一对双引号 / 一对单引号；尾随空白与 CR 去掉；空值当没配；
#    `<key>_backup` 这类前缀相同的邻居不许误配；缩进（含嵌套）的同名键一律不认。
ut_case "the narrow parser keeps every rule for an arbitrary key"
{
  printf 'plain_key: bare-value\n'
  printf 'dq_key: "dq-value"\n'
  printf "sq_key: 'sq-value'\n"
  printf 'crlf_key: crlf-value \r\n'
  printf 'empty_key:\n'
  printf 'plain_key_backup: neighbour-value\n'
  printf 'nested:\n'
  printf '  only_indented_key: must-not-be-seen\n'
  printf '  plain_key: nested-must-not-win\n'
} >"${UT_CRED}"
ut_drive <<'UTEOF'
for ut_k in plain_key dq_key sq_key crlf_key empty_key plain_key_backup only_indented_key; do
  if ut_v="$(sw_ops_read_credentials_key "${UT_CRED}" "${ut_k}")"; then
    printf '%s=[%s]\n' "${ut_k}" "${ut_v}"
  else
    printf '%s=<none>\n' "${ut_k}"
  fi
done
UTEOF
ut_assert_status 0
ut_assert_contains "plain_key=[bare-value]"
ut_assert_contains "dq_key=[dq-value]"
ut_assert_contains "sq_key=[sq-value]"
ut_assert_contains "crlf_key=[crlf-value]"
ut_assert_contains "empty_key=<none>"
ut_assert_contains "plain_key_backup=[neighbour-value]"
ut_assert_contains "only_indented_key=<none>"
ut_assert_not_contains "nested-must-not-win"
ut_assert_not_contains "must-not-be-seen"

# ③ 键名里的正则元字符：必须 die，绝不许静默错配到别的键。
#    构造的正是那个静默错配：文件里只有 `sw-ui_token`，用 `sw.ui_token` 去读——不设防的
#    实现会把 `.` 当通配符、把邻居的值当成自己的读回来（而且一声不吭）。
ut_case "a key name with a regex metacharacter dies instead of silently matching a neighbour"
printf 'sw-ui_token: TESTVALUE_neighbour-must-not-be-read\n' >"${UT_CRED}"
ut_drive <<'UTEOF'
sw_ops_read_credentials_key "${UT_CRED}" 'sw.ui_token'
echo SURVIVED
UTEOF
ut_assert_status 3
ut_assert_contains "凭据文件键名不合法：sw.ui_token"
ut_assert_not_contains "TESTVALUE_neighbour-must-not-be-read"
ut_assert_not_contains "SURVIVED"

# ④ 三个函数**每一个**都自带守卫，不是只有读那一个。
#    判存在这条格外要钉：它内部那条调用带着 `2>/dev/null`，守卫一旦排在它后面，
#    键名违例就会变成一次**一个字都不打**的退出——比报错更难查。
for ut_fn in sw_ops_read_credentials_key sw_ops_credentials_has_key sw_ops_write_credentials_key; do
  ut_case "the key-name guard fires in ${ut_fn}"
  UT_BAD_FN="${ut_fn}" ut_drive <<'UTEOF'
SW_OPS_ENV_VALUE=TESTVALUE_should-never-be-written
"${UT_BAD_FN}" "${UT_CRED}" 'bad*key'
echo SURVIVED
UTEOF
  ut_assert_status 3
  ut_assert_contains "凭据文件键名不合法：bad*key"
  ut_assert_not_contains "SURVIVED"
done
# 文件一个字节都没被动过（写入那一支必须死在动手之前）。
UT_CRED_TEXT="$(<"${UT_CRED}")"
[[ "${UT_CRED_TEXT}" == "sw-ui_token: TESTVALUE_neighbour-must-not-be-read" ]] \
  || fail_assertion "非法键名不该动凭据文件：[${UT_CRED_TEXT}]"

# ⑤ 键名为空 / 没传：同样是 die，且报错要说得出是哪种。
ut_case "a missing key name is a die, not a silent no-op"
ut_drive <<'UTEOF'
sw_ops_credentials_has_key "${UT_CRED}"
echo SURVIVED
UTEOF
ut_assert_status 3
ut_assert_contains "凭据文件键名不合法：<空或未传>"
ut_assert_not_contains "SURVIVED"

# ⑥ 取用路径上的守卫必须在**命令替换之前**跑。
#    _sw_ops_load_ui_token_impl 是在 `if value="$( ... )"` 里调读函数的：子 shell 里的 die
#    只打死子 shell，而那个位置又把 set -e 关了。少了进 $( ) 之前那一道，一个非法键名会
#    退化成"这个键没配"，脚本带着"本机没有 token"若无其事地跑下去。
ut_case "the load path validates the key before the command substitution swallows the die"
mkdir -p "${UT_HOME}/.dsh-sw"
printf 'sw_ui_token: TESTVALUE_load-path-must-not-be-read\n' >"${UT_HOME}/.dsh-sw/.credentials.yaml"
chmod 600 "${UT_HOME}/.dsh-sw/.credentials.yaml"
ut_drive <<'UTEOF'
SW_OPS_CREDENTIALS_UI_TOKEN_KEY='sw.ui_token'
sw_ops_load_ui_token
printf 'SURVIVED source=[%s]\n' "${SW_OPS_UI_TOKEN_SOURCE}"
UTEOF
ut_assert_status 3
ut_assert_contains "凭据文件键名不合法：sw.ui_token"
ut_assert_not_contains "SURVIVED"
# 反面对照：键名合法时这条路照常走通，来源文案与改造前逐字一致。
ut_case "the load path still reads the credentials file when the key is legal"
ut_drive <<'UTEOF'
sw_ops_load_ui_token
printf 'SOURCE=[%s]\n' "${SW_OPS_UI_TOKEN_SOURCE}"
printf 'VALUE=[%s]\n' "${SW_OPS_UI_TOKEN_VALUE}"
UTEOF
ut_assert_status 0
ut_assert_contains "SOURCE=[${UT_HOME}/.dsh-sw/.credentials.yaml 的 sw_ui_token 键]"
ut_assert_contains "VALUE=[TESTVALUE_load-path-must-not-be-read]"
rm -rf "${UT_HOME}/.dsh-sw"

# ⑦ 改名之后的三个函数仍然各自被 sw_ops_xtrace_guard 包着（红线 R5）。
#    上面那两条 `bash -x never prints ...` 走的是 env_set.sh 的整条路，盖得到
#    load / generate / adopt / write；这里补上**读**与**判存在**——它们在 env_set.sh 的
#    正常路径上要么被 die 提前截断、要么藏在 load 里面，那两条用例够不着。
#    三条分开跑，各自钉的东西不一样，理由如实写在这里：
#      · 读：守卫一撤，`value="${BASH_REMATCH[1]}"` 会被原样打出来，"值零出现"就钉得死。
#      · 写：同理，撤掉守卫时 `printf '%s: %s\n' <key> <value>` 那一行会连值一起打出来。
#      · 判存在：它内部那条调用自带 `>/dev/null 2>&1`，守卫撤掉时值**恰好**仍然漏不出来
#        （追踪也一并进了 /dev/null）。所以这一格钉的是**机制本身**：函数体内一行都不许被
#        追踪到。今天它是纵深防御，但那条重定向哪天被人去掉，它就是唯一挡着的东西——
#        本仓在"目前恰好安全"上吃过太多次亏，不留这种缺口。
ut_case "bash -x traces nothing from inside sw_ops_credentials_has_key"
printf '%s: %s\n' "${UT_KEY}" "${UT_FAKE_A}" >"${UT_CRED}"
UT_XTRACE=1
ut_drive <<'UTEOF'
sw_ops_credentials_has_key "${UT_CRED}" "${UT_KEY}" && echo HAS_UNDER_X
UTEOF
ut_assert_status 0
ut_assert_contains "HAS_UNDER_X"
# 自检：追踪确实开着，否则下面那两条"没出现"是空转。
ut_assert_contains "+ sw_ops_credentials_has_key"
ut_assert_contains "+ sw_ops_xtrace_guard _sw_ops_credentials_has_key_impl"
# 函数体内一行都没被追踪到——守卫在位的直接证据。
ut_assert_not_contains "+ _sw_ops_credentials_key_guard"
ut_assert_not_contains "${UT_FAKE_A}"

ut_case "bash -x never prints the value read back by sw_ops_read_credentials_key"
ut_drive <<'UTEOF'
sw_ops_read_credentials_key "${UT_CRED}" "${UT_KEY}" >/dev/null && echo READ_UNDER_X
UTEOF
ut_assert_status 0
ut_assert_contains "READ_UNDER_X"
ut_assert_contains "+ sw_ops_read_credentials_key"
ut_assert_not_contains "${UT_FAKE_A}"

ut_case "bash -x never prints the value written by sw_ops_write_credentials_key"
ut_drive <<'UTEOF'
# 待写入的值只能在追踪关闭时落进全局变量：xtrace 守卫管不到调用点那一行，
# 这一条正是 ui_token.sh 里"值绝不进参数、只经全局变量流转"那段说明的直接体现。
set +x
SW_OPS_ENV_VALUE="${UT_FAKE_B}"
set -x
sw_ops_write_credentials_key "${UT_CRED}" written_under_xtrace
echo WROTE_UNDER_X
UTEOF
UT_XTRACE=""
ut_assert_status 0
ut_assert_contains "WROTE_UNDER_X"
ut_assert_contains "+ sw_ops_write_credentials_key"
ut_assert_not_contains "${UT_FAKE_A}"
ut_assert_not_contains "${UT_FAKE_B}"
# 写入确实发生了（否则"没打印"可以靠"什么都没做"骗过去）。
UT_CRED_TEXT="$(<"${UT_CRED}")"
[[ "${UT_CRED_TEXT}" == "${UT_KEY}: ${UT_FAKE_A}"$'\n'"written_under_xtrace: ${UT_FAKE_B}" ]] \
  || fail_assertion "xtrace 用例里的写入没生效：[${UT_CRED_TEXT}]"

# ⑧ 末尾没有换行的文件，追加任意键都不许粘行——这条在 sw_ui_token 上由上面
#    「--generate appends ...」钉着，这里换一个键名再钉一次，确保它钉的是机制不是那个键。
ut_case "appending an arbitrary key to a newline-less file does not glue lines"
printf 'first_key: keep-me\nlast_line_without_newline: also-keep' >"${UT_CRED}"
ut_drive <<'UTEOF'
SW_OPS_ENV_VALUE="${UT_FAKE_A}"
sw_ops_write_credentials_key "${UT_CRED}" appended_key
UTEOF
ut_assert_status 0
UT_CRED_TEXT="$(<"${UT_CRED}")"
[[ "${UT_CRED_TEXT}" == "first_key: keep-me"$'\n'"last_line_without_newline: also-keep"$'\n'"appended_key: ${UT_FAKE_A}" ]] \
  || fail_assertion "追加粘行了或改动了原有内容：[${UT_CRED_TEXT}]"


# ============================================== 传输失败与远端退出码：一律不重试
# 与 backup.sh / restart.sh 刻意不同：那两个重试的是只读或幂等动作，本脚本重跑一次
# 是"又一次生产写入 + 又一次容器重建"，断链发生在写入前还是写入后外层根本区分不了。
run_case "a transport failure is not retried" TEST_SSH_STATUSES=255 --key SW_USE_FAKE_PUBLISHERS --value false
assert_status 1
assert_log_count "ssh <" 1
assert_contains "${RESULT}" "SSH 连接或传输中断"
assert_contains "${RESULT}" "刻意不自动重试"
assert_not_contains "${RESULT}" "✓ 生产 .env 已变更"

run_case "remote 255 is normalized to 254" TEST_REMOTE_EXIT=255 --key SW_USE_FAKE_PUBLISHERS --value false
assert_status 1
assert_contains "${LOG}" "remote-forced-exit <255>"
assert_contains "${LOG}" "ssh-remote-status <254>"
assert_not_contains "${LOG}" "ssh-remote-status <255>"
assert_contains "${RESULT}" "已从 255 规范化成 254"
assert_log_count "ssh <" 1

# ############################################################################
# ##  白名单扩容（docs/RISKS.md 第 14 条「按键补工具面」）：七个新键
# ############################################################################
#
# 【这一节按两条线组织】
#   ① 每个新键至少一条"拒掉看起来对的坏值"——挑的都是 pydantic **真的认**、而我们主动
#      收紧掉的写法。所以这些用例证明的不是"写了不生效"，而是"这条工具面刻意比后端更严"。
#   ② 每道新闸门的正反两面：拦下来时生产**一个字节都没动**，放行时留下一行可核对的痕迹。

# ------------------------------------------------------- ① 拒掉看起来对的坏值
# SW_LLM_BACKEND：`deepseek` 是最像的那个坏值——它确实是个真名字，但它是
# configs/dsh/cordis.yml 里的**路由名**（SW_DSH_PROVIDER 的取值），不是后端名。
run_case "SW_LLM_BACKEND rejects a cordis route name" --key SW_LLM_BACKEND --value deepseek
assert_rejected_before_remote
assert_contains "${RESULT}" "SW_LLM_BACKEND 的值不合法（当前给的是：deepseek）"
assert_contains "${RESULT}" "anthropic 或 dsh —— 以 core/config.py:99 的 Literal 声明为准"

run_case "SW_LLM_BACKEND rejects a capitalised backend name" --key SW_LLM_BACKEND --value Anthropic
assert_rejected_before_remote
assert_contains "${RESULT}" "SW_LLM_BACKEND 的值不合法"

# SW_GENERATE_ENABLED：`0` 是 pydantic 认的 false，但在这张白名单上 `0` 同时是三个
# DAILY_*_BUDGET 的"全停"。同一个字符在相邻的键上意思不同，正是要挡掉的那类歧义。
run_case "SW_GENERATE_ENABLED rejects 0（布尔与预算共用一张表时 0 是歧义的）" \
  --key SW_GENERATE_ENABLED --value 0
assert_rejected_before_remote
assert_contains "${RESULT}" "SW_GENERATE_ENABLED 的值不合法（当前给的是：0）"
assert_contains "${RESULT}" "true 或 false —— 只认这两个**小写单词**"

# SW_TELEGRAM_ENABLED：`off` 也是 pydantic 认的 false。
run_case "SW_TELEGRAM_ENABLED rejects off" --key SW_TELEGRAM_ENABLED --value off
assert_rejected_before_remote
assert_contains "${RESULT}" "SW_TELEGRAM_ENABLED 的值不合法（当前给的是：off）"

# WECHAT_AUTO_PUBLISH：`True` 是 Python 写法，pydantic 认。
run_case "WECHAT_AUTO_PUBLISH rejects True" --key WECHAT_AUTO_PUBLISH --value True
assert_rejected_before_remote
assert_contains "${RESULT}" "WECHAT_AUTO_PUBLISH 的值不合法（当前给的是：True）"

# DAILY_TOKEN_BUDGET：`-1` 是这一组里**最危险**的坏值——pydantic 认（得到 -1），而
# core/budget.py:118-122 的 max(limit - used, 0) 让负上限与 0 一样是"全停"。
# 有人写 -1 想表达"不限"时会得到"全停"，这是反向故障，必须挡在门口。
run_case "DAILY_TOKEN_BUDGET rejects -1（写它的人想说不限，实际是全停）" \
  --key DAILY_TOKEN_BUDGET --value -1
assert_rejected_before_remote
assert_contains "${RESULT}" "DAILY_TOKEN_BUDGET 的值不合法（当前给的是：-1）"
assert_contains "${RESULT}" "非负十进制整数：不许前导零、下划线、正负号，最多 10 位"

run_case "DAILY_RENDER_SECONDS_BUDGET rejects 1_000_000" \
  --key DAILY_RENDER_SECONDS_BUDGET --value 1_000_000
assert_rejected_before_remote
assert_contains "${RESULT}" "DAILY_RENDER_SECONDS_BUDGET 的值不合法（当前给的是：1_000_000）"

run_case "DAILY_IMAGE_BUDGET rejects a zero-padded number" --key DAILY_IMAGE_BUDGET --value 007
assert_rejected_before_remote
assert_contains "${RESULT}" "DAILY_IMAGE_BUDGET 的值不合法（当前给的是：007）"

# ------------------------------------- ② 无闸门的键：写得进去，警告要说到点子上
run_case "a budget change writes without any gate and warns that 0 means a full stop" \
  --key DAILY_TOKEN_BUDGET --value 0
assert_status 0
assert_contains "${ENV_AFTER}" "DAILY_TOKEN_BUDGET=0"
assert_contains "${RESULT}" "**0 不是「不限」，0 是「当天全停」**"
assert_contains "${RESULT}" "本仓**没有**「不限」这个语义，任何哨兵值都没有"
# 改预算不该顺手去探 Telegram：闸门是**按键**的，不是无差别的。
assert_not_contains "${RESULT}" "事前闸门"
# 也不该为了改预算去取本机 token（那条路径只有 real_publish 才走）。
assert_not_contains "${RESULT}" "已加载本机 token"
assert_contains "${LOG}" "container-recreated"

run_case "DAILY_IMAGE_BUDGET warns about its alias" --key DAILY_IMAGE_BUDGET --value 40
assert_status 0
assert_contains "${RESULT}" "等价别名 SW_DAILY_IMAGE_BUDGET"
assert_contains "${RESULT}" "两个都在 .env 里时**主名赢**"

run_case "SW_GENERATE_ENABLED=false says it stops generation, not publishing" \
  --key SW_GENERATE_ENABLED --value false
assert_status 0
assert_contains "${ENV_AFTER}" "SW_GENERATE_ENABLED=false"
assert_contains "${RESULT}" "只停出稿、不停发布"
assert_contains "${RESULT}" "已经生成好、已排期、已确认的内容**照样会到点发出去**"
assert_not_contains "${RESULT}" "事前闸门"

# 闸门是按键按方向的，**没有闸门的方向必须照旧允许 --write-only**——把禁用无差别推广开
# 就是另一种添乱。这条负例专门钉住"没过度推广"。
run_case "--write-only stays allowed for a gate-free key" \
  --key DAILY_IMAGE_BUDGET --value 12 --write-only
assert_status 0
assert_contains "${ENV_AFTER}" "DAILY_IMAGE_BUDGET=12"
assert_contains "${RESULT}" ".env 已变更（--write-only：未重建容器，变更尚未生效）"
assert_log_count "container-recreated" 0

# ------------------------------------------- ③ 闸门：SW_LLM_BACKEND 的目标后端凭据
# ENV_DEFAULT 里有 DEEPSEEK_API_KEY，**没有** ANTHROPIC_API_KEY——也就是"dsh 挂了想回退
# 到 anthropic，但那边的 key 压根没配"这个真实场景。
run_case "switching to anthropic without its key is refused before anything is written" \
  --key SW_LLM_BACKEND --value anthropic
assert_status 42
assert_contains "${RESULT}" "目标后端 anthropic 要的凭据 ANTHROPIC_API_KEY 在生产 .env 里**没有这一行**"
assert_contains "${RESULT}" "事前闸门拒绝了这次后端切换"
assert_contains "${RESULT}" "懒加载——缺 key 时 core 照常起来，直到第一次真出稿才抛 LLMUnavailable"
# 这缺口是如实登记着的，不是假装不存在。
assert_contains "${RESULT}" "凭据类键刻意仍不在白名单上"
# 拦下来时生产一个字节都没动。
assert_contains "${ENV_AFTER}" "SW_LLM_BACKEND=dsh"
assert_not_contains "${ENV_AFTER}" "SW_LLM_BACKEND=anthropic"
assert_log_count "container-recreated" 0
assert_not_contains "${LOG}" "restart-sh <"
[[ ! -d "${BACKUP_DIR}" ]] || fail_assertion "闸门拦下时不该建备份目录"

ENV_CONTENT="${ENV_DEFAULT}ANTHROPIC_API_KEY=
"
run_case "an empty credential counts as missing, not as present" \
  --key SW_LLM_BACKEND --value anthropic
assert_status 42
assert_contains "${RESULT}" "在生产 .env 里**是空值**"
assert_contains "${ENV_AFTER}" "SW_LLM_BACKEND=dsh"

ENV_CONTENT="${ENV_DEFAULT}ANTHROPIC_API_KEY=sk-ant-TESTFAKE
"
run_case "switching to anthropic goes through once its key is there" \
  --key SW_LLM_BACKEND --value anthropic
assert_status 0
assert_contains "${ENV_AFTER}" "SW_LLM_BACKEND=anthropic"
assert_contains "${RESULT}" "目标后端 anthropic 的凭据 ANTHROPIC_API_KEY 在 .env 里存在且非空，允许切换"
# 放行那行话不许说过头：它只证明了那一行有值。
assert_contains "${RESULT}" "额度够不够、dsh runtime 装没装，这道闸门都答不了"
assert_contains "${LOG}" "container-recreated"
# 凭据值一个字符都不许进 argv，**也不许进脚本输出**——闸门读了 ANTHROPIC_API_KEY 的值
# （为了判"是不是空的"），文案里出现的必须只有变量名。
assert_value_absent_from_argv "sk-ant-TESTFAKE"
assert_not_contains "${RESULT}" "sk-ant-TESTFAKE"

# 反方向也查：切回 dsh 要 DEEPSEEK_API_KEY（SW_DSH_PROVIDER 缺省 = deepseek-official）。
ENV_CONTENT="${ENV_DEFAULT//SW_LLM_BACKEND=dsh/SW_LLM_BACKEND=anthropic}"
run_case "switching back to dsh uses the default provider's credential" \
  --key SW_LLM_BACKEND --value dsh
assert_status 0
assert_contains "${ENV_AFTER}" "SW_LLM_BACKEND=dsh"
assert_contains "${RESULT}" "目标后端 dsh 的凭据 DEEPSEEK_API_KEY 在 .env 里存在且非空，允许切换"
assert_value_absent_from_argv "sk-TESTFAKE-not-a-real-key"
assert_not_contains "${RESULT}" "sk-TESTFAKE-not-a-real-key"

ENV_CONTENT="${ENV_DEFAULT//SW_LLM_BACKEND=dsh/SW_LLM_BACKEND=anthropic}SW_DSH_PROVIDER=gateway
"
run_case "the dsh credential follows SW_DSH_PROVIDER, not a hardcoded guess" \
  --key SW_LLM_BACKEND --value dsh
assert_status 42
assert_contains "${RESULT}" "目标后端 dsh 要的凭据 SW_DSH_GATEWAY_API_KEY 在生产 .env 里**没有这一行**"
assert_contains "${ENV_AFTER}" "SW_LLM_BACKEND=anthropic"

ENV_CONTENT="${ENV_DEFAULT//SW_LLM_BACKEND=dsh/SW_LLM_BACKEND=anthropic}SW_DSH_PROVIDER=nosuchroute
"
run_case "an unregistered dsh route is fail-closed, not guessed" --key SW_LLM_BACKEND --value dsh
assert_status 42
assert_contains "${RESULT}" "不是 configs/dsh/cordis.yml 注册过的路由名"
assert_contains "${RESULT}" "gateway（SW_DSH_GATEWAY_API_KEY）"
assert_contains "${ENV_AFTER}" "SW_LLM_BACKEND=anthropic"
ENV_CONTENT="${ENV_DEFAULT}"

run_case "--write-only is refused for a backend switch too" \
  --key SW_LLM_BACKEND --value dsh --write-only
assert_rejected_before_remote
assert_contains "${RESULT}" "--write-only 不能与 --key SW_LLM_BACKEND --value dsh 一起用"
assert_contains "${RESULT}" "校验目标后端凭据的闸门跑在写入之前"

# ----------------------------------- ④ 闸门：SW_TELEGRAM_ENABLED 的确认卡载体
ENV_CONTENT="${ENV_DEFAULT//SW_USE_FAKE_PUBLISHERS=true/SW_USE_FAKE_PUBLISHERS=false}"
run_case "tearing down the confirm carrier while real publishing is on is refused" \
  --key SW_TELEGRAM_ENABLED --value false
assert_status 39
assert_contains "${RESULT}" "真发布正开着（.env 里 SW_USE_FAKE_PUBLISHERS=false），拒绝关掉确认卡的推送载体"
# R1 因果必须写准：不是"会越权发出去"。
assert_contains "${RESULT}" "关掉它**不会**让内容越权发出去"
assert_contains "${RESULT}" "没人点就跳过不发（记 skipped_unconfirmed）"
assert_contains "${RESULT}" "TTL 在一次都没推成功过时从 scheduled_at 起算"
assert_contains "${RESULT}" "工作台「确认发布」不受 Telegram 影响，走同一个后端函数"
# 出路必须给全，而且中间那一步让生产更安全。
assert_contains "${RESULT}" "--key SW_USE_FAKE_PUBLISHERS --value true"
assert_contains "${RESULT}" "--key SW_TELEGRAM_ENABLED --value false"
# 拦下来时生产一个字节都没动。
assert_not_contains "${ENV_AFTER}" "SW_TELEGRAM_ENABLED"
assert_log_count "container-recreated" 0
assert_not_contains "${LOG}" "restart-sh <"
[[ ! -d "${BACKUP_DIR}" ]] || fail_assertion "闸门拦下时不该建备份目录"

# 同一个键的**反方向**（把载体装回去）永远不受闸门约束——哪怕真发布正开着。
run_case "putting the carrier back is never gated" --key SW_TELEGRAM_ENABLED --value true
assert_status 0
assert_contains "${ENV_AFTER}" "SW_TELEGRAM_ENABLED=true"
assert_not_contains "${RESULT}" "事前闸门"
ENV_CONTENT="${ENV_DEFAULT}"

run_case "tearing down the carrier is allowed when nothing publishes for real" \
  --key SW_TELEGRAM_ENABLED --value false
assert_status 0
assert_contains "${ENV_AFTER}" "SW_TELEGRAM_ENABLED=false"
assert_contains "${RESULT}" "SW_USE_FAKE_PUBLISHERS=true（真发布没开着），允许拆掉 Telegram 载体"
assert_contains "${RESULT}" "工作台「确认发布」是唯一的确认入口"
assert_contains "${LOG}" "container-recreated"

# 读不出真发布状态时 fail-closed，而且与"读出来是 false"分成两个退出码。
ENV_CONTENT='SW_LLM_BACKEND=dsh
DEEPSEEK_API_KEY=sk-TESTFAKE-not-a-real-key
SW_ENV=prod
'
run_case "a missing SW_USE_FAKE_PUBLISHERS line is could-not-tell, not a verdict" \
  --key SW_TELEGRAM_ENABLED --value false
assert_status 40
assert_contains "${RESULT}" "事前闸门**读不出**真发布状态，按 fail-closed 拒绝写入"
assert_contains "${RESULT}" "根本没有 SW_USE_FAKE_PUBLISHERS 这一行"
# 「读不出」与「读出来是 false」必须是两个码：处置动作不同。
assert_contains "${RESULT}" "注意这与「真发布正开着」是两件事"
assert_contains "${RESULT}" "但那是**部署里那份代码**的属性，从这台机器上看不见"
assert_not_contains "${ENV_AFTER}" "SW_TELEGRAM_ENABLED"

ENV_CONTENT="${ENV_DEFAULT//SW_USE_FAKE_PUBLISHERS=true/SW_USE_FAKE_PUBLISHERS=1}"
run_case "a non-canonical boolean is could-not-tell too（1 在 pydantic 眼里是真，我们不赌）" \
  --key SW_TELEGRAM_ENABLED --value false
assert_status 40
assert_contains "${RESULT}" "既不是 true 也不是 false"
assert_not_contains "${ENV_AFTER}" "SW_TELEGRAM_ENABLED"
ENV_CONTENT="${ENV_DEFAULT}"

run_case "--write-only is refused when the carrier gate is in play" \
  --key SW_TELEGRAM_ENABLED --value false --write-only
assert_rejected_before_remote
assert_contains "${RESULT}" "它会拆掉确认卡的推送载体，而拦住这件事的闸门跑在写入之前"

# --------------------------------------- ⑤ 闸门：WECHAT_AUTO_PUBLISH 的认证状态
run_case "opening wechat auto-publish without WECHAT_CERTIFIED is refused" \
  --key WECHAT_AUTO_PUBLISH --value true
assert_status 43
assert_contains "${RESULT}" "没有 WECHAT_CERTIFIED 这一行"
assert_contains "${RESULT}" "拦它不是因为危险，是因为没用"
assert_contains "${RESULT}" "**不会生效的空操作**"
assert_contains "${RESULT}" "scripts/preflight.py:122-133 对这一组合的门禁裁定同样是 FAIL"
assert_not_contains "${ENV_AFTER}" "WECHAT_AUTO_PUBLISH"
assert_log_count "container-recreated" 0

ENV_CONTENT="${ENV_DEFAULT}WECHAT_CERTIFIED=false
"
run_case "an explicit WECHAT_CERTIFIED=false is refused just the same" \
  --key WECHAT_AUTO_PUBLISH --value true
assert_status 43
assert_contains "${RESULT}" "WECHAT_CERTIFIED=false，不是 true"
assert_not_contains "${ENV_AFTER}" "WECHAT_AUTO_PUBLISH"

ENV_CONTENT="${ENV_DEFAULT}WECHAT_CERTIFIED=true
"
run_case "a certified account may open the platform switch" --key WECHAT_AUTO_PUBLISH --value true
assert_status 0
assert_contains "${ENV_AFTER}" "WECHAT_AUTO_PUBLISH=true"
assert_contains "${RESULT}" "WECHAT_CERTIFIED=true，允许打开平台级自动发布"
assert_contains "${RESULT}" "每一条内容仍要审核 UI 写入 confirm_publish 才会真 freepublish"
ENV_CONTENT="${ENV_DEFAULT}"

# 关回草稿箱是安全方向，不设闸门——哪怕 WECHAT_CERTIFIED 压根不在 .env 里。
run_case "closing wechat auto-publish is never gated" --key WECHAT_AUTO_PUBLISH --value false
assert_status 0
assert_contains "${ENV_AFTER}" "WECHAT_AUTO_PUBLISH=false"
assert_not_contains "${RESULT}" "事前闸门"

# ------------------------------ ⑥ WECHAT_CERTIFIED：记的是外部事实，所以**从不拦**
#
# 【这一格的设计要点，测试要能证明的就是这三条】
#   ① 它与 WECHAT_AUTO_PUBLISH 的闸门**必须不对称**：两边都拦对方就是死锁，这一对永远上
#      不去。下面第一条用例直接把那个死锁的反面钉住——认证状态记成 true 不需要开关先为真。
#   ② 不拦，但要把**当场后果**说清：WECHAT_AUTO_PUBLISH 已经是 true 时，这一个写入就让
#      平台级自动发布成立。
#   ③ 虽然不拦，它仍然禁 --write-only——正因为 ② 那种情形是最典型的"上了膛没击发"。

# 拒掉看起来对的坏值：这个键读起来就是个是非题，`yes` 是最自然的错答，而 pydantic 认它。
run_case "WECHAT_CERTIFIED rejects yes（是非题最自然的错答，而 pydantic 认它）" \
  --key WECHAT_CERTIFIED --value yes
assert_rejected_before_remote
assert_contains "${RESULT}" "WECHAT_CERTIFIED 的值不合法（当前给的是：yes）"
assert_contains "${RESULT}" "true 或 false —— 只认这两个**小写单词**"

# ① 开关还关着时照样能如实记录事实——这正是不对称的意义所在。
run_case "recording the certification is never blocked by the server switch（否则这一对会死锁）" \
  --key WECHAT_CERTIFIED --value true
assert_status 0
assert_contains "${ENV_AFTER}" "WECHAT_CERTIFIED=true"
assert_contains "${RESULT}" "WECHAT_AUTO_PUBLISH 仍是 false（或没有这一行，出厂默认 false）"
assert_contains "${RESULT}" "本次变更**不会**让平台级自动发布生效"
# 不拦这件事必须是**明说**的，不是默默放过去。
assert_contains "${RESULT}" "本闸门**不拦**这次写入：WECHAT_CERTIFIED 记的是微信那边的事实，本工具面核实不了"
assert_contains "${RESULT}" "freepublish 那一步报 errcode=48001"
assert_contains "${LOG}" "container-recreated"
# 它绝不去打外网：核实认证状态要连 api.weixin.qq.com，本闸门刻意不做。
assert_not_contains "${LOG}" "weixin"

# ② 开关已经开着时，这一个写入就让平台级自动发布成立——必须当面说。
ENV_CONTENT="${ENV_DEFAULT}WECHAT_AUTO_PUBLISH=true
"
run_case "it says out loud when this one write makes the platform pair live" \
  --key WECHAT_CERTIFIED --value true
assert_status 0
assert_contains "${ENV_AFTER}" "WECHAT_CERTIFIED=true"
assert_contains "${RESULT}" "WECHAT_AUTO_PUBLISH 已经是 true——**这一个写入就让平台级自动发布成立**"
assert_contains "${RESULT}" "此后只差每条内容的 confirm_publish"

# ③ 正因为 ② 那种情形，这个方向禁 --write-only。
run_case "--write-only is refused for the certification claim too" \
  --key WECHAT_CERTIFIED --value true --write-only
assert_rejected_before_remote
assert_contains "${RESULT}" "--write-only 不能与 --key WECHAT_CERTIFIED --value true 一起用"
assert_contains "${RESULT}" "这一个写入就让平台级自动发布成立——那正是最不该留成「上了膛没击发」的一格"
ENV_CONTENT="${ENV_DEFAULT}"

# 反方向（记回 false）是安全方向，不设闸门。
ENV_CONTENT="${ENV_DEFAULT}WECHAT_CERTIFIED=true
"
run_case "recording the certification as false is never gated" --key WECHAT_CERTIFIED --value false
assert_status 0
assert_contains "${ENV_AFTER}" "WECHAT_CERTIFIED=false"
assert_not_contains "${RESULT}" "事前闸门"
assert_contains "${RESULT}" "双确认闸门的第二道随之关上"
ENV_CONTENT="${ENV_DEFAULT}"

# 与上一道闸门配合：认证记成 true 之后，WECHAT_AUTO_PUBLISH=true 就过得去了。
# 这条把"两步走得通"这件事整体钉住——单看任一道闸门都证不了它。
ENV_CONTENT="${ENV_DEFAULT}WECHAT_CERTIFIED=true
"
run_case "the two-step order actually works end to end" --key WECHAT_AUTO_PUBLISH --value true
assert_status 0
assert_contains "${ENV_AFTER}" "WECHAT_AUTO_PUBLISH=true"
assert_contains "${RESULT}" "WECHAT_CERTIFIED=true，允许打开平台级自动发布"
ENV_CONTENT="${ENV_DEFAULT}"

# ============================== SW_TELEGRAM_SIGNING_SECRET：取值路径（凭据文件键名参数化）
#
# 【这一节钉的是"第二个凭据类键真的走自己那一行"】上一批把凭据文件的读 / 判存在 / 写按键名
# 参数化，但当时只有 sw_ui_token 一个真实调用方——把实现原样退回硬编码也不会红。现在有了
# 第二个键，参数化是不是真的接上了，可以从**行为**上看出来了：写错键名的后果是它去读 / 写
# 别人那一行，而"判存在"会对着别人点头，于是"只追加不覆盖"当场失效。
CRED_MISSING=1
CRED_CONTENT=""
ENV_CONTENT="${ENV_DEFAULT}"

run_case "SW_TELEGRAM_SIGNING_SECRET refuses --value（它同样是凭据，值不进 argv）" \
  --key SW_TELEGRAM_SIGNING_SECRET --value hunter2
assert_rejected_before_remote
assert_contains "${RESULT}" "SW_TELEGRAM_SIGNING_SECRET 不接受 --value"
assert_contains "${RESULT}" "/proc/*/cmdline 世界可读"

run_case "SW_TELEGRAM_SIGNING_SECRET needs a value source" --key SW_TELEGRAM_SIGNING_SECRET
assert_rejected_before_remote
assert_contains "${RESULT}" "需要 --generate 或 --from-credentials"

# --generate 写的是**自己那一行**，不是 sw_ui_token 那一行。夹具里先放一个 sw_ui_token：
# 键名若被硬编码回去，这一条会以"已经有这个键，拒绝覆盖"红掉——那正是我们要它红的方式。
CRED_MISSING=0
CRED_CONTENT="sw_ui_token: ${UI_TOKEN}
"
run_case "--generate writes the signing secret to its own credentials key" \
  --key SW_TELEGRAM_SIGNING_SECRET --generate
assert_status 0
case_name="--generate writes the signing secret to its own credentials key"
CRED_AFTER="$(<"${CRED_FILE}")"
assert_contains "${CRED_AFTER}" "sw_ui_token: ${UI_TOKEN}"
SIGNING_GENERATED="$(sed -n 's/^sw_telegram_signing_secret: //p' "${CRED_FILE}")"
[[ "${SIGNING_GENERATED}" =~ ^[0-9a-f]{64}$ ]] \
  || fail_assertion "生成的签名密钥不是 64 位十六进制（此处只校验形状，不打印值）"
[[ "${SIGNING_GENERATED}" != "${UI_TOKEN}" ]] || fail_assertion "签名密钥不该等于 UI token"
assert_contains "${RESULT}" "的 sw_telegram_signing_secret 键（0600）"
assert_contains "${ENV_AFTER}" "SW_TELEGRAM_SIGNING_SECRET=${SIGNING_GENERATED}"
# 原有的 SW_UI_TOKEN 行一个字节都没被碰过（这次写的是另一个 .env 键）。
assert_not_contains "${ENV_AFTER}" "SW_UI_TOKEN="
assert_value_absent_from_argv "${SIGNING_GENERATED}"
assert_not_contains "${RESULT}" "${SIGNING_GENERATED}"
assert_mode "${CRED_FILE}" 600
assert_no_tmp_residue
# 成功提示必须把这个键的**意义**说出来：它买到的是解耦。
assert_contains "${RESULT}" "签名密钥现在**显式落在这个键上**"
assert_contains "${RESULT}" "此后 SW_UI_TOKEN 再怎么换"

# 已有同名键时 --generate 拒绝覆盖，语义与 SW_UI_TOKEN 那条**逐字一致**。
CRED_CONTENT="sw_telegram_signing_secret: ${UI_TOKEN}
"
run_case "--generate refuses to overwrite an existing signing secret" \
  --key SW_TELEGRAM_SIGNING_SECRET --generate
assert_rejected_before_remote
assert_contains "${RESULT}" "已经有 sw_telegram_signing_secret 键，--generate 拒绝覆盖它"
assert_contains "${RESULT}" "旧值被盖掉就再也拿不回来"
assert_contains "${RESULT}" "--key SW_TELEGRAM_SIGNING_SECRET --from-credentials"
case_name="--generate refuses to overwrite an existing signing secret"
[[ "$(<"${CRED_FILE}")" == "${CRED_CONTENT%$'\n'}" ]] || fail_assertion "凭据文件被改动了"

# --from-credentials 读的也是自己那一行。
run_case "--from-credentials reads the signing secret from its own key" \
  --key SW_TELEGRAM_SIGNING_SECRET --from-credentials
assert_status 0
assert_contains "${RESULT}" "的 sw_telegram_signing_secret 键加载本机值"
assert_contains "${ENV_AFTER}" "SW_TELEGRAM_SIGNING_SECRET=${UI_TOKEN}"
assert_value_absent_from_argv "${UI_TOKEN}"
assert_not_contains "${RESULT}" "${UI_TOKEN}"

# 【环境变量那一层是 UI token 专有的，别泛化】导出 SW_OPS_UI_TOKEN 绝不能变成签名密钥的取值
# 来源：那会让"两边为什么不一致"多出一种查不清的可能，而且会把一个用来打探针的值推成签名密钥。
CRED_CONTENT="sw_ui_token: ${UI_TOKEN}
"
run_case "SW_OPS_UI_TOKEN is never a source for the signing secret" \
  SW_OPS_UI_TOKEN="${UI_TOKEN}" --key SW_TELEGRAM_SIGNING_SECRET --from-credentials
assert_rejected_before_remote
assert_contains "${RESULT}" "没有可用的 sw_telegram_signing_secret 键"
assert_contains "${RESULT}" "这个键**没有**对应的环境变量入口"
CRED_MISSING=1
CRED_CONTENT=""

ENV_CONTENT="${ENV_DEFAULT}SW_TELEGRAM_SIGNING_SECRET=${UI_TOKEN}
"
run_case "--show never prints the signing secret either" --show
assert_status 0
assert_contains "${RESULT}" "$(printf '  %-28s 已设置（凭据，值不回显：红线 R5）' SW_TELEGRAM_SIGNING_SECRET)"
assert_not_contains "${RESULT}" "${UI_TOKEN}"
assert_value_absent_from_argv "${UI_TOKEN}"
ENV_CONTENT="${ENV_DEFAULT}"

# bash -x 下签名密钥同样零出现（与 token 同一条红线，走的也是同一批 xtrace 守卫）。
SIGNING_XTRACE='TESTSECRET_xtrace-must-not-leak'
CRED_MISSING=0
CRED_CONTENT="sw_telegram_signing_secret: ${SIGNING_XTRACE}
"
RUN_XTRACE=1
run_case "bash -x never prints the signing secret" \
  --key SW_TELEGRAM_SIGNING_SECRET --from-credentials
assert_status 0
assert_value_absent_from_argv "${SIGNING_XTRACE}"
assert_not_contains "${RESULT}" "${SIGNING_XTRACE}"
assert_contains "${RESULT}" "+ sw_ops_adopt_credentials_key"
RUN_XTRACE=0
CRED_MISSING=1
CRED_CONTENT=""

# ================================================== 签名密钥轮换闸门（判定链四问）
#
# 【它修的是一次真事故】2026-08-22 编排方在生产上跑 --key SW_UI_TOKEN --generate 启用鉴权。
# 生产 .env 里 SW_TELEGRAM_SIGNING_SECRET 是空的，于是 core/telegram.py:151-154 的三级回落
# 里生效的那一级从 bot token 换到了 SW_UI_TOKEN——签名密钥当场被换掉。docs/RISKS.md §8.5
# 把"先确认没有待人点的确认卡"定为第 0 步前置，但那是**人工**前置，被跳过了。
# 下面按判定链的四问逐个钉，外加 fail-closed 与 override 的记录。
#
# 判定链：① 值没变 → 放行；② 改 UI token 而签名密钥已显式设 → 放行；
#         ③ 待人点的确认卡 0 条 → 放行；④ >0 → 拒绝；读不出来 → fail-closed 拒绝。
SIGNING_GATE_STATUS=45
SIGNING_PROBE_STATUS=46

# ---- 分支①：值与 .env 里现在那一行相同，签名密钥一个比特都不会变，放行 -----------------
# 这一格不是优化，是**防自锁**：上一次调用若死在"写入成功、重建失败"之间（退出码 36），
# .env 已是目标值而 core 还不是，重跑本命令是唯一的收敛动作。它必须连探针都不打。
ENV_CONTENT="${ENV_DEFAULT}SW_UI_TOKEN=${UI_TOKEN}
"
run_case "an unchanged value is let through without even probing" \
  TEST_AWAITING_CONFIRM=7 SW_OPS_UI_TOKEN="${UI_TOKEN}" --key SW_UI_TOKEN --from-credentials
assert_status 0
assert_contains "${RESULT}" "SW_UI_TOKEN 在 .env 里已经就是本次要写入的值：这次写入不会改变任何签名密钥，放行"
assert_contains "${RESULT}" "写入成功、容器重建失败"
# 连读数都不该发生：闸门在这一格短路，一个 dashboard 请求都不打。
assert_not_contains "${LOG}" "/api/v1/dashboard"
assert_contains "${RESULT}" "✓ 生产 .env 已变更、已生效"
ENV_CONTENT="${ENV_DEFAULT}"

# ---- 分支②：改 SW_UI_TOKEN，而生产 .env 里签名密钥已显式设且非空 → 放行 -----------------
# 这一格就是新键的全部价值：解耦成立之后，换 UI token 动不到签名密钥。
# 【夹具说明】用 --from-credentials 而不是 --generate，且**不导出** SW_OPS_UI_TOKEN：
# 这样闸门与随后的 restart.sh 探针拿的是同一个（新）值，容器重建之后 core 认它，探针 200。
# 换成"导出旧 token + --generate"的话，闸门这一格照样放行，但 restart.sh 会拿旧值去探新
# 容器而 401——那是一次真实且正确的失败，只是与本条要考察的东西无关。
CRED_MISSING=0
CRED_CONTENT="sw_ui_token: ${UI_TOKEN}
"
ENV_CONTENT="${ENV_DEFAULT}SW_UI_TOKEN=TESTTOKEN_old-value
SW_TELEGRAM_SIGNING_SECRET=TESTSECRET_explicit-value
"
run_case "an explicit signing secret decouples SW_UI_TOKEN from the gate" \
  TEST_AWAITING_CONFIRM=7 --key SW_UI_TOKEN --from-credentials
assert_status 0
assert_contains "${RESULT}" "SW_TELEGRAM_SIGNING_SECRET 已显式设置且非空：改 SW_UI_TOKEN **不会**动确认卡的签名密钥，放行"
assert_contains "${RESULT}" "三级回落停在第 1 级 SW_TELEGRAM_SIGNING_SECRET——SW_UI_TOKEN 排在它后面，够不着"
# 有 7 条卡等着人点也照样放行——因为那 7 条的卡签的不是 SW_UI_TOKEN。
assert_not_contains "${LOG}" "/api/v1/dashboard"
# 别人的凭据值只被判空，绝不回显。
assert_not_contains "${RESULT}" "TESTSECRET_explicit-value"
assert_contains "${ENV_AFTER}" "SW_TELEGRAM_SIGNING_SECRET=TESTSECRET_explicit-value"
assert_contains "${ENV_AFTER}" "SW_UI_TOKEN=${UI_TOKEN}"
assert_contains "${RESULT}" "✓ 生产 .env 已变更、已生效"
CRED_MISSING=1
CRED_CONTENT=""

# 同一个位置，值是**空**的：core/telegram.py:151 会 `or ""` 再 `.strip()`，所以空等于没设，
# 回落照样落到 SW_UI_TOKEN 上 —— 必须走判定链，不许被当成"已显式设置"放行。
for empty_form in '' '   ' '""' "''"; do
  ENV_CONTENT="${ENV_DEFAULT}SW_UI_TOKEN=TESTTOKEN_old-value
SW_TELEGRAM_SIGNING_SECRET=${empty_form}
"
  run_case "an empty signing secret [${empty_form}] does not count as explicitly set" \
    TEST_AWAITING_CONFIRM=2 SW_OPS_UI_TOKEN=TESTTOKEN_old-value --key SW_UI_TOKEN --generate
  assert_status "${SIGNING_GATE_STATUS}"
  assert_contains "${RESULT}" "还有 2 条待人点的确认卡，拒绝换 Telegram 确认卡的签名密钥"
  assert_not_contains "${RESULT}" "已显式设置且非空"
done
ENV_CONTENT="${ENV_DEFAULT}"

# ---- 分支③：待人点的确认卡 0 条 → 放行 ------------------------------------------
run_case "zero pending confirm cards lets the rotation through" \
  TEST_AWAITING_CONFIRM=0 --key SW_UI_TOKEN --generate
assert_status 0
assert_contains "${RESULT}" "事前闸门  待人点的确认卡 0 条，允许换签名密钥"
assert_contains "${RESULT}" "本闸门没有事后那一道"
assert_contains "${LOG}" "curl <-q> <-fsS> <--max-time> <20> <-w> <\n%{http_code}> <--config> <-> <http://127.0.0.1:8000/api/v1/dashboard?days=1>"
assert_contains "${RESULT}" "✓ 生产 .env 已变更、已生效"
# 只报计数：dashboard 的响应真的带 events[].title / attention[].name，一个字节都不许出现。
assert_not_contains "${RESULT}" "SENTINEL_ITEM_TITLE"
assert_not_contains "${RESULT}" "SENTINEL_ACCOUNT_NAME"
assert_not_contains "${RESULT}" "SENTINEL_EVENT_DETAIL"

# ---- 分支④：有卡 → 拒绝，且**给得出处置** ----------------------------------------
# docs/RISKS.md §14 把"闸门给得出诊断、工具面给不出处置"列为反模式。这一条逐项钉住处置。
run_case "pending confirm cards refuse the rotation and hand back a way out" \
  TEST_AWAITING_CONFIRM=3 --key SW_UI_TOKEN --generate
assert_status "${SIGNING_GATE_STATUS}"
assert_contains "${RESULT}" "还有 3 条待人点的确认卡，拒绝换 Telegram 确认卡的签名密钥"
assert_contains "${RESULT}" "**.env 一个字节都没动**"
# 上界口径必须写出来，且绝不许写成"3 条会失效"。
assert_contains "${RESULT}" "条数是**上界**不是精确值"
assert_not_contains "${RESULT}" "3 条会失效"
# 处置一：等卡被点掉或被 TTL 处理掉。
assert_contains "${RESULT}" "等这些卡被人点掉、或被 SW_CONFIRM_TTL_HOURS（默认 24 小时）到点自动驳回"
# 处置二：显式设上签名密钥可以**永久**解开这条耦合，并给出可直接粘贴的命令。
assert_contains "${RESULT}" "bash scripts/ops/env_set.sh --key SW_TELEGRAM_SIGNING_SECRET --generate"
assert_contains "${RESULT}" "这条耦合就永久解开了"
# 处置三：override，名字本身说出后果。
assert_contains "${RESULT}" "--accept-breaking-pending-confirm-cards"
# 处置四（本轮踩到的坑）：--generate 已经在本机落盘了，重跑不是同一条命令。
assert_contains "${RESULT}" "再跑一次 --generate 会被「已经有这个键，拒绝覆盖」挡住"
assert_contains "${RESULT}" "--key SW_UI_TOKEN --from-credentials"
# 生产维持原状：没备份、没写入、没重建、没重启。
assert_contains "${ENV_AFTER}" "SW_USE_FAKE_PUBLISHERS=true"
assert_not_contains "${ENV_AFTER}" "SW_UI_TOKEN="
assert_log_count "container-recreated" 0
assert_not_contains "${LOG}" "restart-sh <"
case_name="pending confirm cards refuse the rotation and hand back a way out"
[[ ! -d "${BACKUP_DIR}" ]] || fail_assertion "闸门拒绝时不该生成备份"

# --generate 那条路上，闸门探针用的是**旧**值（生产现在认的那个），而要写进去的是刚生成的
# 新值。两者不是一回事，输出里必须说清；--from-credentials 上它们本来就是同一个值，
# 多说那一句反而是错的，所以那条路上不许出现它。
ENV_CONTENT="${ENV_DEFAULT}SW_UI_TOKEN=TESTTOKEN_prod-value
"
run_case "--generate says which token the gate probed with" \
  TEST_AWAITING_CONFIRM=3 SW_OPS_UI_TOKEN=TESTTOKEN_prod-value --key SW_UI_TOKEN --generate
assert_status "${SIGNING_GATE_STATUS}"
assert_contains "${RESULT}" "上面这一个是**生产现在认的**那个 token（闸门要用它去读待人点的确认卡条数），不是本次 --generate 出来的新值"
CRED_MISSING=0
CRED_CONTENT="sw_ui_token: TESTTOKEN_prod-value
"
run_case "--from-credentials does not claim the loaded token is a different value" \
  TEST_AWAITING_CONFIRM=3 --key SW_UI_TOKEN --from-credentials
assert_status 0
assert_not_contains "${RESULT}" "不是本次 --generate 出来的新值"
# 顺带：这一条走的是分支①（值与 .env 里那一行相同），所以连探针都没打。
assert_not_contains "${LOG}" "/api/v1/dashboard"
CRED_MISSING=1
CRED_CONTENT=""
ENV_CONTENT="${ENV_DEFAULT}"

# 改签名密钥**本身**没有分支②那格免检：设它就是在改第一级，不管原来落在哪一级。
CRED_MISSING=0
CRED_CONTENT="sw_telegram_signing_secret: ${UI_TOKEN}
"
ENV_CONTENT="${ENV_DEFAULT}SW_TELEGRAM_SIGNING_SECRET=TESTSECRET_already-set
"
run_case "changing the signing secret itself is always gated" \
  TEST_AWAITING_CONFIRM=1 --key SW_TELEGRAM_SIGNING_SECRET --from-credentials
assert_status "${SIGNING_GATE_STATUS}"
assert_contains "${RESULT}" "还有 1 条待人点的确认卡"
assert_not_contains "${RESULT}" "已显式设置且非空"
# 这个方向的"处置二"不该让人去跑自己刚被拦下的那条命令。
assert_contains "${RESULT}" "本命令**就是**那个根治动作"
# --from-credentials 没在本机留下新值，所以重跑就是同一条命令。
assert_contains "${RESULT}" "重跑用同一条命令即可"
assert_contains "${ENV_AFTER}" "SW_TELEGRAM_SIGNING_SECRET=TESTSECRET_already-set"
CRED_MISSING=1
CRED_CONTENT=""
ENV_CONTENT="${ENV_DEFAULT}"

# ---- fail-closed：读不出条数的每一种，都拒绝，且都不许被读成"0 条" -------------------
# ① 真 401：生产开着 token 而本机拿的是另一个值。**这正是 --from-credentials 要收敛的那种
#    不一致**，所以它必然撞上这一格——下面 override 那一节接着钉"它有出路"。
ENV_CONTENT="${ENV_DEFAULT}SW_UI_TOKEN=TESTTOKEN_prod-value
"
run_case "a mismatched token makes the gate fail closed instead of guessing" \
  SW_OPS_UI_TOKEN=TESTTOKEN_local-value --key SW_UI_TOKEN --from-credentials
assert_status "${SIGNING_PROBE_STATUS}"
assert_contains "${RESULT}" "**读不出**待人点的确认卡条数"
assert_contains "${RESULT}" "返回 401 未授权"
assert_contains "${RESULT}" "这是\"不知道\"，不是\"探到了有卡\""
assert_contains "${RESULT}" "本闸门**没有事后那一道**"
assert_contains "${ENV_AFTER}" "SW_UI_TOKEN=TESTTOKEN_prod-value"
assert_log_count "container-recreated" 0
ENV_CONTENT="${ENV_DEFAULT}"

# ② 404：老版本 core 还没有 /api/v1/dashboard。点名说是这一种，不许混进"连不上"。
run_case "a dashboard 404 fails closed and names the missing endpoint" \
  TEST_DASHBOARD_HTTP_CODE=404 --key SW_UI_TOKEN --generate
assert_status "${SIGNING_PROBE_STATUS}"
assert_contains "${RESULT}" "返回 404，这版 core 没有这个端点"
assert_contains "${RESULT}" "404 那一格的含义"

# ③ 传输失败：带上 curl 退出码，绝不误报成鉴权问题。
run_case "a dashboard transport failure fails closed without claiming auth" \
  TEST_DASHBOARD_CURL_STATUS=7 --key SW_UI_TOKEN --generate
assert_status "${SIGNING_PROBE_STATUS}"
assert_contains "${RESULT}" "curl 退出码 7、HTTP 000"
assert_not_contains "${RESULT}" "返回 401 未授权"

# ④ 响应不是 JSON。
run_case "an unparsable dashboard payload fails closed" \
  TEST_DASHBOARD_JSON='not json' --key SW_UI_TOKEN --generate
assert_status "${SIGNING_PROBE_STATUS}"
assert_contains "${RESULT}" "响应不是合法 JSON"

# ⑤ 失败外壳：ok=false 时 data 是 null，绝不许读成"没有卡"。
run_case "a failed dashboard envelope fails closed" \
  TEST_DASHBOARD_JSON='{"ok":false,"data":null,"error":{"code":"internal","message":"boom","detail":null}}' \
  --key SW_UI_TOKEN --generate
assert_status "${SIGNING_PROBE_STATUS}"
assert_contains "${RESULT}" "返回失败外壳"

# ⑥ 字段缺失（老 core 有 counters 却没有 awaiting_confirm）——"取不到"最像"0 条"的一种。
run_case "a counters block without awaiting_confirm fails closed instead of passing" \
  TEST_DASHBOARD_JSON='{"ok":true,"data":{"generated_at":"2026-08-22T02:00:00Z","window_days":1,"counters":{"pending_review":0,"scheduled":0},"budget":{},"platforms":[],"attention":[],"events":[]},"error":null}' \
  --key SW_UI_TOKEN --generate
assert_status "${SIGNING_PROBE_STATUS}"
assert_contains "${RESULT}" "没有 counters.awaiting_confirm 这个非负整数"

# ⑦ 类型不对（契约漂移）：字符串 "0" 不是计数，不许被当成 0 条放行。
run_case "a non integer awaiting_confirm fails closed rather than being coerced" \
  TEST_DASHBOARD_JSON='{"ok":true,"data":{"counters":{"awaiting_confirm":"0"}},"error":null}' \
  --key SW_UI_TOKEN --generate
assert_status "${SIGNING_PROBE_STATUS}"
assert_contains "${RESULT}" "字段缺失或类型不对"

# ⑧ 容器里那一步自己没跑起来（容器没起 / python3 不在）：同样只能说"不知道"。
run_case "a failing in-container parse step fails closed" \
  TEST_AWAITING_EXEC_STATUS=1 --key SW_UI_TOKEN --generate
assert_status "${SIGNING_PROBE_STATUS}"
assert_contains "${RESULT}" "解析 /api/v1/dashboard 的那一步自己失败了（退出码 1）"

# ---- override：名字说出后果，输出如实记下"你接受了什么" -----------------------------
# ① 有卡时：记条数，并且必须说清那是**上界**。
run_case "the override records exactly which cards you accepted breaking" \
  TEST_AWAITING_CONFIRM=3 --key SW_UI_TOKEN --generate --accept-breaking-pending-confirm-cards
assert_status 0
assert_contains "${RESULT}" "待人点的确认卡 3 条，但你给了 --accept-breaking-pending-confirm-cards，放行"
assert_contains "${RESULT}" "你接受的是：这 3 条里**已经推出卡**的那部分"
assert_contains "${RESULT}" "3 是**上界**不是精确值"
assert_contains "${RESULT}" "工作台「确认发布」不经 Telegram"
assert_not_contains "${RESULT}" "3 条会失效"
assert_contains "${RESULT}" "✓ 生产 .env 已变更、已生效"

# ② 读不出条数时：它**也**覆盖，但记录必须诚实——没有条数可记就说没有。
#    这一档不给出路的话，"两边 token 不一致"这种必然 401 的情形永远收敛不了。
ENV_CONTENT="${ENV_DEFAULT}SW_UI_TOKEN=TESTTOKEN_prod-value
"
run_case "the override also unblocks the unknown branch, and says so honestly" \
  SW_OPS_UI_TOKEN=TESTTOKEN_local-value --key SW_UI_TOKEN --from-credentials \
  --accept-breaking-pending-confirm-cards
assert_status 0
assert_contains "${RESULT}" "读不出待人点的确认卡条数（GET /api/v1/dashboard 返回 401 未授权），但你给了 --accept-breaking-pending-confirm-cards，放行"
assert_contains "${RESULT}" "**在不知道有多少张卡等着人点的情况下**换签名密钥"
assert_contains "${RESULT}" "本次**没有条数可记**"
# 收敛真的发生了：.env 换成了本机那个值。
assert_contains "${ENV_AFTER}" "SW_UI_TOKEN=TESTTOKEN_local-value"
ENV_CONTENT="${ENV_DEFAULT}"

# ③ 拿错旗子要当场说清，绝不静默忽略——静默会让人以为自己已经绕过了某道闸门。
run_case "the override is refused on a key it has nothing to do with" \
  --key DAILY_TOKEN_BUDGET --value 5 --accept-breaking-pending-confirm-cards
assert_rejected_before_remote
assert_contains "${RESULT}" "--accept-breaking-pending-confirm-cards 对 --key DAILY_TOKEN_BUDGET 没有意义"
assert_contains "${RESULT}" "有这道闸门的是这几个凭据类键（从白名单派生，不是手写的第二份名单）：SW_UI_TOKEN、SW_TELEGRAM_SIGNING_SECRET、TELEGRAM_BOT_TOKEN"

run_case "--show refuses the override too" --show --accept-breaking-pending-confirm-cards
assert_status 1
assert_contains "${RESULT}" "--show 本来就只读"


# ===================== TELEGRAM_BOT_TOKEN：取值路径（第一个**造不出来**的凭据类键）
#
# 【这一节钉的两件事，别混】
#   ① 它走的是**自己那一行**凭据（telegram_bot_token），不是别人的——键名参数化这一层，
#      第三个键接上之后才真的有对照。
#   ② 它**不能 --generate**，而且拒绝必须说得出为什么、以及正确做法。这一格在脚本里是按
#      策略表第五格（POLICY_CRED_ORIGIN）判的，不是硬写的键名；下面有一条源码级断言钉住
#      "两种取值来源都真的存在"，免得这个性质哪天塌成一个常量、拒绝分支变成死代码。
CRED_MISSING=1
CRED_CONTENT=""
ENV_CONTENT="${ENV_DEFAULT}"

# 形状：`<数字 bot_id>:<授权串>`。这两个夹具值都落在 sw_env_value_re 那条正则里，
# 而且**故意**带上 `-` 与 `_`（授权串的真实字符集里有它们）。
BOT_TOKEN='8123456789:TESTFAKE-bot-token_AAAAAAAAAAAAAAAA'
BOT_TOKEN_ALT='8123456789:TESTFAKE-bot-token_BBBBBBBBBBBBBBBB'

run_case "TELEGRAM_BOT_TOKEN refuses --value（它同样是凭据，值不进 argv）" \
  --key TELEGRAM_BOT_TOKEN --value "${BOT_TOKEN}"
assert_rejected_before_remote
assert_contains "${RESULT}" "TELEGRAM_BOT_TOKEN 不接受 --value"
assert_contains "${RESULT}" "/proc/*/cmdline 世界可读"
# "改用什么"这一句必须跟着分支：给一个造不出值的键推荐 --generate，就是给一条走不通的路。
assert_contains "${RESULT}" "改用 --from-credentials（推送本机已持有的那个）。"
assert_not_contains "${RESULT}" "改用 --generate"

run_case "TELEGRAM_BOT_TOKEN refuses --generate and says where the value comes from" \
  --key TELEGRAM_BOT_TOKEN --generate
assert_rejected_before_remote
assert_contains "${RESULT}" "TELEGRAM_BOT_TOKEN 不能 --generate"
assert_contains "${RESULT}" "它的值由**外部**签发"
assert_contains "${RESULT}" "@BotFather"
assert_contains "${RESULT}" "telegram_bot_token 键"
assert_contains "${RESULT}" "bash scripts/ops/env_set.sh --key TELEGRAM_BOT_TOKEN --from-credentials"
# 拒绝的理由不能只说"造不出来"：真正要命的是那种失败**不响**——线程一直活着、polling 一直报 true。
assert_contains "${RESULT}" "照样报 true"
case_name="TELEGRAM_BOT_TOKEN refuses --generate and says where the value comes from"
[[ ! -f "${CRED_FILE}" ]] || fail_assertion "--generate 被拒时一个字节都不该写进凭据文件"

run_case "TELEGRAM_BOT_TOKEN needs a value source, and that message never offers --generate" \
  --key TELEGRAM_BOT_TOKEN
assert_rejected_before_remote
assert_contains "${RESULT}" "TELEGRAM_BOT_TOKEN 需要 --from-credentials"
assert_contains "${RESULT}" "它**没有** --generate 这条路"
assert_not_contains "${RESULT}" "需要 --generate 或 --from-credentials"

# 另外两个凭据类键的那句话一个字都不许变（性质分支不该把它们也改了）。
run_case "the other credential keys still offer both value sources" --key SW_TELEGRAM_SIGNING_SECRET
assert_rejected_before_remote
assert_contains "${RESULT}" "需要 --generate 或 --from-credentials 二选一"

# ---- --from-credentials 读的是**它自己那一行** ------------------------------------
# 夹具里同时摆上另外两个凭据键：键名若被写死回去，这一条会读到别人的值，而那两个值都过不了
# bot token 的形状校验，所以会当场红——这正是我们要它红的方式。
CRED_MISSING=0
CRED_CONTENT="sw_ui_token: ${UI_TOKEN}
sw_telegram_signing_secret: TESTSECRET_not-a-bot-token
telegram_bot_token: ${BOT_TOKEN}
"
ENV_CONTENT="${ENV_DEFAULT}SW_TELEGRAM_SIGNING_SECRET=TESTSECRET_explicit-value
"
run_case "--from-credentials reads the bot token from its own key（第 1 级挡着，闸门放行）" \
  TEST_AWAITING_CONFIRM=7 --key TELEGRAM_BOT_TOKEN --from-credentials
assert_status 0
assert_contains "${RESULT}" "的 telegram_bot_token 键加载本机值"
assert_contains "${ENV_AFTER}" "TELEGRAM_BOT_TOKEN=${BOT_TOKEN}"
assert_value_absent_from_argv "${BOT_TOKEN}"
assert_not_contains "${RESULT}" "${BOT_TOKEN}"
# 真值表第 4 行：一级非空 → 放行，连读数都不打。7 条卡在那儿也不拦——那 7 条签的不是 bot token。
assert_contains "${RESULT}" ".env 里 SW_TELEGRAM_SIGNING_SECRET 已显式设置且非空：改 TELEGRAM_BOT_TOKEN **不会**动确认卡的签名密钥，放行"
assert_contains "${RESULT}" "三级回落停在第 1 级 SW_TELEGRAM_SIGNING_SECRET——TELEGRAM_BOT_TOKEN 排在它后面，够不着"
assert_not_contains "${LOG}" "/api/v1/dashboard"
# 别人的凭据只被判空，绝不回显；另外两行一个字节都没被碰过。
assert_not_contains "${RESULT}" "TESTSECRET_explicit-value"
assert_contains "${ENV_AFTER}" "SW_TELEGRAM_SIGNING_SECRET=TESTSECRET_explicit-value"
assert_not_contains "${ENV_AFTER}" "SW_UI_TOKEN="
# 动手前的警告与收尾提示都要把这个键**特有**的三件事说出来。
assert_contains "${RESULT}" "换 bot token 换的是**整条推送载体的身份**"
assert_contains "${RESULT}" "409 双轮询"
assert_contains "${RESULT}" "**刻意不在这上面设闸门**"
assert_contains "${RESULT}" "**旧 token 已经作废**"
assert_contains "${RESULT}" "polling 那一格在这个键上不可信"
assert_contains "${RESULT}" "Telegram 轮询冲突（error_code=409）"
assert_contains "${RESULT}" "**不是**再换一次 token"

# 环境变量那一层是 UI token 专有的，这里同样不许泛化。
CRED_CONTENT="sw_ui_token: ${UI_TOKEN}
"
run_case "SW_OPS_UI_TOKEN is never a source for the bot token either" \
  SW_OPS_UI_TOKEN="${UI_TOKEN}" --key TELEGRAM_BOT_TOKEN --from-credentials
assert_rejected_before_remote
assert_contains "${RESULT}" "没有可用的 telegram_bot_token 键"
assert_contains "${RESULT}" "这个键**没有**对应的环境变量入口"
# "怎么新建一个"必须指向 BotFather，而不是一条它压根没有的 --generate。
assert_contains "${RESULT}" "值从哪儿来：人去 @BotFather 拿"
assert_not_contains "${RESULT}" "要新建一个：bash scripts/ops/env_set.sh"

# ---- 形状校验：字符全合法但形状不对，报错必须说"形状"，不许说"字符" ------------------
# 【这条用例是本轮改那句文案的**理由本身**】64 位十六进制串（本仓 --generate 出来的就是这个
# 形状）字符全在共用字符集里，旧文案会说"含有不被允许的字符"再附一份它全部满足的允许集——
# 一次回答得很确定的错答。
CRED_CONTENT="telegram_bot_token: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
"
run_case "a charset-legal value with the wrong shape is refused as a shape problem" \
  --key TELEGRAM_BOT_TOKEN --from-credentials
assert_rejected_before_remote
assert_contains "${RESULT}" "TELEGRAM_BOT_TOKEN 的值过不了形状校验（此处不回显值：它是凭据，红线 R5）"
assert_contains "${RESULT}" "这个键接受的形状：<数字 bot_id>:<授权串>"
assert_contains "${RESULT}" '^[0-9]{5,16}:[A-Za-z0-9_-]{30,64}$'
assert_not_contains "${RESULT}" "含有不被允许的字符"
assert_not_contains "${RESULT}" "aaaaaaaaaaaaaaaa"

# 被截断的 token（最像"粘错了一半"的那种）同样拦下。
CRED_CONTENT="telegram_bot_token: 8123456789:TESTFAKE
"
run_case "a truncated bot token is refused too" --key TELEGRAM_BOT_TOKEN --from-credentials
assert_rejected_before_remote
assert_contains "${RESULT}" "过不了形状校验"

# 真 token 形状的值当然放得过去（反过来钉住上面那条正则不是"什么都拦"）。
CRED_CONTENT="telegram_bot_token: ${BOT_TOKEN_ALT}
"
ENV_CONTENT="${ENV_DEFAULT}SW_TELEGRAM_SIGNING_SECRET=TESTSECRET_explicit-value
"
run_case "a well-shaped bot token goes through" --key TELEGRAM_BOT_TOKEN --from-credentials
assert_status 0
assert_contains "${ENV_AFTER}" "TELEGRAM_BOT_TOKEN=${BOT_TOKEN_ALT}"

# ================== 签名密钥闸门补到第三级：真值表逐行走一遍
#
# 判定按"回落链上排在本键前面的那几级里有没有非空的"，不按键名。三级各自的表现：
#   一级非空                → 放行（上面那条用例已经钉过）
#   一级空、二级非空        → 放行，且必须说清停在**第 2 级**
#   一级空、二级也空        → bot token 就是生效的签名密钥 → 走读数
CRED_MISSING=0
CRED_CONTENT="telegram_bot_token: ${BOT_TOKEN}
"

# ---- 二级挡住三级：链要真的**走过**空着的第一级，而不是遇到它就停 --------------------
ENV_CONTENT="${ENV_DEFAULT}SW_TELEGRAM_SIGNING_SECRET=
SW_UI_TOKEN=${UI_TOKEN}
"
run_case "the chain walks past an empty level-1 and stops at level-2" \
  TEST_AWAITING_CONFIRM=7 SW_OPS_UI_TOKEN="${UI_TOKEN}" --key TELEGRAM_BOT_TOKEN --from-credentials
assert_status 0
assert_contains "${RESULT}" ".env 里 SW_UI_TOKEN 已显式设置且非空：改 TELEGRAM_BOT_TOKEN **不会**动确认卡的签名密钥，放行"
assert_contains "${RESULT}" "三级回落停在第 2 级 SW_UI_TOKEN——TELEGRAM_BOT_TOKEN 排在它后面，够不着"
assert_not_contains "${LOG}" "/api/v1/dashboard"
assert_contains "${ENV_AFTER}" "TELEGRAM_BOT_TOKEN=${BOT_TOKEN}"

# ---- 上面两级都空：bot token **就是**生效的签名密钥，0 条才放行 ----------------------
# .env.example 的出厂形态就是这一格：两级都空。今天的生产不是这个形态（二级设着），
# 但闸门缺了这一格就是一个**活的**缺口，不是理论上的。
ENV_CONTENT="${ENV_DEFAULT}"
run_case "with both higher levels empty the bot token IS the signing key（0 条放行）" \
  TEST_AWAITING_CONFIRM=0 --key TELEGRAM_BOT_TOKEN --from-credentials
assert_status 0
assert_contains "${RESULT}" "事前闸门  待人点的确认卡 0 条，允许换签名密钥"
assert_contains "${LOG}" "/api/v1/dashboard"
assert_not_contains "${RESULT}" "已显式设置且非空"
assert_contains "${ENV_AFTER}" "TELEGRAM_BOT_TOKEN=${BOT_TOKEN}"
# 只报计数：dashboard 的自由文本一个字节都不许出来。
assert_not_contains "${RESULT}" "SENTINEL_ITEM_TITLE"
assert_not_contains "${RESULT}" "SENTINEL_ACCOUNT_NAME"

run_case "with both higher levels empty, pending cards refuse the bot token rotation" \
  TEST_AWAITING_CONFIRM=4 --key TELEGRAM_BOT_TOKEN --from-credentials
assert_status "${SIGNING_GATE_STATUS}"
assert_contains "${RESULT}" "还有 4 条待人点的确认卡，拒绝换 Telegram 确认卡的签名密钥"
assert_contains "${RESULT}" "**.env 一个字节都没动**"
# 处置二对这个键仍然是"去把第一级设上"，而且命令要指向**第一级**那个键。
assert_contains "${RESULT}" "处置二（根治，只做一次）：先把签名密钥显式设上，此后 TELEGRAM_BOT_TOKEN 再怎么换都不动它"
assert_contains "${RESULT}" "bash scripts/ops/env_set.sh --key SW_TELEGRAM_SIGNING_SECRET --generate"
# --from-credentials 没在本机留下新值，所以重跑就是同一条命令。
assert_contains "${RESULT}" "重跑用同一条命令即可"
assert_contains "${ENV_AFTER}" "TELEGRAM_BOT_TOKEN=123456:TESTFAKE"
assert_log_count "container-recreated" 0
assert_not_contains "${LOG}" "restart-sh <"

# fail-closed：两级都空、而条数**读不出来** → 46。这一格与"有卡"是两件事，绝不许被读成 0 条。
# 【为什么这条要单独有】读数那一段是三个键共用的，但"走不走到它"是按链判的。只在
# SW_UI_TOKEN 上钉 fail-closed，测不出"第三级到底有没有真的走到读数这一步"。
run_case "with both higher levels empty, an unreadable count fails closed for the bot token too" \
  TEST_AWAITING_CONFIRM=0 TEST_DASHBOARD_HTTP_CODE=404 --key TELEGRAM_BOT_TOKEN --from-credentials
assert_status "${SIGNING_PROBE_STATUS}"
assert_contains "${RESULT}" "**读不出**待人点的确认卡条数"
assert_contains "${RESULT}" "返回 404，这版 core 没有这个端点"
assert_contains "${RESULT}" "本闸门**没有事后那一道**"
assert_contains "${ENV_AFTER}" "TELEGRAM_BOT_TOKEN=123456:TESTFAKE"
assert_log_count "container-recreated" 0

# ---- 空值判定与 core 同口径：四种"看起来设了其实没设"的写法都不许免检 ------------------
# 与 SW_UI_TOKEN 那一组用的是**同一份**判定（远端 signing_level_is_set，唯一一处）。
for empty_form in '' '   ' '""' "''"; do
  ENV_CONTENT="${ENV_DEFAULT}SW_TELEGRAM_SIGNING_SECRET=${empty_form}
"
  run_case "an empty level-1 [${empty_form}] does not exempt the bot token either" \
    TEST_AWAITING_CONFIRM=2 --key TELEGRAM_BOT_TOKEN --from-credentials
  assert_status "${SIGNING_GATE_STATUS}"
  assert_contains "${RESULT}" "还有 2 条待人点的确认卡"
  assert_not_contains "${RESULT}" "已显式设置且非空"
done
ENV_CONTENT="${ENV_DEFAULT}"

# ---- 值没变那一格（防自锁）对这个键同样成立，而且连探针都不打 -------------------------
ENV_CONTENT="$(printf '%s' "${ENV_DEFAULT}" | sed "s|^TELEGRAM_BOT_TOKEN=.*$|TELEGRAM_BOT_TOKEN=${BOT_TOKEN}|")
"
run_case "an unchanged bot token is let through without even probing" \
  TEST_AWAITING_CONFIRM=7 --key TELEGRAM_BOT_TOKEN --from-credentials
assert_status 0
assert_contains "${RESULT}" "TELEGRAM_BOT_TOKEN 在 .env 里已经就是本次要写入的值：这次写入不会改变任何签名密钥，放行"
assert_not_contains "${LOG}" "/api/v1/dashboard"
ENV_CONTENT="${ENV_DEFAULT}"

# ---- override 对这个键照样有效（它是同一道闸门） --------------------------------------
run_case "the override works for the bot token as well" \
  TEST_AWAITING_CONFIRM=3 --key TELEGRAM_BOT_TOKEN --from-credentials \
  --accept-breaking-pending-confirm-cards
assert_status 0
assert_contains "${RESULT}" "待人点的确认卡 3 条，但你给了 --accept-breaking-pending-confirm-cards，放行"
assert_contains "${RESULT}" "3 是**上界**不是精确值"
assert_contains "${ENV_AFTER}" "TELEGRAM_BOT_TOKEN=${BOT_TOKEN}"

# ---- --write-only：这条禁令**不是本轮新写的**，它是"有闸门就不许 --write-only"自动落到
#      这个键上的结果。这里钉的就是"自动成立"这件事，外加外部签发凭据多出来的那条理由。
run_case "--write-only is refused for the bot token too（有闸门，禁令自动成立）" \
  --key TELEGRAM_BOT_TOKEN --from-credentials --write-only
assert_rejected_before_remote
assert_contains "${RESULT}" "--write-only 不能与 --key TELEGRAM_BOT_TOKEN --from-credentials 一起用"
assert_contains "${RESULT}" "这一格没有事后闸门兜底"
# 外部签发的凭据上，"上了膛没击发"这个比喻说轻了：旧值当场作废，所以是**当场哑火**。
assert_contains "${RESULT}" "是**当场哑火**"
assert_contains "${RESULT}" "polling 只看线程活没活，照样报 true"
assert_not_contains "${RESULT}" "--key TELEGRAM_BOT_TOKEN --value "
assert_not_contains "${ENV_AFTER}" "${BOT_TOKEN}"

# 反过来钉住：那条加料只长在**外部签发**的凭据上，不许溢到另外两个键。
run_case "the extra --write-only reason only shows up on externally issued credentials" \
  SW_OPS_UI_TOKEN="${UI_TOKEN}" --key SW_UI_TOKEN --from-credentials --write-only
assert_rejected_before_remote
assert_contains "${RESULT}" "--write-only 不能与 --key SW_UI_TOKEN --from-credentials 一起用"
assert_not_contains "${RESULT}" "当场哑火"

# ---- bash -x 下 bot token 同样零出现（同一批 xtrace 守卫） ---------------------------
BOT_XTRACE='8123456789:TESTFAKE-xtrace-must-not-leak_AAAA'
CRED_CONTENT="telegram_bot_token: ${BOT_XTRACE}
"
ENV_CONTENT="${ENV_DEFAULT}SW_TELEGRAM_SIGNING_SECRET=TESTSECRET_explicit-value
"
RUN_XTRACE=1
run_case "bash -x never prints the bot token" --key TELEGRAM_BOT_TOKEN --from-credentials
assert_status 0
assert_value_absent_from_argv "${BOT_XTRACE}"
assert_not_contains "${RESULT}" "${BOT_XTRACE}"
assert_contains "${RESULT}" "+ sw_ops_adopt_credentials_key"
RUN_XTRACE=0

CRED_MISSING=1
CRED_CONTENT=""
ENV_CONTENT="${ENV_DEFAULT}"

# =========================================== 纵深防御：R7 前缀校验（本机与远端各一道）
# 这道校验今天是**冗余的**——白名单里的键都不带禁止前缀，正常路径永远走不到它。
# 但"将来有人扩白名单"正是它存在的理由，所以这里用**改写过的副本**把白名单撑开一格，
# 让那条路径真的可达，再断言两道校验各自都拦得住。只有这样它才是被测过的代码，
# 而不是一段谁也没执行过、出问题也没人知道的注释。
MUT_DIR="${TMP}/mutants"
mkdir -p "${MUT_DIR}"
cp "${ROOT}/scripts/ops/ui_token.sh" "${MUT_DIR}/ui_token.sh"
# 副本要能跑完整条路：env_set.sh 会 source 同目录的 ui_token.sh，并调用同目录的 restart.sh。
cp "${ROOT}/scripts/ops/restart.sh" "${MUT_DIR}/restart.sh"

# 副本 ①：把白名单里的 SW_USE_FAKE_PUBLISHERS **改名**成一个带禁止前缀的键。
# 用改名而不是插入新分支：改名只是一条全局替换，不依赖 sed 的替换里能不能写换行
# （BSD sed 与 GNU sed 在这一点上不一样），副本本身出错的可能小得多。
# 本机那道 R7 校验原样保留。
#
# 【本轮把匹配串从 `SW_USE_FAKE_PUBLISHERS)` 放宽成整个标识符——这不是随手改的】
# 白名单扩容之后，这个键在 sw_env_value_re / sw_env_value_help / sw_env_alias 三张表里
# 都躲在 `A|B|C)` 这样的多值分支里，`SW_USE_FAKE_PUBLISHERS)` 一个都匹配不到。结果是
# 副本只改了一半：sw_env_policy 认得 DSH_TESTKEY，sw_env_value_re 不认，脚本在取正则那
# 一步就 set -e 退出了——**用例照样"红"，但红在完全无关的地方**，R7 那条路径一个字节
# 都没跑到。这正是"假件/副本语义与真实语义不符导致测试骗人"的同一类事故。
sed -e 's|SW_USE_FAKE_PUBLISHERS|DSH_TESTKEY|g' "${SCRIPT}" >"${MUT_DIR}/widened.sh"

case_name="the widened-whitelist mutant is actually rewritten"
# 自检其一：改名必须**全量**，一处不漏（否则就是上面说的那种半截副本）。
if grep -q 'SW_USE_FAKE_PUBLISHERS' "${MUT_DIR}/widened.sh"; then
  fail_assertion "副本 ① 里还残留 SW_USE_FAKE_PUBLISHERS，改名没做全——下面两条用例会红在无关的地方"
fi
# 自检其二：五张按键表**每一张**都要认识这个新名字。逐张查，别只数总数。
for mut_table in sw_env_policy sw_env_value_re sw_env_value_help sw_env_alias sw_env_warn; do
  mut_body="$(extract_fn_body "${MUT_DIR}/widened.sh" "${mut_table}")"
  if [[ -z "${mut_body}" ]]; then
    fail_assertion "副本 ① 里抽不出 ${mut_table} 的函数体——抽取器失配了"
  elif ! printf '%s\n' "${mut_body}" | grep -q 'DSH_TESTKEY'; then
    fail_assertion "副本 ① 的 ${mut_table} 里没有 DSH_TESTKEY，改名漏了这张表"
  fi
done

SCRIPT_UNDER_TEST="${MUT_DIR}/widened.sh"
run_case "the local R7 guard stops a forbidden prefix even if the whitelist is widened" \
  --key DSH_TESTKEY --value true
assert_rejected_before_remote
assert_contains "${RESULT}" "命中红线 R7 的禁止前缀"
assert_contains "${RESULT}" "dsh 会拒绝启动，而且**没有开关**"
assert_contains "${RESULT}" "命中它说明白名单被人扩过了"

# 副本 ②：在副本 ① 的基础上再把**本机**那道 R7 校验拆掉，只剩远端那道。
# shellcheck disable=SC2016  # 要匹配的就是被扫脚本里的字面文本 ${KEY}，不能展开
sed -e 's|^sw_env_check_forbidden_prefix "${KEY}"$|: # 本机 R7 校验已在测试副本里拆除|' \
  "${MUT_DIR}/widened.sh" >"${MUT_DIR}/widened_no_local_r7.sh"
case_name="the remote-only mutant is actually rewritten"
# shellcheck disable=SC2016  # 同上：字面文本
if grep -q '^sw_env_check_forbidden_prefix "${KEY}"$' "${MUT_DIR}/widened_no_local_r7.sh"; then
  fail_assertion "副本 ② 没拆掉本机 R7 校验——下面那条用例会退化成假绿"
fi

SCRIPT_UNDER_TEST="${MUT_DIR}/widened_no_local_r7.sh"
run_case "the remote R7 guard stands on its own" --key DSH_TESTKEY --value true
assert_status 1
assert_contains "${RESULT}" "命中红线 R7 的禁止前缀（DSH_ / XDG_ / DYLD_ / BASH_FUNC_）：dsh 会拒绝启动且无开关"
assert_contains "${RESULT}" "远端再校验拒绝了这次变更，什么都没做"
# 远端拦下时 .env 一个字节都没动，也没重建容器。
assert_contains "${ENV_AFTER}" "SW_USE_FAKE_PUBLISHERS=true"
assert_log_count "container-recreated" 0
assert_not_contains "${LOG}" "restart-sh <"
SCRIPT_UNDER_TEST="${SCRIPT}"

# ================================================ stdin 的结构性保证（与另外四份同款）
# 【本文件里这道保证的定位变了，先读这一段】上一版这里只放正例，并如实写着"本脚本的远端
# 正文里没有任何会消费 stdin 的边界命令，所以构造不出被吞掉的后果"。**那句话现在不成立
# 了**：事前预防闸门带进来一条 `printf ... | docker compose exec -T core python3 -c ...`，
# 而 `-T` 只关 TTY、**仍然转发 stdin**。于是这道保证在本文件里从"照抄同款"变成了真正承重
# 的东西，反例也终于构造得出来——补上。
#   正：把逐条命令上的 `</dev/null` 标注**全部删掉**，行为必须逐字不变，证明它们确实已经
#       降级成纵深防御（承重的是那两层组重定向）。
#   反：**要构造出这个后果，光删结构还不够**——本轮实测过两件事，都如实写下来：
#       ① 闸门里那条 exec 是 `printf ... |` 管道喂的，而管道本身就是显式 stdin 来源；
#          删掉花括号它照样吞不到脚本正文。所以反例先把那条管道拆掉，模拟"有人加了一条
#          不带显式 stdin 的 docker exec"——那才是这个缺陷真正的入口形态。
#       ② 即便管道也拆了、结构也删了，在本文件上**整段被吞的后果并不出现**：闸门坐在内层
#          脚本很靠前的位置，bash 早已把这份（不大的）heredoc 缓冲进内存，子进程从文件偏移
#          处读走剩余字节之后，bash 仍从自己的缓冲里把后面的正文跑完。这与 update.sh /
#          verify.sh 的反例不同（那两处的吞点靠后、正文更长），所以**这里不假装能复现同一个
#          后果**。
#       于是这里钉的是**机制**而不是下游后果：那条 exec 的 fd 0 到底接的是什么。
#         正      标注全删、结构保留 → 行为逐字不变（标注确实只是纵深防御）；
#         反·其一 管道拆掉、结构保留 → 假 docker 记到 `exec-stdin <empty>`：组重定向把 fd 0
#                 兜成了 /dev/null，这正是它承重的直接证据；
#         反·其二 管道拆掉 + 结构删掉 → 假 docker 记到 `exec-stdin <not-json>`：它读到的是
#                 **这层 bash 自己的脚本正文**。历史缺陷的机制原样重现；这一次侥幸没造成
#                 可见后果，换一个吞点靠后的脚本就是 update.sh / verify.sh 那两条反例。
#       两条反例合起来证明"承重的是那层组重定向"，而不是别的什么东西恰好挡住了。
STRUCT_DIR="${TMP}/struct"
mkdir -p "${STRUCT_DIR}"
cp "${ROOT}/scripts/ops/ui_token.sh" "${STRUCT_DIR}/ui_token.sh"
cp "${ROOT}/scripts/ops/restart.sh" "${STRUCT_DIR}/restart.sh"
sed -e '/^} <\/dev\/null$/!s| </dev/null||g' "${SCRIPT}" >"${STRUCT_DIR}/annotations_stripped.sh"
# 变异器：把闸门那条 exec 的前置管道拆掉，让它变成一条没有显式 stdin 来源的边界命令。
# shellcheck disable=SC2016  # 匹配的是被改写脚本里的字面文本，不能展开
UNPIPE_SED='s@^  printf .%s. "${sw_probe_body}" | docker compose exec @  docker compose exec @'
sed -e "${UNPIPE_SED}" "${SCRIPT}" >"${STRUCT_DIR}/unpiped.sh"
sed -e "${UNPIPE_SED}" -e '/^} <\/dev\/null$/d' -e '/^{$/d' -e 's| </dev/null||g' \
  "${SCRIPT}" >"${STRUCT_DIR}/structure_removed.sh"

case_name="structural stdin variants are actually rewritten"
struct_orig_lines="$(grep -c "" "${SCRIPT}")"
struct_stripped_lines="$(grep -c "" "${STRUCT_DIR}/annotations_stripped.sh")"
struct_removed_lines="$(grep -c "" "${STRUCT_DIR}/structure_removed.sh")"
struct_annotations_left="$(grep -c -- ' </dev/null' "${STRUCT_DIR}/annotations_stripped.sh" || true)"
if [[ "${struct_orig_lines}" -ne "${struct_stripped_lines}" ]]; then
  fail_assertion "annotations_stripped 不应改变行数：${struct_orig_lines} -> ${struct_stripped_lines}"
fi
# 两个远端脚本各有两层组重定向（外层包装 + 内层正文），共四处 `} </dev/null` 必须留下。
if [[ "${struct_annotations_left}" -ne 4 ]]; then
  fail_assertion "annotations_stripped 里应只剩 4 处 </dev/null（两个远端脚本各两层组重定向），实际 ${struct_annotations_left} 处"
fi
# 改写器自检：sed 一旦失配（比如以后有人给花括号加了缩进），下面那两条反例会静默失去意义。
if [[ $((struct_orig_lines - struct_removed_lines)) -ne 8 ]]; then
  fail_assertion "structure_removed 应恰好删掉 8 行（两个远端脚本各 2 个 { 与 2 个 } </dev/null），实际删掉 $((struct_orig_lines - struct_removed_lines)) 行"
fi
# 拆管道那一步同样要自检：失配的话"反例"其实还带着管道，两条反例都会退化成假绿。
for unpipe_variant in unpiped structure_removed; do
  if ! grep -q '^  docker compose exec -T core python3 -c ' "${STRUCT_DIR}/${unpipe_variant}.sh"; then
    fail_assertion "${unpipe_variant}.sh 里没有拆掉管道的那条 docker exec——拆管道的 sed 多半失配了"
  fi
  # shellcheck disable=SC2016  # 同上：字面文本
  if grep -q '"${sw_probe_body}" | docker compose exec' "${STRUCT_DIR}/${unpipe_variant}.sh"; then
    fail_assertion "${unpipe_variant}.sh 里那条管道还在，拆管道的 sed 没生效"
  fi
done

SCRIPT_UNDER_TEST="${STRUCT_DIR}/annotations_stripped.sh"
run_case "stripping every per-command </dev/null changes nothing" --key SW_USE_FAKE_PUBLISHERS --value false
assert_status 0
assert_contains "${RESULT}" "事前闸门  人工确认闸门通道 enabled=true ready=true polling=true，允许写入"
assert_contains "${RESULT}" ".env 写入  SW_USE_FAKE_PUBLISHERS 已就地替换"
assert_contains "${RESULT}" "容器已按新 .env 重建"
assert_contains "${RESULT}" "✓ 生产 .env 已变更、已生效"

# 反·其一：管道拆掉、结构保留。组重定向把那条 exec 的 fd 0 兜成 /dev/null，于是它读到
# 空输入、python 判 JSON 解析失败退 22，闸门如实说"没探到"并拒绝——**关键在于脚本正文
# 没有被吞**：闸门真的判了，外层也真的收到了 38 这个码。
SCRIPT_UNDER_TEST="${STRUCT_DIR}/unpiped.sh"
run_case "an un-piped exec reads /dev/null while the brace group is there" --key SW_USE_FAKE_PUBLISHERS --value false
# 直接证据：那条 exec 的 fd 0 是 /dev/null，所以它一个字节都没读到。
assert_log_count "exec-stdin <empty>" 1
assert_log_count "exec-stdin <not-json>" 0
# 脚本正文没有被吞：闸门真的判了，而且如实说"没探到"（空输入解析不出 JSON）。
assert_status 38
assert_contains "${RESULT}" "事前预防闸门**探不到**人工确认闸门通道"
assert_contains "${ENV_AFTER}" "SW_USE_FAKE_PUBLISHERS=true"
assert_log_count "container-recreated" 0

# 反·其二：管道拆掉 + 结构删掉 = 回到改造前。那条 exec 的 fd 0 变回"这层 bash 自己的脚本
# 正文"，历史缺陷的机制原样重现。
# 【只钉机制，不假装能钉后果——理由写在本节开头】本文件的吞点靠前、内层 heredoc 也不大，
# bash 早已把它缓冲进内存，所以子进程读走剩余字节之后脚本仍然跑完了。这一次没造成可见
# 后果，纯属吞点位置的侥幸；同样的机制放到吞点靠后的 update.sh / verify.sh 上就是那两条
# 反例里"结论段整段消失、脚本却报通过"。所以这里断言的是**它读到了脚本正文**这件事本身。
SCRIPT_UNDER_TEST="${STRUCT_DIR}/structure_removed.sh"
run_case "removing the brace group makes that exec read the script itself" --key SW_USE_FAKE_PUBLISHERS --value false
assert_contains "${LOG}" "docker <compose> <exec> <-T> <core> <python3> <-c>"
assert_log_count "exec-stdin <not-json>" 1
assert_log_count "exec-stdin <empty>" 0
SCRIPT_UNDER_TEST="${SCRIPT}"

# =============================================== 源码级：白名单写死在脚本里，键数被钉住
# 行为测试证不了"没有第三条路"。这组源码级断言直接**数分支**（本轮扩容后思路照旧，只是
# 从"数一张表"变成"数五张表并要求它们逐键一致"）：多一格、少一格、拼错一个字都判红，
# 逼着改动的人回来读白名单那段理由，而不是顺手加一个键。
#
# 【本轮为什么要五张表一起数】docs/RISKS.md 第 14 条的原话是"补一个键不是往数组里加个
# 元素"。落到代码上，一个键要在 sw_env_policy / sw_env_value_re / sw_env_value_help /
# sw_env_alias / sw_env_warn 五处各有一格。少补一格的后果是：脚本在 `set -e` 下当场退出，
# 而退出点离真正的原因很远（比如取正则那一步），排查起来很不友好。这里让它在测试里就红，
# 并且直接说出是哪张表少了哪个键。
case_name="scripts/ops/env_set.sh pins the whitelist to exactly twelve keys"
WHITELIST_LINE="$(sed -n 's/^SW_ENV_WHITELIST="\(.*\)"$/\1/p' "${SCRIPT}")"
if [[ -z "${WHITELIST_LINE}" ]]; then
  fail_assertion "抽不出 SW_ENV_WHITELIST 那一行——抽取器失配了，下面整节会退化成空转"
fi
# shellcheck disable=SC2086
# 这里**故意**词拆分：SW_ENV_WHITELIST 就是一张空格分隔的大写标识符表。
whitelist_sorted="$(printf '%s\n' ${WHITELIST_LINE} | sort)"
whitelist_count="$(printf '%s\n' "${whitelist_sorted}" | grep -c "" || true)"
if [[ "${whitelist_count}" -ne 12 ]]; then
  fail_assertion "SW_ENV_WHITELIST 里有 ${whitelist_count} 个键，本轮定死 12 个——真要加第十三个，请先按第 14 条那四问补齐五张表，并回来同步这条断言"
fi
# 【这份禁入名单本轮又少了一个，理由要写清，别当成"放松了"】TELEGRAM_BOT_TOKEN 从这里移到了
# 白名单上，因为第 14 条那四问已经逐条答过：取值形状（`<数字 bot_id>:<授权串>`，见
# sw_env_value_re 里那段"钉多紧"的论证）、display 策略（secret，值永不回显）、生效后怎么核验
# （verify.sh 的确认通道那几格与 error_code=409 那一格）、**这个键特有的闸门**——而最后这一问
# 正是本轮的实质工作：它是三级回落的第三级，闸门必须**同时**补到第三级，否则加键这件事本身
# 就是在造一个活的缺口。
# 剩下这几个一个都没答过，所以一个都不许进：
#   TELEGRAM_CHAT_ID    它根本不是凭据，但改了会把 R1 确认卡导到另一个会话；而且它的值是
#                       **从生产流出来**的（要在服务器上跑 core.telegram setup 才知道），
#                       现有的表模型不了那个方向，"新会话真的收得到卡吗"这道闸门也验不了；
#   三个 API key        它们只被 llm_backend_creds 当作"在不在"来读，没有自己的闸门。
for forbidden_key in TELEGRAM_CHAT_ID ANTHROPIC_API_KEY DEEPSEEK_API_KEY SW_DSH_GATEWAY_API_KEY; do
  if printf '%s\n' "${whitelist_sorted}" | grep -qx "${forbidden_key}"; then
    fail_assertion "${forbidden_key} 进了白名单——这几个键的闸门还没想清楚（docs/RISKS.md 第 14 条如实记着这个缺口）"
  fi
done
# 反过来钉住：这两个凭据类键**必须**在白名单里，否则相关的整批用例会静默退化成"键不在名单上"。
for required_key in SW_TELEGRAM_SIGNING_SECRET TELEGRAM_BOT_TOKEN; do
  if ! printf '%s\n' "${whitelist_sorted}" | grep -qx "${required_key}"; then
    fail_assertion "${required_key} 不在白名单里——它那一批闸门用例会全部退化成无意义的拒绝"
  fi
done

for table in sw_env_policy sw_env_value_re sw_env_value_help sw_env_alias sw_env_warn; do
  case_name="${table} covers exactly the whitelist"
  table_body="$(extract_fn_body "${SCRIPT}" "${table}")"
  table_lines="$(printf '%s\n' "${table_body}" | grep -c "" || true)"
  # 抽取器自检：抽空了 / 抽短了一律判红，绝不让"空集 == 空集"变成绿。
  if [[ "${table_lines}" -lt 8 ]]; then
    fail_assertion "抽到的 ${table} 只有 ${table_lines} 行，抽取器多半失配了"
    continue
  fi
  # 兜底分支两种写法都算（`    *)` 单起一行，或 `    *) return 1 ;;` 写在一行里）。
  if ! printf '%s\n' "${table_body}" | grep -q '^    \*)'; then
    fail_assertion "${table} 没有 \`*)\` 兜底分支——漏登记的键必须返回 1 当场炸，不许静默走默认路径"
  fi
  table_keys="$(printf '%s\n' "${table_body}" | extract_case_keys | sort)"
  table_count="$(printf '%s\n' "${table_keys}" | grep -c "" || true)"
  if [[ "${table_count}" -lt 2 ]]; then
    fail_assertion "从 ${table} 里只解析出 ${table_count} 个键名，标签抽取器多半失配了"
    continue
  fi
  missing="$(comm -23 <(printf '%s\n' "${whitelist_sorted}") <(printf '%s\n' "${table_keys}") | tr '\n' ' ')"
  extra="$(comm -13 <(printf '%s\n' "${whitelist_sorted}") <(printf '%s\n' "${table_keys}") | tr '\n' ' ')"
  [[ -z "${missing// /}" ]] || fail_assertion "${table} 少了这些白名单键：${missing}"
  [[ -z "${extra// /}" ]] || fail_assertion "${table} 多出了白名单以外的键：${extra}"
done

# ---- sw_env_policy 的闸门名集合也钉住：新增一道闸门必须回来改这里 ----------------
case_name="sw_env_policy only hands out gates the remote knows"
policy_body="$(extract_fn_body "${SCRIPT}" sw_env_policy)"
policy_gates="$(printf '%s\n' "${policy_body}" | sed -n 's/.*POLICY_GATE="\([a-z_]*\)".*/\1/p' | sort -u)"
if [[ -z "${policy_gates}" ]]; then
  fail_assertion "从 sw_env_policy 里抽不出任何 POLICY_GATE 值——抽取器失配了"
else
  for gate in ${policy_gates}; do
    # 远端那个 case 必须有同名分支，否则这次变更会撞上"无法识别的闸门名"那条纵深防御。
    grep -q "^${gate})$" "${SCRIPT}" \
      || fail_assertion "本地派发了闸门 ${gate}，但远端 case 里没有同名分支"
  done
  for gate in real_publish confirm_carrier llm_backend_creds wechat_certified wechat_claim signing_secret; do
    printf '%s\n' "${policy_gates}" | grep -qx "${gate}" \
      || fail_assertion "闸门 ${gate} 没有任何键在用——要么是漏配了，要么该把它删掉"
  done
fi

# ---- dsh 的 provider → apiKeyEnv 映射不许与 configs/dsh/cordis.yml 漂移 -----------
# 远端没有 YAML 解析器，所以那张映射被照抄进了 env_set.sh 的 bash。照抄就会漂移，
# 所以这里直接解析 cordis.yml 与它对账。**R4：只读那个文件，一个字节都不改。**
case_name="the dsh provider→apiKeyEnv map matches configs/dsh/cordis.yml"
CORDIS="${ROOT}/configs/dsh/cordis.yml"
if [[ ! -f "${CORDIS}" ]]; then
  fail_assertion "找不到 ${CORDIS}——这条对账断言无法执行，不许当作通过"
else
  cordis_pairs="$(awk '
    /^    providers:$/ { inp = 1; next }
    inp && /^      [a-z][a-z0-9-]*:$/ { name = $1; sub(/:$/, "", name); next }
    inp && /^        apiKeyEnv:[[:space:]]*[A-Z]/ { print name, $2; next }
    inp && /^    [a-zA-Z]/ { inp = 0 }
  ' "${CORDIS}")"
  cordis_count="$(printf '%s\n' "${cordis_pairs}" | grep -c "" || true)"
  if [[ -z "${cordis_pairs}" || "${cordis_count}" -lt 4 ]]; then
    fail_assertion "从 cordis.yml 只解析出 ${cordis_count} 条 provider→apiKeyEnv，解析器多半失配了（当前应有 4 条）"
  else
    while read -r cordis_provider cordis_env; do
      [[ -n "${cordis_provider}" ]] || continue
      grep -q "^        ${cordis_provider})[[:space:]]*need_env=\"${cordis_env}\"" "${SCRIPT}" \
        || grep -q "^        ${cordis_provider}|" "${SCRIPT}" \
        || grep -q "|${cordis_provider})[[:space:]]*need_env=\"${cordis_env}\"" "${SCRIPT}" \
        || fail_assertion "cordis.yml 里 provider ${cordis_provider} 的 apiKeyEnv 是 ${cordis_env}，env_set.sh 的映射里对不上"
    done <<<"${cordis_pairs}"
  fi
fi

# ========================= sw_awaiting_confirm 也是单一真相源（与 sw_probe 同款三条）
#
# 【为什么要给它补同款的扫描】"收敛完又悄悄长出第二份"这类回归本仓吃过亏：sw_probe 曾经有
# 四份逐字相同的内联拷贝。这个新片段一出生就有**两个**使用方（本脚本的签名密钥闸门要拿它
# 做判定，scripts/ops/verify.sh 要拿它渲染取证行），而两份实现一旦分叉，后果比 sw_probe 那次
# 更难查：取证与闸门会对同一台生产给出两个不同的答案（"verify 说 0 条、env_set 说有卡"），
# 而两边都"看起来对"。所以三条不变量照抄 tests/ops/test_update.sh 里 sw_probe 那一节：
#   ① scripts/ops/ 下只有一处定义，且就在 ui_token.sh 里；没有任何脚本自己内联；
#   ② 每个使用方都**调用**那个发射函数，恰好一次；
#   ③ 发射进去的字节必须落在远端正文那对花括号的**里面**，且排在 sw_probe 定义之后
#      （它调用 sw_probe）、第一次使用之前。③ 用真实捕获的那条 ssh stdin 流验。
case_name="sw_awaiting_confirm has exactly one definition and it lives in ui_token.sh"
awaiting_owners="$(grep -l "^sw_awaiting_confirm() {$" "${ROOT}"/scripts/ops/*.sh || true)"
awaiting_owner_count="$(printf '%s\n' "${awaiting_owners}" | grep -c . || true)"
if [[ "${awaiting_owner_count}" -ne 1 || "${awaiting_owners}" != "${ROOT}/scripts/ops/ui_token.sh" ]]; then
  fail_assertion "sw_awaiting_confirm 的定义必须只有一处、且在 scripts/ops/ui_token.sh 里；实测持有者：${awaiting_owners:-<无>}（${awaiting_owner_count} 个）"
fi
# 抽取器自检：定义真的在那儿、且带着它那条 docker exec，否则下面几条会退化成"空==空"。
awaiting_def="$(awk '/^sw_awaiting_confirm\(\) \{$/ {on = 1} on {print; if ($0 == "}") exit}' \
  "${ROOT}/scripts/ops/ui_token.sh")"
awaiting_def_lines="$(printf '%s\n' "${awaiting_def}" | grep -c "" || true)"
if [[ "${awaiting_def_lines}" -lt 20 ]]; then
  fail_assertion "从 ui_token.sh 抽到的 sw_awaiting_confirm 只有 ${awaiting_def_lines} 行，抽取器多半失配了"
fi
if [[ "${awaiting_def}" != *'docker compose exec -T core python3 -c'* ]]; then
  fail_assertion "抽到的 sw_awaiting_confirm 里没有那条容器内解析，抽取器多半失配了"
fi

case_name="every user of sw_awaiting_confirm takes it from that one emitter"
# 覆盖度自检放最前面：新增使用方时这里必须先红一次，逼着改动的人回来把它加进列表，
# 而不是悄悄多出一个没被任何断言看着的调用方。
awaiting_callers="$(grep -l '^  sw_ops_emit_awaiting_confirm_definition$' "${ROOT}"/scripts/ops/*.sh \
  | while IFS= read -r f; do basename "${f}"; done | LC_ALL=C sort | tr '\n' ' ')"
AWAITING_EXPECTED_CALLERS="env_set.sh verify.sh "
if [[ "${awaiting_callers}" != "${AWAITING_EXPECTED_CALLERS}" ]]; then
  fail_assertion "调用 sw_ops_emit_awaiting_confirm_definition 的脚本是 [${awaiting_callers}]，期望 [${AWAITING_EXPECTED_CALLERS}]——新增/删减使用方请同步改这条断言"
fi
for awaiting_script in ${AWAITING_EXPECTED_CALLERS}; do
  awaiting_src="${ROOT}/scripts/ops/${awaiting_script}"
  awaiting_emit_count="$(grep -c '^  sw_ops_emit_awaiting_confirm_definition$' "${awaiting_src}" || true)"
  if [[ "${awaiting_emit_count}" -ne 1 ]]; then
    fail_assertion "${awaiting_script} 里 sw_ops_emit_awaiting_confirm_definition 的调用有 ${awaiting_emit_count} 处，应当恰好 1 处"
  fi
  # 内联一份"顺手改一改"的拷贝，正是这条扫描要消灭的东西。招牌行一个都不许出现在脚本自己身上。
  if grep -q "^sw_awaiting_confirm() {$" "${awaiting_src}" \
    || grep -q "^sw_awaiting_count=''$" "${awaiting_src}"; then
    fail_assertion "${awaiting_script} 里又出现了内联的 sw_awaiting_confirm 定义——定义只许有一处（scripts/ops/ui_token.sh）"
  fi
  # 两个使用方都必须**用**它，否则发射进去的定义就是死代码，而下一个人会顺手内联一份。
  if ! grep -q 'sw_awaiting_confirm ' "${awaiting_src}"; then
    fail_assertion "${awaiting_script} 发射了 sw_awaiting_confirm 却一次都不调用它"
  fi
done

# ---- 发射进去的两个片段必须落在花括号里面、且顺序正确（看真实的那条流）------------------
# 放错地方的后果是实打实的：落在外层 `{` 之前，`{ ... } </dev/null` 那层结构性保证盖不到它；
# 落在外层花括号里、内层 heredoc 外，内层 `bash -s` 是另一个进程，拿不到外层定义的函数，
# 远端会以 "sw_awaiting_confirm: command not found" 收场。两种错法都不是源码文本能一眼
# 看出来的，所以直接把 ssh 真正送出去的那串字节捕获下来数花括号深度。
cat >"${TMP}/emit_depth.awk" <<'AWKEOF'
$0 == "{" { depth++ }
$0 == "} </dev/null" { depth-- }
!probe_seen && $0 == "sw_probe_code=''" { probe_depth = depth; probe_seen = 1 }
!awaiting_seen && $0 == "sw_awaiting_count=''" { awaiting_depth = depth; awaiting_seen = 1 }
END {
  printf "%s %s\n", (probe_seen ? probe_depth : "none"), (awaiting_seen ? awaiting_depth : "none")
}
AWKEOF
: >"${TMP}/remote_stream"
run_case "the remote stream carries both emitted snippets inside the brace group" \
  SW_TEST_DUMP_REMOTE="${TMP}/remote_stream" --key SW_UI_TOKEN --generate
assert_status 0
STREAM="$(<"${TMP}/remote_stream")"
case_name="the remote stream carries both emitted snippets inside the brace group"
# 捕获自检：流是空的 / 短得离谱时下面每一条都会退化成无意义的通过。
stream_lines="$(printf '%s\n' "${STREAM}" | grep -c "" || true)"
if [[ "${stream_lines}" -lt 100 ]]; then
  fail_assertion "捕获到的远端脚本流只有 ${stream_lines} 行，捕获多半没生效"
fi
# 前言在花括号**外面**：它是流的第一行，且必须仍然只是 export（ui_token.sh 顶部那段
# "只许放不读 stdin 的内建命令"的警告说的就是这一行）。
assert_contains "$(printf '%s\n' "${STREAM}" | head -n 1)" "export SW_ENV_SET_VALUE="
assert_contains "${STREAM}" "sw_probe_code=''"
assert_contains "${STREAM}" "sw_awaiting_count=''"
read -r probe_depth awaiting_depth <<<"$(awk -f "${TMP}/emit_depth.awk" "${TMP}/remote_stream")"
# 深度必须是 2：env_set.sh 的远端也是两层——外层状态规范化包装一层 `{`，内层变更脚本再一层。
[[ "${probe_depth}" == "2" ]] \
  || fail_assertion "sw_probe 定义出现在花括号深度 ${probe_depth} 处，应当是 2（外层包装 + 内层变更脚本）"
[[ "${awaiting_depth}" == "2" ]] \
  || fail_assertion "sw_awaiting_confirm 定义出现在花括号深度 ${awaiting_depth} 处，应当是 2（外层包装 + 内层变更脚本）"
# 顺序：sw_probe 的定义必须在 sw_awaiting_confirm 的定义之前（后者调用前者），
# 而两者都必须在第一次调用之前。`|| true` 是为了让缺失变成断言失败，而不是让 set -e
# 把整个测试文件在半路打死——那会让下面的用例连跑都没跑。
probe_def_line="$(grep -n "^sw_probe_code=''$" "${TMP}/remote_stream" | head -n 1 | cut -d: -f1 || true)"
awaiting_def_line="$(grep -n "^sw_awaiting_count=''$" "${TMP}/remote_stream" | head -n 1 | cut -d: -f1 || true)"
awaiting_call_line="$(grep -n "^    if sw_awaiting_confirm 'http" "${TMP}/remote_stream" | head -n 1 | cut -d: -f1 || true)"
if [[ -z "${probe_def_line}" || -z "${awaiting_def_line}" || "${probe_def_line}" -ge "${awaiting_def_line}" ]]; then
  fail_assertion "sw_probe 的定义在第 ${probe_def_line:-<无>} 行、sw_awaiting_confirm 在第 ${awaiting_def_line:-<无>} 行，前者必须在前（后者调用它）"
fi
if [[ -z "${awaiting_call_line}" || "${awaiting_def_line}" -ge "${awaiting_call_line}" ]]; then
  fail_assertion "sw_awaiting_confirm 的定义在第 ${awaiting_def_line:-<无>} 行、第一次调用在第 ${awaiting_call_line:-<无>} 行，定义必须在前"
fi

# env_set.sh 是 sw_probe 的**第五个调用方**，而它一份拷贝都不持有——这正是本轮重构的意义。
# 从前不做事前预防的唯一理由就是"要在这里内联第五份 sw_probe"；sw_probe 收成单一真相源
# （scripts/ops/ui_token.sh 的 sw_ops_emit_sw_probe_definition）之后，那个理由不成立了。
# 这里从两侧钉住它：内联一份都不许有，发射函数必须恰好调一次。
case_name="env_set.sh takes sw_probe from the single emitter and inlines none"
if grep -q "^sw_probe_code=''$" "${SCRIPT}" || grep -q '^sw_probe() {$' "${SCRIPT}"; then
  fail_assertion "env_set.sh 里出现了内联的 sw_probe：定义只许有一处（scripts/ops/ui_token.sh），这里应当只调用发射函数"
fi
emit_hits="$(grep -c '^  sw_ops_emit_sw_probe_definition$' "${SCRIPT}" || true)"
if [[ "${emit_hits}" -ne 1 ]]; then
  fail_assertion "env_set.sh 里 sw_ops_emit_sw_probe_definition 的调用有 ${emit_hits} 处，应当恰好 1 处（事前预防闸门要用它）"
fi


# ============ 源码级：本轮那两格新性质，各自有一条"它不许塌成常量"的断言
#
# 【为什么这两条非有不可】两格都是**为了消灭一处硬编码键名**才长出来的：
#   POLICY_CRED_ORIGIN   决定"能不能 --generate"；
#   POLICY_SIGNING_ABOVE 决定"这次写入会不会换掉生效的签名密钥"。
# 它们的共同失效模式不是写错，是**塌成常量**——比如所有键都填 local-csprng，于是
# `--generate` 的拒绝分支变成永不执行的死代码，而所有行为用例照样全绿。所以这里除了
# "每个键都有一格"，还要钉"两种取值都真的存在"。
case_name="sw_env_policy fills the two new per-key grades for every branch"
policy_body="$(extract_fn_body "${SCRIPT}" sw_env_policy)"
policy_credkey_count="$(printf '%s\n' "${policy_body}" | grep -c 'POLICY_CRED_KEY=' || true)"
policy_origin_count="$(printf '%s\n' "${policy_body}" | grep -c 'POLICY_CRED_ORIGIN=' || true)"
policy_above_count="$(printf '%s\n' "${policy_body}" | grep -c 'POLICY_SIGNING_ABOVE=' || true)"
# 抽取器自检：分支数远少于 10 就说明抽错了，下面几条会退化成"0 == 0"。
if [[ "${policy_credkey_count}" -lt 10 ]]; then
  fail_assertion "从 sw_env_policy 里只抽到 ${policy_credkey_count} 处 POLICY_CRED_KEY 赋值，抽取器多半失配了"
fi
if [[ "${policy_origin_count}" -ne "${policy_credkey_count}" ]]; then
  fail_assertion "POLICY_CRED_ORIGIN 有 ${policy_origin_count} 处、POLICY_CRED_KEY 有 ${policy_credkey_count} 处——有分支漏填了这一格，脚本会在 set -u 下带着上一个键的值往下跑"
fi
if [[ "${policy_above_count}" -ne "${policy_credkey_count}" ]]; then
  fail_assertion "POLICY_SIGNING_ABOVE 有 ${policy_above_count} 处、POLICY_CRED_KEY 有 ${policy_credkey_count} 处——有分支漏填了这一格"
fi

case_name="both credential origins really exist（否则 --generate 的拒绝分支是死代码）"
policy_origins="$(printf '%s\n' "${policy_body}" | sed -n 's/.*POLICY_CRED_ORIGIN="\([a-z-]*\)".*/\1/p' | sort -u | tr '\n' ' ')"
if [[ "${policy_origins}" != "- external-issuer local-csprng " ]]; then
  fail_assertion "POLICY_CRED_ORIGIN 的取值集合是 [${policy_origins}]，期望 [- external-issuer local-csprng ]——多一种就是没登记、少一种就是那条分支成了死代码"
fi

# ---- 回落链必须与 core/telegram.py 逐级对齐（R4：只读那个文件，一个字节都不改）--------
# 【这条是本轮最要紧的一条源码级断言】闸门的全部正确性都压在"谁排在谁前面"上，而那份顺序的
# 唯一真相在 core/telegram.py 的 load_config 里。两边一旦漂移，闸门会**静默地**在错误的一级
# 上免检——行为测试看不出来，因为它测的正是那份（已经错了的）表。
case_name="the signing fallback chain in env_set.sh matches core/telegram.py"
CORE_TG="${ROOT}/core/telegram.py"
if [[ ! -f "${CORE_TG}" ]]; then
  fail_assertion "找不到 ${CORE_TG}——这条对账断言无法执行，不许当作通过"
else
  chain_fields="$(awk '/^    secret = \($/ {on = 1; next} on && /^    \)$/ {exit} on {print}' "${CORE_TG}" \
    | sed -n 's/.*settings\.\([a-z_]*\).*/\1/p')"
  chain_count="$(printf '%s\n' "${chain_fields}" | grep -c . || true)"
  if [[ "${chain_count}" -ne 3 ]]; then
    fail_assertion "从 core/telegram.py 的 load_config 里解析出 ${chain_count} 级回落，期望 3 级——解析器失配，或者 core 那边真的改了级数（那时闸门必须跟着改）"
  else
    chain_expected="none"
    chain_level=0
    for chain_field in ${chain_fields}; do
      chain_level=$((chain_level + 1))
      chain_key="$(printf '%s' "${chain_field}" | tr '[:lower:]' '[:upper:]')"
      if ! printf '%s\n' "${whitelist_sorted}" | grep -qx "${chain_key}"; then
        fail_assertion "回落链第 ${chain_level} 级 ${chain_key} 不在白名单里——那一级就没有合规的改法，闸门也管不着它"
      fi
      chain_actual="$(printf '%s\n' "${policy_body}" | awk -v want="${chain_key}" '
        $0 ~ "^    " want "\\)$" { on = 1; next }
        on && /^    [A-Z]/ { on = 0 }
        on && /POLICY_SIGNING_ABOVE=/ {
          line = $0
          sub(/.*POLICY_SIGNING_ABOVE="/, "", line)
          sub(/".*/, "", line)
          print line
          exit
        }')"
      if [[ -z "${chain_actual}" ]]; then
        fail_assertion "sw_env_policy 里抽不出 ${chain_key} 的 POLICY_SIGNING_ABOVE——抽取器失配，或者那个键根本没这一格"
      elif [[ "${chain_actual}" != "${chain_expected}" ]]; then
        fail_assertion "${chain_key} 在 core 里是第 ${chain_level} 级，POLICY_SIGNING_ABOVE 应当是 [${chain_expected}]，实际是 [${chain_actual}]"
      fi
      # 链上的每一级都必须挂着 signing_secret 闸门，否则它那条改法会绕过整道闸门。
      chain_gate="$(printf '%s\n' "${policy_body}" | awk -v want="${chain_key}" '
        $0 ~ "^    " want "\\)$" { on = 1; next }
        on && /^    [A-Z]/ { on = 0 }
        on && /POLICY_GATE=/ {
          line = $0
          sub(/.*POLICY_GATE="/, "", line)
          sub(/".*/, "", line)
          print line
          exit
        }')"
      [[ "${chain_gate}" == "signing_secret" ]] \
        || fail_assertion "回落链上的 ${chain_key} 配的闸门是 [${chain_gate}]，必须是 signing_secret"
      if [[ "${chain_expected}" == "none" ]]; then
        chain_expected="${chain_key}"
      else
        chain_expected="${chain_expected} ${chain_key}"
      fi
    done
  fi
fi

# ---- 闸门不许再按键名分支（本轮就是来拆这个的）------------------------------------
# 上一版远端闸门里写着 `[[ "${signing_decided}" -eq 0 && "${key}" == "SW_UI_TOKEN" ]]`。
# 补到第三级之后判据必须是 signing_above 那张表；把键名写回去，行为用例里"一级挡三级"
# 那两条会红，但**新键**（将来的第四级）那种漏法只有这条扫描看得见。
# 注意大小写：远端正文用的是小写 ${key}，本地那几处 ${KEY} 分支（环境变量层、收尾提示）是
# 合理的、也不在这条扫描的范围里。
case_name="the signing gate decides by the chain table, not by hardcoded key names"
# 两个 grep 模式要匹配的就是远端脚本里**字面的** `"${key}"`，不能在这里展开。
# shellcheck disable=SC2016
if grep -q '"${key}" == "SW_' "${SCRIPT}" || grep -q '"${key}" == "TELEGRAM_' "${SCRIPT}"; then
  fail_assertion "远端脚本里出现了按键名分支的判定——签名密钥那道闸门的判据必须是 signing_above 那张表"
fi
# 判空只有一处实现（多级判定就地展开一遍，是本轮最容易长出的那对双胞胎）。
case_name="the level-emptiness judgement has exactly one implementation"
level_def_count="$(grep -c '^signing_level_is_set() {$' "${SCRIPT}" || true)"
if [[ "${level_def_count}" -ne 1 ]]; then
  fail_assertion "signing_level_is_set 的定义有 ${level_def_count} 处，应当恰好 1 处"
fi
if grep -q 'signing_probe_value=' "${SCRIPT}"; then
  fail_assertion "旧那份就地展开的判空（signing_probe_value）又回来了——判空只许有一处"
fi

# ---- bot token 的形状必须是共用字符集的**真子集**（逐字符验，不靠读代码相信）----------
# 这条断言就是"不要另造一套字符集论证"的依据：形状里允许的每一个字符都已经被
# SW_OPS_UI_TOKEN_ALLOWED_RE 论证过一次（printf '%q' → ssh stdin → export → .env 那条通路）。
case_name="the bot token shape stays inside the shared credential charset"
BOT_SHAPE_RE="$(sed -n "s/^    TELEGRAM_BOT_TOKEN) printf '%s' '\(.*\)' ;;$/\1/p" "${SCRIPT}")"
CRED_CHARSET_RE="$(sed -n "s/^SW_OPS_UI_TOKEN_ALLOWED_RE='\(.*\)'$/\1/p" "${ROOT}/scripts/ops/ui_token.sh")"
if [[ -z "${BOT_SHAPE_RE}" || -z "${CRED_CHARSET_RE}" ]]; then
  fail_assertion "抽不出正则（bot 形状 [${BOT_SHAPE_RE:-<无>}]、共用字符集 [${CRED_CHARSET_RE:-<无>}]）——抽取器失配，本条会退化成空转"
else
  subset_pad="$(printf 'a%.0s' $(seq 1 29))"
  subset_hits=0
  subset_violations=""
  for ascii_code in $(seq 33 126); do
    # 内层 printf 造出 \NNN 八进制转义，外层再把它解成字符——变量出现在格式串里
    # 正是这个惯用法的本体，改成 '%s' 就造不出字符了。
    # shellcheck disable=SC2059
    subset_char="$(printf "\\$(printf '%03o' "${ascii_code}")")"
    # 授权串那一段：末位换成待测字符。形状放行它，共用字符集就必须也放行它。
    if [[ "12345678:${subset_pad}${subset_char}" =~ ${BOT_SHAPE_RE} ]]; then
      subset_hits=$((subset_hits + 1))
      [[ "${subset_char}" =~ ${CRED_CHARSET_RE} ]] \
        || subset_violations="${subset_violations}[${subset_char}]"
    fi
    # bot_id 那一段：首位换成待测字符。
    if [[ "${subset_char}1234567:${subset_pad}a" =~ ${BOT_SHAPE_RE} ]]; then
      subset_hits=$((subset_hits + 1))
      [[ "${subset_char}" =~ ${CRED_CHARSET_RE} ]] \
        || subset_violations="${subset_violations}[${subset_char}]"
    fi
  done
  # 自检：命中数太少说明候选串本身就没被形状放行过，那这一整段等于没测。
  if [[ "${subset_hits}" -lt 60 ]]; then
    fail_assertion "逐字符探测只命中 ${subset_hits} 次，候选串多半根本过不了形状校验——本条退化成空转了"
  fi
  [[ -z "${subset_violations}" ]] \
    || fail_assertion "bot token 形状放行了共用字符集之外的字符：${subset_violations}——那就得另起一套通路论证，而不是照抄"
fi

if [[ "${failures}" -ne 0 ]]; then
  printf 'env_set.sh mechanical tests failed: %s assertion(s)\n' "${failures}" >&2
  exit 1
fi
printf 'env_set.sh mechanical tests passed: %s case(s)\n' "${cases}"
