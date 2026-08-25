/**
 * `/api/v1` 的 TypeScript 影子类型。
 *
 * 唯一真相是 docs/WORKBENCH_API.md（以及后端自动生成的 /openapi.json）。
 * 这里只做"抄一遍字段名"，不做任何口径换算——凡是后端已经算好的量
 * （used_today / quota_left / timeline_at / slot_text）一律直接用。
 */

export type Platform = "wechat_mp" | "xhs" | "douyin";
/** `suspended` = 人工停用（P10）：既不出稿也不发布，但不是故障。 */
export type AccountStatus =
  "ok" | "degraded" | "needs_relogin" | "banned" | "suspended";
export type PublishPhase = "in_flight" | "done" | "failed";
export type RenderState = "pending" | "running" | "done" | "failed" | "lost";
export type CheckStatus = "OK" | "WARN" | "FAIL" | "SKIP";

// ------------------------------------------------------------------ envelope

export interface ApiErrorBody {
  code: string;
  message: string;
  detail?: Record<string, unknown> | null;
}

export interface Envelope<T> {
  ok: boolean;
  data: T | null;
  error: ApiErrorBody | null;
}

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

// ------------------------------------------------------------------ 内容行

export interface MediaSummary {
  total: number;
  images: number;
  videos: number;
  kinds: string[];
  cover_index: number | null;
}

export interface MachineReview {
  at: string | null;
  passed: boolean;
  blocking: number;
  warnings: number;
  stages_run: string[];
  stages_skipped: Record<string, string>;
  suggested_edits: Record<string, string>;
  notes: string[];
}

export interface ContentRow {
  id: string;
  account_id: string;
  account_name: string;
  platform: Platform;
  title: string;
  status: string;
  created_at: string | null;
  updated_at: string | null;
  scheduled_at: string | null;
  slot_text: string;
  published_at: string | null;
  platform_post_id: string | null;
  url: string | null;
  publish_phase: PublishPhase | null;
  attempts: number;
  last_error: string | null;
  needs_watch: boolean;
  cover_url: string | null;
  media: MediaSummary;
  tags: string[];
  review_notes: string | null;
  machine_review: MachineReview | null;
  timeline_at: string | null;
  /** 这个账号发布前要不要人点一下（P12） */
  confirm_required: boolean;
  /** 现在正卡在「等你确认」上 */
  awaiting_confirm: boolean;
  confirmed_at: string | null;
  confirm_pushed_at: string | null;
  /**
   * **决定期限**：到这个时刻还没人点就自动驳回，槽位让出来。
   * 和 `scheduled_at` 是两个不同的时刻——一个固定不动，一个一直在缩短。
   * 工作台上那处双时刻读数要的就是这两个数。
   */
  confirm_deadline: string | null;
}

export interface BundleMedia {
  index: number;
  path: string;
  kind: string;
  cover: boolean;
  exists: boolean;
  /** 出处：imagegen = 生图模型画的照片，render = 本地 HTML 模板截的图（P11） */
  source?: "imagegen" | "render";
}

export interface BundleView {
  platform: Platform;
  title: string;
  body_markdown: string;
  body_html: string | null;
  tags: string[];
  media: BundleMedia[];
  images: BundleMedia[];
  videos: BundleMedia[];
  cover: BundleMedia | null;
  digest: string;
  author: string;
  schedule_at: string | null;
  is_original: boolean | null;
  duration_s: number | null;
  hook: string;
  script: string;
  render: Record<string, unknown>;
}

export interface ReviewLogRow {
  id: string;
  actor: string;
  action: string;
  reason: string;
  at: string;
  is_human: boolean;
  has_diff: boolean;
}

export interface SlotView {
  scheduled_at: string | null;
  slot_text: string;
  account_windows: string;
}

export interface ReviewDetail {
  item: ContentRow;
  bundle: BundleView;
  platform_extra: Record<string, unknown>;
  machine_review: MachineReview | null;
  logs: ReviewLogRow[];
  slot: SlotView;
  diff: string;
  media_url_template: string;
}

export interface ContentDetail {
  item: ContentRow;
  bundle: BundleView;
  platform_extra: Record<string, unknown>;
  logs: ReviewLogRow[];
  account_windows: string;
}

export interface WriteResult {
  item: ContentRow;
  message: string;
  scheduled?: boolean;
  scheduled_at?: string | null;
  slot_text?: string;
}

export interface RescheduleResult {
  item: ContentRow;
  scheduled_at: string | null;
  slot_text: string;
  message: string;
}

/** `GET /content/{id}/slots` 的一枚候选（P19.1，`core/scheduling.py:available_slots` 算出来的真值）。 */
export interface AvailableSlot {
  at: string;
  slot_text: string;
  window: string;
}

/** `GET /content/{id}/slots` 的响应体。`slots` 可能为空——账号被封/停用或 14 天内排不进去，理由在 `note`。 */
export interface AvailableSlotsResult {
  item_id: string;
  account_id: string;
  timezone: string;
  slots: AvailableSlot[];
  note: string;
}

export interface RetryResult {
  item: ContentRow;
  mode: string;
  message: string;
  new_item_id: string | null;
}

// ------------------------------------------------------------------ dashboard

export interface BudgetLine {
  used: number;
  limit: number;
  remaining: number;
}

export interface Budget {
  tokens: BudgetLine;
  render_seconds: BudgetLine;
}

export interface PlatformRollup {
  platform: Platform;
  accounts: number;
  ok: number;
  degraded: number;
  needs_relogin: number;
  banned: number;
  suspended: number;
  pending_review: number;
  scheduled: number;
  published: number;
  used_today: number;
  daily_limit: number;
}

export interface AttentionRow {
  account_id: string;
  name: string;
  platform: Platform;
  status: AccountStatus;
  suspended: number;
}

export interface EventRow {
  kind: "review_log" | "publish";
  at: string;
  actor: string;
  action: string;
  item_id: string;
  title: string;
  account_id: string;
  detail: string;
  url: string | null;
}

export interface Dashboard {
  generated_at: string;
  window_days: number;
  counters: {
    pending_review: number;
    published_today: number;
    published_7d: number;
    failed: number;
    dead_letter: number;
    scheduled: number;
    suspended: number;
    /** 已排期但还等着人点「确认发布」的条数（P12） */
    awaiting_confirm: number;
    rendering: number;
    accounts_needing_relogin: number;
    /** sidecar / 上传器连不上（去看那台机器），和"要扫码"是两回事。 */
    accounts_degraded: number;
    /** 人工停用。不是故障，别算进"需要你处理"。 */
    accounts_suspended: number;
  };
  budget: Budget;
  platforms: PlatformRollup[];
  attention: AttentionRow[];
  events: EventRow[];
}

// ------------------------------------------------------------------ 账号

export interface AccountPolicy {
  daily_limit: number;
  daily_target: number;
  publish_windows: string;
  timezone: string;
  min_interval_minutes: number;
  has_persona: boolean;
  /** 机审干净的稿子自动批准并排期（P12）。默认关 */
  autopilot: boolean;
  /** 发布前要不要人点一下（P12）。默认开，且没有旁路 */
  confirm_required: boolean;
  confirm_ttl_hours: number;
}

export interface AccountRow {
  id: string;
  name: string;
  platform: Platform;
  status: AccountStatus;
  needs_attention: boolean;
  policy: AccountPolicy;
  used_today: number;
  quota_left: number;
  last_published_at: string | null;
  sidecar_endpoint: string | null;
  supports_login: boolean;
  insights_updated_at: string | null;
  insights_error: string;
  created_at: string | null;
  updated_at: string | null;
  // 详情端点额外带的
  pending_review?: number;
  scheduled?: number;
  suspended?: number;
  dead_letter?: number;
  extra?: Record<string, unknown>;
}

/** `GET /system/telegram`。**绝不含 token**，脱敏过的也不含。 */
export interface TelegramChannel {
  enabled: boolean;
  configured: boolean;
  ready: boolean;
  chat_configured: boolean;
  can_sign: boolean;
  polling: boolean;
  username: string;
  sent: number;
  failed: number;
  stats: Record<string, number>;
  detail: string;
  last_error: string;
}

/** `POST /content/{id}/confirm` 与 `/reject` 的返回。 */
export interface ConfirmResult {
  item: ContentRow;
  message: string;
}

/** `POST /accounts` 的入参。id 由服务端生成，前端不许指定。 */
export interface AccountCreate {
  platform: Platform;
  name: string;
  identity_hint?: string;
  publish_windows?: string[];
  min_interval_minutes?: number;
  daily_limit?: number;
  daily_target?: number;
  timezone?: string;
  persona?: string;
  autopilot?: boolean;
  confirm_required?: boolean;
}

/** `PATCH /accounts/{id}`。platform 与 id 不可改。 */
export type AccountPatch = Partial<Omit<AccountCreate, "platform">>;

/** 账号写操作的统一返回。 */
export interface AccountWriteResult {
  account: AccountRow;
  message: string;
  /** 非致命问题（sidecar 未接入 / 没起来）。有就一条条显示，别吞。 */
  warnings: string[];
}

export type SidecarState =
  "running" | "stopped" | "absent" | "none-driver" | "error";

export interface Sidecar {
  account_id: string;
  driver: "docker" | "none";
  state: SidecarState;
  detail: string;
  container: string;
  volume: string;
  image: string;
  port: number | null;
  endpoint: string;
  health: Record<string, unknown> | null;
  healthy: boolean;
  health_detail: string;
  checked_at: string;
}

export interface SidecarActionResult {
  sidecar: Sidecar;
  message: string;
}

/** 手动出稿的结果。`llm === "scripted"` 表示没配模型凭据，内容是预置文案。 */
export interface GenerateResult {
  account_id: string;
  content_item_id: string | null;
  status: string;
  title: string;
  llm: string;
  selected_topic: string | null;
  tokens_used: number;
  elapsed_s: number;
  review_passed: boolean | null;
  review_blocking: number;
  /** 实际配了几张生图。比请求的少（甚至 0）是正常降级，原因在 warnings 里 */
  illustrations: number;
  warnings: string[];
  used_today: number;
  cap: number;
  message: string;
}

/** `POST /accounts/{id}/generate` 的请求体。两个字段都可省。 */
export interface GenerateRequest {
  /** 手工指定选题标题；留空则跑选题 Agent */
  topic?: string;
  /** 配几张生图；留空取服务端默认（SW_GENERATE_ILLUSTRATIONS），0 = 不配图 */
  illustrations?: number;
}

/** 生图可用性 + 今日用量（`GET /system/imagegen`）。 */
export interface ImagegenInfo {
  /** false 时配图开关必须禁用，并把 `reason` 原样显示出来 */
  ready: boolean;
  enabled: string;
  model: string;
  base_url: string;
  has_api_key: boolean;
  /** 不可用的人话原因；ready 时为空 */
  reason: string;
  /** 可执行的修复指引 */
  hint: string;
  used_today: number;
  daily_limit: number;
  remaining: number;
  default_count: number;
}

export interface QrCode {
  account_id: string;
  platform: Platform;
  image_base64: string;
  status: string;
  detail: string;
  account_status: AccountStatus;
  placeholder: boolean;
  expires_in: number;
  fetched_at: string;
}

export interface LoginStatus {
  account_id: string;
  platform: Platform;
  status: string;
  detail: string;
  account_status: AccountStatus;
  logged_in: boolean;
  checked_at: string;
}

export interface LoginStart {
  ok: boolean;
  account_id: string;
  platform: Platform;
  state: string;
  detail: string;
  started_at: string;
}

export interface CodeResult {
  ok: boolean;
  account_id: string;
  pending: number;
  forwarded: boolean;
  forward_detail: string;
}

// ------------------------------------------------------------------ 选题 / 任务

export interface TopicRow {
  id: string;
  source: string;
  title: string;
  url: string | null;
  score: number;
  created_at: string | null;
  used: boolean;
  dismissed: boolean;
  dismissed_at: string | null;
  dismissed_by: string;
  raw: Record<string, unknown>;
}

export interface RenderJobRow {
  id: string;
  content_item_id: string;
  title: string;
  provider: string;
  task_id: string | null;
  state: RenderState;
  progress: number;
  attempts: number;
  result_paths: string[];
  last_error: string | null;
  meta: Record<string, unknown>;
  created_at: string | null;
  updated_at: string | null;
}

export interface PublishRecordRow {
  id: string;
  content_item_id: string;
  account_id: string;
  platform: Platform;
  title: string;
  idem_key: string;
  phase: PublishPhase;
  platform_post_id: string | null;
  url: string | null;
  attempts: number;
  last_error: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface DeadLetterRow {
  item_id: string;
  account_id: string;
  title: string;
  at: string | null;
  reason: string;
}

// ------------------------------------------------------------------ 统计

export interface DailyPoint {
  day: string;
  published: number;
  dead_letter: number;
  cost: Record<string, number>;
}

export interface StatsAccount {
  id: string;
  platform: Platform;
  status: AccountStatus;
  daily_limit: number;
  daily_target: number;
  publish_windows: string;
  min_interval_minutes: number;
  published: number;
  failed: number;
  dead_letter: number;
  pending_review: number;
  scheduled: number;
  suspended: number;
  used_today: number;
  last_published_at: string | null;
  metrics: Record<string, number | null>;
  measured_posts: number;
  snapshots_24h: number;
  snapshots_7d: number;
  cost: Record<string, number>;
  insights_at: string;
}

export interface Stats {
  window_days: number;
  day: string;
  generated_at: string;
  totals: Record<string, number>;
  budget: Budget;
  accounts: StatsAccount[];
  dead_letters: unknown[];
  needs_attention: string[];
  unattributed_cost: Record<string, number>;
  content_counts: Record<string, number>;
  publish_counts: Record<string, number>;
  daily: DailyPoint[];
}

export interface Costs {
  days: number;
  since_day: string;
  today: string;
  budget: Budget;
  by_day: DailyPoint[];
  by_account: {
    account_id: string;
    name: string;
    platform: Platform;
    cost: Record<string, number>;
  }[];
  unattributed: Record<string, number>;
  totals: Record<string, number>;
}

/** 成本页表格的行类型。从 `Costs` 上派生，不另抄一份字段清单。 */
export type CostsAccount = Costs["by_account"][number];

// ------------------------------------------------------------------ 复盘 / 系统

export interface InsightEntry {
  account_id: string;
  date: string;
  title: string;
  headline: string;
  markdown: string;
}

export interface InsightsRow {
  account_id: string;
  name: string;
  platform: Platform;
  updated_at: string | null;
  error: string;
  path: string;
  exists: boolean;
  entries: InsightEntry[];
}

export interface TickResult {
  tick: string;
  stats: Record<string, unknown>;
  elapsed_s: number;
  message?: string;
}

export interface SystemInfo {
  version: string;
  env: string;
  time: string;
  timezone: string;
  llm_backend: string;
  llm_model: string;
  database: string;
  scheduler_enabled: boolean;
  use_fake_publishers: boolean;
  generate_enabled: boolean;
  publishers: string[];
  ticks: string[];
  platforms: Platform[];
  content_statuses: string[];
  review_queue_statuses: string[];
  auth_required: boolean;
  budget: Record<string, number>;
}

export interface TickSpec {
  name: string;
  accepts: string[];
}

export interface TicksInfo {
  ticks: TickSpec[];
  note: string;
}

export interface PreflightCheck {
  name: string;
  status: CheckStatus;
  detail: string;
}

export interface Preflight {
  offline: boolean;
  passed: boolean;
  counts: Partial<Record<CheckStatus, number>>;
  checks: PreflightCheck[];
  ran_at: string;
}

export interface AuthProbe {
  ok: boolean;
  auth_required: boolean;
  message: string;
}
