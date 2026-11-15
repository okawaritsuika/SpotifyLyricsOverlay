"""Collect Spotify originals for the local AI translation workflow."""
from collections import deque
import json
from pathlib import Path
import queue
import re
import secrets
import threading
import time
from urllib.request import Request, urlopen

from catalog_autolink import cached_pool
from lyrics import Line
from playlist_lyrics_store import _read, _safe, _track
from spotify_catalog import parse_track_id
from spotify_public_catalog import parse_public_tracks
from storage import atomic_json, validate_times


MAX_LINKS = 50
MAX_PAGE_BYTES = 8 * 1024 * 1024
MAX_INDEX_BYTES = 8 * 1024 * 1024
_KANA = re.compile(r'[\u3041-\u3096\u30a1-\u30fa\uff66-\uff9d]')
_TRACK_URL = 'https://open.spotify.com/track/'


def _has_japanese(lines):
    return any(_KANA.search(row.original) for row in lines)


def _public_track(track_id):
    url = _TRACK_URL + track_id
    request = Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'text/html'})
    with urlopen(request, timeout=10) as response:
        if parse_track_id(response.geturl()) != track_id:return None
        data = response.read(MAX_PAGE_BYTES + 1)
    if len(data) > MAX_PAGE_BYTES:return None
    tracks = parse_public_tracks(data.decode('utf-8'), url)
    return next((_track(item) for item in tracks if item['id'] == track_id), None)


def _spotify_original(saved, track_id):
    if not saved:return False
    direct = saved.get('direct')
    if direct and direct['source'] == 'Spotify' and direct.get('provider_id') == track_id:return True
    return any(row['source'] == 'Spotify' and row.get('track_id') == track_id
               for row in saved.get('snapshots', ()))


class SpotifyTranslationSources:
    def __init__(self, app):
        self.app = app
        self.store = app.playlist_lyrics.store
        self.directory = Path(app.store.directory)
        self.path = self.directory / 'ai_translations' / 'spotify_sources.json'
        self.queue = deque()
        self.pending = None
        self.results = queue.Queue()
        self.current_track = None
        self.total = self.imported = self.skipped = self.failed = 0
        self.status = ''
        self.last_issue = ''
        self.closed = False

    @property
    def busy(self):
        return self.pending is not None or bool(self.queue)

    def _index(self):
        value = _read(self.path, MAX_INDEX_BYTES, {'version': 1, 'tracks': {}})
        if not isinstance(value, dict) or value.get('version') != 1 or not isinstance(value.get('tracks'), dict):
            raise ValueError('Spotify 번역 자료 목록을 읽을 수 없습니다.')
        result = {}
        for track_id, entry in value['tracks'].items():
            track = _track(entry)
            if not track or track['id'] != track_id or type(entry.get('included')) is not bool:
                raise ValueError('Spotify 번역 자료 목록을 읽을 수 없습니다.')
            result[track_id] = {**track, 'included': entry['included']}
        return result

    def records(self):
        return tuple(self._index().values())

    def _save_index(self, index):
        value = {'version': 1, 'tracks': index}
        if (not _safe(self.path) or
                len(json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8')) > MAX_INDEX_BYTES):
            raise ValueError('Spotify 번역 자료 목록을 저장할 수 없습니다.')
        atomic_json(self.path, value)

    def set_included(self, track_id, included):
        if type(included) is not bool or not isinstance(track_id, str):
            raise ValueError('번역 요청에 포함할 곡을 확인해 주세요.')
        index = self._index()
        if track_id not in index:raise ValueError('가져온 Spotify 원문을 찾을 수 없습니다.')
        if index[track_id]['included'] != included:
            index[track_id]['included'] = included
            self._save_index(index)

    def _remember(self, track):
        index = self._index()
        track_id = track['id']
        entry = {**track, 'included': index.get(track_id, {}).get('included', True)}
        if index.get(track_id) != entry:
            index[track_id] = entry
            self._save_index(index)

    def start(self, links):
        if self.closed or self.busy:raise ValueError('진행 중인 Spotify 원문 가져오기가 끝난 뒤 다시 시도해 주세요.')
        if not isinstance(links, str) or len(links) > MAX_LINKS * 2049:
            raise ValueError(f'Spotify 곡 링크를 {MAX_LINKS}개까지 입력해 주세요.')
        ids = dict.fromkeys(map(parse_track_id, links.split()))
        if not ids or None in ids:raise ValueError('Spotify 곡 링크를 공백 또는 줄바꿈으로 구분해 입력해 주세요.')
        if len(ids) > MAX_LINKS:raise ValueError(f'Spotify 곡 링크를 {MAX_LINKS}개까지 입력해 주세요.')
        self._index()
        workspace = getattr(self.app, 'workspace', None)
        current = _track(workspace.music_catalog.spotify.current_track()) if workspace else None
        self.current_track = current if current and current['id'] in ids else None
        self.total, self.imported, self.skipped, self.failed = len(ids), 0, 0, 0
        self.queue.extend(ids)
        self.last_issue = ''
        self.status = f'Spotify 원문 {self.total}곡 가져오는 중…'
        return self.total

    def _resolve(self, item):
        track_id = item['id']
        try:
            saved = self.store.load(track_id)
            if _spotify_original(saved, track_id):
                self.results.put((item, saved['track'], saved['lines'], ''))
                return
            track = saved['track'] if saved else item.get('current_track')
            if not track:
                track = next((_track(row) for row in cached_pool(self.directory) if row['id'] == track_id), None)
            if not track:track = _public_track(track_id)
            detail = '' if track else 'Spotify 곡 정보를 확인하지 못했습니다.'
            self.results.put((item, track, (), detail))
        except (OSError, ValueError, TypeError, RecursionError):
            self.results.put((item, None, (), 'Spotify 곡 정보를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.'))

    def _completed(self, outcome, detail=''):
        setattr(self, outcome, getattr(self, outcome) + 1)
        label = self.pending.get('track', {}).get('title') or self.pending['id']
        self.pending = None
        if detail:self.last_issue = f'{label} · {detail}'
        done = self.imported + self.skipped + self.failed
        if self.queue:
            self.status = f'Spotify 원문 확인 {done}/{self.total}곡 · 가져옴 {self.imported}곡'
        else:
            self.status = f'Spotify 원문 {self.imported}곡 가져옴 · 원문 없음 또는 일본어 미확인 {self.skipped}곡 · 실패 {self.failed}곡'
            if self.last_issue:self.status += f'\n{self.last_issue}'

    def _register(self, track):
        try:self._remember(track)
        except (OSError, ValueError):self._completed('failed', '번역 자료 목록에 저장하지 못했습니다.')
        else:self._completed('imported')

    def tick(self):
        if self.closed or not self.busy:return
        now = time.monotonic()
        if self.pending is None:
            item = {'id': self.queue.popleft(), 'deadline': now + 45, 'next_at': now}
            if self.current_track and self.current_track['id'] == item['id']:
                item['current_track'] = self.current_track
            self.pending = item
            threading.Thread(target=self._resolve, args=(item,), daemon=True, name='spotify-translation-metadata').start()
            return
        item = self.pending
        while not self.results.empty():
            requested, track, lines, detail = self.results.get_nowait()
            if requested is not item:continue
            if not track:
                self._completed('failed', detail)
                return
            item['track'] = track
            if lines:
                if _has_japanese(lines):self._register(track)
                else:self._completed('skipped', '일본어 원문을 확인하지 못했습니다.')
                return
        service = self.app.background_lyrics
        if now >= item['deadline'] or item.get('job', service.job) is not service.job:
            self._cancel_request()
            self._completed('failed', 'Spotify 요청이 지연되었거나 연결이 변경되었습니다.')
            return
        if 'track' not in item or 'request_id' in item or now < item['next_at']:return
        if not service.job or not service.spotify_authenticated:
            self._completed('failed', '가사 연결에서 Spotify에 먼저 로그인해 주세요.')
            return
        collector = self.app.missing_audio_collector
        if (self.app.playlist_lyrics.active or self.app.catalog_sync.active
                or collector and collector.playlist_request or service.spotify_pending or service.metadata_pending):
            return
        item.update(job=service.job, request_id=secrets.token_hex(16))
        self.status = f"Spotify 일본어 원문 조회 중 · {item['track']['title']}"
        service.job.send({'action': 'batch_lyrics', 'request_id': item['request_id'], 'track': item['track']})

    def receive(self, sender, event):
        item = self.pending
        if (not item or 'request_id' not in item or item['job'] is not sender
                or event.get('kind') != 'batch_lyrics_result' or event.get('request_id') != item['request_id']):
            return False
        track = item['track']
        if event.get('provider_id') != track['id']:
            self._completed('failed', 'Spotify 응답의 곡이 일치하지 않습니다.')
        elif event.get('code') in ('busy', 'interrupted'):
            item.pop('request_id')
            item['next_at'] = time.monotonic() + 1
        elif event.get('code') == 'not_found' or not event.get('code') and not event.get('lines'):
            self._completed('skipped', 'Spotify 가사를 찾지 못했습니다.')
        elif event.get('code') == 'rate_limited':
            self.queue.clear()
            self.failed = self.total - self.imported - self.skipped - 1
            self._completed('failed', 'Spotify 요청 한도에 도달했습니다. 잠시 후 남은 곡을 다시 시도해 주세요.')
        elif event.get('code'):
            detail = ('가사 연결에서 Spotify 로그인 상태를 확인해 주세요.' if event['code'] == 'login_required'
                      else 'Spotify 가사를 불러오지 못했습니다.')
            self._completed('failed', detail)
        else:
            self._save(track, event['lines'])
        return True

    def _save(self, track, rows):
        try:
            if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get('text'), str)
                                                 or not row['text'].strip() for row in rows):
                raise ValueError()
            lines = tuple(Line(row['text'], '', '') for row in rows)
            times = [row.get('time') for row in rows]
            validate_times(times, len(lines))
            if any(stamp is not None and stamp > track['duration'] + 2 for stamp in times):raise ValueError()
            if not _has_japanese(lines):
                self._completed('skipped', '일본어 원문을 확인하지 못했습니다.')
                return
            direct = {'source': 'Spotify', 'provider_id': track['id'], 'lines': lines, 'times': times}
            if not self.store.save(track, lines, direct, [_TRACK_URL + track['id']]):raise ValueError()
        except (OSError, ValueError, TypeError):
            self._completed('failed', 'Spotify 원문 또는 싱크를 저장하지 못했습니다.')
        else:self._register(track)

    def _cancel_request(self):
        if self.pending and self.pending.get('request_id'):
            self.pending['job'].send({'action': 'batch_lyrics_cancel', 'request_id': self.pending['request_id']})

    def cancel(self):
        self._cancel_request()
        self.pending = None
        self.queue.clear()
        self.status = f'Spotify 가져오기 취소 · 원문 {self.imported}곡 저장됨'

    def close(self):
        self.cancel()
        self.closed = True
