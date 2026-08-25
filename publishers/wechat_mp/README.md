# publishers/wechat_mp —— 微信公众号发布器（P1）

实现 `publishers.base.Publisher`，`platform = "wechat_mp"`，通过 `tests/contract/`。

```
client.py          官方 API 的 httpx 薄封装（token / 素材 / 草稿 / 发布 / datacube）
publisher.py       WechatMpPublisher：prepare / publish / health / reconcile / fetch_metrics
wenyan_backend.py  备选后端：@wenyan-md/cli 子进程直发草稿箱
stub.py            不联网的假客户端（契约测试与本地联调）
```

## 两种账号形态

| | 形态 A：**认证**企业号 | 形态 B：个人号 / 未认证号 |
|---|---|---|
| 权限 | 有 `freepublish` | 2025-07 起被回收，只剩 `draft` |
| 本发布器行为 | 双确认闸门全开时 `draft/add` → `freepublish/submit` → 轮询到 `publish_status=0` | `draft/add` 后**停在草稿箱** |
| `PublishResult` | `ok=True, platform_post_id=<draft media_id>, url=<article_url>, raw.stage="published"` | `ok=True, platform_post_id=<draft media_id>, url=None, raw.stage="draft"` |
| 数据回流 | `datacube` 可用 | 无 datacube 权限，`fetch_metrics` 返回 `available=False` + 原因 |
| 人工动作 | 审核 UI 里点批准 | 审核 UI 点批准 + 到公众号后台点「发表」 |

## 双确认闸门

**三者缺一，就只到草稿箱**，且 `raw.gate.blocked_by` 会写清是哪一道拦的：

| 闸门 | 来源 | 谁改 |
|---|---|---|
| `server_switch` | 环境变量 `WECHAT_AUTO_PUBLISH` | 运维（改 `.env` 重启才生效） |
| `account_certified` | 环境变量 `WECHAT_CERTIFIED`（或构造参数 `certified=`） | 运维 |
| `confirm_publish` | `bundle.platform_extra["confirm_publish"] is True` | **审核人**：批准时由审核 UI 写入 |

第三道是**逐条**的：它跟着这一条内容走，落在 `ContentItem.bundle_json` 里，
配合 `ReviewLog` 构成"人确认过这一条"的证据链（`docs/POLICY.md` 要求）。
严格判 `is True`——字符串 `"true"`、数字 `1`、`"on"` 都不算，避免表单值被当成放行。

审核 UI 接入方式：

```python
from publishers.wechat_mp import mark_confirm_publish

item.bundle_json = mark_confirm_publish(item.bundle_json, actor=current_user)
# 再照常写 ReviewLog / approve()
```

## 方法与官方接口对照

| 方法 | 官方接口 | 说明 |
|---|---|---|
| `prepare` | `material/add_material?type=image`、`media/uploadimg` | 校验 title≤32 / author≤16 / digest≤120；上传封面拿 `thumb_media_id`；把正文里非 `mmbiz.qpic.cn` 的 `<img src>` 换成公众号图床 URL。**幂等** |
| `publish` | `draft/add` →（闸门全开）`freepublish/submit` + `freepublish/get` | 轮询到 0 成功；2/3/4/5/6 抛 `PermanentError`；超时抛 `RetryableError` |
| `health` | `cgi-bin/stable_token` | 见下表 |
| `reconcile` | `freepublish/batchget`、`draft/batchget` | 标题 + 摘要匹配，不往用户可见字段塞隐藏标记 |
| `fetch_metrics` | `datacube/getarticletotal`（+ `draft/get` 解析标题） | 未认证号返回 `available=False`，**不伪造 0** |
| — | `datacube/getusersummary` | 账号级用户增减，客户端已封装，采集器暂未接（见"已知限制"） |

`health()` 的映射，以及**为什么公众号永远不会返回 `needs_relogin`**：公众号没有登录
会话，只有 AppSecret 与 IP 白名单；误判成 `needs_relogin` 会白白挂起该账号的全部排期项。

| 情况 | 返回 |
|---|---|
| `stable_token` 正常 | `ok` |
| 40164（IP 不在白名单） | `degraded` + 出口 IP 与排障提示 |
| 40013 / 40125（AppID / AppSecret 无效） | `degraded`（配置问题，不是封号；`banned` 是人工终态，误判代价太大） |
| 限频 / 网络 | `degraded` |

## 配置

```bash
WECHAT_APP_ID=wx...............
WECHAT_APP_SECRET=...                 # 只从环境变量读，绝不入库、日志脱敏
WECHAT_CERTIFIED=false                # 闸门 2
WECHAT_AUTO_PUBLISH=false             # 闸门 1（最安全的默认值）
WECHAT_BACKEND=api                    # api | wenyan
WECHAT_API_BASE=https://api.weixin.qq.com
WECHAT_MEDIA_BASE_DIR=                # 正文相对图片路径的解析根目录，留空 = 进程 CWD
WECHAT_PUBLISH_POLL_INTERVAL=3.0
WECHAT_PUBLISH_POLL_TIMEOUT=120.0
SW_USE_FAKE_PUBLISHERS=false          # P1 起改 false，否则会被 FakePublisher 顶掉
```

`SW_USE_FAKE_PUBLISHERS=true` 时 `core/main.py` 会把三个平台全部覆盖成 `FakePublisher`，
真实公众号发布器不会生效——本地联调完记得改回 `false`。

## wenyan 备选后端

`WECHAT_BACKEND=wenyan` 时改用 `@wenyan-md/cli`（Apache-2.0）子进程：

```bash
npx -y @wenyan-md/cli publish -f article.md --app-id <APPID>
# Server 模式（绕开固定出口 IP 白名单）
npx -y @wenyan-md/cli publish -f article.md --server https://... --api-key-file ~/.config/wenyan/key
```

- 凭据经 `WECHAT_APP_ID` / `WECHAT_APP_SECRET` **环境变量**传给子进程，
  不放 argv（argv 对同机任何用户可见）。
- 它自带渲染 + 传图 + 传封面，所以 `prepare` 跳过图床上传，只做长度校验。
- **只到草稿箱**，永不 freepublish。
- 需要本机有 Node（`NODE_BIN`，默认 `npx`）。

## 故障排查

| 现象 | 原因 / 处置 |
|---|---|
| `PermanentError: IP not in whitelist …` | 出口 IP 不在白名单。异常 detail 里直接给了当前出口 IP，照着加白；见 `docs/OPS.md` 第 3 节 |
| `AppSecret 无效（errcode=40125）` | `.env` 里的 secret 过期或被重置，去公众号后台重置后更新 |
| `errcode=48001` | 未认证号调了 `freepublish`。把 `WECHAT_AUTO_PUBLISH` 或 `WECHAT_CERTIFIED` 改回 false |
| 内容一直停在草稿 | 看 `PublishResult.raw.gate.blocked_by`，它会指出是三道闸门里的哪一道 |
| `freepublish 轮询超时` → 重试 | 正常。重试前 `reconcile` 会先扫已发布列表/草稿箱，命中就不会重复发 |
| 正文图片在公众号里显示不出来 | 图片没换成 `mmbiz.qpic.cn`。检查 `prepare` 是否被跳过，或图片超过 `media/uploadimg` 的 1M 上限 |
| `fetch_metrics` 一直 `available=false` | 未认证号无 datacube 权限；或 `platform_post_id` 映射不到 datacube 的 `msgid`（见下） |
| 封面报"图片文件不存在" | `WECHAT_MEDIA_BASE_DIR` 没设，相对路径按进程 CWD 解析 |

## 已知限制

1. **`platform_post_id` ↔ datacube `msgid` 没有官方映射**。我们回写的是草稿
   `media_id`，`fetch_metrics` 走「msgid 直配 → `draft/get` 取标题后按标题配」
   两级解析；都失败时返回 `available=False` 并写明原因，
   `metrics/collector.py` 再用 `fetch_metrics_for_title(标题)` 兜底一次。
2. `datacube/getarticletotal` / `getarticlesummary` 的**最大时间跨度官方页未明示**，
   本实现按 1 天处理（代码里标了"未核实"）；`getusersummary` 的 7 天已核实。
3. freepublish 成功后原草稿是否仍能 `draft/get` —— **未核实**，故有上面的标题兜底。
4. `datacube` 数据每天 8 点后才出前一天的，`end_date` 上限是"昨天"（东八区）。
5. 公众号 datacube **没有点赞（在看）与评论数**接口，这两个统一字段恒为 `None`。
6. 多图文（`articles` 数组多条）与 `newspic` 图片消息暂不支持，只发单篇 `news`。

## 本地验证

```bash
uv run pytest tests/publishers/test_wechat_mp.py tests/contract -q
uv run python scripts/preflight.py            # 有凭据时会实际探测 stable_token 与 40164
```

样例响应在 `tests/fixtures/wechat/`（按官方文档字段手写，非真实录制，见该目录 README）。
