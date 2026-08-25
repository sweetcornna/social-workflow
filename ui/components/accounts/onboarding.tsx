"use client";

import * as React from "react";

import { SidecarPanel } from "@/components/accounts/sidecar-panel";
import { IconAlert, IconCheck, IconKey, IconQr, IconRefresh, IconWindow } from "@/components/icons";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { LiveDot } from "@/components/ui/live-dot";
import { apiFetch, ApiFailure, describeError } from "@/lib/api";
import { fmtTime } from "@/lib/format";
import { POLL, useApi } from "@/lib/hooks";
import type { AccountRow, LoginStatus, QrCode } from "@/lib/types";

/**
 * 「接入」这一步 —— 三个平台三种做法，各说各的实话。
 *
 * 小红书能在浏览器里走完（core 把 sidecar 的二维码代理过来）；抖音的二维码在
 * **宿主机的有头浏览器**里，服务器代不了；公众号根本没有扫码这回事，是凭据 + IP 白名单。
 * 后两种绝不做成"看起来能点"的假按钮：做不到就写清楚要去哪台机器做什么。
 */

// ------------------------------------------------------------------ 小红书

/** 二维码大图 + 倒计时 + 登录态轮询。扫成功就地变绿。 */
export function QrStage({
  accountId,
  onLoggedIn,
}: {
  accountId: string;
  onLoggedIn?: () => void;
}) {
  const [qr, setQr] = React.useState<QrCode | null>(null);
  const [failure, setFailure] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [left, setLeft] = React.useState(0);

  const { data: status } = useApi<LoginStatus>(
    `/accounts/${accountId}/login/status`,
    undefined,
    { refreshInterval: POLL.loginStatus },
  );
  const loggedIn = Boolean(status?.logged_in);

  const load = React.useCallback(async () => {
    setBusy(true);
    try {
      const data = await apiFetch<QrCode>(`/accounts/${accountId}/login/qrcode`);
      setQr(data);
      setFailure("");
      setLeft(data.expires_in || 0);
    } catch (e) {
      // 取不到二维码的原因五花八门（sidecar 没起来 / 平台不支持 / 上游报错），
      // 一律把后端那句中文原样显示——猜原因只会误导人
      setQr(null);
      setFailure(
        e instanceof ApiFailure && e.code === "upstream_error"
          ? `${e.message}（sidecar 可能未启动，见上方面板）`
          : describeError(e),
      );
    } finally {
      setBusy(false);
    }
  }, [accountId]);

  React.useEffect(() => {
    void load();
  }, [load]);

  React.useEffect(() => {
    if (loggedIn) onLoggedIn?.();
  }, [loggedIn, onLoggedIn]);

  // 倒计时。归零自动换一张——上游每取一次就作废上一张，所以**只在过期后**才重取
  React.useEffect(() => {
    if (left <= 0 || loggedIn) return;
    const t = window.setInterval(() => setLeft((v) => Math.max(0, v - 1)), 1000);
    return () => window.clearInterval(t);
  }, [left, loggedIn]);

  React.useEffect(() => {
    if (qr && left === 0 && !busy && !loggedIn) void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [left, loggedIn]);

  if (loggedIn) {
    return (
      <div
        className="flex items-center gap-2.5 rounded-card border-l-[3px] border-l-ok bg-ok-soft px-4 py-3.5"
        data-testid="qr-logged-in"
      >
        <IconCheck size={16} className="shrink-0 text-ok" />
        <span className="text-[12.5px] leading-relaxed text-fg-2">
          扫上了，登录态正常。
          <span className="ml-1 text-fg-4">
            被挂起的排期项已经自动放回；之后每 10 分钟会自动巡检一次登录态。
          </span>
        </span>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3" data-testid="qr-stage">
      <span className="sw-label flex items-center gap-1.5">
        <IconQr size={12} />
        用小红书 App 扫这个码
      </span>
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start">
        <div className="flex h-[248px] w-[248px] shrink-0 items-center justify-center overflow-hidden rounded-xl border border-line bg-white p-3">
          {qr?.image_base64 ? (
            // 二维码是后端从 sidecar 代理来的 base64 PNG，不走图片优化
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={`data:image/png;base64,${qr.image_base64}`}
              alt="小红书登录二维码"
              data-testid="qr-image"
              className="h-full w-full object-contain"
            />
          ) : (
            <span className="px-4 text-center text-[11.5px] leading-relaxed text-fg-4">
              {failure ? "这里应该是二维码" : "正在取二维码"}
            </span>
          )}
        </div>

        <div className="flex min-w-0 flex-1 flex-col gap-2 text-[12px] text-fg-3">
          {failure ? (
            <div
              className="rounded-lg border-l-[3px] border-l-err bg-err-soft px-3 py-2.5"
              data-testid="qr-failure"
            >
              <div className="mb-1 flex items-center gap-1.5 text-[12.5px] font-medium text-err">
                <IconAlert size={13} />
                取不到二维码
              </div>
              <p className="text-[11.5px] leading-relaxed text-fg-2">{failure}</p>
            </div>
          ) : qr?.placeholder ? (
            <Badge tone="warn" data-testid="qr-placeholder">
              占位图 · 不是能扫的真二维码
            </Badge>
          ) : qr ? (
            <Badge tone="ok">真二维码 · 请用手机扫</Badge>
          ) : null}

          {qr ? <span className="sw-num">剩余有效期 {left} 秒，过期自动换新的</span> : null}
          <p className="leading-relaxed">
            用<strong className="font-medium text-fg-2">这个号本人</strong>的小红书 App
            扫。扫完这一格会自动变绿，不用刷新页面。
          </p>
          <p className="text-[11px] leading-relaxed text-fg-4">
            同一时间只能有一个登录会话，在别处登录会顶掉这里的登录。
          </p>
          <div className="flex items-center gap-2">
            <Button size="sm" onClick={() => void load()} loading={busy} data-testid="qr-refresh">
              <IconRefresh size={12} />
              换一张
            </Button>
            <span className="flex items-center gap-1.5 text-[11px] text-fg-4">
              <LiveDot tone="amber" pulse size={6} />
              {status ? `巡检于 ${fmtTime(status.checked_at)}` : "等待第一次巡检"}
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}

export function XhsOnboarding({
  account,
  onLoggedIn,
}: {
  account: AccountRow;
  onLoggedIn?: () => void;
}) {
  return (
    <div className="flex flex-col gap-4">
      <SidecarPanel accountId={account.id} />
      <div className="h-px bg-line" />
      <QrStage accountId={account.id} onLoggedIn={onLoggedIn} />
    </div>
  );
}

// -------------------------------------------------------------------- 抖音

export function DouyinOnboarding({ account }: { account: AccountRow }) {
  return (
    <GuideCard
      icon={<IconWindow size={13} />}
      title="抖音要在一台有图形界面的机器上接入"
      lead={
        <>
          抖音的上传器是一个<strong className="font-medium text-fg-2">有头浏览器</strong>
          ，必须常驻在你能看见屏幕的机器上（一般是你的 Mac）。服务器代替不了这一步——
          没有显示器就没有可扫的二维码。
        </>
      }
      steps={[
        {
          title: "在那台 Mac 上起上传器",
          code: "uv run python -m publishers.douyin serve --port 8710",
        },
        {
          title: "让 core 找得到它",
          code: "DOUYIN_SERVICE_URL=http://<那台机器的地址>:8710",
          note: "填进 core 那台机器的 .env，然后重启 core。",
        },
        {
          title: "回到这张卡点「打开宿主机登录窗口」",
          note: "窗口会在那台 Mac 上弹出来，人过去扫码；收到短信码可以在这里转发过去。",
        },
      ]}
      footer={
        <>
          发布前会读创作者中心页面上的昵称，跟这个号的{" "}
          <code className="sw-num rounded bg-muted px-1">identity_hint</code>（
          {String(account.extra?.identity_hint ?? "未填")}）比对，不一致直接拒发。
        </>
      }
    />
  );
}

// ------------------------------------------------------------------ 公众号

export function WechatOnboarding() {
  return (
    <GuideCard
      icon={<IconKey size={13} />}
      title="公众号没有扫码这一步，是凭据 + IP 白名单"
      lead={
        <>
          公众号走官方 API。凭据
          <strong className="font-medium text-fg-2">不进数据库、也不经过这个浏览器</strong>
          （docs/POLICY.md 红线），只能在 core 那台机器的环境变量里配。
        </>
      }
      steps={[
        {
          title: "在 core 那台机器的 .env 里填两个值",
          code: "WECHAT_APPID=wx...\nWECHAT_APPSECRET=...",
          note: "填完重启 core。这两个值这里看不到也改不了，是故意的。",
        },
        {
          title: "把服务器的出口 IP 加进公众号后台白名单",
          note: "「设置与开发 → 基本配置 → IP 白名单」。没加的话调用会直接报 errcode 40164。",
        },
        {
          title: "确认认证状态",
          code: "WECHAT_CERTIFIED=true",
          note: "未认证的号只能落草稿箱（2025-07 起权限回收），要人去后台点发布。",
        },
      ]}
      footer={
        <>
          配好之后回「系统 → 自检与任务」跑一次离线自检，结果会明确显示凭据和白名单是否通过。
        </>
      }
    />
  );
}

// ------------------------------------------------------------------ 公共件

function GuideCard({
  icon,
  title,
  lead,
  steps,
  footer,
}: {
  icon: React.ReactNode;
  title: string;
  lead: React.ReactNode;
  steps: { title: string; code?: string; note?: string }[];
  footer?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-3" data-testid="onboarding-guide">
      <div className="flex items-center gap-1.5 text-[13px] font-medium text-fg">
        {icon}
        {title}
      </div>
      <p className="text-[12px] leading-relaxed text-fg-3">{lead}</p>
      <ol className="flex flex-col gap-2.5">
        {steps.map((step, i) => (
          <li key={step.title} className="rounded-lg bg-muted px-3 py-2.5">
            <div className="flex items-baseline gap-2">
              <span className="sw-num text-[11px] text-primary">{i + 1}</span>
              <span className="text-[12.5px] text-fg-2">{step.title}</span>
            </div>
            {step.code ? (
              <pre className="sw-num mt-1.5 overflow-x-auto whitespace-pre-wrap rounded bg-muted-hover px-2.5 py-1.5 text-[11px] text-fg-2">
                {step.code}
              </pre>
            ) : null}
            {step.note ? (
              <p className="mt-1 text-[11.5px] leading-relaxed text-fg-4">{step.note}</p>
            ) : null}
          </li>
        ))}
      </ol>
      {footer ? (
        <p className="text-[11.5px] leading-relaxed text-fg-4">{footer}</p>
      ) : null}
    </div>
  );
}
