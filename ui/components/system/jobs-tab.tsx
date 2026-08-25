"use client";

import Link from "next/link";
import * as React from "react";

import {
  IconAlert,
  IconExternal,
  IconLayers,
  IconRefresh,
  IconRotate,
  IconVideo,
} from "@/components/icons";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DataTable, Ellipsis, type Column } from "@/components/ui/data-table";
import { EmptyState } from "@/components/ui/empty-state";
import { FilterMenu } from "@/components/ui/filter-menu";
import { LiveDot } from "@/components/ui/live-dot";
import { MenuItem } from "@/components/ui/popover";
import { Progress } from "@/components/ui/progress";
import { RowMenu } from "@/components/ui/row-menu";
import { SkeletonTable } from "@/components/ui/skeleton";
import { useToast } from "@/components/ui/toast";
import { apiFetch, describeError } from "@/lib/api";
import { fmtTime, PLATFORM_LABEL, RENDER_STATE_LABEL } from "@/lib/format";
import { POLL, useApi } from "@/lib/hooks";
import type {
  DeadLetterRow,
  Page as PageT,
  PublishRecordRow,
  RenderJobRow,
  RetryResult,
} from "@/lib/types";

const RENDER_FILTERS = [
  { value: "", label: "全部状态" },
  { value: "pending", label: "排队" },
  { value: "running", label: "渲染中" },
  { value: "done", label: "完成" },
  { value: "failed", label: "失败", tone: "err" as const },
  { value: "lost", label: "丢失", tone: "err" as const },
];

const PHASE_FILTERS = [
  { value: "", label: "全部阶段" },
  { value: "in_flight", label: "投递中", tone: "warn" as const },
  { value: "done", label: "已完成", tone: "ok" as const },
  { value: "failed", label: "失败", tone: "err" as const },
];

/**
 * 渲染与死信 tab。
 *
 * 三张表全部走 DataTable（P13）：原来是三份手写 `<ul>`，行高、内边距、
 * hover 底色各写各的，并排放在同一页上看得出三种节奏。
 *
 * 渲染任务的 `lost` 表示 sidecar 重启把任务表丢了；死信是 P0 冻结的终态，
 * 只能复投成新草稿（id 会变）。
 */
export function JobsTab() {
  const toast = useToast();
  const [renderState, setRenderState] = React.useState("");
  const [phase, setPhase] = React.useState("");

  const render = useApi<PageT<RenderJobRow>>("/jobs/render", { state: renderState, limit: 100 });
  // 只在还有进行中的任务时开轮询；全部结束就停（WORKBENCH_API.md §14）
  const running = (render.data?.items ?? []).some(
    (j) => j.state === "running" || j.state === "pending",
  );
  const renderLive = useApi<PageT<RenderJobRow>>(
    running ? "/jobs/render" : null,
    { state: "running", limit: 100 },
    { refreshInterval: POLL.renderJobs },
  );
  void renderLive;

  const records = useApi<PageT<PublishRecordRow>>("/jobs/publish_records", { phase, limit: 100 });
  const dead = useApi<PageT<DeadLetterRow>>("/jobs/dead_letters", { limit: 100 });

  async function requeue(itemId: string) {
    try {
      const res = await apiFetch<RetryResult>(`/content/${itemId}/retry_now`, { method: "POST" });
      toast.ok(res.message);
      await Promise.all([dead.mutate(), records.mutate()]);
    } catch (e) {
      toast.err(describeError(e));
    }
  }

  const renderColumns: Column<RenderJobRow>[] = [
    {
      key: "title",
      header: "任务",
      className: "w-full max-w-0",
      cell: (j) => (
        <span className="flex min-w-0 flex-col">
          <Ellipsis className="text-fg" title={j.title || j.id}>
            {j.title || j.id}
          </Ellipsis>
          {j.last_error ? (
            <Ellipsis className="sw-num text-[10.5px] text-err" title={j.last_error}>
              {j.last_error}
            </Ellipsis>
          ) : null}
        </span>
      ),
    },
    {
      key: "state",
      header: "状态",
      cell: (j) => {
        const bad = j.state === "failed" || j.state === "lost";
        return (
          <span className="flex items-center gap-1.5">
            {j.state === "running" ? <LiveDot tone="amber" pulse size={6} /> : null}
            <Badge tone={bad ? "err" : j.state === "done" ? "ok" : "amber"}>
              {RENDER_STATE_LABEL[j.state] ?? j.state}
            </Badge>
          </span>
        );
      },
    },
    {
      key: "progress",
      header: "进度",
      className: "w-[7rem] min-w-[7rem]",
      cell: (j) => {
        const bad = j.state === "failed" || j.state === "lost";
        return (
          <span className="flex flex-col gap-1">
            <span className="sw-num text-[11.5px] text-fg-2">{j.progress}%</span>
            <Progress
              value={j.progress}
              tone={bad ? "err" : j.state === "done" ? "ok" : "amber"}
              label={`渲染进度 ${j.progress}%`}
            />
          </span>
        );
      },
    },
    {
      key: "provider",
      header: "渲染器",
      className: "max-w-[7rem]",
      cell: (j) => (
        <Ellipsis className="sw-num text-[11.5px] text-fg-3" title={j.provider}>
          {j.provider}
        </Ellipsis>
      ),
    },
    { key: "attempts", header: "尝试", align: "right", cell: (j) => j.attempts },
    {
      key: "updated",
      header: "更新",
      cell: (j) => (
        <span className="sw-num sw-keep text-[11.5px] text-fg-4" title={j.updated_at ?? undefined}>
          {fmtTime(j.updated_at)}
        </span>
      ),
    },
  ];

  const recordColumns: Column<PublishRecordRow>[] = [
    {
      key: "title",
      header: "内容",
      className: "w-full max-w-0",
      cell: (r) => (
        <span className="flex min-w-0 flex-col">
          <Ellipsis className="text-fg" title={r.title || r.id}>
            {r.title || r.id}
          </Ellipsis>
          <Ellipsis className="sw-num text-[10.5px] text-fg-4" title={r.idem_key}>
            {r.idem_key}
          </Ellipsis>
        </span>
      ),
    },
    {
      key: "phase",
      header: "阶段",
      cell: (r) => (
        <span className="flex items-center gap-1.5">
          {r.phase === "in_flight" ? <LiveDot tone="warn" pulse size={6} /> : null}
          <Badge tone={r.phase === "done" ? "ok" : r.phase === "failed" ? "err" : "warn"}>
            {r.phase === "done" ? "已完成" : r.phase === "failed" ? "失败" : "投递中"}
          </Badge>
        </span>
      ),
    },
    {
      key: "platform",
      header: "平台",
      cell: (r) => (
        <span className="sw-keep text-[11.5px] text-fg-3">
          {PLATFORM_LABEL[r.platform] ?? r.platform}
        </span>
      ),
    },
    { key: "attempts", header: "尝试", align: "right", cell: (r) => r.attempts },
    {
      key: "updated",
      header: "更新",
      cell: (r) => (
        <span className="sw-num sw-keep text-[11.5px] text-fg-4" title={r.updated_at ?? undefined}>
          {fmtTime(r.updated_at)}
        </span>
      ),
    },
    {
      key: "actions",
      header: "",
      cell: (r) =>
        r.url ? (
          <a
            href={r.url}
            target="_blank"
            rel="noreferrer"
            aria-label="打开线上链接"
            className="inline-flex p-1 text-fg-4 transition-colors hover:text-primary"
          >
            <IconExternal size={13} />
          </a>
        ) : null,
    },
  ];

  const deadColumns: Column<DeadLetterRow>[] = [
    {
      key: "title",
      header: "内容",
      className: "w-full max-w-0",
      cell: (d) => (
        <span className="flex min-w-0 items-center gap-2">
          <LiveDot tone="err" size={6} />
          <Ellipsis className="text-fg" title={d.title || d.item_id}>
            {d.title || d.item_id}
          </Ellipsis>
        </span>
      ),
    },
    {
      key: "account",
      header: "账号",
      className: "max-w-[9rem]",
      cell: (d) => (
        <Ellipsis className="sw-num text-[11.5px] text-fg-3" title={d.account_id}>
          {d.account_id}
        </Ellipsis>
      ),
    },
    {
      key: "reason",
      header: "原因",
      className: "w-full max-w-0",
      cell: (d) => (
        <Ellipsis className="text-[11.5px] text-err" title={d.reason}>
          {d.reason}
        </Ellipsis>
      ),
    },
    {
      key: "at",
      header: "落库",
      cell: (d) => (
        <span className="sw-num sw-keep text-[11.5px] text-fg-4" title={d.at ?? undefined}>
          {fmtTime(d.at)}
        </span>
      ),
    },
    {
      key: "actions",
      header: "",
      className: "text-right",
      cell: (d) => (
        <span className="flex items-center justify-end gap-1">
          <Button size="sm" variant="danger" onClick={() => void requeue(d.item_id)}>
            <IconRotate size={12} />
            复投
          </Button>
          <RowMenu>
            {(close) => (
              <MenuItem onSelect={close}>
                <Link href={`/schedule/?id=${encodeURIComponent(d.item_id)}`}>在排期里定位</Link>
              </MenuItem>
            )}
          </RowMenu>
        </span>
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <div className="flex justify-end">
        <Button
          size="sm"
          onClick={() => {
            void render.mutate();
            void records.mutate();
            void dead.mutate();
          }}
        >
          <IconRefresh size={12} />
          刷新
        </Button>
      </div>

      <div className="grid gap-4">
        {render.isLoading ? (
          <SkeletonTable rows={4} cols={4} />
        ) : (
          <DataTable
            title="渲染任务"
            subtitle={`共 ${render.data?.total ?? 0} 个${running ? " · 5 秒轮询中" : ""}`}
            toolbar={
              <FilterMenu
                label="状态"
                value={renderState}
                options={RENDER_FILTERS}
                onChange={setRenderState}
              />
            }
            columns={renderColumns}
            rows={render.data?.items ?? []}
            rowKey={(j) => j.id}
            empty={
              <EmptyState
                icon={<IconVideo />}
                title="没有渲染任务"
                description="只有抖音成片才走渲染。队列空着说明没有在途的出片任务。"
              />
            }
          />
        )}

        {records.isLoading ? (
          <SkeletonTable rows={4} cols={4} />
        ) : (
          <DataTable
            title="发布记录"
            subtitle={`共 ${records.data?.total ?? 0} 条 · idem_key 是幂等的唯一真相`}
            toolbar={
              <FilterMenu label="阶段" value={phase} options={PHASE_FILTERS} onChange={setPhase} />
            }
            columns={recordColumns}
            rows={records.data?.items ?? []}
            rowKey={(r) => r.id}
            empty={
              <EmptyState
                icon={<IconLayers />}
                title="还没有发布记录"
                description="批准一条内容并等排期到点；也可以在「自检与任务」里手动跑一次 scheduled_publish。"
              />
            }
          />
        )}
      </div>

      {dead.isLoading ? (
        <SkeletonTable rows={3} cols={5} />
      ) : (
        <DataTable
          title="死信"
          subtitle={`共 ${dead.data?.total ?? 0} 条 · 终态，复投会生成新草稿（id 会变）`}
          toolbar={
            <Badge tone={(dead.data?.total ?? 0) > 0 ? "err" : "ok"}>
              {(dead.data?.total ?? 0) > 0 ? (
                <>
                  <IconAlert size={11} />
                  待复投 {dead.data?.total ?? 0}
                </>
              ) : (
                "队列干净"
              )}
            </Badge>
          }
          columns={deadColumns}
          rows={dead.data?.items ?? []}
          rowKey={(d) => d.item_id}
          empty={
            <EmptyState
              icon={<IconLayers />}
              title="没有死信"
              description="重试三次仍失败的内容才会落到这里。空着是好事。"
            />
          }
        />
      )}
    </div>
  );
}

export default JobsTab;
