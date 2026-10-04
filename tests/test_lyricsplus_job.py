"""Synthetic LyricsPlus scheduling; no network or actual worker threads."""
from dataclasses import replace
import queue
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from lyricsplus_job import LyricsPlusJob, ProviderSyncJob
from media import Playback


class DeferredThread:
    targets = []

    def __init__(self, target, **kwargs):
        self.target = target

    def start(self):
        self.targets.append(self.target)


class LyricsPlusJobTests(unittest.TestCase):
    def setUp(self):
        self.messages = queue.Queue()
        self.result = {'source': 'LyricsPlus', 'times': [1, 2, 3]}
        self.fetch = Mock(return_value=self.result)
        self.job = LyricsPlusJob(self.messages, self.fetch)
        self.playback = Playback('Song', 'Singer', 'Album', 0, 90, True, 100)
        DeferredThread.targets = []
        self.threads = patch('lyricsplus_job.threading.Thread', DeferredThread)
        self.clock = patch('lyricsplus_job.time.monotonic', return_value=100)
        self.threads.start()
        self.now = self.clock.start()

    def tearDown(self):
        self.job.close()
        self.clock.stop()
        self.threads.stop()

    def observe(self, playback=None, **kwargs):
        kwargs.setdefault('reference', ('first', 'last'))
        self.job.observe(playback or self.playback, enabled=True, **kwargs)

    def event(self):
        kind, event = self.messages.get_nowait()
        self.assertEqual(kind, 'lyricsplus')
        return event

    def finish(self):
        DeferredThread.targets.pop(0)()
        return self.job.receive(self.event())

    def test_disabled_by_default_and_complete_sync_never_start_request(self):
        self.job.observe(self.playback)
        self.observe(cached_sync=True)
        self.assertEqual(DeferredThread.targets, [])
        self.assertFalse(self.job.pending)
        self.assertTrue(self.job.done)
        self.fetch.assert_not_called()

    def test_missing_artist_or_timeline_never_start_request(self):
        for playback in (replace(self.playback, artist=''), replace(self.playback, duration=0),
                         replace(self.playback, duration=float('inf')), replace(self.playback, error='unavailable')):
            self.observe(playback)
        self.assertEqual(DeferredThread.targets, [])

    def test_pending_primary_waits_then_missing_primary_allows_fallback(self):
        self.observe(waiting_for_primary=True)
        self.assertEqual(DeferredThread.targets, [])
        self.assertFalse(self.job.done)
        self.assertIn('Spotify 싱크 확인 대기',self.job.status)
        self.observe(waiting_for_primary=False)
        self.assertEqual(self.finish(), (self.playback.key,self.result))

    def test_primary_arrival_invalidates_an_existing_secondary_request(self):
        self.observe()
        self.observe(cached_sync=True)
        self.assertIsNone(self.finish())
        self.assertTrue(self.job.done)
        self.assertFalse(self.job.pending)
        self.observe(cached_sync=True)
        self.assertEqual(DeferredThread.targets, [])

    def test_no_reference_waits_without_request_until_originals_arrive(self):
        for _ in range(3):
            self.job.observe(self.playback, enabled=True)
        self.assertEqual(self.job.status, 'LyricsPlus 원문 대기')
        self.assertFalse(self.job.pending)
        self.assertFalse(self.job.done)
        self.assertEqual(DeferredThread.targets, [])
        self.fetch.assert_not_called()
        self.observe()
        self.assertEqual(self.finish(), (self.playback.key, self.result))

    def test_same_song_is_single_flight_and_forwards_reference_snapshot(self):
        reference = ['first', 'last']
        self.observe(reference=reference)
        reference.append('later mutation')
        self.observe(reference=('first', 'last'))
        self.assertEqual(len(DeferredThread.targets), 1)
        self.assertTrue(self.job.pending)
        self.assertEqual(self.finish(), (self.playback.key, self.result))
        self.fetch.assert_called_once_with(self.playback, reference=('first', 'last'))
        self.assertTrue(self.job.done)
        self.assertFalse(self.job.pending)
        self.observe(reference=('first', 'last'))
        self.assertEqual(DeferredThread.targets, [])
        self.assertTrue(self.messages.empty())

    def test_track_change_rejects_old_response_and_waits_for_single_worker(self):
        self.observe()
        other = replace(self.playback, title='Other')
        self.observe(other)
        self.assertEqual(len(DeferredThread.targets), 1)
        self.assertFalse(self.job.pending)
        self.assertIsNone(self.finish())
        self.observe(other)
        self.assertEqual(self.finish(), (other.key, self.result))

    def test_inflight_a_b_a_response_is_stale_even_when_metadata_matches_again(self):
        self.observe()
        self.observe(replace(self.playback, title='Other'))
        self.observe()
        self.assertIsNone(self.finish())
        self.observe()
        self.assertEqual(self.finish(), (self.playback.key, self.result))
        self.assertEqual(self.fetch.call_count, 2)

    def test_completed_a_b_a_reuses_memory_without_another_request(self):
        self.observe()
        received = self.finish()
        received[1]['times'][0] = 99
        self.now.return_value = 1000
        self.observe(replace(self.playback, title='Other'))
        self.finish()
        self.observe()
        self.assertEqual(DeferredThread.targets, [])
        self.assertEqual(self.job.receive(self.event())[1]['times'], [1, 2, 3])
        self.assertEqual(self.fetch.call_count, 2)

    def test_artist_album_version_and_duration_each_have_separate_cache(self):
        self.observe()
        self.finish()
        for changes in ({'artist': 'Other'}, {'album': 'Other'}, {'title': 'Song (Live)'}, {'duration': 90.1}):
            with self.subTest(changes=changes):
                self.observe(replace(self.playback, **changes))
                self.assertEqual(len(DeferredThread.targets), 1)
                self.finish()
        self.assertEqual(self.fetch.call_count, 5)

    def test_duration_change_invalidates_old_result(self):
        self.observe()
        self.observe(replace(self.playback, duration=91))
        self.assertIsNone(self.finish())

    def test_disable_and_reenable_invalidates_old_result(self):
        self.observe()
        self.job.observe(self.playback, enabled=False)
        self.observe()
        self.assertIsNone(self.finish())
        self.observe()
        self.assertEqual(self.finish(), (self.playback.key, self.result))

    def test_cache_activation_invalidates_old_result_and_no_new_lookup(self):
        self.observe()
        self.observe(cached_sync=True)
        self.assertIsNone(self.finish())
        self.observe(cached_sync=True)
        self.assertEqual(DeferredThread.targets, [])

    def test_no_match_is_cached_but_new_reference_retries(self):
        self.fetch.return_value = None
        self.observe()
        self.assertIsNone(self.finish())
        self.assertTrue(self.job.done)
        self.assertEqual(self.job.status, '검색 결과에서 검증된 LyricsPlus 싱크를 찾지 못함')
        self.now.return_value = 1000
        self.observe()
        self.assertEqual(DeferredThread.targets, [])
        self.observe(replace(self.playback, title='Other'))
        self.finish()
        self.observe()
        self.assertEqual(DeferredThread.targets, [])
        self.assertTrue(self.job.done)
        self.observe(reference=('first', 'middle', 'last'))
        self.assertEqual(len(DeferredThread.targets), 1)
        self.finish()
        self.assertEqual(self.fetch.call_count, 3)

    def test_reference_change_rejects_an_inflight_result(self):
        self.observe()
        self.observe(reference=('first', 'middle', 'last'))
        self.assertIsNone(self.finish())
        self.observe(reference=('first', 'middle', 'last'))
        self.assertEqual(self.finish(), (self.playback.key, self.result))

    def test_disappearing_reference_invalidates_inflight_result_and_waits(self):
        self.observe()
        self.observe(reference=())
        self.assertIsNone(self.finish())
        self.assertEqual(self.job.status, 'LyricsPlus 원문 대기')
        self.assertFalse(self.job.pending)
        self.assertFalse(self.job.done)

    def test_errors_report_only_http_code_timeout_or_type_without_private_details(self):
        private = 'https://private.invalid/?song=private-title&token=private-token'
        failures = ((HTTPError(private, 530, private, {}, None), 'HTTP 530'),
                    (TimeoutError(private), '시간 초과'),
                    (URLError(TimeoutError(private)), '시간 초과'),
                    (URLError(private), 'URLError'),
                    (ValueError(private), 'ValueError'))
        for index, (failure, label) in enumerate(failures):
            with self.subTest(label=label):
                self.fetch.side_effect = failure
                self.observe(replace(self.playback, title=str(index)))
                DeferredThread.targets.pop(0)()
                event = self.event()
                self.assertNotIn(private, repr(event))
                self.assertIsNone(self.job.receive(event))
                self.assertIn(label, self.job.status)
                self.assertNotIn('private', self.job.status)

    def test_transient_failure_retries_then_restarts_bounded_cycle_after_cooldown(self):
        self.fetch.side_effect = [RuntimeError('synthetic private error')] * 3 + [self.result]
        self.observe()
        self.assertIsNone(self.finish())
        self.assertFalse(self.job.done)
        self.now.return_value = 114.9
        self.observe()
        self.assertEqual(DeferredThread.targets, [])
        self.now.return_value = 115
        self.observe()
        self.assertIsNone(self.finish())
        self.assertTrue(self.job.done)
        self.assertIn('5분 후 다시 확인', self.job.status)
        self.now.return_value = 414.9
        self.observe()
        self.assertEqual(DeferredThread.targets, [])
        self.assertEqual(self.fetch.call_count, 2)
        self.assertNotIn('private', self.job.status)
        self.now.return_value = 415
        self.observe()
        self.assertEqual(len(DeferredThread.targets), 1)
        self.assertIsNone(self.finish())
        self.assertFalse(self.job.done)
        self.assertIn('15초 후 한 번 재시도', self.job.status)
        self.now.return_value = 429.9
        self.observe()
        self.assertEqual(DeferredThread.targets, [])
        self.now.return_value = 430
        self.observe()
        self.assertEqual(self.finish(), (self.playback.key, self.result))
        self.assertTrue(self.job.done)
        self.assertEqual(self.fetch.call_count, 4)

    def test_failed_song_revisit_keeps_cooldown_then_retries_after_expiry(self):
        self.fetch.side_effect = [TimeoutError(), TimeoutError(), self.result, self.result]
        self.observe();self.finish()
        self.now.return_value = 115
        self.observe();self.finish()
        other = replace(self.playback, title='Other')
        self.now.return_value = 120
        self.observe(other);self.finish()
        self.now.return_value = 414.9
        self.observe()
        self.assertTrue(self.job.done)
        self.assertIn('5분 후 다시 확인', self.job.status)
        self.assertEqual(DeferredThread.targets, [])
        self.now.return_value = 415
        self.observe()
        self.assertEqual(self.finish(), (self.playback.key, self.result))
        self.assertEqual(self.fetch.call_count, 4)
        self.observe(other)
        self.assertEqual(self.job.receive(self.event()), (other.key, self.result))
        self.observe()
        self.assertEqual(self.job.receive(self.event()), (self.playback.key, self.result))
        self.assertEqual(self.fetch.call_count, 4)

    def test_expired_failure_waits_for_obsolete_worker_before_retrying(self):
        self.fetch.side_effect = [TimeoutError(), TimeoutError(), self.result, self.result]
        self.observe();self.finish()
        self.now.return_value = 115
        self.observe();self.finish()
        self.now.return_value = 414
        self.observe(replace(self.playback, title='Other'))
        self.now.return_value = 415
        self.observe()
        self.assertEqual(len(DeferredThread.targets), 1)
        self.assertFalse(self.job.pending)
        self.assertEqual(self.job.status, '이전 LyricsPlus 조회 완료 대기')
        self.assertIsNone(self.finish())
        self.observe()
        self.assertEqual(len(DeferredThread.targets), 1)
        self.assertEqual(self.finish(), (self.playback.key, self.result))
        self.assertEqual(self.fetch.call_count, 4)

    def test_reported_no_match_is_not_an_error_and_keeps_reason_on_cache_reuse(self):
        self.job.reports_status = True
        reason = '알송 적용 조건 불일치 · 한글 번역 없음'
        def fetch(playback, reference, report):
            report(reason)
            return None
        self.fetch.side_effect = fetch
        self.observe()
        self.assertIsNone(self.finish())
        self.assertEqual(self.job.status, reason)
        self.assertTrue(self.job.done)
        self.now.return_value = 1000
        self.observe()
        self.assertEqual(DeferredThread.targets, [])
        self.observe(replace(self.playback, title='Other'))
        self.finish()
        self.observe()
        self.assertEqual(self.job.status, reason)
        self.assertEqual(self.fetch.call_count, 2)

    def test_reported_success_keeps_reason_when_result_is_reused(self):
        self.job.reports_status = True
        reason = '알송 싱크 확인 · 한글 번역 없음'
        def fetch(playback, reference, report):
            report(reason)
            return self.result
        self.fetch.side_effect = fetch
        self.observe();self.finish()
        self.assertEqual(self.job.status, reason)
        self.observe(replace(self.playback, title='Other'));self.finish()
        self.observe()
        self.assertEqual(self.job.receive(self.event()), (self.playback.key, self.result))
        self.assertEqual(self.job.status, reason)
        self.assertEqual(self.fetch.call_count, 2)

    def test_failed_or_stale_request_cannot_replace_status_with_reported_success(self):
        self.job.reports_status = True
        def fetch(playback, reference, report):
            report('알송 자료 확인')
            raise TimeoutError('private endpoint detail')
        self.fetch.side_effect = fetch
        self.observe();self.finish()
        self.assertIn('시간 초과', self.job.status)
        self.assertNotIn('자료 확인', self.job.status)
        self.now.return_value = 115
        self.observe()
        self.observe(replace(self.playback, title='Other'))
        status = self.job.status
        self.assertIsNone(self.finish())
        self.assertEqual(self.job.status, status)

    def test_transient_failure_can_succeed_on_single_retry(self):
        self.fetch.side_effect = [RuntimeError('test'), self.result]
        self.observe()
        self.finish()
        self.now.return_value = 115
        self.observe()
        self.assertEqual(self.finish(), (self.playback.key, self.result))
        self.assertTrue(self.job.done)

    def test_duplicate_old_event_does_not_clear_retry_pending(self):
        self.fetch.side_effect = [RuntimeError('test'), self.result]
        self.observe()
        DeferredThread.targets.pop(0)()
        event = self.event()
        self.job.receive(event)
        self.now.return_value = 115
        self.observe()
        self.assertIsNone(self.job.receive(event))
        self.assertTrue(self.job.pending)
        self.assertEqual(self.finish(), (self.playback.key, self.result))

    def test_queued_cache_delivery_is_rejected_after_disable(self):
        self.observe()
        self.finish()
        self.job.observe(self.playback, enabled=False)
        self.observe()
        event = self.event()
        self.job.observe(self.playback, enabled=False)
        self.assertIsNone(self.job.receive(event))

    def test_bounded_cache_evicts_oldest_positive_and_negative_entries(self):
        self.job.MAX_ENTRIES = 2
        self.fetch.side_effect = [self.result, None, self.result, self.result]
        for title in ('Song', 'Other', 'Third'):
            self.observe(replace(self.playback, title=title))
            self.finish()
        self.assertEqual(len(self.job._entries), 2)
        self.observe()
        self.assertEqual(len(DeferredThread.targets), 1)
        self.finish()
        self.assertEqual(len(self.job._entries), 2)

    def test_worker_start_failure_uses_same_bounded_retry_path(self):
        with patch('lyricsplus_job.threading.Thread', side_effect=RuntimeError('test')):
            self.observe()
        self.assertIsNone(self.job.receive(self.event()))
        self.assertFalse(self.job.pending)
        self.assertFalse(self.job.done)
        self.fetch.assert_not_called()

    def test_close_rejects_inflight_result_and_future_observations(self):
        self.observe()
        self.job.close()
        self.assertIsNone(self.finish())
        self.observe()
        self.assertEqual(DeferredThread.targets, [])
        self.assertFalse(self.job.pending)

    def test_two_named_providers_keep_independent_requests_results_and_statuses(self):
        other_result = {'source': '알송', 'times': [2, 3, 4]}
        other_fetch = Mock(return_value=other_result)
        other = ProviderSyncJob(self.messages, other_fetch, name='알송')
        try:
            self.observe()
            other.observe(self.playback, enabled=True, reference=('first', 'last'))
            self.assertEqual(len(DeferredThread.targets), 2)
            DeferredThread.targets.pop(0)()
            first_event = self.event()
            self.assertEqual(first_event['provider'], 'LyricsPlus')
            self.assertEqual(self.job.receive(first_event), (self.playback.key, self.result))
            self.assertTrue(other.pending)
            DeferredThread.targets.pop(0)()
            second_event = self.event()
            self.assertEqual(second_event['provider'], '알송')
            self.assertEqual(other.receive(second_event), (self.playback.key, other_result))
            self.assertEqual(self.job.status, 'LyricsPlus 싱크 연결됨')
            self.assertEqual(other.status, '알송 싱크 연결됨')
            self.job.close()
            other.observe(replace(self.playback, title='Other'), enabled=True, reference=('first', 'last'))
            self.assertTrue(other.pending)
            self.assertEqual(len(DeferredThread.targets), 1)
        finally:
            other.close()


if __name__ == '__main__':
    unittest.main()
