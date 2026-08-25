"use client";

import * as React from "react";

import { IconAlert } from "@/components/icons";
import type { SystemInfo } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * 发布器模式警示条。
 *
 * 只有 `use_fake_publishers === true` 时才出现，而且写成一整句人话；
 * 真实模式**什么都不显示**——常驻一个"FAKE"小角标看久了就成了装饰，
 * 真到了模拟模式反而没人注意。数据没回来（`info` 还是 undefined）时也不显示，
 * 免得闪一下一个不实的警告。
 *
 * P13 把它钉在外壳滚动口之外的一条通栏上（不再是内容区里的一张圆角卡）：
 * 它是这台实例的**全局事实**，不是某一页的内容，滚下去就看不见是错的。
 */
export function PublisherModeBanner({
  info,
  className,
}: {
  info?: SystemInfo;
  className?: string;
}) {
  if (!info?.use_fake_publishers) return null;
  return (
    <div
      role="status"
      data-testid="fake-publisher-banner"
      className={cn(
        // 通栏告示：tint 底 + 左色脊（P14.B2 全站统一的告示形），不描边。
        // 它是贴着屏幕边的一条通栏，色脊落在最左端，正好也是"这条与内容无关、
        // 属于整台实例"的位置。
        "flex items-start gap-2 border-l-[3px] border-l-warn bg-warn-soft px-4 py-2",
        "text-[12px] leading-relaxed text-fg-2",
        className,
      )}
    >
      <IconAlert size={14} className="mt-[2px] shrink-0 text-warn" />
      <span>
        <strong className="font-medium text-warn">发布器为模拟模式，不会真的发布。</strong>
        <span className="ml-1.5 text-fg-3">
          批准、排期、发布记录都会照常写库，但没有任何内容会到达平台。要真发，把 core
          的 <code className="sw-num">SW_USE_FAKE_PUBLISHERS</code> 关掉再重启。
        </span>
      </span>
    </div>
  );
}

export default PublisherModeBanner;
