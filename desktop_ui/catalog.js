const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[char]));
const names = value => Array.isArray(value) ? value.filter(Boolean).join(', ') : (value || '');
const modes = [
  ['songs', '수록곡'], ['albums', '음반'], ['people', '가창자'], ['groups', '그룹'], ['lives', '라이브'],
  ['locals', '로컬 음원'], ['favorites', '즐겨찾기'],
];
const listMethods = { songs: 'list', albums: 'albums', people: 'people', groups: 'people', lives: 'lives', locals: 'locals', favorites: 'list' };
const detailMethods = { songs: 'detail', albums: 'album', people: 'person', groups: 'person', lives: 'live', locals: 'local', favorites: 'detail' };
const attrs = (id, selected) => `value="${esc(id)}"${String(id) === String(selected) ? ' selected' : ''}`;
const options = (items, selected) => items.map(({id, label}) => `<option ${attrs(id, selected)}>${esc(label)}</option>`).join('');
const bodyText = (value, placeholder = '—') => esc(value || placeholder);
const brandMarks = new Map(['765','million','cinderella','sidem','shiny','gakuen','valiv'].map(id => [id,id]));
brandMarks.set('other','dictionary');
const brandIcon = id => brandMarks.has(id)
  ? `<img class="catalog-series-icon" src="../resources/series_symbols/${brandMarks.get(id)}.svg" alt="" draggable="false">` : '';
const isColor = value => typeof value === 'string' && /^#[0-9a-f]{6}$/i.test(value);
const groupCategoryLabels = {unit:'정규 유닛', project:'프로젝트', collaboration:'합동 · 기획 유닛',
  duet:'듀엣', class:'학급 유닛', liveDuet:'공연 듀엣'};
const songSeries = [
  {id:'other',label:'기타 · 합동'}, {id:'765',label:'765 ALLSTARS'}, {id:'million',label:'밀리언 라이브!'},
  {id:'cinderella',label:'신데렐라 걸즈'}, {id:'sidem',label:'SideM'}, {id:'shiny',label:'샤이니 컬러즈'},
  {id:'gakuen',label:'학원 아이돌마스터'},
];
const songFields = ['title','artists','album','series','release_date','spotify_url','source_url'];
const songPrompt = query => `다음 곡의 정확한 발매판을 찾아 음악 사전에 추가할 정보를 만들어 줘.
${query || '[찾을 곡명과 알고 있는 가창자·앨범명을 입력]'}
공식 음반 정보와 Spotify를 우선 확인해. 곡명과 가창자는 출처에 표시된 원어로 적어 줘. 발매일은 YYYY-MM-DD로 적고, 확인하지 못한 선택 항목은 빈 문자열로 남겨. Spotify 주소는 동일한 녹음의 트랙 주소를 확인한 경우에만 적어 줘. 출처 주소는 해당 곡 또는 발매판을 확인할 수 있는 HTTPS 링크로 적어 줘.
series는 ${songSeries.map(({id}) => id).join(', ')} 중 하나야. 불명확하면 other로 적어 줘.
곡과 가창자를 확인했다면 음악 사전에 붙여넣을 JSON 객체 하나를 아래 형식으로 반환해 줘.
{"title":"","artists":[""],"album":"","series":"other","release_date":"","spotify_url":"","source_url":""}`;
const creditName = (item, localized) => (localized && item.localizedName) || item.title;
const recordingCredit = {localized:true,showVoice:true};
const bilingualVoices = item => (item.artists || []).map((name,index) => {
  const localized = item.localizedVoices?.[index];
  return localized && localized !== name ? name + ' (' + localized + ')' : name;
}).join(', ');

function credit(item, {showVoice = false, localized = false} = {}) {
  const solid = isColor(item.color);
  const palette = Array.isArray(item.palette) ? item.palette.filter(isColor) : [];
  const group = item.kind === '그룹';
  const accent = solid ? item.color : palette[0];
  const style = accent ? ` style="--credit-accent:${accent}"` : '';
  const alternate = item.localizedName !== item.title ? item.localizedName : '';
  const voice = names(localized ? item.localizedVoices : item.artists);
  const sourceVoice = localized ? bilingualVoices(item) : voice;
  const missingColor = item.kind === '아이돌' && !solid;
  const tooltip = [item.title,alternate,sourceVoice ? `CV. ${sourceVoice}` : '',item.role,missingColor ? '공식 색 미확인' : ''].filter(Boolean).join(' · ');
  return `<span class="catalog-credit${group ? ' catalog-credit-group' : ''}" title="${esc(tooltip)}"${style}><span class="catalog-credit-name">${bodyText(creditName(item,localized))}</span>${showVoice && voice ? `<span class="catalog-credit-cv">CV. ${esc(voice)}</span>` : ''}${item.role ? `<span class="catalog-credit-cv">${esc(item.role)}</span>` : ''}</span>`;
}

function creditLine(items, fallback, max = Infinity, remaining = 0, options) {
  const entries = items?.length ? items : (fallback || []).map(title => ({title}));
  const more = entries.slice(max);
  const total = more.length + remaining;
  return entries.slice(0,max).map(item => credit(item,options)).join('') + (total
    ? `<span class="catalog-credit-count" aria-label="외 ${total}명" title="${esc(more.map(item => creditName(item,options?.localized)).join(' · '))}">+${total}</span>` : '');
}

function rowCredits(item, options) {
  const singers = item.credits?.filter(credit => credit.kind !== '그룹') || [];
  const groups = item.credits?.filter(credit => credit.kind === '그룹') || [];
  const line = (label, contents, showLabel = true) => contents ? `<span class="catalog-credit-line"><span class="${showLabel ? 'catalog-credit-label' : 'catalog-visually-hidden'}">${label}</span><span class="catalog-credit-items">${contents}</span></span>` : '';
  const artistLabel = ['instrumental','spoken'].includes(item.kind) ? '아티스트' : '가창자';
  const remaining = Math.max(0,(item.artistCount || 0) - (item.credits?.length || item.artists?.length || 0));
  if (!singers.length && !groups.length) return line(artistLabel,creditLine([],item.artists,5,remaining),false);
  return (singers.length ? line(artistLabel,creditLine(singers,[],5,remaining,options),false) : '') +
    (groups.length ? line('그룹',creditLine(groups,[],4,singers.length ? 0 : remaining,options)) : '') +
    (item.rosterPreview?.length ? line('멤버',creditLine(item.rosterPreview,[],5,item.rosterRemaining,options)) : '');
}

function recordingCredits(item, options = recordingCredit) {
  const credits = rowCredits(item,options);
  return credits ? `<span class="catalog-recording-credits">${credits}</span>` : '';
}

export function renderCatalog(container, api, _memory, navigation) {
  const defaults = {
    mode: 'songs', query: '', series: '', page: 1, selected: null, person: null, group: null,
    live: null, position: null, sort: undefined, duplicates: undefined, year: '',
    kind: '', playable: false,
  };
  const state = {...defaults, items: [], seriesList: [], serial: 0, detailSerial: 0};
  const events = new AbortController();
  let searchTimer, scanTimer, candidateTimer, lastDetail, disposed = false;
  container.innerHTML = `
    <section class="catalog-page" aria-label="음악 사전">
      <div class="catalog-command">
        <div class="catalog-modes" role="group" aria-label="음악 사전 항목">${modes.map(([id,label]) => `<button type="button" data-mode="${id}" aria-pressed="${id === state.mode}">${label}</button>`).join('')}</div>
        <div class="catalog-actions"><button type="button" data-catalog-action="cancel" hidden>작업 취소</button><button type="button" data-catalog-action="discover">곡 추천</button><button type="button" data-catalog-action="current">현재 곡</button><button type="button" data-catalog-action="add" aria-haspopup="dialog">곡 추가…</button><button type="button" data-catalog-action="updates" aria-haspopup="dialog">자료 갱신…</button><button type="button" data-catalog-action="scan" hidden>폴더 추가</button></div>
      </div>
      <div class="catalog-toolbar">
        <label class="catalog-search"><span aria-hidden="true">⌕</span><input type="search" placeholder="곡명, 가창자, 앨범 검색" aria-label="음악 사전 검색"></label>
        <div class="catalog-filters"><label><span>정렬</span><select class="catalog-sort" aria-label="곡 정렬"></select></label><label class="catalog-spotify-toggle"><input class="catalog-playable" type="checkbox"> Spotify 수록곡만</label><details class="catalog-extra-filters"><summary>필터</summary><div><label>종류 <select class="catalog-kind"></select></label><label><input class="catalog-duplicates" type="checkbox"> 중복 숨김</label></div></details></div>
        <div class="catalog-mode-filters" hidden><label>연도 <select class="catalog-year"></select></label></div>
        <button class="catalog-scope" type="button" hidden>조건 해제 ×</button>
      </div>
      <div class="catalog-message" role="status" hidden></div>
      <div class="catalog-body">
        <aside class="catalog-series" aria-label="시리즈"><div class="catalog-series-list panel-scroll"></div></aside>
        <section class="catalog-results"><div class="catalog-results-heading"><h2 class="catalog-count-title">수록곡 <span class="catalog-count"></span></h2></div><div class="catalog-list panel-scroll"></div><div class="catalog-pagination"></div></section>
        <aside class="catalog-detail" aria-label="상세 정보"><div class="catalog-detail-actions"></div><div class="catalog-detail-body panel-scroll"><p class="catalog-empty">항목을 선택하세요.</p></div></aside>
      </div>
      <dialog class="catalog-dialog"><form><h2 class="catalog-dialog-title"></h2><label class="catalog-dialog-label"><span></span><input class="catalog-dialog-input" required></label><div class="catalog-dialog-results panel-scroll" hidden></div><p class="catalog-dialog-status" role="status"></p><div class="catalog-dialog-buttons"><button class="catalog-confirm" type="submit">적용</button><button type="button" data-dialog-close>닫기</button></div></form></dialog>
      <dialog class="catalog-dialog catalog-add-dialog" aria-labelledby="catalog-add-title">
        <form>
          <h2 id="catalog-add-title">내 사전에 곡 추가</h2>
          <label class="catalog-dialog-label">곡명 (필수) <input name="title" class="catalog-dialog-input" maxlength="200" required autofocus></label>
          <label class="catalog-dialog-label">가창자 · 아티스트 (필수 · 한 줄에 한 명) <textarea name="artists" class="catalog-dialog-input" rows="2" required></textarea></label>
          <label class="catalog-dialog-label">앨범 (선택) <input name="album" class="catalog-dialog-input" maxlength="200"></label>
          <div class="catalog-add-grid">
            <label class="catalog-dialog-label">시리즈 <select name="series">${options(songSeries,'other')}</select></label>
            <label class="catalog-dialog-label">발매일 (선택) <input type="date" name="release_date" class="catalog-dialog-input"></label>
          </div>
          <label class="catalog-dialog-label">Spotify 트랙 주소 (선택) <input name="spotify_url" inputmode="url" class="catalog-dialog-input" maxlength="2048"></label>
          <label class="catalog-dialog-label">출처 주소 (선택) <input type="url" name="source_url" class="catalog-dialog-input" maxlength="2048"></label>
          <details class="catalog-add-import">
            <summary>ChatGPT 답변으로 입력</summary>
            <p>요청문을 ChatGPT에 붙여넣고, 받은 JSON을 가져와 확인하세요.</p>
            <button type="button" data-copy-song-prompt>요청문 복사</button>
            <textarea class="catalog-dialog-input" data-song-prompt rows="4" aria-label="ChatGPT 요청문" readonly hidden></textarea>
            <label class="catalog-dialog-label">ChatGPT가 준 JSON <textarea class="catalog-dialog-input" data-song-json rows="3" maxlength="10000"></textarea></label>
            <button type="button" data-import-song>입력란에 채우기</button>
            <p class="catalog-dialog-status" data-import-song-status role="status"></p>
          </details>
          <p class="catalog-dialog-status" data-add-song-status role="status"></p>
          <div class="catalog-dialog-buttons"><button type="button" data-add-close>닫기</button><button class="catalog-confirm" type="submit">내 사전에 저장</button></div>
        </form>
      </dialog>
    </section>`;
  const q = selector => container.querySelector(selector);
  const on = (element, name, handler) => element.addEventListener(name, handler, {signal: events.signal});
  const list = q('.catalog-list'), detail = q('.catalog-detail-body'), detailActions = q('.catalog-detail-actions');
  const seriesList = q('.catalog-series-list'), pagination = q('.catalog-pagination');
  const search = q('.catalog-search input'), sort = q('.catalog-sort'), kind = q('.catalog-kind');
  const songMode = () => state.mode === 'songs' || state.mode === 'favorites';
  const peopleMode = () => state.mode === 'people' || state.mode === 'groups';
  const seriesLabel = id => state.seriesList.find(series => String(series.id) === String(id))?.label || '';
  const brandLabel = item => item.brand === 'valiv' ? 'vα-liv' : seriesLabel(item.series);
  const msg = value => {
    const el = q('.catalog-message');
    el.textContent = value || '';
    el.hidden = !value;
  };
  const request = (method, payload = {}) => api.request(`catalog.${method}`,payload);

  function renderSeries() {
    const focused = seriesList.contains(document.activeElement) ? document.activeElement.dataset.series : undefined;
    seriesList.innerHTML = [{id:'',label:'전체 시리즈'}, ...state.seriesList.filter(series => series.id !== '')].map(({id,label}) =>
      `<button type="button" data-series="${esc(id)}" aria-pressed="${state.series === id}" aria-label="${esc(label)}" title="${esc(label)}">${id
        ? brandIcon(id) : '<span class="catalog-all-series" aria-hidden="true"><i></i><i></i><i></i><i></i></span>'}</button>`).join('');
    if (focused !== undefined) [...seriesList.children].find(button => button.dataset.series === focused)?.focus({preventScroll:true});
  }

  function renderRows() {
    if (!state.items.length) { list.innerHTML = '<p class="catalog-empty">검색 결과가 없습니다.</p>'; return; }
    list.innerHTML = state.items.map((item, index) => {
      const label = state.series && state.series === item.series && !item.brand ? '' : brandLabel(item);
      const meta = item.album || (state.mode === 'lives' ? item.date : item.releaseDate || '');
      const category = state.mode === 'groups' && ['shiny','gakuen'].includes(state.series) ? groupCategoryLabels[item.groupCategory] : '';
      const heading = category && item.groupCategory !== state.items[index - 1]?.groupCategory
        ? `<h3 class="catalog-group-heading">${category}</h3>` : '';
      const voices = bilingualVoices(item);
      const subtitle = [item.localizedName !== item.title ? item.localizedName : '', voices ? `CV. ${voices}` : ''].filter(Boolean).join(' · ');
      const members = item.memberPreview?.length ? `<span class="catalog-credit-items">${creditLine(item.memberPreview,[],Infinity,
        Math.max(0,item.memberCount - item.memberPreview.length))}</span>` : '';
      const credits = state.mode === 'albums' ? `<small>${Number(item.trackCount) || 0}곡</small>` :
        peopleMode() ? `${subtitle ? `<small>${esc(subtitle)}</small>` : ''}${members}` : rowCredits(item,songMode() ? recordingCredit : undefined);
      return `${heading}<button type="button" class="catalog-row" data-index="${index}" aria-pressed="${state.selected === item.id}">
        <span class="catalog-row-main"><strong class="catalog-row-title" title="${esc(item.title)}">${peopleMode() ? credit(item) : bodyText(item.title)}</strong>${credits ? `<span class="catalog-row-credits">${credits}</span>` : ''}</span>
        <span class="catalog-row-meta">${label ? `<span class="catalog-row-brand" title="${esc(label)}" role="img" aria-label="${esc(label)}">${brandIcon(item.brand || item.series)}</span>` : ''}${meta ? `<small title="${esc(meta)}">${esc(meta)}</small>` : ''}</span>
      </button>`;
    }).join('');
  }

  function listPayload() {
    const {query, series, page, sort, duplicates, kind, playable, person, group, live, position} = state;
    if (songMode()) return {query,series,page,sort,duplicates,kind,playable,performer:person?.id,group:group?.id,
      favorites: state.mode === 'favorites', liveId: live || undefined, position:position || undefined};
    if (state.mode === 'lives') return {query,series,page,year:state.year};
    if (state.mode === 'albums') return {query,series,page};
    if (peopleMode()) return {query,series,page,kind:state.mode === 'groups' ? 'group' : 'idol'};
    return {query,series,page};
  }

  async function loadList(scrollTop = 0) {
    const serial = ++state.serial;
    list.innerHTML = '<p class="catalog-empty">불러오는 중…</p>';
    try {
      const result = await request(listMethods[state.mode],listPayload());
      if (serial !== state.serial) return;
      state.items = result.items || [];
      state.seriesList = result.series || [];
      state.page = Number(result.page) || 1;
      if (songMode()) {
        state.sort = result.sort;
        state.duplicates = result.duplicates;
        sort.innerHTML = options(result.sorts || [],state.sort);
        kind.innerHTML = options(result.kinds || [],state.kind);
        q('.catalog-duplicates').checked = !!state.duplicates;
        q('.catalog-playable').checked = state.playable;
      }
      if (state.mode === 'lives') q('.catalog-year').innerHTML = options([{id:'',label:'모든 연도'}, ...(result.years || []).map(year => ({id:year,label:year}))],state.year);
      q('.catalog-count').textContent = (Number(result.total) || 0).toLocaleString();
      renderSeries();
      renderRows();
      list.scrollTop = scrollTop;
      const pages = Math.max(1,Math.ceil((Number(result.total) || 0) / (Number(result.pageSize) || 1)));
      pagination.innerHTML = pages > 1 ? `<button type="button" data-page="${state.page - 1}" ${state.page <= 1 ? 'disabled' : ''}>이전</button><span>${state.page} / ${pages.toLocaleString()}</span><button type="button" data-page="${state.page + 1}" ${state.page >= pages ? 'disabled' : ''}>다음</button>` : '';
    } catch (error) {
      if (serial === state.serial) {
        list.innerHTML = `<p class="catalog-empty">${esc(error?.message || '불러오지 못했습니다.')}</p>`;
        pagination.replaceChildren();
      }
    }
  }

  function snapshot() {
    const route = Object.fromEntries(Object.keys(defaults).map(key => [key,state[key]]));
    route.query = search.value.trim();
    if (route.query !== state.query) route.page = 1;
    return {...route, listScroll:list.scrollTop, detailScroll:detail.scrollTop};
  }

  function visit(route) {
    if (navigation) navigation.visit(route);
    else restore(route);
  }

  function selectMode(mode, {person = null, group = null, live = null, position = null, query = '', selectId = null} = {}) {
    visit({...snapshot(), mode, query, series:'', page:1, selected:selectId, person, group, live, position,
      listScroll:0, detailScroll:0});
  }

  function changeFilter(changes) {
    visit({...snapshot(), page:1, selected:null, listScroll:0, detailScroll:0, ...changes});
  }

  function restore(route = {}) {
    const next = {...defaults,...route};
    const {mode, query, person, group, live, selected} = next;
    const sameList = Object.keys(defaults).every(key => key === 'selected' || state[key] === next[key]);
    if (sameList && state.items.length && selected) {
      clearTimeout(searchTimer);
      search.value = query;
      if (selected !== state.selected || !lastDetail) loadDetail({id:selected},route.detailScroll);
      else detail.scrollTop = route.detailScroll || 0;
      list.scrollTop = route.listScroll || 0;
      return;
    }
    msg('');
    clearTimeout(searchTimer);
    state.detailSerial++;
    for (const key of Object.keys(defaults)) state[key] = next[key];
    search.value = query;
    search.placeholder = ({albums:'음반명 검색',people:'가창자, 성우 검색',groups:'그룹, 멤버 검색',lives:'공연 검색',locals:'음원 검색'})[mode] || '곡명, 가창자, 앨범 검색';
    detail.innerHTML = '<p class="catalog-empty">항목을 선택하세요.</p>';
    detailActions.replaceChildren();
    for (const button of container.querySelectorAll('[data-mode]')) button.setAttribute('aria-pressed',String(button.dataset.mode === mode));
    q('.catalog-count-title').firstChild.textContent = modes.find(([id]) => id === mode)[1] + ' ';
    q('.catalog-filters').hidden = !songMode();
    q('.catalog-mode-filters').hidden = mode !== 'lives';
    q('.catalog-series').hidden = mode === 'locals';
    q('.catalog-scope').hidden = !person && !group && !live;
    q('.catalog-scope').textContent = group ? `${group.title} · 그룹 ×` : person ? `${person.title} · 가창자 ×` : '공연 조건 ×';
    q('[data-catalog-action="scan"]').hidden = mode !== 'locals';
    q('[data-catalog-action="current"]').hidden = mode === 'locals';
    q('[data-catalog-action="discover"]').hidden = !songMode();
    q('[data-catalog-action="add"]').hidden = !songMode();
    q('[data-catalog-action="updates"]').hidden = mode === 'locals';
    lastDetail = null;
    if (state.seriesList.length) renderSeries();
    loadList(route.listScroll);
    if (selected) loadDetail({id:selected},route.detailScroll);
  }

  const catalogButton = (action,label,extra = '') => `<button type="button" data-detail-action="${action}" ${extra}>${label}</button>`;
  const detailLink = (action,id,title,content = '') => catalogButton(action,
    `<span class="catalog-detail-link-body"><strong>${bodyText(title)}</strong>${content}</span>`,
    `class="catalog-detail-link" data-target="${esc(id)}"`);
  function listSection(label,items,render,tagList = false, hideLabel = false, footer = '') {
    return items?.length ? `<section class="catalog-detail-section"><h3${hideLabel ? ' class="catalog-visually-hidden"' : ''}>${label}</h3><ul${tagList ? ' class="catalog-detail-tags"' : ''}>${items.map(item => `<li>${render(item)}</li>`).join('')}</ul>${footer}</section>` : '';
  }

  function renderDetail(data) {
    const mode = state.mode;
    const albums = listSection('수록 음반',data.releases, item =>
      detailLink('album',item.id,item.title,item.releaseDate ? `<small>${esc(item.releaseDate)}</small>` : ''));
    const tracks = listSection('수록곡',data.tracks, item =>
      detailLink('recording',item.id,item.title,recordingCredits(item)));
    const groupTracks = listSection('수록곡',data.trackPreview, item => detailLink('recording',item.id,item.title),
      false,false,catalogButton('personSongs','더 보기','class="catalog-detail-more" aria-label="그룹 수록곡 더 보기"'));
    const recordings = listSection('다른 녹음',data.recordings, item =>
      detailLink('recording',item.id,item.title,recordingCredits(item)));
    const concerts = listSection('라이브',data.liveEvents, item => {
      const summary = [item.date,item.performanceTitle].filter(Boolean).join(' · ');
      return detailLink('live',item.id,item.title,summary ? `<small>${esc(summary)}</small>` : '');
    });
    const files = listSection('연결된 로컬 음원',data.localFiles, item =>
      detailLink('local',item.id,item.title));
    const creditOptions = songMode() ? recordingCredit : {showVoice:true};
    const linkCredit = item => item.id ? catalogButton(item.kind === '그룹' ? 'group' : 'person',credit(item,creditOptions),
      `class="catalog-credit-link" data-target="${esc(item.id)}"`) : credit(item,creditOptions);
    const member = item => linkCredit(item) + (item.memberStatus ? '<small>멤버 확인 필요</small>' : '');
    const members = listSection('멤버',data.members,member,true);
    const singers = listSection(mode === 'lives' ? '출연진' : '가창자',data.credits?.filter(item => item.kind !== '그룹'),linkCredit,true,true);
    const groups = listSection('그룹',data.credits?.filter(item => item.kind === '그룹'),linkCredit,true);
    const units = listSection('소속 그룹',data.units,linkCredit,true);
    const rosters = (data.rosters || []).map(({group,members}) => listSection(`${esc(creditName(group,songMode()))} 멤버`,members,member,true)).join('');
    const setlist = listSection('세트리스트',data.songs, item => {
      const performers = recordingCredits(item,{});
      return item.matchCount && item.position ? detailLink('setlist',item.position,item.title,performers) :
        `<strong>${bodyText(item.title)}</strong>${performers}`;
    });
    const knownSources = new Set([data.sourceUrl,data.dateSourceUrl,data.setlistSourceUrl]);
    const officialSources = listSection('공식 출처',(data.officialSources || []).map((entry,index) => ({...entry,index}))
      .filter(entry => !knownSources.has(entry.url)), entry =>
      catalogButton('source',bodyText(entry.name),`data-target="official" data-index="${entry.index}"`));
    const duration = typeof data.duration === 'number' && data.duration > 0
      ? `${Math.floor(data.duration/60)}:${String(Math.floor(data.duration%60)).padStart(2,'0')}` : '';
    const category = mode === 'groups' ? groupCategoryLabels[data.groupCategory] : '';
    const kindLabel = songMode() ? [...kind.options].find(option => option.value === data.kind)?.text : data.kind;
    const metadata = [[mode === 'lives' ? '장소' : '앨범',data.album],['발매',data.releaseDate],['재생 시간',duration],[mode === 'lives' ? '공연일' : '종류',category || (mode === 'people' ? '' : kindLabel || data.kind)],['Spotify',data.availability],
      ['등록',data.personal ? '내 사전에 직접 추가' : ''],
      ['매체',names(data.formats)],['성우',mode === 'people' ? bilingualVoices(data) || '미확인' : ''],['색상',mode === 'people' && !isColor(data.color) ? '공식 색 미확인' : '']]
      .filter(([,value]) => value).map(([label,value]) => `<div><dt>${label}</dt><dd>${esc(value)}</dd></div>`).join('');
    const localized = peopleMode() && data.localizedName && data.localizedName !== data.title ? data.localizedName : '';
    const subtitle = mode === 'locals' ? names(data.artists) : localized;
    const brand = brandLabel(data);
    detail.innerHTML = `<header class="catalog-detail-heading">${brand ? `<div class="catalog-detail-brand">${brandIcon(data.brand || data.series)}<span>${esc(brand)}</span></div>` : ''}<h2>${peopleMode() ? credit(data) : bodyText(data.title)}</h2>
      ${subtitle ? `<p class="catalog-detail-artist">${esc(subtitle)}</p>` : ''}</header>
      ${singers}${groups}${rosters}${members}${units}${metadata ? `<dl>${metadata}</dl>` : ''}
      ${albums}${tracks}${groupTracks}${recordings}${concerts}${files}${setlist}${officialSources}`;
    let actions = '';
    if (songMode()) {
      actions = catalogButton('favorite',data.favorite ? '★ 즐겨찾기' : '☆ 즐겨찾기');
      if (data.spotify) actions += catalogButton('play','재생');
      actions += catalogButton('spotifyOpen','Spotify 열기') + catalogButton('spotifyLink','Spotify 주소');
      if (data.spotifyLinked) actions += catalogButton('spotifyUnlink','Spotify 연결 해제');
      if (data.local) actions += catalogButton('localUnlink','음원 연결 해제');
    } else if (peopleMode() && !data.trackPreview?.length && !data.liveEvents?.length) actions = catalogButton('personSongs','수록곡 보기');
    else if (mode === 'locals') actions = catalogButton('localOpen','파일 열기') + catalogButton('localLink','수록곡 연결');
    if (data.sourceUrl && mode !== 'locals') actions += catalogButton('source','출처');
    if (data.dateSourceUrl) actions += catalogButton('source','일정 출처','data-target="date_source_url"');
    if (data.setlistSourceUrl) actions += catalogButton('source','곡목 출처','data-target="setlist_source_url"');
    detailActions.innerHTML = actions;
    detail.scrollTop = 0;
  }

  async function loadDetail(item, scrollTop = 0) {
    const serial = ++state.detailSerial;
    state.selected = item.id;
    lastDetail = null;
    for(const button of list.querySelectorAll('[data-index]')) {
      button.setAttribute('aria-pressed',String(state.items[Number(button.dataset.index)]?.id === item.id));
    }
    detailActions.replaceChildren();
    detail.innerHTML = '<p class="catalog-empty">불러오는 중…</p>';
    try {
      const value = await request(detailMethods[state.mode],{id:item.id});
      if (serial !== state.detailSerial) return;
      lastDetail = value;
      renderDetail(value);
      detail.scrollTop = scrollTop;
    } catch (error) {
      if (serial === state.detailSerial) detail.innerHTML = `<p class="catalog-empty">${esc(error?.message || '상세 정보를 불러오지 못했습니다.')}</p>`;
    }
  }

  const dialog = q('.catalog-dialog:not(.catalog-add-dialog)'), form = dialog.querySelector('form'), dialogInput = dialog.querySelector('.catalog-dialog-input');
  const confirm = dialog.querySelector('.catalog-confirm'), dialogLabel = dialog.querySelector('.catalog-dialog-label span'),
    dialogStatus = dialog.querySelector('.catalog-dialog-status');
  const dialogResults = q('.catalog-dialog-results');
  let dialogWork, chosen, searchSerial = 0;
  on(q('[data-dialog-close]'),'click',() => dialog.close());
  on(dialog,'close',() => { clearTimeout(candidateTimer); searchSerial++; });
  on(form,'submit', async event => {
    event.preventDefault();
    if (!dialogWork) return;
    confirm.disabled = true;
    try { await dialogWork(); dialog.close(); }
    catch (error) { dialogStatus.textContent = error?.message || '적용하지 못했습니다.'; }
    finally { confirm.disabled = false; }
  });
  function editDialog(title,label,work) {
    dialogWork = work;
    chosen = null;
    searchSerial++;
    q('.catalog-dialog-title').textContent = title;
    dialogLabel.textContent = label;
    dialogInput.value = '';
    dialogInput.oninput = null;
    dialogResults.hidden = true;
    dialogResults.replaceChildren();
    dialogStatus.textContent = '';
    dialog.showModal();
  }

  const addDialog = q('.catalog-add-dialog'), addForm = addDialog.querySelector('form');
  const addSave = addDialog.querySelector('.catalog-confirm'), addClose = q('[data-add-close]');
  const addStatus = q('[data-add-song-status]'), importStatus = q('[data-import-song-status]');
  const promptText = q('[data-song-prompt]'), songJson = q('[data-song-json]');
  const songInput = field => addForm.elements.namedItem(field);

  function showAddDialog() {
    addForm.reset();
    q('.catalog-add-import').open = false;
    promptText.hidden = true;
    addStatus.textContent = '';
    importStatus.textContent = '';
    addDialog.showModal();
  }

  on(addClose,'click',() => addDialog.close());
  on(addDialog,'cancel',event => { if (addSave.disabled) event.preventDefault(); });
  on(q('[data-copy-song-prompt]'),'click',async () => {
    const query = ['title','artists','album'].map(field => songInput(field).value.trim()).filter(Boolean).join(' / ');
    promptText.value = songPrompt(query);
    try {
      await navigator.clipboard.writeText(promptText.value);
      importStatus.textContent = '요청문을 복사했습니다.';
    } catch {
      promptText.hidden = false;
      promptText.select();
      importStatus.textContent = '선택된 요청문을 복사하세요.';
    }
  });
  on(q('[data-import-song]'),'click',() => {
    try {
      const text = songJson.value.trim();
      const fenced = text.match(/^```(?:json)?\s*([\s\S]+?)\s*```$/i);
      const song = JSON.parse(fenced?.[1] ?? text);
      if (!song || typeof song !== 'object' || Array.isArray(song) ||
          songFields.some(field => field !== 'artists' && Object.hasOwn(song,field) && typeof song[field] !== 'string') ||
          !song.title?.trim() || !Array.isArray(song.artists) || !song.artists.length ||
          song.artists.some(artist => typeof artist !== 'string' || !artist.trim() || /[\r\n]/.test(artist))) {
        throw new Error('곡명(title)과 가창자 목록(artists)이 포함된 JSON 객체 하나를 붙여넣으세요.');
      }
      const series = song.series?.trim() || 'other';
      if (!songSeries.some(item => item.id === series)) throw new Error('JSON의 시리즈를 확인하세요.');
      const releaseDate = song.release_date?.trim() || '';
      if (releaseDate) {
        const date = new Date(releaseDate);
        if (!/^\d{4}-\d{2}-\d{2}$/.test(releaseDate) || !Number.isFinite(date.getTime()) || date.toISOString().slice(0,10) !== releaseDate) {
          throw new Error('발매일은 YYYY-MM-DD 형식으로 입력하세요.');
        }
      }
      for (const field of songFields) songInput(field).value = field === 'artists' ? song.artists.join('\n') : song[field]?.trim() || '';
      songInput('series').value = series;
      importStatus.textContent = '입력란에 채웠습니다. 출처와 곡 정보를 확인한 뒤 저장하세요.';
    } catch (error) {
      importStatus.textContent = error instanceof SyntaxError ? 'ChatGPT가 반환한 JSON을 그대로 붙여넣으세요.' : error.message;
    }
  });
  on(addForm,'submit',async event => {
    event.preventDefault();
    if (addSave.disabled) return;
    const song = Object.fromEntries(songFields.map(field => [field,songInput(field).value.trim()]));
    song.artists = song.artists.split(/\r?\n/).map(artist => artist.trim()).filter(Boolean);
    addSave.disabled = addClose.disabled = true;
    addStatus.textContent = '';
    try {
      const result = await request('addSong',song);
      if (disposed) return;
      addDialog.close();
      state.items = [];
      visit({...defaults, query:song.title, selected:result.id, duplicates:false});
      msg(result.added ? '내 사전에 곡을 추가했습니다.' : '이미 사전에 등록된 곡입니다.');
    } catch (error) {
      if (!disposed) addStatus.textContent = error?.message || '곡을 추가하지 못했습니다.';
    } finally {
      addSave.disabled = addClose.disabled = false;
    }
  });

  async function detailAction(button) {
    const data = lastDetail;
    if (!data) return;
    const action = button.dataset.detailAction, target = button.dataset.target;
    if (action === 'personSongs') {
      selectMode('songs',state.mode === 'groups' ? {group:data} : {person:data});
      return;
    }
    if (action === 'setlist') { selectMode('songs',{live:data.id,position:Number(target)}); return; }
    if (['person','group','live','local','recording','album'].includes(action)) {
      const mode = ({person:'people',group:'groups',live:'lives',local:'locals',recording:'songs',album:'albums'})[action];
      if (mode === state.mode) visit({...snapshot(),selected:target,detailScroll:0});
      else selectMode(mode,{selectId:target});
      return;
    }
    if (action === 'spotifyLink') {
      editDialog('Spotify 연결','Spotify 곡 주소',async () => {
        await request('spotifyLink',{id:data.id,url:dialogInput.value.trim()});
        if (state.selected === data.id) await loadDetail({id:data.id});
        if (state.playable) loadList();
      });
      const serial = searchSerial;
      const showCandidates = async (method = 'spotifySearch') => {
        try {
          const result = await request(method,{id:data.id});
          if (disposed || !dialog.open || serial !== searchSerial) return;
          dialogStatus.textContent = result.message || '';
          dialogResults.hidden = !result.items?.length;
          dialogResults.innerHTML = (result.items || []).map(track =>
            `<button type="button" data-choose="${esc(track.id)}" data-spotify-candidate>${esc(track.title)} <small>${esc(names(track.artists))} · ${esc(track.album)}</small></button>`
          ).join('');
          if (result.busy) candidateTimer = setTimeout(() => showCandidates('spotifyCandidates'),650);
        } catch (error) {
          if (dialog.open && serial === searchSerial) dialogStatus.textContent = error.message;
        }
      };
      showCandidates();
      return;
    }
    if (action === 'localLink') {
      editDialog('수록곡 연결','곡명 검색', async () => {
        if (!chosen) throw new Error('연결할 수록곡을 선택하세요.');
        await request('localLink',{id:data.id,songId:chosen});
        if (state.selected === data.id) await loadDetail({id:data.id});
      });
      dialogInput.oninput = async () => {
        chosen = null;
        const id = ++searchSerial, query = dialogInput.value.trim();
        if (!query) { dialogResults.hidden = true; return; }
        try {
          const result = await request('list',{query,page:1});
          if (id !== searchSerial || !dialog.open) return;
          dialogResults.hidden = false;
          dialogResults.innerHTML = result.items.map(item => `<button type="button" data-choose="${esc(item.id)}">${esc(item.title)} <small>${esc(names(item.artists))}</small></button>`).join('') || '<p class="catalog-empty">검색 결과가 없습니다.</p>';
        } catch (error) { if (id === searchSerial) dialogStatus.textContent = error.message; }
      };
      return;
    }
    button.disabled = true;
    try {
      if (action === 'localUnlink') await request(action,{songId:data.id});
      else if (action === 'source') await request(action,{mode:state.mode === 'favorites' ? 'songs' : state.mode,id:data.id,
        field:target,index:button.dataset.index === undefined ? undefined : Number(button.dataset.index)});
      else await request(action,{id:data.id});
      if (state.selected === data.id && ['favorite','spotifyUnlink','localUnlink'].includes(action)) {
        await loadDetail({id:data.id});
        if (state.mode === 'favorites' || state.playable && action === 'spotifyUnlink') loadList();
      }
    } catch (error) { msg(error?.message || '작업을 완료하지 못했습니다.'); }
    finally { button.disabled = false; }
  }

  on(dialogResults,'click',event => {
    const button = event.target.closest('[data-choose]');
    if (!button) return;
    chosen = button.dataset.choose;
    if (button.hasAttribute('data-spotify-candidate')) dialogInput.value = chosen;
    for (const item of dialogResults.querySelectorAll('button')) item.setAttribute('aria-pressed',String(item === button));
  });
  on(search,'input',() => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { state.query = search.value.trim(); state.page = 1; loadList(); },220);
  });
  on(sort,'change',() => changeFilter({sort:sort.value}));
  on(kind,'change',() => changeFilter({kind:kind.value}));
  on(q('.catalog-year'),'change',event => changeFilter({year:event.target.value}));
  on(q('.catalog-playable'),'change',event => changeFilter({playable:event.target.checked}));
  on(q('.catalog-duplicates'),'change',event => changeFilter({duplicates:event.target.checked}));
  on(q('.catalog-modes'),'click',event => {
    const button = event.target.closest('[data-mode]');
    if (button && button.dataset.mode !== state.mode) selectMode(button.dataset.mode);
  });
  on(q('.catalog-scope'),'click',() => {
    changeFilter({person:null,group:null,live:null,position:null});
  });
  on(seriesList,'click',event => {
    const button = event.target.closest('[data-series]');
    if (!button || button.dataset.series === state.series) return;
    changeFilter({series:button.dataset.series});
  });
  on(list,'click',event => {
    const button = event.target.closest('[data-index]');
    const item = button && state.items[Number(button.dataset.index)];
    if (item && item.id !== state.selected) visit({...snapshot(),selected:item.id,detailScroll:0});
  });
  on(pagination,'click',event => {
    const button = event.target.closest('[data-page]');
    if (button && !button.disabled) changeFilter({page:Number(button.dataset.page)});
  });
  for (const region of [detailActions,detail]) on(region,'click',event => {
    const button = event.target.closest('[data-detail-action]');
    if (button && !button.disabled) detailAction(button);
  });
  async function startCatalogJob(method, payload) {
    await request(method,payload);
    if (disposed) return;
    const cancel = q('[data-catalog-action="cancel"]');
    clearTimeout(scanTimer);
    const scan = async () => {
      try {
        const result = await request('localStatus');
        if (disposed) return;
        msg(result.message);
        cancel.hidden = !result.busy;
        if (result.busy) scanTimer = setTimeout(scan,650);
        else loadList();
      } catch (error) { msg(error.message); }
    };
    scan();
  }

  on(q('.catalog-actions'),'click',async event => {
    const button = event.target.closest('[data-catalog-action]');
    if (!button) return;
    try {
      if (button.dataset.catalogAction === 'current') {
        const current = await request('current');
        if (!disposed) selectMode('songs',{query:current.query,selectId:current.id});
      } else if (button.dataset.catalogAction === 'discover') {
        const recommendation = await request('discover',listPayload());
        if (!disposed && recommendation.id !== state.selected) visit({...snapshot(),selected:recommendation.id,detailScroll:0});
      } else if (button.dataset.catalogAction === 'cancel') {
        await request('cancel');
      } else if (button.dataset.catalogAction === 'add') {
        showAddDialog();
      } else if (button.dataset.catalogAction === 'updates') {
        await request('updates');
      } else {
        await startCatalogJob('localScan');
      }
    } catch (error) { msg(error?.message || '작업을 완료하지 못했습니다.'); }
  });
  restore(navigation?.initial);
  function dispose() {
    disposed = true;
    clearTimeout(searchTimer);
    clearTimeout(scanTimer);
    clearTimeout(candidateTimer);
    state.serial++;
    state.detailSerial++;
    events.abort();
  }
  return {snapshot, restore, dispose};
}
