"""Playback stays clear of analysis messages while the analysis window reports progress."""
import tempfile
import threading
import time
import tkinter as tk
import unittest

from app import LyricsApp
from audio_progress import AudioProgress
from lyrics import Line,Song
from media import Playback
from storage import Store


class FakeJob:
    def __init__(self,song,playback):
        self.song=song;self.playback_key=playback.key
        self.analysis_key=(playback.key,song.fingerprint)
        self.cancelled=threading.Event();self.realtime_stop=threading.Event()
        self.memory_only=False;self.transport_generation=0
    def cancel(self):self.cancelled.set()
    def close(self):self.cancel()


class AnalysisStatusUITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(self.temp.name)
        self.store.save_settings({'online':False,'auto_audio':False,'borderless':True})
        self.song=Song('sample','Kira Kira','Artist','Album',tuple(Line(f'日本語の例{i}','','예시') for i in range(30)))
        self.store.save_library([self.song])
        self.root=tk.Tk();self.root.withdraw()
        self.app=LyricsApp(self.root,self.store,start_services=False)
        self.playback=Playback(self.song.title,self.song.artist,self.song.album,89,186,True,time.monotonic())
        self.app.set_playback(self.playback)
        self.job=FakeJob(self.song,self.playback)
        self.app.spotify_sample_after=time.monotonic()-5

    def tearDown(self):self.app.close();self.temp.cleanup()

    def attach(self,phase='recording'):
        analyzer=self.app.auto_analyzer
        analyzer.jobs[self.job]=phase
        progress=analyzer.progress[self.job]=AudioProgress(self.song.title)
        progress.update({'kind':'progress','state':phase,'completed':50,'total':100,'text':'작업 중'})

    def event(self,index,**match):
        now=time.monotonic()
        return {'kind':'realtime','observed_at':now,'position':self.playback.position(now),
                'runtime':{'device':'cuda'},'match':{'index':index,**match} if index is not None else None}

    def assert_playback_has_no_analysis_messages(self):
        self.assertFalse(self.app.lyric_frame.grid_slaves(row=2))
        self.assertEqual(self.app.mode_label.cget('text'),'')
        self.assertEqual(self.app.status.cget('text'),'')
        self.assertRegex(self.app.playback_badge.cget('text'),r'^\d{2}:\d{2} / \d{2}:\d{2}$')

    def test_model_preparation_capture_and_tracking_leave_playback_clear(self):
        self.attach()
        for state in ('loading','buffering','failed'):
            with self.subTest(state=state):
                self.app.receive_realtime(self.job,{'kind':'realtime_status','state':state,'text':'분석 상태'})
                self.assert_playback_has_no_analysis_messages()
        self.app.receive_realtime(self.job,self.event(None))
        self.assert_playback_has_no_analysis_messages()
        self.app.receive_realtime(self.job,self.event(5))
        self.app.realtime_matched_at-=40;self.app.render()
        self.assert_playback_has_no_analysis_messages()
        self.assertEqual(self.app.displayed_index(),5)

    def test_compact_overlay_has_no_analysis_row_for_saved_results(self):
        self.app.timing={'times':list(range(0,150,5)),'offset':0,'source':'음원 분석'}
        self.app.render()
        self.app.manual_window_size=True;self.root.geometry('610x207+50+50')
        self.root.deiconify();self.root.update();self.root.update()
        self.assert_playback_has_no_analysis_messages()
        self.assertFalse(self.app.mode_label.winfo_ismapped())
        self.assertTrue(self.app.playback_badge.winfo_ismapped())

    def test_previous_analysis_result_is_available_in_analysis_window(self):
        self.attach('analyzing')
        self.app.playback=Playback('Next','Other','Album',30,180,True,time.monotonic())
        self.app.render()
        self.assert_playback_has_no_analysis_messages()
        self.app.auto_analyzer.receive(self.job,{'kind':'error','text':'구절을 찾지 못함'},[self.song])
        self.app.auto_analyzer.receive(self.job,{'kind':'finished'},[self.song])
        self.app.render()
        self.assert_playback_has_no_analysis_messages()
        self.app.open_audio();self.root.update()
        self.assertIn('오류',self.app.audio_queue_tree.item('auto-last','values')[1])
        self.assertIn('구절을 찾지 못함',self.app.audio_progress_detail.get('1.0','end-1c'))

    def test_long_recovery_requires_two_distinct_strong_lines(self):
        self.app.receive_realtime(self.job,self.event(5))
        evidence=dict(recovery=True,unique=True,confirmations=2,confidence=.9,
                      characters=10,similarity=.95,match_method='kana')
        self.app.receive_realtime(self.job,self.event(22,**evidence))
        self.assertEqual(self.app.realtime_index,5)
        self.app.receive_realtime(self.job,self.event(22,**evidence,global_recovery=True,distinct_lines=1))
        self.assertEqual(self.app.realtime_index,5)
        evidence.update(characters=6)
        self.app.receive_realtime(self.job,self.event(22,**evidence,global_recovery=True,distinct_lines=2,
                                                     recovery_start_index=21,recovery_characters=13))
        self.assertEqual(self.app.realtime_index,22)
        self.app.receive_realtime(self.job,self.event(3,**evidence,global_recovery=True,distinct_lines=2,
                                                     recovery_start_index=2,recovery_characters=13))
        self.assertEqual(self.app.realtime_index,22)
        self.assertEqual(self.store.timing(self.song)['times'],[None]*30)

    def test_manual_analysis_progress_and_result_are_available_in_analysis_window(self):
        self.app.audio_job=self.job
        progress=self.app.audio_manual_progress=AudioProgress(self.song.title,mode='직접')
        progress.update({'kind':'progress','state':'analyzing','completed':50,'total':100,
                         'runtime':{'device':'cuda'}})
        self.app.render()
        self.assert_playback_has_no_analysis_messages()
        self.app.open_audio();self.root.update()
        self.assertIn('50%',self.app.audio_queue_tree.item('manual','values')[2])
        progress.update({'kind':'error','text':'구절을 찾지 못함'})
        progress.update({'kind':'finished'});self.app.audio_job=None
        self.app.render()
        self.assert_playback_has_no_analysis_messages()
        self.app.refresh_audio_progress()
        self.assertIn('오류',self.app.audio_queue_tree.item('manual','values')[1])


if __name__=='__main__':unittest.main()
