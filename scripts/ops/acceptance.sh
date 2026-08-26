#!/usr/bin/env bash
# 用途：在**生产机器上、用生产镜像**跑一遍端到端验收，证「无干预跑完全流程」。
#
# 【为什么这条要单独存在，而不是在本机跑 scripts/acceptance_full_chain.py 就算数】
# 本机跑证的是"这份代码接得上"。它证不了生产那台机器上的镜像里到底有没有 chromium——
# 而这正是"全自动"在生产上跑不动的真实原因：没有 chromium → 封面渲不出来 → 机器审核记
# 一条 warn → autopilot 的自动批准条件是 block == 0 且 warn == 0，一条 warn 就够让它
# 不批 → 每条稿子都退回人工审核台。整条链在门禁一声不吭的情况下断在这里。
#
# 【它碰不到生产的任何真实数据】跑的是 acceptance_full_chain.py 自带的沙盒：临时库、
# 临时媒体目录、FakePublisher、Telegram 关掉、SW_ACCOUNTS_FILE 指向临时文件。这几道
# 保险由那个脚本**在进程内**逐条自检，不成立就以 40 退出——不靠"我记得它是隔离的"。
#
# 自检为什么必须长在那边、而不是由这里在远端 grep 一遍源码：docker-compose.yml 里 core
# **没有**把源码 bind mount 进容器（只挂 core_data 与 accounts.yaml），真正跑的是镜像里
# 烤进去的那一份。在宿主机检出上 grep 等于检查了另一个文件——正常情况下两份一样，而
# "正常情况下一样"恰恰是这类护栏最常见的失效方式。这条通路只负责把 40 如实传出来。
#
# 【它也不会替任何人点确认】脚本建的两个账号是它自己的临时账号；生产台账里那些
# confirm_required=true 的账号一个都不碰。R1 红线不在这条通路上。
set -euo pipefail

SSH_ALIAS="${SW_OPS_SSH_ALIAS:-${SW_TUNNEL_SSH_ALIAS:-workbench-iap}}"
LANE="xhs"
DRY_RUN=0
LANE_SET=0

#: 生产镜像里没有渲染链。和"验收未通过"（1）分得开：一个该去装东西，一个该去查代码。
EXIT_RENDER_CHAIN_MISSING=3
#: 沙盒自检没过——镜像里那份脚本的隔离保险不成立，容器内拒跑。
EXIT_SANDBOX_UNPROVEN=40

die() { printf '\n✗ %s\n' "${1}" >&2; shift; for line in "$@"; do printf '  %s\n' "${line}" >&2; done; exit 1; }
# 和 die 的区别只在退出码：远端判定出来的码要**原样**传出去，否则调用方分不清
# 「该去装 chromium」（3）、「该去查代码」（1）和「拒跑」（40）。
die_with() { local code="${1}"; shift; printf '\n✗ %s\n' "${1}" >&2; shift; for line in "$@"; do printf '  %s\n' "${line}" >&2; done; exit "${code}"; }
note() { printf '  %s\n' "${1}"; }

usage() { cat >&2 <<'USAGE'
用法：bash scripts/ops/acceptance.sh [--lane xhs|wechat] [--dry-run]

在生产机器上、用生产镜像跑一遍端到端验收（隔离沙盒，碰不到真实台账与数据库）。

退出码
  0   验收通过：闸门关的账号零干预走到 measured，闸门开的账号停在 scheduled
  1   验收未通过（脚本自己判红，输出里有失败项）
  3   生产镜像里没有渲染链——autopilot 在那台机器上批不了任何稿子
  40  沙盒自检没过：镜像里那份脚本有隔离保险不成立，容器内拒跑
USAGE
}

while [[ "${#}" -gt 0 ]]; do
  case "${1}" in
    --lane)
      [[ "${#}" -ge 2 && "${2}" != --* ]] || die "--lane 缺少赛道名"
      LANE_SET=$((LANE_SET + 1))
      [[ "${LANE_SET}" -eq 1 ]] || die "--lane 只能指定一次"
      LANE="${2}"
      shift
      ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) usage; exit 0 ;;
    *) usage; die "参数无效：${1}" ;;
  esac
  shift
done

# 白名单校验。赛道名会拼进远端命令，这里只允许两个字面量——不是"过滤掉危险字符"，
# 是"除了这两个词一律不放行"。
case "${LANE}" in
  xhs|wechat) ;;
  *) die "赛道无效：${LANE}" "只有 xhs 与 wechat 两条；两条都要求镜像里有渲染链" ;;
esac

command -v ssh >/dev/null 2>&1 || die "本机没有 ssh 命令"

printf '生产端到端验收（--lane %s）\n\n' "${LANE}"
note "目标 ${SSH_ALIAS}：docker compose run --rm --no-deps core"
note "跑 scripts/acceptance_full_chain.py --offline --lane ${LANE}"
note "隔离：临时库 / 临时媒体目录 / FakePublisher / Telegram 关 / 临时台账（容器内进程自检）"

if [[ "${DRY_RUN}" -eq 1 ]]; then
  printf '\n演练模式：只展示将要执行的动作，不连远端。\n'
  exit 0
fi

set +e
ssh -o ConnectTimeout=25 "${SSH_ALIAS}" "bash -s -- ${LANE}" <<'REMOTE'
set -euo pipefail
# 这层 bash 的脚本正文就是它自己的 stdin（正上方这个 REMOTE heredoc）。任何读 stdin 的
# 子进程都会把"脚本剩下的部分"吞掉，而脚本仍以 0 收尾。所以每一条 docker 都带显式
# stdin 来源——本仓的不变量，不留例外。

lane="${1}"
cd "${HOME}/social_workflow"

# 沙盒自检不在这里做，在容器里做——见本文件头。这里 grep 宿主机检出等于检查了另一个
# 文件：compose 没有把源码 bind mount 进 core，真正跑的是镜像里烤进去的那份。
docker compose run --rm --no-deps core \
  python scripts/acceptance_full_chain.py --offline --lane "${lane}" </dev/null
REMOTE
STATUS=$?
set -e

printf '\n'
case "${STATUS}" in
  0) note "✓ 生产验收通过：闸门关的账号零干预走到 measured，闸门开的账号停在 scheduled" ;;
  "${EXIT_RENDER_CHAIN_MISSING}")
    die_with "${EXIT_RENDER_CHAIN_MISSING}" "生产镜像里没有渲染链" \
        "autopilot 的自动批准要求 block == 0 且 warn == 0，封面渲不出来就是一条 warn。" \
        "后果是这台机器上每条稿子都退回人工审核台——「全自动」对任何平台都不成立。" \
        "修：Dockerfile 里 uv sync 带 --extra render，并 playwright install chromium，" \
        "然后 bash scripts/ops/update.sh --apply。"
    ;;
  "${EXIT_SANDBOX_UNPROVEN}")
    die_with "${EXIT_SANDBOX_UNPROVEN}" "沙盒自检没过，容器内拒跑" \
        "镜像里那份 acceptance_full_chain.py 有隔离保险不成立，上面列了是哪几条。" \
        "在核对清楚之前不要动那道自检——它拦的是「在生产库上跑一遍采集与发布」。"
    ;;
  1) die_with 1 "生产验收未通过" "远端输出里的「失败项」列了具体哪一条断了。" ;;
  *) die_with "${STATUS}" "生产验收异常退出（状态 ${STATUS}）" "多半是 ssh 或 docker 层面的问题，不是验收判定。" ;;
esac
