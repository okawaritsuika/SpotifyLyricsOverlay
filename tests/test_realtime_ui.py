import tempfile,threading,time,tkinter as tk,unittest
from unittest.mock import patch
from types import SimpleNamespace
from app import LyricsApp
from lyrics import Song,Line
from media import Playback
from storage import Store


class RealtimeUITests(unittest.TestCase):
    def test_developer_current_row_keeps_following_live_matches(self):
        self.app.receive_realtime(self.job,self.event(2))
        workspace=self.app.workspace
        self.assertEqual(workspace.developer('get',{})['current'],2)
        self.app.receive_realtime(self.job,self.event(3))
        self.assertEqual(workspace.developer('position',{})['current'],3)
        self.assertEqual(self.store.timing(self.song)['times'],[None]*6)

    def test_developer_receives_the_rendered_row_with_its_playback_position(self):
        workspace=self.app.workspace
        workspace.ready=True
        self.app.receive_realtime(self.job,self.event(2))
        state=workspace.developer('get',{})
        with patch.object(workspace,'send') as send, patch.object(Playback,'position',return_value=10.25):
            self.app.receive_realtime(self.job,self.event(3))
            self.assertEqual(self.app.original_label.cget('text'),self.song.lines[3].original)
            send.assert_called_once_with({'developerPlayback':{
                'track':state['track'],'current':3,'position':10.25}})
            self.app.render()
            send.assert_called_once()
            workspace.developer('leave',{})
            self.app.render()
            send.assert_called_once()

    def test_electron_overlay_and_developer_share_a_playback_update(self):
        self.root.attributes('-alpha',0)
        self.root.deiconify()
        self.root.update_idletasks()
        workspace=self.app.workspace
        workspace.ready=True
        self.app.receive_realtime(self.job,self.event(2))
        state=workspace.developer('get',{})
        self.root.update_idletasks()
        workspace.overlay_requested=True
        workspace.overlay_ready=True

        with (patch.object(workspace,'send') as send,
              patch.object(Playback,'position',return_value=10.25)):
            self.app.receive_realtime(self.job,self.event(3))
            self.root.update_idletasks()
            send.assert_called_once()
            (message,),_ = send.call_args
            self.assertEqual(message['overlay']['current'],3)
            self.assertEqual(message['overlay']['labels']['original']['text'],self.song.lines[3].original)
            self.assertEqual(message['overlay']['labels']['clock']['text'],'00:10 / 03:00')
            self.assertEqual(message['developerPlayback'],{'track':state['track'],'current':3,'position':10.25})
            self.app.render()
            self.root.update_idletasks()
            send.assert_called_once()

    def test_missing_electron_runtime_keeps_the_native_overlay(self):
        workspace=self.app.workspace
        with (patch('desktop_workspace.electron_path',return_value=None),
              patch.object(self.app,'electron_overlay_snapshot') as snapshot):
            workspace.start_overlay()
            self.app.render()
        snapshot.assert_not_called()
        self.assertFalse(workspace.overlay_requested)
        self.assertFalse(workspace.overlay_ready)
        self.assertFalse(self.app.electron_overlay_active)

    def test_electron_overlay_waits_for_ready_and_restores_native_overlay_on_disconnect(self):
        workspace=self.app.workspace
        process=SimpleNamespace(poll=lambda:1)
        workspace.process=process
        workspace.overlay_requested=True

        def poll(message):
            if workspace.callback is not None:
                self.root.after_cancel(workspace.callback)
            workspace.events.put((process,message))
            workspace.poll()

        with patch.object(self.app,'set_electron_overlay_active') as activate:
            poll({'ready':True})
            poll({'overlayReady':True,'hwnd':'invalid'})
            activate.assert_not_called()
            self.assertFalse(workspace.overlay_ready)
            poll({'overlayReady':True,'hwnd':'1234'})
            self.assertTrue(workspace.overlay_ready)
            activate.assert_called_once_with(True,hwnd=1234)
            workspace.developer_subscribed=True
            poll({'workspaceClosed':True})
            self.assertFalse(workspace.developer_subscribed)
            self.assertTrue(workspace.overlay_ready)
            activate.assert_called_once()
            poll(None)
            activate.assert_called_with(False)
        self.assertFalse(workspace.overlay_requested)
        self.assertFalse(workspace.overlay_ready)
        self.assertIsNone(workspace.process)

    def test_native_developer_highlight_applies_the_same_lyric_lead_as_overlay(self):
        self.app.open_developer_mode()
        dialog=self.app.manual_sync_dialog
        self.app.timing={**self.app.timing,'times':[0,10,20,30,40,50]}
        self.app.lyric_advance_ms=250
        dialog.update_times()
        with patch.object(Playback,'position',return_value=9.8):
            self.app.render()
        self.assertEqual(self.app.original_label.cget('text'),self.song.lines[1].original)
        self.assertEqual(dialog.playhead,1)
        self.assertEqual(dialog.selected_index(),0)

    def test_developer_tracking_switch_persists_and_clears_the_active_match(self):
        self.app.receive_realtime(self.job,self.event(1))
        self.assertEqual(self.app.realtime_index,1)
        state=self.app.workspace.developer('setRealtimeTracking',{'enabled':False})
        self.assertFalse(state['realtimeTracking'])
        self.assertFalse(self.store.settings()['realtime_tracking'])
        self.assertFalse(self.app.auto_analyzer.realtime_enabled)
        self.assertIsNone(self.app.realtime_index)
        self.app.receive_realtime(self.job,self.event(2,age=0))
        self.assertIsNone(self.app.realtime_index)
        state=self.app.workspace.developer('setRealtimeTracking',{'enabled':True})
        self.assertTrue(state['realtimeTracking'])
        self.app.receive_realtime(self.job,self.event(1))
        self.assertIsNone(self.app.realtime_index)
        self.app.receive_realtime(self.job,self.event(2,age=0))
        self.assertEqual(self.app.realtime_index,2)

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

    def test_error_detail_remains_available_until_tracking_recovers(self):
        text='음원 구간 대조 오류 (ValueError) · 다음 최신 구간에서 재시도'
        self.app.receive_realtime(self.job,{'kind':'realtime_status','state':'retrying','text':text})
        self.assertEqual(self.app.realtime_detail,text)
        self.app.receive_realtime(self.job,self.event(1))
        self.app.render();self.assertEqual(self.app.realtime_detail,'')

    def test_long_vocalise_holds_until_verified_next_line_and_respects_user_timing(self):
        self.song=Song('repeat','Sample','Artist','Album',tuple(Line(text,'','번역') for text in ('青い空','La...', '続く言葉','終わりの歌')))
        self.app.songs=[self.song];self.app.select_song(self.song)
        now=time.monotonic()
        self.app.playback=Playback('Sample','Artist','Album',150,300,False,now)
        self.assertEqual(self.app.displayed_index(),1)
        self.assertTrue(self.app.vocalise_waiting)
        self.app.render();self.assertEqual(self.app.mode_label.cget('text'),'')
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
            self.assertEqual(self.app.playback_badge.cget('text'),'02:10 / 03:00')
        self.assertEqual(self.store.timing(self.song)['times'],[None]*6)

    def test_fresh_spotify_highlight_can_take_over_after_audio_match_expires(self):
        self.app.receive_realtime(self.job,self.event(1))
        self.app.realtime_matched_at-=7
        self.app.receive_spotify_active(self.playback.key,tuple(x.original for x in self.song.lines),2)
        self.assertTrue(self.app.following_spotify())
        self.assertEqual(self.app.displayed_index(),2)

    def test_first_listen_uses_sound_without_saving_provisional_times(self):
        with patch('app.time.monotonic',return_value=self.playback.captured):
            self.app.receive_realtime(self.job,{'kind':'realtime_status','state':'listening'})
            self.assertIsNone(self.app.displayed_index())
        self.app.receive_realtime(self.job,self.event(2))
        self.assertEqual(self.app.displayed_index(),2)
        self.assertTrue(self.app.following_audio())
        self.assertEqual(self.store.timing(self.song)['times'],[None]*6)

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

    def test_preview_keeps_tracking_until_saved_manual_or_final_analysis_takes_priority(self):
        self.app.receive_realtime(self.job,self.event(2))
        self.app.move(-1)
        self.app.receive_realtime(self.job,self.event(3))
        self.assertEqual(self.app.displayed_index(),3)
        self.assertTrue(self.app.follow.get())
        self.assertIsNone(self.app.manual_index)
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

    def test_click_through_leaves_editors_closed_and_can_be_unlocked(self):
        self.app.lock_available=True
        self.app.toggle_lock()
        self.assertTrue(self.app.locked)
        self.assertTrue(self.app.backdrop.click_through)
        self.assertIsNone(self.app.editor)
        self.assertIsNone(self.app.manual_sync_dialog)
        self.app.toggle_lock()
        self.assertFalse(self.app.locked)
        self.assertFalse(self.app.backdrop.click_through)
