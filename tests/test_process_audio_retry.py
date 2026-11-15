import unittest
from unittest.mock import patch

from process_audio import _wait_for_spotify_process


class SpotifyProcessRetryTests(unittest.TestCase):
    def test_retries_transient_process_ambiguity(self):
        with (patch('process_audio.spotify_process_id', side_effect=(ValueError('ambiguous'), 123)) as probe,
              patch('process_audio.time.monotonic', side_effect=(10, 11)),
              patch('process_audio.time.sleep') as wait):
            self.assertEqual(_wait_for_spotify_process(), 123)
            self.assertEqual(probe.call_count, 2)
            wait.assert_called_once_with(.2)

    def test_stops_when_spotify_process_cannot_be_verified(self):
        with (patch('process_audio.spotify_process_id', side_effect=ValueError('ambiguous')) as probe,
              patch('process_audio.time.monotonic', side_effect=(10, 11, 14)),
              patch('process_audio.time.sleep') as wait):
            with self.assertRaisesRegex(ValueError, 'ambiguous'):
                _wait_for_spotify_process()
            self.assertEqual(probe.call_count, 2)
            wait.assert_called_once_with(.2)
