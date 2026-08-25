/**
 * 数据面唯一入口：envelope 解包 + Bearer 注入 + 401 跳登录。
 *
 * 契约见 docs/WORKBENCH_API.md：
 *   - 每个响应都是 `{ok, data, error}`；失败按 `error.code` 分支，不要按 message
 *   - 媒体端点（/review/{id}/media/{i} 等）**不在** /api/v1 下，也不带 token，
 *     所以 <img>/<video> 直接引用 mediaUrl()/coverUrl() 即可
 */

import type { ApiErrorBody, Envelope } from "./types";

/** FastAPI 把静态导出挂在这个前缀下；dev 服务器同样用它当 basePath。 */
export const BASE_PATH = "/workbench";
/** 数据面基址。生产与开发都是同源的绝对路径（dev 靠 next.config rewrites 代理）。 */
export const API_BASE = "/api/v1";
/** token 存放键。验证码等敏感信息一律不落本地存储（docs/POLICY.md 红线）。 */
export const TOKEN_KEY = "sw_ui_token";

export class ApiFailure extends Error {
  readonly code: string;
  readonly detail: Record<string, unknown> | null;
  readonly status: number;

  constructor(error: ApiErrorBody, status: number) {
    super(error.message || error.code);
    this.name = "ApiFailure";
    this.code = error.code;
    this.detail = error.detail ?? null;
    this.status = status;
  }
}

export function getToken(): string {
  if (typeof window === "undefined") return "";
  try {
    return window.localStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

export function setToken(token: string): void {
  if (typeof window === "undefined") return;
  try {
    if (token) window.localStorage.setItem(TOKEN_KEY, token);
    else window.localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* 隐私模式下 localStorage 会抛，忽略即可 */
  }
}

/** 401 处理器。默认跳登录页；测试里可以替换掉，避免 jsdom 报导航未实现。 */
let unauthorizedHandler: () => void = () => {
  if (typeof window === "undefined") return;
  const here = window.location.pathname;
  if (here.startsWith(`${BASE_PATH}/login`)) return;
  window.location.assign(`${BASE_PATH}/login/`);
};

export function setUnauthorizedHandler(fn: () => void): void {
  unauthorizedHandler = fn;
}

export interface RequestOptions {
  method?: string;
  body?: unknown;
  query?: Record<string, string | number | boolean | null | undefined>;
  signal?: AbortSignal;
  /** 401 时不跳登录（登录页自己的探针要用） */
  silent401?: boolean;
}

export function buildUrl(path: string, query?: RequestOptions["query"]): string {
  const base = path.startsWith("/api/") ? path : `${API_BASE}${path}`;
  if (!query) return base;
  const usp = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v === null || v === undefined || v === "") continue;
    usp.set(k, String(v));
  }
  const qs = usp.toString();
  return qs ? `${base}?${qs}` : base;
}

/** 发一个请求并把 envelope 拆开。失败一律抛 ApiFailure。 */
export async function apiFetch<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, query, signal, silent401 } = options;
  const headers: Record<string, string> = { accept: "application/json" };
  const token = getToken();
  if (token) headers.authorization = `Bearer ${token}`;
  if (body !== undefined) headers["content-type"] = "application/json";

  const resp = await fetch(buildUrl(path, query), {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
    cache: "no-store",
  });

  let envelope: Envelope<T> | null = null;
  try {
    envelope = (await resp.json()) as Envelope<T>;
  } catch {
    envelope = null;
  }

  if (!envelope || typeof envelope.ok !== "boolean") {
    throw new ApiFailure(
      { code: "bad_envelope", message: `响应不是合法的 envelope（HTTP ${resp.status}）` },
      resp.status,
    );
  }

  if (!envelope.ok) {
    const err = envelope.error ?? { code: "unknown", message: "未知错误" };
    if (resp.status === 401 && !silent401) unauthorizedHandler();
    throw new ApiFailure(err, resp.status);
  }

  return envelope.data as T;
}

// ------------------------------------------------------------------ 媒体地址

/** 封面原图。ContentRow.cover_url 为 null 时不要调它，直接显示占位。 */
export function coverUrl(itemId: string): string {
  return `/review/${encodeURIComponent(itemId)}/cover`;
}

/** 按下标取媒体原文件（图片或 mp4）。bundle.media[].exists=false 时不要请求。 */
export function mediaUrl(itemId: string, index: number): string {
  return `/review/${encodeURIComponent(itemId)}/media/${index}`;
}

/** 公众号 body_html 原文。**必须**放进 sandbox iframe。 */
export function previewUrl(itemId: string): string {
  return `/review/${encodeURIComponent(itemId)}/preview`;
}

// ------------------------------------------------------------------ 错误文案

/** 把 ApiFailure 变成一句能直接弹给运营看的中文。 */
export function describeError(err: unknown): string {
  if (err instanceof ApiFailure) return err.message;
  if (err instanceof Error) return err.message;
  return String(err);
}
