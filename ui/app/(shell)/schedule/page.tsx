"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import * as React from "react";

import {
  IconCalendar,
  IconClock,
  IconExternal,
  IconRefresh,
  IconRotate,
} from "@/components/icons";
import { RescheduleModal } from "@/components/content/reschedule-modal";
import { PageHeader } from "@/components/layout/page-header";
import { CoverThumb } from "@/components/review/thumbs";
import { isOnAir, ScheduleBand, timeOf } from "@/components/today/schedule-band";
import { Badge } from "@/components/ui/badge";
import { ConfirmClock } from "@/components/ui/confirm-clock";
import { Button } from "@/components/ui/button";
import { DataTable, Ellipsis, type Column } from "@/components/ui/data-table";
import { EmptyState } from "@/components/ui/empty-state";
import { FilterMenu } from "@/components/ui/filter-menu";
import { LiveDot } from "@/components/ui/live-dot";
import { MenuItem } from "@/components/ui/popover";
import { RowMenu } from "@/components/ui/row-menu";
import { SectionPanel } from "@/components/ui/section-panel";
import { SegmentedControl } from "@/components/ui/segmented";
import { SkeletonTable } from "@/components/ui/skeleton";
import { paginate, TablePager } from "@/components/ui/table-pager";
import { useToast } from "@/components/ui/toast";
import { apiFetch, describeError } from "@/lib/api";
import {
  browserTimeZone,
  fmtClock,
  fmtTime,
  PLATFORM_LABEL,
  STATUS_LABEL,
  toneForStatus,
  zoneNote,
} from "@/lib/format";
import { useApi } from "@/lib/hooks";
import { resolveZone } from "@/lib/tz";
import type {
  AccountRow,
  ConfirmResult,
  ContentRow,
  Page as PageT,
  RetryResult,
} from "@/lib/types";
import { DAY_MINUTES, dayKey, todayRangeIso } from "@/lib/windows";
import { cn } from "@/lib/utils";

/**
 * 内容 → 它所属账号的时区。
 *
 * 排期页同时拿着 `/content` 与 `/accounts`，按 `account_id` join 出 `timezone`；
 * 时刻显示、按天分组、改期弹窗全用这一个函数取时区，只此一处口径。
 * 账号还没加载出来（或库里查无此号）时返回空串，下游 `resolveZone` 会回退浏览器本地
 * 并让界面标注出来。
 */
type TzOf = (accountId: string) => string;

/** 状态漏斗：从生成到回收的主干，外加两条异常支线。 */
const FUNNEL = [
  { value: "", label: "全部状态" },
  { value: "scheduled", label: "已排期" },
  { value: "published", label: "已发布" },
  { value: "publish_failed", label: "发布失败", tone: "err" as const },
  { value: "suspended", label: "已挂起", tone: "warn" as const },
  { value: "dead_letter", label: "死信", tone: "err" as const },
];

const RANGES = [
  { value: "1", label: "今天" },
  { value: "7", label: "近 7 天" },
  { value: "30", label: "近 30 天" },
  { value: "", label: "全部" },
];

const RESCHEDULABLE = new Set(["approved", "scheduled", "suspended"]);
const RETRYABLE = new Set(["retrying", "publish_failed", "dead_letter"]);

const WEEKDAY = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];

export default function SchedulePage() {
  return (
    <React.Suspense fallback={<SchedulePending />}>
      <ScheduleView />
    </React.Suspense>
  );
}

/** 首屏骨架：页头占位 + 表格骨架，与真身逐行等高。 */
function SchedulePending() {
  return (
    <div className="flex h-full w-full max-w-5xl flex-col gap-4 p-4 md:p-6">
      <div className="h-7 w-40 shrink-0" />
      <SkeletonTable rows={10} cols={5} />
    </div>
  );
}

/**
 * 排期 —— 过去与未来在同一根时间轴上。
 *
 * 两个视图：**时间线**（按天分组，每天一条 0–24 时带，底色 = 账号发布窗口）
 * 与**列表**（DataTable，按状态扫一遍）。过滤口径是后端的
 * `coalesce(scheduled_at, updated_at)`，已发布的内容也带着它当初的排期时刻。
 *
 * 页面容器是 `h-full flex-col`：外壳锁死了视口高，列表视图靠它把表格撑满
 * 剩余高度、行在框内滚、分页条钉底。
 */
function ScheduleView() {
  const toast = useToast();
  const router = useRouter();
  const search = useSearchParams();
  const focusId = search.get("id") ?? "";

  const [status, setStatus] = React.useState(search.get("status") ?? "");
  const [view, setView] = React.useState<"timeline" | "list">("timeline");
  const [range, setRange] = React.useState("7");
  const [target, setTarget] = React.useState<ContentRow | null>(null);
  const [page, setPage] = React.useState(1);

  const now = React.useMemo(() => new Date(), []);
  const { data: accounts } = useApi<AccountRow[]>("/accounts");

  // 账号时区集合。`join` 出来的字符串做依赖，免得每次 render 都换一个新数组身份
  const zoneKey = React.useMemo(
    () => [...new Set((accounts ?? []).map((a) => a.policy.timezone))].sort().join("|"),
    [accounts],
  );
  const zones = React.useMemo(() => zoneKey.split("|").filter(Boolean), [zoneKey]);

  const from = React.useMemo(() => {
    if (!range) return undefined;
    const days = Number(range);
    // 起点取各账号时区"今天 00:00"里**最早**的那个：浏览器在 UTC-7 时，
    // Asia/Shanghai 账号今天 12:00 的稿在浏览器本地日里还是昨天，
    // 按浏览器本地零点切会把它整条截掉。
    const todayStart = new Date(todayRangeIso(now, zones).from).getTime();
    // 「今天」= 今天这一天；「近 N 天」= 往回 N-1 天，未来那侧不设上限（排期都在未来）
    return new Date(todayStart - (days - 1) * DAY_MINUTES * 60_000).toISOString();
  }, [now, range, zones]);

  const { data, error, isLoading, mutate } = useApi<PageT<ContentRow>>("/content", {
    status,
    from,
    limit: 200,
  });

  const items = React.useMemo(() => data?.items ?? [], [data]);

  // 筛选一变，之前那个页码多半已经越界；回第一页是唯一不会让人看见空表的选择
  React.useEffect(() => {
    setPage(1);
  }, [status, range, view]);

  // ⌘K / 今日时间带点进来时高亮那一行
  React.useEffect(() => {
    if (!focusId) return;
    const el = document.querySelector(`[data-item-id="${CSS.escape(focusId)}"]`);
    el?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [focusId, items, page, view]);

  /**
   * 工作台里的兜底确认。走的是和 Telegram 那个按钮**同一个后端函数**，
   * 所以两条路的语义、审计日志、重放保护完全一致——Telegram 不是单点：
   * bot 挂了、手机不在身边，这里照样能把稿子放出去。
   */
  async function decide(item: ContentRow, go: boolean) {
    try {
      const res = await apiFetch<ConfirmResult>(`/content/${item.id}/${go ? "confirm" : "reject"}`, {
        method: "POST",
        body: go ? {} : { reason: "在工作台点了「不发」" },
      });
      toast.ok(res.message);
      await mutate();
    } catch (e) {
      toast.err(describeError(e));
    }
  }

  async function retry(item: ContentRow) {
    try {
      const res = await apiFetch<RetryResult>(`/content/${item.id}/retry_now`, { method: "POST" });
      toast.ok(res.message);
      await mutate();
    } catch (e) {
      toast.err(describeError(e));
    }
  }

  const tzOf = React.useMemo<TzOf>(() => {
    const map = new Map((accounts ?? []).map((a) => [a.id, a.policy.timezone]));
    return (accountId: string) => map.get(accountId) ?? "";
  }, [accounts]);

  // 改期弹窗的快捷槽位药丸要按账号的发布窗口算，同一份账号数据多取一个字段而已
  const windowsOf = React.useMemo(() => {
    const map = new Map((accounts ?? []).map((a) => [a.id, a.policy.publish_windows]));
    return (accountId: string) => map.get(accountId) ?? "";
  }, [accounts]);

  const groups = React.useMemo(() => groupByDay(items, tzOf), [items, tzOf]);

  /**
   * "今天"是**每个账号各自的今天**。多时区下同一瞬间可能分属两个日期，
   * 所以这里收的是一个键集合：任一账号时区认为它是今天，那一组就打"今天"角标。
   */
  const todayKeys = React.useMemo(() => {
    const keys = new Set<string>([dayKey(now, browserTimeZone())]);
    for (const a of accounts ?? []) keys.add(dayKey(now, a.policy.timezone));
    return keys;
  }, [accounts, now]);

  // 浏览器与账号不同区时，说一句"这里的时刻是谁的钟点"——轻提示，不做告警条
  const foreignZones = React.useMemo(() => {
    const local = browserTimeZone();
    const set = new Set<string>();
    for (const a of accounts ?? []) {
      const { zone } = resolveZone(a.policy.timezone);
      if (zone !== local) set.add(zone);
    }
    return [...set].sort();
  }, [accounts]);

  const slice = paginate(items, page);

  const actions = (
    <>
      <SegmentedControl
        label="视图"
        value={view}
        onChange={(v) => setView(v as "timeline" | "list")}
        options={[
          { value: "timeline", label: "时间线" },
          { value: "list", label: "列表" },
        ]}
      />
      <SegmentedControl label="时间范围" value={range} onChange={setRange} options={RANGES} />
      <Button size="sm" onClick={() => void mutate()}>
        <IconRefresh size={12} />
        刷新
      </Button>
    </>
  );

  /* 时刻列。账号时区口径（P11.1）：`data-zone` 与 `title` 是 e2e 的观测点，
     也是运营核对"这是谁的钟点"的依据，两者缺一都会让时刻变成裸数字。 */
  const clockCell = (it: ContentRow) => {
    const note = zoneNote(tzOf(it.account_id));
    const tz = tzOf(it.account_id);
    return (
      <span
        className="sw-num whitespace-nowrap text-[12.5px] text-fg-2"
        data-testid="row-clock"
        data-zone={resolveZone(tz).zone}
        title={note ? `${fmtTime(timeOf(it), tz)}（${note}）` : fmtTime(timeOf(it), tz)}
      >
        {fmtClock(timeOf(it), tz)}
      </span>
    );
  };

  const columns: Column<ContentRow>[] = [
    {
      key: "clock",
      header: "时刻",
      cell: clockCell,
    },
    {
      key: "title",
      header: "内容",
      // 唯一吸收剩余宽度的一列：w-full 抢余量、max-w-0 允许被压到 0，
      // 真正的截断发生在内层 span 上（Ellipsis）
      className: "w-full max-w-0",
      cell: (it) => (
        <div className="flex min-w-0 items-center gap-2.5">
          <CoverThumb
            src={it.cover_url}
            kind={it.media.videos > 0 ? "video" : "image"}
            className="h-7 w-7 shrink-0"
          />
          <span className="min-w-0 flex-1">
            <Ellipsis className="text-[12.5px] text-fg" title={it.title || it.id}>
              {it.title || it.id}
            </Ellipsis>
            {it.last_error ? (
              <Ellipsis className="sw-num text-[10.5px] text-err" title={it.last_error}>
                {it.last_error}
              </Ellipsis>
            ) : null}
          </span>
        </div>
      ),
    },
    {
      key: "account",
      header: "账号",
      className: "max-w-[11rem]",
      cell: (it) => (
        <span className="flex min-w-0 flex-col">
          <Ellipsis className="sw-num text-[11.5px] text-fg-2" title={it.account_id}>
            {it.account_id}
          </Ellipsis>
          <span className="sw-keep text-[10.5px] text-fg-4">
            {PLATFORM_LABEL[it.platform] ?? it.platform}
          </span>
        </span>
      ),
    },
    {
      key: "status",
      header: "状态",
      cell: (it) => (
        <span className="flex flex-col items-start gap-1">
          <span className="flex items-center gap-1.5">
            <LiveDot tone={toneForStatus(it.status)} pulse={it.status === "publishing"} size={6} />
            <Badge tone={toneForStatus(it.status)}>{STATUS_LABEL[it.status] ?? it.status}</Badge>
          </span>
          <ConfirmClock item={it} tz={tzOf(it.account_id)} compact />
        </span>
      ),
    },
    {
      key: "attempts",
      header: "尝试",
      align: "right",
      cell: (it) => (
        <span className="sw-num text-[12px] text-fg-3">{it.attempts > 0 ? it.attempts : "—"}</span>
      ),
    },
    {
      key: "actions",
      header: "",
      // 操作列不设宽：内容全是 nowrap 的按钮，让它按内容量宽即可。
      // 真正的保证来自"只有一列吸收余量"——操作列永远不会被压缩。
      className: "text-right",
      cell: (it) => (
        <RowActions
          item={it}
          onReschedule={() => setTarget(it)}
          onRetry={() => void retry(it)}
          onDecide={(go) => void decide(it, go)}
        />
      ),
    },
  ];

  return (
    <div className="flex h-full w-full max-w-5xl flex-col gap-4 p-4 md:p-6">
      <PageHeader title="排期" emphasis="几点发什么" actions={actions} />

      {view === "list" ? (
        error ? (
          <LoadFailure error={error} onRetry={() => void mutate()} />
        ) : isLoading ? (
          <SkeletonTable rows={10} cols={6} />
        ) : (
          <DataTable
            fill
            columns={columns}
            rows={slice.rows}
            rowKey={(it) => it.id}
            rowProps={(it) => ({
              "data-testid": "content-row",
              "data-item-id": it.id,
              className: it.id === focusId ? "bg-primary-soft" : undefined,
            })}
            toolbar={
              <>
                <FilterMenu label="状态" value={status} options={FUNNEL} onChange={onStatus} />
                <span className="flex-1" />
                <ZoneNote zones={foreignZones} />
              </>
            }
            footer={
              <TablePager
                page={slice.page}
                pages={slice.pages}
                total={slice.total}
                onPage={setPage}
              />
            }
            empty={<ScheduleEmpty />}
          />
        )
      ) : (
        <SectionPanel
          className="min-h-0 flex-1"
          title="发布时间线"
          subtitle={`共 ${data?.total ?? 0} 条${status ? ` · ${STATUS_LABEL[status] ?? status}` : ""}`}
          actions={<FilterMenu label="状态" value={status} options={FUNNEL} onChange={onStatus} />}
          bodyClassName="sw-scroll min-h-0 overflow-y-auto"
        >
          <ZoneNote zones={foreignZones} block />

          {error ? (
            <LoadFailure error={error} onRetry={() => void mutate()} className="m-4" />
          ) : isLoading ? (
            <div className="p-4">
              <SkeletonTable rows={6} cols={4} />
            </div>
          ) : items.length === 0 ? (
            <div className="p-6">
              <ScheduleEmpty />
            </div>
          ) : (
            <div className="px-3 py-3">
              {groups.map(([day, rows]) => {
                const isToday = todayKeys.has(day);
                // 只给"真的落在发布轴上"的内容画泳道；纯草稿的一天不画空带子
                const lanes = (accounts ?? []).filter((a) =>
                  rows.some((r) => r.account_id === a.id && isOnAir(r)),
                );
                return (
                  <section key={day} className="mb-4 last:mb-0" data-testid="day-group">
                    <div className="mb-1.5 flex items-center gap-2">
                      <span className="sw-num text-[12.5px] font-medium text-fg">{day}</span>
                      <span className="text-[11.5px] text-fg-4">{weekdayOf(day)}</span>
                      {isToday ? <Badge tone="amber">今天</Badge> : null}
                      <span className="h-px flex-1 bg-line" />
                      <span className="sw-num text-[11px] text-fg-4">{rows.length} 条</span>
                    </div>

                    {lanes.length > 0 ? (
                      <div className="mb-2 overflow-hidden rounded-md">
                        <ScheduleBand
                          accounts={lanes}
                          items={rows}
                          now={now}
                          day={day}
                          showNow={isToday}
                          showLegend={false}
                          className="px-3 py-2.5"
                        />
                      </div>
                    ) : null}

                    <ul className="flex flex-col gap-1">
                      {rows.map((it) => (
                        <TimelineRow
                          key={it.id}
                          item={it}
                          tz={tzOf(it.account_id)}
                          focused={it.id === focusId}
                          onReschedule={() => setTarget(it)}
                          onRetry={() => void retry(it)}
                          onDecide={(go) => void decide(it, go)}
                        />
                      ))}
                    </ul>
                  </section>
                );
              })}
            </div>
          )}
        </SectionPanel>
      )}

      {/*
        改期弹窗挂在**行操作菜单之外**、页面这一层受控：菜单一关就整个卸载，
        弹窗写在菜单项里会跟着消失（dormice 实锤过的坑）。
      */}
      <RescheduleModal
        item={target}
        timezone={target ? tzOf(target.account_id) : ""}
        windows={target ? windowsOf(target.account_id) : ""}
        open={Boolean(target)}
        onClose={() => setTarget(null)}
        onDone={() => void mutate()}
      />
    </div>
  );

  function onStatus(v: string) {
    setStatus(v);
    router.replace(v ? `/schedule/?status=${v}` : "/schedule/", { scroll: false });
  }
}

/** 空态承接了原来页头那段解释：只有真空着的时候才需要读它。 */
function ScheduleEmpty() {
  return (
    <EmptyState
      icon={<IconCalendar />}
      title="这个范围里没有内容"
      description="换个状态或把时间范围拉长。批准一条稿子后，它会按账号的发布窗口落到最近的合法槽位上，然后出现在这里。"
    />
  );
}

function LoadFailure({
  error,
  onRetry,
  className,
}: {
  error: unknown;
  onRetry: () => void;
  className?: string;
}) {
  return (
    <EmptyState
      className={className}
      title="没能取到排期"
      description={describeError(error)}
      action={
        <Button size="sm" onClick={onRetry}>
          <IconRefresh size={12} />
          重试
        </Button>
      }
    />
  );
}

/**
 * 「你看到的时刻是谁的钟点」。
 * 轻提示，不是告警条：只在浏览器与账号确实不同区时出现。做成刺眼的红黄条会
 * 让人以为出事了——其实一切正常，只是你人不在那个时区。
 */
function ZoneNote({ zones, block }: { zones: string[]; block?: boolean }) {
  if (zones.length === 0) return null;
  const text = (
    <>
      时刻按账号时区显示（{zones.join("、")}）；你的浏览器在 {browserTimeZone()}。
    </>
  );
  if (block) {
    return (
      <p className="sw-num border-b border-line px-4 py-2 text-[11px] text-fg-4" data-testid="zone-hint">
        {text}
      </p>
    );
  }
  return (
    <span className="sw-num truncate text-[11px] text-fg-4" data-testid="zone-hint">
      {text}
    </span>
  );
}

/**
 * 按**账号本地日**分组，日期倒序（新的在上），组内按时刻正序（早的在上）。
 *
 * 分组键必须按账号时区算：账号时区 19:00 的稿在 UTC-7 的浏览器上是前一天的 04:00，
 * 按浏览器时区分组会把它整行挪到错误的那一天去（比点位画错更难发现）。
 */
function groupByDay(items: ContentRow[], tzOf: TzOf): [string, ContentRow[]][] {
  const map = new Map<string, ContentRow[]>();
  for (const it of items) {
    const key = dayKey(timeOf(it), tzOf(it.account_id));
    if (!key) continue;
    const arr = map.get(key) ?? [];
    arr.push(it);
    map.set(key, arr);
  }
  return [...map.entries()]
    .sort((a, b) => (a[0] < b[0] ? 1 : -1))
    .map(([day, rows]) => [
      day,
      // 组内按**绝对时刻**排序，与时区无关；跨时区的同组内容也就自然按先后排好
      rows.sort(
        (a, b) => new Date(timeOf(a) ?? 0).getTime() - new Date(timeOf(b) ?? 0).getTime(),
      ),
    ]);
}

/**
 * 日期键 → 星期几。
 *
 * 走 UTC 解读这个**已经不带时区**的日期串：`new Date("2026-08-17T00:00:00")` 按浏览器
 * 本地零点解析，在零点不存在的时区（夏令时前跳）上会滚到别的时刻；日期串本身无关时区，
 * 用 UTC 读最稳。
 */
function weekdayOf(day: string): string {
  const m = day.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!m) return "";
  const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])));
  return Number.isNaN(d.getTime()) ? "" : WEEKDAY[d.getUTCDay()];
}

/**
 * 行操作 —— **一个主操作直出 + 一个改期图标 + 其余进「⋯」**。
 *
 * 原来一行最多长出五个同等权重的按钮（确认发布 / 不发 / 改期 / 重投 / 详情），
 * 把行右侧撑成一片按钮墙：列一窄就换行，行高忽高忽低，人还得逐个读完才知道
 * 该点哪个。现在最多两个可见控件，其余收起来。
 *
 * 为什么改期是**直出的图标钮**而不是菜单项：它是这一页的高频动作（人来排期页
 * 十有八九就是为了挪个时间），藏进菜单等于每次多两步；但它又不该占一个文字
 * 按钮的宽度去和"确认发布"抢眼——图标 + aria-label / title 两者兼得。
 *
 * 动作名与 Telegram 卡片上那两个按钮**逐字一致**（确认发布 / 不发）：
 * 同一件事换个说法，人在每一处都得重新判断一次这是不是同一个操作。
 */
function RowActions({
  item,
  onReschedule,
  onRetry,
  onDecide,
}: {
  item: ContentRow;
  onReschedule: () => void;
  onRetry: () => void;
  onDecide: (go: boolean) => void;
}) {
  return (
    <span className="flex items-center justify-end gap-1">
      {item.awaiting_confirm ? (
        <Button
          size="sm"
          variant="primary"
          onClick={() => onDecide(true)}
          data-testid="confirm-button"
        >
          确认发布
        </Button>
      ) : RETRYABLE.has(item.status) ? (
        <Button size="sm" variant="danger" onClick={onRetry}>
          <IconRotate size={12} />
          {item.status === "dead_letter" ? "复投" : "重投"}
        </Button>
      ) : null}

      {RESCHEDULABLE.has(item.status) ? (
        <Button
          size="icon"
          variant="ghost"
          onClick={onReschedule}
          title="改期"
          aria-label="改期"
          data-testid="reschedule-button"
        >
          <IconClock size={14} />
        </Button>
      ) : null}

      <RowMenu>
        {(close) => (
          <>
            {item.awaiting_confirm ? (
              <MenuItem
                destructive
                onSelect={() => {
                  close();
                  onDecide(false);
                }}
              >
                <span data-testid="reject-button">不发</span>
              </MenuItem>
            ) : null}
            <MenuItem onSelect={close}>
              <Link href={`/review/?id=${encodeURIComponent(item.id)}`}>查看详情</Link>
            </MenuItem>
            {item.url ? (
              <MenuItem icon={<IconExternal />} onSelect={close}>
                <a href={item.url} target="_blank" rel="noreferrer">
                  打开线上链接
                </a>
              </MenuItem>
            ) : null}
          </>
        )}
      </RowMenu>
    </span>
  );
}

function TimelineRow({
  item,
  tz,
  focused,
  onReschedule,
  onRetry,
  onDecide,
}: {
  item: ContentRow;
  /** 该内容所属账号的时区。时刻按它显示，与上方时间带同一根轴。 */
  tz: string;
  focused: boolean;
  onReschedule: () => void;
  onRetry: () => void;
  onDecide: (go: boolean) => void;
}) {
  const tone = toneForStatus(item.status);
  const note = zoneNote(tz);
  return (
    <li
      data-testid="content-row"
      data-item-id={item.id}
      className={cn(
        "flex items-center gap-3 rounded-md px-3 py-1.5 transition-colors duration-150",
        focused ? "border-l-[3px] border-l-primary bg-primary-soft" : "hover:bg-row-hover",
      )}
    >
      <span
        className="sw-num w-11 shrink-0 text-[12.5px] text-fg-2"
        data-testid="row-clock"
        data-zone={resolveZone(tz).zone}
        title={note ? `${fmtTime(timeOf(item), tz)}（${note}）` : fmtTime(timeOf(item), tz)}
      >
        {fmtClock(timeOf(item), tz)}
      </span>
      <LiveDot tone={tone} pulse={item.status === "publishing"} size={7} />
      <CoverThumb
        src={item.cover_url}
        kind={item.media.videos > 0 ? "video" : "image"}
        className="h-7 w-7 shrink-0"
      />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[12.5px] text-fg">{item.title || item.id}</span>
        <span className="mt-0.5 flex flex-wrap items-center gap-1.5">
          <Badge tone={tone}>{STATUS_LABEL[item.status] ?? item.status}</Badge>
          <span className="sw-num text-[10.5px] text-fg-4">
            {PLATFORM_LABEL[item.platform] ?? item.platform} · {item.account_id}
            {/* 与浏览器同区时 note 是空串——同区还标一遍纯属噪音 */}
            {note ? ` · 时刻按 ${note}` : ""}
          </span>
          <ConfirmClock item={item} tz={tz} compact />
          {item.last_error ? (
            <span className="sw-num truncate text-[10.5px] text-err">{item.last_error}</span>
          ) : null}
        </span>
      </span>
      <span className="shrink-0">
        <RowActions
          item={item}
          onReschedule={onReschedule}
          onRetry={onRetry}
          onDecide={onDecide}
        />
      </span>
    </li>
  );
}
