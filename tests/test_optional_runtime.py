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
                 patch.object(optional_runtime,'nvidia_driver_available',return_value=False), \
                 patch.object(optional_runtime,'command') as command, \
                 patch.object(optional_runtime,'worker_command',side_effect=lambda base,module,optional:[module]):
                optional_runtime.install('audio')
            calls=[call.args[0] for call in command.call_args_list]
            self.assertTrue(any('torch==2.8.0+cpu' in call for call in calls))
            self.assertIn(['audio_worker','--install-model',str(root/'models/whisper-small')],calls)
            self.assertFalse(any(str(root/'models/whisper-medium') in call for call in calls))
            self.assertEqual(calls[-1],['compact_audio_runtime',str(root)])
            self.assertTrue((root/'audio-installed.json').is_file())
            self.assertFalse((root/'browser-installed.json').exists())

    def test_failed_install_is_not_marked_ready(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with patch.object(optional_runtime,'runtime_dir',return_value=root), \
                 patch.object(optional_runtime,'ensure_python',return_value=root/'python/python.exe'), \
                 patch.object(optional_runtime,'command',side_effect=OSError('failed')):
                with self.assertRaises(OSError):optional_runtime.install('audio')
            self.assertFalse((root/'audio-installed.json').exists())

    def test_nvidia_driver_adds_live_model_without_replacing_cpu_torch(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with patch.object(optional_runtime,'runtime_dir',return_value=root), \
                 patch.object(optional_runtime,'ensure_python',return_value=root/'python/python.exe'), \
                 patch.object(optional_runtime,'nvidia_driver_available',return_value=True), \
                 patch.object(optional_runtime,'command') as command, \
                 patch.object(optional_runtime,'worker_command',side_effect=lambda base,module,optional:[module]):
                optional_runtime.install('audio')
            calls=[call.args[0] for call in command.call_args_list]
            self.assertTrue(any('torch==2.8.0+cpu' in call for call in calls))
            self.assertIn(['audio_worker','--install-model',str(root/'models/whisper-medium'),'medium'],calls)
            self.assertIn(['compact_audio_runtime',str(root)],calls)

    def test_explicit_cuda_audio_install_uses_matching_wheels_and_skips_cpu_compaction(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with patch.object(optional_runtime,'runtime_dir',return_value=root), \
                 patch.object(optional_runtime,'ensure_python',return_value=root/'python/python.exe'), \
                 patch.object(optional_runtime,'command') as command, \
                 patch.object(optional_runtime,'worker_command',side_effect=lambda base,module,optional:[module]):
                optional_runtime.install('audio-cuda')
            calls=[call.args[0] for call in command.call_args_list]
            cuda_pip=next(call for call in calls if 'torch==2.8.0+cu126' in call)
            self.assertIn('torchaudio==2.8.0+cu126',cuda_pip)
            self.assertIn('torchvision==0.23.0+cu126',cuda_pip)
            self.assertIn(['audio_worker','--install-model',str(root/'models/whisper-medium'),'medium'],calls)
            self.assertTrue(any(optional_runtime.resource_dir()/'requirements-audio-gpu.txt' in call for call in calls))
            self.assertNotIn('compact_audio_runtime',[call[0] for call in calls])
            self.assertTrue((root/'audio-installed.json').is_file())


if __name__=='__main__':unittest.main()
