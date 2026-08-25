"use client";

import { useRouter, useSearchParams } from "next/navigation";
import * as React from "react";

import { IconInbox, IconKeyboard, IconRefresh } from "@/components/icons";
import { DecisionPanel, type DecisionHandle } from "@/components/review/decision-panel";
import { EmptyStage, MediaStage } from "@/components/review/media-stage";
import { QueueRail } from "@/components/review/queue-rail";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { SegmentedControl } from "@/components/ui/segmented";
import { Panel } from "@/components/ui/panel";
import { SkeletonRows } from "@/components/ui/skeleton";
import { describeError } from "@/lib/api";
import { POLL, useApi } from "@/lib/hooks";
import type { ContentRow, Page as PageT, ReviewDetail } from "@/lib/types";

const PLATFORM_CHIPS = [
  { value: "", label: "全部" },
  { value: "wechat_mp", label: "公众号" },
  { value: "xhs", label: "小红书" },
  { value: "douyin", label: "抖音" },
];

const STATUS_CHIPS = [
  { value: "", label: "待审" },
  { value: "rejected", label: "已驳回" },
  { value: "all", label: "全部" },
];

export default function ReviewPage() {
  return (
    <React.Suspense fallback={<SkeletonRows rows={8} className="px-3" />}>
      <ReviewStation />
    </React.Suspense>
  );
}

/**
 * 审核台 —— 三区沉浸布局。
 *
 *   ┌ 队列 236 ┬───────── 媒体主舞台（≥55% 屏宽） ─────────┬ 决策 324 ┐
 *
 * 键盘流是这一页的主输入：`j/k` 换条、`←/→` 翻卡、`a` 批准、`r` 聚焦驳回理由、
 * `Enter` 提交驳回，批完自动切下一条。鼠标能做的事键盘都能做，而且走同一条
 * 代码路径（决策栏暴露 imperative handle，页面只负责把按键转过去）。
 */
function ReviewStation() {
  const router = useRouter();
  const search = useSearchParams();
  const selectedId = search.get("id") ?? "";
  const [platform, setPlatform] = React.useState("");
  const [status, setStatus] = React.useState("");
  const [helpOpen, setHelpOpen] = React.useState(false);

  const queue = useApi<PageT<ContentRow>>(
    "/review",
    { platform, status, limit: 100 },
    { refreshInterval: POLL.reviewQueue },
  );
  const items = React.useMemo(() => queue.data?.items ?? [], [queue.data]);
  // 没指定就落在**第一条还能决策的**（draft / reviewing）上：一坐下来先看到的
  // 应该是"能批的那条"，而不是队列顶上那条已经驳回、只能等改稿的
  const firstActionable = React.useMemo(
    () => items.find((i) => i.status === "draft" || i.status === "reviewing") ?? items[0],
    [items],
  );
  const currentId = selectedId || firstActionable?.id || "";

  const detail = useApi<ReviewDetail>(currentId ? `/review/${currentId}` : null);

  const [watched, setWatched] = React.useState(false);
  const [cardIndex, setCardIndex] = React.useState(0);
  const decisionRef = React.useRef<DecisionHandle>(null);

  React.useEffect(() => {
    setWatched(false);
    setCardIndex(0);
  }, [currentId]);

  const select = React.useCallback(
    (id: string) => {
      router.replace(id ? `/review/?id=${encodeURIComponent(id)}` : "/review/", { scroll: false });
    },
    [router],
  );

  /** j / k：在队列里上下移动。列表里找不到当前条时从头开始。 */
  const step = React.useCallback(
    (delta: number) => {
      if (items.length === 0) return;
      const idx = items.findIndex((i) => i.id === currentId);
      const next = idx < 0 ? 0 : Math.min(items.length - 1, Math.max(0, idx + delta));
      select(items[next].id);
    },
    [currentId, items, select],
  );

  const imageCount = React.useMemo(
    () => (detail.data?.bundle.media ?? []).filter((m) => m.kind !== "video").length,
    [detail.data],
  );

  const turnCard = React.useCallback(
    (delta: number) => {
      if (imageCount <= 1) return;
      setCardIndex((i) => (i + delta + imageCount) % imageCount);
    },
    [imageCount],
  );

  /** 审完自动下一条：先算好去处，再刷队列——刷完这一条通常已经不在列表里了。 */
  const advance = React.useCallback(() => {
    const idx = items.findIndex((i) => i.id === currentId);
    const next = idx >= 0 ? (items[idx + 1] ?? items[idx - 1]) : undefined;
    void queue.mutate();
    void detail.mutate();
    if (next && next.id !== currentId) select(next.id);
  }, [currentId, detail, items, queue, select]);

  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const t = e.target as HTMLElement | null;
      // 正在输入框里打字时，除 Esc 外一律不抢键（驳回理由的 Enter 由 textarea 自己处理）
      const typing =
        !!t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable);
      if (typing) return;

      switch (e.key) {
        case "j":
          e.preventDefault();
          step(1);
          break;
        case "k":
          e.preventDefault();
          step(-1);
          break;
        case "ArrowRight":
          e.preventDefault();
          turnCard(1);
          break;
        case "ArrowLeft":
          e.preventDefault();
          turnCard(-1);
          break;
        case "a":
          e.preventDefault();
          decisionRef.current?.approve();
          break;
        case "r":
          e.preventDefault();
          decisionRef.current?.focusReason();
          break;
        case "?":
          setHelpOpen((v) => !v);
          break;
        default:
          break;
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [step, turnCard]);

  const data = detail.data;
  const hasMedia = (data?.bundle.media ?? []).length > 0;
  const hasArticle = Boolean(data?.bundle.body_html);

  return (
    <div className="relative flex h-full min-h-0 flex-col">
      <div className="flex min-h-0 flex-1 gap-3 px-3">
        {/* ── 左：窄队列 ─────────────────────────────────────────────── */}
        <div className="w-[236px] shrink-0">
          <QueueRail
            items={items}
            currentId={currentId}
            onSelect={select}
            total={queue.data?.total ?? 0}
            isLoading={queue.isLoading}
            error={queue.error}
            onRetry={() => void queue.mutate()}
            toolbar={
              <div className="flex flex-col gap-1.5">
                <SegmentedControl
                  label="平台"
                  value={platform}
                  onChange={setPlatform}
                  options={PLATFORM_CHIPS}
                />
                <SegmentedControl
                  label="状态"
                  value={status}
                  onChange={setStatus}
                  options={STATUS_CHIPS}
                />
              </div>
            }
          />
        </div>

        {currentId ? (
          <>
            {/* ── 中：媒体主舞台（屏宽的一多半） ───────────────────── */}
            <div className="min-w-0 flex-1" data-testid="stage-slot">
              {detail.error ? (
                <Panel className="flex h-full items-center justify-center px-8 text-center text-[12.5px] text-err">
                  {describeError(detail.error)}
                </Panel>
              ) : !data ? (
                <Panel className="h-full">
                  <SkeletonRows rows={10} />
                </Panel>
              ) : hasMedia || hasArticle ? (
                <MediaStage
                  itemId={data.item.id}
                  platform={data.item.platform}
                  media={data.bundle.media}
                  hasArticle={hasArticle}
                  index={cardIndex}
                  onIndexChange={setCardIndex}
                  onVideoEnded={() => setWatched(true)}
                  watched={watched}
                  needsWatch={data.item.needs_watch}
                />
              ) : (
                <EmptyStage platform={data.item.platform} />
              )}
            </div>

            {/* ── 右：决策栏 ──────────────────────────────────────── */}
            <div className="w-[324px] shrink-0">
              {data ? (
                <DecisionPanel
                  ref={decisionRef}
                  key={data.item.id}
                  detail={data}
                  watched={watched}
                  onWatchedChange={setWatched}
                  onDecided={advance}
                  onEdited={() => void detail.mutate()}
                />
              ) : (
                <Panel className="h-full">
                  <SkeletonRows rows={8} />
                </Panel>
              )}
            </div>
          </>
        ) : (
          <div className="flex min-w-0 flex-1 items-center justify-center">
            <EmptyState
              icon={<IconInbox />}
              title={queue.isLoading ? "正在拉队列" : "队列是空的"}
              description="没有等待人工处理的内容。可以去「今日」看今日排期，或等下一轮生成。"
              action={
                <Button size="sm" onClick={() => void queue.mutate()}>
                  <IconRefresh size={13} />
                  重新拉一次
                </Button>
              }
            />
          </div>
        )}
      </div>

      {/* ── 快捷键提示：常驻底部一条 32px 的窄带，永远不压住媒体与决策区 ──
           窄屏收起来（B6 走查）：这一条有 6 组键帽 + 说明，挤进 390px 时会
           折成一堆竖排单字，既读不出来也占掉两行高。触屏上本来也没有 j/k/a/r
           可按，藏起来不损失任何能力——「? 说明」那段文案按 `?` 键同样能开。 */}
      <div className="flex h-9 shrink-0 items-center justify-end gap-2 px-4">
        {currentId ? (
          <div data-testid="shortcut-hint" className="hidden items-center gap-2 sm:flex">
            <IconKeyboard size={13} className="text-fg-4" />
            <Key k="j" /> <Key k="k" />
            <span className="text-[10.5px] text-fg-4">换条</span>
            <Key k="←" /> <Key k="→" />
            <span className="text-[10.5px] text-fg-4">翻卡</span>
            <Key k="a" />
            <span className="text-[10.5px] text-fg-4">批准</span>
            <Key k="r" />
            <span className="text-[10.5px] text-fg-4">驳回理由</span>
            <Key k="Enter" />
            <span className="text-[10.5px] text-fg-4">提交驳回</span>
            <span className="text-fg-5">·</span>
            <button
              type="button"
              onClick={() => setHelpOpen((v) => !v)}
              className="text-[10.5px] text-fg-4 underline-offset-2 hover:text-fg-2 hover:underline"
            >
              ? 说明
            </button>
          </div>
        ) : null}
      </div>

      {helpOpen ? (
        <div
          className="sw-pop absolute inset-x-3 bottom-12 z-20 rounded-card px-4 py-3 text-[12px] leading-relaxed text-fg-2"
          role="dialog"
          aria-label="快捷键说明"
        >
          审完一条会自动切到下一条；含视频的内容必须把成片播到底才解锁批准（后端也拦，
          绕过界面直接打 API 会拿到 422 watch_required）。再按一次 <Key k="?" /> 收起。
        </div>
      ) : null}
    </div>
  );
}

function Key({ k }: { k: string }) {
  return (
    <kbd className="sw-num rounded-pill bg-muted-hover px-1.5 py-[1px] text-[10.5px] text-fg-2">
      {k}
    </kbd>
  );
}
