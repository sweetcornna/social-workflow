import type { Config } from "tailwindcss";

/**
 * Organic 设计系统主题的 Tailwind 映射（P14）。
 *
 * 颜色全部指向 `app/globals.css` 的 `--sw-*` 语义 token，日/夜自动切换；
 * 这里**不写死任何色值**，改色只改一处——P14.B1 只换 token 的值，这份映射表
 * 本身（键名）不动。
 *
 * 与上一版（企业控制台/P13）的差别：圆角基准从 10px 升到 16px、card 从 12px
 * 升到 28px，新增 pill（药丸）与 radius-sm 两档；新增 primary-solid/primary-deep
 * （陶土的两个功能性深浅档）与 scrim（modal 遮罩语义色）。字体栈：sans 从
 * Geist Sans 换成 Figtree（+ 中文回退链不变），新增 display（Caprasimo，仅登录页/
 * 侧栏 wordmark 使用，本批只定义），serif 退场（Instrument Serif 包已移除）。
 * mono 不动，继续是 Geist Mono，供 sw-num/sw-label 与代码类文本使用。
 */
const config: Config = {
  darkMode: ["class"],
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // 基础语义层：`border` 让裸 `border` 类与全局 `*` 默认边框同源
        border: "var(--sw-line)",
        background: "var(--sw-canvas)",
        foreground: "var(--sw-fg)",

        // 底与面
        canvas: "var(--sw-canvas)",
        card: "var(--sw-card)",
        popover: "var(--sw-popover)",
        muted: "var(--sw-muted)",
        "muted-hover": "var(--sw-muted-hover)",
        "muted-strong": "var(--sw-muted-strong)",
        "row-hover": "var(--sw-row-hover)",
        line: "var(--sw-line)",
        "line-strong": "var(--sw-line-strong)",

        // 文字灰阶
        fg: "var(--sw-fg)",
        "fg-2": "var(--sw-fg-2)",
        "fg-3": "var(--sw-fg-3)",
        "fg-4": "var(--sw-fg-4)",
        "fg-5": "var(--sw-fg-5)",

        // 主色与状态语义色
        primary: "var(--sw-primary)",
        "primary-fg": "var(--sw-primary-fg)",
        "primary-solid": "var(--sw-primary-solid)",
        "primary-deep": "var(--sw-primary-deep)",
        "primary-soft": "var(--sw-primary-soft)",
        "primary-line": "var(--sw-primary-line)",
        "primary-band": "var(--sw-primary-band)",
        ok: "var(--sw-ok)",
        "ok-soft": "var(--sw-ok-soft)",
        warn: "var(--sw-warn)",
        "warn-soft": "var(--sw-warn-soft)",
        err: "var(--sw-err)",
        "err-soft": "var(--sw-err-soft)",
        // modal 遮罩语义色（暖炭半透，日夜两套见 globals.css）
        scrim: "var(--sw-scrim)",
      },
      boxShadow: {
        card: "var(--sw-shadow-card)",
        pop: "var(--sw-shadow-pop)",
      },
      borderRadius: {
        DEFAULT: "var(--sw-radius)",
        lg: "var(--sw-radius)",
        md: "var(--sw-radius)",
        sm: "var(--sw-radius-sm)",
        card: "var(--sw-card-radius)",
        pill: "var(--sw-pill-radius)",
      },
      transitionTimingFunction: {
        "sw-out": "cubic-bezier(0.16, 1, 0.3, 1)",
      },
      fontFamily: {
        // Figtree 只有拉丁字形，中文回退走系统无衬线栈（与 fonts.ts 的
        // CJK_FALLBACK 一致，PingFang SC 起头覆盖 macOS/iOS 大多数中文场景）。
        sans: [
          "var(--font-figtree)",
          "PingFang SC",
          "Hiragino Sans GB",
          "Microsoft YaHei",
          "ui-sans-serif",
          "system-ui",
          "sans-serif",
        ],
        mono: [
          "var(--font-geist-mono)",
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "monospace",
        ],
        // 签名字体：仅登录页与侧栏 wordmark 两处类名允许引用（B2 应用）。
        // Caprasimo 无中文字形，回退到与 sans 相同的系统栈；标题字重升级
        // （400→600 补厚）由组件层的 CJK 处理规则负责，不写进字体栈。
        display: [
          "var(--font-caprasimo)",
          "PingFang SC",
          "Hiragino Sans GB",
          "Microsoft YaHei",
          "ui-sans-serif",
          "system-ui",
          "sans-serif",
        ],
      },
      keyframes: {
        "fade-in": {
          from: { opacity: "0", transform: "translateY(2px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        "pop-in": {
          "0%": { opacity: "0", transform: "translateY(-6px) scale(0.99)" },
          "100%": { opacity: "1", transform: "translateY(0) scale(1)" },
        },
      },
      animation: {
        "fade-in": "fade-in 160ms ease-out",
        "pop-in": "pop-in 160ms cubic-bezier(0.16, 1, 0.3, 1) both",
      },
    },
  },
  plugins: [require("tailwindcss-animate")],
};

export default config;
