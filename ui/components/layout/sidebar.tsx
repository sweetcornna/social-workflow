"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import * as React from "react";

import { IconExternal, IconLogout, IconSearch } from "@/components/icons";
import { ThemeToggle } from "@/components/layout/theme-toggle";
import { LiveDot } from "@/components/ui/live-dot";
import { setToken } from "@/lib/api";
import { toneForAccount } from "@/lib/format";
import { externalNav, NAV, navItemFor } from "@/lib/nav";
import type { SystemInfo } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * 侧栏 —— 沙色浮板形态（P14.B2 把 P13 的层级反转过来）。
 *
 * P13 是"侧栏融进底色、内容区是唯一浮起的卡"。Organic 反过来：**导航是一块
 * 沙色面板浮在奶油底上，内容区直接坐在底色上**。理由有两条，一条视觉一条结构：
 *  - 视觉：内容区占了八成屏幕，把它整块罩在一张 surface 上，等于让整个应用
 *    变成 surface 色，奶油底只剩四条边——那样奶油就不是底色，是描边。
 *  - 结构：导航是常驻 chrome，内容是流动的。让常驻的那块有形（浮板 + 28px
 *    圆角），流动的那块无形，人扫一眼就知道哪边是"这台机器"、哪边是"今天的事"。
 *
 * 选中态是**实底陶土药丸**（`.sw-nav-item[data-active]`，定义在 globals.css）：
 * 全站两处实底陶土之一，另一处是主按钮。
 *
 * P13 一并接手了原顶栏的全部职责（顶栏已删）：
 *  - ⌘K 入口成了侧栏头部的第一个菜单项（面包屑是页头的第二份真相，删掉了）
 *  - 主题切换与退出登录下沉到 footer
 *  - 连接状态由 footer 的调度器读数一处表达，不再另设一枚"已连接"药丸
 *
 * `badges` 是导航项上的待办数（目前只有审核台用）——导航本身就该回答
 * "有没有事等我"，不该让人点进去才知道队列是空的。
 */
export function Sidebar({
  info,
  badges,
  onOpenPalette,
}: {
  info?: SystemInfo;
  badges?: Partial<Record<string, number>>;
  onOpenPalette: () => void;
}) {
  const pathname = usePathname();
  const active = navItemFor(pathname ?? "/");
  const logout = React.useCallback(() => {
    setToken("");
    window.location.assign("/workbench/login/");
  }, []);

  const items = NAV.map((item) => ({
    ...item,
    isActive: active?.href === item.href,
    badge: badges?.[item.href] ?? 0,
  }));
  // 「对话」这类外链入口（P14.B5）：没配 NEXT_PUBLIC_SW_CHAT_URL 就是空数组，
  // 两处 map 都不会多渲染一个 DOM 节点——没有半个占位符这回事
  const external = externalNav(info);

  return (
    <>
      {/* ── 桌面：15rem 竖栏 ──────────────────────────────────────────── */}
      <aside
        aria-label="主导航"
        className="sw-card hidden w-[var(--sw-sidebar-w)] shrink-0 flex-col gap-1 px-2.5 py-4 lg:my-2 lg:ml-2 lg:flex"
      >
        <div className="px-2 pb-2">
          {/*
            wordmark 是全站两处 Caprasimo 之一（另一处是登录页）。它是纯拉丁串，
            正好落在自托管的拉丁子集上——中文标题拿不到这张脸，所以 display 字体
            只用在这两个地方，不铺到别的标题上假装有。
          */}
          <Link
            href="/"
            className="block cursor-default truncate font-display text-[17px] leading-none tracking-tight text-fg"
          >
            social_workflow
          </Link>
        </div>

        <button
          type="button"
          onClick={onOpenPalette}
          className="sw-nav-item"
          aria-label="打开命令面板"
        >
          {/* 图标与标签都不写死颜色：hover 时整枚药丸转陶土 tint，
              里面的字与图标得跟着一起走，否则 hover 出来一半是灰的 */}
          <IconSearch size={15} className="shrink-0 opacity-70" />
          <span className="flex-1 text-left font-normal">搜索或跳转</span>
          <kbd className="sw-num shrink-0 rounded-pill bg-muted px-1.5 text-[10px] text-fg-3">
            ⌘K
          </kbd>
        </button>

        <nav className="mt-1 flex flex-col gap-0.5">
          {items.map((item) => {
            const Icon = item.icon;
            return (
              <Link
                key={item.href}
                href={item.href}
                aria-current={item.isActive ? "page" : undefined}
                data-active={item.isActive}
                className="sw-nav-item"
              >
                {/* 选中态是实底陶土，图标必须跟着走底色文字；未选中压到 70%
                    不透明度而不是另给一档灰，hover 才能整枚一起变色 */}
                <Icon size={16} className={cn("shrink-0", !item.isActive && "opacity-70")} />
                <span className="flex-1 truncate">{item.label}</span>
                {item.badge > 0 ? <NavCount n={item.badge} onSolid={item.isActive} /> : null}
              </Link>
            );
          })}
        </nav>

        {external.length > 0 ? (
          <nav aria-label="外部入口" className="mt-1 flex flex-col gap-0.5">
            {external.map((item) => (
              <a
                key={item.href}
                href={item.href}
                target="_blank"
                rel="noopener"
                title={item.hint}
                data-testid="external-nav-item"
                className="sw-nav-item"
              >
                <item.icon size={16} className="shrink-0 opacity-70" />
                <span className="flex-1 truncate">{item.label}</span>
                <IconExternal size={11} className="shrink-0 opacity-50" />
              </a>
            ))}
          </nav>
        ) : null}

        <div className="flex-1" />

        <div className="flex flex-col gap-2 border-t border-line px-2 pt-3">
          <div className="flex min-w-0 items-center gap-2">
            <LiveDot
              tone={info?.scheduler_enabled ? toneForAccount("ok") : "muted"}
              pulse={Boolean(info?.scheduler_enabled)}
              label={info?.scheduler_enabled ? "调度器运行中" : "调度器已关闭"}
              size={7}
            />
            <span className="truncate text-[11.5px] text-fg-3">
              {info?.scheduler_enabled ? "调度器运行中" : "调度器已关"}
            </span>
          </div>
          <div className="sw-num truncate text-[10.5px] text-fg-4">
            v{info?.version ?? "—"} · {info?.env ?? "—"}
          </div>
          <div className="flex items-center justify-between gap-2">
            <ThemeToggle />
            {info?.auth_required ? (
              <button
                type="button"
                title="退出登录"
                aria-label="退出登录"
                className="cursor-default rounded-pill p-1.5 text-fg-4 transition-colors duration-150 hover:bg-primary-soft hover:text-primary-deep"
                onClick={logout}
              >
                <IconLogout size={14} />
              </button>
            ) : null}
          </div>
        </div>
      </aside>

      {/* ── 手机 / 平板：一条横向导航轨 ────────────────────────────────
          竖栏在 <lg 上会把内容挤没，但导航不能因此消失——顶栏删掉之后，
          这条轨是小屏上唯一的换页入口。 */}
      <div className="flex shrink-0 items-center gap-2 px-3 py-2 lg:hidden">
        <Link href="/" className="shrink-0 font-display text-[15px] leading-none tracking-tight text-fg">
          sw
        </Link>
        <nav
          aria-label="主导航"
          className="sw-scroll flex min-w-0 flex-1 items-center gap-1 overflow-x-auto"
        >
          {items.map((item) => {
            const Icon = item.icon;
            return (
              <Link
                key={item.href}
                href={item.href}
                aria-current={item.isActive ? "page" : undefined}
                title={item.hint}
                className={cn(
                  // 横轨与竖栏同一句话：选中是实底陶土药丸，hover 是陶土 tint
                  "flex shrink-0 items-center gap-1.5 rounded-pill px-2.5 py-1 text-[12.5px]",
                  "transition-colors duration-150",
                  item.isActive
                    ? "bg-primary-solid font-medium text-primary-fg"
                    : "text-fg-3 hover:bg-primary-soft hover:text-primary-deep",
                )}
              >
                <Icon size={14} className="shrink-0" />
                <span className="hidden sm:inline">{item.label}</span>
                {item.badge > 0 ? <NavCount n={item.badge} onSolid={item.isActive} /> : null}
              </Link>
            );
          })}
          {external.map((item) => (
            <a
              key={item.href}
              href={item.href}
              target="_blank"
              rel="noopener"
              title={item.hint}
              data-testid="external-nav-item"
              className="flex shrink-0 items-center gap-1.5 rounded-pill px-2.5 py-1 text-[12.5px] text-fg-3 transition-colors duration-150 hover:bg-primary-soft hover:text-primary-deep"
            >
              <item.icon size={14} className="shrink-0" />
              <span className="hidden sm:inline">{item.label}</span>
              <IconExternal size={10} className="shrink-0 opacity-60" />
            </a>
          ))}
        </nav>
        <button
          type="button"
          onClick={onOpenPalette}
          aria-label="搜索或跳转"
          className="shrink-0 rounded-pill p-1.5 text-fg-4 transition-colors duration-150 hover:bg-primary-soft hover:text-primary-deep"
        >
          <IconSearch size={15} />
        </button>
        <ThemeToggle />
      </div>
    </>
  );
}

/**
 * 导航项上的待办计数。实心陶土 —— 它就是"有事等你"本身。
 *
 * `onSolid` 是它坐在**选中态那枚实底陶土药丸**上时的翻转配色：同色叠同色会
 * 直接消失，所以在药丸上改成奶油底 + 陶土数字（正好是药丸自己的反相）。
 */
function NavCount({ n, onSolid }: { n: number; onSolid?: boolean }) {
  return (
    <span
      className={cn(
        "sw-num shrink-0 rounded-pill px-1.5 py-[1px] text-[10px]",
        onSolid ? "bg-primary-fg text-primary-solid" : "bg-primary-solid text-primary-fg",
      )}
      aria-label={`${n} 条待处理`}
    >
      {n}
    </span>
  );
}

export default Sidebar;
