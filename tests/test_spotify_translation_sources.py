import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from lyrics import Line
from playlist_lyrics_store import PlaylistLyricsStore
from spotify_translation_sources import SpotifyTranslationSources, _public_track
from storage import Store


TRACK = 'A' * 22
OTHER = 'B' * 22
URL = 'https://open.spotify.com/track/' + TRACK


def track(track_id=TRACK):
    return {'id': track_id, 'title': 'Test Song', 'artists': ['Singer'], 'album': 'Album', 'duration': 100}


class InlineThread:
    def __init__(self, target, args, **kwargs):
        self.target = target
        self.args = args

    def start(self):
        self.target(*self.args)


class SpotifyTranslationSourceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.worker = Mock()
        self.store = PlaylistLyricsStore(directory.name)
        self.app = SimpleNamespace(store=Store(directory.name),
            playlist_lyrics=SimpleNamespace(store=self.store, active=None),
            background_lyrics=SimpleNamespace(job=self.worker, spotify_authenticated=True,
                                               spotify_pending=False, metadata_pending=False),
            catalog_sync=SimpleNamespace(active=None), missing_audio_collector=None)
        self.source = SpotifyTranslationSources(self.app)
        thread = patch('spotify_translation_sources.threading.Thread', InlineThread)
        self.addCleanup(thread.stop)
        thread.start()
        fetch = patch('spotify_translation_sources._public_track', side_effect=track)
        self.addCleanup(fetch.stop)
        self.fetch = fetch.start()

    def prepare(self, links=URL):
        count = self.source.start(links)
        self.source.tick()
        self.source.tick()
        return count

    def event(self, **values):
        command = self.worker.send.call_args.args[0]
        return {'kind': 'batch_lyrics_result', 'provider_id': command['track']['id'],
                'request_id': command['request_id'], 'code': '',
                'lines': [{'text': '夜空を見上げる', 'time': 10}, {'text': '星がきれい', 'time': 20}], **values}

    def test_link_deduplication_and_exact_id_storage_round_trip(self):
        self.assertEqual(self.prepare(URL + '?si=private\nspotify:track:' + TRACK), 1)
        self.fetch.assert_called_once_with(TRACK)
        command = self.worker.send.call_args.args[0]
        self.assertEqual(command['action'], 'batch_lyrics')
        self.assertEqual(command['track'], track())
        self.assertFalse(self.source.receive(Mock(), self.event()))
        self.assertFalse(self.source.receive(self.worker, self.event(request_id='stale')))
        self.assertTrue(self.source.receive(self.worker, self.event()))
        self.assertFalse(self.source.busy)
        self.assertEqual(self.source.imported, 1)
        self.assertEqual(self.source.records(), ({**track(), 'included': True},))
        saved = self.store.load(TRACK)
        self.assertEqual([row.original for row in saved['lines']], ['夜空を見上げる', '星がきれい'])
        self.assertEqual(saved['direct']['times'], [10, 20])
        self.assertEqual(saved['sources'], (URL,))
        self.assertNotIn('private', self.source.path.read_text('utf-8'))

    def test_already_saved_source_can_be_managed_and_reused_offline(self):
        lines = (Line('星を見る', '', '별을 본다'),)
        direct = {'source':'Spotify', 'provider_id': TRACK, 'lines': lines, 'times': [12]}
        self.assertTrue(self.store.save(track(), lines, direct, [URL]))
        self.app.background_lyrics.job = None
        self.prepare()
        self.assertEqual(self.source.imported, 1)
        self.source.set_included(TRACK, False)
        self.source = SpotifyTranslationSources(self.app)
        self.prepare()
        self.assertFalse(self.source.records()[0]['included'])
        self.assertEqual(self.store.load(TRACK)['lines'], lines)
        self.assertEqual(self.store.load(TRACK)['direct']['times'], [12])
        self.worker.send.assert_not_called()
        self.fetch.assert_not_called()

    def test_current_verified_metadata_avoids_network_lookup_only_for_its_track(self):
        current = Mock(return_value=track())
        self.app.workspace = SimpleNamespace(music_catalog=SimpleNamespace(spotify=SimpleNamespace(current_track=current)))
        self.prepare(URL + '\nspotify:track:' + OTHER)
        self.fetch.assert_not_called()
        self.assertEqual(self.worker.send.call_args.args[0]['track'], track())
        self.source.receive(self.worker, self.event(code='not_found'))
        self.source.tick()
        self.source.tick()
        self.fetch.assert_called_once_with(OTHER)
        self.assertEqual(self.worker.send.call_args.args[0]['track'], track(OTHER))

    def test_merged_source_with_exact_spotify_snapshot_preserves_supplemental_lines(self):
        lines = (Line('夜空を見上げる', '', '밤하늘을 올려다본다'), Line('星がきれい', '', ''),
                 Line('朝の光が来る', '', '아침 빛이 온다'))
        alsong = {'source':'ALSong', 'provider_id': '100', 'lines': lines, 'times': [1, 5, 10]}
        spotify = {'source':'Spotify', 'provider_id': TRACK,
                   'lines': (lines[0], lines[2]), 'times': [1, 10]}
        self.assertTrue(self.store.save(track(), lines, alsong))
        self.assertTrue(self.store.save(track(), spotify['lines'], spotify))
        before = self.store.load(TRACK)
        self.assertTrue(any(item['source'] == 'Spotify' for item in before['snapshots']))
        self.prepare()
        self.assertEqual(self.source.imported, 1)
        self.assertEqual(self.store.load(TRACK), before)
        self.assertEqual([line.original for line in before['lines']], [line.original for line in lines])
        self.worker.send.assert_not_called()
        self.fetch.assert_not_called()

    def test_non_japanese_and_mismatched_responses_do_not_write(self):
        self.prepare(URL + '\nspotify:track:' + OTHER)
        self.assertTrue(self.source.receive(self.worker, self.event(lines=[{'text':'Only English', 'time':None}])))
        self.source.tick()
        self.source.tick()
        self.assertTrue(self.source.receive(self.worker, self.event(provider_id=TRACK)))
        self.assertEqual((self.source.skipped, self.source.failed, self.source.imported), (1, 1, 0))
        self.assertIsNone(self.store.load(TRACK))
        self.assertIsNone(self.store.load(OTHER))

    def test_unsynced_half_width_japanese_and_rate_limit(self):
        self.prepare(URL + '\nspotify:track:' + OTHER)
        self.assertTrue(self.source.receive(self.worker, self.event(lines=[{'text':'ｱｲｳ', 'time':None}])))
        self.source.tick()
        self.source.tick()
        self.assertTrue(self.source.receive(self.worker, self.event(code='rate_limited', retry_after=30)))
        self.assertIn('요청 한도', self.source.status)
        self.assertEqual((self.source.imported, self.source.failed), (1, 1))
        self.assertEqual(self.store.load(TRACK)['direct']['times'], [None])

    def test_collection_worker_busy_resumes_and_cancel_ignores_late_response(self):
        self.app.playlist_lyrics.active = object()
        self.prepare()
        self.worker.send.assert_not_called()
        self.app.playlist_lyrics.active = None
        self.source.tick()
        self.assertTrue(self.source.receive(self.worker, self.event(code='busy')))
        self.source.tick()
        self.assertEqual(self.worker.send.call_count, 1)
        self.source.pending['next_at'] = 0
        self.source.tick()
        self.assertEqual(self.worker.send.call_count, 2)
        self.assertTrue(self.source.receive(self.worker, self.event(code='interrupted')))
        self.source.pending['next_at'] = 0
        self.source.tick()
        self.assertEqual(self.worker.send.call_count, 3)
        late = self.event()
        self.source.cancel()
        self.worker.send.assert_called_with({'action':'batch_lyrics_cancel', 'request_id':late['request_id']})
        self.assertFalse(self.source.receive(self.worker, late))
        self.assertEqual(self.source.records(), ())

    def test_rejects_invalid_input_and_explains_login(self):
        with self.assertRaises(ValueError):self.source.start(URL + ' https://example.com/track/' + OTHER)
        self.app.background_lyrics.spotify_authenticated = False
        self.prepare()
        self.assertIn('로그인', self.source.status)
        self.assertEqual(self.source.failed, 1)


class PublicMetadataTests(unittest.TestCase):
    def test_public_resource_accepts_only_the_exact_track(self):
        response = Mock()
        response.geturl.return_value = URL
        response.read.return_value = b'<html></html>'
        with patch('spotify_translation_sources.urlopen') as open_url, \
             patch('spotify_translation_sources.parse_public_tracks', return_value=[track(OTHER), track()]):
            open_url.return_value.__enter__.return_value = response
            self.assertEqual(_public_track(TRACK), track())
            self.assertEqual(open_url.call_args.args[0].full_url, URL)
            response.geturl.return_value = 'https://open.spotify.com/track/' + OTHER
            self.assertIsNone(_public_track(TRACK))


if __name__ == '__main__':
    unittest.main()
