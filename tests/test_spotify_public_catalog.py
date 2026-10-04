import base64
import copy
import json
import unittest

from spotify_public_catalog import parse_embed_tracks, parse_public_catalog, parse_public_state, parse_public_tracks


TRACK = 'a' * 22
ALBUM = 'b' * 22
OTHER = 'c' * 22
URL = 'https://open.spotify.com/track/' + TRACK


def artists(*names):
    return {'items': [{'profile': {'name': name}} for name in names]}


def track(identifier=TRACK, **changes):
    return {'uri': 'spotify:track:' + identifier, 'id': identifier, 'name': 'Song',
            'artists': artists('Singer'), 'duration': {'totalMilliseconds': 240123},
            'albumOfTrack': {'uri': 'spotify:album:' + ALBUM, 'name': 'Album'}, **changes}


def state(*values):
    return {'entities': {'items': dict(enumerate(values))}}


def html(value, script_id='initialState'):
    value = json.dumps(value, ensure_ascii=False)
    if script_id == 'initialState':value = base64.b64encode(value.encode()).decode()
    return "<html><script type='text/plain' id='" + script_id + "'>\n" + value + "\n</script></html>"


class PublicSpotifyCatalogTests(unittest.TestCase):
    def test_public_html_extracts_only_canonical_metadata(self):
        value = track(playability={'playable': True}, session={'token': 'secret'})
        original = copy.deepcopy(value)
        actual = parse_public_tracks(html(state(value)), URL + '?si=private&token=secret')
        self.assertEqual(actual, [{'id': TRACK, 'title': 'Song', 'artists': ['Singer'],
                                  'album': 'Album', 'duration': 240.123, 'is_playable': None,
                                  'url': URL, 'source_url': URL}])
        self.assertEqual(value, original)

    def test_split_artist_list_is_complete_and_ordered(self):
        value = track(firstArtist=artists('Unit'), otherArtists=artists('Singer A', 'Singer B', 'Unit'))
        self.assertEqual(parse_public_state(state(value))[0]['artists'], ['Unit', 'Singer A', 'Singer B'])

    def test_split_artist_list_requires_both_halves(self):
        for changes in ({'firstArtist': artists('Unit')}, {'otherArtists': artists('Singer')},
                        {'firstArtist': artists(), 'otherArtists': artists('Singer')},
                        {'firstArtist': artists('Unit'), 'otherArtists': {'items': [{'profile': {}}]}}):
            with self.subTest(changes=changes):
                self.assertEqual(parse_public_state(state(track(**changes))), [])
        self.assertEqual(parse_public_state(state(track(firstArtist=artists('Solo'), otherArtists=artists())))[0]['artists'], ['Solo'])

    def test_album_tracks_inherit_only_explicit_album_context(self):
        nested = track()
        nested.pop('albumOfTrack')
        value = {'uri': 'spotify:album:' + ALBUM, 'name': 'Parent album',
                 'tracks': {'items': [{'track': nested}]},
                 'recommendations': [dict(nested, uri='spotify:track:' + OTHER, id=OTHER)]}
        actual = parse_public_state(state(value))
        self.assertEqual([row['id'] for row in actual], [TRACK])
        self.assertEqual(actual[0]['album'], 'Parent album')

    def test_track_page_album_tracks_and_artist_discography_are_independent(self):
        nested = track(OTHER)
        nested.pop('albumOfTrack')
        value = track()
        value['albumOfTrack']['tracks'] = {'items': [{'track': nested}]}
        result = parse_public_state(state(value))
        self.assertEqual([row['id'] for row in result], [TRACK, OTHER])
        self.assertEqual([row['album'] for row in result], ['Album', 'Album'])

    def test_nested_track_own_album_overrides_parent(self):
        album = {'uri': 'spotify:album:' + OTHER, 'name': 'Outer',
                 'tracks': {'items': [{'track': track()}]}}
        self.assertEqual(parse_public_state(state(album))[0]['album'], 'Album')

    def test_album_tracks_v2_inherits_verified_album_context(self):
        nested = track()
        nested.pop('albumOfTrack')
        album = {'uri': 'spotify:album:' + ALBUM, 'name': 'Full album',
                 'tracksV2': {'totalCount': 1, 'items': [{'track': nested}]}}
        self.assertEqual(parse_public_state(state(album))[0]['album'], 'Full album')
        album['tracksV2']['totalCount'] = 100
        result = parse_public_catalog(html(state(album)))
        self.assertEqual(result['albums'][0]['track_count'], 100)
        self.assertEqual(result['albums'][0]['listed_tracks'], 1)

    def test_resource_discovery_retains_only_public_named_album_and_artist_references(self):
        value = track()
        value['artists']['items'][0]['uri'] = 'spotify:artist:' + OTHER
        value['albumOfTrack']['copyright'] = {'secret': 'not retained'}
        source = state(value, value)
        source['session'] = {'uri': 'spotify:artist:' + TRACK, 'name': 'Private name'}
        result = parse_public_catalog(html(source), URL)
        self.assertEqual(len(result['tracks']), 1)
        self.assertEqual(result['albums'], [{'id': ALBUM, 'name': 'Album',
                          'url': 'https://open.spotify.com/album/' + ALBUM}])
        self.assertEqual(result['artists'], [{'id': OTHER, 'name': 'Singer',
                           'url': 'https://open.spotify.com/artist/' + OTHER}])

    def test_playlist_name_never_becomes_album(self):
        value = track()
        value.pop('albumOfTrack')
        playlist = {'uri': 'spotify:playlist:' + ALBUM, 'name': 'Playlist',
                    'content': {'items': [{'itemV2': {'data': value}}]}}
        self.assertEqual(parse_public_state(state(playlist)), [])

    def test_incomplete_artist_lists_are_rejected(self):
        for value in (None, [], artists(), artists(''), {'items': [None]},
                      {'items': [{'profile': {'name': 'Singer'}}], 'totalCount': 2}):
            with self.subTest(value=value):
                self.assertEqual(parse_public_state(state(track(artists=value))), [])

    def test_invalid_duration_and_track_identity_are_rejected(self):
        for value in (None, True, -1, 0, 'bad', float('nan'), float('inf'), 10 ** 1000):
            with self.subTest(value=str(value)[:30]):
                self.assertEqual(parse_public_state(state(track(duration={'totalMilliseconds': value}))), [])
        for changes in ({'uri': 'spotify:track:short'}, {'id': OTHER}, {'name': ''},
                        {'albumOfTrack': {'name': 'Missing album ID'}}, {'duration': 240123}):
            with self.subTest(changes=changes):
                self.assertEqual(parse_public_state(state(track(**changes))), [])

    def test_other_state_roots_are_ignored_and_repeated_tracks_are_deduplicated(self):
        value = state(track(), track())
        value['session'] = {'private': track(OTHER)}
        self.assertEqual([row['id'] for row in parse_public_state(value)], [TRACK])

    def test_source_url_rejects_untrusted_hosts_and_preserves_public_resource(self):
        for url in ('https://evil.test/track/' + TRACK, 'https://user@open.spotify.com/track/' + TRACK,
                    'http://open.spotify.com/track/' + TRACK, 'https://open.spotify.com:444/track/' + TRACK):
            self.assertEqual(parse_public_state(state(track()), url)[0]['source_url'], '')
        url = 'https://open.spotify.com/intl-ja/album/' + ALBUM + '?si=private'
        self.assertEqual(parse_public_state(state(track()), url)[0]['source_url'], 'https://open.spotify.com/album/' + ALBUM)

    def test_malformed_html_or_state_returns_no_records(self):
        for value in (None, '', '<script id="initialState">not base64</script>',
                      '<script id="initialState">e30=</script>', html([]), html({'entities': []})):
            self.assertEqual(parse_public_tracks(value), [])

    def test_embed_rows_provide_discovery_hints_without_artist_guesses(self):
        row = {'uri': 'spotify:track:' + TRACK, 'title': 'Song', 'duration': 240123,
               'subtitle': 'Singer A, Singer B', 'isPlayable': True}
        entity = {'trackList': [row, row, dict(row, uri='spotify:track:bad'),
                                dict(row, uri='spotify:track:' + OTHER, duration=float('nan'))]}
        value = {'props': {'pageProps': {'state': {'data': {'entity': entity}}}}}
        self.assertEqual(parse_embed_tracks(html(value, '__NEXT_DATA__')),
                         [{'id': TRACK, 'title': 'Song', 'duration': 240.123}])


if __name__ == '__main__':unittest.main()
