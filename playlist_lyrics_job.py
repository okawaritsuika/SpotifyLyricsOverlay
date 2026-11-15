"""Resumable playlist lyric collection; the app thread owns every saved result."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import queue
import os
import math
import re
import secrets
import stat
import threading
import time

from lyrics import Line
from lyric_cache import MAX_ENTRY_BYTES, _rows
from media import Playback
from storage import atomic_json, read_json, validate_times
from playlist_lyrics_store import PlaylistLyricsStore, _safe, _track


_TRANSIENT_STATES = {'deferred_cooldown', 'deferred_deadline', 'browser_timeout',
                     'browser_error', 'browser_unavailable', 'request_failed', 'blocked'}


def _row_diagnostics(rows):
    values = rows if isinstance(rows, (list, tuple)) else ()
    texts = [row.get('text') for row in values if isinstance(row, dict)]
    return {'stage': 'rows', 'rows': len(values),
            'blankRows': sum(isinstance(text, str) and not text.strip() for text in texts),
            'symbolRows': sum(isinstance(text, str) and bool(text.strip())
                              and not any(char.isalnum() for char in text) for text in texts),
            'timedCount': sum(isinstance(row, dict) and row.get('time') is not None for row in values)}


def _preserve_rejected_candidate(directory, track, lines, times):
    """Keep only validated public lyric fields, never the worker event itself."""
    try:
        track = _track(track)
        if not track:raise ValueError('Invalid candidate track')
        lines = _rows(lines)
        validate_times(times, len(lines))
        if any(stamp is not None and stamp > track['duration']+2 for stamp in times):raise ValueError()
        value = {'schema_version': 1, 'track': track, 'source': 'Spotify', 'provider_id': track['id'],
                 'lines': [{'original': row.original, 'reading': '', 'korean': ''} for row in lines],
                 'times': list(times), 'reason': 'store_rejected',
                 'rejected_at': datetime.now(timezone.utc).isoformat()}
        if len(json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8')) > MAX_ENTRY_BYTES:
            raise ValueError('Candidate too large')
        path = Path(directory)/'playlist_lyrics'/'rejected_candidates'/(track['id']+'.json')
        if not _safe(path):return {'candidate_saved': False, 'candidate_error': 'unsafe_path'}
        atomic_json(path, value)
        return {'candidate_saved': True}
    except OSError:
        return {'candidate_saved': False, 'candidate_error': 'write_failed'}
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        return {'candidate_saved': False, 'candidate_error': 'invalid_candidate'}


def coverage(value):
    lines = value.get('lines', ()) if value else ()
    direct = value.get('direct') if value else None
    translated = sum(bool(row.korean.strip()) for row in lines)
    times = []
    if direct:
        from background_lyrics import mapped_times
        times = mapped_times(direct, lines)
    timed = sum(stamp is not None for stamp in times)
    return {'lines': len(lines), 'translated': translated, 'timed': timed,
            'complete': bool(lines and translated == len(lines) and timed == len(lines))}


def _clock_signature(direct):
    if not direct:return None
    return (direct['source'], direct['provider_id'], tuple(line.original for line in direct['lines']),
            direct['times'], _clock_signature(direct.get('fallback')))


class PlaylistLyricsJob:
    def __init__(self, app):
        self.app = app
        self.directory = Path(app.store.directory)
        self.path = self.directory / 'playlist_lyrics_job.json'
        self.value = read_json(self.path, {})
        from playlist_lyrics_sources import restore_cooldowns
        restore_cooldowns(self.value)
        self.enabled = self.value.get('enabled') is True
        self.store = PlaylistLyricsStore(self.directory)
        self.manifest_path = self.directory / 'playlist_lyrics' / 'manifest.json'
        self.manifest = read_json(self.manifest_path, {}) if self.enabled or self.value.get('phase') == 'review' else {}
        self.reviewed_next_at = 0
        self.reviewed_seen = {}
        self.events = queue.Queue()
        self.active = None
        self.enrichment_active = None
        self.cancel = threading.Event()
        self.closed = False
        retry_at=self.value.get('retry_at',0)
        remaining=(max(0,retry_at-time.time())
                   if type(retry_at) in (int,float) and math.isfinite(retry_at) else 0)
        self.next_at=time.monotonic()+remaining if remaining else 0
        retry_at=self.value.get('enrichment_retry_at',0)
        remaining=(max(0,retry_at-time.time())
                   if type(retry_at) in (int,float) and math.isfinite(retry_at) else 0)
        self.enrichment_next_at=time.monotonic()+remaining if remaining else 0
        self.enrichment_status = self.value.get('enrichment_status', '')
        self.last_tick = 0
        self.status = '플레이리스트 가사 준비 중' if self.enabled else ''
        self.tracks = None
        self.awake = False

    def keep_awake(self, enabled):
        if os.name != 'nt' or enabled == self.awake:return
        import ctypes
        if ctypes.windll.kernel32.SetThreadExecutionState(0x80000001 if enabled else 0x80000000):
            self.awake = enabled

    @property
    def owns_spotify(self):
        return self.enabled and self.value.get('phase', 'playlist') in ('playlist', 'spotify')

    def save(self):
        self.value.update(status=self.status, enrichment_status=self.enrichment_status,
                          updated_at=datetime.now(timezone.utc).isoformat())
        atomic_json(self.path, self.value)

    def _error(self, code, retry_after=0, enrichment=False):
        request = self.enrichment_active if enrichment else self.active
        if enrichment:self.enrichment_active = None
        else:self.active = None
        delay = max(1, retry_after or 300) if code == 'rate_limited' else 10
        if code == 'login_required':
            delay = 60
            if request and request.get('job'):request['job'].send({'action': 'refresh'})
        if enrichment:
            self.enrichment_next_at = time.monotonic() + delay
            self.value.update(enrichment_retry_at=time.time()+delay,enrichment_retry_after_seconds=delay)
            self.enrichment_status = f'번역·싱크 보완 대기 · {code} · {delay:.0f}초'
        else:
            self.next_at = time.monotonic() + delay
            self.value.update(retry_at=time.time()+delay,retry_after_seconds=delay)
            self.status = f'플레이리스트 가사 대기 · {code} · {delay:.0f}초'
        if request and code not in ('rate_limited', 'busy', 'interrupted', 'cancelled', 'login_required'):
            key = request.get('track', {}).get('id', 'playlist')
            errors = self.value.setdefault('errors', {})
            previous = errors.get(key, {})
            count=previous.get('attempts',0) if previous.get('phase')==request['phase'] else 0
            errors[key] = {'code': code, 'phase':request['phase'],'attempts': count + 1}
            if key != 'playlist' and errors[key]['attempts'] >= 3:
                self.value.setdefault(request['phase'], {})[key] = {'status': 'error', 'code': code}
                if enrichment:self.value.setdefault('enrichment_pending', {}).pop(key, None)
        self.save()

    def receive(self, sender, event):
        request = self.active
        if (not request or request.get('job') is not sender
                or event.get('request_id') != request.get('request_id')):return False
        kind = event.get('kind')
        if kind == 'catalog_playlist_diagnostic':
            value=event.get('value',{})
            if value.get('phase')=='public_track_shape':
                self.value['playlist_public_shape']=value
                self.save()
            return True
        if kind in ('catalog_playlist_progress', 'catalog_playlist_result') and request['phase'] == 'playlist':
            if event.get('code'):
                self._error(event['code'], event.get('retry_after', 0));return True
            entries = event.get('entries')
            if not isinstance(entries, list):
                self._error('missing_playlist_entries');return True
            old = self.manifest.get('entries', [])
            start = event.get('start')
            offset, total = event.get('offset'), event.get('total')
            if (type(start) is not int or start != len(old) or type(total) is not int
                    or type(offset) is not int or offset != start + len(entries)
                    or any(row.get('index') != start+i for i, row in enumerate(entries))
                    or self.manifest.get('total', total) != total):
                self._error('playlist_page_mismatch');return True
            self.manifest.update(schema_version=1, playlist_id=self.value['playlist_id'], total=total,
                                 entries=old+entries, complete=event.get('complete') is True and offset == total)
            atomic_json(self.manifest_path, self.manifest)
            self.value['playlist_offset'] = offset
            self.value['playlist_total'] = total
            self.value.pop('retry_at',None)
            request['started'] = time.monotonic()
            self.status = f'대상 목록 보관 {offset:,}/{total:,}개'
            if self.manifest['complete']:
                tracks={row['track_id']:row['track'] for row in self.manifest['entries']
                        if row.get('status')=='track' and isinstance(row.get('track'),dict)}
                self.app.catalog_sync.store_tracks(list(tracks.values()),
                    'https://open.spotify.com/playlist/'+self.value['playlist_id'])
                self.value['phase'] = 'spotify'
                self.active = None
                self.next_at = time.monotonic() + 1
            elif kind == 'catalog_playlist_result':self.active = None
            self.save()
            return True
        if kind == 'batch_lyrics_result' and request['phase'] == 'spotify':
            track = request['track']
            if event.get('provider_id') != track['id']:
                self._error('track_mismatch');return True
            code = event.get('code')
            if code and code != 'not_found':
                self._error(code, event.get('retry_after', 0));return True
            if not code:
                diagnostic = _row_diagnostics(event.get('lines'))
                try:
                    rows = event['lines']
                    if (not isinstance(rows, (list, tuple))
                            or any(not isinstance(row, dict) or not isinstance(row.get('text'), str) for row in rows)):
                        raise ValueError()
                    lines = tuple(Line(row['text'], '', '') for row in rows)
                    times = [row.get('time') for row in rows]
                    diagnostic['stage'] = 'times'
                    validate_times(times, len(lines))
                    if not lines or any(t is not None and t > track['duration']+2 for t in times):raise ValueError()
                    direct = {'source': 'Spotify', 'provider_id': track['id'], 'lines': lines, 'times': times}
                    diagnostic['stage'] = 'store'
                    if not self.store.save(track, lines, direct, sources=['https://open.spotify.com/track/'+track['id']]):
                        self.value.setdefault('rejected_candidate_diagnostics', {})[track['id']] = (
                            _preserve_rejected_candidate(self.directory, track, lines, times))
                        raise ValueError()
                except (ValueError, TypeError, KeyError):
                    self.value.setdefault('invalid_lyrics_diagnostics', {})[track['id']] = diagnostic
                    self._error('invalid_lyrics');return True
            result = coverage(self.store.load(track['id']))
            self.value.setdefault('spotify', {})[track['id']] = {'status': code or 'received', **result}
            self.value.pop('retry_at',None)
            self.active = None
            self.next_at = time.monotonic() + 3
            self.status = f"Spotify 가사 확인 {len(self.value['spotify']):,}/{len(self._tracks()):,}음원"
            self.save()
            return True
        return False

    def _tracks(self):
        if self.tracks is None:
            self.tracks = {row['track']['id']: row['track'] for row in self.manifest.get('entries', [])
                           if row.get('status') == 'track' and isinstance(row.get('track'), dict)}
        return self.tracks

    def _existing(self, track):
        existing = self.store.load(track['id'])
        if existing:return existing
        for artist in (track['artists'][0], ', '.join(track['artists'])):
            playback = Playback(title=track['title'], artist=artist, album=track['album'], duration=track['duration'])
            cached = self.app.lyric_cache.load(playback)
            provenance = cached.get('merge') if cached else None
            merge = {'snapshots': provenance['snapshots'], 'merge_rows': provenance['rows']} if provenance else {}
            if cached and self.store.save(track, cached['lines'], cached.get('direct'),
                                          sources=['existing_local_cache'], **merge):
                return self.store.load(track['id'])
        return None

    def _tick_reviewed(self, now):
        """Import one atomic local approval on the owning UI thread; never clocks."""
        if now < self.reviewed_next_at:return
        self.reviewed_next_at = now+5
        if self.value.get('phase') not in ('spotify', 'enrichment', 'review') or not self.manifest.get('complete'):return
        directory = self.directory/'playlist_lyrics'/'reviewed'
        if not _safe(directory):return
        try:paths = sorted(directory.glob('*.json'))
        except OSError:return
        for path in paths:
            if not re.fullmatch(r'[A-Za-z0-9]{22}\.json', path.name):continue
            try:
                before = path.lstat()
                signature = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            except OSError:continue
            if self.reviewed_seen.get(path.name) == signature:continue
            identifier = path.stem
            digest = None
            code = 'invalid_file'
            try:
                if not _safe(path) or not stat.S_ISREG(before.st_mode):raise ValueError()
                code = 'file_too_large'
                if before.st_size > MAX_ENTRY_BYTES:raise ValueError()
                code = 'unreadable_file'
                with path.open('rb') as stream:raw = stream.read(MAX_ENTRY_BYTES+1)
                after = path.lstat()
                if signature != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):return
                if len(raw) > MAX_ENTRY_BYTES:raise ValueError()
                digest = hashlib.sha256(raw).hexdigest()
                self.reviewed_seen[path.name] = signature
                previous = self.value.get('reviewed_imports', {}).get(identifier, {})
                if previous.get('sha256') == digest and previous.get('status') == 'applied':continue
                code = 'invalid_json'
                supplied = json.loads(raw)
                code = 'invalid_schema'
                if not isinstance(supplied, dict) or supplied.get('schema_version') != 1:raise ValueError()
                code = 'unknown_track'
                manifest_track = self._tracks().get(identifier)
                if not manifest_track:raise ValueError()
                code = 'missing_stored_lyrics'
                existing = self.store.load(identifier)
                if not existing:raise ValueError()
                code = 'track_mismatch'
                track = supplied.get('track')
                keys = ('id', 'title', 'album', 'duration')
                if (not isinstance(track, dict) or track.get('id') != identifier
                        or type(track.get('duration')) not in (int, float)
                        or any(track.get(key) != existing['track'].get(key)
                               or track.get(key) != manifest_track.get(key) for key in keys)):
                    raise ValueError()
                code = 'invalid_rows'
                rows = _rows(supplied.get('lines'), decoded=True)
                code = 'originals_mismatch'
                if tuple(row.original for row in rows) != tuple(row.original for row in existing['lines']):raise ValueError()
                code = 'save_failed'
                if not self.store.save(existing['track'], rows, existing['direct'], sources=('reviewed_translation',)):
                    raise ValueError()
                code = 'save_verification_failed'
                saved = self.store.load(identifier)
                expected = tuple(Line(old.original,
                                      old.reading if old.reading.strip() else new.reading,
                                      old.korean if old.korean.strip() else new.korean)
                                 for old,new in zip(existing['lines'],rows))
                if (not saved or tuple(saved['lines']) != expected
                        or _clock_signature(saved['direct']) != _clock_signature(existing['direct'])):raise ValueError()
                counted = coverage(saved)
                if counted['complete']:
                    self.value.setdefault('enrichment_pending', {}).pop(identifier, None)
                    self.value.setdefault('enrichment', {})[identifier] = {'status': 'reviewed', **counted}
                    if self.enrichment_active and self.enrichment_active['track']['id'] == identifier:
                        self.enrichment_active['reviewed_complete'] = True
                receipt = {'sha256': digest, 'status': 'applied', 'coverage': counted}
            except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
                self.reviewed_seen[path.name] = signature
                receipt = {'sha256': digest, 'status': 'rejected', 'code': code}
            self.value.setdefault('reviewed_imports', {})[identifier] = receipt
            self.save()
            return

    def _tick_enrichment(self, now):
        while True:
            try:request, result, error = self.events.get_nowait()
            except queue.Empty:break
            if self.enrichment_active is not request:continue
            if request.get('reviewed_complete') and coverage(self.store.load(request['track']['id']))['complete']:
                self.enrichment_active = None
                continue
            track = request['track']
            if result and result.get('lines'):
                merge = {'snapshots': result['snapshots'], 'merge_rows': result['rows']} if result.get('snapshots') else {}
                if not self.store.save(track, result['lines'], result.get('direct'),
                                       sources=result.get('sources', ()), **merge):
                    if not error:
                        self._error('save_failed', enrichment=True);continue
            if error:
                self._error(error[0], error[1], enrichment=True);continue
            counted = coverage(self.store.load(track['id']))
            outcome = {
                'status': 'complete' if counted['complete'] else 'incomplete', **counted,
                'attempts': result.get('attempts', []), 'missing': result.get('missing', [])}
            pending = self.value.setdefault('enrichment_pending', {})
            deferred = [item for item in outcome['attempts'] if item.get('state') in _TRANSIENT_STATES]
            if deferred and not counted['complete']:
                rounds = pending.get(track['id'], {}).get('rounds', 0)
                rounds += any(item['state'] != 'deferred_cooldown' for item in deferred)
                if rounds < 3:
                    delays = [item.get('retry_after') for item in deferred]
                    delay = max([30] + [value for value in delays if type(value) in (int, float)
                                        and math.isfinite(value) and value > 0])
                    pending[track['id']] = {'retry_at': time.time()+delay, 'rounds': rounds,
                                            'attempts': outcome['attempts'], 'missing': outcome['missing']}
                else:
                    pending.pop(track['id'], None)
                    self.value.setdefault('enrichment', {})[track['id']] = {
                        **outcome, 'status': 'error', 'code': 'source_retry_exhausted', 'rounds': rounds}
            else:
                pending.pop(track['id'], None)
                self.value.setdefault('enrichment', {})[track['id']] = outcome
            self.value.pop('enrichment_retry_at',None)
            self.enrichment_active = None
            self.enrichment_next_at = now+1
            self.enrichment_status = f"번역·싱크 보완 {len(self.value.get('enrichment', {})):,}/{len(self._tracks()):,}음원"
            self.save()
        if self.enrichment_active:
            if now-self.enrichment_active['started'] > 240:
                self.enrichment_status = '번역·싱크 제공자 응답 대기'
            return
        if now < self.enrichment_next_at:return
        phase = self.value.get('phase', 'playlist')
        if phase not in ('spotify', 'enrichment'):return
        done = self.value.setdefault('enrichment', {})
        pending = self.value.setdefault('enrichment_pending', {})
        spotify = self.value.get('spotify', {})
        wall_now = time.time()
        scoped = 'priority_track_ids' in self.value
        supplied = self.value.get('priority_track_ids')
        priority_ids = {key for key in supplied if isinstance(key, str)} if isinstance(supplied, list) else set()
        track = existing = None
        for key, candidate in self._tracks().items():
            if (key in done or scoped and key not in priority_ids
                    or pending.get(key, {}).get('retry_at', 0) > wall_now
                    or phase == 'spotify' and key not in spotify):continue
            existing = self.store.load(key) if scoped else self._existing(candidate)
            if scoped and (not existing or not existing.get('lines')):continue
            track = candidate
            break
        if track is None:
            waiting = sum(key in priority_ids and key not in done for key in pending) if scoped else len(pending)
            if waiting:self.enrichment_status = f'번역·싱크 제공자 재시도 대기 · {waiting:,}음원'
            elif scoped:self.enrichment_status = '우선 가사 보완 · 실행 가능한 대상 없음'
            return
        counted = coverage(existing)
        if counted['complete'] and existing.get('snapshots'):
            pending.pop(track['id'], None)
            done[track['id']] = {'status': 'existing', **counted}
            self.save()
            return
        request = {'phase': 'enrichment', 'track': track, 'started': now}
        self.enrichment_active = request
        def enrich():
            try:
                from playlist_lyrics_sources import complete_track
                result = complete_track(Path(__file__).parent, self.directory, track, existing, self.cancel)
                self.events.put((request, result, None))
            except Exception as error:
                self.events.put((request, getattr(error, 'partial', None),
                                 (getattr(error, 'code', type(error).__name__), getattr(error, 'retry_after', 0))))
        threading.Thread(target=enrich, daemon=True, name='playlist-lyric-sources').start()
        self.enrichment_status = f"번역·싱크 보완 · {track['title']}"
        self.save()

    def tick(self):
        self.keep_awake(not self.closed and self.value.get('shutdown_requested') is True)
        if self.closed:return
        now = time.monotonic()
        self._tick_reviewed(now)
        if not self.enabled:return
        if now-self.last_tick < .5:return
        self.last_tick = now
        self._tick_enrichment(now)
        if self.active:
            if self.active.get('job') and self.active['job'] is not self.app.background_lyrics.job:
                self._error('interrupted')
            elif now-self.active['started'] > 90:
                self.active['job'].send({'action': 'catalog_cancel', 'request_id': self.active['request_id']})
                self._error('timeout')
            return
        if now < self.next_at:return
        phase = self.value.get('phase', 'playlist')
        if phase in ('review', 'complete'):
            self.enabled = False
            return
        if phase == 'enrichment':
            self.status = self.enrichment_status
            if not self.enrichment_active and all(key in self.value.get('enrichment', {}) for key in self._tracks()):
                self.finish()
            return
        service = self.app.background_lyrics
        if phase in ('playlist', 'spotify'):
            if not service.job:
                service.connect(visible=False)
                self.next_at = now+15
                self.status = '플레이리스트 가사 · 기존 Spotify 연결 대기'
                return
            if not service.spotify_authenticated:
                self.status = '플레이리스트 가사 · Spotify 인증 대기';return
            if service.spotify_pending or service.metadata_pending:return
            if self.app.catalog_sync.active:return
        if phase == 'playlist':
            playlist = self.value.get('playlist_id', '')
            if not re.fullmatch(r'[A-Za-z0-9]{22}', playlist):
                self.status = '플레이리스트 ID 확인 필요';return
            offset = len(self.manifest.get('entries', []))
            self.active = {'phase': phase, 'job': service.job, 'request_id': secrets.token_hex(16), 'started': now}
            service.job.send({'action': 'catalog_playlist', 'request_id': self.active['request_id'],
                              'playlist_id': playlist, 'offset': offset})
            self.status = f'플레이리스트 목록 {offset:,}개부터 보관'
            return
        tracks = self._tracks()
        done = self.value.setdefault(phase, {})
        track = next((value for key, value in tracks.items() if key not in done), None)
        if track is None:
            self.value['phase'] = 'enrichment'
            self.status = 'Spotify 조회 종료 · 번역과 부족한 싱크 보완 계속'
            self.save()
            return
        existing = self._existing(track)
        counted = coverage(existing)
        direct = existing.get('direct') if existing else None
        spotify_record = (direct and direct['source'] == 'Spotify'
                          or existing and any(value['source'] == 'Spotify' for value in existing.get('snapshots', ())))
        if spotify_record and counted['lines'] and counted['timed'] == counted['lines']:
            done[track['id']] = {'status': 'existing', **counted}
            self.save()
            return
        self.active = {'phase': phase, 'track': track, 'started': now}
        self.active.update(job=service.job, request_id=secrets.token_hex(16))
        service.job.send({'action': 'batch_lyrics', 'request_id': self.active['request_id'], 'track': track})
        self.status = f"Spotify 원문·싱크 조회 · {track['title']}"
        self.save()

    def finish(self):
        if 'priority_track_ids' in self.value:return
        records = []
        for row in self.manifest.get('entries', []):
            value = self.store.load(row.get('track_id', '')) if row.get('track_id') else None
            facts = coverage(value)
            records.append({'index': row['index'], 'track_id': row.get('track_id', ''),
                            'entry_status': row['status'], **facts})
        counts = {'playlist_entries': len(records), 'unique_tracks': len(self._tracks()),
                  'complete_entries': sum(row['complete'] for row in records),
                  'missing_entries': sum(not row['complete'] for row in records),
                  'synced_entries': sum(bool(row['lines'] and row['timed'] == row['lines']) for row in records),
                  'translated_entries': sum(bool(row['lines'] and row['translated'] == row['lines']) for row in records)}
        complete = bool(self.manifest.get('complete') and records and not counts['missing_entries'])
        self.value.update(summary=counts, phase='complete' if complete else 'review', enabled=False,
                          all_complete=complete, collection_pass_finished=True)
        atomic_json(self.directory/'playlist_lyrics'/'report.json',
                    {'summary': counts, 'all_complete': complete, 'records': records,
                     'checked_at': datetime.now(timezone.utc).isoformat()})
        self.status = '플레이리스트 가사 완성' if complete else '플레이리스트 전곡 조회 종료 · 미확보 항목 보완 필요'
        self.enabled = False
        self.save()

    def close(self):
        self.closed = True
        self.keep_awake(False)
        self.cancel.set()
        if self.active and self.active.get('request_id'):
            self.active['job'].send({'action': 'catalog_cancel', 'request_id': self.active['request_id']})
