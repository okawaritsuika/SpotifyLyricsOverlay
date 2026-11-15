"""Read documented LyricsPlus v2 timings, checked against trusted local originals.

Only playback metadata leaves this process. This module neither caches responses
nor sends reference lyrics, credentials, audio, or submissions. A failed primary
request may use the project's published backup once, with the same metadata.
"""
import itertools
import json
import math
import re
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from lyrics import Line, normalized
from lyric_match import match_lyrics
from lyric_merge import has_ordered_overlap


ENDPOINT = 'https://lyricsplus.prjktla.my.id/v2/lyrics/get'
# Published by the same project's YouLyPlus client in background/constants.js.
BACKUP_ENDPOINT = 'https://lyricsplus.binimum.org/v2/lyrics/get'
MAX_RESPONSE_BYTES = 4_000_000
MAX_LYRIC_CHARACTERS = 200_000
MAX_LYRIC_LINES = 1_500
MAX_LINE_CHARACTERS = 4_000
DURATION_TOLERANCE = 2.0
REQUEST_TIMEOUT = 8


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        return None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('LyricsPlus 응답에 중복 JSON 키가 있습니다.')
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError('LyricsPlus 응답의 숫자가 올바르지 않습니다.')


def _request_once(params, endpoint):
    request = Request(endpoint + '?' + urlencode(params), headers={
        'Accept': 'application/json',
        'User-Agent': 'SpotifyLyricsOverlay/0.1 (personal desktop app)',
    })
    with build_opener(_NoRedirect()).open(request, timeout=REQUEST_TIMEOUT) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError('LyricsPlus 응답이 너무 큽니다.')
    return json.loads(body, object_pairs_hook=_unique_object,
                      parse_constant=_invalid_constant)


def _request(params):
    try:
        return _request_once(params, ENDPOINT)
    except HTTPError as error:
        if not 500 <= error.code < 600:
            raise
    except URLError as error:
        if not isinstance(error.reason, (TimeoutError, socket.gaierror)):
            raise
    except (TimeoutError, socket.gaierror):
        pass
    # One backup attempt only. Its error propagates with the final HTTP status
    # or transport cause; malformed responses and access errors never fall back.
    return _request_once(params, BACKUP_ENDPOINT)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except (OverflowError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _text(value):
    return value.strip() if isinstance(value, str) else ''


def parse_ttml_duration(value):
    """Return explicit TTML clock/offset seconds; bare numbers have no unit here."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    offset = re.fullmatch(r'(\d+(?:\.\d+)?)(ms|s|m|h)', value)
    if offset:
        result = float(offset[1]) * {'ms': .001, 's': 1, 'm': 60, 'h': 3600}[offset[2]]
    else:
        clock = re.fullmatch(r'(?:(\d+):)?(\d+):([0-5]\d(?:\.\d+)?)', value)
        if not clock:
            return None
        try:
            if clock[1] is not None and int(clock[2]) >= 60:
                return None
            result = int(clock[1] or 0) * 3600 + int(clock[2]) * 60 + float(clock[3])
        except (ValueError, OverflowError):
            return None
    return result if math.isfinite(result) and result >= 0 else None


def _reference_key(reference):
    if isinstance(reference, (str, bytes)):
        return ''
    try:
        rows = list(itertools.islice(iter(reference), MAX_LYRIC_LINES + 1))
    except TypeError:
        return ''
    if not rows or len(rows) > MAX_LYRIC_LINES:
        return ''
    originals = []
    size = 0
    for row in rows:
        text = row.original if isinstance(row, Line) else row
        if not isinstance(text, str):
            return ''
        size += len(text)
        if size > MAX_LYRIC_CHARACTERS:
            return ''
        originals.append(text)
    return normalized(''.join(originals))


def _metadata_matches(metadata, playback, duration):
    if not isinstance(metadata, dict):
        return False
    for name in ('title', 'artist', 'album'):
        remote = metadata.get(name)
        if remote is None or remote == '':
            continue
        if not isinstance(remote, str):
            return False
        local = _text(getattr(playback, name, ''))
        if remote.strip() and local and normalized(remote) != normalized(local):
            return False
    total = metadata.get('totalDuration')
    if total is not None and total != '':
        total = parse_ttml_duration(total)
        if total is None or total > duration + DURATION_TOLERANCE:
            return False
    # totalDuration comes from TTML body@dur: a shorter lyric document does not
    # prove a shorter recording. It is a bound, never recording identity evidence.
    # processingTime.selectedSongMetadata can echo the query; never inspect it.
    return True


def _line_text(row):
    text = row.get('text')
    if text is not None and not isinstance(text, str):
        return None
    if _text(text):
        return text.strip()
    syllables = row.get('syllabus')
    if syllables is None:
        return ''
    if not isinstance(syllables, list) or len(syllables) > MAX_LINE_CHARACTERS:
        return None
    pieces = []
    size = 0
    for syllable in syllables:
        if not isinstance(syllable, dict) or not isinstance(syllable.get('text'), str):
            return None
        text = syllable['text']
        size += len(text)
        if size > MAX_LINE_CHARACTERS:
            return None
        pieces.append(text)
    return ''.join(pieces).strip()


def parse_payload(payload, playback, reference=(), *, merge_partial=False):
    """Return checked line timings, or None; references must be trusted originals.

    Reference strings (or Line objects) are compared in full, allowing bounded
    spelling differences while preserving the ordered song. References are required:
    LyricsPlus does not guarantee independently verified recording metadata.
    """
    if isinstance(reference, (str, bytes)):
        return None
    try:
        reference = tuple(itertools.islice(iter(reference), MAX_LYRIC_LINES + 1))
    except TypeError:
        return None
    reference_key = _reference_key(reference)
    duration = _number(getattr(playback, 'duration', None))
    if (not reference_key or not duration
            or not normalized(_text(getattr(playback, 'title', '')))
            or not normalized(_text(getattr(playback, 'artist', '')))
            or not isinstance(payload, dict) or payload.get('error')):
        return None
    sync_type = payload.get('type')
    if not isinstance(sync_type, str) or sync_type.casefold() not in ('word', 'line', 'syllable'):
        return None
    if not _metadata_matches(payload.get('metadata', {}), playback, duration):
        return None
    rows = payload.get('lyrics')
    if not isinstance(rows, list) or not rows or len(rows) > MAX_LYRIC_LINES:
        return None
    groups, times = [], []
    previous = -1.0
    size = 0
    limit = duration + DURATION_TOLERANCE
    for row in rows:
        if not isinstance(row, dict):
            return None
        timestamp = _number(row.get('time'))
        if timestamp is None:
            return None
        timestamp /= 1000
        if timestamp < previous or timestamp > limit:
            return None
        previous = timestamp
        if 'duration' in row:
            span = _number(row['duration'])
            if span is None or timestamp + span / 1000 > limit:
                return None
        text = _line_text(row)
        if text is None or len(text) > MAX_LINE_CHARACTERS:
            return None
        size += len(text)
        if size > MAX_LYRIC_CHARACTERS:
            return None
        if not text or text in ('♪', '♫', '♬'):
            continue
        if times and times[-1] == timestamp:
            # Multiple voices may share one onset. Keep each distinct original
            # at that onset and collapse exact duplicate rows, without new times.
            if text not in groups[-1]:
                groups[-1].append(text)
        else:
            groups.append([text])
            times.append(timestamp)
    originals = [' '.join(group) for group in groups]
    unmerged = [text for group in groups for text in group]
    reference_texts = [row.original if isinstance(row, Line) else row for row in reference]
    if not originals or any(len(text) > MAX_LINE_CHARACTERS for text in originals):
        return None
    reference_texts = [text for text in reference_texts if normalized(text)]
    # A whole document at 0 ms can match the words while providing no line sync.
    if times == [0.0] and len(reference_texts) > 1:
        return None
    if (match_lyrics(unmerged, reference_texts) is None and
            not (merge_partial and has_ordered_overlap(reference_texts, originals))):
        return None
    metadata = payload.get('metadata', {})
    identity = {field: _text(metadata.get(field)) for field in ('title', 'artist', 'album')} if merge_partial else {}
    return {'source': 'LyricsPlus', 'provider_id': None,
            'lines': tuple(Line(text, '', '') for text in originals), 'times': times, **identity}


def fetch_lyricsplus(playback, reference=(), fetch=None, report=None, *, merge_partial=False):
    """Fetch checked timings; 404 is a miss and other final errors propagate.

    The injectable fetch receives only the metadata parameter dictionary and
    returns decoded JSON. Missing trusted originals cause no network request.
    Default transport tries the published backup once only after a primary DNS
    error, timeout, or HTTP 5xx. Injected transports are called exactly once.
    """
    def finish(status, result=None):
        if report is not None:
            report(status)
        return result

    title = _text(getattr(playback, 'title', ''))
    artist = _text(getattr(playback, 'artist', ''))
    album = _text(getattr(playback, 'album', ''))
    duration = _number(getattr(playback, 'duration', None))
    if not normalized(title) or not normalized(artist) or not duration:
        return finish('LyricsPlus 조회 대기 · 재생 정보 확인 필요')
    # Materialize bounded references once so a generator is not consumed twice.
    try:
        if isinstance(reference, (str, bytes)):
            return finish('LyricsPlus 조회 대기 · 기준 원문 필요')
        reference = tuple(itertools.islice(iter(reference), MAX_LYRIC_LINES + 1))
    except TypeError:
        return finish('LyricsPlus 조회 대기 · 기준 원문 필요')
    if not _reference_key(reference):
        return finish('LyricsPlus 조회 대기 · 기준 원문 필요')
    params = {'title': title, 'artist': artist, 'album': album, 'duration': duration}
    try:
        payload = (fetch or _request)(params)
    except HTTPError as error:
        if error.code == 404:
            return finish('LyricsPlus 서버 검색 결과 없음 · 다른 출처는 미확인')
        raise
    if not isinstance(payload, dict) or payload.get('error'):
        raise ValueError('LyricsPlus 서버 응답 확인 실패')
    sync_type = payload.get('type')
    if isinstance(sync_type, str) and sync_type.casefold() in ('none', 'unsynced', 'plain'):
        return finish('LyricsPlus 응답에 줄별 시각 없음')
    if not isinstance(sync_type, str) or sync_type.casefold() not in ('line', 'word', 'syllable'):
        return finish('LyricsPlus 응답 형식 확인 실패 · 싱크 미검증')
    if not _metadata_matches(payload.get('metadata', {}), playback, duration):
        return finish('LyricsPlus 후보 곡 정보 불일치 · 적용 보류')
    result = parse_payload(payload, playback, reference, merge_partial=merge_partial)
    return finish('LyricsPlus 싱크 연결됨' if result else
                  'LyricsPlus 후보 원문·시각 검증 불일치 · 적용 보류', result)
