import { describe, expect, it } from "vitest";

import {
  DAY_MINUTES,
  dayKey,
  describeDrafts,
  minutesOfDay,
  parseWindows,
  quickSlots,
  toDrafts,
  todayRangeIso,
  toPayload,
  validateDrafts,
} from "./windows";

describe("parseWindows", () => {
  it("把后端的窗口文案拆成分钟区间", () => {
    expect(parseWindows("12:00-14:00、19:00-22:30")).toEqual([
      { startMin: 720, endMin: 840 },
      { startMin: 1140, endMin: 1350 },
    ]);
  });

  it("「全天」= 整天放行", () => {
    expect(parseWindows("全天")).toEqual([{ startMin: 0, endMin: DAY_MINUTES }]);
  });

  it("跨零点的窗口拆成两段，落在同一根 0–24 轴上", () => {
    expect(parseWindows("22:00-02:00")).toEqual([
      { startMin: 0, endMin: 120 },
      { startMin: 1320, endMin: DAY_MINUTES },
    ]);
  });

  it("空串与拆不出来的片段一律返回空，绝不编一段底色出来", () => {
    expect(parseWindows("")).toEqual([]);
    expect(parseWindows(null)).toEqual([]);
    expect(parseWindows("随便写点什么")).toEqual([]);
    // 合法的那半段留下，非法的那半段丢掉——不因为一个坏值就整条不画
    expect(parseWindows("09:00-11:00、坏值")).toEqual([{ startMin: 540, endMin: 660 }]);
    // 25:00 不是合法时刻
    expect(parseWindows("25:00-26:00")).toEqual([]);
  });
});

const SH = "Asia/Shanghai";
const LA = "America/Los_Angeles";
const LDN = "Europe/London";

/** 生产现场那条稿：账号时区 Asia/Shanghai 的 2026-08-17 19:00，正落在窗口 19:00-22:30 内。 */
const PROD_ISO = "2026-08-17T11:00:00.000Z";

describe("minutesOfDay / dayKey —— 一律按账号时区", () => {
  it("账号时区 19:00 的稿，在任何浏览器时区下都算 19*60 分、落在同一天", () => {
    // 这是本次缺陷的**核心回归**。测试进程本身跑在 America/Los_Angeles
    // （见 vitest.config.ts 的 env.TZ），只要函数漏传 tz 或改回用 Date 的本地方法，
    // 这里就会算出 4*60 并把日期挪走。
    expect(minutesOfDay(PROD_ISO, SH)).toBe(19 * 60);
    expect(dayKey(PROD_ISO, SH)).toBe("2026-08-17");

    // 点位落在窗口 19:00-22:30 之内 —— 生产上正是这一条不成立（点画到了底色外面）
    const [, evening] = parseWindows("12:00-14:00、19:00-22:30");
    const min = minutesOfDay(PROD_ISO, SH)!;
    expect(min).toBeGreaterThanOrEqual(evening.startMin);
    expect(min).toBeLessThan(evening.endMin);
  });

  it("同一瞬间，换个时区就是另一套读数（所以必须显式传）", () => {
    expect(minutesOfDay(PROD_ISO, LA)).toBe(4 * 60);
    expect(minutesOfDay(PROD_ISO, LDN)).toBe(12 * 60);
    expect(dayKey(PROD_ISO, LA)).toBe("2026-08-17");
    expect(dayKey(PROD_ISO, LDN)).toBe("2026-08-17");
  });

  it("跨日期：账号时区的午间槽在 UTC-7 上属于前一天", () => {
    const noon = "2026-08-17T04:00:00.000Z"; // = 12:00 Asia/Shanghai
    expect(minutesOfDay(noon, SH)).toBe(12 * 60);
    expect(dayKey(noon, SH)).toBe("2026-08-17");
    // 用浏览器时区分组会把这条整行挪到 08-16 那一组去
    expect(dayKey(noon, LA)).toBe("2026-08-16");
    expect(minutesOfDay(noon, LA)).toBe(21 * 60);
  });

  it("跨零点：账号时区次日 00:30 就是 0:30，不是 1440+30", () => {
    const iso = "2026-08-17T16:30:00.000Z"; // = 次日 00:30 Asia/Shanghai
    expect(minutesOfDay(iso, SH)).toBe(30);
    expect(dayKey(iso, SH)).toBe("2026-08-18");
  });

  it("DST 切换日：偏移变了，读数照样是墙上那个钟点", () => {
    // 春季切换后的 12:00 PDT（UTC-7）
    expect(minutesOfDay("2026-03-08T19:00:00.000Z", LA)).toBe(12 * 60);
    expect(dayKey("2026-03-08T19:00:00.000Z", LA)).toBe("2026-03-08");
    // 秋季切换前的 01:30 PDT 与切换后的 01:30 PST，钟点相同、偏移不同
    expect(minutesOfDay("2026-11-01T08:30:00.000Z", LA)).toBe(90);
    expect(minutesOfDay("2026-11-01T09:30:00.000Z", LA)).toBe(90);
    expect(dayKey("2026-11-01T09:30:00.000Z", LA)).toBe("2026-11-01");
  });

  it("tz 空 / 非法 → 回退浏览器本地时区（测试进程钉在 LA）", () => {
    expect(minutesOfDay(PROD_ISO, "")).toBe(4 * 60);
    expect(minutesOfDay(PROD_ISO, "Mars/Olympus_Mons")).toBe(4 * 60);
    expect(dayKey(PROD_ISO, null)).toBe("2026-08-17");
  });

  it("拿不到时间就是拿不到，不给默认值", () => {
    expect(minutesOfDay(null, SH)).toBeNull();
    expect(minutesOfDay("不是时间", SH)).toBeNull();
    expect(dayKey(undefined, SH)).toBe("");
    expect(dayKey("不是时间", SH)).toBe("");
  });
});

describe("todayRangeIso", () => {
  it("单个账号时区：给出那个时区今天 00:00 与次日 00:00", () => {
    const now = new Date("2026-08-17T11:00:00.000Z"); // 19:00 Asia/Shanghai
    const { from, to } = todayRangeIso(now, [SH]);
    expect(from).toBe("2026-08-16T16:00:00.000Z"); // = 08-17 00:00 Asia/Shanghai
    expect(new Date(to).getTime() - new Date(from).getTime()).toBe(DAY_MINUTES * 60_000);
  });

  it("多时区取并集：谁的今天都不许被切掉", () => {
    const now = new Date("2026-08-17T11:00:00.000Z");
    const { from, to } = todayRangeIso(now, [SH, LA]);
    // 起点是两者里更早的那个（Asia/Shanghai 的今天先开始）
    expect(from).toBe("2026-08-16T16:00:00.000Z");
    // 终点是更晚的那个（America/Los_Angeles 的今天后结束）
    expect(to).toBe("2026-08-18T07:00:00.000Z");
    // 现场那条稿（账号时区今天 19:00）必须落在区间里 —— 按浏览器本地日去捞就会漏掉它
    const t = new Date(PROD_ISO).getTime();
    expect(t).toBeGreaterThanOrEqual(new Date(from).getTime());
    expect(t).toBeLessThan(new Date(to).getTime());
  });

  it("没给时区（账号还没加载出来）就退化成浏览器本地的今天", () => {
    const now = new Date("2026-08-17T11:00:00.000Z"); // LA: 08-17 04:00
    const { from, to } = todayRangeIso(now);
    expect(from).toBe("2026-08-17T07:00:00.000Z"); // = 08-17 00:00 America/Los_Angeles
    expect(new Date(to).getTime() - new Date(from).getTime()).toBe(DAY_MINUTES * 60_000);
  });

  it("DST 切换日那一天不是 24 小时，起点仍是当地零点", () => {
    // LA 2026-03-08 只有 23 小时；起点照样是当地 00:00 = 08:00Z
    const now = new Date("2026-03-08T20:00:00.000Z");
    const { from } = todayRangeIso(now, [LA]);
    expect(from).toBe("2026-03-08T08:00:00.000Z");
  });
});

describe("toDrafts / toPayload 往返", () => {
  it("后端文案 → 编辑器段 → 提交串，一圈下来不走样", () => {
    const drafts = toDrafts("12:00-14:00、19:00-22:30");
    expect(drafts).toEqual([
      { start: "12:00", end: "14:00" },
      { start: "19:00", end: "22:30" },
    ]);
    expect(toPayload(drafts)).toEqual(["12:00-14:00", "19:00-22:30"]);
  });

  it("「全天」与空串都是「没有窗口」，不是一段 00:00-24:00", () => {
    expect(toDrafts("全天")).toEqual([]);
    expect(toDrafts("")).toEqual([]);
    expect(toPayload([])).toEqual([]);
  });

  it("只填了一半的段不提交（后端会拒，这里先拦下）", () => {
    expect(toPayload([{ start: "09:00", end: "" }])).toEqual([]);
  });
});

describe("validateDrafts", () => {
  it("合法就是空串", () => {
    expect(validateDrafts([{ start: "09:00", end: "11:00" }])).toBe("");
    // 跨零点合法，与后端 parse_window 同口径
    expect(validateDrafts([{ start: "22:00", end: "02:00" }])).toBe("");
  });

  it("起止相同 = 永不放行，直接说清楚该怎么办", () => {
    const msg = validateDrafts([{ start: "09:00", end: "09:00" }]);
    expect(msg).toContain("永不放行");
    expect(msg).toContain("全天");
  });

  it("只填一半 / 格式不对都要拦住", () => {
    expect(validateDrafts([{ start: "09:00", end: "" }])).toContain("开始和结束");
    expect(validateDrafts([{ start: "25:00", end: "26:00" }])).toContain("HH:MM");
  });
});

describe("describeDrafts", () => {
  it("没窗口就说没窗口，不含糊", () => {
    expect(describeDrafts([])).toContain("全天");
  });

  it("跨零点要显式提醒会顺延到第二天", () => {
    expect(describeDrafts([{ start: "22:00", end: "02:00" }])).toContain("跨零点");
  });
});

describe("quickSlots（P14.B4：改期弹窗的「今天/明天首窗」两枚快捷槽位）", () => {
  it("正在窗口内：今天是「现在 + 余量」，明天是第一个窗口的开始点", () => {
    // 2026-08-17T02:00:00Z = Asia/Shanghai 同日 10:00，落在 09:00-11:00 窗口内
    const now = new Date("2026-08-17T02:00:00Z");
    const slots = quickSlots("09:00-11:00、19:00-22:00", "Asia/Shanghai", now);
    expect(slots).toEqual([
      { key: "today", label: "今天 10:05", iso: "2026-08-17T02:05:00.000Z" },
      { key: "tomorrow", label: "明天 09:00", iso: "2026-08-18T01:00:00.000Z" },
    ]);
  });

  it("今天窗口已经全部过去：只剩「明天」一枚，不给一个已经过去的「今天」", () => {
    // 23:00 Shanghai，晚间窗口 19:00-22:00 已经结束
    const now = new Date("2026-08-17T15:00:00Z");
    const slots = quickSlots("09:00-11:00、19:00-22:00", "Asia/Shanghai", now);
    expect(slots.map((s) => s.key)).toEqual(["tomorrow"]);
    expect(slots[0].label).toBe("明天 09:00");
  });

  it("全天放行：今天就是「此刻 + 5 分钟余量」，明天是 00:00", () => {
    // 14:30 Shanghai
    const now = new Date("2026-08-17T06:30:00Z");
    const slots = quickSlots("全天", "Asia/Shanghai", now);
    expect(slots).toEqual([
      { key: "today", label: "今天 14:35", iso: "2026-08-17T06:35:00.000Z" },
      { key: "tomorrow", label: "明天 00:00", iso: "2026-08-17T16:00:00.000Z" },
    ]);
  });

  it("拆不出任何区间（空串 / 不认得的格式）就不给快捷槽位，不编一个默认值", () => {
    expect(quickSlots("", "Asia/Shanghai")).toEqual([]);
    expect(quickSlots(undefined, "Asia/Shanghai")).toEqual([]);
    expect(quickSlots("随便写点什么", "Asia/Shanghai")).toEqual([]);
  });
});
