const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('lyricPreview', {
  onDraft: callback => ipcRenderer.on('preview:draft', (_event,draft,overlay) => callback(draft,overlay)),
  resize: (phase,edge,x,y) => ipcRenderer.send('preview:resize',phase,edge,x,y),
});
