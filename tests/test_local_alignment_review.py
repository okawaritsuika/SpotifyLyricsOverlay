from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
import wave

from japanese_alignment import CTC, HYBRID, LEGACY
from local_analysis import AnalysisStore, automatic_alignment_engine, local_alignment_config
from local_analysis_dialog import protected_candidates
from local_analysis_worker import _align, reanalyze_alignment, review_windows


class LocalAlignmentReviewTests(unittest.TestCase):
    def test_engine_defaults_follow_lyric_language_and_explicit_choice(self):
        self.assertEqual(automatic_alignment_engine(['風が吹く', '進もう']), CTC)
        self.assertEqual(automatic_alignment_engine(['A new day', '오늘도 노래해']), LEGACY)
        self.assertEqual(automatic_alignment_engine(['白日', '夢幻']), LEGACY)
        singing = {'kind': 'lyrics', 'lines': ['歌おう'], 'alignment': {'engine': 'auto'}}
        global_legacy = {'audio_alignment': {'engine': LEGACY}}
        self.assertEqual(local_alignment_config(global_legacy, singing)['engine'], CTC)
        self.assertEqual(local_alignment_config({}, {**singing, 'kind': 'speech'})['engine'], LEGACY)
        self.assertEqual(local_alignment_config({}, {**singing, 'alignment': {'engine': HYBRID}})['engine'], HYBRID)

    def test_requested_and_actual_engine_are_both_saved(self):
        from audio_alignment import AlignmentUnmatched

        def fallback(request, samples, start, progress, emit):
            self.assertEqual(request['alignment_language'], 'ja')
            self.assertEqual(request['line_readings'], ['うたおう'])
            emit('progress', fallback_reason='model missing')
            emit('progress', runtime={'device': 'cpu'})
            raise AlignmentUnmatched('no acoustic match')

        spec = {'kind': 'lyrics', 'lines': ['歌おう'], 'line_readings': ['うたおう']}
        with patch('audio_alignment.align_lyrics', side_effect=fallback):
            result = _align([], spec, 4, {}, Path('.'))
        self.assertEqual((result['requested_engine'], result['engine']), (CTC, LEGACY))
        self.assertEqual(result['fallback_reason'], 'model missing')
        self.assertEqual(result['runtime'], {'device': 'cpu'})
        self.assertEqual(result['line_alignment'][0]['quality'], 'low')

    def test_stable_ts_detects_language_for_untagged_non_japanese_lyrics(self):
        from audio_alignment import _align_with_model

        model = Mock()
        model.detect_language.return_value = ('en', .9, [])
        samples = [0] * 32000
        request = {'alignment_language': None, 'duration': 2, 'lines': ['Hold the line']}
        with patch('audio_alignment._align_full', return_value={'matched': 1}) as align:
            _align_with_model(model, request, samples, 0, Mock())
        self.assertEqual(align.call_args.args[1]['alignment_language'], 'en')

    def test_existing_provider_manual_and_unmapped_boundaries_are_protected(self):
        self.assertEqual(protected_candidates([1, 2, 4, 7, 9], [1, None, None, 8, None],
                                              [True, False, False, True, False], [(1.5, 3)], 10),
                         [None, 2, 4, None, 9])
        self.assertEqual(protected_candidates([1, 4, 4.5, 5], [1, None, None, 6],
                                              [True, False, False, True], [(1.5, 3)], 6),
                         [None, None, 4.5, None])

    def test_bounded_reanalysis_changes_only_separate_candidate_rows(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'recording.wav'
            with wave.open(str(path), 'wb') as wav:
                wav.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
                wav.writeframes(b'\0\0' * 32000)
            store = AnalysisStore(root)
            spec = {'kind': 'lyrics', 'lines': ['歌おう', '聞こえる', 'また会おう', '君を待つ'],
                    'line_readings': ['うたおう', 'きこえる', 'またあおう', 'きみをまつ']}
            job = store.get(store.register(path, spec))
            alignment = {'engine': CTC, 'requested_engine': CTC, 'times': [.1, 1.1, 2.1, 3.1],
                         'line_alignment': [{'quality': q} for q in ('high', 'medium', 'low', 'high')]}
            self.assertEqual(review_windows(alignment, 4), [(1, 3, 1.1, 3.1)])
            store.update(job, 'done', 'aligned', {'duration': 4, 'alignment': alignment})

            def fake_align(samples, clip_spec, duration, settings, runtime, **context):
                self.assertEqual(len(samples), 32000)
                self.assertEqual(clip_spec['lines'], spec['lines'][1:])
                self.assertEqual(clip_spec['line_readings'], spec['line_readings'][1:])
                self.assertEqual(context['confirmed_times'], [1.1, None, 3.1])
                self.assertEqual(context['capture_window'], [1.1, 3.1])
                return {'engine': CTC, 'times': [None, 2.4, None],
                        'line_alignment': [{}, {'quality': 'medium'}, {}]}

            with patch('local_analysis_worker._align', side_effect=fake_align) as align:
                result = reanalyze_alignment(store, store.get(job['id']), {}, root)['result']
            align.assert_called_once()
            self.assertEqual(result['alignment'], alignment)
            self.assertEqual(result['alignment_review']['times'], [None, None, 2.4, None])
            self.assertEqual(result['alignment_review']['attempted'], [2])
            self.assertEqual(AnalysisStore(root).get(job['id'])['result'], result)

            with patch('local_analysis_worker._align', return_value={
                'engine': CTC, 'romaji_profile': 'recovery', 'times': [None, 2.5, None],
                'line_alignment': [{}, {'quality': 'low', 'recovered': True}, {}],
            }) as align:
                result = reanalyze_alignment(store, store.get(job['id']), {}, root,
                                             recover_missing=True)['result']
            self.assertEqual(align.call_args.args[1]['alignment']['profile'], 'recovery')
            self.assertEqual(result['alignment'], alignment)
            self.assertEqual(result['alignment_review']['times'], [None, None, 2.5, None])
