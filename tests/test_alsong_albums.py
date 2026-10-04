"""Different releases may add verified meaning without replacing any clock."""
from dataclasses import replace
from http.client import IncompleteRead
import unittest
from unittest.mock import Mock, patch

from alsong_provider import DETAIL_ACTION, SEARCH_ACTION, fetch_alsong
from lyrics import Line
from tests.test_alsong_provider import detail, soap
from tests import test_alsong_versions as version_fixtures


class AlbumAnnotationTests(unittest.TestCase):
    def setUp(self):
        version_fixtures.VersionTranslationTests.setUp(self)
        self.playback=replace(self.playback,title='Synthetic Song')

    def fetch(self,*items,reference=None,search_items=None):
        def request(action,params):
            if action==SEARCH_ACTION:return soap(*(items if search_items is None else search_items))
            self.assertEqual(action,DETAIL_ACTION)
            return detail(next(item for item in items if item['lyricID']==params['lyricID']))
        calls=Mock(side_effect=request);statuses=[]
        result=fetch_alsong(self.playback,self.reference if reference is None else reference,
                            fetch=calls,report=statuses.append)
        return result,calls,statuses

    def test_other_album_preserves_originals_and_supplies_no_clock_or_reading(self):
        originals=tuple(Line(text,'existing reading','') for text in self.reference)
        result,calls,statuses=self.fetch(self.item,reference=originals)
        self.assertTrue(result['annotation_only'])
        self.assertEqual(tuple(line.original for line in result['lines']),self.reference)
        self.assertEqual([line.korean for line in result['lines']],[f'번역 {i}' for i in range(5)])
        self.assertTrue(all(not line.reading for line in result['lines']))
        self.assertEqual(result['times'],[None]*5)
        self.assertTrue(all(line.reading=='existing reading' for line in originals))
        self.assertEqual(calls.call_count,2)
        self.assertEqual(statuses,['알송 다른 앨범 문장 번역 5줄 보충 · 시각 변경 없음'])

    def test_detail_album_mismatch_reuses_the_already_requested_detail(self):
        for search_album in ('',self.playback.album):
            with self.subTest(search_album=search_album):
                result,calls,_=self.fetch(self.item,search_items=[{**self.item,'album':search_album}])
                self.assertTrue(result['annotation_only'])
                self.assertEqual(result['times'],[None]*5)
                self.assertEqual(calls.call_count,2)

    def test_conflicting_translations_hold_only_the_disputed_rows_regardless_of_clock(self):
        other={**self.item,'lyricID':'456','lyric':self.item['lyric'].replace('[01:','[05:').replace('번역 2','다른 뜻')}
        result,_,_=self.fetch(self.item,other)
        self.assertEqual([line.korean for line in result['lines']],['번역 0','번역 1','','번역 3','번역 4'])
        self.assertEqual(result['times'],[None]*5)

    def test_all_conflicting_translations_are_held(self):
        other={**self.item,'lyricID':'456','lyric':self.item['lyric'].replace('번역','다른 뜻')}
        result,_,statuses=self.fetch(self.item,other)
        self.assertIsNone(result)
        self.assertEqual(statuses,['알송 번역이 서로 달라 적용 보류'])

    def test_more_than_six_registrations_do_not_request_arbitrary_details(self):
        result,calls,_=self.fetch(*[{**self.item,'lyricID':str(i)} for i in range(7)])
        self.assertIsNone(result)
        self.assertEqual(calls.call_count,1)

    def test_different_words_or_an_incomplete_reference_cannot_supply_meaning(self):
        changed={**self.item,'lyric':self.item['lyric'].replace(self.reference[2],'別の曲の言葉')}
        self.assertIsNone(self.fetch(changed)[0])
        self.assertIsNone(self.fetch(self.item,reference=self.reference[:3])[0])

    def test_detail_title_or_artist_mismatch_does_not_supply_translation(self):
        for changes in ({'title':'Other Song'},{'artist':'Other Singer'}):
            with self.subTest(changes=changes):
                result,_,_=self.fetch({**self.item,**changes},search_items=[self.item])
                self.assertIsNone(result)

    def test_two_row_korean_format_does_not_guess_translation_versus_reading(self):
        rows=self.item['lyric'].splitlines()
        item={**self.item,'lyric':'\n'.join(row for index,row in enumerate(rows) if index%3!=2)}
        self.assertIsNone(self.fetch(item)[0])

    def test_a_matching_album_still_keeps_its_verified_timing(self):
        matching={**self.item,'lyricID':'456','album':self.playback.album,'playtime':'90',
                  'lyric':self.item['lyric'].replace('[01:','[00:')}
        result,calls,_=self.fetch(self.item,matching)
        self.assertNotIn('annotation_only',result)
        self.assertEqual(result['provider_id'],'456')
        self.assertEqual(result['times'],[0.0,10.0,20.0,30.0,40.0])
        self.assertEqual(calls.call_count,2)

    def timed_item(self,translated=()):
        rows=[]
        for index,text in enumerate(self.reference):
            stamp=f'[00:{index*10:02d}]'
            rows.append(stamp+text)
            if index in translated:
                rows.extend((stamp+f'기존 독음 {index}',stamp+f'기존 번역 {index}'))
        return {**self.item,'lyricID':'456','album':self.playback.album,'playtime':'90',
                'lyric':'\n'.join(rows)}

    def test_matching_album_clock_can_use_other_album_meaning_without_replacing_timing(self):
        result,calls,statuses=self.fetch(self.timed_item(),self.item)
        self.assertEqual(result['provider_id'],'456')
        self.assertNotIn('annotation_only',result)
        self.assertEqual(result['times'],[0.0,10.0,20.0,30.0,40.0])
        self.assertEqual(tuple(line.original for line in result['lines']),self.reference)
        self.assertEqual([line.korean for line in result['lines']],[f'번역 {i}' for i in range(5)])
        self.assertTrue(all(not line.reading for line in result['lines']))
        self.assertEqual(calls.call_count,3)
        self.assertEqual(statuses,['알송 싱크 유지 · 다른 앨범 번역 5줄 보충'])

    def test_existing_translation_and_reading_survive_blank_only_supplement(self):
        result,calls,_=self.fetch(self.timed_item(translated=(0,2)),self.item)
        self.assertEqual(result['times'],[0.0,10.0,20.0,30.0,40.0])
        self.assertEqual([line.korean for line in result['lines']],
            ['기존 번역 0','번역 1','기존 번역 2','번역 3','번역 4'])
        self.assertEqual([line.reading for line in result['lines']],['기존 독음 0','','기존 독음 2','',''])
        self.assertEqual(calls.call_count,3)

    def test_wrong_supplement_text_keeps_the_verified_clock_and_empty_meanings(self):
        wrong={**self.item,'lyric':self.item['lyric'].replace(self.reference[2],'別の曲の言葉')}
        result,calls,_=self.fetch(self.timed_item(),wrong)
        self.assertEqual(result['times'],[0.0,10.0,20.0,30.0,40.0])
        self.assertTrue(all(not line.korean for line in result['lines']))
        self.assertEqual(calls.call_count,3)

    def test_complete_translation_never_fetches_other_album_details(self):
        result,calls,_=self.fetch(self.timed_item(translated=range(5)),self.item)
        self.assertEqual([line.korean for line in result['lines']],[f'기존 번역 {i}' for i in range(5)])
        self.assertEqual(calls.call_count,2)
        self.assertEqual(calls.call_args_list[-1].args[1],{'lyricID':'456'})

    def test_optional_network_failure_cannot_remove_verified_timing(self):
        item=self.timed_item()
        calls=Mock(side_effect=[soap(item,self.item),detail(item),TimeoutError('optional lookup')])
        result=fetch_alsong(self.playback,self.reference,fetch=calls)
        self.assertEqual(result['times'],[0.0,10.0,20.0,30.0,40.0])
        self.assertEqual(result['provider_id'],'456')

    def test_incomplete_optional_http_response_cannot_remove_verified_timing(self):
        item=self.timed_item()
        calls=Mock(side_effect=[soap(item,self.item),detail(item),IncompleteRead(b'',1)])
        result=fetch_alsong(self.playback,self.reference,fetch=calls)
        self.assertEqual(result['times'],[0.0,10.0,20.0,30.0,40.0])
        self.assertEqual(result['provider_id'],'456')
        self.assertEqual(tuple(line.original for line in result['lines']),self.reference)
        self.assertTrue(all(not line.korean for line in result['lines']))

    def test_known_other_album_supplement_is_bounded_and_called_once(self):
        items=[{**self.item,'lyricID':str(i)} for i in range(7)]
        from alsong_provider import _version_annotations
        with patch('alsong_provider._version_annotations',wraps=_version_annotations) as annotations:
            result,calls,_=self.fetch(self.timed_item(),*items)
        self.assertEqual(result['times'],[0.0,10.0,20.0,30.0,40.0])
        self.assertTrue(all(not line.korean for line in result['lines']))
        self.assertEqual(calls.call_count,2)
        annotations.assert_called_once()

    def test_conflicting_other_album_meanings_remain_empty_with_verified_clock(self):
        other={**self.item,'lyricID':'789','lyric':self.item['lyric'].replace('번역 2','다른 뜻')}
        result,calls,_=self.fetch(self.timed_item(),self.item,other)
        self.assertEqual(result['times'],[0.0,10.0,20.0,30.0,40.0])
        self.assertEqual([line.korean for line in result['lines']],['번역 0','번역 1','','번역 3','번역 4'])
        self.assertEqual(calls.call_count,4)


if __name__=='__main__':unittest.main()
