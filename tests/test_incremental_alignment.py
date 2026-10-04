import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from audio_alignment import _align_with_model,normalize_ranges,uncovered_ranges,INCREMENTAL_METHOD


TEXTS=['朝の窓を開ける','青い花を描こう','夜の道を歩こう','星に願いを届ける']


class Model:
    def __init__(self,texts,positions,clip_start=0,heard=None,shift=0):
        self.texts=texts;self.positions=positions;self.clip_start=clip_start
        self.heard=heard if heard is not None else list(range(len(texts)))
        self.audio_origin=clip_start
        self.shift=shift;self.aligned=[];self.decoded=[]

    def transcribe_original(self,samples,**kwargs):
        self.audio_origin=self.clip_start-samples[0]/16000
        self.decoded.append(len(samples)/16000)
        words=[SimpleNamespace(word=self.texts[i],start=self.positions[i]-self.clip_start,
                    end=self.positions[i]-self.clip_start+1,probability=.9) for i in self.heard]
        segment=SimpleNamespace(compression_ratio=1,avg_logprob=-.2,words=words,
                                end=max((w.end for w in words),default=0))
        return iter([segment]),None

    def align(self,samples,text,**kwargs):
        selected=text.split('\n');self.aligned.append((len(samples)/16000,selected))
        segments=[]
        for value in selected:
            stamp=self.positions[self.texts.index(value)]-(self.audio_origin+samples[0]/16000)+self.shift
            segments.append({'text':value,'start':stamp,'end':stamp+1,'words':[
                {'word':value,'start':stamp,'end':stamp+1,'probability':.9}]})
        return SimpleNamespace(to_dict=lambda:{'segments':segments})


class IncrementalAlignmentTests(unittest.TestCase):
    def request(self,**values):
        return {'lines':TEXTS,'duration':100,'incremental':True,**values}

    def test_range_normalization_and_subtraction(self):
        self.assertEqual(normalize_ranges([[30,40],[0,10],[8,20],[20,25]],100),[[0,25],[30,40]])
        self.assertEqual(uncovered_ranges(5,50,[[0,10],[20,30],[40,90]]),[[10,20],[30,40]])
        self.assertEqual(uncovered_ranges(10,20,[[0,100]]),[])
        self.assertEqual(uncovered_ranges(0,100,[]),[[0,100]])

    def test_bad_coverage_is_rejected_instead_of_skipping_unverified_audio(self):
        for ranges in ([[False,10]],[[0,float('nan')]],[[0,101]],[[4,4]],[[5,2]],'0:5',[[0,10,12]]):
            with self.subTest(ranges=ranges),self.assertRaises(ValueError):normalize_ranges(ranges,100)
        for covered in ([[False,10]],[[0,float('inf')]],[[20,10]],None):
            with self.subTest(covered=covered),self.assertRaises(ValueError):uncovered_ranges(0,40,covered)

    def test_partial_prefix_never_forces_unheard_suffix_and_returns_global_rows(self):
        model=Model(TEXTS,[5,20,55,75],heard=[0,1])
        result=_align_with_model(model,self.request(),range(40*16000),0,Mock())
        self.assertEqual(model.aligned,[(20,TEXTS[:2])])
        self.assertEqual(result['times'],[5,20,None,None])
        self.assertEqual(result['analyzed_ranges'],[[0,40]])
        self.assertTrue(result['partial']);self.assertTrue(result['incremental'])
        self.assertEqual(result['method'],INCREMENTAL_METHOD)

    def test_second_play_skips_covered_prefix_and_preserves_confirmed_and_manual_rows(self):
        known=[5,20,None,75]
        model=Model(TEXTS,[5,20,55,75],clip_start=38,heard=[2,3])
        result=_align_with_model(model,self.request(confirmed_times=known,analyzed_ranges=[[0,40]]),
                                 range(100*16000),0,Mock())
        self.assertEqual(model.decoded,[62])
        self.assertEqual(model.aligned,[(25,TEXTS[2:])])
        self.assertEqual(result['times'],[None,None,55,None])
        self.assertEqual(known,[5,20,None,75])
        self.assertEqual(result['analyzed_ranges'],[[40,100]])

    def test_middle_excerpt_uses_actual_heard_span_with_absolute_clock(self):
        model=Model(TEXTS,[5,20,55,75],clip_start=45,heard=[2])
        result=_align_with_model(model,self.request(confirmed_times=[5,20,None,None]),
                                 range(20*16000),45,Mock())
        self.assertEqual(model.aligned,[(5,[TEXTS[2]])])
        self.assertEqual(result['times'],[None,None,55,None])
        self.assertEqual(result['analyzed_ranges'],[[45,65]])

    def test_internal_backing_call_is_optional_without_changing_stored_lyrics(self):
        originals=[TEXTS[0]+'（Oh Fu-Fu! Oh Yeah!）',*TEXTS[1:]]
        model=Model(TEXTS,[5,20,55,75],heard=[0])
        request=self.request(lines=originals)
        result=_align_with_model(model,request,range(40*16000),0,Mock())
        self.assertEqual(model.aligned,[(5,[TEXTS[0]])])
        self.assertEqual(result['times'],[5,None,None,None])
        self.assertEqual(request['lines'],originals)

    def test_reaching_track_end_does_not_force_unrecognized_suffix_into_anchor_audio(self):
        model=Model(TEXTS,[5,20,55,75],clip_start=45,heard=[2])
        result=_align_with_model(model,self.request(),range(55*16000),45,Mock())
        self.assertEqual(model.aligned,[(5,[TEXTS[2]])])
        self.assertEqual(result['times'],[None,None,55,None])

    def test_late_first_anchor_does_not_force_unrecognized_opening_into_audio(self):
        model=Model(TEXTS,[5,20,55,75],heard=[1])
        result=_align_with_model(model,self.request(),range(40*16000),0,Mock())
        self.assertEqual(model.aligned,[(5,[TEXTS[1]])])
        self.assertEqual(result['times'],[None,20,None,None])

    def test_distant_observations_use_separate_audio_and_lyric_windows(self):
        model=Model(TEXTS,[5,20,55,75],heard=[0,3])
        result=_align_with_model(model,self.request(),range(90*16000),0,Mock())
        self.assertEqual(model.aligned,[(5,[TEXTS[0]]),(5,[TEXTS[3]])])
        self.assertEqual(result['times'],[5,None,None,75])

    def test_one_failed_local_window_does_not_discard_a_later_match(self):
        model=Model(TEXTS,[5,20,55,75],heard=[0,3])
        align=model.align
        model.align=lambda samples,text,**kw:None if text==TEXTS[0] else align(samples,text,**kw)
        result=_align_with_model(model,self.request(),range(90*16000),0,Mock())
        self.assertEqual(result['times'],[None,None,None,75])

    def test_rejected_asr_segment_remains_a_boundary_between_good_fragments(self):
        from audio_alignment import _partial_evidence
        from lyrics import Line
        def segment(text,start,score):
            return SimpleNamespace(compression_ratio=1,avg_logprob=score,end=start+1,
                words=[SimpleNamespace(word=text,start=start,end=start+1,probability=.99)])
        model=Mock();model.transcribe_original.return_value=(iter([
            segment('朝の窓',1,-.1),segment('無関係な言葉',2,-2),segment('を開ける',3,-.1)]),None)
        evidence=_partial_evidence(model,[Line(TEXTS[0],'','')],range(10*16000),0,100,0,1,Mock())
        self.assertEqual(evidence,({},{},{}))

    def test_repeated_chorus_alone_never_identifies_an_occurrence(self):
        texts=[TEXTS[0],TEXTS[0],TEXTS[0]]
        model=Model(texts,[10,30,50],clip_start=20,heard=[1])
        result=_align_with_model(model,self.request(lines=texts),range(20*16000),20,Mock())
        self.assertEqual(result['times'],[None]*3)
        self.assertEqual(result['analyzed_ranges'],[]);self.assertEqual(model.aligned,[])

    def test_forced_alignment_disagreeing_with_independent_evidence_is_not_saved(self):
        model=Model(TEXTS,[5,20,55,75],heard=[0,1],shift=4)
        result=_align_with_model(model,self.request(),range(40*16000),0,Mock())
        self.assertEqual(result['times'],[None]*4);self.assertEqual(result['analyzed_ranges'],[])

    def test_new_candidate_cannot_cross_a_previous_manual_anchor(self):
        model=Model(TEXTS,[5,20,55,75],clip_start=40,heard=[2])
        result=_align_with_model(model,self.request(confirmed_times=[5,20,None,53]),
                                 range(30*16000),40,Mock())
        self.assertEqual(result['times'],[None]*4);self.assertEqual(result['analyzed_ranges'],[])

    def test_fully_covered_capture_launches_no_model_work(self):
        model=Mock()
        result=_align_with_model(model,self.request(analyzed_ranges=[[0,100]]),range(100*16000),0,Mock())
        self.assertEqual(result['times'],[None]*4)
        model.align.assert_not_called();model.transcribe_original.assert_not_called()

    def test_disjoint_uncovered_regions_only_decode_local_context(self):
        class WindowModel(Model):
            def transcribe_original(self,samples,**kwargs):
                self.clip_start=samples[0]/16000
                self.heard=[i for i,t in enumerate(self.positions)
                            if self.clip_start<=t<self.clip_start+len(samples)/16000]
                return super().transcribe_original(samples,**kwargs)
        model=WindowModel(TEXTS,[5,30,55,70])
        result=_align_with_model(model,self.request(confirmed_times=[5,None,55,None],
                                 analyzed_ranges=[[0,20],[40,60],[80,100]]),range(100*16000),0,Mock())
        self.assertEqual(model.decoded,[24,24])
        self.assertEqual(result['times'],[None,30,None,70])
        self.assertEqual(result['analyzed_ranges'],[[20,40],[60,80]])

    def test_malformed_prior_times_are_not_silently_treated_as_empty(self):
        for times in ([],None,[5,4,None,None],[5,False,None,None]):
            model=Mock()
            with self.subTest(times=times),self.assertRaises(ValueError):
                _align_with_model(model,self.request(confirmed_times=times),range(40*16000),0,Mock())
            model.align.assert_not_called();model.transcribe_original.assert_not_called()

    def test_first_full_capture_keeps_the_existing_fast_alignment(self):
        model=Model(TEXTS,[5,20,55,75])
        result=_align_with_model(model,self.request(),range(100*16000),0,Mock())
        self.assertEqual(result['times'],[5,20,55,75])
        self.assertEqual(result['analyzed_ranges'],[[0,100]])
        self.assertFalse(result['partial']);self.assertEqual(model.decoded,[])

    def test_full_alignment_failure_can_recover_only_independently_heard_phrases(self):
        for empty_result in (None,SimpleNamespace(to_dict=lambda:{'segments':[
                {'text':text,'words':[]} for text in TEXTS]})):
            with self.subTest(empty_result=empty_result):
                model=Model(TEXTS,[5,20,55,75],heard=[1])
                align=model.align
                model.align=lambda samples,text,**kw:empty_result if len(samples)==100*16000 else align(samples,text,**kw)
                updates=[]
                result=_align_with_model(model,self.request(),range(100*16000),0,
                                         lambda current,total:updates.append(current/total))
                self.assertEqual(result['times'],[None,20,None,None])
                self.assertTrue(result['partial'])
                self.assertEqual(updates,sorted(updates));self.assertEqual(updates[-1],1)


if __name__=='__main__':unittest.main()
