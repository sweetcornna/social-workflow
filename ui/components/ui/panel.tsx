"use client";

import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * 面 primitive —— 全站唯一的容器语汇（P13 取代原 GlassPanel）。
 *
 * 只剩三档，且都不带模糊/渐变/高光：控制台的层级靠**底色差 + 一条细线**
 * 表达，不靠玻璃厚度。原来的 soft/strong/subtle/primary 四档里，
 * `primary`（琥珀外环，用来标"最该看的那张卡"）被整档删掉——每页都有一张
 * 卡在发光，等于没有一张卡在发光。
 *
 * - `card`（默认）：浮起的内容面，列表、面板、统计卡都用它
 * - `pop`：浮层（弹窗、命令面板、下拉菜单），更重的投影拉开层级
 * - `inset`：面**内部**的次级块，只换底不再浮起
 */
export type PanelVariant = "card" | "pop" | "inset";

const VARIANT: Record<PanelVariant, string> = {
  card: "sw-card",
  pop: "sw-pop",
  inset: "sw-inset",
};

export interface PanelProps extends React.HTMLAttributes<HTMLDivElement> {
  variant?: PanelVariant;
  as?: "div" | "section" | "aside";
}

export const Panel = React.forwardRef<HTMLDivElement, PanelProps>(function Panel(
  { variant = "card", as = "div", className, children, ...rest },
  ref,
) {
  const Tag = as;
  return (
    <Tag
      ref={ref}
      data-panel={variant}
      className={cn("relative rounded-card", VARIANT[variant], className)}
      {...rest}
    >
      {children}
    </Tag>
  );
});

export default Panel;
