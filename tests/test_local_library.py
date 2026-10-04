"""Only temporary audio fixtures are scanned; no player or network is contacted."""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

import local_library
from local_library import LocalLibrary


class LocalLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.folder = self.root / 'music';self.folder.mkdir()
        self.library = LocalLibrary(self.root / 'data')

    def tearDown(self):self.temp.cleanup()

    def audio(self, name='Song.wav', folder=None):
        path = (folder or self.folder) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), 'wb') as output:
            output.setnchannels(1);output.setsampwidth(2);output.setframerate(8000)
            output.writeframes(b'\0\0' * 8000)
        return path

    def test_recursive_wav_scan_is_read_only_and_survives_restart(self):
        path = self.audio('disc/01 - Song.wav')
        outside = self.audio('Outside.wav', self.root)
        before = hashlib.sha256(path.read_bytes()).digest()
        progress = []
        with patch.object(local_library, 'mutagen', None):
            stats = self.library.scan(self.folder, progress.append)
        self.assertEqual(stats['added'], 1)
        self.assertEqual(stats['errors'], 0)
        self.assertFalse(stats['metadata_available'])
        record = self.library.files[0]
        self.assertEqual(record['path'], str(path))
        self.assertEqual(record['title'], 'Song')
        self.assertEqual(record['artists'], [])
        self.assertEqual(record['duration'], 1)
        self.assertEqual(record['metadata_quality'], 'filename')
        self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), before)
        self.assertNotIn(str(outside), str(self.library.files))
        self.assertGreaterEqual(len(progress), 2)
        self.assertEqual(LocalLibrary(self.root / 'data').files, self.library.files)

    @unittest.skipIf(local_library.mutagen is None, 'mutagen is optional')
    def test_actual_id3_tags_and_multiple_artists_are_read_from_wav(self):
        from mutagen.id3 import TALB, TIT2, TPE1
        from mutagen.wave import WAVE
        path = self.audio()
        audio = WAVE(path);audio.add_tags()
        audio.tags.add(TIT2(encoding=3, text=['Song (Chihaya Ver.)']))
        audio.tags.add(TPE1(encoding=3, text=['如月千早', '今井麻美']))
        audio.tags.add(TALB(encoding=3, text=['THE IDOLM@STER']))
        audio.save()
        before = path.read_bytes()
        self.library.scan(self.folder)
        record = self.library.files[0]
        self.assertEqual(record['title'], 'Song (Chihaya Ver.)')
        self.assertEqual(record['artists'], ['如月千早', '今井麻美'])
        self.assertEqual(record['album'], 'THE IDOLM@STER')
        self.assertEqual(record['metadata_quality'], 'tags')
        self.assertAlmostEqual(record['duration'], 1)
        self.assertEqual(path.read_bytes(), before)

    def test_damaged_files_do_not_abort_other_files(self):
        self.audio()
        (self.folder / 'broken.wav').write_bytes(b'not an audio stream')
        (self.folder / 'empty.mp3').write_bytes(b'')
        (self.folder / 'readme.txt').write_text('ignored')
        with patch.object(local_library, 'mutagen', None):stats = self.library.scan(self.folder)
        self.assertEqual(len(self.library.files), 1)
        self.assertEqual(stats['scanned'], 3)
        self.assertEqual(stats['errors'], 1)
        self.assertEqual(stats['skipped'], 2)

    def test_cancellation_preserves_previous_index_links_and_bytes(self):
        self.audio()
        self.library.scan(self.folder)
        file_id = self.library.files[0]['id']
        self.library.link('imas-song', file_id)
        original = self.library.path.read_bytes()
        other = self.root / 'other';self.audio('Another.wav', other)
        event = threading.Event()
        def progress(stats):
            if stats['scanned']:event.set()
        result = self.library.scan(other, progress, event)
        self.assertTrue(result['cancelled'])
        self.assertEqual(self.library.path.read_bytes(), original)
        self.assertEqual(self.library.for_song('imas-song')[0]['id'], file_id)
        self.assertEqual(len(self.library.files), 1)

    def test_already_cancelled_scan_does_not_create_database(self):
        self.audio()
        result = self.library.scan(self.folder, cancel=lambda: True)
        self.assertTrue(result['cancelled'])
        self.assertFalse(self.library.path.exists())

    def test_rescan_preserves_other_roots_links_and_missing_file_records(self):
        first = self.audio()
        self.library.scan(self.folder)
        file_id = self.library.files[0]['id'];self.library.link('imas-song', file_id)
        other = self.root / 'other';self.audio('Other.wav', other)
        self.library.scan(other)
        first.unlink()
        self.library.scan(self.folder)
        reopened = LocalLibrary(self.root / 'data')
        self.assertEqual(len(reopened.files), 2)
        self.assertFalse(reopened.for_song('imas-song')[0]['exists'])
        reopened.unlink('imas-song')
        self.assertEqual(LocalLibrary(self.root / 'data').for_song('imas-song'), [])

    def test_link_is_explicit_idempotent_and_allows_multiple_files(self):
        self.audio();self.audio('Second.wav')
        self.library.scan(self.folder)
        first, second = self.library.files
        self.assertFalse(self.library.links)
        self.library.link('song', first['id']);self.library.link('song', first['id'])
        self.library.link('song', second['id'])
        self.assertEqual(len(self.library.for_song('song')), 2)
        with self.assertRaises(ValueError):self.library.link('song', 'unknown')
        with self.assertRaises(ValueError):self.library.link('', first['id'])

    def test_file_count_and_directory_entry_limits_are_bounded(self):
        for index in range(4):self.audio(f'Song {index}.wav')
        with patch.object(local_library, 'MAX_FILES', 2):result = self.library.scan(self.folder)
        self.assertTrue(result['truncated'])
        self.assertEqual(len(self.library.files), 2)
        other = LocalLibrary(self.root / 'limited')
        with patch.object(local_library, 'MAX_ENTRIES', 1):result = other.scan(self.folder)
        self.assertTrue(result['truncated'])
        self.assertEqual(len(other.files), 1)

    def test_reparse_subdirectory_is_never_entered(self):
        self.audio()
        blocked = self.folder / 'junction';self.audio('Outside.wav', blocked)
        plain = local_library._plain_path
        with patch.object(local_library, '_plain_path', side_effect=lambda path: None if path == blocked else plain(path)):
            result = self.library.scan(self.folder)
        self.assertEqual(len(self.library.files), 1)
        self.assertEqual(result['skipped'], 1)

    def test_actual_symlink_to_other_folder_is_not_followed(self):
        outside = self.root / 'outside';self.audio('Outside.wav', outside)
        link = self.folder / 'link'
        try:link.symlink_to(outside, target_is_directory=True)
        except OSError as error:self.skipTest(str(error))
        result = self.library.scan(self.folder)
        self.assertEqual(self.library.files, [])
        self.assertEqual(result['skipped'], 1)

    def test_file_in_replaced_reparse_parent_is_not_inspected_or_opened(self):
        path = self.audio('disc/Song.wav')
        self.library.scan(self.folder)
        record = self.library.files[0];self.library.link('song', record['id'])
        plain = local_library._plain_path
        def inspect(candidate):
            if candidate == path.parent:return None
            if candidate == path:raise AssertionError('must not inspect a file beyond the reparse parent')
            return plain(candidate)
        with patch.object(local_library, '_plain_path', side_effect=inspect), \
                patch.object(local_library.os, 'startfile', create=True) as start:
            self.assertFalse(self.library.for_song('song')[0]['exists'])
            with self.assertRaises(ValueError):self.library.open_file(record['id'])
            self.assertFalse(start.called)

    def test_invalid_folder_and_corrupt_database_are_not_silently_overwritten(self):
        with self.assertRaises(ValueError):self.library.scan(self.root / 'missing')
        self.library.path.parent.mkdir();self.library.path.write_text('{broken')
        before = self.library.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'local_library.json'):LocalLibrary(self.library.path.parent)
        self.assertEqual(self.library.path.read_bytes(), before)

    def test_write_failure_keeps_previous_memory_and_mapping(self):
        self.audio();self.library.scan(self.folder)
        before = self.library.path.read_bytes()
        with patch.object(local_library, 'atomic_json', side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError):self.library.link('song', self.library.files[0]['id'])
        self.assertEqual(self.library.links, {})
        self.assertEqual(self.library.path.read_bytes(), before)

    def test_candidates_keep_version_suffix_and_reject_other_artists_or_lengths(self):
        for title in ('Song', 'Song (Live)', 'Song (Chihaya Ver.)'):
            self.audio(title + '.wav')
        with patch.object(local_library, 'mutagen', None):self.library.scan(self.folder)
        for record in self.library.files:
            record.update(artists=['Singer'], album='Album', duration=180, metadata_quality='tags')
        song = {'title': 'Song', 'artists': ['Singer'], 'album': 'Album', 'duration': 181}
        candidates = self.library.candidates(song)
        self.assertEqual([item['title'] for item in candidates], ['Song'])
        self.assertEqual(candidates[0]['score'], 100)
        self.assertTrue(candidates[0]['reasons'])
        self.assertEqual(self.library.candidates({**song, 'artists': ['Different Singer']}), [])
        self.assertEqual(self.library.candidates({**song, 'duration': 220}), [])
        self.assertEqual(self.library.candidates({**song, 'title': 'Song (Miki Ver.)'}), [])
        self.assertEqual(len(self.library.candidates(SimpleNamespace(title='Song', artist='Singer', album='Album'))), 1)

    def test_filename_candidates_are_marked_uncertain_and_never_linked(self):
        self.audio()
        with patch.object(local_library, 'mutagen', None):self.library.scan(self.folder)
        result = self.library.candidates({'title': 'Song'})
        self.assertEqual(result[0]['score'], 30)
        self.assertIn('파일명', ' '.join(result[0]['reasons']))
        self.assertFalse(self.library.links)

    def test_open_file_only_opens_explicit_existing_indexed_audio(self):
        path = self.audio()
        with patch.object(local_library.os, 'startfile', create=True) as start:
            self.library.scan(self.folder)
            self.assertFalse(start.called)
            with self.assertRaises(ValueError):self.library.open_file(str(path))
            record = self.library.files[0]
            self.library.open_file(record['id'])
            start.assert_called_once_with(str(path), 'open')
            path.unlink()
            with self.assertRaises(ValueError):self.library.open_file(record['id'])
            self.assertEqual(start.call_count, 1)


if __name__ == '__main__':unittest.main()
