from dataclasses import replace
import tempfile
import unittest
import json
from unittest.mock import patch

from lyric_annotations import (LyricAnnotations,match_annotations,match_parenthetical_annotations,
                               match_vocalise_annotations,match_sequence_annotations,
                               match_verified_annotations,merge_verified_annotations)
from lyrics import Line,Song
from storage import Store


class AnnotationFileValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(self.temp.name);self.cache=LyricAnnotations(self.store)
        self.song=Song('shapes','Title','Artist','Album',
                       (Line('First','original reading','original meaning'),Line('Second','','')))
        self.cache.directory.mkdir()
        self.path=self.cache.directory/'shapes.json'

    def test_corrupt_optional_annotations_keep_original_display_and_cannot_be_overwritten(self):
        entry={'original':'Second','reading':'second reading','korean':'second meaning'}
        valid={'fingerprint':self.song.fingerprint,'lines':[None,entry]}
        invalid=[None,[],[1],'invalid',True,{'fingerprint':self.song.fingerprint}]
        invalid.extend({**valid,'lines':value} for value in
                       (None,{},'ab',[],[entry],[None,1],[None,{}],
                        [None,{**entry,'reading':None}],[None,{**entry,'korean':[]}],
                        [None,{**entry,'original':123}],[None,{**entry,'unexpected':'field'}]))
        for value in invalid:
            with self.subTest(value=value):
                self.path.write_text(json.dumps(value),encoding='utf-8');before=self.path.read_bytes()
                with self.assertRaises(ValueError):self.cache.load(self.song)
                self.assertEqual(self.cache.display_lines(self.song),self.song.lines)
                with self.assertRaises(ValueError):self.cache.save(self.song,self.song.lines,{})
                self.assertEqual(self.path.read_bytes(),before)
                self.path.write_text(json.dumps(valid),encoding='utf-8')
                self.assertEqual(self.cache.display_lines(self.song),
                                 (self.song.lines[0],Line(**entry)))

    def test_unreadable_optional_file_keeps_display_but_blocks_replacement(self):
        with patch('lyric_annotations.read_json',side_effect=OSError('temporary read failure')):
            self.assertEqual(self.cache.display_lines(self.song),self.song.lines)
            with self.assertRaises(OSError):self.cache.save(self.song,self.song.lines,{})
        self.assertFalse(self.path.exists())

    def test_changed_lyric_fingerprint_ignores_incompatible_annotation_rows(self):
        self.path.write_text(json.dumps({'fingerprint':'another document','lines':None}),encoding='utf-8')
        before=self.path.read_bytes()
        self.assertEqual(self.cache.display_lines(self.song),self.song.lines)
        self.assertEqual(self.path.read_bytes(),before)


class AnnotationTests(unittest.TestCase):
    def test_common_matcher_projects_sentences_without_copying_fragment_readings(self):
        originals,supplied=self.parenthetical_document()
        first=originals[0].original
        target=(Line(first[:10],'',''),Line(first[10:],'',''),*originals[1:])
        matched=match_annotations(target,supplied)
        self.assertEqual([line.korean for line in matched],['번역 0','번역 0','번역 1','번역 2','번역 3','번역 4'])
        self.assertEqual([line.reading for line in matched],['','','독음 1','독음 2','독음 3','독음 4'])

    def test_common_matcher_uses_sequence_for_repeated_phrases_with_distinct_meanings(self):
        originals,supplied=self.parenthetical_document()
        text=originals[0].original
        source=(*supplied,replace(supplied[0],korean='마지막 문장 번역'))
        target=(*originals,Line(text[:10],'',''),Line(text[10:],'',''))
        matched=match_annotations(target,source)
        self.assertEqual(matched[0].korean,'번역 0')
        self.assertEqual([line.korean for line in matched[-2:]],['마지막 문장 번역']*2)

    def test_persisted_split_annotations_preserve_originals_readings_and_manual_times(self):
        originals,supplied=self.parenthetical_document()
        text=originals[0].original
        display=(Line(text[:10],'직접 독음','직접 번역'),Line(text[10:],'',''),*originals[1:])
        song=Song('sentence-test','Synthetic','Artist','Album',display)
        with tempfile.TemporaryDirectory() as folder:
            store=Store(folder);store.save_library([song]);store.save_timing(song,{'times':[i*10 for i in range(6)],'offset':.5})
            paths=(store.directory/'library.json',store.directory/'timings'/'sentence-test.json')
            before=[path.read_bytes() for path in paths]
            LyricAnnotations(store).save(song,supplied,{'source':'synthetic'})
            restored=LyricAnnotations(Store(folder)).display_lines(song)
            self.assertEqual([line.original for line in restored],[line.original for line in display])
            self.assertEqual(restored[0],display[0])
            self.assertEqual(restored[1].korean,'번역 0')
            self.assertEqual(restored[1].reading,'')
            self.assertEqual(before,[path.read_bytes() for path in paths])

    def test_sentence_annotations_cover_only_exact_consecutive_full_rows(self):
        originals,supplied=self.parenthetical_document()
        first=originals[0].original
        target=(Line(first[:10],'',''),Line(first[10:20],'',''),Line(first[20:],'',''),*originals[1:])
        self.assertEqual(match_sequence_annotations(target,supplied),[supplied[0]]*3+list(supplied[1:]))

    def test_merged_sentence_annotations_join_complete_meanings_without_readings(self):
        originals,supplied=self.parenthetical_document()
        target=(Line(originals[0].original+originals[1].original,'',''),*originals[2:])
        matched=match_sequence_annotations(target,supplied)
        self.assertEqual(matched[0].korean,'번역 0 / 번역 1')
        self.assertEqual(matched[0].reading,'')
        self.assertEqual(matched[1:],list(supplied[2:]))

    def test_live_split_and_merge_round_trips_do_not_multiply_sentence_meaning(self):
        from live_lyrics import LiveLyrics
        from media import Playback
        originals,supplied=self.parenthetical_document()
        text=originals[0].original
        split=(Line(text[:10],'',''),Line(text[10:],'',''),*originals[1:])
        playback=Playback(title='Synthetic',artist='Example',album='Example')
        live=LiveLyrics();live.receive(playback,None,supplied)
        for turn in range(4):
            with self.subTest(turn=turn):
                live.receive(playback,None,split)
                self.assertEqual([line.korean for line in live.lines[:2]],['번역 0']*2)
                self.assertTrue(all(not line.reading for line in live.lines[:2]))
                live.receive(playback,None,originals)
                self.assertEqual([line.korean for line in live.lines],[line.korean for line in supplied])
                self.assertEqual([line.original for line in live.lines],[line.original for line in originals])

    def test_joined_meanings_keep_distinct_order_and_nonadjacent_repetitions(self):
        originals,supplied=self.parenthetical_document()
        target=(Line(''.join(line.original for line in originals[:3]),'',''),*originals[3:])
        different=(replace(supplied[0],korean='첫 번째 뜻'),
                   replace(supplied[1],korean='다른 뜻'),
                   replace(supplied[2],korean='첫 번째 뜻'),*supplied[3:])
        matched=match_sequence_annotations(target,different)
        self.assertEqual(matched[0].korean,'첫 번째 뜻 / 다른 뜻 / 첫 번째 뜻')
        adjacent=(different[0],replace(different[1],korean='첫 번째 뜻'),
                  replace(different[2],korean='마지막 뜻'),*different[3:])
        self.assertEqual(match_sequence_annotations(target,adjacent)[0].korean,'첫 번째 뜻 / 마지막 뜻')

    def test_sentence_annotations_reject_changed_fragments(self):
        originals,supplied=self.parenthetical_document()
        text=originals[2].original
        target=(*originals[:2],Line(text[:10],'',''),Line(text[10:],'',''),*originals[3:])
        changed=(*supplied[:2],replace(supplied[2],original=text.replace('少年','少女')),*supplied[3:])
        self.assertEqual(match_sequence_annotations(target,changed),[*supplied[:2],None,None,*supplied[3:]])

    def test_shifted_boundaries_share_only_the_smallest_complete_translation_block(self):
        originals,supplied=self.parenthetical_document()
        text=originals[0].original+originals[1].original
        target=(Line(text[:10],'',''),Line(text[10:],'',''),*originals[2:])
        matched=match_sequence_annotations(target,supplied)
        self.assertEqual([line.korean for line in matched[:2]],['번역 0 / 번역 1']*2)
        self.assertTrue(all(not line.reading for line in matched[:2]))
        self.assertEqual(matched[2:],list(supplied[2:]))

    def test_merged_block_requires_every_source_sentence_translation(self):
        originals,supplied=self.parenthetical_document()
        target=(Line(originals[0].original+originals[1].original,'',''),*originals[2:])
        supplied=(supplied[0],replace(supplied[1],korean=''),*supplied[2:])
        self.assertEqual(match_sequence_annotations(target,supplied),[None,*supplied[2:]])

    def test_regrouping_never_shares_a_paragraph_or_more_than_four_rows(self):
        originals,supplied=self.parenthetical_document()
        # A long sentence may be valid as one row, but must not be spread over
        # many playback rows or combined into a paragraph-sized translation.
        long=replace(supplied[0],original=supplied[0].original*6)
        source=(long,*supplied[1:])
        self.assertEqual(match_sequence_annotations((long,*originals[1:]),source),list(source))
        split=(Line(long.original[:90],'',''),Line(long.original[90:],'',''),*originals[1:])
        self.assertEqual(match_sequence_annotations(split,source),[None,None,*supplied[1:]])
        text=originals[0].original
        split=tuple(Line(text[i:i+6],'','') for i in range(0,len(text),6))+originals[1:]
        self.assertEqual(match_sequence_annotations(split,supplied),[None]*len(split[:-4])+list(supplied[1:]))
        merged=(Line(''.join(line.original for line in originals),'',''),)
        self.assertIsNone(match_sequence_annotations(merged,supplied))

    def test_repeated_split_sentences_use_their_verified_sequence_positions(self):
        originals,supplied=self.parenthetical_document()
        text=originals[0].original
        source=(*supplied,replace(supplied[0],korean='마지막 반복의 문장 번역'))
        split=(Line(text[:10],'',''),Line(text[10:],'',''))
        target=(*split,*originals[1:],*split)
        self.assertEqual(match_sequence_annotations(target,source),
                         [supplied[0],supplied[0],*supplied[1:],source[-1],source[-1]])

    def test_sentence_annotations_reject_omitted_or_reordered_verses(self):
        originals,supplied=self.parenthetical_document()
        for target in (originals[:-1],(originals[1],originals[0],*originals[2:])):
            self.assertIsNone(match_sequence_annotations(target,supplied))

    def parenthetical_document(self):
        texts=('朝の窓から青い庭を眺めながら今日の行き先をゆっくり考えた',
               '川の向こうに見える広い公園では小鳥たちが静かに遊んでいた',
               '昼の坂道を歩く少年は遠い町から届いた手紙を大切に持っていた',
               '夕方の電車に乗った少女は初めて訪れる海辺の景色を楽しみにした',
               '夜の広場に集まった友人たちは明日もまた会おうと笑って別れた')
        originals=tuple(Line(text,'','') for text in texts)
        supplied=tuple(Line(text,f'독음 {index}',f'번역 {index}') for index,text in enumerate(texts))
        return originals,supplied

    def test_optional_parenthetical_backing_vocal_keeps_exact_rows_in_sequence(self):
        originals,supplied=self.parenthetical_document()
        incoming=(*supplied[:2],Line('（小さな声で（ハイ））','보컬 독음','보컬 번역'),*supplied[2:])
        self.assertEqual(match_parenthetical_annotations(originals,incoming),list(supplied))

    def vocalise_document(self,syllable='la'):
        originals,supplied=self.parenthetical_document()
        originals=(*originals[:2],Line(syllable*6,'',''),*originals[2:])
        incoming=(*supplied[:2],Line(syllable*8,'',''),Line(syllable*8,'',''),*supplied[2:])
        expected=[*supplied[:2],None,*supplied[2:]]
        return originals,incoming,expected

    def test_repeated_vocalise_counts_and_row_breaks_only_fill_exact_main_rows(self):
        for syllable in ('la','oh','ラ','ら','あ'):
            with self.subTest(syllable=syllable):
                originals,supplied,expected=self.vocalise_document(syllable)
                self.assertEqual(match_vocalise_annotations(originals,supplied),expected)

    def test_vocalise_variation_keeps_main_typo_budget_and_skips_changed_translation(self):
        originals,supplied,expected=self.vocalise_document()
        supplied=(*supplied[:4],replace(supplied[4],original=supplied[4].original.replace('少年','少女')),*supplied[5:])
        expected[3]=None
        self.assertEqual(match_vocalise_annotations(originals,supplied),expected)

    def test_vocalise_variation_keeps_complete_sentence_translation_across_split_rows(self):
        originals,supplied,expected=self.vocalise_document()
        text=originals[0].original
        originals=(Line(text[:10],'',''),Line(text[10:],'',''),*originals[1:])
        self.assertEqual(match_vocalise_annotations(originals,supplied),[supplied[0],supplied[0],*expected[1:]])

    def test_common_matcher_keeps_split_translation_across_vocalise_count_changes(self):
        originals,supplied,expected=self.vocalise_document()
        text=originals[0].original
        target=(Line(text[:10],'',''),Line(text[10:],'',''),*originals[1:])
        matched=match_annotations(target,supplied)
        self.assertEqual([line.korean if line else None for line in matched],
                         [supplied[0].korean,supplied[0].korean,
                          *[line.korean if line else None for line in expected[1:]]])
        self.assertEqual([line.reading for line in matched[:2]],['',''])

    def test_different_chants_and_meaningful_words_are_not_collapsed_together(self):
        originals,supplied,unused=self.vocalise_document()
        for text in ('rararara','ohohohoh','laralarala','lalalove','七つの星','ララララ','lalalala明日'):
            different=(*originals[:2],Line(text,'',''),*originals[3:])
            with self.subTest(text=text):
                self.assertIsNone(match_vocalise_annotations(different,supplied))

    def test_missing_chant_or_missing_reordered_main_verse_is_rejected(self):
        originals,supplied,unused=self.vocalise_document()
        cases=((*originals[:2],*originals[3:]),
               (*originals[:3],*originals[4:]),
               (originals[1],originals[0],*originals[2:]),
               (*originals[:3],Line('X','',''),*originals[3:]))
        for rows in cases:
            self.assertIsNone(match_vocalise_annotations(rows,supplied))

    def test_excessive_vocalise_content_cannot_verify_short_or_different_songs(self):
        originals,supplied,unused=self.vocalise_document()
        huge=(*supplied[:2],Line('la'*200,'',''),*supplied[4:])
        self.assertIsNone(match_vocalise_annotations(originals,huge))
        self.assertIsNone(match_vocalise_annotations(originals[:3],supplied[:4]))

    def test_inline_parenthetical_difference_never_supplies_partial_translation(self):
        originals,supplied=self.parenthetical_document()
        incoming=(replace(supplied[0],original=supplied[0].original+'（小さな声で）'),*supplied[1:])
        self.assertEqual(match_parenthetical_annotations(originals,incoming),[None,*supplied[1:]])

    def test_paired_inline_asides_keep_explicit_main_translation_and_no_aside(self):
        originals,supplied=self.parenthetical_document()
        incoming=(replace(supplied[0],original=supplied[0].original+'（小さな声で（ハイ））',
                          reading=supplied[0].reading+' (백보컬 (하이))',
                          korean=supplied[0].korean+' (작은 목소리로 (응))'),*supplied[1:])
        self.assertEqual(match_parenthetical_annotations(originals,incoming),list(supplied))
        text=originals[0].original
        target=(Line(text[:10],'',''),Line(text[10:],'',''),*originals[1:])
        self.assertEqual(match_parenthetical_annotations(target,incoming),[supplied[0],supplied[0],*supplied[1:]])

    def test_small_paired_aside_fills_a_gap_after_partial_sequence_match(self):
        originals,supplied=self.parenthetical_document()
        text=originals[0].original
        incoming=(replace(supplied[0],original=text[:10]+'(あ)'+text[10:],
                          reading=supplied[0].reading+' (아)',
                          korean=supplied[0].korean+' (아)'),*supplied[1:])
        self.assertEqual(match_sequence_annotations(originals,incoming),[None,*supplied[1:]])
        self.assertEqual(match_annotations(originals,incoming),list(supplied))
        merged,contributors=merge_verified_annotations(originals,[incoming])
        self.assertEqual(merged,supplied)
        self.assertEqual(contributors,(0,))

    def test_common_parenthetical_match_keeps_split_meaning_and_fragment_readings(self):
        originals,supplied=self.parenthetical_document()
        text=originals[0].original
        target=(Line(text[:10],'',''),Line(text[10:],'',''),*originals[1:])
        incoming=(replace(supplied[0],original=text+'(小さな声で)',
                          reading=supplied[0].reading+' (작게)',
                          korean=supplied[0].korean+' (작게)'),*supplied[1:])
        matched=match_annotations(target,incoming)
        self.assertEqual([line.korean for line in matched[:2]],['번역 0']*2)
        self.assertEqual([line.reading for line in matched[:2]],['',''])
        self.assertEqual([line.original for line in matched],
                         [line.original for line in target])

    def test_small_unpaired_aside_stays_blank_and_conflicting_sources_do_not_win(self):
        originals,supplied=self.parenthetical_document()
        text=originals[0].original
        unpaired=(replace(supplied[0],original=text[:10]+'(あ)'+text[10:]),*supplied[1:])
        self.assertIsNone(match_annotations(originals,unpaired)[0])
        paired=(replace(unpaired[0],korean='번역 0 (아)'),*unpaired[1:])
        competing=(replace(supplied[0],korean='다른 번역'),*supplied[1:])
        merged,contributors=merge_verified_annotations(originals,[paired,competing])
        self.assertEqual(merged[0].korean,'')
        self.assertEqual([line.korean for line in merged[1:]],
                         [line.korean for line in supplied[1:]])

    def test_verified_strategy_conflict_cannot_fall_back_to_a_repeated_exact_phrase(self):
        originals=(Line('歌おう','',''),Line('明日へ','',''))
        supplied=(Line('歌おう','','노래하자'),Line('追加','','추가'))
        sequence=[supplied[0],None]
        parenthetical=[Line('歌おう','','함께 노래하자'),None]
        with patch('lyric_annotations.match_sequence_annotations',return_value=sequence), \
             patch('lyric_annotations.match_parenthetical_annotations',return_value=parenthetical), \
             patch('lyric_annotations.match_vocalise_annotations',return_value=None):
            self.assertEqual(match_verified_annotations(originals,supplied),[None,None])
            self.assertEqual(match_annotations(originals,supplied),[None,None])

    def test_complete_sequence_match_skips_optional_matching_paths(self):
        originals,supplied=self.parenthetical_document()
        text=originals[0].original
        target=(Line(text[:10],'',''),Line(text[10:],'',''),*originals[1:])
        with patch('lyric_annotations.match_parenthetical_annotations') as parenthetical, \
             patch('lyric_annotations.match_vocalise_annotations') as vocalise:
            matched=match_verified_annotations(target,supplied)
        parenthetical.assert_not_called()
        vocalise.assert_not_called()
        self.assertEqual([line.korean for line in matched[:2]],['번역 0']*2)
        self.assertEqual([line.reading for line in matched[:2]],['',''])

    def test_paired_asides_require_matching_balanced_translation_structure(self):
        originals,supplied=self.parenthetical_document()
        for translation in ('번역 0 작은 목소리로','번역 0 (작은 목소리로','번역 0 (작게) (응)'):
            incoming=(replace(supplied[0],original=supplied[0].original+'(小さな声で)',
                              korean=translation),*supplied[1:])
            self.assertEqual(match_parenthetical_annotations(originals,incoming),[None,*supplied[1:]])

    def test_selected_aside_does_not_remove_parentheses_present_in_reference(self):
        originals,supplied=self.parenthetical_document()
        target=(Line(originals[0].original+'(歌おう)','',''),*originals[1:])
        incoming=(Line(supplied[0].original+'(歌おう)(小さな声で)',
                       '독음 0(우타오)(백보컬)','번역 0(노래하자)(작게)'),*supplied[1:])
        result=match_parenthetical_annotations(target,incoming)
        self.assertEqual(result[0].korean,'번역 0(노래하자)')
        self.assertEqual(result[0].reading,'독음 0(우타오)')

    def test_parentheses_shared_with_reference_are_not_removed(self):
        originals,supplied=self.parenthetical_document()
        originals=(*originals[:2],Line('（窓を開けよう）','',''),*originals[2:])
        supplied=(*supplied[:2],Line('窓を開けよう','독음','창문을 열자'),*supplied[2:])
        incoming=(*supplied[:1],Line('(小さな声で)','',''),*supplied[1:])
        self.assertEqual(match_parenthetical_annotations(originals,incoming),list(supplied))

    def test_typo_in_main_document_does_not_receive_translation_for_changed_row(self):
        originals,supplied=self.parenthetical_document()
        changed=replace(supplied[2],original=supplied[2].original.replace('少年','少女'))
        incoming=(*supplied[:2],Line('(小さな声で)','',''),changed,*supplied[3:])
        self.assertEqual(match_parenthetical_annotations(originals,incoming),[*supplied[:2],None,*supplied[3:]])

    def test_missing_or_reordered_main_rows_still_reject_entire_annotation_document(self):
        originals,supplied=self.parenthetical_document()
        for incoming in ((*supplied[:2],*supplied[3:]),
                         (supplied[0],supplied[2],supplied[1],*supplied[3:])):
            self.assertIsNone(match_parenthetical_annotations(originals,(*incoming,Line('(小さな声で)','',''))))

    def test_nonparenthetical_omission_and_unbalanced_parenthesis_are_not_optional(self):
        originals,supplied=self.parenthetical_document()
        for extra in ('小さな声で歌う','(小さな声で歌う','小さな声で歌う)','[小さな声で歌う]'):
            self.assertIsNone(match_parenthetical_annotations(originals,(*supplied[:2],Line(extra,'',''),*supplied[2:])))

    def test_omitted_complete_reference_row_is_not_a_typo(self):
        originals,supplied=self.parenthetical_document()
        originals=(*originals[:2],Line('X','',''),*originals[2:])
        incoming=(*supplied[:2],Line('(小さな声で)','',''),*supplied[2:])
        self.assertIsNone(match_parenthetical_annotations(originals,incoming))

    def test_parenthetical_split_rows_keep_the_same_complete_sentence_translation(self):
        originals,supplied=self.parenthetical_document()
        originals=(Line(originals[0].original[:10],'',''),Line(originals[0].original[10:],'',''),*originals[1:])
        incoming=(*supplied[:2],Line('(小さな声で)','',''),*supplied[2:])
        self.assertEqual(match_parenthetical_annotations(originals,incoming),[supplied[0],supplied[0],*supplied[1:]])

    def test_parenthetical_merged_rows_cannot_include_a_removed_source_call(self):
        originals,supplied=self.parenthetical_document()
        target=(Line(originals[0].original+originals[1].original,'',''),*originals[2:])
        incoming=(supplied[0],Line('(小さな声で)','','보컬 번역'),*supplied[1:])
        self.assertEqual(match_parenthetical_annotations(target,incoming),[None,*supplied[2:]])

    def test_parenthetical_regrouping_does_not_copy_meaning_from_inline_omissions(self):
        originals,supplied=self.parenthetical_document()
        text=originals[0].original
        target=(Line(text[:10],'',''),Line(text[10:],'',''),*originals[1:])
        incoming=(replace(supplied[0],original=text+'(小さな声で)'),*supplied[1:])
        self.assertEqual(match_parenthetical_annotations(target,incoming),[None,None,*supplied[1:]])

    def test_repeated_phrase_uses_verified_position_for_different_translations(self):
        originals,supplied=self.parenthetical_document()
        originals=(*originals[:2],Line('また会おう','',''),*originals[2:],Line('また会おう','',''))
        supplied=(*supplied[:2],Line('また会おう','','다시 만나자'),*supplied[2:],Line('また会おう','','또 보자'))
        incoming=(*supplied[:1],Line('(小さな声で)','',''),*supplied[1:])
        self.assertEqual(match_parenthetical_annotations(originals,incoming),list(supplied))

    def test_short_chorus_or_large_omission_cannot_verify_a_recording(self):
        originals,supplied=self.parenthetical_document()
        self.assertIsNone(match_parenthetical_annotations(originals[:1],(*supplied[:1],Line('(ハイ)','',''))))
        extras=(Line('(あいうえおかきくけこさしすせそたちつてと)'*3,'',''),)
        self.assertIsNone(match_parenthetical_annotations(originals,(*supplied,*extras)))

    def test_persisted_translation_survives_restart_without_rewriting_library_or_timing(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(folder)
            song=Song('test','Example','','',(Line('窓を開ける','',''),))
            store.save_library([song]);store.save_timing(song,{'times':[12],'offset':.5})
            paths=[store.directory/'library.json',store.directory/'timings'/'test.json']
            before=[path.read_bytes() for path in paths]
            cache=LyricAnnotations(store)
            cache.save(song,[Line('窓を開ける','まどをあける','창문을 열다')],{'url':'local fixture'})
            reopened=LyricAnnotations(Store(folder))
            self.assertEqual(reopened.display_lines(store.library()[0])[0].korean,'창문을 열다')
            self.assertEqual(reopened.display_lines(song)[0].reading,'まどをあける')
            self.assertEqual(before,[path.read_bytes() for path in paths])

    def test_partial_match_is_by_original_text_not_position_and_preserves_user_translation(self):
        with tempfile.TemporaryDirectory() as folder:
            song=Song('test','Example','','',(Line('空を見て','','직접 번역'),Line('窓を開ける','','')))
            cache=LyricAnnotations(Store(folder))
            count=cache.save(song,[Line('前置き','まえおき','서문'),Line('窓を開ける','まどをあける','창문을 열다')],{})
            self.assertEqual(count,1)
            self.assertEqual([line.korean for line in cache.display_lines(song)],['직접 번역','창문을 열다'])
            changed=replace(song,lines=(Line('別の歌詞','',''),))
            self.assertEqual(cache.display_lines(changed),changed.lines)

    def test_differently_translated_repeated_phrase_requires_same_sequence(self):
        supplied=[Line('歌おう','うたおう','노래하자'),Line('歌おう','うたおう','함께 노래하자')]
        self.assertEqual(match_annotations(supplied,supplied),supplied)
        self.assertEqual(match_annotations([Line('歌おう','','')],supplied),[None])

    def test_later_annotation_fills_only_missing_saved_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(folder)
            song=Song('test','Example','','',(Line('窓を開ける','',''),Line('空を見る','','')))
            store.save_library([song]);store.save_timing(song,{'times':[12,24],'offset':.5})
            paths=[store.directory/'library.json',store.directory/'timings'/'test.json']
            before=[path.read_bytes() for path in paths]
            cache=LyricAnnotations(store)
            cache.save(song,[Line('窓を開ける','まどをあける',''),Line('空を見る','','하늘을 본다')],{})
            cache.save(song,[Line('窓を開ける','conflicting reading','창문을 열다'),
                             Line('空を見る','そらをみる','conflicting translation')],{})
            restored=LyricAnnotations(Store(folder)).display_lines(song)
            self.assertEqual(restored,(Line('窓を開ける','まどをあける','창문을 열다'),
                                       Line('空を見る','そらをみる','하늘을 본다')))
            self.assertEqual(before,[path.read_bytes() for path in paths])

    def test_original_only_saved_match_does_not_block_later_translation(self):
        with tempfile.TemporaryDirectory() as folder:
            cache=LyricAnnotations(Store(folder))
            song=Song('test','Example','','',(Line('窓を開ける','',''),))
            cache.save(song,song.lines,{})
            cache.save(song,[Line('窓を開ける','まどをあける','창문을 열다')],{})
            self.assertEqual(cache.display_lines(song)[0],Line('窓を開ける','まどをあける','창문을 열다'))

    def test_nonmatching_lyrics_do_not_create_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            cache=LyricAnnotations(Store(folder))
            song=Song('test','Example','','',(Line('窓を開ける','',''),))
            with self.assertRaises(ValueError):cache.save(song,[Line('違う歌','ちがううた','다른 노래')],{})
            self.assertFalse(cache.directory.exists())
