"""Compose recording-scoped lyric snapshots without changing provider documents."""
from bisect import bisect_left
from dataclasses import asdict
from functools import lru_cache
import math
import re
from statistics import median

from lyric_annotations import match_verified_annotations
from lyric_match import align_phrases, match_lyrics, phrase_key
from lyrics import Line, is_cinderella_no1, normalized
from storage import validate_times


MAX_SNAPSHOTS = 24
MAX_ROWS = 2000
MAX_TEXT = 4000
_KINDS = {'unknown', 'singing', 'dialogue'}
_PRIORITY = {'manual': 0, 'spotify': 1, 'local': 2, 'lrclib': 3, 'alsong': 4,
             'lyricsplus': 5, 'audio': 8}
_DIALOGUE = re.compile(r'^\s*(?:[^「『\n]{1,30})?[「『][^」』]+[」』][！!？?。…\s]*$')
_SPEAKER_QUOTE = re.compile(r'^\s*[^:：「『\n]{1,30}[:：]\s*[「『]')
_INLINE_QUOTE = re.compile(r'(?:(?<=\s)[^\s:：「『]{1,30}[:：]\s*)?[「『][^」』]+[」』][！!？?。…\s]*$')
_STATUSES = {'manual', 'verified', 'matched', 'audio_aligned', 'rejected', 'unresolved'}


def make_snapshot(source, lines, times=None, *, provider_id=None, recording_verified=False,
                  title='', artist='', album='', duration=None, track_id=None, kinds=(),
                  annotation_only=False):
    lines = tuple(lines)
    if not isinstance(source, str) or not source.strip() or len(source) > 80:
        raise ValueError('Invalid lyric source')
    if not 0 < len(lines) <= MAX_ROWS or any(not isinstance(line, Line) for line in lines):
        raise ValueError('Invalid source lyric rows')
    if any(not isinstance(field, str) or len(field) > MAX_TEXT for line in lines
           for field in (line.original, line.reading, line.korean)):
        raise ValueError('Invalid source lyric text')
    if any(not line.original.strip() for line in lines):
        raise ValueError('Empty source lyric row')
    if any(not isinstance(field, str) or len(field) > MAX_TEXT for field in (title, artist, album)):
        raise ValueError('Invalid recording metadata')
    if type(provider_id) not in (int, str, type(None)) or len(str(provider_id)) > 256:
        raise ValueError('Invalid lyric source ID')
    if track_id is not None and (not isinstance(track_id, str) or len(track_id) > 256):
        raise ValueError('Invalid Spotify track ID')
    if duration is not None and (type(duration) not in (int, float) or not 0 < duration < math.inf):
        raise ValueError('Invalid recording length')
    if type(recording_verified) is not bool or type(annotation_only) is not bool:
        raise ValueError('Invalid source verification')
    times = [None] * len(lines) if times is None else list(times)
    validate_times(times, len(lines))
    kinds = tuple(kinds) or ('unknown',) * len(lines)
    if len(kinds) != len(lines) or any(kind not in _KINDS for kind in kinds):
        raise ValueError('Invalid lyric row kind')
    return {'source': source, 'lines': lines, 'times': tuple(times), 'kinds': kinds,
            'provider_id': provider_id, 'recording_verified': recording_verified,
            'title': title, 'artist': artist, 'album': album, 'duration': duration,
            'track_id': track_id, 'annotation_only': annotation_only}


def snapshot_from_direct(value, *, recording_verified=True):
    value = value.get('merge_candidate', value)
    source = value['source']
    track_id = value.get('track_id')
    if track_id is None and source.casefold() == 'spotify' and isinstance(value.get('provider_id'), str):
        track_id = value['provider_id']
    return make_snapshot(source, value['lines'], value['times'], provider_id=value.get('provider_id'),
                         recording_verified=value.get('recording_verified', recording_verified),
                         title=value.get('title', ''), artist=value.get('artist', ''),
                         album=value.get('album', ''), duration=value.get('duration'), track_id=track_id,
                         kinds=value.get('kinds', ()), annotation_only=value.get('annotation_only', False))


def encode_snapshots(snapshots):
    if len(snapshots) > MAX_SNAPSHOTS:
        raise ValueError('Too many lyric sources')
    return [{**snapshot, 'lines': [asdict(line) for line in snapshot['lines']]} for snapshot in snapshots]


def decode_snapshots(value):
    if not isinstance(value, (list, tuple)) or len(value) > MAX_SNAPSHOTS:
        raise ValueError('Invalid lyric sources')
    result = []
    for snapshot in value:
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get('lines'), list):
            raise ValueError('Invalid lyric source')
        if len(snapshot['lines']) > MAX_ROWS:
            raise ValueError('Too many source lyric rows')
        result.append(make_snapshot(**{**snapshot, 'lines': tuple(Line(**line) for line in snapshot['lines'])}))
    return tuple(result)


def encode_merge(merge):
    return {'snapshots': encode_snapshots(merge['snapshots']), 'rows': merge['rows']}


def decode_merge(value, line_count):
    if not isinstance(value, dict) or not isinstance(value.get('rows'), list):
        raise ValueError('Invalid lyric merge')
    snapshots = decode_snapshots(value['snapshots'])
    rows = value['rows']
    if len(rows) != line_count or any(not isinstance(row, dict) for row in rows):
        raise ValueError('Invalid merged lyric rows')
    validate_times([row['selected_time'] for row in rows], line_count)
    sources = {snapshot['source'] for snapshot in snapshots}
    for row in rows:
        if (row['kind'] not in _KINDS or row['original_source'] not in sources or
                row['status'] not in _STATUSES or type(row['manual']) is not bool or
                row['selected_source'] not in sources | {None} or
                (row['selected_source'] is None) != (row['selected_time'] is None) or
                not isinstance(row['timings'], list) or len(row['timings']) > MAX_SNAPSHOTS or
                not isinstance(row['source_rows'], list) or len(row['source_rows']) > MAX_SNAPSHOTS):
            raise ValueError('Invalid merged lyric provenance')
        for timing in row['timings']:
            if (timing['source'] not in sources or timing['status'] not in _STATUSES or
                    any(type(timing[field]) not in (float, int) or not math.isfinite(timing[field])
                        for field in ('raw_seconds', 'seconds'))):
                raise ValueError('Invalid timing candidate')
        if row['selected_source'] is not None and not any(
                (timing['source'], timing['seconds'], timing['status']) ==
                (row['selected_source'], row['selected_time'], row['status']) for timing in row['timings']):
            raise ValueError('Missing selected timing candidate')
    return {'snapshots': snapshots, 'rows': rows}


def _rank(snapshot):
    name = snapshot['source'].casefold()
    return (_PRIORITY.get(name, 6), not snapshot['recording_verified'],
            -sum(stamp is not None for stamp in snapshot['times']), name,
            str(snapshot['provider_id']), repr(snapshot['lines']), repr(snapshot['times']),
            repr(snapshot['kinds']), snapshot['title'], snapshot['artist'], snapshot['album'],
            snapshot['duration'] or 0, snapshot['track_id'] or '', snapshot['annotation_only'])


def _compatible(playback, snapshot, track_id):
    if track_id and snapshot['track_id'] and track_id != snapshot['track_id']:
        return False
    for field in ('title', 'artist', 'album'):
        known = normalized(getattr(playback, field, '') or '')
        remote = normalized(snapshot[field])
        if known and remote and known != remote:
            return False
    duration = getattr(playback, 'duration', 0)
    return not (duration and snapshot['duration'] and abs(duration - snapshot['duration']) > 2)


def _strong_recording(playback, snapshot, track_id):
    return snapshot['recording_verified'] and (
        bool(track_id and track_id == snapshot['track_id']) or
        all(normalized(getattr(playback, field, '') or '') and
            normalized(getattr(playback, field)) == normalized(snapshot[field])
            for field in ('title', 'artist', 'album')) and
        bool(getattr(playback, 'duration', 0) and snapshot['duration']))


def _text(lines):
    return tuple(line.original for line in lines)


@lru_cache(maxsize=96)
def _compare(left, right):
    blocks = align_phrases(left, right)
    left_keys, right_keys = tuple(map(phrase_key, left)), tuple(map(phrase_key, right))
    left_size, right_size = sum(map(len, left_keys)), sum(map(len, right_keys))
    common = sum(len(key) for block in blocks for key in left_keys[block.left:block.left_end])
    whole = common == left_size == right_size and bool(common)
    # A complete, short reference may have fewer characters than a single
    # spoken addition. Its row order still provides useful context.
    short_expansion = common == left_size and len(right) <= len(left) + max(3, len(left) // 3)
    enough = (len({left_keys[block.left] for block in blocks}) >= 2 and
              common >= .72 * left_size and (common >= .60 * right_size or short_expansion))
    return blocks, whole, enough


def has_ordered_overlap(left, right):
    """Screen partial provider responses before comparing recording clocks."""
    _, whole, enough = _compare(tuple(left), tuple(right))
    return whole or enough


def _clock(reference, supplied, blocks):
    pairs = [(reference[block.left], supplied[block.right]) for block in blocks
             if reference[block.left] is not None and supplied[block.right] is not None]
    errors = [left - right for left, right in pairs]
    correction = 0
    if (len(pairs) >= 3 and pairs[-1][0] - pairs[0][0] >= 12 and
            max(errors) - min(errors) <= .6 and .45 <= abs(median(errors)) <= 8):
        correction = round(median(errors), 3)
    return correction, len(pairs), all(abs(error - correction) <= 2 for error in errors)


def _bounds(anchors, index, duration):
    left = bisect_left(anchors, (index, -1))
    right = left + (left < len(anchors) and anchors[left][0] == index)
    return (anchors[left - 1][1] if left else -1,
            anchors[right][1] if right < len(anchors) else duration or math.inf)


def _gaps(blocks, left_count, right_count):
    left_end = right_end = 0
    for block in blocks:
        if block.left == left_end and block.right > right_end:
            yield left_end, right_end, block.right
        left_end, right_end = block.left_end, block.right_end
    if left_end == left_count and right_end < right_count:
        yield left_end, right_end, right_count


def _kind(snapshot, index):
    kind = snapshot['kinds'][index]
    original = snapshot['lines'][index].original
    if kind == 'unknown' and _SPEAKER_QUOTE.match(original):
        return 'dialogue'
    return kind


def _working_snapshot(snapshot, reference_keys):
    """Expose an inline quote only when the text before it is a known row."""
    lines, times, kinds, source_indices = [], [], [], []
    for index, (line, stamp, kind) in enumerate(zip(snapshot['lines'], snapshot['times'], snapshot['kinds'])):
        quote = _INLINE_QUOTE.search(line.original) if reference_keys else None
        if quote and phrase_key(line.original[:quote.start()]) in reference_keys:
            before, dialogue = line.original[:quote.start()].rstrip(), quote.group().rstrip()
            lines.extend((Line(before, '', line.korean), Line(dialogue, '', line.korean)))
            times.extend((stamp, None))
            kinds.extend((kind, 'dialogue' if _SPEAKER_QUOTE.match(dialogue) else 'unknown'))
            source_indices.extend((index, index))
        else:
            lines.append(line)
            times.append(stamp)
            kinds.append(kind)
            source_indices.append(index)
    return {**snapshot, 'lines': tuple(lines), 'times': tuple(times), 'kinds': tuple(kinds),
            'source_indices': source_indices}


def _reference_clock(base, snapshots, compatible):
    name = base['source'].casefold()
    if name != 'manual':
        return base['times'] if base['recording_verified'] else (None,) * len(base['lines'])
    # A manual correction may deliberately depart from a provider's clock.
    # Compare provider clocks against Spotify and use the user's timestamps
    # separately as hard boundaries when selecting the final time.
    spotify = next((snapshot for number, snapshot in enumerate(snapshots) if compatible[number]
                    and snapshot['recording_verified'] and snapshot['source'].casefold() == 'spotify'), None)
    clock = [None] * len(base['lines'])
    if spotify:
        for block in align_phrases(_text(base['lines']), _text(spotify['lines'])):
            clock[block.left] = spotify['times'][block.right]
    return clock


def _insertions(playback, snapshots, base, clock, compatible, track_id):
    originals = _text(base['lines'])
    anchors = [(index, stamp) for index, stamp in enumerate(clock) if stamp is not None]
    duration = getattr(playback, 'duration', 0)
    candidates = {}
    for number, snapshot in enumerate(snapshots):
        if snapshot is base or snapshot['annotation_only']:
            continue
        other = _text(snapshot['lines'])
        blocks, _, enough = _compare(originals, other)
        if not enough:
            continue
        shift, overlap, agrees = _clock(clock, snapshot['times'], blocks)
        strong = _strong_recording(playback, snapshot, track_id)
        for gap, start, end in _gaps(blocks, len(originals), len(other)):
            keys = tuple(map(phrase_key, other[start:end]))
            if not all(keys):
                continue
            # A known recording can supply a missing untimed phrase. With a
            # weaker identity, require observed timings for every inserted row
            # and two corroborated onsets on the reference clock.
            low, high = _bounds(anchors, gap - .5, duration)
            times = snapshot['times'][start:end]
            proven = (compatible[number] and snapshot['recording_verified'] and agrees and
                      snapshot['source'].casefold() not in ('local', 'manual', 'audio') and
                      (strong or overlap >= 2 and all(stamp is not None for stamp in times)) and
                      all(stamp is None or low < stamp + shift < high for stamp in times))
            variants = candidates.setdefault(gap, {})
            variants.setdefault(keys, []).append((number, start, end, proven))
    result = {}
    for gap, variants in candidates.items():
        proven = [choices for choices in variants.values() if any(choice[3] for choice in choices)]
        if len(proven) != 1:
            continue
        # Prefer a wiki's explicitly labeled dialogue text once independently
        # confirmed at this exact position in the recording.
        choices = [choice for choice in proven[0] if compatible[choice[0]]]
        number, start, end, _ = min(choices, key=lambda choice: (
            not any(_DIALOGUE.fullmatch(line.original) for line in
                    snapshots[choice[0]]['lines'][choice[1]:choice[2]]),
            snapshots[choice[0]]['source'].casefold() not in ('namu', 'sidem', '나무위키', 'sidem ko 위키'),
            choice[0]))
        result[gap] = [(number, index) for index in range(start, end)]
    return result


def _add_annotations(supplied, block, number, readings, translations, allow_reading):
    start, end = block.left, block.left_end
    rows = supplied[block.right:block.right_end]
    if end - start == 1 and allow_reading and all(row.reading.strip() for row in rows):
        readings[start].append((number, ' '.join(row.reading for row in rows)))
    if all(row.korean.strip() for row in rows):
        meanings = []
        for row in rows:
            if not meanings or meanings[-1] != row.korean:
                meanings.append(row.korean)
        for index in range(start, end):
            translations[index].append((number, ' / '.join(meanings)))


def _choose_annotation(choices, owner, snapshots):
    if not choices:
        return '', None
    protected = [(number, value) for number, value in choices
                 if snapshots[number]['source'].casefold() == 'manual']
    if protected:
        return min(protected)[::-1]
    primary = next((value for number, value in reversed(choices) if number == owner), None)
    if primary is not None:
        return primary, owner
    local = [(number, value) for number, value in choices if snapshots[number]['source'].casefold() == 'local']
    if local:
        return min(local)[::-1]
    if len({value.strip() for _, value in choices}) == 1:
        return min(choices)[::-1]
    return '', None


def restore_cached_translations(lines, rows, snapshots):
    """Recover blank translations from exact rows of the recording's prior cache."""
    cached = {snapshot['provider_id']: snapshot['lines'] for snapshot in snapshots
              if snapshot['source'] == 'cache'}
    if not cached:
        return lines, rows
    restored, provenance = list(lines), list(rows)
    for index, (line, row) in enumerate(zip(lines, rows)):
        if line.korean.strip():
            continue
        meanings = set()
        for origin in row['source_rows']:
            if not isinstance(origin, dict) or origin.get('source') != 'cache':
                continue
            provider_id, start = origin.get('provider_id'), origin.get('start')
            supplied = cached.get(provider_id, ()) if type(provider_id) in (str, int, type(None)) else ()
            if type(start) is not int or not 0 <= start < len(supplied) or origin.get('end') != start + 1:
                continue
            source = supplied[start]
            if source.original == line.original and source.korean.strip():
                meanings.add(source.korean)
        if len(meanings) == 1:
            restored[index] = Line(line.original, line.reading, meanings.pop())
            provenance[index] = {**row, 'translation_source': 'cache'}
    return tuple(restored), provenance


def _select_times(candidates, duration):
    selected = [None] * len(candidates)
    for statuses in (('manual',), ('verified',), ('matched', 'audio_aligned')):
        anchors = [(i, value['seconds']) for i, value in enumerate(selected) if value]
        proposed = []
        for index, choices in enumerate(candidates):
            if selected[index]:
                continue
            low, high = _bounds(anchors, index, 0 if statuses == ('manual',) else duration)
            viable = [value for value in choices if value['status'] in statuses and
                      low < value['seconds'] < high]
            if not viable:
                continue
            native = [value for value in viable if value['status'] != 'audio_aligned']
            viable = native or viable
            if len(statuses) > 1 and max(v['seconds'] for v in viable) - min(v['seconds'] for v in viable) > 1.5:
                support = [len({other['source'].casefold() for other in viable
                                if abs(value['seconds'] - other['seconds']) <= .75}) for value in viable]
                majority = [value for value, count in zip(viable, support) if count == max(support) and count >= 2]
                if not majority or max(v['seconds'] for v in majority) - min(v['seconds'] for v in majority) > .75:
                    continue
                viable = majority
            proposed.append((index, viable[0]))
        # A candidate must be after every previous proposal and before every
        # later one. Keep all unaffected rows when a gap contains a conflict.
        after = [math.inf] * len(proposed)
        earliest = math.inf
        for position in range(len(proposed) - 1, -1, -1):
            after[position] = earliest
            earliest = min(earliest, proposed[position][1]['seconds'])
        latest = -1
        for (index, value), following in zip(proposed, after):
            stamp = value['seconds']
            if latest < stamp < following:
                selected[index] = value
            latest = max(latest, stamp)
    return selected


def merge_lyrics(playback, snapshots, *, track_id=None):
    """Merge stable source snapshots. `matched` is corroborated, not measured."""
    if is_cinderella_no1(playback.title):
        from public_lyrics import reviewed_original_conflict
        snapshots = (snapshot for snapshot in snapshots if not reviewed_original_conflict(
            playback.title, playback.artist, playback.album, playback.duration, snapshot['lines']))
    snapshots = tuple(sorted(snapshots, key=_rank)[:MAX_SNAPSHOTS])
    if track_id is None:
        spotify_ids = {item['track_id'] for item in snapshots
                       if item['source'].casefold() == 'spotify' and item['track_id']}
        if len(spotify_ids) == 1:
            track_id = next(iter(spotify_ids))
    compatible = [_compatible(playback, snapshot, track_id) for snapshot in snapshots]
    base_index = next((i for i, snapshot in enumerate(snapshots) if compatible[i] and
                       not snapshot['annotation_only'] and snapshot['source'].casefold() != 'audio'), None)
    if base_index is None:
        return {'lines': (), 'times': [], 'rows': [], 'snapshots': snapshots, 'direct': None}
    reference_keys = {phrase_key(line.original) for line in snapshots[base_index]['lines']} - {''}
    working = tuple(_working_snapshot(snapshot, reference_keys if number != base_index and
                    snapshot['source'].casefold() not in ('manual', 'local', 'audio', 'spotify') else ())
                    for number, snapshot in enumerate(snapshots))
    base = working[base_index]
    duration = getattr(playback, 'duration', 0)
    reference_clock = _reference_clock(base, working, compatible)
    additions = _insertions(playback, working, base, reference_clock, compatible, track_id)
    owners, old_rows = [], []
    for i in range(len(base['lines']) + 1):
        owners.extend(additions.get(i, ()))
        if i < len(base['lines']):
            old_rows.append(len(owners))
            owners.append((base_index, i))
    target = tuple(working[number]['lines'][index] for number, index in owners)
    originals = _text(target)
    base_clock = [None] * len(target)
    for index, stamp in zip(old_rows, reference_clock):
        base_clock[index] = stamp
    readings, translations, candidates, origins = ([[] for _ in target] for _ in range(4))
    kinds = [_kind(working[number], index) for number, index in owners]
    for number, snapshot in enumerate(working):
        supplied = snapshot['lines']
        source = snapshot['source']
        name = source.casefold()
        stamps = snapshot['times']
        blocks, whole, enough = _compare(originals, _text(supplied))
        # Bounded typo matching is retained for existing fully verified clocks.
        precise_onsets = {block.right: block.left for block in blocks}
        if not whole and (match := match_lyrics(_text(supplied), originals)) is not None:
            whole = True
            starts = {start: i for i, start in enumerate(match.target_offsets)}
            precise_onsets.update({i: starts[match.source_to_target[start]]
                                   for i, start in enumerate(match.source_offsets)
                                   if match.source_to_target[start] in starts})
        if number == base_index:
            precise_onsets = {i: index for i, index in enumerate(old_rows)}
        shift, overlap, agrees = _clock(base_clock, stamps, blocks) if number != base_index else (0, 0, True)
        verified = compatible[number] and not snapshot['annotation_only'] and (
            snapshot['recording_verified'] or name == 'manual')
        use_clock = verified and agrees and (number == base_index or whole or
                    enough and (overlap >= 2 or _strong_recording(playback, snapshot, track_id)))
        if len(stamps) == 1 and stamps[0] == 0 and len(target) > 1:
            use_clock = False
        for i, index in sorted(precise_onsets.items()):
            if stamps[i] is None:
                continue
            status = ('manual' if name == 'manual' else 'audio_aligned' if name == 'audio' else
                      'verified' if number == base_index or name == 'spotify' else 'matched')
            candidates[index].append({'source': source, 'provider_id': snapshot['provider_id'],
                'index': snapshot['source_indices'][i], 'raw_seconds': stamps[i], 'seconds': round(stamps[i] + shift, 6),
                'status': status if use_clock else 'rejected'})
        # Cross-version candidates can still supply meaning for whole matches.
        if number == base_index or compatible[number] and (whole or enough) or whole and name not in ('manual', 'local'):
            for block in blocks:
                _add_annotations(supplied, block, number, readings, translations, compatible[number])
                for index in range(block.left, block.left_end):
                    origins[index].append({'source': source, 'provider_id': snapshot['provider_id'],
                                           'start': snapshot['source_indices'][block.right],
                                           'end': snapshot['source_indices'][block.right_end - 1] + 1})
                if (block.left_end == block.left + 1 and block.right_end == block.right + 1 and
                        kinds[block.left] == 'unknown' and compatible[number]):
                    kinds[block.left] = _kind(snapshot, block.right)
        if not whole and (compatible[number] or name not in ('manual', 'local')) and any(line.korean for line in supplied):
            legacy = match_verified_annotations(target, supplied)
            if legacy:
                for index, line in enumerate(legacy):
                    if line and line.korean:
                        translations[index].append((number, line.korean))
    selected = _select_times(candidates, duration + .001 if duration else 0)
    lines, rows = [], []
    inserted = set(range(len(target))).difference(old_rows)
    for index, ((owner, position), line, choice) in enumerate(zip(owners, target, selected)):
        if line.reading.strip():
            readings[index].append((owner, line.reading))
        if line.korean.strip():
            translations[index].append((owner, line.korean))
        reading, reader = _choose_annotation(readings[index], owner, snapshots)
        korean, translator = _choose_annotation(translations[index], owner, snapshots)
        if not reading and index in inserted and re.search(r'[ぁ-ヺ一-龯]', line.original):
            from web_lyrics import reading as generate_reading
            try:
                reading = generate_reading(line.original)
            except (ImportError, RuntimeError, ValueError, OSError):
                pass
        lines.append(Line(line.original, reading, korean))
        rows.append({'kind': kinds[index], 'original_source': snapshots[owner]['source'],
                     'original_provider_id': snapshots[owner]['provider_id'],
                     'original_index': working[owner]['source_indices'][position],
                     'reading_source': snapshots[reader]['source'] if reader is not None else 'generated' if reading else None,
                     'translation_source': snapshots[translator]['source'] if translator is not None else None,
                     'source_rows': origins[index], 'timings': candidates[index],
                     'selected_source': choice['source'] if choice else None,
                     'selected_time': choice['seconds'] if choice else None,
                     'status': choice['status'] if choice else 'unresolved',
                     'confidence': ('high' if choice['status'] in ('manual', 'verified') else 'medium') if choice else None,
                     'manual': any(snapshots[number]['source'].casefold() == 'manual'
                                   for number in (owner, reader, translator) if number is not None) or
                               bool(choice and choice['status'] == 'manual')})
    lines, rows = restore_cached_translations(tuple(lines), rows, snapshots)
    times = [row['selected_time'] for row in rows]
    direct = None
    if any(stamp is not None for stamp in times):
        native = next((snapshot for snapshot in snapshots if _text(snapshot['lines']) == originals and
                       snapshot['times'] == tuple(times) and all(choice is None or
                       (choice['source'], choice['provider_id']) == (snapshot['source'], snapshot['provider_id'])
                       for choice in selected)), None)
        direct = {'source': native['source'] if native else 'Merged',
                  'provider_id': native['provider_id'] if native else None,
                  'lines': native['lines'] if native else lines, 'times': times}
    return {'lines': lines, 'times': times, 'rows': rows, 'snapshots': snapshots, 'direct': direct}
