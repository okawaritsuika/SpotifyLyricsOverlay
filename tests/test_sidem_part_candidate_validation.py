import unittest

from tools.verify_sidem_part_candidates import (
    album_identity, album_release_anchor, clean_url, compatible_roster, credit_matches, digest, parse_source, parts_of,
    official_profile_pages, register_official_cast, resolve_label, verify_runtime,
)


PEOPLE = ['天道輝', '桜庭薫', '柏木翼']
LOOKUP = {name: name for name in PEOPLE}


def article(text, header='DRAMATIC STARS(天道輝・桜庭薫・柏木翼)'):
    return '<h1 class="skin-entryTitle">Test Song</h1><div class="skin-entryBody">' + header + '<p>' + text + '</p></div>'


class SideMPartValidationTests(unittest.TestCase):
    def test_full_roster_and_explicit_labels_are_required(self):
        source = article('(天道):' + 'あいうえお' * 10 + '(桜庭):' + 'かきくけこ' * 10 +
                         '(柏木):' + 'さしすせそ' * 10 + '(全員):' + 'たちつてと' * 10)
        text, values, people, evidence = parse_source(source, 'Test Song', LOOKUP)
        self.assertEqual(people, PEOPLE)
        self.assertEqual(values[:50], [(PEOPLE[0],)] * 50)
        self.assertEqual(values[-50:], [tuple(PEOPLE)] * 50)
        self.assertEqual(evidence['explicit_labels'], 4)

    def test_no_unit_name_expansion(self):
        source = article('(天道):' + 'あ' * 50 + '(桜庭):' + 'い' * 50 +
                         '(柏木):' + 'う' * 50 + '(全員):' + 'え' * 50, 'DRAMATIC STARS')
        with self.assertRaisesRegex(ValueError, 'full-name header'):
            parse_source(source, 'Test Song', LOOKUP)

    def test_ordinary_parenthetical_name_is_preserved(self):
        source = article('(天道):' + 'あ' * 50 + '(桜庭):' + 'い' * 50 + '(輝)' +
                         '(柏木):' + 'う' * 50 + '(全員):' + 'え' * 50)
        text, values, _, _ = parse_source(source, 'Test Song', LOOKUP)
        self.assertIn('輝', text)
        self.assertEqual(values[text.index('輝')], (PEOPLE[1],))

    def test_unknown_explicit_label_clears_scope(self):
        source = article('(天道):' + 'あ' * 50 + '(桜庭):' + 'い' * 50 +
                         '(不明):' + 'unknown' + '(柏木):' + 'う' * 50 + '(全員):' + 'え' * 50)
        text, values, _, evidence = parse_source(source, 'Test Song', LOOKUP)
        self.assertEqual(values[text.index('unknown')], ())
        self.assertEqual(evidence['unknown_labels'], ['不明'])

    def test_mixed_unknown_label_never_grants_known_subset(self):
        self.assertEqual(resolve_label('天道・不明', PEOPLE), ())

    def test_label_with_embedded_colon_and_abbreviations(self):
        self.assertEqual(resolve_label('全員:', PEOPLE), tuple(PEOPLE))
        self.assertEqual(resolve_label('輝･薫', PEOPLE), tuple(PEOPLE[:2]))

    def test_trackers_are_removed(self):
        self.assertEqual(clean_url('http://ameblo.jp/test/entry-123.html?device_id=secret#top'),
                         'https://ameblo.jp/test/entry-123.html')

    def test_album_numbers_cannot_be_fuzzily_matched(self):
        self.assertFalse(album_identity('THE IDOLM@STER SideM GROWING SIGN@L 01',
                                       'THE IDOLM@STER SideM GROWING SIGN@L 02'))
        self.assertTrue(album_identity('GAME『アイドルマスター SideM』 THE IDOLM@STER SideM ST@RTING LINE -02 DRAMATIC STARS',
                                      'THE IDOLM@STER SideM ST@RTING LINE-02 DRAMATIC STARS'))
        self.assertFalse(album_identity('Synthetic Album', 'Synthetic Album Acoustic'))
        self.assertTrue(album_identity('THE IDOLM@STER SideM 2nd ANNIVERSARY DISC 02',
                                       'THE IDOLM@STER SideM 2nd ANNIVERSARY DISC 02 Beit & S.E.M', ['Beit', 'S.E.M']))

    def test_credit_requires_the_complete_identical_roster(self):
        self.assertTrue(credit_matches('DRAMATIC STARS', ['DRAMATIC STARS'], PEOPLE))
        self.assertTrue(credit_matches('天道輝(CV.仲村宗悟)、桜庭薫(CV.内田雄馬)、柏木翼(CV.八代拓)', ['DRAMATIC STARS'], PEOPLE))
        self.assertFalse(credit_matches('DRAMATIC STARS & Beit', ['DRAMATIC STARS'], PEOPLE))
        self.assertFalse(credit_matches('天道輝(CV.仲村宗悟)', ['DRAMATIC STARS'], PEOPLE))

    def test_original_album_date_cannot_anchor_a_reissue_album(self):
        song = {'first_release_date': '2015-01-01', 'releases': [
            {'title': 'Original Album', 'date': '2015-01-01'},
            {'title': 'Anniversary Album', 'date': '2025-01-01'}]}
        self.assertTrue(album_release_anchor(song, 'Original Album', '2015-01-01'))
        self.assertFalse(album_release_anchor(song, 'Anniversary Album', '2015-01-01'))

    def test_official_source_must_prove_the_same_album_and_track(self):
        page = {'url': 'https://www.lantis.jp/test', 'album': 'Original Album',
                'tracks': [{'title': 'Test Song', 'credit': 'DRAMATIC STARS'}]}
        profile = {'title': 'Test Song', 'artists': ['DRAMATIC STARS'], 'album': 'Original Album'}
        self.assertEqual(official_profile_pages(profile, PEOPLE, [page], {}), [page])
        self.assertEqual(official_profile_pages({**profile, 'album': 'Anniversary Album'}, PEOPLE, [page], {}), [])
        self.assertEqual(official_profile_pages({**profile, 'title': 'Other Song'}, PEOPLE, [page], {}), [])

    def test_actor_parentheses_need_a_verified_character_actor_pair(self):
        metadata = {'members': [{'name': name, 'voice_actors': [{'name': actor}]} for name, actor in
                               zip(PEOPLE, ['仲村宗悟', '内田雄馬', '八代拓'])]}
        register_official_cast([], metadata, LOOKUP)
        self.assertTrue(credit_matches('天道輝(仲村宗悟), 桜庭薫(内田雄馬), 柏木翼(八代拓)', ['DRAMATIC STARS'], PEOPLE))
        self.assertFalse(credit_matches('天道輝(八代拓), 桜庭薫(内田雄馬), 柏木翼(八代拓)', ['DRAMATIC STARS'], PEOPLE))
        self.assertFalse(credit_matches('天道輝(CV.八代拓), 桜庭薫(CV.内田雄馬), 柏木翼(CV.八代拓)', ['DRAMATIC STARS'], PEOPLE))

    def test_combined_groups_only_confirm_an_already_explicit_roster(self):
        groups = {'a': {frozenset(PEOPLE[:2])}, 'b': {frozenset(PEOPLE[2:])}}
        self.assertTrue(compatible_roster(['A', 'B'], PEOPLE, LOOKUP, groups))
        self.assertFalse(compatible_roster(['A', 'B'], PEOPLE[:2], LOOKUP, groups))
        self.assertFalse(compatible_roster(['A', 'B', 'Wrong Artist'], PEOPLE, LOOKUP, groups))

    def test_actual_runtime_accepts_exact_parts_and_rejects_other_recordings(self):
        text = 'あ' * 20 + 'い' * 20 + 'う' * 20
        assigned = [(PEOPLE[0],)] * 20 + [()] * 20 + [(PEOPLE[1],)] * 20
        profile = {'title': 'Test Song', 'artists': ['DRAMATIC STARS'], 'album': 'Test Album',
                   'text_sha256': digest(text), 'text_length': len(text),
                   'sources': [{'url': 'https://ameblo.jp/test/entry-123.html', 'kind': 'fan_part_transcription'}],
                   'parts': parts_of(assigned)}
        self.assertEqual(verify_runtime(profile, text, assigned), 3)


if __name__ == '__main__':
    unittest.main()
