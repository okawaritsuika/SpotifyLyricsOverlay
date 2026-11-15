const surface = document.querySelector('.overlay-surface');
const backdrop = document.querySelector('.overlay-backdrop');
const toolBackground = document.querySelector('.overlay-tool-background');
const singers = document.querySelector('.overlay-singers');
const controls = new Map(Array.from(surface.querySelectorAll('[data-action]'), el => [el.dataset.action, el]));
const labels = new Map(Array.from(surface.querySelectorAll('[data-label]'), el => {
  const ink = document.createElement('span');
  ink.className = 'overlay-ink';
  el.append(ink);
  return [el.dataset.label, {el, ink}];
}));
const descriptions = {
  appearance: '가사 표시 설정', lock: '클릭 통과', chrome: '조작줄을 접습니다. 우클릭 메뉴에서 복원할 수 있습니다.', menu: '더 보기',
  previous: '이전 곡', first: '현재 곡 처음으로 이동 · 재생 상태 유지', play: '재생', pause: '일시정지',
  volume: '음량 조절 / 음소거', last: '현재 곡 끝 1초 전으로 이동 · 재생 상태 유지', next: '다음 곡',
};
const outlineOffsets = [[-2,0],[2,0],[0,-2],[0,2],[-1,-1],[1,-1],[-1,1],[1,1]];
const resizeEdges = ['n','ne','e','se','s','sw','w','nw'];
const actionKeys = new Set(['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Enter','Delete','Backspace','Escape','Space','q','w','e','r',...'0123456789']);
const send = (action, payload) => window.lyricOverlay.action(action,payload).catch(error => console.error(error));
let state, singerSignature, drag;

function position(element, rect) {
  const [x,y,width,height] = rect;
  Object.assign(element.style,{left:`${x}px`, top:`${y}px`, width:`${width}px`, height:`${height}px`});
}

function setFont(element, font) {
  if (!font) return;
  element.style.fontFamily = `${JSON.stringify(font.family)}, sans-serif`;
  element.style.fontSize = `${font.size}px`;
  element.style.fontWeight = font.bold ? '700' : '400';
}

function outlineContrast(color) {
  if (!/^#[0-9a-f]{6}$/i.test(color)) return '#090b10';
  const [red,green,blue] = [1,3,5].map(start => parseInt(color.slice(start,start+2),16));
  return red*299+green*587+blue*114 < 128000 ? '#f7f7fa' : '#090b10';
}

function textShadow(color, outline) {
  const [width,blur,chosen] = outline || [];
  if (!width) return '';
  const edgeColor = chosen || outlineContrast(color);
  const scale = width/2;
  return outlineOffsets.map(([x,y]) => `${x*scale}px ${y*scale}px ${blur || 0}px ${edgeColor}`).join(',');
}

function addText(ink, text, spans) {
  if (!spans?.length) {
    ink.textContent = text;
    return;
  }
  const letters = Array.from(text);
  let offset = 0;
  ink.replaceChildren();
  for (const [start,end,colors] of spans) {
    if (start < offset || end <= start || start >= letters.length) continue;
    ink.append(letters.slice(offset,start).join(''));
    const run = document.createElement('span');
    const palette = Array.isArray(colors) ? colors : [colors];
    run.textContent = letters.slice(start,end).join('');
    run.style.color = palette.length === 1 ? palette[0] : '#f4f6fb';
    run.style.background = palette.length > 1 ? `linear-gradient(90deg, ${palette.join(', ')})` : palette[0];
    ink.append(run);
    offset = end;
  }
  ink.append(letters.slice(offset).join(''));
}

function setLabel(node, label) {
  const {el,ink} = node;
  el.hidden = !label;
  if (!label) return;
  const signature = JSON.stringify(label);
  if (node.signature === signature) return;
  node.signature = signature;
  position(el,label.rect);
  setFont(el,label.font);
  el.style.color = label.color;
  el.style.backgroundColor = label.background || '';
  el.style.textAlign = label.align;
  el.style.lineHeight = label.lineHeight ? `${label.lineHeight}px` : '';
  el.style.padding = label.padding?.map(pixel => `${pixel}px`).join(' ') || '';
  el.style.textShadow = textShadow(label.color,label.outline);
  el.style.textOverflow = label.ellipsis && !label.displayText ? 'ellipsis' : '';
  el.dataset.scrollable = Boolean(label.scrollable);
  el.title = label.hint || (label.ellipsis ? label.text : '');
  if (node.text !== label.text) el.scrollTop = 0;
  node.text = label.text;
  addText(ink,label.displayText ?? label.text,label.spans);

  if (node.png !== label.png) {
    el.classList.remove('has-image');
    node.image?.remove();
    node.image = undefined;
    node.png = label.png;
    if (label.png) {
      const image = document.createElement('img');
      image.alt = '';
      image.draggable = false;
      image.hidden = true;
      image.setAttribute('aria-hidden','true');
      image.addEventListener('load',() => {
        if (node.image !== image) return;
        image.hidden = false;
        el.classList.add('has-image');
      },{once:true});
      node.image = image;
      image.src = `data:image/png;base64,${label.png}`;
      el.append(image);
    }
  }
  if (node.image) {
    node.image.style.width = label.imageWidth ? `${label.imageWidth}px` : '';
    node.image.style.height = label.imageHeight ? `${label.imageHeight}px` : '';
  }
}

function setButton(element, action, button, icons) {
  element.hidden = !button;
  if (!button) return;
  position(element,button.rect);
  setFont(element,button.font);
  element.disabled = Boolean(button.disabled);
  const description = button.hint || descriptions[button.kind || action] || button.text;
  if (element.title !== description) element.title = description;
  element.setAttribute('aria-label',button.text || description);
  const icon = icons?.[button.kind];
  if (icon) {
    let image = element.firstElementChild;
    if (!image) {
      image = document.createElement('img');
      image.alt = '';
      image.draggable = false;
      element.replaceChildren(image);
    }
    const src = `data:image/png;base64,${icon}`;
    if (image.getAttribute('src') !== src) image.src = src;
  } else if (element.textContent !== button.text || element.firstElementChild) {
    element.textContent = button.text;
  }
}

function setSingers(values) {
  const signature = JSON.stringify(values);
  if (singerSignature === signature) return;
  singerSignature = signature;
  singers.replaceChildren(...values.map(singer => {
    const image = document.createElement('img');
    image.alt = image.title = singer.hint || '';
    image.draggable = false;
    position(image,singer.rect);
    image.src = `data:image/png;base64,${singer.png}`;
    return image;
  }));
}

function fitSurface() {
  if (!state?.bounds) return;
  const {width,height} = state.bounds;
  surface.style.width = `${width}px`;
  surface.style.height = `${height}px`;
  surface.style.transform = `scale(${innerWidth/width}, ${innerHeight/height})`;
}

window.lyricOverlay.onState(next => {
  const initial = !state;
  state = next;
  fitSurface();
  backdrop.style.backgroundColor = state.backgroundColor;
  backdrop.style.opacity = state.backgroundOpacity;
  surface.style.opacity = state.textOpacity;
  surface.dataset.locked = Boolean(state.locked);
  toolBackground.hidden = !state.toolsRect;
  if (state.toolsRect) position(toolBackground,state.toolsRect);
  document.body.style.setProperty('--overlay-opacity',state.textOpacity);
  for (const grip of document.querySelectorAll('.overlay-grip')) grip.hidden = Boolean(state.locked);
  for (const [key,node] of labels) setLabel(node,state.labels?.[key]);
  for (const [key,element] of controls) setButton(element,key,state.buttons?.[key],state.icons);
  setSingers(state.singers || []);
  if (initial) window.lyricOverlay.ready();
});

window.addEventListener('resize',fitSurface);
for (const edge of resizeEdges) {
  const grip = document.createElement('div');
  grip.className = 'overlay-grip';
  grip.dataset.edge = edge;
  grip.setAttribute('aria-hidden','true');
  document.body.append(grip);
}

document.addEventListener('click', event => {
  const button = event.target.closest('button[data-action]');
  if (!button || state?.locked) return;
  const action = button.dataset.action;
  if (action === 'menu') {
    const rect = button.getBoundingClientRect();
    send(action,{x:Math.round(screenX+rect.left),y:Math.round(screenY+rect.bottom)});
  } else send(action);
});

document.addEventListener('contextmenu', event => {
  event.preventDefault();
  if (!state?.locked) send('menu',{x:event.screenX,y:event.screenY});
});

document.addEventListener('pointerdown', event => {
  if (event.button !== 0 || !state || state.locked || drag || event.target.closest('button,[data-scrollable="true"]')) return;
  const grip = event.target.closest('.overlay-grip');
  const edge = grip?.dataset.edge || 'move';
  const target = grip || surface;
  target.setPointerCapture(event.pointerId);
  grip?.classList.add('active');
  drag = {id:event.pointerId,edge,grip};
  window.lyricOverlay.resize('start',edge,event.screenX,event.screenY);
});

document.addEventListener('pointermove', event => {
  if (drag?.id === event.pointerId) window.lyricOverlay.resize('move',drag.edge,event.screenX,event.screenY);
});

document.addEventListener('lostpointercapture', event => {
  if (drag?.id !== event.pointerId) return;
  drag.grip?.classList.remove('active');
  window.lyricOverlay.resize('end',drag.edge,event.screenX,event.screenY);
  drag = undefined;
});

function onKey(event, phase) {
  if (state?.locked || event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey || event.isComposing) return;
  const key = event.key === ' ' ? 'Space' : event.key;
  if (!actionKeys.has(key)) return;
  if (event.target.closest('button') && (key === 'Enter' || key === 'Space')) return;
  if (phase !== (['ArrowLeft','ArrowRight','Escape'].includes(key) ? 'down' : 'up')) return;
  event.preventDefault();
  send('key',{key,phase,repeat:event.repeat});
}

document.addEventListener('keydown',event => onKey(event,'down'));
document.addEventListener('keyup',event => onKey(event,'up'));
