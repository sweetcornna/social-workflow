# arm64 本地构建（服务器是 aarch64 时用）

上游 `xpzouying/xiaohongshu-mcp` 只发 amd64 镜像，且源码两处写死 amd64：

1. `Dockerfile` 构建段 `GOARCH=amd64` —— 产出的 Go 二进制在 arm64 上起不来
   （crash-loop，日志是 `./app: 1: ELF: not found`，shell 把 amd64 ELF 当脚本解释）。
2. 内置浏览器 CDN（`cdn.one-world.ai/browsers/<ver>/`）只有
   linux-x64 / macos-arm64 / windows-x64，没有 linux-arm64；
   `browser/browser_download.go` 的 `platformAsset()` 在 linux/arm64 直接返回
   "无预编译浏览器" 并拒绝启动。

另有第三坑：上游经 headless_browser 传的 `--fingerprint-platform=windows` 启动
flag 只有他们自家魔改浏览器认识，stock Chromium 151/arm64 实测会让页面加载崩掉
（`-32000 Inspected target navigated or closed`，login/status 必现；纯 rod 同浏览器
同页面正常，逐 flag 二分定位到它）。

## 方案（2026-08-17 生产验证）

- `browser-bin-override.patch`（两处）：
  1. `browser/browser_download.go`：给 `EnsureBrowser()` 加 `XHS_BROWSER_BIN`
     环境变量覆盖——设了就直接用指定的外部浏览器二进制，跳过 CDN 下载。
  2. `browser/browser.go`：`XHS_BROWSER_BIN` 非空时跳过全部指纹选项
     （`WithFingerprint` / `fingerprint-brand` extra flag）——stock 浏览器不认这些
     flag 且会崩；同时这也是 POLICY 要求（不做反检测）。
- `Dockerfile.arm64`：Go 按本机架构构建；运行段换 `debian:bookworm-slim` +
  Debian 仓库的 arm64 `chromium`，`XHS_BROWSER_BIN=/usr/bin/chromium`、
  `CHROMIUM_FLAGS=--no-sandbox`（容器内 root 跑 Chromium 必须）。
- stock Chromium 会忽略上游内置浏览器才认识的指纹 flag——本方案**不引入也不
  保留任何反检测能力**（POLICY 红线，反而比上游更"素"）。

## 服务器上重建步骤

```bash
cd /root && git clone --branch v2.5.0 https://github.com/xpzouying/xiaohongshu-mcp
cd xiaohongshu-mcp
git apply /path/to/browser-bin-override.patch
cp /path/to/Dockerfile.arm64 .
docker build -f Dockerfile.arm64 -t xiaohongshu-mcp:v2.5.0-arm64 .
```

然后 `.env` 里 `SW_XHS_MCP_IMAGE=xiaohongshu-mcp:v2.5.0-arm64`，
已存在的账号容器在工作台账号卡里点「重建」（volume 不删，登录态保留）。

升级上游版本时：重新 checkout 新 tag，`git apply` 本补丁（若冲突需按新代码重打），
Dockerfile.arm64 的 chromium 版本随 Debian 仓库走，无需改动。
