"""Collect independent lyric documents for an ID-bound playlist track."""
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
import imas_lyrics
import imas_timetag
import lyricsplus_provider
import public_lyrics
import web_lyrics
from lyric_annotations import LyricAnnotations
from lyric_cache import _provider, _rows
from lyric_match import match_lyrics
from lyric_merge import has_ordered_overlap, make_snapshot, merge_lyrics, snapshot_from_direct
from lyrics import is_cinderella_no1, normalized
from media import Playback
from reader_bridge import ReaderBridge, recording_base
from saved_annotations import _artist_identity, match_saved_annotations
from storage import Store


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
                elif (source in ('LRCLIB', 'ALSong', 'LyricsPlus', 'namu', 'sidem', 'imas_sidem',
                                 'imas_gakumas', 'imas_shiny', 'imas_timetag')
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


def _request(source, call, cancel, deadline, *args, **kwargs):
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
            return call(*args, **kwargs)
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
    return result


def _safe_source(value):
    if not isinstance(value, str):return ''
    if value in _PROVIDERS or value in ('existing_local_cache', 'saved_library', 'playlist_lyrics'):
        return value
    try:return imas_timetag.validate_url(value)
    except ValueError:pass
    try:
        parts = urlsplit(value)
        if parts.username or parts.password:return ''
        if parts.hostname == 'open.spotify.com' and re.fullmatch(r'/track/[A-Za-z0-9]{22}', parts.path):
            return 'https://open.spotify.com'+parts.path
        try:web_lyrics.validate_url(value)
        except ValueError:imas_lyrics.validate_url(value)
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
    return [(song.id, displayed[song.id]) for song in songs
            if any(match_saved_annotations(playback, lines, (song,), lambda song: displayed[song.id]))]


def _playlist_annotations(directory, title, lines, track_id):
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
    candidates = (store.load(identifier) for identifier in cached[1].get(normalized(recording_base(title)), ())
                  if identifier != track_id)
    return [(value['track']['id'], value['lines']) for value in candidates if value and _whole(lines, value['lines'])]


def _browser(base, directory, playback, lines, cancel, deadline, preserve_sources=False):
    """One isolated hidden reader, validated by the unchanged ReaderBridge."""
    from internal_reader import BrowserJob, available
    global _browser_cooldown
    if not available(base):raise _Deferred('browser_unavailable')
    remaining = _browser_cooldown-time.monotonic()
    if remaining > 0:raise _Deferred('deferred_cooldown', math.ceil(remaining))
    _check(cancel, deadline)
    received, events = queue.Queue(), queue.Queue()
    bridge = ReaderBridge(messages=received, preserve_sources=preserve_sources)
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
                        if not preserve_sources and lines and not _whole(lines, rows):
                            raise ValueError('Different whole lyric')
                        addresses = (value.get('url', ''), *bridge.receipt.get('translation_sources', ()))
                        provenance = list(dict.fromkeys(safe for address in addresses if (safe := _safe_source(address))))
                        if preserve_sources:
                            documents = [(document_rows, safe, title) for url, title, document_rows in result[5]
                                         if (safe := _safe_source(url))]
                            return rows, provenance, documents
                        return rows, provenance
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
    lines, sources, attempts, snapshots = (), [], [], []
    merged = merge_lyrics(playback, (), track_id=track['id'])
    initial_snapshots = ()

    def add_sources(values):
        for value in values:
            safe = _safe_source(value)
            if safe and safe not in sources and len(sources) < 24:sources.append(safe)

    def result():
        timed = sum(stamp is not None for stamp in merged['times'])
        translated = sum(bool(row.korean.strip()) for row in lines)
        missing = [] if lines else ['originals']
        if not lines or timed != len(lines):missing.append('timing')
        if not lines or translated != len(lines):missing.append('korean_translation')
        return {**merged, 'sources': sources, 'attempts': attempts, 'complete': not missing, 'missing': missing,
                'counts': {'lines': len(lines), 'timed': timed, 'translated': translated}}

    def attempt(source, call):
        entry = {'source': source}
        attempts.append(entry)
        try:
            _check(cancel, deadline)
            value = call()
            entry['state'] = 'received' if value is not None else 'no_verified_result'
            return value, entry
        except SourceRateLimited as error:
            if lines and tuple(snapshots) != initial_snapshots:error.partial = result()
            raise
        except _Cancelled:raise
        except _Deferred as error:
            entry.update(state=error.state, **error.details)
            if error.retry_after:entry['retry_after'] = error.retry_after
        except (OSError, ValueError, TypeError, KeyError, HTTPException) as error:
            http = _http_error(error)
            entry['state'] = 'blocked' if http and http.code in (401, 403) else 'request_failed' if isinstance(error, (OSError, HTTPException)) or http else 'invalid_or_unmatched'
            if http:entry['http_status'] = http.code
        return None, entry

    def recompute():
        nonlocal lines, merged
        merged = merge_lyrics(playback, snapshots, track_id=track['id'])
        lines = merged['lines']

    def register(snapshot):
        if snapshot not in snapshots:
            snapshots.append(snapshot)
            recompute()

    def supplement(source, supplied, provenance, **metadata):
        if not supplied:return False
        try:
            supplied = _decode_rows(supplied)
            snapshot = make_snapshot(source, supplied, **metadata)
        except (ValueError, TypeError, KeyError):
            return False
        previous = lines
        register(snapshot)
        if previous == lines and not _whole(lines, supplied):return False
        if previous != lines:add_sources(provenance)
        return True

    if existing:
        try:
            value = existing.get('direct') if 'direct' in existing else existing if 'source' in existing else None
            if value and value.get('source') == 'Spotify' and value.get('provider_id') != track['id']:
                raise ValueError('Wrong Spotify recording')
            saved = _decode_rows(existing['lines'])
            snapshots.extend(existing.get('snapshots', ()))
            if not snapshots:
                direct = _direct(value, track)
                if direct:
                    snapshots.append(snapshot_from_direct(direct))
                    if direct.get('fallback'):snapshots.append(snapshot_from_direct(direct['fallback']))
                    add_sources((direct['source'],))
                if not direct or saved != direct['lines']:
                    source = 'manual' if 'reviewed_translation' in existing.get('sources', ()) else 'local'
                    snapshots.append(make_snapshot(source, saved, track_id=track['id'], recording_verified=True,
                                                   annotation_only=bool(direct)))
            recompute()
            if lines:add_sources(existing.get('sources', ('existing_local_cache',)))
        except (ValueError, TypeError, KeyError):
            snapshots.clear()
            recompute()
            attempts.append({'source': 'existing', 'state': 'invalid_or_unmatched'})
    initial_snapshots = tuple(snapshots)
    # A complete legacy clock describes only the rows it already has. Check
    # alternate originals once during an active collection, then keep the
    # independent documents so later collections can skip the same check.
    review_legacy = bool(existing and not existing.get('snapshots') and lines and
                         all(row.korean.strip() for row in lines) and
                         all(stamp is not None for stamp in merged['times']))

    def accept(value, entry):
        if not value:return
        try:
            supplied = _decode_rows(value['lines'])
            candidate = _direct(value, track)
            snapshot = snapshot_from_direct({**value, **candidate}) if candidate else None
        except (ValueError, TypeError, KeyError):
            entry['state'] = 'invalid_or_unmatched'
            return
        before_times = merged['times']
        if snapshot:
            previous = lines
            register(snapshot)
            accepted = previous != lines or _whole(lines, supplied) or before_times != merged['times']
        elif value.get('annotation_only') and value.get('source') in _PROVIDERS:
            accepted = supplement(value['source'], supplied, (value['source'],), provider_id=value.get('provider_id'),
                                  annotation_only=True)
        else:
            accepted = False
        if accepted:
            add_sources((value['source'],))
            entry.update(state='verified', lines=len(supplied), translated=sum(bool(row.korean.strip()) for row in supplied))
        else:
            entry['state'] = 'whole_lyrics_mismatch'

    def local_annotations():
        if not lines:return
        values, entry = attempt('saved_library', lambda: _saved_annotations(directory, playback, lines))
        for identifier, rows in values or ():
            if supplement('saved_library', rows, ('saved_library',), provider_id=identifier, annotation_only=True):
                entry['state'] = 'verified'
        values, entry = attempt('playlist_lyrics', lambda: _playlist_annotations(directory, playback.title, lines, track['id']))
        for identifier, rows in values or ():
            if supplement('playlist_lyrics', rows, ('playlist_lyrics',), provider_id=identifier, annotation_only=True):
                entry['state'] = 'verified'

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
        if value:supplement('local', value, ('saved_library',), recording_verified=True, track_id=track['id'])
    local_annotations()

    def need_translation():return not lines or any(not row.korean.strip() for row in lines)
    def need_timing():return not lines or any(stamp is None for stamp in merged['times'])

    def have_document(source):
        return any(snapshot['source'] == source and not snapshot['annotation_only'] for snapshot in snapshots)

    def try_lrclib():
        value, entry = attempt('LRCLIB', lambda: public_lyrics.fetch_public_lyrics(playback,
            fetch=lambda params: _request('LRCLIB', public_lyrics._request, cancel, deadline, params),
            merge_metadata=True))
        accept(value, entry)

    if need_timing() and not have_document('LRCLIB'):
        try_lrclib()
        local_annotations()

    if need_timing() and not have_document('imas_timetag') and imas_timetag.supports(
            playback.title, playback.artist, playback.album, playback.duration, track['id']):
        value, entry = attempt('imas_timetag', lambda: imas_timetag.find_candidate(
            playback.title, playback.artist, playback.album, playback.duration, track_id=track['id'],
            fetch=lambda url: _request('imas_timetag', imas_timetag.fetch_text, cancel, deadline, url)))
        if value:
            if (not lines or _whole(lines, value['lines'])) and supplement(
                    value['source'], value['lines'], (value['url'], value['lyrics_url']), times=value['times'],
                    track_id=track['id'], title=playback.title, artist=playback.artist, album=playback.album,
                    duration=playback.duration, recording_verified=True, provider_id=value['url']):
                add_sources((value['url'], value['lyrics_url']))
                entry['state'] = 'verified'
            else:
                entry['state'] = 'whole_lyrics_mismatch'

    # A timed provider can supply rows that a wiki later annotates. Keep the
    # discovery decision from before the timed provider arrives.
    find_wiki = review_legacy or need_translation() or need_timing()

    # ALSong serves both native timing and whole-document translated annotations.
    if lines and not have_document('ALSong') and (review_legacy or need_translation() or need_timing()):
        value, entry = attempt('ALSong', lambda: alsong_provider.fetch_alsong(playback, reference=lines,
            fetch=lambda *args: _request('ALSong', alsong_provider._request, cancel, deadline, *args),
            merge_partial=True))
        accept(value, entry)

    blocked_http = False
    if find_wiki:
        title = (playback.title if is_cinderella_no1(playback.title)
                 else recording_base(playback.title, preserve_spelling=True))
        urls = [web_lyrics.namu_url(title)]
        if re.search('sidem', playback.album, re.I):urls.insert(0, web_lyrics.sidem_url(title))
        for url in urls:
            source = web_lyrics.source_id(url)
            if have_document(source):continue
            value, entry = attempt(source, lambda: _request(source, web_lyrics.fetch_lyrics, cancel, deadline,
                                                           title, url, None, lines, merge_partial=True))
            # An HTTP transport's cooldown does not establish that the normal
            # browser path is blocked. _browser enforces its own cooldown.
            blocked_http |= entry.get('state') in ('blocked', 'deferred_cooldown')
            if value:
                address = _safe_source(value[1].get('url', ''))
                if supplement(source, value[0], (address,), provider_id=address[:256],
                              title=value[1].get('title', title)):entry['state'] = 'verified'
                else:entry['state'] = 'whole_lyrics_mismatch'
        series = imas_lyrics.series_for(playback.album, playback.artist)
        source = imas_lyrics.source_for(series)
        if not lines and source:
            value, entry = attempt(source, lambda: imas_lyrics.find_candidate(playback.title, series,
                fetch=lambda url: _request(source, imas_lyrics.fetch_html, cancel, deadline, url)))
            if value:
                address = _safe_source(value['url'])
                if address and supplement(source, value['lines'], (address,), provider_id=address[:256],
                                          title=playback.title):entry['state'] = 'verified'
                else:entry['state'] = 'invalid_or_unmatched'
        if need_translation() and blocked_http:
            value, entry = attempt('wiki_browser', lambda: _browser(
                base, directory, playback, lines, cancel, deadline, preserve_sources=True))
            if value:
                documents = value[2] if len(value) > 2 else [(value[0], value[1][0], title)]
                accepted = False
                for rows, address, name in documents:
                    if supplement(web_lyrics.source_id(address), rows, (address,),
                                  provider_id=address[:256], title=name):accepted = True
                entry['state'] = 'verified' if accepted else 'whole_lyrics_mismatch'
        local_annotations()

    def wiki_has_additional_text():
        originals = tuple(line.original for line in lines)
        return any(snapshot['source'] in ('namu', 'sidem') and len(snapshot['lines']) > len(lines)
                   and not _whole(lines, snapshot['lines'])
                   and has_ordered_overlap(originals, tuple(line.original for line in snapshot['lines']))
                   for snapshot in snapshots)

    # An observed wiki gap can warrant a clock lookup even when every
    # currently displayed Spotify row already has an onset.
    if (wiki_has_additional_text() and not have_document('LRCLIB')
            and not any(item['source'] == 'LRCLIB' for item in attempts)):
        try_lrclib()

    # Wiki originals can be a reference, but cannot supply any native onset.
    if need_timing() and lines and not have_document('ALSong'):
        if not any(item['source'] == 'ALSong' for item in attempts):
            value, entry = attempt('ALSong', lambda: alsong_provider.fetch_alsong(playback, reference=lines,
                fetch=lambda *args: _request('ALSong', alsong_provider._request, cancel, deadline, *args),
                merge_partial=True))
            accept(value, entry)
    if lines and not have_document('LyricsPlus') and (need_timing() or wiki_has_additional_text()):
        value, entry = attempt('LyricsPlus', lambda: lyricsplus_provider.fetch_lyricsplus(playback, reference=lines,
            fetch=lambda params: _request('LyricsPlus', lyricsplus_provider._request, cancel, deadline, params),
            merge_partial=True))
        accept(value, entry)
    if cancel.is_set():raise _Cancelled()
    return result()
