from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from audio_progress import AudioProgress
from media import Playback
from tests import test_audio as fixture
from tests import test_auto_audio as auto_fixture


class CleanupUITests(unittest.TestCase):
    def setUp(self):fixture.AudioUITests.setUp(self)
    def tearDown(self):fixture.AudioUITests.tearDown(self)

    def warning(self,stage='temporary'):
        return {'kind':'finished','cleanup_errors':[{'stage':stage,'error':'PermissionError','winerror':32}],
                'temporary_directory':str(Path(self.temp.name)/'unreleased-audio')}

    def test_saved_manual_candidate_stays_completed_and_cleanup_path_survives_reopen(self):
        self.app.open_audio()
        job=SimpleNamespace(song=self.song,cancelled=threading.Event())
        job.recording_context=self.app.recording_contexts.capture(self.song,
            Playback(self.song.title,self.song.artist,self.song.album,duration=90))
        self.app.audio_job=job;self.app.audio_manual_progress=AudioProgress(self.song.title,mode='직접')
        self.app.receive_audio(job,{'kind':'result','result':{'times':[10,20,30]}})
        candidate=self.app.audio_draft_path(self.song).read_bytes()
        with patch.object(self.app,'notify',wraps=self.app.notify) as notify:
            self.app.receive_audio(job,self.warning());self.app.receive_audio(job,self.warning())
            notify.assert_called_once()
        self.app.close_audio();self.app.open_audio();self.root.update()
        self.assertIn('분석 완료',self.app.audio_queue_tree.item('manual','values')[1])
        detail=self.app.audio_progress_detail.get('1.0','end')
        self.assertIn('임시 파일 정리 실패',detail)
        self.assertIn(self.warning()['temporary_directory'],detail)
        self.assertEqual(self.app.audio_draft_path(self.song).read_bytes(),candidate)
        self.assertEqual(self.app.audio_draft['times'],[10,20,30])

    def test_failed_auto_timing_remains_error_with_resource_cleanup_warning(self):
        self.app.open_audio()
        auto=self.app.auto_analyzer;auto.enabled=True;auto.job_factory=auto_fixture.FakeJob
        auto.observe(self.song,Playback(self.song.title,self.song.artist,self.song.album,duration=90,playing=True,captured=time.monotonic()))
        job=next(iter(auto.jobs))
        with patch.object(self.store,'save_timing',side_effect=PermissionError('test denied')):
            with self.assertRaises(OSError):
                self.app.receive_auto_audio(job,{'kind':'result','result':{'times':[10,20,30]}})
        with patch.object(self.app,'notify',wraps=self.app.notify) as notify:
            self.app.receive_auto_audio(job,self.warning('stdout'))
            self.app.receive_auto_audio(job,self.warning('stdout'))
            notify.assert_called_once()
            self.assertIn('분석 자원 정리 경고',notify.call_args.args[0])
        self.root.update()
        self.assertIn('오류',self.app.audio_queue_tree.item('auto-last','values')[1])
        detail=self.app.audio_progress_detail.get('1.0','end')
        self.assertIn('시간표 적용 실패',detail)
        self.assertIn('임시 자원 정리 경고',detail)
        self.assertNotIn('정리할 폴더:',detail)
        self.assertEqual(self.store.timing(self.song)['times'],[None]*3)


if __name__=='__main__':unittest.main()
