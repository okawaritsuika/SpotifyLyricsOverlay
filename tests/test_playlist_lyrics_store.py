from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lyrics import Line
from media import Playback
from playlist_lyrics_store import PlaylistLyricsStore


class PlaylistLyricsStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = PlaylistLyricsStore(self.root)
        self.track = {'id': '0123456789ABCDEFGHIJKL', 'title': 'Song (Solo Ver.)',
                      'artists': ['天海春香 (CV.中村繪里子)', 'Second Artist'], 'album': 'Album', 'duration': 200}
        self.lines = (Line('First lyric line', 'reading', 'translation'), Line('Second lyric line', '', ''))
        self.direct = {'source': 'Spotify', 'provider_id': self.track['id'], 'lines': self.lines, 'times': [0, 10]}
        self.playback = Playback(self.track['title'], self.track['artists'][0], self.track['album'], duration=200)

    def tearDown(self):
        self.temp.cleanup()

    def path(self):
        return self.store.tracks_directory / (self.track['id'] + '.json')

    def save(self, lines=None, direct=None):
        return self.store.save(self.track, self.lines if lines is None else lines,
                               self.direct if direct is None else direct, sources=('Spotify',))

    def test_fresh_instance_restores_id_bound_lyrics_and_sanitized_metadata(self):
        self.assertTrue(self.store.save({**self.track, 'profile': 'PRIVATE'}, self.lines,
                                       {**self.direct, 'access_token': 'PRIVATE'}, ('Spotify',)))
        restored = PlaylistLyricsStore(self.root).load(self.track['id'])
        self.assertEqual(restored, {'track': self.track, 'lines': self.lines, 'direct': self.direct, 'sources': ('Spotify',)})
        self.assertNotIn('PRIVATE', self.path().read_text(encoding='utf-8'))
        self.assertEqual(self.store.metadata(), (self.track,))

    def test_find_accepts_registered_alias_and_all_credits_but_rejects_recording_changes(self):
        self.assertTrue(self.save())
        self.assertIsNotNone(self.store.find(self.playback))
        self.assertIsNotNone(self.store.find(replace(self.playback, artist='아마미 하루카 (CV.나카무라 에리코)')))
        self.assertIsNotNone(self.store.find(replace(self.playback, artist=', '.join(self.track['artists']))))
        for fields in ({'title': 'Song'}, {'title': 'Song (Live)'}, {'album': 'Other Album'}, {'album': ''},
                       {'artist': 'Second Artist'}, {'artist': self.track['artists'][0] + ', Other Artist'},
                       {'duration': 203}):
            self.assertIsNone(self.store.find(replace(self.playback, **fields)))

    def test_multiple_matching_ids_are_ambiguous(self):
        self.assertTrue(self.save())
        other = {**self.track, 'id': 'z' * 22}
        self.assertTrue(self.store.save(other, self.lines, None))
        self.assertIsNone(self.store.find(self.playback))

    def test_updates_preserve_existing_annotations_and_more_complete_clock(self):
        self.assertTrue(self.save())
        update = (Line('First lyric line', 'new reading', 'new translation'),
                  Line('Second lyric line', 'second reading', 'second translation'))
        self.assertTrue(self.save(update, {**self.direct, 'lines': update, 'times': [None, None]}))
        saved = self.store.load(self.track['id'])
        self.assertEqual(saved['lines'], (self.lines[0], update[1]))
        self.assertEqual(saved['direct']['times'], [0, 10])

    def test_late_spotify_keeps_other_source_onsets_and_reviewed_annotations(self):
        middle = Line('A missing middle line', '', '')
        lines = (self.lines[0], middle, self.lines[1])
        alsong = {'source': 'ALSong', 'provider_id': '123', 'lines': lines, 'times': [0, 5, 10]}
        self.assertTrue(self.store.save(self.track, lines, alsong))
        self.assertTrue(self.save())
        merged = self.store.load(self.track['id'])
        self.assertEqual(merged['lines'], lines)
        self.assertEqual(merged['direct']['times'], [0, 5, 10])
        self.assertEqual([row['selected_source'] for row in merged['merge_rows']], ['Spotify', 'ALSong', 'Spotify'])
        self.assertEqual({item['source'] for item in merged['snapshots']}, {'Spotify', 'ALSong', 'local'})

        reviewed = (lines[0], Line(middle.original, '', '검토한 번역'), lines[2])
        self.assertTrue(self.store.save(self.track, reviewed, merged['direct'], sources=('reviewed_translation',)))
        self.assertTrue(self.store.save(self.track, merged['lines'], merged['direct'],
                                       snapshots=merged['snapshots'], merge_rows=merged['merge_rows']))
        restored = PlaylistLyricsStore(self.root).load(self.track['id'])
        self.assertEqual(restored['lines'], reviewed)
        self.assertEqual(restored['direct']['times'], [0, 5, 10])
        self.assertEqual(restored['merge_rows'][1]['translation_source'], 'manual')

    def test_partial_old_text_can_be_completed_without_erasing_annotations(self):
        self.assertTrue(self.save())
        complete = (self.lines[0], Line('A missing middle line', '', 'middle translation'), self.lines[1])
        direct = {**self.direct, 'lines': complete, 'times': [0, 5, 10]}
        self.assertTrue(self.save(complete, direct))
        saved = self.store.load(self.track['id'])
        self.assertEqual(saved['lines'], complete)
        self.assertEqual(saved['direct']['times'], [0, 5, 10])

    def test_exact_original_rows_with_symbols_preserve_originals_and_verified_times(self):
        lines=(Line('First lyric line','',''),Line('♪ ♪','',''),Line('…','',''),Line('Last lyric line','',''))
        direct={**self.direct,'lines':lines,'times':[0,5,6,10]}
        self.assertTrue(self.save(lines,direct))
        saved=self.store.load(self.track['id'])
        self.assertEqual(saved['lines'],lines)
        self.assertEqual(saved['direct']['times'],[0,5,6,10])
        self.assertTrue(self.save(lines,{**direct,'times':[None]*len(lines)}))
        saved=self.store.load(self.track['id'])
        self.assertEqual(saved['lines'],lines)
        self.assertEqual(saved['direct']['times'],[0,5,6,10])

    def test_exact_unsynced_symbol_rows_remain_unsynced(self):
        lines=(Line('First lyric line','',''),Line('☆','',''),Line('Last lyric line','',''))
        direct={**self.direct,'lines':lines,'times':[None]*len(lines)}
        self.assertTrue(self.save(lines,direct))
        saved=self.store.load(self.track['id'])
        self.assertEqual(saved['lines'],lines)
        self.assertEqual(saved['direct']['times'],[None]*len(lines))

    def test_exact_symbol_rows_fill_annotations_by_position_and_preserve_saved_fields(self):
        old = (Line('Repeated lyric', 'saved reading', ''), Line('<♪>', '', ''),
               Line('Repeated lyric', '', 'saved translation'), Line('Last lyric', '', ''))
        direct = {**self.direct, 'lines': old, 'times': [0, 5, 10, 15]}
        self.assertTrue(self.save(old, direct))
        update = (Line('Repeated lyric', 'replacement reading', 'first translation'),
                  Line('<♪>', 'symbol reading', 'symbol translation'),
                  Line('Repeated lyric', 'second reading', 'replacement translation'),
                  Line('Last lyric', 'last reading', 'last translation'))
        self.assertTrue(self.save(update, {**direct, 'lines': update, 'times': [None] * 4}))
        expected = (Line('Repeated lyric', 'saved reading', 'first translation'), update[1],
                    Line('Repeated lyric', 'second reading', 'saved translation'), update[3])
        saved = PlaylistLyricsStore(self.root).load(self.track['id'])
        self.assertEqual(saved['lines'], expected)
        self.assertEqual(saved['direct'], direct)
        # A late callback with the old blank annotations cannot erase the review.
        self.assertTrue(self.save(old, direct))
        self.assertEqual(PlaylistLyricsStore(self.root).load(self.track['id'])['lines'], expected)

    def test_symbol_shortcut_does_not_accept_changed_missing_or_reordered_original_rows(self):
        lines=(Line('First lyric line','',''),Line('♪ ♪','',''),Line('Last lyric line','',''))
        direct={**self.direct,'lines':lines,'times':[0,5,10]}
        self.assertTrue(self.save(lines,direct));before=self.path().read_bytes()
        for altered in ((lines[0],Line('☆☆','',''),lines[2]),
                        (lines[0],lines[2]),(lines[2],lines[1],lines[0])):
            with self.subTest(originals=tuple(row.original for row in altered)):
                self.assertFalse(self.save(altered,direct))
                self.assertFalse(self.save(altered,{**direct,'lines':altered,'times':list(range(len(altered)))}))
                self.assertEqual(self.path().read_bytes(),before)
        self.assertFalse(self.save(lines,{**direct,'provider_id':'z'*22}))
        self.assertEqual(self.path().read_bytes(),before)

    def test_wrong_id_unsupported_provider_and_unrelated_replacement_preserve_file(self):
        self.assertTrue(self.save())
        before = self.path().read_bytes()
        bad_lines = (Line('Unrelated opening text', '', ''), Line('Unrelated ending text', '', ''))
        for direct in ({**self.direct, 'provider_id': 'z' * 22}, {**self.direct, 'source': 'AI'},
                       {**self.direct, 'times': [10, 1]}, {**self.direct, 'times': [0, 203]}):
            self.assertFalse(self.save(direct=direct))
        self.assertFalse(self.save(bad_lines, {**self.direct, 'lines': bad_lines}))
        self.assertEqual(self.path().read_bytes(), before)

    def test_translation_update_does_not_rewrite_unchanged_metadata_index(self):
        self.assertTrue(self.save())
        from storage import atomic_json
        with patch('playlist_lyrics_store.atomic_json', wraps=atomic_json) as write:
            self.assertTrue(self.save())
        self.assertEqual([call.args[0] for call in write.call_args_list], [self.path()])

    def test_find_prefilters_other_titles_before_artist_alias_lookup(self):
        self.assertTrue(self.save())
        self.assertTrue(self.store.save({**self.track, 'id': 'x' * 22, 'title': 'Different Song'}, self.lines, None))
        from playlist_lyrics_store import _artist
        with patch('playlist_lyrics_store._artist', wraps=_artist) as translate:
            self.assertIsNotNone(self.store.find(self.playback))
        self.assertEqual(translate.call_count, 1 + 2 * len(self.track['artists']))

    def test_invalid_file_and_stale_index_cannot_supply_another_recording(self):
        self.assertTrue(self.save())
        value = json.loads(self.path().read_text(encoding='utf-8'))
        value['track']['title'] = 'Different Song'
        self.path().write_text(json.dumps(value))
        self.assertIsNone(self.store.find(self.playback))
        self.path().write_bytes(b'x' * (1024 * 1024 + 1))
        self.assertIsNone(self.store.load(self.track['id']))
        self.assertIsNone(self.store.load('../private'))

    def test_no_two_hundred_entry_eviction_or_personal_data_changes(self):
        personal = self.root / 'library.json'
        personal.write_text('personal')
        for index in range(201):
            track = {**self.track, 'id': f'{index:022d}'}
            self.assertTrue(self.store.save(track, self.lines, None))
        self.assertIsNotNone(PlaylistLyricsStore(self.root).load('0' * 22))
        self.assertEqual(len(self.store.metadata()), 201)
        self.assertEqual(personal.read_text(), 'personal')


if __name__ == '__main__':
    unittest.main()
