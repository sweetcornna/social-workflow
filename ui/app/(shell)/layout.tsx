"use client";

import * as React from "react";

import { CommandPalette } from "@/components/layout/command-palette";
import { PublisherModeBanner } from "@/components/layout/publisher-mode-banner";
import { Sidebar } from "@/components/layout/sidebar";
import { POLL, useApi } from "@/lib/hooks";
import type { Dashboard, SystemInfo } from "@/lib/types";

/**
 * 工作台外壳 —— inset 版式，**锁定视口高**（参照 dormice AppShell）。
 *
 * 版式
 * ----
 * 外层 `h-svh overflow-hidden`。P14.B2 把层级反转过来：**侧栏是浮起的沙色板，
 * 内容区直接坐在奶油底上**（P13 是反的）。摘掉内容区的卡面之后，页面自己的
 * 卡片（列表、面板、统计卡）才是屏幕上唯一浮起的一层——原来它们是"卡上叠卡"，
 * 两层 surface 之间只差一档明度，卡片边界基本读不出来。
 * 滚动**只发生在内容区的滚动口**（`min-h-0 flex-1 overflow-y-auto`）。
 * 这条纪律换来的是列表页那个形状：表格吃满剩余高度、行在框内滚、分页条钉底
 * ——页面整体跟着滚的话，分页条会一直躺在首屏之外。
 *
 * 页面自带容器（max-w / padding 按页型，见各页根节点），外壳只给滚动口。
 *
 * 顶栏已删（P13）：面包屑是页头的第二份真相；⌘K、主题、退出都进了侧栏。
 *
 * `/system/info` 兼作认证探针——token 模式下它会 401，api 层的默认处理器
 * 直接把人送去 /workbench/login/。所以这里不用再写一遍登录门。
 *
 * 审核台不走这层壳（见 app/(focus)/），它要把整块屏让给媒体。
 */
export default function ShellLayout({ children }: { children: React.ReactNode }) {
  const { data: info } = useApi<SystemInfo>("/system/info", undefined, {
    refreshInterval: POLL.accounts,
  });
  // 侧栏上的"审核台 N"角标：导航本身就该回答"有没有事等我"
  const { data: dash } = useApi<Dashboard>("/dashboard", undefined, {
    refreshInterval: POLL.dashboard,
  });
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
    <div className="flex h-svh flex-col overflow-hidden lg:flex-row">
      <Sidebar
        info={info}
        badges={{ "/review/": dash?.counters.pending_review ?? 0 }}
        onOpenPalette={() => setPaletteOpen(true)}
      />

      <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
        {/*
          警示条钉在滚动口**外面**：它一旦进了滚动口，列表页那句 `h-full`
          就会连带把它的高度算进去，表格与页面同时出现滚动条。
        */}
        <PublisherModeBanner info={info} className="shrink-0" />
        <main className="sw-scroll min-h-0 flex-1 overflow-y-auto">{children}</main>
      </div>

      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} />
    </div>
  );
}
