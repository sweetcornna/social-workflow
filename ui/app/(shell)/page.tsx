"use client";

import Link from "next/link";
import * as React from "react";

import {
  IconAlert,
  IconArrowRight,
  IconCalendar,
  IconExternal,
  IconInbox,
  IconMessage,
  IconLayers,
  IconPulse,
  IconUsers,
  IconWallet,
} from "@/components/icons";
import { PageHeader } from "@/components/layout/page-header";
import { AttentionDeck, type TodoItem } from "@/components/today/attention-deck";
import { isOnAirToday, ScheduleBand } from "@/components/today/schedule-band";
import { TopicPool } from "@/components/today/topic-pool";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { LiveDot } from "@/components/ui/live-dot";
import { SectionPanel } from "@/components/ui/section-panel";
import { SkeletonRows } from "@/components/ui/skeleton";
import { describeError } from "@/lib/api";
import {
  ACTION_LABEL,
  fmtCompact,
  fmtNum,
  fmtPercent,
  fmtSince,
  fmtTime,
} from "@/lib/format";
import { POLL, useApi } from "@/lib/hooks";
import type {
  AccountRow,
  ContentRow,
  Dashboard,
  EventRow,
  Page as PageT,
  Stats,
} from "@/lib/types";
import { todayRangeIso } from "@/lib/windows";

const FAILED_ACTIONS = new Set([
  "publish_failed",
  "dead_letter",
  "failed",
  "reject",
  "suspend",
]);

/**
 * 今日 —— 工作台着陆页。
 *
 * 它只回答运营每天早上的两个问题：**有什么等我处理**、**今天几点发什么**。
 *
 * **刻意不抄 dormice 的仪表盘公式**（顶排四张统计卡 + 一张主图）。那套公式
 * 假设着陆页是指标面，读者是来看趋势的；而这一页是**待办面**，读者是来问
 * "现在轮到我做什么吗"的。四张永远亮着的统计卡会把唯一重要的那件事
 * （「需要你」）挤到折叠线以下，还会在什么都不用做的日子里假装页面很忙。
 *
 * 所以版式是：一行关键读数（压扁的统计卡）→「需要你」待办组（hero）→
 * 今日时间带 → 最近活动 → 选题池。数字让位给待办，这是任务导向 IA 的核心。
 */
export default function TodayPage() {
  const { data, error, isLoading, mutate } = useApi<Dashboard>("/dashboard", undefined, {
    refreshInterval: POLL.dashboard,
  });
  const { data: stats } = useApi<Stats>("/stats", { days: 7 });
  const { data: accounts } = useApi<AccountRow[]>("/accounts", undefined, {
    refreshInterval: POLL.accounts,
  });

  const now = React.useMemo(() => new Date(), []);

  // 账号时区集合。字符串做依赖，免得每次 render 都换一个新数组身份触发重取
  const zoneKey = React.useMemo(
    () => [...new Set((accounts ?? []).map((a) => a.policy.timezone))].sort().join("|"),
    [accounts],
  );
  const zones = React.useMemo(() => zoneKey.split("|").filter(Boolean), [zoneKey]);

  // 今日排期带只要今天这一天：口径是后端的 coalesce(scheduled_at, updated_at)。
  // 区间取**各账号时区今天的并集**——按浏览器本地日去捞，浏览器在 UTC-7 时
  // Asia/Shanghai 账号今天 19:00 的稿落在本地的昨天，会被整条截掉，时间带就空了。
  // 多捞回来的那部分由时间带按各泳道自己的 tz 筛掉，不会串台。
  const range = React.useMemo(() => todayRangeIso(now, zones), [now, zones]);
  const { data: todayContent } = useApi<PageT<ContentRow>>("/content", {
    from: range.from,
    to: range.to,
    limit: 200,
  });

  const counters = data?.counters;
  const pending = counters?.pending_review ?? 0;
  // 「无人值守」链路上唯一还需要人的一步。它排在待审前面：待审是可以攒着的，
  // 等确认的稿有 TTL，拖过去就自动驳回了
  const awaiting = counters?.awaiting_confirm ?? 0;
  const relogin = counters?.accounts_needing_relogin ?? 0;
  // degraded 也得算进"需要你"。之前只看 needs_relogin，4 个号连不上 sidecar
  // 时首页照样说"账号都在线"——那是在骗人
  const degraded = counters?.accounts_degraded ?? 0;
  const dead = counters?.dead_letter ?? 0;
  const failed = counters?.failed ?? 0;

  const tokenPct = Math.round(
    fmtPercent(data?.budget.tokens.used ?? 0, data?.budget.tokens.limit ?? 0),
  );
  const renderPct = Math.round(
    fmtPercent(data?.budget.render_seconds.used ?? 0, data?.budget.render_seconds.limit ?? 0),
  );
  const budgetPct = Math.max(tokenPct, renderPct);

  const accountsOk = (data?.platforms ?? []).reduce((n, p) => n + p.ok, 0);
  const accountsTotal = (data?.platforms ?? []).reduce((n, p) => n + p.accounts, 0);
  // 人工停用的号不算"异常"，但也不算"在线"——分母里把它们摘掉，
  // 否则停用两个号首页就永远显示"3/5 正常"，看着像坏了
  const accountsPaused = counters?.accounts_suspended ?? 0;
  const accountsLive = Math.max(accountsTotal - accountsPaused, 0);

  const todos: TodoItem[] = [
    {
      key: "confirm",
      active: awaiting > 0,
      icon: IconMessage,
      tone: "amber",
      title: (
        <>
          <Strong tone="amber">{fmtNum(awaiting)} 条</Strong> 等你确认发布
        </>
      ),
      detail: "已排期。确认后才会发布，工作台与 Telegram 均可操作",
      href: "/schedule/?status=scheduled",
      cta: "去确认",
    },
    {
      key: "pending",
      active: pending > 0,
      icon: IconInbox,
      tone: "amber",
      title: (
        <>
          <Strong tone="amber">{fmtNum(pending)} 条</Strong> 等你审
        </>
      ),
      detail: "批准即排期；含视频的必须先看完成片才解锁批准",
      href: "/review/",
      cta: "去审核台",
    },
    {
      key: "relogin",
      active: relogin > 0,
      icon: IconUsers,
      tone: "err",
      title: (
        <>
          <Strong tone="err">{fmtNum(relogin)} 个账号</Strong> 需要扫码 / 验证码
        </>
      ),
      detail: "掉线期间该号的排期项会被挂起，登录回来自动放回队列",
      href: "/accounts/",
      cta: "去扫码",
    },
    {
      key: "degraded",
      active: degraded > 0,
      icon: IconUsers,
      tone: "warn",
      title: (
        <>
          <Strong tone="warn">{fmtNum(degraded)} 个账号</Strong> 连不上它的 sidecar
        </>
      ),
      // 与上面那条刻意用不同的说法：这条不需要你掏手机，需要你去看那台机器
      detail: "不是登录过期：容器 / 上传器没在跑。稿子照出，但发布会失败",
      href: "/accounts/",
      cta: "去看机器",
    },
    {
      key: "dead",
      active: dead > 0,
      icon: IconLayers,
      tone: "err",
      title: (
        <>
          死信 <Strong tone="err">{fmtNum(dead)} 条</Strong>
        </>
      ),
      detail: "终态，不能原地复活；只能复投成新的待审草稿",
      href: "/system/?tab=jobs",
      cta: "去复投",
    },
    {
      key: "failed",
      active: failed > 0,
      icon: IconAlert,
      tone: "warn",
      title: (
        <>
          发布失败 <Strong tone="warn">{fmtNum(failed)} 条</Strong>
        </>
      ),
      detail: "还没落到死信，可以在排期页直接重投",
      href: "/schedule/?status=publish_failed",
      cta: "去重投",
    },
    {
      key: "budget",
      active: budgetPct >= 70,
      icon: IconWallet,
      tone: budgetPct >= 90 ? "err" : "warn",
      title: (
        <>
          今日预算已用 <Strong tone={budgetPct >= 90 ? "err" : "warn"}>{budgetPct}%</Strong>
        </>
      ),
      detail: `tokens ${tokenPct}% · 渲染秒 ${renderPct}%；闸门按 UTC 日重置`,
      href: "/system/?tab=costs",
      cta: "看成本",
    },
  ];
  const liveTodos = todos.filter((t) => t.active).length;

  const events = (data?.events ?? []).slice(0, 12);
  // "今天这根轴上有没有内容"要与时间带同口径：按**各账号自己时区的今天**数，
  // 否则捞回来的并集里那些别人时区的内容会让这句话说反
  const tzOf = React.useMemo(
    () => new Map((accounts ?? []).map((a) => [a.id, a.policy.timezone])),
    [accounts],
  );
  const onAirToday = (todayContent?.items ?? []).filter((it) =>
    isOnAirToday(it, tzOf.get(it.account_id) ?? "", now),
  ).length;

  return (
    <div className="flex w-full max-w-5xl flex-col gap-4 p-4 md:p-6">
      <PageHeader
        title="今日"
        emphasis="待办与今天的轴"
        actions={
          <>
            <Link href="/review/">
              <Button variant="primary">
                <IconInbox size={13} />
                进审核台
              </Button>
            </Link>
            <Link href="/schedule/">
              <Button>
                看排期
                <IconArrowRight size={13} />
              </Button>
            </Link>
          </>
        }
      />

      {/*
        关键数字压成一行读数，不做四张统计卡。理由见文件头：这是待办面不是
        指标面，数字在这里只是背景，不该占掉首屏三分之一。
      */}
      <div className="sw-num flex flex-wrap items-center gap-x-3 gap-y-1 text-[11.5px] text-fg-4">
        <span className="flex items-center gap-1.5 text-fg-3">
          <LiveDot
            tone={data && accountsOk === accountsLive ? "ok" : "warn"}
            pulse={Boolean(data)}
            size={6}
          />
          {accountsOk}/{accountsLive || 0} 账号正常
        </span>
        {accountsPaused > 0 ? <Readout>{accountsPaused} 个已停用</Readout> : null}
        <Readout>近 7 天发布 {fmtNum(stats?.totals.published ?? 0)}</Readout>
        <Readout>失败 {fmtNum(stats?.totals.failed ?? 0)}</Readout>
        <Readout>
          今日 tokens {fmtCompact(data?.budget.tokens.used ?? 0)}（{tokenPct}%）
        </Readout>
        <Readout>渲染 {fmtCompact(data?.budget.render_seconds.used ?? 0)} 秒</Readout>
        <Readout>数据 {fmtTime(data?.generated_at)}</Readout>
      </div>

      {/* ── hero：需要你 ─────────────────────────────────────────────── */}
      <section className="flex flex-col gap-2">
        <h2 className="flex items-center gap-2 text-[13.5px] font-medium text-fg">
          需要你
          {liveTodos > 0 ? <Badge tone="amber">{liveTodos}</Badge> : null}
        </h2>
        {error ? (
          <EmptyState
            title="没能取到今天的状态"
            description={describeError(error)}
            action={
              <Button size="sm" onClick={() => void mutate()}>
                重试
              </Button>
            }
          />
        ) : isLoading ? (
          <SkeletonRows rows={2} className="p-0" />
        ) : (
          <AttentionDeck todos={todos} />
        )}
      </section>

      {/* ── 今日排期时间带 + 最近动静 ────────────────────────────────── */}
      <div className="grid gap-4 xl:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
        <SectionPanel
          title="今日排期"
          actions={
            <Link
              href="/schedule/"
              className="text-[12px] text-fg-3 transition-colors hover:text-primary"
            >
              全部排期
            </Link>
          }
        >
          {isLoading ? (
            <SkeletonRows rows={4} />
          ) : (accounts ?? []).length === 0 ? (
            <div className="p-6">
              <EmptyState
                icon={<IconCalendar />}
                title="台账里还没有账号"
                description="写好 accounts.yaml 后跑 uv run python -m core.accounts sync 把它灌进库，这里才画得出泳道。"
              />
            </div>
          ) : (
            <>
              <ScheduleBand accounts={accounts ?? []} items={todayContent?.items ?? []} now={now} />
              {onAirToday === 0 ? (
                <p className="border-t border-line px-4 py-2.5 text-[11.5px] text-fg-4">
                  今天这根轴上还没有内容。批准一条稿子就会落到某个合法槽位上——
                  草稿不画在这里，它们还没有发布时刻。
                </p>
              ) : null}
            </>
          )}
        </SectionPanel>

        <SectionPanel
          title={
            <span className="flex items-center gap-2">
              <LiveDot tone="amber" pulse size={6} />
              最近活动
            </span>
          }
          bodyClassName="sw-scroll max-h-[360px] overflow-y-auto"
        >
          {isLoading ? (
            <SkeletonRows rows={5} />
          ) : events.length === 0 ? (
            <div className="p-6">
              <EmptyState
                icon={<IconPulse />}
                title="还没有动静"
                description="审核日志与发布记录会混排在这里。批准一条内容，或等一次 tick 跑完就有了。"
              />
            </div>
          ) : (
            <ul className="divide-y divide-line">
              {events.map((ev, i) => (
                <EventLine key={`${ev.kind}-${ev.item_id}-${ev.at}-${i}`} ev={ev} />
              ))}
            </ul>
          )}
        </SectionPanel>
      </div>

      <TopicPool />
    </div>
  );
}

/** 读数行里的一格。前面自带一个中点分隔，省得每处手写。 */
function Readout({ children }: { children: React.ReactNode }) {
  return (
    <span className="flex items-center gap-3">
      <span aria-hidden="true" className="text-fg-5">
        ·
      </span>
      {children}
    </span>
  );
}

/**
 * 待办标题里的那个数。
 * 原来是琥珀色 `<em>`；现在只加重字重、按 tone 上色，不再倾斜——
 * 中文的斜体是浏览器伪造的，字形会歪得不成样子。
 */
function Strong({ tone, children }: { tone: "amber" | "warn" | "err"; children: React.ReactNode }) {
  return (
    <span
      className={
        tone === "err" ? "font-medium text-err" : tone === "warn" ? "font-medium text-warn" : "font-medium text-primary"
      }
    >
      {children}
    </span>
  );
}

function EventLine({ ev }: { ev: EventRow }) {
  const failed = FAILED_ACTIONS.has(ev.action);
  const done = ev.action === "done" || ev.action === "approve";
  const tone = failed ? "err" : done ? "ok" : "amber";
  return (
    <li className="flex items-start gap-2.5 px-4 py-2.5 transition-colors hover:bg-row-hover">
      <LiveDot tone={tone} className="mt-[6px]" size={6} />
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2">
          <span className="truncate text-[12.5px] text-fg">{ev.title || ev.item_id}</span>
          <span className="sw-num shrink-0 text-[10.5px] text-fg-4">{fmtSince(ev.at)}前</span>
        </div>
        <div className="mt-0.5 flex items-center gap-1.5 text-[11px] text-fg-3">
          <Badge tone={tone}>{ACTION_LABEL[ev.action] ?? ev.action}</Badge>
          <span className="sw-num truncate">{ev.account_id}</span>
        </div>
      </div>
      {ev.url ? (
        <a
          href={ev.url}
          target="_blank"
          rel="noreferrer"
          className="mt-1 shrink-0 text-fg-4 transition-colors hover:text-primary"
          aria-label="打开线上链接"
        >
          <IconExternal size={13} />
        </a>
      ) : null}
    </li>
  );
}
