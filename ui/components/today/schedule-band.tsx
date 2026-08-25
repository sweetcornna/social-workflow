"use client";

import Link from "next/link";
import * as React from "react";

import { Badge } from "@/components/ui/badge";
import {
  browserTimeZone,
  fmtTime,
  PLATFORM_LABEL,
  STATUS_LABEL,
  toneForStatus,
} from "@/lib/format";
import { resolveZone } from "@/lib/tz";
import type { AccountRow, ContentRow } from "@/lib/types";
import { DAY_MINUTES, dayKey, minutesOfDay, parseWindows } from "@/lib/windows";
import { cn } from "@/lib/utils";

/**
 * 今日排期横向时间带。
 *
 * 一根 0–24 时轴，一账号一条泳道：底色是该账号的发布窗口（`policy.publish_windows`
 * 拆出来的），点是今天落在这条轴上的内容（`timeline_at`）。回答的是运营每天
 * 第二个问题——"今天几点发什么、还有哪些空窗"。
 *
 * **一条泳道 = 那个账号自己的一天**：底色、点位、"现在"竖线，三者统一按
 * `a.policy.timezone` 算。P11 之前点位用的是浏览器本地时区，而底色是账号时区的，
 * 于是浏览器在 UTC-7 时，账号时区 19:00 的稿画在 04:00——**点落在底色外面**，
 * 看着就像"调度器把稿排到了不许发的时段"。两套口径不许再共存于同一根轴。
 *
 * 两点如实说明：
 *  - 账号时区与浏览器时区不同时，泳道右侧标出账号时区；账号没配时区就回退浏览器本地，
 *    并如实标注是回退来的，不静默假装算对了。
 *  - 没有内容就是没有内容，绝不往轴上摆演示点。
 */

const TICKS = [0, 3, 6, 9, 12, 15, 18, 21, 24];

/**
 * 只有真的落在发布轴上的内容才画成点。
 *
 * 草稿 / 审核中 / 已驳回还没有排期时刻，它们的 `timeline_at` 是 `updated_at`
 * ——那是"最后一次改动的时间"，不是"几点发"。把它们画上去等于凭空给了一个
 * 并不存在的发布时刻，属于编数据。它们的去处是审核台，不是这根轴。
 */
const ON_AIR = new Set([
  "approved",
  "scheduled",
  "suspended",
  "publishing",
  "published",
  "measured",
  "publish_failed",
  "retrying",
  "dead_letter",
]);

/** 这条内容是否已经落在发布轴上（有真实的排期 / 发布时刻）。 */
export function isOnAir(item: ContentRow): boolean {
  return ON_AIR.has(item.status);
}

/**
 * 这条内容画在轴上的哪个时刻。
 *
 * 与后端 `/content` 的 `timeline_at`（`coalesce(scheduled_at, updated_at)`）同口径，
 * 三处取值散在不同文件里过，统一收到这里，免得哪天改漏一处又对不上。
 */
export function timeOf(item: ContentRow): string | null {
  return item.timeline_at ?? item.scheduled_at ?? item.updated_at;
}

/** 这条内容是不是落在"它自己账号时区的今天"。今日页统计与时间带共用。 */
export function isOnAirToday(item: ContentRow, tz: string | null | undefined, now: Date): boolean {
  return isOnAir(item) && dayKey(timeOf(item), tz) === dayKey(now, tz);
}

export function ScheduleBand({
  accounts,
  items,
  now,
  day,
  showNow = true,
  showLegend = true,
  className,
}: {
  accounts: AccountRow[];
  items: ContentRow[];
  now: Date;
  /**
   * 要画的是哪一天，`YYYY-MM-DD`。**这个键是按各泳道自己的账号时区解读的**。
   *
   * 不传 = 每条泳道画"它自己时区的今天"（今日页就是这个语义：Asia/Shanghai 的号
   * 和 America/Los_Angeles 的号，"今天"本来就是两个不同的绝对区间）。
   */
  day?: string;
  /** 只有"今天"那条带子才该画"现在"竖线。 */
  showNow?: boolean;
  showLegend?: boolean;
  className?: string;
}) {
  const localTz = browserTimeZone();

  /**
   * 一次算清"每条泳道该画哪几条"。
   *
   * 筛选口径是**该账号时区**下的日期键：调用方给的 `items` 可能横跨两个账号本地日
   * （今日页为了不漏掉别的时区，捞的是各时区今天的并集），在这里按各自的 tz 收敛。
   * 排序与渲染共用这一份结果，免得"排在前面"和"画出来"两套口径又不一致。
   */
  const byAccount = React.useMemo(() => {
    const map = new Map<string, ContentRow[]>();
    for (const a of accounts) {
      const laneDay = day ?? dayKey(now, a.policy.timezone);
      map.set(
        a.id,
        items.filter(
          (it) =>
            it.account_id === a.id &&
            ON_AIR.has(it.status) &&
            dayKey(timeOf(it), a.policy.timezone) === laneDay,
        ),
      );
    }
    return map;
  }, [accounts, items, day, now]);

  // 有内容的账号排前面，其余按平台聚拢——空泳道也要留着，它就是"今天这个号还空着"
  const lanes = React.useMemo(
    () =>
      [...accounts].sort((a, b) => {
        const na = byAccount.get(a.id)?.length ?? 0;
        const nb = byAccount.get(b.id)?.length ?? 0;
        if (na !== nb) return nb - na;
        return a.platform.localeCompare(b.platform) || a.id.localeCompare(b.id);
      }),
    [accounts, byAccount],
  );

  return (
    <div className={cn("px-4 py-3.5", className)} data-testid="schedule-band">
      {/* 时刻刻度 */}
      <div className="relative mb-1.5 ml-[136px] h-4">
        {TICKS.map((h) => (
          <span
            key={h}
            className="sw-num absolute -translate-x-1/2 text-[10px] text-fg-4"
            style={{ left: `${(h / 24) * 100}%` }}
          >
            {String(h).padStart(2, "0")}
          </span>
        ))}
      </div>

      <div className="flex flex-col gap-1.5">
        {lanes.map((a) => {
          const spans = parseWindows(a.policy.publish_windows);
          // 一条泳道 = 这个账号时区里的一天：底色、点位、竖线全用同一个 tz
          const tz = a.policy.timezone;
          const { zone, fallback } = resolveZone(tz);
          const rows = byAccount.get(a.id) ?? [];
          const nowMin = minutesOfDay(now.toISOString(), tz) ?? 0;
          const nowPct = (nowMin / DAY_MINUTES) * 100;
          const tzMismatch = fallback || zone !== localTz;
          return (
            <div key={a.id} className="flex items-center gap-3">
              <div className="w-[124px] shrink-0">
                <div className="truncate text-[12px] text-fg-2" title={a.name}>
                  {a.name}
                </div>
                <div className="sw-num truncate text-[10px] text-fg-4">
                  {PLATFORM_LABEL[a.platform] ?? a.platform} · {a.used_today}/
                  {a.policy.daily_limit}
                </div>
              </div>

              <div className="relative h-8 flex-1 overflow-hidden rounded-md bg-muted">
                {spans.map((s, i) => (
                  <span
                    key={i}
                    aria-hidden
                    // e2e 要按**几何位置**硬断言"点落在底色内"，所以底色得能被选中。
                    // 这条陶土带是签名元素之一（另一处是 confirm-clock 的告急读数）：
                    // 走 accent-300 而不是 100 档——100 档留给「需要你」那块唯一的
                    // tint 面板，而且 100 档在 muted 轨道上几乎看不出窗口边界，
                    // 而"哪几段能发"正是这根轴要回答的问题。
                    //
                    // 填色走 primary-band 而不是 primary-line：亮侧两者同值，暗侧
                    // band 沉一档。理由在 globals.css 的 --sw-primary-band 注释里
                    // （全天号铺满 00-24 时，line 那一档在暗色下会糊成板砖）。
                    data-testid="window-span"
                    data-account={a.id}
                    className="absolute inset-y-0 bg-primary-band"
                    style={{
                      left: `${(s.startMin / DAY_MINUTES) * 100}%`,
                      width: `${((s.endMin - s.startMin) / DAY_MINUTES) * 100}%`,
                    }}
                  />
                ))}
                {TICKS.slice(1, -1).map((h) => (
                  <span
                    key={h}
                    aria-hidden
                    className="absolute inset-y-0 w-px bg-line"
                    style={{ left: `${(h / 24) * 100}%` }}
                  />
                ))}
                {showNow ? (
                  <span
                    aria-hidden
                    className="absolute inset-y-0 w-px bg-primary/70"
                    style={{ left: `${nowPct}%` }}
                  />
                ) : null}
                {rows.map((it) => {
                  const min = minutesOfDay(timeOf(it), tz);
                  if (min === null) return null;
                  const tone = toneForStatus(it.status);
                  return (
                    <Link
                      key={it.id}
                      href={`/schedule/?id=${encodeURIComponent(it.id)}`}
                      data-testid="band-item"
                      // 硬断言用：点位分钟数与它按账号时区算出来的钟点，e2e 直接读这两个属性
                      data-minutes={min}
                      data-zone={zone}
                      title={`${fmtTime(timeOf(it), tz)}（${zone}） · ${it.title} · ${
                        STATUS_LABEL[it.status] ?? it.status
                      }`}
                      className={cn(
                        "absolute top-1/2 h-3 w-3 -translate-x-1/2 -translate-y-1/2 rounded-full border-2",
                        "border-[color:var(--sw-canvas)] transition-transform hover:scale-[1.35]",
                        tone === "ok" && "bg-ok",
                        tone === "err" && "bg-err",
                        tone === "warn" && "bg-warn",
                        tone === "amber" && "bg-primary",
                        tone === "muted" && "bg-fg-4",
                      )}
                      style={{ left: `${(min / DAY_MINUTES) * 100}%` }}
                    >
                      <span className="sr-only">{it.title}</span>
                    </Link>
                  );
                })}
              </div>

              <div className="w-[104px] shrink-0 text-right">
                {rows.length > 0 ? (
                  <span className="sw-num text-[11px] text-fg-3">{rows.length} 条</span>
                ) : (
                  <span className="text-[11px] text-fg-5">空窗</span>
                )}
                {tzMismatch ? (
                  <div
                    className="sw-num truncate text-[9.5px] text-fg-4"
                    data-testid="lane-zone"
                    title={
                      fallback
                        ? `这个号没配时区，整条轴按你的浏览器时区 ${zone} 画`
                        : `整条轴按账号时区 ${zone} 画（你的浏览器在 ${localTz}）`
                    }
                  >
                    {fallback ? `${zone}（回退）` : zone}
                  </div>
                ) : null}
              </div>
            </div>
          );
        })}
      </div>

      {showLegend ? (
        <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-line pt-2.5">
          <span className="sw-label">图例</span>
          <LegendDot className="bg-primary" text="待发" />
          <LegendDot className="bg-ok" text="已发" />
          <LegendDot className="bg-err" text="失败 / 死信" />
          {/* 图例色块必须与轨道上真正的底色同色，否则图例本身就在说谎 */}
          <Badge tone="amber" className="bg-primary-line">
            底色 = 账号发布窗口
          </Badge>
          {/* 每条泳道都是"那个账号自己的一天"，所以竖线位置逐泳道算，不是一根通天的线 */}
          {showNow ? (
            <span className="sw-num text-[10.5px] text-fg-4">
              竖线 = 现在
            </span>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function LegendDot({ className, text }: { className: string; text: string }) {
  return (
    <span className="flex items-center gap-1.5 text-[11px] text-fg-3">
      <span aria-hidden className={cn("h-2.5 w-2.5 rounded-full", className)} />
      {text}
    </span>
  );
}

export default ScheduleBand;
