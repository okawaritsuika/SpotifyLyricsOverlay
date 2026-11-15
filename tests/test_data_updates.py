from copy import deepcopy
import hashlib
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

import data_updates
from batch_translation import _document, _encoded
from lyric_annotations import LyricAnnotations
from lyrics import Line, Song
from live_timing import original_signature
from native_timing import NativeTiming
from storage import Store, atomic_json


class DataUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / 'data'
        self.data.mkdir()
        self.song = Song('a' * 24, 'Song', 'Artist', 'Album',
                         (Line('first', '', ''), Line('second', '', '')))
        self.counter = 0

    def pack(self, documents, extra=None):
        documents = deepcopy(documents)
        bodies = {name: json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
                  for name, value in documents.items()}
        bodies['_manifest.json'] = json.dumps({'schema_version': 1, 'files': len(bodies),
                                              'bytes': sum(map(len, bodies.values()))}).encode()
        self.counter += 1
        path = self.root / ('package-' + str(self.counter) + '.zip')
        with ZipFile(path, 'w', compression=ZIP_DEFLATED) as archive:
            for name, body in bodies.items():archive.writestr(name, body)
            if extra:
                for name, body in extra:archive.writestr(name, body)
        return path

    def write(self, name, value):
        atomic_json(self.data / name, value)

    def read(self, name):
        return json.loads((self.data / name).read_text(encoding='utf-8'))

    def clock(self, song=None, times=None, offset=0):
        return {'fingerprint': (song or self.song).fingerprint, 'times': times or [10, 20],
                'offset': offset, 'source': '직접 기록', 'origins': ['manual', 'manual']}

    def stage_apply(self, documents):
        package = self.pack(documents)
        data_updates.stage_package(package, self.data)
        return data_updates.apply_pending(self.data)

    def catalogs(self, stamp, title='Reviewed song'):
        source = 'https://example.com/source'
        return {
            'song_catalog.json': {'schema_version': 1, 'generated_at': stamp, 'sources': [],
                'songs': [{'id': 'official:example', 'title': title, 'artists': ['Singer'],
                           'series': ['765'], 'source_url': source}]},
            'live_catalog.json': {'schema_version': 1, 'coverage': 'partial', 'verified_at': stamp,
                'events': [{'id': 'show', 'title': 'Show', 'date': '2026-01-01', 'series': ['765'],
                            'source_url': source, 'songs': [{'title': title}]}]},
            'imas_db_people.json': {'schema_version': 1, 'verified_at': stamp[:10],
                'generated_at': stamp, 'characters': [{'name': 'Singer'}]},
            'group_credits.json': {'schema_version': 2, 'groups': [], 'members': []},
        }

    def snapshot(self):
        return {path.relative_to(self.data).as_posix(): path.read_bytes()
                for path in self.data.rglob('*.json') if not any(part.startswith('.') for part in path.relative_to(self.data).parts)}

    def test_staging_does_not_change_live_data(self):
        self.write('library.json', [self.song.to_dict()])
        self.write('settings.json', {'font_size': 37, 'token': 'private'})
        self.write('timings/' + self.song.id + '.json', self.clock(times=[10, None]))
        before = self.snapshot()
        package = self.pack({'library.json': [self.song.to_dict()], 'timings/' + self.song.id + '.json': self.clock()})
        summary = data_updates.stage_package(package, self.data)
        self.assertEqual(summary['status'], 'staged')
        self.assertEqual(self.snapshot(), before)
        self.assertTrue((self.data / data_updates.PENDING).is_file())
        self.assertEqual(len(data_updates.validate_package(package)), 3)

    def test_release_edits_apply_on_missing_files_and_preserve_local_edits(self):
        alsong = {'duration': 100, 'provider_id': '123', 'lines': list(self.song.to_dict()['lines']), 'times': [10, 20]}
        released_edits = {
            'singer_edits': {'0': ['Singer'], '1': {'carry': ['Singer']}, '2': []},
            'singer_rosters': ['Singer', 'Guest'],
            'lyric_text_edits': {'0': 'corrected original', '1': {'reading': 'reading', 'korean': ''}},
            'confirmed_alsong': alsong,
        }
        local_edits = {
            'singer_edits': {'0': []},
            'singer_rosters': ['Guest', 'Singer'],
            'lyric_text_edits': {'0': {'korean': 'local translation'}},
            'confirmed_alsong': {**alsong, 'provider_id': '456'},
        }
        existing = 'a' * 64 + '.json'
        missing = 'b' * 64 + '.json'
        for folder, value in local_edits.items():
            self.write(f'{folder}/{existing}', value)
        documents = {f'{folder}/{filename}': value for folder, value in released_edits.items()
                     for filename in (existing, missing)}
        result = self.stage_apply(documents)
        self.assertEqual(result['added_files'], len(released_edits))
        self.assertEqual(result['updated_files'], 0)
        for folder, value in released_edits.items():
            self.assertEqual(self.read(f'{folder}/{missing}'), value)
            self.assertEqual(self.read(f'{folder}/{existing}'), local_edits[folder])

    def test_release_edits_reject_fields_outside_the_runtime_schema(self):
        invalid_edits = {
            'singer_edits': {'0': {'carry': ['Singer'], 'private': 'metadata'}},
            'singer_rosters': {'names': ['Singer'], 'private': 'metadata'},
            'lyric_text_edits': {'0': {'korean': 'translation', 'private': 'metadata'}},
            'confirmed_alsong': {'duration': 100, 'provider_id': '123', 'lines': self.song.to_dict()['lines'],
                                 'times': [10, 20], 'private': 'metadata'},
        }
        for folder, value in invalid_edits.items():
            with self.subTest(folder=folder), self.assertRaises(ValueError):
                data_updates.validate_package(self.pack({f"{folder}/{'a' * 64}.json": value}))

    def test_new_alsong_binding_does_not_shadow_existing_clock(self):
        key = 'Song\0Artist\0Album'
        name = 'confirmed_alsong/' + hashlib.sha256(key.encode()).hexdigest() + '.json'
        rows = self.song.to_dict()['lines']
        self.write('library.json', [self.song.to_dict()])
        self.write('timings/' + self.song.id + '.json', self.clock(times=[5, 10]))
        result = self.stage_apply({name: {'duration': 100, 'provider_id': '123',
                                          'lines': rows, 'times': [10, 20]}})
        self.assertEqual(result['deferred_files'], 1)
        self.assertFalse((self.data / name).exists())

    def test_reviewed_catalogs_replace_legacy_scrape_and_do_not_roll_back(self):
        current = self.catalogs('2027-06-01T00:00:00Z')
        self.write('song_catalog.json', self.catalogs('2028-01-01T00:00:00Z', 'Legacy song')['song_catalog.json'])
        self.write('personal_catalog.json', {'tracks': [{'title': 'Private song'}]})
        private = (self.data / 'personal_catalog.json').read_bytes()
        with patch.object(data_updates, 'resource_dir', return_value=self.root):
            result = self.stage_apply(current)
            self.assertEqual(result['added_files'], 3)
            self.assertEqual(result['updated_files'], 1)
            self.assertEqual(self.read('song_catalog.json')['songs'][0]['title'], 'Reviewed song')
            self.assertEqual(self.read('group_credits.json')[data_updates.CATALOG_RELEASE_STAMP],
                             '2027-06-01T00:00:00+00:00')
            older = self.stage_apply(self.catalogs('2027-05-31T00:00:00+09:00', 'Older song'))
            self.assertEqual(older['unchanged_files'], 4)
            self.assertEqual(self.read('song_catalog.json')['songs'][0]['title'], 'Reviewed song')
        self.assertEqual((self.data / 'personal_catalog.json').read_bytes(), private)

    def test_newer_bundled_catalogs_win_and_group_only_update_has_own_revision(self):
        bundled = self.catalogs('2027-06-01T00:00:00Z')
        for name, value in bundled.items():
            atomic_json(self.root / 'resources' / name, value)
        older = self.catalogs('2027-05-01T00:00:00Z', 'Older song')
        older['group_credits.json']['generated_at'] = '2027-06-02T00:00:00Z'
        with patch.object(data_updates, 'resource_dir', return_value=self.root):
            result = self.stage_apply(older)
        self.assertEqual(result['unchanged_files'], 3)
        self.assertEqual(result['added_files'], 1)
        self.assertFalse((self.data / 'song_catalog.json').exists())
        self.assertEqual(self.read('group_credits.json')[data_updates.CATALOG_RELEASE_STAMP],
                         '2027-06-02T00:00:00+00:00')

    def test_new_app_bundle_takes_precedence_over_older_installed_snapshots(self):
        from explorer_library import people_database_snapshot
        from live_catalog import LiveCatalog
        from song_catalog import SongCatalog

        bundled = self.root / 'resources'
        package = self.catalogs('2027-06-01T00:00:00Z', 'Older song')
        with patch.object(data_updates, 'resource_dir', return_value=self.root):
            self.stage_apply(package)
        for name, value in self.catalogs('2027-06-02T00:00:00Z', 'Bundled song').items():
            atomic_json(bundled / name, value)
        songs = SongCatalog(bundled / 'song_catalog.json', self.data / 'song_catalog.json')
        shows = LiveCatalog(bundled / 'live_catalog.json', self.data / 'live_catalog.json')
        people = people_database_snapshot(bundled, self.data / 'imas_db_people.json')
        self.assertEqual(songs.songs[0]['title'], 'Bundled song')
        self.assertEqual(shows.events[0]['songs'][0]['title'], 'Bundled song')
        self.assertEqual(people['verified_at'], '2027-06-02')

    def test_incomplete_and_invalid_catalog_packages_fail_before_staging(self):
        complete = self.catalogs('2027-06-01T00:00:00Z')
        incomplete = {name: value for name, value in complete.items() if name != 'group_credits.json'}
        invalid = [
            {'song_catalog.json': {**complete['song_catalog.json'], 'songs': []}},
            {'live_catalog.json': {**complete['live_catalog.json'], 'events': [{}]}},
            {'imas_db_people.json': {**complete['imas_db_people.json'], 'characters': [42]}},
            {'group_credits.json': {**complete['group_credits.json'], 'generated_at': 'unknown'}},
            {'personal_catalog.json': {'tracks': []}},
        ]
        for documents in [incomplete, *({**complete, **bad} for bad in invalid)]:
            with self.subTest(documents=documents.keys()):
                with self.assertRaises(ValueError):
                    data_updates.stage_package(self.pack(documents), self.data)
        self.assertFalse((self.data / data_updates.PENDING).exists())

    def test_blank_translation_uses_annotations_and_does_not_invalidate_manual_clock(self):
        original = Song(self.song.id, 'Song', 'Artist', 'Album', (Line('first', '', 'my translation'), self.song.lines[1]))
        incoming = Song(self.song.id, 'Song', 'Artist', 'Album', (Line('first', 'new reading', 'different translation'), Line('second', '', 'new second')))
        self.write('library.json', [original.to_dict()])
        self.write('timings/' + original.id + '.json', self.clock(original, [10, None]))
        before_library = (self.data / 'library.json').read_bytes()
        result = self.stage_apply({'library.json': [incoming.to_dict()],
                                  'timings/' + original.id + '.json': self.clock(incoming)})
        self.assertEqual(result['status'], 'applied')
        self.assertEqual((self.data / 'library.json').read_bytes(), before_library)
        display = LyricAnnotations(Store(self.data)).display_lines(original)
        self.assertEqual([line.korean for line in display], ['my translation', 'new second'])
        clock = Store(self.data).timing(original)
        self.assertEqual(clock['fingerprint'], original.fingerprint)
        self.assertEqual(clock['times'], [10, 20])

    def test_existing_offset_or_timestamp_conflict_defers_entire_clock(self):
        for existing, incoming in ((self.clock(times=[10, None], offset=.5), self.clock()),
                                   (self.clock(times=[11, None]), self.clock())):
            with self.subTest(existing=existing):
                self.write('library.json', [self.song.to_dict()])
                name = 'timings/' + self.song.id + '.json'
                self.write(name, existing)
                before = (self.data / name).read_bytes()
                result = self.stage_apply({'library.json': [self.song.to_dict()], name: incoming})
                self.assertEqual((self.data / name).read_bytes(), before)
                self.assertGreaterEqual(result['deferred_files'], 1)
                # Different package bytes are needed for the next subtest.
                (self.data / data_updates.RECEIPTS).unlink()

    def test_clock_update_preserves_explicit_local_clear_and_fills_other_gap(self):
        self.write('library.json', [self.song.to_dict()])
        name = 'timings/' + self.song.id + '.json'
        self.write(name, {**self.clock(times=[None, None]), 'origins': [None, None], 'cleared_rows': [0]})
        self.stage_apply({name: self.clock()})
        self.assertEqual(self.read(name)['cleared_rows'], [0])
        self.assertEqual(self.read(name)['times'], [None, 20])

    def test_conflicting_alsong_row_layout_defers_timing_update(self):
        key = 'Song\0Artist\0Album'
        identity = 'memory-' + hashlib.sha256(key.encode()).hexdigest()[:24]
        name = 'live_timings/' + hashlib.sha256(identity.encode()).hexdigest() + '.json'
        saved = {'signature': original_signature(self.song), 'times': [None, None],
                 'added_alsong_rows': [{'before': 0, 'text': 'local row', 'time': None, 'cleared': True}]}
        incoming = {'signature': saved['signature'], 'times': [10, 20],
                    'added_alsong_rows': [{'before': 1, 'text': 'shared row', 'time': None, 'cleared': True}]}
        self.write('library.json', [self.song.to_dict()])
        self.write(name, saved)
        result = self.stage_apply({name: incoming})
        self.assertEqual(result['deferred_files'], 1)
        self.assertEqual(self.read(name), saved)

    def test_incompatible_row_count_or_originals_cannot_replace_saved_work(self):
        edited = Song(self.song.id, 'Song', 'Artist', 'Album', (Line('user correction', '', 'mine'),))
        self.write('library.json', [edited.to_dict()])
        self.write('timings/' + edited.id + '.json', {'fingerprint': edited.fingerprint, 'times': [13], 'offset': 2})
        before = self.snapshot()
        result = self.stage_apply({'library.json': [self.song.to_dict()], 'timings/' + self.song.id + '.json': self.clock()})
        self.assertEqual(self.snapshot(), before)
        self.assertGreaterEqual(result['deferred_files'], 1)

    def test_existing_user_override_and_settings_are_never_overwritten(self):
        self.write('library.json', [self.song.to_dict()])
        settings = {'font_size': 50, 'lyric_sources': {self.song.id: {'user_override': True, 'url': 'my source'}},
                    'links': {'Song\0Artist\0Album': self.song.id}, 'deleted_lyrics': [], 'email': 'private'}
        self.write('settings.json', settings)
        self.write('timings/' + self.song.id + '.json', self.clock(times=[10, None]))
        incoming = self.song.to_dict();incoming['lines'][0]['korean'] = 'new translation'
        documents = {'library.json': [incoming], 'timings/' + self.song.id + '.json': self.clock(Song.from_dict(incoming)),
                     'settings.json': {'lyric_sources': {self.song.id: {'user_override': True}}, 'links': {'Song\0Artist\0Album': self.song.id}}}
        before = self.snapshot()
        self.stage_apply(documents)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.data / 'lyric_annotations').exists())

    def test_deleted_lyrics_are_not_reintroduced(self):
        self.write('library.json', [])
        self.write('settings.json', {'deleted_lyrics': ['Song\0Artist\0Album']})
        result = self.stage_apply({'library.json': [self.song.to_dict()],
                                  'timings/' + self.song.id + '.json': self.clock()})
        self.assertEqual(self.read('library.json'), [])
        self.assertFalse((self.data / 'timings' / (self.song.id + '.json')).exists())
        self.assertGreaterEqual(result['deferred_files'], 1)

    def test_shared_translation_map_and_track_index_keep_existing_and_add_new_entries(self):
        history = _encoded(_document({'title': 'Song', 'artist': 'Artist', 'album': 'Album',
                                      'lines': [dict(original='first', reading='', korean=''), dict(original='second', reading='', korean='')]}))
        identifier = history['id']
        self.write('ai_translations/imports.json', {'version': 1, 'documents': {identifier: {'1': 'my translation'}}})
        old_track = {'id': 'A' * 22, 'title': 'A', 'album': 'Album', 'artists': ['Artist'], 'duration': 100}
        new_track = {**old_track, 'id': 'B' * 22, 'title': 'B'}
        self.write('playlist_lyrics/index.json', {'schema_version': 1, 'tracks': {old_track['id']: old_track}})
        new_lyrics = {'schema_version': 1, 'track': new_track, 'lines': self.song.to_dict()['lines'], 'direct': None, 'sources': []}
        self.stage_apply({'ai_translations/history/' + identifier + '.json': history,
                          'ai_translations/imports.json': {'version': 1, 'documents': {identifier: {'1': 'overwrite', '2': 'new translation'}}},
                          'playlist_lyrics/index.json': {'schema_version': 1, 'tracks': {new_track['id']: new_track}},
                          'playlist_lyrics/tracks/' + new_track['id'] + '.json': new_lyrics})
        self.assertEqual(self.read('ai_translations/imports.json')['documents'][identifier], {'1': 'my translation', '2': 'new translation'})
        self.assertEqual(set(self.read('playlist_lyrics/index.json')['tracks']), {old_track['id'], new_track['id']})

    def test_interrupt_replays_additions_and_same_package_is_not_applied_twice(self):
        package = self.pack({'library.json': [self.song.to_dict()], 'timings/' + self.song.id + '.json': self.clock()})
        data_updates.stage_package(package, self.data)
        original = data_updates.atomic_json
        def interrupted(path, value):
            if Path(path).parent.name == 'timings':raise OSError('simulated power loss')
            return original(path, value)
        with patch.object(data_updates, 'atomic_json', side_effect=interrupted):
            with self.assertRaises(OSError):data_updates.apply_pending(self.data)
        self.assertTrue((self.data / data_updates.PENDING).exists())
        self.assertEqual(Store(self.data).library(), [self.song])
        result = data_updates.apply_pending(self.data)
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(Store(self.data).timing(self.song)['times'], [10, 20])
        before = self.snapshot()
        self.assertEqual(data_updates.stage_package(package, self.data)['status'], 'already_applied')
        self.assertEqual(data_updates.apply_pending(self.data)['status'], 'none')
        self.assertEqual(self.snapshot(), before)

    def test_corrupt_current_json_aborts_before_any_live_write(self):
        path = self.data / 'ai_translations/imports.json';path.parent.mkdir()
        path.write_bytes(b'{broken')
        self.write('library.json', [])
        package = self.pack({'library.json': [self.song.to_dict()], 'ai_translations/imports.json': {'version': 1, 'documents': {}}})
        data_updates.stage_package(package, self.data)
        before = self.snapshot()
        with self.assertRaises(ValueError):data_updates.apply_pending(self.data)
        self.assertEqual(self.snapshot(), before)

    def test_staged_archive_tampering_preserves_every_live_file(self):
        self.write('library.json', [])
        package = self.pack({'library.json': [self.song.to_dict()]})
        staged = data_updates.stage_package(package, self.data)
        (self.data / data_updates.STAGING / (staged['pack_hash'] + '.zip')).write_bytes(b'not a zip')
        before = self.snapshot()
        with self.assertRaises(ValueError):data_updates.apply_pending(self.data)
        self.assertEqual(self.snapshot(), before)

    def test_archive_paths_links_duplicate_entries_unknown_fields_and_size_bombs_are_rejected(self):
        self.write('library.json', [])
        before = self.snapshot()
        for name in ('../outside.json', '/absolute.json', 'C:/private.json', 'library.json/../settings.json',
                     'unknown.json', 'browser_profile/cookies.json', 'timings\\bad.json'):
            with self.subTest(name=name):
                package = self.pack({}, [(name, b'{}')])
                with self.assertRaises(ValueError):data_updates.stage_package(package, self.data)
        info = ZipInfo('library.json');info.create_system = 3;info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaises(ValueError):data_updates.stage_package(self.pack({}, [(info, b'outside')]), self.data)
        with self.assertRaises(ValueError):data_updates.stage_package(self.pack({'library.json': []}, [('library.json', b'[]')]), self.data)
        poisoned = self.song.to_dict();poisoned['lines'][0]['access_token'] = 'secret'
        with self.assertRaises(ValueError):data_updates.stage_package(self.pack({'library.json': [poisoned]}), self.data)
        with patch.object(data_updates, 'MAX_TOTAL_BYTES', 16):
            with self.assertRaises(ValueError):data_updates.stage_package(self.pack({'library.json': [self.song.to_dict()]}), self.data)
        with patch.object(data_updates, 'MAX_RATIO', 1):
            with self.assertRaises(ValueError):data_updates.stage_package(self.pack({'library.json': [self.song.to_dict()]}), self.data)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.data / data_updates.PENDING).exists())

    def test_non_dictionary_payloads_and_boolean_versions_raise_value_error(self):
        for documents in ({'ai_translations/imports.json': []},
                          {'ai_translations/imports.json': {'version': True, 'documents': {}}},
                          {'wiki_documents.json': {'a' * 64: []}}):
            with self.subTest(documents=documents):
                with self.assertRaises(ValueError):data_updates.stage_package(self.pack(documents), self.data)

    def test_invalid_receipt_entry_fails_cleanly_before_removing_pending_or_live_data(self):
        digest = 'a' * 64
        self.write(data_updates.PENDING, {'version': 1, 'pack_hash': digest})
        self.write(data_updates.RECEIPTS, {'version': 1, 'applied': {digest: 7}})
        self.write('library.json', [self.song.to_dict()])
        before = self.snapshot()
        with self.assertRaises(ValueError):data_updates.apply_pending(self.data)
        self.assertTrue((self.data / data_updates.PENDING).exists())
        self.assertEqual(self.snapshot(), before)

    def test_modern_native_clock_cannot_shadow_conflicting_legacy_clock(self):
        self.write('library.json', [self.song.to_dict()])
        legacy = hashlib.sha256((self.song.id + self.song.fingerprint).encode()).hexdigest()
        modern = hashlib.sha256((self.song.id + original_signature(self.song)).encode()).hexdigest()
        self.write('native_timings/' + legacy + '.json', {'times': [5, 10], 'duration': 100})
        self.assertEqual(NativeTiming(self.data).select(self.song, 100), [5, 10])
        result = self.stage_apply({'library.json': [self.song.to_dict()],
                                  'native_timings/' + modern + '.json': {'times': [6, 12], 'duration': 100}})
        self.assertEqual(NativeTiming(self.data).select(self.song, 100), [5, 10])
        self.assertFalse((self.data / 'native_timings' / (modern + '.json')).exists())
        self.assertGreaterEqual(result['deferred_files'], 1)

    def test_modern_native_clock_may_fill_gaps_while_retaining_legacy_stamps(self):
        self.write('library.json', [self.song.to_dict()])
        legacy = hashlib.sha256((self.song.id + self.song.fingerprint).encode()).hexdigest()
        modern = hashlib.sha256((self.song.id + original_signature(self.song)).encode()).hexdigest()
        self.write('native_timings/' + legacy + '.json', {'times': [5, None], 'duration': 100})
        self.stage_apply({'library.json': [self.song.to_dict()],
                          'native_timings/' + modern + '.json': {'times': [5, 12], 'duration': 100}})
        self.assertEqual(NativeTiming(self.data).select(self.song, 100), [5, 12])
        self.assertEqual(self.read('native_timings/' + legacy + '.json')['times'], [5, None])

    def test_unknown_new_native_hash_is_deferred_when_local_native_data_exists(self):
        self.write('native_timings/' + 'a' * 64 + '.json', {'times': [5, 10], 'duration': 100})
        result = self.stage_apply({'native_timings/' + 'b' * 64 + '.json': {'times': [6, 12], 'duration': 100}})
        self.assertGreaterEqual(result['deferred_files'], 1)
        self.assertFalse((self.data / 'native_timings' / ('b' * 64 + '.json')).exists())

    def test_gap_in_provider_clock_is_added_without_replacing_its_fixed_stamp(self):
        track = {'id': 'A' * 22, 'title': 'Song', 'album': 'Album', 'artists': ['Artist'], 'duration': 100}
        rows = self.song.to_dict()['lines']
        direct = {'source': 'Spotify', 'provider_id': track['id'], 'lines': rows, 'times': [10, None]}
        current = {'schema_version': 1, 'track': track, 'lines': rows, 'direct': direct, 'sources': []}
        self.write('playlist_lyrics/tracks/' + track['id'] + '.json', current)
        incoming = deepcopy(current);incoming['direct']['times'][1] = 20
        self.stage_apply({'playlist_lyrics/tracks/' + track['id'] + '.json': incoming,
                          'playlist_lyrics/index.json': {'schema_version': 1, 'tracks': {track['id']: track}}})
        self.assertEqual(self.read('playlist_lyrics/tracks/' + track['id'] + '.json')['direct']['times'], [10, 20])

    def test_new_clock_namespace_cannot_override_an_existing_saved_recording_clock(self):
        for kind in ('native', 'live', 'cache'):
            with self.subTest(kind=kind):
                self.data = self.root / kind;self.data.mkdir()
                self.write('library.json', [self.song.to_dict()])
                saved = {**self.clock(times=[5, 10]), 'origins': ['audio', 'audio'],
                         'audio_finalized': True, 'source': '음원 분석'}
                self.write('timings/' + self.song.id + '.json', saved)
                documents = {'library.json': [self.song.to_dict()]}
                key = 'Song\0Artist\0Album'
                if kind == 'native':
                    identity = hashlib.sha256((self.song.id + original_signature(self.song)).encode()).hexdigest()
                    name = 'native_timings/' + identity + '.json'
                    documents[name] = {'times': [6, 12], 'duration': 100}
                elif kind == 'live':
                    song_id = 'memory-' + hashlib.sha256(key.encode()).hexdigest()[:24]
                    identity = hashlib.sha256(song_id.encode()).hexdigest()
                    name = 'live_timings/' + identity + '.json'
                    documents[name] = {'times': [6, 12], 'signature': original_signature(self.song), 'offset': 0}
                else:
                    name = 'lyrics_cache/' + hashlib.sha256(key.encode()).hexdigest() + '.json'
                    rows = self.song.to_dict()['lines'];rows[0]['korean'] = 'new translation'
                    documents[name] = {'version': 1, 'key': key, 'duration': 100, 'saved_at': 0, 'lines': rows,
                        'direct': {'source': 'Spotify', 'provider_id': 'A' * 22, 'lines': rows, 'times': [6, 12]}}
                result = self.stage_apply(documents)
                self.assertEqual(Store(self.data).timing(self.song), saved)
                self.assertGreaterEqual(result['deferred_files'], 1)
                if kind == 'cache':
                    self.assertIsNone(self.read(name)['direct'])
                    self.assertEqual(self.read(name)['lines'][0]['korean'], 'new translation')
                else:self.assertFalse((self.data / name).exists())

    def test_existing_provider_clock_blocks_a_new_higher_priority_manual_clock(self):
        self.write('library.json', [self.song.to_dict()])
        key = 'Song\0Artist\0Album';name = 'lyrics_cache/' + hashlib.sha256(key.encode()).hexdigest() + '.json'
        rows = self.song.to_dict()['lines']
        self.write(name, {'version': 1, 'key': key, 'duration': 100, 'saved_at': 0, 'lines': rows,
                         'direct': {'source': 'Spotify', 'provider_id': 'A' * 22, 'lines': rows, 'times': [5, 10]}})
        result = self.stage_apply({'library.json': [self.song.to_dict()],
                                  'timings/' + self.song.id + '.json': self.clock(times=[6, 12])})
        self.assertGreaterEqual(result['deferred_files'], 1)
        self.assertFalse((self.data / 'timings' / (self.song.id + '.json')).exists())
        self.assertEqual(self.read(name)['direct']['times'], [5, 10])

    def test_unrelated_unknown_clock_does_not_block_a_proven_new_recording(self):
        self.write('native_timings/' + 'a' * 64 + '.json', {'times': [5, 10], 'duration': 100})
        key = 'New song\0New artist\0New album'
        identity = 'memory-' + hashlib.sha256(key.encode()).hexdigest()[:24]
        song = Song(identity, 'New song', 'New artist', 'New album', self.song.lines)
        modern = hashlib.sha256((song.id + original_signature(song)).encode()).hexdigest()
        cache_name = 'lyrics_cache/' + hashlib.sha256(key.encode()).hexdigest() + '.json'
        rows = self.song.to_dict()['lines']
        self.stage_apply({cache_name: {'version': 1, 'key': key, 'duration': 100, 'saved_at': 0, 'lines': rows,
                         'direct': {'source': 'Spotify', 'provider_id': 'A' * 22, 'lines': rows, 'times': [6, 12]}},
                         'native_timings/' + modern + '.json': {'times': [6, 12], 'duration': 100}})
        self.assertEqual(self.read(cache_name)['direct']['times'], [6, 12])
        self.assertEqual(NativeTiming(self.data).select(song, 100), [6, 12])
        self.assertEqual(self.read('native_timings/' + 'a' * 64 + '.json')['times'], [5, 10])


if __name__ == '__main__':unittest.main()
