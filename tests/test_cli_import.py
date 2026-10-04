"""Command-line imports have the same additive semantics as the editor."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app import run
from lyrics import Line, Song
from storage import Store


class CommandLineImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.store = Store(self.directory)
        self.saved = Song('saved', 'Saved Song', 'Artist', 'Album', (Line('空', 'そら', '하늘'),))
        self.imported = Song('imported', 'New Song', 'Artist', 'Album', (Line('花', 'はな', '꽃'),))
        self.store.save_library([self.saved])
        self.store.save_settings({'links': {'old-playback': 'saved'}, 'opacity': .75})
        self.store.save_timing(self.saved, {'times': [12], 'offset': -.25, 'source': 'manual'})
        self.args = SimpleNamespace(data_dir=self.directory, import_folder=self.directory / 'incoming',
                                    no_media=True, dictionary=False)

    def invoke(self, imported):
        with patch('app.import_folder', return_value=imported), patch('app.tk.Tk'), patch('app.LyricsApp'):
            run(self.args)

    def test_import_keeps_other_saved_songs_and_manual_timing(self):
        timing_path = self.directory / 'timings/saved.json'
        before = timing_path.read_bytes()
        self.invoke([self.imported])
        self.assertEqual(self.store.library(), [self.saved, self.imported])
        self.assertEqual(timing_path.read_bytes(), before)
        self.assertEqual(self.store.settings()['links'], {'old-playback': 'saved'})
        self.assertEqual(self.store.settings()['opacity'], .75)

    def test_reimport_updates_same_id_once_and_keeps_unrelated_song(self):
        self.store.save_library([self.saved, self.imported])
        changed = Song('imported', 'New Song', 'Artist', 'Album', (Line('花が咲く', '', '꽃이 피다'),))
        self.invoke([changed])
        self.assertEqual(self.store.library(), [self.saved, changed])

    def test_invalid_import_preserves_all_saved_data(self):
        before = {path: path.read_bytes() for path in self.directory.rglob('*.json')}
        with patch('app.import_folder', side_effect=ValueError('Invalid document')), self.assertRaises(ValueError):
            run(self.args)
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_invalid_settings_cannot_leave_a_partially_imported_library(self):
        settings_path = self.directory / 'settings.json'
        settings_path.write_text('{broken', encoding='utf-8')
        before = {path: path.read_bytes() for path in self.directory.rglob('*.json')}
        with self.assertRaises(ValueError):self.invoke([self.imported])
        self.assertEqual({path: path.read_bytes() for path in before}, before)


if __name__ == '__main__':unittest.main()
