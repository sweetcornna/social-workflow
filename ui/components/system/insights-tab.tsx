"use client";

import * as React from "react";

import { IconBook, IconRefresh, IconSpark } from "@/components/icons";
import { DataState } from "@/components/layout/data-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { FilterMenu } from "@/components/ui/filter-menu";
import { Panel } from "@/components/ui/panel";
import { Markdown } from "@/components/ui/markdown";
import { SectionPanel } from "@/components/ui/section-panel";
import { useToast } from "@/components/ui/toast";
import { apiFetch, describeError } from "@/lib/api";
import { fmtTime, PLATFORM_LABEL } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import type { InsightsRow, TickResult } from "@/lib/types";

/**
 * 复盘 tab。
 * 没配 LLM 凭据时整体跳过，不会回落到假模型——宁可空着，也不要被预置假文本污染。
 */
export function InsightsTab() {
  const toast = useToast();
  const [tab, setTab] = React.useState("");
  const [running, setRunning] = React.useState(false);

  const { data, error, isLoading, mutate } = useApi<InsightsRow[]>("/insights");
  const rows = data ?? [];
  const current = rows.find((r) => r.account_id === tab) ?? rows[0];

  async function run(accountId?: string) {
    setRunning(true);
    try {
      const res = await apiFetch<TickResult>("/insights/run", {
        method: "POST",
        body: { account_id: accountId ?? null, force: true },
      });
      const stats = res.stats as Record<string, number>;
      toast.ok(
        res.message ?? `复盘跑完：扫描 ${stats.scanned ?? 0} 个账号，写入 ${stats.written ?? 0} 份`,
      );
      await mutate();
    } catch (e) {
      toast.err(describeError(e));
    } finally {
      setRunning(false);
    }
  }

  return (
    <DataState isLoading={isLoading} error={error} onRetry={() => void mutate()} rows={4}>
      {rows.length === 0 ? (
        <EmptyState
          icon={<IconBook />}
          title="没有可复盘的账号"
          description="账号台账是空的。先把 accounts.yaml 同步入库，复盘 Agent 才有对象。"
        />
      ) : (
        <>
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <FilterMenu
              label="账号"
              value={current?.account_id ?? ""}
              onChange={setTab}
              options={rows.map((r) => ({
                value: r.account_id,
                label: r.name,
                count: r.entries.length,
                tone: r.error ? ("err" as const) : undefined,
              }))}
            />
            <div className="flex items-center gap-2">
              <Button size="sm" onClick={() => void mutate()}>
                <IconRefresh size={12} />
                刷新
              </Button>
              <Button size="sm" variant="primary" onClick={() => void run()} loading={running}>
                <IconSpark size={12} />
                全部账号跑一次
              </Button>
            </div>
          </div>

          {current ? (
            <SectionPanel
              title={current.name}
              subtitle={`${PLATFORM_LABEL[current.platform] ?? current.platform} · ${
                current.entries.length
              } 篇`}
              actions={
                <>
                  {current.updated_at ? (
                    <Badge tone="muted">上次写盘 {fmtTime(current.updated_at)}</Badge>
                  ) : (
                    <Badge tone="warn">从没写过</Badge>
                  )}
                  <Button size="sm" onClick={() => void run(current.account_id)} loading={running}>
                    <IconSpark size={12} />
                    只跑这个账号
                  </Button>
                </>
              }
            >
              <div className="px-4 py-4">
                {current.error ? (
                  <div className="mb-3 rounded-md border-l-[3px] border-l-err bg-err-soft px-3 py-2.5 text-[12px] text-err">
                    上次复盘失败：{current.error}
                  </div>
                ) : null}

                {current.entries.length === 0 ? (
                  <EmptyState
                    className="my-6"
                    icon={<IconBook />}
                    title="这个账号还没有复盘结论"
                    description="点右上角「只跑这个账号」。每个账号内部有 24 小时节流，这里的按钮会带 force 跳过它。"
                  />
                ) : (
                  <div className="flex flex-col gap-3">
                    {current.entries.map((e, i) => (
                      <Panel key={`${e.date}-${i}`} variant="inset" className="px-4 py-3.5">
                        <div className="mb-1.5 flex flex-wrap items-baseline gap-2">
                          <span className="sw-num text-[12px] text-fg-3">{e.date}</span>
                          <span className="text-[15px] font-medium text-fg">{e.title}</span>
                        </div>
                        {e.headline ? (
                          <p className="mb-2 border-l-2 border-primary-line pl-2.5 text-[12.5px] text-fg-2">{e.headline}</p>
                        ) : null}
                        <Markdown source={e.markdown} />
                      </Panel>
                    ))}
                  </div>
                )}
              </div>
            </SectionPanel>
          ) : null}
        </>
      )}
    </DataState>
  );
}

export default InsightsTab;
