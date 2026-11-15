const { app, BrowserWindow, ipcMain, nativeImage, protocol, screen, session } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const readline = require('node:readline');
const net = require('node:net');

if (process.env.SPOTIFY_OVERLAY_WORKSPACE_PROFILE) app.setPath('userData',process.env.SPOTIFY_OVERLAY_WORKSPACE_PROFILE);
const homeURL = 'overlay-ui://workspace/desktop_ui/index.html';
const previewURL = 'overlay-ui://workspace/desktop_ui/preview.html';
const overlayURL = 'overlay-ui://workspace/desktop_ui/overlay.html';
protocol.registerSchemesAsPrivileged([{ scheme: 'overlay-ui', privileges: { standard: true, secure: true, supportFetchAPI: true, corsEnabled: true } }]);
const views = new Set(['appearance', 'catalog', 'developer']);
const actions = new Set([
  'appearance.get', 'appearance.apply', 'appearance.preview', 'appearance.closePreview',
  'appearance.presetLoad', 'appearance.presetSave', 'appearance.reset',
  'catalog.list', 'catalog.detail', 'catalog.play', 'catalog.favorite', 'catalog.people',
  'catalog.albums', 'catalog.album',
  'catalog.person', 'catalog.lives', 'catalog.live', 'catalog.locals', 'catalog.local',
  'catalog.localOpen', 'catalog.localLink', 'catalog.localUnlink', 'catalog.current',
  'catalog.source', 'catalog.localScan', 'catalog.localStatus', 'catalog.spotifyLink',
  'catalog.spotifyUnlink', 'catalog.spotifyOpen',
  'catalog.updates', 'catalog.addSong', 'catalog.cancel',
  'catalog.discover', 'catalog.spotifySearch', 'catalog.spotifyCandidates',
  'developer.get', 'developer.position', 'developer.select', 'developer.record', 'developer.clear', 'developer.saveLine',
  'developer.transport', 'developer.lineTool',
  'developer.step', 'developer.seek', 'developer.setAuto', 'developer.offset', 'developer.lead',
  'developer.open', 'developer.leave', 'developer.setRealtimeTracking', 'developer.compact',
]);
const pending = new Map();
const overlayActions = new Set(['appearance','lock','chrome','menu','previous','first','play','volume','last','next','key','hide','close']);
const resizeEdges = new Set(['n','ne','e','se','s','sw','w','nw']);
let window;
let workspaceLoaded = false;
let overlay;
let overlayState;
let overlayFailed = false;
let keepOverlay = process.argv[2] === 'overlay';
let quitting = false;
let preview;
let previewBounds;
let fullWorkspace;
let sequence = 0;
let view = views.has(process.argv[2]) ? process.argv[2] : null;
const port = Number(process.env.SPOTIFY_OVERLAY_WORKSPACE_PORT);
const secret = process.env.SPOTIFY_OVERLAY_WORKSPACE_SECRET;
delete process.env.SPOTIFY_OVERLAY_WORKSPACE_SECRET;
if (!Number.isInteger(port) || port < 1 || port > 65535 || !/^[a-f0-9]{64}$/.test(secret)) {
  app.exit(1);
}
const channel = net.createConnection({ host: '127.0.0.1', port });
const send = data => channel.write(JSON.stringify(data) + '\n');
channel.on('connect', () => channel.write(secret + '\n'));
channel.on('error', () => app.exit(1));
channel.on('close', () => app.quit());

function show(next) {
  view = next;
  if (!window) {
    createWorkspace();
    return;
  }
  if (window.isDestroyed() || !workspaceLoaded) return;
  if (window.isMinimized()) window.restore();
  window.webContents.send('workspace:view', next);
  window.show();
  window.focus();
  send({workspaceShown: true});
}

function requestPython(method, payload) {
  return new Promise((resolve, reject) => {
    const id = ++sequence;
    const timeout = setTimeout(() => {
      pending.delete(id);
      reject(new Error('가사 앱의 응답 시간이 초과되었습니다.'));
    }, 18000);
    pending.set(id, {resolve, reject, timeout});
    send({id, method, payload});
  });
}

function closePreview() {
  preview?.window.destroy();
  return true;
}

function setDeveloperCompact(compact) {
  if (compact && !fullWorkspace) {
    fullWorkspace = { bounds: window.getNormalBounds(), maximized: window.isMaximized() };
    const area = screen.getDisplayMatching(window.getBounds()).workArea;
    if (fullWorkspace.maximized) window.unmaximize();
    window.setMinimumSize(360,300);
    const width = Math.min(520, area.width), height = Math.min(540, area.height);
    const {x,y} = fullWorkspace.bounds;
    window.setBounds({x:Math.max(area.x,Math.min(x,area.x+area.width-width)),
      y:Math.max(area.y,Math.min(y,area.y+area.height-height)),width,height});
  } else if (!compact && fullWorkspace) {
    const {bounds,maximized} = fullWorkspace;
    fullWorkspace = undefined;
    if (window.isMaximized()) window.unmaximize();
    window.setMinimumSize(640,460);
    window.setBounds(bounds);
    if (maximized) window.maximize();
  }
  return !!fullWorkspace;
}

function isOverlaySender(event) {
  return overlay && !overlay.isDestroyed() && event.sender === overlay.webContents &&
    event.senderFrame === overlay.webContents.mainFrame && event.senderFrame.url === overlayURL;
}

function updateOverlay(state) {
  if (overlayFailed) return;
  overlayState = state;
  keepOverlay = true;
  if (!overlay) {
    const popup = new BrowserWindow({
      ...screen.screenToDipRect(null,state.bounds), title: '아이마스 가사 · Spotify 오버레이',
      show: false, frame: false, transparent: true, alwaysOnTop: true, hasShadow: false,
      resizable: false, skipTaskbar: true, fullscreenable: false,
      icon: nativeImage.createFromPath(path.join(__dirname,'..','resources','app.ico')),
      webPreferences: {preload:path.join(__dirname,'overlay.cjs'), contextIsolation:true,
        nodeIntegration:false, sandbox:true, backgroundThrottling:false},
    });
    overlay = popup;
    popup.removeMenu();
    popup.on('close',event => {
      if (quitting) return;
      event.preventDefault();
      requestPython('overlay.close',{}).catch(error => console.error(error.message));
    });
    popup.webContents.setWindowOpenHandler(() => ({action:'deny'}));
    popup.webContents.on('will-navigate',event => event.preventDefault());
    popup.webContents.once('render-process-gone', () => { if (!popup.isDestroyed()) popup.destroy(); });
    popup.once('closed', () => {
      overlay = undefined;
      overlayFailed = true;
      keepOverlay = false;
      send({overlayFailed:true});
    });
    popup.loadURL(overlayURL).then(() => {
      if (overlay === popup) drawOverlay();
    }).catch(error => {
      console.error(error.message);
      if (!popup.isDestroyed()) popup.destroy();
    });
  } else if (!overlay.webContents.isLoadingMainFrame()) {
    drawOverlay();
  }
}

let overlayDrag;
let savingOverlayBounds = false;
let appliedOverlayBounds;

function sameBounds(first, second) {
  return first && ['x','y','width','height'].every(key => first[key] === second[key]);
}

function applyOverlayBounds(bounds) {
  if (!overlay || overlay.isDestroyed()) return;
  if (sameBounds(appliedOverlayBounds,bounds)) return;
  overlay.setBounds(screen.screenToDipRect(null,bounds));
  appliedOverlayBounds = bounds;
}

function drawOverlay() {
  if (!overlayDrag && !savingOverlayBounds) applyOverlayBounds(overlayState.bounds);
  overlay.setIgnoreMouseEvents(overlayState.locked === true);
  overlay.webContents.send('overlay:state',overlayState);
  if (overlayState.visible && !overlay.isVisible()) overlay.showInactive();
  if (!overlayState.visible && overlay.isVisible()) overlay.hide();
}

ipcMain.on('overlay:ready', event => {
  if (!isOverlaySender(event)) return;
  const handle = overlay.getNativeWindowHandle();
  const hwnd = handle.length === 8 ? handle.readBigUInt64LE().toString() : String(handle.readUInt32LE());
  send({overlayReady:true,hwnd});
});

ipcMain.handle('overlay:action', (event, action, payload) => {
  if (!isOverlaySender(event) || !overlayActions.has(action) || !payload ||
      typeof payload !== 'object' || Array.isArray(payload)) throw new Error('지원하지 않는 가사창 동작입니다.');
  if (action === 'menu') {
    if (!Number.isInteger(payload.x) || !Number.isInteger(payload.y)) throw new Error('메뉴 위치를 확인하세요.');
    payload = screen.dipToScreenPoint(payload);
  }
  return requestPython(`overlay.${action}`,payload);
});

function draggedBounds(drag, x, y, minimumWidth, minimumHeight) {
  const dx = Math.round(x-drag.x), dy = Math.round(y-drag.y);
  const {edge,bounds} = drag;
  if (edge === 'move') return {...bounds,x:bounds.x+dx,y:bounds.y+dy};
  const west = edge.includes('w'), north = edge.includes('n');
  const width = Math.min(3840,Math.max(minimumWidth,bounds.width+(west ? -dx : edge.includes('e') ? dx : 0)));
  const height = Math.min(2160,Math.max(minimumHeight,bounds.height+(north ? -dy : edge.includes('s') ? dy : 0)));
  return {x:bounds.x+(west ? bounds.width-width : 0),y:bounds.y+(north ? bounds.height-height : 0),width,height};
}

ipcMain.on('overlay:resize', async (event, phase, edge, x, y) => {
  if (!isOverlaySender(event) || overlayState?.locked || savingOverlayBounds) return;
  if (phase === 'end' && overlayDrag) {
    const bounds = screen.dipToScreenRect(null,overlay.getBounds());
    const resize = overlayDrag.edge !== 'move';
    overlayDrag = undefined;
    if (sameBounds(appliedOverlayBounds,bounds)) return;
    savingOverlayBounds = true;
    appliedOverlayBounds = undefined;
    try {
      const accepted = await requestPython('overlay.bounds',{bounds,resize});
      if (accepted) applyOverlayBounds(accepted);
    } catch (error) {
      console.error(error.message);
      applyOverlayBounds(overlayState.bounds);
    } finally {
      savingOverlayBounds = false;
    }
    return;
  }
  if (!(Number.isFinite(x) && Number.isFinite(y))) return;
  if (phase === 'start' && (edge === 'move' || resizeEdges.has(edge))) {
    overlayDrag = {edge,x,y,bounds:overlay.getBounds()};
  } else if (phase === 'move' && overlayDrag) {
    const [width,height] = overlayState.minSize ?? [240,100];
    const minimum = screen.screenToDipRect(overlay,{x:0,y:0,width,height});
    overlay.setBounds(draggedBounds(overlayDrag,x,y,minimum.width,minimum.height));
  }
});

async function updatePreview({draft, overlay, show = true}) {
  if (!draft || typeof draft !== 'object' || !draft.text_styles) return false;
  if (!preview && !show) return false;
  if (!preview) {
    const area = screen.getDisplayMatching(window.getBounds()).workArea;
    const width = Math.min(Math.max(240,Number(overlay?.width) || 850),area.width - 32);
    const height = Math.min(Math.max(100,Number(overlay?.height) || 260),area.height - 32);
    const popup = new BrowserWindow({
      x: area.x + Math.round((area.width - width) / 2), y: area.y + area.height - height - 24,
      width, height, ...previewBounds, title: '가사 미리보기', parent: window, show: false, transparent: true,
      frame: false, resizable: false, alwaysOnTop: true, skipTaskbar: true, hasShadow: false,
      webPreferences: {preload:path.join(__dirname,'preview.cjs'), contextIsolation:true, nodeIntegration:false, sandbox:true},
    });
    popup.removeMenu();
    popup.webContents.setWindowOpenHandler(() => ({action:'deny'}));
    popup.webContents.on('will-navigate',event => event.preventDefault());
    for (const event of ['move','resize']) popup.on(event, () => { previewBounds = popup.getBounds(); });
    popup.once('closed', () => { if (preview?.window === popup) preview = undefined; });
    popup.webContents.once('render-process-gone', () => { if (!popup.isDestroyed()) popup.destroy(); });
    preview = {window:popup, overlay, ready:popup.loadURL(previewURL)};
  }
  const current = preview;
  try { await current.ready; }
  catch (error) {
    if (current.window.isDestroyed()) return false;
    current.window.destroy();
    throw error;
  }
  if (current !== preview) return false;
  current.window.webContents.send('preview:draft', draft, current.overlay);
  if (show) current.window.showInactive();
  return true;
}

ipcMain.on('preview:resize', (event, phase, edge, x, y) => {
  if (!preview || event.sender !== preview.window.webContents || event.senderFrame !== event.sender.mainFrame
      || event.senderFrame.url !== previewURL) return;
  if (phase === 'end') { preview.drag = undefined; return; }
  if (!(Number.isFinite(x) && Number.isFinite(y))) return;
  if (phase === 'start' && resizeEdges.has(edge)) {
    preview.drag = {edge,x,y,bounds:preview.window.getBounds()};
    return;
  }
  if (phase !== 'move' || !preview.drag) return;
  preview.window.setBounds(draggedBounds(preview.drag,x,y,240,100));
});

readline.createInterface({ input: channel, crlfDelay: Infinity })
  .on('line', line => {
    let message;
    try { message = JSON.parse(line); } catch { return; }
    if (message.close) { app.quit(); return; }
    if (views.has(message.view)) { show(message.view); return; }
    if (message.activateOverlay) {
      if (overlay && !overlay.isDestroyed() && overlayState?.visible) overlay.showInactive();
      return;
    }
    if (message.overlay) updateOverlay(message.overlay);
    if (message.developerPlayback) {
      if (window && !window.isDestroyed()) window.webContents.send('workspace:developerPlayback', message.developerPlayback);
    }
    const waiter = pending.get(message.id);
    if (!waiter) return;
    pending.delete(message.id);
    clearTimeout(waiter.timeout);
    if (typeof message.error === 'string') waiter.reject(new Error(message.error));
    else waiter.resolve(message.result);
  });

ipcMain.handle('workspace:request', async (event, method, payload) => {
  if (event.sender !== window?.webContents || event.senderFrame !== window.webContents.mainFrame ||
      event.senderFrame.url !== homeURL || !actions.has(method) ||
      !payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new Error('지원하지 않는 작업입니다.');
  }
  if (method === 'appearance.preview') return updatePreview(payload);
  if (method === 'appearance.closePreview') return closePreview();
  if (method === 'developer.compact') {
    if (typeof payload.enabled !== 'boolean') throw new Error('가사 창 모드를 확인하세요.');
    return setDeveloperCompact(payload.enabled);
  }
  if (method === 'developer.leave') setDeveloperCompact(false);
  const result = await requestPython(method,payload);
  if (method === 'appearance.apply') closePreview();
  return result;
});

async function createWorkspace() {
  const work = new BrowserWindow({
    width: 1250, height: 840, minWidth: 640, minHeight: 460, show: false,
    backgroundColor: '#f6f6fa',
    titleBarStyle: 'hidden',
    titleBarOverlay: { color: '#f8f8fc', symbolColor: '#414656', height: 43 },
    icon: nativeImage.createFromPath(path.join(__dirname, '..', 'resources', 'app.ico')),
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true, nodeIntegration: false, sandbox: true, backgroundThrottling: false,
      additionalArguments: ['--workspace-view=' + view],
    },
  });
  window = work;
  workspaceLoaded = false;
  work.removeMenu();
  work.on('app-command', (_event, command) => {
    if (command === 'browser-backward' || command === 'browser-forward') {
      work.webContents.send('workspace:history', command === 'browser-backward' ? -1 : 1);
    }
  });
  work.once('closed', () => {
    fullWorkspace = undefined;
    window = undefined;
    workspaceLoaded = false;
    send({workspaceClosed:true});
  });
  work.webContents.setWindowOpenHandler(() => ({action:'deny'}));
  work.webContents.on('will-navigate', event => event.preventDefault());
  work.webContents.on('render-process-gone', (_event,detail) => {
    console.error('Workspace renderer exited',detail.reason);
    if (!work.isDestroyed()) {
      send({workspaceFailed:true});
      work.destroy();
    }
  });
  try {
    await work.loadURL(homeURL);
    if (window === work && !work.isDestroyed()) {
      workspaceLoaded = true;
      show(view);
    }
  } catch (error) {
    if (!work.isDestroyed()) {
      console.error(error.message);
      send({workspaceFailed:true});
      work.destroy();
    }
  }
}

app.setName('아이마스 가사 오버레이');
app.setAppUserModelId('SpotifyLyricsOverlay.Workspace');
app.whenReady().then(() => {
  const local = session.defaultSession;
  local.protocol.handle('overlay-ui', request => {
    const url = new URL(request.url);
    if (url.hostname !== 'workspace' || !/^\/(?:desktop_ui\/[a-z.]+\.(?:html|js|css)|resources\/(?:app\.png|series_symbols\/[a-z0-9_]+\.svg|fonts\/[a-z]+\.ttf))$/i.test(url.pathname)) {
      return new Response(null, { status: 404 });
    }
    const types = { '.html': 'text/html', '.css': 'text/css', '.js': 'text/javascript',
      '.svg': 'image/svg+xml', '.png': 'image/png', '.ttf': 'font/ttf' };
    try {
      const type = path.extname(url.pathname);
      const data = fs.readFileSync(path.join(__dirname, '..', url.pathname.slice(1)));
      const body = ['.png', '.ttf'].includes(type) ? new Uint8Array(data) : data.toString('utf8');
      return new Response(body, { headers: { 'Content-Type': types[type] } });
    } catch (error) {
      console.error(error.message);
      return new Response('화면 파일을 찾지 못했습니다.', { status: 404 });
    }
  });
  local.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  local.setPermissionCheckHandler(() => false);
  send({ ready: true });
  if (view) show(view);
}).catch(error => { console.error(error); app.exit(1); });
app.on('before-quit', () => { quitting = true; });
app.on('window-all-closed', () => { if (!keepOverlay) app.quit(); });
