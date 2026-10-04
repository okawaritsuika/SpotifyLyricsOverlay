async function applyReader(api,tabId,options,restore=false) {
  if(!Number.isInteger(tabId))throw new Error('현재 탭을 찾지 못했습니다.');
  await api.scripting.executeScript({target:{tabId},files:['lyric_text.js','document_match.js','reader.js']});
  const result=await api.scripting.executeScript({target:{tabId},args:[options,restore],func:async(settings,undo)=>{
    try{return undo?window.__originalPageReader.restore():await window.__originalPageReader.apply(settings);}
    catch(error){return {ok:false,error:error.message};}
  }});
  return result[0]?.result||{ok:false,error:'표시를 변경하지 못했습니다.'};
}
async function openOriginal(api,message) {
  const url=new URL(message.url);
  const allowed=typeof module!=='undefined'?require('./document_match.js').documentURL:documentURL;
  if(!allowed(url.href))throw new Error('나무위키 또는 SideM KO 위키의 문서 주소를 입력하세요.');
  if(!await api.permissions.contains({origins:[url.origin+'/*']}))throw new Error('이 사이트 접근이 허용되지 않았습니다.');
  const tab=message.smallWindow
    ?(await api.windows.create({url:'about:blank',type:'popup',width:720,height:640,focused:true})).tabs[0]
    :await api.tabs.create({url:'about:blank',active:true});
  // Only temporary display settings; never store source page content.
  await api.storage.session.set({['reader:'+tab.id]:{origin:url.origin,options:message.options,expires:Date.now()+120000}});
  await api.tabs.update(tab.id,{url:url.href});
  return {ok:true};
}
async function pageLoaded(api,tabId,tab) {
  const key='reader:'+tabId;
  const pending=(await api.storage.session.get(key))[key];
  if(!pending || tab.url==='about:blank')return;
  await api.storage.session.remove(key);
  if(pending.expires<Date.now() || new URL(tab.url).origin!==pending.origin)return;
  if(pending.automatic && !(await api.storage.local.get({spotifyEnabled:false})).spotifyEnabled)return;
  if(pending.automatic){
    const active=await api.storage.session.get('spotifyLast');
    if(active.spotifyLast!==pending.token+':'+pending.track)return;
    await api.storage.session.set({spotifyURL:tab.url,spotifyAttempt:{tabId,pending},spotifyRetry:null});
  }
  if(pending.automatic){
    await api.scripting.executeScript({target:{tabId},files:['document_match.js']});
    const blocked=await api.scripting.executeScript({target:{tabId},func:()=>hasVisibleAccessChallenge()});
    if(blocked[0]?.result===true){await api.storage.session.set({spotifyStatus:'원본 사이트 접근 확인 필요 · 원본 탭을 확인하세요.',spotifyRetry:null});return;}
    const found=await api.scripting.executeScript({target:{tabId},args:[pending.title,pending.visited||[]],func:(title,visited)=>candidateUrls(document.querySelectorAll('a[href]'),title,location.href,visited)});
    const queueCandidates=typeof module!=='undefined'?require('./document_match.js').documentCandidates:documentCandidates;
    const queued=queueCandidates(pending.title,[...(pending.candidates||[]),...(Array.isArray(found[0]?.result)?found[0].result:[])],pending.visited||[],tab.url);
    pending.candidates=queued.candidates;pending.unexamined=!!pending.unexamined||queued.unexamined;
  }
  let result;
  try{result=await applyReader(api,tabId,pending.options);}
  catch(error){throw new Error('원본 페이지 처리 · '+String(error.message||error));}
  if(pending.automatic && ((await api.storage.session.get('spotifyLast')).spotifyLast!==pending.token+':'+pending.track
      || !(await api.storage.local.get({spotifyEnabled:false})).spotifyEnabled))return;
  if(!result.ok){
    if(pending.automatic&&await nextCandidate(api,tabId,pending))return;
    await api.action.setBadgeText({tabId,text:'!'});await api.action.setTitle({tabId,title:result.error});
    if(pending.automatic){
      await api.storage.session.set({spotifyStatus:result.error});
      await scheduleRetry(api,tabId,pending,result.error);
    }
  }else{
    await api.action.setBadgeText({tabId,text:''});
    if(pending.automatic){
      const transfer=typeof module!=='undefined'?require('./overlay_transfer.js').transferToOverlay:transferToOverlay;
      let reply;
      try{reply=await transfer(api,tabId,pending);}
      catch(error){throw new Error('가사 전달 · '+String(error.message||error));}
      // Local delivery can finish after the watcher starts the next song.
      // Its acknowledgement must not alter that song's retry or selection.
      if((await api.storage.session.get('spotifyLast')).spotifyLast!==pending.token+':'+pending.track
          || !(await api.storage.local.get({spotifyEnabled:false})).spotifyEnabled)return;
      if(reply.status==='needs_confirmation'){
        const stored=(await api.storage.session.get('spotifyChoices')).spotifyChoices;
        const urls=stored?.pending?.track===pending.track?stored.urls:[];
        await api.storage.session.set({spotifyChoices:{tabId,pending,urls:[...new Set([...urls,reply.url])].slice(0,8)}});
      }
      if(reply.status==='unmatched'){
        const choices=(await api.storage.session.get('spotifyChoices')).spotifyChoices;
        const sourceURL=typeof module!=='undefined'?require('./document_match.js').documentURL:documentURL;
        if(choices?.pending?.track===pending.track){
          const urls=choices.urls.filter(url=>sourceURL(url)!==sourceURL(tab.url));
          if(urls.length)await api.storage.session.set({spotifyChoices:{...choices,urls}});
          else await api.storage.session.remove('spotifyChoices');
        }
      }
      if(reply.status==='unmatched'||reply.status==='needs_confirmation'){
        if(await nextCandidate(api,tabId,pending))return;
        if((await api.storage.session.get('spotifyLast')).spotifyLast!==pending.token+':'+pending.track
            || !(await api.storage.local.get({spotifyEnabled:false})).spotifyEnabled)return;
        const choices=(await api.storage.session.get('spotifyChoices')).spotifyChoices;
        if(choices?.urls?.length===1&&pending.searched&&!pending.candidates?.length&&!pending.uniqueTitleURL&&!pending.unexamined){
          // Only pages with a matching title and a readable lyric table reach choices.
          await api.storage.session.set({spotifyChoices:{...choices,pending}});
          await chooseCandidate(api,choices.urls[0],true);return;
        }
        await api.storage.session.set({spotifyStatus:choices?.urls?.length?'Spotify 원문 없음 · 아래 후보에서 곡을 선택하세요.':'일치하는 가사 문서를 찾지 못했습니다. 원본 주소를 확인하세요.'});
        if(!choices?.urls?.length)await scheduleRetry(api,tabId,pending,'가사 대조 결과 확인 중');return;
      }
      if(reply.status==='queued'){
        await api.storage.session.remove('spotifyChoices');
        await api.storage.session.set({spotifyAwaiting:true});
        await scheduleRetry(api,tabId,pending,'가사창 표시 확인 대기');
      }
      await api.storage.session.set({spotifyStatus:reply.status==='queued'?'번역·독음 전달됨 · 가사창 표시 확인 중':'곡 변경 또는 연결 중지 · 다음 곡 대기'});
    }
  }
}
async function nextCandidate(api,tabId,pending){
  if(!(await api.storage.local.get({spotifyEnabled:false})).spotifyEnabled)return false;
  if((await api.storage.session.get('spotifyLast')).spotifyLast!==pending.token+':'+pending.track)return false;
  const queueCandidates=typeof module!=='undefined'?require('./document_match.js').documentCandidates:documentCandidates;
  const queued=queueCandidates(pending.title,pending.candidates||[],pending.visited||[]);
  pending.candidates=queued.candidates;pending.unexamined=!!pending.unexamined||queued.unexamined;
  if((pending.visited||[]).length>=8)return false;
  if(!pending.candidates?.length && !pending.searched){
    pending.searched=true;
    await api.storage.session.set({spotifyStatus:'제목을 줄여 원본 사이트 자동완성 후보 확인 중'});
    await applyReader(api,tabId,{},true);
    await api.scripting.executeScript({target:{tabId},files:['document_match.js','source_suggestions.js']});
    const results=await api.scripting.executeScript({target:{tabId},args:[pending.title],func:title=>sourceSuggestions(title)});
    const searched=queueCandidates(pending.title,Array.isArray(results[0]?.result)?results[0].result:[],pending.visited||[]);
    pending.candidates=searched.candidates;pending.unexamined=!!pending.unexamined||searched.unexamined;
  }
  // The track may have changed while the page's autocomplete was loading.
  if(!(await api.storage.local.get({spotifyEnabled:false})).spotifyEnabled || (await api.storage.session.get('spotifyLast')).spotifyLast!==pending.token+':'+pending.track)return false;
  const url=pending.candidates?.shift();if(!url)return false;
  const allowed=typeof module!=='undefined'?require('./document_match.js').documentURL:documentURL;
  if(!allowed(url)||new URL(url).origin!==pending.origin)return false;
  pending.visited=[...(pending.visited||[]),url];pending.expires=Date.now()+120000;
  await api.storage.session.set({spotifyURL:url,['reader:'+tabId]:pending,spotifyStatus:'가사 문서 '+pending.visited.length+'번째 · 제목·아티스트·Spotify 원문 대조 중'});
  await api.tabs.update(tabId,{url});return true;
}
async function setAutomatic(api,message){
  if(message.enabled && !await api.permissions.contains({origins:['https://namu.wiki/*','http://127.0.0.1/*']}))
    throw new Error('자동 연동 권한을 먼저 허용하세요.');
  const size=Number(message.options?.size)||22;
  if(size<12||size>48)throw new Error('글자 크기는 12–48입니다.');
  await api.storage.local.set({spotifyEnabled:!!message.enabled,spotifySize:size,spotifySelector:String(message.options?.selector||'').slice(0,1000)});
  await api.storage.session.remove('spotifyLast');
  await api.storage.session.set({spotifyStatus:message.enabled?'Spotify 연결 시작…':'자동 연동 꺼짐'});
  return {ok:true};
}
async function reportPageFailure(api,tabId,error){
  const message='가사창 연결 실패 · '+String(error.message||error).slice(0,240);
  await api.storage.session.set({spotifyStatus:message});
  await api.action.setBadgeText({tabId,text:'!'});
  await api.action.setTitle({tabId,title:message});
  const attempt=(await api.storage.session.get('spotifyAttempt')).spotifyAttempt;
  if(attempt?.tabId===tabId)await scheduleRetry(api,tabId,attempt.pending,message);
}
async function scheduleRetry(api,tabId,pending,reason){
  const key=pending.token+':'+pending.track;
  if((await api.storage.session.get('spotifyLast')).spotifyLast!==key)return;
  if(/403|captcha|challenge|denied|cannot access|permission|권한|접근|차단/i.test(String(reason)))return;
  const attempt=(pending.retryCount||0)+1;
  if(attempt>3)return;
  let tab;try{tab=await api.tabs.get(tabId);}catch(_){return;}
  const allowed=typeof module!=='undefined'?require('./document_match.js').documentURL:documentURL;
  if(!allowed(tab.url))return;
  if(!await api.permissions.contains({origins:[new URL(tab.url).origin+'/*','http://127.0.0.1/*']}))return;
  const delay=[5,15,30][attempt-1];
  await api.storage.session.set({spotifyRetry:{key,tabId,url:tab.url,pending:{...pending,retryCount:attempt},at:Date.now()+delay*1000},spotifyStatus:String(reason||'연결 준비 중')+' · '+delay+'초 후 자동 재시도 ('+attempt+'/3)'});
}
async function retryConnection(api,retry){
  if((await api.storage.session.get('spotifyLast')).spotifyLast!==retry.key || !(await api.storage.local.get({spotifyEnabled:false})).spotifyEnabled)return;
  let tab;try{tab=await api.tabs.get(retry.tabId);}catch(_){return;}
  if(tab.url!==retry.url)return;
  await api.storage.session.set({['reader:'+tab.id]:{...retry.pending,expires:Date.now()+120000}});
  try{await pageLoaded(api,tab.id,tab);}catch(error){await reportPageFailure(api,tab.id,error);}
}
async function chooseCandidate(api,url,automatic=false){
  const saved=(await api.storage.session.get('spotifyChoices')).spotifyChoices;
  const active=(await api.storage.session.get('spotifyLast')).spotifyLast;
  if(!saved||!saved.urls.includes(url)||active!==saved.pending.token+':'+saved.pending.track)throw new Error('곡이 바뀌었습니다. 현재 곡 후보를 확인하세요.');
  if(automatic&&(saved.urls.length!==1||!saved.pending.searched||saved.pending.candidates?.length||saved.pending.unexamined))throw new Error('가사 후보 대조가 끝나지 않았습니다.');
  if(!(await api.storage.local.get({spotifyEnabled:false})).spotifyEnabled)throw new Error('자동 연동이 꺼져 있습니다.');
  const allowed=typeof module!=='undefined'?require('./document_match.js').documentURL:documentURL;
  if(!allowed(url))throw new Error('문서 주소를 확인하세요.');
  if(!await api.permissions.contains({origins:[new URL(url).origin+'/*']}))throw new Error('이 사이트 접근이 허용되지 않았습니다.');
  const pending={...saved.pending,origin:new URL(url).origin,selectedURL:automatic?undefined:url,uniqueTitleURL:automatic?url:undefined,expires:Date.now()+120000};
  const tab=await api.tabs.get(saved.tabId);
  await api.storage.session.set({['reader:'+tab.id]:pending,spotifyURL:url,spotifyStatus:automatic?'가사가 확인된 문서 1개 · 자동 연결 중':'선택한 문서 연결 중'});
  const sourceURL=typeof module!=='undefined'?require('./document_match.js').documentURL:documentURL;
  if(sourceURL(tab.url)===sourceURL(url))await pageLoaded(api,tab.id,tab);else await api.tabs.update(tab.id,{url});
  return {ok:true};
}
async function recoverAutomatic(api){
  const local=await api.storage.local.get({spotifyLinkVersion:'',spotifyEnabled:false});
  const code=(await api.storage.local.get('spotifyReaderVersion')).spotifyReaderVersion;
  if(code!=='2.7.0'){
    await api.storage.session.remove(['spotifyLast','spotifyRetry','spotifyAttempt','spotifyChoices']);
    await api.storage.local.set({spotifyReaderVersion:'2.7.0'});
  }
  if(local.spotifyLinkVersion==='2.4.0')return;
  const {spotifyStatus=''}=await api.storage.session.get('spotifyStatus');
  const stoppedByOldTabLogic=/^읽기 창에서 다른 페이지를 열어|^읽기 창을 닫아/.test(spotifyStatus);
  const restore=stoppedByOldTabLogic && await api.permissions.contains({origins:['https://namu.wiki/*','http://127.0.0.1/*']});
  await api.storage.local.set({spotifyLinkVersion:'2.4.0',...(restore?{spotifyEnabled:true}:{})});
  if(restore){await api.storage.session.remove('spotifyLast');await api.storage.session.set({spotifyStatus:'자동 연동 복구 · 현재 곡 연결 중'});}
}
if(typeof module!=='undefined')module.exports={applyReader,openOriginal,pageLoaded,setAutomatic,reportPageFailure,nextCandidate,recoverAutomatic,chooseCandidate,scheduleRetry,retryConnection};
if(typeof chrome!=='undefined') {
  importScripts('spotify_link.js');
  importScripts('document_match.js');
  importScripts('overlay_transfer.js');
  const watcher=new SpotifyReader(chrome);
  watcher.retry=retry=>retryConnection(chrome,retry);
  let interval=null,configuration=Promise.resolve();
  const tick=()=>watcher.tick().catch(()=>{});
  async function configureNow(){
    await recoverAutomatic(chrome);
    if(interval){clearInterval(interval);interval=null;}
    if(await watcher.enabled()){
      await chrome.alarms.create('spotify-reader',{periodInMinutes:.5});
      interval=setInterval(tick,500);tick();
    }else await chrome.alarms.clear('spotify-reader');
  }
  function configure(){configuration=configuration.catch(()=>{}).then(configureNow);return configuration;}
  chrome.runtime.onMessage.addListener((message,sender,reply)=>{
    if(sender.id!==chrome.runtime.id || sender.url!==chrome.runtime.getURL('popup.html'))return;
    const task=message.action==='choose-candidate'?()=>chooseCandidate(chrome,message.url):message.action==='set-automatic'?()=>setAutomatic(chrome,message):message.action==='open-reader'?()=>openOriginal(chrome,message):
      ['apply-reader','restore-reader'].includes(message.action)?()=>applyReader(chrome,message.tabId,message.options,message.action==='restore-reader'):null;
    if(!task)return;
    task().then(result=>{reply(result);if(message.action==='set-automatic'&&message.enabled)tick();}).catch(error=>reply({ok:false,error:error.message}));return true;
  });
  chrome.tabs.onUpdated.addListener((id,change,tab)=>{if(change.status==='complete')pageLoaded(chrome,id,tab).catch(error=>reportPageFailure(chrome,id,error));});
  chrome.tabs.onRemoved.addListener(id=>{chrome.storage.session.remove('reader:'+id);watcher.removed(id).catch(()=>{});});
  chrome.tabs.onActivated.addListener(info=>watcher.retained(info.tabId).catch(()=>{}));
  chrome.storage.onChanged.addListener((changes,area)=>{if(area==='local'&&changes.spotifyEnabled)configure().catch(()=>{});});
  chrome.alarms.onAlarm.addListener(alarm=>{if(alarm.name==='spotify-reader')tick();});
  chrome.runtime.onStartup.addListener(()=>configure().catch(()=>{}));
  chrome.runtime.onInstalled.addListener(()=>configure().catch(()=>{}));
  configure().catch(()=>{});
}
