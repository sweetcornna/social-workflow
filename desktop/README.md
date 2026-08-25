# 工作台桌面版（macOS / Windows）

把 `ui/` 那个工作台装进一个 Electron 薄壳，做成可安装的桌面应用。
**壳里没有 core**：它只是把打包进去的静态产物挂起来，再把数据面反代到你配置的 core 地址。

- appId `xyz.cornna.sw-workbench`
- macOS：`.dmg` + `.zip`，arm64 与 x64 各一份，**不签名不公证**（见下面「首次打开」）
- Windows：NSIS 安装包，x64，非静默、不需要管理员、装在当前用户下
- 产物名一律纯 ASCII：`sw-workbench-<版本>-<os>-<arch>.<ext>`

---

## 它到底做了什么

起进程时在 `127.0.0.1` 上开一个**随机端口**的 HTTP 服务（`src/server.js`），然后开一个窗口指向 `/workbench/`：

| 路径 | 行为 |
| --- | --- |
| `/workbench/**` | 直接读 app 里打包的 `ui/out`。语义对齐 core 的 `StaticFiles(html=True)`：目录补 `index.html`、未命中回 Next 导出的 `404.html`；`_next/static/` 缓存一年，HTML 一律 `no-cache` |
| `/api/**` | **流式**反代到 core |
| `/review/**` | 同上。封面 / 图集 / 成片 / 公众号预览走的就是这里，pipe 不缓冲，`Range` 原样透传（大 mp4 能拖进度条） |
| 其它 | 404 |

几件刻意为之的事：

- **`Host` 头原样转发**。core 的 `trailingSlash` 会产生 307/308，FastAPI 用 `Host` 拼 `Location`——转发原 Host，跳转才会指回壳自己，而不是把浏览器踢到 core 的地址上。
- **core 连不上时壳不接管界面**。`/api/*` 回一个合法 envelope（`{"ok":false,"error":{"code":"core_unreachable",…}}`），工作台按自己的离线态渲染。壳不弹窗、不换页。
- **不是开放代理**。只 listen 环回地址；请求行必须是 origin-form（`GET /path`），绝对形式 URI 直接 400；代理目标永远是当前配置的那一个 origin，跟请求头无关。
- **app 里不落任何密钥**。UI token 还是 UI 自己放在浏览器 localStorage 里，配置文件只有一个 `coreOrigin`。

安全开关：`contextIsolation` 开、`nodeIntegration` 关、`sandbox` 开；站外链接一律交给系统浏览器；single instance lock。

## core 地址

优先级：

1. 环境变量 `SW_CORE_ORIGIN`
2. `userData/config.json` 里的 `coreOrigin`（菜单「设置 core 地址…」写的就是它）
3. 默认 `http://127.0.0.1:18000`（P17 的本机隧道口）

`userData` 的位置：macOS `~/Library/Application Support/social_workflow 工作台/`，
Windows `%APPDATA%\social_workflow 工作台\`。
菜单里的小窗会把当前生效地址和配置文件路径显示出来；env 压着配置文件时也会明说。

只存 origin（`scheme://host:port`），**路径会被丢掉**——core 的 `/api/v1` 与 `/review` 都挂在根上。

## 首次打开（macOS）

应用**没有签名也没有公证**（本项目没有 Apple 开发者账号），Gatekeeper 会拦一下。两种绕法：

1. 「访达」里**右键点应用 → 打开**，弹窗里再点一次「打开」；
2. 或者去掉隔离标记：

   ```bash
   xattr -dr com.apple.quarantine "/Applications/social_workflow 工作台.app"
   ```

Windows 的 SmartScreen 同理：点「更多信息 → 仍要运行」。

---

## 本地开发

```bash
# 1. 先出静态产物（壳 dev 态直接吃 ui/out）。别设 NEXT_PUBLIC_SW_CHAT_URL——桌面版不出对话入口
cd ui && pnpm install && pnpm build

# 2. 一份隔离 core（独立 SQLite + fake 发布器，不碰 data/）
bash ui/e2e/serve.sh 8000

# 3. 起壳
cd desktop && pnpm install
SW_CORE_ORIGIN=http://127.0.0.1:8000 pnpm start
```

`pnpm install` 之后如果 `node_modules/electron/path.txt` 不存在，说明 pnpm 的
side-effects 缓存跳过了 electron 的 postinstall，补一次即可：

```bash
node node_modules/electron/install.js
```

### 冒烟

```bash
node scripts/smoke.mjs --core http://127.0.0.1:8000 --out /tmp/sw-shell-shots
```

两轮：带 `SW_CORE_ORIGIN` 起一次（验 env 优先级 + 渲染 + 数据面全 2xx），再用空的
userData 起一次，走**真实的菜单回调**把地址改成 core / 改成错地址 / 再改回来，
每一步都落截图和请求日志。playwright 从 `ui/node_modules` 借，桌面壳自己不引它。

### 图标

`assets/icon.html` 是源文件（Organic 配色 + Caprasimo 的 `sw`），
`scripts/make-icon.mjs` 用 playwright 把它栅格化成 `assets/icon.png`（1024×1024，透明角）。
electron-builder 再从这张 png 自动转 icns / ico。字体是 OFL 的，只在生成时从
`ui/node_modules/@fontsource/caprasimo` 现读现内联，不入库。

```bash
node scripts/make-icon.mjs
```

### 打包

```bash
pnpm build:mac   # dmg + zip，arm64/x64
pnpm build:win   # nsis x64（在 Windows 上跑；本机 mac 跑不出来）
```

产物在 `desktop/dist/`。

## 发版

`.github/workflows/desktop-release.yml`：打 `v*` tag 自动跑 macOS + Windows 两条矩阵，
构建完把产物追加到对应 tag 的 Release。也可以手动 dispatch：填 tag 就发 Release，
留空就只构建、产物挂在 workflow artifacts 上（拿来验 Windows 能不能过）。

---

## 验证留痕（P18，本机 macOS 26 / Apple Silicon 真跑）

`screenshots/` 里的四张都来自**真实进程**，零 mock：

| 图 | 是什么 |
| --- | --- |
| `01-packaged-app-today.png` | 从 `sw-workbench-0.1.0-mac-arm64.dmg` 挂载出来的 `.app`（`app.isPackaged=true`），今日页 |
| `02-packaged-app-review-video.png` | 同一个打包应用的审核台：成片经 `/review/{id}/media/0` 代理流式播出（`206` + `Content-Range`） |
| `03-core-origin-prompt.png` | 菜单「设置 core 地址…」的小窗 |
| `04-core-unreachable.png` | core 连不上时**工作台自己的**离线态，壳没有接管界面 |

Windows 产物**没有**在本机验证过（electron-builder 在 macOS 上打 win 包要先下 winCodeSign/wine
那套东西，本机拉不下来），只能由 `desktop-release.yml` 的 windows-latest 矩阵证明。
