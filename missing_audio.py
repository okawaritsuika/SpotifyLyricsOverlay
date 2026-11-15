"""Find and analyze Spotify playlists using verified recordings and lyrics."""
from collections import deque
import ctypes
import re
import secrets
from hashlib import sha256
from pathlib import Path
from threading import Event, Thread
import time

from audio_capture_cache import find_capture
from audio_job import AudioJob, ready
from audio_progress import AudioProgress
from local_library import AUDIO_EXTENSIONS
from lyrics import Line, Song
from quiet_audio import QuietAudio
from recording_context import recording_key
from local_analysis import lyric_digest
from spotify_catalog import SpotifyCatalogError
from spotify_desktop import DesktopSpotify
from storage import atomic_json, read_json, validate_times


CAPTURE_LINKS = 'spotify_missing_audio_captures.json'
ANALYZED_TRACKS = 'spotify_playlist_analysis_results.json'


def spotify_clocks(value, track_id):
    sources = [item for item in value.get('snapshots', ()) if item['source'].casefold() == 'spotify'
               and item['recording_verified'] and not item['annotation_only'] and item.get('track_id') == track_id]
    direct = value.get('direct') or {}
    if direct.get('source') == 'Spotify' and direct.get('provider_id') == track_id:
        sources.append(direct)
    return sources


def missing_tracks(directory, audio_directory, store, manifest=None):
    original = read_json(directory / 'playlist_lyrics/manifest.json', {})
    if manifest is None:
        manifest = original
    entries = manifest.get('entries', [])
    files = {p.stem.casefold() for p in audio_directory.iterdir()
             if p.suffix.lower() in AUDIO_EXTENSIONS and p.is_file() and p.stat().st_size
             } if audio_directory.is_dir() else set()
    captures = directory / 'audio_captures'
    recorded = {folder.name for folder in captures.iterdir() if folder.is_dir()} if captures.is_dir() else set()
    capture_links = read_json(directory / CAPTURE_LINKS, {})
    analyzed = read_json(directory / ANALYZED_TRACKS, {})
    failures = {row['position']: row.get('stage') for row in read_json(audio_directory / 'mp3-failures.json', [])
                if Path(row.get('link_list', '')).name == 'idolmaster-youtube-links.json'}
    links = read_json(audio_directory.parent / 'playlist-youtube/idolmaster-youtube-links.json', {})
    linked = {row.get('track_id') for row in links.get('entries', []) if row.get('youtube_url')}
    downloaded = set()
    failure_by_id = {}
    for position, entry in enumerate(original.get('entries', []), 1):
        track_id = entry.get('track_id')
        # The local MP3 names follow the original export's order.
        title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', (entry.get('track') or {}).get('title', ''))[:100].rstrip(' .')
        if f'{position:04d} {title}'.casefold() in files:
            downloaded.add(track_id)
        if position in failures:
            failure_by_id[track_id] = failures[position]
    result = []
    seen = set()
    for position, entry in enumerate(entries, 1):
        track = entry.get('track') or {}
        track_id = entry.get('track_id')
        if (track.get('id') != track_id or not isinstance(track_id, str) or not re.fullmatch('[A-Za-z0-9]{22}',track_id)
                or not track.get('artists') or not track.get('title') or not 10 <= track.get('duration', 0) <= 900):
            continue
        if track_id in downloaded or track_id in seen:
            continue
        seen.add(track_id)
        linked_key = capture_links.get(track_id)
        if isinstance(linked_key, str) and len(linked_key.split('\0')) == 3 and find_capture(captures,linked_key,track['duration']):
            continue
        if any(recording_key(track['title'],artist,track['album']) in recorded
               and find_capture(captures,'\0'.join((track['title'],artist,track['album'])),track['duration'])
               for artist in {*track['artists'],', '.join(track['artists'])}):
            continue
        value = store.load(track_id)
        if not value or not value['lines']:
            continue
        if analyzed.get(track_id) == lyric_digest([line.original for line in value['lines']]):
            continue
        sources = spotify_clocks(value, track_id)
        if not sources or any(stamp is not None for item in sources for stamp in item['times']):
            continue
        reason = {'download': '다운로드 실패', 'convert': '변환 실패'}.get(failure_by_id.get(track_id))
        if not reason and manifest is not original:
            reason = '음원 미확보'
        result.append({'track': value['track'], 'lines': value['lines'], 'position': position,
                       'reason': reason or ('파일 미확보' if track_id in linked else '영상 링크 없음')})
    return result


class CollectionPlaylist:
    """Prepare a capture list through the connected Spotify worker."""

    def __init__(self, collector, playlist_id):
        self.collector = collector
        self.service = collector.app.background_lyrics
        self.job = self.service.job
        if self.job is None or not self.service.spotify_authenticated:
            raise ValueError('가사 연결에서 Spotify 계정에 먼저 로그인해 주세요.')
        self.manifest = {'playlist_id': playlist_id, 'entries': []}
        self.total = None
        self.tracks = None
        self.track = None
        self.checked = 0
        self.error = ''
        self.next_at = 0
        self.busy_attempts = 0
        self.send('catalog_playlist', playlist_id=playlist_id, offset=0)

    def send(self, action, **payload):
        self.request_id = secrets.token_hex(16)
        self.deadline = time.monotonic() + 60
        self.job.send(dict(action=action, request_id=self.request_id, **payload))

    def cancel(self):
        if self.request_id:
            self.job.send({'action': 'catalog_cancel', 'request_id': self.request_id})
            self.request_id = None

    def receive(self, sender, event):
        if sender is not self.job or not self.request_id or event.get('request_id') != self.request_id:
            return False
        kind = event.get('kind')
        if kind not in ('catalog_playlist_result', 'catalog_playlist_progress', 'batch_lyrics_result'):
            return True
        code = event.get('code')
        if kind == 'batch_lyrics_result' and code == 'busy' and self.busy_attempts < 3:
            self.request_id = None
            self.busy_attempts += 1
            self.next_at = time.monotonic() + 3
            self.tracks.appendleft(self.track)
            return True
        if code and not (kind == 'batch_lyrics_result' and code == 'not_found'):
            self.request_id = None
            self.error = event.get('error') or 'Spotify 응답을 확인하지 못했습니다.'
            if code == 'rate_limited' and event.get('retry_after'):
                self.error += f" ({event['retry_after']}초 후 다시 시도)"
            return True
        if kind == 'batch_lyrics_result':
            self.request_id = None
            self.busy_attempts = 0
            self.next_at = time.monotonic() + 3
            if not code:
                try:self.save_lyrics(event)
                except (TypeError, ValueError, KeyError):
                    self.error = 'Spotify 가사 응답을 확인하지 못했습니다.'
            self.checked += 1
            return True
        entries = self.manifest['entries']
        page = event.get('entries')
        start, offset, total = (event.get(key) for key in ('start', 'offset', 'total'))
        complete = kind == 'catalog_playlist_result'
        if (event.get('playlist_id') != self.manifest['playlist_id']
                or not isinstance(page, list) or type(start) is not int or type(offset) is not int
                or start != len(entries) or offset != start + len(page)
                or type(total) is not int or total < offset or self.total not in (None, total)
                or any(not isinstance(row, dict) or row.get('index') != start + i for i, row in enumerate(page))
                or (complete and (event.get('complete') is not True or offset != total))):
            self.cancel()
            self.error = 'Spotify 재생목록을 끝까지 확인하지 못했습니다.'
            return True
        self.total = total
        entries.extend(page)
        self.deadline = time.monotonic() + 60
        if complete:
            self.request_id = None
            self.tracks = deque({row['track']['id']: row['track'] for row in entries
                                 if row.get('status') == 'track'}.values())
            self.track_total = len(self.tracks)
        return True

    def save_lyrics(self, event):
        track = self.track
        if event.get('provider_id') != track['id']:
            raise ValueError('Spotify 가사 응답의 곡이 요청한 곡과 다릅니다.')
        rows = event.get('lines') or []
        lines = tuple(Line(row['text'], '', '') for row in rows)
        times = [row.get('time') for row in rows]
        validate_times(times, len(lines))
        if lines and all(stamp is None for stamp in times):
            direct = dict(source='Spotify', provider_id=track['id'], lines=lines, times=times)
            self.collector.app.playlist_lyrics.store.save(track, lines, direct)

    def poll(self):
        """Advance at most one lyric check and return the completed manifest."""
        if self.error:
            raise ValueError(self.error)
        if self.service.job is not self.job or (self.request_id and time.monotonic() >= self.deadline):
            self.cancel()
            raise ValueError('Spotify 재생목록 조회가 중단되었습니다. 다시 불러와 주세요.')
        if self.request_id or self.tracks is None or time.monotonic() < self.next_at:
            return None
        if self.service.spotify_pending or self.service.metadata_pending:
            return self.manifest if not self.tracks else None
        end = time.monotonic() + .02
        while self.tracks and time.monotonic() < end:
            self.track = self.tracks.popleft()
            track_id = self.track['id']
            record = self.collector.app.playlist_lyrics.store.load(track_id)
            if not (record and record['lines'] and spotify_clocks(record, track_id)):
                self.send('batch_lyrics', track=self.track)
                return None
            self.checked += 1
        return self.manifest if not self.tracks else None

    def progress(self):
        if self.tracks is None:
            return f"Spotify 재생목록 읽는 중 · {len(self.manifest['entries']):,}/{self.total if self.total is not None else '?'}개"
        return f'Spotify 가사·싱크 확인 중 · {self.checked:,}/{self.track_total:,}곡'


class MissingAudioCollector:
    def __init__(self, app, base):
        self.app, self.base = app, Path(base)
        self.desktop = DesktopSpotify(app)
        self.quiet = QuietAudio(app.store.directory / 'personal_audio_restore.json')
        self.active = False
        self.stopping = False
        self.sleep_held = False
        self.status = ''
        self.play_worker = None
        self.play_result = None
        self.cancel_play = Event()
        self.current = None
        self.job = None
        self.jobs = {}
        self.collected = set()
        self.review_needed = set()
        self.queue = deque()
        self.total = self.saved = self.reviewed = self.failed = 0
        self.pause_deadline = self.last_pause = self.next_tick = 0.
        self.seek_deadline = None
        self.prepare_step = ''
        self.prepare_deadline = 0.
        self.capture_ready = False
        self.play_requested = False
        self.route_unavailable_since = None
        self.results_path = app.store.directory / ANALYZED_TRACKS
        self.capture_links = app.store.directory / CAPTURE_LINKS
        self.capture_error = ''
        self.failures = {}
        self.selection_path = self.app.store.directory / 'playlist_lyrics' / 'capture_selection.json'
        self.playlist_request = None

    def inventory(self, manifest=None):
        return missing_tracks(self.app.store.directory, self.base / 'output/mp3', self.app.playlist_lyrics.store, manifest)

    def cancel_playlist(self):
        if self.playlist_request:
            self.playlist_request.cancel()
            self.playlist_request = None

    @property
    def owns_playback(self):
        return self.active or self.stopping

    def start(self, items):
        app = self.app
        if self.active or self.stopping or self.jobs:
            return
        if not app.services_started or not app.playback.has_timeline or app.playlist_lyrics.owns_spotify:
            raise ValueError('Spotify에서 곡을 선택하고 다른 재생목록 작업이 끝난 뒤 시작하세요.')
        if app.audio_job:
            raise ValueError('직접 시작한 재생음 분석이 끝난 뒤 시작하세요.')
        if self.quiet.path.exists():
            raise ValueError('Spotify를 일시정지하고 소리 복원을 먼저 눌러 주세요.')
        if not items:
            self.status = '분석할 곡이 없습니다.'
            return
        ready(self.base)
        self.quiet.enable()
        try:
            if not ctypes.windll.kernel32.SetThreadExecutionState(0x80000001):
                raise OSError('수집 중 절전 방지를 설정하지 못했습니다.')
        except OSError:
            self.quiet.restore()
            raise
        self.sleep_held = self.active = True
        app.clear_realtime()
        for job, phase in tuple(app.auto_analyzer.jobs.items()):
            if phase in ('waiting', 'recording'):
                app.auto_analyzer._cancel_job(job,'shutdown','playlist-analysis-cancel')
        self.route_unavailable_since = None
        self.saved = self.reviewed = self.failed = 0
        self.failures.clear()
        self.queue = deque(items)
        self.total = len(items)
        self.status = f'재생목록 분석 대기 · {self.total}곡'

    def _play_next(self):
        self.current = self.queue.popleft()
        self.cancel_play.clear()
        self.play_result = None
        self.prepare_step = ''
        self.status = 'Spotify에서 곡 여는 중 · ' + self.current['track']['title']

        def play():
            try:
                self.play_result = bool(self.desktop.play(self.current['track'],self.cancel_play,timeout=12))
            except (OSError, ValueError, RuntimeError, SpotifyCatalogError):
                self.play_result = False

        self.play_worker = Thread(target=play, daemon=True, name='missing-audio-play')
        self.play_worker.start()

    def _start_capture(self):
        app = self.app
        playback = app.playback
        expected = self.current['track']
        from spotify_desktop import _matches_playback
        now = time.monotonic()
        current = self.desktop.current_track()
        if (not _matches_playback(playback, expected) or now - playback.captured > 3
                or current and current['id'] != expected['id']):
            self._failure('재생 중인 곡을 확인하지 못했습니다.')
            return
        if not self.prepare_step:
            self.prepare_step, self.prepare_deadline = 'pause', now + 12
            self.status = '곡 처음부터 분석할 준비 중 · ' + expected['title']
            if playback.playing and not app.monitor.request_playback('pause'):
                self._failure('Spotify 일시정지를 요청할 수 없습니다.')
                return
        if now >= self.prepare_deadline:
            self._failure('Spotify 일시정지 또는 곡 처음 이동을 확인하지 못했습니다.')
            return
        if playback.playing:
            return
        if self.prepare_step == 'pause':
            if not app.monitor.request_seek('first',playback.key):
                return
            self.prepare_step = 'seek'
            self.seek_deadline = playback.captured
            return
        if playback.captured <= self.seek_deadline or playback.position() > .8:
            return
        lines = self.current['lines']
        song = app.song if (app.is_bound() and tuple(line.original for line in app.song.lines)
                            == tuple(line.original for line in lines)) else None
        memory = not song or app.live_lyrics.is_temporary(song)
        if not song:
            song = Song('memory-' + sha256(playback.key.encode()).hexdigest()[:24],
                        playback.title,playback.artist,playback.album,lines)
        analyzer = app.auto_analyzer
        job = AudioJob(self.base,song,playback,analyzer.mailbox,memory_only=memory,
                       playlist_analysis=True,incremental=True,
                       realtime_device=analyzer.realtime_device,alignment=analyzer.alignment,
                       capture_cache=analyzer.capture_cache)
        signature = analyzer.signature(song,memory)
        if memory:analyzer.live_signatures[song.id] = signature
        job.analysis_key = (playback.key,signature)
        job.recording_duration = playback.duration
        job.transport_generation = analyzer.transport_generation
        analyzer.jobs[job] = 'waiting'
        analyzer.progress[job] = AudioProgress(song.title,mode='재생목록')
        self.jobs[job] = (playback.key,playback.duration,expected['id'])
        self.job = job
        self.capture_ready = self.play_requested = False
        self.capture_error = ''
        self.seek_deadline = None
        self.prepare_deadline = now + 15
        self.status = '재생목록 분석 연결 대기 · ' + song.title

    def _failure(self, message):
        self.failed += 1
        if self.current:
            self.failures[self.current['track']['id']] = message
        self.job = self.current = None
        self.seek_deadline = None
        self.status = message

    def tick(self):
        now = time.monotonic()
        if now < self.next_tick or not (self.active or self.stopping):
            return
        self.next_tick = now + .35
        app = self.app
        try:
            if self.stopping:
                self._restore(now)
                return
            handles = [job.process for job in self.jobs if not job.capture_complete and job.process is not None]
            try:
                self.quiet.maintain(handles)
                self.route_unavailable_since = None
            except ValueError as error:
                if ('소리 연결을 확인할 수 없습니다' not in str(error) and
                        '프로세스가 변경' not in str(error)):
                    raise
                if self.route_unavailable_since is None:
                    self.route_unavailable_since = now
                if now - self.route_unavailable_since >= 4:
                    raise
                return
            if self.job:
                job = self.job
                if job.capture_complete:
                    if app.playback.playing and now - self.last_pause >= 1:
                        app.monitor.request_playback('pause')
                        self.last_pause = now
                elif not self.play_requested:
                    if now >= self.prepare_deadline:
                        job.cancel()
                        self._failure('Spotify 분석 시작을 확인하지 못했습니다.')
                    elif self.capture_ready and app.monitor.request_playback('play'):
                        self.play_requested = True
                        self.status = '곡 전체 수집 중 · ' + self.current['track']['title']
                        self.prepare_deadline = now + 15
                elif not job.capture_started and now >= self.prepare_deadline:
                    job.cancel()
                    self._failure('Spotify 재생 시작을 확인하지 못했습니다.')
                return
            if self.play_worker:
                if self.play_worker.is_alive():
                    return
                self.play_worker = None
                if not self.play_result:
                    self._failure('Spotify에서 이 곡의 재생을 확인하지 못했습니다.')
                    return
            if self.current:
                self._start_capture()
            elif not self.queue:
                self.stop('재생목록 분석 종료')
            elif not self.jobs and not app.auto_analyzer.jobs:
                self._play_next()
        except (OSError,ValueError) as error:
            if self.stopping:
                self.stopping = False
                self.status += ' · ' + str(error)
                self._release_sleep()
            else:
                self.stop(str(error))

    def accepts_result(self, job):
        target = self.jobs.get(job)
        return bool(target and self.accepts_recording(*target))

    def accepts_recording(self, key, duration, track_id):
        entry = self.app.background_lyrics.artist_metadata.entries.get((key, float(duration)))
        return not entry or entry['track']['id'] == track_id

    def receive(self, job, event):
        if job is self.job and event.get('kind') == 'progress' and event.get('state') == 'waiting':
            self.capture_ready = True
        if job is self.job and (event.get('kind') == 'error' or event.get('state') in ('discarded','unmatched')):
            self.capture_error = event.get('text','')
        if job in self.jobs and event.get('kind') == 'finished':
            key, duration, track_id = self.jobs.pop(job)
            outcome = job.analysis_committed
            if outcome == 'complete':
                self.collected.add(track_id)
                self.review_needed.discard(track_id)
                try:
                    signature = lyric_digest([line.original for line in job.song.lines])
                    atomic_json(self.results_path, {**read_json(self.results_path, {}), track_id:signature})
                except (OSError, ValueError) as error:
                    self.capture_error = f'분석은 완료했습니다. 완료 목록 저장 실패 · {error}'
            elif self.accepts_recording(key, duration, track_id) and find_capture(self.app.auto_analyzer.capture_cache, key, duration):
                try:atomic_json(self.capture_links, {**read_json(self.capture_links, {}), track_id:key})
                except (OSError, ValueError) as error:
                    self.capture_error = self.capture_error or f'재분석용 데이터 연결 실패 · {error}'
            if outcome == 'review':self.review_needed.add(track_id)
            if job is self.job and self.active:
                if outcome == 'complete':
                    self.saved += 1
                    self.status = self.capture_error or '분석 완료 · ' + job.song.title
                elif outcome == 'review':
                    self.reviewed += 1
                    self.status = '싱크 검토 필요 · ' + job.song.title
                else:
                    self._failure(self.capture_error or '분석 결과를 확인하지 못했습니다. 다음 곡으로 이동합니다.')
                self.job = self.current = None

    def stop(self, reason='재생목록 분석 중지'):
        if not self.active:
            return
        self.active, self.stopping = False, True
        self.cancel_play.set()
        self.status = reason
        self.pause_deadline = time.monotonic() + 15
        if self.job and not self.job.capture_complete:
            try:self.job.finish_capture()
            except OSError:self.job.cancel()
        self.job = self.current = None

    def _restore(self, now):
        app = self.app
        if self.play_worker and self.play_worker.is_alive():
            return
        if app.playback.has_timeline and not app.playback.playing and now - app.playback.captured <= 3:
            try:
                self.quiet.restore()
            except (OSError,ValueError) as error:
                if now < self.pause_deadline:
                    return
                self.status += ' · ' + str(error)
            self.stopping = False
        elif now >= self.pause_deadline:
            self.stopping = False
            self.status += ' · Spotify 일시정지 후 소리 복원 버튼을 눌러 주세요.'
        elif now - self.last_pause >= 1:
            app.monitor.request_playback('pause')
            self.last_pause = now
        if not self.stopping:
            self._release_sleep()

    def _release_sleep(self):
        if self.sleep_held:
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
            self.sleep_held = False

    def restore(self):
        if (not self.app.playback.has_timeline or self.app.playback.playing
                or time.monotonic() - self.app.playback.captured > 3):
            raise ValueError('Spotify를 일시정지한 뒤 소리를 복원하세요.')
        self.quiet.restore()
        self.status = 'Spotify 원래 출력 복원 완료'

    def summary(self):
        progress = self.app.auto_analyzer.progress.get(self.job)
        detail = f' · {progress.text}' if progress and progress.phase in ('recording','queued','analyzing') else ''
        return f'{self.status}{detail} · {self.saved}/{self.total}곡 완료 · 검토 {self.reviewed}곡 · 실패 {self.failed}곡'

    def snapshot(self):
        return {'active':self.active,'stopping':self.stopping,'status':self.summary(),
                'current':self.current['track']['title'] if self.current else '',
                'saved':self.saved,'failed':self.failed,'remaining':len(self.queue),
                'recording':bool(self.job and self.job.capture_started and not self.job.capture_complete)}
