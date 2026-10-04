"""Other recording versions can provide meaning, never their clock."""
from dataclasses import replace
import unittest
from unittest.mock import Mock

from alsong_provider import DETAIL_ACTION,SEARCH_ACTION,fetch_alsong
from lyrics import Line
from media import Playback
from tests.test_alsong_provider import soap,detail


class VersionTranslationTests(unittest.TestCase):
    def test_length_label_looks_up_base_but_rejects_a_longer_document(self):
        for suffix in (' - Game Size',' (Game Size)',' - TV Size',' (GAME VERSION)'):
            playback=replace(self.playback,title='Synthetic Song'+suffix)
            result,_,_=self.fetch(self.item,playback=playback)
            self.assertTrue(result['annotation_only'])
            self.assertEqual(result['times'],[None]*5)
            result,_,_=self.fetch(self.item,playback=playback,reference=self.reference[:3])
            self.assertIsNone(result)

    def setUp(self):
        self.reference=('朝の窓から青い庭を眺めながら今日の行き先をゆっくり考えた',
                        '川の向こうに見える広い公園では小鳥たちが静かに遊んでいた',
                        '昼の坂道を歩く少年は遠い町から届いた手紙を大切に持っていた',
                        '夕方の電車に乗った少女は初めて訪れる海辺の景色を楽しみにした',
                        '夜の広場に集まった友人たちは明日もまた会おうと笑って別れた')
        self.playback=Playback(title='Synthetic Song -25 colors-',artist='Example Group',
                               album='New Recording',duration=90)
        self.item={'lyricID':'123','title':'Synthetic Song','artist':'Example Group',
                   'album':'Original Recording','playtime':'360',
                   'lyric':'\n'.join(f'[01:{index*10:02d}]{text}\n[01:{index*10:02d}]독음 {index}\n'
                                     f'[01:{index*10:02d}]번역 {index}'
                                     for index,text in enumerate(self.reference))}

    def fetch(self,*items,playback=None,reference=None):
        playback=playback or self.playback
        def request(action,params):
            if action==SEARCH_ACTION:
                return soap(*items) if params['title']=='Synthetic Song' else soap()
            return detail(next(item for item in items if item['lyricID']==params['lyricID']))
        calls=Mock(side_effect=request);statuses=[]
        result=fetch_alsong(playback,self.reference if reference is None else reference,
                            fetch=calls,report=statuses.append)
        return result,calls,statuses

    def test_base_version_translation_preserves_reference_and_returns_no_time(self):
        result,calls,statuses=self.fetch(self.item)
        self.assertTrue(result['annotation_only'])
        self.assertEqual(result['times'],[None]*5)
        self.assertEqual(result['lines'],tuple(Line(text,'',f'번역 {i}') for i,text in enumerate(self.reference)))
        self.assertEqual(calls.call_count,4)
        self.assertEqual(calls.call_args_list[2].args,
                         (SEARCH_ACTION,{'title':'Synthetic Song','artist':'Example Group','pageNo':'1'}))
        self.assertEqual(statuses,['알송 다른 버전 문장 번역 5줄 보충 · 시각 변경 없음'])

    def test_base_search_preserves_compatibility_title_characters(self):
        for title in ('Resonance⁺','ﬂower Song','Title…'):
            with self.subTest(title=title):
                item={**self.item,'title':title}
                playback=replace(self.playback,title=title+' -25 colors-')
                def request(action,params):
                    if action==SEARCH_ACTION:return soap(item) if params['title']==title else soap()
                    return detail(item)
                calls=Mock(side_effect=request)
                result=fetch_alsong(playback,self.reference,fetch=calls)
                self.assertTrue(result['annotation_only'])
                self.assertEqual(result['times'],[None]*len(self.reference))
                self.assertEqual(calls.call_args_list[2].args[1]['title'],title)

    def test_supported_recording_labels_share_translation_rule(self):
        for title in ('Synthetic Song (Singer Ver.)','Synthetic Song [Live]',
                      'Synthetic Song - Remastered 2026','Synthetic Song -28 colours-',
                      'Synthetic Song (25色 Version)'):
            with self.subTest(title=title):
                self.assertIsNotNone(self.fetch(self.item,playback=replace(self.playback,title=title))[0])

    def test_unrecognized_parenthetical_title_does_not_broaden_search(self):
        result,calls,_=self.fetch(self.item,playback=replace(self.playback,title='Synthetic Song (春)'))
        self.assertIsNone(result);self.assertEqual(calls.call_count,2)

    def test_multiple_base_registrations_ignore_clock_differences_but_hold_conflicting_meanings(self):
        items=[{**self.item,'lyricID':str(i),'lyric':self.item['lyric'].replace('[01:',f'[{i:02d}:')}
               for i in range(1,6)]
        items[-1]['lyric']=items[-1]['lyric'].replace('번역 2','다른 번역')
        result,calls,_=self.fetch(*items)
        self.assertEqual(calls.call_count,8)
        self.assertEqual([line.korean for line in result['lines']],['번역 0','번역 1','','번역 3','번역 4'])
        self.assertEqual(result['times'],[None]*5)

    def test_more_than_six_base_registrations_remain_bounded(self):
        result,calls,_=self.fetch(*[{**self.item,'lyricID':str(i)} for i in range(7)])
        self.assertIsNone(result);self.assertEqual(calls.call_count,3)

    def test_other_artist_title_short_cut_and_different_words_are_rejected(self):
        for changes in ({'artist':'Another Group'},{'title':'Synthetic Song (GAME VERSION)'},
                        {'lyric':self.item['lyric'].replace(self.reference[2],'別の曲だけの言葉')},
                        {'lyric':'\n'.join(self.item['lyric'].splitlines()[:9])}):
            with self.subTest(changes=tuple(changes)):
                self.assertIsNone(self.fetch({**self.item,**changes})[0])

    def test_rejected_source_does_not_claim_the_search_had_no_results(self):
        item={**self.item,'lyric':self.item['lyric'].replace(self.reference[2],'別の曲だけの言葉')}
        result,_,statuses=self.fetch(item)
        self.assertIsNone(result)
        self.assertEqual(statuses,['알송 다른 버전 원문·번역 검증 실패 · 적용 보류'])

    def test_split_original_rows_keep_the_complete_verified_sentence_meaning(self):
        reference=(self.reference[0][:10],self.reference[0][10:],*self.reference[1:])
        result,_,_=self.fetch(self.item,reference=reference)
        self.assertEqual([line.korean for line in result['lines']],['번역 0','번역 0','번역 1','번역 2','번역 3','번역 4'])
        self.assertEqual(tuple(line.original for line in result['lines']),reference)
        self.assertTrue(all(line.reading=='' for line in result['lines']))
        self.assertEqual(result['times'],[None]*6)

    def test_changed_original_rows_do_not_get_a_guessed_translation(self):
        changed={**self.item,'lyric':self.item['lyric'].replace('少年','少女')}
        result,_,_=self.fetch(changed)
        self.assertEqual([line.korean for line in result['lines']],['번역 0','번역 1','','번역 3','번역 4'])

    def test_merged_original_rows_keep_every_complete_meaning_without_timestamps(self):
        reference=(self.reference[0]+self.reference[1],*self.reference[2:])
        result,_,_=self.fetch(self.item,reference=reference)
        self.assertEqual([line.korean for line in result['lines']],['번역 0 / 번역 1','번역 2','번역 3','번역 4'])
        self.assertEqual(tuple(line.original for line in result['lines']),reference)
        self.assertTrue(all(not line.reading for line in result['lines']))
        self.assertEqual(result['times'],[None]*4)

    def test_conflicting_sentence_translations_are_held_on_every_split_row(self):
        reference=(self.reference[0][:10],self.reference[0][10:20],self.reference[0][20:],*self.reference[1:])
        other={**self.item,'lyricID':'456','lyric':self.item['lyric'].replace('번역 0','다른 문장 번역')}
        result,_,_=self.fetch(self.item,other,reference=reference)
        self.assertEqual([line.korean for line in result['lines']],['','','','번역 1','번역 2','번역 3','번역 4'])
        self.assertEqual(result['times'],[None]*7)

    def test_base_result_is_retained_after_empty_exact_title_retry(self):
        fetch=Mock(side_effect=[soap(self.item),soap(),detail(self.item)])
        result=fetch_alsong(self.playback,self.reference,fetch=fetch)
        self.assertTrue(result['annotation_only']);self.assertEqual(fetch.call_count,3)
        self.assertEqual(fetch.call_args_list[1].args,
                         (SEARCH_ACTION,{'title':self.playback.title,'artist':'','pageNo':'1'}))

    def test_wrong_detail_identity_cannot_supply_meaning(self):
        for changes in ({'lyricID':'456'},{'artist':'Another Group'},{'title':'Another Song'}):
            with self.subTest(changes=changes):
                fetch=Mock(side_effect=[soap(),soap(),soap(self.item),detail({**self.item,**changes})])
                self.assertIsNone(fetch_alsong(self.playback,self.reference,fetch=fetch))

    def test_short_chorus_does_not_authorize_cross_version_lookup(self):
        result,calls,_=self.fetch(self.item,reference=self.reference[:1])
        self.assertIsNone(result);self.assertEqual(calls.call_count,2)


if __name__=='__main__':unittest.main()
