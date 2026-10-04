"""Korean discovery aids never rewrite or merge recording identity."""
import copy
import unittest
from unittest.mock import patch

from explorer_library import matches_query
from song_aliases import alias_information,generate_readings,kana_hangul,search_aliases,title_readings
from song_catalog import SongCatalog


class SongAliasTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog=SongCatalog()

    def song(self,title='KAWAII♡めたもる交響曲',series='shiny'):
        return {'id':'original','title':title,'artists':['Original singer'],'album':'Original album',
                'series':[series],'aliases':[]}

    def test_real_catalog_and_dictionary_find_korean_title_without_altering_records(self):
        before=copy.deepcopy([song for song in self.catalog.songs if song['title'].startswith('KAWAII♡')])
        found=self.catalog.search('귀여운 메타모르')
        self.assertEqual(len(found),3)
        self.assertEqual({song['title'] for song in found},
                         {'KAWAII♡めたもる交響曲','KAWAII♡めたもる交響曲 (Game Size)',
                          'KAWAII♡めたもる交響曲 (Off Vocal)'})
        self.assertTrue(all(matches_query(song,'귀여운 교향곡') for song in found))
        self.assertEqual(found,before)
        self.assertTrue(all(song['aliases']==[] for song in found))

    def test_version_filters_and_ids_are_not_folded(self):
        game=self.catalog.search('귀여운 Game Size')
        off=self.catalog.search('귀여운 Off Vocal')
        self.assertEqual(len(game),1);self.assertEqual(len(off),1)
        self.assertNotEqual(game[0]['id'],off[0]['id'])
        self.assertEqual(off[0]['kind'],'instrumental')
        self.assertFalse(matches_query(self.song(),'귀여운 Game Size'))

    def test_unrelated_brand_or_unknown_arrangement_does_not_inherit_curated_translation(self):
        self.assertFalse(matches_query(self.song(series='765'),'귀여운'))
        self.assertFalse(matches_query(self.song(title='KAWAII♡めたもる交響曲 (Unknown Remix)'),'귀여운'))
        self.assertTrue(matches_query(self.song(title='KAWAII ウォーズ'),'KAWAII'))
        self.assertFalse(matches_query(self.song(title='KAWAII ウォーズ'),'교향곡'))

    def test_readings_translations_and_confirmed_titles_remain_distinct(self):
        info=alias_information(self.song())
        self.assertEqual(info['korean_titles'],[])
        self.assertIn('귀여운 메타모르 교향곡',info['search_translations'])
        self.assertIn('카와이 메타모르 코쿄쿄쿠',info['readings'])
        self.assertEqual(info['source_urls'],['https://shinycolors.lantis.jp/releaseinfo/lacm-24821/'])

    def test_generic_kana_kanji_hangul_and_romaji_search(self):
        for query in ('하나자카리','hanazakari','はなざかり'):
            with self.subTest(query=query):
                self.assertTrue(self.catalog.search(query))
        self.assertTrue(self.catalog.search('카운트다운 러브'))
        self.assertTrue(matches_query(self.song(title='ユニット'),'유닛토'))

    def test_kana_reading_handles_initial_vowels_nasal_yoon_and_sokuon(self):
        self.assertEqual(kana_hangul('イチバン'),'이치반')
        self.assertEqual(kana_hangul('ウサギ'),'우사기')
        self.assertEqual(kana_hangul('ハッピー'),'핫피')
        self.assertEqual(kana_hangul('メタモル',loan_endings=True),'메타모르')
        self.assertEqual(kana_hangul('こうきょうきょく'),'코쿄쿄쿠')

    def test_generated_reading_preserves_latin_words_and_version_qualifiers(self):
        readings=generate_readings('花ざかりWeekend✿ (Game Size)')
        self.assertTrue(readings)
        self.assertTrue(all('Weekend' in value and '(Game Size)' in value for value in readings))
        self.assertTrue(any('하나자카리' in value for value in readings))

    def test_bundled_readings_need_no_runtime_converter(self):
        title_readings.cache_clear()
        with patch('song_aliases._reader',side_effect=AssertionError('Unexpected runtime conversion')):
            self.assertTrue(title_readings('花ざかりWeekend✿'))

    def test_new_title_fallback_is_cached_and_english_titles_stay_unchanged(self):
        title_readings.cache_clear()
        with patch('song_aliases.generate_readings',return_value=('새 독음',)) as convert:
            self.assertEqual(title_readings('新曲XYZunique'),('새 독음',))
            self.assertEqual(title_readings('新曲XYZunique'),('새 독음',))
            convert.assert_called_once()
        self.assertEqual(search_aliases(self.song(title='English Title (Live 2025)')),())
