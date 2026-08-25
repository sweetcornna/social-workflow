# metrics/ — 数据回流与复盘

```
collector.py   24h / 7d 窗口判定 + 快照写入（P1 落地）
insights.py    复盘 Agent：7d 指标 → Claude 结论 → 回灌选题（P4 落地）
```

采集入口是 `core/scheduler.py:tick_metrics`，它只是 `metrics.collector.collect_all` 的薄壳。

## 契约

指标由 `Publisher.fetch_metrics(platform_post_id) -> dict` 产出。真实发布器应只返回合法
JSON 值的普通 dict：每层 key 是 exact `str`，scalar 是 exact
`None`/`str`/`bool`/`int`/`float`，容器是 Mapping/list/tuple（tuple 会变成 JSON array/list）。
采集器只归一化一次：它以一次 `items()` 快照把 Mapping 递归复制成 builtin dict/list，再把
安全副本交给 JSON serializer、SQLAlchemy 和 post-id repair；共享引用允许，当前递归路径的环拒绝。
只有没有明确声明 `available=False` 的成功结果才会写入 `MetricSnapshot.metrics_json`；该表**只追加**，
永不更新或删除。

统一字段（缺失填 `None`，不要伪造 0）：

```python
{
    "views": int | None,
    "likes": int | None,
    "comments": int | None,
    "shares": int | None,
    "collects": int | None,
    "follows": int | None,
}
```

实现方可以在同一个 dict 里追加平台原生口径与诊断字段。公众号实现额外给出：
`read/like/share/comment/collect`（原生别名）、`available`（本次是否真拿到数据）、
`reason`（拿不到的原因）、`source`、`stat_date`、`wechat`（原始日粒度明细）。
`available=False` 时所有统一字段都是 `None`——**这不是 0，是"没数据"**。
它是一次失败的采集尝试，不落 `MetricSnapshot`、不把内容推进到 `measured`、也不覆盖
24h/7d 窗口。只有归一化后的 builtin dict 中 `metrics.get("available") is False` 才是不可用；字段
缺失（包括 `FakePublisher`）和其它非 `False` 值仍按成功兼容处理。数组、字符串、数字和
`None` 是 malformed payload，同样不落快照，但会与 `unavailable` 分开统计。非 exact 字符串 key、
重复 key、scalar 子类、set、自定义对象、循环引用、NaN 和 Infinity 都是 `malformed`；普通协议异常也
同样隔离。验证在结果事务的任何写入前完成，攻击对象不进入 `get`、serializer、SQLAlchemy、repair、
异常或日志。没有元素、字节或时间上限，5 MiB 合法 payload 必须完整保留；无限 iterator 或永不返回的
协议操作须由未来的进程隔离处理，当前进程内不承诺终止它们。

## 采样节奏与窗口判定

发布后 **24h** 与 **7d** 各一次快照。窗口状态**不额外建表、不写进 `metrics_json`**，
完全由 `MetricSnapshot.snapshot_at` 与发布时刻（`PublishRecord` phase=done 的
`updated_at`）推导：

- 窗口 `24h` 已覆盖 ⟺ 存在 `snapshot_at >= published_at + 24h` 的**可用**快照
- 窗口 `7d`  已覆盖 ⟺ 存在 `snapshot_at >= published_at + 7d` 的**可用**快照

一次 tick 只打**一张**快照：两个窗口同时到期（中间停机了几天）时，这一张同时满足两者。

```python
collect_all(respect_windows=True)  # 生产：只在窗口到期且未覆盖时采样
collect_all()  # 默认：每次调用都采一张，便于手动触发 / 联调
```

`create_scheduler()` 注册的 `tick_metrics` job 以 `respect_windows=True` 每 6 小时跑一次。
不可用、malformed 或发布器异常都不在一次调用内重试。采集器会把每条**已开始**处理的内容
持久记录到 `metric_collection_attempts`，下轮仍未覆盖的内容会重新进入候选，并按
`(last_attempt_at or due_at, due_at, item_id)` 选择最久未尝试者。相同 UTC 六小时桶内一条内容
最多开始一次；每个候选在独立短事务中原子 claim 并提交后才构造 publisher。并发 tick 的 claim
冲突不计入 `attempted`，败者会继续遍历并处理其它候选，桶耗尽才为空批。这样持续失败项会在
每次尝试后让出队首，动态新增/删除内容也不会改变旧积压的服务顺序。
claim 使用 SQLite 单语句 UPSERT，冲突分支仅在已存桶号严格小于目标桶号时更新；因此同桶、
旧桶和乱序触发都不能覆盖更新的 claim，公开路径保证每条内容每桶至多一次 publisher 调用。

claim 后的 publisher 主 fetch、可选标题 fallback 和 health 均不持有数据库事务；每项 outcome、
快照、post id、内容状态和 health 再用独立短事务提交。DB/状态机错误回滚该项并向上传播，不能
伪报成 publisher 异常，也不会回滚此前已提交项。结果事务第一条写与最终 outcome 写都要求
`item_id + bucket + last_outcome=claimed`，所以旧桶迟到和同桶重复结果不会写任何业务表。
SQLite 仍是单写者数据库；锁等待遵从数据库 URL 配置的连接 `timeout`，超时会以稳定脱敏的
数据库错误停止当前 tick，不会改变此前已经提交的单项。

claim 是 **at-most-once per bucket** 租约。进程在 claim 后崩溃时，该内容本桶不会再调用
publisher，attempt 会保留 `claimed`；下一桶在窗口仍欠账时重新进入公平队列，但受 backlog 和
`max_items` 影响，不承诺紧邻下一 tick 一定处理。候选和历史快照查询使用联接/子查询，不展开
全量 ID；历史快照流式读取，每条内容只在内存中保留最新可用时刻。

### 生产：未来 bucket（时钟异常）

`metric_collection_attempts.last_attempt_bucket` 是
`floor(UTC Unix timestamp / 21600)`。因此，如果错误的未来时钟曾运行过一次 metrics tick，
该行的 bucket 会大于恢复后时钟计算出的当前 bucket；单调 claim 有意拒绝把它倒退，内容会一直
等到时钟追上才会再次尝试。这是运维异常，不是应用应自动改写的状态。

典型信号是每轮 metrics 的 `attempted=0`，或少数应到期内容长期没有新尝试。先核对**宿主机和
core 容器**的 UTC 时间并修复 NTP/时钟漂移；时钟仍不正确时，绝不能改数据库。正常的“同一 UTC
六小时桶内最多开始一次”不需要处置，只有 `last_attempt_bucket > current_bucket` 才是此故障的
证据。

默认只读诊断、停服后的带事务受保护修复、复检、回滚和恢复服务的完整生产 runbook 在
[docs/OPS.md §5.1](../docs/OPS.md#51-指标采集-future-bucket-时钟异常)。仅生产值班人员可执行；
它只会在明确确认后删除受影响的 attempt 辅助行，绝不修改真实 payload 或 `metric_snapshots`。

返回统计中 `attempted` 是已开始内容级采集的数量，`unavailable`、`malformed`、`errors` 分别
是三种结果，`history_unavailable`、`history_malformed` 是本轮发现并忽略的历史坏快照；它们都
不等同于 `skipped`。`max_items` 是每次调用最多成功 claim 并开始处理的**内容数**，不是网络调用
上限：一条内容可能触发主 fetch、标题 fallback 和 health。`None` 使用默认 50，0 是空批，负数
会被拒绝，大于候选数时只处理实际可 claim 的候选。真实平台的并发、锁竞争和平台限流仍未验证。

## 各平台来源

| 平台 | 来源 | License / 集成 | 状态 |
|---|---|---|---|
| wechat_mp | `datacube/getarticletotal`（需认证号） | 官方 API | P1 已实现 |
| xhs | xiaohongshu-mcp `GET /api/v1/user/me`（退回 `POST /api/v1/feeds/detail`） | Apache-2.0 sidecar | P2 已实现 |
| douyin | 宿主机上传器读**创作者中心数据页**（`GET /accounts/{id}/metrics/{post_id}`） | 自研，Patchright(Apache-2.0) 独立进程 | P3 已实现 |

### 小红书的口径与 post_id 映射问题

指标取自自己主页 feed 里的 `interactInfo`（比开详情页便宜得多，详情页要真开一个浏览器标签）：

| 统一字段 | 小红书来源 | 说明 |
|---|---|---|
| `views` | — | **恒为 `None`**：小红书不对外暴露阅读量。不是 0 |
| `likes` / `collects` / `comments` / `shares` | `likedCount` / `collectedCount` / `commentCount` / `sharedCount` | 平台给的是字符串（`"1.2万"`），由 `client.parse_count` 折算 |
| `follows` | — | 账号级指标，在 `user/me` 的 `interactions` 里，不属于单条笔记 |

计数解析不出来（例如页面给的是 `"赞"` 这种标签文字）时返回 `None`，**不伪造 0**。
额外字段：`xhs.{note_type, *_raw}` 保留平台原始字符串，便于事后核对折算是否正确。

**post_id 映射**：上游 `POST /api/v1/publish` 的响应里**没有笔记 id**，
发布器发完会扫主页对账；对账不到就记占位 id（`xhs-unresolved-*` / `xhs-scheduled-*`，
定时发布必然先记后者）。采集器对此的处理：

1. `fetch_metrics(占位 id)` → `available=False` + 原因；
2. 采集器调可选能力 `fetch_metrics_for_title(item.title)` 按标题兜底；
3. 命中后 `_repair_post_id` 把真实 id（和链接）**回填**进 `PublishRecord`，下次走正常路径。

回填时会显式保住 `PublishRecord.updated_at`（它被 `published_at_of` 当作"发布时刻"用来算
24h/7d 窗口，而该列带 `onupdate`）——否则一次指标回填会把整个采样窗口往后推。

### 抖音的口径

指标由**宿主机上传器**从创作者中心的数据页读，是**尽力而为**的：那页的 DOM 未在真实
站点验证，读不到就返回 `available=false` + `reason`，绝不伪造 0。

| 统一字段 | 抖音来源 | 说明 |
|---|---|---|
| `views` / `likes` / `comments` / `shares` | 数据页该作品行的四个数字 | 列顺序按 2026-08 页面观察，**未验证** |
| `collects` / `follows` | — | 数据页没有单列，恒为 `None` |

**不走 `douyin-mcp`（AGPL）/ `TikTokDownloader`（GPL）**：既然上传器已经有一个登录着的
浏览器，再拉一个 copyleft sidecar 进来只为读四个数字不划算。将来要更细的数据
（完播率、涨粉）再按"独立进程 + HTTP"接它们。

**post_id 映射**：与小红书同构——发布时解析不出 `aweme_id` 就记
`douyin-unresolved-*`，由 `fetch_metrics_for_title` 按标题兜底回填（见 `_repair_post_id`）。
占位 id 的识别用跨平台的 `publishers.base.is_placeholder_post_id`。

### 公众号的 post_id 映射问题

我们回写的 `platform_post_id` 是草稿 `media_id`，而 datacube 用的是 `msgid`，
两者没有官方映射。解析链：

1. `platform_post_id` 形如 `12003_3` → 直接按 `msgid` 匹配；
2. 否则 `draft/get(media_id)` 取标题 → 按标题匹配 datacube 行；
3. 仍失败 → 返回 `available=False` + 原因，采集器再调**可选能力**
   `publisher.fetch_metrics_for_title(item.title)` 兜底一次。

`fetch_metrics_for_title` 不属于 P0 冻结契约，采集器用 `hasattr` 探测，
没有这个方法的发布器不受影响。

## 复盘（P4）
`review_agent.py`：读近 30 天快照 → 给选题打权重 → 回写 `Topic.score` 与账号 persona。

## 复盘 Agent（`insights.py`，P4）

P3 之前 `metrics/` 是**只写不读**的：指标存下来没人用，选题 Agent 每天从零开始。
P4 补上回路——这是"数据回流 → 复盘 → 选题权重"闭环的最后一环：

```
MetricSnapshot(7d) ──► collect_summary ──► Claude(InsightsReport) ──► insights.md
                                                                          │
                                       sourcing/selector.py ◄─────────────┘
```

```bash
curl -sX POST 'localhost:8000/dev/tick/insights?force=true'
cat prompts/accounts/xhs-demo-01/insights.md
```

### 几个刻意的选择

| 决定 | 为什么 |
|---|---|
| 结论**写文件**（`prompts/accounts/<id>/insights.md`）而不是写库 | 它和 `persona.md` 一样是人要看、要能手改、要出 git diff 的资产，不该埋在 SQLite 的 JSON 列里 |
| **当前 LLM 后端凭据缺失时整体跳过**（`llm_credentials_ready()`），不回落 `ScriptedLLM` | 生成的稿子人会审；假的复盘结论却会**持续**污染后续每一次选题决策 |
| 7d 内已发不足 `INSIGHTS_MIN_POSTS`（默认 3）就不跑 | 三条数据推不出规律，只会生成一段像模像样的噪声 |
| 汇总里 **`None` 不是 0** | 小红书永远拿不到 `views`。填 0 会让模型得出"阅读量极低"的错误结论 |
| 每账号按 `INSIGHTS_INTERVAL_HOURS`（默认 24h）各自节流 | job 每 6 小时跑一次，多跑几次不能重复烧 token |
| 模型调用失败也**推进时间戳** | 否则每轮 tick 都会对同一个坏账号重试，一天能烧掉整个预算 |

结构化输出 `InsightsReport` 的字段：`headline` / `what_worked` / `what_failed` /
`topic_guidance` / `title_patterns` / `best_slots` / `next_actions` / `confidence` / `note`。
prompt 在 `prompts/metrics/insights.md`，明确要求"只说数据支持的话、给可迁移的模式而不是复述数字、
样本 < 5 条一律 `confidence=low`"。

条目在文件里用 `<!-- insight -->` 分隔，只保留最近 `INSIGHTS_KEEP`（默认 6）条；
选题时只读最近 2 条（`sourcing.selector.DEFAULT_INSIGHTS_ENTRIES`）——更早的多半已被推翻。
