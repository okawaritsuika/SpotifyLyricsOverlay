/* Original nodes remain in place. Automatic mode compares tables in RAM only. */
function lyricTableCandidates(nodes){
  const result=[];let level=0;
  for(const node of nodes){
    if(/^H[1-6]$/.test(node.tagName)){
      const depth=Number(node.tagName.slice(1));
      if(level && depth<=level)level=0;
      if(!level && /^\s*(?:\d+\.?\s*)*가사(?:\s|\[|$)/.test(node.textContent||''))level=depth;
    }else if(level && node.tagName==='TABLE'){result.push(node);}
  }
  return result;
}
function chooseLyricTable(tables,references=[],read=readOverlayText,source='namu',fullReferences=[]){
  const keyOf=typeof module!=='undefined'?require('./document_match.js').lyricKey:lyricKey;
  let best=null,bestScore=0;const candidates=[];
  for(const table of tables){
    const text=read(table),rows=text.split(/\r?\n/).map(row=>row.trim()).filter(Boolean);
    let triples=0,pairs=0;
    for(let i=0;i<rows.length-1;i++){
      if(/[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}A-Za-z]/u.test(rows[i]) && !/[가-힣]/.test(rows[i]) && /[가-힣]/.test(rows[i+1])){
        if(i+2<rows.length && /[가-힣]/.test(rows[i+2]))triples++;
        else if(source==='sidem')pairs++;
      }
    }
    const translations=triples+pairs;
    const originalRows=rows.filter(row=>/[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}A-Za-z]/u.test(row)&&!/[가-힣]/.test(row)&&keyOf(row).length>=2).map(keyOf);
    const originals=originalRows.length;
    if(!translations&&originals<2)continue;
    const body=keyOf(text),matches=new Set(references.map(keyOf).filter(key=>key.length>=6 && body.includes(key))).size;
    // Within the lyrics section, untranslated originals remain a usable fallback.
    const score=matches*100000+(translations?10000+translations:Math.min(originals,9999));
    candidates.push({table,originalRows,score});
  }
  const referenceRows=fullReferences.map(keyOf).filter(Boolean),referenceText=referenceRows.join('');
  const referencePhrases=[...new Set(referenceRows.filter(key=>key.length>=6))];
  function duplicateContainer(parent){
    if(referenceRows.length<8 || referenceText.length<100 || referencePhrases.length<4)return false;
    for(const child of candidates){
      if(child===parent || !parent.table.contains?.(child.table) || child.originalRows.length<12)continue;
      const original=child.originalRows.join(''),ratio=original.length/referenceText.length;
      if(ratio<.8 || ratio>1.2 || referencePhrases.filter(key=>original.includes(key)).length/referencePhrases.length<.8)continue;
      if(parent.originalRows.length<child.originalRows.length*2 || parent.originalRows.length%child.originalRows.length)continue;
      if(!parent.originalRows.every((row,index)=>row===child.originalRows[index%child.originalRows.length]))continue;
      const copies=candidates.filter(other=>other!==parent && parent.table.contains?.(other.table)
        && other.originalRows.length===child.originalRows.length && other.originalRows.every((row,index)=>row===child.originalRows[index]));
      if(copies.some((a,index)=>copies.slice(index+1).some(b=>!a.table.contains?.(b.table)&&!b.table.contains?.(a.table))))return true;
    }
    return false;
  }
  for(const candidate of candidates){
    // Version tables can live inside one layout table. Repeated refrains in a
    // single table stay intact; only separate complete copies supported by the
    // reference document can disqualify their combined outer container.
    if(duplicateContainer(candidate))continue;
    if(candidate.score>bestScore){best=candidate.table;bestScore=candidate.score;}
  }
  return best;
}
if(typeof module!=='undefined')module.exports={lyricTableCandidates,chooseLyricTable};
(() => {
  if(typeof window==='undefined')return;
  if(window.__originalPageReader)return;
  let cleanup=()=>{},cancelPick=()=>{};
  function apply(options={}) {
    const size=Math.min(48,Math.max(12,Number(options.size)||22));
    function focus(target) {
      if(!target || target===document.documentElement || target===document.body)
        throw new Error('본문 안의 구체적인 영역을 선택하세요.');
      cancelPick();cleanup();
      if(!target.getClientRects().length)throw new Error('숨겨진 영역입니다. 보이는 영역을 선택하세요.');
      const marked=[];
      function mark(node,name){if(!node.classList.contains(name)){const hadClass=node.hasAttribute('class');node.classList.add(name);marked.push([node,name,hadClass]);}}
      mark(target,'opr-reader-target');
      for(let parent=target.parentElement;parent;parent=parent.parentElement)mark(parent,'opr-reader-path');
      const style=document.createElement('style');
      style.textContent=`
        .opr-reader-path > :not(.opr-reader-path):not(.opr-reader-target){display:none!important}
        html.opr-reader-path,body.opr-reader-path{overflow:auto!important;min-height:100%!important;background:#f8f6f0!important}
        .opr-reader-path{display:block!important;position:static!important;transform:none!important;contain:none!important;width:auto!important;max-width:none!important;height:auto!important;max-height:none!important;margin:0!important;padding:0!important;border:0!important;overflow:visible!important}
        .opr-reader-target{display:block!important;position:relative!important;float:none!important;transform:none!important;box-sizing:border-box!important;width:calc(100% - 32px)!important;max-width:960px!important;height:auto!important;max-height:none!important;overflow:auto!important;margin:24px auto!important;padding:20px!important;background:#fff!important;color:#202733!important;box-shadow:0 3px 22px #0002!important;border-radius:10px!important;font-size:${size}px!important;line-height:1.85!important}
        .opr-reader-target :not(rt):not(rp){font-size:inherit!important;line-height:inherit!important}
        .opr-reader-target img,.opr-reader-target video{max-width:100%!important;height:auto!important}
        .opr-reader-target header,.opr-reader-target nav,.opr-reader-target aside,.opr-reader-target footer{display:none!important}
        .opr-reader-target table{max-width:100%!important}
        .opr-reader-target rt{font-size:.55em!important}
      `;
      (document.head||document.documentElement).appendChild(style);
      cleanup=()=>{style.remove();for(const [node,name,hadClass] of marked){node.classList.remove(name);if(!hadClass&&!node.classList.length)node.removeAttribute('class');}cleanup=()=>{};};
      target.scrollIntoView({block:'start'});
      return {ok:true};
    }
    cancelPick();
    if(options.autoLyrics && !options.selector){
      cleanup();
      function locate(){
        const tables=lyricTableCandidates(document.querySelectorAll('h1,h2,h3,h4,h5,h6,table'));
        // A single Hangul row is a translation only on the selected bilingual wiki.
        const source=location.origin==='https://sidem-ko.fandom.com'&&location.pathname.startsWith('/ko/wiki/')?'sidem':'namu';
        return chooseLyricTable(tables.filter(node=>node.getClientRects().length),options.references||[],readOverlayText,source,options.fullReferences||[]);
      }
      const target=locate();if(target)return focus(target);
      return new Promise((resolve,reject)=>{
        const observer=new MutationObserver(()=>{
          const target=locate();if(!target)return;
          observer.disconnect();clearTimeout(timer);
          try{resolve(focus(target));}catch(error){reject(error);}
        });
        const timer=setTimeout(()=>{observer.disconnect();reject(new Error('원문이 있는 가사 표를 찾지 못했습니다. 원본 페이지를 확인하세요.'));},5000);
        observer.observe(document.documentElement,{childList:true,subtree:true});
      });
    }
    if(options.selector) {
      let targets;
      try{targets=document.querySelectorAll(options.selector);}catch(_){throw new Error('CSS 선택자 문법을 확인하세요.');}
      if(targets.length!==1)throw new Error('정확히 한 영역을 지정하거나 선택자를 비우고 직접 선택하세요.');
      return focus(targets[0]);
    }
    cleanup();
    let hovered=null,previousOutline='',previousPriority='';
    function clearHover(){if(hovered){if(previousOutline)hovered.style.setProperty('outline',previousOutline,previousPriority);else hovered.style.removeProperty('outline');hovered=null;}}
    function region(event){const node=event.target;return node instanceof Element?(node.closest('table')||node.closest('section,article,main,p,div')||node):null;}
    const move=event=>{
      const node=region(event);if(node===hovered)return;clearHover();
      if(node && node!==document.body && node!==document.documentElement){hovered=node;previousOutline=node.style.getPropertyValue('outline');previousPriority=node.style.getPropertyPriority('outline');node.style.setProperty('outline','3px solid #009977','important');}
    };
    const click=event=>{const node=region(event);if(!node||node===document.body||node===document.documentElement)return;event.preventDefault();event.stopImmediatePropagation();focus(node);};
    const key=event=>{if(event.key==='Escape'){event.preventDefault();cancelPick();}};
    cancelPick=()=>{clearHover();document.removeEventListener('pointermove',move,true);document.removeEventListener('click',click,true);document.removeEventListener('keydown',key,true);cancelPick=()=>{};};
    document.addEventListener('pointermove',move,true);document.addEventListener('click',click,true);document.addEventListener('keydown',key,true);
    return {ok:true,picking:true};
  }
  window.__originalPageReader={apply,restore:()=>{cancelPick();cleanup();return {ok:true};}};
})();
