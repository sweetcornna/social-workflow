"use client";

import { LegacyRedirect } from "@/components/layout/legacy-redirect";

export default function LegacyStatsPage() {
  return (
    <LegacyRedirect
      to="/system/?tab=stats"
      title="统计"
      where="每日序列与账号表进了「系统 · 统计」，成本曲线单独成了「系统 · 成本」。"
    />
  );
}
