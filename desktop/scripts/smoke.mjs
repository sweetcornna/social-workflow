#!/usr/bin/env node
/**
 * 桌面壳冒烟：起真壳、打真 core，把证据落成截图 + 一行行的请求日志。
 *
 *   node desktop/scripts/smoke.mjs --core http://127.0.0.1:8000 --out /tmp/shots
 *
 * 跑之前要有两样东西：
 *   1. `cd ui && pnpm build`（壳 dev 态直接吃 ui/out）
 *   2. 一份隔离 core：`bash ui/e2e/serve.sh 8000`
 *
 * 两轮，对应 P18 验证清单第 1 条：
 *   轮 1（带 SW_CORE_ORIGIN）：env 优先级生效 → 工作台渲染、/api/v1 全 2xx/3xx
 *   轮 2（不带 env，userData 是空的）：默认隧道口 18000 连不上 → 工作台离线态；
 *        菜单「设置 core 地址…」改成真 core → 数据回来；改成错地址 → 又离线；再改回来
 * 轮 2 走的是菜单项**真实的 click 回调** + 小窗真实的保存流程，不是直接写配置文件。
 * 每轮都用独立的 --user-data-dir，跑多少次结果都一样。
 *
 * playwright 从 ui/ 借（桌面壳自己不引），和 scripts/make-icon.mjs 同一套路数。
 */

import { createRequire } from 'node:module';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DESKTOP = path.resolve(HERE, '..');
const REPO = path.resolve(DESKTOP, '..');
const UI_MODULES = path.join(REPO, 'ui', 'node_modules');

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

const CORE = arg('core', 'http://127.0.0.1:8000');
const BAD_CORE = arg('bad-core', 'http://127.0.0.1:59999');
const OUT = path.resolve(arg('out', path.join(DESKTOP, 'dist', 'smoke')));
fs.mkdirSync(OUT, { recursive: true });

function resolveFromUi(pkg) {
  const direct = path.join(UI_MODULES, pkg, 'index.js');
  if (fs.existsSync(direct)) return direct;
  const pnpmDir = path.join(UI_MODULES, '.pnpm');
  const hit = fs.existsSync(pnpmDir)
    ? fs
        .readdirSync(pnpmDir)
        .filter((n) => n.startsWith(`${pkg}@`))
        .sort()
        .reverse()
        .map((n) => path.join(pnpmDir, n, 'node_modules', pkg, 'index.js'))
        .find((p) => fs.existsSync(p))
    : null;
  if (!hit) throw new Error(`找不到 ${pkg}：先在 ui/ 里跑一次 \`pnpm install\``);
  return hit;
}

const require = createRequire(import.meta.url);
const { _electron: electron } = require(resolveFromUi('playwright'));

const electronBin = require(path.join(DESKTOP, 'node_modules', 'electron'));
if (typeof electronBin !== 'string') throw new Error('electron 二进制没装好');
if (!fs.existsSync(path.join(REPO, 'ui', 'out', 'index.html'))) {
  throw new Error('ui/out 不存在：先 `cd ui && pnpm build`');
}

const log = [];
function note(line) {
  log.push(line);
  console.log(line);
}

/** 让页面把该发的请求都发完；SWR 一直在轮询，networkidle 永远等不到。 */
async function settle(page, ms = 2500) {
  await page.waitForLoadState('domcontentloaded').catch(() => {});
  await page.waitForTimeout(ms);
}

function launch(userDataDir, extraEnv) {
  const env = { ...process.env };
  delete env.SW_CORE_ORIGIN;
  Object.assign(env, extraEnv ?? {});
  return electron.launch({
    executablePath: electronBin,
    args: [DESKTOP, `--user-data-dir=${userDataDir}`],
    cwd: DESKTOP,
    env,
  });
}

function attachApiLog(page) {
  const api = [];
  page.on('response', (r) => {
    const u = new URL(r.url());
    if (u.pathname.startsWith('/api/') || u.pathname.startsWith('/review/')) {
      api.push(`${r.status()} ${r.request().method()} ${u.pathname}${u.search}`);
    }
  });
  return api;
}

function verdict(tag, api) {
  const bad = api.filter((l) => !/^[23]\d\d /.test(l));
  note(`[${tag}] 共 ${api.length} 个数据面请求，非 2xx/3xx ${bad.length} 个${bad.length ? `：${bad.join(' | ')}` : ''}`);
  return bad;
}

const tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'sw-shell-smoke-'));

// ============================================================ 轮 1：env 优先级
{
  const app = await launch(path.join(tmpRoot, 'ud1'), { SW_CORE_ORIGIN: CORE });
  try {
    const page = await app.firstWindow();
    const api = attachApiLog(page);
    await settle(page, 3500);

    note(`[1] 页面 URL = ${page.url()}`);
    note(`[1] 窗口标题 = ${JSON.stringify(await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].getTitle()))}`);
    note(`[1] 文档标题 = ${JSON.stringify(await page.title())}`);
    await page.screenshot({ path: path.join(OUT, '1-env-online.png') });
    note(`[1] 请求样本：\n    ${api.slice(0, 14).join('\n    ')}`);
    verdict('1', api);

    // 菜单文案落成证据
    const menu = await app.evaluate(({ Menu }) => {
      const dump = (items) =>
        items.map((i) => ({ label: i.label, type: i.type, sub: i.submenu ? dump(i.submenu.items) : undefined }));
      return dump(Menu.getApplicationMenu().items);
    });
    fs.writeFileSync(path.join(OUT, 'menu.json'), JSON.stringify(menu, null, 2));
    note(`[1] 顶层菜单：${menu.map((m) => m.label).join(' / ')}`);

    // 小窗要如实说"env 压着配置文件"
    await app.evaluate(({ Menu }) => Menu.getApplicationMenu().getMenuItemById('set-core-origin').click());
    const prompt = await app.waitForEvent('window');
    await prompt.waitForLoadState('domcontentloaded');
    await prompt.waitForTimeout(600);
    note(`[1] 小窗生效地址 = ${await prompt.textContent('#effective')}`);
    note(`[1] 小窗 env 提示 = ${(await prompt.textContent('#env'))?.trim()}`);
    await prompt.screenshot({ path: path.join(OUT, '1-prompt-env-override.png') });
    await prompt.click('#cancel');
  } finally {
    await app.close();
  }
}

// =================================================== 轮 2：配置文件 + 菜单真实改
{
  const app = await launch(path.join(tmpRoot, 'ud2'));
  try {
    const page = await app.firstWindow();
    const api = attachApiLog(page);
    await settle(page, 3500);

    note(`\n[2a] 全新 userData → 默认 http://127.0.0.1:18000（没起 core）`);
    note(`[2a] 请求样本：\n    ${api.slice(0, 6).join('\n    ')}`);
    verdict('2a', api);
    await page.screenshot({ path: path.join(OUT, '2a-default-offline.png') });

    async function setCoreViaMenu(value, shot) {
      await app.evaluate(({ Menu }) => Menu.getApplicationMenu().getMenuItemById('set-core-origin').click());
      const prompt = await app.waitForEvent('window');
      await prompt.waitForLoadState('domcontentloaded');
      await prompt.waitForTimeout(500);
      await prompt.fill('#origin', value);
      if (shot) await prompt.screenshot({ path: path.join(OUT, shot) });
      await prompt.click('#save');
      await settle(page, 3500);
    }

    api.length = 0;
    await setCoreViaMenu(CORE, '2-prompt.png');
    note(`\n[2b] 菜单改成 ${CORE}`);
    note(`[2b] 请求样本：\n    ${api.slice(0, 14).join('\n    ')}`);
    verdict('2b', api);
    await page.screenshot({ path: path.join(OUT, '2b-menu-online.png') });

    api.length = 0;
    await setCoreViaMenu(BAD_CORE);
    note(`\n[2c] 菜单改成错地址 ${BAD_CORE}`);
    note(`[2c] 请求样本：\n    ${api.slice(0, 6).join('\n    ')}`);
    verdict('2c', api);
    await page.screenshot({ path: path.join(OUT, '2c-menu-offline.png') });

    api.length = 0;
    await setCoreViaMenu(CORE);
    note(`\n[2d] 菜单改回 ${CORE}`);
    note(`[2d] 请求样本：\n    ${api.slice(0, 14).join('\n    ')}`);
    verdict('2d', api);
    await page.screenshot({ path: path.join(OUT, '2d-menu-back-online.png') });

    const cfg = path.join(tmpRoot, 'ud2', 'config.json');
    note(`\n[2] 落盘的 config.json = ${fs.existsSync(cfg) ? fs.readFileSync(cfg, 'utf8').trim() : '（不存在）'}`);
  } finally {
    await app.close();
  }
}

fs.writeFileSync(path.join(OUT, 'smoke.log'), `${log.join('\n')}\n`);
fs.rmSync(tmpRoot, { recursive: true, force: true });
note(`\n证据目录：${OUT}`);
