/* RAM-only source comparison and discovery of same-title links already in the page. */
function lyricKey(value){return String(value).normalize('NFKC').toLocaleLowerCase().replace(/[^\p{L}\p{N}]/gu,'');}
function hasVisibleAccessChallenge(root=document,styleOf=node=>getComputedStyle(node)){
  if(/^(just a moment|access denied|attention required)/i.test(String(root.title||'').trim()))return true;
  return [...root.querySelectorAll('#challenge-form,iframe[src*="captcha"],iframe[src*="challenges.cloudflare"]')].some(node=>{
    if(![...node.getClientRects()].some(rect=>rect.width>0&&rect.height>0))return false;
    const visibility=styleOf(node).visibility;
    if(visibility==='hidden'||visibility==='collapse')return false;
    for(let parent=node;parent;parent=parent.parentElement){
      const style=styleOf(parent);
      if(style.display==='none'||style.contentVisibility==='hidden'||parseFloat(style.opacity)===0)return false;
    }
    return true;
  });
}
function documentParts(value){
  const url=new URL(value);
  const prefix=url.origin==='https://namu.wiki'?'/w/':url.origin==='https://sidem-ko.fandom.com'?'/ko/wiki/':'';
  if(!prefix||!url.pathname.startsWith(prefix)||url.pathname.length===prefix.length||url.username||url.password)return null;
  const name=decodeURIComponent(url.pathname.slice(prefix.length));
  return {url,prefix,name:prefix==='/ko/wiki/'?name.replace(/_/g,' '):name};
}
function documentName(value){try{return documentParts(value)?.name||'';}catch(_){return '';}}
function documentURL(value){
  try{
    const parts=documentParts(value);if(!parts)return '';
    const {url,prefix,name}=parts;
    url.pathname=prefix+encodeURIComponent(prefix==='/ko/wiki/'?name.replace(/ /g,'_'):name);
    url.hash='';url.search='';return url.href;
  }catch(_){return '';}
}
function sameDocumentURL(left,right){
  const a=documentURL(left),b=documentURL(right);
  // Compare compatibility spellings such as … / ... without rewriting navigation URLs.
  return !!a&&!!b&&new URL(a).origin===new URL(b).origin&&documentName(a).normalize('NFKC')===documentName(b).normalize('NFKC');
}
function sameDocumentPath(left,right){
  // Navigation must try distinct compatibility spellings if one has no table.
  const a=documentURL(left),b=documentURL(right);
  return !!a&&a===b;
}
function documentTitle(value){
  const original=String(value).trim();
  const clean=original.replace(/\s*(?:[-–—]\s*|[([]\s*|\s+)(?:(?:19|20)\d{2}\s*(?:ver(?:sion)?\.?|remaster(?:ed)?)|(?:ver(?:sion)?\.?|remaster(?:ed)?)\s*(?:19|20)\d{2})\s*[)\]]?\s*$/i,'').trim();
  // A year inside a singer/version group must not leave half a title behind.
  return balancedGroups(clean.normalize('NFKC'))?clean:original;
}
function balancedGroups(value){
  const stack=[];
  for(const char of value){
    if(char==='('||char==='[')stack.push(char);
    else if(char===')'||char===']')if(stack.pop()!==(char===')'?'(':'['))return false;
  }
  return !stack.length;
}
function trailingGroup(value){
  if(!/[)\]]$/.test(value))return null;
  const stack=[];
  for(let index=value.length-1;index>=0;index--){
    const char=value[index];
    if(char===')'||char===']')stack.push(char);
    else if(char==='('||char==='['){
      if(stack.pop()!==(char==='('?')':']'))return null;
      if(!stack.length)return [value.slice(0,index).trim(),value.slice(index+1,-1).trim()];
    }
  }
  return null;
}
function versionSuffix(value){
  return /(?:^|[^A-Za-z0-9]|(?:19|20)\d{2}\s*)(?:ver(?:sion)?\.?|recording|remaster(?:ed)?|버전|バージョン)(?:\s*(?:19|20)\d{2})?\s*$/i.test(value);
}
function recordingTitles(value){
  let title=documentTitle(String(value).normalize('NFKC'));
  const titles=[title];
  for(let index=0;index<8;index++){
    const group=trailingGroup(title),suffix=title.match(/^(.*)\s+[-–—]\s*(.+)$/);
    const base=group&&versionSuffix(group[1])?group[0]:suffix&&versionSuffix(suffix[2])?suffix[1].trim():'';
    if(!base)break;
    title=documentTitle(base);titles.push(title);
  }
  return titles;
}
function recordingBase(title){return recordingTitles(title).at(-1);}
function originalTitlePrefix(title,candidate){
  // NFKC is useful for comparing titles, but a wiki path must retain the
  // source spelling (e.g. a superscript plus or a compatibility ligature).
  const expected=String(candidate).normalize('NFKC').trim(),original=String(title);
  for(let end=1;end<=original.length;end++){
    const prefix=original.slice(0,end).trim();
    if(prefix.normalize('NFKC')===expected)return prefix;
  }
  return candidate;
}
function titleCandidates(title,includeColors=true){
  const safe=recordingTitles(title),result=[documentTitle(title)];
  for(const candidate of safe)if(candidate&&!result.some(other=>lyricKey(other)===lyricKey(candidate)))result.push(candidate);
  // Numbered ensemble editions provide a search lead, never a lyric-title alias.
  const colors=includeColors&&safe.at(-1).match(/^(.+?)\s*[-–—]\s*[1-9]\d*\s+colors\s*[-–—]\s*$/i);
  if(colors&&colors[1].trim()&&!result.some(other=>lyricKey(other)===lyricKey(colors[1])))result.push(colors[1].trim());
  // A length edit is also a discovery lead, never proof of the full lyric.
  const size=includeColors&&safe.at(-1).match(/^(.+?)\s+[-–—]\s*(?:game|tv)\s+size\s*$/i);
  if(size&&!result.some(other=>lyricKey(other)===lyricKey(size[1])))result.push(size[1].trim());
  let plain=safe.at(-1),group;
  while(result.length<9&&(group=trailingGroup(plain))&&group[0]){
    plain=documentTitle(group[0]);
    if(!result.some(other=>lyricKey(other)===lyricKey(plain)))result.push(plain);
  }
  return result.filter(Boolean).slice(0,9).map(candidate=>originalTitlePrefix(title,candidate));
}
function widthSpelling(value){
  // Fold width forms together (including kana + voiced marks), but preserve
  // other compatibility characters such as superscript +, ligatures and ….
  return String(value).replace(/[\u3000\uFF01-\uFFEF]+/gu,part=>part.normalize('NFKC')).normalize('NFC');
}
function documentSpellings(value){
  const forms=[value,widthSpelling(value)],result=[...forms];
  // U+301C wave dash is not an NFKC width pair. Wiki paths use all three
  // spellings, so try each direction without treating navigation as approval.
  for(const form of forms)if(/[~〜～]/.test(form)){
    for(const mark of ['~','～','〜'])result.push(form.replace(/[~〜～]/g,mark));
  }
  return [...new Set(result)];
}
function searchPrefixes(title){
  const candidates=titleCandidates(title),base=candidates.at(-1)||documentTitle(title),chars=Array.from(base);
  const shorter=[base.split(/\s+[-–—]\s+/)[0].trim(),chars.slice(0,Math.ceil(chars.length*.75)).join('').trim(),chars.slice(0,Math.ceil(chars.length*.5)).join('').trim()];
  return [...new Set([...candidates,...candidates.map(widthSpelling),...shorter.flatMap(value=>[value,widthSpelling(value)])])].filter(x=>Array.from(x).length>=2).slice(0,6);
}
function sameDocumentTitle(url,title){
  try{
    const parts=documentParts(url);if(!parts)return false;
    const name=documentTitle(parts.name.normalize('NFKC')),expected=lyricKey(documentTitle(String(title).normalize('NFKC')));
    if(!expected)return false;
    if(lyricKey(name)===expected)return true;
    // Wiki disambiguators are allowed, but another singer's version is a different document.
    let plain=name.normalize('NFKC'),group;
    while((group=trailingGroup(plain))&&!versionSuffix(group[1])){
      plain=documentTitle(group[0]);
      if(lyricKey(plain)===expected)return true;
    }
    return false;
  }catch(_){return false;}
}
function sameLyricTitle(url,title){
  return recordingTitles(title).some(variant=>sameDocumentTitle(url,variant));
}
function candidateTitleRank(url,title){
  const index=titleCandidates(title).findIndex(candidate=>sameDocumentTitle(url,candidate));
  return index<0?Infinity:index;
}
function artistInPage(artist,text){
  const name=lyricKey(String(artist||'').normalize('NFKC').replace(/\([^)]*\)/g,''));
  return name.length>=3&&lyricKey(text).includes(name);
}
function titleAliasKeys(url,title,artist){
  if(sameLyricTitle(url,title)||!titleCandidates(title,false).some(candidate=>sameDocumentTitle(url,candidate)))return null;
  const formal=lyricKey(recordingBase(title));
  const performer=lyricKey(String(artist||'').normalize('NFKC').replace(/\([^)]*\)/g,''));
  return formal.length>=6&&performer.length>=3?{title:formal,artist:performer}:null;
}
function titleAliasEvidence(url,title,artist,text){
  const keys=titleAliasKeys(url,title,artist),body=lyricKey(text);
  return keys&&body.includes(keys.title)&&body.includes(keys.artist)?keys:null;
}
function matchesReference(text,references){
  const body=lyricKey(text),matched=new Set();
  for(const reference of references||[]){const key=lyricKey(reference);if(key.length>=6&&body.includes(key))matched.add(key);}
  return matched.size>=2;
}
function candidateUrls(links,title,current,visited=[]){
  const result=[],currentURL=documentURL(current);
  for(const link of links){
    try{
      const url=documentURL(typeof link==='string'?link:link.href);
      if(!url||!Number.isFinite(candidateTitleRank(url,title))||visited.some(other=>sameDocumentPath(url,other)))continue;
      if(currentURL&&new URL(url).origin!==new URL(currentURL).origin)continue;
      if(!sameDocumentPath(url,currentURL)&&!result.some(other=>sameDocumentPath(url,other)))result.push(url);
    }catch(_){}
  }
  // The ninth candidate lets bounded callers retain an incomplete-discovery flag.
  return result.sort((a,b)=>candidateTitleRank(a,title)-candidateTitleRank(b,title)).slice(0,9);
}
function documentCandidates(title,urls,visited=[],current=''){
  const result=[];
  const seed=[current,...visited,...urls].map(documentURL).find(Boolean)||'https://namu.wiki/w/Example';
  const source=documentParts(seed);
  const base=source.url.origin+source.prefix;
  // Keep original spellings first. Equal comparison keys must not discard
  // distinct width/tilde navigation paths when the first one has no table.
  const names=titleCandidates(title).flatMap(documentSpellings);
  for(const value of [...names.map(name=>base+encodeURIComponent(source.prefix==='/ko/wiki/'?name.replace(/ /g,'_'):name)),...urls]){
    const url=documentURL(value);
    if(!url||sameDocumentPath(url,current)||visited.some(other=>sameDocumentPath(url,other))||result.some(other=>sameDocumentPath(url,other)))continue;
    if(new URL(url).origin!==source.url.origin)continue;
    result.push(url);
  }
  result.sort((a,b)=>candidateTitleRank(a,title)-candidateTitleRank(b,title));
  return {candidates:result.slice(0,8),unexamined:result.length>Math.max(0,8-visited.length)};
}
function classifyOverlayDocument(value,state,pending={}){
  if(!documentURL(value.url))return {status:'unmatched'};
  const references=state.reference_lines||[];
  const explicit=sameDocumentURL(state.document_url,value.url)&&(!state.document_learned||references.length<2);
  const selected=sameDocumentURL(pending.selectedURL,value.url);
  const unique=sameDocumentURL(pending.uniqueTitleURL,value.url)&&pending.searched&&!pending.candidates?.length&&!pending.unexamined;
  let matchMode='lyrics';
  if(!explicit && !selected && references.length>=2 && !matchesReference(value.text,references))return {status:'unmatched'};
  if(!explicit && !selected && references.length<2 && Array.isArray(state.reference_lines)){
    if(!sameLyricTitle(value.url,state.title)){
      const keys=titleAliasKeys(value.url,state.title,state.artist),evidence=value.titleAlias;
      if(keys&&typeof evidence?.title==='string'&&typeof evidence?.artist==='string'
          &&lyricKey(evidence.title)===keys.title&&lyricKey(evidence.artist)===keys.artist)
        return {status:'matched',matchMode:'metadata_alias',selected};
      return Number.isFinite(candidateTitleRank(value.url,state.title))
        ?{status:'needs_confirmation',url:documentURL(value.url)}:{status:'unmatched'};
    }
    const rank=candidateTitleRank(value.url,state.title);
    const alternatives=[...(pending.candidates||[]),...(pending.visited||[])].some(url=>
      !sameDocumentURL(url,value.url)&&candidateTitleRank(url,state.title)<=rank);
    if(value.artistMatch)matchMode='metadata';
    else if(unique)matchMode='unique_title';
    else if(!alternatives&&sameDocumentTitle(value.url,state.title))matchMode='title';
    else return {status:'needs_confirmation',url:documentURL(value.url)};
  }
  return {status:'matched',matchMode,selected};
}
if(typeof module!=='undefined')module.exports={lyricKey,hasVisibleAccessChallenge,documentParts,documentName,documentURL,sameDocumentURL,matchesReference,candidateUrls,documentTitle,searchPrefixes,sameDocumentTitle,recordingBase,sameLyricTitle,titleCandidates,documentCandidates,artistInPage,titleAliasEvidence,classifyOverlayDocument};
