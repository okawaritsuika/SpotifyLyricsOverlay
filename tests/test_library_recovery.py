"""Malformed personal/optional metadata must not destroy saved choices."""
import json
from pathlib import Path
import tempfile
import unittest

from explorer_library import ExplorerLibrary, cached_candidates, spotify_url, group_duplicate_songs
from local_library import LocalLibrary, _duration, _file_id


class LibraryRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_personal_history_and_favorites_validate_members_without_writing(self):
        path = self.root / 'explorer_library.json'
        for field in ('favorites', 'recent'):
            for member in (None, {}, [], True, 42):
                with self.subTest(field=field, member=member):
                    path.write_text(json.dumps({field: ['old', member]}), encoding='utf-8')
                    original = path.read_bytes()
                    with self.assertRaises(ValueError):
                        ExplorerLibrary(self.root)
                    self.assertEqual(path.read_bytes(), original)

    def test_invalid_spotify_id_is_rejected_before_saving(self):
        library = ExplorerLibrary(self.root)
        library.toggle_favorite('saved')
        original = library.path.read_bytes()
        for track in (None, [], 42, {'id': None}, {'id': 42}, {'id': []}):
            with self.subTest(track=track):
                self.assertEqual(spotify_url(track), '')
                with self.assertRaises(ValueError):
                    library.link_spotify('saved', track)
                self.assertEqual(library.path.read_bytes(), original)
                self.assertEqual(library.value['spotify'], {})

    def test_optional_metadata_keeps_valid_candidates_amid_malformed_rows(self):
        valid = {'id': 'a' * 22, 'title': 'Song', 'artists': ['Artist'], 'album': 'Album', 'duration': 123}
        long_valid = {**valid, 'id': 'b' * 22, 'duration': 86401}
        invalid = [None, [], 42, {'track': None}, {'track': []},
                   {'track': {**valid, 'id': None}}, {'track': {**valid, 'artists': 'Artist'}},
                   {'track': {**valid, 'album': []}}, {'track': {**valid, 'duration': '123'}},
                   {'track': {**valid, 'duration': 10 ** 400}}]
        path = self.root / 'artist_metadata.json'
        path.write_text(json.dumps({'entries': invalid + [{'track': valid}, {'track': long_valid}]}), encoding='utf-8')
        original = path.read_bytes()
        result = cached_candidates(self.root, {'title': 'Song'})
        self.assertEqual([row['id'] for row in result], [valid['id'], long_valid['id']])
        self.assertIsNone(result[0]['is_playable'])
        self.assertEqual(path.read_bytes(), original)

    def test_optional_metadata_bad_container_returns_no_candidates(self):
        path = self.root / 'artist_metadata.json'
        for entries in (None, 42, 'invalid', {'track': 'invalid'}):
            with self.subTest(entries=entries):
                path.write_text(json.dumps({'entries': entries}), encoding='utf-8')
                original = path.read_bytes()
                self.assertEqual(cached_candidates(self.root, {'title': 'Song'}), [])
                self.assertEqual(path.read_bytes(), original)

    def test_oversized_duration_cannot_break_duplicate_list_or_local_lookup(self):
        self.assertIsNone(_duration(10 ** 400))
        row = {'id': 'a', 'title': 'Song', 'artists': ['Artist'], 'kind': 'song', 'duration': 10 ** 400}
        self.assertEqual(group_duplicate_songs([row, {**row, 'id': 'b'}]), [[row], [{**row, 'id': 'b'}]])

    def test_invalid_saved_local_duration_is_a_preserving_validation_error(self):
        path = self.root / 'local_library.json'
        music = self.root / 'music'; music.mkdir()
        audio = music / 'Song.wav'
        record = {'id': _file_id(audio), 'path': str(audio), 'title': 'Song', 'artists': [],
                  'album': '', 'duration': 10 ** 400, 'metadata_quality': 'filename'}
        path.write_text(json.dumps({'version': 1, 'files': [record], 'links': {}, 'folders': [str(music)]}))
        original = path.read_bytes()
        with self.assertRaises(ValueError):
            LocalLibrary(self.root)
        self.assertEqual(path.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
