import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

from character_voice import SAMPLE_RATE, VoiceAnalyzer, VoiceAudio


@unittest.skipUnless(importlib.util.find_spec('numpy') and importlib.util.find_spec('av'),
                     'optional audio runtime is unavailable')
class CharacterVoiceTests(unittest.TestCase):
    def setUp(self):
        import numpy as np

        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.analyzer = VoiceAnalyzer(self.directory, singer_model='spectral', default_threshold=.38)
        time = np.arange(SAMPLE_RATE * 2) / SAMPLE_RATE
        self.voices = {name: (.13 * np.sin(2 * np.pi * freq * time) +
                             .08 * np.sin(2 * np.pi * freq * 1.07 * time)).astype(np.float32)
                       for name, freq in zip('ABCD', (170, 470, 1400, 3750))}
        self.profiles = {name: self.analyzer.enroll(self.audio(samples)) for name, samples in self.voices.items()}

    def audio(self, samples, kind='singing'):
        return VoiceAudio(samples, kind, 'test-isolated')

    def write_wav(self, samples):
        import numpy as np

        path = self.directory / 'isolated.wav'
        with wave.open(str(path), 'wb') as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(SAMPLE_RATE)
            target.writeframes((samples * 32767).astype(np.int16).tobytes())
        return path

    def test_solo_and_multilabel_estimates_from_controlled_signals(self):
        for truth in ('A', 'BD', 'ACD', 'ABCD'):
            with self.subTest(truth=truth):
                probe = self.audio(sum(self.voices[voice] for voice in truth) / len(truth))
                row = self.analyzer.score(probe, ranges=[(0, 2000)], profiles=self.profiles)[0]
                self.assertEqual(row['singers'], list(truth))
                self.assertEqual(row['estimated_count'], len(truth))
                self.assertEqual(row['status'], 'estimated')

    def test_subsegments_do_not_imply_simultaneous_singers(self):
        import numpy as np

        probe = self.audio(np.concatenate([self.voices['A'], self.voices['B']]))
        row = self.analyzer.score(probe, ranges=[(0, 4000)], profiles=self.profiles)[0]
        self.assertIsNone(row['estimated_count'])
        self.assertEqual([sub['singers'] for sub in row['subsegments']], [['A'], ['B']])

    def test_missing_calibration_and_wrong_kind_abstain(self):
        path = self.write_wav(self.voices['A'])
        singing = self.analyzer.prepare(path, isolated=True)
        self.assertEqual(singing.duration_ms, 2000)
        self.analyzer.default_threshold = None
        row = self.analyzer.score(singing, ranges=[(0, 2000)], profiles=self.profiles)[0]
        self.assertGreater(row['scores']['A'], .95)
        self.assertEqual(row['status'], 'uncertain')
        self.assertEqual(row['singers'], [])
        self.assertIsNone(row['estimated_count'])
        dialogue = self.analyzer.prepare(path, kind='speech')
        self.assertNotEqual(self.analyzer.model_id('speech'), self.analyzer.model_id('singing'))
        row = self.analyzer.score(dialogue, ranges=[(0, 2000)], profiles=self.profiles)[0]
        self.assertEqual(row['status'], 'unavailable')
        self.assertTrue(all(score is None for score in row['scores'].values()))

    def test_calibrated_decisions_can_confirm_a_subset_with_missing_profiles(self):
        analyzer = VoiceAnalyzer(self.directory, singer_model='spectral', calibration={'*': [.2, .6]})
        audio = self.audio((self.voices['A'] + self.voices['C']) / 2)
        row = analyzer.score(audio, ranges=[(0, 2000)], profiles={**self.profiles, 'D': []})[0]
        self.assertEqual(row['singers'], ['A', 'C'])
        self.assertEqual(row['decisions']['B'], 'below_threshold')
        self.assertEqual(row['decisions']['D'], 'uncertain')
        self.assertEqual(row['status'], 'uncertain')
        self.assertIsNone(row['estimated_count'])
        self.assertEqual(set(row['ranked'][:2]), {'A', 'C'})

    def test_unavailable_separator_preserves_identity_scores_and_abstention(self):
        analyzer = VoiceAnalyzer(self.directory, singer_model='spectral', separator='unmixx')
        audio = self.audio(self.voices['A'])
        with patch('singing_voice_separation.UnmixxSeparator', side_effect=FileNotFoundError('checkpoint missing')):
            row = analyzer.score(audio, ranges=[(0, 2000)], profiles=self.profiles)[0]
        self.assertEqual(row['ranked'][0], 'A')
        self.assertEqual(row['status'], 'uncertain')
        self.assertIn('checkpoint missing', row['subsegments'][0]['separation_error'])

    def test_calibrated_gap_rejects_distant_candidates_without_forcing_two(self):
        analyzer = VoiceAnalyzer(self.directory, singer_model='spectral',
                                 calibration={'*': [.52, .52]}, max_score_gap=.025)
        for scores, singers in (({'A': .60, 'B': .58, 'C': .56, 'D': .50}, ['A', 'B']),
                                ({'A': .60, 'B': .55, 'C': .54, 'D': .53}, ['A']),
                                ({'A': .60, 'B': .59, 'C': .59, 'D': .58}, ['A', 'B', 'C', 'D'])):
            with self.subTest(scores=scores):
                result = analyzer._summarize(scores)
                self.assertEqual(result['singers'], singers)
                self.assertEqual(result['estimated_count'], len(singers))

    def test_silence_noise_and_external_references(self):
        import numpy as np

        quiet = np.zeros_like(self.voices['A'])
        noise = np.random.default_rng(41).normal(0, .04, len(quiet)).astype(np.float32)
        self.assertEqual(self.analyzer.enroll(self.audio(quiet)), [])
        self.assertEqual(self.analyzer.enroll(self.audio(noise)), [])
        audio = self.write_wav(self.voices['A'])
        playlist = self.directory / 'reference.ffconcat'
        playlist.write_text(f"ffconcat version 1.0\nfile '{audio.as_posix()}'\n", encoding='utf-8')
        with self.assertRaises((ValueError, OSError)):
            self.analyzer.prepare(playlist, isolated=True)

    def test_stem_cache_reuses_audio_and_separation_revision(self):
        from character_voice import SEPARATION_WEIGHTS

        (self.analyzer.models_dir).mkdir(parents=True)
        (self.analyzer.models_dir / SEPARATION_WEIGHTS).write_bytes(b'first checkpoint fingerprint')
        path = self.write_wav(self.voices['A'])
        with patch.object(self.analyzer, '_separate', return_value=self.voices['A']) as separate:
            first = self.analyzer.prepare(path)
            second = self.analyzer.prepare(path)
            separate.assert_called_once()
        self.assertEqual(first.preparation_id, second.preparation_id)
        self.assertEqual(first.samples.tobytes(), second.samples.tobytes())
        (self.analyzer.models_dir / SEPARATION_WEIGHTS).write_bytes(b'updated checkpoint fingerprint')
        updated = VoiceAnalyzer(self.directory, singer_model='spectral')
        self.assertNotEqual(first.preparation_id, updated.preparation_id())


if __name__ == '__main__':
    unittest.main()
