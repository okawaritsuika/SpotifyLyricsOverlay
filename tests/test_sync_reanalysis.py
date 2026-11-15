from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from lyrics import Line, Song
from sync_reanalysis import SyncReanalysis, applied_job, connect, enqueue_spotify_capture, run_batch


BASE = Path(__file__).resolve().parent.parent


def item(identifier, *, eligible=True):
    return {'id': identifier, 'kind': 'job', 'target': identifier, 'title': identifier,
            'lines': ['first', 'second', 'third'], 'protected': [True, False, False],
            'before': {'times': [1., None, 3.], 'engine': 'legacy'}, 'eligible': eligible,
            'reason': '' if eligible else 'missing audio', 'duration': 4,
            'auto': True, 'incomplete': True, 'outdated': True}


def candidate(times=None):
    return {'times': times or [1.2, 2., 3.1], 'details': [{'quality': 'high'}] * 3,
            'seconds': .5, 'engine': 'current', 'version': 2}


class SyncReanalysisTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.service = self.open_service()
        self.service.items = {key: item(key, eligible=key != 'missing') for key in ('one', 'two', 'three', 'missing')}
        launch = patch.object(SyncReanalysis, '_launch', autospec=True)
        launch.start()
        self.addCleanup(launch.stop)

    def open_service(self):
        return SyncReanalysis(self.directory, BASE, runtime=self.directory)

    def run_worker(self, analyze):
        with patch('sync_reanalysis_sources.analyze', side_effect=analyze) as called:
            run_batch(self.directory, self.directory)
        return [call.args[0]['id'] for call in called.call_args_list]

    def test_spotify_capture_uses_existing_worker_after_queued_mp3(self):
        self.service.start('selected', ('one','two'))
        song=Song('memory-song','Capture','Singer','Album',(Line('first','',''),Line('second','',''),Line('third','','')))
        track={'id':'s'*22,'title':'Capture','artists':['Singer'],'album':'Album'}
        with (patch('audio_capture_cache.read_capture',return_value={'start_position':0,'clip_tail':0}),
              patch('sync_reanalysis.subprocess.Popen') as launch):
            enqueue_spotify_capture(self.directory,BASE,track,song,'capture-key',self.directory,4)
            enqueue_spotify_capture(self.directory,BASE,track,song,'capture-key',self.directory,4)
        launch.assert_not_called()
        order=self.run_worker(lambda *_args,**_kwargs:candidate())
        self.assertEqual(order[:2],['one','two'])
        self.assertEqual(len(order),3)
        self.assertTrue(order[2].startswith('spotify-capture:'))
    def test_paused_then_cancelled_batch_resumes_pending_after_reopen(self):
        def pause_after_first(*_args, **_kwargs):
            self.service.pause()
            return candidate()

        self.assertEqual(self.service.preview('all'), {'target': 4, 'eligible': 3})
        self.service.start('all', force=True)
        self.assertEqual(self.run_worker(pause_after_first), ['one'])
        self.assertEqual(self.service.snapshot()['state'], 'paused')
        self.service.cancel()
        reopened = self.open_service()
        self.assertEqual(reopened.snapshot()['state'], 'cancelled')
        self.assertEqual(reopened.snapshot()['remaining'], 2)
        self.assertEqual(reopened.snapshot()['skipped'], 1)
        self.assertEqual(reopened.compare('one')['after']['timed'], 3)
        reopened.resume()
        self.assertEqual(self.run_worker(lambda *_args, **_kwargs: candidate()), ['two', 'three'])
        self.assertEqual(reopened.snapshot()['done'], 3)
        self.assertEqual(reopened.snapshot()['state'], 'finished')

    def test_reopen_recovers_a_crashed_running_row(self):
        self.service.start('all')
        with connect(self.directory, write=True) as db:
            db.execute("UPDATE queue SET status='running' WHERE id='one'")
        reopened = self.open_service()
        self.assertEqual(reopened.snapshot()['state'], 'paused')
        self.assertEqual(reopened.snapshot()['remaining'], 3)
        self.assertEqual(reopened.snapshot()['running'], '')
        reopened.resume()
        self.assertEqual(self.run_worker(lambda *_args, **_kwargs: candidate()), ['one', 'two', 'three'])

    def test_pause_during_last_song_finishes_and_later_scope_keeps_candidate(self):
        def pause_after_last(*_args, **_kwargs):
            self.service.pause()
            return candidate()

        self.service.start('selected', ('one',))
        self.assertEqual(self.run_worker(pause_after_last), ['one'])
        self.assertEqual(self.service.snapshot()['state'], 'finished')
        self.service.start('selected', ('two',))
        self.assertEqual(self.run_worker(lambda *_args, **_kwargs: candidate()), ['two'])
        self.assertEqual(self.service.snapshot()['done'], 1)
        self.assertIsNotNone(self.open_service().compare('one'))

    def test_retry_runs_failed_song_only_while_pending_songs_stay_queued(self):
        def corrupt_first(*_args, **_kwargs):
            self.service.pause()
            raise ValueError('corrupt audio')

        self.service.start('all')
        self.assertEqual(self.run_worker(corrupt_first), ['one'])
        self.assertEqual(self.service.snapshot()['failed'], 1)
        self.assertIn('corrupt audio', self.service.list_items()[0]['error'])
        self.assertEqual(self.service.retry_failed(), 1)
        self.assertEqual(self.run_worker(lambda *_args, **_kwargs: candidate()), ['one'])
        self.assertEqual(self.service.snapshot()['failed'], 0)
        self.assertEqual(self.service.snapshot()['remaining'], 2)

    def test_incomplete_mode_passes_retained_anchors_and_replaces_only_missing_rows(self):
        self.service.start('incomplete')

        def align(*_args, **options):
            self.assertEqual(options['anchors'], [1., None, 3.])
            return candidate([None, 2., None])

        self.assertEqual(self.run_worker(align), ['one', 'two', 'three'])
        self.assertEqual([row['after'] for row in self.service.compare('one')['rows']], [1., 2., 3.])

    def test_apply_selected_rows_preserves_manual_and_restores_original(self):
        self.service.start('selected', ('one', 'two'))
        self.run_worker(lambda *_args, **_kwargs: candidate())
        baseline = {'id': 'one', 'result': {'alignment': {'times': [1., None, 3.]}}}
        review = self.service.compare('one')
        self.assertTrue(review['improved'])
        self.assertTrue(review['rows'][0]['protected'])
        self.assertEqual(self.service.apply(ids=('one',), line_indices=(0, 1)), 1)
        self.assertEqual(applied_job(self.directory, baseline)['result']['alignment']['times'], [1., 2., 3.])
        self.assertEqual(baseline['result']['alignment']['times'], [1., None, 3.])
        self.assertEqual(self.service.apply(improvements_only=True), 2)
        self.assertEqual(applied_job(self.directory, baseline)['result']['alignment']['times'], [1., 2., 3.1])
        self.assertEqual(self.service.discard(('one',)), 1)
        self.assertIsNone(self.service.compare('one'))
        reopened = self.open_service()
        self.assertEqual(reopened.restore(), 2)
        self.assertEqual(applied_job(self.directory, baseline)['result']['alignment']['times'], [1., 2., 3.])
        self.assertEqual(reopened.restore(('one',)), 1)
        self.assertEqual(applied_job(self.directory, baseline), baseline)


if __name__ == '__main__':
    unittest.main()
