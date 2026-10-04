/* Build a transient plain-text reading of the selected table without changing its DOM. */
function readOverlayText(root,visible=null,styleOf=node=>typeof getComputedStyle==='function'?getComputedStyle(node):{}){
  const parts=[];let size=0;
  function add(text){size+=text.length;if(size>200000)throw new Error('가사 표가 너무 큽니다.');parts.push(text);}
  function walk(node){
    if(node.nodeType===3){add(node.nodeValue||'');return;}
    if(node.nodeType!==1)return;
    if(['RT','RP','SUP','SCRIPT','STYLE','NOSCRIPT','TEMPLATE'].includes(node.tagName))return;
    if(node.getAttribute?.('role')==='tooltip' || node.getAttribute?.('aria-hidden')==='true'
        || node.getAttribute?.('hidden')!=null || visible && !visible(node))return;
    const style=styleOf(node);
    if(style.display==='none' || ['hidden','collapse'].includes(style.visibility)
        || style.contentVisibility==='hidden' || style.opacity!=='' && Number(style.opacity)===0)return;
    if(node.tagName==='BR'){add('\n');return;}
    const block=style.display ? ['block','flow-root','flex','grid','list-item','table-row','table-cell'].includes(style.display)
      : ['P','DIV','TR','TD','TH','LI'].includes(node.tagName);
    if(block)add('\n');
    for(const child of node.childNodes)walk(child);
    if(block)add('\n');
  }
  walk(root);return parts.join('').trim();
}
if(typeof module!=='undefined')module.exports={readOverlayText};
