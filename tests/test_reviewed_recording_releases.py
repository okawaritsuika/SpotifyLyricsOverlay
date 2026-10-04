from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import catalog_sources as sources
from song_catalog import SongCatalog, read_catalog, refresh_catalog
from tests.test_song_catalog import song, write_catalog


RELEASE = 'bcb2b23c-4422-4bbb-a8b9-8a4b349ddf27'
DIGITAL = '0c0052c1-0018-49a7-8a58-040db1523bee'
TITLE = '283 Production MUSICAL Performance 騎士団のヴェール – Veil of Order – Music Collection'
RECORDING = '2464afde-0c18-4cc4-abed-001af30380bd'
SOLO = 'fc69d565-d973-4ccd-8d86-60f55eee8040'


def raw_bgm(**changes):
    value = {'id': RECORDING, 'title': 'Norsfelus', 'length': 90000,
             'artist-credit': [{'name': '大木奏弥', 'artist': {'name': '大木奏弥', 'type': 'Person'}}],
             'releases': [{'id': RELEASE, 'title': TITLE, 'status': 'Official'}]}
    value.update(changes)
    return value


class ReviewedRecordingReleaseTests(unittest.TestCase):
    def test_exact_official_bgm_is_collected_as_shiny_instrumental(self):
        result = sources.normalize_musicbrainz([raw_bgm()])
        self.assertEqual(len(result), 1)
        item = result[0]
        self.assertEqual((item['id'], item['title'], item['artists']),
                         ('mb:' + RECORDING, 'Norsfelus', ['大木奏弥']))
        self.assertEqual(item['series'], ['shiny'])
        self.assertEqual(item['kind'], 'instrumental')
        self.assertEqual(item['duration'], 90)
        self.assertEqual(item['series_source_urls'], ['https://shinycolors.lantis.jp/releaseinfo/laca-35264/'])
        self.assertIn('release:"283 Production"', sources.catalog_queries())

    def test_partial_identity_or_unofficial_status_never_admits_composer_record(self):
        changes = [{'id': '11111111-2222-3333-4444-555555555555'}, {'title': 'Norsfelus (New mix)'},
                   {'releases': [{'id': RELEASE, 'title': TITLE + ' unofficial', 'status': 'Official'}]},
                   {'releases': [{'id': DIGITAL, 'title': TITLE, 'status': 'Official'}]}]
        for status in ('Bootleg', 'Pseudo-Release', None):
            changes.append({'releases': [{'id': RELEASE, 'title': TITLE, 'status': status}]})
        for change in changes:
            with self.subTest(change=change):
                self.assertEqual(sources.normalize_musicbrainz([raw_bgm(**change)]), [])

    def test_physical_and_digital_dates_keep_their_distinct_sources(self):
        value = raw_bgm(id=SOLO, title='メモワール・アンタクト', releases=[
            {'id': RELEASE, 'title': TITLE, 'status': 'Official'},
            {'id': DIGITAL, 'title': TITLE, 'status': 'Official'}])
        before = deepcopy(value)
        releases = sources.normalize_musicbrainz([value])[0]['releases']
        self.assertEqual([release['date'] for release in releases[:2]], ['2026-03-11', '2026-03-20'])
        self.assertEqual([release['date_source_kind'] for release in releases[:2]], ['official_label', 'musicbrainz'])
        self.assertEqual(releases[1]['date_source_url'], 'https://musicbrainz.org/release/' + DIGITAL)
        self.assertEqual(value, before)
        value['releases'][1]['date'] = '2026-03-21'
        updated = sources.normalize_musicbrainz([value])[0]['releases'][1]
        self.assertEqual(updated['date'], '2026-03-21')
        self.assertNotIn('date_source_url', updated)

    def test_cached_series_fix_changes_only_verified_musicbrainz_records(self):
        item = song('mb:' + SOLO, 'メモワール・アンタクト', series=['other'],
                    releases=[{'id': RELEASE, 'title': TITLE}], missing=['series', 'spotify'])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cache.json'
            write_catalog(path, [item])
            before = path.read_bytes()
            result = SongCatalog(path).songs[0]
            self.assertEqual(result['series'], ['shiny'])
            self.assertEqual(result['missing'], ['spotify'])
            self.assertEqual(result['releases'][0]['date'], '2026-03-11')
            self.assertEqual(result['releases'][0]['formats'], ['CD'])
            self.assertEqual(result['releases'][0]['id'], item['releases'][0]['id'])
            self.assertEqual(path.read_bytes(), before)
        local = {**item, 'source_id': 'local'}
        self.assertEqual(sources.reclassify_unknown_series([local]), 0)

    def test_malformed_evidence_fails_closed_without_partial_approval(self):
        valid = json.loads(sources.REVIEWED_RECORDING_RELEASES_PATH.read_text(encoding='utf-8'))
        bad_values = [None, {}, {'schema_version': True, 'entries': []},
                      {**valid, 'entries': [valid['entries'][1]] * 2}]
        for field, value in [('series', 'other'), ('source_urls', ['javascript:unsafe']),
                             ('date', '2026-02-31'), ('date_source_kind', 'unknown'),
                             ('recordings', [{'id': RECORDING, 'title': None}])]:
            bad = deepcopy(valid)
            bad['entries'][1][field] = value
            bad_values.append(bad)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'reviewed.json'
            with patch.object(sources, 'REVIEWED_RECORDING_RELEASES_PATH', path):
                self.assertEqual(sources.normalize_musicbrainz([raw_bgm()]), [])
                for value in bad_values:
                    path.write_text(json.dumps(value), encoding='utf-8')
                    with self.subTest(value=value):
                        self.assertEqual(sources.normalize_musicbrainz([raw_bgm()]), [])

    def test_refresh_keeps_added_record_when_next_search_omits_it(self):
        person = {'id': 'idol', 'name': 'Idol', 'aliases': [], 'members': [],
                  'kind': 'idol', 'series': ['shiny']}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'catalog.json'
            write_catalog(path, [song('prior')])
            with patch('song_catalog.parse_imasparql', return_value=([], [person])), \
                    patch('song_catalog.fetch_musicbrainz', return_value=([raw_bgm()], [])):
                first = refresh_catalog(path, fetcher=lambda *a, **k: b'archive')
            self.assertTrue(first['updated'])
            self.assertEqual(first['added'], 1)
            with patch('song_catalog.parse_imasparql', return_value=([], [person])), \
                    patch('song_catalog.fetch_musicbrainz', return_value=([], [])):
                second = refresh_catalog(path, fetcher=lambda *a, **k: b'archive')
            self.assertTrue(second['updated'])
            result = {item['id']: item for item in read_catalog(path)['songs']}
            self.assertEqual(set(result), {'prior', 'mb:' + RECORDING})
            self.assertEqual(result['mb:' + RECORDING]['series'], ['shiny'])
            self.assertEqual(result['mb:' + RECORDING]['kind'], 'instrumental')


if __name__ == '__main__':
    unittest.main()
