"""Weak whole-track candidates must get independent partial verification."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from audio_alignment import ALIGNMENT_METHOD, INCREMENTAL_METHOD, AlignmentUnmatched, _align_with_model
from tests.test_incremental_alignment import Model, TEXTS


class WholeCaptureFallbackTests(unittest.TestCase):
    def run_alignment(self, matched=1, heard=(1,), shift=0, texts=TEXTS):
        model=Model(texts,[5,20,55,75,90][:len(texts)],heard=list(heard),shift=shift)
        align=model.align
        segments=[{'text':text,'start':stamp,'end':stamp+1,'words':
            [{'word':text,'start':stamp,'end':stamp+1,'probability':.9}] if index<matched else []}
            for index,(text,stamp) in enumerate(zip(texts,model.positions))]
        model.align=lambda samples,text,**options:(SimpleNamespace(to_dict=lambda:{'segments':segments})
            if len(samples)==100*16000 else align(samples,text,**options))
        progress=[]
        result=_align_with_model(model,{'lines':texts,'duration':100,'incremental':True},
                                 range(100*16000),0,lambda current,total:progress.append(current/total))
        return result,model,progress

    def test_weak_whole_capture_recovers_only_independently_verified_rows(self):
        result,model,progress=self.run_alignment(matched=1,heard=(1,))
        self.assertEqual(result['method'],INCREMENTAL_METHOD)
        self.assertEqual(result['times'],[None,20,None,None])
        self.assertTrue(result['incremental']);self.assertTrue(result['partial'])
        self.assertEqual(result['analyzed_ranges'],[[0,100]])
        self.assertEqual(model.decoded,[100])
        self.assertEqual(progress,sorted(progress));self.assertEqual(progress[-1],1)

    def test_accepted_whole_capture_keeps_fast_path_without_recognition(self):
        result,model,_=self.run_alignment(matched=3,heard=(1,))
        self.assertEqual(result['method'],ALIGNMENT_METHOD)
        self.assertEqual(result['times'],[5,20,55,None])
        self.assertFalse(result['partial']);self.assertEqual(model.decoded,[])

    def test_exact_sixty_percent_keeps_existing_full_alignment_threshold(self):
        result,model,_=self.run_alignment(matched=3,texts=[*TEXTS,'新しい歌を届ける'])
        self.assertEqual(result['method'],ALIGNMENT_METHOD)
        self.assertEqual(result['times'],[5,20,55,None,None])
        self.assertEqual(model.decoded,[])

    def test_no_independent_support_preserves_original_unapplied_candidate(self):
        result,model,_=self.run_alignment(matched=1,heard=())
        self.assertEqual(model.decoded,[100])
        self.assertEqual(result['method'],ALIGNMENT_METHOD)
        self.assertEqual(result['times'],[5,None,None,None])
        self.assertTrue(result['incremental'])
        self.assertFalse(result['partial'])

    def test_disagreement_never_promotes_forced_candidates_to_verified_method(self):
        result,model,_=self.run_alignment(matched=1,heard=(1,),shift=4)
        self.assertEqual(model.decoded,[100])
        self.assertEqual(result['method'],ALIGNMENT_METHOD)
        self.assertEqual(result['times'],[5,None,None,None])

    def test_explicit_unmatched_fallback_keeps_full_candidate_for_review(self):
        with patch('audio_alignment._align_incremental',side_effect=AlignmentUnmatched('No reliable phrases')) as fallback:
            result,_,progress=self.run_alignment(matched=1)
        fallback.assert_called_once()
        self.assertEqual(result['method'],ALIGNMENT_METHOD)
        self.assertEqual(result['times'],[5,None,None,None])
        self.assertEqual(progress[-1],1)

    def test_runtime_error_remains_available_for_existing_cuda_retry(self):
        with patch('audio_alignment._align_incremental',side_effect=RuntimeError('CUDA failed')):
            with self.assertRaisesRegex(RuntimeError,'CUDA failed'):self.run_alignment(matched=1)


if __name__=='__main__':unittest.main()
