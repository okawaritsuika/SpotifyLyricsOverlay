"""Conservative Korean-meaning recognition for pronunciation-free wiki tables."""
import unittest

from web_lyrics import has_korean_translation_pairs,parse_html,parse_table_text


PAIRS=(
    ('君がいてくれたから (We find our star.)','네가 있어줬으니까 (We find our star.)'),
    ('見つけられたよ','찾아낸 거야'),
    ('遠い空に憧れてた','먼 하늘을 동경하고 있었어'),
    ('気持ちのまま走って','마음 가는 대로 달려왔어'),
    ('その手に掲げてる','그 손에 들고 있는'),
)


def table(pairs=PAIRS):return '\n'.join(value for pair in pairs for value in pair)


class BilingualNamuTests(unittest.TestCase):
    def parse(self,text,**options):
        return parse_table_text(text,reader=lambda original:'local reading',**options)

    def test_multiple_distinct_korean_grammar_rows_establish_bilingual_table(self):
        for strict in (True,False):
            with self.subTest(strict=strict):
                lines=self.parse(table(),strict=strict)
                self.assertEqual(tuple(line.original for line in lines),tuple(row[0] for row in PAIRS))
                self.assertEqual(tuple(line.korean for line in lines),tuple(row[1] for row in PAIRS))
                self.assertTrue(all(line.reading=='local reading' for line in lines))

    def test_shared_document_classifier_does_not_change_its_groups(self):
        groups=tuple((original,(korean,)) for original,korean in PAIRS)
        before=repr(groups)
        self.assertTrue(has_korean_translation_pairs(groups))
        self.assertEqual(repr(groups),before)
        self.assertFalse(has_korean_translation_pairs(groups[:2]))
        self.assertFalse(has_korean_translation_pairs(groups+((PAIRS[0][0],('발음','뜻')),)))

    def test_meaning_without_a_grammar_marker_can_follow_confirmed_table_convention(self):
        lines=self.parse(table(PAIRS+(('一緒に','함께'),)))
        self.assertEqual(lines[-1].korean,'함께')

    def test_pure_japanese_transcription_is_not_a_translation(self):
        pronunciation=('키미가 이테 쿠레타카라','미츠케라레타요','토오이 소라니 아코가레테타',
                       '키모치노 마마 하싯테','소노 테니 카카게테루')
        text=table(tuple((pair[0],reading) for pair,reading in zip(PAIRS,pronunciation)))
        self.assertTrue(all(not line.korean for line in self.parse(text,strict=False)))
        with self.assertRaises(ValueError):self.parse(text)

    def test_one_or_two_meaning_rows_do_not_establish_unknown_table_convention(self):
        for count in (1,2):
            with self.subTest(count=count):
                text=table(PAIRS[:count])
                self.assertTrue(all(not line.korean for line in self.parse(text,strict=False)))
                with self.assertRaises(ValueError):self.parse(text)

    def test_repeated_refrain_does_not_multiply_grammar_evidence(self):
        text=table(PAIRS[:1]*6)
        self.assertTrue(all(not line.korean for line in self.parse(text,strict=False)))

    def test_same_meaning_repeated_for_distinct_originals_is_insufficient(self):
        text=table(tuple((original,'함께 있었어') for original,_ in PAIRS))
        self.assertTrue(all(not line.korean for line in self.parse(text,strict=False)))

    def test_sparse_meaning_rows_do_not_approve_a_mostly_ambiguous_table(self):
        ambiguous=tuple((f'別の歌詞{index}','마도오 아케루') for index in range(8))
        self.assertTrue(all(not line.korean for line in self.parse(table(PAIRS[:3]+ambiguous),strict=False)))

    def test_regular_three_row_table_never_promotes_its_lone_pronunciation(self):
        text='窓を開ける\n마도오 아케루\n창문을 열었어\n'+table(PAIRS[:3])+'\n夜が来る\n요루가 쿠루'
        lines=self.parse(text,strict=False)
        self.assertEqual(lines[0].korean,'창문을 열었어')
        self.assertTrue(all(not line.korean for line in lines[1:]))
        with self.assertRaisesRegex(ValueError,'불완전'):self.parse(text)

    def test_triples_and_sidem_pairs_keep_existing_conventions(self):
        triple='窓を開ける\n마도오 아케루\n창문을 열다'
        self.assertEqual(self.parse(triple)[0].korean,'창문을 열다')
        self.assertEqual(self.parse(table(PAIRS[:1]),source='sidem')[0].korean,PAIRS[0][1])

    def test_english_refrain_without_korean_keeps_its_place(self):
        lines=self.parse(table()+'\nWe find our star.')
        self.assertEqual(len(lines),len(PAIRS)+1)
        self.assertEqual(lines[-1].original,'We find our star.')
        self.assertEqual(lines[-1].korean,'')

    def test_missing_originals_translation_does_not_shift_the_next_pairs_meaning(self):
        pairs=PAIRS[:2]+(('空を見て',),)+PAIRS[2:]
        text='\n'.join(value for pair in pairs for value in pair)
        lines=self.parse(text,strict=False)
        self.assertEqual(tuple(line.original for line in lines),tuple(pair[0] for pair in pairs))
        self.assertEqual(tuple(line.korean for line in lines),
                         (PAIRS[0][1],PAIRS[1][1],'',PAIRS[2][1],PAIRS[3][1],PAIRS[4][1]))
        with self.assertRaisesRegex(ValueError,'불완전'):self.parse(text)

    def test_ruby_readings_are_removed_before_pasted_table_original_is_collected(self):
        text=table().replace('君','<ruby><rb>君</rb><rp>(</rp><rt>きみ</rt><rp>)</rp></ruby>',1)
        lines=self.parse(text)
        self.assertEqual(lines[0].original,PAIRS[0][0])
        self.assertEqual(lines[0].korean,PAIRS[0][1])

    def test_html_source_and_pasted_table_use_the_same_bilingual_rules(self):
        html='<html><h1>Synthetic Song</h1><h2>가사</h2><table><tr><td>'+table().replace('\n','<br>')+'</td></tr></table></html>'
        lines,title=parse_html(html,'Synthetic Song',reader=lambda original:'local reading')
        self.assertEqual(title,'Synthetic Song')
        self.assertEqual(lines,self.parse(table()))


if __name__=='__main__':unittest.main()
