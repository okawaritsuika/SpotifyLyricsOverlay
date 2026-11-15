"""Bounded local lyric cache keyed by the complete Spotify recording metadata."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time

from lyrics import Line, is_cinderella_no1
from lyric_merge import decode_merge, encode_merge
from storage import validate_times


MAX_ENTRY_BYTES = 1024 * 1024
MAX_ROWS = 1500
MAX_TEXT = 4000
_CACHE_NAME = re.compile(r'[0-9a-f]{64}\.json\Z')
_FAILURES = (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError)


def _linked(info):
    # Windows directory junctions are reparse points, not necessarily symlinks.
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def _number(value, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('Invalid cache number')
    if value < 0 or positive and value == 0:
        raise ValueError('Invalid cache number')
    return value


def _identity(playback):
    values = (playback.title, playback.artist, playback.album)
    if any(not isinstance(value, str) or len(value) > MAX_TEXT or '\0' in value for value in values):
        raise ValueError('Invalid cache metadata')
    if not playback.title.strip() or not playback.artist.strip():
        raise ValueError('Incomplete cache metadata')
    return '\0'.join(values), _number(playback.duration, positive=True)


def _rows(value, decoded=False):
    if not isinstance(value, (list, tuple)) or not 0 < len(value) <= MAX_ROWS:
        raise ValueError('Invalid cached row count')
    rows = []
    size = 0
    for row in value:
        if decoded:
            if not isinstance(row, dict):raise ValueError('Invalid cached row')
            row = Line(row['original'], row['reading'], row['korean'])
        if not isinstance(row, Line):raise ValueError('Invalid lyric row')
        for text in (row.original, row.reading, row.korean):
            if not isinstance(text, str) or len(text) > MAX_TEXT:
                raise ValueError('Invalid lyric text')
            size += len(text.encode('utf-8'))
        if not row.original.strip() or size > MAX_ENTRY_BYTES:
            raise ValueError('Invalid lyric text size')
        rows.append(row)
    return tuple(rows)


def _provider(value, duration, decoded=False, allow_fallback=True):
    if value is None:return None
    if not isinstance(value, dict):raise ValueError('Invalid cached provider')
    source = value['source']
    if not isinstance(source, str) or not source.strip() or len(source) > 80:
        raise ValueError('Invalid provider source')
    provider_id = value.get('provider_id')
    if (isinstance(provider_id, bool) or not isinstance(provider_id, (str, int, type(None)))
            or isinstance(provider_id, str) and len(provider_id) > 256
            or isinstance(provider_id, int) and not 0 <= provider_id <= 2**63 - 1):
        raise ValueError('Invalid provider identifier')
    lines = _rows(value['lines'], decoded)
    times = value['times']
    validate_times(times, len(lines))
    if any(stamp is not None and stamp > duration + 2 for stamp in times):
        raise ValueError('Cached timing exceeds recording')
    result={'source': source, 'lines': lines, 'times': list(times), 'provider_id': provider_id}
    if 'fallback' in value:
        if (not allow_fallback or source!='Spotify'
                or not any(stamp is not None for stamp in times)):
            raise ValueError('Invalid auxiliary timing parent')
        fallback=_provider(value['fallback'],duration,decoded,allow_fallback=False)
        if (fallback is None or fallback['source']=='Spotify'
                or not any(stamp is not None for stamp in fallback['times'])):
            raise ValueError('Invalid auxiliary timing provider')
        result['fallback']=fallback
    return result


def _encode_rows(lines):
    return [{'original': line.original, 'reading': line.reading, 'korean': line.korean} for line in lines]


def _rejected_original(playback, lines):
    if not is_cinderella_no1(playback.title):return False
    from public_lyrics import reviewed_original_conflict
    return reviewed_original_conflict(playback.title, playback.artist, playback.album, playback.duration, lines)


def _reuse(value):
    if value is None:return None
    if not isinstance(value, dict):raise ValueError('Invalid cache reuse provenance')
    key = value.get('source_key')
    if not isinstance(key, str):raise ValueError('Invalid reuse recording')
    parts = key.split('\0')
    if len(parts) != 3 or not parts[0].strip() or not parts[1].strip() or any(len(part) > MAX_TEXT for part in parts):
        raise ValueError('Invalid reuse recording')
    return {'source_key': key, 'source_duration': _number(value['source_duration'], positive=True)}


class LyricCache:
    def __init__(self, directory, max_entries=200, max_bytes=32 * 1024 * 1024):
        self.directory = Path(os.path.abspath(os.fspath(directory)))
        self.max_entries = max(0, int(max_entries))
        self.max_bytes = max(0, int(max_bytes))
        self.lock = threading.Lock()

    def _root_safe(self, create=False):
        # Never resolve a link and then treat its external target as cache-owned.
        for path in (self.directory, *self.directory.parents):
            try:info = path.lstat()
            except FileNotFoundError:continue
            if _linked(info) or not stat.S_ISDIR(info.st_mode):return False
        if create:self.directory.mkdir(parents=True, exist_ok=True)
        if not self.directory.is_dir():return False
        info = self.directory.lstat()
        return not _linked(info) and stat.S_ISDIR(info.st_mode)

    def _path(self, key):
        return self.directory / (hashlib.sha256(key.encode('utf-8')).hexdigest() + '.json')

    def _regular(self, path):
        if path.parent != self.directory or not _CACHE_NAME.fullmatch(path.name):return None
        try:info = path.lstat()
        except FileNotFoundError:return None
        return info if not _linked(info) and stat.S_ISREG(info.st_mode) else None

    def load(self, playback):
        try:
            key, duration = _identity(playback)
            with self.lock:
                if not self._root_safe():return None
                path = self._path(key)
                info = self._regular(path)
                if info is None or info.st_size > MAX_ENTRY_BYTES:return None
                with path.open('rb') as handle:body = handle.read(MAX_ENTRY_BYTES + 1)
                if len(body) > MAX_ENTRY_BYTES:return None
                value = json.loads(body)
                if (not isinstance(value, dict) or type(value.get('version')) is not int
                        or value['version'] != 1 or value.get('key') != key):return None
                saved_duration = _number(value['duration'], positive=True)
                if abs(saved_duration - duration) > 2:return None
                saved_at = _number(value['saved_at'])
                lines = _rows(value['lines'], decoded=True)
                if _rejected_original(playback, lines):return None
                direct = _provider(value.get('direct'), saved_duration, decoded=True)
                result = {'lines': lines, 'direct': direct, 'saved_at': saved_at}
                if 'merge' in value:
                    try:
                        merge = decode_merge(value['merge'], len(lines))
                    except _FAILURES:
                        # The old display data is still usable if optional
                        # provenance is damaged. Recollect it on playback.
                        if direct and direct['source'] == 'Merged':
                            result['direct'] = None
                    else:
                        result['merge'] = merge
                reuse = _reuse(value.get('reuse'))
                if reuse is not None:
                    from recording_reuse import compatibility
                    title, artist, album = reuse['source_key'].split('\0')
                    compatible = compatibility({'title': title, 'artist': artist, 'album': album,
                                                'duration': reuse['source_duration']}, playback)
                    if compatible is None:return None
                    if not compatible['timing_compatible']:result['direct'] = None
                    result['reuse'] = {**reuse, **compatible}
                return result
        except _FAILURES:
            return None

    @staticmethod
    def supplement_exact(cached, bundled):
        """Fill an exact recording from the release without replacing local annotations."""
        if not cached or not bundled:return cached or bundled
        local, supplied = cached['lines'], bundled['lines']
        if tuple(row.original for row in local) != tuple(row.original for row in supplied):
            return cached
        lines = tuple(Line(row.original, row.reading if row.reading.strip() else extra.reading,
                           row.korean if row.korean.strip() else extra.korean)
                      for row, extra in zip(local, supplied))
        direct = cached.get('direct')
        if not direct or not any(stamp is not None for stamp in direct['times']):
            direct = bundled.get('direct') or direct
        return {**cached, 'lines': lines, 'direct': direct}

    def prefer_bundled_manual(self, playback, cached, *, bundled_data, timing_store):
        """Recover an exact lyric layout for saved manual or audio timestamps."""
        from live_timing import LiveTimingStore, original_signature
        from lyrics import Song
        from storage import read_json
        from timing_quality import cleared_rows, manual_priority, timing_origins

        def saved_times(store, identities, *, include_audio=False):
            matches = []
            for identity in identities:
                raw = read_json(store.path(Song(*identity, ())), {})
                if not isinstance(raw, dict):raise ValueError('Invalid saved timing document')
                if raw and (manual_priority(raw) or cleared_rows(raw)
                            or raw.get('removed_alsong_rows') or raw.get('added_alsong_rows')
                            or include_audio and 'audio' in timing_origins(raw)):
                    matches.append((identity, raw))
            return matches

        def paired(candidate, store, matches, *, require_stamp):
            if not candidate:return None
            song = Song(*matches[0][0], candidate['lines'])
            signature = original_signature(song)
            for _, raw in matches:
                if raw.get('signature') != signature:continue
                timing = store.clean(raw, song)
                stamps = [stamp for stamp in timing['times'] if stamp is not None]
                if require_stamp and not stamps:continue
                if all(stamp + timing['offset'] <= duration + 2 for stamp in stamps):
                    return {**candidate, 'manual_timing': timing}
            return None

        try:
            key, duration = _identity(playback)
            if not playback.album.strip():return cached
            suffix = hashlib.sha256(key.encode()).hexdigest()[:24]
            bundle = Path(bundled_data)
            bundled_store = LiveTimingStore(bundle)
            identities = tuple((prefix + suffix, playback.title, playback.artist, playback.album)
                               for prefix in ('memory-', 'completed-'))
            local = saved_times(timing_store, identities)
            # Local manual data remains authoritative even if its lyric layout was evicted.
            store = timing_store if local else bundled_store
            matches = local or saved_times(bundled_store, identities, include_audio=True)
            if not matches:return cached

            result = paired(cached, store, matches, require_stamp=not local)
            if result:return cached if local else result
            result = paired(LyricCache(bundle / 'lyrics_cache').load(playback), store,
                            matches, require_stamp=not local)
            if result:return result

            from playlist_lyrics_store import PlaylistLyricsStore
            recording = PlaylistLyricsStore(bundle).find(playback)
            return paired(recording, store, matches, require_stamp=not local) or cached
        except _FAILURES:
            return cached

    def entries(self):
        """Enumerate validated recordings for the local translation worklist."""
        from media import Playback
        candidates = []
        try:
            with self.lock:
                if not self._root_safe():return ()
                read_bytes = 0
                for path in sorted(self.directory.iterdir()):
                    info = self._regular(path)
                    if info is None or info.st_size > MAX_ENTRY_BYTES:continue
                    read_bytes += info.st_size
                    if read_bytes > self.max_bytes:break
                    try:
                        with path.open('rb') as handle:body = handle.read(MAX_ENTRY_BYTES + 1)
                        if len(body) > MAX_ENTRY_BYTES:continue
                        value = json.loads(body)
                        key = value['key']
                        if not isinstance(key, str) or path != self._path(key):continue
                        parts = key.split('\0')
                        if len(parts) != 3:continue
                        playback = Playback(*parts, duration=value['duration'])
                        _identity(playback)
                        candidates.append(playback)
                    except _FAILURES:continue
            # load performs the same complete row/provider validation used on playback.
            return tuple((playback, cached) for playback in candidates
                         if (cached := self.load(playback)) is not None)
        except _FAILURES:return ()

    def load_related(self, playback, timing_store=None):
        """Read related releases; exact load and original cache files stay intact."""
        from media import Playback
        from recording_reuse import compatibility, load_saved_timing, select_related
        try:
            _identity(playback)
            candidates = []
            with self.lock:
                if not self._root_safe():return None
                read_bytes = 0
                for path in self.directory.iterdir():
                    info = self._regular(path)
                    if info is None or info.st_size > MAX_ENTRY_BYTES:continue
                    read_bytes += info.st_size
                    if read_bytes > self.max_bytes:return None
                    try:
                        with path.open('rb') as handle:body = handle.read(MAX_ENTRY_BYTES + 1)
                        if len(body) > MAX_ENTRY_BYTES:continue
                        value = json.loads(body)
                        if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1:continue
                        key = value['key']
                        if not isinstance(key, str) or path != self._path(key):continue
                        if key == playback.key:continue
                        parts = key.split('\0')
                        if len(parts) != 3:continue
                        duration = _number(value['duration'], positive=True)
                        _identity(Playback(*parts, duration=duration))
                        reuse = _reuse(value.get('reuse'))
                        source_key = reuse['source_key'] if reuse else key
                        source_duration = reuse['source_duration'] if reuse else duration
                        title, artist, album = source_key.split('\0')
                        source = {'title': title, 'artist': artist, 'album': album, 'duration': source_duration}
                        compatible = compatibility(source, playback)
                        if compatible is None:continue
                        lines = _rows(value['lines'], decoded=True)
                        candidate = {**source, 'key': source_key, 'lines': lines,
                                     'saved_at': _number(value['saved_at']), 'compatibility': compatible,
                                     'borrowed': bool(reuse),
                                     'direct': _provider(value.get('direct'), duration, decoded=True)}
                        if timing_store and (compatible['timing_compatible'] or compatible.get('tracking_compatible')):
                            candidate['timing'] = load_saved_timing(timing_store, key, lines)
                        candidates.append(candidate)
                    except _FAILURES:continue
            return select_related(candidates, playback)
        except _FAILURES:return None

    def _prune(self, keep):
        if not self._root_safe():return False
        entries = []
        for path in self.directory.iterdir():
            info = self._regular(path)
            if info is not None:entries.append((info.st_mtime_ns, path.name, path, info.st_size))
        total = sum(entry[3] for entry in entries)
        count = len(entries)
        for _, _, path, size in sorted(entries):
            if count <= self.max_entries and total <= self.max_bytes:return True
            if path == keep:continue
            if not self._root_safe() or self._regular(path) is None:continue
            try:path.unlink()
            except OSError:continue
            count -= 1
            total -= size
        return count <= self.max_entries and total <= self.max_bytes

    def save(self, playback, lines, direct=None, reuse=None, merge=None):
        temporary = None
        try:
            if not self.max_entries or not self.max_bytes:return False
            key, duration = _identity(playback)
            lines = _rows(lines)
            if _rejected_original(playback, lines):return False
            direct = _provider(direct, duration)
            value = {'version': 1, 'key': key, 'duration': duration,
                     'saved_at': _number(time.time()), 'lines': _encode_rows(lines), 'direct': None}
            reuse = _reuse(reuse)
            if reuse is not None:value['reuse'] = reuse
            if direct is not None:
                value['direct'] = {**direct, 'lines': _encode_rows(direct['lines'])}
                if 'fallback' in direct:
                    fallback=direct['fallback']
                    value['direct']['fallback']={**fallback,'lines':_encode_rows(fallback['lines'])}
            if merge is not None:
                encoded = encode_merge(merge)
                decode_merge(encoded, len(lines))
                value['merge'] = encoded
            body = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
            if len(body) > min(MAX_ENTRY_BYTES, self.max_bytes):return False
            with self.lock:
                if not self._root_safe(create=True):return False
                path = self._path(key)
                if path.exists() or path.is_symlink():
                    if self._regular(path) is None:return False
                descriptor, filename = tempfile.mkstemp(prefix='.lyric-', suffix='.tmp', dir=self.directory)
                temporary = Path(filename)
                with os.fdopen(descriptor, 'wb') as handle:
                    handle.write(body)
                    handle.flush()
                    os.fsync(handle.fileno())
                if not self._root_safe():return False
                if path.exists() or path.is_symlink():
                    if self._regular(path) is None:return False
                os.replace(temporary, path)
                temporary = None
                if not self._prune(path):
                    if self._root_safe() and self._regular(path) is not None:path.unlink()
                    return False
                return True
        except _FAILURES:
            return False
        finally:
            if temporary is not None:
                try:
                    if self._root_safe() and temporary.parent == self.directory and not temporary.is_symlink():
                        temporary.unlink(missing_ok=True)
                except OSError:pass
