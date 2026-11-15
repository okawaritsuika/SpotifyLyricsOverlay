"""Automatic cancellation must explain why, without claiming a saved result."""
from dataclasses import replace
import queue
import tempfile
import unittest
from unittest.mock import patch

from audio_progress import AudioProgress, CANCELLATION_DETAILS
from auto_audio import AutoAnalyzer
from lyrics import Line, Song
from media import Playback
from storage import Store, atomic_json
from tests.test_auto_audio import FakeJob


class CancellationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.auto=AutoAnalyzer('.',Store(self.temp.name),queue.Queue(),job_factory=FakeJob)
        self.addCleanup(self.auto.close);self.auto.enabled=True
        self.now=100.
        clock=patch('auto_audio.time.monotonic',side_effect=lambda:self.now)
        clock.start();self.addCleanup(clock.stop)
        self.song=Song('example','Example','Artist','Album',tuple(Line(str(i),'','') for i in range(3)))
        self.playback=Playback('Example','Artist','Album',0,100,True,self.now)

    def pending(self,position=0):
        self.playback=replace(self.playback,base=position,captured=self.now)
        self.auto.observe(None,self.playback)
        return next(iter(self.auto.jobs))

    def finished_reason(self,job,reason):
        self.assertTrue(job.cancelled.is_set())
        self.assertEqual(self.auto.progress[job].snapshot()['cancellation_reason'],reason)
        self.auto.receive(job,{'kind':'finished'},[self.song])
        record=self.auto.activity_snapshot()['recent'][0]
        self.assertEqual(record['state'],'cancelled')
        self.assertEqual(record['cancellation_reason'],reason)
        self.assertEqual(record['detail'],CANCELLATION_DETAILS[reason])
        return record

    def test_confirmed_sync_explains_redundant_analysis_and_rejects_late_result(self):
        self.auto.observe(self.song,self.playback);job=next(iter(self.auto.jobs))
        self.auto.store.save_timing(self.song,{'times':[10,20,30],'source':'직접 기록'})
        path=self.auto.store.directory/'timings'/'example.json';before=path.read_bytes()
        self.auto.observe(self.song,self.playback,external_sync=True)
        self.auto.receive(job,{'kind':'result','result':{'times':[11,21,31]}},[self.song])
        self.assertEqual(path.read_bytes(),before)
        self.assertFalse(self.auto.draft_path(self.song).exists())
        self.finished_reason(job,'external_sync')

    def test_disable_does_not_replace_the_original_cancellation_reason(self):
        job=self.pending()
        self.auto.observe(self.song,self.playback,external_sync=True)
        self.auto.set_enabled(False)
        self.finished_reason(job,'external_sync')

    def test_disable_has_its_own_reason(self):
        job=self.pending();self.auto.set_enabled(False)
        self.finished_reason(job,'disabled')

    def test_pending_track_pause_seek_and_duration_changes_are_distinguished(self):
        cases=(('track_changed',{'title':'Other','base':0}),
               ('playback_paused',{'playing':False,'base':1}),
               ('playback_seeked',{'base':40}),
               ('duration_changed',{'duration':150,'base':1}))
        for reason,changes in cases:
            with self.subTest(reason=reason):
                job=self.pending();job.capture_started=True
                self.now+=1
                self.auto.observe(None,replace(self.playback,captured=self.now,**changes))
                self.finished_reason(job,reason)
                for remaining in tuple(self.auto.jobs):
                    self.auto._cancel_pending_capture(remaining,'disabled')
                    self.auto.receive(remaining,{'kind':'finished'},[])
                self.auto.last_playback=None

    def test_obsolete_request_explains_cleanup_without_polluting_recent_activity(self):
        job=self.pending()
        self.auto.observe(None,replace(self.playback,title='Other'))
        self.assertEqual(self.auto.progress[job].cancellation_reason,'obsolete_waiting')
        self.auto.receive(job,{'kind':'finished'},[])
        self.assertEqual(self.auto.activity_snapshot()['recent'],[])

    def test_saved_candidate_does_not_claim_to_have_saved_new_timings(self):
        job=self.pending()
        atomic_json(self.auto.draft_path(self.song),{'fingerprint':self.song.fingerprint,'times':[10,None,None]})
        self.auto.observe(self.song,self.playback)
        self.finished_reason(job,'saved_candidate')
        self.assertEqual(self.auto.store.timing(self.song)['times'],[None]*3)

    def test_saved_manual_sync_explains_why_prefetch_is_unnecessary(self):
        job=self.pending()
        self.auto.store.save_timing(self.song,{'times':[10,20,30],'source':'직접 기록'})
        self.auto.observe(self.song,self.playback)
        self.finished_reason(job,'saved_sync')

    def test_exhausted_remaining_interval_is_distinguished_from_completed_sync(self):
        job=self.pending(position=90)
        self.auto.store.save_timing(self.song,{'times':[5,None,None],'source':'음원 분석',
            'analyzed_ranges':[[0,30]],'analysis_duration':100})
        self.now+=1;self.playback=replace(self.playback,base=91,captured=self.now)
        self.auto.observe(self.song,self.playback)
        self.finished_reason(job,'saved_intervals')

    def test_retained_partial_capture_has_its_own_reason(self):
        job=self.pending(position=30)
        atomic_json(self.auto.draft_path(self.song),{'fingerprint':self.song.fingerprint,
            'times':[10,None,None],'partial':True,'captured_seconds':20})
        self.auto.observe(self.song,self.playback)
        self.finished_reason(job,'saved_partial')

    def test_shutdown_explains_unbound_capture_without_changing_finish_policy(self):
        job=self.pending();self.auto.begin_shutdown()
        self.finished_reason(job,'shutdown')


class CancellationProgressTests(unittest.TestCase):
    def test_reason_survives_late_worker_text_and_finished_event(self):
        progress=AudioProgress('Example')
        progress.update({'kind':'progress','cancellation_reason':'playback_paused'},cancelled=True)
        self.assertIn('재생이 정지',progress.snapshot()['detail'])
        progress.update({'kind':'progress','text':'late worker message'},cancelled=True)
        progress.update({'kind':'finished','text':'another stale message'},cancelled=True)
        self.assertEqual(progress.snapshot()['detail'],CANCELLATION_DETAILS['playback_paused'])
        self.assertEqual(progress.snapshot()['cancellation_reason'],'playback_paused')

    def test_manual_cancellation_without_reason_remains_compatible(self):
        progress=AudioProgress('Manual',mode='직접')
        progress.update({'kind':'progress'},cancelled=True)
        self.assertEqual(progress.text,'임시 음원을 정리하고 있습니다.')
        progress.update({'kind':'finished'},cancelled=True)
        self.assertEqual(progress.text,'분석 취소됨 · 기존 시간표는 그대로입니다.')
        self.assertEqual(progress.snapshot()['cancellation_reason'],'')


if __name__=='__main__':unittest.main()
