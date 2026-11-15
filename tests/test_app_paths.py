import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app_paths
from lyrics import Line, Song
from storage import atomic_json


class ReleasePathsTests(unittest.TestCase):
    def write_seed(self, base, documents):
        source = base / 'resources' / 'seed_data'
        for name, value in documents.items():
            atomic_json(source / name, value)
        total = sum((source / name).stat().st_size for name in documents)
        atomic_json(source / '_manifest.json', {'schema_version': 1, 'files': len(documents), 'bytes': total})

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
                with patch('app_version.VERSION','99.0.0'), patch('data_updates._plan',side_effect=AssertionError('unchanged data')):
                    app_paths.seed_data(data)
                self.assertFalse((data/'settings.json').exists())

    def test_existing_release_adopts_new_songs_and_preserves_local_edits(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            data = base / 'data'
            data.mkdir()
            marker = data / '.public-seed-installed'
            marker.touch()
            new_song = Song('a' * 24, 'New song', 'Singer', 'Album', (Line('new', '', 'translated'),))
            local_song = Song('b' * 24, 'Local song', 'Singer', 'Album', (Line('local', '', ''),))
            removed_song = Song('c' * 24, 'Removed song', 'Singer', 'Album', (Line('removed', '', ''),))
            roster = 'singer_rosters/' + 'a' * 64 + '.json'
            timing = 'timings/' + new_song.id + '.json'
            removal = 'timings/' + removed_song.id + '.json'
            clock = {'fingerprint': new_song.fingerprint, 'times': [12], 'offset': 0,
                     'source': '직접 기록', 'origins': ['manual']}
            self.write_seed(base, {'library.json': [new_song.to_dict(), removed_song.to_dict()],
                                   timing: clock,
                                   removal: {**clock, 'fingerprint': removed_song.fingerprint},
                                   roster: ['Bundled singer']})
            originals = {'settings.json': {'font_size': 37, 'deleted_lyrics': [removed_song.id]},
                         'spotify_login.json': {'token': 'private'}, roster: ['My singer']}
            for name, value in {**originals, 'library.json': [local_song.to_dict()]}.items():
                atomic_json(data / name, value)

            with patch.object(app_paths, 'resource_dir', return_value=base):
                app_paths.seed_data(data)
                self.assertEqual(json.loads((data / timing).read_bytes()), clock)
                library = json.loads((data / 'library.json').read_bytes())
                self.assertEqual([Song.from_dict(row) for row in library], [local_song, new_song])
                for name, value in originals.items():
                    self.assertEqual(json.loads((data / name).read_bytes()), value)
                self.assertFalse((data / removal).exists())
                self.assertEqual(len(json.loads(marker.read_bytes())['content_sha256']), 64)

                (data / timing).unlink()
                with patch('data_updates._plan', side_effect=AssertionError('seed already installed')):
                    app_paths.seed_data(data)
                self.assertFalse((data / timing).exists())

    def test_invalid_upgrade_does_not_acknowledge_legacy_marker(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            data = base / 'data'
            data.mkdir()
            marker = data / '.public-seed-installed'
            marker.touch()
            roster = 'singer_rosters/' + 'a' * 64 + '.json'
            self.write_seed(base, {roster: {'names': ['Singer'], 'unknown': 'field'}})
            with patch.object(app_paths, 'resource_dir', return_value=base):
                with self.assertRaises(ValueError):
                    app_paths.seed_data(data)
                self.assertEqual(marker.read_bytes(), b'')
                self.assertFalse((data / roster).exists())

    def test_frozen_uses_writable_user_directory(self):
        with patch.object(app_paths,'frozen',return_value=True), patch.dict(os.environ,{'SPOTIFY_LYRICS_HOME':'C:/private-runtime'}):
            self.assertEqual(app_paths.default_data_dir(),Path('C:/private-runtime/data'))
            self.assertEqual(app_paths.runtime_dir(),Path('C:/private-runtime/runtime'))

    def test_frozen_worker_reenters_exe_without_python_arguments(self):
        with patch.object(app_paths,'frozen',return_value=True), patch.object(app_paths.sys,'executable','overlay.exe'):
            for module in ('spotify_controls','spotify_worker','browser_worker'):
                self.assertEqual(app_paths.worker_command(Path('.'),module),['overlay.exe','--worker',module])

    def test_frozen_worker_can_reuse_parent_bundle_without_affecting_detached_workers(self):
        bundle = Path('bundle').resolve()
        system = str(Path('system').resolve())
        inherited = {'PYINSTALLER_RESET_ENVIRONMENT': '1', 'PYTHONPATH': 'custom',
                     '_PYI_APPLICATION_HOME_DIR': str(bundle),
                     'PATH': os.pathsep.join((str(bundle / 'dlls'), system))}
        with (patch.object(app_paths, 'frozen', return_value=True),
              patch.object(app_paths, 'resource_dir', return_value=bundle),
              patch.dict(os.environ, inherited, clear=True)):
            attached = app_paths.worker_environment(bundle, browser=True, reuse_frozen_bundle=True)
            detached = app_paths.worker_environment(bundle)
            self.assertNotIn('PYINSTALLER_RESET_ENVIRONMENT', attached)
            self.assertEqual(attached['_PYI_APPLICATION_HOME_DIR'], str(bundle))
            self.assertNotIn('PYTHONPATH', attached)
            self.assertEqual(attached['PATH'], system)
            self.assertEqual(detached['PYINSTALLER_RESET_ENVIRONMENT'], '1')
            self.assertEqual(os.environ['PYINSTALLER_RESET_ENVIRONMENT'], '1')

    def test_optional_runtime_never_downloads_during_availability_check(self):
        from optional_runtime import audio_installed
        with tempfile.TemporaryDirectory() as folder, patch('optional_runtime.runtime_dir',return_value=Path(folder)):
            self.assertFalse(audio_installed())


if __name__=='__main__':unittest.main()
