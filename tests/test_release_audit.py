import tempfile
import json
import unittest
from pathlib import Path

from release_audit import audit_paths


class ReleaseAuditTests(unittest.TestCase):
    def test_public_lyrics_are_allowed(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            path = base / 'resources' / 'seed_data' / 'library.json'
            path.parent.mkdir(parents=True)
            path.write_text('[{"title":"Song","korean":"번역"}]', encoding='utf-8')
            self.assertEqual(audit_paths(base, [path]), 1)

    def test_private_data_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            path = base / 'data' / 'library.json'
            path.parent.mkdir()
            path.write_text('[]', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'private/runtime'):
                audit_paths(base, [path])

    def test_only_reconstructed_public_song_links_may_seed_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder)
            source=base/'resources/seed_data'
            source.mkdir(parents=True)
            (source/'library.json').write_text('[{"id":"abc"}]',encoding='utf-8')
            path=source/'settings.json'
            safe={'lyric_sources':{'abc':{'user_override':True}},'links':{'Song\0Singer\0Album':'abc'}}
            path.write_text(json.dumps(safe),encoding='utf-8')
            self.assertEqual(audit_paths(base,[path]),1)
            for unsafe in ({**safe,'spotify_authenticated':True},
                           {**safe,'links':{'Song\0Singer\0Album':'unknown'}},
                           {**safe,'lyric_sources':{'abc':{'user_override':1}}},
                           {**safe,'lyric_sources':{'abc':{'user_override':True,'source_folder':'private'}}}):
                path.write_text(json.dumps(unsafe),encoding='utf-8')
                with self.assertRaisesRegex(ValueError,'private/runtime'):
                    audit_paths(base,[path])

    def test_secret_is_not_echoed(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            path = base / 'module.py'
            secret = 'ghp_' + 'a' * 36
            path.write_text('token = ' + repr(secret), encoding='utf-8')
            with self.assertRaises(ValueError) as caught:
                audit_paths(base, [path])
            self.assertNotIn(secret, str(caught.exception))

    def test_home_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            path = base / 'notes.md'
            path.write_text(str(Path.home() / 'Documents'), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'home path'):
                audit_paths(base, [path])

    def test_outside_tree_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            with self.assertRaisesRegex(ValueError, 'outside'):
                audit_paths(base, [base.parent / 'another-file.py'])


if __name__ == '__main__':
    unittest.main()
