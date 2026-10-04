import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from lyrics import Line
from playlist_lyrics_job import PlaylistLyricsJob
from storage import atomic_json


TRACK = {'id': 'a'*22, 'title': 'Fixture song', 'artists': ['Fixture artist'],
         'album': 'Fixture album', 'duration': 200.0}


class RejectedPlaylistLyricsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.job = PlaylistLyricsJob(SimpleNamespace(store=SimpleNamespace(directory=self.directory)))
        self.worker = Mock()
        self.job.value = {'enabled': True, 'phase': 'spotify'}
        self.job.tracks = {TRACK['id']: TRACK}
        self.job.active = {'phase': 'spotify', 'track': dict(TRACK), 'request_id': 'request-one',
                           'job': self.worker, 'started': 0}
        self.path = self.directory/'playlist_lyrics'/'rejected_candidates'/(TRACK['id']+'.json')

    def event(self, **changes):
        return {'kind': 'batch_lyrics_result', 'request_id': 'request-one', 'provider_id': TRACK['id'],
                'source': 'Spotify', 'code': '',
                'lines': [{'text': '新しい朝', 'time': 1.25}, {'text': '未来へ', 'time': 4.75}], **changes}

    def reject_existing(self):
        self.assertTrue(self.job.store.save(TRACK, (Line('different existing lyric', 'reading', '기존 번역'),),
                                           None, sources=['existing_local_cache']))
        path = self.job.store.tracks_directory/(TRACK['id']+'.json')
        return path, path.read_bytes()

    def test_rejected_exact_recording_is_preserved_without_event_secrets_or_store_overwrite(self):
        path, original = self.reject_existing()
        self.job.active['track']['authorization'] = 'private-track-secret'
        event = self.event(authorization='private-bearer-secret', cookies='private-cookie-secret',
                           raw_response={'secret': 'private-response-secret'})
        event['lines'][0]['headers'] = {'secret': 'private-line-secret'}
        self.assertTrue(self.job.receive(self.worker, event))
        body = self.path.read_text(encoding='utf-8')
        candidate = json.loads(body)
        self.assertEqual(candidate['track'], TRACK)
        self.assertEqual(candidate['source'], 'Spotify')
        self.assertEqual(candidate['provider_id'], TRACK['id'])
        self.assertEqual(candidate['times'], [1.25, 4.75])
        self.assertEqual(candidate['lines'], [{'original': '新しい朝', 'reading': '', 'korean': ''},
                                               {'original': '未来へ', 'reading': '', 'korean': ''}])
        self.assertEqual(set(candidate), {'schema_version', 'track', 'source', 'provider_id',
                                         'lines', 'times', 'reason', 'rejected_at'})
        self.assertNotIn('private-', body)
        self.assertEqual(path.read_bytes(), original)
        diagnostic = self.job.value['invalid_lyrics_diagnostics'][TRACK['id']]
        self.assertEqual(diagnostic['stage'], 'store')
        self.assertTrue(self.job.value['rejected_candidate_diagnostics'][TRACK['id']]['candidate_saved'])
        self.assertEqual(self.job.value['errors'][TRACK['id']]['code'], 'invalid_lyrics')
        self.assertIsNone(self.job.active)

    def test_successful_receive_does_not_create_rejected_candidate(self):
        self.assertTrue(self.job.receive(self.worker, self.event()))
        self.assertFalse(self.path.exists())
        self.assertEqual(self.job.value['spotify'][TRACK['id']]['status'], 'received')

    def test_stale_sender_request_and_wrong_recording_do_not_create_candidates(self):
        self.reject_existing()
        self.assertFalse(self.job.receive(Mock(), self.event()))
        self.assertFalse(self.job.receive(self.worker, self.event(request_id='stale')))
        self.assertTrue(self.job.receive(self.worker, self.event(provider_id='b'*22)))
        self.assertFalse(self.path.exists())
        self.assertEqual(self.job.value['errors'][TRACK['id']]['code'], 'track_mismatch')

    def test_invalid_timing_is_not_preserved_as_a_valid_provider_candidate(self):
        self.reject_existing()
        self.assertTrue(self.job.receive(self.worker, self.event(lines=[{'text': 'bad time', 'time': 999.}])))
        self.assertFalse(self.path.exists())
        self.assertEqual(self.job.value['invalid_lyrics_diagnostics'][TRACK['id']]['stage'], 'times')

    def test_diagnostic_write_failure_still_records_error_and_releases_request(self):
        self.reject_existing()
        def write(path, value):
            if Path(path).parent.name == 'rejected_candidates':raise OSError('private-file-error')
            return atomic_json(path, value)
        with patch('playlist_lyrics_job.atomic_json', side_effect=write):
            self.assertTrue(self.job.receive(self.worker, self.event()))
        diagnostic = self.job.value['rejected_candidate_diagnostics'][TRACK['id']]
        self.assertFalse(diagnostic['candidate_saved'])
        self.assertEqual(diagnostic['candidate_error'], 'write_failed')
        self.assertEqual(self.job.value['errors'][TRACK['id']]['attempts'], 1)
        self.assertIsNone(self.job.active)
        self.assertNotIn('private-file-error', self.job.path.read_text(encoding='utf-8'))

    def test_unsafe_diagnostic_path_is_not_written(self):
        self.reject_existing()
        with patch('playlist_lyrics_job._safe', return_value=False):
            self.assertTrue(self.job.receive(self.worker, self.event()))
        self.assertFalse(self.path.exists())
        self.assertEqual(self.job.value['rejected_candidate_diagnostics'][TRACK['id']]['candidate_error'],
                         'unsafe_path')

    def test_oversized_lyric_row_is_not_preserved(self):
        self.reject_existing()
        self.assertTrue(self.job.receive(self.worker, self.event(lines=[{'text': 'x'*4001, 'time': 1.}])))
        self.assertFalse(self.path.exists())
        self.assertEqual(self.job.value['rejected_candidate_diagnostics'][TRACK['id']]['candidate_error'],
                         'invalid_candidate')


if __name__ == '__main__':
    unittest.main()
