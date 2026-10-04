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

    def test_short_long_short_transition_updates_all_text_before_layout_and_fits(self):
        heights = []
        labels = (self.app.original_label, self.app.reading_label, self.app.korean_label)
        with patch.object(self.root, 'update_idletasks', wraps=self.root.update_idletasks) as forced_idle:
            for index, line in enumerate(self.lines):
                self.app.manual_index = index
                self.app.render()
                # All three languages are updated before any queued layout runs.
                self.assertEqual(tuple(label.cget('text') for label in labels),
                                 (line.original, line.reading, line.korean))
                self.root.update()
                requested = self.app.lyric_frame.winfo_reqheight() + self.app.footer_height + 17
                self.assertEqual(self.root.winfo_height(), requested)
                for label in labels:
                    self.assertTrue(label.winfo_ismapped())
                    self.assertEqual(label.winfo_height(), label.winfo_reqheight())
                    self.assertLessEqual(label.bbox('lyric')[3], label.winfo_height())
                heights.append(self.root.winfo_height())
            forced_idle.assert_not_called()
        self.assertGreater(heights[1], heights[0])
        self.assertEqual(heights[2], heights[0])
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


if __name__ == '__main__':
    unittest.main()
