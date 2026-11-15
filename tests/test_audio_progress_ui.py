import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from app import LyricsApp
from audio_progress import AudioProgress
from lyrics import Line,Song
from media import Playback
from storage import Store


class FakeJob:
    def __init__(self,base,song,playback,messages,**options):
        self.song=song
        self.cancelled=threading.Event()
    def cancel(self):self.cancelled.set()
    def close(self):self.cancel()


class ProgressUITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(self.temp.name)
        self.store.save_settings({'online':False,'transparent':False,'borderless':False})
        self.songs=[Song(str(i),'Example '+str(i),'Artist','Album',(Line('テスト','','예시'),)*3) for i in range(3)]
        self.store.save_library(self.songs)
        self.root=tk.Tk();self.root.withdraw()
        self.app=LyricsApp(self.root,self.store,start_services=False)
        self.app.auto_analyzer.enabled=True
        self.app.auto_analyzer.job_factory=FakeJob

    def tearDown(self):
        self.app.close();self.temp.cleanup()

    def automatic(self,index,phase,completed=None):
        song=self.songs[index]
        playback=Playback(song.title,song.artist,song.album,0,100,True,time.monotonic())
        self.app.playback=playback
        self.app.auto_analyzer.observe(song,playback)
        job=list(self.app.auto_analyzer.jobs)[-1]
        self.app.receive_auto_audio(job,{'kind':'progress','state':phase,'text':phase,
                                      'completed':completed,'total':100})
        return job

    def values(self,job):
        return self.app.audio_queue_tree.item(str(id(job)),'values')

    def test_skipped_waiting_request_disappears_without_hiding_completed_capture_work(self):
        old=self.automatic(0,'waiting')
        current=self.automatic(1,'analyzing',20)
        self.app.open_audio();self.root.update()
        self.assertTrue(old.cancelled.is_set())
        self.assertNotIn(str(id(old)),self.app.audio_queue_tree.get_children())
        self.assertIn(str(id(current)),self.app.audio_queue_tree.get_children())
        self.app.receive_auto_audio(old,{'kind':'finished'})
        self.assertFalse(self.app.auto_analyzer.activity_snapshot()['recent'])
        self.assertIn(str(id(current)),self.app.audio_queue_tree.get_children())

    def test_open_and_reopen_shows_active_job_without_cancelling_auto(self):
        job=self.automatic(0,'analyzing',32)
        self.app.open_audio();self.root.update()
        self.assertNotIn('분석 대기를 누른',self.app.audio_status.get())
        self.assertEqual(self.values(job)[2],'32%')
        self.app.close_audio()
        self.assertFalse(job.cancelled.is_set())
        self.app.receive_auto_audio(job,{'kind':'progress','state':'analyzing','text':'alignment','completed':57,'total':100})
        self.app.open_audio();self.root.update()
        self.assertEqual(self.values(job)[2],'57%')

    def test_queue_lists_previous_analysis_and_current_collection(self):
        old=self.automatic(0,'analyzing',40)
        queued=self.automatic(1,'queued')
        current=self.automatic(2,'recording',12)
        self.app.open_audio();self.root.update()
        self.assertEqual(len(self.app.audio_queue_tree.get_children()),3)
        self.assertIn('음원 분석',self.values(old)[1])
        self.assertIn('순서 대기',self.values(queued)[1])
        self.assertIn('소리 수집',self.values(current)[1])
        self.assertEqual(self.app.audio_queue_tree.selection(),(str(id(current)),))
        self.app.audio_queue_tree.selection_set(str(id(queued)))
        self.app.refresh_audio_progress()
        self.assertIn('대기',self.app.audio_progress_detail.get('1.0','end'))

    def test_error_survives_finished_and_reopen_without_false_waiting(self):
        job=self.automatic(0,'recording',10)
        self.app.receive_auto_audio(job,{'kind':'error','text':'수집 중단 이유'})
        self.app.receive_auto_audio(job,{'kind':'finished'})
        self.app.open_audio();self.root.update()
        row=self.app.audio_queue_tree.item('auto-last','values')
        self.assertIn('오류',row[1])
        self.assertIn('수집 중단 이유',self.app.audio_progress_detail.get('1.0','end'))

    def test_half_second_refresh_does_not_rebuild_candidates_or_start_jobs(self):
        job=self.automatic(0,'analyzing',20)
        self.app.open_audio();self.root.update()
        self.app.audio_progress_updated=0
        with patch.object(self.app,'refresh_audio') as candidates, patch.object(self.app.auto_analyzer,'job_factory') as factory:
            self.app.tick()
            candidates.assert_not_called();factory.assert_not_called()
        self.assertGreater(self.app.audio_progress_updated,0)
        self.assertEqual(self.values(job)[2],'20%')

    def test_manual_progress_and_cancel_do_not_affect_automatic_job(self):
        automatic=self.automatic(0,'analyzing',30)
        manual=FakeJob('.',self.songs[1],None,None)
        self.app.audio_job=manual
        self.app.audio_manual_progress=AudioProgress(manual.song.title,mode='직접')
        self.app.receive_audio(manual,{'kind':'progress','state':'recording','text':'capture','completed':22,'total':100})
        self.app.open_audio();self.root.update()
        self.assertEqual(self.app.audio_queue_tree.selection(),('manual',))
        self.app.cancel_audio()
        self.assertTrue(manual.cancelled.is_set());self.assertFalse(automatic.cancelled.is_set())
        self.assertIn('취소 중',self.app.audio_queue_tree.item('manual','values')[1])
        self.app.receive_audio(manual,{'kind':'finished'})
        self.assertIn('취소됨',self.app.audio_queue_tree.item('manual','values')[1])

    def test_minimum_window_fits_both_tabs_and_long_status(self):
        self.automatic(0,'analyzing',30)
        self.app.open_audio()
        self.app.audio_status.set('아주 긴 상태 안내 '+('긴 제목과 작업 설명 '*40))
        window=self.app.audio_window;window.geometry('560x460+20+20')
        for tab,table in ((0,self.app.audio_queue_tree),(1,self.app.audio_tree)):
            self.app.audio_tabs.select(tab);self.root.update();self.root.update()
            self.assertGreater(table.winfo_height(),60)
            for widget in (self.app.audio_start_button,self.app.audio_cancel_button,self.app.audio_apply_button,table):
                self.assertTrue(widget.winfo_ismapped())
                self.assertGreaterEqual(widget.winfo_rootx(),window.winfo_rootx())
                self.assertGreaterEqual(widget.winfo_rooty(),window.winfo_rooty())
                self.assertLessEqual(widget.winfo_rootx()+widget.winfo_width(),window.winfo_rootx()+window.winfo_width())
                self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(),window.winfo_rooty()+window.winfo_height())

    def test_playlist_analysis_reuses_audio_window_and_its_own_controls(self):
        track_id = 'a' * 22
        item = {'track': {'id': track_id, 'title': 'Example', 'artists': ['Artist']},
                'position': 1, 'reason': '싱크 없음'}
        self.app.open_audio()
        window = self.app.audio_window
        self.assertIsNone(self.app.audio_playlist_panel)
        with patch('playlist_analysis_panel.MissingAudioCollector.inventory', return_value=[item]):
            self.app.open_audio(playlist=True)
            panel = self.app.audio_playlist_panel
            deadline = time.monotonic() + 2
            while panel.loading and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.01)
        self.assertIs(self.app.audio_window, window)
        self.assertEqual(self.app.audio_tabs.select(), str(self.app.audio_playlist_tab))
        window.geometry('560x460+20+20')
        self.root.update()
        self.assertGreater(panel.table.winfo_height(), 60)
        self.assertEqual(panel.table.get_children(), (track_id,))
        self.assertTrue(panel.start_button.instate(('!disabled',)))
        self.assertFalse(self.app.audio_start_button.winfo_ismapped())
        panel.collector.review_needed.add(track_id)
        panel.refresh()
        self.assertEqual(panel.table.set(track_id, 'state'), '싱크 검토 필요')
        self.assertTrue(panel.start_button.instate(('disabled',)))
        self.app.audio_tabs.select(0)
        self.root.update()
        self.assertTrue(self.app.audio_start_button.winfo_ismapped())
        self.app.open_audio(playlist=True)
        self.assertIs(self.app.audio_playlist_panel, panel)
        self.app.close_audio()
        self.assertIsNone(self.app.audio_playlist_panel)

    def test_playlist_result_validation_precedes_writing_sync(self):
        job = FakeJob('.', self.songs[0], None, None)
        job.playlist_analysis = True
        collector = Mock(accepts_result=Mock(return_value=False))
        with (patch.object(self.app, 'missing_audio_collector', collector),
              patch.object(self.app.auto_analyzer, 'receive') as receive):
            self.app._receive_analysis_job(job, {'kind': 'result'}, self.songs)
        collector.accepts_result.assert_called_once_with(job)
        self.assertEqual(receive.call_args.args[1]['kind'], 'error')
        self.assertEqual(collector.receive.call_args.args[1], receive.call_args.args[1])


if __name__=='__main__':unittest.main()
