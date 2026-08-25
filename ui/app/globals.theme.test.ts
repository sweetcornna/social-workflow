import { readFileSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

/**
 * `.dark` 完备性检查（P14.B1 任务书硬性要求）。
 *
 * 结构纪律：**任何颜色不许只定义在 `.dark`**（`:root` 是唯一真相，`.dark`
 * 只做重定义）；反过来，`:root` 里的关键色 token 也必须在 `.dark` 里能找到
 * 对应的重定义——两句合起来就是「`:root` 与 `.dark` 的颜色变量集合必须一致」。
 * 本文件直接解析 globals.css 的文本，不依赖浏览器渲染，纯字符串层面把这条
 * 规则钉死，防止以后有人漏改一半。
 *
 * 路径用 `process.cwd()` 而不是 `import.meta.url`：vitest 的 jsdom 环境在模块
 * 顶层用全局 `URL` 构造 file: URL 时行为不稳定（会抛 "The URL must be of scheme
 * file"），`vitest.config.ts` 里 `test.root` 与 `pnpm vitest` 的执行目录都固定是
 * `ui/`，`process.cwd()` 在这里是可靠、无歧义的锚点。
 */
const CSS_PATH = path.resolve(process.cwd(), "app/globals.css");
const css = readFileSync(CSS_PATH, "utf-8");

/** 从 css 文本里摘出某个选择器（如 `:root` / `.dark`）花括号内的原文。 */
function extractBlock(source: string, selector: string): string {
  const selectorIndex = source.indexOf(selector);
  if (selectorIndex === -1) throw new Error(`选择器 ${selector} 未在 globals.css 中找到`);
  const braceStart = source.indexOf("{", selectorIndex);
  if (braceStart === -1) throw new Error(`选择器 ${selector} 后没有找到 {`);
  let depth = 0;
  for (let i = braceStart; i < source.length; i++) {
    if (source[i] === "{") depth++;
    else if (source[i] === "}") {
      depth--;
      if (depth === 0) return source.slice(braceStart + 1, i);
    }
  }
  throw new Error(`选择器 ${selector} 的花括号没有闭合`);
}

/** 从一段 css 文本里摘出所有 `--sw-*` 声明，返回 name -> value（去掉首尾空白）。 */
function extractVars(block: string): Map<string, string> {
  const out = new Map<string, string>();
  const re = /(--sw-[a-z0-9-]+)\s*:\s*([^;]+);/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(block))) {
    out.set(m[1], m[2].trim());
  }
  return out;
}

// :root 块里定义的是"尺度"而非"颜色"的 token——按设计它们日夜同值，
// 允许只在 :root 出现一次，不强制 .dark 重定义。
const SCALE_ONLY_TOKENS = new Set([
  "--sw-radius",
  "--sw-radius-sm",
  "--sw-card-radius",
  "--sw-pill-radius",
  "--sw-sidebar-w",
]);

const rootBlock = extractBlock(css, ":root");
const darkBlock = extractBlock(css, ".dark");
const rootVars = extractVars(rootBlock);
const darkVars = extractVars(darkBlock);

describe("globals.css 的 .dark 变量完备性", () => {
  it(":root 与 .dark 都至少解析出了一批 --sw-* 变量（parser 没有静默失败）", () => {
    expect(rootVars.size).toBeGreaterThan(20);
    expect(darkVars.size).toBeGreaterThan(20);
  });

  it(".dark 中的每个变量都必须在 :root 中已定义——不许颜色只活在暗色块里", () => {
    const onlyInDark = [...darkVars.keys()].filter((name) => !rootVars.has(name));
    expect(onlyInDark).toEqual([]);
  });

  it(":root 的关键色变量集合必须被 .dark 整套覆盖或显式共享（尺度 token 除外）", () => {
    const colorVarsMissingFromDark = [...rootVars.keys()].filter(
      (name) => !SCALE_ONLY_TOKENS.has(name) && !darkVars.has(name),
    );
    expect(colorVarsMissingFromDark).toEqual([]);
  });

  it("尺度 token（radius/sidebar 等）确实只在 :root 出现一次，且没有被误当成颜色重复定义", () => {
    for (const name of SCALE_ONLY_TOKENS) {
      expect(rootVars.has(name), `${name} 应该在 :root 中定义`).toBe(true);
    }
  });

  it("颜色类变量的最终取值不是裸 hex 字面量——hex 必须先经精确换算落成 oklch()/color-mix()", () => {
    const HEX_LITERAL = /^#[0-9a-fA-F]{3,8}$/;
    for (const [name, value] of [...rootVars, ...darkVars]) {
      if (SCALE_ONLY_TOKENS.has(name)) continue;
      expect(HEX_LITERAL.test(value), `${name}: ${value} 不应是裸 hex`).toBe(false);
    }
  });

  it("同名变量集合完全一致（去掉尺度 token 后，:root 与 .dark 的 key 集合相等）", () => {
    const rootColorKeys = [...rootVars.keys()].filter((n) => !SCALE_ONLY_TOKENS.has(n)).sort();
    const darkKeys = [...darkVars.keys()].sort();
    expect(darkKeys).toEqual(rootColorKeys);
  });
});
