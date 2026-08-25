"use client";

import useSWR, { type SWRConfiguration, type SWRResponse } from "swr";
import { apiFetch, buildUrl, type RequestOptions } from "./api";

/** docs/WORKBENCH_API.md §14 的轮询建议，集中一处，页面别自己拍脑袋。 */
export const POLL = {
  dashboard: 5_000,
  reviewQueue: 10_000,
  loginStatus: 3_000,
  renderJobs: 5_000,
  accounts: 30_000,
  never: 0,
} as const;

/**
 * GET 一个端点。`path` 传 null 表示条件性跳过（SWR 惯例）。
 * key 用完整 URL，所以 query 变了会自动重新拉。
 */
export function useApi<T>(
  path: string | null,
  query?: RequestOptions["query"],
  config?: SWRConfiguration<T>,
): SWRResponse<T> {
  const key = path === null ? null : buildUrl(path, query);
  return useSWR<T>(key, (url: string) => apiFetch<T>(url), {
    revalidateOnFocus: false,
    shouldRetryOnError: false,
    ...config,
  });
}
