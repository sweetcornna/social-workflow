import { describe, expect, it } from "vitest";

import {
  browserTimeZone,
  formatWallInput,
  isValidZone,
  offsetMsInZone,
  parseWall,
  partsInZone,
  resolveZone,
  wallTimeToUtc,
} from "./tz";

/**
 * 时区口径的底座测试。
 *
 * 第一条用例是**元测试**：它盯的不是代码，是测试环境本身。
 */

const SH = "Asia/Shanghai";
const LA = "America/Los_Angeles";
const LDN = "Europe/London";

/** 现场那条稿：账号时区 Asia/Shanghai 的 2026-08-17 19:00（生产截图上画在了 04:00）。 */
const PROD_ISO = "2026-08-17T11:00:00.000Z";

/** 同一天的午间槽：账号时区 12:00，在 UTC-7 上会退到**前一天** 21:00。 */
const NOON_ISO = "2026-08-17T04:00:00.000Z";

describe("测试环境的时区（元测试）", () => {
  it("绝不能跑在 Asia/Shanghai —— 那会让整类时区缺陷重新隐形", () => {
    // 这条用例是 P11.1 的护栏本身。P11 那个"账号时区 19:00 画在 04:00"的缺陷
    // 带着 49 vitest + 41 playwright 全绿进了生产，唯一原因就是开发机与 CI 都在
    // Asia/Shanghai：浏览器本地时区 == 账号时区，两套口径重合，什么都测不出来。
    // 谁要是把 vitest.config.ts 的 `env.TZ` 删了或改回 Asia/Shanghai，这里先红。
    expect(process.env.TZ).toBe(LA);
    expect(browserTimeZone()).toBe(LA);
    expect(browserTimeZone()).not.toBe(SH);
  });

  it("环境确实会把账号时区的时刻读错 —— 缺陷的复现条件在", () => {
    // 账号时区 19:00 那一瞬间，用 Date 的本地方法（P11 之前的写法）读出来是 04:00。
    // 这一行就是生产截图上那个"橙点画在 04:00、落在窗口底色外面"。
    expect(new Date(PROD_ISO).getHours()).toBe(4);
    // 午间那个槽更狠：钟点错了不算，**日期还退了一天**，整行会被分到错误的那一组
    const noon = new Date(NOON_ISO);
    expect(noon.getHours()).toBe(21);
    expect(noon.getDate()).toBe(16);
  });
});

describe("resolveZone", () => {
  it("合法时区原样返回，不算回退", () => {
    expect(resolveZone(SH)).toEqual({ zone: SH, fallback: false });
    expect(resolveZone(LDN)).toEqual({ zone: LDN, fallback: false });
  });

  it("空 / 非法一律回退浏览器本地，并**如实报告回退了**", () => {
    // fallback 必须是 true：UI 拿它决定要不要标注"这是回退来的"。
    // 静默假装算对了，就是把 P11 的缺陷换个地方再犯一次。
    for (const bad of ["", "   ", null, undefined, "Mars/Olympus_Mons", "UTC+8"]) {
      expect(resolveZone(bad)).toEqual({ zone: LA, fallback: true });
    }
  });

  it("isValidZone 认 IANA 名，不认偏移量写法", () => {
    expect(isValidZone(SH)).toBe(true);
    expect(isValidZone("UTC")).toBe(true);
    expect(isValidZone("Asia/Shangbai")).toBe(false);
    expect(isValidZone("")).toBe(false);
  });
});

describe("partsInZone", () => {
  it("同一瞬间在三个时区读出三套墙上时间", () => {
    const d = new Date(PROD_ISO);
    expect(partsInZone(d, SH)).toMatchObject({ year: 2026, month: 8, day: 17, hour: 19, minute: 0 });
    // 同一瞬间，UTC-7 的浏览器读出来是 04:00 —— 这就是生产截图上那个错位
    expect(partsInZone(d, LA)).toMatchObject({ year: 2026, month: 8, day: 17, hour: 4, minute: 0 });
    expect(partsInZone(d, LDN)).toMatchObject({ year: 2026, month: 8, day: 17, hour: 12, minute: 0 });
  });

  it("午间槽在 UTC-7 上连日期都退一天", () => {
    const d = new Date(NOON_ISO);
    expect(partsInZone(d, SH)).toMatchObject({ day: 17, hour: 12, minute: 0 });
    expect(partsInZone(d, LA)).toMatchObject({ day: 16, hour: 21, minute: 0 });
  });

  it("午夜是 00 不是 24（hourCycle h23）", () => {
    // hour12:false 在部分 ICU 版本上把午夜格式化成 "24"，那会让分钟偏移算成 1440
    const midnight = new Date("2026-08-17T16:00:00Z"); // = 次日 00:00 Asia/Shanghai
    expect(partsInZone(midnight, SH)).toMatchObject({ day: 18, hour: 0, minute: 0 });
  });
});

describe("offsetMsInZone", () => {
  const H = 3_600_000;

  it("常年不变的时区", () => {
    expect(offsetMsInZone(new Date(PROD_ISO), SH)).toBe(8 * H);
    expect(offsetMsInZone(new Date(PROD_ISO), "UTC")).toBe(0);
  });

  it("有夏令时的时区按瞬间给偏移，不是一个常数", () => {
    // 2026-03-08 10:00Z 是 LA 的春季切换点：之前 PST(-8)，之后 PDT(-7)
    expect(offsetMsInZone(new Date("2026-03-08T09:59:00Z"), LA)).toBe(-8 * H);
    expect(offsetMsInZone(new Date("2026-03-08T10:00:00Z"), LA)).toBe(-7 * H);
    // 2026-11-01 09:00Z 是秋季切换点：之前 PDT(-7)，之后 PST(-8)
    expect(offsetMsInZone(new Date("2026-11-01T08:59:00Z"), LA)).toBe(-7 * H);
    expect(offsetMsInZone(new Date("2026-11-01T09:00:00Z"), LA)).toBe(-8 * H);
    // 伦敦夏令时同理
    expect(offsetMsInZone(new Date("2026-08-17T11:00:00Z"), LDN)).toBe(1 * H);
    expect(offsetMsInZone(new Date("2026-01-17T11:00:00Z"), LDN)).toBe(0);
  });
});

describe("wallTimeToUtc —— 墙上时间反算回 UTC", () => {
  const wall = (year: number, month: number, day: number, hour: number, minute = 0) => ({
    year,
    month,
    day,
    hour,
    minute,
    second: 0,
  });

  it("账号时区的 19:00 反算成那一瞬间", () => {
    expect(wallTimeToUtc(wall(2026, 8, 17, 19), SH).toISOString()).toBe(PROD_ISO);
    // 同一串钟点，换个时区就是完全不同的瞬间——这正是写路径必须显式传 tz 的原因
    expect(wallTimeToUtc(wall(2026, 8, 17, 19), LA).toISOString()).toBe("2026-08-18T02:00:00.000Z");
    expect(wallTimeToUtc(wall(2026, 8, 17, 19), LDN).toISOString()).toBe("2026-08-17T18:00:00.000Z");
  });

  it("跨零点：账号时区次日 00:30", () => {
    expect(wallTimeToUtc(wall(2026, 8, 18, 0, 30), SH).toISOString()).toBe(
      "2026-08-17T16:30:00.000Z",
    );
  });

  it("DST 春季前跳：不存在的 02:30 顺跳到 03:30，不抛错也不静默给个错值", () => {
    // LA 2026-03-08 的 02:00–03:00 在挂钟上根本不存在
    const t = wallTimeToUtc(wall(2026, 3, 8, 2, 30), LA);
    expect(t.toISOString()).toBe("2026-03-08T10:30:00.000Z");
    // 顺跳过去 = 落在 03:30 PDT，与 Date 及主流日期库同惯例
    expect(partsInZone(t, LA)).toMatchObject({ day: 8, hour: 3, minute: 30 });
  });

  it("DST 春季前跳：切换后的 03:30 要走两步修正才算得对", () => {
    // 第一步猜出来的偏移是切换前的 -8h，落点跑到 04:30；必须用第二步的 -7h 重算
    const t = wallTimeToUtc(wall(2026, 3, 8, 3, 30), LA);
    expect(t.toISOString()).toBe("2026-03-08T10:30:00.000Z");
    expect(partsInZone(t, LA)).toMatchObject({ day: 8, hour: 3, minute: 30 });
  });

  it("DST 秋季回拨：重复出现的 01:30 取先发生的那次（仍在夏令时内）", () => {
    // LA 2026-11-01 的 01:00–02:00 会走两遍：08:30Z(PDT) 与 09:30Z(PST)
    const t = wallTimeToUtc(wall(2026, 11, 1, 1, 30), LA);
    expect(t.toISOString()).toBe("2026-11-01T08:30:00.000Z");
    expect(partsInZone(t, LA)).toMatchObject({ day: 1, hour: 1, minute: 30 });
    // 后发生的那次确实也存在，只是我们不选它——口径要确定，不能随机
    expect(partsInZone(new Date("2026-11-01T09:30:00.000Z"), LA)).toMatchObject({
      hour: 1,
      minute: 30,
    });
  });

  it("往返：墙上时间 → 瞬间 → 墙上时间，在 DST 切换日附近也不走样", () => {
    const zones = [SH, LA, LDN];
    const walls = [
      wall(2026, 3, 8, 1, 30),
      wall(2026, 3, 8, 12, 0),
      wall(2026, 3, 29, 3, 0),
      wall(2026, 8, 17, 19, 0),
      wall(2026, 10, 25, 1, 30),
      wall(2026, 11, 1, 1, 30),
      wall(2026, 11, 1, 23, 59),
      wall(2026, 12, 31, 0, 0),
    ];
    for (const zone of zones) {
      for (const w of walls) {
        const back = partsInZone(wallTimeToUtc(w, zone), zone);
        // 只有"这个墙上时间在该时区不存在"时才允许不等（春季前跳的那一小时）
        const same =
          back.year === w.year &&
          back.month === w.month &&
          back.day === w.day &&
          back.hour === w.hour &&
          back.minute === w.minute;
        if (!same) {
          expect(`${zone} ${formatWallInput(w)}`).toBe("America/Los_Angeles 2026-03-08T02:30");
        }
      }
    }
  });
});

describe("parseWall / formatWallInput", () => {
  it("认 datetime-local 的两种写法，多余的秒也收得下", () => {
    expect(parseWall("2026-08-17T19:00")).toMatchObject({ year: 2026, month: 8, day: 17, hour: 19 });
    expect(parseWall("2026-08-17T19:00:30")).toMatchObject({ second: 30 });
    expect(parseWall("2026-08-17 19:00")).toMatchObject({ hour: 19 });
  });

  it("拆不动就是 null，不猜", () => {
    for (const bad of ["", null, undefined, "2026-08-17", "不是时间", "2026-13-17T19:00", "2026-08-17T25:00"]) {
      expect(parseWall(bad)).toBeNull();
    }
  });

  it("格式化补零，直接能喂回 input", () => {
    expect(formatWallInput({ year: 2026, month: 8, day: 7, hour: 9, minute: 5, second: 0 })).toBe(
      "2026-08-07T09:05",
    );
  });
});
