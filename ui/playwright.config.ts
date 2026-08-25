import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright 走的是**最终部署形态**：真实 FastAPI + `ui/out` 静态产物挂在
 * `/workbench`，不是 `next dev`，也没有任何 mock。
 *
 * 跑之前先构建一次前端：`bash scripts/build_ui.sh`。
 *
 * 三台 core：
 *  - 8123 不鉴权、造过数，跑关键流程与「工作态」截图
 *  - 8124 开 `SW_UI_TOKEN`，只用来验登录门
 *  - 8125 不鉴权、**永远不造数**，专供「空态」截图。空态是交付物的一半：
 *    零 mock 的前提下，界面在没有数据时也必须诚实且好看
 */
const BASE = "http://127.0.0.1:8123";
const AUTH_BASE = "http://127.0.0.1:8124";
const EMPTY_BASE = "http://127.0.0.1:8125";
export const E2E_TOKEN = "playwright-token-abc123456789";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: [["list"]],
  use: {
    baseURL: BASE,
    viewport: { width: 1440, height: 960 },
    locale: "zh-CN",
    /**
     * **浏览器时区钉死在 America/Los_Angeles，绝不能是 Asia/Shanghai。**
     *
     * 台账里的号全是 Asia/Shanghai。浏览器跟着一起设成 Asia/Shanghai，就等于让
     * "浏览器本地时区"和"账号时区"永远重合——P11 那个把账号时区 19:00 画到 04:00 的
     * 缺陷，正是这样带着 41 条 e2e 全绿溜进生产的。固定成一个**差 15/16 小时、
     * 偏移为负、且有夏令时**的时区，这类口径错位才必然暴露。
     *
     * 时区仍然是固定值（不是"跟着机器走"），截图与断言照样可复现。
     */
    timezoneId: "America/Los_Angeles",
    trace: "off",
    screenshot: "off",
  },
  projects: [
    { name: "seed", testMatch: /seed\.setup\.ts/ },
    {
      name: "chromium",
      dependencies: ["seed"],
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1440, height: 960 },
        launchOptions: {
          args: [
            "--force-color-profile=srgb",
            "--disable-partial-raster",
            "--disable-skia-runtime-opts",
            "--run-all-compositor-stages-before-draw",
            "--disable-lcd-text",
          ],
        },
      },
      testMatch: /.*\.spec\.ts/,
    },
  ],
  webServer: [
    {
      command: "bash e2e/serve.sh 8123",
      url: `${BASE}/health`,
      reuseExistingServer: false,
      timeout: 120_000,
      stdout: "pipe",
      stderr: "pipe",
    },
    {
      command: `bash e2e/serve.sh 8124 ${E2E_TOKEN}`,
      url: `${AUTH_BASE}/health`,
      reuseExistingServer: false,
      timeout: 120_000,
      stdout: "pipe",
      stderr: "pipe",
    },
    {
      command: "bash e2e/serve.sh 8125",
      url: `${EMPTY_BASE}/health`,
      reuseExistingServer: false,
      timeout: 120_000,
      stdout: "pipe",
      stderr: "pipe",
    },
  ],
});

export const AUTH_ORIGIN = AUTH_BASE;
export const EMPTY_ORIGIN = EMPTY_BASE;
