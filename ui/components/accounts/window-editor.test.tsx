import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import * as React from "react";
import { describe, expect, it } from "vitest";

import type { WindowDraft } from "@/lib/windows";

import { WindowEditor } from "./window-editor";

/** 受控壳子：把 value 状态提到测试里。 */
function Harness({ initial }: { initial: WindowDraft[] }) {
  const [value, setValue] = React.useState<WindowDraft[]>(initial);
  return <WindowEditor value={value} onChange={setValue} />;
}

describe("WindowEditor（P14.B4：预设药丸是主路径，自定义段默认收起）", () => {
  it("当前值原样是某个预设时，自定义段默认收起，那枚预设是选中态", () => {
    render(<Harness initial={[{ start: "12:00", end: "14:00" }, { start: "19:00", end: "22:30" }]} />);

    expect(screen.getByTestId("window-preset-午休 + 晚间")).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByTestId("window-start-0")).not.toBeInTheDocument();
    expect(screen.queryByTestId("window-add")).not.toBeInTheDocument();
  });

  it("当前值不是任何预设（编辑一个历史账号）时，自定义段默认就是展开的", () => {
    render(<Harness initial={[{ start: "08:15", end: "09:45" }]} />);

    expect(screen.getByTestId("window-preset-custom")).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("window-start-0")).toBeInTheDocument();
  });

  it("点一枚预设药丸：直接把值换成那个预设，不用先点自定义", async () => {
    render(<Harness initial={[]} />);

    await userEvent.click(screen.getByTestId("window-preset-早高峰"));

    expect(screen.getByTestId("window-preview")).toHaveTextContent("07:00-09:00");
    expect(screen.getByTestId("window-preset-早高峰")).toHaveAttribute("aria-selected", "true");
    // 还是收起的——选预设不等于要看逐段编辑器
    expect(screen.queryByTestId("window-start-0")).not.toBeInTheDocument();
  });

  it("点「自定义」展开逐段编辑器，之前的值原样保留（不清空）", async () => {
    render(<Harness initial={[{ start: "12:00", end: "14:00" }, { start: "19:00", end: "22:30" }]} />);

    await userEvent.click(screen.getByTestId("window-preset-custom"));

    expect(screen.getByTestId("window-start-0")).toHaveValue("12:00");
    expect(screen.getByTestId("window-start-1")).toHaveValue("19:00");
  });

  it("展开自定义段后改一个钟点，预览文案跟着变", async () => {
    render(<Harness initial={[{ start: "12:00", end: "14:00" }]} />);

    await userEvent.click(screen.getByTestId("window-preset-custom"));
    // input[type=time] 不走 userEvent.type 的逐字敲键（真实浏览器是分段编辑控件，
    // jsdom 不模拟那套 UI）；直接 fireEvent.change 设值，等价于用户把这段时间填完
    fireEvent.change(screen.getByTestId("window-start-0"), { target: { value: "11:30" } });

    expect(screen.getByTestId("window-preview")).toHaveTextContent("11:30-14:00");
  });

  it("全天预设的值是空数组，选中态照样认得出来", () => {
    render(<Harness initial={[]} />);
    expect(screen.getByTestId("window-preset-全天")).toHaveAttribute("aria-selected", "true");
  });
});
