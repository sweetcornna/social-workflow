'use strict';

/**
 * 壳内嵌的 HTTP 服务器（127.0.0.1 随机口）。它只干两件事：
 *
 *   1. 把打包进 app 的 ui 静态导出挂在 `/workbench`——语义对齐 core 的
 *      `StaticFiles(html=True)`：目录补 `index.html`、未命中回 Next 导出的 `404.html`，
 *      并复刻 core 的缓存头（`_next/static/` immutable、HTML no-cache）。
 *   2. 把 `/api/*` 和 `/review/*` **流式**反代到 core。媒体（图集 / mp4 / 公众号预览）
 *      走的就是 `/review/{id}/media/{i}`，必须 pipe 不缓冲，也不能碰 Range。
 *
 * 为什么手写而不是引 http-proxy：需要的只有"把 req pipe 到 upstream、把 upstream
 * pipe 回 res"，node 内置 http 就够；桌面壳的依赖面越小，供应链和体积越好交代。
 *
 * 安全：只 listen 127.0.0.1；代理目标只有解析出来的那一个 origin（req.url 必须以
 * `/` 开头，绝对形式 URI 直接拒），所以这不是一个开放代理。
 */

const fs = require('node:fs');
const fsp = require('node:fs/promises');
const http = require('node:http');
const https = require('node:https');
const path = require('node:path');

/** core 把静态导出挂在这个前缀下，ui 的 basePath 也是它。 */
const BASE_PATH = '/workbench';
/** 数据面 + 媒体面。媒体端点刻意不在 /api/v1 下（见 ui/lib/api.ts 注释）。 */
const PROXY_PREFIXES = ['/api/', '/review/'];

/** 逐跳头，转发两个方向都要摘掉（RFC 9110 §7.6.1）。 */
const HOP_BY_HOP = new Set([
  'connection',
  'keep-alive',
  'proxy-authenticate',
  'proxy-authorization',
  'te',
  'trailer',
  'transfer-encoding',
  'upgrade',
]);

const IMMUTABLE_PREFIX = '_next/static/';
const IMMUTABLE_CACHE = 'public, max-age=31536000, immutable';
const HTML_CACHE = 'no-cache';

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.txt': 'text/plain; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.webp': 'image/webp',
  '.avif': 'image/avif',
  '.ico': 'image/x-icon',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.ttf': 'font/ttf',
  '.mp4': 'video/mp4',
  '.webm': 'video/webm',
  '.map': 'application/json; charset=utf-8',
};

function isProxyPath(pathname) {
  return PROXY_PREFIXES.some((p) => pathname === p.slice(0, -1) || pathname.startsWith(p));
}

function sendText(res, status, body, headers = {}) {
  const buf = Buffer.from(body, 'utf8');
  res.writeHead(status, {
    'content-type': 'text/plain; charset=utf-8',
    'content-length': String(buf.length),
    'cache-control': 'no-store',
    ...headers,
  });
  res.end(buf);
}

/**
 * core 连不上时的应答。
 *
 * `/api/*` 回一个**合法 envelope**：ui 的 apiFetch 按 `{ok,error}` 分支，随便回点
 * 别的它只会报 `bad_envelope`，用户看到的错更没信息量。壳到此为止——离线怎么画是
 * UI 自己的事，壳不弹窗不换页。
 */
function sendCoreUnreachable(res, pathname, coreOrigin, err) {
  const reason = err && err.code ? err.code : 'ECONNFAILED';
  if (pathname.startsWith('/api/')) {
    const body = Buffer.from(
      JSON.stringify({
        ok: false,
        data: null,
        error: {
          code: 'core_unreachable',
          message: `连不上 core（${coreOrigin}）：${reason}。菜单「设置 core 地址…」可以改。`,
          detail: { core_origin: coreOrigin, errno: reason },
        },
      }),
      'utf8',
    );
    res.writeHead(502, {
      'content-type': 'application/json; charset=utf-8',
      'content-length': String(body.length),
      'cache-control': 'no-store',
    });
    res.end(body);
    return;
  }
  sendText(res, 502, `连不上 core（${coreOrigin}）：${reason}\n`);
}

function proxy(req, res, coreOrigin, pathname) {
  let target;
  try {
    target = new URL(req.url, coreOrigin);
  } catch {
    sendText(res, 400, '请求 URL 非法\n');
    return;
  }
  // 目标 origin 必须还是配置的那个——防止 req.url 是绝对形式把我们变成开放代理
  if (target.origin !== coreOrigin) {
    sendText(res, 400, '只允许代理到已配置的 core 地址\n');
    return;
  }

  const headers = {};
  for (const [k, v] of Object.entries(req.headers)) {
    if (!HOP_BY_HOP.has(k.toLowerCase())) headers[k] = v;
  }
  // host 原样转发（还是壳的 127.0.0.1:随机口）：FastAPI 的 redirect_slashes 用它拼
  // Location，trailingSlash 产生的 308 才会指回壳，而不是把浏览器踢到 core 上去。

  const mod = target.protocol === 'https:' ? https : http;
  const upstream = mod.request(
    {
      protocol: target.protocol,
      hostname: target.hostname,
      port: target.port || (target.protocol === 'https:' ? 443 : 80),
      method: req.method,
      path: target.pathname + target.search,
      headers,
    },
    (up) => {
      const out = {};
      for (const [k, v] of Object.entries(up.headers)) {
        if (!HOP_BY_HOP.has(k.toLowerCase())) out[k] = v;
      }
      res.writeHead(up.statusCode || 502, out);
      up.pipe(res);
      up.on('error', () => res.destroy());
    },
  );

  upstream.on('error', (err) => {
    if (res.headersSent) {
      res.destroy();
      return;
    }
    sendCoreUnreachable(res, pathname, coreOrigin, err);
  });

  res.on('close', () => {
    if (!res.writableEnded) upstream.destroy();
  });
  req.on('error', () => upstream.destroy());
  req.pipe(upstream);
}

function cacheHeaderFor(relPath, contentType) {
  if (relPath.startsWith(IMMUTABLE_PREFIX)) return IMMUTABLE_CACHE;
  if (contentType.startsWith('text/html')) return HTML_CACHE;
  return null;
}

async function statOrNull(p) {
  try {
    return await fsp.stat(p);
  } catch {
    return null;
  }
}

function serveFile(req, res, filePath, relPath, status = 200) {
  const type = MIME[path.extname(filePath).toLowerCase()] || 'application/octet-stream';
  const headers = { 'content-type': type };
  const cache = cacheHeaderFor(relPath, type);
  if (cache) headers['cache-control'] = cache;

  const stream = fs.createReadStream(filePath);
  stream.on('open', () => {
    res.writeHead(status, headers);
    stream.pipe(res);
  });
  stream.on('error', () => {
    if (!res.headersSent) sendText(res, 500, '读取静态资源失败\n');
    else res.destroy();
  });
  res.on('close', () => stream.destroy());
}

/**
 * 静态分发，语义对齐 `StaticFiles(directory=ui/out, html=True)`。
 * `root` 不存在时给一句人话，而不是白屏——那说明打包时漏了 extraResources。
 */
async function serveStatic(req, res, root, pathname) {
  let decoded;
  try {
    decoded = decodeURIComponent(pathname);
  } catch {
    sendText(res, 400, '路径编码非法\n');
    return;
  }
  if (decoded.includes('\0')) {
    sendText(res, 400, '路径非法\n');
    return;
  }

  if (decoded === '/' || decoded === BASE_PATH) {
    res.writeHead(302, { location: `${BASE_PATH}/`, 'cache-control': 'no-store' });
    res.end();
    return;
  }
  if (!decoded.startsWith(`${BASE_PATH}/`)) {
    sendText(res, 404, '不存在\n');
    return;
  }

  if (!(await statOrNull(root))) {
    sendText(res, 500, `工作台静态产物缺失：${root}\n打包时漏了 extraResources（ui/out → workbench）。\n`);
    return;
  }

  const rel = decoded.slice(BASE_PATH.length + 1);
  const rootResolved = path.resolve(root);
  const target = path.resolve(rootResolved, rel);
  if (target !== rootResolved && !target.startsWith(rootResolved + path.sep)) {
    sendText(res, 403, '越界访问\n');
    return;
  }

  const st = await statOrNull(target);
  if (st && st.isDirectory()) {
    const index = path.join(target, 'index.html');
    if (await statOrNull(index)) {
      serveFile(req, res, index, `${rel.replace(/\/?$/, '/')}index.html`);
      return;
    }
  } else if (st && st.isFile()) {
    serveFile(req, res, target, rel);
    return;
  }

  // 没命中 → Next 导出的 404 页（和 core 上 html=True 的行为一致）
  const notFound = path.join(rootResolved, '404.html');
  if (await statOrNull(notFound)) {
    serveFile(req, res, notFound, '404.html', 404);
    return;
  }
  sendText(res, 404, '不存在\n');
}

/**
 * 起服务。
 * @param {{ webRoot: string, getCoreOrigin: () => string }} opts
 * @returns {Promise<{ server: import('node:http').Server, port: number, origin: string }>}
 */
function startServer({ webRoot, getCoreOrigin }) {
  const server = http.createServer((req, res) => {
    // 绝对形式 URI（`GET http://host/path`）只有真代理才该接受，这里一律拒
    if (!req.url || !req.url.startsWith('/')) {
      sendText(res, 400, '请求 URL 非法\n');
      return;
    }
    const pathname = req.url.split('?')[0];
    if (isProxyPath(pathname)) {
      proxy(req, res, getCoreOrigin(), pathname);
      return;
    }
    serveStatic(req, res, webRoot, pathname).catch(() => {
      if (!res.headersSent) sendText(res, 500, '内部错误\n');
      else res.destroy();
    });
  });

  // 长响应（大媒体、慢生成）不能被壳自己掐断
  server.timeout = 0;
  server.requestTimeout = 0;
  server.headersTimeout = 0;
  server.keepAliveTimeout = 72_000;

  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const addr = server.address();
      const port = typeof addr === 'object' && addr ? addr.port : 0;
      resolve({ server, port, origin: `http://127.0.0.1:${port}` });
    });
  });
}

module.exports = { BASE_PATH, PROXY_PREFIXES, startServer, isProxyPath };
