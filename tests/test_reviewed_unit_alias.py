"""A reviewed unit identity is joined in memory only after its evidence matches."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from explorer_library import performers, resolve_group_members
from song_catalog import SongCatalog
from test_song_catalog import song, write_catalog


UNIT_ID = 'imas:ニュージェネレーション'
OFFICIAL = 'https://cinderella-library.idolmaster-official.jp/history/detail/?p=20150422_2'


def unit():
    return {'id': UNIT_ID, 'name': 'ニュージェネレーション', 'kind': 'group', 'series': ['cinderella'],
            'aliases': [], 'members': ['島村卯月', '渋谷凛', '本田未央'],
            'members_ids': ['imas:Shimamura_Uzuki', 'imas:Shibuya_Rin', 'imas:Honda_Mio'],
            'source_url': 'https://example.org/original-rdf',
            'provenance': [{'source_id': 'synthetic', 'url': 'https://example.org/original-rdf'}]}


class ReviewedUnitAliasTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'catalog.json'
        self.item = song('target', 'Synthetic', artists=['new generations'], series=['cinderella'],
                         performer_ids=[], performer_aliases=[])

    def load(self, people=None, rows=None):
        write_catalog(self.path, rows or [deepcopy(self.item)], performers=people or [unit()])
        before = self.path.read_bytes()
        catalog = SongCatalog(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        return catalog

    def test_exact_identity_joins_without_changing_recording_or_external_identity(self):
        catalog = self.load()
        self.assertEqual(catalog.songs[0]['performer_ids'], [UNIT_ID])
        self.assertEqual(catalog.songs[0]['artists'], ['new generations'])
        person = catalog.performers[0]
        self.assertEqual(person['id'], UNIT_ID)
        self.assertEqual(person['name'], unit()['name'])
        self.assertEqual(person['members'], unit()['members'])
        self.assertEqual(person['members_ids'], unit()['members_ids'])
        self.assertIn('new generations', person['aliases'])
        self.assertIn(OFFICIAL, person['source_urls'])
        self.assertEqual(person['source_url'], unit()['source_url'])
        self.assertEqual(person['provenance'], unit()['provenance'])

    def test_each_identity_mismatch_rejects_derived_alias(self):
        changes = {'id': 'other:unit', 'name': 'Different unit', 'kind': 'idol', 'series': ['million'],
                   'members': unit()['members'][:2], 'members_ids': unit()['members_ids'][:2]}
        for field, value in changes.items():
            with self.subTest(field=field):
                candidate = unit();candidate[field] = value
                catalog = self.load([candidate])
                self.assertEqual(catalog.songs[0]['performer_ids'], [])
                self.assertEqual(catalog.performers, [candidate])

    def test_duplicate_id_and_same_name_owner_are_rejected(self):
        for other in (unit(), {**unit(), 'id': 'other:unit', 'series': ['million']}):
            catalog = self.load([unit(), other])
            self.assertEqual(catalog.songs[0]['performer_ids'], [])
            self.assertEqual(catalog.performers, [unit(), other])

    def test_other_alias_owner_rejects_width_case_and_spacing_variants(self):
        for alias in ('ＮＥＷ ＧＥＮＥＲＡＴＩＯＮＳ', 'newgenerations', 'ニュージェネレーションズ', '뉴 제너레이션즈'):
            with self.subTest(alias=alias):
                other = {**unit(), 'id': 'other:unit', 'name': 'Other', 'aliases': [alias]}
                catalog = self.load([unit(), other])
                self.assertEqual(catalog.performers, [unit(), other])

    def test_existing_aliases_remain_when_guard_fails(self):
        candidate = unit();candidate.update(series=['million'], aliases=['Existing alias', 'new generations'])
        catalog = self.load([candidate])
        self.assertEqual(catalog.performers, [candidate])

    def test_only_exact_single_credit_and_empty_or_same_id_rows_are_enriched(self):
        rows = [deepcopy(self.item), {**self.item, 'id':'already', 'performer_ids':[UNIT_ID]},
                {**self.item, 'id':'other-id', 'performer_ids':['other:unit']},
                {**self.item, 'id':'multi', 'artists':['new generations','Another performer']},
                {**self.item, 'id':'case', 'artists':['New Generations']},
                {**self.item, 'id':'unrelated', 'artists':['Another performer']}]
        catalog = self.load(rows=rows)
        actual = {row['id']:row for row in catalog.songs}
        for row in rows[2:]:self.assertEqual(actual[row['id']], row)
        self.assertEqual(actual['target']['performer_ids'], [UNIT_ID])
        self.assertEqual(actual['already']['performer_ids'], [UNIT_ID])

    def test_repeated_reload_does_not_accumulate_aliases_or_sources(self):
        catalog = self.load()
        before = self.path.read_bytes()
        expected = deepcopy(catalog.performers)
        for _ in range(3):
            self.assertTrue(catalog.reload())
            self.assertEqual(catalog.performers, expected)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(catalog.performers[0]['aliases'].count('new generations'), 1)
        self.assertEqual(catalog.performers[0]['source_urls'].count(OFFICIAL), 1)

    def test_reload_rechecks_current_roster_without_reusing_previous_alias(self):
        catalog = self.load()
        self.assertIn('new generations', catalog.performers[0]['aliases'])
        changed = unit();changed['members'] = changed['members'][:2]
        write_catalog(self.path, [deepcopy(self.item)], performers=[changed])
        self.assertTrue(catalog.reload())
        self.assertEqual(catalog.performers, [changed])
        self.assertEqual(catalog.songs[0]['performer_ids'], [])

    def test_existing_source_urls_are_preserved(self):
        candidate = unit();candidate['source_urls'] = ['https://example.org/retained']
        person = self.load([candidate]).performers[0]
        self.assertEqual(person['source_urls'][0], 'https://example.org/retained')
        self.assertIn(OFFICIAL, person['source_urls'])

    def test_checked_alias_reaches_explorer_and_preserves_confirmed_members(self):
        catalog = self.load()
        people = performers(external=catalog.performers)
        canonical = next(person for person in people if person['id']=='group:newgenerations')
        self.assertEqual(canonical.get('external_ids'), [UNIT_ID])
        self.assertIn(OFFICIAL, canonical['source_urls'])
        self.assertFalse(any(person['id']==UNIT_ID for person in people))
        self.assertEqual([member['name'] for member in resolve_group_members(canonical, people)], unit()['members'])


if __name__ == '__main__':unittest.main()
