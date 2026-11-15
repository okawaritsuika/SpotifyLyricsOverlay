const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function overlaySource(filename) {
  return readFileSync(path.join(__dirname, '../desktop_ui', filename), 'utf8');
}

function electronFixture() {
  const requests = [];
  const channel = new EventEmitter();
  channel.write = text => requests.push(JSON.parse(text));
  const replies = new EventEmitter();
  const ipc = new Map();
  const ipcMain = { on: (name, callback) => ipc.set(name, callback),
    handle: (name, callback) => ipc.set(name, callback) };
  const app = Object.assign(new EventEmitter(), { setName() {}, setAppUserModelId() {},
    whenReady: () => new Promise(() => {}) });
  let bounds = { x: 10, y: 20, width: 500, height: 300 };
  const frame = { url: 'overlay-ui://workspace/desktop_ui/overlay.html' };
  const window = { webContents: { mainFrame: frame, isLoadingMainFrame: () => false, send() {} },
    isDestroyed: () => false, isVisible: () => true, setIgnoreMouseEvents() {},
    getBounds: () => bounds, setBounds: next => { bounds = next; } };
  const menus = [];
  const Menu = { buildFromTemplate: items => ({ items,
    popup(options) { this.options = options; menus.push(this); },
    closePopup() { this.options.callback(); } }) };
  const electron = { app, ipcMain, Menu, protocol: { registerSchemesAsPrivileged() {} },
    screen: { dipToScreenRect: (_, rect) => rect, screenToDipRect: (_, rect) => rect } };
  const modules = { electron, 'node:net': { createConnection: () => channel },
    'node:readline': { createInterface: () => replies } };
  vm.runInNewContext(overlaySource('main.cjs') + `
    overlay = testWindow;
    overlayState = {visible:true, bounds:overlay.getBounds()};
    applyOverlayBounds(overlayState.bounds);
  `, {
    require: name => modules[name] ?? require(name), console,
    process: { argv: ['electron', 'desktop_ui'], env: {
      SPOTIFY_OVERLAY_WORKSPACE_PORT: '5555', SPOTIFY_OVERLAY_WORKSPACE_SECRET: 'a'.repeat(64),
    } },
    setTimeout: () => 0, clearTimeout() {}, testWindow: window,
  });
  const event = { sender: window.webContents, senderFrame: frame };
  const receive = message => replies.emit('line', JSON.stringify(message));
  return { requests, menus, getBounds: window.getBounds,
    resize: (phase, x, y) => ipc.get('overlay:resize')(event, phase, 'move', x, y),
    action: (name, payload) => ipc.get('overlay:action')(event, name, payload),
    update: bounds => receive({ overlay: { visible: true, bounds } }),
    reply: (request, result) => receive({ id: request.id, result }),
  };
}

test('a second drag moves immediately, even while the first position is being saved', async () => {
  const overlay = electronFixture();
  await overlay.resize('start', 50, 100);
  await overlay.resize('move', 80, 100);
  const originalSave = overlay.resize('end');
  const first = overlay.requests[0];
  assert.equal(first.payload.bounds.x, 40);

  await overlay.resize('start', 90, 100);
  await overlay.resize('move', 140, 100);
  assert.equal(overlay.getBounds().x, 90);
  overlay.reply(first, { ...first.payload.bounds, x: 41 });
  await originalSave;
  assert.equal(overlay.getBounds().x, 90);

  const latestSave = overlay.resize('end');
  const latest = overlay.requests[1];
  assert.equal(latest.payload.bounds.x, 90);
  overlay.update({ ...first.payload.bounds, x: 41 });
  assert.equal(overlay.getBounds().x, 90);
  const accepted = { ...latest.payload.bounds, x: 91 };
  overlay.reply(latest, accepted);
  await latestSave;
  overlay.update(accepted);
  assert.equal(overlay.getBounds().x, 91);
});

test('an intervening click leaves the accepted position intact and sends no extra bounds request', async () => {
  for (const replyWhilePressed of [false, true]) {
    const overlay = electronFixture();
    await overlay.resize('start', 50, 100);
    await overlay.resize('move', 80, 100);
    const pending = overlay.resize('end');
    const first = overlay.requests[0];
    const accepted = { ...first.payload.bounds, x: 41 };

    await overlay.resize('start', 90, 100);
    if (replyWhilePressed) {
      overlay.reply(first, accepted);
      await pending;
      assert.equal(overlay.getBounds().x, 40);
    }
    await overlay.resize('end');
    if (!replyWhilePressed) overlay.reply(first, accepted);
    await pending;
    assert.equal(overlay.requests.length, 1);
    assert.equal(overlay.getBounds().x, 41);
  }
});

test('starting a drag cancels a pending overlay menu and a dismissed menu can reopen', async () => {
  const overlay = electronFixture();
  const items = [{ label: '헤더 숨기기 / 복원' }, null, { label: 'Spotify와 함께 시작 / 종료', checked: true }];
  const delayed = overlay.action('menu', {});
  await overlay.resize('start', 80, 100);
  overlay.reply(overlay.requests[0], items);
  assert.equal(await delayed, false);
  assert.equal(overlay.menus.length, 0);
  await overlay.resize('end');

  const first = overlay.action('menu', {});
  overlay.reply(overlay.requests[1], items);
  assert.equal(await first, true);
  assert.equal(overlay.menus[0].items[2].checked, true);
  overlay.menus[0].options.callback();
  const second = overlay.action('menu', { x: -99999, y: -99999 });
  overlay.reply(overlay.requests[2], items);
  assert.equal(await second, true);
  assert.equal('x' in overlay.menus[1].options, false);
  assert.equal('y' in overlay.menus[1].options, false);
  overlay.menus[1].items[2].click();
  assert.equal(overlay.requests[3].method, 'overlay.menuSelect');
  assert.equal(overlay.requests[3].payload.index, 2);
  overlay.reply(overlay.requests[3], true);
});

test('pointer release and cancellation each end a drag immediately and only once', () => {
  const events = new Map();
  const calls = [];
  const dragTarget = { querySelectorAll: () => [], setPointerCapture() {}, closest: () => null };
  const document = { querySelector: () => dragTarget, body: { append() {} },
    createElement: () => ({ dataset: {}, setAttribute() {} }),
    addEventListener: (name, callback) => events.set(name, callback) };
  const window = { addEventListener() {}, lyricOverlay: { onState() {},
    resize: phase => calls.push(phase) } };
  vm.runInNewContext(overlaySource('overlay.js') + '\nstate = {locked:false};', { document, window });

  const pointer = { pointerId: 1, button: 0, target: dragTarget, screenX: 50, screenY: 50 };
  events.get('pointerdown')(pointer);
  events.get('pointerup')(pointer);
  events.get('pointerdown')(pointer);
  events.get('pointercancel')(pointer);
  events.get('lostpointercapture')(pointer);
  assert.deepEqual(calls, ['start', 'end', 'start', 'end']);
});
