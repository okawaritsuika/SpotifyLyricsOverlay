import copy
import unittest
from types import SimpleNamespace

from spotify_catalog import SpotifyCatalogError, parse_track_id, rank_candidates


TRACK = 'a' * 22
OTHER = 'b' * 22


def track(**changes):
    return {'id': TRACK, 'title': 'Crossing! (千早 Ver.)', 'artists': ['如月千早'],
            'album': 'Album', 'duration': 240, 'is_playable': None, **changes}


class SpotifyCatalogTests(unittest.TestCase):
    def test_track_links_accept_only_spotify_tracks(self):
        for value in (TRACK, 'spotify:track:' + TRACK, 'https://open.spotify.com/track/' + TRACK,
                      'https://open.spotify.com/intl-ja/track/' + TRACK + '?si=share',
                      'https://open.spotify.com:443/track/' + TRACK + '/', '  ' + TRACK + '  '):
            with self.subTest(value=value):self.assertEqual(parse_track_id(value), TRACK)

    def test_track_links_reject_other_resources_and_hosts(self):
        for value in ('http://open.spotify.com/track/' + TRACK, 'spotify:local:a:b:c:12',
                      'https://open.spotify.com.evil.test/track/' + TRACK,
                      'https://user@open.spotify.com/track/' + TRACK,
                      'https://open.spotify.com/album/' + TRACK,
                      'https://open.spotify.com:444/track/' + TRACK,
                      'https://open.spotify.com:bad/track/' + TRACK,
                      'https://open.spotify.com/track/' + TRACK + '/other',
                      'spotify:track:' + TRACK + '?play=true', 'x', None, 42, 'x' * 2049):
            with self.subTest(value=value):self.assertIsNone(parse_track_id(value))

    def test_exact_recording_ranks_first_without_confirming_it(self):
        wrong = track(id=OTHER, title='Crossing! (Live)', album='Live Album')
        values = rank_candidates(track(), [wrong, track()])
        self.assertEqual([item['id'] for item in values], [TRACK, OTHER])
        self.assertEqual(values[0]['match_score'], 100)
        self.assertFalse(values[0]['version_conflict'])
        self.assertTrue(values[1]['version_conflict'])
        self.assertIsNone(values[0]['is_playable'])
        self.assertNotIn('confirmed', values[0])

    def test_other_album_is_an_explicit_version_conflict(self):
        candidate = rank_candidates(track(), [track(album='Re-recording')])[0]
        self.assertTrue(candidate['version_conflict'])
        self.assertIn('앨범 확인 필요', candidate['match_reasons'])

    def test_missing_album_requires_review_without_inventing_a_conflict(self):
        candidate = rank_candidates(track(), [track(album='')])[0]
        self.assertFalse(candidate['version_conflict'])
        self.assertIn('앨범 확인 필요', candidate['match_reasons'])

    def test_uses_existing_unicode_normalization_without_stripping_versions(self):
        candidate = rank_candidates(track(), [track(title='Ｃｒｏｓｓｉｎｇ！ (千早 Ver.)')])[0]
        self.assertEqual(candidate['match_score'], 100)
        candidate = rank_candidates(track(), [track(title='Crossing!')])[0]
        self.assertTrue(candidate['version_conflict'])

    def test_object_song_uses_artist_field(self):
        song = SimpleNamespace(title=track()['title'], artist='如月千早', album='Album', duration=240)
        self.assertEqual(rank_candidates(song, [track()])[0]['match_score'], 100)

    def test_invalid_records_are_skipped_and_optional_fields_are_tolerated(self):
        candidate = track(artists=None, duration=float('nan'))
        values = rank_candidates(track(), [None, {}, track(id='bad'), candidate])
        self.assertEqual(len(values), 1)
        self.assertIn('가창자 확인 필요', values[0]['match_reasons'])
        self.assertIn('길이 확인 필요', values[0]['match_reasons'])

    def test_invalid_durations_never_receive_a_length_match(self):
        for duration in (None, True, 'bad', -1, float('nan'), float('inf'), 10 ** 1000):
            with self.subTest(duration=str(duration)[:20]):
                candidate = rank_candidates(track(), [track(duration=duration)])[0]
                self.assertNotIn('길이 일치', candidate['match_reasons'])
                wanted = rank_candidates(track(duration=duration), [track()])[0]
                self.assertNotIn('길이 일치', wanted['match_reasons'])

    def test_ranking_leaves_input_and_availability_unchanged(self):
        tracks = [track(is_playable=False), track(id=OTHER, is_playable=True)]
        original = copy.deepcopy(tracks)
        ranked = rank_candidates(track(), tracks)
        self.assertEqual(tracks, original)
        self.assertEqual([item['is_playable'] for item in ranked], [False, True])
        self.assertEqual([item['id'] for item in ranked], [TRACK, OTHER])

    def test_error_contract_is_unchanged(self):
        error = SpotifyCatalogError('로그인이 필요합니다.', 'login_required')
        self.assertEqual(str(error), '로그인이 필요합니다.')
        self.assertEqual(error.code, 'login_required')
        self.assertIsNone(error.retry_after)


if __name__ == '__main__':unittest.main()
