import { describe, expect, it } from "vitest";

import { parseFindings } from "./findings";

describe("机器审核 findings 解析", () => {
  it("按 [level] 前缀分级，拆出规则名与正文", () => {
    const out = parseFindings([
      "机器审核通过：block 0 / warn 1 / info 0",
      "[warn] lexicon.绝对化用语 · 命中「最轻」 · 建议：改成「更轻」",
      "[block] precheck.封面缺失 · 小红书至少要一张卡片",
    ]);

    expect(out).toHaveLength(3);
    expect(out[0]).toMatchObject({ level: "note", rule: "" });
    expect(out[1]).toMatchObject({ level: "warn", rule: "lexicon.绝对化用语" });
    expect(out[1].text).toContain("命中「最轻」");
    expect(out[2]).toMatchObject({ level: "block", rule: "precheck.封面缺失" });
  });

  it("空输入与空行不产生条目", () => {
    expect(parseFindings(null)).toEqual([]);
    expect(parseFindings([])).toEqual([]);
    expect(parseFindings(["", "   "])).toEqual([]);
  });
});
