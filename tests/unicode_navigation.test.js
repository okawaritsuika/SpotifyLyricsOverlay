const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
const {lyricKey,sameLyricTitle,documentCandidates,candidateUrls,searchPrefixes,classifyOverlayDocument}=require('../extension/document_match.js');
const url=name=>'https://namu.wiki/w/'+encodeURIComponent(name);
const widths=[
  ['Ｓｏｎｇ１２３！','Song123!'],
  ['Song　Subtitle','Song Subtitle'],
  ['ｶﾞｰﾙ･ﾊﾟｰﾃｨｰ','ガール・パーティー'],
  ['Song（別の物語）','Song(別の物語)'],
  ['カ\u3099ール','ガール'],
  ['Resonance⁺　ＦＬＯＷＥＲ…','Resonance⁺ FLOWER…'],
];

test('shared width fixtures agree in both directions and preserve meaningful kana differences',()=>{
  for(const [left,right,expected] of require('./title_variants.json').widths){
    assert.equal(sameLyricTitle(url(left),right),expected,`${left} → ${right}`);
    assert.equal(sameLyricTitle(url(right),left),expected,`${right} → ${left}`);
    assert.equal(lyricKey(left)===lyricKey(right),expected);
  }
});

test('width and composed-kana alternatives are navigated after the original path',()=>{
  for(const [title,canonical] of widths){
    const result=documentCandidates(title,[]).candidates;
    assert.equal(result[0],url(title));
    assert.ok(result.includes(url(canonical)),title);
    assert.ok(documentCandidates(title,[],[url(title)],url(title)).candidates.includes(url(canonical)),title);
    const visited=result;
    assert.equal(documentCandidates(title,[],visited,visited.at(-1)).candidates.length,0,title);
    assert.ok(searchPrefixes(title).includes(canonical),title);
  }
});

test('all three tilde spellings are tried in either direction without suggestions',()=>{
  for(const mark of ['~','～','〜']){
    const title=`Synthetic Song ${mark}Another Story${mark}`,current=url(title);
    const expected=['~','～','〜'].map(other=>url(`Synthetic Song ${other}Another Story${other}`));
    const result=documentCandidates(title,[]).candidates;
    assert.equal(result[0],current);
    assert.deepEqual(new Set(result),new Set(expected));
    assert.deepEqual(new Set(documentCandidates(title,[],[current],current).candidates),new Set(expected.filter(value=>value!==current)));
  }
  const mixed='Ｓｏｎｇ　〜ｶﾞｰﾙ～';
  const result=documentCandidates(mixed,[]).candidates;
  assert.equal(result[0],url(mixed));
  for(const mark of ['~','～','〜'])assert.ok(result.includes(url(`Song ${mark}ガール${mark}`)));
  assert.ok(result.length<=8);
});

test('discovered width spellings work in both directions while different lyrics and versions fail',()=>{
  for(const pair of widths){
    for(const [title,name] of [pair,[...pair].reverse()]){
      const source=url(name);
      assert.deepEqual(candidateUrls([source],title,url(title)),[source]);
      const value={url:source,text:'synthetic source',artistMatch:true};
      assert.equal(classifyOverlayDocument(value,{title,reference_lines:[]}).status,'matched');
      assert.equal(classifyOverlayDocument(value,{title,reference_lines:['conflicting phrase one','conflicting phrase two']}).status,'unmatched');
    }
  }
  const title='Ｓｏｎｇ（Ａ Ｖｅｒ．）';
  assert.equal(classifyOverlayDocument({url:url('Song(B Ver.)'),text:'source',artistMatch:true},{title,reference_lines:[]}).status,'unmatched');
  assert.notEqual(lyricKey('ガール'),lyricKey('ガル'));
  assert.notEqual(lyricKey('キャラ'),lyricKey('キヤラ'));
});

test('native reader and extension queue share width alternatives without cross-site navigation',async()=>{
  const title='Ｓｏｎｇ　ｶﾞｰﾙ',current=url(title),canonical=url('Song ガール');
  const context={URL,location:{href:current},window:{__originalPageReader:{apply:async()=>{throw Error('no table');}}},
    document:{querySelectorAll:()=>[]},readOverlayText:()=>''};
  vm.createContext(context);
  for(const file of ['extension/document_match.js','native_page.js'])vm.runInContext(fs.readFileSync(require.resolve('../'+file),'utf8'),context);
  context.hasVisibleAccessChallenge=()=>false;
  const pending={visited:[current],candidates:[]};
  const native=await context.nativePageRead({title,reference_lines:[]},pending);
  assert.equal(native.status,'no_table');
  assert.deepEqual(Array.from(native.candidates),[canonical]);
  const other='https://sidem-ko.fandom.com/ko/wiki/'+encodeURIComponent('Song_ガール');
  assert.deepEqual(documentCandidates(title,[other],pending.visited,current).candidates,[canonical]);
  const sidem='https://sidem-ko.fandom.com/ko/wiki/'+encodeURIComponent(title.replace(/ /g,'_'));
  assert.ok(documentCandidates(title,[current],[sidem],sidem).candidates.includes(other));
  assert.ok(documentCandidates(title,[current],[sidem],sidem).candidates.every(value=>value.startsWith('https://sidem-ko.fandom.com/')));
});
