import type * as React from "react";

import {
  IconBook,
  IconCalendar,
  IconChart,
  IconInbox,
  IconLayers,
  IconMessage,
  IconPulse,
  IconShield,
  IconUsers,
  IconWallet,
  type IconProps,
} from "@/components/icons";
import type { SystemInfo } from "@/lib/types";

export interface NavItem {
  href: string;
  label: string;
  /** 面包屑与命令面板用的一句话说明。 */
  hint: string;
  icon: React.ComponentType<IconProps>;
}

/**
 * 主导航 = 运营一天的动线，不是后端模块的目录。
 *
 * 「看有什么等我审 → 沉浸式审内容 → 关心今天发什么 → 处理账号异常」，
 * 剩下的观测面（统计 / 成本 / 复盘 / 渲染与死信 / 门禁）全部收进「系统」的 tab，
 * 因为它们是**出事之后**才去翻的，不该每天占一格。
 */
export const NAV: NavItem[] = [
  { href: "/", label: "今日", hint: "需要你处理的事 + 今天几点发什么", icon: IconPulse },
  {
    href: "/review/",
    label: "审核台",
    hint: "沉浸式过审：j/k 换条 · a 批准 · r 驳回",
    icon: IconInbox,
  },
  { href: "/schedule/", label: "排期", hint: "按天时间线：已发 / 待发 / 失败与改期", icon: IconCalendar },
  { href: "/accounts/", label: "账号", hint: "健康、扫码待办、今日配额与发布窗口", icon: IconUsers },
  { href: "/system/", label: "系统", hint: "统计 · 成本 · 复盘 · 渲染与死信 · 门禁", icon: IconShield },
];

/** 系统页的 tab。合并了原来的 统计 / 成本 / 复盘 / 任务 / 系统 五张平行页。 */
export const SYSTEM_TABS = [
  { value: "stats", label: "统计" },
  { value: "costs", label: "成本" },
  { value: "insights", label: "复盘" },
  { value: "jobs", label: "渲染与死信" },
  { value: "runtime", label: "自检与任务" },
] as const;

export type SystemTab = (typeof SYSTEM_TABS)[number]["value"];

export function isSystemTab(v: string | null | undefined): v is SystemTab {
  return SYSTEM_TABS.some((t) => t.value === v);
}

/**
 * 命令面板里的二级目的地。
 * 统计 / 复盘 / 死信不再是一级页，⌘K 得能直接把人送到对应 tab，
 * 否则"少了几页"就变成了"东西找不着了"。
 */
export const SUB_NAV: NavItem[] = [
  { href: "/system/?tab=stats", label: "统计", hint: "每日序列、账号表", icon: IconChart },
  { href: "/system/?tab=costs", label: "成本", hint: "tokens 与渲染秒曲线", icon: IconWallet },
  { href: "/system/?tab=insights", label: "复盘", hint: "按账号的运营结论", icon: IconBook },
  { href: "/system/?tab=jobs", label: "渲染与死信", hint: "渲染任务、发布记录、死信", icon: IconLayers },
  { href: "/system/?tab=runtime", label: "门禁与心跳", hint: "preflight、定时任务、运行信息", icon: IconShield },
];

/**
 * P6 的旧路径 → 新 IA。页面级重定向用它，书签不会失效。
 * 顺序有意义：先长后短，`startsWith` 才不会被 `/` 抢先命中。
 */
export const LEGACY_ROUTES: Record<string, string> = {
  "/content/": "/schedule/",
  "/topics/": "/",
  "/jobs/": "/system/?tab=jobs",
  "/stats/": "/system/?tab=stats",
  "/insights/": "/system/?tab=insights",
};

export function navItemFor(pathname: string): NavItem | undefined {
  const normalized = pathname.endsWith("/") ? pathname : `${pathname}/`;
  if (normalized === "/") return NAV[0];
  return NAV.find((n) => n.href !== "/" && normalized.startsWith(n.href));
}

/**
 * 外链导航项（P14.B5）。「对话」入口指向独立部署的 chat 控制台——不是本工作台
 * 的一个路由，`href` 是完整 URL 不是站内路径，渲染层要认得 `external: true`
 * 才知道该开新标签页而不是走 `next/link`。
 */
export interface ExternalNavItem {
  href: string;
  label: string;
  hint: string;
  icon: React.ComponentType<IconProps>;
  external: true;
}

/**
 * 外链导航表。目前只有「对话」一项，读构建期内联的 `NEXT_PUBLIC_SW_CHAT_URL`——
 * 静态导出没有服务端，运行时改不了这个值，只能在 `bash scripts/build_ui.sh`
 * 之前设好环境变量。没配就是**没有这个入口**（返回空数组），不渲染半个占位符。
 *
 * `info` 参数当前不读：chat 控制台的地址将来可能改由 `/system/info` 下发
 * （见 P15 brief），提前留好签名位置，届时改的是函数体，调用方不用跟着改。
 */
export function externalNav(info?: SystemInfo): ExternalNavItem[] {
  void info;
  const url = process.env.NEXT_PUBLIC_SW_CHAT_URL;
  if (!url) return [];
  return [
    {
      href: url,
      label: "对话",
      hint: "chat 控制台，独立部署，新标签页打开",
      icon: IconMessage,
      external: true,
    },
  ];
}
