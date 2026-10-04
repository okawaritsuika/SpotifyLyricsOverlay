import unittest

from audio_sync import match_partial_words,match_words,partial_alignment_text
from lyrics import Line


def lines(*values):
    return [Line(text,'','') for text in values]


def word(text,start=2,end=4,probability=.95):
    return {'word':text,'start':start,'end':end,'probability':probability}


class PartialLyricMatchTests(unittest.TestCase):
    def test_exact_row_is_not_distributed_across_unheard_later_rows(self):
        text=lines('明日は晴れるよ','明るい夜','日は昇る','晴れた海','れる','よかった')
        heard=[word('明日は晴れるよ')]
        self.assertEqual(match_words(text,heard)['times'],[None]*6)
        result=match_partial_words(text,heard)
        self.assertEqual(result['times'],[2,None,None,None,None,None])
        self.assertEqual(result['coverage'][0],1)
        self.assertEqual(result['ends'][0],4)

    def test_distinct_consecutive_rows_keep_their_measured_positions(self):
        text=lines('青い空を眺めよう','朝の窓を開けよう','遠い山まで歩こう','山の向こうは青い空')
        result=match_partial_words(text,[word(text[1].original,2,4),word(text[2].original,6,9)],40,100)
        self.assertEqual(result['times'],[None,42,46,None])
        self.assertEqual(result['ends'],[None,44,49,None])
        self.assertEqual(result['matched'],2)

    def test_katakana_asr_and_kanji_lyrics_share_japanese_reading(self):
        result=match_partial_words(lines('君に会いたい'),[word('キミニアイタイ')],10,100)
        self.assertEqual(result['times'],[12]);self.assertEqual(result['ends'],[14])

    def test_homophone_orthography_is_not_a_mismatch(self):
        result=match_partial_words(lines('君に会いたい'),[word('君に逢いたい')])
        self.assertEqual(result['times'],[2]);self.assertEqual(result['coverage'],[1])

    def test_reading_expansion_keeps_original_word_start_and_end(self):
        result=match_partial_words(lines('学校に行こう'),[
            word('ガッコウ',1,1.8),word('ニ',1.9,2.1),word('イコウ',2.2,3.1)],50,100)
        self.assertEqual(result['times'],[51]);self.assertEqual(result['ends'],[53.1])

    def test_low_confidence_word_splits_evidence_instead_of_joining_fragments(self):
        result=match_partial_words(lines('朝の窓を開けよう'),[
            word('朝の窓',1,2),word('を',2,2.2,.1),word('開けよう',2.2,4)])
        self.assertEqual(result['times'],[None])

    def test_unknown_word_separator_also_prevents_fragment_joining(self):
        result=match_partial_words(lines('朝の窓を開けよう'),[
            word('朝の窓',1,2),None,word('開けよう',2.2,4)])
        self.assertEqual(result['times'],[None])

    def test_unique_following_row_identifies_only_the_earlier_chorus(self):
        text=lines('明日は晴れるよ','青い海に向かおう','明日は晴れるよ')
        result=match_partial_words(text,[word('明日は晴れるよ',1,3),word('青い海に向かおう',5,7)])
        self.assertEqual(result['times'],[1,5,None])

    def test_repeated_chorus_without_a_unique_anchor_remains_ambiguous(self):
        result=match_partial_words(lines('明日は晴れるよ','明日は晴れるよ'),[word('明日は晴れるよ')])
        self.assertEqual(result['times'],[None,None])

    def test_unique_preceding_row_identifies_only_the_later_chorus(self):
        text=lines('明日は晴れるよ','青い海に向かおう','明日は晴れるよ')
        result=match_partial_words(text,[word('青い海に向かおう',1,3),word('明日は晴れるよ',5,7)])
        self.assertEqual(result['times'],[None,1,5])

    def test_two_unique_anchors_identify_the_middle_of_three_refrains(self):
        text=lines('明日は晴れるよ','青い海に向かおう','明日は晴れるよ','遠い山まで歩こう','明日は晴れるよ')
        result=match_partial_words(text,[word('青い海に向かおう',5,7),word('明日は晴れるよ',10,12),
                                        word('遠い山まで歩こう',15,17)])
        self.assertEqual(result['times'],[None,5,10,15,None])

    def test_two_possible_refrains_before_one_anchor_still_remain_ambiguous(self):
        text=lines('明日は晴れるよ','明日は晴れるよ','青い海に向かおう')
        result=match_partial_words(text,[word('明日は晴れるよ',1,3),word('青い海に向かおう',5,7)])
        self.assertEqual(result['times'],[None,None,5])

    def test_same_phonetic_refrain_is_resolved_only_by_the_unique_anchor(self):
        text=lines('君に会いたい','青い海に向かおう','君に逢いたい')
        result=match_partial_words(text,[word('キミニアイタイ',1,3),word('青い海に向かおう',5,7)])
        self.assertEqual(result['times'],[1,5,None])

    def test_same_phonetic_line_with_different_kanji_is_ambiguous(self):
        result=match_partial_words(lines('君に会いたい','君に逢いたい'),[word('君に会いたい')])
        self.assertEqual(result['times'],[None,None])

    def test_phrase_contained_in_a_longer_other_row_is_not_unique(self):
        result=match_partial_words(lines('明日は晴れるよ','今日も明日は晴れるよと願う'),[word('明日は晴れるよ')])
        self.assertEqual(result['times'],[None,None])

    def test_backing_asides_do_not_make_the_same_chorus_unique(self):
        text=lines('明日は晴れるよ','明日は（Oh Fu-Fu!）晴れるよ','（Yeah）明日は晴れるよ')
        result=match_partial_words(text,[word('明日は晴れるよ')])
        self.assertEqual(result['times'],[None,None,None])

    def test_absent_aside_candidate_still_counts_as_a_possible_chorus(self):
        text=lines('明日は晴れるよ','明日は（Oh Fu-Fu! Oh Yeah!）晴れるよ','青い海に向かおう')
        result=match_partial_words(text,[word('明日は晴れるよ',1,3),word('青い海に向かおう',5,7)])
        self.assertEqual(result['times'],[None,None,5])

    def test_independent_anchor_can_exclude_the_aside_variant(self):
        text=lines('明日は晴れるよ','青い海に向かおう','明日は（Oh Fu-Fu!）晴れるよ')
        result=match_partial_words(text,[word('明日は晴れるよ',1,3),word('青い海に向かおう',5,7)])
        self.assertEqual(result['times'],[1,5,None])

    def test_absent_longer_row_candidate_remains_ambiguous_before_an_anchor(self):
        text=lines('明日は晴れるよ','今日も明日は晴れるよと願う','青い海に向かおう')
        result=match_partial_words(text,[word('明日は晴れるよ',1,3),word('青い海に向かおう',5,7)])
        self.assertEqual(result['times'],[None,None,5])

    def test_internal_backing_call_is_optional_when_the_main_onset_is_heard(self):
        text='朝の窓（Oh Fu-Fu! Yeah!）を開けよう'
        result=match_partial_words(lines(text),[word('朝の窓を開けよう')])
        self.assertEqual(result['times'],[2]);self.assertEqual(result['ends'],[4])
        self.assertEqual(partial_alignment_text(text),'朝の窓を開けよう')

    def test_trailing_backing_call_is_optional_for_matching_and_forced_text(self):
        text='朝の窓を開けよう（Oh Fu-Fu! Yeah!）'
        result=match_partial_words(lines(text),[word('朝の窓を開けよう')])
        self.assertEqual(result['times'],[2]);self.assertEqual(result['ends'],[4])
        self.assertEqual(partial_alignment_text(text),'朝の窓を開けよう')

    def test_main_surface_can_match_in_the_reading_channel(self):
        text='学校（Oh Fu-Fu! Yeah!）に行こう'
        result=match_partial_words(lines(text),[word('ガッコウニイコウ')])
        self.assertEqual(result['times'],[2])

    def test_prefix_call_is_never_removed_or_replaced_by_a_later_main_onset(self):
        for text in ('（Oh Fu-Fu! Yeah!）朝の窓を開けよう','♪（Oh Fu-Fu!）朝の窓を開けよう'):
            with self.subTest(text=text):
                self.assertEqual(partial_alignment_text(text),text)
                self.assertEqual(match_partial_words(lines(text),[word('朝の窓を開けよう')])['times'],[None])

    def test_unbalanced_parenthesis_does_not_create_a_main_surface(self):
        text='朝の窓（Oh Fu-Fu! を開けよう'
        self.assertEqual(partial_alignment_text(text),text)
        self.assertEqual(match_partial_words(lines(text),[word('朝の窓を開けよう')])['times'],[None])

    def test_main_variant_cannot_accept_a_missing_first_character(self):
        result=match_partial_words(lines('君と明日（Oh Fu-Fu! Yeah!）の空を見よう'),[word('と明日の空を見よう')])
        self.assertEqual(result['times'],[None])

    def test_repeated_observation_cannot_choose_one_of_two_times(self):
        result=match_partial_words(lines('明日は晴れるよ'),[
            word('明日は晴れるよ',1,3),word('明日は晴れるよ',9,11)])
        self.assertEqual(result['times'],[None])

    def test_common_characters_with_large_internal_skips_are_not_a_phrase(self):
        result=match_partial_words(lines('あいうえおかき'),[word('あxいyうzえおかき')])
        self.assertEqual(result['times'],[None])

    def test_short_or_vocalise_fragments_do_not_supply_a_clock(self):
        for text in ('君と','らららら','Wow wow wow'):
            with self.subTest(text=text):
                self.assertEqual(match_partial_words(lines(text),[word(text)])['times'],[None])

    def test_observation_longer_than_twenty_five_seconds_is_rejected(self):
        result=match_partial_words(lines('朝の窓を開けよう'),[
            word('朝の窓',1,2),word('を開けよう',29,31)])
        self.assertEqual(result['times'],[None])

    def test_mid_word_substring_does_not_invent_a_line_onset(self):
        result=match_partial_words(lines('窓を開けよう'),[word('準備して窓を開けよう')])
        self.assertEqual(result['times'],[None])

    def test_duplicate_starts_cannot_create_nonmonotonic_times(self):
        result=match_partial_words(lines('朝の窓を開けよう','遠い山まで歩こう'),[
            word('朝の窓を開けよう',2,4),word('遠い山まで歩こう',2,4)])
        self.assertEqual(result['matched'],1)
        self.assertEqual([t for t in result['times'] if t is not None],[2])


if __name__=='__main__':unittest.main()
