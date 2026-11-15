import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from song_catalog import (PERSONAL_CATALOG_NAME, SongCatalog, add_spotify_playlist,
                          parse_personal_song_json, read_catalog, refresh_catalog)
from catalog_sources import fetch_musicbrainz


def song(identifier='s1', title='Twinkle way', **changes):
    value = {'id': identifier, 'title': title, 'artists': ['イルミネーションスターズ'],
             'series': ['shiny'], 'source_url': 'https://musicbrainz.org/recording/example',
             'source_id': 'musicbrainz', 'performer_aliases': ['일루미네이션 스타즈', '사쿠라기 마노'],
             'kind': 'song'}
    value.update(changes)
    return value


def write_catalog(path, songs=None, **changes):
    value = {'schema_version': 1, 'generated_at': '2026-10-02',
             'sources': [{'id': 'musicbrainz', 'title': 'MusicBrainz'}],
             'songs': [song()] if songs is None else songs}
    value.update(changes)
    Path(path).write_text(json.dumps(value), encoding='utf-8')
    return value


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.base = self.root / 'bundled.json'
        self.update = self.root / 'update.json'
        write_catalog(self.base)

    def test_search_matches_korean_members_and_case_width(self):
        catalog = SongCatalog(self.base)
        self.assertEqual(len(catalog.search('Ｔｗｉｎｋｌｅ')), 1)
        self.assertEqual(len(catalog.search(performer='사쿠라기 마노')), 1)
        self.assertEqual(len(catalog.search(series='shiny')), 1)
        self.assertEqual(catalog.search(series='million'), [])

    def test_personal_songs_keep_versions_and_survive_public_snapshot_updates(self):
        path = self.root / PERSONAL_CATALOG_NAME
        catalog = SongCatalog(self.base, self.update, personal_path=path)
        fields = dict(title='Twinkle way', artists=['イルミネーションスターズ'], album='First Edition',
                      series='shiny', release_date='2026-11-15')
        first = catalog.add_personal_song(fields)
        repeat = catalog.add_personal_song({**fields, 'title': 'Ｔｗｉｎｋｌｅ way'})
        alternate = catalog.add_personal_song({**fields, 'album': 'Another Edition'})
        self.assertTrue(first['added'])
        self.assertFalse(repeat['added'])
        self.assertEqual(repeat['song']['id'], first['song']['id'])
        self.assertNotEqual(first['song']['id'], alternate['song']['id'])
        self.assertEqual(first['song']['first_release_date'], '2026-11-15')
        self.assertEqual(catalog.search('another edition')[0]['id'], alternate['song']['id'])

        write_catalog(self.update, [song('s2', 'New public song')])
        restored = SongCatalog(self.base, self.update, personal_path=path)
        self.assertEqual({row['id'] for row in restored.songs},
                         {'s1', 's2', first['song']['id'], alternate['song']['id']})
        self.assertEqual({row['id'] for row in read_catalog(self.update)['songs']}, {'s2'})

    def test_personal_import_canonicalizes_spotify_and_reuses_identical_recording(self):
        path = self.root / PERSONAL_CATALOG_NAME
        url = 'https://open.spotify.com/track/' + 'A' * 22
        write_catalog(self.base, [song(spotify_url=url)])
        catalog = SongCatalog(self.base, personal_path=path)
        pasted = '```json\n' + json.dumps(dict(title='Track', artists=['Artist'],
                                                spotify_url=url + '?si=ref')) + '\n```'
        fields = parse_personal_song_json(pasted)
        self.assertEqual(fields['spotify_url'], url)
        self.assertEqual(catalog.add_personal_song(fields), {'song': catalog.songs[0], 'added': False})
        self.assertFalse(path.exists())

    def test_invalid_personal_import_and_damaged_store_preserve_original(self):
        for value in ('[{"title":"Song"}]', '{"title":"Song","title":"Other","artists":["Artist"]}',
                      '{"title":"Song","artists":["Artist"],"release_date":"2026-02-31"}',
                      '{"title":"Song","artists":["Artist"],"source_url":"https://example.test:bad"}',
                      '{"title":"Song","artists":["Artist"],"id":"s1"}'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_personal_song_json(value)
        path = self.root / PERSONAL_CATALOG_NAME
        path.write_bytes(b'{broken')
        catalog = SongCatalog(self.base, personal_path=path)
        self.assertEqual(len(catalog.songs), 1)
        with self.assertRaisesRegex(ValueError, '원본은 보존'):
            catalog.add_personal_song(dict(title='Song', artists=['Artist']))
        self.assertEqual(path.read_bytes(), b'{broken')

    def test_playlist_links_exact_recordings_without_replacing_manual_choices(self):
        main, alternate, uncatalogued, excluded = ['https://open.spotify.com/track/' + k * 22 for k in 'abcd']
        tracks = [song('spotify:' + url.rsplit('/', 1)[-1], title, spotify_url=url,
                       spotify_recording_candidates=ids, album='Spotify Album', duration=124.4)
                  for url, title, ids in [(main, 'Full Version', ['catalog:full']),
                                           (alternate, 'Demo', ['catalog:demo']),
                                           (uncatalogued, 'Instrumental', []),
                                           (excluded, 'Remix', ['catalog:remix'])]]
        songs = {'catalog:full': song('catalog:full', 'Full Version', missing=['spotify']),
                 'catalog:demo': song('catalog:demo', 'Demo'),
                 'catalog:remix': song('catalog:remix', 'Remix')}
        library = {'spotify': {'catalog:demo': {'id': 'z' * 22}},
                   'automatic_link_exclusions': ['catalog:remix']}
        add_spotify_playlist(songs, {'songs': tracks}, library)
        self.assertEqual(songs['catalog:full']['spotify_url'], main)
        self.assertEqual(songs['catalog:full']['spotify_recording']['album'], 'Spotify Album')
        self.assertEqual(songs['catalog:full']['spotify_recording']['duration'], 124.4)
        self.assertNotIn(tracks[0]['id'], songs)
        self.assertNotIn('spotify_url', songs['catalog:demo'])
        self.assertEqual(songs[tracks[1]['id']]['spotify_url'], alternate)
        self.assertEqual(songs[tracks[2]['id']]['spotify_url'], uncatalogued)
        self.assertNotIn(tracks[3]['id'], songs)
        self.assertEqual(library['spotify'], {'catalog:demo': {'id': 'z' * 22}})

    def test_update_overlay_retains_missing_bundled_entries(self):
        write_catalog(self.update, [song('s2', 'New song')])
        catalog = SongCatalog(self.base, self.update)
        self.assertEqual({s['id'] for s in catalog.songs}, {'s1', 's2'})
        self.assertEqual(catalog.status, 'updated')

    def test_old_overlay_keeps_official_edition_and_spotify_link(self):
        spotify = 'https://open.spotify.com/track/' + 'a' * 22
        edition = {'id': 'label:1', 'title': 'Official Edition', 'date': '2026-11-14',
                   'date_source_kind': 'official_label'}
        write_catalog(self.base, [song(spotify_url=spotify, releases=[edition])])
        write_catalog(self.update, [song(artists=['Updated credit'], missing=['spotify', 'genre'])])
        previous = self.update.read_bytes()
        actual = SongCatalog(self.base, self.update).songs[0]
        self.assertEqual(actual['artists'], ['Updated credit'])
        self.assertEqual(actual['releases'], [edition])
        self.assertEqual(actual['spotify_url'], spotify)
        self.assertEqual(actual['missing'], ['genre'])
        self.assertEqual(self.update.read_bytes(), previous)

    def test_old_update_file_gets_reviewed_series_fix_before_search_and_live_links(self):
        item = song('masterpiece', 'M@STERPIECE', series=['other'],
                    releases=[{'id': 'r1', 'title': 'THE IDOLM@STER STARLIT SEASON 03'}],
                    missing=['series', 'work_link'])
        write_catalog(self.update, [item])
        before = self.update.read_bytes()
        catalog = SongCatalog(self.base, self.update)
        self.assertEqual(catalog.search('M@STERPIECE', series='765')[0]['id'], 'masterpiece')
        self.assertEqual(self.update.read_bytes(), before)

    def test_reviewed_nonvocal_kind_applies_to_old_cache_without_rewriting_it(self):
        from tests.test_recording_kind_evidence import RELEASE, RELEASE_TITLE, RECORDING, TITLE
        item = song('mb:' + RECORDING, TITLE, series=['765'], type='song',
                    releases=[{'id': RELEASE, 'title': RELEASE_TITLE}])
        write_catalog(self.update, [item])
        before = self.update.read_bytes()
        catalog = SongCatalog(self.base, self.update)
        corrected = next(row for row in catalog.songs if row['id'] == item['id'])
        self.assertEqual(corrected['kind'], 'instrumental')
        self.assertEqual(corrected['type'], 'instrumental')
        self.assertEqual(corrected['artists'], item['artists'])
        self.assertEqual(corrected['releases'], item['releases'])
        self.assertEqual(self.update.read_bytes(), before)

    def test_partial_refresh_also_corrects_retained_reviewed_nonvocal_records(self):
        from tests.test_recording_kind_evidence import RELEASE, RELEASE_TITLE, RECORDING, TITLE
        item = song('mb:' + RECORDING, TITLE, series=['765'], type='song',
                    releases=[{'id': RELEASE, 'title': RELEASE_TITLE}])
        write_catalog(self.update, [item])
        person = {'id': 'p1', 'name': 'Idol', 'kind': 'idol', 'series': ['765'],
                  'aliases': [], 'members': []}
        with patch('song_catalog.parse_imasparql', return_value=([], [person])), \
                patch('song_catalog.fetch_musicbrainz', side_effect=OSError('offline')):
            result = refresh_catalog(self.update, fetcher=lambda *args, **kwargs: b'archive')
        self.assertTrue(result['updated'])
        corrected = read_catalog(self.update)['songs'][0]
        self.assertEqual(corrected['id'], item['id'])
        self.assertEqual(corrected['kind'], 'instrumental')
        self.assertEqual(corrected['type'], 'instrumental')
        self.assertEqual(corrected['artists'], item['artists'])

    def test_invalid_update_uses_bundled_catalog(self):
        self.update.write_text('{broken', encoding='utf-8')
        catalog = SongCatalog(self.base, self.update)
        self.assertEqual(len(catalog.songs), 1)
        self.assertEqual(catalog.status, 'bundled')
        self.assertEqual(catalog.generated_at, '2026-10-02')
        self.assertEqual(catalog.sources[0]['id'], 'musicbrainz')

    def test_corrupt_both_files_retains_in_memory_data(self):
        catalog = SongCatalog(self.base, self.update)
        self.base.write_text('broken')
        self.assertFalse(catalog.reload())
        self.assertEqual(len(catalog.songs), 1)
        self.assertEqual(catalog.status, 'retained')

    def test_invalid_catalog_does_not_accept_unsafe_links_or_nan(self):
        for changes in ({'source_url': 'javascript:alert(1)'}, {'duration': float('nan')},
                        {'spotify_url': 'https://example.test/track/' + 'a' * 22},
                        {'series': ['invented']}, {'artists': 'not-a-list'}):
            with self.subTest(changes=changes):
                write_catalog(self.update, [song(**changes)])
                self.assertIsNone(read_catalog(self.update))

    def test_duplicate_ids_reject_snapshot(self):
        write_catalog(self.update, [song(), song()])
        self.assertIsNone(read_catalog(self.update))

    def test_malformed_nested_releases_and_recordings_fall_back_without_rewriting(self):
        for field in ('releases', 'recordings'):
            for invalid in (None, 'not a list', [None], [{'title':None}], [{'id':42}], [{'artists':'Singer'}]):
                with self.subTest(field=field,invalid=invalid):
                    write_catalog(self.update,[song('bad',series=['other'],**{field:invalid})])
                    before=self.update.read_bytes()
                    self.assertIsNone(read_catalog(self.update))
                    catalog=SongCatalog(self.base,self.update)
                    self.assertEqual(catalog.status,'bundled')
                    self.assertEqual([item['id'] for item in catalog.songs],['s1'])
                    self.assertEqual(self.update.read_bytes(),before)

    def test_large_or_nonfinite_nested_duration_cannot_bypass_snapshot_fallback(self):
        for duration in (10**400, float('inf'), float('nan'), True, -1):
            for changes in ({'duration':duration},{'recordings':[{'duration':duration}]}):
                with self.subTest(duration=str(duration)[:20],fields=list(changes)):
                    write_catalog(self.update,[song('bad',**changes)])
                    self.assertIsNone(read_catalog(self.update))
                    self.assertEqual(SongCatalog(self.base,self.update).status,'bundled')

    def test_partial_nested_metadata_remains_compatible(self):
        write_catalog(self.update,[song('partial',releases=[{'title':''}],
            recordings=[{'title':'Twinkle way','artists':[],'duration':None,'isrcs':[]}])])
        catalog=SongCatalog(self.base,self.update)
        self.assertEqual(catalog.status,'updated')
        self.assertEqual({item['id'] for item in catalog.songs},{'s1','partial'})

    def test_total_source_failure_never_replaces_previous_file(self):
        write_catalog(self.update)
        before = self.update.read_bytes()
        def fail(*args, **kwargs):
            raise OSError('offline')
        with patch('song_catalog.fetch_musicbrainz', side_effect=OSError('offline')):
            result = refresh_catalog(self.update, fetcher=fail)
        self.assertFalse(result['updated'])
        self.assertEqual(self.update.read_bytes(), before)
        self.assertEqual(len(result['errors']), 2)

    def test_incomplete_musicbrainz_query_retains_the_previous_snapshot(self):
        write_catalog(self.update)
        before = self.update.read_bytes()
        recording = {'id': '11111111-2222-3333-4444-555555555555', 'title': 'A'}
        response = json.dumps({'count': 2, 'offset': 0,
                               'recordings': [recording, recording]}).encode()
        def incomplete(**kwargs):
            return fetch_musicbrainz(fetcher=lambda *a, **k: response,
                                     queries=['release:test'], interval=0)
        def fail(*args, **kwargs):
            raise OSError('other source offline')
        with patch('song_catalog.fetch_musicbrainz', side_effect=incomplete):
            result = refresh_catalog(self.update, fetcher=fail)
        self.assertFalse(result['updated'])
        self.assertEqual(self.update.read_bytes(), before)
        self.assertTrue(any('MusicBrainz' in error for error in result['errors']))

    def test_cancel_preserves_previous_file_and_private_data(self):
        write_catalog(self.update)
        private = self.root / 'user_library.json'
        private.write_text('{"favorites":["s1"],"local_path":"private.mp3"}')
        before, private_before = self.update.read_bytes(), private.read_bytes()
        cancel = threading.Event()
        cancel.set()
        result = refresh_catalog(self.update, cancel=cancel,
                                 fetcher=lambda *a, **k: self.fail('must not fetch'))
        self.assertTrue(result['cancelled'])
        self.assertEqual(self.update.read_bytes(), before)
        self.assertEqual(private.read_bytes(), private_before)

    def test_partial_failure_keeps_previous_source_and_updates_other(self):
        write_catalog(self.update)
        performer = {'id': 'p1', 'name': 'Idol', 'aliases': [], 'members': [],
                     'kind': 'idol', 'series': ['other']}
        with patch('song_catalog.parse_imasparql', return_value=([], [performer])), \
                patch('song_catalog.fetch_musicbrainz', side_effect=OSError('offline')):
            result = refresh_catalog(self.update, fetcher=lambda *a, **k: b'archive')
        self.assertTrue(result['updated'])
        value = read_catalog(self.update)
        self.assertEqual(value['songs'][0]['id'], 's1')
        self.assertEqual(value['performers'][0]['id'], 'p1')
        self.assertEqual(next(s for s in value['sources'] if s['id'] == 'musicbrainz')['status'],
                         'retained_after_error')


if __name__ == '__main__':
    unittest.main()
