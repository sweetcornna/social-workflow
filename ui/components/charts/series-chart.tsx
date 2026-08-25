"use client";

import * as React from "react";

import { fmtCompact, fmtDay } from "@/lib/format";

/**
 * 自绘 SVG 图表（不引图表库）。两种形态就够用了：
 *  - `GroupedBars`：每日发布 / 死信双序列柱图
 *  - `CostLine`：成本曲线 + 预算虚线
 * 配色只走暖色（琥珀 / 余烬 / 暖绿 / 暗红），凉色是 brand-spec 禁区。
 */

export interface Series {
  key: string;
  label: string;
  color: string;
  values: number[];
}

export function GroupedBars({
  labels,
  series,
  height = 190,
}: {
  labels: string[];
  series: Series[];
  height?: number;
}) {
  const max = Math.max(1, ...series.flatMap((s) => s.values));
  const cols = labels.length || 1;
  // 刻度：量级小的时候用整数档，免得 0.25/0.5/0.75 全被四舍五入成同一个数
  const gridLines =
    max <= 4
      ? Array.from({ length: max + 1 }, (_, i) => i / max)
      : [0, 0.25, 0.5, 0.75, 1];

  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-3">
        {series.map((s) => (
          <span key={s.key} className="flex items-center gap-1.5 text-[11.5px] text-fg-3">
            <span
              aria-hidden
              className="h-2 w-2 rounded-[2px]"
              style={{ background: s.color }}
            />
            {s.label}
          </span>
        ))}
      </div>
      <div className="relative" style={{ height }}>
        {/* 网格线 */}
        <div className="absolute inset-0 flex flex-col justify-between">
          {gridLines
            .slice()
            .reverse()
            .map((g) => (
              <div key={g} className="flex items-center gap-2">
                <span className="sw-num w-8 shrink-0 text-right text-[10px] text-fg-5">
                  {fmtCompact(Math.round(max * g))}
                </span>
                <span className="h-px flex-1 bg-line" />
              </div>
            ))}
        </div>
        {/* 柱子 */}
        <div className="absolute inset-0 flex items-end gap-1 pl-10">
          {Array.from({ length: cols }).map((_, i) => (
            <div key={i} className="flex h-full flex-1 items-end justify-center gap-[2px]">
              {series.map((s) => {
                const v = s.values[i] ?? 0;
                const pct = (v / max) * 100;
                return (
                  <div
                    key={s.key}
                    className="w-full max-w-[16px] rounded-t-[3px] transition-[height] duration-500"
                    style={{
                      height: `${Math.max(v > 0 ? 3 : 0.6, pct)}%`,
                      background: s.color,
                      opacity: v > 0 ? 0.85 : 0.22,
                    }}
                    title={`${labels[i]} · ${s.label} ${v}`}
                  />
                );
              })}
            </div>
          ))}
        </div>
      </div>
      <div className="mt-1.5 flex gap-1 pl-10">
        {labels.map((l) => (
          <span
            key={l}
            className="sw-num flex-1 text-center text-[10px] text-fg-4"
            title={l}
          >
            {fmtDay(l)}
          </span>
        ))}
      </div>
    </div>
  );
}

export function CostLine({
  labels,
  values,
  budget,
  height = 190,
  color = "var(--sw-primary)",
  unit = "",
}: {
  labels: string[];
  values: number[];
  /** 预算线（今日闸门），画成暗红虚线。0 或缺省则不画。 */
  budget?: number;
  height?: number;
  color?: string;
  unit?: string;
}) {
  const gradientId = React.useId();
  const max = Math.max(1, ...values, budget ?? 0);
  const w = 100;
  const h = 100;
  const n = Math.max(values.length, 2);
  const pts = values.map((v, i) => ({
    x: (i / (n - 1)) * w,
    y: h - (v / max) * h * 0.92,
  }));
  const line = pts.map((p, i) => `${i === 0 ? "M" : "L"} ${p.x.toFixed(2)} ${p.y.toFixed(2)}`).join(" ");
  const area = pts.length ? `${line} L ${w} ${h} L 0 ${h} Z` : "";
  const budgetY = budget && budget > 0 ? h - (budget / max) * h * 0.92 : null;

  return (
    <div>
      <div className="relative" style={{ height }}>
        <svg
          viewBox={`0 0 ${w} ${h}`}
          preserveAspectRatio="none"
          width="100%"
          height="100%"
          style={{ display: "block" }}
        >
          <defs>
            <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={color} stopOpacity="0.34" />
              <stop offset="100%" stopColor={color} stopOpacity="0.03" />
            </linearGradient>
          </defs>
          {[0.25, 0.5, 0.75].map((g) => (
            <line
              key={g}
              x1="0"
              x2={w}
              y1={h * g}
              y2={h * g}
              stroke="var(--sw-line)"
              strokeWidth="1"
              vectorEffect="non-scaling-stroke"
            />
          ))}
          {area ? <path d={area} fill={`url(#${gradientId})`} /> : null}
          {line ? (
            <path
              d={line}
              fill="none"
              stroke={color}
              strokeWidth="2"
              vectorEffect="non-scaling-stroke"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          ) : null}
          {budgetY !== null ? (
            <line
              x1="0"
              x2={w}
              y1={budgetY}
              y2={budgetY}
              stroke="var(--sw-err)"
              strokeWidth="1.5"
              strokeDasharray="4 3"
              vectorEffect="non-scaling-stroke"
            />
          ) : null}
        </svg>
      </div>
      <div className="mt-1.5 flex justify-between">
        {labels.map((l, i) =>
          i === 0 || i === labels.length - 1 || i === Math.floor(labels.length / 2) ? (
            <span key={l} className="sw-num text-[10px] text-fg-4">
              {fmtDay(l)}
            </span>
          ) : null,
        )}
      </div>
      {budget && budget > 0 ? (
        <p className="sw-num mt-1 text-[10.5px] text-fg-4">
          <span className="mr-1 inline-block h-px w-4 border-t border-dashed border-err align-middle" />
          今日预算线 {fmtCompact(budget)} {unit}
        </p>
      ) : null}
    </div>
  );
}
