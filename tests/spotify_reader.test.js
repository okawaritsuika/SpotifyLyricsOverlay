const test=require('node:test');
const assert=require('node:assert/strict');
const {SpotifyReader}=require('../extension/spotify_link.js');
const {setAutomatic,pageLoaded}=require('../extension/background.js');
function fake(){
  const local={spotifyEnabled:true},session={},tabs=new Map(),calls=[];
  const area=data=>({get:async key=>typeof key==='string'?{[key]:data[key]}:{...key,...data},set:async v=>Object.assign(data,v),remove:async keys=>{for(const k of Array.isArray(keys)?keys:[keys])delete data[k];}});
  const api={runtime:{id:'a'.repeat(32)},storage:{local:area(local),session:area(session)},
    permissions:{contains:async()=>true},
    windows:{create:async v=>{calls.push(['create',v]);const t={id:9,url:v.url};tabs.set(9,t);return {tabs:[t]};}},
    tabs:{create:async v=>{calls.push(['create',v]);const t={id:9+tabs.size,url:v.url,active:!!v.active};tabs.set(t.id,t);return t;},get:async id=>{if(!tabs.has(id))throw new Error('closed');return tabs.get(id);},update:async(id,v)=>{calls.push(['navigate',v.url]);Object.assign(tabs.get(id),v);},remove:async id=>{calls.push(['remove',id]);tabs.delete(id);}},
    scripting:{executeScript:async x=>{calls.push(['script',x]);return [{result:{ok:true}}];}},action:{setBadgeText:async()=>{},setTitle:async()=>{}}};
  let state={title:'Example Song',track:'one',playing:true,token:'test-session'};
  const request=async(url,options)=>{calls.push(['local',url,options]);return {ok:true,json:async()=>state};};
  return {api,local,session,tabs,calls,watcher:new SpotifyReader(api,request),state:v=>{state={token:'test-session',...v};}};
}
test('track change reuses one inactive source tab',async()=>{
  const x=fake();await x.watcher.tick();await x.watcher.tick();
  assert.equal(x.calls.filter(c=>c[0]==='create').length,1);
  assert.equal(x.calls.filter(c=>c[0]==='navigate').length,1);
  x.state({title:'Next Song',track:'two',playing:true});await x.watcher.tick();
  assert.equal(x.calls.filter(c=>c[0]==='create').length,1);
  assert.equal(x.calls.filter(c=>c[0]==='navigate').length,2);
  assert.equal(x.calls.filter(c=>c[0]==='local').every(c=>c[1]==='http://127.0.0.1:47831/state'),true);
  assert.equal(x.session['reader:9'].options.autoLyrics,true);
  assert.equal(x.calls.find(c=>c[0]==='create')[1].active,false);
});
test('app restart retries same track with the new memory session',async()=>{
  const x=fake();await x.watcher.tick();
  x.state({title:'Example Song',track:'one',playing:true,token:'new-session'});await x.watcher.tick();
  assert.equal(x.calls.filter(c=>c[0]==='navigate').length,2);
});
test('disabled or paused state opens nothing',async()=>{
  const x=fake();x.local.spotifyEnabled=false;await x.watcher.tick();assert.equal(x.calls.length,0);
  x.local.spotifyEnabled=true;x.state({title:'Example',track:'one',playing:false});await x.watcher.tick();
  assert.equal(x.calls.filter(c=>c[0]==='create').length,0);
});
test('closing reader keeps automatic mode and waits for the next track',async()=>{
  const x=fake();await x.watcher.tick();await x.watcher.removed(9);await x.watcher.tick();
  assert.equal(x.local.spotifyEnabled,true);assert.equal(x.session.spotifyTab,undefined);
  assert.equal(x.calls.filter(c=>c[0]==='create').length,1);
  x.state({title:'Next',track:'two',playing:true});await x.watcher.tick();
  assert.equal(x.calls.filter(c=>c[0]==='create').length,2);
});
test('user navigated window is not overwritten',async()=>{
  const x=fake();await x.watcher.tick();x.tabs.get(9).url='https://example.com/';
  x.state({title:'Next',track:'two',playing:true});await x.watcher.tick();
  assert.equal(x.local.spotifyEnabled,true);assert.equal(x.calls.filter(c=>c[0]==='navigate').length,2);
  assert.equal(x.tabs.get(9).url,'https://example.com/');assert.equal(x.session.spotifyTab,10);
});
test('permission denial and disabled pending navigation cannot apply',async()=>{
  const x=fake();x.api.permissions.contains=async()=>false;
  await assert.rejects(setAutomatic(x.api,{enabled:true,options:{}}));
  await x.watcher.tick();assert.equal(x.calls.filter(c=>c[0]==='create').length,0);
  x.session['reader:9']={origin:'https://namu.wiki',expires:Date.now()+1000,automatic:true};
  await pageLoaded(x.api,9,{url:'https://namu.wiki/w/Test'});
  assert.equal(x.calls.filter(c=>c[0]==='script').length,0);
});
test('bridge failure has visible status and no page navigation',async()=>{
  const x=fake();x.watcher.request=async()=>{throw new Error('offline');};await x.watcher.tick();
  assert.match(x.session.spotifyStatus,/앱 연결 대기/);assert.equal(x.calls.filter(c=>c[0]==='create').length,0);
});


test('default browser fetch is called without the SpotifyReader receiver',async()=>{
  const x=fake();const original=globalThis.fetch;let calls=0;
  globalThis.fetch=async function(){
    'use strict';
    assert.equal(this,undefined,'native browser fetch rejects a SpotifyReader receiver');calls++;
    return {ok:true,json:async()=>({title:'Test',track:'test',token:'session',playing:false})};
  };
  try{
    await new SpotifyReader(x.api).tick();
    assert.equal(calls,1);assert.equal(x.session.spotifyStatus,'Spotify 재생 대기');
  }finally{globalThis.fetch=original;}
});

test('connection failures expose HTTP and browser error details',async()=>{
  const x=fake();x.watcher.request=async()=>({ok:false,status:403});
  await x.watcher.tick();assert.match(x.session.spotifyStatus,/HTTP 403/);
  x.watcher.request=async()=>{throw new TypeError('Failed to fetch');};
  await x.watcher.tick();assert.match(x.session.spotifyStatus,/Failed to fetch/);
});


test('page injection failures retain their stage and native error in popup status',async()=>{
  const x=fake();await x.watcher.tick();
  x.api.scripting.executeScript=async()=>{throw new Error('Cannot access contents of url');};
  let failure;
  try{await pageLoaded(x.api,9,{url:x.tabs.get(9).url});}catch(error){failure=error;}
  assert.match(failure.message,/Cannot access contents/);
  const {reportPageFailure}=require('../extension/background.js');
  await reportPageFailure(x.api,9,failure);
  assert.match(x.session.spotifyStatus,/Cannot access contents/);
});

test('retry clears deduplication and reconnects the currently playing song',async()=>{
  const x=fake();await x.watcher.tick();
  await setAutomatic(x.api,{enabled:true,options:{}});await x.watcher.tick();
  assert.equal(x.calls.filter(c=>c[0]==='navigate').length,2);
});


test('explicit document URL is used while references are still unavailable',async()=>{
  const x=fake();const url='https://namu.wiki/w/Example%20Song(Series)';
  x.state({title:'Example Song',track:'one',playing:true,document_url:url,reference_lines:[]});
  await x.watcher.tick();assert.equal(x.calls.find(c=>c[0]==='navigate')[1],url);
});
test('song without Spotify lyrics opens its document instead of waiting for a screen',async()=>{
  const x=fake();x.state({title:'Same Title',track:'one',playing:true,reference_lines:[]});
  await x.watcher.tick();assert.equal(x.calls.filter(c=>c[0]==='navigate').length,1);
  assert.equal(decodeURIComponent(x.tabs.get(9).url),'https://namu.wiki/w/Same Title');
});
test('candidate retry is bounded and uses only original-site document links',async()=>{
  const {nextCandidate}=require('../extension/background.js');const x=fake();await x.watcher.tick();
  const pending=x.session['reader:9'];pending.candidates=['https://namu.wiki/w/Example%20Song(Series)'];
  assert.equal(await nextCandidate(x.api,9,pending),true);
  assert.equal(pending.visited.length,2);
  pending.candidates=['https://example.com/'];assert.equal(await nextCandidate(x.api,9,pending),false);
  pending.visited=Array(8).fill('test');pending.candidates=['https://namu.wiki/w/Test'];
  assert.equal(await nextCandidate(x.api,9,pending),false);
});
test('SideM suggestion navigates that wiki and saved document always takes priority',async()=>{
  const x=fake(),title='Beyond The Dream - GROWING SIGN@L Ver.';
  const url='https://sidem-ko.fandom.com/ko/wiki/'+encodeURIComponent(title.replace(/ /g,'_'));
  const permissions=[];x.api.permissions.contains=async value=>(permissions.push(value),true);
  x.state({title,track:'one',playing:true,suggested_document_url:url});
  await x.watcher.tick();
  assert.equal(x.tabs.get(9).url,url);
  assert.equal(x.session['reader:9'].origin,'https://sidem-ko.fandom.com');
  assert.ok(permissions.some(value=>value.origins.includes('https://sidem-ko.fandom.com/*')));
  const {nextCandidate}=require('../extension/background.js');
  assert.equal(await nextCandidate(x.api,9,x.session['reader:9']),true);
  assert.equal(x.tabs.get(9).url,'https://sidem-ko.fandom.com/ko/wiki/Beyond_The_Dream');
  x.state({title,track:'two',playing:true,suggested_document_url:url,document_url:'https://namu.wiki/w/Saved'});
  await x.watcher.tick();
  assert.equal(x.tabs.get(9).url,'https://namu.wiki/w/Saved');
});

test('automatic navigation preserves the recording name from Spotify',async()=>{
  const x=fake(),title='蒼い鳥 (OathOne Recording)';
  x.state({title,track:'one',playing:true,reference_lines:[]});await x.watcher.tick();
  assert.equal(x.calls.find(c=>c[0]==='navigate')[1],'https://namu.wiki/w/'+encodeURIComponent(title));
});

test('a recording document without a lyric table tries the original song automatically once',async()=>{
  const {nextCandidate}=require('../extension/background.js');
  for(const name of ['蒼い鳥','眠り姫','Synthetic Song']){
    const x=fake(),title=name+' (OathOne Recording)',base='https://namu.wiki/w/'+encodeURIComponent(name);
    x.state({title,track:'one',playing:true,reference_lines:[]});await x.watcher.tick();
    const pending=x.session['reader:9'];
    x.api.scripting.executeScript=async opts=>[{result:opts.files?undefined:typeof opts.args?.[0]==='string'?[]:opts.args?{ok:false,error:'원문·발음·번역이 있는 가사 표를 찾지 못했습니다.'}:false}];
    await pageLoaded(x.api,9,{url:x.tabs.get(9).url});
    assert.equal(x.tabs.get(9).url,base);assert.equal(pending.track,'one');assert.equal(pending.title,title);
    assert.equal(x.calls.filter(c=>c[0]==='navigate').length,2);
    pending.searched=true;pending.candidates=[];
    assert.equal(await nextCandidate(x.api,9,pending),false);
  }
});

test('Chrome discovers singer versions and meaningful-parenthesis fallbacks in the common title order',async()=>{
  const {nextCandidate}=require('../extension/background.js');
  const {titleCandidates}=require('../extension/document_match.js');
  for(const title of ['Crossing! (如月千早 Ver.)','Other Song [Other Singer Version]','Song (Another Story) (Singer Ver.)']){
    const x=fake();x.state({title,track:'one',playing:true,reference_lines:[]});await x.watcher.tick();
    const pending=x.session['reader:9'];pending.searched=true;
    for(const candidate of titleCandidates(title).slice(1)){
      assert.equal(await nextCandidate(x.api,9,pending),true);
      assert.equal(x.tabs.get(9).url,'https://namu.wiki/w/'+encodeURIComponent(candidate));
    }
    assert.equal(await nextCandidate(x.api,9,pending),false);
  }
});

test('Chrome retains truncated-discovery status and never automatically approves an unfinished sole candidate',async()=>{
  const {nextCandidate,chooseCandidate}=require('../extension/background.js');
  const x=fake(),title='Song (Singer Ver.)';
  x.state({title,track:'one',playing:true,reference_lines:[]});await x.watcher.tick();
  const pending=x.session['reader:9'];
  pending.candidates=Array.from({length:12},(_,i)=>'https://namu.wiki/w/'+encodeURIComponent('Song (Series '+i+')'));
  assert.equal(await nextCandidate(x.api,9,pending),true);
  assert.equal(x.tabs.get(9).url,'https://namu.wiki/w/Song');assert.equal(pending.unexamined,true);
  const source=x.tabs.get(9).url;
  x.session.spotifyChoices={tabId:9,pending:{...pending,searched:true,candidates:[]},urls:[source]};
  await assert.rejects(chooseCandidate(x.api,source,true),/대조가 끝나지/);
});

test('only readable lyric candidates count toward ambiguity after a recording page fails',async()=>{
  for(const multiple of [false,true]){
    const x=fake(),title='M@STERPIECE (OathOne Recording)',source='https://namu.wiki/w/M%40STERPIECE',other=source+'(Series)';
    x.state({title,track:'one',playing:true,reference_lines:[]});await x.watcher.tick();
    const recording=x.tabs.get(9).url,posts=[],oldFetch=globalThis.fetch;
    globalThis.fetch=async(url,options)=>{
      if(options.method==='POST'){posts.push(JSON.parse(options.body));return {ok:true,json:async()=>({status:'queued'})};}
      return {ok:true,json:async()=>({title,track:'one',token:'test-session',reference_lines:[]})};
    };
    x.api.scripting.executeScript=async options=>{
      const current=x.tabs.get(9).url;
      return [{result:options.files?undefined:options.func?.toString().includes('readOverlayText')?{text:'synthetic lyrics',url:current,artistMatch:false}:
        options.func?.toString().includes('candidateUrls')?(multiple&&current.startsWith(source+'#')?[other]:[]):
        options.args?.length===2?{ok:current!==recording||options.args[1]===true,error:'가사 표 없음'}:
        options.args?.length===1?[]:false}];
    };
    try{
      await pageLoaded(x.api,9,{...x.tabs.get(9)});
      assert.equal(x.tabs.get(9).url,source);assert.equal(posts.length,0);
      x.tabs.get(9).url=source+'#s-4';
      await pageLoaded(x.api,9,{...x.tabs.get(9)});
      if(multiple){
        assert.equal(x.tabs.get(9).url,other);
        await pageLoaded(x.api,9,{...x.tabs.get(9)});
        assert.equal(posts.length,0);assert.equal(x.session.spotifyChoices.urls.length,2);
        assert.match(x.session.spotifyStatus,/후보에서 곡을 선택/);
      }else{
        assert.equal(posts.length,1);assert.equal(posts[0].match_mode,'unique_title');assert.equal(posts[0].user_selected,false);
        assert.equal(x.session.spotifyChoices,undefined);assert.equal(x.session.spotifyAwaiting,true);
        assert.equal(x.calls.filter(c=>c[0]==='navigate').length,2);
      }
    }finally{globalThis.fetch=oldFetch;}
  }
});

test('automatic candidate selection rejects unfinished or multiple candidates',async()=>{
  const {chooseCandidate}=require('../extension/background.js');
  for(const [searched,candidates,extra] of [[false,[],[]],[true,['https://namu.wiki/w/Other'],[]],[true,[],['https://namu.wiki/w/Other']]]){
    const x=fake();await x.watcher.tick();const url=x.tabs.get(9).url;
    x.session.spotifyChoices={tabId:9,pending:{...x.session['reader:9'],searched,candidates},urls:[url,...extra]};
    await assert.rejects(chooseCandidate(x.api,url,true),/대조가 끝나지/);
  }
});

test('late contradictory Spotify lyrics discard the sole candidate without an automatic loop',async()=>{
  const x=fake(),title='M@STERPIECE (OathOne Recording)',source='https://namu.wiki/w/M%40STERPIECE';
  x.state({title,track:'one',playing:true,reference_lines:[]});await x.watcher.tick();
  const pending=x.session['reader:9'];pending.visited.push(source);pending.searched=true;x.tabs.get(9).url=source;
  const oldFetch=globalThis.fetch;let reads=0,posts=0;
  globalThis.fetch=async(url,options)=>{
    if(options.method==='POST'){posts++;assert.fail('contradictory lyrics cannot connect');}
    assert.ok(++reads<=2,'candidate must not retry itself indefinitely');
    return {ok:true,json:async()=>({title,track:'one',token:'test-session',reference_lines:reads===1?[]:['静かな空を見上げる','新しい道を歩いている']})};
  };
  x.api.scripting.executeScript=async options=>[{result:options.files?undefined:options.func?.toString().includes('readOverlayText')?
    {text:'contradictory synthetic lyrics',url:source,artistMatch:false}:options.args?.length===2?{ok:true}:options.args?.length===1?[]:false}];
  try{await pageLoaded(x.api,9,{...x.tabs.get(9)});}finally{globalThis.fetch=oldFetch;}
  assert.equal(reads,2);assert.equal(posts,0);assert.equal(x.session.spotifyChoices,undefined);
});
test('late Spotify lyrics recheck ambiguous documents once without a reconnect click',async()=>{
  const x=fake();await x.watcher.tick();
  const pending=x.session['reader:9'];
  x.session.spotifyChoices={tabId:9,pending,urls:[x.tabs.get(9).url,'https://namu.wiki/w/Example%20Song(Series)']};
  let retries=[];x.watcher.retry=async retry=>retries.push(retry);
  await x.watcher.tick();assert.equal(retries.length,0);
  x.state({title:'Example Song',track:'one',playing:true,reference_lines:['first reference','second reference']});
  await x.watcher.tick();await x.watcher.tick();
  assert.equal(retries.length,1);
  assert.deepEqual(retries[0].pending.visited,[x.tabs.get(9).url]);
  assert.deepEqual(retries[0].pending.candidates,['https://namu.wiki/w/Example%20Song(Series)']);
  assert.equal(JSON.stringify(x.session).includes('first reference'),false);
});
test('late references do not recheck a displayed or old-track candidate',async()=>{
  const x=fake();await x.watcher.tick();
  x.session.spotifyChoices={tabId:9,pending:x.session['reader:9'],urls:[x.tabs.get(9).url]};
  x.watcher.retry=async()=>assert.fail('must not recheck');
  x.state({title:'Example Song',track:'one',playing:true,reference_lines:['a','b'],receipt:{status:'displayed'}});
  await x.watcher.tick();
  x.session.spotifyChoices.pending={...x.session.spotifyChoices.pending,track:'old'};
  x.state({title:'Example Song',track:'one',playing:true,reference_lines:['a','b']});
  await x.watcher.tick();
});
test('actual display acknowledgement ends retry and reports confirmed display',async()=>{
  const x=fake();await x.watcher.tick();
  x.session.spotifyAwaiting=true;x.session.spotifyRetry.at=0;
  x.watcher.retry=async()=>assert.fail('displayed text needs no retransmission');
  x.state({title:'Example Song',track:'one',playing:true,receipt:{status:'displayed'}});
  await x.watcher.tick();
  assert.equal(x.session.spotifyAwaiting,false);assert.equal(!!x.session.spotifyRetry,false);
  assert.match(x.session.spotifyStatus,/표시 확인됨/);
  assert.equal(x.calls.filter(c=>c[0]==='remove').length,1);
  await x.watcher.tick();assert.equal(x.calls.filter(c=>c[0]==='create').length,1);
});

test('automatic close preserves active pinned navigated and unowned tabs',async()=>{
  for(const condition of ['active','pinned','navigated','unowned','loading']){
    const x=fake();await x.watcher.tick();x.session.spotifyAwaiting=true;
    if(condition==='active')x.tabs.get(9).active=true;
    if(condition==='pinned')x.tabs.get(9).pinned=true;
    if(condition==='navigated')x.tabs.get(9).url='https://namu.wiki/w/Personal';
    if(condition==='unowned')delete x.session.spotifyOwnedTab;
    if(condition==='loading')x.tabs.get(9).pendingUrl='https://example.com/';
    x.state({title:'Example Song',track:'one',playing:true,receipt:{status:'displayed'}});
    await x.watcher.tick();assert.equal(x.calls.filter(c=>c[0]==='remove').length,0,condition);
  }
});

test('automatic close rechecks tab and ownership after its last storage write',async()=>{
  for(const condition of ['active','pinned','navigated','loading','retained','track','tab','source']){
    const x=fake();await x.watcher.tick();
    const original=x.api.storage.session.set;
    x.api.storage.session.set=async value=>{
      await original(value);
      if(value.spotifyClosingTab!==9)return;
      if(condition==='active')x.tabs.get(9).active=true;
      if(condition==='pinned')x.tabs.get(9).pinned=true;
      if(condition==='navigated')x.tabs.get(9).url='https://namu.wiki/w/Personal';
      if(condition==='loading')x.tabs.get(9).pendingUrl='https://example.com/';
      if(condition==='retained')await x.watcher.retained(9);
      if(condition==='track')x.session.spotifyLast='test-session:new-track';
      if(condition==='tab')x.session.spotifyTab=10;
      if(condition==='source')x.session.spotifyURL='https://namu.wiki/w/New';
    };
    await x.watcher.closeConfirmedTab('test-session:one');
    assert.equal(x.calls.filter(c=>c[0]==='remove').length,0,condition);
    assert.equal(x.session.spotifyClosingTab,undefined,condition);
    assert.equal(x.tabs.has(9),true,condition);
  }
});

test('failed automatic close clears only its own closing marker',async()=>{
  const x=fake();await x.watcher.tick();
  x.api.tabs.remove=async()=>{throw new Error('synthetic close failure');};
  await x.watcher.closeConfirmedTab('test-session:one');
  assert.equal(x.session.spotifyClosingTab,undefined);
  assert.equal(x.tabs.has(9),true);
  x.api.tabs.remove=async()=>{x.session.spotifyClosingTab=10;throw new Error('synthetic failure');};
  await x.watcher.closeConfirmedTab('test-session:one');
  assert.equal(x.session.spotifyClosingTab,10);
});

test('retention wins when an ownership snapshot resolves before a stalled tab read',async()=>{
  const x=fake();await x.watcher.tick();
  const original=x.api.tabs.get;let reads=0;
  x.api.tabs.get=async id=>{
    if(++reads===2){
      // The parallel storage read has already captured its old ownership value.
      await Promise.resolve();
      x.tabs.get(id).active=true;await x.watcher.retained(id);x.tabs.get(id).active=false;
    }
    return {...await original(id)};
  };
  await x.watcher.closeConfirmedTab('test-session:one');
  assert.equal(x.calls.filter(c=>c[0]==='remove').length,0);
  assert.equal(x.session.spotifyClosingTab,undefined);
  assert.equal(x.tabs.has(9),true);
});

test('queued failed and unacknowledged transfers keep their source tab',async()=>{
  for(const status of ['queued','display_failed','waiting']){
    const x=fake();await x.watcher.tick();x.session.spotifyAwaiting=true;
    x.state({title:'Example Song',track:'one',playing:true,receipt:{status}});
    await x.watcher.tick();assert.equal(x.calls.filter(c=>c[0]==='remove').length,0,status);
  }
});

test('selected source tab is not navigated on next track',async()=>{
  const x=fake();await x.watcher.tick();const original=x.tabs.get(9).url;x.tabs.get(9).active=true;
  x.state({title:'Next',track:'two',playing:true});await x.watcher.tick();
  assert.equal(x.tabs.get(9).url,original);assert.equal(x.calls.filter(c=>c[0]==='create').length,2);
});

test('a tab the user opened is retained even after returning to another tab',async()=>{
  const x=fake();await x.watcher.tick();const url=x.tabs.get(9).url;
  await x.watcher.retained(9);x.session.spotifyAwaiting=true;
  x.state({title:'Example Song',track:'one',playing:true,receipt:{status:'displayed'}});await x.watcher.tick();
  assert.equal(x.calls.filter(c=>c[0]==='remove').length,0);
  x.state({title:'Next',track:'two',playing:true});await x.watcher.tick();
  assert.equal(x.tabs.get(9).url,url);assert.equal(x.calls.filter(c=>c[0]==='create').length,2);
});

test('native backend does not open Chrome tabs or require its permissions',async()=>{
  const x=fake();x.api.permissions.contains=async()=>false;
  x.state({title:'Example Song',track:'one',playing:true,reader_backend:'internal'});
  await x.watcher.tick();assert.equal(x.calls.filter(c=>c[0]==='create').length,0);
  assert.match(x.session.spotifyStatus,/내부 브라우저/);
});

test('switching back from internal reconnects the same song once',async()=>{
  const x=fake();await x.watcher.tick();
  x.state({title:'Example Song',track:'one',playing:true,reader_backend:'internal'});await x.watcher.tick();
  x.state({title:'Example Song',track:'one',playing:true,reader_backend:'chrome'});await x.watcher.tick();await x.watcher.tick();
  assert.equal(x.calls.filter(c=>c[0]==='navigate').length,2);
});

test('tab removal event does not overwrite confirmed success status',async()=>{
  const x=fake();await x.watcher.tick();x.session.spotifyAwaiting=true;
  x.api.tabs.remove=async id=>{x.tabs.delete(id);await x.watcher.removed(id);};
  x.state({title:'Example Song',track:'one',playing:true,receipt:{status:'displayed'}});
  await x.watcher.tick();assert.match(x.session.spotifyStatus,/표시 확인됨/);
  assert.equal(x.session.spotifyTab,undefined);
});
test('pausing after delivery still confirms display without opening another page',async()=>{
  const x=fake();await x.watcher.tick();
  x.session.spotifyAwaiting=true;x.session.spotifyRetry.at=0;
  x.watcher.retry=async()=>assert.fail('paused display needs no retry');
  x.state({title:'Example Song',track:'one',playing:false,receipt:{status:'displayed'}});
  await x.watcher.tick();await x.watcher.tick();
  assert.equal(x.session.spotifyAwaiting,false);assert.equal(!!x.session.spotifyRetry,false);
  assert.match(x.session.spotifyStatus,/표시 확인됨/);
  assert.equal(x.calls.filter(c=>c[0]==='navigate').length,1);
});
test('a slow response from the previous song cannot replace the new connection state',async()=>{
  const x=fake();await x.watcher.tick();
  const oldFetch=globalThis.fetch;let release,started;
  const posted=new Promise(resolve=>started=resolve);
  globalThis.fetch=async url=>{
    if(url.endsWith('/state'))return {ok:true,json:async()=>({title:'Example Song',track:'one',token:'test-session',reference_lines:[]})};
    started();await new Promise(resolve=>release=resolve);
    return {ok:true,json:async()=>({status:'queued'})};
  };
  x.api.scripting.executeScript=async options=>[{result:options.files?undefined:
    options.func?.toString().includes('readOverlayText')?{text:'synthetic',url:x.tabs.get(9).url,artistMatch:true}:
    options.args?.length===2?{ok:true}:options.args?.length===1?[]:false}];
  let loading;
  try{
    loading=pageLoaded(x.api,9,{...x.tabs.get(9)});await posted;
    x.state({title:'Next Song',track:'two',playing:true});await x.watcher.tick();
    const expected=x.session.spotifyStatus;
    release();await loading;
    assert.equal(x.session.spotifyAwaiting,false);
    assert.equal(x.session.spotifyStatus,expected);
    assert.equal(x.session.spotifyRetry.pending.track,'two');
  }finally{release?.();await loading;globalThis.fetch=oldFetch;}
});
test('queued HTTP delivery retains a display watchdog instead of claiming display',async()=>{
  const x=fake();await x.watcher.tick();
  const oldFetch=globalThis.fetch;
  globalThis.fetch=async url=>({ok:true,json:async()=>url.endsWith('/state')?
    {title:'Example Song',track:'one',token:'test-session',reference_lines:[]}:{status:'queued'}});
  x.api.scripting.executeScript=async options=>[{result:options.files?undefined:
    options.func?.toString().includes('readOverlayText')?{text:'synthetic',url:x.tabs.get(9).url,artistMatch:true}:
    options.args?.length===2?{ok:true}:options.args?.length===1?[]:false}];
  try{await pageLoaded(x.api,9,x.tabs.get(9));}finally{globalThis.fetch=oldFetch;}
  assert.equal(x.session.spotifyAwaiting,true);
  assert.equal(x.session.spotifyRetry.pending.retryCount,1);
  assert.match(x.session.spotifyStatus,/표시 확인 중/);
  assert.ok(x.session.spotifyAttempt);
});
test('failed display remains eligible for bounded automatic retry',async()=>{
  const x=fake();await x.watcher.tick();
  x.session.spotifyAwaiting=true;x.session.spotifyRetry.at=0;
  let count=0;x.watcher.retry=async()=>count++;
  x.state({title:'Example Song',track:'one',playing:true,receipt:{status:'display_failed'}});
  await x.watcher.tick();await x.watcher.tick();
  assert.equal(count,1);assert.match(x.session.spotifyStatus,/표시하지 못/);
});
test('late-reference retry does not act on an access restriction',async()=>{
  const x=fake();await x.watcher.tick();x.session.spotifyRetry=null;
  x.session.spotifyChoices={tabId:9,pending:x.session['reader:9'],urls:[x.tabs.get(9).url]};
  x.session.spotifyStatus='원본 사이트 접근 확인 필요 · 원본 탭을 확인하세요.';
  x.watcher.retry=async()=>assert.fail('do not retry an access barrier');
  x.state({title:'Example Song',track:'one',playing:true,reference_lines:['a','b']});
  await x.watcher.tick();assert.equal(x.session.spotifyReferencesRetried,false);
});

test('version suffix opens the base document without disabling on site redirects',async()=>{
  const x=fake();x.state({title:'リフレクトサイン-2022ver',track:'one',playing:true});await x.watcher.tick();
  assert.equal(decodeURIComponent(x.tabs.get(9).url),'https://namu.wiki/w/リフレクトサイン');
  x.tabs.get(9).url='https://namu.wiki/w/Canonical';
  x.state({title:'Next',track:'two',playing:true});await x.watcher.tick();
  assert.equal(x.local.spotifyEnabled,true);assert.equal(x.calls.filter(c=>c[0]==='create').length,1);
});

test('old automatic stop is recovered once, an intentional off setting stays off',async()=>{
  const {recoverAutomatic}=require('../extension/background.js');const x=fake();
  x.local.spotifyEnabled=false;x.session.spotifyStatus='읽기 창에서 다른 페이지를 열어 자동 연동을 멈췄습니다.';
  await recoverAutomatic(x.api);assert.equal(x.local.spotifyEnabled,true);
  x.local.spotifyEnabled=false;await recoverAutomatic(x.api);assert.equal(x.local.spotifyEnabled,false);
  const y=fake();y.local.spotifyEnabled=false;y.session.spotifyStatus='자동 연동 꺼짐';
  await recoverAutomatic(y.api);assert.equal(y.local.spotifyEnabled,false);
});

test('autocomplete candidates are tried after static candidates and stale discovery cannot navigate',async()=>{
  const {nextCandidate}=require('../extension/background.js');const x=fake();await x.watcher.tick();
  const pending=x.session['reader:9'];
  x.api.scripting.executeScript=async opts=>[{result:opts.func&&opts.args?.[0]===pending.title?['https://namu.wiki/w/Example%20Song(Series)']:{ok:true}}];
  assert.equal(await nextCandidate(x.api,9,pending),true);assert.equal(pending.searched,true);
  assert.equal(x.tabs.get(9).url,'https://namu.wiki/w/Example%20Song(Series)');
  const y=fake();await y.watcher.tick();const old=y.session['reader:9'];
  y.api.scripting.executeScript=async()=>{y.session.spotifyLast='new-song';return [{result:['https://namu.wiki/w/Other']}];};
  assert.equal(await nextCandidate(y.api,9,old),false);
  assert.equal(y.calls.filter(c=>c[0]==='navigate').length,1);
});

test('navigation failure retries after backoff without a permanent deduplication lock',async()=>{
  const x=fake(),update=x.api.tabs.update;let calls=0;
  x.api.tabs.update=async(...args)=>{if(++calls===1)throw new Error('temporary tab failure');return update(...args);};
  await x.watcher.tick();assert.equal(x.session.spotifyLast,undefined);
  await x.watcher.tick();assert.equal(calls,1);
  x.watcher.retryAfter=0;await x.watcher.tick();assert.equal(calls,2);
});

test('candidate selection is scoped to the current song and stored URL list',async()=>{
  const {chooseCandidate}=require('../extension/background.js');const x=fake();await x.watcher.tick();
  const url='https://namu.wiki/w/Example(Series)';
  x.session.spotifyChoices={tabId:9,pending:x.session['reader:9'],urls:[url]};
  await assert.rejects(chooseCandidate(x.api,'https://namu.wiki/w/Unlisted'));
  await chooseCandidate(x.api,url);assert.equal(x.session['reader:9'].selectedURL,url);
  x.session.spotifyLast='new-track';await assert.rejects(chooseCandidate(x.api,url));
});

test('same track transient failure retries automatically without manual reconnection',async()=>{
  const {scheduleRetry}=require('../extension/background.js');const x=fake();await x.watcher.tick();
  const pending=x.session['reader:9'];await scheduleRetry(x.api,9,pending,'Local app network error');
  let count=0;x.watcher.retry=async value=>{count++;assert.equal(value.pending.retryCount,1);};
  await x.watcher.tick();assert.equal(count,0);
  x.session.spotifyRetry.at=0;await x.watcher.tick();assert.equal(count,1);
  await x.watcher.tick();assert.equal(count,1);
});

test('retry stops after success, a song change or access denial',async()=>{
  const {scheduleRetry}=require('../extension/background.js');const x=fake();await x.watcher.tick();
  const pending=x.session['reader:9'];delete x.session.spotifyRetry;
  await scheduleRetry(x.api,9,pending,'HTTP 403 access denied');assert.equal(x.session.spotifyRetry,undefined);
  await scheduleRetry(x.api,9,{...pending,retryCount:3},'Network error');assert.equal(x.session.spotifyRetry,undefined);
  await scheduleRetry(x.api,9,pending,'Network error');x.session.spotifyRetry.at=0;
  x.watcher.retry=async()=>{throw new Error('must not retry after displayed');};
  x.state({title:'Example Song',track:'one',playing:true,receipt:{status:'displayed'}});await x.watcher.tick();
  x.state({title:'Next',track:'two',playing:true});await x.watcher.tick();
  assert.equal(x.session.spotifyRetry.pending.track,'two');
});

test('extension update reconnects the current song once without changing enabled preference',async()=>{
  const {recoverAutomatic}=require('../extension/background.js');const x=fake();
  x.local.spotifyLinkVersion='2.4.0';x.local.spotifyReaderVersion='2.5.5';x.local.spotifyEnabled=false;x.session.spotifyLast='old';
  await recoverAutomatic(x.api);
  assert.equal(x.session.spotifyLast,undefined);assert.equal(x.local.spotifyEnabled,false);
  assert.equal(x.local.spotifyReaderVersion,require('../extension/manifest.json').version);
  x.session.spotifyLast='new';await recoverAutomatic(x.api);assert.equal(x.session.spotifyLast,'new');
});
