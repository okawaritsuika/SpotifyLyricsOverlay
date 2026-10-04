"""Visible status distinguishes uncertain live tracking and prior-track work."""
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

    def test_running_without_match_and_lost_match_have_explicit_visible_status(self):
        self.attach()
        self.app.receive_realtime(self.job,self.event(None))
        self.assertIn('GPU 분석 중 · 구절 미확인',self.app.analysis_badge.cget('text'))
        self.assertIn('수집 50%',self.app.analysis_badge.cget('text'))
        self.app.receive_realtime(self.job,self.event(5))
        self.assertIn('6행 확인',self.app.analysis_badge.cget('text'))
        self.app.realtime_matched_at-=40;self.app.render()
        self.assertIn('GPU 재탐색',self.app.analysis_badge.cget('text'))
        self.assertEqual(self.app.displayed_index(),5)

    def test_compact_overlay_keeps_status_separate_from_hidden_mode_label(self):
        self.attach();self.app.receive_realtime(self.job,self.event(None))
        self.app.manual_window_size=True;self.root.geometry('610x207+50+50')
        self.root.deiconify();self.root.update();self.root.update()
        badge=self.app.analysis_badge
        self.assertTrue(badge.winfo_ismapped())
        self.assertFalse(self.app.mode_label.winfo_ismapped())
        self.assertGreater(badge.winfo_height(),10)
        self.assertGreaterEqual(badge.winfo_rooty(),self.root.winfo_rooty())
        self.assertLessEqual(badge.winfo_rooty()+badge.winfo_height(),self.root.winfo_rooty()+207)
        self.assertLessEqual(self.app.lyric_content.winfo_rooty()+self.app.lyric_content.winfo_height(),badge.winfo_rooty())

    def test_previous_analysis_and_terminal_result_remain_after_next_track(self):
        self.attach('analyzing')
        self.app.playback=Playback('Next','Other','Album',30,180,True,time.monotonic())
        self.app.render()
        self.assertIn('Kira Kira: 음원 분석 50%',self.app.analysis_badge.cget('text'))
        self.app.auto_analyzer.receive(self.job,{'kind':'error','text':'구절을 찾지 못함'},[self.song])
        self.app.auto_analyzer.receive(self.job,{'kind':'finished'},[self.song])
        self.app.render()
        self.assertIn('Kira Kira: 분석 오류',self.app.analysis_badge.cget('text'))
        self.assertIn('구절을 찾지 못함',self.app.analysis_hint.text)
        self.app.open_audio();self.root.update()
        self.assertIn('오류',self.app.audio_queue_tree.item('auto-last','values')[1])

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

    def test_manual_analysis_is_visible_even_with_auto_disabled_then_survives_completion(self):
        self.app.audio_job=self.job;self.app.audio_manual_key=self.playback.key
        progress=self.app.audio_manual_progress=AudioProgress(self.song.title,mode='직접')
        progress.update({'kind':'progress','state':'analyzing','completed':50,'total':100,
                         'runtime':{'device':'cuda'}})
        self.app.render()
        self.assertIn('직접 GPU 음원 분석 50%',self.app.analysis_badge.cget('text'))
        self.assertNotIn('분석 꺼짐',self.app.analysis_badge.cget('text'))
        progress.update({'kind':'error','text':'구절을 찾지 못함'})
        progress.update({'kind':'finished'});self.app.audio_job=None
        self.app.render()
        self.assertIn('오류',self.app.analysis_badge.cget('text'))


if __name__=='__main__':unittest.main()
