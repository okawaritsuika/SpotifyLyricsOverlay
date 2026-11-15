const clock = value => {
  if (value == null || !Number.isFinite(Number(value))) return '—:——';
  const n = Math.max(0, Number(value));
  return `${Math.floor(n / 60)}:${(n % 60).toFixed(2).padStart(5, '0')}`;
};

const playbackSteps = new Map([['q',-5], ['w',-2], ['e',1], ['r',5]]);
const singerKeys = '1234567890';

export function renderDeveloper(container, api, memory = {}) {
  let state, editing, playingRow, stopped = false, polling = false, saving = false, compact = false;
  let latestPlayback;
  let statusUntil = 0, scrollHeldUntil = 0;
  let revision = 0, pending = 0, nextFullPoll = 0;
  const drafts = memory.drafts ??= new Map();
  const events = new AbortController();
  const rowElements = new Map();
  container.innerHTML = `<section class="dev-workspace" aria-label="개발자 모드">
    <div class="dev-grid"><section class="dev-main">
      <section class="dev-card dev-now">
        <div class="dev-track"><div class="dev-track-text"><h2 class="dev-title">곡을 기다리는 중</h2><p class="dev-artist"></p></div><div><span class="dev-source"></span><span class="dev-live"></span></div><div class="dev-position"><strong>—:——</strong></div></div>
        <div class="dev-transport"><div class="dev-button-group" aria-label="재생 조작"><button type="button" data-action="transport" data-value="first">처음</button><button type="button" data-action="transport" data-value="toggle">재생</button><button type="button" data-action="step" data-value="-5">−5초</button><button type="button" data-action="step" data-value="-2">−2초</button><button type="button" data-action="step" data-value="1">+1초</button><button type="button" data-action="step" data-value="5">+5초</button></div><div class="dev-button-group"><button type="button" data-action="playlist-open">재생목록 변경…</button><button type="button" data-action="transport" data-value="next">다음 작업곡</button></div></div>
      </section>
      <section class="dev-card dev-lines-card"><div class="dev-section-head"><h2>가사 <small class="dev-count"></small></h2><div class="dev-edit-actions"><label class="dev-follow"><input type="checkbox" data-action="follow" checked> 선택 줄 가운데</label><div class="dev-edit-commit dev-button-group"><button type="button" data-action="discard-edits" disabled>취소</button><button type="button" data-action="apply-edits" disabled>가사 수정 적용</button></div><button type="button" data-action="compact" aria-pressed="false">심플 창</button></div></div>
        <div class="dev-line-indicators dev-button-group" aria-label="선택 줄과 가사창 줄" hidden><span class="dev-selected-number"></span><span class="dev-playing-number"></span><button type="button" data-action="jump-current" title="가사창에 표시 중인 분홍 줄 선택 (Enter)" aria-keyshortcuts="Enter" disabled>현재 싱크로</button><button type="button" data-action="record-selected" disabled>선택 줄 기록</button></div>
        <div class="dev-shortcuts" role="group" aria-label="키보드 단축키"><b>단축키</b><span><kbd>←</kbd> <kbd>→</kbd> 앞뒤 줄 싱크로 이동</span><span><kbd>Enter</kbd> 현재 싱크로</span><span><kbd>↑</kbd> 앞 줄 끝 기록</span><span><kbd>↓</kbd> 다음 줄 시작 기록</span><span><kbd>Delete</kbd> 싱크 지우기</span><span><kbd>Q</kbd> <kbd>W</kbd> <kbd>E</kbd> <kbd>R</kbd> 재생 위치 −5 / −2 / +1 / +5초</span></div>
        <div class="dev-compact-edit-actions" hidden></div>
        <div class="dev-lines panel-scroll" tabindex="0" aria-label="가사 편집 목록"></div></section>
    </section><aside class="dev-side panel-scroll">
      <section class="dev-card dev-adjust"><h2>싱크 조정</h2><div class="dev-adjust-row"><b>전체 오프셋</b><strong class="dev-offset">0.00초</strong></div><div class="dev-button-group dev-offset-buttons"><button type="button" data-action="offset" data-value="-0.5">−0.5초</button><button type="button" data-action="offset" data-value="-0.1">−0.1초</button><button type="button" data-action="offset" data-value="0.1">+0.1초</button><button type="button" data-action="offset" data-value="0.5">+0.5초</button></div><label class="dev-lead-label" for="dev-lead">미리 표시 <small>ms</small></label><div class="dev-lead-input"><input id="dev-lead" type="number" step="10" min="0" max="500"><button type="button" data-action="lead-save">적용</button></div><label class="dev-auto-wrap"><input type="checkbox" data-action="auto"> 자동 음원 분석</label><label class="dev-auto-wrap"><input type="checkbox" data-action="realtime-tracking" disabled> 재생 중 가사 추적</label></section>
      <section class="dev-card dev-adjust dev-line-tools"><h2>선택한 가사 줄 <small class="dev-selection"></small></h2>
        <div class="dev-button-group"><button type="button" data-action="previous-row">앞 줄 선택</button><button type="button" data-action="line-tool" data-value="listen">줄 듣기</button></div>
        <div class="dev-button-group dev-line-structure"><button type="button" data-action="line-tool" data-value="add">앞에 가사 추가</button><button type="button" data-action="line-tool" data-value="remove">가사 줄 삭제</button></div>
        <div class="dev-singer-keys"><b>가창자 숫자키</b><div class="dev-selected-singers" role="group" aria-label="가창자 숫자키"></div></div><div class="dev-button-group dev-singer-tools"><button type="button" data-action="line-tool" data-value="roster">곡 참여 가창자 편집</button><button type="button" data-action="singer" data-value="space" aria-keyshortcuts="Space">비우기</button><button type="button" data-action="singer" data-value="BackSpace" aria-keyshortcuts="Backspace">자동</button></div>
        <div class="dev-button-group"><label class="dev-singer-input"><input type="checkbox" data-action="singer-input"> 가사창 가창자 입력</label></div>
        <div class="dev-button-group dev-alsong-tools" hidden><button type="button" data-action="line-tool" data-value="confirmAlsong" hidden>알송 확정</button><button type="button" data-action="line-tool" data-value="unlinkAlsong" hidden>알송 연결 해제</button></div>
      </section>
      <details class="dev-card dev-tools"><summary><b>고급 도구</b><span class="dev-chevron">⌄</span></summary><div class="dev-tool-list"><button data-tool="connection">가사 연결</button><button data-tool="audio">음원 분석</button><button data-tool="alsong">알송 가사</button><button data-tool="translation">AI 일괄 번역</button><button data-tool="translationResources">내 자료 관리</button></div></details>
    </aside></div><div class="dev-status" role="status"></div>
    <dialog class="dev-row-dialog"><form><h2></h2><label hidden>원문 <input name="lyric" maxlength="4000"></label><div class="dev-button-group"><button type="button" data-action="close-dialog">취소</button><button type="submit">적용</button></div></form></dialog>
    <dialog class="dev-playlist-dialog" aria-labelledby="dev-playlist-title"><form><h2 id="dev-playlist-title">재생목록 변경</h2><label for="dev-work-playlist">Spotify 재생목록 공유 링크</label><input id="dev-work-playlist" name="playlist" maxlength="2048" required><p role="status">Spotify에서 목록을 재생하면 다음 작업곡이 그 대기열을 따라갑니다.</p><div class="dev-button-group"><button type="button" data-action="playlist-close">취소</button><button type="submit">Spotify에서 열기</button></div></form></dialog></section>`;
  const $ = selector => container.querySelector(selector);
  const list = $('.dev-lines'), dialog = $('.dev-row-dialog');
  const playlistDialog = $('.dev-playlist-dialog'), playlistInput = $('#dev-work-playlist');
  const follow = $('[data-action="follow"]');
  const indicators = $('.dev-line-indicators');
  const selectedNumber = $('.dev-selected-number'), playingNumber = $('.dev-playing-number');
  const nextTrack = $('[data-action="transport"][data-value="next"]');
  const singerKeyPanel = $('.dev-singer-keys');
  const compactEditActions = $('.dev-compact-edit-actions');
  follow.checked = memory.follow !== false;
  list.classList.toggle('is-following',follow.checked);
  let structuralEdit, rowControlsState;
  const status = (message, immediate = false) => {
    if (immediate) statusUntil = Date.now() + 5000;
    if (immediate || Date.now() >= statusUntil) $('.dev-status').textContent = message;
  };
  const activeDrafts = () => [...drafts].filter(([,draft]) => draft.track === state?.track);
  const draftKey = (index,field) => `${state.track}:${index}:${field}`;
  const focusedText = () => document.activeElement?.matches('input[data-field]') && list.contains(document.activeElement);
  function singerTag(element, name, key) {
    element.textContent = state.singerLabels?.[name] || name;
    element.title = name;
    if (key != null) {
      const badge = document.createElement('kbd');
      badge.textContent = key;
      element.prepend(badge);
    }
    element.classList.add('dev-singer-tag');
    const color = state?.singerColors?.[name];
    if (/^#[0-9a-f]{6}$/i.test(color ?? '')) element.style.setProperty('--dev-singer-color',color);
    return element;
  }

  function selectedRow() {
    return editing ?? state?.selected;
  }

  function updateActions() {
    const hasEdits = activeDrafts().length > 0;
    $('[data-action="apply-edits"]').disabled = saving || !hasEdits;
    $('[data-action="discard-edits"]').disabled = saving || (!hasEdits && !(compact && editing != null));
    compactEditActions.hidden = !compact || (editing == null && !hasEdits);
    const index = selectedRow(), row = state?.rows[index];
    $('.dev-selection').textContent = row ? `${index+1}번` : '';
    $('[data-action="record-selected"]').disabled = saving || !row || state.transportBusy;
    $('[data-action="previous-row"]').disabled = saving || !row || index <= 0;
    for (const button of container.querySelectorAll('button[data-action="line-tool"]')) {
      const action = button.dataset.value;
      button.disabled = saving || !row
        || (action === 'roster' && !state.singerEditable)
        || (action === 'listen' && (row.time == null || state.transportBusy))
        || (['remove','add'].includes(action) && (hasEdits || state.transportBusy || (action === 'remove' && state.rows.length <= 1)));
    }
    $('[data-value="confirmAlsong"]').hidden = state?.alsong !== 'temporary';
    $('[data-value="unlinkAlsong"]').hidden = !state?.alsong;
    $('.dev-alsong-tools').hidden = !state?.alsong;
    const singerInput = $('[data-action="singer-input"]');
    singerInput.checked = !!state?.singerInput;
    singerInput.disabled = saving || !row || !state.singerEditable;
    const options = state?.singerOptions || [];
    const selected = row?.singers || [];
    const singerButtons = $('.dev-selected-singers');
    const signature = JSON.stringify([options,selected,!!row && state.singerEditable,saving,state?.singerColors,state?.singerLabels]);
    singerKeyPanel.hidden = !options.length;
    if (singerButtons.dataset.signature !== signature) {
      singerButtons.dataset.signature = signature;
      singerButtons.replaceChildren(...options.map((singer,i) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.dataset.action = 'singer';
        button.dataset.value = singerKeys[i];
        button.setAttribute('aria-keyshortcuts',singerKeys[i]);
        button.disabled = !row || !state.singerEditable || saving;
        button.setAttribute('aria-pressed', String(selected.includes(singer)));
        return singerTag(button,singer,singerKeys[i]);
      }));
    }
    for (const button of $('.dev-singer-tools').querySelectorAll('[data-action="singer"]')) {
      button.disabled = !row || !state.singerEditable || saving;
    }
  }

  function centerRow(index) {
    const element = rowElements.get(index);
    if (!element || list.clientHeight === 0) return;
    const row = element.getBoundingClientRect(), bounds = list.getBoundingClientRect();
    const delta = row.top - bounds.top - Math.max(0,(list.clientHeight-row.height)/2);
    if (Math.abs(delta) > 1) list.scrollTop += delta;
  }

  function updatePlayback(playback) {
    $('.dev-position strong').textContent = clock(playback.position);
    const index = playback.track === state.track ? playback.current : null;
    const current = rowElements.get(index);
    if (playingRow !== index) {
      const previous = rowElements.get(playingRow);
      previous?.classList.remove('is-current');
      previous?.querySelector('.dev-line-hit').removeAttribute('aria-current');
      playingRow = index;
      current?.classList.add('is-current');
      current?.querySelector('.dev-line-hit').setAttribute('aria-current','true');
    }
    indicators.hidden = !rowElements.has(selectedRow()) && !current;
    $('[data-action="jump-current"]').disabled = saving || !current;
    playingNumber.textContent = current ? `가사창 ${playingRow+1}번` : '가사창 —';
  }

  function updateLive(next) {
    Object.assign(state,next);
    // A pending request can return after a newer playback message.
    updatePlayback(latestPlayback ?? next);
    const toggle = $('[data-action="transport"][data-value="toggle"]');
    toggle.textContent = next.playing ? '일시정지' : '재생';
    for (const button of container.querySelectorAll('.dev-transport button,.dev-edit-actions [data-action="transport"]')) {
      if (button.dataset.action === 'playlist-open') {
        button.disabled = next.transportBusy || saving;
        continue;
      }
      button.disabled = saving || !next.active || (next.transportBusy && (button.dataset.action === 'transport' || !next.seeking))
        || (button.dataset.value === 'first' && !state.matched);
    }
    updateActions();
    const selected = selectedRow();
    const prior = list.querySelector('.is-selected');
    const selectedElement = rowElements.get(selected) ?? null;
    if (prior !== selectedElement) {
      if (prior) {
        prior.classList.remove('is-selected');
        prior.querySelector('.dev-line-hit').setAttribute('aria-pressed','false');
      }
      if (selectedElement) {
        selectedElement.classList.add('is-selected');
        selectedElement.querySelector('.dev-line-hit').setAttribute('aria-pressed','true');
        if (!follow.checked) selectedElement.scrollIntoView({block:'nearest'});
      }
      scrollHeldUntil = 0;
    }
    selectedNumber.textContent = selectedElement ? `선택 ${selected+1}번` : '선택 —';
    if (follow.checked && !focusedText() && !dialog.open && !playlistDialog.open && Date.now() >= scrollHeldUntil) centerRow(selected);
    const locked = !!state.transportBusy || saving;
    if (rowControlsState !== locked) {
      rowControlsState = locked;
      for (const [index, element] of rowElements) {
        element.querySelector('[data-action="clear"]').disabled = locked || state.rows[index].time == null;
      }
    }
  }

  function update(next) {
    const trackChanged = state?.track !== next.track;
    if (trackChanged) {
      playingRow = null;
      rowElements.clear();
      list.replaceChildren();
      list.scrollTop = 0;
      if (state?.track) editing = null;
      if (dialog.open) dialog.close();
      scrollHeldUntil = 0;
    }
    state = next;
    $('.dev-live').textContent = next.active ? '연결됨' : '연결 대기';
    $('.dev-title').textContent = next.title || '곡을 기다리는 중';
    $('.dev-artist').textContent = next.artist || (next.title ? '아티스트 정보 없음' : 'Spotify에서 곡을 재생해 주세요.');
    $('.dev-source').textContent = next.source || '';
    $('.dev-offset').textContent = `${Number(next.offset || 0).toFixed(2)}초`;
    if (!saving) status(next.status || '');
    $('[data-action="auto"]').checked = !!next.auto;
    const tracking = $('[data-action="realtime-tracking"]');
    tracking.checked = next.realtimeTracking;
    tracking.disabled = false;
    const lead = $('#dev-lead');
    if (document.activeElement !== lead) lead.value = next.lead ?? 0;
    const synced = next.rows.filter(row => row.time != null).length;
    $('.dev-count').textContent = next.rows.length ? `싱크 ${synced}/${next.rows.length}줄` : '';
    if (!next.rows.length) {
      if (!list.querySelector('.dev-empty')) list.innerHTML = `<p class="dev-empty">${next.title ? '연결된 가사가 없습니다.' : '재생 중인 곡이 없습니다.'}</p>`;
    } else {
      list.querySelector('.dev-empty')?.remove();
    }
    const remaining = new Set(rowElements.keys());
    for (const row of next.rows) {
      const index = row.index;
      let element = rowElements.get(index);
      if (!element) {
        element = document.createElement('article');
        element.className = 'dev-line';
        element.dataset.index = index;
        element.innerHTML = `<button type="button" class="dev-line-hit" data-action="seek" aria-pressed="false"><span class="dev-line-number"></span><span class="dev-line-body"><span class="dev-line-top"><span class="dev-line-time"></span><span class="dev-singers"></span></span><strong class="dev-original"></strong><span class="dev-reading" lang="ja"></span><span class="dev-translation"></span></span></button><div class="dev-line-actions"><button type="button" data-action="seek" title="이 줄을 선택하고 재생 위치 이동" aria-label="이 줄을 선택하고 재생 위치 이동">이동</button><button type="button" data-action="edit" aria-expanded="false">편집</button><button type="button" data-action="clear" title="이 줄의 싱크 지우기">싱크 지우기</button></div><div class="dev-line-fields" hidden><label><span>원문</span><input data-field="original" aria-label="원문"></label><label><span>읽기</span><input data-field="reading" aria-label="읽기"></label><label><span>번역</span><input data-field="translation" aria-label="번역"></label></div>`;
        rowElements.set(index,element);
        list.append(element);
      }
      remaining.delete(index);
      element.querySelector('.dev-line-number').textContent = String(index + 1).padStart(2, '0');
      const origin = ({audio:'AI',manual:'수동',provider:'제공'})[row.timingOrigin];
      element.querySelector('.dev-line-time').textContent = row.time == null ? '싱크 없음' : `${clock(row.time)}${origin ? ` · ${origin}` : ''}`;
      const cast = element.querySelector('.dev-singers');
      const singers = (row.singers || []).map(name => [name,singerKeys[next.singerOptions.indexOf(name)]]);
      const castKey = JSON.stringify(singers.map(([name,key]) => [name,key,next.singerColors?.[name],next.singerLabels?.[name]]));
      if (cast.dataset.signature !== castKey) {
        cast.dataset.signature = castKey;
        cast.replaceChildren(...singers.map(([name,key]) => singerTag(document.createElement('span'),name,key)));
      }
      const edit = element.querySelector('[data-action="edit"]');
      edit.disabled = saving;
      edit.setAttribute('aria-expanded',String(editing === index));
      element.querySelector('.dev-line-fields').hidden = editing !== index;
      element.querySelector('.dev-line-actions [data-action="seek"]').disabled = row.time == null || saving;
      element.querySelector('[data-action="clear"]').disabled = row.time == null || saving || next.transportBusy;
      for (const field of ['original','reading','translation']) {
        const input = element.querySelector(`[data-field="${field}"]`);
        const value = drafts.get(draftKey(index,field))?.value ?? row[field] ?? '';
        element.querySelector(`.dev-${field}`).textContent = value;
        if (document.activeElement !== input) input.value = value;
        input.disabled = saving;
      }
    }
    for (const key of remaining) {
      rowElements.get(key).remove();
      rowElements.delete(key);
    }
    updateLive(next);
  }

  async function refresh() {
    if (stopped || polling || pending || saving) return;
    polling = true;
    const serial = revision;
    try {
      if (!state || Date.now() >= nextFullPoll) {
        const next = await api('developer.get',{});
        if (!stopped && revision === serial) {
          nextFullPoll = Date.now() + 1500;
          update(next);
        }
      } else {
        const next = await api('developer.position',{});
        if (!stopped && revision === serial && next.track === state.track && next.matched === state.matched) updateLive(next);
        else nextFullPoll = 0;
      }
    } catch (error) {
      if (!stopped) status(error?.message || String(error));
    } finally {
      polling = false;
    }
  }

  async function request(method, payload = {}) {
    const serial = ++revision;
    pending++;
    try {
      const next = await api(method,payload);
      if (!stopped && revision === serial) update(next);
      return next;
    } catch (error) {
      if (!stopped && revision === serial) status(error?.message || String(error),true);
      return null;
    } finally {
      pending--;
    }
  }

  function rowAction(action,index,extra = {}) {
    if (!state?.track || !state.rows[index] || saving) return;
    if (action === 'select') scrollHeldUntil = 0;
    return request(`developer.${action}`,{index,track:state.track,...extra});
  }

  function selectPlayingRow() {
    const index = playingRow;
    if (!rowElements.has(index)) return;
    editing = null;
    rowAction('select',index);
    if (!follow.checked) centerRow(index);
  }

  function browseWithoutFollowing() { scrollHeldUntil = Date.now() + 4000; }
  list.addEventListener('wheel',browseWithoutFollowing,{signal: events.signal,passive:true});
  list.addEventListener('pointerdown',event => {
    if (event.target === list) browseWithoutFollowing();
  },{signal: events.signal});
  list.addEventListener('keydown',event => {
    if (event.target === list && ['PageUp','PageDown','Home','End'].includes(event.key)) browseWithoutFollowing();
  },{signal:events.signal});

  container.addEventListener('input', event => {
    const input = event.target.closest('[data-field]');
    if (!input || !state?.track || saving) return;
    const index = Number(input.closest('.dev-line').dataset.index), field = input.dataset.field;
    const track = state.track, key = draftKey(index,field);
    if ((state.rows[index]?.[field] ?? '') === input.value) drafts.delete(key);
    else drafts.set(key, {index, field, track, value:input.value});
    input.closest('.dev-line').querySelector(`.dev-${field}`).textContent = input.value;
    updateActions();
  }, {signal: events.signal});

  async function applyEdits() {
    if (saving) return;
    const changes = activeDrafts();
    if (!changes.length) return;
    saving = true;
    revision++;
    update(state);
    let failure = '';
    try {
      for (const [key,draft] of changes) {
        if (stopped) break;
        const next = await api('developer.saveLine',draft);
        if (!next?.matched || next.track !== draft.track) throw new Error('재생 곡이 바뀌었습니다.');
        drafts.delete(key);
        if (!stopped) update(next);
      }
    } catch (error) {
      failure = error?.message || '가사를 저장하지 못했습니다.';
    } finally {
      saving = false;
      if (!stopped) {
        if (!failure) {
          editing = null;
          list.focus({preventScroll:true});
        }
        update(state);
        status(failure || '가사 수정을 적용했습니다.',true);
      }
    }
  }

  function openStructuralEdit(action,index) {
    structuralEdit = {action,index,track:state.track};
    const input = dialog.querySelector('input');
    dialog.querySelector('h2').textContent = action === 'add' ? `${index+1}번 줄 앞에 가사 추가` : `${index+1}번 가사 줄 삭제`;
    input.parentElement.hidden = action !== 'add';
    input.required = action === 'add';
    input.value = '';
    dialog.querySelector('button[type="submit"]').textContent = action === 'add' ? '추가' : '삭제';
    dialog.showModal();
    if (action === 'add') input.focus();
  }

  dialog.querySelector('form').addEventListener('submit',async event => {
    event.preventDefault();
    if (!structuralEdit || structuralEdit.track !== state.track) { dialog.close(); return; }
    const {action,index} = structuralEdit;
    const value = dialog.querySelector('input').value.trim();
    if (action === 'add' && !value) return;
    dialog.close();
    await rowAction('lineTool',index,{tool:action,value});
  },{signal:events.signal});

  playlistDialog.querySelector('form').addEventListener('submit',async event => {
    event.preventDefault();
    const button = playlistDialog.querySelector('button[type="submit"]');
    if (button.disabled) return;
    button.disabled = true;
    const next = await request('developer.openWorkPlaylist',{value:playlistInput.value.trim()});
    button.disabled = false;
    if (!stopped && next) playlistDialog.close();
    else if (!stopped) playlistDialog.querySelector('[role="status"]').textContent = $('.dev-status').textContent;
  },{signal:events.signal});

  container.addEventListener('change', event => {
    if (event.target.matches('[data-action="auto"]')) request('developer.setAuto',{enabled:event.target.checked});
    if (event.target.matches('[data-action="realtime-tracking"]')) {
      request('developer.setRealtimeTracking',{enabled:event.target.checked});
    }
    if (event.target.matches('[data-action="singer-input"]')) {
      rowAction('lineTool',selectedRow(),{tool:'singerInput',enabled:event.target.checked});
    }
    if (event.target === follow) {
      memory.follow = follow.checked;
      const firstRow = rowElements.get(0);
      const previousTop = firstRow?.getBoundingClientRect().top;
      list.classList.toggle('is-following',follow.checked);
      if (!follow.checked && previousTop != null) {
        list.scrollTop += firstRow.getBoundingClientRect().top - previousTop;
      }
      scrollHeldUntil = 0;
      if (state) updateLive(state);
    }
  }, {signal: events.signal});

  container.addEventListener('click', async event => {
    const button = event.target.closest('button');
    if (!button || button.disabled) return;
    if (event.detail > 0 && !dialog.open && !playlistDialog.open) list.focus({preventScroll:true});
    if (button.dataset.tool) { request('developer.open',{tool:button.dataset.tool}); return; }
    const action = button.dataset.action, value = button.dataset.value;
    const index = Number(button.closest('.dev-line')?.dataset.index);
    if (action === 'compact') {
      button.disabled = true;
      try {
        compact = await api('developer.compact',{enabled:!compact});
        if (stopped) return;
        $('.dev-workspace').classList.toggle('is-compact',compact);
        button.setAttribute('aria-pressed',String(compact));
        button.textContent = compact ? '전체 모드' : '심플 창';
        const structure = $('.dev-line-structure'), commit = $('.dev-edit-commit');
        if (compact) {
          follow.closest('label').after(nextTrack);
          compactEditActions.before(singerKeyPanel);
          compactEditActions.append(structure,commit);
        } else {
          $('[data-action="playlist-open"]').after(nextTrack);
          $('.dev-singer-tools').before(structure,singerKeyPanel);
          button.before(commit);
        }
        $('[data-action="apply-edits"]').textContent = compact ? '저장' : '가사 수정 적용';
        scrollHeldUntil = 0;
        if (state) update(state);
        list.focus({preventScroll:true});
      } catch (error) {
        if (!stopped) status(error?.message || String(error),true);
      } finally {
        button.disabled = false;
      }
    } else if (action === 'apply-edits') applyEdits();
    else if (action === 'discard-edits') {
      for (const [key] of activeDrafts()) drafts.delete(key);
      editing = null;
      update(state);
      list.focus({preventScroll:true});
      status('가사 수정을 취소했습니다.',true);
    } else if (action === 'edit') {
      editing = editing === index ? null : index;
      update(state);
      if (editing != null) {
        rowElements.get(index).querySelector('[data-field="original"]').focus();
        rowAction('select',index);
      }
    } else if (action === 'seek') {
      editing = null;
      rowAction('seek',index);
    } else if (action === 'jump-current') {
      selectPlayingRow();
    } else if (action === 'record-selected') {
      rowAction('record',selectedRow());
    } else if (action === 'clear') {
      rowAction(action,index);
    } else if (action === 'previous-row') {
      const target = selectedRow()-1;
      editing = null;
      rowAction('select',target);
    } else if (action === 'line-tool') {
      const row = selectedRow();
      if (value === 'add' || value === 'remove') openStructuralEdit(value,row);
      else rowAction('lineTool',row,{tool:value});
    } else if (action === 'singer') rowAction('lineTool',selectedRow(),{tool:'singer',key:value});
    else if (action === 'playlist-open') {
      const id = state?.workPlaylist;
      playlistInput.value = /^[A-Za-z0-9]{22}$/.test(id ?? '') ? `https://open.spotify.com/playlist/${id}` : '';
      playlistDialog.querySelector('[role="status"]').textContent = 'Spotify에서 목록을 재생하면 다음 작업곡이 그 대기열을 따라갑니다.';
      playlistDialog.showModal();
      playlistInput.select();
    } else if (action === 'playlist-close') playlistDialog.close();
    else if (action === 'transport') request('developer.transport',{tool:value});
    else if (action === 'step') request('developer.step',{seconds:Number(value)});
    else if (action === 'offset') request('developer.offset',{delta:Number(value)});
    else if (action === 'lead-save' && $('#dev-lead').reportValidity()) request('developer.lead',{value:Number($('#dev-lead').value)});
    else if (action === 'close-dialog') dialog.close();
  }, {signal: events.signal});

  document.addEventListener('keydown',event => {
    const target = event.target;
    if (saving || event.defaultPrevented || event.ctrlKey || event.altKey || event.shiftKey || event.metaKey || event.isComposing
      || dialog.open || playlistDialog.open || target.isContentEditable || target.closest('input:not([type="checkbox"]), textarea, select')) return;
    const seconds = playbackSteps.get(event.key.toLowerCase());
    if (seconds != null && state?.active) {
      event.preventDefault();
      request('developer.step',{seconds});
      return;
    }
    if (!state?.matched) return;
    const index = selectedRow();
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      const target = Math.max(0,Math.min(state.rows.length-1,index + (event.key === 'ArrowLeft' ? -1 : 1)));
      event.preventDefault();
      editing = null;
      rowAction('seek',target);
    } else if (!state.transportBusy && !event.repeat && event.key === 'ArrowDown') {
      const target = state.recordingCursor + 1;
      if (state.rows[target]) {
        event.preventDefault();
        rowAction('lineTool',target,{tool:'recordNext'});
      }
    } else if (!state.transportBusy && !event.repeat && event.key === 'ArrowUp' && index > 0) {
      event.preventDefault();
      rowAction('lineTool',index,{tool:'recordEnd'});
    } else if (!event.repeat && event.key === 'Enter' && !target.closest('button, summary')) {
      event.preventDefault();
      selectPlayingRow();
    } else if (!state.transportBusy && !event.repeat && event.key === 'Delete' && state.rows[index]?.time != null) {
      event.preventDefault();
      rowAction('clear',index);
    } else if (state.singerEditable && !event.repeat) {
      const key = event.key;
      const slot = key.length === 1 ? singerKeys.indexOf(key) : -1;
      if (slot >= 0 && slot < state.singerOptions.length || key === 'Backspace' || key === ' ' && !target.closest('button, summary, input')) {
        event.preventDefault();
        rowAction('lineTool',index,{tool:'singer',key: key === ' ' ? 'space' : key === 'Backspace' ? 'BackSpace' : key});
      }
    }
  },{signal:events.signal});

  const stopPlayback = window.workspace.onDeveloperPlayback(next => {
    const trackChanged = latestPlayback?.track !== next.track;
    latestPlayback = next;
    if (!state) return;
    updatePlayback(next);
    if (trackChanged && next.track !== state.track) {
      nextFullPoll = 0;
      refresh();
    }
  });
  refresh();
  const timer = setInterval(refresh,200);
  return () => { stopped = true; stopPlayback(); clearInterval(timer); events.abort(); };
}
