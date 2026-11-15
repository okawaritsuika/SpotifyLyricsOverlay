import { drawLyricPreview } from './appearance.js';

const preview = document.querySelector('.appearance-preview');
const shell = document.querySelector('.preview-overlay');
const tools = document.querySelector('.preview-tools');
let draft, overlay;

function layout() {
  if (!draft) return;
  const width = preview.clientWidth;
  const policy = draft.compact_layout;
  const priorities = policy?.priorities || {};
  const stages = [...new Set(Object.values(priorities).filter(Boolean))].sort((a,b) => a-b);
  const autoHide = policy?.mode === 'auto_hide' && stages.length;
  const fields = ['title','artists','singers','reading','original','korean'];
  const lines = Object.fromEntries(fields.map(key => [key,preview.querySelector(`.appearance-preview-${key}`)]));
  const metadata = preview.querySelector('.preview-metadata');
  const current = preview.querySelector('.appearance-preview-current');
  const singers = lines.singers;
  const requiredHeight = () => (metadata.hidden ? 0 : metadata.offsetHeight+8) + Math.max(current.offsetHeight,singers?.offsetHeight || 0)+10;
  const size = scale => {
    metadata.hidden = !fields.slice(0,2).some(key => lines[key] && !lines[key].hidden);
    preview.style.setProperty('--preview-scale', scale);
    preview.style.setProperty('--preview-gutter', singers && !singers.hidden ? `${Math.round(132*scale)}px` : '0px');
  };
  for (const line of Object.values(lines)) if (line) line.hidden = false;
  tools.hidden = overlay?.borderless !== false || width < 760;
  size(1);
  if (requiredHeight() > preview.clientHeight) tools.hidden = true;
  if (autoHide) {
    const thresholds = [760,540,440,360,300,260];
    for (const [index,priority] of stages.entries()) {
      if (width >= thresholds[index] && requiredHeight() <= preview.clientHeight) break;
      for (const key of fields) if (lines[key] && priorities[key] === priority) lines[key].hidden = true;
      size(1);
    }
  }
  let scale = Math.min(1,width/(autoHide ? 440 : 760));
  size(scale);
  while (scale > .05 && requiredHeight() > preview.clientHeight) {
    scale = Math.max(.05,scale-.025);
    size(scale);
  }
  const ink = Array.from(current.querySelectorAll('div:not([hidden]) .appearance-preview-ink'));
  const longest = Math.max(0,...ink.map(el => el.getBoundingClientRect().width));
  const sideWidth = Math.min(172,Math.floor((width-44-longest-24)/2));
  preview.style.setProperty('--preview-side-width', `${Math.max(0,sideWidth)}px`);
  for (const key of ['previous','next']) {
    const line = preview.querySelector(`.appearance-preview-${key}`);
    if (line) line.hidden = width < 500 || sideWidth < 42;
  }
}

window.lyricPreview.onDraft((next,chrome) => {
  draft = next;
  if (overlay !== chrome) {
    overlay = chrome;
    for (const group of document.querySelectorAll('[data-transport]')) {
      group.replaceChildren(...group.dataset.transport.split(' ').map(kind => {
        const button = document.createElement('span');
        button.className = 'preview-symbol';
        if (overlay?.icons?.[kind]) {
          const icon = document.createElement('img');
          icon.src = `data:image/png;base64,${overlay.icons[kind]}`;
          icon.alt = '';
          button.append(icon);
        }
        return button;
      }));
    }
  }
  drawLyricPreview(preview,draft,{overlay:true});
  shell.style.backgroundColor = preview.style.backgroundColor;
  preview.style.removeProperty('background-color');
  shell.style.setProperty('--preview-text-opacity', draft.opacity);
  layout();
});
new ResizeObserver(layout).observe(shell);
document.fonts.addEventListener('loadingdone', layout);

for (const edge of ['n','ne','e','se','s','sw','w','nw']) {
  const grip = document.createElement('div');
  grip.className = 'preview-grip';
  grip.dataset.edge = edge;
  let pointer;
  grip.addEventListener('pointerdown', event => {
    if (event.button !== 0) return;
    pointer = event.pointerId;
    grip.setPointerCapture(pointer);
    window.lyricPreview.resize('start',edge,event.screenX,event.screenY);
  });
  grip.addEventListener('pointermove', event => {
    if (pointer === event.pointerId) window.lyricPreview.resize('move',edge,event.screenX,event.screenY);
  });
  grip.addEventListener('lostpointercapture', () => {
    pointer = undefined;
    window.lyricPreview.resize('end',edge);
  });
  document.body.append(grip);
}
document.querySelector('button').addEventListener('click', () => window.close());
document.addEventListener('keydown', event => { if (event.key === 'Escape') window.close(); });
