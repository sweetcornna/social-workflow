"use client";

import * as React from "react";

import { IconChevronDown, IconExternal, IconFlame, IconTrend, IconX } from "@/components/icons";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { Panel } from "@/components/ui/panel";
import { SkeletonRows } from "@/components/ui/skeleton";
import { useToast } from "@/components/ui/toast";
import { apiFetch, describeError } from "@/lib/api";
import { fmtTime } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import type { Page as PageT, TopicRow } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * 选题池折叠区。
 *
 * 选题不再是一级页——它是"今天写什么"的输入，不是每天必须处理的待办，
 * 所以收在今日页底部，默认折起、展开才拉数据（不进轮询）。
 */
export function TopicPool() {
  const toast = useToast();
  const [open, setOpen] = React.useState(false);

  const { data, error, isLoading, mutate } = useApi<PageT<TopicRow>>(
    open ? "/topics" : null,
    { used: "false", limit: 30 },
  );
  const items = data?.items ?? [];

  async function dismiss(topic: TopicRow) {
    try {
      await apiFetch<TopicRow>(`/topics/${topic.id}/dismiss`, {
        method: "POST",
        body: { actor: "operator", reason: "工作台手动弃用", dismissed: !topic.dismissed },
      });
      toast.ok(topic.dismissed ? "已恢复这条选题" : "已标记弃用（只影响工作台展示）");
      await mutate();
    } catch (e) {
      toast.err(describeError(e));
    }
  }

  return (
    <Panel className="overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        data-testid="topic-pool-toggle"
        className="flex w-full items-center gap-2.5 px-4 py-3 text-left transition-colors hover:bg-muted"
      >
        <IconFlame size={15} className="shrink-0 text-fg-3" />
        <span className="min-w-0 flex-1">
          <span className="block text-[14px] font-medium text-fg">选题池</span>
          <span className="block truncate text-[11.5px] text-fg-3">
            热榜来源与热度。弃用标记只影响这里的展示，选题 Agent 还不读它。
          </span>
        </span>
        {open && data ? <Badge tone="muted">{data.total} 条未用</Badge> : null}
        <IconChevronDown
          size={14}
          className={cn("shrink-0 text-fg-4 transition-transform", open && "rotate-180")}
        />
      </button>

      {open ? (
        <div className="border-t border-line">
          {error ? (
            <p className="px-4 py-6 text-center text-[12.5px] text-err">{describeError(error)}</p>
          ) : isLoading ? (
            <SkeletonRows rows={4} />
          ) : items.length === 0 ? (
            <EmptyState
              className="my-6"
              icon={<IconFlame />}
              title="选题池为空"
              description="到「系统 · 自检与任务」运行一次 sourcing，或检查 TrendRadar sidecar 连接。"
            />
          ) : (
            <ul className="sw-scroll max-h-[320px] divide-y divide-line overflow-y-auto">
              {items.map((t) => (
                <li
                  key={t.id}
                  className="flex items-start gap-3 px-4 py-2.5 transition-colors hover:bg-row-hover"
                  data-dismissed={t.dismissed || undefined}
                >
                  <span className="flex w-10 shrink-0 flex-col items-center pt-0.5">
                    <IconTrend size={13} className="text-primary" />
                    <span className="sw-num mt-0.5 text-[12px] text-fg">
                      {t.score.toFixed(1)}
                    </span>
                  </span>
                  <div className="min-w-0 flex-1">
                    <div
                      className={cn(
                        "truncate text-[13px]",
                        t.dismissed ? "text-fg-4 line-through" : "text-fg",
                      )}
                    >
                      {t.title}
                    </div>
                    <div className="mt-0.5 flex flex-wrap items-center gap-1.5">
                      <Badge tone="muted">{t.source}</Badge>
                      {t.dismissed ? <Badge tone="err">已弃用</Badge> : null}
                      <span className="sw-num text-[10.5px] text-fg-4">
                        {fmtTime(t.created_at)}
                      </span>
                    </div>
                  </div>
                  <div className="flex shrink-0 items-center gap-1.5">
                    {t.url ? (
                      <a
                        href={t.url}
                        target="_blank"
                        rel="noreferrer"
                        aria-label="打开原文"
                        className="p-1 text-fg-4 transition-colors hover:text-primary"
                      >
                        <IconExternal size={13} />
                      </a>
                    ) : null}
                    <Button size="sm" onClick={() => void dismiss(t)}>
                      {t.dismissed ? (
                        "恢复"
                      ) : (
                        <>
                          <IconX size={11} />
                          弃用
                        </>
                      )}
                    </Button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : null}
    </Panel>
  );
}

export default TopicPool;
