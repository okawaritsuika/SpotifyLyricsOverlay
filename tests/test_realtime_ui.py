import tempfile,threading,time,tkinter as tk,unittest
from unittest.mock import patch
from types import SimpleNamespace
from app import LyricsApp
from lyrics import Song,Line
from media import Playback
from storage import Store


class RealtimeUITests(unittest.TestCase):
    def test_large_recovery_requires_two_strong_unique_acoustic_confirmations(self):
        self.app.receive_realtime(self.job,self.event(0))
        event=self.event(5)
        self.app.receive_realtime(self.job,event)
        self.assertEqual(self.app.realtime_index,0)
        event['match'].update(recovery=True,unique=True,confirmations=2,confidence=.85,
                              characters=9,similarity=.95,match_method='kana')
        self.app.receive_realtime(self.job,event)
        self.assertEqual(self.app.realtime_index,5)
        self.assertEqual(self.store.timing(self.song)['times'],[None]*6)

    def test_recovery_does_not_allow_ambiguous_chorus_or_backward_motion(self):
        self.app.receive_realtime(self.job,self.event(0))
        event=self.event(5)
        event['match'].update(recovery=True,unique=False,confirmations=2,confidence=.85,
                              characters=9,similarity=.95,match_method='text')
        self.app.receive_realtime(self.job,event)
        self.assertEqual(self.app.realtime_index,0)
        self.app.receive_realtime(self.job,self.event(3))
        self.app.receive_realtime(self.job,self.event(1))
        self.assertEqual(self.app.realtime_index,3)

    def test_error_detail_remains_visible_until_tracking_recovers(self):
        text='음원 구간 대조 오류 (ValueError) · 다음 최신 구간에서 재시도'
        self.app.receive_realtime(self.job,{'kind':'realtime_status','state':'retrying','text':text})
        self.app.render();self.assertIn(text,self.app.mode_label.cget('text'))
        self.app.receive_realtime(self.job,self.event(1))
        self.app.render();self.assertEqual(self.app.realtime_detail,'')
        self.assertNotIn(text,self.app.mode_label.cget('text'))

    def test_long_vocalise_holds_until_verified_next_line_and_respects_user_timing(self):
        self.song=Song('repeat','Sample','Artist','Album',tuple(Line(text,'','번역') for text in ('青い空','La...', '続く言葉','終わりの歌')))
        self.app.songs=[self.song];self.app.select_song(self.song)
        now=time.monotonic()
        self.app.playback=Playback('Sample','Artist','Album',150,300,False,now)
        self.assertEqual(self.app.displayed_index(),1)
        self.assertTrue(self.app.vocalise_waiting)
        self.app.render();self.assertIn('반복 구절',self.app.mode_label.cget('text'))
        self.job.song=self.song
        self.app.receive_realtime(self.job,{'kind':'realtime','observed_at':now,'position':150,
            'match':{'index':2,'confidence':.8,'onset':149}})
        self.assertEqual(self.app.displayed_index(),2)
        self.app.clear_realtime()
        self.app.timing={'times':[None,None,145,None],'offset':0,'source':'직접 기록'}
        self.assertEqual(self.app.displayed_index(),2)
        self.app.timing={'times':[None]*4,'offset':0}
        self.app.manual_index=3;self.app.follow.set(False)
        self.assertEqual(self.app.displayed_index(),3)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(self.temp.name)
        self.song=Song('sample','Sample','Artist','Album',tuple(Line(f'青い空{i}','','') for i in range(6)))
        self.store.save_library([self.song]);self.store.save_settings({'online':False,'auto_audio':False,'borderless':False})
        self.root=tk.Tk();self.root.withdraw();self.app=LyricsApp(self.root,self.store,start_services=False)
        self.playback=Playback('Sample','Artist','Album',10,180,True,time.monotonic())
        self.app.set_playback(self.playback)
        self.job=SimpleNamespace(song=self.song,playback_key=self.playback.key,cancelled=threading.Event())
        self.app.spotify_sample_after=time.monotonic()-5

    def tearDown(self):self.app.close();self.temp.cleanup()

    def event(self,index,age=.4):
        observed=time.monotonic()-age
        position=self.playback.base+(observed-self.playback.captured)
        return {'kind':'realtime','observed_at':observed,'position':position,'match':{'index':index,'confidence':.8}}

    def test_loading_uses_estimate_but_stale_match_keeps_last_confirmed_row(self):
        self.app.playback=Playback('Sample','Artist','Album',90,180,False,time.monotonic())
        self.app.receive_realtime(self.job,{'kind':'realtime_status','state':'loading'})
        self.assertFalse(self.app.following_audio())
        self.assertGreater(self.app.displayed_index(),0)
        now=time.monotonic()
        self.app.receive_realtime(self.job,{'kind':'realtime','observed_at':now,'position':90,
            'match':{'index':2,'confidence':.8,'onset':89}})
        self.assertEqual(self.app.displayed_index(),2)
        self.app.playback=Playback('Sample','Artist','Album',130,180,False,now)
        with patch('app.time.monotonic',return_value=now+7):
            self.assertFalse(self.app.following_audio())
            self.assertEqual(self.app.displayed_index(),2)
            self.app.render()
            self.assertIn('음원 재확인 대기',self.app.playback_badge.cget('text'))
        self.assertEqual(self.store.timing(self.song)['times'],[None]*6)

    def test_fresh_spotify_highlight_can_take_over_after_audio_match_expires(self):
        self.app.receive_realtime(self.job,self.event(1))
        self.app.realtime_matched_at-=7
        self.app.receive_spotify_active(self.playback.key,tuple(x.original for x in self.song.lines),2)
        self.assertTrue(self.app.following_spotify())
        self.assertEqual(self.app.displayed_index(),2)

    def test_first_listen_uses_sound_without_saving_provisional_times(self):
        self.app.receive_realtime(self.job,{'kind':'realtime_status','state':'listening'})
        self.assertIsNone(self.app.displayed_index())
        self.app.receive_realtime(self.job,self.event(2))
        self.assertEqual(self.app.displayed_index(),2)
        self.assertTrue(self.app.following_audio())
        self.assertEqual(self.store.timing(self.song)['times'],[None]*6)
        self.assertIn('실시간 음원',self.app.mode_label.cget('text'))

    def test_delayed_observation_uses_capture_clock_not_latest_poll_time(self):
        self.app.receive_realtime(self.job,self.event(1,age=2.2))
        self.assertEqual(self.app.displayed_index(),1)

    def test_old_other_track_and_seek_results_are_ignored(self):
        self.app.receive_realtime(self.job,self.event(1,age=4))
        self.assertIsNone(self.app.realtime_index)
        event=self.event(1);self.app.spotify_sample_after=time.monotonic()
        self.app.receive_realtime(self.job,event)
        self.assertIsNone(self.app.realtime_index)
        self.job.playback_key='other';self.app.receive_realtime(self.job,self.event(1))
        self.assertIsNone(self.app.realtime_index)

    def test_preview_and_saved_manual_or_final_analysis_keep_priority(self):
        self.app.receive_realtime(self.job,self.event(2))
        self.app.move(-1)
        self.app.receive_realtime(self.job,self.event(3))
        self.assertEqual(self.app.displayed_index(),1)
        self.app.resume_follow()
        for extra in ({'source':'직접 기록'},{'source':'음원 분석','audio_finalized':True}):
            self.app.timing={'times':[1,20,30,40,50,60],'offset':0,**extra}
            self.assertFalse(self.app.following_audio())
            self.assertEqual(self.app.displayed_index(),0)

    def test_cancelled_worker_never_remains_in_live_mode(self):
        self.app.receive_realtime(self.job,self.event(1))
        self.job.cancelled.set()
        self.assertFalse(self.app.following_audio())

    def test_regular_phrase_pace_waits_for_actual_next_model_result(self):
        for index,onset in enumerate((10,12,14)):
            now=time.monotonic()
            self.app.playback=Playback('Sample','Artist','Album',onset+.5,180,False,now)
            self.app.receive_realtime(self.job,{'kind':'realtime','observed_at':now,'position':onset+.5,
                'match':{'index':index,'confidence':.8,'onset':onset}})
        self.app.playback=Playback('Sample','Artist','Album',16.1,180,False,time.monotonic())
        self.app.render()
        self.assertEqual(self.app.realtime_index,2)
        self.assertEqual(self.app.displayed_index(),2)
        self.assertNotIn('속도 예측',self.app.mode_label.cget('text'))
        self.assertEqual(self.store.timing(self.song)['times'],[None]*6)
        self.app.timing={'times':[None,None,None,18,None,None],'offset':0,'source':'직접 기록'}
        self.assertEqual(self.app.displayed_index(),2)
        self.app.set_playback(Playback('Sample','Artist','Album',80,180,False,time.monotonic()))
        self.assertIsNone(self.app.realtime_index)

    def test_partial_phrase_positions_never_predict_an_unheard_next_row(self):
        for index,position in enumerate((10,12,14)):
            now=time.monotonic()
            self.app.playback=Playback('Sample','Artist','Album',position+.5,180,False,now)
            self.app.receive_realtime(self.job,{'kind':'realtime','observed_at':now,'position':position+.5,
                'match':{'index':index,'confidence':.4,'onset':None,'evidence_start':position}})
        self.app.playback=Playback('Sample','Artist','Album',16.1,180,False,time.monotonic())
        self.app.render()
        self.assertEqual(self.app.realtime_index,2)
        self.assertEqual(self.app.displayed_index(),2)
        self.assertNotIn('임시 표시',self.app.mode_label.cget('text'))
        self.assertEqual(self.store.timing(self.song)['times'],[None]*6)

    def test_click_through_keeps_a_focusable_editor_and_closing_it_unlocks(self):
        self.app.lock_available=True
        self.app.toggle_lock()
        self.assertTrue(self.app.locked);self.assertIsNotNone(self.app.editor)
        self.app.close_editor()
        self.assertFalse(self.app.locked)
