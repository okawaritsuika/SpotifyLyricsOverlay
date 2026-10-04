const test=require('node:test');
const assert=require('node:assert/strict');
const {lyricTableCandidates,chooseLyricTable}=require('../extension/reader.js');

const originals=Array.from({length:24},(_,index)=>`Synthetic original phrase number ${index}`);
const lyricText=rows=>rows.map(row=>`${row}\n한글 발음\n한국어 번역`).join('\n');
function table(text,children=[]){
  return {tagName:'TABLE',text,contains(node){return children.some(child=>child===node||child.contains(node));}};
}
function versions(rows=originals){
  const children=Array.from({length:3},()=>table(lyricText(rows)));
  const parent=table(children.map(child=>child.text).join('\n'),children);
  return {parent,children,tables:[parent,...children]};
}
const choose=(tables,refs=originals)=>chooseLyricTable(tables,refs,node=>node.text,'namu',refs);

test('lyrics section exposes nested version tables without including later sections',()=>{
  const f=versions(),outside=table('Outside'),heading=text=>({tagName:'H2',textContent:text});
  assert.deepEqual(lyricTableCandidates([outside,heading('3. 가사'),...f.tables,heading('4. 기타'),outside]),f.tables);
});

test('separate complete version copies choose one full song using provider evidence',()=>{
  const f=versions();
  assert.equal(choose(f.tables),f.children[0]);
  assert.equal(f.parent.text,Array(3).fill(lyricText(originals)).join('\n'));
});

test('a single table retains all repeated verses and choruses',()=>{
  const repeated=table(lyricText([...originals,...originals,...originals]));
  assert.equal(choose([repeated]),repeated);
  assert.equal(repeated.text.split('\n').length,24*3*3);
});

test('additional verses outside nested chorus tables keep the complete outer table',()=>{
  const f=versions();f.parent.text=lyricText(['A unique introductory verse'])+'\n'+f.parent.text;
  assert.equal(choose(f.tables),f.parent);
});

test('a provider document containing the full repeated performance prevents shortening',()=>{
  const f=versions();
  assert.equal(choose(f.tables,[...originals,...originals,...originals]),f.parent);
});

test('missing or short provider references do not authorize shortening',()=>{
  for(const refs of [[],originals.slice(0,2)]){
    const f=versions();assert.equal(choose(f.tables,refs),f.parent);
  }
  const f=versions();
  assert.equal(chooseLyricTable(f.tables,originals,node=>node.text),f.parent);
});

test('one physical copy wrapped in multiple nested tables is not independent evidence',()=>{
  const leaf=table(lyricText(originals)),wrapper=table(leaf.text,[leaf]);
  const parent=table(leaf.text+'\n'+leaf.text,[wrapper]);
  assert.equal(choose([parent,wrapper,leaf]),parent);
});

test('different version texts and incomplete matching references preserve the outer table',()=>{
  const f=versions();f.children[2].text=lyricText(['Another opening phrase',...originals.slice(1)]);
  f.parent.text=f.children.map(child=>child.text).join('\n');
  assert.equal(choose(f.tables),f.parent);
  const unknown=versions();
  assert.equal(choose(unknown.tables,originals.map(row=>row.replace('original','unrelated'))),unknown.parent);
});
