'use strict';

/**
 * core 地址的解析与落盘。
 *
 * 优先级（P18 任务书裁决，不重新论证）：
 *   env `SW_CORE_ORIGIN`  >  userData/config.json 的 `coreOrigin`  >  默认隧道口
 *
 * 只存 origin（scheme://host:port），**不存路径**：core 的 `/api/v1`、`/review`
 * 都挂在根上，允许带路径前缀只会让代理拼错 URL。
 * 这个文件里不会出现任何凭据——token 由 UI 自己放在浏览器 localStorage 里。
 */

const fs = require('node:fs');
const path = require('node:path');

/** 默认地址 = P17 的本机隧道口。壳不猜别的。 */
const DEFAULT_CORE_ORIGIN = 'http://127.0.0.1:18000';

function configPath(app) {
  return path.join(app.getPath('userData'), 'config.json');
}

function readConfig(app) {
  try {
    const parsed = JSON.parse(fs.readFileSync(configPath(app), 'utf8'));
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    // 文件不存在 / 被手改坏了都当作"没配"，不要让壳起不来
    return {};
  }
}

/**
 * 把用户输入规整成 origin。非法返回 null（调用方负责报错，别静默吞掉）。
 * 允许省略 scheme（`127.0.0.1:8000` → `http://127.0.0.1:8000`）。
 */
function normalizeOrigin(value) {
  const raw = String(value ?? '').trim();
  if (!raw) return null;
  const withScheme = /^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(raw) ? raw : `http://${raw}`;
  let url;
  try {
    url = new URL(withScheme);
  } catch {
    return null;
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return null;
  if (!url.hostname) return null;
  return url.origin;
}

function envCoreOrigin() {
  return normalizeOrigin(process.env.SW_CORE_ORIGIN);
}

function storedCoreOrigin(app) {
  return normalizeOrigin(readConfig(app).coreOrigin);
}

function resolveCoreOrigin(app) {
  return envCoreOrigin() || storedCoreOrigin(app) || DEFAULT_CORE_ORIGIN;
}

/** 写回 config.json。传 null 表示清空这一项（回落到默认）。 */
function writeCoreOrigin(app, origin) {
  const cfg = readConfig(app);
  if (origin) cfg.coreOrigin = origin;
  else delete cfg.coreOrigin;
  const target = configPath(app);
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, `${JSON.stringify(cfg, null, 2)}\n`, 'utf8');
}

module.exports = {
  DEFAULT_CORE_ORIGIN,
  configPath,
  envCoreOrigin,
  normalizeOrigin,
  resolveCoreOrigin,
  storedCoreOrigin,
  writeCoreOrigin,
};
