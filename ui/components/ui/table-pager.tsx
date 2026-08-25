"use client";

import * as React from "react";

import { IconChevronLeft, IconChevronRight } from "@/components/icons";
import { cn } from "@/lib/utils";

/**
 * 列表页底部常驻分页（参照 dormice `components/TablePager.tsx`）。
 *
 * 左「共 N 条，第 x / y 页」右「上一页 / 页码窗 / 下一页」，**一页也显示**：
 * 分页条时有时无，人就得先确认它在不在，再去找它在哪。常驻的一条读数反而
 * 是最省事的——它顺带回答了"总共多少条"。
 *
 * 纯前端切片：列表本来就整份在手（后端一次给 200 条），不为翻页再打一次网络。
 */

/** 一页多少条。表格 fill 形态下 20 行刚好把 1440×960 的框铺满。 */
export const PAGE_SIZE = 20;

/**
 * 切出第 page 页（1 起）。page 越界时夹回合法区间，**不报错也不留空表**：
 * 筛选一变总数就缩水，停在第 7 页却只剩 2 页是正常操作序列，不是异常。
 */
export function paginate<T>(
  items: T[],
  page: number,
  size = PAGE_SIZE,
): { rows: T[]; page: number; pages: number; total: number } {
  const total = items.length;
  const pages = Math.max(1, Math.ceil(total / size));
  const safe = Math.min(Math.max(1, Math.floor(page) || 1), pages);
  return { rows: items.slice((safe - 1) * size, safe * size), page: safe, pages, total };
}

/**
 * 居中的页码窗，最多 `span` 个。
 * 当前页尽量居中，撞到两端时整窗贴边（否则末页附近窗口会缩成一两个格子）。
 */
export function pageWindow(page: number, pages: number, span = 5): number[] {
  const size = Math.min(span, pages);
  let start = Math.max(1, page - Math.floor(size / 2));
  if (start + size - 1 > pages) start = pages - size + 1;
  return Array.from({ length: size }, (_, i) => start + i);
}

export function TablePager({
  page,
  pages,
  total,
  onPage,
  className,
}: {
  page: number;
  pages: number;
  total: number;
  onPage: (next: number) => void;
  className?: string;
}) {
  const win = pageWindow(page, pages);
  return (
    <nav
      aria-label="分页"
      data-testid="table-pager"
      className={cn("flex flex-wrap items-center justify-between gap-2 px-4 py-2.5", className)}
    >
      <span className="sw-num text-[11.5px] text-fg-4">
        共 {total} 条，第 {page} / {pages} 页
      </span>
      <div className="flex items-center gap-1">
        <PagerButton
          label="上一页"
          disabled={page <= 1}
          onClick={() => onPage(page - 1)}
        >
          <IconChevronLeft size={13} />
        </PagerButton>
        {win.map((n) => (
          <PagerButton
            key={n}
            label={`第 ${n} 页`}
            active={n === page}
            onClick={() => onPage(n)}
          >
            <span className="sw-num">{n}</span>
          </PagerButton>
        ))}
        <PagerButton
          label="下一页"
          disabled={page >= pages}
          onClick={() => onPage(page + 1)}
        >
          <IconChevronRight size={13} />
        </PagerButton>
      </div>
    </nav>
  );
}

function PagerButton({
  label,
  active,
  disabled,
  onClick,
  children,
}: {
  label: string;
  active?: boolean;
  disabled?: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      aria-current={active ? "page" : undefined}
      disabled={disabled}
      onClick={onClick}
      className={cn(
        // 页码钮药丸化；当前页与其余选中态同一句话（陶土 tint + 深阶字）
        "flex h-7 min-w-[1.75rem] items-center justify-center rounded-pill px-1.5 text-[12px]",
        "transition-colors duration-150 disabled:cursor-not-allowed disabled:opacity-40",
        active
          ? "bg-primary-soft font-medium text-primary-deep"
          : "text-fg-3 hover:bg-primary-soft hover:text-primary-deep",
      )}
    >
      {children}
    </button>
  );
}

export default TablePager;
