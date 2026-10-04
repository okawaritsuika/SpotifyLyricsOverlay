import unittest

from tools.build_lyric_part_expansion import aligned_values, compatible, installation_candidates, parse_page


class PartExpansionTests(unittest.TestCase):
    def page(self, content):
        return ('<div class="skin-entryBody"><span style="color:#f00">天海春香</span>'
                '<span style="color:#00f">如月千早</span>' + content + '</div>')

    def parse(self, content):
        return parse_page(self.page(content), {'天海春香': '天海春香', '如月千早': '如月千早'})

    def test_color_only_does_not_carry_into_uncolored_calls(self):
        text, people, _ = self.parse('<span style="color:#f00">あいうえお</span>' + 'かきくけこ' * 25)
        self.assertTrue(all(value == ('天海春香',) for value in people[:5]))
        self.assertFalse(any(people[5:]))

    def test_explicit_all_scope_ends_at_a_new_solo_color(self):
        text, people, _ = self.parse('（全員）あいうえお<span style="color:#f00">かきくけこ</span>' + 'さしすせそ' * 25)
        self.assertTrue(all(set(value) == {'天海春香', '如月千早'} for value in people[:5]))
        self.assertTrue(all(value == ('天海春香',) for value in people[5:10]))
        self.assertFalse(any(people[10:]))

    def test_explicit_same_singer_scope_ends_when_color_takes_over(self):
        text, people, _ = self.parse('（春香）あいうえお<span style="color:#f00">かきくけこ</span>' + 'さしすせそ' * 25)
        self.assertTrue(all(value == ('天海春香',) for value in people[:10]))
        self.assertFalse(any(people[10:]))

    def test_parenthetical_singer_label_can_cross_spans(self):
        text, people, _ = self.parse('<span>（千</span><span>早）</span>' + 'あいうえお' * 25)
        self.assertEqual(text, 'あいうえお' * 25)
        self.assertTrue(all(value == ('如月千早',) for value in people))

    def test_unrecognized_parenthetical_call_is_retained_without_credit(self):
        text, people, _ = self.parse('(yeah)' + 'あいうえお' * 25)
        self.assertTrue(text.startswith('yeah'))
        self.assertFalse(any(people))

    def test_ruby_reading_is_not_appended_to_lyrics(self):
        text, _, _ = self.parse('<ruby>春<rt>はる</rt></ruby>' + 'あいうえお' * 25)
        self.assertTrue(text.startswith('春あ'))

    def test_row_removal_cannot_pass_high_similarity(self):
        source = 'あいうえお' * 100 + 'さしすせそ' * 20
        target = source[:250] + source[260:]
        self.assertIsNone(aligned_values(source, target, [('天海春香',)] * len(source))[0])

    def test_exact_text_maps_repeated_phrases_in_sequence(self):
        source = 'あいうえお' * 60
        values = [('天海春香',)] * 150 + [('如月千早',)] * 150
        self.assertEqual(aligned_values(source, source, values)[0], values)

    def test_substituted_character_is_uncredited_on_the_approved_path(self):
        source = 'あいうえお' * 60
        target = source[:133] + 'か' + source[134:]
        values = [('天海春香',)] * 150 + [('如月千早',)] * 150
        mapped, _ = aligned_values(source, target, values)
        self.assertEqual(mapped[:133], values[:133])
        self.assertEqual(mapped[133], ())
        self.assertEqual(mapped[134:], values[134:])

    def test_unit_roster_must_match_every_explicit_source_singer(self):
        groups = {'unit': {frozenset(('天海春香', '如月千早'))}}
        self.assertTrue(compatible(['Unit'], ['天海春香', '如月千早'], {}, groups))
        self.assertFalse(compatible(['Unit'], ['天海春香'], {}, groups))
        self.assertFalse(compatible(['天海春香'], ['天海春香', '如月千早'], {'天海春香': '天海春香'}, groups))

    def test_install_excludes_source_only_fingerprints(self):
        pending = {'title': 'Pending', 'album': 'Album', 'text_sha256': 'a' * 64}
        checked = {'title': 'Checked', 'album': 'Album', 'text_sha256': 'b' * 64}
        report = {'pages': [{'profiles': [
            {**pending, 'mode': 'source_fingerprint_pending_playback'},
            {**checked, 'mode': 'local_display'}]}]}
        self.assertEqual(installation_candidates([pending, checked], report), [checked])


if __name__ == '__main__':
    unittest.main()
