/**
 * 发布窗口文案 → 一天之内的分钟区间。
 *
 * 后端把 `AccountPolicy.publish_windows` 序列化成 `"12:00-14:00、19:00-22:30"`
 * 这样的**展示文案**（没有窗口时是 `"全天"`），JSON 里没有结构化字段
 * （见报告的契约缺口）。今日排期带要按窗口画底色，只能在展示层把它拆回来——
 * 拆不动的原样返回空数组，宁可不画底色，也不要画一条编出来的底色。
 *
 * 跨零点的窗口（`"22:00-02:00"`）拆成两段，落在同一根 0–24 时轴上。
 *
 * **这根轴是账号时区的一天**（窗口文案本来就是按 `AccountPolicy.timezone` 写的）。
 * 所以往轴上放点、按天分组，都得显式传同一个时区，见 `minutesOfDay` / `dayKey`。
 */

import {
  browserTimeZone,
  formatWallDay,
  partsInZone,
  resolveZone,
  toDate,
  wallTimeToUtc,
} from "./tz";

export interface WindowSpan {
  /** 距离 00:00 的分钟数，[0, 1440)。 */
  startMin: number;
  /** 距离 00:00 的分钟数，(0, 1440]。 */
  endMin: number;
}

export const DAY_MINUTES = 24 * 60;

const RANGE_RE = /^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$/;

function toMinutes(h: string, m: string): number | null {
  const hh = Number(h);
  const mm = Number(m);
  if (!Number.isInteger(hh) || !Number.isInteger(mm)) return null;
  if (hh < 0 || hh > 24 || mm < 0 || mm > 59) return null;
  return hh * 60 + mm;
}

/** `"全天"` / 空串 → 整天放行（`[{0, 1440}]`）；拆不出来的片段直接丢掉。 */
export function parseWindows(text: string | null | undefined): WindowSpan[] {
  const raw = (text ?? "").trim();
  if (!raw) return [];
  if (raw === "全天") return [{ startMin: 0, endMin: DAY_MINUTES }];

  const spans: WindowSpan[] = [];
  for (const part of raw.split(/[、,，]/)) {
    const m = part.trim().match(RANGE_RE);
    if (!m) continue;
    const start = toMinutes(m[1], m[2]);
    const end = toMinutes(m[3], m[4]);
    if (start === null || end === null) continue;
    if (end === start) continue;
    if (end > start) {
      spans.push({ startMin: start, endMin: Math.min(end, DAY_MINUTES) });
    } else {
      // 跨零点：22:00-02:00 → [22:00, 24:00) + [00:00, 02:00)
      spans.push({ startMin: start, endMin: DAY_MINUTES });
      spans.push({ startMin: 0, endMin: end });
    }
  }
  return spans.sort((a, b) => a.startMin - b.startMin);
}

/**
 * 该时刻在**账号时区**那一天里的分钟偏移。用于把内容点放到时间带上。
 *
 * 必须传 `tz`：窗口底色是 `parseWindows()` 从账号时区的窗口文案拆出来的，
 * 点位不按同一个时区算，就会画到底色外面去（P11 的生产缺陷）。
 * `tz` 空/非法时回退浏览器本地时区——调用方有义务在界面上标注。
 */
export function minutesOfDay(iso: string | null | undefined, tz: string | null | undefined): number | null {
  const d = toDate(iso);
  if (!d) return null;
  const w = partsInZone(d, resolveZone(tz).zone);
  return w.hour * 60 + w.minute;
}

/**
 * **账号时区**下的日期键 `YYYY-MM-DD`。按天分组与"是不是今天"都用它。
 *
 * 同一个瞬间在不同时区可能属于不同的日期——这正是要显式传 `tz` 的原因：
 * 账号时区 19:00 那条稿，在 UTC-7 的浏览器里是前一天的 04:00，
 * 用浏览器时区分组会把整行挪到错误的那一天。
 */
export function dayKey(value: Date | string | null | undefined, tz: string | null | undefined): string {
  const d = toDate(value);
  if (!d) return "";
  return formatWallDay(partsInZone(d, resolveZone(tz).zone));
}

// ------------------------------------------------------------- 可视化编辑

/** 编辑器里的一段窗口。两个 `HH:MM` 字符串，直接喂给 `<input type="time">`。 */
export interface WindowDraft {
  start: string;
  end: string;
}

const TIME_RE = /^([01]\d|2[0-3]):([0-5]\d)$/;

/** 后端的展示文案 `"12:00-14:00、19:00-22:30"` → 编辑器的可编辑段。 */
export function toDrafts(text: string | null | undefined): WindowDraft[] {
  const raw = (text ?? "").trim();
  if (!raw || raw === "全天") return [];
  const out: WindowDraft[] = [];
  for (const part of raw.split(/[、,，]/)) {
    const m = part.trim().match(RANGE_RE);
    if (!m) continue;
    out.push({ start: `${m[1].padStart(2, "0")}:${m[2]}`, end: `${m[3].padStart(2, "0")}:${m[4]}` });
  }
  return out;
}

/** 编辑器的段 → 提交给 `/accounts` 的字符串数组。空段直接丢掉。 */
export function toPayload(drafts: WindowDraft[]): string[] {
  return drafts
    .filter((d) => d.start && d.end)
    .map((d) => `${d.start}-${d.end}`);
}

/**
 * 提交前的本地校验。返回一句中文，空串表示可以提交。
 *
 * 刻意与后端 `core/accounts.py:parse_window` 同口径：起止相同 = 永不放行（后端会拒），
 * 跨零点合法。本地先拦一道纯粹是为了少一次往返，**不代替**后端校验。
 */
export function validateDrafts(drafts: WindowDraft[]): string {
  for (const d of drafts) {
    if (!d.start && !d.end) continue;
    if (!d.start || !d.end) return "每段窗口都要填开始和结束两个时间。";
    if (!TIME_RE.test(d.start) || !TIME_RE.test(d.end)) {
      return "时间要写成 24 小时制的 HH:MM，比如 09:00。";
    }
    if (d.start === d.end) {
      return `${d.start}-${d.end} 起止相同，等于永不放行；想全天放行就把窗口全删掉。`;
    }
  }
  return "";
}

/** 给人看的一句预览：`"每天 12:00-14:00、19:00-22:30 之间发"`。 */
export function describeDrafts(drafts: WindowDraft[]): string {
  const parts = toPayload(drafts);
  if (parts.length === 0) return "没设窗口 = 全天都可以发。";
  const crossing = drafts.some((d) => d.start && d.end && d.end < d.start);
  const tail = crossing ? "（有跨零点的窗口，会顺延到第二天）" : "";
  return `每天 ${parts.join("、")} 之间发${tail}`;
}

/**
 * "今天"的 UTC 区间，给 `/content?from=&to=` 用。
 *
 * `zones` 是这一屏会画到的**账号时区**集合。多个账号可以在不同时区，
 * 各自的"今天"是不同的绝对区间——这里取它们的**并集**，宁可多捞一点，
 * 也不能让浏览器时区把别人的今天切掉：浏览器在 UTC-7 时，Asia/Shanghai
 * 账号今天 19:00 的稿落在 UTC-7 的**昨天** 04:00，按浏览器本地日去捞就是空的。
 *
 * 捞回来之后由时间带按 `dayKey(iso, 账号时区)` 逐条筛，不会串台。
 * `zones` 为空（账号还没加载出来）时退化成浏览器本地的今天。
 */
export function todayRangeIso(
  now: Date = new Date(),
  zones: readonly string[] = [],
): { from: string; to: string } {
  const list = zones.length > 0 ? zones : [browserTimeZone()];
  let from = Number.POSITIVE_INFINITY;
  let to = Number.NEGATIVE_INFINITY;
  for (const tz of list) {
    const zone = resolveZone(tz).zone;
    const w = partsInZone(now, zone);
    const start = wallTimeToUtc(
      { year: w.year, month: w.month, day: w.day, hour: 0, minute: 0, second: 0 },
      zone,
    ).getTime();
    from = Math.min(from, start);
    to = Math.max(to, start + DAY_MINUTES * 60_000);
  }
  return { from: new Date(from).toISOString(), to: new Date(to).toISOString() };
}

// ------------------------------------------------------------- 改期快捷槽位

/** 改期弹窗的一枚快捷槽位（P14.B4：给最常见的"就现在/明早发"两个念头一键提交）。 */
export interface QuickSlot {
  key: "today" | "tomorrow";
  /** 「今天 19:35」/「明天 09:00」——按账号时区算出来的钟点。 */
  label: string;
  /** 直接喂给 `/content/{id}/reschedule` 的 ISO 时刻。 */
  iso: string;
}

/** 提交前留的余量：正好取"此刻"当目标，请求还没到后端就已经过去了，白白吃一次 422。 */
const SLOT_LEAD_MINUTES = 5;

const pad2 = (n: number) => String(n).padStart(2, "0");

/**
 * 从账号的 `publish_windows` 展示文案里，算出「今天首窗」与「明天首窗」两枚快捷槽位。
 *
 * 只服务"点一下就提交"这一个场景，不追求穷尽所有边界——挑到的时刻是否真的合法
 * （撞上 min_interval、当天已到日上限……）一律交给后端；不合法就走既有的
 * 422 `invalid_slot` → `suggested_slot` 兜底，不在前端重复一遍排期算法。
 *
 * `windowsText` 拆不出任何区间（空串、格式不认得）时返回空数组——宁可不给快捷按钮，
 * 也不要凭空编一个"全天"当默认（真正的"全天放行"，后端序列化成字面的 `"全天"`，
 * 会被 `parseWindows` 正确识别为整天一个大区间）。
 */
export function quickSlots(
  windowsText: string | null | undefined,
  tz: string | null | undefined,
  now: Date = new Date(),
): QuickSlot[] {
  const spans = parseWindows(windowsText);
  if (spans.length === 0) return [];

  const zone = resolveZone(tz).zone;
  const wall = partsInZone(now, zone);
  const nowMin = wall.hour * 60 + wall.minute;

  const slots: QuickSlot[] = [];

  // 今天：今天剩余窗口里最早能发的一点（正在进行中的窗口就是"现在 + 余量"）。
  // 措辞是「今天」不是「今晚」：窗口可以落在一天里的任何时段，午休窗的号点开
  // 会拿到 12:00 这个钟点，写成「今晚 12:00」是明摆着的错话（B6 走查实拍到）。
  const remaining = spans.find((s) => s.endMin > nowMin + SLOT_LEAD_MINUTES);
  if (remaining) {
    const targetMin = Math.max(nowMin + SLOT_LEAD_MINUTES, remaining.startMin);
    const hh = Math.floor(targetMin / 60);
    const mm = targetMin % 60;
    const at = wallTimeToUtc(
      { year: wall.year, month: wall.month, day: wall.day, hour: hh, minute: mm, second: 0 },
      zone,
    );
    slots.push({ key: "today", label: `今天 ${pad2(hh)}:${pad2(mm)}`, iso: at.toISOString() });
  }

  // 明天：第一个窗口的开始点。日期用纯日历算术前推一天，不掺时区偏移（避免 DST 干扰"哪一天"）
  const first = spans[0];
  const tomorrow = new Date(Date.UTC(wall.year, wall.month - 1, wall.day + 1, 12, 0, 0));
  const hh = Math.floor(first.startMin / 60);
  const mm = first.startMin % 60;
  const at = wallTimeToUtc(
    {
      year: tomorrow.getUTCFullYear(),
      month: tomorrow.getUTCMonth() + 1,
      day: tomorrow.getUTCDate(),
      hour: hh,
      minute: mm,
      second: 0,
    },
    zone,
  );
  slots.push({ key: "tomorrow", label: `明天 ${pad2(hh)}:${pad2(mm)}`, iso: at.toISOString() });

  return slots;
}
