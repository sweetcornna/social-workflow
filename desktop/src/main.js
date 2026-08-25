'use strict';

/**
 * social_workflow 工作台桌面壳（薄壳）。
 *
 * 壳只负责三件事：起内嵌服务（静态 + 反代）、开一个窗口、给一个改 core 地址的口子。
 * **不做业务**：core 连不上时不弹窗不换页，工作台自己有离线态，壳保持哑。
 */

const path = require('node:path');
const { app, BrowserWindow, Menu, dialog, ipcMain, nativeTheme, shell } = require('electron');

const {
  DEFAULT_CORE_ORIGIN,
  configPath,
  envCoreOrigin,
  normalizeOrigin,
  resolveCoreOrigin,
  storedCoreOrigin,
  writeCoreOrigin,
} = require('./config');
const { BASE_PATH, startServer } = require('./server');

const APP_TITLE = 'social_workflow 工作台';
/** Organic 底色：奶油 / 暖炭。开窗那一下别闪白。 */
const CREAM = '#f5ead8';
const CHARCOAL = '#201e1d';

/** 打包后静态产物在 resources/workbench；dev 直接吃 ui/out。 */
const WEB_ROOT = app.isPackaged
  ? path.join(process.resourcesPath, 'workbench')
  : path.resolve(__dirname, '..', '..', 'ui', 'out');

let mainWindow = null;
let promptWindow = null;
let localOrigin = '';
let coreOrigin = DEFAULT_CORE_ORIGIN;

function currentCoreOrigin() {
  return coreOrigin;
}

function isInternalUrl(url) {
  return Boolean(localOrigin) && url.startsWith(`${localOrigin}/`);
}

function openExternal(url) {
  if (/^https?:\/\//i.test(url)) void shell.openExternal(url);
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1024,
    minHeight: 640,
    title: APP_TITLE,
    backgroundColor: nativeTheme.shouldUseDarkColors ? CHARCOAL : CREAM,
    autoHideMenuBar: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      spellcheck: false,
    },
  });

  // Next 会把 document.title 写成页面标题；桌面版窗口标题固定成产品名
  mainWindow.webContents.on('page-title-updated', (event) => {
    event.preventDefault();
    mainWindow.setTitle(APP_TITLE);
  });

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    openExternal(url);
    return { action: 'deny' };
  });
  mainWindow.webContents.on('will-navigate', (event, url) => {
    if (!isInternalUrl(url)) {
      event.preventDefault();
      openExternal(url);
    }
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });

  void mainWindow.loadURL(`${localOrigin}${BASE_PATH}/`);
}

/** 改完地址整页重载（不是 reload()）：SWR 缓存也一起丢掉，免得看见上一台 core 的数。 */
function reloadWorkbench() {
  if (mainWindow) void mainWindow.loadURL(`${localOrigin}${BASE_PATH}/`);
}

// ------------------------------------------------------------ 设置 core 地址

function openCoreOriginPrompt() {
  if (promptWindow) {
    promptWindow.focus();
    return;
  }
  promptWindow = new BrowserWindow({
    width: 560,
    // useContentSize：给的是网页区高度，别让标题栏把按钮挤出可视区。
    // 有 env 覆盖时多一块提示条，高度跟着涨。
    height: envCoreOrigin() ? 400 : 336,
    useContentSize: true,
    parent: mainWindow ?? undefined,
    modal: Boolean(mainWindow),
    resizable: false,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    title: '设置 core 地址',
    backgroundColor: nativeTheme.shouldUseDarkColors ? CHARCOAL : CREAM,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      preload: path.join(__dirname, 'prompt-preload.js'),
    },
  });
  promptWindow.setMenuBarVisibility(false);
  promptWindow.on('closed', () => {
    promptWindow = null;
  });
  void promptWindow.loadFile(path.join(__dirname, 'prompt.html'));
}

function fromPrompt(event) {
  return promptWindow && event.sender === promptWindow.webContents;
}

ipcMain.handle('core-origin:state', (event) => {
  if (!fromPrompt(event)) return null;
  const env = envCoreOrigin();
  return {
    effective: currentCoreOrigin(),
    stored: storedCoreOrigin(app) || '',
    envOverride: env || '',
    defaultOrigin: DEFAULT_CORE_ORIGIN,
    configFile: configPath(app),
  };
});

ipcMain.handle('core-origin:save', (event, value) => {
  if (!fromPrompt(event)) return { ok: false, message: '非法来源' };
  const next = normalizeOrigin(value);
  if (!next) {
    return { ok: false, message: '地址不合法。示例：http://127.0.0.1:18000' };
  }
  try {
    writeCoreOrigin(app, next);
  } catch (err) {
    return { ok: false, message: `写配置失败：${err && err.message ? err.message : err}` };
  }
  coreOrigin = resolveCoreOrigin(app);
  if (promptWindow) promptWindow.close();
  reloadWorkbench();
  return { ok: true, effective: coreOrigin };
});

ipcMain.handle('core-origin:cancel', (event) => {
  if (fromPrompt(event) && promptWindow) promptWindow.close();
  return null;
});

// ---------------------------------------------------------------------- 菜单

function buildMenu() {
  const isMac = process.platform === 'darwin';
  const settingsItem = {
    // id 是给冒烟脚本用的：scripts/smoke.mjs 靠它触发真实的菜单回调
    id: 'set-core-origin',
    label: '设置 core 地址…',
    accelerator: 'CmdOrCtrl+,',
    click: openCoreOriginPrompt,
  };

  /** @type {Electron.MenuItemConstructorOptions[]} */
  const template = [];

  if (isMac) {
    template.push({
      label: APP_TITLE,
      submenu: [
        { label: `关于 ${APP_TITLE}`, role: 'about' },
        { type: 'separator' },
        settingsItem,
        { type: 'separator' },
        { label: '服务', role: 'services' },
        { type: 'separator' },
        { label: `隐藏 ${APP_TITLE}`, role: 'hide' },
        { label: '隐藏其他', role: 'hideOthers' },
        { label: '全部显示', role: 'unhide' },
        { type: 'separator' },
        { label: '退出', role: 'quit' },
      ],
    });
  } else {
    template.push({
      label: '文件',
      submenu: [settingsItem, { type: 'separator' }, { label: '退出', role: 'quit' }],
    });
  }

  template.push({
    label: '编辑',
    submenu: [
      { label: '撤销', role: 'undo' },
      { label: '重做', role: 'redo' },
      { type: 'separator' },
      { label: '剪切', role: 'cut' },
      { label: '拷贝', role: 'copy' },
      { label: '粘贴', role: 'paste' },
      { label: '全选', role: 'selectAll' },
    ],
  });

  template.push({
    label: '视图',
    submenu: [
      { label: '重新加载', accelerator: 'CmdOrCtrl+R', click: reloadWorkbench },
      { label: '开发者工具', role: 'toggleDevTools' },
      { type: 'separator' },
      { label: '实际大小', role: 'resetZoom' },
      { label: '放大', role: 'zoomIn' },
      { label: '缩小', role: 'zoomOut' },
      { type: 'separator' },
      { label: '全屏', role: 'togglefullscreen' },
    ],
  });

  template.push({
    label: '窗口',
    submenu: isMac
      ? [
          { label: '最小化', role: 'minimize' },
          { label: '缩放', role: 'zoom' },
          { type: 'separator' },
          { label: '前置全部窗口', role: 'front' },
        ]
      : [
          { label: '最小化', role: 'minimize' },
          { label: '关闭', role: 'close' },
        ],
  });

  template.push({
    label: '帮助',
    submenu: [
      {
        label: '当前 core 地址…',
        click: () => {
          const env = envCoreOrigin();
          void dialog.showMessageBox(mainWindow ?? undefined, {
            type: 'info',
            title: '当前 core 地址',
            message: currentCoreOrigin(),
            detail: [
              env ? `来源：环境变量 SW_CORE_ORIGIN（覆盖配置文件）` : '来源：配置文件 / 默认值',
              `配置文件：${configPath(app)}`,
              `壳内嵌服务：${localOrigin}`,
            ].join('\n'),
            buttons: ['好'],
          });
        },
      },
      {
        label: '项目主页',
        click: () => openExternal('https://github.com/sweetcornna/social-workflow'),
      },
    ],
  });

  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// -------------------------------------------------------------------- 生命周期

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (!mainWindow) return;
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.focus();
  });

  app.whenReady().then(async () => {
    coreOrigin = resolveCoreOrigin(app);
    try {
      const started = await startServer({ webRoot: WEB_ROOT, getCoreOrigin: currentCoreOrigin });
      localOrigin = started.origin;
    } catch (err) {
      dialog.showErrorBox('启动失败', `内嵌服务起不来：${err && err.message ? err.message : err}`);
      app.quit();
      return;
    }
    buildMenu();
    createWindow();

    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
  });

  app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') app.quit();
  });

  // 兜底：任何 webContents 都不许开新窗口 / 跳外站
  app.on('web-contents-created', (_event, contents) => {
    contents.setWindowOpenHandler(({ url }) => {
      openExternal(url);
      return { action: 'deny' };
    });
  });
}
