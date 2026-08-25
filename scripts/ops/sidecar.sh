#!/usr/bin/env bash
# 用途：受**白名单**约束地在生产上就地生成 sidecar 配置、按 profile 起停单个 sidecar，
#       并在启动之前用**远端 `docker compose config` 解析后的 `host_ip`** 强制确认端口只绑回环。
#
# 【本脚本的第一目的是那道端口闸门，不是"把容器起起来"】docs/RISKS.md 第 15 条刚查出
# `xhs-downloader` 与每账号小红书 sidecar 曾经绑 `0.0.0.0`。生产是合租机器（§8.2：同机还跑着
# 一堆与本项目无关的同租户容器），而**绑 0.0.0.0 的暴露面不止"同网段的机器"**：
# 同一台宿主机上其他 docker 网络里的容器，经默认网关（`docker0`，通常 `172.17.0.1`）就直接
# 够得着——这条是 §15.2 用 `nginx:alpine` 做对照实验**实测**出来的，不是推断。
# 每账号小红书 sidecar 里装着该账号扫码登录后的 cookies，它的 HTTP 接口就是"以这个身份发
# 笔记"，而鉴权只有一个默认为空的 `AUTH_TOKEN`（留空即不鉴权）。所以"起 sidecar"这件事
# 本身没什么难的，难的是**起之前必须证明它只绑回环**——本脚本的绝大部分复杂度都在这上面。
#
# 【为什么不需要新造"把本机文件推到生产"的能力】docs/RISKS.md 第 9 条第 1 步原本把卡点记成
# "配置文件被 .gitignore 排除 ⇒ 不会随 update.sh 的 git 快进带过去，必须直接在生产主机上就位；
# 而 scripts/ops/ 里没有一个能把本机文件推到生产"。那句话至今仍然为真，但**推论不成立**：
#   · `sidecars/trendradar/config.example.yaml` 与 `frequency_words.example.txt` **都已提交进仓库**，
#     会随 `update.sh` 的快进部署到生产（`sidecars/trendradar/config/.gitkeep` 也在版本控制里，
#     所以那个目录本身也会被创建出来）；
#   · 这两份样例里**没有任何凭据**（`key|token|secret|password|webhook|credential` 六个关键词
#     零命中），`.gitignore:8-10` 排除 `config/*` 的理由写在旁边，是"可能被人填上自部署 newsnow
#     地址等本地信息"——**预防性排除，不是因为必须填密钥**。
# 所以正解是**在生产上从已部署的模板就地生成**：零传输、零新增攻击面、确定性可复现，
# 与 `scripts/ci_local.sh` 的 compose job 本地那几行 `cp *.example.* → config/` 完全同一个口径。
# 本脚本**没有**、也不打算有"推任意文件到生产"的能力。
#
# 【三个 sidecar，只管两个，第三个是有意排除不是暂不支持】见下面 sw_sidecar_policy()。
# `mpt`（MoneyPrinterTurbo，出片链路）需要素材源 key 且依赖模型网关，本轮明确不启用；
# 它在 `--status` 里照样如实列出来，但 `--up` / `--down` / `--materialize` 一律拒绝。
#
# 【本脚本刻意**不**套 R1 闸门】起 sidecar 不改变发布语义：`use_fake_publishers` 一个字节都没动，
# 假发布器该挂着还挂着。给它套一道"确认通道必须活着"的互锁，会变成又一处"为对称而对称"的
# 闸门——闸门的正当性来自它挡住的那件坏事，而这里没有那件坏事。`--status` 会如实带上当前
# `use_fake_publishers` 的值供人对照，仅此而已。
#
# 【本脚本一律不碰 core】不重启、不重建、不 build。唯一用到 core 容器的地方是
# `docker compose exec -T core python3` 解析 JSON——那是**只读**的，与 status.sh / verify.sh /
# restart.sh 同一手法（本仓从不假设远端宿主机上有 python3，JSON 一律进容器解析）。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH_ALIAS="${SW_OPS_SSH_ALIAS:-${SW_TUNNEL_SSH_ALIAS:-workbench-iap}}"

# 远端退出码协议（**这里是唯一定义处**，全部用 printf '%q' 传给远端当位置参数，远端不硬编码
# 任何一个字面量）。都要避开 ssh 保留的 255，也避开远端脚本可能自然产生的 1/2。
#
# 【为什么从 50 起编】20/30-43 已经被 restart.sh / status.sh / env_set.sh 占着各自的含义
# （尤其 41 在 scripts/ops/ 下恒等于"401 未授权"）。同一个数字在这个目录里只许有一种含义，
# 所以本脚本整段另起一档，不去挤那些号。
SIDECAR_PORT_GATE_STATUS=50      # 端口闸门判红：解析出来的发布地址不是回环，拒绝启动
SIDECAR_PORT_UNKNOWN_STATUS=51   # 端口闸门**判不了**（config 取不到 / 解析不了 / 服务不在结果里）
SIDECAR_CONFIG_MISSING_STATUS=52 # --up：该 sidecar 的配置文件没就位（先跑 --materialize）
SIDECAR_UP_STATUS=53             # `docker compose up -d <服务>` 自己失败
SIDECAR_PROBE_STATUS=54          # 起是起了，但探不到它在监听
SIDECAR_TEMPLATE_MISSING_STATUS=55 # --materialize：生产上那份**模板**不存在（部署没带过去？）
SIDECAR_WRITE_STATUS=56          # --materialize：生成失败
SIDECAR_RUNTIME_BIND_STATUS=57   # 起完之后**实际**绑定不是回环：已当场停掉并删除该容器
SIDECAR_TABLE_MISMATCH_STATUS=58 # 脚本里那张服务表与远端解析结果对不上，拒绝按猜测行事

die() { printf '\n✗ %s\n' "${1}" >&2; shift; for line in "$@"; do printf '  %s\n' "${line}" >&2; done; exit 1; }
# 与 die 逐字同样的输出，只是退出码由调用方给（手法与 scripts/ops/env_set.sh 的 die_with 同源）。
#
# 【退出码的分工，一条规则说完】本机参数校验失败一律 `die`（退 1，与 scripts/ops/ 其余脚本
# 一致）；**远端协议码原样透出**（就是上面那组 `SIDECAR_*_STATUS` 常量），因为它们各自回答一个
# 不同的问题——"闸门判红"与"闸门判不了"的处置动作完全不同，让调用方靠 grep 中文文案去区分
# 是不可接受的。
# ssh 自己的 255 也原样透出，好与远端的 254（远端脚本自身退 255 的规范化结果）分开。
die_with() { local rc="${1}"; shift; printf '\n✗ %s\n' "${1}" >&2; shift; for line in "$@"; do printf '  %s\n' "${line}" >&2; done; exit "${rc}"; }
note() { printf '  %s\n' "${1}"; }
warn() { printf '  ⚠ %s\n' "${1}"; }
ok() { printf '  ✓ %s\n' "${1}"; }

# 工作台 API token 的取用与注入（含 argv 零暴露的理由），见该文件头部说明。
# 必须在 die/note 之后 source：库里报错走调用方的 die。
# shellcheck source=scripts/ops/ui_token.sh
. "${SCRIPT_DIR}/ui_token.sh"

usage() {
  cat >&2 <<'USAGE'
用法：
  bash scripts/ops/sidecar.sh --status
      只读：逐个 sidecar 报容器状态、**解析后的**端口绑定、配置文件是否就位；
      并如实带上当前 use_fake_publishers 的值供人对照。不起、不停、不写任何文件。

  bash scripts/ops/sidecar.sh --materialize <sidecar>
      在**生产上**从已部署的模板就地生成该 sidecar 的配置文件。
      **已存在则不覆盖**（人可能在上面填过自部署地址等本地信息），如实报"已存在，跳过"。

  bash scripts/ops/sidecar.sh --up <sidecar>
      按该 sidecar 的 profile 起它一个。启动**之前**先过端口回环闸门（见下），
      启动**之后**再核一次实际绑定并探它是不是真在监听。

  bash scripts/ops/sidecar.sh --down <sidecar>
      停掉并删除该 sidecar 的容器。只按**显式服务名**操作，永不执行裸 `docker compose down`。

白名单（写死在脚本里，**不接受运行时扩展**）：
  trendradar        热榜聚合。两个配置文件缺任一，上游 entrypoint.sh 直接 exit 1。
                    8080 上是 `python -m http.server` 挂 /app/output，**没有 REST API**。
  xhs-downloader    小红书采集。无配置依赖。本仓侧**连一个 token 变量都不存在**。

  mpt               **有意排除，不是暂不支持**：出片链路依赖模型网关，且需要用户自己去
                    pexels/pixabay 申请素材源 key，本轮明确不启用。`--status` 仍会如实列出它。

端口回环闸门（`--up` 的硬前置，本脚本最重要的一道）：
  校验依据是**远端 `docker compose config` 解析之后的 `host_ip`**，不是 grep 源文件——
  变量插值、`docker-compose.override.yml` 叠加要由 compose 自己算。只放行 127.0.0.1 与 ::1，
  其余（含解析不出来、服务不在结果里）一律拒绝启动，fail-closed。
USAGE
  exit 2
}

# --------------------------------------------------------- 按服务决定的策略（显式表格）
#
# 【为什么是写死的表而不是"从 compose 文件里读出来"】三条理由：
#   ① 白名单的意义就在于它是**人审过的名单**。从 compose 里枚举服务名，等于谁往
#      docker-compose.yml 里加一个服务，本工具就自动获得起它的能力——那不是白名单。
#   ② `container_port` / `probe_path` 这两格是"探它活没活"的判据，compose 文件里没有。
#   ③ 表与远端解析结果对不上时要**拒绝**而不是猜（退 58）。有一张本地的表，才谈得上对照。
# 新增一个 sidecar 必须在这里补齐六格，漏一格函数返回 1、调用方当场 die。
#
# | 服务 | profile | 容器端口 | 探测路径 | 本工具管不管 | 配置文件（目标=模板，均相对 ~/social_workflow） |
# |---|---|---|---|---|---|
# | trendradar     | sourcing | 8080 | / | 是 | config/config.yaml=config.example.yaml 等两份 |
# | xhs-downloader | xhs      | 5556 | / | 是 | 无 |
# | mpt            | video    | 8080 | / | **否** | config.toml=config.example.toml（只用于 --status 报告） |
SW_SIDECAR_WHITELIST="trendradar xhs-downloader"
# --status 报告面比白名单宽一格：mpt 不归本工具起停，但"它现在什么样"该让人看见。
SW_SIDECAR_REPORTED="trendradar xhs-downloader mpt"

POLICY_PROFILE=""
POLICY_CONTAINER_PORT=""
POLICY_PROBE_PATH=""
POLICY_MANAGED=""
POLICY_CONFIGS=""
POLICY_WARN=""
sw_sidecar_policy() {
  POLICY_PROFILE=""
  POLICY_CONTAINER_PORT=""
  POLICY_PROBE_PATH=""
  POLICY_MANAGED=""
  POLICY_CONFIGS=""
  POLICY_WARN=""
  case "$1" in
    trendradar)
      POLICY_PROFILE="sourcing"
      POLICY_CONTAINER_PORT="8080"
      POLICY_PROBE_PATH="/"
      POLICY_MANAGED="yes"
      POLICY_CONFIGS="sidecars/trendradar/config/config.yaml=sidecars/trendradar/config.example.yaml,sidecars/trendradar/config/frequency_words.txt=sidecars/trendradar/frequency_words.example.txt"
      ;;
    xhs-downloader)
      POLICY_PROFILE="xhs"
      POLICY_CONTAINER_PORT="5556"
      POLICY_PROBE_PATH="/"
      POLICY_MANAGED="yes"
      POLICY_CONFIGS="-"
      # 回环绑定挡的是同网段邻居与其它 docker 网络里的容器，**挡不住同一台机器上的其他
      # 进程**。这个服务在本仓侧连一个 token 变量都不存在（`.env.example` 里只有 BASE_URL），
      # 所以这句话必须在起它之前说出口，而不是藏在 README 里。
      POLICY_WARN="xhs-downloader 在本仓侧**没有任何鉴权**（连 AUTH_TOKEN 这样的变量都不存在）。回环绑定挡得住同网段邻居和其它 docker 网络里的容器，挡不住同一台宿主机上的其他进程——合租机器上这一点要心里有数（docs/RISKS.md §15.1）。"
      ;;
    mpt)
      POLICY_PROFILE="video"
      POLICY_CONTAINER_PORT="8080"
      POLICY_PROBE_PATH="/"
      POLICY_MANAGED="no"
      POLICY_CONFIGS="sidecars/mpt/config.toml=sidecars/mpt/config.example.toml"
      ;;
    *)
      return 1
      ;;
  esac
}

# 送给远端的一条 spec：`名字:profile:容器端口:探测路径:管不管:配置对`。
# 六格全部来自上面那张表，远端不认识白名单本身、也不自作主张——与 env_set.sh 的
# `键:display:别名` 是同一手法。字段里不含空白，所以拼成一个词传过去是安全的。
sw_sidecar_spec() {
  sw_sidecar_policy "$1" || return 1
  printf '%s:%s:%s:%s:%s:%s' \
    "$1" "${POLICY_PROFILE}" "${POLICY_CONTAINER_PORT}" "${POLICY_PROBE_PATH}" \
    "${POLICY_MANAGED}" "${POLICY_CONFIGS}"
}

# ------------------------------------------------------------------------ 参数解析
MODE=""
SERVICE=""

# 名字这一格在本机、在发起 SSH 之前判完。调用方一律用 `"${2:-}"` 传值：缺参数时传进来的
# 是空串，由这里的 -n 判掉——**不要**在这里数 `$#`，函数收到的永远是两个参数，那样数出来
# 的永远是 2，等于没判（这类"看起来在校验、其实恒真"的写法本仓栽过）。
require_service() {
  local flag="$1" value="$2"
  [[ -n "${value}" ]] || die "${flag} 后面要跟 sidecar 名字" "可用：${SW_SIDECAR_WHITELIST}"
  case "${value}" in
    -*) die "${flag} 后面要跟 sidecar 名字，收到的是一个选项：${value}" "可用：${SW_SIDECAR_WHITELIST}" ;;
  esac
}

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --status)
      [[ -z "${MODE}" ]] || die "--status / --materialize / --up / --down 只能选一个"
      MODE="status"; shift ;;
    --materialize)
      [[ -z "${MODE}" ]] || die "--status / --materialize / --up / --down 只能选一个"
      require_service "$1" "${2:-}"
      MODE="materialize"; SERVICE="$2"; shift 2 ;;
    --up)
      [[ -z "${MODE}" ]] || die "--status / --materialize / --up / --down 只能选一个"
      require_service "$1" "${2:-}"
      MODE="up"; SERVICE="$2"; shift 2 ;;
    --down)
      [[ -z "${MODE}" ]] || die "--status / --materialize / --up / --down 只能选一个"
      require_service "$1" "${2:-}"
      MODE="down"; SERVICE="$2"; shift 2 ;;
    -h|--help)
      usage ;;
    *)
      printf '\n✗ 无法识别的参数：%s\n\n' "$1" >&2; usage ;;
  esac
done

[[ -n "${MODE}" ]] || usage
command -v ssh >/dev/null 2>&1 || die "本机没有 ssh 命令"

# 名字白名单：在本机、在发起 SSH 之前判完。`mpt` 有专属文案——"暂不支持"与"有意排除"
# 是两种完全不同的话，读到前者的人会去等下一版，读到后者的人才知道该去解决什么。
if [[ "${MODE}" != "status" ]]; then
  if [[ "${SERVICE}" == "mpt" ]]; then
    die "mpt **有意不由本工具启用**，这不是「暂不支持」" \
      "理由一：它的出片链路依赖模型网关，而模型网关当前正卡在配额上（docs/RISKS.md 第 4 条），起了也跑不通整条链路。" \
      "理由二：它必须先有素材源 key——sidecars/mpt/config.example.toml 写明 pexels_api_keys / pixabay_api_keys 必填其一，否则渲染在 materials 阶段必然失败。那两个 key 只能由**用户自己**去 pexels.com / pixabay.com 申请，不是工具面能补的。" \
      "所以本轮的边界是明确的：只启用 trendradar（采集）与 xhs-downloader（采集），出片链路整体不在射程内。" \
      "想看它现在什么样：bash scripts/ops/sidecar.sh --status（mpt 照样如实列出）。"
  fi
  case " ${SW_SIDECAR_WHITELIST} " in
    *" ${SERVICE} "*) : ;;
    *)
      die "sidecar 名字 ${SERVICE} 不在白名单里，拒绝执行" \
        "白名单**写死在脚本里，不接受运行时扩展**，当前两个：${SW_SIDECAR_WHITELIST}" \
        "为什么写死：白名单的意义就在于它是一份**人审过的名单**。若改成「从 docker-compose.yml 里枚举服务名」，谁往 compose 里加一个服务，本工具就自动获得起它的能力——那就不是白名单了。" \
        "另外，「这个 sidecar 起之前要不要校验配置文件、容器端口是几、探哪个路径」这几格 compose 文件里没有，只能由脚本里那张表回答。"
      ;;
  esac
  sw_sidecar_policy "${SERVICE}" || die "内部错误：白名单里的 ${SERVICE} 在策略表里没有对应条目"
  [[ "${POLICY_MANAGED}" == "yes" ]] || die "内部错误：${SERVICE} 的策略表写着本工具不管它"
fi

# 要送给远端的 spec 列表。--status 报三个，其余三个模式只报一个。
SPECS=""
if [[ "${MODE}" == "status" ]]; then
  # shellcheck disable=SC2066,SC2086
  # 这里**故意**对 SW_SIDECAR_REPORTED 做词拆分——它就是一张空格分隔的服务名表，
  # 元素全是本脚本硬编码的标识符，既不含空白也不含通配符。
  for reported in ${SW_SIDECAR_REPORTED}; do
    spec="$(sw_sidecar_spec "${reported}")" || die "内部错误：${reported} 在策略表里缺格"
    SPECS="${SPECS}${spec} "
  done
else
  spec="$(sw_sidecar_spec "${SERVICE}")" || die "内部错误：${SERVICE} 在策略表里缺格"
  SPECS="${spec} "
fi

# 【只有 --status 会取 token】它是四个模式里唯一要打 /api/v1 的（读 use_fake_publishers）。
# 其余三个模式一个凭据都不需要，那就一个字节都不取、也不往远端送——送进去的东西越少越好。
# 未取用时 sw_ops_emit_token_prologue 照样会发一行 `export SW_OPS_UI_TOKEN=''`：远端 `set -u`
# 有定义可读，且四条路径的远端代码完全同构。tests/ops/test_sidecar.sh 有一条用例直接钉死
# "--up 时那条流里 token 明文 0 次"。
if [[ "${MODE}" == "status" ]]; then
  sw_ops_load_ui_token
fi

# 远端脚本正文。单独成一个函数，是为了让 ssh 的 stdin 能由三段拼接而成：
#   ① sw_ops_emit_token_prologue —— 一行 `export SW_OPS_UI_TOKEN=<%q 转义的值>`，
#      它落在下面那个**外层** `{` 的**外面**（见 ui_token.sh 里那段"只许放内建命令"的警告）；
#   ② 这里的正文 —— 两段引号 heredoc，不做任何本地展开；
#   ③ 夹在两段之间的 sw_ops_emit_sw_probe_definition —— 远端 sw_probe 的唯一定义处，
#      发射进来的字节落在**内层**那对花括号里，与 restart.sh / env_set.sh 的位置完全一致。
# token 只能走这条路：ssh 只转发 stdin 这一个通道，而 argv 是同机其他用户可读的。
sidecar_remote_script() {
  cat <<'REMOTE_HEAD'
{
set -uo pipefail
# 起停跑在独立的 Bash 里，errexit 契约与这层状态规范化包装互不影响。ssh 用 255 表示传输
# 中断，所以远端脚本自身的 255 必须先改写成别的非零码，避免与断链混淆。
# update.sh / verify.sh / restart.sh / env_set.sh 都有这层，这里对齐。
remote_status=0
bash -s -- "$@" <<'REMOTE_SIDECAR' || remote_status=$?
{
set -euo pipefail
# ！！【stdin 的结构性保证——改本段任何一行之前先读完这一段】
# 历史缺陷：这层 bash 的脚本正文曾经**就是它自己的 stdin**（正上方这个 REMOTE_SIDECAR
# heredoc；本机把它拼成一条流送进 ssh——REMOTE_HEAD + 发射进来的 sw_probe 定义 + REMOTE_TAIL）。
# 任何在本段里被调用、又会读 stdin 的子进程，都会把"脚本剩下的部分"当成自己的输入吞掉，
# 后面的步骤随之全部消失，而脚本仍以 0 收尾——外层照样打印"✓ 已启动"。
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
# 【本段里那两条真会读 stdin 的命令】都是 `docker compose exec -T core python3`，而且都由
# **前置管道**显式喂（`printf '%s' "${json}" | docker compose exec …`）。其余那些 `</dev/null`
# （`docker compose ps` / `up` / `port` / `stop` / `rm` / `curl`）真实命令并不读 stdin，属于
# **纵深防御**，但一条都不删：本脚本自己声明了"每条边界命令都要有显式 stdin 来源"这条不
# 变量，就不留例外。反过来也不要因为"反正有 `</dev/null`"就删掉花括号与 `} </dev/null`：
# 两层各守一半。verify.sh / update.sh / restart.sh / status.sh / env_set.sh 是同一套口径。
mode="$1"; shift
gate_status="$1"; shift
unknown_status="$1"; shift
config_missing_status="$1"; shift
up_status="$1"; shift
probe_status="$1"; shift
template_missing_status="$1"; shift
write_status="$1"; shift
runtime_bind_status="$1"; shift
table_mismatch_status="$1"; shift
# 剩下的位置参数全是 spec：`名字:profile:容器端口:探测路径:管不管:配置对`
# 数组展开一律写成 `${x[@]+"${x[@]}"}`：bash 3.2 在 `set -u` 下展开空数组会直接报
# unbound variable（值班工作站与本仓的 ops 测试都跑在 3.2 上，这不是理论问题）。
specs=("$@")
if [[ "${#specs[@]}" -eq 0 ]]; then
  printf '✗ 远端一条 spec 都没收到：ssh 的 argv 边界出问题了。\n' >&2
  exit 2
fi

# ---- 工作台 API 探针（带鉴权；token 一个字符都不进 argv）------------------------
# 【定义不在本文件里】紧跟这段注释的 sw_probe 定义由本机的
# sw_ops_emit_sw_probe_definition 发射进这条脚本流，**唯一定义处是 scripts/ops/ui_token.sh**。
# 发射进来的字节落在**内层**那对花括号的里面，与 restart.sh / env_set.sh 完全相同的位置，
# 所以上面那道 `{ ... } </dev/null` 的结构性保证一字未变。
# 本脚本只在 `--status` 里用它一次：读 /api/v1/system/info 的 use_fake_publishers 供人对照。
# **它不用来探 sidecar**——理由写在下面 sidecar_probe 上方，那是两件不同的事。
REMOTE_HEAD
  sw_ops_emit_sw_probe_definition
  cat <<'REMOTE_TAIL'

cd "${HOME}/social_workflow"

# 逗号分隔串 → 一行一项。profile 名与配置对里都不含空白，所以调用方可以直接
# `for x in $(sidecar_split_commas "$s")` 拿去用；空项会被丢掉。
# 单独抽出来是因为 bash 3.2 里"临时改 IFS 再改回来"写在循环体内极易出错（改回来的那一句
# 会被 `continue` 跳过），而这个函数只有一个出口。
sidecar_split_commas() {
  local blob="$1" saved_ifs="${IFS}" one
  [[ -n "${blob}" && "${blob}" != "-" ]] || { IFS="${saved_ifs}"; return 0; }
  IFS=','
  for one in ${blob}; do
    [[ -z "${one}" ]] || printf '%s\n' "${one}"
  done
  IFS="${saved_ifs}"
}

# ============================================================================
# 端口解析：本脚本最重要的一件事
# ============================================================================
#
# 【为什么用远端 `docker compose config` 而不是别的办法——四条，逐条都能被验证】
#   ① **要判的是"生效值"，不是"源文件里写了什么"。** `docker-compose.yml` 里写的是
#      `127.0.0.1:${XHS_DOWNLOADER_HOST_PORT:-5556}:5556`——变量插值由 compose 在解析时完成，
#      grep 拿到的是插值**之前**的字符串。
#   ② **`docker-compose.override.yml` 只有 compose 自己看得见。** 它在 `.gitignore` 里
#      （"环境本地的 compose 覆盖(如服务器上的环回端口绑定),不入库"），也就是说**仓库里
#      根本没有那个文件**，本机怎么 grep 都不可能知道生产上叠了什么。本机实测过它的合并
#      语义：默认文件发现会把 override 合进来并且**能改掉 host_ip**，而一旦显式写
#      `-f docker-compose.yml`，override 就**不再被加载**。所以下面那两条命令
#      （`config` 与 `up`）一律**不带 `-f`**，让两者走完全相同的文件发现路径——闸门校验的
#      必须正是紧接着要执行的那一条 `up` 会得到的配置，否则闸门等于没校验。
#   ③ **只有 compose 知道"没写 host_ip"等于什么。** 解析结果里 `5556:5556` 这种写法**不会**
#      产生 `host_ip` 键（本机实测），它的含义是 0.0.0.0。所以判定必须是"取不到 host_ip
#      就按 0.0.0.0 处理"，这正是 docs/RISKS.md §15.6 那条 jq 复核命令里 `// "0.0.0.0"` 的
#      来历。任何基于 grep 的写法都会把这种**最危险的形态**当成"没匹配到，跳过"。
#   ④ **必须在远端。** 生产上跑的是生产上那份文件与那份 `.env`；本机的 compose 解析结果与
#      它没有任何必然关系（红线 R3 的另一面：本机看到的不算数）。
#
# 【为什么不用 `docker compose port`】它读的是**已经跑起来的容器**的实际映射，端口那时已经
# 开在那儿了——用来做"起之前的闸门"在时序上就不成立。它在本脚本里另有其位：`--up` 之后再
# 用它核一次**运行期真身**（见下面 sidecar_runtime_binding），两道判定问的是不同的问题。
#
# 【为什么 JSON 进 core 容器解析】本仓从不假设远端**宿主机**上有 python3；status.sh /
# restart.sh / verify.sh / update.sh / env_set.sh 的 JSON 解析全部走
# `docker compose exec -T core python3`。代价如实写明：**core 没起来时这道闸门判不了**，
# 那时本脚本 fail-closed 拒绝启动 sidecar（退 ${unknown_status}）。这个失效模式是可接受的
# ——core 都没起的时候，起一个 sidecar 不是当务之急；而"判不了就放行"是不可接受的。
#
# 【只放行 127.0.0.1 与 ::1，这是白名单】`localhost` 之类**一律拒绝**：它要过 /etc/hosts
# 才知道指向哪儿，而"绑在哪个地址"这件事不该由一次名字解析来回答。
sidecar_scan_rows=""
sidecar_scan() {
  # $1 = 逗号分隔的 profile 列表；$2 = 要判定的服务名（空串 = 只报告、不判定）
  local profiles="$1" gate="$2"
  local json="" rc=0
  local -a args=()
  local one
  for one in $(sidecar_split_commas "${profiles}"); do
    args+=(--profile "${one}")
  done
  sidecar_scan_rows=""
  # 先赋值再判退出码：`local x="$(...)"` 会把 command substitution 的退出码吃掉。
  json="$(docker compose ${args[@]+"${args[@]}"} config --format json </dev/null 2>/dev/null)" || return 90
  [[ -n "${json}" ]] || return 90
  sidecar_scan_rows="$(printf '%s' "${json}" | docker compose exec -T core python3 -c '
import json
import sys

gate = sys.argv[1] if len(sys.argv) > 1 else ""
try:
    doc = json.load(sys.stdin)
except Exception:
    raise SystemExit(21)
services = doc.get("services")
if not isinstance(services, dict):
    raise SystemExit(21)
gate_seen = False
gate_ports = 0
gate_bad = 0
for name in sorted(services):
    entry = services[name] or {}
    ports = entry.get("ports") or []
    if name == gate:
        gate_seen = True
    if not ports:
        print("port\t{}\t-\t-\t-\tnone".format(name))
        continue
    for item in ports:
        item = item or {}
        # 没有 host_ip 键就是 0.0.0.0 —— compose 对 "5556:5556" 这种写法不产生该键，
        # 而那恰恰是最危险的形态，绝不能当成"没匹配到"跳过。
        host_ip = str(item.get("host_ip") or "0.0.0.0").strip()
        published = str(item.get("published") or "")
        target = str(item.get("target") or "")
        loopback = host_ip in ("127.0.0.1", "::1")
        print("port\t{}\t{}\t{}\t{}\t{}".format(
            name, host_ip, published, target, "loopback" if loopback else "EXPOSED"))
        if name == gate:
            gate_ports += 1
            if not loopback:
                gate_bad += 1
if not gate:
    raise SystemExit(0)
if not gate_seen:
    raise SystemExit(22)
if gate_bad:
    raise SystemExit(23)
if gate_ports == 0:
    raise SystemExit(24)
raise SystemExit(0)
' "${gate}")" || rc=$?
  return "${rc}"
}

# 把扫描结果按人读的样子打出来。**任何一次扫描，无论判红判绿，都要打**——闸门的价值一半在
# 于拦住，另一半在于让人看见它凭什么拦。
sidecar_print_rows() {
  local tag rname rhost rpub rtgt rverdict
  local printed=0
  while IFS=$'\t' read -r tag rname rhost rpub rtgt rverdict; do
    [[ "${tag}" == "port" ]] || continue
    printed=1
    case "${rverdict}" in
      loopback) printf '  %-16s %-10s %s -> %s  回环\n' "${rname}" "${rhost}" "${rpub}" "${rtgt}" ;;
      none)     printf '  %-16s %s\n' "${rname}" "（未发布任何宿主机端口）" ;;
      *)        printf '  %-16s %-10s %s -> %s  ⚠ 暴露（非回环）\n' "${rname}" "${rhost}" "${rpub}" "${rtgt}" ;;
    esac
  done <<<"${sidecar_scan_rows}"
  [[ "${printed}" -eq 1 ]] || printf '  <解析结果里一条端口记录都没有>\n'
}

# 取某个服务在解析结果里发布 <容器端口> 的那一条的宿主机端口。取不到回空串。
sidecar_published_for() {
  local want_service="$1" want_target="$2"
  local tag rname rhost rpub rtgt rverdict
  while IFS=$'\t' read -r tag rname rhost rpub rtgt rverdict; do
    [[ "${tag}" == "port" ]] || continue
    [[ "${rname}" == "${want_service}" ]] || continue
    [[ "${rtgt}" == "${want_target}" ]] || continue
    printf '%s' "${rpub}"
    return 0
  done <<<"${sidecar_scan_rows}"
  return 1
}

# ---- sidecar 存活探针（**刻意不是 sw_probe**）----------------------------------
# 两条理由，都不是风格问题：
#   ① 判定语义不同。sw_probe 带 `-f`，HTTP >= 400 即判失败；而这里要回答的是"它在不在
#      监听"，一个 404 恰恰证明**它在**。trendradar 的 8080 是 `python -m http.server`
#      （GET / 给目录列表），xhs-downloader 的 5556 上有什么路由本仓没有约定——所以判据
#      只能是"拿到了任何一个真实的 HTTP 状态码"。这与 scripts/preflight.py 的 `_probe_http`
#      口径一致（它对任何状态码都记 OK，连不上才记 WARN）。
#   ② **绝不能把 core 的 token 送给第三方 sidecar。** sw_probe 会在 SW_OPS_UI_TOKEN 非空时
#      往请求里塞 `Authorization: Bearer <token>`，而这两个 sidecar 都是上游镜像，它们会把
#      收到的头写进自己的日志谁也说不准。红线 R5 的实质是"凭据只出现在它必须出现的地方"。
# `-q` 仍然必须打头（忽略 ~/.curlrc，理由见 ui_token.sh 头部），`-o /dev/null` 丢弃响应体
# ——我们只要状态码，不要把一个上游服务的返回内容打进运维日志。
sidecar_probe_code=""
sidecar_probe() {
  local url="$1" max_time="$2" code=""
  code="$(curl -q -s -o /dev/null --max-time "${max_time}" -w '%{http_code}' "${url}" </dev/null)" || code="000"
  [[ "${code}" =~ ^[0-9]{3}$ ]] || code="000"
  sidecar_probe_code="${code}"
  [[ "${code}" != "000" ]]
}

# ---- 运行期真身：起完之后实际绑在哪 --------------------------------------------
# 判定规则与 verify.sh / update.sh 的 core 端口门禁**逐字同源**：恰好一条
# `127.0.0.1:<port>` 或 `[::1]:<port>`，端口是无前导零的十进制 1..65535；空值、多行、CR、
# 公网地址、畸形端口一律拒绝。末尾哨兵 `\037` 让 command substitution 不吞掉换行，
# 以便真的能拒绝"多行"。
runtime_host=""
runtime_port=""
runtime_mapping=""
runtime_error=""
sidecar_runtime_binding() {
  local profile="$1" service="$2" container_port="$3"
  local captured mapping port
  runtime_host=""; runtime_port=""; runtime_mapping=""; runtime_error=""
    # `--profile` 不能省：带 profile 的服务不在默认命令的服务集里，省了会报 no such service。
  if ! captured="$(docker compose --profile "${profile}" port "${service}" "${container_port}" </dev/null && printf '\037')"; then
    runtime_error="读不出 ${service}:${container_port} 的实际发布端口（容器没起来？）"
    return 1
  fi
  if [[ "${captured}" != *$'\037' ]]; then
    runtime_error="无法完整读取 ${service}:${container_port} 的实际发布端口"
    return 1
  fi
  captured="${captured%$'\037'}"
  [[ "${captured}" == *$'\n' ]] && captured="${captured%$'\n'}"
  mapping="${captured}"
  if [[ -z "${mapping}" || "${mapping}" == *$'\n'* || "${mapping}" == *$'\r'* ]]; then
    runtime_error="${service}:${container_port} 必须是恰好一条 loopback 映射，拒绝映射：$(printf '%q' "${mapping}")"
    return 1
  fi
  if [[ ! "${mapping}" =~ ^(127\.0\.0\.1|\[::1\]):([1-9][0-9]{0,4})$ ]]; then
    runtime_error="${service}:${container_port} 必须是规范的 loopback:port，拒绝映射：$(printf '%q' "${mapping}")"
    return 1
  fi
  port="${BASH_REMATCH[2]}"
  if [[ "${port}" -gt 65535 ]]; then
    runtime_error="${service}:${container_port} 发布端口必须在 1..65535，拒绝映射：$(printf '%q' "${mapping}")"
    return 1
  fi
  runtime_mapping="${mapping}"
  runtime_port="${port}"
  if [[ "${mapping}" == \[::1\]:* ]]; then
    runtime_host='[::1]'
  else
    runtime_host='127.0.0.1'
  fi
  return 0
}

# spec 拆包：把 `名字:profile:容器端口:探测路径:管不管:配置对` 摊进六个变量。
spec_name=""; spec_profile=""; spec_container_port=""; spec_probe_path=""; spec_managed=""; spec_configs=""
sidecar_unpack() {
  IFS=':' read -r spec_name spec_profile spec_container_port spec_probe_path spec_managed spec_configs <<<"$1"
}

# 配置对拆包：`目标=模板,目标=模板`；`-` 表示这个服务没有配置依赖。
# 回调式遍历（bash 3.2 没有 nameref），$1 是回调函数名。
sidecar_each_config() {
  local configs="$1" callback="$2" pair target template
  for pair in $(sidecar_split_commas "${configs}"); do
    target="${pair%%=*}"
    template="${pair#*=}"
    "${callback}" "${target}" "${template}" || return $?
  done
  return 0
}

case "${mode}" in

# ============================================================== 只读：--status
status)
  # profile 取所有被报告服务的并集：不带 --profile 的话，带 profile 的服务在
  # `ps` 与 `config` 的结果里根本不出现，那会看成"没起"而不是"没查"。
  status_profiles=""
  for spec in ${specs[@]+"${specs[@]}"}; do
    sidecar_unpack "${spec}"
    status_profiles="${status_profiles}${spec_profile},"
  done

  printf '\nCompose 服务（-a 含已停止；没列出来的就是从未创建过）\n'
  status_ps_args=()
  for one in $(sidecar_split_commas "${status_profiles}"); do
    status_ps_args+=(--profile "${one}")
  done
  docker compose ${status_ps_args[@]+"${status_ps_args[@]}"} ps -a </dev/null

  printf '\n端口绑定（远端 docker compose config 解析后的 host_ip，不是 grep 源文件）\n'
  status_scan_rc=0
  sidecar_scan "${status_profiles}" "" || status_scan_rc=$?
  if [[ "${status_scan_rc}" -eq 0 ]]; then
    sidecar_print_rows
  else
    printf '  <解析不出来：docker compose config / 容器内 JSON 解析失败（内部码 %s）>\n' "${status_scan_rc}"
    printf '  只读视图到此为止，不猜。--up 遇到同样的情况会 fail-closed 拒绝启动。\n'
  fi
  # 覆盖面要说准，别让人把这张表读成"全部发布口都在这儿了"。**每账号小红书 sidecar 不在**：
  # 它们由 scripts/gen_xhs_sidecars.py 生成到 docker-compose.xhs.yml，那个文件在 .gitignore 里、
  # 不入库，也不在默认文件发现范围内；本工具的白名单也不管它们。
  printf '  （上表只覆盖默认 compose 组合。**每账号小红书 sidecar 不在里面**——它们在不入库的\n'
  printf '    docker-compose.xhs.yml 里，本工具也不管它们的起停。看它们的绑定要另外加 -f 那个文件，\n'
  printf '    命令见 docs/RISKS.md §15.6。）\n'

  printf '\n配置文件（缺就位则该 sidecar 起不来；生成用 --materialize）\n'
  status_report_config() {
    local target="$1" template="$2"
    if [[ -f "${target}" ]]; then
      printf '  %-52s 就位\n' "${target}"
    elif [[ -f "${template}" ]]; then
      printf '  %-52s 缺失（模板已部署：%s）\n' "${target}" "${template}"
    else
      printf '  %-52s 缺失，且模板也不在：%s\n' "${target}" "${template}"
    fi
  }
  for spec in ${specs[@]+"${specs[@]}"}; do
    sidecar_unpack "${spec}"
    if [[ "${spec_configs}" == "-" ]]; then
      printf '  %-52s （%s 没有配置依赖）\n' "-" "${spec_name}"
      continue
    fi
    sidecar_each_config "${spec_configs}" status_report_config
  done

  printf '\n本工具管哪几个\n'
  for spec in ${specs[@]+"${specs[@]}"}; do
    sidecar_unpack "${spec}"
    if [[ "${spec_managed}" == "yes" ]]; then
      printf '  %-16s 可 --materialize / --up / --down（profile: %s）\n' "${spec_name}" "${spec_profile}"
    else
      printf '  %-16s **有意排除**，本工具不起停它（profile: %s）\n' "${spec_name}" "${spec_profile}"
    fi
  done

  # 【为什么这一格在这里】起 sidecar 不改变发布语义，本脚本因此**没有** R1 闸门；
  # 但"我正要把采集/发布链路的零件接上，此刻真发布到底开着没有"是操作者该同时看到的一件事。
  # 取不到时如实说取不到，绝不留一个看起来像 false 的空白。
  printf '\n对照：模拟发布器（本脚本不改它，只是让你一眼看到当前值）\n'
  info_probe_rc=0
  sw_probe 'http://127.0.0.1:8000/api/v1/system/info' 10 >/dev/null 2>/dev/null || info_probe_rc=$?
  if [[ "${info_probe_rc}" -ne 0 ]]; then
    if [[ "${sw_probe_code}" == "401" ]]; then
      printf '  use_fake_publishers  <未取到：GET /api/v1/system/info 返回 401（core 已启用 SW_UI_TOKEN，本机未提供或不匹配）>\n'
      printf '                       补上 SW_OPS_UI_TOKEN 后重跑；或直接看 bash scripts/ops/verify.sh。\n'
    else
      printf '  use_fake_publishers  <未取到：GET /api/v1/system/info 失败（curl 退出码 %s，HTTP %s）>\n' \
        "${info_probe_rc}" "${sw_probe_code}"
    fi
  else
    fake_rc=0
    printf '%s' "${sw_probe_body}" | docker compose exec -T core python3 -c '
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
' >/dev/null 2>&1 || fake_rc=$?
    case "${fake_rc}" in
      0)  printf '  use_fake_publishers  true（什么都不会真发）\n' ;;
      10) printf '  use_fake_publishers  false（真发布已开启）\n' ;;
      *)  printf '  use_fake_publishers  <未取到：响应拿到了但解析不了>\n' ;;
    esac
  fi

  # 防回归哨兵：它排在本段所有读 stdin 的边界命令之后，一旦从输出里消失，就说明有命令又把
  # 脚本正文吞了——那时后面几段等于没跑，脚本却仍会以 0 收尾。测试直接断言它。
  printf '\n五段读取完毕（Compose 服务 / 端口绑定 / 配置文件 / 管辖范围 / 模拟发布器）\n'
  exit 0
  ;;

# ========================================================= 生成配置：--materialize
materialize)
  sidecar_unpack "${specs[0]}"
  if [[ "${spec_configs}" == "-" ]]; then
    printf '  %s 没有配置依赖，本次一个文件都没生成（这不是失败，是这个服务本来就不需要配置）。\n' "${spec_name}"
    printf '\n生成完毕（0 个新文件）\n'
    exit 0
  fi
  materialize_created=0
  materialize_kept=0
  materialize_one() {
    local target="$1" template="$2" dir
    # **已存在则不覆盖**：人可能在上面填过自部署 newsnow 地址之类的本地信息，
    # 而本工具没有任何办法分辨"模板原样"与"人改过"。宁可让人自己删掉再来。
    if [[ -f "${target}" ]]; then
      printf '  %-52s 已存在，跳过（不覆盖）\n' "${target}"
      materialize_kept=$((materialize_kept + 1))
      return 0
    fi
    if [[ ! -f "${template}" ]]; then
      printf '✗ 模板不在生产上：%s\n' "${template}" >&2
      printf '  这份模板是**进过 git 的**，本该随 update.sh 的快进部署过来。它不在，说明生产 HEAD 早于模板落地的那一版，或者工作树被人动过。\n' >&2
      printf '  处置：先跑 bash scripts/ops/verify.sh 看生产 HEAD 到底是哪一版，必要时 bash scripts/ops/update.sh --apply 部署到位，再重跑本命令。\n' >&2
      return "${template_missing_status}"
    fi
    dir="$(dirname "${target}")"
    mkdir -p "${dir}" || { printf '✗ 建目录失败：%s\n' "${dir}" >&2; return "${write_status}"; }
    cp -- "${template}" "${target}" || { printf '✗ 生成失败：%s <- %s\n' "${target}" "${template}" >&2; return "${write_status}"; }
    [[ -f "${target}" ]] || { printf '✗ 生成后目标仍不存在：%s\n' "${target}" >&2; return "${write_status}"; }
    printf '  %-52s 已生成（来源模板：%s）\n' "${target}" "${template}"
    materialize_created=$((materialize_created + 1))
    return 0
  }
  materialize_rc=0
  sidecar_each_config "${spec_configs}" materialize_one || materialize_rc=$?
  [[ "${materialize_rc}" -eq 0 ]] || exit "${materialize_rc}"
  printf '\n生成完毕（新建 %s 个，已存在保留 %s 个）\n' "${materialize_created}" "${materialize_kept}"
  exit 0
  ;;

# ================================================================= 启动：--up
up)
  sidecar_unpack "${specs[0]}"

  # ---- 第一关：配置文件必须先就位 ------------------------------------------------
  # 【为什么要在这里挡一道，而不是"起起来再看"】trendradar 缺配置时上游 entrypoint.sh
  # 直接 `exit 1`，而 compose 里它带着 `restart: unless-stopped`——起了就是一个无休止的
  # 崩溃重启循环，还得再来一趟把它清掉。挡在前面的代价是零。
  up_config_missing=0
  up_check_config() {
    local target="$1" template="$2"
    if [[ ! -f "${target}" ]]; then
      printf '  %-52s 缺失（模板：%s）\n' "${target}" "${template}" >&2
      up_config_missing=1
    fi
    return 0
  }
  sidecar_each_config "${spec_configs}" up_check_config
  if [[ "${up_config_missing}" -eq 1 ]]; then
    printf '✗ %s 的配置文件没就位，拒绝启动。\n' "${spec_name}" >&2
    printf '  上游 entrypoint.sh 缺文件直接 exit 1，而 compose 里它是 restart: unless-stopped——起了就是一个崩溃重启循环。\n' >&2
    printf '  处置：bash scripts/ops/sidecar.sh --materialize %s（从已部署的模板就地生成，已存在不覆盖），然后重跑本命令。\n' "${spec_name}" >&2
    exit "${config_missing_status}"
  fi

  # ---- 第二关：端口回环闸门（本脚本存在的主要理由）--------------------------------
  printf '\n端口闸门（远端 docker compose config 解析后的 host_ip）\n'
  up_scan_rc=0
  up_no_published=0
  sidecar_scan "${spec_profile}" "${spec_name}" || up_scan_rc=$?
  sidecar_print_rows
  case "${up_scan_rc}" in
    0)
      : ;;
    23)
      printf '✗ 端口闸门判红：%s 解析出来的发布地址**不是回环**，拒绝启动。\n' "${spec_name}" >&2
      printf '  上面那张表里带「暴露」的行就是证据，它来自远端 docker compose 自己算出来的 host_ip（含变量插值与 docker-compose.override.yml 的叠加），不是 grep 源文件。\n' >&2
      printf '  为什么这条是硬拒绝：生产是合租机器（docs/RISKS.md §8.2），同机其它 docker 网络里的容器经默认网关（172.17.0.1）就够得着 0.0.0.0 上的发布口——这条是 §15.2 实测出来的。\n' >&2
      printf '  而这两个 sidecar 都没有可靠鉴权：xhs-downloader 在本仓侧连一个 token 变量都不存在；每账号小红书 sidecar 的 AUTH_TOKEN 默认为空，留空即不鉴权，而它带着该账号的登录态 cookies。\n' >&2
      printf '  处置：把绑定收敛到 127.0.0.1 再部署上去（docs/RISKS.md 第 15 条已在仓库里改好，生产要等下一次 update.sh --apply 才生效），或检查生产上的 docker-compose.override.yml 是不是又把它放开了。\n' >&2
      exit "${gate_status}"
      ;;
    22)
      printf '✗ 端口闸门判不了：服务 %s 不在 `docker compose --profile %s config` 的解析结果里，拒绝启动。\n' "${spec_name}" "${spec_profile}" >&2
      printf '  可能是生产上的 compose 文件与本脚本那张表对不上（服务改名/删了），也可能 profile 名不对。\n' >&2
      printf '  fail-closed：证明不了它只绑回环，就不启动。先跑 bash scripts/ops/sidecar.sh --status 看解析结果长什么样。\n' >&2
      exit "${unknown_status}"
      ;;
    24)
      # 没有发布口 = 没有东西可暴露，闸门本身放行；但它同时意味着**宿主机上根本探不到它**。
      # 这时不能再跑那两道"读发布端口 → 探它"的核验：`docker compose port` 会给出空输出，
      # 而把"本来就没有发布口"判成"绑定核不过"、进而把容器停掉删掉，是一次字面为假的处置。
      # 所以走一条单独的收尾，并且**明说本次没有探过**——不许含糊成"已启动"。
      up_no_published=1
      printf '  %s 在解析结果里**没有发布任何宿主机端口**——没有端口可暴露，闸门放行。\n' "${spec_name}"
      printf '  但这也意味着宿主机上探不到它：下面的运行期绑定核验与存活探针**都不会跑**，本次不会有"它真的起来了"的证据。\n'
      printf '  容器间访问走服务名（compose 网络），不经宿主机发布口。\n'
      ;;
    90)
      printf '✗ 端口闸门判不了：远端 `docker compose config` 执行失败，拒绝启动。\n' >&2
      printf '  fail-closed。先跑 bash scripts/ops/sidecar.sh --status，或在生产上看 compose 文件是不是坏了。\n' >&2
      exit "${unknown_status}"
      ;;
    *)
      printf '✗ 端口闸门判不了：解析 compose 配置失败（内部码 %s），拒绝启动。\n' "${up_scan_rc}" >&2
      printf '  常见原因：core 容器没起来（本仓的 JSON 一律进 core 容器用 python3 解析），或者 config 的输出不是合法 JSON。\n' >&2
      printf '  fail-closed：判不了就不启动。先跑 bash scripts/ops/status.sh 看 core 什么状态。\n' >&2
      exit "${unknown_status}"
      ;;
  esac

  # 表与解析结果的对照：本脚本要靠 `容器端口` 这一格去做启动后的运行期核验与探测。
  # 解析结果里压根没有这个容器端口时**拒绝按猜测行事**——那说明生产上的服务定义变了。
  if [[ "${up_no_published}" -eq 0 ]]; then
    if ! sidecar_published_for "${spec_name}" "${spec_container_port}" >/dev/null; then
      printf '✗ 本脚本那张表说 %s 的容器端口是 %s，但解析结果里没有这一条，拒绝启动。\n' \
        "${spec_name}" "${spec_container_port}" >&2
      printf '  两者对不上时不猜：起完之后的运行期核验与存活探针都要靠这个端口，猜错了就会"探了个别的东西然后宣布成功"。\n' >&2
      printf '  处置：对照上面那张表改 scripts/ops/sidecar.sh 的 sw_sidecar_policy（那是六格必填的显式表）。\n' >&2
      exit "${table_mismatch_status}"
    fi
  fi

  # ---- 第三关：起 ---------------------------------------------------------------
  # **只按显式服务名起**，绝不裸 `up`：裸 up 会把 core 一起重建。
  printf '\n启动\n'
  if ! docker compose --profile "${spec_profile}" up -d "${spec_name}" </dev/null; then
    printf '✗ docker compose up -d %s 失败。\n' "${spec_name}" >&2
    printf '  core 没有被碰过：本命令只按显式服务名操作。\n' >&2
    exit "${up_status}"
  fi

  # ---- 第四关：起完之后再核一次**实际**绑定 --------------------------------------
  # 【为什么闸门已经过了还要再核一次】上面那道判的是"配置解析出来应该绑哪儿"，这道判的是
  # "现在真的绑在哪儿"。两者会不一致的现实情形：容器早就存在且是用旧配置起的，而 compose
  # 出于某种原因没有重建它。这一格判红时**当场停掉并删除**该容器——端口已经开了，
  # 打印一行警告就收工等于把它留在那儿开着。
  if [[ "${up_no_published}" -eq 1 ]]; then
    printf '\n%s 已创建，但本次**没有**做运行期绑定核验、也**没有**探活——它在解析结果里一个宿主机端口都不发布，宿主机上无从探起。\n' "${spec_name}"
    printf '要确认它真的起来了：docker compose --profile %s logs --tail 50 %s\n' "${spec_profile}" "${spec_name}"
    printf '\n启动完毕（端口闸门通过；无发布口，运行期核验与存活探针本次未执行）\n'
    exit 0
  fi

  printf '\n运行期绑定核验\n'
  if ! sidecar_runtime_binding "${spec_profile}" "${spec_name}" "${spec_container_port}"; then
    printf '✗ 起是起来了，但读不出/读不对它的实际绑定：%s\n' "${runtime_error}" >&2
    printf '  **已当场停掉并删除该容器**：证明不了它只绑回环，就不能让它继续开着。\n' >&2
    docker compose --profile "${spec_profile}" stop "${spec_name}" </dev/null || true
    docker compose --profile "${spec_profile}" rm -f "${spec_name}" </dev/null || true
    printf '  core 没有被碰过。处置：看 docker compose --profile %s config 的解析结果，以及生产上的 docker-compose.override.yml。\n' "${spec_profile}" >&2
    exit "${runtime_bind_status}"
  fi
  printf '  %s:%s -> %s（回环）\n' "${spec_name}" "${spec_container_port}" "${runtime_mapping}"

  # ---- 第五关：探它是不是真在监听 ------------------------------------------------
  # 【探不到就如实报，不要打印"已启动"了事】`docker compose up -d` 返回 0 只说明容器被创建
  # 并进入了 running，它**不**说明里面那个进程起来了——trendradar 缺配置时是 entrypoint
  # 直接退出，xhs-downloader 起不来也是一样。所以这里必须真的去打一次。
  printf '\n存活探针（%s，任何 HTTP 状态码都算"在监听"；连不上才算没起来）\n' "${spec_probe_path}"
  probe_url="http://${runtime_host}:${runtime_port}${spec_probe_path}"
  probe_attempt=1
  probe_ok=0
  while [[ "${probe_attempt}" -le 10 ]]; do
    if sidecar_probe "${probe_url}" 5; then
      printf '  GET %s -> HTTP %s（第 %s 次）\n' "${probe_url}" "${sidecar_probe_code}" "${probe_attempt}"
      probe_ok=1
      break
    fi
    sleep 2
    probe_attempt=$((probe_attempt + 1))
  done
  if [[ "${probe_ok}" -ne 1 ]]; then
    printf '✗ 容器起来了，但 20 秒内探不到 %s 在监听（curl 拿到的状态码是 000 = 连不上）。\n' "${probe_url}" >&2
    printf '  **不宣称已启动**：容器 running 只说明进程被拉起过，不说明它没有立刻退出。\n' >&2
    printf '  容器**没有**被停掉，留着给你看日志。最近 20 行：\n' >&2
    docker compose --profile "${spec_profile}" logs --tail 20 "${spec_name}" </dev/null >&2 || true
    printf '  清理：bash scripts/ops/sidecar.sh --down %s\n' "${spec_name}" >&2
    exit "${probe_status}"
  fi

  # 防回归哨兵：排在本段所有读 stdin 的边界命令之后，一旦从输出里消失，就说明有命令又把
  # 脚本正文吞了——那时闸门与探针等于没跑，脚本却仍会以 0 收尾。测试直接断言它。
  printf '\n启动完毕（端口闸门 → 起 → 运行期绑定核验 → 存活探针，四步都过了）\n'
  exit 0
  ;;

# ================================================================ 停止：--down
down)
  sidecar_unpack "${specs[0]}"
  # **永不执行裸 `docker compose down`**：那会连 core 一起拆掉。这里只按显式服务名
  # stop + rm，两条命令的最后一个参数都是白名单里那个名字。
  printf '\n停止\n'
  docker compose --profile "${spec_profile}" stop "${spec_name}" </dev/null
  docker compose --profile "${spec_profile}" rm -f "${spec_name}" </dev/null
  printf '\n已停止并删除容器：%s（core 没有被碰过）\n' "${spec_name}"
  exit 0
  ;;

*)
  printf '✗ 远端收到了不认识的模式：%s\n' "${mode}" >&2
  exit 2
  ;;
esac
} </dev/null
REMOTE_SIDECAR
if [[ "${remote_status}" -eq 255 ]]; then
  exit 254
fi
exit "${remote_status}"
} </dev/null
REMOTE_TAIL
}

# ssh(1) 不保留 argv 边界：host 之后的参数会被用单个空格拼成一个字符串发给远端，再由远端
# 登录 shell 重新分词（空参数会就此消失）。所以自己造那一个字符串并用 printf '%q' 转义——
# verify.sh / update.sh / restart.sh / status.sh / env_set.sh 是同一手法。注入面：九个退出码
# 是本脚本自己定义的十进制常量；模式与 spec 都来自上面那张写死的表和白名单校验之后的名字。
# token 绝不走这条路：它在 stdin 流里（见 sidecar_remote_script 上方说明）。
#
# stdin 用**进程替换**而不是管道喂：`… | ssh …` 在 `set -o pipefail` 下会让写端的 SIGPIPE
# 有机会顶掉 ssh 自己的退出码，而下面整段分派完全靠 ssh 的退出码。
sidecar_remote() {
  # shellcheck disable=SC2086
  # SPECS 要按空格拆成多个位置参数，每一段都是刚从策略表拼出来的六段式常量，不含空白。
  ssh -o ConnectTimeout=25 "${SSH_ALIAS}" \
    "bash -s -- $(printf '%q ' "${MODE}" \
      "${SIDECAR_PORT_GATE_STATUS}" "${SIDECAR_PORT_UNKNOWN_STATUS}" \
      "${SIDECAR_CONFIG_MISSING_STATUS}" "${SIDECAR_UP_STATUS}" \
      "${SIDECAR_PROBE_STATUS}" "${SIDECAR_TEMPLATE_MISSING_STATUS}" \
      "${SIDECAR_WRITE_STATUS}" "${SIDECAR_RUNTIME_BIND_STATUS}" \
      "${SIDECAR_TABLE_MISMATCH_STATUS}" ${SPECS})" \
    < <(sw_ops_emit_token_prologue; sidecar_remote_script)
}

case "${MODE}" in
  status)      printf '生产 sidecar 现状\n\n' ;;
  materialize) printf '生产 sidecar 配置生成（从已部署的模板就地生成，不推送任何本机文件）\n\n' ;;
  up)          printf '生产 sidecar 启动：%s\n\n' "${SERVICE}" ;;
  down)        printf '生产 sidecar 停止：%s\n\n' "${SERVICE}" ;;
esac

note "连接 ${SSH_ALIAS}（IAP 首包通常需 5-10 秒）"
case "${MODE}" in
  status)
    note "只读：不起、不停、不写任何文件"
    sw_ops_note_ui_token
    ;;
  materialize)
    note "只从生产上**已部署的模板**生成配置；已存在的文件一律不覆盖"
    ;;
  up)
    note "起之前先过端口回环闸门（依据是远端 docker compose config 解析后的 host_ip）"
    note "不碰 core：只按显式服务名操作，绝不裸 up / 裸 down"
    [[ -z "${POLICY_WARN}" ]] || warn "${POLICY_WARN}"
    ;;
  down)
    note "只按显式服务名 stop + rm，绝不执行裸 docker compose down"
    ;;
esac

# 【刻意不自动重试】update.sh --apply 是同一个取舍：`--up` / `--down` / `--materialize` 都会
# 改变生产状态，传输在半路断掉时**生产处于什么状态是不明的**，再跑一遍等于在不明状态上叠
# 一次操作。`--status` 只读，重跑一次的成本是一条命令，也没必要在脚本里替人做决定。
remote_rc=0
sidecar_remote || remote_rc=$?

case "${remote_rc}" in
  0) : ;;
  "${SIDECAR_PORT_GATE_STATUS}")
    die_with "${SIDECAR_PORT_GATE_STATUS}" "端口闸门判红：${SERVICE} 解析出来的发布地址不是回环，**没有启动任何容器**" \
      "证据在上面那张表里（来自远端 docker compose 自己算出来的 host_ip，含变量插值与 docker-compose.override.yml 的叠加）。" \
      "这不是误报：生产是合租机器，同机其它 docker 网络里的容器经默认网关就够得着 0.0.0.0 上的发布口（docs/RISKS.md §15.2 实测）。" \
      "生产维持原状——闸门在 up 之前判，拦下来的代价是零。" \
      "出处：docs/RISKS.md 第 15 条、scripts/ops/README.md「sidecar 启用」。"
    ;;
  "${SIDECAR_PORT_UNKNOWN_STATUS}")
    die_with "${SIDECAR_PORT_UNKNOWN_STATUS}" "端口闸门判不了：拿不到或解析不了远端的 compose 配置，**没有启动任何容器**" \
      "fail-closed：证明不了它只绑回环就不启动。这与「判红」是两件事，具体哪一种见上面远端的输出。" \
      "常见原因：core 容器没起来（本仓的 JSON 一律进 core 容器用 python3 解析）、compose 文件有语法问题、或服务名/profile 与脚本里那张表对不上。" \
      "取证：bash scripts/ops/sidecar.sh --status；core 的状态看 bash scripts/ops/status.sh。"
    ;;
  "${SIDECAR_CONFIG_MISSING_STATUS}")
    die_with "${SIDECAR_CONFIG_MISSING_STATUS}" "${SERVICE} 的配置文件没就位，**没有启动任何容器**" \
      "缺哪一份见上面远端的输出。上游 entrypoint.sh 缺文件直接 exit 1，而 compose 里它是 restart: unless-stopped——起了就是一个崩溃重启循环。" \
      "处置：bash scripts/ops/sidecar.sh --materialize ${SERVICE}（从生产上已部署的模板就地生成，已存在不覆盖），然后重跑本命令。"
    ;;
  "${SIDECAR_UP_STATUS}")
    die_with "${SIDECAR_UP_STATUS}" "docker compose up -d ${SERVICE} 失败" \
      "具体报错见上面远端的输出。core 没有被碰过：本命令只按显式服务名操作。" \
      "取证：bash scripts/ops/sidecar.sh --status。"
    ;;
  "${SIDECAR_PROBE_STATUS}")
    die_with "${SIDECAR_PROBE_STATUS}" "${SERVICE} 的容器起来了，但探不到它在监听" \
      "**刻意不宣称成功**：容器 running 只说明进程被拉起过，不说明它没有立刻退出。最近 20 行日志已打在上面。" \
      "容器没有被停掉，留着给你看日志。清理：bash scripts/ops/sidecar.sh --down ${SERVICE}。" \
      "端口绑定这一格是好的（运行期核验已通过），坏的是容器里那个进程。"
    ;;
  "${SIDECAR_TEMPLATE_MISSING_STATUS}")
    die_with "${SIDECAR_TEMPLATE_MISSING_STATUS}" "生产上找不到 ${SERVICE} 的配置模板，**一个文件都没生成**" \
      "那份模板是进过 git 的，本该随 update.sh 的快进部署过来。它不在，说明生产 HEAD 早于模板落地的那一版，或者工作树被人动过。" \
      "处置：bash scripts/ops/verify.sh 看生产 HEAD 是哪一版，必要时 bash scripts/ops/update.sh --apply 部署到位，再重跑本命令。" \
      "本工具**没有**「把本机文件推到生产」的能力，也不打算有——它只会在生产上从已部署的模板复制。"
    ;;
  "${SIDECAR_WRITE_STATUS}")
    die_with "${SIDECAR_WRITE_STATUS}" "在生产上生成 ${SERVICE} 的配置文件失败" \
      "具体哪一份、失败在哪一步见上面远端的输出。已存在的文件一律没有被覆盖。" \
      "常见原因：目录权限、磁盘满。磁盘水位看 bash scripts/ops/status.sh。"
    ;;
  "${SIDECAR_RUNTIME_BIND_STATUS}")
    die_with "${SIDECAR_RUNTIME_BIND_STATUS}" "${SERVICE} 起来之后的实际绑定**没能被证明是回环**（读不出、或读出来不是回环），容器已被当场停掉并删除" \
      "闸门在 up 之前判的是「配置解析出来应该绑哪儿」，这一道判的是「现在真的绑在哪儿」，两者不一致（常见于容器早就存在且是用旧配置起的）。" \
      "端口已经开过一瞬间，所以这里不是打印一行警告了事，而是直接停掉并删除。生产上现在没有这个容器。" \
      "处置：看 docker compose --profile <profile> config 的解析结果，以及生产上的 docker-compose.override.yml。"
    ;;
  "${SIDECAR_TABLE_MISMATCH_STATUS}")
    die_with "${SIDECAR_TABLE_MISMATCH_STATUS}" "脚本里那张服务表与远端解析结果对不上，**没有启动任何容器**" \
      "本脚本要靠「容器端口」那一格去做启动后的运行期核验与存活探测；解析结果里没有那一条时不猜——猜错了就会「探了个别的东西然后宣布成功」。" \
      "处置：对照 bash scripts/ops/sidecar.sh --status 的输出，改 scripts/ops/sidecar.sh 里 sw_sidecar_policy 那张六格必填的表。"
    ;;
  254)
    die_with 254 "远端脚本自身以 255 退出（已被规范化为 254）" \
      "这不是 IAP 断链——包装层专门把它与断链区分开了。远端到底怎么了见上面的输出。" \
      "生产状态不明，本脚本**刻意不自动重试**。先跑 bash scripts/ops/sidecar.sh --status 看现在什么样。"
    ;;
  255)
    die_with 255 "SSH 传输中断（IAP 断链）" \
      "生产状态不明：改动可能已经发生，也可能一个字节都没动。本脚本**刻意不自动重试**——在状态不明时叠一次操作比重跑一次贵得多。" \
      "先跑 bash scripts/ops/sidecar.sh --status 看现在什么样，再决定下一步。"
    ;;
  *)
    exit "${remote_rc}"
    ;;
esac

case "${MODE}" in
  status)      ok "sidecar 现状读取完成" ;;
  materialize) ok "配置生成完成（已存在的文件一个都没覆盖）" ;;
  up)          ok "${SERVICE} 已启动：端口闸门、运行期绑定核验与存活探针都过了" ;;
  down)        ok "${SERVICE} 已停止并删除（core 没有被碰过）" ;;
esac
