import queue
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app import LyricsApp
from auto_audio import AutoAnalyzer
from live_lyrics import LiveLyrics
from live_timing import LiveTimingStore
from lyrics import Line, Song
from media import Playback
from storage import Store, atomic_json


class BeforeWindow(Exception):pass


class LiveCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store=Store(self.temp.name)
        self.live=LiveLyrics(LiveTimingStore(self.store.directory))
        self.playback=Playback('Synthetic','Artist','Album',duration=180)
        self.lines=(Line('窓を開ける','','번역'),Line('花を描く','',''))
        self.song=self.live.receive(self.playback,None,self.lines)
        self.auto=AutoAnalyzer('.',self.store,queue.Queue())
        self.addCleanup(self.auto.close)
        self.app=SimpleNamespace(audio_window=None,audio_job=None,locked=False,song=self.song,
            live_lyrics=self.live,store=self.store,auto_analyzer=self.auto,audio_manual_progress=None,
            audio_status=Mock(),root=None,audio_draft_readonly=False)
        self.app.audio_draft_path=lambda song:LyricsApp.audio_draft_path(self.app,song)

    def save_candidate(self,**changes):
        candidate={'times':[10,20],'fingerprint':self.auto.signature(self.song,True),**changes}
        path=self.auto.draft_path(self.song,True)
        atomic_json(path,candidate)
        return path,candidate

    def load(self):
        with patch('app.tk.Toplevel',side_effect=BeforeWindow),self.assertRaises(BeforeWindow):
            LyricsApp.open_audio(self.app)

    def test_live_candidate_uses_hashed_path_and_original_signature_without_writes(self):
        path,candidate=self.save_candidate()
        before=path.read_bytes()
        self.load()
        self.assertEqual(self.app.audio_draft,candidate)
        self.assertTrue(self.app.audio_draft_readonly)
        self.assertEqual(path.read_bytes(),before)
        self.assertFalse(self.app.audio_draft_path(self.song).exists())
        self.assertFalse(self.live.timing_store.path(self.song).exists())

    def test_readonly_cannot_be_applied_after_live_context_is_cleared(self):
        self.save_candidate();self.load()
        self.live.clear()
        self.app.songs=[self.song]
        with patch.object(self.store,'save_timing') as save,self.assertRaisesRegex(ValueError,'보기 전용'):
            LyricsApp.apply_audio(self.app)
        save.assert_not_called()

    def test_translation_only_updates_still_allow_original_candidate_preview(self):
        _,candidate=self.save_candidate()
        self.app.song=self.live.receive(self.playback,None,
            tuple(Line(line.original,'발음','새 번역') for line in self.lines))
        self.load()
        self.assertEqual(self.app.audio_draft,candidate)

    def test_different_original_rows_do_not_receive_saved_candidate_times(self):
        self.save_candidate()
        self.app.song=self.live.receive(self.playback,None,
            tuple(Line('別の長い原文'+str(index),'','') for index in range(2)))
        self.load()
        self.assertIsNone(self.app.audio_draft)

    def test_invalid_live_times_are_rejected_without_changing_candidate(self):
        path,_=self.save_candidate(times=[10,-1])
        before=path.read_bytes()
        with self.assertRaises(ValueError):LyricsApp.open_audio(self.app)
        self.assertEqual(path.read_bytes(),before)
        self.assertIsNone(self.app.audio_draft)

    def test_malformed_candidate_is_rejected_without_window_or_file_write(self):
        for value in ([1,2],{'fingerprint':self.auto.signature(self.song,True)}):
            with self.subTest(value=value):
                path=self.auto.draft_path(self.song,True)
                atomic_json(path,value);before=path.read_bytes()
                with patch('app.tk.Toplevel') as window,self.assertRaises(ValueError):
                    LyricsApp.open_audio(self.app)
                window.assert_not_called()
                self.assertEqual(path.read_bytes(),before)

    def test_library_candidate_reopen_resets_readonly_and_keeps_original_path(self):
        self.save_candidate();self.load()
        library=Song('library','Library','Artist','Album',self.lines)
        candidate={'times':[5,15],'fingerprint':library.fingerprint,'duration':180}
        atomic_json(self.app.audio_draft_path(library),candidate)
        self.app.song=library
        self.load()
        self.assertFalse(self.app.audio_draft_readonly)
        self.assertEqual(self.app.audio_draft,candidate)


if __name__=='__main__':unittest.main()
