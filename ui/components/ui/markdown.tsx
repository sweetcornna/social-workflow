"use client";

import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * 极简 markdown 渲染器。
 *
 * 复盘条目的 `markdown` 是复盘 Agent 写的原文，只用到标题 / 列表 / 粗体 /
 * 行内 code 这几种语法。为了不给工作台引一个 markdown 库（也为了不碰
 * dangerouslySetInnerHTML），这里手写解析成 React 节点——不支持的语法原样
 * 当纯文本显示，永远不会执行任何东西。
 */

function inline(text: string, keyBase: string): React.ReactNode[] {
  const nodes: React.ReactNode[] = [];
  // 粗体与行内 code 交替匹配；其余原样
  const re = /(\*\*[^*]+\*\*|`[^`]+`)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) nodes.push(text.slice(last, m.index));
    const token = m[0];
    if (token.startsWith("**")) {
      nodes.push(
        <strong key={`${keyBase}-b${i}`} className="font-medium text-fg">
          {token.slice(2, -2)}
        </strong>,
      );
    } else {
      nodes.push(
        <code
          key={`${keyBase}-c${i}`}
          className="sw-num rounded bg-muted px-1 py-[1px] text-[0.92em] text-primary"
        >
          {token.slice(1, -1)}
        </code>,
      );
    }
    last = m.index + token.length;
    i += 1;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

export function Markdown({ source, className }: { source: string; className?: string }) {
  const blocks = React.useMemo(() => {
    const lines = source.split("\n");
    const out: React.ReactNode[] = [];
    let list: string[] = [];

    const flush = () => {
      if (list.length === 0) return;
      out.push(
        <ul key={`ul-${out.length}`} className="my-1.5 ml-4 list-disc space-y-1">
          {list.map((li, i) => (
            <li key={i}>{inline(li, `li-${out.length}-${i}`)}</li>
          ))}
        </ul>,
      );
      list = [];
    };

    lines.forEach((raw, idx) => {
      const line = raw.trimEnd();
      if (/^\s*[-*]\s+/.test(line)) {
        list.push(line.replace(/^\s*[-*]\s+/, ""));
        return;
      }
      flush();
      if (!line.trim()) return;
      const h = line.match(/^(#{1,4})\s+(.*)$/);
      if (h) {
        const level = h[1].length;
        out.push(
          <p
            key={`h-${idx}`}
            className={cn(
              "mb-1.5 mt-3 first:mt-0 font-medium text-fg",
              level <= 2 ? "text-[16px]" : "text-[14px]",
            )}
          >
            {inline(h[2], `h-${idx}`)}
          </p>,
        );
        return;
      }
      out.push(
        <p key={`p-${idx}`} className="my-1.5">
          {inline(line, `p-${idx}`)}
        </p>,
      );
    });
    flush();
    return out;
  }, [source]);

  return (
    <div className={cn("text-[12.5px] leading-relaxed text-fg-2", className)}>{blocks}</div>
  );
}

export default Markdown;
