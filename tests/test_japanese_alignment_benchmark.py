from argparse import Namespace
from importlib.util import find_spec
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

from tools.benchmark_japanese_alignment import load_case, measure, payload_for, run_case, spotify_reference, summarize
from tools.prepare_jsut_song_benchmark import transcribe


class JapaneseAlignmentBenchmarkTests(unittest.TestCase):
    @unittest.skipUnless(find_spec('faster_whisper'), 'optional audio runtime is unavailable')
    def test_local_wave_does_not_require_catalog_and_reference_stays_in_driver(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            with wave.open(str(root / 'clip.wav'), 'wb') as stream:
                stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                stream.writeframes(b'\0' * 32000)
            entry = {'song_id': 'local', 'audio': 'clip.wav', 'split': 'evaluation', 'lines': ['あ'],
                     'reference_source': 'hand labeled', 'reference_times': [0.1]}
            case = load_case(entry, root, None, {})
            self.assertEqual((case['start'], case['end'], case['reference']), (0, 1, [0.1]))
            self.assertEqual(len(case['recording_key']), 64)
            entry['reference_times'] = [1.1]
            with self.assertRaises(ValueError):
                load_case(entry, root, None, {})

    def test_jsut_hts_labels_yield_phrase_text_and_independent_reference_times(self):
        labels = '\n'.join(f'{onset} {onset + 1} x@a^b-{phone}+e=f' for onset, phone in
                           [(0, 'sil'), (200000, 'k'), (300000, 'a'), (400000, 'N'), (500000, 'br'),
                            (600000, 'ty'), (700000, 'u'), (800000, 'cl'), (900000, 'pau')])
        self.assertEqual(transcribe(labels), (['かん', 'てゅっ'], [0.02, 0.06]))

    def test_provider_split_uses_only_matching_line_boundaries(self):
        entry = {
            'track': {'id': 'recording'},
            'direct': {
                'source': 'Spotify',
                'provider_id': 'recording',
                'lines': [{'original': text} for text in ['花が', '咲く', '雨が降る']],
                'times': [1.0, 1.5, 4.0],
            },
        }
        self.assertEqual(spotify_reference(entry, ['花が咲く', '雨が', '降る']), [1.0, 4.0, None])
        with self.assertRaises(ValueError):
            spotify_reference(entry, ['花が咲く', '雪が降る'])
        entry['direct']['provider_id'] = 'alternate-recording'
        with self.assertRaises(ValueError):
            spotify_reference(entry, ['花が咲く', '雨が降る'])

    def test_missing_repeated_unsung_and_outside_clip_are_distinct(self):
        case = {'lines': ['雨が降る', '花が咲く', '歌う', '花が咲く', '足音', '名前'],
                'start': 2, 'end': 16,
                'reference': [1, 5, 8, 12, None, None], 'absent': {4}}
        result = measure(case, {'times': [None, 12.1, None, None, 14, 15]})
        self.assertEqual(result['reference_rows'], 3)
        self.assertEqual(result['missing_rows'], 2)
        self.assertEqual(result['raw']['mae_ms'], 7100)
        self.assertEqual(result['median_signed_offset_ms'], 7100)
        self.assertEqual(result['offset_corrected']['mae_ms'], 0)
        self.assertEqual(result['wrong_repeat_onsets'], 1)
        self.assertEqual(result['wrong_repeat_ratio'], .5)
        self.assertEqual(result['false_matches_on_absent'], 1)
        self.assertEqual(result['rows'][0]['reference_status'], 'outside_capture')
        self.assertEqual(result['rows'][-1]['reference_status'], 'unknown')

    def test_preferred_window_and_lateness_are_scored_separately_from_raw_onsets(self):
        case = {'lines': ['朝', '昼', '夜'], 'start': 0, 'end': 4,
                'reference': [.1, 1, 2], 'absent': set()}
        result = measure(case, {'times': [.05, 1.22, None]}, display_advance_ms=150)
        self.assertEqual([row['onset'] for row in result['rows']], [.05, 1.22, None])
        self.assertEqual([row['display_onset'] for row in result['rows']], [0, 1.07, None])
        self.assertEqual(result['raw_preference']['preferred_rows'], 1)
        self.assertEqual(result['display']['preferred_rows'], 2)
        self.assertEqual(result['display']['reference_in_preferred_range'], .6667)
        self.assertEqual(result['display']['late_ratio'], .5)
        self.assertEqual(result['display']['over_100ms_late_rows'], 0)
        self.assertEqual(result['failed_500ms'], 1)

    def test_worker_request_cannot_receive_reference_or_original_request_keys(self):
        case = {'audio': 'capture.wav', 'start': 1.0, 'tail': 0, 'duration': 32,
                'lines': ['花が咲く'], 'reference': [2.0], 'absent': {0},
                'confirmed_times': [3.0], 'reference_path': 'ground-truth.json'}
        args = Namespace(legacy_model=Path('.'), legacy_gpu_model=Path('.'), romaji_model=Path('.'),
                         legacy_device='cpu', romaji_device='cpu', pyshiro={})
        payload = payload_for(case, 'romaji_ctc', args)
        self.assertEqual(set(payload), {'audio', 'start', 'tail', 'request'})
        self.assertEqual(payload['request']['lines'], ['花が咲く'])
        self.assertTrue(case.keys().isdisjoint(payload['request'].keys() - {'lines', 'duration'}))

    def test_common_rows_do_not_compare_different_engine_coverage(self):
        case = {'lines': ['花が咲く', '雨が降る'], 'start': 0, 'end': 30,
                'reference': [1.0, 4.0], 'absent': set()}
        results = []
        for engine, times in [('stable_ts_legacy', [1.2, 4.5]), ('romaji_ctc', [1.1, None])]:
            results.append({'split': 'evaluation', 'engine': engine, 'id': 'track',
                            **measure(case, {'times': times})})
        summary = summarize(results, ['stable_ts_legacy', 'romaji_ctc'])['evaluation']
        self.assertEqual(summary['stable_ts_legacy']['raw']['count'], 2)
        self.assertEqual(summary['stable_ts_legacy']['common_matched_raw']['count'], 1)
        self.assertEqual(summary['stable_ts_legacy']['common_matched_raw']['mae_ms'], 200)
        self.assertEqual(summary['stable_ts_legacy']['pairwise_common_matched_raw']['romaji_ctc']['count'], 1)
        self.assertEqual(summary['romaji_ctc']['reference_within_500ms'], .5)

    def test_fallback_result_cannot_be_scored_as_requested_hybrid_engine(self):
        case = dict.fromkeys(('id', 'title', 'split', 'audio', 'duration', 'start',
                             'end', 'tail', 'reference_source', 'reference_path'))
        actual = {'engine': 'romaji_ctc', 'refinement_warning': 'pyshiro failed'}
        process = SimpleNamespace(returncode=0, stdout=json.dumps(actual))
        with patch('tools.benchmark_japanese_alignment.payload_for', return_value={}), \
             patch('tools.benchmark_japanese_alignment.subprocess.run', return_value=process):
            run = run_case(case, 'hybrid_japanese', Namespace(timeout=10))
        self.assertEqual(run['engine'], 'hybrid_japanese')
        self.assertEqual(run['actual_engine'], 'romaji_ctc')
        self.assertEqual(run['error'], 'pyshiro failed')


if __name__ == '__main__':
    unittest.main()
