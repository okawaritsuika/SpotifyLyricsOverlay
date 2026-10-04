/* Receive player metadata; source lyrics are passed to the overlay separately in memory. */
class SpotifyReader {
  constructor(api,request=(...args)=>fetch(...args)){this.api=api;this.request=request;this.busy=false;this.retryAfter=0;this.retainedTabs=new Set();}
  async status(text){await this.api.storage.session.set({spotifyStatus:text});}
  async enabled(){return (await this.api.storage.local.get({spotifyEnabled:false})).spotifyEnabled;}
  async tick(){
    if(this.busy||Date.now()<this.retryAfter)return;
    this.busy=true;
    try{
      if(!await this.enabled())return;
      let state;
      try{
        const response=await this.request('http://127.0.0.1:47831/state',{
          headers:{'X-Reader-Extension':this.api.runtime.id},cache:'no-store',signal:AbortSignal.timeout(3000)
        });
        if(!response.ok)throw new Error('로컬 앱 HTTP '+response.status);
        state=await response.json();
        if(typeof state.title!=='string'||typeof state.track!=='string'||typeof state.token!=='string')throw new Error('앱 버전이 맞지 않습니다. 가사 앱을 재시작하세요.');
      }catch(error){await this.status('가사 앱 연결 대기 · '+String(error.message||error).slice(0,160));return;}
      if(!state.title){await this.status('Spotify 재생 대기');return;}
      if(state.reader_backend==='internal'){
        await this.api.storage.session.set({spotifyBackend:'internal'});
        await this.status('앱 내부 브라우저 사용 중 · Chrome 자동 연결은 대기합니다.');return;
      }
      const session=await this.api.storage.session.get({spotifyLast:'',spotifyTab:null,spotifyOwnedTab:null,spotifyURL:'',spotifyRetry:null,
        spotifyChoices:null,spotifyReferencesRetried:false,spotifyStatus:'',spotifyAwaiting:false,spotifyBackend:'chrome'});
      if(session.spotifyBackend==='internal'){
        await this.api.storage.session.remove(['spotifyLast','spotifyRetry','spotifyAttempt','spotifyChoices']);
        await this.api.storage.session.set({spotifyBackend:'chrome'});
        session.spotifyLast='';
      }
      const requestKey=state.token+':'+state.track;
      if(session.spotifyLast===requestKey){
        if(session.spotifyAwaiting && state.receipt?.status==='displayed'){
          await this.api.storage.session.set({spotifyAwaiting:false,spotifyRetry:null,spotifyAttempt:null,
            spotifyStatus:'가사창 표시 확인됨 · 확인한 가사는 앱이 PC에 저장합니다.'});
          await this.closeConfirmedTab(requestKey);
          return;
        }
        if(session.spotifyAwaiting && state.receipt?.status==='display_failed'){
          await this.api.storage.session.set({spotifyAwaiting:false,
            spotifyStatus:session.spotifyRetry?'가사 앱에서 표시하지 못했습니다 · 자동 재시도 대기':'가사 앱에서 표시하지 못했습니다 · 앱 상태를 확인하세요.'});
        }
        // Receipt processing is independent of playback. Pausing immediately
        // after delivery must not leave the last transfer unacknowledged.
        if(!state.playing)return;
        const choices=session.spotifyChoices;
        if(this.retry && !session.spotifyReferencesRetried && state.receipt?.status!=='displayed'
            && (state.reference_lines||[]).length>=2 && choices?.pending?.token+':'+choices?.pending?.track===requestKey
            && choices.tabId===session.spotifyTab && choices.urls?.length
            && !/403|captcha|challenge|denied|cannot access|permission|권한|접근|차단/i.test(session.spotifyStatus)){
          // A song may reach the source page before Spotify exposes its lyrics.
          // Recheck the already discovered candidates once when text is available.
          // Persist only the flag and URLs, never the newly received references.
          await this.api.storage.session.set({spotifyReferencesRetried:true,spotifyRetry:null});
          await this.retry({key:requestKey,tabId:choices.tabId,url:session.spotifyURL,pending:{...choices.pending,
            visited:[session.spotifyURL],candidates:choices.urls.filter(url=>url!==session.spotifyURL),
            searched:true,selectedURL:undefined}});
          return;
        }
        const retry=session.spotifyRetry;
        if(retry?.key===requestKey && retry.at<=Date.now() && this.retry && state.receipt?.status!=='displayed'){
          await this.api.storage.session.remove('spotifyRetry');await this.retry(retry);
        }
        return;
      }
      if(!state.playing){await this.status('Spotify 재생 대기');return;}
      const allowed=typeof module!=='undefined'?require('./document_match.js').documentURL:documentURL;
      const cleanTitle=typeof module!=='undefined'?require('./document_match.js').documentTitle:documentTitle;
      const url=allowed(state.document_url)||allowed(state.suggested_document_url)||'https://namu.wiki/w/'+encodeURIComponent(cleanTitle(state.title));
      if(!await this.api.permissions.contains({origins:[new URL(url).origin+'/*','http://127.0.0.1/*']})){
        await this.api.storage.local.set({spotifyEnabled:false});await this.status('자동 연동 권한이 해제되었습니다.');return;
      }
      const settings=await this.api.storage.local.get({spotifySize:22,spotifySelector:''});
      let tab;
      if(session.spotifyTab!==null){
        try{tab=await this.api.tabs.get(session.spotifyTab);}catch(_){}
        // Site redirects and document navigation must not switch automatic mode off.
        // Leave unrelated browsing alone and create a new inactive source tab.
        if(tab && (session.spotifyOwnedTab!==tab.id || tab.active || tab.pinned || tab.url!=='about:blank' && !allowed(tab.url))){
          await this.api.storage.session.remove('reader:'+tab.id);tab=null;
        }
      }
      if(!await this.enabled())return;
      if(!tab){
        tab=await this.api.tabs.create({url:'about:blank',active:false});
        await this.api.storage.session.set({spotifyOwnedTab:tab.id});
      }
      const pending={origin:new URL(url).origin,options:{selector:settings.spotifySelector,size:settings.spotifySize,autoLyrics:true},expires:Date.now()+120000,automatic:true,track:state.track,token:state.token,title:state.title,visited:[url],candidates:[]};
      await this.api.storage.session.set({spotifyLast:requestKey,spotifyTab:tab.id,spotifyURL:url,spotifyChoices:null,spotifyAttempt:null,spotifyReferencesRetried:false,spotifyAwaiting:false,
        spotifyRetry:{key:requestKey,tabId:tab.id,url,pending:{...pending,retryCount:1},at:Date.now()+20000},['reader:'+tab.id]:pending});
      if(!await this.enabled()){
        await this.api.storage.session.remove('reader:'+tab.id);return;
      }
      try{await this.api.tabs.update(tab.id,{url});}
      catch(error){await this.api.storage.session.remove(['spotifyLast','reader:'+tab.id]);this.retryAfter=Date.now()+3000;throw error;}
      await this.status(state.title+' · 가사창 연결 준비 중');
    }catch(error){await this.status('자동 연동 실패 · '+String(error.message||error).slice(0,160));}
    finally{this.busy=false;}
  }
  async closeConfirmedTab(key){
    const session=await this.api.storage.session.get({spotifyLast:'',spotifyTab:null,spotifyOwnedTab:null,spotifyURL:''});
    if(session.spotifyLast!==key || session.spotifyTab===null || session.spotifyOwnedTab!==session.spotifyTab)return;
    let tab;try{tab=await this.api.tabs.get(session.spotifyTab);}catch(_){return;}
    if(tab.active || tab.pinned || tab.pendingUrl || tab.url!==session.spotifyURL)return;
    if((await this.api.storage.session.get('spotifyLast')).spotifyLast!==key)return;
    await this.api.storage.session.set({spotifyClosingTab:tab.id});
    try{
      const [current,latest]=await Promise.all([
        this.api.tabs.get(tab.id),
        this.api.storage.session.get({spotifyLast:'',spotifyTab:null,spotifyOwnedTab:null,spotifyURL:''})
      ]);
      if(this.retainedTabs.has(tab.id) || latest.spotifyLast!==key || latest.spotifyTab!==tab.id || latest.spotifyOwnedTab!==tab.id
          || latest.spotifyURL!==session.spotifyURL || current.active || current.pinned || current.pendingUrl
          || current.url!==latest.spotifyURL)return;
      await this.api.tabs.remove(tab.id);
      await this.removed(tab.id);
    }catch(_){}
    finally{
      if((await this.api.storage.session.get('spotifyClosingTab')).spotifyClosingTab===tab.id)
        await this.api.storage.session.remove('spotifyClosingTab');
    }
  }
  async retained(tabId){
    // Revoke ownership before storage awaits; an in-flight close may hold an old snapshot.
    this.retainedTabs.add(tabId);
    const state=await this.api.storage.session.get('spotifyOwnedTab');
    if(state.spotifyOwnedTab===tabId)await this.api.storage.session.remove('spotifyOwnedTab');
  }
  async removed(tabId){
    this.retainedTabs.delete(tabId);
    const state=await this.api.storage.session.get({spotifyTab:null,spotifyClosingTab:null});
    if(state.spotifyTab===tabId){
      // Keep the last track so closing a tab does not immediately reopen it.
      await this.api.storage.session.remove(['spotifyTab','spotifyOwnedTab','spotifyURL','spotifyRetry','spotifyAttempt','reader:'+tabId]);
      if(state.spotifyClosingTab!==tabId)await this.status('자동 연동 유지 · 다음 곡에서 새 원본 탭 연결');
    }
    if(state.spotifyClosingTab===tabId)await this.api.storage.session.remove('spotifyClosingTab');
  }
}
if(typeof module!=='undefined')module.exports={SpotifyReader};
