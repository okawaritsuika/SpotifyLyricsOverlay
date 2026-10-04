"""Read Spotify's Windows session; control playback only on explicit requests."""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import threading
import time


@dataclass(frozen=True)
class Playback:
    title: str = ''
    artist: str = ''
    album: str = ''
    base: float = 0.0
    duration: float = 0.0
    playing: bool = False
    captured: float = 0.0
    rate: float = 1.0
    error: str = ''

    @property
    def key(self):
        return '\0'.join((self.title, self.artist, self.album))

    @property
    def has_timeline(self):
        return bool(self.title and self.duration > 0 and not self.error)

    def position(self, now=None):
        now = time.monotonic() if now is None else now
        elapsed = max(0, now - self.captured) * self.rate if self.playing else 0
        return min(self.duration, max(0, self.base + elapsed))


def timeline_position(position, updated_age, playing, duration, rate=1):
    return min(duration, max(0, position + (max(0, updated_age) * rate if playing else 0)))


def _spotify_session(manager, playing_status):
    fallback = None
    for session in manager.get_sessions():
        try:
            if 'spotify' not in session.source_app_user_model_id.lower():continue
            info = session.get_playback_info()
            if info is None:continue
            playing = info.playback_status == playing_status
        except (OSError, RuntimeError):
            # A session may disappear after enumeration. Its stale object must
            # not hide another Spotify instance that can still provide data.
            continue
        if playing:return session
        if fallback is None:fallback = session
    return fallback


class SpotifyMonitor:
    def __init__(self, messages):
        self.messages = messages
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name='spotify-readonly')
        self.control_lock = threading.Lock()

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def request_skip(self, direction):
        """Accept one explicit previous/next request without waiting on Windows.

        False means no request was started (invalid, stopped, or already busy).
        Each accepted request sends exactly one media_control completion event.
        """
        if direction not in ('previous', 'next') or self.stop_event.is_set():
            return False
        if not self.control_lock.acquire(blocking=False):
            return False
        try:
            threading.Thread(target=self._run_skip, args=(direction,), daemon=True,
                             name='spotify-track-control').start()
        except Exception:
            self.control_lock.release()
            return False
        return True

    def _run_skip(self, direction):
        try:
            success, message = asyncio.run(self._bounded_skip(direction))
        except asyncio.TimeoutError:
            success, message = False, 'Spotify 곡 이동 응답을 4초 안에 확인하지 못했습니다.'
        except asyncio.CancelledError:
            success, message = False, 'Spotify 곡 이동 요청이 취소되었습니다.'
        except ImportError:
            success, message = False, 'Spotify 곡 이동에 필요한 Windows 재생 정보 모듈이 없습니다.'
        except Exception as error:
            success, message = False, f'Spotify 곡 이동 요청 실패 ({type(error).__name__})'
        finally:
            self.control_lock.release()
        self.messages.put(('media_control', (direction, success, message)))

    async def _bounded_skip(self, direction):
        return await asyncio.wait_for(self._skip(direction), timeout=4)

    async def _skip(self, direction):
        if self.stop_event.is_set():
            return False, '가사 앱이 종료되어 곡 이동 요청을 취소했습니다.'
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as Manager,
            GlobalSystemMediaTransportControlsSessionPlaybackStatus as Status,
        )
        manager = await Manager.request_async()
        if self.stop_event.is_set():
            return False, '가사 앱이 종료되어 곡 이동 요청을 취소했습니다.'
        session = _spotify_session(manager, Status.PLAYING)
        if session is None:
            return False, 'Spotify 데스크톱 앱에서 곡을 재생해 주세요.'
        if self.stop_event.is_set():
            return False, '가사 앱이 종료되어 곡 이동 요청을 취소했습니다.'
        label = '이전 곡' if direction == 'previous' else '다음 곡'
        operation = session.try_skip_previous_async if direction == 'previous' else session.try_skip_next_async
        success = bool(await operation())
        message = (f'Spotify에 {label} 이동 요청을 보냈습니다.' if success else
                   f'Spotify가 {label} 이동 요청을 처리하지 못했습니다.')
        return success, message

    def request_playback(self, action):
        """Request an explicit play/pause action on Spotify only, without blocking Tk."""
        if action not in ('play', 'pause') or self.stop_event.is_set():
            return False
        if not self.control_lock.acquire(blocking=False):
            return False
        try:
            threading.Thread(target=self._run_playback, args=(action,), daemon=True,
                             name='spotify-playback-control').start()
        except Exception:
            self.control_lock.release()
            return False
        return True

    def _run_playback(self, action):
        try:
            success, message = asyncio.run(self._bounded_playback(action))
        except asyncio.TimeoutError:
            success, message = False, 'Spotify 재생 제어 응답을 4초 안에 확인하지 못했습니다.'
        except asyncio.CancelledError:
            success, message = False, 'Spotify 재생 제어 요청이 취소되었습니다.'
        except ImportError:
            success, message = False, 'Spotify 재생 제어에 필요한 Windows 재생 정보 모듈이 없습니다.'
        except Exception as error:
            success, message = False, f'Spotify 재생 제어 요청 실패 ({type(error).__name__})'
        finally:
            self.control_lock.release()
        self.messages.put(('media_playback', (action, success, message)))

    async def _bounded_playback(self, action):
        return await asyncio.wait_for(self._playback(action), timeout=4)

    async def _playback(self, action):
        if self.stop_event.is_set():
            return False, '가사 앱이 종료되어 재생 제어 요청을 취소했습니다.'
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as Manager,
            GlobalSystemMediaTransportControlsSessionPlaybackStatus as Status,
        )
        manager = await Manager.request_async()
        if self.stop_event.is_set():
            return False, '가사 앱이 종료되어 재생 제어 요청을 취소했습니다.'
        session = _spotify_session(manager, Status.PLAYING)
        if session is None:
            return False, 'Spotify 데스크톱 앱에서 곡을 선택해 주세요.'
        if self.stop_event.is_set():
            return False, '가사 앱이 종료되어 재생 제어 요청을 취소했습니다.'
        # Explicit actions avoid reversing a newer Spotify state with a toggle.
        operation = session.try_play_async if action == 'play' else session.try_pause_async
        success = bool(await operation())
        label = '재생' if action == 'play' else '일시정지'
        return success, (f'Spotify에 {label} 요청을 보냈습니다.' if success else
                         f'Spotify가 {label} 요청을 처리하지 못했습니다.')

    def request_seek(self, edge, expected_key):
        """Seek within the clicked track, sharing the previous/next busy guard.

        Each accepted request sends exactly one media_seek completion event.
        """
        if (edge not in ('first', 'last') or self.stop_event.is_set()
                or not isinstance(expected_key, str) or not expected_key.split('\0', 1)[0]):
            return False
        if not self.control_lock.acquire(blocking=False):
            return False
        try:
            threading.Thread(target=self._run_seek, args=(edge, expected_key), daemon=True,
                             name='spotify-position-control').start()
        except Exception:
            self.control_lock.release()
            return False
        return True

    def _run_seek(self, edge, expected_key):
        try:
            success, message = asyncio.run(self._bounded_seek(edge, expected_key))
        except asyncio.TimeoutError:
            success, message = False, 'Spotify 재생 위치 이동 응답을 4초 안에 확인하지 못했습니다.'
        except asyncio.CancelledError:
            success, message = False, 'Spotify 재생 위치 이동 요청이 취소되었습니다.'
        except ImportError:
            success, message = False, 'Spotify 재생 위치 이동에 필요한 Windows 재생 정보 모듈이 없습니다.'
        except Exception as error:
            success, message = False, f'Spotify 재생 위치 이동 요청 실패 ({type(error).__name__})'
        finally:
            self.control_lock.release()
        self.messages.put(('media_seek', (edge, success, message)))

    async def _bounded_seek(self, edge, expected_key):
        return await asyncio.wait_for(self._seek(edge, expected_key), timeout=4)

    async def _seek(self, edge, expected_key):
        if self.stop_event.is_set():
            return False, '가사 앱이 종료되어 재생 위치 이동 요청을 취소했습니다.'
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as Manager,
            GlobalSystemMediaTransportControlsSessionPlaybackStatus as Status,
        )
        manager = await Manager.request_async()
        if self.stop_event.is_set():
            return False, '가사 앱이 종료되어 재생 위치 이동 요청을 취소했습니다.'
        session = _spotify_session(manager, Status.PLAYING)
        if session is None:
            return False, 'Spotify 데스크톱 앱에서 곡을 재생해 주세요.'
        props = await session.try_get_media_properties_async()
        if self.stop_event.is_set():
            return False, '가사 앱이 종료되어 재생 위치 이동 요청을 취소했습니다.'
        if props is None or Playback(props.title, props.artist, props.album_title).key != expected_key:
            return False, '재생 중인 곡이 바뀌어 위치 이동을 취소했습니다. 다시 눌러 주세요.'
        info = session.get_playback_info()
        if not info.controls.is_playback_position_enabled:
            return False, 'Spotify에서 현재 곡의 재생 위치를 이동할 수 없습니다.'
        timeline = session.get_timeline_properties()
        start, end = timeline.start_time, timeline.end_time
        if (not isinstance(start, timedelta) or not isinstance(end, timedelta)
                or start < timedelta(0) or end <= start
                or (end // timedelta(microseconds=1)) * 10 > 2**63 - 1):
            return False, 'Spotify에서 현재 곡의 길이를 확인하지 못했습니다.'
        # Keep a paused track at its end instead of triggering the next track.
        target = start if edge == 'first' else max(start, end - timedelta(seconds=1))
        # WinRT uses 100 ns ticks; timedelta retains microsecond precision.
        ticks = (target // timedelta(microseconds=1)) * 10
        if self.stop_event.is_set():
            return False, '가사 앱이 종료되어 재생 위치 이동 요청을 취소했습니다.'
        success = bool(await session.try_change_playback_position_async(ticks))
        label = '처음' if edge == 'first' else '끝부분'
        message = (f'Spotify에 곡 {label} 이동 요청을 보냈습니다.' if success else
                   f'Spotify가 곡 {label} 이동 요청을 처리하지 못했습니다.')
        return success, message

    def _run(self):
        try:
            asyncio.run(self._poll())
        except ImportError:
            self.messages.put(('media', Playback(error='재생 정보 모듈이 없습니다. setup.ps1을 실행하세요.')))
        except Exception as error:
            self.messages.put(('media', Playback(error=f'Windows 재생 정보 연결 실패 ({type(error).__name__})')))

    async def _poll(self):
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as Manager,
            GlobalSystemMediaTransportControlsSessionPlaybackStatus as Status,
        )
        manager = None
        while not self.stop_event.is_set():
            try:
                if manager is None:
                    manager = await asyncio.wait_for(Manager.request_async(), timeout=4)
                if self.stop_event.is_set():break
                # Prefer a playing Spotify session, if more than one exists.
                session = _spotify_session(manager, Status.PLAYING)
                if session is None:
                    snapshot = Playback(error='Spotify 데스크톱 앱에서 곡을 재생하세요.')
                else:
                    props = await asyncio.wait_for(session.try_get_media_properties_async(), timeout=4)
                    if self.stop_event.is_set():break
                    timeline = session.get_timeline_properties()
                    info = session.get_playback_info()
                    playing = info.playback_status == Status.PLAYING
                    duration = timeline.end_time.total_seconds()
                    age = (datetime.now(timezone.utc) - timeline.last_updated_time).total_seconds()
                    rate = info.playback_rate or 1.0
                    base = timeline_position(timeline.position.total_seconds(), age,
                                             playing, duration, rate)
                    snapshot = Playback(props.title, props.artist, props.album_title, base,
                                        duration, playing, time.monotonic(), rate)
                if not self.stop_event.is_set():self.messages.put(('media', snapshot))
            except Exception as error:
                # Recreate a failed Windows connection instead of terminating
                # the monitor or retrying a permanently disconnected manager.
                manager = None
                if not self.stop_event.is_set():
                    self.messages.put(('media', Playback(error=f'Spotify 연결 재시도 중 ({type(error).__name__})')))
            if not self.stop_event.is_set():await asyncio.sleep(.5)
