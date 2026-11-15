from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from app import LyricsApp
from audio_sync import capture_problem, match_words, merge_candidates
from lyrics import Line, Song
from media import Playback
from storage import Store


def word(text, start, end=None, probability=.95):
    return dict(word=text, start=start, end=end or start+1, probability=probability)


class AudioSyncTests(unittest.TestCase):
    def test_split_words_repeated_chorus_and_audio_start(self):
        lines = [Line(t,'','') for t in ['窓を開ける','花を描く','夜が来る','花を描く']]
        words = [word('窓を',1),word('開ける',2),word('花を描く',5),
                 word('夜が来る',10),word('花を',14),word('描く',15)]
        result = match_words(lines,words,2,100)
        self.assertEqual(result['times'],[3,7,12,16])

    def test_unrelated_uncertain_and_short_lines_are_not_timed(self):
        lines = [Line(t,'','') for t in ['新しい空を見上げる','はい','窓を開ける']]
        words = [word('別の話です',1),word('はい',5),word('窓を開ける',7,probability=.1)]
        self.assertEqual(match_words(lines,words)['times'],[None]*3)

    def test_missing_line_onset_is_not_guessed(self):
        lines=[Line('明るい空を見上げる','','')]
        self.assertEqual(match_words(lines,[word('空を見上げる',10)])['times'],[None])

    def test_same_word_timestamp_cannot_create_duplicate_line_starts(self):
        lines=[Line('窓を開ける','',''),Line('花を描く','','')]
        self.assertEqual(match_words(lines,[word('窓を開ける花を描く',10)])['times'],[10,None])

    def test_long_intervening_transcript_cannot_join_remote_syllables(self):
        lines=[Line('窓を開ける','','')]
        words=[word('窓を',1),word('これはとても長い別の会話です',2),word('開ける',30)]
        self.assertEqual(match_words(lines,words)['times'],[None])

    def test_merge_preserves_manual_anchors_and_positive_offset(self):
        current={'times':[10,None,30,None], 'offset':2, 'custom':'retained'}
        result,count=merge_candidates(current,[9,22,40,50])
        self.assertEqual(result['times'],[10,20,30,48])
        self.assertEqual(current['times'],[10,None,30,None])
        self.assertEqual((result['offset'],result['custom'],count),(2,'retained',2))

    def test_merge_rejects_conflicts_and_negative_adjusted_time(self):
        current={'times':[None,10,None,20], 'offset':5}
        self.assertEqual(merge_candidates(current,[2,9,40,50])[0]['times'],current['times'])

    def test_capture_aborts_pause_seek_new_song_stale_snapshot(self):
        now=time.monotonic()
        valid=Playback('Example','A','B',12,180,True,now)
        self.assertEqual(capture_problem(valid,valid.key,12,now),'')
        for snapshot,expected,moment in [
            (Playback('Example','A','B',12,180,False,now),12,now),
            (valid,25,now), (valid,12,now+4),
            (Playback('Other','A','B',12,180,True,now),12,now),
            (Playback(error='disconnected'),12,now)]:
            self.assertTrue(capture_problem(snapshot,valid.key,expected,moment))


class AudioUITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(self.temp.name)
        self.song=Song('a','Example','Artist','Album',tuple(Line(x,'','예시') for x in ['窓を開ける','花を描く','夜が来る']))
        self.other=Song('b','Other','Artist','Album',self.song.lines)
        self.store.save_library([self.song,self.other])
        self.store.save_settings({'online':False,'transparent':False,'borderless':False})
        self.root=tk.Tk()
        self.root.withdraw()
        self.app=LyricsApp(self.root,self.store,start_services=False)
        self.app.select_song(self.song)

    def tearDown(self):
        self.app.audio_job=None
        self.app.close()
        self.temp.cleanup()

    def result(self,times=None):
        job=SimpleNamespace(song=self.song,cancelled=threading.Event())
        job.recording_context=self.app.recording_contexts.capture(self.song,
            Playback(self.song.title,self.song.artist,self.song.album,duration=180))
        self.app.audio_job=job
        self.app.receive_audio(job,{'kind':'result','result':{'times':times or [10,20,30], 'duration':180}})
        self.app.receive_audio(job,{'kind':'finished'})
        return job

    def receive_public(self,result):
        context=self.app.recording_contexts.capture(self.song,
            Playback(self.song.title,self.song.artist,self.song.album,duration=180))
        self.app.query_running[self.song.id]=context
        self.app.receive_timing(self.song,result,'',context)

    def test_result_is_preview_only_and_preserves_existing_timing(self):
        self.app.open_audio()
        self.store.save_timing(self.song,{'times':[8,None,None],'offset':2})
        self.result()
        self.assertEqual(self.store.timing(self.song)['times'],[8,None,None])
        self.app.apply_audio()
        self.assertEqual(self.store.timing(self.song)['times'],[8,18,28])
        self.assertEqual(self.store.timing(self.song)['offset'],2)

    def test_late_result_does_not_switch_current_song(self):
        self.app.open_audio()
        self.app.select_song(self.other)
        self.result()
        self.app.apply_audio()
        self.assertEqual(self.app.song,self.other)
        self.assertEqual(self.store.timing(self.other)['times'],[None]*3)
        self.assertEqual(self.store.timing(self.song)['times'],[10,20,30])

    def test_cancelled_result_cannot_save_a_draft(self):
        job=SimpleNamespace(song=self.song,cancelled=threading.Event())
        job.cancelled.set()
        self.app.audio_job=job
        self.app.receive_audio(job,{'kind':'result','result':{'times':[10,20,30]}})
        self.assertFalse(self.app.audio_draft_path(self.song).exists())

    def test_changed_lyrics_reject_stale_result(self):
        changed=Song('a','Example','Artist','Album',self.song.lines[::-1])
        self.app.songs=[changed]
        self.result()
        self.assertFalse(self.app.audio_draft_path(self.song).exists())

    def test_preview_survives_window_reopen(self):
        self.app.open_audio()
        self.result()
        self.app.close_audio()
        self.app.open_audio()
        self.assertEqual(self.app.audio_draft['times'],[10,20,30])

    def test_existing_manual_edit_during_analysis_is_preserved(self):
        self.app.open_audio()
        self.result()
        self.store.save_timing(self.song,{'times':[None,21,None],'offset':0})
        self.app.apply_audio()
        self.assertEqual(self.store.timing(self.song)['times'],[10,21,30])

    def test_public_timing_can_replace_audio_but_never_a_late_manual_edit(self):
        self.store.save_timing(self.song,{'times':[2,None,70],'offset':0,'source':'음원 분석'})
        self.receive_public({'times':[10,20,30],'offset':0,'matched':3,'source':'LRCLIB'})
        self.assertEqual(self.store.timing(self.song)['times'],[10,20,30])
        self.assertTrue((self.store.directory/'audio_backups'/'a.before-public.json').exists())
        self.store.save_timing(self.song,{'times':[9,None,None],'offset':0,'source':'직접 기록'})
        self.receive_public({'times':[10,20,30],'offset':0,'matched':3,'source':'LRCLIB'})
        self.assertEqual(self.store.timing(self.song)['times'],[9,20,30])

    def test_small_dialog_keeps_all_buttons_and_table_visible(self):
        self.app.open_audio()
        self.app.audio_window.geometry('560x460+10+10')
        self.root.update()
        self.root.update()
        window=self.app.audio_window
        for widget in [self.app.audio_start_button,self.app.audio_cancel_button,self.app.audio_apply_button]:
            self.assertTrue(widget.winfo_ismapped())
            self.assertLessEqual(widget.winfo_rootx()+widget.winfo_width(),window.winfo_rootx()+window.winfo_width())
            self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(),window.winfo_rooty()+window.winfo_height())
        self.assertGreater(self.app.audio_queue_tree.winfo_height(),60)
        self.app.audio_tabs.select(1);self.root.update()
        self.assertGreater(self.app.audio_tree.winfo_height(),60)

    def test_closing_analysis_window_cancels_capture(self):
        self.app.open_audio()
        with patch('audio_job.AudioJob') as factory:
            job=factory.return_value
            self.app.audio_job=job
            self.app.close_audio()
            job.cancel.assert_called_once()

    def test_start_requires_bound_playback(self):
        self.app.open_audio()
        with self.assertRaises(ValueError):
            self.app.start_audio()

    def test_developer_option_toggles_and_persists_automatic_analysis_without_playback(self):
        self.app.open_developer_mode()
        button=self.app.manual_sync_dialog.auto_audio_button
        with patch('auto_audio.ready'):
            button.invoke()
        self.assertTrue(self.app.auto_analyzer.enabled)
        self.assertTrue(self.store.settings()['auto_audio'])
        button.invoke()
        self.assertFalse(self.app.auto_analyzer.enabled)
        self.assertFalse(self.store.settings()['auto_audio'])


if __name__=='__main__':
    unittest.main()
