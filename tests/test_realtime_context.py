import copy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

import numpy as np
from realtime_audio import ContextAligner,StreamingAligner


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.acoustic=SimpleNamespace(follower=SimpleNamespace(index=None,pending=None,last_window=-1),
                                      stalled=0,search_step=0,analyze=Mock(return_value=None))
        with patch('realtime_audio.PartialAligner',return_value=self.acoustic):
            self.aligner=ContextAligner(Mock(),['青い空を見上げて','明日へ向かって歩く'])

    def test_short_startup_never_forces_lyrics_over_limited_context(self):
        with patch.object(StreamingAligner,'analyze') as decode:
            for seconds in (1,3,5,7):
                self.assertIsNone(self.aligner.analyze(np.zeros(seconds*16000),0))
            self.assertIsNone(self.aligner.analyze(np.zeros(127999),0))
            self.acoustic.analyze.assert_not_called();decode.assert_not_called()

    def test_direct_acoustic_advance_sets_exact_evidence_without_free_decoding(self):
        self.acoustic.analyze.return_value={'index':0,'confidence':.7,'evidence_start':6.,'evidence_end':7.8}
        with patch.object(StreamingAligner,'analyze') as decode:
            result=self.aligner.analyze(np.zeros(128000),0)
        self.assertEqual(result['match_method'],'supplied-lyrics')
        self.assertEqual(self.aligner.follower.index,0)
        self.assertEqual(self.aligner.follower.confirmed_end,7.8)
        decode.assert_not_called()

    def test_stalled_direct_alignment_uses_recognition_to_recover_both_positions(self):
        self.aligner.follower.index=self.acoustic.follower.index=0
        self.acoustic.stalled=6
        def recover(*args):
            self.aligner.follower.index=1;self.aligner.follower.confirmed_end=27.8
            return {'index':1,'confidence':.9,'evidence_end':27.8,'match_method':'kana'}
        with patch.object(StreamingAligner,'analyze',side_effect=recover) as decode:
            result=self.aligner.analyze(np.zeros(128000),20)
        decode.assert_called_once()
        self.assertEqual((result['index'],self.acoustic.follower.index,self.aligner.follower.index),(1,1,1))
        self.assertEqual(self.acoustic.stalled,0)
        self.assertEqual(self.aligner.follower.confirmed_end,27.8)

    def test_failed_recovery_does_not_invent_a_line(self):
        self.acoustic.stalled=7
        with patch.object(StreamingAligner,'analyze',return_value=None):
            self.assertIsNone(self.aligner.analyze(np.zeros(128000),20))
        self.assertIsNone(self.aligner.follower.index)

    def test_extra_checkpoint_restores_pending_and_search_after_unpublished_result(self):
        self.acoustic.follower.pending={'index':1,'evidence':{'end':7.8}}
        before=copy.deepcopy(vars(self.acoustic.follower))
        snapshot=self.aligner.acoustic_checkpoint()
        self.acoustic.follower.pending['evidence']['end']=9
        self.acoustic.follower.index=1;self.acoustic.follower.last_window=9
        self.acoustic.stalled=8;self.acoustic.search_step=3
        self.aligner.restore_acoustic(snapshot)
        self.assertEqual(vars(self.acoustic.follower),before)
        self.assertEqual((self.acoustic.stalled,self.acoustic.search_step),(0,0))


if __name__=='__main__':unittest.main()
