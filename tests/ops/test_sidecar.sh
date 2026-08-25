#!/bin/bash
# No network, SSH, or Docker: every boundary command is a local argv-recording fake.
#
# 【假件保真度是这份测试的地基，先读这一段】本仓已经两次被"假件语义与真命令不符"坑到：
#   ① 假 `ssh` 直接透传 argv——真实 ssh 会把 host 之后的参数用**单个空格**拼成一个字符串
#      交给远端登录 shell 重新分词，空参数就此消失。
#   ② 假 `docker compose exec` 不消费 stdin——真实 `-T` 只关 TTY、**仍然转发 stdin**，会把
#      远端脚本剩下的部分整段吞掉，而脚本仍以 0 收尾。
# 所以这里的四个假件都按真实语义写，并且**关键那几条不是"假装"而是真的做**：
#   · 假 ssh 逐字复刻 argv 拼接与重新分词；
#   · 假 `docker compose exec -T core python3 -c` 直接把收到的 python 源码与 stdin 交给本机
#     python3 真跑——被测脚本那段端口解析逻辑因此是被**真正执行**过的，不是被信任的；
#   · 假 `docker compose config` 按 `--profile` 真的做过滤，并且 **`5556:5556` 这种写法不产生
#     `host_ip` 键**（本机用真 docker compose 5.1.3 实测过的形态）——那正是最危险的形态，
#     假件要是自作主张补一个 `"host_ip":"0.0.0.0"`，端口闸门那条"必然判红"就成了空话；
#   · 假 `curl` 的应答**跟容器状态走**：只有假 docker 真的把某个 sidecar 起起来、并且它不是
#     被显式标成"起来就退"的那种，对应的 host:port 才会出现在监听表里；否则一律 000 + 退 7。
#     没有这一条，"探不到就如实报"那几条用例只能靠读代码相信。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="${ROOT}/scripts/ops/sidecar.sh"
# 被测脚本路径。默认是仓库里那一份；「stdin 结构性保证」一节会临时指向改写过的副本。
SCRIPT_UNDER_TEST="${SCRIPT}"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT
mkdir -p "${TMP}/bin" "${TMP}/home dir" "${TMP}/state"
FIXTURE="${TMP}/home dir/social_workflow"

# 【为什么这里不像其它 ops 测试那样把 HOME/social_workflow 软链到仓库根】
# `--materialize` 会在 `${HOME}/social_workflow` 底下**真的建文件**。软链到 ROOT 就等于让
# 测试往仓库工作树里写东西。所以这里造一棵独立的夹具树，只把**真实的模板文件**拷进去
# ——模板内容仍然是仓库里那一份，"生成出来的东西与模板逐字节相同"才验得准。
reset_fixture() {
  rm -rf "${FIXTURE}"
  mkdir -p "${FIXTURE}/sidecars/trendradar/config" "${FIXTURE}/sidecars/mpt"
  cp "${ROOT}/sidecars/trendradar/config.example.yaml" "${FIXTURE}/sidecars/trendradar/config.example.yaml"
  cp "${ROOT}/sidecars/trendradar/frequency_words.example.txt" "${FIXTURE}/sidecars/trendradar/frequency_words.example.txt"
  cp "${ROOT}/sidecars/mpt/config.example.toml" "${FIXTURE}/sidecars/mpt/config.example.toml"
  : >"${FIXTURE}/sidecars/trendradar/config/.gitkeep"
}
place_configs() {
  cp "${FIXTURE}/sidecars/trendradar/config.example.yaml" "${FIXTURE}/sidecars/trendradar/config/config.yaml"
  cp "${FIXTURE}/sidecars/trendradar/frequency_words.example.txt" "${FIXTURE}/sidecars/trendradar/config/frequency_words.txt"
}
reset_fixture
place_configs

cat >"${TMP}/bin/bash" <<'EOF'
#!/bin/bash
if [[ "${1:-}" == "-s" ]]; then
  # 记录远端 shell 重新分词后真正交到脚本手里的位置参数。
  # depth=1 是 ssh 送达远端外层包装的那一份（即"线上真实收到什么"）；
  # depth=2 是外层再转交给内层脚本的那一份。空串用 [] 定界，必须看得见。
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
  # 行为断言分辨，只能直接看线上真正发出的那串字节。
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

[[ "${1:-}" == "compose" ]] || exit 93
shift

# `--profile` 在真实 compose 里是**全局**选项，位置在子命令之前。这里照样解析在前面，
# 顺带钉住"被测脚本确实把 profile 传对了位置"。
profiles=","
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --profile) profiles="${profiles}${2:-},"; shift 2 ;;
    -f|--file) shift 2 ;;
    *) break ;;
  esac
done
sub="${1:-}"
[[ "$#" -eq 0 ]] || shift

# 【红线：本脚本一律不碰 core】任何会改动部署的子命令，只要参数里出现 core 就当场判死。
case "${sub}" in
  up|down|restart|stop|start|kill|rm|build|create)
    for arg in ${@+"$@"}; do
      if [[ "${arg}" == "core" ]]; then
        printf 'fake docker: sidecar.sh must never mutate core\n' >&2
        exit 95
      fi
    done
    ;;
esac
# 裸 `docker compose down` 会连 core 与网络一起拆掉。被测脚本永远不该走到这里。
if [[ "${sub}" == "down" ]]; then
  printf 'fake docker: bare compose down is forbidden\n' >&2
  exit 94
fi

mapping_for() {
  case "$1" in
    trendradar)     printf '%s' "${TEST_PORT_MAPPING:-127.0.0.1:${TEST_TRENDRADAR_PUBLISHED:-8081}}" ;;
    xhs-downloader) printf '%s' "${TEST_PORT_MAPPING:-127.0.0.1:${TEST_XHS_PUBLISHED:-5556}}" ;;
    *)              printf '%s' "${TEST_PORT_MAPPING:-127.0.0.1:9999}" ;;
  esac
}

# ---- docker compose config --format json ---------------------------------------
# 真实语义（本机 docker compose 5.1.3 实测）：
#   · 不读 stdin；
#   · 带 profile 的服务只有在 `--profile <名>` 出现时才进解析结果；
#   · **`"5556:5556"` 这种写法不产生 `host_ip` 键**——键缺席就等于 0.0.0.0。
#     假件必须原样复刻这一点：补一个 "0.0.0.0" 出来会让被测脚本走上一条真实里不存在的分支。
if [[ "${sub}" == "config" ]]; then
  [[ "${TEST_CONFIG_FAIL:-0}" -eq 0 ]] || exit 1
  fmt=""
  cfg_i=1
  cfg_args=("$@")
  while [[ "${cfg_i}" -le "$#" ]]; do
    if [[ "${cfg_args[cfg_i-1]}" == "--format" ]]; then fmt="${cfg_args[cfg_i]:-}"; fi
    cfg_i=$((cfg_i + 1))
  done
  # 本仓只用 `--format json` 这一种。换了就**大声失败**，绝不静默给出别的东西。
  if [[ "${fmt}" != "json" ]]; then
    printf 'fake docker: unsupported config format <%s>\n' "${fmt}" >&2
    exit 92
  fi
  if [[ -n "${TEST_CONFIG_JSON:-}" ]]; then
    printf '%s' "${TEST_CONFIG_JSON}"
    exit 0
  fi
  services=""
  add_service() {
    # $1 服务名  $2 host_ip（空串 = 不产生该键，等于 0.0.0.0）  $3 宿主机端口  $4 容器端口
    local entry
    if [[ -n "$2" ]]; then
      entry="$(printf '"%s":{"image":"x","ports":[{"mode":"ingress","host_ip":"%s","target":%s,"published":"%s","protocol":"tcp"}]}' "$1" "$2" "$4" "$3")"
    else
      entry="$(printf '"%s":{"image":"x","ports":[{"mode":"ingress","target":%s,"published":"%s","protocol":"tcp"}]}' "$1" "$4" "$3")"
    fi
    if [[ -z "${services}" ]]; then services="${entry}"; else services="${services},${entry}"; fi
  }
  add_service core "127.0.0.1" 8000 8000
  case "${profiles}" in
    *,sourcing,*)
      [[ "${TEST_OMIT_SERVICE:-}" == "trendradar" ]] \
        || add_service trendradar "${TEST_TRENDRADAR_HOST_IP-127.0.0.1}" "${TEST_TRENDRADAR_PUBLISHED:-8081}" "${TEST_TRENDRADAR_TARGET:-8080}" ;;
  esac
  case "${profiles}" in
    *,xhs,*)
      [[ "${TEST_OMIT_SERVICE:-}" == "xhs-downloader" ]] \
        || add_service xhs-downloader "${TEST_XHS_HOST_IP-127.0.0.1}" "${TEST_XHS_PUBLISHED:-5556}" 5556 ;;
  esac
  case "${profiles}" in
    *,video,*) add_service mpt "127.0.0.1" 8080 8080 ;;
  esac
  printf '{"name":"social_workflow","services":{%s}}' "${services}"
  exit 0
fi

# ---- docker compose exec -T core python3 -c CODE [ARG] --------------------------
# 真实 `-T` **转发 stdin**，容器内 python 自己把它读干净。这里直接把收到的源码与 stdin
# 交给本机 python3 真跑——被测脚本那段端口解析因此是被真正执行过的。
if [[ "${sub}" == "exec" ]]; then
  if [[ "${TEST_CORE_EXEC_FAIL:-0}" -ne 0 ]]; then
    cat >/dev/null
    printf 'fake docker: core container is not running\n' >&2
    exit "${TEST_CORE_EXEC_FAIL}"
  fi
  if [[ "${1:-}" != "-T" || "${2:-}" != "core" || "${3:-}" != "python3" || "${4:-}" != "-c" ]]; then
    cat >/dev/null
    printf 'fake docker: unsupported exec form\n' >&2
    exit 97
  fi
  exec_code="$5"
  shift 5
  exec python3 -c "${exec_code}" ${@+"$@"}
fi

if [[ "${sub}" == "ps" ]]; then
  printf 'NAME      IMAGE   SERVICE   STATUS\n'
  printf 'sw-core   sw      core      running\n'
  if [[ -f "${TEST_STATE_DIR}/running" ]]; then
    while IFS= read -r running_svc; do
      [[ -n "${running_svc}" ]] || continue
      printf 'sw-%-8s img     %-14s running\n' "${running_svc}" "${running_svc}"
    done <"${TEST_STATE_DIR}/running"
  fi
  exit 0
fi

if [[ "${sub}" == "up" ]]; then
  # 真实 `docker compose up -d` 不读 stdin。
  up_svc=""
  for arg in ${@+"$@"}; do
    case "${arg}" in -*) ;; *) up_svc="${arg}" ;; esac
  done
  if [[ -z "${up_svc}" ]]; then
    printf 'fake docker: bare compose up is forbidden (it would rebuild core)\n' >&2
    exit 96
  fi
  [[ "${TEST_UP_STATUS:-0}" -eq 0 ]] || exit "${TEST_UP_STATUS}"
  printf '%s\n' "${up_svc}" >>"${TEST_STATE_DIR}/running"
  # 只有"真的起来并在监听"的容器才进监听表。TEST_SIDECAR_DEAD 模拟"容器 running，但里面
  # 那个进程起来就退了"——这正是 `up -d` 返回 0 却什么都没起来的真实形态。
  if [[ "${TEST_SIDECAR_DEAD:-}" != "${up_svc}" ]]; then
    mapping_for "${up_svc}" >>"${TEST_STATE_DIR}/listening"
    printf '\n' >>"${TEST_STATE_DIR}/listening"
  fi
  printf 'Container sw-%s  Started\n' "${up_svc}"
  exit 0
fi

if [[ "${sub}" == "port" ]]; then
  port_svc="${1:-}"
  if ! grep -qxF -- "${port_svc}" "${TEST_STATE_DIR}/running" 2>/dev/null; then
    printf 'fake docker: no container for service %s\n' "${port_svc}" >&2
    exit 1
  fi
  # 容器已退出时真实 `docker compose port` 会给出空输出并以 0 收尾——被测脚本必须拒绝空值。
  [[ "${TEST_PORT_EMPTY:-0}" -eq 0 ]] || exit 0
  mapping_for "${port_svc}"
  printf '\n'
  exit 0
fi

if [[ "${sub}" == "stop" || "${sub}" == "rm" ]]; then
  for arg in ${@+"$@"}; do
    case "${arg}" in -*) continue ;; esac
    if [[ -f "${TEST_STATE_DIR}/running" ]]; then
      grep -vxF -- "${arg}" "${TEST_STATE_DIR}/running" >"${TEST_STATE_DIR}/running.new" 2>/dev/null || true
      mv "${TEST_STATE_DIR}/running.new" "${TEST_STATE_DIR}/running"
    fi
    if [[ -f "${TEST_STATE_DIR}/listening" ]]; then
      grep -vxF -- "$(mapping_for "${arg}")" "${TEST_STATE_DIR}/listening" >"${TEST_STATE_DIR}/listening.new" 2>/dev/null || true
      mv "${TEST_STATE_DIR}/listening.new" "${TEST_STATE_DIR}/listening"
    fi
  done
  exit 0
fi

if [[ "${sub}" == "logs" ]]; then
  printf 'fake sidecar log line\n'
  exit 0
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

# 忠实模拟本仓真正用到的那几个 curl 语义（curl 8.7.1 实测）：
#   -f          HTTP >= 400 时**不输出响应体**，退出码 22。
#   -w <fmt>    传输结束后把 fmt 追加到 stdout；连不上时 %{http_code} 是 000，且**照样输出**。
#   -o <file>   响应体写进该文件（本仓只用 /dev/null）。
#   --config -  从 **stdin** 读配置并读到 EOF；本仓用它注入 Authorization 头。
# 两种调用形态都要支持：
#   sw_probe        -q -fsS --max-time N -w '\n%{http_code}' --config - URL
#   sidecar_probe   -q -s -o /dev/null --max-time N -w '%{http_code}' URL
curl_url=""
curl_write_out=""
curl_output=""
curl_config_stdin=0
curl_fail_flag=0
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
    -o|--output)
      curl_i=$((curl_i + 1))
      if [[ "${curl_i}" -le "$#" ]]; then curl_output="${!curl_i}"; fi
      ;;
    --max-time|-m)
      curl_i=$((curl_i + 1))
      ;;
    -*)
      case "${curl_arg}" in *f*) curl_fail_flag=1 ;; esac
      ;;
    *)
      curl_url="${curl_arg}"
      ;;
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
  # $1 = 响应体（HTTP >= 400 且带 -f 时按真实语义丢弃）；$2 = HTTP 状态码；$3 = curl 退出码
  local body="$1" code="$2" status="$3"
  if [[ "${status}" -eq 0 ]]; then
    if [[ -n "${curl_output}" ]]; then
      printf '%s' "${body}" >"${curl_output}"
    else
      printf '%s' "${body}"
    fi
  elif [[ "${status}" -eq 22 ]]; then
    printf 'curl: (22) The requested URL returned error: %s\n' "${code}" >&2
  else
    printf 'curl: (%s) fake transport failure\n' "${status}" >&2
  fi
  if [[ -n "${curl_write_out}" ]]; then
    # 假件只实现本仓用到的这两种 -w 格式。格式一变就**大声失败**，绝不静默给出错误状态码。
    case "${curl_write_out}" in
      '\n%{http_code}') printf '\n%s' "${code}" ;;
      '%{http_code}')   printf '%s' "${code}" ;;
      *)
        printf 'curl-fake-unsupported-write-out <%s>\n' "${curl_write_out}" >>"${TEST_LOG}"
        exit 91
        ;;
    esac
  fi
  exit "${status}"
}

curl_hostport="${curl_url#http://}"
curl_hostport="${curl_hostport%%/*}"

if [[ "${curl_hostport}" == "127.0.0.1:8000" ]]; then
  # core 的 /api/v1：模拟"生产 .env 已配 SW_UI_TOKEN"之后 require_token 的真实行为。
  if [[ -n "${TEST_REQUIRE_TOKEN:-}" && "${curl_url}" == */api/v1/* ]]; then
    if [[ "${curl_auth_header}" != "Authorization: Bearer ${TEST_REQUIRE_TOKEN}" ]]; then
      curl_emit '' '401' 22
    fi
  fi
  [[ "${TEST_CURL_STATUS:-0}" -ne 0 ]] && curl_emit '' '000' "${TEST_CURL_STATUS}"
  curl_code="${TEST_INFO_HTTP_CODE:-200}"
  [[ "${curl_code}" -ge 400 && "${curl_fail_flag}" -eq 1 ]] && curl_emit '' "${curl_code}" 22
  if [[ "${TEST_INFO_JSON+x}" == x ]]; then
    curl_emit "${TEST_INFO_JSON}" "${curl_code}" 0
  fi
  curl_emit '{"ok":true,"data":{"use_fake_publishers":true}}' "${curl_code}" 0
fi

# sidecar 探针：**应答跟容器状态走**。只有假 docker 真的把它起起来、且它不是"起来就退"的
# 那种，对应的 host:port 才在监听表里。否则就是连不上——真实 curl 那时给 000 并退 7。
if grep -qxF -- "${curl_hostport}" "${TEST_STATE_DIR}/listening" 2>/dev/null; then
  curl_sidecar_code="${TEST_SIDECAR_HTTP_CODE:-200}"
  if [[ "${curl_sidecar_code}" -ge 400 && "${curl_fail_flag}" -eq 1 ]]; then
    curl_emit '' "${curl_sidecar_code}" 22
  fi
  curl_emit 'fake sidecar body' "${curl_sidecar_code}" 0
fi
curl_emit '' '000' 7
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

assert_file_contains() {
  local path="$1" needle="$2"
  [[ -f "${path}" ]] || { fail_assertion "文件不存在：${path}"; return; }
  grep -qF -- "${needle}" "${path}" \
    || fail_assertion "文件 ${path} 里没有 [${needle}]"
}

# 一个容器都没起过。端口闸门判红、配置缺失、表对不上这几条都必须满足它——
# "拦住了"与"起了再停"是两件完全不同的事。
assert_nothing_started() {
  assert_not_contains "${LOG}" "<up> <-d>"
  assert_not_contains "${LOG}" "<stop>"
  assert_not_contains "${LOG}" "<rm>"
}

# 本脚本一律不碰 core。假 docker 对"改动型子命令 + core"直接退 95，这里再从日志侧钉一遍。
assert_core_untouched() {
  assert_not_contains "${LOG}" "<up> <-d> <core>"
  assert_not_contains "${LOG}" "<restart> <core>"
  assert_not_contains "${LOG}" "<stop> <core>"
  assert_not_contains "${LOG}" "<rm> <-f> <core>"
  assert_not_contains "${LOG}" "<build>"
  # 裸 down 会连 core 一起拆；假 docker 会以 94 判死，这里也从日志侧钉住。
  assert_not_contains "${LOG}" "<compose> <down>"
}

assert_token_absent_from_argv() {
  local token="$1" hits
  hits="$(grep -F -c -- "${token}" <<<"${LOG}" || true)"
  if [[ "${hits}" -ne 0 ]]; then
    fail_assertion "token 明文在 argv 记录里出现了 ${hits} 次，必须是 0；log: ${LOG}"
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
  rm -rf "${TMP}/state"
  mkdir -p "${TMP}/state"
  set +e
  # `env -u SW_OPS_UI_TOKEN` 让每个用例从"本机没配 token"这个确定起点开始，不受跑测试的人
  # 自己 shell 里的导出影响。TEST_AUTH_LOG 与 TEST_LOG 是**两个文件**：TEST_LOG 记 argv，
  # 要被断言"token 出现 0 次"；TEST_AUTH_LOG 记 curl 从 --config - 里真正解析出的头，
  # 要被断言"头确实送到了"。混在一起就没法同时证明这两件事。
  RESULT="$(env -u SW_OPS_UI_TOKEN ${env_args[@]+"${env_args[@]}"} \
    PATH="${TMP}/bin:${PATH}" \
    HOME="${TMP}/home dir" \
    TEST_LOG="${TMP}/log" \
    TEST_AUTH_LOG="${TMP}/auth" \
    TEST_STATE_DIR="${TMP}/state" \
    /bin/bash "${SCRIPT_UNDER_TEST}" "$@" 2>&1)"
  STATUS=$?
  set -e
  LOG="$(<"${TMP}/log")"
  AUTH_LOG="$(<"${TMP}/auth")"
}

# ==================================================== 本机参数校验（一次 SSH 都不许发）
run_case "no subcommand prints usage"
assert_status 2
assert_contains "${RESULT}" "用法："
assert_log_count "ssh <" 0

run_case "an unknown flag prints usage" --frobnicate
assert_status 2
assert_contains "${RESULT}" "无法识别的参数：--frobnicate"
assert_log_count "ssh <" 0

run_case "two subcommands are rejected" --status --up trendradar
assert_status 1
assert_contains "${RESULT}" "只能选一个"
assert_log_count "ssh <" 0

# `--up` 后面缺名字：函数里判的是**值是不是空**，不是 `$#`（那样恒为 2，等于没判）。
run_case "--up without a name is rejected" --up
assert_status 1
assert_contains "${RESULT}" "--up 后面要跟 sidecar 名字"
assert_log_count "ssh <" 0

run_case "--up followed by a flag is rejected" --up --status
assert_status 1
assert_contains "${RESULT}" "收到的是一个选项"
assert_log_count "ssh <" 0

# mpt：**有意排除**，不是"暂不支持"。文案必须说清是哪一种，否则读的人会去等下一版。
run_case "--up mpt is refused with the real reason" --up mpt
assert_status 1
assert_contains "${RESULT}" "mpt **有意不由本工具启用**，这不是「暂不支持」"
assert_contains "${RESULT}" "模型网关"
assert_contains "${RESULT}" "pexels_api_keys / pixabay_api_keys 必填其一"
assert_contains "${RESULT}" "--status（mpt 照样如实列出）"
assert_log_count "ssh <" 0

run_case "--materialize mpt is refused too" --materialize mpt
assert_status 1
assert_contains "${RESULT}" "mpt **有意不由本工具启用**"
assert_log_count "ssh <" 0

run_case "--down mpt is refused too" --down mpt
assert_status 1
assert_contains "${RESULT}" "mpt **有意不由本工具启用**"
assert_log_count "ssh <" 0

# 白名单之外的任意名字：拒绝，并说清"为什么写死"。
run_case "an arbitrary name is refused by the whitelist" --up postgres
assert_status 1
assert_contains "${RESULT}" "sidecar 名字 postgres 不在白名单里"
assert_contains "${RESULT}" "白名单**写死在脚本里，不接受运行时扩展**，当前两个：trendradar xhs-downloader"
assert_log_count "ssh <" 0

run_case "a shell-ish name is refused before ssh" --up 'trendradar;id'
assert_status 1
assert_contains "${RESULT}" "不在白名单里"
assert_log_count "ssh <" 0

run_case "core itself is not startable through this tool" --up core
assert_status 1
assert_contains "${RESULT}" "不在白名单里"
assert_log_count "ssh <" 0

# ============================================================== --status（只读）
run_case "--status prints all five sections" --status
assert_status 0
assert_log_count "ssh <" 1
assert_core_untouched
assert_nothing_started
assert_contains "${RESULT}" "Compose 服务（-a 含已停止；没列出来的就是从未创建过）"
assert_contains "${RESULT}" "sw-core   sw      core      running"
# 端口绑定来自远端解析结果，三个 profile 都被打开过（否则带 profile 的服务根本不出现）
assert_contains "${LOG}" "docker <compose> <--profile> <sourcing> <--profile> <xhs> <--profile> <video> <config> <--format> <json>"
assert_contains "${RESULT}" "端口绑定（远端 docker compose config 解析后的 host_ip，不是 grep 源文件）"
assert_contains "${RESULT}" "trendradar       127.0.0.1  8081 -> 8080  回环"
assert_contains "${RESULT}" "xhs-downloader   127.0.0.1  5556 -> 5556  回环"
assert_contains "${RESULT}" "core             127.0.0.1  8000 -> 8000  回环"
# 覆盖面必须说准：每账号小红书 sidecar 在不入库的 docker-compose.xhs.yml 里，不在这张表上。
# 少了这句，这张全是"回环"的表会被读成"全部发布口都安全"，而那是一次很贵的误读。
assert_contains "${RESULT}" "**每账号小红书 sidecar 不在里面**"
assert_contains "${RESULT}" "docs/RISKS.md §15.6"
# 配置文件：trendradar 两份都就位（夹具里 place_configs 过），mpt 那份没有
assert_contains "${RESULT}" "sidecars/trendradar/config/config.yaml"
assert_contains "${RESULT}" "sidecars/trendradar/config/frequency_words.txt"
assert_contains "${RESULT}" "就位"
assert_contains "${RESULT}" "sidecars/mpt/config.toml"
# 管辖范围：mpt 必须写明是**有意排除**
assert_contains "${RESULT}" "trendradar       可 --materialize / --up / --down（profile: sourcing）"
assert_contains "${RESULT}" "xhs-downloader   可 --materialize / --up / --down（profile: xhs）"
assert_contains "${RESULT}" "mpt              **有意排除**，本工具不起停它（profile: video）"
# R1 无关，但如实带上 use_fake_publishers 供人对照
assert_contains "${RESULT}" "use_fake_publishers  true（什么都不会真发）"
assert_contains "${RESULT}" "五段读取完毕"
assert_contains "${RESULT}" "✓ sidecar 现状读取完成"

run_case "--status reports real publishing honestly when it is on" \
  TEST_INFO_JSON='{"ok":true,"data":{"use_fake_publishers":false}}' --status
assert_status 0
assert_contains "${RESULT}" "use_fake_publishers  false（真发布已开启）"

# --status 是**只读视图**，不是闸门：看到暴露也照样把话说完并以 0 收尾，
# 真正拦人的是 --up 那一道。两者的分工必须钉死，否则以后有人会把闸门挪到这里来。
run_case "--status shows an exposed binding but stays a read-only view" TEST_XHS_HOST_IP= --status
assert_status 0
assert_contains "${RESULT}" "xhs-downloader   0.0.0.0    5556 -> 5556  ⚠ 暴露（非回环）"
assert_contains "${RESULT}" "五段读取完毕"
assert_nothing_started

run_case "--status degrades honestly when the core probe returns 401" TEST_INFO_HTTP_CODE=401 --status
assert_status 0
assert_contains "${RESULT}" "use_fake_publishers  <未取到：GET /api/v1/system/info 返回 401"
assert_contains "${RESULT}" "五段读取完毕"

run_case "--status degrades honestly when the core probe cannot connect" TEST_CURL_STATUS=7 --status
assert_status 0
assert_contains "${RESULT}" "use_fake_publishers  <未取到：GET /api/v1/system/info 失败（curl 退出码 7"
assert_contains "${RESULT}" "五段读取完毕"

run_case "--status says so when compose config cannot be parsed" TEST_CONFIG_FAIL=1 --status
assert_status 0
assert_contains "${RESULT}" "<解析不出来：docker compose config / 容器内 JSON 解析失败"
assert_contains "${RESULT}" "只读视图到此为止，不猜"
assert_contains "${RESULT}" "五段读取完毕"

# ------------------------------------------------------- 工作台 API token（红线 R5）
UI_TOKEN='TESTTOKEN_sidecar-A1b2+/=.:@'

run_case "--status carries the ui token in the config stream and never in argv" SW_OPS_UI_TOKEN="${UI_TOKEN}" --status
assert_status 0
assert_contains "${AUTH_LOG}" "url <http://127.0.0.1:8000/api/v1/system/info> header <Authorization: Bearer ${UI_TOKEN}>"
assert_token_absent_from_argv "${UI_TOKEN}"
assert_not_contains "${RESULT}" "${UI_TOKEN}"
assert_contains "${RESULT}" "已加载工作台 API token（来源：环境变量 SW_OPS_UI_TOKEN）"

# 生产开了 token、本机配对了：照常出全部五段。
run_case "--status against an auth-enabled core with the matching token" \
  TEST_REQUIRE_TOKEN="${UI_TOKEN}" SW_OPS_UI_TOKEN="${UI_TOKEN}" --status
assert_status 0
assert_contains "${RESULT}" "use_fake_publishers  true（什么都不会真发）"
assert_token_absent_from_argv "${UI_TOKEN}"

# 【--up 一个凭据都不需要，就一个字节都不送】这条不是洁癖：送出去的东西越少，泄漏面越小。
run_case "--up never carries the token to the remote" \
  SW_OPS_UI_TOKEN="${UI_TOKEN}" SW_TEST_DUMP_REMOTE="${TMP}/stream_up" --up trendradar
assert_status 0
STREAM_UP="$(<"${TMP}/stream_up")"
assert_contains "$(printf '%s\n' "${STREAM_UP}" | head -n 1)" "export SW_OPS_UI_TOKEN=''"
assert_token_absent_from_argv "${UI_TOKEN}"
if [[ "${STREAM_UP}" == *"${UI_TOKEN}"* ]]; then
  fail_assertion "--up 的远端脚本流里出现了 token 明文，必须是 0 次"
fi

# 本机字符集校验只在 --status 那条路上跑（那是唯一用 token 的模式）。
run_case "--status rejects a malformed token before ssh" SW_OPS_UI_TOKEN='TESTTOKEN_bad"quote' --status
assert_status 1
assert_contains "${RESULT}" "工作台 API token 含有不被允许的字符"
assert_log_count "ssh <" 0

# =========================================== ssh 的 argv 边界与远端脚本流的结构
run_case "the protocol codes and specs reach the remote intact" --status
assert_status 0
# ssh 那一条命令串只钉前缀：`printf %q` 到底给逗号加不加反斜杠是 **bash 版本相关**的
# （3.2 会写成 `\,`，5.x 不会），而那正是它该做的事——转义方式无关紧要，**远端重新分词之后
# 拿到什么**才是要害。所以下面三条断言看的是 depth=1/2 真正收到的 argv。
assert_contains "${LOG}" "ssh-command <bash -s -- status 50 51 52 53 54 55 56 57 58 "
assert_contains "${LOG}" "remote-args depth=1 argc=13 [status] [50] [51] [52] [53] [54] [55] [56] [57] [58] [trendradar:sourcing:8080:/:yes:sidecars/trendradar/config/config.yaml=sidecars/trendradar/config.example.yaml,sidecars/trendradar/config/frequency_words.txt=sidecars/trendradar/frequency_words.example.txt] [xhs-downloader:xhs:5556:/:yes:-] [mpt:video:8080:/:no:sidecars/mpt/config.toml=sidecars/mpt/config.example.toml]"
assert_contains "${LOG}" "remote-args depth=2 argc=13 [status] [50] [51] [52] [53] [54] [55] [56] [57] [58] [trendradar:"

# 发射进去的 sw_probe 必须落在**内层**花括号里面：落在外面时 `{ ... } </dev/null` 那层
# 结构性保证盖不到它，而内层 `bash -s` 是另一个进程，根本拿不到外层定义的函数。
cat >"${TMP}/probe_depth.awk" <<'AWKEOF'
$0 == "{" { depth++ }
$0 == "} </dev/null" { depth-- }
!seen && $0 == "sw_probe_code=''" { print depth; seen = 1 }
END { if (!seen) print "none" }
AWKEOF

run_case "the remote stream carries sw_probe inside the brace group" \
  SW_TEST_DUMP_REMOTE="${TMP}/remote_stream" SW_OPS_UI_TOKEN="${UI_TOKEN}" --status
assert_status 0
STREAM="$(<"${TMP}/remote_stream")"
stream_lines="$(printf '%s\n' "${STREAM}" | grep -c "" || true)"
if [[ "${stream_lines}" -lt 100 ]]; then
  fail_assertion "捕获到的远端脚本流只有 ${stream_lines} 行，捕获多半没生效"
fi
# 前言在花括号**外面**：它是流的第一行，且必须仍然只是一条 export。
assert_contains "$(printf '%s\n' "${STREAM}" | head -n 1)" "export SW_OPS_UI_TOKEN="
assert_contains "${STREAM}" "sw_probe_code=''"
probe_depth="$(awk -f "${TMP}/probe_depth.awk" "${TMP}/remote_stream")"
if [[ "${probe_depth}" != "2" ]]; then
  fail_assertion "sw_probe 定义出现在花括号深度 ${probe_depth} 处，应当是 2（外层包装 + 内层脚本）"
fi
def_line="$(grep -n "^sw_probe_code=''$" "${TMP}/remote_stream" | head -n 1 | cut -d: -f1 || true)"
call_line="$(grep -n '^  sw_probe .http' "${TMP}/remote_stream" | head -n 1 | cut -d: -f1 || true)"
if [[ -z "${def_line}" || -z "${call_line}" || "${def_line}" -ge "${call_line}" ]]; then
  fail_assertion "sw_probe 的定义在第 ${def_line:-<无>} 行、第一次调用在第 ${call_line:-<无>} 行，定义必须在前"
fi

# =================================================== 端口回环闸门（本脚本最重要的一道）
#
# 【为什么这几条用例是这份测试的重点】docs/RISKS.md 第 15 条：xhs-downloader 与每账号小红书
# sidecar 曾经绑 0.0.0.0，而生产是合租机器，同机其它 docker 网络里的容器经默认网关就够得着
# （§15.2 实测）。每账号 sidecar 带着该账号的登录态 cookies 且 AUTH_TOKEN 默认为空。
# 所以"起之前必须证明它只绑回环"这件事，比"能不能起起来"重要得多。
#
# 假 docker 的 config 在 host_ip 为空时**不产生该键**——这正是 `5556:5556` 那种写法的真实
# 解析形态，也是最危险的形态。被测脚本必须把"没有 host_ip"当成 0.0.0.0，而不是"没匹配到"。
run_case "--up refuses to start when the resolved binding is not loopback" \
  TEST_TRENDRADAR_HOST_IP= --up trendradar
assert_status 50
assert_contains "${RESULT}" "trendradar       0.0.0.0    8081 -> 8080  ⚠ 暴露（非回环）"
assert_contains "${RESULT}" "端口闸门判红：trendradar 解析出来的发布地址**不是回环**，拒绝启动"
assert_contains "${RESULT}" "同机其它 docker 网络里的容器经默认网关（172.17.0.1）就够得着"
assert_contains "${RESULT}" "AUTH_TOKEN 默认为空，留空即不鉴权"
assert_contains "${RESULT}" "**没有启动任何容器**"
assert_nothing_started
assert_core_untouched

run_case "--up refuses the exposed xhs-downloader too" TEST_XHS_HOST_IP= --up xhs-downloader
assert_status 50
assert_contains "${RESULT}" "xhs-downloader   0.0.0.0    5556 -> 5556  ⚠ 暴露（非回环）"
assert_contains "${RESULT}" "端口闸门判红"
assert_nothing_started

# 显式写着 0.0.0.0 的形态（compose 会原样保留该键）同样要被拦下。
run_case "--up refuses an explicit 0.0.0.0 host_ip" TEST_TRENDRADAR_HOST_IP=0.0.0.0 --up trendradar
assert_status 50
assert_contains "${RESULT}" "⚠ 暴露（非回环）"
assert_nothing_started

# `localhost` **不放行**：它要过 /etc/hosts 才知道指向哪儿，而"绑在哪个地址"这件事不该由
# 一次名字解析来回答。这是白名单（只认 127.0.0.1 / ::1），不是黑名单。
run_case "--up refuses a non-literal loopback spelling" TEST_TRENDRADAR_HOST_IP=localhost --up trendradar
assert_status 50
assert_contains "${RESULT}" "⚠ 暴露（非回环）"
assert_nothing_started

# ::1 是回环，放行。
run_case "--up accepts the ipv6 loopback" \
  TEST_TRENDRADAR_HOST_IP=::1 TEST_PORT_MAPPING='[::1]:8081' --up trendradar
assert_status 0
assert_contains "${RESULT}" "trendradar       ::1        8081 -> 8080  回环"
assert_contains "${RESULT}" "trendradar:8080 -> [::1]:8081（回环）"
assert_contains "${LOG}" "curl <-q> <-s> <-o> </dev/null> <--max-time> <5> <-w> <%{http_code}> <http://[::1]:8081/>"

# 一个宿主机端口都不发布 = 没有东西可暴露，闸门放行；但那也意味着宿主机上探不到它。
# 这条路径必须**明说本次没有探过**，绝不能含糊成"已启动"——也绝不能把"本来就没有发布口"
# 判成"绑定核不过"进而把容器停掉删掉（那是一次字面为假的处置）。
run_case "--up says plainly that it could not probe a service with no published port" \
  TEST_CONFIG_JSON='{"services":{"core":{"image":"x","ports":[{"mode":"ingress","host_ip":"127.0.0.1","target":8000,"published":"8000","protocol":"tcp"}]},"trendradar":{"image":"x"}}}' \
  --up trendradar
assert_status 0
assert_contains "${RESULT}" "在解析结果里**没有发布任何宿主机端口**"
assert_contains "${RESULT}" "运行期绑定核验与存活探针**都不会跑**，本次不会有"
assert_contains "${RESULT}" "启动完毕（端口闸门通过；无发布口，运行期核验与存活探针本次未执行）"
assert_contains "${LOG}" "docker <compose> <--profile> <sourcing> <up> <-d> <trendradar>"
# 既没有去读发布端口，也没有把容器拆掉。
assert_not_contains "${LOG}" "<port> <trendradar>"
assert_not_contains "${LOG}" "<stop> <trendradar>"
assert_not_contains "${LOG}" "curl <-q> <-s>"

# 闸门判不了的三种形态，一律 fail-closed。**"判不了"与"判红"是两件事**，退出码也不同。
run_case "--up fails closed when compose config itself fails" TEST_CONFIG_FAIL=1 --up trendradar
assert_status 51
assert_contains "${RESULT}" "端口闸门判不了：远端 \`docker compose config\` 执行失败，拒绝启动"
assert_nothing_started

run_case "--up fails closed when the service is absent from the resolved config" \
  TEST_OMIT_SERVICE=trendradar --up trendradar
assert_status 51
assert_contains "${RESULT}" "服务 trendradar 不在 \`docker compose --profile sourcing config\` 的解析结果里"
assert_contains "${RESULT}" "fail-closed：证明不了它只绑回环，就不启动"
assert_nothing_started

run_case "--up fails closed when the core container cannot parse the json" \
  TEST_CORE_EXEC_FAIL=1 --up trendradar
assert_status 51
assert_contains "${RESULT}" "端口闸门判不了：解析 compose 配置失败"
assert_contains "${RESULT}" "core 容器没起来"
assert_nothing_started

run_case "--up fails closed when the resolved config is not valid json" \
  TEST_CONFIG_JSON='{"services": [' --up trendradar
assert_status 51
assert_nothing_started

# 脚本里那张表与解析结果对不上时**拒绝按猜测行事**：启动后的核验与探测都要靠容器端口，
# 猜错了就会"探了个别的东西然后宣布成功"。
run_case "--up refuses when the policy table and the resolved config disagree" \
  TEST_TRENDRADAR_TARGET=9999 --up trendradar
assert_status 58
assert_contains "${RESULT}" "本脚本那张表说 trendradar 的容器端口是 8080，但解析结果里没有这一条"
assert_nothing_started

# ============================================================ --up 正常路径与起后核验
run_case "--up starts exactly one service and proves it is listening" --up trendradar
assert_status 0
# 闸门在前，起在后，顺序不能反
assert_contains "${RESULT}" "端口闸门（远端 docker compose config 解析后的 host_ip）"
assert_contains "${RESULT}" "trendradar       127.0.0.1  8081 -> 8080  回环"
assert_contains "${LOG}" "docker <compose> <--profile> <sourcing> <config> <--format> <json>"
assert_contains "${LOG}" "docker <compose> <--profile> <sourcing> <up> <-d> <trendradar>"
assert_contains "${LOG}" "docker <compose> <--profile> <sourcing> <port> <trendradar> <8080>"
assert_contains "${RESULT}" "trendradar:8080 -> 127.0.0.1:8081（回环）"
assert_contains "${RESULT}" "GET http://127.0.0.1:8081/ -> HTTP 200（第 1 次）"
assert_contains "${RESULT}" "启动完毕（端口闸门 → 起 → 运行期绑定核验 → 存活探针，四步都过了）"
assert_contains "${RESULT}" "✓ trendradar 已启动"
assert_core_untouched
# 探 sidecar **绝不能**带上 core 的 Authorization 头：那是把 core 的凭据交给一个上游镜像。
assert_not_contains "${AUTH_LOG}" "8081"

run_case "--up xhs-downloader warns about the missing auth before connecting" --up xhs-downloader
assert_status 0
assert_contains "${RESULT}" "xhs-downloader 在本仓侧**没有任何鉴权**"
assert_contains "${RESULT}" "挡不住同一台宿主机上的其他进程"
assert_contains "${LOG}" "docker <compose> <--profile> <xhs> <up> <-d> <xhs-downloader>"
assert_contains "${RESULT}" "✓ xhs-downloader 已启动"

# 任何一个 HTTP 状态码都算"在监听"——404 恰恰证明它在。判据与 scripts/preflight.py 的
# _probe_http 一致；用 sw_probe（带 -f）会把这条判成失败，那是错的。
run_case "--up treats any http status as alive" TEST_SIDECAR_HTTP_CODE=404 --up trendradar
assert_status 0
assert_contains "${RESULT}" "GET http://127.0.0.1:8081/ -> HTTP 404（第 1 次）"
assert_contains "${RESULT}" "✓ trendradar 已启动"

# 配置没就位就拒绝启动：上游 entrypoint.sh 缺文件直接 exit 1，而它是 restart: unless-stopped，
# 起了就是一个崩溃重启循环。
rm -f "${FIXTURE}/sidecars/trendradar/config/config.yaml"
run_case "--up refuses when the sidecar config is not in place" --up trendradar
assert_status 52
assert_contains "${RESULT}" "sidecars/trendradar/config/config.yaml"
assert_contains "${RESULT}" "配置文件没就位，拒绝启动"
assert_contains "${RESULT}" "崩溃重启循环"
assert_contains "${RESULT}" "bash scripts/ops/sidecar.sh --materialize trendradar"
assert_nothing_started
place_configs

run_case "--up reports a compose up failure without claiming success" TEST_UP_STATUS=1 --up trendradar
assert_status 53
assert_contains "${RESULT}" "docker compose up -d trendradar 失败"
assert_not_contains "${RESULT}" "✓ trendradar 已启动"

# 起完之后实际绑定不是回环：端口已经开了，所以**当场停掉并删除**，而不是打印一行警告了事。
run_case "--up tears the container down when the runtime binding is exposed" \
  TEST_PORT_MAPPING='0.0.0.0:8081' --up trendradar
assert_status 57
assert_contains "${RESULT}" "**已当场停掉并删除该容器**"
assert_contains "${LOG}" "docker <compose> <--profile> <sourcing> <stop> <trendradar>"
assert_contains "${LOG}" "docker <compose> <--profile> <sourcing> <rm> <-f> <trendradar>"
assert_contains "${RESULT}" "没能被证明是回环"
assert_not_contains "${RESULT}" "✓ trendradar 已启动"
assert_core_untouched

# 容器已退出时 `docker compose port` 给出空输出并以 0 收尾——必须拒绝空值，不能当成"通过"。
run_case "--up rejects an empty runtime port mapping" TEST_PORT_EMPTY=1 --up trendradar
assert_status 57
assert_contains "${RESULT}" "必须是恰好一条 loopback 映射"
assert_not_contains "${RESULT}" "✓ trendradar 已启动"

# 探不到就如实报。**这条最容易被写成"打印已启动了事"**：up -d 返回 0 只说明容器被创建过。
run_case "--up never claims success when the probe cannot reach the sidecar" \
  TEST_SIDECAR_DEAD=trendradar --up trendradar
assert_status 54
assert_contains "${RESULT}" "20 秒内探不到 http://127.0.0.1:8081/ 在监听"
assert_contains "${RESULT}" "**不宣称已启动**"
assert_contains "${RESULT}" "fake sidecar log line"
assert_contains "${RESULT}" "bash scripts/ops/sidecar.sh --down trendradar"
assert_not_contains "${RESULT}" "✓ trendradar 已启动"
assert_not_contains "${RESULT}" "启动完毕"
# 探针失败**不**拆容器：绑定这一格是好的，日志要留着给人看。
assert_not_contains "${LOG}" "<stop> <trendradar>"

# ================================================================ --materialize
reset_fixture
run_case "--materialize creates both files from the deployed templates" --materialize trendradar
assert_status 0
assert_contains "${RESULT}" "sidecars/trendradar/config/config.yaml"
assert_contains "${RESULT}" "已生成（来源模板：sidecars/trendradar/config.example.yaml）"
assert_contains "${RESULT}" "已生成（来源模板：sidecars/trendradar/frequency_words.example.txt）"
assert_contains "${RESULT}" "生成完毕（新建 2 个，已存在保留 0 个）"
assert_contains "${RESULT}" "✓ 配置生成完成"
if ! cmp -s "${FIXTURE}/sidecars/trendradar/config.example.yaml" "${FIXTURE}/sidecars/trendradar/config/config.yaml"; then
  fail_assertion "生成出来的 config.yaml 与模板不是逐字节相同"
fi
if ! cmp -s "${FIXTURE}/sidecars/trendradar/frequency_words.example.txt" "${FIXTURE}/sidecars/trendradar/config/frequency_words.txt"; then
  fail_assertion "生成出来的 frequency_words.txt 与模板不是逐字节相同"
fi
# 一个容器都没起过，也没连 core 去改什么。
assert_nothing_started

# 【已存在则不覆盖】人可能在上面填过自部署 newsnow 地址之类的本地信息，而本工具没有任何
# 办法分辨"模板原样"与"人改过"。这一条是硬要求，反向补丁（改成覆盖）必须让这里变红。
reset_fixture
printf 'MY-LOCAL-EDIT: newsnow at http://10.0.0.9/api/s\n' >"${FIXTURE}/sidecars/trendradar/config/config.yaml"
run_case "--materialize never overwrites an existing config" --materialize trendradar
assert_status 0
assert_contains "${RESULT}" "已存在，跳过（不覆盖）"
assert_contains "${RESULT}" "生成完毕（新建 1 个，已存在保留 1 个）"
assert_file_contains "${FIXTURE}/sidecars/trendradar/config/config.yaml" "MY-LOCAL-EDIT"
if grep -qF 'newsnow.busiyi.world' "${FIXTURE}/sidecars/trendradar/config/config.yaml"; then
  fail_assertion "已存在的 config.yaml 被模板覆盖了——人填进去的本地信息丢了"
fi
# 另一份不存在的照常生成
if ! cmp -s "${FIXTURE}/sidecars/trendradar/frequency_words.example.txt" "${FIXTURE}/sidecars/trendradar/config/frequency_words.txt"; then
  fail_assertion "另一份该生成的没生成出来"
fi

reset_fixture
# 哨兵内容要**足够独特**：拿 `A` / `B` 这种一个字符去断言，模板里随便一处大写字母就能
# 让它误绿——反向补丁（改成覆盖）时那条断言就抓不到。
printf 'SENTINEL-A-do-not-overwrite\n' >"${FIXTURE}/sidecars/trendradar/config/config.yaml"
printf 'SENTINEL-B-do-not-overwrite\n' >"${FIXTURE}/sidecars/trendradar/config/frequency_words.txt"
run_case "--materialize is a no-op when both files already exist" --materialize trendradar
assert_status 0
assert_contains "${RESULT}" "生成完毕（新建 0 个，已存在保留 2 个）"
assert_file_contains "${FIXTURE}/sidecars/trendradar/config/config.yaml" "SENTINEL-A-do-not-overwrite"
assert_file_contains "${FIXTURE}/sidecars/trendradar/config/frequency_words.txt" "SENTINEL-B-do-not-overwrite"

# 模板不在生产上 = 部署没带过去。**不要**退化成"那我从本机拷一份过去"——本工具没有、
# 也不打算有推文件的能力。
reset_fixture
rm -f "${FIXTURE}/sidecars/trendradar/frequency_words.example.txt"
run_case "--materialize reports a missing template instead of inventing one" --materialize trendradar
assert_status 55
assert_contains "${RESULT}" "模板不在生产上：sidecars/trendradar/frequency_words.example.txt"
assert_contains "${RESULT}" "本该随 update.sh 的快进部署过来"
assert_contains "${RESULT}" "「把本机文件推到生产」的能力"
[[ ! -f "${FIXTURE}/sidecars/trendradar/config/frequency_words.txt" ]] \
  || fail_assertion "模板缺失时不该生成任何东西"

reset_fixture
place_configs
run_case "--materialize says so when the sidecar has no config at all" --materialize xhs-downloader
assert_status 0
assert_contains "${RESULT}" "xhs-downloader 没有配置依赖，本次一个文件都没生成"
assert_contains "${RESULT}" "生成完毕（0 个新文件）"

# ======================================================================= --down
run_case "--down stops and removes exactly one service" --down trendradar
assert_status 0
assert_contains "${LOG}" "docker <compose> <--profile> <sourcing> <stop> <trendradar>"
assert_contains "${LOG}" "docker <compose> <--profile> <sourcing> <rm> <-f> <trendradar>"
assert_contains "${RESULT}" "已停止并删除容器：trendradar（core 没有被碰过）"
assert_contains "${RESULT}" "✓ trendradar 已停止并删除"
assert_core_untouched

# ========================================================= 传输失败与远端 255 规范化
run_case "an ssh transport failure is reported without retrying" TEST_SSH_STATUS=255 --up trendradar
# ssh 自己的 255 原样透出，好与远端的 254（远端脚本自身退 255 的规范化结果）分开。
assert_status 255
assert_contains "${RESULT}" "SSH 传输中断（IAP 断链）"
assert_contains "${RESULT}" "刻意不自动重试"
assert_log_count "ssh <" 1

# ============================================ stdin 的结构性保证（正例；反例见下方说明）
#
# 远端正文外面那两对花括号 + 尾部的 `} </dev/null` 是现在唯一承重的那一层：
#   ① `{ ... }` 是一条复合命令，bash 必须整条解析完才开始执行，正文因此在第一条命令跑起来
#      之前就已经离开输入流，任何读 stdin 的子进程都吞不到它；
#   ② `</dev/null` 挂在整个组上，组内所有命令与子进程继承的 fd 0 就是 /dev/null。
# 这里只放**正例**：把逐条命令上的 `</dev/null` 标注全部删掉，行为必须逐字不变，证明那些
# 标注已经降级成纵深防御。
#
# 【反例（把结构也删掉、让历史缺陷重现）刻意不放在这里，理由如实写明】能把脚本正文吞掉的
# 只有真的会读 stdin 的命令，而本脚本里那样的命令只有两条 `docker compose exec -T`，它们
# **本来就由前置管道显式喂**（`printf '%s' "${json}" | docker compose exec …`），删掉标注
# 与结构都不会让它们去读脚本正文。其余那些带 `</dev/null` 的（`ps` / `up` / `port` /
# `stop` / `rm` / `logs` / `curl`）真实命令根本不读 stdin——要造出"被吞掉"就得让假件去读，
# 那就不忠实了，等于用一个假的失败模式换一条好看的用例。反例在 tests/ops/test_update.sh 与
# test_verify.sh 里有（那两个脚本有不带管道的 `docker compose exec -T core python3
# scripts/preflight.py`），本文件不重复。test_status.sh 也是同一处置。
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
if [[ "${struct_annotations_left}" -ne 2 ]]; then
  fail_assertion "annotations_stripped 里应只剩 2 处 </dev/null（两层组重定向），实际 ${struct_annotations_left} 处"
fi

SCRIPT_UNDER_TEST="${STRUCT_DIR}/annotations_stripped.sh"
run_case "stripping every per-command </dev/null changes nothing" --status
assert_status 0
assert_contains "${RESULT}" "五段读取完毕"
assert_contains "${RESULT}" "✓ sidecar 现状读取完成"
run_case "stripping every per-command </dev/null does not weaken the port gate" \
  TEST_TRENDRADAR_HOST_IP= --up trendradar
assert_status 50
assert_nothing_started
SCRIPT_UNDER_TEST="${SCRIPT}"

# ============================================ 源码级：白名单与策略表不许悄悄漂移
case_name="the whitelist stays exactly two entries"
cases=$((cases + 1))
whitelist_line="$(sed -n 's/^SW_SIDECAR_WHITELIST="\(.*\)"$/\1/p' "${SCRIPT}")"
if [[ "${whitelist_line}" != "trendradar xhs-downloader" ]]; then
  fail_assertion "白名单变成了 [${whitelist_line}]，期望 [trendradar xhs-downloader]——真要加一个，先把它的六格策略、闸门与用例一起补上，再改这条断言"
fi
reported_line="$(sed -n 's/^SW_SIDECAR_REPORTED="\(.*\)"$/\1/p' "${SCRIPT}")"
if [[ "${reported_line}" != "trendradar xhs-downloader mpt" ]]; then
  fail_assertion "--status 的报告面变成了 [${reported_line}]，期望 [trendradar xhs-downloader mpt]"
fi

# 端口判定必须是**白名单**（只认 127.0.0.1 与 ::1）。有人把它改成"排除 0.0.0.0"这类黑名单
# 时这条会红：黑名单永远漏得掉下一个写法（`::`、`0.0.0.0` 的十进制写法、某个网卡地址……）。
case_name="the loopback check is a whitelist, not a blacklist"
cases=$((cases + 1))
if ! grep -qF 'host_ip in ("127.0.0.1", "::1")' "${SCRIPT}"; then
  fail_assertion "端口判定不再是那条 127.0.0.1/::1 白名单了——黑名单写法漏得掉下一个形态，不接受"
fi
if ! grep -qF 'str(item.get("host_ip") or "0.0.0.0")' "${SCRIPT}"; then
  fail_assertion "解析里不再把「没有 host_ip 键」当成 0.0.0.0 了——那正是 5556:5556 的真实解析形态，也是最危险的形态"
fi

if [[ "${failures}" -ne 0 ]]; then
  printf 'sidecar.sh mechanical tests failed: %s assertion(s)\n' "${failures}" >&2
  exit 1
fi
printf 'sidecar.sh mechanical tests passed: %s case(s)\n' "${cases}"
