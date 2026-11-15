from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from lyric_cache import LyricCache
from lyrics import Line
from media import Playback
from playlist_lyrics_store import PlaylistLyricsStore
from recording_reuse import compatibility
from work_reuse import reviewed_arrangement


ALBUM = ('THE IDOLM@STER 20th anniversary ORCHESTRA CONCERT '
         'SYMPHONY OF BRILLIANT STARS コンサートアルバム')


class OrchestraReuseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.cache = LyricCache(self.directory / 'lyrics_cache')
        self.source = Playback('FairyTaleじゃいられない', '最上静香 (CV.田所あずさ)',
                               'THE IDOLM@STER MILLION THE@TER GENERATION 02', duration=222.36)
        self.target = Playback('FairyTaleじゃいられない（Day 第2幕「BRILLIANT STAGE」より） - Live',
                               '堀江晶太', ALBUM, duration=284.666)
        self.lines = (Line('青い空を見る', '독음', '번역'), Line('花を描く', '', ''))
        self.direct = {'source': 'Spotify', 'lines': self.lines,
                       'times': [1, 15], 'provider_id': 'a' * 22}

    def tearDown(self):self.temporary.cleanup()

    def test_real_concert_decoration_reuses_text_beyond_thirty_seconds_without_any_clock(self):
        self.assertTrue(self.cache.save(self.source, self.lines, self.direct))
        before = {path.name: path.read_bytes() for path in self.cache.directory.iterdir()}
        value = self.cache.load_related(self.target)
        self.assertEqual(value['lines'], self.lines)
        self.assertIsNone(value['direct'])
        self.assertIsNone(value['timing'])
        self.assertIsNone(value['tracking_times'])
        self.assertFalse(value['reuse']['timing_compatible'])
        self.assertTrue(value['reuse']['work_evidence'])
        self.assertEqual(value['reuse']['source_key'], self.source.key)
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.cache.directory.iterdir()})

    def test_equal_lengths_and_same_artist_still_cannot_share_a_concert_clock(self):
        value = compatibility(vars(self.source), replace(self.target,
                              artist=self.source.artist, duration=self.source.duration))
        self.assertFalse(value['timing_compatible'])
        self.assertFalse(value['tracking_compatible'])

    def test_durable_playlist_lyrics_follow_the_same_text_only_policy(self):
        store = PlaylistLyricsStore(self.directory)
        track = {'id': 'a' * 22, 'title': self.source.title, 'artists': [self.source.artist],
                 'album': self.source.album, 'duration': self.source.duration}
        self.assertTrue(store.save(track, self.lines, self.direct))
        value = store.find_related(self.target)
        self.assertEqual(value['lines'], self.lines)
        self.assertIsNone(value['direct'])
        self.assertIsNone(value['tracking_times'])

    def test_saved_arrangement_text_reloads_under_its_own_recording_identity(self):
        self.assertTrue(self.cache.save(self.source, self.lines, self.direct))
        value = self.cache.load_related(self.target)
        self.assertTrue(self.cache.save(self.target, value['lines'], reuse=value['reuse']))
        self.assertEqual(self.cache.load(self.target)['lines'], self.lines)
        self.assertIsNone(self.cache.load(self.target)['direct'])
        self.assertEqual(self.cache.load(self.source)['direct']['times'], [1, 15])
        self.assertIsNone(self.cache.load(replace(self.target, album='Different live album')))

    def test_same_title_requires_a_known_original_performer(self):
        source = {**vars(self.source), 'artist': 'Unrelated singer'}
        self.assertIsNone(compatibility(source, self.target))

    def test_other_albums_dates_edits_and_generic_live_titles_are_not_guessed(self):
        for target in (replace(self.target, album='Another Orchestra Album'),
                       replace(self.target, title=self.target.title.replace('Day', 'Night')),
                       replace(self.target, title='FairyTaleじゃいられない - Live'),
                       replace(self.target, title='FairyTaleじゃいられない (Orchestra Ver.)')):
            with self.subTest(target=target):
                self.assertIsNone(compatibility(vars(self.source), target))

    def test_instrumental_medleys_and_unverified_vocals_do_not_get_original_lyrics(self):
        for title in ('第1楽章 初～Pon De Beach～サマカニ!!（Day/Night 第1幕「夏の交響曲」より） - Live',
                      'キミとセミブルー（Day 第2幕「BRILLIANT STAGE」より） - Live',
                      'Unknown（Night 第2幕「BRILLIANT STAGE」より） - Live'):
            with self.subTest(title=title):self.assertIsNone(reviewed_arrangement(title, ALBUM))

    def test_reviewed_night_vocal_is_a_different_recording_of_a_known_work(self):
        source = {'title': 'ホントウノワタシ', 'artist': '田中琴葉 (CV.種田梨沙)',
                  'album': 'THE IDOLM@STER LIVE THE@TER HARMONY 06', 'duration': 323.866}
        target = Playback('ホントウノワタシ（Night 第2幕「BRILLIANT STAGE」より） - Live',
                          '野井洋児', ALBUM, duration=325)
        value = compatibility(source, target)
        self.assertFalse(value['timing_compatible'])
        self.assertFalse(value['tracking_compatible'])


if __name__ == '__main__':unittest.main()
