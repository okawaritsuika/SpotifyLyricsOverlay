import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools.collect_spotify_links import expand
from tests.test_spotify_public_catalog import ALBUM, OTHER, TRACK, artists, html, state, track


PLAYLIST_URL = 'https://open.spotify.com/playlist/' + TRACK
ARTIST_URL = 'https://open.spotify.com/artist/' + OTHER
ALBUM_URL = 'https://open.spotify.com/album/' + ALBUM


def response(value, status=200, headers=None):
    result = Mock(status_code=status, text=value, content=value.encode(), headers=headers or {})
    result.raise_for_status.return_value = None
    return result


class PublicCatalogExpansionTests(unittest.TestCase):
    def test_known_performer_releases_expand_without_exact_dictionary_album_name(self):
        song = {'title': 'Song', 'artists': ['Singer'], 'album': 'Translated album'}
        public_artist = {'uri': 'spotify:artist:' + OTHER, 'profile': {'name': 'Singer'}}
        foreign_artist = {'uri': 'spotify:artist:' + ALBUM, 'profile': {'name': 'Unrelated'}}
        public_album = {'uri': 'spotify:album:' + ALBUM, 'name': 'Original album'}
        public_artist['discography'] = {'albums': {'items': [public_album]}}
        page = html(state({'items': [public_artist, foreign_artist]}))
        nested = track()
        nested.pop('albumOfTrack')
        album_page = html(state({**public_album, 'tracksV2': {'items': [{'track': nested}]}}))
        def fetch(url, **kwargs):
            if '/playlist/' in url and '/embed/' not in url:return response(page)
            if url == ARTIST_URL:return response(html(state(public_artist)))
            if url == ALBUM_URL:return response(album_page)
            return response(html(state()))
        with tempfile.TemporaryDirectory() as directory, \
             patch('tools.collect_spotify_links.requests.get', side_effect=fetch) as get, \
             patch('tools.collect_spotify_links.time.sleep'):
            result = expand({'playlists': [{'url': PLAYLIST_URL + '?si=private'}]},
                            Path(directory), {'tracks': []}, {'songs': [song], 'performers': [
                                {'name': 'Singer', 'kind': 'idol'}]}, max_pages=10)
            urls = [call.args[0] for call in get.call_args_list]
            self.assertIn(ARTIST_URL, urls)
            self.assertIn(ALBUM_URL, urls)
            self.assertNotIn('https://open.spotify.com/artist/' + ALBUM, urls)
            self.assertFalse(any('?' in url for url in urls))
            self.assertEqual(result['tracks'], 1)
            saved = json.loads((Path(directory) / 'public-tracks.json').read_text(encoding='utf-8'))
            self.assertEqual(saved['tracks'][0]['album'], 'Original album')
            self.assertEqual(set(saved['resources']['artists']), {OTHER})

    def test_unrelated_release_does_not_expand_because_a_voice_actor_is_known(self):
        value = track(name='Unrelated solo song', albumOfTrack={'uri': 'spotify:album:' + ALBUM, 'name': 'Unrelated album'})
        value['artists']['items'][0]['uri'] = 'spotify:artist:' + OTHER
        next_album = {'uri': 'spotify:album:' + TRACK, 'name': 'Unrelated next album'}
        def fetch(url, **kwargs):
            if url == ALBUM_URL:return response(html(state(value, next_album)))
            return response(html(state()))
        with tempfile.TemporaryDirectory() as directory, \
             patch('tools.collect_spotify_links.requests.get', side_effect=fetch) as get, \
             patch('tools.collect_spotify_links.time.sleep'):
            path = Path(directory) / 'public-tracks.json'
            path.write_text(json.dumps({'resources': {'artists': {}, 'albums': {
                ALBUM: {'id': ALBUM, 'name': 'Unrelated album', 'url': ALBUM_URL}}}}), encoding='utf-8')
            expand({}, Path(directory), {'tracks': []}, {'songs': [{'title': 'Wanted song', 'artists': ['Singer']}]})
            self.assertNotIn('https://open.spotify.com/album/' + TRACK,
                             [call.args[0] for call in get.call_args_list])

    def test_rate_limit_stops_before_other_pages_and_saves_retry_information(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch('tools.collect_spotify_links.requests.get', return_value=response('', 429, {'Retry-After': '120'})) as get, \
             patch('tools.collect_spotify_links.time.sleep'):
            result = expand({'playlists': [{'url': PLAYLIST_URL}]}, Path(directory),
                            {'tracks': []}, {'songs': []}, max_pages=10)
            self.assertTrue(result['rate_limited'])
            self.assertEqual(get.call_count, 1)
            saved = json.loads((Path(directory) / 'public-tracks.json').read_text(encoding='utf-8'))
            self.assertEqual(saved['expansion']['retry_after'], 120)
            self.assertEqual(saved['tracks'], [])
            resumed = expand({'playlists': [{'url': PLAYLIST_URL}]}, Path(directory),
                             {'tracks': []}, {'songs': []}, max_pages=10)
            self.assertEqual(resumed['requests'], 0)
            self.assertEqual(get.call_count, 1)

    def test_resuming_preserves_unfetched_embed_discoveries(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch('tools.collect_spotify_links.requests.get', return_value=response(html(state()))) as get, \
             patch('tools.collect_spotify_links.time.sleep'):
            path = Path(directory) / 'public-tracks.json'
            path.write_text(json.dumps({'tracks': [], 'discovered': [{'id': TRACK}]}), encoding='utf-8')
            expand({}, Path(directory), {'tracks': []}, {'songs': []})
            self.assertEqual(get.call_args.args[0], 'https://open.spotify.com/track/' + TRACK)

    def test_manifest_recording_ids_are_validated_and_fetched(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch('tools.collect_spotify_links.requests.get', return_value=response(html(state()))) as get, \
             patch('tools.collect_spotify_links.time.sleep'):
            result = expand({'track_ids': [TRACK, 'not-an-id', None]}, Path(directory),
                            {'tracks': []}, {'songs': []})
            self.assertEqual(result['requests'], 1)
            self.assertEqual(get.call_args.args[0], 'https://open.spotify.com/track/' + TRACK)

    def test_known_album_source_is_used_without_requerying_its_representative_track(self):
        row = {'id': TRACK, 'title': 'Song', 'artists': ['Singer'], 'album': 'Album', 'source_url': ALBUM_URL}
        with tempfile.TemporaryDirectory() as directory, \
             patch('tools.collect_spotify_links.requests.get', return_value=response(html(state()))) as get, \
             patch('tools.collect_spotify_links.time.sleep'):
            expand({}, Path(directory), {'tracks': [row]},
                   {'performers': [{'name': 'Singer', 'kind': 'idol'}]})
            self.assertEqual(get.call_count, 1)
            self.assertEqual(get.call_args.args[0], ALBUM_URL)

    def test_generic_same_title_seed_cannot_authorize_an_unknown_artist(self):
        row = {'id': TRACK, 'title': 'ALIVE', 'artists': ['Unrelated band'],
               'album': 'Unrelated album', 'source_url': ALBUM_URL}
        with tempfile.TemporaryDirectory() as directory, \
             patch('tools.collect_spotify_links.requests.get') as get:
            result = expand({}, Path(directory), {'tracks': [row]}, {'songs': [{'title': 'ALIVE'}]})
            self.assertEqual(result['tracks'], 1)
            get.assert_not_called()

    def test_album_results_skip_obsolete_queued_embed_track_requests(self):
        album = {'uri': 'spotify:album:' + ALBUM, 'name': 'Album'}
        nested = track(OTHER)
        nested.pop('albumOfTrack')
        embed = {'props': {'pageProps': {'state': {'data': {'entity': {'trackList': [
            {'uri': 'spotify:track:' + OTHER, 'title': 'Song', 'duration': 240123}]}}}}}}
        def fetch(url, **kwargs):
            if '/embed/playlist/' in url:return response(html(embed, '__NEXT_DATA__'))
            if '/album/' in url:return response(html(state({**album, 'tracksV2': {'items': [{'track': nested}]}})))
            return response(html(state(album)))
        with tempfile.TemporaryDirectory() as directory, \
             patch('tools.collect_spotify_links.requests.get', side_effect=fetch) as get, \
             patch('tools.collect_spotify_links.time.sleep'):
            result = expand({'playlists': [{'url': PLAYLIST_URL}]}, Path(directory),
                            {'tracks': []}, {'songs': [{'title': 'Song', 'album': 'Album'}]})
            self.assertEqual(result['tracks'], 1)
            self.assertFalse(any('/track/' in call.args[0] for call in get.call_args_list))

    def test_request_budget_keeps_unfinished_work_visible(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch('tools.collect_spotify_links.requests.get', return_value=response(html(state()))), \
             patch('tools.collect_spotify_links.time.sleep'):
            result = expand({'playlists': [{'url': PLAYLIST_URL}]}, Path(directory),
                            {'tracks': []}, {'songs': []}, max_pages=1)
            self.assertEqual(result['requests'], 1)
            self.assertEqual(result['pending'], 1)
            saved = json.loads((Path(directory) / 'public-tracks.json').read_text(encoding='utf-8'))
            self.assertTrue(saved['expansion']['limit_reached'])

    def test_partial_album_listing_discovers_remaining_embed_rows(self):
        nested = track()
        nested.pop('albumOfTrack')
        album = {'uri': 'spotify:album:' + ALBUM, 'name': 'Album',
                 'tracksV2': {'totalCount': 2, 'items': [{'track': nested}]}}
        embed = {'props': {'pageProps': {'state': {'data': {'entity': {'trackList': [
            {'uri': 'spotify:track:' + OTHER, 'title': 'Other song', 'duration': 210000}]}}}}}}
        def fetch(url, **kwargs):
            if '/embed/album/' in url:return response(html(embed, '__NEXT_DATA__'))
            if url.endswith('/track/' + OTHER):return response(html(state(track(OTHER))))
            return response(html(state(album)))
        with tempfile.TemporaryDirectory() as directory, \
             patch('tools.collect_spotify_links.requests.get', side_effect=fetch) as get, \
             patch('tools.collect_spotify_links.time.sleep'):
            path = Path(directory) / 'public-tracks.json'
            path.write_text(json.dumps({'resources': {'artists': {}, 'albums': {
                ALBUM: {'id': ALBUM, 'name': 'Album', 'url': ALBUM_URL}}}}), encoding='utf-8')
            result = expand({}, Path(directory), {'tracks': []},
                            {'songs': [{'title': 'Song', 'album': 'Album'}]})
            urls = [call.args[0] for call in get.call_args_list]
            self.assertIn(ALBUM_URL.replace('open.spotify.com/', 'open.spotify.com/embed/'), urls)
            self.assertIn('https://open.spotify.com/track/' + OTHER, urls)
            self.assertEqual(result['tracks'], 2)


if __name__ == '__main__':unittest.main()
