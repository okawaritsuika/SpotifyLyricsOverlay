"""Opt-in, serial provider lookups with bounded retries and memory reuse."""
from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
import math
import threading
import time
from urllib.error import HTTPError, URLError


def _error_label(error):
    if isinstance(error, HTTPError):
        return f'HTTP {error.code}' if type(error.code) is int and 100 <= error.code <= 599 else 'HTTP 오류'
    if isinstance(error, TimeoutError) or isinstance(error, URLError) and isinstance(error.reason, TimeoutError):
        return '시간 초과'
    return type(error).__name__


class ProviderSyncJob:
    MAX_ENTRIES = 32
    RETRY_DELAY = 15
    ERROR_COOLDOWN = 300

    def __init__(self, messages, fetch, name='LyricsPlus', reports_status=False):
        self.messages, self.fetch = messages, fetch
        self.name = name
        self.reports_status = reports_status
        self.status = f'{self.name} 보조 조회 꺼짐'
        self.pending = False
        self.done = False
        self.closed = False
        self.generation = 0
        self.key = ''
        self.playback = None
        self._state = None
        self._identity = None
        self._serial = 0
        self._current = None
        self._running = None
        self._entries = OrderedDict()

    def _remember(self, identity, entry):
        self._entries[identity] = entry
        self._entries.move_to_end(identity)
        while len(self._entries) > self.MAX_ENTRIES:
            self._entries.popitem(last=False)

    def _token(self):
        self._serial += 1
        self._current = (self.generation, self._serial)
        self.pending = True
        return self._current

    def observe(self, playback, enabled=False, cached_sync=False, reference=(), waiting_for_primary=False):
        if self.closed:
            return
        reference = tuple(reference)
        fingerprint = hashlib.sha256(json.dumps(reference, ensure_ascii=False).encode('utf-8')).hexdigest()
        identity = (playback.key, playback.duration, fingerprint)
        eligible = bool(playback.has_timeline and playback.artist.strip()
                        and math.isfinite(playback.duration))
        state = (identity, bool(enabled), bool(cached_sync), eligible, bool(waiting_for_primary))
        if state != self._state:
            self.generation += 1
            self._state, self._identity = state, identity
            self._current = None
            self.pending = self.done = False
        self.playback, self.key = playback, playback.key
        if not enabled:
            self.status = f'{self.name} 보조 조회 꺼짐'
            return
        if cached_sync:
            self.done = True
            self.status = f'저장한 싱크 사용 · {self.name} 조회 생략'
            return
        if waiting_for_primary:
            self.status = f'Spotify 싱크 확인 대기 · {self.name} 조회 보류'
            return
        if not eligible:
            self.status = f'{self.name} 조회에 필요한 재생 정보 대기'
            return
        if not reference:
            self.status = f'{self.name} 원문 대기'
            return
        if self.pending:
            return
        entry = self._entries.get(identity)
        if (entry and entry['done'] and entry.get('error')
                and time.monotonic() >= entry['retry_at']):
            # A failed lookup is not evidence of missing lyrics. Start a new
            # bounded retry cycle after the cooldown, even on the same song.
            del self._entries[identity]
            entry = None
            self.done = False
        if self.done:
            return
        if entry:
            self._entries.move_to_end(identity)
            if entry['done']:
                if entry['result'] is None:
                    self.done = True
                    self.status = entry['status']
                else:
                    token = self._token()
                    self.status = f'확인한 {self.name} 결과 재사용'
                    self.messages.put(('lyricsplus', {'provider': self.name, 'token': token, 'identity': identity,
                        'result': deepcopy(entry['result']), 'error': False, 'attempts': entry['attempts'],
                        'provider_status': entry['status']}))
                return
            if time.monotonic() < entry['retry_at']:
                self.status = entry['status']
                return
        # An obsolete request still owns the single worker until its event arrives.
        if self._running is not None:
            self.status = f'이전 {self.name} 조회 완료 대기'
            return
        token = self._token()
        self._running = token
        attempts = entry['attempts'] + 1 if entry else 1
        self.status = f'{self.name}에서 현재 곡의 싱크 확인 중'

        def run():
            statuses = []
            try:
                options = {'report': statuses.append} if self.reports_status else {}
                result = self.fetch(playback, reference=reference, **options)
                error, error_label = False, ''
            except Exception as failure:
                result, error = None, True
                error_label = _error_label(failure)
            self.messages.put(('lyricsplus', {'provider': self.name, 'token': token, 'identity': identity,
                'result': result, 'error': error, 'error_label': error_label, 'attempts': attempts,
                'provider_status': statuses[-1] if statuses else ''}))

        try:
            threading.Thread(target=run, daemon=True, name='lyricsplus-lookup').start()
        except Exception as failure:
            self.messages.put(('lyricsplus', {'provider': self.name, 'token': token, 'identity': identity,
                'result': None, 'error': True, 'error_label': _error_label(failure), 'attempts': attempts}))

    def receive(self, event):
        token = event.get('token')
        if token == self._running:
            self._running = None
        if (self.closed or token != self._current or event.get('identity') != self._identity
                or token is None or token[0] != self.generation):
            return None
        self._current = None
        self.pending = False
        attempts = event['attempts']
        result = event.get('result')
        if event.get('error'):
            self.done = attempts >= 2
            reason = event.get('error_label', '조회 오류')
            self.status = f'{self.name} 조회 실패 ({reason}) · ' + (
                '5분 후 다시 확인' if self.done else '15초 후 한 번 재시도')
            retry_at = time.monotonic() + (self.ERROR_COOLDOWN if self.done else self.RETRY_DELAY)
        else:
            self.done = True
            reported = event.get('provider_status')
            self.status = reported if isinstance(reported, str) and 0 < len(reported) <= 160 else (
                f'{self.name} 싱크 연결됨' if result is not None else f'검색 결과에서 검증된 {self.name} 싱크를 찾지 못함')
            retry_at = 0
        self._remember(self._identity, {'result': deepcopy(result), 'done': self.done,
            'attempts': attempts, 'retry_at': retry_at, 'status': self.status,
            'error': bool(event.get('error'))})
        return (self.key, result) if result is not None else None

    def close(self):
        self.closed = True
        self.generation += 1
        self._current = None
        self.pending = False


LyricsPlusJob = ProviderSyncJob
