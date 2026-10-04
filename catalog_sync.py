"""Resumable catalog collection owned by the app, independent of the dictionary."""
from datetime import datetime, timezone
from pathlib import Path
import json
import queue
import re
import secrets
import threading
import time

from artist_metadata import validated_track
from catalog_autolink import cached_pool, local_matches, recording_title_key, MAX_PUBLIC_TRACKS, MAX_PUBLIC_BYTES
from catalog_link_queue import CatalogLinkQueue
from explorer_library import ExplorerLibrary, performers, search_key
from song_catalog import SongCatalog
from storage import atomic_json, read_json


class CatalogSync:
    def __init__(self, app):
        self.app = app
        self.directory = Path(app.store.directory)
        self.path = self.directory / 'spotify_catalog_sync.json'
        self.value = read_json(self.path, {})
        self.enabled = isinstance(self.value, dict) and self.value.get('enabled') is True
        self.status = '전체 Spotify 연결 준비 중' if self.enabled else ''
        self.events = queue.Queue()
        self.active = None
        self.next_at = 0
        self.matching = False
        self.pool_stamp = None
        self.catalog = None
        self.links = None
        self.cancel = threading.Event()
        self.last_tick = 0
        self.paused = self.value.get('paused') is True if isinstance(self.value, dict) else False
        self.closed = False
        self.import_stamps = {}

    def library(self):
        dialog = self.app.explorer_dialog
        return dialog.library if dialog else ExplorerLibrary(self.directory)

    def save(self):
        self.value['status'] = self.status
        self.value['updated_at'] = datetime.now(timezone.utc).isoformat()
        atomic_json(self.path, self.value)

    def store_tracks(self, tracks, source, search=False):
        path = self.directory / 'spotify_public_tracks.json'
        value = read_json(path, {})
        pool = {item['id']: item for item in value.get('tracks', [])
                if isinstance(item, dict) and isinstance(item.get('id'), str)}
        for item in tracks:
            if not isinstance(item, dict) or not item.get('artists'):continue
            if search and self.catalog and recording_title_key(item.get('title')) not in self.title_keys:continue
            request = {key: item.get(key) for key in ('title', 'album', 'duration')}
            request['artist'] = item['artists'][0]
            track = validated_track(request, item)
            if track:
                origin = source or item.get('source_url', '')
                if not re.fullmatch(r'https://open\.spotify\.com/(?:embed/)?(?:track|album|artist|playlist)/[A-Za-z0-9]{22}', origin):
                    origin = 'https://open.spotify.com/track/' + track['id']
                pool[track['id']] = {**track, 'source_url': origin, 'is_playable': None, 'checked_at': ''}
        if len(pool)>MAX_PUBLIC_TRACKS:raise ValueError('Spotify 메타데이터 저장 한도 초과 · 기존 자료 보존')
        saved = {'schema_version': 1, 'collected_at': datetime.now(timezone.utc).isoformat(),
                 'tracks': list(pool.values())}
        if len(json.dumps(saved, ensure_ascii=False, indent=2).encode('utf-8')) > MAX_PUBLIC_BYTES:
            raise ValueError('Spotify 메타데이터 저장 크기 초과 · 기존 자료 보존')
        atomic_json(path, saved)

    def receive(self, sender, event):
        if sender is not self.app.background_lyrics.job or not self.active:return False
        if (event.get('kind') == 'catalog_playlist_diagnostic'
                and event.get('request_id') == self.active.get('request_id')):
            value = event.get('value', {})
            safe = {key: value[key] for key in ('phase', 'operation', 'variable_keys', 'playlist_matches',
                    'has_persisted_query', 'http_status', 'total', 'count') if key in value}
            self.value['diagnostics'] = (self.value.get('diagnostics', []) + [safe])[-40:]
            self.save()
            return True
        if event.get('kind') not in ('catalog_playlist_progress', 'catalog_playlist_result'):return False
        if event.get('request_id') != self.active.get('request_id'):return False
        playlist = self.active['playlist_id']
        entry = self.value['playlists'][playlist]
        self.store_tracks(event.get('tracks') or [], 'https://open.spotify.com/playlist/' + playlist)
        for key in ('offset', 'total'):
            value = event.get(key)
            if type(value) is int and value >= 0:entry[key] = value
        if (event.get('complete') is True and type(entry.get('total')) is int
                and entry.get('offset') == entry['total']):
            entry['complete'] = True
            entry.pop('error', None)
        error = event.get('code')
        if error:
            entry['error'] = error
            if error not in ('rate_limited','login_required','interrupted','busy','cancelled'):
                entry['failures'] = entry.get('failures', 0) + 1
                if entry['failures'] >= 3:entry['deferred'] = True
            wait = event.get('retry_after', 300) if error == 'rate_limited' else 30
            self.next_at = time.monotonic() + max(1, wait if type(wait) in (int, float) else 300)
        else:
            entry.pop('error', None)
            entry.pop('failures', None)
        self.status = f"플레이리스트 {entry.get('offset', 0):,}/{entry.get('total') or '?'}개 확인"
        if error:self.status += ' · ' + str(event.get('error') or error)
        if event['kind'] == 'catalog_playlist_result':self.active = None
        self.save()
        return True

    def apply_matches(self, matches):
        library = self.library()
        linked = library.link_automatic_many(matches)
        self.links.library = library
        self.value['linked_recordings'] = len(library.value['spotify'])
        if linked and self.app.explorer_dialog:
            self.app.explorer_dialog.filter_songs(preserve_selected=True)
        return linked

    def tick(self, allow_search=True):
        if not self.enabled or self.closed:return
        if self.paused:
            self.status = '전체 Spotify 연결 일시정지'
            return
        now = time.monotonic()
        if now - self.last_tick < .5:return
        self.last_tick = now
        if self.catalog is None:
            self.catalog = SongCatalog(update_path=self.directory / 'song_catalog.json')
            self.title_keys = {recording_title_key(song['title']) for song in self.catalog.songs}
            self.people = performers(external=self.catalog.performers,
                                     database_path=self.directory / 'imas_db_people.json')
            self.links = CatalogLinkQueue(self.directory, self.catalog.songs, self.library(), self.people)
        while True:
            try:kind, payload = self.events.get_nowait()
            except queue.Empty:break
            if kind == 'matches':
                self.matching = False
                stamp, matches = payload
                current = self.directory / 'spotify_public_tracks.json'
                if (current.stat().st_mtime_ns if current.exists() else 0) != stamp:continue
                linked = self.apply_matches(matches)
                self.value['compared_recordings'] = len(self.catalog.songs)
                self.value['last_batch_links'] = linked
                self.save()
            elif kind == 'match_error':
                self.matching = False
                self.status = '수집 자료 대조 실패 · ' + payload
                self.save()
            elif kind == 'search':
                request, song, tracks = payload
                if self.active is not request:continue
                self.store_tracks(tracks, '', search=True)
                self.links.library = self.library()
                self.links.pool = cached_pool(self.directory)
                linked = self.links.apply(song, tracks)
                if linked and self.app.explorer_dialog:
                    self.app.explorer_dialog.filter_songs(preserve_selected=True)
                self.active = None
                self.next_at = now + 1
                self.value['last_search'] = song['title']
                self.value['coverage'] = self.links.coverage()
                self.value.get('query_failures', {}).pop(search_key(song['title']), None)
                self.save()
            elif kind == 'search_error':
                request, payload = payload
                if self.active is not request:continue
                self.active = None
                if payload not in ('rate_limited','login_required','interrupted','busy','cancelled'):
                    failures = self.value.setdefault('query_failures', {})
                    key = search_key(request['title'])
                    old = failures.get(key, {})
                    failures[key] = {'attempts': old.get('attempts', 0)+1, 'code': payload}
                self.next_at = now + (300 if payload == 'rate_limited' else 30)
                self.status = '전체 검색 대기 · ' + payload
                self.save()
        path = self.directory / 'spotify_public_tracks.json'
        for name in self.value.get('import_files', []):
            imported = (self.directory / name).resolve()
            if not imported.is_relative_to(self.directory.resolve()) or not imported.is_file():continue
            changed = imported.stat().st_mtime_ns
            if self.import_stamps.get(name) != changed:
                self.store_tracks(read_json(imported, {}).get('tracks', []), '')
                self.import_stamps[name] = changed
        stamp = path.stat().st_mtime_ns if path.exists() else 0
        if not self.matching and stamp != self.pool_stamp:
            self.pool_stamp = stamp
            self.matching = True
            def match():
                try:self.events.put(('matches', (stamp, local_matches(self.catalog.songs, cached_pool(self.directory), self.people))))
                except Exception as error:self.events.put(('match_error', type(error).__name__))
            threading.Thread(target=match, name='catalog-batch-match', daemon=True).start()
        service = self.app.background_lyrics
        if self.active and self.active.get('job') is not service.job:
            self.active = None
            self.next_at = now + 5
        if not allow_search:return
        if self.paused:
            self.status = '전체 Spotify 연결 일시정지'
            return
        if self.active or now < self.next_at:return
        if not service.job or not service.spotify_authenticated:
            self.status = '전체 연결 · 기존 Spotify 로그인 연결 대기'
            return
        if service.spotify_pending or service.metadata_pending:
            self.status = '전체 연결 · 현재 재생곡 가사·가수 조회가 끝나면 계속'
            return
        for playlist, entry in self.value.get('playlists', {}).items():
            if entry.get('complete') or entry.get('deferred') or not re.fullmatch(r'[A-Za-z0-9]{22}', playlist):continue
            request_id = secrets.token_hex(16)
            self.active = {'playlist_id': playlist, 'request_id': request_id, 'job': service.job}
            service.job.send({'action': 'catalog_playlist', 'request_id': request_id,
                              'playlist_id': playlist, 'offset': entry.get('offset', 0)})
            self.status = f"플레이리스트 전체 읽기 · {entry.get('offset', 0):,}개부터 계속"
            self.save()
            return
        self.links.library = self.library()
        deferred = {key for key, value in self.value.get('query_failures', {}).items() if value.get('attempts', 0)>=3}
        song = self.links.next_song(skip=deferred)
        if song is None:
            self.status = self.links.status_text() + ' · 이번 전체 검색 순회 종료'
            self.value['coverage'] = self.links.coverage()
            self.value['search_pass_finished'] = True
            if deferred:self.status += f' · 조회 오류 {len(deferred):,}곡 별도 기록'
            if not self.matching:
                self.enabled = False
                self.value['enabled'] = False
                self.save()
            return
        self.active = {'job': service.job, 'title': song['title']}
        request = self.active
        self.status = self.links.status_text() + ' · 검색 중: ' + song['title']
        def search():
            try:self.events.put(('search', (request, song, service.catalog_search(song['title'], cancel=self.cancel))))
            except Exception as error:self.events.put(('search_error', (request, getattr(error, 'code', type(error).__name__))))
        threading.Thread(target=search, name='catalog-full-search', daemon=True).start()

    def close(self):
        self.closed = True
        self.cancel.set()

    def pause(self, paused):
        self.paused = paused
        self.value['paused'] = paused
        if paused:
            self.cancel.set()
            if self.active and self.active.get('request_id'):
                self.active['job'].send({'action': 'catalog_cancel', 'request_id': self.active['request_id']})
        else:self.cancel = threading.Event()
        self.save()
