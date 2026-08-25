"use client";

import * as React from "react";

import { IconBox, IconRefresh, IconRotate } from "@/components/icons";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { LiveDot } from "@/components/ui/live-dot";
import { useToast } from "@/components/ui/toast";
import { apiFetch, describeError } from "@/lib/api";
import type { Tone } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import type { Sidecar, SidecarActionResult, SidecarState } from "@/lib/types";

/**
 * sidecar 状态 → 人话。
 *
 * 关键是 `none-driver`：那台 core 压根没接管容器，界面必须直说"未接入"，
 * 不能显示一个转圈的"启动中"让人以为马上就好。
 */
const STATE: Record<SidecarState, { label: string; tone: Tone; what: string }> = {
  running: { label: "在跑", tone: "ok", what: "容器活着。扫码、发布都走它。" },
  stopped: {
    label: "已停",
    tone: "warn",
    what: "容器还在但没跑。点「启动」拉起来——登录态在 volume 里，不用重新扫码。",
  },
  absent: {
    label: "还没建",
    tone: "warn",
    what: "这个号还没有自己的容器。点「启动」会按一账号一容器的规矩创建一个。",
  },
  "none-driver": {
    label: "未接入",
    tone: "muted",
    what: "这台 core 的 SW_SIDECAR_DRIVER=none，不接管容器。扫码要先在服务器上改成 docker。",
  },
  error: { label: "探不到", tone: "err", what: "问 docker 要状态时出错了，详情见下面那行。" },
};

/**
 * 小红书 sidecar 面板：状态 + 健康探测 + 起停重建。
 *
 * 一账号一容器一 volume 一端口是 POLICY 红线，所以这里把容器名、volume 名与端口
 * 都摊开显示——运维在服务器上 `docker ps` 时看到的就是这几个名字。
 */
export function SidecarPanel({ accountId }: { accountId: string }) {
  const toast = useToast();
  const [busy, setBusy] = React.useState("");
  const { data, error, isLoading, mutate } = useApi<Sidecar>(
    `/accounts/${accountId}/sidecar`,
    undefined,
    { refreshInterval: 8000 },
  );

  async function act(action: "start" | "stop" | "recreate") {
    setBusy(action);
    try {
      const res = await apiFetch<SidecarActionResult>(
        `/accounts/${accountId}/sidecar/${action}`,
        { method: "POST" },
      );
      await mutate(res.sidecar, { revalidate: false });
      toast.ok(res.message);
    } catch (e) {
      // 起不来是常态（镜像没构建、端口占用、驱动是 none），原样把后端那句话显示出来
      toast.err(describeError(e));
    } finally {
      setBusy("");
      void mutate();
    }
  }

  if (isLoading && !data) {
    return <p className="text-[12px] text-fg-4">正在问 sidecar 状态</p>;
  }
  if (error && !data) {
    return (
      <p className="text-[12px] text-err" data-testid="sidecar-error">
        取 sidecar 状态失败：{describeError(error)}
      </p>
    );
  }
  if (!data) return null;

  const meta = STATE[data.state] ?? STATE.error;

  return (
    <div className="flex flex-col gap-2.5" data-testid="sidecar-panel" data-state={data.state}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="sw-label flex items-center gap-1.5">
          <IconBox size={12} />
          sidecar
        </span>
        <LiveDot tone={meta.tone} pulse={data.state === "running"} size={7} />
        <Badge tone={meta.tone} data-testid="sidecar-state">
          {meta.label}
        </Badge>
        <span className="sw-num text-[10.5px] text-fg-4">驱动 {data.driver}</span>
        {data.healthy ? <Badge tone="ok">/health 通了</Badge> : null}
      </div>

      <p className="text-[12px] leading-relaxed text-fg-3" data-testid="sidecar-what">
        {meta.what}
      </p>
      {/* none-driver 时后端那句话与上面的解释是同一件事，不重复说两遍 */}
      {data.detail && data.state !== "none-driver" ? (
        <p className="text-[11.5px] leading-relaxed text-fg-4">{data.detail}</p>
      ) : null}
      {!data.healthy && data.health_detail ? (
        <p className="text-[11.5px] leading-relaxed text-fg-4" data-testid="sidecar-health">
          健康探测：{data.health_detail}
        </p>
      ) : null}

      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 rounded-lg bg-muted px-3 py-2 text-[11px]">
        <dt className="text-fg-4">容器</dt>
        <dd className="sw-num truncate text-fg-2">{data.container}</dd>
        <dt className="text-fg-4">volume</dt>
        <dd className="sw-num truncate text-fg-2">{data.volume}</dd>
        <dt className="text-fg-4">端口</dt>
        <dd className="sw-num text-fg-2">{data.port ?? "—"}</dd>
        <dt className="text-fg-4">镜像</dt>
        <dd className="sw-num truncate text-fg-2">{data.image || "—"}</dd>
      </dl>

      <div className="flex flex-wrap items-center gap-1.5">
        <Button
          size="sm"
          variant={data.state === "running" ? "outline" : "primary"}
          data-testid="sidecar-start"
          loading={busy === "start"}
          onClick={() => void act("start")}
        >
          启动
        </Button>
        <Button size="sm" data-testid="sidecar-stop" loading={busy === "stop"} onClick={() => void act("stop")}>
          停止
        </Button>
        <Button
          size="sm"
          data-testid="sidecar-recreate"
          loading={busy === "recreate"}
          onClick={() => void act("recreate")}
        >
          <IconRotate size={12} />
          重建
        </Button>
        <Button size="sm" variant="ghost" onClick={() => void mutate()}>
          <IconRefresh size={12} />
          刷新
        </Button>
      </div>
      <p className="text-[11px] text-fg-4">
        重建只换容器，<strong className="font-medium text-fg-3">不删 volume</strong>
        ，扫过的码不用重扫。
      </p>
    </div>
  );
}

export default SidecarPanel;
