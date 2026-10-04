import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from catalog_sync import CatalogSync
from catalog_link_queue import CatalogLinkQueue
from explorer_library import ExplorerLibrary
from tests.test_catalog_autolink import track


class CatalogSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.playlist = 'p' * 22
        (self.directory / 'spotify_catalog_sync.json').write_text(json.dumps(
            {'enabled': True, 'playlists': {self.playlist: {'offset': 0}}}))
        self.job = Mock()
        self.app = SimpleNamespace(store=SimpleNamespace(directory=self.directory), explorer_dialog=None,
                                   background_lyrics=SimpleNamespace(job=self.job))
        self.sync = CatalogSync(self.app)
        self.sync.active = {'request_id': 'test', 'playlist_id': self.playlist, 'job': self.job}

    def test_every_page_persists_progress_and_only_validated_public_fields(self):
        event = {'kind': 'catalog_playlist_progress', 'request_id': 'test', 'offset': 100, 'total': 203,
                 'tracks': [track(authorization='never persist', profile={'name': 'never persist'})]}
        self.assertTrue(self.sync.receive(self.job, event))
        state = json.loads(self.sync.path.read_text())
        self.assertEqual(state['playlists'][self.playlist]['offset'], 100)
        self.assertFalse(state['playlists'][self.playlist].get('complete', False))
        public = json.loads((self.directory / 'spotify_public_tracks.json').read_text())['tracks'][0]
        self.assertNotIn('authorization', public)
        self.assertNotIn('profile', public)
        self.assertEqual(public['source_url'], 'https://open.spotify.com/playlist/' + self.playlist)
        self.assertIsNotNone(self.sync.active)

    def test_final_page_and_restart_keep_completed_offset(self):
        self.sync.receive(self.job, {'kind': 'catalog_playlist_result', 'request_id': 'test',
            'offset': 203, 'total': 203, 'complete': True, 'tracks': [track()]})
        self.assertIsNone(self.sync.active)
        restarted = CatalogSync(self.app)
        self.assertEqual(restarted.value['playlists'][self.playlist],
                         {'offset': 203, 'total': 203, 'complete': True})

    def test_failed_page_is_never_counted_complete_and_stale_workers_cannot_write(self):
        self.assertFalse(self.sync.receive(Mock(), {'kind': 'catalog_playlist_result', 'request_id': 'test'}))
        self.sync.receive(self.job, {'kind': 'catalog_playlist_result', 'request_id': 'test',
            'offset': 100, 'total': 203, 'complete': False, 'tracks': [], 'code': 'rate_limited', 'retry_after': 600})
        state = self.sync.value['playlists'][self.playlist]
        self.assertEqual(state['offset'], 100)
        self.assertEqual(state['error'], 'rate_limited')
        self.assertFalse(state.get('complete', False))

    def test_batch_commit_preserves_current_manual_links_favorites_and_removed_links(self):
        library = ExplorerLibrary(self.directory)
        library.link_spotify('manual', track(id='m'*22))
        library.unlink_spotify('removed')
        library.toggle_favorite('favorite')
        self.assertEqual(library.link_automatic_many({name: track() for name in
            ('manual', 'removed', 'new', 'favorite')}), 2)
        saved = ExplorerLibrary(self.directory)
        self.assertEqual(saved.linked_track('manual')['id'], 'm'*22)
        self.assertIsNone(saved.linked_track('removed'))
        self.assertTrue(saved.is_favorite('favorite'))
        self.assertEqual(len(saved.value['spotify']), 3)

    def test_stale_search_error_cannot_cancel_a_new_playlist_request(self):
        self.sync.catalog = SimpleNamespace(songs=[])
        self.sync.people = []
        self.sync.pool_stamp = 0
        self.sync.links = CatalogLinkQueue(self.directory, [], ExplorerLibrary(self.directory))
        current = self.sync.active
        old = {'job': Mock(), 'title': 'Old song'}
        self.sync.events.put(('search_error', (old, 'response')))
        self.sync.tick()
        self.assertIs(self.sync.active, current)

    def test_over_limit_import_preserves_existing_readable_cache(self):
        self.sync.store_tracks([track()], '')
        path = self.directory / 'spotify_public_tracks.json'
        before = path.read_bytes()
        with patch('catalog_sync.MAX_PUBLIC_TRACKS', 1), self.assertRaises(ValueError):
            self.sync.store_tracks([track(id='b'*22)], '')
        self.assertEqual(path.read_bytes(), before)

    def test_partial_playlist_completion_cannot_be_marked_finished(self):
        self.sync.receive(self.job, {'kind': 'catalog_playlist_result', 'request_id': 'test',
            'offset': 25, 'total': 2938, 'complete': True, 'tracks': []})
        self.assertFalse(self.sync.value['playlists'][self.playlist].get('complete', False))


if __name__ == '__main__':unittest.main()
