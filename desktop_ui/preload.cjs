const { contextBridge, ipcRenderer } = require('electron');

const initial = process.argv.find(value => value.startsWith('--workspace-view='));
contextBridge.exposeInMainWorld('workspace', {
  initialView: initial?.split('=')[1] || 'appearance',
  request: (method, payload) => ipcRenderer.invoke('workspace:request', method, payload),
  onView: callback => ipcRenderer.on('workspace:view', (_event, view) => callback(view)),
  onHistory: callback => ipcRenderer.on('workspace:history', (_event, direction) => callback(direction)),
  onDeveloperPlayback: callback => {
    const listener = (_event, playback) => callback(playback);
    ipcRenderer.on('workspace:developerPlayback', listener);
    return () => ipcRenderer.removeListener('workspace:developerPlayback', listener);
  },
});
