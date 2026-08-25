"use client";

import * as React from "react";

import { IconClock, IconMinus, IconPlus } from "@/components/icons";
import { Button } from "@/components/ui/button";
import { describeDrafts, type WindowDraft } from "@/lib/windows";
import { cn } from "@/lib/utils";

/** 常用作息。一键铺上去比让人一格格填时间快得多。 */
const PRESETS: { label: string; hint: string; drafts: WindowDraft[] }[] = [
  {
    label: "全天",
    hint: "不限时段",
    drafts: [],
  },
  {
    label: "午休 + 晚间",
    hint: "12:00-14:00 / 19:00-22:30",
    drafts: [
      { start: "12:00", end: "14:00" },
      { start: "19:00", end: "22:30" },
    ],
  },
  {
    label: "早高峰",
    hint: "07:00-09:00",
    drafts: [{ start: "07:00", end: "09:00" }],
  },
  {
    label: "工作时段",
    hint: "10:00-12:00 / 14:00-18:00",
    drafts: [
      { start: "10:00", end: "12:00" },
      { start: "14:00", end: "18:00" },
    ],
  },
];

const DAY_MIN = 24 * 60;

function minutes(hhmm: string): number | null {
  const m = hhmm.match(/^(\d{2}):(\d{2})$/);
  if (!m) return null;
  return Number(m[1]) * 60 + Number(m[2]);
}

/** 逐段精确比较，判断当前值是不是原样某个预设——决定要不要默认展开自定义段。 */
function draftsEqual(a: WindowDraft[], b: WindowDraft[]): boolean {
  if (a.length !== b.length) return false;
  return a.every((d, i) => d.start === b[i].start && d.end === b[i].end);
}

/**
 * 发布时段的可视化编辑器。
 *
 * 设计取舍：既给**时间轴预览**（一眼看出"晚上那段是不是太窄"），也保留
 * `<input type="time">` 的精确输入——纯拖拽在 3 小时的窗口上分不清 19:00 和 19:15，
 * 而排期是按分钟算的。
 *
 * 简化三原则第 1 条（P14.B4）：**预设药丸是主路径**，四枚常用作息置顶，点一下就完事，
 * 大多数人不用再往下看。自定义段（逐段增删时间输入）默认收起，只在两种情形展开：
 * 人主动点了「自定义」，或者当前值本来就不是任何预设（多半是在编辑一个历史账号，
 * 已经有非预设的时间——原样展开，不假装它是某个预设）。
 */
export function WindowEditor({
  value,
  onChange,
  error,
}: {
  value: WindowDraft[];
  onChange: (next: WindowDraft[]) => void;
  error?: string;
}) {
  const set = (index: number, patch: Partial<WindowDraft>) =>
    onChange(value.map((d, i) => (i === index ? { ...d, ...patch } : d)));

  const preview = React.useMemo(() => describeDrafts(value), [value]);

  const matchedPreset = React.useMemo(
    () => PRESETS.find((p) => draftsEqual(p.drafts, value)),
    [value],
  );
  const isNonPreset = !matchedPreset;
  const [forceCustom, setForceCustom] = React.useState(isNonPreset);
  const showCustom = forceCustom || isNonPreset;

  return (
    <div className="flex flex-col gap-2.5" data-testid="window-editor">
      {/* 预设药丸：置顶，主路径——点一下就完事，不用碰下面的自定义段 */}
      <div className="flex flex-wrap items-center gap-1.5" role="tablist" aria-label="发布窗口预设">
        {PRESETS.map((p) => {
          const active = !showCustom && matchedPreset === p;
          return (
            <button
              key={p.label}
              type="button"
              role="tab"
              aria-selected={active}
              title={p.hint}
              data-testid={`window-preset-${p.label}`}
              onClick={() => {
                onChange(p.drafts.map((d) => ({ ...d })));
                setForceCustom(false);
              }}
              className={cn(
                "rounded-pill px-2.5 py-1 text-[11.5px] transition-colors duration-150",
                active
                  ? "bg-primary-soft font-medium text-primary-deep"
                  : "bg-muted text-fg-3 hover:bg-primary-soft hover:text-primary-deep",
              )}
            >
              {p.label}
            </button>
          );
        })}
        <button
          type="button"
          role="tab"
          aria-selected={showCustom}
          data-testid="window-preset-custom"
          onClick={() => setForceCustom(true)}
          className={cn(
            "rounded-pill px-2.5 py-1 text-[11.5px] transition-colors duration-150",
            showCustom
              ? "bg-primary-soft font-medium text-primary-deep"
              : "bg-muted text-fg-3 hover:bg-primary-soft hover:text-primary-deep",
          )}
        >
          自定义
        </button>
      </div>

      {/* 24 小时时间轴：底色 = 会发的时段，不管在哪种模式下都留着 */}
      <div
        className="relative h-9 overflow-hidden rounded-lg bg-muted"
        aria-hidden="true"
      >
        {value.length === 0 ? (
          <div className="absolute inset-0 bg-primary/30" />
        ) : (
          value.flatMap((d, i) => {
            const s = minutes(d.start);
            const e = minutes(d.end);
            if (s === null || e === null || s === e) return [];
            const spans = e > s ? [[s, e]] : [
              [s, DAY_MIN],
              [0, e],
            ];
            return spans.map(([from, to], j) => (
              <div
                key={`${i}-${j}`}
                className="absolute inset-y-0 bg-primary/55 shadow-[inset_0_0_0_1px_var(--sw-primary)]"
                style={{
                  left: `${(from / DAY_MIN) * 100}%`,
                  width: `${((to - from) / DAY_MIN) * 100}%`,
                }}
              />
            ));
          })
        )}
        <div className="absolute inset-0 flex">
          {Array.from({ length: 8 }, (_, i) => (
            <div
              key={i}
              className={cn(
                "flex-1 border-r border-line/70 pt-[1px] text-center font-mono text-[9px] text-fg-4",
                i === 7 && "border-r-0",
              )}
            >
              {String(i * 3).padStart(2, "0")}
            </div>
          ))}
        </div>
      </div>

      {showCustom ? (
        <>
          {value.map((draft, index) => (
            <div key={index} className="flex items-center gap-2">
              <IconClock size={12} className="shrink-0 text-fg-4" />
              <input
                type="time"
                value={draft.start}
                aria-label={`第 ${index + 1} 段开始时间`}
                data-testid={`window-start-${index}`}
                onChange={(e) => set(index, { start: e.target.value })}
                className="sw-num h-8 rounded-pill bg-canvas px-3 text-[12.5px] text-fg"
              />
              <span className="text-fg-4">—</span>
              <input
                type="time"
                value={draft.end}
                aria-label={`第 ${index + 1} 段结束时间`}
                data-testid={`window-end-${index}`}
                onChange={(e) => set(index, { end: e.target.value })}
                className="sw-num h-8 rounded-pill bg-canvas px-3 text-[12.5px] text-fg"
              />
              <Button
                size="sm"
                variant="ghost"
                aria-label={`删掉第 ${index + 1} 段`}
                data-testid={`window-remove-${index}`}
                onClick={() => onChange(value.filter((_, i) => i !== index))}
              >
                <IconMinus size={12} />
              </Button>
            </div>
          ))}

          <Button
            size="sm"
            data-testid="window-add"
            onClick={() => onChange([...value, { start: "09:00", end: "11:00" }])}
          >
            <IconPlus size={12} />
            加一段
          </Button>
        </>
      ) : null}

      <p
        className={cn("text-[11.5px] leading-relaxed", error ? "text-err" : "text-fg-3")}
        data-testid="window-preview"
      >
        {error || preview}
      </p>
    </div>
  );
}

export default WindowEditor;
