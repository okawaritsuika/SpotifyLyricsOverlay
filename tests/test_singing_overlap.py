import importlib.util
import json
import unittest

from singing_overlap import FEATURE_NAMES, fit_overlap_model, overlap_features, predict_overlap


@unittest.skipUnless(importlib.util.find_spec('numpy'), 'optional audio runtime is unavailable')
class SingingOverlapTests(unittest.TestCase):
    @staticmethod
    def tone(frequency, phase=0.):
        import numpy as np

        time = np.arange(44100) / 44100
        return (.2 * (1 + .2 * np.sin(time * 13 + phase)) * np.sin(2 * np.pi * frequency * time)).astype('float32')

    def test_same_clock_and_stem_order(self):
        import numpy as np

        first, second = self.tone(270), self.tone(360, .5)
        mixture = first + second
        features = overlap_features(mixture, [first, second])
        swapped = overlap_features(mixture, [second, first])
        self.assertTrue(features['has_stems'])
        self.assertGreater(features['pitch_disagreement'], .9)
        self.assertLess(features['waveform_correlation'], .01)
        self.assertAlmostEqual(features['residual_ratio'], 0.)
        for name in FEATURE_NAMES:
            self.assertAlmostEqual(features[name], swapped[name])
        with self.assertRaises(ValueError):
            overlap_features(mixture, np.stack([first, second])[:, :-1])

    def test_digital_silence_and_missing_separator_are_distinct(self):
        import numpy as np

        tone = self.tone(320)
        self.assertEqual(predict_overlap(overlap_features(np.zeros_like(tone)))['count'], 0)
        self.assertEqual(predict_overlap(overlap_features(tone))['status'], 'uncertain')
        self.assertFalse(overlap_features(tone, np.zeros((2, len(tone))))['has_stems'])
        self.assertEqual(predict_overlap(overlap_features(tone, [tone * .5, tone * .5]))['status'], 'uncertain')

    @unittest.skipUnless(importlib.util.find_spec('sklearn'), 'optional overlap fitter is unavailable')
    def test_classifier_uses_independence_beyond_power_ratio(self):
        rows, counts = [], []
        for first_frequency, second_frequency in ((245, 360), (262, 403), (309, 454), (325, 483)):
            first, second = self.tone(first_frequency), self.tone(second_frequency)
            rows.extend((overlap_features(first * 2, [first, first]), overlap_features(first + second, [first, second])))
            counts.extend((1, 2))
        model = json.loads(json.dumps(fit_overlap_model(rows, counts)))
        first, second = self.tone(285), self.tone(432)
        duplicated = predict_overlap(overlap_features(first * 2, [first, first]), model)
        independent = predict_overlap(overlap_features(first + second, [first, second]), model)
        self.assertEqual(duplicated['count'], 1)
        self.assertEqual(independent['status'], 'overlap')
        self.assertEqual(independent['min_count'], 2)
        self.assertIsNone(independent['count'])
        self.assertGreater(independent['score'], duplicated['score'])

    @unittest.skipUnless(importlib.util.find_spec('sklearn'), 'optional overlap fitter is unavailable')
    def test_requires_both_classes(self):
        tone = self.tone(300)
        row = overlap_features(tone, [tone, tone * 0])
        with self.assertRaises(ValueError):
            fit_overlap_model([row, row], [1, 1])
