#!/usr/bin/env bash
# 用途：一次 SSH 往返只读采集生产 core 的部署证据（git HEAD、端口门禁、健康探针、
#       人工确认闸门通道、待人点的确认卡条数、Telegram 409），零副作用。
set -euo pipefail

EXPECTED_SHA=""
SHA_COUNT=0
RUN_PREFLIGHT=0
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH_ALIAS="${SW_OPS_SSH_ALIAS:-${SW_TUNNEL_SSH_ALIAS:-workbench-iap}}"

die() { printf '\n✗ %s\n' "${1}" >&2; shift; for line in "$@"; do printf '  %s\n' "${line}" >&2; done; exit 1; }
note() { printf '  %s\n' "${1}"; }
ok() { printf '  ✓ %s\n' "${1}"; }

# 工作台 API token 的取用与注入（含 argv 零暴露的理由），见该文件头部说明。
# 必须在 die/note 之后 source：库里报错走调用方的 die。
# shellcheck source=scripts/ops/ui_token.sh
. "${SCRIPT_DIR}/ui_token.sh"

usage() { cat >&2 <<'USAGE'
用法：bash scripts/ops/verify.sh [--sha <40位小写SHA>] [--preflight]

纯只读部署核验：不备份、不 fetch、不构建、不重启，可重复运行、无副作用。
  --sha        要求生产 HEAD 严格等于该 40 位小写完整提交；不给则只如实打印 HEAD。
  --preflight  额外在容器内执行 scripts/preflight.py（含外部连通性探测，默认不跑）。
USAGE
}

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --sha)
      [[ "$#" -ge 2 && "$2" != --* ]] || die "--sha 缺少 SHA"
      SHA_COUNT=$((SHA_COUNT + 1))
      [[ "${SHA_COUNT}" -eq 1 ]] || die "--sha 只能指定一次"
      EXPECTED_SHA="$2"
      shift
      ;;
    --preflight)
      RUN_PREFLIGHT=1
      ;;
    -h|--help) usage; exit 0 ;;
    *) usage; die "参数无效：$1" ;;
  esac
  shift
done

# 参数校验全部在本机、在 SSH 之前完成。参数绝不原样拼入 shell 文本：见下方 verify_remote，
# 一律先经 printf '%q' 转义再交给远端 shell 重新分词，远端只当位置参数用。
if [[ "${SHA_COUNT}" -eq 1 ]]; then
  [[ "${EXPECTED_SHA}" =~ ^[0-9a-f]{40}$ ]] || die "SHA 必须是 40 位小写十六进制完整提交：${EXPECTED_SHA}"
fi

command -v ssh >/dev/null 2>&1 || die "本机没有 ssh 命令"

# 取用在 SSH 之前完成：token 字符集不合法要在本机就报错退出，绝不带着一个会破坏 curl
# 配置语法的值去连生产。未配置时这一步什么都不做，后续行为与改造前逐字一致。
sw_ops_load_ui_token

printf '生产部署核验\n\n'
note "连接 ${SSH_ALIAS}（IAP 首包通常需 5-10 秒）"
sw_ops_note_ui_token
note "只读模式：不调用 backup.sh，不 fetch/pull/merge，不 build/up/restart，不写远端文件"
if [[ "${RUN_PREFLIGHT}" -eq 1 ]]; then
  note "已显式开启容器内 preflight 门禁（含外部连通性探测，可能耗时）"
else
  note "未开启容器内 preflight（需显式 --preflight）"
fi

# 远端脚本正文。单独成一个函数，是为了让 ssh 的 stdin 能由三段拼接而成：
#   ① sw_ops_emit_token_prologue —— 一行 `export SW_OPS_UI_TOKEN=<%q 转义的值>`，
#      它落在下面那个外层 `{` 的**外面**（见 ui_token.sh 里那段"只许放内建命令"的警告）；
#   ② 这里的正文 —— 两段引号 heredoc，不做任何本地展开；
#   ③ 夹在两段之间的两个发射函数 —— sw_ops_emit_sw_probe_definition（远端 sw_probe 的唯一
#      定义处）与 sw_ops_emit_awaiting_confirm_definition（远端"待人点的确认卡条数"读数的
#      唯一定义处，它调用 sw_probe，所以必须排在后面）。发射进来的字节落在**内层**那对花括号
#      里，位置与它们替换掉的那些内联拷贝完全相同。
# token 只能走这条路：ssh 只转发 stdin 这一个通道，而 argv 是同机其他用户可读的。
verify_remote_script() {
  cat <<'REMOTE_HEAD'
{
set -uo pipefail
# 核验跑在独立的 Bash 里，errexit 契约与这层状态规范化包装互不影响。ssh 用 255 表示
# 传输中断，所以远端脚本自身的 255 必须先改写成别的非零码，避免与断链混淆。
remote_status=0
bash -s -- "$@" <<'REMOTE_VERIFY' || remote_status=$?
{
set -uo pipefail
# ！！【stdin 的结构性保证——改本段任何一行之前先读完这一段】
# 历史缺陷：这层 bash 的脚本正文曾经**就是它自己的 stdin**（正上方那个 REMOTE_VERIFY
# heredoc；本机把它拼成一条流送进 ssh——REMOTE_HEAD + 发射进来的 sw_probe 定义 +
# REMOTE_TAIL）。任何在本段里被调用、又会读 stdin 的子进程，都会把"脚本剩下的部分"当成自己的
# 输入吞掉：后面的检查、「核验结论」段和 exit 判定随之全部消失，脚本以 0 收尾——取证工具
# 在它最严格的模式下反而变得不可能失败。生产已实证：
# `docker compose exec -T core python3 scripts/preflight.py` 一步就吞掉了整段结论
# （`-T` 只是不分配 TTY，**仍然转发 stdin**，这正是
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
# 【下面那些 `</dev/null` 现在算什么】它们从"承重"降级为**纵深防御**，但一条都不删：
#   · 它们让每条边界命令的 stdin 来源在阅读时是自明的，不必回头翻这段说明；
#   · 万一有人拆掉外面这对花括号，或把某一段搬进一个没有这层保护的新上下文，它们是第二道
#     防线；
#   · tests/ops/test_update.sh 的静态扫描仍然钉着它们。
# 反过来同样成立：**不要**因为"反正有 `</dev/null`"就删掉花括号与 `} </dev/null`，
# 也**不要**因为"反正有花括号"就删掉那些 `</dev/null`。两层各守一半，缺一层就退回到靠人工
# 纪律维持的脆弱不变量。
expected_sha="$1"; run_preflight="$2"

# ---- 工作台 API 探针（带鉴权；token 一个字符都不进 argv）------------------------
# 【定义不在本文件里】紧跟这段注释的 sw_probe 定义由本机的
# sw_ops_emit_sw_probe_definition 发射进这条脚本流，**唯一定义处是 scripts/ops/ui_token.sh**。
# 从前这里是四份逐字相同的内联拷贝之一（verify/update/restart/status 各一份），靠
# tests/ops/test_update.sh 的一条逐字比对断言维持同步；收成一份之后，那条断言改成钉死
# "只有一处定义、没有任何脚本自己内联"。
# 发射进来的字节落在**内层**那对花括号的里面，与内联拷贝时的位置完全相同，所以本段开头
# 那道 `{ ... } </dev/null` 的结构性保证一字未变（重新论证见 ui_token.sh 该函数上方）。
# token 由本机经 ssh 的脚本流注入到 SW_OPS_UI_TOKEN（理由见 scripts/ops/ui_token.sh 头部）。
# 它用 `curl --config -` 从 **stdin** 读配置：带 token 的是 `header = "..."` 那一行，
# 而 curl 的 argv 里只有 `--config -`。/proc/*/cmdline 对同机其他用户可读，而这台是合租机器
# （docs/RISKS.md §8.2），所以 token 绝不能进 argv。printf 是 bash 内建，不 fork、不产生
# 自己的 cmdline；管道本身也是显式的 stdin 来源，符合上面那条不变量。
# 判定语义与改造前逐字相同：仍然是 `-f`（HTTP >= 400 仍退 22），URL 与超时一字未改；
# 只多了一个 `-w`，把状态码追加到响应体末尾——`-f` 在 4xx 时不输出响应体，光看退出码 22
# 分不出"401 未授权"与"500 挂了"，而这两者的处置完全不同（docs/RISKS.md §8.4）。
REMOTE_HEAD
  sw_ops_emit_sw_probe_definition
  sw_ops_emit_awaiting_confirm_definition
  cat <<'REMOTE_TAIL'
# 401 既不是部署故障也不是运行时故障，而是运维侧凭据没配好。docs/RISKS.md §8.4 点名了这个
# 误判风险，所以这里给的是"去哪里配、怎么自查"，不是一句"拿不到"。一次运行只详述一遍。
sw_auth_help_shown=0
sw_print_auth_help() {
  if [[ "${sw_auth_help_shown}" -eq 1 ]]; then
    printf '  处置  同上一条 401 的说明。\n'
    return 0
  fi
  sw_auth_help_shown=1
  printf '  根因  core 已启用 SW_UI_TOKEN 鉴权（core/api/common.py::require_token，对除 /auth/login 外的全部 /api/v1/* 生效），\n'
  printf '        而本次探针没带上匹配的 token。这不是部署故障，也不是 core 运行时故障——core 正常应答了 401。\n'
  printf '  处置  在值班工作站二选一，然后重跑本脚本：\n'
  printf '        ① export SW_OPS_UI_TOKEN=<生产 .env 里 SW_UI_TOKEN 的同一个值>\n'
  printf '        ② 在 ~/.dsh-sw/.credentials.yaml（0600）里加一行：sw_ui_token: <同一个值>\n'
  printf '  自查  已经配了还是 401 = 值不匹配。以生产 .env 里 SW_UI_TOKEN 的原文为准，别把引号或行尾空格一起复制。\n'
  printf '  出处  docs/RISKS.md 第 8 条、scripts/ops/README.md「工作台 API token」\n'
}
repo_dir="${HOME}/social_workflow"
cd "${repo_dir}" || { printf '\n✗ 无法进入仓库目录：%s\n' "${repo_dir}" >&2; exit 3; }

# 取证脚本的价值在于一次拿全事实：所有检查先全部跑完，最后统一裁定，绝不第一个失败就中断。
failed=0
verdicts=()
gate_pass() { verdicts+=("  ✓ $1"); }
gate_fail() { verdicts+=("  ✗ $1"); failed=$((failed + 1)); }

printf '\nGit 部署核验\n'
branch="$(git symbolic-ref --quiet --short HEAD </dev/null)" || branch='<detached HEAD>'
head_sha=""
head_sha="$(git rev-parse --verify HEAD </dev/null)" || head_sha=""
printf '  当前分支  %s\n' "${branch}"
printf '  当前提交  %s\n' "${head_sha:-<未知>}"
if [[ -z "${head_sha}" ]]; then
  gate_fail "HEAD 可读  无法读取当前 HEAD"
else
  gate_pass "HEAD 可读  ${head_sha}"
fi

worktree_status=""
if worktree_status="$(git status --porcelain </dev/null)"; then
  if [[ -z "${worktree_status}" ]]; then
    printf '  工作树    干净\n'
    gate_pass "工作树干净"
  else
    printf '  工作树    有未提交改动：\n'
    printf '%s\n' "${worktree_status}" | sed 's/^/    /'
    gate_fail "工作树干净  存在未提交改动"
  fi
else
  printf '  工作树    <无法读取 git status>\n'
  gate_fail "工作树干净  无法读取 git status"
fi

if [[ -n "${expected_sha}" ]]; then
  if [[ -n "${head_sha}" && "${head_sha}" == "${expected_sha}" ]]; then
    printf '  期望 SHA  %s（一致）\n' "${expected_sha}"
    gate_pass "HEAD 等于期望 SHA  ${expected_sha}"
  else
    printf '  期望 SHA  %s（不一致）\n' "${expected_sha}"
    gate_fail "HEAD 等于期望 SHA  HEAD=${head_sha:-<未知>}，期望=${expected_sha}"
  fi
else
  printf '  期望 SHA  <未指定 --sha，跳过比对>\n'
fi

# ---- 发布线（事实）：HEAD 到底在哪条 origin 线上 --------------------------------
#
# 【为什么换掉从前那段「远端对比 origin/<本地分支名>」】它假设"本地分支名 = 远端分支名"，
# 而这台生产服务器上这个假设**是错的**：本地检出的分支名叫 `main`，但它的 HEAD 承载的是
# GitHub 上 `p14-organic` 的顶端——历史上都是 `update.sh --ref p14-organic --sha <SHA>`
# 部署、`git merge --ff-only` 把这个叫 main 的本地分支快进过去的（docs/RISKS.md 第 11 条）。
# 于是取证输出长这样：
#     远端对比  origin/main=fb9b656…
#               HEAD 领先 14 个提交，落后 0 个提交
# 每个字都为真，但它把 `origin/main` 摆成了参照系，读的人会得出「生产上跑着 14 个没推上去
# 的提交」这个**完全错误**的结论——实际生产与 `origin/p14-organic` 一字不差。第 11 条担心
# 的正是这种误判，而这段输出自己在制造它。取证工具不该需要读者先知道一条陷阱才能读懂。
#
# 【改成报告事实】用 `git branch -r --contains <HEAD>` 问 git：**HEAD 落在哪些 origin ref
# 的历史里**。命中多个就全列出来，绝不挑一个当"那条线"——挑哪一个都是替读的人做一次没有
# 依据的猜测，而那正是要修的毛病。再对每一条说清 HEAD 是**正好等于它的顶端**，还是在这条
# 线上但落后若干提交。
#
# 【方向别搞反】`--contains <commit>` 列的是"顶端的历史里包含这个 commit"的 ref，也就是
# HEAD 是它们的祖先、或就等于它们。所以对每条命中的 ref，HEAD 只可能"等于顶端"或"落后
# N 个提交"，**不可能领先**。上面那个陈旧的 `origin/main` 恰恰因为落在 HEAD 后面而根本
# 不会出现在列表里——这正是它当年不该被拿来当参照系的原因。
#
# 【只读原则】只用本地已有的 remote-tracking ref，绝不 fetch —— fetch 会改写本地 refs。
# 代价如实说：本地 remote-tracking ref 可能陈旧，所以"一条都没命中"**不等于**"这个提交不在
# 任何远端线上"，也可能只是本地 refs 太旧。
#
# 【本段刻意不产生任何裁定】既不 gate_pass 也不 gate_fail。它是事实陈述，不是门禁：上面
# 那条陈旧性使得任何一种结果都可能有良性解释，把它做成门禁只会制造另一类误判。要判"部署
# 的是不是这一版"，用 `--sha`——那才是有确定答案的问题。
contain_all=""
contain_count=0
# 【为什么要单独一个"清单本身可不可信"的标志】下面用这份清单去对照部署标记。"清单是空的"
# 与"根本没读到清单"是两件事：前者可以说"HEAD 不在那条线的历史里"，后者只能说"对不了"。
# 少了这个标志，读取失败时会打出一句听起来很确定、实际毫无依据的结论——本任务修的恰恰
# 就是这一类"字面为真、指向错误"的输出。
contain_known=0
if [[ -z "${head_sha}" ]]; then
  printf '  发布线    <HEAD 未知，无法判断它在哪条线上>\n'
else
  contain_raw=""
  if ! contain_raw="$(git branch -r --contains "${head_sha}" </dev/null 2>/dev/null)"; then
    printf '  发布线    <git branch -r --contains 读取失败>\n'
  else
    contain_known=1
    printf '  发布线    HEAD 被下列 origin ref 包含（本地 remote-tracking ref，未 fetch）：\n'
    # 用默认 IFS 分词：git 的两格缩进被 read 吃掉，`origin/HEAD -> origin/main` 这类符号
    # ref 会让第二个字段非空，据此跳过——它只是别名，不是一条独立的发布线。
    while read -r contain_ref contain_rest; do
      [[ -n "${contain_ref}" ]] || continue
      [[ -z "${contain_rest}" ]] || continue
      [[ "${contain_ref}" == origin/* ]] || continue
      contain_count=$((contain_count + 1))
      contain_all="${contain_all}${contain_ref} "
      contain_tip=""
      if ! contain_tip="$(git rev-parse --verify --quiet "refs/remotes/${contain_ref}" </dev/null)"; then
        printf '            %s  <顶端读取失败>\n' "${contain_ref}"
        continue
      fi
      if [[ "${contain_tip}" == "${head_sha}" ]]; then
        printf '            %s=%s  HEAD 正好等于它的顶端\n' "${contain_ref}" "${contain_tip}"
      else
        contain_behind="<未知>"
        contain_behind="$(git rev-list --count "${head_sha}..${contain_tip}" </dev/null)" || contain_behind="<未知>"
        printf '            %s=%s  HEAD 在这条线上，落后它 %s 个提交（落后不算失败）\n' \
          "${contain_ref}" "${contain_tip}" "${contain_behind}"
      fi
    done <<<"${contain_raw}"
    if [[ "${contain_count}" -eq 0 ]]; then
      printf '            <一条都没命中>\n'
      printf '            这不必然是坏消息：本脚本不 fetch，本地 remote-tracking ref 可能陈旧；\n'
      printf '            也可能这个提交确实还没推上去。要确认请在值班工作站上比对，不要在生产上 fetch。\n'
    fi
  fi
fi

# ---- 部署标记（意图）：上一次经本工具面部署的是哪条线 ----------------------------
#
# 【这两段是两种不同的东西，别混着读】
#   发布线（上面）   = **事实**：这个提交确实落在哪条 origin 线上，由 git 自己回答。
#   部署标记（这里） = **意图**：上一次 `update.sh --apply` 打算部署哪条线，由那次部署留下。
# 两者不一致本身就是值得看见的信号——有人手工 merge/pull 过、从别的线部署过、或者标记本身
# 就旧了。所以下面在能比的时候会明说一致还是不一致，并且**明说以哪一边为准**（事实那边），
# 但不替读的人下"所以出事了"这种结论。
#
# 【标记缺失是正常情形，不是失败】手工部署过、或者当前这一版早于本功能上线，都会没有标记。
# 那时如实说"没有记录"，不报错、也不猜。本段同样不产生任何裁定。
#
# 【为什么在 ~/sw-deploy-state 而不是仓库里】仓库目录是 git 工作树：放进去会让上面那道
# 「工作树干净」判失败、让 update.sh 拒绝部署；就算写进 .gitignore 也不行——.gitignore 是
# 被版本控制的，一次快进随时可能把它换掉。放在工作树外面，git 的任何操作都碰不到它。
# 下面这行字面量与 scripts/ops/update.sh 里写标记的那一处**必须逐字一致**，
# tests/ops/test_update.sh 有一条源码级断言比对两边。
deploy_marker_file="${HOME}/sw-deploy-state/last-deploy"
marker_ref=""
marker_sha=""
marker_at=""
marker_schema=""
marker_bad=0
if [[ ! -f "${deploy_marker_file}" ]]; then
  printf '  部署标记  <没有记录>（手工部署过，或这一版早于标记功能上线——正常情形，不是失败）\n'
else
  # 极窄解析，与本仓读凭据文件同一口径：只认下面四个键，形状对不上就说"读不懂"。
  # **绝不猜**——一条猜出来的"上次部署的是 X"比没有记录更坏。
  while IFS= read -r marker_line || [[ -n "${marker_line}" ]]; do
    case "${marker_line}" in
      schema=*) marker_schema="${marker_line#schema=}" ;;
      ref=*) marker_ref="${marker_line#ref=}" ;;
      sha=*) marker_sha="${marker_line#sha=}" ;;
      at=*) marker_at="${marker_line#at=}" ;;
      '') ;;
      *) marker_bad=1 ;;
    esac
  done <"${deploy_marker_file}"
  [[ "${marker_schema}" == "1" ]] || marker_bad=1
  [[ "${marker_sha}" =~ ^[0-9a-f]{40}$ ]] || marker_bad=1
  [[ "${marker_ref}" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] || marker_bad=1
  [[ "${marker_at}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] || marker_bad=1
  if [[ "${marker_bad}" -ne 0 ]]; then
    printf '  部署标记  <%s 的内容读不懂，按「没有记录」处理；本脚本不猜>\n' "${deploy_marker_file}"
    marker_ref=""
    marker_sha=""
  else
    printf '  部署标记  ref=%s  sha=%s  at=%s\n' "${marker_ref}" "${marker_sha}" "${marker_at}"
    printf '            （上次经 update.sh --apply 部署的发布线；这是**意图**，上面的发布线是**事实**）\n'
  fi
fi

if [[ -n "${marker_sha}" ]]; then
  if [[ -n "${head_sha}" && "${marker_sha}" == "${head_sha}" ]]; then
    printf '            对照  标记里的 sha 与当前 HEAD 一致\n'
  else
    printf '            对照  ⚠ 标记里的 sha 与当前 HEAD 不一致（HEAD=%s）：标记之后有人动过生产的 HEAD\n' \
      "${head_sha:-<未知>}"
    printf '                  （手工 merge/pull、或走了本工具面之外的部署路径）\n'
  fi
  # ref 一侧：把"意图"拿去和上面那份"事实"清单对照。标记里可能写 `p14-organic`，也可能写
  # 沿用 upstream 那条路的 `origin/main`，所以先归一成 `origin/<短名>` 再比。
  marker_origin_ref="origin/${marker_ref#origin/}"
  if [[ "${contain_known}" -ne 1 ]]; then
    printf '            对照  标记说部署的是 %s；上面那份发布线清单没读到，所以这一格**对不了**\n' \
      "${marker_origin_ref}"
    printf '                  （"没读到"不等于"不在那条线上"——不下结论）\n'
  else
    case " ${contain_all}" in
      *" ${marker_origin_ref} "*)
        printf '            对照  标记里的 %s 与 HEAD 实际所在的发布线一致\n' "${marker_origin_ref}" ;;
      *)
        printf '            对照  ⚠ 标记说部署的是 %s，但 HEAD 不在它的历史里（上面的发布线清单里没有它）\n' \
          "${marker_origin_ref}"
        printf '                  以 --contains 那份事实为准；标记只记录上一次部署的意图，也可能只是本地 remote-tracking ref 陈旧\n' ;;
    esac
  fi
fi

printf '\n容器与端口门禁\n'
if docker compose ps </dev/null; then
  gate_pass "Compose 服务可读"
else
  printf '  <docker compose ps 读取失败>\n'
  gate_fail "Compose 服务可读  docker compose ps 失败"
fi

port_mapping=""
published_port=""
probe_host=""
port_error=""
read_core_loopback_port() {
  local captured mapping port
  # 末尾哨兵让 command substitution 不吞掉 compose 输出中的换行，以便拒绝空行和多行。
  if ! captured="$(docker compose port core 8000 </dev/null && printf '\037')"; then
    port_error="无法读取 core 8000 的发布端口。"
    return 1
  fi
  if [[ "${captured}" != *$'\037' ]]; then
    port_error="无法完整读取 core 8000 的发布端口。"
    return 1
  fi
  captured="${captured%$'\037'}"
  [[ "${captured}" == *$'\n' ]] && captured="${captured%$'\n'}"
  mapping="${captured}"
  if [[ -z "${mapping}" || "${mapping}" == *$'\n'* || "${mapping}" == *$'\r'* ]]; then
    port_error="core 8000 端口必须是恰好一条 loopback 映射，拒绝映射：$(printf '%q' "${mapping}")"
    return 1
  fi
  if [[ ! "${mapping}" =~ ^(127\.0\.0\.1|\[::1\]):([1-9][0-9]{0,4})$ ]]; then
    port_error="core 8000 端口必须是规范的 loopback:port，拒绝映射：$(printf '%q' "${mapping}")"
    return 1
  fi
  port="${BASH_REMATCH[2]}"
  if [[ "${port}" -gt 65535 ]]; then
    port_error="core 8000 发布端口必须在 1..65535，拒绝映射：$(printf '%q' "${mapping}")"
    return 1
  fi
  port_mapping="${mapping}"
  published_port="${port}"
  if [[ "${mapping}" == \[::1\]:* ]]; then
    probe_host='[::1]'
  else
    probe_host='127.0.0.1'
  fi
  return 0
}
if read_core_loopback_port; then
  printf '  端口门禁  core:8000 -> loopback（%s）\n' "${port_mapping}"
  gate_pass "端口门禁  core:8000 -> ${port_mapping}"
else
  printf '  端口门禁  %s\n' "${port_error}"
  gate_fail "端口门禁  ${port_error}"
fi

# 下一节「人工确认闸门通道」要靠它决定裁定的松紧，所以这个布尔量必须跨节存活。
# 取不到就保持 <未知>：无法证明"什么都不会真发"时按真发布从严裁定。
fake_publishers="<未知>"

printf '\n健康与运行时探针\n'
if [[ -z "${probe_host}" ]]; then
  printf '  <跳过：端口门禁未通过，没有可信的 loopback 探测目标>\n'
  gate_fail "健康探针 GET /health 200  未执行（端口门禁未通过）"
  gate_fail "运行环境 env=prod  未执行（端口门禁未通过）"
else
  health_status=0
  # `/health` 注册在 app 根上、不过 /api/v1 的鉴权守卫（core/main.py::_register_routes），
  # 所以它跟 token 无关；仍然走同一个 sw_probe，是为了三条探针只有一套判定与一套 stdin 语义。
  sw_probe "http://${probe_host}:${published_port}/health" 10 >/dev/null || health_status=$?
  if [[ "${health_status}" -eq 0 ]]; then
    printf '  健康探针  GET /health 200\n'
    gate_pass "健康探针 GET /health 200"
  else
    printf '  健康探针  GET /health 未返回 200（curl 退出码 %s）\n' "${health_status}"
    gate_fail "健康探针 GET /health 200  curl 退出码 ${health_status}"
  fi

  info_json=""
  info_probe_status=0
  sw_probe "http://${probe_host}:${published_port}/api/v1/system/info" 10 >/dev/null || info_probe_status=$?
  if [[ "${info_probe_status}" -ne 0 ]]; then
    if [[ "${sw_probe_code}" == "401" ]]; then
      # §8.4 点名的误判风险就在这里：不区分的话，运维会把"本机没配 token"读成"生产挂了"。
      printf '  运行时信息  <GET /api/v1/system/info 返回 401 未授权>\n'
      sw_print_auth_help
      gate_fail "运行环境 env=prod  /api/v1/system/info 返回 401（core 已启用 SW_UI_TOKEN，本机未提供或不匹配）"
    else
      printf '  运行时信息  <无法获取 /api/v1/system/info>\n'
      gate_fail "运行环境 env=prod  无法获取 /api/v1/system/info"
    fi
  else
    info_json="${sw_probe_body}"
    info_status=0
    info_text="$(printf '%s' "${info_json}" | docker compose exec -T core python3 -c '
import json
import sys

raw = sys.stdin.read()
try:
    payload = json.loads(raw)
except ValueError:
    print("  运行时信息  <JSON 解析失败>")
    raise SystemExit(21)
if not isinstance(payload, dict) or not payload.get("ok"):
    print("  运行时信息  </api/v1/system/info 返回失败外壳>")
    raise SystemExit(21)
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
print("  裁定  模拟发布器={}：如实记录，本身不构成失败项（生产既定裁决）".format(data.get("use_fake_publishers", "<缺失>")))
print("  裁定  鉴权={}：如实记录，本身不构成失败项（生产既定裁决）".format(data.get("auth_required", "<缺失>")))
env = data.get("env")
if env != "prod":
    print("  裁定  环境必须是 prod，实际 {}".format(env if env is not None else "<缺失>"))
    raise SystemExit(20)
' 2>&1)" || info_status=$?
    printf '%s\n' "${info_text}"
    case "${info_status}" in
      0) gate_pass "运行环境 env=prod" ;;
      20) gate_fail "运行环境 env=prod  实际 env 不是 prod" ;;
      *) gate_fail "运行环境 env=prod  无法解析 /api/v1/system/info（退出码 ${info_status}）" ;;
    esac

    # 下一节的互锁只要 use_fake_publishers 的布尔量。刻意**不动**上面那段解析：
    # 它的三条既有裁定（env=prod / 模拟发布器如实记录 / 鉴权如实记录）必须原样保留，
    # 在它的退出码里再塞一个维度会把裁定矩阵搅成一团。这里另起一次极小解析，
    # 只用退出码把布尔量带出来：不打印任何东西，也不产生任何裁定。
    fake_status=0
    printf '%s' "${info_json}" | docker compose exec -T core python3 -c '
import json
import sys

try:
    payload = json.loads(sys.stdin.read())
    value = (payload.get("data") or {}).get("use_fake_publishers")
except Exception:
    raise SystemExit(11)
if value is True:
    raise SystemExit(0)
if value is False:
    raise SystemExit(10)
raise SystemExit(11)
' >/dev/null 2>&1 || fake_status=$?
    case "${fake_status}" in
      0) fake_publishers=true ;;
      10) fake_publishers=false ;;
      *) fake_publishers="<未知>" ;;
    esac
  fi
fi

printf '\n人工确认闸门通道（Telegram）\n'
# R1 红线：内容上线必须由人点一下才真发——在 Telegram 闸门消息上点，或在工作台点
# 「确认发布」（同一后端 core.confirm.confirm_item，见 SW-AGENT.md §2 R1）。Telegram 是
# 这道确认的**主载体**，所以这条通道不是"通知美化"。
#
# 通道死掉的真实后果**不是**"内容照样越权发出去"——恰恰相反，内容会**发不出去**：
#   core/scheduler.py:498-505  tick_scheduled_publish 里的**人工确认闸门**
#                              `confirm_required(policy) and item.confirmed_at is None`
#                              → stats["skipped_unconfirmed"] → continue，**跳过不发**
#                              叫名字不叫序数：早期文档里的"第五道"是"第 5 个被加进来的"
#                              （出处 docs/briefs/p12_brief_autopilot_telegram.md:24-26），
#                              执行顺序里它排第 4（core/scheduler.py:499 的内联注释 ④）。
#                              序数会随代码漂，
#                              行号也会——所以上面同时给了函数名与条件表达式做锚点。
#   core/accounts.py:216,313   confirm_required 默认 True，且只有显式 false 才关得掉
#   core/confirm.py:9-11       autopilot 只影响"自动批准"，不影响"发布前要人点"，无旁路
#   core/confirm.py:23-25      SW_CONFIRM_TTL_HOURS（默认 24）到点**自动驳回并通知**；
#                              一次都没推成功过则从 scheduled_at 起算
# 即：内容在排期处静默堆积 → TTL 到点被自动驳回 → **发布链路停摆**。三格分别对应：
# enabled=false（SW_TELEGRAM_ENABLED 关着，build_telegram_notifier() 直接返回 None，一条卡
# 都推不出去）、ready=false（token/chat_id 不全，卡推不出去）、polling=false（卡推得出去，
# 但人点了没有长轮询线程去收回调）。任何一格为假都必须是失败项，所以下面有一道互锁。
# 外加第四种：polling=true 但线程**假活**（活着却一次也没成功轮询过）——判据与它的边界
# 写在下面那段 python 里，见「轮询实况」与「唯一参与裁定的新判据」两块注释。
#
# 注意 R1 **并没有**因此失去载体：core/confirm.py:254-255 明写"没有 Telegram 不是
# 错误：工作台里的兜底确认按钮照样能用"。工作台那个载体不受 Telegram 影响。但它要求操作者
# **知道**主载体已经死了，所以这里必须是一道显式失败项，而不是静默降级。
#
# 反过来，当前生产挂着模拟发布器（use_fake_publishers=true）时什么都不会真发，
# 这时通道状态只如实记录、不构成失败项（与本脚本对"模拟发布器/鉴权"的既定裁决一致）。
#
# 探针 GET /api/v1/system/telegram（core/api/system.py::telegram_info）**不发任何网络请求**，
# 只读配置 + 本进程轮询线程状态，可以安全地放进取证路径。它返回的 TelegramOut 契约上
# **绝不含 token**（脱敏指纹也不给），其中 detail 是 ready=false 时"照着做"的一句话，
# 所以这里原样打印它——不打印才是坑人，人拿不到下一步该干什么。
if [[ -z "${probe_host}" ]]; then
  printf '  <跳过：端口门禁未通过，没有可信的 loopback 探测目标>\n'
  printf '  待人点的确认卡  <未取到：端口门禁未通过，没有可信的 loopback 探测目标。「未取到」不是「0 条」>\n'
  gate_fail "人工确认闸门通道 enabled+ready+polling  未执行（端口门禁未通过）"
else
  telegram_json=""
  telegram_status=0
  telegram_probe_status=0
  sw_probe "http://${probe_host}:${published_port}/api/v1/system/telegram" 10 >/dev/null || telegram_probe_status=$?
  if [[ "${telegram_probe_status}" -ne 0 ]]; then
    if [[ "${sw_probe_code}" == "401" ]]; then
      printf '  确认通道  <GET /api/v1/system/telegram 返回 401 未授权>\n'
      sw_print_auth_help
      telegram_status=31
    else
      printf '  确认通道  <无法获取 /api/v1/system/telegram>\n'
      telegram_status=30
    fi
  else
    telegram_json="${sw_probe_body}"
    # 与 system/info 那节同一手法：python 解析 JSON、以退出码回话，shell 只做码→裁定映射。
    telegram_text="$(printf '%s' "${telegram_json}" | docker compose exec -T core python3 -c '
import json
import re
import sys

raw = sys.stdin.read()
try:
    payload = json.loads(raw)
except ValueError:
    print("  确认通道  <JSON 解析失败>")
    raise SystemExit(22)
if not isinstance(payload, dict) or not payload.get("ok"):
    print("  确认通道  </api/v1/system/telegram 返回失败外壳>")
    raise SystemExit(22)
data = payload.get("data") or {}
fields = (
    ("总开关 enabled", "enabled"),
    ("已配 token configured", "configured"),
    ("可推送 ready", "ready"),
    ("已知会话 chat_configured", "chat_configured"),
    ("可签名 can_sign", "can_sign"),
    ("轮询线程 polling", "polling"),
    ("bot 用户名", "username"),
    ("本进程已推送", "sent"),
    ("本进程推送失败", "failed"),
)
for label, key in fields:
    print("  {}  {}".format(label, data.get(key, "<缺失>")))
# detail 是给人照着做的指引；TelegramOut 契约保证它不含 token，如实打印。
print("  指引 detail  {}".format(data.get("detail") or "<空：通道可用时为空>"))
# enabled 取自 TelegramOut 的契约字段（core/api/system.py::TelegramOut.enabled，
# 源头是 SW_TELEGRAM_ENABLED），必须**直接判**：ready 只看 token+chat_id，压根不看总开关。
# 靠 polling 去间接兜住"总开关关着"是在赌 poller 的启动条件恰好包含 enabled——
# 那是实现细节不是契约，哪天变了这层兜底会静默失效，而失效方向是危险方向。
enabled = data.get("enabled") is True
ready = data.get("ready") is True
polling = data.get("polling") is True

# ------------------------------------------------------------------ 轮询实况
# 【为什么必须有这一段】polling 那一格是 core/telegram.py:981 的
# bool(poller and poller.alive)，**只看线程活没活**。而 _loop（809-831）里 poll_once()
# 抛 TelegramError 时只做三件事：stats.errors += 1、记 last_error、退避 2s→120s
# （POLL_BACKOFF_MAX）后 continue —— **线程不会退出，它会无限重试**。于是 bot token 失效 /
# 被 Telegram 撤销 / 网络长期不通时轮询线程**假活**：polling 照报 true，实际一条 callback
# 都收不到，人点了确认卡按钮没有任何线程去收，而上面那道 enabled+ready+polling 互锁
# 照样给绿灯。判死活要用的两个字段（TelegramOut.stats / .last_error，源头
# core/telegram.py:983-984）API 早就返回了，只是此前没人读。
MISSING = object()
# last_error 是 TelegramError 的文案，**可能带上游 URL —— 而 URL 里就是 token**。
# core/telegram.py:262 的注释声称"只报方法名"，但 263 行把整个 httpx 异常（{exc}）插了
# 进去，且 api_base 可配；本文件「Telegram 轮询冲突」那节也已写明"Telegram 报错文本
# 可能带上游 URL，回显等于泄露 token"。所以这里**不信任契约**：按 token 形状主动打码
# （比 tests/ops/test_verify.sh::assert_no_token_shape 的判据更宽一位），压成一行再截断。


def redact(text):
    out = re.sub("[0-9]{5,}:[A-Za-z0-9_-]{20,}", "<已打码的 bot token>", text)
    out = " ".join(out.split())
    if len(out) > 200:
        out = out[:200] + "……（已截断）"
    return out


stats_fields = (
    ("轮询成功次数 stats.polls", "polls"),
    ("收到更新条数 stats.updates", "updates"),
    ("已处理回调 stats.handled", "handled"),
    ("拒绝的回调 stats.rejected", "rejected"),
    ("累计错误次数 stats.errors", "errors"),
)
raw_stats = data.get("stats", MISSING)
polls = None
errors = None
if raw_stats is MISSING:
    print("  轮询实况 stats  <未取到：这个 core 没返回 stats 字段（老版本 core）。「未取到」不是「0 次」>")
elif not isinstance(raw_stats, dict):
    print("  轮询实况 stats  <未取到：stats 不是对象（{}）。「未取到」不是「0 次」>".format(type(raw_stats).__name__))
elif not raw_stats:
    # channel_status() 没有 poller 时给 {}（core/telegram.py:983 的 if poller else {}）。
    # 那是"没有轮询线程对象"，不是"轮询了 0 次"——本仓口径里这两件事不许混。
    print("  轮询实况 stats  <无：本进程没有轮询线程对象，channel_status() 给出空 stats。「无」不是「0 次」>")
else:
    for label, key in stats_fields:
        value = raw_stats.get(key, MISSING)
        if value is MISSING:
            print("  {}  <缺失：这个 core 的 stats 里没有这一项>".format(label))
        elif isinstance(value, bool) or not isinstance(value, int):
            print("  {}  <类型不对：{}>".format(label, type(value).__name__))
        else:
            print("  {}  {}".format(label, value))
    polls = raw_stats.get("polls")
    errors = raw_stats.get("errors")
    if isinstance(polls, bool) or not isinstance(polls, int):
        polls = None
    if isinstance(errors, bool) or not isinstance(errors, int):
        errors = None
raw_last_error = data.get("last_error", MISSING)
if raw_last_error is MISSING:
    print("  最近一次错误 last_error  <未取到：这个 core 没返回 last_error 字段（老版本 core）。「未取到」不是「没出过错」>")
elif not isinstance(raw_last_error, str):
    print("  最近一次错误 last_error  <类型不对：{}>".format(type(raw_last_error).__name__))
elif not raw_last_error:
    print("  最近一次错误 last_error  <空：本进程启动以来没记到过错误>")
else:
    print("  最近一次错误 last_error  {}".format(redact(raw_last_error)))
print("  口径  last_error 与 stats.errors 都**只增不减**：core/telegram.py 只在 787/816/825 三处")
print("        写 last_error，poll_once()（835-852）成功时一行都没碰它；stats.errors 同样从不")
print("        清零，还混进了 handle_callback 的业务异常（915）。所以两者非空/非零只证明「本")
print("        进程启动以来出过错」，**不证明现在是坏的**——单凭它们判红，一次早上抖过的网络")
print("        会让此后每次核验都红，这种闸门很快就没人看了。故它们只报告，不参与裁定。")

# 【唯一参与裁定的新判据】stats.polls 只在 get_updates **成功返回之后**自增
# （core/telegram.py:838），且从不清零。所以 polls==0 等价于"本进程启动以来 getUpdates
# 一次都没成功过"，而且它**自愈**：成功一次就永远 >0，一次已恢复的抖动留不下假红。
# 再要求 errors>0，是为了排掉"进程刚起、首次 long polling 还没返回"那个窗口
# （poll_timeout 默认 30s）。errors 混了 handle_callback 的业务异常也不影响这条判据：
# 那条路径要先收到过回调，也就必然 polls>=1，够不到 polls==0。
# 【够不着的那一半，如实说】"先健康跑了几天、然后 token 被撤销"这一类，polls 会冻在一个大数
# 上，单次快照里与"健康 + 历史抖动过几次"**无法区分**：契约里没有"上次成功轮询的时刻"
# （started_at 在 core/telegram.py:767/782 有，但 channel_status 没导出），而 R4 禁止改 core。
# 采两次样求差值也不行：空闲时 polls 每 poll_timeout 秒才跳一次，poll_timeout 可配
# （SW_TELEGRAM_POLL_TIMEOUT_SECONDS）且同样不在契约里——那等于让本脚本对一个未知量猜等待
# 时长，猜短了就是新造出来的假红，正是上面要躲的那个坑。所以这一半留白，不假装能测。
dead_poller = polling and polls == 0 and (errors or 0) > 0
if not polling:
    print("  假活判据  未评估：polling 已经是 false，不必再问它是不是假活")
elif polls is None:
    print("  假活判据  未评估：取不到 stats.polls，无法判断轮询有没有成功推进过（取不到不是证据，不因此判红）")
elif dead_poller:
    print("  假活判据  **命中**：polling=true 但 stats.polls=0——轮询线程活着，本进程启动以来")
    print("        一次 getUpdates 都没成功过（stats.polls 只在 get_updates 成功返回后自增，")
    print("        core/telegram.py:838），且已失败 {} 次。人点了确认卡按钮，没有线程能收到那次回调。".format(errors))
    # 唯一一种会误伤的情形，直说出来并给出零成本的排除法：core 刚重建完、首次 long polling
    # 还没返回（poll_timeout 默认 30s）而期间恰好失败过一次，也是 polls=0 + errors>0。
    # 本脚本纯只读、可重复跑，所以"等一分钟重跑"就能把这两者分开——不是让人猜，是让人验。
    print("        排除法  刚 up -d --force-recreate 过 core 的话，等一分钟重跑本脚本：真活的轮询")
    print("                会把 stats.polls 顶上去；仍是 0 就不是启动窗口，是通道真的坏了。")
    print("        先查    bot 用户名那一格空 = 连启动时的 getMe 都没成功（core/telegram.py:784），")
    print("                八成是 TELEGRAM_BOT_TOKEN 失效或被撤销；再看 docker compose logs core。")
elif polls == 0:
    print("  假活判据  未命中：stats.polls=0 但 stats.errors 为 0 或取不到，可能只是进程刚起、首次")
    print("        long polling 还没返回（poll_timeout 默认 30s），证据不足以判死")
else:
    print("  假活判据  未命中：stats.polls={} > 0，本进程确实成功轮询过".format(polls))
    print("        注：单次快照看不出「先好后坏」（契约里没有上次成功轮询的时刻），这一半测不了，不假装能测")

# 裁定与改造前逐一等价，只多了 dead_poller 这一条：原式是
# (enabled and ready and polling)→0 / not enabled→25 / 23 if not ready else 24，
# 展开成下面的先后判断后，那四种组合拿到的码一个都没变。
if not enabled:
    raise SystemExit(25)
if not ready:
    raise SystemExit(23)
if not polling:
    raise SystemExit(24)
if dead_poller:
    raise SystemExit(26)
raise SystemExit(0)
' 2>&1)" || telegram_status=$?
    printf '%s\n' "${telegram_text}"
  fi

  case "${telegram_status}" in
    0) telegram_summary="ready=true polling=true" ;;
    22) telegram_summary="无法解析 /api/v1/system/telegram" ;;
    23) telegram_summary="ready=false（那张要点的卡片根本推不出去）" ;;
    24) telegram_summary="ready=true 但 polling=false（卡片能推出去，人点了没有线程去收）" ;;
    25) telegram_summary="总开关 enabled=false（SW_TELEGRAM_ENABLED 关着，build_telegram_notifier() 直接返回 None，一条都发不出去）" ;;
    # 26 是本轮新增的一格：polling=true 却是**假活**。判据、自愈性、以及它够不着的那一半，
    # 都写在上面那段 python 的「唯一参与裁定的新判据」注释里，这里不重复。
    26) telegram_summary="polling=true 但轮询假活（stats.polls=0：线程活着，本进程启动以来一次 getUpdates 都没成功过；卡片能推出去，人点了收不回来）" ;;
    30) telegram_summary="无法获取 /api/v1/system/telegram" ;;
    31) telegram_summary="/api/v1/system/telegram 返回 401（core 已启用 SW_UI_TOKEN，本机未提供或不匹配）" ;;
    *) telegram_summary="/api/v1/system/telegram 解析异常（退出码 ${telegram_status}）" ;;
  esac

  # 互锁：松紧完全由 use_fake_publishers 决定。
  if [[ "${fake_publishers}" == "true" ]]; then
    printf '  裁定  模拟发布器=true：确认通道 %s，如实记录，本身不构成失败项（生产既定裁决）\n' \
      "${telegram_summary}"
    gate_pass "人工确认闸门通道 enabled+ready+polling  模拟发布器=true：如实记录 ${telegram_summary}，不构成失败项"
  else
    if [[ "${fake_publishers}" == "false" ]]; then
      strict_why="真发布已开启（模拟发布器=false）"
    else
      strict_why="模拟发布器状态取不到（use_fake_publishers=${fake_publishers}），按真发布从严裁定"
    fi
    if [[ "${telegram_status}" -eq 0 ]]; then
      printf '  裁定  %s：确认通道 %s\n' "${strict_why}" "${telegram_summary}"
      gate_pass "人工确认闸门通道 enabled+ready+polling  ${strict_why}，通道 ${telegram_summary}"
    else
      printf '  裁定  %s：确认通道 %s\n' "${strict_why}" "${telegram_summary}"
      printf '        后果  确认卡推不出去或回调收不回来 → 内容在排期处堆积（scheduler 记 skipped_unconfirmed）→ SW_CONFIRM_TTL_HOURS 到点自动驳回，发布链路停摆\n'
      printf '        兜底  工作台的「确认发布」按钮不受 Telegram 影响，仍可用于确认（同一后端 core.confirm.confirm_item）\n'
      gate_fail "人工确认闸门通道 enabled+ready+polling  ${strict_why}，但 ${telegram_summary}"
    fi
  fi

  # ---- 待人点的确认卡条数（把 docs/RISKS.md §8.5 的「第 0 步」变成真能执行的检查）------
  # 【为什么必须有这一格】§8.5 给"换签名密钥"定了一条前置：**先确认没有待人点的确认卡**。
  # 生产 .env 里 SW_TELEGRAM_SIGNING_SECRET 为空时，确认卡 callback_data 的签名密钥会回落到
  # SW_UI_TOKEN（core/telegram.py::load_config 的 专用 → SW_UI_TOKEN → bot token 三级回落）。
  # 换 SW_UI_TOKEN 等于换签名密钥，**已推出去还没人点的卡**按下去会验签失败（日志
  # bad_signature，用户侧表现为按钮没反应），最终被 TTL 自动驳回。
  # 在这一格出现之前，scripts/ops 下**没有任何脚本**能回答"现在到底有没有"：
  #   · status.sh 只读 /api/v1/system/info，而 core/api/system.py::system_info 返回的
  #     SystemInfo 里一个队列计数都没有——只有 content_statuses / review_queue_statuses
  #     这两个**状态名字的列表**，不是条数；
  #   · 本段上面那几格只有通道的 enabled/ready/polling/can_sign，同样没有条目数；
  #   · 红线 R3 不允许手工 ssh 上去查库。
  # 一条无法执行的前置检查等于没有前置检查（编排方本人执行 --generate 时就没能真正验证它），
  # 所以把它落成这里的一个只读读数。
  #
  # 【读数本身不在本文件里，这一段只负责渲染】探针 + 解析那一整套是
  # scripts/ops/ui_token.sh 的 sw_ops_emit_awaiting_confirm_definition 发射进来的
  # `sw_awaiting_confirm`，**唯一定义处在那里**。本文件与 scripts/ops/env_set.sh 是它的两个
  # 使用方：这里把它渲染成给人看的取证行，那里拿它做闸门判定。
  # 两边共用一份读数是刻意的——两份实现会立刻分叉成"取证说 0 条、闸门说有卡"。
  # 端点选择、上界口径、"没取到 ≠ 0 条"的理由都写在那个发射函数上方，这里不重复。
  #
  # 【本段刻意不产生任何裁定】既不 gate_pass 也不 gate_fail。有卡等着人点**不是故障**，
  # 是正常运营状态，写法与上面「模拟发布器/鉴权：如实记录，本身不构成失败项」一致。
  # 顺带保住「核验结论」的裁定条数不变，tests/ops/test_verify.sh 的
  # assert_conclusion_verdicts 不会因为本段而漂移。
  #
  # 【降级必须把"没取到"和"0 条"分开】401 / 404（老版本 core 没这个端点）/ 其它传输失败 /
  # JSON 解析失败 / 字段缺失或类型不对 / 解析那一步自己没跑起来，每一种都由 sw_awaiting_confirm
  # 给出一句自己的原因，这里一律渲染成"未取到"，绝不渲染成"0 条"。
  # days=1：awaiting_confirm 由 _awaiting_confirm(session) 单独算，与统计窗口无关，
  # 把窗口收到最小只是为了少让生产跑一遍 7 天聚合。老版本 core 不认这个参数也无妨——
  # FastAPI 默认忽略多余的查询参数。超时给 20 秒：这个端点比 system/* 重（要跑 build_dashboard）。
  if sw_awaiting_confirm "http://${probe_host}:${published_port}/api/v1/dashboard?days=1" 20; then
    printf '  待人点的确认卡  %s 条\n' "${sw_awaiting_count}"
    printf '  口径  counters.awaiting_confirm（core/api/dashboard.py::_awaiting_confirm）：status=scheduled\n'
    printf '        且 confirmed_at 为空、且该账号策略 confirm_required=true 的条数。它**不看**\n'
    printf '        confirm_pushed_at，所以对「换签名密钥会搞坏几条」而言这是个**上界**：还没推出卡的\n'
    printf '        条目也计在里面，而它们的卡是换密钥之后才生成的、签的是新密钥，不会失效。\n'
    printf '  裁定  待人点的确认卡 %s 条：如实记录，本身不构成失败项（有人等着点是正常运营状态，不是故障）\n' \
      "${sw_awaiting_count}"
    if [[ "${sw_awaiting_count}" -gt 0 ]]; then
      printf '  提醒  改 SW_UI_TOKEN 前先看这一格（docs/RISKS.md §8.5 第 0 步）：生产 .env 里\n'
      printf '        SW_TELEGRAM_SIGNING_SECRET 为空时，确认卡 callback_data 的签名密钥回落到 SW_UI_TOKEN\n'
      printf '        （core/telegram.py::load_config）。换 token 等于换密钥，这 %s 条里**已经推出卡**的\n' \
        "${sw_awaiting_count}"
      printf '        那部分按下去会验签失败（日志 bad_signature，用户侧表现为按钮没反应），最终被 TTL 自动驳回。\n'
      printf '        scripts/ops/env_set.sh 现在把这条前置做成了闸门：改 SW_UI_TOKEN /\n'
      printf '        SW_TELEGRAM_SIGNING_SECRET 时它会自己读这一格，有卡就拒绝写入。\n'
    fi
  else
    printf '  待人点的确认卡  <未取到：%s。「未取到」不是「0 条」>\n' "${sw_awaiting_reason}"
    # 401 那一格与本文件其余两条探针同一处置：不是部署故障，是运维侧凭据没配好。
    if [[ "${sw_awaiting_code}" == "401" ]]; then
      sw_print_auth_help
    fi
  fi
fi

printf '\nTelegram 轮询冲突（error_code=409）\n'
# 锚点必须是固定串 error_code=409（core/telegram.py 里 Telegram API 失败信封的格式）。
# 绝不能匹配裸 409：生产日志绝大多数是 uvicorn 访问行，客户端临时端口随机撞上 409 三连字符
# 是常态（例：`127.0.0.1:44092 - "GET /health HTTP/1.1" 200 OK`），裸匹配必然假阳性。
# 只打印计数、绝不回显命中的原始日志行：Telegram 报错文本可能带上游 URL，回显等于泄露 token。
logs_text=""
if logs_text="$(docker compose logs --tail 2000 core </dev/null 2>&1)"; then
  conflict_count="$(printf '%s\n' "${logs_text}" | grep -c -F 'error_code=409' || true)"
  [[ -n "${conflict_count}" ]] || conflict_count=0
  printf '  近 2000 行中 error_code=409 的日志行数  %s\n' "${conflict_count}"
  if [[ "${conflict_count}" -eq 0 ]]; then
    gate_pass "Telegram error_code=409 计数为 0"
  else
    printf '  提示  历史事故：两套部署抢同一个 bot token 轮询，会吞掉用户的确认发布回调\n'
    gate_fail "Telegram error_code=409 计数为 0  实测 ${conflict_count} 行"
  fi
else
  printf '  <无法读取 core 日志>\n'
  gate_fail "Telegram error_code=409 计数为 0  无法读取 core 日志"
fi

if [[ "${run_preflight}" == "1" ]]; then
  printf '\n可选门禁：容器内 preflight\n'
  preflight_status=0
  # 这个 `</dev/null` 现在是**纵深防御**，不再是唯一保证——承重的是包住本段正文的
  # `{ ... } </dev/null`（见本段开头那块说明）。保留它的三条理由：读代码时 stdin 来源自明；
  # 花括号一旦被人拆掉它就是第二道防线；静态扫描仍然钉着它。请勿删除。
  # 它挡的是这个历史后果：`docker compose exec -T` 会把 stdin 转发进容器（`-T` 只关掉 TTY，
  # 不关掉 stdin 转发），少了显式来源时 preflight 会把下面的「核验结论」段、失败项汇总和
  # exit 1 判定整个吞掉，于是 `verify.sh --preflight` 在工作树脏 / HEAD 不符 / 确认闸门通道
  # 死掉时**照样报"全部通过"**。生产 2026-08-22 已实测到这个后果。
  docker compose exec -T core python3 scripts/preflight.py </dev/null || preflight_status=$?
  printf '  preflight 退出码  %s\n' "${preflight_status}"
  # 防回归哨兵：这一行必须出现在输出里。它一旦消失，就说明上面某条命令又把脚本正文吞了。
  printf '  preflight 之后脚本仍在执行\n'
  if [[ "${preflight_status}" -eq 0 ]]; then
    gate_pass "容器内 preflight（--preflight）"
  else
    gate_fail "容器内 preflight（--preflight）  退出码 ${preflight_status}"
  fi
else
  printf '\n可选门禁：容器内 preflight 未执行（需显式 --preflight；它会做外部连通性探测，可能超时）\n'
fi

printf '\n核验结论\n'
for verdict in ${verdicts[@]+"${verdicts[@]}"}; do
  printf '%s\n' "${verdict}"
done
if [[ "${failed}" -ne 0 ]]; then
  printf '\n✗ 生产部署核验失败：%s 项未通过（上面已列全，未在首个失败处中断）\n' "${failed}" >&2
  exit 1
fi
printf '\n全部核验项通过。\n'
exit 0
} </dev/null
REMOTE_VERIFY
if [[ "${remote_status}" -eq 255 ]]; then
  exit 254
fi
exit "${remote_status}"
} </dev/null
REMOTE_TAIL
}

# ssh(1) 不保留 argv 边界：host 之后的所有参数会被“用单空格拼成一个字符串”发给远端，
# 再由远端登录 shell 重新分词。所以绝不能写成
#     ssh ... bash -s -- "${EXPECTED_SHA}" "${RUN_PREFLIGHT}"
# ——未指定 --sha 时 EXPECTED_SHA 是空串，拼接后彻底消失，远端只收到 1 个参数，
# `set -u` 下 $2 立刻 unbound 报错。这里先用 %q 转成远端 shell 的字面量（空串会变成 ''），
# 重新分词后参数个数与内容原样存活。注入面为零：EXPECTED_SHA 已被 ^[0-9a-f]{40}$ 收敛，
# RUN_PREFLIGHT 是本脚本自己生成的 0/1，%q 再兜一层。后人请勿“优化”回直接传 argv。
#
# stdin 用**进程替换**而不是管道喂：`… | ssh …` 在 `set -o pipefail` 下会让写端的 SIGPIPE
# 有机会顶掉 ssh 自己的退出码，而下面那段重试逻辑完全靠"255 才是传输中断"这条判据。
# 进程替换让本函数的退出码就是 ssh 的退出码，一个字节都不掺别的。
verify_remote() {
  ssh -o ConnectTimeout=25 "${SSH_ALIAS}" "bash -s -- $(printf '%q ' "${EXPECTED_SHA}" "${RUN_PREFLIGHT}")" \
    < <(sw_ops_emit_token_prologue; verify_remote_script)
}

completed=0
attempt=1
verify_status=0
while :; do
  verify_status=0
  if verify_remote; then
    completed=1
    break
  else
    # 立刻捕获 ssh 的确切状态。只有 255 表示传输中断；远端所有核验失败都必须直接终止不重试。
    verify_status=$?
  fi
  if [[ "${verify_status}" -eq 255 && "${attempt}" -eq 1 ]]; then
    note "IAP 连接中断，3 秒后重试一次核验"
    sleep 3
    attempt=2
    continue
  fi
  break
done
[[ "${completed}" -eq 1 ]] || die "生产部署核验失败"
ok "生产部署核验通过"
