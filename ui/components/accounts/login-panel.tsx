"use client";

import * as React from "react";

import { IconKey, IconMessage, IconQr, IconRefresh, IconWindow } from "@/components/icons";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { FieldLabel, Input } from "@/components/ui/field";
import { LiveDot } from "@/components/ui/live-dot";
import { useToast } from "@/components/ui/toast";
import { apiFetch, ApiFailure, describeError } from "@/lib/api";
import { ACCOUNT_STATUS_LABEL, fmtTime, toneForAccount } from "@/lib/format";
import { POLL, useApi } from "@/lib/hooks";
import type { AccountRow, CodeResult, LoginStart, LoginStatus, QrCode } from "@/lib/types";

/**
 * 登录面板。三个平台三种形态：
 *  - 小红书：core 代理二维码，前端拼 data URI 显示，按 expires_in 到期重取；
 *  - 抖音：二维码在宿主机浏览器窗口里，core 只负责把窗口弹出来 + 转发短信码；
 *  - 公众号：没有扫码这回事，看的是凭据与 IP 白名单。
 *
 * 红线（docs/POLICY.md）：不做任何自动打码 / 验证码识别，二维码只呈现给人扫；
 * 验证码不落任何本地存储。
 */
export function LoginPanel({ account }: { account: AccountRow }) {
  const toast = useToast();
  const [pollOn, setPollOn] = React.useState(true);

  const { data: status } = useApi<LoginStatus>(
    account.supports_login ? `/accounts/${account.id}/login/status` : null,
    undefined,
    { refreshInterval: pollOn ? POLL.loginStatus : 0 },
  );

  React.useEffect(() => {
    if (status?.logged_in) setPollOn(false);
  }, [status?.logged_in]);

  if (!account.supports_login) {
    return (
      <p className="text-[12px] text-fg-4">这个平台的发布器没有登录动作，跳过。</p>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-2">
        <LiveDot
          tone={status?.logged_in ? "ok" : toneForAccount(account.status)}
          pulse={!status?.logged_in}
        />
        <span className="text-[12.5px] text-fg-2">
          {status?.logged_in ? "登录态正常" : (status?.detail || "登录态待确认")}
        </span>
        {status ? (
          <span className="sw-num text-[10.5px] text-fg-4">
            巡检于 {fmtTime(status.checked_at)}
          </span>
        ) : null}
      </div>

      {account.platform === "xhs" ? <QrLogin account={account} /> : null}
      {account.platform === "douyin" ? <HostWindowLogin account={account} /> : null}
      {account.platform === "wechat_mp" ? <WechatCredentials account={account} /> : null}

      {account.platform !== "wechat_mp" ? (
        <SmsCode accountId={account.id} onDone={(m) => toast.ok(m)} />
      ) : null}
    </div>
  );
}

function QrLogin({ account }: { account: AccountRow }) {
  const toast = useToast();
  const [qr, setQr] = React.useState<QrCode | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [left, setLeft] = React.useState(0);

  const load = React.useCallback(async () => {
    setBusy(true);
    try {
      const data = await apiFetch<QrCode>(`/accounts/${account.id}/login/qrcode`);
      setQr(data);
      setLeft(data.expires_in || 0);
    } catch (e) {
      if (e instanceof ApiFailure && e.code === "not_supported") setQr(null);
      else toast.err(describeError(e));
    } finally {
      setBusy(false);
    }
  }, [account.id, toast]);

  React.useEffect(() => {
    void load();
  }, [load]);

  // 到期倒计时；归零就自动重取一张（真 sidecar 会给真的有效期）
  React.useEffect(() => {
    if (left <= 0) return;
    const t = window.setInterval(() => setLeft((v) => Math.max(0, v - 1)), 1000);
    return () => window.clearInterval(t);
  }, [left]);

  React.useEffect(() => {
    if (qr && left === 0 && !busy) void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [left]);

  return (
    <div className="flex flex-col items-start gap-2.5">
      <span className="sw-label flex items-center gap-1.5">
        <IconQr size={12} />
        扫码登录
      </span>
      <div className="flex items-start gap-4">
        <div className="flex h-[190px] w-[190px] items-center justify-center overflow-hidden rounded-lg border border-line bg-white p-2">
          {qr?.image_base64 ? (
            // 二维码是后端代理来的 base64 PNG，不走图片优化
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={`data:image/png;base64,${qr.image_base64}`}
              alt="登录二维码"
              className="h-full w-full object-contain"
            />
          ) : (
            <span className="text-[11.5px] text-fg-4">二维码加载中</span>
          )}
        </div>
        <div className="flex flex-col gap-2 text-[12px] text-fg-3">
          {qr?.placeholder ? (
            <Badge tone="warn">占位图 · 不是能扫的真二维码</Badge>
          ) : (
            <Badge tone="ok">真二维码 · 请用手机扫</Badge>
          )}
          <span className="sw-num">剩余有效期 {left} 秒</span>
          <span>扫码成功后账号自动回 ok，被挂起的排期项一并放回。</span>
          <Button size="sm" onClick={() => void load()} loading={busy}>
            <IconRefresh size={12} />
            换一张
          </Button>
        </div>
      </div>
    </div>
  );
}

function HostWindowLogin({ account }: { account: AccountRow }) {
  const toast = useToast();
  const [state, setState] = React.useState<LoginStart | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [unsupported, setUnsupported] = React.useState(false);

  async function start() {
    setBusy(true);
    try {
      const res = await apiFetch<LoginStart>(`/accounts/${account.id}/login/start`, {
        method: "POST",
      });
      setState(res);
      toast.ok("已请求宿主机弹出登录窗口，请去那台机器上扫码");
    } catch (e) {
      if (e instanceof ApiFailure && e.code === "not_supported") {
        setUnsupported(true);
        toast.warn(e.message);
      } else {
        toast.err(describeError(e));
      }
    } finally {
      setBusy(false);
    }
  }

  const steps = ["idle", "opening", "waiting_user", "logged_in"];
  const current = state?.state ?? "idle";

  return (
    <div className="flex flex-col gap-2.5">
      <span className="sw-label flex items-center gap-1.5">
        <IconWindow size={12} />
        宿主机窗口登录
      </span>
      <p className="text-[12px] leading-relaxed text-fg-3">
        抖音的二维码在<strong className="font-medium text-fg-2">宿主机浏览器窗口</strong>
        里，core 不代理图片。点下面的按钮把窗口弹出来，然后去那台机器上扫码。
      </p>

      <ol className="flex flex-wrap items-center gap-1.5">
        {steps.map((s, i) => {
          const idx = steps.indexOf(current);
          const done = idx >= 0 && i <= idx;
          return (
            <li key={s} className="flex items-center gap-1.5">
              <Badge tone={done ? "amber" : "muted"}>
                {["未开始", "正在打开", "等待扫码", "已登录"][i]}
              </Badge>
              {i < steps.length - 1 ? <span className="text-fg-5">→</span> : null}
            </li>
          );
        })}
      </ol>

      {state?.detail ? (
        <p className="sw-num text-[11.5px] text-fg-3">{state.detail}</p>
      ) : null}

      <div>
        <Button onClick={start} loading={busy} disabled={unsupported}>
          <IconWindow size={13} />
          打开宿主机登录窗口
        </Button>
        {unsupported ? (
          <p className="mt-1.5 text-[11.5px] text-fg-4">
            当前实例的抖音发布器不支持这一步（可能是 fake 发布器）。
          </p>
        ) : null}
      </div>
    </div>
  );
}

function WechatCredentials({ account }: { account: AccountRow }) {
  const extra = (account.extra ?? {}) as Record<string, unknown>;
  const rows = Object.entries(extra).filter(([k]) => !k.startsWith("_"));
  return (
    <div className="flex flex-col gap-2.5">
      <span className="sw-label flex items-center gap-1.5">
        <IconKey size={12} />
        凭据与白名单
      </span>
      <p className="text-[12px] leading-relaxed text-fg-3">
        公众号走的是 API 凭据，没有扫码这一步。凭据本身不经过工作台——
        <code className="sw-num mx-1 rounded bg-muted px-1">Account.extra</code>
        里只有环境变量名。IP 白名单没配的话，发布会在调用时直接失败。
      </p>
      {rows.length > 0 ? (
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 rounded-lg bg-muted px-3 py-2.5">
          {rows.map(([k, v]) => (
            <React.Fragment key={k}>
              <dt className="sw-num text-[11.5px] text-fg-4">{k}</dt>
              <dd className="sw-num truncate text-[11.5px] text-fg-2">{String(v)}</dd>
            </React.Fragment>
          ))}
        </dl>
      ) : (
        <p className="text-[11.5px] text-fg-4">extra 是空的。</p>
      )}
    </div>
  );
}

function SmsCode({ accountId, onDone }: { accountId: string; onDone: (m: string) => void }) {
  const toast = useToast();
  const [code, setCode] = React.useState("");
  const [busy, setBusy] = React.useState(false);

  async function submit() {
    setBusy(true);
    try {
      const res = await apiFetch<CodeResult>(`/accounts/${accountId}/login/code`, {
        method: "POST",
        body: { code },
      });
      // 验证码提交完立刻从组件状态里抹掉，绝不落本地存储
      setCode("");
      onDone(
        res.forwarded
          ? `验证码已转发给发布器：${res.forward_detail}`
          : `验证码已进队列（待取 ${res.pending} 条）`,
      );
    } catch (e) {
      toast.err(describeError(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <FieldLabel htmlFor="sms-code" className="flex items-center gap-1.5">
        <IconMessage size={12} />
        短信验证码
      </FieldLabel>
      <div className="flex items-center gap-2">
        <Input
          id="sms-code"
          value={code}
          inputMode="numeric"
          autoComplete="one-time-code"
          maxLength={8}
          placeholder="6 位数字"
          onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
          className="sw-num w-32"
        />
        <Button onClick={submit} loading={busy} disabled={!code}>
          提交
        </Button>
      </div>
      <p className="text-[11px] text-fg-4">
        系统不做任何自动打码 / 验证码识别；验证码不落库、不写日志明文，也不进本地存储。
      </p>
    </div>
  );
}

export function AccountStatusBadge({ status }: { status: AccountRow["status"] }) {
  // 原来 needs_relogin 会让徽标闪。P13 撤掉了：这个号的卡片本来就整张排在
  // 「需要你现在处理」那一组的最前面，闪不闪都跑不掉，闪只是让人眼睛累。
  return <Badge tone={toneForAccount(status)}>{ACCOUNT_STATUS_LABEL[status] ?? status}</Badge>;
}
