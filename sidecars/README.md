# sidecars/ — 独立进程组件

这里只放 Dockerfile / 配置，**不放这些项目的源码**（走官方镜像或 git submodule）。

GPL / AGPL 组件必须以独立进程出现，core 只通过 HTTP 调用，绝不 `import` 或复制其代码。

| sidecar | 上游 | License | 用途 | compose profile | 阶段 |
|---|---|---|---|---|---|
| `xiaohongshu-mcp` | `xpzouying/xiaohongshu-mcp` | Apache-2.0 | 小红书发布 / 搜索 / 登录二维码（容器内 **18060**，镜像 `xpzouying/xiaohongshu-mcp:v2.5.0`，见 [`xhs/README.md`](xhs/README.md)） | **不走 profile**：由 `scripts/gen_xhs_sidecars.py` 按 `accounts.yaml` 一账号一 service 生成到 `docker-compose.xhs.yml`（该文件在 `.gitignore` 里，起之前先跑生成器） | P2 **已落地** |
| `mpt` | `harry0703/MoneyPrinterTurbo` | MIT | 视频合成 HTTP API | `video` | P3 **已落地** |
| `xhs-downloader` | `JoeanAmier/XHS-Downloader` | **GPL-3.0** | 小红书爆款采集。**compose 里有 service，但本仓侧没有调用方**——计划中的 `sourcing/xhs_search.py` 不存在，`sourcing/collector.py` 的 `SOURCES` 里也没有它；生产上**有意未起**（`docs/RISKS.md` 第 9 条） | `xhs` | P2 **未接通** |
| `trendradar` | `sansan0/TrendRadar` | **GPL-3.0** | 热榜聚合（**本仓模板启用了哪些平台以下面的复核命令为准**；上游可选平台的**总数未核实**，这里刻意不写任何一个总数——见 [`trendradar/README.md`](trendradar/README.md)「未核实」段） | `sourcing` | P4 **已落地** |
| `douyin-metrics` | `Kuhakucai/douyin-mcp` | **AGPL-3.0** | 抖音公开指标。**当前不接这个 sidecar**：指标由宿主机上传器从创作者中心数据页读，为了四个数字再拉一个 copyleft 容器不划算（`metrics/README.md`「抖音的口径」）。**计划保留**：将来要更细的数据（完播率 / 涨粉）仍按"独立进程 + HTTP"接它 | **尚未进 compose**——`docker-compose.yml` 里没有这个 service | P3 **当前不接** |

**这张表是摘录，不是真相**——它只给路线图视角，每一列都有能当场跑出答案的来源：

```bash
# compose profile 一列：service 名 + 紧跟它的 profiles 行就是答案。
# 输出里没有的（core 不带 profile；douyin-metrics 压根不是 service）就是表里写的那样
grep -nE '^  [a-z][a-z0-9-]*:$|profiles:' docker-compose.yml

# trendradar 本仓模板实际启用的平台：读者当场数，所以正文不写数字
grep -n '^ *- id:' sidecars/trendradar/config.example.yaml
```

**上游 / License / 阶段三列以 `docs/THIRD_PARTY.md` §4 为准**——那份是全仓 License 台账，
本表是它的摘录；两边对不上时改这里，不是改那里。

**「已落地」= 仓库侧集成已完成**（客户端 + 生成器 + 测试），**不等于生产上正在跑**——
`mpt` 就是典型：已落地，但生产上**有意排除**。各 sidecar 在生产上的实际起停状态以
`docs/RISKS.md` 第 9 / 第 15 条为准（与 `docs/THIRD_PARTY.md` §4 同一口径，那边还给了
逐行可跑的复核命令，这里不复述）。

**不进 Docker 的例外**：抖音发布器（Patchright 有头浏览器）必须跑在有图形界面的宿主机上，
见 `publishers/douyin/README.md`。

小红书 sidecar 是**单进程单账号**（cookies 存单一 `./data`），
因此必须一账号一容器 + 一独立 volume + 一独立端口，禁止共享 volume。
