"use client";

import * as React from "react";

import { IconX } from "@/components/icons";
import { cn } from "@/lib/utils";

/**
 * 轻量模态框（不引组件库）：遮罩 + 浮层卡 + Esc 关闭 + 焦点回收。
 * 改期弹窗、确认弹窗、复盘全文都用它。
 *
 * P13：标题从衬线换 sans/medium，遮罩从暖褐半透明换成中性黑，
 * 卡面走 `sw-pop`（同一张面 + 更重投影），不再是磨砂玻璃。
 * P14：遮罩改回暖炭半透（`--sw-scrim`，经 tailwind `bg-scrim` 语义色名引用），
 * 呼应 Organic 的暖色调；不再是字面量黑。
 */
export interface ModalProps {
  open: boolean;
  onClose: () => void;
  title: React.ReactNode;
  description?: React.ReactNode;
  footer?: React.ReactNode;
  children?: React.ReactNode;
  className?: string;
}

export function Modal({ open, onClose, title, description, footer, children, className }: ModalProps) {
  const ref = React.useRef<HTMLDivElement>(null);
  // 让 role=dialog 有个**可访问名**（= 标题）。没有它，屏幕阅读器只念得出"对话框"，
  // 测试里也没法按名字取到某一个弹窗
  const titleId = React.useId();

  React.useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    ref.current?.focus();
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-[80] flex items-center justify-center p-4">
      <button
        type="button"
        aria-label="关闭"
        onClick={onClose}
        className="absolute inset-0 cursor-default bg-scrim"
      />
      <div
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className={cn(
          "sw-pop relative z-10 flex w-full max-w-lg animate-pop-in flex-col rounded-card outline-none",
          className,
        )}
      >
        <div className="flex items-start justify-between gap-3 border-b border-line px-5 py-3">
          <div className="min-w-0">
            <h3 id={titleId} className="text-[15px] font-semibold leading-tight text-fg">
              {title}
            </h3>
            {description ? (
              <p className="mt-1 text-[11.5px] leading-relaxed text-fg-3">{description}</p>
            ) : null}
          </div>
          <button
            type="button"
            aria-label="关闭弹窗"
            onClick={onClose}
            className="shrink-0 rounded-pill p-1 text-fg-4 transition-colors duration-150 hover:bg-muted hover:text-fg-2"
          >
            <IconX size={15} />
          </button>
        </div>
        <div className="sw-scroll max-h-[65vh] overflow-y-auto px-5 py-4">{children}</div>
        {footer ? (
          <div className="flex items-center justify-end gap-2 border-t border-line px-5 py-3">
            {footer}
          </div>
        ) : null}
      </div>
    </div>
  );
}

export default Modal;
