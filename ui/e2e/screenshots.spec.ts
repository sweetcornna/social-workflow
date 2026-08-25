import { expect, test, type Page } from "@playwright/test";

import { AUTH_ORIGIN, EMPTY_ORIGIN } from "../playwright.config";
import { E2E_TIME_ANCHOR } from "./time";

/**
 * 全站走查 + 截图。
 *
 * 五页新信息架构（今日 / 审核台 / 排期 / 账号 / 系统），每页 **日 / 夜 × 工作态 / 空态**
 * 四张，外加登录页与几张关键交互态，全部落在 `ui/screenshots/`（入库供审计对照）。
 *
 * 工作态跑在造过数的 8123，空态跑在**永不造数**的 8125——零 mock 的前提下，
 * 空态是要单独验收的：没有数据时界面也必须诚实（不填假条目）且仍然好看。
 *
 * 主题靠 `?theme=light|dark`——根布局的内联启动脚本认这个参数，并顺手持久化，
 * 所以截图不依赖点击那颗 pill，也就不会拍到过渡态。
 */

const PAGES: { slug: string; path: string; ready: string }[] = [
  { slug: "01-today", path: "/workbench/", ready: '[data-testid="page-header"]' },
  { slug: "02-review", path: "/workbench/review/", ready: '[data-testid="queue-rail"]' },
  { slug: "03-schedule", path: "/workbench/schedule/", ready: '[data-testid="page-header"]' },
  { slug: "04-accounts", path: "/workbench/accounts/", ready: '[data-testid="page-header"]' },
  { slug: "05-system", path: "/workbench/system/", ready: '[data-testid="page-header"]' },
];

// 必须早于每条用例里的 page.goto：首屏的 new Date()、Date.now() 和定时器从这里起同锚。
test.beforeEach(async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.clock.install({ time: new Date(E2E_TIME_ANCHOR) });
});

async function settle(page: Page) {
  // 数据是轮询来的，networkidle 永远等不到；等骨架消失更靠谱
  await page.waitForFunction(() => document.querySelectorAll(".sw-shimmer").length === 0, null, {
    timeout: 20_000,
  });
  // 模态框首次出现的英文字形会按需加载；必须等字体完成排版，不能采样首帧栅格。
  await page.evaluate(async () => {
    await document.fonts.ready;
    await new Promise(requestAnimationFrame);
    await new Promise(requestAnimationFrame);
  });
  await page.waitForTimeout(700);
}

// 全局 reducedMotion 先关闭纯装饰动效；截图时再冻结浏览器原生控件的动效并隐藏光标。
const FROZEN = { animations: "disabled", caret: "hide" } as const;

for (const theme of ["light", "dark"] as const) {
  for (const state of ["work", "empty"] as const) {
    const origin = state === "empty" ? EMPTY_ORIGIN : "";
    for (const p of PAGES) {
      test(`截图 ${p.slug} · ${state} · ${theme}`, async ({ page }) => {
        await page.goto(`${origin}${p.path}?theme=${theme}`);
        await expect(page.locator(p.ready).first()).toBeVisible();
        await settle(page);
        await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
        await page.screenshot({ path: `screenshots/${p.slug}-${state}-${theme}.png`, ...FROZEN });
      });
    }
  }
}

test("截图 05b-system-jobs · 两个主题", async ({ page }) => {
  for (const theme of ["light", "dark"] as const) {
    await page.goto(`/workbench/system/?tab=jobs&theme=${theme}`);
    // 用 heading 而不是裸文本：fake 发布器的提示条里也有"发布记录"四个字，
    // getByText 会同时命中两处，strict mode 直接报错
    await expect(page.getByRole("heading", { name: "发布记录" })).toBeVisible();
    await settle(page);
    await page.screenshot({ path: `screenshots/05b-system-jobs-${theme}.png`, ...FROZEN });
  }
});

test("截图 05c-system-preflight · 两个主题", async ({ page }) => {
  // 门禁自检里的 `docker` 一项即使 --offline 也会真跑 `docker info` 探当前主机
  // （scripts/preflight.py::check_docker，唯一不受 offline 与 e2e.env 摆布的一项：
  // 它不读任何环境变量，结果取决于这台机器有没有装 docker / daemon 有没有在跑，
  // 同一台机器不同时刻都可能翻面，e2e.env 钉不住这种"机器事实"）。它一 OK/WARN
  // 翻面，唯一会变的像素就是这四枚汇总徽标的计数——逐条检查详情列表本身超出
  // `max-h-[420px]` 的内部滚动视口，从不会被截进图里。用 mask 盖掉这四个数字，
  // 而不是假装这台机器的 docker 状态是确定的。
  const countBadges = page.getByText(/^(OK|WARN|FAIL|SKIP) \d+$/);
  for (const theme of ["light", "dark"] as const) {
    await page.goto(`/workbench/system/?tab=runtime&theme=${theme}`);
    await page.getByRole("button", { name: "跑一次离线自检" }).click();
    // docker 探测最多 15 秒，这里放宽等待
    await expect(page.getByText(/OK \d+/)).toBeVisible({ timeout: 60_000 });
    await settle(page);
    // 外壳锁死了视口高，滚动发生在内容区那个滚动口里——window.scrollTo 对它无效，
    // 拍出来会是一张滚到半路的图（P13 改版后实锤）
    await page.evaluate(() => {
      document.querySelector("main.overflow-y-auto")?.scrollTo(0, 0);
      window.scrollTo(0, 0);
    });
    await page.waitForTimeout(200);
    await page.screenshot({
      path: `screenshots/05c-system-preflight-${theme}.png`,
      ...FROZEN,
      mask: [countBadges],
    });
  }
});

test("截图 06-login · 两个主题", async ({ page }) => {
  for (const theme of ["light", "dark"] as const) {
    // 未开鉴权的实例会把登录页直接放行回首页，所以登录页必须去 8124 那台截
    await page.goto(`${AUTH_ORIGIN}/workbench/login/?theme=${theme}`);
    await expect(page.getByText("先验明")).toBeVisible();
    await settle(page);
    await page.screenshot({ path: `screenshots/06-login-${theme}.png`, ...FROZEN });
  }
});

test("截图 07-review-video · 成片舞台与看完解锁", async ({ page }) => {
  await page.goto("/workbench/review/?theme=dark");
  const videoRow = page.locator('[data-testid="queue-row"]', { hasText: "需看完" }).first();
  await expect(videoRow).toBeVisible();
  await videoRow.click();
  await expect(page.getByTestId("watch-badge")).toContainText("看完才能批准");
  // 不能截浏览器原生视频控件的加载转圈；等样本片已可播后再存档。
  await page.getByTestId("video-0").evaluate(async (element) => {
    const video = element as HTMLVideoElement;
    video.muted = true;
    await video.play();
    await new Promise(requestAnimationFrame);
    video.pause();
    if (video.currentTime > 0) {
      const seeked = new Promise<void>((resolve) =>
        video.addEventListener("seeked", () => resolve(), { once: true }),
      );
      video.currentTime = 0;
      await Promise.race([
        seeked,
        new Promise<never>((_, reject) =>
          window.setTimeout(() => reject(new Error("样本片回到首帧超时")), 5_000),
        ),
      ]);
    }
  });
  await settle(page);
  await page.screenshot({ path: "screenshots/07-review-video-dark.png", ...FROZEN });
});

test("命令面板 ⌘K 能开、能搜、能跳", async ({ page }) => {
  await page.goto("/workbench/?theme=light");
  // 等水合完成（侧栏的 ⌘K 菜单项出来了，keydown 监听就挂上了）
  await expect(page.getByRole("button", { name: "打开命令面板" })).toBeVisible();
  // 先等背后那一页把数据读完：截图里露出一屏骨架，看着像面板把页面弄坏了
  await settle(page);
  await page.keyboard.press("ControlOrMeta+k");
  const palette = page.getByRole("dialog", { name: "命令面板" });
  await expect(palette).toBeVisible();

  await page.getByPlaceholder(/跳转页面/).fill("审核");
  await expect(palette.getByText("审核台").first()).toBeVisible();
  // 浮层有 160ms 入场动画，立刻截会拍到半透明的过渡态
  await page.waitForTimeout(300);
  await page.screenshot({ path: "screenshots/08-command-palette-light.png", ...FROZEN });

  await palette.getByText("审核台").first().click();
  await expect(page).toHaveURL(/\/workbench\/review\//);
});

test("旧路径仍然可用：/content 会把人送到 /schedule", async ({ page }) => {
  await page.goto("/workbench/content/?theme=light");
  await expect(page).toHaveURL(/\/workbench\/schedule\//);
  await expect(page.getByText("几点发什么")).toBeVisible();
});

test("截图 15-account-detail · 接入面板（sidecar + 二维码）两个主题", async ({ page }) => {
  for (const theme of ["light", "dark"] as const) {
    await page.goto(`/workbench/accounts/?id=xhs-demo-01&theme=${theme}`);
    // sidecar 面板 + 扫码位都要成形。扫码位有两种真相：还没登录时是二维码大图，
    // 已登录（e2e 用 FakePublisher，巡检回 ok）时是"扫上了"——两种都算，
    // 但**必须是其中一种**，不能两个都不渲染
    await expect(page.getByTestId("sidecar-panel")).toBeVisible();
    await expect(
      page.getByTestId("qr-stage").or(page.getByTestId("qr-logged-in")),
    ).toBeVisible();
    await settle(page);
    await page.screenshot({ path: `screenshots/15-account-detail-${theme}.png`, ...FROZEN });
  }
});

test("截图 16-account-wizard · 选平台那一步（暗色）", async ({ page }) => {
  await page.goto("/workbench/accounts/?theme=dark");
  await page.getByTestId("add-account").click();
  await expect(page.getByTestId("pick-platform-xhs")).toBeVisible();
  await settle(page);
  await page.screenshot({ path: "screenshots/16-account-wizard-platform-dark.png", ...FROZEN });
});

test("截图 17-account-guides · 抖音与公众号的接入指引", async ({ page }) => {
  for (const [slug, id] of [
    ["douyin", "douyin-demo-01"],
    ["wechat", "wechat-demo-01"],
  ] as const) {
    await page.goto(`/workbench/accounts/?id=${id}&theme=light`);
    await expect(page.getByTestId("onboarding-guide")).toBeVisible();
    await settle(page);
    await page.screenshot({ path: `screenshots/17-account-guide-${slug}-light.png`, ...FROZEN });
  }
});

/**
 * 手机宽度存档（P13 起两张，P14.B6 补齐到五页）。
 *
 * 顶栏删掉之后，小屏上的换页入口是那条横向导航轨；列表页在 390px 下靠
 * 卡片式行而不是表格横滚。这几张截图入库，是为了让"手机上不塌"这条验收
 * 有个可对照的物证，而不是只有一句断言。B6 走查要求 ≤420px 五页全过，
 * 所以五页各留一张——只有两张时，审核台与账号页的窄屏回归没人看得见。
 */
test("截图 18-mobile · 五页（390px）", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 780 });
  for (const [slug, path, ready] of [
    ["today", "/workbench/", '[data-testid="page-header"]'],
    ["review", "/workbench/review/", '[data-testid="queue-rail"]'],
    ["schedule", "/workbench/schedule/", '[data-testid="page-header"]'],
    ["accounts", "/workbench/accounts/", '[data-testid="page-header"]'],
    ["system", "/workbench/system/", '[data-testid="page-header"]'],
  ] as const) {
    await page.goto(`${path}?theme=light`);
    await expect(page.locator(ready).first()).toBeVisible();
    await settle(page);
    // 窄屏最容易塌的就是横向：任何一页出现横滚都算不合格
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow, `${slug} 在 390px 下不该横向溢出`).toBeLessThanOrEqual(1);
    await page.screenshot({ path: `screenshots/18-mobile-${slug}-light.png`, ...FROZEN });
  }
});

/* ─────────────────── P14.B4 新增交互态的存档（B6 补拍） ───────────────────
 *
 * B4 把三处高频操作简化了，但简化的证据一张都没入库。下面三张是"简化后
 * 长什么样"的物证，交付报告里的步数对比要靠它们才不是空口。
 */

test("截图 19-wizard-advanced · 向导第二步的折叠高级项", async ({ page }) => {
  for (const theme of ["light", "dark"] as const) {
    await page.goto(`/workbench/accounts/?theme=${theme}`);
    await page.getByTestId("add-account").click();
    await page.getByTestId("pick-platform-xhs").click();
    // 折叠头默认收着，摘要行里写着当前默认值——这正是"可见字段 8→3"的落点
    const advanced = page.getByTestId("account-advanced");
    await expect(advanced).toBeVisible();
    await expect(advanced).not.toHaveAttribute("open", /.*/);
    await settle(page);
    await page.screenshot({
      path: `screenshots/19-wizard-advanced-collapsed-${theme}.png`,
      ...FROZEN,
    });
  }
});

test("截图 20-reschedule-quick-slots · 改期的快捷槽位药丸", async ({
  page,
  request,
}) => {
  // 挑一条**账号带发布窗口**的：快捷槽位是从 publish_windows 算出来的，
  // 全天窗口的号算不出"今晚/明天"这两枚药丸
  const accounts: { id: string; policy: { publish_windows: string } }[] = (
    await (await request.get("/api/v1/accounts")).json()
  ).data;
  const windowed = new Set(
    accounts
      .filter((a) => a.policy.publish_windows && a.policy.publish_windows !== "全天")
      .map((a) => a.id),
  );
  const rows: { id: string; status: string; account_id: string }[] = (
    await (await request.get("/api/v1/content?limit=200")).json()
  ).data.items;
  const target = rows.find(
    (r) =>
      ["approved", "scheduled", "suspended"].includes(r.status) &&
      windowed.has(r.account_id),
  );
  expect(target, "seed 应当留下一条带窗口账号的可改期内容").toBeTruthy();

  await page.goto("/workbench/schedule/?theme=light");
  await page.getByRole("tab", { name: "全部" }).last().click();
  const row = page.locator(`[data-item-id="${target!.id}"]`);
  await expect(row).toBeVisible();
  await row.getByTestId("reschedule-button").click();
  await expect(page.getByTestId("reschedule-quick-slots")).toBeVisible();
  await page.waitForTimeout(300); // 浮层入场动画
  await page.screenshot({
    path: "screenshots/20-reschedule-quick-slots-light.png",
    ...FROZEN,
  });
});

test("截图 21-reject-reason-presets · 常用驳回理由药丸", async ({ page }) => {
  await page.goto("/workbench/review/?theme=light");
  await expect(page.locator('[data-testid="queue-row"]').first()).toBeVisible();
  await settle(page);
  // r 键聚焦理由框，药丸就在它上面一行
  await page.keyboard.press("r");
  const presets = page.getByTestId("reject-reason-presets");
  await expect(presets).toBeVisible();
  // 点两枚，把"续写不覆盖、用「；」拼接"这条规则也拍进去
  await page.getByTestId("reject-reason-preset-事实存疑").click();
  await page.getByTestId("reject-reason-preset-标题夸张").click();
  await expect(page.getByTestId("reject-reason")).toHaveValue(/事实存疑；标题夸张/);
  await page.screenshot({
    path: "screenshots/21-reject-reason-presets-light.png",
    ...FROZEN,
  });
});
