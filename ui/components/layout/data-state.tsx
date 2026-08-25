"use client";

import * as React from "react";

import { IconAlert, IconRefresh } from "@/components/icons";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { SkeletonRows } from "@/components/ui/skeleton";
import { describeError } from "@/lib/api";

/**
 * 列表/面板的三态包装：加载中骨架、出错空态（带重试）、正常内容。
 * 页面不要自己写 `if (isLoading) ...`，统一从这里走，三态观感才一致。
 */
export function DataState({
  isLoading,
  error,
  onRetry,
  rows = 4,
  children,
}: {
  isLoading: boolean;
  error?: unknown;
  onRetry?: () => void;
  rows?: number;
  children: React.ReactNode;
}) {
  if (error) {
    return (
      <EmptyState
        // 取数失败的空态：底色从中性 muted 转陶红 tint + 左色脊，与全站告示形一致
        className="my-6 border-l-[3px] border-l-err bg-err-soft"
        icon={<IconAlert />}
        title="没能取到数据"
        description={describeError(error)}
        action={
          onRetry ? (
            <Button size="sm" onClick={onRetry}>
              <IconRefresh size={13} />
              重试
            </Button>
          ) : undefined
        }
      />
    );
  }
  if (isLoading) return <SkeletonRows rows={rows} />;
  return <>{children}</>;
}

export default DataState;
