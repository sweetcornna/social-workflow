/**
 * 走查的唯一时间锚点。serve.sh 把同一个值交给三台 core，避免前后端各算各的“现在”。
 *
 * 验收跨日期鲁棒性时可通过 `SW_E2E_TIME_ANCHOR=... pnpm exec playwright test` 临时覆盖。
 */
export const E2E_TIME_ANCHOR =
  process.env.SW_E2E_TIME_ANCHOR ?? "2026-08-19T11:00:00.000Z";

export const E2E_TIME_MS = Date.parse(E2E_TIME_ANCHOR);

if (Number.isNaN(E2E_TIME_MS)) {
  throw new Error(`SW_E2E_TIME_ANCHOR 不是合法 ISO 时间：${E2E_TIME_ANCHOR}`);
}
