import unittest

from realtime_lyrics import RealtimeLyricMatcher


def word(text,start=4,end=7.6,probability=.9):
    return {'word':text,'start':start,'end':end,'probability':probability}


class RealtimeLyricMatcherTests(unittest.TestCase):
    def matcher(self,lines,**kwargs):
        return RealtimeLyricMatcher(lines,reader=lambda text:text,**kwargs)

    def confirm(self,matcher,text,left=0):
        self.assertIsNone(matcher.select([word(text)],left,8))
        return matcher.select([word(text,start=3,end=6.6)],left+1,8)

    def test_acquisition_needs_two_different_overlapping_audio_windows(self):
        matcher=self.matcher(['青い空を見上げながら歩こう','風が吹いた'])
        words=[word('青い空を見上げ')]
        self.assertIsNone(matcher.select(words,0,8))
        self.assertIsNone(matcher.select(words,0,8))
        self.assertIsNone(matcher.select(words,0,7))
        self.assertIsNone(matcher.index)
        result=matcher.select([word('青い空を見上げ',start=3,end=6.6)],1,8)
        self.assertEqual(result['index'],0)
        self.assertEqual(result['confirmations'],2)
        self.assertAlmostEqual(result['onset'],4)
        self.assertTrue(result['unique'])

    def test_kanji_and_hiragana_use_existing_local_readings(self):
        matcher=RealtimeLyricMatcher(['青い空を見上げながら歩こう','風に吹かれて'])
        result=self.confirm(matcher,'あおいそらをみあげ',left=10)
        self.assertEqual(result['index'],0)
        self.assertEqual(result['match_method'],'kana')

    def test_katakana_width_and_punctuation_are_equivalent(self):
        matcher=self.matcher(['キラキラと輝く未来へ'])
        result=self.confirm(matcher,'ｷﾗｷﾗと、輝く未来')
        self.assertEqual(result['index'],0)

    def test_clipped_middle_of_line_cannot_invent_an_onset(self):
        matcher=self.matcher(['きみと青い空を見上げながら歩こう'])
        result=self.confirm(matcher,'空を見上げながら')
        self.assertEqual(result['index'],0)
        self.assertIsNone(result['onset'])
        self.assertEqual(result['evidence_start'],4)

    def test_audio_window_edge_cannot_supply_true_line_onset(self):
        matcher=self.matcher(['青い空を見上げながら歩こう'])
        self.assertIsNone(matcher.select([word('青い空を見上げ',start=0)],0,8))
        result=matcher.select([word('青い空を見上げ',start=0)],1,8)
        self.assertIsNone(result['onset'])

    def test_recent_partial_next_line_beats_complete_earlier_line(self):
        matcher=self.matcher(['青い空を見上げながら歩こう','風が吹いた帰り道'])
        self.confirm(matcher,'青い空を見上げ')
        result=matcher.select([word('青い空を見上げながら歩こう',start=.3,end=4.5),
                               word('風が吹いた',start=5.4,end=7.7)],2,8)
        self.assertEqual(result['index'],1)
        self.assertAlmostEqual(result['onset'],7.4)

    def test_small_recognition_error_can_still_match_a_long_phrase(self):
        matcher=self.matcher(['星屑の夜空を見上げながら歩こう'])
        result=self.confirm(matcher,'星屑の夜空を見あげながら')
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result['similarity'],.86)

    def test_music_and_low_probability_hallucinations_never_move_by_time(self):
        matcher=self.matcher(['青い空を見上げながら歩こう','風が吹いた帰り道'])
        for left in (0,1,20,70):
            self.assertIsNone(matcher.select([word('青い空を見上げ',probability=.1)],left,8))
        self.assertIsNone(matcher.index)
        self.assertIsNone(matcher.select([word('ご視聴ありがとうございました')],80,8))

    def test_old_words_and_collapsed_or_nonfinite_timestamps_are_rejected(self):
        for value in (word('青い空を見上げ',start=0,end=1),
                      word('青い空を見上げ',start=7,end=7.02),
                      word('青い空を見上げ',start=-1),
                      word('青い空を見上げ',end=9),
                      word('青い空を見上げ',probability=float('nan'))):
            with self.subTest(value=value):
                matcher=self.matcher(['青い空を見上げながら歩こう'])
                for left in (0,1):self.assertIsNone(matcher.select([value],left,8))
                self.assertIsNone(matcher.index)

    def test_short_common_words_and_vocalise_are_not_anchors(self):
        for phrase in ('君と','そして','ララララララ','ああああああ'):
            with self.subTest(phrase=phrase):
                matcher=self.matcher([phrase+'青い空を見上げよう'])
                self.assertIsNone(matcher.select([word(phrase)],0,8))
                self.assertIsNone(matcher.select([word(phrase)],1,8))
                self.assertIsNone(matcher.index)

    def test_same_refrain_cannot_acquire_a_chorus_occurrence(self):
        matcher=self.matcher(['青い空を見上げながら','星の光が遠く揺れる','青い空を見上げながら'])
        for left in (0,1,2):self.assertIsNone(matcher.select([word('青い空を見上げ')],left,8))
        self.assertIsNone(matcher.index)

    def test_distant_same_refrain_does_not_defeat_adjacent_continuity(self):
        matcher=self.matcher(['始まりの音を探して','青い空を見上げながら',
                              '星の光が遠く揺れる','青い空を見上げながら'])
        self.confirm(matcher,'始まりの音を探して')
        result=matcher.select([word('青い空を見上げ')],2,8)
        self.assertEqual(result['index'],1)
        self.assertFalse(result['unique'])

    def test_repeated_phrase_with_no_continuity_cannot_recover(self):
        matcher=self.matcher(['始まりの音を探して','次の歌は別の言葉',
                              '青い空を見上げながら','青い空を見上げながら'])
        self.confirm(matcher,'始まりの音を探して')
        for left in (2,3):self.assertIsNone(matcher.select([word('青い空を見上げ')],left,8))
        self.assertEqual(matcher.index,0)

    def test_unique_forward_recovery_requires_two_windows_and_stays_bounded(self):
        lines=['始まりの音を探して']+['別の言葉です']*3+['星屑の夜空を見上げて']
        lines+=['違う景色ばかり']*10+['遠くで揺れる新しい夢']
        matcher=self.matcher(lines)
        self.confirm(matcher,lines[0])
        self.assertIsNone(matcher.select([word(lines[4])],2,8))
        result=matcher.select([word(lines[4])],3,8)
        self.assertEqual(result['index'],4)
        self.assertTrue(result['recovery'])
        self.assertEqual(result['confirmations'],2)
        for left in (4,5):self.assertIsNone(matcher.select([word(lines[15])],left,8))
        self.assertEqual(matcher.index,4)

    def test_confirmed_position_never_moves_backwards(self):
        matcher=self.matcher(['青い空を見上げながら','星の光が遠く揺れる'])
        self.confirm(matcher,'青い空を見上げ')
        self.assertEqual(matcher.select([word('星の光が遠く揺れる')],2,8)['index'],1)
        for left in (3,4):self.assertIsNone(matcher.select([word('青い空を見上げ')],left,8))
        self.assertEqual(matcher.index,1)

    def test_mid_song_estimate_and_current_index_are_only_bounded_search_hints(self):
        lines=['別の言葉です']*30;lines[18]='星屑の夜空を見上げて';lines[1]='遠くで揺れる新しい夢'
        matcher=self.matcher(lines,start_index=18)
        for left in (100,101):
            self.assertIsNone(matcher.select([word(lines[1])],left,8,current_index=1))
        self.assertIsNone(matcher.index)
        self.assertEqual(self.confirm(matcher,lines[18],left=102)['index'],18)

    def test_prediction_cannot_widen_initial_search_beyond_the_seed_bounds(self):
        lines=['別の言葉です']*20;lines[11]='星屑の夜空を見上げて'
        matcher=self.matcher(lines)
        for left in (0,1):
            self.assertIsNone(matcher.select([word(lines[11])],left,8,current_index=11))
        self.assertIsNone(matcher.index)

    def test_pending_evidence_expires_across_a_nonoverlapping_window(self):
        matcher=self.matcher(['青い空を見上げながら'])
        self.assertIsNone(matcher.select([word('青い空を見上げ')],0,8))
        self.assertIsNone(matcher.select([word('青い空を見上げ')],20,8))
        self.assertIsNone(matcher.index)

    def test_progressing_unique_phrases_can_jointly_confirm_acquisition(self):
        matcher=self.matcher(['青い空を見上げながら','星の光が遠く揺れる'])
        self.assertIsNone(matcher.select([word('青い空を見上げ',start=6,end=7.2)],0,8))
        result=matcher.select([word('星の光が遠く揺れる',start=6.4)],1,8)
        self.assertEqual(result['index'],1)
        self.assertEqual(result['confirmations'],2)

    def test_same_phrase_at_inconsistent_audio_times_does_not_confirm(self):
        matcher=self.matcher(['青い空を見上げながら'])
        self.assertIsNone(matcher.select([word('青い空を見上げ',start=4,end=6)],0,8))
        self.assertIsNone(matcher.select([word('青い空を見上げ',start=6)],1,8))
        self.assertIsNone(matcher.index)
        # Re-anchoring needs another observation agreeing with the newer time.
        result=matcher.select([word('青い空を見上げ',start=5,end=6.6)],2,8)
        self.assertEqual(result['index'],0)
        self.assertEqual(result['confirmations'],2)

    def test_adjacent_phrase_cannot_confirm_over_the_same_earlier_audio(self):
        matcher=self.matcher(['青い空を見上げながら','星の光が遠く揺れる'])
        self.assertIsNone(matcher.select([word('青い空を見上げ',start=6,end=7.8)],0,8))
        self.assertIsNone(matcher.select([word('星の光が遠く揺れる',start=4,end=6.5)],1,8))
        self.assertIsNone(matcher.index)

    def test_uncertain_word_does_not_join_fragments_into_false_evidence(self):
        matcher=self.matcher(['青い空を見上げながら'])
        words=[word('青い空',start=3,end=4),word('全然別の言葉',start=4,end=6,probability=.05),
               word('を見上げ',start=6,end=7.6)]
        for left in (0,1):self.assertIsNone(matcher.select(words,left,8))
        self.assertIsNone(matcher.index)

    def test_good_recent_phrase_survives_a_low_confidence_hallucinated_suffix(self):
        matcher=self.matcher(['青い空を見上げながら歩こう','星の光が遠く揺れる'])
        for left in (0,1):
            words=[word('青い空を見上げながら',start=1.26-left,end=7.28-left),
                   word('おめでとう',start=7.28-left,end=7.6-left,probability=.06),
                   word('ございます',start=7.6-left,end=7.98-left,probability=.23)]
            result=matcher.select(words,left,8)
            if not left:self.assertIsNone(result)
        self.assertEqual(result['index'],0)
        self.assertAlmostEqual(result['evidence_end'],7.28)
        self.assertEqual(result['confirmations'],2)

    def test_a_short_confident_suffix_does_not_erase_a_good_separate_run(self):
        matcher=self.matcher(['青い空を見上げながら歩こう'])
        for left in (0,1):
            words=[word('青い空を見上げながら',start=2-left,end=7-left),
                   word('不明',start=7-left,end=7.4-left,probability=.1),
                   word('はい',start=7.4-left,end=7.98-left)]
            result=matcher.select(words,left,8)
        self.assertEqual(result['index'],0)

    def test_recent_short_runs_cannot_combine_across_an_uncertain_middle(self):
        matcher=self.matcher(['青い空を見上げながら'])
        for left in (0,1):
            words=[word('青い空',start=4,end=6),
                   word('聞こえない言葉',start=6,end=6.6,probability=.05),
                   word('を見上げ',start=6.6,end=7.8)]
            self.assertIsNone(matcher.select(words,left,8))
        self.assertIsNone(matcher.index)

    def test_old_good_run_remains_stale_even_when_followed_by_noisy_words(self):
        matcher=self.matcher(['青い空を見上げながら'])
        words=[word('青い空を見上げ',start=.3,end=4),
               word('おめでとうございます',start=6,end=7.8,probability=.05)]
        for left in (0,1):self.assertIsNone(matcher.select(words,left,8))
        self.assertIsNone(matcher.index)

    def recovery_lines(self):
        lines=['まだ聞いていない歌詞']*35
        lines[0]='始まりの音を探して'
        lines[22]='星屑の夜空を見上げて'
        lines[23]='遠くで揺れる新しい夢'
        return lines

    def test_two_distinct_ordered_phrases_recover_beyond_ten_rows(self):
        lines=self.recovery_lines();matcher=self.matcher(lines)
        self.confirm(matcher,lines[0])
        self.assertIsNone(matcher.select([word(lines[22],start=6,end=7.2)],40,8))
        self.assertEqual(matcher.index,0)
        result=matcher.select([word(lines[23],start=6.4,end=7.8)],41,8)
        self.assertEqual(result['index'],23)
        self.assertTrue(result['global_recovery'])
        self.assertEqual(result['distinct_lines'],2)
        self.assertEqual(result['recovery_start_index'],22)
        self.assertEqual(result['confirmations'],2)
        self.assertGreaterEqual(result['confidence'],.8)
        self.assertGreaterEqual(result['characters'],8)

    def test_late_initial_acquisition_is_not_permanently_trapped_near_seed(self):
        lines=self.recovery_lines();matcher=self.matcher(lines)
        for left in (0,15,30):self.assertIsNone(matcher.select([],left,8))
        self.assertIsNone(matcher.select([word(lines[22],start=6,end=7.2)],60,8))
        result=matcher.select([word(lines[23],start=6.4,end=7.8)],61,8)
        self.assertEqual(result['index'],23)
        self.assertTrue(result['global_recovery'])

    def test_one_distant_phrase_repeated_across_windows_cannot_move_position(self):
        lines=self.recovery_lines();matcher=self.matcher(lines)
        self.confirm(matcher,lines[0])
        for left in (40,41,42,43):
            self.assertIsNone(matcher.select([word(lines[22])],left,8))
        self.assertEqual(matcher.index,0)

    def test_two_distant_phrases_over_the_same_audio_cannot_confirm_recovery(self):
        lines=self.recovery_lines();matcher=self.matcher(lines)
        self.confirm(matcher,lines[0])
        self.assertIsNone(matcher.select([word(lines[22],start=6,end=7.8)],40,8))
        self.assertIsNone(matcher.select([word(lines[23],start=4,end=6.8)],41,8))
        self.assertEqual(matcher.index,0)

    def test_distant_nonadjacent_recognized_rows_cannot_confirm_a_random_jump(self):
        lines=self.recovery_lines();lines[29]='輝く朝日に手を伸ばして'
        matcher=self.matcher(lines);self.confirm(matcher,lines[0])
        self.assertIsNone(matcher.select([word(lines[22],start=6,end=7.2)],40,8))
        self.assertIsNone(matcher.select([word(lines[29],start=6.4,end=7.8)],41,8))
        self.assertEqual(matcher.index,0)

    def test_a_repeated_chorus_cannot_anchor_distant_recovery(self):
        lines=self.recovery_lines();lines[32]=lines[22]
        matcher=self.matcher(lines);self.confirm(matcher,lines[0])
        self.assertIsNone(matcher.select([word(lines[22],start=6,end=7.2)],40,8))
        self.assertIsNone(matcher.select([word(lines[23],start=6.4,end=7.8)],41,8))
        self.assertEqual(matcher.index,0)

    def test_almost_identical_chorus_competitor_blocks_distant_recovery(self):
        lines=self.recovery_lines();lines[32]='星屑の夜空を見あげて'
        matcher=self.matcher(lines);self.confirm(matcher,lines[0])
        self.assertIsNone(matcher.select([word(lines[22],start=6,end=7.2,probability=.85)],40,8))
        self.assertIsNone(matcher.select([word(lines[23],start=6.4,end=7.8)],41,8))
        self.assertEqual(matcher.index,0)

    def test_distant_recovery_requires_both_rows_to_have_strong_evidence(self):
        lines=self.recovery_lines();matcher=self.matcher(lines)
        self.confirm(matcher,lines[0])
        self.assertIsNone(matcher.select([word(lines[22],start=6,end=7.2,probability=.79)],40,8))
        self.assertIsNone(matcher.select([word(lines[23],start=6.4,end=7.8)],41,8))
        self.assertEqual(matcher.index,0)

    def test_distant_recovery_never_reverses_a_confirmed_position(self):
        lines=self.recovery_lines();matcher=self.matcher(lines,start_index=22)
        self.assertEqual(self.confirm(matcher,lines[22],left=40)['index'],22)
        for left in (42,43):
            self.assertIsNone(matcher.select([word(lines[0])],left,8))
        self.assertEqual(matcher.index,22)

    def test_two_short_but_distinct_strong_rows_can_jointly_recover(self):
        for phrases in (('星屑と夢の青空','明日へ進もう'),('明日へ進もう','星屑と夢の青空')):
            with self.subTest(phrases=phrases):
                lines=self.recovery_lines();lines[22],lines[23]=phrases
                matcher=self.matcher(lines);self.confirm(matcher,lines[0])
                self.assertIsNone(matcher.select([word(lines[22],start=6,end=7.2)],40,8))
                result=matcher.select([word(lines[23],start=6.4,end=7.8)],41,8)
                self.assertEqual(result['index'],23)
                self.assertTrue(result['global_recovery'])
                self.assertEqual(result['characters'],6)
                self.assertEqual(result['recovery_characters'],13)

    def test_two_very_short_rows_do_not_supply_enough_total_recovery_evidence(self):
        lines=self.recovery_lines();lines[22]='明日へ進もう';lines[23]='輝く夜の星へ'
        matcher=self.matcher(lines);self.confirm(matcher,lines[0])
        self.assertIsNone(matcher.select([word(lines[22],start=6,end=7.2)],40,8))
        self.assertIsNone(matcher.select([word(lines[23],start=6.4,end=7.8)],41,8))
        self.assertEqual(matcher.index,0)

    def anchored_lines(self):
        return ['始まりの音を探して','知らない言葉の行','星屑の夜空を見上げて',
                '夜空の夢へ','違う景色ばかり','夜空の夢へ']

    def test_unique_preceding_row_locates_the_adjacent_repeated_phrase(self):
        lines=self.anchored_lines();matcher=self.matcher(lines)
        self.confirm(matcher,lines[0])
        self.assertIsNone(matcher.select([word(lines[2],start=6,end=7.2)],40,8))
        result=matcher.select([word(lines[3],start=6.4,end=7.8)],41,8)
        self.assertEqual(result['index'],3)
        self.assertEqual(result['confirmations'],2)
        self.assertFalse(result['unique'])
        self.assertTrue(result['sequence_confirmed'])

    def test_repeated_phrase_without_unique_predecessor_does_not_skip_rows(self):
        lines=self.anchored_lines();lines[4]=lines[2]
        matcher=self.matcher(lines);self.confirm(matcher,lines[0])
        self.assertIsNone(matcher.select([word(lines[2],start=6,end=7.2)],40,8))
        self.assertIsNone(matcher.select([word(lines[3],start=6.4,end=7.8)],41,8))
        self.assertEqual(matcher.index,0)

    def test_repeated_successor_must_be_heard_after_the_unique_predecessor(self):
        lines=self.anchored_lines();matcher=self.matcher(lines)
        self.confirm(matcher,lines[0])
        self.assertIsNone(matcher.select([word(lines[2],start=6,end=7.2)],40,8))
        self.assertIsNone(matcher.select([word(lines[3],start=4,end=6.8)],41,8))
        self.assertEqual(matcher.index,0)

    def test_weak_unique_predecessor_cannot_disambiguate_repeated_successor(self):
        lines=self.anchored_lines();matcher=self.matcher(lines)
        self.confirm(matcher,lines[0])
        self.assertIsNone(matcher.select([word(lines[2],start=6,end=7.2,probability=.65)],40,8))
        self.assertIsNone(matcher.select([word(lines[3],start=6.4,end=7.8)],41,8))
        self.assertEqual(matcher.index,0)

    def test_two_fresh_short_acoustic_matches_can_bridge_one_unheard_row(self):
        lines=['始まりの音を探して','未知の言葉','星の道へ','別の行','星の道へ']
        matcher=self.matcher(lines);self.confirm(matcher,lines[0])
        self.assertIsNone(matcher.select([word(lines[2],start=6,end=7.7,probability=.95)],2,8))
        result=matcher.select([word(lines[2],start=5,end=6.7,probability=.95)],3,8)
        self.assertEqual(result['index'],2)
        self.assertEqual(result['confirmations'],2)
        self.assertFalse(result['unique'])
        self.assertAlmostEqual(matcher.confirmed_end,9.7)

    def test_old_confirmed_context_does_not_locate_a_short_repeated_phrase(self):
        lines=['始まりの音を探して','未知の言葉','星の道へ','別の行','星の道へ']
        matcher=self.matcher(lines);self.confirm(matcher,lines[0])
        for left in (40,41):self.assertIsNone(matcher.select([word(lines[2],probability=.95)],left,8))
        self.assertEqual(matcher.index,0)

    def test_multiple_reachable_repeats_are_not_resolved_by_nearness_alone(self):
        lines=['始まりの音を探して','未知の言葉','星の道へ','星の道へ']
        matcher=self.matcher(lines);self.confirm(matcher,lines[0])
        for left in (2,3,4):self.assertIsNone(matcher.select([word(lines[2],probability=.95)],left,8))
        self.assertEqual(matcher.index,0)

    def test_one_weak_window_does_not_erase_a_recent_short_phrase_anchor(self):
        lines=['始まりの音を探して','未知の言葉','星の道へ','別の行','星の道へ']
        matcher=self.matcher(lines);self.confirm(matcher,lines[0])
        self.assertIsNone(matcher.select([word(lines[2],start=6,end=7.7,probability=.95)],2,8))
        self.assertIsNone(matcher.select([word('曖昧な音',probability=.1)],3,8))
        result=matcher.select([word(lines[2],start=4,end=5.9,probability=.95)],4,8)
        self.assertEqual(result['index'],2)
        self.assertEqual(result['confirmations'],2)

    def test_recent_short_confirmation_still_needs_consistent_acoustic_time(self):
        lines=['始まりの音を探して','未知の言葉','星の道へ']
        matcher=self.matcher(lines);self.confirm(matcher,lines[0])
        self.assertIsNone(matcher.select([word(lines[2],start=4,end=5.9,probability=.95)],2,8))
        self.assertIsNone(matcher.select([word(lines[2],start=6,end=7.8,probability=.95)],3,8))
        self.assertEqual(matcher.index,0)

    def test_retained_acoustic_anchor_expires_instead_of_confirming_after_a_gap(self):
        lines=['始まりの音を探して','未知の言葉','星の道へ']
        matcher=self.matcher(lines);self.confirm(matcher,lines[0])
        self.assertIsNone(matcher.select([word(lines[2],start=6,end=7.7,probability=.95)],2,8))
        self.assertIsNone(matcher.select([],7,8))
        self.assertIsNone(matcher.pending)
        self.assertIsNone(matcher.select([word(lines[2],start=6,end=7.7,probability=.95)],8,8))
        self.assertEqual(matcher.index,0)


if __name__=='__main__':unittest.main()
