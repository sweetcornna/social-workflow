'use strict';

/**
 * 「设置 core 地址」小窗的 preload。
 * 只桥三个方法出去，页面拿不到 node、也拿不到 ipcRenderer 本体。
 */

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('swPrompt', {
  state: () => ipcRenderer.invoke('core-origin:state'),
  save: (value) => ipcRenderer.invoke('core-origin:save', String(value ?? '')),
  cancel: () => ipcRenderer.invoke('core-origin:cancel'),
});
