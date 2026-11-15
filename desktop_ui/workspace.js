import { renderAppearance } from './appearance.js';
import { renderCatalog } from './catalog.js';
import { renderDeveloper } from './developer.js';

const renderers = { appearance: renderAppearance, catalog: renderCatalog, developer: renderDeveloper };
const workspace = document.getElementById('workspace');
const navigation = document.querySelector('nav');
const request = (method, payload = {}) => window.workspace.request(method, payload);
request.request = request;
const memories = { appearance: {}, catalog: {}, developer: {} };
const entries = [];
let cursor = -1;
let current = '';
let controller;
let version = 0;

function capture() {
  if (controller?.snapshot) entries[cursor].catalog = controller.snapshot();
}

function updateHistoryButtons() {
  navigation.querySelector('[data-history="-1"]').disabled = cursor <= 0;
  navigation.querySelector('[data-history="1"]').disabled = cursor >= entries.length - 1;
}

function visit(entry) {
  capture();
  entries.splice(cursor + 1, entries.length, entry);
  if (entries.length > 80) entries.shift();
  cursor = entries.length - 1;
  show(entry);
}

function select(view) {
  if (Object.hasOwn(renderers, view) && current !== view) visit({view});
}

function travel(direction) {
  if ((direction !== -1 && direction !== 1) || workspace.querySelector('dialog[open]')) return;
  const target = cursor + direction;
  if (!entries[target]) return;
  capture();
  cursor = target;
  show(entries[cursor]);
}

async function show({view, catalog}) {
  updateHistoryButtons();
  if (current === view && controller?.restore) {
    controller.restore(catalog);
    return;
  }
  const serial = ++version;
  controller?.dispose?.();
  controller = undefined;
  if (current === 'appearance') request('appearance.closePreview').catch(() => {});
  if (current === 'developer') request('developer.leave').catch(() => {});
  current = view;
  for (const button of navigation.querySelectorAll('[data-view]')) {
    if (button.dataset.view === view) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  }
  const panel = document.createElement('div');
  workspace.replaceChildren(panel);
  try {
    let next = await renderers[view](panel, request, memories[view], {
      initial: catalog,
      visit: route => { if (serial === version) visit({view, catalog:route}); },
    });
    if (typeof next === 'function') next = {dispose: next};
    if (serial === version) controller = next;
    else next?.dispose?.();
  } catch (error) {
    if (serial !== version) return;
    panel.className = 'workspace-error';
    panel.textContent = error.message;
  }
}

navigation.addEventListener('click', event => {
  const arrow = event.target.closest('[data-history]');
  if (arrow) { travel(Number(arrow.dataset.history)); return; }
  const button = event.target.closest('button[data-view]');
  if (button) select(button.dataset.view);
});
document.addEventListener('keydown', event => {
  if (!event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
  if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
  event.preventDefault();
  travel(event.key === 'ArrowLeft' ? -1 : 1);
});
window.workspace.onHistory(travel);
window.workspace.onView(select);
select(window.workspace.initialView);
