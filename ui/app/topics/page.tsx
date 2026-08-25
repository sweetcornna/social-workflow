"use client";

import { LegacyRedirect } from "@/components/layout/legacy-redirect";

export default function LegacyTopicsPage() {
  return (
    <LegacyRedirect
      to="/"
      title="选题"
      where="选题不再是一级页——它是「今天写什么」的输入，收进了「今日」页底部的选题池折叠区。"
    />
  );
}
