"""Serial public-source enrichment for an ID-bound, non-playing playlist track.

This worker never saves lyrics, reads a Spotify profile, or starts playback.
Only real provider onsets can complete timing; annotations require the whole
original document to agree. The caller owns durable writes and retry policy.
"""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from pathlib import Path
import math
import queue
import re
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit

import alsong_provider
import lyricsplus_provider
import public_lyrics
import web_lyrics
from background_lyrics import mapped_times
from lyric_annotations import LyricAnnotations, merge_verified_annotations
from lyric_cache import _provider, _rows
from lyric_match import match_lyrics
from lyrics import Line, normalized
from media import Playback
from reader_bridge import ReaderBridge, recording_base
from saved_annotations import _artist_identity, match_saved_annotations
from storage import Store, validate_times


REQUEST_INTERVAL = 1.0
TRACK_SECONDS = 120.0
BROWSER_SECONDS = 60.0
_PROVIDERS = {'Spotify', 'LRCLIB', 'ALSong', 'LyricsPlus'}
_request_lock = threading.Lock()
_next_request = 0.0
_cooldowns = {}
_browser_cooldown = 0.0
_local_cache = {}
_store_cache = {}
_annotation_indexes = {}


class SourceRateLimited(Exception):
    code = 'rate_limited'

    def __init__(self, source, retry_after):
        super().__init__('Public lyric source rate limited')
        self.source = source
        self.retry_after = max(1, math.ceil(retry_after))


class _Deferred(Exception):
    def __init__(self, state, retry_after=0, details=None):
        self.state, self.retry_after = state, retry_after
        self.details = details or {}


class _Cancelled(Exception):
    code = 'cancelled'


def restore_cooldowns(state):
    """Restore evidenced provider waits from trusted, persisted job state.

    Pending retry_at values are wall-clock deadlines. Preserve their remaining
    duration rather than restarting retry_after when the process restarts.
    """
    global _browser_cooldown
    pending = state.get('enrichment_pending') if isinstance(state, dict) else None
    if not isinstance(pending, dict):return
    wall_now, monotonic_now = time.time(), time.monotonic()
    with _request_lock:
        for value in pending.values():
            if not isinstance(value, dict):continue
            retry_at, attempts = value.get('retry_at'), value.get('attempts')
            if type(retry_at) not in (int, float) or not isinstance(attempts, list):continue
            try:
                deadline = monotonic_now+(retry_at-wall_now)
                if not math.isfinite(deadline) or retry_at <= wall_now:continue
            except OverflowError:continue
            for attempt in attempts:
                if not isinstance(attempt, dict):continue
                source, status, retry = attempt.get('source'), attempt.get('state'), attempt.get('retry_after')
                if type(retry) not in (int, float):continue
                try:
                    if not math.isfinite(retry) or retry <= 0:continue
                except OverflowError:continue
                if source == 'wiki_browser':
                    if status in ('deferred_cooldown', 'blocked', 'browser_timeout', 'browser_error'):
                        _browser_cooldown = max(_browser_cooldown, deadline)
                elif (source in ('LRCLIB', 'ALSong', 'LyricsPlus', 'namu', 'sidem')
                        and status in ('deferred_cooldown', 'blocked', 'request_failed')):
                    _cooldowns[source] = max(_cooldowns.get(source, 0), deadline)


def _check(cancel, deadline):
    if cancel.is_set():raise _Cancelled()
    if time.monotonic() >= deadline:raise _Deferred('deferred_deadline')


def _http_error(error):
    seen = set()
    while error is not None and id(error) not in seen:
        if isinstance(error, HTTPError):return error
        seen.add(id(error))
        error = error.__cause__ or error.__context__
    return None


def _retry_after(error):
    value = error.headers.get('Retry-After', '') if error.headers else ''
    try:
        seconds = float(value)
        if math.isfinite(seconds) and seconds >= 0:return max(1, seconds)
    except (ValueError, TypeError):pass
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:date = date.replace(tzinfo=timezone.utc)
        return max(1, (date-datetime.now(timezone.utc)).total_seconds())
    except (ValueError, TypeError, OverflowError):return 300


def _request(source, call, cancel, deadline, *args):
    """Pace every provider transport, including bounded search/detail retries."""
    global _next_request
    _check(cancel, deadline)
    with _request_lock:
        remaining = _cooldowns.get(source, 0)-time.monotonic()
        if remaining > 0:raise _Deferred('deferred_cooldown', math.ceil(remaining))
        delay = max(0, _next_request-time.monotonic())
        if time.monotonic()+delay >= deadline:raise _Deferred('deferred_deadline')
        if cancel.wait(delay):raise _Cancelled()
        _check(cancel, deadline)
        try:
            return call(*args)
        except Exception as error:
            http = _http_error(error)
            if http and http.code == 429:
                retry = _retry_after(http)
                _cooldowns[source] = time.monotonic()+retry
                # Not an OSError/ValueError: optional-provider catch blocks must
                # never swallow a throttle and continue issuing more requests.
                raise SourceRateLimited(source, retry) from None
            if http and http.code in (401, 403):
                _cooldowns[source] = time.monotonic()+1800
            elif isinstance(error, (OSError, HTTPException)) and (not http or http.code >= 500):
                _cooldowns[source] = time.monotonic()+300
            raise
        finally:
            _next_request = time.monotonic()+REQUEST_INTERVAL


def _whole(left, right):
    if not left or not right:return False
    source, target = tuple(row.original for row in left), tuple(row.original for row in right)
    # Provider originals can include a standalone music mark. It must not make
    # an unchanged Spotify document fail to recognize its own supplied clock.
    if source == target:return True
    return match_lyrics(source, target) is not None


def _decode_rows(value):
    return _rows(value, decoded=bool(value and isinstance(value[0], dict)))


def _direct(value, track):
    if value is None:return None
    if not isinstance(value, dict):raise ValueError('Invalid lyric provider')
    if value.get('source') not in _PROVIDERS or value.get('annotation_only'):return None
    result = _provider(value, track['duration'],
                       decoded=bool(value.get('lines') and isinstance(value['lines'][0], dict)))
    if result['source'] == 'Spotify' and result.get('provider_id') != track['id']:return None
    if result.get('fallback') and result['fallback']['source'] not in _PROVIDERS:
        result.pop('fallback')
    return result if any(stamp is not None for stamp in result['times']) else None


def _timed(direct, lines):
    if not direct or not lines or not _whole(direct['lines'], lines):return [None]*len(lines)
    times = mapped_times(direct, lines)
    validate_times(times, len(lines))
    return times


def _safe_source(value):
    if not isinstance(value, str):return ''
    if value in _PROVIDERS or value in ('existing_local_cache', 'saved_library', 'playlist_lyrics'):
        return value
    try:
        parts = urlsplit(value)
        if parts.username or parts.password:return ''
        if parts.hostname == 'open.spotify.com' and re.fullmatch(r'/track/[A-Za-z0-9]{22}', parts.path):
            return 'https://open.spotify.com'+parts.path
        web_lyrics.validate_url(value)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))
    except ValueError:return ''


def _library(directory):
    directory = Path(directory)
    def stamp(path):
        try:return path.stat().st_mtime_ns
        except OSError:return 0
    key = str(directory.absolute())
    version = (stamp(directory/'library.json'), stamp(directory/'lyric_annotations'))
    cached = _local_cache.get(key)
    if cached is None or cached[0] != version:
        store = Store(directory)
        annotations = LyricAnnotations(store)
        songs = store.library()
        # Cache optional annotation reads once per library generation.
        displayed = {song.id: annotations.display_lines(song) for song in songs}
        cached = (version, songs, displayed)
        _local_cache[key] = cached
    return cached[1], cached[2]


def _saved_annotations(directory, playback, lines):
    songs, displayed = _library(directory)
    extras = match_saved_annotations(playback, lines, songs, lambda song: displayed[song.id])
    return tuple(Line(row.original, extra.reading, extra.korean) if extra else row
                 for row, extra in zip(lines, extras))


def _playlist_annotations(directory, title, lines):
    from playlist_lyrics_store import PlaylistLyricsStore
    key = str(Path(directory).absolute())
    store = _store_cache.setdefault(key, PlaylistLyricsStore(directory))
    try:version = store.index_path.stat().st_mtime_ns
    except FileNotFoundError:return []
    cached = _annotation_indexes.get(key)
    if cached is None or cached[0] != version:
        titles = {}
        for track in store.metadata():
            titles.setdefault(normalized(recording_base(track['title'])), []).append(track['id'])
        cached = (version, titles)
        _annotation_indexes[key] = cached
    # Only metadata is cached; a completed translation can replace a file
    # without changing the metadata index. Read those few title candidates.
    candidates = (store.load(identifier) for identifier in cached[1].get(normalized(recording_base(title)), ()))
    return [tuple(value['lines']) for value in candidates if value and _whole(lines, value['lines'])]


def _browser(base, directory, playback, lines, cancel, deadline):
    """One isolated hidden reader, validated by the unchanged ReaderBridge."""
    from internal_reader import BrowserJob, available
    global _browser_cooldown
    if not available(base):raise _Deferred('browser_unavailable')
    remaining = _browser_cooldown-time.monotonic()
    if remaining > 0:raise _Deferred('deferred_cooldown', math.ceil(remaining))
    _check(cancel, deadline)
    received, events = queue.Queue(), queue.Queue()
    bridge = ReaderBridge(messages=received)
    bridge.set_backend('internal')
    bridge.observe(playback)
    bridge.set_reference(playback.key, [row.original for row in lines], complete=True)
    state = bridge.snapshot()
    job = BrowserJob(base, state, events, visible=False,
                     profile=Path(directory)/'playlist_lyrics'/'browser_profile')
    limit = min(deadline, time.monotonic()+BROWSER_SECONDS)
    try:
        while time.monotonic() < limit:
            _check(cancel, deadline)
            try:kind, payload = events.get(timeout=min(.25, max(.01, limit-time.monotonic())))
            except queue.Empty:continue
            if kind != 'internal_reader' or payload[0] is not job:continue
            event = payload[1]
            if event.get('kind') == 'lyrics':
                value = dict(event.get('value', {}))
                value.update(user_selected=False, token=state['token'], track=state['track'])
                bridge.observe(playback)
                if bridge.receive(value) != 'queued':raise ValueError('Rejected reader delivery')
                while not received.empty():
                    name, result = received.get_nowait()
                    if name == 'reader_lyrics':
                        rows = _decode_rows(result[1])
                        if lines and not _whole(lines, rows):raise ValueError('Different whole lyric')
                        addresses = (value.get('url', ''), *bridge.receipt.get('translation_sources', ()))
                        return rows, list(dict.fromkeys(safe for address in addresses if (safe := _safe_source(address))))
                raise ValueError('Missing reader delivery')
            if event.get('kind') in ('blocked', 'error', 'choices', 'finished'):
                state = {'blocked': 'blocked', 'choices': 'needs_document_selection'}.get(event['kind'], 'browser_error')
                details = {}
                if event.get('error_code') in ('timeout', 'script', 'no_table'):
                    details['error_code'] = event['error_code']
                    if event['error_code'] == 'no_table':state = 'no_verified_document'
                if event.get('stage') in ('startup', 'navigation', 'evaluation'):
                    details['stage'] = event['stage']
                message = event.get('text')
                if isinstance(message, str) and message.startswith(('내부 브라우저 ', '일치하는 가사 표')):
                    details['detail'] = message[:300]
                # A particular document's absence/ambiguity does not establish
                # that the browser or the entire source is unavailable.
                if state in ('no_verified_document', 'needs_document_selection'):
                    raise _Deferred(state, details=details)
                _browser_cooldown = time.monotonic()+1800
                raise _Deferred(state, 1800, details)
        _browser_cooldown = time.monotonic()+1800
        raise _Deferred('browser_timeout', 1800)
    finally:
        job.close()


def complete_track(base, directory, track, existing=None, cancel=None):
    """Return rows/direct/provenance and honest missing/deferred source states.

    ``track`` uses public Spotify metadata (id/title/artists/album/duration).
    ``existing`` may be a provider result or a validated cache/store result.
    No caller-owned object is mutated. A throttle raises SourceRateLimited;
    cancellation raises an exception with code='cancelled'. Other misses are
    recorded, and never mean a song is globally unavailable.
    """
    cancel = cancel or threading.Event()
    deadline = time.monotonic()+TRACK_SECONDS
    _check(cancel, deadline)
    artists = track.get('artists') or [track.get('artist', '')]
    if (not isinstance(artists, list) or not artists or any(not isinstance(name, str) or not name.strip() for name in artists)
            or not re.fullmatch(r'[A-Za-z0-9]{22}', track.get('id', ''))
            or not isinstance(track.get('title'), str) or not track['title'].strip()
            or not isinstance(track.get('album', ''), str)
            or isinstance(track.get('duration'), bool) or not isinstance(track.get('duration'), (int, float))
            or not math.isfinite(track['duration']) or track['duration'] <= 0):
        raise ValueError('Invalid playlist recording metadata')
    playback = Playback(title=track['title'], artist=', '.join(artists),
                        album=track.get('album', ''), duration=track['duration'])
    lines, direct, sources, attempts = (), None, [], []

    def add_sources(values):
        for value in values:
            safe = _safe_source(value)
            if safe and safe not in sources and len(sources) < 24:sources.append(safe)

    def attempt(source, call):
        entry = {'source': source}
        attempts.append(entry)
        try:
            _check(cancel, deadline)
            value = call()
            entry['state'] = 'received' if value is not None else 'no_verified_result'
            return value, entry
        except (SourceRateLimited, _Cancelled):raise
        except _Deferred as error:
            entry.update(state=error.state, **error.details)
            if error.retry_after:entry['retry_after'] = error.retry_after
        except (OSError, ValueError, TypeError, KeyError, HTTPException) as error:
            http = _http_error(error)
            entry['state'] = 'blocked' if http and http.code in (401, 403) else 'request_failed' if isinstance(error, (OSError, HTTPException)) or http else 'invalid_or_unmatched'
            if http:entry['http_status'] = http.code
        return None, entry

    if existing:
        try:
            value = existing.get('direct') if 'direct' in existing else existing if 'source' in existing else None
            direct = _direct(value, track)
            lines = _decode_rows(existing['lines'])
            if value and value.get('source') == 'Spotify' and value.get('provider_id') != track['id']:
                lines, direct = (), None
            elif direct:
                # Keep Spotify's source segmentation and every onset intact.
                if direct['source'] == 'Spotify':
                    prior = lines
                    lines = direct['lines']
                    if _whole(lines, prior):lines, _ = merge_verified_annotations(lines, [prior])
                elif not _whole(lines, direct['lines']):direct = None
            if lines:add_sources(existing.get('sources', ('existing_local_cache',)))
            if direct:add_sources((direct['source'],))
        except (ValueError, TypeError, KeyError):
            lines, direct = (), None
            attempts.append({'source': 'existing', 'state': 'invalid_or_unmatched'})

    def supplement(supplied, provenance):
        nonlocal lines
        if not supplied:return False
        supplied = _decode_rows(supplied)
        if not lines:
            lines = supplied
            add_sources(provenance)
            return True
        if not _whole(lines, supplied):return False
        lines, contributors = merge_verified_annotations(lines, [supplied])
        if contributors:add_sources(provenance)
        return True

    def accept(value, entry, timing=True):
        nonlocal direct
        if not value:return
        supplied = _decode_rows(value['lines'])
        if not supplement(supplied, (value['source'],)):
            entry['state'] = 'whole_lyrics_mismatch'
            return
        candidate = _direct(value, track) if timing else None
        if candidate and (direct is None or direct['source'] != 'Spotify'):
            before = sum(stamp is not None for stamp in _timed(direct, lines))
            after = sum(stamp is not None for stamp in _timed(candidate, lines))
            if after > before:direct = candidate
        elif candidate and direct['source'] == 'Spotify' and candidate['source'] != 'Spotify':
            # Preserve the exact Spotify object and onsets. A verified fallback
            # may supply only missing shared boundaries in mapped_times.
            enriched = {**direct, 'fallback': candidate}
            if sum(t is not None for t in _timed(enriched, lines)) > sum(t is not None for t in _timed(direct, lines)):
                direct = enriched
        if direct:add_sources((direct['source'],))
        entry.update(state='verified', lines=len(supplied), translated=sum(bool(row.korean.strip()) for row in supplied))

    def local_annotations():
        if not lines:return
        value, entry = attempt('saved_library', lambda: _saved_annotations(directory, playback, lines))
        if value and supplement(value, ('saved_library',)):entry['state'] = 'verified'
        values, entry = attempt('playlist_lyrics', lambda: _playlist_annotations(directory, playback.title, lines))
        if values:
            approved = [rows for rows in values if _whole(lines, rows)]
            merged, contributors = merge_verified_annotations(lines, approved)
            if contributors:supplement(merged, ('playlist_lyrics',));entry['state'] = 'verified'

    # A local recording can bootstrap a reference only on complete metadata.
    if not lines:
        def local_originals():
            songs, displayed = _library(directory)
            identity = (normalized(playback.title), _artist_identity(playback.artist), normalized(playback.album))
            if not all(identity):return None
            matches = [song for song in songs if (normalized(song.title), _artist_identity(song.artist), normalized(song.album)) == identity]
            if len(matches) == 1:return displayed[matches[0].id]
            return None
        value, entry = attempt('saved_library_originals', local_originals)
        if value:supplement(value, ('saved_library',))
    local_annotations()

    if len(_timed(direct, lines)) == 0 or any(stamp is None for stamp in _timed(direct, lines)):
        value, entry = attempt('LRCLIB', lambda: public_lyrics.fetch_public_lyrics(playback,
            fetch=lambda params: _request('LRCLIB', public_lyrics._request, cancel, deadline, params)))
        accept(value, entry)
        local_annotations()

    def need_translation():return not lines or any(not row.korean.strip() for row in lines)
    def need_timing():return not lines or any(stamp is None for stamp in _timed(direct, lines))

    # ALSong serves both native timing and whole-document translated annotations.
    if lines and (need_translation() or need_timing()):
        value, entry = attempt('ALSong', lambda: alsong_provider.fetch_alsong(playback, reference=lines,
            fetch=lambda *args: _request('ALSong', alsong_provider._request, cancel, deadline, *args)))
        accept(value, entry)

    blocked_http = False
    if need_translation():
        title = recording_base(playback.title)
        urls = [web_lyrics.namu_url(title)]
        if re.search('sidem', playback.album, re.I):urls.insert(0, web_lyrics.sidem_url(title))
        for url in urls:
            if not need_translation():break
            source = web_lyrics.source_id(url)
            value, entry = attempt(source, lambda: _request(source, web_lyrics.fetch_lyrics, cancel, deadline, title, url, None, lines))
            # An HTTP transport's cooldown does not establish that the normal
            # browser path is blocked. _browser enforces its own cooldown.
            blocked_http |= entry.get('state') in ('blocked', 'deferred_cooldown')
            if value:
                if supplement(value[0], (value[1].get('url', ''),)):entry['state'] = 'verified'
                else:entry['state'] = 'whole_lyrics_mismatch'
        if need_translation() and blocked_http:
            value, entry = attempt('wiki_browser', lambda: _browser(base, directory, playback, lines, cancel, deadline))
            if value:
                if supplement(value[0], value[1]):entry['state'] = 'verified'
                else:entry['state'] = 'whole_lyrics_mismatch'
        local_annotations()

    # Wiki originals can be a reference, but cannot supply any native onset.
    if need_timing() and lines:
        if not any(item['source'] == 'ALSong' for item in attempts):
            value, entry = attempt('ALSong', lambda: alsong_provider.fetch_alsong(playback, reference=lines,
                fetch=lambda *args: _request('ALSong', alsong_provider._request, cancel, deadline, *args)))
            accept(value, entry)
    if need_timing() and lines:
        value, entry = attempt('LyricsPlus', lambda: lyricsplus_provider.fetch_lyricsplus(playback, reference=lines,
            fetch=lambda params: _request('LyricsPlus', lyricsplus_provider._request, cancel, deadline, params)))
        accept(value, entry)
    times = _timed(direct, lines)
    timed = sum(stamp is not None for stamp in times)
    translated = sum(bool(row.korean.strip()) for row in lines)
    missing = ([] if lines else ['originals'])
    if not lines or timed != len(lines):missing.append('timing')
    if not lines or translated != len(lines):missing.append('korean_translation')
    return {'lines': tuple(lines), 'direct': direct, 'sources': sources, 'attempts': attempts,
            'complete': not missing, 'missing': missing,
            'counts': {'lines': len(lines), 'timed': timed, 'translated': translated}}
