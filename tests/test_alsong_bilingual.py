"""Conservative document-wide meaning evidence for ALSong two-row lyrics."""
from dataclasses import replace
import unittest
from unittest.mock import Mock

from alsong_provider import SEARCH_ACTION, _parallel_annotations, fetch_alsong
from tests.test_alsong_provider import detail, soap
from tests import test_alsong_versions as version_fixtures


class AlsongBilingualTests(unittest.TestCase):
    def setUp(self):
        version_fixtures.VersionTranslationTests.setUp(self)
        self.playback=replace(self.playback,title='Synthetic Song')
        self.meanings=('오늘의 목적지를 천천히 생각했어','새들이 조용히 놀고 있었어',
            '소년은 편지를 소중하게 간직했어','소녀는 바닷가 풍경이 기대됐어',
            '친구들은 내일 다시 만나자고 웃었어')
        self.rows=[]
        for index,(original,korean) in enumerate(zip(self.reference,self.meanings)):
            stamp=f'[00:{index*10:02d}]'
            self.rows.extend((stamp+original,stamp+korean))
        self.item={**self.item,'title':self.playback.title,'album':self.playback.album,
                   'playtime':'90','lyric':'\n'.join(self.rows)}

    def fetch(self,item=None,playback=None,reference=None):
        item=self.item if item is None else item
        calls=Mock(side_effect=lambda action,params:soap(item) if action==SEARCH_ACTION else detail(item))
        result=fetch_alsong(playback or self.playback,self.reference if reference is None else reference,fetch=calls)
        return result,calls

    def test_document_verified_meaning_pairs_keep_original_timestamps(self):
        result,calls=self.fetch()
        self.assertEqual([line.korean for line in result['lines']],list(self.meanings))
        self.assertTrue(all(not line.reading for line in result['lines']))
        self.assertEqual(result['times'],[0.0,10.0,20.0,30.0,40.0])
        self.assertNotIn('annotation_only',result)
        self.assertEqual(calls.call_count,2)

    def test_other_album_pairs_provide_meaning_without_any_clock(self):
        result,_=self.fetch(item={**self.item,'album':'Another Album','playtime':'360'})
        self.assertTrue(result['annotation_only'])
        self.assertEqual([line.korean for line in result['lines']],list(self.meanings))
        self.assertEqual(result['times'],[None]*5)

    def test_ambiguous_pronunciation_pairs_remain_unclassified(self):
        readings=('아사노마도카라','카와노무코오니','히루노사카미치','유우가타노덴샤','요루노히로바니')
        rows=[row if index%2==0 else row[:8]+readings[index//2] for index,row in enumerate(self.rows)]
        self.assertFalse(_parallel_annotations(rows))

    def test_one_or_two_meaning_markers_do_not_approve_a_document(self):
        rows=[row if index%2==0 or index<4 else row[:8]+'하나오미테이루' for index,row in enumerate(self.rows)]
        self.assertFalse(_parallel_annotations(rows))

    def test_repeated_same_phrase_is_not_multiple_independent_meaning_evidence(self):
        rows=[]
        for index in range(5):
            stamp=f'[00:{index*10:02d}]'
            rows.extend((stamp+self.reference[0],stamp+self.meanings[0]))
        self.assertFalse(_parallel_annotations(rows))

    def test_three_row_document_does_not_reclassify_a_missing_translation_as_meaning(self):
        rows=[]
        for index in range(5):
            stamp=f'[00:{index*10:02d}]'
            rows.extend((stamp+self.reference[index],stamp+self.meanings[index]))
            if index<3:rows.append(stamp+f'확인된 뜻 {index}')
        values=_parallel_annotations(rows)
        self.assertEqual(len(values),3)
        self.assertTrue(all(value[1].startswith('확인된 뜻') for value in values.values()))

    def test_different_timestamps_do_not_pair_original_and_korean(self):
        rows=[row if index%2==0 else row.replace(':00]',':01]').replace(':10]',':11]')
              .replace(':20]',':21]').replace(':30]',':31]').replace(':40]',':41]')
              for index,row in enumerate(self.rows)]
        self.assertFalse(_parallel_annotations(rows))

    def test_whole_original_and_clock_guards_still_apply(self):
        changed={**self.item,'lyric':self.item['lyric'].replace(self.reference[2],'別の曲の言葉')}
        self.assertIsNone(self.fetch(item=changed)[0])
        invalid_clock={**self.item,'lyric':self.item['lyric'].replace('[00:30]','[00:15]')}
        self.assertIsNone(self.fetch(item=invalid_clock)[0])
        self.assertIsNone(self.fetch(reference=self.reference[:3])[0])


if __name__=='__main__':unittest.main()
