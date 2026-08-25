"use client";

import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * 输入井（P14.B2）。
 *
 * 两处换法都来自 porting-notes 第 3 节："卡片靠 surface 填色与底分界，不再靠
 * 描边；输入框在录入密集界面里退回 bg 色才读得出"。所以输入框不是"卡面色 +
 * 一条描边"，而是**比卡面亮一档的 canvas 井**，边框整条退场——一屏十几个字段
 * 的表单里，十几条描边比十几块底色吵得多。
 *
 * hover 再往 popover（neutral-100）抬半档，是"这里可以点"的最轻表达。
 */
const BASE =
  "w-full border-0 bg-canvas px-3.5 py-2 " +
  "text-[13px] text-fg placeholder:text-fg-4 transition-colors duration-150 " +
  "hover:bg-popover";

export const Input = React.forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(
  function Input({ className, ...rest }, ref) {
    return (
      <input ref={ref} className={cn(BASE, "h-[2.125rem] rounded-pill py-0", className)} {...rest} />
    );
  },
);

/**
 * 多行输入是药丸化的**例外**：文本从第二行起会贴着 999px 的圆弧走，左边界
 * 一行比一行缩进。多行结构一律退回 16px 基准圆角（Organic radius-md）。
 */
export const Textarea = React.forwardRef<
  HTMLTextAreaElement,
  React.TextareaHTMLAttributes<HTMLTextAreaElement>
>(function Textarea({ className, ...rest }, ref) {
  return (
    <textarea
      ref={ref}
      className={cn(BASE, "min-h-[80px] rounded-md leading-relaxed", className)}
      {...rest}
    />
  );
});

/**
 * 下拉箭头（P14.B2 交接项，B4 落地）。
 *
 * 原生 `<select>` 的箭头是浏览器画的——三个平台三种几何，暗色下常年是一块刺眼的
 * 白底三角。porting-notes 第 4 节的门禁是"CSS 无 `url()`，select 下拉箭头
 * `appearance:none` + 绝对定位内联 SVG"，这里是唯一实现处：`<select>` 本身去掉
 * 原生外观，箭头改成同一枚 `IconChevronDown` 的路径，绝对定位叠上去，
 * `pointer-events-none` 让点击穿透到底下的 `<select>`。
 *
 * 包一层 `relative` div 是必须的：箭头要相对"这个控件"定位，而不是相对页面上
 * 更远的祖先——`className`（宽度类之类）挪到外层 div 上，`<select>` 自己永远
 * `w-full` 撑满，两者才不会因为 `cn()` 的类合并互相打架。
 */
export const Select = React.forwardRef<
  HTMLSelectElement,
  React.SelectHTMLAttributes<HTMLSelectElement>
>(function Select({ className, children, ...rest }, ref) {
  return (
    <div className={cn("relative", className)}>
      <select
        ref={ref}
        className={cn(BASE, "h-[2.125rem] w-full appearance-none rounded-pill py-0 pr-8")}
        {...rest}
      >
        {children}
      </select>
      <svg
        aria-hidden="true"
        focusable="false"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={2.25}
        strokeLinecap="round"
        strokeLinejoin="round"
        className="pointer-events-none absolute right-3 top-1/2 h-3 w-3 -translate-y-1/2 text-fg-4"
      >
        <path d="m6 9 6 6 6-6" />
      </svg>
    </div>
  );
});

export function FieldLabel({ className, children, ...rest }: React.LabelHTMLAttributes<HTMLLabelElement>) {
  return (
    <label className={cn("sw-label mb-1.5 block", className)} {...rest}>
      {children}
    </label>
  );
}

/** 复选框：视频"已完整观看"闸门用的就是它。 */
export function Checkbox({
  checked,
  onChange,
  label,
  hint,
  disabled,
  id,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: React.ReactNode;
  hint?: React.ReactNode;
  disabled?: boolean;
  id?: string;
}) {
  const autoId = React.useId();
  const inputId = id ?? autoId;
  return (
    <div className="flex items-start gap-2.5">
      <input
        id={inputId}
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
        // accent-color 已由 globals.css 的 base 层统一给到所有 input，这里不再重复
        className="mt-[3px] h-3.5 w-3.5 shrink-0 cursor-pointer disabled:cursor-not-allowed disabled:opacity-45"
      />
      <label
        htmlFor={inputId}
        className={cn(
          "cursor-pointer text-[12.5px] leading-relaxed text-fg-2",
          disabled && "cursor-not-allowed opacity-60",
        )}
      >
        {label}
        {hint ? <span className="mt-0.5 block text-[11.5px] text-fg-3">{hint}</span> : null}
      </label>
    </div>
  );
}
