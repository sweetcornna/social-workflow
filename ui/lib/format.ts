/**
 * 展示层格式化。
 *
 * 三条纪律：
 *  1. 时间一律 UTC ISO8601 进来。**跟排期有关的时刻（几点发）按账号时区显示**，
 *     调用时显式传 `tz`；机器时间戳（巡检于、上次写盘）才按浏览器本地时区。
 *  2. `slot_text`、`已排期至 …（Asia/Shanghai）` 是后端算好的人话，**原样显示**，
 *     不要在前端重算——重算就会再引入一次口径分叉。
 *  3. 成本单位是 token 数与渲染秒数，**不是钱**（WORKBENCH_API.md 已知限制 5），
 *     所以任何地方都不许出现 ¥ / $。
 */

import {
  browserTimeZone,
  formatWallInput,
  parseWall,
  partsInZone,
  resolveZone,
  toDate,
  wallTimeToUtc,
} from "./tz";
import type { Platform } from "./types";

export const PLATFORM_LABEL: Record<Platform, string> = {
  wechat_mp: "公众号",
  xhs: "小红书",
  douyin: "抖音",
};

export const STATUS_LABEL: Record<string, string> = {
  topic: "选题",
  drafting: "生成中",
  draft: "草稿",
  reviewing: "审核中",
  rejected: "已驳回",
  approved: "已批准",
  scheduled: "已排期",
  suspended: "已挂起",
  publishing: "发布中",
  published: "已发布",
  measured: "已回收",
  publish_failed: "发布失败",
  retrying: "重试中",
  dead_letter: "死信",
};

export const ACCOUNT_STATUS_LABEL: Record<string, string> = {
  ok: "正常",
  degraded: "降级",
  needs_relogin: "需重登",
  banned: "已封禁",
  suspended: "已停用",
};

/**
 * 账号状态 → 一句"这意味着什么"。
 *
 * 三种不正常长得很像但处置完全不同，卡片上必须分开说：
 * needs_relogin 要人去掏手机扫码，degraded 是那台机器上的容器/上传器不见了，
 * suspended 是人自己关的。
 */
export const ACCOUNT_STATUS_MEANING: Record<string, string> = {
  ok: "登录态正常，按窗口正常出稿与发布。",
  degraded:
    "无法连接 sidecar / 上传器。内容生成不受影响，发布会失败。请检查对应机器。",
  needs_relogin: "登录态失效。需要用手机重新扫码登录。期间排期会被挂起。",
  banned: "平台侧封禁。需要人工确认后才能解除，系统不会自动恢复。",
  suspended: "账号已手动停用。不出稿也不发布，历史记录保留。",
};

export const ACTION_LABEL: Record<string, string> = {
  approve: "批准",
  reject: "驳回",
  edit: "改稿",
  schedule: "排期",
  machine_review: "机器审核",
  publish: "发布",
  publish_failed: "发布失败",
  dead_letter: "转死信",
  suspend: "挂起",
  resume: "恢复",
  reconciled: "对账",
  requeue: "复投",
  in_flight: "投递中",
  done: "已完成",
  failed: "失败",
};

export const RENDER_STATE_LABEL: Record<string, string> = {
  pending: "排队",
  running: "渲染中",
  done: "完成",
  failed: "失败",
  lost: "丢失",
};

/** 语义色：ok / warn / err / muted。三色沿用 brand-spec 的 oklch 值。 */
export type Tone = "ok" | "warn" | "err" | "muted" | "amber";

export function toneForStatus(status: string): Tone {
  if (
    status === "dead_letter" ||
    status === "publish_failed" ||
    status === "rejected"
  )
    return "err";
  if (status === "retrying" || status === "suspended" || status === "reviewing")
    return "warn";
  if (status === "published" || status === "measured") return "ok";
  if (
    status === "scheduled" ||
    status === "approved" ||
    status === "publishing"
  )
    return "amber";
  return "muted";
}

export function toneForAccount(status: string): Tone {
  if (status === "banned") return "err";
  if (status === "needs_relogin") return "err";
  if (status === "degraded") return "warn";
  if (status === "ok") return "ok";
  // suspended 是人自己关的，不该染成告警色吓人
  return "muted";
}

export function toneForCheck(status: string): Tone {
  if (status === "FAIL") return "err";
  if (status === "WARN") return "warn";
  if (status === "OK") return "ok";
  return "muted";
}

// ------------------------------------------------------------------ 时间

/**
 * 时刻格式化都收口在这里，`tz` 语义统一：
 *
 *  - **传了** IANA 时区 → 按那个时区显示。排期相关的时刻（几点发）一律传**账号时区**，
 *    否则浏览器在 UTC-7 时账号时区 19:00 会显示成 04:00（P11 的生产缺陷）。
 *  - **没传** → 浏览器本地时区。这是给机器时间戳用的（巡检于、上次写盘、事件时间）——
 *    那些是"这台机器什么时候干了什么"，跟账号排期无关，按看的人所在时区读才对。
 *  - 传了但**空/非法** → 回退浏览器本地并由调用方在界面上标注（`zoneNote`）。
 */
const DATETIME_OPTS: Intl.DateTimeFormatOptions = {
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
};

const FULL_OPTS: Intl.DateTimeFormatOptions = {
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
};

const FORMATTERS = new Map<string, Intl.DateTimeFormat>();

function formatIn(
  iso: string | null | undefined,
  tz: string | undefined,
  opts: Intl.DateTimeFormatOptions,
  tag: string,
): string {
  const d = toDate(iso);
  if (!d) return "—";
  // tz === undefined 是"就要浏览器本地"，与"传了个坏值"不是一回事，不走 resolveZone
  const zone = tz === undefined ? browserTimeZone() : resolveZone(tz).zone;
  const key = `${tag}|${zone}`;
  let fmt = FORMATTERS.get(key);
  if (!fmt) {
    fmt = new Intl.DateTimeFormat("zh-CN", { ...opts, timeZone: zone });
    FORMATTERS.set(key, fmt);
  }
  return fmt.format(d);
}

/** `08-17 19:00`。排期时刻请传账号时区。 */
export function fmtTime(iso: string | null | undefined, tz?: string): string {
  return formatIn(iso, tz, DATETIME_OPTS, "dt");
}

export function fmtFullTime(
  iso: string | null | undefined,
  tz?: string,
): string {
  return formatIn(iso, tz, FULL_OPTS, "full");
}

/**
 * 只要钟点 `19:00`。
 *
 * 时间线那一列以前写的是 `fmtTime(...).slice(-5)`——靠字符串末尾切，
 * 换个 locale 或补上时区后缀就会切出别的东西。这里直接用 `formatToParts` 取时分。
 */
export function fmtClock(iso: string | null | undefined, tz?: string): string {
  const d = toDate(iso);
  if (!d) return "—";
  const zone = tz === undefined ? browserTimeZone() : resolveZone(tz).zone;
  const w = partsInZone(d, zone);
  return `${String(w.hour).padStart(2, "0")}:${String(w.minute).padStart(2, "0")}`;
}

/**
 * 时刻旁边那句"这是哪个时区的"。
 *
 * 与浏览器同区就返回空串——同区时标一遍纯属噪音；不同区（或账号时区没配、
 * 回退到了浏览器本地）才说话，语言沿用时间带上"窗口按账号时区"的角标。
 */
export function zoneNote(tz: string | null | undefined): string {
  const { zone, fallback } = resolveZone(tz);
  if (fallback) return `${zone}（账号没配时区，按你的浏览器时区显示）`;
  return zone === browserTimeZone() ? "" : zone;
}

/** 浏览器时区与账号时区是不是两回事。不同才值得在界面上提一句。 */
export function zoneDiffers(tz: string | null | undefined): boolean {
  const { zone, fallback } = resolveZone(tz);
  return fallback || zone !== browserTimeZone();
}

export { browserTimeZone } from "./tz";

/** 相对时长，用于"等待了多久"。 */
export function fmtSince(
  iso: string | null | undefined,
  now: number = Date.now(),
): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
  const diff = Math.max(0, now - t);
  return fmtDuration(diff);
}

export function fmtDuration(ms: number): string {
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s} 秒`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} 分钟`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} 小时 ${m % 60} 分`;
  const d = Math.floor(h / 24);
  return `${d} 天 ${h % 24} 小时`;
}

/**
 * 「还剩多久」。与 `fmtDuration` 的区别只有一处：整点不拖一条 `0 分` 的尾巴
 * （`23 小时`，不是 `23 小时 0 分`）。
 *
 * 与后端 `core.confirm.humanize_delta` **逐字对齐**：同一条内容的余量在 Telegram
 * 卡片上和工作台上必须是同一句话，两处措辞不一样会让人以为是两回事。
 */
export function fmtRemaining(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(total / 60);
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  if (h && m) return `${h} 小时 ${m} 分`;
  if (h) return `${h} 小时`;
  return `${m} 分钟`;
}

/** `YYYY-MM-DD` 日期串 → `MM-DD`（这是 UTC 切日的日期，不做时区换算）。 */
export function fmtDay(day: string): string {
  return day.length >= 10 ? day.slice(5) : day;
}

/**
 * UTC ISO → `<input type="datetime-local">` 的值，**按 `tz` 这个时区的钟点**。
 *
 * 写路径是这次最要命的一处：`<input type="datetime-local">` 本身不带时区，
 * 它显示什么、解析出什么，全看我们喂进去/读出来的那个串代表哪个时区的钟点。
 * 以前用的是浏览器本地时区，于是运营在 UTC-7 看到的是 04:00、想改成 19:00
 * 填下去，提交出去的却是账号时区的次日 02:00——要么被后端 422 `invalid_slot` 挡回，
 * 要么排到一个合法但根本不是他要的时刻。
 */
export function toLocalInputValue(
  iso: string | null | undefined,
  tz?: string,
): string {
  const d = toDate(iso) ?? new Date();
  const zone = tz === undefined ? browserTimeZone() : resolveZone(tz).zone;
  return formatWallInput(partsInZone(d, zone));
}

/**
 * `<input type="datetime-local">` 的值 → 带 Z 的 UTC ISO8601，
 * 把它当作 **`tz` 这个时区的墙上时间**来反算。
 *
 * 夏令时由 `wallTimeToUtc` 的两步法处理（不存在的时刻顺跳、重复的时刻取先发生那次），
 * 见 `lib/tz.ts`，那两种歧义都有单独的测试钉着。
 */
export function fromLocalInputValue(value: string, tz?: string): string | null {
  const wall = parseWall(value);
  if (!wall) return null;
  const zone = tz === undefined ? browserTimeZone() : resolveZone(tz).zone;
  return wallTimeToUtc(wall, zone).toISOString();
}

// ------------------------------------------------------------------ 数字

const NUM = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 });

export function fmtNum(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return NUM.format(n);
}

/** 大数缩写：12345 → 1.2万。给热度、token 数用。 */
export function fmtCompact(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  if (Math.abs(n) < 10_000) return NUM.format(Math.round(n));
  if (Math.abs(n) < 100_000_000) return `${(n / 10_000).toFixed(1)}万`;
  return `${(n / 100_000_000).toFixed(2)}亿`;
}

export function fmtPercent(used: number, limit: number): number {
  if (!limit || limit <= 0) return 0;
  return Math.min(100, Math.max(0, (used / limit) * 100));
}

/** 成本字典 → "tokens 20,532 · 渲染 42s"。单位是量，不是钱。 */
export const COST_LABEL: Record<string, string> = {
  tokens: "tokens",
  render_seconds: "渲染秒",
};

export function fmtCost(
  cost: Record<string, number> | null | undefined,
): string {
  if (!cost) return "—";
  const parts = Object.entries(cost)
    .filter(([, v]) => typeof v === "number" && v > 0)
    .map(([k, v]) => `${COST_LABEL[k] ?? k} ${fmtCompact(v)}`);
  return parts.length ? parts.join(" · ") : "—";
}

export function truncate(text: string, max = 80): string {
  if (!text) return "";
  return text.length <= max ? text : `${text.slice(0, max)}…`;
}
