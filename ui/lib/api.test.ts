import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  apiFetch,
  ApiFailure,
  buildUrl,
  getToken,
  setToken,
  setUnauthorizedHandler,
  TOKEN_KEY,
} from "./api";

function mockResponse(body: unknown, status = 200) {
  return {
    status,
    json: async () => body,
  } as unknown as Response;
}

describe("buildUrl", () => {
  it("拼 /api/v1 前缀，并丢掉空 query", () => {
    expect(buildUrl("/review")).toBe("/api/v1/review");
    expect(buildUrl("/review", { status: "", platform: "xhs", limit: 50 })).toBe(
      "/api/v1/review?platform=xhs&limit=50",
    );
  });

  it("已经带 /api/ 的路径原样用（SWR 的 key 就是完整 URL）", () => {
    expect(buildUrl("/api/v1/dashboard")).toBe("/api/v1/dashboard");
  });
});

describe("apiFetch envelope", () => {
  beforeEach(() => {
    setToken("");
    setUnauthorizedHandler(() => {});
  });

  it("成功时只把 data 交出去", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(mockResponse({ ok: true, data: { total: 3 }, error: null })),
    );
    await expect(apiFetch<{ total: number }>("/review")).resolves.toEqual({ total: 3 });
  });

  it("失败时抛 ApiFailure，带 code 与 detail", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        mockResponse(
          {
            ok: false,
            data: null,
            error: {
              code: "invalid_slot",
              message: "不是合法发布时刻",
              detail: { reason: "窗口", suggested_slot: "2026-08-17T01:00:00+00:00" },
            },
          },
          422,
        ),
      ),
    );
    const err = await apiFetch("/content/x/reschedule", { method: "POST" }).catch((e) => e);
    expect(err).toBeInstanceOf(ApiFailure);
    expect((err as ApiFailure).code).toBe("invalid_slot");
    expect((err as ApiFailure).status).toBe(422);
    expect((err as ApiFailure).detail).toMatchObject({ reason: "窗口" });
  });

  it("响应不是 envelope 时也要抛，而不是把 undefined 交给页面", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(mockResponse({ detail: "boom" }, 500)));
    const err = await apiFetch("/dashboard").catch((e) => e);
    expect(err).toBeInstanceOf(ApiFailure);
    expect((err as ApiFailure).code).toBe("bad_envelope");
  });

  it("有 token 时注入 Authorization 头（不放 query string）", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(mockResponse({ ok: true, data: null, error: null }));
    vi.stubGlobal("fetch", fetchMock);
    setToken("demo-token-abc123");
    expect(getToken()).toBe("demo-token-abc123");

    await apiFetch("/dashboard");

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/dashboard");
    expect(url).not.toContain("token=");
    expect((init.headers as Record<string, string>).authorization).toBe(
      "Bearer demo-token-abc123",
    );
  });

  it("401 触发跳登录处理器，silent401 时不触发", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        mockResponse(
          { ok: false, data: null, error: { code: "unauthorized", message: "缺少 token" } },
          401,
        ),
      ),
    );
    const onUnauthorized = vi.fn();
    setUnauthorizedHandler(onUnauthorized);

    await apiFetch("/dashboard").catch(() => {});
    expect(onUnauthorized).toHaveBeenCalledTimes(1);

    await apiFetch("/auth/login", { method: "POST", silent401: true }).catch(() => {});
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it("setToken('') 清掉存储", () => {
    setToken("x");
    expect(window.localStorage.getItem(TOKEN_KEY)).toBe("x");
    setToken("");
    expect(window.localStorage.getItem(TOKEN_KEY)).toBeNull();
  });
});
