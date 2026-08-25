"use client";

import * as React from "react";

import { Badge } from "@/components/ui/badge";
import { MiniSparkline } from "@/components/ui/mini-sparkline";
import { Panel } from "@/components/ui/panel";
import type { Tone } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * 统计卡 —— 按 dormice StatCard 的解剖统一（P13）。
 *
 * 四段，自上而下：**小标签 / 大数字 / footer 两行说明 / 右下角陪衬件**。
 * 原来那条横贯卡底的暖色山丘 sparkline 底纹撤了：它占掉卡片近三分之一的高度
 * 却不承载可读的量（没有刻度也没有 tooltip），是纯装饰。陪衬件缩到右下角一枚
 * 定宽柱图，走**中性主色**——四张卡并排时，颜色不该成为比较它们的依据。
 *
 * 四列布局下 footer 左栏只有约 8 个汉字宽，`hint` / `sub` 按这个上限写；
 * 超了会被 line-clamp 切掉（切出来的省略号是内容截断，不是状态省略号）。
 */
export interface StatCardProps {
  label: string;
  value: React.ReactNode;
  /** 数字右侧的小单位（%、条、秒）。 */
  unit?: string;
  /** footer 第一行：这个数字是怎么来的。 */
  hint?: React.ReactNode;
  /** footer 第二行：更细的口径或时间范围。 */
  sub?: React.ReactNode;
  /** 右下角陪衬件的数据序列。给了才画。 */
  series?: number[];
  badge?: { text: string; tone: Tone };
  onClick?: () => void;
  className?: string;
}

export function StatCard({
  label,
  value,
  unit,
  hint,
  sub,
  series,
  badge,
  onClick,
  className,
}: StatCardProps) {
  const bars = (series ?? []).slice(-14);
  const max = Math.max(...bars, 1);

  return (
    <Panel
      className={cn(
        "flex min-h-[108px] flex-col justify-between p-4",
        onClick && "cursor-pointer transition-colors hover:bg-muted",
        className,
      )}
      onClick={onClick}
    >
      <div className="flex items-start justify-between gap-2">
        <span className="sw-label truncate">{label}</span>
        {badge ? <Badge tone={badge.tone}>{badge.text}</Badge> : null}
      </div>

      <div className="mt-2.5 flex items-baseline gap-1">
        <span className="sw-num text-[26px] font-medium leading-none text-fg">{value}</span>
        {unit ? <span className="sw-num text-[12px] text-fg-4">{unit}</span> : null}
      </div>

      <div className="mt-2.5 flex items-end justify-between gap-3">
        <div className="min-w-0 flex-1">
          {hint ? (
            <div className="truncate text-[11.5px] leading-snug text-fg-3">{hint}</div>
          ) : null}
          {sub ? (
            <div className="sw-num truncate text-[11px] leading-snug text-fg-4">{sub}</div>
          ) : null}
        </div>
        {bars.length > 1 ? (
          <MiniSparkline
            className="shrink-0"
            height={18}
            bars={bars.map((v) => ({ height: (v / max) * 100, tone: "amber" }))}
          />
        ) : null}
      </div>
    </Panel>
  );
}

export default StatCard;
