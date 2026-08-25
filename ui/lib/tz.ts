/**
 * 时区口径的唯一出处。
 *
 * 排期是**账号的事**：窗口、日上限、槽位全按 `AccountPolicy.timezone` 算。
 * 浏览器在哪个时区纯属偶然，不该影响"这条稿几点发"的读数。P11 之前
 * `windows.ts` / `format.ts` 里那几个函数用的是 `Date` 的本地方法
 * （`getHours()` / `getFullYear()`），等于把浏览器时区**当成了**账号时区——
 * 开发机与 CI 都在 Asia/Shanghai，两套口径重合，49 vitest + 41 playwright
 * 全绿也没抓到；换到 UTC-7 的浏览器上，账号时区 19:00 的稿会画在 04:00。
 *
 * 所以这里定一条规矩：**凡是要落到"账号那一天的钟点"上的换算，都显式传 IANA 时区。**
 *
 * 零新依赖：`Intl.DateTimeFormat` + `formatToParts` 已经够用，不引第三方日期库。
 * 也**不手搓偏移量表**——夏令时的规则只有 ICU 数据知道，硬编码必错。
 */

/** 一个"墙上时间"：某个时区里挂钟显示的年月日时分秒，不含偏移量。 */
export interface WallTime {
  year: number;
  /** 1–12，不是 `Date` 那种 0–11。 */
  month: number;
  day: number;
  hour: number;
  minute: number;
  second: number;
}

/** `resolveZone` 的结果。`fallback` 为真时 UI 必须如实标注，不许假装算对了。 */
export interface ResolvedZone {
  zone: string;
  fallback: boolean;
}

const FORMATTERS = new Map<string, Intl.DateTimeFormat>();
const ZONE_VALID = new Map<string, boolean>();

/** 浏览器（或 node）自己的时区。拿不到就退到 UTC——总得有个能算的东西。 */
export function browserTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

/** 这个字符串是不是 ICU 认识的 IANA 时区。结果缓存，别在渲染里反复 try/catch。 */
export function isValidZone(tz: string | null | undefined): boolean {
  if (!tz) return false;
  const cached = ZONE_VALID.get(tz);
  if (cached !== undefined) return cached;
  let ok = false;
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: tz });
    ok = true;
  } catch {
    ok = false;
  }
  ZONE_VALID.set(tz, ok);
  return ok;
}

/**
 * 时区兜底。
 *
 * 账号没配时区、或配了个 ICU 不认识的串时，**回退到浏览器本地时区并把这件事说出来**
 * （`fallback: true`）。调用方有义务在界面上标注——静默假装算对了，就是把 P11 那个
 * 缺陷换个地方再犯一次。
 */
export function resolveZone(tz: string | null | undefined): ResolvedZone {
  const raw = (tz ?? "").trim();
  if (raw && isValidZone(raw)) return { zone: raw, fallback: false };
  return { zone: browserTimeZone(), fallback: true };
}

function formatterFor(zone: string): Intl.DateTimeFormat {
  const hit = FORMATTERS.get(zone);
  if (hit) return hit;
  const fmt = new Intl.DateTimeFormat("en-US", {
    timeZone: zone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    // h23 而不是 hour12:false：后者在部分 ICU 版本上把午夜格式化成 "24"
    hourCycle: "h23",
  });
  FORMATTERS.set(zone, fmt);
  return fmt;
}

/** 某个瞬间在某时区的墙上时间。`zone` 必须已经过 `resolveZone`。 */
export function partsInZone(date: Date, zone: string): WallTime {
  const parts = formatterFor(zone).formatToParts(date);
  const bag: Record<string, string> = {};
  for (const p of parts) {
    if (p.type !== "literal") bag[p.type] = p.value;
  }
  return {
    year: Number(bag.year),
    month: Number(bag.month),
    day: Number(bag.day),
    hour: Number(bag.hour),
    minute: Number(bag.minute),
    second: Number(bag.second),
  };
}

/**
 * 该瞬间该时区相对 UTC 的偏移（毫秒，东为正）。
 *
 * 做法是"把墙上时间当成 UTC 读一遍再作差"——这样夏令时切换由 ICU 负责，
 * 我们一个偏移量都不用记。
 */
export function offsetMsInZone(date: Date, zone: string): number {
  const w = partsInZone(date, zone);
  const asIfUtc = Date.UTC(w.year, w.month - 1, w.day, w.hour, w.minute, w.second);
  return asIfUtc - date.getTime();
}

function sameWall(a: WallTime, b: WallTime): boolean {
  return (
    a.year === b.year &&
    a.month === b.month &&
    a.day === b.day &&
    a.hour === b.hour &&
    a.minute === b.minute
  );
}

/**
 * 反向换算：某时区的墙上时间 → 那一瞬间（UTC）。
 *
 * **两步法**，专门为了夏令时：
 *  1. 先把墙上时间当成 UTC 得到一个猜测瞬间，用它问出一个偏移量，减掉 → 候选 A；
 *  2. 用候选 A 再问一次偏移量。若与第一次不同（说明猜测点落在切换的另一侧），
 *     按新偏移量重算 → 候选 B，谁的墙上时间对得上就要谁。
 *
 * 两个候选都对不上，说明这个墙上时间在该时区**根本不存在**（春季前跳，
 * 如 America/Los_Angeles 2026-03-08 的 02:00–03:00）。此时取候选 A，
 * 也就是"顺着跳过去"的那个瞬间（02:30 → 03:30 PDT），与 `Date` 及主流日期库同惯例。
 *
 * 秋季回拨那一小时会出现两次（LA 2026-11-01 的 01:00–02:00），本函数取**先发生的那次**
 * （仍在夏令时内）。两种歧义都有测试钉着。
 */
export function wallTimeToUtc(wall: WallTime, zone: string): Date {
  const guess = Date.UTC(wall.year, wall.month - 1, wall.day, wall.hour, wall.minute, wall.second);
  const offset1 = offsetMsInZone(new Date(guess), zone);
  const candidateA = new Date(guess - offset1);

  const offset2 = offsetMsInZone(candidateA, zone);
  if (offset2 === offset1) return candidateA;

  const candidateB = new Date(guess - offset2);
  if (sameWall(partsInZone(candidateB, zone), wall)) return candidateB;
  return candidateA;
}

const WALL_RE = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?$/;

/** `<input type="datetime-local">` 的值 → 墙上时间。拆不动就是 null，不猜。 */
export function parseWall(value: string | null | undefined): WallTime | null {
  const m = (value ?? "").trim().match(WALL_RE);
  if (!m) return null;
  const wall: WallTime = {
    year: Number(m[1]),
    month: Number(m[2]),
    day: Number(m[3]),
    hour: Number(m[4]),
    minute: Number(m[5]),
    second: Number(m[6] ?? "0"),
  };
  if (wall.month < 1 || wall.month > 12) return null;
  if (wall.day < 1 || wall.day > 31) return null;
  if (wall.hour > 23 || wall.minute > 59 || wall.second > 59) return null;
  return wall;
}

const pad2 = (n: number) => String(n).padStart(2, "0");

/** 墙上时间 → `<input type="datetime-local">` 认的 `YYYY-MM-DDTHH:MM`。 */
export function formatWallInput(w: WallTime): string {
  return `${w.year}-${pad2(w.month)}-${pad2(w.day)}T${pad2(w.hour)}:${pad2(w.minute)}`;
}

/** 墙上时间 → 日期键 `YYYY-MM-DD`。 */
export function formatWallDay(w: WallTime): string {
  return `${w.year}-${pad2(w.month)}-${pad2(w.day)}`;
}

/** 字符串 / Date → Date；拿不到有效时刻就是 null，不给默认值。 */
export function toDate(value: Date | string | null | undefined): Date | null {
  if (value === null || value === undefined || value === "") return null;
  const d = value instanceof Date ? value : new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}
