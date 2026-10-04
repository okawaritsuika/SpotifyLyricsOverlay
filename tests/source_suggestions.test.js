const test=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');

function fixture(responses=null){
  let links=[{href:'https://namu.wiki/w/Unrelated'}];const typed=[];
  class Input{
    get value(){return this.text||'';}set value(value){this.text=value;}
    getClientRects(){return [{}];}getAttribute(){return null;}focus(){}blur(){}
    dispatchEvent(event){
      if(event.type!=='input')return;
      typed.push(this.value);
      if(responses)links=(responses[this.value]||[]).map(href=>({href}));
      else if(this.value==='リフレク')links=[{href:'https://namu.wiki/w/リフレクトサイン(Series)'},{href:'https://evil.example/w/リフレクトサイン'}];
      else links=[];
    }
  }
  const input=new Input();input.value='previous search';
  const document={activeElement:null,querySelector:()=>null,getElementById:()=>null,
    querySelectorAll:selector=>selector.startsWith('input')?[input]:selector==='a[href]'?links:[]};
  const context=vm.createContext({document,getComputedStyle:node=>node.style||{},location:{origin:'https://namu.wiki',pathname:'/w/Example'},HTMLInputElement:Input,URL,Event:class{constructor(type){this.type=type;}},KeyboardEvent:class{constructor(type){this.type=type;}},setTimeout:callback=>callback()});
  for(const file of ['document_match.js','source_suggestions.js'])vm.runInContext(fs.readFileSync(path.join(__dirname,'../extension',file),'utf8'),context);
  return {context,document,input,typed};
}

test('original search UI is shortened, candidate URLs filtered and search restored',async()=>{
  const f=fixture();const result=await vm.runInContext('sourceSuggestions("リフレクトサイン-2022ver")',f.context);
  assert.equal(result.length,1);assert.equal(decodeURIComponent(result[0]),'https://namu.wiki/w/リフレクトサイン(Series)');
  assert.ok(f.typed.includes('リフレク'));assert.equal(f.input.value,'previous search');
});

test('width-normalized searches retain actual source paths in either direction',async()=>{
  const original='Ｓｏｎｇ　ｶﾞｰﾙ',canonical='Song ガール';
  for(const [title,name] of [[original,canonical],[canonical,original]]){
    const source='https://namu.wiki/w/'+encodeURIComponent(name);
    const f=fixture({[canonical]:[source,'https://evil.example/w/'+encodeURIComponent(name)]});
    const result=await f.context.sourceSuggestions(title);
    assert.deepEqual(Array.from(result),[source]);
    assert.ok(f.typed.includes(canonical));
    assert.ok(f.typed.length<=7); // Up to six searches plus restoration.
    assert.equal(f.input.value,'previous search');
  }
});

test('missing search input or a challenge page does not trigger searching',async()=>{
  const f=fixture();f.document.title='Just a moment...';
  assert.equal((await vm.runInContext('sourceSuggestions("Example")',f.context)).length,0);
  assert.equal(f.typed.length,0);
  f.document.title='Song';f.document.querySelectorAll=()=>[];
  assert.equal((await vm.runInContext('sourceSuggestions("Example")',f.context)).length,0);
});

test('hidden challenge elements do not classify a normal article as an access barrier',()=>{
  const {hasVisibleAccessChallenge}=require('../extension/document_match.js');
  const make=(style={},rects=[{width:300,height:65}],parentElement=null)=>({style,getClientRects:()=>rects,parentElement});
  const hidden=[make({},[]),make({display:'none'}),make({visibility:'hidden'}),make({visibility:'collapse'}),
    make({opacity:'0'}),make({contentVisibility:'hidden'}),make({},[{width:0,height:0}]),
    make({},undefined,make({opacity:'0'})),make({},undefined,make({display:'none'}))];
  for(const node of hidden){
    assert.equal(hasVisibleAccessChallenge({title:'M@STERPIECE - 나무위키',querySelectorAll:()=>[node]},n=>n.style),false);
  }
});

test('rendered challenge controls and explicit access-denied pages still stop reading',()=>{
  const {hasVisibleAccessChallenge}=require('../extension/document_match.js');
  const visible={style:{visibility:'visible',opacity:'1'},getClientRects:()=>[{width:300,height:65}],parentElement:null};
  assert.equal(hasVisibleAccessChallenge({title:'Song',querySelectorAll:()=>[visible]},n=>n.style),true);
  for(const title of ['Just a moment...','Access denied','Attention required!']){
    assert.equal(hasVisibleAccessChallenge({title,querySelectorAll:()=>[]},()=>({})),true);
  }
});

test('a hidden verification iframe does not suppress the original page search',async()=>{
  const f=fixture(),query=f.document.querySelectorAll;
  f.document.querySelectorAll=selector=>selector.startsWith('#challenge-form')?[{getClientRects:()=>[],style:{}}]:query(selector);
  const result=await vm.runInContext('sourceSuggestions("リフレクトサイン-2022ver")',f.context);
  assert.equal(result.length,1);assert.ok(f.typed.includes('リフレク'));
});

test('a visible verification iframe prevents all search input',async()=>{
  const f=fixture(),query=f.document.querySelectorAll;
  f.document.querySelectorAll=selector=>selector.startsWith('#challenge-form')?[{getClientRects:()=>[{width:300,height:65}],style:{opacity:'1'}}]:query(selector);
  assert.equal((await vm.runInContext('sourceSuggestions("Example")',f.context)).length,0);
  assert.equal(f.typed.length,0);
});
