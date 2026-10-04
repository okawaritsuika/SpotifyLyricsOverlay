from pathlib import Path
import json
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

from audio_worker import analyze, capture
from media import Playback


class CaptureTests(unittest.TestCase):
    def test_gpu_final_analysis_uses_new_limits_and_keeps_runtime_on_numeric_progress(self):
        import numpy as np
        runtime={'device':'cuda','model':'whisper-turbo','compute_type':'int8_float16','fallback':False}
        def alignment(request,samples,start,progress,emit):
            emit('progress',state='analyzing',runtime=runtime,text='GPU')
            progress(1,10)
            return {'matched':1,'times':[1]}
        decoder=SimpleNamespace(decode_audio=lambda *args,**kwargs:np.ones(16000,dtype=np.float32))
        with patch.dict(sys.modules,{'faster_whisper.audio':decoder}), \
             patch('analysis_limits.apply_analysis_limits') as limits, \
             patch('audio_worker.align_lyrics',side_effect=alignment),patch('audio_worker.emit') as emit:
            result=analyze({'realtime_device':'auto','final_cpu_percent':1},Path('unused.wav'),0)
        limits.assert_called_once_with(cpu_percent=20,memory_mb=4096,gpu=True)
        event=next(call.kwargs for call in emit.call_args_list if 'completed' in call.kwargs)
        self.assertEqual(event['runtime'],runtime)
        self.assertIn('GPU',event['text'])
        self.assertEqual(result['model'],'faster-whisper-turbo')
        self.assertEqual(result['runtime'],runtime)

    def test_analysis_reports_numeric_progress_without_loading_model(self):
        import numpy as np
        def alignment(request,samples,start,progress,**kwargs):
            for value in (np.float32(10),np.float32(30),np.float32(100)):
                progress(value,np.float32(100))
            return {'matched':1,'times':[1]}
        decoder=SimpleNamespace(decode_audio=lambda *args,**kwargs:np.ones(16000,dtype=np.float32))
        with patch.dict(sys.modules,{'faster_whisper.audio':decoder}), \
             patch('analysis_limits.apply_analysis_limits'), \
             patch('audio_worker.align_lyrics',side_effect=alignment), \
             patch('audio_worker.emit') as emit:
            result=analyze({},Path('unused.wav'),0)
        events=[call.kwargs for call in emit.call_args_list if 'completed' in call.kwargs]
        self.assertEqual([event['completed'] for event in events],[10.,30.,100.])
        self.assertTrue(all(event['state']=='analyzing' and event['total']==100 for event in events))
        json.dumps(events)
        self.assertEqual(result['captured_seconds'],1)

    def test_full_recording_preserves_partial_result_from_guided_recovery(self):
        import numpy as np
        decoder=SimpleNamespace(decode_audio=lambda *args,**kwargs:np.ones(16000,dtype=np.float32))
        with patch.dict(sys.modules,{'faster_whisper.audio':decoder}), \
             patch('analysis_limits.apply_analysis_limits'),patch('audio_worker.emit'), \
             patch('audio_worker.align_lyrics',return_value={'matched':1,'times':[.5],'partial':True}):
            result=analyze({'duration':1},Path('unused.wav'),0)
        self.assertTrue(result['partial'])

    def run_capture(self,change_at=None,seek_at=None,reset_at=None,startup_delay=0,start_position=0):
        clock=[float(start_position)]
        initial=Playback('Test','Artist','Album',0,20,True,0)
        class Stream:
            rate=8000;channels=1
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def start(self): clock[0]+=startup_delay
            def read(self,timeout=None):
                at=clock[0];clock[0]+=.256
                return b'\0'*(2048*2),2048,at
            def close(self): pass
        def snapshot(*args):
            now=clock[0]
            return Playback('Other' if change_at is not None and now>=change_at else 'Test',
                'Artist','Album',0 if reset_at is not None and now>=reset_at else now+(5 if seek_at is not None and now>=seek_at else 0),20,True,now)
        with tempfile.TemporaryDirectory() as directory, \
             patch('audio_worker.ProcessLoopback',Stream), \
             patch('audio_worker.SpotifyMonitor'), \
             patch('audio_worker.time.monotonic',side_effect=lambda:clock[0]), \
             patch('audio_worker.latest',side_effect=snapshot), \
             patch('audio_worker.emit') as emit:
            path=Path(directory)/'sample.wav'
            result=capture({'duration':20,'key':initial.key,'allow_partial':start_position>0},path)
            self.events=[call.kwargs for call in emit.call_args_list]
            with wave.open(str(path),'rb') as saved:
                seconds=saved.getnframes()/saved.getframerate()
            return result,seconds

    def test_normal_end_preserves_complete_audio(self):
        result,seconds=self.run_capture()
        self.assertEqual(result,{'start_position':0,'clip_tail':0,'capture_source':'spotify-process'})
        self.assertEqual(seconds,20)

    def test_capture_reports_numeric_duration_for_full_and_partial_audio(self):
        for start,expected_total in ((0,20),(12,8)):
            with self.subTest(start=start):
                self.run_capture(start_position=start)
                events=[e for e in self.events if e.get('state')=='recording']
                self.assertGreater(len(events),3)
                self.assertTrue(all(e['total']==expected_total for e in events))
                amounts=[e['completed'] for e in events]
                self.assertEqual(amounts,sorted(amounts))
                self.assertGreater(amounts[-1],expected_total-2)
                self.assertLessEqual(amounts[-1],expected_total)

    def test_memory_mid_song_capture_keeps_its_actual_media_position(self):
        result,seconds=self.run_capture(start_position=12)
        self.assertEqual(result['start_position'],12)
        self.assertEqual(seconds,8)

    def test_memory_request_receives_originals_only_from_stdin(self):
        import io,json
        from audio_worker import load_request
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'request.json';path.write_text('{"memory_lines":true}',encoding='utf-8')
            with patch('audio_worker.sys.stdin',io.StringIO(json.dumps(['例文です']))):
                self.assertEqual(load_request(path)['lines'],['例文です'])
            self.assertNotIn('例文です',path.read_text(encoding='utf-8'))
            with patch('audio_worker.sys.stdin',io.StringIO('{"invalid":"data"}')):
                with self.assertRaises(ValueError):load_request(path)

    def test_stream_startup_delay_does_not_shift_all_recorded_onsets(self):
        result,seconds=self.run_capture(startup_delay=.6)
        self.assertAlmostEqual(result['start_position'],.6)
        self.assertAlmostEqual(seconds,19.4,places=3)

    def test_early_next_title_at_song_end_keeps_prefix_for_analysis(self):
        result,seconds=self.run_capture(change_at=17)
        self.assertEqual(result['clip_tail'],1)
        self.assertGreaterEqual(seconds,17)
        self.assertLess(seconds,18)

    def test_short_accidental_track_switch_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'곡이 바뀌어'):
            self.run_capture(change_at=5)

    def test_seek_aborts_instead_of_creating_shifted_timestamps(self):
        with self.assertRaisesRegex(ValueError,'재생 위치가 바뀌어'):
            self.run_capture(seek_at=8)

    def test_end_position_reset_before_metadata_keeps_completed_prefix(self):
        result,seconds=self.run_capture(reset_at=19)
        self.assertEqual(result['clip_tail'],1)
        self.assertGreaterEqual(seconds,19)

    def test_mid_track_restart_still_aborts(self):
        with self.assertRaisesRegex(ValueError,'재생 위치가 바뀌어'):
            self.run_capture(reset_at=12)


if __name__=='__main__': unittest.main()
