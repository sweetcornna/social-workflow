"use client";

import * as React from "react";

import { IconInbox } from "@/components/icons";
import { DataState } from "@/components/layout/data-state";
import { CoverThumb } from "@/components/review/thumbs";
import { Badge } from "@/components/ui/badge";
import { LiveDot } from "@/components/ui/live-dot";
import { fmtSince, PLATFORM_LABEL, STATUS_LABEL, type Tone } from "@/lib/format";
import type { ContentRow } from "@/lib/types";
import { cn } from "@/lib/utils";

/** 机审色点：阻断 = 红，仅警告 = 黄，通过 = 绿，没跑过 = 灰。 */
export function machineTone(item: ContentRow): Tone {
  const mr = item.machine_review;
  if (!mr) return "muted";
  if (mr.blocking > 0) return "err";
  if (mr.warnings > 0) return "warn";
  return "ok";
}

function machineLabel(item: ContentRow): string {
  const mr = item.machine_review;
  if (!mr) return "没跑过机器审核";
  return `机器审核 阻断 ${mr.blocking} 警告 ${mr.warnings}`;
}

/**
 * 左侧窄队列。
 *
 * 一行只回答四件事：长什么样（封面）、是什么（标题 + 平台）、机器怎么看（色点）、
 * 等了多久。宽度压到 236px，把屏幕让给中间的媒体舞台。
 */
export function QueueRail({
  items,
  currentId,
  onSelect,
  total,
  isLoading,
  error,
  onRetry,
  toolbar,
}: {
  items: ContentRow[];
  currentId: string;
  onSelect: (id: string) => void;
  total: number;
  isLoading: boolean;
  error?: unknown;
  onRetry: () => void;
  /** 筛选条。紧贴表头，不占滚动区。 */
  toolbar?: React.ReactNode;
}) {
  const activeRef = React.useRef<HTMLButtonElement>(null);

  // j/k 换条时把当前行滚进视野——不然翻到第 20 条人就看不见自己在哪了
  React.useEffect(() => {
    activeRef.current?.scrollIntoView({ block: "nearest" });
  }, [currentId]);

  return (
    <aside
      data-testid="queue-rail"
      className="sw-card flex h-full min-h-0 w-full flex-col overflow-hidden rounded-card"
    >
      <header className="flex shrink-0 items-baseline justify-between gap-2 border-b border-line px-3 py-2.5">
        <h2 className="text-[13.5px] font-medium text-fg">队列</h2>
        <span className="sw-num text-[11px] text-fg-4">{total} 条</span>
      </header>

      {toolbar ? (
        <div className="shrink-0 border-b border-line px-3 py-2">{toolbar}</div>
      ) : null}

      <div className="sw-scroll min-h-0 flex-1 overflow-y-auto">
        <DataState isLoading={isLoading} error={error} onRetry={onRetry} rows={6}>
          {items.length === 0 ? (
            // 主区已经有一整块"队列是空的"了，这里只留一句，不重复喊两遍
            <p className="px-3 py-6 text-center text-[11.5px] leading-relaxed text-fg-4">
              <IconInbox size={18} className="mx-auto mb-1.5 block" />
              这个筛选下没有内容
            </p>
          ) : (
            <ul>
              {items.map((it) => {
                const active = it.id === currentId;
                return (
                  <li key={it.id}>
                    <button
                      ref={active ? activeRef : undefined}
                      type="button"
                      onClick={() => onSelect(it.id)}
                      aria-current={active}
                      data-testid="queue-row"
                      data-item-id={it.id}
                      className={cn(
                        "flex w-full items-start gap-2.5 border-b border-line px-3 py-2.5 text-left transition-colors",
                        active
                          ? "bg-muted-hover shadow-[inset_2px_0_0_var(--sw-primary)]"
                          : "hover:bg-row-hover",
                      )}
                    >
                      <CoverThumb
                        src={it.cover_url}
                        kind={it.media.videos > 0 ? "video" : "image"}
                        className="h-10 w-10"
                      />
                      <span className="min-w-0 flex-1">
                        <span className="line-clamp-2 text-[12.5px] leading-snug text-fg">
                          {it.title || it.id}
                        </span>
                        <span className="mt-1 flex flex-wrap items-center gap-1">
                          <Badge tone="muted">{PLATFORM_LABEL[it.platform] ?? it.platform}</Badge>
                          {it.status !== "draft" ? (
                            <Badge tone={it.status === "rejected" ? "err" : "warn"}>
                              {STATUS_LABEL[it.status] ?? it.status}
                            </Badge>
                          ) : null}
                          {it.needs_watch ? <Badge tone="amber">需看完</Badge> : null}
                        </span>
                        <span className="mt-1 flex items-center gap-1.5">
                          <LiveDot tone={machineTone(it)} size={6} label={machineLabel(it)} />
                          <span className="sw-num text-[10px] text-fg-4">
                            等待 {fmtSince(it.created_at)}
                          </span>
                        </span>
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </DataState>
      </div>
    </aside>
  );
}

export default QueueRail;
