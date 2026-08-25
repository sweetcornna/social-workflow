"use client";

import * as React from "react";

import {
  IconAlert,
  IconCheck,
  IconChevronDown,
  IconClock,
  IconEdit,
  IconExternal,
  IconShield,
  IconX,
} from "@/components/icons";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmClock } from "@/components/ui/confirm-clock";
import { Checkbox, FieldLabel, Input, Textarea } from "@/components/ui/field";
import { LiveDot } from "@/components/ui/live-dot";
import { Modal } from "@/components/ui/modal";
import { useToast } from "@/components/ui/toast";
import { apiFetch, ApiFailure, describeError } from "@/lib/api";
import {
  ACTION_LABEL,
  fmtFullTime,
  fmtTime,
  PLATFORM_LABEL,
  STATUS_LABEL,
  toneForStatus,
} from "@/lib/format";
import { FINDING_LABEL, FINDING_TONE, parseFindings } from "@/lib/findings";
import type { ConfirmResult, ReviewDetail, WriteResult } from "@/lib/types";
import { cn } from "@/lib/utils";

const ACTOR = "operator";

/** 常用驳回理由（P14.B4）。点一下填进理由框，不用每次都从头打字。 */
const COMMON_REJECT_REASONS = ["事实存疑", "标题夸张", "配图不符", "不像人设", "平台风险"];

export interface DecisionHandle {
  approve: () => void;
  reject: () => void;
  focusReason: () => void;
}

/**
 * 右侧决策栏。
 *
 * 视频闸门与后端 `watch_required` 严格对应：`needs_watch=true` 时，
 * "已完整观看成片"没勾上，批准钮就是灰的（成片播完会自动勾上）。
 * 后端那一道 422 仍然存在——这里只是把它前移到界面上，不是替代。
 *
 * 键盘流由页面驱动：`a` / `r` / `Enter` 都打在这里暴露的 imperative handle 上，
 * 所以鼠标和键盘走的是同一条代码路径，不会出现"键盘能过、鼠标不能过"。
 */
export const DecisionPanel = React.forwardRef<
  DecisionHandle,
  {
    detail: ReviewDetail;
    watched: boolean;
    onWatchedChange: (next: boolean) => void;
    /** 批准 / 驳回成功后：刷新队列并自动切到下一条。 */
    onDecided: () => void;
    /** 改稿保存后：只刷新，不换条。 */
    onEdited: () => void;
  }
>(function DecisionPanel(
  { detail, watched, onWatchedChange, onDecided, onEdited },
  ref,
) {
  const toast = useToast();
  const { item, bundle, machine_review: mr, logs, slot, diff } = detail;

  const [reason, setReason] = React.useState("");
  const [busy, setBusy] = React.useState<"approve" | "reject" | null>(null);
  const [result, setResult] = React.useState<WriteResult | null>(null);
  const [editOpen, setEditOpen] = React.useState(false);
  const reasonRef = React.useRef<HTMLTextAreaElement>(null);
  const watchRef = React.useRef<HTMLDivElement>(null);

  React.useEffect(() => {
    setReason("");
    setResult(null);
  }, [item.id]);

  const [deciding, setDeciding] = React.useState<"confirm" | "reject" | null>(
    null,
  );

  /**
   * 工作台里的兜底确认。和 Telegram 卡片上那两个按钮走**同一个后端函数**，
   * 动作名也逐字一致（确认发布 / 不发）——Telegram 不能是单点。
   */
  const decide = React.useCallback(
    async (go: boolean) => {
      if (deciding) return;
      setDeciding(go ? "confirm" : "reject");
      try {
        const res = await apiFetch<ConfirmResult>(
          `/content/${item.id}/${go ? "confirm" : "reject"}`,
          {
            method: "POST",
            body: go ? {} : { actor: ACTOR, reason: "在工作台点了「不发」" },
          },
        );
        toast.ok(res.message);
        onEdited();
      } catch (e) {
        toast.err(describeError(e));
      } finally {
        setDeciding(null);
      }
    },
    [deciding, item.id, onEdited, toast],
  );

  const findings = parseFindings(mr?.notes);
  // 后端只让 draft / reviewing 批准（其它状态 409 invalid_state）
  const canDecide = item.status === "draft" || item.status === "reviewing";
  // 但已驳回的稿子仍然要能改：改完状态回 draft，就又回到人工卡点上了
  const canEdit = canDecide || item.status === "rejected";
  const watchBlocked = item.needs_watch && !watched;

  const approve = React.useCallback(async () => {
    if (!canDecide || busy) return;
    if (watchBlocked) {
      toast.warn("含视频的内容必须先完整观看成片才能批准");
      watchRef.current?.scrollIntoView({ block: "center", behavior: "smooth" });
      return;
    }
    setBusy("approve");
    try {
      const res = await apiFetch<WriteResult>(`/review/${item.id}/approve`, {
        method: "POST",
        body: { actor: ACTOR, watched },
      });
      setResult(res);
      toast.ok(res.message);
      onDecided();
    } catch (e) {
      if (e instanceof ApiFailure && e.code === "watch_required") {
        toast.err(e.message);
        watchRef.current?.scrollIntoView({
          block: "center",
          behavior: "smooth",
        });
      } else {
        toast.err(describeError(e));
      }
    } finally {
      setBusy(null);
    }
  }, [busy, canDecide, item.id, onDecided, toast, watchBlocked, watched]);

  const reject = React.useCallback(async () => {
    if (!canDecide || busy) return;
    if (!reason.trim()) {
      toast.warn("驳回必须写理由，会回传给改稿 Agent。");
      reasonRef.current?.focus();
      return;
    }
    setBusy("reject");
    try {
      const res = await apiFetch<WriteResult>(`/review/${item.id}/reject`, {
        method: "POST",
        body: { actor: ACTOR, reason },
      });
      setResult(res);
      toast.ok(res.message);
      setReason("");
      onDecided();
    } catch (e) {
      if (e instanceof ApiFailure && e.code === "reason_required") {
        toast.err(e.message);
        reasonRef.current?.focus();
      } else {
        toast.err(describeError(e));
      }
    } finally {
      setBusy(null);
    }
  }, [busy, canDecide, item.id, onDecided, reason, toast]);

  React.useImperativeHandle(
    ref,
    () => ({
      approve: () => void approve(),
      reject: () => void reject(),
      focusReason: () => reasonRef.current?.focus(),
    }),
    [approve, reject],
  );

  /** 点一下常用理由药丸：续写而不是覆盖，理由框里已经写的内容不会被打断。 */
  const appendReason = React.useCallback((phrase: string) => {
    setReason((prev) => {
      const trimmed = prev.trimEnd();
      return trimmed ? `${trimmed}；${phrase}` : phrase;
    });
    reasonRef.current?.focus();
  }, []);

  return (
    <aside
      data-testid="decision-panel"
      className="sw-card flex h-full min-h-0 w-full flex-col overflow-hidden rounded-card"
    >
      <header className="shrink-0 border-b border-line px-3.5 py-3">
        <h2 className="text-[15px] font-medium leading-snug text-fg">
          {item.title || item.id}
        </h2>
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
          <Badge tone="muted">
            {PLATFORM_LABEL[item.platform] ?? item.platform}
          </Badge>
          <Badge tone={toneForStatus(item.status)}>
            {STATUS_LABEL[item.status] ?? item.status}
          </Badge>
          <span className="sw-num truncate text-[10px] text-fg-4">
            {item.account_id}
          </span>
        </div>
      </header>

      <div className="sw-scroll min-h-0 flex-1 overflow-y-auto px-3.5 py-3">
        {result ? (
          <div
            data-testid="decision-result"
            className="mb-3 rounded-lg border-l-[3px] border-l-ok bg-ok-soft px-3 py-2.5"
          >
            <div className="flex items-center gap-1.5 text-[12.5px] font-medium text-ok">
              <IconCheck size={13} />
              {result.message}
            </div>
            {result.slot_text ? (
              <div className="sw-num mt-1 flex items-center gap-1.5 text-[11.5px] text-fg-2">
                <IconClock size={12} />
                排期槽位 {result.slot_text}
              </div>
            ) : null}
            {/*
              批准不是最后一步。不说这一句的话，人会以为点完批准就发了，
              然后在 Telegram 上收到一张"这是什么？"的卡片。
            */}
            {result.item?.confirm_required ? (
              <div className="mt-1 text-[11.5px] text-fg-2">
                发布前需再确认一次，可在 Telegram 或排期页完成。
              </div>
            ) : null}
          </div>
        ) : null}

        {/* 机器审核结论 —— 人要先知道机器怎么看 */}
        <Section title="机器审核" icon={<IconShield size={12} />}>
          {mr ? (
            <>
              <div className="mb-2 flex flex-wrap items-center gap-1.5">
                <Badge tone={mr.passed ? "ok" : "err"}>
                  {mr.passed ? "通过" : "未通过"}
                </Badge>
                <Badge tone={mr.blocking > 0 ? "err" : "muted"}>
                  阻断 {mr.blocking}
                </Badge>
                <Badge tone={mr.warnings > 0 ? "warn" : "muted"}>
                  警告 {mr.warnings}
                </Badge>
                <span className="sw-num text-[10px] text-fg-4">
                  {fmtTime(mr.at)}
                </span>
              </div>
              <ul className="flex flex-col gap-1.5">
                {findings.map((f, i) => (
                  <li
                    key={i}
                    className="flex items-start gap-2 rounded-md bg-muted px-2.5 py-1.5"
                  >
                    <Badge
                      tone={FINDING_TONE[f.level]}
                      className="mt-[1px] shrink-0"
                    >
                      {FINDING_LABEL[f.level]}
                    </Badge>
                    <span className="min-w-0 flex-1 text-[11.5px] leading-relaxed text-fg-2">
                      {f.rule ? (
                        <span className="sw-num mr-1.5 text-fg-3">
                          {f.rule}
                        </span>
                      ) : null}
                      {f.text}
                    </span>
                  </li>
                ))}
              </ul>
              {Object.keys(mr.stages_skipped ?? {}).length > 0 ? (
                <p className="mt-2 text-[10.5px] text-fg-4">
                  跳过的阶段：
                  {Object.entries(mr.stages_skipped)
                    .map(([k, v]) => `${k}（${v}）`)
                    .join("、")}
                </p>
              ) : null}
            </>
          ) : (
            <p className="text-[11.5px] text-fg-4">
              这条内容还没有机器审核结论。
            </p>
          )}
        </Section>

        <Collapsible title="正文文案" defaultOpen={false} testid="body-toggle">
          <div className="sw-prose sw-scroll max-h-64 overflow-y-auto rounded-lg bg-muted px-3 py-2.5 text-[12.5px]">
            {bundle.body_markdown || "（正文为空）"}
          </div>
          {bundle.tags.length > 0 ? (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {bundle.tags.map((t) => (
                <Badge key={t} tone="muted">
                  #{t}
                </Badge>
              ))}
            </div>
          ) : null}
        </Collapsible>

        {diff ? (
          <Collapsible
            title="最近一次人工改稿"
            defaultOpen={false}
            testid="diff-toggle"
          >
            <pre className="sw-scroll sw-num max-h-52 overflow-auto rounded-lg bg-muted px-3 py-2.5 text-[10.5px] leading-relaxed text-fg-2">
              {diff}
            </pre>
          </Collapsible>
        ) : null}

        <Section title={item.awaiting_confirm ? "发布前确认" : "排期"}>
          <div className="rounded-lg bg-muted px-3 py-2.5 text-[11.5px] text-fg-2">
            {item.awaiting_confirm ? (
              <>
                <ConfirmClock item={item} />
                <div className="mt-2.5 flex flex-wrap gap-1.5">
                  <Button
                    size="sm"
                    variant="primary"
                    onClick={() => void decide(true)}
                    loading={deciding === "confirm"}
                    data-testid="confirm-button"
                  >
                    确认发布
                  </Button>
                  <Button
                    size="sm"
                    onClick={() => void decide(false)}
                    loading={deciding === "reject"}
                    data-testid="confirm-reject-button"
                  >
                    不发
                  </Button>
                </div>
              </>
            ) : (
              <div className="sw-num">
                {slot.slot_text ? `已排期至 ${slot.slot_text}` : "尚未排期"}
              </div>
            )}
            <div className="mt-1.5 text-[11px] text-fg-3">
              账号可发窗口：{slot.account_windows || "全天"}
            </div>
          </div>
        </Section>

        <Collapsible
          title={`审计日志（${logs.length}）`}
          defaultOpen={false}
          testid="logs-toggle"
        >
          {logs.length === 0 ? (
            <p className="text-[11.5px] text-fg-4">还没有日志。</p>
          ) : (
            <ol className="relative ml-1 border-l border-line pl-3.5">
              {logs.map((l) => (
                <li key={l.id} className="relative pb-2.5 last:pb-0">
                  <span className="absolute -left-[19px] top-[5px]">
                    <LiveDot tone={l.is_human ? "amber" : "muted"} size={6} />
                  </span>
                  <div className="flex items-baseline gap-2">
                    <span className="text-[12px] text-fg">
                      {ACTION_LABEL[l.action] ?? l.action}
                    </span>
                    <span className="sw-num text-[10px] text-fg-4">
                      {fmtFullTime(l.at)}
                    </span>
                  </div>
                  <div className="mt-0.5 text-[11px] leading-relaxed text-fg-3">
                    <span className="sw-num">{l.actor}</span>
                    {l.reason ? ` · ${l.reason}` : ""}
                  </div>
                </li>
              ))}
            </ol>
          )}
        </Collapsible>
      </div>

      {/* ── 决策区：常驻底部，永远不用滚动去找批准钮 ────────────────── */}
      <div className="shrink-0 border-t border-line px-3.5 py-3">
        {item.needs_watch ? (
          <div
            ref={watchRef}
            className={cn(
              "mb-2.5 rounded-lg border-l-[3px] px-3 py-2",
              watched
                ? "border-l-ok bg-ok-soft"
                : "border-l-warn bg-warn-soft",
            )}
          >
            <Checkbox
              id="watched-gate"
              checked={watched}
              onChange={onWatchedChange}
              disabled={!canDecide}
              label="已完整观看成片"
              hint="播到底会自动勾上；勾选写进合规证据链（platform_extra.watched_by/at）。"
            />
          </div>
        ) : null}

        {canDecide ? (
          <>
            <FieldLabel htmlFor="reject-reason">
              驳回理由（必填，回传给改稿 Agent）
            </FieldLabel>
            <div className="mb-1.5 flex flex-wrap gap-1.5" data-testid="reject-reason-presets">
              {COMMON_REJECT_REASONS.map((r) => (
                <button
                  key={r}
                  type="button"
                  data-testid={`reject-reason-preset-${r}`}
                  onClick={() => appendReason(r)}
                  className="rounded-pill bg-muted px-2.5 py-1 text-[11px] text-fg-3 transition-colors duration-150 hover:bg-primary-soft hover:text-primary-deep"
                >
                  {r}
                </button>
              ))}
            </div>
            <Textarea
              id="reject-reason"
              ref={reasonRef}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              onKeyDown={(e) => {
                // Enter 直接提交驳回，Shift+Enter 换行 —— 键盘流的最后一步
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  void reject();
                }
              }}
              placeholder="Enter 提交驳回；快捷键 r 聚焦"
              data-testid="reject-reason"
              className="mb-2 min-h-[54px] text-[12.5px]"
            />
            <div className="flex items-center gap-2">
              <Button
                variant="primary"
                className="h-10 flex-1 text-[14px]"
                onClick={() => void approve()}
                loading={busy === "approve"}
                disabled={watchBlocked || busy !== null}
                data-testid="approve-button"
                title={
                  watchBlocked ? "含视频的内容必须先完整观看成片" : "快捷键 a"
                }
              >
                <IconCheck size={15} />
                批准并排期
                <kbd className="sw-num ml-1 rounded border border-current/30 px-1 text-[10px] opacity-80">
                  a
                </kbd>
              </Button>
              <Button
                variant="danger"
                className="h-10 px-4 text-[14px]"
                onClick={() => void reject()}
                loading={busy === "reject"}
                disabled={busy !== null}
                data-testid="reject-button"
                title="快捷键 r 聚焦理由，Enter 提交"
              >
                <IconX size={14} />
                驳回
              </Button>
              <Button
                variant="ghost"
                className="h-10 px-2.5"
                onClick={() => setEditOpen(true)}
                title="人工改稿"
                aria-label="人工改稿"
              >
                <IconEdit size={15} />
              </Button>
            </div>
            {watchBlocked ? (
              <p
                data-testid="watch-blocked-hint"
                className="mt-2 flex items-center gap-1.5 text-[11.5px] text-warn"
              >
                <IconAlert size={12} />
                勾选「已完整观看成片」后才能批准
              </p>
            ) : null}
          </>
        ) : (
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-[12px] text-fg-3">
              当前状态 {STATUS_LABEL[item.status] ?? item.status}
              {canEdit ? "，改完稿会回到草稿重新过审。" : "，不在人工卡点上。"}
            </span>
            {canEdit ? (
              <Button
                variant="primary"
                size="sm"
                onClick={() => setEditOpen(true)}
              >
                <IconEdit size={12} />
                改稿
              </Button>
            ) : null}
            {item.url ? (
              <a
                href={item.url}
                target="_blank"
                rel="noreferrer"
                className="flex items-center gap-1 text-[12px] text-primary hover:underline"
              >
                看线上 <IconExternal size={12} />
              </a>
            ) : null}
          </div>
        )}
      </div>

      <EditModal
        open={editOpen}
        onClose={() => setEditOpen(false)}
        itemId={item.id}
        initialTitle={bundle.title}
        initialBody={bundle.body_markdown}
        initialTags={bundle.tags}
        onSaved={onEdited}
      />
    </aside>
  );
});

function Section({
  title,
  icon,
  children,
}: {
  title: string;
  icon?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="mb-4 last:mb-0">
      <h3 className="sw-label mb-1.5 flex items-center gap-1.5">
        {icon}
        {title}
      </h3>
      {children}
    </section>
  );
}

/** 折叠块。正文 / diff / 日志都折起来——决策栏的主角是那两个按钮。 */
function Collapsible({
  title,
  defaultOpen,
  testid,
  children,
}: {
  title: string;
  defaultOpen?: boolean;
  testid?: string;
  children: React.ReactNode;
}) {
  const [open, setOpen] = React.useState(Boolean(defaultOpen));
  return (
    <section className="mb-4 last:mb-0">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        data-testid={testid}
        className="sw-label mb-1.5 flex w-full items-center gap-1.5 hover:text-fg-2"
      >
        <IconChevronDown
          size={12}
          className={cn("transition-transform", !open && "-rotate-90")}
        />
        {title}
      </button>
      {open ? children : null}
    </section>
  );
}

function EditModal({
  open,
  onClose,
  itemId,
  initialTitle,
  initialBody,
  initialTags,
  onSaved,
}: {
  open: boolean;
  onClose: () => void;
  itemId: string;
  initialTitle: string;
  initialBody: string;
  initialTags: string[];
  onSaved: () => void;
}) {
  const toast = useToast();
  const [title, setTitle] = React.useState(initialTitle);
  const [body, setBody] = React.useState(initialBody);
  const [tags, setTags] = React.useState(initialTags.join("、"));
  const [busy, setBusy] = React.useState(false);

  React.useEffect(() => {
    if (!open) return;
    setTitle(initialTitle);
    setBody(initialBody);
    setTags(initialTags.join("、"));
  }, [open, initialTitle, initialBody, initialTags]);

  async function save() {
    setBusy(true);
    try {
      await apiFetch<WriteResult>(`/review/${itemId}/edit`, {
        method: "POST",
        body: {
          actor: ACTOR,
          title,
          body_markdown: body,
          // 后端要的是数组（表单端点那边才是逗号串）
          tags: tags
            .split(/[、,，\s]+/)
            .map((t) => t.trim())
            .filter(Boolean),
        },
      });
      toast.ok("已保存改稿，状态回到草稿");
      onSaved();
      onClose();
    } catch (e) {
      toast.err(describeError(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="人工改稿"
      description="保存后状态回到草稿，before/after 进审计日志，右栏立刻能看到 diff。"
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            取消
          </Button>
          <Button variant="primary" onClick={() => void save()} loading={busy}>
            保存
          </Button>
        </>
      }
    >
      <FieldLabel htmlFor="edit-title">标题</FieldLabel>
      <Input
        id="edit-title"
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        className="mb-3"
      />
      <FieldLabel htmlFor="edit-body">正文 markdown</FieldLabel>
      <Textarea
        id="edit-body"
        value={body}
        onChange={(e) => setBody(e.target.value)}
        className="mb-3 min-h-[180px]"
      />
      <FieldLabel htmlFor="edit-tags">标签（顿号 / 逗号分隔）</FieldLabel>
      <Input
        id="edit-tags"
        value={tags}
        onChange={(e) => setTags(e.target.value)}
      />
    </Modal>
  );
}

export default DecisionPanel;
