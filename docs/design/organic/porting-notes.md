# Organic 移植笔记(中文控制台先例提炼)

来源:Claude Design "Organic" 项目 `qianmo/` 目录(spec-impl / spec-dark / page-chat 三份成稿,2026-08-18 拉取提炼)。
这些是把 Organic 落到一个中文控制台时验证过的结论,本项目 P14/P15 直接采信。

## 1. 字体(CJK 现实)

- Caprasimo 与 Figtree 都**没有中日韩字形**。中文界面正文本来就落在系统栈上,损失只在拉丁词与数字。
- 降级方案(推荐,先例采用):中文标题落系统无衬线栈,**标题统一 `font-weight: 600` 补厚度**——Caprasimo 400 本身厚,换中文系统字后 400 显薄。这是唯一允许的排版参数偏离。
- 自托管拉丁子集只救 wordmark 与大号数字(P14 的取舍:@fontsource figtree latin 做 body 拉丁面,caprasimo 只用于登录页/侧栏 wordmark)。
- Organic 的 `.btn` 用 heading 字体——中英混排按钮会看出两种字面,处理办法是**写文案时避开按钮内混拉丁**。
- Organic 无等宽 token,**必须自增 `--font-mono`**(地址/端点/时刻/trace 靠它对齐)。本项目继续 Geist Mono + tabular-nums。

## 2. 暗色派生(spec-dark 完整映射表)

规则:**亮色是完整一套,暗色只做重定义;色阶语义方向整条翻转**(亮底 100 浅→900 深,暗底 100 深→900 浅),这样「背景取 100 文字取 800」的组件类一个字节都不用改。任何颜色不许只定义在暗色块里。

| token | 亮 | 暗 | 暗色角色 |
|---|---|---|---|
| bg | #f5ead8 | #201e1d | 暖炭不是纯黑 |
| surface | #ebddc5 | #2e2b25 | 即亮色 neutral-900 档 |
| text | #201e1d | #f5ead8 | 与底对调 |
| accent | #c67139 | #f6a06b | 陶土抬到 400 档,对暗底 7.5:1 |
| accent-2 | #7a8a5e | #aebf92 | 鼠尾草抬到 400 档 |
| divider | text 16% 掺底 | text 18% 掺底 | |
| neutral 100..900 | #f9f4ed #eee7db #dcd3c4 #c0b6a5 #a19786 #82796a #645c50 #474238 #2e2b25 | #2a2723 #383430 #4a453d #625c51 #7d7568 #9a9184 #b8b0a2 #d5cec1 #ece6da | 整条翻转 |
| accent 100..900 | #fff2eb #ffe1d0 #ffc6a5 #f6a06b #d67f48 #b2622d #8c491a #643312 #402310 | #3a2113 #5b3115 #8c491a #b2622d #d67f48 #ffb888 #ffcfae #ffe4d2 #fff2eb | 500 两套同值(刻意锚点) |
| accent-2 100..900 | #f0fae1 #e1eecc #ccdbb2 #aebf92 #8fa073 #728157 #56633f #3d472b #272e1b | #1e2415 #2e3a1f #46562e #63783f #8fa073 #b8cc95 #d2e2b4 #e6f0d2 #f3f9e6 | 500 两套同值 |
| shadow-sm | 0 1px 2px 墨14% | 发丝亮边(奶油10%) | 暗底阴影不可见,换一圈亮边 |
| shadow-md | 0 3px 10px 墨16% | 亮边11% + 3px 12px 黑45% | 亮边定形,暗影定高度 |
| shadow-lg | 0 12px 32px 墨22% | 亮边13% + 16px 40px 黑55% | 对话框 |
| **scrim(新增)** | #2e2b25 半透 | #050403 半透 | 遮罩专用——neutral-900 翻转后变最浅档,不能再当遮罩 |

## 3. 色 token 移植要点(spec-impl)

- muted 文字用 `color-mix(text N%, transparent)` 掺底,**不用灰阶**——「不许把盘去饱和成灰」的直接落点。
- 卡片靠 surface 填色与底分界,**不再靠描边**;输入框在录入密集界面里退回 bg 色才读得出(卡内输入井比 surface 亮一档)。
- destructive 不做满屏红(先例归陶土深阶;本项目裁决保留一个"陶红" h≈30,理由见 P14 任务书)。
- 焦点环统一 `2px solid accent + offset 2px`;选中 nav 项 = 实心陶土药丸 + 底色文字(不是浅灰底)。
- 需要新增的 token 只有 `--font-mono` 与 `--color-scrim` 两个(先例结论;本项目另加 primary-solid/primary-deep,见任务书)。

## 4. 样式与行为门禁(先例采用,本项目同样遵守)

- CSS 无 `url()`(图标全部内联 SVG,含 select 下拉箭头:`appearance:none` + 绝对定位内联 SVG)。
- 动效只有 hover/active/focus 过渡,全部 150ms;`prefers-reduced-motion` 降到 0(本项目按 P13 口径:装饰全停、spinner 只减速)。
- Lucide 内联:`viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.75"` 圆头圆角;纯装饰 `aria-hidden`,纯图标按钮 `aria-label`。
- 折叠区优先原生 `<details>/<summary>`;默认值与量纲写进 label,不留空框让人猜。

## 5. 简化三原则(直接适用于 P14-B4)

1. 默认值写进 label 而不是留空框(「条数 · 默认 200 · 上限 500」)。
2. 后端钉死的值不做成输入框——改一行只读小字。
3. 自由文本能换成选择就换掉(打错这一整类错误就不存在了)。

## 6. 对话页解剖(page-chat,P15-S3 与未来对话面参照)

- 结构:264px 侧栏(sand 面板)+ 主区。侧栏 = 品牌位(拉丁小字 + 中文大字 wordmark)/ 药丸导航 / 「开一条新会话」主按钮 / 会话轨道分组(track:两行标题 + 元信息,选中态 accent-100 底 + accent-300 inset 描边)/ footer 环境说明。
- 转录:`msg` 网格(34px 圆头像 + 内容),气泡 = surface 底大圆角(用户消息 accent-100 底);msg-foot 放投递状态链(tag + 连接段)与元信息 mono 药丸。
- composer:surface 大圆角块,`focus-within` 整块焦点环;textarea 透明无边框;底部一行 = 目标 select(内联箭头)+ 状态点 + 圆形发送按钮。
- 空态:左文右 blob 装饰(内联 SVG 圆组合),标题 + 一句下一步 + 主按钮。
