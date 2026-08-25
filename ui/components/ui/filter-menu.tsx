"use client";

import * as React from "react";

import { IconCheck, IconPlus } from "@/components/icons";
import { Badge } from "@/components/ui/badge";
import { Popover } from "@/components/ui/popover";
import type { Tone } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * 工具栏筛选器（参照 dormice `components/FilterMenu.tsx`）。
 *
 * 形态：**药丸 + 加号**，选中后在药丸里长出一条竖分隔 + 一枚 Badge 显当前值。
 * 为什么是这个形态而不是原来那一排 chip：
 *  - 一排常驻 chip 会把"有哪些取值"和"现在选了什么"混在同一行，取值一多
 *    就换行，工具栏高度随数据抖动
 *  - 加号是"这里可以加一个条件"的通用语汇，收起时只占一个按钮宽，
 *    展开才列取值；当前值就写在按钮上，不用回头去那排 chip 里找哪个是亮的
 *
 * P14.B2 换掉虚线：虚线是发丝几何，Organic 明令禁止（"不画锐角，也不画只有
 * 发丝的几何"）。"未设条件"改用**更浅的填色**表达——未选是 muted 井，选中转
 * 陶土 tint + 深阶字，同一枚药丸靠底色深浅说话，不靠线型虚实。
 *
 * 单选。`value === ""` 即"全部"（不设条件），与后端的空串筛选口径一致。
 */

export interface FilterOption {
  value: string;
  label: string;
  count?: number;
  tone?: Tone;
}

export function FilterMenu({
  label,
  value,
  options,
  onChange,
  className,
}: {
  /** 筛选维度名，如「状态」「平台」。收起时显示它。 */
  label: string;
  value: string;
  options: FilterOption[];
  onChange: (next: string) => void;
  className?: string;
}) {
  const [open, setOpen] = React.useState(false);
  const anchor = React.useRef<HTMLButtonElement>(null);
  const current = options.find((o) => o.value === value);
  // 空串选项（「全部」）算"没设条件"，不在按钮上显示成一枚 Badge
  const active = Boolean(value) && Boolean(current);

  return (
    <>
      <button
        ref={anchor}
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        data-testid={`filter-${label}`}
        onClick={() => setOpen((v) => !v)}
        className={cn(
          "inline-flex h-7 shrink-0 items-center gap-1.5 rounded-pill px-2.5 text-[12px]",
          "transition-colors duration-150",
          active
            ? "bg-primary-soft text-primary-deep hover:bg-primary-line"
            : "bg-muted text-fg-3 hover:bg-primary-soft hover:text-primary-deep",
          className,
        )}
      >
        <IconPlus size={12} className="shrink-0" />
        <span className="whitespace-nowrap">{label}</span>
        {active ? (
          <>
            <span aria-hidden="true" className="mx-0.5 h-3.5 w-px shrink-0 bg-primary-line" />
            <Badge tone={current?.tone ?? "muted"}>{current?.label}</Badge>
          </>
        ) : null}
      </button>

      <Popover
        open={open}
        onClose={() => setOpen(false)}
        anchorRef={anchor}
        label={`${label}筛选`}
        className="max-h-[18rem] overflow-y-auto"
      >
        {options.map((o) => {
          const on = o.value === value;
          return (
            <button
              key={o.value || "__all"}
              type="button"
              role="menuitemradio"
              aria-checked={on}
              onClick={() => {
                onChange(o.value);
                setOpen(false);
              }}
              className={cn(
                "flex w-full items-center gap-2 whitespace-nowrap rounded-md px-2 py-1.5 text-left text-[12.5px]",
                "transition-colors hover:bg-muted",
                on ? "text-fg" : "text-fg-2",
              )}
            >
              <span className="flex h-3.5 w-3.5 shrink-0 items-center justify-center">
                {on ? <IconCheck size={13} className="text-primary" /> : null}
              </span>
              <span className="flex-1">{o.label}</span>
              {typeof o.count === "number" ? (
                <span className="sw-num text-[11px] text-fg-4">{o.count}</span>
              ) : null}
            </button>
          );
        })}
      </Popover>
    </>
  );
}

export default FilterMenu;
