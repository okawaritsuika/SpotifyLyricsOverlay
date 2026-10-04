"""Search aids only: original recording titles and credits remain unchanged.

Generated readings and editorial translations are not official Korean titles.
Recording verification and playback/timing identity must never use these terms.
"""
from functools import lru_cache
import json
from pathlib import Path
import re
import unicodedata


RESOURCES = Path(__file__).resolve().parent / 'resources'
JAPANESE = re.compile(r'[\u3040-\u30ff\u3400-\u9fff]')
_DECORATION = re.compile(r'[♡♥☆★♪♫✿・]')
_KANA = {}
for kana, hangul in (
    ('アイウエオ', '아 이 우 에 오'), ('ァィゥェォ', '아 이 우 에 오'),
    ('カキクケコ', '카 키 쿠 케 코'), ('ガギグゲゴ', '가 기 구 게 고'),
    ('サシスセソ', '사 시 스 세 소'), ('ザジズゼゾ', '자 지 즈 제 조'),
    ('タチツテト', '타 치 츠 테 토'), ('ダヂヅデド', '다 지 즈 데 도'),
    ('ナニヌネノ', '나 니 누 네 노'), ('ハヒフヘホ', '하 히 후 헤 호'),
    ('バビブベボ', '바 비 부 베 보'), ('パピプペポ', '파 피 푸 페 포'),
    ('マミムメモ', '마 미 무 메 모'), ('ヤユヨャュョ', '야 유 요 야 유 요'),
    ('ラリルレロ', '라 리 루 레 로'), ('ワヲヮヴヵヶ', '와 오 와 부 카 케')):
    _KANA.update(zip(kana, hangul.split()))
_COMBINATIONS = dict(zip(
    'キャ キュ キョ ギャ ギュ ギョ シャ シュ ショ ジャ ジュ ジョ チャ チュ チョ '
    'ニャ ニュ ニョ ヒャ ヒュ ヒョ ビャ ビュ ビョ ピャ ピュ ピョ ミャ ミュ ミョ '
    'リャ リュ リョ シェ ジェ チェ ティ ディ トゥ ドゥ ファ フィ フェ フォ フュ '
    'ウィ ウェ ウォ ヴァ ヴィ ヴェ ヴォ ヴュ'.split(),
    '캬 큐 쿄 갸 규 교 샤 슈 쇼 자 주 조 차 추 초 '
    '냐 뉴 뇨 햐 휴 효 뱌 뷰 뵤 퍄 퓨 표 먀 뮤 묘 '
    '랴 류 료 셰 제 체 티 디 투 두 파 피 페 포 퓨 '
    '위 웨 워 바 비 베 보 뷰'.split()))


def _key(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(text))).casefold()


def _final(parts, consonant):
    if parts and len(parts[-1]) == 1 and '가' <= parts[-1] <= '힣':
        value = ord(parts[-1]) - 0xac00
        if value % 28 == 0:
            parts[-1] = chr(ord(parts[-1]) + consonant)
            return True
    return False


def kana_hangul(value, *, long_vowels=False, loan_endings=False):
    """Approximate kana sounds; this is neither translation nor title authority."""
    value = unicodedata.normalize('NFKC', value)
    value = ''.join(chr(ord(char)+0x60) if 'ぁ' <= char <= 'ゖ' else char for char in value)
    parts=[];index=0;previous=''
    while index < len(value):
        char=value[index]
        pair=value[index:index+2]
        if char=='ッ':
            _final(parts,19);index+=1;previous='';continue
        if char=='ン':
            if not _final(parts,4):parts.append('응')
            index+=1;previous='';continue
        if char=='ー':
            index+=1;continue  # Korean search spelling normally omits vowel length.
        if previous and not long_vowels and ((char=='ウ' and previous in 'オコゴソゾトドノホボポモヨロョ')
                                or (char=='イ' and previous in 'エケゲセゼテデネヘベペメレェ')):
            index+=1;previous=char;continue
        token=pair if pair in _COMBINATIONS else char
        converted=_COMBINATIONS.get(token) or _KANA.get(token,token)
        if loan_endings and index+len(token)==len(value):
            converted={'ル':'르','ブ':'브','ト':'트'}.get(token,converted)
        parts.append(converted);previous=token[-1];index+=len(token)
    return ''.join(parts)


@lru_cache(maxsize=1)
def _reader():
    from pykakasi import kakasi
    return kakasi()


def generate_readings(title):
    if not JAPANESE.search(title):return ()
    pieces=_reader().convert(title)
    kana=[piece['kana'] for piece in pieces]
    values=[''.join(kana_hangul(piece) for piece in kana),
            ''.join(kana_hangul(piece,long_vowels=True) for piece in kana),
            ''.join(kana_hangul(piece,loan_endings=True) for piece in kana),
            ''.join(piece['hira'] for piece in pieces),
            ''.join(piece['hepburn'] for piece in pieces)]
    return tuple(dict.fromkeys(value for value in values if _key(value)!=_key(title)))


@lru_cache(maxsize=2)
def _resource(name):
    try:
        with (RESOURCES/name).open('rb') as stream:raw=stream.read(8*1024*1024+1)
        if len(raw)>8*1024*1024:return {}
        value=json.loads(raw)
        return value if isinstance(value,dict) and value.get('schema_version')==1 else {}
    except (OSError,ValueError):return {}


@lru_cache(maxsize=1)
def _reading_index():
    entries=_resource('song_readings.json').get('entries',{})
    if not isinstance(entries,dict):return {}
    return {_key(title):tuple(values) for title,values in entries.items()
            if isinstance(values,list) and all(isinstance(value,str) for value in values)}


@lru_cache(maxsize=20000)
def title_readings(title):
    if not JAPANESE.search(title):return ()
    return _reading_index().get(_key(title)) or generate_readings(title)


def alias_information(song):
    """Keep provenance categories distinct; exact titles and brands only."""
    result={'korean_titles':[], 'readings':[], 'search_translations':[], 'source_urls':[]}
    title=_key(song.get('title',''))
    series=set(song.get('series',[]))
    for item in _resource('song_aliases.json').get('entries',[]):
        if not isinstance(item,dict):continue
        if title not in {_key(value) for value in item.get('matching_titles',[])}:continue
        if not series.intersection(item.get('series',[])):continue
        for field in result:result[field].extend(item.get(field,[]))
    return result


@lru_cache(maxsize=20000)
def _search_aliases(title, series):
    info=alias_information({'title':title,'series':series})
    values=info['korean_titles']+info['readings']+info['search_translations']+list(title_readings(title))
    values += [_DECORATION.sub('',value) for value in values]
    return tuple(dict.fromkeys(values))


def search_aliases(song):
    return _search_aliases(song.get('title',''),tuple(sorted(song.get('series',[]))))
