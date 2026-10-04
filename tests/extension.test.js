const test=require('node:test');
const assert=require('node:assert/strict');
const {readerRequest,openReader}=require('../extension/popup.js');
const {openOriginal,pageLoaded,applyReader}=require('../extension/background.js');
const form={url:'https://namu.wiki/w/Example#lyrics',selector:'#lyrics',size:22,smallWindow:true};
function fake(granted=true) {
  const calls=[],session={};
  const api={
    permissions:{request:async x=>(calls.push(['permission',x]),granted),contains:async()=>granted},
    runtime:{sendMessage:async x=>(calls.push(['message',x]),{ok:true})},
    windows:{create:async x=>(calls.push(['window',x]),{tabs:[{id:7}]})},
    tabs:{create:async x=>(calls.push(['tab',x]),{id:8}),update:async(id,x)=>calls.push(['navigate',id,x])},
    storage:{session:{set:async x=>Object.assign(session,x),get:async key=>({[key]:session[key]}),remove:async key=>{delete session[key];}}},
    scripting:{executeScript:async x=>(calls.push(['script',x]),[{result:{ok:true}}])},
    action:{setBadgeText:async x=>calls.push(['badge',x]),setTitle:async x=>calls.push(['title',x])}
  };
  return {api,calls,session};
}
test('button requests only named origin before sending the open command',async()=>{
  const {api,calls}=fake();await openReader(api,form);
  assert.deepEqual(calls[0],['permission',{origins:['https://namu.wiki/*']}]);
  assert.equal(calls[1][1].url,form.url);
  assert.deepEqual(Object.keys(calls[1][1]).sort(),['action','options','smallWindow','url']);
});
test('permission denial never creates a page or sends an open command',async()=>{
  const {api,calls}=fake(false);await assert.rejects(openReader(api,form));
  assert.equal(calls.length,1);
});
test('foreign sites, executable URLs and credentials fail before permission request',()=>{
  for(const url of ['https://example.com/','javascript:alert(1)','file:///a','https://user:pass@namu.wiki/w/a'])
    assert.throws(()=>readerRequest({...form,url}));
  assert.throws(()=>readerRequest({...form,size:99}));
});
test('small window opens original URL and keeps only temporary display settings',async()=>{
  const {api,calls,session}=fake();await openOriginal(api,readerRequest(form));
  assert.equal(calls[0][0],'window');assert.equal(calls.at(-1)[2].url,form.url);
  assert.deepEqual(Object.keys(session['reader:7']).sort(),['expires','options','origin']);
  assert.equal(calls.filter(x=>x[0]==='script').length,0);
});
test('tab mode works and original page completion injects only once',async()=>{
  const {api,calls,session}=fake();await openOriginal(api,readerRequest({...form,smallWindow:false}));
  await pageLoaded(api,8,{url:'about:blank'});assert.ok(session['reader:8']);
  await pageLoaded(api,8,{url:form.url});assert.equal(session['reader:8'],undefined);
  assert.equal(calls.filter(x=>x[0]==='script').length,2);
  await pageLoaded(api,8,{url:form.url});
  assert.equal(calls.filter(x=>x[0]==='script').length,2);
});
test('cross origin redirects and expired requests do not inject',async()=>{
  for(const expired of [true,false]){
    const {api,calls,session}=fake();await openOriginal(api,readerRequest(form));
    if(expired)session['reader:7'].expires=0;
    await pageLoaded(api,7,{url:expired?form.url:'https://example.com/'});
    assert.equal(calls.filter(x=>x[0]==='script').length,0);
    assert.equal(session['reader:7'],undefined);
  }
});
test('current page restore returns status without page text',async()=>{
  const {api,calls}=fake();assert.deepEqual(await applyReader(api,9,{},true),{ok:true});
  assert.deepEqual(calls[0][1].files,['lyric_text.js','document_match.js','reader.js']);
  assert.equal(calls[1][1].args[1],true);
});

