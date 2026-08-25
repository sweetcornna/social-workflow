#!/usr/bin/env bash
# 用途：经 IAP SSH 只读查看生产 core 的编排状态、探针、磁盘和数据卷文件。
#
# 【为什么本脚本不报 git 分支 / 发布线——这是一次明确的决定，不是遗漏】
# scripts/ops/verify.sh 在纠正部署参照系时（docs/RISKS.md 第 11 条）新增了「发布线」与
# 「部署标记」两段。本脚本刻意**不**跟进，理由三条，按分量排：
#   ① 那两段一旦搬过来，就是**第二份**要跟着 verify.sh 一起改的实现。本轮花掉全部风险预算
#      做的正是相反的事——把 sw_probe 的四份拷贝收成一份。为了对称再造一份新的双胞胎，
#      方向是错的。真要做，得先把它也做成 ui_token.sh 里的发射片段，那是另一件事的规模。
#   ② 本脚本的四段输出与"三段/四段读取完毕"那条防回归哨兵是绑死的（401 降级路径只跑三段，
#      措辞必须与实际发生的事一致）。加第五段要动哨兵、退出码语义与它们的测试，
#      而这些正是上一批花了力气才对齐的东西。
#   ③ 收益很小：verify.sh 是**纯只读、零副作用、可重复跑**的，问"生产现在在哪条线上"直接
#      `bash scripts/ops/verify.sh` 就有答案，而且是带部署标记对照的完整答案。
# 所以口径统一在一个地方：**发布线的问题归 verify.sh**。本脚本回答的是"编排/探针/磁盘/
# 数据卷现在什么样"。将来若真要补，请连同 ① 一起补，别只搬输出。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH_ALIAS="${SW_OPS_SSH_ALIAS:-${SW_TUNNEL_SSH_ALIAS:-workbench-iap}}"

# 远端退出码协议（**这里是唯一定义处**，用 printf '%q' 传给远端当位置参数，远端不硬编码）：
#   PROBE_UNAUTHORIZED_STATUS  Core 探针拿到 401：core 已启用 SW_UI_TOKEN 而本机没有匹配的
#                              token。其余三段照常打印完，最后才以这个码收尾。
# 要避开 ssh 保留的 255，也要避开远端脚本可能自然产生的 1/2。与 restart.sh 取同一个 41，
# 让"401 未授权"在 scripts/ops/ 下是同一个数字。
PROBE_UNAUTHORIZED_STATUS=41

die() { printf '\n✗ %s\n' "${1}" >&2; shift; for line in "$@"; do printf '  %s\n' "${line}" >&2; done; exit 1; }
note() { printf '  %s\n' "${1}"; }
ok() { printf '  ✓ %s\n' "${1}"; }

# 工作台 API token 的取用与注入（含 argv 零暴露的理由），见该文件头部说明。
# 必须在 die/note 之后 source：库里报错走调用方的 die。
# shellcheck source=scripts/ops/ui_token.sh
. "${SCRIPT_DIR}/ui_token.sh"

command -v ssh >/dev/null 2>&1 || die "本机没有 ssh 命令"

# 取用在 SSH 之前完成：token 字符集不合法要在本机就报错退出。
# 未配置时这一步什么都不做，后续行为与改造前逐字一致。
sw_ops_load_ui_token

printf '生产 core 状态\n\n'
note "连接 ${SSH_ALIAS}（IAP 首包通常需 5-10 秒）"
sw_ops_note_ui_token

# 远端脚本正文。单独成一个函数，是为了让 ssh 的 stdin 能由三段拼接而成：
#   ① sw_ops_emit_token_prologue —— 一行 `export SW_OPS_UI_TOKEN=<%q 转义的值>`，
#      它落在下面那个 `{` 的**外面**（见 ui_token.sh 里那段"只许放内建命令"的警告）；
#   ② 这里的正文 —— 两段引号 heredoc，不做任何本地展开；
#   ③ 夹在两段之间的 sw_ops_emit_sw_probe_definition —— 远端 sw_probe 的唯一定义处，
#      发射进来的字节落在 `{` 的**里面**，位置与它替换掉的那份内联拷贝完全相同。
# token 只能走这条路：ssh 只转发 stdin 这一个通道，而 argv 是同机其他用户可读的。
status_remote_script() {
  cat <<'REMOTE_HEAD'
{
set -euo pipefail
# ！！【stdin 的结构性保证——改本段任何一行之前先读完这一段】
# 历史缺陷：这层 bash 的脚本正文曾经**就是它自己的 stdin**（本机把它拼成一条流送进 ssh：
# 上面的 REMOTE_HEAD、中间发射进来的 sw_probe 定义、下面的 REMOTE_TAIL）。
# 任何读 stdin 的子进程都会把"脚本剩下的部分"吞掉，而脚本仍以 0 收尾——
# `docker compose exec -T` 真的会这么干（`-T` 只关 TTY，不关 stdin 转发，这正是
# `docker compose exec -T db psql < dump.sql` 能工作的原因）。
#
# 现在这个缺陷被**结构性**堵死，靠的是包住整段正文的那对花括号和它尾部的 `} </dev/null`：
#   ① `{ ... }` 是一条复合命令。Bash 必须把它**整条解析完**才能开始执行，所以正文在第一条
#      命令跑起来之前就已经全部读进内存了——此后它不再"在流里"，谁也吞不掉它。
#   ② `</dev/null` 挂在整个组上，组内每一条命令（以及它们的子进程）继承的 fd 0 就是
#      /dev/null，读 stdin 只会立刻拿到 EOF。
# 这不依赖任何平台特性，只用 POSIX shell 语法。`exec 0</dev/null` 这类写法在这里**不可用**：
# 实测（bash 3.2 与 5.x 一致）它会连 bash 自己的脚本输入一起改掉，脚本从该行起直接结束。
#
# 【下面那些 `</dev/null` 现在算什么】它们从"承重"降级为**纵深防御**，但一条都不删——
# 包括 `docker compose ps` / `df` 这种"看起来不读 stdin"的：真实命令不读，但本脚本自己声明了
# 这条不变量就不留例外。反过来也**不要**因为"反正有 `</dev/null`"就删掉花括号与
# `} </dev/null`：两层各守一半。verify.sh / update.sh / restart.sh 是同一套口径。
probe_unauthorized_status="$1"

# ---- 工作台 API 探针（带鉴权；token 一个字符都不进 argv）------------------------
# 【定义不在本文件里】紧跟这段注释的 sw_probe 定义由本机的
# sw_ops_emit_sw_probe_definition 发射进这条脚本流，**唯一定义处是 scripts/ops/ui_token.sh**。
# 从前这里是四份逐字相同的内联拷贝之一（verify/update/restart/status 各一份），靠
# tests/ops/test_update.sh 的一条逐字比对断言维持同步；收成一份之后，那条断言改成钉死
# "只有一处定义、没有任何脚本自己内联"。
# 发射进来的字节落在本段最外面那个 `{` 的**里面**，与内联拷贝时的位置完全相同，所以
# 上面那道 `{ ... } </dev/null` 的结构性保证一字未变（重新论证见 ui_token.sh 该函数上方）。
# 理由见 scripts/ops/ui_token.sh 头部：token 经 `curl --config -` 的配置流注入，curl 的 argv
# 里只有 `--config -`。/proc/*/cmdline 对同机其他用户可读，而这台是合租机器
# （docs/RISKS.md §8.2）。判定语义与改造前逐字相同（仍是 `-f`，URL 与超时一字未改）；
# 只多一个 `-w`，把状态码追加到响应体末尾，好把 401 与"连不上/超时/500"分开。
REMOTE_HEAD
  sw_ops_emit_sw_probe_definition
  cat <<'REMOTE_TAIL'

cd "${HOME}/social_workflow"

printf '\nCompose 服务\n'
docker compose ps </dev/null

printf '\nCore 探针\n'
# 【401 在本脚本里为什么是"降级"而不是"整体失败"——这条与另外三个脚本刻意不同，先读理由】
# status.sh 是**纯只读查看工具**：它没有裁定语义、没有 verdicts、没有"通过/失败"结论行。
# 门禁与取证是 verify.sh 的职责。它的四段里只有这一段要鉴权，另外三段（Compose 服务 /
# 磁盘水位 / 数据卷文件）与 token 毫无关系。
# 401 的含义非常确定：**core 活着、正常应答了，只是本机没有匹配的凭据**。这种情况下把另外
# 三段一并掐掉，是让一个与它们无关的条件决定它们的可见性——没有道理。所以 401 只让
# **这一段**降级，其余三段照常打印，最后再以 41 收尾（外层据此打一条说清根因的失败行）。
# 降级绝不等于静默：下面会打印明确的 401 告警与可行动提示。
#
# 【降级只对 401，别把它想成"core 挂了也还能看磁盘"】这一点必须写清楚，免得下一个人带着错误
# 预期在真出事那天操作：**core 真挂了（磁盘撑满、OOM、容器没起来）时探针拿到的不是 401**，
# 而是连接拒绝 / 超时（curl rc 7 / 28），那条路按下面的设计**保持原地中止**——`磁盘水位` 与
# `数据卷文件` 两段一个字都不会打。想在 core 已死的情况下看磁盘，请直接走 SSH 或别的路径，
# 不要指望本脚本。
# 之所以只给 401 开这个口子：它是唯一**已知、良性、且能被精确识别**的失败（核对 http 状态码
# 就够了）。连不上 / 超时 / 5xx 都是**未知**情形，生产可能真有问题，把它们也降级成"看起来
# 只是少了一段"是危险方向；而且保持它们的既有语义本来就是向后兼容的硬要求，本轮不动。
probe_status=0
sw_probe 'http://127.0.0.1:8000/api/v1/system/info' 10 >/dev/null || probe_status=$?
auth_blocked=0
if [[ "${probe_status}" -ne 0 && "${sw_probe_code}" == "401" ]]; then
  auth_blocked=1
  printf '  <GET /api/v1/system/info 返回 401 未授权：本段跳过，其余段落照常>\n'
  printf '  根因  core 已启用 SW_UI_TOKEN 鉴权（core/api/common.py::require_token，对除 /auth/login 外的全部 /api/v1/* 生效），\n'
  printf '        本次探针没带上匹配的 token。这不是部署故障，也不是 core 运行时故障——core 正常应答了 401。\n'
  printf '  处置  在值班工作站二选一，然后重跑本脚本：\n'
  printf '        ① export SW_OPS_UI_TOKEN=<生产 .env 里 SW_UI_TOKEN 的同一个值>\n'
  printf '        ② 在 ~/.dsh-sw/.credentials.yaml（0600）里加一行：sw_ui_token: <同一个值>\n'
  printf '  自查  已经配了还是 401 = 值不匹配。以生产 .env 里 SW_UI_TOKEN 的原文为准，别把引号或行尾空格一起复制。\n'
  printf '  出处  docs/RISKS.md 第 8 条、scripts/ops/README.md「工作台 API token」\n'
else
  # 成功、以及**非 401** 的失败，都走这一条：与改造前逐字一致。非 401 失败时 sw_probe_body
  # 是空的，容器内 json.load 会抛异常，管道在 set -euo pipefail 下把脚本原地中止——
  # 与改造前 `curl … | docker compose exec …` 的可观察后果（中止、退出码 1）相同。
  printf '%s' "${sw_probe_body}" | docker compose exec -T core python3 -c '
import json
import sys

payload = json.load(sys.stdin)
if not payload.get("ok"):
    raise SystemExit("/api/v1/system/info 返回失败外壳")
data = payload.get("data") or {}
fields = (
    ("版本", "version"),
    ("环境", "env"),
    ("服务时间", "time"),
    ("时区", "timezone"),
    ("调度器", "scheduler_enabled"),
    ("生成开关", "generate_enabled"),
    ("模拟发布器", "use_fake_publishers"),
    ("鉴权", "auth_required"),
)
for label, key in fields:
    print("  {}  {}".format(label, data.get(key, "<缺失>")))
publishers = data.get("publishers") or []
print("  已注册发布器  {}".format(", ".join(str(item) for item in publishers) or "<无>"))
'
fi

printf '\n磁盘水位\n'
df -h "${HOME}/social_workflow" </dev/null

printf '\n数据卷文件\n'
docker compose exec -T core python3 - <<'PY'
from pathlib import Path

root = Path("/app/data")
if not root.is_dir():
    raise SystemExit("/app/data 不存在或不是目录")

files = sorted(path for path in root.rglob("*") if path.is_file())
if not files:
    print("  <空>")
for path in files:
    print("  {}  {} bytes".format(path.relative_to(root), path.stat().st_size))
PY

# 防回归哨兵：这里必须打出一行，它排在本段所有读 stdin 的边界命令之后；一旦消失，就说明某条
# 命令又把脚本正文吞了——那时后面两段等于没跑，脚本却仍会以 0 收尾。测试直接断言它。
# **措辞必须与实际发生的事一致**：401 降级路径上 Core 探针那一段一个字节都没产出，实际只跑成
# 了三段，那时打"四段读取完毕"就是一句字面意思与事实相反的话。本项目反复栽在这一类上
# （"打印'探针均通过'但探针实际被跳过"），哨兵的技术目的不构成说假话的理由，所以分支化。
if [[ "${auth_blocked}" -eq 1 ]]; then
  printf '\n三段读取完毕（Core 探针段因 401 未取到，见上）\n'
  exit "${probe_unauthorized_status}"
fi
printf '\n四段读取完毕\n'
exit 0
} </dev/null
REMOTE_TAIL
}

# ssh(1) 不保留 argv 边界：host 之后的参数会被用单个空格拼成一个字符串发给远端，再由远端
# 登录 shell 重新分词。所以自己造那一个字符串并用 printf '%q' 转义——verify.sh / update.sh /
# restart.sh 是同一手法。注入面为零：这个值是本脚本自己定义的十进制常量。
# token 绝不走这条路：它在 stdin 流里（见 status_remote_script 上方说明）。
#
# stdin 用**进程替换**而不是管道喂：`… | ssh …` 在 `set -o pipefail` 下会让写端的 SIGPIPE
# 有机会顶掉 ssh 自己的退出码，而下面要按退出码分派 401 协议。
status_remote() {
  ssh -o ConnectTimeout=25 "${SSH_ALIAS}" "bash -s -- $(printf '%q ' "${PROBE_UNAUTHORIZED_STATUS}")" \
    < <(sw_ops_emit_token_prologue; status_remote_script)
}

status_rc=0
status_remote || status_rc=$?

if [[ "${status_rc}" -eq "${PROBE_UNAUTHORIZED_STATUS}" ]]; then
  die "Core 探针段未取到：401 未授权（core 已启用 SW_UI_TOKEN，本机未提供或不匹配）" \
    "其余三段（Compose 服务 / 磁盘水位 / 数据卷文件）已在上面完整打印——它们与鉴权无关，没有被这条 401 连坐。" \
    "这不是生产故障：core 正常应答了 401，缺的是运维侧凭据。" \
    "处置：export SW_OPS_UI_TOKEN=<生产 .env 里 SW_UI_TOKEN 的同一个值>，或写进 ~/.dsh-sw/.credentials.yaml（0600）的 sw_ui_token 键，然后重跑本脚本。" \
    "出处：docs/RISKS.md 第 8 条、scripts/ops/README.md「工作台 API token」。"
fi
# 其它非零退出保持改造前语义：ssh 或远端怎么失败的就怎么传出去，不加解释也不改码。
[[ "${status_rc}" -eq 0 ]] || exit "${status_rc}"

ok "状态读取完成"
