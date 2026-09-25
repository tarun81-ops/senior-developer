// The page's only bridge to the shell (D26): one call that returns exactly
// { baseUrl, token }. No Node, no ipcRenderer, nothing else.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("sda", {
  connection: () => ipcRenderer.invoke("sda:connection"),
});
