import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ThemeProvider } from "@/lib/theme";

vi.mock("next/navigation", () => ({
  usePathname: () => "/",
}));

import { Sidebar } from "./sidebar";

function renderSidebar() {
  return render(
    <ThemeProvider>
      <Sidebar onOpenPalette={() => {}} />
    </ThemeProvider>,
  );
}

/**
 * 「对话」外链入口的显隐（P14.B5）。桌面竖栏与移动横轨各渲染一份，
 * Sidebar 组件本身同时输出两套 DOM（用 CSS 控制哪套可见），所以这里按
 * `getAllByTestId` 断言"有几个"而不是"有没有"。
 */
describe("Sidebar · 外链入口", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("没配 NEXT_PUBLIC_SW_CHAT_URL 时，「对话」不出现在任何一处导航里", () => {
    vi.stubEnv("NEXT_PUBLIC_SW_CHAT_URL", "");
    renderSidebar();
    expect(screen.queryByText("对话")).not.toBeInTheDocument();
    expect(screen.queryAllByTestId("external-nav-item")).toHaveLength(0);
  });

  it("配了之后，桌面竖栏与移动横轨各出现一枚「对话」外链，指向配置的 URL、新标签页打开", () => {
    vi.stubEnv("NEXT_PUBLIC_SW_CHAT_URL", "https://chat.example.com/");
    renderSidebar();

    const links = screen.getAllByTestId("external-nav-item");
    // 桌面竖栏一枚 + 移动横轨一枚，两套 DOM 都渲染（CSS 控制哪套可见）
    expect(links).toHaveLength(2);
    for (const link of links) {
      expect(link).toHaveAttribute("href", "https://chat.example.com/");
      expect(link).toHaveAttribute("target", "_blank");
      expect(link).toHaveAttribute("rel", "noopener");
      expect(link).toHaveTextContent("对话");
    }
  });
});
