import copy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from realtime_audio import run


class RealtimeWorkerStateTests(unittest.TestCase):
    def run_windows(self,first_error=False,global_recovery=False):
        class Done(Exception):pass

        class Samples:
            def __len__(self):return 128000
            def __mul__(self,other):return self

        initial={'index':0,'pending':{'index':4,'evidence':{'end':6.8}},'last_window':7.0,'confirmed_end':6.8}
        if global_recovery:initial['pending'].update(scope='global',index=22,characters=7,confidence=.95)
        follower=SimpleNamespace(**copy.deepcopy(initial))
        before=[];events=[]

        class Aligner:
            def __init__(self):
                self.follower=follower
                self.acoustic_state={'follower':{'index':0,'pending':{'evidence':{'end':6.8}},
                                                 'last_window':7.0},'stalled':5,'search_step':2}
                self.acoustic_initial=copy.deepcopy(self.acoustic_state)
                self.acoustic_before=[];self.saved_acoustic=[];self.restored_acoustic=[]

            def acoustic_checkpoint(self):
                self.saved_acoustic.append(copy.deepcopy(self.acoustic_state))
                return copy.deepcopy(self.acoustic_state)

            def restore_acoustic(self,state):
                self.restored_acoustic.append(copy.deepcopy(state))
                self.acoustic_state=copy.deepcopy(state)

            def analyze(self,samples,start_position):
                before.append(copy.deepcopy(vars(self.follower)))
                self.acoustic_before.append(copy.deepcopy(self.acoustic_state))
                # A match four rows ahead must still be classified as recovery
                # if the preceding result never reached the UI.
                recovery=self.follower.index<3
                self.follower.pending['evidence']['end']=start_position+7.8
                self.follower.index=23 if global_recovery else 4
                self.follower.last_window=start_position+8
                self.follower.confirmed_end=start_position+7.8
                self.acoustic_state['follower']['index']=self.follower.index
                self.acoustic_state['follower']['pending']['evidence']['end']=start_position+7.8
                self.acoustic_state['follower']['last_window']=start_position+8
                self.acoustic_state['stalled']=0;self.acoustic_state['search_step']=3
                if first_error and len(before)==1:
                    raise RuntimeError('inference failed after changing state')
                result={'index':self.follower.index,'confidence':.9,'characters':9,'similarity':1.,
                        'unique':True,'recovery':recovery,'match_method':'kana',
                        'confirmations':2 if recovery else 1}
                if global_recovery:
                    result.update(global_recovery=True,distinct_lines=2,recovery_start_index=22,
                                  characters=6,recovery_characters=13)
                return result

        aligner=Aligner()
        next_observed=101 if first_error else 104
        windows=[{'sequence':1,'file':'live-1.wav','observed_at':100,
                  'start_position':0,'end_position':8},
                 {'sequence':2,'file':'live-0.wav','observed_at':next_observed,
                  'start_position':next_observed-100,'end_position':next_observed-92}]
        # Deadline, first-loop/pre-inference, then either the late result or
        # the next loop after an exception, followed by the fresh result.
        moments=([100,100,100,101,101,101.1] if first_error else
                 [100,100,100,104,104,104,104.1])
        samples=Samples()
        modules={'numpy':SimpleNamespace(sqrt=lambda value:value,mean=lambda value:1.),
                 'faster_whisper.audio':SimpleNamespace(decode_audio=lambda *args,**kwargs:samples)}
        runtime={'device':'cuda','model':'local-model','fallback':False}

        def emit(kind,**value):
            events.append({'kind':kind,**value})
            if kind=='realtime':raise Done()

        with patch.dict('sys.modules',modules),patch('analysis_limits.apply_analysis_limits'), \
             patch('audio_runtime.load_realtime_model',return_value=(object(),runtime)), \
             patch('realtime_audio.StreamingAligner',return_value=aligner), \
             patch('realtime_audio.ContextAligner',return_value=aligner), \
             patch('realtime_audio.read_json',side_effect=[windows[0],windows[0],windows[1],windows[1]]), \
             patch('realtime_audio.time.monotonic',side_effect=moments), \
             patch.object(Path,'read_bytes',return_value=b'synthetic'):
            with self.assertRaises(Done):
                run({'model':'unused','lines':['synthetic']*5,'duration':60,
                     'key':'track','realtime_device':'cuda'},'.',emit)
        return initial,before,events,aligner

    def test_late_recovery_rolls_back_before_the_next_window_is_confirmed(self):
        initial,before,events,aligner=self.run_windows()
        self.assertEqual(before,[initial,initial])
        self.assertTrue(any(event.get('state')=='late' for event in events))
        published=[event for event in events if event['kind']=='realtime']
        self.assertEqual(len(published),1)
        self.assertEqual(published[0]['observed_at'],104)
        self.assertEqual(published[0]['position'],12)
        self.assertTrue(published[0]['match']['recovery'])
        self.assertEqual(published[0]['match']['confirmations'],2)
        self.assertEqual(aligner.follower.index,4)
        self.assertEqual(aligner.follower.last_window,12)

    def test_failed_inference_rolls_back_mutated_pending_evidence_before_retry(self):
        initial,before,events,aligner=self.run_windows(first_error=True)
        self.assertEqual(before,[initial,initial])
        self.assertTrue(any(event.get('state')=='retrying' for event in events))
        self.assertFalse(any(event.get('state')=='late' for event in events))
        published=[event for event in events if event['kind']=='realtime']
        self.assertEqual(len(published),1)
        self.assertTrue(published[0]['match']['recovery'])
        self.assertEqual(published[0]['match']['confirmations'],2)
        self.assertEqual(aligner.follower.last_window,9)

    def test_late_global_recovery_restores_the_prior_distinct_phrase_anchor(self):
        initial,before,events,aligner=self.run_windows(global_recovery=True)
        self.assertEqual(before,[initial,initial])
        self.assertEqual(before[1]['pending']['scope'],'global')
        published=[event for event in events if event['kind']=='realtime']
        self.assertEqual(len(published),1)
        self.assertEqual(published[0]['match']['index'],23)
        self.assertTrue(published[0]['match']['global_recovery'])
        self.assertTrue(published[0]['match']['recovery'])
        self.assertEqual(published[0]['match']['confirmations'],2)
        self.assertEqual(published[0]['match']['recovery_start_index'],22)
        self.assertEqual(aligner.follower.index,23)

    def test_late_window_restores_both_acoustic_search_and_asr_evidence(self):
        initial,before,events,aligner=self.run_windows()
        self.assertEqual(before,[initial,initial])
        self.assertEqual(aligner.saved_acoustic,[aligner.acoustic_initial]*2)
        self.assertEqual(aligner.restored_acoustic,[aligner.acoustic_initial])
        self.assertEqual(aligner.acoustic_before,[aligner.acoustic_initial]*2)
        self.assertEqual(aligner.acoustic_state['follower']['index'],4)
        self.assertEqual(aligner.acoustic_state['follower']['last_window'],12)
        self.assertAlmostEqual(aligner.follower.confirmed_end,11.8)

    def test_decoder_error_restores_both_acoustic_search_and_asr_evidence(self):
        initial,before,events,aligner=self.run_windows(first_error=True)
        self.assertEqual(before,[initial,initial])
        self.assertEqual(aligner.saved_acoustic,[aligner.acoustic_initial]*2)
        self.assertEqual(aligner.restored_acoustic,[aligner.acoustic_initial])
        self.assertEqual(aligner.acoustic_before,[aligner.acoustic_initial]*2)
        self.assertEqual(aligner.acoustic_state['follower']['index'],4)
        self.assertEqual(aligner.acoustic_state['follower']['last_window'],9)
        self.assertAlmostEqual(aligner.follower.confirmed_end,8.8)

    def metadata_contention(self,second_read):
        class Done(Exception):pass

        class Samples:
            def __len__(self):return 128000
            def __mul__(self,other):return self

        windows=[{'sequence':index+1,'file':f'live-{index}.wav','observed_at':100,
                  'start_position':index,'end_position':index+8} for index in range(2)]
        reads=([windows[0],PermissionError('file being replaced'),windows[1],windows[1]]
               if second_read else [PermissionError('file being replaced'),windows[0],windows[0]])
        analyze=Mock(return_value={'index':0,'confidence':.9})
        aligner=SimpleNamespace(follower=SimpleNamespace(index=None,pending=None,last_window=-1,
                                                         confirmed_end=-1),analyze=analyze)
        decode=Mock(return_value=Samples())
        modules={'numpy':SimpleNamespace(sqrt=lambda value:value,mean=lambda value:1.),
                 'faster_whisper.audio':SimpleNamespace(decode_audio=decode)}
        events=[]

        def emit(kind,**value):
            events.append({'kind':kind,**value})
            if kind=='realtime':raise Done()

        with patch.dict('sys.modules',modules),patch('analysis_limits.apply_analysis_limits'), \
             patch('audio_runtime.load_realtime_model',return_value=(object(),{'device':'cuda'})), \
             patch('realtime_audio.ContextAligner',return_value=aligner), \
             patch('realtime_audio.read_json',side_effect=reads) as metadata, \
             patch('realtime_audio.time.monotonic',return_value=100), \
             patch('realtime_audio.time.sleep'),patch.object(Path,'read_bytes',return_value=b'synthetic'):
            with self.assertRaises(Done):
                run({'model':'unused','lines':['synthetic'],'duration':60,
                     'key':'track','realtime_device':'cuda'},'.',emit)
        published=[event for event in events if event['kind']=='realtime']
        self.assertEqual(len(published),1)
        self.assertEqual(published[0]['position'],9 if second_read else 8)
        self.assertEqual(published[0]['match']['index'],0)
        self.assertFalse(any(event.get('state') in ('failed','retrying') for event in events))
        analyze.assert_called_once();decode.assert_called_once()
        self.assertEqual(metadata.call_count,4 if second_read else 3)

    def test_locked_initial_metadata_read_skips_to_the_next_available_window(self):
        self.metadata_contention(second_read=False)

    def test_locked_sequence_verification_skips_that_audio_and_uses_the_next_window(self):
        self.metadata_contention(second_read=True)


if __name__=='__main__':unittest.main()
