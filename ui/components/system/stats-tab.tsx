"use client";

import * as React from "react";

import { IconChart, IconRefresh } from "@/components/icons";
import { GroupedBars } from "@/components/charts/series-chart";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DataTable, Ellipsis, type Column } from "@/components/ui/data-table";
import { EmptyState } from "@/components/ui/empty-state";
import { LiveDot } from "@/components/ui/live-dot";
import { SectionPanel } from "@/components/ui/section-panel";
import { SegmentedControl } from "@/components/ui/segmented";
import { SkeletonStat, SkeletonTable } from "@/components/ui/skeleton";
import { StatCard } from "@/components/ui/stat-card";
import {
  ACCOUNT_STATUS_LABEL,
  fmtCompact,
  fmtNum,
  fmtTime,
  PLATFORM_LABEL,
  toneForAccount,
} from "@/lib/format";
import { useApi } from "@/lib/hooks";
import type { Stats, StatsAccount } from "@/lib/types";
import { describeError } from "@/lib/api";

const DAY_CHIPS = [
  { value: "7", label: "7 天" },
  { value: "14", label: "14 天" },
  { value: "30", label: "30 天" },
];

/** 指标列。`null` 不是 0，是"这个平台没有该字段"，界面显示 —。 */
const METRICS: { key: string; label: string }[] = [
  { key: "views", label: "曝光" },
  { key: "likes", label: "点赞" },
  { key: "comments", label: "评论" },
  { key: "shares", label: "转发" },
  { key: "collects", label: "收藏" },
  { key: "follows", label: "涨粉" },
];

/** 统计 tab：每日序列 + 各账号表。口径全部以后端为准，前端不做二次换算。 */
export function StatsTab() {
  const [days, setDays] = React.useState("7");
  const stats = useApi<Stats>("/stats", { days });
  const daily = stats.data?.daily ?? [];
  const accounts = stats.data?.accounts ?? [];

  const columns: Column<StatsAccount>[] = [
    {
      key: "account",
      header: "账号",
      className: "w-full max-w-0",
      cell: (a) => (
        <span className="flex min-w-0 items-center gap-2">
          <LiveDot tone={toneForAccount(a.status)} size={6} />
          <Ellipsis className="sw-num text-fg" title={a.id}>
            {a.id}
          </Ellipsis>
          <Badge tone="muted">{PLATFORM_LABEL[a.platform] ?? a.platform}</Badge>
        </span>
      ),
    },
    {
      key: "status",
      header: "状态",
      cell: (a) => (
        <span className="sw-keep text-[12px] text-fg-3">
          {ACCOUNT_STATUS_LABEL[a.status] ?? a.status}
        </span>
      ),
    },
    // 钱与量右对齐 + tabular——列对不齐的数字表，读的人得逐行找小数点
    { key: "published", header: "发布", align: "right", cell: (a) => a.published },
    { key: "failed", header: "失败", align: "right", cell: (a) => a.failed },
    { key: "dead", header: "死信", align: "right", cell: (a) => a.dead_letter },
    { key: "pending", header: "待审", align: "right", cell: (a) => a.pending_review },
    {
      key: "today",
      header: "今日",
      align: "right",
      cell: (a) => `${a.used_today}/${a.daily_limit}`,
    },
    ...METRICS.map<Column<StatsAccount>>((m) => ({
      key: m.key,
      header: m.label,
      align: "right",
      cell: (a) =>
        a.metrics?.[m.key] === null || a.metrics?.[m.key] === undefined
          ? "—"
          : fmtCompact(a.metrics[m.key] as number),
    })),
    {
      key: "cost",
      header: "成本",
      align: "right",
      cell: (a) => fmtCompact(a.cost?.tokens ?? 0),
    },
    {
      // 时长 / 时刻列照旧左对齐 muted，绝对时间挂 title
      key: "last",
      header: "上次发布",
      cell: (a) => (
        <span className="sw-num sw-keep text-[11.5px] text-fg-4" title={a.last_published_at ?? undefined}>
          {fmtTime(a.last_published_at)}
        </span>
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <SegmentedControl label="窗口" value={days} onChange={setDays} options={DAY_CHIPS} />
        <Button size="sm" onClick={() => void stats.mutate()}>
          <IconRefresh size={12} />
          刷新
        </Button>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {stats.isLoading ? (
          // 骨架与真身同数量同高度：数据回来时这一排不会往下长
          Array.from({ length: 4 }).map((_, i) => <SkeletonStat key={i} />)
        ) : (
          <>
            <StatCard
              label={`${days} 天发布`}
              value={fmtNum(stats.data?.totals.published ?? 0)}
              hint="成功落地的条数"
              sub={`统计日 ${stats.data?.day ?? "—"}`}
              series={daily.map((d) => d.published)}
            />
            <StatCard
              label="待审"
              value={fmtNum(stats.data?.totals.pending_review ?? 0)}
              hint="等人工卡点"
              sub="批准即排期"
            />
            <StatCard
              label="死信"
              value={fmtNum(stats.data?.totals.dead_letter ?? 0)}
              hint="终态，需复投"
              sub="复投会换新 id"
              series={daily.map((d) => d.dead_letter)}
            />
            <StatCard
              label="今日 tokens"
              value={fmtCompact(stats.data?.budget.tokens.used ?? 0)}
              hint="闸门按 UTC 日重置"
              sub={`预算 ${fmtCompact(stats.data?.budget.tokens.limit ?? 0)}`}
              series={daily.map((d) => d.cost.tokens ?? 0)}
            />
          </>
        )}
      </div>

      <SectionPanel title="每日发布与死信" subtitle={`最近 ${days} 天`}>
        <div className="px-4 py-4">
          {stats.error ? (
            <EmptyState title="没能取到统计" description={describeError(stats.error)} />
          ) : daily.length === 0 ? (
            <EmptyState
              icon={<IconChart />}
              title="窗口内没有数据"
              description="窗口按发布记录的更新时刻切。把范围拉长，或先让排期跑到一次发布。"
            />
          ) : (
            <GroupedBars
              labels={daily.map((d) => d.day)}
              series={[
                {
                  key: "published",
                  label: "已发布",
                  color: "var(--sw-ok)",
                  values: daily.map((d) => d.published),
                },
                {
                  key: "dead",
                  label: "死信",
                  color: "var(--sw-err)",
                  values: daily.map((d) => d.dead_letter),
                },
              ]}
            />
          )}
        </div>
      </SectionPanel>

      {stats.isLoading ? (
        <SkeletonTable rows={5} cols={6} />
      ) : (
        <DataTable
          columns={columns}
          rows={accounts}
          rowKey={(a) => a.id}
          // 指标列多，这张表是全站唯一允许横滚的：它本来就是一张宽报表，
          // 而且没有行操作列会被挤出去
          minWidth={1000}
          empty={
            <EmptyState
              icon={<IconChart />}
              title="窗口内没有账号数据"
              description="metrics 取每条内容最新一张快照再求和；这个窗口里还没有任何快照。"
            />
          }
          footer={
            Object.keys(stats.data?.unattributed_cost ?? {}).length > 0 ? (
              <p className="px-4 py-2.5 text-[11.5px] text-fg-4">
                未归集成本（CostLedger 里没打 account_id 标签的部分）：
                <span className="sw-num ml-1 text-fg-3">
                  {Object.entries(stats.data?.unattributed_cost ?? {})
                    .map(([k, v]) => `${k} ${fmtCompact(v)}`)
                    .join(" · ")}
                </span>
              </p>
            ) : undefined
          }
        />
      )}
    </div>
  );
}

export default StatsTab;
