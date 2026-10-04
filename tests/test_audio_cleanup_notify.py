import queue
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from app import LyricsApp
from audio_progress import AudioProgress
from auto_audio import AutoAnalyzer
from live_lyrics import LiveLyrics
from lyrics import Line, Song
from media import Playback
from storage import Store
from tests import test_auto_audio as auto_fixture
from tests import test_manual_audio_persistence as status_fixture


WARNING={'kind':'finished','cleanup_errors':[{'stage':'temporary','error':'PermissionError','winerror':32}],
         'temporary_directory':'C:/synthetic-temp'}


class CleanupNotificationTests(unittest.TestCase):
    def base_app(self,**options):
        status=status_fixture.Status();status.set('기존 분석 상태')
        app=SimpleNamespace(realtime_job=None,audio_status=status,audio_state='',
            refresh_audio_progress=Mock(),refresh_audio=Mock(),notify=Mock(),**options)
        app.notify_audio_cleanup=lambda event:LyricsApp.notify_audio_cleanup(app,event)
        return app

    def test_manual_warning_notifies_once_without_replacing_result_error_or_cancellation(self):
        for kind,cancelled,phase in (('result',False,'분석 완료'),('error',False,'오류'),('result',True,'취소됨')):
            with self.subTest(phase=phase):
                job=SimpleNamespace(cancelled=threading.Event())
                if cancelled:job.cancelled.set()
                progress=AudioProgress('Synthetic');progress.update({'kind':kind,'text':'기존 작업 설명'})
                app=self.base_app(audio_job=job,audio_manual_progress=progress)
                LyricsApp.receive_audio(app,job,WARNING)
                LyricsApp.receive_audio(app,job,WARNING)
                app.notify.assert_called_once()
                self.assertIn('임시 음원 파일 정리 실패',app.notify.call_args.args[0])
                self.assertEqual(progress.snapshot()['phase'],phase)
                if not cancelled:
                    self.assertEqual(progress.text,'기존 작업 설명')
                    self.assertEqual(app.audio_status.get(),'기존 분석 상태')
                self.assertIn('정리할 폴더: C:/synthetic-temp',progress.snapshot()['detail'])

    def test_auto_warning_notifies_only_for_an_owned_job_and_preserves_status(self):
        with tempfile.TemporaryDirectory() as directory:
            song=Song('sample','Synthetic','Artist','Album',(Line('Example','',''),))
            auto=AutoAnalyzer('.',Store(directory),queue.Queue(),job_factory=auto_fixture.FakeJob)
            self.addCleanup(auto.close);auto.enabled=True
            auto.observe(song,Playback(song.title,song.artist,song.album,duration=90,playing=True,captured=time.monotonic()))
            job=next(iter(auto.jobs));auto.progress[job].update({'kind':'result','text':'기존 완료 설명'})
            auto.status='기존 자동 분석 상태'
            app=self.base_app(audio_job=None,auto_analyzer=auto,songs=[song],song=None,live_lyrics=LiveLyrics())
            LyricsApp.receive_auto_audio(app,job,WARNING)
            LyricsApp.receive_auto_audio(app,job,WARNING)
            app.notify.assert_called_once()
            self.assertEqual(auto.last_progress.phase,'completed')
            self.assertEqual(auto.last_progress.text,'기존 완료 설명')
            self.assertEqual(app.audio_status.get(),'기존 자동 분석 상태')

    def test_normal_finished_has_no_extra_notification(self):
        job=SimpleNamespace(cancelled=threading.Event())
        app=self.base_app(audio_job=job,audio_manual_progress=AudioProgress('Synthetic'))
        LyricsApp.receive_audio(app,job,{'kind':'finished'})
        app.notify.assert_not_called()

    def test_non_file_warning_does_not_claim_temporary_file_failure(self):
        app=self.base_app()
        LyricsApp.notify_audio_cleanup(app,{'cleanup_errors':[{'stage':'stdout','error':'OSError'}]})
        self.assertIn('분석 자원 정리 경고',app.notify.call_args.args[0])
        self.assertNotIn('폴더',app.notify.call_args.args[0])

    def test_unknown_warning_payloads_do_not_show_a_false_cleanup_notice(self):
        app=self.base_app()
        for errors in (None,{},'failed',[],[None], [{'stage':'unsupported'}]):
            LyricsApp.notify_audio_cleanup(app,{'cleanup_errors':errors})
        app.notify.assert_not_called()


if __name__=='__main__':unittest.main()
