#!/usr/bin/env bash
# shellcheck shell=bash
# 用途：工作台 API token（core 的 `SW_UI_TOKEN`）在**值班工作站侧**的取用与注入。
#       被若干脚本以 `source` 引用，不单独执行。**sourcer 名单以
#       `grep -l 'ui_token\.sh' scripts/ops/*.sh scripts/*.sh` 为准**——这里刻意不枚举、
#       也不写总数：名单仍在增长，
#       复述一份就多一处要跟着改、且没人钉着的真相。
#
# 【为什么需要它】core/api/common.py::require_token：`SW_UI_TOKEN` 只要是非空字符串，
# 除 `/auth/login` 外的全部 `/api/v1/*` 一律要求 `Authorization: Bearer <token>`，缺头 /
# 非 Bearer 格式 / 值不匹配都返回 401；`/health` 注册在 app 根上，不过这道守卫。
# 运维脚本探 `/api/v1/system/info` 与 `/api/v1/system/telegram`，所以生产一旦启用 token，
# 不带头的探针会全部 401 —— 这正是 docs/RISKS.md §8.4 记录的实施前置。
#
# 【两个名字不要混】
#   SW_UI_TOKEN      服务端配置项，写在生产 `.env` 里，core 自己读。
#   SW_OPS_UI_TOKEN  运维侧持有的同一个值，只存在于值班工作站与远端 shell 的进程环境里。
# 刻意用不同的名字：叫同一个名会让人以为"在本机 export 一下 core 就生效了"。
#
# 【红线 R5 + argv 零暴露】token 不进仓库、不进日志、不进任何进程的 argv。
# 生产是合租机器（docs/RISKS.md §8.2 列了同机不相关的同租户容器），
# `/proc/*/cmdline` 对同机其他用户可读，argv 泄漏是现实威胁而不是理论威胁。因此：
#   本地取用  只从环境变量或 `~/.dsh-sw/.credentials.yaml`（0600）读，绝不从仓库读。
#   本地→远端 由 sw_ops_emit_token_prologue 用 **bash 内建** printf 写进送给远端 bash 的
#             脚本流（即 ssh 的 stdin）。内建命令不 fork，不产生自己的 /proc/*/cmdline。
#             ssh 的 argv 里只有 `bash -s -- <已校验的位置参数>`，没有 token。
#   远端→curl 远端脚本把 token 写进 `curl --config -` 的配置流（同样是内建 printf + 管道），
#             curl 的 argv 里只有 `--config -`。探针的 curl 以 `-q` **打头**：
#             不加它时，远端家目录里一个带 `verbose` / `trace-ascii <file>` 的 `~/.curlrc`
#             就能把 `Authorization: Bearer <token>` 明文写进磁盘文件（本机实测复现）。
#             `-q` 同时挡掉 `.curlrc` 改超时、加 `-o`、加 `--fail-with-body` 这类行为不
#             确定性——探针的行为应当完全由脚本决定。`-q` 必须是第一个参数才生效。
#   落盘      零落盘（以 `-q` 为前提，见上）：远端只把它放进进程环境。
#             `/proc/<pid>/environ` 只有属主与 root 可读，与世界可读的 `cmdline` 不是一回事；
#             这是本方案唯一的同机暴露面，与"运维用户自己的 ~/.env 可读"同级，
#             不引入新的跨用户暴露。
#   xtrace    经手 token 的那几个函数在入口关掉 xtrace、出口恢复（见 sw_ops_xtrace_guard；
#             名单就是本文件里所有经 sw_ops_xtrace_guard 包一层的函数，不另外记一份）。
#             不这么做时 `bash -x scripts/ops/verify.sh 2>&1 | tee /tmp/x.log` 会把 token
#             明文打出来五次——而"401 老是修不好就开 -x 看看、再把输出贴进工单"是最自然的
#             排查动作，直接踩红线 R5 的"凭据不进对话"。

# 允许的字符集：A-Za-z0-9 与 . _ - + / = : @
# **这是白名单：不在集合里的字符一律拒绝**，不是"只拒绝某几类坏字符"。报错文案必须照这个
# 口径写——列一份"不允许"清单会让 token 里带 `%` 的人对着清单挨个排除、得出"我这个应该合法"
# 的结论，然后卡在一条自己看不懂的报错上。
#
# 为什么定得这么窄，分层说清（每一条都本机实测过，别把它们混成一句）：
#   ① `"` 与 `\` —— **硬性的**。token 是通过 curl 配置流里的
#      `header = "Authorization: Bearer <token>"` 注入的，curl 解析双引号值时把 `\` 当转义、
#      把 `"` 当定界符（tool_parsecfg.c 的 unslashquote）。实测：裸 `"` 让值在那里静默截断，
#      裸 `\` 被直接吃掉——发出去的头不是你以为的那一个，而且没有任何报错。
#   ② 空白与控制字符 —— **理由不是 curl 会截断**。实测空格在双引号参数里能原样通过
#      （`header = "Authorization: Bearer AB CD"` 真的发出 `Bearer AB CD`）。排除它们是因为
#      它们不是合法的凭据字符：RFC 6750 的 b64token 只允许 ALPHA/DIGIT/`-._~+/` 加末尾 `=`，
#      带空白的 Bearer 凭据会让 "Bearer " 之后的解析变得含混，服务端行为不可依赖。
#   ③ `~` —— `printf '%q'` 不引用它，而远端 `export X=~abc` 会触发波浪号展开（本机实测）。
#   ④ 其余（`# % $ * ! , ; ( ) [ ]` 等）—— curl 其实**能**安全携带它们。排除是**安全侧的
#      主动收紧**：宁可拒绝一个本来能用的 token，也不要在一条很难查的路径上出错。代价要如实
#      承认：`Django get_random_secret_key()` 与密码管理器"带符号"输出这类会被拒。
# 常见生成方式都落在白名单内：`openssl rand -base64 32`（A-Za-z0-9+/=）、
# `python3 -c 'import secrets;print(secrets.token_urlsafe(32))'`（A-Za-z0-9_-）、hex、
# uuid、JWT（三段 base64url 用 `.` 连接）。
SW_OPS_UI_TOKEN_ALLOWED_RE='^[A-Za-z0-9._+/=:@-]+$'
SW_OPS_UI_TOKEN_ALLOWED_TEXT='A-Z a-z 0-9 以及 . _ - + / = : @'

# 凭据文件 `~/.dsh-sw/.credentials.yaml` 里存放各个凭据的那些键分别叫什么。
# **键名是参数、不是硬编码**：下面三个凭据文件函数（读 / 判存在 / 写）都按键名参数化，
# 好让 scripts/ops/env_set.sh 里的凭据类键复用同一套"值不进 argv、不回显、写进凭据文件"
# 的流程。上一批把函数参数化，本批用它加进了第二个键。
# 这几个常量只回答"某个键在凭据文件里叫什么"，**不是任何函数的默认值**：三个函数都要求
# 调用方显式传键名、不给默认，免得将来某个新键忘了传参时静默地去读 / 写别人那一行。
#
# 【为什么每个 .env 键在凭据文件里另起一个名字，而不是照抄 .env 的键名】凭据文件的键名会被
# 原样拼进两条正则并写回文件（见 _sw_ops_credentials_key_guard），所以它有自己的字符集
# 约束；而且这份文件是**值班工作站**的东西，不是生产 .env 的镜像。两边刻意各叫各的。
SW_OPS_CREDENTIALS_UI_TOKEN_KEY='sw_ui_token'
# 生产 .env 的 SW_TELEGRAM_SIGNING_SECRET（Telegram 确认卡 callback_data 的 HMAC 签名密钥，
# core/telegram.py:151-154 三级回落里的**第一级**）在凭据文件里的键名。
# shellcheck disable=SC2034
# 本文件自己一次都不读它——它由 sourcer（scripts/ops/env_set.sh 的 sw_env_policy 那张表）取用，
# 单独扫 ui_token.sh 时 shellcheck 看不到那个使用点。上面那个 UI token 的常量不触发同一条
# 告警，只是因为本文件里恰好还有一处自用（_sw_ops_load_ui_token_impl），不是别的差别。
SW_OPS_CREDENTIALS_TELEGRAM_SIGNING_SECRET_KEY='sw_telegram_signing_secret'
# 生产 .env 的 TELEGRAM_BOT_TOKEN（core/telegram.py:151-154 三级回落里的**第三级**，同时也是
# 整条 Telegram 推送载体的身份）在凭据文件里的键名。
# 【它与上面两个的差别，改这一行之前先看清】上面两个的值是**本机 CSPRNG 造出来的**
# （sw_ops_generate_credential，走 --generate）；这一个的值由 **BotFather 签发**，本机造不
# 出来，所以 scripts/ops/env_set.sh 对它**明确拒绝** --generate（判据是那张策略表的第五格
# POLICY_CRED_ORIGIN=external-issuer，不是硬写的键名）。人从 BotFather 拿到之后自己写进
# 这个键，再走 --from-credentials 推上去。
# shellcheck disable=SC2034
# 与上一个常量同理：本文件自己一次都不读它，取用方是 env_set.sh 的 sw_env_policy 那张表。
SW_OPS_CREDENTIALS_TELEGRAM_BOT_TOKEN_KEY='telegram_bot_token'

# 生产 .env 的 DEEPSEEK_API_KEY（dsh 后端打模型网关用的那把）在凭据文件里的键名。
# 【它与上面三个的差别】上面两个是本机 CSPRNG 造出来的，bot token 由 BotFather 签发；
# 这一个由**模型网关**签发，同样本机造不出来，所以 POLICY_CRED_ORIGIN 也是 external-issuer，
# --generate 会被拒绝。人从网关控制台拿到之后自己写进这个键，再走 --from-credentials 推上去。
# 它也是第一个**不在签名密钥回落链上**的凭据类键：改它动不到任何一张确认卡，
# 所以它的闸门不是 signing_secret，而是自己那道 llm_key_live（写之前先问网关认不认这把 key）。
# shellcheck disable=SC2034
# 与上两个常量同理：本文件自己一次都不读它，取用方是 env_set.sh 的 sw_env_policy 那张表。
SW_OPS_CREDENTIALS_DEEPSEEK_API_KEY_KEY='deepseek_api_key'

# 取用结果。SOURCE 为空表示"本次不带 token"（保持改造前的未鉴权行为）。
SW_OPS_UI_TOKEN_VALUE=""
SW_OPS_UI_TOKEN_SOURCE=""

# xtrace 防泄漏守卫。
#
# 【为什么必须有】`set -x` 会把每条被执行的命令连同**展开后的参数**打到 stderr。经手 token
# 的那几行因此会变成：
#     + value=<token>
#     + [[ -n <token> ]]
#     + [[ <token> =~ ^[A-Za-z0-9._+/=:@-]+$ ]]
#     + SW_OPS_UI_TOKEN_VALUE=<token>
#     + printf 'export SW_OPS_UI_TOKEN=%q\n' <token>
# 一次运行五行明文。而"401 老是修不好 → bash -x scripts/ops/verify.sh 2>&1 | tee /tmp/x.log
# → 把这段贴进工单求助"是最自然的排查动作，直接踩红线 R5 的「凭据不进对话」。
#
# 【为什么不用 `local -`】那是 bash 4.4+ 的写法，而值班工作站是 macOS 自带的 bash 3.2.57。
# 这里用 `case "$-"` 存档 / 恢复，3.2 与 5.x 都成立。
#
# 【为什么是包装器而不是每个函数内联】被保护的函数有多个 return 出口，内联"出口恢复"很容易
# 漏掉一条；包装器只有一个出口，漏不掉。
#
# 【已知边界，如实写明】token 非法时 die() 在 xtrace 关闭状态下退出，那几行错误输出不会被
# 追踪——这是刻意的：错误文案本身不含 token，而重新打开 xtrace 只为追踪一句 printf 不值得
# 冒再泄漏一次的风险。远端不受影响：远端脚本没有 `-x`，xtrace 也不跨 ssh 传递。
sw_ops_xtrace_guard() {
  # 用法：sw_ops_xtrace_guard <实现函数名> [参数...]
  local sw_x=0 sw_rc=0
  case "$-" in *x*) sw_x=1; set +x ;; esac
  "$@" || sw_rc=$?
  [[ "${sw_x}" -eq 0 ]] || set -x
  return "${sw_rc}"
}

# 凭据文件键名的字符集守卫。**这是键名参数化之后新增的唯一一道硬约束，改它之前把理由看完。**
#
# 【为什么非有不可】键名会被**原样拼进两条正则**：`grep -m1 -E "^<key>:"`，以及 bash 内建的
# `[[ "${line}" =~ ^<key>:[[:space:]]*(.*)$ ]]`。键名里只要出现一个正则元字符
# （`.` `*` `+` `?` `[` `]` `(` `)` `|` `^` `$` `\` 等），匹配到的就不再是那个键：
# `sw.token` 会匹配上 `sw-token:` 与 `sw_token:`；`a|b` 会被拆成 `^a` 与 `b:` 两支。
# 而这类错配是**静默**的——读回来的是别的键的值，判存在会对着别的键点头，而"只追加、不改写"
# 的写入正是靠那个判存在来保证不覆盖已有凭据的。
#
# 【为什么选"校验并 die"而不是"转义"】两层理由：
#   ① 转义要同时对付两套方言（grep 的 ERE 与 bash 内建正则），两边都没有可移植的
#      `\Q...\E`；转义器本身就是一处新的、很难测全的正确性面。
#   ② 更要紧的是：正则安全只是问题的一半。键名还要能被 `printf '%s: %s\n'` 原样写回文件、
#      并被同一套极窄解析读回来。键名里带 `:`、空白、换行、`#`、引号都会让这个往返破掉，
#      而转义一个字也拦不住它。一份字符集白名单一次性把两件事都钉死。
# 代价如实说：将来真需要带 `-` 或 `.` 的键名时，要回到这里改（并重新论证往返安全），
# 而不是在调用点绕过去。
#
# 【为什么是 die 而不是 return 1】传进来的键名是脚本自己写的字面量，不是外来数据；违例是
# **编码错误**，不是"这个键没配"。悄悄回落成后者，会把一个拼错的键名伪装成"本机没有 token"，
# 然后在生产上以一个谁也查不到根因的 401 收场。
# 调用方必须已定义 die()——与本文件其余部分同一条前置契约。
#
# 【为什么不套 sw_ops_xtrace_guard】它只经手**键名**，不经手任何值；键名不是凭据，拼进报错
# 文案也是安全的。而且它的每一个调用点本身都已经在另一层守卫里面了。
_sw_ops_credentials_key_guard() {
  [[ "${1-}" =~ ^[A-Za-z0-9_]+$ ]] || die \
    "凭据文件键名不合法：${1:-<空或未传>}" \
    "只允许 A-Z a-z 0-9 与下划线，且不得为空。键名会被拼进 grep -E '^<key>:' 与 bash 的 [[ =~ ]]，正则元字符会让它静默地匹配到别的键；键名还要能被原样写回凭据文件、并被同一套极窄解析读回来。" \
    "这是编码错误、不是配置问题：改调用点传进来的键名，别在守卫里放宽。"
}

# 从凭据文件里取**指定键**的值。用法：<文件> <键名>，两个参数都必传。
# **极窄解析，刻意不是 YAML 解析器**：只认顶格（无缩进）的
# `<key>:` 键，值可以裸写，也可以用一对单引号或双引号包起来。不支持嵌套、锚点、
# 折叠标量、多文档、行内注释。
# 【本轮只把键名参数化，解析语义一字未改】上面那些"不支持"一条都没松，取值的每一步也没动；
# 变的只是"要找哪个键"从哪儿来。键名先过 _sw_ops_credentials_key_guard，理由见其上方。
# 取舍写明：本仓不给 shell 脚本引入 YAML 依赖（为了读一个键去背一个解析器不划算），
# 所以宁可"看不懂就当没配"，也不猜。取不到时脚本回落到未鉴权路径——生产没开 token 时
# 照常成功，开了 token 时会拿到 401 并打印可行动提示，两条路都不会把人引向错误结论。
_sw_ops_read_credentials_key_impl() {
  local file="$1" key="${2-}" line value
  _sw_ops_credentials_key_guard "${key}"
  line="$(grep -m1 -E "^${key}:" "${file}" 2>/dev/null)" || return 1
  [[ -n "${line}" ]] || return 1
  [[ "${line}" =~ ^${key}:[[:space:]]*(.*)$ ]] || return 1
  value="${BASH_REMATCH[1]}"
  # 去尾随空白（含 CR，兼容 CRLF 文件）；前导空白已被上面的正则吃掉。
  while [[ "${value}" == *[[:space:]] ]]; do value="${value%?}"; done
  case "${value}" in
    \"*\") value="${value#\"}"; value="${value%\"}" ;;
    \'*\') value="${value#\'}"; value="${value%\'}" ;;
  esac
  [[ -n "${value}" ]] || return 1
  printf '%s' "${value}"
}
sw_ops_read_credentials_key() { sw_ops_xtrace_guard _sw_ops_read_credentials_key_impl "$@"; }

# 取用 token 并校验字符集。**调用方必须已定义 die()**——这是 source 本文件的前置契约，
# 不满足时会在报错路径上炸成 command not found，而那正是最需要它说话的时候。
# sourcer 名单以 `grep -l 'ui_token\.sh' scripts/ops/*.sh scripts/*.sh` 为准；当前每一个
# 都在文件头、`source` 那一行之前定义了 die()。
# 优先级：环境变量 SW_OPS_UI_TOKEN > ~/.dsh-sw/.credentials.yaml 的 sw_ui_token 键。
# 理由：环境变量是"这一次调用"的显式意图（换 token、做对照实验最方便），凭据文件是长期
# 约定；显式优先于约定。**只要已导出就采信，哪怕是空串**——空串表示"本次显式不带 token"，
# 用来复现未鉴权路径；未导出才去读文件。若空串也静默回落到文件，"我明明清空了却还是带上
# 了"会变成一个没人查得清的谜题。
#
# 【那条优先级是 UI token **专有**的，别把它泛化】凭据文件的读 / 判存在 / 写已经按键名
# 参数化了，但"先看环境变量"这一层没有：`SW_OPS_UI_TOKEN` 是为 UI token 单独约定的一个
# 名字，新的凭据类键（首个是 SW_TELEGRAM_SIGNING_SECRET）**没有**对应的环境变量。所以本
# 函数只服务 UI token、保留原名；将来要给新键做取用，请直接调
# _sw_ops_read_credentials_key_impl，别把这个环境变量分支一并抄过去。
_sw_ops_load_ui_token_impl() {
  local cred_file="${HOME}/.dsh-sw/.credentials.yaml"
  local key="${SW_OPS_CREDENTIALS_UI_TOKEN_KEY}"
  local value="" source=""

  SW_OPS_UI_TOKEN_VALUE=""
  SW_OPS_UI_TOKEN_SOURCE=""

  if [[ -n "${SW_OPS_UI_TOKEN+x}" ]]; then
    value="${SW_OPS_UI_TOKEN}"
    source="环境变量 SW_OPS_UI_TOKEN"
  elif [[ -r "${cred_file}" ]]; then
    # 键名守卫在这里**再走一遍，这不是冗余**：下一行是命令替换，子 shell 里的 die 只打死
    # 子 shell；而 `if value="$( ... )"` 这个位置又把 set -e 关了，于是一个非法键名会退化成
    # "这个键没配"继续往下跑。先在父 shell 里校一遍，违例才会真的中止整个脚本。
    _sw_ops_credentials_key_guard "${key}"
    if value="$(_sw_ops_read_credentials_key_impl "${cred_file}" "${key}")"; then
      source="${cred_file} 的 ${key} 键"
    else
      value=""
    fi
  fi

  [[ -n "${value}" ]] || return 0

  # 报错文本里绝不出现 token 本身（红线 R5：凭据不进日志、不进对话）。
  [[ "${value}" =~ ${SW_OPS_UI_TOKEN_ALLOWED_RE} ]] || die \
    "工作台 API token 含有不被允许的字符（来源：${source}；此处不回显 token 值）" \
    "这是**白名单**：只允许 ${SW_OPS_UI_TOKEN_ALLOWED_TEXT}；**其余字符一律拒绝**（含引号、反斜杠、空白、控制字符、~ 以及 # % $ * ! , ; ( ) [ ] 等）。" \
    "为什么这么窄，分三层：① \" 与 \\ 是硬性的——token 经 curl 配置流的 header = \"Authorization: Bearer <token>\" 注入，curl 在双引号值里把 \\ 当转义、把 \" 当定界符，带上它们会**静默**发出一个语法被破坏的头；② 空白与控制字符不是合法的凭据字符（RFC 6750 的 b64token 不含它们），注意 curl 本身其实能原样带过空格，所以别按\"curl 会截断\"去理解；③ 其余字符 curl 能安全携带，排除它们是安全侧的主动收紧。" \
    "代价如实说：Django get_random_secret_key() 与密码管理器\"带符号\"输出这类会被拒。" \
    "处置：换一个白名单内的值——openssl rand -base64 32、python3 -c 'import secrets;print(secrets.token_urlsafe(32))'、hex、uuid、JWT 都可以，并同步改生产 .env 里的 SW_UI_TOKEN。" \
    "详见 scripts/ops/README.md「工作台 API token」一节。"

  SW_OPS_UI_TOKEN_VALUE="${value}"
  SW_OPS_UI_TOKEN_SOURCE="${source}"
}
sw_ops_load_ui_token() { sw_ops_xtrace_guard _sw_ops_load_ui_token_impl; }

# 只报来源，绝不报值，也不报长度（长度也是信息）。未配置时一个字都不打——
# 未配置路径的输出必须与改造前逐字一致。调用方必须已定义 note()。
sw_ops_note_ui_token() {
  [[ -n "${SW_OPS_UI_TOKEN_SOURCE}" ]] || return 0
  note "已加载工作台 API token（来源：${SW_OPS_UI_TOKEN_SOURCE}）；探针会带 Authorization: Bearer，token 值不打印、不进 argv"
}

# 生成送给远端 bash 的脚本流的**第一行**。printf 是 bash 内建，参数不会产生
# /proc/*/cmdline 条目；`%q` 保证远端 shell 重新解析时拿回逐字符相同的值（空值产出 ''）。
# 未配置时照样输出一个空赋值：远端 `set -u` 有定义可读，且两条路径的远端代码完全同构。
#
# ！！【这里只允许放不读 stdin 的内建命令】这一行在远端正文那对花括号的**外面**：远端 bash
# 先执行它，此时脚本剩余正文（含整个 `{ ... }` 组）**还在输入流里没被解析**。也就是说
# `{ ... } </dev/null` 那层结构性保证**对本前言不生效**。
# 今天安全，因为 `export` 是 bash 内建：不 fork、不读 stdin。但将来谁往前言里加一条会 fork
# 且读 stdin 的命令（`cat`、`docker …`、`ssh …`、任何管道右端），它就会把整段远端正文吞掉，
# 而脚本照样以 0 收尾——历史缺陷原样重现，且**没有任何现有测试会红**（测试盯的是花括号
# 里面那一层）。要加东西请先把它挪进花括号内，或者给前言也套一层保护并补上对应的测试。
_sw_ops_emit_token_prologue_impl() {
  printf 'export SW_OPS_UI_TOKEN=%q\n' "${SW_OPS_UI_TOKEN_VALUE}"
}
sw_ops_emit_token_prologue() { sw_ops_xtrace_guard _sw_ops_emit_token_prologue_impl; }

# =============================================================================
# 远端 `sw_probe` 的**唯一定义处**（单一真相源）
# =============================================================================
#
# 【为什么它长在这里而不是各脚本里】`sw_probe` 必须在**远端**执行，而远端只有一条脚本流、
# 没有别的文件可以 source——本机的函数定义带不过去。此前的做法是在 verify.sh / update.sh /
# restart.sh / status.sh 的远端 heredoc 里各内联一份**逐字相同**的拷贝，靠
# tests/ops/test_update.sh 的一条源码级断言逐字比对四份来维持"改一处要改四处"。
# 那条断言拦得住漂移，但拦不住"第五个调用方来了怎么办"——正确答案从来就是把它收成一份、
# 由本函数把定义**发射进远端脚本流**，而不是把 4 改成 5。env_set.sh 的事前预防闸门
# （docs/RISKS.md §12.3）就是那第五个调用方。
#
# 【发射位置：必须在远端正文那对花括号的**里面**，与 sw_ops_emit_token_prologue 相反】
# 这一点是本函数与上面两个 `*_prologue` 的关键差别，写在最显眼处免得被照着抄错：
#   · `sw_ops_emit_token_prologue` 发射的是脚本流的**第一行**，落在 `{` 之前——它因此
#     享受不到 `{ ... } </dev/null` 的保护，所以那里只许放不 fork、不读 stdin 的内建命令。
#   · 本函数发射的是**远端正文的一部分**，调用方必须把它拼在内层 `{` 之后、`} </dev/null`
#     之前。落在组内，它就和它替换掉的那四份内联拷贝处在**完全相同**的位置上。
# 调用方形如：
#     x_remote_script() {
#       cat <<'REMOTE_HEAD'
#       ... 直到内层 `{` 与前面若干行 ...
#     REMOTE_HEAD
#       sw_ops_emit_sw_probe_definition
#       cat <<'REMOTE_TAIL'
#       ... 正文其余部分，含 `} </dev/null` ...
#     REMOTE_TAIL
#     }
# 拼出来的字节流与"整段写成一个 heredoc"逐字等价——远端 bash 只看到 fd 0 上的一串字节，
# 它是由一次 `cat` 还是三次拼出来的，对它不可见。tests/ops/test_update.sh 的
# 「remote stream carries sw_probe inside the brace group」用例直接拿被捕获的真实流验这一点。
#
# 【`{ ... } </dev/null` 那道结构性保证为什么仍然成立——重新论证，不是引用旧结论】
#   ① 保证的主语是**远端** bash 读到的那串字节，不是本机怎么产生它的。本机侧的变化
#      （一个 heredoc 变成"heredoc + 本函数 + heredoc"）全部发生在进程替换 `<(...)` 的
#      子 shell 里，写端；远端读到的字节序列一字未变，`{` 与 `} </dev/null` 还在原位。
#   ② 远端 bash 仍然必须把 `{ ... }` 这条复合命令**整条解析完**才能开始执行它——这是
#      shell 语法层的要求，与字节从哪儿来无关。于是正文在第一条命令跑起来之前就已经离开
#      输入流，谁也吞不掉它。
#   ③ 组重定向 `</dev/null` 依然挂在整个组上，组内每条命令及其子进程继承的 fd 0 仍是
#      /dev/null。
#   ④ 唯一新增的面是**本机侧**：本函数会不会把本机脚本自己的 stdin 吃掉？不会——它只有
#      一条 `cat <<'SW_PROBE'`，stdin 被 heredoc 覆盖，`cat` 读的是那个 heredoc 而不是
#      继承来的 fd 0。这与调用方原本那条 `cat <<'REMOTE'` 是同一形态、同一保证。
#      要往本函数里加东西，请守住这一条：**每条命令的 stdin 必须是它自带的显式来源。**
#
# 【为什么不套 sw_ops_xtrace_guard】本函数不经手任何凭据值：下面那是**引号 heredoc**，
# `${SW_OPS_UI_TOKEN}` 只是原样发给远端的一串字面字符，本机一次展开都不会发生。`bash -x` 也
# 不追踪 heredoc 正文，只会多打一行 `+ cat`。刻意不加守卫，免得下一个人以为这里有值经手。
#
# 【内容与被它替换掉的四份内联拷贝逐字相同】判定语义一字未改：仍然是 `-f`（HTTP >= 400 退
# 22）、URL 与超时由调用方给、`-w` 把状态码追加到响应体末尾以便把 401 与"连不上/超时/5xx"
# 分开。改这段等于同时改所有发射方的远端脚本（取用方以
# `grep -l 'sw_ops_emit_sw_probe_definition' scripts/ops/*.sh` 为准，本文件是定义处、不算），
# 改之前先读上面两块说明。
_sw_ops_emit_sw_probe_definition_impl() {
  cat <<'SW_PROBE'
sw_probe_code=''
sw_probe_body=''
sw_probe_curl_config() {
  [[ -n "${SW_OPS_UI_TOKEN:-}" ]] || return 0
  printf 'header = "Authorization: Bearer %s"\n' "${SW_OPS_UI_TOKEN}"
}
sw_probe() {
  local url="$1" max_time="$2" raw status
  status=0
  # `-q` 必须是第一个参数：它让 curl 忽略 ~/.curlrc。不加时，家目录里一个带
  # `verbose` / `trace-ascii <file>` 的 .curlrc 就能把 Authorization 头明文写进磁盘文件
  # （本机实测复现），"零落盘"这条断言会当场破功；它同时挡掉 .curlrc 改超时 / 加 -o /
  # 加 --fail-with-body 这类行为不确定性——探针的行为必须完全由本脚本决定。
  raw="$(sw_probe_curl_config | curl -q -fsS --max-time "${max_time}" -w '\n%{http_code}' --config - "${url}")" || status=$?
  if [[ "${raw}" == *$'\n'* ]]; then
    sw_probe_code="${raw##*$'\n'}"
    sw_probe_body="${raw%$'\n'*}"
  else
    sw_probe_code="${raw}"
    sw_probe_body=''
  fi
  [[ "${sw_probe_code}" =~ ^[0-9]{3}$ ]] || sw_probe_code='000'
  return "${status}"
}
SW_PROBE
}
sw_ops_emit_sw_probe_definition() { _sw_ops_emit_sw_probe_definition_impl; }

# =============================================================================
# 远端 `sw_awaiting_confirm`（待人点的确认卡条数）的**唯一定义处**
# =============================================================================
#
# 【它回答的是哪一问】docs/RISKS.md §8.5 给"换签名密钥"定的第 0 步前置：**现在有没有已经
# 推出去、还没人点的确认卡**。生产 .env 里 SW_TELEGRAM_SIGNING_SECRET 为空时，确认卡
# callback_data 的 HMAC 签名密钥按 专用 → SW_UI_TOKEN → bot token 回落
# （core/telegram.py:151-154），换掉生效的那一级等于换掉签名密钥，已推出去的卡按下去会
# 验签失败（日志 bad_signature，用户侧表现为按钮没反应），最终被 TTL 自动驳回。
#
# 【为什么长在这里，而不是在 verify.sh 与 env_set.sh 各写一份】它有两个使用方，而两个使用方
# 想要的东西不同：verify.sh 要把它**打给人看**（取证），env_set.sh 要拿它**做闸门判定**。
# 两份实现会立刻分叉：一边改了口径、一边没改，然后"取证说 0 条、闸门说有卡"。本仓刚花一整批
# 把 sw_probe 的四份内联拷贝收敛成一份发射片段，为同一个理由造一对新的双胞胎，方向是错的。
# 所以这里只发射**读数**，不发射任何文案：函数把结果放进三个全局变量，文案由各自的调用方写。
# 取用方以 `grep -l 'sw_ops_emit_awaiting_confirm_definition' scripts/ops/*.sh` 为准，
# 本文件是定义处、不算使用方。
#
# 【为什么挑 /api/v1/dashboard 而不是 /api/v1/content】counters.awaiting_confirm 是 core 里
# **已经存在**的一等公民字段（core/api/dashboard.py::Counters.awaiting_confirm，由同文件的
# _awaiting_confirm(session) 算出），服务端一次算完、返回一个整数，不分页。
# /api/v1/content?status=scheduled 的行里虽然带 confirm_pushed_at，但它是分页的
# （core/api/common.py 的 MAX_LIMIT=200），要在远端 shell 里翻页累加才敢报总数。
#
# 【口径：它是个上界，别把它当精确值】awaiting_confirm 的定义是
#     status == scheduled  且  confirmed_at 为空  且  该账号策略 confirm_required=true
# 它**不看 confirm_pushed_at**。所以对"换签名密钥会搞坏几条"而言这是**上界**：还没推出卡的
# 条目也计在里面，而那些条目的卡是换密钥之后才生成的、签的是新密钥、不会失效。偏大的方向
# 是安全的（当闸门用正合适），但调用方的文案里**绝不许**写成"这么多条会失效"。
#
# 【只报计数，绝不回显内容】/api/v1/dashboard 的响应里**确实**带 events[].title 与
# attention[].name（core/api/dashboard.py::Event / AttentionAccount）。容器里那段 python
# 只往 stdout 写一行 `count <整数>` 或 `unknown <原因>`，两者都不含响应里的任何自由文本，
# 所以那些字段一个字节都流不出来。这不是靠调用方自觉，是靠这里的输出形状。
#
# 【"没取到"与"0 条"必须分开，这是本函数存在的第二个理由】把取不到渲染成 0 会让一道闸门在
# 探针坏掉的时候自动放行——那正好是最不该放行的时刻。所以：
#   取到    sw_awaiting_count=<非负整数>，sw_awaiting_reason=''，返回 0
#   没取到  sw_awaiting_count=''，sw_awaiting_reason=<一句话原因>，返回 1
# sw_awaiting_code 是这次探针拿到的 HTTP 状态码（拿不到时是 sw_probe 给的 '000'），
# 留给调用方分辨 401（那一格要给的是"去配 token"，不是"core 挂了"）。
#
# 【发射位置与 sw_ops_emit_sw_probe_definition 完全相同：必须在远端正文那对花括号的里面】
# 而且必须排在 sw_probe 定义**之后**——它调用 sw_probe。理由与那一段逐字相同，不重复。
# 【为什么不套 sw_ops_xtrace_guard】同 sw_ops_emit_sw_probe_definition：这是一段引号
# heredoc，本机一次展开都不会发生，不经手任何凭据值。
_sw_ops_emit_awaiting_confirm_definition_impl() {
  cat <<'SW_AWAITING_CONFIRM'
sw_awaiting_count=''
sw_awaiting_reason=''
sw_awaiting_code=''
sw_awaiting_confirm() {
  local url="$1" max_time="$2" probe_rc=0 parse_rc=0 parsed=''
  sw_awaiting_count=''
  sw_awaiting_reason=''
  sw_awaiting_code=''
  sw_probe "${url}" "${max_time}" >/dev/null 2>/dev/null || probe_rc=$?
  sw_awaiting_code="${sw_probe_code}"
  if [[ "${probe_rc}" -ne 0 ]]; then
    case "${sw_probe_code}" in
      401) sw_awaiting_reason='GET /api/v1/dashboard 返回 401 未授权' ;;
      404) sw_awaiting_reason='GET /api/v1/dashboard 返回 404，这版 core 没有这个端点' ;;
      *)   sw_awaiting_reason="GET /api/v1/dashboard 取不到，curl 退出码 ${probe_rc}、HTTP ${sw_probe_code}" ;;
    esac
    return 1
  fi
  # 容器里那段 python 永远以 0 退出，并且只往 stdout 写一行：`count <整数>` 或
  # `unknown <原因>`。非 0 只剩一种含义——这一步本身没跑起来（容器没起、python3 不在），
  # 那同样只能说"没取到"，绝不能默认成 0 条。stderr 丢掉：docker / python 的原始错误
  # 文本可能很长且不面向运维，退出码已经足够定位。
  parsed="$(printf '%s' "${sw_probe_body}" | docker compose exec -T core python3 -c '
import json
import sys

raw = sys.stdin.read()
try:
    payload = json.loads(raw)
except ValueError:
    print("unknown /api/v1/dashboard 的响应不是合法 JSON")
    raise SystemExit(0)
if not isinstance(payload, dict) or not payload.get("ok"):
    print("unknown /api/v1/dashboard 返回失败外壳")
    raise SystemExit(0)
data = payload.get("data")
counters = data.get("counters") if isinstance(data, dict) else None
value = counters.get("awaiting_confirm") if isinstance(counters, dict) else None
if isinstance(value, bool) or not isinstance(value, int) or value < 0:
    print("unknown 响应里没有 counters.awaiting_confirm 这个非负整数（字段缺失或类型不对）")
    raise SystemExit(0)
print("count {}".format(value))
' 2>/dev/null)" || parse_rc=$?
  if [[ "${parse_rc}" -ne 0 ]]; then
    sw_awaiting_reason="解析 /api/v1/dashboard 的那一步自己失败了（退出码 ${parse_rc}）"
    return 1
  fi
  case "${parsed}" in
    'count '*)
      sw_awaiting_count="${parsed#count }"
      if ! [[ "${sw_awaiting_count}" =~ ^(0|[1-9][0-9]*)$ ]]; then
        sw_awaiting_count=''
        sw_awaiting_reason='解析 /api/v1/dashboard 的那一步给出了不是非负整数的计数'
        return 1
      fi
      return 0
      ;;
    'unknown '*)
      sw_awaiting_reason="${parsed#unknown }"
      return 1
      ;;
    *)
      sw_awaiting_reason='解析 /api/v1/dashboard 的那一步给出了无法识别的输出'
      return 1
      ;;
  esac
}
SW_AWAITING_CONFIRM
}
sw_ops_emit_awaiting_confirm_definition() { _sw_ops_emit_awaiting_confirm_definition_impl; }

# =============================================================================
# 以下这一节的函数只服务 scripts/ops/env_set.sh（受白名单约束的生产 .env 变更）。
# 放在本文件而不是 env_set.sh 里，理由只有一个：**经手凭据的代码集中在一处**——
# sw_ops_xtrace_guard 与它上面那段"为什么必须有"写在这里，凭据字符集白名单也定义在这里。
# 其余 sourcer 只是 source 到它们、一次都不调用，无副作用（逐个函数的取用方以
# `grep -l <函数名> scripts/ops/*.sh scripts/*.sh` 为准；当前每一个都只落在本文件与 env_set.sh）。
#
# 【**值**绝不进参数，只经全局变量流转】这不是风格洁癖，是 xtrace 的硬约束：
# sw_ops_xtrace_guard 只能关掉**函数体内**的追踪，调用方那一行
# `+ some_fn <token>` 与 `+ VAR=<token>` 仍然会被打出来。所以值只能经全局变量流转，
# 调用点上不许出现它。改这几个函数签名前先想清楚这一条。
# 界线画在"是不是值"，而不是"收不收参数"：文件路径与凭据文件键名都不是凭据（它们本来就会
# 出现在报错文案里），走参数没问题；要写的那个值始终只从 SW_OPS_ENV_VALUE 读。
# =============================================================================

# 待写入生产 .env 的值。可能是凭据，只在本进程内存里流转：不进 argv、不进日志、
# 不进任何输出；送往远端时经 sw_ops_emit_env_value_prologue 走 ssh 的 stdin 流。
SW_OPS_ENV_VALUE=""

# 在本机生成一个新的凭据值（256 bit，十六进制）。
#
# 【为什么不叫 sw_ops_generate_ui_token 了】上一版它只服务工作台 API token，本轮
# SW_TELEGRAM_SIGNING_SECRET 也走 `--generate`，而两者要的东西一字不差：一个来自 CSPRNG 的
# 256 bit 值，落在本文件顶部那个字符集白名单里面。留着旧名字等于让第二个调用方去用一个
# 说谎的函数名，或者复制一份逐字相同的实现——本仓刚花一整批消灭过后者。所以改名，
# 不新增第二份。调用方只有 scripts/ops/env_set.sh（`grep -n 'sw_ops_generate_credential'
# scripts/ops/*.sh` 就是名单）。
#
# 【熵源】只用密码学安全的来源，**绝不用 `$RANDOM`**（bash 的线性同余，可预测）。
# 首选 `openssl rand -hex 32`（macOS 与常见 Linux 都自带），退路是直接读 /dev/urandom
# 再用 `od` 转十六进制。两条路都是 256 bit。
#
# 【为什么是 hex 而不是 base64】`openssl rand -base64 32` 的输出含 `+` `/` `=`，
# 它们**确实**都在本文件顶部那个白名单里，能用；但 hex 的字符集是纯 `0-9a-f`，是白名单
# 的真子集，与 curl 配置流、`printf '%q'`、shell 波浪号展开、URL 编码全都无关。
# 生成端能选一个"怎么传都不会出事"的字符集时，就不该把安全性押在下游解析的正确性上。
# 代价是长一倍（64 字符），而这两个值都从不需要人肉输入，所以这个代价是零。
# 签名密钥同理：core/telegram.py 只把它当 HMAC 的 key 用，对字符集没有额外要求。
#
# 成功时把值写进 SW_OPS_ENV_VALUE 并返回 0；拿不到熵源或自检不过返回 1（调用方负责 die）。
_sw_ops_generate_credential_impl() {
  local value=""
  SW_OPS_ENV_VALUE=""
  if command -v openssl >/dev/null 2>&1; then
    value="$(openssl rand -hex 32 2>/dev/null)" || value=""
  fi
  if [[ -z "${value}" && -r /dev/urandom ]] && command -v od >/dev/null 2>&1; then
    value="$(od -An -vtx1 -N32 /dev/urandom 2>/dev/null | tr -d ' \n')" || value=""
  fi
  # 自检：长度与字符集都对上才算数。熵源静默给出短值 / 空值时必须失败，不许凑合——
  # 一个 8 位的凭据与没有凭据的区别只在纸面上。
  [[ "${#value}" -eq 64 ]] || return 1
  [[ "${value}" =~ ^[0-9a-f]{64}$ ]] || return 1
  # 纵深防御：生成出来的值也要过一遍与外来 token 相同的白名单校验。
  [[ "${value}" =~ ${SW_OPS_UI_TOKEN_ALLOWED_RE} ]] || return 1
  SW_OPS_ENV_VALUE="${value}"
}
sw_ops_generate_credential() { sw_ops_xtrace_guard _sw_ops_generate_credential_impl; }

# 把 sw_ops_load_ui_token 取到的值转成"待写入生产 .env 的值"。
# 单独一个函数、而不是在调用点写 `SW_OPS_ENV_VALUE="${SW_OPS_UI_TOKEN_VALUE}"`，
# 就是为了让这次赋值发生在 xtrace 守卫内（见本节开头那段说明）。
_sw_ops_adopt_loaded_ui_token_impl() {
  SW_OPS_ENV_VALUE="${SW_OPS_UI_TOKEN_VALUE}"
  [[ -n "${SW_OPS_ENV_VALUE}" ]] || return 1
}
sw_ops_adopt_loaded_ui_token() { sw_ops_xtrace_guard _sw_ops_adopt_loaded_ui_token_impl; }

# 把凭据文件里 <键名> 那一行的值转成"待写入生产 .env 的值"。用法：<文件> <键名>。
# 这是 UI token 之外的凭据类键走 `--from-credentials` 的那条路。
#
# 【它与 sw_ops_load_ui_token + sw_ops_adopt_loaded_ui_token 的区别，只有一条，但很要紧】
# 那一对在读文件**之前**先看环境变量 `SW_OPS_UI_TOKEN`。那一层是 UI token **专有**的：
# 运维侧本来就持有同一个值去打探针，所以给它留了一个"这一次调用换个值"的显式入口。
# 别的凭据类键（首个是 SW_TELEGRAM_SIGNING_SECRET）**没有**对应的环境变量，也不该有——
# 运维侧不需要持有它，它只是要被推到生产 .env 去的一个值。凭空发明一个
# `SW_OPS_TELEGRAM_SIGNING_SECRET` 只会多一条"值可能从哪来"的路径，而每多一条这样的路径，
# "两边为什么不一致"就多一种查不清的可能。这条取舍在 _sw_ops_load_ui_token_impl 上方也写着。
#
# 【键名守卫要在父 shell 里先走一遍，理由与 _sw_ops_load_ui_token_impl 那处逐字相同】
# 下面那行是命令替换，子 shell 里的 die 只打死子 shell；而 `if x="$( ... )"` 这个位置又把
# set -e 关了，于是一个非法键名会退化成"这个键没配"继续往下跑。
#
# 取不到 / 取到空值返回 1（调用方负责 die），并保证 SW_OPS_ENV_VALUE 是空的——
# 绝不让上一次调用的残值被当成本次的取值推到生产去。
_sw_ops_adopt_credentials_key_impl() {
  local file="$1" key="${2-}"
  _sw_ops_credentials_key_guard "${key}"
  SW_OPS_ENV_VALUE=""
  [[ -r "${file}" ]] || return 1
  SW_OPS_ENV_VALUE="$(_sw_ops_read_credentials_key_impl "${file}" "${key}")" || SW_OPS_ENV_VALUE=""
  [[ -n "${SW_OPS_ENV_VALUE}" ]] || return 1
}
sw_ops_adopt_credentials_key() { sw_ops_xtrace_guard _sw_ops_adopt_credentials_key_impl "$@"; }

# 凭据文件里有没有顶格的 <key> 键。**只回答有没有，值一律丢进 /dev/null。**
# 用法：<文件> <键名>，两个参数都必传。
_sw_ops_credentials_has_key_impl() {
  local file="$1" key="${2-}"
  # 守卫必须排在最前面，早于下面那条带 `2>/dev/null` 的调用：那条重定向会把 die 的报错一起
  # 吞掉，键名违例就成了一次**一个字都不打**的退出。也早于 `-r` 那条：编码错误不该因为
  # "文件恰好不存在"而被掩盖成一句 return 1。
  _sw_ops_credentials_key_guard "${key}"
  [[ -r "${file}" ]] || return 1
  _sw_ops_read_credentials_key_impl "${file}" "${key}" >/dev/null 2>&1
}
sw_ops_credentials_has_key() { sw_ops_xtrace_guard _sw_ops_credentials_has_key_impl "$@"; }

# 把 SW_OPS_ENV_VALUE 追加为凭据文件的 <key> 键。用法：<文件> <键名>，两个参数都必传；
# 要写的那个**值**不走参数，始终只从 SW_OPS_ENV_VALUE 读（理由见本节开头）。
#
# 【只追加，不改写】调用方必须先用 sw_ops_credentials_has_key 拿**同一个键名**确认它不存在。
# 这里不做"替换已有键"：那需要真的理解文件结构，而本仓刻意不给 shell 引入 YAML 解析器
# （见 _sw_ops_read_credentials_key_impl 上方的取舍说明）。宁可让人自己删掉旧键。
#
# 【原子写入 + 0600】先写同目录临时文件、chmod 600、再 mv（同目录 mv 是 rename(2)，原子）。
# 绝不就地编辑：写到一半断电/断链会把值班工作站唯一那份凭据弄成半截。
#
# 【末尾换行】原文件最后一个字节不是换行时，直接追加会把 `<key>:` 粘在上一行尾巴上，
# 那一行会变成谁也解析不出来的东西。`$(tail -c 1 ...)` 会吃掉尾随换行，所以它为空**就等于**
# 最后一个字节是换行——用这一点判，不用再读一遍整个文件。
_sw_ops_write_credentials_key_impl() {
  local file="$1" key="${2-}" dir tmp
  _sw_ops_credentials_key_guard "${key}"
  [[ -n "${SW_OPS_ENV_VALUE}" ]] || return 1
  dir="$(dirname "${file}")"
  mkdir -p "${dir}" || return 1
  chmod 700 "${dir}" 2>/dev/null || true
  tmp="${file}.sw-ops-tmp.$$"
  [[ ! -e "${tmp}" ]] || return 1
  (
    umask 077
    : >"${tmp}"
  ) || return 1
  chmod 600 "${tmp}" || { rm -f "${tmp}"; return 1; }
  if [[ -f "${file}" ]]; then
    cat "${file}" >>"${tmp}" || { rm -f "${tmp}"; return 1; }
    if [[ -s "${file}" && -n "$(tail -c 1 "${file}")" ]]; then
      printf '\n' >>"${tmp}" || { rm -f "${tmp}"; return 1; }
    fi
  fi
  printf '%s: %s\n' "${key}" "${SW_OPS_ENV_VALUE}" >>"${tmp}" || { rm -f "${tmp}"; return 1; }
  mv "${tmp}" "${file}" || { rm -f "${tmp}"; return 1; }
  chmod 600 "${file}" || return 1
}
sw_ops_write_credentials_key() { sw_ops_xtrace_guard _sw_ops_write_credentials_key_impl "$@"; }

# 校验 SW_OPS_ENV_VALUE 的形状。**必须是一个函数**，不能在调用点直接写
# `[[ "${SW_OPS_ENV_VALUE}" =~ ${re} ]]`：`[[ ... ]]` 是一条命令，xtrace 会把它连同
# **展开后的两侧**一起打出来，于是 `bash -x` 下就多了一行
#     + [[ <token> =~ ^[A-Za-z0-9._+/=:@-]+$ ]]
# 这正是 tests/ops/test_env_set.sh 里那条 `bash -x never prints the token` 用例第一次
# 跑就抓到的真实泄漏点（不是假想的）。包成函数之后，调用点那行只剩正则。
_sw_ops_env_value_matches_impl() {
  [[ "${SW_OPS_ENV_VALUE}" =~ $1 ]]
}
sw_ops_env_value_matches() { sw_ops_xtrace_guard _sw_ops_env_value_matches_impl "$@"; }

# 生成送给远端 bash 的脚本流里那一行 `export SW_ENV_SET_VALUE=<%q 转义>`。
# 与 sw_ops_emit_token_prologue 完全同源同理由：printf 是 bash 内建，参数不产生
# /proc/*/cmdline 条目；`%q` 保证远端 shell 重新解析时拿回逐字符相同的值（空值产出 ''）。
#
# ！！这一行同样在远端正文那对花括号的**外面**，所以那层 `{ ... } </dev/null` 的结构性
# 保证对它不生效。今天安全，因为 export 是内建、不 fork、不读 stdin。要往前言里加东西，
# 先读 sw_ops_emit_token_prologue 上方那段警告。
_sw_ops_emit_env_value_prologue_impl() {
  printf 'export SW_ENV_SET_VALUE=%q\n' "${SW_OPS_ENV_VALUE}"
}
sw_ops_emit_env_value_prologue() { sw_ops_xtrace_guard _sw_ops_emit_env_value_prologue_impl; }

# 拿 SW_OPS_ENV_VALUE 里那把 key 去问一次模型网关，只回一个 HTTP 状态码（连不上回 000）。
# 【为什么值走 --config - 而不是 -H】同本文件里其余几处探针的理由：`-H "Authorization: …"`
# 会把值留在 argv，也就是 /proc/*/cmdline 与 ps 输出里。printf 是 bash 内建、不 fork，
# 管道右端的 curl 只看到 `--config -`，值从它的 stdin 走。
# 外面包 sw_ops_xtrace_guard：调用方开着 bash -x 时，printf 那行会连同展开后的值一起被打出来。
# 只打 /models（GET、幂等、不产生用量），不打 /chat/completions：闸门要问的是"这把 key 网关
# 认不认"，一个真推理请求既慢又花钱，还会因为模型名不对而假红。
_sw_ops_probe_llm_key_impl() {
  local base="${1%/}"
  printf 'header = "Authorization: Bearer %s"\n' "${SW_OPS_ENV_VALUE}" \
    | curl --silent --output /dev/null --write-out '%{http_code}' \
        --connect-timeout 10 --max-time 25 --config - "${base}/models" 2>/dev/null
}
sw_ops_probe_llm_key() { sw_ops_xtrace_guard _sw_ops_probe_llm_key_impl "$@"; }
