import type { Metadata, Viewport } from "next";
import { GeistMono } from "geist/font/mono";

import { Providers } from "@/components/providers";
import { THEME_BOOT_SCRIPT } from "@/lib/theme";
import { caprasimo, figtree } from "./fonts";
import "./globals.css";

export const metadata: Metadata = {
  title: "运营工作台 · social_workflow",
  description: "三平台内容运营工作台：审核、排期、账号健康、成本与复盘。",
};

export const viewport: Viewport = {
  themeColor: [
    // 与 globals.css 的 --sw-canvas 同色：移动端浏览器的地址栏底色要跟着
    // 页面底色，否则顶上会浮一条别的灰。P14 换成 Organic 的暖奶油/暖炭。
    { media: "(prefers-color-scheme: light)", color: "#f5ead8" },
    { media: "(prefers-color-scheme: dark)", color: "#201e1d" },
  ],
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html
      lang="zh-CN"
      suppressHydrationWarning
      className={`${figtree.variable} ${GeistMono.variable} ${caprasimo.variable}`}
    >
      <head>
        {/* 主题在水合前定死，避免浅底闪一下再翻成深底 */}
        <script dangerouslySetInnerHTML={{ __html: THEME_BOOT_SCRIPT }} />
      </head>
      {/* antialiased 是 dormice 点名的一条：macOS 默认亚像素渲染会把 medium 描成 semibold */}
      <body className="min-h-dvh bg-canvas font-sans text-fg antialiased">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
