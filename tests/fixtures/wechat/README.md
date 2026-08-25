# tests/fixtures/wechat —— 公众号接口样例响应

这些 JSON 是**按官方文档字段手写**的样例响应（不是从真实公众号录制的——本仓库没有真实
凭据，也不允许把真实 token / media_id 落盘）。字段名与结构逐条对照官方文档核对，
核对结论与"未核实"项写在 `publishers/wechat_mp/client.py` 的模块 docstring 里。

`tests/publishers/test_wechat_mp.py` 用 `respx` 把它们挂到对应路由上重放。

| 文件 | 对应接口 | 场景 |
|---|---|---|
| `stable_token.json` | `cgi-bin/stable_token` | 成功 |
| `stable_token_40164.json` | 同上 | 出口 IP 不在白名单 |
| `stable_token_40125.json` | 同上 | AppSecret 无效 |
| `errcode_40001.json` | 任意 | access_token 失效（触发刷新重试） |
| `errcode_45009.json` | 任意 | 达到日调用上限（可重试） |
| `media_uploadimg.json` | `cgi-bin/media/uploadimg` | 正文配图，返回 mmbiz URL |
| `material_add_material_image.json` | `cgi-bin/material/add_material?type=image` | 封面永久素材 |
| `draft_add.json` | `cgi-bin/draft/add` | 建草稿成功 |
| `draft_get.json` | `cgi-bin/draft/get` | 单条草稿详情 |
| `draft_batchget.json` | `cgi-bin/draft/batchget` | 对账用草稿列表 |
| `freepublish_submit.json` | `cgi-bin/freepublish/submit` | 提交发布任务 |
| `freepublish_get_publishing.json` | `cgi-bin/freepublish/get` | `publish_status=1` 发布中 |
| `freepublish_get_success.json` | 同上 | `publish_status=0` 成功 + `article_url` |
| `freepublish_get_failed.json` | 同上 | `publish_status=3` 常规失败 + `fail_idx` |
| `freepublish_batchget.json` | `cgi-bin/freepublish/batchget` | 对账用已发布列表 |
| `datacube_getarticletotal.json` | `datacube/getarticletotal` | 图文总数据（累计量） |
| `datacube_getusersummary.json` | `datacube/getusersummary` | 用户增减 |
