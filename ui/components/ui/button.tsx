"use client";

import * as React from "react";

import { cn } from "@/lib/utils";

export type ButtonVariant = "primary" | "ghost" | "outline" | "danger";
export type ButtonSize = "sm" | "md" | "icon";

/**
 * 四态一律走**色阶过档**，不再用 opacity 调明暗（P14.B2）。
 *
 * Organic 原文的 `.btn-primary` 是 500 底 / hover 600 / active 700 三档递进。
 * 我们的起点被迫抬高一档：500 陶土配奶油字只有 3.9:1（Organic 自己也写明
 * accent-to-ground 只保证 3:1，够 chrome 不够正文），所以实底按钮从 600
 * （--sw-primary-solid）起步，hover 走到 700（--sw-primary-deep）。色阶到此
 * 用尽，按压态便改用一层内投影表达"按下去了"——比再造一个 800 档 token 诚实。
 *
 * 安静的两档（outline / ghost）hover 一律是**陶土 tint**：Organic 要求每个可
 * 交互元素都有一层取自 accent 色阶的 hover，灰底 hover 在这张沙色卡面上根本
 * 看不出来（muted 与 card 只差一档明度）。按压再深一档到 accent-300。
 */
const VARIANT: Record<ButtonVariant, string> = {
  primary: [
    "bg-primary-solid text-primary-fg border-transparent",
    "hover:bg-primary-deep active:bg-primary-deep",
    "active:shadow-[inset_0_2px_4px_var(--sw-scrim)]",
  ].join(" "),
  // 默认按钮：填色分界（muted 井）而不是描边，hover 转陶土 tint
  outline: "border-transparent bg-muted text-fg-2 hover:bg-primary-soft hover:text-primary-deep active:bg-primary-line",
  ghost:
    "border-transparent bg-transparent text-fg-3 hover:bg-primary-soft hover:text-primary-deep active:bg-primary-line",
  danger: "border-transparent bg-transparent text-err hover:bg-err-soft active:bg-err-soft",
};

const SIZE: Record<ButtonSize, string> = {
  sm: "h-7 px-2.5 text-[12px] gap-1.5",
  md: "h-[2.125rem] px-3 text-[13px] gap-1.5",
  // 行操作的「⋯」触发器：正方形，不给文字留位
  icon: "h-7 w-7 p-0 text-[12px]",
};

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  loading?: boolean;
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "outline", size = "md", loading, className, children, disabled, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type="button"
      disabled={disabled || loading}
      data-loading={loading || undefined}
      className={cn(
        // whitespace-nowrap + shrink-0：列宽紧张时中文按钮名被劈成两行是最常见的塌法
        // rounded-pill：Organic 的 `.btn` 收尾一行把按钮/标签/输入全压成 999px
        "inline-flex shrink-0 select-none items-center justify-center whitespace-nowrap rounded-pill border",
        "font-medium transition-[background-color,color,box-shadow] duration-150",
        "disabled:cursor-not-allowed disabled:opacity-45",
        SIZE[size],
        VARIANT[variant],
        className,
      )}
      {...rest}
    >
      {loading ? <Spinner /> : null}
      {children}
    </button>
  );
});

/**
 * 常驻的加载指示。
 *
 * dormice 铁律：loading 是 `<Spinner /> + 原样不变的文案`，**不是**文案后面
 * 缀省略号。所以这个组件只负责转，一个字都不带。
 */
export function Spinner({ className }: { className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        "h-3 w-3 shrink-0 animate-spin rounded-full border-[1.5px] border-current border-t-transparent",
        className,
      )}
    />
  );
}

export default Button;
