from dataclasses import replace
import unittest
from unittest.mock import call,patch

from lyric_annotations import _local_reading,readings_for_display
from lyrics import Line,Song


class DisplayReadingTests(unittest.TestCase):
    def setUp(self):_local_reading.cache_clear()
    def tearDown(self):_local_reading.cache_clear()

    def test_current_phrases_are_read_whole_and_successes_are_cached(self):
        lines=(Line('青い空を','','뜻'),Line('見ながら歩こう','','뜻'),Line('青い空を','','반복'))
        before=tuple(lines);song=Song('test','Song','Singer','Album',lines);fingerprint=song.fingerprint
        with patch('web_lyrics.reading',side_effect=['あおい そら を','みながら あるこう']) as read:
            expected=('あおい そら を','みながら あるこう','あおい そら を')
            self.assertEqual(readings_for_display(lines),expected)
            self.assertEqual(readings_for_display(lines),expected)
        self.assertEqual(read.call_args_list,[call('青い空を'),call('見ながら歩こう')])
        self.assertEqual(lines,before);self.assertEqual(song.fingerprint,fingerprint)

    def test_supplied_reading_arriving_later_always_replaces_the_local_display(self):
        line=Line('明日の空','','뜻')
        with patch('web_lyrics.reading',return_value='あした の そら') as read:
            self.assertEqual(readings_for_display((line,)),('あした の そら',))
            for supplied in ('あすのそら','아스노 소라'):
                self.assertEqual(readings_for_display((replace(line,reading=supplied),)),(supplied,))
        read.assert_called_once_with(line.original)
        self.assertEqual(line.reading,'')

    def test_only_empty_japanese_rows_are_generated(self):
        lines=(Line('青空','user reading','뜻'),Line('English words','',''),
               Line('123 !','',''),Line('','',''),Line('한글','',''),Line('花を描く','  ',''))
        with patch('web_lyrics.reading',return_value='はな を えがく') as read:
            self.assertEqual(readings_for_display(lines),('user reading','','','','','はな を えがく'))
        read.assert_called_once_with('花を描く')

    def test_failure_is_not_cached_or_allowed_to_hide_other_rows(self):
        lines=(Line('青空','',''),Line('花','',''),Line('青空','',''))
        with patch('web_lyrics.reading',side_effect=[RuntimeError('temporary'), 'はな','あおぞら']) as read:
            self.assertEqual(readings_for_display(lines),('','はな',''))
            self.assertEqual(readings_for_display(lines),('あおぞら','はな','あおぞら'))
        self.assertEqual(read.call_args_list,[call('青空'),call('花'),call('青空')])

    def test_empty_converter_result_can_recover_on_next_refresh(self):
        lines=(Line('歌おう','',''),)
        with patch('web_lyrics.reading',side_effect=['  ','うたおう']):
            self.assertEqual(readings_for_display(lines),('',))
            self.assertEqual(readings_for_display(lines),('うたおう',))
        self.assertEqual(readings_for_display(()),())


if __name__=='__main__':unittest.main()
