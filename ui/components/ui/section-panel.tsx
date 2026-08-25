"use client";

import * as React from "react";

import { Panel, type PanelVariant } from "@/components/ui/panel";
import { cn } from "@/lib/utils";

/**
 * 带标题栏的内容面板：左标题（可带一句读数）+ 右操作区 + 分隔线。
 *
 * `subtitle` 保留，但口径收紧成**只放读数与状态**（「共 42 条」「离线模式 ·
 * 跑于 …」）。原来塞在这里的口径解释（"metrics 取每条内容最新一张快照再求和"）
 * 属于文档不属于界面——它每次渲染都在，却只在第一次有用。
 */
export interface SectionPanelProps extends Omit<React.HTMLAttributes<HTMLDivElement>, "title"> {
  title: React.ReactNode;
  subtitle?: React.ReactNode;
  actions?: React.ReactNode;
  variant?: PanelVariant;
  bodyClassName?: string;
}

export function SectionPanel({
  title,
  subtitle,
  actions,
  variant = "card",
  className,
  bodyClassName,
  children,
  ...rest
}: SectionPanelProps) {
  return (
    <Panel variant={variant} className={cn("flex flex-col overflow-hidden", className)} {...rest}>
      <div className="flex min-h-[3rem] items-center justify-between gap-3 border-b border-line px-4 py-2.5">
        <div className="min-w-0">
          <h2 className="truncate text-[13.5px] font-semibold text-fg">{title}</h2>
          {subtitle ? (
            <p className="sw-num mt-0.5 truncate text-[11px] text-fg-4">{subtitle}</p>
          ) : null}
        </div>
        {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
      </div>
      <div className={cn("min-h-0 flex-1", bodyClassName)}>{children}</div>
    </Panel>
  );
}

export default SectionPanel;
