import { afterEach, describe, expect, it, vi } from "vitest";

import { externalNav } from "./nav";

/**
 * 「对话」外链入口（P14.B5）：读构建期内联的 `NEXT_PUBLIC_SW_CHAT_URL`。
 * 静态导出没有服务端，这个值改不了运行时——所以这里直接操纵
 * `process.env`，模拟"构建时有没有配这个变量"两种情形。
 */
describe("externalNav", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("没配 NEXT_PUBLIC_SW_CHAT_URL 时返回空数组，不给半个占位符", () => {
    vi.stubEnv("NEXT_PUBLIC_SW_CHAT_URL", "");
    expect(externalNav()).toEqual([]);
  });

  it("配了就返回一条「对话」入口，external 标志与 href 都对得上", () => {
    vi.stubEnv("NEXT_PUBLIC_SW_CHAT_URL", "https://chat.example.com/");
    const items = externalNav();
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({
      href: "https://chat.example.com/",
      label: "对话",
      external: true,
    });
  });

  it("info 参数目前只是预留位置，传不传都不影响结果", () => {
    vi.stubEnv("NEXT_PUBLIC_SW_CHAT_URL", "https://chat.example.com/");
    const withInfo = externalNav({
      version: "x",
      env: "test",
      time: "",
      timezone: "UTC",
      llm_backend: "",
      llm_model: "",
      database: "",
      scheduler_enabled: false,
      use_fake_publishers: false,
      generate_enabled: false,
      publishers: [],
      ticks: [],
      platforms: [],
      content_statuses: [],
      review_queue_statuses: [],
      auth_required: false,
      budget: {},
    });
    expect(withInfo).toEqual(externalNav());
  });
});
