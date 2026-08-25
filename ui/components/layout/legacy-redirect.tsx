"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import * as React from "react";

import { IconArrowRight } from "@/components/icons";
import { Panel } from "@/components/ui/panel";

/**
 * 旧路径重定向。
 *
 * P9 把 9+1 页并成 5 页，旧地址（/content、/topics、/jobs、/stats、/insights）
 * 全部保留成一页薄薄的跳板：静态导出没有服务端 301，只能在客户端 `replace`。
 * 同时给一句人话说明搬到哪去了——书签失效比页面变了更让人恼火。
 */
export function LegacyRedirect({
  to,
  title,
  where,
}: {
  to: string;
  /** 旧页面叫什么。 */
  title: string;
  /** 新去处的人话描述。 */
  where: string;
}) {
  const router = useRouter();

  React.useEffect(() => {
    router.replace(to);
  }, [router, to]);

  return (
    <main className="flex min-h-dvh items-center justify-center px-4">
      <Panel variant="pop" className="w-full max-w-[440px] px-7 py-8 text-center">
        <h1 className="text-[19px] font-medium leading-tight text-fg">
          「{title}」<span className="text-primary">搬家了</span>
        </h1>
        <p className="mt-3 text-[12.5px] leading-relaxed text-fg-3">{where}</p>
        <Link
          href={to}
          className="mt-5 inline-flex items-center gap-1.5 text-[13px] text-primary hover:underline"
        >
          正在带你过去
          <IconArrowRight size={13} />
        </Link>
      </Panel>
    </main>
  );
}

export default LegacyRedirect;
