#!/usr/bin/env bash
# 用途：先备份生产数据，再演练或执行 core 的受限纯快进更新、重建和门禁检查。
set -euo pipefail

MODE="--dry-run"
TARGET_REF=""
EXPECTED_SHA=""
MODE_COUNT=0
REF_COUNT=0
SHA_COUNT=0
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
用法：bash scripts/ops/update.sh [--dry-run|--apply] [--ref <分支> --sha <40位小写SHA>]

不带 --ref/--sha 时沿用当前分支的 upstream；指定更新时两项必须同时提供。
USAGE
}

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --dry-run|--apply)
      MODE_COUNT=$((MODE_COUNT + 1))
      [[ "${MODE_COUNT}" -eq 1 ]] || die "--dry-run 与 --apply 只能指定一次"
      MODE="$1"
      ;;
    --ref)
      [[ "$#" -ge 2 && "$2" != --* ]] || die "--ref 缺少分支名"
      REF_COUNT=$((REF_COUNT + 1))
      [[ "${REF_COUNT}" -eq 1 ]] || die "--ref 只能指定一次"
      TARGET_REF="$2"
      shift
      ;;
    --sha)
      [[ "$#" -ge 2 && "$2" != --* ]] || die "--sha 缺少 SHA"
      SHA_COUNT=$((SHA_COUNT + 1))
      [[ "${SHA_COUNT}" -eq 1 ]] || die "--sha 只能指定一次"
      EXPECTED_SHA="$2"
      shift
      ;;
    -h|--help) usage; exit 0 ;;
    *) usage; die "参数无效：$1" ;;
  esac
  shift
done

if [[ "${REF_COUNT}" -ne "${SHA_COUNT}" ]]; then
  die "指定部署必须同时提供 --ref 和 --sha"
fi
if [[ "${REF_COUNT}" -eq 1 ]]; then
  # 先做严格字符过滤；远端仍用 git check-ref-format 复核。参数绝不拼入 shell 文本。
  [[ "${TARGET_REF}" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ && "${TARGET_REF}" != *".."* && "${TARGET_REF}" != */ && "${TARGET_REF}" != */.*/ && "${TARGET_REF}" != *"@{"* ]] \
    || die "远端 ref 格式非法：${TARGET_REF}"
  [[ "${EXPECTED_SHA}" =~ ^[0-9a-f]{40}$ ]] || die "SHA 必须是 40 位小写十六进制完整提交：${EXPECTED_SHA}"
fi

command -v ssh >/dev/null 2>&1 || die "本机没有 ssh 命令"

# 取用在备份与 SSH 之前完成：token 字符集不合法要在本机就报错退出，绝不带着一个会破坏
# curl 配置语法的值去连生产。未配置时这一步什么都不做，后续行为与改造前逐字一致。
sw_ops_load_ui_token

printf '生产 core 更新\n\n'
sw_ops_note_ui_token
note "先执行在线备份（演练同样会创建并轮转备份）"
bash "${SCRIPT_DIR}/backup.sh"

if [[ "${MODE}" == "--dry-run" ]]; then
  note "演练模式：只 fetch、核验目标和展示提交区间，不改工作树、不构建、不重启"
else
  note "执行模式：将核验目标、纯快进更新、构建、启动并跑容器内门禁"
fi

# 远端脚本正文。单独成一个函数，是为了让 ssh 的 stdin 能由三段拼接而成：
#   ① sw_ops_emit_token_prologue —— 一行 `export SW_OPS_UI_TOKEN=<%q 转义的值>`，
#      它落在下面那个外层 `{` 的**外面**（见 ui_token.sh 里那段"只许放内建命令"的警告）；
#   ② 这里的正文 —— 两段引号 heredoc，不做任何本地展开；
#   ③ 夹在两段之间的 sw_ops_emit_sw_probe_definition —— 远端 sw_probe 的唯一定义处，
#      发射进来的字节落在**内层**那对花括号里，位置与它替换掉的那份内联拷贝完全相同。
# token 只能走这条路：ssh 只转发 stdin 这一个通道，而 argv 是同机其他用户可读的。
update_remote_script() {
  cat <<'REMOTE_HEAD'
{
set -uo pipefail
# Run the update in a separate Bash so its errexit contract is independent of
# this status-normalizing wrapper. ssh reserves 255 for transport failures, so
# never let a remote script's own 255 become indistinguishable from that.
remote_status=0
bash -s -- "$@" <<'REMOTE_UPDATE' || remote_status=$?
{
set -euo pipefail
# ！！【stdin 的结构性保证——改本段任何一行之前先读完这一段】
# 历史缺陷：这层 bash 的脚本正文曾经**就是它自己的 stdin**（正上方那个 REMOTE_UPDATE
# heredoc；本机把它拼成一条流送进 ssh——REMOTE_HEAD + 发射进来的 sw_probe 定义 +
# REMOTE_TAIL）。任何在本段里被调用、又会读 stdin 的子进程，都会把"脚本剩下的部分"当成自己的
# 输入吞掉，后面的步骤随之全部消失，而脚本仍以 0 收尾——外层照样打印"✓ 更新完成"。
# 生产已实证：`docker compose exec -T core python3 scripts/preflight.py` 会吞掉它后面的
# /api/v1/system/info 探针循环（`-T` 只是不分配 TTY，**仍然转发 stdin**，这正是
# `docker compose exec -T db psql < dump.sql` 能工作的原因），于是"部署后 core 真的活过来
# 了"这件事根本没被证明过。
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
#   · 它们让每条边界命令的 stdin 来源在阅读时是自明的；
#   · 万一有人拆掉外面这对花括号，或把某一段搬进一个没有这层保护的新上下文，它们是第二道
#     防线；
#   · 本文件的静态扫描（tests/ops/test_update.sh）仍然钉着它们。
# 反过来同样成立：**不要**因为"反正有 `</dev/null`"就删掉花括号与 `} </dev/null`，
# 也**不要**因为"反正有花括号"就删掉那些 `</dev/null`。两层各守一半。
mode="$1"; requested_ref="$2"; expected_sha="$3"

# ---- 工作台 API 探针（带鉴权；token 一个字符都不进 argv）------------------------
# 【定义不在本文件里】紧跟这段注释的 sw_probe 定义由本机的
# sw_ops_emit_sw_probe_definition 发射进这条脚本流，**唯一定义处是 scripts/ops/ui_token.sh**。
# 从前这里是四份逐字相同的内联拷贝之一（verify/update/restart/status 各一份），靠
# tests/ops/test_update.sh 的一条逐字比对断言维持同步；收成一份之后，那条断言改成钉死
# "只有一处定义、没有任何脚本自己内联"。
# 发射进来的字节落在**内层**那对花括号的里面，与内联拷贝时的位置完全相同，所以本段开头
# 那道 `{ ... } </dev/null` 的结构性保证一字未变（重新论证见 ui_token.sh 该函数上方）。
# 理由见 scripts/ops/ui_token.sh 头部：token 经 `curl --config -` 的配置流注入，curl 的 argv
# 里只有 `--config -`。/proc/*/cmdline 对同机其他用户可读，而这台是合租机器
# （docs/RISKS.md §8.2）。判定语义与改造前逐字相同（仍是 `-f`，HTTP >= 400 仍退 22，
# URL/超时/重试次数一字未改）；只多一个 `-w`，把状态码追加到响应体末尾，好把 401 与
# "连不上/超时"分开。
REMOTE_HEAD
  sw_ops_emit_sw_probe_definition
  cat <<'REMOTE_TAIL'
cd "${HOME}/social_workflow"
repo_dir="${HOME}/social_workflow"
original_head="$(git rev-parse --verify HEAD </dev/null)"
original_branch="$(git symbolic-ref --quiet --short HEAD </dev/null || printf '<detached HEAD>')"

rollback_hint() {
  printf '\n✗ 更新失败。未自动回滚。更新前 branch=%s, HEAD=%s\n' "${original_branch}" "${original_head}" >&2
  printf '  如需人工恢复（仅在确认工作树无须保留的改动后）：\n' >&2
  if [[ "${original_branch}" == "<detached HEAD>" ]]; then
    printf '  cd %q && git switch --detach %q && docker compose build core && docker compose up -d core\n' "${repo_dir}" "${original_head}" >&2
  else
    printf '  cd %q && git switch -- %q && git reset --hard %q && docker compose build core && docker compose up -d core\n' "${repo_dir}" "${original_branch}" "${original_head}" >&2
  fi
  printf '  上述命令只操作本机工作树和 compose；不会推送、删除或改写远端 ref。\n' >&2
}
abort_update() {
  printf '%s\n' "$1" >&2
  [[ "${mode}" == "--apply" ]] && rollback_hint
  exit 1
}
unexpected_failure() {
  local status="$?"
  rollback_hint
  exit "${status}"
}
[[ "${mode}" == "--apply" ]] && trap unexpected_failure ERR

if [[ -n "${requested_ref}" ]]; then
  git check-ref-format --branch "${requested_ref}" </dev/null >/dev/null || abort_update "远端 ref 格式非法：${requested_ref}"
  remote_ref="refs/remotes/origin/${requested_ref}"
  # 目标必须来自 origin 的 remote-tracking ref；只抓 FETCH_HEAD 会绕过这条核验。
  git fetch --no-tags --prune origin "+refs/heads/${requested_ref}:refs/remotes/origin/${requested_ref}" </dev/null
  raw_ref_sha="$(git rev-parse --verify "${remote_ref}" </dev/null)" || abort_update "找不到远端 ref：${remote_ref}"
  target_sha="$(git rev-parse --verify "${remote_ref}^{commit}" </dev/null)" || abort_update "远端 ref 不是 commit：${remote_ref}"
  [[ "${raw_ref_sha}" == "${target_sha}" ]] || abort_update "远端 ref 必须直接指向 commit：${remote_ref}"
  [[ "${target_sha}" == "${expected_sha}" ]] || abort_update "远端 ref 与期望 SHA 不一致：${remote_ref}=${target_sha}，期望 ${expected_sha}"
  target_label="${remote_ref}"
else
  upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' </dev/null)" || abort_update "当前分支没有 upstream；请显式提供 --ref 与 --sha"
  git fetch --prune </dev/null
  target_sha="$(git rev-parse --verify "${upstream}" </dev/null)" || abort_update "找不到跟踪分支：${upstream}"
  target_label="${upstream}"
fi

worktree_status="$(git status --porcelain </dev/null)" || abort_update "无法确认工作树状态，拒绝执行更新。"
[[ -z "${worktree_status}" ]] || abort_update "工作树有未提交改动，拒绝执行更新。"
behind="$(git rev-list --count "${original_head}..${target_sha}" </dev/null)"
ahead="$(git rev-list --count "${target_sha}..${original_head}" </dev/null)"
if [[ "${ahead}" -ne 0 && "${behind}" -ne 0 ]]; then
  abort_update "当前 HEAD 与目标分叉，不能安全快进更新。"
fi
[[ "${ahead}" -eq 0 ]] || abort_update "当前 HEAD 领先目标，不能安全快进更新。"
# 计数已经排除了领先和分叉；祖先检查再独立确认目标确实可纯快进。
git merge-base --is-ancestor "${original_head}" "${target_sha}" </dev/null \
  || abort_update "当前 HEAD 不是目标的祖先，不能安全快进更新。"

printf '当前分支  %s\n当前提交  %s\n更新目标  %s\n目标 SHA   %s\n待前进    %s 个提交\n本地领先  %s 个提交\n' "${original_branch}" "${original_head}" "${target_label}" "${target_sha}" "${behind}" "${ahead}"
printf '\n将要前进的提交\n'
[[ "${behind}" -eq 0 ]] && printf '  <无，当前提交已是目标>\n' || git --no-pager log --oneline "${original_head}..${target_sha}" </dev/null
if [[ "${mode}" == "--dry-run" ]]; then
  printf '\n演练完成：目标 SHA 已核验；未执行 merge、compose build、compose up 或 preflight。\n'
  exit 0
fi

if [[ -n "${requested_ref}" ]]; then
  git merge --ff-only "${expected_sha}" </dev/null
else
  # 兼容原有 upstream 更新路径；只允许 Git 自己按已配置 upstream 纯快进 pull。
  git pull --ff-only </dev/null
fi
deployed_sha="$(git rev-parse --verify HEAD </dev/null)"
[[ "${deployed_sha}" == "${target_sha}" ]] || abort_update "部署后 HEAD 与已核验目标不一致：HEAD=${deployed_sha}，目标=${target_sha}"
docker compose build core </dev/null
docker compose up -d core </dev/null

read_core_loopback_port() {
  local captured mapping port
  # 末尾哨兵让 command substitution 不吞掉 compose 输出中的换行，以便拒绝空行和多行。
  captured="$(docker compose port core 8000 </dev/null && printf '\037')" \
    || abort_update "无法读取 core 8000 的发布端口。"
  [[ "${captured}" == *$'\037' ]] || abort_update "无法完整读取 core 8000 的发布端口。"
  captured="${captured%$'\037'}"
  [[ "${captured}" == *$'\n' ]] && captured="${captured%$'\n'}"
  mapping="${captured}"
  [[ -n "${mapping}" && "${mapping}" != *$'\n'* && "${mapping}" != *$'\r'* ]] \
    || abort_update "core 8000 端口必须是恰好一条 loopback 映射，拒绝映射：$(printf '%q' "${mapping}")"
  [[ "${mapping}" =~ ^(127\.0\.0\.1|\[::1\]):([1-9][0-9]{0,4})$ ]] \
    || abort_update "core 8000 端口必须是规范的 loopback:port，拒绝映射：$(printf '%q' "${mapping}")"
  port="${BASH_REMATCH[2]}"
  [[ "${port}" -le 65535 ]] \
    || abort_update "core 8000 发布端口必须在 1..65535，拒绝映射：$(printf '%q' "${mapping}")"
  port_mapping="${mapping}"
  published_port="${port}"
  if [[ "${mapping}" == \[::1\]:* ]]; then
    probe_host='[::1]'
  else
    probe_host='127.0.0.1'
  fi
}
port_mapping=""
published_port=""
probe_host=""
read_core_loopback_port
printf '端口门禁  core:8000 -> loopback（%s）\n' "${port_mapping}"
# 这个 `</dev/null` 现在是**纵深防御**，不再是唯一保证——承重的是包住本段正文的
# `{ ... } </dev/null`（见本段开头那块说明）。保留它的理由：读代码时 stdin 来源自明；
# 花括号一旦被人拆掉它就是第二道防线；静态扫描仍然钉着它。请勿删除。
# 它挡的是这个历史后果：`docker compose exec -T` 会把 stdin 转发进容器（`-T` 只关掉 TTY，
# 不关掉 stdin 转发），少了显式来源时 preflight 会把下面整段 /api/v1/system/info 探针循环
# 连同 abort_update 一起吞掉，脚本以 0 收尾，外层照样打印
# "✓ 更新完成，端口门禁、容器内门禁和探针均通过"——而"部署后 core 真的活过来了"从来没有
# 被证明过。
docker compose exec -T core python3 scripts/preflight.py </dev/null
# 防回归哨兵：这一行必须出现在输出里。它一旦消失，就说明 preflight 又把脚本正文吞了。
printf '容器内门禁  preflight 通过，继续做探针\n'
attempt=1
probe_ok=0
probe_unauthorized=0
info_json=""
while [[ "${attempt}" -le 15 ]]; do
  # 与改动前的差别只有两处：① 响应体从「直接丢弃」改成「收进变量」，因为紧随其后的 R1 闸门
  # 要读这次响应里的 use_fake_publishers；② 探针改走 sw_probe，好把 401 与"连不上"分开。
  # 探针成败的判定依据仍然只有 curl 的退出码，URL、超时、重试次数（15 次 × sleep 2）与打印
  # 的那一行一字未变。`2>/dev/null` 也照旧：curl 的 -S 错误行不进部署输出。
  probe_status=0
  sw_probe "http://${probe_host}:${published_port}/api/v1/system/info" 5 >/dev/null 2>/dev/null || probe_status=$?
  if [[ "${probe_status}" -eq 0 ]]; then
    printf '\n探针  GET /api/v1/system/info 200（第 %s 次）\n' "${attempt}"
    info_json="${sw_probe_body}"
    probe_ok=1
    break
  fi
  if [[ "${sw_probe_code}" == "401" ]]; then
    # 401 意味着 core **已经起来并在应答**，再等 30 秒毫无意义。这里立刻停手并把原因说白：
    # docs/RISKS.md §8.4 点名的误判风险就是"把运维侧缺 token 读成部署失败"。
    probe_unauthorized=1
    break
  fi
  sleep 2; attempt=$((attempt + 1))
done
if [[ "${probe_unauthorized}" -eq 1 ]]; then
  abort_update "探针拿到 401：core 已启用 SW_UI_TOKEN 鉴权，而本次部署没带上匹配的 token。
  先别按下面的人工恢复指引回滚：401 恰恰证明新版 core 已经起来并在正常应答，代码侧很可能是好的。真正缺的是运维侧凭据，中止只是因为没有 token 就无法完成探针与 R1 红线闸门的核验。
  处置：在值班工作站 export SW_OPS_UI_TOKEN=<生产 .env 里 SW_UI_TOKEN 的同一个值>（或写进 ~/.dsh-sw/.credentials.yaml 的 sw_ui_token 键），先跑 bash scripts/ops/verify.sh --sha ${target_sha} 确认这一版到底部署成没有，再决定要不要重跑本脚本。
  已经配了还是 401 = 值不匹配：以生产 .env 里 SW_UI_TOKEN 的原文为准，别把引号或行尾空格一起复制。
  出处：docs/RISKS.md 第 8 条、scripts/ops/README.md「工作台 API token」。"
fi
[[ "${probe_ok}" -eq 1 ]] || abort_update "core 在 30 秒内未恢复 /api/v1/system/info 200。"

# ===== R1 红线闸门：真发布开启时，人工确认闸门通道必须是活的 =====================
# 为什么放在这里：这道闸门只有在 /api/v1/system/info 探针**成功之后**才有意义。探针成功
# 才证明新版 core 已经起来了，此刻读到的 use_fake_publishers 与确认通道状态反映的是
# **刚部署的这一版**的真实配置。放在 build/up 之前读到的是旧版的状态，检查了也白检查。
#
# 语义是把 scripts/ops/verify.sh 里那道同款互锁**原样搬过来**（不是重新发明），逐条对齐：
#   use_fake_publishers=true       → 什么都不会真发，确认通道状态不构成阻断，只如实记录
#   use_fake_publishers=false      → 从严：要求 enabled+ready+polling 三者皆真，否则阻断
#   use_fake_publishers 取不到/不可解析 → 无法证明"什么都不会真发"，按真发布从严裁定
#
# R1 红线：内容上线必须由人点一下才真发——在 Telegram 闸门消息上点，或在工作台点
# 「确认发布」（同一后端 core.confirm.confirm_item，见 SW-AGENT.md §2 R1）。Telegram 是
# 这道确认的**主载体**。
# 通道死掉的真实后果**不是**"内容照样越权发出去"——恰恰相反，内容会**发不出去**：
#   core/scheduler.py:498-505  tick_scheduled_publish 里的**人工确认闸门**
#                              `confirm_required(policy) and item.confirmed_at is None`
#                              → stats["skipped_unconfirmed"] → continue，**跳过不发**
#                              （叫名字不叫序数，理由见 verify.sh 同段注释）
#   core/accounts.py:216,313   confirm_required 默认 True，只有显式 false 才关得掉
#   core/confirm.py:9-11       autopilot 只影响"自动批准"，不影响"发布前要人点"，无旁路
#   core/confirm.py:23-25      SW_CONFIRM_TTL_HOURS（默认 24）到点**自动驳回并通知**
# 即：内容在排期处静默堆积 → TTL 到点被自动驳回 → **发布链路停摆**。三格分别对应：
# enabled=false（SW_TELEGRAM_ENABLED 关着，build_telegram_notifier() 直接返回 None，一条卡
# 都推不出去）、ready=false（token/chat_id 不全，卡推不出去）、polling=false（卡推得出去，
# 但人点了没有长轮询线程去收回调）。
# 注意 R1 **并没有**因此失去载体：core/confirm.py:254-255 明写"没有 Telegram 不是
# 错误：工作台里的兜底确认按钮照样能用"。工作台那个载体不受 Telegram 影响。但它要求操作者
# **知道**主载体已经死了，所以这里必须是一道显式闸门，而不是静默降级。
#
# 这道闸门是**事后检测，不是预防**：它坐在 docker compose build + up -d 之后，触发时新版
# core 已经在跑了。abort_update 只打印人工回滚指引、不自动回滚（见 rollback_hint）。
#
# 为什么不是去改 scripts/preflight.py::check_notifier：那个检查也跑在本部署流程里，且被
# 刻意设计成永不 FAIL；把它升级成 FAIL，任何一次 Telegram 侧的临时抖动都会卡死整个生产
# 部署，杀伤力和它要防的问题不对称。这里是针对性的安全条件——只有"真发布开启 + 确认通道
# 是死的"这个**具体组合**才阻断部署，其余通知降级场景一律不牵连。
#
# 本段每一条边界命令的 stdin 都是显式的：curl 用 `</dev/null`，两处
# `docker compose exec -T` 由 `printf ... |` 管道喂（管道本身就是显式来源）。理由见本段
# 开头的说明块——少一处，这道闸门连同它后面的收尾就会被整段吞掉，而脚本照样以 0 收尾。
fake_publishers="<未知>"
fake_status=0
# 与 verify.sh 同一手法：python 解析 JSON、只用退出码回话，shell 只做退出码→裁定的映射。
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

if [[ "${fake_publishers}" == "true" ]]; then
  # 生产既定裁决：挂着模拟发布器时什么都不会真发，确认通道状态如实记录、不构成阻断。
  # 这条路径刻意**不去探测** /api/v1/system/telegram：多打一个请求就不再是"行为不变"。
  printf 'R1 闸门  模拟发布器=true：本版什么都不会真发，人工确认闸门通道不构成阻断（未探测；取证请跑 scripts/ops/verify.sh）\n'
else
  if [[ "${fake_publishers}" == "false" ]]; then
    strict_why="真发布已开启（模拟发布器=false）"
    strict_hint="或把 SW_USE_FAKE_PUBLISHERS 设回 true 后重新部署：那时什么都不会真发，本闸门放行。"
  else
    # 取不到布尔量时**不能**再建议"设回 true"——它很可能已经是 true 了，真正坏掉的是上面
    # 那条容器内解析（docker compose exec 打嗝、core 里没有 python3、容器没起来……）。
    # 给一句已经成立、因而不可执行的建议，只会把人引到错误的方向。
    strict_why="模拟发布器状态取不到（use_fake_publishers=${fake_publishers}），按真发布从严裁定"
    strict_hint="注意本次连 use_fake_publishers 都没读出来（读取方式：docker compose exec -T core python3 解析 /api/v1/system/info）。所以先别急着改 SW_USE_FAKE_PUBLISHERS——它可能已经是 true 了。先查 docker compose ps / docker compose logs core，确认容器起着且容器内 python3 能跑通，再重试部署。"
  fi
  # 探针 GET /api/v1/system/telegram（core/api/system.py::telegram_info）**不发任何网络
  # 请求**，只读配置 + 本进程轮询线程状态；TelegramOut 契约上绝不含 token。
  telegram_json=""
  telegram_status=0
  telegram_probe_status=0
  sw_probe "http://${probe_host}:${published_port}/api/v1/system/telegram" 5 >/dev/null 2>/dev/null || telegram_probe_status=$?
  if [[ "${telegram_probe_status}" -ne 0 ]]; then
    if [[ "${sw_probe_code}" == "401" ]]; then
      telegram_status=31
    else
      telegram_status=30
    fi
  else
    telegram_json="${sw_probe_body}"
    # enabled 必须**直接判**：ready 只看 token+chat_id，压根不看总开关。靠 polling 去间接
    # 兜住"总开关关着"是在赌 poller 的启动条件恰好包含 enabled——那是实现细节不是契约，
    # 哪天变了这层兜底会静默失效，而失效方向是危险方向。
    printf '%s' "${telegram_json}" | docker compose exec -T core python3 -c '
import json
import sys

try:
    payload = json.loads(sys.stdin.read())
except ValueError:
    raise SystemExit(22)
if not isinstance(payload, dict) or not payload.get("ok"):
    raise SystemExit(22)
data = payload.get("data") or {}
enabled = data.get("enabled") is True
ready = data.get("ready") is True
polling = data.get("polling") is True
if enabled and ready and polling:
    raise SystemExit(0)
if not enabled:
    raise SystemExit(25)
raise SystemExit(23 if not ready else 24)
' >/dev/null 2>&1 || telegram_status=$?
  fi
  case "${telegram_status}" in
    0) telegram_summary="enabled=true ready=true polling=true" ;;
    22) telegram_summary="无法解析 /api/v1/system/telegram" ;;
    23) telegram_summary="ready=false（那张要点的卡片根本推不出去）" ;;
    24) telegram_summary="ready=true 但 polling=false（卡片能推出去，人点了没有线程去收）" ;;
    25) telegram_summary="总开关 enabled=false（SW_TELEGRAM_ENABLED 关着，build_telegram_notifier() 直接返回 None，一条都发不出去）" ;;
    30) telegram_summary="无法获取 /api/v1/system/telegram" ;;
    31) telegram_summary="/api/v1/system/telegram 返回 401（core 已启用 SW_UI_TOKEN，本机未提供或不匹配）" ;;
    *) telegram_summary="/api/v1/system/telegram 解析异常（退出码 ${telegram_status}）" ;;
  esac
  if [[ "${telegram_status}" -ne 0 ]]; then
    abort_update "R1 红线闸门未通过：${strict_why}，但人工确认闸门通道 ${telegram_summary}。
  后果不是内容会越权发出去——恰恰相反：发布前的人工确认闸门等不到人的那一票，内容会被跳过不发（scheduler 记 skipped_unconfirmed），在排期处静默堆积，并在 SW_CONFIRM_TTL_HOURS（默认 24 小时）到点被自动驳回。发布链路等于停摆。
  兜底：工作台的「确认发布」按钮不受 Telegram 影响，仍可用于确认（同一后端 core.confirm.confirm_item）——但那要求人知道主载体已经死了，这也正是本闸门存在的理由。
  注意这是事后检测：新版 core 此刻已经 build + up 过并且正在跑，下面的恢复指引需要人工执行。
  回滚代码有没有用，要看是哪一格：enabled 与 ready 取决于服务器 .env 里的 SW_TELEGRAM_*，回滚代码修不好；但 polling 取决于**正在跑的这份代码**——长轮询线程由 core/main.py:104 的 lifespan 起、core/telegram.py:981 按 poller.alive 判活。所以刚部署完就红在 polling 这一格时，第一顺位假设是新版把 poller 起崩了，回滚到上一版是有效解法。
  排查：bash scripts/ops/verify.sh（会打印 enabled/configured/ready/chat_configured/polling/detail 全部字段与下一步指引）。
  ${strict_hint}"
  fi
  printf 'R1 闸门  %s：人工确认闸门通道 %s\n' "${strict_why}" "${telegram_summary}"
fi
# 上面这条 `R1 闸门  ...` 是唯一的放行输出，同时兼作防回归哨兵：它排在本段所有读 stdin
# 的边界命令**之后**，一旦它从输出里消失，就说明闸门里某条命令又把脚本正文吞了——那时
# 闸门等于没跑，而脚本仍会以 0 收尾、外层照样打印"✓ 更新完成"。测试直接断言它存在。

# ---- 部署标记：把"这次部署的是哪条线"记下来 ---------------------------------------
#
# 【为什么要有它】scripts/ops/verify.sh 的「发布线」一段回答的是**事实**（这个提交落在哪条
# origin 线上），但它回答不了**意图**（有人打算部署哪条线）。两者不一致——有人手工 merge
# 过、或从另一条线部署过——是值得被看见的信号，而没有记录就永远看不见。docs/RISKS.md
# 第 11 条的误判土壤正是"本地分支名不等于发布线"；多一条意图记录，人一眼就能分辨。
#
# 【位置在 git 工作树外面】放进 ~/social_workflow 会让 verify.sh 的「工作树干净」判失败、
# 让本脚本下一次自己拒绝部署；写进 .gitignore 也不行——.gitignore 是被版本控制的，本脚本
# 的快进随时可能把它换掉。下面这行字面量与 scripts/ops/verify.sh 里读它的那一处**必须逐字
# 一致**，tests/ops/test_update.sh 有一条源码级断言比对两边。
#
# 【写失败绝不让部署失败，但也绝不留下一条过期的记录】部署本身已经成功了，为一条簿记文件
# 把它判失败是错的。反过来，写不成时把上一次的旧标记留在原地更坏：verify.sh 会照实报
# "上次部署的是 X"，而那已经不是真的。**错的记录比没有记录坏**，所以写失败时主动把旧的
# 删掉，让 verify.sh 如实回到"没有记录"。两条路径都打印一行，绝不静默。
#
# 【原子写入】同目录临时文件 + mv（rename(2)）：verify.sh 读到的要么是完整的旧记录、要么是
# 完整的新记录，不会是半截。
#
# 【时间戳在远端取】它要回答的是"生产上是什么时候部署的"，用服务器自己的钟才对得上服务器
# 的日志；从值班工作站传一个过来还得先解释两边的时钟差。取不到规范形状就**不写**，不凑合。
deploy_marker_file="${HOME}/sw-deploy-state/last-deploy"
deploy_marker_dir="${deploy_marker_file%/*}"
deploy_marker_tmp="${deploy_marker_file}.tmp.$$"
deploy_marker_at=""
deploy_marker_at="$(date -u +%Y-%m-%dT%H:%M:%SZ </dev/null 2>/dev/null)" || deploy_marker_at=""
# 记的是这次部署的发布线：--ref 形态记 --ref 的原值（如 p14-organic），沿用 upstream 的形态
# 记那个 upstream 名（如 origin/main）。verify.sh 两种写法都认（它归一成 origin/<短名> 再比）。
if [[ -n "${requested_ref}" ]]; then
  deploy_marker_ref="${requested_ref}"
else
  deploy_marker_ref="${target_label}"
fi
deploy_marker_written=0
if [[ ! "${deploy_marker_at}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]]; then
  printf '部署标记  ⚠ 远端 date -u 给不出规范的 UTC 时间戳，本次不写标记（部署本身已经成功）\n'
elif ! mkdir -p "${deploy_marker_dir}" </dev/null 2>/dev/null; then
  printf '部署标记  ⚠ 无法创建 %s，本次不写标记（部署本身已经成功）\n' "${deploy_marker_dir}"
elif {
       printf 'schema=1\n'
       printf 'ref=%s\n' "${deploy_marker_ref}"
       printf 'sha=%s\n' "${deployed_sha}"
       printf 'at=%s\n' "${deploy_marker_at}"
     } >"${deploy_marker_tmp}" 2>/dev/null && mv "${deploy_marker_tmp}" "${deploy_marker_file}" </dev/null 2>/dev/null; then
  deploy_marker_written=1
fi
if [[ "${deploy_marker_written}" -eq 1 ]]; then
  printf '部署标记  已记录 ref=%s sha=%s at=%s（%s）\n' \
    "${deploy_marker_ref}" "${deployed_sha}" "${deploy_marker_at}" "${deploy_marker_file}"
else
  rm -f "${deploy_marker_tmp}" "${deploy_marker_file}" </dev/null 2>/dev/null || true
  printf '部署标记  ⚠ 写入失败，已把可能存在的旧标记一并删掉——一条过期的记录比没有记录更坏；verify.sh 会如实报「没有记录」\n'
fi
trap - ERR
exit 0
} </dev/null
REMOTE_UPDATE
if [[ "${remote_status}" -eq 255 ]]; then
  exit 254
fi
exit "${remote_status}"
} </dev/null
REMOTE_TAIL
}

# ssh 不保留 argv 边界：ssh(1) 明确写着 host 之后的参数会"用单个空格拼接后"发给
# 远端，由远端登录 shell 重新分词。若照 `ssh host bash -s -- "$A" "$B" "$C"` 写，
# 不带 --ref/--sha 时 B、C 是空串，拼出 `bash -s -- --dry-run  `，重新分词后两个空
# 参数彻底消失，远端 `set -u` 下读 "$2" 立刻 unbound variable 退出——README 主推的
# 默认用法因此在真实服务器上是坏的。
# 所以这里必须自己造那一个字符串，并用 printf '%q' 转义，让空参数以 '' 的形式活到
# 远端重新分词之后。请勿"优化"回裸 argv 形式或裸插值：
#   - %q 对空串产出 ''，重新分词后仍是一个（空）参数；对已过校验的 ref/sha 产出原样字符
#   - 注入面为零：MODE 取自固定字面量集合，TARGET_REF 已被
#     ^[A-Za-z0-9][A-Za-z0-9._/-]*$ 过滤（无 shell 元字符），EXPECTED_SHA 已被
#     ^[0-9a-f]{40}$ 过滤；%q 再兜一层
#   - 参数仍然不是手工拼接的裸插值——%q 转义是这里唯一被允许的拼接方式
# token 绝不走这条路：它在 stdin 流里（见 update_remote_script 上方说明）。
#
# stdin 用**进程替换**而不是管道喂：`… | ssh …` 在 `set -o pipefail` 下会让写端的 SIGPIPE
# 有机会顶掉 ssh 自己的退出码，而下面那段重试逻辑完全靠"255 才是传输中断"这条判据。
update_remote() {
  ssh -o ConnectTimeout=25 "${SSH_ALIAS}" "bash -s -- $(printf '%q ' "${MODE}" "${TARGET_REF}" "${EXPECTED_SHA}")" \
    < <(sw_ops_emit_token_prologue; update_remote_script)
}

completed=0
attempt=1
update_status=0
while :; do
  update_status=0
  if update_remote; then
    completed=1
    break
  else
    # Capture ssh's exact status immediately. Only 255 denotes transport loss;
    # all remote validation/runtime failures must terminate without a retry.
    update_status=$?
  fi
  if [[ "${MODE}" == "--dry-run" && "${update_status}" -eq 255 && "${attempt}" -eq 1 ]]; then
    note "IAP 连接中断，3 秒后重试一次演练"
    sleep 3
    attempt=2
    continue
  fi
  break
done
[[ "${completed}" -eq 1 ]] || die "更新${MODE}失败"
if [[ "${MODE}" == "--dry-run" ]]; then
  ok "更新演练完成"
else
  ok "更新完成，端口门禁、容器内门禁和探针均通过"
fi
