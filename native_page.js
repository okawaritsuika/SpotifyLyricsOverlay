/* Host-driven reader: no native API, app token, network request or persistence. */
async function nativePageRead(state,pending){
  if(hasVisibleAccessChallenge())return {status:'blocked'};
  const links=candidateUrls(document.querySelectorAll('a[href]'),state.title,location.href,pending.visited||[]);
  const queued=documentCandidates(state.title,[...(pending.candidates||[]),...links],pending.visited||[],location.href);
  pending.candidates=queued.candidates;pending.unexamined=!!pending.unexamined||queued.unexamined;
  try{await window.__originalPageReader.apply({autoLyrics:true,references:state.reference_lines||[],fullReferences:state.full_reference_lines||[]});}
  catch(_){return {status:'no_table',url:location.href,candidates:pending.candidates,unexamined:pending.unexamined};}
  if(hasVisibleAccessChallenge())return {status:'blocked'};
  const target=document.querySelector('.opr-reader-target');
  const text=target?readOverlayText(target):'';
  if(!text||text.length>200000)return {status:'no_table',url:location.href,candidates:pending.candidates,unexamined:pending.unexamined};
  const article=document.querySelector('main,article,[role="main"]')||document.body;
  const value={text,url:location.href,artistMatch:artistInPage(state.artist||'',article.textContent),
    titleAlias:titleAliasEvidence(location.href,state.title,state.artist||'',article.textContent)};
  const decision=classifyOverlayDocument(value,state,pending);
  const tableHTML=target?.outerHTML||'';
  return {...decision,url:value.url,candidates:pending.candidates,unexamined:pending.unexamined,...(decision.status==='matched'?{text,
    ...(tableHTML.length<=200000?{table_html:tableHTML}:{})}:{}),
    ...(decision.matchMode==='metadata_alias'?{titleAlias:value.titleAlias}:{})};
}
