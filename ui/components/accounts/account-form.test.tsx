import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import * as React from "react";
import { describe, expect, it } from "vitest";

import { AccountForm, defaultsFor, toBody, validate, type AccountFormValue, type FormErrors } from "./account-form";

describe("defaultsFor", () => {
  it("各平台的缺省值就是台账里那套保守口径", () => {
    expect(defaultsFor("xhs").daily_limit).toBe("10");
    expect(defaultsFor("douyin").daily_limit).toBe("2");
    expect(defaultsFor("wechat_mp").daily_limit).toBe("1");
    // 小红书的流量高峰在午休与晚间，默认就铺这两段
    expect(defaultsFor("xhs").windows).toEqual([
      { start: "12:00", end: "14:00" },
      { start: "19:00", end: "22:30" },
    ]);
  });
});

describe("validate", () => {
  it("名字必填", () => {
    const errors = validate("xhs", { ...defaultsFor("xhs"), name: "   " });
    expect(errors.name).toBeTruthy();
  });

  it("抖音必须填 identity_hint，并说清楚为什么", () => {
    const errors = validate("douyin", { ...defaultsFor("douyin"), name: "抖音号" });
    expect(errors.identity_hint).toContain("防发错号");
    // 别的平台不该被这条拦住
    expect(validate("xhs", { ...defaultsFor("xhs"), name: "小红书号" }).identity_hint).toBeUndefined();
  });

  it("日上限超过平台硬顶就地拦住，并说出硬顶是多少", () => {
    const errors = validate("douyin", {
      ...defaultsFor("douyin"),
      name: "抖音号",
      identity_hint: "抖音号",
      daily_limit: "99",
    });
    expect(errors.daily_limit).toContain("10");
  });

  it("窗口非法要给出错原因（与后端同口径）", () => {
    const errors = validate("xhs", {
      ...defaultsFor("xhs"),
      name: "号",
      windows: [{ start: "09:00", end: "09:00" }],
    });
    expect(errors.windows).toContain("永不放行");
  });

  it("填对了就一条错误都没有", () => {
    expect(validate("xhs", { ...defaultsFor("xhs"), name: "小红书主号" })).toEqual({});
  });
});

describe("toBody", () => {
  it("空字段变成 undefined 而不是空串——PATCH 的语义是「没传就不改」", () => {
    const body = toBody({
      ...defaultsFor("xhs"),
      name: "  小红书主号  ",
      identity_hint: "",
      persona: "",
      timezone: "",
    });
    expect(body.name).toBe("小红书主号");
    expect(body.identity_hint).toBeUndefined();
    expect(body.persona).toBeUndefined();
    expect(body.timezone).toBeUndefined();
  });

  it("窗口按后端要的字符串数组提交", () => {
    const body = toBody({ ...defaultsFor("xhs"), name: "号" });
    expect(body.publish_windows).toEqual(["12:00-14:00", "19:00-22:30"]);
  });

  it("数字字段传数字，非数字一律不传", () => {
    const body = toBody({ ...defaultsFor("xhs"), name: "号", daily_target: "", daily_limit: "7" });
    expect(body.daily_limit).toBe(7);
    expect(body.daily_target).toBeUndefined();
  });
});

/** 受控壳子：把 AccountForm 的 value 状态提到测试里，onChange 直接写回 state。 */
function Harness({
  platform = "xhs" as const,
  initial,
  errors = {},
  advanced,
  onValue,
}: {
  platform?: "xhs" | "douyin" | "wechat_mp";
  initial?: AccountFormValue;
  errors?: FormErrors;
  advanced?: "inline" | "collapsed";
  /** 每次 onChange 都把最新值抛出来，方便断言 toBody() 的载荷。 */
  onValue?: (v: AccountFormValue) => void;
}) {
  const [value, setValue] = React.useState<AccountFormValue>(initial ?? defaultsFor(platform));
  return (
    <AccountForm
      platform={platform}
      value={value}
      errors={errors}
      advanced={advanced}
      onChange={(next) => {
        setValue(next);
        onValue?.(next);
      }}
    />
  );
}

describe("AccountForm 交互（P14.B4：简化三原则——默认值进 label / 钉死值不做输入框 / 自由文本换选择）", () => {
  it("时区、最小间隔是下拉选择；每天出稿是分段控件；日上限硬顶之外的档位在下拉里直接不存在", () => {
    render(<Harness platform="douyin" />);

    // 时区：Select，不再是自由文本框
    const tz = screen.getByTestId("account-timezone");
    expect(tz.tagName).toBe("SELECT");
    expect((tz as HTMLSelectElement).value).toBe("Asia/Shanghai");

    // 最小间隔：Select，量纲写进 label
    const interval = screen.getByTestId("account-min-interval");
    expect(interval.tagName).toBe("SELECT");
    expect(within(interval).getByRole("option", { name: "120 分钟" })).toBeInTheDocument();

    // 每天出稿：分段控件（tablist），不是数字输入框
    const target = screen.getByTestId("account-daily-target");
    expect(target.getAttribute("role")).toBe("tablist");
    expect(within(target).getByRole("tab", { name: "1" })).toHaveAttribute("aria-selected", "true");

    // 日上限：抖音硬顶 10 条，下拉最大值就是 10——超顶档位不是校验拦住的，是选项里压根没有
    const limit = screen.getByTestId("account-daily-limit") as HTMLSelectElement;
    const values = [...limit.options].map((o) => Number(o.value));
    expect(Math.max(...values)).toBe(10);
    expect(values).not.toContain(15);
  });

  it("点分段控件的「不自动」会把 daily_target 改成 0，载荷跟着变", async () => {
    let latest: AccountFormValue | undefined;
    render(<Harness platform="xhs" onValue={(v) => (latest = v)} />);

    await userEvent.click(
      within(screen.getByTestId("account-daily-target")).getByRole("tab", { name: "不自动" }),
    );

    expect(latest?.daily_target).toBe("0");
  });

  it("时区选「服务器默认」时，toBody 提交 undefined——留空语义与原先的空文本框一致", async () => {
    let latest: AccountFormValue | undefined;
    render(<Harness platform="xhs" onValue={(v) => (latest = v)} />);

    await userEvent.selectOptions(screen.getByTestId("account-timezone"), "服务器默认（留空）");

    expect(latest?.timezone).toBe("");
    expect(toBody(latest!).timezone).toBeUndefined();
  });

  it("最小间隔改选 30 分钟，载荷里就是数字 30", async () => {
    let latest: AccountFormValue | undefined;
    render(<Harness platform="xhs" onValue={(v) => (latest = v)} />);

    await userEvent.selectOptions(screen.getByTestId("account-min-interval"), "30 分钟");

    expect(latest?.min_interval_minutes).toBe("30");
    expect(toBody(latest!).min_interval_minutes).toBe(30);
  });
});

describe("AccountForm 交互 · 高级设置折叠（P14.B4，新建向导用）", () => {
  it("collapsed 模式默认折叠，折叠头一行摘要当前默认值", () => {
    render(<Harness platform="xhs" advanced="collapsed" />);

    const details = screen.getByTestId("account-advanced") as HTMLDetailsElement;
    expect(details.open).toBe(false);
    const toggle = screen.getByTestId("account-advanced-toggle");
    expect(toggle).toHaveTextContent("高级设置（已按平台预填）");
    expect(toggle).toHaveTextContent("日上限 10 条");
    expect(toggle).toHaveTextContent("每天出稿 1 条");
    expect(toggle).toHaveTextContent("间隔 90 分钟");
  });

  it("点开折叠头就能展开", async () => {
    render(<Harness platform="xhs" advanced="collapsed" />);

    await userEvent.click(screen.getByTestId("account-advanced-toggle"));

    expect((screen.getByTestId("account-advanced") as HTMLDetailsElement).open).toBe(true);
  });

  it("日上限校验报错时，折叠段自动展开——不把报错藏在收起来的地方", () => {
    render(<Harness platform="xhs" advanced="collapsed" errors={{ daily_limit: "超过硬顶" }} />);

    expect((screen.getByTestId("account-advanced") as HTMLDetailsElement).open).toBe(true);
  });

  it("inline 模式（编辑弹窗的默认呈现）不包 details，字段原样铺开", () => {
    render(<Harness platform="xhs" />);

    expect(screen.queryByTestId("account-advanced")).not.toBeInTheDocument();
    expect(screen.getByTestId("account-daily-limit")).toBeInTheDocument();
  });
});
