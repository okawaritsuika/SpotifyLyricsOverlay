"""Catalog search shares the existing Spotify window without disrupting lyrics."""
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from background_lyrics import BackgroundLyrics, CatalogSearchError
from spotify_worker import SpotifyBrowser, catalog_tracks


TRACK = 'a' * 22


def payload(**extra):
    return {'data': {'searchV2': {'tracksV2': {'items': [{'item': {'data': {
        '__typename': 'Track', 'uri': 'spotify:track:' + TRACK, 'name': 'Test (Solo Ver.)',
        'artists': {'items': [{'profile': {'name': 'Artist'}}]},
        'albumOfTrack': {'name': 'Album'}, 'duration': {'totalMilliseconds': 240000}, **extra,
    }}}]}}}}


class BrowserCatalogTests(unittest.TestCase):
    def setUp(self):
        self.window = Mock()
        self.window.get_current_url.return_value = 'https://open.spotify.com/'
        self.browser = SpotifyBrowser(self.window, queue.Queue(), threading.Event())
        navigation=patch.object(self.browser,'navigate_search',side_effect=self.window.load_url)
        navigation.start();self.addCleanup(navigation.stop)
        self.browser.authenticated = True
        self.request = {'action': 'catalog_search', 'request_id': 'request-1', 'title': 'Test'}
        self.old = {'action': 'lookup', 'key': 'playing', 'title': 'Existing', 'artist': 'Artist',
                    'album': 'Album', 'duration': 240}

    def test_catalog_preserves_lyric_request_and_never_fetches_lyrics_or_credentials(self):
        self.browser.pending = self.old
        with patch('spotify_worker.emit') as emit, patch('spotify_worker.fetch_lyrics') as fetch:
            self.browser.command(self.request)
            self.browser.receive(('search', 'Test', payload(playability={'playable': True})))
        fetch.assert_not_called()
        self.assertIs(self.browser.pending, self.old)
        self.assertEqual(self.browser.generation, 0)
        self.assertEqual(emit.call_count, 1)
        self.assertEqual(emit.call_args.args, ('catalog_result',))
        self.assertEqual(emit.call_args.kwargs['request_id'], 'request-1')
        self.assertEqual(emit.call_args.kwargs['tracks'][0]['title'], 'Test (Solo Ver.)')
        self.assertNotIn('authorization', emit.call_args.kwargs)
        self.window.show.assert_not_called()

    def test_catalog_waits_for_pending_lyric_search_and_fetch(self):
        self.browser.pending = self.old
        self.browser.active_query = 'Existing Artist'
        self.browser.search_deadline = time.monotonic() + 10
        self.browser.command(self.request)
        self.window.load_url.assert_not_called()
        self.assertIsNotNone(self.browser.catalog_pending)
        self.browser.search_deadline = 0;self.browser.active_query = '';self.browser.fetching = True
        self.browser.tick_catalog()
        self.window.load_url.assert_not_called()
        self.browser.fetching = False;self.browser.tick_catalog()
        self.window.load_url.assert_called_once_with('https://open.spotify.com/search/Test')
        self.assertIs(self.browser.pending, self.old)

    def test_new_playback_lookup_interrupts_catalog_and_gets_priority(self):
        self.browser.command(self.request)
        with patch('spotify_worker.emit') as emit:
            self.browser.command(self.old)
        result = next(call for call in emit.call_args_list if call.args == ('catalog_result',))
        self.assertEqual(result.kwargs['code'], 'interrupted')
        self.assertIsNone(self.browser.catalog_active)
        self.assertEqual(self.browser.active_query, 'Existing Artist')
        self.assertEqual(self.browser.pending, self.old)
        with patch('spotify_worker.emit') as emit:
            self.browser.receive(('search', 'Test', payload()))
        emit.assert_not_called()

    def test_new_lyric_lookup_interrupts_queued_catalog_too(self):
        self.browser.fetching = True;self.browser.command(self.request)
        with patch('spotify_worker.emit') as emit:self.browser.command(self.old)
        result = next(call for call in emit.call_args_list if call.args == ('catalog_result',))
        self.assertEqual(result.kwargs['code'], 'interrupted')
        self.assertIsNone(self.browser.catalog_pending)

    def test_catalog_not_logged_in_asks_for_existing_connection(self):
        self.browser.authenticated = False
        with patch('spotify_worker.emit') as emit:self.browser.command(self.request)
        self.assertEqual(emit.call_args.kwargs['code'], 'login_required')
        self.window.load_url.assert_not_called()
        self.window.show.assert_not_called()

    def test_catalog_search_timeout_retains_original_pending_and_status(self):
        self.browser.pending = self.old
        self.browser.last_status = ('status', 'ready', 'Lyrics ready', True, 'playing')
        previous = self.browser.last_status
        self.browser.command(self.request)
        with patch('spotify_worker.emit') as emit:
            self.browser.tick_catalog(self.browser.catalog_deadline + 1)
        self.assertEqual(emit.call_args.kwargs['code'], 'timeout')
        self.assertIs(self.browser.pending, self.old)
        self.assertEqual(self.browser.last_status, previous)

    def test_matching_cancel_stops_only_its_catalog_request(self):
        self.browser.command(self.request)
        with patch('spotify_worker.emit') as emit:
            self.browser.command({'action': 'catalog_cancel', 'request_id': 'other'})
            emit.assert_not_called()
            self.browser.command({'action': 'catalog_cancel', 'request_id': 'request-1'})
        self.assertEqual(emit.call_args.kwargs['code'], 'cancelled')
        self.assertIsNone(self.browser.catalog_active)

    def test_catalog_search_response_error_does_not_become_lyric_error(self):
        for status,code in ((429,'rate_limited'),(500,'response')):
            with self.subTest(status=status):
                self.browser.command(self.request)
                with patch('spotify_worker.emit') as emit:
                    self.browser.receive(('response_error', 'search', 'Test', status))
                self.assertEqual(emit.call_args.args, ('catalog_result',))
                self.assertEqual(emit.call_args.kwargs['code'], code)

    def test_parallel_catalog_request_gets_busy_without_replacing_first(self):
        self.browser.command(self.request)
        with patch('spotify_worker.emit') as emit:
            self.browser.command({**self.request, 'request_id': 'request-2'})
        self.assertEqual(emit.call_args.kwargs['request_id'], 'request-2')
        self.assertEqual(emit.call_args.kwargs['code'], 'busy')
        self.assertEqual(self.browser.catalog_active['request_id'], 'request-1')

    def test_catalog_availability_only_comes_from_explicit_booleans(self):
        self.assertIsNone(catalog_tracks(payload())[0]['is_playable'])
        self.assertIsNone(catalog_tracks(payload(playability={'playable': 'true'}))[0]['is_playable'])
        self.assertTrue(catalog_tracks(payload(playability={'playable': True}))[0]['is_playable'])
        blocked = catalog_tracks(payload(playability={'playable': False, 'reason': 'MARKET_RESTRICTED'}))[0]
        self.assertFalse(blocked['is_playable'])
        self.assertEqual(blocked['restriction'], 'MARKET_RESTRICTED')
        self.assertTrue(catalog_tracks(payload(isPlayable=True))[0]['is_playable'])

    def test_catalog_repeated_search_reloads_hidden_web_player(self):
        self.window.get_current_url.return_value = 'https://open.spotify.com/search/Test'
        with patch.object(self.browser, 'reload') as reload:self.browser.command(self.request)
        reload.assert_called_once()
        self.window.load_url.assert_not_called()


class BackgroundCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.service = BackgroundLyrics('.', self.temp.name, queue.Queue())
        self.job = Mock()
        self.service.job = self.job;self.service.spotify_authenticated = True

    def emit_result(self, command, **values):
        self.service.receive(self.job, {'kind': 'catalog_result', 'request_id': command['request_id'],
                                       'tracks': [{'id': TRACK}], 'error': '', 'code': '', **values})

    def test_catalog_result_is_routed_before_lyric_enabled_or_playback_checks(self):
        self.service.enabled = False
        self.service.spotify_state = 'ready';self.service.spotify_status = 'Original status'
        self.job.send.side_effect = lambda command: self.emit_result(command)
        self.assertEqual(self.service.catalog_search('Test', 'Artist', 'Album'), [{'id': TRACK}])
        command = self.job.send.call_args.args[0]
        self.assertEqual(command['action'], 'catalog_search')
        self.assertEqual(command['title'], 'Test')
        self.assertNotIn('artist', command)
        self.assertEqual(self.service.spotify_status, 'Original status')
        self.assertEqual(self.service.spotify_state, 'ready')
        self.assertEqual(self.service._catalog_waiters, {})

    def test_wrong_request_or_worker_cannot_satisfy_waiter(self):
        def send(command):
            event = {'kind': 'catalog_result', 'request_id': command['request_id'], 'tracks': ['wrong']}
            self.service.receive(Mock(), event)
            self.service.receive(self.job, {**event, 'request_id': 'other'})
            self.emit_result(command)
        self.job.send.side_effect = send
        self.assertEqual(self.service.catalog_search('Test'), [{'id': TRACK}])

    def test_interrupted_search_is_an_error_not_empty_catalog(self):
        self.job.send.side_effect = lambda command:self.emit_result(command, error='Lyrics first', code='interrupted')
        with self.assertRaises(CatalogSearchError) as raised:self.service.catalog_search('Test')
        self.assertEqual(raised.exception.code, 'interrupted')
        self.assertEqual(self.service._catalog_waiters, {})

    def test_wait_timeout_sends_cancel_and_cleans_pending_request(self):
        with patch('background_lyrics.CATALOG_TIMEOUT', .01):
            with self.assertRaises(CatalogSearchError) as raised:self.service.catalog_search('Test')
        self.assertEqual(raised.exception.code, 'timeout')
        self.assertEqual(self.job.send.call_args.args[0]['action'], 'catalog_cancel')
        self.assertEqual(self.service._catalog_waiters, {})

    def test_cancellation_before_request_does_not_search(self):
        cancel = threading.Event();cancel.set()
        with self.assertRaises(CatalogSearchError) as raised:self.service.catalog_search('Test', cancel=cancel)
        self.assertEqual(raised.exception.code, 'cancelled')
        self.assertFalse(any(call.args[0]['action'] == 'catalog_search' for call in self.job.send.call_args_list))

    def test_cancellation_after_response_prevents_stale_results(self):
        cancel = threading.Event()
        def send(command):
            self.emit_result(command);cancel.set()
        self.job.send.side_effect = send
        with self.assertRaises(CatalogSearchError) as raised:self.service.catalog_search('Test', cancel=cancel)
        self.assertEqual(raised.exception.code, 'cancelled')

    def test_worker_exit_releases_waiter_without_waiting_for_deadline(self):
        self.job.send.side_effect = lambda _:self.service.receive(self.job, {'kind': 'finished', 'exit_code': 1})
        with self.assertRaises(CatalogSearchError) as raised:self.service.catalog_search('Test')
        self.assertEqual(raised.exception.code, 'interrupted')
        self.assertIsNone(self.service.job)

    def test_missing_login_never_launches_a_browser_implicitly(self):
        self.service.spotify_authenticated = False
        with self.assertRaises(CatalogSearchError) as raised:self.service.catalog_search('Test')
        self.assertEqual(raised.exception.code, 'login_required')
        self.job.send.assert_not_called()


if __name__ == '__main__':unittest.main()
