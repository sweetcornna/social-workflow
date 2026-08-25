import { expect, test as setup } from "@playwright/test";

import { E2E_TIME_MS } from "./time";

/**
 * 造数据。跑在所有用例之前（playwright.config.ts 的 `seed` project）。
 *
 * 全部走 core 自己的 `/dev/*` 端点与 `/api/v1`，不直接改库：
 *  - `/dev/seed` ×4：1 个 fake 小红书账号 + 4 条图文草稿；首次同时注入固定候选选题，
 *    避免外部热榜源不可达时影响后续的完整生成链验收
 *  - `/dev/run_douyin_pipeline?skip_render=true` ×3：口播脚本 + 样本成片，
 *    产出 `needs_watch=true` 的草稿（视频闸门 e2e 与截图各用一条）
 *  - 批准 + 确认其中一条并跑 tick，让看板 / 统计 / 发布记录不是全空
 *  - 另外两条批准了**故意不确认**：一条在带窗口的抖音号上（排期页的「等你确认」
 *    现场），一条在全天号上**且已经到点**（发布前确认闸门的观测对象，见下方
 *    「确认闸门的观测对象」注释）
 *  - `sourcing` tick 拉热榜填选题页（拉不到就走空态）
 */
setup("造 e2e 数据", async ({ request }) => {
  const seededIds: string[] = [];
  for (let i = 0; i < 4; i++) {
    const seeded = await request.post(`/dev/seed${i === 0 ? "?include_topic=true" : ""}`);
    expect(seeded.ok(), await seeded.text()).toBeTruthy();
    seededIds.push((await seeded.json()).content_item_id);
  }

  // 把 `/dev/seed` 那个号的最小间隔压到 0（走公开的改配置端点，不直接改库）。
  //
  // 为什么必须这么做：`tick_scheduled_publish` 的五道闸门是**有序**的——
  // ①账号健康 ②发布时段窗口 ③限频 ④人工确认 ⑤发布器。要让 flows.spec 的
  // 「发布前确认」用例真的观测到闸门④，它盯的那条稿必须同时满足：已经到点
  // （`scheduled_at <= now`，否则 tick 根本扫不到）、账号全天可发（否则先撞②）、
  // 且不被限频挡住（否则先撞③）。
  //
  // 全仓只有 `acc_demo_xhs` 没有 publish_windows（台账里四个号全带窗口），所以
  // 观测对象只能落在它头上。但它同时还要承担"造一条真发出去的记录"（看板 /
  // 统计 / 发布记录不能全空），而小红书没有平台级最小间隔下限、缺省回落到
  // `SW_MIN_PUBLISH_INTERVAL_SECONDS=900`——于是"已发一条"和"另一条也到点"
  // 在同一个号上互斥：第二条会被排到 15 分钟后，永远扫不到。把间隔改成 0，
  // 两条才能都落在"此刻"。
  //
  // 这正是 flows.spec.ts:673 那条 `skipped_unconfirmed > 0` 断言长期失败的根因：
  // 原来的观测对象是抖音号上那条，抖音窗口是 12:00-13:30 / 18:00-22:00
  // (Asia/Shanghai)，批准即排期只会把它排到**下一个窗口开始**——真实跑测的时刻
  // 落在窗口外时它就是一条未来的稿，tick 扫不到，`skipped_unconfirmed` 恒为 0。
  const relax = await request.patch("/api/v1/accounts/acc_demo_xhs", {
    data: { min_interval_minutes: 0 },
  });
  expect(relax.ok(), await relax.text()).toBeTruthy();

  // accounts.yaml 已在启动时同步入库；确认抖音账号在
  const accounts = await request.get("/api/v1/accounts");
  expect(accounts.ok()).toBeTruthy();
  const ids: string[] = (await accounts.json()).data.map(
    (a: { id: string }) => a.id,
  );
  expect(ids).toContain("douyin-demo-01");

  // 抖音全链路（跳过真渲染，直接挂 tests/fixtures 里的样本片）。
  //
  // 跑**三**条，各有各的去处，缺一条后面就有用例要落空：
  //   [0] 这里就地批准掉 → 给"改期"用例一个**账号带发布窗口**的可改期目标
  //       （`/dev/seed` 造的 acc_demo_xhs 没有 publish_windows，改期怎么填都合法，
  //        验不到 invalid_slot 那条路；只有 accounts.yaml 里的号才有窗口）
  //   [1] 留给 flows 的"视频闸门"用例去界面上批准
  //   [2] 留在队列里，给审核页截图当素材
  //
  // 参数组合是**全离线**的：skip_sourcing 不拉热榜、use_llm_review=false 不调模型、
  // skip_render 直接挂样本片、make_cover=false 不起 chromium。所以这里硬断言成功——
  // 跑不成就是真的坏了，不能退化成 console.warn 让下游用例静默 skip。
  const douyinIds: string[] = [];
  for (const topic of [
    "通勤成本上涨怎么省",
    "小户型收纳的三个死角",
    "租房党的厨房改造清单",
  ]) {
    const douyin = await request.post(
      "/dev/run_douyin_pipeline?account_id=douyin-demo-01&skip_render=true&make_cover=false" +
        `&skip_sourcing=true&use_llm_review=false&topic=${encodeURIComponent(topic)}`,
      { timeout: 120_000 },
    );
    expect(
      douyin.ok(),
      `run_douyin_pipeline(${topic}) 失败：${await douyin.text()}`,
    ).toBeTruthy();
    const id = (await douyin.json()).content_item_id;
    expect(
      id,
      `run_douyin_pipeline(${topic}) 没返回 content_item_id`,
    ).toBeTruthy();
    douyinIds.push(id);
  }

  // [0] 批准掉。含视频 → 必须带 watched:true 过闸门（否则 422 watch_required）。
  // 批准即排期：排上了是 scheduled，排不上停在 approved，两种都算可改期。
  const approveVideo = await request.post(
    `/api/v1/review/${douyinIds[0]}/approve`,
    {
      data: {
        actor: "playwright",
        reason: "e2e 造数：给改期用例留一个带窗口的目标",
        watched: true,
      },
    },
  );
  expect(approveVideo.ok(), await approveVideo.text()).toBeTruthy();

  // 批准第一条图文草稿 → 批准即排期，看板/时间线/审计日志立刻有内容
  const approve = await request.post(`/api/v1/review/${seededIds[0]}/approve`, {
    data: { actor: "playwright", reason: "e2e 造数" },
  });
  expect(approve.ok(), await approve.text()).toBeTruthy();

  // 驳回第二条，让「已驳回待改」不是 0
  const reject = await request.post(`/api/v1/review/${seededIds[1]}/reject`, {
    data: { actor: "playwright", reason: "标题太平，换一个更具体的钩子" },
  });
  expect(reject.ok(), await reject.text()).toBeTruthy();

  // ── 确认闸门的观测对象 ──────────────────────────────────────────────
  // 第四条图文草稿：批准（→ 排到"此刻"，因为上面刚把该号最小间隔压成 0）后
  // **故意不确认**。它是 flows.spec「发布前确认」用例唯一合格的观测对象——
  // 全天号、已到点、不撞限频，五道闸门里只剩"没人点"能挡住它。
  // 排期页的「等你确认」现场因此有两处：这条（今天此刻）与下面视频那条
  // （抖音下一个窗口），两种时相都在截图里。
  // 先把内容改得和别的种子稿不一样。`/dev/seed` 每次吐出的内容包是**逐字节相同**的，
  // 而发布幂等键 = (账号, 平台, 内容哈希, 槽位到分钟)——同一个号、同一分钟、同样的
  // 内容，第二条会命中第一条已经 done 的幂等记录，`publish_with_idempotency` 直接
  // 返回既有结果（这是"绝不重复发"的正确行为），但那条内容自己会一直停在
  // `scheduled`。观测对象必须能真的发出去，所以这里给它一份独有的正文。
  const distinct = await request.post(`/api/v1/review/${seededIds[3]}/edit`, {
    data: {
      actor: "playwright",
      title: "阳台种薄荷的三个新手坑",
      body_markdown:
        "# 阳台种薄荷的三个新手坑\n\n" +
        "1. 浇水过勤：薄荷耐旱，盆土表层干透再浇。\n" +
        "2. 不掐尖：长到 15cm 就掐顶，否则只长高不长叶。\n" +
        "3. 西晒暴晒：夏天下午挪进阴凉处，叶片才不发黄。\n\n" +
        "（e2e 造数：与其它种子稿区分开，避免撞上发布幂等键。）",
      tags: ["阳台", "种植", "新手"],
      reason: "e2e 造数：让确认闸门的观测对象有独立的内容哈希",
    },
  });
  expect(distinct.ok(), await distinct.text()).toBeTruthy();

  const awaiting = await request.post(`/api/v1/review/${seededIds[3]}/approve`, {
    data: { actor: "playwright", reason: "e2e 造数：留给确认闸门当观测对象" },
  });
  expect(awaiting.ok(), await awaiting.text()).toBeTruthy();
  const awaitingBody = (await awaiting.json()).data;
  expect(
    awaitingBody.item.status,
    "确认闸门的观测对象必须真的排上期（approved 说明槽位没算出来）",
  ).toBe("scheduled");
  expect(
    Date.parse(awaitingBody.item.scheduled_at),
    "观测对象必须已经到点，否则 tick 扫不到它",
  ).toBeLessThanOrEqual(E2E_TIME_MS + 1000);

  // P12：批准之后还有第五道闸门——发布前的人工确认。不点这一下，
  // `scheduled_publish` 一条都发不出去，看板 / 统计 / 发布记录会全空。
  // 只确认图文那条：视频那条**故意留着不确认**，排期页上就有一个真实的
  // 「等你确认」现场（截图用它当带窗口账号的样本）。
  const confirm = await request.post(
    `/api/v1/content/${seededIds[0]}/confirm`,
    {
      data: { actor: "playwright" },
    },
  );
  expect(confirm.ok(), await confirm.text()).toBeTruthy();

  // 跑几个毫秒级 tick，让发布记录/统计不是全空
  for (const tick of [
    "scheduled_publish",
    "retry_sweep",
    "render_jobs",
    "metrics",
  ]) {
    const res = await request.post(`/api/v1/system/ticks/${tick}`);
    expect(res.ok(), `${tick}: ${await res.text()}`).toBeTruthy();
  }

  // 选题页要有东西看。这个 tick 会拉外网热榜，拉不到就算了（走空态）
  const sourcing = await request.post("/api/v1/system/ticks/sourcing", {
    timeout: 90_000,
  });
  if (!sourcing.ok()) {
    console.warn(
      "sourcing tick 没跑成（多半是拉不到外网热榜），选题页会是空态",
    );
  }
});
