"""LRC alignment preserves supplied translations; missing timings stay missing."""
from dataclasses import dataclass
from difflib import SequenceMatcher
import json
import math
import re
import unicodedata
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from lyrics import normalized


@dataclass(frozen=True)
class Cue:
    time: float
    text: str


def parse_lrc(text):
    offset_match = re.search(r'\[offset:([+-]?\d+)\]', text, re.I)
    offset = int(offset_match.group(1)) / 1000 if offset_match else 0
    cues = []
    pattern = r'\[(\d+):(\d{2})(?:[.:](\d{1,3}))?\]'
    for line in text.splitlines():
        stamps = list(re.finditer(pattern, line))
        if not stamps:
            continue
        lyric = re.sub(pattern, '', line).strip()
        for stamp in stamps:
            minutes, seconds, fraction = stamp.groups()
            if int(seconds) >= 60:
                continue
            moment = int(minutes) * 60 + int(seconds) + float('0.' + (fraction or '0'))
            cues.append(Cue(max(0.0, moment - offset), lyric))
    return sorted(cues, key=lambda cue: cue.time)


def align_lines(lines, cues):
    # SequenceMatcher handles repeated refrains in sequence, not a text->time dict.
    usable = [cue for cue in cues if normalized(cue.text)]
    originals = [normalized(line.original) for line in lines]
    remote = [normalized(cue.text) for cue in usable]
    matcher = SequenceMatcher(None, originals, remote, autojunk=False)
    times = [None] * len(lines)
    for block in matcher.get_matching_blocks():
        for index in range(block.size):
            times[block.a + index] = usable[block.b + index].time
    # Duplicate/simultaneous LRC cues cannot silently create an invalid timeline.
    previous = -1
    for index, stamp in enumerate(times):
        if stamp is not None:
            if stamp <= previous:
                times[index] = None
            else:
                previous = stamp
    return times


def active_line(times, position, offset=0):
    """Positive offset delays lyrics. None entries never receive invented times."""
    effective = position - offset
    active = None
    for index, stamp in enumerate(times):
        if stamp is not None and stamp <= effective + 1e-7:
            active = index
    return active


def stamp_line(times, index, position, offset=0):
    result = list(times)
    result[index] = round(max(0, position - offset), 3)
    from storage import validate_times
    validate_times(result, len(result))
    return result


def edit_manual_time(timing,index,stamp):
    """A user correction outranks machine anchors, never other user anchors."""
    from timing_quality import timing_origins,automatic_times
    from storage import validate_times
    times=list(timing['times']);origins=timing_origins(timing);automatic=automatic_times(timing)
    if stamp is not None:
        for other,value in enumerate(times):
            if other==index or value is None or origins[other]!='audio':continue
            if (other<index and value>=stamp) or (other>index and value<=stamp):
                times[other]=None;origins[other]=None
    times[index]=stamp;origins[index]='manual' if stamp is not None else None
    validate_times(times,len(times))
    return {**timing,'times':times,'origins':origins,'automatic_times':automatic}


def restore_auto_time(timing,index,candidate=None):
    """Release one manual row without changing the other selected timings."""
    updated=edit_manual_time(timing,index,None)
    value=updated['automatic_times'][index] if candidate is None else round(candidate-timing.get('offset',0),3)
    before=next((x for x in reversed(updated['times'][:index]) if x is not None),-1)
    after=next((x for x in updated['times'][index+1:] if x is not None),math.inf)
    if value is not None and math.isfinite(value) and value>=0 and before<value<after:
        updated['times'][index]=value;updated['origins'][index]='audio'
    return updated


def blend_live_index(timing,effective,position,candidate):
    """Use a corrected row during its interval; keep tracking other rows."""
    from timing_quality import saved_rows
    fixed=saved_rows(timing);scheduled=active_line(effective,position,timing.get('offset',0))
    if scheduled is not None and fixed[scheduled]:
        next_known=scheduled==len(effective)-1 or effective[scheduled+1] is not None
        if candidate is None or candidate<=scheduled or next_known:return scheduled
    if candidate is None:return None
    moment=position-timing.get('offset',0);lower=0;upper=len(effective)-1
    for index,(stamp,protected) in enumerate(zip(timing['times'],fixed)):
        if not protected:continue
        if stamp<=moment:
            lower=index+1 if scheduled is not None and scheduled>index else index
        else:
            upper=index-1;break
    return min(upper,max(lower,candidate)) if upper>=lower else None


def estimate_times(lines, duration, known=None, intro=12.0, outro=8.0):
    """A length-based draft, never an audio alignment. Preserve measured anchors."""
    count = len(lines)
    known = list(known) if known is not None else [None] * count
    from storage import validate_times
    validate_times(known, count)
    if not count or not math.isfinite(duration) or duration <= 0:
        return known
    if not all(math.isfinite(x) and x >= 0 for x in (intro, outro)):
        raise ValueError('전주·후주 시간은 0 이상의 숫자여야 합니다.')
    small_kana = set('ぁぃぅぇぉゃゅょゎァィゥェォャュョヮ')
    weights = []
    for line in lines:
        text = unicodedata.normalize('NFKC', line.reading or line.original)
        units = sum(c.isalnum() and c not in small_kana for c in text)
        weights.append(max(3, units + 2))
    prefix = [0]
    for weight in weights:
        prefix.append(prefix[-1] + weight)
    anchors = [(i,t) for i,t in enumerate(known) if t is not None]
    # Timings for a different cut may exceed the reported duration; do not invent
    # extra timestamps outside that track or discard the user's existing record.
    if anchors and anchors[-1][1] >= duration:
        return known
    start = min(intro, duration * .20)
    end = duration - min(outro, duration * .10)
    if not anchors or anchors[0][0] > 0:
        if anchors:
            start = min(start, anchors[0][1] * .25)
        if not anchors or anchors[0][1] > 0:
            anchors.insert(0, (0, start))
    if anchors and anchors[-1][1] >= end:
        end = (anchors[-1][1] + duration) / 2
    anchors.append((count, end))
    result = list(known)
    for (left, begin), (right, finish) in zip(anchors, anchors[1:]):
        span_weight = prefix[right] - prefix[left]
        for index in range(left, right):
            if result[index] is None:
                result[index] = begin + (finish-begin) * (prefix[index]-prefix[left]) / span_weight
    validate_times(result, count)
    return result


def get_public_timing(song, duration=0, fetch=None):
    """Only title is sent to LRCLIB. No local translation, reading or file upload."""
    title, artist = normalized(song.title), normalized(song.artist)
    if not title or not artist or not song.lines:
        return None
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        return None
    try:
        if not math.isfinite(duration) or duration < 0:
            return None
    except OverflowError:
        return None

    def request(params):
        url = 'https://lrclib.net/api/search?' + urlencode(params)
        with urlopen(Request(url, headers={'User-Agent': 'SpotifyLyricsOverlay/0.1 (personal desktop app)'}),
                     timeout=12) as response:
            body = response.read(4_000_001)
        if len(body) > 4_000_000:
            raise ValueError('공개 가사 응답이 너무 큽니다.')
        return json.loads(body)

    results = (fetch or request)({'track_name': song.title})
    if not isinstance(results, list):
        return None
    candidates = []
    album = normalized(song.album)
    for item in results:
        if not isinstance(item, dict) or item.get('instrumental') is True:
            continue
        if (not isinstance(item.get('trackName'), str)
                or not isinstance(item.get('artistName'), str)
                or normalized(item['trackName']) != title
                or normalized(item['artistName']) != artist):
            continue
        remote_album = item.get('albumName')
        remote_album = normalized(remote_album) if isinstance(remote_album, str) else ''
        if album and remote_album and remote_album != album:
            continue
        text = item.get('syncedLyrics')
        if not isinstance(text, str) or not text or len(text) > 200_000:
            continue
        if isinstance(item.get('duration'), bool):
            continue
        try:
            remote_duration = float(item.get('duration'))
        except (TypeError, ValueError, OverflowError):
            continue
        if (not math.isfinite(remote_duration) or remote_duration <= 0
                or duration and abs(remote_duration - duration) > 4):
            continue
        try:
            cues = parse_lrc(text)
        except (ValueError, OverflowError):
            continue
        limit = min(remote_duration, duration) if duration else remote_duration
        if (len(cues) > 2000
                or any(not math.isfinite(cue.time) or cue.time > limit + 2 for cue in cues)):
            continue
        times = align_lines(song.lines, cues)
        matched = sum(t is not None for t in times)
        if matched / len(times) < .80:
            continue
        candidates.append({'times': times, 'matched': matched, 'source': 'LRCLIB',
                           'offset': 0.0, 'lrclib_id': item.get('id'),
                           '_album': remote_album})
    exact = [item for item in candidates if album and item['_album'] == album]
    choices = exact or candidates
    if not choices:
        return None
    # Equal lyrics and duration can still belong to different solo recordings.
    # Prefer complete candidates only when every overlapping onset agrees.
    for index in range(len(song.lines)):
        if len({item['times'][index] for item in choices if item['times'][index] is not None}) > 1:
            return None
    best = max(choices, key=lambda item: item['matched'])
    return {key: value for key, value in best.items() if key != '_album'}
