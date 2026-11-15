import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import imas_lyrics
import imas_timetag
import playlist_lyrics_sources
from lyrics import Line
from playlist_lyrics_store import PlaylistLyricsStore


class ImasLyricsTests(unittest.TestCase):
    def test_game_cut_is_not_replaced_with_unlabelled_lyrics(self):
        url = 'https://semython.github.io/GAKUMASU_lyrics/sample.html'
        pages = {imas_lyrics.GAKUMAS_INDEX: f'<a href="{url}">Example</a>',
                 url: '<h1>Example</h1><p><span class="line" data-part="A">Hello</span> '
                      '<span class="line" data-part="B">world</span><br>'
                      '<span class="line" data-part="A">次の行</span></p>'}
        found = imas_lyrics.find_candidate('Example', 'gakuen', fetch=pages.__getitem__)
        self.assertEqual(tuple(line.original for line in found['lines']), ('Hello world', '次の行'))
        self.assertIsNone(imas_lyrics.find_candidate('Example (GAME VERSION)', 'gakuen', fetch=pages.__getitem__))

    def test_timetag_lookup_rejects_other_recording_before_fetch(self):
        result = imas_timetag.find_candidate(imas_timetag._TITLE, imas_timetag._ARTIST,
            imas_timetag._ALBUM, imas_timetag._DURATION, track_id='Z' * 22,
            fetch=lambda url: self.fail('Wrong recording sent to provider'))
        self.assertIsNone(result)

    def test_verified_timetag_survives_playlist_collection_and_reload(self):
        track = {'id': imas_timetag._TRACK_ID, 'title': imas_timetag._TITLE,
                 'artists': [imas_timetag._ARTIST], 'album': imas_timetag._ALBUM,
                 'duration': imas_timetag._DURATION}
        candidate = {'source': 'imas_timetag', 'lines': (Line('歌の始まり', '', ''), Line('歌の終わり', '', '')),
                     'times': [12, 30], 'url': imas_timetag._TAGS_URL, 'lyrics_url': imas_timetag._LYRICS_URL}
        with tempfile.TemporaryDirectory() as temp, \
                patch('imas_timetag.find_candidate', return_value=candidate), \
                patch('public_lyrics.fetch_public_lyrics', return_value=None), \
                patch('alsong_provider.fetch_alsong', return_value=None), \
                patch('web_lyrics.fetch_lyrics', return_value=None):
            directory = Path(temp)
            result = playlist_lyrics_sources.complete_track(directory, directory, track, cancel=threading.Event())
            self.assertEqual(result['times'], [12, 30])
            self.assertIn(candidate['url'], result['sources'])
            self.assertIn(candidate['lyrics_url'], result['sources'])
            store = PlaylistLyricsStore(directory)
            self.assertTrue(store.save(track, result['lines'], result['direct'], result['sources'],
                                       snapshots=result['snapshots'], merge_rows=result['rows']))
            self.assertEqual(store.load(track['id'])['direct']['times'], [12, 30])
