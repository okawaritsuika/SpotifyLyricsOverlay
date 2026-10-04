import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from artist_colors import ArtistColors, RESOURCE_DIRECTORY, _read_entries, catalog, character_color_spans
from artist_names import artist_label


class ArtistColorTests(unittest.TestCase):
    def setUp(self):
        self.entries = [
            {'name': '如月千早', 'color': '#2743D2', 'kind': 'idol'},
            {'name': '天海春香', 'color': '#e22b30', 'kind': 'idol'},
            {'name': '日高愛', 'color': '#e4007f', 'kind': 'idol'},
            {'name': 'ノクチル', 'color': '#80c8ef', 'kind': 'group'},
            {'name': 'High×Joker', 'color': '#e56c2d', 'kind': 'group'},
            {'name': 'W', 'color': '#e5cd36', 'kind': 'group'},
        ]
        self.names = [
            {'name': '如月 千早', 'ko': '키사라기 치하야', 'kind': 'idol',
             'aliases': ['Chihaya Kisaragi', 'Kisaragi Chihaya']},
            {'name': '天海春香', 'ko': '아마미 하루카', 'kind': 'idol', 'aliases': ['Haruka Amami']},
            {'name': '日高愛', 'ko': '히다카 아이', 'kind': 'idol', 'aliases': ['Ai']},
            {'name': '今井麻美', 'ko': '이마이 아사미', 'kind': 'voice_actor', 'aliases': ['Asami Imai']},
            {'name': 'ノクチル', 'ko': '녹칠', 'kind': 'group', 'aliases': ['noctchill']},
            {'name': 'High×Joker', 'ko': '하이 조커', 'kind': 'group'},
            {'name': 'W', 'ko': '더블유', 'kind': 'group'},
        ]
        self.catalog = ArtistColors(self.entries, self.names)

    def test_exact_alias_ranges_keep_original_width_spacing_and_case(self):
        for name in ('如月 千早', '키사라기 치하야', 'CHIHAYA KISARAGI', 'Ｋｉｓａｒａｇｉ　Ｃｈｉｈａｙａ'):
            with self.subTest(name=name):
                self.assertEqual(self.catalog.spans('  ' + name + '  '), ((2, 2 + len(name), '#2743d2'),))

    def test_cv_credit_shares_character_color_through_closing_parenthesis(self):
        for name, credit in (('如月 千早', '（CV. 今井 麻美）'),
                             ('Chihaya Kisaragi', ' (CV: Asami Imai)'),
                             ('키사라기 치하야', ' (CV. 이마이 아사미)'),
                             ('如月千早', '（ＣＶ． 今井麻美）')):
            self.assertEqual(self.catalog.spans(name + credit), ((0, len(name + credit), '#2743d2'),))

    def test_voice_actors_and_unknown_names_stay_neutral(self):
        for name in ('今井麻美', 'Asami Imai', '이마이 아사미', 'Unknown (CV. 今井麻美)',
                     'Unknown Group', 'ノクチル (CV. Actor)'):
            self.assertEqual(self.catalog.spans(name, 'THE IDOLM@STER SideM'), ())

    def test_group_colors_resolve_korean_and_original_aliases(self):
        for name,color in (('ノクチル','#80c8ef'),('녹칠','#80c8ef'),('noctchill','#80c8ef'),
                           ('High×Joker','#e56c2d'),('하이 조커','#e56c2d'),('W','#e5cd36'),('더블유','#e5cd36')):
            self.assertEqual(self.catalog.spans(name,'THE IDOLM@STER'),((0,len(name),color),))
        self.assertEqual(self.catalog.spans('W','Other Album'),())

    def test_each_character_and_group_gets_its_own_color(self):
        text = '키사라기 치하야, 녹칠 × 아마미 하루카, Chihaya Kisaragi'
        start2 = text.index('아마미')
        start3 = text.index('Chihaya')
        self.assertEqual(self.catalog.spans(text),
                         ((0, len('키사라기 치하야'), '#2743d2'),
                          (text.index('녹칠'), text.index('녹칠')+2, '#80c8ef'),
                          (start2, start2 + len('아마미 하루카'), '#e22b30'),
                          (start3, len(text), '#2743d2')))

    def test_full_cv_credit_is_colored_but_composite_separators_are_not(self):
        for separator in (', ', '、', ' × ', ' x ', ' & ', ' / ', ' + ', '; ', '，', ' ＆ ', '\n', '\r\n'):
            first = '如月千早 (CV. 今井麻美, Actor)'
            text = first + separator + '天海春香'
            start = text.index('天海')
            self.assertEqual(self.catalog.spans(text), ((0, len(first), '#2743d2'), (start, start + 4, '#e22b30')))

    def test_cv_at_end_of_composite_credit_keeps_both_character_ranges(self):
        text = '天海春香、如月千早 (CV. 今井麻美)'
        self.assertEqual(self.catalog.spans(text), ((0, 4, '#e22b30'), (5, len(text), '#2743d2')))

    def test_multiple_character_voice_pairs_keep_their_own_color(self):
        first='天海春香 (CV. 中村繪里子)';second='如月千早 (CV. 今井麻美)'
        text='  '+first+' × '+second+'  '
        start=text.index(second)
        self.assertEqual(self.catalog.spans(text),((2,2+len(first),'#e22b30'),
                                                  (start,start+len(second),'#2743d2')))

    def test_unit_punctuation_is_preserved_in_composite_credit(self):
        text = '天海春香 × High×Joker × 如月千早'
        start=text.index('High')
        self.assertEqual(self.catalog.spans(text), ((0, 4, '#e22b30'), (start,start+10,'#e56c2d'),
                                                  (len(text) - 4, len(text), '#2743d2')))

    def test_missing_group_color_preserves_member_palette_and_explicit_color_wins(self):
        entries=[{'name':'Red Idol','kind':'idol','color':'#ff0000'},
                 {'name':'Blue Idol','kind':'idol','color':'#0000ff'},
                 {'name':'Mixed Group','kind':'group','members':['Red Idol','Blue Idol','Red Alias']}]
        names=[{'name':'Red Idol','aliases':['Red Alias']},
               {'name':'Mixed Group','ko':'혼합 그룹'}]
        colors=ArtistColors(entries,names)
        self.assertEqual(colors.spans('혼합 그룹'),((0,5,('#ff0000','#0000ff')),))
        self.assertEqual(len(colors.gradient_groups),1)
        entries[-1]['color']='#123456'
        colors=ArtistColors(entries,names)
        self.assertEqual(colors.spans('혼합 그룹'),((0,5,'#123456'),))
        self.assertFalse(colors.gradient_groups)

    def test_incomplete_ambiguous_or_nested_rosters_do_not_guess_a_color(self):
        for members in ([],None,'如月千早',['如月千早',None],['如月千早','Unknown'],
                        ['如月千早','今井麻美'],['如月千早','ノクチル']):
            colors=ArtistColors(self.entries+[{'name':'Mixed Group','kind':'group','members':members}],self.names)
            self.assertEqual(colors.spans('Mixed Group'),())
        entry={'name':'Mixed Group','kind':'group','members':['如月千早','Alias']}
        names=self.names+[{'name':'天海春香','aliases':['Alias']},{'name':'日高愛','aliases':['Alias']}]
        self.assertEqual(ArtistColors(self.entries+[entry],names).spans('Mixed Group'),())

    def test_conflicting_group_colors_or_member_lists_stay_neutral(self):
        entries=self.entries+[{'name':'ノクチル','kind':'group','color':'#123456','members':['如月千早']}]
        self.assertEqual(ArtistColors(entries,self.names).spans('녹칠'),())
        entries=self.entries+[{'name':'Mixed Group','kind':'group','members':[name]}
                              for name in ('如月千早','天海春香')]
        self.assertEqual(ArtistColors(entries,self.names).spans('Mixed Group'),())

    def test_unknown_does_not_inherit_color_or_match_substring(self):
        self.assertEqual(self.catalog.spans('如月千早'), ((0, 4, '#2743d2'),))
        for text in ('', 'Unknown Singer', 'Singer Chihaya Kisaragi Tribute',
                     '如月千早 (Tribute)', 'Song (如月千早 Ver.)', '如月千早への歌', 'AC/DC'):
            self.assertEqual(self.catalog.spans(text), ())

    def test_poplinks_affiliation_preserves_full_aliases_and_unknown_suffixes(self):
        suffix = ' from THE IDOLM@STER POPLINKS'
        alias = '如月千早' + suffix
        colors = ArtistColors(self.entries, self.names + [
            {'name': '天海春香', 'aliases': [alias]}])
        self.assertEqual(colors.spans(alias), ((0, len(alias), '#e22b30'),))
        for value in ('Unknown Singer' + suffix, '如月千早 from Another Project',
                      '如月千早 from THE IDOLM@STER POPLINKS Tribute'):
            with self.subTest(value=value):self.assertEqual(self.catalog.spans(value), ())

    def test_short_alias_requires_franchise_album_context(self):
        self.assertEqual(self.catalog.spans('Ai', 'Other Album'), ())
        self.assertEqual(self.catalog.spans('Ａｉ', 'THE IDOLM@STER'), ((0, 2, '#e4007f'),))

    def test_ambiguous_alias_even_with_uncolored_owner_is_neutral(self):
        names = self.names + [{'name': 'Other Singer', 'ko': '키사라기 치하야', 'aliases': ['Chihaya Kisaragi']}]
        colors = ArtistColors(self.entries, names)
        self.assertEqual(colors.spans('Chihaya Kisaragi'), ())
        self.assertEqual(colors.spans('키사라기 치하야 (CV. Actor)'), ())
        self.assertEqual(colors.spans('如月千早'), ((0, 4, '#2743d2'),))

    def test_conflicting_character_colors_are_neutral(self):
        colors = ArtistColors(self.entries + [{'name': '如月 千早', 'color': '#112233', 'kind': 'idol'}], self.names)
        self.assertEqual(colors.spans('如月千早, Chihaya Kisaragi'), ())

    def test_invalid_rows_are_ignored(self):
        invalid = [None, 'bad', {}, {'name': 'A', 'color': 'blue', 'kind': 'idol'},
                   {'name': 'B', 'color': '#123456', 'kind': 'voice_actor'},
                   {'name': [], 'color': '#123456', 'kind': 'idol'},
                   {'name': 'C', 'color': ['#123456'], 'kind': 'idol'}]
        colors = ArtistColors(invalid + self.entries, [None, {}, {'name': []}] + self.names)
        self.assertEqual(colors.spans('如月千早'), ((0, 4, '#2743d2'),))


class ArtistColorCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patch = patch('artist_colors.RESOURCE_DIRECTORY', self.root)
        self.patch.start()
        catalog.cache_clear()

    def tearDown(self):
        catalog.cache_clear()
        self.patch.stop()
        self.temp.cleanup()

    def test_optional_missing_catalog_returns_no_ranges(self):
        self.assertEqual(character_color_spans('如月千早'), ())

    def test_malformed_file_is_skipped_and_valid_file_still_loads(self):
        colors = self.root / 'artist_colors'
        colors.mkdir()
        (colors / 'imas_legacy.json').write_text('{broken', encoding='utf-8')
        (colors / 'imas_modern.json').write_text(json.dumps({'schema_version': 1, 'entries': [
            {'name': '如月千早', 'color': '#2743d2', 'kind': 'idol'}]}), encoding='utf-8')
        self.assertEqual(character_color_spans('如月千早'), ((0, 4, '#2743d2'),))
        with patch('artist_colors._read_entries', side_effect=AssertionError('catalog read again')):
            self.assertEqual(character_color_spans('如月千早'), ((0, 4, '#2743d2'),))

    def test_bad_schema_or_shape_is_ignored(self):
        colors = self.root / 'artist_colors'
        colors.mkdir()
        path = colors / 'imas_legacy.json'
        for data in ([], None, {'schema_version': 2, 'entries': []}, {'schema_version': 1, 'entries': {}}):
            path.write_text(json.dumps(data), encoding='utf-8')
            catalog.cache_clear()
            self.assertEqual(character_color_spans('如月千早'), ())


class ActualArtistColorCatalogTests(unittest.TestCase):
    def test_poplinks_colors_all_five_characters_without_coloring_affiliation(self):
        from group_credits import with_character_voices

        suffix = ' from THE IDOLM@STER POPLINKS'
        raw = '天海春香、島村卯月、春日未来、天道 輝、櫻木真乃' + suffix
        label = artist_label(with_character_voices([raw], 'THE IDOLM@STER'), 'THE IDOLM@STER')
        spans = character_color_spans(label, 'THE IDOLM@STER')
        self.assertEqual([label[start:end].split(' (CV.')[0] for start, end, _ in spans],
                         ['아마미 하루카', '시마무라 우즈키', '카스가 미라이', '텐도 테루', '사쿠라기 마노'])
        self.assertEqual(spans[-1], (label.index('사쿠라기 마노'), label.index(suffix), '#ffbad6'))
        for credit in ('櫻木真乃', '사쿠라기 마노', '사쿠라기 마노 (CV. 세키네 히토미)'):
            with self.subTest(credit=credit):
                self.assertEqual(character_color_spans('  ' + credit + suffix, 'THE IDOLM@STER'),
                                 ((2, 2 + len(credit), '#ffbad6'),))

    def test_franchise_ensembles_use_brand_accents_without_changing_unit_palettes(self):
        entries = _read_entries(RESOURCE_DIRECTORY / 'artist_colors')
        names = _read_entries(RESOURCE_DIRECTORY / 'artist_names')
        colors = ArtistColors(entries, names)
        expected = {
            'シャイニーカラーズ': ('#7cb0ff', 28),
            'シンデレラガールズ': ('#2581c7', 190),
            '765PRO ALLSTARS': ('#ff74b8', 13),
            '765 MILLION ALLSTARS': ('#ffc000', 52),
            'MILLIONSTARS': ('#ffc000', 39),
            '315 ALLSTARS': ('#11be93', 49),
            '315 STARS': ('#11be93', 0),
        }
        for name, (color, member_count) in expected.items():
            with self.subTest(name=name):
                rows = [entry for entry in entries if entry['name'] == name]
                self.assertEqual(len(rows), 1)
                self.assertEqual(len(rows[0].get('members', [])), member_count)
                self.assertEqual(rows[0]['basis'], 'official-site-brand-accent')
                for text in (name, artist_label([name], 'THE IDOLM@STER')):
                    self.assertEqual(colors.spans(text, 'THE IDOLM@STER'),
                                     ((0, len(text), color),))
        for name in ('Clover', 'Re;IRIS', "I'm a Cutie Finder", 'CINDERELLA PROJECT',
                     'MILLIONSTARS Team1st', 'Team.Stella'):
            with self.subTest(unit=name):
                self.assertIsInstance(colors.spans(name, 'THE IDOLM@STER')[0][2], tuple)

    def test_hatsuboshi_uses_gakuen_brand_color_and_preserves_member_roster(self):
        entries = _read_entries(RESOURCE_DIRECTORY / 'artist_colors')
        names = _read_entries(RESOURCE_DIRECTORY / 'artist_names')
        colors = ArtistColors(entries, names)
        groups = [entry for entry in entries if entry['name'] == '初星学園']
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['members'], [
            '花海咲季', '月村手毬', '藤田ことね', '有村麻央', '葛城リーリヤ',
            '倉本千奈', '紫雲清夏', '篠澤広', '姫崎莉波', '花海佑芽',
            '秦谷美鈴', '十王星南', '雨夜燕',
        ])
        for text in ('初星学園', '하츠보시 학원', artist_label(['初星学園'], 'THE IDOLM@STER')):
            with self.subTest(name=text):
                self.assertEqual(colors.spans(text, 'THE IDOLM@STER'),
                                 ((0, len(text), '#f39800'),))

    def test_all_catalog_idols_resolve_original_names_and_korean_display_names(self):
        entries = _read_entries(RESOURCE_DIRECTORY / 'artist_colors')
        names = _read_entries(RESOURCE_DIRECTORY / 'artist_names')
        colors = ArtistColors(entries, names)
        idols = [entry for entry in entries if entry['kind'] == 'idol']
        self.assertTrue(idols)
        for entry in idols:
            for text in (entry['name'], artist_label([entry['name']], 'THE IDOLM@STER')):
                with self.subTest(name=entry['name'], display=text):
                    self.assertEqual(colors.spans(text, 'THE IDOLM@STER'),
                                     ((0, len(text), entry['color'].lower()),))

    def test_catalog_groups_resolve_and_voice_actors_remain_neutral(self):
        entries = _read_entries(RESOURCE_DIRECTORY / 'artist_colors')
        names = _read_entries(RESOURCE_DIRECTORY / 'artist_names')
        colors = ArtistColors(entries, names)
        for entry in entries:
            if entry['kind'] != 'group':continue
            for text in (entry['name'],artist_label([entry['name']],'THE IDOLM@STER')):
                with self.subTest(name=text):
                    spans=colors.spans(text,'THE IDOLM@STER')
                    self.assertEqual(len(spans),1)
                    self.assertEqual(spans[0][:2],(0,len(text)))
                    if entry.get('color'):self.assertEqual(spans[0][2],entry['color'].lower())
        for entry in names:
            if entry['kind'] != 'voice_actor':continue
            for text in (entry['name'], entry['ko']):
                with self.subTest(name=text):
                    self.assertEqual(colors.spans(text, 'THE IDOLM@STER'), ())


if __name__ == '__main__':unittest.main()
