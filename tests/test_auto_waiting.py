"""Skipping an automatic capture must not leave the old song waiting for minutes."""
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest

from audio_job import AudioJob
from auto_audio import AutoAnalyzer
from lyrics import Line,Song
from media import Playback
from storage import Store
from tests.test_auto_audio import FakeJob


class AutoWaitingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.auto=AutoAnalyzer('.',Store(self.temp.name),queue.Queue(),job_factory=FakeJob)
        self.auto.enabled=True;self.addCleanup(self.auto.close)
        self.songs=[Song(str(i),f'Song {i}','Artist','Album',(Line('Example','',''),)) for i in range(6)]

    def observe(self,index):
        song=self.songs[index]
        self.auto.observe(song,Playback(song.title,song.artist,song.album,duration=100,
                                       playing=True,captured=time.monotonic()))

    def test_skipped_waiting_is_cancelled_hidden_and_removed_when_cleanup_finishes(self):
        self.observe(0);old=next(iter(self.auto.jobs))
        self.observe(1)
        self.assertTrue(old.cancelled.is_set());self.assertTrue(old.obsolete_waiting)
        self.assertIn(old,self.auto.jobs)  # The owned worker still counts until exit.
        self.assertEqual([record['title'] for record in self.auto.activity_snapshot()['active']],['Song 1'])
        self.assertNotIn(old.analysis_key,self.auto.seen)
        self.assertNotIn(old.analysis_key,self.auto.attempts)
        self.auto.receive(old,{'kind':'finished'},self.songs)
        self.assertNotIn(old,self.auto.jobs);self.assertNotIn(old,self.auto.progress)
        self.assertIsNone(self.auto.last_progress);self.assertEqual(self.auto.activity_snapshot()['recent'],[])

    def test_fast_song_changes_do_not_accumulate_waiting_rows_or_old_slots_after_exit(self):
        self.observe(0)
        for index in range(1,len(self.songs)):
            old=next(iter(self.auto.jobs));self.observe(index)
            self.assertTrue(old.cancelled.is_set())
            self.auto.receive(old,{'kind':'finished'},self.songs)
            self.assertEqual(len(self.auto.jobs),1)
            self.assertEqual([record['title'] for record in self.auto.activity_snapshot()['active']],
                             [self.songs[index].title])
        self.assertFalse(self.auto.recent_progress)

    def test_actual_worker_cap_is_preserved_while_cancel_is_still_running(self):
        entered=threading.Event();release=threading.Event()
        self.observe(0);old=next(iter(self.auto.jobs))
        def slow_cancel():entered.set();release.wait(timeout=3)
        old.cancel=slow_cancel
        try:
            self.observe(1)
            self.assertTrue(entered.wait(timeout=1));self.assertFalse(release.is_set())
            self.observe(2);self.observe(3)
            self.assertEqual(len(self.auto.jobs),3)
            self.assertFalse(self.auto.activity_snapshot()['active'])
            self.assertIn('가득',self.auto.status)
        finally:release.set()

    def test_queued_analyzing_and_recording_work_survive_another_track(self):
        for phase in ('queued','analyzing','recording'):
            with self.subTest(phase=phase):
                self.auto.close();self.auto.enabled=True;self.auto.seen.clear();self.auto.last_playback=None
                self.observe(0);old=next(iter(self.auto.jobs))
                self.auto.receive(old,{'kind':'progress','state':phase,'text':phase},self.songs)
                self.observe(1)
                self.assertFalse(old.cancelled.is_set());self.assertIn(old,self.auto.progress)

    def test_delayed_ui_waiting_event_cannot_cancel_audio_that_already_started_or_completed(self):
        for field in ('capture_started','capture_complete'):
            with self.subTest(field=field):
                self.auto.close();self.auto.enabled=True;self.auto.seen.clear();self.auto.last_playback=None
                self.observe(0);old=next(iter(self.auto.jobs));setattr(old,field,True)
                self.assertEqual(self.auto.jobs[old],'waiting')
                self.observe(1)
                self.assertFalse(old.cancelled.is_set())

    def test_current_tracks_waiting_job_is_not_cancelled_by_polling(self):
        self.observe(0);job=next(iter(self.auto.jobs))
        for _ in range(4):self.observe(0)
        self.assertEqual(list(self.auto.jobs),[job]);self.assertFalse(job.cancelled.is_set())

    def test_real_waiting_child_is_terminated_promptly_without_cancelling_manual_job(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder)
            (base/'audio_worker.py').write_text(
                "import json,sys,time\nfrom pathlib import Path\n"
                "Path(sys.argv[1]).with_name('capture.wav').write_bytes(b'temporary audio')\n"
                "print(json.dumps({'kind':'progress','state':'waiting','text':'waiting'}),flush=True)\n"
                "time.sleep(180)\n",encoding='utf-8')
            messages=queue.Queue();auto=AutoAnalyzer(base,Store(base/'data'),messages);auto.enabled=True
            song=self.songs[0]
            playback=Playback(song.title,song.artist,song.album,duration=100,playing=True,captured=time.monotonic())
            manual=AudioJob(base,song,playback,queue.Queue())
            try:
                auto.observe(song,playback);job=next(iter(auto.jobs));temporary=Path(job.temp.name)
                _,(source,event)=messages.get(timeout=5);auto.receive(source,event,[song])
                self.assertEqual(event['state'],'waiting')
                before=time.monotonic()
                other=self.songs[1]
                auto.observe(other,Playback(other.title,other.artist,other.album,duration=100,
                                           playing=True,captured=time.monotonic()),external_sync=True)
                job.thread.join(timeout=3)
                self.assertFalse(job.thread.is_alive());self.assertLess(time.monotonic()-before,3)
                self.assertFalse(temporary.exists());self.assertIsNotNone(job.process.poll())
                while not messages.empty():
                    _,(source,event)=messages.get_nowait();auto.receive(source,event,[song])
                self.assertFalse(auto.jobs);self.assertFalse(auto.activity_snapshot()['active'])
                self.assertFalse(manual.cancelled.is_set());self.assertIsNone(manual.process.poll())
            finally:auto.close();manual.close()


if __name__=='__main__':unittest.main()
