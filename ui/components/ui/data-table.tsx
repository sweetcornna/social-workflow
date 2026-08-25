"use client";

import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * 全站表皮的**唯一定义处**（P13，参照 dormice `components/DataTable.tsx`）。
 *
 * 在这一个文件里定死的东西，别处一律不许再写第二遍：
 *  - 卡面容器（.sw-card：填色分界 + 28px 圆角，P14.B2 起不描边）
 *  - 表头 h-12、单元格 px-4 py-2.5、首末列 pl-6/pr-6
 *  - 吸顶表头走 **muted 实底**（P14.B2 从"与卡面同底色"改过来：填色分界之后
 *    表头需要自己的一档底色才分得出"这行是表头"；关键是它必须**不透明**，
 *    半透明表头在滚动时会透出行文字）
 *  - `[&_tr]:transition-none`：本工作台 2~5 秒轮询重渲，行上留着 transition
 *    会让整表在每次刷新时集体微闪
 *
 * 两种形态
 * --------
 *  - 默认：随内容长高，用在面板里的短表
 *  - `fill`：吃掉父 flex 列的全部剩余高度，行多框内滚、行少留白。
 *    要求父容器是 `h-full flex flex-col`（外壳已经锁死视口高，见 (shell)/layout）
 *
 * **横向滚动只有一个口**：滚动 div 同时承担横滚与纵滚，吸顶表头才不会在
 * 横滚时与内容错位。列宽紧张时的正解是压窄列、truncate 内层 span，
 * 不是让表格横滚——横滚常驻等于行操作默认不可见。
 */

export interface Column<T> {
  key: string;
  header: React.ReactNode;
  /** 数值列右对齐 + tabular-nums（钱与量右对齐，时长/时刻照旧左对齐 muted）。 */
  align?: "left" | "right";
  /** 单元格类名，列宽约束（max-w / w-）写在这里。 */
  className?: string;
  /** 表头独有的类名。默认继承对齐方式。 */
  headClassName?: string;
  cell: (row: T) => React.ReactNode;
}

export interface DataTableProps<T> {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T) => string;
  /** 行上的额外属性（data-testid / data-item-id / 高亮态）。 */
  rowProps?: (row: T) => React.HTMLAttributes<HTMLTableRowElement> & Record<string, unknown>;
  /** 吃满父容器剩余高度、行在框内滚。列表页用它，分页条才钉得住底。 */
  fill?: boolean;
  /** 面板标题。给了就渲染 h2 —— 表格也是面板，标题栏与 SectionPanel 同一形状。 */
  title?: React.ReactNode;
  /** 标题下的一行读数（「共 42 条」）。只放数与状态，不放口径解释。 */
  subtitle?: React.ReactNode;
  /** 标题栏右侧的工具栏（筛选、刷新）。无标题时它占满整行。 */
  toolbar?: React.ReactNode;
  /** 表格下方常驻区（TablePager）。空表时不渲染。 */
  footer?: React.ReactNode;
  /** 零行时的替代内容。 */
  empty?: React.ReactNode;
  /** 表格最小宽度。列多的表传它，其余不传——不传才不会平白造出横滚。 */
  minWidth?: number;
  className?: string;
}

export function DataTable<T>({
  columns,
  rows,
  rowKey,
  rowProps,
  fill,
  title,
  subtitle,
  toolbar,
  footer,
  empty,
  minWidth,
  className,
}: DataTableProps<T>) {
  const isEmpty = rows.length === 0;

  return (
    <div
      data-slot="data-table"
      className={cn(
        "sw-card flex min-h-0 flex-col overflow-hidden rounded-card",
        fill && "h-full flex-1",
        className,
      )}
    >
      {title || toolbar ? (
        <div className="flex min-h-[3rem] shrink-0 flex-wrap items-center justify-between gap-2 border-b border-line px-4 py-2.5">
          {title ? (
            <div className="min-w-0">
              <h2 className="truncate text-[13.5px] font-semibold text-fg">{title}</h2>
              {subtitle ? (
                <p className="sw-num mt-0.5 truncate text-[11px] text-fg-4">{subtitle}</p>
              ) : null}
            </div>
          ) : null}
          {toolbar ? (
            <div
              className={cn(
                "flex min-w-0 flex-wrap items-center gap-2",
                title ? "shrink-0" : "flex-1",
              )}
            >
              {toolbar}
            </div>
          ) : null}
        </div>
      ) : null}

      {isEmpty ? (
        // fill 形态用 flex-1 占位（三态等高不跳）；随内容长高的形态不撑，
        // 否则一张"这里是空的"的卡会比有内容时还高
        <div
          className={cn(
            "flex min-h-0 items-center justify-center p-4",
            fill && "flex-1",
          )}
        >
          {empty}
        </div>
      ) : (
        <div className={cn("sw-scroll min-h-0 overflow-auto", fill ? "flex-1" : "max-h-[70vh]")}>
          <table
            className="w-full border-collapse text-left text-[12.5px] [&_tr]:transition-none"
            style={minWidth ? { minWidth } : undefined}
          >
            <thead>
              <tr className="border-b border-line">
                {columns.map((c, i) => (
                  <th
                    key={c.key}
                    scope="col"
                    className={cn(
                      // 吸顶表头走 muted 实底；z-10 压住滚上来的行
                      "sticky top-0 z-10 h-12 bg-muted px-4 align-middle",
                      // 字色用 fg-3 而不是 fg-4：muted 是全站最亮的一档底，
                      // 暗色下 fg-4 落在它上面实算只有 4.04:1（B6 走查实测），
                      // 过不了 AA。改档位比改 muted 便宜——muted 一动，它与
                      // popover 的距离会从 ΔE 6 压到 3，撞回 B1 注释里躲开的那个坑。
                      "text-[11.5px] font-normal text-fg-3",
                      i === 0 && "pl-6",
                      i === columns.length - 1 && "pr-6",
                      c.align === "right" && "text-right",
                      c.headClassName ?? c.className,
                    )}
                  >
                    {/* 表头文字不许换行：列一窄就先劈表头，看着像坏了 */}
                    <span className="whitespace-nowrap">{c.header}</span>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const extra = rowProps?.(row) ?? {};
                const { className: rowClass, ...restProps } = extra as {
                  className?: string;
                } & Record<string, unknown>;
                return (
                  <tr
                    key={rowKey(row)}
                    className={cn(
                      "border-b border-line last:border-0 hover:bg-row-hover",
                      rowClass,
                    )}
                    {...restProps}
                  >
                    {columns.map((c, i) => (
                      <td
                        key={c.key}
                        className={cn(
                          // 默认 nowrap：单元格自己换行会让行高在轮询重渲时忽高忽低。
                          // 要吸收剩余宽度的那一列传 `w-full max-w-0`，内层 span truncate。
                          "whitespace-nowrap px-4 py-2.5 align-middle text-fg-2",
                          i === 0 && "pl-6",
                          i === columns.length - 1 && "pr-6",
                          c.align === "right" && "text-right tabular-nums",
                          c.className,
                        )}
                      >
                        {c.cell(row)}
                      </td>
                    ))}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {footer && !isEmpty ? (
        <div className="shrink-0 border-t border-line">{footer}</div>
      ) : null}
    </div>
  );
}

/**
 * 单元格里的长文本。
 *
 * truncate 必须放**内层 span**：外层 td 是 flex/表格布局的一部分，直接在它上面
 * 截会两侧裁字（dormice 在 Badge 上踩过同一个坑）。
 */
export function Ellipsis({
  children,
  title,
  className,
}: {
  children: React.ReactNode;
  title?: string;
  className?: string;
}) {
  return (
    <span className={cn("block min-w-0 truncate", className)} title={title}>
      {children}
    </span>
  );
}

export default DataTable;
