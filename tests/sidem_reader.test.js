const test=require('node:test');
const assert=require('node:assert/strict');
const {documentURL,documentName,sameDocumentURL,documentCandidates,sameLyricTitle,classifyOverlayDocument,candidateUrls}=require('../extension/document_match.js');
const {chooseLyricTable}=require('../extension/reader.js');
const {openReader,readerRequest}=require('../extension/popup.js');
const base='https://sidem-ko.fandom.com/ko/wiki/';

test('only the selected Korean SideM wiki is added and its title spaces are normalized',()=>{
  assert.equal(documentURL(base+'Example%20Song#가사'),base+'Example_Song');
  assert.equal(documentName(base+'Example_Song'),'Example Song');
  assert.equal(sameDocumentURL(base+'Example_Song',base+'Example%20Song'),true);
  assert.equal(sameDocumentURL(base+'Example','https://namu.wiki/w/Example'),false);
  for(const bad of ['https://another.fandom.com/ko/wiki/Example','https://sidem-ko.fandom.com/wiki/Example',
    'https://sidem-ko.fandom.com:444/ko/wiki/Example','https://user@sidem-ko.fandom.com/ko/wiki/Example',
    'http://sidem-ko.fandom.com/ko/wiki/Example',base])assert.equal(documentURL(bad),'');
});

test('recording variants discover the base song inside the selected wiki only',()=>{
  const title='Beyond The Dream - GROWING SIGN@L Ver.',current=base+encodeURIComponent(title.replace(/ /g,'_'));
  const candidates=documentCandidates(title,[],[current],current).candidates;
  assert.ok(candidates.includes(base+'Beyond_The_Dream'));
  assert.ok(candidates.every(url=>url.startsWith(base)));
  assert.equal(sameLyricTitle(base+'Beyond_The_Dream',title),true);
  assert.deepEqual(candidateUrls([base+'Beyond_The_Dream','https://namu.wiki/w/Beyond_The_Dream'],title,current),[base+'Beyond_The_Dream']);
});

test('SideM source suggestions never approve contradictory Spotify originals',()=>{
  const value={url:base+'Example_Song',text:'窓を開けて空を見る\n창문을 열어 하늘을 본다\n花を描いて道を歩く\n꽃을 그리고 길을 걷는다'};
  const state={title:'Example Song',suggested_document_url:value.url,reference_lines:['another original phrase','different second phrase']};
  assert.equal(classifyOverlayDocument(value,state).status,'unmatched');
  state.reference_lines=['窓を開けて空を見る','花を描いて道を歩く'];
  assert.equal(classifyOverlayDocument(value,state).status,'matched');
});

test('bilingual and untranslated tables keep original reader selection usable',()=>{
  const pair={text:'窓を開ける\n창문을 열다\n花を描く\n꽃을 그리다'};
  const legend={text:'가사\n텐도 테루\n카시와기 츠바사'};
  assert.equal(chooseLyricTable([legend,pair],[],t=>t.text),pair);
  const original={text:'窓を開ける\n花を描く'};
  assert.equal(chooseLyricTable([legend,original],[],t=>t.text),original);
});

test('SideM pairs outrank untranslated tables with equal reference evidence only on that source',()=>{
  const pairs={text:'Synthetic phrase one\n첫 번째 뜻\nSynthetic phrase two\n두 번째 뜻'};
  const originals={text:'Synthetic phrase one\nSynthetic phrase two\nSynthetic phrase three'};
  for(const refs of [[],['Synthetic phrase one','Synthetic phrase two']]){
    assert.equal(chooseLyricTable([pairs,originals],refs,t=>t.text,'sidem'),pairs);
    assert.equal(chooseLyricTable([pairs,originals],refs,t=>t.text,'namu'),originals);
  }
  assert.equal(chooseLyricTable([pairs,originals],['Synthetic phrase three'],t=>t.text,'sidem'),originals);
  const onePair={text:'Synthetic phrase one\n첫 번째 뜻'};
  assert.equal(chooseLyricTable([onePair],[],t=>t.text,'sidem'),onePair);
  assert.equal(chooseLyricTable([onePair],[],t=>t.text,'namu'),null);
});

test('automatic table reading enables bilingual scoring for the exact SideM page origin only',()=>{
  const fs=require('node:fs'),vm=require('node:vm');
  for(const [origin,pathname,expected] of [
    ['https://sidem-ko.fandom.com','/ko/wiki/Example','sidem'],
    ['https://namu.wiki','/w/Example','namu'],
    ['https://other.fandom.com','/ko/wiki/Example','namu'],
    ['https://sidem-ko.fandom.com','/wiki/Example','namu']]){
    const context={window:{},document:{querySelectorAll:()=>[]},location:{origin,pathname},readOverlayText:()=>''};
    vm.createContext(context);vm.runInContext(fs.readFileSync(require.resolve('../extension/reader.js'),'utf8'),context);
    let source;
    context.chooseLyricTable=(_tables,_references,_reader,value)=>{source=value;throw new Error('test choice reached');};
    assert.throws(()=>context.window.__originalPageReader.apply({autoLyrics:true}),/test choice reached/);
    assert.equal(source,expected);
  }
});

test('manual SideM opening requests only that exact named origin',async()=>{
  const calls=[];
  const api={permissions:{request:async value=>(calls.push(value),true)},runtime:{sendMessage:async value=>(calls.push(value),{ok:true})}};
  await openReader(api,{url:base+'Example_Song',selector:'',size:22});
  assert.deepEqual(calls[0],{origins:['https://sidem-ko.fandom.com/*']});
  assert.equal(calls[1].url,base+'Example_Song');
  assert.throws(()=>readerRequest({url:'https://other.fandom.com/ko/wiki/Example',selector:'',size:22}));
});

test('native page route returns SideM bilingual rows unchanged after original matching',async()=>{
  const fs=require('node:fs'),vm=require('node:vm');
  const references=['窓を開けて空を見る','花を描いて道を歩く'];
  const text=references[0]+'\n창문을 열어 하늘을 본다\n'+references[1]+'\n꽃을 그리고 길을 걷는다';
  const url=base+'Example_Song';
  let selected;
  const context={URL,location:{href:url},window:{__originalPageReader:{apply:async options=>{selected=options;return {ok:true};}}},
    document:{querySelectorAll:()=>[],querySelector:selector=>selector==='.opr-reader-target'?{}:{textContent:''}},
    readOverlayText:()=>text};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(require.resolve('../extension/document_match.js'),'utf8')+'\n'+fs.readFileSync(require.resolve('../native_page.js'),'utf8'),context);
  context.hasVisibleAccessChallenge=()=>false;
  const complete=[...references,...references];
  const result=await context.nativePageRead({title:'Example Song',reference_lines:references,full_reference_lines:complete},{visited:[url],candidates:[]});
  assert.equal(result.status,'matched');assert.equal(result.text,text);assert.equal(result.url,url);
  assert.equal(result.matchMode,'lyrics');
  assert.deepEqual(selected.references,references);assert.deepEqual(selected.fullReferences,complete);
});
