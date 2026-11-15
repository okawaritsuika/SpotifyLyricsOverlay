"""Exercise lyric transitions through Tk's actual idle layout queue."""
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

from app import LyricsApp
from lyrics import Line, Song
from media import Playback
from storage import Store


class LyricRenderLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.store.save_settings({'online': False, 'auto_audio': False, 'web_auto': False,
                                  'borderless': False, 'reader_mode': 'chrome',
                                  'show_original': True, 'show_reading': True})
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = LyricsApp(self.root, self.store, start_services=False)
        self.errors = []
        self.root.report_callback_exception = lambda *error: self.errors.append(error)
        self.lines = (
            Line('短い歌詞', '짧은 독음', '첫 번째 번역'),
            Line('新しい歌詞を確認するための少し長い文章になります。' * 4,
                 '새로운 노래 가사 독음 구절 ' * 4, '두 번째 번역'),
            Line('次の歌詞', '다음 독음', '세 번째 번역'),
        )
        song = Song('layout-probe', 'Layout probe', 'Singer', 'Album', self.lines)
        self.app.songs = [song]
        self.app.set_playback(Playback(title=song.title, artist=song.artist,
                                       album=song.album, duration=180))
        self.app.select_song(song)
        self.app.follow.set(False)
        self.app.manual_index = 0
        self.app.chrome_available = True
        self.app.toggle_chrome()
        self.root.attributes('-alpha', .01)
        self.root.geometry('1040x260+10+40')
        self.root.deiconify()
        self.app.render()
        self.root.update()

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def test_short_long_short_transition_keeps_window_and_rows_in_place(self):
        labels = (self.app.original_label, self.app.reading_label, self.app.korean_label)
        anchored = (*labels, self.app.track_title_label, self.app.track_artist_label)
        with patch.object(self.root, 'update_idletasks', wraps=self.root.update_idletasks) as forced_idle:
            for manual in (False, True):
                self.app.manual_window_size = manual
                if manual:
                    self.app.compact_layout['mode'] = 'shrink_only'
                    self.root.geometry('560x200+10+40')
                    self.root.update()
                before = self.root.geometry(), tuple(label.winfo_rooty() for label in anchored)
                for index in (0, 1, 2, 0):
                    with self.subTest(manual=manual,index=index):
                        line = self.lines[index]
                        self.app.manual_index = index
                        self.app.render()
                        self.assertEqual(tuple(label.cget('text') for label in labels),
                                         (line.original, line.reading, line.korean))
                        self.root.update()
                        self.assertEqual((self.root.geometry(), tuple(label.winfo_rooty() for label in anchored)),before)
                        for label in labels:
                            self.assertTrue(label.winfo_ismapped())
                            self.assertLessEqual(label.bbox('lyric')[3], label.winfo_height())
                            self.assertLessEqual(label.winfo_rooty()+label.winfo_height(),
                                                 self.app.transport.winfo_rooty())
            forced_idle.assert_not_called()
        self.assertEqual(self.errors, [])

    def test_repeated_fit_requests_run_once_without_reentry_or_identical_geometry(self):
        fit = self.app.fit_height
        depth = 0
        depths = []

        def traced_fit():
            nonlocal depth
            depth += 1
            depths.append(depth)
            try:
                fit()
            finally:
                depth -= 1

        with patch.object(self.app, 'fit_height', side_effect=traced_fit) as fitting, \
                patch.object(self.root, 'geometry', wraps=self.root.geometry) as geometry:
            for _ in range(8):
                self.app.request_fit_height()
            self.root.update()
            fitting.assert_called_once_with()
            geometry.assert_not_called()
        self.assertEqual(depths, [1])
        self.assertIsNone(self.app.pending_height_fit)
        self.assertEqual(self.errors, [])

    def test_credit_wrap_keeps_lyrics_centered_at_the_same_position(self):
        solo = ('One singer',)
        group = tuple(f'Performer with a long name {i}' for i in range(12))
        with patch.object(self.app,'displayed_artists',return_value=solo) as artists:
            self.app.manual_window_size = True
            self.root.geometry('1040x550+10+40')
            self.app.last_render = None
            self.app.render()
            self.root.update()
            metadata,lyrics,footer = self.app.track_metadata,self.app.lyric_content,self.app.transport
            artist = self.app.track_artist_label
            position = (metadata.winfo_height(),lyrics.winfo_rooty())
            above = lyrics.winfo_rooty() - metadata.winfo_rooty() - metadata.winfo_height()
            below = footer.winfo_rooty() - lyrics.winfo_rooty() - lyrics.winfo_height()
            self.assertGreater(above,8)
            self.assertAlmostEqual(above,below,delta=8)
            for names,overflow in ((group,True),((),False),(solo,False)):
                artists.return_value = names
                self.app.render()
                self.root.update()
                self.assertEqual((metadata.winfo_height(),lyrics.winfo_rooty()),position)
                self.assertEqual(artist.overflow,overflow)
        self.assertEqual(self.errors, [])

    def test_untranslated_english_uses_the_translation_row_and_fits(self):
        lines = (
            Line('Keep singing', '', ''),
            Line('We can keep singing together and reach for a new horizon. ' * 4, '', ' '),
            Line('Sing with me', '', '나와 함께 노래해'),
            Line('歌い続けよう Keep singing', '', ''),
        )
        song = Song('english-layout', 'English layout', 'Singer', 'Album', lines)
        self.app.songs.append(song)
        self.app.set_playback(Playback(song.title,song.artist,song.album,duration=180))
        before = self.app.display_lines
        self.app.follow.set(False)
        self.app.manual_window_size = True
        self.app.compact_layout['mode'] = 'shrink_only'
        self.root.geometry('560x280+10+40')
        translation = self.app.korean_label
        expected = (lines[0].original,lines[1].original,lines[2].korean,'')
        for show_original in (True,False):
            self.app.show_original.set(show_original)
            positions = []
            for index,line in enumerate(lines):
                with self.subTest(show_original=show_original,index=index):
                    self.app.manual_index = index
                    self.app.render()
                    self.root.update()
                    self.assertEqual(translation.cget('text'),expected[index])
                    self.assertEqual(self.app.original_label.cget('text'),line.original if show_original else '')
                    self.assertTrue(translation.winfo_ismapped())
                    if expected[index]:
                        self.assertLessEqual(translation.bbox('lyric')[3],translation.winfo_height())
                    self.assertLessEqual(translation.winfo_rooty()+translation.winfo_height(),
                                         self.app.transport.winfo_rooty())
                    positions.append(translation.winfo_rooty())
            self.assertEqual(len(set(positions)),1)
        self.assertEqual(self.app.display_lines,before)
        self.assertEqual(self.app.song.lines,lines)
        self.app.manual_index = 0
        self.app.tick()
        self.assertFalse(self.app.reader_bridge.snapshot()['overlay']['current_has_translation'])
        self.assertEqual(self.errors, [])


if __name__ == '__main__':
    unittest.main()
