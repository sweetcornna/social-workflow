import localFont from "next/font/local";

/**
 * 字体全部自托管，构建产物不含任何外链（brand-spec 硬约束）。
 *
 * - Body：Figtree（OFL-1.1，来自 `@fontsource/figtree`，node_modules 里的 woff2 直接进
 *   next/font/local，构建期不联网）。拉丁 400/600/700 三档静态字重，对齐 Organic 规格
 *   （styles.css 的 `Figtree:wght@400;600;700`）。它没有中日韩字形，正文本来就落在系统
 *   无衬线栈上，损失只在拉丁词与数字——回退链与 tailwind.config.ts 的 fontFamily.sans 一致。
 *   P14 起取代 Geist Sans（Geist Mono 不受影响，继续供 sw-num/sw-label 使用）。
 * - Display：Caprasimo（OFL-1.1，来自 `@fontsource/caprasimo`）。上游只发行 400 一档、无斜体，
 *   本批只定义 `--font-caprasimo` 变量与 fontFamily.display 映射，应用到 className 是 B2 的事
 *   （只允许登录页 wordmark 与侧栏 wordmark 两处引用）。中文没有字形，回退到与 sans 相同的
 *   系统无衬线栈；Caprasimo 400 本身很厚，换系统字后显薄，标题统一升到 font-weight 600 补厚度
 *   （porting-notes.md 第 1 节结论，是唯一允许的排版参数偏离）。
 */
// next/font 的编译期插件要求参数是字面量，不能引用外部常量（哪怕值完全一样）；
// 两处回退链只能各写一份，保持文字一致即可。
export const figtree = localFont({
  src: [
    {
      path: "../node_modules/@fontsource/figtree/files/figtree-latin-400-normal.woff2",
      weight: "400",
      style: "normal",
    },
    {
      path: "../node_modules/@fontsource/figtree/files/figtree-latin-600-normal.woff2",
      weight: "600",
      style: "normal",
    },
    {
      path: "../node_modules/@fontsource/figtree/files/figtree-latin-700-normal.woff2",
      weight: "700",
      style: "normal",
    },
  ],
  display: "swap",
  variable: "--font-figtree",
  fallback: ["PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "ui-sans-serif", "system-ui", "sans-serif"],
});

export const caprasimo = localFont({
  src: [
    {
      path: "../node_modules/@fontsource/caprasimo/files/caprasimo-latin-400-normal.woff2",
      weight: "400",
      style: "normal",
    },
  ],
  display: "swap",
  variable: "--font-caprasimo",
  fallback: ["PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "ui-sans-serif", "system-ui", "sans-serif"],
});
