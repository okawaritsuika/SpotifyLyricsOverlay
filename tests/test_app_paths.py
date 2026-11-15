import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app_paths


class ReleasePathsTests(unittest.TestCase):
    def test_public_seed_preserves_existing_data_and_runs_once(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);source=base/'resources/seed_data';source.mkdir(parents=True)
            (source/'library.json').write_text('["public"]',encoding='utf-8')
            (source/'settings.json').write_text('{}',encoding='utf-8')
            (source/'_manifest.json').write_text('{}',encoding='utf-8')
            data=base/'data';data.mkdir();(data/'library.json').write_text('["mine"]',encoding='utf-8')
            with patch.object(app_paths,'resource_dir',return_value=base):
                app_paths.seed_data(data)
                self.assertEqual((data/'library.json').read_text(encoding='utf-8'),'["mine"]')
                self.assertFalse((data/'_manifest.json').exists())
                (data/'settings.json').unlink()
                app_paths.seed_data(data)
                self.assertFalse((data/'settings.json').exists())

    def test_frozen_uses_writable_user_directory(self):
        with patch.object(app_paths,'frozen',return_value=True), patch.dict(os.environ,{'SPOTIFY_LYRICS_HOME':'C:/private-runtime'}):
            self.assertEqual(app_paths.default_data_dir(),Path('C:/private-runtime/data'))
            self.assertEqual(app_paths.runtime_dir(),Path('C:/private-runtime/runtime'))

    def test_frozen_worker_reenters_exe_without_python_arguments(self):
        with patch.object(app_paths,'frozen',return_value=True), patch.object(app_paths.sys,'executable','overlay.exe'):
            for module in ('spotify_controls','spotify_worker','browser_worker'):
                self.assertEqual(app_paths.worker_command(Path('.'),module),['overlay.exe','--worker',module])

    def test_optional_runtime_never_downloads_during_availability_check(self):
        from optional_runtime import audio_installed
        with tempfile.TemporaryDirectory() as folder, patch('optional_runtime.runtime_dir',return_value=Path(folder)):
            self.assertFalse(audio_installed())


if __name__=='__main__':unittest.main()
