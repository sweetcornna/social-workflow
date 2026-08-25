"use client";

import * as React from "react";
import { createPortal } from "react-dom";

import { cn } from "@/lib/utils";

/**
 * 锚定浮层 —— 筛选菜单与行操作「⋯」菜单共用的唯一实现。
 *
 * 为什么要 portal + fixed 定位，而不是简单的 `absolute right-0`
 * ---------------------------------------------------------------
 * 表格是**框内滚动**的（DataTable 的 fill 形态），滚动口上有 overflow。
 * 菜单如果留在行内用 absolute，最后几行的菜单会被滚动口裁掉——这是"行操作
 * 默认不可见"的另一种形态，与列宽把操作列挤出去一样不可接受。
 * 所以浮层挂到 body，位置每次打开时按触发器的 viewport 矩形算。
 *
 * 随之而来的代价要老实处理：viewport 坐标在页面滚动后就过期了，所以
 * **滚动与改窗即关闭**（不做跟随重定位——那需要一个观察者，配不上这点收益）。
 */

export interface PopoverProps {
  open: boolean;
  onClose: () => void;
  /** 触发器元素。浮层按它的矩形定位。 */
  anchorRef: React.RefObject<HTMLElement | null>;
  /** 浮层对齐到触发器的哪一侧。 */
  align?: "start" | "end";
  /** 可访问名。role=menu 需要它，否则读屏只念得出"菜单"。 */
  label: string;
  className?: string;
  children: React.ReactNode;
}

export function Popover({
  open,
  onClose,
  anchorRef,
  align = "start",
  label,
  className,
  children,
}: PopoverProps) {
  const panelRef = React.useRef<HTMLDivElement>(null);
  const [rect, setRect] = React.useState<DOMRect | null>(null);
  // 静态导出下首帧没有 document，portal 要等挂载后才建
  const [mounted, setMounted] = React.useState(false);
  React.useEffect(() => setMounted(true), []);

  React.useEffect(() => {
    if (!open) return;
    setRect(anchorRef.current?.getBoundingClientRect() ?? null);

    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
        // 关闭后焦点回到触发器，键盘用户不会掉到文档开头
        anchorRef.current?.focus();
      }
    };
    const onPointer = (e: PointerEvent) => {
      const t = e.target as Node;
      if (panelRef.current?.contains(t) || anchorRef.current?.contains(t)) return;
      onClose();
    };
    // 捕获阶段收滚动：表格滚动口的 scroll 事件不冒泡到 window
    const onScroll = () => onClose();

    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onPointer);
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", onScroll);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onPointer);
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", onScroll);
    };
  }, [open, onClose, anchorRef]);

  // 打开后把焦点送进浮层，Tab 才不会跳回页面顶部
  React.useEffect(() => {
    if (!open) return;
    panelRef.current?.focus();
  }, [open, rect]);

  if (!mounted || !open || !rect) return null;

  // 贴着触发器下沿 4px；靠近视口底部时翻到上方，免得菜单被切掉
  const below = rect.bottom + 4;
  const flip = below > window.innerHeight - 180;
  const style: React.CSSProperties = {
    position: "fixed",
    top: flip ? undefined : below,
    bottom: flip ? window.innerHeight - rect.top + 4 : undefined,
    left: align === "start" ? rect.left : undefined,
    right: align === "end" ? window.innerWidth - rect.right : undefined,
  };

  return createPortal(
    <div
      ref={panelRef}
      role="menu"
      aria-label={label}
      tabIndex={-1}
      style={style}
      className={cn(
        "sw-pop z-[85] min-w-[10rem] animate-pop-in rounded-lg p-1 outline-none",
        className,
      )}
    >
      {children}
    </div>,
    document.body,
  );
}

/** 菜单项。图标 + 文案，`destructive` 走红字。 */
export function MenuItem({
  onSelect,
  icon,
  destructive,
  disabled,
  children,
}: {
  onSelect: () => void;
  icon?: React.ReactNode;
  destructive?: boolean;
  disabled?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      role="menuitem"
      disabled={disabled}
      onClick={onSelect}
      className={cn(
        "flex w-full items-center gap-2 whitespace-nowrap rounded-pill px-2.5 py-1.5 text-left text-[12.5px]",
        "transition-colors duration-150 disabled:cursor-not-allowed disabled:opacity-45",
        destructive ? "text-err hover:bg-err-soft" : "text-fg-2 hover:bg-muted hover:text-fg",
      )}
    >
      {icon ? <span className="shrink-0 [&_svg]:h-3.5 [&_svg]:w-3.5">{icon}</span> : null}
      {children}
    </button>
  );
}

export default Popover;
