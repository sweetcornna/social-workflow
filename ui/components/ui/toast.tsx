"use client";

import * as React from "react";

import { IconAlert, IconCheck, IconX } from "@/components/icons";
import { cn } from "@/lib/utils";

export type ToastTone = "ok" | "err" | "warn";

interface ToastItem {
  id: number;
  tone: ToastTone;
  message: string;
}

interface ToastApi {
  push: (message: string, tone?: ToastTone) => void;
  ok: (message: string) => void;
  err: (message: string) => void;
  warn: (message: string) => void;
}

const ToastContext = React.createContext<ToastApi | null>(null);

let seq = 0;

/** 右下角吐司。API 层的错误统一从这里冒出来，页面不要自己 alert。 */
export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [items, setItems] = React.useState<ToastItem[]>([]);

  const push = React.useCallback((message: string, tone: ToastTone = "ok") => {
    const id = ++seq;
    setItems((prev) => [...prev, { id, tone, message }]);
    window.setTimeout(() => {
      setItems((prev) => prev.filter((t) => t.id !== id));
    }, 5200);
  }, []);

  const api = React.useMemo<ToastApi>(
    () => ({
      push,
      ok: (m: string) => push(m, "ok"),
      err: (m: string) => push(m, "err"),
      warn: (m: string) => push(m, "warn"),
    }),
    [push],
  );

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div
        className="pointer-events-none fixed bottom-5 right-5 z-[90] flex w-[min(380px,calc(100vw-2.5rem))] flex-col gap-2"
        role="status"
        aria-live="polite"
      >
        {items.map((t) => (
          <div
            key={t.id}
            className={cn(
              // 语气不靠描边表达：色脊 + 色图标就够，一条 tone 色的细线在 16px
              // 圆角上只会把吐司描成贴纸（P14.B2 全站统一的告示形）
              "pointer-events-auto flex items-start gap-2.5 rounded-lg px-3.5 py-3 text-[13px] animate-fade-in",
              "sw-pop border-l-[3px]",
              t.tone === "ok" && "border-l-ok",
              t.tone === "err" && "border-l-err",
              t.tone === "warn" && "border-l-warn",
            )}
          >
            <span
              className={cn(
                "mt-[1px] shrink-0",
                t.tone === "ok" && "text-ok",
                t.tone === "warn" && "text-warn",
                t.tone === "err" && "text-err",
              )}
            >
              {t.tone === "ok" ? <IconCheck size={15} /> : <IconAlert size={15} />}
            </span>
            <span className="flex-1 leading-relaxed text-fg-2">{t.message}</span>
            <button
              type="button"
              aria-label="关闭提示"
              className="shrink-0 rounded p-0.5 text-fg-4 transition-colors hover:text-fg-2"
              onClick={() => setItems((prev) => prev.filter((x) => x.id !== t.id))}
            >
              <IconX size={13} />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastApi {
  const ctx = React.useContext(ToastContext);
  if (!ctx) throw new Error("useToast 必须在 <ToastProvider> 内使用");
  return ctx;
}
