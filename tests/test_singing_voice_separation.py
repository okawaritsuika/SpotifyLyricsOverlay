import importlib.util
from tempfile import TemporaryDirectory
from threading import Lock
import unittest
from unittest.mock import Mock

from singing_voice_separation import SingerInformedSeparator, UnmixxSeparator


class SeparationAssetTests(unittest.TestCase):
    def test_missing_models_fail_with_asset_path(self):
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(FileNotFoundError, 'unmixx.*conf.yml'):
                UnmixxSeparator(directory)
            with self.assertRaisesRegex(FileNotFoundError, 'singer-informed.*separator.json'):
                SingerInformedSeparator(directory)


@unittest.skipUnless(all(importlib.util.find_spec(name) for name in ('numpy', 'torch', 'torchaudio')),
                     'Optional torch audio runtime is unavailable')
class SeparationInferenceTests(unittest.TestCase):
    def test_unmixx_cuda_error_falls_back_and_keeps_original_time_axis(self):
        import numpy as np
        import torch

        separator = UnmixxSeparator.__new__(UnmixxSeparator)
        separator.sample_rate = 24000
        separator.device = 'cuda'
        separator._allow_cpu_fallback = True
        separator._lock = Lock()
        separator._model = Mock(win=960)

        def predict(audio):
            if separator.device == 'cuda':
                raise RuntimeError('CUDA is out of memory')
            return torch.stack((audio, torch.zeros_like(audio)))

        separator._infer = Mock(side_effect=predict)
        mixture = np.zeros(24251, dtype=np.float32)
        stems = separator.separate(mixture)
        self.assertEqual(separator.device, 'cpu')
        separator._model.to.assert_called_once_with('cpu')
        self.assertEqual(separator._infer.call_count, 2)
        self.assertEqual(stems.shape, (2, len(mixture)))
        self.assertEqual(stems.dtype, mixture.dtype)

    def test_film_extract_returns_exact_residual_with_short_input(self):
        import numpy as np

        separator = SingerInformedSeparator.__new__(SingerInformedSeparator)
        separator._fft_size = 4096
        separator._on_device = Mock(side_effect=lambda operation, mixture, _: mixture * .25)
        samples = np.ones(2117, dtype=np.float32)
        target, residual = separator.extract(samples, np.zeros(32, dtype=np.float32))
        np.testing.assert_array_equal(target, samples * .25)
        np.testing.assert_array_equal(target + residual, samples)


if __name__ == '__main__':
    unittest.main()
