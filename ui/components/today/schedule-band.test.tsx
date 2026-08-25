import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { AccountRow, ContentRow } from "@/lib/types";
import { parseWindows } from "@/lib/windows";

import { ScheduleBand } from "./schedule-band";

/**
 * 时间带的时区回归。
 *
 * 生产上的症状是**点画在窗口底色外面**：窗口底色按账号时区画（12–14 / 19–22:30），
 * 点位却按浏览器本地时区画（04:00），肉眼看就是"调度器把稿排到了不许发的时段"。
 * 测试进程钉在 America/Los_Angeles（vitest.config.ts 的 env.TZ），账号在 Asia/Shanghai，
 * 两者差 15 小时——口径一旦分叉，下面的断言必红。
 */

const WINDOWS = "12:00-14:00、19:00-22:30";

function account(over: Partial<AccountRow> = {}): AccountRow {
  return {
    id: "xhs-01",
    name: "小红书测试号 01",
    platform: "xhs",
    status: "ok",
    needs_attention: false,
    policy: {
      daily_limit: 10,
      daily_target: 1,
      publish_windows: WINDOWS,
      timezone: "Asia/Shanghai",
      min_interval_minutes: 90,
      has_persona: true,
    },
    used_today: 0,
    quota_left: 10,
    last_published_at: null,
    sidecar_endpoint: null,
    supports_login: true,
    insights_updated_at: null,
    insights_error: "",
    created_at: null,
    updated_at: null,
    ...over,
  } as AccountRow;
}

function item(over: Partial<ContentRow> = {}): ContentRow {
  return {
    id: "itm_evening",
    account_id: "xhs-01",
    account_name: "小红书测试号 01",
    platform: "xhs",
    title: "地铁通勤 30 分钟能做什么",
    status: "scheduled",
    created_at: null,
    updated_at: null,
    // 账号时区 2026-08-17 19:00，正落在窗口 19:00-22:30 内
    scheduled_at: "2026-08-17T11:00:00.000Z",
    slot_text: "08-17 19:00（Asia/Shanghai）",
    published_at: null,
    platform_post_id: null,
    url: null,
    publish_phase: null,
    attempts: 0,
    last_error: null,
    needs_watch: false,
    cover_url: null,
    media: { total: 0, images: 0, videos: 0, kinds: [], cover_index: null },
    tags: [],
    review_notes: null,
    machine_review: null,
    timeline_at: "2026-08-17T11:00:00.000Z",
    ...over,
  } as unknown as ContentRow;
}

/** 那个点落在哪些窗口区间里。返回命中的区间数——0 就是画到底色外面去了。 */
function windowsCovering(minutes: number): number {
  return parseWindows(WINDOWS).filter((s) => minutes >= s.startMin && minutes < s.endMin).length;
}

describe("ScheduleBand 的时区口径", () => {
  it("账号时区 19:00 的稿，点必须落在窗口底色**内**", () => {
    render(
      <ScheduleBand
        accounts={[account()]}
        items={[item()]}
        now={new Date("2026-08-17T11:00:00.000Z")}
        day="2026-08-17"
      />,
    );

    const dot = screen.getByTestId("band-item");
    // 19:00 = 1140 分。修之前这里是 240（浏览器本地 04:00），落在两段窗口之外
    expect(dot).toHaveAttribute("data-minutes", String(19 * 60));
    expect(dot).toHaveAttribute("data-zone", "Asia/Shanghai");
    expect(windowsCovering(Number(dot.getAttribute("data-minutes")))).toBe(1);

    // 左偏移是按同一根轴算的百分比，与窗口底色同口径
    expect((dot as HTMLElement).style.left).toBe(`${((19 * 60) / 1440) * 100}%`);
    // 悬浮提示里要带上时区，别给一个没头没尾的裸时刻
    expect(dot).toHaveAttribute("title", expect.stringContaining("Asia/Shanghai"));
  });

  it("浏览器与账号不同区时，泳道上标出账号时区", () => {
    render(
      <ScheduleBand
        accounts={[account()]}
        items={[item()]}
        now={new Date("2026-08-17T11:00:00.000Z")}
        day="2026-08-17"
      />,
    );
    expect(screen.getByTestId("lane-zone")).toHaveTextContent("Asia/Shanghai");
  });

  it("账号没配时区就如实标「回退」，不静默假装算对了", () => {
    render(
      <ScheduleBand
        accounts={[account({ policy: { ...account().policy, timezone: "" } })]}
        items={[item()]}
        now={new Date("2026-08-17T11:00:00.000Z")}
        day="2026-08-17"
      />,
    );
    expect(screen.getByTestId("lane-zone")).toHaveTextContent("回退");
    // 回退到浏览器本地（LA）后，点位就是那个 04:00 —— 但界面已经说清楚它是回退来的
    expect(screen.getByTestId("band-item")).toHaveAttribute("data-minutes", String(4 * 60));
  });

  it("按账号时区筛当天：别人时区的那一天不许串到这条泳道上", () => {
    render(
      <ScheduleBand
        accounts={[account()]}
        items={[
          item(),
          // 账号时区 08-18 12:00，不属于 08-17 这一组
          item({
            id: "itm_next_day",
            scheduled_at: "2026-08-18T04:00:00.000Z",
            timeline_at: "2026-08-18T04:00:00.000Z",
          }),
        ]}
        now={new Date("2026-08-17T11:00:00.000Z")}
        day="2026-08-17"
      />,
    );
    const dots = screen.getAllByTestId("band-item");
    expect(dots).toHaveLength(1);
    expect(dots[0]).toHaveAttribute("data-minutes", String(19 * 60));
  });

  it("不传 day 时，每条泳道画的是**它自己时区的今天**", () => {
    // 此刻账号时区是 08-17 19:00，而浏览器本地（LA）是 08-17 04:00。
    // 这条稿属于账号时区的今天，必须画出来。
    render(
      <ScheduleBand accounts={[account()]} items={[item()]} now={new Date("2026-08-17T11:00:00.000Z")} />,
    );
    expect(screen.getByTestId("band-item")).toHaveAttribute("data-minutes", String(19 * 60));
  });
});
