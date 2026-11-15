"""Discover analyzed recordings and run review-only alignment against retained audio."""
from hashlib import sha256
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import time

from audio_capture_cache import SAMPLE_RATE, INFO_NAME, capture_path, load_capture_samples, read_capture
from japanese_alignment import ALIGNMENT_VERSION, CTC, HYBRID, LEGACY, release_ctc
from live_timing import LiveTimingStore, original_signature
from local_analysis import alignment_recipe, audio_digest, automatic_alignment_engine, input_file, lyric_digest
from lyrics import Song
from recording_context import known_duration, recording_key
from storage import Store, read_json, validate_times
from timing_quality import timing_origins


BATCH_CTC_PROFILE = 'recovery'
PREPROCESSING_VERSION = 1
ANCHOR_RECOVERY_VERSION = 1
_READ_ERRORS = (OSError, ValueError, KeyError, TypeError, AttributeError)


def _before(alignment, count, seconds=None):
    times = alignment.get('times')
    try:
        validate_times(times, count)
    except ValueError:
        times = [None] * count
    details = alignment.get('line_alignment') or alignment.get('details')
    if not isinstance(details, list) or len(details) != count or any(not isinstance(row, dict) for row in details):
        details = [{} for _ in times]
    return {'times': times, 'details': details, 'seconds': seconds,
            'engine': alignment.get('engine') or alignment.get('model') or alignment.get('method') or '',
            'version': alignment.get('alignment_version'), 'profile': alignment.get('romaji_profile')}


def _status(item, *, auto, manual):
    before = item['before']
    times = before['times']
    engine = automatic_alignment_engine(item['lines'])
    item.update(engine=before['engine'], timed=sum(value is not None for value in times), total=len(times),
                manual=manual, auto=auto,
                incomplete=any(value is None or detail.get('quality') == 'low'
                               for value, detail in zip(times, before['details'])),
                outdated=auto and (before['engine'] != engine or before['version'] != ALIGNMENT_VERSION
                                   or engine == CTC and before['profile'] != BATCH_CTC_PROFILE))
    return item


def _has_auto(timing):
    times = timing.get('times')
    automatic = timing.get('automatic_times')
    return isinstance(times, list) and ('audio' in timing_origins(timing) or
                                       isinstance(automatic, list) and any(stamp is not None for stamp in automatic))


def _audio(path, kind='local'):
    if path:
        try:
            path = input_file(path)
            if path.stat().st_size:
                return {'path': str(path), 'audio_kind': kind, 'eligible': True, 'reason': ''}
        except _READ_ERRORS:
            pass
    reason = '연결된 음원 파일이 없거나 읽을 수 없습니다.' if path else '저장된 음원이 없어 새 녹음 또는 로컬 음원이 필요합니다.'
    return {'path': str(path or ''), 'audio_kind': 'missing', 'eligible': False, 'reason': reason}


def _jobs(directory, tracks):
    path = directory / 'local_analysis.sqlite3'
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("""
            SELECT jobs.id, path, sha256, spec, status, updated,
                   json_extract(result, '$.alignment') AS alignment,
                   json_extract(result, '$.seconds.alignment') AS seconds,
                   json_extract(result, '$.duration') AS duration,
                   json_extract(result, '$.playlist_clock') AS playlist_clock
            FROM jobs
            WHERE json_extract(spec, '$.kind') IN ('lyrics', 'group')
              AND (status IN ('done', 'failed') OR json_extract(result, '$.alignment') IS NOT NULL)
            ORDER BY EXISTS(SELECT 1 FROM playback_links WHERE playback_links.id=jobs.id) DESC,
                     status='done' DESC, coalesce(json_extract(spec, '$.analysis_only'), 0) ASC,
                     json_extract(spec, '$.kind')='group' DESC, updated DESC
        """).fetchall()
    items = {}
    paths = {}
    for row in rows:
        spec = json.loads(row['spec'])
        lines = spec.get('lines')
        if not lines:
            continue
        track_id = spec.get('track_id') or ''
        recording = spec.get('recording') or ''
        readings = spec.get('line_readings') or []
        identity = (track_id, recording, row['sha256'], lyric_digest(lines), lyric_digest(readings))
        if identity in items and items[identity]['eligible']:
            continue
        if row['path'] not in paths:
            paths[row['path']] = _audio(row['path'])
        audio = paths[row['path']]
        if identity in items and not audio['eligible']:
            continue
        alignment = json.loads(row['alignment'] or '{}')
        before = _before(alignment, len(lines), row['seconds'])
        supplied = alignment.get('engine') == 'supplied' or not alignment and spec.get('times') is not None
        if supplied and not alignment:
            before = _before({'times': spec['times'], 'engine': 'supplied'}, len(lines))
        protected = [supplied and value is not None for value in before['times']]
        track = spec.get('playlist_recording') or {}
        metadata = tracks.get(track_id) or {}
        clock = json.loads(row['playlist_clock'] or 'null')
        parts = recording.split('\0')
        title, artist, album = parts if len(parts) == 3 else ('', '', '')
        job_id = row['id']
        item = {'id': 'job:' + job_id, 'kind': 'job', 'target': job_id,
                'identifier': track_id or recording.replace('\0', ' · ') or row['sha256'][:16],
                'track_id': track_id, 'recording': recording,
                'title': spec.get('title') or track.get('title') or title or metadata.get('title') or Path(row['path']).stem,
                'artist': spec.get('artist') or ', '.join(track.get('artists') or []) or artist or
                          ', '.join(metadata.get('artists') or []),
                'album': spec.get('album') or track.get('album') or album or metadata.get('album', ''),
                'updated': row['updated'], 'lines': lines, 'line_readings': readings,
                'before': before, 'protected': protected, 'sha256': row['sha256'],
                'duration': spec.get('expected_duration') or row['duration'],
                'playlist_recording': track, 'playlist_clock': clock,
                'apply_blocked': bool(clock and not clock.get('usable')), **audio}
        items[identity] = _status(item, auto=not supplied, manual=any(protected))
    return list(items.values())


def _capture(directory, recording, duration):
    if not isinstance(recording, str) or re.fullmatch(r'[a-f0-9]{64}', recording) is None:
        return _audio(None), None
    root = directory / 'audio_captures' / recording
    candidates = []
    for info_path in root.glob('*/' + INFO_NAME):
        try:
            hint = read_json(info_path, {})
            measured = known_duration(hint.get('duration'))
            if measured is None or duration is not None and abs(measured - duration) > 2:
                continue
            info = read_capture(info_path.parent, duration or measured)
            audio = _audio(capture_path(info_path.parent), 'capture')
            if info and audio['eligible']:
                candidates.append((audio, info))
        except _READ_ERRORS:
            continue
    if len(candidates) == 1:
        return candidates[0]
    if duration is not None and candidates:
        return min(candidates, key=lambda pair: abs(pair[1]['duration'] - duration))
    return _audio(None), None


def _song_item(directory, song, timing, draft, clock, updated, local, *, live=False, track_id='', duration=None):
    signature = original_signature(song) if live else song.fingerprint
    if draft.get('fingerprint') != signature:
        draft = {}
    lines = [line.original for line in song.lines]
    recording = draft.get('recording_key') or (recording_key(*clock.split('\0')) if clock else
                                               recording_key(song.title, song.artist, song.album))
    duration = known_duration(draft.get('duration') or timing.get('analysis_duration') or duration)
    audio, capture = _capture(directory, recording, duration)
    if not capture and not draft and not _has_auto(timing):
        return None
    if not audio['eligible']:
        file_links = local.for_song(song.id) if local else []
        choices = [file for file in file_links if file['exists'] and (duration is None or
                   file['duration'] is not None and abs(file['duration'] - duration) <= 2)]
        if len(choices) == 1:
            audio = _audio(choices[0]['path'])
            duration = duration or choices[0]['duration']
        elif file_links:
            audio = {**_audio(None), 'path': file_links[0]['path'],
                     'reason': '연결된 음원 중 기존 녹음과 일치하는 파일 하나를 찾을 수 없습니다.'}
    if capture:
        duration = capture['duration']
        updated = updated or Path(audio['path']).stat().st_mtime
    source = draft or {'engine': timing.get('audio_method') or timing.get('source')}
    before = _before(source, len(lines))
    base_times = timing.get('times', [None] * len(lines))
    offset = timing.get('offset', 0.)
    origins = timing_origins(timing) if timing else [None] * len(lines)
    protected = [stamp is not None and (origin != 'audio' or timing.get('source') == 'LRCLIB')
                 for stamp, origin in zip(base_times, origins)]
    if any(stamp is not None for stamp in base_times):
        clock_times = [None if stamp is None else stamp + offset for stamp in base_times]
        before['details'] = [detail if value == candidate else {} for detail, value, candidate in
                             zip(before['details'], clock_times, before['times'])]
        before['times'] = clock_times
    item_id = 'song:' + song.id + ':' + signature + ':' + recording + ':' + str(duration or '')
    item = {'id': item_id, 'kind': 'song', 'target': song.id, 'fingerprint': signature,
            'live': live, 'identifier': track_id or song.id, 'track_id': track_id,
            'title': song.title, 'artist': song.artist, 'album': song.album,
            'recording': recording, 'recording_key': recording, 'duration': duration,
            'updated': updated, 'before': before, 'base_times': base_times, 'offset': offset,
            'protected': protected, 'lines': lines, 'line_readings': [line.reading for line in song.lines],
            'capture': capture, **audio}
    return _status(item, auto=True, manual=any(protected))


def _read_state(path):
    try:
        value = read_json(path, {})
        return (value if isinstance(value, dict) else {}), path.stat().st_mtime
    except _READ_ERRORS:
        return {}, None


def _orphan(name, draft, timing, updated):
    source = draft or {**timing, 'engine': timing.get('audio_method') or timing.get('source')}
    times = source.get('times')
    if not isinstance(times, list) or not (draft or _has_auto(timing)):
        return None
    identifier = Path(name).stem
    manual = bool(timing.get('times') and 'manual' in timing_origins(timing))
    return _status({'id': 'orphan:' + identifier, 'kind': 'song', 'target': identifier,
                    'title': '가사 문서 미연결', 'artist': '', 'identifier': identifier,
                    'updated': updated, 'lines': [''] * len(times), 'protected': [True] * len(times),
                    'before': _before(source, len(times)), **_audio(None),
                    'reason': '이 분석의 가사 문서와 녹음 식별 정보를 확인할 수 없습니다.'}, auto=True, manual=manual)


def _library_songs(directory, store, local, settings):
    from collections import defaultdict

    recording_links = defaultdict(list)
    for key, song_id in (settings.get('links') or {}).items():
        if len(key.split('\0')) == 3:
            recording_links[song_id].append(key)
    library = store.library()
    known = {song.id for song in library}
    for song in library:
        timing_path = directory / 'timings' / (song.id + '.json')
        draft, updated = _read_state(directory / 'audio_drafts' / (song.id + '.json'))
        if not timing_path.is_file() and not draft:
            continue
        try:
            timing = store.timing(song)
        except _READ_ERRORS:
            continue
        draft_recording = draft.get('recording_key')
        clocks = recording_links[song.id]
        clock = next((key for key in clocks if recording_key(*key.split('\0')) == draft_recording), None)
        if not clock and len(clocks) == 1:
            clock = clocks[0]
        item = _song_item(directory, song, timing, draft, clock, updated or _read_state(timing_path)[1], local)
        if item:
            yield item

    draft_dir = directory / 'audio_drafts'
    timing_dir = directory / 'timings'
    paths = {path.name for path in (*draft_dir.glob('*.json'), *timing_dir.glob('*.json'))}
    for name in paths:
        if Path(name).stem in known:
            continue
        draft, analyzed = _read_state(draft_dir / name)
        timing, saved = _read_state(timing_dir / name)
        item = _orphan(name, draft, timing, analyzed or saved)
        if item:
            yield item


def _live_songs(directory, local, manual_rows, playlist, tracks):
    from lyric_cache import LyricCache
    from missing_audio import CAPTURE_LINKS

    timing_store = LiveTimingStore(directory)
    draft_dir = directory / 'live_audio_drafts'
    drafts = {path.name: path for path in draft_dir.glob('*.json')}
    saved = {path.name for path in timing_store.directory.glob('*.json')}
    visited = set()
    captures = {path.name for path in (directory / 'audio_captures').glob('*') if path.is_dir()}
    if not drafts and not saved and not captures:
        return

    def inspect(key, lines, duration, track_id):
        has_capture = sha256(key.encode()).hexdigest() in captures
        for prefix in ('memory-', 'completed-'):
            song = Song(prefix + sha256(key.encode()).hexdigest()[:24], *key.split('\0'), tuple(lines))
            path = timing_store.path(song)
            if path.name in visited or not (path.name in drafts or path.name in saved or
                                            has_capture and prefix == 'memory-'):
                continue
            draft, updated = _read_state(draft_dir / path.name)
            try:
                timing = timing_store.load(song) or {}
            except _READ_ERRORS:
                continue
            if track_id and timing:
                manual = [i for i, origin in enumerate(timing_origins(timing)) if origin == 'manual']
                lyrics = lyric_digest(line.original for line in lines)
                manual_rows.setdefault((track_id, lyrics), set()).update(manual)
            item = _song_item(directory, song, timing, draft, key, updated or _read_state(path)[1], local,
                              live=True, track_id=track_id, duration=duration)
            if item:
                visited.add(path.name)
                yield item

    for playback, value in LyricCache(directory / 'lyrics_cache').entries():
        direct = value.get('direct') or {}
        track_id = direct.get('provider_id') if direct.get('source') == 'Spotify' else ''
        yield from inspect(playback.key, value['lines'], playback.duration, track_id)

    links, _ = _read_state(directory / CAPTURE_LINKS)
    for track_id, key in links.items():
        if track_id in tracks and isinstance(key, str) and len(key.split('\0')) == 3:
            value = playlist.load(track_id)
            if value:
                yield from inspect(key, value['lines'], tracks[track_id]['duration'], track_id)

    names = drafts.keys() | saved
    for track in tracks.values():
        for artist in dict.fromkeys([', '.join(track['artists']), track['artists'][0]]):
            key = '\0'.join((track['title'], artist, track['album']))
            suffix = sha256(key.encode()).hexdigest()[:24]
            paths = [sha256((prefix + suffix).encode()).hexdigest() + '.json'
                     for prefix in ('memory-', 'completed-')]
            if not (sha256(key.encode()).hexdigest() in captures or
                    any(name in names and name not in visited for name in paths)):
                continue
            value = playlist.load(track['id'])
            if value:
                yield from inspect(key, value['lines'], track['duration'], track['id'])

    # Legacy live drafts can survive the original lyric cache. Their hashed
    # file names cannot identify a recording or restore the lyric text.
    for name in names - visited:
        draft, analyzed = _read_state(draft_dir / name)
        timing, saved_at = _read_state(timing_store.directory / name)
        item = _orphan(name, draft, timing, analyzed or saved_at)
        if item:
            yield item


def discover(directory):
    """Read the active database and saved drafts without registering or rewriting work."""
    from local_library import LocalLibrary
    from playlist_lyrics_store import PlaylistLyricsStore

    directory = Path(directory).absolute()
    store = Store(directory)
    settings = store.settings()
    try:
        local = LocalLibrary(directory)
    except _READ_ERRORS:
        local = None
    playlist = PlaylistLyricsStore(directory)
    tracks = {track['id']: track for track in playlist.metadata()}
    manual_rows = {}
    songs = [*_library_songs(directory, store, local, settings),
             *_live_songs(directory, local, manual_rows, playlist, tracks)]
    jobs = _jobs(directory, tracks)
    for job in jobs:
        manual = manual_rows.get((job['track_id'], lyric_digest(job['lines'])), ())
        for index in manual:
            job['protected'][index] = True
        job['manual'] |= bool(manual)
    items = jobs + songs
    return sorted(items, key=lambda item: (not item['eligible'], item['title'].casefold(), item['identifier']))


def analyze(item, settings, runtime, engine='default', force=False, lookup=None, *, model_cache=None, anchors=None):
    """Run the verified engine using the recording clock and a versioned cache key."""
    from local_analysis_worker import _align, decode_audio, playlist_clock
    import numpy as np

    if not item['eligible']:
        raise ValueError(item['reason'])
    if engine not in ('default', 'experimental', CTC, HYBRID, LEGACY):
        raise ValueError('알 수 없는 재분석 엔진입니다.')
    path = input_file(item['path'])
    initial = path.stat()
    digest = audio_digest(path)
    if item.get('sha256') and digest != item['sha256']:
        raise ValueError('등록한 음원 파일이 변경되었습니다. 현재 버전을 다시 등록하세요.')
    runtime = Path(runtime)
    alignment = dict(settings.get('audio_alignment') or {})
    if engine == 'default':
        engine = automatic_alignment_engine(item['lines'])
    alignment['engine'] = HYBRID if engine == 'experimental' else engine
    if alignment['engine'] == CTC:
        alignment['profile'] = BATCH_CTC_PROFILE
    if alignment['engine'] == HYBRID:
        alignment['profile'] = 'baseline'
    device = alignment.get('device') or settings.get('audio_device')
    alignment['device'] = device if device in ('auto', 'cuda', 'cpu', 'gpu', 'dml') else 'auto'
    spec = {'kind': 'lyrics', 'lines': item['lines'], 'line_readings': item.get('line_readings') or [],
            'alignment': alignment}
    recipe = alignment_recipe(spec, runtime)
    spec['alignment_recipe'] = recipe
    expected = item.get('duration')
    capture = None
    if item.get('audio_kind') == 'capture':
        capture = read_capture(path.parent, expected)
        if capture is None or capture != item.get('capture') or path != capture_path(path.parent):
            raise ValueError('녹음 시계 정보가 변경되었거나 수집 음원을 읽을 수 없습니다.')
    start = capture['start_position'] if capture else 0.
    tail = capture['clip_tail'] if capture else 0.
    cache_key = lyric_digest([digest, recipe, PREPROCESSING_VERSION, ANCHOR_RECOVERY_VERSION,
                             expected, start, tail, anchors])

    def clock_for(times):
        if not item.get('playlist_recording'):
            return None
        if anchors is not None:
            times = [stamp if anchor is None else anchor for anchor, stamp in zip(anchors, times)]
        return playlist_clock(path, item['playlist_recording'], times)

    cached = lookup(cache_key) if lookup and not force else None
    if cached and cached.get('engine') == alignment['engine']:
        validate_times(cached['times'], len(item['lines']))
        clock = clock_for(cached['times'])
        return {**cached, 'cached': True, 'playlist_clock': clock,
                'apply_blocked': bool(clock and not clock.get('usable'))}

    started = time.perf_counter()
    samples = load_capture_samples(path) if capture else decode_audio(path)
    if tail:
        samples = samples[:max(0, len(samples) - int(tail * SAMPLE_RATE))]
    if not len(samples) or float(np.sqrt(np.mean(samples * samples))) < .0001:
        raise ValueError('분석할 수 있는 재생음이 없습니다.')
    final = path.stat()
    if (initial.st_size, initial.st_mtime_ns) != (final.st_size, final.st_mtime_ns):
        raise ValueError('재분석 중 음원 파일이 변경되었습니다.')
    duration = len(samples) / SAMPLE_RATE
    if capture:
        duration = expected
    elif expected is not None and abs(expected - duration) > 2:
        raise ValueError('음원 길이가 기존에 연결한 녹음과 2초 넘게 다릅니다.')
    context = {}
    if anchors is not None:
        context.update(confirmed_times=[t if t is not None and 0 <= t < duration else None for t in anchors],
                       incremental=True)
    if model_cache is not None:
        if alignment['engine'] == LEGACY:
            release_ctc()
            context['final_model_cache'] = model_cache
        elif model_cache:
            model_cache.clear()
    result = _align(samples, spec, duration, settings, runtime, start_position=start, **context)
    if result['engine'] != alignment['engine']:
        why = result.get('fallback_reason') or result.get('refinement_warning') or result['engine']
        raise ValueError(f'선택한 엔진 실행 실패 ({why})')
    clock = clock_for(result['times'])
    return {**result, 'details': result['line_alignment'], 'seconds': time.perf_counter() - started,
            'version': result.get('alignment_version', ALIGNMENT_VERSION), 'duration': duration,
            'cache_key': cache_key, 'audio_sha256': digest, 'cached': False, 'playlist_clock': clock,
            'apply_blocked': bool(clock and not clock.get('usable'))}
