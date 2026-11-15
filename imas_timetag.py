"""Read a reviewed recording from the TimeTag database and its public lyrics."""
import hashlib
import math
import re
from urllib.request import Request, urlopen

from lyric_match import phrase_key
from lyrics import Line, normalized


# The public TimeTag index identifies the same release, cast and 278.200s
# runtime. The ID below is the matching Spotify recording of COCX-39035 #15.
_TRACK_ID = '5kY52r3bPRhCTTyUF2fkam'
_TITLE = 'ススメ☆オトメ 〜jewel parade〜'
_ARTIST = 'CINDERELLA PROJECT'
_ALBUM = 'THE IDOLM@STER CINDERELLA GIRLS ANIMATION PROJECT 00 ST@RTER BEST'
_DURATION = 278.2
_TAGS_URL = 'https://timetag.main.jp/herodb/herodb.cgi?table=timetag&MODE=Get&KEY=15100212435800'
_LYRICS_URL = 'https://utaten.com/lyric/to17020702/'
_ROW_COUNT = 37
_ORIGINALS_HASH = '1eb8aae340a6499ba82ebc565aa6b67e1b3af07b3e69cc441e79274bbb6fdf1d'
_TAG = re.compile(r'\[(\d{2}):([0-5]\d):(\d{2})\](\d{0,3})', re.ASCII)


def supports(title, artist, album, duration, track_id):
    return (track_id == _TRACK_ID and type(duration) in (int, float)
            and math.isfinite(duration) and abs(duration - _DURATION) <= .01
            and all(isinstance(value, str) and normalized(value) == normalized(expected)
                    for value, expected in ((title, _TITLE), (artist, _ARTIST), (album, _ALBUM))))


def validate_url(address):
    if address not in (_TAGS_URL, _LYRICS_URL):
        raise ValueError('Unrecognized TimeTag source URL')
    return address


def fetch_text(address):
    validate_url(address)
    limit = 250_000
    request = Request(address, headers={'User-Agent': 'SpotifyLyricsOverlay/1.0 (personal lyrics reader)'})
    with urlopen(request, timeout=12) as response:
        if response.url != address:
            raise ValueError('Unexpected TimeTag source redirect')
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError('TimeTag source is too large')
    return data.decode('ascii' if address == _TAGS_URL else 'utf-8')


def _originals(html):
    from bs4 import BeautifulSoup

    document = BeautifulSoup(html, 'html.parser')
    body = document.select_one('.lyricBody .hiragana')
    if body is None:
        return ()
    for note in body.select('.rt, rt, rp, script, style'):
        note.decompose()
    for boundary in body.find_all('br'):
        boundary.replace_with('\n')
    rows = tuple(line.strip() for line in body.get_text().splitlines() if line.strip())
    digest = hashlib.sha256('\n'.join(map(phrase_key, rows)).encode('utf-8')).hexdigest()
    return rows if len(rows) == _ROW_COUNT and digest == _ORIGINALS_HASH else ()


def _onsets(text, rows, duration):
    header = re.fullmatch(r'success\r?\nTIMETAG=(.*?)\s*\[EOM\]\s*', text, re.S)
    if not header:
        return []
    tagged = tuple(line for line in header[1].splitlines() if line.strip())
    if len(tagged) != len(rows):
        return []
    previous_end = -1
    onsets = []
    for raw, original in zip(tagged, rows):
        tags = list(_TAG.finditer(raw))
        if len(tags) < 2 or ''.join(tag[0] for tag in tags) != raw:
            return []
        if not all(tag[4] and int(tag[4]) for tag in tags[:-1]) or tags[-1][4]:
            return []
        # The repeated chorus has one extra display space in the published text.
        size = sum(int(tag[4]) for tag in tags[:-1])
        if size not in (len(original), len(re.sub(r' +', ' ', original))):
            return []
        ticks = [6000 * int(tag[1]) + 100 * int(tag[2]) + int(tag[3]) for tag in tags]
        if ticks[0] < previous_end or not ticks[0] < ticks[-1] <= duration * 100 or any(
                left > right for left, right in zip(ticks, ticks[1:])):
            return []
        previous_end = ticks[-1]
        onsets.append(ticks[0] / 100)
    return onsets


def find_candidate(title, artist, album, duration, *, track_id, fetch=None):
    if not supports(title, artist, album, duration, track_id):
        return None
    fetch = fetch or fetch_text
    rows = _originals(fetch(_LYRICS_URL))
    if not rows:
        return None
    times = _onsets(fetch(_TAGS_URL), rows, duration)
    if not times:
        return None
    return {'source': 'imas_timetag', 'url': _TAGS_URL, 'lyrics_url': _LYRICS_URL,
            'lines': tuple(Line(row, '', '') for row in rows), 'times': times}
