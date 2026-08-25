import { describe, expect, it } from "vitest";

import {
  fmtClock,
  fmtCost,
  fmtDuration,
  fmtFullTime,
  fmtTime,
  fromLocalInputValue,
  toLocalInputValue,
  toneForStatus,
  zoneDiffers,
  zoneNote,
} from "./format";

const SH = "Asia/Shanghai";
const LA = "America/Los_Angeles";
const LDN = "Europe/London";

/** 生产现场那条稿：账号时区 Asia/Shanghai 的 2026-08-17 19:00。 */
const PROD_ISO = "2026-08-17T11:00:00.000Z";

describe("展示层格式化", () => {
  it("成本只显示量，绝不出现货币符号", () => {
    const text = fmtCost({ tokens: 20532, render_seconds: 42 });
    expect(text).toContain("tokens");
    expect(text).toContain("渲染秒");
    expect(text).not.toMatch(/[¥$]/);
    expect(fmtCost({})).toBe("—");
  });

  it("状态色按语义分档，dead_letter 必须是 err", () => {
    expect(toneForStatus("dead_letter")).toBe("err");
    expect(toneForStatus("published")).toBe("ok");
    expect(toneForStatus("scheduled")).toBe("amber");
    expect(toneForStatus("draft")).toBe("muted");
  });

  it("datetime-local 的串按浏览器本地时区转 UTC ISO（不传 tz 时）", () => {
    const iso = fromLocalInputValue("2026-08-17T09:30");
    expect(iso).toMatch(/Z$/);
    expect(new Date(iso!).getHours()).toBe(9);
    expect(fromLocalInputValue("")).toBeNull();
  });

  it("时长按量级换单位", () => {
    expect(fmtDuration(30_000)).toBe("30 秒");
    expect(fmtDuration(5 * 60_000)).toBe("5 分钟");
    expect(fmtDuration(3 * 3_600_000)).toContain("小时");
  });
});

describe("读路径：时刻按账号时区显示", () => {
  it("账号时区 19:00 的稿，在 UTC-7 的浏览器上也必须显示 19:00", () => {
    // 测试进程钉在 America/Los_Angeles，不传 tz 就会显示 04:00 —— 那正是生产上的错
    expect(fmtClock(PROD_ISO, SH)).toBe("19:00");
    expect(fmtTime(PROD_ISO, SH)).toContain("19:00");
    expect(fmtTime(PROD_ISO, SH)).toContain("08");
    expect(fmtTime(PROD_ISO, SH)).toContain("17");
    expect(fmtFullTime(PROD_ISO, SH)).toContain("19:00:00");
  });

  it("不传 tz = 浏览器本地，机器时间戳走的就是这条", () => {
    expect(fmtClock(PROD_ISO)).toBe("04:00");
    expect(fmtClock(PROD_ISO, LDN)).toBe("12:00");
  });

  it("fmtClock 直接取时分，不靠切字符串", () => {
    // 以前是 fmtTime(...).slice(-5)，补上时区后缀或换 locale 就会切出别的东西
    expect(fmtClock("2026-08-17T16:30:00.000Z", SH)).toBe("00:30");
    expect(fmtClock(null, SH)).toBe("—");
    expect(fmtClock("不是时间", SH)).toBe("—");
  });

  it("tz 非法就回退浏览器本地，并让 zoneNote 把这件事说出来", () => {
    expect(fmtClock(PROD_ISO, "Mars/Olympus_Mons")).toBe("04:00");
    expect(zoneNote("Mars/Olympus_Mons")).toContain("账号没配时区");
    expect(zoneNote("")).toContain("账号没配时区");
  });

  it("与浏览器同区就不标注（同区还标一遍纯属噪音）", () => {
    expect(zoneNote(LA)).toBe("");
    expect(zoneDiffers(LA)).toBe(false);
    expect(zoneNote(SH)).toBe(SH);
    expect(zoneDiffers(SH)).toBe(true);
  });
});

describe("写路径：datetime-local 按账号时区读写", () => {
  it("填进去的是账号时区的钟点，提交出去的是对应的那一瞬间", () => {
    // 运营在 UTC-7 的浏览器前，想把稿排到账号时区 19:00
    expect(fromLocalInputValue("2026-08-17T19:00", SH)).toBe(PROD_ISO);
    // 修之前提交出去的是这个 —— 账号时区的**次日 02:00**，落在窗口外，后端 422
    expect(fromLocalInputValue("2026-08-17T19:00", LA)).toBe("2026-08-18T02:00:00.000Z");
  });

  it("往返不走样：ISO → 输入框 → ISO", () => {
    for (const tz of [SH, LA, LDN]) {
      const shown = toLocalInputValue(PROD_ISO, tz);
      expect(fromLocalInputValue(shown, tz)).toBe(PROD_ISO);
    }
    // 账号时区那一栏显示的就是 19:00，不是 04:00
    expect(toLocalInputValue(PROD_ISO, SH)).toBe("2026-08-17T19:00");
    expect(toLocalInputValue(PROD_ISO, LA)).toBe("2026-08-17T04:00");
  });

  it("DST 秋季回拨日：填 01:30 取先发生的那次，往返回来还是 01:30", () => {
    const iso = fromLocalInputValue("2026-11-01T01:30", LA);
    expect(iso).toBe("2026-11-01T08:30:00.000Z");
    expect(toLocalInputValue(iso, LA)).toBe("2026-11-01T01:30");
  });

  it("DST 春季前跳日：02:30 在 LA 根本不存在，顺跳到 03:30 而不是给个错值", () => {
    const iso = fromLocalInputValue("2026-03-08T02:30", LA);
    expect(iso).toBe("2026-03-08T10:30:00.000Z");
    // 这是唯一一处往返不等的情形，且是**如实**的：那个钟点当天没有发生过
    expect(toLocalInputValue(iso, LA)).toBe("2026-03-08T03:30");
    // 同一串钟点在没有夏令时的账号时区里完全正常
    expect(toLocalInputValue(fromLocalInputValue("2026-03-08T02:30", SH), SH)).toBe(
      "2026-03-08T02:30",
    );
  });

  it("拆不动的输入就是 null，不猜一个时刻提交出去", () => {
    expect(fromLocalInputValue("", SH)).toBeNull();
    expect(fromLocalInputValue("不是时间", SH)).toBeNull();
    expect(fromLocalInputValue("2026-08-17", SH)).toBeNull();
  });
});
