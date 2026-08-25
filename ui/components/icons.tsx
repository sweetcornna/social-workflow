import * as React from "react";

/**
 * 图标集 —— 路径取自 **Lucide**（https://lucide.dev，ISC License，
 * Copyright (c) 2026 Lucide Icons and Contributors），逐个内联进本文件。
 *
 * 为什么是内联而不是 `lucide-react`
 * --------------------------------
 * 一是不给工作台再加一个运行时依赖（静态导出的产物里不该出现一个图标库的
 * tree-shaking 残渣）；二是门面必须稳定：全站四十来处 `<IconX size={15} />`
 * 的调用形状、`IconProps`、`size` 语义在这次换血里**一个字都没变**，换掉的
 * 只有 `<svg>` 里的几何。三是 CSS 里不许出现 `url()`（porting-notes 门禁），
 * 图标只能是内联 SVG。
 *
 * 与上一版（P13 手绘 1.6px 描边）的差别
 * ------------------------------------
 * Organic 点名 Lucide、并要求 **stroke-width 2.75** 的"更圆更重"的观感。手绘那
 * 套 1.6px 发丝线是上一版中性灰阶控制台的声调，落在暖奶油底上会显得又细又脆，
 * 和 28px 圆角、药丸按钮不是一种语言。
 *
 * 描边档位见文件末尾 `STROKE` 的说明（实测截图后定档，不是照抄规格）。
 */

/** 全站默认描边宽度。定档理由见文件末尾。 */
const STROKE = 2.25;

export interface IconProps extends React.SVGProps<SVGSVGElement> {
  size?: number;
}

function make(path: React.ReactNode, displayName: string) {
  const Comp = React.forwardRef<SVGSVGElement, IconProps>(function Icon(
    { size = 16, strokeWidth = STROKE, ...rest },
    ref,
  ) {
    return (
      <svg
        ref={ref}
        width={size}
        height={size}
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={strokeWidth}
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
        focusable="false"
        {...rest}
      >
        {path}
      </svg>
    );
  });
  Comp.displayName = displayName;
  return Comp;
}

// ── 导航 ────────────────────────────────────────────────────────────────
export const IconPulse = make(
  <path d="M22 12h-2.48a2 2 0 0 0-1.93 1.46l-2.35 8.36a.25.25 0 0 1-.48 0L9.24 2.18a.25.25 0 0 0-.48 0l-2.35 8.36A2 2 0 0 1 4.49 12H2" />,
  "IconPulse",
);
export const IconInbox = make(
  <>
    <polyline points="22 12 16 12 14 15 10 15 8 12 2 12" />
    <path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z" />
  </>,
  "IconInbox",
);
export const IconUsers = make(
  <>
    <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" />
    <path d="M16 3.128a4 4 0 0 1 0 7.744" />
    <path d="M22 21v-2a4 4 0 0 0-3-3.87" />
    <circle cx="9" cy="7" r="4" />
  </>,
  "IconUsers",
);
export const IconCalendar = make(
  <>
    <path d="M8 2v3" />
    <path d="M16 2v3" />
    <rect x="3" y="3" width="18" height="18" rx="2" />
    <path d="M3 9h18" />
    <path d="M8 13h.01" />
    <path d="M12 13h.01" />
    <path d="M16 13h.01" />
    <path d="M8 17h.01" />
    <path d="M12 17h.01" />
    <path d="M16 17h.01" />
  </>,
  "IconCalendar",
);
export const IconFlame = make(
  <path d="M12 3q1 4 4 6.5t3 5.5a1 1 0 0 1-14 0 5 5 0 0 1 1-3 1 1 0 0 0 5 0c0-2-1.5-3-1.5-5q0-2 2.5-4" />,
  "IconFlame",
);
export const IconLayers = make(
  <>
    <path d="M12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83z" />
    <path d="M2 12a1 1 0 0 0 .58.91l8.6 3.91a2 2 0 0 0 1.65 0l8.58-3.9A1 1 0 0 0 22 12" />
    <path d="M2 17a1 1 0 0 0 .58.91l8.6 3.91a2 2 0 0 0 1.65 0l8.58-3.9A1 1 0 0 0 22 17" />
  </>,
  "IconLayers",
);
export const IconChart = make(
  <>
    <path d="M3 3v16a2 2 0 0 0 2 2h16" />
    <path d="M18 17V9" />
    <path d="M13 17V5" />
    <path d="M8 17v-3" />
  </>,
  "IconChart",
);
export const IconBook = make(
  <>
    <path d="M12 5v16" />
    <path d="M20.001 19A2 2 0 0022 17V5a2 2 0 00-1.999-2L16 3.002A5 5 0 0012 5a5 5 0 00-4-2H4a2 2 0 00-2 2v12a2 2 0 001.999 2H8a5 5 0 014 2 5 5 0 014-2z" />
  </>,
  "IconBook",
);
export const IconShield = make(
  <>
    <path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z" />
    <path d="m9 12 2 2 4-4" />
  </>,
  "IconShield",
);

// ── 操作 / 状态 ─────────────────────────────────────────────────────────
export const IconSearch = make(
  <>
    <path d="m21 21-4.34-4.34" />
    <circle cx="11" cy="11" r="8" />
  </>,
  "IconSearch",
);
export const IconSun = make(
  <>
    <circle cx="12" cy="12" r="4" />
    <path d="M12 2v2" />
    <path d="M12 20v2" />
    <path d="m4.93 4.93 1.41 1.41" />
    <path d="m17.66 17.66 1.41 1.41" />
    <path d="M2 12h2" />
    <path d="M20 12h2" />
    <path d="m6.34 17.66-1.41 1.41" />
    <path d="m19.07 4.93-1.41 1.41" />
  </>,
  "IconSun",
);
export const IconMoon = make(
  <path d="M20.985 12.486a9 9 0 1 1-9.473-9.472c.405-.022.617.46.402.803a6 6 0 0 0 8.268 8.268c.344-.215.825-.004.803.401" />,
  "IconMoon",
);
export const IconCheck = make(<path d="M20 6 9 17l-5-5" />, "IconCheck");
export const IconX = make(
  <>
    <path d="M18 6 6 18" />
    <path d="m6 6 12 12" />
  </>,
  "IconX",
);
export const IconChevronRight = make(<path d="m9 18 6-6-6-6" />, "IconChevronRight");
export const IconChevronLeft = make(<path d="m15 18-6-6 6-6" />, "IconChevronLeft");
export const IconChevronDown = make(<path d="m6 9 6 6 6-6" />, "IconChevronDown");
/**
 * 行操作的「⋯」触发器。Lucide 的 ellipsis 本来就是横排三点——本工作台的行高
 * 只有 40px 出头，竖排三点在视觉上比横排更"高"，会把行撑得更紧（P13 的结论，
 * 这次换 Lucide 几何时正好与它一致，不必再自绘）。
 */
export const IconMore = make(
  <>
    <circle cx="12" cy="12" r="1" />
    <circle cx="19" cy="12" r="1" />
    <circle cx="5" cy="12" r="1" />
  </>,
  "IconMore",
);
export const IconArrowRight = make(
  <>
    <path d="M5 12h14" />
    <path d="m12 5 7 7-7 7" />
  </>,
  "IconArrowRight",
);
export const IconAlert = make(
  <>
    <path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3" />
    <path d="M12 9v4" />
    <path d="M12 17h.01" />
  </>,
  "IconAlert",
);
export const IconRefresh = make(
  <>
    <path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8" />
    <path d="M21 3v5h-5" />
    <path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16" />
    <path d="M8 16H3v5" />
  </>,
  "IconRefresh",
);
export const IconExternal = make(
  <>
    <path d="M15 3h6v6" />
    <path d="M10 14 21 3" />
    <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
  </>,
  "IconExternal",
);
export const IconPlay = make(
  <path d="M5 5a2 2 0 0 1 3.008-1.728l11.997 6.998a2 2 0 0 1 .003 3.458l-12 7A2 2 0 0 1 5 19z" />,
  "IconPlay",
);
export const IconImage = make(
  <>
    <rect width="18" height="18" x="3" y="3" rx="2" ry="2" />
    <circle cx="9" cy="9" r="2" />
    <path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21" />
  </>,
  "IconImage",
);
export const IconVideo = make(
  <>
    <path d="m16 13 5.223 3.482a.5.5 0 0 0 .777-.416V7.87a.5.5 0 0 0-.752-.432L16 10.5" />
    <rect x="2" y="6" width="14" height="12" rx="2" />
  </>,
  "IconVideo",
);
export const IconClock = make(
  <>
    <circle cx="12" cy="12" r="10" />
    <path d="M12 6v6l4 2" />
  </>,
  "IconClock",
);
export const IconQr = make(
  <>
    <rect width="5" height="5" x="3" y="3" rx="1" />
    <rect width="5" height="5" x="16" y="3" rx="1" />
    <rect width="5" height="5" x="3" y="16" rx="1" />
    <path d="M21 16h-3a2 2 0 0 0-2 2v3" />
    <path d="M21 21v.01" />
    <path d="M12 7v3a2 2 0 0 1-2 2H7" />
    <path d="M3 12h.01" />
    <path d="M12 3h.01" />
    <path d="M12 16v.01" />
    <path d="M16 12h1" />
    <path d="M21 12v.01" />
    <path d="M12 21v-1" />
  </>,
  "IconQr",
);
export const IconLogout = make(
  <>
    <path d="m16 17 5-5-5-5" />
    <path d="M21 12H9" />
    <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
  </>,
  "IconLogout",
);
export const IconKey = make(
  <>
    <path d="M2.586 17.414A2 2 0 0 0 2 18.828V21a1 1 0 0 0 1 1h3a1 1 0 0 0 1-1v-1a1 1 0 0 1 1-1h1a1 1 0 0 0 1-1v-1a1 1 0 0 1 1-1h.172a2 2 0 0 0 1.414-.586l.814-.814a6.5 6.5 0 1 0-4-4z" />
    <circle cx="16.5" cy="7.5" r=".5" fill="currentColor" />
  </>,
  "IconKey",
);
export const IconEdit = make(
  <>
    <path d="M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
    <path d="M18.375 2.625a1 1 0 0 1 3 3l-9.013 9.014a2 2 0 0 1-.853.505l-2.873.84a.5.5 0 0 1-.62-.62l.84-2.873a2 2 0 0 1 .506-.852z" />
  </>,
  "IconEdit",
);
export const IconRotate = make(
  <>
    <path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" />
    <path d="M3 3v5h5" />
  </>,
  "IconRotate",
);
export const IconWindow = make(
  <>
    <rect x="2" y="4" width="20" height="16" rx="2" />
    <path d="M10 4v4" />
    <path d="M2 8h20" />
    <path d="M6 4v4" />
  </>,
  "IconWindow",
);
export const IconMessage = make(
  <path d="M2.992 16.342a2 2 0 0 1 .094 1.167l-1.065 3.29a1 1 0 0 0 1.236 1.168l3.413-.998a2 2 0 0 1 1.099.092 10 10 0 1 0-4.777-4.719" />,
  "IconMessage",
);
export const IconSpark = make(
  <>
    <path d="M11.017 2.814a1 1 0 0 1 1.966 0l1.051 5.558a2 2 0 0 0 1.594 1.594l5.558 1.051a1 1 0 0 1 0 1.966l-5.558 1.051a2 2 0 0 0-1.594 1.594l-1.051 5.558a1 1 0 0 1-1.966 0l-1.051-5.558a2 2 0 0 0-1.594-1.594l-5.558-1.051a1 1 0 0 1 0-1.966l5.558-1.051a2 2 0 0 0 1.594-1.594z" />
    <path d="M20 2v4" />
    <path d="M22 4h-4" />
    <circle cx="4" cy="20" r="2" />
  </>,
  "IconSpark",
);
export const IconCommand = make(
  <path d="M15 6v12a3 3 0 1 0 3-3H6a3 3 0 1 0 3 3V6a3 3 0 1 0-3 3h12a3 3 0 1 0-3-3" />,
  "IconCommand",
);
export const IconFilter = make(
  <path d="M10 20a1 1 0 0 0 .553.895l2 1A1 1 0 0 0 14 21v-7a2 2 0 0 1 .517-1.341L21.74 4.67A1 1 0 0 0 21 3H3a1 1 0 0 0-.742 1.67l7.225 7.989A2 2 0 0 1 10 14z" />,
  "IconFilter",
);
/**
 * 实心小圆点。Lucide 没有"实心点"这一枚（`circle` 是 r=10 的描边圆，`dot`
 * 是圆里套点），而这里要的就是一个纯粹的色点——图例、状态标记用它。
 * 保留自绘，是这批里唯一没有 Lucide 对应物的图标。
 */
export const IconDot = make(<circle cx="12" cy="12" r="4" fill="currentColor" />, "IconDot");
export const IconTrend = make(
  <>
    <path d="M16 7h6v6" />
    <path d="m22 7-8.5 8.5-5-5L2 17" />
  </>,
  "IconTrend",
);
export const IconWallet = make(
  <>
    <path d="M19 7V4a1 1 0 0 0-1-1H5a2 2 0 0 0 0 4h15a1 1 0 0 1 1 1v4h-3a2 2 0 0 0 0 4h3a1 1 0 0 0 1-1v-2a1 1 0 0 0-1-1" />
    <path d="M3 5v14a2 2 0 0 0 2 2h15a1 1 0 0 0 1-1v-4" />
  </>,
  "IconWallet",
);
export const IconLock = make(
  <>
    <rect width="18" height="11" x="3" y="11" rx="2" ry="2" />
    <path d="M7 11V7a5 5 0 0 1 10 0v4" />
  </>,
  "IconLock",
);
export const IconUnlock = make(
  <>
    <rect width="18" height="11" x="3" y="11" rx="2" ry="2" />
    <path d="M7 11V7a5 5 0 0 1 9.9-1" />
  </>,
  "IconUnlock",
);
export const IconKeyboard = make(
  <>
    <path d="M10 8h.01" />
    <path d="M12 12h.01" />
    <path d="M14 8h.01" />
    <path d="M16 12h.01" />
    <path d="M18 8h.01" />
    <path d="M6 8h.01" />
    <path d="M7 16h10" />
    <path d="M8 12h.01" />
    <rect width="20" height="16" x="2" y="4" rx="2" />
  </>,
  "IconKeyboard",
);

export const IconPlus = make(
  <>
    <path d="M5 12h14" />
    <path d="M12 5v14" />
  </>,
  "IconPlus",
);
export const IconMinus = make(<path d="M5 12h14" />, "IconMinus");
/** 停用 / 启用：电源符号。 */
export const IconPower = make(
  <>
    <path d="M12 2v10" />
    <path d="M18.4 6.6a9 9 0 1 1-12.77.04" />
  </>,
  "IconPower",
);
/** sidecar 容器。 */
export const IconBox = make(
  <>
    <path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z" />
    <path d="m3.3 7 8.7 5 8.7-5" />
    <path d="M12 22V12" />
  </>,
  "IconBox",
);

/** 品牌方章。P13 起只出现在登录页——外壳的 wordmark 是纯文字。 */
export function BrandMark({ size = 30 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      aria-hidden="true"
      focusable="false"
      style={{ display: "block", flex: "none" }}
    >
      {/*
        一条发布轴 + 一个待决策的点，正好是这个产品在做的事。方章的圆角跟着
        Organic 的 radius 走（8px 之于 32px 见方，与 --sw-radius-sm 同档）；
        底色用 primary-solid 而不是 primary(500)，与主按钮同一块实底陶土，
        上面的奶油笔画才够对比度。
      */}
      <rect x="0" y="0" width="32" height="32" rx="8" fill="var(--sw-primary-solid)" />
      <path
        d="M6 21h20"
        stroke="var(--sw-primary-fg)"
        strokeOpacity="0.55"
        strokeWidth="2"
        strokeLinecap="round"
      />
      <circle cx="12" cy="21" r="2" fill="var(--sw-primary-fg)" fillOpacity="0.55" />
      <circle cx="21" cy="21" r="3" fill="var(--sw-primary-fg)" />
      <path
        d="M21 9v7"
        stroke="var(--sw-primary-fg)"
        strokeOpacity="0.55"
        strokeWidth="2"
        strokeLinecap="round"
      />
    </svg>
  );
}

/*
 * 描边档位（P14.B2 实测定档）
 * --------------------------
 * Organic 规格写的是 2.75。本工作台的图标实际渲染尺寸是 12–18px（行内 12/13px、
 * 导航 15/16px），把 24 见方的几何缩到 12px 时，2.75 的笔画会占到图形净宽的近
 * 四分之一——闭合形状（IconBox 的箱体、IconCalendar 的格子、IconQr 的三个定位
 * 角）内部空白被笔画吃掉，糊成一个色块。2.25 在同一批截图里保住了这些内部空白，
 * 而在 16px 上仍然明显比 P13 的 1.6px 更圆更重，Organic 要的那份"重"已经拿到。
 *
 * 结论：**定档 2.25**，对规格的 2.75 是一处有意偏离，理由是渲染尺寸不同
 * （Organic 的组件页图标是 20–24px 展示尺寸，我们最密的地方只有 12px）。
 * 对比截图与逐图判读见 docs/briefs/p14_b2_report.md。
 */
