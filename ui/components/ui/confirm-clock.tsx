"use client";

import * as React from "react";

import { fmtClock, fmtRemaining, zoneNote } from "@/lib/format";
import type { ContentRow } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * 「发布前确认」的双时刻读数。
 *
 * 为什么不是一个「待确认」徽章
 * ----------------------------
 * 徽章会把这条信息里唯一有张力的东西丢掉：一条等确认的稿**同时挂着两个时刻**，
 * 而且它们朝相反方向走——发布槽位（19:00）钉死不动，决定期限一直在缩短。
 * 运营真正要判断的是"我还有没有时间想一下"，那是第二个数回答的，徽章答不了。
 *
 * 所以做成两行读数：上行是钟点（固定），下行是余量（递减），共用 `sw-num` 的
 * tabular 数字，读起来像同一台仪表上的两个刻度。这是现有类型系统本来就擅长的事
 * （统计页的数字全走这套），不是新发明一套东西。
 *
 * 全页唯一允许"抢眼"的地方就是余量告急那一档（升到 `primary`，琥珀色系里更重的
 * 一档）。**没有闪烁、没有动效**——要传达的是紧迫，不是焦虑，而且这一页在
 * `prefers-reduced-motion` 下必须完全安静。
 *
 * 刻意**没有**进度条：3 小时 40 分占整个窗口的几成，对"发不发"这个决定没有影响，
 * 那条进度条只是仪表盘的默认答案。删掉它是这处设计里拿掉的那件多余配饰。
 *
 * 为什么按状态门控（不只按 `confirm_required`）
 * ----------------------------------------------
 * 排期页对每一行都无差别渲染 `<ConfirmClock compact />`，`confirm_required` 只说
 * "这条内容需要人确认"，不说"现在还有没有意义去读这个钟"——已经发出去的 `published`
 * 行如果还读出"已确认，到点就发"，人会以为它还没发；被 TTL 自动驳回的 `rejected` 行
 * 读出"决定期限已过"，那是个过期的、已经不再成立的读数。两者都会误导运营。读数只在
 * "还没发、确认与否仍有意义"的 `scheduled` 状态才该出现。
 */

/** 余量低于这个值就升到更重的一档。2 小时 = "今天之内处理" 与 "现在就得看" 的分界 */
export const URGENT_MS = 2 * 60 * 60 * 1000;

export interface ConfirmReadout {
  /** 上行：发布钟点（账号时区） */
  slot: string;
  /** 时区标注。与浏览器同区时为空串——同区标一遍纯属噪音 */
  zone: string;
  /** 下行整句 */
  line: string;
  /** 余量告急 */
  urgent: boolean;
  /** 已经确认过了 */
  done: boolean;
}

/**
 * 算出两行读数。抽成纯函数是为了让文案能被单测钉死——
 * 这几句话是运营每天要看的东西，改一个字都该是有意为之。
 */
export function readConfirmClock(
  item: ContentRow,
  now: number,
  tz?: string,
): ConfirmReadout {
  const slot = fmtClock(item.scheduled_at, tz);
  const zone = zoneNote(tz);
  if (item.confirmed_at) {
    return { slot, zone, line: "已确认，到点就发", urgent: false, done: true };
  }
  const left = item.confirm_deadline
    ? new Date(item.confirm_deadline).getTime() - now
    : Number.NaN;
  if (Number.isNaN(left)) {
    return { slot, zone, line: "等你确认", urgent: false, done: false };
  }
  if (left <= 0) {
    // 已经过期但巡检还没跑到。说清楚接下来会发生什么，别让人以为还能点
    return {
      slot,
      zone,
      line: "决定期限已过，下一轮会自动驳回",
      urgent: true,
      done: false,
    };
  }
  return {
    slot,
    zone,
    line: `还有 ${fmtRemaining(left)}决定`,
    urgent: left <= URGENT_MS,
    done: false,
  };
}

export interface ConfirmClockProps {
  item: ContentRow;
  /** 该内容所属账号的时区。时刻按它显示，与排期页那根轴同一口径（P11.1） */
  tz?: string;
  /** 当前时刻（毫秒）。传了就不自己走表，测试靠它钉死 */
  now?: number;
  /** 排期页行内用紧凑版（只有下行），审核台右栏用完整版 */
  compact?: boolean;
  className?: string;
}

export function ConfirmClock({
  item,
  tz,
  now,
  compact,
  className,
}: ConfirmClockProps) {
  // 每分钟自己走一格。秒级刷新对"还有 3 小时 40 分"这种粒度毫无意义，
  // 只会让整页每秒重渲染一次
  const [tick, setTick] = React.useState(() => now ?? Date.now());
  React.useEffect(() => {
    if (now !== undefined) return;
    const timer = window.setInterval(() => setTick(Date.now()), 60_000);
    return () => window.clearInterval(timer);
  }, [now]);

  if (!item.confirm_required || item.status !== "scheduled") return null;
  const read = readConfirmClock(item, now ?? tick, tz);
  // 告急那一档从 primary(500) 降到 primary-deep(700)：这行是**正文级读数**，
  // 500 陶土对卡面只有 3 点几比一（Organic 自己写明 accent 只保证 chrome 级
  // 对比度，tint 底与正文要用深阶）。行为、文案、testid 一个字没动。
  const tone = read.done
    ? "text-ok"
    : read.urgent
      ? "text-primary-deep"
      : "text-fg-3";

  if (compact) {
    return (
      <span
        className={cn("sw-num text-[10.5px] leading-tight", tone, className)}
        data-testid="confirm-clock"
        data-urgent={read.urgent ? "1" : "0"}
      >
        {read.line}
      </span>
    );
  }

  return (
    <div
      className={cn("flex flex-col gap-1", className)}
      data-testid="confirm-clock"
      data-urgent={read.urgent ? "1" : "0"}
    >
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <span className="sw-num text-[22px] leading-none text-fg">
          {read.slot}
        </span>
        <span className="text-[12px] text-fg-3">发布</span>
        {read.zone ? (
          <span className="sw-num text-[10.5px] text-fg-4">
            {read.zone}
          </span>
        ) : null}
      </div>
      <div className={cn("sw-num text-[12.5px] leading-tight", tone)}>
        {read.line}
      </div>
    </div>
  );
}
