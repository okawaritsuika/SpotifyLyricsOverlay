"""Provider scheduling and cue mapping without network, browser, or UI services."""
from dataclasses import replace
import io
from pathlib import Path
import queue
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from background_lyrics import BackgroundLyrics, SpotifyJob, mapped_times,prefer_spotify_timing
from lyrics import Line
from media import Playback


def result(source='LRCLIB', times=None, texts=('青い空を見る', '花を描く', '最後の言葉')):
    return {'source': source, 'lines': tuple(Line(text, '', '') for text in texts),
            'times': [10, 20, 30] if times is None else times, 'provider_id': 'test'}


class DeferredThread:
    pending = []

    def __init__(self, target, **kwargs):
        self.target = target

    def start(self):
        self.pending.append(self.target)


class FakeSpotifyJob:
    def __init__(self, base, profile, messages, visible=False):
        self.commands = []
        self.profile = profile
        self.visible = visible
        self.closed = False

    def send(self, command):
        self.commands.append(command)

    def close(self):
        self.closed = True


class BackgroundLyricsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.messages = queue.Queue()
        self.fetch = Mock(return_value=result())
        self.service = BackgroundLyrics('.', self.temp.name, self.messages,
                                        job_factory=FakeSpotifyJob, fetch=self.fetch)
        self.playback = Playback('Test Song', 'Singer', 'Album', 12, 90, True, 100)
        DeferredThread.pending = []
        self.threads = patch('background_lyrics.threading.Thread', DeferredThread)
        self.clock = patch('background_lyrics.time.monotonic', return_value=100)
        self.threads.start()
        self.now = self.clock.start()

    def tearDown(self):
        self.service.close()
        self.clock.stop()
        self.threads.stop()
        self.temp.cleanup()

    def finish_public(self):
        DeferredThread.pending.pop(0)()
        kind, (sender, event) = self.messages.get_nowait()
        self.assertEqual(kind, 'background_lyrics')
        return self.service.receive(sender, event)

    def spotify_event(self, **updates):
        return {'kind': 'lyrics', 'key': self.service.request_key, 'track_id': 'track123',
                'lines': [{'text': line.original, 'time': stamp}
                          for line, stamp in zip(result()['lines'], result()['times'])], **updates}

    def test_public_lyrics_request_needs_no_spotify_job_or_screen(self):
        self.service.observe(self.playback)
        self.assertIsNone(self.service.job)
        self.assertEqual(self.finish_public(), (self.playback.key, result()))
        self.fetch.assert_called_once_with(self.playback)
        self.assertEqual(list(Path(self.temp.name).rglob('*')), [])
        for _ in range(5):
            self.service.observe(self.playback)
        self.assertEqual(DeferredThread.pending, [])

    def test_spotify_pending_defers_public_lookup_until_current_terminal_result(self):
        self.service.connect()
        self.service.observe(self.playback)
        self.assertTrue(self.service.spotify_pending)
        self.assertEqual(DeferredThread.pending, [])
        self.service.receive(self.service.job,{'kind':'status','key':self.service.request_key,
                            'state':'ready','text':'connected','authenticated':True})
        self.service.observe(self.playback)
        self.assertEqual(DeferredThread.pending, [])
        self.service.receive(self.service.job,{'kind':'status','key':self.service.request_key,
                            'state':'not_found','text':'not found'})
        self.service.observe(self.playback)
        self.assertEqual(self.finish_public(),(self.playback.key,result()))

    def test_partial_spotify_result_keeps_public_lookup_enabled(self):
        self.service.connect()
        self.service.observe(self.playback)
        event=self.spotify_event(lines=[{'text':'first','time':10},{'text':'second','time':None}])
        self.assertIsNotNone(self.service.receive(self.service.job,event))
        for _ in range(3):
            self.service.observe(self.playback,spotify_sync_present=False)
        self.assertTrue(self.service.spotify_sync_present)
        self.assertTrue(self.service.public_enabled)
        self.assertEqual(len(DeferredThread.pending),1)
        self.assertEqual(self.finish_public(),(self.playback.key,result()))

    def test_spotify_hint_does_not_suppress_public_without_a_complete_cache(self):
        self.service.observe(self.playback,spotify_sync_present=True)
        self.assertTrue(self.service.public_enabled)
        self.assertTrue(self.service.spotify_sync_present)
        self.assertEqual(len(DeferredThread.pending),1)
        self.service.observe(self.playback,spotify_sync_present=False)
        self.assertFalse(self.service.spotify_sync_present)
        self.assertEqual(self.finish_public(),(self.playback.key,result()))

    def test_spotify_partial_result_preserves_public_already_in_flight(self):
        self.service.observe(self.playback)
        self.service.connect()
        self.service.receive(self.service.job,self.spotify_event(
            lines=[{'text':'first','time':10},{'text':'second','time':None}]))
        self.assertEqual(self.finish_public(),(self.playback.key,result()))
        self.service.observe(self.playback)
        self.assertEqual(DeferredThread.pending, [])
        self.assertTrue(self.service.public_done)

    def test_public_result_can_arrive_while_spotify_lookup_is_pending(self):
        self.service.observe(self.playback)
        self.service.connect()
        self.assertTrue(self.service.spotify_pending)
        self.assertEqual(self.finish_public(),(self.playback.key,result()))
        self.service.receive(self.service.job,{'kind':'status','key':self.service.request_key,
                            'state':'error','text':'failed'})
        self.service.observe(self.playback)
        self.assertEqual(DeferredThread.pending,[])

    def test_valid_unsynced_response_releases_received_sync_but_keeps_current_cache_hint(self):
        self.service.connect()
        self.service.observe(self.playback)
        self.service.receive(self.service.job,self.spotify_event())
        untimed=self.spotify_event(lines=[{'text':'first','time':None}])
        self.service.observe(self.playback,spotify_sync_present=True)
        self.service.receive(self.service.job,untimed)
        self.assertTrue(self.service.spotify_sync_present)
        self.service.observe(self.playback,spotify_sync_present=False)
        self.assertFalse(self.service.spotify_sync_present)
        self.assertEqual(self.finish_public(),(self.playback.key,result()))

    def test_other_recording_and_changed_duration_release_received_spotify_sync(self):
        self.service.connect()
        self.service.observe(self.playback)
        self.service.receive(self.service.job,self.spotify_event())
        old=self.spotify_event()
        longer=replace(self.playback,duration=91)
        self.service.observe(longer)
        self.assertFalse(self.service.spotify_sync_present)
        self.assertTrue(self.service.spotify_pending)
        self.assertIsNone(self.service.receive(self.service.job,old))
        self.service.receive(self.service.job,self.spotify_event())
        self.service.observe(replace(longer,title='Other'))
        self.assertFalse(self.service.spotify_sync_present)
        self.assertTrue(self.service.spotify_pending)
        self.assertEqual(DeferredThread.pending, [])

    def test_terminal_spotify_states_allow_public_fallback_without_sync(self):
        self.service.connect()
        for state in ('not_found','unsynced','error','login_required','closed'):
            with self.subTest(state=state):
                playback=replace(self.playback,title=state)
                self.service.observe(playback)
                self.assertEqual(DeferredThread.pending, [])
                self.service.receive(self.service.job,{'kind':'status','key':self.service.request_key,
                                    'state':state,'text':state})
                self.service.observe(playback)
                self.assertEqual(self.finish_public(),(playback.key,result()))

    def test_next_track_after_failed_login_uses_public_fallback_without_opening_lyrics(self):
        from spotify_worker import SpotifyBrowser
        self.service.connect(visible=False)
        browser=SpotifyBrowser(Mock(),queue.Queue(),Mock())
        def receive(kind,**event):
            self.service.receive(self.service.job,{'kind':kind,**event})
        with patch('spotify_worker.emit',side_effect=receive):
            browser.receive(('token',None,{'isAnonymous':True}))
            for title in ('First track','Next track'):
                playback=replace(self.playback,title=title)
                self.service.observe(playback)
                self.assertTrue(self.service.spotify_pending)
                browser.command(self.service.job.commands[-1])
                self.assertFalse(self.service.spotify_pending)
                self.service.observe(playback)
                self.assertEqual(self.finish_public(),(playback.key,result()))
        browser.window.show.assert_not_called()

    def test_old_track_result_is_rejected_and_current_track_can_start_next(self):
        self.service.observe(self.playback)
        other = replace(self.playback, title='Other')
        self.service.observe(other)
        self.assertIsNone(self.finish_public())
        self.service.observe(other)
        self.assertEqual(self.finish_public(), (other.key, result()))

    def test_a_b_a_rejects_first_a_public_and_spotify_results(self):
        self.service.observe(self.playback)
        self.service.connect()
        old = self.spotify_event()
        first_key = self.service.request_key
        self.service.observe(replace(self.playback, title='Other'))
        self.service.observe(self.playback)
        self.assertNotEqual(first_key, self.service.request_key)
        self.assertIsNone(self.service.receive(self.service.job, old))
        self.assertIsNone(self.finish_public())
        self.service.receive(self.service.job,{'kind':'status','key':self.service.request_key,
                            'state':'not_found','text':'current lookup finished'})
        self.service.observe(self.playback)
        self.assertEqual(self.finish_public(), (self.playback.key, result()))

    def test_disabling_online_invalidates_pending_requests_and_cancels_lookup(self):
        self.service.observe(self.playback)
        self.service.connect()
        old = self.spotify_event()
        self.service.observe(self.playback, enabled=False)
        self.assertEqual(self.service.job.commands[-1]['key'], '')
        self.assertIsNone(self.service.receive(self.service.job, old))
        self.assertIsNone(self.finish_public())
        self.service.observe(self.playback, enabled=False)
        self.assertEqual(DeferredThread.pending, [])
        self.service.observe(self.playback, enabled=True)
        self.assertEqual(DeferredThread.pending, [])
        self.service.receive(self.service.job,{'kind':'status','key':self.service.request_key,
                            'state':'not_found','text':'current lookup finished'})
        self.service.observe(self.playback)
        self.assertEqual(self.finish_public(), (self.playback.key, result()))

    def test_disabling_public_provider_never_fetches_but_spotify_still_works(self):
        self.service.observe(self.playback, public_enabled=False)
        self.service.connect()
        for _ in range(3):
            self.service.observe(self.playback, public_enabled=False)
        self.fetch.assert_not_called()
        self.assertEqual(DeferredThread.pending, [])
        self.assertIn('꺼짐', self.service.status)
        self.assertEqual(self.service.receive(self.service.job, self.spotify_event())[1]['source'], 'Spotify')

    def test_disabling_public_provider_discards_pending_public_result(self):
        self.service.observe(self.playback)
        self.service.observe(self.playback, public_enabled=False)
        self.assertIsNone(self.finish_public())
        self.assertIn('꺼짐', self.service.status)
        self.service.observe(self.playback, public_enabled=False)
        self.assertEqual(DeferredThread.pending, [])
        self.assertEqual(self.fetch.call_count, 1)

    def test_provider_failures_retry_once_after_delay_then_stop(self):
        self.fetch.side_effect = TimeoutError('offline')
        self.service.observe(self.playback)
        self.assertIsNone(self.finish_public())
        self.assertFalse(self.service.public_done)
        self.now.return_value = 114.9
        self.service.observe(self.playback)
        self.assertEqual(DeferredThread.pending, [])
        self.now.return_value = 115
        self.service.observe(self.playback)
        self.assertIsNone(self.finish_public())
        self.assertTrue(self.service.public_done)
        self.now.return_value = 1_000
        self.service.observe(self.playback)
        self.assertEqual(DeferredThread.pending, [])
        self.assertEqual(self.fetch.call_count, 2)

    def test_no_match_is_terminal_without_repeated_requests(self):
        self.fetch.return_value = None
        self.service.observe(self.playback)
        self.assertIsNone(self.finish_public())
        self.service.observe(self.playback)
        self.assertEqual(DeferredThread.pending, [])
        self.assertIn('없음', self.service.status)

    def test_provider_rejection_detail_survives_the_background_worker(self):
        self.service.reports_status=True
        def rejected(playback, report):
            report('LRCLIB 후보 시각 충돌')
        self.fetch.side_effect=rejected
        self.service.observe(self.playback)
        self.assertIsNone(self.finish_public())
        self.assertTrue(self.service.public_done)
        self.assertEqual(self.service.status,'LRCLIB 후보 시각 충돌')

    def test_exhausted_network_retries_do_not_report_sync_absence(self):
        self.fetch.side_effect=TimeoutError()
        self.service.observe(self.playback)
        self.finish_public()
        self.now.return_value=115
        self.service.observe(self.playback)
        self.finish_public()
        self.assertIn('조회 실패',self.service.status)
        self.assertIn('미확인',self.service.status)

    def test_successful_retry_clears_the_error_even_if_search_has_no_match(self):
        for response in (result(),None):
            with self.subTest(matched=response is not None):
                self.service.observe(replace(self.playback,title=str(response is not None)))
                self.fetch.side_effect=[TimeoutError(),response]
                self.finish_public()
                self.now.return_value+=15
                self.service.observe(self.service.playback)
                self.finish_public()
                self.assertEqual(self.service.public_retry,0)
                self.assertEqual(self.service.retry_at,0)
                self.assertTrue(self.service.public_done)
                self.assertNotIn('실패',self.service.status)

    def test_plain_lyrics_have_explicit_unsynced_status(self):
        self.fetch.return_value = result(times=[None] * 3)
        self.service.observe(self.playback)
        self.assertEqual(self.finish_public()[1]['times'], [None] * 3)
        self.assertIn('싱크 없음', self.service.status)

    def test_spotify_job_receives_metadata_and_validated_lyrics(self):
        self.service.observe(self.playback)
        self.service.connect()
        command = self.service.job.commands[-1]
        self.assertEqual(command, {'action': 'lookup', 'key': self.service.request_key,
                                  'title': 'Test Song', 'artist': 'Singer', 'album': 'Album', 'duration': 90})
        accepted = self.service.receive(self.service.job, self.spotify_event())
        self.assertEqual(accepted[0], self.playback.key)
        self.assertEqual(accepted[1]['source'], 'Spotify')
        self.assertEqual(accepted[1]['times'], [10, 20, 30])
        self.assertIsNone(self.service.receive(object(), self.spotify_event()))

    def test_spotify_timestamps_beyond_current_recording_are_rejected(self):
        self.service.observe(self.playback)
        self.service.connect()
        self.assertIsNone(self.service.receive(self.service.job,
                          self.spotify_event(lines=[{'text': 'too late', 'time': 95}])))

    def test_close_ignores_late_results_and_closes_browser_job(self):
        self.service.observe(self.playback)
        self.service.connect()
        job = self.service.job
        event = self.spotify_event()
        self.service.close()
        self.assertTrue(job.closed)
        self.assertIsNone(self.service.receive(job, event))
        self.assertIsNone(self.finish_public())

    def test_same_metadata_duration_change_invalidates_prior_result(self):
        self.service.observe(self.playback)
        key = self.service.request_key
        self.service.observe(replace(self.playback, duration=150))
        self.assertNotEqual(self.service.request_key, key)
        self.assertIsNone(self.finish_public())

    def connected_spotify(self):
        self.service.observe(self.playback, public_enabled=False)
        self.service.connect(visible=True)
        self.service.receive(self.service.job, {'kind': 'status', 'state': 'ready',
                                               'text': 'Spotify 계정 연결됨', 'authenticated': True})
        return self.service.job

    def finish_spotify(self, job=None, code=1):
        self.service.receive(job or self.service.job, {'kind': 'finished', 'exit_code': code})

    def test_worker_exit_preserves_specific_error_exit_code_and_known_authentication(self):
        job = self.connected_spotify()
        self.service.receive(job, {'kind': 'status', 'state': 'error', 'text': 'Spotify HTTP 429 응답',
                                   'authenticated': True})
        self.finish_spotify(job, code=7)
        self.assertIsNone(self.service.job)
        self.assertEqual(self.service.spotify_last_error, 'Spotify HTTP 429 응답')
        self.assertEqual(self.service.spotify_exit_code, 7)
        self.assertIn('Spotify HTTP 429 응답', self.service.spotify_status)
        self.assertIn('7', self.service.spotify_status)
        self.assertTrue(self.service.spotify_authenticated)
        self.assertEqual(self.service.spotify_retry_at, 105)
        self.assertEqual(self.service.spotify_state, 'reconnecting')

    def test_worker_recovery_reuses_profile_hidden_after_five_seconds(self):
        old = self.connected_spotify()
        self.finish_spotify()
        self.now.return_value = 104.9
        self.service.observe(self.playback, public_enabled=False)
        self.assertIsNone(self.service.job)
        self.now.return_value = 105
        self.service.observe(self.playback, public_enabled=False)
        job = self.service.job
        self.assertIsNotNone(job)
        self.assertIsNot(job, old)
        self.assertFalse(job.visible)
        self.assertEqual(job.profile, old.profile)
        self.assertEqual(job.commands[-1]['title'], self.playback.title)
        self.assertEqual(self.service.spotify_state, 'reconnecting')
        self.fetch.assert_not_called()

    def test_worker_recovery_stops_after_five_and_fifteen_second_retries(self):
        self.connected_spotify()
        self.finish_spotify()
        self.now.return_value = 105
        self.service.observe(self.playback, public_enabled=False)
        self.assertIsNotNone(self.service.job)
        self.finish_spotify()
        self.assertEqual(self.service.spotify_retry_at, 120)
        self.now.return_value = 119.9
        self.service.observe(self.playback, public_enabled=False)
        self.assertIsNone(self.service.job)
        self.now.return_value = 120
        self.service.observe(self.playback, public_enabled=False)
        self.assertIsNotNone(self.service.job)
        self.finish_spotify()
        self.assertEqual(self.service.spotify_retry_count, 2)
        self.assertEqual(self.service.spotify_retry_at, 0)
        self.assertEqual(self.service.spotify_state, 'error')
        self.now.return_value = 1000
        self.service.observe(self.playback, public_enabled=False)
        self.assertIsNone(self.service.job)

    def test_valid_lyrics_reset_recovery_budget_but_authenticated_status_does_not(self):
        self.connected_spotify()
        self.finish_spotify()
        self.now.return_value = 105
        self.service.observe(self.playback, public_enabled=False)
        job = self.service.job
        self.service.receive(job, {'kind': 'status', 'state': 'ready', 'text': 'Connected', 'authenticated': True})
        self.assertEqual(self.service.spotify_retry_count, 1)
        self.assertIsNotNone(self.service.receive(job, self.spotify_event()))
        self.assertEqual(self.service.spotify_retry_count, 0)
        self.finish_spotify()
        self.assertEqual(self.service.spotify_retry_at, 110)

    def test_pending_recovery_uses_current_song_metadata_only(self):
        self.connected_spotify()
        self.finish_spotify()
        other = replace(self.playback, title='Current Song', artist='Current Singer', duration=180)
        self.now.return_value = 105
        self.service.observe(other, public_enabled=False)
        self.assertEqual(self.service.job.commands, [{'action': 'lookup', 'key': self.service.request_key,
            'title': 'Current Song', 'artist': 'Current Singer', 'album': 'Album', 'duration': 180}])

    def test_offline_cancels_pending_recovery_and_online_resumes_with_new_delay(self):
        self.connected_spotify()
        self.finish_spotify()
        self.service.observe(self.playback, enabled=False, public_enabled=False)
        self.assertEqual(self.service.spotify_retry_at, 0)
        self.now.return_value = 1000
        self.service.observe(self.playback, enabled=True, public_enabled=False)
        self.assertIsNone(self.service.job)
        self.assertEqual(self.service.spotify_retry_at, 1005)
        self.assertEqual(self.service.spotify_state, 'reconnecting')
        self.now.return_value = 1005
        self.service.observe(self.playback, enabled=True, public_enabled=False)
        self.assertIsNotNone(self.service.job)

    def test_disconnecting_for_public_selection_cancels_spotify_intent_and_retry(self):
        self.connected_spotify()
        self.finish_spotify()
        self.service.disconnect()
        self.assertFalse(self.service.spotify_requested)
        self.assertEqual(self.service.spotify_retry_at, 0)
        self.now.return_value = 1000
        self.service.observe(self.playback, public_enabled=True)
        self.assertIsNone(self.service.job)

    def test_login_required_exit_does_not_retry_or_replace_authentication_message(self):
        job = self.connected_spotify()
        self.service.receive(job, {'kind': 'status', 'state': 'login_required',
                                   'text': 'Spotify 인증이 만료되었습니다.', 'authenticated': False})
        self.finish_spotify(code=0)
        self.assertEqual(self.service.spotify_state, 'login_required')
        self.assertIn('Spotify 인증이 만료되었습니다.', self.service.spotify_status)
        self.assertEqual(self.service.spotify_retry_at, 0)
        self.assertFalse(self.service.spotify_authenticated)

    def test_window_close_still_recovers_but_explicit_service_close_cancels_retry(self):
        job = self.connected_spotify()
        self.service.receive(job, {'kind': 'status', 'state': 'closed', 'text': '연결 창을 닫았습니다.'})
        self.finish_spotify(code=0)
        self.assertIn('연결 창을 닫았습니다.', self.service.spotify_status)
        self.assertEqual(self.service.spotify_retry_at, 105)
        self.service.connect()
        self.finish_spotify()
        self.service.close()
        self.assertEqual(self.service.spotify_retry_at, 0)
        self.now.return_value = 1000
        self.service.observe(self.playback, public_enabled=False)
        self.assertIsNone(self.service.job)

    def test_old_worker_events_cannot_change_replacement_state(self):
        old = self.connected_spotify()
        self.finish_spotify()
        self.now.return_value = 105
        self.service.observe(self.playback, public_enabled=False)
        replacement = self.service.job
        status = self.service.spotify_status
        self.service.receive(old, {'kind': 'status', 'state': 'login_required',
                                  'text': 'stale error', 'authenticated': False})
        self.finish_spotify(old, code=9)
        self.assertIs(self.service.job, replacement)
        self.assertEqual(self.service.spotify_status, status)

    def test_recovery_spawn_failure_is_bounded_and_preserves_error_type(self):
        self.connected_spotify()
        self.finish_spotify()
        self.service.job_factory = Mock(side_effect=OSError('synthetic failure'))
        self.now.return_value = 105
        self.service.observe(self.playback, public_enabled=False)
        self.assertIn('OSError', self.service.spotify_last_error)
        self.assertEqual(self.service.spotify_retry_at, 120)
        self.now.return_value = 120
        self.service.observe(self.playback, public_enabled=False)
        self.now.return_value = 1000
        self.service.observe(self.playback, public_enabled=False)
        self.assertEqual(self.service.job_factory.call_count, 2)
        self.assertEqual(self.service.spotify_retry_at, 0)

    def test_manual_connect_resets_exhausted_recovery_budget(self):
        self.service.spotify_retry_count = 2
        self.connected_spotify()
        self.assertTrue(self.service.spotify_requested)
        self.assertEqual(self.service.spotify_retry_count, 0)
        self.finish_spotify()
        self.assertEqual(self.service.spotify_retry_at, 105)

    def test_startup_spawn_failure_before_media_observe_schedules_on_first_online_observe(self):
        factory = Mock(side_effect=[OSError('startup'), FakeSpotifyJob('.', self.service.profile, self.messages)])
        self.service.job_factory = factory
        self.service.connect(visible=False)
        self.assertFalse(self.service.enabled)
        self.assertEqual(self.service.spotify_state, 'error')
        self.assertEqual(self.service.spotify_retry_at, 0)
        self.service.observe(self.playback, public_enabled=False)
        self.assertEqual(self.service.spotify_retry_at, 105)
        self.assertEqual(self.service.spotify_state, 'reconnecting')
        self.now.return_value = 105
        self.service.observe(self.playback, public_enabled=False)
        self.assertIsNotNone(self.service.job)
        self.assertEqual(factory.call_args.kwargs, {'visible': False})
        self.assertEqual(self.service.job.commands[-1]['title'], self.playback.title)

    def test_online_resume_does_not_reset_exhausted_retry_budget(self):
        self.connected_spotify()
        self.service.spotify_retry_count = 2
        self.finish_spotify()
        self.service.observe(self.playback, enabled=False, public_enabled=False)
        self.now.return_value = 1000
        self.service.observe(self.playback, enabled=True, public_enabled=False)
        self.assertEqual(self.service.spotify_retry_count, 2)
        self.assertEqual(self.service.spotify_retry_at, 0)
        self.assertIsNone(self.service.job)

    def test_online_resume_does_not_reconnect_when_login_is_required(self):
        job = self.connected_spotify()
        self.service.receive(job, {'kind': 'status', 'state': 'login_required',
                                   'text': '로그인 필요', 'authenticated': False})
        self.finish_spotify(code=0)
        self.service.observe(self.playback, enabled=False, public_enabled=False)
        self.now.return_value = 1000
        self.service.observe(self.playback, enabled=True, public_enabled=False)
        self.assertEqual(self.service.spotify_state, 'login_required')
        self.assertEqual(self.service.spotify_retry_at, 0)
        self.assertIsNone(self.service.job)

    def test_complete_cached_sync_skips_lyrics_but_requests_missing_artist_metadata_once(self):
        self.service.connect()
        for _ in range(4):
            self.service.observe(self.playback, public_enabled=True, cached_sync=True)
        self.assertEqual(len(self.service.job.commands), 1)
        self.assertTrue(self.service.job.commands[0]['metadata_only'])
        self.assertFalse(self.service.spotify_pending)
        self.assertEqual(DeferredThread.pending, [])
        self.fetch.assert_not_called()
        self.assertTrue(self.service.public_enabled)
        self.assertIn('재조회 생략', self.service.status)

    def test_cached_sync_invalidates_inflight_results_from_both_providers(self):
        self.service.observe(self.playback)
        self.service.connect()
        old = self.spotify_event()
        before = self.service.request_key
        self.service.observe(self.playback, cached_sync=True)
        self.assertNotEqual(self.service.request_key, before)
        self.assertEqual(self.service.job.commands[-2]['key'], '')
        self.assertTrue(self.service.job.commands[-1]['metadata_only'])
        self.assertFalse(self.service.spotify_pending)
        self.assertIsNone(self.service.receive(self.service.job, old))
        self.assertIsNone(self.finish_public())
        self.assertFalse(self.service.public_done)

    def test_same_track_queries_normally_when_complete_cache_is_no_longer_available(self):
        self.service.connect()
        self.service.observe(self.playback, cached_sync=True)
        cached_key = self.service.request_key
        self.service.observe(self.playback, cached_sync=False)
        self.assertNotEqual(self.service.request_key, cached_key)
        self.assertTrue(self.service.spotify_pending)
        self.assertEqual(self.service.job.commands[-1]['title'], self.playback.title)
        self.assertEqual(DeferredThread.pending, [])
        self.service.receive(self.service.job,{'kind':'status','key':self.service.request_key,
                            'state':'not_found','text':'current lookup finished'})
        self.service.observe(self.playback)
        self.assertEqual(self.finish_public(), (self.playback.key, result()))

    def test_switching_from_cached_track_to_uncached_track_looks_up_new_track(self):
        self.service.connect()
        self.service.observe(self.playback, public_enabled=False, cached_sync=True)
        other = replace(self.playback, title='Uncached')
        self.service.observe(other, public_enabled=False, cached_sync=False)
        self.assertEqual(self.service.job.commands[-1]['title'], 'Uncached')
        self.assertTrue(self.service.spotify_pending)
        self.fetch.assert_not_called()
        self.assertFalse(self.service.public_enabled)

    def test_cached_a_b_uncached_a_rejects_first_a_response(self):
        self.service.observe(self.playback, public_enabled=False)
        self.service.connect()
        old = self.spotify_event()
        self.service.observe(replace(self.playback, title='Cached B'), public_enabled=False, cached_sync=True)
        self.service.observe(self.playback, public_enabled=False)
        self.assertIsNone(self.service.receive(self.service.job, old))
        self.assertTrue(self.service.spotify_pending)

    def test_cached_sync_prevents_hidden_recovery_lookup_until_uncached_playback(self):
        self.connected_spotify()
        self.finish_spotify()
        self.service.observe(self.playback, public_enabled=False, cached_sync=True)
        self.now.return_value = 1000
        self.service.observe(self.playback, public_enabled=False, cached_sync=True)
        self.assertIsNone(self.service.job)
        self.assertFalse(self.service.spotify_pending)
        self.service.observe(self.playback, public_enabled=False, cached_sync=False)
        self.assertEqual(self.service.spotify_retry_at, 1005)
        self.now.return_value = 1005
        self.service.observe(self.playback, public_enabled=False)
        self.assertIsNotNone(self.service.job)
        self.assertTrue(self.service.spotify_pending)

    def test_spotify_pending_tracks_lookup_completion_and_does_not_clear_on_auth_ready(self):
        self.service.observe(self.playback, public_enabled=False)
        self.service.connect()
        self.assertTrue(self.service.spotify_pending)
        self.service.receive(self.service.job, {'kind': 'status', 'key': self.service.request_key,
                            'state': 'ready', 'text': '계정 연결됨', 'authenticated': True})
        self.assertTrue(self.service.spotify_pending)
        self.assertIsNotNone(self.service.receive(self.service.job, self.spotify_event()))
        self.assertFalse(self.service.spotify_pending)

    def test_current_terminal_status_clears_pending_but_old_track_status_cannot(self):
        self.service.observe(self.playback, public_enabled=False)
        self.service.connect()
        old_key = self.service.request_key
        self.service.observe(replace(self.playback, title='New Song'), public_enabled=False)
        status = self.service.spotify_status
        self.service.receive(self.service.job, {'kind': 'status', 'key': old_key,
                            'state': 'not_found', 'text': 'Old track not found'})
        self.assertTrue(self.service.spotify_pending)
        self.assertEqual(self.service.spotify_status, status)
        self.service.receive(self.service.job, {'kind': 'status', 'key': self.service.request_key,
                            'state': 'not_found', 'text': 'Current track not found'})
        self.assertFalse(self.service.spotify_pending)

    def test_current_worker_old_track_logout_and_login_update_connection_only(self):
        job=self.connected_spotify()
        old_key=self.service.request_key
        self.service.observe(replace(self.playback,title='Other'),public_enabled=False)
        for kind in ('status','metadata_status'):
            for authenticated,state in ((False,'login_required'),(True,'ready')):
                with self.subTest(kind=kind,authenticated=authenticated):
                    self.service.metadata_pending=True
                    self.service.spotify_pending=True
                    self.service.receive(job,{'kind':kind,'key':old_key,'state':state,
                                              'text':'old track detail','authenticated':authenticated})
                    self.assertEqual(self.service.spotify_authenticated,authenticated)
                    self.assertEqual(self.service.spotify_state,state)
                    self.assertNotEqual(self.service.spotify_status,'old track detail')
                    self.assertTrue(self.service.spotify_pending)
                    self.assertTrue(self.service.metadata_pending)

    def test_current_worker_authentication_remains_current_when_cached_lookup_is_skipped(self):
        job=self.connected_spotify()
        old_key=self.service.request_key
        with patch.object(self.service,'display_artists',return_value=('Verified Singer',)):
            self.service.observe(replace(self.playback,title='Cached Other'),public_enabled=False,cached_sync=True)
        for authenticated,state in ((False,'login_required'),(True,'ready')):
            self.service.receive(job,{'kind':'metadata_status','key':old_key,'state':state,
                                      'authenticated':authenticated})
            self.assertEqual(self.service.spotify_authenticated,authenticated)
            self.assertEqual(self.service.spotify_state,state)
        self.assertFalse(self.service.spotify_pending)
        self.assertFalse(self.service.metadata_pending)

    def test_old_worker_positive_and_negative_authentication_cannot_replace_new_connection(self):
        old=self.connected_spotify()
        self.service.disconnect()
        self.service.connect()
        for kind in ('status','metadata_status'):
            for authenticated,state in ((True,'ready'),(False,'login_required')):
                self.service.spotify_authenticated=not authenticated
                before=self.service.spotify_state,self.service.spotify_status
                self.service.receive(old,{'kind':kind,'key':self.service.request_key,'state':state,
                                          'authenticated':authenticated})
                self.assertEqual(self.service.spotify_authenticated,not authenticated)
                self.assertEqual((self.service.spotify_state,self.service.spotify_status),before)

    def test_old_track_terminal_status_with_auth_does_not_finish_new_lookup(self):
        job=self.connected_spotify()
        old_key=self.service.request_key
        self.service.observe(replace(self.playback,title='Other'),public_enabled=False)
        for kind in ('status','metadata_status'):
            self.service.spotify_state='searching';self.service.spotify_status='current lookup'
            self.service.metadata_pending=True
            self.service.receive(job,{'kind':kind,'key':old_key,'state':'not_found',
                                      'text':'old track missing','authenticated':True})
            self.assertEqual(self.service.spotify_state,'searching')
            self.assertEqual(self.service.spotify_status,'current lookup')
            self.assertTrue(self.service.spotify_pending)
            self.assertTrue(self.service.metadata_pending)

    def test_all_current_terminal_states_release_pending_lookup(self):
        self.service.observe(self.playback, public_enabled=False)
        self.service.connect()
        for state in ('not_found', 'unsynced', 'error', 'login_required', 'closed'):
            self.service._lookup()
            self.assertTrue(self.service.spotify_pending)
            self.service.receive(self.service.job, {'kind': 'status', 'key': self.service.request_key,
                                'state': state, 'text': state})
            self.assertFalse(self.service.spotify_pending, state)

    def test_disconnect_and_disable_release_pending_lookup(self):
        self.connected_spotify()
        self.assertTrue(self.service.spotify_pending)
        self.service.observe(self.playback, enabled=False, public_enabled=False)
        self.assertFalse(self.service.spotify_pending)
        self.service.observe(self.playback, public_enabled=False)
        self.assertTrue(self.service.spotify_pending)
        self.service.disconnect()
        self.assertFalse(self.service.spotify_pending)


class SpotifyJobReadTests(unittest.TestCase):
    def job(self, exit_code=7):
        job = SpotifyJob.__new__(SpotifyJob)
        job.messages = queue.Queue()
        job.closed = False
        job.process = SimpleNamespace(stdout=io.StringIO('{"kind":"status","state":"error","text":"specific failure"}\n'),
                                      stdin=io.StringIO(), wait=Mock(return_value=exit_code),
                                      poll=Mock(return_value=None),terminate=Mock())
        return job

    def test_reader_forwards_specific_error_before_finished_event_with_exit_code(self):
        job = self.job()
        job._read()
        first = job.messages.get_nowait()[1][1]
        last = job.messages.get_nowait()[1][1]
        self.assertEqual(first['text'], 'specific failure')
        self.assertEqual(last, {'kind': 'finished', 'exit_code': 7})
        job.process.wait.assert_called_once_with(timeout=1)

    def test_deliberate_close_does_not_emit_unexpected_finished_event(self):
        job = self.job()
        job.closed = True
        job._read()
        self.assertTrue(job.messages.empty())

    def test_reader_ignores_non_object_frames_before_valid_status(self):
        job=self.job()
        job.process.stdout=io.StringIO('null\n42\n[]\n{"kind":"status","state":"ready"}\n')
        job._read()
        events=[job.messages.get_nowait()[1][1] for _ in range(job.messages.qsize())]
        self.assertEqual(events,[{'kind':'status','state':'ready'},{'kind':'finished','exit_code':7}])

    def test_reader_wait_is_bounded_when_output_closes_before_process_exit(self):
        job = self.job()
        job.process.wait.side_effect = [subprocess.TimeoutExpired('spotify worker', 1),1]
        job._read()
        job.messages.get_nowait()
        self.assertEqual(job.messages.get_nowait()[1][1], {'kind': 'finished', 'exit_code': 1})
        job.process.terminate.assert_called_once_with()
        self.assertEqual([call.kwargs for call in job.process.wait.call_args_list],[{'timeout':1},{'timeout':3}])

    def test_broken_input_close_does_not_prevent_process_reaping_or_finished(self):
        job=self.job()
        job.process.stdin=SimpleNamespace(close=Mock(side_effect=OSError('broken pipe')))
        job._read()
        job.process.wait.assert_called_once_with(timeout=1)
        job.messages.get_nowait()
        self.assertEqual(job.messages.get_nowait()[1][1],{'kind':'finished','exit_code':7})


class BackgroundMappingTests(unittest.TestCase):
    def test_spotify_and_auxiliary_arrival_order_preserves_the_same_primary_clock(self):
        spotify=result('Spotify',times=[10,None,30])
        auxiliary=result('LRCLIB',times=[9,20,31])
        before_spotify=list(spotify['times']);before_auxiliary=list(auxiliary['times'])
        first=prefer_spotify_timing(spotify,auxiliary)
        second=prefer_spotify_timing(auxiliary,spotify)
        self.assertEqual(first,second)
        self.assertEqual(first['source'],'Spotify')
        self.assertEqual(first['times'],[10,None,30])
        self.assertNotIn('fallback',first)
        self.assertEqual(mapped_times(first,spotify['lines']),[10,None,30])
        self.assertEqual(spotify['times'],before_spotify)
        self.assertEqual(auxiliary['times'],before_auxiliary)

    def test_legacy_auxiliary_can_fill_real_prefix_and_suffix_onsets_without_overwriting_spotify(self):
        spotify=result('Spotify',times=[None,40,None])
        auxiliary=result('LRCLIB',times=[31,41,51])
        combined={**spotify,'fallback':auxiliary}
        self.assertEqual(mapped_times(combined,spotify['lines']),[31,40,51])
        self.assertEqual(mapped_times({key:value for key,value in combined.items() if key!='fallback'},
                                      spotify['lines']),[None,40,None])

    def test_legacy_auxiliary_onsets_cannot_cross_or_equal_neighboring_spotify_anchors(self):
        spotify=result('Spotify',times=[10,None,30])
        for middle in (8,10,30,35):
            auxiliary=result('LRCLIB',times=[1,middle,40])
            combined={**spotify,'fallback':auxiliary}
            with self.subTest(middle=middle):
                self.assertEqual(mapped_times(combined,spotify['lines']),[10,None,30])

    def test_legacy_auxiliary_mapping_requires_exact_phrase_onsets_and_repeated_sequence_order(self):
        spotify=result('Spotify',times=[10,None,30,40,None,60],
                       texts=('ab','cdef','sunrise','ab','cdef','ending'))
        auxiliary=result('LRCLIB',times=[11,20,31,41,50,61],
                         texts=('abcd','ef','sunrise','ab','cdef','ending'))
        combined={**spotify,'fallback':auxiliary}
        self.assertEqual(mapped_times(combined,spotify['lines']),[10,None,30,40,50,60])

    def test_legacy_auxiliary_cannot_start_a_row_after_a_known_spotify_fragment_inside_it(self):
        spotify=result('Spotify',times=[10,20,30],texts=('abcd','ef','sunrise'))
        target=tuple(Line(text,'','') for text in ('ab','cdef','sunrise'))
        for middle,expected in ((15,15),(20,None),(25,None)):
            auxiliary=result('LRCLIB',times=[9,middle,31],texts=('ab','cdef','sunrise'))
            combined={**spotify,'fallback':auxiliary}
            with self.subTest(middle=middle):
                self.assertEqual(mapped_times(combined,target),[10,expected,30])

    def test_legacy_auxiliary_cannot_precede_a_known_fragment_in_the_previous_merged_row(self):
        spotify=result('Spotify',times=[10,20,None,40],texts=('ab','cd','ef','sunrise'))
        target=tuple(Line(text,'','') for text in ('abcd','ef','sunrise'))
        for middle,expected in ((15,None),(20,None),(25,25)):
            auxiliary=result('LRCLIB',times=[9,middle,41],texts=('abcd','ef','sunrise'))
            combined={**spotify,'fallback':auxiliary}
            with self.subTest(middle=middle):
                self.assertEqual(mapped_times(combined,target),[10,expected,40])

    def test_legacy_auxiliary_prefix_and_suffix_are_rejected_when_they_cross_spotify(self):
        spotify=result('Spotify',times=[None,40,None])
        for timestamps,expected in (([41,42,51],[None,40,51]),([31,32,39],[31,40,None])):
            combined={**spotify,'fallback':result('LRCLIB',times=timestamps)}
            self.assertEqual(mapped_times(combined,spotify['lines']),expected)

    def test_unknown_display_rows_do_not_receive_legacy_auxiliary_timing(self):
        spotify=result('Spotify',times=[10,None,30])
        auxiliary=result('LRCLIB',times=[9,20,31])
        combined={**spotify,'fallback':auxiliary}
        target=(spotify['lines'][0],Line('Unrelated','',''),spotify['lines'][-1])
        self.assertEqual(mapped_times(combined,target),[10,None,30])
        self.assertEqual(mapped_times(combined,(Line('Unrelated','',''),)),[None])

    def test_new_spotify_clock_removes_legacy_fallback_without_mutating_input(self):
        spotify=result('Spotify',times=[10,None,30])
        auxiliary=result('LRCLIB',times=[9,20,31])
        combined={**spotify,'fallback':auxiliary}
        newer=result('Spotify',times=[10,None,18])
        updated=prefer_spotify_timing(combined,newer)
        self.assertEqual(updated['times'],[10,None,18])
        self.assertNotIn('fallback',updated)
        self.assertEqual(combined['fallback'],auxiliary)
        self.assertEqual(mapped_times(updated,newer['lines']),[10,None,18])

    def test_sparse_spotify_refresh_cannot_erase_a_more_complete_identical_clock(self):
        complete=result('Spotify',times=[10,20,30])
        auxiliary=result('LRCLIB',times=[9,21,31])
        current=prefer_spotify_timing(complete,auxiliary)
        sparse=result('Spotify',times=[11,None,31])
        self.assertEqual(prefer_spotify_timing(current,sparse),current)
        self.assertEqual(prefer_spotify_timing(sparse,current),current)
        self.assertEqual(current['times'],[10,20,30])
        self.assertNotIn('fallback',current)

    def test_spotify_refresh_with_changed_original_rows_keeps_new_row_grouping(self):
        current=result('Spotify',times=[10,20,30],texts=('ab','cd','sunrise'))
        incoming=result('Spotify',times=[10,None],texts=('abcd','sunrise'))
        updated=prefer_spotify_timing(current,incoming)
        self.assertEqual(updated['lines'],incoming['lines'])
        self.assertEqual(updated['times'],incoming['times'])

    def test_more_complete_external_clock_never_fills_partial_spotify(self):
        spotify=result('Spotify',times=[10,None,30])
        weak=result('LRCLIB',times=[9,None,31])
        strong=result('LyricsPlus',times=[9,20,31])
        left=prefer_spotify_timing(prefer_spotify_timing(spotify,weak),strong)
        right=prefer_spotify_timing(prefer_spotify_timing(spotify,strong),weak)
        self.assertEqual(left,right)
        self.assertEqual(left,spotify)
        refreshed={**strong,'times':[9,22,31]}
        self.assertEqual(prefer_spotify_timing(left,refreshed),spotify)

    def test_equal_coverage_from_different_auxiliary_sources_is_order_independent(self):
        spotify=result('Spotify',times=[10,None,30])
        first=result('LRCLIB',times=[9,20,31])
        second=result('LyricsPlus',times=[9,22,31])
        left=prefer_spotify_timing(prefer_spotify_timing(spotify,first),second)
        right=prefer_spotify_timing(prefer_spotify_timing(spotify,second),first)
        self.assertEqual(left,right)
        self.assertEqual(left,spotify)

    def test_selecting_existing_spotify_also_removes_legacy_fallback(self):
        spotify=result('Spotify',times=[10,None,30])
        legacy={**spotify,'fallback':result('LRCLIB')}
        for incoming in (None,result('ALSong'),result('Spotify',times=[None]*3)):
            with self.subTest(incoming=incoming):
                selected=prefer_spotify_timing(legacy,incoming)
                self.assertEqual(selected,spotify)
                self.assertIsNot(selected['times'],legacy['times'])
        self.assertIn('fallback',legacy)

    def test_unsynced_spotify_does_not_displace_a_real_auxiliary_clock(self):
        spotify=result('Spotify',times=[None]*3)
        auxiliary=result('LRCLIB')
        self.assertIsNone(prefer_spotify_timing(spotify,auxiliary))
        self.assertIsNone(prefer_spotify_timing(auxiliary,spotify))
        timed=result('Spotify')
        self.assertEqual(prefer_spotify_timing(timed,spotify),timed)

    def test_unrelated_or_unsynced_auxiliary_is_not_retained(self):
        spotify=result('Spotify',times=[10,None,30])
        for auxiliary in (result('LRCLIB',times=[None]*3),
                          result('LRCLIB',texts=('different','whole','song'))):
            self.assertEqual(prefer_spotify_timing(spotify,auxiliary),spotify)

    def test_one_common_phrase_cannot_verify_a_different_auxiliary_lyric_document(self):
        spotify=result('Spotify',times=[10,None,30],texts=('opening','middle','ending'))
        auxiliary=result('LRCLIB',times=[9,20,31],texts=('unrelatedfirst','middle','unrelatedlast'))
        self.assertEqual(prefer_spotify_timing(spotify,auxiliary),spotify)
        # A previously cached or manually supplied bundle must pass the same
        # whole-document check before its auxiliary times can fill anything.
        self.assertEqual(mapped_times({**spotify,'fallback':auxiliary},spotify['lines']),[10,None,30])

    def test_meaningful_missing_verse_cannot_be_accepted_as_auxiliary_clock(self):
        spotify=result('Spotify',times=[10,None,30],texts=('opening','middle','ending'))
        auxiliary=result('LRCLIB',times=[9,15,20,31],
                         texts=('opening','differentverse','middle','ending'))
        self.assertEqual(prefer_spotify_timing(spotify,auxiliary),spotify)
        self.assertEqual(mapped_times({**spotify,'fallback':auxiliary},spotify['lines']),[10,None,30])

    def test_whole_document_typo_budget_still_allows_verified_legacy_auxiliary_onsets(self):
        texts=tuple(f'朝の光を見ながら遠い空の向こうへ歩いて行こう{i}' for i in range(20))
        times=[10+i*3 for i in range(20)];times[3]=None
        spotify=result('Spotify',times=times,texts=texts)
        remote=list(texts);remote[3]=remote[3].replace('光','灯')
        auxiliary=result('LyricsPlus',times=[10+i*3 for i in range(20)],texts=remote)
        combined={**spotify,'fallback':auxiliary}
        self.assertEqual(mapped_times(combined,spotify['lines']),auxiliary['times'])
        self.assertEqual(combined['times'],times)

    def test_shifted_boundaries_never_use_an_interior_phrase_time_as_a_row_start(self):
        lines=tuple(Line(text,'','') for text in ('ab','cdef','sunrise'))
        for source in ('Spotify','LRCLIB','ALSong','LyricsPlus'):
            with self.subTest(source=source):
                provider=result(source=source,times=[10,20,30],texts=('abcd','ef','sunrise'))
                self.assertEqual(mapped_times(provider,lines),[10,None,30])

    def test_missing_start_is_not_replaced_by_a_later_fragment_of_the_same_phrase(self):
        lines=tuple(Line(text,'','') for text in ('ab','cdef','sunrise'))
        for source in ('Spotify','LRCLIB','ALSong','LyricsPlus'):
            with self.subTest(source=source):
                provider=result(source=source,times=[None,20,30],texts=('abcd','ef','sunrise'))
                self.assertEqual(mapped_times(provider,lines),[None,None,30])

    def test_verified_provider_typos_map_to_existing_originals_without_dropping_rows(self):
        texts=tuple(f'朝の光を見ながら遠い空の向こうへ歩いて行こう{i}' for i in range(20))
        remote=list(texts);remote[3]=remote[3].replace('光','灯');remote[17]=remote[17].replace('空','雲')
        lines=tuple(Line(text,'','') for text in texts)
        for source in ('ALSong','LyricsPlus'):
            provider={**result(times=list(range(20)),texts=remote),'source':source}
            self.assertEqual(mapped_times(provider,lines),list(range(20)))
        provider['source']='Spotify'
        self.assertIsNone(mapped_times(provider,lines)[3])

    def test_typo_mapping_and_different_line_breaks_never_invent_missing_onsets(self):
        texts=tuple(f'朝の光を見ながら遠い空の向こうへ歩いて行こう{i}' for i in range(20))
        remote=list(texts);remote[3]=remote[3].replace('光','灯')
        remote[8:10]=[remote[8]+' '+remote[9]]
        times=[i for i in range(20) if i!=9]
        provider={**result(times=times,texts=remote),'source':'LyricsPlus'}
        expected=list(range(20));expected[9]=None
        self.assertEqual(mapped_times(provider,tuple(Line(text,'','') for text in texts)),expected)

    def test_merged_provider_phrase_only_supplies_first_split_translation_time(self):
        lines = tuple(Line(text, '', '') for text in ('青い空', 'を見る', '花を描く', '最後の言葉'))
        self.assertEqual(mapped_times(result(), lines), [10, None, 20, 30])

    def test_split_provider_phrase_supplies_first_time_of_merged_translation(self):
        provider = result(times=[10, 12, 20, 30], texts=('青い空', 'を見る', '花を描く', '最後の言葉'))
        self.assertEqual(mapped_times(provider, result()['lines']), [10, 20, 30])

    def test_unknown_and_unsynced_rows_never_receive_invented_times(self):
        provider = result(times=[None, None, None])
        self.assertEqual(mapped_times(provider, result()['lines']), [None] * 3)
        self.assertEqual(mapped_times(result(), (Line('Unrelated', '', ''),)), [None])


if __name__ == '__main__':
    unittest.main()
