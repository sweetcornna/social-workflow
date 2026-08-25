import { expect, test, type Page } from "@playwright/test";

import { AUTH_ORIGIN, E2E_TOKEN } from "../playwright.config";
import { E2E_TIME_ANCHOR, E2E_TIME_MS } from "./time";

/**
 * 关键流程 e2e。跑在真实 core 上，写操作真的会改库。
 * 全部是硬断言——找不到造好的数据说明造数或链路真的回归了，必须让它红，不许 skip。
 *
 * **浏览器时区固定为 America/Los_Angeles**（playwright.config.ts 的 `use.timezoneId`），
 * 而台账里的号都是 Asia/Shanghai——两者差 15/16 小时。这是刻意的：P11 那个
 * "账号时区 19:00 的稿画在 04:00、点落到窗口底色外面"的缺陷，正因为浏览器与账号同区
 * 而带着 41 条 e2e 全绿溜进了生产。时区不同，这类口径错位才必然暴露。
 */

test.beforeEach(async ({ page }) => {
  const anchor = new Date(E2E_TIME_ANCHOR);
  await page.clock.install({ time: anchor });
  // flows 会把真实生成耗时渲染成「等待 N 秒」；固定 Date 的同时保留真实 timers，
  // 让轮询/动效照常运行，但页面所有“现在”始终与 core 的锚点完全一致。
  await page.clock.setFixedTime(anchor);
});

/** N 天后的那一瞬间。 */
function daysFromNow(n: number): Date {
  return new Date(E2E_TIME_MS + n * 86_400_000);
}

/** 某个瞬间在某时区的日期 `YYYY-MM-DD`。用来拼账号时区的墙上时间。 */
function wallDayIn(at: Date, tz: string): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: tz,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(at);
  const bag: Record<string, string> = {};
  for (const p of parts) if (p.type !== "literal") bag[p.type] = p.value;
  return `${bag.year}-${bag.month}-${bag.day}`;
}

/** 截在终态：流程断言照常验证真实数据，画面不采样短暂动效。 */
async function capture(page: Page, path: string): Promise<void> {
  await page.waitForFunction(
    () => document.querySelectorAll(".sw-shimmer").length === 0,
  );
  await page.waitForTimeout(400);
  await page.screenshot({ path, animations: "disabled", caret: "hide" });
}

test("流程页面与 core 共用同一 e2e 时间锚点", async ({ page }) => {
  await page.goto("/workbench/?theme=light");
  expect(await page.evaluate(() => Date.now())).toBe(E2E_TIME_MS);
});

test("审核台：媒体主舞台占屏 ≥55%，三区都在", async ({ page }) => {
  await page.goto("/workbench/review/?theme=light");
  await expect(page.locator('[data-testid="queue-row"]').first()).toBeVisible();

  await expect(page.getByTestId("queue-rail")).toBeVisible();
  await expect(page.getByTestId("decision-panel")).toBeVisible();
  await expect(page.getByTestId("shortcut-hint")).toBeVisible();

  const stage = page.getByTestId("media-stage");
  await expect(stage).toBeVisible();
  const box = await stage.boundingBox();
  const vw = page.viewportSize()!.width;
  expect(box, "媒体舞台应当量得到尺寸").toBeTruthy();
  // 媒体是审核对象的本体，必须大：任务书的硬指标是 ≥55% 屏宽
  expect(box!.width / vw).toBeGreaterThanOrEqual(0.55);
});

test("审核台键盘流：j/k 换条，←/→ 翻卡", async ({ page }) => {
  await page.goto("/workbench/review/?theme=light");
  const rows = page.locator('[data-testid="queue-row"]');
  await expect(rows.first()).toBeVisible();
  const count = await rows.count();
  expect(count, "队列里应该有内容（seed 造过）").toBeGreaterThan(1);

  // 默认落在第一条"还能决策"的上面（不一定是队首——队首可能是已驳回的）
  const selectedIndex = async () => {
    const flags = await rows.evaluateAll((els) =>
      els.map((e) => e.getAttribute("aria-current") === "true"),
    );
    return flags.indexOf(true);
  };
  const start = await selectedIndex();
  expect(start, "默认应当选中某一条").toBeGreaterThanOrEqual(0);
  expect(start, "j 键要有下一条可去").toBeLessThan(count - 1);

  const nextId = await rows.nth(start + 1).getAttribute("data-item-id");
  const startId = await rows.nth(start).getAttribute("data-item-id");
  expect(nextId).toBeTruthy();

  await page.keyboard.press("j");
  await expect(page).toHaveURL(new RegExp(`id=${encodeURIComponent(nextId!)}`));
  expect(await selectedIndex()).toBe(start + 1);

  await page.keyboard.press("k");
  await expect(page).toHaveURL(
    new RegExp(`id=${encodeURIComponent(startId!)}`),
  );
  expect(await selectedIndex()).toBe(start);

  // 找一条多图的（/dev/seed 造的小红书草稿有 2 张），验 ←/→ 翻卡
  const multiCard = page
    .locator('[data-testid="queue-row"]')
    .filter({ hasNotText: "需看完" });
  await multiCard.first().click();
  const counter = page.getByTestId("card-counter");
  if (await counter.isVisible()) {
    await expect(counter).toContainText("1 /");
    await page.keyboard.press("ArrowRight");
    await expect(counter).toContainText("2 /");
    await page.keyboard.press("ArrowLeft");
    await expect(counter).toContainText("1 /");
  }
});

test("视频闸门：界面锁 + 后端 422 → 看完解锁 → a 键批准 → 自动下一条", async ({
  page,
  request,
}) => {
  await page.goto("/workbench/review/?theme=light");
  await expect(page.locator('[data-testid="queue-row"]').first()).toBeVisible();

  // 找那条含视频的抖音草稿（/dev/run_douyin_pipeline 造的）。
  // seed 硬断言过三条抖音链路都跑成了，所以这里也硬断言——没有就是真回归了，
  // 不能 test.skip 掉，否则闸门失效时这条用例会"绿着"什么都没验。
  const videoRow = page
    .locator('[data-testid="queue-row"]', { hasText: "需看完" })
    .first();
  await expect(videoRow, "队列里应该有含视频的草稿（seed 造过）").toBeVisible();
  await videoRow.click();
  const itemId = await videoRow.getAttribute("data-item-id");
  expect(itemId).toBeTruthy();

  // 1) 界面闸门：没看完时批准钮是灰的，且舞台上明说"看完才能批准"
  const approve = page.getByTestId("approve-button");
  await expect(approve).toBeVisible();
  await expect(approve).toBeDisabled();
  await expect(page.getByTestId("watch-badge")).toContainText("看完才能批准");
  await expect(page.getByTestId("watch-blocked-hint")).toBeVisible();

  // 2) 键盘也拦得住：按 a 不会偷偷放行
  await page.keyboard.press("a");
  await expect(page.getByText(/必须先完整观看成片/).first()).toBeVisible();
  await expect(approve).toBeDisabled();

  // 3) 后端闸门：绕过界面直接打 API，必须 422 watch_required
  const denied = await request.post(`/api/v1/review/${itemId}/approve`, {
    data: { actor: "playwright" },
  });
  expect(denied.status()).toBe(422);
  expect((await denied.json()).error.code).toBe("watch_required");

  // 4) 成片播完 → 自动勾上「已完整观看」→ 解锁
  await page
    .locator("video")
    .first()
    .evaluate((v: HTMLVideoElement) => {
      v.dispatchEvent(new Event("ended"));
    });
  await expect(page.getByLabel(/已完整观看成片/)).toBeChecked();
  await expect(page.getByTestId("watch-badge")).toContainText("已看完");
  await expect(approve).toBeEnabled();

  // 5) 按 a 批准 → 排期槽位就地显示 → 自动切下一条
  await page.keyboard.press("a");
  await expect(page.getByText(/已批准/).first()).toBeVisible();
  await expect(page).not.toHaveURL(
    new RegExp(`id=${encodeURIComponent(itemId!)}`),
  );

  const after = await request.get(`/api/v1/review/${itemId}`);
  const status = (await after.json()).data.item.status;
  expect(["approved", "scheduled"]).toContain(status);
});

test("改期弹窗：非法时刻被后端挡回，一键改用建议槽位", async ({
  page,
  request,
}) => {
  // 挑一条**账号有发布窗口**的可改期内容——全天窗口的账号挑什么时刻都合法，
  // 那就验不到 invalid_slot 这条路
  const accounts: { id: string; policy: { publish_windows: string } }[] = (
    await (await request.get("/api/v1/accounts")).json()
  ).data;
  const windowed = new Set(
    accounts
      .filter(
        (a) => a.policy.publish_windows && a.policy.publish_windows !== "全天",
      )
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
  // seed 已经就地批准了一条 douyin-demo-01（窗口 12:00-13:30 / 18:00-22:00）的内容，
  // 所以这里一定找得到。硬断言而非 skip：找不到说明造数或"批准即排期"回归了。
  expect(
    target,
    "seed 应当留下一条「账号带发布窗口」的可改期内容",
  ).toBeTruthy();

  // 时间范围默认近 7 天，已排期的可能落在未来 → 用「全部」把它捞出来
  await page.goto("/workbench/schedule/?theme=light");
  await page.getByRole("tab", { name: "全部" }).last().click();

  const row = page.locator(`[data-item-id="${target!.id}"]`);
  await expect(row).toBeVisible();
  await row.getByTestId("reschedule-button").click();

  const input = page.getByTestId("reschedule-input");
  await expect(input).toBeVisible();

  // 输入框按**账号时区**填（浏览器在 America/Los_Angeles，账号在 Asia/Shanghai）。
  // 凌晨 3:17 不在任何 demo 账号的窗口里，且落在未来（避免撞上「已过去」那条分支）
  const tz = await input.getAttribute("data-zone");
  expect(tz, "输入框要标出它按哪个时区解析").toBeTruthy();
  await input.fill(`${wallDayIn(daysFromNow(2), tz!)}T03:17`);
  await page.getByRole("button", { name: "改到这个时间" }).click();

  const hint = page.getByTestId("slot-hint");
  await expect(hint).toBeVisible();
  await expect(hint).toContainText("未通过校验");
  await capture(page, "screenshots/09-reschedule-slot-hint-light.png");

  const suggest = page.getByTestId("use-suggested-slot");
  await expect(suggest).toBeVisible();
  await suggest.click();
  await expect(page.getByText(/已改期至/).first()).toBeVisible();
  await expect(input).toBeHidden();
});

/** 找一条「账号带发布窗口」的可改期内容——全天窗口的账号挑什么时刻都合法，验不到真值/估算的差别。 */
async function findWindowedReschedulable(
  request: import("@playwright/test").APIRequestContext,
): Promise<{ id: string; status: string; account_id: string }> {
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
  expect(target, "seed 应当留下一条「账号带发布窗口」的可改期内容").toBeTruthy();
  return target!;
}

test("改期弹窗：快捷槽位药丸来自后端真值（P19.2 GET /content/{id}/slots），文本原样一致、点一下提交成功", async ({
  page,
  request,
}) => {
  const target = await findWindowedReschedulable(request);

  await page.goto("/workbench/schedule/?theme=light");
  await page.getByRole("tab", { name: "全部" }).last().click();
  const row = page.locator(`[data-item-id="${target.id}"]`);
  await expect(row).toBeVisible();
  await row.getByTestId("reschedule-button").click();

  const pillsBox = page.getByTestId("reschedule-quick-slots");
  await expect(pillsBox).toBeVisible();
  const pills = pillsBox.getByRole("button");
  await expect(pills.first()).toBeVisible();

  // 弹窗打开时前端也调了同一个端点——这里紧挨着再调一次，取当下的后端真值来核对
  // 药丸文本是不是原样透传（不是前端自己拼的日期），不钉死具体日期/时刻
  const slotsResp = await (
    await request.get(`/api/v1/content/${target.id}/slots`)
  ).json();
  expect(slotsResp.ok, "P19.1 的 /slots 端点应当在线").toBe(true);
  const expectedSlots: { slot_text: string }[] = slotsResp.data.slots;
  expect(
    expectedSlots.length,
    "带发布窗口的活跃账号应当至少能排出一个合法槽位",
  ).toBeGreaterThan(0);

  const pillCount = await pills.count();
  expect(pillCount, "药丸数量应当等于后端返回的候选数").toBe(expectedSlots.length);
  // 药丸的 testid 是 backend-N（P19.2：区别于前端估算的 today/tomorrow）
  await expect(pills.first()).toHaveAttribute("data-testid", "reschedule-quick-backend-0");
  for (let i = 0; i < pillCount; i += 1) {
    await expect(pills.nth(i)).toHaveText(expectedSlots[i].slot_text);
  }

  await pills.first().click();
  await expect(page.getByText(/已改期至/).first()).toBeVisible();
  await expect(page.getByTestId("reschedule-input")).toBeHidden();
});

test("改期弹窗：/slots 端点不可达时静默回落前端估算，不报错、不闪错", async ({
  page,
  request,
}) => {
  const target = await findWindowedReschedulable(request);

  // 模拟后端真值端点挂了——UI 必须静默落回 quickSlots() 这份前端估算，不许冒错
  await page.route(`**/api/v1/content/${target.id}/slots*`, (route) =>
    route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({
        ok: false,
        error: { code: "internal", message: "e2e 模拟：/slots 不可达" },
      }),
    }),
  );

  await page.goto("/workbench/schedule/?theme=light");
  await page.getByRole("tab", { name: "全部" }).last().click();
  const row = page.locator(`[data-item-id="${target.id}"]`);
  await expect(row).toBeVisible();
  await row.getByTestId("reschedule-button").click();

  // 账号带发布窗口，前端估算算得出来——「今天/明天」两枚药丸顶上，不是干哑巴
  await expect(page.getByTestId("reschedule-quick-today")).toBeVisible();
  await expect(page.getByTestId("reschedule-quick-tomorrow")).toBeVisible();
  // 没有因为后端 500 就弹一句错误、也没有崩成 slot-hint 那套校验提示
  await expect(page.getByTestId("slot-hint")).toBeHidden();
  await expect(page.getByText("e2e 模拟：/slots 不可达")).toBeHidden();
  await expect(page.getByTestId("reschedule-input")).toBeVisible();
});

test("时区口径：浏览器在 UTC-7，排期仍按账号时区显示，点落在窗口底色内", async ({
  page,
  request,
}) => {
  // 这条用例守的是 P11 的生产缺陷本身，全程硬断言，一步都不许 skip。

  // 1) 找一条**账号带发布窗口**的可改期内容，账号的窗口与时区都从 API 拿，不写死在用例里。
  //    全天窗口的账号挑什么时刻都合法，验不到"点落在底色内"这件事。
  const accounts: {
    id: string;
    policy: { publish_windows: string; timezone: string };
  }[] = (await (await request.get("/api/v1/accounts")).json()).data;
  const windowed = new Map(
    accounts
      .filter(
        (a) =>
          a.policy.publish_windows &&
          a.policy.publish_windows !== "全天" &&
          a.policy.timezone,
      )
      .map((a) => [a.id, a]),
  );

  const rows: { id: string; status: string; account_id: string }[] = (
    await (await request.get("/api/v1/content?limit=200")).json()
  ).data.items;
  const target = rows.find(
    (r) =>
      ["approved", "scheduled", "suspended"].includes(r.status) &&
      windowed.has(r.account_id),
  );
  // seed 就地批准过一条 douyin-demo-01（窗口 12:00-13:30 / 18:00-22:00）的内容，
  // 所以这里一定找得到。硬断言而非 skip：找不到说明造数或「批准即排期」回归了。
  expect(
    target,
    "seed 应当留下一条「账号带发布窗口」的可改期内容",
  ).toBeTruthy();
  const acc = windowed.get(target!.account_id)!;

  const tz = acc.policy.timezone;
  // 前提校验：浏览器与账号必须**不同区**，否则这条用例什么都验不到（正是当初的漏网原因）
  const browserTz = await page.evaluate(
    () => Intl.DateTimeFormat().resolvedOptions().timeZone,
  );
  expect(browserTz, "playwright 的浏览器时区应当钉在 America/Los_Angeles").toBe(
    "America/Los_Angeles",
  );
  expect(tz, "账号时区必须与浏览器时区不同，用例才有意义").not.toBe(browserTz);

  // 2) 从窗口文案里取一个**窗口内的整点**，作为要排的目标时刻
  const spans = acc.policy.publish_windows
    .split(/[、,，]/)
    .map((s) => s.trim().match(/^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$/))
    .filter(Boolean)
    .map((m) => ({
      startMin: Number(m![1]) * 60 + Number(m![2]),
      endMin: Number(m![3]) * 60 + Number(m![4]),
    }));
  expect(spans.length, "窗口文案应当拆得出区间").toBeGreaterThan(0);
  // 最后一段窗口里的第一个整点：起始那个小时 +1，稳稳落在窗口内部
  const span = spans[spans.length - 1];
  const hour = Math.floor(span.startMin / 60) + 1;
  const targetMin = hour * 60;
  expect(targetMin, "取到的整点应当在窗口内").toBeGreaterThanOrEqual(
    span.startMin,
  );
  expect(targetMin, "取到的整点应当在窗口内").toBeLessThan(span.endMin);
  const hh = String(hour).padStart(2, "0");
  // 排到**账号时区的明天**，避开"已经过去了"那条分支
  const wall = `${wallDayIn(daysFromNow(1), tz)}T${hh}:00`;

  await page.goto("/workbench/schedule/?theme=light");
  await page.getByRole("tab", { name: "全部" }).last().click();

  // 4) 页面上要有一句"这些时刻是谁的钟点"——不同区却不标，运营就会照自己的表读错
  const zoneHint = page.getByTestId("zone-hint");
  await expect(zoneHint).toBeVisible();
  await expect(zoneHint).toContainText(tz);
  await expect(zoneHint).toContainText(browserTz);

  // 5) 写路径：按账号时区填一个窗口内的时刻，必须**改期成功**，不许被 422 invalid_slot 挡回
  const row = page.locator(`[data-item-id="${target!.id}"]`);
  await expect(row).toBeVisible();
  await row.getByTestId("reschedule-button").click();
  const input = page.getByTestId("reschedule-input");
  await expect(input).toBeVisible();
  await expect(input).toHaveAttribute("data-zone", tz);
  // 标签要明说按账号时区填
  await expect(
    page.getByText(new RegExp(`按账号时区（${tz.replace("/", "\\/")}）填`)),
  ).toBeVisible();

  await input.fill(wall);
  await page.getByRole("button", { name: "改到这个时间" }).click();

  // 后端回的是它自己算的人话，必须落在我们填的那个账号时区钟点上
  await expect(page.getByText(/已改期至/).first()).toBeVisible();
  await expect(page.getByTestId("slot-hint")).toBeHidden();

  // 6) 读路径：后端存的 UTC 时刻，换算回账号时区必须还是我们填的那个钟点
  const after = (
    await (await request.get(`/api/v1/content?limit=200`)).json()
  ).data.items.find((r: { id: string }) => r.id === target!.id);
  expect(after.slot_text, "后端应当给出账号时区的人话").toContain(`${hh}:00`);
  expect(after.slot_text).toContain(tz);
  const storedHour = Number(
    new Intl.DateTimeFormat("en-US", {
      timeZone: tz,
      hour: "2-digit",
      hourCycle: "h23",
    })
      .formatToParts(new Date(after.scheduled_at))
      .find((p) => p.type === "hour")!.value,
  );
  expect(
    storedHour * 60,
    "存进库的 UTC 时刻换回账号时区必须是我们填的钟点",
  ).toBe(targetMin);

  // 7) 界面：那一行的时刻显示的是账号时区的钟点，不是浏览器本地的
  await page.reload();
  await page.getByRole("tab", { name: "全部" }).last().click();
  const clock = page
    .locator(`[data-item-id="${target!.id}"] [data-testid="row-clock"]`)
    .first();
  await expect(clock).toBeVisible();
  await expect(clock).toHaveText(`${hh}:00`);
  await expect(clock).toHaveAttribute("data-zone", tz);

  // 8) **点必须落在窗口底色内**——生产上错的就是这一条
  const dot = page
    .locator(
      `[data-testid="band-item"][href*="${encodeURIComponent(target!.id)}"]`,
    )
    .first();
  await expect(dot).toBeVisible();
  await expect(dot).toHaveAttribute("data-zone", tz);
  await expect(dot).toHaveAttribute("data-minutes", String(targetMin));

  // 不只比数字，还比**几何位置**：点的中心要落在某一段底色的横向范围内
  const dotBox = (await dot.boundingBox())!;
  expect(dotBox, "点应当量得到位置").toBeTruthy();
  const dotCenter = dotBox.x + dotBox.width / 2;
  const shades = page.locator(
    `[data-testid="window-span"][data-account="${acc!.id}"]`,
  );
  const shadeCount = await shades.count();
  expect(shadeCount, "该账号的泳道上应当画出了窗口底色").toBeGreaterThan(0);
  let covered = false;
  for (let i = 0; i < shadeCount; i++) {
    const b = (await shades.nth(i).boundingBox())!;
    if (b && dotCenter >= b.x - 1 && dotCenter <= b.x + b.width + 1)
      covered = true;
  }
  expect(covered, "内容点必须落在账号发布窗口的底色范围内").toBe(true);

  await capture(page, "screenshots/12-schedule-account-timezone-light.png");
});

test("token 登录门：未登录被弹到登录页，输对 token 才放行", async ({
  page,
}) => {
  // 这台实例开了 SW_UI_TOKEN，/api/v1/* 全部 401
  await page.goto(`${AUTH_ORIGIN}/workbench/?theme=dark`);
  await expect(page).toHaveURL(/\/workbench\/login\//);
  await expect(page.getByTestId("token-input")).toBeVisible();

  // 错 token → 留在登录页并报错
  await page.getByTestId("token-input").fill("wrong-token");
  await page.getByTestId("token-submit").click();
  // 按 testid 取，别用 getByRole("alert")——Next 的路由播报器
  // （`#__next-route-announcer__`）也是 role=alert，两个一起命中会触发严格模式冲突
  await expect(page.getByTestId("token-error")).toBeVisible();
  await capture(page, "screenshots/10-login-gate-dark.png");

  // 对 token → 进工作台
  await page.getByTestId("token-input").fill(E2E_TOKEN);
  await page.getByTestId("token-submit").click();
  await expect(page).toHaveURL(/\/workbench\/$/);
  await expect(page.getByText("需要你").first()).toBeVisible();
});

test("媒体端点不带 token 也能取，<img>/<video> 才引得动", async ({
  request,
}) => {
  const listed = await request.get("/api/v1/review?limit=50");
  const items: { id: string; media: { videos: number } }[] = (
    await listed.json()
  ).data.items;
  const withVideo = items.find((i) => i.media.videos > 0);
  expect(withVideo, "seed 应当留下带视频的内容").toBeTruthy();

  const res = await request.get(`/review/${withVideo!.id}/media/0`);
  expect(res.status()).toBe(200);
  expect(res.headers()["content-type"]).toContain("video/");
});

/* ───────────────────────── P10：账号全生命周期 ─────────────────────────
 *
 * 这一组走的是任务书要求的那条真链：**添加账号 → 看到它 → 出一条稿 → 到审核台**。
 * 全在真 core 上跑（`SW_SIDECAR_DRIVER=none`、台账是副本），写操作真的会改库与台账。
 */

// e2e core 的台账副本每次都会重建，取景账号名可以固定，让服务端分配的 id 也稳定。
const SCREENSHOT_XHS_ACCOUNT_NAME = "e2e-xhs-screenshot";
const SCREENSHOT_DOUYIN_ACCOUNT_NAME = "e2e-douyin-invalid";

test("添加小红书账号：走完全表单 → 卡片出现 → sidecar 如实显示未接入", async ({
  page,
  request,
}) => {
  const name = SCREENSHOT_XHS_ACCOUNT_NAME;
  await page.goto("/workbench/accounts/?theme=light");
  await page.getByTestId("add-account").click();

  // 第一步：选平台
  const wizard = page.getByRole("dialog");
  await expect(wizard.getByTestId("wizard-steps")).toBeVisible();
  await wizard.getByTestId("pick-platform-xhs").click();

  // 第二步：表单。第二步只露名称 + 发布窗口，日上限/出稿/间隔/时区/人设收进
  // 「高级设置」折叠段（P14.B4）——已经按平台预填好了，偏离默认值才要点开
  await wizard.getByTestId("account-name").fill(name);
  await expect(wizard.getByTestId("window-editor")).toBeVisible();
  // 预设药丸是主路径；这里要精确改一个钟点，点「自定义」展开逐段输入
  await wizard.getByTestId("window-preset-custom").click();
  await wizard.getByTestId("window-start-0").fill("11:30");
  await expect(wizard.getByTestId("window-preview")).toContainText(
    "11:30-14:00",
  );
  await wizard.getByTestId("account-advanced-toggle").click();
  await wizard.getByTestId("account-daily-limit").selectOption("8");
  await wizard
    .getByTestId("account-daily-target")
    .getByRole("tab", { name: "1" })
    .click();
  await wizard.getByTestId("account-min-interval").selectOption("90");
  await capture(page, "screenshots/11-account-wizard-form-light.png");
  await wizard.getByTestId("wizard-submit").click();

  // 第三步：接入。账号已经落库，sidecar 驱动是 none → 必须**如实**说未接入
  await expect(wizard.getByTestId("wizard-created")).toBeVisible();
  await expect(wizard.getByTestId("wizard-warning")).toContainText(
    "sidecar 未接入",
  );
  const panel = wizard.getByTestId("sidecar-panel");
  await expect(panel).toBeVisible();
  await expect(panel).toHaveAttribute("data-state", "none-driver");
  await expect(wizard.getByTestId("sidecar-state")).toContainText("未接入");
  await capture(page, "screenshots/12-account-wizard-onboard-light.png");

  await wizard.getByTestId("wizard-done").click();

  // 卡片真的出现在页面上，而且后端台账里也有（不是只在前端 state 里）
  const card = page.locator('[data-testid="account-card"]', { hasText: name });
  await expect(card).toBeVisible();

  const listed = await request.get("/api/v1/accounts?platform=xhs");
  const rows: {
    id: string;
    name: string;
    policy: { publish_windows: string };
  }[] = (await listed.json()).data;
  const created = rows.find((r) => r.name === name);
  expect(created, "新建的账号必须真的在 /api/v1/accounts 里").toBeTruthy();
  expect(created!.policy.publish_windows).toContain("11:30-14:00");

  // 台账与 DB 不许漂移：check 端点等价物 —— 再同步一次应当没有任何待新建/待更新
  const synced = await request.post("/dev/sync_accounts?dry_run=true");
  const report = await synced.json();
  expect(report.created, `台账漂移了：${JSON.stringify(report)}`).toEqual([]);
  expect(report.updated, `台账漂移了：${JSON.stringify(report)}`).toEqual([]);
});

test("添加账号的表单校验：抖音缺 identity_hint、窗口起止相同、日上限选项不含超硬顶档位", async ({
  page,
}) => {
  await page.goto("/workbench/accounts/?theme=light");
  await page.getByTestId("add-account").click();
  const wizard = page.getByRole("dialog");
  await wizard.getByTestId("pick-platform-douyin").click();

  // 1) 名字空 + identity_hint 空 → 两条都得拦住，且说清楚为什么
  await wizard.getByTestId("wizard-submit").click();
  await expect(
    wizard.getByText("请填写名称。仅用于工作台显示，可随时修改。"),
  ).toBeVisible();
  await expect(wizard.getByText(/防发错号的唯一依据/)).toBeVisible();

  // 2) 窗口起止相同 = 永不放行（抖音缺省值不落在任何预设上，自定义段本来就是展开的）
  await wizard.getByTestId("account-name").fill(SCREENSHOT_DOUYIN_ACCOUNT_NAME);
  await wizard.getByTestId("account-identity-hint").fill("抖音 e2e 测试号");
  await wizard.getByTestId("window-end-0").fill("12:00");
  await wizard.getByTestId("wizard-submit").click();
  await expect(wizard.getByTestId("window-preview")).toContainText("永不放行");

  // 3) 日上限硬顶（P14.B4）：抖音上限 10 条，超顶档位在下拉里直接不存在——
  // 不用再填个 99 等着后端拒，UI 层面就选不出来
  await wizard.getByTestId("account-advanced-toggle").click();
  const limitValues = await wizard
    .getByTestId("account-daily-limit")
    .locator("option")
    .evaluateAll((opts) => opts.map((o) => Number((o as HTMLOptionElement).value)));
  expect(Math.max(...limitValues), "日上限下拉不该有超过硬顶的档位").toBe(10);

  await wizard.getByTestId("window-end-0").fill("13:30");
  await capture(page, "screenshots/13-account-wizard-invalid-light.png");

  // 一路错到这里都还停在表单上，什么都没建出来
  await expect(wizard.getByTestId("wizard-created")).toBeHidden();
});

test("出一条稿：账号卡上点一下 → 真跑生成链 → 跳审核台并定位到这条", async ({
  page,
  request,
}) => {
  await page.goto("/workbench/accounts/?theme=light");
  const card = page.locator(
    '[data-testid="account-card"][data-account-id="xhs-demo-01"]',
  );
  await expect(card).toBeVisible();

  const before = await request.get(
    "/api/v1/review?limit=200&account_id=xhs-demo-01",
  );
  const beforeIds = new Set(
    ((await before.json()).data.items as { id: string }[]).map((i) => i.id),
  );

  await card.getByTestId("generate-button").click();

  // P11：出稿先过弹层——这一下要花钱，人得先看清楚自己要付什么
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();

  // 这台 e2e core 的 SW_IMAGEGEN_ENABLED=false，所以配图控件必须**完全不渲染**
  // （P14.B4：不可用不给一个灰掉的控件让人自己猜），而且要把原因如实写出来
  await expect(dialog.getByTestId("illustration-count")).toHaveCount(0);
  await expect(dialog.getByTestId("imagegen-note")).toContainText(
    "SW_IMAGEGEN_ENABLED=false",
  );
  await capture(page, "screenshots/16-generate-modal-imagegen-off-light.png");

  await dialog.getByTestId("generate-submit").click();

  // 生成链是真的（ScriptedLLM + 机器审核 + 入队），几秒到十几秒
  await expect(page).toHaveURL(/\/workbench\/review\/\?id=/, {
    timeout: 90_000,
  });
  const url = new URL(page.url());
  const newId = url.searchParams.get("id");
  expect(newId, "跳转必须带上新条目的 id").toBeTruthy();
  expect(beforeIds.has(newId!), "应当是一条新的内容").toBeFalsy();

  // 审核台里真的选中了它
  const row = page.locator(
    `[data-testid="queue-row"][data-item-id="${newId}"]`,
  );
  await expect(row).toBeVisible();
  await expect(row).toHaveAttribute("aria-current", "true");
  await expect(page.getByTestId("decision-panel")).toBeVisible();

  // 后端也确认它是这个号名下的待审草稿
  const detail = await request.get(`/api/v1/review/${newId}`);
  const item = (await detail.json()).data.item;
  expect(item.account_id).toBe("xhs-demo-01");
  expect(["draft", "reviewing", "rejected"]).toContain(item.status);
  await capture(page, "screenshots/14-generated-in-review-light.png");
});

test("编辑账号：改窗口与日上限 → 保存 → 列表与后端都变了", async ({
  page,
  request,
}) => {
  await page.goto("/workbench/accounts/?theme=light");
  const card = page.locator(
    '[data-testid="account-card"][data-account-id="xhs-demo-02"]',
  );
  await expect(card).toBeVisible();
  await card.getByTestId("edit-button").click();

  const modal = page.getByRole("dialog", { name: /编辑/ });
  await expect(modal).toBeVisible();
  // 编辑弹窗是「inline」呈现（不折叠），表单要预填当前值，而不是空的
  await expect(modal.getByTestId("account-daily-limit")).toHaveValue("10");

  await modal.getByTestId("account-daily-limit").selectOption("6");
  // 这个号的窗口原样是「午休 + 晚间」预设，默认收着——点「自定义」才能改单段钟点
  await modal.getByTestId("window-preset-custom").click();
  await modal.getByTestId("window-start-0").fill("13:00");
  await modal.getByTestId("edit-save").click();
  await expect(modal).toBeHidden();

  await expect(card).toContainText("13:00-14:00");
  await expect(card).toContainText("/6");

  const listed = await request.get("/api/v1/accounts/xhs-demo-02");
  const account = (await listed.json()).data;
  expect(account.policy.daily_limit).toBe(6);
  expect(account.policy.publish_windows).toContain("13:00-14:00");

  // 台账也跟着改了：再同步一次没有待更新
  const synced = await request.post("/dev/sync_accounts?dry_run=true");
  expect((await synced.json()).updated).toEqual([]);
});

test("停用 / 启用：账号灰下来，出稿钮锁住，启用后回到正常", async ({
  page,
  request,
}) => {
  await page.goto("/workbench/accounts/?theme=light");
  const card = page.locator(
    '[data-testid="account-card"][data-account-id="wechat-demo-01"]',
  );
  await expect(card).toBeVisible();

  await card.getByTestId("toggle-active-button").click();
  await expect(card).toHaveAttribute("data-account-status", "suspended");
  await expect(card).toContainText("已停用");
  await expect(card.getByTestId("generate-button")).toBeDisabled();
  expect(
    (await (await request.get("/api/v1/accounts/wechat-demo-01")).json()).data
      .status,
  ).toBe("suspended");

  await card.getByTestId("toggle-active-button").click();
  await expect(card).toHaveAttribute("data-account-status", "ok");
  await expect(card.getByTestId("generate-button")).toBeEnabled();
});

test("发布前确认：没点之前不发，点完读数变成已确认（P12）", async ({
  page,
  request,
}) => {
  // 造数时故意留了一条批准但**没确认**的稿（见 seed.setup.ts）。
  // 它是这条闸门唯一的观测对象：窗口、限频、账号健康都不挡它，只有"没人点"挡得住。
  const listed = await request.get("/api/v1/content?status=scheduled&limit=50");
  const rows = (await listed.json()).data.items as Array<{
    id: string;
    scheduled_at: string | null;
    awaiting_confirm: boolean;
    confirm_deadline: string | null;
  }>;
  // **必须挑"已经到点"的那条**：`tick_scheduled_publish` 只扫 `scheduled_at <= now`，
  // 而排期页上等确认的稿不止一条——抖音号那条排在下一个发布窗口（未来），拿它当
  // 观测对象时 tick 根本扫不到，`skipped_unconfirmed` 会恒为 0。列表按排期时刻
  // 倒序返回，未来的排在前面，所以 `find(awaiting)` 不加这个条件必然挑错。
  const waiting = rows.find(
    (r) =>
      r.awaiting_confirm &&
      r.scheduled_at !== null &&
      Date.parse(r.scheduled_at) <= E2E_TIME_MS,
  );
  expect(waiting, "造数应留下一条**已到点**且等确认的稿").toBeTruthy();
  // 双时刻读数要的两个数：发布槽位与决定期限，必须都在
  expect(waiting!.confirm_deadline).toBeTruthy();

  // 没确认之前，跑一轮定时发布也一条都不发。
  // 闸门是有序的（账号 → 窗口 → 限频 → 确认 → 发布器），所以这里连读两个数：
  // 确认闸门确实计上了数，且这条稿不是被更早的窗口/限频闸门顺手挡住的。
  const tick = await request.post("/api/v1/system/ticks/scheduled_publish");
  const stats = (await tick.json()).data.stats;
  expect(stats.skipped_unconfirmed, JSON.stringify(stats)).toBeGreaterThan(0);
  expect(stats.published, "没人点之前一条都不许发").toBe(0);
  const still = await request.get(`/api/v1/content/${waiting!.id}`);
  expect((await still.json()).data.item.status).toBe("scheduled");

  await page.goto(`/workbench/schedule/?id=${waiting!.id}&theme=light`);
  const row = page.locator(`[data-item-id="${waiting!.id}"]`).first();
  await expect(row).toBeVisible();

  // 行内是一句递减的余量，不是一个"待确认"徽章
  const clock = row.getByTestId("confirm-clock").first();
  await expect(clock).toContainText(/还有 .*决定/);

  await row.getByTestId("confirm-button").first().click();
  // 本行断言而非全页断言：种子里视频闸门用例（「视频闸门」）自己也会批准并排上一条
  // 需要确认的稿子且故意不确认（作为它自己的既定现场），所以点完这一行之后，
  // 页面上仍会有别的行还带着 confirm-button——只有这一行的按钮必须消失。
  await expect(row.getByTestId("confirm-button")).toHaveCount(0);
  await expect(row.getByTestId("confirm-clock").first()).toContainText(
    "已确认",
  );

  // 后端也真的记下了，而且重复点会被挡住（一条内容只认第一次有效点击）
  const after = await request.get(`/api/v1/content/${waiting!.id}`);
  expect((await after.json()).data.item.confirmed_at).toBeTruthy();
  const replay = await request.post(`/api/v1/content/${waiting!.id}/confirm`, {
    data: {},
  });
  expect(replay.status()).toBe(409);
  expect((await replay.json()).error.code).toBe("confirm_conflict");

  // 点完之后，**同一个 tick** 就把它发出去了。这一步反过来钉死了上面那次跳过
  // 确实只因为"没人点"：账号健康、发布时段窗口、限频这三道闸门排在确认之前，
  // 但凡有一道不放行，这里也照样发不出去。少了它，`skipped_unconfirmed` 那个
  // 读数只是一个全局计数，证明不了挡住这一条的是哪道闸门。
  const republish = await request.post("/api/v1/system/ticks/scheduled_publish");
  const afterStats = (await republish.json()).data.stats;
  const done = await request.get(`/api/v1/content/${waiting!.id}`);
  expect(
    (await done.json()).data.item.status,
    `点完确认后应当立刻发得出去：${JSON.stringify(afterStats)}`,
  ).toBe("published");
});

test("提醒渠道：没配 Telegram 时说清为什么、怎么补（P12）", async ({
  page,
}) => {
  // e2e 里 Telegram 显式关掉（serve.sh）。这一块不能只显示"未配置"三个字，
  // 要给一句人能照着做的话。
  // 提醒渠道面板挂在「系统」页的 runtime tab 下（非默认 tab，见 screenshots.spec.ts
  // 里同一块面板的截图用例），不带 ?tab=runtime 进来的话这一块根本不会挂载
  await page.goto("/workbench/system/?tab=runtime&theme=light");
  const panel = page.getByTestId("telegram-guidance");
  await expect(panel).toBeVisible();
  await expect(panel).toContainText("TELEGRAM_BOT_TOKEN");
  await expect(page.getByTestId("telegram-headline")).toContainText("提醒渠道");
});

test("「对话」外链入口：没配 NEXT_PUBLIC_SW_CHAT_URL 时侧栏零 DOM，不留半个占位符（P14.B5）", async ({
  page,
}) => {
  // e2e 用的静态产物由 scripts/build_ui.sh 构建，构建期没有注入这个变量——
  // 断言的正是"没配置 = 没有这个入口"，而不是"有一个禁用的入口"
  await page.goto("/workbench/?theme=light");
  await expect(page.getByRole("link", { name: "今日" }).first()).toBeVisible();
  await expect(page.getByText("对话")).toHaveCount(0);
  await expect(page.getByTestId("external-nav-item")).toHaveCount(0);
});

/* ───────────────────── P13：企业控制台版式的两条硬指标 ─────────────────────
 *
 * 这一组守的是「视觉重构不许把可用性换掉」。两条都是**实测**，不肉眼估：
 * dormice 的原话是"验收标准：scrollWidth === clientWidth（headless 实测）"。
 */

test("外壳锁视口：滚动只发生在内容区，body 不出现纵向滚动条", async ({ page }) => {
  await page.goto("/workbench/schedule/?theme=light");
  await expect(page.getByRole("heading", { name: "排期" })).toBeVisible();

  // 1) 文档本身不滚：外壳是 h-svh overflow-hidden
  const doc = await page.evaluate(() => ({
    docOverflow: document.documentElement.scrollHeight - document.documentElement.clientHeight,
    bodyOverflow: document.body.scrollHeight - document.body.clientHeight,
  }));
  expect(doc.docOverflow, "文档不该有纵向溢出").toBeLessThanOrEqual(1);
  expect(doc.bodyOverflow, "body 不该有纵向溢出").toBeLessThanOrEqual(1);

  // 2) 滚动口真的存在，而且就是内容区那一个
  const port = page.locator("main.overflow-y-auto");
  await expect(port).toHaveCount(1);

  // 变异验证发现：常规视口下种子内容可能恰好装得下，外层锁没了 docOverflow 也量不出来。
  // 把视口压矮到内容必然超出，再量一次——"锁没了"在任何数据量下都藏不住。
  await page.setViewportSize({ width: 1280, height: 480 });
  const short = await page.evaluate(() => ({
    docOverflow: document.documentElement.scrollHeight - document.documentElement.clientHeight,
    bodyOverflow: document.body.scrollHeight - document.body.clientHeight,
  }));
  expect(short.docOverflow, "矮视口下文档也不该有纵向溢出").toBeLessThanOrEqual(1);
  expect(short.bodyOverflow, "矮视口下 body 也不该有纵向溢出").toBeLessThanOrEqual(1);
});

test("列宽军备：列表页表格不许横滚，分页条钉在框底可见", async ({ page }) => {
  await page.goto("/workbench/schedule/?theme=light");
  // 时间范围拉到「全部」，保证列表里一定有行（已排期的可能落在未来）
  await page.getByRole("tab", { name: "全部" }).last().click();
  await page.getByRole("tab", { name: "列表" }).click();

  const table = page.locator('[data-slot="data-table"]');
  await expect(table).toBeVisible();
  await expect(page.locator('[data-testid="content-row"]').first()).toBeVisible();

  // 表格的滚动口：横向必须不溢出——横滚常驻等于行操作默认不可见
  const port = table.locator("div.overflow-auto").first();
  const metrics = await port.evaluate((el) => ({
    scrollWidth: el.scrollWidth,
    clientWidth: el.clientWidth,
  }));
  expect(
    metrics.scrollWidth,
    `列表页表格横向溢出了 ${metrics.scrollWidth - metrics.clientWidth}px，操作列会被挤出去`,
  ).toBe(metrics.clientWidth);

  // 操作列的按钮真的在视口里（上面那条量的是溢出，这条量的是"看得见"）
  await expect(page.locator('[data-testid="row-menu"]').first()).toBeInViewport();

  // 分页条常驻且钉在框底：它一旦跟着页面滚，就永远躺在首屏之外
  const pager = page.getByTestId("table-pager");
  await expect(pager).toBeVisible();
  await expect(pager).toBeInViewport();
  await expect(pager).toContainText("共");
});

test("手机宽度：五页都不塌，导航仍然到得了", async ({ page }) => {
  // 用例名一直写着"五页"，但循环里只有四条路径——审核台（(focus) 壳、三区版式，
  // 最可能在窄屏上塌的那一页）从来没被量过。B6 走查把它补进来；(focus) 壳的
  // 横向导航轨用的是同一个 aria-label="主导航"，断言不必分叉。
  //
  // 两个宽度都量：任务书的走查口径是 **≤420px**，而 390 与 420 会落在不同的
  // 断点侧（Tailwind sm=640 之下还有组件自己的 min-width 兜底），只量一个
  // 宽度会漏掉"某一档正好卡住"的那类回归。
  const paths = [
    "/workbench/",
    "/workbench/review/",
    "/workbench/schedule/",
    "/workbench/accounts/",
    "/workbench/system/",
  ];
  for (const width of [390, 420]) {
    await page.setViewportSize({ width, height: 780 });
    for (const path of paths) {
      await page.goto(`${path}?theme=light`);
      // 顶栏删了之后，小屏上那条横向导航轨是唯一的换页入口，必须在
      await expect(page.getByRole("navigation", { name: "主导航" })).toBeVisible();
      // 整页不许横向溢出（塌版最典型的症状）
      const over = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      );
      expect(over, `${path} 在 ${width}px 宽下横向溢出 ${over}px`).toBeLessThanOrEqual(1);
    }
  }
});

/**
 * reduced-motion 降级（P13 口径，B6 走查补断言）。
 *
 * 口径是"装饰全停、spinner 减速"——不是把过渡也一并砍光：150ms 的**颜色**过渡
 * 不属于前庭风险，砍掉反而让按压态失去反馈。这里量的正是这条口径本身，
 * 免得以后有人把 globals.css 那个 media 块删了也没人发现。
 *
 * 探针元素当场造、量完就删：`.sw-shimmer` 只在加载途中存在，靠它抓不稳。
 */
test("动效降级：骨架与入场动画全停，spinner 减速到 1.8s", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/workbench/?theme=light");
  await expect(page.getByTestId("page-header")).toBeVisible();

  const probe = await page.evaluate(() => {
    const read = (cls: string) => {
      const el = document.createElement("div");
      el.className = cls;
      document.body.appendChild(el);
      const s = getComputedStyle(el);
      const out = { name: s.animationName, duration: s.animationDuration };
      el.remove();
      return out;
    };
    return {
      matches: matchMedia("(prefers-reduced-motion: reduce)").matches,
      shimmer: read("sw-shimmer"),
      fade: read("animate-fade-in"),
      pop: read("animate-pop-in"),
      spin: read("animate-spin"),
    };
  });

  expect(probe.matches, "浏览器应当真的报告 reduce").toBe(true);
  expect(probe.shimmer.name, "骨架微光要停").toBe("none");
  expect(probe.fade.name, "淡入要停").toBe("none");
  expect(probe.pop.name, "弹入要停").toBe("none");
  // spinner 不停——转圈是"还在跑"的唯一读数，停了等于说谎；只减速
  expect(probe.spin.name, "spinner 不该被停掉").not.toBe("none");
  expect(probe.spin.duration, "spinner 应当减速到 1.8s").toBe("1.8s");
});
