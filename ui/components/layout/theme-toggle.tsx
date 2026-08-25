"use client";

import * as React from "react";

import { IconMoon, IconSun } from "@/components/icons";
import { useTheme } from "@/lib/theme";
import { cn } from "@/lib/utils";

/**
 * 日 / 夜切换 —— 两格分段控件，选中格换底不换色。
 *
 * P13 把它从顶栏最右挪到侧栏 footer（顶栏已删）。P14.B2 药丸化，选中格与
 * SegmentedControl 用同一句话（陶土 tint 而不是实底）：主题切换是一天用不到
 * 一次的开关，不该在侧栏里常年亮着一块实底主色——实底留给导航选中项与主按钮。
 */
export function ThemeToggle() {
  const { theme, setTheme } = useTheme();

  return (
    <div
      role="group"
      aria-label="日夜主题"
      className="flex items-center rounded-pill bg-muted p-0.5"
    >
      {(
        [
          { key: "light" as const, label: "日间", Icon: IconSun },
          { key: "dark" as const, label: "夜间", Icon: IconMoon },
        ]
      ).map(({ key, label, Icon }) => {
        const on = theme === key;
        return (
          <button
            key={key}
            type="button"
            aria-pressed={on}
            aria-label={label}
            title={label}
            data-theme-option={key}
            onClick={() => setTheme(key)}
            className={cn(
              "flex h-[1.375rem] w-7 cursor-default items-center justify-center rounded-pill py-1",
              "transition-colors duration-150",
              on ? "bg-primary-soft text-primary-deep" : "text-fg-4 hover:text-fg-2",
            )}
          >
            <Icon size={13} />
          </button>
        );
      })}
    </div>
  );
}

export default ThemeToggle;
