import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import optional_runtime


class OptionalRuntimeTests(unittest.TestCase):
    def test_download_rejects_wrong_digest_before_execution(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'download.zip'
            with patch.object(optional_runtime,'urlopen',return_value=io.BytesIO(b'bad content')):
                with self.assertRaises(ValueError):optional_runtime.download('https://example.test/file',path,'0'*64)
            self.assertFalse(path.exists())

    def test_audio_explicit_install_targets_external_runtime_and_records_success_last(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with patch.object(optional_runtime,'runtime_dir',return_value=root), \
                 patch.object(optional_runtime,'ensure_python',return_value=root/'python/python.exe'), \
                 patch.object(optional_runtime,'command') as command, \
                 patch.object(optional_runtime,'worker_command',side_effect=lambda base,module,optional:[module]):
                optional_runtime.install('audio')
            calls=[call.args[0] for call in command.call_args_list]
            self.assertTrue(any('torch==2.8.0+cpu' in call for call in calls))
            self.assertIn(['audio_worker','--install-model',str(root/'models/whisper-small')],calls)
            self.assertEqual(calls[-1],['compact_audio_runtime',str(root)])
            self.assertTrue((root/'audio-installed.json').is_file())
            self.assertFalse((root/'browser-installed.json').exists())

    def test_failed_install_is_not_marked_ready(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with patch.object(optional_runtime,'runtime_dir',return_value=root), \
                 patch.object(optional_runtime,'ensure_python',return_value=root/'python/python.exe'), \
                 patch.object(optional_runtime,'command',side_effect=OSError('failed')):
                with self.assertRaises(OSError):optional_runtime.install('browser')
            self.assertFalse((root/'browser-installed.json').exists())


if __name__=='__main__':unittest.main()
