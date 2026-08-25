"use client";

import * as React from "react";

import { IconMore } from "@/components/icons";
import { Button } from "@/components/ui/button";
import { Popover } from "@/components/ui/popover";

/**
 * 行操作的「⋯」菜单。
 *
 * 为什么行上不再摆一排常驻小按钮
 * ------------------------------
 * 排期页原来一行最多能长出五个按钮（确认发布 / 不发 / 改期 / 重投 / 详情），
 * 它们把行右侧撑成一片按钮墙：列一窄就换行，行高忽高忽低；而且五个按钮
 * 视觉权重相同，人得逐个读完才知道该点哪个。
 *
 * 新规矩：**一个高频主操作直出，其余收进「⋯」**。哪个是主操作由行的状态
 * 决定（等确认的行是「确认发布」，失败的行是「重投」），调用方自己传。
 *
 * 踩过的坑（dormice 实锤，这里照着避）：**弹窗必须挂在菜单外受控**。
 * 菜单一关就整个卸载，把 `<Modal>` 写在菜单项里会跟着菜单一起消失。
 * 所以本组件只负责"点了哪一项"，改期弹窗一律由页面在菜单外渲染。
 */
export function RowMenu({
  label = "更多操作",
  children,
}: {
  label?: string;
  /** 菜单项。用 `MenuItem`；点完要关菜单的自己调 `onSelect` 里的 close。 */
  children: (close: () => void) => React.ReactNode;
}) {
  const [open, setOpen] = React.useState(false);
  const anchor = React.useRef<HTMLButtonElement>(null);
  const close = React.useCallback(() => setOpen(false), []);

  return (
    <>
      <Button
        ref={anchor}
        size="icon"
        variant="ghost"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={label}
        data-testid="row-menu"
        onClick={() => setOpen((v) => !v)}
      >
        <IconMore size={15} />
      </Button>
      <Popover open={open} onClose={close} anchorRef={anchor} align="end" label={label}>
        {children(close)}
      </Popover>
    </>
  );
}

export default RowMenu;
