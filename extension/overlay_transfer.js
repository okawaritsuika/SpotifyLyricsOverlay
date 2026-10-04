/* User-authorized transient transfer to the local overlay; never persisted. */
async function transferToOverlay(api,tabId,pending,request=fetch){
  if(!(await api.storage.local.get({spotifyEnabled:false})).spotifyEnabled)return {status:'disabled'};
  const headers={'X-Reader-Extension':api.runtime.id};
  const stateResponse=await request('http://127.0.0.1:47831/state',{headers,cache:'no-store',signal:AbortSignal.timeout(3000)});
  if(!stateResponse.ok)throw new Error('가사 앱 연결을 확인하세요.');
  const state=await stateResponse.json();
  if(state.reader_backend==='internal')return {status:'disabled'};
  if(state.track!==pending.track || state.token!==pending.token)return {status:'stale'};
  await api.scripting.executeScript({target:{tabId},files:['lyric_text.js']});
  await api.scripting.executeScript({target:{tabId},files:['document_match.js']});
  const results=await api.scripting.executeScript({target:{tabId},args:[state.artist||'',pending.options||{},state.reference_lines||[],state.title||'',state.full_reference_lines||[]],func:async(artist,options,references,title,fullReferences)=>{
    if(options.autoLyrics && !options.selector && window.__originalPageReader){
      await window.__originalPageReader.apply({...options,references,fullReferences});
    }
    const target=document.querySelector('.opr-reader-target');
    if(!target)return {error:'표시할 가사 표를 찾지 못했습니다.'};
    const text=readOverlayText(target);
    if(!text || text.length>200000)return {error:'가사 표의 크기와 내용을 확인하세요.'};
    const article=document.querySelector('main,article,[role="main"]')||document.body;
    const tableHTML=target.outerHTML||'';
    return {text,...(tableHTML.length<=200000?{table_html:tableHTML}:{}),url:location.href,artistMatch:artistInPage(artist,article.textContent),
      titleAlias:titleAliasEvidence(location.href,title,artist,article.textContent)};
  }});
  const value=results[0]?.result;
  if(!value?.text)throw new Error(value?.error||'가사 내용을 받지 못했습니다.');
  if(!(await api.storage.local.get({spotifyEnabled:false})).spotifyEnabled)return {status:'disabled'};
  const classify=typeof module!=='undefined'?require('./document_match.js').classifyOverlayDocument:classifyOverlayDocument;
  const decision=classify(value,state,pending);
  if(decision.status!=='matched')return decision;
  const payload={track:pending.track,token:pending.token,text:value.text,url:value.url,match_mode:decision.matchMode,user_selected:decision.selected,
    ...(value.table_html?{table_html:value.table_html}:{}),
    ...(decision.matchMode==='metadata_alias'?{title_alias:value.titleAlias}:{})};
  let body=JSON.stringify(payload);
  if(new TextEncoder().encode(body).length>1000000&&payload.table_html){delete payload.table_html;body=JSON.stringify(payload);}
  const response=await request('http://127.0.0.1:47831/lyrics',{
    method:'POST',headers:{...headers,'Content-Type':'application/json'},cache:'no-store',signal:AbortSignal.timeout(15000),
    body
  });
  const result=await response.json();
  if(!response.ok)throw new Error(result.error||'가사창 연결 실패');
  return result;
}
if(typeof module!=='undefined')module.exports={transferToOverlay};
