"use client";

import { useRouter } from "next/navigation";
import * as React from "react";

import {
  AccountForm,
  toBody,
  validate,
  type AccountFormValue,
  type FormErrors,
} from "@/components/accounts/account-form";
import { IconAlert, IconEdit, IconImage, IconPower, IconQr, IconSpark } from "@/components/icons";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/field";
import { Modal } from "@/components/ui/modal";
import { SegmentedControl } from "@/components/ui/segmented";
import { useToast } from "@/components/ui/toast";
import { apiFetch, ApiFailure, describeError } from "@/lib/api";
import { useApi } from "@/lib/hooks";
import type {
  AccountRow,
  AccountWriteResult,
  GenerateRequest,
  GenerateResult,
  ImagegenInfo,
  Platform,
} from "@/lib/types";
import { cn } from "@/lib/utils";
import { toDrafts } from "@/lib/windows";

/**
 * 配图张数可选项（P14.B4：勾选框 + 张数选择器合并成一条 Segmented）。
 * 上限与后端 `MAX_ILLUSTRATIONS` 对齐；公众号题图与抖音封面只用得上一张，
 * 只给「无 / 1」两档，别摆四个选项让人以为能配四张封面。
 */
function illustrationOptions(singleImage: boolean): { value: string; label: string }[] {
  const max = singleImage ? 1 : 4;
  const opts = [{ value: "0", label: "无" }];
  for (let n = 1; n <= max; n++) opts.push({ value: String(n), label: String(n) });
  return opts;
}

/** 账号行 → 编辑表单的初值。 */
export function formValueOf(account: AccountRow): AccountFormValue {
  const extra = (account.extra ?? {}) as Record<string, unknown>;
  return {
    name: account.name,
    identity_hint: String(extra.identity_hint ?? ""),
    windows: toDrafts(account.policy.publish_windows),
    min_interval_minutes: String(account.policy.min_interval_minutes),
    daily_limit: String(account.policy.daily_limit),
    daily_target: String(account.policy.daily_target),
    timezone: account.policy.timezone,
    persona: String(extra.persona ?? ""),
  };
}

/**
 * 登录按钮的字面。三个平台的"登录"根本不是同一件事，别都写成「扫码」——
 * 公众号压根没有码可扫，写着扫码就是骗人。
 */
const LOGIN_LABEL: Record<Platform, (urgent: boolean) => string> = {
  xhs: (urgent) => (urgent ? "去扫码" : "重新扫码"),
  douyin: (urgent) => (urgent ? "去宿主机登录" : "宿主机登录"),
  wechat_mp: () => "看接入说明",
};

/**
 * 账号卡上的那排动作：出一条稿 / 登录 / 编辑 / 停用启用。
 *
 * 每个动作的失败态都要落到界面上（吐司里是后端原话）：出稿超额说"今天已经出了几条"，
 * sidecar 没起来说"去看那台机器"，绝不静默失败。
 */
export function AccountActions({
  account,
  onOpen,
  onChanged,
  compact,
}: {
  account: AccountRow;
  /** 打开这个号的详情弹窗（扫码 / sidecar 都在里面）。 */
  onOpen: () => void;
  onChanged: () => void;
  compact?: boolean;
}) {
  const router = useRouter();
  const toast = useToast();
  const [busy, setBusy] = React.useState("");
  const [editing, setEditing] = React.useState(false);
  const [generateOpen, setGenerateOpen] = React.useState(false);
  const [topicReason, setTopicReason] = React.useState("");

  const suspended = account.status === "suspended";
  const size = compact ? ("sm" as const) : ("md" as const);

  async function generate(body: GenerateRequest) {
    setBusy("generate");
    try {
      const res = await apiFetch<GenerateResult>(`/accounts/${account.id}/generate`, {
        method: "POST",
        body,
      });
      toast.ok(`${res.message}（用了 ${res.tokens_used} tokens，${res.elapsed_s} 秒）`);
      setGenerateOpen(false);
      setTopicReason("");
      onChanged();
      if (res.content_item_id) {
        // 直接把人送到审核台并定位到这条 —— 出稿的下一步一定是看它
        router.push(`/review/?id=${encodeURIComponent(res.content_item_id)}`);
      }
      // 配图降级不是失败，但人要知道这条稿没有真实照片，否则会以为开关没生效
      if (body.illustrations && res.illustrations === 0) {
        const why = res.warnings.find((w) => w.includes("配图")) ?? "";
        toast.err(why || "这条稿没配上图，去系统页看看生图是否可用。");
      }
    } catch (e) {
      const message = describeError(e);
      // 选题池空是**可以就地救回来**的失败：与其让人对着一句报错发呆，
      // 不如把输入框推到他面前，直接给这次生成指定一个题目
      if (e instanceof ApiFailure && e.code === "generation_failed" && message.includes("选题池")) {
        setTopicReason(message);
        setGenerateOpen(true);
      } else {
        toast.err(message);
      }
    } finally {
      setBusy("");
    }
  }

  async function toggleActive() {
    const action = suspended ? "reactivate" : "deactivate";
    setBusy(action);
    try {
      const res = await apiFetch<AccountWriteResult>(`/accounts/${account.id}/${action}`, {
        method: "POST",
        body: {},
      });
      toast.ok(res.message);
      onChanged();
    } catch (e) {
      toast.err(describeError(e));
    } finally {
      setBusy("");
    }
  }

  return (
    <>
      {/*
        P13 的按钮纪律：**带字的只留高频两个，其余降成图标钮**。
        原来一排四个等宽文字按钮，六张卡并排就是二十四个按钮、还会折行，
        卡片高度跟着账号数抖；而且「出一条稿」当时是实心琥珀 —— 一屏六块
        实心主色，主色就不再是主色了。现在实心只留给页头那颗「添加账号」。

        编辑 / 停用降成图标钮而不是收进「⋯」菜单：菜单是 portal 挂到 body 的，
        收进去就不再是"这张卡里的东西"，卡片作用域内取不到它们
        （e2e 正是按 `card.getByTestId(...)` 点的，这不是迁就测试，
        而是测试恰好钉住了"操作必须属于它那张卡"这条语义）。
      */}
      <div className="flex flex-wrap items-center gap-1.5" data-testid="account-actions">
        <Button
          size={size}
          data-testid="generate-button"
          loading={busy === "generate"}
          disabled={suspended}
          title={suspended ? "这个号停用中，先启用再出稿" : "跑一次生成链，产出一条待审稿"}
          onClick={() => {
            setTopicReason("");
            setGenerateOpen(true);
          }}
        >
          <IconSpark size={12} className="text-primary" />
          生成稿件
        </Button>
        {account.supports_login ? (
          <Button size={size} data-testid="relogin-button" onClick={onOpen}>
            <IconQr size={12} />
            {LOGIN_LABEL[account.platform](account.status === "needs_relogin")}
          </Button>
        ) : null}
        <Button
          size="icon"
          variant="ghost"
          data-testid="edit-button"
          title="编辑"
          aria-label="编辑账号"
          onClick={() => setEditing(true)}
        >
          <IconEdit size={14} />
        </Button>
        <Button
          size="icon"
          // 停用是可逆的（历史都还在），别染成危险红把它演成删除
          variant="ghost"
          data-testid="toggle-active-button"
          title={suspended ? "启用" : "停用"}
          aria-label={suspended ? "启用账号" : "停用账号"}
          loading={busy === "deactivate" || busy === "reactivate"}
          onClick={() => void toggleActive()}
        >
          <IconPower size={14} />
        </Button>
      </div>

      <EditAccountModal
        account={account}
        open={editing}
        onClose={() => setEditing(false)}
        onSaved={onChanged}
      />

      <GenerateModal
        open={generateOpen}
        platform={account.platform}
        reason={topicReason}
        busy={busy === "generate"}
        onClose={() => setGenerateOpen(false)}
        onSubmit={(body) => void generate(body)}
      />
    </>
  );
}

/**
 * 出稿弹层：选题（可选）+ 配图开关与张数。
 *
 * 做成弹层而不是"点一下直接跑"，是因为**这一下要花钱**：一条稿几万 token 起步，
 * 开了配图还要按张烧生图额度。多点一次的代价，换的是人看得见自己要付什么。
 *
 * 选题池空了的补救也合并在这里：同一个弹层多一条说明，不必再弹第二个框。
 */
function GenerateModal({
  open,
  platform,
  reason,
  busy,
  onClose,
  onSubmit,
}: {
  open: boolean;
  platform: Platform;
  /** 非空 = 上一次因为选题池空失败了，把原因摆在人面前 */
  reason: string;
  busy: boolean;
  onClose: () => void;
  onSubmit: (body: GenerateRequest) => void;
}) {
  // 弹层打开时才拉：这个端点不发网络请求，但也没必要在列表页反复问
  const { data: imagegen } = useApi<ImagegenInfo>(open ? "/system/imagegen" : null);
  const [topic, setTopic] = React.useState("");
  // 0 = 不配图；开关 + 张数选择器合并成这一个数（P14.B4）
  const [illustrations, setIllustrations] = React.useState(0);

  React.useEffect(() => {
    if (open) setTopic("");
  }, [open]);

  // 公众号题图与抖音封面只用得上一张，张数选择对它们没有意义
  const singleImage = platform !== "xhs";
  const ready = imagegen?.ready ?? false;
  const exhausted = ready && (imagegen?.remaining ?? 0) <= 0;
  const canPickImages = ready && !exhausted;

  // 服务端说了算：默认张数与"能不能配图"都以 /system/imagegen 为准
  React.useEffect(() => {
    if (!imagegen) return;
    const defaultReady = imagegen.ready && imagegen.default_count > 0 && (imagegen.remaining ?? 0) > 0;
    setIllustrations(defaultReady ? (singleImage ? 1 : imagegen.default_count) : 0);
  }, [imagegen, singleImage]);

  function submit() {
    const body: GenerateRequest = {
      illustrations: canPickImages ? illustrations : 0,
    };
    if (topic.trim()) body.topic = topic.trim();
    onSubmit(body);
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="生成稿件"
      description="跑一次完整生成链：选题 → 写稿 → 配图 → 机器审核 → 进人工队列。"
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            取消
          </Button>
          <Button
            variant="primary"
            loading={busy}
            data-testid="generate-submit"
            onClick={submit}
          >
            开始生成
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        {reason ? (
          <div
            className="flex items-start gap-2 rounded-lg border-l-[3px] border-l-warn bg-warn-soft px-3 py-2.5"
            data-testid="topic-reason"
            role="alert"
          >
            <IconAlert size={13} className="mt-[2px] shrink-0 text-warn" />
            <p className="text-[11.5px] leading-relaxed text-fg-2">{reason}</p>
          </div>
        ) : null}

        <div>
          <p className="sw-label mb-1.5">选题（可留空）</p>
          <Input
            autoFocus
            value={topic}
            maxLength={200}
            data-testid="topic-input"
            placeholder="例如「租房不打孔怎么收纳」"
            onChange={(e) => setTopic(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !busy) submit();
            }}
          />
          <p className="mt-1.5 text-[11.5px] leading-relaxed text-fg-4">
            填了就跳过选题 Agent 直接写稿；留空则从热榜里挑一个。
          </p>
        </div>

        <div className="rounded-lg bg-muted px-3 py-2.5">
          {canPickImages ? (
            <>
              <div className="flex items-center justify-between gap-3">
                <span className="inline-flex items-center gap-1.5 text-[12.5px] text-fg-2">
                  <IconImage size={12} />
                  配图
                </span>
                <SegmentedControl
                  label="配图张数"
                  data-testid="illustration-count"
                  value={String(illustrations)}
                  onChange={(v) => setIllustrations(Number(v))}
                  options={illustrationOptions(singleImage)}
                  className={busy ? "pointer-events-none opacity-60" : undefined}
                />
              </div>
              <p className="mt-1.5 text-[11px] leading-relaxed text-fg-4">
                {singleImage
                  ? "生成一张真实照片质感的图当题图/封面底，标题仍由模板排版。"
                  : "文字卡之后追加真实照片质感的配图，封面仍是标题卡。"}
              </p>
            </>
          ) : null}
          {/*
            生图不可用或额度用完时，不渲染一个灰掉的控件让人自己猜为什么点不动——
            直接不渲染那个控件，只留一行读原因的说明（P14.B4）
          */}
          {imagegen ? (
            <p
              className={cn(
                "text-[11px] leading-relaxed text-fg-4",
                canPickImages && "mt-2",
              )}
              data-testid="imagegen-note"
            >
              {canPickImages ? (
                <>
                  {imagegen.model} · 今天已用 {imagegen.used_today} / {imagegen.daily_limit} 张
                </>
              ) : exhausted ? (
                <>今天的生图额度（{imagegen.daily_limit} 张）已经用完了，这条稿只出文字版式。</>
              ) : (
                // 不可用时必须说清楚为什么，别只给一个灰掉的控件
                <>
                  {imagegen.reason}
                  {imagegen.hint ? ` ${imagegen.hint}` : ""}
                </>
              )}
            </p>
          ) : null}
        </div>
      </div>
    </Modal>
  );
}

export function EditAccountModal({
  account,
  open,
  onClose,
  onSaved,
}: {
  account: AccountRow;
  open: boolean;
  onClose: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const [value, setValue] = React.useState<AccountFormValue>(() => formValueOf(account));
  const [errors, setErrors] = React.useState<FormErrors>({});
  const [serverError, setServerError] = React.useState("");
  const [busy, setBusy] = React.useState(false);

  React.useEffect(() => {
    if (open) {
      setValue(formValueOf(account));
      setErrors({});
      setServerError("");
    }
  }, [open, account]);

  async function save() {
    const found = validate(account.platform, value);
    setErrors(found);
    if (Object.keys(found).length > 0) return;
    setBusy(true);
    setServerError("");
    try {
      const res = await apiFetch<AccountWriteResult>(`/accounts/${account.id}`, {
        method: "PATCH",
        body: toBody(value),
      });
      toast.ok(res.message);
      onSaved();
      onClose();
    } catch (e) {
      setServerError(describeError(e));
      if (e instanceof ApiFailure && e.code === "invalid_window") {
        setErrors((prev) => ({ ...prev, windows: e.message }));
      }
      if (e instanceof ApiFailure && e.code === "limit_above_ceiling") {
        setErrors((prev) => ({ ...prev, daily_limit: e.message }));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={`编辑 ${account.name}`}
      description={`${account.id} · 平台和 id 不能改（改了就是另一个号，历史内容会对不上）`}
      className="max-w-2xl"
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            取消
          </Button>
          <Button variant="primary" onClick={() => void save()} loading={busy} data-testid="edit-save">
            保存
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        {serverError ? (
          <div
            className="flex items-start gap-2 rounded-lg border-l-[3px] border-l-err bg-err-soft px-3 py-2.5"
            data-testid="edit-error"
            role="alert"
          >
            <IconAlert size={13} className="mt-[2px] shrink-0 text-err" />
            <p className="text-[11.5px] leading-relaxed text-fg-2">{serverError}</p>
          </div>
        ) : null}
        <AccountForm
          platform={account.platform}
          value={value}
          onChange={setValue}
          errors={errors}
        />
        <p className="text-[11px] leading-relaxed text-fg-4">
          保存会同时改 accounts.yaml 和数据库，两边始终一致；只重写这一条，别的账号和注释不动。
        </p>
      </div>
    </Modal>
  );
}

export default AccountActions;
