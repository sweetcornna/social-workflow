"use client";

import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * 页头 —— openasi / dormice 版式（P13 取代原 30px 衬线大标题 + 段落副标）。
 *
 * 规矩只有三条：
 *  1. `h1 text-xl font-semibold`，一档字号，不随页面变。字重是 P14.B2 从
 *     medium 升上来的：Organic 的标题face 是 Caprasimo（本身就厚），中文落到
 *     系统栈之后 400/500 显薄，porting-notes 给的处方是"中文标题统一 600 补
 *     厚度"——这是全系统唯一允许的排版参数偏离
 *  2. **不带副标描述**。原来每页顶上那段解释（"改期走的是与批准即排期完全
 *     同一套校验…"）是给第一次来的人写的，对每天用它的人是永久占位的噪音。
 *     有信息量的那几句下沉到空态或就近的帮助位，纯氛围的删掉。
 *  3. 操作按钮与标题 `justify-between` 同行
 *
 * `emphasis` 是标题的第二段（「排期 · 几点发什么」里的后半句）：它不是描述，
 * 是这一页的职能本身，所以留在 h1 里，只压成次级色。
 */
export function PageHeader({
  title,
  emphasis,
  actions,
  className,
}: {
  title: React.ReactNode;
  emphasis?: React.ReactNode;
  actions?: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex flex-wrap items-center justify-between gap-x-4 gap-y-2", className)}>
      <h1 data-testid="page-header" className="min-w-0 text-xl font-semibold tracking-tight text-fg">
        {title}
        {emphasis ? (
          <span className="ml-2 text-[15px] font-normal text-fg-4">{emphasis}</span>
        ) : null}
      </h1>
      {actions ? <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div> : null}
    </div>
  );
}

export default PageHeader;
