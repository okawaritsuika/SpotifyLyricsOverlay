"""Parse explicitly entered lyrics without a file, web request or timing guess."""
import hashlib
import re
from lyrics import Line,Song,normalized
from web_lyrics import reading

FORMATS=('원문만 · 한 줄에 한 구절','원문 / 번역 · 2줄씩','원문 / 독음 / 번역 · 3줄씩')


def parse_manual(text,mode=0,reader=reading):
    if mode not in range(len(FORMATS)):raise ValueError('입력 형식을 선택하세요.')
    if len(text)>500_000:raise ValueError('가사 부분만 입력하세요. 최대 500,000자입니다.')
    text=text.replace('\r\n','\n').replace('\r','\n').strip().lstrip('\ufeff')
    if not text:raise ValueError('가사를 입력하거나 붙여넣으세요.')
    width=mode+1
    if width==1:groups=[[row.strip()] for row in text.splitlines() if row.strip()]
    else:
        blocks=re.split(r'\n[ \t]*\n+',text)
        groups=[]
        for block in blocks:
            rows=[row.strip() for row in block.splitlines()]
            if len(rows)%width:
                raise ValueError(f'각 묶음을 {width}줄씩 입력하세요. 번역이 없으면 「원문만」을 선택하세요.')
            groups.extend(rows[i:i+width] for i in range(0,len(rows),width))
    if len(groups)>600:raise ValueError('한 곡은 최대 600구절까지 입력할 수 있습니다.')
    result=[]
    for group in groups:
        original=group[0]
        if not original:raise ValueError('원문이 비어 있는 구절이 있습니다.')
        kana=group[1] if width==3 else reader(original)
        translation=group[-1] if width>1 else ''
        result.append(Line(original,kana,translation))
    return tuple(result)


def manual_song(title,artist,album,lines):
    values=tuple(value.strip() for value in (title,artist,album))
    if not values[0]:raise ValueError('곡 제목을 입력하세요.')
    if any(len(value)>300 for value in values):raise ValueError('곡 정보는 항목당 300자까지 입력할 수 있습니다.')
    if not lines:raise ValueError('가사를 입력하세요.')
    key='manual\0'+'\0'.join(normalized(value) for value in values)
    return Song(hashlib.sha256(key.encode()).hexdigest()[:24],*values,tuple(lines))
