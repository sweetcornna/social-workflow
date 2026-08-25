import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ConfirmClock, readConfirmClock, URGENT_MS } from "./confirm-clock";
import type { ContentRow } from "@/lib/types";

/**
 * 双时刻读数的文案。
 *
 * 这几句是运营每天要看的东西——改一个字都该是有意为之，所以逐句钉在这里。
 * 组件本身只是把这份读数摆到页面上，真正的判断全在 `readConfirmClock` 里。
 */

const NOW = Date.parse("2026-08-18T09:00:00Z");

function row(over: Partial<ContentRow> = {}): ContentRow {
  return {
    id: "itm_1",
    account_id: "acc-1",
    account_name: "甜玉米",
    platform: "xhs",
    title: "租房收纳的第三个坑",
    status: "scheduled",
    created_at: null,
    updated_at: null,
    scheduled_at: "2026-08-18T11:00:00Z",
    slot_text: "08-18 19:00（Asia/Shanghai）",
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
    timeline_at: null,
    confirm_required: true,
    awaiting_confirm: true,
    confirmed_at: null,
    confirm_pushed_at: "2026-08-18T08:00:00Z",
    confirm_deadline: "2026-08-19T08:00:00Z",
    ...over,
  } as ContentRow;
}

describe("readConfirmClock", () => {
  it("上行是账号时区的发布钟点，不是浏览器时区的", () => {
    // 浏览器被钉在 America/Los_Angeles（见 vitest.config.ts），
    // 11:00Z 在那边是 04:00，在 Asia/Shanghai 是 19:00
    expect(readConfirmClock(row(), NOW, "Asia/Shanghai").slot).toBe("19:00");
    expect(readConfirmClock(row(), NOW, undefined).slot).toBe("04:00");
  });

  it("下行是递减的决定余量，不是又一个固定时刻", () => {
    const read = readConfirmClock(row(), NOW, "Asia/Shanghai");
    expect(read.line).toBe("还有 23 小时决定");
    expect(read.urgent).toBe(false);
  });

  it("余量告急时升到更重的一档", () => {
    const near = new Date(NOW + URGENT_MS - 60_000).toISOString();
    const read = readConfirmClock(
      row({ confirm_deadline: near }),
      NOW,
      "Asia/Shanghai",
    );
    expect(read.urgent).toBe(true);
    expect(read.line).toContain("还有");
  });

  it("刚过阈值那一分钟还不算告急：阈值是「要不要现在看」，不是吓人用的", () => {
    const just = new Date(NOW + URGENT_MS + 60_000).toISOString();
    expect(
      readConfirmClock(row({ confirm_deadline: just }), NOW, "Asia/Shanghai")
        .urgent,
    ).toBe(false);
  });

  it("期限已过时说清接下来会发生什么，不让人以为还能点", () => {
    const past = new Date(NOW - 60_000).toISOString();
    const read = readConfirmClock(
      row({ confirm_deadline: past }),
      NOW,
      "Asia/Shanghai",
    );
    expect(read.line).toBe("决定期限已过，下一轮会自动驳回");
    expect(read.urgent).toBe(true);
  });

  it("确认过了就换成一句确定的话，并且不再告急", () => {
    const read = readConfirmClock(
      row({ confirmed_at: "2026-08-18T08:30:00Z" }),
      NOW,
      "Asia/Shanghai",
    );
    expect(read.line).toBe("已确认，到点就发");
    expect(read.done).toBe(true);
    expect(read.urgent).toBe(false);
  });

  it("还没推过卡（没有期限）时也要说话，不留空白", () => {
    const read = readConfirmClock(
      row({ confirm_deadline: null }),
      NOW,
      "Asia/Shanghai",
    );
    expect(read.line).toBe("等你确认");
  });

  it("与浏览器同区时不标时区——同区标一遍纯属噪音", () => {
    expect(readConfirmClock(row(), NOW, "America/Los_Angeles").zone).toBe("");
    expect(readConfirmClock(row(), NOW, "Asia/Shanghai").zone).toBe(
      "Asia/Shanghai",
    );
  });
});

describe("ConfirmClock 组件的渲染门控", () => {
  // 排期页对每一行都无差别渲染 <ConfirmClock compact />。
  // `confirm_required` 只说这条内容需要人确认，不说"现在读这个钟还有没有意义"——
  // 已发布的 published 行读出"已确认，到点就发"会让人以为还没发，
  // 被 TTL 驳回的 rejected 行读出"决定期限已过"是个过期读数。两者都该钉死不渲染。

  it("scheduled 状态下正常渲染读数", () => {
    render(<ConfirmClock item={row({ status: "scheduled" })} now={NOW} tz="Asia/Shanghai" compact />);
    expect(screen.getByTestId("confirm-clock")).toBeInTheDocument();
  });

  it("published 状态不渲染——已经发出去了不该说「到点就发」", () => {
    render(<ConfirmClock item={row({ status: "published" })} now={NOW} tz="Asia/Shanghai" compact />);
    expect(screen.queryByTestId("confirm-clock")).not.toBeInTheDocument();
  });

  it("rejected 状态不渲染——TTL 自动驳回后「决定期限已过」是个过期读数", () => {
    render(<ConfirmClock item={row({ status: "rejected" })} now={NOW} tz="Asia/Shanghai" compact />);
    expect(screen.queryByTestId("confirm-clock")).not.toBeInTheDocument();
  });
});
