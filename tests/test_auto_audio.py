import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from auto_audio import AutoAnalyzer
from audio_alignment import ALIGNMENT_METHOD
from lyrics import Song,Line
from media import Playback
from storage import Store,read_json


class FakeJob:
    def __init__(self,base,song,playback,messages,**options):
        self.song=song;self.cancelled=threading.Event()
        self.options=options;self.memory_only=options.get('memory_only',False)
        self.incremental=options.get('incremental',False)
        self.skip_final='start_index' in options and not self.incremental
        self.waiting_for_lyrics=options.get('defer_lyrics',False)
    def bind_lyrics(self,song,memory_only=False,start_index=None,analysis_context=None):
        self.song=song;self.memory_only=memory_only;self.waiting_for_lyrics=False
        self.bound_start_index=start_index
        if analysis_context:self.options.update(analysis_context)
        return True
    def cancel(self):self.cancelled.set()
    def close(self):self.cancel()


class AutoAudioTests(unittest.TestCase):
    def test_activity_preserves_previous_tracks_analysis_after_confirmed_sync_transition(self):
        self.observe(0);old=next(iter(self.auto.jobs))
        self.auto.receive(old,{'kind':'progress','state':'analyzing','text':'정렬','completed':25,'total':100},self.songs)
        song=self.songs[1]
        playback=Playback(song.title,song.artist,song.album,0,100,True,time.monotonic())
        self.auto.observe(song,playback,external_sync=True)
        status=self.auto.status
        self.auto.receive(old,{'kind':'progress','state':'analyzing','text':'정렬','completed':30,'total':100},self.songs)
        record=self.auto.activity_snapshot()['active'][0]
        self.assertEqual(record['track_key'],old.analysis_key[0])
        self.assertEqual(record['title'],self.songs[0].title)
        self.assertEqual(record['state'],'analyzing')
        self.assertEqual(record['percent'],30)
        self.assertEqual(self.auto.status,status)

    def test_recent_completion_remains_visible_while_new_track_runs(self):
        self.observe(0);old=next(iter(self.auto.jobs))
        self.auto.receive(old,{'kind':'result','result':{'times':[10,20,30]}},self.songs)
        self.auto.receive(old,{'kind':'finished'},self.songs)
        self.observe(1)
        snapshot=self.auto.activity_snapshot()
        self.assertEqual(len(snapshot['active']),1)
        self.assertEqual(snapshot['recent'][0]['title'],self.songs[0].title)
        self.assertEqual(snapshot['recent'][0]['state'],'completed')
        snapshot['recent'][0]['title']='changed by viewer'
        self.assertEqual(self.auto.activity_snapshot()['recent'][0]['title'],self.songs[0].title)

    def test_recent_activity_is_bounded_without_retaining_finished_jobs(self):
        for index in range(5):
            self.observe(index);job=next(iter(self.auto.jobs))
            self.auto.receive(job,{'kind':'error','text':'수집 중단'},self.songs)
            self.auto.receive(job,{'kind':'finished'},self.songs)
        records=self.auto.activity_snapshot()['recent']
        self.assertEqual([record['title'] for record in records],['Song 4','Song 3','Song 2'])
        self.assertFalse(self.auto.jobs)
        self.assertFalse(self.auto.progress)
        self.assertTrue(all(isinstance(value,(str,bool,int,float,type(None)))
                            for record in records for key,value in record.items() if key!='runtime'))
        self.assertTrue(all(record['runtime']=={} for record in records))

    def test_activity_explains_mid_song_capture_will_save_usable_partial_audio(self):
        self.memory_observe(self.songs[0],50)
        record=self.auto.activity_snapshot()['active'][0]
        self.assertTrue(record['final_analysis'])
        self.assertIn('10초 이상',record['detail'])

    def test_background_jobs_keep_separate_live_progress_and_release_finished_job(self):
        self.observe(0);first=next(iter(self.auto.jobs))
        self.auto.receive(first,{'kind':'progress','state':'analyzing','text':'alignment','completed':20,'total':100},self.songs)
        self.observe(1);second=list(self.auto.jobs)[1]
        self.auto.receive(second,{'kind':'progress','state':'queued','text':'queue'},self.songs)
        self.assertEqual(self.auto.progress[first].snapshot()['percent'],20)
        self.assertEqual(self.auto.progress[second].snapshot()['phase'],'분석 순서 대기')
        self.auto.receive(first,{'kind':'error','text':'failed'},self.songs)
        self.auto.receive(first,{'kind':'finished'},self.songs)
        self.assertNotIn(first,self.auto.progress)
        self.assertEqual(self.auto.last_progress.snapshot()['phase'],'오류')
        self.assertFalse(hasattr(self.auto.last_progress,'song'))
        self.assertEqual(list(self.auto.progress),[second])

    def test_disable_marks_each_job_as_cancelling_then_cancelled(self):
        self.observe(0);job=next(iter(self.auto.jobs))
        self.auto.set_enabled(False)
        self.assertEqual(self.auto.progress[job].snapshot()['phase'],'취소 중')
        self.auto.receive(job,{'kind':'finished'},self.songs)
        self.assertEqual(self.auto.last_progress.snapshot()['phase'],'취소됨')
        self.assertFalse(self.auto.progress)

    def test_pause_resume_restarts_failed_capture_mid_song_once_not_on_every_poll(self):
        song=self.songs[0];self.memory_observe(song,20);job=next(iter(self.auto.jobs))
        self.auto.observe(song,Playback(song.title,song.artist,song.album,22,100,False,time.monotonic()),memory_only=True)
        self.auto.receive(job,{'kind':'error','text':'paused'},[song])
        self.auto.receive(job,{'kind':'finished'},[song])
        self.memory_observe(song,22);replacement=next(iter(self.auto.jobs))
        self.assertIsNot(replacement,job);self.assertFalse(replacement.skip_final)
        for _ in range(4):self.memory_observe(song,22)
        self.assertEqual(len(self.auto.jobs),1)

    def test_seek_finishing_after_new_snapshot_still_allows_bounded_retry(self):
        song=self.songs[0];self.memory_observe(song,20);job=next(iter(self.auto.jobs))
        self.memory_observe(song,50)
        self.assertEqual(len(self.auto.jobs),1)
        self.auto.receive(job,{'kind':'error','text':'seek'},[song])
        self.auto.receive(job,{'kind':'finished'},[song])
        self.memory_observe(song,50);replacement=next(iter(self.auto.jobs))
        self.assertIsNot(replacement,job);self.assertFalse(replacement.skip_final)
        self.auto.receive(replacement,{'kind':'error','text':'failed'},[song])
        self.auto.receive(replacement,{'kind':'finished'},[song])
        for position in (51,52,60,0):self.memory_observe(song,position)
        self.assertFalse(self.auto.jobs)

    def test_background_progress_does_not_replace_current_tracks_status(self):
        self.observe(0);old=next(iter(self.auto.jobs))
        self.observe(1);status=self.auto.status
        self.auto.receive(old,{'kind':'progress','state':'analyzing','text':'old track'},self.songs)
        self.assertEqual(self.auto.status,status)
        self.auto.observe(None,Playback('No lyrics',base=20,duration=100,playing=True,captured=time.monotonic()))
        status=self.auto.status
        self.auto.receive(old,{'kind':'error','text':'old failure'},self.songs)
        self.assertEqual(self.auto.status,status)

    def test_completed_analysis_is_reused_even_with_uncertain_gaps(self):
        self.songs[0]=Song('0','Song 0','Artist','Album',(Line('例文です','','예시'),)*4)
        self.observe(0);job=next(iter(self.auto.jobs))
        self.auto.receive(job,{'kind':'result','result':{'times':[10,20,30,None],
            'method':ALIGNMENT_METHOD,'partial':False}},self.songs)
        self.assertTrue(self.store.timing(self.songs[0])['audio_finalized'])
        self.auto.receive(job,{'kind':'finished'},self.songs)
        self.auto.seen.clear();self.observe(0)
        self.assertFalse(self.auto.jobs)

    def test_partial_recording_is_never_marked_as_full_analysis(self):
        self.observe(0);job=next(iter(self.auto.jobs))
        self.auto.receive(job,{'kind':'result','result':{'times':[10,20,30],
            'method':ALIGNMENT_METHOD,'partial':True}},self.songs)
        self.assertFalse(self.store.timing(self.songs[0])['audio_finalized'])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(self.temp.name)
        self.songs=[Song(str(i),f'Song {i}','Artist','Album',(Line('例文です','','예시'),)*3) for i in range(5)]
        self.auto=AutoAnalyzer('.',self.store,queue.Queue(),job_factory=FakeJob)
        self.auto.enabled=True
    def tearDown(self):self.auto.close();self.temp.cleanup()
    def observe(self,index):
        song=self.songs[index]
        self.auto.observe(song,Playback(song.title,song.artist,song.album,0,100,True,time.monotonic()))

    def memory_observe(self,song,position=0):
        self.auto.observe(song,Playback(song.title,song.artist,song.album,position,100,True,time.monotonic()),memory_only=True)

    def test_completed_text_reanalyzes_missing_rows_and_keeps_seeded_manual_times(self):
        song=self.songs[0]
        self.auto.live_timing.save(song,{'times':[5,None,40],'offset':0,'source':'직접 기록',
            'origins':['manual',None,'manual'],'automatic_times':[8,None,35],'completion_pending':True})
        self.memory_observe(song);job=next(iter(self.auto.jobs))
        self.auto.receive(job,{'kind':'result','result':{'times':[10,20,30],
            'method':ALIGNMENT_METHOD,'partial':False}},[song])
        restored=self.auto.live_timing.load(song)
        self.assertEqual(restored['times'],[5,20,40]);self.assertNotIn('completion_pending',restored)
        self.auto.receive(job,{'kind':'finished'},[song]);self.auto.seen.clear()
        self.memory_observe(song);self.assertFalse(self.auto.jobs)

    def test_memory_lyrics_are_analyzed_and_only_numeric_data_survives(self):
        song=self.songs[0]
        self.auto.live_timing.save(song,{'times':[5,None,None],'offset':2})
        self.memory_observe(song);job=next(iter(self.auto.jobs))
        self.assertTrue(job.memory_only);self.assertFalse(job.skip_final)
        self.auto.receive(job,{'kind':'result','result':{'times':[10,20,30],
            'method':ALIGNMENT_METHOD,'text':'must never persist'}},[song])
        restored=self.auto.live_timing.load(song)
        self.assertEqual(restored['times'],[5,18,28])
        self.assertTrue(restored['audio_finalized'])
        self.assertEqual(restored['origins'],['manual','audio','audio'])
        for path in self.store.directory.rglob('*.json'):
            body=path.read_text(encoding='utf-8')
            for text in ('例文です','예시','must never persist',song.title):self.assertNotIn(text,body)
        self.assertFalse((self.store.directory/'timings').exists())
        self.auto.receive(job,{'kind':'finished'},[])
        self.auto.seen.clear();self.memory_observe(song)
        self.assertFalse(self.auto.jobs)

    def test_translation_refresh_keeps_one_job_and_accepts_originals_matching_result(self):
        song=self.songs[0];self.memory_observe(song)
        job=next(iter(self.auto.jobs))
        translated=Song(song.id,song.title,song.artist,song.album,tuple(Line(x.original,'new reading','new translation') for x in song.lines))
        self.memory_observe(translated)
        self.assertEqual(len(self.auto.jobs),1)
        self.assertIs(self.auto.receive(job,{'kind':'result','result':{'times':[10,20,30]}},[translated]),translated)

    def test_changed_originals_reject_old_result_even_after_leaving_the_track(self):
        song=self.songs[0];self.memory_observe(song)
        job=next(iter(self.auto.jobs))
        changed=Song(song.id,song.title,song.artist,song.album,(Line('違う歌詞です','',''),)*3)
        self.memory_observe(changed)
        self.assertIsNone(self.auto.receive(job,{'kind':'result','result':{'times':[10,20,30]}},[]))
        self.assertFalse(self.auto.live_timing.path(song).exists())

    def test_mid_song_memory_capture_tracks_provisionally_then_allows_full_next_play(self):
        song=self.songs[0];self.memory_observe(song,50)
        job=next(iter(self.auto.jobs))
        self.assertFalse(job.skip_final);self.assertGreater(job.options['start_index'],0)
        self.auto.receive(job,{'kind':'progress','state':'partial','text':'partial'},[song])
        self.auto.receive(job,{'kind':'finished'},[song])
        self.memory_observe(song,50);self.assertFalse(self.auto.jobs)
        self.memory_observe(song,0)
        full=next(iter(self.auto.jobs));self.assertFalse(full.skip_final)
        self.assertFalse(self.auto.live_timing.path(song).exists())

    def test_mid_song_failure_does_not_restart_on_every_poll(self):
        song=self.songs[0];self.memory_observe(song,50)
        job=next(iter(self.auto.jobs))
        self.auto.receive(job,{'kind':'error','text':'failed'},[song])
        self.auto.receive(job,{'kind':'finished'},[song])
        self.memory_observe(song,51);self.assertFalse(self.auto.jobs)
    def test_complete_manual_song_is_skipped(self):
        self.store.save_timing(self.songs[0],{'times':[1,2,3],'offset':0})
        self.observe(0)
        self.assertFalse(self.auto.jobs)

    def test_current_and_old_audio_methods_are_reused_without_automatic_reanalysis(self):
        self.store.save_timing(self.songs[0],{'times':[1,2,3],'offset':0,'source':'음원 분석','audio_method':ALIGNMENT_METHOD})
        self.observe(0);self.assertFalse(self.auto.jobs)
        self.store.save_timing(self.songs[1],{'times':[1,2,3],'offset':0,'source':'음원 분석','audio_method':'bounded-gap-alignment-v1'})
        self.observe(1);self.assertFalse(self.auto.jobs)
        self.assertEqual(self.store.timing(self.songs[1])['times'],[1,2,3])

    def test_legacy_analysis_with_gaps_and_manual_corrections_is_not_recaptured(self):
        for index,timing in enumerate([
                {'times':[10,None,30],'offset':0,'source':'음원 분석'},
                {'times':[10,None,30],'offset':0,'source':'직접 기록 / 음원 분석'},
                {'times':[10,None,30],'offset':0,'source':'직접 편집','origins':['manual',None,'audio']} ]):
            self.store.save_timing(self.songs[index],timing);self.observe(index)
        self.assertFalse(self.auto.jobs)

    def test_partial_saved_draft_waits_for_track_start_but_changed_lyrics_can_analyze(self):
        from storage import atomic_json
        song=self.songs[0]
        path=self.store.directory/'audio_drafts'/(song.id+'.json')
        atomic_json(path,{'times':[10,None,None],'fingerprint':song.fingerprint,'partial':True})
        self.auto.observe(song,Playback(song.title,song.artist,song.album,20,100,True,time.monotonic()))
        self.assertFalse(self.auto.jobs)
        self.observe(0);self.assertEqual(len(self.auto.jobs),1)
        job=next(iter(self.auto.jobs));self.auto.receive(job,{'kind':'finished'},[song])
        self.auto.seen.clear()
        atomic_json(path,{'times':[10,None,None],'fingerprint':'changed'})
        self.observe(0);self.assertEqual(len(self.auto.jobs),1)

    def seed_partial_memory(self):
        from storage import atomic_json
        song=Song('partial','Partial Song','Artist','Album',(Line('例文です','','예시'),)*4)
        self.auto.live_timing.save(song,{'times':[5,20,None,40],'offset':0,'source':'직접 기록 / 음원 분석',
            'origins':['manual','audio',None,'audio'],'automatic_times':[10,20,None,40]})
        atomic_json(self.auto.draft_path(song,True),{'times':[10,20,None,40],
            'fingerprint':self.auto.signature(song,True),'partial':True})
        return song

    def test_saved_partial_memory_waits_mid_song_then_completes_on_next_start(self):
        song=self.seed_partial_memory()
        for position in (20,21,22,50):self.memory_observe(song,position)
        self.assertFalse(self.auto.jobs)
        self.assertIn('3/4',self.auto.status);self.assertIn('처음',self.auto.status)
        self.memory_observe(song,0)
        self.assertEqual(len(self.auto.jobs),1)
        job=next(iter(self.auto.jobs))
        self.assertFalse(job.skip_final)
        self.assertEqual(job.options['max_start_position'],10)
        self.auto.receive(job,{'kind':'result','result':{'times':[10,20,30,40],
            'method':ALIGNMENT_METHOD,'partial':False}},[song])
        self.assertEqual(self.auto.live_timing.load(song)['times'],[5,20,30,40])

    def test_saved_partial_has_only_one_completion_attempt_per_session(self):
        song=self.seed_partial_memory();self.memory_observe(song,0)
        job=next(iter(self.auto.jobs))
        self.auto.receive(job,{'kind':'result','result':{'times':[10,20,None,40],
            'method':ALIGNMENT_METHOD,'partial':True}},[song])
        self.auto.receive(job,{'kind':'finished'},[song])
        for position in (20,0,20,0):self.memory_observe(song,position)
        self.assertFalse(self.auto.jobs)
        self.assertIn('3/4',self.auto.status);self.assertIn('종료',self.auto.status)

    def test_partial_result_allows_one_later_full_start_without_mid_song_retry(self):
        song=self.songs[0];self.memory_observe(song,0);job=next(iter(self.auto.jobs))
        self.auto.receive(job,{'kind':'result','result':{'times':[10,20,None],
            'method':ALIGNMENT_METHOD,'partial':True}},[song])
        self.auto.receive(job,{'kind':'finished'},[song])
        for position in (30,40,50):self.memory_observe(song,position)
        self.assertFalse(self.auto.jobs)
        self.memory_observe(song,0)
        self.assertEqual(len(self.auto.jobs),1)

    def test_complete_analysis_with_gaps_reports_actual_coverage(self):
        song=self.songs[0]
        self.store.save_timing(song,{'times':[10,None,30],'offset':0,
            'source':'음원 분석','audio_finalized':True})
        self.observe(0)
        self.assertFalse(self.auto.jobs)
        self.assertIn('2/3',self.auto.status);self.assertIn('부분',self.auto.status)

    def test_returning_to_saved_partial_refreshes_coverage_without_mid_song_job(self):
        song=self.seed_partial_memory();self.memory_observe(song,20)
        self.auto.observe(None,Playback('Other Song',base=20,duration=100,playing=True,captured=time.monotonic()))
        pending=next(iter(self.auto.jobs))
        self.memory_observe(song,30)
        self.assertTrue(pending.cancelled.is_set())
        self.auto.receive(pending,{'kind':'finished'},[])
        self.assertFalse(self.auto.jobs)
        self.assertIn('3/4',self.auto.status);self.assertIn('처음',self.auto.status)

    def test_finalized_timeline_does_not_retry_a_stale_partial_draft(self):
        song=self.seed_partial_memory()
        timing=self.auto.live_timing.load(song)
        self.auto.live_timing.save(song,{**timing,'audio_finalized':True})
        self.memory_observe(song,0)
        self.assertFalse(self.auto.jobs)
        self.assertIn('3/4',self.auto.status);self.assertNotIn('한 번 보완',self.auto.status)
    def test_next_track_can_capture_while_previous_analysis_waits(self):
        self.observe(0);first=next(iter(self.auto.jobs))
        self.auto.receive(first,{'kind':'progress','state':'queued','text':'전체 분석 순서 대기'},self.songs)
        self.observe(1);second=list(self.auto.jobs)[-1]
        self.auto.receive(second,{'kind':'progress','state':'queued','text':'전체 분석 순서 대기'},self.songs)
        self.assertEqual(len(self.auto.jobs),2)
        self.observe(0)
        self.assertEqual(len(self.auto.jobs),2)
    def test_queue_limit_prevents_unbounded_audio_and_model_work(self):
        for i in range(5):self.observe(i)
        self.assertEqual(len(self.auto.jobs),3)
    def test_auto_result_fills_blanks_preserving_manual_and_backup(self):
        self.store.save_timing(self.songs[0],{'times':[5,None,None],'offset':2})
        self.observe(0);job=next(iter(self.auto.jobs))
        self.auto.receive(job,{'kind':'result','result':{'times':[10,20,30]}},self.songs)
        self.assertEqual(self.store.timing(self.songs[0])['times'],[5,18,28])
        backup=read_json(self.store.directory/'audio_backups'/'0.before-gap-v1.json',{})
        self.assertEqual(backup['times'],[5,None,None])
    def test_disabling_auto_prevents_late_result_writes(self):
        self.observe(0);job=next(iter(self.auto.jobs))
        self.auto.set_enabled(False)
        self.auto.receive(job,{'kind':'result','result':{'times':[10,20,30]}},self.songs)
        self.assertEqual(self.store.timing(self.songs[0])['times'],[None]*3)
        self.assertTrue(job.cancelled.is_set())

    def test_turning_auto_back_on_can_retry_a_user_cancelled_first_listen(self):
        self.observe(0);old=next(iter(self.auto.jobs))
        self.auto.set_enabled(False)
        self.auto.receive(old,{'kind':'finished'},self.songs)
        with patch('auto_audio.ready'):self.auto.set_enabled(True)
        self.observe(0)
        self.assertEqual(len(self.auto.jobs),1)
        self.assertIsNot(next(iter(self.auto.jobs)),old)

    def test_sparse_recognition_is_saved_as_draft_without_changing_timing(self):
        self.observe(0);job=next(iter(self.auto.jobs))
        self.auto.receive(job,{'kind':'result','result':{'times':[10,None,None]}},self.songs)
        self.assertEqual(self.store.timing(self.songs[0])['times'],[None]*3)
        draft=read_json(self.store.directory/'audio_drafts'/'0.json',{})
        self.assertEqual(draft['times'],[10,None,None])
        self.assertIn('후보만',self.auto.status)

    def test_full_queue_does_not_mark_unstarted_song_as_already_analyzed(self):
        for i in range(4):self.observe(i)
        job=next(iter(self.auto.jobs));self.auto.receive(job,{'kind':'finished'},self.songs)
        self.observe(3)
        self.assertTrue(any(job.song.id=='3' for job in self.auto.jobs))

    def test_failed_capture_retries_on_next_start_only_and_is_bounded(self):
        self.observe(0);job=next(iter(self.auto.jobs))
        for attempt in range(2):
            self.auto.receive(job,{'kind':'error','text':'seek'},self.songs)
            self.auto.receive(job,{'kind':'finished'},self.songs)
            self.observe(0);self.assertFalse(self.auto.jobs)
            song=self.songs[0]
            self.auto.observe(song,Playback(song.title,song.artist,song.album,20,100,True,time.monotonic()))
            self.observe(0)
            if attempt==0:job=next(iter(self.auto.jobs))
        self.assertFalse(self.auto.jobs)

    def current_playback(self,index=0,position=0):
        song=self.songs[index]
        return Playback(song.title,song.artist,song.album,position,100,True,time.monotonic())

    def test_external_sync_prevents_automatic_job_and_does_not_mark_song_seen(self):
        song=self.songs[0];playback=self.current_playback(position=20)
        self.auto.observe(song,playback,memory_only=True,external_sync=True)
        self.assertFalse(self.auto.jobs)
        self.assertFalse(self.auto.seen)
        self.assertFalse(self.auto.attempts)
        self.assertIn('자동 분석 생략',self.auto.status)

    def test_external_sync_cancels_active_job_and_rejects_queued_results_without_changing_manual_timing(self):
        song=self.songs[0]
        self.store.save_timing(song,{'times':[5,None,None],'offset':2,'source':'직접 기록'})
        path=self.store.directory/'timings'/(song.id+'.json');before=path.read_bytes()
        self.observe(0);job=next(iter(self.auto.jobs))
        self.auto.observe(song,self.current_playback(),external_sync=True)
        self.assertTrue(job.cancelled.is_set())
        self.assertIsNone(self.auto.receive(job,{'kind':'result','result':{'times':[10,20,30]}},self.songs))
        self.assertEqual(path.read_bytes(),before)
        self.assertFalse(self.auto.draft_path(song).exists())
        self.assertNotIn(job.analysis_key,self.auto.seen)
        self.assertNotIn(job.analysis_key,self.auto.attempts)

    def test_external_sync_cancels_only_current_tracks_automatic_jobs(self):
        self.observe(0);old=next(iter(self.auto.jobs))
        self.auto.receive(old,{'kind':'progress','state':'queued','text':'전체 분석 순서 대기'},self.songs)
        self.observe(1);current=list(self.auto.jobs)[1]
        old_attempts=self.auto.attempts[old.analysis_key]
        self.auto.observe(self.songs[1],self.current_playback(1),external_sync=True)
        self.assertTrue(current.cancelled.is_set())
        self.assertFalse(old.cancelled.is_set())
        self.assertIn(old.analysis_key,self.auto.seen)
        self.assertEqual(self.auto.attempts[old.analysis_key],old_attempts)
        self.assertIsNotNone(self.auto.receive(old,{'kind':'result','result':{'times':[10,20,30]}},self.songs))

    def test_awaiting_provider_preserves_audio_before_enabling_analysis(self):
        song=self.songs[0];playback=self.current_playback(position=20)
        self.auto.observe(song,playback,memory_only=True,awaiting_sync=True)
        job=next(iter(self.auto.jobs))
        self.assertTrue(job.waiting_for_lyrics)
        self.assertIsNone(job.song)
        self.assertFalse(self.auto.seen)
        self.auto.observe(song,playback,memory_only=True,awaiting_sync=False)
        self.assertEqual(len(self.auto.jobs),1)
        self.assertFalse(job.waiting_for_lyrics)
        self.assertIs(job.song,song)

    def test_awaiting_provider_does_not_cancel_an_already_started_job(self):
        self.observe(0);job=next(iter(self.auto.jobs))
        self.auto.observe(self.songs[0],self.current_playback(),awaiting_sync=True)
        self.assertFalse(job.cancelled.is_set())
        self.assertEqual(len(self.auto.jobs),1)

    def test_external_sync_disappearance_allows_new_job_after_cancelled_worker_finishes(self):
        song=self.songs[0];playback=self.current_playback(position=20)
        self.auto.observe(song,playback,memory_only=True);old=next(iter(self.auto.jobs))
        self.auto.observe(song,playback,memory_only=True,external_sync=True)
        self.auto.receive(old,{'kind':'finished'},[song])
        self.auto.observe(song,playback,memory_only=True,external_sync=True)
        self.assertFalse(self.auto.jobs)
        self.auto.observe(song,playback,memory_only=True)
        replacement=next(iter(self.auto.jobs))
        self.assertIsNot(replacement,old)
        self.assertEqual(self.auto.attempts[replacement.analysis_key],1)

    def test_external_sync_clears_prior_failed_attempt_even_when_worker_already_finished(self):
        song=self.songs[0];playback=self.current_playback(position=20)
        self.auto.observe(song,playback,memory_only=True);old=next(iter(self.auto.jobs))
        self.auto.receive(old,{'kind':'error','text':'failed'},[song])
        self.auto.receive(old,{'kind':'finished'},[song])
        self.auto.observe(song,playback,memory_only=True,external_sync=True)
        self.assertNotIn(old.analysis_key,self.auto.retryable)
        self.auto.observe(song,playback,memory_only=True)
        self.assertEqual(len(self.auto.jobs),1)

    def test_external_sync_marks_cancelled_before_slow_worker_shutdown_finishes(self):
        self.observe(0);job=next(iter(self.auto.jobs))
        entered=threading.Event();release=threading.Event();finished=threading.Event()
        def slow_cancel():
            entered.set()
            release.wait(timeout=2)
            finished.set()
        job.cancel=slow_cancel
        try:
            self.auto.observe(self.songs[0],self.current_playback(),external_sync=True)
            self.assertTrue(job.cancelled.is_set())
            self.assertTrue(entered.wait(timeout=1))
            self.assertFalse(finished.is_set())
            self.assertIsNone(self.auto.receive(job,{'kind':'result','result':{'times':[10,20,30]}},self.songs))
        finally:
            release.set();self.assertTrue(finished.wait(timeout=1))

    def test_disable_returns_before_slow_shutdown_and_reenable_rejects_old_result(self):
        self.observe(0);job=next(iter(self.auto.jobs))
        entered=threading.Event();release=threading.Event();finished=threading.Event()
        calls=[]
        def slow_cancel():
            calls.append(1);entered.set()
            release.wait(timeout=2)
            finished.set()
        original_cancel=job.cancel;job.cancel=slow_cancel
        try:
            self.auto.set_enabled(False)
            self.assertTrue(job.cancelled.is_set())
            self.assertTrue(entered.wait(timeout=1))
            self.assertFalse(finished.is_set())
            self.auto.set_enabled(False)
            with patch('auto_audio.ready'):self.auto.set_enabled(True)
            self.assertIsNone(self.auto.receive(job,{'kind':'result','result':{'times':[10,20,30]}},self.songs))
            self.assertEqual(calls,[1])
            self.assertFalse((self.store.directory/'timings').exists())
        finally:
            release.set();self.assertTrue(finished.wait(timeout=1));job.cancel=original_cancel


if __name__=='__main__':unittest.main()
