"""Reuse local lyric documents without confusing a different edit's clock."""
import re
import unicodedata
import hashlib

from lyrics import Song, is_cinderella_no1, normalized
from recording_context import known_duration


_EDIT = re.compile(r'(?:\s*[\[(]\s*|\s+[-–—]\s*)(game|tv|short)\s*'
                   r'(?:size|ver\.?|version)\s*[\])]?\s*$', re.I)
_ARRANGEMENT = re.compile(r'\b(?:live|remix|acoustic|instrumental|karaoke|slowed|sped)\b'
                          r'|ライブ|ライヴ|カラオケ|オフボーカル', re.I)


def title_identity(title):
    title = unicodedata.normalize('NFKC', title).strip()
    from reader_bridge import _trailing_group
    kind = '';voice = []
    for _ in range(8):
        edit = _EDIT.search(title)
        if edit:
            kind = edit[1].lower();title = title[:edit.start()].strip();continue
        group = _trailing_group(title)
        if not group:
            suffix = re.match(r'^(.*?)\s+[-–—]\s*(.+)$', title)
            group = (suffix[1], suffix[2]) if suffix else None
        if not group or not group[0] or _ARRANGEMENT.search(group[1]):break
        version = re.fullmatch(r'(.*?)\s*(?:ver(?:sion)?\.?|バージョン|버전)', group[1], re.I)
        solo = re.fullmatch(r'solo|ソロ', group[1], re.I)
        if not version and not solo:break
        voice.append(normalized(version[1] if version else group[1]))
        title = group[0].strip()
    return normalized(title), kind or ('vocal:' + ':'.join(voice) if voice else '')


def linked_edit(title, artist):
    """Recognize credited solo remixes separately from the full recording."""
    if is_cinderella_no1(title):return None
    from reader_bridge import _trailing_group
    from saved_annotations import _artist_identity

    title = unicodedata.normalize('NFKC', title).strip()
    solo = re.search(r'\s+[-–—]\s+(.+)$', title)
    group = _trailing_group(title)
    credit = group[1] if group else solo[1] if solo else ''
    singer = re.fullmatch(r'(.+?)\s*ソロ[・･]?\s*リミックス', credit)
    if singer:
        performer = _artist_identity(artist)
        if not performer or _artist_identity(singer[1]) != performer:return None
        base = group[0] if group else title[:solo.start()]
        return normalized(base), 'solo-remix'
    if _arrangement(title):return None
    base, edit = title_identity(title)
    return base, edit if edit in ('game', 'tv', 'short') else 'full'


def _work_evidence(source, playback):
    from work_reuse import same_work
    return same_work(source, playback)


def _arrangement(title):
    # LIVE can be part of the composition's name (for example DRIVE A LIVE).
    suffix = re.search(r'(?:[\[(]|\s[-–—]\s)(.+)$', unicodedata.normalize('NFKC', title))
    return bool(suffix and _ARRANGEMENT.search(suffix[1]))


def compatibility(source, playback):
    """Text permits a 30 s length difference; a borrowed clock permits 2 s."""
    if is_cinderella_no1(playback.title) or is_cinderella_no1(source['title']):return None
    saved_duration = known_duration(source.get('duration'))
    duration = known_duration(playback.duration)
    if saved_duration is None or duration is None:return None
    title, edit = title_identity(source['title'])
    requested, target_edit = title_identity(playback.title)
    if not title:return None
    if title != requested:
        from work_reuse import arrangement_text_matches
        if not arrangement_text_matches(source, playback):return None
        # Concert arrangements can have a much longer intro or a different
        # tempo. Only the verified work's words are reusable, never its clock
        # (including a tracking hint), even when the lengths happen to agree.
        return {'duration_difference': abs(saved_duration-duration),
                'timing_compatible': False, 'tracking_compatible': False,
                'same_artist': False, 'work_evidence': True}
    artist = normalized(source['artist'])
    requested_artist = normalized(playback.artist)
    if artist and artist != requested_artist:
        from artist_names import catalog
        artist = normalized(catalog().translate(source['artist'], idolmaster=True))
        requested_artist = normalized(catalog().translate(playback.artist, idolmaster=True))
    if not artist or not requested_artist:return None
    same_artist = artist == requested_artist
    vocal_change = edit != target_edit and (edit.startswith('vocal:') or target_edit.startswith('vocal:'))
    evidence = _work_evidence(source, playback) if not same_artist or vocal_change else None
    if (not same_artist or vocal_change) and not evidence:return None
    # Equal lengths cannot identify two concert performances or arrangements.
    if (_arrangement(source['title']) or _arrangement(playback.title)) and (
            normalized(source['album']) != normalized(playback.album)):
        return None
    difference = abs(saved_duration - duration)
    if difference > 30:return None
    same_cut = ((edit if edit in ('game', 'tv', 'short') else 'full')
                == (target_edit if target_edit in ('game', 'tv', 'short') else 'full'))
    return {'duration_difference': difference,
            'timing_compatible': same_artist and not vocal_change and difference <= 2
                and (edit == target_edit or not edit or not target_edit),
            'tracking_compatible': difference <= 2 and same_cut,
            'same_artist': same_artist, 'work_evidence': evidence}


def _originals(lines):
    return tuple(normalized(line.original) for line in lines)


def _provider(candidate, lines):
    value = candidate.get('direct')
    if not value:return None
    from background_lyrics import mapped_times
    times = mapped_times(value, lines)
    if not any(stamp is not None for stamp in times):return None
    # This recording has not been confirmed by Spotify. Keep future provider
    # lookups enabled and never borrow the other recording's Spotify ID.
    return {'source': '저장 캐시', 'lines': lines, 'times': times, 'provider_id': None}


def _saved_timing(candidate):
    timing = candidate.get('timing')
    if not timing:return None
    from timing_quality import saved_rows, timing_origins
    keep = saved_rows(timing)
    if not any(keep):return None
    result = {**timing, 'times': [stamp if accepted else None for stamp, accepted in zip(timing['times'], keep)],
              'origins': [origin if accepted else None for origin, accepted in zip(timing_origins(timing), keep)]}
    if result.get('automatic_times') is not None:
        result['automatic_times'] = [stamp if accepted else None
                                     for stamp, accepted in zip(result['automatic_times'], keep)]
    return result


def load_saved_timing(store, key, lines):
    title, artist, album = key.split('\0')
    timings = []
    for prefix in ('memory-', 'completed-'):
        song = Song(prefix + hashlib.sha256(key.encode()).hexdigest()[:24], title, artist, album, lines)
        try:timing = store.load(song)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):continue
        timing = _saved_timing({'timing': timing})
        if timing:timings.append(timing)
    if len(timings) == 2:
        first, second = timings
        shared = [(a + first.get('offset', 0), b + second.get('offset', 0))
                  for a, b in zip(first['times'], second['times']) if a is not None and b is not None]
        if not shared or any(abs(a - b) > .75 for a, b in shared):return None
    return max(timings, key=lambda value: sum(stamp is not None for stamp in value['times']), default=None)


def related_quality(value):
    """Prefer useful clocks over a text-only hit from another local store."""
    count = len(value['lines'])
    direct = value.get('direct')
    saved = _saved_timing(value)
    clock = saved['times'] if saved else direct['times'] if direct else []
    reference = value.get('tracking_times') or []
    return (sum(stamp is not None for stamp in clock)/max(1, count),
            sum(stamp is not None for stamp in reference)/max(1, count),
            sum(bool(line.korean) for line in value['lines'])/max(1, count))


def related_reference(lines, candidates):
    """Map agreeing clocks to the current text without claiming ownership."""
    from background_lyrics import mapped_times
    clocks = []
    original = ''.join(_originals(lines))
    for candidate in candidates:
        if ''.join(_originals(candidate['lines'])) != original:continue
        if candidate.get('reuse', {}).get('tracking_conflict'):return None
        reference = candidate.get('tracking_times')
        if not reference:continue
        mapped = mapped_times({'source': '저장 캐시', 'lines': candidate['lines'], 'times': reference}, lines)
        if any(stamp is not None for stamp in mapped):clocks.append(mapped)
    for index, first in enumerate(clocks):
        for second in clocks[index+1:]:
            shared = [(a, b) for a, b in zip(first, second) if a is not None and b is not None]
            if not shared or any(abs(a-b)>.75 for a, b in shared):return None
    return max(clocks, key=lambda value: sum(stamp is not None for stamp in value), default=None)


def _anniversary_clocks(candidates, playback):
    from types import SimpleNamespace
    from work_reuse import anniversary_work

    work = anniversary_work(playback) if candidates else ''
    if not work:return []
    clocks = []
    for candidate in candidates:
        compatible = candidate['compatibility']
        if (not compatible.get('work_evidence') or compatible['duration_difference'] > 2
                or candidate.get('borrowed') or anniversary_work(SimpleNamespace(**candidate)) != work):
            continue
        direct = candidate.get('direct')
        saved = _saved_timing(candidate)
        if direct and direct['source'] == 'Spotify':
            lines, times, priority = direct['lines'], direct['times'], 2
        elif saved:
            lines, priority = candidate['lines'], 1
            times = [None if stamp is None else stamp + saved.get('offset', 0) for stamp in saved['times']]
        elif direct and direct['source'] != '저장 캐시':
            lines, times, priority = direct['lines'], direct['times'], 0
        else:
            continue
        if (sum(stamp is not None for stamp in times) < .8 * len(lines)
                or any(stamp is not None and not 0 <= stamp < playback.duration for stamp in times)):
            continue
        rank = (priority, -compatible['duration_difference'], candidate['key'])
        clocks.append((rank, lines, times, candidate))
    return clocks


def anniversary_reference(lines, clocks):
    """Use one nearby recording whose lyric onsets can be mapped in order."""
    from lyric_match import align_phrases, match_lyrics, phrase_key

    target = [line.original for line in lines]
    best, coverage = None, 0
    for _, source, times, _ in sorted(clocks, key=lambda clock: clock[0], reverse=True):
        original = [line.original for line in source]
        match = match_lyrics(original, target)
        if match:
            onsets = {}
            for start, stamp in zip(match.source_offsets, times):
                if stamp is not None:onsets.setdefault(match.source_to_target[start], stamp)
            mapped = [onsets.get(start) for start in match.target_offsets]
        else:
            blocks = align_phrases(original, target)
            shared = sum(len(phrase_key(row)) for block in blocks for row in target[block.right:block.right_end])
            size = max(sum(len(phrase_key(row)) for row in original), sum(len(phrase_key(row)) for row in target))
            if not size or shared < .85 * size:
                continue
            mapped = [None] * len(lines)
            for block in blocks:mapped[block.right] = times[block.left]
        matched = sum(stamp is not None for stamp in mapped)
        if matched == len(lines):return mapped
        minimum = .6 if match else .85
        if matched > coverage and matched >= max(min(2, len(lines)), minimum * len(lines)):
            best, coverage = mapped, matched
    return best


def select_related(candidates, playback):
    clocks = _anniversary_clocks(candidates, playback)
    selected = _select_related(candidates, playback)
    if clocks and selected is None:
        # When provider documents differ, a verified anniversary recording
        # can supply a starting document for a version with no local text.
        for clock in sorted(clocks, key=lambda clock: clock[0], reverse=True):
            candidate = clock[3]
            if anniversary_reference(candidate['lines'], [clock]) is not None:
                selected = _select_related([candidate], playback)
                break
    if selected and clocks:selected['anniversary_clocks'] = clocks
    return selected


def _select_related(candidates, playback):
    """Fail closed on text or timing conflicts; never scale saved timestamps."""
    if not candidates:return None
    originals={''.join(_originals(candidate['lines'])) for candidate in candidates}
    text_variant=len(originals)>1
    if text_variant:
        # Known performances can spell the same work differently. A uniquely
        # closest recording length can choose its document, but cannot identify
        # a borrowed clock or break a tie between divergent lyric documents.
        if not all(candidate['compatibility'].get('work_evidence')
                   and not candidate['compatibility'].get('same_artist',True)
                   for candidate in candidates):return None
        def cut(title):
            edit=title_identity(title)[1]
            return edit if edit in ('game','tv','short') else 'full'
        if any(cut(candidate['title'])!=cut(playback.title) for candidate in candidates):return None
        nearest=min(candidate['compatibility']['duration_difference'] for candidate in candidates)
        if nearest>2:return None
        candidates=[candidate for candidate in candidates
                    if abs(candidate['compatibility']['duration_difference']-nearest)<.01]
        if len({''.join(_originals(candidate['lines'])) for candidate in candidates})!=1:return None
    def score(candidate):
        timing = _saved_timing(candidate)
        provider = candidate.get('direct')
        count = sum(stamp is not None for stamp in timing['times']) if timing else (
            sum(stamp is not None for stamp in provider['times']) if provider else 0)
        return (candidate['compatibility']['timing_compatible'], bool(timing), count,
                sum(bool(line.reading) + bool(line.korean) for line in candidate['lines']),
                candidate.get('saved_at', 0))
    selected = max(candidates, key=lambda candidate:(
        sum(bool(line.korean) for line in candidate['lines'])/len(candidate['lines']),score(candidate))) if text_variant else max(candidates,key=score)
    lines = selected['lines']
    from background_lyrics import mapped_times
    from lyric_annotations import merge_verified_annotations
    lines, _ = merge_verified_annotations(lines, [candidate['lines'] for candidate in candidates if candidate is not selected])
    clocks = [];tracking_clocks = []
    direct = timing = None
    for candidate in candidates:
        compatible = candidate['compatibility']['timing_compatible']
        tracking = candidate['compatibility'].get('tracking_compatible', compatible)
        supplied = _provider(candidate, lines) if compatible or tracking else None
        saved = _saved_timing(candidate) if compatible or tracking else None
        if supplied and any(stamp is not None and not 0 <= stamp <= playback.duration + 2 for stamp in supplied['times']):
            supplied = None
        timing_clock = ([stamp + saved.get('offset', 0) if stamp is not None else None for stamp in saved['times']]
                        if saved else None)
        if timing_clock and any(stamp is not None and not 0 <= stamp <= playback.duration + 2 for stamp in timing_clock):
            saved = timing_clock = None
        if timing_clock:
            timing_clock = mapped_times({'source': '저장 캐시', 'lines': candidate['lines'],
                                         'times': timing_clock}, lines)
            if not any(stamp is not None for stamp in timing_clock):timing_clock = None
        clock = timing_clock or (supplied['times'] if supplied else None)
        if clock and any(stamp is not None for stamp in clock):
            if compatible:clocks.append(clock)
            if tracking:tracking_clocks.append(clock)
        if candidate is selected and compatible:direct, timing = supplied, saved
    # Multiple releases with conflicting clocks can still share the text, but
    # neither clock has enough evidence to become the requested recording's.
    def conflicting(values):
        for i, first in enumerate(values):
            for second in values[i + 1:]:
                shared = [(a, b) for a, b in zip(first, second) if a is not None and b is not None]
                if not shared or any(abs(a - b) > .75 for a, b in shared):return True
        return False
    conflict = conflicting(clocks)
    tracking_conflict = conflicting(tracking_clocks)
    if conflict or text_variant:direct = timing = None
    tracking_times = (max(tracking_clocks, key=lambda values: sum(stamp is not None for stamp in values))
                      if tracking_clocks and not tracking_conflict and not text_variant else None)
    return {'lines': lines, 'direct': direct, 'timing': timing, 'tracking_times': tracking_times,
            'saved_at': selected.get('saved_at', 0),
            'reuse': {'source_key': selected['key'], 'source_duration': selected['duration'],
                      'duration_difference': selected['compatibility']['duration_difference'],
                      'timing_compatible': selected['compatibility']['timing_compatible'] and not conflict and not text_variant,
                      'timing_conflict': conflict, 'tracking_conflict': tracking_conflict or text_variant,
                      'same_artist': selected['compatibility'].get('same_artist', True),
                      'work_evidence': selected['compatibility'].get('work_evidence')}}
