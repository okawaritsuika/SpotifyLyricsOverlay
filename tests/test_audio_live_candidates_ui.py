from dataclasses import replace
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import test_audio as fixture
from tests import test_auto_audio as auto_fixture
from audio_progress import AudioProgress
from media import Playback
from storage import atomic_json


class LiveCandidateUITests(unittest.TestCase):
    def setUp(self):fixture.AudioUITests.setUp(self)
    def tearDown(self):fixture.AudioUITests.tearDown(self)

    def prepare_live(self):
        playback=Playback('Temporary Track','Artist','Album',duration=180,captured=time.monotonic())
        song=self.app.live_lyrics.receive(playback,None,self.song.lines)
        self.app.playback=playback
        self.app.select_song(song)
        path=self.app.auto_analyzer.draft_path(song,True)
        atomic_json(path,{'times':[10,20,30],'fingerprint':self.app.auto_analyzer.signature(song,True)})
        return song,path

    def test_live_candidate_is_visible_and_readonly_even_after_track_change(self):
        song,path=self.prepare_live();before=path.read_bytes()
        self.app.auto_analyzer.enabled=True
        self.app.open_audio();self.app.audio_tabs.select(1);self.root.update()
        self.assertEqual(self.app.audio_tabs.tab(1,'text'),'실시간 가사 분석 후보 · 보기 전용')
        self.assertEqual(self.app.audio_tree.item('1','values'),('00:20.00',song.lines[1].original))
        self.assertEqual(str(self.app.audio_apply_button.cget('state')),'disabled')
        self.app.live_lyrics.clear();self.app.select_song(self.other)
        self.app.refresh_audio()
        self.assertEqual(str(self.app.audio_apply_button.cget('state')),'disabled')
        with self.assertRaisesRegex(ValueError,'보기 전용'):self.app.apply_audio()
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(self.store.timing(self.other)['times'],[None]*3)
        self.assertFalse(self.app.auto_analyzer.live_timing.path(song).exists())

    def test_reopening_library_candidate_restores_existing_explicit_apply(self):
        self.prepare_live();self.app.open_audio();self.app.close_audio()
        self.app.select_song(self.song)
        atomic_json(self.app.audio_draft_path(self.song),{
            'times':[10,20,30],'fingerprint':self.song.fingerprint,'duration':180})
        self.app.open_audio();self.root.update()
        self.assertFalse(self.app.audio_draft_readonly)
        self.assertEqual(str(self.app.audio_apply_button.cget('state')),'normal')
        self.assertEqual(self.app.audio_tabs.tab(1,'text'),'싱크 후보')
        self.app.apply_audio()
        self.assertEqual(self.store.timing(self.song)['times'],[10,20,30])

    def test_failed_live_timing_commit_shows_error_and_keeps_candidate_viewable(self):
        song,draft=self.prepare_live();draft.unlink()
        auto=self.app.auto_analyzer
        auto.enabled=True;auto.job_factory=auto_fixture.FakeJob
        auto.live_timing.save(song,{'times':[5,None,None],'offset':0})
        path=auto.live_timing.path(song);before=path.read_bytes()
        auto.observe(song,replace(self.app.playback,playing=True),memory_only=True)
        job=next(iter(auto.jobs))
        with patch.object(auto.live_timing,'save',side_effect=PermissionError('test denied')):
            with self.assertRaises(OSError):
                self.app.receive_auto_audio(job,{'kind':'result','result':{'times':[10,20,30]}})
        self.app.receive_auto_audio(job,{'kind':'finished'})
        self.app.open_audio();self.root.update()
        self.assertIn('오류',self.app.audio_queue_tree.item('auto-last','values')[1])
        self.assertIn('시간표 적용 실패',self.app.audio_progress_detail.get('1.0','end'))
        self.assertEqual(self.app.audio_draft['times'],[10,20,30])
        self.assertEqual(str(self.app.audio_apply_button.cget('state')),'disabled')
        self.assertEqual(path.read_bytes(),before)

    def test_new_library_manual_job_resets_readonly_and_completed_result_is_applicable(self):
        self.prepare_live();self.app.open_audio()
        self.app.live_lyrics.clear();self.app.select_song(self.song)
        self.app.playback=Playback(self.song.title,self.song.artist,self.song.album,
                                  duration=180,playing=True,captured=time.monotonic())
        job=SimpleNamespace(song=self.song,cancelled=threading.Event(),cancel=lambda:None)
        with patch('app.audio_ready'),patch('app.AudioJob',return_value=job):self.app.start_audio()
        self.assertFalse(self.app.audio_draft_readonly)
        self.assertIsNone(self.app.audio_draft)
        self.app.receive_audio(job,{'kind':'result','result':{'times':[10,20,30],'duration':180}})
        self.app.receive_audio(job,{'kind':'finished'})
        self.assertEqual(str(self.app.audio_apply_button.cget('state')),'normal')
        self.app.apply_audio()
        self.assertEqual(self.store.timing(self.song)['times'],[10,20,30])

    def test_failed_manual_candidate_save_remains_error_after_finished_and_reopen(self):
        self.app.open_audio()
        path=self.app.audio_draft_path(self.song)
        atomic_json(path,{'times':[5,None,None],'fingerprint':self.song.fingerprint})
        before=path.read_bytes()
        job=SimpleNamespace(song=self.song,cancelled=threading.Event())
        job.recording_context=self.app.recording_contexts.capture(self.song,
            Playback(self.song.title,self.song.artist,self.song.album,duration=180))
        self.app.audio_job=job
        self.app.audio_manual_progress=AudioProgress(self.song.title,mode='직접')
        with patch('app.atomic_json',side_effect=PermissionError('test denied')):
            with self.assertRaises(OSError):
                self.app.receive_audio(job,{'kind':'result','result':{'times':[10,20,30]}})
        self.app.receive_audio(job,{'kind':'finished'})
        self.app.close_audio();self.app.open_audio();self.root.update()
        self.assertIn('오류',self.app.audio_queue_tree.item('manual','values')[1])
        self.assertIn('후보 저장 실패',self.app.audio_progress_detail.get('1.0','end'))
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(self.store.timing(self.song)['times'],[None]*3)


if __name__=='__main__':unittest.main()
