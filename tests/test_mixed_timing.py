import tempfile,threading,time,tkinter as tk,unittest
from types import SimpleNamespace
from app import LyricsApp
from audio_sync import merge_candidates
from lyrics import Song,Line
from media import Playback
from storage import Store,atomic_json
from sync import edit_manual_time,restore_auto_time,blend_live_index
from timing_quality import saved_rows
from live_timing import LiveTimingStore


class MixedTimingTests(unittest.TestCase):
    def test_one_correction_and_restore_keep_the_other_automatic_rows(self):
        original={'times':[10,20,30,40],'offset':2,'source':'음원 분석'}
        edited=edit_manual_time(original,1,23)
        self.assertEqual(edited['times'],[10,23,30,40])
        self.assertEqual(edited['origins'],['audio','manual','audio','audio'])
        self.assertEqual(edited['automatic_times'],original['times'])
        self.assertEqual(restore_auto_time(edited,1)['times'],original['times'])
        self.assertEqual(edited['offset'],2)

    def test_reanalysis_preserves_correction_and_updates_its_automatic_alternative(self):
        edited=edit_manual_time({'times':[10,20,30],'source':'음원 분석','offset':2},1,22)
        updated,_=merge_candidates(edited,[13,23,33],replace_audio=True)
        self.assertEqual(updated['times'],[11,22,31])
        self.assertEqual(restore_auto_time(updated,1)['times'],[11,21,31])

    def test_corrected_row_owns_only_its_interval_and_automatic_follow_continues(self):
        timing={'times':[10,25,30,40],'origins':['audio','manual','audio','audio'],'offset':0}
        for position,candidate,expected in [(24,2,0),(25,0,1),(29,3,1),(30,2,2),(35,3,3)]:
            self.assertEqual(blend_live_index(timing,timing['times'],position,candidate),expected)
        self.assertEqual(saved_rows(timing),[False,True,False,False])

    def test_unknown_next_start_does_not_lock_live_follow_at_the_manual_row(self):
        timing={'times':[None,20,None,50],'origins':[None,'manual',None,'manual'],'offset':2}
        self.assertEqual(blend_live_index(timing,timing['times'],24,2),2)
        self.assertEqual(blend_live_index(timing,timing['times'],21,2),0)
        self.assertEqual(blend_live_index(timing,timing['times'],52,2),3)

    def test_conflicting_automatic_restore_does_not_overwrite_neighboring_manual_rows(self):
        timing={'times':[10,25,30],'origins':['manual','manual','manual'],'offset':0,
                'automatic_times':[5,35,40]}
        restored=restore_auto_time(timing,1)
        self.assertEqual(restored['times'],[10,None,30])
        self.assertEqual(restored['origins'],['manual',None,'manual'])

    def test_memory_only_song_keeps_numeric_mixed_timing_provenance_on_reload(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            song=Song('memory','Example','','',tuple(Line(str(i),'','') for i in range(3)))
            store=LiveTimingStore(Path(folder))
            mixed=edit_manual_time({'times':[10,20,30],'offset':0,'source':'음원 분석'},1,22)
            store.save(song,mixed);loaded=store.load(song)
            self.assertEqual(loaded['origins'],['audio','manual','audio'])
            self.assertEqual(restore_auto_time(loaded,1)['times'],[10,20,30])


class MixedTimingUITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(self.temp.name)
        self.song=Song('mixed','Mixed','Artist','Album',tuple(Line(f'日本語の例{i}','','예시') for i in range(4)))
        self.store.save_library([self.song]);self.store.save_settings({'online':False,'auto_audio':False,'use_estimate':False,'borderless':False})
        self.root=tk.Tk();self.root.withdraw();self.app=LyricsApp(self.root,self.store,start_services=False)
        self.app.set_playback(Playback('Mixed','Artist','Album',10,180,False,time.monotonic()))
        self.app.spotify_sample_after=0
    def tearDown(self):self.app.close();self.temp.cleanup()
    def position(self,value):self.app.playback=Playback('Mixed','Artist','Album',value,180,False,time.monotonic())

    def test_sparse_manual_record_allows_spotify_and_live_audio_to_follow_other_rows(self):
        self.app.timing={'times':[None,20,None,None],'offset':0,'source':'직접 기록'}
        self.assertFalse(self.app.has_saved_sync())
        originals=[line.original for line in self.song.lines]
        self.position(10);self.app.receive_spotify_active(self.app.playback.key,originals,0)
        self.assertTrue(self.app.following_spotify());self.assertEqual(self.app.displayed_index(),0)
        self.position(22);self.app.receive_spotify_active(self.app.playback.key,originals,2)
        self.assertEqual(self.app.displayed_index(),2)
        job=SimpleNamespace(song=self.song,playback_key=self.app.playback.key,cancelled=threading.Event())
        self.app.receive_realtime(job,{'kind':'realtime','observed_at':time.monotonic(),'position':22,'match':{'index':2}})
        self.assertTrue(self.app.following_audio());self.assertEqual(self.app.displayed_index(),2)

    def test_align_now_changes_only_selected_row_and_preserves_global_offset(self):
        self.app.timing={'times':[10,20,30,40],'offset':2,'source':'음원 분석'}
        self.app.manual_index=1;self.app.follow.set(False);self.position(25)
        self.app.align_now()
        self.assertEqual(self.store.timing(self.song)['times'],[10,23,30,40])
        self.assertEqual(self.store.timing(self.song)['offset'],2)
        self.assertTrue(self.app.follow.get());self.assertEqual(self.app.displayed_index(),1)

    def test_restore_button_recovers_saved_automatic_candidate_and_marks_row(self):
        self.app.timing={'times':[10,25,30,40],'offset':0,'origins':['manual','manual','audio','audio']}
        atomic_json(self.store.directory/'audio_drafts'/'mixed.json',{'times':[9,20,30,40],'fingerprint':self.song.fingerprint})
        self.app.open_editor();self.root.update();self.app.select_editor_row(1);self.root.update()
        self.app.restore_selected_auto();self.root.update()
        self.assertEqual(self.app.timing['times'],[10,20,30,40])
        self.assertEqual(self.app.tree.item('0')['values'][3],'수동')
        self.assertEqual(self.app.tree.item('1')['values'][3],'자동')
        self.assertEqual(self.store.timing(self.song)['origins'],['manual','audio','audio','audio'])

    def test_small_editor_keeps_restore_control_inside_the_window(self):
        self.app.open_editor();self.app.editor.geometry('560x660+40+40')
        self.root.update();self.root.update()
        button=self.app.restore_auto_button;window=self.app.editor
        self.assertTrue(button.winfo_ismapped())
        self.assertLessEqual(button.winfo_rootx()+button.winfo_width(),window.winfo_rootx()+window.winfo_width())
        self.assertLessEqual(button.winfo_rooty()+button.winfo_height(),window.winfo_rooty()+window.winfo_height())

    def test_remembered_spotify_times_fill_other_rows_without_replacing_manual_correction(self):
        self.app.timing={'times':[10,25,None,40],'offset':2,'origins':['audio','manual',None,'audio']}
        self.app.native_timing.select(self.song,180);self.app.native_timing.times=[11,20,35,None]
        self.assertEqual(self.app.effective_times()[0],[9,25,33,40])
        self.assertEqual(self.app.timing['times'],[10,25,None,40])
