from types import SimpleNamespace
import unittest
from unittest.mock import patch

from work_reuse import same_work,work_title


class WorkReuseTests(unittest.TestCase):
    def target(self,title,artist):return SimpleNamespace(title=title,artist=artist)

    def test_performer_versions_share_a_known_work(self):
        source={'title':'MEET THE WORLD! (DRAMATIC STARS Ver.)','artist':'DRAMATIC STARS'}
        self.assertTrue(same_work(source,self.target('MEET THE WORLD! (Jupiter Ver.)','Jupiter')))
        self.assertFalse(same_work(source,self.target('MEET THE WORLD!','Unrelated Singer')))

    def test_meaningful_subtitle_does_not_become_the_original_work(self):
        self.assertNotEqual(work_title('Song (Another Story)'),work_title('Song'))
        self.assertEqual(work_title('Song (如月千早 Ver.)'),work_title('Song'))
        self.assertEqual(work_title('Song (MILLION C@STING 04 ver.)'),work_title('Song'))

    def test_real_character_solo_and_unit_versions_have_work_evidence(self):
        source={'title':'Harmony 4 You (舞浜 歩 Ver.)','artist':'舞浜 歩 (CV.戸田めぐみ)'}
        self.assertTrue(same_work(source,self.target('Harmony 4 You (木下ひなた Ver.)','木下ひなた (CV.田村奈央)')))
        source={'title':'グッドサイン','artist':'765 MILLION ALLSTARS'}
        self.assertTrue(same_work(source,self.target('グッドサイン - プリンセススターズ ver.','プリンセススターズ')))

    def test_same_title_in_unrelated_series_does_not_supply_work_evidence(self):
        with patch('work_reuse._artist_identity',side_effect=lambda value:value),patch('work_reuse._index',return_value={
                ('song','A'):{('song','million',False)},('song','B'):{('song','sidem',False)}}):
            self.assertFalse(same_work({'title':'Song','artist':'A'},self.target('Song','B')))

    def test_ambiguous_work_evidence_is_rejected(self):
        records={('song','A'):{('first','million',False),('second','million',False)},
                 ('song','B'):{('first','million',False),('second','million',False)}}
        with patch('work_reuse._artist_identity',side_effect=lambda value:value),patch('work_reuse._index',return_value=records):
            self.assertFalse(same_work({'title':'Song','artist':'A'},self.target('Song','B')))


if __name__=='__main__':unittest.main()
