"use client";

import { useRouter } from "next/navigation";
import * as React from "react";

import { IconArrowRight, IconSearch } from "@/components/icons";
import { Badge } from "@/components/ui/badge";
import { apiFetch } from "@/lib/api";
import { PLATFORM_LABEL, STATUS_LABEL } from "@/lib/format";
import { NAV, SUB_NAV } from "@/lib/nav";
import type { AccountRow, ContentRow, Page } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * ⌘K 命令面板。
 * 结构参考 corlinman（MIT）`ui/components/ui/command-palette.tsx` @50ae94a4，
 * 改造：去掉 cmdk 依赖自己写键盘导航；结果源换成本项目的"跳页 + 按 id/标题
 * 搜内容 + 搜账号"，打开时才拉数据（不进轮询）。
 */

/** 还在人工卡点上的状态——只有它们能在审核台里打开。 */
const REVIEWABLE = new Set(["draft", "reviewing", "rejected"]);

interface Row {
  key: string;
  group: string;
  title: string;
  hint: string;
  href: string;
  badge?: string;
}

export function CommandPalette({ open, onClose }: { open: boolean; onClose: () => void }) {
  const router = useRouter();
  const [query, setQuery] = React.useState("");
  const [cursor, setCursor] = React.useState(0);
  const [content, setContent] = React.useState<ContentRow[]>([]);
  const [accounts, setAccounts] = React.useState<AccountRow[]>([]);
  const inputRef = React.useRef<HTMLInputElement>(null);

  React.useEffect(() => {
    if (!open) return;
    setQuery("");
    setCursor(0);
    inputRef.current?.focus();
    let cancelled = false;
    // 面板打开才拉一次；两个列表都不大，客户端过滤足够
    void Promise.allSettled([
      apiFetch<Page<ContentRow>>("/content", { query: { limit: 200 } }),
      apiFetch<AccountRow[]>("/accounts"),
    ]).then(([c, a]) => {
      if (cancelled) return;
      if (c.status === "fulfilled") setContent(c.value.items);
      if (a.status === "fulfilled") setAccounts(a.value);
    });
    return () => {
      cancelled = true;
    };
  }, [open]);

  const rows = React.useMemo<Row[]>(() => {
    const q = query.trim().toLowerCase();
    const nav: Row[] = NAV.map((n) => ({
      key: `nav:${n.href}`,
      group: "页面",
      title: n.label,
      hint: n.hint,
      href: n.href,
    }));
    // 统计 / 复盘 / 死信收进了系统页的 tab，⌘K 必须还能直达，否则就是"东西找不着了"
    const subNav: Row[] = SUB_NAV.map((n) => ({
      key: `sub:${n.href}`,
      group: "系统 tab",
      title: n.label,
      hint: n.hint,
      href: n.href,
    }));
    const accountRows: Row[] = accounts.map((a) => ({
      key: `acc:${a.id}`,
      group: "账号",
      title: a.name,
      hint: a.id,
      href: `/accounts/?id=${encodeURIComponent(a.id)}`,
      badge: PLATFORM_LABEL[a.platform] ?? a.platform,
    }));
    const contentRows: Row[] = content.map((c) => ({
      key: `itm:${c.id}`,
      group: "内容",
      title: c.title || c.id,
      hint: `${c.id} · ${STATUS_LABEL[c.status] ?? c.status}`,
      // 还在人工卡点上的去审核台，其余的去排期页定位——别把已发布的稿子
      // 送进一个根本不收它的队列
      href: REVIEWABLE.has(c.status)
        ? `/review/?id=${encodeURIComponent(c.id)}`
        : `/schedule/?id=${encodeURIComponent(c.id)}`,
      badge: PLATFORM_LABEL[c.platform] ?? c.platform,
    }));
    const all = [...nav, ...subNav, ...accountRows, ...contentRows];
    if (!q) return all.slice(0, 24);
    return all
      .filter((r) => `${r.title} ${r.hint}`.toLowerCase().includes(q))
      .slice(0, 24);
  }, [query, content, accounts]);

  const go = React.useCallback(
    (row: Row | undefined) => {
      if (!row) return;
      onClose();
      router.push(row.href);
    },
    [onClose, router],
  );

  React.useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose();
        return;
      }
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setCursor((c) => Math.min(rows.length - 1, c + 1));
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setCursor((c) => Math.max(0, c - 1));
      } else if (e.key === "Enter") {
        e.preventDefault();
        go(rows[cursor]);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, rows, cursor, go, onClose]);

  if (!open) return null;

  let lastGroup = "";

  return (
    <div className="fixed inset-0 z-[95] flex items-start justify-center px-4 pt-[12vh]">
      <button
        type="button"
        aria-label="关闭命令面板"
        onClick={onClose}
        className="absolute inset-0 cursor-default bg-scrim"
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-label="命令面板"
        className="sw-pop relative z-10 w-full max-w-xl animate-pop-in overflow-hidden rounded-card"
      >
        <div className="flex items-center gap-2.5 border-b border-line px-4 py-3">
          <IconSearch size={15} className="shrink-0 text-fg-4" />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setCursor(0);
            }}
            placeholder="跳转页面，或按 id / 标题搜内容与账号"
            aria-label="搜索"
            className="h-6 w-full bg-transparent text-[14px] text-fg outline-none placeholder:text-fg-4"
          />
          <kbd className="sw-num shrink-0 rounded-pill bg-muted px-1.5 py-0.5 text-[10px] text-fg-3">
            ESC
          </kbd>
        </div>

        <div className="sw-scroll max-h-[52vh] overflow-y-auto py-1.5">
          {rows.length === 0 ? (
            <p className="px-4 py-8 text-center text-[12.5px] text-fg-3">
              没有匹配项。换个关键词，或直接输入内容 id。
            </p>
          ) : (
            rows.map((row, i) => {
              const showGroup = row.group !== lastGroup;
              lastGroup = row.group;
              return (
                <React.Fragment key={row.key}>
                  {showGroup ? <div className="sw-label px-4 pb-1 pt-2.5">{row.group}</div> : null}
                  <button
                    type="button"
                    onMouseEnter={() => setCursor(i)}
                    onClick={() => go(row)}
                    className={cn(
                      "flex w-full items-center gap-3 px-4 py-2 text-left transition-colors",
                      i === cursor ? "bg-muted-hover" : "hover:bg-muted",
                    )}
                  >
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-[13px] text-fg">{row.title}</span>
                      <span className="sw-num block truncate text-[11px] text-fg-4">
                        {row.hint}
                      </span>
                    </span>
                    {row.badge ? <Badge tone="muted">{row.badge}</Badge> : null}
                    <IconArrowRight
                      size={13}
                      className={cn("shrink-0", i === cursor ? "text-primary" : "text-fg-5")}
                    />
                  </button>
                </React.Fragment>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
}

export default CommandPalette;
