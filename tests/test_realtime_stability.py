"""Missing acoustic evidence must not alternate real lyrics with time guesses."""
import time
import unittest
from unittest.mock import patch

import test_realtime_ui as fixtures
from media import Playback


class RealtimeStabilityTests(unittest.TestCase):
    setUp = fixtures.RealtimeUITests.setUp
    tearDown = fixtures.RealtimeUITests.tearDown

    def start_short_recording(self):
        self.now = time.monotonic()
        self.app.spotify_sample_after = self.now - 1
        self.app.playback = Playback('Sample', 'Artist', 'Album', 10, 30,
                                     True, self.now)

    def match(self, elapsed, index=2, onset=9.5):
        self.app.receive_realtime(self.job, {
            'kind': 'realtime', 'observed_at': self.now + elapsed,
            'position': 10 + elapsed,
            'match': {'index': index, 'confidence': .8, 'onset': onset},
        })

    def test_repeated_gaps_keep_the_last_confirmed_phrase(self):
        self.start_short_recording()
        displayed = []
        for elapsed, fresh in ((0, True), (5.9, False), (6.1, False),
                               (6.2, True), (12.3, False), (12.4, True)):
            with patch('app.time.monotonic', return_value=self.now + elapsed):
                if fresh:
                    self.match(elapsed)
                displayed.append(self.app.displayed_index())
        self.assertEqual(displayed, [2] * 6)
        self.assertEqual(self.store.timing(self.song)['times'], [None] * 6)

    def test_fresh_next_phrase_advances_after_a_gap(self):
        self.start_short_recording()
        with patch('app.time.monotonic', return_value=self.now):
            self.match(0)
        with patch('app.time.monotonic', return_value=self.now + 12.3):
            self.assertEqual(self.app.displayed_index(), 2)
        with patch('app.time.monotonic', return_value=self.now + 12.4):
            self.match(12.4, index=3, onset=22)
            self.assertEqual(self.app.displayed_index(), 3)
        with patch('app.time.monotonic', return_value=self.now + 19):
            self.assertEqual(self.app.displayed_index(), 3)

    def test_seek_clears_the_confirmed_phrase_and_accepts_new_evidence(self):
        self.start_short_recording()
        with patch('app.time.monotonic', return_value=self.now):
            self.match(0)
        with patch('app.time.monotonic', return_value=self.now + 7):
            self.app.set_playback(Playback('Sample', 'Artist', 'Album', 1, 30,
                                          True, self.now + 7))
            self.assertIsNone(self.app.realtime_index)
            self.assertFalse(self.app.holding_audio())
            self.assertIsNone(self.app.displayed_index())
        with patch('app.time.monotonic', return_value=self.now + 8):
            self.app.receive_realtime(self.job, {
                'kind': 'realtime', 'observed_at': self.now + 8, 'position': 2,
                'match': {'index': 0, 'confidence': .8, 'onset': 1.5},
            })
            self.assertEqual(self.app.displayed_index(), 0)

    def test_provider_timestamps_take_priority_over_the_held_phrase(self):
        self.start_short_recording()
        with patch('app.time.monotonic', return_value=self.now):
            self.match(0)
            self.app.receive_direct_lyrics(self.app.playback.key, {
                'source': 'Spotify', 'lines': self.song.lines,
                'times': [1, 3, 5, 7, 9, 11],
            })
            self.assertTrue(self.app.has_direct_sync())
            self.assertEqual(self.app.displayed_index(), 4)
        with patch('app.time.monotonic', return_value=self.now + 7):
            self.assertEqual(self.app.displayed_index(), 5)


if __name__ == '__main__':
    unittest.main()
