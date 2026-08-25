import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SWRConfig } from "swr";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ToastProvider } from "@/components/ui/toast";
import { setUnauthorizedHandler } from "@/lib/api";
import type { AccountRow, ImagegenInfo } from "@/lib/types";

import { AccountActions } from "./account-actions";

// 组件出稿成功后会 router.push 去审核台；jsdom 里没有 app router，打个桩
const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));

const ACCOUNT = {
  id: "xhs-demo-01",
  name: "小红书 Demo 01",
  platform: "xhs",
  status: "ok",
  needs_attention: false,
  policy: {
    daily_limit: 5,
    daily_target: 2,
    publish_windows: "09:00-11:00",
    timezone: "Asia/Shanghai",
    min_interval_minutes: 30,
    has_persona: true,
  },
  used_today: 0,
  quota_left: 5,
  supports_login: true,
  extra: {},
} as unknown as AccountRow;

const READY: ImagegenInfo = {
  ready: true,
  enabled: "auto",
  model: "gpt-image-2",
  base_url: "https://api.example/v1",
  has_api_key: true,
  reason: "",
  hint: "",
  used_today: 4,
  daily_limit: 40,
  remaining: 36,
  default_count: 2,
};

const NOT_READY: ImagegenInfo = {
  ...READY,
  ready: false,
  has_api_key: false,
  used_today: 0,
  remaining: 40,
  reason: "没配 SW_IMAGEGEN_API_KEY（也没有可回落的 DEEPSEEK_API_KEY）",
  hint: "把生图专用 key 写进 core 那台机器的 .env",
};

function envelope(data: unknown) {
  return { status: 200, json: async () => ({ ok: true, error: null, data }) } as unknown as Response;
}

/** 生图端点返回 `info`，出稿端点返回一条已入库的稿。 */
function stubFetch(info: ImagegenInfo, generated: Record<string, unknown> = {}) {
  const fetchMock = vi.fn(async (url: string) => {
    if (String(url).includes("/system/imagegen")) return envelope(info);
    return envelope({
      account_id: ACCOUNT.id,
      content_item_id: null, // 不跳转，免得测试里要 mock router
      status: "draft",
      title: "标题",
      llm: "real",
      selected_topic: null,
      tokens_used: 1200,
      elapsed_s: 3.1,
      review_passed: true,
      review_blocking: 0,
      illustrations: 2,
      warnings: [],
      used_today: 1,
      cap: 4,
      message: "出好了",
      ...generated,
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderActions() {
  return render(
    // 每条用例一套全新的 SWR 缓存，否则上一条的 /system/imagegen 会被下一条读到
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <ToastProvider>
        <AccountActions account={ACCOUNT} onOpen={() => {}} onChanged={() => {}} />
      </ToastProvider>
    </SWRConfig>,
  );
}

async function openModal() {
  await userEvent.click(screen.getByTestId("generate-button"));
  await screen.findByTestId("generate-submit");
}

/** 取出这次出稿请求的 body。 */
function generateBody(fetchMock: ReturnType<typeof vi.fn>) {
  const call = fetchMock.mock.calls.find(
    ([url, init]) => String(url).includes("/generate") && init,
  );
  return JSON.parse((call?.[1] as RequestInit).body as string);
}

/** 取「配图」分段控件里当前选中的那个 tab。 */
function activeIllustrationTab() {
  const seg = screen.getByTestId("illustration-count");
  return within(seg)
    .getAllByRole("tab")
    .find((el) => el.getAttribute("aria-selected") === "true");
}

describe("出稿弹层的配图控件（P14.B4：勾选框 + 张数选择器合并成一条 Segmented）", () => {
  beforeEach(() => {
    setUnauthorizedHandler(() => {});
  });

  it("生图可用时默认按服务端默认张数选中，按服务端默认张数提交", async () => {
    const fetchMock = stubFetch(READY);
    renderActions();
    await openModal();

    await screen.findByTestId("illustration-count");
    await waitFor(() => expect(activeIllustrationTab()).toHaveTextContent("2"));
    // 用量要如实摆出来，人才知道今天还能配几张
    expect(screen.getByTestId("imagegen-note")).toHaveTextContent("今天已用 4 / 40 张");

    await userEvent.click(screen.getByTestId("generate-submit"));
    await waitFor(() => expect(generateBody(fetchMock)).toEqual({ illustrations: 2 }));
  });

  it("能改张数", async () => {
    const fetchMock = stubFetch(READY);
    renderActions();
    await openModal();
    const seg = await screen.findByTestId("illustration-count");
    await waitFor(() => expect(activeIllustrationTab()).toHaveTextContent("2"));

    await userEvent.click(within(seg).getByRole("tab", { name: "4" }));
    await userEvent.click(screen.getByTestId("generate-submit"));
    await waitFor(() => expect(generateBody(fetchMock)).toEqual({ illustrations: 4 }));
  });

  it("选「无」就提交 0 张", async () => {
    const fetchMock = stubFetch(READY);
    renderActions();
    await openModal();
    const seg = await screen.findByTestId("illustration-count");
    await waitFor(() => expect(activeIllustrationTab()).toHaveTextContent("2"));

    await userEvent.click(within(seg).getByRole("tab", { name: "无" }));
    await userEvent.click(screen.getByTestId("generate-submit"));
    await waitFor(() => expect(generateBody(fetchMock)).toEqual({ illustrations: 0 }));
  });

  it("生图没接通时，配图控件整个不渲染，只留一行原因说明——不是灰一个控件让人自己猜", async () => {
    const fetchMock = stubFetch(NOT_READY, { illustrations: 0 });
    renderActions();
    await openModal();

    const note = await screen.findByTestId("imagegen-note");
    // 不许自己编一句"暂不可用"，要说清楚缺什么、怎么补
    expect(note).toHaveTextContent("没配 SW_IMAGEGEN_API_KEY");
    expect(note).toHaveTextContent("写进 core 那台机器的 .env");
    // 配图控件不该出现（不是禁用，是压根不渲染）
    expect(screen.queryByTestId("illustration-count")).toBeNull();

    // 红线：配不上图也照样能出稿
    await userEvent.click(screen.getByTestId("generate-submit"));
    await waitFor(() => expect(generateBody(fetchMock)).toEqual({ illustrations: 0 }));
  });

  it("额度用完时同样不渲染配图控件，说明写清楚是额度问题", async () => {
    stubFetch({ ...READY, used_today: 40, remaining: 0 });
    renderActions();
    await openModal();

    await waitFor(() => expect(screen.getByTestId("imagegen-note")).toHaveTextContent("已经用完"));
    expect(screen.queryByTestId("illustration-count")).toBeNull();
  });

  it("选题填了就一起提交", async () => {
    const fetchMock = stubFetch(READY);
    renderActions();
    await openModal();
    await screen.findByTestId("illustration-count");
    await waitFor(() => expect(activeIllustrationTab()).toHaveTextContent("2"));

    await userEvent.type(screen.getByTestId("topic-input"), "租房收纳");
    await userEvent.click(screen.getByTestId("generate-submit"));
    await waitFor(() =>
      expect(generateBody(fetchMock)).toEqual({ illustrations: 2, topic: "租房收纳" }),
    );
  });

  it("开了配图却一张都没配上时，如实提示而不是假装成功", async () => {
    stubFetch(READY, {
      illustrations: 0,
      warnings: ["这条内容没有生成配图：这把 key 的分组没有图像生成权限"],
    });
    renderActions();
    await openModal();
    await screen.findByTestId("illustration-count");
    await waitFor(() => expect(activeIllustrationTab()).toHaveTextContent("2"));

    await userEvent.click(screen.getByTestId("generate-submit"));
    expect(await screen.findByText(/没有图像生成权限/)).toBeInTheDocument();
  });
});
