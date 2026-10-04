"""Malformed saved files and rejected writes must preserve the last good data."""
from pathlib import Path
import tempfile
import unittest

from lyrics import Line, Song
from storage import Store, atomic_json, validate_times


class StorageIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.store = Store(self.directory)
        self.song = Song('sample', 'Song', 'Artist', 'Album', (Line('花', 'はな', '꽃'),))

    def assert_rejected_unchanged(self, filename, value, read):
        path = self.directory / filename
        atomic_json(path, value)
        before = path.read_bytes()
        with self.assertRaises(ValueError):read()
        self.assertEqual(path.read_bytes(), before)

    def test_settings_wrong_json_container_is_reported_before_ui_initialization(self):
        for value in ([], None, 'settings', True):
            with self.subTest(value=value):
                self.assert_rejected_unchanged('settings.json', value, self.store.settings)

    def test_library_bad_rows_are_reported_without_partial_load_or_rewrite(self):
        valid = self.song.to_dict()
        for value in ({}, [valid, {}], [valid, None], [{**valid, 'title': None}],
                      [{**valid, 'lines': [{'original': '花', 'reading': [], 'korean': '꽃'}]}]):
            with self.subTest(value=value):
                self.assert_rejected_unchanged('library.json', value, self.store.library)

    def test_matching_timing_with_missing_times_is_reported(self):
        self.assert_rejected_unchanged('timings/sample.json',
                                      {'fingerprint': self.song.fingerprint, 'offset': 0},
                                      lambda: self.store.timing(self.song))

    def test_timing_wrong_container_is_reported_and_preserved(self):
        for value in ([], None, 'timing'):
            with self.subTest(value=value):
                self.assert_rejected_unchanged('timings/sample.json', value,
                                              lambda: self.store.timing(self.song))

    def test_invalid_offset_write_cannot_replace_good_manual_timing(self):
        self.store.save_timing(self.song, {'times': [12.5], 'offset': -.25, 'source': 'manual'})
        path = self.directory / 'timings/sample.json'
        before = path.read_bytes()
        for offset in ('0.5', True, None, float('inf'), float('nan'), 10**400):
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                self.store.save_timing(self.song, {'times': [99], 'offset': offset})
            self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.store.timing(self.song)['times'], [12.5])

    def test_oversized_integer_times_report_validation_error(self):
        with self.assertRaises(ValueError):validate_times([10**400], 1)

    def test_boolean_saved_offset_is_not_a_one_second_correction(self):
        self.assert_rejected_unchanged('timings/sample.json',
                                      {'fingerprint': self.song.fingerprint, 'times': [12.5], 'offset': True},
                                      lambda: self.store.timing(self.song))

    def test_minimal_timing_retains_default_offset_and_fingerprint_protection(self):
        self.store.save_timing(self.song, {'times': [12.5]})
        self.assertEqual(self.store.timing(self.song)['times'], [12.5])
        self.assertEqual(self.store.timing(self.song)['offset'], 0)
        other = Song('sample', 'Song', 'Artist', 'Album', (Line('空', '', ''),))
        self.assertEqual(self.store.timing(other)['times'], [None])


if __name__ == '__main__':unittest.main()
