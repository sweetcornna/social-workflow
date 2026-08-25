"use client";

import { useRouter, useSearchParams } from "next/navigation";
import * as React from "react";

import { PageHeader } from "@/components/layout/page-header";
import { CostsTab } from "@/components/system/costs-tab";
import { InsightsTab } from "@/components/system/insights-tab";
import { JobsTab } from "@/components/system/jobs-tab";
import { RuntimeTab } from "@/components/system/runtime-tab";
import { StatsTab } from "@/components/system/stats-tab";
import { SegmentedControl } from "@/components/ui/segmented";
import { SkeletonRows } from "@/components/ui/skeleton";
import { isSystemTab, SYSTEM_TABS, type SystemTab } from "@/lib/nav";

export default function SystemPage() {
  return (
    <React.Suspense fallback={<SkeletonRows rows={8} />}>
      <SystemView />
    </React.Suspense>
  );
}

/**
 * 系统 —— 出事之后才翻的那一摞。
 *
 * 原来的 统计 / 成本 / 复盘 / 任务 / 系统 五张平行页合成一页五个 tab：
 * 它们都不是每天必做的事，不该各占一格侧栏。tab 走 `?tab=`，
 * ⌘K 与今日页的待办都能直接深链进来。
 *
 * P13 删掉了页头那行随 tab 变的口径说明（"窗口按 PublishRecord.updated_at 切"
 * 之类）。那五句是文档不是界面：它们每次渲染都在，却只在第一次有用，而且
 * 会随 tab 抖动——页头高度不该跟着内容变。真正要贴身解释的口径已经就近落在
 * 对应面板的读数与空态里。
 */
function SystemView() {
  const router = useRouter();
  const search = useSearchParams();
  const raw = search.get("tab");
  const tab: SystemTab = isSystemTab(raw) ? raw : "stats";

  return (
    <div className="flex w-full max-w-5xl flex-col gap-4 p-4 md:p-6">
      <PageHeader
        title="系统"
        emphasis="观测与门禁"
        actions={
          <SegmentedControl
            label="系统视图"
            value={tab}
            onChange={(v) => router.replace(`/system/?tab=${v}`, { scroll: false })}
            options={SYSTEM_TABS.map((t) => ({ value: t.value, label: t.label }))}
          />
        }
      />

      {tab === "stats" ? <StatsTab /> : null}
      {tab === "costs" ? <CostsTab /> : null}
      {tab === "insights" ? <InsightsTab /> : null}
      {tab === "jobs" ? <JobsTab /> : null}
      {tab === "runtime" ? <RuntimeTab /> : null}
    </div>
  );
}
