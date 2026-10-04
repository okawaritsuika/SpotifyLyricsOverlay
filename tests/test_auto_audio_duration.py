"""Do not apply an older recording's in-flight analysis to a different length."""
from dataclasses import replace
import queue
import tempfile
import threading
import time
import unittest

from auto_audio import AutoAnalyzer
from lyrics import Line,Song
from media import Playback
from storage import Store
from test_auto_audio import FakeJob


class AutoAudioDurationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(self.temp.name)
        self.song=Song('duration','Same title','Same artist','Same album',
                       tuple(Line(text,'','') for text in ('One','Two','Three')))
        self.auto=AutoAnalyzer('.',self.store,queue.Queue(),job_factory=FakeJob)
        self.auto.enabled=True

    def tearDown(self):
        self.auto.close();self.temp.cleanup()

    def observe(self,duration,song=None,**options):
        song=song or self.song
        snapshot=Playback(song.title,song.artist,song.album,0,duration,True,time.monotonic())
        self.auto.observe(song,snapshot,**options)
        return snapshot

    def result(self,job):
        return self.auto.receive(job,{'kind':'result','result':{'times':[10,20,30]}},[self.song])

    def test_duration_conflict_rejects_old_result_and_preserves_manual_file_bytes(self):
        for memory in (False,True):
            with self.subTest(memory=memory):
                self.observe(90,memory_only=memory);old=list(self.auto.jobs)[-1]
                self.observe(180,memory_only=memory,awaiting_sync=True)
                timing={'times':[15,None,None],'offset':.5,'origins':['manual',None,None]}
                if memory:
                    self.auto.live_timing.save(self.song,timing)
                    path=self.auto.live_timing.path(self.song)
                else:
                    self.store.save_timing(self.song,timing)
                    path=self.store.directory/'timings'/(self.song.id+'.json')
                before=path.read_bytes()
                self.assertTrue(old.cancelled.is_set())
                self.assertIsNone(self.result(old))
                self.assertEqual(path.read_bytes(),before)
                self.assertFalse(self.auto.draft_path(self.song,memory).exists())
                self.assertNotIn(old.analysis_key,self.auto.seen)
                self.assertNotIn(old.analysis_key,self.auto.retryable)
                self.assertNotIn(old.analysis_key,self.auto.attempts)
                self.auto.receive(old,{'kind':'finished'},[self.song])

    def test_new_duration_starts_new_generation_before_cancelled_worker_finishes(self):
        self.observe(90);old=next(iter(self.auto.jobs))
        self.auto.retryable.add(old.analysis_key);self.auto.attempts[old.analysis_key]=2
        self.observe(180);current=list(self.auto.jobs)[-1]
        self.assertIsNot(current,old)
        self.assertTrue(old.cancelled.is_set());self.assertFalse(current.cancelled.is_set())
        self.assertEqual(current.recording_duration,180)
        self.assertGreater(current.transport_generation,old.transport_generation)
        self.assertEqual(self.auto.attempts[current.analysis_key],1)
        self.assertNotIn(current.analysis_key,self.auto.retryable)
        self.auto.receive(old,{'kind':'error','text':'late failure'},[self.song])
        self.auto.receive(old,{'kind':'finished'},[self.song])
        self.assertEqual(self.auto.attempts[current.analysis_key],1)
        self.assertIn(current.analysis_key,self.auto.seen)
        self.assertIs(self.result(current),self.song)

    def test_return_to_original_duration_cannot_revive_old_job(self):
        self.observe(90);first=next(iter(self.auto.jobs))
        self.observe(180);second=list(self.auto.jobs)[-1]
        self.observe(90);third=list(self.auto.jobs)[-1]
        self.assertEqual(len({first,second,third}),3)
        self.assertTrue(first.cancelled.is_set());self.assertTrue(second.cancelled.is_set())
        self.assertFalse(third.cancelled.is_set())
        self.assertIsNone(self.result(first));self.assertIsNone(self.result(second))
        self.assertFalse((self.store.directory/'timings').exists())
        self.assertIs(self.result(third),self.song)

    def test_unknown_duration_and_two_second_jitter_keep_active_job(self):
        self.observe(90);job=next(iter(self.auto.jobs))
        for duration in (0,88,90,92,0,90):
            self.observe(duration)
            self.assertFalse(job.cancelled.is_set())
            self.assertEqual(list(self.auto.jobs),[job])
        self.assertIs(self.result(job),self.song)

    def test_unknown_snapshot_does_not_hide_later_conflict(self):
        self.observe(90);job=next(iter(self.auto.jobs))
        self.observe(0);self.observe(180,awaiting_sync=True)
        self.assertTrue(job.cancelled.is_set())
        self.assertIsNone(self.result(job))

    def test_progressive_small_updates_cannot_drift_away_from_job_duration(self):
        self.observe(90);job=next(iter(self.auto.jobs))
        for duration in (91,92):self.observe(duration,awaiting_sync=True)
        self.assertFalse(job.cancelled.is_set())
        self.observe(93,awaiting_sync=True)
        self.assertTrue(job.cancelled.is_set())

    def test_unknown_or_invalid_lengths_do_not_replace_known_context(self):
        self.observe(90);job=next(iter(self.auto.jobs))
        for duration in (0,-1,True,float('nan'),float('inf'),10**1000):
            self.observe(duration,awaiting_sync=True)
            self.assertFalse(job.cancelled.is_set())
        self.observe(92.001,awaiting_sync=True)
        self.assertTrue(job.cancelled.is_set())

    def test_cancelled_workers_keep_queue_bound_until_finished_and_can_retry_afterward(self):
        for duration in (90,180,90):self.observe(duration)
        self.assertEqual(len(self.auto.jobs),3)
        self.observe(180)
        self.assertTrue(all(job.cancelled.is_set() for job in self.auto.jobs))
        self.assertEqual(len(self.auto.jobs),3)
        for job in tuple(self.auto.jobs):self.auto.receive(job,{'kind':'finished'},[self.song])
        self.observe(180)
        self.assertEqual(len(self.auto.jobs),1)
        self.assertEqual(self.auto.attempts[next(iter(self.auto.jobs)).analysis_key],1)

    def test_duration_conflict_keeps_other_tracks_background_job_and_result(self):
        other=replace(self.song,id='other',title='Other recording')
        self.observe(90,other);background=next(iter(self.auto.jobs))
        self.auto.receive(background,{'kind':'progress','state':'queued','text':'전체 분석 순서 대기'},[other])
        self.observe(90);old=list(self.auto.jobs)[-1]
        self.observe(180,awaiting_sync=True)
        self.assertFalse(background.cancelled.is_set());self.assertTrue(old.cancelled.is_set())
        self.assertIn(background.analysis_key,self.auto.seen)
        self.assertEqual(self.auto.attempts[background.analysis_key],1)
        self.assertIs(self.auto.receive(background,{'kind':'result','result':{'times':[10,20,30]}},[other]),other)

    def test_return_to_same_metadata_with_new_length_cancels_background_job(self):
        self.observe(90);old=next(iter(self.auto.jobs))
        self.observe(90,replace(self.song,id='other',title='Other recording'))
        self.observe(180,awaiting_sync=True)
        self.assertTrue(old.cancelled.is_set());self.assertIsNone(self.result(old))

    def test_duration_change_resets_finished_failed_attempt_before_fallback(self):
        self.observe(90);old=next(iter(self.auto.jobs))
        self.auto.receive(old,{'kind':'error','text':'failure'},[self.song])
        self.auto.receive(old,{'kind':'finished'},[self.song])
        self.auto.attempts[old.analysis_key]=2
        self.observe(0);self.observe(180,awaiting_sync=True)
        self.assertNotIn(old.analysis_key,self.auto.seen)
        self.assertNotIn(old.analysis_key,self.auto.retryable)
        self.assertNotIn(old.analysis_key,self.auto.attempts)
        self.observe(180)
        self.assertEqual(len(self.auto.jobs),1)
        self.assertEqual(self.auto.attempts[old.analysis_key],1)

    def test_duration_cancel_is_immediate_while_worker_shutdown_is_slow(self):
        self.observe(90);old=next(iter(self.auto.jobs))
        entered=threading.Event();release=threading.Event();finished=threading.Event()
        original_cancel=old.cancel;calls=[]
        def slow_cancel():
            calls.append(1);entered.set();release.wait(timeout=2);finished.set()
        old.cancel=slow_cancel
        try:
            self.observe(180,awaiting_sync=True)
            self.assertTrue(old.cancelled.is_set())
            self.assertTrue(entered.wait(timeout=1));self.assertFalse(finished.is_set())
            self.observe(180,awaiting_sync=True)
            self.assertEqual(calls,[1]);self.assertIsNone(self.result(old))
        finally:
            release.set();finished.wait(timeout=1);old.cancel=original_cancel


if __name__=='__main__':unittest.main()
