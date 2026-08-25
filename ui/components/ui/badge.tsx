"use client";

import * as React from "react";

import type { Tone } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * 状态徽标。
 *
 * P13 定下、本次保留的两条：字体走 sans（徽标里绝大多数是中文，mono 没有汉字
 * 字形，实际渲染的是回退字体，字重行高都对不齐；数字仍 tabular-nums），以及
 * 没有 `pulse` 脉冲（会闪的徽标看久了就成了装饰，"正在进行"由 LiveDot 一处表达）。
 *
 * P14.B2 换的是形与分界：Organic 的 `.tag` 是**纯 tint 填色的药丸**——100 档底
 * 配 700/800 档字，一条描边都不画。P13 那句"药丸形留给侧栏导航项"随之作废：
 * 导航选中态现在是实底陶土，与 tint 徽标在颜色上就分得开，不必再靠形状分工。
 * amber 档的字从 primary(500) 降到 primary-deep(700)：500 落在 accent-100 上
 * 只有 3 点几比一，Organic 自己也写了"tint 底上的正文要用深阶"。
 */
const TONE: Record<Tone, string> = {
  ok: "text-ok bg-ok-soft",
  warn: "text-warn bg-warn-soft",
  err: "text-err bg-err-soft",
  amber: "text-primary-deep bg-primary-soft",
  // 中性档特意用 muted-hover（neutral-300）：muted 与卡面只差一档明度，
  // 徽标会糊在卡面上——它得读得出是一枚"贴上去的"标签
  muted: "text-fg-2 bg-muted-hover",
};

export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement> {
  tone?: Tone;
}

export function Badge({ tone = "muted", className, children, ...rest }: BadgeProps) {
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-pill px-2 py-[1px]",
        "text-[11px] font-medium leading-[1.5] tabular-nums",
        TONE[tone],
        className,
      )}
      {...rest}
    >
      {children}
    </span>
  );
}

export default Badge;
