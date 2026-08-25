"use client";

import * as React from "react";

import { IconImage, IconPlay, IconVideo } from "@/components/icons";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

/** 列表行的封面缩略图。`cover_url` 为 null 时给一个暖调的空框，不请求。 */
export function CoverThumb({
  src,
  kind,
  className,
}: {
  src: string | null;
  kind: "image" | "video";
  className?: string;
}) {
  return (
    <span
      className={cn(
        "relative flex h-11 w-11 shrink-0 items-center justify-center overflow-hidden rounded-md",
        "bg-muted text-fg-3",
        className,
      )}
    >
      {src ? (
        // 媒体是后端直出的原文件，静态导出没有图片优化服务，用原生 img
        // eslint-disable-next-line @next/next/no-img-element
        <img src={src} alt="" className="h-full w-full object-cover" />
      ) : kind === "video" ? (
        <IconPlay size={14} />
      ) : (
        <IconImage size={14} />
      )}
      {src && kind === "video" ? (
        <span className="absolute inset-0 flex items-center justify-center bg-black/35 text-white">
          <IconPlay size={13} />
        </span>
      ) : null}
    </span>
  );
}

export function MediaSummaryBadges({ images, videos }: { images: number; videos: number }) {
  return (
    <>
      {images > 0 ? (
        <Badge tone="muted">
          <IconImage size={10} />
          {images}
        </Badge>
      ) : null}
      {videos > 0 ? (
        <Badge tone="amber">
          <IconVideo size={10} />
          {videos}
        </Badge>
      ) : null}
    </>
  );
}
