"use client";

import Link from "next/link";
import * as React from "react";

import { IconArrowRight, IconCheck, type IconProps } from "@/components/icons";
import type { Tone } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * 「需要你」待办清单 —— 今日页的 hero。
 *
 * 规矩很简单：**只列真的成立的事**。计数为 0 的条目根本不渲染；
 * 一条都不成立时给一句诚实的"今天没有需要你的事"，不摆四张永远亮着的卡。
 *
 * 着装：整组收进**一张陶土 tint 面板**（accent-100），逐条用左侧一道色脊分级。
 * 这是 P14 签名元素规则点名的位置——全站只有这一处 100 档 tint 面板 + 色脊，
 * 别处不许再用它做装饰，所以它一出现就等于"今天有事等你"。
 * （P13 时它只是一张普通卡面；色脊的分级逻辑原样保留：原来每条都铺满 tone 的
 * 色块，三条并排时整块屏幕都是彩色，反而分不出哪条更急。）
 */
export interface TodoItem {
  key: string;
  /** 为 0 / false 时这条待办不存在。 */
  active: boolean;
  icon: React.ComponentType<IconProps>;
  tone: Tone;
  title: React.ReactNode;
  detail: string;
  href: string;
  cta: string;
}

const SPINE: Record<Tone, string> = {
  err: "bg-err",
  warn: "bg-warn",
  amber: "bg-primary",
  ok: "bg-ok",
  muted: "bg-line-strong",
};

const ICON: Record<Tone, string> = {
  err: "text-err",
  warn: "text-warn",
  amber: "text-primary",
  ok: "text-ok",
  muted: "text-fg-4",
};

export function AttentionDeck({ todos }: { todos: TodoItem[] }) {
  const live = todos.filter((t) => t.active);

  if (live.length === 0) {
    return (
      <div
        data-testid="todo-empty"
        className="flex items-start gap-2.5 rounded-card bg-muted px-4 py-3.5"
      >
        <IconCheck size={15} className="mt-[2px] shrink-0 text-ok" />
        <span className="text-[12.5px] leading-relaxed text-fg-2">
          今天没有待处理事项。
          <span className="ml-1 text-fg-4">
            {/*
              这句话的每一个分句都对应上面 todos 里的一条：只要有一条 active，
              这个空态根本不会渲染。「账号都在线」曾经是在 degraded 账号存在时
              照样显示的——那是这一页最贵的一次说谎，别再让它回来。
            */}
            审核队列为空，账号连接正常，无死信，预算充足。可以查看排期。
          </span>
        </span>
      </div>
    );
  }

  return (
    <ul
      className="divide-y divide-primary-line overflow-hidden rounded-card bg-primary-soft shadow-card"
      data-testid="todo-list"
    >
      {live.map((t) => {
        const Icon = t.icon;
        return (
          <li key={t.key}>
            <Link
              href={t.href}
              data-testid="todo-item"
              data-todo={t.key}
              className="group relative flex items-center gap-3 py-3 pl-5 pr-4 transition-colors hover:bg-row-hover"
            >
              {/* 左脊：一条 3px 的色带，是这一组里唯一的彩色 */}
              <span
                aria-hidden="true"
                className={cn("absolute inset-y-0 left-0 w-[3px]", SPINE[t.tone])}
              />
              <Icon size={16} className={cn("shrink-0", ICON[t.tone])} />
              <span className="min-w-0 flex-1">
                <span className="block text-[13.5px] text-fg">{t.title}</span>
                <span className="block truncate text-[11.5px] text-fg-3">{t.detail}</span>
              </span>
              <span className="flex shrink-0 items-center gap-1 text-[12px] text-fg-4 transition-colors group-hover:text-primary">
                <span className="sw-keep">{t.cta}</span>
                <IconArrowRight size={13} />
              </span>
            </Link>
          </li>
        );
      })}
    </ul>
  );
}

export default AttentionDeck;
