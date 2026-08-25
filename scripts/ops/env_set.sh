#!/usr/bin/env bash
# 用途：受**白名单**约束地查看 / 变更生产 `.env` 里的键，并让变更真正在运行中的 core 上生效。
#
# 【为什么会有这个脚本】红线 R3 规定生产只经 `scripts/ops/`、不直连生产、不手敲远程命令；
# 而在本脚本出现之前，`scripts/ops/` 下**没有任何改 `.env` 的能力**（`grep -n '\.env'
# scripts/ops/*.sh` 的命中全是错误提示文案）。两者相加的结果是：改 `.env` 这件事没有合规
# 做法。这卡住了 docs/RISKS.md 第 8 条（给生产加 `SW_UI_TOKEN`），更要紧的是第 9 条第 3 步
# 与第 12 条——把 `SW_USE_FAKE_PUBLISHERS` 翻成 false（从"什么都不会真发"翻成"真的会发出
# 去"）是整条风险里最危险的一次变更，而它此前完全在工具面之外手工进行，也就完全绕过了
# R1 红线闸门。**本脚本的第一目的是把那次变更拉回闸门里，不是方便。**
#
# 【白名单写死在这里，不接受运行时扩展】唯一真相源是下方那行 SW_ENV_WHITELIST。
# **这里刻意不写键数、也不重复枚举**：数字写在这、被数的东西在别处，扩容时必然对不上
# ——本文件在三个位置踩过这个坑。要知道当前是哪些键，读那一行：
#     grep -n '^SW_ENV_WHITELIST=' scripts/ops/env_set.sh
# 同一口径见 docs/RISKS.md §8.5。（usage 里那份清单是例外：它把键**全列在紧下方**，
# 数字与被数的东西挨着，属于枚举自证。）
# 任意键编辑能力**刻意不做**：那是个巨大的脚枪（`.env` 里有 LLM key、Telegram bot token、
# 数据库 URL；写坏任何一条 core 都起不来）。传别的键一律拒绝并说明原因。
#
# 【白名单为什么写死——这条约束的意义在于"逼出工作量"】docs/RISKS.md 第 14 条讲得很清楚：
# 补一个键**不是往数组里加个元素**，而是要给它想清楚四件事——取值形状、display 策略、
# 生效后怎么核验、以及**这个键特有的闸门**。写死白名单正是为了让"加一个键"这件事必须
# 经过这四问，而不是顺手加一行。按这个规矩补进来的键，闸门**逐个不同**（下面这份清单
# 是说明性的，不是白名单本身——白名单以 SW_ENV_WHITELIST 那一行为准）：
#   SW_LLM_BACKEND        → 切过去之前先确认**目标后端的凭据**在 .env 里存在且非空
#   SW_TELEGRAM_ENABLED   → 关掉之前先确认**真发布没开着**（别拆掉确认卡的载体）
#   WECHAT_AUTO_PUBLISH   → 打开之前先确认 WECHAT_CERTIFIED=true（未认证号没有 freepublish 权限）
#   WECHAT_CERTIFIED      → 它记的是**微信那边的事实**，本工具面无法核实，所以这一格
#                           **不拦，但要把话说清**：这次变更会不会让平台级自动发布当场生效
#   SW_USE_FAKE_PUBLISHERS→ 打开真发布之前先确认确认通道活着（本仓 B1 已落地）
#   三个 DAILY_*_BUDGET / SW_GENERATE_ENABLED → 无闸门，但各有各的**警告**（见 sw_env_warn）
# 凭据类键本轮**补齐到第三个**：TELEGRAM_BOT_TOKEN。三个键（SW_UI_TOKEN /
# SW_TELEGRAM_SIGNING_SECRET / TELEGRAM_BOT_TOKEN）共用 secret 策略、"人自己读文件"的零回显
# 流程，以及**同一道** signing_secret 闸门——因为它们恰好就是 core/telegram.py:151-154 那条
# 三级回落的三级：确认卡 callback_data 的 HMAC 签名密钥按 SW_TELEGRAM_SIGNING_SECRET →
# SW_UI_TOKEN → TELEGRAM_BOT_TOKEN 取第一个非空的那一级。换掉**生效的那一级**就是换签名密钥，
# 已推出去还没人点的卡会验签失败。2026-08-22 生产上真发生过一次（换 SW_UI_TOKEN 时第一级为空，
# 回落从第三级跳到第二级），事后查证是 0 条，那是运气。
# 上一批的闸门覆盖了第一、二级；本批把 bot token 加进白名单，**同时**把闸门补到第三级——
# 只加键不补闸门，等于亲手造一个活的缺口（一级二级都空时，换 bot token 就是换签名密钥）。
# 判定不按键名分支，按"回落链上排在它前面的那几级里有没有非空的"，真值表写在远端那道闸门上方。
# bot token 还是第一个**不能 --generate** 的凭据类键：值由 BotFather 签发，本机 CSPRNG 造不出来。
# 这一格同样做成按键表上的一格（POLICY_CRED_ORIGIN），不是在取值路径里硬写一个键名。
# 其余凭据类键（TELEGRAM_CHAT_ID / 各种 API key）**仍然不在名单上**，第 14 条里如实保留
# 它们仍无路径；TELEGRAM_CHAT_ID 尤其不能顺手加——它的值是**从生产流出来**的（要在服务器上
# 跑 core.telegram setup 才知道），现有的表模型不了那个方向，而且"新会话可达没有"这道闸门
# 不真发一条 Telegram 消息根本验不了。
#
# 【三条按键决定的策略，刻意做成显式表格而不是启发式】见下面 sw_env_policy()。
# 绝不做"看起来像密码就藏"这种猜测：策略必须能被读代码的人一眼核对。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH_ALIAS="${SW_OPS_SSH_ALIAS:-${SW_TUNNEL_SSH_ALIAS:-workbench-iap}}"
CRED_FILE="${HOME}/.dsh-sw/.credentials.yaml"

# 远端退出码协议（**这里是唯一定义处**，全部用 printf '%q' 传给远端当位置参数，
# 远端不硬编码任何一个字面量）。都要避开 ssh 保留的 255，也避开远端自然产生的 1/2。
#
# 【41 空着不用】restart.sh / status.sh 拿 41 表示"401 未授权"。本脚本**现在确实会探
# /api/v1**（事前预防闸门），401 也确实可能撞上——但它被归进"没探到"那一档（见 38），
# 因为对本脚本来说 401 与超时是同一类事：拿不到通道状态。留 41 空着，是为了不让同一个
# 数字在 scripts/ops/ 下有两种含义。（上一版这里写着"本脚本不探 /api/v1，用不到它"，
# 那句话在事前预防闸门落地之后就不成立了，本轮更正。）
ENV_MISSING_STATUS=30       # 远端 ~/social_workflow/.env 不存在
ENV_DUPLICATE_STATUS=31     # 同一个键在 .env 里出现多次，拒绝猜哪一条生效
ENV_BAD_KEY_STATUS=32       # 键名过不了远端的 R7 / 形状校验（纵深防御，本地已挡过一次）
ENV_BAD_VALUE_STATUS=33     # 值过不了远端的再校验（纵深防御，同上）
ENV_BACKUP_STATUS=34        # `.env` 备份失败——没有备份就绝不动 `.env`
ENV_WRITE_STATUS=35         # 原子写入失败
ENV_RECREATE_STATUS=36      # `docker compose up -d` 失败：`.env` 已改但没生效
# 下面两个只在 `--key SW_USE_FAKE_PUBLISHERS --value false` 这个方向上可能出现，
# 都发生在**写 `.env` 之前**，触发时远端一个字节都没动过（连备份都没建）。
# 两者必须**分开**：它们是两件不同的事，处置动作也不同。
ENV_PRECHECK_GATE_STATUS=37   # 探到了，但确认闸门通道不活（enabled/ready/polling 有假）
ENV_PRECHECK_PROBE_STATUS=38  # 没探到（连不上 / 超时 / 401 / 解析不了），也就是"不知道"
# 下面四个是本轮（白名单扩容）新增的事前闸门，同样都发生在**写 `.env` 之前**。
# 每个键的闸门问的是**不同的问题**，所以各有各的码——共用一个码等于逼调用方去 grep 中文
# 文案才知道到底哪一格红了。
ENV_CARRIER_GATE_STATUS=39    # SW_TELEGRAM_ENABLED=false：探到了，真发布正开着，拒绝拆掉确认卡载体
ENV_CARRIER_PROBE_STATUS=40   # 同上方向，但没探到真发布状态，也就是"不知道"
# 41 留空，见上方说明。
ENV_BACKEND_CREDS_STATUS=42   # SW_LLM_BACKEND：目标后端的凭据在 .env 里缺失或为空
ENV_WECHAT_CERT_STATUS=43     # WECHAT_AUTO_PUBLISH=true 但 WECHAT_CERTIFIED 不是 true
# 【44 刻意没有分配】WECHAT_CERTIFIED=true 那道闸门（wechat_claim）**从不拒绝**，所以它
# 不需要退出码。这不是漏了：它要问的"这个号凭什么算已认证"是一个本工具面**结构上答不了**
# 的问题（那是微信那边的事实），而一道答不了自己问题的闸门比没有闸门更糟。理由完整写在
# 远端 wechat_claim 分支上方。**没有码这件事本身就是那个结论的痕迹**，别顺手补一个。
# 下面两个是本轮（凭据类键 + 签名密钥轮换闸门）新增的，方向与 37/38、39/40 完全一致：
# "探到了，不行" 与 "没探到" 必须是两个码，因为处置动作不同。同样都发生在**写 `.env` 之前**。
ENV_SIGNING_GATE_STATUS=45    # 签名密钥轮换：探到了，还有待人点的确认卡，拒绝换密钥
ENV_SIGNING_PROBE_STATUS=46   # 同上方向，但读不出待人点的确认卡条数，也就是"不知道"

die() { printf '\n✗ %s\n' "${1}" >&2; shift; for line in "$@"; do printf '  %s\n' "${line}" >&2; done; exit 1; }
# 与 die 逐字同样的输出，只是退出码由调用方给。
# 【只给事前预防闸门那两格用，别顺手推广】本脚本其余所有失败一律走 die（退 1），那是既有
# 契约，不在本轮改动范围里。这两格特殊在于它们要回答的是两个**不同的**问题——
# "探到了，通道不活" 与 "没探到，不知道"——处置动作完全不同，而让调用方靠 grep 中文文案
# 去区分是不可接受的。所以只有它们带自己的退出码（见文件头部那两个常量）。
die_with() { local rc="${1}"; shift; printf '\n✗ %s\n' "${1}" >&2; shift; for line in "$@"; do printf '  %s\n' "${line}" >&2; done; exit "${rc}"; }
note() { printf '  %s\n' "${1}"; }
warn() { printf '  ⚠ %s\n' "${1}"; }
ok() { printf '  ✓ %s\n' "${1}"; }

# 凭据取用 / 生成 / 注入。必须在 die/note 之后 source：库里报错走调用方的 die。
# shellcheck source=scripts/ops/ui_token.sh
. "${SCRIPT_DIR}/ui_token.sh"

usage() {
  cat >&2 <<'USAGE'
用法：
  bash scripts/ops/env_set.sh --show
      只读：逐键回答生产 .env 里"这个键设了没有"。SW_UI_TOKEN 的值**永远不回显**。

  bash scripts/ops/env_set.sh --key <白名单键> --value <值>
      改一个白名单键。合法值与闸门**逐键不同**，见下表——不要指望它们共用一套规则。

  bash scripts/ops/env_set.sh --key <凭据类键> --generate
      在**本机**生成一个新值（CSPRNG，256 bit），先写进 ~/.dsh-sw/.credentials.yaml（0600），
      再推到生产 .env。全程不打印、不进 argv。凭据文件里已有对应的键时拒绝执行。
      **不是每个凭据类键都能这样**：值由外部签发的键（TELEGRAM_BOT_TOKEN 由 BotFather 签发）
      本机造不出来，对它们 --generate 会被**明确拒绝**并告诉你正确做法。

  bash scripts/ops/env_set.sh --key <凭据类键> --from-credentials
      把本机已持有的那个值推到生产 .env。用于"两边不一致"的收敛，也用于 --generate 推送
      失败后的重试。SW_UI_TOKEN 额外先看环境变量 SW_OPS_UI_TOKEN（那一层是它专有的）。

白名单（十二个键，写死在脚本里，**不接受运行时扩展**）：
  SW_UI_TOKEN                  凭据          只走 --generate / --from-credentials，值永不回显
                                             闸门：生产 .env 里 SW_TELEGRAM_SIGNING_SECRET 为空时，
                                             换它就是换确认卡的签名密钥 —— 有待人点的卡就拒绝
  SW_TELEGRAM_SIGNING_SECRET   凭据          同上取值方式；它**就是**确认卡的 HMAC 签名密钥
                                             闸门：换它总是换签名密钥 —— 有待人点的卡就拒绝
                                             显式设上它之后，SW_UI_TOKEN 再怎么换都不动签名密钥
  TELEGRAM_BOT_TOKEN           凭据          **只走 --from-credentials**：值由 BotFather 签发，
                                             本机造不出来，--generate 会被拒绝并告诉你怎么办
                                             闸门：它是三级回落的**第三级**——上面两级都为空时
                                             换它就是换签名密钥，那时有待人点的卡就拒绝
                                             另注：旧 token 立即失效，而 polling 那一格照样报 true
  SW_USE_FAKE_PUBLISHERS       true|false    false = 真发布开启
                                             闸门：先探人工确认通道，不活就拒绝写入
  SW_LLM_BACKEND               anthropic|dsh 闸门：目标后端的凭据必须在生产 .env 里存在且非空
  SW_GENERATE_ENABLED          true|false    false = 只停"出稿"，**不停"发布"**
  SW_TELEGRAM_ENABLED          true|false    false = 拆掉确认卡的推送载体
                                             闸门：真发布正开着时拒绝（先把真发布关了再来）
  WECHAT_AUTO_PUBLISH          true|false    闸门：WECHAT_CERTIFIED 必须已经是 true，
                                             否则这次变更是个**不会生效的空操作**
  WECHAT_CERTIFIED             true|false    记的是**微信那边的事实**，本工具面核实不了
                                             true 方向不拦，但会告诉你平台级自动发布是否
                                             因此当场生效；写错的代价是发布时报 48001
  DAILY_TOKEN_BUDGET           非负整数      0 = 当天全停，**不是**「不限」
  DAILY_RENDER_SECONDS_BUDGET  非负整数      同上
  DAILY_IMAGE_BUDGET           非负整数      同上；注意等价别名 SW_DAILY_IMAGE_BUDGET

  布尔键只认 `true` / `false` 两个**小写单词**；整数键只认无前导零的非负十进制数。
  理由（不是"写别的不生效"，那句话是错的）见脚本里 sw_env_value_re() 上方的注释。

可选：
  --write-only    只改 .env，不重建容器、不重启：变更**不会生效**。
                  **任何会触发事前闸门的方向都禁用它**（上表"闸门"那一列）：闸门要么在写入
                  前跑、要么靠 restart.sh 在生效后跑，而 --write-only 把"生效"整段跳过了。

  --accept-breaking-pending-confirm-cards
                  只对上面那几个凭据类键有意义（它们就是签名密钥回落链的三级）。明知**已推出去还没人点的确认卡会因此失效**
                  （按下去 bad_signature，最终被 TTL 自动驳回）仍然换密钥。名字说的就是后果。
                  用了之后输出里会如实记下你接受的是哪一批（条数，或"条数都没读到"）。
                  它同时覆盖"读不出条数"那一档——否则一台读不到 /api/v1/dashboard 的生产
                  （老版本 core、或者恰恰是 token 两边不一致导致的 401）会永远换不了密钥，
                  而"换 token"正是那种不一致唯一的收敛动作。

细节见 scripts/ops/README.md「改生产 .env」一节。
USAGE
  exit 2
}

# ----------------------------------------------------------------- 按键决定的策略
#
# 三条策略都**按键显式列举**，不做任何"看起来像密码就藏起来"的启发式判断。启发式的问题
# 不是它今天判错，而是没人能一眼核对它明天判不判得对；而这里每一格都要能被 code review
# 逐条对着看。新增键必须在这里补齐三格，漏一格脚本会直接拒绝执行。
#
# | 键 | 值来源 | display | 写入前的闸门 gate | 凭据文件键名 | 凭据来源 | 签名链上级 | 值形状（源码坐标） |
# |---|---|---|---|---|---|---|---|
# | SW_UI_TOKEN                 | credentials | secret | signing_secret    | sw_ui_token | local-csprng | SW_TELEGRAM_SIGNING_SECRET | 字符集白名单（ui_token.sh） |
# | SW_TELEGRAM_SIGNING_SECRET  | credentials | secret | signing_secret    | sw_telegram_signing_secret | local-csprng | none | 同上（core/telegram.py:151 只当 HMAC key 用） |
# | TELEGRAM_BOT_TOKEN          | credentials | secret | signing_secret    | telegram_bot_token | external-issuer | SW_TELEGRAM_SIGNING_SECRET SW_UI_TOKEN | 自己那条形状（见 sw_env_value_re） |
# | SW_USE_FAKE_PUBLISHERS      | argv        | plain  | real_publish      | -  | - | - | core/config.py:53  bool |
# | SW_LLM_BACKEND              | argv        | plain  | llm_backend_creds | -  | - | - | core/config.py:99   Literal["anthropic","dsh"] |
# | SW_GENERATE_ENABLED         | argv        | plain  | 无                | -  | - | - | core/config.py:82  bool |
# | SW_TELEGRAM_ENABLED         | argv        | plain  | confirm_carrier   | -  | - | - | core/config.py:351 bool |
# | WECHAT_AUTO_PUBLISH         | argv        | plain  | wechat_certified  | -  | - | - | core/config.py:233 bool |
# | WECHAT_CERTIFIED            | argv        | plain  | wechat_claim      | -  | - | - | core/config.py:231 bool |
# | DAILY_TOKEN_BUDGET          | argv        | plain  | 无                | -  | - | - | core/config.py:369 int |
# | DAILY_RENDER_SECONDS_BUDGET | argv        | plain  | 无                | -  | - | - | core/config.py:370 int |
# | DAILY_IMAGE_BUDGET          | argv        | plain  | 无                | -  | - | - | core/config.py:375 int |
#
# 【第四格「凭据文件键名」为什么长在这张表里，而不是另起一张】它就是"这个键在
#   ~/.dsh-sw/.credentials.yaml 里叫什么"，只有 value_source=credentials 的键有（其余填 `-`）。
#   另起一张表意味着 tests/ops/test_env_set.sh 那条"五张表逐键一一对应"的断言要么跟着扩，
#   要么漏掉一张没人钉着的表——而漏掉的那一格恰恰会让新键静默去读 / 写别人的凭据行。
#   长在 sw_env_policy 里，它就自动被那条断言盖住了。
# 【本轮多出的第五、六格，同一条理由，但各自解决一处**硬编码键名**】
#   凭据来源 POLICY_CRED_ORIGIN —— `local-csprng` 本机 CSPRNG 造得出来（--generate 可用）；
#     `external-issuer` 由外部签发、本机造不出来（--generate 必须拒绝）。TELEGRAM_BOT_TOKEN
#     是第一个后者。把它写成取值路径里的 `case ${KEY}` 是错的：那正是这一整批在拆的东西，
#     而且第二个这样的键（将来的各种 API key）一进来就会静默走错路。问性质，不问键名。
#   签名链上级 POLICY_SIGNING_ABOVE —— 这个键在 core/telegram.py:151-154 那条签名密钥回落链上
#     **排在它前面**的那几级（按级序，第 1 级在前，空格分隔）；`none` = 它自己就是第 1 级；
#     `-` = 它压根不在那条链上。signing_secret 闸门的第 2 段只看这一格：这几级里但凡有一级
#     非空，本次写入就动不到生效的签名密钥。真值表写在远端那道闸门上方。将来若冒出第四级，
#     改这一格即可，闸门代码一个字不用动。
# display=secret 的理由：它是凭据，红线 R5「凭据永不进仓库、不进对话、不进 argv、不进日志」。
# display=plain  的理由：除 SW_UI_TOKEN 之外的都是**布尔量 / 枚举 / 整数**，不是凭据。它们的值本身就是
#   运维要看的那条事实（"现在到底会不会真发"、"预算还剩多少"），藏起来只会逼人去别处猜。
#   verify.sh 与 status.sh 早就在如实打印其中几个，本脚本与它们保持同一口径。
# value_source=credentials 的理由：token 绝不能经 --value 进 argv——生产是合租机器，
#   `/proc/*/cmdline` 世界可读（docs/RISKS.md §8.2）。传 --value 给 SW_UI_TOKEN 会被拒绝。

# 白名单的**唯一真相源**。sw_env_policy / sw_env_value_re / --show 都从这里派生，
# tests/ops/test_env_set.sh 有一条源码级断言核对"这张表与 sw_env_policy 的分支一一对应"——
# 少一格、多一格、拼错一个字都会红。
SW_ENV_WHITELIST="SW_UI_TOKEN SW_TELEGRAM_SIGNING_SECRET TELEGRAM_BOT_TOKEN SW_USE_FAKE_PUBLISHERS SW_LLM_BACKEND SW_GENERATE_ENABLED SW_TELEGRAM_ENABLED WECHAT_AUTO_PUBLISH WECHAT_CERTIFIED DAILY_TOKEN_BUDGET DAILY_RENDER_SECONDS_BUDGET DAILY_IMAGE_BUDGET"

POLICY_VALUE_SOURCE=""
POLICY_DISPLAY=""
POLICY_GATE=""
# 只有 value_source=credentials 的键有值，其余一律 `-`（见上表第四格的说明）。
POLICY_CRED_KEY=""
# 第五格：这个值本机造得出来吗（local-csprng / external-issuer；非凭据类键一律 `-`）。
POLICY_CRED_ORIGIN=""
# 第六格：签名密钥回落链上排在本键前面的那几级（按级序；`none` = 本键就是第 1 级；`-` = 不在链上）。
POLICY_SIGNING_ABOVE=""
sw_env_policy() {
  case "$1" in
    SW_UI_TOKEN)
      POLICY_VALUE_SOURCE="credentials"; POLICY_DISPLAY="secret"; POLICY_GATE="signing_secret"
      POLICY_CRED_KEY="${SW_OPS_CREDENTIALS_UI_TOKEN_KEY}"
      POLICY_CRED_ORIGIN="local-csprng"
      POLICY_SIGNING_ABOVE="SW_TELEGRAM_SIGNING_SECRET" ;;
    SW_TELEGRAM_SIGNING_SECRET)
      POLICY_VALUE_SOURCE="credentials"; POLICY_DISPLAY="secret"; POLICY_GATE="signing_secret"
      POLICY_CRED_KEY="${SW_OPS_CREDENTIALS_TELEGRAM_SIGNING_SECRET_KEY}"
      POLICY_CRED_ORIGIN="local-csprng"
      POLICY_SIGNING_ABOVE="none" ;;
    TELEGRAM_BOT_TOKEN)
      POLICY_VALUE_SOURCE="credentials"; POLICY_DISPLAY="secret"; POLICY_GATE="signing_secret"
      POLICY_CRED_KEY="${SW_OPS_CREDENTIALS_TELEGRAM_BOT_TOKEN_KEY}"
      POLICY_CRED_ORIGIN="external-issuer"
      POLICY_SIGNING_ABOVE="SW_TELEGRAM_SIGNING_SECRET SW_UI_TOKEN" ;;
    SW_USE_FAKE_PUBLISHERS)
      POLICY_VALUE_SOURCE="argv"; POLICY_DISPLAY="plain"; POLICY_GATE="real_publish"
      POLICY_CRED_KEY="-"; POLICY_CRED_ORIGIN="-"; POLICY_SIGNING_ABOVE="-" ;;
    SW_LLM_BACKEND)
      POLICY_VALUE_SOURCE="argv"; POLICY_DISPLAY="plain"; POLICY_GATE="llm_backend_creds"
      POLICY_CRED_KEY="-"; POLICY_CRED_ORIGIN="-"; POLICY_SIGNING_ABOVE="-" ;;
    SW_GENERATE_ENABLED)
      POLICY_VALUE_SOURCE="argv"; POLICY_DISPLAY="plain"; POLICY_GATE="none"
      POLICY_CRED_KEY="-"; POLICY_CRED_ORIGIN="-"; POLICY_SIGNING_ABOVE="-" ;;
    SW_TELEGRAM_ENABLED)
      POLICY_VALUE_SOURCE="argv"; POLICY_DISPLAY="plain"; POLICY_GATE="confirm_carrier"
      POLICY_CRED_KEY="-"; POLICY_CRED_ORIGIN="-"; POLICY_SIGNING_ABOVE="-" ;;
    WECHAT_AUTO_PUBLISH)
      POLICY_VALUE_SOURCE="argv"; POLICY_DISPLAY="plain"; POLICY_GATE="wechat_certified"
      POLICY_CRED_KEY="-"; POLICY_CRED_ORIGIN="-"; POLICY_SIGNING_ABOVE="-" ;;
    WECHAT_CERTIFIED)
      POLICY_VALUE_SOURCE="argv"; POLICY_DISPLAY="plain"; POLICY_GATE="wechat_claim"
      POLICY_CRED_KEY="-"; POLICY_CRED_ORIGIN="-"; POLICY_SIGNING_ABOVE="-" ;;
    DAILY_TOKEN_BUDGET|DAILY_RENDER_SECONDS_BUDGET|DAILY_IMAGE_BUDGET)
      POLICY_VALUE_SOURCE="argv"; POLICY_DISPLAY="plain"; POLICY_GATE="none"
      POLICY_CRED_KEY="-"; POLICY_CRED_ORIGIN="-"; POLICY_SIGNING_ABOVE="-" ;;
    *)
      return 1 ;;
  esac
}

# 从白名单派生"哪些键满足某一格"，只给**报错文案**用。
# 【为什么不手写一份枚举】文件头那条规矩：数字（或名单）写在这、被数的东西在别处，扩容时
# 必然对不上——本文件在三个位置踩过这个坑，而本轮又有两处文案在数凭据类键。派生一次就没有
# 第二份真相。跑在**子 shell** 里：sw_env_policy 是靠副作用回话的，直接循环会把调用方刚取好的
# 那一份 POLICY_* 覆盖掉。
# 用法：sw_env_keys_where gate signing_secret / sw_env_keys_where value_source credentials
sw_env_keys_where() {
  local field="$1" want="$2"
  (
    local k got joined=""
    # shellcheck disable=SC2066,SC2086
    # 这里**故意**对 SW_ENV_WHITELIST 做词拆分（同 --show 那一处，元素全是硬编码的大写标识符）。
    for k in ${SW_ENV_WHITELIST}; do
      sw_env_policy "${k}"
      case "${field}" in
        gate)         got="${POLICY_GATE}" ;;
        value_source) got="${POLICY_VALUE_SOURCE}" ;;
        cred_origin)  got="${POLICY_CRED_ORIGIN}" ;;
        *)            return 1 ;;
      esac
      if [[ "${got}" == "${want}" ]]; then
        if [[ -z "${joined}" ]]; then joined="${k}"; else joined="${joined}、${k}"; fi
      fi
    done
    printf '%s' "${joined}"
  )
}

# 值的合法形状，同样按键列举。远端会用同一条正则再校验一次（纵深防御），所以这里返回的
# 是正则本身而不是一个布尔——单一真相源，避免"本地放行、远端拒绝"或反过来的漂移。
#
# 【布尔键为什么只认 `true` / `false` 两个词——理由本轮重写过，旧说法不准确】
# 先把事实摆正：pydantic **确实**认 `TRUE` / `True` / `1` / `yes` / `on` / `t`
# （本机 `TypeAdapter(bool).validate_python` 逐个实测过）。所以拒绝它们**不是**因为
# "设了不生效"——上一版这里写的"避免看起来设上了其实没有"那句话是错的，本轮更正。
# 真实理由有三条，第三条最要命：
#   ① 同一个语义有六七种写法时，"这个开关到底是开是关"要靠记住 pydantic 的真值表才能回答，
#      而这恰恰是运维最需要一眼看懂的东西；
#   ② `--show` 与 `verify.sh` 打印的是 `.env` 原文，写法不统一时人得在脑子里做一次转换；
#   ③ **`0` 和 `1` 在这张白名单里是歧义的**：三个 `DAILY_*_BUDGET` 就在同一张表上，那里
#      `0` 是整数"全停"，而在布尔键上 `0` 是 `false`、`1` 是 `true`。同一个字符在相邻的
#      两个键上意思完全不同，抄错一次就是一次静默事故。所以布尔键只认两个**单词**。
# 这是**主动收紧**：宁可拒绝一个本来能用的写法，也不要留下一条很难查的路径。
#
# 【整数键为什么拒绝 `-1` / `007` / `1_000_000`】同样都是 pydantic 认、而我们不认：
#   `-1`        pydantic 认（得到 -1）。而 core/budget.py:118-122 的 remaining() 是
#               `max(limit - used, 0)`，负数上限与 0 一样是**全停**——有人写 `-1` 想表达
#               "不限"时会得到"全停"，这是本仓最不能接受的那类反向故障，必须挡在门口。
#   `007`       pydantic 认（得到 7）。挡掉是为了不引入八进制联想。
#   `1_000_000` pydantic 认（得到 1000000）。挡掉是为了让 `.env` 里的值可以直接 grep 比对。
sw_env_value_re() {
  case "$1" in
    # 【本机 CSPRNG 造出来的那两个凭据类键共用同一份字符集白名单】签名密钥其实只被
    # core/telegram.py 当 HMAC 的 key 用（对字符集没有额外要求），这里仍然收到同一个集合里，
    # 理由是它与 token 走**同一条**通路：printf '%q' → ssh stdin → 远端 export → 写进 .env。
    # 两个值形状一致，"这条路上什么字符是安全的"就只需要论证一次。
    SW_UI_TOKEN|SW_TELEGRAM_SIGNING_SECRET) printf '%s' "${SW_OPS_UI_TOKEN_ALLOWED_RE}" ;;
    # 【bot token 是第一个有**自己形状**的凭据类键，所以先把"钉多紧"这件事说清】
    # Telegram 官方从来没给 token 定过格式契约：Bot API 文档只说它"looks something like
    # `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`"。所以这里只钉文档确实说了的那一半
    # ——`<数字 bot_id>:<授权串>`——长度取一个**宽到不会误伤真 token**的区间：
    #   · 授权串：手上两份官方样例（Bot API 文档那个、BotFather 教程里的
    #     `4839574812:AAFD39kkdpWt3ywyRZergyOLMaJhac60qc`）都是 34 字符，而业界的秘密扫描
    #     规则普遍按 35 写（`A` + 34）。34 与 35 都落在 30-64 里，不必赌哪个对。
    #   · bot_id：它就是 Telegram 的 user id。官方样例短到 6 位，而 id 空间在向 2^52
    #     （十进制 16 位）扩，所以取 5-16。
    # 【为什么不敢再紧一格——两侧代价都不小，所以只钉能确定的那一半】
    #   误拒一个真 token = 把人锁在"工具面告诉你该换、却换不了"，那正是本仓刚修过的病；
    #   误收一个错值   = core 拿着一个 Telegram 不认的 token，长轮询**不会退出**，它按
    #                    2s→120s 退避无限重试（core/telegram.py:810-831），而
    #                    /api/v1/system/telegram 的 polling 只看线程活没活
    #                    （core/telegram.py:978-988 channel_status），照样报 true。
    #   也就是说错值**不响**。这道形状校验拦得住的只是"粘错了东西"（chat_id、64 位十六进制
    #   密钥、URL、被截断的值、带引号或空白的值）；"这个 token 到底能不能用"要靠换完之后
    #   verify.sh 那几格取证，本脚本不假装答得了。
    # 【字符集不用另论证】这条形状允许的字符（数字、`:`、A-Za-z0-9_-）全都落在
    # SW_OPS_UI_TOKEN_ALLOWED_RE 里面，是它的**真子集**，所以上面那条通路论证原样适用。
    # tests/ops/test_env_set.sh 有一条逐字符的源码级断言钉住这个子集关系。
    TELEGRAM_BOT_TOKEN) printf '%s' '^[0-9]{5,16}:[A-Za-z0-9_-]{30,64}$' ;;
    # 布尔五连：core/config.py:53 / :82 / :351 / :233 / :231 都声明成 bool
    SW_USE_FAKE_PUBLISHERS|SW_GENERATE_ENABLED|SW_TELEGRAM_ENABLED|WECHAT_AUTO_PUBLISH|WECHAT_CERTIFIED)
      printf '%s' '^(true|false)$' ;;
    # core/config.py:99 `sw_llm_backend: Literal["anthropic", "dsh"]`——合法值以这行为准，
    # 不照抄文档：文档里出现过 gateway / deepseek-official 之类，那些是 dsh 的**路由名**
    # （configs/dsh/cordis.yml 里的 provider），不是 SW_LLM_BACKEND 的取值。
    SW_LLM_BACKEND) printf '%s' '^(anthropic|dsh)$' ;;
    # 非负十进制整数，无前导零、无下划线、无正负号、最多 10 位。
    DAILY_TOKEN_BUDGET|DAILY_RENDER_SECONDS_BUDGET|DAILY_IMAGE_BUDGET)
      printf '%s' '^(0|[1-9][0-9]{0,9})$' ;;
    *) return 1 ;;
  esac
}

# 给人看的"这个键该填什么"，同样按键列举。它与上面那条正则是**同一件事的两种表述**，
# 必须一起改：正则决定放不放行，这段决定被拒时人看到什么。分开写是因为正则不能自解释
# （`^(0|[1-9][0-9]{0,9})$` 对着终端念不出"非负整数、不许前导零"）。
sw_env_value_help() {
  case "$1" in
    SW_UI_TOKEN|SW_TELEGRAM_SIGNING_SECRET)
      printf '%s' '凭据，不经 --value 传（用 --generate / --from-credentials）' ;;
    TELEGRAM_BOT_TOKEN)
      printf '%s' '<数字 bot_id>:<授权串>，由 BotFather 签发 —— 本机造不出来，所以没有 --generate，只走 --from-credentials' ;;
    SW_USE_FAKE_PUBLISHERS|SW_GENERATE_ENABLED|SW_TELEGRAM_ENABLED|WECHAT_AUTO_PUBLISH|WECHAT_CERTIFIED)
      printf '%s' 'true 或 false —— 只认这两个**小写单词**' ;;
    SW_LLM_BACKEND) printf '%s' 'anthropic 或 dsh —— 以 core/config.py:99 的 Literal 声明为准' ;;
    DAILY_TOKEN_BUDGET|DAILY_RENDER_SECONDS_BUDGET|DAILY_IMAGE_BUDGET)
      printf '%s' '非负十进制整数：不许前导零、下划线、正负号，最多 10 位' ;;
    *) return 1 ;;
  esac
}

# 【等价别名——只有一个键有，但它是个真陷阱】pydantic 的 AliasChoices 让一个字段认多个
# 环境变量名。本白名单里只有 DAILY_IMAGE_BUDGET 是这种情况（core/config.py:375-378
# `AliasChoices("daily_image_budget", "sw_daily_image_budget")`）。
# 陷阱在 `--show`：`.env` 里只写了 `SW_DAILY_IMAGE_BUDGET=5` 时，只看主名会答"未设置"，
# 而人据此以为默认值 40 在生效——这正是本仓最不能接受的那类"回答得很确定但答错了"。
# 所以 --show 对有别名的键会把别名也查一遍。本机实测过取值优先级（AliasChoices 顺序：
# 主名先、别名后，两个都在时主名赢）。返回 `-` 表示这个键没有别名。
sw_env_alias() {
  case "$1" in
    DAILY_IMAGE_BUDGET) printf '%s' 'SW_DAILY_IMAGE_BUDGET' ;;
    SW_UI_TOKEN|SW_TELEGRAM_SIGNING_SECRET|TELEGRAM_BOT_TOKEN|SW_USE_FAKE_PUBLISHERS|SW_LLM_BACKEND|SW_GENERATE_ENABLED)
      printf '%s' '-' ;;
    SW_TELEGRAM_ENABLED|WECHAT_AUTO_PUBLISH|WECHAT_CERTIFIED|DAILY_TOKEN_BUDGET|DAILY_RENDER_SECONDS_BUDGET)
      printf '%s' '-' ;;
    *) return 1 ;;
  esac
}

# 【第四张按键表：动手前的警告】它回答的是"改这个键会发生什么"，与闸门是两件事——
# 闸门管"拦不拦"，这里管"不拦也得让人知道"。每个键都必须在这里有一格，包括那些
# **刻意没有警告**的键：显式写一句"无"比让它落进 `*)` 兜底强，后者会让"漏了一个键"
# 与"这个键确实没什么可说的"变成同一种表现。
#
# 读全局：KEY 之外还读 VALUE（方向不同话术不同）。凭据键没有 VALUE，它那一格不看方向。
sw_env_warn() {
  case "$1" in
    SW_UI_TOKEN)
      warn "副作用：生产 .env 里 SW_TELEGRAM_SIGNING_SECRET 为空时（.env.example 的默认形态就是空），"
      note "        Telegram 确认卡的 HMAC 签名密钥会按 SW_TELEGRAM_SIGNING_SECRET → SW_UI_TOKEN → bot token"
      note "        的顺序回落（core/telegram.py:151-154）。改了 SW_UI_TOKEN 就等于换了签名密钥，"
      note "        **此前已推出去、还没人点的确认卡按下去会验签失败**（core/telegram.py:901 起，"
      note "        日志记 bad_signature，用户侧表现为按钮没反应），并最终被 TTL 自动驳回。"
      note "闸门：这条前置**不再靠人记得**（docs/RISKS.md §8.5 的第 0 步 2026-08-22 就被跳过过一次）。"
      note "      写入前本脚本会先读生产 .env：SW_TELEGRAM_SIGNING_SECRET 已显式设且非空就直接放行"
      note "      （那时换 UI token 动不到签名密钥）；否则去读待人点的确认卡条数，0 条放行、有卡拒绝、"
      note "      读不出来 fail-closed。"
      note "根治：把 SW_TELEGRAM_SIGNING_SECRET 显式设上（本脚本现在能做：--key SW_TELEGRAM_SIGNING_SECRET"
      note "      --generate），此后 SW_UI_TOKEN 再怎么换都不会动签名密钥，这道闸门也就不再拦它。"
      ;;
    SW_TELEGRAM_SIGNING_SECRET)
      warn "这个键**就是** Telegram 确认卡 callback_data 的 HMAC 签名密钥（core/telegram.py:151-154 三级回落的第一级）。"
      note "        设它 / 换它**总是**会改变生效的签名密钥：要么把回落从第二三级拉回第一级，要么换掉已有的第一级。"
      note "        后果与换 SW_UI_TOKEN 那条完全一样——**已推出去、还没人点的确认卡按下去会验签失败**"
      note "        （日志 bad_signature，用户侧表现为按钮没反应），最终被 TTL 自动驳回。"
      note "闸门：所以这个键**没有免检方向**，每次都去读待人点的确认卡条数：0 条放行、有卡拒绝、读不出来 fail-closed。"
      note "价值：这一次代价换来的是**解耦**。显式设上之后，回落链停在第一级，SW_UI_TOKEN 就再也不是签名密钥了——"
      note "      此后轮换 UI token（换鉴权、疑似泄漏、例行更换）不会再牵连任何一张确认卡。"
      note "      挑一个待人点的确认卡为 0 的时刻做这一次，之后那条耦合就永久解开了。"
      ;;
    TELEGRAM_BOT_TOKEN)
      warn "换 bot token 换的是**整条推送载体的身份**：BotFather 一签发新 token，**旧 token 当场作废**。"
      note "        长轮询线程不会因此退出——core/telegram.py:810-831 的 _loop 抓到 TelegramError 之后按 2s→120s 退避**无限重试**。"
      note "        于是 /api/v1/system/telegram 的 polling 仍然报 true（core/telegram.py:978-988 的 channel_status 只看线程活没活），"
      note "        **换 token 期间这一格是骗人的**：要判通道真的活没活，看同一份响应里的 last_error 与 stats.errors，别只看 polling。"
      note "        这也正是本键禁用 --write-only 的实质理由：新 token 写进 .env 而容器没重建时，运行中的 core 还拿着一个已经作废的旧 token，"
      note "        从这一刻起一张卡都推不出去，而没有任何一格会因此变红。"
      warn "签名密钥：bot token 是 core/telegram.py:151-154 三级回落的**第三级**。"
      note "        只有 SW_TELEGRAM_SIGNING_SECRET 与 SW_UI_TOKEN **都为空**时它才是生效的签名密钥——那时换它就是换签名密钥。"
      note "        闸门先读生产 .env 判这一格：上面两级里任一非空就直接放行（换 bot token 动不到签名密钥）；"
      note "        两级都空才去读待人点的确认卡条数，0 条放行、有卡拒绝、读不出来 fail-closed。"
      warn "409 双轮询：**同一个新 token** 若被粘进两个部署，两边都会 getUpdates，Telegram 只喂一个，另一边持续 error_code=409（docs/RISKS.md 第 1 条的老账）。"
      note "        本脚本**刻意不在这上面设闸门**：verify.sh 数的是近 2000 行日志里的 error_code=409，那是个比 awaiting_confirm 弱得多的信号——"
      note "        历史行会把新冲突淹掉（旧账让它假红），而真正的新冲突要等下一次轮询失败才写得进日志（时间窗让它假绿）。既漏又误的判据不该拿来拦人。"
      note "        所以这一格只做**提示**：换完之后跑一次 bash scripts/ops/verify.sh，看「Telegram 轮询冲突（error_code=409）」那一格。"
      note "        真撞上 409，处置是让另一个部署停下来、或给它换一个 bot，而**不是**再换一次 token（再换一次只会把冲突原样搬到新 token 上）。"
      note "取值：本机造不出来（值由 BotFather 签发），所以**没有 --generate**。人拿到之后写进 ${CRED_FILE} 的 ${SW_OPS_CREDENTIALS_TELEGRAM_BOT_TOKEN_KEY} 键（0600），再 --from-credentials 推上去。"
      ;;
    SW_USE_FAKE_PUBLISHERS)
      if [[ "${VALUE}" == "false" ]]; then
        warn "SW_USE_FAKE_PUBLISHERS=false 表示**真发布开启**：此后经人工确认的内容会真的发到平台上。"
        note "本次会：**先探确认通道（事前预防）** → 备份 .env → 原子写入 → 重建容器让它生效 → 调 scripts/ops/restart.sh 走那道 R1 红线闸门（事后检测）"
        note "两道闸门的要求相同：人工确认闸门通道 enabled && ready && polling 三者皆真，否则本脚本以失败告终（fail-closed）"
        note "事前那道在**写 .env 之前**判：不通过就什么都不做，.env 一个字节不动、连备份都不建"
        note "探不到通道（连不上 / 超时 / 401）与探到了但通道不活，是**两件事**：文案与退出码都不同，但都拒绝写入（默认 fail-closed，理由写在脚本注释里）"
        note "如实说明：事后那道仍然是**事后检测**——它触发时带着新值的 core 已经在跑了（docs/RISKS.md 第 12 条同一条局限）。事前预防挡的是可预见的那一半，挡不住写入到生效之间才发生的变化"
      else
        note "这是**回到安全状态**的方向（真发布关闭，一切改走模拟发布器），所以**不设任何闸门**：出事时人必须能一条命令退回来。"
      fi
      ;;
    SW_LLM_BACKEND)
      warn "切换 LLM 后端会换掉**整条出稿链**的执行方式（generation/llm.py:604-609 按这个值二选一）。"
      note "闸门：写入前先核对**目标后端**的凭据在生产 .env 里存在且非空——两个方向都查。"
      note "      这条闸门的意义就是不让「回退」把 core 换到一个起不来的后端上：generation/llm.py:271-278 是**懒加载**，缺 key 时进程照常起来、直到第一次真出稿才抛 LLMUnavailable，那时故障已经在排期里了。"
      if [[ "${VALUE}" == "anthropic" ]]; then
        note "      anthropic 要的是 ANTHROPIC_API_KEY（core/config.py:100）。"
        note "成本：anthropic 直连 Claude Messages API，是**真花钱**的那条路；切过去之后 DAILY_TOKEN_BUDGET 就是唯一的止损闸（core/budget.py）。"
      else
        note "      dsh 要的那个变量名由 .env 里的 SW_DSH_PROVIDER 决定（core/config.py:140 默认 deepseek-official），映射表在 configs/dsh/cordis.yml 里，远端闸门会替你查。"
        note "依赖：dsh 后端要起一个 deepseek-harness runtime 子进程（generation/llm_dsh.py）。凭据齐备**不等于** runtime 装好了——这条闸门只答得了前一半。"
      fi
      ;;
    SW_GENERATE_ENABLED)
      if [[ "${VALUE}" == "false" ]]; then
        warn "SW_GENERATE_ENABLED=false 是**只停出稿、不停发布**的刹车：core/scheduler.py:264-266 让 tick_generate 空转直接返回。"
        note "已经生成好、已排期、已确认的内容**照样会到点发出去**——要连发布一起停，那是另一件事（把账号停用，或改 SW_USE_FAKE_PUBLISHERS）。"
        note "手动出稿不受影响：POST /dev/tick/generate 与工作台的手动生成仍然可用。"
      else
        note "恢复自动出稿。注意预算闸门是独立的：DAILY_TOKEN_BUDGET 见底时照样只出选题不出稿。"
      fi
      ;;
    SW_TELEGRAM_ENABLED)
      if [[ "${VALUE}" == "false" ]]; then
        warn "SW_TELEGRAM_ENABLED=false 会拆掉确认卡的**推送载体**：core/telegram.py:650-654 的 build_telegram_notifier() 直接返回 None，一条卡都推不出去。"
        note "**因果要写准**：这**不是**「内容会越权发出去」。恰恰相反——core/scheduler.py:498-505 的人工确认闸门看的是 item.confirmed_at，没人点就跳过不发（记 skipped_unconfirmed）。R1 红线不因此失效。"
        note "真正的后果是**静默停摆再静默丢弃**：内容堆在排期处，而 core/confirm.py:571-573 的 TTL 在一次都没推成功过时从 scheduled_at 起算，到点（SW_CONFIRM_TTL_HOURS，默认 24 小时）自动驳回并释放槽位。"
        note "第二载体仍在：工作台的「确认发布」按钮不受 Telegram 影响，走的是**同一个**后端函数（core/api/content.py:283-297 → core/confirm.py:315 confirm_item）。但那要求人知道主载体已经没了。"
        note "闸门：真发布正开着（.env 里 SW_USE_FAKE_PUBLISHERS=false）时**拒绝**执行。理由不是洁癖——那种组合会被 restart.sh 的 R1 闸门必然判红，与其改完生产再失败，不如写入前就停手。"
      else
        note "装回确认卡的推送载体，不设闸门。装回来之后请跑一次 bash scripts/ops/verify.sh 确认 enabled/ready/polling 三格都真。"
      fi
      ;;
    WECHAT_AUTO_PUBLISH)
      if [[ "${VALUE}" == "true" ]]; then
        warn "WECHAT_AUTO_PUBLISH=true 只是公众号**双确认闸门的第一道**（publishers/wechat_mp/publisher.py:238-249）。"
        note "三道全真才会 freepublish：server_switch（本键）+ account_certified（WECHAT_CERTIFIED）+ confirm_publish（逐条内容，由审核 UI 写入）。"
        note "闸门：WECHAT_CERTIFIED 不是 true 时**拒绝**执行。不是因为危险，恰恰相反——那样改完是个**不会生效的空操作**（照旧只落草稿箱），而人会以为自动发布已经开了。scripts/preflight.py:122-133 对同一组合的裁定也是 FAIL，这里与它同口径。"
      else
        note "回到只落草稿箱（最安全的默认值，core/config.py:233 的出厂值就是 false），不设闸门。"
      fi
      ;;
    WECHAT_CERTIFIED)
      if [[ "${VALUE}" == "true" ]]; then
        warn "WECHAT_CERTIFIED 记的**不是我们的决定，是微信那边的事实**：这个主体到底认证没有。本工具面**核实不了它**，只能照抄你填的值。"
        note "写错的代价说准：它不会让内容越权发出去。发布链路会先把稿子存进草稿箱（publishers/wechat_mp/publisher.py:294 draft_add 已成功），"
        note "        然后在 freepublish 那一步撞上 errcode=48001「无接口权限」，由 publishers/wechat_mp/client.py:338 抛 PermanentError——**响、快、按条报，不是静默故障**。"
        note "怎么真的确认：公众号后台的认证状态为准；本仓侧 scripts/preflight.py:88-95 会把这一格作为 OK/WARN 如实打出来。"
        note "闸门：**这个方向刻意不拦**。理由不是宽容，是这道题本工具面结构上答不了（详见远端 wechat_claim 分支的注释）；"
        note "        但写入前会去 .env 读 WECHAT_AUTO_PUBLISH，告诉你这次变更会不会让平台级自动发布**当场生效**。"
      else
        note "把认证状态记回 false：双确认闸门的第二道随之关上，公众号内容退回只落草稿箱。这是安全方向，不设闸门。"
        note "注意它与 WECHAT_AUTO_PUBLISH 是**两件事**：这一格记的是事实（号认证没有），那一格是我们的开关（要不要自动发）。"
      fi
      ;;
    DAILY_TOKEN_BUDGET|DAILY_RENDER_SECONDS_BUDGET|DAILY_IMAGE_BUDGET)
      warn "预算是**按 UTC 日**重置的硬上限（core/budget.py:41-56 today_key），不是软提示：超了直接抛 BudgetExhausted，上层降级为只出选题不出稿并告警。"
      note "**0 不是「不限」，0 是「当天全停」**：core/budget.py:118-122 的 remaining() 是 max(limit - used, 0)，上限为 0 时任何一次 ensure/charge 都会当场抛。"
      note "本仓**没有**「不限」这个语义，任何哨兵值都没有。想放开就把上限调大，别指望 0 或负数（负数已被取值校验挡掉，它的实际效果与 0 一样是全停）。"
      if [[ "$1" == "DAILY_IMAGE_BUDGET" ]]; then
        note "这个键有等价别名 SW_DAILY_IMAGE_BUDGET（core/config.py:375-378）。两个都在 .env 里时**主名赢**（本机实测过 AliasChoices 的顺序）；本脚本写的就是主名。"
      fi
      ;;
    *)
      return 1 ;;
  esac
}

# 红线 R7 的纵深防御。
#
# `.env` 里出现 `DSH_` / `XDG_` / `DYLD_` / `BASH_FUNC_` 前缀的变量名时 dsh 会**拒绝启动，
# 无开关**（SW-AGENT.md §2 R7、docs/OPS.md 7.5.1.2；本项目自己那几个已改名成 `SW_DSH_*`）。
# 上面那张白名单已经排除了这种可能——这道校验因此**今天是冗余的**。留着它的理由只有一个：
# 将来若有人扩白名单，这道校验还在，而且它同时长在本机与远端两侧。
# 它是纵深防御，不是主防线；主防线永远是白名单本身。
sw_env_check_forbidden_prefix() {
  case "$1" in
    DSH_*|XDG_*|DYLD_*|BASH_FUNC_*)
      die "键名 ${1} 命中红线 R7 的禁止前缀（DSH_ / XDG_ / DYLD_ / BASH_FUNC_）" \
        "带这些前缀的变量名一旦进 .env，dsh 会拒绝启动，而且**没有开关**可以绕开。" \
        "出处：SW-AGENT.md 第 2 节 R7、docs/OPS.md 7.5.1.2。本项目自己那几个已改名成 SW_DSH_*。" \
        "这道校验是纵深防御：白名单本来就不含这类键名，命中它说明白名单被人扩过了。"
      ;;
  esac
}

# ------------------------------------------------------------------------- 参数
MODE=""
KEY=""
VALUE=""
VALUE_GIVEN=0
TOKEN_SOURCE_MODE=""
WRITE_ONLY=0
ACCEPT_BREAKING=0

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --show)
      [[ -z "${MODE}" ]] || die "--show 与 --key 只能选一个"
      MODE="show"; shift ;;
    --key)
      [[ -z "${MODE}" ]] || die "--show 与 --key 只能选一个，且 --key 只能给一次"
      [[ "$#" -ge 2 ]] || die "--key 后面要跟键名"
      MODE="set"; KEY="$2"; shift 2 ;;
    --value)
      [[ "${VALUE_GIVEN}" -eq 0 ]] || die "--value 只能指定一次"
      [[ "$#" -ge 2 ]] || die "--value 后面要跟值"
      VALUE="$2"; VALUE_GIVEN=1; shift 2 ;;
    --generate)
      [[ -z "${TOKEN_SOURCE_MODE}" ]] || die "--generate 与 --from-credentials 只能选一个"
      TOKEN_SOURCE_MODE="generate"; shift ;;
    --from-credentials)
      [[ -z "${TOKEN_SOURCE_MODE}" ]] || die "--generate 与 --from-credentials 只能选一个"
      TOKEN_SOURCE_MODE="from-credentials"; shift ;;
    --write-only)
      WRITE_ONLY=1; shift ;;
    # 名字刻意长且说得出后果。**绝不叫 --force**：那种名字什么也没说，用的人不会在按下去
    # 之前想一遍自己接受了什么，而这里要接受的是"别人已经收到的那张卡按下去不会有反应"。
    --accept-breaking-pending-confirm-cards)
      ACCEPT_BREAKING=1; shift ;;
    -h|--help)
      usage ;;
    *)
      printf '\n✗ 无法识别的参数：%s\n\n' "$1" >&2; usage ;;
  esac
done

[[ -n "${MODE}" ]] || usage
command -v ssh >/dev/null 2>&1 || die "本机没有 ssh 命令"

# 备份时间戳在本机生成并传给远端：外层因此知道备份路径，失败提示里能把它打出来。
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
[[ "${STAMP}" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || die "生成的 UTC 时间戳格式异常：${STAMP}"

# ============================================================== 只读：--show
#
# 【它只回答"设了没有"】读取能力刻意不能回显凭据值（红线 R5）。非凭据键的值可以显示，
# 理由见上面 sw_env_policy() 的策略表——**这是按键决定的，不是启发式**。
# 远端只看白名单里那几个键（外加 DAILY_IMAGE_BUDGET 的等价别名），`.env` 里其余任何一行
# 都不读、不数、不打印。
#
# 【要查的键从 SW_ENV_WHITELIST 派生，不再手写第二份清单】上一版这里硬编码着两个键，
# 与白名单是两处真相；键一多，"白名单加了键但 --show 看不见"就是迟早的事，而那种漏法
# 恰恰无声无息。现在本地按白名单逐个查 sw_env_policy / sw_env_alias 拼出参数，任一函数
# 缺一格都会当场 die，不会悄悄少查一个键。
show_remote_script() {
  cat <<'REMOTE'
{
set -uo pipefail
remote_status=0
bash -s -- "$@" <<'REMOTE_SHOW' || remote_status=$?
{
set -euo pipefail
# ！！【stdin 的结构性保证——改本段任何一行之前先读完这一段】
# 与其余那几个远端脚本同一套口径（走 ssh 的那批 ops 脚本，见 env_set_remote_script 上方
# 那段说明）。这一项刻意不给一条权威 grep：它的特征串就是那段组重定向本身，而讲它的注释
# 里也全是同一串，grep 分不开"真在用"与"只是在讲"——ui_token.sh 就会被误列进来，它自己
# 不用组，它是往别人的组里发射。逐字照抄它们的理由：
# 这层 bash 的脚本正文曾经**就是它自己的 stdin**（正上方这个 REMOTE_SHOW heredoc），
# 任何读 stdin 的子进程都会把"脚本剩下的部分"吞掉，而脚本仍以 0 收尾。
# 现在靠包住整段正文的那对花括号和尾部的 `} </dev/null` 结构性堵死：
#   ① `{ ... }` 是一条复合命令，bash 必须整条解析完才开始执行，正文因此在第一条命令
#      跑起来之前就已经离开输入流；
#   ② `</dev/null` 挂在整个组上，组内所有命令与子进程的 fd 0 都是 /dev/null。
# 下面那些逐条 `</dev/null` 已从"承重"降级为纵深防御，但一条都不删。
missing_status="$1"; shift

env_file="${HOME}/social_workflow/.env"
if [[ ! -f "${env_file}" ]]; then
  printf '✗ 远端 %s 不存在，无法回答任何一个键的状态。\n' "${env_file}" >&2
  exit "${missing_status}"
fi

# 参数形如 `SW_UI_TOKEN:secret:-` —— 键名、display 策略、等价别名（`-` 表示没有）。
# 三格全由本地那三张按键表决定并传进来，远端不自作主张、也不认识白名单本身。
scan_key=""      # 本轮要数的名字（主名或别名）
scan_hits=0
scan_value=""
scan_env() {
  scan_hits=0
  scan_value=""
  local scan_prefix="${scan_key}="
  while IFS= read -r line || [[ -n "${line}" ]]; do
    case "${line}" in
      "${scan_prefix}"*)
        scan_hits=$((scan_hits + 1))
        # 用偏移切片取值，不用 ${var#pattern}：模式里嵌引号在 bash 3.2/5.x 之间有解析差异，
        # 而键名的长度是确定的，切片没有任何歧义。
        scan_value="${line}"
        scan_value="${scan_value:${#scan_prefix}}"
        ;;
    esac
  done <"${env_file}"
}

for spec in "$@"; do
  key="${spec%%:*}"
  spec_rest="${spec#*:}"
  policy="${spec_rest%%:*}"
  alias_key="${spec_rest#*:}"
  scan_key="${key}"
  scan_env
  hits="${scan_hits}"
  value="${scan_value}"
  if [[ "${hits}" -eq 0 ]]; then
    printf '  %-28s 未设置\n' "${key}"
  elif [[ "${policy}" == "plain" ]]; then
    printf '  %-28s 已设置  %s\n' "${key}" "${value}"
  else
    # 凭据：只报"设了"，**不报值、也不报长度**——长度也是信息。
    printf '  %-28s 已设置（凭据，值不回显：红线 R5）\n' "${key}"
  fi
  if [[ "${hits}" -gt 1 ]]; then
    printf '  %-28s ⚠ 同名键出现 %s 次，dotenv 语义下后一条覆盖前一条，本脚本拒绝写入这样的 .env\n' "" "${hits}"
  fi
  # 等价别名：只有本地那张表说这个键有别名时才查。**这一格不是装饰**——
  # 别名单独在 .env 里而主名没有时，只报"未设置"会让人以为出厂默认值在生效，
  # 那是一次回答得很确定的错答。
  if [[ "${alias_key}" != "-" ]]; then
    scan_key="${alias_key}"
    scan_env
    if [[ "${scan_hits}" -gt 0 && "${hits}" -eq 0 ]]; then
      printf '  %-28s ⚠ 主名未设置，但等价别名 %s=%s 在 .env 里——**生效的是它**，不是出厂默认值\n' \
        "" "${alias_key}" "${scan_value}"
    elif [[ "${scan_hits}" -gt 0 ]]; then
      printf '  %-28s ⚠ 等价别名 %s=%s 也在 .env 里；两者都在时**主名赢**，别名那行是死配置\n' \
        "" "${alias_key}" "${scan_value}"
    fi
  fi
done
# 防回归哨兵：它排在本段所有边界命令之后，一旦从输出里消失，就说明有命令又把脚本正文吞了。
printf '\n读取完毕（只看白名单里的键，.env 其余内容一个字节都没读进输出）\n'
exit 0
} </dev/null
REMOTE_SHOW
if [[ "${remote_status}" -eq 255 ]]; then
  exit 254
fi
exit "${remote_status}"
} </dev/null
REMOTE
}

if [[ "${MODE}" == "show" ]]; then
  [[ "${VALUE_GIVEN}" -eq 0 ]] || die "--show 不接受 --value"
  [[ -z "${TOKEN_SOURCE_MODE}" ]] || die "--show 不接受 --generate / --from-credentials"
  [[ "${WRITE_ONLY}" -eq 0 ]] || die "--show 本来就只读，不接受 --write-only"
  [[ "${ACCEPT_BREAKING}" -eq 0 ]] || die "--show 本来就只读，不会碰任何签名密钥，不接受 --accept-breaking-pending-confirm-cards"

  printf '生产 .env 白名单键状态\n\n'
  note "连接 ${SSH_ALIAS}（IAP 首包通常需 5-10 秒）"
  note "只读：不备份、不写入、不重建容器、不重启"
  printf '\n'

  # ssh(1) 不保留 argv 边界：host 之后的参数会被用单个空格拼成一个字符串发给远端，再由
  # 远端登录 shell 重新分词，空参数会就此消失。所以自己造那一个字符串并用 printf '%q'
  # 转义——同一批走 ssh 的 ops 脚本都是这个手法，名单以
  # `grep -l "printf '%q '" scripts/ops/*.sh` 为准。三个位置参数都是本
  # 脚本自己定义的常量，注入面为零。
  # 逐键从三张按键表派生 `键:display:别名`。任一表缺一格，函数返回 1、set -e 当场退出，
  # 不会出现"白名单里有、--show 却看不见"的静默漏项。
  SHOW_SPECS=""
  # shellcheck disable=SC2066,SC2086
  # 这里**故意**对 SW_ENV_WHITELIST 做词拆分——它就是一张空格分隔的键名表，
  # 元素全是本脚本硬编码的大写标识符，既不含空白也不含通配符。
  for whitelist_key in ${SW_ENV_WHITELIST}; do
    sw_env_policy "${whitelist_key}"
    show_alias="$(sw_env_alias "${whitelist_key}")"
    SHOW_SPECS="${SHOW_SPECS}${whitelist_key}:${POLICY_DISPLAY}:${show_alias} "
  done

  show_rc=0
  # shellcheck disable=SC2086
  # 同上：SHOW_SPECS 要按空格拆成多个位置参数，每一段都是刚拼出来的三段式常量。
  ssh -o ConnectTimeout=25 "${SSH_ALIAS}" \
    "bash -s -- $(printf '%q ' "${ENV_MISSING_STATUS}" ${SHOW_SPECS})" \
    < <(show_remote_script) || show_rc=$?
  exit "${show_rc}"
fi

# ============================================================== 变更：--key ...
sw_env_policy "${KEY}" || die \
  "键 ${KEY} 不在白名单里，拒绝执行" \
  "白名单**写死在脚本里，不接受运行时扩展**，当前是：${SW_ENV_WHITELIST}" \
  "为什么不做任意键编辑：生产 .env 里有 LLM key、Telegram bot token、数据库 URL——写坏任何一条 core 都起不来。" \
  "为什么写死：docs/RISKS.md 第 14 条——补一个键**不是往数组里加个元素**，而是要给它想清楚四件事（取值形状、display 策略、生效后怎么核验、这个键特有的闸门）。写死白名单正是为了让「加一个键」必须经过这四问。" \
  "名单上的凭据类键是：$(sw_env_keys_where value_source credentials)（值永不回显，都走 --from-credentials，其中本机造得出来的那几个还能 --generate，并共用同一道签名密钥轮换闸门）。这份名单是从白名单**派生**的，不是手写的第二份真相。" \
  "其余凭据类键（TELEGRAM_CHAT_ID / 各种 API key）**刻意仍不在名单上**：它们各自的闸门还没想清楚，docs/RISKS.md 第 14 条如实记着这个缺口。TELEGRAM_CHAT_ID 尤其别顺手加——它的值是**从生产流出来**的（要在服务器上跑 core.telegram setup 才知道），而「新会话真的收得到卡吗」这道闸门不真发一条 Telegram 消息就验不了。" \
  "真要加，请按上面那四问补齐 sw_env_policy / sw_env_value_re / sw_env_value_help / sw_env_alias / sw_env_warn 五处（sw_env_policy 那一格现在有六列），而不是把这里改成通用编辑器。"
sw_env_check_forbidden_prefix "${KEY}"

# 这三行都用命令替换取值：函数对未登记的键返回 1，`set -e` 会让赋值当场失败退出。
# 也就是说"白名单里加了键却漏补某张表"这件事在**第一次真的去改它**时就会炸，而不是
# 悄悄走一条没校验的路。tests/ops/test_env_set.sh 另有源码级断言把五张表一起数。
VALUE_RE="$(sw_env_value_re "${KEY}")"
VALUE_HELP="$(sw_env_value_help "${KEY}")"

case "${POLICY_VALUE_SOURCE}" in
  argv)
    [[ -z "${TOKEN_SOURCE_MODE}" ]] || die "${KEY} 的值用 --value 给，不接受 --generate / --from-credentials"
    [[ "${VALUE_GIVEN}" -eq 1 ]] || die "${KEY} 需要 --value：${VALUE_HELP}"
    [[ "${VALUE}" =~ ${VALUE_RE} ]] || die \
      "${KEY} 的值不合法（当前给的是：${VALUE}）" \
      "这个键接受的形状：${VALUE_HELP}" \
      "正则（本机与远端用的是同一条）：${VALUE_RE}" \
      "**拒绝不等于「写了不生效」**：pydantic 认的写法比这里宽（TRUE / 1 / yes / -1 / 007 都能解析）。这里主动收紧，理由逐条写在脚本里 sw_env_value_re() 上方。"
    # 布尔量 / 枚举 / 整数都不是凭据（策略表里 display=plain），所以这次赋值不需要 xtrace 守卫。
    SW_OPS_ENV_VALUE="${VALUE}"
    ;;
  credentials)
    # 【"能不能 --generate" 问的是**性质**，不是键名——这一段刻意不写 `case ${KEY}`】
    # TELEGRAM_BOT_TOKEN 是第一个造不出来的凭据类键：它的值由 BotFather 签发，本机 CSPRNG
    # 造出来的 256 bit 随机值 Telegram 一个字都不认。把这条规则写成一个硬编码的键名，等于
    # 在这一整批正在拆的地方又长出一处硬编码，而第二个这样的键（将来的各种 API key）一进来
    # 就会静默走错路。所以判据是策略表第五格 POLICY_CRED_ORIGIN。
    # 三句文案都跟着分支：凭据类键根本没有 --value，照抄一句"改用 --generate"给一个造不出
    # 值的键，就是给了一条走不通的路——而那正是本文件反复在拒绝的那类"回答得很确定但答错了"。
    if [[ "${POLICY_CRED_ORIGIN}" == "external-issuer" ]]; then
      CRED_WAYS="--from-credentials（推送本机已持有的那个）"
      CRED_NEED="${KEY} 需要 --from-credentials（它**没有** --generate 这条路：值由外部签发，本机造不出来）"
      CRED_NEW_HINT="值从哪儿来：人去 @BotFather 拿（/mybots → API Token 看现有的，/revoke 换一个新的），再自己写进 ${CRED_FILE} 的 ${POLICY_CRED_KEY} 键（0600，顶格一行 ${POLICY_CRED_KEY}: <值>）。**本脚本造不出这个值**，所以它没有 --generate。"
    else
      CRED_WAYS="--generate（本机生成一个新的）或 --from-credentials（推送本机已持有的那个）"
      CRED_NEED="${KEY} 需要 --generate 或 --from-credentials 二选一"
      CRED_NEW_HINT="要新建一个：bash scripts/ops/env_set.sh --key ${KEY} --generate"
    fi
    [[ "${VALUE_GIVEN}" -eq 0 ]] || die \
      "${KEY} 不接受 --value" \
      "它是凭据。--value 会把它写进本脚本的 argv，而生产是合租机器、/proc/*/cmdline 世界可读（docs/RISKS.md §8.2），本机的 ps 输出同理。" \
      "改用 ${CRED_WAYS}。"
    if [[ "${POLICY_CRED_ORIGIN}" == "external-issuer" && "${TOKEN_SOURCE_MODE}" == "generate" ]]; then
      die "${KEY} 不能 --generate" \
        "它的值由**外部**签发（bot token 来自 Telegram 的 @BotFather），本机的 CSPRNG 造不出来：生成一个 256 bit 随机值推到生产，只会让 core 拿着一个 Telegram 不认的 token。" \
        "而且那种失败**不响**——长轮询线程不会退出，它按 2s→120s 退避无限重试（core/telegram.py:810-831），而 /api/v1/system/telegram 的 polling 只看线程活没活（core/telegram.py:978-988），照样报 true。也就是说 --generate 出来的假 token 会静悄悄地把确认卡通道弄死。" \
        "正确做法：${CRED_NEW_HINT}" \
        "写好之后：bash scripts/ops/env_set.sh --key ${KEY} --from-credentials" \
        "值绝不要念给编排方听、也绝不要经 --value 传（红线 R5、docs/RISKS.md §8.2）。"
    fi
    [[ -n "${TOKEN_SOURCE_MODE}" ]] || die "${CRED_NEED}"
    ;;
esac

# --------------------------------------------------------------- R1 红线闸门
#
# 【两道闸门，不是一道——先读这段分工】
#
#   （下面这段讲的是 SW_USE_FAKE_PUBLISHERS 那一对。本轮新增的 signing_secret 闸门是**只有事前
#   没有事后**的另一类：restart.sh 那道 R1 闸门不看待确认条数，兜不住它，理由写在远端那个分支上方。）
#   事前预防（写 `.env` 之前）  远端正文里那段 `case "${active_gate}"` 的 real_publish 分支：先探
#     `/api/v1/system/telegram`，`enabled` / `ready` / `polling` 三者不全为真就**拒绝写入**，
#     `.env` 一个字节都不动、连备份都不建。拦住的是**可预见**的那一半：动手时通道就已经死了。
#   事后检测（重建之后）        调 `scripts/ops/restart.sh`，走它那道已有的 R1 闸门。
#     兜住的是**写入到生效之间**发生变化的那一半——探针到容器重建之间隔着几秒到几十秒，
#     Telegram 那边随时可能掉线。
#
# 事前拦不住后半段，事后拦不住前半段，所以**两道都在，谁也不替换谁**。
#
# 【这段历史留着，免得有人以为事前预防是"顺手就能做"的】上一批刻意没做事前预防，理由不是
# 没想到，而是代价与当时的目标不匹配：探 /api/v1/* 需要一份带鉴权的 sw_probe，而
# scripts/ops/ 下当时有**恰好四份**逐字相同的内联拷贝，tests/ops/test_update.sh 有一条源码级
# 断言同时钉死"四份逐字相同"与"持有者恰好四个"。在这里内联第五份，要么让那条断言变红，
# 要么把 4 改成 5——而正确的动作从来是把 sw_probe 收成 ssh stdin 流的共享片段。本轮先做了
# 那个重构（`sw_ops_emit_sw_probe_definition`，唯一定义处在 scripts/ops/ui_token.sh），
# 这个理由随之不成立，事前预防才补上。docs/RISKS.md §12.3 记着这条因果。
#
# 【事后那道闸门的局限仍然如实写着】它坐在 `up -d --force-recreate` 之后，触发时带着新值的
# core 已经在跑了（docs/RISKS.md 第 12 条同一条局限）。事前预防把**可预见**的那一类挡在了
# 写入之前，但挡不住写入之后才发生的变化——不掩饰这一点。
#
# 本脚本能做、也做了的三件事，把这条局限压到最小：
#   ① 任何会触发事前闸门的方向都**禁用 --write-only**（本轮从"只禁 SW_USE_FAKE_PUBLISHERS
#      =false"推广开，理由对四道闸门是同一条）。理由不是洁癖："`.env` 已经是新值但运行中
#      的 core 还是旧值"是一个**上了膛没有击发**的状态——此后任何人一次寻常的
#      `docker compose up -d`（第 12 条明写它绕得过所有闸门）都会静默把新值带上来，
#      而那一刻没有任何闸门在场。强制在**本次调用、有人盯着**的时候把它走完，
#      闸门就一定会跑。
#   ② restart.sh 的闸门未通过时，本脚本以失败收尾（fail-closed），绝不打印成功行，并给出
#      一条可直接粘贴的**反向命令**与 .env 备份路径。
#   ③ 绝不自动回退。这与 update.sh 的 rollback_hint 契约一致：恢复命令永远只是文本。
#      自动回退意味着在一次已经出错的流程里再自动改一次生产 .env、再重建一次容器，
#      而"闸门为什么红"这件事还没人看过。
# 判定用 ${VALUE}（只有 value_source=argv 的键才有它）而**不是** ${SW_OPS_ENV_VALUE}：
# 后者在凭据类键那条路上装着凭据，而 `[[ ... ]]` 会被 xtrace 连同展开后的两侧一起
# 打出来。这一条在本轮之后更要紧了：signing_secret 是**第一道长在凭据类键上的闸门**，
# 而它恰恰**不看方向**（下面那个分支里没有 ${VALUE} 的比较），所以凭据值一次都不会被比较。
# ------------------------------------------------- 这次调用**实际**会触发哪道闸门
#
# POLICY_GATE 是"这个键有哪道闸门"，ACTIVE_GATE 是"这次这个方向要不要跑它"。两者必须分开：
# 有的闸门是**单向**的，而方向性恰恰是安全设计本身——每道各自写明它开在哪个方向：
#   real_publish      只在 --value false（打开真发布）时开。**反方向永远不受约束**：
#                     出事时人必须能退回安全状态，绝不能被一道闸门锁死在危险状态里。
#   confirm_carrier   只在 --value false（拆掉 Telegram 载体）时开。把载体装回去不设卡。
#   wechat_certified  只在 --value true（打开自动发布）时开。关回草稿箱不设卡。
#   wechat_claim      只在 --value true（把认证状态记成真）时开。**事前闸门里唯一从不拒绝的
#                     一道**——它存在是为了两件闸门以外的事：把"这次变更会不会让平台级自动
#                     发布当场生效"讲清楚，以及顺带禁掉 --write-only（理由见下面那段）。
#                     为什么不拒绝，写在远端那个分支上方，不在这里重复。
#   llm_backend_creds **两个方向都开**，因为它问的不是"方向危不危险"而是"目标后端能不能起
#                     得来"。切到哪一边都要求那一边的凭据在 .env 里就位——这条闸门的全部
#                     意义就是不让"回退"把 core 换到一个起不来的后端上（docs/RISKS.md 第 14 条
#                     点名它是代价最高的那个键，难点正在这里，不在校验值本身）。
#   signing_secret    **没有方向可言**，所以凭据类键上一律无条件点亮。这不是偷懒：
#                     凭据类键根本没有 ${VALUE}（值不进 argv），"往哪个方向改"这个问题在
#                     它们身上不成立；能问的只有"这次写入会不会换掉生效的签名密钥"，
#                     而那要读生产 .env 才知道——那是远端那个分支的事，不是这里的事。
#                     本地能确定的只有一件：这几个键都**可能**换掉签名密钥，所以闸门必须跑。
ACTIVE_GATE="none"
if [[ "${POLICY_GATE}" == "real_publish" && "${VALUE}" == "false" ]]; then
  ACTIVE_GATE="real_publish"
elif [[ "${POLICY_GATE}" == "confirm_carrier" && "${VALUE}" == "false" ]]; then
  ACTIVE_GATE="confirm_carrier"
elif [[ "${POLICY_GATE}" == "wechat_certified" && "${VALUE}" == "true" ]]; then
  ACTIVE_GATE="wechat_certified"
elif [[ "${POLICY_GATE}" == "wechat_claim" && "${VALUE}" == "true" ]]; then
  ACTIVE_GATE="wechat_claim"
elif [[ "${POLICY_GATE}" == "llm_backend_creds" ]]; then
  ACTIVE_GATE="llm_backend_creds"
elif [[ "${POLICY_GATE}" == "signing_secret" ]]; then
  ACTIVE_GATE="signing_secret"
fi

# override 只对 signing_secret 有意义，别的方向给了就是拿错了旗子——静默忽略会让人以为
# 自己已经绕过了某道闸门。拒绝它，并说清它管的是哪一件事。
if [[ "${ACCEPT_BREAKING}" -eq 1 && "${ACTIVE_GATE}" != "signing_secret" ]]; then
  die "--accept-breaking-pending-confirm-cards 对 --key ${KEY} 没有意义" \
    "它只接受一件事：换 Telegram 确认卡的 HMAC 签名密钥时，明知已推出去还没人点的卡会因此失效仍然继续。" \
    "有这道闸门的是这几个凭据类键（从白名单派生，不是手写的第二份名单）：$(sw_env_keys_where gate signing_secret)。" \
    "本次要改的 ${KEY} 与签名密钥无关，去掉这个旗子重跑。"
fi

GATE_REAL_PUBLISH=0
GATE_SIGNING_SECRET=0
[[ "${ACTIVE_GATE}" != "real_publish" ]]   || GATE_REAL_PUBLISH=1
[[ "${ACTIVE_GATE}" != "signing_secret" ]] || GATE_SIGNING_SECRET=1

# 【这一步不能省：这两道闸门要打 /api/v1，而 core 启用 SW_UI_TOKEN 之后不带头的探针一律 401】
# 少了它，闸门在一台**已经启用鉴权**的生产上永远只会 fail-closed 在 401 上，而它给出的处置
# 指引（"export SW_OPS_UI_TOKEN=…"）根本不会生效——本机导出了也送不到远端，因为远端脚本流里
# 压根没有那一行 export。那等于把人锁死，而且锁死的方式还带着一句**错误**的指引。
# 取用放在 SSH 之前：字符集不合法要在本机就报错退出，绝不带着一个会破坏 curl 配置语法的
# 值去连生产。其余那几道闸门只读远端 `.env`（宿主机上的一个文件），不打 /api/v1、不进容器，
# 给它们平白加一道会 die 的校验没有收益。
#
# ！！【顺序：这一段必须排在"取凭据值"那一整节**之前**，别把它挪回去】
# `--key SW_UI_TOKEN --generate` 会把**新** token 写进 ~/.dsh-sw/.credentials.yaml。
# 而探针要用的是生产**现在**认的那个 token，也就是**旧**的那个。先生成后取用的话，
# sw_ops_load_ui_token 读到的是刚落盘的新值，探针必然 401，闸门必然 fail-closed——
# 一条本来该放行的路径会 100% 被自己刚写下的值挡死。本地先取、后生成，这个顺序是承重的。
if [[ "${GATE_REAL_PUBLISH}" -eq 1 || "${GATE_SIGNING_SECRET}" -eq 1 ]]; then
  sw_ops_load_ui_token
  sw_ops_note_ui_token
  # 这句只在 `--key SW_UI_TOKEN --generate` 那一条路上才成立、也才有必要说：那时上面取到的是
  # **旧**值（探针要用生产现在认的那个），而马上要写进去的是一个刚生成的新值，两者不是一回事。
  # `--from-credentials` 上它们本来就是同一个值，多说一句反而是错的。
  # 另外要求 SW_OPS_UI_TOKEN_SOURCE 非空：没取到时 sw_ops_note_ui_token 一个字都不打
  # （未配置路径的输出必须与改造前逐字一致），这里跟着不打，否则会凭空多出一句指着不存在的
  # 上一行说话的注解。
  if [[ -n "${SW_OPS_UI_TOKEN_SOURCE}" && "${GATE_SIGNING_SECRET}" -eq 1 \
     && "${KEY}" == "SW_UI_TOKEN" && "${TOKEN_SOURCE_MODE}" == "generate" ]]; then
    note "上面这一个是**生产现在认的**那个 token（闸门要用它去读待人点的确认卡条数），不是本次 --generate 出来的新值"
  fi
fi

# 【--write-only 的禁用范围本轮从一个键推广到了"任何会触发闸门的方向"】
# 原来的理由只讲了 SW_USE_FAKE_PUBLISHERS，但它其实对每一道闸门都成立，而且是同一条：
# 闸门要么在**写入前**跑（事前三道），要么靠 restart.sh 在**生效后**跑（事后一道），
# 而 --write-only 把"生效"整段跳过了。跳过之后留下的是一个"上了膛没击发"的 .env：
# 此后任何人一次寻常的 docker compose up -d（docs/RISKS.md 第 12 条明写它绕得过所有闸门）
# 都会把新值静默带上来，而那一刻没有任何闸门在场，也没有任何人在看。
# 强制在**本次调用、有人盯着**的时候把它走完，闸门就一定会跑。
# 反过来说：ACTIVE_GATE=none 的方向（关真发布、装回 Telegram 载体、改预算、停生成）
# **照旧允许 --write-only**——那些方向本来就没有闸门要跑，禁用它只是添乱。
#
# 【凭据类键都落在这条禁令里，如实记一笔】上一批之前 `--key SW_UI_TOKEN --from-credentials
# --write-only` 是允许的（那时它的 POLICY_GATE 是 none）。现在不允许了，而且理由就是上面
# 那条、一字不用改：把一个新 token 写进 .env 却不重建容器，留下的正是"上了膛没击发"——
# 下一个撞上 `docker compose up -d` 的人会在无人值守的情况下同时启用鉴权、换掉签名密钥。
# 这不是顺手收紧：本条禁令的措辞本来就是"任何会触发事前闸门的方向"，凭据类键都有闸门。
#
# 报错文案里那句"用什么命令"必须按取值方式分支：凭据类键根本没有 --value（值不进 argv），
# 照抄 `--value ${VALUE}` 会打出 `--key SW_UI_TOKEN --value `，一个空的、不存在的用法。
if [[ "${POLICY_VALUE_SOURCE}" == "credentials" ]]; then
  CHANGE_DESC="--key ${KEY} --${TOKEN_SOURCE_MODE}"
else
  CHANGE_DESC="--key ${KEY} --value ${VALUE}"
fi
if [[ "${ACTIVE_GATE}" != "none" && "${WRITE_ONLY}" -eq 1 ]]; then
  case "${ACTIVE_GATE}" in
    real_publish)      gate_why="它会把真发布打开，而 R1 闸门（事前探确认通道 + 事后 restart.sh）就长在写入与生效这两步上" ;;
    confirm_carrier)   gate_why="它会拆掉确认卡的推送载体，而拦住这件事的闸门跑在写入之前" ;;
    wechat_certified)  gate_why="它会打开公众号平台级自动发布，而校验账号认证状态的闸门跑在写入之前" ;;
    wechat_claim)      gate_why="WECHAT_AUTO_PUBLISH 已经是 true 时，这一个写入就让平台级自动发布成立——那正是最不该留成「上了膛没击发」的一格" ;;
    llm_backend_creds) gate_why="它会切换 LLM 后端，而校验目标后端凭据的闸门跑在写入之前" ;;
    signing_secret)    gate_why="它可能换掉 Telegram 确认卡的 HMAC 签名密钥，而读待人点确认卡条数的闸门跑在写入之前；更要紧的是这一格没有事后闸门兜底——restart.sh 的 R1 闸门不看待确认条数" ;;
    *)                 gate_why="这个方向有事前闸门" ;;
  esac
  # 【外部签发的凭据上，"上了膛没击发"这个比喻其实说轻了——如实补一句】
  # 判据是策略表第五格（POLICY_CRED_ORIGIN），不是键名：签发方一发新值、旧值当场作废，
  # 于是"`.env` 已是新值、运行中的 core 还是旧值"不是一个等着被谁触发的隐患，而是**当场**
  # 就哑火——core 拿着一个已经作废的凭据，而 polling 那一格照样报 true（它只看线程活没活）。
  if [[ "${POLICY_CRED_ORIGIN}" == "external-issuer" ]]; then
    gate_why="${gate_why}。而且它的值由**外部**签发：签发方一发新值、旧值当场作废，所以这个键上的「.env 已改、容器没重建」不是「上了膛没击发」，是**当场哑火**——core 手里那个旧凭据从这一刻起就不管用了，卡一张都推不出去，而 /api/v1/system/telegram 的 polling 只看线程活没活，照样报 true"
  fi
  die "--write-only 不能与 ${CHANGE_DESC} 一起用" \
    "${gate_why}。" \
    "--write-only 会留下一个上了膛没击发的状态：.env 已经是新值，运行中的 core 还是旧值。" \
    "此后任何人一次寻常的 docker compose up -d（docs/RISKS.md 第 12 条明写它绕得过所有闸门）都会静默把新值带上来，而那一刻没有任何闸门在场。" \
    "去掉 --write-only 重跑：本脚本会重建容器让它生效，该跑的闸门一道都不会漏。"
fi

# ------------------------------------------------------- 凭据类键的取值路径
#
# 【这条设计难点正面写在这里】启用 token 之后，所有经 IAP 隧道访问 /api/v1/* 的人都要带
# `Authorization: Bearer`，否则 401——包括工作台前端与 scripts/workbench_mcp.py 对话台。
# 而红线 R5 规定凭据不进对话，所以**没有任何人可以把生成的 token 念给用户听**。
# 于是 token 的流向被设计成"人自己去读文件"，全程零回显：
#
#   --generate         本机 CSPRNG 生成 → 写进 ~/.dsh-sw/.credentials.yaml（0600）
#                      → 经 ssh 的 stdin 流推到生产 .env。**先本地、后远端**，见下。
#   --from-credentials 本机已有的值（环境变量 SW_OPS_UI_TOKEN 优先，其次凭据文件）
#                      → 同一条通路推到生产 .env。
#   人要用的时候        自己 `cat ~/.dsh-sw/.credentials.yaml`。脚本、编排方、对话里都不出现它。
#
# 【为什么必须先写本地再推远端】这个顺序是不对称的，反过来会出事：
#   本地成功、远端失败 → 本机有一个生产没有的值。ops 脚本会带一个多余的 Authorization 头，
#     而 core/api/common.py::require_token 在未启用时直接 return，多余的头被忽略——无害，
#     重跑 --from-credentials 即可收敛。
#   远端成功、本地失败 → 生产要求一个**没有人持有**的 token，工作台前端、对话台、以及所有
#     source 了 ui_token.sh 的脚本（名单以 `grep -l 'ui_token\.sh' scripts/ops/*.sh scripts/*.sh`
#     为准）同时被 401 挡在门外，而唯一能恢复它的路径（本脚本）也要经 SSH——那是自锁。
#   所以：本地先落盘，远端后写。
#
# 【凭据文件已存在 / 已有 sw_ui_token 键怎么办】
#   文件不存在      → 创建，目录 0700、文件 0600。
#   文件存在无该键  → 只**追加**一行，其余内容逐字保留（同目录临时文件 + mv，见 ui_token.sh）。
#   文件存在有该键  → --generate **拒绝执行**。覆盖一个还在用的凭据是不可逆的：旧值一旦被
#     盖掉就再也拿不回来，而生产上可能正用着它。要换新的，请人自己确认后删掉那一行再来，
#     或者直接用 --from-credentials 把现有的值推上去。这里不替人做这个决定。
#
# 【两边不一致怎么办】**不比对、不打印，靠 401 判**。比对需要把两个值放在一起，而任何一次
#   回显都踩 R5。正确的收敛动作是 --from-credentials（以本机为准推一次），然后跑
#   `bash scripts/ops/verify.sh`：探针 200 就是一致，401 就是不一致。401 这一格，带探针的
#   那几个 ops 脚本都已经有指向根因的提示（docs/RISKS.md §8.4）。
#
# 【本节从"只服务 SW_UI_TOKEN"变成"服务每一个凭据类键"，两处仍然按键分支，别抹平】
#   ① 凭据文件里的键名从 sw_env_policy 的第四格取（${POLICY_CRED_KEY}），不再硬编码。
#      硬编码的后果是无声的：第二个键会去读 / 写 UI token 那一行，而"判存在"会对着别人点头，
#      于是"只追加不覆盖"这条保证当场失效。
#   ② `--from-credentials` 的取用**只有 SW_UI_TOKEN 会先看环境变量**。那一层是它专有的：
#      运维侧本来就要持有 UI token 去打探针，所以给了一个"这一次调用换个值"的显式入口。
#      别的凭据类键没有、也不该有对应的环境变量——运维侧不需要持有签名密钥，它只是一个要被
#      推到生产 .env 去的值；凭空发明 SW_OPS_TELEGRAM_SIGNING_SECRET 只会多一条"值可能从哪
#      儿来"的路径，而每多一条，"两边为什么不一致"就多一种查不清的可能。
#      同一条取舍在 scripts/ops/ui_token.sh 的 _sw_ops_load_ui_token_impl 上方也写着。
if [[ "${POLICY_VALUE_SOURCE}" == "credentials" ]]; then
  if [[ "${TOKEN_SOURCE_MODE}" == "generate" ]]; then
    if sw_ops_credentials_has_key "${CRED_FILE}" "${POLICY_CRED_KEY}"; then
      die "${CRED_FILE} 里已经有 ${POLICY_CRED_KEY} 键，--generate 拒绝覆盖它" \
        "覆盖一个还在用的凭据是不可逆的：旧值被盖掉就再也拿不回来，而生产上可能正用着它。" \
        "要把本机现有的那个值推到生产：bash scripts/ops/env_set.sh --key ${KEY} --from-credentials" \
        "确实要换一个新的：请人自己打开 ${CRED_FILE} 删掉那一行（确认没有别处在用），再重跑 --generate。" \
        "此处不回显任何值——判断「还在不在用」请跑 bash scripts/ops/verify.sh 看探针是 200 还是 401。"
    fi
    sw_ops_generate_credential || die \
      "本机拿不到密码学安全的随机源，拒绝生成 ${KEY} 的值" \
      "依次试过 openssl rand -hex 32 与 /dev/urandom + od，两条都不可用。" \
      "绝不退化到 \$RANDOM：那是线性同余，可预测；用它当鉴权 token 等于没有鉴权，用它当 HMAC 签名密钥等于确认卡可以被伪造。" \
      "处置：装上 openssl，或自己用别的机器生成一个落在白名单字符集内的值，写进 ${CRED_FILE} 的 ${POLICY_CRED_KEY} 键后改用 --from-credentials。"
    sw_ops_write_credentials_key "${CRED_FILE}" "${POLICY_CRED_KEY}" || die \
      "写入 ${CRED_FILE} 失败，生产 .env 一个字节都没动" \
      "这个顺序是刻意的：先本地落盘、后推远端。反过来会让生产要求一个没有人持有的值，那是自锁。" \
      "检查该文件与 $(dirname "${CRED_FILE}") 的权限与磁盘空间后重跑。"
    note "已在本机生成新值并写入 ${CRED_FILE} 的 ${POLICY_CRED_KEY} 键（0600）；值不打印、不进 argv"
    note "人要用它的时候自己读那个文件——编排方不会、也不该把它念出来（红线 R5）"
  elif [[ "${KEY}" == "SW_UI_TOKEN" ]]; then
    sw_ops_load_ui_token
    sw_ops_adopt_loaded_ui_token || die \
      "本机没有可用的工作台 API token，--from-credentials 无从推起" \
      "取用顺序：环境变量 SW_OPS_UI_TOKEN（已导出就采信，哪怕是空串）> ${CRED_FILE} 的顶格 ${POLICY_CRED_KEY} 键。" \
      "要新建一个：bash scripts/ops/env_set.sh --key SW_UI_TOKEN --generate" \
      "详见 scripts/ops/README.md「工作台 API token」一节。"
    note "已加载本机 token（来源：${SW_OPS_UI_TOKEN_SOURCE}）；值不打印、不进 argv"
  else
    sw_ops_adopt_credentials_key "${CRED_FILE}" "${POLICY_CRED_KEY}" || die \
      "${CRED_FILE} 里没有可用的 ${POLICY_CRED_KEY} 键，--from-credentials 无从推起" \
      "取用只有这一条路：${CRED_FILE}（0600）里顶格的 ${POLICY_CRED_KEY} 键。这个键**没有**对应的环境变量入口，理由写在本节开头。" \
      "${CRED_NEW_HINT}" \
      "详见 scripts/ops/README.md「工作台 API token」一节（凭据文件的格式与权限要求是同一套）。"
    note "已从 ${CRED_FILE} 的 ${POLICY_CRED_KEY} 键加载本机值；值不打印、不进 argv"
  fi
fi

# 纵深防御：不管值从哪条路来，写出去之前都要过一遍与远端相同的形状校验。
# 报错文案按 display 策略分支——凭据一律不回显。
# 走 sw_ops_env_value_matches 而不是在这里直接写 `[[ ... =~ ... ]]`：后者会被 xtrace
# 连同展开后的值一起打出来（理由与实测见 scripts/ops/ui_token.sh 里那个函数的注释）。
if ! sw_ops_env_value_matches "${VALUE_RE}"; then
  if [[ "${POLICY_DISPLAY}" == "secret" ]]; then
    # 【这条文案本轮从"字符集"改成"形状"，是被逼的，不是润色】上一版逐字写着"含有不被允许
    # 的字符"，那句话在两个凭据类键上是对的（它们的 VALUE_RE **就是**字符集白名单本身），
    # 但 TELEGRAM_BOT_TOKEN 有自己的形状：一个 64 位十六进制串字符全合法、形状完全不对，
    # 照旧文案会得到一句"含有不被允许的字符"+一份它全部满足的允许集——一次回答得很确定的错答。
    die "${KEY} 的值过不了形状校验（此处不回显值：它是凭据，红线 R5）" \
      "这个键接受的形状：${VALUE_HELP}" \
      "正则（本机与远端用的是同一条）：${VALUE_RE}" \
      "凭据类键还共用一层**传输通路**的字符集白名单：${SW_OPS_UI_TOKEN_ALLOWED_TEXT}；其余字符一律拒绝。理由分层写在 scripts/ops/ui_token.sh 顶部与 scripts/ops/README.md「字符集限制」。" \
      "换一个合形状的值再来。本脚本不回显它现在长什么样——要看，人自己去读值的来源（环境变量，或 ${CRED_FILE}）。"
  else
    die "${KEY} 的值不合法：${SW_OPS_ENV_VALUE}"
  fi
fi

# ---------------------------------------------------------------- 远端变更脚本
#
# 【它现在打 /api/v1，也进容器了——这一条变过，先读】上一版的远端正文刻意不碰这两样：
# `.env` 是宿主机上的文件，编辑它只要 bash 内建与 cp/mv，少一层依赖就少一条会出错的路径。
# 事前预防闸门（docs/RISKS.md §12.3）把这一点改了：**只有 `--key SW_USE_FAKE_PUBLISHERS
# --value false` 这一个方向**会在写 `.env` 之前探一次 `/api/v1/system/telegram`，并用容器
# 内 python3 解析它。另一道会打 /api/v1 的是 signing_secret，端点不同（`/api/v1/dashboard`，
# 读待人点的确认卡条数）。其余那几道闸门**只读已经载入内存的 .env**，不 curl、不进容器、
# 不需要 token——哪几道、以及为什么，都写在下面那段闸门总注释里（那里逐道标着它探什么）。
# 直接后果：`docker compose exec -T` 吞掉脚本正文那个历史缺陷在本文件里**从此有了载体**，
# 所以那道 `{ ... } </dev/null` 在这里也从"照抄同款"变成了真正承重的东西；
# tests/ops/test_env_set.sh 相应补了一条把结构删掉、让缺陷重现的反例。
#
# 【本文件里有好几条这样的 grep，它们指的不是几个不同的脚本群】走 ssh 的那批 ops 脚本
# 共享好几项手法：argv 用 printf 的 %q 转义、发射 sw_probe、发射 token 前言、`{ ... }`
# 组重定向。每项各给一条自己的命令，是因为它们**可以**分头漂移，不是因为名单本来就不同。
# 两个读法陷阱：① 发射类的那两条 grep 会把 ui_token.sh 一起列出来——那是函数的定义处，
# 不是使用方；② 今天唯一名单真的不同的是下面那条 255→254 规范化，它没有 status.sh。
#
# 远端 stdin 流由三段拼成，与其余发射 sw_probe 的脚本同款（名单以
# `grep -l 'sw_ops_emit_sw_probe_definition' scripts/ops/*.sh` 为准，结果里的 ui_token.sh
# 是定义处、不算使用方）：
#   ① sw_ops_emit_env_value_prologue —— 一行 `export SW_ENV_SET_VALUE=<%q 转义的值>`，
#      落在外层 `{` 的**外面**（见 ui_token.sh 里那段"只许放内建命令"的警告）；
#   ② 这里的两段引号 heredoc；
#   ③ 夹在中间的两个发射函数：
#      · sw_ops_emit_sw_probe_definition —— 远端 sw_probe 的唯一定义处。env_set.sh 就是那个
#        "第五个调用方"：从前不做事前预防的理由正是"要内联第五份 sw_probe"，sw_probe 收成
#        单一真相源之后，这个理由不成立了。
#      · sw_ops_emit_awaiting_confirm_definition —— 远端"待人点的确认卡条数"读数的唯一定义处
#        （scripts/ops/verify.sh 是另一个使用方）。它调用 sw_probe，所以必须排在后面。
#        为什么不在这里再写一份：那正是上一条刚花一整批消灭掉的东西，而这一对的分叉后果更糟——
#        取证与闸门读的会是两个口径，"verify 说 0 条、env_set 说有卡"没人查得清。
env_set_remote_script() {
  cat <<'REMOTE_HEAD'
{
set -uo pipefail
# 远端脚本自身若因故退出 255，会与 ssh 的"传输中断"混淆。这里对齐同一批远端脚本的做法：
# 先改写成 254 再往外传。名单以 `grep -l 'exit 254' scripts/ops/*.sh` 为准——它比上面
# 那几条 grep 少一个 status.sh，那是 status.sh 刻意的选择（它把 ssh 与远端的退出码原样
# 透出，见 status.sh 里"不加解释也不改码"那一行），不是漏掉。
remote_status=0
bash -s -- "$@" <<'REMOTE_ENV_SET' || remote_status=$?
{
set -euo pipefail
# ！！【stdin 的结构性保证——改本段任何一行之前先读完这一段】
# 与其余那几个远端脚本同一套口径：`{ ... }` 是一条复合命令，bash 必须整条解析完才开始执行，
# 正文因此在第一条命令跑起来之前就离开了输入流；`} </dev/null` 让组内所有命令与子进程的
# fd 0 都是 /dev/null。逐条 `</dev/null` 已降级为纵深防御，但一条都不删。
# 本段读 `.env` 用的是显式的 `<"${env_file}"` 重定向，不碰 fd 0 的默认来源。
#
# 【本文件的这道保证从"照抄同款"变成了真正承重】上一版这里没有任何会消费 stdin 的边界命令，
# 所以这段话在本文件里只是纪律。本轮的事前预防闸门带进来一条
# `printf ... | docker compose exec -T core python3 -c ...`——`-T` 只关 TTY、**仍然转发
# stdin**，它一旦少了显式来源，就会把闸门后面的备份、写入、重建整段吞掉，而脚本以 0 收尾、
# 外层照样打印"✓ 生产 .env 已变更、已生效"。那条命令自带前置管道（显式来源），外面还有这层
# 组重定向兜底，两层各守一半。tests/ops/test_env_set.sh 有一正一反两条用例钉住它。

# ---- 工作台 API 探针（带鉴权；token 一个字符都不进 argv）------------------------
# 【定义不在本文件里】紧跟这段注释的 sw_probe 定义由本机的
# sw_ops_emit_sw_probe_definition 发射进这条脚本流，**唯一定义处是 scripts/ops/ui_token.sh**。
# 它只被下面那道事前预防闸门用到；`--show`、`SW_UI_TOKEN`、`--value true` 这几条路径
# 一次都不会调用它（定义摆在那儿不产生任何行为）。
# 发射进来的字节落在**内层**那对花括号里面，所以上面那道结构性保证盖得到它。
REMOTE_HEAD
  sw_ops_emit_sw_probe_definition
  sw_ops_emit_awaiting_confirm_definition
  cat <<'REMOTE_TAIL'
key="$1"; value_re="$2"; policy="$3"; stamp="$4"
duplicate_status="$5"; bad_key_status="$6"; bad_value_status="$7"
backup_status="$8"; write_status="$9"; missing_status="${10}"; recreate_status="${11}"
write_only="${12}"
# 【本轮从"一个布尔开关"变成"一个闸门名"】上一版这里是 precheck_gate=0/1，因为当时只有
# 一道事前闸门。后来闸门一道一道加上来，各问不同的问题、拒绝时各有各的退出码（唯一的例外
# 是 wechat_claim：它从不拒绝，所以刻意没有码），用一个字符串分派比给每道闸门各来一个布尔
# 清楚得多——也让远端不必知道"哪个键配哪道闸门"，那是本地那张策略表的事。
# 合法取值就是 none 加上下面那个 case 里出现的每一个闸门名（它们与本地 sw_env_policy 里的
# POLICY_GATE 一一对应），case 有 `*)` 兜底，收到别的一律拒绝执行。
active_gate="${13}"
precheck_gate_status="${14}"; precheck_probe_status="${15}"
carrier_gate_status="${16}"; carrier_probe_status="${17}"
backend_creds_status="${18}"; wechat_cert_status="${19}"
signing_gate_status="${20}"; signing_probe_status="${21}"
# 0/1。1 表示调用方给了 --accept-breaking-pending-confirm-cards，只对 signing_secret 有意义
# （本地已经拒绝过把它用在别的键上）。
accept_breaking="${22}"
# 本键在 core/telegram.py:151-154 那条签名密钥回落链上**排在它前面**的那几级：按级序
# （第 1 级在前）、空格分隔的 .env 键名。`none` = 本键自己就是第 1 级、上面没有任何一级；
# `-` = 它压根不在那条链上（那时 active_gate 也不会是 signing_secret）。
# 【为什么这份"谁在谁前面"由本地给、远端不自己知道】与 active_gate 同一条理由：远端不该
# 认识"哪个键配哪道闸门 / 哪个键在第几级"，那是本地那张策略表的事。将来回落链多一级，
# 改表即可，这段闸门代码一个字不用动。
signing_above="${23}"
# 回落链的第 1 级键名（根治动作要指向它）。signing_above 为 `none` / `-` 时这一格没有意义，
# 也只在 signing_above 是真名单时才会被用到。
signing_root_key="${signing_above%% *}"

value="${SW_ENV_SET_VALUE:-}"
env_file="${HOME}/social_workflow/.env"
backup_dir="${HOME}/sw-env-backups"

# 新建的文件一律 0600 / 目录 0700。umask 要在**创建任何文件之前**设，事后 chmod 会留下
# 一个虽然短暂但真实的可读窗口——`.env` 里有 LLM key 与 bot token，而这是台合租机器。
umask 077

# ---- 纵深防御：键名与值在远端再校验一次 -------------------------------------
# 本地已经挡过一次。这里再挡是因为两侧的失效方式不同：本地那道靠白名单 case，远端这道
# 只认传进来的形状。任何一侧被绕过或改错，另一侧还在。
case "${key}" in
  DSH_*|XDG_*|DYLD_*|BASH_FUNC_*)
    printf '✗ 键名 %s 命中红线 R7 的禁止前缀（DSH_ / XDG_ / DYLD_ / BASH_FUNC_）：dsh 会拒绝启动且无开关。\n' "${key}" >&2
    exit "${bad_key_status}"
    ;;
esac
if ! [[ "${key}" =~ ^[A-Z][A-Z0-9_]*$ ]]; then
  printf '✗ 键名 %s 形状不合法：只接受大写字母开头、由大写字母/数字/下划线组成的名字。\n' "${key}" >&2
  exit "${bad_key_status}"
fi
if [[ -z "${value}" ]]; then
  # 空值不是"清空这个键"，而是"值没送到"（stdin 前言没跑 / 变量被覆盖）。
  # 真要关掉鉴权或删掉一个键，请人自己评估后手工处理——那是一次安全降级，不该由本脚本代劳。
  printf '✗ 没有收到要写入的值。本脚本不接受空值：清空一个键是安全降级，不在它的职责里。\n' >&2
  exit "${bad_value_status}"
fi
if ! [[ "${value}" =~ ${value_re} ]]; then
  if [[ "${policy}" == "plain" ]]; then
    printf '✗ 值 %s 过不了远端再校验（形状：%s）。\n' "${value}" "${value_re}" >&2
  else
    printf '✗ 值过不了远端再校验（此处不回显凭据值；形状：%s）。\n' "${value_re}" >&2
  fi
  exit "${bad_value_status}"
fi

if [[ ! -f "${env_file}" ]]; then
  printf '✗ 远端 %s 不存在。本脚本只改已有的 .env，不凭空造一个——生产 .env 里还有 LLM key 与 Telegram bot token，凭空造出来的那份会让 core 起不来。\n' "${env_file}" >&2
  exit "${missing_status}"
fi

# ---- 读入整份 .env -----------------------------------------------------------
# `|| [[ -n "${line}" ]]` 那一半专治"文件末尾没有换行"：没有它，最后一行会被 read 丢掉，
# 而丢掉之后写回去就等于**吞掉一行真实配置**。有它之后最后一行照样进数组，下面写回时
# 每一行都补上换行——所以新键也永远不会被粘到上一行的尾巴后面。
lines=()
line_count=0
while IFS= read -r line || [[ -n "${line}" ]]; do
  lines+=("${line}")
  line_count=$((line_count + 1))
done <"${env_file}"
# 自己数行数，不用数组长度展开：`lines=()` 之后的空数组在 bash 4.4 之前的 `set -u` 下行为
# 不统一（有的版本把它当未设置）。生产是 bash 5.x，但这条脚本会写 .env，不值得为省一个
# 计数器去赌远端 bash 的版本。

# ---- 在已经读进 lines[] 的 .env 里查**别的**键（三道新闸门要用）--------------------
# 只查内存里那份快照，不再读一次文件：闸门看到的必须与下面即将写回去的是同一份内容，
# 中间再读一次就给了"两次读之间文件变了"一个立足点。
# dotenv 语义是**后一条覆盖前一条**（python-dotenv / pydantic-settings 都如此，本机实测），
# 所以取最后一条。命中数一并给出去，让调用方自己决定"出现多次"算不算"不知道"。
lookup_hits=0
lookup_value=""
env_lookup() {
  lookup_hits=0
  lookup_value=""
  local want_prefix="$1=" j=0
  while [[ "${j}" -lt "${line_count}" ]]; do
    case "${lines[${j}]}" in
      "${want_prefix}"*)
        lookup_hits=$((lookup_hits + 1))
        lookup_value="${lines[${j}]}"
        lookup_value="${lookup_value:${#want_prefix}}"
        ;;
    esac
    j=$((j + 1))
  done
}

# 【签名密钥回落链上"这一级到底设了没有"——**唯一一处**判定】口径必须与 core 同：
# core/telegram.py:151-154 的每一级都是 `(settings.X or "").strip()`，所以裸空与纯空白都算
# 未设置；dotenv 又会剥掉成对的引号，所以 .env 里写 X="" 或 X='' 同样等于未设置。
# 先删掉全部空白，再看是不是只剩一对引号，两种情形一次判完。
# 【为什么是一个函数，而不是在闸门里就地写一遍】闸门现在要对**多级**做同一个判定
# （第三级那个键上面有两级），就地展开必然出现第二份口径，而两份一旦分叉，"这一级算不算
# 设了"会在同一台生产上给出两个答案——那正是上一批刚花一整批消灭掉的那类双胞胎。
# 【R5：它读的是**别人的凭据值**，只判空、绝不打印，判完立刻清掉】与 llm_backend_creds
# 那一格同一条纪律：所有文案里出现的都是**变量名**，不是值。清空不是因为怕它被打印（那由
# 写法保证），而是不让一个装着凭据的变量继续活到下面备份 / 写入 / 重建那一大段里去。
# 返回 0 = 这一级非空（也就是"回落会停在它这里"）。
signing_level_is_set() {
  local probe=""
  env_lookup "$1"
  if [[ "${lookup_hits}" -gt 0 ]]; then
    probe="${lookup_value//[[:space:]]/}"
    case "${probe}" in
      '""'|"''") probe="" ;;
    esac
  fi
  lookup_value=""
  [[ -n "${probe}" ]]
}

hits=0
hit_index=-1
current=""
prefix="${key}="
i=0
while [[ "${i}" -lt "${line_count}" ]]; do
  case "${lines[${i}]}" in
    "${key}="*)
      hits=$((hits + 1))
      if [[ "${hit_index}" -lt 0 ]]; then
        hit_index="${i}"
        # 偏移切片而不是 ${var#pattern}：模式里嵌引号在不同 bash 上解析不一致，
        # 而键名长度是确定的，切片没有歧义。
        current="${lines[${i}]}"
        current="${current:${#prefix}}"
      fi
      ;;
  esac
  i=$((i + 1))
done

# 同名键出现多次时**拒绝执行**，不猜。dotenv 语义是后一条覆盖前一条：只改第一条会得到
# 一次"写成功了但根本没生效"的静默失败——本项目最不能接受的那一类。删掉多余的行是破坏性
# 操作，交给人。
if [[ "${hits}" -gt 1 ]]; then
  printf '✗ %s 在 .env 里出现了 %s 次。本脚本拒绝猜哪一条生效：dotenv 语义下后一条覆盖前一条，只改第一条会写成功却不生效。\n' "${key}" "${hits}" >&2
  printf '  请人自己打开 .env 把多余的行删掉（这是破坏性操作，不由脚本代劳），再重跑。\n' >&2
  exit "${duplicate_status}"
fi

# 【`changed` 提到闸门之前算，只为了 signing_secret 那一道】它要回答"这次写入会不会换掉
# 生效的签名密钥"，而"值和现在这一行一模一样"是那个问题最干脆的一个否定答案。就地再写一遍
# 同样的比较会造出第二份真相，而这两份一旦分叉，闸门与"跳过备份与写入"就会对同一次调用
# 给出互相矛盾的判断。别的闸门都不看它。
changed=1
if [[ "${hits}" -eq 1 && "${current}" == "${value}" ]]; then
  changed=0
fi

# ==== 事前预防闸门（各问各的问题）===============================================
#
# 【为什么是一键一道、而不是一道通用的】docs/RISKS.md 第 14 条讲得很直白：补一个键不是往
# 数组里加个元素，得给它想清楚**这个键特有的闸门**。它们问的确实是互不相干的问题：
#   real_publish       打开真发布之前：人工确认闸门通道活不活？（要打 /api/v1，进容器解析）
#   confirm_carrier    拆掉 Telegram 载体之前：真发布是不是正开着？（只读 .env）
#   llm_backend_creds  切 LLM 后端之前：目标那一边的凭据在不在？（只读 .env）
#   wechat_certified   打开公众号自动发布之前：账号认证那一格是不是真？（只读 .env）
#   wechat_claim       把认证状态记成真之前：这次写入会不会让自动发布当场生效？（只读 .env；
#                      **它从不拒绝**，为什么不拦见它自己那一段）
#   signing_secret     换确认卡签名密钥之前：现在有没有待人点的卡？（先读 .env，再打 /api/v1）
# 把它们塞进一道"通用闸门"就等于让改个预算也要去探 Telegram，纯属添乱。
#
# 【上面标着"只读 .env"的那几道为什么不去问运行中的 core】三条理由，第三条是决定性的：
#   ① 时点对得上：本脚本紧接着就会 `up -d --force-recreate`，容器环境在**创建时**由
#      `env_file: .env` 定型（docker-compose.yml 的 core 服务，`environment:` 块里没有
#      任何一个白名单键，所以 .env 就是唯一来源）。也就是说 .env 里的值正是**这次变更
#      落地之后** core 会看到的东西——比现在正跑着的那份更准。
#   ② 少一层依赖：不需要 curl、不需要 token、不需要容器活着。
#   ③ 决定性的一条：`SW_LLM_BACKEND` 的典型场景就是"dsh 挂了要回退"。如果闸门要靠
#      `docker compose exec core` 才能判，那么容器起不来的时候闸门也判不了，人就被锁在
#      了坏状态里——闸门反过来挡住了恢复动作。只读 .env 没有这个失效模式。
#   如实说明它的边界：有人若通过 compose 的 `environment:` 或宿主机 shell 往容器里另塞
#   同名变量，.env 就不再是唯一来源，这几道闸门会看错。已核实当前 docker-compose.yml
#   没有这么做；真要那么做的时候，得回来改这里。
#
# ---- real_publish：真发布开启之前，确认闸门通道必须是活的 -----------------------
#
# 【它与那道事后闸门的分工——两道都在，不是替换】
#   事前（这里）      写 `.env` **之前**探一次。拦住的是**可预见**的那一半：动手时通道就
#                     已经是死的。拦下来的代价是零——`.env` 一个字节没动，连备份都没建，
#                     容器没重建过，核心根本不知道有人来过。
#   事后（restart.sh） 写完、重建完之后再判一次。兜住的是**写入到生效之间**发生变化的那
#                     一半：探针到重建之间隔着几秒到几十秒，Telegram 那边随时可能掉线。
# 事前拦不住后半段，事后拦不住前半段。所以两道都要，谁也不许替换谁。
# 参照 docs/RISKS.md §12.3：那里原本记着"考虑过事前预防但没做"，理由是需要第五份
# sw_probe；sw_probe 收成单一真相源之后这个理由不成立了，本轮补上。
#
# 【"探到了但不行" 与 "没探到" 必须分开】它们不是同一件事：
#   探到了不行 → 事实明确，处置是去修 Telegram 配置（或先别开真发布）。退 ${precheck_gate_status}。
#   没探到     → 我们**不知道**通道活不活。退 ${precheck_probe_status}。
# 两种情形的文案与退出码都不同，外层据此给不同的下一步。
#
# 【"没探到"时选 fail-closed，理由如实写在这里】默认拒绝写入，不放行也不只是警告：
#   ① 这个方向是整条风险登记册里最危险的一次切换（从"什么都不会真发"翻成"真的会发出去"）。
#      放行等于在**无法证明** R1 红线的主载体活着的情况下，把它打开。
#   ② 探针打的是同一台机器上的 loopback，探的是一个"本来就必须活着"的服务。探不到本身
#      就是一个强信号——不是噪声。
#   ③ 放行的收益近乎零：紧接着的 restart.sh 事后闸门会用**同一条**探针再问一次，大概率
#      同样探不到而失败。区别只在于那时 `.env` 已经是 false、容器已经带着它重建过了。
#      也就是说 fail-open 换来的不是"能办成事"，而是"以更坏的姿势失败"。
#   ④ 拒绝的代价是可逆且很小的：修好凭据/网络再重跑一次，什么都没发生过。
#   反方向（`--value true`，也就是关掉真发布）**永远不受本闸门约束**——出事时人必须能退
#   回安全状态，绝不能被一道闸门锁死在危险状态里。这一点由本地那段 ACTIVE_GATE 判定保证：
#   real_publish 只在 false 方向被点亮。
case "${active_gate}" in
none)
  : ;;
real_publish)
  # docker compose 要在项目目录里跑。这里的 cd 与本段末尾那条 `up -d` 前的 cd 是同一个
  # 目录，重复无害；提前到这里只是因为闸门要用 docker。
  cd "${HOME}/social_workflow"
  precheck_probe_rc=0
  # URL 与超时与 scripts/ops/restart.sh 那道事后闸门**逐字相同**：两道闸门必须问同一个
  # 问题，否则"事前放行、事后卡住"会变成一个查不清的谜题。
  sw_probe 'http://127.0.0.1:8000/api/v1/system/telegram' 5 >/dev/null 2>/dev/null || precheck_probe_rc=$?
  if [[ "${precheck_probe_rc}" -ne 0 ]]; then
    if [[ "${sw_probe_code}" == "401" ]]; then
      printf '✗ 事前预防闸门：探不到人工确认闸门通道——GET /api/v1/system/telegram 返回 401 未授权。\n' >&2
      printf '  这是"没探到"，不是"探到了不行"：core 正常应答了，缺的是运维侧凭据。\n' >&2
      printf '  在拿不到通道状态的情况下打开真发布，等于在无法证明 R1 主载体活着时把它打开，所以这里 fail-closed，.env 一个字节都没动。\n' >&2
      printf '  处置：export SW_OPS_UI_TOKEN=<生产 .env 里 SW_UI_TOKEN 的同一个值>（或写进 ~/.dsh-sw/.credentials.yaml 的 sw_ui_token 键），然后重跑本命令。\n' >&2
    else
      printf '✗ 事前预防闸门：探不到人工确认闸门通道——GET /api/v1/system/telegram 取不到（curl 退出码 %s，HTTP %s）。\n' \
        "${precheck_probe_rc}" "${sw_probe_code}" >&2
      printf '  这是"没探到"，不是"探到了不行"：本次无法判断通道活不活。\n' >&2
      printf '  探的是同机 loopback 上一个本来就该活着的服务，所以探不到本身就是一个信号。在拿不到状态的情况下打开真发布是不可接受的，故 fail-closed，.env 一个字节都没动。\n' >&2
      printf '  处置：先跑 bash scripts/ops/status.sh 与 bash scripts/ops/verify.sh 看 core 到底什么状态，修好之后再重跑本命令。\n' >&2
    fi
    exit "${precheck_probe_status}"
  fi
  # 解析与其余那几个远端脚本同一手法：在容器里用 python 解析 JSON、**只用退出码回话**，
  # shell 只做退出码→裁定的映射。名单以
  # `grep -n 'docker compose exec -T core python3 -c' scripts/ops/*.sh` 为准——但那条 grep
  # 的命中**分两种，别混**：凡是把输出接进变量的（命令替换那种形状）是要把文本取回来的
  # 另一种变体，不属于这一种。按形状分辨即可，这里不记名单。
  # enabled 必须**直接判**：ready 只看 token+chat_id，压根不看总开关；靠 polling 去间接
  # 兜住"总开关关着"是在赌实现细节。
  precheck_parse_rc=0
  printf '%s' "${sw_probe_body}" | docker compose exec -T core python3 -c '
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
' >/dev/null 2>&1 || precheck_parse_rc=$?
  case "${precheck_parse_rc}" in
    0)  precheck_summary="" ;;
    22) precheck_summary="无法解析 /api/v1/system/telegram 的响应" ;;
    23) precheck_summary="ready=false（那张要点的卡片根本推不出去）" ;;
    24) precheck_summary="ready=true 但 polling=false（卡片能推出去，人点了没有线程去收）" ;;
    25) precheck_summary="总开关 enabled=false（SW_TELEGRAM_ENABLED 关着，build_telegram_notifier() 直接返回 None，一条卡都推不出去）" ;;
    *)  precheck_summary="解析异常（退出码 ${precheck_parse_rc}）" ;;
  esac
  if [[ "${precheck_parse_rc}" -eq 22 || "${precheck_parse_rc}" -gt 25 ]]; then
    # 响应拿到了却解析不了，本质仍然是"不知道通道活不活"，归到"没探到"那一档。
    printf '✗ 事前预防闸门：探不到人工确认闸门通道——%s。\n' "${precheck_summary}" >&2
    printf '  这是"没探到"，不是"探到了不行"。fail-closed，.env 一个字节都没动。\n' >&2
    printf '  处置：先查 docker compose ps 与 docker compose logs core，确认容器起着且容器内 python3 能跑通，再重跑本命令。\n' >&2
    exit "${precheck_probe_status}"
  fi
  if [[ "${precheck_parse_rc}" -ne 0 ]]; then
    printf '✗ 事前预防闸门：人工确认闸门通道不可用——%s。\n' "${precheck_summary}" >&2
    printf '  这是"探到了，不行"：状态是确定的，所以拒绝在这个前提下打开真发布。\n' >&2
    printf '  **.env 一个字节都没动**，没有备份、没有重建容器，生产维持原状——这正是事前预防相对事后检测的全部价值。\n' >&2
    printf '  后果如果放任不管：确认闸门等不到人的那一票，内容会被跳过不发（scheduler 记 skipped_unconfirmed），在排期处静默堆积，并在 SW_CONFIRM_TTL_HOURS（默认 24 小时）到点被自动驳回。\n' >&2
    printf '  兜底：工作台的「确认发布」按钮不受 Telegram 影响，仍可用于确认（同一后端 core.confirm.confirm_item）——但那要求人知道主载体已经死了。\n' >&2
    printf '  处置：修好上面那一格（enabled/ready 看服务器 .env 的 SW_TELEGRAM_*；polling 看正在跑的这份代码），再重跑本命令。取证：bash scripts/ops/verify.sh。\n' >&2
    exit "${precheck_gate_status}"
  fi
  # 放行也要留痕，而且这行话必须与实际发生的事一致：这里只证明了"此刻通道是活的"。
  printf '  事前闸门  人工确认闸门通道 enabled=true ready=true polling=true，允许写入\n'
  printf '            （只代表此刻；写入到生效之间仍可能变化，所以下面还有 restart.sh 那道事后闸门）\n'
  ;;

# ---- confirm_carrier：拆掉 Telegram 确认卡载体之前，真发布不能正开着 -------------
#
# 【这道闸门为什么是"拒绝"而不是"警告"——判断依据是可验证的，不是口味】
# 真发布开着（SW_USE_FAKE_PUBLISHERS=false）而 SW_TELEGRAM_ENABLED 被翻成 false 时，
# 本脚本收尾要调 scripts/ops/restart.sh，而它那道 R1 闸门的判据是
# `use_fake_publishers=false 时要求 enabled && ready && polling`——enabled 恰恰会因为
# 这次变更变成 false。也就是说：放行 = **必然**在改完生产之后失败。
# 那正是本文件已经写过的那条道理（"fail-open 换来的不是能办成事，而是以更坏的姿势失败"）。
# 拒绝在写入前发生，代价是零；放行的代价是 .env 改了、容器重建了、Telegram 真关了，
# 然后闸门判红、脚本失败收尾。
#
# 【因果必须写准，本仓在这上面栽过】关掉 Telegram **不等于**内容会越权发出去。
#   core/telegram.py:650-654  build_telegram_notifier() 返回 None → 一条确认卡都推不出去
#   core/scheduler.py:498-505 确认闸门看的是 item.confirmed_at，没人点就**跳过不发**
#                             （stats 记 skipped_unconfirmed）→ R1 红线不因此失效
#   core/api/content.py:283-297 → core/confirm.py:315 confirm_item
#                             工作台「确认发布」是**同一个后端函数**，完全不经 Telegram
#   core/confirm.py:571-573   一次都没推成功过时 TTL 从 scheduled_at 起算，到点
#                             expire_confirmation 自动驳回并释放槽位
# 所以真正的后果是"静默停摆 → 静默丢弃"，不是"越权发布"。
confirm_carrier)
  env_lookup SW_USE_FAKE_PUBLISHERS
  if [[ "${lookup_hits}" -eq 0 ]]; then
    printf '✗ 事前闸门：读不出真发布状态——生产 .env 里根本没有 SW_USE_FAKE_PUBLISHERS 这一行。\n' >&2
    printf '  这是"不知道"，不是"探到了不行"。fail-closed，**.env 一个字节都没动**。\n' >&2
    printf '  为什么不知道也拒绝：代码里的默认值确实是 true（core/config.py:53），但那是**部署里那份代码**的属性，从这台机器上看不见；赌错的代价是改完生产之后被 restart.sh 的 R1 闸门判红。\n' >&2
    printf '  出路（这条命令本身没有闸门，而且把它显式写成 true 会让生产更安全）：\n' >&2
    printf '      bash scripts/ops/env_set.sh --key SW_USE_FAKE_PUBLISHERS --value true\n' >&2
    exit "${carrier_probe_status}"
  fi
  if [[ "${lookup_value}" != "true" && "${lookup_value}" != "false" ]]; then
    printf '✗ 事前闸门：读不出真发布状态——.env 里 SW_USE_FAKE_PUBLISHERS=%s，既不是 true 也不是 false。\n' "${lookup_value}" >&2
    printf '  本脚本写进去的布尔值永远是这两个小写单词；出现别的写法说明这一行是手工改的。\n' >&2
    printf '  这是"不知道"。fail-closed，**.env 一个字节都没动**。\n' >&2
    printf '  出路：bash scripts/ops/env_set.sh --key SW_USE_FAKE_PUBLISHERS --value true（或 false），把它归一化之后再来。\n' >&2
    exit "${carrier_probe_status}"
  fi
  if [[ "${lookup_value}" == "false" ]]; then
    printf '✗ 事前闸门：真发布正开着（.env 里 SW_USE_FAKE_PUBLISHERS=false），拒绝关掉确认卡的推送载体。\n' >&2
    printf '  **.env 一个字节都没动**，没有备份、没有重建容器，生产维持原状。\n' >&2
    printf '  因果写准：关掉它**不会**让内容越权发出去。core/scheduler.py:498-505 的人工确认闸门看的是 item.confirmed_at，没人点就跳过不发（记 skipped_unconfirmed），R1 红线不因此失效。\n' >&2
    printf '  真正的后果是静默停摆再静默丢弃：卡推不出去（core/telegram.py:650-654 返回 None），内容堆在排期处，core/confirm.py:571-573 的 TTL 在一次都没推成功过时从 scheduled_at 起算，到点（SW_CONFIRM_TTL_HOURS，默认 24 小时）自动驳回并释放槽位。\n' >&2
    printf '  第二载体仍在：工作台「确认发布」不受 Telegram 影响，走同一个后端函数（core/api/content.py:283-297 → core/confirm.py:315 confirm_item）——但那要求人知道主载体已经没了。\n' >&2
    printf '  为什么是拒绝而不是警告：这个组合会被 scripts/ops/restart.sh 的 R1 闸门**必然**判红（真发布开着而 enabled=false），本脚本随后失败收尾。与其改完生产再失败，不如写入前就停手。\n' >&2
    printf '  出路（两步，中间那一步让生产更安全，不存在被锁死）：\n' >&2
    printf '      bash scripts/ops/env_set.sh --key SW_USE_FAKE_PUBLISHERS --value true\n' >&2
    printf '      bash scripts/ops/env_set.sh --key SW_TELEGRAM_ENABLED --value false\n' >&2
    exit "${carrier_gate_status}"
  fi
  if [[ "${lookup_hits}" -gt 1 ]]; then
    printf '  事前闸门  ⚠ SW_USE_FAKE_PUBLISHERS 在 .env 里出现 %s 次，按 dotenv 语义取最后一条\n' "${lookup_hits}"
  fi
  printf '  事前闸门  .env 里 SW_USE_FAKE_PUBLISHERS=true（真发布没开着），允许拆掉 Telegram 载体\n'
  printf '            （之后工作台「确认发布」是唯一的确认入口；内容仍然必须有人点，只是没人来提醒了）\n'
  ;;

# ---- llm_backend_creds：切 LLM 后端之前，目标那一边的凭据必须在 .env 里且非空 -----
#
# 【为什么两个方向都查】docs/RISKS.md 第 14 条点名这个键代价最高，场景是"dsh 挂了要回退"。
# 难点从来不是校验 anthropic / dsh 这两个字符串，而是：切过去之前得确认那一边起得来。
# generation/llm.py:271-278 是**懒加载**——缺 key 时 core 照常启动、照常接请求，直到第一次
# 真出稿才抛 LLMUnavailable。也就是说一次没凭据的"回退"不会当场报错，它把故障推迟到排期
# 里，比不回退更糟。所以这道闸门在写入前就问。
llm_backend_creds)
  need_env=""
  case "${value}" in
    anthropic)
      need_env="ANTHROPIC_API_KEY"
      ;;
    dsh)
      # dsh 用哪个凭据变量取决于走哪条路由：generation/llm_dsh.py:266-277 的
      # dsh_credentials_ready() → provider_api_key_env(entries, settings.dsh_provider)
      # 去 configs/dsh/cordis.yml 里读那条 provider 的 apiKeyEnv。这里把那张映射照抄成
      # bash（远端没有 YAML 解析器），tests/ops/test_env_set.sh 有一条断言直接解析
      # cordis.yml 与这段比对，防止两边漂移。
      env_lookup SW_DSH_PROVIDER
      if [[ "${lookup_hits}" -eq 0 ]]; then
        dsh_provider="deepseek-official"   # core/config.py:140 的默认值
      else
        dsh_provider="${lookup_value}"
      fi
      case "${dsh_provider}" in
        deepseek-official|deepseek) need_env="DEEPSEEK_API_KEY" ;;
        gateway)                    need_env="SW_DSH_GATEWAY_API_KEY" ;;
        anthropic)                  need_env="ANTHROPIC_API_KEY" ;;
        *)
          printf '✗ 事前闸门：.env 里 SW_DSH_PROVIDER=%s 不是 configs/dsh/cordis.yml 注册过的路由名，无法确定 dsh 要用哪个凭据变量。\n' "${dsh_provider}" >&2
          printf '  已注册的四条：deepseek-official / deepseek（DEEPSEEK_API_KEY）、gateway（SW_DSH_GATEWAY_API_KEY）、anthropic（ANTHROPIC_API_KEY）。\n' >&2
          printf '  fail-closed，**.env 一个字节都没动**：路由名不对的话 dsh runtime 握手本来也会失败（core/config.py:139），切过去只会换一种死法。\n' >&2
          exit "${backend_creds_status}"
          ;;
      esac
      ;;
    *)
      printf '✗ 事前闸门：目标后端 %s 不认识（只接受 anthropic / dsh）。这不该走到远端，说明本地校验被绕过了。\n' "${value}" >&2
      exit "${backend_creds_status}"
      ;;
  esac
  env_lookup "${need_env}"
  if [[ "${lookup_hits}" -eq 0 || -z "${lookup_value}" ]]; then
    if [[ "${lookup_hits}" -eq 0 ]]; then
      printf '✗ 事前闸门：目标后端 %s 要的凭据 %s 在生产 .env 里**没有这一行**，拒绝切换。\n' "${value}" "${need_env}" >&2
    else
      printf '✗ 事前闸门：目标后端 %s 要的凭据 %s 在生产 .env 里**是空值**，拒绝切换。\n' "${value}" "${need_env}" >&2
    fi
    printf '  **.env 一个字节都没动**，没有备份、没有重建容器。\n' >&2
    printf '  为什么这也要拦：generation/llm.py:271-278 是懒加载——缺 key 时 core 照常起来，直到第一次真出稿才抛 LLMUnavailable。这次「回退」不会当场报错，它只是把故障推迟到排期里，比不回退更糟。\n' >&2
    printf '  处置：先把 %s 写进生产 .env 并确认非空，再重跑本命令。\n' "${need_env}" >&2
    printf '  **本脚本做不到这一步**：凭据类键刻意不在白名单上（要走 secret 策略与零回显流程，且改签名密钥有 R1 邻近副作用），docs/RISKS.md 第 14 条如实记着这个缺口。\n' >&2
    exit "${backend_creds_status}"
  fi
  # 【R5：这一格是本脚本唯一会把**别人的凭据值**读进变量的地方，用完立刻清掉】
  # 上面只判了 `-z`，从头到尾没打印过它——所有文案里出现的都是**变量名** ${need_env}，
  # 不是值。清空不是因为怕它被打印（那由上面的写法保证），而是不让一个装着 API key 的
  # 变量继续活到下面备份/写入/重建那一大段里去：那段代码将来会被人改，而改的人不该
  # 需要先知道"哦这里还有个变量装着凭据"。
  lookup_value=""
  printf '  事前闸门  目标后端 %s 的凭据 %s 在 .env 里存在且非空，允许切换\n' "${value}" "${need_env}"
  printf '            （只证明了那一行有值：额度够不够、dsh runtime 装没装，这道闸门都答不了）\n'
  ;;

# ---- wechat_certified：打开公众号平台级自动发布之前，账号认证那一格必须是真 --------
#
# 【拦它不是因为危险，是因为"没用"】publishers/wechat_mp/publisher.py:238-249 的双确认闸门
# 要 server_switch && account_certified && confirm_publish 三者皆真。WECHAT_CERTIFIED 是假
# 的时候，把 WECHAT_AUTO_PUBLISH 翻成 true 是个**不会生效的空操作**——内容照旧只落草稿箱，
# 而人会以为自动发布已经开了。那正是本仓最不能接受的一类：改成功了、看起来对、其实没生效。
# scripts/preflight.py:122-133 对同一组合的裁定就是 FAIL，这里与它同口径，不另发明说法。
# 拒绝的代价恰好是零：被拒之后生产所处的状态，与放行之后的实际行为**完全一样**（只落草稿箱）。
wechat_certified)
  env_lookup WECHAT_CERTIFIED
  if [[ "${lookup_hits}" -eq 0 || "${lookup_value}" != "true" ]]; then
    if [[ "${lookup_hits}" -eq 0 ]]; then
      printf '✗ 事前闸门：生产 .env 里没有 WECHAT_CERTIFIED 这一行（出厂默认 false，core/config.py:231），拒绝打开 WECHAT_AUTO_PUBLISH。\n' >&2
    else
      printf '✗ 事前闸门：生产 .env 里 WECHAT_CERTIFIED=%s，不是 true，拒绝打开 WECHAT_AUTO_PUBLISH。\n' "${lookup_value}" >&2
    fi
    printf '  **.env 一个字节都没动**，没有备份、没有重建容器。\n' >&2
    printf '  拦它不是因为危险，是因为没用：publishers/wechat_mp/publisher.py:238-249 的双确认闸门要 server_switch && account_certified && confirm_publish 三者皆真，认证那一格是假时这次变更是个**不会生效的空操作**，内容照旧只落草稿箱——而人会以为自动发布已经开了。\n' >&2
    printf '  同口径：scripts/preflight.py:122-133 对这一组合的门禁裁定同样是 FAIL。\n' >&2
    printf '  账号确实已认证：把 WECHAT_CERTIFIED=true 写进生产 .env 后重跑。**本脚本做不到**——它不在白名单上，docs/RISKS.md 第 14 条如实记着这个缺口。\n' >&2
    printf '  账号还没认证：2025-07 起未认证主体的 freepublish 权限被回收（core/config.py:230），此时唯一合规路径就是继续只落草稿箱、由人在公众号后台点发表。\n' >&2
    exit "${wechat_cert_status}"
  fi
  printf '  事前闸门  .env 里 WECHAT_CERTIFIED=true，允许打开平台级自动发布\n'
  printf '            （这只是三道闸门里的第一道；每一条内容仍要审核 UI 写入 confirm_publish 才会真 freepublish）\n'
  ;;

# ---- wechat_claim：把认证状态记成真之前……什么都不拦。这一段讲的是**为什么不拦** ------
#
# 【先把这道闸门与上一道的不对称说清楚，因为它看着像对称其实不是】
# 上一道（wechat_certified）在 WECHAT_AUTO_PUBLISH=true 时要求 WECHAT_CERTIFIED 已经为真。
# 镜像过来很容易写成："WECHAT_CERTIFIED=true 时要求 WECHAT_AUTO_PUBLISH 已经为真"。
# **那样两道合起来是一个死锁**：两个键都是 false 的起点上，改哪一个都会被另一道拦住，
# 这一对永远上不去。所以这对开关里**必须恰好有一个不被对方约束**，而只能是这一个——
# 因为另一个（WECHAT_AUTO_PUBLISH）是我们自己的开关，而这一个记的是**外部事实**，
# 正确的次序本来就是"先如实记录事实，再打开开关"。不对称是被逼出来的，不是随手定的。
#
# 【"它凭什么为真"这一问，本工具面结构上答不了——所以不做成拦截】
#   ① 这个值代表的是微信那边的认证状态。要核实只有一条路：从生产向 api.weixin.qq.com
#      发一次真实出站请求。本仓对这件事有明确先例——verify.sh 的 --preflight 之所以是
#      **显式 opt-in**，正是因为外部连通性探测历史上跑到超时把上游会话卡死过。
#   ② 更要命的是失效模式：让这个闸门依赖腾讯可达，等于"腾讯抖一下，本地一次改配置就做不
#      成"。这与本文件已经拒绝过的那个模式是同一个（llm_backend_creds 那段第 ③ 条：
#      闸门若要靠外部依赖才能判，外部一坏它就反过来挡住恢复动作）。
#   ③ 一道绝大多数时候只会回答"我不知道"的闸门，会把人训练成忽略闸门。
#   ④ 而且拒绝的代价在这里**不是零**：WECHAT_CERTIFIED 没有任何别的合规改法，拦住它就是
#      把人锁死在"闸门告诉你这一格是假的、你却没有办法改它"——那恰恰是本轮要修的那件事。
#
# 【那"骗过自己的闸门"这个担忧靠什么答】靠两件事，都不需要网络：
#   ㈠ 说清代价，并说准它是**响的**不是静默的：填错时稿子照样先进草稿箱
#      （publishers/wechat_mp/publisher.py:294 draft_add 已成功），随后 freepublish 撞上
#      errcode=48001「无接口权限」，publishers/wechat_mp/client.py:338 抛 PermanentError，
#      按条报错。**没有任何内容会因此越权发出去**——它是发不出去，不是发错。本仓给闸门/
#      拒绝留位置的标准一直是"故障静默不静默"（预算 -1、关 Telegram 都是静默的，所以拦；
#      这一条是响的，所以说清楚就够）。
#   ㈡ 把这次变更的**当场后果**摆出来：去 .env 读 WECHAT_AUTO_PUBLISH。它已经是 true 时，
#      这一个写入就让 server_switch && account_certified 这一对当场成立——人必须知道自己
#      按下的不只是"记录一个事实"。
#
# 【它仍然是一道 ACTIVE_GATE，唯一的实质作用是禁掉 --write-only】上面 ㈡ 那种情形下，
# "`.env` 里这一对已经成立、运行中的 core 还没有"正是最典型的"上了膛没击发"。
wechat_claim)
  env_lookup WECHAT_AUTO_PUBLISH
  if [[ "${lookup_hits}" -eq 0 || "${lookup_value}" == "false" ]]; then
    printf '  事前闸门  .env 里 WECHAT_AUTO_PUBLISH 仍是 false（或没有这一行，出厂默认 false）\n'
    printf '            所以本次变更**不会**让平台级自动发布生效：公众号内容照旧只落草稿箱\n'
  elif [[ "${lookup_value}" == "true" ]]; then
    printf '  事前闸门  ⚠ .env 里 WECHAT_AUTO_PUBLISH 已经是 true——**这一个写入就让平台级自动发布成立**\n'
    printf '            （双确认闸门的前两道 server_switch && account_certified 就此都为真；\n'
    printf '             此后只差每条内容的 confirm_publish，那一格由审核 UI 在人工批准时写入）\n'
  else
    printf '  事前闸门  ⚠ .env 里 WECHAT_AUTO_PUBLISH=%s，既不是 true 也不是 false\n' "${lookup_value}"
    printf '            读不准它，所以这次变更会不会让平台级自动发布当场生效，**本脚本不下结论**\n'
  fi
  printf '            本闸门**不拦**这次写入：WECHAT_CERTIFIED 记的是微信那边的事实，本工具面核实不了\n'
  printf '            填错的代价：稿子照样进草稿箱，freepublish 那一步报 errcode=48001（PermanentError，按条报）\n'
  ;;

# ---- signing_secret：换 Telegram 确认卡的签名密钥之前，不能还有待人点的卡 ------------
#
# 【这道闸门修的是一次真事故，不是假想】2026-08-22 编排方在生产上跑
# `env_set.sh --key SW_UI_TOKEN --generate` 启用鉴权。生产 .env 里 SW_TELEGRAM_SIGNING_SECRET
# 是空的（.env.example 的默认形态就是空），于是 core/telegram.py:151-154 的三级回落
#     SW_TELEGRAM_SIGNING_SECRET → SW_UI_TOKEN → TELEGRAM_BOT_TOKEN
# 里生效的那一级从第三级换到了第二级——**签名密钥当场被换掉了**。已推出去还没人点的卡按下去
# 会 bad_signature（用户侧表现为按钮没反应），最终被 TTL 自动驳回。事后查证是 0 条、没造成
# 损失，但那是运气：docs/RISKS.md §8.5 把"先确认没有待人点的确认卡"定为第 0 步前置，而那是
# **人工**前置，被跳过了。一条只写在文档里、没人执行也没人发现的前置，等于没有前置。
#
# 【三个键各自在什么条件下会动到签名密钥——真值表，别再推导一遍】
# core/telegram.py:151-154：
#     secret = 一级 SW_TELEGRAM_SIGNING_SECRET → 二级 SW_UI_TOKEN → 三级 TELEGRAM_BOT_TOKEN
# 每一级都是 `(… or "").strip()`，取第一个非空的那一级。下表里 S / U 表示"一级 / 二级在
# 生产 .env 里非空"，判空口径见远端 signing_level_is_set（裸空、纯空白、"" 、'' 都算空）。
#
#   本次要改的键                  S    U    生效密钥现在来自    这次写入会不会换掉生效的密钥
#   ---------------------------  ---  ---  -----------------  --------------------------------
#   SW_TELEGRAM_SIGNING_SECRET   任意 任意  任意               **会**（写完必然是一级；无免检方向）
#   SW_UI_TOKEN                  真   任意  一级               不会 → 放行，不设闸门
#   SW_UI_TOKEN                  假   任意  二级(本键) 或三级   **会** → 去读待人点的确认卡条数
#   TELEGRAM_BOT_TOKEN           真   任意  一级               不会 → 放行，不设闸门
#   TELEGRAM_BOT_TOKEN           假   真    二级               不会 → 放行，不设闸门
#   TELEGRAM_BOT_TOKEN           假   假    三级(本键)          **会** → 去读待人点的确认卡条数
#
# 六行的规律只有一条：**回落链上排在本键前面的那几级里，只要有一级非空，本次写入就动不到
# 生效的密钥**（那时回落在更高的一级就停住了，够不到本键）。所以这道闸门不按键名分支，
# 只按"我上面有谁"分支——那份"上面有谁"是本地 sw_env_policy 的 POLICY_SIGNING_ABOVE 那一格。
# 两个推论，都别绕过去：
#   · 第一级（SW_TELEGRAM_SIGNING_SECRET）上面没有任何一级，所以它**永远**没有免检方向；
#   · 第三级（TELEGRAM_BOT_TOKEN）绝大多数时候都会走"放行、不设闸门"那一格——今天的生产
#     一级为空但二级非空（2026-08-22 那次之后 SW_UI_TOKEN 就设上了），所以换 bot token 动不到
#     签名密钥。**但这不是"所以不用管"**：一级二级都空是 .env.example 的出厂形态，闸门缺了
#     这一格就是一个活的缺口，而不是一个理论上的缺口。
#
# 【判定链，四问，顺序是有讲究的】
#   ① 这次写入会不会真的改变什么？`changed=0`（值与 .env 里现在那一行一模一样）→ 放行。
#      这一格不是优化，是**防自锁**：上一次调用若正好死在"写入成功、容器重建失败"之间
#      （退出码 36），.env 已是目标值而运行中的 core 还不是，重跑本命令是唯一的收敛动作；
#      让闸门去挡它，等于闸门反过来锁死了恢复路径。签名密钥在这条路径上一个比特都不会变。
#   ② 回落链上排在本键前面的那几级里，有任何一级**已显式设且非空**？→ 放行（见上面的真值表）。
#      这一格就是 SW_TELEGRAM_SIGNING_SECRET 那个键的全部价值，也是本闸门唯一的**永久解法**：
#      把第一级设上，此后轮换第二、三级都不再受本闸门约束。改第一级本身时**没有这一格**。
#   ③ 读待人点的确认卡条数：0 条 → 放行。
#   ④ >0 条 → **拒绝**（除非 --accept-breaking-pending-confirm-cards）。
#      读不出来 → **fail-closed 拒绝**（同样可被那个旗子覆盖，理由见下）。
#
# 【读数只有一份定义】`sw_awaiting_confirm` 由本机的 sw_ops_emit_awaiting_confirm_definition
# 发射进这条脚本流，唯一定义处在 scripts/ops/ui_token.sh；scripts/ops/verify.sh 用**同一份**
# 把它渲染成取证行。端点选择、上界口径、"没取到 ≠ 0 条"的理由都写在那个函数上方。
# 这里只强调一件与闸门有关的事：那个计数是**上界**（awaiting_confirm 不看 confirm_pushed_at，
# 还没推出卡的条目也计在内）。当闸门用，偏大的方向恰好是安全的；但文案里绝不许写成
# "这么多条会失效"——那是把上界当精确值卖。
#
# 【为什么"读不出来"也拒绝】与 real_publish 那道同一条道理：在**无法证明**没有卡在等人点的
# 前提下换掉签名密钥，等于把一次可预见的破坏交给运气。而且这一格**没有事后闸门兜底**——
# restart.sh 那道 R1 闸门问的是"确认通道活不活"，它对"卡还能不能验签"一无所知，判绿也说明
# 不了任何事。事前这一道就是唯一的一道。
#
# 【为什么 override 也覆盖"读不出来"这一档——这一条是被逼出来的，不是宽容】
#   `--key SW_UI_TOKEN --from-credentials` 的头号用途就是"两边不一致的收敛"，而两边不一致时
#   探针拿到的**必然**是 401，也就是"读不出来"。若这一档不给出路，那条收敛命令就永远跑不成，
#   闸门恰好锁死了它本该保护的那件事的恢复路径。老版本 core 没有 /api/v1/dashboard（404）、
#   容器起不来（exec 失败）同理。所以旗子两档通吃，但两档记下来的话不一样：有条数就记条数，
#   没条数就明说"你是在不知道有多少张卡的情况下换的"。
signing_secret)
  signing_decided=0
  if [[ "${changed}" -eq 0 ]]; then
    printf '  事前闸门  %s 在 .env 里已经就是本次要写入的值：这次写入不会改变任何签名密钥，放行\n' "${key}"
    printf '            （这一格保的是「写入成功、容器重建失败」之后的重跑：那时 .env 已是目标值、\n'
    printf '             运行中的 core 还不是，闸门不该反过来挡住把它收敛回去的那条命令）\n'
    signing_decided=1
  fi
  # 第 2 段：照着回落链走，一级都不认识键名。signing_above 里的键按级序排（第 1 级在前），
  # 命中第一个非空的就停——那正是 core 的取值顺序，所以"停在第几级"这句话是准的。
  if [[ "${signing_decided}" -eq 0 && "${signing_above}" != "none" && "${signing_above}" != "-" ]]; then
    signing_level=0
    for above_key in ${signing_above}; do
      signing_level=$((signing_level + 1))
      if signing_level_is_set "${above_key}"; then
        printf '  事前闸门  .env 里 %s 已显式设置且非空：改 %s **不会**动确认卡的签名密钥，放行\n' \
          "${above_key}" "${key}"
        printf '            （core/telegram.py:151-154 的三级回落停在第 %s 级 %s——%s 排在它后面，够不着；\n' \
          "${signing_level}" "${above_key}" "${key}"
        printf '             这正是 SW_TELEGRAM_SIGNING_SECRET 那个键存在的意义：解耦）\n'
        signing_decided=1
        break
      fi
    done
  fi
  if [[ "${signing_decided}" -eq 0 ]]; then
    # docker compose 要在项目目录里跑。与 real_publish 那一格同一个目录，重复无害。
    cd "${HOME}/social_workflow"
    if sw_awaiting_confirm 'http://127.0.0.1:8000/api/v1/dashboard?days=1' 20; then
      if [[ "${sw_awaiting_count}" -eq 0 ]]; then
        printf '  事前闸门  待人点的确认卡 0 条，允许换签名密钥\n'
        printf '            （口径 counters.awaiting_confirm，见 core/api/dashboard.py::_awaiting_confirm；\n'
        printf '             只代表此刻——本闸门没有事后那一道，写入到生效之间新推出去的卡挡不住）\n'
      elif [[ "${accept_breaking}" -eq 1 ]]; then
        printf '  事前闸门  ⚠ 待人点的确认卡 %s 条，但你给了 --accept-breaking-pending-confirm-cards，放行\n' "${sw_awaiting_count}"
        printf '            你接受的是：这 %s 条里**已经推出卡**的那部分，按下去会验签失败（日志 bad_signature，\n' "${sw_awaiting_count}"
        printf '            用户侧表现为按钮没反应），并在 SW_CONFIRM_TTL_HOURS（默认 24 小时）到点被自动驳回。\n'
        printf '            %s 是**上界**不是精确值：awaiting_confirm 不看 confirm_pushed_at，还没推出卡的条目\n' "${sw_awaiting_count}"
        printf '            也计在里面，而它们的卡是换密钥之后才生成的、签的是新密钥，不会失效。\n'
        printf '            兜底：工作台「确认发布」不经 Telegram（core/api/content.py → core/confirm.py:315），\n'
        printf '            这 %s 条仍可在工作台上点掉——但那要求人知道那些按钮已经不管用了。\n' "${sw_awaiting_count}"
      else
        printf '✗ 事前闸门：还有 %s 条待人点的确认卡，拒绝换 Telegram 确认卡的签名密钥。\n' "${sw_awaiting_count}" >&2
        printf '  **.env 一个字节都没动**，没有备份、没有重建容器，生产维持原状。\n' >&2
        printf '  为什么拦：%s 这次写入会换掉生效的 HMAC 签名密钥（core/telegram.py:151-154 的三级回落），已推出去还没人点的卡按下去会验签失败（日志 bad_signature，用户侧表现为按钮没反应），最终被 TTL 自动驳回。\n' "${key}" >&2
        printf '  条数口径：counters.awaiting_confirm 是**上界**——它不看 confirm_pushed_at，还没推出卡的条目也计在里面，那些卡是换密钥之后才生成的、不会失效。所以这 %s 条里会坏的是"已经推出去"的那一部分，不是全部。\n' "${sw_awaiting_count}" >&2
        printf '  处置一（等）：等这些卡被人点掉，或被 SW_CONFIRM_TTL_HOURS（默认 24 小时）到点自动驳回，再重跑本命令。取证：bash scripts/ops/verify.sh 会打印这一格。\n' >&2
        if [[ "${signing_above}" != "none" ]]; then
          printf '  处置二（根治，只做一次）：先把签名密钥显式设上，此后 %s 再怎么换都不动它——这条耦合就永久解开了：\n' "${key}" >&2
          printf '      bash scripts/ops/env_set.sh --key %s --generate\n' "${signing_root_key}" >&2
          printf '      （注意那一条**本身**也走这道闸门：它就是在改签名密钥。挑一个 0 条的时刻做它。）\n' >&2
        else
          printf '  处置二：这一条**就是**那个根治动作——设上它之后 SW_UI_TOKEN 再怎么换都不会动签名密钥。它本身只能挑一个 0 条的时刻做。\n' >&2
        fi
        printf '  处置三（明知故犯）：加 --accept-breaking-pending-confirm-cards。输出里会如实记下你接受了多少条会受影响。\n' >&2
        exit "${signing_gate_status}"
      fi
    elif [[ "${accept_breaking}" -eq 1 ]]; then
      printf '  事前闸门  ⚠ 读不出待人点的确认卡条数（%s），但你给了 --accept-breaking-pending-confirm-cards，放行\n' "${sw_awaiting_reason}"
      printf '            你接受的是：**在不知道有多少张卡等着人点的情况下**换签名密钥。已推出去还没人点的卡\n'
      printf '            按下去会验签失败（bad_signature），最终被 TTL 自动驳回。\n'
      printf '            本次**没有条数可记**——这一行就是全部记录：你接受的是一个未知数，不是一个已知的小数字。\n'
    else
      printf '✗ 事前闸门：**读不出**待人点的确认卡条数——%s。按 fail-closed 拒绝换签名密钥。\n' "${sw_awaiting_reason}" >&2
      printf '  这是"不知道"，不是"探到了有卡"。**.env 一个字节都没动**，没有备份、没有重建容器。\n' >&2
      printf '  为什么不知道也拒绝：这次写入会换掉生效的 HMAC 签名密钥，而本闸门**没有事后那一道**——restart.sh 的 R1 闸门问的是确认通道活不活，它对"卡还能不能验签"一无所知，判绿也说明不了任何事。事前这一道就是唯一的一道。\n' >&2
      printf '  处置：先让这条读数能跑通，再重跑本命令。取证：bash scripts/ops/verify.sh 里「待人点的确认卡」那一格与这里读的是同一个数。\n' >&2
      printf '        401  = 本机的 token 与生产不一致（或没配）。export SW_OPS_UI_TOKEN=<生产 .env 里 SW_UI_TOKEN 的同一个值>，或写进 ~/.dsh-sw/.credentials.yaml 的 sw_ui_token 键。\n' >&2
      printf '        404  = 这版 core 还没有 /api/v1/dashboard，本闸门在它上面读不出任何东西。\n' >&2
      printf '        其它 = 先 bash scripts/ops/status.sh 看 core 起没起来。\n' >&2
      printf '  明知故犯：加 --accept-breaking-pending-confirm-cards。它**刻意**也覆盖这一档——否则"两边 token 不一致"这种必然 401 的情形会永远收敛不了，而 --from-credentials 正是那种不一致唯一的收敛动作。\n' >&2
      exit "${signing_probe_status}"
    fi
  fi
  ;;

*)
  printf '✗ 内部错误：远端收到无法识别的闸门名 %s。拒绝执行，**.env 一个字节都没动**。\n' "${active_gate}" >&2
  printf '  合法值只有 none / real_publish / confirm_carrier / llm_backend_creds / wechat_certified / wechat_claim / signing_secret，由本地 sw_env_policy() 那张表派生。\n' >&2
  printf '  走到这里说明本地与远端对闸门集合的认识不一致——这是纵深防御的那一层在说话，不要绕过它。\n' >&2
  exit "${bad_value_status}"
  ;;
esac

if [[ "${changed}" -eq 0 ]]; then
  # 值未变化：不备份、不写入。**但仍然继续往下走重建**——本脚本的契约是"让这个键在运行中
  # 的 core 上等于这个值"，不是"编辑一个文件"。上一次执行若正好死在写入与重建之间，
  # .env 已是目标值而 core 还不是，跳过重建就会把那个半截状态永久留下。
  printf '  .env 变更  %s 的值与本次要写入的值相同，跳过备份与写入\n' "${key}"
else
  # ---- 备份先于写入 ----------------------------------------------------------
  # scripts/ops/backup.sh 备份的是数据库与台账（`social_workflow.db` + `accounts.yaml`），
  # **不含 .env**——已核实。所以这一份必须本脚本自己做。
  # 放在 ~/sw-env-backups 而**不是** ~/social_workflow 下：后者是 git 工作树，
  # `.env.bak-*` 这类文件不在 .gitignore 里，会让 verify.sh 的"工作树干净"判失败、
  # 让 update.sh 拒绝部署。备份不该有这种副作用。
  mkdir -p "${backup_dir}" || { printf '✗ 无法创建备份目录 %s\n' "${backup_dir}" >&2; exit "${backup_status}"; }
  chmod 700 "${backup_dir}" || { printf '✗ 无法收紧备份目录权限 %s\n' "${backup_dir}" >&2; exit "${backup_status}"; }
  backup_file="${backup_dir}/env-${stamp}"
  if [[ -e "${backup_file}" ]]; then
    printf '✗ 备份文件已存在，拒绝覆盖：%s\n' "${backup_file}" >&2
    exit "${backup_status}"
  fi
  cp "${env_file}" "${backup_file}" || { printf '✗ 备份 .env 失败：%s\n' "${backup_file}" >&2; exit "${backup_status}"; }
  chmod 600 "${backup_file}" || { printf '✗ 无法收紧备份文件权限：%s\n' "${backup_file}" >&2; exit "${backup_status}"; }
  printf '  .env 备份  %s（0600）\n' "${backup_file}"

  # ---- 原子写入 --------------------------------------------------------------
  # 先写同目录临时文件、chmod 600、再 mv。同目录 mv 是 rename(2)，原子：`.env` 在任何一
  # 瞬间要么是旧的完整内容、要么是新的完整内容，**绝不会是半截**。就地编辑（sed -i 之类）
  # 写到一半断电或断链就会把 .env 弄坏，而 .env 坏了 core 起不来。
  # 临时文件必须与 .env 同目录（跨文件系统的 mv 会退化成 copy+unlink，不再原子），
  # 而那个目录是 git 工作树——所以用 trap 保证任何退出路径都不留残渣，否则一个残留的
  # 临时文件会让 verify.sh 判"工作树不干净"。
  tmp_file="${env_file}.sw-ops-tmp.$$"
  cleanup_tmp() { [[ -z "${tmp_file}" ]] || rm -f "${tmp_file}"; }
  trap cleanup_tmp EXIT
  if [[ -e "${tmp_file}" ]]; then
    printf '✗ 临时文件已存在，拒绝覆盖：%s\n' "${tmp_file}" >&2
    exit "${write_status}"
  fi
  : >"${tmp_file}" || { printf '✗ 无法创建临时文件：%s\n' "${tmp_file}" >&2; exit "${write_status}"; }
  chmod 600 "${tmp_file}" || { printf '✗ 无法收紧临时文件权限：%s\n' "${tmp_file}" >&2; exit "${write_status}"; }
  {
    i=0
    while [[ "${i}" -lt "${line_count}" ]]; do
      if [[ "${i}" -eq "${hit_index}" ]]; then
        # 键已存在：**替换那一行**，不追加重复行。追加会造出上面刚拒绝过的那种 .env。
        printf '%s=%s\n' "${key}" "${value}"
      else
        printf '%s\n' "${lines[${i}]}"
      fi
      i=$((i + 1))
    done
    if [[ "${hits}" -eq 0 ]]; then
      printf '%s=%s\n' "${key}" "${value}"
    fi
  } >>"${tmp_file}" || { printf '✗ 写临时文件失败：%s\n' "${tmp_file}" >&2; exit "${write_status}"; }
  mv "${tmp_file}" "${env_file}" || { printf '✗ 原子替换失败，.env 未被改动：%s\n' "${env_file}" >&2; exit "${write_status}"; }
  tmp_file=""
  chmod 600 "${env_file}" || { printf '✗ 无法收紧 .env 权限：%s\n' "${env_file}" >&2; exit "${write_status}"; }
  if [[ "${hits}" -eq 1 ]]; then
    printf '  .env 写入  %s 已就地替换（原子：临时文件 + mv）\n' "${key}"
  else
    printf '  .env 写入  %s 已新增（原子：临时文件 + mv）\n' "${key}"
  fi
fi

if [[ "${write_only}" -eq 1 ]]; then
  printf '  生效     跳过（--write-only）：.env 已改，**运行中的 core 还是旧值**\n'
  printf '\n远端变更完毕（--write-only：未重建容器）\n'
  exit 0
fi

# ---- 让 .env 真正生效 --------------------------------------------------------
# 【这一步为什么不能交给 restart.sh】容器的环境变量在**创建时**定型：compose 把
# `env_file: .env` 解析进服务配置，随后写进容器的 Config.Env，之后再没有任何 API 能改它。
# `docker compose restart` 只是 restart 那个已存在的容器，**不重建、也就读不到新的 .env**
# （compose 官方文档对 restart 的说明就是"配置改动不会被这条命令反映出来"）。
# 所以 restart.sh 那条 `docker compose restart core` 单独用**改不动任何 .env 变更**——
# 这一点 docs/RISKS.md 第 12 条里"改 .env 再重启"的措辞不够精确，本轮已在那里补正。
# 必须 `up -d` 让 compose 重建容器。用 --force-recreate 而不是依赖 compose 的配置哈希：
# 哈希算法是实现细节，而"这次一定要带着新 .env 重来"是本脚本的硬要求，不该押在实现细节上。
# --no-build：构建是 update.sh 的职责。镜像缺失时这里应当**大声失败**，而不是顺手替
# update.sh 构建一个没人核验过 SHA 的镜像。
cd "${HOME}/social_workflow"
if ! docker compose up -d --force-recreate --no-build core </dev/null; then
  printf '✗ docker compose up -d --force-recreate --no-build core 失败：.env 已经是新值，但运行中的 core 还是旧值。\n' >&2
  printf '  这是个半截状态：此后任何人一次寻常的 docker compose up -d 都会把新值带上来，而那一刻没有闸门在场。\n' >&2
  printf '  先查 docker compose ps 与 docker compose logs core；镜像缺失请走 scripts/ops/update.sh --apply（构建是它的职责）。\n' >&2
  exit "${recreate_status}"
fi
# 防回归哨兵：它排在本段所有边界命令之后。一旦从输出里消失，就说明有命令又把脚本正文吞了
# ——那时"已重建"这句话会在什么都没做的情况下被打出来。测试直接断言它存在。
printf '  生效     容器已按新 .env 重建（docker compose up -d --force-recreate --no-build core）\n'
printf '\n远端变更完毕\n'
exit 0
} </dev/null
REMOTE_ENV_SET
if [[ "${remote_status}" -eq 255 ]]; then
  exit 254
fi
exit "${remote_status}"
} </dev/null
REMOTE_TAIL
}

# ------------------------------------------------------------------------ 执行
printf '生产 .env 变更\n\n'
note "连接 ${SSH_ALIAS}（IAP 首包通常需 5-10 秒）"
note "白名单键  ${KEY}"
if [[ "${POLICY_DISPLAY}" == "plain" ]]; then
  note "目标值    ${SW_OPS_ENV_VALUE}"
else
  note "目标值    <不回显：凭据，红线 R5>"
fi

# 逐键的动手前警告。函数对未登记的键返回 1 → set -e 当场退出，"漏补一个键"不会静默通过。
sw_env_warn "${KEY}"
if [[ "${WRITE_ONLY}" -eq 1 ]]; then
  warn "--write-only：只改 .env，不重建容器。变更**不会生效**，直到有人重建 core。"
fi
printf '\n'

# ssh(1) 不保留 argv 边界（见 --show 分支上方那段说明）。所有位置参数都是本脚本自己定义
# 的常量或已校验过的键名/正则，注入面为零；**值不在其中**——它在 stdin 流里。
# stdin 用**进程替换**而不是管道喂：`… | ssh …` 在 set -o pipefail 下会让写端的 SIGPIPE
# 有机会顶掉 ssh 自己的退出码，而下面要按退出码分派远端协议。
#
# 【前言有两行，都在外层花括号外面】
#   sw_ops_emit_env_value_prologue  `export SW_ENV_SET_VALUE=<%q>`——要写进 .env 的值。
#   sw_ops_emit_token_prologue      `export SW_OPS_UI_TOKEN=<%q>`——real_publish 与
#                                   signing_secret 这两道闸门的探针要用；其余那几道只读 .env。
# 后者未配置时输出空赋值，远端两条路径的代码因此完全同构（与其余调用
# sw_ops_emit_token_prologue 的脚本同一口径，名单以
# `grep -l 'sw_ops_emit_token_prologue' scripts/ops/*.sh` 为准，结果里的 ui_token.sh 是
# 定义处、不算使用方）。
# 两行都只是 bash 内建 export：不 fork、不读 stdin，所以放在花括号外面是安全的——
# 要往前言里加别的东西，先读 scripts/ops/ui_token.sh 里那段警告。
env_set_remote() {
  ssh -o ConnectTimeout=25 "${SSH_ALIAS}" \
    "bash -s -- $(printf '%q ' "${KEY}" "${VALUE_RE}" "${POLICY_DISPLAY}" "${STAMP}" \
      "${ENV_DUPLICATE_STATUS}" "${ENV_BAD_KEY_STATUS}" "${ENV_BAD_VALUE_STATUS}" \
      "${ENV_BACKUP_STATUS}" "${ENV_WRITE_STATUS}" "${ENV_MISSING_STATUS}" \
      "${ENV_RECREATE_STATUS}" "${WRITE_ONLY}" \
      "${ACTIVE_GATE}" "${ENV_PRECHECK_GATE_STATUS}" "${ENV_PRECHECK_PROBE_STATUS}" \
      "${ENV_CARRIER_GATE_STATUS}" "${ENV_CARRIER_PROBE_STATUS}" \
      "${ENV_BACKEND_CREDS_STATUS}" "${ENV_WECHAT_CERT_STATUS}" \
      "${ENV_SIGNING_GATE_STATUS}" "${ENV_SIGNING_PROBE_STATUS}" "${ACCEPT_BREAKING}" \
      "${POLICY_SIGNING_ABOVE}")" \
    < <(sw_ops_emit_env_value_prologue; sw_ops_emit_token_prologue; env_set_remote_script)
}

# **不重试**。所有远端失败都是"状态可能已经变了"的失败：重跑一次 .env 编辑不是幂等的
# 观察动作，而是又一次生产写入 + 又一次容器重建。ssh 传输中断（255）同样不重试——
# 断链发生在写入之前还是之后，外层根本区分不了，而"再改一次生产 .env"不是可以自动做的事。
# 这与 backup.sh / restart.sh 的一次重试**刻意不同**：那两个的重试对象是只读或幂等动作。
remote_rc=0
env_set_remote || remote_rc=$?

# 措辞要在"备份了"和"没备份"两种情形下都成立：值未变化时远端刻意不备份（也没什么可回退的）。
# 真发生了备份时，远端自己已经打过一行 `.env 备份  <路径>（0600）`。
# 这一句按被改的是哪个键分支：改 SW_UI_TOKEN 时"根治"是去设签名密钥，而改签名密钥本身时
# 这条命令**就是**那个根治动作，再让人去跑一遍自己刚被拦下的命令是纯粹的绕圈。
# 【--generate 被闸门拦下之后，重跑的命令不是同一条——这一句非有不可】
# 取值路径是"先本地落盘、后推远端"（那个顺序是承重的，理由见上面凭据类键那一节）。
# 所以闸门拒绝时，新值**已经在** ~/.dsh-sw/.credentials.yaml 里了；再跑一次 --generate 会
# 撞上"凭据文件里已经有这个键，拒绝覆盖"，而那条报错不会提这里发生过什么。
if [[ "${TOKEN_SOURCE_MODE}" == "generate" ]]; then
  signing_retry_hint="注意重跑用的**不是同一条命令**：--generate 已经把新值写进 ${CRED_FILE} 的 ${POLICY_CRED_KEY} 键了（先本地落盘、后推远端是刻意的顺序），再跑一次 --generate 会被「已经有这个键，拒绝覆盖」挡住。条件满足之后请改用：bash scripts/ops/env_set.sh --key ${KEY} --from-credentials"
else
  signing_retry_hint="重跑用同一条命令即可：本次没有在本机留下任何新值。"
fi

# 判据是策略表第六格（这个键在回落链上有没有更高的级），不是键名：`none` 表示本键就是
# 第一级，那时"根治"就是本命令自己，再让人去跑一遍刚被拦下的命令是纯粹的绕圈。
if [[ "${POLICY_SIGNING_ABOVE}" != "none" ]]; then
  signing_root_fix_hint="处置二（根治，只做一次）：bash scripts/ops/env_set.sh --key ${POLICY_SIGNING_ABOVE%% *} --generate —— 显式设上签名密钥之后，回落链停在第一级，此后 ${KEY} 再怎么换都不会动它，这条耦合就永久解开了。注意那一条本身也走同一道闸门（它就是在改签名密钥），挑一个 0 条的时刻做。"
else
  signing_root_fix_hint="处置二：本命令**就是**那个根治动作——显式设上 SW_TELEGRAM_SIGNING_SECRET 之后，SW_UI_TOKEN 与 TELEGRAM_BOT_TOKEN 再怎么换都不会动签名密钥。它本身只能挑一个 0 条的时刻做，没有别的绕法。"
fi

env_backup_hint="回退用的 .env 备份：远端 ~/sw-env-backups/env-${STAMP}（0600）——上面远端输出里那行「.env 备份」就是它；值未变化时不会有备份，那种情形也没有需要回退的东西。"

case "${remote_rc}" in
  0) : ;;
  "${ENV_MISSING_STATUS}")
    die "远端 ~/social_workflow/.env 不存在，什么都没做" \
      "本脚本只改已有的 .env，不凭空造一个：生产 .env 里还有 LLM key 与 Telegram bot token，凭空造出来的那份会让 core 起不来。" \
      "先确认服务器上的部署目录是不是 ~/social_workflow（bash scripts/ops/status.sh 能看到 Compose 服务）。" ;;
  "${ENV_DUPLICATE_STATUS}")
    die "生产 .env 里 ${KEY} 出现了多次，本脚本拒绝猜哪一条生效，什么都没做" \
      "dotenv 语义下后一条覆盖前一条：只改第一条会写成功却不生效，是最难查的那类静默失败。" \
      "删掉多余的行是破坏性操作，交给人：请人自己编辑 .env 后重跑本脚本。" \
      "想先看现状：bash scripts/ops/env_set.sh --show（它会把重复次数打出来）。" ;;
  "${ENV_BAD_KEY_STATUS}"|"${ENV_BAD_VALUE_STATUS}")
    die "远端再校验拒绝了这次变更，什么都没做（键名或值的形状不合法）" \
      "本地与远端各有一道形状校验，这是纵深防御：走到这里说明两侧对同一个值的判断不一致。" \
      "具体是哪一格见上面远端的输出。凭据值在任何一侧都不会被回显。" ;;
  "${ENV_PRECHECK_GATE_STATUS}")
    die_with "${ENV_PRECHECK_GATE_STATUS}" "事前预防闸门拒绝了这次变更：人工确认闸门通道不可用，**生产 .env 一个字节都没动**" \
      "具体红在哪一格见上面远端的输出（enabled / ready / polling）。" \
      "这是**事前预防**，不是事后检测：没有备份、没有写入、没有重建容器，生产维持原状——真发布**没有**被打开。" \
      "后果如果放任不管：确认闸门等不到人的那一票，内容会被跳过不发（scheduler 记 skipped_unconfirmed），在排期处静默堆积，并在 SW_CONFIRM_TTL_HOURS（默认 24 小时）到点被自动驳回。" \
      "兜底：工作台的「确认发布」按钮不受 Telegram 影响，仍可用于确认（同一后端 core.confirm.confirm_item）——但那要求人知道主载体已经死了。" \
      "哪一格由什么决定：enabled / ready 看服务器 .env 里的 SW_TELEGRAM_*；polling 看正在跑的那份代码（core/main.py:104 的 lifespan 起线程，core/telegram.py:981 判活）。" \
      "取证：bash scripts/ops/verify.sh（会打印 enabled/configured/ready/chat_configured/polling/detail 全部字段与下一步指引）。" \
      "修好之后重跑本命令即可；本次没有任何需要回退的东西。" ;;
  "${ENV_PRECHECK_PROBE_STATUS}")
    die_with "${ENV_PRECHECK_PROBE_STATUS}" "事前预防闸门**探不到**人工确认闸门通道，按 fail-closed 拒绝写入，**生产 .env 一个字节都没动**" \
      "注意这与「探到了，通道不活」是两件事：那一种是状态确定的拒绝，这一种是「不知道」。上面远端的输出里写着具体是连不上、超时还是 401。" \
      "为什么不知道也拒绝：这个方向是把「什么都不会真发」翻成「真的会发出去」，在无法证明 R1 主载体活着的前提下打开它是不可接受的；而且探的是同机 loopback 上一个本来就该活着的服务，探不到本身就是信号。" \
      "也别指望放行会更顺：紧接着的 restart.sh 会用同一条探针再问一次，多半同样探不到，区别只是那时 .env 已经是 false、容器已经带着它重建过了。" \
      "401 那一格的处置：export SW_OPS_UI_TOKEN=<生产 .env 里 SW_UI_TOKEN 的同一个值>，或写进 ~/.dsh-sw/.credentials.yaml（0600）的 sw_ui_token 键。" \
      "其余情形的处置：先跑 bash scripts/ops/status.sh 与 bash scripts/ops/verify.sh 看 core 到底什么状态，修好之后重跑本命令。" ;;
  "${ENV_CARRIER_GATE_STATUS}")
    die_with "${ENV_CARRIER_GATE_STATUS}" "事前闸门拒绝了这次变更：真发布正开着，不能拆掉确认卡的推送载体，**生产 .env 一个字节都没动**" \
      "没有备份、没有写入、没有重建容器，Telegram **仍然开着**，生产维持原状。" \
      "因果写准：关掉 Telegram **不会**让内容越权发出去。人工确认闸门看的是 item.confirmed_at（core/scheduler.py:498-505），没人点就跳过不发（记 skipped_unconfirmed），R1 红线不因此失效。" \
      "真正的后果是静默停摆再静默丢弃：内容堆在排期处，TTL 在一次都没推成功过时从 scheduled_at 起算（core/confirm.py:571-573），到点（SW_CONFIRM_TTL_HOURS，默认 24 小时）自动驳回并释放槽位。" \
      "第二载体仍在：工作台「确认发布」不受 Telegram 影响，走同一个后端函数（core/api/content.py:283-297 → core/confirm.py:315 confirm_item）。" \
      "为什么是拒绝而不是警告：真发布开着 + enabled=false 这个组合会被 scripts/ops/restart.sh 的 R1 闸门必然判红，放行只会换来「改完生产之后再失败」。" \
      "出路（两步，中间那一步让生产更安全，不存在被锁死）：" \
      "    bash scripts/ops/env_set.sh --key SW_USE_FAKE_PUBLISHERS --value true" \
      "    bash scripts/ops/env_set.sh --key SW_TELEGRAM_ENABLED --value false" ;;
  "${ENV_CARRIER_PROBE_STATUS}")
    die_with "${ENV_CARRIER_PROBE_STATUS}" "事前闸门**读不出**真发布状态，按 fail-closed 拒绝写入，**生产 .env 一个字节都没动**" \
      "注意这与「真发布正开着」是两件事：那一种状态确定，这一种是「不知道」。上面远端的输出里写着是缺行还是值的写法不认识。" \
      "为什么不知道也拒绝：代码里的默认值确实是 true（core/config.py:53），但那是**部署里那份代码**的属性，从运维这一侧看不见；赌错的代价是改完生产之后被 restart.sh 的 R1 闸门判红。" \
      "出路：先把它显式写清楚再来（这条命令本身没有闸门，写成 true 只会让生产更安全）：" \
      "    bash scripts/ops/env_set.sh --key SW_USE_FAKE_PUBLISHERS --value true" \
      "想先看现状：bash scripts/ops/env_set.sh --show（白名单键一次全列）。" ;;
  "${ENV_BACKEND_CREDS_STATUS}")
    die_with "${ENV_BACKEND_CREDS_STATUS}" "事前闸门拒绝了这次后端切换：目标后端的凭据在生产 .env 里缺失或为空，**生产 .env 一个字节都没动**" \
      "具体缺的是哪个变量名见上面远端的输出（anthropic 要 ANTHROPIC_API_KEY；dsh 要哪个取决于 .env 里的 SW_DSH_PROVIDER，映射在 configs/dsh/cordis.yml）。" \
      "为什么这也要拦：generation/llm.py:271-278 是懒加载——缺 key 时 core 照常起来，直到第一次真出稿才抛 LLMUnavailable。一次没凭据的「回退」不会当场报错，它把故障推迟到排期里，比不回退更糟。" \
      "处置：先把那个凭据写进生产 .env 并确认非空，再重跑本命令。" \
      "**本脚本做不到那一步**：凭据类键刻意仍不在白名单上（要走 secret 策略与零回显流程，且改签名密钥有 R1 邻近副作用），docs/RISKS.md 第 14 条如实记着这个缺口。" ;;
  "${ENV_WECHAT_CERT_STATUS}")
    die_with "${ENV_WECHAT_CERT_STATUS}" "事前闸门拒绝了这次变更：WECHAT_CERTIFIED 不是 true，**生产 .env 一个字节都没动**" \
      "拦它不是因为危险，是因为没用：双确认闸门要 server_switch && account_certified && confirm_publish 三者皆真（publishers/wechat_mp/publisher.py:238-249），认证那一格是假时这次变更是个**不会生效的空操作**——内容照旧只落草稿箱，而人会以为自动发布已经开了。" \
      "同口径：scripts/preflight.py:122-133 对这一组合的门禁裁定同样是 FAIL。" \
      "账号确实已认证：把 WECHAT_CERTIFIED=true 写进生产 .env 后重跑本命令。**本脚本做不到**——它不在白名单上，docs/RISKS.md 第 14 条如实记着这个缺口。" \
      "账号还没认证：2025-07 起未认证主体的 freepublish 权限被回收（core/config.py:230），此时唯一合规路径就是继续只落草稿箱、由人在公众号后台点发表。" ;;
  "${ENV_SIGNING_GATE_STATUS}")
    die_with "${ENV_SIGNING_GATE_STATUS}" "事前闸门拒绝了这次变更：还有待人点的确认卡，不能换 Telegram 确认卡的签名密钥，**生产 .env 一个字节都没动**" \
      "具体多少条见上面远端的输出。没有备份、没有写入、没有重建容器，生产维持原状。" \
      "为什么这次写入会换掉签名密钥：core/telegram.py:151-154 的三级回落是 SW_TELEGRAM_SIGNING_SECRET → SW_UI_TOKEN → bot token，改掉生效的那一级就是改密钥。已推出去还没人点的卡按下去会验签失败（日志 bad_signature，用户侧表现为按钮没反应），最终被 TTL 自动驳回。" \
      "条数是**上界**不是精确值：counters.awaiting_confirm 不看 confirm_pushed_at，还没推出卡的条目也计在里面，那些卡是换密钥之后才生成的、签的是新密钥，不会失效。" \
      "处置一（等）：等这些卡被人点掉、或被 SW_CONFIRM_TTL_HOURS（默认 24 小时）到点自动驳回，再重跑本命令。" \
      "${signing_root_fix_hint}" \
      "处置三（明知故犯）：加 --accept-breaking-pending-confirm-cards 重跑。输出里会如实记下你接受了多少条会受影响。" \
      "${signing_retry_hint}" \
      "取证：bash scripts/ops/verify.sh —— 它的「待人点的确认卡」那一格与本闸门读的是同一个数（同一份 sw_awaiting_confirm，唯一定义处在 scripts/ops/ui_token.sh）。" ;;
  "${ENV_SIGNING_PROBE_STATUS}")
    die_with "${ENV_SIGNING_PROBE_STATUS}" "事前闸门**读不出**待人点的确认卡条数，按 fail-closed 拒绝换签名密钥，**生产 .env 一个字节都没动**" \
      "注意这与「还有待人点的卡」是两件事：那一种条数是确定的，这一种是「不知道」。上面远端的输出里写着具体是 401、404 还是别的。" \
      "为什么不知道也拒绝：这次写入会换掉生效的 HMAC 签名密钥，而这道闸门**没有事后那一道**——restart.sh 的 R1 闸门问的是确认通道活不活，它对「卡还能不能验签」一无所知，判绿也说明不了任何事。" \
      "401 那一格的处置：export SW_OPS_UI_TOKEN=<生产 .env 里 SW_UI_TOKEN 的同一个值>，或写进 ~/.dsh-sw/.credentials.yaml（0600）的 sw_ui_token 键。" \
      "404 那一格的含义：这版 core 还没有 /api/v1/dashboard，本闸门在它上面读不出任何东西。" \
      "其余情形：先跑 bash scripts/ops/status.sh 与 bash scripts/ops/verify.sh 看 core 到底什么状态。" \
      "明知故犯：加 --accept-breaking-pending-confirm-cards。它**刻意**也覆盖这一档——否则「两边 token 不一致」这种必然 401 的情形永远收敛不了，而 --from-credentials 正是那种不一致唯一的收敛动作。" \
      "${signing_retry_hint}" ;;
  "${ENV_BACKUP_STATUS}")
    die "备份生产 .env 失败，**没有动 .env**" \
      "本脚本的硬规则是备份先于写入：拿不到备份就绝不写。" \
      "先查远端 ~/sw-env-backups 的权限与磁盘水位（bash scripts/ops/status.sh 有磁盘水位一段）。" ;;
  "${ENV_WRITE_STATUS}")
    die "写入生产 .env 失败" \
      "写入是原子的（同目录临时文件 + mv），所以 .env 要么还是旧的完整内容、要么已是新的完整内容，不会是半截。" \
      "临时文件由 trap 清理；万一残留，它叫 ~/social_workflow/.env.sw-ops-tmp.<pid>，留着会让 verify.sh 判'工作树不干净'。" \
      "${env_backup_hint}" ;;
  "${ENV_RECREATE_STATUS}")
    die "生产 .env 已经改成新值，但容器重建失败——变更**没有生效**" \
      "这是一个半截状态：.env 是新的、运行中的 core 还是旧的。此后任何人一次寻常的 docker compose up -d 都会把新值带上来，而那一刻没有任何闸门在场。" \
      "先查 docker compose ps / docker compose logs core（bash scripts/ops/status.sh 一次能看到前两段）。" \
      "镜像缺失请走 bash scripts/ops/update.sh --apply：构建是它的职责，本脚本刻意带 --no-build。" \
      "要退回旧值：bash scripts/ops/env_set.sh --key ${KEY} ...（反向命令见 scripts/ops/README.md），或 ${env_backup_hint}" ;;
  254)
    die "远端脚本自身异常退出（已从 255 规范化成 254），生产状态不明" \
      "本脚本对任何远端失败都**不重试**：重跑一次 .env 编辑不是幂等的观察动作，而是又一次生产写入 + 又一次容器重建。" \
      "先跑 bash scripts/ops/env_set.sh --show 看 .env 现在到底是什么状态，再决定下一步。" \
      "${env_backup_hint}" ;;
  255)
    die "SSH 连接或传输中断（ssh 退出 255），生产状态不明" \
      "断链发生在写入之前还是之后，外层区分不了，所以**刻意不自动重试**——那等于在状态不明时又改一次生产。" \
      "先跑 bash scripts/ops/env_set.sh --show 看 .env 现在到底是什么状态，再决定下一步。" \
      "${env_backup_hint}" ;;
  *)
    die "远端以退出码 ${remote_rc} 失败，生产状态不明" \
      "先跑 bash scripts/ops/env_set.sh --show 看 .env 现在到底是什么状态。" \
      "${env_backup_hint}" ;;
esac

if [[ "${WRITE_ONLY}" -eq 1 ]]; then
  printf '\n'
  ok ".env 已变更（--write-only：未重建容器，变更尚未生效）"
  warn "运行中的 core 仍是旧值。注意 docker compose restart **不会**让 .env 变更生效——容器环境在创建时定型，必须重建（up -d）。"
  note "要让它生效：去掉 --write-only 重跑本脚本。"
  exit 0
fi

# ------------------------------------------------------- 重启与 R1 闸门（委托）
#
# 【为什么是调 restart.sh 而不是复用它的闸门函数】那道闸门不是一个可以被 source 的函数：
# 它整段内联在 restart.sh 的远端 heredoc 里（远端只有一条脚本流，没有别的文件可读），
# 与探针、退出码协议、告警文案长在一起。把它抽出来共享，等于对一个刚通过对抗性审查、
# 且已经在生产上跑过的脚本做结构性改动——收益是省掉一次容器 restart，代价是那道闸门本身
# 的风险。**这个交换不划算**，所以这里直接调它，让它按自己的契约跑完整条路。
#
# 【代价如实说：core 会被动两次】上面 `up -d --force-recreate` 已经重建过一次容器（这次
# 是让 .env 生效所必需的），restart.sh 还会再 `docker compose restart core` 一次。第二次
# 在功能上是多余的，它换来的是"闸门的实现只有一份、且是已经在生产上验证过的那一份"。
# core 的冷启动是秒级，而这本来就是一次有人盯着的停机变更。
note "调用 scripts/ops/restart.sh：它会备份数据库与台账、重启 core、探针确认，并走 R1 红线闸门"
printf '\n'
restart_rc=0
bash "${SCRIPT_DIR}/restart.sh" || restart_rc=$?

if [[ "${restart_rc}" -ne 0 ]]; then
  # fail-closed：闸门没过就以失败收尾，绝不打印成功行。
  if [[ "${GATE_REAL_PUBLISH}" -eq 1 ]]; then
    die "restart.sh 未通过（退出码 ${restart_rc}）：生产 .env 已经是 SW_USE_FAKE_PUBLISHERS=false，真发布**已经开启**" \
      "具体红在哪一格见上面 restart.sh 的输出。若红的是 R1 红线闸门，含义是：真发布开着，而人工确认闸门通道不可用。" \
      "后果不是内容会越权发出去——恰恰相反：确认闸门等不到人的那一票，内容会被跳过不发（scheduler 记 skipped_unconfirmed），在排期处静默堆积，并在 SW_CONFIRM_TTL_HOURS（默认 24 小时）到点被自动驳回。" \
      "兜底：工作台的「确认发布」按钮不受 Telegram 影响，仍可用于确认（同一后端 core.confirm.confirm_item）。" \
      "反向命令（本脚本刻意**不自动执行**任何恢复动作，与 update.sh 的 rollback_hint 同一契约）：" \
      "    bash scripts/ops/env_set.sh --key SW_USE_FAKE_PUBLISHERS --value true" \
      "取证：bash scripts/ops/verify.sh（十道门禁一次跑完，含确认通道全部字段）。" \
      "${env_backup_hint}"
  fi
  die "restart.sh 未通过（退出码 ${restart_rc}）：生产 .env 已经改成新值并已重建容器" \
    "具体红在哪一格见上面 restart.sh 的输出。常见的一格是探针 401——那说明 core 已经在应答，缺的是运维侧凭据。" \
    "取证：bash scripts/ops/verify.sh。" \
    "${env_backup_hint}"
fi

printf '\n'
ok "生产 .env 已变更、已生效，restart.sh 与 R1 闸门均已跑完"
if [[ "${GATE_REAL_PUBLISH}" -eq 1 ]]; then
  warn "真发布现在是开启状态。docs/RISKS.md 第 12 条从「潜在」转为「活跃」，请按第 9 条收尾：跑一次 bash scripts/ops/verify.sh 做完整取证。"
fi
if [[ "${KEY}" == "SW_TELEGRAM_SIGNING_SECRET" ]]; then
  ok "签名密钥现在**显式落在这个键上**：core/telegram.py:151-154 的三级回落停在第一级。"
  note "这一步的价值就是**解耦**：此后 SW_UI_TOKEN 再怎么换（换鉴权、疑似泄漏、例行轮换）都不会动 Telegram 确认卡的签名密钥，已推出去的卡不会因为换 token 而失效。"
  note "本脚本的签名密钥闸门也随之只在改这个键时才拦你——改 SW_UI_TOKEN 会走「已显式设置且非空，放行」那一格。"
  note "值在 ${CRED_FILE} 的 ${POLICY_CRED_KEY} 键里，人要用的时候自己去读——它不会被打印，也不该被念出来（红线 R5）。"
  warn "此刻起已推出去、还没人点的确认卡（如果有）签的是**旧**密钥。上面闸门那几行写着本次实际的条数口径。"
fi
if [[ "${KEY}" == "TELEGRAM_BOT_TOKEN" ]]; then
  ok "新 bot token 已生效：容器已按新 .env 重建，长轮询线程是带着新 token 起来的。"
  warn "**旧 token 已经作废**（BotFather 一签发新的就作废旧的）。此刻起用旧 token 发出去的一切都会被 Telegram 拒。"
  warn "polling 那一格在这个键上不可信：core/telegram.py:978-988 的 channel_status 只看线程活没活，而 core/telegram.py:810-831 的 _loop 拿到坏 token 也会一直退避重试、线程一直活着。"
  note "所以取证要看**别的**：bash scripts/ops/verify.sh —— 看确认通道那一段的 last_error 与 stats（errors 在涨就是 token 不对），以及「Telegram 轮询冲突（error_code=409）」那一格。"
  note "409 那一格的含义：**同一个 token 被两个部署同时轮询**时 Telegram 只喂一个，另一个持续 409（docs/RISKS.md 第 1 条）。本脚本刻意不在它上面设闸门——它数的是近 2000 行日志里的计数，旧账会让它假红、新冲突要等下一次轮询失败才写得进日志会让它假绿，既漏又误。"
  note "真撞上 409：处置是让另一个部署停下来、或给它换一个 bot，**不是**再换一次 token（再换一次只会把冲突原样搬到新 token 上）。"
  note "值在 ${CRED_FILE} 的 ${POLICY_CRED_KEY} 键里，人要用的时候自己去读——它不会被打印，也不该被念出来（红线 R5）。"
fi
if [[ "${KEY}" == "SW_UI_TOKEN" ]]; then
  note "此后所有经 IAP 隧道访问 /api/v1/* 的一方都要带同一个值：工作台前端、scripts/workbench_mcp.py 对话台（读 SW_UI_TOKEN 环境变量）、以及所有 source 了 scripts/ops/ui_token.sh 的脚本（读 SW_OPS_UI_TOKEN 或凭据文件；名单不写死在这里，当场跑 grep -l 'ui_token.sh' scripts/ops/*.sh scripts/*.sh 就有）。"
  note "值在 ${CRED_FILE} 的 ${POLICY_CRED_KEY} 键里，人要用的时候自己去读——它不会被打印，也不该被念出来（红线 R5）。"
fi
