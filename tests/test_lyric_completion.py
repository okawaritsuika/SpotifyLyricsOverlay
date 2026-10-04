import unittest
from lyrics import Line
from lyric_completion import complete_lines,expand_timing


def rows(*texts):return tuple(Line(text,'','') for text in texts)


class CompletionTests(unittest.TestCase):
    def test_join_wrapped_rows_keep_translation_and_insert_missing_repeats(self):
        saved=(Line('青い空を見る','あおい','푸른 하늘'),Line('最後の言葉','さいご','마지막'))
        supplied=rows('青い空','を見る','新しい言葉','新しい言葉','最後の言葉')
        lines,indices=complete_lines(saved,supplied)
        self.assertEqual(indices,(0,None,None,1))
        self.assertEqual(lines[0],saved[0]);self.assertEqual(lines[-1],saved[-1])
        self.assertEqual(lines[1:3],supplied[2:4])
        timing={'times':[12,90],'origins':['manual','audio'],'automatic_times':[11,90],
                'offset':.5,'audio_finalized':True,'fingerprint':'old',
                'analyzed_ranges':[[0,100]],'analysis_duration':120}
        expanded=expand_timing(timing,indices)
        self.assertEqual(expanded['times'],[12,None,None,90])
        self.assertEqual(expanded['origins'],['manual',None,None,'audio'])
        self.assertEqual(expanded['automatic_times'],[11,None,None,90])
        self.assertEqual(expanded['offset'],.5)
        self.assertNotIn('fingerprint',expanded);self.assertNotIn('audio_finalized',expanded)
        self.assertEqual(timing['times'],[12,90])
        self.assertNotIn('analyzed_ranges',expanded)
        self.assertNotIn('analysis_duration',expanded)

    def test_line_wrapping_alone_is_not_missing_lyrics(self):
        self.assertIsNone(complete_lines(rows('青い空を見る','最後の言葉'),rows('青い空','を見る','最後の','言葉')))

    def test_partial_reordered_and_other_song_are_not_applied(self):
        saved=rows('青い空を見る','最後の言葉')
        for supplied in (rows('青い空を見る','違う歌','違う終わり'),
                         rows('最後の言葉','新しい言葉','青い空を見る'),rows('青い空を見る')):
            self.assertIsNone(complete_lines(saved,supplied))

    def test_missing_prefix_suffix_and_repeat_keep_order(self):
        saved=(Line('青い空を見る','','하늘'),Line('最後の言葉','','마지막'))
        lines,indices=complete_lines(saved,rows('新しい始まり','青い空を見る','最後の言葉','青い空を見る'))
        self.assertEqual(indices,(None,0,1,None));self.assertEqual(lines[-1].korean,'하늘')

    def test_saved_45_rows_with_split_lines_and_37_omissions(self):
        saved=rows(*(f'合成文章{i}続き' for i in range(45)))
        supplied=[]
        for i,line in enumerate(saved):
            if i==43:supplied.extend(rows(*(f'追加文章{j}' for j in range(37))))
            if i<13:supplied.extend(rows(f'合成文章{i}','続き'))
            else:supplied.append(line)
        self.assertEqual(len(supplied),95)
        lines,indices=complete_lines(saved,supplied)
        self.assertEqual(len(lines),82);self.assertEqual(sum(i is None for i in indices),37)
        self.assertEqual([line for line,i in zip(lines,indices) if i is not None],list(saved))


if __name__=='__main__':unittest.main()
