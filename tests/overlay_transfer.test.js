const test=require('node:test');
const assert=require('node:assert/strict');
const {transferToOverlay}=require('../extension/overlay_transfer.js');
const {readOverlayText}=require('../extension/lyric_text.js');
function fixture(){
  const calls=[];
  const fail=()=>{throw new Error('page text must not be stored');};
  const api={runtime:{id:'a'.repeat(32)},storage:{local:{get:async()=>({spotifyEnabled:true}),set:fail},session:{set:fail}},
    scripting:{executeScript:async()=>{calls.push('read');return [{result:{text:'synthetic test lyrics',url:'https://namu.wiki/w/Test'}}];}}};
  const pending={track:'one',token:'session'};
  const request=async(url,options)=>{calls.push({url,options});return {ok:true,json:async()=>url.endsWith('/state')?pending:{status:'queued'}};};
  return {api,pending,request,calls};
}
test('approved text transfer only goes to loopback POST without storage calls',async()=>{
  const f=fixture();const result=await transferToOverlay(f.api,9,f.pending,f.request);
  assert.equal(result.status,'queued');
  const requests=f.calls.filter(x=>typeof x==='object');
  assert.deepEqual(requests.map(x=>x.url),['http://127.0.0.1:47831/state','http://127.0.0.1:47831/lyrics']);
  assert.equal(JSON.parse(requests[1].options.body).text,'synthetic test lyrics');
});
test('stale track or restarted app is rejected before reading text',async()=>{
  for(const update of [{track:'two'},{token:'old'}]){
    const f=fixture();const result=await transferToOverlay(f.api,9,{...f.pending,...update},f.request);
    assert.equal(result.status,'stale');assert.equal(f.calls.includes('read'),false);
  }
});
test('disabled mode never reads or sends page text',async()=>{
  const f=fixture();f.api.storage.local.get=async()=>({spotifyEnabled:false});
  assert.equal((await transferToOverlay(f.api,9,f.pending,f.request)).status,'disabled');
  assert.equal(f.calls.length,0);
});
test('ruby readings and footnotes do not contaminate original lyric matching',()=>{
  const text=value=>({nodeType:3,nodeValue:value});
  const element=(tag,...children)=>({nodeType:1,tagName:tag,childNodes:children});
  const root=element('TD',element('RUBY',text('窓'),element('RT',text('まど'))),text('を開ける'),element('SUP',text('[1]')),
    element('BR'),text('마도오 아케루'),element('BR'),text('창문을 열다'));
  const before=JSON.stringify(root);
  assert.equal(readOverlayText(root,()=>true),'窓を開ける\n마도오 아케루\n창문을 열다');
  assert.equal(JSON.stringify(root),before);
});
test('matched table color markup accompanies the existing local lyric delivery',async()=>{
  const f=fixture();
  f.api.scripting.executeScript=async()=>[{result:{text:'synthetic test lyrics',url:'https://namu.wiki/w/Test',
    table_html:'<table><td style="color:#ee762e">synthetic text</td></table>'}}];
  await transferToOverlay(f.api,9,f.pending,f.request);
  const post=f.calls.find(x=>typeof x==='object'&&x.url.endsWith('/lyrics'));
  assert.equal(JSON.parse(post.options.body).table_html,'<table><td style="color:#ee762e">synthetic text</td></table>');
});
test('large optional color markup never makes a valid UTF8 lyric payload too large',async()=>{
  const f=fixture();const text='空'.repeat(170000);
  f.api.scripting.executeScript=async()=>[{result:{text,url:'https://namu.wiki/w/Test',table_html:'<table>'+text+'</table>'}}];
  await transferToOverlay(f.api,9,f.pending,f.request);
  const post=f.calls.find(x=>typeof x==='object'&&x.url.endsWith('/lyrics'));
  assert.ok(new TextEncoder().encode(post.options.body).length<=1000000);
  assert.equal(JSON.parse(post.options.body).text,text);
  assert.equal(JSON.parse(post.options.body).table_html,undefined);
});

test('hidden descriptions and performer tooltips cannot split lyric translation groups',()=>{
  const text=value=>({nodeType:3,nodeValue:value});
  const element=(tag,attrs={},...children)=>({nodeType:1,tagName:tag,childNodes:children,
    getAttribute:name=>attrs[name]??null,style:attrs.style||{}});
  const artifacts=[
    element('SPAN',{style:{visibility:'hidden'}},text('숨은 설명')),
    element('SPAN',{style:{visibility:'collapse'}},text('숨은 설명')),
    element('SPAN',{style:{opacity:'0'}},text('숨은 설명')),
    element('SPAN',{'aria-hidden':'true'},text('보조 설명')),
    element('SPAN',{hidden:''},text('숨은 설명')),
    element('DIV',{role:'tooltip'},text('Example Ensemble'),element('BR'),text('예시 합창단')),
    element('NOSCRIPT',{},text('보조 설명')),
    element('TEMPLATE',{},text('보조 설명')),
  ];
  const root=element('TD',{},text('窓を開ける'),element('BR'),...artifacts,
    text('마도오 아케루'),element('BR'),text('창문을 열다'));
  const before=JSON.stringify(root);
  assert.equal(readOverlayText(root,()=>true,node=>node.style||{}),'窓を開ける\n마도오 아케루\n창문을 열다');
  assert.equal(JSON.stringify(root),before);
});

test('inline layout keeps styled lyric fragments together and real breaks intact',()=>{
  const text=value=>({nodeType:3,nodeValue:value});
  const element=(tag,display,...children)=>({nodeType:1,tagName:tag,style:{display},childNodes:children});
  const root=element('TD','table-cell',text('窓を'),element('DIV','inline',text('開ける')),
    element('BR','inline'),text('마도오 아케루'),element('BR','inline'),text('창문을 '),element('DIV','inline',text('열다')),
    element('DIV','block',text('Open the window')));
  assert.equal(readOverlayText(root,()=>true,node=>node.style),'窓を開ける\n마도오 아케루\n창문을 열다\nOpen the window');
});


test('lyric comparison requires two distinct matching phrases',()=>{
  const {matchesReference,candidateUrls}=require('../extension/document_match.js');
  const text='静かな空を見上げる\n新しい道を歩いている';
  assert.equal(matchesReference(text,['静かな空を見上げる','新しい道を歩いている']),true);
  assert.equal(matchesReference(text,['静かな空を見上げる','静かな空を見上げる']),false);
  assert.equal(matchesReference(text,['別の人が歌う言葉','違う場所の物語']),false);
  assert.deepEqual(candidateUrls(['https://namu.wiki/w/Same(Series)','https://namu.wiki/w/Other','https://example.com/w/Same'], 'Same','https://namu.wiki/w/Same'),['https://namu.wiki/w/Same(Series)']);
});

test('mismatched Spotify phrases never send source text to the app',async()=>{
  const f=fixture();let posts=0;
  const result=await transferToOverlay(f.api,9,f.pending,async(url,options)=>{
    if(options.method==='POST')posts++;
    return {ok:true,json:async()=>({...f.pending,reference_lines:['静かな空を見上げる','新しい道を歩いている']})};
  });
  assert.equal(result.status,'unmatched');assert.equal(posts,0);
});

test('automatic selection receives complete original order separately from matching hints',async()=>{
  const vm=require('node:vm'),f=fixture(),complete=['Synthetic phrase one','Synthetic phrase two','Synthetic phrase one'];
  f.pending.full_reference_lines=complete;f.pending.options={autoLyrics:true};
  let selected;
  f.api.scripting.executeScript=async request=>{
    if(!request.func)return [];
    const context={args:request.args,window:{__originalPageReader:{apply:async options=>{selected=options;}}},
      document:{querySelector:()=>({textContent:'',outerHTML:'<table></table>'})},
      location:{href:'https://namu.wiki/w/Test'},readOverlayText:()=> 'synthetic test lyrics',
      artistInPage:()=>false,titleAliasEvidence:()=>null};
    return [{result:await vm.runInNewContext('('+request.func.toString()+')(...args)',context)}];
  };
  assert.equal((await transferToOverlay(f.api,9,f.pending,f.request)).status,'queued');
  assert.deepEqual(selected.references,[]);
  assert.deepEqual(selected.fullReferences,complete);
});

test('document titles strip release suffixes and preserve meaningful title text',()=>{
  const {documentTitle,searchPrefixes,candidateUrls}=require('../extension/document_match.js');
  for(const suffix of ['-2022ver',' - 2022 Ver.',' (2022 version)',' – Remastered 2022'])assert.equal(documentTitle('リフレクトサイン'+suffix),'リフレクトサイン');
  for(const title of ['Embers glow','Song (Another Story)','2022','Song-2022'])assert.equal(documentTitle(title),title);
  const prefixes=searchPrefixes('リフレクトサイン-2022ver');
  assert.equal(prefixes[0],'リフレクトサイン');assert.ok(prefixes.some(x=>x.length<8));
  assert.deepEqual(candidateUrls(['https://namu.wiki/w/リフレクトサイン(Series)'],'リフレクトサイン-2022ver',''),['https://namu.wiki/w/%E3%83%AA%E3%83%95%E3%83%AC%E3%82%AF%E3%83%88%E3%82%B5%E3%82%A4%E3%83%B3(Series)']);
});

test('without Spotify lyrics a unique title connects, while ambiguous candidates need selection',async()=>{
  for(const [candidates,artistMatch,selected,expected] of [[[],false,false,'queued'],[['https://namu.wiki/w/Test(Other)'],false,false,'needs_confirmation'],[['https://namu.wiki/w/Test(Other)'],true,false,'queued'],[['https://namu.wiki/w/Test(Other)'],false,true,'queued']]){
    const f=fixture();f.pending.candidates=candidates;f.pending.visited=['https://namu.wiki/w/Test'];
    if(selected)f.pending.selectedURL='https://namu.wiki/w/Test';
    f.api.scripting.executeScript=async()=>[{result:{text:'synthetic',url:'https://namu.wiki/w/Test',artistMatch}}];
    let payload;
    const reply=await transferToOverlay(f.api,9,f.pending,async(url,options)=>{
      if(options.method==='POST'){payload=JSON.parse(options.body);return {ok:true,json:async()=>({status:'queued'})};}
      return {ok:true,json:async()=>({...f.pending,title:'Test',artist:'Example Artist',reference_lines:[]})};
    });
    assert.equal(reply.status,expected);
    if(expected==='needs_confirmation')assert.equal(payload,undefined);
    else assert.equal(payload.user_selected,selected);
  }
});

test('metadata fallback cannot bypass mismatching Spotify lyrics',async()=>{
  const f=fixture();let posts=0;
  f.api.scripting.executeScript=async()=>[{result:{text:'wrong lyrics',url:'https://namu.wiki/w/Test',artistMatch:true}}];
  const reply=await transferToOverlay(f.api,9,f.pending,async(url,options)=>{
    if(options.method==='POST')posts++;
    return {ok:true,json:async()=>({...f.pending,title:'Test',reference_lines:['静かな空を見上げる','新しい道を歩いている']})};
  });
  assert.equal(reply.status,'unmatched');assert.equal(posts,0);
});

test('metadata comparison handles qualified titles and voice actor suffixes',()=>{
  const {sameDocumentTitle,artistInPage}=require('../extension/document_match.js');
  assert.equal(sameDocumentTitle('https://namu.wiki/w/輝夜(Series)','輝夜'),true);
  assert.equal(sameDocumentTitle('https://namu.wiki/w/Other','輝夜'),false);
  assert.equal(artistInPage('Example Singer (CV. Someone)','Performed by Example Singer.'),true);
  assert.equal(artistInPage('Other Singer','Performed by Example Singer.'),false);
});

test('recording titles match the complete document name without mixing other versions',()=>{
  const {sameDocumentTitle,candidateUrls}=require('../extension/document_match.js');
  const title='蒼い鳥 (OathOne Recording)',url='https://namu.wiki/w/'+encodeURIComponent(title);
  const other='https://namu.wiki/w/'+encodeURIComponent('蒼い鳥 (Other Recording)');
  const base='https://namu.wiki/w/'+encodeURIComponent('蒼い鳥');
  assert.equal(sameDocumentTitle(url,title),true);
  assert.equal(sameDocumentTitle(url,'蒼い鳥（OathOne Recording）'),true);
  assert.equal(sameDocumentTitle(base,title),false);
  assert.equal(sameDocumentTitle(other,title),false);
  assert.deepEqual(candidateUrls([base,other,url,url+'#s-3'],title,''),[url,base]);
  assert.deepEqual(candidateUrls([url],title,url),[]);
});

test('recording fallback is shared across song titles and keeps meaningful parentheses',()=>{
  const {recordingBase,sameLyricTitle,candidateUrls}=require('../extension/document_match.js');
  for(const name of ['蒼い鳥','眠り姫','Synthetic Song']){
    const title=name+' (OathOne Recording)',base='https://namu.wiki/w/'+encodeURIComponent(name);
    assert.equal(recordingBase(title),name);assert.equal(recordingBase(name+'（Another Recording）'),name);
    assert.equal(sameLyricTitle(base,title),true);
    assert.equal(sameLyricTitle(base+'(Series)',title),true);
    assert.deepEqual(candidateUrls([base],title,''),[base]);
    assert.equal(sameLyricTitle('https://namu.wiki/w/Unrelated',title),false);
  }
  assert.equal(recordingBase('Song (Another Story)'),'Song (Another Story)');
  assert.equal(sameLyricTitle('https://namu.wiki/w/Song','Song (Another Story)'),false);
});

test('original lyrics for a recording need artist evidence when Spotify references are absent',async()=>{
  for(const name of ['蒼い鳥','眠り姫'])for(const artistMatch of [true,false]){
    const f=fixture(),title=name+' (OathOne Recording)',source='https://namu.wiki/w/'+encodeURIComponent(name);
    f.api.scripting.executeScript=async()=>[{result:{text:'synthetic test lyrics',url:source,artistMatch}}];
    let payload;
    const reply=await transferToOverlay(f.api,9,f.pending,async(url,options)=>{
      if(options.method==='POST'){payload=JSON.parse(options.body);return {ok:true,json:async()=>({status:'queued'})};}
      return {ok:true,json:async()=>({...f.pending,title,reference_lines:[]})};
    });
    assert.equal(reply.status,artistMatch?'queued':'needs_confirmation');
    if(artistMatch){assert.equal(payload.match_mode,'metadata');assert.equal(payload.track,f.pending.track);assert.equal(payload.user_selected,false);}
    else assert.equal(payload,undefined);
  }
});

test('recording fallback accepts matching lyrics but never overrides contradictory Spotify lyrics',async()=>{
  for(const matches of [true,false]){
    const f=fixture(),references=['静かな空を見上げる','新しい道を歩いている'];
    f.api.scripting.executeScript=async()=>[{result:{text:matches?references.join('\n'):'unrelated lyrics',url:'https://namu.wiki/w/'+encodeURIComponent('眠り姫'),artistMatch:true}}];
    let posts=0;
    const reply=await transferToOverlay(f.api,9,f.pending,async(url,options)=>{
      if(options.method==='POST'){posts++;return {ok:true,json:async()=>({status:'queued'})};}
      return {ok:true,json:async()=>({...f.pending,title:'眠り姫 (OathOne Recording)',reference_lines:references})};
    });
    assert.equal(reply.status,matches?'queued':'unmatched');assert.equal(posts,matches?1:0);
  }
});

test('anchors and equivalent URL escaping are one lyric document, not competing candidates',()=>{
  const {documentURL,candidateUrls}=require('../extension/document_match.js');
  const source='https://namu.wiki/w/M%40STERPIECE';
  assert.equal(documentURL(source+'#s-4'),source);
  assert.equal(documentURL('https://namu.wiki/w/M@STERPIECE?x=1#s-5'),source);
  assert.deepEqual(candidateUrls([source,source+'#s-4','https://namu.wiki/w/M@STERPIECE'],'M@STERPIECE (OathOne Recording)',''),[source]);
  assert.deepEqual(candidateUrls([source],'M@STERPIECE (OathOne Recording)',source+'#s-4'),[]);
});

test('unique lyric candidate connects without artist evidence only after discovery is exhausted',async()=>{
  for(const [searched,candidates,expected] of [[true,[],'queued'],[false,[],'needs_confirmation'],[true,['https://namu.wiki/w/M%40STERPIECE(Series)'],'needs_confirmation']]){
    const f=fixture(),source='https://namu.wiki/w/M%40STERPIECE';
    Object.assign(f.pending,{uniqueTitleURL:source,searched,candidates,visited:['recording',source]});
    f.api.scripting.executeScript=async()=>[{result:{text:'synthetic lyrics',url:source+'#s-4',artistMatch:false}}];
    let payload;
    const result=await transferToOverlay(f.api,9,f.pending,async(url,options)=>{
      if(options.method==='POST'){payload=JSON.parse(options.body);return {ok:true,json:async()=>({status:'queued'})};}
      return {ok:true,json:async()=>({...f.pending,title:'M@STERPIECE (OathOne Recording)',reference_lines:[]})};
    });
    assert.equal(result.status,expected);
    if(payload){assert.equal(payload.match_mode,'unique_title');assert.equal(payload.user_selected,false);}
  }
});

test('exact recording title transfers without Spotify references or manual document selection',async()=>{
  const f=fixture(),title='蒼い鳥 (OathOne Recording)',source='https://namu.wiki/w/'+encodeURIComponent(title);
  f.api.scripting.executeScript=async()=>[{result:{text:'synthetic test lyrics',url:source,artistMatch:false}}];
  let payload;
  const reply=await transferToOverlay(f.api,9,f.pending,async(url,options)=>{
    if(options.method==='POST'){payload=JSON.parse(options.body);return {ok:true,json:async()=>({status:'queued'})};}
    return {ok:true,json:async()=>({...f.pending,title,reference_lines:[]})};
  });
  assert.equal(reply.status,'queued');assert.equal(payload.url,source);
  assert.equal(payload.match_mode,'title');assert.equal(payload.user_selected,false);
});

test('user-reported titles keep their URL spelling while comparison tolerates punctuation',()=>{
  const {documentTitle,sameDocumentTitle,candidateUrls}=require('../extension/document_match.js');
  for(const title of ['Give me some more...','THE IDOLM@STER','アイシテの呪縛～Je vous aime～']){
    assert.equal(documentTitle(title),title);
    assert.equal(sameDocumentTitle('https://namu.wiki/w/'+encodeURIComponent(title),title),true);
  }
  assert.equal(sameDocumentTitle('https://namu.wiki/w/THE%20IDOLM%40STER(%EA%B3%A1)','THE IDOLM@STER'),true);
  assert.equal(candidateUrls(['https://namu.wiki/w/THE%20IDOLM%40STER(%EA%B3%A1)'],'THE IDOLM@STER','').length,1);
});

test('reader searches child sections and skips metadata tables outside the lyrics section',()=>{
  const {lyricTableCandidates,chooseLyricTable}=require('../extension/reader.js');
  const h=(level,text)=>({tagName:'H'+level,textContent:text});
  const table=text=>({tagName:'TABLE',text,contains:()=>false});
  const metadata=table('credits'),legend=table('Singer names'),first=table('窓を開ける\n마도오 아케루\n창문을 열다'),second=table('花を描く\n하나오 카쿠\n꽃을 그리다\nOpen the window'),unrelated=table('other');
  const nodes=[metadata,h(2,'3. 가사[편집]'),legend,h(3,'3.1. Original'),first,h(3,'3.2. Full version'),second,h(2,'4. 기타'),unrelated];
  const candidates=lyricTableCandidates(nodes);
  assert.deepEqual(candidates,[legend,first,second]);
  assert.equal(chooseLyricTable(candidates,['Open the window'],node=>node.text),second);
  assert.equal(chooseLyricTable([legend],[],node=>node.text),null);
});

test('original-only tables are readable only from the existing lyrics-section candidates',()=>{
  const {lyricTableCandidates,chooseLyricTable}=require('../extension/reader.js');
  const h=(level,text)=>({tagName:'H'+level,textContent:text});
  const table=text=>({tagName:'TABLE',text,contains:()=>false});
  const unrelated=table('Long unrelated paragraph one\nLong unrelated paragraph two\nLong unrelated paragraph three');
  for(const text of ['窓を開ける\n花を描く','Open the window\nDraw a flower','窓を開ける\n마도오 아케루\n花を描く\n하나오 카쿠']){
    const lyrics=table(text),legend=table('Singer names');
    const candidates=lyricTableCandidates([unrelated,h(2,'3. 가사[편집]'),legend,lyrics,h(2,'4. 기타'),unrelated]);
    assert.deepEqual(candidates,[legend,lyrics]);
    assert.equal(chooseLyricTable(candidates,[],node=>node.text),lyrics);
  }
  assert.equal(chooseLyricTable([table('Credits'),table('한글 안내\n한글 설명')],[],node=>node.text),null);
});

test('table selection prefers translations unless Spotify phrases identify the original-only table',()=>{
  const {chooseLyricTable}=require('../extension/reader.js');
  const translated={text:'窓を開ける\n마도오 아케루\n창문을 열다'};
  const originals={text:'A synthetic lyric phrase one\nA synthetic lyric phrase two\nA synthetic lyric phrase three'};
  assert.equal(chooseLyricTable([originals,translated],[],node=>node.text),translated);
  assert.equal(chooseLyricTable([originals,translated],['A synthetic lyric phrase one','A synthetic lyric phrase two'],node=>node.text),originals);
});

test('ellipsis redirects retain selected and saved document approval',()=>{
  const {classifyOverlayDocument}=require('../extension/document_match.js');
  const alias='https://namu.wiki/w/dear...',canonical='https://namu.wiki/w/dear%E2%80%A6';
  const value={url:canonical+'?from=dear...#s-4',text:'synthetic source lyrics'};
  const state={title:'dear...',reference_lines:['unrelated reference one','unrelated reference two']};
  assert.deepEqual(classifyOverlayDocument(value,state,{selectedURL:alias}),{status:'matched',matchMode:'lyrics',selected:true});
  assert.equal(classifyOverlayDocument(value,{...state,document_url:alias}).status,'matched');
  for(const other of ['https://namu.wiki/w/Dear...','https://namu.wiki/w/dear(Other)','https://example.com/w/dear...']){
    assert.equal(classifyOverlayDocument(value,{...state,document_url:other},{selectedURL:other}).status,'unmatched');
  }
});

test('compatibility spellings remain separate navigation candidates while retaining title approval',()=>{
  const {candidateUrls,classifyOverlayDocument}=require('../extension/document_match.js');
  const alias='https://namu.wiki/w/dear...',canonical='https://namu.wiki/w/dear%E2%80%A6';
  assert.deepEqual(candidateUrls([canonical,alias],'dear…',''),[canonical,alias]);
  assert.deepEqual(candidateUrls([alias,canonical],'dear…',''),[alias,canonical]);
  assert.deepEqual(candidateUrls([alias,canonical],'dear…',canonical+'?from=dear...'),[alias]);
  const result=classifyOverlayDocument({url:canonical,text:'synthetic',artistMatch:false},
    {title:'dear…',reference_lines:[]},{uniqueTitleURL:alias,searched:true,candidates:[],visited:['other',alias]});
  assert.equal(result.status,'matched');assert.equal(result.matchMode,'unique_title');
});

test('native reader accepts a matching ellipsis title before trying its ASCII candidate',async()=>{
  const fs=require('node:fs'),vm=require('node:vm');
  const alias='https://namu.wiki/w/dear...',canonical='https://namu.wiki/w/dear%E2%80%A6';
  const context={URL,location:{href:canonical+'?from=dear...'},
    window:{__originalPageReader:{apply:async()=>({ok:true})}},
    document:{querySelectorAll:()=>[alias],querySelector:query=>query==='.opr-reader-target'?{}:{textContent:''}},
    readOverlayText:()=> 'synthetic source lyrics'};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(require.resolve('../extension/document_match.js'),'utf8')+'\n'+
    fs.readFileSync(require.resolve('../native_page.js'),'utf8'),context);
  context.hasVisibleAccessChallenge=()=>false;
  const result=await context.nativePageRead({title:'dear…',reference_lines:[]},{visited:[canonical],candidates:[alias]});
  assert.equal(result.status,'matched');assert.equal(result.matchMode,'title');
  assert.deepEqual(Array.from(result.candidates),[alias]);assert.equal(result.url,context.location.href);
});
