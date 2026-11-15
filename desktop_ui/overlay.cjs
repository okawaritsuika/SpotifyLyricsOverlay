const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('lyricOverlay', {
  onState: callback => ipcRenderer.on('overlay:state', (_event, state) => callback(state)),
  ready: () => ipcRenderer.send('overlay:ready'),
  action: (action, payload = {}) => ipcRenderer.invoke('overlay:action', action, payload),
  resize: (phase, edge, x, y) => ipcRenderer.send('overlay:resize', phase, edge, x, y),
});
