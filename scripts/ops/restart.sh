#!/usr/bin/env bash
# 用途：先做生产在线备份，再重启 core，确认系统信息探针恢复 200，并核验 R1 人工确认闸门通道。
set -euo pipefail

SSH_ALIAS="${SW_OPS_SSH_ALIAS:-${SW_TUNNEL_SSH_ALIAS:-workbench-iap}}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 远端退出码协议（**这里是唯一定义处**，两个值都用 printf '%q' 传给远端当位置参数，
# 远端不再硬编码任何一个字面量）：
#   R1_GATE_UNPROBED_STATUS  模拟发布器=true：闸门放行，但刻意**没有**探测确认通道。
#                            这也是"成功"，只是收尾话术必须与"已核验"区分开——当前生产就是
#                            这个状态，每次重启都宣称"已核验"等于每次都说一句假话。
#   R1_GATE_FAIL_STATUS      R1 闸门未通过。外层据此**不重试**：再重启一次也修不好 .env。
#   PROBE_UNAUTHORIZED_STATUS 探针拿到 401：core 已启用 SW_UI_TOKEN 而本机没有匹配的 token。
#                            外层据此**不重试**：core 已经在应答了，再重启一次只是白白多打断
#                            一次生产，缺的是凭据不是重启（docs/RISKS.md §8.4）。
# 三者都必须避开 ssh 保留的 255，也避开远端脚本可能自然产生的 1/2。
R1_GATE_UNPROBED_STATUS=20
R1_GATE_FAIL_STATUS=40
PROBE_UNAUTHORIZED_STATUS=41

die() { printf '\n✗ %s\n' "${1}" >&2; shift; for line in "$@"; do printf '  %s\n' "${line}" >&2; done; exit 1; }
note() { printf '  %s\n' "${1}"; }
ok() { printf '  ✓ %s\n' "${1}"; }

# 工作台 API token 的取用与注入（含 argv 零暴露的理由），见该文件头部说明。
# 必须在 die/note 之后 source：库里报错走调用方的 die。
# shellcheck source=scripts/ops/ui_token.sh
. "${SCRIPT_DIR}/ui_token.sh"

command -v ssh >/dev/null 2>&1 || die "本机没有 ssh 命令"

# 取用在备份与 SSH 之前完成：token 字符集不合法要在本机就报错退出。
# 未配置时这一步什么都不做，后续行为与改造前逐字一致。
sw_ops_load_ui_token

printf '生产 core 重启\n\n'
sw_ops_note_ui_token
note "先执行在线备份"
bash "${SCRIPT_DIR}/backup.sh"

note "连接 ${SSH_ALIAS}，重启 core"

# 远端脚本正文。单独成一个函数，是为了让 ssh 的 stdin 能由三段拼接而成：
#   ① sw_ops_emit_token_prologue —— 一行 `export SW_OPS_UI_TOKEN=<%q 转义的值>`，
#      它落在下面那个外层 `{` 的**外面**（见 ui_token.sh 里那段"只许放内建命令"的警告）；
#   ② 这里的正文 —— 两段引号 heredoc，不做任何本地展开；
#   ③ 夹在两段之间的 sw_ops_emit_sw_probe_definition —— 远端 sw_probe 的唯一定义处，
#      发射进来的字节落在**内层**那对花括号里，位置与它替换掉的那份内联拷贝完全相同。
# token 只能走这条路：ssh 只转发 stdin 这一个通道，而 argv 是同机其他用户可读的。
restart_remote_script() {
  cat <<'REMOTE_HEAD'
{
set -uo pipefail
# 重启跑在独立的 Bash 里，errexit 契约与这层状态规范化包装互不影响。ssh 用 255 表示传输
# 中断，所以远端脚本自身的 255 必须先改写成别的非零码，避免与断链混淆——否则外层会把它
# 当成 IAP 断链而**再重启一次生产 core**。update.sh / verify.sh 都有这层，这里对齐。
remote_status=0
bash -s -- "$@" <<'REMOTE_RESTART' || remote_status=$?
{
set -euo pipefail
# ！！【stdin 的结构性保证——改本段任何一行之前先读完这一段】
# 历史缺陷：这层 bash 的脚本正文曾经**就是它自己的 stdin**（正上方那个 REMOTE_RESTART
# heredoc；本机把它拼成一条流送进 ssh——REMOTE_HEAD + 发射进来的 sw_probe 定义 +
# REMOTE_TAIL）。任何在本段里被调用、又会读 stdin 的子进程，都会把"脚本剩下的部分"当成自己的
# 输入吞掉，后面的步骤随之全部消失，而脚本仍以 0 收尾——外层照样打印"✓ core 已重启"。
# `docker compose exec -T` 正是这样一个进程（`-T` 只是不分配 TTY，**仍然转发 stdin**，
# 这正是 `docker compose exec -T db psql < dump.sql` 能工作的原因）。
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
# 包括 `docker compose restart` 这种"看起来不读 stdin"的：真实 docker 不 attach stdin，但
# 本脚本自己声明了这条不变量就不能有例外（历史上正是"看起来没事"的那条命令把整段探针 +
# 闸门吞掉，脚本却以 0 收尾）。反过来也**不要**因为"反正有 `</dev/null`"就删掉花括号与
# `} </dev/null`：两层各守一半。
gate_unprobed_status="$1"; gate_fail_status="$2"; probe_unauthorized_status="$3"

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
# （docs/RISKS.md §8.2）。判定语义与改造前逐字相同（仍是 `-f`，URL/超时/15 次重试一字未改）；
# 只多一个 `-w`，把状态码追加到响应体末尾，好把 401 与"连不上/超时"分开。
REMOTE_HEAD
  sw_ops_emit_sw_probe_definition
  cat <<'REMOTE_TAIL'

cd "${HOME}/social_workflow"
docker compose restart core </dev/null

attempt=1
probe_ok=0
probe_unauthorized=0
info_json=''
while [[ "${attempt}" -le 15 ]]; do
  # 与改动前的差别只有两处：① 响应体从「直接丢弃」改成「收进变量」，下面的 R1 闸门要读这次
  # 响应里的 use_fake_publishers；② 探针改走 sw_probe，好把 401 与"连不上"分开。
  # 探针成败的判定依据仍然只有 curl 的退出码，URL、超时与 15 次重试一字未变，闸门也因此
  # 不必再多打一个 /api/v1/system/info 请求。`2>/dev/null` 照旧。
  probe_status=0
  sw_probe 'http://127.0.0.1:8000/api/v1/system/info' 5 >/dev/null 2>/dev/null || probe_status=$?
  if [[ "${probe_status}" -eq 0 ]]; then
    printf '  探针  GET /api/v1/system/info 200（第 %s 次）\n' "${attempt}"
    info_json="${sw_probe_body}"
    probe_ok=1
    break
  fi
  if [[ "${sw_probe_code}" == "401" ]]; then
    # 401 意味着 core 已经起来并在应答，再等 30 秒毫无意义，重试更是白白多打断一次生产。
    probe_unauthorized=1
    break
  fi
  sleep 2
  attempt=$((attempt + 1))
done

if [[ "${probe_unauthorized}" -eq 1 ]]; then
  printf '✗ 探针拿到 401：core 已启用 SW_UI_TOKEN 鉴权，而本机没带上匹配的 token。\n' >&2
  printf '  注意这不是重启失败：401 恰恰证明 core 已经重启完成并在正常应答。缺的是运维侧凭据，\n' >&2
  printf '  没有 token 就无法完成探针确认与 R1 红线闸门的核验，所以这里停手而不是宣称成功。\n' >&2
  printf '  处置：export SW_OPS_UI_TOKEN=<生产 .env 里 SW_UI_TOKEN 的同一个值>，或写进\n' >&2
  printf '        ~/.dsh-sw/.credentials.yaml（0600）的 sw_ui_token 键，然后重跑本脚本。\n' >&2
  printf '  已经配了还是 401 = 值不匹配：以生产 .env 里 SW_UI_TOKEN 的原文为准。\n' >&2
  printf '  出处：docs/RISKS.md 第 8 条、scripts/ops/README.md「工作台 API token」。\n' >&2
  exit "${probe_unauthorized_status}"
fi

if [[ "${probe_ok}" -ne 1 ]]; then
  printf '✗ core 在 30 秒内未恢复 /api/v1/system/info 200。最后一次探针错误如下：\n' >&2
  # 原来这里靠 set -e 让 curl 的退出码成为脚本退出码；显式写出来，顺带堵住"第 16 次
  # 偏偏成功了就以 0 收尾、外层反而打印成功"这个口子。
  sw_probe 'http://127.0.0.1:8000/api/v1/system/info' 5 >/dev/null || exit $?
  exit 1
fi

# ===== R1 红线闸门：真发布开启时，人工确认闸门通道必须是活的 =====================
# 【为什么 restart.sh 也必须有这道闸门】use_fake_publishers 与 SW_TELEGRAM_* 都是服务器
# .env 里的变量。把 SW_USE_FAKE_PUBLISHERS 翻成 false（docs/RISKS.md 第 9 条第 3 步「关闭
# 假发布器」）只需要改 .env 再重启——这条路**根本不经过 update.sh --apply**。整条风险里
# 最危险的那一次切换，恰恰是最可能绕开部署闸门的一次，所以同一道互锁在这里也要有一份。
# （手工 `docker compose up -d` 仍然绕得过去，这一点在 docs/RISKS.md §12 里如实写明。）
#
# 【判定语义】与 scripts/ops/update.sh、scripts/ops/verify.sh 完全一致：
#   use_fake_publishers=true       → 放行，只如实记录一行，不探测确认通道
#   use_fake_publishers=false      → 从严：要求 enabled+ready+polling 三者皆真
#   use_fake_publishers 取不到/不可解析 → 无法证明"什么都不会真发"，按真发布从严裁定
#
# 【R1 与真实后果】内容上线必须由人点一下才真发——在 Telegram 闸门消息上点，或在工作台点
# 「确认发布」（同一后端 core.confirm.confirm_item）。Telegram 是主载体。通道死掉的后果
# **不是**内容越权发出去，恰恰相反：core/scheduler.py:498-505 里的**人工确认闸门**
# （tick_scheduled_publish 中的 `confirm_required(policy) and item.confirmed_at is None`；
# 叫名字不叫序数，理由见 verify.sh 同段注释）会把内容跳过不发（skipped_unconfirmed），
# 内容在排期处静默堆积，再由 SW_CONFIRM_TTL_HOURS（默认 24，
# core/confirm.py）到点自动驳回——发布链路停摆。工作台那个载体不受 Telegram 影响仍可用，
# 但那要求人**知道**主载体死了，所以这里必须是一道显式闸门。
#
# 【收尾话术必须分支化】放行有两种，它们不是一回事：模拟发布器挂着时闸门**根本没探测**
# 通道，此时宣称"确认闸门通道已核验"是假话（而这正是当前生产状态，等于每次重启都说一次）。
# 所以两条路径用不同的退出码回话（$1 / $2，由外层用 %q 传进来），外层据此打不同的收尾行。
fake_publishers="<未知>"
fake_status=0
# 与 verify.sh / update.sh 同一手法：python 解析 JSON、只用退出码回话，shell 只做映射。
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
  # 挂着模拟发布器时什么都不会真发，通道状态不构成阻断。刻意**不去探测**确认通道：
  # 多打一个请求就不再是"这条路径行为不变"。所以也**不能**声称通道已核验。
  printf '  R1 闸门  模拟发布器=true：本次重启后什么都不会真发，人工确认闸门通道不构成阻断（未探测；取证请跑 scripts/ops/verify.sh）\n'
  exit "${gate_unprobed_status}"
fi

if [[ "${fake_publishers}" == "false" ]]; then
  strict_why="真发布已开启（模拟发布器=false）"
  strict_hint="要么修好确认通道，要么把 .env 里的 SW_USE_FAKE_PUBLISHERS 设回 true 后重新执行本脚本。"
else
  # 取不到布尔量时不能再建议"设回 true"——它很可能已经是 true 了，坏掉的是上面那条
  # 容器内解析（docker exec 打嗝 / 容器没起来 / 容器里没有 python3）。
  strict_why="模拟发布器状态取不到（use_fake_publishers=${fake_publishers}），按真发布从严裁定"
  strict_hint="本次连 use_fake_publishers 都没读出来，先别改 SW_USE_FAKE_PUBLISHERS（它可能已经是 true）；先查 docker compose ps 与 docker compose logs core，确认容器起着且容器内 python3 能跑通。"
fi
telegram_json=''
telegram_status=0
telegram_probe_status=0
sw_probe 'http://127.0.0.1:8000/api/v1/system/telegram' 5 >/dev/null 2>/dev/null || telegram_probe_status=$?
if [[ "${telegram_probe_status}" -ne 0 ]]; then
  if [[ "${sw_probe_code}" == "401" ]]; then
    telegram_status=31
  else
    telegram_status=30
  fi
else
  telegram_json="${sw_probe_body}"
  # enabled 必须**直接判**：ready 只看 token+chat_id，压根不看总开关；靠 polling 间接兜
  # 是在赌 poller 的启动条件恰好包含 enabled——那是实现细节不是契约。
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
  23) telegram_summary="ready=false（确认卡根本推不出去）" ;;
  24) telegram_summary="ready=true 但 polling=false（卡能推出去，人点了没有线程去收）" ;;
  25) telegram_summary="总开关 enabled=false（SW_TELEGRAM_ENABLED 关着，build_telegram_notifier() 直接返回 None，一条卡都发不出去）" ;;
  30) telegram_summary="无法获取 /api/v1/system/telegram" ;;
  31) telegram_summary="/api/v1/system/telegram 返回 401（core 已启用 SW_UI_TOKEN，本机未提供或不匹配）" ;;
  *) telegram_summary="/api/v1/system/telegram 解析异常（退出码 ${telegram_status}）" ;;
esac
if [[ "${telegram_status}" -ne 0 ]]; then
  printf '✗ R1 红线闸门未通过：%s，但人工确认闸门通道 %s\n' "${strict_why}" "${telegram_summary}" >&2
  printf '  后果不是内容会越权发出去——恰恰相反：确认闸门等不到人的那一票，内容会被跳过不发（scheduler 记 skipped_unconfirmed），在排期处静默堆积，并在 SW_CONFIRM_TTL_HOURS（默认 24 小时）到点被自动驳回，发布链路等于停摆。\n' >&2
  printf '  兜底：工作台的「确认发布」按钮不受 Telegram 影响，仍可用于确认（同一后端 core.confirm.confirm_item）。\n' >&2
  printf '  %s\n' "${strict_hint}" >&2
  exit "${gate_fail_status}"
fi
printf '  R1 闸门  %s：人工确认闸门通道 %s\n' "${strict_why}" "${telegram_summary}"
# 上面那两条 `R1 闸门  …` 是仅有的放行输出，同时兼作防回归哨兵：它们排在本段所有读 stdin
# 的边界命令之后，一旦从输出里消失，就说明闸门里某条命令又把脚本正文吞了——那时闸门等于
# 没跑，脚本仍会以 0 收尾、外层照样打印"✓ core 已重启"。测试直接断言它们存在。
exit 0
} </dev/null
REMOTE_RESTART
if [[ "${remote_status}" -eq 255 ]]; then
  exit 254
fi
exit "${remote_status}"
} </dev/null
REMOTE_TAIL
}

# ssh(1) 不保留 argv 边界：host 之后的参数会被用单个空格拼成一个字符串发给远端，再由
# 远端登录 shell 重新分词。所以这里自己造那一个字符串，并用 printf '%q' 转义——本仓
# update.sh / verify.sh 是同一手法。注入面为零：三个值都是本脚本自己定义的十进制常量。
# 这样做的收益是退出码协议**只在上面定义一次**，远端零硬编码。
# token 绝不走这条路：它在 stdin 流里（见 restart_remote_script 上方说明）。
#
# stdin 用**进程替换**而不是管道喂：`… | ssh …` 在 `set -o pipefail` 下会让写端的 SIGPIPE
# 有机会顶掉 ssh 自己的退出码，而下面那段重试与协议码判定完全靠 ssh 的退出码。
restart_remote() {
  ssh -o ConnectTimeout=25 "${SSH_ALIAS}" "bash -s -- $(printf '%q ' "${R1_GATE_UNPROBED_STATUS}" "${R1_GATE_FAIL_STATUS}" "${PROBE_UNAUTHORIZED_STATUS}")" \
    < <(sw_ops_emit_token_prologue; restart_remote_script)
}

restarted=0
gate_failed=0
gate_unprobed=0
probe_unauthorized=0
attempt=1
while [[ "${attempt}" -le 2 ]]; do
  restart_status=0
  if restart_remote; then
    restarted=1
    break
  else
    restart_status=$?
  fi
  # 模拟发布器路径：闸门放行但没探测通道。这也是成功，只是收尾话术不同。
  if [[ "${restart_status}" -eq "${R1_GATE_UNPROBED_STATUS}" ]]; then
    restarted=1
    gate_unprobed=1
    break
  fi
  # R1 闸门未通过时重试没有意义：再重启一次也修不好 .env 里的 Telegram 配置，
  # 只会白白多打断一次生产 core。其它失败（含 IAP 断链）保持原有的一次重试。
  if [[ "${restart_status}" -eq "${R1_GATE_FAIL_STATUS}" ]]; then
    gate_failed=1
    break
  fi
  # 401 同理，而且更明确：core 已经在应答了，重启第二次纯属多打断一次生产。
  if [[ "${restart_status}" -eq "${PROBE_UNAUTHORIZED_STATUS}" ]]; then
    probe_unauthorized=1
    break
  fi
  if [[ "${attempt}" -lt 2 ]]; then
    note "IAP 连接中断或重启命令失败，3 秒后重试一次"
    sleep 3
  fi
  attempt=$((attempt + 1))
done

if [[ "${probe_unauthorized}" -eq 1 ]]; then
  die "探针拿到 401：core 已启用 SW_UI_TOKEN，而本机没有匹配的 token" \
    "这不是重启失败——401 证明 core 已经重启完成并在正常应答；缺的是运维侧凭据。" \
    "没有 token 就无法完成探针确认与 R1 红线闸门核验，所以这里如实停手，而不是宣称成功。" \
    "处置：export SW_OPS_UI_TOKEN=<生产 .env 里 SW_UI_TOKEN 的同一个值>，或写进 ~/.dsh-sw/.credentials.yaml（0600）的 sw_ui_token 键，然后重跑本脚本。" \
    "取证：配好后跑 bash scripts/ops/verify.sh，它会把 core 的运行时信息与确认通道状态全部打出来。" \
    "出处：docs/RISKS.md 第 8 条、scripts/ops/README.md「工作台 API token」。"
fi

if [[ "${gate_failed}" -eq 1 ]]; then
  die "R1 确认闸门未通过：core 已重启，但不应就此收工" \
    "真发布已开启（或 use_fake_publishers 取不到）而人工确认闸门通道不可用，具体是哪一格见上面远端的输出。" \
    "这里没有「回滚」可言：restart.sh 不动代码，use_fake_publishers 与 SW_TELEGRAM_* 都在服务器 .env 里。" \
    "但请注意 polling 那一格不是 .env 决定的：长轮询线程由 core/main.py:104 的 lifespan 起、core/telegram.py:981 判活。若怀疑是新版代码把 poller 起崩了，那是代码问题，要走 update.sh 回滚，不是本脚本。" \
    "取证：bash scripts/ops/verify.sh（打印 enabled/configured/ready/chat_configured/polling/detail 全部字段与下一步指引）。" \
    "工作台的「确认发布」按钮不受 Telegram 影响，仍可用于确认（同一后端 core.confirm.confirm_item）。"
fi
[[ "${restarted}" -eq 1 ]] || die "core 重启或探针确认失败"

if [[ "${gate_unprobed}" -eq 1 ]]; then
  # 刻意不说"已核验"：这条路径根本没探测确认通道。当前生产就是这个状态。
  ok "core 已重启、探针恢复 200，R1 闸门已记录（模拟发布器=true，未探测通道）"
else
  ok "core 已重启、探针恢复 200，R1 确认闸门通道已核验"
fi
