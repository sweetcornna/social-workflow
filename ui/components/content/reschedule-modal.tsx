"use client";

import * as React from "react";

import { IconAlert, IconClock } from "@/components/icons";
import { Button } from "@/components/ui/button";
import { FieldLabel, Input } from "@/components/ui/field";
import { Modal } from "@/components/ui/modal";
import { useToast } from "@/components/ui/toast";
import { apiFetch, ApiFailure, describeError } from "@/lib/api";
import { browserTimeZone, fromLocalInputValue, toLocalInputValue } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import { resolveZone } from "@/lib/tz";
import type { AvailableSlotsResult, ContentRow, RescheduleResult } from "@/lib/types";
import { quickSlots } from "@/lib/windows";

/** 弹窗里实际渲染的一枚快捷槽位药丸——不管来源是后端真值还是前端估算，形状统一。 */
interface QuickSlotPill {
  key: string;
  label: string;
  iso: string;
}

/** 后端 422 `invalid_slot` 的 detail 形状。 */
export interface SlotHint {
  reason: string;
  suggested_slot: string | null;
  suggested_slot_text: string | null;
  account_windows: string;
}

export function parseSlotHint(err: unknown): SlotHint | null {
  if (!(err instanceof ApiFailure) || err.code !== "invalid_slot" || !err.detail) return null;
  const d = err.detail as Record<string, unknown>;
  return {
    reason: String(d.reason ?? ""),
    suggested_slot: (d.suggested_slot as string | null) ?? null,
    suggested_slot_text: (d.suggested_slot_text as string | null) ?? null,
    account_windows: String(d.account_windows ?? ""),
  };
}

/**
 * 改期弹窗。
 *
 * 改期走的是与"批准即排期"完全同一套校验（core/scheduling.py 的 SlotConstraints），
 * 所以前端不做任何本地合法性判断——挑错了就让后端 422 回来，把
 * `detail.suggested_slot` 做成"改用这个时间"的一键按钮。
 *
 * **输入框里的钟点是账号时区的**。`<input type="datetime-local">` 本身不带时区，
 * 以前按浏览器本地时区读写：运营在 UTC-7 想排「19:00」，提交出去的是账号时区次日 02:00，
 * 于是要么被 422 `invalid_slot` 挡回，要么排到一个合法但根本不是他要的时刻。
 * 现在显示与解析都按 `timezone` 走，并在标签上把这件事说明白。
 */
export function RescheduleModal({
  item,
  timezone,
  windows,
  open,
  onClose,
  onDone,
}: {
  item: ContentRow | null;
  /**
   * 该内容所属账号的 IANA 时区。输入框按它显示与解析。
   *
   * 传空 / 传了个非法值时回退到浏览器本地时区，并在标签上如实标注是回退来的——
   * 静默按浏览器时区提交正是这次要修的缺陷本身。
   */
  timezone?: string;
  /**
   * 账号的 `publish_windows` 展示文案（P14.B4），用来算「今天首窗 / 明天首窗」
   * 两枚快捷槽位。**P19.2 起只是兜底**——弹窗打开时优先拉后端
   * `GET /content/{id}/slots` 真值（见下面 `slotsPath`），这份前端估算只在
   * 后端没答上来（没拉到、拉失败、明确说没有）时才顶上；两边都拆不出/给不出
   * 才不渲染快捷槽位，直接退回自选时间——不猜、不编一个"全天"出来。
   */
  windows?: string;
  open: boolean;
  onClose: () => void;
  onDone: (res: RescheduleResult) => void;
}) {
  const toast = useToast();
  const [value, setValue] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [hint, setHint] = React.useState<SlotHint | null>(null);

  const { zone, fallback } = resolveZone(timezone);
  const localZone = browserTimeZone();

  React.useEffect(() => {
    if (!open || !item) return;
    setValue(toLocalInputValue(item.scheduled_at ?? new Date().toISOString(), zone));
    setHint(null);
  }, [open, item, zone]);

  /*
   * P19.2：快捷槽位改用后端真值。`GET /content/{id}/slots`（P19.1）复用
   * `core/scheduling.py:available_slots()`——真的知道 min_interval、当天已用
   * 配额、其它已排期项，不会像旧版 `quickSlots()` 那样偶尔撞上后端校验。
   * 弹窗打开时才拉（`slotsPath` 在关闭/无内容时为 null，SWR 惯例的条件跳过），
   * 不额外传 `count`，吃后端默认的 6 个。
   *
   * 后端没答上来——还没拉回来、请求失败、或它明确说没有——一律**静默**落回
   * `quickSlots()` 这份纯前端估算，不在弹窗里冒错（P19.1 兜底路径仍在：
   * 前端估算挑错了，就让既有的 422 `invalid_slot` → `suggested_slot` 兜底接住，
   * 见下面的 `submit()`）。
   */
  const slotsPath = open && item ? `/content/${item.id}/slots` : null;
  const { data: slotsData } = useApi<AvailableSlotsResult>(slotsPath);

  // 前端估算只在弹窗打开时算一次——用 `open` 而不是每次 render 都重算，
  // 免得输入框每敲一个字，「今天」的余量窗口就跟着抖一次
  const fallbackSlots = React.useMemo(
    () => (open ? quickSlots(windows, zone) : []),
    [open, windows, zone],
  );
  const backendSlots = slotsData?.slots ?? [];
  const slots: QuickSlotPill[] =
    backendSlots.length > 0
      ? backendSlots.map((s, i) => ({ key: `backend-${i}`, label: s.slot_text, iso: s.at }))
      : fallbackSlots;
  // 后端答上来了、但明确说没有槽位（账号被封/停用、14 天内排不进去）——原话当只读
  // 小字亮出来，不重新组织措辞（P14 冻结：后端回传原话直显）
  const slotsNote = backendSlots.length === 0 ? (slotsData?.note ?? null) : null;

  async function submit(isoOverride?: string) {
    if (!item) return;
    const iso = isoOverride ?? fromLocalInputValue(value, zone);
    if (!iso) {
      toast.err("请先选一个时间");
      return;
    }
    setBusy(true);
    try {
      const res = await apiFetch<RescheduleResult>(`/content/${item.id}/reschedule`, {
        method: "POST",
        body: { scheduled_at: iso, actor: "operator" },
      });
      toast.ok(res.message);
      onDone(res);
      onClose();
    } catch (e) {
      const parsed = parseSlotHint(e);
      if (parsed) {
        setHint(parsed);
        toast.warn(describeError(e));
      } else {
        toast.err(describeError(e));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="改期"
      description={
        item
          ? `${item.title} · ${item.account_id}。窗口、最小间隔、日上限都由后端把关。`
          : undefined
      }
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            取消
          </Button>
          <Button variant="primary" onClick={() => void submit()} loading={busy}>
            改到这个时间
          </Button>
        </>
      }
    >
      {slots.length > 0 ? (
        <div className="mb-3">
          <FieldLabel>快捷槽位</FieldLabel>
          <div className="flex flex-wrap gap-1.5" data-testid="reschedule-quick-slots">
            {slots.map((s) => (
              <button
                key={s.key}
                type="button"
                data-testid={`reschedule-quick-${s.key}`}
                disabled={busy}
                onClick={() => void submit(s.iso)}
                className="rounded-pill bg-primary-soft px-3 py-1.5 text-[12.5px] font-medium text-primary-deep transition-colors duration-150 hover:bg-primary-line disabled:opacity-45"
              >
                {s.label}
              </button>
            ))}
          </div>
          {slotsNote ? (
            <p className="mt-1.5 text-[11px] text-fg-4" data-testid="reschedule-slots-note">
              {slotsNote}
            </p>
          ) : null}
        </div>
      ) : slotsNote ? (
        <p className="mb-3 text-[11px] text-fg-4" data-testid="reschedule-slots-note">
          {slotsNote}
        </p>
      ) : null}

      <FieldLabel htmlFor="reschedule-at">
        {fallback
          ? `自选时间（这个号没配时区，按你的浏览器时区 ${zone} 填）`
          : `自选时间 —— 按账号时区（${zone}）填`}
      </FieldLabel>
      <Input
        id="reschedule-at"
        type="datetime-local"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        data-testid="reschedule-input"
        data-zone={zone}
        className="sw-num"
      />
      {/* 轻提示：人不在账号那个时区时，最容易照着自己的钟点填错 */}
      {!fallback && zone !== localZone ? (
        <p className="sw-num mt-1.5 text-[11px] text-fg-4" data-testid="reschedule-zone-hint">
          浏览器时区 {localZone}；此处按账号时区填写，窗口与日上限同此口径。
        </p>
      ) : null}
      {item?.slot_text ? (
        <p className="sw-num mt-2 flex items-center gap-1.5 text-[11.5px] text-fg-3">
          <IconClock size={12} />
          当前排期 {item.slot_text}
        </p>
      ) : null}

      {hint ? (
        <div
          className="mt-3 rounded-lg border-l-[3px] border-l-warn bg-warn-soft px-3 py-2.5"
          data-testid="slot-hint"
        >
          <div className="flex items-center gap-1.5 text-[12.5px] font-medium text-warn">
            <IconAlert size={13} />
            未通过校验：{hint.reason}
          </div>
          <p className="sw-num mt-1 text-[11.5px] text-fg-2">
            账号窗口 {hint.account_windows}
          </p>
          {hint.suggested_slot ? (
            <Button
              size="sm"
              variant="primary"
              className="mt-2"
              data-testid="use-suggested-slot"
              onClick={() => void submit(hint.suggested_slot ?? undefined)}
              loading={busy}
            >
              改用 {hint.suggested_slot_text}
            </Button>
          ) : (
            <p className="mt-1.5 text-[11.5px] text-fg-3">
              14 天内没有可用槽位。放宽发布窗口或提高日上限后重试。
            </p>
          )}
        </div>
      ) : null}
    </Modal>
  );
}

export default RescheduleModal;
