from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import catalog_sources as sources
import live_catalog as live
from song_catalog import SongCatalog, read_catalog
from tests.test_song_catalog import song, write_catalog
from tests.test_live_catalog import snapshot


ROOT = Path(__file__).resolve().parents[1]
ABC = 'd57fd28d-a3b5-4b5b-b434-1291ef98c5f7'


class ReviewedMetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = json.loads((ROOT / 'resources/song_catalog.json').read_text(encoding='utf-8'))
        cls.raw_by_id = {item['id']: item for item in cls.raw['songs']}
        cls.reviews = json.loads(sources.REVIEWED_RECORDING_RELEASES_PATH.read_text(encoding='utf-8'))
        cls.abc = next(entry for entry in cls.reviews['entries'] if entry['release_id'] == ABC)

    def test_abc_exact_ten_dates_keep_original_and_official_source(self):
        for recording in self.abc['recordings']:
            original = self.raw_by_id['mb:' + recording['id']]
            item = deepcopy(original)
            sources.enrich_reviewed_releases([item])
            self.assertEqual(original['first_release_date'], '2013-01-18')
            self.assertEqual(item['first_release_date'], '2023-01-18')
            self.assertEqual(item['first_release_date_original'], '2013-01-18')
            self.assertEqual(item['first_release_date_source_url'], self.abc['date_source_url'])
            self.assertEqual(item['releases'][0]['date'], '2023-01-18')
            self.assertEqual(item['releases'][0]['date_original'], '2013-01-18')
            once = deepcopy(item)
            sources.enrich_reviewed_releases([item])
            self.assertEqual(item, once)

    def test_first_date_never_inferred_from_minimum_or_another_release(self):
        original = self.raw_by_id['mb:' + self.abc['recordings'][0]['id']]
        for field, value in [('id', 'mb:11111111-2222-3333-4444-555555555555'),
                             ('title', original['title'] + ' (new mix)'), ('source_id', 'local')]:
            item = deepcopy(original)
            item[field] = value
            before = deepcopy(item)
            sources.enrich_reviewed_releases([item])
            self.assertEqual(item, before)
        item = deepcopy(original)
        item['releases'].append({'id': 'another', 'title': 'Earlier release', 'date': '2012-01-01'})
        sources.enrich_reviewed_releases([item])
        self.assertEqual(item['first_release_date'], '2013-01-18')
        self.assertNotIn('first_release_date_source_url', item)
        item = deepcopy(original)
        item['first_release_date'] = item['releases'][0]['date'] = '2018-01-18'
        sources.enrich_reviewed_releases([item])
        self.assertEqual(item['first_release_date'], '2018-01-18')
        self.assertEqual(item['releases'][0]['date'], '2018-01-18')
        self.assertNotIn('date_source_url', item['releases'][0])
        for upstream_date in ('2024-01-18', '', '2023-01-18'):
            item = deepcopy(original)
            item['releases'][0]['date'] = upstream_date
            sources.enrich_reviewed_releases([item])
            self.assertEqual(item['first_release_date'], '2013-01-18')
            self.assertNotIn('first_release_date_original', item)

    def test_nine_verified_media_formats_and_mixed_media_remain_distinct(self):
        formatted = [entry for entry in self.reviews['entries'] if 'formats' in entry]
        self.assertEqual(len(formatted), 9)
        for entry in formatted:
            recording = entry['recordings'][0]
            item = deepcopy(self.raw_by_id['mb:' + recording['id']])
            item['releases'] = [{'id': entry['release_id'], 'title': entry['release_title']}]
            sources.enrich_reviewed_releases([item])
            release = item['releases'][0]
            self.assertEqual(release['formats'], entry['formats'])
            self.assertEqual(release['format_source_url'], 'https://musicbrainz.org/release/' + entry['release_id'])
            item['releases'] = [{'id': entry['release_id'], 'title': 'Different title'}]
            sources.enrich_reviewed_releases([item])
            self.assertNotIn('formats', item['releases'][0])
        self.assertEqual(sum(entry['formats'] == ['CD', 'Blu-ray'] for entry in formatted), 2)

    def test_review_metadata_malformed_types_and_source_pairs_fail_closed(self):
        changes = [('formats', 'CD'), ('formats', [None]), ('formats', ['CD', 'CD']),
                   ('format_source_url', 'https://example.com/wrong'), ('date_source_url', []),
                   ('source_urls', ['https://:secret@example.test/source']),
                   ('date_source_kind', []), ('incorrect_date', '2020-02-31')]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'review.json'
            for field, value in changes:
                bad = deepcopy(self.reviews)
                bad['entries'][0][field] = value
                path.write_text(json.dumps(bad), encoding='utf-8')
                with self.subTest(field=field, value=value), patch.object(sources, 'REVIEWED_RECORDING_RELEASES_PATH', path):
                    self.assertEqual(sources._reviewed_recording_releases(), {})

    def test_malformed_public_cache_metadata_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'catalog.json'
            for field, value in [('formats', 'CD'), ('formats', [None]), ('date_source_url', []),
                                 ('date_source_url', 'javascript:unsafe'), ('format_source_url', {}),
                                 ('date_source_url', 'https://:secret@example.test/source'),
                                 ('date_source_kind', []), ('date_original', [])]:
                write_catalog(path, [song(releases=[{'id': 'release', 'title': 'title', field: value}])])
                self.assertIsNone(read_catalog(path), field)
            for field in ('first_release_date', 'first_release_date_original',
                          'first_release_date_source_url', 'first_release_date_source_kind'):
                write_catalog(path, [song(**{field: []})])
                self.assertIsNone(read_catalog(path), field)

    def test_movie_exact_query_and_all_recording_identities(self):
        self.assertIn('release:"ラムネ色 青春" OR release:"虹色ミラクル"', sources.catalog_queries())
        movie = self.reviews['entries'][5:9]
        identifiers = {row['id'] for entry in movie for row in entry['recordings']}
        self.assertEqual(len(identifiers), 22)
        loaded = {item['id']: item for item in SongCatalog().songs}
        self.assertTrue(all(loaded['mb:' + identifier]['series'] == ['765'] for identifier in identifiers))
        self.assertEqual(sum(loaded['mb:' + identifier]['kind'] == 'spoken' for identifier in identifiers), 2)
        for entry in movie:
            for row in entry['recordings']:
                self.assertTrue(any(release['id'] == entry['release_id']
                                    for release in loaded['mb:' + row['id']]['releases']))

    def test_missing_reviewed_edition_requires_existing_exact_evidence(self):
        entry = self.reviews['entries'][6]
        normal = self.reviews['entries'][5]
        row = next(row for row in entry['recordings'] if row in normal['recordings'])
        item = deepcopy(self.raw_by_id['mb:' + row['id']])
        item['releases'] = [{'id': normal['release_id'], 'title': normal['release_title']}]
        sources.enrich_reviewed_releases([item])
        self.assertEqual({release['id'] for release in item['releases']}, {normal['release_id'], entry['release_id']})
        for releases in ([], [{'id': normal['release_id'], 'title': 'Unverified title'}]):
            item['releases'] = deepcopy(releases)
            sources.enrich_reviewed_releases([item])
            self.assertEqual(item['releases'], releases)

    def test_five_reviewed_missing_memberships_require_exact_artist_identity(self):
        checked = 0
        for entry in self.reviews['entries']:
            for row in entry['recordings']:
                if 'release_when_missing_artists' not in row:
                    continue
                checked += 1
                original = self.raw_by_id['mb:' + row['id']]
                for field, value in [('artists', ['Other artist']), ('source_id', 'local'),
                                     ('title', original['title'] + ' Remix'),
                                     ('id', 'mb:11111111-2222-3333-4444-555555555555')]:
                    item = deepcopy(original)
                    item[field] = value
                    before = deepcopy(item)
                    sources.enrich_reviewed_releases([item])
                    self.assertEqual(item, before)
                item = deepcopy(original)
                sources.enrich_reviewed_releases([item])
                self.assertTrue(any(release['id'] == entry['release_id'] for release in item['releases']))
        self.assertEqual(checked, 5)

    def test_endless_tour_original_album_restores_full_trio_evidence(self):
        item = deepcopy(self.raw_by_id['mb:541c4ba8-da57-49f3-a495-c56f9c3b082e'])
        original = deepcopy(item)
        sources.enrich_reviewed_releases([item])
        release = next(row for row in item['releases'] if row['title'] == 'ENDLESS TOUR')
        self.assertEqual(release['id'], '34bb554e-f765-418e-a320-e488355fed31')
        self.assertEqual(release['date'], '2020-04-29')
        self.assertEqual(release['date_source_url'], 'https://www.lantis.jp/imas/release_LACM-14961.html')
        self.assertEqual(item['artists'], original['artists'])
        self.assertEqual(item['performer_ids'], ['imas:Kasuga_Mirai', 'imas:Mogami_Shizuka', 'imas:Hakozaki_Serika'])
        self.assertEqual(item['first_release_date'], original['first_release_date'])
        once = deepcopy(item)
        sources.enrich_reviewed_releases([item])
        self.assertEqual(item, once)


class ReviewedLiveWorkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.aliases = json.loads(live.WORK_ALIASES_PATH.read_text(encoding='utf-8'))
        cls.songs = {item['id']: item for item in SongCatalog().songs}

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'live.json'
        value = snapshot()
        value['events'][0].update(series=['sidem'], songs=[{'title': 'MEET THE WORLD!'}])
        self.path.write_text(json.dumps(value), encoding='utf-8')
        self.catalog = live.LiveCatalog(self.path)

    def test_sixteen_exact_recordings_link_both_directions_without_merging(self):
        items = [self.songs['mb:' + entry['recording_id']] for entry in self.aliases['entries']]
        self.assertEqual(len(items), 16)
        for item in items:
            matches = self.catalog.for_song(item)
            self.assertEqual(len(matches), 1)
            self.assertTrue(matches[0]['recording_version_unverified'])
            self.assertEqual(matches[0]['match_basis'], 'known_work_title_and_series')
        row = self.catalog.songs_for_event('million-day2', items)[0]
        self.assertEqual(len({item['id'] for item in row['matches']}), 16)

    def test_partial_identity_or_instrumental_never_uses_reviewed_alias(self):
        original = self.songs['mb:' + self.aliases['entries'][0]['recording_id']]
        changes = [('id', 'mb:11111111-2222-3333-4444-555555555555'), ('title', original['title'] + ' Remix'),
                   ('artists', ['Other']), ('source_id', 'local'), ('series', ['million']),
                   ('releases', []), ('kind', 'instrumental')]
        for field, value in changes:
            item = deepcopy(original)
            item[field] = value
            with self.subTest(field=field):
                self.assertEqual(self.catalog.for_song(item), [])
                self.assertEqual(self.catalog.songs_for_event('million-day2', [item])[0]['matches'], [])
        self.assertNotEqual(live.work_key('MEET THE WORLD! (Unknown Ver.)'), live.work_key('MEET THE WORLD!'))

    def test_corrupt_alias_resource_fails_closed(self):
        path = self.path.parent / 'aliases.json'
        for field, value in [('artists', '315 ALLSTARS'), ('source_urls', ['javascript:unsafe']),
                             ('source_urls', ['https://:secret@example.test/source']),
                             ('series', ['other']), ('recording_id', 'wrong'), ('work_title', None)]:
            bad = deepcopy(self.aliases)
            bad['entries'][0][field] = value
            path.write_text(json.dumps(bad), encoding='utf-8')
            with patch.object(live, 'WORK_ALIASES_PATH', path):
                self.assertEqual(live._reviewed_work_aliases(), {})

    def test_quote_typography_preserves_punctuation_and_recording_qualifiers(self):
        for left, right in [('“W”onderful Tactics!', '"W"onderful Tactics!'),
                            ('HEAT BEAT “Identity”', 'HEAT BEAT "Identity"'),
                            ('”Your” HOME TOWN', '"Your" HOME TOWN')]:
            self.assertEqual(live.work_key(left), live.work_key(right))
            self.assertNotEqual(live.work_key(left), live.work_key(right.replace('"', '')))
            self.assertNotEqual(live.work_key(left), live.work_key(right + ' (Remix)'))


if __name__ == '__main__':
    unittest.main()
