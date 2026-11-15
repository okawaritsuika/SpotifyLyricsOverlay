"""Fetch original lyrics and verified recording timings without a Spotify window.

Only playback metadata is sent to LRCLIB. Results stay in memory; this module
does not read or save local lyrics, translations, credentials, or audio.
"""
import hashlib
import json
import math
import re
import unicodedata
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from lyrics import Line, is_cinderella_no1, normalized
from lyric_match import align_phrases
from lyric_merge import has_ordered_overlap
from sync import parse_lrc


MAX_RESPONSE_BYTES = 4_000_000
MAX_LYRIC_CHARACTERS = 200_000
MAX_LYRIC_LINES = 2_000
DURATION_TOLERANCE = 2.0


def _request(params):
    request = Request('https://lrclib.net/api/search?' + urlencode(params),
                      headers={'User-Agent': 'SpotifyLyricsOverlay/0.1 (personal desktop app)'})
    with urlopen(request, timeout=8) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError('공개 가사 응답이 너무 큽니다.')
    return json.loads(body)


def _duration(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _text(value):
    return value.strip() if isinstance(value, str) else ''


def _artist_credit(value):
    text=unicodedata.normalize('NFKC',value).strip()
    match=re.fullmatch(r'(.*?)\s*\(\s*CV\s*[.:]?\s*([^()]+)\)',text,re.I)
    if match and normalized(match[1]) and normalized(match[2]):
        return normalized(match[1]),normalized(match[2])
    return normalized(text),''


def _same_artist(left,right):
    left_name,left_cv=_artist_credit(left)
    right_name,right_cv=_artist_credit(right)
    return bool(left_name and left_name==right_name
                and (not left_cv or not right_cv or left_cv==right_cv))


def reviewed_original_conflict(title, artist, album, duration, lines):
    """Exclude known wrong originals by recording and full lyric content."""
    title_key = normalized(title)
    if is_cinderella_no1(title) and title_key != normalized('シンデレラNo.1'):
        # The 25 legacy solo entries copied the 105-line group document.
        rejected = '869dffe3a845b527deca416f7cd8a43658e141aaa8cdc0809263136451079ac0'
    elif (title_key == normalized('Thank You!')
          and normalized(album) == normalized('THE IDOLM@STER LIVE THE@TER PERFORMANCE 13')
          and _same_artist(artist, '春日未来 (CV.山崎はるか)')
          and _duration(duration) is not None and abs(float(duration)-285.706) <= DURATION_TOLERANCE):
        # Evidence: playlist-review-20261004-0907/wrong-original-evidence.json.
        rejected = '93d6f03d428652c8296d7e52568446fc7f95ea2c3063c2841b9e17ade2df1cd2'
    else:
        return False
    digest = hashlib.sha256(normalized(''.join(row.original for row in lines)).encode('utf-8')).hexdigest()
    return digest == rejected


def _lyrics(item, duration, merge_partial=False):
    synced = _text(item.get('syncedLyrics'))
    plain = _text(item.get('plainLyrics'))
    if len(synced) > MAX_LYRIC_CHARACTERS or len(plain) > MAX_LYRIC_CHARACTERS:
        return None
    rows = tuple(Line(row.strip(), '', '') for row in plain.splitlines() if row.strip())
    if synced:
        cues = [cue for cue in parse_lrc(synced) if cue.text.strip()]
        # Simultaneous lyrics cannot form the strictly increasing timeline
        # expected by the overlay. Keep plain text if the provider has it.
        times = [cue.time for cue in cues]
        if (cues and len(cues) <= MAX_LYRIC_LINES
                and all(math.isfinite(stamp) and 0 <= stamp <= duration for stamp in times)
                and all(left < right for left, right in zip(times, times[1:]))):
            if not plain or normalized(plain) == normalized(''.join(cue.text for cue in cues)):
                return tuple(Line(cue.text.strip(), '', '') for cue in cues), times
            if merge_partial and 0 < len(rows) <= MAX_LYRIC_LINES and len(cues) >= 2:
                left = tuple(row.original for row in rows)
                right = tuple(cue.text for cue in cues)
                if has_ordered_overlap(left, right):
                    blocks = align_phrases(left, right)
                    if sum(block.right_end - block.right for block in blocks) == len(cues):
                        mapped = [None] * len(rows)
                        for block in blocks:
                            mapped[block.left] = times[block.right]
                        return rows, mapped
    if not rows or len(rows) > MAX_LYRIC_LINES:
        return None
    return rows, [None] * len(rows)


def _candidates(results,title,artist,album,duration,merge_partial=False):
    candidates = []
    for item in results:
        if not isinstance(item, dict) or item.get('instrumental') is True:
            continue
        if (normalized(_text(item.get('trackName'))) != normalized(title)
                or not _same_artist(_text(item.get('artistName')),artist)):
            continue
        remote_album = normalized(_text(item.get('albumName')))
        if album and remote_album and remote_album != album:
            continue
        remote_duration = _duration(item.get('duration'))
        if remote_duration is None or abs(remote_duration - duration) > DURATION_TOLERANCE:
            continue
        lyrics = _lyrics(item, duration, merge_partial)
        if lyrics is not None:
            lines, times = lyrics
            if reviewed_original_conflict(title,artist,album,duration,lines):continue
            candidates.append({'source': 'LRCLIB', 'lines': lines, 'times': times,
                               'provider_id': item.get('id'),
                               '_album': remote_album, '_duration': remote_duration})
    return candidates


def fetch_public_lyrics(playback, fetch=None, report=None, *, merge_metadata=False):
    """Return an unambiguous metadata match, or None; transport errors propagate.

    Full title matching retains every recording/version marker. Duration is
    required even for plain lyrics, so a different cut is not silently selected.
    A title-only retry may find artist credits indexed with or without a CV
    suffix; all returned candidates still pass the same recording checks.
    Search results are bounded by the provider and never prove global absence.
    The optional fetch receives query parameters; report receives a status text.
    """
    def finish(status,result=None):
        if report is not None:report(status)
        return result
    title, artist = _text(playback.title), _text(playback.artist)
    duration = _duration(playback.duration)
    if not normalized(title) or not normalized(artist) or duration is None:
        return finish('LRCLIB 조회에 필요한 곡 정보 부족')
    request=fetch or _request
    results=request({'track_name': title, 'artist_name': artist})
    if not isinstance(results,list):
        return finish('LRCLIB 응답 형식 확인 실패')
    album=normalized(_text(playback.album))
    candidates=_candidates(results,title,artist,album,duration,merge_metadata)
    preferred=[candidate for candidate in candidates if album and candidate['_album']==album] or candidates
    if not any(any(stamp is not None for stamp in candidate['times']) for candidate in preferred):
        fallback=request({'track_name':title})
        if not isinstance(fallback,list):
            return finish('LRCLIB 응답 형식 확인 실패')
        results=results+fallback
        candidates.extend(_candidates(fallback,title,artist,album,duration,merge_metadata))
    same_album = [candidate for candidate in candidates if album and candidate['_album'] == album]
    if same_album:
        candidates = same_album
    if not candidates:
        return finish('LRCLIB 후보 검증 불일치' if results else 'LRCLIB 검색 결과 없음')
    # Identical reuploads are harmless. Differing lyrics or differing synced
    # clocks are ambiguous, even if one record happens to be returned first.
    originals = {tuple(line.original for line in candidate['lines']) for candidate in candidates}
    if len(originals) != 1:
        return finish('LRCLIB 후보 가사 충돌')
    synced = [candidate for candidate in candidates if any(t is not None for t in candidate['times'])]
    if len({tuple(candidate['times']) for candidate in synced}) > 1:
        return finish('LRCLIB 후보 시각 충돌')
    selected = (synced or candidates)[0]
    result = {key: value for key, value in selected.items() if not key.startswith('_')}
    if merge_metadata:
        # Title and the complete artist credit passed the provider-specific
        # checks above. Album and duration come from the actual result.
        result.update(title=title, artist=artist, album=selected['_album'], duration=selected['_duration'])
    return finish('LRCLIB 싱크 연결됨' if synced else 'LRCLIB 원문만 연결됨', result)
