import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

import numpy as np

from audio_capture_cache import (CAPTURE_NAME, INFO_NAME, SAMPLE_RATE, WAV_NAME, capture_frames,
                                 capture_path, discard_private_capture, discard_retry_capture, find_capture, load_capture_samples,
                                 recording_directory, save_capture, write_capture_audio)
from media import Playback


class AudioCaptureCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache = self.root / 'audio_captures'
        self.playback = Playback('Track', 'Artist', 'Album', duration=12)
        self.info = {'start_position': 0.5, 'clip_tail': 1.0, 'capture_source': 'spotify-process'}
        self.samples = np.full(11 * SAMPLE_RATE, 0.25, dtype=np.float32)

    def test_ignores_short_or_truncated_audio_and_different_recordings(self):
        key, duration = self.playback.key, self.playback.duration
        save_capture(self.cache, key, duration, self.info, self.samples[:3 * SAMPLE_RATE])
        self.assertIsNone(find_capture(self.cache, key, duration))
        save_capture(self.cache, key, duration, self.info, self.samples)
        cached = find_capture(self.cache, key, duration)
        self.assertIsNotNone(cached)
        self.assertIsNone(find_capture(self.cache, key, duration + 4))
        self.assertIsNone(find_capture(self.cache, Playback('Other', 'Artist', 'Album').key, duration))
        self.assertFalse((cached / WAV_NAME).exists())
        path = cached / CAPTURE_NAME
        self.assertEqual(capture_path(cached), path)
        with self.assertRaises(wave.Error), wave.open(str(path), 'rb'):
            pass
        with path.open('r+b') as audio:
            audio.truncate(path.stat().st_size - 2)
        self.assertIsNone(find_capture(self.cache, key, duration))
        save_capture(self.cache, key, duration, self.info, self.samples)
        self.assertEqual(find_capture(self.cache, key, duration), cached)

    def test_analysis_audio_supports_stereo_working_payload_and_preview_bytes(self):
        path = self.root / 'preview.sloa'
        frames, rate = 4800, 48000
        pcm = np.full((frames, 2), 8192, dtype='<i2').tobytes()
        with write_capture_audio(path, rate=rate, channels=2) as audio:
            audio.write(pcm)
        self.assertEqual(capture_frames(path), (frames, rate, 2))
        samples = load_capture_samples(path)
        self.assertEqual(samples.dtype, np.float32)
        self.assertEqual(len(samples), 1600)
        np.testing.assert_array_equal(load_capture_samples(path.read_bytes()), samples)
        self.assertGreater(float(samples.mean()), .24)

    def test_cleanup_leaves_metadata_and_legacy_wav_untouched(self):
        key, duration = self.playback.key, self.playback.duration
        cached = save_capture(self.cache, key, duration, self.info, self.samples)
        path = capture_path(cached)
        self.assertFalse(discard_private_capture(path, self.root, duration))
        self.assertFalse(discard_private_capture(path, self.cache, duration + .1))
        self.assertFalse(discard_retry_capture(self.cache, Playback('Other', 'Artist', 'Album').key, duration))
        self.assertTrue(discard_retry_capture(self.cache, key, duration))
        self.assertTrue((cached / INFO_NAME).is_file())
        self.assertIsNone(find_capture(self.cache, key, duration))

        with wave.open(str(cached / WAV_NAME), 'wb') as audio:
            audio.setparams((1, 2, SAMPLE_RATE, 0, 'NONE', 'not compressed'))
            audio.writeframes((self.samples * 32768).astype('<i2').tobytes())
        self.assertEqual(find_capture(self.cache, key, duration), cached)
        self.assertFalse(discard_private_capture(capture_path(cached), self.cache, duration))
        np.testing.assert_array_equal(load_capture_samples(capture_path(cached)), self.samples)

    def test_worker_deletes_working_audio_and_only_retains_a_retry_candidate(self):
        import audio_worker

        key, duration = self.playback.key, self.playback.duration
        request = self.root / 'request.json'
        request.write_text(json.dumps({'key': key, 'duration': duration, 'capture_cache': str(self.cache)}),
                           encoding='utf-8')
        request.with_name(INFO_NAME).write_text(json.dumps(self.info), encoding='utf-8')
        path = request.with_name(CAPTURE_NAME)
        for times, needs_retry in (([1., 2.], False), ([1., None], True)):
            with self.subTest(times=times):
                with write_capture_audio(path) as audio:
                    audio.write((self.samples * 32768).astype('<i2').tobytes())
                with patch('audio_worker.sys.argv', ['audio_worker.py', '--analyze', str(request)]), \
                     patch('audio_worker.emit') as emit, patch('analysis_limits.apply_analysis_limits'), \
                     patch('audio_worker.align_lyrics', return_value={'matched': 1, 'times': times}):
                    audio_worker.main()
                self.assertEqual(emit.call_args.args, ('result',))
                self.assertIs(emit.call_args.kwargs['result']['capture_retry_needed'], needs_retry)
                self.assertFalse(path.exists())
                cached = find_capture(self.cache, key, duration)
                self.assertEqual(cached is not None, needs_retry)
                if cached is not None:
                    self.assertEqual(capture_path(cached).name, CAPTURE_NAME)
        self.assertEqual(list(self.root.rglob('*.wav')), [])

    def test_reanalysis_skips_capture_for_private_and_legacy_payloads(self):
        import audio_worker

        key, duration = self.playback.key, self.playback.duration
        request = self.root / 'request.json'
        cached = save_capture(self.cache, key, duration, self.info, self.samples)
        legacy = recording_directory(self.cache, key) / str(round(duration + 1))
        legacy.mkdir()
        (legacy / INFO_NAME).write_text(json.dumps({**self.info, 'duration': duration}), encoding='utf-8')
        with wave.open(str(legacy / WAV_NAME), 'wb') as audio:
            audio.setparams((1, 2, SAMPLE_RATE, 0, 'NONE', 'not compressed'))
            audio.writeframes((self.samples * 32768).astype('<i2').tobytes())
        with patch('audio_worker.emit') as emit, patch('analysis_limits.apply_analysis_limits'), \
             patch('audio_worker.capture') as capture, patch('audio_worker.align_lyrics',
                   side_effect=lambda *args, **kwargs: {'matched': 1, 'times': [1.0], 'line_alignment': [True]}) as align:
            for folder in (cached, legacy):
                data = {'key': key, 'duration': duration, 'cached_capture': str(folder), 'capture_cache': str(self.cache)}
                request.write_text(json.dumps(data), encoding='utf-8')
                with patch('audio_worker.sys.argv', ['audio_worker.py', str(request)]):
                    audio_worker.main()
                with patch('audio_worker.sys.argv', ['audio_worker.py', '--analyze', str(request)]):
                    audio_worker.main()
            emit.assert_any_call('captured')
            self.assertEqual(emit.call_args.args, ('result',))
            capture.assert_not_called()
            self.assertEqual(align.call_count, 2)
            first, second = [call.args for call in align.call_args_list]
            np.testing.assert_array_equal(first[1], second[1])
            self.assertEqual(len(second[1]), 10 * SAMPLE_RATE)
            self.assertEqual(first[2], second[2])
            self.assertEqual(second[2], self.info['start_position'])


if __name__ == '__main__':
    unittest.main()
