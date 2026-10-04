"""Persist verified wiki color spans against the current recording's full text."""
from pathlib import Path
import json

from artist_names import artist_label
from group_credits import group_member_lines
from lyrics import normalized
from lyric_match import match_lyrics
from lyric_parts import _recording, text_fingerprint, MAX_BYTES
from storage import atomic_json, read_json
from web_lyrics import document_name


def save_wiki_parts(directory, playback, target_lines, sources, artists=()):
    if not playback.title or not playback.artist or not target_lines:return False
    names=tuple(dict.fromkeys((playback.artist,*artists)))
    credits=artist_label(names,playback.album)+' '+ ' '.join(group_member_lines(
        names,playback.album,update_path=Path(directory)/'group_credits.json'))
    credit_key=normalized(credits)
    entries=[]
    for source in sources:
        profile=source['profile']
        originals=source['lines']
        # A base-song table can donate translation to a solo/version recording,
        # but identical words alone do not establish the same performers.
        if normalized(document_name(source['url']))!=normalized(playback.title):continue
        if any(normalized(singer) not in credit_key for part in profile['parts'] for singer in part['singers']):continue
        if text_fingerprint(originals)!=(profile.get('text_sha256'),profile.get('text_length')):continue
        alignment=match_lyrics([line.original for line in originals],
                               [line.original for line in target_lines])
        if alignment is None:continue
        projection=alignment.source_to_target
        fingerprint,length=text_fingerprint(target_lines)
        entry={'title':playback.title,'artists':list(names),
               'album':playback.album,'text_sha256':fingerprint,'text_length':length,
               'sources':[{'kind':'fan_part_transcription','url':source['url'],
                           'note':'문서의 명시적 색상 범례와 원문 구간을 전체 가사 순서로 대조'}],
               'parts':[{**part,'start':projection[part['start']],'end':projection[part['end']]}
                        for part in profile['parts']
                        if projection[part['start']]<projection[part['end']]]}
        if _recording(entry) is not None:entries.append(entry)
    if not entries:return False
    path=Path(directory)/'wiki_lyric_parts.json'
    if path.exists() and path.stat().st_size>MAX_BYTES:raise ValueError('가창자 자료 파일이 너무 큽니다.')
    catalog=read_json(path,{'schema_version':1,'recordings':[]})
    if (not isinstance(catalog,dict) or catalog.get('schema_version')!=1
            or not isinstance(catalog.get('recordings'),list)
            or any(_recording(row) is None for row in catalog['recordings'])):
        raise ValueError('기존 위키 가창자 자료를 확인하세요. 파일은 보존했습니다.')
    def identity(row):
        return (row['title'],tuple(row['artists']),row.get('album',''),
                row['text_sha256'],tuple(item['url'] for item in row['sources']))
    recordings={identity(row):row for row in catalog['recordings']}
    changed=False
    for entry in entries:
        key=identity(entry)
        if recordings.get(key)!=entry:recordings[key]=entry;changed=True
    if not changed:return False
    if len(recordings)>5000:raise ValueError('위키 가창자 자료가 저장 한도에 도달했습니다.')
    value={'schema_version':1,'recordings':list(recordings.values())}
    if len(json.dumps(value,ensure_ascii=False,indent=2).encode('utf-8'))>MAX_BYTES:
        raise ValueError('위키 가창자 자료가 저장 한도에 도달했습니다.')
    atomic_json(path,value)
    return True
