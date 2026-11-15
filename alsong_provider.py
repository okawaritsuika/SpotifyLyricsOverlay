"""Read verified ALSong timings and clock-free translation annotations.

Only search metadata and selected lyric IDs are sent to the public endpoint.
Reference lyrics stay local; this module does not save or follow redirects.
"""
import html
from http.client import HTTPException
import itertools
import math
import re
from types import SimpleNamespace
import unicodedata
import xml.etree.ElementTree as ET
from urllib.request import HTTPRedirectHandler, Request, build_opener
from xml.sax.saxutils import escape

from lyrics import Line, normalized
from lyric_match import match_lyrics
from lyric_annotations import (match_parenthetical_annotations, match_vocalise_annotations,
                               match_sequence_annotations)
from lyric_asides import aside_onsets
from lyric_merge import has_ordered_overlap
from web_lyrics import has_korean_translation_pairs


ENDPOINT = 'http://lyrics.alsong.co.kr/alsongwebservice/service1.asmx'
REQUEST_TIMEOUT = 5
MAX_RESPONSE_BYTES = 2_000_000
MAX_LYRIC_CHARACTERS = 200_000
MAX_LYRIC_LINES = 1_500
MAX_LINE_CHARACTERS = 4_000
MAX_CANDIDATES = 100
MAX_DETAILS = 3
MAX_TRANSLATION_DETAILS = 6
SEARCH_ACTION = 'GetResembleLyricList2'
DETAIL_ACTION = 'GetLyricByID2'
# Public client constant and protocol reference, not obtained from an account:
# https://github.com/medAndro/lyrs-eggsong/blob/main/src/provider.js
# lyrs-eggsong is distributed under Apache-2.0:
# https://github.com/medAndro/lyrs-eggsong/blob/main/LICENSE
ENC_DATA = ('7c2d15b8f51ac2f3b2a37d7a445c3158455defb8a58d621eb77a3ff8ae4921318'
            'e49cefe24e515f79892a4c29c9a3e204358698c1cfe79c151c04f9561e945096c'
            'cd1d1c0a8d8f265a2f3fa7995939b21d8f663b246bbc433c7589da7e68047524'
            'b80e16f9671b6ea0faaf9d6cde1b7dbcf1b89aa8a1d67a8bbc566664342e12')
_SEARCH_FIELDS = {'lyricID', 'title', 'artist', 'album'}
_DETAIL_FIELDS = _SEARCH_FIELDS | {'lyric', 'playtime'}
_STAMP = re.compile(r'^\[(\d{1,3}):([0-5]\d)(?:[.:](\d{1,3}))?\]')
_HANGUL = re.compile(r'[\u1100-\u11ff\u3130-\u318f\uac00-\ud7a3]')
_JAPANESE = re.compile(r'[\u3040-\u30ff\u3400-\u9fff]')


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        return None


def _request(action, params):
    allowed = {'title', 'artist', 'pageNo'} if action == SEARCH_ACTION else {'lyricID'}
    if action not in (SEARCH_ACTION, DETAIL_ACTION) or set(params) != allowed:
        raise ValueError('지원하지 않는 알송 요청입니다.')
    fields = ''.join('<' + key + '>' + escape(str(value)) + '</' + key + '>'
                     for key, value in params.items())
    body = ('<?xml version="1.0" encoding="utf-8"?>'
            '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
            '<soap:Body><' + action + ' xmlns="ALSongWebServer">'
            '<encData>' + ENC_DATA + '</encData>' + fields
            + '</' + action + '></soap:Body></soap:Envelope>')
    request = Request(ENDPOINT, data=body.encode('utf-8'), headers={
        'User-Agent': 'gSOAP/2.7', 'Content-Type': 'text/xml; charset=utf-8',
        'SOAPAction': 'ALSongWebServer/' + action,
    })
    with build_opener(_NoRedirect()).open(request, timeout=REQUEST_TIMEOUT) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError('알송 응답이 너무 큽니다.')
    return body


def _text(value):
    return value.strip() if isinstance(value, str) else ''


def _reference(reference):
    if isinstance(reference, (str, bytes)):
        return ()
    try:
        rows = list(itertools.islice(iter(reference), MAX_LYRIC_LINES + 1))
    except TypeError:
        return ()
    if not rows or len(rows) > MAX_LYRIC_LINES:
        return ()
    texts = [row.original if isinstance(row, Line) else row for row in rows]
    if (any(not isinstance(text, str) or len(text) > MAX_LINE_CHARACTERS for text in texts)
            or sum(map(len, texts)) > MAX_LYRIC_CHARACTERS):
        return ()
    return tuple(text for text in texts if normalized(text))


def _local(element):
    return element.tag.rsplit('}', 1)[-1]


def _response(payload, action):
    if not isinstance(payload, (str, bytes)):
        raise ValueError('알송 응답 형식이 올바르지 않습니다.')
    if isinstance(payload, str):
        payload = payload.encode('utf-8')
    if len(payload) > MAX_RESPONSE_BYTES:
        raise ValueError('알송 응답이 너무 큽니다.')
    # The service uses plain XML strings, never DTDs or entity declarations.
    if b'\0' in payload or b'<!doctype' in payload.lower() or b'<!entity' in payload.lower():
        raise ValueError('알송 응답에 지원하지 않는 XML 선언이 있습니다.')
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as error:
        raise ValueError('알송 응답 XML이 올바르지 않습니다.') from error
    responses = [element for element in root.iter() if _local(element) == action + 'Response']
    if any(_local(element) == 'Fault' for element in root.iter()) or len(responses) != 1:
        raise ValueError('알송 검색 응답을 확인하지 못했습니다.')
    return responses[0]


def _fields(element, allowed):
    children = [child for child in element if _local(child) in allowed]
    if len({_local(child) for child in children}) != len(children) or any(len(child) for child in children):
        raise ValueError('알송 검색 결과 필드가 올바르지 않습니다.')
    # Do not retain contributor names, email addresses, comments, or extra data.
    return {_local(child): child.text or '' for child in children}


def _records(payload):
    response = _response(payload, SEARCH_ACTION)
    results = [child for child in response if _local(child) == SEARCH_ACTION + 'Result']
    # The live service omits Result entirely for zero matches.
    if not results and not len(response) and not _text(response.text):
        return []
    if len(results) != 1:
        raise ValueError('알송 검색 목록을 확인하지 못했습니다.')
    records = []
    for element in results[0]:
        if _local(element) != 'ST_SEARCHLYRIC_LIST':
            continue
        if len(records) >= MAX_CANDIDATES:
            raise ValueError('알송 검색 결과가 너무 많습니다.')
        records.append(_fields(element, _SEARCH_FIELDS))
    return records


def _detail(payload):
    response = _response(payload, DETAIL_ACTION)
    outputs = [child for child in response if _local(child) == 'output']
    if not outputs:
        return None
    if len(outputs) != 1:
        raise ValueError('알송 가사 상세 응답이 중복되었습니다.')
    return _fields(outputs[0], _DETAIL_FIELDS)


def _identifier(value):
    value = _text(value)
    return value if value.isascii() and value.isdigit() and len(value) <= 32 else None


def validate_alsong_query(title, artist, album):
    title, artist, album = title.strip(), artist.strip(), album.strip()
    if not normalized(title):
        raise ValueError('곡 제목을 입력하세요.')
    if any(len(value) > 512 for value in (title, artist, album)):
        raise ValueError('검색어를 512자 이하로 입력하세요.')
    return title, artist, album


def search_alsong(title, artist='', album='', *, fetch=None):
    """List registrations for an explicit, editable search."""
    title, artist, album = validate_alsong_query(title, artist, album)
    records = _records((fetch or _request)(SEARCH_ACTION, {'title': title, 'artist': artist, 'pageNo': '1'}))
    items = {}
    for record in records:
        identifier = _identifier(record.get('lyricID'))
        if identifier and _text(record.get('title')):
            items.setdefault(identifier, {field: _text(record.get(field)) for field in _SEARCH_FIELDS})
    # Album is a local preference. ALSong commonly leaves it empty.
    wanted = normalized(album)
    return sorted(items.values(), key=lambda item: bool(wanted and normalized(item['album']) != wanted))


def load_alsong_registration(identifier, duration, *, fetch=None):
    """Decode only the selected registration for a manual preview."""
    if not _identifier(identifier):
        raise ValueError('알송 등록 번호를 확인할 수 없습니다.')
    detail = _detail((fetch or _request)(DETAIL_ACTION, {'lyricID': identifier}))
    if not detail or _identifier(detail.get('lyricID')) != identifier:
        raise ValueError('선택한 알송 등록본을 받지 못했습니다.')
    rows = _document_rows(_text(detail.get('lyric')))
    if rows is None:
        raise ValueError('알송 가사 길이가 지원 범위를 벗어납니다.')
    annotations = _parallel_annotations(rows)
    groups = _parallel_groups(row for row in rows if not re.fullmatch(
        r'\[(?:ar|ti|al|by|re|ve|offset):[^\]]*\]', row.strip(), re.I))
    lines, times = [], []
    for stamp, texts in groups:
        text = texts[0]
        extra = annotations.get((normalized(text), stamp))
        if extra is None:
            text = ' '.join(texts)
        if len(text) > MAX_LINE_CHARACTERS:
            raise ValueError('알송 가사 한 줄이 너무 깁니다.')
        lines.append(Line(text, *(extra or ('', ''))))
        times.append(stamp)
    if not lines:
        raise ValueError('선택한 등록본의 가사가 비어 있습니다.')
    playtime = _text(detail.get('playtime'))
    seconds = float(playtime) if re.fullmatch(r'\d+(?:\.\d+)?', playtime) else 0
    seconds = seconds if seconds and math.isfinite(seconds) else None
    known = [stamp for stamp in times if stamp is not None]
    clock_mismatch = (seconds is not None and abs(seconds-duration) > 2
                      or any(stamp >= duration for stamp in known))
    ordered = all(before < after for before, after in zip(known, known[1:]))
    return {'provider_id': identifier, 'title': _text(detail.get('title')), 'artist': _text(detail.get('artist')),
            'album': _text(detail.get('album')), 'lines': tuple(lines),
            'duration': seconds,
            'last_onset': known[-1] if known else None,
            'times': times if ordered else [None]*len(lines), 'clock_mismatch': clock_mismatch}


def _artist_matches(local, remote):
    def artists(value):
        # Trim each list item before splitting collaborations so the leading X
        # in "Other、 X JAPAN" stays part of the name. Slash and apostrophe are
        # never separators; x and ampersand require whitespace on both sides.
        return {normalized(part) for group in re.split(r'[,、;×]', _text(value))
                for part in re.split(r'\s+[xX&＆]\s+', group.strip()) if normalized(part)}
    local_artists = artists(local)
    remote_artists = artists(remote)
    return bool(local_artists and local_artists <= remote_artists)


def _metadata_matches(item, playback):
    return (normalized(_text(item.get('title'))) == normalized(playback.title)
            and _artist_matches(playback.artist, item.get('artist')))


def _stamped_row(row):
    stamp = _STAMP.match(row)
    if stamp is None:
        return None, row
    fraction = stamp[3] or ''
    timestamp = int(stamp[1]) * 60 + int(stamp[2]) + (int(fraction) / 10 ** len(fraction) if fraction else 0)
    return timestamp, row[stamp.end():].strip()


def _document_rows(lyric):
    if len(lyric) > MAX_LYRIC_CHARACTERS:
        return None
    lyric = re.sub(r'<br\s*/?>', '\n', html.unescape(lyric), flags=re.I)
    lyric = re.sub(r'</?(?:font|b|i|u)(?:\s[^<>]*)?>', '', lyric, flags=re.I)
    rows = lyric.splitlines()
    return rows if len(rows) <= MAX_LYRIC_LINES else None


def _parallel_groups(rows):
    groups = []
    for row in rows:
        timestamp, text = _stamped_row(row.strip())
        if not normalized(text):
            continue
        if timestamp is None or not groups or timestamp != groups[-1][0]:
            groups.append((timestamp, []))
        groups[-1][1].append(text)
    return groups


def _parallel_annotations(rows):
    # Three-row groups keep their established original/reading/meaning roles.
    # Two-row groups require independent document-wide Korean meaning evidence;
    # a lone Korean row alone still cannot establish reading versus meaning.
    groups = _parallel_groups(rows)
    pairs_are_meanings=has_korean_translation_pairs([
        (texts[0],texts[1:]) for _,texts in groups
        if texts and re.search(r'[A-Za-z\u3040-\u30ff\u3400-\u9fff]',texts[0])
        and not _HANGUL.search(texts[0])])
    annotations = {}
    for timestamp, texts in groups:
        if (timestamp is None or len(texts) not in ((2,3) if pairs_are_meanings else (3,))
                or not re.search(r'[A-Za-z\u3040-\u30ff\u3400-\u9fff]',texts[0]) or _HANGUL.search(texts[0])
                or any(not _HANGUL.search(text) or _JAPANESE.search(text) for text in texts[1:])):
            continue
        key = (normalized(texts[0]), timestamp)
        value = ('',texts[1]) if len(texts)==2 else (texts[1],texts[2])
        annotations[key] = value if key not in annotations or annotations[key] == value else None
    return annotations if len(annotations) >= 2 else {}


def _formatted_rows(rows, item, reference_key):
    """Repair proven display slots only when the remaining whole original is exact."""
    if any(len(row.strip()) > MAX_LINE_CHARACTERS for row in rows):
        return rows
    groups = _parallel_groups(rows)
    stamps = [stamp for stamp, _ in groups if stamp is not None]
    if any(left > right for left, right in zip(stamps, stamps[1:])):
        return rows
    japanese_triples = {normalized(texts[0]) for stamp, texts in groups
                        if stamp is not None and len(texts) == 3
                        and _JAPANESE.search(texts[0]) and not _HANGUL.search(texts[0])
                        and all(_HANGUL.search(text) and not _JAPANESE.search(text) for text in texts[1:])}
    repeated = {(stamp, texts[0]) for stamp, texts in groups
                if len(japanese_triples) >= 2 and stamp is not None and len(texts) == 3
                and len(set(texts)) == 1 and re.search(r'[A-Za-z]', texts[0])
                and not _JAPANESE.search(texts[0]) and not _HANGUL.search(texts[0])}
    header_time = None
    if len(groups) > 1 and groups[0][0] is not None and groups[1][0] is not None:
        stamp, texts = groups[0]
        originals = [text for text in texts if not (_HANGUL.search(text) and not _JAPANESE.search(text))]
        title = normalized(_text(item.get('strTitle')))
        album = re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', _text(item.get('strAlbumName')))).casefold()
        # A credit may abbreviate the album's franchise prefix, retaining its
        # exact final words. A title alone, or a title later in the song, is lyric.
        album_headers = [text for text in originals if normalized(text) != title
                         and (normalized(text) == normalized(album) or
                              len(normalized(text)) >= 8 and album.endswith(' ' + re.sub(
                                  r'\s+', ' ', unicodedata.normalize('NFKC', text)).casefold()))]
        if (0 <= stamp < groups[1][0] and len(originals) == 2
                and any(normalized(text) == title for text in originals) and len(album_headers) == 1):
            header_time = stamp
    if not repeated and header_time is None:
        return rows
    cleaned, seen = [], set()
    in_header = header_time is not None
    previous_stamp = None
    for row in rows:
        stamp, text = _stamped_row(row.strip())
        if in_header:
            if stamp == header_time:
                continue
            if normalized(text):
                in_header = False
        if normalized(text) and stamp != previous_stamp:
            seen.clear()
            previous_stamp = stamp
        key = (stamp, text)
        if key in repeated:
            if key in seen:
                continue
            seen.add(key)
        cleaned.append(row)
    originals = []
    for row in cleaned:
        stamp, text = _stamped_row(row.strip())
        if _HANGUL.search(text) and not _JAPANESE.search(text):
            continue
        if re.fullmatch(r'\[(?:ar|ti|al|by|re|ve):[^\]]*\]|\[offset:0\]', text, re.I):
            continue
        originals.append(text)
    # Never turn a missing repeated lyric/verse into an accepted formatting
    # difference, or drop a legitimate first lyric that happens to be the title.
    return cleaned if normalized(''.join(originals)) == reference_key else rows


def _candidate(item, playback, reference, duration, merge_partial=False):
    # Retain the entire recording title. A matching chorus cannot distinguish a
    # live version, another singer's recording, or a shorter edit on its own.
    if (normalized(_text(item.get('strTitle'))) != normalized(playback.title)
            or not _artist_matches(playback.artist, item.get('strArtistName'))
            or _text(item.get('strOnlyLyricWord')) not in ('', '0')):
        return None
    rows = _document_rows(item.get('strLyric', ''))
    if rows is None:
        return None
    reference_key = normalized(''.join(reference))
    japanese_reference = bool(_JAPANESE.search(reference_key)) and not _HANGUL.search(reference_key)
    if japanese_reference:
        rows = _formatted_rows(rows, item, reference_key)
    originals, times = [], []
    source_originals, source_starts, source_times = [], [], []
    position = 0
    previous = -1.0
    source_lines = 0
    metadata = {normalized(_text(item.get(field))) for field in ('strTitle', 'strArtistName', 'strAlbumName')}
    for row in rows:
        row = row.strip()
        if len(row) > MAX_LINE_CHARACTERS:
            return None
        if re.fullmatch(r'\[(?:ar|ti|al|by|re|ve):[^\]]*\]', row, re.I):
            continue
        if re.fullmatch(r'\[offset:0\]', row, re.I):
            continue
        if row.startswith('[') and _STAMP.match(row) is None and re.match(r'^\[(?:[+-]?\d|[a-z]+:)', row, re.I):
            return None
        timestamp, row = _stamped_row(row)
        if re.match(r'^\[(?:[+-]?\d|[a-z]+:)', row, re.I):
            return None
        key = normalized(row)
        if not key:
            continue
        # Korean readings/translations in a Japanese document can carry their
        # own repeated stamps. They never supply an original's timestamp.
        if japanese_reference and _HANGUL.search(row) and not _JAPANESE.search(row):
            continue
        if timestamp in (None, 0) and key in metadata and not reference_key.startswith(key, position):
            continue
        source_originals.append(row)
        source_times.append(timestamp)
        source_lines += 1
        if timestamp is not None:
            if (timestamp > duration or timestamp < previous
                    or (timestamp == previous and times[-1] != timestamp)):
                return None
            previous = timestamp
        if times and timestamp is not None and timestamp == times[-1]:
            originals[-1] += ' ' + row
            if len(originals[-1]) > MAX_LINE_CHARACTERS:
                return None
        else:
            originals.append(row)
            times.append(timestamp)
            source_starts.append(position)
        position += len(key)
    if (not any(time is not None for time in times)
            or (source_lines > 1 and times == [0])):
        return None
    # Validate before merging equal-time rows, so a missing complete original
    # cannot be mistaken for a few allowed character edits inside a longer row.
    match = match_lyrics(source_originals, reference)
    annotations = _parallel_annotations(rows) if japanese_reference else {}
    remote_album = _text(item.get('strAlbumName'))
    album = normalized(_text(getattr(playback, 'album', '')))
    merge_metadata = {'title': _text(item.get('strTitle')),
                      'artist': _text(playback.artist),  # _artist_matches checked the complete credit.
                      'album': remote_album, 'duration': item.get('duration')}
    if match is None:
        if (merge_partial and album and normalized(remote_album) == album and
                has_ordered_overlap(reference, originals)):
            return {'source': 'ALSong', 'provider_id': _identifier(item.get('strInfoID')),
                    'lines': tuple(Line(text, *(annotations.get((normalized(text), stamp)) or ('', '')))
                                   for text, stamp in zip(originals, times)), 'times': times,
                    **merge_metadata, '_album': album, '_edits': MAX_LYRIC_CHARACTERS,
                    '_positions': tuple(normalized(text) for text in originals)}
        asides = (aside_onsets(source_originals, reference)
                  if album and normalized(_text(item.get('strAlbumName'))) == album else None)
        if asides:
            edits, pairs = asides
            mapped = [None] * len(reference)
            display = [Line(text, '', '') for text in reference]
            for source, target in pairs:
                mapped[target] = source_times[source]
                if normalized(source_originals[source]) == normalized(reference[target]):
                    extra = annotations.get((normalized(source_originals[source]), source_times[source]))
                    if extra:
                        display[target] = Line(reference[target], *extra)
            if sum(stamp is not None for stamp in mapped) >= 2:
                offsets = []
                offset = 0
                for text in reference:
                    offsets.append(offset)
                    offset += len(normalized(text))
                return {'source': 'ALSong', 'provider_id': _identifier(item.get('strInfoID')),
                        'lines': tuple(display), 'times': mapped, '_album': album,
                        '_edits': edits, '_positions': tuple(offsets)}
        supplied=tuple(Line(text,*(annotations.get((normalized(text),timestamp)) or ('','')))
                       for text,timestamp in zip(source_originals,source_times))
        matched=match_parenthetical_annotations(reference,supplied) if annotations else None
        if matched is None and annotations:
            matched=match_vocalise_annotations(reference,supplied)
        if matched:
            return {'source':'ALSong','provider_id':_identifier(item.get('strInfoID')),
                    'annotation_only':True,
                    'lines':tuple(Line(text,extra.reading if normalized(text)==normalized(extra.original) else '',
                                       extra.korean) if extra else Line(text,'','')
                                  for text,extra in zip(reference,matched)),
                    'times':[None]*len(reference),
                    '_album':normalized(_text(item.get('strAlbumName')))}
        return None
    return {'source': 'ALSong', 'provider_id': _identifier(item.get('strInfoID')),
            'lines': tuple(Line(text, *(annotations.get((normalized(text), timestamp)) or ('', '')))
                           for text, timestamp in zip(originals, times)), 'times': times,
            '_album': normalized(_text(item.get('strAlbumName'))), '_edits': match.edits,
            **(merge_metadata if merge_partial else {}),
            '_positions': tuple(match.source_to_target[start] for start in source_starts)}


def _clocks_agree(left, right):
    # Compare observed onsets at the same reference boundaries. Different row
    # layouts stay ambiguous; this never creates or averages timestamps.
    if left['_positions'] != right['_positions']:
        return False
    paired = [(first, second) for first, second in zip(left['times'], right['times'])
              if first is not None and second is not None]
    return bool(paired) and all(abs(first - second) <= 0.5 + 1e-9 for first, second in paired)


def _translation_base_title(title):
    # Only recognizable recording labels may broaden translation discovery.
    # This title is never used to select another recording's clock.
    text=unicodedata.normalize('NFKC',title).strip()
    suffix=re.search(r'\s*\(([^()]+)\)\s*$|\s*\[([^\[\]]+)\]\s*$|\s+[-–—]\s*(.+?)[-–—]?\s*$',text)
    if suffix is None:return ''
    label=next(value for value in suffix.groups() if value is not None)
    if not re.search(r'\b(?:ver\.?|version|live|remix|remaster(?:ed)?)\b|\b(?:game|tv)\s+size\b|\d+\s*(?:colou?rs?\b|色)',label,re.I):
        return ''
    base=text[:suffix.start()].strip()
    if len(normalized(base))<3:return ''
    # Compatibility normalization identifies the suffix, but the service's
    # title search is spelling-sensitive. Keep the original prefix in queries.
    original=title.strip()
    for end in range(1,len(original)+1):
        prefix=original[:end].strip()
        if unicodedata.normalize('NFKC',prefix)==base:return prefix
    return base


def _version_annotations(playback,reference,request,records=None,diagnostics=None,
                         *,exact_title=False,details=None):
    title=playback.title if exact_title else _translation_base_title(playback.title)
    if not title or not 100<=sum(len(normalized(row)) for row in reference)<=6000:
        return None
    lookup=SimpleNamespace(title=title,artist=playback.artist,album=getattr(playback,'album',''))
    if records is None:
        records=_records(request(SEARCH_ACTION,{'title':title,'artist':playback.artist,'pageNo':'1'}))
    by_id={_identifier(item.get('lyricID')):item for item in records
           if _identifier(item.get('lyricID')) and _metadata_matches(item,lookup)}
    if not by_id:return None
    if len(by_id)>MAX_TRANSLATION_DETAILS:
        if diagnostics is not None:
            diagnostics.append('알송 번역 상세 조회 한도 초과 · 후보 미검증')
        return None
    candidates=[]
    for identifier in by_id:
        detail=(details[identifier] if details is not None and identifier in details else
                _detail(request(DETAIL_ACTION,{'lyricID':identifier})))
        if not detail or _identifier(detail.get('lyricID'))!=identifier or not _metadata_matches(detail,lookup):
            continue
        item={'strInfoID':identifier,'strTitle':detail.get('title',''),
              'strArtistName':detail.get('artist',''),'strAlbumName':detail.get('album',''),
              'strLyric':detail.get('lyric','')}
        # Parse and verify all original words as usual. Recording duration and
        # clock disagreements cannot invalidate a translation, but no source
        # timestamp is ever returned from this path.
        candidate=_candidate(item,lookup,reference,math.inf)
        if candidate is None:continue
        matched=match_sequence_annotations(reference,candidate['lines'])
        if matched:candidates.append((identifier,matched))
    if not candidates:
        if diagnostics is not None:
            kind='앨범' if exact_title else '버전'
            diagnostics.append(f'알송 다른 {kind} 원문·번역 검증 실패 · 적용 보류')
        return None
    lines=[];conflicts=False
    for index,text in enumerate(reference):
        choices={rows[index].korean.strip() for _,rows in candidates
                 if rows[index] and rows[index].korean.strip()}
        conflicts=conflicts or len(choices)>1
        # Preserve the current reading; this fallback supplies meaning only.
        lines.append(Line(text,'',next(iter(choices)) if len(choices)==1 else ''))
    if sum(bool(line.korean) for line in lines)<2:
        if diagnostics is not None:
            diagnostics.append('알송 번역이 서로 달라 적용 보류' if conflicts else
                               '알송 확인된 번역 구간 부족 · 적용 보류')
        return None
    return {'source':'ALSong','provider_id':min(candidates,key=lambda item:int(item[0]))[0],
            'annotation_only':True,'lines':tuple(lines),'times':[None]*len(lines)}


def fetch_alsong(playback, reference=(), fetch=None, report=None, *, merge_partial=False):
    """Return reference-verified timings, or None; network/XML errors propagate.

    ``fetch(action, params)`` receives search metadata or a selected lyric ID
    and returns SOAP XML. Exact recordings use two searches and three details
    at most. A different album, or an unregistered version's base-title search,
    may supply translations from at most six details after a full local match.
    If the first search has no matching title and artist, retry by title once.
    Broader discovery keeps the original metadata and recording checks.
    ``report`` receives one fixed diagnostic message before a normal return;
    remote text and contributor details are never included in that message.
    """
    def finish(status, result=None):
        if report is not None:report(status)
        return result

    title, artist = _text(getattr(playback, 'title', '')), _text(getattr(playback, 'artist', ''))
    duration = getattr(playback, 'duration', None)
    if (not normalized(title) or not normalized(artist)
            or isinstance(duration, bool) or not isinstance(duration, (int, float))
            or not math.isfinite(duration) or duration <= 0):
        return finish('알송 조회 대기 · 재생 정보 확인 필요')
    reference = _reference(reference)
    if not reference:
        return finish('알송 조회 대기 · 기준 원문 필요')
    request = fetch or _request
    records = _records(request(SEARCH_ACTION, {'title': title, 'artist': artist, 'pageNo': '1'}))
    if not any(_metadata_matches(item, playback) for item in records):
        records += _records(request(SEARCH_ACTION, {'title': title, 'artist': '', 'pageNo': '1'}))
    annotation_diagnostics=[]
    if not records:
        annotations=_version_annotations(playback,reference,request,diagnostics=annotation_diagnostics)
        if annotations:
            count=sum(bool(line.korean) for line in annotations['lines'])
            return finish(f'알송 다른 버전 문장 번역 {count}줄 보충 · 시각 변경 없음',annotations)
        return finish(annotation_diagnostics[-1] if annotation_diagnostics else '알송 검색 결과 없음')
    exact = [item for item in records if _metadata_matches(item, playback)]
    if not exact:
        annotations=_version_annotations(playback,reference,request,records,annotation_diagnostics)
        if annotations:
            count=sum(bool(line.korean) for line in annotations['lines'])
            return finish(f'알송 다른 버전 문장 번역 {count}줄 보충 · 시각 변경 없음',annotations)
    records=exact
    if not records:
        return finish(annotation_diagnostics[-1] if annotation_diagnostics else '알송 곡명·가수 불일치')
    records = [item for item in records if _identifier(item.get('lyricID'))]
    if not records:
        return finish('알송 상세 정보 확인 실패')
    album = normalized(_text(getattr(playback, 'album', '')))
    # Missing album metadata is common here. An explicitly different release
    # must not supply a recording clock, even when the title and words match.
    all_album_records=records
    records = [item for item in records if not album or not _text(item.get('album'))
               or normalized(_text(item.get('album'))) == album]
    if not records:
        annotations=_version_annotations(playback,reference,request,all_album_records,
                                         annotation_diagnostics,exact_title=True)
        if annotations:
            count=sum(bool(line.korean) for line in annotations['lines'])
            return finish(f'알송 다른 앨범 문장 번역 {count}줄 보충 · 시각 변경 없음',annotations)
        return finish(annotation_diagnostics[-1] if annotation_diagnostics else '알송 앨범 불일치')
    same_album = [item for item in records if album and normalized(_text(item.get('album'))) == album]
    records = same_album or records
    by_id = {_identifier(item['lyricID']): item for item in records}
    if len(by_id) > MAX_DETAILS:
        return finish('알송 상세 조회 한도 초과 · 후보 미검증')
    candidates = []
    matched_details = 0
    hangul_by_id = {}
    different_album_details={}
    for identifier in by_id:
        detail = _detail(request(DETAIL_ACTION, {'lyricID': identifier}))
        if not detail or _identifier(detail.get('lyricID')) != identifier or not _metadata_matches(detail, playback):
            continue
        detail_album = normalized(_text(detail.get('album')))
        if album and detail_album and detail_album != album:
            different_album_details[identifier]=detail
            continue
        matched_details += 1
        if _text(detail.get('lyric')):
            hangul_by_id[identifier] = bool(_HANGUL.search(html.unescape(detail['lyric'])))
        playtime = _text(detail.get('playtime'))
        seconds = None
        if playtime:
            if not re.fullmatch(r'\d+(?:\.\d+)?', playtime):
                continue
            seconds = float(playtime)
            if not math.isfinite(seconds) or (seconds > 0 and abs(seconds - duration) > 2):
                continue
        item = {'strInfoID': identifier, 'strTitle': detail.get('title', ''),
                'strArtistName': detail.get('artist', ''), 'strAlbumName': detail.get('album', ''),
                'strLyric': detail.get('lyric', ''), 'duration': seconds or None}
        candidate = _candidate(item, playback, reference, duration, merge_partial)
        if candidate is not None:
            candidates.append(candidate)
    same_album = [candidate for candidate in candidates if album and candidate['_album'] == album]
    candidates = same_album or candidates
    if not candidates:
        if different_album_details:
            annotations=_version_annotations(playback,reference,request,
                [by_id[identifier] for identifier in different_album_details],annotation_diagnostics,
                exact_title=True,details=different_album_details)
            if annotations:
                count=sum(bool(line.korean) for line in annotations['lines'])
                return finish(f'알송 다른 앨범 문장 번역 {count}줄 보충 · 시각 변경 없음',annotations)
        if not matched_details:
            return finish(annotation_diagnostics[-1] if annotation_diagnostics else '알송 상세 정보 확인 실패')
        status = '알송 적용 조건 불일치'
        if len(hangul_by_id) == len(by_id) and not any(hangul_by_id.values()):
            status += ' · 한글 번역 없음'
        return finish(status)
    timed=[candidate for candidate in candidates if not candidate.get('annotation_only')]
    if not timed:
        # All documents verified the same main lyric, but their clocks were not
        # verified against the reference. Merge only uncontested exact-row text.
        lines=[]
        for index,text in enumerate(reference):
            choices={(candidate['lines'][index].reading,candidate['lines'][index].korean)
                     for candidate in candidates if candidate['lines'][index].korean.strip()}
            lines.append(Line(text,*next(iter(choices))) if len(choices)==1 else Line(text,'',''))
        translated=sum(bool(line.korean.strip()) for line in lines)
        if not translated:return finish('알송 번역이 서로 달라 적용 보류')
        selected=min(candidates,key=lambda candidate:int(candidate['provider_id']))
        return finish(f'알송 번역 {translated}줄 보충 · 기존 싱크 유지',
                      {'source':'ALSong','provider_id':selected['provider_id'],'annotation_only':True,
                       'lines':tuple(lines),'times':[None]*len(lines)})
    candidates=timed
    if any(not _clocks_agree(left, right)
           for left, right in itertools.combinations(candidates, 2)):
        return finish('알송 시간표가 서로 달라 적용 보류')
    selected = min(candidates, key=lambda candidate: (
        candidate['_edits'], -sum(time is not None for time in candidate['times']),
        -sum(bool(line.korean.strip()) for line in candidate['lines']),
        int(candidate['provider_id']), candidate['provider_id']))
    supplemented=0
    if any(not line.korean.strip() for line in selected['lines']):
        # The clock has already been selected. An explicitly different album
        # may fill meaning only, aligned to these exact selected source rows.
        other_records=[item for item in all_album_records if
            _identifier(item.get('lyricID')) in different_album_details or
            (album and _text(item.get('album')) and normalized(_text(item.get('album')))!=album)]
        if other_records:
            try:
                annotations=_version_annotations(playback,
                    tuple(line.original for line in selected['lines']),request,other_records,
                    exact_title=True,details=different_album_details)
            except (OSError,ValueError,UnicodeError,HTTPException):
                # An optional supplement must never discard verified timing.
                annotations=None
            if annotations:
                lines=[]
                for line,extra in zip(selected['lines'],annotations['lines']):
                    if not line.korean.strip() and extra.korean.strip():
                        lines.append(Line(line.original,line.reading,extra.korean))
                        supplemented+=1
                    else:
                        lines.append(line)
                selected={**selected,'lines':tuple(lines)}
    translated = sum(bool(line.korean.strip()) for line in selected['lines'])
    if supplemented:
        status = f'알송 싱크 유지 · 다른 앨범 번역 {supplemented}줄 보충'
    elif translated:
        status = f'알송 자료 확인 · 번역 {translated}줄'
    elif hangul_by_id.get(selected['provider_id']):
        status = '알송 싱크 확인 · 번역 형식 확인 필요'
    else:
        status = '알송 싱크 확인 · 한글 번역 없음'
    return finish(status, {key: value for key, value in selected.items() if not key.startswith('_')})
