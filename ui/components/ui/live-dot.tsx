"use client";

import * as React from "react";

import type { Tone } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * 8px 状态点，可选呼吸脉冲。
 * 全站唯一还允许动的装饰：`pulse` 打开一圈 ping。留着它是因为它表达的是
 * "这个东西此刻真的在跑"（调度器、投递中、渲染中），不是为了好看；
 * 徽标上的脉冲已经在 P13 删掉了，动效只此一处。
 * reduced-motion 由 `motion-safe:` 前缀接管，不在组件里订阅 matchMedia。
 */
const DOT: Record<Tone, string> = {
  ok: "bg-ok",
  warn: "bg-warn",
  err: "bg-err",
  amber: "bg-primary",
  muted: "bg-fg-4",
};

const RING: Record<Tone, string> = {
  ok: "bg-ok/45",
  warn: "bg-warn/45",
  err: "bg-err/50",
  amber: "bg-primary/45",
  muted: "bg-fg-4/30",
};

export interface LiveDotProps extends React.HTMLAttributes<HTMLSpanElement> {
  tone?: Tone;
  pulse?: boolean;
  label?: string;
  size?: number;
}

export const LiveDot = React.forwardRef<HTMLSpanElement, LiveDotProps>(function LiveDot(
  { tone = "ok", pulse = false, label, size = 8, className, ...rest },
  ref,
) {
  const style = { width: size, height: size } as React.CSSProperties;
  return (
    <span
      ref={ref}
      className={cn("relative inline-flex shrink-0", className)}
      style={style}
      data-tone={tone}
      {...rest}
    >
      {pulse ? (
        <span
          aria-hidden="true"
          className={cn(
            "absolute inset-0 rounded-full motion-safe:animate-ping motion-reduce:hidden",
            RING[tone],
          )}
        />
      ) : null}
      <span
        aria-hidden="true"
        className={cn("relative inline-flex rounded-full", DOT[tone])}
        style={style}
      />
      {label ? <span className="sr-only">{label}</span> : null}
    </span>
  );
});

export default LiveDot;
