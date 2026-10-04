from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app import LyricsApp
from audio_progress import AudioProgress
from lyrics import Line, Song
from media import Playback
from recording_context import RecordingContexts
from storage import atomic_json


class Status:
    def __init__(self):self.value='분석 중'
    def set(self,value):self.value=value
    def get(self):return self.value


class ManualPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'candidate.json'
        atomic_json(self.path,{'previous':'candidate'});self.before=self.path.read_bytes()
        self.song=Song('manual','Synthetic','Artist','Album',(Line('Example','',''),))
        contexts=RecordingContexts()
        self.job=SimpleNamespace(song=self.song,cancelled=threading.Event(),recording_context=contexts.capture(
            self.song,Playback(self.song.title,self.song.artist,self.song.album,duration=90)))
        self.app=SimpleNamespace(audio_job=self.job,realtime_job=None,audio_draft=None,
            audio_manual_progress=AudioProgress(self.song.title,mode='직접'),
            recording_contexts=contexts,songs=[self.song],audio_status=Status(),audio_state='음원 분석 중',
            refresh_audio_progress=Mock(),refresh_audio=Mock(),notify=Mock(),audio_draft_path=lambda song:self.path)

    def assert_failed(self):
        LyricsApp.receive_audio(self.app,self.job,{'kind':'finished'})
        self.assertIsNone(self.app.audio_job)
        self.assertEqual(self.app.audio_manual_progress.snapshot()['phase'],'오류')
        self.assertIn('후보 저장 실패',self.app.audio_status.get())
        self.assertEqual(self.path.read_bytes(),self.before)
        self.assertIsNone(self.app.audio_draft)

    def test_failed_draft_write_preserves_old_file_and_reports_error_after_finished(self):
        with patch('app.atomic_json',side_effect=PermissionError('test denied')):
            with self.assertRaises(OSError):
                LyricsApp.receive_audio(self.app,self.job,{'kind':'result','result':{'times':[10]}})
        self.assert_failed()

    def test_invalid_result_reports_error_without_publishing_completed_or_writing(self):
        with patch('app.atomic_json') as write,self.assertRaises(ValueError):
            LyricsApp.receive_audio(self.app,self.job,{'kind':'result','result':{'times':[-1]}})
        write.assert_not_called()
        self.assert_failed()

    def test_completion_is_published_only_after_candidate_was_saved(self):
        def save(path,result):
            self.assertNotEqual(self.app.audio_manual_progress.snapshot()['phase'],'분석 완료')
            atomic_json(path,result)
        with patch('app.atomic_json',side_effect=save):
            LyricsApp.receive_audio(self.app,self.job,{'kind':'result','result':{'times':[10]}})
        LyricsApp.receive_audio(self.app,self.job,{'kind':'finished'})
        self.assertEqual(self.app.audio_manual_progress.snapshot()['phase'],'분석 완료')
        self.assertEqual(self.app.audio_draft['times'],[10])
        self.assertFalse(self.app.audio_draft_readonly)


if __name__=='__main__':unittest.main()
