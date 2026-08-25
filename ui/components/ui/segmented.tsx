"use client";

import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * 分段控件 —— 视图 / tab / 时间档位的切换器（P13 取代原 FilterChipGroup）。
 *
 * 与 FilterMenu 的分工是**语义分工，不是外观分工**：
 *  - 取值是"同一份数据的几种看法"（时间线 / 列表、7 / 14 / 30 天、系统五个
 *    tab）→ 分段控件。取值少、必须一眼全见、切换是无损的。
 *  - 取值是"筛掉一部分数据"（状态、平台、渲染态）→ FilterMenu。取值可能很多、
 *    收起时只需要显示"现在筛的是什么"。
 *
 * 原来两者共用一个组件，于是系统页的五个 tab 和排期页的六档状态长得一模一样，
 * 人得靠位置猜哪个是换页哪个是过滤。
 *
 * 形态是连排方格（不是一排独立药丸）：连排才读得出"这几个是一组、互斥"。
 * 角色仍是 tablist/tab —— 键盘与读屏的语义没变。
 */
export interface SegmentOption {
  value: string;
  label: string;
  count?: number;
  disabled?: boolean;
}

export function SegmentedControl({
  options,
  value,
  onChange,
  label,
  className,
  "data-testid": testId,
}: {
  options: SegmentOption[];
  value: string;
  onChange: (next: string) => void;
  label?: string;
  className?: string;
  "data-testid"?: string;
}) {
  return (
    <div
      role="tablist"
      aria-label={label}
      data-testid={testId}
      className={cn(
        // 药丸轨（Organic 收尾一行把 .seg 也压成 999px）：容器走 muted 井，
        // 去描边，靠底色差把"这几个是一组"圈起来
        "inline-flex shrink-0 items-center rounded-pill bg-muted p-0.5",
        className,
      )}
    >
      {options.map((opt) => {
        const active = opt.value === value;
        return (
          <button
            key={opt.value}
            type="button"
            role="tab"
            aria-selected={active}
            disabled={opt.disabled}
            data-active={active || undefined}
            onClick={() => onChange(opt.value)}
            className={cn(
              // transition-none：系统页 tab 挂在轮询数据上方，留着过渡会跟着重渲闪
              "flex h-6 items-center gap-1 whitespace-nowrap rounded-pill px-2.5 text-[12px] transition-none",
              // 选中态是**陶土 tint 药丸**，不是实底：实底陶土在这套语言里被
              // 签名规则留给了"导航选中 + 主按钮"两处；页内的"看哪一份"用
              // 浅一档的 tint 表达，与 FilterMenu 的选中态同一句话
              active
                ? "bg-primary-soft font-medium text-primary-deep"
                : "text-fg-3 hover:text-fg",
              opt.disabled && "cursor-not-allowed opacity-45",
            )}
          >
            {opt.label}
            {typeof opt.count === "number" ? (
              <span className="sw-num text-[11px] opacity-70">{opt.count}</span>
            ) : null}
          </button>
        );
      })}
    </div>
  );
}

export default SegmentedControl;
