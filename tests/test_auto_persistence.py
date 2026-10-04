"""Failed persistence must not turn an analysis result into a claimed saved sync."""
from dataclasses import replace
from pathlib import Path
import queue
import tempfile
import time
import unittest
from unittest.mock import patch

from auto_audio import AutoAnalyzer
from lyrics import Line, Song
from media import Playback
from storage import Store, atomic_json
from test_auto_audio import FakeJob


class AutoPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.song = Song('persistence', 'Synthetic', 'Singer', 'Album',
                         tuple(Line(text, '', '') for text in ('First phrase', 'Second phrase', 'Third phrase')))
        self.playback = Playback(self.song.title, self.song.artist, self.song.album,
                                 0, 90, True, time.monotonic())
        self.auto = self.make_auto()

    def make_auto(self):
        auto = AutoAnalyzer('.', self.store, queue.Queue(), job_factory=FakeJob)
        auto.enabled = True
        self.addCleanup(auto.close)
        return auto

    def setup_job(self, memory_only=False):
        value = {'times': [5, None, None], 'offset': .5, 'origins': ['manual', None, None]}
        if memory_only:
            self.auto.live_timing.save(self.song, value)
            path = self.auto.live_timing.path(self.song)
        else:
            self.store.save_timing(self.song, value)
            path = self.store.directory / 'timings' / (self.song.id + '.json')
        self.auto.observe(self.song, self.playback, memory_only=memory_only)
        return next(iter(self.auto.jobs)), path, path.read_bytes()

    def result(self, times=(10, 20, 30), partial=False):
        return {'kind': 'result', 'result': {'times': list(times), 'duration': 90, 'partial': partial}}

    def assert_error_after_finished(self, job):
        self.assertEqual(self.auto.progress[job].snapshot()['phase'], '오류')
        self.auto.receive(job, {'kind': 'finished'}, [self.song])
        self.assertEqual(self.auto.last_progress.snapshot()['phase'], '오류')
        self.assertNotIn('분석 완료', self.auto.last_progress.snapshot()['detail'])

    def fail_final_save(self, memory_only=False):
        job, path, before = self.setup_job(memory_only)
        owner, method = (self.auto.live_timing, 'save') if memory_only else (self.store, 'save_timing')
        with patch.object(owner, method, side_effect=PermissionError('persistent timing denial')):
            with self.assertRaises(OSError):
                self.auto.receive(job, self.result(), [self.song])
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(self.auto.draft_path(self.song, memory_only).is_file())
        return job, path, before

    def test_draft_failure_is_error_and_preserves_original(self):
        job, path, before = self.setup_job()
        with patch('auto_audio.atomic_json', side_effect=PermissionError('persistent draft denial')):
            with self.assertRaises(OSError):
                self.auto.receive(job, self.result(), [self.song])
        self.assertFalse(self.auto.draft_path(self.song).exists())
        self.assertEqual(path.read_bytes(), before)
        self.assertIn('후보', self.auto.progress[job].text)
        self.assertIn('실패', self.auto.progress[job].text)
        self.assert_error_after_finished(job)

    def test_json_validation_failure_never_reports_completed(self):
        job, path, before = self.setup_job()
        event = self.result()
        event['result']['confidence'] = float('nan')
        with self.assertRaises(ValueError):
            self.auto.receive(job, event, [self.song])
        self.assertFalse(self.auto.draft_path(self.song).exists())
        self.assertEqual(path.read_bytes(), before)
        self.assert_error_after_finished(job)

    def test_background_failure_does_not_replace_current_song_status(self):
        job, path, before = self.setup_job()
        self.auto.receive(job,{'kind':'progress','state':'analyzing','text':'전체 분석 중'},[self.song])
        other = Song('other', 'Other', 'Singer', 'Album', self.song.lines)
        self.auto.observe(other, Playback(other.title, other.artist, other.album,
                                         0, 90, True, time.monotonic()))
        status = self.auto.status
        with patch('auto_audio.atomic_json', side_effect=PermissionError('persistent draft denial')):
            with self.assertRaises(OSError):
                self.auto.receive(job, self.result(), [self.song, other])
        self.assertEqual(self.auto.status, status)
        self.assertEqual(path.read_bytes(), before)
        self.assert_error_after_finished(job)

    def test_backup_failure_keeps_candidate_and_reports_unapplied(self):
        job, path, before = self.setup_job()
        def write(destination, value):
            if Path(destination).parent.name == 'audio_backups':
                raise PermissionError('persistent backup denial')
            return atomic_json(destination, value)
        with patch('auto_audio.atomic_json', side_effect=write):
            with self.assertRaises(OSError):
                self.auto.receive(job, self.result(), [self.song])
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(self.auto.draft_path(self.song).is_file())
        self.assertIn('후보', self.auto.progress[job].text)
        self.assertIn('적용 실패', self.auto.progress[job].text)
        self.assert_error_after_finished(job)

    def test_library_timing_failure_keeps_reviewable_candidate(self):
        job, path, before = self.fail_final_save()
        self.assertIn('분석창', self.auto.progress[job].text)
        self.assertIn('적용 실패', self.auto.progress[job].text)
        self.assert_error_after_finished(job)
        self.assertEqual(path.read_bytes(), before)

    def test_live_failure_does_not_offer_unsupported_candidate_dialog(self):
        job, path, before = self.fail_final_save(memory_only=True)
        self.assertIn('후보', self.auto.progress[job].text)
        self.assertIn('적용 실패', self.auto.progress[job].text)
        self.assertNotIn('분석창', self.auto.progress[job].text)
        self.assert_error_after_finished(job)
        self.assertEqual(path.read_bytes(), before)

    def test_failed_full_result_never_retries_on_repeated_observation(self):
        job, path, before = self.fail_final_save()
        self.auto.receive(job, {'kind': 'finished'}, [self.song])
        for _ in range(20):
            self.auto.observe(self.song, replace(self.playback, captured=time.monotonic()))
        self.auto.observe(self.song, replace(self.playback, playing=False))
        self.auto.observe(self.song, replace(self.playback, captured=time.monotonic()))
        self.assertFalse(self.auto.jobs)
        self.assertEqual(path.read_bytes(), before)

    def test_restart_labels_candidate_without_claiming_saved_audio_timing(self):
        job, path, before = self.fail_final_save()
        self.auto.receive(job, {'kind': 'finished'}, [self.song])
        recovered = self.make_auto()
        recovered.observe(self.song, self.playback)
        self.assertFalse(recovered.jobs)
        self.assertIn('후보', recovered.status)
        self.assertIn('분석창', recovered.status)
        self.assertNotIn('저장한 싱크', recovered.status)
        self.assertEqual(path.read_bytes(), before)

    def test_live_restart_labels_candidate_without_dialog_promise(self):
        job, path, before = self.fail_final_save(memory_only=True)
        self.auto.receive(job, {'kind': 'finished'}, [self.song])
        recovered = self.make_auto()
        recovered.observe(self.song, self.playback, memory_only=True)
        self.assertFalse(recovered.jobs)
        self.assertIn('후보', recovered.status)
        self.assertNotIn('분석창', recovered.status)
        self.assertEqual(path.read_bytes(), before)

    def test_successful_timing_write_reports_completed_and_preserves_manual(self):
        job, path, before = self.setup_job()
        self.assertIs(self.auto.receive(job, self.result(), [self.song]), self.song)
        self.auto.receive(job, {'kind': 'finished'}, [self.song])
        self.assertEqual(self.auto.last_progress.snapshot()['phase'], '분석 완료')
        self.assertEqual(self.store.timing(self.song)['times'], [5, 19.5, 29.5])
        recovered = self.make_auto()
        recovered.observe(self.song, self.playback)
        self.assertFalse(recovered.jobs)
        self.assertIn('저장한 싱크', recovered.status)

    def test_low_coverage_remains_a_completed_candidate_without_timing_write(self):
        job, path, before = self.setup_job()
        self.assertIsNone(self.auto.receive(job, self.result((10, None, None)), [self.song]))
        self.auto.receive(job, {'kind': 'finished'}, [self.song])
        self.assertEqual(self.auto.last_progress.snapshot()['phase'], '분석 완료')
        self.assertIn('후보만', self.auto.last_progress.text)
        self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
