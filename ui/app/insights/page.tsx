"use client";

import { LegacyRedirect } from "@/components/layout/legacy-redirect";

export default function LegacyInsightsPage() {
  return (
    <LegacyRedirect
      to="/system/?tab=insights"
      title="复盘"
      where="按账号的运营结论进了「系统 · 复盘」。"
    />
  );
}
