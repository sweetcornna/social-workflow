"use client";

import * as React from "react";

import { WindowEditor } from "@/components/accounts/window-editor";
import { FieldLabel, Input, Select, Textarea } from "@/components/ui/field";
import { SegmentedControl } from "@/components/ui/segmented";
import { browserTimeZone, PLATFORM_LABEL } from "@/lib/format";
import type { Platform } from "@/lib/types";
import { toDrafts, toPayload, validateDrafts, type WindowDraft } from "@/lib/windows";
import { cn } from "@/lib/utils";

/** 平台硬顶，与 `core/accounts.py:PLATFORM_DAILY_CEILING` 对齐（有回归测试盯着后端那份）。 */
export const DAILY_CEILING: Partial<Record<Platform, number>> = { douyin: 10, xhs: 50 };

/** 各平台的缺省值。新建时先铺上去，人只需要改想改的那几个。 */
export const PLATFORM_DEFAULTS: Record<
  Platform,
  { daily_limit: number; daily_target: number; min_interval_minutes: number; windows: string }
> = {
  xhs: { daily_limit: 10, daily_target: 1, min_interval_minutes: 90, windows: "12:00-14:00、19:00-22:30" },
  douyin: { daily_limit: 2, daily_target: 1, min_interval_minutes: 120, windows: "12:00-13:30、18:00-22:00" },
  wechat_mp: { daily_limit: 1, daily_target: 1, min_interval_minutes: 0, windows: "07:00-09:00" },
};

/**
 * 简化三原则（P14.B4，porting-notes 第 5 节）的落点：时区/间隔/日上限/出稿
 * 频次都从自由文本改成有限选项——打错这一整类错误就不存在了，超顶这类值
 * 也不再靠校验拦，而是从选项里直接消失。
 *
 * 每个候选表都配一个 `xxxOptions(current)` 函数：如果当前值（多半来自编辑
 * 一个历史账号）不在候选表里，原样插进去，绝不因为换了控件就悄悄改掉已存的值。
 */
const COMMON_TIMEZONES = [
  "Asia/Shanghai",
  "Asia/Hong_Kong",
  "Asia/Tokyo",
  "Asia/Singapore",
  "America/Los_Angeles",
  "America/New_York",
  "Europe/London",
  "UTC",
];

const MIN_INTERVAL_CANDIDATES = [0, 30, 60, 90, 120, 180];

const DAILY_TARGET_OPTIONS: { value: string; label: string }[] = [
  { value: "0", label: "不自动" },
  { value: "1", label: "1" },
  { value: "2", label: "2" },
  { value: "3", label: "3" },
];

/** 日上限候选（条）。超过平台硬顶的档位由 `dailyLimitOptions` 按 ceiling 直接裁掉。 */
const DAILY_LIMIT_CANDIDATES = [1, 2, 3, 4, 5, 6, 8, 10, 15, 20, 30, 40, 50];

/** 时区候选：服务器默认 + 常用列表 + 运行时探测到的浏览器时区（去重）。 */
function timezoneOptions(current: string): { value: string; label: string }[] {
  const seen = new Set<string>();
  const opts: { value: string; label: string }[] = [{ value: "", label: "服务器默认（留空）" }];
  const push = (zone: string, label: string) => {
    if (!zone || seen.has(zone)) return;
    seen.add(zone);
    opts.push({ value: zone, label });
  };
  push("Asia/Shanghai", "Asia/Shanghai（默认）");
  push(browserTimeZone(), `${browserTimeZone()}（浏览器时区）`);
  for (const zone of COMMON_TIMEZONES) push(zone, zone);
  if (current && !seen.has(current)) push(current, `${current}（当前值）`);
  return opts;
}

/** 最小间隔候选（分钟），量纲写进 label。 */
function minIntervalOptions(current: string): { value: string; label: string }[] {
  const values = new Set(MIN_INTERVAL_CANDIDATES);
  const n = Number(current);
  if (current !== "" && Number.isInteger(n) && n >= 0) values.add(n);
  return [...values].sort((a, b) => a - b).map((n) => ({ value: String(n), label: `${n} 分钟` }));
}

/** 每天出稿候选：理论上不会越界，编辑态防御式地兜住历史脏值。 */
function dailyTargetOptions(current: string): { value: string; label: string }[] {
  if (current === "" || DAILY_TARGET_OPTIONS.some((o) => o.value === current)) {
    return DAILY_TARGET_OPTIONS;
  }
  return [...DAILY_TARGET_OPTIONS, { value: current, label: current }];
}

/** 日上限候选：按平台硬顶裁掉超顶档位——「超顶」这类选择在选项层面就不存在。 */
function dailyLimitOptions(
  ceiling: number | undefined,
  current: string,
): { value: string; label: string }[] {
  const values = new Set(
    DAILY_LIMIT_CANDIDATES.filter((n) => ceiling === undefined || n <= ceiling),
  );
  const n = Number(current);
  if (current !== "" && Number.isInteger(n) && n >= 0) values.add(n);
  return [...values].sort((a, b) => a - b).map((n) => ({ value: String(n), label: `${n} 条` }));
}

/** 「高级设置」折叠头的一行摘要——不用展开就能确认平台预填的默认值对不对。 */
function advancedSummary(value: AccountFormValue): string {
  const target = value.daily_target === "0" ? "不自动出稿" : `每天出稿 ${value.daily_target || "0"} 条`;
  return [
    `日上限 ${value.daily_limit || "0"} 条`,
    target,
    `间隔 ${value.min_interval_minutes || "0"} 分钟`,
    `时区 ${value.timezone || "服务器默认"}`,
  ].join(" · ");
}

export interface AccountFormValue {
  name: string;
  identity_hint: string;
  windows: WindowDraft[];
  min_interval_minutes: string;
  daily_limit: string;
  daily_target: string;
  timezone: string;
  persona: string;
}

export function defaultsFor(platform: Platform): AccountFormValue {
  const preset = PLATFORM_DEFAULTS[platform];
  return {
    name: "",
    identity_hint: "",
    windows: toDrafts(preset.windows),
    min_interval_minutes: String(preset.min_interval_minutes),
    daily_limit: String(preset.daily_limit),
    daily_target: String(preset.daily_target),
    timezone: "Asia/Shanghai",
    persona: "",
  };
}

export interface FormErrors {
  name?: string;
  identity_hint?: string;
  windows?: string;
  daily_limit?: string;
}

/**
 * 提交前的本地校验。只拦"一看就知道不对"的那几样，其余交给后端——
 * 前端不复制业务规则，只是省一次往返。
 */
export function validate(platform: Platform, value: AccountFormValue): FormErrors {
  const errors: FormErrors = {};
  if (!value.name.trim()) errors.name = "请填写名称。仅用于工作台显示，可随时修改。";
  if (platform === "douyin" && !value.identity_hint.trim()) {
    errors.identity_hint = "抖音必填。发布前与创作者中心昵称比对，不一致即中止发布——这是防发错号的唯一依据。";
  }
  const windowError = validateDrafts(value.windows);
  if (windowError) errors.windows = windowError;

  const ceiling = DAILY_CEILING[platform];
  const limit = Number(value.daily_limit);
  if (value.daily_limit !== "" && (!Number.isFinite(limit) || limit < 0)) {
    errors.daily_limit = "日上限要填一个非负整数。";
  } else if (ceiling !== undefined && limit > ceiling) {
    errors.daily_limit = `${PLATFORM_LABEL[platform]}的日上限硬顶是 ${ceiling} 条（保守限频口径），填不上去。`;
  }
  return errors;
}

function numberOrUndefined(text: string): number | undefined {
  if (text.trim() === "") return undefined;
  const n = Number(text);
  return Number.isFinite(n) ? n : undefined;
}

/** 表单值 → `POST /accounts` / `PATCH /accounts/{id}` 的 body（不含 platform）。 */
export function toBody(value: AccountFormValue) {
  return {
    name: value.name.trim(),
    identity_hint: value.identity_hint.trim() || undefined,
    publish_windows: toPayload(value.windows),
    min_interval_minutes: numberOrUndefined(value.min_interval_minutes),
    daily_limit: numberOrUndefined(value.daily_limit),
    daily_target: numberOrUndefined(value.daily_target),
    timezone: value.timezone.trim() || undefined,
    persona: value.persona.trim() || undefined,
  };
}

function Row({
  label,
  hint,
  error,
  htmlFor,
  children,
}: {
  label: string;
  hint?: React.ReactNode;
  error?: string;
  htmlFor?: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <FieldLabel htmlFor={htmlFor}>{label}</FieldLabel>
      {children}
      {error ? (
        <p className="mt-1 text-[11.5px] leading-relaxed text-err" role="alert">
          {error}
        </p>
      ) : hint ? (
        <p className="mt-1 text-[11.5px] leading-relaxed text-fg-4">{hint}</p>
      ) : null}
    </div>
  );
}

/**
 * 账号表单。新建向导与「编辑」弹窗共用同一份 —— 两处字段不一致是最容易出的低级 bug。
 */
export function AccountForm({
  platform,
  value,
  onChange,
  errors,
  disabledFields,
  advanced = "inline",
}: {
  platform: Platform;
  value: AccountFormValue;
  onChange: (next: AccountFormValue) => void;
  errors: FormErrors;
  /** 编辑态里不该改的字段（目前只有 name 之外的都可改，留个口子）。 */
  disabledFields?: Set<keyof AccountFormValue>;
  /**
   * 日上限/出稿/间隔/时区/人设这五个字段的呈现方式（P14.B4）。
   *
   * - `"inline"`（默认，编辑弹窗用）：原样铺开——编辑是一次明确的动作，人已经打算
   *   改点什么了，不该再让他多点一下才看得到字段。
   * - `"collapsed"`（新建向导用）：收进「高级设置」`<details>`，默认折叠——平台
   *   预填的默认值已经够用，新建向导的第二步只该露"必须由人决定"的那几样
   *   （名称、抖音昵称、发布窗口）。日上限校验出错时自动展开，不藏起报错。
   */
  advanced?: "inline" | "collapsed";
}) {
  const set = (patch: Partial<AccountFormValue>) => onChange({ ...value, ...patch });
  const off = (k: keyof AccountFormValue) => disabledFields?.has(k) ?? false;
  const ceiling = DAILY_CEILING[platform];

  const tzOptions = React.useMemo(() => timezoneOptions(value.timezone), [value.timezone]);
  const intervalOptions = React.useMemo(
    () => minIntervalOptions(value.min_interval_minutes),
    [value.min_interval_minutes],
  );
  const targetOptions = React.useMemo(
    () => dailyTargetOptions(value.daily_target),
    [value.daily_target],
  );
  const limitOptions = React.useMemo(
    () => dailyLimitOptions(ceiling, value.daily_limit),
    [ceiling, value.daily_limit],
  );

  const [advancedOpen, setAdvancedOpen] = React.useState(Boolean(errors.daily_limit));
  React.useEffect(() => {
    if (errors.daily_limit) setAdvancedOpen(true);
  }, [errors.daily_limit]);

  const advancedFields = (
    <>
      <div className="grid gap-3 sm:grid-cols-3">
        <Row
          label={ceiling !== undefined ? `日上限（条，硬顶 ${ceiling}）` : "日上限（条）"}
          htmlFor="acc-limit"
          error={errors.daily_limit}
          hint={ceiling === undefined ? "每天最多发几条" : undefined}
        >
          <Select
            id="acc-limit"
            data-testid="account-daily-limit"
            value={value.daily_limit}
            disabled={off("daily_limit")}
            onChange={(e) => set({ daily_limit: e.target.value })}
          >
            {limitOptions.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </Select>
        </Row>
        <Row label="每天出稿">
          <SegmentedControl
            label="每天出稿"
            data-testid="account-daily-target"
            value={value.daily_target}
            onChange={(v) => set({ daily_target: v })}
            options={targetOptions}
          />
        </Row>
        <Row label="最小间隔" htmlFor="acc-interval" hint="两次发布至少隔多久">
          <Select
            id="acc-interval"
            data-testid="account-min-interval"
            value={value.min_interval_minutes}
            onChange={(e) => set({ min_interval_minutes: e.target.value })}
          >
            {intervalOptions.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </Select>
        </Row>
      </div>

      <Row label="时区" htmlFor="acc-tz" hint="发布窗口、日上限都按这个时区算。">
        <Select
          id="acc-tz"
          data-testid="account-timezone"
          value={value.timezone}
          onChange={(e) => set({ timezone: e.target.value })}
        >
          {tzOptions.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </Select>
      </Row>

      <Row
        label="人设（可留空）"
        htmlFor="acc-persona"
        hint="留空则读 prompts/accounts/<id>/persona.md —— 长人设推荐写在那个文件里，版本可追。"
      >
        <Textarea
          id="acc-persona"
          data-testid="account-persona"
          value={value.persona}
          rows={3}
          maxLength={4000}
          placeholder="一句话说清这个号写给谁、什么口吻"
          onChange={(e) => set({ persona: e.target.value })}
          className={cn("min-h-[68px]")}
        />
      </Row>
    </>
  );

  return (
    <div className="flex flex-col gap-3.5">
      <Row label="名字" error={errors.name} htmlFor="acc-name" hint="仅用于工作台显示，可随时修改。">
        <Input
          id="acc-name"
          data-testid="account-name"
          value={value.name}
          maxLength={64}
          placeholder={`例如「${PLATFORM_LABEL[platform]}主号」`}
          onChange={(e) => set({ name: e.target.value })}
          disabled={off("name")}
        />
      </Row>

      {platform === "douyin" ? (
        <Row
          label="创作者中心昵称（identity_hint）"
          error={errors.identity_hint}
          htmlFor="acc-hint"
          hint="发布前会读页面昵称并与这里比对，不一致直接拒发。这是防发错号的唯一依据，必填。"
        >
          <Input
            id="acc-hint"
            data-testid="account-identity-hint"
            value={value.identity_hint}
            maxLength={64}
            placeholder="抖音 App「我」页面显示的那个名字"
            onChange={(e) => set({ identity_hint: e.target.value })}
          />
        </Row>
      ) : null}

      <div>
        <FieldLabel>发布窗口</FieldLabel>
        <WindowEditor
          value={value.windows}
          onChange={(windows) => set({ windows })}
          error={errors.windows}
        />
      </div>

      {advanced === "collapsed" ? (
        <details
          data-testid="account-advanced"
          open={advancedOpen}
          onToggle={(e) => setAdvancedOpen(e.currentTarget.open)}
          className="rounded-lg bg-muted px-3 py-2.5"
        >
          <summary
            data-testid="account-advanced-toggle"
            className="cursor-pointer select-none text-[12.5px] font-medium text-fg-2"
          >
            高级设置（已按平台预填）
            <span className="ml-1.5 font-normal text-fg-4">{advancedSummary(value)}</span>
          </summary>
          <div className="mt-3 flex flex-col gap-3.5">{advancedFields}</div>
        </details>
      ) : (
        advancedFields
      )}
    </div>
  );
}

export default AccountForm;
