"""Consumer Spotify integration: existing login, normal song links, no app keys."""
from datetime import datetime, timezone
import math
import os
import threading
import time
import webbrowser

from artist_metadata import playback_request, request_identity, validated_track
from background_lyrics import CatalogSearchError
from spotify_catalog import SpotifyCatalogError, parse_playlist_id, parse_track_id
from spotify_controls import TrackPlayRequest


_MAX_SAMPLE_AGE = 3
_MAX_PLAY_WAIT = 30


def _fresh(playback, now, after=None):
    captured = playback.captured
    return (playback.has_timeline and isinstance(captured, (int, float))
            and not isinstance(captured, bool) and math.isfinite(captured)
            and 0 <= now - captured <= _MAX_SAMPLE_AGE
            and (after is None or captured > after))


def _matches_playback(playback, track):
    # GSMTC does not expose a Spotify recording ID. Without complete tags and a
    # matching length, opening a song cannot prove which version is playing.
    return bool(playback.album and track.get('album')
                and validated_track(playback_request(playback), track) is not None)


class DesktopSpotify:
    def __init__(self, app):
        self.app = app
        self._controls = set()
        self._control_lock = threading.Lock()
        self._closed = False
        self.last_control_result = {}

    def close(self):
        """Stop child controls synchronously before the dictionary/app exits."""
        with self._control_lock:
            self._closed = True
            for control in self._controls:control.close()
            self._controls.clear()

    @property
    def connected(self):
        return bool(self.app.background_lyrics.spotify_authenticated)

    def search(self, title, artist='', album='', cancel=None):
        try:
            return self.app.background_lyrics.catalog_search(title, artist, album, cancel=cancel)
        except CatalogSearchError as error:
            raise SpotifyCatalogError(str(error), error.code) from error

    def _current_track(self, playback, now):
        if not _fresh(playback, now):return None
        request = playback_request(playback)
        entry = self.app.background_lyrics.artist_metadata.entries.get(request_identity(request))
        if not isinstance(entry, dict):return None
        track = validated_track(request, entry.get('track'))
        if track is None:return None
        return {**track, 'url': 'https://open.spotify.com/track/' + track['id'],
                'is_playable': True if playback.playing else None,
                'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                'verification': 'observed_playback' if playback.playing else 'observed_metadata'}

    def current_track(self):
        return self._current_track(self.app.playback, time.monotonic())

    @staticmethod
    def _open(track_id):
        try:
            os.startfile('spotify:track:' + track_id)
            return True
        except (OSError, AttributeError):
            try:opened = webbrowser.open('https://open.spotify.com/track/' + track_id)
            except (OSError, webbrowser.Error) as error:
                raise SpotifyCatalogError('Spotify 곡 링크를 열지 못했습니다.', 'open_failed') from error
            if not opened:
                raise SpotifyCatalogError('Spotify 곡 링크를 열지 못했습니다.', 'open_failed')
            return False

    def open(self, track):
        track_id = parse_track_id(track.get('id'))
        if not track_id:raise ValueError('연결할 Spotify 곡 링크가 올바르지 않습니다.')
        self._open(track_id)

    @staticmethod
    def open_playlist(value):
        playlist_id = parse_playlist_id(value)
        if not playlist_id:
            raise ValueError('Spotify 재생목록 공유 링크를 입력해 주세요.')
        try:
            os.startfile('spotify:playlist:' + playlist_id)
        except (OSError, AttributeError) as error:
            raise ValueError('Spotify 데스크톱 앱에서 재생목록을 열지 못했습니다.') from error
        return playlist_id

    def play(self, track, cancel=None, timeout=8):
        """Open and invoke the requested row; verify a new Windows playing sample.

        False means the song was opened without confirmed playback (or the
        request was cancelled). Spotify may require the user to press Play.
        Never send a global play key, which could start an unrelated song.
        """
        cancel = cancel or threading.Event()
        if cancel.is_set() or self._closed:return False
        self.last_control_result = {}
        track_id = parse_track_id(track.get('id'))
        if not track_id:raise ValueError('Spotify 곡 링크를 먼저 연결하세요.')
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 < timeout <= _MAX_PLAY_WAIT):
            raise ValueError('재생 확인 시간은 0초 초과 30초 이하여야 합니다.')
        requested_at = time.monotonic()
        if not self._open(track_id):return False
        deadline = requested_at + timeout
        track = {**track, 'id': track_id}
        control = None
        try:
            while not cancel.is_set() and not self._closed:
                now = time.monotonic()
                if now >= deadline:break
                playback = self.app.playback
                if playback.playing and _fresh(playback, now, after=requested_at):
                    current = self._current_track(playback, now)
                    # A different known ID must not be overridden by similar tags.
                    if (current is None or current['id'] == track_id) and _matches_playback(playback, track):
                        return not cancel.is_set()
                # Give normal URI autoplay a chance, then invoke the verified row.
                # The worker cannot emit a global Play or select a different track.
                if control is None and now - requested_at >= .6 and not cancel.is_set():
                    with self._control_lock:
                        if self._closed or cancel.is_set():break
                        control = TrackPlayRequest(track)
                        self._controls.add(control)
                cancel.wait(min(.2, max(0, deadline - time.monotonic())))
            return False
        finally:
            if control is not None:
                with self._control_lock:
                    if control in self._controls:
                        control.close()
                        self._controls.discard(control)
                    self.last_control_result = control.result
