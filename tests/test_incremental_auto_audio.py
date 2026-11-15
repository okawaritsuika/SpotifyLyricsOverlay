"""Interrupted recordings accumulate clocks and resume only uncovered audio."""
from dataclasses import replace
import queue
import tempfile
import time
import unittest

from audio_alignment import INCREMENTAL_METHOD
from auto_audio import AutoAnalyzer
from lyrics import Line,Song
from media import Playback
from storage import Store
from test_auto_audio import FakeJob


class IncrementalAutoTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(self.temp.name)
        self.song=Song('incremental','Example','Singer','Album',tuple(Line('固有の文章'+str(i),'','') for i in range(10)))
        self.auto=self.make_auto()

    def make_auto(self):
        value=AutoAnalyzer('.',self.store,queue.Queue(),job_factory=FakeJob)
        value.enabled=True;self.addCleanup(value.close);return value

    def observe(self,position,playing=True,**options):
        playback=Playback(self.song.title,self.song.artist,self.song.album,position,120,playing,time.monotonic())
        self.auto.observe(self.song,playback,memory_only=True,**options)

    def deliver(self,job,values,start,end):
        result={'times':values,'method':INCREMENTAL_METHOD,'incremental':True,
                'analyzed_ranges':[[start,end]],'captured_seconds':end-start,'partial':True}
        self.auto.receive(job,{'kind':'result','result':result},[self.song])
        self.auto.receive(job,{'kind':'finished'},[self.song])

    def test_provider_anchors_capture_only_the_missing_window_and_keep_manual_timing(self):
        times=[8,None,16,24,32,40,48,56,64,72]
        merged={'times':times,'row_kinds':['singing','dialogue']+['singing']*8}
        self.auto.live_timing.save(self.song,{'times':[8]+[None]*9,'offset':0,'origins':['manual']+[None]*9})
        self.observe(0,external_sync=True,merged_timing=merged)
        self.assertFalse(self.auto.jobs)
        self.observe(6,external_sync=True,merged_timing=merged)
        job=next(iter(self.auto.jobs))
        self.assertEqual(job.options['capture_window'],(8,16))
        self.assertEqual(job.options['confirmed_times'],times)
        self.deliver(job,[None,12]+[None]*8,8,16)
        saved=self.auto.live_timing.load(self.song)
        self.assertEqual(saved['times'],[8,12]+[None]*8)
        self.assertEqual(saved['origins'][:2],['manual','audio'])

    def test_unmatched_or_new_provider_clock_leaves_existing_timing(self):
        merged={'times':[8,None,16]+[None]*7,'row_kinds':['singing','dialogue']+['unknown']*8}
        self.auto.live_timing.save(self.song,{'times':[8]+[None]*9,'offset':0,'origins':['manual']+[None]*9})
        self.observe(6,external_sync=True,merged_timing=merged)
        job=next(iter(self.auto.jobs))
        self.auto.receive(job,{'kind':'progress','state':'unmatched','text':'no match'},[self.song])
        self.auto.receive(job,{'kind':'finished'},[self.song])
        self.assertEqual(self.auto.live_timing.load(self.song)['times'],[8]+[None]*9)
        self.observe(6,playing=False,external_sync=True,merged_timing=merged)
        self.observe(6,external_sync=True,merged_timing=merged)
        retry=next(iter(self.auto.jobs))
        filled={**merged,'times':[8,13,16]+[None]*7}
        self.observe(7,external_sync=True,merged_timing=filled)
        self.assertTrue(retry.cancelled.is_set())
        self.deliver(retry,[None,12]+[None]*8,8,16)
        self.assertEqual(self.auto.live_timing.load(self.song)['times'],[8]+[None]*9)

    def test_merged_partial_never_starts_capture_when_disabled(self):
        self.auto.enabled=False
        merged={'times':[8,None,16]+[None]*7,'row_kinds':['unknown']*10}
        self.observe(6,external_sync=True,merged_timing=merged)
        self.assertFalse(self.auto.jobs)

    def test_late_bounded_result_cannot_change_shared_local_lyrics_for_another_recording(self):
        self.store.save_timing(self.song,{'times':[8]+[None]*9,'offset':0,'origins':['manual']+[None]*9})
        merged={'times':[8,None,16]+[None]*7,'row_kinds':['unknown']*10}
        first=Playback(self.song.title,self.song.artist,self.song.album,6,120,True,time.monotonic())
        self.auto.observe(self.song,first,external_sync=True,merged_timing=merged)
        job=next(iter(self.auto.jobs));job.capture_started=True
        second=replace(first,album='Other recording')
        self.auto.observe(self.song,second,external_sync=True,merged_timing=merged)
        self.assertFalse(job.cancelled.is_set())
        self.deliver(job,[None,12]+[None]*8,8,16)
        self.assertEqual(self.store.timing(self.song)['times'],[8]+[None]*9)


    def test_small_verified_prefix_saves_below_old_whole_song_threshold(self):
        self.observe(0);job=next(iter(self.auto.jobs))
        self.deliver(job,[10,22]+[None]*8,0,35)
        saved=self.auto.live_timing.load(self.song)
        self.assertEqual(saved['times'],[10,22]+[None]*8)
        self.assertEqual(saved['analyzed_ranges'],[[0,35]])
        self.assertFalse(saved.get('audio_finalized'))
        self.assertIn('이어 분석',self.auto.status)

    def test_restart_and_two_continuations_preserve_prior_rows_and_manual_time(self):
        self.auto.live_timing.save(self.song,{'times':[8]+[None]*9,'offset':0,'origins':['manual']+[None]*9})
        self.observe(0);self.deliver(next(iter(self.auto.jobs)),[10,22]+[None]*8,0,35)
        self.auto=self.make_auto()
        self.observe(0,awaiting_sync=True);self.assertFalse(self.auto.jobs)
        self.assertIn('00:35',self.auto.status)
        self.observe(34);job=next(iter(self.auto.jobs))
        self.assertEqual(job.options['capture_from'],33)
        self.assertEqual(job.options['analyzed_ranges'],[[0,35]])
        self.assertEqual(job.options['confirmed_times'][:2],[8,22])
        self.deliver(job,[11,23,45,57]+[None]*6,35,70)
        saved=self.auto.live_timing.load(self.song)
        self.assertEqual(saved['times'][:4],[8,22,45,57])
        self.assertEqual(saved['analyzed_ranges'],[[0,70]])
        self.auto=self.make_auto();self.observe(0);self.assertFalse(self.auto.jobs)
        self.observe(69);job=next(iter(self.auto.jobs))
        self.deliver(job,[None]*4+[75,82,88,94,100,106],70,120)
        saved=self.auto.live_timing.load(self.song)
        self.assertEqual(saved['times'],[8,22,45,57,75,82,88,94,100,106])
        self.assertTrue(saved['audio_finalized'])
        self.assertEqual(saved['analyzed_ranges'],[[0,120]])
        self.auto=self.make_auto();self.observe(0);self.assertFalse(self.auto.jobs)

    def test_short_discard_is_quiet_until_a_new_playback_start(self):
        self.observe(0);job=next(iter(self.auto.jobs))
        self.auto.receive(job,{'kind':'progress','state':'discarded','text':'짧은 재생 제외'},[self.song])
        self.auto.receive(job,{'kind':'finished'},[self.song])
        self.observe(1);self.assertFalse(self.auto.jobs)
        self.assertFalse(self.auto.live_timing.path(self.song).exists())
        self.observe(2,False);self.observe(2)
        self.assertEqual(len(self.auto.jobs),1)

    def test_translation_refresh_retains_coverage_but_different_rows_do_not(self):
        self.observe(0);self.deliver(next(iter(self.auto.jobs)),[10,22]+[None]*8,0,35)
        translated=replace(self.song,lines=tuple(replace(line,korean='번역') for line in self.song.lines))
        self.assertEqual(self.auto.live_timing.load(translated)['analyzed_ranges'],[[0,35]])
        changed=replace(self.song,lines=tuple(replace(line,original='다른 내용'+str(i)) for i,line in enumerate(self.song.lines)))
        self.assertIsNone(self.auto.live_timing.load(changed))

    def test_slightly_shorter_media_duration_keeps_completed_saved_coverage(self):
        self.auto.live_timing.save(self.song,{'times':[10,22]+[None]*8,'offset':0,
            'origins':['audio']*2+[None]*8,'analyzed_ranges':[[0,120]],'analysis_duration':120})
        playback=Playback(self.song.title,self.song.artist,self.song.album,0,119.9,True,time.monotonic())
        self.auto.observe(self.song,playback,memory_only=True)
        self.assertFalse(self.auto.jobs)
        self.assertEqual(self.auto.retained_ranges(self.auto.live_timing.load(self.song),119.9),[[0,119.9]])
        self.assertEqual(self.auto.live_timing.load(self.song)['analyzed_ranges'],[[0,120]])

    def test_result_merge_clips_old_tail_to_accepted_current_duration(self):
        self.observe(34);job=next(iter(self.auto.jobs));job.recording_duration=119.9
        self.auto.live_timing.save(self.song,{'times':[10,22]+[None]*8,'offset':0,
            'origins':['audio']*2+[None]*8,'analyzed_ranges':[[0,35],[100,120]],'analysis_duration':120})
        self.deliver(job,[None,None,45,57]+[None]*6,35,100)
        saved=self.auto.live_timing.load(self.song)
        self.assertEqual(saved['analyzed_ranges'],[[0,119.9]])
        self.assertEqual(saved['analysis_duration'],119.9)
        self.assertEqual(saved['times'][:4],[10,22,45,57])
        self.assertTrue(saved['audio_finalized'])

    def test_range_jitter_never_rebases_or_accepts_corrupt_original_bounds(self):
        original={'analyzed_ranges':[[1,120]],'analysis_duration':120}
        self.assertEqual(self.auto.retained_ranges(original,121),[[1,120]])
        self.assertEqual(self.auto.retained_ranges(original,118),[[1,118]])
        self.assertEqual(self.auto.retained_ranges(original,117.9),[])
        with self.assertRaises(ValueError):
            self.auto.retained_ranges({'analyzed_ranges':[[0,120.1]],'analysis_duration':120},121)

    def test_shutdown_finishes_bound_clip_without_starting_more_work(self):
        self.observe(0);job=next(iter(self.auto.jobs));requested=[]
        job.finish_capture=lambda:requested.append(True)
        self.assertTrue(self.auto.begin_shutdown());self.assertEqual(requested,[True])
        self.assertFalse(job.cancelled.is_set())
        self.observe(80);self.assertEqual(list(self.auto.jobs),[job])
        self.deliver(job,[10,22]+[None]*8,0,35)
        self.assertEqual(self.auto.live_timing.load(self.song)['analyzed_ranges'],[[0,35]])


if __name__=='__main__':unittest.main()
