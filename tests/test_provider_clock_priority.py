"""Verified provider onsets survive lyric regrouping and stale live anchors."""
from dataclasses import replace
import tempfile
import time
import tkinter as tk
import unittest

from app import LyricsApp
from lyrics import Line, Song
from media import Playback
from storage import Store


SOURCE = ('朝の光', '君を待つ', '花を描く', '風の音', '朝の光', '最後の言葉')
DISPLAY = (SOURCE[0]+SOURCE[1], 'Everyday anyway', SOURCE[2]+SOURCE[3],
           'Everyday anyway', SOURCE[4]+SOURCE[5])


class ProviderClockPriorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.store.save_settings({'online': False, 'auto_audio': False, 'web_auto': False,
                                  'borderless': False, 'use_estimate': True, 'reader_mode': 'chrome',
                                  'lyric_advance_ms': 0})
        self.root = tk.Tk(); self.root.withdraw()
        self.app = LyricsApp(self.root, self.store, start_services=False)
        self.playback = Playback('Song', 'Singer', 'Album', 12, 90, False, time.monotonic())
        self.app.set_playback(self.playback)

    def tearDown(self):
        self.app.close(); self.temp.cleanup()

    def provider(self, texts=SOURCE, times=(10, 12, 20, 22, 30, 32)):
        return {'source': 'Spotify', 'lines': tuple(Line(text, '', '') for text in texts),
                'times': list(times), 'provider_id': 'recording'}

    def receive(self):
        self.app.receive_direct_lyrics(self.playback.key, self.provider())
        lines = tuple(Line(text, '', '번역 '+str(i)) for i, text in enumerate(DISPLAY))
        self.app.receive_reader_lyrics(self.app.reader_bridge.snapshot()['track'], lines)

    def position(self, value):
        self.playback = replace(self.playback, base=value, captured=time.monotonic())
        self.app.set_playback(self.playback); self.app.render()

    def test_regrouped_main_vocals_keep_provider_clock_without_inventing_backing_times(self):
        self.receive()
        self.assertEqual(self.app.direct_times(), [10, None, 20, None, 30])
        self.assertTrue(self.app.has_direct_sync())
        self.assertFalse(self.app.has_complete_sync())
        self.position(19.9)
        self.assertEqual(self.app.displayed_index(), 0)
        self.position(20)
        self.assertEqual(self.app.displayed_index(), 2)
        self.position(29.9)
        self.assertEqual(self.app.displayed_index(), 2)
        self.position(30)
        self.assertEqual(self.app.displayed_index(), 4)
        self.assertEqual(self.app.timing['times'], [None]*5)
        self.assertNotIn('추정 싱크', self.app.mode_label.cget('text'))

    def test_repeated_lyrics_follow_their_own_occurrence_despite_stale_anchor(self):
        self.receive()
        self.position(31)
        self.app.realtime_anchor = (0, 30)
        self.app.realtime_matched_at = time.monotonic()
        self.app.realtime_index = 0
        self.assertEqual(self.app.displayed_index(), 4)
        self.position(11)
        self.assertEqual(self.app.displayed_index(), 0)

    def test_manual_onset_offset_and_preview_keep_priority(self):
        self.receive()
        self.app.timing['times'][2] = 24
        self.app.timing['origins'][2] = 'manual'
        self.app.timing['offset'] = 2
        self.position(25.9)
        self.assertEqual(self.app.displayed_index(), 0)
        self.position(26)
        self.assertEqual(self.app.displayed_index(), 2)
        self.app.manual_index = 1; self.app.follow.set(False)
        self.assertEqual(self.app.displayed_index(), 1)
        self.assertEqual(self.app.timing['times'], [None, None, 24, None, None])

    def test_split_row_waits_for_next_confirmed_onset_with_estimates_enabled(self):
        self.app.receive_direct_lyrics(self.playback.key, self.provider(('青い空を見る', '花を描く', '最後の言葉'), (10, 20, 30)))
        self.app.receive_reader_lyrics(self.app.reader_bridge.snapshot()['track'],
            tuple(Line(text, '', '') for text in ('青い空', 'を見る', '花を描く', '最後の言葉')))
        self.assertTrue(self.app.has_direct_sync())
        self.position(19)
        self.assertEqual(self.app.displayed_index(), 0)
        self.position(20)
        self.assertEqual(self.app.displayed_index(), 2)

    def test_audio_gap_cannot_cross_provider_onset_inside_a_split_display_row(self):
        self.app.receive_direct_lyrics(self.playback.key,self.provider(('abcd','ef','sunrise'),(10,20,30)))
        self.app.receive_reader_lyrics(self.app.reader_bridge.snapshot()['track'],
            tuple(Line(text,'','') for text in ('ab','cdef','sunrise')))
        self.app.timing={**self.app.timing,'times':[5,25,35],'origins':['audio']*3,'audio_finalized':True}
        self.assertEqual(self.app.effective_times()[0],[10,None,30])
        self.app.timing['times'][1]=15
        self.assertEqual(self.app.effective_times()[0],[10,15,30])

    def test_short_excerpt_in_long_document_is_not_a_complete_provider_clock(self):
        texts = tuple(f'固有の歌詞{i}番' for i in range(100))
        self.app.song = Song('test', 'Song', 'Singer', 'Album', tuple(Line(text, '', '') for text in texts))
        self.app.direct_result = (self.playback.key, 90, self.provider((texts[0], texts[49], texts[99]), (1, 40, 80)))
        self.assertFalse(self.app.has_direct_sync())

    def test_unrelated_source_and_sparse_provider_times_are_not_promoted(self):
        self.receive()
        self.app.direct_result = (self.playback.key, 90, self.provider(SOURCE, (10, None, None, None, None, 32)))
        self.assertFalse(self.app.has_direct_sync())
        self.app.direct_result = (self.playback.key, 90,
            self.provider(('別の曲', '関係ない言葉', SOURCE[-1]), (10, 20, 30)))
        self.assertFalse(self.app.has_direct_sync())

    def test_fully_matched_grouping_without_any_mapped_onset_is_not_direct_sync(self):
        texts = tuple(f'固有の歌詞{i}番' for i in range(20))
        grouped = tuple(''.join(texts[index:index+5]) for index in range(0, 20, 5))
        self.app.song = Song('grouped', 'Song', 'Singer', 'Album',
                             tuple(Line(text, '', '') for text in grouped))
        # The provider has 80% timed rows, but every displayed row begins with
        # one of the untimed rows. Text coverage cannot invent those onsets.
        times = tuple(None if index % 5 == 0 else index+1 for index in range(20))
        self.app.direct_result = (self.playback.key, 90, self.provider(texts, times))
        self.assertEqual(self.app.direct_times(), [None]*4)
        self.assertFalse(self.app.has_direct_sync())

    def test_late_old_recording_result_preserves_current_song_and_manual_timing(self):
        self.receive()
        old_key = self.playback.key
        self.playback = replace(self.playback, title='New song')
        self.app.set_playback(self.playback)
        self.app.receive_direct_lyrics(self.playback.key, self.provider())
        self.app.timing['times'][0] = 11
        song = self.app.song; direct = self.app.direct_result
        self.app.receive_direct_lyrics(old_key, self.provider(('古い曲',), (5,)))
        self.assertIs(self.app.song, song)
        self.assertIs(self.app.direct_result, direct)
        self.assertEqual(self.app.timing['times'][0], 11)

    def test_changed_duration_does_not_let_invalid_old_clock_outrank_new_partial_clock(self):
        self.app.receive_direct_lyrics(self.playback.key, self.provider())
        self.playback = replace(self.playback, duration=93)
        self.app.set_playback(self.playback)
        self.assertEqual(self.app.direct_times(), [])
        fresh = self.provider(times=(11, 13, 21, 23, 31, None))
        self.app.receive_direct_lyrics(self.playback.key, fresh)
        self.assertEqual(self.app.direct_result[2], fresh)
        self.assertEqual(self.app.direct_result[1], 93)
        self.assertEqual(self.app.direct_times(), [11, 13, 21, 23, 31, None])

    def partial_provider(self):
        self.receive()
        texts = SOURCE + tuple(f'表示とは違う原文{i}' for i in range(7))
        times = (10, 12, 20, 22, 30, 32, 40, 45, 50, 55, 60, 65, 70)
        self.app.direct_result = (self.playback.key, 90, self.provider(texts, times))
        self.assertFalse(self.app.has_direct_sync())

    def test_partial_match_onsets_override_stale_anchor_at_and_after_every_measurement(self):
        self.partial_provider()
        for index, stamp in ((0, 10), (2, 20), (4, 30)):
            for epsilon in (0, .001):
                with self.subTest(index=index, epsilon=epsilon):
                    self.position(stamp+epsilon)
                    self.app.realtime_anchor = (0, 120)
                    self.app.realtime_matched_at = time.monotonic()
                    self.assertEqual(self.app.displayed_index(), index)

    def test_partial_match_future_anchors_cannot_be_crossed_early(self):
        self.partial_provider()
        self.position(18)
        self.app.realtime_anchor = (4, 0)
        self.app.realtime_matched_at = time.monotonic()
        self.assertLess(self.app.displayed_index(), 2)
        self.position(28)
        self.app.realtime_anchor = (4, 0)
        self.app.realtime_matched_at = time.monotonic()
        self.assertLess(self.app.displayed_index(), 4)

    def test_partial_match_respects_manual_replacement_and_offset(self):
        self.partial_provider()
        self.app.timing['times'][2] = 24
        self.app.timing['origins'][2] = 'manual'
        self.app.timing['offset'] = 2
        self.position(25.999)
        self.app.realtime_anchor = (4, 0)
        self.app.realtime_matched_at = time.monotonic()
        self.assertLess(self.app.displayed_index(), 2)
        self.position(26.001)
        self.app.realtime_anchor = (0, 120)
        self.app.realtime_matched_at = time.monotonic()
        self.assertEqual(self.app.displayed_index(), 2)


if __name__ == '__main__':
    unittest.main()
