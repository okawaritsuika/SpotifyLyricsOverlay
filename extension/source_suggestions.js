/* Ask the original page's own search UI. Only candidate URLs leave this script. */
async function sourceSuggestions(title){
  if(!documentURL(location.href||location.origin+location.pathname))return [];
  if(hasVisibleAccessChallenge())return [];
  const input=[...document.querySelectorAll('input[type="search"],input[name="q"],input[placeholder*="검색"],input[placeholder*="Search"]')].find(x=>!x.disabled&&x.getClientRects().length);
  if(!input)return [];
  const original=input.value,focused=document.activeElement,found=[];
  const set=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
  const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  const baseline=new Set([...document.querySelectorAll('a[href]')].map(x=>x.href));
  try{
    for(const prefix of searchPrefixes(title)){
      input.focus();set.call(input,prefix);
      input.dispatchEvent(new Event('input',{bubbles:true}));
      input.dispatchEvent(new KeyboardEvent('keyup',{key:prefix.slice(-1),bubbles:true}));
      await wait(1000);
      const controlled=document.getElementById(input.getAttribute('aria-controls'));
      const suggestions=new Set([...document.querySelectorAll('[role="listbox"] a[href],[role="option"] a[href],[class*="autocomplete"] a[href],[class*="suggest"] a[href]'),...(controlled?.querySelectorAll('a[href]')||[])]);
      for(const link of document.querySelectorAll('a[href]')){
        if(!suggestions.has(link)&&baseline.has(link.href))continue;
        try{
          const candidate=documentURL(link.href);if(!candidate)continue;
          const url=new URL(candidate),name=documentName(candidate);
          if(url.origin!==location.origin||!lyricKey(name).startsWith(lyricKey(prefix)))continue;
          url.search='';url.hash='';if(!found.includes(url.href))found.push(url.href);
        }catch(_){}
      }
      if(found.length>=8)break;
    }
  }finally{
    set.call(input,original);input.dispatchEvent(new Event('input',{bubbles:true}));
    input.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
    input.blur();if(focused?.isConnected)focused.focus();
  }
  return found.slice(0,8);
}
if(typeof module!=='undefined')module.exports={sourceSuggestions};
