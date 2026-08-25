"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import * as React from "react";

import { IconSearch } from "@/components/icons";
import { CommandPalette } from "@/components/layout/command-palette";
import { PublisherModeBanner } from "@/components/layout/publisher-mode-banner";
import { ThemeToggle } from "@/components/layout/theme-toggle";
import { POLL, useApi } from "@/lib/hooks";
import { NAV, navItemFor } from "@/lib/nav";
import type { SystemInfo } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * 专注外壳 —— 审核台专用。
 *
 * 审核台是这个工作台唯一"一坐下就是一小时"的页面，媒体必须占到屏宽一半以上，
 * 所以它不进普通外壳（15rem 侧栏 + inset 卡面的内外边距会把中间挤没）。
 * 这里换成一条 44px 的细导航轨：品牌 + 五格动线 + ⌘K + 日夜，剩下整屏交给内容。
 * 这是**任务书点名保留的三区版式**，P13 只重新着装，不动结构。
 *
 * 导航轨的药丸与侧栏同一套语汇（`sw-nav-item` 是竖栏形态，这里是横排形态），
 * 但同样 `cursor-default`：它也是应用 chrome。
 *
 * `/system/info` 同样兼作认证探针：token 模式下它 401，api 层直接把人送去登录页。
 */
export default function FocusLayout({ children }: { children: React.ReactNode }) {
  const { data: info } = useApi<SystemInfo>("/system/info", undefined, {
    refreshInterval: POLL.accounts,
  });
  const pathname = usePathname() ?? "/";
  const active = navItemFor(pathname);
  const [paletteOpen, setPaletteOpen] = React.useState(false);

  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((v) => !v);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  return (
    <div className="flex h-svh flex-col overflow-hidden bg-canvas">
      <header className="flex h-11 shrink-0 items-center gap-3 border-b border-line px-3">
        <Link
          href="/"
          className="shrink-0 cursor-default font-display text-[15px] leading-none tracking-tight text-fg"
          aria-label="回到今日"
        >
          social_workflow
        </Link>

        <nav
          aria-label="主导航"
          className="sw-scroll flex min-w-0 items-center gap-0.5 overflow-x-auto"
        >
          {NAV.map((item) => {
            const Icon = item.icon;
            const isActive = active?.href === item.href;
            return (
              <Link
                key={item.href}
                href={item.href}
                aria-current={isActive ? "page" : undefined}
                title={item.hint}
                className={cn(
                  // 与侧栏、移动横轨同一句话：选中实底陶土药丸，hover 陶土 tint
                  "flex shrink-0 cursor-default items-center gap-1.5 rounded-pill px-2.5 py-1",
                  "text-[12.5px] transition-colors duration-150",
                  isActive
                    ? "bg-primary-solid font-medium text-primary-fg"
                    : "text-fg-3 hover:bg-primary-soft hover:text-primary-deep",
                )}
              >
                <Icon size={13} className={cn(!isActive && "opacity-70")} />
                <span className="hidden md:inline">{item.label}</span>
              </Link>
            );
          })}
        </nav>

        <span className="flex-1" />

        <button
          type="button"
          onClick={() => setPaletteOpen(true)}
          aria-label="打开命令面板"
          className={cn(
            "hidden cursor-default items-center gap-2 rounded-pill bg-muted px-2.5 py-1",
            "text-[12px] text-fg-3 transition-colors duration-150",
            "hover:bg-primary-soft hover:text-primary-deep sm:flex",
          )}
        >
          <IconSearch size={13} />
          <kbd className="sw-num text-[10px] text-fg-4">⌘K</kbd>
        </button>
        <ThemeToggle />
      </header>

      <PublisherModeBanner info={info} className="shrink-0 border-x-0 border-t-0" />

      <main className="min-h-0 flex-1">{children}</main>

      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} />
    </div>
  );
}
