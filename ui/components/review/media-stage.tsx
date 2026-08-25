"use client";

import * as React from "react";

import {
  IconAlert,
  IconChevronRight,
  IconImage,
  IconLock,
  IconUnlock,
  IconVideo,
} from "@/components/icons";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import { mediaUrl, previewUrl } from "@/lib/api";
import { PLATFORM_LABEL } from "@/lib/format";
import type { BundleMedia, Platform } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * 媒体主舞台 —— 审核台的正中间，占屏 ≥55%。
 *
 * 审核对象的**本体**是媒体，不是那段 markdown：小红书是一叠图卡、抖音是一条成片、
 * 公众号是排过版的图文。所以这块必须大到能真的看清，键盘左右直接翻页。
 *
 * 三条纪律（WORKBENCH_API.md §13）：
 *  1. 媒体端点不在 /api/v1 下也不带 token，`<img>` / `<video>` 直接引用；
 *  2. `exists=false` 的项**不发请求**，显示缺件提示——那必然是 404；
 *  3. 公众号 `body_html` 只能进 sandbox iframe，直接插页面会污染全局样式。
 */

export type StageMode = "video" | "cards" | "article";

export interface MediaStageProps {
  itemId: string;
  platform: Platform;
  media: BundleMedia[];
  hasArticle: boolean;
  /** 当前图卡下标（受控，键盘 ←/→ 由页面驱动）。 */
  index: number;
  onIndexChange: (next: number) => void;
  /** 成片播完——视频闸门靠它自动解锁。 */
  onVideoEnded: () => void;
  watched: boolean;
  needsWatch: boolean;
}

export function stageModesFor(
  platform: Platform,
  media: BundleMedia[],
  hasArticle: boolean,
): StageMode[] {
  const modes: StageMode[] = [];
  if (media.some((m) => m.kind === "video")) modes.push("video");
  if (platform === "wechat_mp" && hasArticle) modes.push("article");
  if (media.some((m) => m.kind !== "video")) modes.push("cards");
  return modes;
}

const MODE_LABEL: Record<StageMode, string> = {
  video: "成片",
  cards: "图卡",
  article: "图文正文",
};

export function MediaStage({
  itemId,
  platform,
  media,
  hasArticle,
  index,
  onIndexChange,
  onVideoEnded,
  watched,
  needsWatch,
}: MediaStageProps) {
  const videos = React.useMemo(() => media.filter((m) => m.kind === "video"), [media]);
  const images = React.useMemo(() => media.filter((m) => m.kind !== "video"), [media]);
  const modes = React.useMemo(
    () => stageModesFor(platform, media, hasArticle),
    [platform, media, hasArticle],
  );
  const [mode, setMode] = React.useState<StageMode>(modes[0] ?? "cards");

  React.useEffect(() => {
    setMode(modes[0] ?? "cards");
  }, [modes, itemId]);

  return (
    <section
      data-testid="media-stage"
      className="sw-card flex h-full min-h-0 flex-col overflow-hidden rounded-card"
    >
      <header className="flex shrink-0 items-center gap-2 border-b border-line px-3 py-2">
        <span className="sw-label">{PLATFORM_LABEL[platform] ?? platform}</span>
        {modes.length > 1 ? (
          <div role="tablist" aria-label="舞台视图" className="flex items-center gap-1">
            {modes.map((m) => (
              <button
                key={m}
                type="button"
                role="tab"
                aria-selected={m === mode}
                onClick={() => setMode(m)}
                className={cn(
                  // 舞台视图切换：与 SegmentedControl 同一句话（tint 药丸），不描边
                  "rounded-pill px-2.5 py-[3px] font-mono text-[11px] transition-colors duration-150",
                  m === mode
                    ? "bg-primary-soft text-primary-deep"
                    : "bg-muted text-fg-3 hover:text-fg-2",
                )}
              >
                <span className="font-sans">{MODE_LABEL[m]}</span>
              </button>
            ))}
          </div>
        ) : null}
        <span className="flex-1" />
        {mode === "cards" && images.length > 1 ? (
          <span className="sw-num text-[12px] text-fg-2" data-testid="card-counter">
            {Math.min(index + 1, images.length)} / {images.length}
          </span>
        ) : null}
        {needsWatch ? (
          <Badge tone={watched ? "ok" : "warn"} data-testid="watch-badge">
            {watched ? (
              <>
                <IconUnlock size={10} />
                已看完
              </>
            ) : (
              <>
                <IconLock size={10} />
                看完才能批准
              </>
            )}
          </Badge>
        ) : null}
      </header>

      <div className="relative min-h-0 flex-1">
        {mode === "video" ? (
          <VideoStage
            itemId={itemId}
            video={videos[0]}
            onEnded={onVideoEnded}
            watched={watched}
          />
        ) : mode === "article" ? (
          <ArticleStage itemId={itemId} />
        ) : (
          <CardStage
            itemId={itemId}
            images={images}
            index={index}
            onIndexChange={onIndexChange}
          />
        )}
      </div>

      {mode === "cards" && images.length > 1 ? (
        <footer className="sw-scroll flex shrink-0 gap-1.5 overflow-x-auto border-t border-line px-3 py-2">
          {images.map((m, i) => {
            // 生图配的照片和 HTML 模板截的文字卡长得完全不一样，但缩到 48px 就分不清了。
            // 角标让人一眼看出"这张是模型画的"——审图和审版式是两种看法
            const generated = m.source === "imagegen";
            return (
              <button
                key={m.index}
                type="button"
                onClick={() => onIndexChange(i)}
                aria-label={`第 ${i + 1} 张（${generated ? "生成配图" : "文字卡"}）`}
                aria-current={i === index}
                data-testid="card-thumb"
                data-media-source={m.source ?? "render"}
                className={cn(
                  // 缩略图选中态：2px 陶土环。这里保留描边——缩略图是图像，只有"框住它"
                  // 才说得清选的是哪一张，换成 tint 底会被图片自己盖掉
                  "relative h-12 w-12 shrink-0 overflow-hidden rounded-md transition-colors",
                  i === index
                    ? "border-2 border-primary"
                    : "border border-line hover:border-line-strong",
                )}
              >
                {m.exists ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={mediaUrl(itemId, m.index)}
                    alt=""
                    className="h-full w-full object-cover"
                  />
                ) : (
                  <span className="flex h-full w-full items-center justify-center text-fg-5">
                    <IconImage size={14} />
                  </span>
                )}
                <span
                  data-testid="thumb-kind-badge"
                  className={cn(
                    "absolute bottom-0 right-0 px-[3px] text-[9px] font-medium leading-[13px]",
                    "rounded-tl-md bg-[rgba(20,10,4,0.72)]",
                    generated ? "text-primary" : "text-fg-3",
                  )}
                >
                  {generated ? "图" : "卡"}
                </span>
              </button>
            );
          })}
        </footer>
      ) : null}
    </section>
  );
}

function VideoStage({
  itemId,
  video,
  onEnded,
  watched,
}: {
  itemId: string;
  video: BundleMedia | undefined;
  onEnded: () => void;
  watched: boolean;
}) {
  const [progress, setProgress] = React.useState(0);

  if (!video) return <MissingFile icon={<IconVideo />} text="这条内容标了含视频，但内容包里没有成片文件" />;
  if (!video.exists) {
    return (
      <MissingFile
        icon={<IconVideo />}
        text="成片文件不在本机（生成机与展示机不同盘）。不能凭封面批准——请到出片那台机器上看完再来。"
      />
    );
  }

  return (
    <div className="flex h-full flex-col bg-black/70">
      <div className="flex min-h-0 flex-1 items-center justify-center p-3">
        <video
          controls
          preload="metadata"
          className="h-full max-h-full w-auto max-w-full rounded-lg"
          src={mediaUrl(itemId, video.index)}
          onEnded={onEnded}
          onTimeUpdate={(e) => {
            const el = e.currentTarget;
            if (el.duration > 0) setProgress((el.currentTime / el.duration) * 100);
          }}
          data-testid={`video-${video.index}`}
        />
      </div>
      <div className="shrink-0 bg-muted px-3 py-2">
        <div className="mb-1 flex items-center justify-between gap-2 text-[11.5px]">
          <span className="text-fg-3">
            {watched ? "已完整观看，批准已解锁" : "播到底会自动解锁批准（合规证据链的一部分）"}
          </span>
          <span className="sw-num text-fg-2">{Math.round(progress)}%</span>
        </div>
        <Progress value={progress} tone={watched ? "ok" : "amber"} label="成片观看进度" />
      </div>
    </div>
  );
}

function CardStage({
  itemId,
  images,
  index,
  onIndexChange,
}: {
  itemId: string;
  images: BundleMedia[];
  index: number;
  onIndexChange: (i: number) => void;
}) {
  if (images.length === 0) {
    return <MissingFile icon={<IconImage />} text="这条内容没有图片附件" />;
  }
  const active = images[Math.min(index, images.length - 1)];

  return (
    <div className="relative flex h-full items-center justify-center bg-muted/60 p-3">
      {active?.exists ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={mediaUrl(itemId, active.index)}
          alt={`第 ${index + 1} 张`}
          data-testid="card-image"
          className="max-h-full max-w-full rounded-lg object-contain shadow-card"
        />
      ) : (
        <MissingFile icon={<IconImage />} text="这张图的文件不在本机" />
      )}

      {images.length > 1 ? (
        <>
          <StageArrow
            side="left"
            label="上一张"
            onClick={() => onIndexChange((index - 1 + images.length) % images.length)}
          />
          <StageArrow
            side="right"
            label="下一张"
            onClick={() => onIndexChange((index + 1) % images.length)}
          />
          <span className="sw-num absolute bottom-3 left-1/2 -translate-x-1/2 rounded-full bg-[rgba(20,10,4,0.6)] px-2.5 py-1 text-[11.5px] text-white">
            {Math.min(index + 1, images.length)} / {images.length}
            <span className="ml-2 opacity-70">← → 翻卡</span>
          </span>
        </>
      ) : null}
    </div>
  );
}

function StageArrow({
  side,
  label,
  onClick,
}: {
  side: "left" | "right";
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      className={cn(
        "absolute top-1/2 flex h-9 w-9 -translate-y-1/2 items-center justify-center rounded-full",
        "sw-pop text-fg-2",
        "transition-colors hover:bg-muted-hover hover:text-fg",
        side === "left" ? "left-3" : "right-3",
      )}
    >
      <IconChevronRight size={16} className={side === "left" ? "rotate-180" : undefined} />
    </button>
  );
}

/** 公众号 body_html 预览。sandbox 全关，只允许渲染。 */
function ArticleStage({ itemId }: { itemId: string }) {
  return (
    <div className="h-full bg-white">
      <iframe
        title="公众号图文预览"
        src={previewUrl(itemId)}
        sandbox=""
        className="h-full w-full"
      />
    </div>
  );
}

/** 媒体缺件时的诚实提示。不是"占位素材"，是"这个文件不在这台机器上"。 */
function MissingFile({ icon, text }: { icon: React.ReactNode; text: string }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-2.5 px-8 text-center text-fg-4">
      <span className="[&_svg]:h-7 [&_svg]:w-7">{icon}</span>
      <span className="max-w-[36ch] text-[12.5px] leading-relaxed">{text}</span>
    </div>
  );
}

/** 完全没有媒体时的舞台（公众号纯文本 / 内容包还没生成图）。 */
export function EmptyStage({ platform }: { platform: Platform }) {
  return (
    <section
      data-testid="media-stage"
      className="sw-card flex h-full flex-col items-center justify-center gap-2.5 rounded-card px-8 text-center text-fg-4"
    >
      <IconAlert className="h-7 w-7" />
      <p className="max-w-[40ch] text-[13px] leading-relaxed">
        这条{PLATFORM_LABEL[platform] ?? platform}内容没有任何媒体附件。
        <br />
        右边只有文案可以审——如果它本该有图或成片，说明生成环节没跑完，别急着批准。
      </p>
    </section>
  );
}

export default MediaStage;
