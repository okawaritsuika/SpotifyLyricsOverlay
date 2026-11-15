from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import numpy as np

from pyshiro_refine import PyshiroRefiner, PyshiroUnavailable


_TEST_WORKER = '''
import base64, json, sys
import numpy as np
print(json.dumps({'ready': True}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    audio = np.frombuffer(base64.b64decode(request['pcm']), dtype='<f4')
    if len(audio) != 32000 or not np.allclose(audio, 0.05):
        print(json.dumps({'error': 'expected a 2-second crop'}), flush=True)
    else:
        print(json.dumps({'candidate': [25, 1300, -32.0]}), flush=True)
'''


class PyshiroRefinerTests(unittest.TestCase):
    def test_crop_is_bounded_by_neighbors_and_offsets_returned(self):
        with TemporaryDirectory() as directory, patch('pyshiro_refine._WORKER', _TEST_WORKER):
            model = Path(directory) / 'model.hsmm'
            phonemap = Path(directory) / 'phonemap.json'
            model.touch()
            phonemap.touch()
            refiner = PyshiroRefiner(sys.executable, model, phonemap)
            try:
                audio = np.full(16_000 * 10, 0.05, dtype=np.float32)
                result = refiner.refine(audio, ['k', 'i'], 3000, 4500, start_limit_ms=2800, end_limit_ms=4800)
                self.assertEqual((result.window_start_ms, result.window_end_ms), (2800, 4800))
                self.assertEqual((result.start_ms, result.end_ms), (2825, 4100))
                process = refiner._process
                self.assertEqual(refiner.refine(audio, ['k', 'i'], 3000, 4500, start_limit_ms=2800,
                                                end_limit_ms=4800), result)
                self.assertIs(refiner._process, process)
            finally:
                refiner.close()
            self.assertIsNotNone(process.poll())

    def test_silence_and_overlong_windows_skip_refinement(self):
        with TemporaryDirectory() as directory:
            asset = Path(directory) / 'asset'
            asset.touch()
            refiner = PyshiroRefiner(sys.executable, asset, asset)
            self.assertIsNone(refiner.refine(np.zeros(96_000), ['a'], 3000, 4000))
            self.assertIsNone(refiner.refine(np.ones(512_000), ['a'], 2000, 29_000))
            self.assertIsNone(refiner._process)

    def test_missing_checkpoint_is_reported(self):
        with TemporaryDirectory() as directory:
            missing = Path(directory) / 'model.hsmm'
            with self.assertRaisesRegex(PyshiroUnavailable, 'model.hsmm'):
                PyshiroRefiner(sys.executable, missing, missing)
