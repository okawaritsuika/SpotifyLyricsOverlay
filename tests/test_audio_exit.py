import threading
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from audio_alignment import INCREMENTAL_METHOD
from media import Playback
from tests import test_audio as fixture


class FinishableJob:
    def __init__(self,base,song,playback,messages,**options):
        self.song=song;self.cancelled=threading.Event()
        self.incremental=options.get('incremental',False)
        self.memory_only=options.get('memory_only',False)
        self.waiting_for_lyrics=options.get('defer_lyrics',False)
        self.skip_final=False;self.capture_complete=False;self.capture_started=True
        self.finish_calls=0
    def finish_capture(self):
        self.finish_calls+=1
        return True
    def cancel(self):self.cancelled.set()
    def close(self):self.cancel()


class AudioExitTests(unittest.TestCase):
    def setUp(self):
        fixture.AudioUITests.setUp(self)
        self.auto=self.app.auto_analyzer
        self.auto.enabled=True;self.auto.job_factory=FinishableJob

    def tearDown(self):
        if not self.app.closed:self.app._finish_close()
        self.temp.cleanup()

    def job(self,song=None):
        song=song or self.song
        playback=Playback(song.title,song.artist,song.album,0,90,True,time.monotonic())
        self.auto.observe(song,playback)
        return next(job for job in self.auto.jobs if job.song.id==song.id)

    def finish(self,job):
        result={'times':[5,None,None],'method':INCREMENTAL_METHOD,'incremental':True,
                'partial':True,'analyzed_ranges':[[0,20]],'captured_seconds':20,'duration':90,'matched':1}
        self.app.messages.put(('auto_audio',(job,{'kind':'result','result':result})))
        self.app.messages.put(('auto_audio',(job,{'kind':'finished'})))

    def pump(self,seconds=.5):
        end=time.monotonic()+seconds
        while not self.app.closed and time.monotonic()<end:
            self.root.update();time.sleep(.01)

    def test_close_hides_windows_then_saves_delayed_result_before_destroying_app(self):
        self.store.save_timing(self.song,{'times':[None,35,None],'origins':[None,'manual',None],
                                         'offset':0,'source':'직접 기록'})
        job=self.job()
        extra=tk.Toplevel(self.root);nested=tk.Toplevel(extra)
        self.root.deiconify();self.root.update()
        rect=[self.root.winfo_width(),self.root.winfo_height(),self.root.winfo_x(),self.root.winfo_y()]
        reopened=[]
        self.root.after(10,lambda:reopened.append(True))
        original_playback=self.app.playback
        self.app.messages.put(('media',Playback('Different',duration=100)))
        with patch.object(self.app,'set_playback') as playback,patch.object(self.app,'receive_reader_lyrics') as lyrics:
            self.app.close();self.app.close()
            self.assertTrue(self.app.closing);self.assertFalse(self.app.closed)
            self.assertEqual(job.finish_calls,1)
            self.assertFalse(job.cancelled.is_set())
            self.assertEqual(self.root.state(),'withdrawn')
            self.assertEqual(extra.state(),'withdrawn');self.assertEqual(nested.state(),'withdrawn')
            self.assertEqual(self.store.settings()['window_rect'],rect)
            self.app.messages.put(('reader_lyrics',('changed lyrics',)))
            self.app.tick()
            self.auto.observe(self.other,Playback(self.other.title,self.other.artist,self.other.album,
                              duration=90,playing=True,captured=time.monotonic()))
            self.assertEqual(list(self.auto.jobs),[job])
            timer=threading.Timer(.03,self.finish,args=(job,));timer.start()
            self.pump();timer.join()
            playback.assert_not_called();lyrics.assert_not_called()
        self.assertTrue(self.app.closed)
        self.assertEqual(self.app.playback,original_playback)
        self.assertEqual(reopened,[])
        saved=self.store.timing(self.song)
        self.assertEqual(saved['times'],[5,35,None])
        self.assertEqual(saved['origins'],['audio','manual',None])
        self.assertEqual(saved['analyzed_ranges'],[[0,20]])
        self.assertFalse(saved['audio_finalized'])

    def test_already_queued_and_recording_jobs_both_finish_before_close(self):
        first=self.job();first.capture_complete=True;self.auto.jobs[first]='queued'
        second=self.job(self.other)
        self.app.close()
        self.assertFalse(first.cancelled.is_set());self.assertFalse(second.cancelled.is_set())
        self.finish(first);self.pump(.15)
        self.assertFalse(self.app.closed)
        self.assertEqual(self.store.timing(self.song)['times'],[5,None,None])
        self.finish(second);self.pump()
        self.assertTrue(self.app.closed)
        self.assertEqual(self.store.timing(self.other)['times'],[5,None,None])

    def test_manual_worker_is_closed_immediately_while_automatic_result_drains(self):
        job=self.job();manual=Mock();self.app.audio_job=manual
        self.app.close()
        manual.close.assert_called_once_with()
        self.assertIsNone(self.app.audio_job)
        self.assertFalse(self.app.closed)
        self.finish(job);self.pump()
        self.assertTrue(self.app.closed)
        manual.close.assert_called_once_with()

    def test_short_listen_discard_finishes_shutdown_without_saved_timing(self):
        job=self.job();self.app.close()
        self.app.messages.put(('auto_audio',(job,{'kind':'progress','state':'discarded','text':'짧은 재생'})))
        self.app.messages.put(('auto_audio',(job,{'kind':'finished'})))
        self.pump()
        self.assertTrue(self.app.closed)
        self.assertEqual(self.store.timing(self.song)['times'],[None]*3)

    def test_no_eligible_job_closes_immediately(self):
        self.app.close()
        self.assertTrue(self.app.closed)
        self.app.close()

    def test_unbound_capture_closes_without_waiting_for_lyrics(self):
        job=self.job();job.waiting_for_lyrics=True
        self.app.close()
        self.assertTrue(self.app.closed)
        self.assertTrue(job.cancelled.is_set())
        self.assertEqual(job.finish_calls,0)


if __name__=='__main__':unittest.main()
