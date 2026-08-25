"use client";

import { useRouter, useSearchParams } from "next/navigation";
import * as React from "react";

import { AccountActions } from "@/components/accounts/account-actions";
import { CreateAccountWizard } from "@/components/accounts/create-wizard";
import { AccountStatusBadge, LoginPanel } from "@/components/accounts/login-panel";
import {
  DouyinOnboarding,
  WechatOnboarding,
  XhsOnboarding,
} from "@/components/accounts/onboarding";
import { IconAlert, IconClock, IconPlus, IconRefresh, IconUsers } from "@/components/icons";
import { DataState } from "@/components/layout/data-state";
import { PageHeader } from "@/components/layout/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { FilterMenu } from "@/components/ui/filter-menu";
import { Panel } from "@/components/ui/panel";
import { LiveDot } from "@/components/ui/live-dot";
import { Modal } from "@/components/ui/modal";
import { Progress } from "@/components/ui/progress";
import { SkeletonRows } from "@/components/ui/skeleton";
import {
  ACCOUNT_STATUS_MEANING,
  fmtTime,
  PLATFORM_LABEL,
  toneForAccount,
} from "@/lib/format";
import { POLL, useApi } from "@/lib/hooks";
import type { AccountRow, Platform } from "@/lib/types";
import { cn } from "@/lib/utils";

const PLATFORM_FILTERS = [
  { value: "", label: "全部平台" },
  { value: "wechat_mp", label: "公众号" },
  { value: "xhs", label: "小红书" },
  { value: "douyin", label: "抖音" },
];

/**
 * 出事之后到底该做什么 —— 按 **状态 × 平台** 给指引，不是只按平台。
 *
 * needs_relogin（要人掏手机扫码）与 degraded（那台机器上的容器/上传器不见了）
 * 处置完全不同，混成一句"去登录"会让人白折腾一轮。
 */
const RECOVERY: Record<string, Record<Platform, { title: string; steps: string }>> = {
  needs_relogin: {
    xhs: {
      title: "扫码回来",
      steps:
        "core 会把 sidecar 的二维码代理过来。点开这张卡，用这个号本人的小红书 App 扫一下；二维码过期会自动换新的。",
    },
    douyin: {
      title: "去宿主机窗口扫码",
      steps:
        "抖音的二维码在宿主机的浏览器窗口里，core 不代理图片。点开卡片按「打开宿主机登录窗口」，再去那台机器上扫；收到短信码可以在卡片里转发给发布器。",
    },
    wechat_mp: {
      title: "查凭据与 IP 白名单",
      steps:
        "公众号走 API 凭据，没有扫码这一步。掉线通常是凭据过期或调用方 IP 不在白名单里——凭据不经过工作台，去 core 那台机器的环境变量里核对。",
    },
  },
  degraded: {
    xhs: {
      title: "sidecar 连不上",
      steps:
        "不是登录过期，是这个号的容器没在跑或者端口不通。点开卡片看 sidecar 那一块，直接在这里起它；起不来的话去服务器 docker logs 看一眼。",
    },
    douyin: {
      title: "上传器连不上",
      steps:
        "宿主机上的抖音上传器没在跑，或者 DOUYIN_SERVICE_URL 指错了。去那台机器上重新起 `uv run python -m publishers.douyin serve`。",
    },
    wechat_mp: {
      title: "接口调不通",
      steps:
        "公众号 API 调不通，可能是网络或凭据问题。去「系统 → 自检与任务」跑一次自检，会显示是哪一环出的问题。",
    },
  },
  banned: {
    xhs: {
      title: "平台侧封禁",
      steps: "系统不会自动恢复被封的号。先去平台确认申诉结果，处理完再手工把状态改回来。",
    },
    douyin: {
      title: "平台侧封禁",
      steps: "系统不会自动恢复被封的号。先去平台确认申诉结果，处理完再手工把状态改回来。",
    },
    wechat_mp: {
      title: "平台侧封禁",
      steps: "系统不会自动恢复被封的号。先去平台确认申诉结果，处理完再手工把状态改回来。",
    },
  },
};

/** 需要人现在动手的状态。degraded 也算——它意味着这个号现在发不出去。 */
const NEEDS_ACTION = new Set(["needs_relogin", "banned", "degraded"]);

export default function AccountsPage() {
  return (
    <React.Suspense fallback={<SkeletonRows rows={6} />}>
      <AccountsGrid />
    </React.Suspense>
  );
}

/**
 * 账号 —— 从建号到扫码到出稿，一条链都在这一页里。
 *
 * 页面顺序就是处理顺序：先是"需要你现在处理的账号"（带明确行动指引），
 * 再是全部账号的配额与窗口。掉线的账号会把排期项挂起，扫码回来后自动放回。
 */
function AccountsGrid() {
  const router = useRouter();
  const search = useSearchParams();
  const openId = search.get("id") ?? "";
  const wantsNew = search.get("new") === "1";
  const [platform, setPlatform] = React.useState("");
  const [wizardOpen, setWizardOpen] = React.useState(false);

  const { data, error, isLoading, mutate } = useApi<AccountRow[]>(
    "/accounts",
    { platform },
    { refreshInterval: POLL.accounts },
  );
  const { data: detail, mutate: mutateDetail } = useApi<AccountRow>(
    openId ? `/accounts/${openId}` : null,
  );

  React.useEffect(() => {
    if (wantsNew) setWizardOpen(true);
  }, [wantsNew]);

  const accounts = data ?? [];
  const attention = accounts.filter((a) => NEEDS_ACTION.has(a.status));
  const rest = accounts.filter((a) => !NEEDS_ACTION.has(a.status));
  const paused = accounts.filter((a) => a.status === "suspended").length;

  const open = React.useCallback(
    (id: string) => router.replace(`/accounts/?id=${encodeURIComponent(id)}`),
    [router],
  );
  const refresh = React.useCallback(() => {
    void mutate();
    void mutateDetail();
  }, [mutate, mutateDetail]);

  return (
    <div className="flex w-full max-w-5xl flex-col gap-4 p-4 md:p-6">
      <PageHeader
        title="账号"
        emphasis="健康与配额"
        actions={
          <>
            <FilterMenu
              label="平台"
              value={platform}
              onChange={setPlatform}
              options={PLATFORM_FILTERS}
            />
            <Button onClick={() => void mutate()}>
              <IconRefresh size={13} />
              刷新
            </Button>
            <Button variant="primary" onClick={() => setWizardOpen(true)} data-testid="add-account">
              <IconPlus size={13} />
              添加账号
            </Button>
          </>
        }
      />

      <DataState isLoading={isLoading} error={error} onRetry={() => void mutate()} rows={4}>
        {accounts.length === 0 ? (
          <EmptyState
            icon={<IconUsers />}
            title="还没有账号"
            description="点右上角「添加账号」建第一个：会同时写进 accounts.yaml 和数据库，不用再去服务器敲命令。掉线的账号会把它名下的排期项挂起，登录回来后自动放回。"
            action={
              <Button variant="primary" onClick={() => setWizardOpen(true)} data-testid="add-account-empty">
                <IconPlus size={13} />
                添加账号
              </Button>
            }
          />
        ) : (
          <>
            {attention.length > 0 ? (
              <section className="mb-5" data-testid="attention-section">
                <h2 className="mb-2 flex items-center gap-2 text-[13.5px] font-medium text-fg">
                  <LiveDot tone="err" pulse size={7} />
                  需要你现在处理
                  <Badge tone="err">{attention.length}</Badge>
                </h2>
                <div className="grid gap-3 lg:grid-cols-2">
                  {attention.map((a) => (
                    <RecoveryCard
                      key={a.id}
                      account={a}
                      onOpen={() => open(a.id)}
                      onChanged={refresh}
                    />
                  ))}
                </div>
              </section>
            ) : (
              <div
                className="mb-5 flex items-center gap-2.5 rounded-card bg-muted px-4 py-3"
                data-testid="accounts-all-clear"
              >
                <LiveDot tone="ok" size={7} />
                <span className="text-[12.5px] text-fg-2">
                  没有需要扫码、补验证码或去看机器的账号。
                  {paused > 0 ? (
                    <span className="ml-1 text-fg-4">
                      （另有 {paused} 个号是你自己停用的，不算异常。）
                    </span>
                  ) : null}
                </span>
              </div>
            )}

            <h2 className="sw-label mb-2">
              {attention.length > 0 ? "其余账号" : "全部账号"}（{rest.length}）
            </h2>
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {rest.map((a) => (
                <AccountCard
                  key={a.id}
                  account={a}
                  onOpen={() => open(a.id)}
                  onChanged={refresh}
                />
              ))}
            </div>
          </>
        )}
      </DataState>

      <Modal
        open={Boolean(openId)}
        onClose={() => router.replace("/accounts/")}
        title={detail?.name ?? openId}
        description={
          detail
            ? `${PLATFORM_LABEL[detail.platform]} · ${detail.id} · 窗口 ${detail.policy.publish_windows}（${detail.policy.timezone}）`
            : undefined
        }
        className="max-w-2xl"
      >
        {detail ? (
          <div className="flex flex-col gap-4">
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              <Metric label="今日已发" value={`${detail.used_today}/${detail.policy.daily_limit}`} />
              <Metric label="待审" value={String(detail.pending_review ?? 0)} />
              <Metric label="已排期" value={String(detail.scheduled ?? 0)} />
              <Metric label="已挂起" value={String(detail.suspended ?? 0)} />
            </div>

            <p className="text-[12px] leading-relaxed text-fg-3" data-testid="status-meaning">
              {ACCOUNT_STATUS_MEANING[detail.status] ?? detail.status}
            </p>

            <AccountActions
              account={detail}
              onOpen={() => undefined}
              onChanged={refresh}
              compact
            />

            <div className="h-px bg-line" />

            {detail.platform === "xhs" ? <XhsOnboarding account={detail} onLoggedIn={refresh} /> : null}
            {detail.platform === "douyin" ? (
              <>
                <DouyinOnboarding account={detail} />
                <div className="h-px bg-line" />
                <LoginPanel account={detail} />
              </>
            ) : null}
            {detail.platform === "wechat_mp" ? <WechatOnboarding /> : null}
          </div>
        ) : (
          <SkeletonRows rows={5} />
        )}
      </Modal>

      <CreateAccountWizard
        open={wizardOpen}
        onClose={() => {
          setWizardOpen(false);
          if (wantsNew) router.replace("/accounts/");
          void mutate();
        }}
        onCreated={() => void mutate()}
      />
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md bg-muted px-3 py-2">
      <div className="sw-label mb-0.5">{label}</div>
      <div className="sw-num text-[17px] font-medium text-fg">{value}</div>
    </div>
  );
}

/** 出事账号的待办卡：状态 + 影响 + 该做什么 + 动作。 */
function RecoveryCard({
  account,
  onOpen,
  onChanged,
}: {
  account: AccountRow;
  onOpen: () => void;
  onChanged: () => void;
}) {
  const guide = RECOVERY[account.status]?.[account.platform];
  const suspendedItems = account.suspended ?? 0;
  const tone = toneForAccount(account.status);
  return (
    <Panel
      className="border-l-2 border-l-err px-4 py-3.5"
      data-testid="account-card"
      data-account-id={account.id}
      data-account-status={account.status}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="sw-label">{PLATFORM_LABEL[account.platform] ?? account.platform}</div>
          <div className="mt-1 truncate text-[15px] font-medium text-fg">{account.name}</div>
          <div className="sw-num mt-0.5 truncate text-[11px] text-fg-4">{account.id}</div>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          <LiveDot tone={tone} pulse />
          <AccountStatusBadge status={account.status} />
        </div>
      </div>

      {guide ? (
        <div
          className={cn(
            "mt-3 rounded-md border-l-[3px] px-3 py-2.5",
            tone === "warn" ? "border-l-warn bg-warn-soft" : "border-l-err bg-err-soft",
          )}
        >
          <div
            className={cn(
              "mb-1 flex items-center gap-1.5 text-[12.5px] font-medium",
              tone === "warn" ? "text-warn" : "text-err",
            )}
          >
            <IconAlert size={13} />
            {guide.title}
          </div>
          <p className="text-[11.5px] leading-relaxed text-fg-2">{guide.steps}</p>
          {suspendedItems > 0 ? (
            <p className="mt-1.5 text-[11.5px] text-fg-3">
              这个号名下有 <span className="sw-num text-fg-2">{suspendedItems}</span>{" "}
              条已挂起的排期，恢复后会自动放回。
            </p>
          ) : null}
        </div>
      ) : null}

      <div className="mt-3 flex items-center justify-between gap-2">
        <span className="sw-num text-[11px] text-fg-4">
          上次发布 {fmtTime(account.last_published_at)}
        </span>
        <Button variant="primary" size="sm" onClick={onOpen} data-testid="recover-button">
          去处理
        </Button>
      </div>
      <div className="mt-2">
        <AccountActions account={account} onOpen={onOpen} onChanged={onChanged} compact />
      </div>
    </Panel>
  );
}

function AccountCard({
  account,
  onOpen,
  onChanged,
}: {
  account: AccountRow;
  onOpen: () => void;
  onChanged: () => void;
}) {
  const pct =
    account.policy.daily_limit > 0 ? (account.used_today / account.policy.daily_limit) * 100 : 0;
  const tone = toneForAccount(account.status);
  const paused = account.status === "suspended";
  return (
    <Panel
      className={cn("px-4 py-3.5 transition-colors hover:bg-row-hover", paused && "opacity-70")}
      data-testid="account-card"
      data-account-id={account.id}
      data-account-status={account.status}
    >
      <div
        role="button"
        tabIndex={0}
        className="cursor-pointer"
        onClick={onOpen}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onOpen();
          }
        }}
      >
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <div className="sw-label">{PLATFORM_LABEL[account.platform] ?? account.platform}</div>
            <div className="mt-1 truncate text-[15px] font-medium text-fg">{account.name}</div>
            <div className="sw-num mt-0.5 truncate text-[11px] text-fg-4">{account.id}</div>
          </div>
          <div className="flex shrink-0 items-center gap-1.5">
            <LiveDot tone={tone} />
            <AccountStatusBadge status={account.status} />
          </div>
        </div>

        <div className="mt-3.5">
          <div className="mb-1 flex items-baseline justify-between">
            <span className="sw-label">今日配额</span>
            <span className="sw-num text-[12.5px] text-fg-2">
              {account.used_today}
              <span className="text-fg-4">/{account.policy.daily_limit}</span>
            </span>
          </div>
          <Progress value={pct} tone={pct >= 100 ? "err" : "amber"} label="今日配额占用" />
        </div>

        <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[11.5px]">
          <dt className="flex items-center gap-1 text-fg-4">
            <IconClock size={11} />
            发布窗口
          </dt>
          <dd className="sw-num truncate text-fg-2">{account.policy.publish_windows}</dd>
          <dt className="text-fg-4">最小间隔</dt>
          <dd className="sw-num text-fg-2">{account.policy.min_interval_minutes} 分钟</dd>
          <dt className="text-fg-4">上次发布</dt>
          <dd className="sw-num text-fg-2">{fmtTime(account.last_published_at)}</dd>
        </dl>
      </div>

      <div className="mt-3 border-t border-line pt-3">
        <AccountActions account={account} onOpen={onOpen} onChanged={onChanged} compact />
      </div>
    </Panel>
  );
}
