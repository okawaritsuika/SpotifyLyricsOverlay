import unittest

from lyric_asides import aside_onsets
from lyric_match import match_lyrics


def documents():
    rows = [''.join(chr(0x4e00 + row * 30 + column) for column in range(30))
            for row in range(30)]
    source, target = list(rows), list(rows)
    source[1] += '（小さな声）'
    target[1] += '（小さな声）'
    target[12] += '（ハイ）'
    return source, target


class AsideOnsetTests(unittest.TestCase):
    def test_backing_call_differences_keep_exact_main_row_starts(self):
        source, target = documents()
        edits, pairs = aside_onsets(source, target)
        self.assertEqual(edits, 0)
        self.assertEqual(pairs, tuple((index, index) for index in range(30)))

    def test_small_distributed_typos_do_not_weaken_ordinary_matching(self):
        source, target = documents()
        for index in range(2, 11):
            source[index] = source[index][:10] + '異' + source[index][11:]
        self.assertIsNone(match_lyrics(source, target))
        edits, pairs = aside_onsets(source, target)
        self.assertEqual(edits, 9)
        self.assertEqual(pairs, tuple((index, index) for index in range(30)
                                     if index not in range(2, 11)))

    def test_merged_rows_supply_only_the_known_first_onset(self):
        source, target = documents()
        source[4:6] = [''.join(source[4:6])]
        pairs = dict(aside_onsets(source, target)[1])
        self.assertEqual(pairs[4], 4)
        self.assertEqual(pairs[5], 6)
        self.assertNotIn(5, pairs.values())

    def test_backing_call_prefix_has_no_known_main_vocal_onset(self):
        source, target = documents()
        source[3] = '（声）' + source[3]
        target[4] = '（声）' + target[4]
        pairs = dict(aside_onsets(source, target)[1])
        self.assertNotIn(3, pairs)
        self.assertNotIn(4, pairs.values())

    def test_missing_main_row_and_different_order_are_rejected(self):
        for change in ('missing', 'order', 'new_verse'):
            source, target = documents()
            if change == 'missing':
                del source[8]
            elif change == 'order':
                source[8], source[9] = source[9], source[8]
            else:
                source[8] = '他の歌曲の異なる歌詞が続きます' * 2
            with self.subTest(change=change):
                self.assertIsNone(aside_onsets(source, target))

    def test_omitted_short_row_cannot_hide_inside_typo_budget(self):
        source, target = documents()
        target.insert(4, 'あ')
        self.assertIsNone(aside_onsets(source, target))

    def test_typo_budget_and_consecutive_edit_limit_are_bounded(self):
        for changes in (13, 3):
            source, target = documents()
            if changes == 13:
                for index in range(2, 15):
                    source[index] = source[index][:10] + '異' + source[index][11:]
            else:
                source[4] = source[4][:10] + '異異異' + source[4][13:]
            self.assertIsNone(aside_onsets(source, target))

    def test_only_different_explicit_calls_on_both_sides_qualify(self):
        source, target = documents()
        for first, second in ((source, source), ([row.split('（')[0] for row in source], target)):
            self.assertIsNone(aside_onsets(first, second))

    def test_malformed_excessive_and_long_parentheticals_are_rejected(self):
        for call in ('（unclosed', '（' + '声' * 121 + '）', '（' + '声' * 100 + '）'):
            source, target = documents()
            if len(call) < 110 and call.endswith('）'):
                source = [row + call for row in source]
            else:
                source[4] += call
            self.assertIsNone(aside_onsets(source, target))


if __name__ == '__main__':
    unittest.main()
