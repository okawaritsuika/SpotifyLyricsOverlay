import queue
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from audio_alignment import align_lyrics
from auto_audio import AutoAnalyzer
from japanese_alignment import CTC, HYBRID, JapaneseRuntimeError, align_japanese, alignment_options, merge_review_results
from lyrics import Line, Song
from media import Playback
from storage import Store, read_json
from test_auto_audio import FakeJob


def row(phones=('a', 'i'), error=None):
    return SimpleNamespace(phones=phones, ctc_phones=phones, reading='あい', error=error)


def candidate(start, end, ambiguous=False):
    return SimpleNamespace(start_ms=start, end_ms=end, score=2.1, margin=.6, ambiguous=ambiguous)


class JapaneseAlignmentTests(unittest.TestCase):
    def test_absolute_times_preserve_saved_rows_and_skip_covered_regions(self):
        rows = [row(), row(), row(), row(error='unknown'), row()]
        ctc = Mock()
        ctc.align_lines.return_value = [None, candidate(2200, 3600), candidate(10000, 12200),
                                       None, candidate(13000, 14400, ambiguous=True)]
        model = Mock(provider='CPUExecutionProvider', fallback_reason=None)
        request = {'lines': ['前', '愛', '後', '読', '戻'], 'duration': 90, 'incremental': True,
                   'confirmed_times': [33.5, None, None, None, None], 'analyzed_ranges': [[32, 36]]}
        with patch.dict(sys.modules, romaji_ctc=ctc), patch('japanese_alignment.prepare_ctc',return_value=(model, rows)):
            result = align_japanese(request, range(16*16000), 32, Mock())
        self.assertEqual(result['times'], [None, None, 42, None, None])
        self.assertEqual(result['analyzed_ranges'], [[36, 48]])
        self.assertEqual(ctc.align_lines.call_args.kwargs['search_bounds'][1], (1501, 16000))
        self.assertEqual([detail['status'] for detail in result['line_alignment']],
                         ['retained', 'conflict', 'candidate', 'missing', 'conflict'])
        self.assertTrue(result['review_only'])

    def test_refines_short_clip_and_records_conflict_without_a_timestamp(self):
        model = Mock(provider='CPUExecutionProvider', fallback_reason=None)
        ctc = Mock()
        ctc.align_lines.return_value = [candidate(2200, 4000), candidate(6200, 8200)]
        pyshiro = Mock()
        refine = pyshiro.PyshiroRefiner.return_value.refine
        refine.side_effect = [candidate(2040, 4100), candidate(7000, 8500)]
        request = {'lines': ['愛', '後'], 'duration': 90, 'alignment_engine': HYBRID,
                   'pyshiro': {'python': 'python', 'checkpoint': 'v2.hsmm', 'phonemap': 'map.json'}}
        samples = range(16*16000)
        with patch.dict(sys.modules, romaji_ctc=ctc, pyshiro_refine=pyshiro), \
             patch('japanese_alignment.prepare_ctc',return_value=(model, [row(), row()])):
            result = align_japanese(request, samples, 15, Mock())
        self.assertEqual(result['times'], [17.04, None])
        self.assertEqual(result['line_alignment'][0]['status'], 'candidate')
        self.assertEqual(result['line_alignment'][0]['engine'], HYBRID)
        self.assertTrue(result['line_alignment'][0]['estimated'])
        self.assertEqual(result['line_alignment'][1]['reason'], 'ctc_pyshiro_disagreement')
        self.assertEqual(refine.call_args_list[0].args, (samples, ('a', 'i'), 600, 5600))
        self.assertEqual(refine.call_args_list[0].kwargs,
                         {'reading':'あい','start_limit_ms':600,'end_limit_ms':5600})
        pyshiro.PyshiroRefiner.return_value.close.assert_called_once()

    def test_pyshiro_failure_preserves_pure_ctc_candidates_without_new_inference(self):
        request = {'lines': ['愛', '後'], 'duration': 90, 'alignment_engine': HYBRID,
                   'pyshiro': {'python': 'python', 'checkpoint': 'v2.hsmm', 'phonemap': 'map.json'}}
        samples = range(16*16000)
        for failure_stage in ('prepare', 'inference'):
            with self.subTest(failure_stage=failure_stage):
                model = Mock(provider='CPUExecutionProvider', fallback_reason=None)
                ctc, pyshiro, emit = Mock(), Mock(), Mock()
                ctc.align_lines.return_value = [candidate(2200, 4000), candidate(6200, 8200)]
                refiner = pyshiro.PyshiroRefiner.return_value
                if failure_stage == 'prepare':
                    pyshiro.PyshiroRefiner.side_effect = RuntimeError('optional model not installed')
                else:
                    refiner.refine.side_effect = [candidate(2040, 4100), RuntimeError('worker stopped')]
                with patch.dict(sys.modules, romaji_ctc=ctc, pyshiro_refine=pyshiro), \
                     patch('japanese_alignment.prepare_ctc',return_value=(model, [row(), row()])):
                    result = align_japanese(request, samples, 15, Mock(), emit)
                self.assertEqual(result['times'], [17.2, 21.2])
                self.assertEqual(result['engine'], CTC)
                self.assertEqual(result['requested_engine'], HYBRID)
                self.assertTrue(result['review_only'])
                self.assertIn('RuntimeError', result['refinement_warning'])
                self.assertEqual([detail['engine'] for detail in result['line_alignment']], [CTC, CTC])
                self.assertNotIn('pyshiro_start', result['line_alignment'][0])
                self.assertIn('RomajiASR 단독', emit.call_args.kwargs['text'])
                ctc.align_lines.assert_called_once()
                model.emissions.assert_called_once_with(samples)
                if failure_stage == 'inference':
                    refiner.close.assert_called_once()

    def test_runtime_failure_falls_back_for_review_and_unmatched_result_does_not(self):
        with patch('japanese_alignment.align_japanese', side_effect=JapaneseRuntimeError('missing')), \
             patch('audio_alignment.align_stable_ts',return_value={'times': [8]}) as legacy:
            result = align_lyrics({'alignment_engine': HYBRID}, [], 0, Mock())
        legacy.assert_called_once()
        self.assertEqual(result['requested_engine'], HYBRID)
        self.assertTrue(result['review_only'])
        no_matches = {'times': [None], 'review_only': True}
        with patch('japanese_alignment.align_japanese',return_value=no_matches), \
             patch('audio_alignment.align_stable_ts') as legacy:
            self.assertEqual(align_lyrics({'alignment_engine': CTC}, [], 0, Mock()), no_matches)
        legacy.assert_not_called()

    def test_review_candidate_leaves_manual_and_provider_timing_intact(self):
        song = Song('sample','Song','Artist','Album',tuple(Line(value,'','') for value in ('愛','空','光','風')))
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            auto = AutoAnalyzer('.',store,queue.Queue(),job_factory=FakeJob,alignment={'engine': HYBRID})
            self.addCleanup(auto.close)
            auto.enabled = True
            playback = Playback(song.title,song.artist,song.album,0,90,True,time.monotonic())
            auto.observe(song,playback)
            job = next(iter(auto.jobs))
            self.assertEqual(job.options['alignment']['engine'],HYBRID)
            store.save_timing(song,{'times': [5,10,None,None], 'origins': ['manual','provider',None,None], 'offset': 0})
            timing = Path(directory)/'timings'/'sample.json'
            before = timing.read_bytes()
            auto.receive(job,{'kind':'result','result':{'times':[6,11,20,25],'method':HYBRID,
                         'engine':HYBRID,'review_only':True}},[song])
            self.assertEqual(timing.read_bytes(), before)
            self.assertEqual(read_json(auto.draft_path(song),{})['times'], [6,11,20,25])

    def test_default_options_leave_legacy_request_shape(self):
        self.assertEqual(alignment_options(None, '.'), {})
        options = alignment_options({'engine':HYBRID,'realtime':True},'runtime')
        self.assertTrue(options['romaji_realtime'])
        self.assertEqual(Path(options['pyshiro']['checkpoint']).name,'pyshiro-jp-v2.hsmm')

    def test_disjoint_recordings_accumulate_candidates_without_contradictory_repeats(self):
        previous = {'times':[5,10,None,None],'line_alignment':[{'start':5},{'start':10},{},{}]}
        result = {'duration':60,'analyzed_ranges':[[20,40]],'times':[None,21,26,31],
                  'line_alignment':[{}, {'start':21},{'start':26},{'start':31}]}
        merged = merge_review_results(previous,result,[[0,20]])
        self.assertEqual(merged['times'],[5,None,26,31])
        self.assertEqual(merged['analyzed_ranges'],[[0,40]])
        self.assertEqual(merged['line_alignment'][1]['status'],'conflict')

    def test_realtime_requires_repeat_observation_and_resets_after_seek(self):
        from realtime_audio import RomajiFollower
        request = {'lines':['愛','風','愛']}
        ctc = Mock()
        ctc.align_lines.side_effect = [[candidate(1000,2900),None,None],
                                      [None,candidate(1000,2900),None],
                                      [None,candidate(1000,2900),None],
                                      [None,candidate(1000,2900),None]]
        with patch('realtime_audio.prepare_ctc',return_value=(Mock(),[row(),row(('k','a')),row()])), \
             patch.dict(sys.modules,romaji_ctc=ctc):
            follower = RomajiFollower(request)
            self.assertIsNone(follower.analyze(range(48000),0))
            self.assertIsNone(follower.analyze(range(48000),1))
            self.assertEqual(follower.analyze(range(48000),2)['index'],1)
            self.assertIsNone(follower.analyze(range(48000),35))
            self.assertIsNone(follower.follower.index)


if __name__ == '__main__':
    unittest.main()
