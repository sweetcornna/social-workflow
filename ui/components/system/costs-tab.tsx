"use client";

import * as React from "react";

import { IconRefresh, IconWallet } from "@/components/icons";
import { CostLine } from "@/components/charts/series-chart";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DataTable, Ellipsis, type Column } from "@/components/ui/data-table";
import { EmptyState } from "@/components/ui/empty-state";
import { BudgetRing } from "@/components/ui/progress";
import { SectionPanel } from "@/components/ui/section-panel";
import { SegmentedControl } from "@/components/ui/segmented";
import { SkeletonTable } from "@/components/ui/skeleton";
import { describeError } from "@/lib/api";
import { fmtCompact, fmtCost, PLATFORM_LABEL } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import type { Costs, CostsAccount } from "@/lib/types";

const DAY_CHIPS = [
  { value: "7", label: "7 天" },
  { value: "14", label: "14 天" },
  { value: "30", label: "30 天" },
];

/**
 * 成本 tab。
 * 单位是 token 数与渲染秒数，**不是钱**（没有汇率表），所以这一页不出现任何货币符号。
 */
export function CostsTab() {
  const [days, setDays] = React.useState("14");
  const costs = useApi<Costs>("/costs", { days });

  const byDay = costs.data?.by_day ?? [];
  const labels = byDay.map((d) => d.day);
  const tokenSeries = byDay.map((d) => d.cost.tokens ?? 0);
  const renderSeries = byDay.map((d) => d.cost.render_seconds ?? 0);
  const hasAny = tokenSeries.some((v) => v > 0) || renderSeries.some((v) => v > 0);

  const columns: Column<CostsAccount>[] = [
    {
      key: "name",
      header: "账号",
      className: "w-full max-w-0",
      cell: (a) => (
        <span className="flex min-w-0 items-center gap-2">
          <Ellipsis className="text-fg" title={a.name}>
            {a.name}
          </Ellipsis>
          <Badge tone="muted">{PLATFORM_LABEL[a.platform] ?? a.platform}</Badge>
        </span>
      ),
    },
    {
      key: "id",
      header: "标识",
      className: "max-w-[10rem]",
      cell: (a) => (
        <Ellipsis className="sw-num text-[11.5px] text-fg-4" title={a.account_id}>
          {a.account_id}
        </Ellipsis>
      ),
    },
    {
      key: "cost",
      header: "用量",
      align: "right",
      cell: (a) => <span className="sw-num text-[12px] text-fg-2">{fmtCost(a.cost)}</span>,
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <SegmentedControl label="窗口" value={days} onChange={setDays} options={DAY_CHIPS} />
        <Button size="sm" onClick={() => void costs.mutate()}>
          <IconRefresh size={12} />
          刷新
        </Button>
      </div>

      <SectionPanel
        title="成本曲线"
        subtitle={`最近 ${days} 天 · 单位是量不是金额`}
        actions={<IconWallet size={14} className="text-fg-4" />}
      >
        {costs.error ? (
          <div className="p-6">
            <EmptyState title="没能取到成本" description={describeError(costs.error)} />
          </div>
        ) : byDay.length === 0 ? (
          <div className="p-6">
            <EmptyState
              icon={<IconWallet />}
              title="窗口内没有成本记录"
              description="成本按 token 数与渲染秒数计量，跑过一次生成或渲染后这里才有点。"
            />
          </div>
        ) : (
          <div className="grid gap-4 px-4 py-4 sm:grid-cols-[1fr_auto]">
            <div className="flex flex-col gap-4">
              <div>
                <div className="sw-label mb-1">tokens</div>
                <CostLine
                  labels={labels}
                  values={tokenSeries}
                  budget={costs.data?.budget.tokens.limit}
                  height={120}
                  unit="tokens"
                />
              </div>
              <div>
                <div className="sw-label mb-1">渲染秒</div>
                <CostLine
                  labels={labels}
                  values={renderSeries}
                  budget={costs.data?.budget.render_seconds.limit}
                  height={120}
                  unit="秒"
                />
              </div>
              {!hasAny ? (
                <p className="text-[11.5px] text-fg-4">
                  这个窗口里还没有任何计量记录——曲线贴地是真实的 0，不是没取到数。
                </p>
              ) : null}
            </div>
            <div className="flex flex-row items-center justify-around gap-3 sm:flex-col">
              <BudgetRing
                used={costs.data?.budget.tokens.used ?? 0}
                limit={costs.data?.budget.tokens.limit ?? 0}
                label="tokens"
                caption={fmtCompact(costs.data?.budget.tokens.used ?? 0)}
                size={84}
              />
              <BudgetRing
                used={costs.data?.budget.render_seconds.used ?? 0}
                limit={costs.data?.budget.render_seconds.limit ?? 0}
                label="渲染秒"
                caption={fmtCompact(costs.data?.budget.render_seconds.used ?? 0)}
                size={84}
              />
            </div>
          </div>
        )}
      </SectionPanel>

      {costs.isLoading ? (
        <SkeletonTable rows={4} cols={3} />
      ) : (
        <DataTable
          title="按账号归集"
          subtitle={`共 ${(costs.data?.by_account ?? []).length} 个账号有计量`}
          columns={columns}
          rows={costs.data?.by_account ?? []}
          rowKey={(a) => a.account_id}
          empty={
            <EmptyState
              icon={<IconWallet />}
              title="窗口内没有可归集的成本"
              description="归集靠 CostLedger 上的 account_id 标签；没打标签的那部分记在下面的未归集里。"
            />
          }
          footer={
            Object.keys(costs.data?.unattributed ?? {}).length > 0 ? (
              <p className="px-4 py-2.5 text-[11.5px] text-fg-4">
                未归集：
                <span className="sw-num ml-1 text-fg-3">{fmtCost(costs.data?.unattributed)}</span>
              </p>
            ) : undefined
          }
        />
      )}
    </div>
  );
}

export default CostsTab;
