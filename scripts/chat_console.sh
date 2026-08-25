#!/usr/bin/env bash
# 对话台启动器 —— 起 profile sw 的 hermes 对话台（社工台特化版 desktop）。
#
#   bash scripts/chat_console.sh            # 起桌面端（默认）
#   bash scripts/chat_console.sh serve      # 只起后端 hermes serve，不开窗口
#   bash scripts/chat_console.sh telegram   # 把同一个 agent 挂到 Telegram（需另一个 bot）
#   bash scripts/chat_console.sh doctor     # 只体检，什么都不起
#
# 这个脚本只做三件事：体检、把 profile 钉死成 sw、把进程起起来。它不装依赖、
# 不改配置。LLM 密钥在 ~/.hermes/profiles/sw/.env 里，由 hermes 自己读，本脚本
# 不碰；**唯一经手的凭据是工作台 API token**（见下面「工作台 API token」一节），
# 它只在本进程内存与进程环境里流转，从头到尾不打印、不进 argv、不落盘。
#
# 红线速览（详见 docs/OPS.md「对话台」节）：
#   - 工具面里**没有**「确认发布」这个函数，也不会有。内容上线只由人在 Telegram
#     闸门或工作台上点一下。
#   - review_approve / review_reject / review_edit 三个写工具被 MCP elicitation
#     闸门守着：桌面端会弹审批面板，人点了「Run」才真的发出那条 POST；点
#     「Reject」= 零写请求，稿件原地不动。
#   - 客户端不支持确认交互时 fail-closed（不执行，不是降级放行）。

set -euo pipefail

MODE="${1:-desktop}"

# ── 路径（写死，因为对话台就住在这一个 fork 里）────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DESKTOP_ROOT="${SW_DESKTOP_ROOT:-$HOME/project/social_workflow/sw-hermes-desktop}"
DESKTOP_APP="$DESKTOP_ROOT/apps/desktop"
# 任务书要求显式用这一份运行时：0.20.4 editable，指向本 fork。
VENV_HERMES="$DESKTOP_ROOT/.venv/bin/hermes"
# 桌面端 spawn 的是 `<python> -m hermes_cli.main serve`，要的是解释器而不是
# 那个 console-script shim；两者出自同一个 .venv，所以钉住 VENV_HERMES 就等于
# 钉住了 VENV_PYTHON（下面会一起校验）。
VENV_PYTHON="$DESKTOP_ROOT/.venv/bin/python"

PROFILE="${SW_HERMES_PROFILE:-sw}"
# HERMES_HOME 必须显式钉住。桌面端的 resolveHermesHome()（electron/main.ts:690）
# 在传了 HERMES_DESKTOP_USER_DATA_DIR 时会把 HERMES_HOME 挪到
# <userData>/hermes-home 底下——而 profile sw 住在 ~/.hermes/profiles/sw，
# 于是后端会以 "Profile 'sw' does not exist" 退出。传了这一条才对得上。
HERMES_HOME_DIR="${SW_HERMES_HOME:-$HOME/.hermes}"
PROFILE_DIR="$HERMES_HOME_DIR/profiles/$PROFILE"
CORE_URL="${SW_MCP_BASE_URL:-http://127.0.0.1:8000}"
SERVE_PORT="${SW_HERMES_SERVE_PORT:-9119}"

# 桌面端从 <userData>/active-profile.json 读该用哪个 profile（electron/main.ts
# readActiveDesktopProfile）。默认就是正式安装用的那个目录。
if [[ "$(uname -s)" == "Darwin" ]]; then
  DEFAULT_USER_DATA="$HOME/Library/Application Support/social_workflow"
else
  DEFAULT_USER_DATA="${XDG_CONFIG_HOME:-$HOME/.config}/social_workflow"
fi
USER_DATA="${SW_CHAT_USER_DATA:-$DEFAULT_USER_DATA}"

die() { printf '\n✗ %s\n' "$1" >&2; shift; for line in "$@"; do printf '  %s\n' "$line" >&2; done; exit 1; }
note() { printf '  %s\n' "$1"; }

# ── 工作台 API token ───────────────────────────────────────────────────────
# core 一旦配上非空 `SW_UI_TOKEN`（`core/api/common.py::require_token`），除 `/auth/login`
# 外的全部 `/api/v1/*` 都要求 `Authorization: Bearer`。对话台有**两处**要它：
#   ① 下面那个 core 探活（打的是 `/api/v1/system/info`，挂在 AuthGuard 下）；
#   ② workbench MCP server（`scripts/workbench_mcp.py:179` 从 `SW_UI_TOKEN` 读、
#      `:200-201` 注入头、`:93` 备了 401 的提示文案）。
# 背景与上线顺序见 docs/RISKS.md 第 8 条。
#
# 【取值口径与 scripts/ops/ 那四个探针脚本完全一致，实现直接复用同一份库】
# 不另写一套：`scripts/ops/ui_token.sh` 已经把字符集白名单、xtrace 泄漏防护、凭据文件的
# 极窄解析、「已导出就采信哪怕空串」这些细节全处理过，每一条都有实测理由写在它自己的注释
# 里。必须在 die/note 之后 source：库里报错走调用方的 die()。
# `disable=SC1091` 只是让不带 `-x` 的 shellcheck 也能退 0；`source=` 那行保证带 `-x` 时
# 它照样跟进去做跨文件检查（`shellcheck -x scripts/chat_console.sh`）。
# shellcheck source=scripts/ops/ui_token.sh
# shellcheck disable=SC1091
. "$SCRIPT_DIR/ops/ui_token.sh"

# 本脚本只在库外面加一层：**多认一个环境变量名 `SW_UI_TOKEN`**，且它优先级最高。
# 理由是这个名字就是 MCP 子进程真正读的那个名字（workbench_mcp.py:179）——人在 shell 里
# export 了它，本脚本却无视它、还反手用别的值覆盖掉，比多认一个名字糟得多。
# 于是对话台侧的优先级是：`SW_UI_TOKEN` > `SW_OPS_UI_TOKEN` > `~/.dsh-sw/.credentials.yaml`
# 的 `sw_ui_token` 键。前两者**只要已导出就采信，哪怕是空串**（空串 = 本次显式不带 token，
# 用来复现未鉴权路径），语义与 ops 侧逐字相同。
#
# 【为什么是一个函数，而不是调用点上的一行赋值】`ui_token.sh` 里「所有经手值的函数都不收
# 参数、只读写全局」那一段说的就是这条硬约束：`SW_OPS_UI_TOKEN="$SW_UI_TOKEN"` 写在调用点
# 时，`bash -x` 会原样打出 `+ SW_OPS_UI_TOKEN=<token>`。搬运只能发生在 xtrace 守卫内部。
SW_CHAT_TOKEN_SOURCE_OVERRIDE=""
# SC2034：`SW_OPS_UI_TOKEN` 在本文件里确实没人读——读它的是上面 source 进来的
# `ui_token.sh`（`_sw_ops_load_ui_token_impl`）。不带 `-x` 的 shellcheck 看不见那一侧。
# shellcheck disable=SC2034
_sw_chat_adopt_env_ui_token_impl() {
  SW_CHAT_TOKEN_SOURCE_OVERRIDE=""
  [[ -n "${SW_UI_TOKEN+x}" ]] || return 0
  SW_OPS_UI_TOKEN="$SW_UI_TOKEN"
  SW_CHAT_TOKEN_SOURCE_OVERRIDE="环境变量 SW_UI_TOKEN"
}
sw_chat_adopt_env_ui_token() { sw_ops_xtrace_guard _sw_chat_adopt_env_ui_token_impl; }

# 把取到的值放进本进程环境的 `SW_UI_TOKEN`——那是通往 MCP 子进程那条链的入口
# （下面 `sw_chat_token_wiring` 上方写了整条链）。同样必须包成函数：
# `export SW_UI_TOKEN=<值>` 写在调用点会被 xtrace 原样打出来。
#
# 【取不到时也照样 export 一个空串】这样「体检报的来源」与「子进程实际拿到的东西」永远是
# 同一件事，不会出现「脚本说没配、子进程却从别处捡到一个」的谜题。空串对下游与未设置完全
# 等价：`workbench_mcp.py:179` 是 `os.environ.get("SW_UI_TOKEN") or ""`，
# `:200-201` 只在非空时才加头。
_sw_chat_export_ui_token_impl() {
  export SW_UI_TOKEN="$SW_OPS_UI_TOKEN_VALUE"
}
sw_chat_export_ui_token() { sw_ops_xtrace_guard _sw_chat_export_ui_token_impl; }

# profile 的 config.yaml 有没有把 `SW_UI_TOKEN` 转发给 MCP 子进程。
#
# 【这一步不能省：token 不会自己走进 MCP 子进程】hermes 起 stdio MCP server 时会**过滤**
# 父进程环境（`sw-hermes-desktop/tools/mcp_tool.py::_build_safe_env`，放行名单只有
# PATH/HOME/USER/LANG/LC_ALL/TERM/SHELL/TMPDIR、一批 Windows 变量、`XDG_*`、以及被外部密钥
# 源标记过的键）。`SW_UI_TOKEN` 不在名单里，**光在本脚本里 export 是到不了子进程的**——
# 这也正是 `SW_MCP_BASE_URL` 当初非写进 config.yaml 不可的原因。
# 唯一的注入口是 profile 的 config.yaml：`mcp_servers.workbench.env` 下的值会做 `${VAR}`
# 插值，所以那一行必须写成：
#     SW_UI_TOKEN: '${SW_UI_TOKEN}'
# **值本身绝不写进 config.yaml**：那是个 0644 的普通文件，凭据只该待在 0600 的凭据文件里。
#
# 【插值有**两层**，别只看第二层——孤立读第二层会得出错的结论】
#   第 1 层 `hermes_cli/config.py::_env_expand_match`（由 `_expand_env_vars` 在
#           `:2695/:2704` 递归调用，`load_config()` 里就跑完了）。结尾是
#           `return os.environ.get(inner, raw)` —— **is-set 语义**：变量只要**已设置**，
#           哪怕是空串也拿它替换；**未设置**才保留字面量。
#   第 2 层 `tools/mcp_tool.py::_interpolate_env_vars` → `agent/secret_scope.py::get_secret`，
#           那行是 `_get_secret(name, m.group(0)) or m.group(0)` —— truthiness 语义。
# **第 1 层先跑，占位符在它那儿就已经被吃掉了**，所以第 2 层那个 `or` 回落在实际链路上
# 永远轮不到。只读第 2 层会以为"空串也保留字面量"，那是错的（本机实测证伪，见下）。
#
# 【反过来那个坑一并检出来——三种输入状态，两种 401 症状不同】
# 「config.yaml 写了转发、本机却没取到 token」时，MCP 子进程实际拿到什么（本机实测）：
#   · `SW_UI_TOKEN` **未设置**  → 子进程拿到字面量 `${SW_UI_TOKEN}`，会把它当真 token
#     发出去；core 应答 401 **`token 不正确`**。
#   · `SW_UI_TOKEN` **是空串**  → 子进程拿到 `''`（**不是**字面量），不发 Authorization 头；
#     core 应答 401 **`缺少 Authorization`**。
#   · 有值 → 真值，200。
# 两种 401 的文案不同，这正是排查时用来区分「转发写了但变量没设」与「变量设了但为空」的
# 线索。本脚本恒定 export（见 sw_chat_export_ui_token），所以实际落在第二种；但人手动起
# hermes、或从别处继承环境时第一种照样会发生，所以两种都要说。
#
# 判定刻意做得很窄（只按字面量 grep，不解析 YAML），与 ui_token.sh 读凭据文件同一个取舍：
# 本仓不给 shell 引入 YAML 依赖，看不懂就当没配，绝不猜。
sw_chat_token_wiring() {
  local cfg="$1"
  # SC2016：要找的就是**字面量** ${SW_UI_TOKEN}，单引号 + -F 是对的，别改成双引号。
  # shellcheck disable=SC2016
  if grep -qF '${SW_UI_TOKEN}' "$cfg" 2>/dev/null; then
    printf 'forward'
  elif grep -qE '^[[:space:]]*SW_UI_TOKEN:' "$cfg" 2>/dev/null; then
    printf 'pinned'
  else
    printf 'absent'
  fi
}

# core 探活。token 经 `curl --config -` 的**配置流**注入，curl 的 argv 里只有 `--config -`；
# `printf` 是 bash 内建，不 fork，不产生自己的 `/proc/*/cmdline`。`-q` 必须是第一个参数才
# 生效——不加时远端/本机家目录里一份带 `trace-ascii <file>` 的 `~/.curlrc` 就能把
# `Authorization: Bearer <token>` 明文写进磁盘文件（理由与实测见 scripts/ops/ui_token.sh 头部）。
#
# 【为什么不能再用裸 `curl -sf`】`/api/v1/system/info` 挂在 AuthGuard 下。core 一旦启用
# token，不带头的探针会拿到 401，而 `-sf` 只会说"失败了"——体检就会把一个活得好好的 core
# 报成"没起来"，把人引向完全错误的方向。docs/RISKS.md §8.4 记的正是运维脚本上同一个坑。
# 所以这里把状态码取回来，让 401 与"连不上"分得开。
_sw_chat_probe_core_impl() {
  local code=""
  if [[ -n "$SW_OPS_UI_TOKEN_VALUE" ]]; then
    code="$(printf 'header = "Authorization: Bearer %s"\n' "$SW_OPS_UI_TOKEN_VALUE" \
      | curl -q -s -o /dev/null -w '%{http_code}' --max-time 3 --config - "$1" 2>/dev/null)" || code=""
  else
    code="$(curl -q -s -o /dev/null -w '%{http_code}' --max-time 3 "$1" 2>/dev/null)" || code=""
  fi
  [[ "$code" =~ ^[0-9]{3}$ ]] || code="000"
  printf '%s' "$code"
}
sw_chat_probe_core() { sw_ops_xtrace_guard _sw_chat_probe_core_impl "$@"; }

# 取用在任何网络动作之前完成：token 字符集不合法要当场报错退出。
# 未配置时这三行什么都不做，后续输出与改造前逐字一致。
sw_chat_adopt_env_ui_token
sw_ops_load_ui_token
if [[ -n "$SW_OPS_UI_TOKEN_SOURCE" && -n "$SW_CHAT_TOKEN_SOURCE_OVERRIDE" ]]; then
  # 只改「来源」这行标签文本，不碰值——所以不需要 xtrace 守卫。
  SW_OPS_UI_TOKEN_SOURCE="$SW_CHAT_TOKEN_SOURCE_OVERRIDE"
fi
sw_chat_export_ui_token

# ── 体检 ───────────────────────────────────────────────────────────────────

printf '对话台体检\n'

[[ -x "$VENV_HERMES" ]] || die "找不到 hermes 运行时：$VENV_HERMES" \
  "对话台不用 PATH 上的 hermes（那是用户 default profile 的），只用这一份。" \
  "修：cd $DESKTOP_ROOT && uv venv && uv pip install -e ."
[[ -x "$VENV_PYTHON" ]] || die "找不到解释器：$VENV_PYTHON" \
  "与上面同一个 .venv，缺了说明 venv 是坏的。重建：cd $DESKTOP_ROOT && uv venv && uv pip install -e ."
note "运行时  $($VENV_HERMES --version 2>/dev/null | head -1) · $VENV_HERMES"

# MCP SDK 在不在。**这一条必须查**：hermes 的 MCP 发现被 `_MCP_AVAILABLE` 守着，
# 缺 mcp 包时整段是**静默 no-op**——没有报错、没有红字，只是
# mcp__workbench__* 工具集体不存在（一个都没有），日志里唯一的线索是一行
# 「Background MCP discovery completed with zero connected servers」。
# P15.H5 实测时就被这个坑了一次（venv 重建后掉了 mcp extra）。
if ! "$VENV_PYTHON" -c "import mcp" >/dev/null 2>&1; then
  die "运行时缺 mcp SDK（$VENV_PYTHON 里 import mcp 失败）" \
    "后果：MCP 发现整段静默 no-op，工作台的 mcp__workbench__* 工具会**一个都不存在**，" \
    "而界面上不会有任何报错——只有日志里一行 'zero connected servers'。" \
    "修：cd $DESKTOP_ROOT && uv pip install --python .venv/bin/python 'mcp==2.0.0' 'httpx2==2.7.0' 'starlette==1.3.1'" \
    "（这三个就是 hermes pyproject.toml 的 [mcp] extra 原样。）"
fi
note "MCP SDK 已装"

[[ -f "$PROFILE_DIR/config.yaml" ]] || die "profile '$PROFILE' 不存在（缺 $PROFILE_DIR/config.yaml）" \
  "建：$VENV_HERMES profile create ${PROFILE}，然后照 docs/OPS.md「对话台」节填 config.yaml。"
note "profile $PROFILE · $PROFILE_DIR"

# MCP server 的脚本在不在（config.yaml 里那条 stdio 命令指向它）
MCP_SCRIPT="$SCRIPT_DIR/workbench_mcp.py"
[[ -f "$MCP_SCRIPT" ]] || die "找不到 workbench MCP server：$MCP_SCRIPT"
note "MCP     $MCP_SCRIPT"

# 工作台 API token：**只报来源，绝不报值，也不报长度**（长度也是信息），与四个 ops 脚本
# 同一体例。没取到 token 且 config.yaml 也没写转发时一个字都不打——那是 core 未开鉴权的
# 常态形态，输出必须与改造前逐字一致。
TOKEN_WIRING="$(sw_chat_token_wiring "$PROFILE_DIR/config.yaml")"
if [[ -n "$SW_OPS_UI_TOKEN_SOURCE" ]]; then
  # 花括号是必需的：紧跟其后的是全角 `）`，不加时 bash 会把它的首字节当成变量名的一部分
  # （实测报 `SW_OPS_UI_TOKEN_SOURCE<乱码>: unbound variable`）。
  note "token   已加载（来源：${SW_OPS_UI_TOKEN_SOURCE}）；值不打印、不进 argv"
  if [[ "$TOKEN_WIRING" == "forward" ]]; then
    note "        config.yaml 已把它转发给 MCP 子进程"
  else
    printf '\n⚠ 取到了 token，但 MCP 子进程收不到它\n' >&2
    cat >&2 <<EOF
  hermes 起 stdio MCP server 时会过滤父进程环境（tools/mcp_tool.py::_build_safe_env
  的放行名单里没有 SW_UI_TOKEN），所以本脚本 export 的值到不了 workbench_mcp.py。
  后果：core 开了鉴权时，每个 mcp__workbench__* 工具都会答 401，而本脚本的探活是好的。

  修：在 $PROFILE_DIR/config.yaml 的 mcp_servers.workbench.env 底下写成

      SW_UI_TOKEN: '\${SW_UI_TOKEN}'

  写的是这个**字面的占位符**，不是 token 的值——config.yaml 是 0644 的普通文件，
  凭据只该待在 0600 的 ~/.dsh-sw/.credentials.yaml 里。hermes 会用本进程环境里的
  同名变量把它插值掉。改完重开对话台。
EOF
  fi
elif [[ "$TOKEN_WIRING" == "forward" ]]; then
  printf '\n⚠ config.yaml 写了 SW_UI_TOKEN 转发，但本机没取到 token\n' >&2
  cat >&2 <<EOF
  这个组合在 core 开了鉴权之后一定 401（未开鉴权时无害）。两种成因、两种症状，
  别搞混——它们在 core 那边的 401 文案不一样，那正是区分它们的线索：
    · SW_UI_TOKEN **未设置**：hermes 第一层插值保留字面量
      （hermes_cli/config.py::_env_expand_match 结尾的 os.environ.get(inner, raw)），
      MCP 子进程会拿一串 \${SW_UI_TOKEN} 当真 token 发出去 → core 答 **token 不正确**。
    · SW_UI_TOKEN **是空串**：拿到的是 ''（**不是**字面量），不发 Authorization 头
      → core 答 **缺少 Authorization**。本脚本恒定 export，所以你现在多半是这一种。

  二选一：
    ① 配上 token：在 ~/.dsh-sw/.credentials.yaml（0600）加一行 sw_ui_token: <生产
       .env 里 SW_UI_TOKEN 的同一个值>，或 export SW_UI_TOKEN=<同一个值>；
    ② 还没到启用的时候：把 $PROFILE_DIR/config.yaml 里那一行改回 SW_UI_TOKEN: ''。
EOF
fi

# core 活着没。这是最常见的"起来了但什么都问不出来"的原因，所以提示写全。
CORE_CODE="$(sw_chat_probe_core "$CORE_URL/api/v1/system/info")"
if [[ "$CORE_CODE" == "200" ]]; then
  note "core    $CORE_URL 可达"
elif [[ "$CORE_CODE" == "401" ]]; then
  printf '\n⚠ core 活着，但拒绝了这次探活（%s 返回 401）\n' "$CORE_URL" >&2
  cat >&2 <<EOF
  根因  core 已启用 SW_UI_TOKEN 鉴权（core/api/common.py::require_token，对除
        /auth/login 外的全部 /api/v1/* 生效），本次没带上匹配的 token。
        **这不是 core 故障**——它正常应答了 401。
  处置  在本机二选一，然后重开对话台：
        ① export SW_UI_TOKEN=<core 那边 SW_UI_TOKEN 的同一个值>
        ② 在 ~/.dsh-sw/.credentials.yaml（0600）里加一行：sw_ui_token: <同一个值>
        再确认 $PROFILE_DIR/config.yaml 的 mcp_servers.workbench.env 里有
        SW_UI_TOKEN: '\${SW_UI_TOKEN}'，否则 MCP 子进程仍然拿不到它。
  自查  已经配了还是 401 = 值不匹配。以 core 那边 .env 里 SW_UI_TOKEN 的原文为准，
        别把引号或行尾空格一起复制。
  出处  docs/RISKS.md 第 8 条、scripts/ops/README.md「工作台 API token」
EOF
else
  printf '\n⚠ core 没起来（%s 连不上）\n' "$CORE_URL" >&2
  [[ "$CORE_CODE" == "000" ]] || printf '  （实际拿到 HTTP %s——不是连不上，是那个地址上的服务答了别的东西）\n' "$CORE_CODE" >&2
  cat >&2 <<EOF
  对话台照样能开，但每个 mcp__workbench__* 工具都会答「连不上工作台」——
  看板、审核队列、排期一个都问不出来。

  起一份（选一个）：
    正式实例   cd $REPO_ROOT && uv run uvicorn core.main:app --host 127.0.0.1 --port 8000
    隔离实例   cd $REPO_ROOT && bash ui/e2e/serve.sh 8000
               （独立 SQLite + fake 发布器，不碰 data/；拿它练手最安全）

  core 在别的地址：SW_MCP_BASE_URL=http://host:port bash scripts/chat_console.sh
  注意 profile 的 config.yaml 里 mcp_servers.workbench.env.SW_MCP_BASE_URL 也要一致。
EOF
fi

if [[ "$MODE" == "doctor" ]]; then
  printf '\n体检完毕（doctor 模式，什么都没起）。\n'
  exit 0
fi

# ── serve：只起后端 ────────────────────────────────────────────────────────

if [[ "$MODE" == "serve" ]]; then
  cat <<EOF

起 hermes serve（headless，JSON-RPC/WebSocket）
  地址  127.0.0.1:$SERVE_PORT
  说明  桌面端平时会**自己** spawn 一份 serve（--port 0，随机端口），所以这条
        路只在你要外挂前端或调后端时才用；日常开对话台请直接跑 desktop 模式。
  停    Ctrl-C

EOF
  # SW_UI_TOKEN 已经在本进程环境里（sw_chat_export_ui_token），`env` 原样带过去。
  # **刻意不写成 `env SW_UI_TOKEN=… `**：那会把值放进 env(1) 的 argv，而 argv 是
  # 世界可读的（`ps` / `/proc/*/cmdline`），红线 R5 明确禁止。
  exec env HERMES_HOME="$HERMES_HOME_DIR" \
    "$VENV_HERMES" --profile "$PROFILE" serve --host 127.0.0.1 --port "$SERVE_PORT"
fi

# ── telegram：把同一个 agent 挂到 Telegram 上 ─────────────────────────────

if [[ "$MODE" == "telegram" ]]; then
  cat <<EOF

起 hermes gateway（Telegram 平台）
  profile  $PROFILE
  工具面   和桌面端**同一份** —— profile 的 mcp_servers.workbench.tools.include，
           改稿走 review_edit / review_approve / review_reject / content_reschedule。
  停       Ctrl-C

  ⚠ **必须用一个和确认闸门不同的 bot**。社交工作流自己在轮询 TELEGRAM_BOT_TOKEN
    那个 bot（core/telegram.py 的确认卡闸门）；两个进程轮询同一个 token，Telegram
    会回 error_code=409，两边都收不全消息。scripts/ops/verify.sh 有一条专门盯 409。
    没配过就先跑：$VENV_HERMES --profile $PROFILE gateway setup

  ⚠ 红线 R1 不受影响：白名单里**没有** confirm 工具，这个 agent 改得了稿、
    批得了审，但"确认发布"那一下仍然只能由人在闸门卡片或工作台点。

EOF
  # 同 serve 模式：SW_UI_TOKEN 已在本进程环境里，`env` 原样带过去，
  # 绝不写成 `env SW_UI_TOKEN=…`（argv 世界可读，红线 R5）。
  exec env HERMES_HOME="$HERMES_HOME_DIR" \
    "$VENV_HERMES" --profile "$PROFILE" gateway run
fi

if [[ "$MODE" != "desktop" ]]; then
  die "不认识的模式：$MODE" "用法：bash scripts/chat_console.sh [desktop|serve|telegram|doctor]"
fi

# ── desktop：起 Electron ───────────────────────────────────────────────────

[[ -f "$DESKTOP_APP/dist/index.html" ]] || die "桌面端还没构建（缺 $DESKTOP_APP/dist/index.html）" \
  "构建：cd $DESKTOP_APP && npm run build" \
  "（npm 依赖要从 monorepo 根装：cd $DESKTOP_ROOT && npm install）"
[[ -d "$DESKTOP_ROOT/node_modules" ]] || die "monorepo 依赖没装" "装：cd $DESKTOP_ROOT && npm install"

# 把 profile 钉死成 sw。桌面端读的就是这个文件；没有它会回落到用户的 default
# profile —— 那是另一套 SOUL/config，不该被对话台碰。
mkdir -p "$USER_DATA"
CURRENT_PROFILE=""
if [[ -f "$USER_DATA/active-profile.json" ]]; then
  CURRENT_PROFILE="$(sed -n 's/.*"profile"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$USER_DATA/active-profile.json" | head -1)"
fi
if [[ "$CURRENT_PROFILE" != "$PROFILE" ]]; then
  printf '{\n  "profile": "%s"\n}\n' "$PROFILE" > "$USER_DATA/active-profile.json"
  note "active-profile.json → ${PROFILE}（原来是 ${CURRENT_PROFILE:-<未设置>}）"
else
  note "active-profile.json 已是 $PROFILE"
fi

cat <<EOF

起对话台（Electron）
  profile   $PROFILE
  运行时    ${VENV_PYTHON}（= $VENV_HERMES 的同一个 .venv）
  core      $CORE_URL
  HERMES_HOME $HERMES_HOME_DIR
  用户数据  $USER_DATA
  停        关窗口，或 Ctrl-C

EOF

cd "$DESKTOP_APP"
# HERMES_DESKTOP_HERMES_ROOT 钉住本 fork（否则会去找 PATH 上的 hermes 或托管安装）；
# HERMES_DESKTOP_IGNORE_EXISTING=1 明确不复用 PATH 上那份；
# HERMES_DESKTOP_PYTHON 钉住 .venv 的解释器。三者一起保证跑的是本 fork 的代码。
#
# SW_UI_TOKEN 走的是**进程环境继承**这条路，不出现在下面任何一行 argv 里（红线 R5）：
# 本脚本 export → env(1) → Electron → 桌面端 spawn 后端时 `env: { ...process.env, … }`
# （apps/desktop/electron/main.ts:10524）→ hermes serve 的 os.environ → 它用这个值把
# config.yaml 里 mcp_servers.workbench.env 的 `${SW_UI_TOKEN}` 插值掉 →
# StdioServerParameters.env → workbench_mcp.py。链上每一跳都是 environ，不是 cmdline。
exec env \
  HERMES_HOME="$HERMES_HOME_DIR" \
  HERMES_DESKTOP_HERMES_ROOT="$DESKTOP_ROOT" \
  HERMES_DESKTOP_IGNORE_EXISTING=1 \
  HERMES_DESKTOP_PYTHON="$VENV_PYTHON" \
  HERMES_DESKTOP_USER_DATA_DIR="$USER_DATA" \
  npx electron .
