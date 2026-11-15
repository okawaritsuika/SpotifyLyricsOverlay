from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import wave

from local_analysis import AnalysisStore, lyric_digest, worker_lock
from local_analysis_dialog import overlap_text, singer_text
from local_analysis_worker import decode_audio, import_manifest, playlist_clock, run_queue, segment_ranges
from storage import Store, atomic_json


class LocalAnalysisTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.audio = self.root / 'voice.wav'
        with wave.open(str(self.audio), 'wb') as wav:
            wav.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
            wav.writeframes(b'\0\0' * 24000)
        self.store = AnalysisStore(self.root / 'data')
        self.spec = {'kind': 'lyrics', 'recording': 'Title\0Artist\0Album',
                     'expected_duration': 3, 'lines': ['歌いましょう', 'また明日']}

    def test_hash_deduplication_keeps_versions_and_lyrics_separate(self):
        job = self.store.register(self.audio, self.spec)
        copy = self.root / 'copy.wav'
        copyfile(self.audio, copy)
        self.assertEqual(self.store.register(copy, self.spec), job)
        self.assertNotEqual(self.store.register(self.audio, {**self.spec, 'version': 'remix'}), job)
        self.assertIsNone(self.store.linked(self.spec['recording'], lyric_digest(['別の歌詞']), 3))
        self.assertIsNone(self.store.linked(self.spec['recording'], lyric_digest(self.spec['lines']), 6))
        self.assertEqual(len(self.store.pending()), 2)

    def test_alignment_reuses_hash_and_recipe_without_reusing_recording_length(self):
        from japanese_alignment import CTC, LEGACY

        first = self.store.register(self.audio, self.spec)
        alignment = {'engine': CTC, 'times': [0.25, 1.25], 'matched': 2}
        with patch('local_analysis_worker._align', return_value=alignment):
            self.assertEqual(run_queue(self.store, job_id=first, emit=lambda _: None), 0)
        second = self.store.register(self.audio, {**self.spec, 'version': 'copy'})
        mismatch = self.store.register(self.audio, {**self.spec, 'expected_duration': 6})
        with patch('local_analysis_worker.decode_audio') as decode, patch('local_analysis_worker._align') as align:
            self.assertEqual(run_queue(self.store, job_ids=[second, mismatch], emit=lambda _: None), 1)
            decode.assert_not_called()
            align.assert_not_called()
        result = self.store.get(second)['result']
        self.assertEqual(result['alignment'], alignment)
        self.assertEqual(result['alignment_reused_from'], first)
        self.assertIn('2초', self.store.get(mismatch)['error'])
        Store(self.store.directory).save_settings({'audio_alignment': {'engine': LEGACY}})
        changed = self.store.register(self.audio, self.spec)
        self.assertNotEqual(changed, first)
        self.assertIsNone(self.store.cached_alignment(self.store.get(changed)))

    def test_playlist_rejects_untagged_recordings_and_shifted_timelines(self):
        track = {'anchors': [2, 12, 22]}
        with patch('local_library.recording_tags_match', return_value=False):
            self.assertFalse(playlist_clock(self.audio, track, [2, 12, 22])['usable'])
        with patch('local_library.recording_tags_match', return_value=True):
            self.assertTrue(playlist_clock(self.audio, track, [2.4, 12.5, 22.6])['usable'])
            shifted = playlist_clock(self.audio, track, [3.5, 13.6, 23.7])
        self.assertFalse(shifted['usable'])
        self.assertEqual(shifted['median_offset'], 1.6)

    def test_retry_preserves_alignment_and_rereads_saved_results(self):
        spec = {**self.spec, 'kind': 'group', 'participants': ['a', 'd'], 'times': [0, 1.5]}
        job = self.store.register(self.audio, spec)
        with patch('local_analysis_worker._voice', side_effect=ValueError('model missing')), \
             patch('local_analysis_worker._align') as align:
            self.assertEqual(run_queue(self.store, emit=lambda _: None), 1)
            align.assert_not_called()
        before = self.store.get(job)
        self.assertEqual((before['status'], before['stage']), ('failed', 'aligned'))
        with patch('local_analysis_worker.decode_audio') as decode, \
             patch('local_analysis_worker._voice', return_value={'model': 'fixture', 'rows': []}):
            self.assertEqual(run_queue(self.store, job_id=job, retry=True, emit=lambda _: None), 0)
            decode.assert_not_called()
        after = AnalysisStore(self.store.directory).get(job)
        self.assertEqual(after['status'], 'done')
        self.assertEqual(after['result']['alignment'], before['result']['alignment'])
        self.assertEqual(after['result']['voice']['model'], 'fixture')

    def test_queue_lock_prevents_a_second_worker(self):
        with worker_lock(self.store.directory):
            with self.assertRaises(ValueError):
                with worker_lock(self.store.directory):
                    self.fail('second worker acquired lock')

    def test_real_wav_decode_and_segment_gaps(self):
        self.assertEqual(decode_audio(self.audio).shape, (48000,))
        self.assertEqual(segment_ranges([0, None, 6], [], 9), ([(6000, 9000)], [2]))
        self.assertEqual(segment_ranges([0, None, 6], [{'end': 1.5}], 9),
                         ([(0, 1500), (6000, 9000)], [0, 2]))

    def test_uncertain_voice_cannot_create_a_profile(self):
        spec = {'kind': 'solo', 'character': {'id': 'a', 'name': 'A'},
                'source': 'model estimate', 'evidence': 'estimated'}
        with self.assertRaises(ValueError):
            self.store.register(self.audio, spec)

    def test_manifest_can_pick_up_new_audio_and_skip_unchanged_files(self):
        manifest = self.root / 'inputs.json'
        target = self.root / 'later.wav'
        atomic_json(manifest, {'inputs': [{**self.spec, 'path': 'later.wav'}]})
        seen = {}
        self.assertEqual(list(import_manifest(self.store, manifest, seen))[0]['status'], 'audio_missing')
        copyfile(self.audio, target)
        self.assertEqual(list(import_manifest(self.store, manifest, seen))[0]['status'], 'registered')
        self.assertEqual(list(import_manifest(self.store, manifest, seen)), [])

    def test_profile_preserves_verified_exemplars_from_same_recording(self):
        spec = {'kind': 'solo', 'character': {'id': 'a', 'name': 'A'},
                'source': 'liner notes', 'evidence': 'official'}
        for start in (0, 1000):
            bounds = [start, start + 1000]
            job = self.store.get(self.store.register(self.audio, {**spec, 'ranges': [bounds]}))
            self.store.save_profile(job, 'fixture', [{'start_ms': start, 'end_ms': start + 1000}])
        profiles, _ = self.store.profiles(['a'], 'fixture')
        self.assertEqual([sample['start_ms'] for sample in profiles['a']], [0, 1000])
        digest = job['sha256']
        self.assertTrue(all(sample['audio_sha256'] == digest and sample['created_at'] for sample in profiles['a']))
        self.assertEqual(self.store.profiles(['a'], 'fixture', exclude_sha256=digest)[0], {'a': []})

    def test_singer_review_survives_analysis_changes_and_preserves_existing_parts(self):
        spec = {**self.spec, 'kind': 'group', 'participants': ['a', 'b'], 'times': [0, 1.5]}
        job = self.store.register(self.audio, spec)
        self.store.review_singers(job, 0, ['b'])
        self.store.review_singers(job, 1, [])
        with self.assertRaises(ValueError):
            self.store.review_singers(job, 0, ['unknown'])
        replacement = self.store.register(self.audio, {**spec, 'voice': {'singer_model': 'sony'}})
        reopened = AnalysisStore(self.store.directory).linked(spec['recording'], lyric_digest(spec['lines']), 3)
        self.assertEqual(reopened['id'], replacement)
        self.assertEqual(reopened['singer_reviews'], {'0': ['b'], '1': []})
        self.assertEqual(singer_text(reopened, 0), '사용자 확정 · b')
        self.assertEqual(singer_text(reopened, 1), '사용자 확정 · 무가창')
        self.assertEqual(singer_text(reopened, 0, ['기존 수동 파트']), '기존 수동 파트')

    def test_raw_top_score_is_not_displayed_as_an_attribution(self):
        job = {'spec': {'kind': 'group'}, 'status': 'done', 'result': {'voice': {'rows': [
            {'index': 0, 'scores': {'a': .62, 'b': .4}, 'singers': [], 'status': 'uncertain'}]}}}
        self.assertEqual(singer_text(job, 0), '')

    def test_review_export_uses_explicit_labels_only_and_preserves_timing(self):
        spec = {**self.spec, 'kind': 'group', 'participants': ['a', 'b'], 'times': [0, 1.5]}
        job_id = self.store.register(self.audio, spec)
        self.store.update(self.store.get(job_id), 'done', 'voice', {'alignment': {'times': [0, 1.5]},
                          'voice': {'rows': [{'index': 0, 'status': 'estimated', 'singers': ['a']}]}})
        self.store.review_singers(job_id, 1, ['b'])
        exported = self.store.reviewed_labels(job_id)
        self.assertEqual(exported['rows'], [{'index': 1, 'lyric': spec['lines'][1], 'start_s': 1.5, 'singers': ['b']}])
        self.assertEqual(overlap_text({'status': 'overlap', 'count': None, 'min_count': 2}), '2명 이상 추정')

    def test_profile_update_keeps_previous_voice_result(self):
        group = {**self.spec, 'kind': 'group', 'participants': ['a'], 'times': [0, 1.5]}
        job_id = self.store.register(self.audio, group)
        voice = {'model': 'fixture', 'rows': [{'index': 0, 'status': 'estimated', 'singers': ['a']}]}
        self.store.update(self.store.get(job_id), 'done', 'voice', {'voice': voice})
        spec = {'kind': 'solo', 'character': {'id': 'a', 'name': 'A'}, 'source': 'liner notes', 'evidence': 'official'}
        solo = self.store.get(self.store.register(self.audio, spec))
        self.store.save_profile(solo, 'fixture', [{'start_ms': 0, 'end_ms': 1000}])
        reopened = self.store.get(job_id)
        self.assertEqual(reopened['status'], 'queued')
        self.assertEqual(reopened['result']['voice_last_completed'], voice)
