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
from sync import lyric_advance_ms, lyric_display_time


class LyricDisplayAdvanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.store.save_settings({'online': False, 'use_estimate': False})
        self.song = Song('lead', 'Test Song', 'Artist', 'Album',
                         tuple(Line(text, '', '') for text in ('青い空', '赤い花', '夜の星')))
        self.store.save_library([self.song])
        self.playback = Playback('Test Song', 'Artist', 'Album', 0, 90, False, time.monotonic())
        self.open_app()
        self.addCleanup(lambda: self.app.close())

    def open_app(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = LyricsApp(self.root, self.store, start_services=False)
        self.app.set_playback(self.playback)

    def position(self, value, **kwargs):
        self.app.set_playback(replace(self.playback, base=value, **kwargs))
        self.app.render()
        return self.app.displayed_index()

    def test_local_and_manual_onsets_use_one_lead_after_per_song_offset(self):
        timing = {'times': [10, 20, 30], 'offset': .2, 'origins': ['audio', 'manual', 'audio']}
        self.store.save_timing(self.song, timing)
        self.app.select_song(self.song)
        self.assertEqual(self.app.lyric_advance_ms, 150)
        self.assertEqual(self.position(20), 0)
        self.assertEqual(self.position(20.05), 1)
        self.assertEqual(self.app.displayed_index(advance=False), 0)
        self.assertEqual(self.position(30.05), 2)
        self.assertEqual(self.store.timing(self.song), {**timing, 'fingerprint': self.song.fingerprint})

    def test_spotify_clock_lead_follows_pause_resume_seek_and_track_change(self):
        times = [10, 20, 30]
        self.app.receive_direct_lyrics(self.playback.key, {'source': 'Spotify', 'times': times,
                                        'lines': self.song.lines, 'provider_id': 'track123'})
        self.assertEqual(self.position(19.84), 0)
        self.assertEqual(self.position(19.85), 1)
        self.assertEqual(self.position(29, playing=False, captured=100), 1)
        with patch('media.time.monotonic', return_value=101):
            self.assertEqual(self.position(28.86, playing=True, captured=100), 2)
        self.assertEqual(self.position(11), 0)
        self.assertEqual(self.app.direct_times(), times)
        self.app.set_playback(replace(self.playback, title='Next Song', base=29.9))
        self.assertIsNone(self.app.displayed_index())

    def test_slider_applies_immediately_and_persists_when_app_reopens(self):
        self.store.save_timing(self.song, {'times': [10, 20, 30], 'offset': 0, 'source': '음원 분석'})
        self.app.select_song(self.song)
        self.position(19.8)
        self.assertEqual(self.app.original_label.cget('text'), self.song.lines[0].original)
        self.app.open_appearance()
        dialog = self.app.appearance_dialog
        dialog.advance_scale.set(237)
        self.assertEqual(dialog.advance_label.cget('text'), '237 ms')
        self.assertEqual(self.app.original_label.cget('text'), self.song.lines[1].original)
        dialog.change_advance(0)
        self.assertEqual(self.app.original_label.cget('text'), self.song.lines[0].original)
        dialog.change_advance(300)
        dialog.close()
        self.app.close()
        self.open_app()
        self.assertEqual(self.app.lyric_advance_ms, 300)
        self.assertEqual(self.position(19.8), 1)

    def test_stamping_next_line_uses_unadvanced_row_and_clock(self):
        self.app.timing = {'times': [10, 20, 30], 'offset': .2, 'source': '음원 분석'}
        self.assertEqual(self.position(20.1), 1)
        self.app.stamp()
        self.assertEqual(self.store.timing(self.song)['times'], [10, 19.9, 30])

    def test_setting_validation_and_clamp_at_track_start(self):
        self.assertEqual(lyric_advance_ms('invalid'), 150)
        self.assertEqual(lyric_advance_ms(600), 500)
        self.assertEqual(lyric_advance_ms(-100), 0)
        self.assertEqual(lyric_display_time(.1, 150), 0)
        self.assertAlmostEqual(lyric_display_time(20, 150, .2), 20.05)


if __name__ == '__main__':
    unittest.main()
