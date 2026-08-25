"use client";

import * as React from "react";

import type { Tone } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * 定宽柱状 sparkline —— 统计卡右下角的陪衬件。
 *
 * 为什么每根柱子都带一条**基线**：上一版只画有值的部分，于是全 0 的序列
 * 渲染成一排悬空的小短横，看着像界面画坏了而不是"这几天真的是 0"。
 * 现在零值落在基线上、有值从基线长上去，读出来就是"有 / 没有"。
 *
 * 中性色：四张统计卡并排时，颜色不该成为比较它们的依据（dormice 的
 * 「sparkline 中性色」那条针对的就是这种无好坏之分的量）。
 */
export interface SparkBar {
  /** 0–100 的高度百分比。 */
  height: number;
  tone?: Tone;
}

const TONE_VAR: Record<Tone, string> = {
  ok: "var(--sw-ok)",
  warn: "var(--sw-warn)",
  err: "var(--sw-err)",
  amber: "var(--sw-primary)",
  muted: "var(--sw-fg-4)",
};

export interface MiniSparklineProps extends React.HTMLAttributes<HTMLDivElement> {
  bars: SparkBar[];
  height?: number;
  label?: string;
}

export function MiniSparkline({
  bars,
  height = 16,
  label,
  className,
  ...rest
}: MiniSparklineProps) {
  return (
    <div
      className={cn("flex items-end gap-[3px] border-b border-line-strong", className)}
      style={{ height }}
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      {...rest}
    >
      {bars.map((b, i) => {
        const pct = Math.max(0, Math.min(100, Number.isFinite(b.height) ? b.height : 0));
        return (
          <span
            key={i}
            className="w-[3px] rounded-[1px]"
            style={{
              // 零值也留 1px，落在基线上就是一个"这天是 0"的读数
              height: pct <= 0 ? 1 : `${Math.max(12, pct)}%`,
              background:
                pct <= 0
                  ? "var(--sw-line-strong)"
                  : `color-mix(in oklch, ${TONE_VAR[b.tone ?? "amber"]} 55%, transparent)`,
            }}
          />
        );
      })}
    </div>
  );
}

export default MiniSparkline;
