"""Explicit track controls using fake WinRT sessions; never touch real playback."""
import asyncio
from datetime import timedelta
import queue
import sys
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from media import Playback, SpotifyMonitor


class FakeSession:
    def __init__(self, app='Spotify.exe', playing=True, result=True, error=None):
        self.source_app_user_model_id = app
        self.playing = playing
        self.result = result
        self.error = error
        self.calls = []
        self.entered = threading.Event()
        self.release = None
        self.properties = SimpleNamespace(title='Song', artist='Artist', album_title='Album')
        self.properties_entered = threading.Event()
        self.properties_release = None
        self.timeline = SimpleNamespace(start_time=timedelta(0), end_time=timedelta(seconds=180))
        self.seek_enabled = True

    def get_playback_info(self):
        return SimpleNamespace(playback_status='playing' if self.playing else 'paused',
                               controls=SimpleNamespace(is_playback_position_enabled=self.seek_enabled))

    async def try_get_media_properties_async(self):
        self.properties_entered.set()
        if self.properties_release is not None:
            while not self.properties_release.is_set():
                await asyncio.sleep(.005)
        return self.properties

    def get_timeline_properties(self):
        return self.timeline

    async def try_change_playback_position_async(self, ticks):
        return await self.skip(('seek', ticks))

    async def skip(self, direction):
        self.calls.append(direction)
        self.entered.set()
        if self.release is not None:
            while not self.release.is_set():
                await asyncio.sleep(.005)
        if self.error:raise self.error
        return self.result

    async def try_skip_previous_async(self):
        return await self.skip('previous')

    async def try_skip_next_async(self):
        return await self.skip('next')

    async def try_play_async(self):
        return await self.skip('play')

    async def try_pause_async(self):
        return await self.skip('pause')


class MediaControlTests(unittest.TestCase):
    def setUp(self):
        self.messages = queue.Queue()
        self.monitor = SpotifyMonitor(self.messages)
        self.track_key = Playback('Song', 'Artist', 'Album').key
        self.sessions = []
        self.manager_entered = threading.Event()
        self.manager_release = None
        test = self
        class Manager:
            @staticmethod
            async def request_async():
                test.manager_entered.set()
                if test.manager_release is not None:
                    while not test.manager_release.is_set():
                        await asyncio.sleep(.005)
                return SimpleNamespace(get_sessions=lambda: test.sessions)
        modules = {name: ModuleType(name) for name in (
            'winrt', 'winrt.windows', 'winrt.windows.media', 'winrt.windows.media.control')}
        module = modules['winrt.windows.media.control']
        module.GlobalSystemMediaTransportControlsSessionManager = Manager
        module.GlobalSystemMediaTransportControlsSessionPlaybackStatus = SimpleNamespace(PLAYING='playing')
        self.modules = patch.dict(sys.modules, modules)
        self.modules.start()

    def tearDown(self):
        self.monitor.stop()
        if self.manager_release:self.manager_release.set()
        for session in self.sessions:
            if session.release:session.release.set()
            if session.properties_release:session.properties_release.set()
        self.modules.stop()

    def completion(self, event_name='media_control'):
        event = self.messages.get(timeout=2)
        self.assertEqual(event[0], event_name)
        self.assertEqual(len(event[1]), 3)
        self.assertIsInstance(event[1][1], bool)
        self.assertIsInstance(event[1][2], str)
        return event[1]

    def test_previous_and_next_call_only_requested_method(self):
        session = FakeSession()
        self.sessions = [session]
        for direction in ('previous', 'next'):
            self.assertTrue(self.monitor.request_skip(direction))
            result = self.completion()
            self.assertEqual(result[:2], (direction, True))
            self.assertIn('Spotify', result[2])
        self.assertEqual(session.calls, ['previous', 'next'])
        self.assertTrue(self.messages.empty())

    def test_play_pause_targets_spotify_with_explicit_action(self):
        other=FakeSession('OtherPlayer.exe',playing=True)
        spotify=FakeSession(playing=False)
        self.sessions=[other,spotify]
        for action in ('play','pause'):
            self.assertTrue(self.monitor.request_playback(action))
            self.assertEqual(self.completion('media_playback')[:2],(action,True))
        self.assertEqual(spotify.calls,['play','pause'])
        self.assertEqual(other.calls,[])

    def test_play_pause_requires_spotify_and_releases_guard_after_failure(self):
        self.sessions=[FakeSession('OtherPlayer.exe')]
        self.assertTrue(self.monitor.request_playback('play'))
        self.assertFalse(self.completion('media_playback')[1])
        spotify=FakeSession(result=False)
        self.sessions.append(spotify)
        self.assertTrue(self.monitor.request_playback('pause'))
        self.assertFalse(self.completion('media_playback')[1])
        spotify.result=True
        self.assertTrue(self.monitor.request_playback('play'))
        self.assertTrue(self.completion('media_playback')[1])

    def test_pending_play_pause_blocks_seek_skip_and_duplicate_actions(self):
        spotify=FakeSession();spotify.release=threading.Event()
        self.sessions=[spotify]
        self.assertTrue(self.monitor.request_playback('pause'))
        self.assertTrue(spotify.entered.wait(timeout=1))
        self.assertFalse(self.monitor.request_playback('play'))
        self.assertFalse(self.monitor.request_skip('next'))
        self.assertFalse(self.monitor.request_seek('last',self.track_key))
        spotify.release.set()
        self.assertTrue(self.completion('media_playback')[1])

    def test_play_pause_cancel_timeout_and_stop_do_not_leave_busy_guard(self):
        spotify=FakeSession(error=asyncio.CancelledError())
        self.sessions=[spotify]
        self.assertTrue(self.monitor.request_playback('pause'))
        self.assertFalse(self.completion('media_playback')[1])
        wait_for=asyncio.wait_for
        async def shorter(awaitable,timeout):
            self.assertEqual(timeout,4)
            return await wait_for(awaitable,timeout=.02)
        self.manager_release=threading.Event()
        with patch('media.asyncio.wait_for',side_effect=shorter):
            self.assertTrue(self.monitor.request_playback('play'))
            self.assertFalse(self.completion('media_playback')[1])
        self.assertFalse(self.monitor.control_lock.locked())
        self.monitor.stop()
        self.assertFalse(self.monitor.request_playback('pause'))
        self.assertFalse(self.monitor.request_playback('toggle'))

    def test_stop_during_play_pause_lookup_prevents_operation(self):
        self.manager_release=threading.Event();spotify=FakeSession()
        self.sessions=[spotify]
        self.assertTrue(self.monitor.request_playback('pause'))
        self.assertTrue(self.manager_entered.wait(timeout=1))
        self.monitor.stop();self.manager_release.set()
        self.assertFalse(self.completion('media_playback')[1])
        self.assertEqual(spotify.calls,[])

    def test_playing_spotify_is_preferred_over_paused_spotify_and_other_apps(self):
        other = FakeSession('OtherPlayer.exe', playing=True)
        paused = FakeSession('Spotify.exe', playing=False)
        active = FakeSession('SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify', playing=True)
        self.sessions = [other, paused, active]
        self.assertTrue(self.monitor.request_skip('next'))
        self.assertTrue(self.completion()[1])
        self.assertEqual(active.calls, ['next'])
        self.assertEqual(paused.calls, [])
        self.assertEqual(other.calls, [])

    def test_paused_spotify_can_accept_control_without_controlling_playing_other_app(self):
        other = FakeSession('OtherPlayer.exe', playing=True)
        spotify = FakeSession(playing=False)
        self.sessions = [other, spotify]
        self.assertTrue(self.monitor.request_skip('previous'))
        self.assertTrue(self.completion()[1])
        self.assertEqual(spotify.calls, ['previous'])
        self.assertEqual(other.calls, [])

    def test_no_spotify_session_reports_failure_without_global_fallback(self):
        other = FakeSession('OtherPlayer.exe')
        self.sessions = [other]
        self.assertTrue(self.monitor.request_skip('next'))
        direction, success, message = self.completion()
        self.assertEqual((direction, success), ('next', False))
        self.assertIn('Spotify 데스크톱', message)
        self.assertEqual(other.calls, [])

    def test_stale_spotify_session_does_not_hide_a_working_session(self):
        for unavailable in (OSError('removed session'),RuntimeError('closed session'),None):
            with self.subTest(unavailable=type(unavailable).__name__):
                stale=FakeSession();active=FakeSession()
                if unavailable is None:stale.get_playback_info=lambda:None
                else:
                    def failed(error=unavailable):raise error
                    stale.get_playback_info=failed
                self.sessions=[stale,active]
                self.assertTrue(self.monitor.request_skip('next'))
                self.assertTrue(self.completion()[1])
                self.assertEqual(active.calls,['next'])
                self.assertEqual(stale.calls,[])

    def test_unavailable_spotify_sessions_do_not_control_another_player(self):
        stale=FakeSession();other=FakeSession('OtherPlayer.exe')
        stale.get_playback_info=lambda:None
        self.sessions=[stale,other]
        self.assertTrue(self.monitor.request_skip('next'))
        self.assertFalse(self.completion()[1])
        self.assertEqual((stale.calls,other.calls),([],[]))

    def test_stale_session_before_paused_spotify_does_not_block_seek(self):
        stale=FakeSession();paused=FakeSession(playing=False)
        def failed():raise OSError('removed session')
        stale.get_playback_info=failed
        self.sessions=[stale,paused]
        self.assertTrue(self.monitor.request_seek('first',self.track_key))
        self.assertTrue(self.completion('media_seek')[1])
        self.assertEqual(paused.calls,[('seek',0)])
        self.assertEqual(stale.calls,[])

    def test_rejected_control_reports_failure_and_releases_busy_guard(self):
        session = FakeSession(result=False)
        self.sessions = [session]
        self.assertTrue(self.monitor.request_skip('next'))
        self.assertFalse(self.completion()[1])
        session.result = True
        self.assertTrue(self.monitor.request_skip('previous'))
        self.assertTrue(self.completion()[1])

    def test_winrt_exception_is_nonfatal_and_reports_type(self):
        self.sessions = [FakeSession(error=OSError('sensitive detail must not be printed'))]
        self.assertTrue(self.monitor.request_skip('next'))
        result = self.completion()
        self.assertFalse(result[1])
        self.assertIn('OSError', result[2])
        self.assertNotIn('sensitive detail', result[2])

    def test_cancelled_windows_operation_still_emits_one_completion(self):
        self.sessions = [FakeSession(error=asyncio.CancelledError())]
        self.assertTrue(self.monitor.request_skip('next'))
        result = self.completion()
        self.assertFalse(result[1])
        self.assertIn('취소', result[2])
        self.assertTrue(self.messages.empty())

    def test_request_does_not_block_and_only_one_operation_runs_at_a_time(self):
        session = FakeSession()
        session.release = threading.Event()
        self.sessions = [session]
        self.assertTrue(self.monitor.request_skip('next'))
        self.assertTrue(session.entered.wait(timeout=1))
        self.assertFalse(self.monitor.request_skip('previous'))
        self.assertTrue(self.messages.empty())
        self.assertEqual(session.calls, ['next'])
        session.release.set()
        self.assertTrue(self.completion()[1])
        self.assertTrue(self.monitor.request_skip('previous'))
        self.assertTrue(self.completion()[1])

    def test_stopped_monitor_and_invalid_directions_are_not_accepted(self):
        self.sessions = [FakeSession()]
        for direction in ('play', '', None, 'NEXT'):
            self.assertFalse(self.monitor.request_skip(direction))
        self.monitor.stop()
        self.assertFalse(self.monitor.request_skip('next'))
        self.assertTrue(self.messages.empty())
        self.assertFalse(self.manager_entered.is_set())

    def test_stop_during_manager_lookup_prevents_later_skip(self):
        self.manager_release = threading.Event()
        session = FakeSession()
        self.sessions = [session]
        self.assertTrue(self.monitor.request_skip('next'))
        self.assertTrue(self.manager_entered.wait(timeout=1))
        self.monitor.stop()
        self.manager_release.set()
        self.assertFalse(self.completion()[1])
        self.assertEqual(session.calls, [])

    def test_entire_operation_uses_four_second_timeout_and_reports_timeout(self):
        wait_for = asyncio.wait_for
        timeouts = []
        async def shorter_timeout(awaitable, timeout):
            timeouts.append(timeout)
            return await wait_for(awaitable, timeout=.02)
        self.manager_release = threading.Event()
        with patch('media.asyncio.wait_for', side_effect=shorter_timeout):
            self.assertTrue(self.monitor.request_skip('next'))
            result = self.completion()
        self.assertEqual(timeouts, [4])
        self.assertFalse(result[1])
        self.assertIn('4초', result[2])

    def test_control_timeout_also_covers_the_session_skip_call(self):
        wait_for = asyncio.wait_for
        async def shorter_timeout(awaitable, timeout):
            return await wait_for(awaitable, timeout=.02)
        session = FakeSession()
        session.release = threading.Event()
        self.sessions = [session]
        with patch('media.asyncio.wait_for', side_effect=shorter_timeout):
            self.assertTrue(self.monitor.request_skip('previous'))
            self.assertFalse(self.completion()[1])
        self.assertEqual(session.calls, ['previous'])

    def test_missing_winrt_reports_failure_without_starting_any_real_operation(self):
        with patch.object(self.monitor, '_skip', new=AsyncMock(side_effect=ImportError('missing'))):
            self.assertTrue(self.monitor.request_skip('next'))
            result = self.completion()
        self.assertFalse(result[1])
        self.assertIn('모듈', result[2])

    def test_worker_start_failure_returns_false_and_does_not_stay_busy(self):
        with patch('media.threading.Thread.start', side_effect=RuntimeError('cannot start')):
            self.assertFalse(self.monitor.request_skip('next'))
        self.assertTrue(self.messages.empty())
        self.sessions = [FakeSession()]
        self.assertTrue(self.monitor.request_skip('next'))
        self.assertTrue(self.completion()[1])

    def test_seek_first_and_last_use_exact_100_nanosecond_ticks(self):
        session = FakeSession()
        session.timeline.end_time = timedelta(seconds=201, microseconds=234567)
        self.sessions = [session]
        for edge in ('first', 'last'):
            self.assertTrue(self.monitor.request_seek(edge, self.track_key))
            self.assertEqual(self.completion('media_seek')[:2], (edge, True))
        self.assertEqual(session.calls, [('seek', 0), ('seek', 2002345670)])
        self.assertTrue(self.messages.empty())

    def test_seek_respects_nonzero_start_and_clamps_short_track_end(self):
        session = FakeSession()
        session.timeline.start_time = timedelta(seconds=2)
        session.timeline.end_time = timedelta(seconds=2.5)
        self.sessions = [session]
        for edge in ('first', 'last'):
            self.assertTrue(self.monitor.request_seek(edge, self.track_key))
            self.assertTrue(self.completion('media_seek')[1])
        self.assertEqual(session.calls, [('seek', 20000000), ('seek', 20000000)])

    def test_seek_prefers_playing_spotify_and_does_not_control_other_sessions(self):
        other = FakeSession('OtherPlayer.exe')
        paused = FakeSession(playing=False)
        active = FakeSession('SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify')
        self.sessions = [other, paused, active]
        self.assertTrue(self.monitor.request_seek('first', self.track_key))
        self.assertTrue(self.completion('media_seek')[1])
        self.assertEqual(active.calls, [('seek', 0)])
        self.assertEqual(paused.calls, [])
        self.assertEqual(other.calls, [])

    def test_seek_works_while_paused_without_resuming_or_skipping(self):
        session = FakeSession(playing=False)
        self.sessions = [session]
        self.assertTrue(self.monitor.request_seek('last', self.track_key))
        self.assertTrue(self.completion('media_seek')[1])
        self.assertFalse(session.playing)
        self.assertEqual(session.calls, [('seek', 1790000000)])

    def test_seek_rechecks_full_track_identity_after_click(self):
        session = FakeSession()
        self.sessions = [session]
        for field in ('title', 'artist', 'album_title'):
            with self.subTest(field=field):
                self.manager_entered.clear()
                self.manager_release = threading.Event()
                self.assertTrue(self.monitor.request_seek('last', self.track_key))
                self.assertTrue(self.manager_entered.wait(timeout=1))
                original = getattr(session.properties, field)
                setattr(session.properties, field, 'Different')
                self.manager_release.set()
                result = self.completion('media_seek')
                self.assertFalse(result[1])
                self.assertIn('곡이 바뀌어', result[2])
                setattr(session.properties, field, original)
        self.assertEqual(session.calls, [])

    def test_seek_missing_properties_or_disabled_position_control_never_seeks(self):
        session = FakeSession()
        self.sessions = [session]
        original = session.properties
        session.properties = None
        self.assertTrue(self.monitor.request_seek('first', self.track_key))
        self.assertFalse(self.completion('media_seek')[1])
        session.properties = original
        session.seek_enabled = False
        self.assertTrue(self.monitor.request_seek('last', self.track_key))
        result = self.completion('media_seek')
        self.assertFalse(result[1])
        self.assertIn('이동할 수 없습니다', result[2])
        self.assertEqual(session.calls, [])

    def test_seek_invalid_timeline_never_sends_a_position(self):
        session = FakeSession()
        self.sessions = [session]
        zero = timedelta(0)
        for start, end in ((zero, zero), (zero, timedelta(seconds=-1)),
                           (timedelta(seconds=-1), timedelta(seconds=10)),
                           (timedelta(seconds=10), timedelta(seconds=5)),
                           (None, timedelta(seconds=10)), (zero, float('inf')),
                           (zero, timedelta.max)):
            with self.subTest(start=start, end=end):
                session.timeline.start_time, session.timeline.end_time = start, end
                self.assertTrue(self.monitor.request_seek('last', self.track_key))
                result = self.completion('media_seek')
                self.assertFalse(result[1])
                self.assertIn('길이', result[2])
        self.assertEqual(session.calls, [])

    def test_seek_without_spotify_does_not_control_other_app(self):
        other = FakeSession('OtherPlayer.exe')
        self.sessions = [other]
        self.assertTrue(self.monitor.request_seek('first', self.track_key))
        result = self.completion('media_seek')
        self.assertFalse(result[1])
        self.assertIn('Spotify 데스크톱', result[2])
        self.assertEqual(other.calls, [])

    def test_seek_rejection_or_exception_emits_one_failure_and_unlocks(self):
        session = FakeSession(result=False)
        self.sessions = [session]
        for error in (None, OSError('sensitive detail'), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                session.error = error
                self.assertTrue(self.monitor.request_seek('last', self.track_key))
                result = self.completion('media_seek')
                self.assertFalse(result[1])
                self.assertNotIn('sensitive detail', result[2])
                self.assertTrue(self.messages.empty())
        session.error, session.result = None, True
        self.assertTrue(self.monitor.request_skip('next'))
        self.assertTrue(self.completion()[1])

    def test_seek_and_skip_share_busy_guard_in_both_directions(self):
        session = FakeSession()
        self.sessions = [session]
        session.release = threading.Event()
        self.assertTrue(self.monitor.request_skip('previous'))
        self.assertTrue(session.entered.wait(timeout=1))
        self.assertFalse(self.monitor.request_seek('first', self.track_key))
        session.release.set()
        self.assertTrue(self.completion()[1])
        session.entered.clear()
        session.release = threading.Event()
        self.assertTrue(self.monitor.request_seek('last', self.track_key))
        self.assertTrue(session.entered.wait(timeout=1))
        self.assertFalse(self.monitor.request_skip('next'))
        self.assertFalse(self.monitor.request_seek('first', self.track_key))
        self.assertTrue(self.messages.empty())
        session.release.set()
        self.assertTrue(self.completion('media_seek')[1])
        self.assertEqual(session.calls, ['previous', ('seek', 1790000000)])

    def test_seek_invalid_edge_missing_identity_or_stopped_monitor_not_accepted(self):
        for edge, key in (('next', self.track_key), (None, self.track_key),
                          ('LAST', self.track_key), ('first', ''),
                          ('last', None), ('first', Playback().key)):
            self.assertFalse(self.monitor.request_seek(edge, key))
        self.monitor.stop()
        self.assertFalse(self.monitor.request_seek('first', self.track_key))
        self.assertFalse(self.manager_entered.is_set())
        self.assertTrue(self.messages.empty())

    def test_seek_stop_during_properties_lookup_prevents_position_change(self):
        session = FakeSession()
        session.properties_release = threading.Event()
        self.sessions = [session]
        self.assertTrue(self.monitor.request_seek('last', self.track_key))
        self.assertTrue(session.properties_entered.wait(timeout=1))
        self.monitor.stop()
        session.properties_release.set()
        self.assertFalse(self.completion('media_seek')[1])
        self.assertEqual(session.calls, [])

    def test_seek_four_second_timeout_covers_manager_properties_and_control(self):
        wait_for = asyncio.wait_for
        timeouts = []
        async def shorter_timeout(awaitable, timeout):
            timeouts.append(timeout)
            return await wait_for(awaitable, timeout=.02)
        for phase in ('manager', 'properties', 'control'):
            with self.subTest(phase=phase):
                session = FakeSession()
                self.sessions = [session]
                self.manager_release = threading.Event() if phase == 'manager' else None
                session.properties_release = threading.Event() if phase == 'properties' else None
                session.release = threading.Event() if phase == 'control' else None
                with patch('media.asyncio.wait_for', side_effect=shorter_timeout):
                    self.assertTrue(self.monitor.request_seek('first', self.track_key))
                    result = self.completion('media_seek')
                self.assertFalse(result[1])
                self.assertIn('4초', result[2])
                self.assertTrue(self.messages.empty())
                self.assertEqual(session.calls, [('seek', 0)] if phase == 'control' else [])
        self.assertEqual(timeouts, [4, 4, 4])

    def test_seek_missing_module_reports_failure(self):
        with patch.object(self.monitor, '_seek', new=AsyncMock(side_effect=ImportError('missing'))):
            self.assertTrue(self.monitor.request_seek('first', self.track_key))
            result = self.completion('media_seek')
        self.assertFalse(result[1])
        self.assertIn('모듈', result[2])
        self.assertTrue(self.messages.empty())

    def test_seek_worker_start_failure_unlocks_without_completion(self):
        with patch('media.threading.Thread.start', side_effect=RuntimeError('cannot start')):
            self.assertFalse(self.monitor.request_seek('first', self.track_key))
        self.assertTrue(self.messages.empty())
        self.sessions = [FakeSession()]
        self.assertTrue(self.monitor.request_seek('first', self.track_key))
        self.assertTrue(self.completion('media_seek')[1])


if __name__ == '__main__':
    unittest.main()
