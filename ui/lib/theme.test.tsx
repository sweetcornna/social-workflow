import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import { ThemeToggle } from "@/components/layout/theme-toggle";

import { applyTheme, readTheme, THEME_KEY, ThemeProvider } from "./theme";

describe("日夜主题", () => {
  beforeEach(() => {
    document.documentElement.className = "";
    document.documentElement.removeAttribute("data-theme");
    window.localStorage.clear();
  });

  it("applyTheme 同时落 .dark 类与 data-theme 属性", () => {
    applyTheme("dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");

    applyTheme("light");
    expect(document.documentElement.classList.contains("dark")).toBe(false);
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
  });

  it("readTheme 优先读 localStorage，读不到才问 prefers-color-scheme", () => {
    window.localStorage.setItem(THEME_KEY, "dark");
    expect(readTheme()).toBe("dark");

    window.localStorage.clear();
    // setup 里的 matchMedia stub 恒 matches:false，即系统偏好为日间
    expect(readTheme()).toBe("light");
  });

  it("切换 pill 会持久化，并把类名翻过去", async () => {
    render(
      <ThemeProvider>
        <ThemeToggle />
      </ThemeProvider>,
    );

    await userEvent.click(screen.getByRole("button", { name: "夜间" }));
    expect(window.localStorage.getItem(THEME_KEY)).toBe("dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(screen.getByRole("button", { name: "夜间" })).toHaveAttribute("aria-pressed", "true");

    await userEvent.click(screen.getByRole("button", { name: "日间" }));
    expect(window.localStorage.getItem(THEME_KEY)).toBe("light");
    expect(document.documentElement.classList.contains("dark")).toBe(false);
  });
});
