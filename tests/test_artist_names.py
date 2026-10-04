import unittest

from artist_names import ArtistNames


class ArtistNameTests(unittest.TestCase):
    def setUp(self):
        self.catalog = ArtistNames([
            {'name': 'イルミネーションスターズ', 'ko': '일루미네이션 스타즈', 'aliases': ['illumination STARS']},
            {'name': 'ノクチル', 'ko': '녹칠'},
            {'name': '如月千早', 'ko': '키사라기 치하야', 'aliases': ['Chihaya Kisaragi']},
            {'name': '今井麻美', 'ko': '이마이 아사미'},
            {'name': 'W', 'ko': '더블유'},
            {'name': 'High×Joker', 'ko': '하이 조커'},
        ])

    def test_multiple_verified_artists_are_all_translated_in_order(self):
        self.assertEqual(self.catalog.display(('イルミネーションスターズ', 'ノクチル')),
                         '일루미네이션 스타즈, 녹칠')

    def test_spotify_character_and_cv_credit(self):
        self.assertEqual(self.catalog.translate('如月 千早（CV. 今井 麻美）'),
                         '키사라기 치하야 (CV. 이마이 아사미)')

    def test_composite_credit_and_group_punctuation(self):
        self.assertEqual(self.catalog.translate('イルミネーションスターズ × ノクチル'),
                         '일루미네이션 스타즈 × 녹칠')
        self.assertEqual(self.catalog.translate('High×Joker'), '하이 조커')
        self.assertEqual(self.catalog.translate('AC/DC'), 'AC/DC')

    def test_fullwidth_and_multiline_credit_separators_match_color_spans(self):
        for separator in ('，', ' ＋ ', ' ／ ', ' ｘ ', ' Ｘ ', '\n', '\r\n'):
            with self.subTest(separator=separator):
                self.assertEqual(self.catalog.translate('ノクチル' + separator + '如月千早'),
                                 '녹칠' + separator + '키사라기 치하야')

    def test_cv_credit_and_unknown_performer_survive_fullwidth_separator(self):
        self.assertEqual(self.catalog.translate('如月千早 (CV. 今井麻美)，Unknown Singer'),
                         '키사라기 치하야 (CV. 이마이 아사미)，Unknown Singer')
        self.assertEqual(self.catalog.translate('Unknown［ノクチル，W］，如月千早'),
                         'Unknown［ノクチル，W］，키사라기 치하야')

    def test_official_group_punctuation_survives_fullwidth_separator(self):
        names=ArtistNames([
            {'name':'Re;IRIS','ko':'리리스','kind':'group'},
            {'name':'High×Joker','ko':'하이 조커','kind':'group'},
            {'name':"I'm a Cutie Finder",'ko':'아임 어 큐티 파인더','kind':'group'},
            {'name':'ノクチル','ko':'녹칠','kind':'group'},
        ])
        self.assertEqual(names.translate("Re;IRIS，High×Joker ＋ I'm a Cutie Finder ／ ノクチル"),
                         "Re;IRIS，High×Joker ＋ I'm a Cutie Finder ／ 녹칠")

    def test_whole_token_only_unknown_suffix_and_names_preserved(self):
        for name in ('ノクチルズ', 'Singer Chihaya Kisaragi Tribute', 'New Artist'):
            self.assertEqual(self.catalog.translate(name), name)

    def test_poplinks_affiliation_translates_character_and_preserves_source_text(self):
        names = ArtistNames([{'name': '櫻木真乃', 'ko': '사쿠라기 마노'}])
        suffix = '  from THE IDOLM@STER POPLINKS'
        self.assertEqual(names.translate('櫻木真乃' + suffix), '사쿠라기 마노' + suffix)
        for value in ('Unknown Singer' + suffix, '櫻木真乃 from Another Project',
                      '櫻木真乃 from THE IDOLM@STER POPLINKS Tribute'):
            with self.subTest(value=value):self.assertEqual(names.translate(value), value)

    def test_poplinks_affiliation_does_not_override_existing_full_name_alias(self):
        alias = '櫻木真乃 from THE IDOLM@STER POPLINKS'
        entries = [{'name': '櫻木真乃', 'ko': '사쿠라기 마노'},
                   {'name': 'Official Credit', 'ko': '공식 크레딧', 'aliases': [alias]}]
        names = ArtistNames(entries)
        self.assertEqual(names.translate(alias), '공식 크레딧')
        ambiguous = ArtistNames(entries + [
            {'name': 'Other Credit', 'ko': '다른 크레딧', 'aliases': [alias]}])
        self.assertEqual(ambiguous.translate(alias), alias)

    def test_latin_alias_case_and_fullwidth_spacing(self):
        self.assertEqual(self.catalog.translate('Ｉｌｌｕｍｉｎａｔｉｏｎ　ＳＴＡＲＳ'), '일루미네이션 스타즈')

    def test_short_artist_name_needs_franchise_context(self):
        self.assertEqual(self.catalog.display(('W',), 'Other Album'), 'W')
        self.assertEqual(self.catalog.display(('W',), 'THE IDOLM@STER SideM'), '더블유')

    def test_ambiguous_alias_never_picks_first_or_last(self):
        names = ArtistNames([{'name': 'One', 'ko': '하나', 'aliases': ['shared artist']},
                             {'name': 'Two', 'ko': '둘', 'aliases': ['shared artist']}])
        self.assertEqual(names.translate('shared artist'), 'shared artist')

    def test_latin_group_names_keep_canonical_spelling_and_punctuation(self):
        names=ArtistNames([
            {'name':'Re;IRIS','ko':'리리스','kind':'group','aliases':['Re:IRIS']},
            {'name':"I'm a Cutie Finder",'ko':'아임 어 큐티 파인더','kind':'group'},
            {'name':'Clover','ko':'클로버','kind':'group'},
            {'name':'High×Joker','ko':'하이 조커','kind':'group'},
            {'name':'W','ko':'더블유','kind':'group'},
            {'name':'ノクチル','ko':'녹칠','kind':'group'},
            {'name':'Julia','ko':'줄리아','kind':'idol'},
        ])
        self.assertEqual(names.display(['Re:IRIS',"I'm a Cutie Finder",'Clover','High×Joker','W',
                                        'ノクチル','Julia'],'THE IDOLM@STER'),
                         "Re;IRIS, I'm a Cutie Finder, Clover, High×Joker, W, 녹칠, 줄리아")
        self.assertEqual(names.translate('리리스'),'Re;IRIS')
        self.assertEqual(names.translate('Unknown English Group'),'Unknown English Group')


if __name__ == '__main__':unittest.main()
