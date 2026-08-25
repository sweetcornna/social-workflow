"use client";

import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * 骨架屏。
 *
 * dormice 纪律：**镜像真实结构**，逐行对齐真实卡片的解剖，loading 与 loaded
 * 等高不跳动；不许一块大 Skeleton 了事。所以这里不只导出一个方块，还导出了
 * 各真实版式对应的骨架（表格 / 统计卡），页面按自己接下来要渲染什么来挑。
 *
 * 判据很简单：如果 loading 态和 loaded 态并排截图，两张图的行数、行高、列位
 * 应当一一对上。对不上就说明骨架在骗人——它在说"马上会出现这个形状"，
 * 而实际出现的是另一个形状。
 */
export function Skeleton({ className, ...rest }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("sw-shimmer rounded-md", className)} {...rest} />;
}

/** 通用列表骨架：n 行等高条。用在还没换到 DataTable 的短列表上。 */
export function SkeletonRows({ rows = 4, className }: { rows?: number; className?: string }) {
  return (
    <div className={cn("flex flex-col gap-2 p-4", className)}>
      {Array.from({ length: rows }).map((_, i) => (
        <Skeleton key={i} className="h-11 w-full" />
      ))}
    </div>
  );
}

/**
 * 表格骨架：表头 h-12 + n 行 py-2.5，与 DataTable 的行高逐格对齐。
 * 首末列的 pl-6/pr-6 也照抄，骨架条的左右端点才落在真实文字的起止处。
 */
export function SkeletonTable({ rows = 8, cols = 4 }: { rows?: number; cols?: number }) {
  return (
    <div className="sw-card flex min-h-0 flex-1 flex-col overflow-hidden rounded-card" aria-hidden="true">
      <div className="flex h-12 shrink-0 items-center gap-4 border-b border-line pl-6 pr-6">
        {Array.from({ length: cols }).map((_, i) => (
          <Skeleton key={i} className={cn("h-3", i === 0 ? "w-32 flex-1" : "w-16")} />
        ))}
      </div>
      <div className="min-h-0 flex-1 overflow-hidden">
        {Array.from({ length: rows }).map((_, r) => (
          <div key={r} className="flex items-center gap-4 border-b border-line py-2.5 pl-6 pr-6">
            {Array.from({ length: cols }).map((_, i) => (
              <Skeleton key={i} className={cn("h-3.5", i === 0 ? "w-40 flex-1" : "w-14")} />
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

/**
 * 统计卡骨架：小标签 / 大数字 / footer 两行，与 StatCard 的四段解剖同高。
 * 两排都占位——只占一排的话，数据回来时卡片会往下长半截。
 */
export function SkeletonStat() {
  return (
    <div className="sw-card flex min-h-[108px] flex-col justify-between rounded-card p-4" aria-hidden="true">
      <Skeleton className="h-2.5 w-16" />
      <Skeleton className="mt-3 h-7 w-24" />
      <div className="mt-3 flex items-end justify-between gap-2">
        <div className="flex flex-1 flex-col gap-1.5">
          <Skeleton className="h-2.5 w-20" />
          <Skeleton className="h-2.5 w-14" />
        </div>
        <Skeleton className="h-4 w-12" />
      </div>
    </div>
  );
}

export default Skeleton;
