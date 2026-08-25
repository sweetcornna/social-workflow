"use client";

import { LegacyRedirect } from "@/components/layout/legacy-redirect";

export default function LegacyContentPage() {
  return (
    <LegacyRedirect
      to="/schedule/"
      title="内容与排期"
      where="现在叫「排期」：按天分组的时间线是主视图，窗口底色、已发 / 待发 / 失败三态和改期都在那里。"
    />
  );
}
