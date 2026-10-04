"""Confirmed lyric replacement/removal through the real, offline Tk application."""
from copy import deepcopy
from dataclasses import replace
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from app import LyricsApp
from lyrics import Line, Song
from media import Playback
from storage import Store
from tests.test_auto_audio import FakeJob


class LyricEditUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.original = Song('saved', 'Edit Fixture', 'Singer', 'Album',
                             (Line('青い空を見る', 'あおいそらをみる', '이전 하늘'),
                              Line('風の音を聞く', 'かぜのおとをきく', '이전 바람')))
        self.other = Song('other', 'Other Fixture', 'Other Singer', 'Other Album',
                         (Line('花を描く', 'はなをえがく', '꽃을 그리다'),))
        self.playback = Playback(self.original.title, self.original.artist,
                                 self.original.album, 10, 120, False, time.monotonic())
        self.store.save_library([self.original, self.other])
        self.store.save_settings({'online': False, 'auto_audio': False, 'web_auto': False,
                                  'borderless': False, 'transparent': False, 'use_estimate': False,
                                  'reader_mode': 'chrome', 'links': {self.playback.key: self.original.id}})
        self.store.save_timing(self.original, {'times': [12, 25], 'offset': .5,
                                              'source': 'manual', 'origins': ['manual', 'manual']})
        self.store.save_timing(self.other, {'times': [4], 'offset': 0})
        self.network = patch('web_lyrics.urlopen', side_effect=AssertionError('network is disabled'))
        self.network.start()
        self.start_app()

    def start_app(self):
        self.root = tk.Tk(); self.root.withdraw()
        self.app = LyricsApp(self.root, self.store, start_services=False)
        self.app.set_playback(self.playback)
        self.root.update_idletasks()

    def tearDown(self):
        self.app.close(); self.network.stop(); self.temp.cleanup()

    def snapshot(self):
        return {path.relative_to(self.store.directory).as_posix(): path.read_bytes()
                for path in self.store.directory.rglob('*.json')}

    def incoming(self, lines=None):
        return replace(self.original, id='incoming', lines=tuple(lines or (
            Line('新しい空を見上げる', 'あたらしいそらをみあげる', '새 하늘을 올려다보다'),
            Line('遠い道を歩こう', 'とおいみちをあるこう', '먼 길을 걷자'),
            Line('また明日会おう', 'またあしたあおう', '내일 다시 만나자'))))

    def save(self, song):
        with patch('app.messagebox.askyesno', return_value=True) as confirm:
            self.assertTrue(self.app.add_manual_song(song, self.playback.key))
        confirm.assert_called_once()

    def provider(self, lines=None):
        rows = tuple(lines or self.original.lines)
        return {'source': 'Spotify', 'lines': rows,
                'times': [10 + 10 * index for index in range(len(rows))], 'provider_id': 'fixture'}

    def temporary_lyrics(self):
        self.playback = replace(self.playback, title='Unsaved Live Fixture')
        self.app.set_playback(self.playback)
        self.assertIsNone(self.app.song)
        self.app.receive_reader_lyrics(self.app.reader_bridge.snapshot()['track'], self.original.lines)
        self.assertTrue(self.app.live_lyrics.is_temporary(self.app.song))
        return self.app.song

    def test_cancel_replace_and_delete_leave_all_saved_bytes_and_active_state_unchanged(self):
        self.app.manual_index = 1; self.app.follow.set(False)
        before = self.snapshot()
        state = (self.app.song, self.app.display_lines, deepcopy(self.app.timing),
                 deepcopy(self.app.settings), self.app.manual_index, self.app.follow.get())
        for action in (lambda: self.app.add_manual_song(self.incoming(), self.playback.key),
                       self.app.delete_selected_lyrics):
            with self.subTest(action=action), patch('app.messagebox.askyesno', return_value=False) as confirm:
                self.assertFalse(action())
                confirm.assert_called_once()
                self.assertEqual(confirm.call_args.kwargs['icon'], 'warning')
                self.assertEqual(confirm.call_args.kwargs['default'], 'no')
                self.assertEqual(self.snapshot(), before)
                self.assertEqual((self.app.song, self.app.display_lines, self.app.timing,
                                  self.app.settings, self.app.manual_index, self.app.follow.get()), state)

    def test_confirmed_original_replacement_keeps_saved_identity_resets_clock_and_preserves_other_song(self):
        before = self.snapshot()
        incoming = self.incoming()
        self.save(incoming)
        saved = replace(self.original, lines=incoming.lines)
        self.assertEqual(self.app.song, saved)
        self.assertEqual(self.app.display_lines, incoming.lines)
        self.assertEqual(self.store.library(), [saved, self.other])
        self.assertEqual(self.store.settings()['links'][self.playback.key], self.original.id)
        self.assertTrue(self.store.settings()['lyric_sources'][self.original.id]['user_override'])
        self.assertEqual(self.app.timing['times'], [None] * len(incoming.lines))
        self.assertEqual(self.app.timing['offset'], 0)
        self.assertEqual(self.snapshot()['timings/other.json'], before['timings/other.json'])
        self.assertEqual(len(list((self.store.directory / 'lyric_edit_backups').glob('*.json'))), 1)
        self.app.set_playback(Playback('Other track'))
        self.app.set_playback(self.playback)
        self.assertEqual(self.app.song, saved)
        self.assertEqual(self.app.display_lines, incoming.lines)

    def test_translation_only_replacement_keeps_manual_clock_and_offset(self):
        before = deepcopy(self.app.timing)
        incoming = self.incoming([replace(line, reading='수정 독음', korean='수정 번역')
                                  for line in self.original.lines])
        self.save(incoming)
        self.assertEqual(self.app.display_lines, incoming.lines)
        expected = {**before, 'fingerprint': self.app.song.fingerprint}
        self.assertEqual(self.app.timing, expected)
        self.assertEqual(self.store.timing(self.app.song), expected)

    def test_web_dialog_cancel_keeps_preview_open_and_confirm_saves_replacement(self):
        self.app.open_web_lyrics(); dialog = self.app.web_dialog
        dialog.paste.insert('1.0', '窓を開ける\n마도오 아케루\n창문을 열다\n\n花を描く\n하나오 카쿠\n꽃을 그리다')
        dialog.parse()
        before = self.snapshot()
        with patch('app.messagebox.askyesno', return_value=False) as confirm:
            dialog.save()
        confirm.assert_called_once()
        self.assertIs(confirm.call_args.kwargs['parent'], dialog.window)
        self.assertTrue(dialog.window.winfo_exists())
        self.assertEqual(str(dialog.save_button.cget('state')), 'normal')
        self.assertIn('취소', dialog.status.get())
        self.assertEqual(self.snapshot(), before)
        with patch('app.messagebox.askyesno', return_value=True):dialog.save()
        self.assertEqual(self.app.song.id, self.original.id)
        self.assertEqual(self.app.display_lines, dialog.lines)
        self.assertEqual(self.app.timing['times'], [None, None])
        self.assertEqual(str(dialog.save_button.cget('state')), 'disabled')

    def test_deleted_lyrics_stay_deleted_after_cached_replay_restart_and_late_provider_results(self):
        self.assertTrue(self.app.lyric_cache.save(self.playback, self.original.lines, direct=self.provider()))
        other_clock = (self.store.directory / 'timings/other.json').read_bytes()
        with patch('app.messagebox.askyesno', return_value=True) as confirm:
            self.assertTrue(self.app.delete_selected_lyrics())
        confirm.assert_called_once()
        self.assertIsNone(self.app.song)
        self.assertEqual(self.store.library(), [self.other])
        self.assertNotIn(self.playback.key, self.store.settings()['links'])
        self.assertIn(self.playback.key, self.store.settings()['deleted_lyrics'])
        self.assertFalse((self.store.directory / 'timings/saved.json').exists())
        self.assertEqual((self.store.directory / 'timings/other.json').read_bytes(), other_clock)
        self.app.set_playback(Playback('Other track')); self.app.set_playback(self.playback)
        self.assertIsNone(self.app.song)
        self.app.close(); self.start_app()
        self.assertIsNone(self.app.song)
        self.app.restore_cached_lyrics()
        self.app.receive_direct_lyrics(self.playback.key, self.provider())
        self.app.receive_reader_lyrics(self.app.reader_bridge.snapshot()['track'], self.original.lines)
        self.assertFalse(self.app.add_web_song(self.original, {'url': 'old automatic result'}))
        self.assertIsNone(self.app.song)
        self.assertEqual(self.app.display_lines, ())
        self.assertEqual(self.store.library(), [self.other])
        self.app.set_playback(Playback(self.other.title, self.other.artist, self.other.album,
                                       10, 120, False, time.monotonic()))
        self.assertEqual(self.app.song, self.other)

    def test_confirmed_text_survives_late_reader_annotations_and_matching_provider_timing(self):
        incoming = self.incoming()
        self.save(incoming)
        corrected = self.app.song
        self.app.receive_reader_lyrics(self.app.reader_bridge.snapshot()['track'], self.original.lines)
        self.app.receive_direct_lyrics(self.playback.key, self.provider())
        self.assertEqual(self.app.song, corrected)
        self.assertEqual(self.app.display_lines, incoming.lines)
        automatic = tuple(replace(line, reading='이전 자동 독음', korean='이전 자동 번역')
                          for line in incoming.lines)
        self.app.receive_direct_lyrics(self.playback.key, {'source': 'ALSong', 'annotation_only': True,
                                                         'lines': automatic, 'times': [1, 2, 3]})
        self.app.receive_direct_lyrics(self.playback.key, self.provider(automatic))
        self.assertEqual(self.app.song, corrected)
        self.assertEqual(self.app.display_lines, incoming.lines)
        self.assertEqual(self.app.direct_times(), [10, 20, 30])
        self.assertEqual(self.app.timing['times'], [None, None, None])
        self.app.close(); self.start_app()
        self.assertEqual(self.app.song, corrected)
        self.assertEqual(self.app.display_lines, incoming.lines)
        self.assertEqual(self.app.direct_times(), [10, 20, 30])

    def test_pending_audio_prefetch_without_song_is_cancelled_when_lyrics_are_replaced(self):
        self.app.auto_analyzer.job_factory = FakeJob
        self.app.auto_analyzer._prefetch(self.playback, self.playback.position())
        pending = next(iter(self.app.auto_analyzer.jobs))
        self.assertIsNone(pending.song)
        self.assertTrue(pending.waiting_for_lyrics)
        self.assertFalse(pending.cancelled.is_set())
        incoming = self.incoming()
        self.save(incoming)
        self.assertTrue(pending.cancelled.is_set())
        self.assertEqual(self.app.display_lines, incoming.lines)

    def test_saving_temporary_lyrics_preserves_exact_original_manual_clock_across_restart(self):
        temporary = self.temporary_lyrics()
        clock = {'times': [11, 28], 'origins': ['manual', 'manual'], 'offset': .75,
                 'source': 'manual', 'estimate_intro': 9., 'estimate_outro': 5.}
        self.app.save_current_timing(clock)
        self.app.timing = self.app.live_lyrics.timing = clock
        persisted = self.app.live_lyrics.timing_store.load(temporary)
        incoming = replace(temporary, id='incoming', lines=tuple(
            replace(line, reading='수정 독음', korean='수정 번역') for line in temporary.lines))
        self.save(incoming)
        saved = self.app.song
        self.assertEqual(saved.id, temporary.id)
        self.assertFalse(self.app.live_lyrics.is_temporary(saved))
        self.assertIn(saved, self.store.library())
        self.assertEqual(self.app.display_lines, incoming.lines)
        self.assertEqual(self.app.timing, {**persisted, 'fingerprint': saved.fingerprint})
        self.assertEqual(self.store.timing(saved), self.app.timing)
        self.app.close(); self.start_app()
        self.assertEqual(self.app.song, saved)
        self.assertEqual(self.app.timing, {**persisted, 'fingerprint': saved.fingerprint})

    def test_deleting_temporary_lyrics_keeps_cached_replay_and_restart_empty(self):
        temporary = self.temporary_lyrics()
        self.app.save_current_timing({'times': [11, 28], 'offset': .75})
        self.assertIsNotNone(self.app.lyric_cache.load(self.playback))
        library = self.store.library()
        with patch('app.messagebox.askyesno', return_value=True):
            self.assertTrue(self.app.delete_selected_lyrics())
        self.assertIsNone(self.app.song)
        self.assertEqual(self.store.library(), library)
        self.assertIn(self.playback.key, self.store.settings()['deleted_lyrics'])
        self.app.set_playback(Playback('Other track')); self.app.set_playback(self.playback)
        self.assertIsNone(self.app.song)
        self.app.close(); self.start_app()
        self.app.receive_reader_lyrics(self.app.reader_bridge.snapshot()['track'], temporary.lines)
        self.app.receive_direct_lyrics(self.playback.key, self.provider(temporary.lines))
        self.assertIsNone(self.app.song)
        self.assertEqual(self.app.display_lines, ())
        self.assertEqual(self.store.library(), library)

    def test_deleting_completed_overlay_removes_saved_base_and_suppresses_replay(self):
        completed_lines = (self.original.lines[0], Line('新しい言葉', 'あたらしいことば', '새로운 말'),
                           self.original.lines[1])
        self.assertTrue(self.app.complete_saved_lyrics(completed_lines))
        self.assertTrue(self.app.live_lyrics.is_temporary(self.app.song))
        self.assertEqual(self.app.live_lyrics.completion_base, self.original.id)
        transient_id = self.app.song.id
        self.assertNotEqual(transient_id, self.original.id)
        self.app.cache_current_lyrics()
        with patch('app.messagebox.askyesno', return_value=True):
            self.assertTrue(self.app.delete_selected_lyrics())
        self.assertEqual(self.store.library(), [self.other])
        self.assertFalse((self.store.directory / 'timings/saved.json').exists())
        self.assertIsNone(self.app.song)
        self.app.set_playback(Playback('Other track')); self.app.set_playback(self.playback)
        self.assertIsNone(self.app.song)
        self.app.close(); self.start_app()
        self.assertIsNone(self.app.song)
        self.assertEqual(self.store.library(), [self.other])

    def test_minimum_dialog_and_editor_keep_save_and_delete_buttons_visible(self):
        self.app.open_editor(); self.app.editor.geometry('560x660+20+20')
        self.app.open_web_lyrics(); dialog = self.app.web_dialog
        dialog.window.geometry('560x530+40+40')
        self.root.update(); self.root.update()
        for window, button in ((self.app.editor, self.app.delete_lyrics_button),
                               (dialog.window, dialog.save_button)):
            with self.subTest(button=button.cget('text')):
                self.assertTrue(button.winfo_ismapped())
                self.assertGreaterEqual(button.winfo_rootx(), window.winfo_rootx())
                self.assertGreaterEqual(button.winfo_rooty(), window.winfo_rooty())
                self.assertLessEqual(button.winfo_rootx() + button.winfo_width(),
                                     window.winfo_rootx() + window.winfo_width())
                self.assertLessEqual(button.winfo_rooty() + button.winfo_height(),
                                     window.winfo_rooty() + window.winfo_height())
        self.assertGreater(dialog.paste.winfo_height(), 70)


if __name__ == '__main__':unittest.main()
