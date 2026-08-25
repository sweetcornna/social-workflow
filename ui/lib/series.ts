"use client";

import * as React from "react";

/**
 * 会话内滚动序列。
 *
 * 待审 / 渲染中这类计数后端没有历史序列（只有当下值），但 stat card 底部的
 * 山丘不能凭空编数——所以就在浏览器里攒：每次轮询把新值推进定长缓冲，
 * 画的是"这次打开工作台以来的走势"。刷新页面即清零，不会骗人。
 */
export function useRollingSeries(value: number | undefined, length = 16): number[] {
  const [series, setSeries] = React.useState<number[]>([]);

  React.useEffect(() => {
    if (typeof value !== "number" || Number.isNaN(value)) return;
    setSeries((prev) => {
      if (prev.length && prev[prev.length - 1] === value) return prev;
      const next = [...prev, value];
      return next.length > length ? next.slice(next.length - length) : next;
    });
  }, [value, length]);

  if (series.length === 0) return typeof value === "number" ? [value, value] : [0, 0];
  if (series.length === 1) return [series[0], series[0]];
  return series;
}
