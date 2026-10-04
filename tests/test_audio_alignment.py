import unittest
from unittest.mock import Mock,patch
from audio_alignment import line_candidates,recover_bounded_gaps
from types import SimpleNamespace
from lyrics import Line


def segment(text,start=10,probability=.8):
    return {'text':text,'words':[{'word':text,'start':start,'end':start+2,'probability':probability}]}


class AlignmentTests(unittest.TestCase):
    def test_gpu_alignment_failure_releases_model_and_retries_cpu_once(self):
        from audio_alignment import align_lyrics
        gpu=Mock();cpu=Mock();emit=Mock();expected={'times':[1],'matched':1}
        modules={'torch':SimpleNamespace(set_num_threads=Mock(),set_num_interop_threads=Mock())}
        with patch.dict('sys.modules',modules),patch('audio_runtime.load_final_model',side_effect=[
                (gpu,{'device':'cuda'}),(cpu,{'device':'cpu','model':'whisper-small'})]) as load, \
             patch('audio_alignment._align_with_model',side_effect=[RuntimeError('private payload'),expected]) as align:
            result=align_lyrics({'model':'local','final_device':'auto'},[0],0,Mock(),emit)
        self.assertIs(result,expected)
        gpu.model.unload_model.assert_called_once_with()
        self.assertEqual(load.call_count,2)
        self.assertEqual(load.call_args.args[0]['final_device'],'cpu')
        self.assertEqual(align.call_count,2)
        event=emit.call_args.kwargs
        self.assertTrue(event['restart']);self.assertEqual(event['runtime']['fallback_reason'],'cuda_inference_failed')
        self.assertNotIn('private',str(emit.call_args_list))

    def test_cpu_alignment_error_is_not_retried(self):
        from audio_alignment import align_lyrics
        modules={'torch':SimpleNamespace(set_num_threads=Mock(),set_num_interop_threads=Mock())}
        with patch.dict('sys.modules',modules),patch('audio_runtime.load_final_model',return_value=(Mock(),{'device':'cpu'})) as load, \
             patch('audio_alignment._align_with_model',side_effect=RuntimeError('failure')):
            with self.assertRaises(RuntimeError):align_lyrics({'model':'local'},[0],0,Mock())
        load.assert_called_once()

    def test_gap_recovery_requires_both_unchanged_anchors(self):
        lines=[Line(text,'','') for text in ('窓を開ける','花を描く','夜を歩く')]
        original=[{'start':10,'end':12},{'start':20,'end':22},{'start':30,'end':32}]
        baseline={'times':[10,None,30],'duration':50,'matched':2}
        for right,expected in [(21,[10,20,30]),(25,[10,None,30])]:
            local=[segment(line.original,stamp) for line,stamp in zip(lines,[1,11,right])]
            model=SimpleNamespace(align=lambda *args,**kw:SimpleNamespace(to_dict=lambda:{'segments':local}))
            result=recover_bounded_gaps(model,lines,[0]*800000,0,50,original,baseline)
            self.assertEqual(result['times'],expected)
            self.assertEqual(baseline['times'],[10,None,30])

    def test_gap_recovery_rejects_conflicting_onset_even_when_endpoints_agree(self):
        lines=[Line(text,'','') for text in ('窓を開ける','花を描く','夜を歩く')]
        original=[{'start':10,'end':12},{'start':20,'end':22},{'start':30,'end':32}]
        local=[segment(line.original,stamp) for line,stamp in zip(lines,[1,16,21])]
        model=SimpleNamespace(align=lambda *args,**kw:SimpleNamespace(to_dict=lambda:{'segments':local}))
        baseline={'times':[10,None,30],'duration':50,'matched':2}
        result=recover_bounded_gaps(model,lines,[0]*800000,0,50,original,baseline)
        self.assertEqual(result['times'],[10,None,30])
        self.assertEqual(result['recovered_indices'],[])

    def test_gap_recovery_never_invents_open_ended_or_large_missing_runs(self):
        lines=[Line('窓を開ける','','')]*10
        model=SimpleNamespace(align=lambda *args,**kw:self.fail('must not infer an unbounded gap'))
        baseline={'times':[None,10]+[None]*7+[45],'duration':50,'matched':2}
        self.assertEqual(recover_bounded_gaps(model,lines,[0]*800000,0,50,[{'end':48}]*10,baseline)['times'],baseline['times'])

    def test_supplied_lyrics_keep_absolute_playback_offset(self):
        lines=[Line('窓を開ける','',''),Line('花を描く','','')]
        result=line_candidates(lines,[segment(lines[0].original),segment(lines[1].original,20)],2,100)
        self.assertEqual(result['times'],[12,22])

    def test_wrong_grouping_or_text_is_rejected(self):
        lines=[Line('窓を開ける','','')]
        for segments in [[],[segment('無関係な言葉')]]:
            with self.assertRaises(ValueError):line_candidates(lines,segments,0,100)

    def test_collapsed_line_onset_is_never_guessed(self):
        value=segment('窓を開ける')
        value['words']=[{'word':'窓','start':10,'end':10,'probability':.8},
                        {'word':'を開ける','start':11,'end':13,'probability':.8}]
        self.assertEqual(line_candidates([Line(value['text'],'','')],[value],0,100)['times'],[None])

    def test_weak_durationless_or_outside_audio_lines_stay_empty(self):
        lines=[Line('窓を開ける','','')]
        for value in [segment(lines[0].original,probability=.1),segment(lines[0].original,start=101)]:
            self.assertEqual(line_candidates(lines,[value],0,100)['times'],[None])
        value=segment(lines[0].original);value['words'][0]['end']=10
        self.assertEqual(line_candidates(lines,[value],0,100)['times'],[None])

    def test_duplicate_onsets_cannot_overwrite_neighbor_order(self):
        lines=[Line('窓を開ける','','')]*2
        self.assertEqual(line_candidates(lines,[segment(lines[0].original)]*2,0,100)['times'],[10,None])


if __name__=='__main__':unittest.main()
