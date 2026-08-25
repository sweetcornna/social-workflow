import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import * as React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ToastProvider } from "@/components/ui/toast";
import { setUnauthorizedHandler } from "@/lib/api";
import type { ReviewDetail } from "@/lib/types";

import { DecisionPanel, type DecisionHandle } from "./decision-panel";

const ITEM = {
  id: "itm_video",
  account_id: "douyin-demo-01",
  account_name: "抖音 Demo 01",
  platform: "douyin",
  title: "通勤成本上涨怎么省 · 口播",
  status: "draft",
  created_at: "2026-08-16T15:24:25Z",
  updated_at: "2026-08-16T15:24:25Z",
  scheduled_at: null,
  slot_text: "",
  published_at: null,
  platform_post_id: null,
  url: null,
  publish_phase: null,
  attempts: 0,
  last_error: null,
  needs_watch: true,
  cover_url: null,
  media: { total: 1, images: 0, videos: 1, kinds: ["video"], cover_index: null },
  tags: [],
  review_notes: null,
  machine_review: null,
  timeline_at: "2026-08-16T15:24:25Z",
};

function detailOf(overrides: Record<string, unknown> = {}): ReviewDetail {
  return {
    item: { ...ITEM, ...overrides },
    bundle: {
      platform: "douyin",
      title: ITEM.title,
      body_markdown: "口播稿正文",
      body_html: null,
      tags: [],
      media: [{ index: 0, path: "out.mp4", kind: "video", cover: false, exists: true }],
      images: [],
      videos: [],
      cover: null,
      digest: "",
      author: "",
      schedule_at: null,
      is_original: null,
      duration_s: 42,
      hook: "",
      script: "",
      render: {},
    },
    platform_extra: {},
    machine_review: null,
    logs: [],
    slot: { scheduled_at: null, slot_text: "", account_windows: "全天" },
    diff: "",
    media_url_template: "/review/{item_id}/media/{index}",
  } as unknown as ReviewDetail;
}

function Harness({
  detail,
  onDecided = () => {},
  handleRef,
}: {
  detail: ReviewDetail;
  onDecided?: () => void;
  handleRef?: React.RefObject<DecisionHandle | null>;
}) {
  const [watched, setWatched] = React.useState(false);
  return (
    <ToastProvider>
      <DecisionPanel
        ref={handleRef}
        detail={detail}
        watched={watched}
        onWatchedChange={setWatched}
        onDecided={onDecided}
        onEdited={() => {}}
      />
    </ToastProvider>
  );
}

function okApprove() {
  return {
    status: 200,
    json: async () => ({
      ok: true,
      error: null,
      data: {
        item: { ...ITEM, status: "scheduled" },
        message: "已批准，已排期至 08-17 19:00（Asia/Shanghai）",
        scheduled: true,
        scheduled_at: "2026-08-17T11:00:00Z",
        slot_text: "08-17 19:00（Asia/Shanghai）",
      },
    }),
  } as unknown as Response;
}

describe("决策栏 · 视频闸门", () => {
  beforeEach(() => {
    setUnauthorizedHandler(() => {});
  });

  it("needs_watch 时批准钮先是灰的，勾上「已完整观看」才解锁，watched 会带给后端", async () => {
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => okApprove());
    vi.stubGlobal("fetch", fetchMock);

    render(<Harness detail={detailOf()} />);

    const approve = screen.getByTestId("approve-button");
    expect(approve).toBeDisabled();
    expect(screen.getByTestId("watch-blocked-hint")).toBeInTheDocument();

    await userEvent.click(screen.getByLabelText(/已完整观看成片/));
    expect(approve).toBeEnabled();

    await userEvent.click(approve);

    await waitFor(() => {
      const post = fetchMock.mock.calls.find(([, init]) => (init as RequestInit)?.method === "POST");
      expect(post).toBeTruthy();
      expect(JSON.parse((post![1] as RequestInit).body as string)).toMatchObject({ watched: true });
    });

    // 批准成功后就地显示排期槽位——不用去别的页面确认排到了几点
    expect(
      await screen.findByText(/^排期槽位 08-17 19:00（Asia\/Shanghai）$/),
    ).toBeInTheDocument();
  });

  it("不含视频的内容没有闸门，批准钮一开始就是可点的", () => {
    vi.stubGlobal("fetch", vi.fn(async () => okApprove()));
    render(
      <Harness
        detail={detailOf({ needs_watch: false, media: { ...ITEM.media, videos: 0 } })}
      />,
    );
    expect(screen.getByTestId("approve-button")).toBeEnabled();
    expect(screen.queryByLabelText(/已完整观看成片/)).not.toBeInTheDocument();
  });

  it("后端回 422 watch_required 时弹提示，而不是静默失败", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        status: 422,
        json: async () => ({
          ok: false,
          data: null,
          error: {
            code: "watch_required",
            message: "含视频的内容必须先完整观看成片，并勾选「已完整观看」才能批准",
            detail: null,
          },
        }),
      })) as unknown as typeof fetch,
    );

    render(<Harness detail={detailOf()} />);
    await userEvent.click(screen.getByLabelText(/已完整观看成片/));
    await userEvent.click(screen.getByTestId("approve-button"));

    expect(await screen.findByText(/必须先完整观看成片/)).toBeInTheDocument();
  });
});

describe("决策栏 · 键盘流走的是同一条代码路径", () => {
  beforeEach(() => {
    setUnauthorizedHandler(() => {});
  });

  it("handle.approve() 在闸门没过时不发请求，只提示", async () => {
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => okApprove());
    vi.stubGlobal("fetch", fetchMock);
    const ref = React.createRef<DecisionHandle>();

    render(<Harness detail={detailOf()} handleRef={ref} />);

    await act(async () => ref.current!.approve());
    expect(await screen.findByText(/必须先完整观看成片才能批准/)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();

    await userEvent.click(screen.getByLabelText(/已完整观看成片/));
    await act(async () => ref.current!.approve());
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
  });

  it("驳回理由空着时不发请求；写了理由按 Enter 直接提交并回调 onDecided", async () => {
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => ({
      status: 200,
      json: async () => ({
        ok: true,
        error: null,
        data: { item: { ...ITEM, status: "rejected" }, message: "已驳回" },
      }),
    })) as unknown as typeof fetch;
    vi.stubGlobal("fetch", fetchMock);
    const onDecided = vi.fn();
    const ref = React.createRef<DecisionHandle>();

    render(<Harness detail={detailOf()} onDecided={onDecided} handleRef={ref} />);

    await act(async () => ref.current!.reject());
    expect(await screen.findByText(/驳回必须写理由/)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();

    // r → 聚焦理由；打字；Enter → 提交
    act(() => ref.current!.focusReason());
    const reason = screen.getByTestId("reject-reason");
    expect(reason).toHaveFocus();
    await userEvent.type(reason, "标题夸大，换一个更具体的钩子{Enter}");

    await waitFor(() => expect(onDecided).toHaveBeenCalled());
    const post = (fetchMock as unknown as ReturnType<typeof vi.fn>).mock.calls.find(
      ([, init]) => (init as RequestInit)?.method === "POST",
    );
    expect(JSON.parse((post![1] as RequestInit).body as string)).toMatchObject({
      reason: "标题夸大，换一个更具体的钩子",
    });
  });
});

describe("决策栏 · 常用驳回理由药丸（P14.B4：点一下填入，可续写）", () => {
  beforeEach(() => {
    setUnauthorizedHandler(() => {});
  });

  it("空理由框点一下药丸，直接填入那句理由并聚焦", async () => {
    vi.stubGlobal("fetch", vi.fn());
    render(<Harness detail={detailOf()} />);

    await userEvent.click(screen.getByTestId("reject-reason-preset-标题夸张"));

    const reason = screen.getByTestId("reject-reason") as HTMLTextAreaElement;
    expect(reason.value).toBe("标题夸张");
    expect(reason).toHaveFocus();
  });

  it("理由框已经写了字时，再点一枚药丸是续写而不是覆盖", async () => {
    vi.stubGlobal("fetch", vi.fn());
    render(<Harness detail={detailOf()} />);

    const reason = screen.getByTestId("reject-reason") as HTMLTextAreaElement;
    await userEvent.type(reason, "封面糊了");
    await userEvent.click(screen.getByTestId("reject-reason-preset-配图不符"));

    expect(reason.value).toBe("封面糊了；配图不符");
  });
});
