"use client";

import * as React from "react";

export type Theme = "light" | "dark";

export const THEME_KEY = "sw_ui_theme";

/** 把主题落到 <html> 上：`.dark` 类 + `data-theme` 属性（截图脚本按后者断言）。 */
export function applyTheme(theme: Theme): void {
  if (typeof document === "undefined") return;
  const el = document.documentElement;
  el.setAttribute("data-theme", theme);
  el.classList.toggle("dark", theme === "dark");
}

export function readTheme(): Theme {
  if (typeof window === "undefined") return "light";
  try {
    const url = new URL(window.location.href);
    const q = url.searchParams.get("theme");
    if (q === "light" || q === "dark") return q;
    const stored = window.localStorage.getItem(THEME_KEY);
    if (stored === "light" || stored === "dark") return stored;
  } catch {
    /* 隐私模式 */
  }
  if (window.matchMedia?.("(prefers-color-scheme: dark)").matches) return "dark";
  return "light";
}

/**
 * 首屏内联脚本：在 React 水合之前就把主题定下来，避免暖米底闪一下再翻成暖暮。
 * `?theme=` 优先于 localStorage（截图与演示用），并顺手持久化。
 */
export const THEME_BOOT_SCRIPT = `
(function(){try{
  var el=document.documentElement;
  var k=${JSON.stringify(THEME_KEY)};
  var q=(location.search||"").match(/[?&]theme=(light|dark)/);
  var t=q?q[1]:localStorage.getItem(k);
  if(t!=="light"&&t!=="dark"){
    t=(window.matchMedia&&window.matchMedia("(prefers-color-scheme: dark)").matches)?"dark":"light";
  }
  if(q){try{localStorage.setItem(k,t);}catch(e){}}
  el.setAttribute("data-theme",t);
  if(t==="dark")el.classList.add("dark");else el.classList.remove("dark");
}catch(e){}})();
`;

export interface ThemeApi {
  theme: Theme;
  setTheme: (next: Theme) => void;
  toggle: () => void;
}

const ThemeContext = React.createContext<ThemeApi | null>(null);

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  // 初值必须与内联脚本一致，否则第一帧会闪。服务端渲染阶段固定 "light"，
  // 客户端在 effect 里立刻纠正（DOM 上的类名早就对了，纠的只是 React state）。
  const [theme, setThemeState] = React.useState<Theme>("light");

  React.useEffect(() => {
    const t = readTheme();
    setThemeState(t);
    // 必须重新落一次：React 19 水合 <html> 时会按自己渲染的 className 复位，
    // 把首屏内联脚本加上的 `.dark` / data-theme 抹掉。这里补回来。
    applyTheme(t);
  }, []);

  const setTheme = React.useCallback((next: Theme) => {
    setThemeState(next);
    applyTheme(next);
    try {
      window.localStorage.setItem(THEME_KEY, next);
    } catch {
      /* 隐私模式 */
    }
  }, []);

  const value = React.useMemo<ThemeApi>(
    () => ({ theme, setTheme, toggle: () => setTheme(theme === "dark" ? "light" : "dark") }),
    [theme, setTheme],
  );

  return React.createElement(ThemeContext.Provider, { value }, children);
}

export function useTheme(): ThemeApi {
  const ctx = React.useContext(ThemeContext);
  if (!ctx) throw new Error("useTheme 必须在 <ThemeProvider> 内使用");
  return ctx;
}
