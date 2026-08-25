"use client";

import * as React from "react";

import { IconMessage, IconRefresh } from "@/components/icons";
import { DataState } from "@/components/layout/data-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { LiveDot } from "@/components/ui/live-dot";
import { SectionPanel } from "@/components/ui/section-panel";
import { useApi } from "@/lib/hooks";
import type { TelegramChannel } from "@/lib/types";

/**
 * 提醒渠道（Telegram）的连通状态。
 *
 * 叫「提醒渠道」不叫 notifier：界面按运营的世界命名，不按实现命名。
 *
 * 这一块**不会发任何网络请求去探活**（后端也不会），所以放在页面加载路径上是安全的。
 * 真要探活是 `uv run python -m core.telegram check`。
 *
 * 不可用时必须说清**为什么 + 怎么补**，而不是给一个灰掉的开关或者"未配置"三个字。
 * 最常见的那一种（token 配了、没人发过 /start）尤其要说得像句人话。
 */
export function ReminderChannel() {
  const { data, error, isLoading, mutate } =
    useApi<TelegramChannel>("/system/telegram");

  return (
    <SectionPanel
      title="提醒渠道"
      subtitle="发布前的确认卡、掉线提醒都走这里"
      actions={
        <Button size="sm" onClick={() => void mutate()}>
          <IconRefresh size={12} />
          刷新
        </Button>
      }
    >
      <DataState
        isLoading={isLoading}
        error={error}
        onRetry={() => void mutate()}
        rows={3}
      >
        {data ? <ChannelBody data={data} /> : null}
      </DataState>
    </SectionPanel>
  );
}

/** 一句话说清现在是什么状态、接下来该做什么。空态是行动邀请，不是"暂无数据"。 */
function guidance(d: TelegramChannel): { headline: string; body: string } {
  const who = d.username ? `@${d.username}` : "你的 bot";
  // 先看有没有 token，再看开关——建 bot、拿 token 是接入 Telegram 天然的第一步
  // （见 docs/OPS.md），没有 token 时单说"把开关打开"是一句瘸腿的指引：
  // 照做也不会亮起来，人还得再回来看一遍。两者都缺时，先把最前置的那步说清楚。
  if (!d.configured) {
    return {
      headline: "还没有接提醒渠道",
      body: "把 TELEGRAM_BOT_TOKEN 写进服务器的 .env（600 权限，不入库、不进前端），重启 core。没有它，等确认的稿只能在这个工作台里点。",
    };
  }
  if (!d.enabled) {
    return {
      headline: "提醒渠道关着",
      body: "服务器上 SW_TELEGRAM_ENABLED=false。改成 true 再重启 core，这一块就会亮起来。",
    };
  }
  if (!d.chat_configured) {
    return {
      headline: "不知道该推给谁",
      body: `还没有人给 ${who} 发过 /start，所以系统不知道该把确认卡推到哪个会话。发一句 /start，再回来点刷新。`,
    };
  }
  if (!d.can_sign) {
    return {
      headline: "能推，但不带按钮",
      body: "没有签名密钥（SW_TELEGRAM_SIGNING_SECRET 或 SW_UI_TOKEN），按钮就防不住伪造，所以系统只发纯文字提醒。配一个密钥再重启，确认卡上的按钮就回来了。",
    };
  }
  if (!d.polling) {
    return {
      headline: "推得出去，但收不回来",
      body: d.last_error
        ? `长轮询没在跑：${d.last_error}。卡片还能推出去，但你点的按钮不会有反应——先看 core 的日志。`
        : "长轮询没在跑，你点的按钮不会有反应。重启一次 core；期间可以在排期页上点确认。",
    };
  }
  return {
    headline: `${who} 在线`,
    body: "确认卡会推到这里；你点的按钮会在几秒内生效。",
  };
}

function ChannelBody({ data }: { data: TelegramChannel }) {
  const live = data.ready && data.polling && data.can_sign;
  const g = guidance(data);
  const stats = data.stats ?? {};

  return (
    <div className="px-4 py-3.5">
      <div className="flex items-start gap-2.5">
        <LiveDot
          tone={live ? "ok" : data.configured ? "warn" : "muted"}
          pulse={live}
          className="mt-1"
          size={8}
        />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span
              className="text-[13px] text-fg"
              data-testid="telegram-headline"
            >
              {g.headline}
            </span>
            {data.username ? (
              <Badge tone="muted">
                <IconMessage size={10} />@{data.username}
              </Badge>
            ) : null}
            {live ? <Badge tone="ok">长轮询在跑</Badge> : null}
          </div>
          <p
            className="mt-1 text-[11.5px] leading-relaxed text-fg-3"
            data-testid="telegram-guidance"
          >
            {g.body}
          </p>
        </div>
      </div>

      {/*
        计数只在真的推过东西之后才有意义。一台刚起来的机器显示一排 0，
        看着像坏了——不如不显示。
      */}
      {data.configured ? (
        <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-1.5 sm:grid-cols-4">
          <Stat label="本进程已推" value={data.sent} />
          <Stat
            label="推送失败"
            value={data.failed}
            tone={data.failed > 0 ? "err" : undefined}
          />
          <Stat label="收到回调" value={stats.handled ?? 0} />
          <Stat
            label="拒掉的回调"
            value={stats.rejected ?? 0}
            hint="来源或签名对不上。任何人都能找到这个 bot，出现几条是正常的"
          />
        </dl>
      ) : null}
    </div>
  );
}

function Stat({
  label,
  value,
  tone,
  hint,
}: {
  label: string;
  value: number;
  tone?: "err";
  hint?: string;
}) {
  return (
    <div className="rounded bg-muted px-2 py-1.5" title={hint}>
      <dt className="text-[10px] text-fg-4">{label}</dt>
      <dd
        className={`sw-num text-[15px] ${tone === "err" ? "text-err" : "text-fg"}`}
      >
        {value}
      </dd>
    </div>
  );
}
