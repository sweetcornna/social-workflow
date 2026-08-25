"use client";

import * as React from "react";

import type { Tone } from "@/lib/format";
import { cn } from "@/lib/utils";

const FILL: Record<Tone, string> = {
  ok: "bg-ok",
  warn: "bg-warn",
  err: "bg-err",
  amber: "bg-primary",
  muted: "bg-fg-4",
};

/** 细进度条。渲染任务进度、预算占用都用它。 */
export function Progress({
  value,
  tone = "amber",
  className,
  label,
}: {
  value: number;
  tone?: Tone;
  className?: string;
  label?: string;
}) {
  const pct = Math.max(0, Math.min(100, Number.isFinite(value) ? value : 0));
  return (
    <div
      className={cn("h-1.5 w-full overflow-hidden rounded-full bg-muted-strong", className)}
      role="progressbar"
      aria-valuenow={Math.round(pct)}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label={label}
    >
      <div
        className={cn("h-full rounded-full transition-[width] duration-500", FILL[tone])}
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

/** 预算环。used/limit 画成一个 SVG 圆环，中间放百分比。 */
export function BudgetRing({
  used,
  limit,
  label,
  caption,
  size = 92,
}: {
  used: number;
  limit: number;
  label: string;
  caption?: string;
  size?: number;
}) {
  const pct = limit > 0 ? Math.min(100, (used / limit) * 100) : 0;
  const r = 38;
  const c = 2 * Math.PI * r;
  const tone = pct >= 90 ? "var(--sw-err)" : pct >= 70 ? "var(--sw-warn)" : "var(--sw-primary)";
  return (
    <div className="flex flex-col items-center gap-1.5">
      <svg width={size} height={size} viewBox="0 0 100 100" aria-hidden="true">
        <circle
          cx="50"
          cy="50"
          r={r}
          fill="none"
          stroke="var(--sw-muted-strong)"
          strokeWidth="9"
        />
        <circle
          cx="50"
          cy="50"
          r={r}
          fill="none"
          stroke={tone}
          strokeWidth="9"
          strokeLinecap="round"
          strokeDasharray={`${(c * pct) / 100} ${c}`}
          transform="rotate(-90 50 50)"
        />
        <text
          x="50"
          y="50"
          textAnchor="middle"
          dominantBaseline="central"
          className="sw-num"
          fontSize="20"
          fill="var(--sw-fg)"
        >
          {pct < 1 && pct > 0 ? "<1" : Math.round(pct)}
        </text>
        <text
          x="50"
          y="66"
          textAnchor="middle"
          dominantBaseline="central"
          fontSize="11"
          fill="var(--sw-fg-4)"
        >
          %
        </text>
      </svg>
      <span className="sw-label">{label}</span>
      {caption ? <span className="sw-num text-[11px] text-fg-3">{caption}</span> : null}
    </div>
  );
}
