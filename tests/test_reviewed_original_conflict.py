import json
from pathlib import Path
import tempfile
import unittest

from lyrics import Line
from media import Playback
from playlist_lyrics_store import PlaylistLyricsStore
from public_lyrics import fetch_public_lyrics, reviewed_original_conflict
from storage import atomic_json


class ReviewedOriginalConflictTests(unittest.TestCase):
    def setUp(self):
        self.document = json.loads((Path(__file__).parent/'fixtures/reviewed-wrong-original.json').read_text(encoding='utf8'))
        self.track = self.document['track']
        self.bad = tuple(Line(**row) for row in self.document['lines'])

    def conflict(self, **changes):
        track = {**self.track, **changes}
        return reviewed_original_conflict(track['title'],track['artists'][0],track['album'],track['duration'],self.bad)

    def test_verified_content_is_rejected_only_for_the_reviewed_recording(self):
        self.assertTrue(self.conflict())
        for changed in ({'title':'君との明日を願うから'}, {'album':'Another album'},
                        {'duration':347}, {'artists':['Another singer']}):
            self.assertFalse(self.conflict(**changed))
        # A line-break change cannot reintroduce the same wrong song.
        joined = (Line(''.join(row.original for row in self.bad),'',''),)
        self.assertTrue(reviewed_original_conflict(self.track['title'],self.track['artists'][0],
                                                  self.track['album'],self.track['duration'],joined))

    def test_metadata_matched_lrclib_wrong_song_is_not_returned(self):
        playback = Playback(self.track['title'],self.track['artists'][0],self.track['album'],duration=self.track['duration'])
        bad = {'id':1, 'trackName':playback.title,'artistName':playback.artist,
               'albumName':playback.album,'duration':playback.duration,
               'plainLyrics':'\n'.join(row.original for row in self.bad)}
        self.assertIsNone(fetch_public_lyrics(playback,fetch=lambda params:[bad]))
        corrected = {**bad,'id':2,'plainLyrics':'Corrected original from provider'}
        self.assertEqual(fetch_public_lyrics(playback,fetch=lambda params:[bad,corrected])['provider_id'],2)

    def test_wrong_file_is_preserved_and_hidden_until_corrected_source_is_saved(self):
        with tempfile.TemporaryDirectory() as root:
            store = PlaylistLyricsStore(root)
            path = store.tracks_directory/(self.track['id']+'.json')
            atomic_json(path,self.document)
            before = path.read_bytes()
            self.assertIsNone(store.load(self.track['id']))
            self.assertFalse(store.save(self.track,self.bad,None))
            self.assertEqual(path.read_bytes(),before)
            corrected = (Line('Corrected original from provider','existing reading','existing translation'),)
            self.assertTrue(store.save(self.track,corrected,None,sources=('verified_source',)))
            self.assertEqual(store.load(self.track['id'])['lines'],corrected)
            self.assertEqual(json.loads((store.directory/'rejected'/path.name).read_text(encoding='utf8')),self.document)
            self.assertFalse(store.save(self.track,self.bad,None))
            self.assertEqual(store.load(self.track['id'])['lines'],corrected)


if __name__ == '__main__':
    unittest.main()
