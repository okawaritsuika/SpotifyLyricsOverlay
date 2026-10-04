"""Late public/manual candidates must retain the recording they came from."""
import gc
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from app import LyricsApp
from lyrics import Line,Song
from media import Playback
from storage import Store,atomic_json


class FakeJob:
    def __init__(self,base,song,playback,messages,**options):
        self.song=song;self.playback_key=playback.key
        self.cancelled=threading.Event()
    def cancel(self):self.cancelled.set()
    def close(self):self.cancel()


class RecordingContextUITests(unittest.TestCase):
    def setUp(self):
        gc.collect();self.temp=tempfile.TemporaryDirectory();self.store=Store(self.temp.name)
        self.song=Song('recording','Same title','Singer','Album',tuple(Line(x,'','') for x in ('One','Two','Three')))
        self.other=Song('other','Other title','Singer','Album',self.song.lines)
        self.store.save_library([self.song,self.other])
        self.store.save_settings({'online':False,'auto_audio':False,'borderless':False,'use_estimate':False})
        self.root=tk.Tk();self.root.withdraw();self.app=LyricsApp(self.root,self.store,start_services=False)
        self.play(90)

    def tearDown(self):
        self.app.close();self.temp.cleanup();self.app=self.root=None;gc.collect()

    def play(self,duration,song=None):
        song=song or self.song
        self.app.set_playback(Playback(song.title,song.artist,song.album,0,duration,False,time.monotonic()))

    def files(self):
        return {str(p.relative_to(self.store.directory)):p.read_bytes()
                for p in self.store.directory.rglob('*.json')}

    def request_public(self):
        with patch('app.threading.Thread') as factory:
            self.app.fetch_timing(True)
        return factory.call_args.kwargs['target'] if factory.called else None

    def finish_public(self,worker):
        with patch('app.get_public_timing',return_value={'times':[10,20,30],'matched':3,'source':'LRCLIB'}):worker()
        kind,args=self.app.messages.get_nowait();self.assertEqual(kind,'timing')
        self.app.receive_timing(*args)

    def start_audio(self):
        with patch('app.audio_ready'),patch('app.AudioJob',FakeJob):self.app.start_audio()
        return self.app.audio_job

    def finish_audio(self,job):
        self.app.receive_audio(job,{'kind':'result','result':{'times':[10,20,30],'duration':90}})
        self.app.receive_audio(job,{'kind':'finished'})

    def seed_draft(self,duration=90):
        value={'times':[10,20,30],'fingerprint':self.song.fingerprint}
        if duration is not None:value['duration']=duration
        atomic_json(self.app.audio_draft_path(self.song),value)
        self.app.audio_song=self.song;self.app.audio_draft=value
        self.store.save_timing(self.song,{'times':[5,None,None],'offset':2,'origins':['manual',None,None]})
        self.app.select_song(self.song)

    def test_public_response_after_duration_conflict_cannot_write(self):
        worker=self.request_public();self.play(180);before=self.files()
        self.finish_public(worker)
        self.assertEqual(self.files(),before)

    def test_public_response_cannot_revive_after_same_length_returns(self):
        worker=self.request_public();self.play(180);self.play(90);before=self.files()
        self.finish_public(worker)
        self.assertEqual(self.files(),before)

    def test_old_public_completion_does_not_clear_replacement_query(self):
        old=self.request_public();self.play(180);new=self.request_public()
        self.assertIsNotNone(new)
        self.finish_public(old)
        self.assertIn(self.song.id,self.app.query_running)
        self.finish_public(new)
        self.assertNotIn(self.song.id,self.app.query_running)
        self.assertEqual(self.store.timing(self.song)['times'],[10,20,30])

    def test_public_background_result_remains_valid_when_another_song_plays(self):
        worker=self.request_public();self.play(180,self.other)
        self.finish_public(worker)
        self.assertEqual(self.store.timing(self.song)['times'],[10,20,30])
        self.assertEqual(self.app.song,self.other)
        self.assertEqual(self.store.timing(self.other)['times'],[None]*3)

    def test_manual_audio_conflict_rejects_draft_and_finishes_cleanup(self):
        job=self.start_audio();self.play(180);before=self.files()
        self.assertTrue(job.cancelled.is_set())
        self.finish_audio(job)
        self.assertEqual(self.files(),before);self.assertIsNone(self.app.audio_job)

    def test_manual_audio_result_cannot_revive_after_return(self):
        job=self.start_audio();self.play(180);self.play(90);before=self.files()
        self.finish_audio(job)
        self.assertEqual(self.files(),before);self.assertIsNone(self.app.audio_draft)

    def test_manual_background_completion_and_application_keep_current_other_song(self):
        job=self.start_audio();self.play(180,self.other);self.finish_audio(job)
        self.assertIsNotNone(self.app.audio_draft)
        self.app.apply_audio()
        self.assertEqual(self.store.timing(self.song)['times'],[10,20,30])
        self.assertEqual(self.app.song,self.other)

    def test_known_draft_length_conflict_preserves_manual_timing_and_preview(self):
        self.seed_draft();self.play(180);before=self.files()
        with self.assertRaises(ValueError):self.app.apply_audio()
        self.assertEqual(self.files(),before);self.assertIsNotNone(self.app.audio_draft)

    def test_restore_rejects_wrong_length_before_releasing_manual_row(self):
        self.seed_draft();self.play(180)
        self.app.open_editor();self.root.update();self.app.select_editor_row(0);self.root.update()
        before=self.files();timing=self.app.timing.copy()
        with self.assertRaises(ValueError):self.app.restore_selected_auto()
        self.assertEqual(self.files(),before);self.assertEqual(self.app.timing,timing)

    def test_legacy_unknown_draft_keeps_compatibility_without_assigning_owner(self):
        self.seed_draft(None);self.play(180);draft=self.app.audio_draft_path(self.song).read_bytes()
        self.app.apply_audio()
        self.assertEqual(self.store.timing(self.song)['times'],[5,18,28])
        self.assertNotIn('duration',self.store.timing(self.song))
        self.assertEqual(self.app.audio_draft_path(self.song).read_bytes(),draft)

    def test_persisted_valid_draft_can_be_reopened_after_returning_to_its_length(self):
        self.seed_draft();self.play(180);self.play(90)
        self.app.audio_draft=None;self.app.open_audio();self.root.update()
        self.app.apply_audio()
        self.assertEqual(self.store.timing(self.song)['times'],[5,18,28])

    def test_two_second_tolerance_keeps_manual_job_and_persisted_candidate(self):
        job=self.start_audio();self.play(92)
        self.assertFalse(job.cancelled.is_set());self.finish_audio(job);self.app.apply_audio()
        self.assertEqual(self.store.timing(self.song)['times'],[10,20,30])

    def test_realtime_highlight_stops_immediately_on_manual_job_duration_conflict(self):
        job=self.start_audio()
        event={'kind':'realtime','position':0,'observed_at':time.monotonic(),
               'match':{'index':0,'confidence':1,'onset':0}}
        self.app.receive_audio(job,event)
        self.assertTrue(self.app.following_audio())
        self.play(180)
        self.assertFalse(self.app.following_audio())
        self.app.clear_realtime();self.app.receive_audio(job,{**event,'observed_at':time.monotonic()})
        self.assertIsNone(self.app.realtime_job)

    def test_manual_shutdown_does_not_block_duration_transition(self):
        job=self.start_audio();original=job.cancel
        entered=threading.Event();release=threading.Event();finished=threading.Event()
        def slow_cancel():
            entered.set();release.wait(timeout=2);finished.set()
        job.cancel=slow_cancel
        try:
            self.play(180)
            self.assertTrue(job.cancelled.is_set());self.assertTrue(entered.wait(timeout=1))
            self.assertFalse(finished.is_set())
        finally:
            release.set();finished.wait(timeout=1);job.cancel=original

    def test_new_draft_records_start_duration_instead_of_worker_claim(self):
        job=self.start_audio()
        self.app.receive_audio(job,{'kind':'result','result':{'times':[10,20,30],'duration':999}})
        self.assertEqual(self.app.audio_draft['duration'],90)
        self.assertEqual(len(self.app.audio_draft['recording_key']),64)
        self.assertNotIn('generation',self.app.audio_draft)

    def test_public_error_cleans_only_its_current_request(self):
        worker=self.request_public()
        with patch('app.get_public_timing',side_effect=ValueError('synthetic')):worker()
        _,args=self.app.messages.get_nowait();before=self.files()
        self.app.receive_timing(*args)
        self.assertNotIn(self.song.id,self.app.query_running)
        self.assertEqual(self.files(),before)

    def test_observed_conflict_before_cache_failure_still_rejects_old_public_request(self):
        worker=self.request_public()
        with patch.object(self.app,'restore_cached_lyrics',side_effect=OSError('synthetic')):
            with self.assertRaises(OSError):self.play(180)
        self.play(90);before=self.files();self.finish_public(worker)
        self.assertEqual(self.files(),before)


if __name__=='__main__':unittest.main()
