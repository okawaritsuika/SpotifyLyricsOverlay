const FIELDS = [
  ['title', '곡 제목'], ['artists', '곡 가창자'],
  ['singers', '구절 가창자'], ['original', '일본어 원문'],
  ['reading', '후리가나'], ['korean', '한국어 번역'],
  ['previous', '이전 가사'], ['next', '다음 가사'],
];
const SAMPLE = {
  title: 'M@STERPIECE', artists: '765PRO ALLSTARS', singers: '天海春香 · 如月千早',
  original: '夢を初めて願って', reading: 'ゆめを はじめて ねがって', korean: '처음으로 꿈을 소원하며',
  previous: 'さあ今を輝こう', next: '今日までどのくらい歩いた？',
};
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const clone = value => JSON.parse(JSON.stringify(value));
const hex = value => /^#[0-9a-f]{6}$/i.test(value || '') ? value : '#10151f';
const outlineContrast = color => {
  const rgb = [1, 3, 5].map(i => parseInt(hex(color).slice(i, i + 2), 16));
  return rgb[0] * 299 + rgb[1] * 587 + rgb[2] * 114 < 128000 ? '#f7f7fa' : '#090b10';
};
const effectValue = value => Number(value) ? `${value} px` : '끔';

function drawPreviewColors(preview, draft) {
  const color = hex(draft.background_color);
  const rgb = [1,3,5].map(i => parseInt(color.slice(i, i + 2), 16));
  preview.style.backgroundColor = `rgba(${rgb.join(',')},${draft.transparent ? 0 : draft.background_opacity})`;
  preview.style.setProperty('--preview-text-opacity', draft.opacity);
}
export function drawLyricPreview(preview, draft, {overlay = false} = {}) {
  drawPreviewColors(preview, draft);
  const line = key => {
    const s = draft.text_styles[key]; if (!s || s.visible === false) return '';
    const el = document.createElement('div'); el.className = `appearance-preview-${key}`;
    const addText = (name, tag = 'span', parent = el) => {
      const text = document.createElement(tag);
      text.className = 'appearance-preview-ink';
      text.textContent = name;
      if (s.outline_width > 0) text.dataset.outlineText = name;
      parent.append(text);
    };
    if (key === 'previous' || key === 'next') {
      addText(SAMPLE[key], 'bdi');
    } else if (key === 'singers') {
      const matchesDarkInk = outlineContrast(s.color) === '#f7f7fa';
      const names = overlay ? ['하루카','치하야'] : SAMPLE.singers.split(' · ');
      for (const [index, name] of names.entries()) {
        const chip = document.createElement('span');
        chip.style.background = matchesDarkInk ? '#f6f7fa' : '#0f1726';
        chip.style.borderColor = ['#f27b9f', '#83b8d4'][index];
        addText(name, 'span', chip);
        el.append(chip);
      }
    } else addText(SAMPLE[key]);
    el.style.fontFamily = `"${String(s.family).replace(/["\\]/g, '')}", sans-serif`;
    el.style.setProperty('--sample-size', `${overlay ? Number(s.size) * 4/3 : Math.max(8, Math.min(36, Number(s.size) * .8))}px`);
    el.style.fontWeight = s.bold ? '700' : '400'; el.style.color = hex(s.color);
    el.style.setProperty('--sample-outline', `${Number(s.outline_width) * 1.6}px`);
    el.style.setProperty('--sample-blur', `${Number(s.outline_blur) * .8}px`);
    el.style.setProperty('--sample-edge-color', s.outline_color ? hex(s.outline_color) : outlineContrast(s.color));
    const spacing = s.outline_width ? Math.max(0, s.outline_width + 1.7 * s.outline_blur - 2) * .6 : 0;
    el.style.setProperty('--sample-outline-space', `${Math.round(spacing)}px`);
    return el.outerHTML;
  };
  const metadata = `${line('title')}${line('artists')}`;
  const singers = line('singers');
  const lyrics = `<div class="appearance-preview-current">${line('reading')}${line('original')}${line('korean')}</div>`;
  preview.innerHTML = `${overlay ? `<div class="preview-metadata">${metadata}</div>` : metadata+singers}<div class="appearance-preview-lyrics">${line('previous')}${line('next')}${overlay ? singers : ''}${lyrics}</div>`;
}

export async function renderAppearance(container, api, memory = {}) {
  container.innerHTML = '<div class="appearance-loading">외형 설정을 불러오는 중입니다.</div>';
  let data;
  try { data = await api.request('appearance.get'); }
  catch (error) { container.innerHTML = `<div class="appearance-error">설정을 불러오지 못했습니다: ${esc(error.message || error)}</div>`; return; }
  const originalMatches = memory.original && JSON.stringify(memory.original) === JSON.stringify(data.draft);
  let draft = clone(originalMatches ? memory.draft : data.draft), original = clone(data.draft), presets = data.presets || [];
  let nativePreview = false, previewPending = false, previewTask;
  const families = data.families || [];
  const outlineLimits = data.outline_limits;
  let openOutline = memory.openOutline;
  container.innerHTML = `
    <section class="appearance-page" aria-label="가사 표시 설정">
      <div class="appearance-grid">
        <div class="appearance-main appearance-card">
          <div class="appearance-tabs" role="group" aria-label="설정 항목">
            <button type="button" data-category="typography" aria-pressed="true" aria-controls="appearance-typography">글꼴과 색상</button>
            <button type="button" data-category="backdrop" aria-pressed="false" aria-controls="appearance-backdrop">배경과 투명도</button>
            <button type="button" data-category="compact" aria-pressed="false" aria-controls="appearance-compact">창 축소</button>
          </div>
          <div class="appearance-controls panel-scroll">
            <div id="appearance-typography" class="appearance-typography" data-panel="typography"></div>
            <div id="appearance-backdrop" class="appearance-backdrop" data-panel="backdrop" hidden></div>
            <div id="appearance-compact" class="appearance-compact" data-panel="compact" hidden></div>
          </div>
        </div>
        <aside class="appearance-side panel-scroll">
          <section class="appearance-card">
            <h2>미리보기</h2>
            <div class="appearance-preview-stage"><div class="appearance-preview"></div></div>
            <button type="button" class="appearance-button appearance-button-wide" data-action="preview">별도 창으로 보기</button>
          </section>
          <section class="appearance-card appearance-preset-card"><h2><label for="appearance-preset">프리셋</label></h2><select id="appearance-preset" class="appearance-select"></select><div class="appearance-preset-actions"><button type="button" class="appearance-button" data-action="load">불러오기</button><button type="button" class="appearance-button" data-action="save">저장</button></div></section>
        </aside>
      </div>
      <footer class="appearance-footer"><button type="button" class="appearance-button appearance-reset" data-action="reset">기본값 복원</button><div class="appearance-feedback" role="status" aria-live="polite"></div><div><button type="button" class="appearance-button" data-action="cancel">취소</button><button type="button" class="appearance-button appearance-primary" data-action="apply">적용</button></div></footer>
      <dialog class="appearance-dialog"><form method="dialog"><h2>프리셋 저장</h2><div class="appearance-dialog-mode"><label><input type="radio" name="save-mode" value="new" checked> 새로 만들기</label><label><input type="radio" name="save-mode" value="replace"> 덮어쓰기</label></div><div class="appearance-save-input"></div><div class="appearance-dialog-error" role="alert"></div><div class="appearance-dialog-actions"><button class="appearance-button" value="cancel">취소</button><button class="appearance-button appearance-primary" value="save">저장</button></div></form></dialog>
    </section>`;
  const q = selector => container.querySelector(selector);
  const feedback = (message, error = false) => { const el = q('.appearance-feedback'); el.textContent = message; el.classList.toggle('is-error', error); };
  const request = async (method, payload) => { try { return await api.request(method, payload); } catch (error) { feedback(error.message || String(error), true); throw error; } };
  const presetOptions = (selected = '') => `<option value="">프리셋 선택</option>${presets.map(name => `<option value="${esc(name)}" ${name === selected ? 'selected' : ''}>${esc(name)}</option>`).join('')}`;
  const fontOptions = selected => [...new Set([selected,...families])].filter(Boolean).map(family =>
    `<option value="${esc(family)}" ${family === selected ? 'selected' : ''}>${esc(family)}</option>`).join('');
  const outlineSlider = (key, label, prop, title, value) => `<label class="appearance-effect">
    <span>${title} <span class="appearance-effect-value">${effectValue(value)}</span></span>
    <input type="range" aria-label="${label} 외곽선 ${title}" min="0" max="${outlineLimits[prop]}" step="${data.outline_step}"
      value="${value}" data-key="${key}" data-prop="${prop}">
  </label>`;
  function draw() {
    q('.appearance-typography').innerHTML = FIELDS.map(([key, label]) => {
      const s = draft.text_styles[key]; if (!s) return '';
      const expanded = openOutline === key;
      return `<div class="appearance-style-row">
        <label class="appearance-style-name"><input type="checkbox" data-key="${key}" data-prop="visible" ${s.visible !== false ? 'checked' : ''}><strong>${label}</strong></label>
        <div class="appearance-style-controls">
          <label><span class="appearance-sr">${label} 글꼴</span><select class="appearance-font" data-key="${key}" data-prop="family">${fontOptions(s.family)}</select></label>
          <label><span class="appearance-sr">${label} 크기</span><input class="appearance-size" type="number" min="8" max="72" value="${esc(s.size)}" data-key="${key}" data-prop="size"></label>
          <label class="appearance-bold" title="굵게"><input type="checkbox" aria-label="${label} 굵게" data-key="${key}" data-prop="bold" ${s.bold ? 'checked' : ''}><span>B</span></label>
          <label class="appearance-color" title="${label} 글자색"><input type="color" aria-label="${label} 글자색" value="${hex(s.color)}" data-key="${key}" data-prop="color"><span>${esc(s.color).toUpperCase()}</span></label>
          <button type="button" class="appearance-outline-toggle" aria-label="${label} 외곽선 설정" aria-expanded="${expanded}" aria-controls="appearance-outline-${key}" data-outline="${key}">외곽선</button>
        </div>
        <div id="appearance-outline-${key}" class="appearance-outline-controls" ${expanded ? '' : 'hidden'}>
          ${outlineSlider(key, label, 'outline_width', '두께', s.outline_width)}
          ${outlineSlider(key, label, 'outline_blur', '흐림', s.outline_blur)}
          <div class="appearance-effect appearance-edge-color">
            <label class="appearance-color">색상 <input type="color" aria-label="${label} 외곽선 색상" value="${s.outline_color ? hex(s.outline_color) : outlineContrast(s.color)}" data-key="${key}" data-prop="outline_color"></label>
            <label class="appearance-check"><input type="checkbox" data-key="${key}" data-prop="outline_auto" ${s.outline_color ? '' : 'checked'}> 자동 대비</label>
          </div>
        </div>
      </div>`;
    }).join('');
    q('.appearance-backdrop').innerHTML = `<div class="appearance-sliders"><label><span>글자 불투명도 <b data-percent="opacity">${Math.round(draft.opacity * 100)}%</b></span><input type="range" min="0.15" max="1" step="0.01" value="${draft.opacity}" data-setting="opacity"></label><label><span>배경 불투명도 <b data-percent="background_opacity">${Math.round(draft.background_opacity * 100)}%</b></span><input type="range" min="0" max="1" step="0.01" value="${draft.background_opacity}" data-setting="background_opacity"></label></div><div class="appearance-background-options"><label class="appearance-check"><input type="checkbox" data-setting="transparent" ${!draft.transparent ? 'checked' : ''}> 배경 표시</label><label class="appearance-background-color">배경색 <input type="color" value="${hex(draft.background_color)}" data-setting="background_color"><span>${esc(draft.background_color).toUpperCase()}</span></label></div>`;
    const compact = draft.compact_layout || {mode:'auto_hide', priorities:{}};
    q('.appearance-compact').innerHTML = `<div class="appearance-modes"><label><input type="radio" name="appearance-mode" value="auto_hide" ${compact.mode === 'auto_hide' ? 'checked' : ''}><span><strong>순서대로 숨기기</strong></span></label><label><input type="radio" name="appearance-mode" value="shrink_only" ${compact.mode === 'shrink_only' ? 'checked' : ''}><span><strong>모두 축소하기</strong></span></label></div><div class="appearance-priorities ${compact.mode === 'shrink_only' ? 'is-disabled' : ''}"><div class="appearance-priority-title">숨김 순서</div>${FIELDS.filter(([key]) => !['previous','next'].includes(key)).map(([key,label]) => `<label>${label}<select data-priority="${key}" ${compact.mode === 'shrink_only' ? 'disabled' : ''}>${Array.from({length:7}, (_,i) => `<option value="${i}" ${Number(compact.priorities?.[key] ?? 0) === i ? 'selected' : ''}>${i ? `${i}번째` : '숨기지 않음'}</option>`).join('')}</select></label>`).join('')}</div>`;
    q('#appearance-preset').innerHTML = presetOptions();
    drawPreview();
  }
  const drawPreview = () => drawLyricPreview(q('.appearance-preview'), draft);
  const drawTransparency = () => drawPreviewColors(q('.appearance-preview'), draft);
  function syncPreview() {
    if (!nativePreview || !previewPending || previewTask) return;
    previewPending = false;
    previewTask = request('appearance.preview', {draft:clone(draft), show:false})
      .then(visible => { if (visible === false) nativePreview = false; })
      .catch(() => { nativePreview = false; })
      .finally(() => { previewTask = undefined; syncPreview(); });
  }
  function changed(transparencyOnly = false) {
    feedback('');
    if (transparencyOnly) drawTransparency();
    else drawPreview();
    previewPending = nativePreview;
    syncPreview();
  }
  q('.appearance-page').addEventListener('input', event => {
    const el = event.target, key = el.dataset.key, prop = el.dataset.prop, setting = el.dataset.setting;
    if (key && prop) {
      const style = draft.text_styles[key];
      const edge = q(`[data-key="${key}"][data-prop="outline_color"]`);
      if (prop === 'outline_auto') {
        style.outline_color = el.checked ? null : edge.value;
        if (el.checked) edge.value = outlineContrast(style.color);
      } else if (prop === 'outline_color') {
        style.outline_color = el.value;
        q(`[data-key="${key}"][data-prop="outline_auto"]`).checked = false;
      } else if (el.type === 'range') {
        style[prop] = Number(el.value);
        el.closest('.appearance-effect').querySelector('.appearance-effect-value').textContent = effectValue(el.value);
      } else {
        style[prop] = el.type === 'checkbox' ? el.checked : prop === 'size' ? Math.max(8, Math.min(72, Number(el.value) || 8)) : el.value;
        if (prop === 'color') {
          el.nextElementSibling.textContent = el.value.toUpperCase();
          if (!style.outline_color) edge.value = outlineContrast(style.color);
        }
      }
    }
    else if (setting) {
      draft[setting] = setting === 'transparent' ? !el.checked : el.type === 'range' ? Number(el.value) : el.value;
      const percent = q(`[data-percent="${setting}"]`);
      if (percent) percent.textContent = `${Math.round(Number(el.value) * 100)}%`;
      if (['background_opacity','background_color'].includes(setting)) {
        draft.transparent = false;
        q('[data-setting="transparent"]').checked = true;
      }
      if (setting === 'background_color') {
        el.nextElementSibling.textContent = el.value.toUpperCase();
        if (draft.background_opacity === 0) {
          draft.background_opacity = original.background_opacity || .55;
          q('[data-setting="background_opacity"]').value = draft.background_opacity;
          q('[data-percent="background_opacity"]').textContent = `${Math.round(draft.background_opacity * 100)}%`;
        }
      }
    }
    else if (el.dataset.priority) draft.compact_layout.priorities[el.dataset.priority] = Number(el.value);
    else if (el.name === 'appearance-mode') { draft.compact_layout.mode = el.value; draw(); }
    else return;
    changed(Boolean(setting));
  });
  q('.appearance-page').addEventListener('click', async event => {
    const outlineButton = event.target.closest('[data-outline]');
    if (outlineButton) {
      const key = outlineButton.dataset.outline;
      openOutline = openOutline === key ? null : key;
      for (const button of container.querySelectorAll('[data-outline]')) {
        const expanded = button.dataset.outline === openOutline;
        button.setAttribute('aria-expanded', String(expanded));
        q(`#${button.getAttribute('aria-controls')}`).hidden = !expanded;
      }
      return;
    }
    const category = event.target.closest('[data-category]')?.dataset.category;
    if (category) {
      for (const tab of container.querySelectorAll('[data-category]')) tab.setAttribute('aria-pressed', String(tab.dataset.category === category));
      for (const panel of container.querySelectorAll('[data-panel]')) panel.hidden = panel.dataset.panel !== category;
      q('.appearance-controls').scrollTop = 0;
      return;
    }
    const action = event.target.closest('[data-action]')?.dataset.action; if (!action) return;
    try {
      if (action === 'preview') {
        const button = q('[data-action="preview"]');
        button.disabled = true;
        try {
          nativePreview = false;
          await previewTask;
          if (!container.isConnected) return;
          nativePreview = true;
          previewTask = request('appearance.preview', {draft:clone(draft), overlay:data.overlay})
            .catch(error => { nativePreview = false; throw error; })
            .finally(() => { previewTask = undefined; syncPreview(); });
          await previewTask;
          feedback('별도 미리보기를 표시했습니다.');
        } finally { button.disabled = false; }
      }
      if (action === 'reset') { draft = clone(await request('appearance.reset')); draw(); changed(); feedback('기본값을 불러왔습니다. 적용하면 저장됩니다.'); }
      if (action === 'cancel') { draft = clone(original); draw(); changed(); feedback('변경사항을 취소했습니다.'); }
      if (action === 'apply') {
        const applied = clone(draft);
        nativePreview = previewPending = false;
        await request('appearance.apply', applied);
        original = applied;
        feedback('외형 설정을 적용했습니다.');
      }
      if (action === 'load') { const name = q('#appearance-preset').value; if (!name) { feedback('불러올 프리셋을 선택하세요.', true); return; } draft = clone(await request('appearance.presetLoad', {name})); draw(); q('#appearance-preset').value = name; changed(); feedback(`“${name}” 프리셋을 불러왔습니다.`); }
      if (action === 'save') openSave();
    } catch (_) { /* Error is displayed by request. */ }
  });
  function openSave() {
    const dialog = q('.appearance-dialog'), form = dialog.querySelector('form'), input = q('.appearance-save-input'), error = q('.appearance-dialog-error');
    form.elements['save-mode'].value = 'new'; error.textContent = '';
    const update = () => { input.innerHTML = form.elements['save-mode'].value === 'new' ? '<label>프리셋 이름<input name="preset-name" maxlength="80" placeholder="예: 나의 기본 설정" autofocus></label>' : `<label>덮어쓸 프리셋<select name="preset-name">${presetOptions(q('#appearance-preset').value)}</select></label>`; };
    update(); form.onchange = event => { if (event.target.name === 'save-mode') update(); };
    form.onsubmit = async event => {
      event.preventDefault(); if (event.submitter?.value === 'cancel') { dialog.close(); return; }
      const name = form.elements['preset-name'].value.trim(), replace = form.elements['save-mode'].value === 'replace';
      if (!name) { error.textContent = '프리셋 이름을 입력하거나 선택하세요.'; return; }
      if (!replace && presets.some(item => item.toLocaleLowerCase() === name.toLocaleLowerCase())) { error.textContent = '같은 이름의 프리셋이 있습니다. 덮어쓰기를 선택하세요.'; return; }
      try { const result = await request('appearance.presetSave', {name, replace, draft:clone(draft)}); presets = Array.isArray(result) ? result : result.names; q('#appearance-preset').innerHTML = presetOptions(name); dialog.close(); feedback(`“${name}” 프리셋을 저장했습니다.`); } catch (_) { error.textContent = '프리셋을 저장하지 못했습니다. 다시 시도하세요.'; }
    };
    dialog.showModal();
  }
  draw();
  return () => {
    nativePreview = previewPending = false;
    memory.draft = draft;
    memory.original = original;
    memory.openOutline = openOutline;
  };
}
