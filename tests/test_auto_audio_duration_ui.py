"""Exercise recording-length cancellation through the actual app dispatcher."""
import gc
import tempfile
import time
import tkinter as tk
import unittest

from app import LyricsApp
from lyrics import Line
from media import Playback
from storage import Store
from test_auto_audio import FakeJob as BaseFakeJob


class FakeJob(BaseFakeJob):
    def __init__(self,base,song,playback,messages,**options):
        super().__init__(base,song,playback,messages,**options)
        self.playback_key=playback.key


class AutoAudioDurationUITests(unittest.TestCase):
    def setUp(self):
        gc.collect()
        self.temp=tempfile.TemporaryDirectory();self.store=Store(self.temp.name)
        self.store.save_settings({'online':False,'auto_audio':False,'web_auto':False,'borderless':False,
                                  'use_estimate':False,'reader_mode':'chrome'})
        self.root=tk.Tk();self.root.withdraw()
        self.app=LyricsApp(self.root,self.store,start_services=False)
        self.app.auto_analyzer.job_factory=FakeJob
        self.app.auto_analyzer.enabled=True

    def tearDown(self):
        self.app.close();self.temp.cleanup()
        self.app=self.root=None;gc.collect()

    def playback(self,duration):
        return Playback('Duration fixture','Example singer','Example album',0,duration,True,time.monotonic())

    def start_live(self):
        lines=tuple(Line(text,'','') for text in ('First phrase','Second phrase','Third phrase'))
        self.app.set_playback(self.playback(90))
        self.app.select_song(self.app.live_lyrics.receive(self.app.playback,None,lines))
        self.app.observe_auto_audio()
        return next(iter(self.app.auto_analyzer.jobs))

    def test_changed_duration_rejects_old_final_result_through_app_and_preserves_manual_file(self):
        old=self.start_live()
        self.app.set_playback(self.playback(180))
        timing={'times':[15,None,None],'offset':0,'origins':['manual',None,None]}
        self.app.save_current_timing(timing);self.app.timing=self.app.live_lyrics.timing
        path=self.app.live_lyrics.timing_store.path(self.app.song);before=path.read_bytes()
        self.app.receive_auto_audio(old,{'kind':'result','result':{'times':[10,20,30],'duration':90}})
        self.assertTrue(old.cancelled.is_set())
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(self.app.timing['times'],[15,None,None])
        self.assertFalse(self.app.auto_analyzer.draft_path(self.app.song,True).exists())

    def test_changed_duration_rejects_old_realtime_event_and_return_does_not_revive_it(self):
        old=self.start_live()
        event={'kind':'realtime','position':0,'observed_at':time.monotonic(),
               'match':{'index':1,'confidence':1,'onset':0}}
        self.app.receive_auto_audio(old,event)
        self.assertIs(self.app.realtime_job,old)
        self.app.set_playback(self.playback(180))
        self.assertTrue(old.cancelled.is_set())
        self.assertFalse(self.app.following_audio())
        self.app.clear_realtime()
        self.app.receive_auto_audio(old,{**event,'observed_at':time.monotonic()})
        self.assertIsNone(self.app.realtime_job)
        self.app.set_playback(self.playback(90))
        self.app.receive_auto_audio(old,{**event,'observed_at':time.monotonic()})
        self.assertIsNone(self.app.realtime_job)


if __name__=='__main__':unittest.main()
