"""Read public wiki lyric tables; never treat source text as program instructions."""
from datetime import datetime,timezone
import hashlib
import re
import threading
from urllib.error import HTTPError
from urllib.parse import quote,urlparse,unquote
from urllib.request import Request,urlopen

from lyric_match import align_phrases, phrase_key
from lyrics import Line,Song,normalized

JA=re.compile(r'[\u3040-\u30ff\u3400-\u9fff]')
LATIN=re.compile(r'[A-Za-z]')
KO=re.compile(r'[가-힣]')
_readers=threading.local()

# These are Korean grammatical forms, not a list of song-specific meanings.
# Japanese transcriptions such as "키미가 이테 쿠레타카라" do not establish
# the same evidence as Korean inflections or an auxiliary "... 거야" phrase.
_KOREAN_GRAMMAR=re.compile(
    r'[가-힣]*(?:있|없|않|했|됐|왔|갔|였|었|았|겠|줬)[가-힣]+'
    r'|[가-힣]+\s+(?:거야|거지|거예요|것이|것을|것은|것도|것처럼|수\s+있[가-힣]+)(?![가-힣])')


def has_korean_translation_pairs(groups):
    """Recognize a bilingual table only with repeated document-wide evidence.

    Each group is (original, following_hangul_rows) for the complete document.
    A normal three-row table's missing translation must never turn its lone
    pronunciation into a meaning. Repeated refrains count once, and one or
    two Korean-looking lines cannot approve an otherwise ambiguous table.
    """
    if any(len(following)>1 for _,following in groups):return False
    pairs={(normalized(original),following[0]) for original,following in groups
           if len(following)==1}
    evidence={(original,korean) for original,korean in pairs if _KOREAN_GRAMMAR.search(korean)}
    return (len({original for original,_ in evidence})>=3 and
            len({korean for _,korean in evidence})>=3 and len(evidence)*3>=len(pairs))


def namu_url(title):
    return 'https://namu.wiki/w/'+quote(title.strip(),safe='')


def sidem_url(title):
    return 'https://sidem-ko.fandom.com/ko/wiki/'+quote(title.strip().replace(' ','_'),safe='')


def source_id(url):
    parsed=urlparse(url)
    if (parsed.scheme!='https' or parsed.port not in (None,443) or parsed.username or parsed.password):
        raise ValueError('나무위키 또는 SideM KO 위키의 HTTPS 문서 주소를 입력하세요.')
    for host,prefix,source in (('namu.wiki','/w/','namu'),('sidem-ko.fandom.com','/ko/wiki/','sidem')):
        if parsed.hostname==host and parsed.path.startswith(prefix) and parsed.path[len(prefix):]:return source
    raise ValueError('나무위키 또는 SideM KO 위키의 HTTPS 문서 주소를 입력하세요.')


def validate_url(url):
    source_id(url)
    return url.split('#',1)[0]


def document_name(url):
    source=source_id(url)
    name=unquote(urlparse(url).path[3 if source=='namu' else 9:])
    return name.replace('_',' ') if source=='sidem' else name


def document_identity(url):
    import unicodedata
    return source_id(url),unicodedata.normalize('NFKC',document_name(url))


def reading(text):
    from fugashi import Tagger
    import jaconv
    from pykakasi import kakasi
    if not hasattr(_readers,'tagger'):
        _readers.tagger=Tagger()
        _readers.fallback=kakasi()
    parts=[]
    for word in _readers.tagger(text):
        kana=word.feature.kana
        if not kana or kana=='*':
            kana=''.join(item['hira'] for item in _readers.fallback.convert(word.surface))
        parts.append(jaconv.kata2hira(kana))
    return ' '.join(parts)


def parse_table_text(text,reader=reading,strict=True,source='namu'):
    from bs4 import BeautifulSoup
    if source not in ('namu','sidem'):raise ValueError('지원하지 않는 가사 출처입니다.')
    if len(text)>1_000_000: raise ValueError('가사 표가 너무 큽니다. 가사 부분만 복사해 주세요.')
    # Supports both copied text and Markdown tables containing literal <br> tags.
    soup=BeautifulSoup(text,'html.parser')
    for item in soup.select('rt,rp'):item.decompose()
    for br in soup.find_all('br'):br.replace_with('\n')
    text=soup.get_text()
    rows=[]
    for line in text.replace('\r','').splitlines():
        line=re.sub(r'\[\d+\]','',line).strip().strip('|').strip()
        if line and not re.fullmatch(r'[:\- ]+',line): rows.append(line)
    groups=[];index=0
    while index<len(rows):
        original=rows[index]
        if not (JA.search(original) or LATIN.search(original)) or KO.search(original):
            index+=1
            continue
        following=[]; cursor=index+1
        while cursor<len(rows) and (KO.search(rows[cursor]) or not (JA.search(rows[cursor]) or LATIN.search(rows[cursor]))):
            if KO.search(rows[cursor]): following.append(rows[cursor])
            cursor+=1
        groups.append((original,following))
        index=max(cursor,index+1)
    pairs=source=='sidem' or has_korean_translation_pairs(groups)
    lines=[];skipped=0
    for original,following in groups:
        # Namu normally uses pronunciation then translation; a consistently
        # bilingual table needs independent Korean grammar evidence first.
        if len(following)==2 or pairs and len(following)==1:
            lines.append(Line(original,reader(original),following[-1]))
        elif not following and LATIN.search(original) and not JA.search(original):
            # English refrains may be printed once, without pronunciation or meaning.
            lines.append(Line(original,reader(original),''))
        else:
            skipped+=1
            if not strict:lines.append(Line(original,reader(original),''))
    if not lines: raise ValueError('가사 표에서 일본어·영어 원문을 찾지 못했습니다.')
    if strict and not any(line.korean for line in lines): raise ValueError('원문과 한국어 번역이 확인되는 가사 묶음을 찾지 못했습니다.')
    if strict and skipped:
        raise ValueError(f'{skipped}개 구절의 번역 묶음이 불완전합니다. 가사 표 전체를 복사해 주세요.')
    return tuple(lines)


def _duplicate_table_container(table,lines,candidates,references):
    """Exclude a layout containing separate copies of one referenced song."""
    reference=[normalized(getattr(row,'original',row)) for row in references]
    reference_text=''.join(reference)
    phrases={row for row in reference if len(row)>=6}
    if len(reference)<8 or len(reference_text)<100 or len(phrases)<4:return False
    parent=[normalized(line.original) for line in lines]
    def contains(outer,inner):return any(ancestor is outer for ancestor in inner.parents)
    for child,child_lines in candidates:
        if child is table or not contains(table,child) or len(child_lines)<12:continue
        rows=[normalized(line.original) for line in child_lines];text=''.join(rows)
        if not .8<=len(text)/len(reference_text)<=1.2 or sum(phrase in text for phrase in phrases)/len(phrases)<.8:continue
        if len(parent)<len(rows)*2 or len(parent)%len(rows):continue
        if any(row!=rows[index%len(rows)] for index,row in enumerate(parent)):continue
        copies=[node for node,node_lines in candidates if node is not table and contains(table,node)
                and [normalized(line.original) for line in node_lines]==rows]
        if any(not contains(left,right) and not contains(right,left)
               for index,left in enumerate(copies) for right in copies[index+1:]):return True
    return False


def parse_html(html,expected_title,reader=reading,source='namu',reference_lines=(),*,merge_partial=False):
    from bs4 import BeautifulSoup
    soup=BeautifulSoup(html,'html.parser')
    heading=soup.find('h1')
    document_title=heading.get_text(' ',strip=True) if heading else (soup.title.get_text() if soup.title else '')
    document_title=re.sub(r'\s*(?:-\s*나무위키|\|\s*SideM KO 위키).*$', '',document_title).strip()
    if normalized(document_title)!=normalized(expected_title):
        raise ValueError('문서 제목이 현재 곡과 다릅니다. 정확한 가사 문서를 지정해 주세요.')
    section=next((h for h in soup.find_all(['h2','h3','h4'])
                  if re.search(r'(?:^|\s|\.)가사(?:\s|\[|$)',h.get_text(' ',strip=True))),None)
    if section is None: raise ValueError('문서에서 가사 섹션을 찾지 못했습니다.')
    candidates=[]
    for element in section.find_all_next():
        if element.name in ('h2','h3','h4') and int(element.name[1])<=int(section.name[1]):
            break
        if element.name=='table': candidates.append(element)
    if not candidates: raise ValueError('가사 표를 찾지 못했습니다. 표를 복사해서 가져올 수 있습니다.')
    parsed_tables=[]
    references=tuple(getattr(line,'original',line) for line in reference_lines) if merge_partial else ()
    for table in candidates:
        # Parse each candidate independently; editing the outer table would
        # otherwise also alter every nested version's source nodes.
        content=BeautifulSoup(str(table),'html.parser')
        for item in content.select('script,style,sup,rt,rp'):
            item.decompose()
        for br in content.find_all('br'): br.replace_with('\n')
        for cell in content.find_all(['td','th','p','div']): cell.append('\n')
        try:
            parsed=parse_table_text(content.get_text(),reader,strict=not bool(references),source=source)
        except ValueError: continue
        parsed_tables.append((table,parsed))
    if references:
        approved=[]
        size=sum(len(phrase_key(row)) for row in references)
        for table,parsed in parsed_tables:
            if _duplicate_table_container(table,parsed,parsed_tables,reference_lines):continue
            originals=tuple(line.original for line in parsed)
            keys=tuple(map(phrase_key,originals))
            matches=align_phrases(references,originals)
            common=sum(len(key) for block in matches for key in keys[block.right:block.right_end])
            length=sum(map(len,keys))
            if common and common>=.72*size and common>=.60*length:
                approved.append(((common,common/length),keys,parsed))
        if not approved:raise ValueError('현재 곡과 구절 순서가 일치하는 가사 표를 찾지 못했습니다.')
        best_score=max(score for score,_,_ in approved)
        best=[(keys,rows) for score,keys,rows in approved if score==best_score]
        if len({keys for keys,_ in best})>1:raise ValueError('가사 표에 서로 다른 버전이 있어 선택을 보류했습니다.')
        return max((rows for _,rows in best),key=lambda rows:sum(bool(row.korean) for row in rows)),document_title
    best=None
    for table,parsed in parsed_tables:
        if _duplicate_table_container(table,parsed,parsed_tables,reference_lines):continue
        if best is None or len(parsed)>len(best):best=parsed
    if best is None: raise ValueError('가사 표의 원문·발음·번역을 안전하게 구분하지 못했습니다. 표를 복사해 확인해 주세요.')
    return best,document_title


def fetch_lyrics(title,url='',fetch=None,reference_lines=(),*,merge_partial=False):
    url=validate_url(url or namu_url(title))
    source=source_id(url)
    label='SideM KO 위키' if source=='sidem' else '나무위키'
    def request(address):
        try:
            with urlopen(Request(address,headers={'User-Agent':'SpotifyLyricsOverlay/0.2 (personal lyrics reader)'}),timeout=15) as response:
                if source_id(response.url)!=source:raise ValueError('다른 사이트로 이동하여 가져오기를 중단했습니다.')
                data=response.read(3_000_001)
                if len(data)>3_000_000: raise ValueError('문서가 너무 큽니다. 가사 표만 가져와 주세요.')
                return data.decode('utf-8')
        except HTTPError as error:
            if error.code in (403,429):
                raise ValueError(f'{label}가 자동 접근을 제한했습니다. 원본 브라우저에서 확인하거나 가사 표를 복사해 가져오세요.') from error
            raise ValueError(f'{label} 문서를 가져오지 못했습니다 (HTTP {error.code}).') from error
    lines,document_title=parse_html((fetch or request)(url),title,source=source,reference_lines=reference_lines,
                                    merge_partial=merge_partial)
    return lines,{'url':url,'title':document_title,'fetched_at':datetime.now(timezone.utc).isoformat(),
                  'translation':label+'의 한국어 번역','reading':'로컬 자동 히라가나 독음'}


def make_song(title,artist,album,lines):
    key='web\0'+'\0'.join(normalized(x) for x in (title,artist,album))
    return Song(hashlib.sha256(key.encode()).hexdigest()[:24],title,artist,album,tuple(lines))
