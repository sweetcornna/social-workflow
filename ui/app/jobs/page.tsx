"use client";

import { LegacyRedirect } from "@/components/layout/legacy-redirect";

export default function LegacyJobsPage() {
  return (
    <LegacyRedirect
      to="/system/?tab=jobs"
      title="任务"
      where="渲染任务、发布记录与死信合进了「系统 · 渲染与死信」。"
    />
  );
}
