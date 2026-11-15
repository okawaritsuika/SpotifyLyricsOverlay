"""Exercise Tk state transitions with temporary data and no external services."""
from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from app import LyricsApp
from lyric_performers import LYRIC_GUTTER
from lyrics import Line, Song
from media import Playback
from storage import Store


class UITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.a = Song('a','Example A','Artist A','Example',tuple(Line(f'日本語の例{i}', f'にほんごの れい{i}', f'가사 예시 {i}') for i in range(4)))
        self.b = Song('b','Example B','Artist B','Example',self.a.lines)
        self.store.save_library([self.a,self.b])
        self.store.save_settings({'online':False,'use_estimate':False,'transparent':False,'borderless':False})
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = LyricsApp(self.root,self.store,start_services=False)

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def playback(self,song,position=10):
        self.app.set_playback(Playback(song.title,song.artist,song.album,position,180,False,time.monotonic()))

    def receive_public(self,song,result):
        context=self.app.recording_contexts.capture(song,Playback(song.title,song.artist,song.album,duration=180))
        self.app.query_running[song.id]=context
        self.app.receive_timing(song,result,'',context)

    def test_track_change_clears_previous_lyric(self):
        self.store.save_timing(self.a,{'times':[0,20,40,60],'offset':0,'source':'직접 기록'})
        self.playback(self.a)
        self.assertEqual(self.app.displayed_index(),0)
        self.app.set_playback(Playback('Unregistered','other','',5,120,False,time.monotonic()))
        self.assertIsNone(self.app.song)
        self.assertNotIn('가사 예시',self.app.korean_label.cget('text'))

    def test_volume_panel_is_reused_and_closed_with_overlay(self):
        with patch('volume_popup.SpotifyVolumeController'):
            self.app.volume_button.invoke()
            panel=self.app.volume_popup
            self.assertIsNotNone(panel)
            self.assertTrue(panel.visible)
            self.app.volume_button.invoke()
            self.assertFalse(panel.visible)
            self.app.volume_button.invoke()
            self.assertIs(self.app.volume_popup,panel)
            self.assertTrue(panel.visible)
            self.app.close()
            self.assertTrue(panel.closed)

    def test_minimized_overlay_keeps_matching_incoming_tracks(self):
        self.root.deiconify();self.root.update();self.root.iconify();self.root.update()
        self.assertEqual(self.root.state(),'iconic')
        for song in (self.a,self.b):
            snapshot=Playback(song.title,song.artist,song.album,12,180,False,time.monotonic())
            self.app.messages.put(('media',snapshot));self.app.tick()
            self.assertEqual(self.app.song.id,song.id)
            self.assertEqual(self.app.playback.key,snapshot.key)
            self.assertEqual(self.root.state(),'iconic')
        self.root.deiconify();self.root.update()
        self.assertEqual(self.app.song.id,self.b.id)

    def test_lifecycle_setting_registers_watcher_and_persists(self):
        with patch('app.configure_startup') as startup,patch('app.ensure_watcher') as watcher:
            self.app.spotify_lifecycle.set(True)
            self.app.apply_spotify_lifecycle()
            startup.assert_called_once_with(self.store.directory,True)
            watcher.assert_called_once_with(self.store.directory)
            self.assertTrue(self.store.settings()['spotify_lifecycle'])
            self.assertTrue(self.app.lifecycle.enabled)
            self.app.spotify_lifecycle.set(False)
            self.app.apply_spotify_lifecycle()
            self.assertEqual(startup.call_args.args,(self.store.directory,False))
            watcher.assert_called_once()
            self.assertFalse(self.store.settings()['spotify_lifecycle'])
            self.assertFalse(self.app.lifecycle.enabled)

    def test_spotify_exit_closes_normally_only_when_enabled(self):
        with patch.object(self.app,'close') as close:
            self.app.messages.put(('spotify_exit',None));self.app.tick()
            close.assert_not_called()
            self.app.spotify_lifecycle.set(True)
            self.app.messages.put(('spotify_exit',None));self.app.tick()
            close.assert_called_once()

    def test_lifecycle_registration_failure_restores_setting(self):
        self.app.spotify_lifecycle.set(True)
        with patch('app.configure_startup',side_effect=OSError('registration failed')):
            with self.assertRaises(OSError):self.app.apply_spotify_lifecycle()
        self.assertFalse(self.app.spotify_lifecycle.get())
        self.assertFalse(self.app.lifecycle.enabled)

    def test_lifecycle_startup_preserves_header_and_window_preferences(self):
        self.app.settings.update(borderless=True,window_rect=[1040,267,830,32])
        self.store.save_settings(self.app.settings)
        self.app.spotify_lifecycle.set(True)
        with patch('app.configure_startup'),patch('app.ensure_watcher'),patch.object(self.app,'save_settings') as save:
            self.app.apply_spotify_lifecycle(persist=False)
            save.assert_not_called()
        self.assertTrue(self.store.settings()['borderless'])
        self.assertEqual(self.store.settings()['window_rect'],[1040,267,830,32])

    def test_initial_window_preserves_requested_desktop_width(self):
        self.root.deiconify()
        self.root.update()
        self.root.update()
        from windows import work_area
        left,top,right,bottom = work_area()
        self.assertEqual(self.root.winfo_width(),min(1040,right-left-40))

    def test_end_key_records_the_transition_and_keeps_the_new_current_line(self):
        self.playback(self.a,10)
        self.app.open_editor()
        self.root.update()
        self.app.stamp()
        self.root.update()
        self.assertEqual(self.app.cursor,1)
        self.assertEqual(self.app.displayed_index(),1)
        self.playback(self.a,20)
        self.app.stamp()
        self.root.update()
        self.assertEqual(self.app.cursor,2)
        self.assertEqual(self.app.displayed_index(),2)
        self.assertEqual(self.store.timing(self.a)['times'],[None,10,20,None])
        self.assertFalse(self.app.follow.get())
        self.assertEqual(self.app.mode_label.cget('text'),'')
        self.playback(self.a,50)
        self.assertEqual(self.app.displayed_index(),2)

    def test_stamping_preview_of_other_song_is_refused(self):
        self.playback(self.a)
        self.app.select_song(self.b)
        self.app.stamp()
        self.assertEqual(self.store.timing(self.b)['times'],[None]*4)

    def test_manual_record_can_correct_ahead_of_wrong_audio_anchors(self):
        self.store.save_timing(self.a,{'times':[10,20,25,50],'offset':0,'source':'음원 분석'})
        self.playback(self.a,30)
        self.app.manual_index=0;self.app.follow.set(False)
        self.app.stamp()
        saved=self.store.timing(self.a)
        self.assertEqual(saved['times'],[10,30,None,50])
        self.assertEqual(saved['origins'],['audio','manual',None,'audio'])
        self.assertEqual(self.app.cursor,1)

    def test_manual_edit_clears_only_conflicting_machine_rows(self):
        self.store.save_timing(self.a,{'times':[10,20,30,50],'offset':2,
                                     'origins':['manual','audio','audio','manual']})
        self.playback(self.a,35)
        self.app.open_editor();self.root.update()
        self.app.select_editor_row(2);self.root.update()
        self.app.time_entry.delete(0,'end');self.app.time_entry.insert(0,'00:15.000')
        self.app.save_selected_time()
        saved=self.store.timing(self.a)
        self.assertEqual(saved['times'],[10,None,15,50])
        self.assertEqual(saved['offset'],2)
        before=self.store.timing(self.a)
        self.app.time_entry.delete(0,'end');self.app.time_entry.insert(0,'00:05.000')
        with self.assertRaises(ValueError):self.app.save_selected_time()
        self.assertEqual(self.store.timing(self.a),before)

    def test_estimates_are_never_saved_as_measured_times(self):
        self.app.use_estimate.set(True)
        self.playback(self.a,50)
        self.assertIsNotNone(self.app.displayed_index())
        self.assertTrue(self.app.effective_times()[1])
        self.assertEqual(self.store.timing(self.a)['times'],[None]*4)
        self.app.use_estimate.set(False)
        self.app.render()
        self.assertIsNone(self.app.displayed_index())

    def test_align_now_uses_selected_line_and_resumes_automatic_follow(self):
        self.app.use_estimate.set(True)
        self.playback(self.a,50)
        self.app.manual_index=2
        self.app.follow.set(False)
        self.app.align_now()
        self.assertTrue(self.app.follow.get())
        self.assertEqual(self.app.displayed_index(),2)
        self.assertEqual(self.store.timing(self.a)['times'],[None,None,50,None])
        self.assertEqual(self.store.timing(self.a)['offset'],0)

    def test_header_can_hide_and_restore_without_losing_timing(self):
        self.playback(self.a,12)
        self.app.stamp()
        original=self.store.timing(self.a)
        self.app.chrome_available=True
        self.root.deiconify()
        self.root.update()
        self.app.toggle_chrome()
        self.root.update()
        self.assertTrue(self.root.overrideredirect())
        self.assertFalse(self.app.tools.winfo_ismapped())
        self.assertFalse(self.app.status.winfo_ismapped())
        self.app.toggle_chrome()
        self.root.update()
        self.assertFalse(self.root.overrideredirect())
        self.assertTrue(self.app.tools.winfo_ismapped())
        self.assertEqual(self.store.timing(self.a),original)

    def test_subtitle_clock_is_independent_of_sync_source(self):
        self.playback(self.a,32)
        self.app.use_estimate.set(True);self.app.render()
        self.assertEqual(self.app.playback_badge.cget('text'),'00:32 / 03:00')
        self.app.move(1)
        self.assertEqual(self.app.playback_badge.cget('text'),'00:32 / 03:00')
        self.app.timing={'times':[0,20,40,60],'offset':0,'source':'직접 기록'}
        self.app.resume_follow()
        self.assertEqual(self.app.playback_badge.cget('text'),'00:32 / 03:00')
        self.app.chrome_available=True
        self.root.deiconify();self.root.update()
        self.app.toggle_chrome();self.root.update()
        self.assertTrue(self.app.playback_badge.winfo_ismapped())
        self.assertFalse(self.app.tools.winfo_ismapped())

    def test_grouped_toolbar_fits_on_both_sides_of_responsive_breakpoint(self):
        self.root.deiconify()
        for width in (560,899,900,1040):
            self.root.geometry(f'{width}x360+10+30');self.root.update();self.root.update()
            for button in self.app.tool_buttons:
                self.assertTrue(button.winfo_ismapped())
                self.assertGreaterEqual(button.winfo_rootx(),self.root.winfo_rootx())
                self.assertLessEqual(button.winfo_rootx()+button.winfo_width(),self.root.winfo_rootx()+width)
                self.assertGreaterEqual(button.winfo_width(),button.winfo_reqwidth())

    def test_library_localizes_credits_without_changing_song_identity(self):
        song=Song('imas','Example','天海春香 (CV: 中村繪里子)','THE IDOLM@STER',self.a.lines)
        self.app.songs.append(song)
        self.app.open_editor();self.root.update()
        entry=self.app.song_picker['values'][-1]
        self.assertIn('아마미 하루카',entry)
        self.assertIn('나카무라 에리코',entry)
        self.assertEqual(song.artist,'天海春香 (CV: 中村繪里子)')
        self.assertEqual(self.app.picker_songs[-1].id,'imas')

    def test_large_display_scaling_keeps_toolbar_and_subtitle_status_readable(self):
        self.app.close()
        self.root=tk.Tk();self.root.withdraw()
        scale=float(self.root.tk.call('tk','scaling'))
        try:
            self.root.tk.call('tk','scaling',2.0)
            self.app=LyricsApp(self.root,self.store,start_services=False)
            self.playback(self.a,32)
            self.root.deiconify()
            for width in (560,900,1040):
                self.root.geometry(f'{width}x360+10+30')
                self.root.update();self.app.render();self.root.update()
                for button in self.app.tool_buttons:
                    self.assertGreaterEqual(button.winfo_width(),button.winfo_reqwidth())
                badge=self.app.playback_badge
                self.assertLessEqual(badge.bbox('lyric')[3],badge.winfo_height())
                self.assertLessEqual(badge.winfo_rooty()+badge.winfo_height(),self.root.winfo_rooty()+self.root.winfo_height())
        finally:
            self.root.tk.call('tk','scaling',scale)

    def test_header_hides_without_recovery_shortcut_and_restores_from_menu(self):
        self.app.chrome_available=False
        self.app.toggle_chrome()
        self.assertTrue(self.root.overrideredirect())
        item=next(item for item in self.app.menu_items() if item and item['label']=='헤더 숨기기 / 복원')
        item['command']()
        self.assertFalse(self.root.overrideredirect())

    def test_saved_subtitle_mode_restores_without_registered_recovery_key(self):
        self.app.settings['borderless']=True
        self.app.messages.put(('hotkeys',[7]))
        self.app.tick()
        self.assertTrue(self.root.overrideredirect())
        self.app.messages.put(('hotkeys',[]))
        self.app.tick()
        self.assertTrue(self.root.overrideredirect())

    def test_window_position_is_saved_for_next_start(self):
        self.root.deiconify()
        self.root.geometry('800x320+150+220')
        self.root.update()
        self.app.save_settings()
        rect=self.store.settings()['window_rect']
        self.assertEqual(rect[0],800)
        self.assertEqual(rect[2:],[150,220])

    def test_transparency_removes_background_and_preserves_text(self):
        self.playback(self.a)
        self.app.move(1)
        before=self.app.korean_label.cget('text')
        self.app.toggle_transparent()
        self.assertEqual(self.root.attributes('-transparentcolor'),'#010203')
        self.assertEqual(self.app.korean_label.cget('text'),before)
        self.assertEqual(len(self.app.korean_label.find_withtag('lyric')),9)
        self.app.toggle_transparent()
        self.assertEqual(self.root.attributes('-transparentcolor'),'#010203')
        self.assertEqual(self.app.backdrop.opacity,self.app.background_opacity.get())

    def test_long_outlined_lyrics_wrap_and_fit(self):
        self.root.deiconify()
        self.root.geometry('560x210+10+10')
        self.app.korean_label.configure(text='긴 가사 예시를 표시합니다. '*20)
        self.root.update()
        self.root.update()
        box=self.app.korean_label.bbox('lyric')
        self.assertLessEqual(box[3],self.app.korean_label.winfo_height())
        self.assertGreater(box[3]-box[1],60)
        self.assertLessEqual(box[2],self.app.korean_label.winfo_width())
        self.assertGreaterEqual(box[0],0)

    def test_long_lyrics_use_full_width_and_transitions_fit(self):
        lines=(Line('短い例','みじかいれい','짧은 예'),
               Line(('日本語の表示確認を続ける文章です。'*2)[:28],
                    ('ひらがなのながいひょうじかくにんです'*3)[:44],
                    ('긴 가사도 빈 공간에 자연스럽게 표시합니다. '*2)[:31]),
               Line('日本語の長い表示例 '*8,'にほんごのながいひょうじれい '*7,'더 긴 대사도 줄바꿈하여 표시합니다. '*5))
        song=Song('long','Rat A Tat!!!','ミリオンスターズ','Example',lines)
        self.app.songs.append(song)
        self.playback(song);self.app.follow.set(False)
        self.app.chrome_available=True;self.app.toggle_chrome()
        self.root.deiconify();self.root.geometry('1040x210+10+30')
        heights=[]
        for index in (0,1,2,0,1,0):
            self.app.manual_index=index;self.app.render();self.root.update()
            heights.append(self.root.winfo_height())
            title=self.app.track_title_label;content=self.app.lyric_content
            self.assertEqual((title.winfo_rootx()-self.root.winfo_rootx(),
                              title.winfo_rooty()-self.root.winfo_rooty()),(22,6))
            self.assertAlmostEqual(content.winfo_rootx()+content.winfo_width()/2,
                                   self.root.winfo_rootx()+self.root.winfo_width()/2,delta=1)
            previous_bottom=content.winfo_rooty()
            for label in (self.app.reading_label,self.app.original_label,self.app.korean_label):
                box=label.bbox('lyric')
                self.assertGreaterEqual(label.cget('wraplength'),980)
                self.assertGreaterEqual(box[0],0)
                self.assertLessEqual(box[2],label.winfo_width())
                self.assertGreaterEqual(label.winfo_rooty()+box[1],previous_bottom)
                previous_bottom=label.winfo_rooty()+box[3]
                self.assertLessEqual(previous_bottom,self.app.track_start_button.winfo_rooty())
                self.assertEqual(len(label.find_withtag('lyric')),9)
                if index==1:
                    line_height=int(self.root.tk.call('font','metrics',label.cget('font'),'-linespace'))
                    self.assertLessEqual(box[3]-box[1],line_height*2+8)
            self.assertLessEqual(previous_bottom,self.root.winfo_rooty()+self.root.winfo_height())
        self.assertEqual(len(set(heights)),1)

    def test_late_network_result_does_not_switch_song_or_overwrite_manual_edit(self):
        self.playback(self.b)
        self.receive_public(self.a,dict(times=[1,2,3,4],matched=4,source='LRCLIB',offset=0))
        self.assertEqual(self.app.song,self.b)
        self.assertEqual(self.store.timing(self.a)['times'],[1,2,3,4])
        self.app.stamp()
        self.receive_public(self.b,dict(times=[1,2,3,4],matched=4,source='LRCLIB',offset=0))
        self.assertEqual(self.store.timing(self.b)['times'],[1,10,None,None])

    def test_changed_library_rejects_stale_network_result(self):
        changed = Song('a',self.a.title,self.a.artist,self.a.album,self.a.lines[::-1])
        self.app.songs = [changed,self.b]
        self.receive_public(self.a,dict(times=[1,2,3,4],matched=4,source='LRCLIB',offset=0))
        self.assertEqual(self.store.timing(changed)['times'],[None]*4)

    def test_end_of_recording_does_not_overwrite_last_line(self):
        self.playback(self.a)
        self.app.open_editor()
        for index in range(4):
            self.playback(self.a,10+index*10)
            self.app.stamp()
            self.root.update()
        self.assertEqual(self.app.cursor,3)
        self.app.stamp()
        self.assertEqual(self.store.timing(self.a)['times'],[None,10,20,30])

    def test_end_key_uses_displayed_line_instead_of_stale_recording_cursor(self):
        self.store.save_timing(self.a,{'times':[10,20,30,40],'offset':0,'source':'음원 분석'})
        self.playback(self.a,25)
        self.assertEqual(self.app.displayed_index(),1)
        self.assertEqual(self.app.cursor,0)
        self.app.stamp()
        self.assertEqual(self.store.timing(self.a)['times'],[10,20,25,40])
        self.assertEqual(self.app.displayed_index(),2)

    def test_end_key_preserves_current_start_and_subtracts_global_offset(self):
        self.store.save_timing(self.a,{'times':[8,18,28,38],'offset':2,
                                     'origins':['manual','audio','audio','manual']})
        self.playback(self.a,21)
        self.app.manual_index=0;self.app.follow.set(False)
        self.app.stamp()
        self.assertEqual(self.store.timing(self.a)['times'],[8,19,28,38])
        self.assertEqual(self.store.timing(self.a)['offset'],2)
        self.assertEqual(self.app.displayed_index(),1)
        self.app.resume_follow()
        self.assertEqual(self.app.displayed_index(),1)
        self.playback(self.a,20.8)
        self.assertEqual(self.app.displayed_index(),0)
        self.playback(self.a,20.9)
        self.assertEqual(self.app.displayed_index(),1)
        self.assertEqual(self.app.displayed_index(advance=False),0)

    def test_conflicting_end_key_keeps_saved_manual_times_and_advances_selection(self):
        self.store.save_timing(self.a,{'times':[10,20,30,40],'offset':0,'source':'직접 기록'})
        self.playback(self.a,35)
        self.app.manual_index=0;self.app.follow.set(False);self.app.cursor=0
        before=self.store.timing(self.a)
        self.app.stamp()
        self.assertEqual(self.store.timing(self.a),before)
        self.assertEqual(self.app.displayed_index(),1)
        self.assertEqual(self.app.cursor,1)
        self.assertIn('충돌',self.app.error_notice[0])

    def test_small_overlay_and_editor_keep_controls_inside_window(self):
        self.playback(self.a)
        self.root.deiconify()
        self.root.geometry('560x210+10+10')
        self.app.move(1)
        self.app.open_editor()
        self.app.editor.geometry('560x540+10+10')
        self.root.update()
        self.root.update()
        for button in self.app.tool_buttons:
            self.assertGreater(button.winfo_width(),20)
            self.assertLessEqual(button.winfo_rootx()+button.winfo_width(), self.root.winfo_rootx()+self.root.winfo_width())
        self.assertLessEqual(self.app.status.winfo_y()+self.app.status.winfo_height(),self.root.winfo_height())
        self.assertGreater(self.app.tree.winfo_height(),30)
        self.assertLessEqual(self.app.time_entry.winfo_rooty()+self.app.time_entry.winfo_height(),self.app.editor.winfo_rooty()+self.app.editor.winfo_height())
        self.assertLessEqual(self.app.outro_entry.winfo_rooty()+self.app.outro_entry.winfo_height(),self.app.editor.winfo_rooty()+self.app.editor.winfo_height())

    def test_track_buttons_request_spotify_without_changing_lyric_selection_or_timing(self):
        self.playback(self.a)
        self.app.move(1)
        before=self.store.timing(self.a)
        selected=self.app.displayed_index()
        with patch.object(self.app.monitor,'request_skip',return_value=True) as skip:
            self.app.next_track_button.invoke()
            self.app.change_track('previous')
            skip.assert_called_once_with('next')
            self.assertEqual(self.app.next_track_button.cget('state'),'disabled')
            self.assertEqual(self.app.song,self.a)
            self.assertEqual(self.app.displayed_index(),selected)
            self.assertEqual(self.store.timing(self.a),before)
            self.app.messages.put(('media_control',('next',True,'다음 곡 이동 요청 완료')))
            self.app.tick()
            self.assertEqual(self.app.next_track_button.cget('state'),'normal')
            self.assertIsNone(self.app.pending_track_change)
            self.app.previous_track_button.invoke()
            self.assertEqual(skip.call_args.args,('previous',))

    def test_play_pause_keeps_lyrics_and_waits_for_observed_playback_state(self):
        self.playback(self.a);self.app.move(1)
        timing=dict(self.app.timing);index=self.app.displayed_index()
        with patch.object(self.app.monitor,'request_playback',return_value=True) as control:
            self.app.play_pause_button.invoke()
            control.assert_called_once_with('play')
            self.assertEqual(self.app.play_pause_button.cget('state'),'disabled')
            self.app.messages.put(('media_playback',('play',True,'재생 요청 완료')))
            self.app.tick()
            self.assertEqual(self.app.play_pause_button.cget('image'),str(self.app.transport_images['play']))
            self.assertEqual(self.app.timing,timing)
            self.assertEqual(self.app.displayed_index(),index)
            from dataclasses import replace
            self.app.set_playback(replace(self.app.playback,playing=True,captured=time.monotonic()))
            self.app.render()
            self.assertEqual(self.app.play_pause_button.cget('image'),str(self.app.transport_images['pause']))
            self.app.play_pause_button.invoke()
            self.assertEqual(control.call_args.args,('pause',))
            self.app.messages.put(('media_playback',('pause',False,'Spotify 일시정지 실패')))
            self.app.tick()
            self.assertIsNone(self.app.pending_playback)
            self.assertIn('실패',self.app.transport_notice.cget('text'))

    def test_play_pause_remains_visible_and_fits_when_header_hidden(self):
        self.playback(self.a)
        self.app.chrome_available=True;self.app.toggle_chrome();self.root.deiconify()
        for width in (560,1040):
            self.root.geometry(f'{width}x210+10+30');self.root.update();self.root.update()
            button=self.app.play_pause_button
            self.assertTrue(button.winfo_ismapped())
            self.assertLessEqual(button.winfo_rootx()+button.winfo_width(),self.app.playback_badge.winfo_rootx())
            self.assertGreater(button.winfo_rootx(),self.app.track_start_button.winfo_rootx()+self.app.track_start_button.winfo_width())

    def test_track_buttons_and_failure_remain_visible_when_header_is_hidden(self):
        self.root.deiconify();self.root.geometry('560x210+10+10')
        self.app.chrome_available=True;self.app.toggle_chrome()
        self.root.update();self.root.update()
        height=self.root.winfo_height()
        with patch.object(self.app.monitor,'request_skip',return_value=True):
            self.app.previous_track_button.invoke()
        self.app.messages.put(('media_control',('previous',False,'Spotify 데스크톱 앱을 실행해 주세요.')))
        self.app.tick();self.root.update();self.root.update()
        self.assertFalse(self.app.tools.winfo_ismapped())
        for button in (self.app.previous_track_button,self.app.next_track_button):
            self.assertTrue(button.winfo_ismapped())
            self.assertEqual(button.cget('state'),'normal')
            self.assertLessEqual(button.winfo_rootx()+button.winfo_width(),self.root.winfo_rootx()+self.root.winfo_width())
        self.assertTrue(self.app.transport_notice.winfo_ismapped())
        self.assertIn('Spotify',self.app.transport_notice.cget('text'))
        self.assertLessEqual(self.app.transport_notice.winfo_rooty()+self.app.transport_notice.winfo_height(),self.root.winfo_rooty()+self.root.winfo_height())
        self.assertEqual(self.root.winfo_height(),height)
        previous=self.app.previous_track_button;following=self.app.next_track_button
        self.assertGreaterEqual(previous.winfo_rootx(),self.root.winfo_rootx()+10)
        self.assertLessEqual(following.winfo_rootx()+following.winfo_width(),
                             self.root.winfo_rootx()+self.root.winfo_width()-10)
        self.assertLessEqual(previous.winfo_height(),24)

    def test_rejected_track_request_keeps_buttons_available(self):
        with patch.object(self.app.monitor,'request_skip',return_value=False):
            self.app.next_track_button.invoke()
        self.assertIsNone(self.app.pending_track_change)
        self.assertEqual(self.app.next_track_button.cget('state'),'normal')
        self.assertTrue(self.app.transport_notice.cget('text'))

    def test_single_arrows_seek_music_and_resume_lyrics_without_editing_timing(self):
        self.playback(self.a);self.app.move(1)
        selected=self.app.displayed_index();before=self.store.timing(self.a)
        with patch.object(self.app.monitor,'request_seek',return_value=True) as seek, \
                patch.object(self.app.monitor,'request_skip') as skip:
            self.app.track_end_button.invoke()
            seek.assert_called_once_with('last',self.app.playback.key)
            self.assertEqual(self.app.displayed_index(),selected)
            for button in (self.app.previous_track_button,self.app.track_start_button,
                           self.app.track_end_button,self.app.next_track_button):
                self.assertEqual(button.cget('state'),'disabled')
            self.app.track_start_button.invoke();self.app.next_track_button.invoke()
            seek.assert_called_once();skip.assert_not_called()
            self.app.messages.put(('media_seek',('last',True,'곡 끝부분 이동 완료')))
            self.app.tick()
            self.assertTrue(self.app.follow.get());self.assertIsNone(self.app.manual_index)
            self.assertIsNone(self.app.pending_seek)
            self.assertEqual(self.store.timing(self.a),before)
            self.app.track_start_button.invoke()
            self.assertEqual(seek.call_args.args,('first',self.app.playback.key))

    def test_seek_buttons_require_timeline_and_failure_keeps_follow_and_timing(self):
        for button in (self.app.track_start_button,self.app.track_end_button):
            self.assertEqual(button.cget('state'),'disabled')
        self.playback(self.a)
        before=self.store.timing(self.a);selected=self.app.displayed_index()
        self.app.move(1)
        self.assertTrue(self.app.follow.get())
        self.assertEqual(self.store.timing(self.a),before)
        self.assertEqual(self.app.displayed_index(),selected)
        with patch.object(self.app.monitor,'request_seek',return_value=False):
            self.app.track_start_button.invoke()
            self.assertIsNone(self.app.pending_seek)
            self.assertEqual(self.app.track_start_button.cget('state'),'normal')
        with patch.object(self.app.monitor,'request_seek',return_value=True):
            self.app.track_start_button.invoke()
            self.app.messages.put(('media_seek',('first',False,'재생 위치 이동 실패')))
            self.app.tick()
        self.assertEqual(self.app.displayed_index(),selected)
        self.assertTrue(self.app.follow.get())
        self.assertEqual(self.store.timing(self.a),before)
        self.assertIsNone(self.app.pending_seek)
        self.assertIn('실패',self.app.transport_notice.cget('text'))

    def test_old_seek_completion_does_not_change_new_track_follow_or_timing(self):
        self.playback(self.a)
        with patch.object(self.app.monitor,'request_seek',return_value=True):
            self.app.track_end_button.invoke()
        self.playback(self.b)
        selected=self.app.displayed_index();before=self.store.timing(self.b)
        self.app.move(1)
        self.assertTrue(self.app.follow.get())
        self.assertEqual(self.store.timing(self.b),before)
        self.assertEqual(self.app.displayed_index(),selected)
        self.app.messages.put(('media_seek',('last',True,'이전 곡의 완료 안내')))
        self.app.tick()
        self.assertEqual(self.app.song,self.b)
        self.assertTrue(self.app.follow.get())
        self.assertEqual(self.app.displayed_index(),selected)
        self.assertEqual(self.store.timing(self.b),before)
        self.assertEqual(self.app.transport_notice.cget('text'),'')
        self.assertEqual(self.app.track_end_button.cget('state'),'normal')

    def test_paired_arrows_fit_below_text_without_shifting_lyric_center(self):
        self.playback(self.a);self.app.move(1)
        self.app.chrome_available=True;self.app.toggle_chrome();self.root.deiconify()
        buttons=(self.app.previous_track_button,self.app.track_start_button,
                 self.app.track_end_button,self.app.next_track_button)
        self.assertEqual([button.cget('image') for button in buttons],
                         [str(self.app.transport_images[kind]) for kind in ('previous','first','last','next')])
        for width in (560,1040):
            self.root.geometry(f'{width}x210+10+30');self.root.update();self.root.update()
            frame=self.app.lyric_frame
            for button in buttons:
                self.assertTrue(button.winfo_ismapped())
                self.assertGreaterEqual(button.winfo_rooty(),frame.winfo_rooty()+frame.winfo_height())
                self.assertLessEqual(button.winfo_rooty()+button.winfo_height(),self.root.winfo_rooty()+self.root.winfo_height())
            self.assertLess(buttons[0].winfo_rootx()+buttons[0].winfo_width(),buttons[1].winfo_rootx())
            self.assertLess(buttons[2].winfo_rootx()+buttons[2].winfo_width(),buttons[3].winfo_rootx())
            lyrics=self.app.lyric_content
            self.assertAlmostEqual(lyrics.winfo_rootx()+lyrics.winfo_width()/2,
                                   self.root.winfo_rootx()+self.root.winfo_width()/2,delta=1)

    def test_four_character_voice_pairs_are_visible_together_at_regular_width(self):
        artists=('Clover','矢吹可奈 (CV.木戸衣吹)','北沢志保 (CV.雨宮 天)',
                 '箱崎星梨花 (CV.麻倉もも)','高坂海美 (CV.上田麗奈)')
        self.root.deiconify();self.root.geometry('1040x260+10+30')
        with patch.object(self.app.background_lyrics,'display_artists',return_value=artists):
            self.app.set_playback(Playback('Clover Days','Clover','THE IDOLM@STER',12,180,False,time.monotonic()))
            self.root.update();self.root.update()
            label=self.app.track_artist_label
            self.assertEqual(len(label.cget('text').splitlines()),1)
            self.assertFalse(label.overflow)
            self.assertEqual(len(label.cget('outline_spans')),5)
            self.assertLessEqual(len(label._layout_lines),3)
            self.assertEqual(self.app.lyric_content.winfo_width(),self.root.winfo_width()-44-2*LYRIC_GUTTER)
            self.assertLessEqual(label.winfo_rooty()+label.winfo_height(),self.app.lyric_content.winfo_rooty())

    def test_full_artist_list_wraps_without_ellipsis_in_narrow_overlay(self):
        artists=tuple('A Long Performer Name Number '+str(index) for index in range(8))
        playback=Playback('Long Song Title','Lead Artist','Album',12,180,False,time.monotonic())
        self.root.deiconify();self.root.geometry('560x210+10+10')
        with patch.object(self.app.background_lyrics,'display_artists',return_value=artists):
            self.app.set_playback(playback)
            self.root.update();self.root.update()
            label=self.app.track_artist_label
            self.assertEqual(self.app.track_title_label.cget('text'),'Long Song Title')
            self.assertEqual(label.cget('text'),', '.join(artists))
            self.assertNotIn('…',label.cget('text'))
            self.assertTrue(label.overflow)
            self.assertLess(label.yview()[1],1)
            self.assertGreater(label.winfo_height(),30)
            self.assertEqual(self.app.playback.key,playback.key)

    def test_large_ensemble_is_scrollable_without_displacing_lyrics(self):
        artists=tuple('A Very Long Performer Name '+str(index) for index in range(100))
        self.root.deiconify();self.root.geometry('560x210+10+10')
        with patch.object(self.app.background_lyrics,'display_artists',return_value=artists):
            self.app.set_playback(Playback('Song','Lead','Album',12,180,False,time.monotonic()))
            self.root.update();self.root.update()
            header=self.app.track_artist_label
            self.assertEqual(self.app.track_title_label.cget('text'),'Song')
            self.assertEqual(header.cget('text'),', '.join(artists))
            self.assertTrue(header.scrollbar.winfo_ismapped())
            self.assertLess(header.winfo_height(),100)
            self.assertLessEqual(self.app.korean_label.winfo_rooty()+self.app.korean_label.winfo_height(),
                                 self.root.winfo_rooty()+self.root.winfo_height())
            header.yview_moveto(1)
            self.assertGreaterEqual(header.yview()[1],.99)
            header.event_generate('<MouseWheel>',delta=120)
            self.assertLess(header.yview()[1],1)
            self.app.show_track_title_tip()
            self.assertIsNone(self.app.track_title_tip)

    def test_arriving_full_artist_metadata_refreshes_korean_header_without_track_change(self):
        from artist_names import ArtistNames
        self.root.deiconify();self.root.geometry('560x210+10+10')
        names=ArtistNames([{'name':'イルミネーションスターズ','ko':'일루미네이션 스타즈'},
                           {'name':'ノクチル','ko':'녹칠'}])
        artists=['イルミネーションスターズ']
        playback=Playback('Sparkle',artists[0],'THE IDOLM@STER SHINY COLORS',12,253.3,False,time.monotonic())
        with patch.object(self.app.background_lyrics,'display_artists',side_effect=lambda _:tuple(artists)), \
                patch('app.artist_label',side_effect=names.display), \
                patch('app.group_member_lines',return_value=()):
            self.app.set_playback(playback)
            artists.append('ノクチル')
            self.app.render()
            self.root.update();self.root.update()
            self.assertEqual(self.app.track_title_label.cget('text'),'Sparkle')
            self.assertEqual(self.app.track_artist_label.cget('text'),'일루미네이션 스타즈, 녹칠')
            self.assertEqual(self.app.playback,playback)
            self.app.show_track_title_tip()
            tooltip=self.app.track_title_tip.winfo_children()[0].cget('text')
            self.assertIn('일루미네이션 스타즈, 녹칠',tooltip)
            self.assertIn('イルミネーションスターズ, ノクチル',tooltip)

    def test_lyrics_stay_on_window_center_and_title_stays_at_top_left(self):
        self.playback(self.a)
        self.app.move(1)
        self.app.chrome_available=True;self.app.toggle_chrome()
        self.root.deiconify()
        for width in (560,819,820,1040,1440):
            self.root.geometry(f'{width}x210+10+10')
            self.root.update();self.root.update()
            title=self.app.track_title_label;artist=self.app.track_artist_label
            lyrics=self.app.lyric_content
            self.assertTrue(title.winfo_ismapped())
            self.assertTrue(artist.winfo_ismapped())
            window_center=self.root.winfo_rootx()+self.root.winfo_width()/2
            self.assertAlmostEqual(lyrics.winfo_rootx()+lyrics.winfo_width()/2,window_center,delta=1)
            self.assertEqual(title.winfo_rootx(),self.app.lyric_frame.winfo_rootx())
            self.assertEqual(title.winfo_rooty(),self.app.lyric_frame.winfo_rooty())
            self.assertLessEqual(artist.winfo_rooty()+artist.winfo_height(),lyrics.winfo_rooty())
            self.assertGreater(artist.winfo_rooty(),title.winfo_rooty())
            self.assertEqual(lyrics.winfo_width(),width-44-2*LYRIC_GUTTER)
            self.assertLessEqual(self.root.winfo_height(),290)
            self.app.update_track_title();self.root.update()
            self.assertEqual(title.grid_info()['row'],0)
            self.assertEqual(title.master,self.app.track_metadata)

    def test_title_does_not_shift_when_credits_arrive_or_lyrics_wrap(self):
        artists=['天海春香 (CV.中村繪里子)']
        self.root.deiconify();self.root.geometry('1040x260+10+30')
        with patch.object(self.app.background_lyrics,'display_artists',side_effect=lambda _:tuple(artists)):
            self.playback(self.a);self.app.move(1);self.root.update();self.root.update()
            title=self.app.track_title_label
            position=(title.winfo_rootx()-self.root.winfo_rootx(),title.winfo_rooty()-self.root.winfo_rooty())
            artists.extend(['如月千早 (CV.今井麻美)','星井美希 (CV.長谷川明子)'])
            self.app.render();self.root.update();self.root.update()
            self.app.original_label.configure(text='日本語の長い表示例 '*12)
            self.app.fit_height();self.root.update();self.root.update()
            self.assertEqual((title.winfo_rootx()-self.root.winfo_rootx(),title.winfo_rooty()-self.root.winfo_rooty()),position)
            lyrics=self.app.lyric_content
            self.assertAlmostEqual(lyrics.winfo_rootx()+lyrics.winfo_width()/2,
                                   self.root.winfo_rootx()+self.root.winfo_width()/2,delta=1)

    def test_character_colors_include_attached_voice_credit_and_clear_on_track_change(self):
        artists=('如月千早 (CV.今井麻美)','天海春香 (CV.中村繪里子)','765PRO ALLSTARS')
        self.root.deiconify();self.root.geometry('1040x260+10+10')
        with patch.object(self.app.background_lyrics,'display_artists',return_value=artists):
            self.app.set_playback(Playback('Color example',artists[0],'THE IDOLM@STER',12,180,False,time.monotonic()))
            self.root.update();self.root.update()
            label=self.app.track_artist_label;text=label.cget('text')
            spans=label.cget('outline_spans')
            self.assertEqual([text[start:end] for start,end,_ in spans],
                             ['키사라기 치하야 (CV.이마이 아사미)','아마미 하루카 (CV.나카무라 에리코)',
                              '765PRO ALLSTARS'])
            self.assertEqual(len({color for _,_,color in spans}),3)
            self.assertEqual(self.app.track_title_label.cget('outline_spans'),())
            for lyric in (self.app.track_title_label,self.app.original_label,self.app.reading_label,self.app.korean_label):
                items=lyric.find_withtag('lyric')
                if lyric.cget('text'):
                    self.assertEqual(len(items),9)
            self.assertEqual({lyric.itemcget(item,'fill') for item in items[:-1]},{'#090b10'})
        self.app.set_playback(Playback('Other','Unknown Artist','Album',12,180,False,time.monotonic()))
        self.root.update();self.root.update()
        self.assertEqual(self.app.track_artist_label.cget('outline_spans'),())

    def test_group_only_header_attaches_colored_members_without_changing_playback(self):
        playback=Playback('Clover Days','Clover','THE IDOLM@STER MILLION LIVE!',12,180,False,time.monotonic())
        before=(self.store.directory/'library.json').read_bytes()
        self.root.deiconify()
        with patch.object(self.app.background_lyrics,'display_artists',return_value=('Clover',)):
            self.app.set_playback(playback)
            for width in (1040,560):
                self.root.geometry(f'{width}x260+10+10');self.root.update();self.root.update()
                label=self.app.track_artist_label;text=label.cget('text')
                self.assertTrue(text.startswith('Clover\n'))
                self.assertEqual(text.count('Clover'),1)
                self.assertNotIn('멤버:',text)
                for name in ('야부키 카나','키도 이부키','키타자와 시호','아마미야 소라'):
                    self.assertIn(name,text)
                spans=label.cget('outline_spans')
                self.assertTrue(any('야부키 카나' in text[start:end] and '키도 이부키' in text[start:end]
                                    for start,end,_ in spans))
                self.assertLessEqual(label.winfo_rooty()+label.winfo_height(),self.app.lyric_content.winfo_rooty())
                self.assertEqual(self.app.playback,playback)
                self.assertEqual(self.app.displayed_artists(),('Clover',))
        self.app.set_playback(Playback('Other','Unknown Artist','Album',12,180,False,time.monotonic()))
        self.root.update();self.root.update()
        self.assertNotIn('Clover',self.app.track_artist_label.cget('text'))
        self.assertEqual(self.app.track_artist_label.cget('outline_spans'),())
        self.assertEqual((self.store.directory/'library.json').read_bytes(),before)

    def test_bare_character_gets_voice_but_contrast_songwriters_remain_original(self):
        artists=('初星学園','佐々木恵梨','鵜飼大幹','中村ヒロ','篠澤 広')
        playback=Playback('コントラスト','初星学園','THE IDOLM@STER',12,180,False,time.monotonic())
        with patch.object(self.app.background_lyrics,'display_artists',return_value=artists):
            self.app.set_playback(playback);self.root.update();self.root.update()
            text=self.app.track_artist_label.cget('text')
            self.assertIn('시노사와 히로 (CV. 카와무라 레이나)',text)
            for name in artists[1:4]:self.assertIn(name,text)
            self.assertEqual(text.count('CV.'),1)
            self.assertEqual(self.app.playback,playback)
            self.assertEqual(self.app.displayed_artists(),artists)
            self.assertTrue(any(text[start:end]=='시노사와 히로 (CV. 카와무라 레이나)'
                for start,end,_ in self.app.track_artist_label.cget('outline_spans')))

    def test_hatsuboshi_project_alone_does_not_add_student_roster_to_overlay(self):
        playback=Playback('Example','初星学園','THE IDOLM@STER',12,180,False,time.monotonic())
        artists=('初星学園',)
        with patch.object(self.app.background_lyrics,'display_artists',return_value=artists):
            self.app.set_playback(playback);self.root.update();self.root.update()
            self.assertEqual(self.app.track_artist_label.cget('text'),'하츠보시 학원')
            self.assertEqual(self.app.displayed_artists(),artists)
            self.assertEqual(self.app.playback,playback)

    def test_colored_artist_artwork_moves_with_the_application(self):
        self.root.deiconify();self.root.geometry('1040x260+10+10')
        with patch.object(self.app.background_lyrics,'display_artists',return_value=('如月千早',)):
            self.app.set_playback(Playback('Moving credit','如月千早','THE IDOLM@STER',12,180,
                                           False,time.monotonic()))
            self.root.update();self.root.update()
            label=self.app.track_artist_label;layer=label._artist_layer
            with patch.object(layer,'show',wraps=layer.show) as show:
                self.root.geometry('+55+65');self.root.update();self.root.update()
                self.assertTrue(show.called)
                self.assertEqual(show.call_args.args[1:3],(label.winfo_rootx(),label.winfo_rooty()))

    def test_empty_track_keeps_symmetric_gutter_and_error_keeps_metadata(self):
        self.root.deiconify();self.root.geometry('560x210+10+10')
        self.playback(self.a);self.root.update();self.root.update()
        self.app.error_notice=('연결 상태를 확인하세요.',time.monotonic()+30)
        self.app.last_render=None;self.app.render();self.root.update();self.root.update()
        self.assertTrue(self.app.track_metadata.winfo_ismapped())
        self.assertTrue(self.app.mode_label.winfo_ismapped())
        self.assertIn('연결 상태',self.app.mode_label.cget('text'))
        self.app.set_playback(Playback());self.root.update();self.root.update()
        self.assertFalse(self.app.track_metadata.winfo_ismapped())
        self.assertEqual(self.app.lyric_content.winfo_x(),LYRIC_GUTTER)
        self.assertEqual(self.app.lyric_content.winfo_width(),self.app.lyric_frame.winfo_width()-2*LYRIC_GUTTER)
        self.assertAlmostEqual(self.app.lyric_content.winfo_rootx()+self.app.lyric_content.winfo_width()/2,
                               self.root.winfo_rootx()+self.root.winfo_width()/2,delta=1)

    def test_track_notice_uses_lyric_width_without_growing_compact_overlay(self):
        self.playback(self.a);self.app.move(1)
        self.app.chrome_available=True;self.app.toggle_chrome()
        self.root.deiconify();self.root.geometry('560x210+10+10')
        self.root.update();self.root.update()
        height=self.root.winfo_height()
        self.app.show_track_notice('곡 이동 요청을 처리할 수 없습니다. 잠시 후 다시 눌러 주세요.')
        self.root.update();self.root.update()
        notice=self.app.transport_notice
        self.assertTrue(notice.winfo_ismapped())
        self.assertEqual(notice.winfo_width(),self.app.lyric_content.winfo_width())
        self.assertLessEqual(notice.winfo_rooty()+notice.winfo_height(),self.root.winfo_rooty()+height)
        self.assertEqual(self.root.winfo_height(),height)
        self.app.track_notice_until=time.monotonic()-1
        self.app.tick();self.root.update()
        self.assertFalse(notice.winfo_ismapped())
        self.assertEqual(self.app.transport_notice.cget('text'),'')
        self.app.show_track_notice('');self.root.update()
        self.assertFalse(notice.winfo_ismapped())


if __name__ == '__main__':
    unittest.main()
