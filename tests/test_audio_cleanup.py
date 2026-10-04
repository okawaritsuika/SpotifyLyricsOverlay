"""Worker completion survives cleanup failure without changing analysis outcome."""
import json
import queue
import threading
import unittest
from unittest.mock import MagicMock, Mock, patch

from audio_job import AudioJob
from audio_progress import AudioProgress


class AudioCleanupTests(unittest.TestCase):
    def process(self, *events):
        stream = MagicMock()
        stream.__iter__.side_effect = lambda: iter(json.dumps(event) + '\n' for event in events)
        return Mock(stdout=stream, wait=Mock(return_value=0))

    def job(self, *events, cancelled=False, analyze=False):
        job = AudioJob.__new__(AudioJob)
        job.messages = queue.Queue()
        job.cancelled = threading.Event()
        if cancelled:job.cancelled.set()
        job.process_lock = threading.Lock()
        job.skip_final = False
        job.waiting_for_lyrics = False
        job.temp = Mock(name='temporary-directory')
        job.temp.name = 'synthetic-temporary-directory'
        job._stop_realtime = Mock()
        job.request_path = 'synthetic-request.json'
        job.analysis_request_path = job.request_path
        if analyze:
            job.process = self.process({'kind': 'captured'})
            job._launch = Mock(return_value=self.process(*events))
        else:job.process = self.process(*events)
        return job

    def events(self, job):
        AudioJob._read(job)
        received = []
        while not job.messages.empty():received.append(job.messages.get_nowait()[1][1])
        self.assertEqual(sum(event['kind'] == 'finished' for event in received), 1)
        self.assertEqual(received[-1]['kind'], 'finished')
        return received

    def assert_warning(self, events, stage):
        errors = events[-1].get('cleanup_errors', [])
        self.assertTrue(any(error['stage'] == stage for error in errors), errors)
        self.assertEqual(events[-1]['temporary_directory'], 'synthetic-temporary-directory')

    def test_success_has_one_finished_without_cleanup_warning(self):
        job = self.job({'kind': 'result', 'result': {'times': [10]}})
        events = self.events(job)
        self.assertEqual([event['kind'] for event in events], ['result', 'finished'])
        self.assertNotIn('cleanup_errors', events[-1])
        job.temp.cleanup.assert_called_once()

    def test_worker_error_is_preserved_without_duplicate_error(self):
        job = self.job({'kind': 'error', 'text': 'synthetic worker failure'})
        events = self.events(job)
        self.assertEqual([event['kind'] for event in events], ['error', 'finished'])
        self.assertEqual(events[0]['text'], 'synthetic worker failure')

    def test_cancelled_result_is_not_published_but_finished_is(self):
        job = self.job({'kind': 'result', 'result': {'times': [10]}}, cancelled=True)
        events = self.events(job)
        self.assertEqual([event['kind'] for event in events], ['finished'])

    def test_temporary_cleanup_failure_keeps_success_and_finishes(self):
        job = self.job({'kind': 'result', 'result': {'times': [10]}})
        job.temp.cleanup.side_effect = PermissionError('synthetic locked temp')
        events = self.events(job)
        self.assertEqual([event['kind'] for event in events], ['result', 'finished'])
        self.assert_warning(events, 'temporary')

    def test_cancel_cleanup_failure_does_not_become_worker_error(self):
        job = self.job(cancelled=True)
        job.temp.cleanup.side_effect = PermissionError('synthetic locked temp')
        events = self.events(job)
        self.assertEqual([event['kind'] for event in events], ['finished'])
        self.assert_warning(events, 'temporary')

    def test_worker_error_and_cleanup_warning_remain_distinct(self):
        job = self.job({'kind': 'error', 'text': 'synthetic worker failure'})
        job.temp.cleanup.side_effect = OSError('synthetic cleanup failure')
        events = self.events(job)
        self.assertEqual([event['kind'] for event in events], ['error', 'finished'])
        self.assertEqual(events[0]['text'], 'synthetic worker failure')
        self.assert_warning(events, 'temporary')

    def test_final_realtime_shutdown_failure_releases_analysis_lock(self):
        job = self.job({'kind': 'result', 'result': {'times': [10]}}, analyze=True)
        job._stop_realtime.side_effect = [None, PermissionError('synthetic final shutdown race')]
        with patch('audio_job.ANALYSIS_LOCK') as analysis_lock:
            analysis_lock.acquire.return_value = True
            events = self.events(job)
            analysis_lock.release.assert_called_once()
        job.temp.cleanup.assert_called_once()
        self.assert_warning(events, 'realtime')
        self.assertEqual([event['kind'] for event in events], ['progress', 'result', 'finished'])

    def test_final_stdout_close_failure_still_cleans_temporary_files(self):
        job = self.job({'kind': 'result', 'result': {'times': [10]}})
        job.process.stdout.close.side_effect = OSError('synthetic final close failure')
        events = self.events(job)
        job.temp.cleanup.assert_called_once()
        self.assert_warning(events, 'stdout')
        self.assertEqual([event['kind'] for event in events], ['result', 'finished'])

    def test_terminal_result_shutdown_failure_does_not_become_worker_error(self):
        job = self.job({'kind': 'result', 'result': {'times': [10]}})
        job._stop_realtime.side_effect = OSError('synthetic final shutdown failure')
        events = self.events(job)
        self.assertEqual([event['kind'] for event in events], ['result', 'finished'])
        self.assert_warning(events, 'realtime')
        job.temp.cleanup.assert_called_once()

    def test_failed_capture_shutdown_does_not_start_another_model(self):
        job = self.job({'kind': 'result', 'result': {'times': [10]}}, analyze=True)
        job._stop_realtime.side_effect = OSError('synthetic capture shutdown failure')
        events = self.events(job)
        self.assertEqual([event['kind'] for event in events], ['error', 'finished'])
        job._launch.assert_not_called()
        self.assert_warning(events, 'realtime')

    def test_multiple_final_cleanup_failures_are_all_reported_once(self):
        job = self.job({'kind': 'result', 'result': {'times': [10]}}, analyze=True)
        job._stop_realtime.side_effect = [None, OSError('synthetic final shutdown failure')]
        job._launch.return_value.stdout.close.side_effect = OSError('synthetic final close failure')
        job.temp.cleanup.side_effect = OSError('synthetic locked temp')
        with patch('audio_job.ANALYSIS_LOCK') as analysis_lock:
            analysis_lock.acquire.return_value = True
            events = self.events(job)
            analysis_lock.release.assert_called_once()
        self.assertEqual([error['stage'] for error in events[-1]['cleanup_errors']], ['realtime', 'stdout', 'temporary'])
        self.assertEqual([event['kind'] for event in events], ['progress', 'result', 'finished'])

    def test_unexpected_shutdown_exception_still_releases_lock_and_finishes(self):
        job = self.job({'kind': 'result', 'result': {'times': [10]}}, analyze=True)
        job._stop_realtime.side_effect = [None, RuntimeError('synthetic programming defect')]
        with patch('audio_job.ANALYSIS_LOCK') as analysis_lock:
            analysis_lock.acquire.return_value = True
            with self.assertRaisesRegex(RuntimeError, 'programming defect'):AudioJob._read(job)
            analysis_lock.release.assert_called_once()
        job.temp.cleanup.assert_called_once()
        events = []
        while not job.messages.empty():events.append(job.messages.get_nowait()[1][1])
        self.assertEqual([event['kind'] for event in events], ['progress', 'result', 'finished'])
        self.assert_warning(events, 'realtime')

    def test_unexpected_stdout_exception_still_cleans_temporary_files_and_finishes(self):
        job = self.job({'kind': 'result', 'result': {'times': [10]}})
        job.process.stdout.close.side_effect = RuntimeError('synthetic programming defect')
        with self.assertRaisesRegex(RuntimeError, 'programming defect'):AudioJob._read(job)
        job.temp.cleanup.assert_called_once()
        events = []
        while not job.messages.empty():events.append(job.messages.get_nowait()[1][1])
        self.assertEqual([event['kind'] for event in events], ['result', 'finished'])
        self.assert_warning(events, 'stdout')


class AudioCleanupProgressTests(unittest.TestCase):
    def finished(self, stage='temporary'):
        return {'kind': 'finished', 'cleanup_errors': [{'stage': stage, 'error': 'PermissionError'}],
                'temporary_directory': 'synthetic-temporary-directory'}

    def test_success_phase_and_text_remain_when_cleanup_fails(self):
        progress = AudioProgress('Synthetic')
        progress.update({'kind': 'result'})
        progress.text = 'Synthetic saved timing'
        progress.update(self.finished())
        self.assertEqual(progress.phase, 'completed')
        self.assertEqual(progress.text, 'Synthetic saved timing')
        self.assertIn('임시 파일 정리 실패', progress.snapshot()['detail'])
        self.assertIn('synthetic-temporary-directory', progress.snapshot()['detail'])

    def test_persistence_error_phase_and_detail_remain(self):
        progress = AudioProgress('Synthetic')
        progress.update({'kind': 'error', 'text': 'Synthetic candidate saved but timing failed'})
        progress.update(self.finished())
        self.assertEqual(progress.phase, 'error')
        self.assertEqual(progress.text, 'Synthetic candidate saved but timing failed')
        self.assertIn('임시 파일 정리 실패', progress.snapshot()['detail'])

    def test_cancel_phase_and_detail_remain(self):
        progress = AudioProgress('Synthetic')
        progress.update(self.finished(), cancelled=True)
        self.assertEqual(progress.phase, 'cancelled')
        self.assertIn('분석 취소됨', progress.snapshot()['detail'])
        self.assertIn('임시 파일 정리 실패', progress.snapshot()['detail'])

    def test_non_file_resource_warning_does_not_claim_folder_remains(self):
        progress = AudioProgress('Synthetic')
        progress.update({'kind': 'result'})
        progress.update(self.finished('realtime'))
        self.assertEqual(progress.phase, 'completed')
        self.assertIn('임시 자원 정리 경고', progress.snapshot()['detail'])
        self.assertNotIn('정리할 폴더', progress.snapshot()['detail'])

    def test_normal_finished_is_unchanged(self):
        progress = AudioProgress('Synthetic')
        progress.update({'kind': 'result'})
        progress.text = 'Synthetic normal completion'
        progress.update({'kind': 'finished'})
        self.assertEqual(progress.snapshot()['detail'], 'Synthetic normal completion')


class RealtimeCleanupTests(unittest.TestCase):
    def job(self):
        job = AudioJob.__new__(AudioJob)
        job.messages = queue.Queue()
        job.cancelled = threading.Event()
        job.realtime_stop = threading.Event()
        job.process_lock = threading.Lock()
        job.realtime_process = None
        job.request_path = 'synthetic-request.json'
        job.analysis_request_path = job.request_path
        process = Mock()
        process.stdout = MagicMock()
        process.stdout.__iter__.return_value = iter(())
        process.wait.return_value = 0
        job._launch = Mock(return_value=process)
        return job, process

    def test_wait_failure_closes_stdout_and_releases_realtime_lock(self):
        job, process = self.job()
        process.wait.side_effect = [0, OSError('synthetic final realtime wait failure')]
        lock = threading.Lock()
        with patch('audio_job.REALTIME_LOCK', lock):
            with self.assertRaises(OSError):AudioJob._read_realtime(job)
        self.assertFalse(lock.locked())
        process.stdout.close.assert_called_once()

    def test_stdout_failure_releases_realtime_lock(self):
        job, process = self.job()
        process.stdout.close.side_effect = OSError('synthetic realtime close failure')
        lock = threading.Lock()
        with patch('audio_job.REALTIME_LOCK', lock):
            with self.assertRaises(OSError):AudioJob._read_realtime(job)
        self.assertFalse(lock.locked())

    def test_unexpected_wait_failure_releases_realtime_lock(self):
        job, process = self.job()
        process.wait.side_effect = [0, RuntimeError('synthetic realtime programming defect')]
        lock = threading.Lock()
        with patch('audio_job.REALTIME_LOCK', lock):
            with self.assertRaises(RuntimeError):AudioJob._read_realtime(job)
        self.assertFalse(lock.locked())
        process.stdout.close.assert_called_once()


if __name__ == '__main__':unittest.main()
