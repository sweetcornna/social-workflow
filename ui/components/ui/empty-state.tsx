"use client";

import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * 空态：icon + 一句话 + 行动建议。
 *
 * P13 起它多了一个职责：**接住从页头下沉下来的那些解释**。页头不再挂副标
 * （见 PageHeader），"这一页会发生什么、为什么现在是空的、下一步做什么"
 * 全部写在这里——只有真的空着的时候才需要读它，读完就再也不会看见。
 *
 * 标题从衬线换成 sans/medium：衬线在中性灰阶的控制台里读起来像引文，
 * 而这是一句状态说明，不是引文。
 *
 * P14.B2：虚线框退场，换成一块 muted 填色的大圆角。虚线框在这套语言里是双重
 * 错误——既是发丝几何，又在暗示"这里可以拖进来点什么"，而空态其实只是在陈述
 * 一个事实。填色块只说"这一格是空的"，不多说。
 */
export interface EmptyStateProps extends React.HTMLAttributes<HTMLDivElement> {
  icon?: React.ReactNode;
  title: string;
  description?: React.ReactNode;
  action?: React.ReactNode;
}

export function EmptyState({
  icon,
  title,
  description,
  action,
  className,
  ...rest
}: EmptyStateProps) {
  return (
    <div
      role="status"
      className={cn(
        "mx-auto flex w-full max-w-md animate-fade-in flex-col items-center justify-center gap-2",
        "rounded-card bg-muted px-6 py-8 text-center",
        className,
      )}
      {...rest}
    >
      {icon ? (
        <span aria-hidden="true" className="text-fg-5 [&_svg]:h-6 [&_svg]:w-6">
          {icon}
        </span>
      ) : null}
      <div className="text-[14px] font-medium text-fg">{title}</div>
      {description ? (
        <div className="max-w-[38ch] text-[12px] leading-relaxed text-fg-3">{description}</div>
      ) : null}
      {action ? <div className="mt-1.5">{action}</div> : null}
    </div>
  );
}

export default EmptyState;
