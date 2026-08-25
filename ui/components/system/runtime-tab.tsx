"use client";

import * as React from "react";

import {
  IconAlert,
  IconPlay,
  IconRefresh,
  IconShield,
} from "@/components/icons";
import { DataState } from "@/components/layout/data-state";
import { ReminderChannel } from "@/components/system/reminder-channel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { LiveDot } from "@/components/ui/live-dot";
import { SectionPanel } from "@/components/ui/section-panel";
import { useToast } from "@/components/ui/toast";
import { apiFetch, describeError } from "@/lib/api";
import { toneForCheck } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import type { Preflight, SystemInfo, TickResult, TicksInfo } from "@/lib/types";

/**
 * preflight 与 tick tab。
 * 手动触发与定时任务走的是同一批函数（`core.scheduler.TICKS`）。
 * preflight 慢且会重建 DB 引擎，只在点击时跑，绝不轮询。
 */
export function RuntimeTab() {
  const toast = useToast();
  const info = useApi<SystemInfo>("/system/info");
  const ticks = useApi<TicksInfo>("/system/ticks");

  const [preflight, setPreflight] = React.useState<Preflight | null>(null);
  const [preflightBusy, setPreflightBusy] = React.useState(false);
  const [tickBusy, setTickBusy] = React.useState<string | null>(null);
  const [lastTick, setLastTick] = React.useState<TickResult | null>(null);

  async function runPreflight(offline: boolean) {
    setPreflightBusy(true);
    try {
      const res = await apiFetch<Preflight>("/system/preflight", {
        query: { offline },
      });
      setPreflight(res);
      toast[res.passed ? "ok" : "warn"](
        res.passed ? "门禁全部通过" : `门禁有 ${res.counts.FAIL ?? 0} 项 FAIL`,
      );
    } catch (e) {
      toast.err(describeError(e));
    } finally {
      setPreflightBusy(false);
    }
  }

  async function runTick(name: string) {
    setTickBusy(name);
    try {
      const res = await apiFetch<TickResult>(`/system/ticks/${name}`, {
        method: "POST",
      });
      setLastTick(res);
      toast.ok(
        res.message ?? `${name} 跑完，耗时 ${res.elapsed_s.toFixed(3)}s`,
      );
    } catch (e) {
      toast.err(describeError(e));
    } finally {
      setTickBusy(null);
    }
  }

  const d = info.data;

  return (
    <div className="grid gap-4 xl:grid-cols-[1.2fr_1fr]">
      <SectionPanel
        title="门禁自检 preflight"
        subtitle={
          preflight
            ? `${preflight.offline ? "离线模式" : "联网模式"} · 跑于 ${preflight.ran_at}`
            : "点一下才跑"
        }
        actions={
          <>
            <Button
              size="sm"
              onClick={() => void runPreflight(true)}
              loading={preflightBusy}
            >
              <IconShield size={12} />
              离线自检
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => void runPreflight(false)}
              loading={preflightBusy}
            >
              联网自检
            </Button>
          </>
        }
      >
        {preflight ? (
          <>
            <div className="flex flex-wrap items-center gap-1.5 border-b border-line px-4 py-2.5">
              <Badge tone={preflight.passed ? "ok" : "err"}>
                {preflight.passed ? "通过" : "未通过"}
              </Badge>
              {(["OK", "WARN", "FAIL", "SKIP"] as const).map((s) => (
                <Badge key={s} tone={toneForCheck(s)}>
                  {s} {preflight.counts[s] ?? 0}
                </Badge>
              ))}
            </div>
            <ul className="sw-scroll max-h-[420px] divide-y divide-line overflow-y-auto">
              {preflight.checks.map((c, i) => (
                <li
                  key={`${c.name}-${i}`}
                  className="flex items-start gap-3 px-4 py-2.5"
                >
                  <LiveDot
                    tone={toneForCheck(c.status)}
                    className="mt-1.5"
                    size={7}
                  />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-baseline gap-2">
                      <span className="text-[12.5px] text-fg">
                        {c.name}
                      </span>
                      <Badge tone={toneForCheck(c.status)}>{c.status}</Badge>
                    </div>
                    <p className="mt-0.5 text-[11.5px] leading-relaxed text-fg-3">
                      {c.detail}
                    </p>
                  </div>
                </li>
              ))}
            </ul>
          </>
        ) : (
          <EmptyState
            className="my-8"
            icon={<IconShield />}
            title="还没跑过门禁"
            description="离线自检约数秒（docker 探测上限 15 秒）；联网自检实际探测公众号、MPT 与 sidecar，约 10–20 秒。"
            action={
              <Button
                size="sm"
                onClick={() => void runPreflight(true)}
                loading={preflightBusy}
              >
                跑一次离线自检
              </Button>
            }
          />
        )}
      </SectionPanel>

      <div className="flex flex-col gap-4">
        <ReminderChannel />

        <SectionPanel title="定时任务" subtitle={ticks.data?.note}>
          <DataState
            isLoading={ticks.isLoading}
            error={ticks.error}
            onRetry={() => void ticks.mutate()}
          >
            <ul className="divide-y divide-line">
              {(ticks.data?.ticks ?? []).map((t) => (
                <li
                  key={t.name}
                  className="flex items-center justify-between gap-3 px-4 py-2.5 transition-colors hover:bg-row-hover"
                >
                  <div className="min-w-0">
                    <div className="sw-num text-[12.5px] text-fg">
                      {t.name}
                    </div>
                    <div className="mt-0.5 text-[11px] text-fg-4">
                      {t.accepts.length
                        ? `接受参数：${t.accepts.join("、")}`
                        : "无参数"}
                    </div>
                  </div>
                  <Button
                    size="sm"
                    onClick={() => void runTick(t.name)}
                    loading={tickBusy === t.name}
                    disabled={tickBusy !== null}
                  >
                    <IconPlay size={11} />
                    跑一次
                  </Button>
                </li>
              ))}
            </ul>
          </DataState>
          {lastTick ? (
            <div className="border-t border-line px-4 py-3">
              <div className="sw-label mb-1.5">
                最近一次：{lastTick.tick} · {lastTick.elapsed_s.toFixed(3)}s
              </div>
              <dl className="grid grid-cols-2 gap-x-3 gap-y-1 sm:grid-cols-3">
                {Object.entries(lastTick.stats ?? {}).map(([k, v]) => (
                  <div key={k} className="rounded-md bg-muted px-2 py-1">
                    <dt className="sw-num text-[10px] text-fg-4">{k}</dt>
                    <dd className="sw-num text-[13px] text-fg">
                      {String(v)}
                    </dd>
                  </div>
                ))}
              </dl>
              {lastTick.message ? (
                <p className="mt-2 flex items-start gap-1.5 text-[11.5px] text-warn">
                  <IconAlert size={12} className="mt-[2px] shrink-0" />
                  {lastTick.message}
                </p>
              ) : null}
            </div>
          ) : null}
        </SectionPanel>

        <SectionPanel
          title="运行信息"
          subtitle={`${d?.version ?? "—"} · ${d?.env ?? "—"}`}
          actions={
            <Button size="sm" onClick={() => void info.mutate()}>
              <IconRefresh size={12} />
              刷新
            </Button>
          }
        >
          <DataState
            isLoading={info.isLoading}
            error={info.error}
            onRetry={() => void info.mutate()}
          >
            <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 px-4 py-3 text-[12px]">
              <Row k="版本" v={`${d?.version ?? "—"} · ${d?.env ?? "—"}`} />
              <Row k="服务器时间" v={d?.time ?? "—"} />
              <Row k="时区" v={d?.timezone ?? "—"} />
              <Row
                k="LLM"
                v={`${d?.llm_backend ?? "—"} / ${d?.llm_model || "—"}`}
              />
              <Row k="数据库" v={d?.database ?? "—"} />
              <Row
                k="调度器"
                v={
                  <Badge tone={d?.scheduler_enabled ? "ok" : "muted"}>
                    {d?.scheduler_enabled ? "已开启" : "已关闭"}
                  </Badge>
                }
              />
              <Row
                k="发布器"
                v={
                  <span className="flex flex-wrap items-center gap-1.5">
                    {(d?.publishers ?? []).map((p) => (
                      <Badge key={p} tone="muted">
                        {p}
                      </Badge>
                    ))}
                  </span>
                }
              />
              <Row
                k="发布模式"
                v={
                  d ? (
                    d.use_fake_publishers ? (
                      <span className="text-warn">模拟（不会真的发布）</span>
                    ) : (
                      <span className="text-ok">真实发布</span>
                    )
                  ) : (
                    "—"
                  )
                }
              />
              <Row
                k="生成开关"
                v={
                  <Badge tone={d?.generate_enabled ? "ok" : "muted"}>
                    {d?.generate_enabled ? "已开启" : "已关闭"}
                  </Badge>
                }
              />
              <Row
                k="token 认证"
                v={
                  <Badge tone={d?.auth_required ? "amber" : "muted"}>
                    {d?.auth_required ? "已开启" : "未开启"}
                  </Badge>
                }
              />
            </dl>
          </DataState>
        </SectionPanel>
      </div>
    </div>
  );
}

function Row({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <>
      <dt className="text-fg-4">{k}</dt>
      <dd className="sw-num min-w-0 break-all text-fg-2">{v}</dd>
    </>
  );
}

export default RuntimeTab;
