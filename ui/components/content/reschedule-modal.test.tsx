import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SWRConfig } from "swr";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ToastProvider } from "@/components/ui/toast";
import { ApiFailure, setUnauthorizedHandler } from "@/lib/api";
import type { AvailableSlotsResult, ContentRow } from "@/lib/types";

import { parseSlotHint, RescheduleModal } from "./reschedule-modal";

const ITEM = {
  id: "itm_demo_sched",
  account_id: "xhs-demo-01",
  account_name: "小红书 Demo 01",
  platform: "xhs",
  title: "地铁通勤 30 分钟能做什么",
  status: "scheduled",
  created_at: null,
  updated_at: null,
  scheduled_at: "2026-08-17T01:30:00Z",
  slot_text: "08-17 09:30（Asia/Shanghai）",
  published_at: null,
  platform_post_id: null,
  url: null,
  publish_phase: null,
  attempts: 0,
  last_error: null,
  needs_watch: false,
  cover_url: null,
  media: { total: 0, images: 0, videos: 0, kinds: [], cover_index: null },
  tags: [],
  review_notes: null,
  machine_review: null,
  timeline_at: "2026-08-17T01:30:00Z",
} as unknown as ContentRow;

const INVALID_SLOT = {
  ok: false,
  data: null,
  error: {
    code: "invalid_slot",
    message: "08-18 11:00（Asia/Shanghai） 不是账号 xhs-demo-01 的合法发布时刻，被「窗口」挡住",
    detail: {
      reason: "窗口",
      suggested_slot: "2026-08-17T01:00:00+00:00",
      suggested_slot_text: "08-17 09:00（Asia/Shanghai）",
      account_windows: "09:00-11:00、19:00-22:00",
    },
  },
};

/** `GET /content/{id}/slots` 的默认空应答——不干扰那些不关心快捷槽位的用例。 */
function emptySlots(note = "该内容暂无可用发布槽位。"): { ok: true; error: null; data: AvailableSlotsResult } {
  return {
    ok: true,
    error: null,
    data: {
      item_id: ITEM.id,
      account_id: ITEM.account_id,
      timezone: "Asia/Shanghai",
      slots: [],
      note,
    },
  };
}

/**
 * 按 URL 分流的 fetch mock：`GET .../slots` 走 `slotsEnvelope`（默认空槽位）；
 * 其余调用（改期提交）按顺序消费 `postEnvelopes`，用完了重复最后一个。
 *
 * 弹窗一打开就会自己发一次 `GET .../slots`（P19.2），跟用例手动点出来的改期提交
 * 混在同一个 `fetch` mock 里——不按 URL 分流，两路调用的顺序会互相踩。
 */
function routedFetchMock(postEnvelopes: unknown[], slotsEnvelope: unknown = emptySlots()) {
  let i = 0;
  return vi.fn(async (url: unknown) => {
    if (String(url).includes("/slots")) {
      return { status: 200, json: async () => slotsEnvelope } as unknown as Response;
    }
    const resp = postEnvelopes[Math.min(i, postEnvelopes.length - 1)];
    i += 1;
    return resp as unknown as Response;
  });
}

/** 从 mock 调用记录里挑出改期提交（`POST .../reschedule`）那几次。 */
function rescheduleCalls(fetchMock: ReturnType<typeof vi.fn>) {
  return fetchMock.mock.calls.filter((call: unknown[]) => String(call[0]).includes("/reschedule"));
}

/** 每条用例一套全新的 SWR 缓存，否则上一条的 `/slots` 应答会被下一条读到。 */
function renderModal(node: React.ReactElement) {
  return render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <ToastProvider>{node}</ToastProvider>
    </SWRConfig>,
  );
}

describe("parseSlotHint", () => {
  it("只认 invalid_slot，别的错误一律 null", () => {
    const ok = parseSlotHint(new ApiFailure(INVALID_SLOT.error, 422));
    expect(ok).toMatchObject({ reason: "窗口", suggested_slot_text: "08-17 09:00（Asia/Shanghai）" });

    expect(parseSlotHint(new ApiFailure({ code: "not_found", message: "没了" }, 404))).toBeNull();
    expect(parseSlotHint(new Error("网络炸了"))).toBeNull();
  });
});

describe("改期弹窗的槽位建议", () => {
  beforeEach(() => {
    setUnauthorizedHandler(() => {});
  });

  it("422 后渲染「被 X 挡住」与一键改用最近合法槽位", async () => {
    const fetchMock = routedFetchMock([
      // 第一次挑了非法时刻
      { status: 422, json: async () => INVALID_SLOT },
      // 第二次用后端建议的时刻
      {
        status: 200,
        json: async () => ({
          ok: true,
          error: null,
          data: {
            item: ITEM,
            scheduled_at: "2026-08-17T01:00:00+00:00",
            slot_text: "08-17 09:00（Asia/Shanghai）",
            message: "已改期至 08-17 09:00（Asia/Shanghai）",
          },
        }),
      },
    ]);
    vi.stubGlobal("fetch", fetchMock);

    const onDone = vi.fn();
    renderModal(<RescheduleModal item={ITEM} open onClose={() => {}} onDone={onDone} />);

    await userEvent.click(screen.getByRole("button", { name: "改到这个时间" }));

    const hint = await screen.findByTestId("slot-hint");
    expect(hint).toHaveTextContent("未通过校验：窗口");
    expect(hint).toHaveTextContent("09:00-11:00、19:00-22:00");

    const useSuggested = screen.getByTestId("use-suggested-slot");
    expect(useSuggested).toHaveTextContent("改用 08-17 09:00（Asia/Shanghai）");

    await userEvent.click(useSuggested);
    await waitFor(() => expect(onDone).toHaveBeenCalled());

    const calls = rescheduleCalls(fetchMock);
    expect(calls).toHaveLength(2);
    expect(JSON.parse((calls[1][1] as RequestInit).body as string)).toMatchObject({
      scheduled_at: "2026-08-17T01:00:00+00:00",
    });
  });

  it("输入框按**账号时区**显示与提交，不按浏览器时区", async () => {
    // 测试进程钉在 America/Los_Angeles（vitest.config.ts 的 env.TZ），账号在 Asia/Shanghai。
    // ITEM.scheduled_at = 2026-08-17T01:30Z = 账号时区 09:30 / 浏览器时区前一天 18:30。
    const fetchMock = routedFetchMock([
      {
        status: 200,
        json: async () => ({
          ok: true,
          error: null,
          data: {
            item: ITEM,
            scheduled_at: "2026-08-17T11:00:00+00:00",
            slot_text: "08-17 19:00（Asia/Shanghai）",
            message: "已改期至 08-17 19:00（Asia/Shanghai）",
          },
        }),
      },
    ]);
    vi.stubGlobal("fetch", fetchMock);

    renderModal(
      <RescheduleModal item={ITEM} timezone="Asia/Shanghai" open onClose={() => {}} onDone={() => {}} />,
    );

    // 1) 显示：现有排期要按账号时区显示成 09:30，不是浏览器时区的前一天 18:30
    const input = screen.getByTestId("reschedule-input") as HTMLInputElement;
    expect(input.value).toBe("2026-08-17T09:30");
    expect(input).toHaveAttribute("data-zone", "Asia/Shanghai");

    // 2) 标签与轻提示都要把"填的是哪个时区的钟点"说明白
    expect(screen.getByText(/按账号时区（Asia\/Shanghai）填/)).toBeInTheDocument();
    expect(screen.getByTestId("reschedule-zone-hint")).toHaveTextContent("America/Los_Angeles");

    // 3) 提交：运营填账号时区 19:00，提交出去必须是 11:00Z
    await userEvent.clear(input);
    await userEvent.type(input, "2026-08-17T19:00");
    await userEvent.click(screen.getByRole("button", { name: "改到这个时间" }));

    await waitFor(() => expect(rescheduleCalls(fetchMock)).toHaveLength(1));
    const body = JSON.parse((rescheduleCalls(fetchMock)[0][1] as RequestInit).body as string);
    // 修之前这里会是 2026-08-18T02:00:00.000Z（账号时区次日凌晨，窗口外 → 422）
    expect(body.scheduled_at).toBe("2026-08-17T11:00:00.000Z");
  });

  it("账号没配时区时回退浏览器本地，并在标签上如实标注", async () => {
    const fetchMock = routedFetchMock([]);
    vi.stubGlobal("fetch", fetchMock);
    renderModal(<RescheduleModal item={ITEM} timezone="" open onClose={() => {}} onDone={() => {}} />);
    // 回退了就得说出来，不许静默按浏览器时区提交
    expect(screen.getByText(/没配时区/)).toBeInTheDocument();
    expect(screen.getByText(/America\/Los_Angeles/)).toBeInTheDocument();
    expect(screen.getByTestId("reschedule-input")).toHaveValue("2026-08-16T18:30");
    // 弹窗打开也会自己拉一次 /slots——等它落地，别把 act() 警告漏到下一条用例
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
  });

  it("14 天内算不出槽位时不给按钮，改成一句解释", async () => {
    const fetchMock = routedFetchMock([
      {
        status: 422,
        json: async () => ({
          ...INVALID_SLOT,
          error: {
            ...INVALID_SLOT.error,
            detail: { ...INVALID_SLOT.error.detail, suggested_slot: null, suggested_slot_text: null },
          },
        }),
      },
    ]);
    vi.stubGlobal("fetch", fetchMock);

    renderModal(<RescheduleModal item={ITEM} open onClose={() => {}} onDone={() => {}} />);

    await userEvent.click(screen.getByRole("button", { name: "改到这个时间" }));
    await screen.findByTestId("slot-hint");
    expect(screen.queryByTestId("use-suggested-slot")).not.toBeInTheDocument();
    expect(screen.getByText(/放宽发布窗口或提高日上限后重试/)).toBeInTheDocument();
  });
});

describe("改期弹窗 · 快捷槽位药丸——前端估算兜底路径（P14.B4，P19.2 起只在后端答不出时顶上）", () => {
  beforeEach(() => {
    setUnauthorizedHandler(() => {});
    // 只伪造 Date，setTimeout/Promise 走真实时钟——userEvent 的内部等待不受影响
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-08-17T02:00:00Z")); // Asia/Shanghai 同日 10:00
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("后端答不出（本用例给空槽位）时，传了 windows 就渲染「今天/明天」两枚药丸，点一下直接提交", async () => {
    const fetchMock = routedFetchMock(
      [
        {
          status: 200,
          json: async () => ({
            ok: true,
            error: null,
            data: {
              item: ITEM,
              scheduled_at: "2026-08-17T02:05:00.000Z",
              slot_text: "08-17 10:05（Asia/Shanghai）",
              message: "已改期至 08-17 10:05（Asia/Shanghai）",
            },
          }),
        },
      ],
      emptySlots(),
    );
    vi.stubGlobal("fetch", fetchMock);
    const onDone = vi.fn();

    renderModal(
      <RescheduleModal
        item={ITEM}
        timezone="Asia/Shanghai"
        windows="09:00-11:00、19:00-22:00"
        open
        onClose={() => {}}
        onDone={onDone}
      />,
    );

    const today = screen.getByTestId("reschedule-quick-today");
    expect(today).toHaveTextContent("今天 10:05");
    expect(screen.getByTestId("reschedule-quick-tomorrow")).toHaveTextContent("明天 09:00");

    await userEvent.click(today);
    await waitFor(() => expect(onDone).toHaveBeenCalled());

    const calls = rescheduleCalls(fetchMock);
    expect(calls).toHaveLength(1);
    const body = JSON.parse((calls[0][1] as RequestInit).body as string);
    expect(body.scheduled_at).toBe("2026-08-17T02:05:00.000Z");
  });

  it("没传 windows（拆不出区间）、后端也没槽位时不渲染快捷槽位，只留自选时间——不编一个默认值出来", async () => {
    const fetchMock = routedFetchMock([]);
    vi.stubGlobal("fetch", fetchMock);
    renderModal(
      <RescheduleModal item={ITEM} timezone="Asia/Shanghai" open onClose={() => {}} onDone={() => {}} />,
    );

    expect(screen.queryByTestId("reschedule-quick-slots")).not.toBeInTheDocument();
    expect(screen.getByTestId("reschedule-input")).toBeInTheDocument();
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
  });
});

describe("改期弹窗 · 后端真值槽位（P19.2：GET /content/{id}/slots 优先，quickSlots 兜底）", () => {
  beforeEach(() => {
    setUnauthorizedHandler(() => {});
  });

  it("后端给出槽位时，药丸直接显示后端的 slot_text（不自行格式化），点一下用后端的 at 提交", async () => {
    const backendSlots = {
      ok: true,
      error: null,
      data: {
        item_id: ITEM.id,
        account_id: ITEM.account_id,
        timezone: "Asia/Shanghai",
        slots: [
          { at: "2026-08-20T01:30:00Z", slot_text: "08-20 09:30（Asia/Shanghai）", window: "09:00-11:00" },
          { at: "2026-08-20T11:00:00Z", slot_text: "08-20 19:00（Asia/Shanghai）", window: "19:00-22:00" },
        ],
        note: "已返回最近 2 个合法发布槽位。",
      },
    };
    const fetchMock = routedFetchMock(
      [
        {
          status: 200,
          json: async () => ({
            ok: true,
            error: null,
            data: {
              item: ITEM,
              scheduled_at: "2026-08-20T01:30:00Z",
              slot_text: "08-20 09:30（Asia/Shanghai）",
              message: "已改期至 08-20 09:30（Asia/Shanghai）",
            },
          }),
        },
      ],
      backendSlots,
    );
    vi.stubGlobal("fetch", fetchMock);
    const onDone = vi.fn();

    renderModal(
      <RescheduleModal
        item={ITEM}
        timezone="Asia/Shanghai"
        windows="09:00-11:00、19:00-22:00"
        open
        onClose={() => {}}
        onDone={onDone}
      />,
    );

    const first = await screen.findByTestId("reschedule-quick-backend-0");
    expect(first).toHaveTextContent("08-20 09:30（Asia/Shanghai）");
    expect(screen.getByTestId("reschedule-quick-backend-1")).toHaveTextContent("08-20 19:00（Asia/Shanghai）");
    // 后端有真值就不该再冒出前端估算的「今天/明天」两枚
    expect(screen.queryByTestId("reschedule-quick-today")).not.toBeInTheDocument();
    expect(screen.queryByTestId("reschedule-slots-note")).not.toBeInTheDocument();

    await userEvent.click(first);
    await waitFor(() => expect(onDone).toHaveBeenCalled());

    const calls = rescheduleCalls(fetchMock);
    expect(calls).toHaveLength(1);
    expect(JSON.parse((calls[0][1] as RequestInit).body as string)).toMatchObject({
      scheduled_at: "2026-08-20T01:30:00Z",
    });
  });

  it("后端拉取失败时静默回落前端估算，不报错、不崩", async () => {
    const fetchMock = vi.fn(async (url: unknown) => {
      if (String(url).includes("/slots")) {
        return {
          status: 500,
          json: async () => ({ ok: false, error: { code: "internal", message: "服务器炸了" } }),
        } as unknown as Response;
      }
      throw new Error("这条用例不该走到改期提交");
    });
    vi.stubGlobal("fetch", fetchMock);

    renderModal(
      <RescheduleModal
        item={ITEM}
        timezone="Asia/Shanghai"
        windows="09:00-11:00、19:00-22:00"
        open
        onClose={() => {}}
        onDone={() => {}}
      />,
    );

    // 前端估算顶上，两枚老药丸还在，没有因为后端 500 就整个哑掉
    expect(screen.getByTestId("reschedule-quick-today")).toBeInTheDocument();
    expect(screen.getByTestId("reschedule-quick-tomorrow")).toBeInTheDocument();
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    // 静默：既不把后端错误文案露出来，也不额外画一条提示
    expect(screen.queryByText(/服务器炸了/)).not.toBeInTheDocument();
    expect(screen.queryByTestId("reschedule-slots-note")).not.toBeInTheDocument();
  });

  it("后端答了但没有槽位、前端估算还能顶上时，药丸与 note 原话并排显示", async () => {
    const fetchMock = routedFetchMock(
      [],
      emptySlots("账号当前为 suspended，恢复后再查询可用发布时间。"),
    );
    vi.stubGlobal("fetch", fetchMock);

    renderModal(
      <RescheduleModal
        item={ITEM}
        timezone="Asia/Shanghai"
        windows="09:00-11:00、19:00-22:00"
        open
        onClose={() => {}}
        onDone={() => {}}
      />,
    );

    expect(screen.getByTestId("reschedule-quick-today")).toBeInTheDocument();
    const note = await screen.findByTestId("reschedule-slots-note");
    expect(note).toHaveTextContent("账号当前为 suspended，恢复后再查询可用发布时间。");
  });

  it("两边都拿不到槽位时，只显示后端给的原因，不渲染药丸", async () => {
    const fetchMock = routedFetchMock(
      [],
      emptySlots("未来 14 天内仅找到 0 个合法发布槽位，受窗口、间隔或日上限限制。"),
    );
    vi.stubGlobal("fetch", fetchMock);

    renderModal(
      <RescheduleModal item={ITEM} timezone="Asia/Shanghai" open onClose={() => {}} onDone={() => {}} />,
    );

    expect(screen.queryByTestId("reschedule-quick-slots")).not.toBeInTheDocument();
    const note = await screen.findByTestId("reschedule-slots-note");
    expect(note).toHaveTextContent("未来 14 天内仅找到 0 个合法发布槽位");
  });
});
