# sourcing/ — 选题采集

**P1 已落地：newsnow + douyin-hot-hub + 去重 + 选题决策 Agent。**
小红书（P2）、TrendRadar（P4）待实施。

## 契约

采集器只产出 `sourcing.base.RawTopic`（值对象），入库统一走 `persist_topics`：

```python
RawTopic(source, title, url, score, raw)  →  core.models.Topic
```

- `source` 取值即模块名（`newsnow` / `douyin_hot_hub` / `xhs_search` / `trendradar`）。
- `score` 归一化到 `[0, 1]`，跨源可比。热榜大多只给名次不给可比热度值，
  所以统一用 `rank_score(rank, total)`（第 1 名 1.0，末名 `1/total`），
  原始热度值放进 `raw`。
- `raw` 保留源站原始字段，不裁剪，便于事后复盘。

## 模块

| 模块 | 来源 | License / 集成 | 状态 |
|---|---|---|---|
| `base.py` | — | `RawTopic` + `persist_topics` + `rank_score` | ✅ P1 |
| `dedupe.py` | — | 标题归一化 + simhash + 编辑距离 + 包含度 | ✅ P1 |
| `newsnow.py` | `ourongxing/newsnow` | MIT，**只调 HTTP API** | ✅ P1 |
| `douyin_hot_hub.py` | `lonnyzhang423/douyin-hot-hub` | MIT，**只读仓库归档 JSON** | ✅ P1 |
| `selector.py` | 自研 | 选题决策 Agent（Claude 结构化输出） | ✅ P1 |
| `xhs_search.py` | `JoeanAmier/XHS-Downloader` | **GPL-3.0 → 只作 sidecar，HTTP 调用，不 import** | P2 |
| `trendradar_client.py` | `sansan0/TrendRadar` | **GPL-3.0 → sidecar** | P4 |

## newsnow

`NEWSNOW_BASE_URL` **默认为空**，必须显式配自部署实例或公开实例——
不默认去打别人的服务器。榜单 id 由 `NEWSNOW_SOURCES` 配置（默认
`weibo,zhihu,baidu,toutiao`，四个都已实测有效）。

实测核实的接口形态（2026-08-15）：

```
GET  {base}/api/s?id=<source>     单榜
POST {base}/api/s/entire          批量，body {"sources": [...]}
```

响应 `{status, id, updatedTime, items[], info}`，item 为
`{id, title, url, mobileUrl?, pubDate?, extra?{hover, info, icon}}`。

几个坑：

- `items` 服务端**固定截断 30 条**。
- 非法 source id 返回 **HTTP 500**（不是 4xx），已翻译成可读错误。
- `extra.diff` 是前端算的排名变化，**API 不返回**。
- 批量接口**只读缓存**，冷启动的源会被整个略掉 → 默认走逐个 GET，
  静默少数据比慢几百毫秒糟糕得多。
- 单个榜单失败不影响其它榜单；全失败才抛。

## douyin-hot-hub

⚠️ **仓库结构与早期描述不同，以下为实测：**

- `archives/YYYY-MM-DD.md` 是 **Markdown**（扁平，无年月子目录）。
- JSON 在 **`raw/YYYY-MM-DD/<board>.json`**，是抖音上游 API 的**原样转储**。

| 文件 | 榜单 | 状态 |
|---|---|---|
| `hot-search.json` | 抖音热榜 | 有数据（`data.word_list`，~49 条） |
| `hot-music.json` | 音乐榜 | 有数据（`music_list`） |
| `hot-star.json` | 明星榜 | 当前为空 |
| `hot-live.json` | 直播榜 | 当前 0 字节，已停更 |

条目**没有 `url` 字段**，按上游做法拼 `https://www.douyin.com/search/<urlencode(word)>`。
当天归档可能还没生成（上游是定时任务），默认按天回溯 3 天。

## 去重

热榜同一件事在不同平台标题常只差几个字，纯 hash 挡不住，纯编辑距离又是 O(n²)。
四级判定（`is_duplicate`）：

1. **归一化精确相等**——挡标点/全半角/emoji/名次前缀/"热沸爆"角标差异。
2. **子串包含**——"国足输球" / "国足输球了"。
3. **编辑距离相似度**——短标题（<8 字）收紧到 0.9，防"大涨/大跌"被判成同一条。
4. **字符集包含度**——补编辑距离对**语序重排**无能为力的情况
   （"XX 回应 YY" vs "XX 就 YY 作出回应"）。

simhash 只当**粗筛**（阈值放宽到 26）：短中文标题上它的位分布不稳，
当决策依据会大量漏检。

`Deduper` 的候选集用**字符倒排索引**生成（共享 ≥2 字符才比对），不是 simhash 分桶——
分桶会漏检，倒排索引召回高、代价低。

所有哈希走 `hashlib`，**不用内置 `hash()`**：后者对 str 加了进程级随机盐，
跨进程结果不一致，重启后"上次跑过的去重"会失效。

## 选题决策 Agent

`selector.select_topics(candidates, llm, persona=..., recent=...)`，四维打分
（`fit` / `freshness` / `depth` / `risk`，risk 分数越高越安全）+ `overall` + `angle`。

- 候选用短 id（`c1`、`c2`）参与对话，省 token 也避免模型抄错长 id。
- `recent` 取该账号最近 14 天的选题，**不只看 published**——正在审核和已排期的
  也算占用了这个选题，否则同一天会生成两篇同题稿。
- 模型返回**空 `recommended` 是合法结果**（"今天没有值得写的"），
  上层必须能接受"今天不出稿"，不能强行取 top-1。

## 红线

采集与发布账号物理隔离；不复制 MediaCrawler（禁商用）任何代码；
不破解私有签名（a_bogus / x-s），只读公开归档与公开 API。
