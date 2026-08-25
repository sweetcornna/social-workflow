#!/usr/bin/env node
/**
 * 把 assets/icon.html 栅格化成 assets/icon.png（1024×1024，透明角）。
 *
 *   node desktop/scripts/make-icon.mjs
 *
 * 依赖 ui/ 里已装好的 playwright（devDep，桌面壳自己不引）。Caprasimo 字体从
 * ui/node_modules/@fontsource/caprasimo 现读现内联——OFL 字体不入库，只入库产物 png。
 * electron-builder 再从这张 png 自动转 icns / ico，所以只维护这一个源。
 */

import { createRequire } from 'node:module';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DESKTOP = path.resolve(HERE, '..');
const REPO = path.resolve(DESKTOP, '..');
const UI_MODULES = path.join(REPO, 'ui', 'node_modules');

function resolvePlaywright() {
  const direct = path.join(UI_MODULES, 'playwright-core', 'index.js');
  if (fs.existsSync(direct)) return direct;
  // pnpm 的实际落点：ui/node_modules/.pnpm/playwright-core@x.y.z/node_modules/playwright-core
  const pnpmDir = path.join(UI_MODULES, '.pnpm');
  if (fs.existsSync(pnpmDir)) {
    const hit = fs
      .readdirSync(pnpmDir)
      .filter((name) => name.startsWith('playwright-core@'))
      .sort()
      .reverse()
      .map((name) => path.join(pnpmDir, name, 'node_modules', 'playwright-core', 'index.js'))
      .find((p) => fs.existsSync(p));
    if (hit) return hit;
  }
  throw new Error('找不到 playwright-core：先在 ui/ 里跑一次 `pnpm install`');
}

function readFontDataUri() {
  const candidates = [
    path.join(UI_MODULES, '@fontsource', 'caprasimo', 'files', 'caprasimo-latin-400-normal.woff2'),
  ];
  const pnpmDir = path.join(UI_MODULES, '.pnpm');
  if (fs.existsSync(pnpmDir)) {
    for (const name of fs.readdirSync(pnpmDir)) {
      if (!name.startsWith('@fontsource+caprasimo@')) continue;
      candidates.push(
        path.join(
          pnpmDir,
          name,
          'node_modules',
          '@fontsource',
          'caprasimo',
          'files',
          'caprasimo-latin-400-normal.woff2',
        ),
      );
    }
  }
  const hit = candidates.find((p) => fs.existsSync(p));
  if (!hit) throw new Error('找不到 Caprasimo woff2：先在 ui/ 里跑一次 `pnpm install`');
  return `data:font/woff2;base64,${fs.readFileSync(hit).toString('base64')}`;
}

const require = createRequire(import.meta.url);
const { chromium } = require(resolvePlaywright());

const TOKEN = '__CAPRASIMO_WOFF2__';
const template = fs.readFileSync(path.join(DESKTOP, 'assets', 'icon.html'), 'utf8');
const hits = template.split(TOKEN).length - 1;
if (hits !== 1) throw new Error(`icon.html 里 ${TOKEN} 出现了 ${hits} 次，应当恰好 1 次`);
const html = template.replace(TOKEN, readFontDataUri());
const out = path.join(DESKTOP, 'assets', 'icon.png');

const browser = await chromium.launch();
try {
  const page = await browser.newPage({
    viewport: { width: 1024, height: 1024 },
    deviceScaleFactor: 1,
  });
  await page.setContent(html, { waitUntil: 'load' });
  // 光等 fonts.ready 不够：字体没真被用上时它也 resolve，字形会静默退化成衬线体
  const loaded = await page.evaluate(async () => {
    await document.fonts.load('430px Caprasimo', 'sw');
    await document.fonts.ready;
    return document.fonts.check('430px Caprasimo');
  });
  if (!loaded) throw new Error('Caprasimo 没加载上，图标会退化成系统衬线体——停下来别出错图');
  await page.screenshot({ path: out, omitBackground: true });
  console.log(`icon → ${out} (${fs.statSync(out).size} bytes)`);
} finally {
  await browser.close();
}
