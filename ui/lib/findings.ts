import type { Tone } from "./format";

/**
 * 机器审核 findings 的解析。
 *
 * 后端目前只把摘要按行写进 `ContentItem.review_notes`（WORKBENCH_API.md
 * 已知限制 1：没有结构化持久化），所以 `machine_review.notes` 是一串文本行，
 * 形如 `[warn] lexicon.绝对化用语 · 命中「最轻」 · 建议：改成「更轻」`。
 * 这里只做**展示层**的分级拆解，不做任何语义推断——拆不出等级的行原样保留。
 */

export type FindingLevel = "block" | "warn" | "info" | "note";

export interface Finding {
  level: FindingLevel;
  /** 规则名（`lexicon.绝对化用语`），拆不出就是空串。 */
  rule: string;
  text: string;
}

const LEVEL_RE = /^\[(block|warn|info)\]\s*/i;

export function parseFindings(notes: string[] | null | undefined): Finding[] {
  if (!notes || notes.length === 0) return [];
  return notes
    .map((raw) => raw.trim())
    .filter(Boolean)
    .map((raw) => {
      const m = raw.match(LEVEL_RE);
      const level = (m ? m[1].toLowerCase() : "note") as FindingLevel;
      const body = m ? raw.slice(m[0].length) : raw;
      const dot = body.indexOf(" · ");
      const rule = dot > 0 && level !== "note" ? body.slice(0, dot) : "";
      const text = rule ? body.slice(dot + 3) : body;
      return { level, rule, text };
    });
}

export const FINDING_LABEL: Record<FindingLevel, string> = {
  block: "阻断",
  warn: "警告",
  info: "提示",
  note: "摘要",
};

export const FINDING_TONE: Record<FindingLevel, Tone> = {
  block: "err",
  warn: "warn",
  info: "amber",
  note: "muted",
};
