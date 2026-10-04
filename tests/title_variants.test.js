const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
const {documentTitle,recordingBase,titleCandidates,searchPrefixes,sameDocumentTitle,sameLyricTitle,candidateUrls,documentCandidates, titleAliasEvidence,classifyOverlayDocument}=require('../extension/document_match.js');
const url=name=>'https://namu.wiki/w/'+encodeURIComponent(name);

test('game and TV sizes discover a base without approving the full recording',()=>{
  for(const size of ['Game Size','TV Size']){
    const title='Synthetic Song - '+size;
    assert.ok(titleCandidates(title).includes('Synthetic Song'));
    assert.equal(recordingBase(title),title);
    assert.equal(sameLyricTitle(url('Synthetic Song'),title),false);
    assert.equal(titleAliasEvidence(url('Synthetic Song'),title,'Example','Synthetic Song Example'),null);
    assert.ok(!titleCandidates(title+' - Off Vocal').includes('Synthetic Song'));
  }
});

test('shared receiver fixtures preserve compact year suffixes and reject other performers',()=>{
  const cases=require('./title_variants.json');
  for(const [title,base] of cases.versions)assert.equal(recordingBase(title),base,title);
  for(const [name,title,expected] of cases.comparisons)assert.equal(sameLyricTitle(url(name),title),expected,title+' → '+name);
});

test('version suffixes share one rule across songs, performers, brackets and dash spellings',()=>{
  const rows=[
    ['Crossing! (如月千早 Ver.)','Crossing!'],
    ['Other Song (Different Singer Version)','Other Song'],
    ['蒼い鳥 (OathOne Recording)','蒼い鳥'],
    ['Song（別の歌手 Ｖｅｒ．）','Song'],
    ['Song [Singer Version]','Song'],
    ['Song (Singer (CV. Someone) Ver.)','Song'],
    ['Song (Singer Ver.) [Remastered]','Song'],
    ['Song (Singer Ver. 2024)','Song'],
    ['Song (가수 버전)','Song'],
    ['Song (歌手バージョン)','Song'],
    ['Song - Singer Ver.','Song'],
    ['Song – Singer Recording','Song'],
    ['Song — Remastered 2024','Song'],
    ['Song - Another Story - Singer Ver.','Song - Another Story'],
  ];
  for(const [title,base] of rows){
    assert.equal(recordingBase(title),base,title);
    assert.equal(sameLyricTitle(url(base),title),true,title);
    assert.equal(titleCandidates(title).at(-1),base,title);
  }
  assert.equal(documentTitle('Song (Singer Ver. 2024)'),'Song (Singer Ver. 2024)');
  assert.equal(documentTitle('Song（Singer Ver.）'),'Song（Singer Ver.）');
});

test('candidate priority preserves exact titles and meaningful parentheses before broad fallback',()=>{
  const title='Song (Another Story) (Singer Ver.)';
  assert.deepEqual(titleCandidates(title),[title,'Song (Another Story)','Song']);
  assert.equal(recordingBase(title),'Song (Another Story)');
  assert.equal(sameLyricTitle(url('Song'),title),false);
  assert.equal(sameLyricTitle(url('Song (Another Story)'),title),true);
  assert.deepEqual(candidateUrls([url('Song'),url('Song (Another Story)'),url(title)],title,''),
    [url(title),url('Song (Another Story)'),url('Song')]);
  assert.deepEqual(titleCandidates('Song (Singer Ver.) [Game]'),['Song (Singer Ver.) [Game]','Song (Singer Ver.)','Song']);
  assert.equal(sameLyricTitle(url('Song'),'Song (Singer Ver.) [Game]'),false);
  assert.equal(recordingBase('Song (Live Mix)'),'Song (Live Mix)');
});

test('search prefixes include version and bracket bases before shortened guesses',()=>{
  for(const title of ['Crossing! [如月千早 Version]','Crossing!（如月千早 Ver.）','Crossing! - Singer Ver.']){
    assert.deepEqual(searchPrefixes(title).slice(0,2),[title,'Crossing!']);
    assert.ok(searchPrefixes(title).length<=6);
  }
  assert.deepEqual(searchPrefixes('Song (Another Story) (Singer Ver.)').slice(0,3),
    ['Song (Another Story) (Singer Ver.)','Song (Another Story)','Song']);
});

test('numbered colors editions add the base as a search candidate while preserving recording identity',()=>{
  const base='Spread the Wings!!';
  for(const count of [19,25]){
    const title=base+' -'+count+' colors-';
    assert.deepEqual(titleCandidates(title),[title,base]);
    assert.equal(recordingBase(title),title);
    assert.equal(sameLyricTitle(url(base),title),false);
    assert.deepEqual(searchPrefixes(title).slice(0,2),[title,base]);
    assert.deepEqual(documentCandidates(title,[],[url(title)],url(title)).candidates,[url(base)]);
    assert.deepEqual(candidateUrls([url(base),url(title)],title,''),[url(title),url(base)]);
  }
  for(const title of [base+' －２５ ｃｏｌｏｒｓ－',base+' – 25 COLORS –',base+'-19 colors-',base+' -25 colors- (2024 Remaster)']){
    assert.ok(titleCandidates(title).includes(base),title);
    assert.equal(sameLyricTitle(url(base),title),false,title);
  }
});

test('derived discovery URLs preserve compatibility title spelling',()=>{
  for(const base of ['Resonance⁺','ﬂower song','Title…']){
    const title=base+' -25 colors-';
    assert.deepEqual(titleCandidates(title),[title,base]);
    assert.deepEqual(documentCandidates(title,[],[url(title)],url(title)).candidates,[url(base)]);
    assert.deepEqual(searchPrefixes(title).slice(0,2),[title,base]);
    assert.equal(sameLyricTitle(url(base),title),false);
    assert.equal(classifyOverlayDocument({url:url(base),text:'',artistMatch:true},
      {title,artist:'Example Group',reference_lines:[]},{searched:true}).status,'needs_confirmation');
  }
  const title='Resonance⁺（Singer Ｖｅｒ．）';
  assert.equal(titleCandidates(title).at(-1),'Resonance⁺');
});

test('colors search candidates need matching lyrics or manual selection even when they are the sole candidate',()=>{
  const base='Spread the Wings!!',title=base+' -25 colors-',artist='Example Ensemble',source=url(base);
  const references=['synthetic phrase number one','synthetic phrase number two'];
  const state={title,artist,reference_lines:[]};
  const value={url:source,text:references.join('\n'),artistMatch:true};
  const pending={searched:true,uniqueTitleURL:source,candidates:[],visited:[url(title),source]};
  const article=title+'\n'+artist;
  assert.equal(titleAliasEvidence(source,title,artist,article),null);
  assert.equal(classifyOverlayDocument(value,state,pending).status,'needs_confirmation');
  assert.equal(classifyOverlayDocument({...value,titleAlias:{title,artist}},state,pending).status,'needs_confirmation');
  assert.equal(classifyOverlayDocument(value,{...state,reference_lines:references},pending).status,'matched');
  assert.equal(classifyOverlayDocument({...value,text:'unrelated source'},{...state,reference_lines:references},pending).status,'unmatched');
  assert.equal(classifyOverlayDocument(value,state,{...pending,selectedURL:source}).status,'matched');
  assert.equal(classifyOverlayDocument({...value,url:url(title),artistMatch:false},state,{candidates:[source]}).matchMode,'title');
});

test('colors discovery does not remove off-vocal or other meaningful suffixes',()=>{
  for(const title of ['Synthetic Song -25 colors- - Off Vocal','Synthetic Song -25 colors- (Off Vocal)',
    'Synthetic Song -25 colors- instrumental','Synthetic Song -blue colors-',
    'Synthetic Song -25 colors- Another Story','Synthetic Song (25 colors)']){
    assert.equal(recordingBase(title),title);
    assert.equal(sameLyricTitle(url('Synthetic Song'),title),false);
    if(!title.endsWith('(25 colors)'))assert.ok(!titleCandidates(title).includes('Synthetic Song'),title);
  }
  const title='Synthetic Song (Another Story) -25 colors-';
  assert.deepEqual(titleCandidates(title),[title,'Synthetic Song (Another Story)']);
  assert.equal(titleCandidates(' -25 colors-').some(candidate=>!candidate.trim()),false);
});

test('parenthetical subtitles beneath a long arrangement suffix already reach the short title candidate',()=>{
  const titles=[
    ['アイ NEED YOU（FOR WONDERFUL STORY） - THE IDOLM@STER 20th Anniversary Piano Arrange Version','アイ NEED YOU'],
    ['Synthetic Song [Another Story] - Anniversary Piano Arrange Version','Synthetic Song'],
  ];
  const references=['synthetic phrase number one','synthetic phrase number two'];
  for(const [title,base] of titles){
    const source=url(base),pending={searched:true,uniqueTitleURL:source,candidates:[],visited:[url(title),source]};
    assert.equal(titleCandidates(title).at(-1),base);
    assert.ok(searchPrefixes(title).includes(base));
    assert.ok(documentCandidates(title,[],[url(title)],url(title)).candidates.includes(source));
    const value={url:source,text:references.join('\n'),artistMatch:true};
    assert.equal(classifyOverlayDocument(value,{title,reference_lines:[]},pending).status,'needs_confirmation');
    assert.equal(classifyOverlayDocument(value,{title,reference_lines:references},pending).status,'matched');
    assert.equal(classifyOverlayDocument({...value,text:'unrelated source'},{title,reference_lines:references},pending).status,'unmatched');
    assert.equal(classifyOverlayDocument(value,{title,reference_lines:[]},{...pending,selectedURL:source}).status,'matched');
  }
});

test('generic subtitle aliases need both the formal title and current artist in the article',()=>{
  const title='アイ NEED YOU（FOR WONDERFUL STORY） - THE IDOLM@STER 20th Anniversary Piano Arrange Version';
  const source=url('アイ NEED YOU'),artist='Example Artist (CV. Someone)',formal=recordingBase(title);
  const article='Official title: '+formal+'\nPerformed by Example Artist';
  const evidence=titleAliasEvidence(source,title,artist,article);
  assert.ok(evidence);
  const state={title,artist,reference_lines:[]},value={url:source,text:'synthetic lyrics',artistMatch:true,titleAlias:evidence};
  assert.equal(classifyOverlayDocument(value,state).matchMode,'metadata_alias');
  for(const text of [formal,'Example Artist','アイ NEED YOU\nExample Artist','Another title\nExample Artist']){
    assert.equal(titleAliasEvidence(source,title,artist,text),null);
  }
  for(const changed of [null,{...evidence,title:'アイ NEED YOU'},{...evidence,artist:'Other Artist'}]){
    assert.equal(classifyOverlayDocument({...value,titleAlias:changed},state).status,'needs_confirmation');
  }
  for(const name of ['Unrelated Song','アイ NEED YOU (Other Singer Ver.)']){
    assert.equal(titleAliasEvidence(url(name),title,artist,article),null);
    assert.equal(classifyOverlayDocument({...value,url:url(name)},state).status,'unmatched');
  }
  assert.equal(classifyOverlayDocument(value,{...state,reference_lines:['different source phrase one','different source phrase two']}).status,'unmatched');
});

test('a readable exact document takes priority over weaker fallback candidates',()=>{
  for(const title of ['Crossing! (如月千早 Ver.)','Song (Another Story)']){
    const source=url(title),fallback=url(titleCandidates(title).at(-1));
    const result=classifyOverlayDocument({url:source,text:'synthetic',artistMatch:false},
      {title,reference_lines:[]},{candidates:[fallback],visited:[source]});
    assert.equal(result.status,'matched');assert.equal(result.matchMode,'title');
  }
});

test('different performer versions cannot become a title or metadata match through the base title',()=>{
  for(const name of ['Song (Other Singer Ver.)','Song [Other Singer Version]','Song - Other Singer Recording',
    'Song (Other Singer Ver.) (Game)']){
    assert.equal(sameDocumentTitle(url(name),'Song'),false,name);
    assert.equal(sameLyricTitle(url(name),'Song (Singer Ver.)'),false,name);
    assert.equal(classifyOverlayDocument({url:url(name),text:'synthetic',artistMatch:true},
      {title:'Song (Singer Ver.)',reference_lines:[]}).status,'unmatched',name);
  }
  assert.equal(sameDocumentTitle(url('Song (Singer Ver.) [Game]'),'Song (Singer Ver.)'),true);
});

test('unknown parenthetical fallback stays selectable until lyrics or explicit selection confirm it',()=>{
  const title='Song (Another Story)',source=url('Song');
  const references=['synthetic phrase number one','synthetic phrase number two'];
  const value={url:source,text:references.join('\n'),artistMatch:true};
  const pending={searched:true,uniqueTitleURL:source,candidates:[],visited:[url(title),source]};
  assert.equal(classifyOverlayDocument(value,{title,reference_lines:[]},pending).status,'needs_confirmation');
  assert.equal(classifyOverlayDocument(value,{title,reference_lines:references},pending).status,'matched');
  assert.equal(classifyOverlayDocument(value,{title,reference_lines:[]},{...pending,selectedURL:source}).status,'matched');
  assert.equal(classifyOverlayDocument({...value,text:'wrong source'},{title,reference_lines:references},pending).status,'unmatched');
});

test('known version fallback accepts artist evidence or exhausted discovery and rejects conflicting phrases',()=>{
  const title='Crossing! (如月千早 Ver.)',source=url('Crossing!');
  const state={title,reference_lines:[]},value={url:source,text:'synthetic',artistMatch:false};
  assert.equal(classifyOverlayDocument({...value,artistMatch:true},state).matchMode,'metadata');
  assert.equal(classifyOverlayDocument(value,state).status,'needs_confirmation');
  const pending={searched:true,uniqueTitleURL:source,candidates:[],visited:[url(title),source]};
  assert.equal(classifyOverlayDocument(value,state,pending).matchMode,'unique_title');
  assert.equal(classifyOverlayDocument(value,state,{...pending,unexamined:true}).status,'needs_confirmation');
  assert.equal(classifyOverlayDocument({...value,artistMatch:true},{...state,reference_lines:['conflicting phrase one','conflicting phrase two']},pending).status,'unmatched');
});

test('learned document mappings recheck new phrases while manual selections retain approval',()=>{
  const title='Song (Singer Ver.)',source=url('Song');
  const value={url:source,text:'synthetic source',artistMatch:true};
  const state={title,document_url:source,reference_lines:['conflicting phrase one','conflicting phrase two']};
  assert.equal(classifyOverlayDocument(value,{...state,document_learned:false}).status,'matched');
  assert.equal(classifyOverlayDocument(value,{...state,document_learned:true}).status,'unmatched');
  assert.equal(classifyOverlayDocument(value,{...state,reference_lines:[],document_learned:true}).status,'matched');
});

function nativeContext(title,links=[]){
  const context={URL,location:{href:url(title)},window:{__originalPageReader:{apply:async()=>{throw new Error('no table');}}},
    document:{querySelectorAll:()=>links},readOverlayText:()=>''};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(require.resolve('../extension/document_match.js'),'utf8')+'\n'+
    fs.readFileSync(require.resolve('../native_page.js'),'utf8'),context);
  context.hasVisibleAccessChallenge=()=>false;
  return context;
}

test('wave and fullwidth tilde titles try the ASCII wiki path without page suggestions',async()=>{
  const source=url('Synthetic Song ~Another Story~');
  for(const mark of ['〜','～']){
    const title=`Synthetic Song ${mark}Another Story${mark}`,current=url(title);
    const other=url(`Synthetic Song ${mark==='〜'?'～':'〜'}Another Story${mark==='〜'?'～':'〜'}`);
    assert.deepEqual(documentCandidates(title,[]).candidates,[current,source,other]);
    assert.deepEqual(documentCandidates(title,[],[current],current).candidates,[source,other]);
    assert.deepEqual(documentCandidates(title,[],[current,source,other],source).candidates,[]);
    const context=nativeContext(title);
    const result=await context.nativePageRead({title,reference_lines:[]},{visited:[current],candidates:[]});
    assert.equal(result.status,'no_table');
    assert.deepEqual(Array.from(result.candidates),[source,other]);
    const value={url:source,text:'synthetic source',artistMatch:true};
    assert.equal(classifyOverlayDocument(value,{title,reference_lines:[]}).status,'matched');
    assert.equal(classifyOverlayDocument(value,{title,reference_lines:['conflicting phrase one','conflicting phrase two']}).status,'unmatched');
  }
});

test('native discovery retries the original compatibility path after a normalized path has no table',async()=>{
  for(const [base,wrong] of [['Resonance⁺','Resonance+'],['ﬂower song','flower song'],['Title…','Title...']]){
    const title=base+' -25 colors-',context=nativeContext(wrong);
    const result=await context.nativePageRead({title,reference_lines:[]},
      {visited:[url(title),url(wrong)],candidates:[]});
    assert.equal(result.status,'no_table');
    assert.deepEqual(Array.from(result.candidates),[url(base)]);
    assert.deepEqual(candidateUrls([url(base),url(wrong)],title,url(wrong),[url(wrong)]),[url(base)]);
  }
});

test('native reader forwards only the alias evidence used for an approved subtitle document',async()=>{
  const title='Synthetic Song (Another Story) - Anniversary Piano Arrange Version';
  const context=nativeContext('Synthetic Song');
  context.window.__originalPageReader.apply=async()=>({ok:true});
  context.document.querySelector=selector=>selector==='.opr-reader-target'?{}:{textContent:'Synthetic Song (Another Story)\nExample Artist'};
  context.readOverlayText=()=> 'synthetic lyrics';
  const result=await context.nativePageRead({title,artist:'Example Artist',reference_lines:[]},{visited:[url(title)],candidates:[]});
  assert.equal(result.status,'matched');assert.equal(result.matchMode,'metadata_alias');
  assert.deepEqual({...result.titleAlias},titleAliasEvidence(url('Synthetic Song'),title,'Example Artist','Synthetic Song (Another Story)\nExample Artist'));
  assert.equal(result.text,'synthetic lyrics');
});

test('extension forwards compact alias evidence with metadata_alias approval',async()=>{
  const {transferToOverlay}=require('../extension/overlay_transfer.js');
  const title='Synthetic Song (Another Story) - Anniversary Piano Arrange Version',source=url('Synthetic Song');
  const titleAlias=titleAliasEvidence(source,title,'Example Artist','Synthetic Song (Another Story)\nExample Artist');
  const state={track:'track',token:'token',title,artist:'Example Artist',reference_lines:[]};
  let payload;
  const api={runtime:{id:'a'.repeat(32)},storage:{local:{get:async()=>({spotifyEnabled:true})}},
    scripting:{executeScript:async options=>{
      if(options.func)assert.equal(options.args[3],title);
      return [{result:{url:source,text:'synthetic lyrics',artistMatch:true,titleAlias}}];
    }}};
  const reply=await transferToOverlay(api,7,state,async(address,options)=>{
    if(options.method==='POST'){payload=JSON.parse(options.body);return {ok:true,json:async()=>({status:'queued'})};}
    return {ok:true,json:async()=>state};
  });
  assert.equal(reply.status,'queued');assert.equal(payload.match_mode,'metadata_alias');
  assert.deepEqual(payload.title_alias,titleAlias);
});

test('native discovery queues known versions, colors editions and generic parentheses through the shared title order',async()=>{
  for(const title of ['Crossing! (如月千早 Ver.)','Other Song [Other Singer Version]','Song (Another Story) (Singer Ver.)',
    'Spread the Wings!! -25 colors-','Spread the Wings!! -19 colors-']){
    const context=nativeContext(title),result=await context.nativePageRead({title,reference_lines:[]},{visited:[url(title)],candidates:[]});
    assert.equal(result.status,'no_table');
    assert.equal(result.url,context.location.href);
    assert.deepEqual(Array.from(result.candidates),titleCandidates(title).slice(1).map(url));
  }
});

test('many discovered links retain the direct base and cannot claim that search was exhaustive',async()=>{
  const title='Song (Singer Ver.)',links=Array.from({length:12},(_,i)=>url('Song (Series '+i+')'));
  const discovered=candidateUrls(links,title,url(title));
  assert.equal(discovered.length,9);
  const queued=documentCandidates(title,discovered,[url(title)],url(title));
  assert.equal(queued.candidates[0],url('Song'));assert.equal(queued.candidates.length,8);assert.equal(queued.unexamined,true);
  const context=nativeContext(title,links),result=await context.nativePageRead({title,reference_lines:[]},{visited:[url(title)],candidates:[]});
  assert.equal(result.candidates[0],url('Song'));assert.equal(result.unexamined,true);
});
