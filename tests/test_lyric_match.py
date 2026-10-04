import unittest

from lyric_match import MAX_FUZZY_CHARACTERS, match_lyrics


def song_rows():
    # 939 normalized characters, with distinct characters to expose misalignment.
    text = ''.join(chr(0xAC00 + index) for index in range(939))
    return [text[index:index + 30] for index in range(0, len(text), 30)]


def replace_character(rows, row, index, value='X'):
    rows = list(rows)
    rows[row] = rows[row][:index] + value + rows[row][index + 1:]
    return rows


class LyricMatchTests(unittest.TestCase):
    def test_exact_normalized_text_allows_short_split_and_merged_rows(self):
        result = match_lyrics(['Ａ B!', 'C d'], ['a', 'bc', 'd'])
        self.assertEqual(result.edits, 0)
        self.assertEqual(result.source_offsets, (0, 2))
        self.assertEqual(result.target_offsets, (0, 1, 3))
        self.assertEqual(result.source_to_target, (0, 1, 2, 3, 4))

    def test_two_typos_in_939_characters_are_accepted(self):
        source = song_rows()
        target = replace_character(replace_character(source, 1, 9), 20, 17)
        result = match_lyrics(source, target)
        self.assertEqual(result.edits, 2)
        self.assertEqual(result.source_to_target, tuple(range(940)))

    def test_typos_allow_different_row_splits(self):
        source = song_rows()
        changed = replace_character(replace_character(source, 2, 5), 20, 12)
        target = [''.join(changed[index:index + 2]) for index in range(0, len(changed), 2)]
        result = match_lyrics(source, target)
        self.assertEqual(result.edits, 2)
        self.assertEqual(result.target_offsets, tuple(range(0, 939, 60)))
        self.assertEqual(result.source_to_target, tuple(range(940)))

    def test_one_inserted_character_shifts_only_following_boundaries(self):
        source = song_rows()
        target = list(source)
        target[2] = target[2][:10] + 'X' + target[2][10:]
        result = match_lyrics(source, target)
        self.assertEqual(result.edits, 1)
        self.assertEqual(result.source_to_target[:70], tuple(range(70)))
        self.assertEqual(result.source_to_target[70:], tuple(range(71, 941)))
        self.assertEqual(result.target_offsets[3], 91)

    def test_one_deleted_character_collapses_a_boundary(self):
        source = song_rows()
        target = list(source)
        target[2] = target[2][:10] + target[2][11:]
        result = match_lyrics(source, target)
        self.assertEqual(result.edits, 1)
        self.assertEqual(result.source_to_target[:71], tuple(range(71)))
        self.assertEqual(result.source_to_target[71:], tuple(range(70, 939)))

    def test_short_or_wholly_different_text_is_rejected(self):
        self.assertIsNone(match_lyrics(['hello'], ['hallo']))
        self.assertIsNone(match_lyrics(['a' * 200], ['b' * 200]))

    def test_one_percent_limit_and_eight_character_cap(self):
        for size, edits, accepted in ((100, 1, True), (199, 2, False),
                                      (200, 2, True), (939, 8, True), (939, 9, False)):
            with self.subTest(size=size, edits=edits):
                source = ''.join(chr(0xAC00 + index) for index in range(size))
                target = list(source)
                for index in range(edits):
                    target[10 * index + 5] = 'X'
                self.assertEqual(match_lyrics([source], [''.join(target)]) is not None, accepted)

    def test_three_consecutive_edits_are_rejected(self):
        source = song_rows()
        for count in (2, 3):
            target = list(source)
            target[3] = target[3][:10] + 'X' * count + target[3][10 + count:]
            self.assertEqual(match_lyrics(source, target) is not None, count == 2)

    def test_too_many_changes_within_one_row_are_rejected(self):
        source = song_rows() + ['abcdefghij']
        target = source[:-1] + ['XYcdefghiZ']
        self.assertIsNone(match_lyrics(source, target))

    def test_missing_added_replaced_or_reordered_rows_are_rejected(self):
        source = song_rows()
        for target in (source[:3] + source[4:],
                       source[:3] + ['X'] + source[3:],
                       source[:3] + [source[4], source[3]] + source[5:]):
            self.assertIsNone(match_lyrics(source, target))
        short_row = source[:3] + ['X'] + source[3:]
        self.assertIsNone(match_lyrics(short_row, source))
        self.assertIsNone(match_lyrics(short_row, source[:3] + ['Y'] + source[3:]))

    def test_gap_at_row_onset_is_not_a_typo_inside_a_row(self):
        source = song_rows()
        target = list(source)
        target[3] = 'X' + target[3]
        self.assertIsNone(match_lyrics(source, target))
        self.assertIsNone(match_lyrics(target, source))

    def test_repeated_characters_do_not_conceal_a_missing_short_row(self):
        source = ['z' + 'a' * 200, 'a', 'a' * 200 + 'z']
        target = [source[0], source[2]]
        self.assertIsNone(match_lyrics(source, target))
        self.assertIsNone(match_lyrics(target, source))

    def test_other_typo_does_not_conceal_a_missing_repeated_short_row(self):
        for short_row in ('a', 'aa'):
            with self.subTest(short_row=short_row):
                source = ['z' + 'a' * 200, short_row, 'a' * 200 + 'z', 'uniqueendingline']
                target = [source[0], source[2], source[3].replace('q', 'x')]
                self.assertIsNone(match_lyrics(source, target))
                self.assertIsNone(match_lyrics(target, source))

    def test_unchanged_short_rows_allow_a_typo_elsewhere(self):
        source = song_rows() + ['a', 'bc', 'def', 'ghij']
        target = replace_character(source, 2, 10)
        self.assertEqual(match_lyrics(source, target).edits, 1)
        self.assertEqual(match_lyrics(target, source).edits, 1)

    def test_short_rows_can_merge_with_substitution_elsewhere(self):
        source = song_rows() + ['a', 'bc']
        target = replace_character(source, 2, 10)
        target = target[:-2] + ['abc']
        self.assertEqual(match_lyrics(source, target).edits, 1)
        self.assertEqual(match_lyrics(target, source).edits, 1)

    def test_unchanged_short_rows_allow_an_internal_gap_elsewhere(self):
        source = song_rows() + ['a', 'bc', 'def', 'ghij']
        target = list(source)
        target[2] = target[2][:10] + 'X' + target[2][10:]
        self.assertEqual(match_lyrics(source, target).edits, 1)
        self.assertEqual(match_lyrics(target, source).edits, 1)

    def test_repeated_chorus_keeps_order_and_stable_offsets(self):
        rows = song_rows()
        source = rows[:8] + rows[8:16] + rows[:8] + rows[16:24]
        target = replace_character(source, 18, 12)
        result = match_lyrics(source, target)
        self.assertEqual(result.edits, 1)
        self.assertEqual(result.source_offsets, result.target_offsets)
        self.assertEqual(result.source_to_target, tuple(range(961)))
        self.assertEqual(result, match_lyrics(source, target))
        self.assertIsNone(match_lyrics(source, target[:16] + target[24:]))

    def test_fuzzy_work_is_bounded_but_large_exact_text_still_matches(self):
        source = 'a' * (MAX_FUZZY_CHARACTERS + 1)
        self.assertIsNone(match_lyrics([source], [source[:100] + 'b' + source[101:]]))
        self.assertEqual(match_lyrics([source], [source]).edits, 0)

    def test_empty_or_normalized_blank_rows_are_rejected(self):
        for source, target in (([], []), (['a', '!'], ['a']), (['a'], ['', 'a'])):
            self.assertIsNone(match_lyrics(source, target))


if __name__ == '__main__':
    unittest.main()
