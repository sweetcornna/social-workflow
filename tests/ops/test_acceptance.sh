#!/bin/bash
# scripts/ops/acceptance.sh —— 在生产机器上跑端到端验收的那条运维通路。
# 零网络、零 ssh、零 docker：ssh 是本地的 argv + stdin 记录假件。
#
# 【这份测试盯的是什么】不是"验收能不能过"（那是 tests/test_acceptance_script.py 的事），
# 是这条**运维通路**本身：赛道名进不了远端 shell、隔离前置检查一条不少、远端判定的退出码
# 原样传出来、以及远端 heredoc 里每条 docker 都带显式 stdin 来源。
#
# scripts/ci_local.sh 的 ops job 会自动发现 tests/ops/test_*.sh，不需要登记。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="${ROOT}/scripts/ops/acceptance.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

mkdir -p "${TMP}/bin"

# 假 ssh：把完整 argv 记一份，把喂进来的远端脚本正文（stdin）另记一份。
# 两份必须分开——"参数没被拼进去"和"远端脚本长什么样"是两个不同的断言。
cat >"${TMP}/bin/ssh" <<'EOF'
#!/bin/bash
{
  printf 'ssh'
  printf ' <%s>' "$@"
  printf '\n'
} >>"${TEST_LOG}"
cat >>"${TEST_STDIN_LOG}"
exit "${TEST_SSH_STATUS:-0}"
EOF
chmod +x "${TMP}/bin/"*

# ── 断言 ───────────────────────────────────────────────────────────────────
failures=0
cases=0
case_name=""
RESULT=""
STATUS=0
LOG=""
REMOTE_SRC=""

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

# 用法：run_case "名字" [VAR=VAL ...] -- [脚本参数...]
run_case() {
  case_name="$1"; shift
  cases=$((cases + 1))
  local envs=()
  while [[ "${#}" -gt 0 && "${1}" != "--" ]]; do envs+=("$1"); shift; done
  [[ "${1:-}" == "--" ]] && shift
  : >"${TMP}/log"
  : >"${TMP}/stdin-log"
  set +e
  RESULT="$(
    env -i \
      PATH="${TMP}/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
      HOME="${TMP}/home" \
      TEST_LOG="${TMP}/log" \
      TEST_STDIN_LOG="${TMP}/stdin-log" \
      ${envs[@]+"${envs[@]}"} \
      /bin/bash "${SCRIPT}" "$@" 2>&1
  )"
  STATUS=$?
  set -e
  LOG="$(cat "${TMP}/log")"
  REMOTE_SRC="$(cat "${TMP}/stdin-log")"
}

# ── 用例 ───────────────────────────────────────────────────────────────────

# 1. 默认赛道是 xhs，而且赛道名走 `bash -s --` 的位置参数，不拼进远端脚本正文。
run_case "default lane is xhs and travels as a positional arg" --
assert_status 0
assert_contains "${LOG}" "bash -s -- xhs"
assert_contains "${RESULT}" "生产端到端验收（--lane xhs）"
assert_contains "${RESULT}" "✓ 生产验收通过"

# 2. --lane wechat 照样传过去。
run_case "explicit lane passes through" -- --lane wechat
assert_status 0
assert_contains "${LOG}" "bash -s -- wechat"

# 3. 赛道名是**白名单**，不是"过滤危险字符"。带分号的一律拒，而且一步都不许连远端。
run_case "injection-shaped lane is refused before any ssh" -- --lane 'xhs; rm -rf /'
assert_status 1
assert_contains "${RESULT}" "赛道无效"
[[ -z "${LOG}" ]] || fail_assertion "非法赛道居然连了远端；log: ${LOG}"

# 4. --dry-run 一步都不连远端。
run_case "dry-run touches nothing" -- --dry-run
assert_status 0
assert_contains "${RESULT}" "演练模式"
[[ -z "${LOG}" ]] || fail_assertion "演练模式居然连了远端；log: ${LOG}"

# 5. 参数错误当场停。
run_case "unknown flag stops" -- --nope
assert_status 1
assert_contains "${RESULT}" "参数无效"
run_case "lane twice stops" -- --lane xhs --lane wechat
assert_status 1
assert_contains "${RESULT}" "只能指定一次"

# 6. 远端判定的退出码要**原样**传出来。3 和 1 混成一个码，调用方就分不清
#    "该去装 chromium"和"该去查代码"。
run_case "render-chain-missing keeps exit code 3" TEST_SSH_STATUS=3 --
assert_status 3
assert_contains "${RESULT}" "生产镜像里没有渲染链"
assert_contains "${RESULT}" "--extra render"

run_case "sandbox refusal keeps exit code 40" TEST_SSH_STATUS=40 --
assert_status 40
assert_contains "${RESULT}" "隔离前置检查没过"
assert_not_contains "${RESULT}" "绕过"$'\n'  # 提示里不许给"绕过它"的做法

run_case "acceptance failure keeps exit code 1" TEST_SSH_STATUS=1 --
assert_status 1
assert_contains "${RESULT}" "生产验收未通过"

# 7. 远端脚本正文：五道隔离保险一条都不许少，而且是**在远端当场核**，
#    不是在本机 assert 一句"我记得它是隔离的"。
case_name="remote body verifies all five isolation guards"
cases=$((cases + 1))
for guard in SW_USE_FAKE_PUBLISHERS SW_TELEGRAM_ENABLED SW_SYNC_ACCOUNTS_ON_START \
             SW_ACCOUNTS_FILE SW_DATABASE_URL; do
  assert_contains "${REMOTE_SRC}" "${guard}"
done
assert_contains "${REMOTE_SRC}" "隔离前置检查没过，拒跑"

# 8. 远端 heredoc 的 stdin 不变量：每条 docker 都要有显式 stdin 来源，否则它会把
#    "脚本剩下的部分"吞掉，而脚本仍以 0 收尾——历史事故正是这么来的。
case_name="every remote docker command pins its stdin"
cases=$((cases + 1))
while IFS= read -r line; do
  [[ "${line}" == *"</dev/null"* ]] && continue
  # 续行结尾的那几行由下一行收尾，跳过
  [[ "${line}" == *"\\" ]] && continue
  fail_assertion "远端有条 docker 没钉 stdin：${line}"
done < <(grep -n 'docker compose' "${SCRIPT}" | grep -v '^\s*#' | grep -v 'note ' || true)

# 9. 这条通路上不许出现任何"替人点确认"的动作（红线 R1 / R4）。
case_name="no confirm action sneaks into this ops path"
cases=$((cases + 1))
if grep -qiE 'confirm_item|/confirm|approve_publish|confirm_required=false|--confirm' "${SCRIPT}"; then
  fail_assertion "运维通路里出现了确认发布类动作（红线 R1）"
fi

if [[ "${failures}" -ne 0 ]]; then
  printf 'ops/acceptance.sh tests failed: %s assertion(s)\n' "${failures}" >&2
  exit 1
fi
printf 'ops/acceptance.sh tests passed: %s case(s)\n' "${cases}"
