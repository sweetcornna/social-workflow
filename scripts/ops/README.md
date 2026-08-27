# 生产运维脚本

这些脚本经 SSH 别名 `workbench-iap` 管理服务器的 `~/social_workflow`。所有 SSH
连接均设置 25 秒建连超时，适配 IAP 首包约 5–10 秒的延迟。可用
`SW_OPS_SSH_ALIAS` 覆盖别名，也兼容 `SW_TUNNEL_SSH_ALIAS`。

脚本不会读取或打印 `.env`、密钥、token 或台账内容——**例外有两个**：`verify.sh --preflight`
（见该脚本一节）；以及 `env_set.sh`，它按一份写死的**白名单**读写生产 `.env`，但**永远
不回显凭据值**（见「改生产 `.env`」一节）。生产 core 启用 `SW_UI_TOKEN` 后，`restart.sh` / `update.sh` / `verify.sh`
的探针需要带 `Authorization: Bearer`，配置方式见下面「工作台 API token」一节；脚本只报
token 的来源，**永远不打印它的值**。

`ui_token.sh` 不是命令，是被本目录里若干脚本 `source` 的库（token 取用、生成、字符集校验、
注入远端）。当前的 `source` 方以 `grep -l 'ui_token\.sh' scripts/ops/*.sh` 为准：
`status.sh` / `restart.sh` / `update.sh` / `verify.sh` 用它取 token 探 `/api/v1/*`；
`env_set.sh` 另外用它生成 token、写凭据文件、把值经 stdin 流送到远端；`sidecar.sh` 只在
`--status` 这一个模式里用它探 `/api/v1/system/info`（其余三个模式一个凭据都不取、也不往远端送）。

**本目录之外曾有一个 `source` 方**：`scripts/chat_console.sh`（对话台启动器）。对话台
2026-08-27 已删除（`docs/OPS.md` 7.7），所以这份库现在的消费方**只在本目录内**。

| 脚本 | 用途 |
| --- | --- |
| `status.sh` | 只读显示 Compose 服务、core 信息探针、磁盘水位和数据卷文件。 |
| `logs.sh [行数] [-f]` | 只读查看 core 最近日志，默认 200 行，`-f` 持续跟随。 |
| `backup.sh` | 用 SQLite 在线备份 API 创建一致性快照，并把数据库和生产台账拷到本机。 |
| `restart.sh` | 自动备份后重启 core，直到 `/api/v1/system/info` 返回 200。 |
| `update.sh [--dry-run\|--apply] [--ref <分支> --sha <40位小写SHA>]` | 自动备份后 fetch 并显示提交区间；可将 `origin/<分支>` 钉死到指定完整 SHA。默认演练，只有 `--apply` 会更新、重建和重启。 |
| `verify.sh [--sha <40位小写SHA>] [--preflight]` | 纯只读部署核验：一次 SSH 往返采集 git HEAD、端口门禁、健康探针与 Telegram 409 计数，无任何副作用，可重复运行。 |
| `env_set.sh --show \| --key <白名单键> ...` | 按一份写死的**白名单**（键与闸门见「改生产 `.env`」一节那张表）查看 / 变更生产 `.env`：逐键校验、**逐键的事前闸门**、备份后原子写入、重建容器让它生效，并由 `restart.sh` 走 R1 红线闸门。 |
| `sidecar.sh --status \| --materialize \| --up \| --down <sidecar>` | 受**白名单**约束地在生产上就地生成 sidecar 配置、按 profile 起停**单个** sidecar；`--up` 之前先用远端 `docker compose config` 解析后的 `host_ip` 强制确认端口只绑回环。一律不碰 core。 |

## 常用命令

```bash
bash scripts/ops/status.sh
bash scripts/ops/logs.sh 100
bash scripts/ops/logs.sh -f
bash scripts/ops/backup.sh
bash scripts/ops/restart.sh
bash scripts/ops/update.sh             # 默认 --dry-run（本服务器打不通，见「这台生产服务器的部署形态」）
bash scripts/ops/update.sh --apply     # 真实更新（同上）
# 只允许部署 origin/p14-organic 恰为该完整 SHA 的纯快进版本
bash scripts/ops/update.sh --dry-run --ref p14-organic --sha 0123456789abcdef0123456789abcdef01234567
bash scripts/ops/update.sh --apply --ref p14-organic --sha 0123456789abcdef0123456789abcdef01234567
# 只读取证：如实打印生产 HEAD，不核对具体 SHA
bash scripts/ops/verify.sh
# 部署后核验：要求生产 HEAD 逐字符等于这个完整 SHA
bash scripts/ops/verify.sh --sha 0123456789abcdef0123456789abcdef01234567
# 只读：白名单里的键在生产 .env 里设了没有（凭据值永不回显）
bash scripts/ops/env_set.sh --show
# 翻模拟发布器（false = 真发布开启，必须过 R1 闸门）
bash scripts/ops/env_set.sh --key SW_USE_FAKE_PUBLISHERS --value false
# 给生产加工作台 API token（本机生成 → 写凭据文件 → 推生产，全程不回显）
bash scripts/ops/env_set.sh --key SW_UI_TOKEN --generate
# 在生产机器上、用生产镜像跑一遍端到端验收（隔离沙盒，碰不到真实台账与数据库）
bash scripts/ops/acceptance.sh --dry-run
bash scripts/ops/acceptance.sh --lane xhs
```

指定 `--ref` 时必须同时给 `--sha`（40 位**小写**完整提交；短 SHA 和大写都会被拒绝）。分支名
只接受字母、数字、`.`、`_`、`/`、`-` 组成且不以 `-` 开头的合法 Git branch，所有参数都会在
备份和 SSH 之前完成校验。脚本只执行以下受限 heads fetch，不取 tags：

```text
git fetch --no-tags --prune origin +refs/heads/<ref>:refs/remotes/origin/<ref>
```

随后要求 remote-tracking ref 直接指向 commit（不接受经 tag peel 的间接对象），raw ref、peeled
commit 与指定 SHA 三者完全相同。只有工作树干净且当前 HEAD 是目标祖先，才执行
`git merge --ff-only <SHA>`；merge 后、build 前还会再次要求 HEAD 精确等于已核验 SHA。

未带 `--ref` / `--sha` 时沿用当前分支 upstream：检查 upstream 存在，`fetch --prune` 后拒绝
dirty、ahead 和 diverged 状态，`--apply` 只执行 `git pull --ff-only`，并在 build 前核验 HEAD
仍等于 fetch 后的目标。

`--dry-run` 也会先调用 `backup.sh`，因此同样会创建并轮转受管备份；它不会 merge、build、up
或运行 preflight。生产运行前，远端 `.env` / secret 管理必须已提供 `SW_ENV=prod` 与所有必需
凭据；运维脚本不会打印或写入任何真实密钥。

演练只会在 SSH 本身以 255 报告连接/传输中断时等待 3 秒并重试一次；远端 ref、SHA、工作树、
祖先关系等校验失败以及其他远端非零退出都会立即失败，不会误报为 IAP 中断。远端更新脚本自身
若意外退出 255，包装层会先把它规范为非 255 失败，避免触发部署重试。`--apply` 永不自动重试。

## 这台生产服务器的部署形态

先说结论：这台服务器上，`update.sh` 不带 `--ref/--sha` 的默认形态**打不通**；唯一能用的是
显式指定 `--ref p14-organic --sha <SHA>`。以下事实采集于 2026-08-22，采集方式为
`bash scripts/ops/verify.sh` 只读核验（未 fetch、未 merge，无任何写操作）：

```text
当前分支  main
当前提交  51d5d7049499d7d3b35614e9b2019b412901e4ad
工作树    干净
远端对比  origin/main=fb9b6566d2604ba61ef374938d5bd9ac05c5c4e9
          HEAD 领先 14 个提交，落后 0 个提交
```

**上面这段是 2026-08-22 那次采集的原始输出，逐字保留不改——它是取证记录。** 但要注意
`verify.sh` 此后换掉了那个参照系：「远端对比」这一段已经不存在了，取而代之的是「发布线」
与「部署标记」（见下面 `verify.sh` 一节）。换掉的原因恰恰就是这段记录暴露的问题：拿
`origin/<同名分支>` 当基准，算出来的 `领先 14 个提交` 字面为真、指向却是错的——生产真正
所在的发布线是 `origin/p14-organic`，不是 `origin/main`。`docs/RISKS.md` 第 11 条记着这条。

**分支命名陷阱**：生产检出的本地分支名叫 `main`，但它的 HEAD 是 `51d5d70`——这是 GitHub 上
**`p14-organic`** 分支的顶端，不是 `main` 的顶端。历史上是用
`update.sh --ref p14-organic --sha <SHA>` 部署的：这个形态只把 `origin/<ref>` 钉死到指定
SHA，再对当前分支做 `git merge --ff-only`，不会改本地分支名——于是本地那个叫 `main` 的分支
被快进到了 `p14-organic` 的提交上。**本地分支名与它实际承载的发布线不是一回事：看到
`verify.sh` 输出的「当前分支」是 `main`，不要以为部署的是 GitHub 的 `main`。**

与此相关，生产的 `origin/main` 这个 remote-tracking ref 是陈旧的，停在 `fb9b656`；GitHub 上
`main` 的真实位置是 `7421918`。已用 `git merge-base --is-ancestor` 验证：`fb9b656` 和
`7421918` 都是 `51d5d70` 的祖先，所以不存在"生产其实落后了"的可能。

**为什么无参数形态在这台服务器上永远跑不通**：`update.sh` 不带 `--ref/--sha` 时走当前分支的
`@{upstream}`，也就是 `origin/main`。`git fetch --prune` 后 `origin/main` 会更新到 GitHub
真实位置 `7421918`；但 `7421918` 是当前 HEAD（`51d5d70`）的祖先，于是算出来
`ahead=12, behind=0`，脚本会以「当前 HEAD 领先目标，不能安全快进更新」中止。**这个 12 与
上面「这台生产服务器的部署形态」小节 `verify.sh` 原始输出里的 `ahead=14` 是两个不同的基点，
不要混用**：`verify.sh` 从不 fetch，比较的是本地陈旧的 remote-tracking ref `fb9b656`（`git
rev-list --count fb9b6566d2604ba61ef374938d5bd9ac05c5c4e9..51d5d70` = 14 个提交）；`update.sh`
会先 `git fetch --prune` 把 `origin/main` 刷新到 GitHub 真实位置 `7421918` 再比较（`git
rev-list --count 7421918..51d5d70` = 12 个提交）。两个数字都不影响结论：无论基点是 14 还是
12，HEAD 都领先目标，护栏都会中止。**这是护栏在正常工作，不是故障，不要试图绕过**——护栏是
承重的，绝不会把生产回滚到旧提交；实际含义只是无参数形态在这台服务器上用不了，会给出一句有
意义的拒绝而已。

（历史背景：`update.sh` 无参数形态此前还有一个已修复的缺陷——ssh 不保留 argv 边界，空参数
会在拼接后消失，导致远端 `set -u` 下 `$2` unbound 直接崩溃；现在这个缺陷已修好，无参数形态
能正常运行到「拒绝更新」这一步。**能跑通不等于在这台服务器上有用**，结论不变：这台服务器
只能走 `--ref/--sha`。）

这台服务器上唯一可用的部署命令形态：

```bash
# 演练：只 fetch 并核验目标，不改工作树
bash scripts/ops/update.sh --dry-run --ref p14-organic --sha 0123456789abcdef0123456789abcdef01234567
# 真实部署
bash scripts/ops/update.sh --apply --ref p14-organic --sha 0123456789abcdef0123456789abcdef01234567
```

把示例 SHA 换成想部署的真实 40 位小写完整提交；分支名固定是 `p14-organic`（这台服务器实际
承载的发布线），不要改成 `main`。

部署后核验：

```bash
bash scripts/ops/verify.sh --sha <刚部署的SHA>
```

重点看「当前分支」（预期仍显示 `main`，这是分支命名陷阱的自然结果，不是核验失败）、
「发布线」（预期能看到 `origin/p14-organic`，这才是它真正所在的那条线）与「部署标记」
（`update.sh --apply` 会留下 `ref=p14-organic`；对照两格都应显示一致）。

## 工作台 API token

**先说结论：生产 `.env` 里现在没有 `SW_UI_TOKEN`，所以本节现在什么都不用配；一旦有人给
生产加上它，不配就会看到 401。** 背景与上线顺序见 `docs/RISKS.md` 第 8 条。

`core/api/common.py::require_token`：`SW_UI_TOKEN` 只要是非空字符串，除 `/auth/login` 外的
全部 `/api/v1/*` 都要求 `Authorization: Bearer <token>`，缺头 / 非 Bearer 格式 / 值不匹配
一律 401。`/health` 注册在 app 根上，不过这道守卫。**哪些脚本会打 `/api/v1`、因此必须能带上
这个头，以 `grep -l 'ui_token\.sh' scripts/ops/*.sh` 为准**（结果里的 `ui_token.sh` 是库不是
命令，不算）；打的是哪个端点各不相同：`verify.sh` / `update.sh` / `restart.sh` / `status.sh`
探 `/api/v1/system/info`（前三个还探 `/api/v1/system/telegram`），`sidecar.sh` 只在 `--status`
一个模式里探 `/api/v1/system/info`，`env_set.sh` 只在两道要打 `/api/v1` 的事前闸门上探
`/api/v1/system/telegram` 与 `/api/v1/dashboard`。`logs.sh` 与 `backup.sh` 不打 `/api/v1/*`，
不受影响——也就是说**`scripts/ops/` 下已经没有裸探 `/api/v1` 的地方了**。

### 怎么配

两个来源，**环境变量优先**：

```bash
# ① 环境变量（推荐：这一次调用的显式意图，换值、做对照实验都最方便）
export SW_OPS_UI_TOKEN='<与生产 .env 里 SW_UI_TOKEN 完全相同的值>'

# ② 凭据文件（长期约定，红线 R5 指定的存放位置，权限必须 0600）
#    只认顶格、无缩进的 sw_ui_token 键；值可以裸写，也可以用一对单引号或双引号包起来。
printf 'sw_ui_token: <同一个值>\n' >> ~/.dsh-sw/.credentials.yaml
chmod 600 ~/.dsh-sw/.credentials.yaml
```

优先级与两条特殊规则：

- `SW_OPS_UI_TOKEN` **只要已导出就采信，哪怕是空串**。空串 = "本次显式不带 token"，用来复现
  未鉴权路径；这时**不会**回落去读凭据文件。没导出这个变量，才会去读文件。
- 凭据文件里读不到 `sw_ui_token`（键不存在、缩进了、写成嵌套结构、值里有行内注释）时，
  一律当作"没配"，脚本回落到不带 token 的老行为。这是刻意的取舍：本仓不给 shell 脚本引入
  YAML 解析依赖，所以只做极窄匹配，**看不懂就不猜**。要用嵌套结构，请改用环境变量。

**名字不要和服务端搞混**：`SW_UI_TOKEN` 是服务端配置项（写在生产 `.env` 里，core 自己读）；
`SW_OPS_UI_TOKEN` 是运维侧持有的同一个值。在本机 export `SW_UI_TOKEN` 不会让生产生效。

### 字符集限制（会硬性拒绝）

只允许 `A-Z a-z 0-9` 以及 `.` `_` `-` `+` `/` `=` `:` `@`。

原因分层说清（每一条都本机实测过，别混成一句）：

1. `"` 与 `\` 是**硬性的**：token 是通过 curl 配置流里的
   `header = "Authorization: Bearer <token>"` 这一行注入的（这样它就不会出现在 curl 的
   argv 里）。curl 解析双引号值时把 `\` 当转义、把 `"` 当定界符：实测裸 `"` 让值在那里
   **静默截断**，裸 `\` 被直接吃掉——发出去的头不是你以为的那一个，且没有任何报错。
2. 空白与控制字符——**理由不是"curl 会截断"**：真 curl 8.7.1 实测，
   `header = "Authorization: Bearer AB CD"` 会原样发出 `Bearer AB CD`，空格并不截断。
   排除它们的正确理由是 RFC 6750 的 `b64token` 语法本来就不含空白与控制字符，它们不是
   合法的 Bearer token 字符。
3. `~`：远端注入这一步是 `export SW_OPS_UI_TOKEN=~abc` 这类展开，会触发 shell 的波浪号
   展开，值会被悄悄换成别的东西。

脚本在**本机**、在备份与 SSH 之前就校验，不合法直接报错退出，既不发出一个语法被破坏的
请求，也不放行一个会被 shell 悄悄改写的值。错误信息里不会回显 token 本身。

常见生成方式都在允许集内：

```bash
openssl rand -base64 32
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

### 字符集偏窄的代价（本轮不放宽）

白名单之外，还有一类字符——`# % $ * ! , ; ( ) [ ]` 甚至空格——curl 配置流其实**能**安全
携带，但同样被拒。排除它们不是因为不安全，而是**安全侧的主动收紧**：宁可拒绝一个本来能用
的 token，也不要在一条很难查的路径上出错；只要报错说清楚（列出允许的字符与分层理由），
这个代价可以接受。

现实影响：`openssl rand -base64 32`、`secrets.token_urlsafe(32)`、hex、uuid、JWT
都落在允许集内，不受影响；会被拒的是 Django `get_random_secret_key()`（字符集含
`!@#$%^&*()`）、以及密码管理器"带符号"这一类默认输出——生成时选纯字母数字或上面几种
方式即可绕开。

本轮**不放宽**这个白名单。

### 不配会怎样 / 配错会看到什么

| 情形 | 现象 |
| --- | --- |
| 生产没启用 `SW_UI_TOKEN`，本机也没配 | 与改造前逐字一致，不打印任何与 token 有关的行 |
| 生产没启用，本机配了 | 照常通过。未启用时 `require_token` 直接 return，多余的头被忽略——**所以可以先配本机再改服务器，中间没有必然失败的窗口** |
| 生产启用了，本机没配（或值不对） | 见下面一段，**逐脚本各不相同** |
| token 含非法字符 | 备份与 SSH 之前就报错退出，列出允许的字符与原因 |
| 配置成功 | 脚本开头多一行 `已加载工作台 API token（来源：…）`，只报来源，**不报值也不报长度** |

**拿到 401 时各脚本的行为**（都不会把它误报成部署故障或运行时故障）。
**哪些脚本会遇到 401，以 `grep -l 401 scripts/ops/*.sh` 为准**（结果里的 `ui_token.sh` 是库
不是命令，不算），下面逐条对应——这里刻意不写总数：没有任何测试钉着这份清单与目录一致，
一个数字只会在下一次加脚本时变成假话，而条目本身就在下面、数得过来：

- `verify.sh`：`运行环境 env=prod` 与 `人工确认闸门通道` 两项判失败，但打印的是「根因 / 处置 /
  自查 / 出处」四行提示，明写"这不是部署故障，也不是 core 运行时故障——core 正常应答了 401"。
  其余各道门禁照跑，结论段仍然完整（本来就绝不短路）。
- `update.sh --apply`：探针第一次拿到 401 就停手（不再空等 30 秒），提示里明说"401 恰恰证明
  新版 core 已经起来并在正常应答，**先别按下面的人工恢复指引回滚**"，并给出"配好后先跑
  `verify.sh --sha <目标SHA>` 确认这一版到底部署成没有"。
- `restart.sh`：同样立刻停手且**不重试**（core 已经在应答，重启第二次只是白白多打断一次生产），
  提示"这不是重启失败"。
- `status.sh`：**只让 Core 探针那一段降级**，打印明确的 401 告警与处置提示，
  Compose 服务 / 磁盘水位 / 数据卷文件三段照常打印完，最后才以失败收尾。理由不是"三段与
  鉴权无关所以不该连坐"这类泛泛的说法，而是 401 的含义非常确定：**core 活着、正常应答了，
  只是本机没有匹配的凭据**——它是唯一**已知、良性、且能被精确识别**的失败（核对 HTTP
  状态码就够了），所以只有它够格降级。连不上 / 超时 / 5xx 是未知情形，生产可能真有问题，
  保持原来的"在探针段原地中止"语义不变。
  **注意这个降级不覆盖"磁盘满了把 core 撑挂"这类场景**：core 真的被撑挂时探针拿到的不是
  401，而是连接拒绝或超时（curl rc 7 / 28），走的正是上面"原地中止"那条路——`磁盘水位` 与
  `数据卷文件` 一个字都不会打。想在 core 已经挂了的情况下看磁盘，要走 SSH 或别的路径，不要
  指望本脚本这个降级。
- `env_set.sh`：401 只可能出现在**写 `.env` 之前**要打 `/api/v1` 的那两道事前闸门上——
  `--key SW_USE_FAKE_PUBLISHERS --value false` 探 `/api/v1/system/telegram`，三个凭据类键的
  `signing_secret` 探 `/api/v1/dashboard` 读待人点的确认卡条数。两道都把 401 归进"**没探到**"
  那一档（分别退 `38` 与 `46`），而不是"探到了不行"（`37` / `45`）——这两件事的处置动作完全
  不同。fail-closed：**`.env` 一个字节都没动**，没有备份、没有重建容器，生产维持原状；提示
  直接说补上 `SW_OPS_UI_TOKEN` 再重跑。注意它**不用 `41`** 这个码：41 在本目录里恒等于
  "401 未授权"，而对本脚本来说 401 与超时是同一类事（拿不到通道状态），所以 41 在它那里
  刻意空着。`signing_secret` 那一档还多一条出路：401 恰恰是"两边 token 不一致"的表现，而
  `--from-credentials` 正是它唯一的收敛动作，所以 `--accept-breaking-pending-confirm-cards`
  **刻意**也覆盖 `46`。
- `sidecar.sh`：只有 `--status` 会打 `/api/v1`（读 `use_fake_publishers` 供人对照）。401 时
  **只让那一格降级**成 `<未取到：…401…>` 并给出补 token 的提示，其余四段照常打印，
  **并且仍以 0 收尾**——它是只读视图，那一格是给人对照的事实、不是门禁，这一点与 `status.sh`
  （降级之后仍以失败收尾）**刻意不同**。`--up` / `--down` / `--materialize` 一个凭据都不取、
  也不打 `/api/v1`，**根本不存在 401 这条路径**。

### 凭据不进 argv（为什么要这么绕）

生产是**合租机器**：同机跑着与本项目无关的同租户容器
/ `temporal` / `postgres` 等容器（`docs/RISKS.md` §8.2）。`/proc/*/cmdline` 对同机其他用户
可读，argv 泄漏是现实威胁，不是理论威胁。所以：

- 本地侧用 bash **内建** `printf` 把 `export SW_OPS_UI_TOKEN=<%q 转义>` 写进送给远端 bash
  的脚本流（ssh 的 stdin）。内建命令不 fork，不产生自己的 `/proc/*/cmdline`；ssh 的 argv
  里只有 `bash -s -- <已校验的位置参数>`。
- 远端侧同样用内建 `printf` 把 `header = "…"` 写进 `curl --config -` 的配置流，curl 的 argv
  里只有 `--config -`。带 token 的探针只有 `sw_probe` 一份定义（见本节末尾），它的 curl 以
  **`-q`** 打头（必须是第一个参数才生效）。
- **全程不落远端磁盘，前提是这个 `-q`**：不加它时，远端家目录里一份带 `verbose` /
  `trace-ascii <file>` 的 `~/.curlrc` 就能把 `Authorization: Bearer <token>` 明文追加进磁盘
  文件（本机实测复现）。`-q` 是双重作用，别只当它是个可有可无的旧参数顺手删掉：一是堵住
  `.curlrc` 导致的凭据落盘，二是让探针的行为不再受本机/远端 `.curlrc` 影响——`.curlrc`
  还能悄悄改超时、加 `-o`、加 `--fail-with-body`。加了 `-q` 之后 token 才只进远端进程环境
  （`/proc/<pid>/environ` 只有属主与 root 可读，与世界可读的 `cmdline` 不是一回事）。
- `tests/ops/` 里有对应断言：假件把每次调用的完整 argv 落盘，断言 token 明文出现 **0 次**；
  同时另有一份**分开的**记录断言头确实送到了、值正确。只证明"没泄漏"不证明"送到了"不算数。

### 曾经的第二个消费方（对话台，已移除）

`scripts/chat_console.sh` 曾复用同一份 `ui_token.sh`。对话台 2026-08-27 删除后这条路没有了，
连同它的回归用例 `tests/ops/test_chat_console.sh`（25 例）一起去掉。

留一句在这里，是因为它当年逼出来的那条设计仍然生效：**这份库允许消费方各自扩展"认哪些
环境变量名、优先级怎么排"，但取值逻辑只有一份**——字符集白名单、xtrace 守卫、凭据文件的
极窄解析、「已导出就采信哪怕空串」这几条不许各写各的。将来再有第二个消费方，照这条来。

实现在 `scripts/ops/ui_token.sh`（不单独执行；`source` 方以本文件开头那条
`grep -l 'ui_token\.sh' scripts/ops/*.sh scripts/*.sh` 为准，这里不写个数）。

**远端那份 `sw_probe` 现在只有一份定义，本文档上一版说的"四份逐字相同的拷贝"已经不成立。**
从前它必须内联进每个脚本各自的 heredoc（远端只有一条脚本流，没有别的文件可读），于是
`status.sh` / `restart.sh` / `update.sh` / `verify.sh` 里躺着四份逐字相同的拷贝，靠一条源码级
断言比对着。那条断言拦得住"改漏一处"，却拦不住真正的代价：**要给第五个脚本加探针，就得再抄
一份**——`env_set.sh` 当初不做事前预防闸门，理由正是这个。现在唯一定义处是
`ui_token.sh` 的 `sw_ops_emit_sw_probe_definition()`，由它把定义**发射进 ssh 的 stdin 流**，
每个调用方各自在自己的 heredoc 中间插一次（**当前有哪些调用方以
`grep -l 'sw_ops_emit_sw_probe_definition' scripts/ops/*.sh` 为准**，结果里的 `ui_token.sh`
是定义处、不算，这里同样不写个数）。`tests/ops/test_update.sh` 的断言也随之换了对象：
现在钉的是"定义只有一处""每个调用方恰好调一次发射函数、一份都不内联"。

**同一套办法后来又用了一次**：远端"待人点的确认卡条数"的读数
（`sw_ops_emit_awaiting_confirm_definition` → 远端 `sw_awaiting_confirm`）也是单一定义处，
`verify.sh` 把它渲染成取证行、`env_set.sh` 的 `signing_secret` 闸门拿它当判据，**两边读的
是同一个数**。那段片段只发射"读数"、不发射文案（容器侧 python 只打一行 `count <n>` /
`unknown <reason>`），从结构上保证 `events[].title` 与 `attention[].name` 漏不出来。
它同样有三条防回归静态扫描——"在调用方里内联一份字节完全相同的第二份定义"这个变异，
`test_verify.sh` 的行为用例**全部照过**，只有静态扫描看得见它。

## 改生产 `.env`（`env_set.sh`）

**先说结论：这是 `scripts/ops/` 下唯一能写生产 `.env` 的路径，白名单写死在脚本里，
不接受运行时扩展**（当前是哪些键见下面那张表；唯一真相源是
`grep -n '^SW_ENV_WHITELIST=' scripts/ops/env_set.sh`）。 它的第一目的不是方便，是把「把 `SW_USE_FAKE_PUBLISHERS` 翻成
`false`」这次变更拉回 R1 闸门里——在它出现之前，整条风险登记册里最危险的那一次切换
（从"什么都不会真发"翻成"真的会发出去"）完全在工具面之外手工进行，也就完全绕过了闸门
（`docs/RISKS.md` 第 9 条第 3 步、第 12 条）。

### 白名单里的键，以及各自的闸门

**这里刻意不写键数**：数字写在这、被数的东西在脚本里，扩容时必然对不上（本仓在三处踩过
这个坑）。当前名单当场跑一条命令就有，下表按它给出的顺序逐键展开：

```bash
sed -n 's/^SW_ENV_WHITELIST="\(.*\)"$/\1/p' scripts/ops/env_set.sh
```

| 键 | 合法值（源码坐标） | 值可否回显 | **这个键特有的事前闸门** |
| --- | --- | --- | --- |
| `SW_UI_TOKEN` | 字符集白名单（`ui_token.sh`）；只走 `--generate` / `--from-credentials`，**不接受 `--value`** | 否 | `signing_secret`：`.env` 里 `SW_TELEGRAM_SIGNING_SECRET` 已显式设且非空就放行（那时换它动不到签名密钥），否则读待人点的确认卡条数——`0` 条放行、有卡拒绝、读不出来 fail-closed。详见下面「签名密钥轮换闸门」 |
| `SW_TELEGRAM_SIGNING_SECRET` | 与 `SW_UI_TOKEN` 同一份字符集白名单（两个值走同一条通路）；同样只走 `--generate` / `--from-credentials` | 否 | 同一道 `signing_secret`，但**没有免检方向**：它就是回落链第一级（`core/telegram.py:151-154`），设它 / 换它总会改变生效的密钥，所以每次都读条数 |
| `TELEGRAM_BOT_TOKEN` | `<数字 bot_id>:<授权串>`（`^[0-9]{5,16}:[A-Za-z0-9_-]{30,64}$`，钉多紧的理由见 `sw_env_value_re()` 上方）；**只走 `--from-credentials`**——值由 BotFather 签发，本机造不出来，`--generate` 会被明确拒绝 | 否 | 同一道 `signing_secret`，它是回落链**第三级**：上面两级任一非空就放行（换它动不到签名密钥），两级都空才读条数 |
| `SW_USE_FAKE_PUBLISHERS` | `true\|false`（`core/config.py:53`） | 是 | `--value false` 时先探人工确认闸门通道，不活就拒绝写入 |
| `SW_LLM_BACKEND` | `anthropic\|dsh`（`core/config.py:99` 的 `Literal`） | 是 | **两个方向都查**：目标后端的凭据必须在生产 `.env` 里存在且非空 |
| `SW_GENERATE_ENABLED` | `true\|false`（`core/config.py:82`） | 是 | 无（但有警告：它只停出稿，不停发布） |
| `SW_TELEGRAM_ENABLED` | `true\|false`（`core/config.py:351`） | 是 | `--value false` 时：真发布正开着就拒绝（别拆掉确认卡载体） |
| `WECHAT_AUTO_PUBLISH` | `true\|false`（`core/config.py:233`） | 是 | `--value true` 时：`WECHAT_CERTIFIED` 必须已经是 `true` |
| `WECHAT_CERTIFIED` | `true\|false`（`core/config.py:231`） | 是 | `--value true` 时有一道**从不拒绝**的闸门：它记的是微信那边的事实，本工具面核实不了，所以只把当场后果讲清（`WECHAT_AUTO_PUBLISH` 已是 `true` 时，这一个写入就让平台级自动发布成立），并据此禁掉 `--write-only`。**不能反过来要求「开关先为真」——那样两道合起来就是死锁，这一对永远上不去** |
| `DAILY_TOKEN_BUDGET` | 非负十进制整数（`core/config.py:369`） | 是 | 无（但有警告：`0` 是"当天全停"，不是"不限"） |
| `DAILY_RENDER_SECONDS_BUDGET` | 非负十进制整数（`core/config.py:370`） | 是 | 同上 |
| `DAILY_IMAGE_BUDGET` | 非负十进制整数（`core/config.py:375`） | 是 | 同上；另有等价别名 `SW_DAILY_IMAGE_BUDGET` |

**白名单写死，是为了逼出工作量，不是为了省事。** `docs/RISKS.md` 第 14 条的原话：补一个键
**不是往数组里加个元素**，而是要给它想清楚四件事——取值形状、`display` 策略、生效后怎么核验、
以及**这个键特有的闸门**。落到代码上就是五张按键表（`sw_env_policy` / `sw_env_value_re` /
`sw_env_value_help` / `sw_env_alias` / `sw_env_warn`）各补一格，漏一格脚本在 `set -e` 下当场退出。
`sw_env_policy` 那一格本身还不止一列——值来源 / `display` / 闸门 / 凭据文件键名 / 凭据来源 /
签名链上级，**当前有几列以那个函数的赋值为准**（`sed -n '/^sw_env_policy()/,/^}/p'
scripts/ops/env_set.sh`）。后两列是随凭据类键一起长出来的，各自替掉一处硬编码键名：
`POLICY_CRED_ORIGIN` 回答"这个值本机造得出来吗"（`local-csprng` / `external-issuer`），
`POLICY_SIGNING_ABOVE` 回答"它在签名密钥回落链上，上面还压着哪几级"。
`tests/ops/test_env_set.sh` 有一组源码级断言**数这五张表的分支**，并要求它们与
`SW_ENV_WHITELIST` 逐键一一对应，另有两条钉住 `sw_env_policy`：每个分支的格数必须相等，
`POLICY_CRED_ORIGIN` 的取值集合必须恰好是那三个（防止这一格塌成常量、把 `--generate`
的拒绝分支变成死代码）。少一格、多一格、拼错一个字都判红。

**凭据类键不再是"一律不加"。** 先补的是签名密钥回落链那三级；2026-08-26 又补了第四个`DEEPSEEK_API_KEY`，它**不在**那条链上，靠自己那道 `llm_key_live` 闸门够格——写之前在**本机**拿新值 `GET <baseURL>/models` 问一次网关，认才写（`47` = 网关拒绝，`48` = 探不到）。它是唯一一道跑在 `ssh` 之前的闸门，因为它要问的东西本机就答得了。边界写在 `docs/OPS.md` 那张表里：生产的 `SW_DSH_DEEPSEEK_BASE_URL` 不在白名单上、工具面读不到，所以闸门探的是值班机认识的那个网关，它会把 `host=` 打出来让人对一眼。 上一版这里写着
"`TELEGRAM_BOT_TOKEN` / `SW_TELEGRAM_SIGNING_SECRET` 刻意仍不在名单上……值得单独一批"，
那一批 2026-08-23 做掉了，那句话本轮更正：它们连同早就在名单上的 `SW_UI_TOKEN`，正好是
`core/telegram.py:151-154` 那条三级回落的第一、二、三级，因而共用 `secret` 策略、"人自己读
文件"的零回显流程，以及**同一道** `signing_secret` 事前闸门。当时那句"改签名密钥有 R1 邻近
副作用"也不再只是一句警告——它落成了闸门本身（见下面「签名密钥轮换闸门」）。

**没补进来的仍然没补，理由各自具体，不是"还没排上"：**

- **各种 API key**（`ANTHROPIC_API_KEY` 等）：直接后果是 `SW_LLM_BACKEND` 的闸门可能告诉你
  "`ANTHROPIC_API_KEY` 没配"，而**本工具面补不了它**。值班人拿得到一句精确的诊断、却拿不到
  一条能执行的处置，这个缺口在 `docs/RISKS.md` 第 14 条里如实登记着，不假装不存在。
- **`TELEGRAM_CHAT_ID`**：它**有理由地**不加，两条都是结构性的。① 闸门验证不了——这个键要
  问的是"换过去之后新会话真的收得到卡吗"，而那不真发一条 Telegram 消息就确认不了，一道
  答不了自己问题的闸门比没有闸门更糟（同 `wechat_claim` 那条的取舍）；② 方向反了——它的值
  是**从生产流出来**的（要在服务器上跑 `python -m core.telegram setup` 才知道是多少），而
  现有的按键表模型的是"本机拿一个值推到生产"，模型不了那个方向。

任意键编辑能力**刻意不做**。生产 `.env` 里有 LLM key、Telegram bot token、数据库 URL——
写坏任何一条 core 都起不来。传别的键一律拒绝，并在报错里说明为什么不该把它改成通用编辑器。

**红线 R7 的纵深防御**：`.env` 里出现 `DSH_` / `XDG_` / `DYLD_` / `BASH_FUNC_` 前缀的变量名
时 dsh 会拒绝启动且**没有开关**。白名单本来就排除了这种可能，所以这道校验今天是冗余的；
留着它是为了"将来有人扩白名单"，而且**本机与远端各有一道**。测试用改写过的副本把白名单
撑开一格，让这条路径真的可达，再分别断言两道校验各自都拦得住——否则它就是一段谁也没执行过
的代码。

### 值为什么不回显

按**键**决定，不是"看起来像密码就藏"的启发式——启发式的问题不是它今天判错，而是没人能
一眼核对它明天判不判得对。

- **`display=secret` 的那几个是凭据**，红线 R5：不进仓库、不进对话、不进 argv、不进日志。
  `--show` 只回答「已设置 / 未设置」，**连长度都不报**（长度也是信息）。它们也不接受
  `--value`：那会把凭据写进本脚本自己的 argv，而生产是合租机器、`/proc/*/cmdline` 世界可读
  （`docs/RISKS.md` §8.2）。当前是哪几个键从白名单派生、不手写第二份清单，脚本自己的报错
  文案就是这么打的（`sw_env_keys_where value_source credentials`）。
- **其余的都是布尔量 / 枚举 / 整数，不是凭据。** 它们的值就是运维要看的那条事实（"现在到底
  会不会真发"、"预算还剩多少"），藏起来只会逼人去别处猜。`verify.sh` 与 `status.sh` 早就在
  如实打印其中几个（`模拟发布器  True`），本脚本同一口径。

`--show` 只看白名单里那几个键（外加下面说的那个等价别名），`.env` 其余内容一个字节都不读进输出。
要查的键**从 `SW_ENV_WHITELIST` 派生**，不再手写第二份清单——键一多，"白名单加了键但
`--show` 看不见"就是迟早的事，而那种漏法无声无息。

### 取值为什么比 pydantic 还严（以及上一版这里写错了什么）

布尔键只认 `true` / `false` 两个**小写单词**，整数键只认无前导零的非负十进制数。

**这不是因为"写别的不生效"——那句话是错的，本轮已更正。** 本机实测：pydantic 确实认
`TRUE` / `True` / `1` / `yes` / `on` / `t`，整数侧也确实认 `-1` / `007` / `1_000_000`。
所以这里是**主动收紧**，理由三条，第三条最要命：

1. 同一个语义有六七种写法时，"这个开关到底是开是关"要靠记住 pydantic 的真值表才能回答，
   而这恰恰是运维最需要一眼看懂的东西；
2. `--show` 与 `verify.sh` 打印的是 `.env` 原文，写法不统一时人得在脑子里做一次转换；
3. **`0` 和 `1` 在这张白名单上是歧义的**：三个 `DAILY_*_BUDGET` 就在同一张表里，那里 `0` 是
   整数"当天全停"，而在布尔键上 `0` 是 `false`、`1` 是 `true`。同一个字符在相邻两个键上意思
   完全不同，抄错一次就是一次静默事故。

整数侧最该拦的是 `-1`：pydantic 认它（得到 `-1`），而 `core/budget.py:118-122` 的
`remaining() = max(limit - used, 0)` 让负上限与 `0` 一样是**全停**。有人写 `-1` 想表达"不限"
时会得到"全停"——这是反向故障。**本仓没有"不限"这个语义，任何哨兵值都没有**；要放开就把上限
调大。

**`DAILY_IMAGE_BUDGET` 有一个等价别名 `SW_DAILY_IMAGE_BUDGET`**（`core/config.py:375-378` 的
`AliasChoices`）。`--show` 会把别名也查一遍：只写了别名时它会明说"**生效的是它**，不是出厂
默认值"；两个都写了时明说"主名赢，别名那行是死配置"（本机实测过 `AliasChoices` 的优先级）。
少了这一格，`--show` 会对一个其实配置过的键回答"未设置"——一次很确定的错答。

### token 从哪来、人怎么拿到（红线 R5 下的完整答案）

启用 `SW_UI_TOKEN` 之后，所有经 IAP 隧道访问 `/api/v1/*` 的一方都要带
`Authorization: Bearer`：工作台前端、`scripts/workbench_mcp.py` 工具面，以及所有
`source` 了 `ui_token.sh` 的 ops 脚本（名单以本文件开头那条 `grep -l` 为准，这里不写个数）。
而红线 R5 规定凭据不进对话，所以**没有任何人可以把生成的 token 念给用户听**。于是 token
的流向被设计成"人自己去读文件"，全程零回显：

```bash
# ① 本机生成 → 写 ~/.dsh-sw/.credentials.yaml（0600）→ 推生产 .env → 重建 → 走闸门
bash scripts/ops/env_set.sh --key SW_UI_TOKEN --generate
# ② 本机已有一个值（环境变量 SW_OPS_UI_TOKEN 优先，其次凭据文件），推上去收敛
bash scripts/ops/env_set.sh --key SW_UI_TOKEN --from-credentials
# ③ 人要用它的时候，自己读那个文件
cat ~/.dsh-sw/.credentials.yaml
```

几个刻意的决定：

- **先本机落盘，后推远端。这个顺序不对称，反过来会自锁。** 本地成功、远端失败 = 本机多持有
  一个生产没有的值，`require_token` 在未启用时直接 return，多余的头被忽略，无害，重跑
  `--from-credentials` 即可收敛；远端成功、本地失败 = 生产要求一个**没有人持有**的 token，
  工作台、MCP 工具面、所有 `source` 了 `ui_token.sh` 的脚本同时被 401 挡在门外，而唯一能恢复它的
  路径也要经 SSH。
- **凭据文件已存在**：只追加一行，其余内容逐字保留（同目录临时文件 + `mv`，原文件末尾没有
  换行时先补一个，绝不粘连）。
- **凭据文件里已有这个键对应的那一行**：`--generate` **拒绝执行**。覆盖一个还在用的凭据不
  可逆，旧值盖掉就再也拿不回来，而生产上可能正用着它。要换新的，请人自己确认后删掉那一行；
  只想让两边一致，用 `--from-credentials`。键名不写死在实现里，由 `sw_env_policy` 的
  `POLICY_CRED_KEY` 那一格给（`SW_UI_TOKEN` → `sw_ui_token`，其余键各有各的），
  所以这条规则对每个凭据类键各管各的那一行，互不串门。
- **两边不一致怎么办：不比对、不打印，靠 401 判。** 比对需要把两个值放在一起，任何一次回显
  都踩 R5。收敛动作是 `--from-credentials`，然后跑 `bash scripts/ops/verify.sh`——探针 200 就
  是一致，401 就是不一致，各脚本在 401 上都已有指向根因的提示（逐条见上面「不配会怎样 / 配错会看到什么」）。
- **熵源**：`openssl rand -hex 32`，退路是 `/dev/urandom` + `od`，两条都拿不到就**拒绝生成**，
  绝不退化到 `$RANDOM`（线性同余，可预测）。选 hex 不选 base64 的理由：hex 的 `0-9a-f` 是
  上面「字符集限制」那个白名单的真子集，与 curl 配置流、`printf '%q'`、波浪号展开全都无关；
  长一倍的代价为零，因为 token 从不需要人肉输入。

**换 `SW_UI_TOKEN` 有一条容易被忽略的副作用**：生产 `.env` 里 `SW_TELEGRAM_SIGNING_SECRET`
为空时（`.env.example` 的默认形态就是空），Telegram 确认卡 `callback_data` 的 HMAC 签名密钥
按 `SW_TELEGRAM_SIGNING_SECRET` → `SW_UI_TOKEN` → `TELEGRAM_BOT_TOKEN` 的顺序回落
（`core/telegram.py:151-154`）。改了 `SW_UI_TOKEN` 就等于换了签名密钥，**此前已推出去、
还没人点的确认卡按下去会验签失败**（`core/telegram.py:901` 起，日志记 `bad_signature`，
用户侧表现为按钮没反应），最终被 TTL 自动驳回。

**上一版这里写的是"动手前先确认没有待确认条目"，本轮更正：那条前置不再靠人记得了。**
2026-08-22 生产上启用鉴权时它就被跳过过一次（事后查证是 0 条、没造成损失，那是运气），
所以它已经落成脚本自己的 `signing_secret` 事前闸门——见下面「签名密钥轮换闸门」。

### 翻 `SW_USE_FAKE_PUBLISHERS` 时闸门怎么走

```bash
bash scripts/ops/env_set.sh --key SW_USE_FAKE_PUBLISHERS --value false   # 真发布开启
bash scripts/ops/env_set.sh --key SW_USE_FAKE_PUBLISHERS --value true    # 退回模拟发布器
```

一次调用里发生的事，按顺序：

1. 本机校验（白名单 / R7 / 值只接受 `true`\|`false`，大小写敏感）；
2. 一次 SSH：远端再校验 → **备份 `.env`** → **原子写入** → `docker compose up -d
   --force-recreate --no-build core`；
3. 本机调 `bash scripts/ops/restart.sh`：它备份数据库与台账、重启 core、探针确认，并跑那道
   **R1 红线闸门**——真发布开启时要求人工确认闸门通道 `enabled && ready && polling` 三者皆真；
4. 闸门不过 → 本脚本以失败收尾（**fail-closed**），绝不打印成功行，并给出可直接粘贴的反向
   命令与 `.env` 备份路径。

#### 两道闸门，不是一道：事前预防 + 事后检测

**本文档上一版说"没做成事前预防"，那已经不成立了——现在事前那道也在。** 顺序如下：

| | 什么时候判 | 判什么 | 判红的代价 |
| --- | --- | --- | --- |
| **事前预防** | 写 `.env` **之前** | 探 `GET /api/v1/system/telegram`，`enabled` / `ready` / `polling` 三者必须皆真 | 零。`.env` 一个字节没动、**连备份都没建**、容器没重建过，core 根本不知道有人来过 |
| **事后检测** | 重建容器**之后** | 调 `scripts/ops/restart.sh`，走它那道既有的 R1 闸门（判据逐字相同） | 带着新值的 core 已经在跑了（`docs/RISKS.md` 第 12 条同一条局限） |

**两道都在，谁也不替换谁。** 事前拦住的是**可预见**的那一半（动手时通道就已经死了）；事后
兜住的是**写入到生效之间**发生变化的那一半——探针到容器重建之间隔着几秒到几十秒，Telegram
那边随时可能掉线。事前拦不住后半段，事后拦不住前半段。

事前那道之所以从前没做，理由是"要在 `env_set.sh` 里内联第五份 `sw_probe`"。`sw_probe` 收成
单一真相源之后（见上面「工作台 API token」那节末尾），这个理由不成立了，于是补上。
`docs/RISKS.md` §12.3 记着这条因果。

**两个退出码，`37` 与 `38`，含义不同，处置也不同——刻意分开，不许靠 grep 中文文案区分：**

| 退出码 | 含义 | 下一步 |
| --- | --- | --- |
| `37` | **探到了，通道不行**：`enabled` / `ready` / `polling` 里有假。事实明确 | 去修那一格（`enabled` / `ready` 看服务器 `.env` 的 `SW_TELEGRAM_*`；`polling` 看正在跑的那份代码），或者先别开真发布 |
| `38` | **没探到**：连不上 / 超时 / **401** / 响应解析不了。也就是"不知道" | 401 那格：`export SW_OPS_UI_TOKEN=<生产 .env 里同一个值>`；其余：先跑 `status.sh` 与 `verify.sh` 看 core 到底什么状态 |

**"探不到"为什么也拒绝（fail-closed），理由四条：**

1. 这个方向是整条风险登记册里最危险的一次切换。放行等于在**无法证明** R1 红线的主载体活着的
   情况下把它打开；
2. 探针打的是同一台机器上的 loopback，探的是一个"本来就必须活着"的服务。探不到本身就是一个
   强信号，不是噪声；
3. **放行的收益近乎零**：紧接着的 `restart.sh` 会用**同一条**探针再问一次，大概率同样探不到
   而失败。区别只在于那时 `.env` 已经是 `false`、容器已经带着它重建过了。也就是说 fail-open
   换来的不是"能办成事"，而是"**以更坏的姿势失败**"；
4. 拒绝的代价可逆且很小：修好凭据/网络再跑一次，什么都没发生过。

**反方向（`--value true`，关掉真发布）永远不受这道闸门约束**——出事时人必须能退回安全状态，
绝不能被一道闸门锁死在危险状态里。

**事前那道要带 token。** core 启用 `SW_UI_TOKEN` 之后，不带 `Authorization` 头的探针一律 401，
所以 `--value false` 这条路径会先在本机取 token（`SW_OPS_UI_TOKEN` > 凭据文件），再经 stdin
流把它送到远端。少了这一步，事前闸门在一台已启用鉴权的生产上会永远 fail-closed 在 401 上，
而它给出的处置指引根本不会生效——那等于把人锁死在"关不掉假发布器"的状态里。
**取不取 token 是按「这次会不会跑一道要打 `/api/v1` 的闸门」决定的，不是按键名。** 现在有
两道这样的闸门：这一道，以及三个凭据类键共用的 `signing_secret`（它要读待人点的确认卡条数）。
其余每一条路径一个请求都不多打：`--show`、`--value true`、以及那几道只读 `.env` 的闸门。
**上一版这里把 `SW_UI_TOKEN` 列进"不取 token"那一档，本轮更正**——它现在有 `signing_secret`
闸门，因此会取。判据在脚本里就是 `GATE_REAL_PUBLISH || GATE_SIGNING_SECRET` 这一个条件。

把事后那道的局限压到最小的三件事：

- **任何会触发事前闸门的方向都禁用 `--write-only`**（措辞从"只禁 `SW_USE_FAKE_PUBLISHERS
  =false"推广到了每一道闸门，理由是同一条；不写道数，判据在脚本里就是 `ACTIVE_GATE != none`
  这一个条件）。`.env` 已是新值而运行中的 core 还是旧值，是一个
  **上了膛没有击发**的状态——此后任何人一次寻常的 `docker compose up -d`（第 12 条明写它绕得
  过所有闸门）都会静默把新值带上来，而那一刻没有任何闸门在场。强制在本次调用、有人盯着的
  时候把它走完，闸门就一定会跑。
  **没有闸门的方向照旧允许 `--write-only`**（关真发布、装回 Telegram 载体、改预算、停生成）
  ——把禁用无差别推广开只是添乱。
  **三个凭据类键因此一律不能 `--write-only`**：`signing_secret` 没有方向可言、无条件点亮。
  这条在 `TELEGRAM_BOT_TOKEN` 上尤其要紧，而且比喻要换一个——签发方一发新值、**旧 token
  当场作废**，所以它身上的"`.env` 已改、容器没重建"不是"上了膛没击发"，是**当场哑火**：
  运行中的 core 攥着一个已经失效的凭据，从这一刻起一张卡都推不出去，而 `/api/v1/system/telegram`
  的 `polling` 只看轮询线程活没活（线程捕到错误只退避重试、不退出），照样报 `true`。
  **`verify.sh` 新增的那道假活判据在这一格上恰恰救不了你**：它的判据是 `stats.polls==0`，
  而这台 core 在旧 token 被作废之前已经成功轮询过无数次，`polls` 会**冻在一个大数上**，
  与"健康 + 历史抖动过几次"在单次快照里无法区分（理由见下面 `verify.sh` 那节）。
  也就是说这条路上没有任何一格会因此变红——禁掉 `--write-only` 是唯一挡着它的东西。
- 闸门不过就以失败收尾，绝不打印成功行。
- **绝不自动回退**，只给文本命令——与 `update.sh` 的 `rollback_hint` 同一契约。自动回退意味着
  在一次已经出错的流程里再自动改一次生产 `.env`、再重建一次容器，而"闸门为什么红"还没人看过。

#### 只读远端 `.env` 的那几道事前闸门（随白名单扩容一起加的）

**每个键有哪道闸门以 `sw_env_policy` 的第三格为准**（`sed -n '/^sw_env_policy()/,/^}/p'
scripts/ops/env_set.sh`），这里不写道数。下表是**只读远端 `.env`** 的那几道：
不 curl、不进容器、不需要 token。三条理由，第三条是决定性的：
① 时点对得上——本脚本紧接着就会 `up -d --force-recreate`，容器环境在创建时由 `env_file: .env`
定型（`docker-compose.yml` 的 core 服务，`environment:` 块里没有任何一个白名单键），所以
`.env` 里的值正是**这次变更落地之后** core 会看到的东西；② 少一层依赖；③ `SW_LLM_BACKEND` 的
典型场景就是"dsh 挂了要回退"，要是闸门得靠 `docker compose exec core` 才能判，容器起不来的
时候闸门也判不了，人就被锁在坏状态里——闸门反过来挡住了恢复动作。

| 退出码 | 闸门 | 触发方向与判据 | 为什么是拒绝 |
| --- | --- | --- | --- |
| `39` | `confirm_carrier` | `SW_TELEGRAM_ENABLED=false` 且 `.env` 里 `SW_USE_FAKE_PUBLISHERS=false` | 见下 |
| `40` | `confirm_carrier` | 同方向，但 `SW_USE_FAKE_PUBLISHERS` 缺行 / 写法不是 `true`\|`false`，也就是"不知道" | fail-closed，同 `38` 那套理由 |
| `42` | `llm_backend_creds` | 目标后端的凭据在 `.env` 里缺失或为空（`anthropic` → `ANTHROPIC_API_KEY`；`dsh` → 由 `.env` 的 `SW_DSH_PROVIDER` 经 `configs/dsh/cordis.yml` 的 `apiKeyEnv` 决定） | `generation/llm.py:271-278` 是**懒加载**：缺 key 时 core 照常起来，直到第一次真出稿才抛 `LLMUnavailable`。一次没凭据的"回退"不会当场报错，它把故障推迟到排期里，**比不回退更糟** |
| `43` | `wechat_certified` | `WECHAT_AUTO_PUBLISH=true` 而 `.env` 里 `WECHAT_CERTIFIED` 不是 `true` | 不是因为危险，是因为**没用**：`publishers/wechat_mp/publisher.py:238-249` 的双确认闸门要三者皆真，认证那格是假时这次变更是个**不会生效的空操作**，而人会以为自动发布已经开了。`scripts/preflight.py:122-133` 对同一组合的裁定同样是 `FAIL` |
| **没有** | `wechat_claim` | `WECHAT_CERTIFIED=true`，去 `.env` 读 `WECHAT_AUTO_PUBLISH` | **它从不拒绝，所以不需要退出码**——`44` 因此空着，**那个空位就是这个结论的痕迹，别顺手补一个**。它要问的"这个号凭什么算已认证"是本工具面结构上答不了的（那是微信那边的事实），而一道答不了自己问题的闸门比没有闸门更糟。它存在是为了两件闸门以外的事：把"这次写入会不会让平台级自动发布当场生效"讲清，以及顺带禁掉 `--write-only` |

**`SW_TELEGRAM_ENABLED=false` 那条的因果必须写准（本仓在这上面栽过一次）。** 关掉 Telegram
**不等于**内容会越权发出去：

- `core/telegram.py:650-654` `build_telegram_notifier()` 返回 `None` → 一条确认卡都推不出去；
- `core/scheduler.py:498-505` 的人工确认闸门看的是 `item.confirmed_at`，没人点就**跳过不发**
  （记 `skipped_unconfirmed`）→ **R1 红线不因此失效**；
- 工作台的「确认发布」按钮不受 Telegram 影响，走的是**同一个后端函数**
  （`core/api/content.py:283-297` → `core/confirm.py:315` `confirm_item`）；
- 真正的后果是**静默停摆再静默丢弃**：内容堆在排期处，而 `core/confirm.py:571-573` 的 TTL 在
  一次都没推成功过时从 `scheduled_at` 起算，到点（`SW_CONFIRM_TTL_HOURS`，默认 24 小时）
  `expire_confirmation` 自动驳回并释放槽位。

**为什么做成拒绝而不是只警告**——判据是可验证的，不是口味：真发布开着而 `enabled=false`
这个组合会被 `restart.sh` 的 R1 闸门**必然**判红，本脚本随后失败收尾。放行 = **必然**在改完
生产之后失败，也就是上面第 3 条那句"以更坏的姿势失败"。而拒绝**不会把人锁死**，出路是两步，
中间那一步还让生产更安全：

```bash
bash scripts/ops/env_set.sh --key SW_USE_FAKE_PUBLISHERS --value true   # 这条没有闸门
bash scripts/ops/env_set.sh --key SW_TELEGRAM_ENABLED --value false
```

**为什么必须重建容器而不是只 `restart`**：容器的环境变量在**创建时**定型——compose 把
`env_file: .env` 解析进服务配置、写进容器的 `Config.Env`，之后再没有任何 API 能改它。
`docker compose restart` 只是 restart 那个已存在的容器，**读不到新的 `.env`**（compose 官方
文档对 `restart` 的说明就是"配置改动不会被这条命令反映出来"）。所以 `restart.sh` 那条
`docker compose restart core` 单独用**改不动任何 `.env` 变更**。代价是 core 会被动两次
（本脚本重建一次 + `restart.sh` 再 restart 一次）：第二次功能上多余，换来的是"闸门的实现
只有一份、且是已经在生产上验证过的那一份"。core 冷启动是秒级，而这本来就是一次有人盯着的
停机变更。

### 签名密钥轮换闸门（`signing_secret`，三个凭据类键共用）

**它拦的是一件很容易被当成"只是换个 token"的事。** 确认卡 `callback_data` 的 HMAC 签名密钥
是三级回落（`core/telegram.py:151-154`，每一级都是 `(… or "").strip()`，取第一个非空的）：

```text
一级 SW_TELEGRAM_SIGNING_SECRET → 二级 SW_UI_TOKEN → 三级 TELEGRAM_BOT_TOKEN
```

换掉**当前生效的那一级**就是换签名密钥，**已推出去、还没人点的卡按下去会验签失败**
（日志 `bad_signature`，用户侧表现为按钮没反应），最终被 TTL 自动驳回。2026-08-22 生产上
真发生过一次：换 `SW_UI_TOKEN` 时一级为空，生效的那级从三级跳到二级。事后查证是 0 条，
**那是运气**——`docs/RISKS.md` §8.5 把"先确认没有待人点的确认卡"定为第 0 步前置，而那是
**人工**前置，被跳过了。一条只写在文档里、没人执行也没人发现的前置，等于没有前置。

#### 什么时候会拦、什么时候直接放行（一条规律，不用背表）

**回落链上排在本键前面的那几级里，但凡有一级非空，这次写入就动不到生效的密钥**——那时回落
在更高的一级就停住了，够不到本键。表里六行只有这一条规律：

| 要改的键 | 一级 `S` | 二级 `U` | 生效密钥现在来自 | 这次写入会不会换掉它 |
| --- | --- | --- | --- | --- |
| `SW_TELEGRAM_SIGNING_SECRET` | 任意 | 任意 | 任意 | **会**（写完必然是一级；**没有免检方向**） |
| `SW_UI_TOKEN` | 非空 | 任意 | 一级 | 不会 → 放行，不设闸门 |
| `SW_UI_TOKEN` | 空 | 任意 | 二级（本键）或三级 | **会** → 去读待人点的确认卡条数 |
| `TELEGRAM_BOT_TOKEN` | 非空 | 任意 | 一级 | 不会 → 放行，不设闸门 |
| `TELEGRAM_BOT_TOKEN` | 空 | 非空 | 二级 | 不会 → 放行，不设闸门 |
| `TELEGRAM_BOT_TOKEN` | 空 | 空 | 三级（本键） | **会** → 去读待人点的确认卡条数 |

判空与 core 同口径（`or ""` 再 `.strip()`）：裸空、纯空白、`""`、`''` 四种都算未设置；远端
只有 `signing_level_is_set` 一份定义。两个推论别绕过去：**第一级上面没有任何一级，所以它
永远没有免检方向**；**第三级今天几乎总会走"放行"那一格**（生产一级为空、二级非空），
但"一级二级都空"正是 `.env.example` 的出厂形态，闸门缺了这一格就是一个**活的**缺口。

**远端那道闸门里一个键名都不认识。** 它按 `sw_env_policy` 的 `POLICY_SIGNING_ABOVE`
（按级序排的键名表）循环，命中第一个非空的就停——那正是 core 的取值顺序，所以"停在第几级"
这句话是准的。将来回落链多一级，改那一格即可，闸门代码一个字不用动。

#### 判定分四段，顺序有讲究

1. **值没变就放行，且不探测。** 这一格不是优化，是**防自锁**：上一次调用若正好死在"写入
   成功、容器重建失败"之间（退出码 `36`），`.env` 已是目标值而运行中的 core 还不是，重跑本
   命令是唯一的收敛动作；让闸门去挡它，等于闸门反过来锁死了恢复路径。签名密钥在这条路径上
   一个比特都不会变。
2. **上级非空免检**（见上面那张表）。这一格就是 `SW_TELEGRAM_SIGNING_SECRET` 那个键的全部
   价值，也是本闸门唯一的**永久解法**。改一级本身时**没有这一格**。
3. **读待人点的确认卡条数，`0` 条放行。**
4. **`>0` 拒绝，退 `45`；读不出来 fail-closed 拒绝，退 `46`。** 两个码刻意分开：**"探到了，
   有卡"与"没探到，不知道"是两件事，处置动作不同**，不许靠 grep 中文文案区分（同 `37` / `38`、
   `39` / `40` 那两对）。

条数的口径是 `counters.awaiting_confirm`（`core/api/dashboard.py::_awaiting_confirm`），
读数只有一份定义（`ui_token.sh` 的 `sw_ops_emit_awaiting_confirm_definition`，`verify.sh`
用的是**同一份**）。它是**上界**：不看 `confirm_pushed_at`，还没推出卡的条目也计在里面，
而那些卡是换密钥之后才生成的、签的是新密钥、不会失效。当闸门用，偏大的方向恰好是安全的；
但**文案里绝不许写成"这么多条会失效"**，那是把上界当精确值卖。

**"读不出来"为什么也拒绝**：这一格**没有事后闸门兜底**——`restart.sh` 那道 R1 闸门问的是
"确认通道活不活"，它对"卡还能不能验签"一无所知，判绿也说明不了任何事。事前这一道就是
唯一的一道，在**无法证明**没有卡在等人点的前提下换密钥，等于把一次可预见的破坏交给运气。

#### 被拦住之后有四条出路（闸门给得出诊断，就得给得出处置）

```bash
# 处置一（等）：等卡被人点掉，或被 SW_CONFIRM_TTL_HOURS（默认 24 小时）到点自动驳回，再重跑。
bash scripts/ops/verify.sh          # 「待人点的确认卡」那一格与闸门读的是同一个数

# 处置二（根治，只做一次）：把回落链第一级显式设上，此后二、三级再怎么换都不动签名密钥。
bash scripts/ops/env_set.sh --key SW_TELEGRAM_SIGNING_SECRET --generate
# ↑ 注意这一条**本身**也走同一道闸门（它就是在改签名密钥）。挑一个 0 条的时刻做它。

# 处置三（明知故犯）：名字说的就是后果。输出里会如实记下你接受的是哪一批。
bash scripts/ops/env_set.sh --key SW_UI_TOKEN --generate --accept-breaking-pending-confirm-cards
```

第四条是重试的姿势：`--generate` 推送失败之后重试要用 **`--from-credentials`**，不是再
`--generate` 一次——新值已经落在本机凭据文件里了，再 `--generate` 会被"这个键已有一行"挡住。

**`--accept-breaking-pending-confirm-cards` 同时覆盖 `45` 与 `46`，后半是被逼出来的，不是宽容。**
`--from-credentials` 的头号用途就是"两边 token 不一致的收敛"，而两边不一致时探针拿到的
**必然**是 401，也就是 `46` 那一档；这一档不给出路，那条收敛命令就永远跑不成，闸门恰好锁死
了它本该保护之物的恢复路径（老版本 core 没有 `/api/v1/dashboard`、容器起不来同理）。两档
记下来的话**如实不同**：有条数就记条数并注明它是上界，没条数就明说"你接受的是一个**未知数**，
不是一个已知的小数字"。这个旗子给到别的键上会被**拒绝**，不是静默忽略——静默忽略会让人
以为自己已经绕过了某道闸门。

#### 换完 `TELEGRAM_BOT_TOKEN` 之后还要看一格：409 双轮询

**同一个新 token 被粘进两个部署**时，两边都会 `getUpdates`，Telegram 只喂一个，另一边持续
`error_code=409`（`docs/RISKS.md` 第 1 条的老账）。所以换完跑一次 `bash scripts/ops/verify.sh`，
看「Telegram 轮询冲突（`error_code=409`）」那一格。真撞上了，**处置是让另一个部署停下来、
或给它换一个 bot，而不是再换一次 token**——再换一次只会把冲突原样搬到新 token 上。

**这一格刻意只提示、不设闸门。** `verify.sh` 数的是近 2000 行日志里的 `error_code=409`，
那是个比 `awaiting_confirm` 弱得多的信号，**两个方向都会错**：历史行会把新冲突淹掉（旧账让它
假红），而真正的新冲突要等下一次轮询失败才写得进日志（时间窗让它假绿）。既漏又误的判据不该
拿来拦人——这与上面那道闸门选 `stats.polls` 当判据是同一条取舍。

### 备份在哪

`backup.sh` 备份的是数据库与台账（`social_workflow.db` + `accounts.yaml`），**不含 `.env`**。
所以 `env_set.sh` 自己备份，**备份先于写入**，拿不到备份就绝不写：

```text
远端 ~/sw-env-backups/env-<UTC时间戳>     文件 0600，目录 0700
```

放在 `~/sw-env-backups` 而**不是** `~/social_workflow` 下，是因为后者是 git 工作树：
`.env.bak-*` 这类文件不在 `.gitignore` 里，会让 `verify.sh` 判「工作树不干净」、让 `update.sh`
拒绝部署。写入是**原子的**：同目录临时文件 → `chmod 600` → `mv`（同目录 `mv` 是 `rename(2)`），
`.env` 在任何一瞬间要么是旧的完整内容、要么是新的完整内容，绝不会是半截——就地编辑写到一半
断链就会把 `.env` 弄坏，而 `.env` 坏了 core 起不来。临时文件由 `trap` 在任何退出路径上清掉。

值与 `.env` 里原有的值相同时**不备份也不写**（没有需要回退的东西），但**仍然重建容器**：
本脚本的契约是"让这个键在运行中的 core 上等于这个值"，不是"编辑一个文件"。

### 失败了怎么办

**本脚本对任何失败都不重试**（与 `backup.sh` / `restart.sh` 的一次重试刻意不同：那两个重试的
是只读或幂等动作，而重跑一次 `.env` 编辑是"又一次生产写入 + 又一次容器重建"）。

| 现象 | 含义与处置 |
| --- | --- |
| 本机就报错（白名单 / R7 / 值形状 / token 字符集 / 没有熵源） | 生产一个字节都没动，备份与 SSH 都没发生。按提示改参数重来。 |
| `--generate` 说凭据文件里已有这个键那一行 | 生产没动，凭据文件也没动。改用 `--from-credentials`，或人工确认后删掉那一行。**`--generate` 推送失败之后的重试也走这一条**：值已经落盘了，再 `--generate` 一次只会撞上同一句拒绝。 |
| `--generate` 说这个键本机造不出来 | `TELEGRAM_BOT_TOKEN` 的值由 BotFather 签发（策略表第五格 `POLICY_CRED_ORIGIN=external-issuer`），所以它**没有 `--generate`**。人去 `@BotFather` 取，自己写进 `~/.dsh-sw/.credentials.yaml` 的 `telegram_bot_token` 键（0600），再 `--from-credentials` 推上去。 |
| 远端 `.env` 不存在 | 什么都没做。本脚本只改已有的 `.env`，不凭空造一个。先用 `status.sh` 确认部署目录。 |
| 同名键出现多次 | 什么都没做。dotenv 语义是后一条覆盖前一条，只改第一条会**写成功却不生效**。删多余行是破坏性操作，交给人。`--show` 会把重复次数打出来。 |
| 备份失败 | **`.env` 没动**。查远端 `~/sw-env-backups` 权限与磁盘水位（`status.sh` 有磁盘水位一段）。 |
| 写入失败 | `.env` 要么还是旧的完整内容、要么已是新的完整内容，不会是半截。万一残留 `~/social_workflow/.env.sw-ops-tmp.<pid>`，删掉它，否则 `verify.sh` 会判「工作树不干净」。 |
| 容器重建失败 | **半截状态**：`.env` 是新的、运行中的 core 还是旧的。查 `docker compose ps` / `logs core`；镜像缺失走 `update.sh --apply`（构建是它的职责，本脚本刻意带 `--no-build`）。 |
| 事前闸门判红（退出码 `37`） | 探到了，人工确认闸门通道不活。**`.env` 一个字节都没动**，没有备份、没有重建容器，真发布**没有**被打开。修好那一格再重跑，本次没有需要回退的东西。 |
| 事前闸门探不到（退出码 `38`） | 连不上 / 超时 / 401 / 解析不了，也就是"不知道"。同样 fail-closed，生产维持原状。401 那格补 `SW_OPS_UI_TOKEN`，其余先跑 `status.sh` / `verify.sh`。 |
| 载体闸门判红（退出码 `39`） | 真发布正开着，拒绝关掉 Telegram 确认卡载体。生产维持原状。出路见上面那两条命令。 |
| 载体闸门读不出（退出码 `40`） | `.env` 里 `SW_USE_FAKE_PUBLISHERS` 缺行或写法不认识。先把它显式写清楚（`--value true` 更安全）再来。 |
| 后端凭据闸门判红（退出码 `42`） | 目标后端的凭据缺失或为空。生产维持原状。**本工具面补不了那个凭据**——`ANTHROPIC_API_KEY` 这类 key 不在白名单上（进了名单的凭据类键只有签名密钥回落链那三级），得先把它写进生产 `.env`。 |
| 公众号认证闸门判红（退出码 `43`） | `WECHAT_CERTIFIED` 不是 `true`，这次变更本来也不会生效。账号确实已认证的话先跑 `env_set.sh --key WECHAT_CERTIFIED --value true`（**先记事实，再开开关**；那一步不会被反向拦住，理由见键表里那一行），再重跑本命令。 |
| 签名密钥闸门判红（退出码 `45`） | 探到了：还有待人点的确认卡，拒绝换签名密钥。**`.env` 一个字节都没动**，没有备份、没有重建容器。四条出路见上面「签名密钥轮换闸门」。 |
| 签名密钥闸门读不出（退出码 `46`） | 读不出待人点的确认卡条数（401 / 404 / core 没起来），也就是"不知道"，fail-closed。401 那格补 `SW_OPS_UI_TOKEN`；**这一档也能被 `--accept-breaking-pending-confirm-cards` 覆盖**，否则"两边 token 不一致"这种必然 401 的情形永远收敛不了。 |
| `restart.sh` 未通过 | `.env` 已改且已生效。看 `restart.sh` 自己的输出判断红在哪一格；若红的是 R1 闸门，反向命令是 `env_set.sh --key SW_USE_FAKE_PUBLISHERS --value true`。 |
| SSH 断链 / 远端 254 | **生产状态不明**，且刻意不自动重试。先跑 `env_set.sh --show` 看 `.env` 现在到底是什么状态，再决定下一步。 |

任何一条失败路径都不会回显凭据值。

## verify.sh：只读部署核验

`update.sh --dry-run` 也会打印当前提交，但它会先调用 `backup.sh` 创建并轮转备份，属于写操作，
不适合当取证工具。`verify.sh` 是纯只读的那一件：**一次 SSH 往返**采集全部证据，可重复运行、
无任何副作用。它不调用 `backup.sh`，不执行 `git fetch/pull/merge/checkout/reset`，不执行
`docker compose build/up/restart/down/stop`，不写任何远端文件，也不读取或打印 `.env`、token、
密钥或 `accounts.yaml`。它同样不含、也不会新增任何"确认发布"能力——那是人工闸门。

**上面这条只读、不泄密的承诺只对主路径（不带 `--preflight`）成立。** 一旦加上 `--preflight`，
它会在容器内执行 `scripts/preflight.py`，而该脚本本身会读 `.env`（`check_env_file`，
`scripts/preflight.py:47-56`）、会从生产向外部服务发出真实 HTTP 请求（`_probe_wechat_token`
探公众号 access_token，`scripts/preflight.py:143-171`；Pexels / Pixabay / dsh / TrendRadar
同理），并会打印密钥的脱敏指纹——成功分支打印 `sk-ant-…<后四位>`
（`scripts/preflight.py:76`），格式校验失败分支还会打印 `key[:7]`。这些都只在显式传
`--preflight` 时才会发生；主路径（默认，不带 `--preflight`）的只读与不泄密承诺依然成立。

`--sha` 可选。给了就严格要求生产 HEAD 与它逐字符相等；必须是 40 位小写十六进制完整提交，短
SHA、大写、非十六进制、重复指定和缺值都会在本机、在发起 SSH 之前被拒绝。不给时只如实打印
HEAD，不做比对。

`--preflight` 可选，**默认不跑**。容器内 `scripts/preflight.py` 会做外部连通性探测，历史上跑到
超时把上游会话卡死过，所以必须显式 opt-in。给了才执行
`docker compose exec -T core python3 scripts/preflight.py` 并如实打印其退出码；退出码非 0 计入
失败项。

一次 SSH 内按顺序采集：

1. **Git 部署核验**：当前分支（detached 时打印 `<detached HEAD>`）、HEAD 完整 SHA、
   `git status --porcelain` 是否为空、给了 `--sha` 时的相等性，然后是**发布线**与**部署标记**
   两段（下面单独说）。全部只用本地已有的 remote-tracking ref，**不 fetch**——fetch 会改写
   本地 refs，就不是只读了。**落后不算失败**，如实打印。
2. **容器与端口门禁**：打印 `docker compose ps`，随后校验 `docker compose port core 8000`，
   规则与 `update.sh` 完全相同——恰好一条 `127.0.0.1:<port>` 或 `[::1]:<port>`，端口必须是
   无前导零的十进制 `1..65535`；空值、多行、CR、公网地址和畸形端口一律拒绝。
3. **健康与运行时探针**：`curl -fsS --max-time 10 http://<loopback>:<port>/health` 必须返回 200
   （`/health` 在 DB 不可用时返回 503，`curl -f` 会据此失败）；
   随后取 `/api/v1/system/info`，交由 `docker compose exec -T core python3` 解析并逐行打印
   `version / env / time / timezone / scheduler_enabled / generate_enabled /
   use_fake_publishers / auth_required / publishers`。
4. **人工确认闸门通道（Telegram）**：取 `GET /api/v1/system/telegram`
   （`core/api/system.py::telegram_info`，**不发任何网络请求**，只读配置 + 本进程轮询线程
   状态），逐行打印 `enabled / configured / ready / chat_configured / can_sign / polling /
   username / sent / failed / detail`，再打印**轮询实况**（`stats.*` 与 `last_error`）与
   下面说的那条假活判据；探针本身失败或返回 JSON 不可解析时如实打印
   `<无法获取 /api/v1/system/telegram>` / `<JSON 解析失败>`，同样并入下面的裁定。裁定按
   互锁进行，松紧完全由上一步 `/api/v1/system/info` 里的 `use_fake_publishers` 决定：
   - `use_fake_publishers=true`（当前生产既定状态）：通道状态（含探针失败的情形）**如实
     记录，不构成失败项**；
   - `use_fake_publishers=false`（真发布已开启）**或该值取不到**（含 `/system/info` 探针
     失败、JSON 不可解析等取不到布尔量的情形，按同一条从严规则处理）：要求 `enabled`、
     `ready`、`polling` **三者皆真**，否则判失败。真发布开着而确认闸门通道是死的，后果
     **不是**内容会越权发出去——恰恰相反，内容会**发不出去**：`core/scheduler.py:498-505`
     里 `tick_scheduled_publish` 的**人工确认闸门**（锚点用函数名 + 条件表达式，行号会漂：
     `confirm_required(policy) and item.confirmed_at is None`）把它跳过不发
     （`skipped_unconfirmed`），内容在排期处静默堆积，再由 `SW_CONFIRM_TTL_HOURS`（默认
     24，见 `core/confirm.py`「超时不许无限堆积」）到点**自动驳回并通知**——发布链路停摆。
     注意红线 R1（发布确认只属于人）**并没有**因此失去载体：工作台的「确认发布」按钮不受
     Telegram 影响，仍可确认（同一后端 `core.confirm.confirm_item`，见
     `core/confirm.py:254-255`）。判失败的意义在于让操作者**知道**主载体死了，而不是让它
     静默降级。
   - **`polling` 那一格会骗人，所以从严那一档还多问一句（退出码 `26`）。** `polling` 就是
     `bool(poller and poller.alive)`（`core/telegram.py:981`），**只看线程活没活**；而
     `_loop` 捕到 `TelegramError` 之后只是 `stats.errors += 1`、记 `last_error`、按 2s→120s
     退避再 `continue`——**线程不会退出，它会无限重试**。于是 bot token 失效 / 被撤销 /
     网络长期不通时轮询线程**假活**：`polling` 照报 `true`，实际一条 callback 都收不到，
     人点了确认卡按钮没有任何线程去收，而 `enabled+ready+polling` 那道互锁照样给绿灯。
     新判据只有一条参与裁定：**`polling=true` 且 `stats.polls==0`（取得到）且
     `stats.errors>0` → 判红，退 `26`**。选 `stats.polls` 是因为它只在 `get_updates`
     **成功返回之后**才自增（`core/telegram.py:838`）且从不清零，所以 `polls==0` 严格等价于
     "本进程启动以来一次 `getUpdates` 都没成功过"，而且它**自愈**——成功一次就永远 `>0`，
     一次已恢复的抖动留不下假红。加上 `errors>0` 是为了排掉"进程刚起、首次 long polling
     还没返回"那个窗口。判红时输出自带**零成本排除法**：刚 `up -d --force-recreate` 过的话
     等一分钟重跑（本脚本纯只读、可重复），真活的轮询会把 `polls` 顶上去；仍是 `0` 就不是
     启动窗口。
   - **`last_error` 与 `stats.errors` 只报告、不参与裁定**，这是刻意的：两者都**只增不减**
     （`poll_once()` 成功路径一行都没碰 `last_error`，`stats.errors` 还混进了
     `handle_callback` 的业务异常），非空只证明"本进程启动以来出过错"，**不证明现在是坏的**。
     拿它们判红，一次凌晨三点的网络抖动会让此后每一次核验永久变红，那样的闸门很快就没人看了。
   - **够不着的那一半如实写在输出里，不假装能测**：单次快照区分不了"先健康跑了几天、然后
     token 被撤销"（那时 `polls` 冻在一个大数上）与"健康 + 历史抖动过几次"。契约里没有
     "上次成功轮询的时刻"，而红线 R4 禁止改 core；采两次样求差也不行——空闲时 `polls` 每
     `poll_timeout` 秒才跳一次，那个值可配且不在契约里，等于让脚本对一个未知量猜等待时长，
     猜短了就是新造出来的假红。
   - `last_error` 打印前按 token 形状打码。**这是纵深防御，不是在修一个活着的泄漏**：
     `core/telegram.py:263` 确实把整个异常对象插进了文案（而 `:262` 的注释声称"只报方法名"，
     注释与代码不符），但实测能到达那个 `except` 的 httpx 异常（`ReadTimeout` /
     `ConnectError`）字符串里**不带 URL**，唯一会带的 `HTTPStatusError` 需要
     `raise_for_status()`，而 `core/telegram.py` 里一次都没调过。哪天有人补上它，这道打码
     就是唯一挡着的东西。
5. **Telegram 轮询冲突**：统计 `docker compose logs --tail 2000 core` 里
   `grep -c -F 'error_code=409'` 的命中行数，期望 0。
6. **可选门禁**：给了 `--preflight` 才跑容器内 preflight。

#### 发布线（事实）与部署标记（意图）

**这两段是两种不同的东西，别混着读。** 本文档上一版描述的"HEAD 与 `origin/<当前分支>` 的
领先/落后提交数"已经不存在了——那个参照系是错的，理由见 `docs/RISKS.md` 第 11 条：生产的
本地分支名与它实际所在的发布线没有必然关系（这台机器上本地分支叫 `main`，而部署走的是
`origin/p14-organic` 那条线），拿"同名的 origin ref"当基准，算出来的领先/落后数字字面为真、
指向却是错的。

- **发布线 = 事实。** `git branch -r --contains <HEAD>` 直接问 git："这个提交落在哪些 origin
  线的历史里？"逐条打印命中的 ref 与它的顶端：HEAD 正好等于顶端就明说，落后就打印落后几个
  提交（**落后不算失败**）。`origin/HEAD -> origin/main` 这类符号 ref 会被跳过——它只是别名，
  不是一条独立的发布线。一条都没命中时**不下结论**：本脚本不 fetch，本地 remote-tracking ref
  可能陈旧，也可能这个提交确实还没推上去。
- **部署标记 = 意图。** `~/sw-deploy-state/last-deploy`，由 `update.sh --apply` 在部署成功后
  原子写入，记 `schema` / `ref` / `sha` / `at` 四个键。它回答的是"上一次经本工具面部署的是
  哪条线"。放在工作树**外面**，因为放进去会让上面那道「工作树干净」判失败、让 `update.sh`
  拒绝部署（写进 `.gitignore` 也不行——`.gitignore` 是被版本控制的，一次快进随时可能换掉它）。

**标记缺失是正常情形，不是失败。** 手工部署过、或者当前这一版早于标记功能上线，都会没有
标记；那时如实打印「没有记录」。标记内容形状对不上（`schema` 不是 `1`、`sha` 不是 40 位小写
十六进制、`at` 不是 ISO-8601 UTC……）时按「没有记录」处理并明说读不懂——**绝不猜**，一条猜
出来的"上次部署的是 X"比没有记录更坏。

**两者不一致意味着什么**：标记之后有人动过生产的 HEAD（手工 `merge`/`pull`、或走了本工具面
之外的部署路径），或者标记本身就旧了。脚本会明说不一致，并明说**以 `--contains` 那份事实为
准**——但不替读的人下"所以出事了"这种结论。发布线清单本身没读出来时，这一格会明说"**对不了**"
而不是当成"不在那条线上"：那是两件事。

**这两段都不产生任何裁定**（既不 `gate_pass` 也不 `gate_fail`）。它们是事实陈述，不是门禁：
不 fetch 带来的陈旧性使得任何一种结果都可能有良性解释，做成门禁只会制造另一类误判。要判
"部署的是不是这一版"，用 `--sha`——那才是有确定答案的问题。

**409 的匹配锚点必须是固定串 `error_code=409`，绝不能匹配裸 `409`。** 生产日志绝大多数是
uvicorn 的访问行，行里那串数字是客户端**临时端口号**，随机撞上 `409` 三连字符是常态，例如
`127.0.0.1:44092 - "GET /health HTTP/1.1" 200 OK`——裸匹配必然假阳性，会让核验永远失败。
真正的冲突只来自 `core/telegram.py` 里 Telegram API 失败信封的
`error_code=<code>` 文案，经轮询失败的 `logger.warning` 落盘。历史事故是两套部署抢同一个 bot
token 轮询，会吞掉用户的"确认发布"回调，所以非 0 就是失败项。脚本**只打印命中行数、绝不回显
原始日志行**：Telegram 报错文本可能带上游 URL，回显等于泄露 token。`tests/ops/test_verify.sh`
里有一条专门的回归用例把 `44092` 这个假阳性钉死。

失败项（任一不通过即非零退出）共十项：HEAD 可读、工作树干净、HEAD 等于期望 SHA（仅给了
`--sha` 时）、Compose 服务可读（`docker compose ps` 读取失败也计入）、端口门禁、健康探针
`GET /health` 200、运行环境 `env=prod`、**人工确认闸门通道 enabled+ready+polling**、Telegram
`error_code=409` 计数为 0、容器内 preflight（仅给了 `--preflight` 时）。端口门禁不合规时没有
可信的 loopback 探测目标，健康探针、`env=prod` 与人工确认闸门通道**三项**记为"未执行（端口
门禁未通过）"，同样计入失败项。

**明确不构成失败项**：HEAD 落后它所在的发布线若干提交；发布线一条都没命中；部署标记缺失、
读不懂、或与 HEAD 不一致；`use_fake_publishers` 与
`auth_required` 的取值本身——这两项只如实打印并各附一行裁定说明（当前生产
`use_fake_publishers=true`、`auth_required=false` 都是既定裁决）；以及
`use_fake_publishers=true` 时人工确认闸门通道的状态——同样只如实记录，不构成失败项（见上方
「人工确认闸门通道」一节的互锁规则）。

裁定规则：**先把所有检查跑完，最后统一裁定**，绝不第一个失败就中断——取证脚本的价值在于一次
拿全事实。结尾的"核验结论"逐项列出每个失败项的 ✓/✗；全部通过打印 `✓ 生产部署核验通过` 并退出
0，否则列全所有失败项后非零退出。

传输与状态语义同 `update.sh`：只有 ssh 本身以 255 报告连接/传输中断才等待 3 秒重试一次；远端
任何核验失败都立即失败、绝不重试。远端核验脚本自身若意外退出 255，包装层会先规范成 254，避免
与 IAP 断链混淆。

参数经 `printf '%q '` 转义后拼成**单一**远端命令字符串再交给 ssh，绝不拼进任何 shell 文本。
这一层是必需的：ssh(1) 不保留 argv 边界，host 之后的参数会被用单空格拼成一个字符串发给远端、
由远端登录 shell 重新分词，未指定 `--sha` 时的空串参数会就此消失，远端 `set -u` 下直接报错。

```text
$ bash scripts/ops/verify.sh --sha 0123456789abcdef0123456789abcdef01234567
生产部署核验

  连接 workbench-iap（IAP 首包通常需 5-10 秒）
  只读模式：不调用 backup.sh，不 fetch/pull/merge，不 build/up/restart，不写远端文件
  未开启容器内 preflight（需显式 --preflight）

Git 部署核验
  当前分支  p14-organic
  当前提交  0123456789abcdef0123456789abcdef01234567
  工作树    干净
  期望 SHA  0123456789abcdef0123456789abcdef01234567（一致）
  发布线    HEAD 被下列 origin ref 包含（本地 remote-tracking ref，未 fetch）：
            origin/p14-organic=0123456789abcdef0123456789abcdef01234567  HEAD 正好等于它的顶端
            origin/release=3333333333333333333333333333333333333333  HEAD 在这条线上，落后它 3 个提交（落后不算失败）
  部署标记  ref=p14-organic  sha=0123456789abcdef0123456789abcdef01234567  at=2026-08-22T16:10:04Z
            （上次经 update.sh --apply 部署的发布线；这是**意图**，上面的发布线是**事实**）
            对照  标记里的 sha 与当前 HEAD 一致
            对照  标记里的 origin/p14-organic 与 HEAD 实际所在的发布线一致

容器与端口门禁
NAME      IMAGE                  SERVICE   STATUS
sw-core   social_workflow-core   core      running
  端口门禁  core:8000 -> loopback（127.0.0.1:8000）

健康与运行时探针
  健康探针  GET /health 200
  版本  0.1.0
  环境  prod
  服务时间  2026-08-22T16:15:10.635248Z
  时区  Asia/Shanghai
  调度器  True
  生成开关  True
  模拟发布器  True
  鉴权  False
  已注册发布器  xhs, douyin
  裁定  模拟发布器=True：如实记录，本身不构成失败项（生产既定裁决）
  裁定  鉴权=False：如实记录，本身不构成失败项（生产既定裁决）

人工确认闸门通道（Telegram）
  总开关 enabled  True
  已配 token configured  True
  可推送 ready  True
  已知会话 chat_configured  True
  可签名 can_sign  True
  轮询线程 polling  True
  bot 用户名  xhs_sweetcornna_bot
  本进程已推送  0
  本进程推送失败  0
  指引 detail  <空：通道可用时为空>
  轮询成功次数 stats.polls  128
  收到更新条数 stats.updates  4
  已处理回调 stats.handled  4
  拒绝的回调 stats.rejected  0
  累计错误次数 stats.errors  0
  最近一次错误 last_error  <空：本进程启动以来没记到过错误>
  口径  last_error 与 stats.errors 都**只增不减**（略，脚本里逐条写着）：非空只证明"出过错"，
        不证明现在是坏的，所以它们只报告、不参与裁定
  假活判据  未命中：stats.polls=128 > 0，本进程确实成功轮询过
        注：单次快照看不出「先好后坏」（契约里没有上次成功轮询的时刻），这一半测不了，不假装能测
  裁定  模拟发布器=true：确认通道 ready=true polling=true，如实记录，本身不构成失败项（生产既定裁决）

Telegram 轮询冲突（error_code=409）
  近 2000 行中 error_code=409 的日志行数  0

可选门禁：容器内 preflight 未执行（需显式 --preflight；它会做外部连通性探测，可能超时）

核验结论
  ✓ HEAD 可读  0123456789abcdef0123456789abcdef01234567
  ✓ 工作树干净
  ✓ HEAD 等于期望 SHA  0123456789abcdef0123456789abcdef01234567
  ✓ Compose 服务可读
  ✓ 端口门禁  core:8000 -> 127.0.0.1:8000
  ✓ 健康探针 GET /health 200
  ✓ 运行环境 env=prod
  ✓ 人工确认闸门通道 enabled+ready+polling  模拟发布器=true：如实记录 ready=true polling=true，不构成失败项
  ✓ Telegram error_code=409 计数为 0

全部核验项通过。
  ✓ 生产部署核验通过
```

**与 `SW_UI_TOKEN` 鉴权加固的耦合（2026-08-22 已解除）**：`/api/v1/system/info` 与
`/api/v1/system/telegram` 都挂在 `AuthGuard` 下（`core/api/__init__.py:43-46`、
`core/api/common.py:210`），所以生产启用 `SW_UI_TOKEN` 后，`verify.sh` 这两处探针必须带
`Authorization: Bearer`。**这条路径已经打通**：配置方式、字符集限制、不配会怎样、配错会看到
什么，全部集中在本文档上面的「工作台 API token」一节，此处不重复。
未配 token 时，`verify.sh` 拿到 401 会打「根因 / 处置 / 自查 / 出处」四行，明写"这不是部署
故障，也不是 core 运行时故障"，而不是让人把它误判成生产挂了。背景见 `docs/RISKS.md` §8.4。

`backup.sh` 会在容器卷中生成
`/app/data/backups/sw-<UTC时间戳>.db`，只轮转删除符合这个严格命名格式且由本脚本
创建的旧快照，卷内保留最近 7 份。随后把两个文件以 `0700` 目录权限写至本机：

```text
~/sw-server-backups/20260819T221530Z/
├── social_workflow.db
└── accounts.yaml
```

可用 `SW_SERVER_BACKUP_DIR` 替换本机备份根目录。

## 生产端到端验收（`acceptance.sh`）

```bash
bash scripts/ops/acceptance.sh --dry-run          # 只说要做什么，不连远端
bash scripts/ops/acceptance.sh                    # 默认 --lane xhs
bash scripts/ops/acceptance.sh --lane wechat
```

它在生产机器上 `docker compose run --rm --no-deps core`，跑仓库里那份
`scripts/acceptance_full_chain.py`。两个临时账号：闸门关的那个要零干预走到 `measured`，
闸门开的那个要停在 `scheduled` 并记一笔 `skipped_unconfirmed`。两半都过才算通过。

### 为什么本机跑一遍不算数

本机跑证的是「这份代码接得上」。它证不了生产那台机器的**镜像里到底有没有 chromium**——
而这正是「全自动」在生产上跑不动的真实原因，且一度真的发生过：

> 镜像原来只 `uv sync --extra dsh`。没有 chromium → 封面 / 卡片渲不出来 → 机器审核记一条
> `cover.missing`（小红书那条 `xhs.image.missing` 甚至是 block）→ autopilot 的自动批准条件
> 是 `block == 0 且 warn == 0`，**一条 warn 就够让它不批** → 每条稿子都退回人工审核台。
> 换公众号纯文平台也躲不开，那条 warn 对公众号照样成立。

门禁在补上「渲染链」那两项之前，对着这台机器说的是「28 项零 FAIL」。

### 它碰不到生产的任何真实数据

跑的是那份脚本自带的沙盒：临时库、临时媒体目录、`FakePublisher`、Telegram 关掉、
`SW_ACCOUNTS_FILE` 指向临时文件。**远端在真正执行之前会逐条 `grep` 核对这五道保险还在**，
少一条就拒跑并说出少的是哪一条——不靠「我记得它是隔离的」，靠当场验。

它也不会替任何人点确认：建的两个账号是它自己的临时账号，生产台账里那些
`confirm_required=true` 的账号一个都不碰。R1 红线不在这条通路上。

### 退出码

| 码 | 含义 |
| --- | --- |
| `0` | 通过：闸门关的账号零干预走到 `measured`，闸门开的账号停在 `scheduled` |
| `1` | 验收未通过（脚本自己判红，输出里有「失败项」） |
| `3` | 生产镜像里没有渲染链——autopilot 在那台机器上批不了任何稿子 |
| `40` | 隔离前置检查没过：远端那份脚本少了保险，拒跑 |

`3` 和 `1` 分开是有用的：一个该去装东西（`--extra render` + `playwright install chromium`
进镜像，然后 `update.sh --apply`），一个该去查代码。混成一个码，调用方就分不清。

## sidecar 启用（`sidecar.sh`）

风险册第 9 条第 1 步「起 sidecar 容器」的合规通道。四个子命令，都只走一次 SSH：

```bash
# 只读：逐个 sidecar 报容器状态、解析后的端口绑定、配置文件是否就位
bash scripts/ops/sidecar.sh --status
# 在生产上从**已部署的模板**就地生成配置（已存在则不覆盖）
bash scripts/ops/sidecar.sh --materialize trendradar
# 起一个（先过端口回环闸门，起完再核实际绑定并探活）
bash scripts/ops/sidecar.sh --up trendradar
bash scripts/ops/sidecar.sh --up xhs-downloader
# 停一个（stop + rm，按显式服务名）
bash scripts/ops/sidecar.sh --down trendradar
```

### 白名单，以及 `mpt` 为什么是**有意排除**

名单写死在脚本里、不接受运行时扩展，当前是 `trendradar` 与 `xhs-downloader`
（复核：`sed -n 's/^SW_SIDECAR_WHITELIST="\(.*\)"$/\1/p' scripts/ops/sidecar.sh`；
`tests/ops/test_sidecar.sh` 有一条源码级断言钉着它，改名单必须同时改断言）。

`mpt` **不是"暂不支持"**：它的出片链路依赖模型网关，而且必须先有素材源 key
（`sidecars/mpt/config.example.toml` 写明 `pexels_api_keys` / `pixabay_api_keys` 必填其一，
只能由用户自己去申请）。`--up` / `--down` / `--materialize` 传它会被明确拒绝并说明理由；
`--status` 仍然如实把它列出来。

为什么不改成"从 `docker-compose.yml` 里枚举服务名"：那样谁往 compose 里加一个服务，本工具
就自动获得起它的能力——那就不是白名单了。而且「这个 sidecar 的容器端口是几、探哪个路径、
起之前要不要校验配置文件」这几格 compose 文件里没有，只能由脚本里那张显式表回答。

### 端口回环闸门（`--up` 的硬前置，本脚本最重要的一道）

`docs/RISKS.md` 第 15 条刚查出 `xhs-downloader` 与每账号小红书 sidecar 曾经绑 `0.0.0.0`。
生产是合租机器，**暴露面不止"同网段的机器"**：同机其它 docker 网络里的容器经默认网关
（`docker0`，通常 `172.17.0.1`）就够得着——§15.2 实测。而每账号小红书 sidecar 带着该账号的
登录态 cookies，`AUTH_TOKEN` 默认为空、留空即不鉴权。所以起之前必须先证明它只绑回环。

**校验依据是远端 `docker compose config` 解析之后的 `host_ip`，不是 grep 源文件**，四条理由：

1. 要判的是**生效值**。`127.0.0.1:${XHS_DOWNLOADER_HOST_PORT:-5556}:5556` 里的变量插值由
   compose 在解析时完成，grep 拿到的是插值之前的字符串。
2. `docker-compose.override.yml` **只有 compose 自己看得见**。它在 `.gitignore` 里（注释写着
   「环境本地的 compose 覆盖(如服务器上的环回端口绑定)」），仓库里根本没有那个文件。
   本机实测过它的合并语义：默认文件发现会把它合进来**并且能改掉 `host_ip`**，而一旦显式写
   `-f docker-compose.yml`，它就**不再被加载**。所以脚本里那条 `config` 与紧接着那条 `up`
   **都不带 `-f`**，走完全相同的文件发现路径——闸门校验的必须正是 `up` 会得到的那份配置。
3. 只有 compose 知道「没写 `host_ip`」等于什么：`5556:5556` 这种写法在解析结果里**不产生
   该键**（本机实测），含义是 `0.0.0.0`。判定因此必须是"取不到就按 `0.0.0.0` 处理"——
   这正是 `docs/RISKS.md` §15.6 那条 jq 复核命令里 `// "0.0.0.0"` 的来历。任何基于 grep 的
   写法都会把**最危险的那种形态**当成"没匹配到，跳过"。
4. 必须在**远端**：生产跑的是生产那份文件与那份 `.env`，本机的解析结果不算数（红线 R3 的另一面）。

只放行 `127.0.0.1` 与 `::1`，**这是白名单**——`localhost` 之类一律拒绝：它要过 `/etc/hosts`
才知道指向哪儿，而"绑在哪个地址"不该由一次名字解析来回答。

**覆盖面要说准**：`--status` 那张表与这道闸门都只覆盖**默认 compose 组合**。
**每账号小红书 sidecar 不在里面**——它们由 `scripts/gen_xhs_sidecars.py` 生成到
`docker-compose.xhs.yml`，那个文件在 `.gitignore` 里、不入库，也不在默认文件发现范围内；
本工具的白名单同样不管它们的起停。看它们的绑定要另外 `-f` 上那个文件，命令见
`docs/RISKS.md` §15.6。`--status` 的输出里会把这句话打出来，免得那张全是"回环"的表被读成
"全部发布口都安全"。

判不了（`docker compose config` 失败、服务不在解析结果里、core 容器起不来因而没法解析 JSON）
一律 **fail-closed**，与"判红"用不同的退出码：处置动作不同，不该逼人 grep 中文文案去区分。

**起完之后还有第二道**：`docker compose --profile <p> port <服务> <容器端口>` 读**运行期真身**，
规则与 `verify.sh` 的 core 端口门禁同源（恰好一条 `127.0.0.1:<port>` 或 `[::1]:<port>`）。
这一格判红时**当场 stop + rm 掉该容器**——端口已经开过，打印一行警告了事等于把它留在那儿开着。

### 起完要探，探不到就如实报

`docker compose up -d` 返回 0 只说明容器被创建并进入 running，**不**说明里面那个进程没有
立刻退出。所以 `--up` 之后必须真的打一次。两点与 core 那几条探针**刻意不同**：

- **不用 `sw_probe`**。`sw_probe` 带 `-f`（HTTP >= 400 判失败），而这里要回答的是"它在不在
  监听"，一个 404 恰恰证明**它在**。trendradar 的 8080 是 `python -m http.server` 挂
  `/app/output`（**没有 REST API**），xhs-downloader 的 5556 上有什么路由本仓没有约定。
  判据因此是"拿到了任何一个真实的 HTTP 状态码"，与 `scripts/preflight.py` 的 `_probe_http`
  同一口径。
- **绝不把 core 的 token 送给 sidecar**。`sw_probe` 会在 `SW_OPS_UI_TOKEN` 非空时塞
  `Authorization: Bearer`，而这两个 sidecar 都是上游镜像，它们会把收到的头写进自己的日志
  谁也说不准。所以 `--up` / `--down` / `--materialize` 这三个模式**一个凭据都不取、也不往
  远端送**（只有 `--status` 会取，因为它要读 `use_fake_publishers`）。

探不到时**不宣称已启动**：容器留着不动（绑定那一格已经核过，是好的），打印最近的日志，
并给出 `--down` 清理命令。

### trendradar 首次 `--up` 探不到，通常不是故障

**实测（2026-08-23，生产）**：`--up trendradar` **第一次**就以「容器起来了，但探不到它在监听」
收尾；等它把首轮抓取跑完再跑一次**同一条命令**，第 1 个探针就 `HTTP 200`，四步全过。中间
**什么都没改**——没改配置、没改脚本、没动容器。

原因在 compose 里明写着：`trendradar` 的环境是 `RUN_MODE=cron` + `IMMEDIATE_RUN=true`。
`IMMEDIATE_RUN` 的存在理由是好的（否则第一个 `CRON_SCHEDULE` 周期里 `output/` 是空的，core 只
能吃 404），但代价是**容器启动之后先把配置里那份平台清单整轮抓完，`python -m http.server` 在
那之后才开始响应**；而存活探针的窗口只有几十秒（确切值以脚本为准：
`grep -n 'probe_attempt' scripts/ops/sidecar.sh`），一轮外网抓取——尤其是里面有源在超时重试的
时候——比这个窗口长得多。所以探针如实报"探不到"、脚本如实拒绝宣称成功：**这两个行为本身都是
对的**，不要为了让它变绿而去动它。

**怎么与真故障区分：读脚本失败时自动打出来的那段容器日志，判据是"它在不在干活"。**

| 日志长什么样 | 结论 | 怎么办 |
| --- | --- | --- |
| 逐平台的进度行，形如 `获取 <平台> 成功` / `请求 <平台> 失败: <状态码> … 秒后重试` | **首轮抓取还没跑完**，不是故障 | 等首轮抓完再重跑 `bash scripts/ops/sidecar.sh --up trendradar`。这一轮要抓多少个平台由生产上那份 `sidecars/trendradar/config/config.yaml` 的 `platforms:` 段决定（`--materialize` 生成它时用的模板在库里，量级复核：`grep -c '^ *- id:' sidecars/trendradar/config.example.yaml`） |
| 上游 `entrypoint.sh` 报缺文件后 `exit 1`，或日志停在启动横幅之后再无新行 | **真故障** | 先 `--materialize trendradar` 把配置补齐再 `--up`；仍不行就 `--down trendradar` 清掉，别把 `restart: unless-stopped` 的崩溃重启循环留在生产上 |
| 整片 ANSI 光标定位转义序列（进程在画一个交互式 TUI） | **不是"起慢了"，是这个镜像的默认入口压根不是 HTTP 服务** | 这条在 `xhs-downloader` 上真实发生过，处置与理由见 `docs/RISKS.md` 第 9 条第 1 步：先把"它的接口到底是什么"定下来，再谈起不起 |

**只发生在冷启动那一轮**：容器跑起来之后 `output/` 里已经有当天的数据，`http.server` 一直在
听，后续 `--up`（或 compose 自己的 `restart`）不会再撞上这个窗口。

**为什么本节只是记录、没有把探针窗口调大**：改超时是**行为变更**，而且是对**所有** sidecar
一起放宽——"进程起来就立刻退出"这类真故障的暴露时间会跟着一起往后推。要改就单独一轮改，连
`tests/ops/test_sidecar.sh` 的用例一起，不要顺手塞进别的批次。

### 配置怎么就位：在生产上从已部署的模板生成，不推送任何本机文件

`sidecars/trendradar/config.example.yaml` 与 `frequency_words.example.txt` **都已提交进仓库**
（`sidecars/trendradar/config/.gitkeep` 也在，所以那个目录本身也会被创建），会随
`update.sh` 的快进部署过去；两份样例里没有任何凭据。所以 `--materialize` 做的是
**在生产上复制**，零传输、零新增攻击面——口径与 `scripts/ci_local.sh` 的 compose job 本地那
几行 `cp *.example.* → config/` 完全相同。

**已存在则不覆盖**：人可能在上面填过自部署 newsnow 地址之类的本地信息，而本工具没有任何
办法分辨"模板原样"与"人改过"，宁可如实报"已存在，跳过"。模板本身不在生产上时**报错而不是
从本机拷一份过去**——本工具没有、也不打算有"推任意文件到生产"的能力。

### 不碰 core，也不套 R1 闸门

- 一律不重启、不重建、不 build core；只按**显式服务名**操作，绝不裸 `up`、绝不裸
  `docker compose down`（那会连 core 与网络一起拆）。唯一用到 core 容器的地方是
  `docker compose exec -T core python3` 解析 JSON，那是只读的，与其余几个脚本同一手法。
- **刻意不套 R1 闸门**：起 sidecar 不改变发布语义，`use_fake_publishers` 一个字节都没动。
  给它套一道"确认通道必须活着"的互锁会变成又一处"为对称而对称"。`--status` 会如实带上当前
  `use_fake_publishers` 的值供人对照，仅此而已。

### 退出码

本机参数校验失败一律退 `1`；**远端协议码原样透出**，因为它们回答的是不同的问题。
唯一定义处是脚本顶部那组 `SIDECAR_*_STATUS` 常量（连同各自的一行说明），
复核：`grep -n '^SIDECAR_.*_STATUS=' scripts/ops/sidecar.sh`。
`ssh` 自己的 `255`（传输中断）也原样透出，与远端的 `254`（远端脚本自身退 255 的规范化结果）
分开。**任何模式都不自动重试**：`--up` / `--down` / `--materialize` 会改变生产状态，传输在
半路断掉时生产状态是不明的，在不明状态上叠一次操作比重跑一次贵得多。

### 顺序不能反

端口收敛（`docs/RISKS.md` 第 15 条）**必须先部署到生产**，`--up` 才有意义。**这一步已经发生**
（2026-08-23）：收敛随那一批部署上去之后，`--status` 的端口绑定表每一行都是 `127.0.0.1`，闸门
这才放行。在那之前 `--up xhs-downloader` 会被闸门判红，这正是它该做的事——要确认现在站在哪一边，
跑一次 `bash scripts/ops/sidecar.sh --status` 看那张表，别照抄这里的结论。

## 典型输出

```text
$ bash scripts/ops/status.sh
生产 core 状态

  连接 workbench-iap（IAP 首包通常需 5-10 秒）

Compose 服务
NAME      IMAGE                    SERVICE   STATUS
sw-core   social_workflow-core     core      running

Core 探针
  版本  0.1.0
  环境  prod
  调度器  True
  ✓ 状态读取完成

$ bash scripts/ops/backup.sh
生产 core 在线备份

  卷内快照  /app/data/backups/sw-20260819T221530Z.db  7340032 bytes
  ✓ 本机备份完成：/Users/you/sw-server-backups/20260819T221530Z
  数据库 7340032 bytes
  生产台账 128 bytes

$ bash scripts/ops/update.sh
生产 core 更新

  演练模式：只 fetch、核验目标和展示提交区间，不改工作树、不构建、不重启
当前提交  0123456789abcdef
待前进    2 个提交

将要前进的提交
89abcde 修复探针超时
fedcba9 更新依赖锁文件
  ✓ 更新演练完成
```

`--apply` 的任一步失败都会记录更新前 branch/HEAD，并在远端输出经 Bash `%q` 转义、可复制的
**人工**恢复命令。脚本不会自动执行 `switch`、`reset`、重建或回滚；恢复命令只操作服务器本地
工作树与 Compose，绝不会 push、删除或改写远端 ref。原状态是分支时先 `git switch -- <分支>`
再提示 `git reset --hard <旧HEAD>`；原状态是 detached HEAD 时只提示
`git switch --detach <旧HEAD>`，不含 `reset --hard`。分支恢复中的 `reset --hard` 会丢弃服务器
工作树中未提交的代码改动，因此只应在人工确认后执行。

Compose 把 core 固定发布为 `127.0.0.1:8000:8000`。启动后脚本还会读取
`docker compose port core 8000`：只放行恰好一条 `127.0.0.1:<port>` 或 `[::1]:<port>`，端口
必须是无前导零的十进制 `1..65535`；任意公网地址、空值、多行、CR 或畸形端口都会在 preflight
和 HTTP 探针之前被拒绝。
