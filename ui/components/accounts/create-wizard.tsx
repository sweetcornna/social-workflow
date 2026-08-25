"use client";

import * as React from "react";

import {
  AccountForm,
  defaultsFor,
  toBody,
  validate,
  type AccountFormValue,
  type FormErrors,
} from "@/components/accounts/account-form";
import {
  DouyinOnboarding,
  WechatOnboarding,
  XhsOnboarding,
} from "@/components/accounts/onboarding";
import {
  IconAlert,
  IconArrowRight,
  IconCheck,
  IconImage,
  IconKey,
  IconVideo,
} from "@/components/icons";
import { Button } from "@/components/ui/button";
import { Modal } from "@/components/ui/modal";
import { useToast } from "@/components/ui/toast";
import { apiFetch, ApiFailure, describeError } from "@/lib/api";
import { PLATFORM_LABEL } from "@/lib/format";
import type { AccountRow, AccountWriteResult, Platform } from "@/lib/types";
import { cn } from "@/lib/utils";

/** 平台选择卡的说明。写清楚"选了它接下来要干什么"，而不是只放个图标。 */
const PLATFORM_CARDS: {
  platform: Platform;
  icon: React.ReactNode;
  what: string;
  next: string;
}[] = [
  {
    platform: "xhs",
    icon: <IconImage size={17} />,
    what: "图文笔记。一个号一个 sidecar 容器，登录态在容器自己的 volume 里。",
    next: "建完就在这里扫码，几分钟能走完。",
  },
  {
    platform: "douyin",
    icon: <IconVideo size={17} />,
    what: "口播短视频。上传器是有头浏览器，常驻在你自己的机器上。",
    next: "建完要去那台机器起上传器，服务器代替不了。",
  },
  {
    platform: "wechat_mp",
    icon: <IconKey size={17} />,
    what: "公众号长文。走官方 API，没有扫码，靠凭据 + IP 白名单。",
    next: "建完要去 core 那台机器的 .env 里配凭据。",
  },
];

type Step = "platform" | "form" | "onboard";

/**
 * 「添加账号」向导。
 *
 * 三步，对话式：**选平台 → 填表 → 接入**。第三步按平台分叉，能在浏览器里做完的
 * （小红书）就做完，做不完的（抖音 / 公众号）如实告诉人去哪台机器做什么，
 * 绝不给一个点了没反应的按钮。
 *
 * 建号本身在第二步就已经落库了 —— 第三步失败（sidecar 起不来、二维码取不到）
 * 不会让账号消失，人可以关掉弹窗、修好那台机器、回来点「重新扫码」。
 */
export function CreateAccountWizard({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated?: (account: AccountRow) => void;
}) {
  const toast = useToast();
  const [step, setStep] = React.useState<Step>("platform");
  const [platform, setPlatform] = React.useState<Platform>("xhs");
  const [value, setValue] = React.useState<AccountFormValue>(() => defaultsFor("xhs"));
  const [errors, setErrors] = React.useState<FormErrors>({});
  const [serverError, setServerError] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [created, setCreated] = React.useState<AccountRow | null>(null);
  const [warnings, setWarnings] = React.useState<string[]>([]);

  const reset = React.useCallback(() => {
    setStep("platform");
    setPlatform("xhs");
    setValue(defaultsFor("xhs"));
    setErrors({});
    setServerError("");
    setCreated(null);
    setWarnings([]);
  }, []);

  React.useEffect(() => {
    if (open) reset();
  }, [open, reset]);

  function pick(next: Platform) {
    setPlatform(next);
    setValue(defaultsFor(next));
    setErrors({});
    setStep("form");
  }

  async function submit() {
    const found = validate(platform, value);
    setErrors(found);
    if (Object.keys(found).length > 0) return;

    setBusy(true);
    setServerError("");
    try {
      const res = await apiFetch<AccountWriteResult>("/accounts", {
        method: "POST",
        body: { platform, ...toBody(value) },
      });
      setCreated(res.account);
      setWarnings(res.warnings ?? []);
      setStep("onboard");
      onCreated?.(res.account);
      toast.ok(res.message);
    } catch (e) {
      // 后端的报错都是人话（窗口写错会给例子、超硬顶会说硬顶是多少），原样显示
      setServerError(describeError(e));
      if (e instanceof ApiFailure && e.code === "invalid_window") {
        setErrors((prev) => ({ ...prev, windows: e.message }));
      }
      if (e instanceof ApiFailure && e.code === "limit_above_ceiling") {
        setErrors((prev) => ({ ...prev, daily_limit: e.message }));
      }
      if (e instanceof ApiFailure && e.code === "identity_hint_required") {
        setErrors((prev) => ({ ...prev, identity_hint: e.message }));
      }
    } finally {
      setBusy(false);
    }
  }

  const titles: Record<Step, string> = {
    platform: "添加账号",
    form: `新建${PLATFORM_LABEL[platform]}账号`,
    onboard: created ? `${created.name} · 接入` : "接入",
  };
  const descriptions: Record<Step, string> = {
    platform: "先说清楚是哪个平台 —— 接下来要做的事完全不一样。",
    form: "这些都写进 accounts.yaml，之后随时能改。",
    onboard: created ? `${created.id} · 已经在台账和库里了` : "",
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={titles[step]}
      description={descriptions[step]}
      className="max-w-2xl"
      footer={
        step === "form" ? (
          <>
            <Button variant="ghost" onClick={() => setStep("platform")} disabled={busy}>
              上一步
            </Button>
            <Button variant="primary" onClick={() => void submit()} loading={busy} data-testid="wizard-submit">
              创建账号
              <IconArrowRight size={13} />
            </Button>
          </>
        ) : step === "onboard" ? (
          <Button variant="primary" onClick={onClose} data-testid="wizard-done">
            <IconCheck size={13} />
            完成
          </Button>
        ) : null
      }
    >
      <StepRail step={step} />

      {step === "platform" ? (
        <div className="mt-3 grid gap-2">
          {PLATFORM_CARDS.map((card) => (
            <button
              key={card.platform}
              type="button"
              data-testid={`pick-platform-${card.platform}`}
              onClick={() => pick(card.platform)}
              className={cn(
                "group flex items-start gap-3 rounded-card border px-4 py-3 text-left",
                "border-line bg-muted transition-colors hover:bg-muted-hover",
              )}
            >
              <span className="mt-[2px] shrink-0 text-primary">{card.icon}</span>
              <span className="min-w-0 flex-1">
                <span className="block text-[14px] text-fg">
                  {PLATFORM_LABEL[card.platform]}
                </span>
                <span className="mt-0.5 block text-[11.5px] leading-relaxed text-fg-3">
                  {card.what}
                </span>
                <span className="mt-1 block text-[11.5px] leading-relaxed text-fg-4">
                  {card.next}
                </span>
              </span>
              <IconArrowRight
                size={14}
                className="mt-1 shrink-0 text-fg-5 transition-colors group-hover:text-primary"
              />
            </button>
          ))}
        </div>
      ) : null}

      {step === "form" ? (
        <div className="mt-3 flex flex-col gap-3">
          {serverError ? (
            <div
              className="flex items-start gap-2 rounded-lg border-l-[3px] border-l-err bg-err-soft px-3 py-2.5"
              data-testid="wizard-error"
              role="alert"
            >
              <IconAlert size={13} className="mt-[2px] shrink-0 text-err" />
              <p className="text-[11.5px] leading-relaxed text-fg-2">{serverError}</p>
            </div>
          ) : null}
          <AccountForm
            platform={platform}
            value={value}
            onChange={setValue}
            errors={errors}
            advanced="collapsed"
          />
        </div>
      ) : null}

      {step === "onboard" && created ? (
        <div className="mt-3 flex flex-col gap-4">
          <div
            className="flex items-start gap-2.5 rounded-card border-l-[3px] border-l-ok bg-ok-soft px-4 py-3"
            data-testid="wizard-created"
          >
            <IconCheck size={15} className="mt-[2px] shrink-0 text-ok" />
            <div className="min-w-0 text-[12px] leading-relaxed text-fg-2">
              账号 <span className="sw-num">{created.id}</span> 已写进 accounts.yaml 并同步进库。
              <span className="ml-1 text-fg-4">
                台账和数据库现在是一致的，重新部署也不会丢。
              </span>
            </div>
          </div>

          {warnings.map((w) => (
            <div
              key={w}
              className="flex items-start gap-2.5 rounded-lg border-l-[3px] border-l-warn bg-warn-soft px-3 py-2.5"
              data-testid="wizard-warning"
            >
              <IconAlert size={13} className="mt-[2px] shrink-0 text-warn" />
              <p className="text-[11.5px] leading-relaxed text-fg-2">{w}</p>
            </div>
          ))}

          {created.platform === "xhs" ? <XhsOnboarding account={created} /> : null}
          {created.platform === "douyin" ? <DouyinOnboarding account={created} /> : null}
          {created.platform === "wechat_mp" ? <WechatOnboarding /> : null}
        </div>
      ) : null}
    </Modal>
  );
}

const STEPS: { key: Step; label: string }[] = [
  { key: "platform", label: "选平台" },
  { key: "form", label: "填配置" },
  { key: "onboard", label: "接入" },
];

function StepRail({ step }: { step: Step }) {
  const index = STEPS.findIndex((s) => s.key === step);
  return (
    <ol className="flex items-center gap-2" data-testid="wizard-steps">
      {STEPS.map((s, i) => (
        <li key={s.key} className="flex items-center gap-2">
          <span
            className={cn(
              // 步骤条：走 tint 药丸，当前步是唯一一枚实底陶土（与主按钮同一块底色，
              // 文字用 primary-fg 才够 4.5:1——原来的 canvas 色字只有 3.9:1）
              "inline-flex items-center gap-1.5 rounded-pill px-2.5 py-[3px] font-mono text-[10.5px]",
              i < index && "bg-ok-soft text-ok",
              i === index && "bg-primary-solid text-primary-fg",
              i > index && "bg-muted text-fg-3",
            )}
            aria-current={i === index ? "step" : undefined}
          >
            {i < index ? <IconCheck size={10} /> : <span className="tabular-nums">{i + 1}</span>}
            <span className="font-sans">{s.label}</span>
          </span>
          {i < STEPS.length - 1 ? <span className="text-fg-5">→</span> : null}
        </li>
      ))}
    </ol>
  );
}

export default CreateAccountWizard;
