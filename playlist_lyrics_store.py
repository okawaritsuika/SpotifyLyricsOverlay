"""Durable, ID-bound playlist lyrics; personal libraries and clocks stay separate."""
import json
from pathlib import Path
import re
import stat
import threading
import time

from artist_metadata import validated_track
from artist_names import catalog
import imas_timetag
from lyric_annotations import merge_verified_annotations
from lyric_cache import MAX_ENTRY_BYTES, _encode_rows, _linked, _provider, _rows
from lyric_completion import complete_lines
from lyric_match import match_lyrics, phrase_key
from lyric_merge import decode_merge, encode_merge, make_snapshot, merge_lyrics, snapshot_from_direct
from lyrics import Line, normalized
from media import Playback
from public_lyrics import reviewed_original_conflict
from spotify_api import select_track, text_key
from storage import atomic_json


_ID = re.compile(r'[A-Za-z0-9]{22}\Z')
_FAILURES = (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError)
_INDEX_BYTES = 32 * 1024 * 1024


def _track(value):
    if not isinstance(value, dict) or not isinstance(value.get('artists'), list) or not value['artists']:return None
    request = {key: value.get(key) for key in ('title', 'album', 'duration')}
    request['artist'] = value['artists'][0]
    return validated_track(request, value)


def _safe(path):
    for item in (path, *path.parents):
        try:info = item.lstat()
        except FileNotFoundError:continue
        if _linked(info):return False
    return True


def _read(path, maximum, default=None):
    if not _safe(path):raise ValueError('Linked playlist lyric path')
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:raise ValueError('Playlist lyric size')
        with path.open('rb') as stream:body = stream.read(maximum + 1)
    except FileNotFoundError:return default
    if len(body) > maximum:raise ValueError('Playlist lyric size')
    return json.loads(body)


def _sources(values):
    if not isinstance(values, (list, tuple)) or len(values) > 32:raise ValueError('Playlist lyric sources')
    if any(not isinstance(value, str) or len(value) > 2048 or '\0' in value for value in values):
        raise ValueError('Playlist lyric sources')
    return tuple(dict.fromkeys(value for value in values if value.strip()))


def _linked_work(saved):
    sources = saved['sources']
    if 'same_work_reference' not in sources:return None
    tracks = [match[1] for source in sources if (match := re.fullmatch(
        r'https://open\.spotify\.com/track/([A-Za-z0-9]{22})', source))]
    return tracks[0] if len(tracks) == 1 else None


def _direct(value, track, decoded=False, merged=False):
    direct = _provider(value, track['duration'], decoded=decoded)
    if direct and (direct['source'] not in ('Spotify', 'LRCLIB', 'ALSong', 'LyricsPlus', 'imas_timetag')
            and not (merged and direct['source'] in ('Merged', 'manual'))
            or direct.get('fallback', {}).get('source', 'LRCLIB') not in ('LRCLIB', 'ALSong', 'LyricsPlus')):
        raise ValueError('Playlist lyric provider source')
    if direct and direct['source'] == 'Spotify' and direct.get('provider_id') != track['id']:
        raise ValueError('Playlist lyric provider ID mismatch')
    if direct and direct['source'] == 'imas_timetag':
        if not merged or not imas_timetag.supports(track['title'], ', '.join(track['artists']),
                                                   track['album'], track['duration'], track['id']):
            raise ValueError('Playlist time-tag recording mismatch')
        imas_timetag.validate_url(direct.get('provider_id'))
    return direct


def _encode_direct(value):
    if value is None:return None
    result = {**value, 'lines': _encode_rows(value['lines'])}
    if value.get('fallback') is not None:result['fallback'] = _encode_direct(value['fallback'])
    return result


def _quality(value):
    if not value:return (False, 0, False, 0)
    count = sum(stamp is not None for stamp in value['times'])
    return (count == len(value['times']), count / len(value['times']), value['source'] == 'Spotify', count)


def _artist(value):
    translated = catalog().translate(value, idolmaster=True)
    return text_key(re.sub(r'\bCV\s*[.:：]?\s*', 'CV', translated, flags=re.I))


def _same_originals(source, target):
    return bool(source and target and tuple(row.original for row in source)==tuple(row.original for row in target))


def _same_phrases(source, target):
    return tuple(phrase_key(row.original) or row.original for row in source) == tuple(
        phrase_key(row.original) or row.original for row in target)


def _aligned(source, target):
    return (_same_originals(source,target)
            or match_lyrics([row.original for row in source], [row.original for row in target]) is not None
            or complete_lines(source, target) is not None)


def _merge_recording(track, snapshots):
    playback = Playback(title=track['title'], artist=', '.join(track['artists']),
                        album=track['album'], duration=track['duration'])
    return merge_lyrics(playback, snapshots, track_id=track['id'])


def _decode_recording_merge(value, lines, direct, track):
    merge = decode_merge(value, len(lines))
    if any(snapshot['source'] == 'Spotify' and snapshot['track_id'] not in (None, track['id'])
           or snapshot['source'] == 'imas_timetag' and
              (snapshot['track_id'] != track['id'] or not snapshot['recording_verified'])
           for snapshot in merge['snapshots']):
        raise ValueError('Playlist lyric snapshot recording mismatch')
    if direct and direct['source'] == 'imas_timetag' and not any(
            snapshot['source'] == direct['source'] and snapshot['provider_id'] == direct['provider_id']
            and snapshot['times'] == tuple(direct['times']) for snapshot in merge['snapshots']):
        raise ValueError('Playlist lyric source snapshot mismatch')
    times = [row['selected_time'] for row in merge['rows']]
    if times != (direct['times'] if direct else [None] * len(lines)):
        raise ValueError('Playlist lyric merge clock mismatch')
    return merge


class PlaylistLyricsStore:
    def __init__(self, directory):
        self.directory = Path(directory) / 'playlist_lyrics'
        self.tracks_directory = self.directory / 'tracks'
        self.index_path = self.directory / 'index.json'
        self.lock = threading.RLock()
        self._annotation_cache = None

    def _load(self, track_id, include_rejected=False):
        if not isinstance(track_id, str) or not _ID.fullmatch(track_id):return None
        value = _read(self.tracks_directory / (track_id + '.json'), MAX_ENTRY_BYTES)
        if value is None:return None
        if not isinstance(value, dict) or value.get('schema_version') != 1:raise ValueError('Playlist lyric version')
        track = _track(value.get('track'))
        if not track or track['id'] != track_id:raise ValueError('Playlist lyric track')
        lines = _rows(value['lines'], decoded=True)
        if not include_rejected and reviewed_original_conflict(track['title'],track['artists'][0],
                track['album'],track['duration'],lines):return None
        direct = _direct(value.get('direct'), track, decoded=True, merged='merge' in value)
        if direct and not _aligned(direct['lines'], lines):raise ValueError('Playlist lyric text mismatch')
        merge = None
        if 'merge' in value:
            try:merge = _decode_recording_merge(value['merge'], lines, direct, track)
            except _FAILURES:
                if direct and direct['source'] in ('Merged', 'manual', 'imas_timetag'):direct = None
        result = {'track': track, 'lines': lines, 'direct': direct,
                  'sources': _sources(value.get('sources', []))}
        if merge:
            result['snapshots'] = merge['snapshots']
            result['merge_rows'] = merge['rows']
        return result

    def load(self, track_id):
        try:
            with self.lock:return self._load(track_id)
        except _FAILURES:return None

    def _index(self):
        value = _read(self.index_path, _INDEX_BYTES, {'schema_version': 1, 'tracks': {}})
        if (not isinstance(value, dict) or value.get('schema_version') != 1
                or not isinstance(value.get('tracks'), dict) or len(value['tracks']) > 50000):
            raise ValueError('Playlist lyric index')
        return value['tracks']

    def metadata(self):
        """Read the public track index without opening every lyric document."""
        try:
            with self.lock:return tuple(track for value in self._index().values() if (track := _track(value)))
        except _FAILURES:return ()

    def save(self, track, lines, direct, sources=(), *, snapshots=(), merge_rows=()):
        try:
            track = _track(track)
            if not track:return False
            lines = _rows(lines)
            if reviewed_original_conflict(track['title'],track['artists'][0],track['album'],track['duration'],lines):return False
            sources = _sources(sources)
            reviewed_update = 'reviewed_translation' in sources
            with self.lock:
                old = self._load(track['id'], include_rejected=True)
                if old and reviewed_original_conflict(old['track']['title'],old['track']['artists'][0],
                        old['track']['album'],old['track']['duration'],old['lines']):
                    # Preserve the rejected source document before a corrected result replaces it.
                    rejected = _read(self.tracks_directory / (track['id'] + '.json'), MAX_ENTRY_BYTES)
                    backup = self.directory / 'rejected' / (track['id'] + '.json')
                    if not _safe(backup):return False
                    if backup.exists() and _read(backup,MAX_ENTRY_BYTES) != rejected:return False
                    if not backup.exists():atomic_json(backup,rejected)
                    old = None
                saved_snapshots = old.get('snapshots', ()) if old else ()
                direct = _direct(direct, track, merged=bool(snapshots or saved_snapshots))
                if direct and not _same_originals(direct['lines'],lines) and match_lyrics(
                        [line.original for line in direct['lines']], [line.original for line in lines]) is None:
                    raise ValueError('Playlist lyric text mismatch')
                if old:
                    same_rows = _same_originals(old['lines'], lines)
                    same_phrases = bool(snapshots) and _same_phrases(old['lines'], lines)
                    if not (same_rows or same_phrases or _aligned(old['lines'], lines) or _aligned(lines, old['lines'])):
                        raise ValueError('Playlist lyric replacement mismatch')
                    # Never erase saved annotations or reduce the original text.
                    if same_rows or same_phrases:
                        # Exact arrays also bind symbol-only and repeated rows to their positions.
                        lines = tuple(Line(supplied.original,
                                           saved.reading if saved.reading.strip() else supplied.reading,
                                           saved.korean if saved.korean.strip() else supplied.korean)
                                      for saved, supplied in zip(old['lines'], lines))
                    elif not snapshots:
                        completed = complete_lines(old['lines'], lines)
                        base = completed[0] if completed else old['lines']
                        lines, _ = merge_verified_annotations(base, [lines])
                    else:
                        # An annotation may have arrived while the collector ran.
                        lines, _ = merge_verified_annotations(lines, [old['lines']])
                    previous = old['direct']
                    fresh_spotify = (direct and direct['source'] == 'Spotify' and previous
                                     and previous['source'] != 'Spotify' and not snapshots)
                    if not snapshots and not saved_snapshots and not fresh_spotify and previous and (_quality(previous) >= _quality(direct)
                            or direct and not _aligned(previous['lines'], direct['lines'])):
                        direct = previous
                    sources = tuple(dict.fromkeys((*old['sources'], *sources)))
                    if snapshots or saved_snapshots or fresh_spotify:
                        documents = list(snapshots or saved_snapshots)
                        if fresh_spotify and not saved_snapshots:
                            documents.append(snapshot_from_direct(previous))
                            documents.append(make_snapshot('local', old['lines'], track_id=track['id'],
                                                           recording_verified=True, annotation_only=True))
                        if direct and not snapshots and direct != previous and direct['source'] != 'Merged':
                            candidate = snapshot_from_direct(direct)
                            if candidate not in documents:documents.append(candidate)
                        for snapshot in saved_snapshots:
                            if snapshot not in documents:documents.append(snapshot)
                        legacy_review = not saved_snapshots and 'reviewed_translation' in old['sources']
                        if (reviewed_update or legacy_review) and not any(
                                snapshot['source'] == 'manual' and snapshot['annotation_only']
                                and snapshot['lines'] == lines for snapshot in documents):
                            documents = [snapshot for snapshot in documents
                                         if snapshot['source'] != 'manual' or not snapshot['annotation_only']]
                            documents.append(make_snapshot('manual', lines, track_id=track['id'],
                                                           recording_verified=True, annotation_only=True))
                        elif not snapshots and same_rows and lines != old['lines']:
                            documents = [snapshot for snapshot in documents
                                         if snapshot['provider_id'] != 'playlist-local-annotations']
                            documents.append(make_snapshot('local', lines, track_id=track['id'],
                                provider_id='playlist-local-annotations', recording_verified=True,
                                annotation_only=True))
                        if tuple(documents) != tuple(snapshots):
                            result = _merge_recording(track, documents)
                            lines, direct = result['lines'], result['direct']
                            snapshots, merge_rows = result['snapshots'], result['rows']
                direct = _direct(direct, track, merged=bool(snapshots))
                if direct and not _aligned(direct['lines'], lines):raise ValueError('Playlist lyric text mismatch')
                value = {'schema_version': 1, 'track': track, 'lines': _encode_rows(lines),
                         'direct': _encode_direct(direct), 'sources': list(_sources(sources)),
                         'saved_at': time.time()}
                if snapshots:
                    value['merge'] = encode_merge({'snapshots': snapshots, 'rows': list(merge_rows)})
                    _decode_recording_merge(value['merge'], lines, direct, track)
                if len(json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8')) > MAX_ENTRY_BYTES:return False
                if not _safe(self.tracks_directory) or not _safe(self.index_path):return False
                index = self._index()
                changed = index.get(track['id']) != track
                index[track['id']] = track
                saved_index = {'schema_version': 1, 'tracks': index}
                if len(json.dumps(saved_index, ensure_ascii=False, indent=2).encode('utf-8')) > _INDEX_BYTES:return False
                atomic_json(self.tracks_directory / (track['id'] + '.json'), value)
                self._annotation_cache = None
                if changed:atomic_json(self.index_path, saved_index)
            return True
        except _FAILURES:return False

    def find(self, playback):
        """Require exact title/album/length and a unique recording's known credits."""
        try:
            request = {'title': playback.title, 'artist': _artist(playback.artist),
                       'album': playback.album, 'duration': playback.duration}
            # A missing album must not share another release's clock.
            if not request['album'].strip():return None
            with self.lock:
                tracks = []
                for value in self._index().values():
                    if (not isinstance(value, dict) or text_key(value.get('title')) != text_key(request['title'])
                            or text_key(value.get('album')) != text_key(request['album'])):continue
                    if abs(float(value.get('duration', 0)) - float(request['duration'])) > 2:continue
                    track = _track(value)
                    if track:tracks.append({**track, 'artists': [_artist(name) for name in track['artists']]})
                candidate = select_track(request, tracks)
                loaded = self._load(candidate['id']) if candidate else None
                if loaded:
                    actual = {**loaded['track'], 'artists': [_artist(name) for name in loaded['track']['artists']]}
                    if select_track(request, [actual]):return loaded
                return None
        except _FAILURES:return None

    def find_annotations(self, title, album, originals):
        """Find translations by verified lyric text, regardless of performer credits."""
        from reader_bridge import recording_base

        if not originals:return ()
        key = (title, album, originals)
        with self.lock:
            if self._annotation_cache and self._annotation_cache[0] == key:
                return self._annotation_cache[1]
            title_key, album_key = normalized(title), normalized(album)
            work = normalized(recording_base(title))
            if not work:return ()
            try:index = self._index()
            except _FAILURES:return ()
            candidates = [[], [], []]
            for track_id, value in index.items():
                candidate = value.get('title') if isinstance(value, dict) else None
                if not isinstance(candidate, str):continue
                if normalized(candidate) == title_key:
                    rank = 0 if normalized(str(value.get('album') or '')) == album_key else 1
                elif normalized(recording_base(candidate)) == work:
                    rank = 2
                else:continue
                saved = self.load(track_id)
                if saved and any(row.korean.strip() for row in saved['lines']):
                    candidates[rank].append(saved['lines'])
            lines = tuple(Line(original, '', '') for original in originals)
            result = ()
            for sources in candidates:
                if not sources:continue
                merged, contributors = merge_verified_annotations(lines, sources)
                if contributors:
                    result = tuple(row.korean for row in merged)
                    break
            self._annotation_cache = (key, result)
            return result

    def find_related(self, playback, timing_store=None):
        """Reuse a matching release before another network lookup is required."""
        from dataclasses import replace
        from recording_reuse import compatibility, load_saved_timing, select_related
        try:
            requested = replace(playback, artist=_artist(playback.artist))
            candidates = []
            with self.lock:
                for value in self._index().values():
                    track = _track(value)
                    if not track:continue
                    matching = track['artists'][0]
                    source = {'title': track['title'], 'artist': matching, 'album': track['album'],
                              'duration': track['duration']}
                    compatible = compatibility({**source, 'artist': _artist(matching)}, requested)
                    if compatible is None:continue
                    saved = self._load(track['id'])
                    if saved is None:continue
                    key = '\0'.join((track['title'], matching, track['album']))
                    if key == playback.key:continue
                    candidate = {**source, 'key': key, 'lines': saved['lines'], 'direct': saved['direct'],
                                 'compatibility': compatible}
                    if timing_store and (compatible['timing_compatible'] or compatible.get('tracking_compatible')):
                        candidate['timing'] = load_saved_timing(timing_store, key, saved['lines'])
                    candidates.append(candidate)
            return select_related(candidates, playback)
        except _FAILURES:return None

    def find_linked_reference(self, playback, saved, timing_store, lyric_cache):
        """Use clocks from recordings already linked to the same work and edit."""
        from recording_reuse import linked_edit, load_saved_timing, related_reference

        work = _linked_work(saved)
        if not work:return None
        target = saved['track']
        edit = linked_edit(target['title'], target['artists'][0])
        if edit is None:return None
        candidates = []
        with self.lock:
            try:index = self._index()
            except _FAILURES:return None
            for value in index.values():
                track = _track(value)
                if (not track or track['id'] == target['id'] or abs(track['duration']-playback.duration)>2
                        or abs(track['duration']-target['duration'])>2
                        or linked_edit(track['title'], track['artists'][0]) != edit):continue
                try:source = self._load(track['id'])
                except _FAILURES:continue
                if not source or source['track'] != track or _linked_work(source) != work:continue
                for artist in dict.fromkeys([', '.join(track['artists']), track['artists'][0]]):
                    recording = Playback(track['title'], artist, track['album'], duration=track['duration'])
                    cached = lyric_cache.load(recording)
                    lines = cached['lines'] if cached else source['lines']
                    timing = load_saved_timing(timing_store, recording.key, lines)
                    if timing is None and lines != source['lines']:
                        lines = source['lines']
                        timing = load_saved_timing(timing_store, recording.key, lines)
                    if not timing or abs(timing.get('analysis_duration', track['duration'])-track['duration'])>2:
                        continue
                    times = [round(stamp+timing['offset'],3) if stamp is not None else None
                             for stamp in timing['times']]
                    if all(stamp is None or 0<=stamp<min(track['duration'],playback.duration) for stamp in times):
                        candidates.append({'lines': lines, 'tracking_times': times})
        times = related_reference(saved['lines'], candidates)
        return {'lines': saved['lines'], 'tracking_times': times} if times else None
