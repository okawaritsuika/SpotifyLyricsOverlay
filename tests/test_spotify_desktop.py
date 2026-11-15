from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import webbrowser

from artist_metadata import playback_request, request_identity
from background_lyrics import CatalogSearchError
from media import Playback
from spotify_catalog import SpotifyCatalogError
from spotify_desktop import DesktopSpotify


TRACK = 'a' * 22
OTHER = 'b' * 22


def track(**changes):
    return {'id': TRACK, 'title': 'Crossing! (千早 Ver.)', 'artists': ['如月千早'],
            'album': 'Album', 'duration': 240, **changes}


class ClockEvent:
    """Deterministic observation/cancellation without opening Spotify or sleeping."""
    def __init__(self, now, update):
        self.now = now
        self.update = update
        self.cancelled = False

    def is_set(self):return self.cancelled

    def set(self):self.cancelled = True

    def wait(self, seconds):
        self.now[0] += seconds
        if self.update:self.update()
        return self.cancelled


class DesktopSpotifyTests(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        playback = Playback(title=track()['title'], artist='如月千早', album='Album',
                            duration=240, playing=True, captured=99.8)
        self.background = SimpleNamespace(spotify_authenticated=True,
            artist_metadata=SimpleNamespace(entries={}), catalog_search=Mock())
        self.app = SimpleNamespace(playback=playback, background_lyrics=self.background)
        self.service = DesktopSpotify(self.app)
        self.cancel = ClockEvent(self.now, self.new_sample)
        self.launch = self.start_patch(patch('spotify_desktop.os.startfile', create=True))
        self.browser = self.start_patch(patch('spotify_desktop.webbrowser.open', return_value=True))
        self.controls = self.start_patch(patch('spotify_desktop.TrackPlayRequest'))
        self.start_patch(patch('spotify_desktop.time.monotonic', side_effect=lambda: self.now[0]))

    def start_patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def new_sample(self):
        self.app.playback = replace(self.app.playback, captured=self.now[0])

    def cache(self, value=None):
        request = playback_request(self.app.playback)
        self.background.artist_metadata.entries[request_identity(request)] = {
            'request': request, 'track': value or track()}

    def play(self, value=None):
        return self.service.play(value or track(), cancel=self.cancel, timeout=1)

    def test_search_reuses_existing_login_and_forwards_cancellation(self):
        self.background.catalog_search.return_value = [track()]
        self.assertTrue(self.service.connected)
        self.assertEqual(self.service.search('Song', 'Artist', 'Album', self.cancel), [track()])
        self.background.catalog_search.assert_called_once_with('Song', 'Artist', 'Album', cancel=self.cancel)
        self.background.spotify_authenticated = False
        self.assertFalse(self.service.connected)

    def test_search_errors_keep_code_for_ui(self):
        self.background.catalog_search.side_effect = CatalogSearchError('로그인이 필요합니다.', 'login_required')
        with self.assertRaises(SpotifyCatalogError) as caught:self.service.search('Song')
        self.assertEqual(caught.exception.code, 'login_required')
        self.assertEqual(str(caught.exception), '로그인이 필요합니다.')

    def test_open_uses_normal_spotify_uri(self):
        self.service.open(track())
        self.launch.assert_called_once_with('spotify:track:' + TRACK)
        self.browser.assert_not_called()

    def test_open_normalizes_supplied_spotify_url(self):
        self.service.open(track(id='https://open.spotify.com/track/' + TRACK + '?si=share'))
        self.launch.assert_called_once_with('spotify:track:' + TRACK)

    def test_work_playlist_opens_only_the_normalized_playlist_uri(self):
        self.assertEqual(self.service.open_playlist('https://open.spotify.com/playlist/' + TRACK + '?si=share'), TRACK)
        self.launch.assert_called_once_with('spotify:playlist:' + TRACK)
        self.launch.reset_mock()
        with self.assertRaises(ValueError):
            self.service.open_playlist('https://open.spotify.com/track/' + TRACK)
        self.launch.assert_not_called()
        self.controls.assert_not_called()

    def test_browser_fallback_uses_canonical_url_not_untrusted_track_url(self):
        self.launch.side_effect = OSError('no client')
        self.service.open(track(url='https://untrusted.example'))
        self.browser.assert_called_once_with('https://open.spotify.com/track/' + TRACK)

    def test_browser_fallback_does_not_report_playback_success(self):
        self.launch.side_effect = OSError('no client')
        self.cache()
        self.assertFalse(self.play())
        self.assertEqual(self.now[0], 100)
        self.browser.assert_called_once()

    def test_no_handler_reports_open_error(self):
        self.launch.side_effect = OSError('no client')
        self.browser.return_value = False
        with self.assertRaises(SpotifyCatalogError) as caught:self.service.open(track())
        self.assertEqual(caught.exception.code, 'open_failed')

    def test_browser_exception_reports_open_error(self):
        self.launch.side_effect = OSError('no client')
        self.browser.side_effect = webbrowser.Error('no browser')
        with self.assertRaises(SpotifyCatalogError) as caught:self.play()
        self.assertEqual(caught.exception.code, 'open_failed')

    def test_invalid_id_never_opens_anything(self):
        for operation in (self.service.open, self.service.play):
            with self.assertRaises(ValueError):operation(track(id='spotify:local:a:b:c:1'))
        self.launch.assert_not_called()
        self.browser.assert_not_called()

    def test_current_track_requires_fresh_sample_and_matching_metadata(self):
        self.cache()
        result = self.service.current_track()
        self.assertEqual(result['id'], TRACK)
        self.assertTrue(result['is_playable'])
        self.assertEqual(result['verification'], 'observed_playback')
        self.app.playback = replace(self.app.playback, captured=96)
        self.assertIsNone(self.service.current_track())

    def test_current_track_does_not_accept_future_or_invalid_sample(self):
        self.cache()
        for captured in (101, float('nan'), float('inf'), True):
            with self.subTest(captured=captured):
                self.app.playback = replace(self.app.playback, captured=captured)
                self.assertIsNone(self.service.current_track())

    def test_current_paused_track_has_unknown_playability(self):
        self.cache()
        self.app.playback = replace(self.app.playback, playing=False)
        result = self.service.current_track()
        self.assertIsNone(result['is_playable'])
        self.assertEqual(result['verification'], 'observed_metadata')

    def test_current_track_rejects_stale_cache_under_new_identity(self):
        self.cache()
        self.app.playback = replace(self.app.playback, album='Other Album')
        self.assertIsNone(self.service.current_track())

    def test_current_track_revalidates_cache_recording(self):
        for value in (track(album='Other Album'), track(duration=270), track(id='bad'), None):
            with self.subTest(value=value):
                request = playback_request(self.app.playback)
                self.background.artist_metadata.entries[request_identity(request)] = {'track': value}
                self.assertIsNone(self.service.current_track())

    def test_current_track_does_not_mutate_cached_artists(self):
        self.cache()
        result = self.service.current_track()
        result['artists'].append('Other Singer')
        self.assertEqual(self.service.current_track()['artists'], ['如月千早'])

    def test_matching_new_playing_sample_confirms_request(self):
        self.cache()
        self.assertTrue(self.play())
        self.assertGreater(self.now[0], 100)
        self.launch.assert_called_once_with('spotify:track:' + TRACK)

    def test_matching_complete_tags_can_confirm_when_id_is_unavailable(self):
        self.assertTrue(self.play())

    def test_unchanged_pre_request_sample_cannot_confirm(self):
        self.cache()
        self.cancel.update = None
        self.assertFalse(self.play())

    def test_sample_at_request_time_is_not_new(self):
        self.cache()
        self.cancel.update = lambda: setattr(self.app, 'playback', replace(self.app.playback, captured=100))
        self.assertFalse(self.play())

    def test_fresh_sample_must_be_actually_playing(self):
        self.cache()
        self.app.playback = replace(self.app.playback, playing=False)
        self.assertFalse(self.play())

    def test_conflicting_known_id_cannot_fall_back_to_identical_tags(self):
        self.cache(track(id=OTHER))
        self.assertFalse(self.play())

    def test_new_snapshot_with_wrong_duration_cannot_confirm(self):
        self.app.playback = replace(self.app.playback, duration=270)
        self.assertFalse(self.play())

    def test_metadata_fallback_preserves_title_version_album_and_singer(self):
        for changes in ({'title': 'Crossing! (Live)'}, {'album': 'Re-recording'},
                        {'artist': '如月千早(CV:別人)'}, {'album': ''}):
            with self.subTest(changes=changes):
                self.now[0] = 100
                self.app.playback = Playback(title=track()['title'], artist='如月千早', album='Album',
                                            duration=240, playing=True, captured=99.8)
                self.app.playback = replace(self.app.playback, **changes)
                self.assertFalse(self.play())

    def test_matching_cached_id_does_not_override_requested_version(self):
        self.cache()
        for changes in ({'title': 'Crossing! (Live)'}, {'album': 'Other Album'}, {'duration': 270}):
            with self.subTest(changes=changes):
                self.now[0] = 100
                self.assertFalse(self.play(track(**changes)))

    def test_incomplete_target_tags_cannot_confirm_metadata_only(self):
        for missing in ('title', 'artists', 'album', 'duration'):
            with self.subTest(missing=missing):
                self.now[0] = 100
                value = track();value.pop(missing)
                self.assertFalse(self.play(value))

    def test_joined_artist_list_is_supported_without_discarding_credits(self):
        self.app.playback = replace(self.app.playback, artist='如月千早 / 天海春香')
        self.assertTrue(self.play(track(artists=['如月千早', '天海春香'])))

    def test_cancel_before_open_has_no_side_effect(self):
        self.cancel.set()
        self.assertFalse(self.play())
        self.launch.assert_not_called()
        self.browser.assert_not_called()

    def test_cancel_while_waiting_cannot_confirm_a_late_sample(self):
        def update():
            self.new_sample()
            self.cancel.set()
        self.cancel.update = update
        self.assertFalse(self.play())
        self.launch.assert_called_once()

    def test_cancel_during_recording_check_cannot_report_success(self):
        def current(*args):
            self.cancel.set()
            return track()
        with patch.object(self.service, '_current_track', side_effect=current):
            self.assertFalse(self.play())

    def test_invalid_timeout_cannot_start_playback(self):
        for timeout in (0, -1, 31, True, None, '1', float('nan'), float('inf')):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):self.service.play(track(), timeout=timeout)
        self.launch.assert_not_called()

    def test_connection_error_snapshot_cannot_confirm(self):
        self.cache()
        self.app.playback = replace(self.app.playback, error='Spotify disconnected')
        self.assertFalse(self.play())

    def test_uri_without_autoplay_requests_targeted_control_and_cleans_it_up(self):
        self.app.playback = replace(self.app.playback, playing=False)
        self.assertFalse(self.play())
        self.controls.assert_called_once_with(track())
        self.controls.return_value.close.assert_called_once()

    def test_autoplay_does_not_invoke_row_again(self):
        self.assertTrue(self.play())
        self.controls.assert_not_called()

    def test_closed_service_does_not_launch_spotify(self):
        self.service.close()
        self.assertFalse(self.play())
        self.launch.assert_not_called()

    def test_close_synchronously_stops_control_before_work_finishes(self):
        self.app.playback = replace(self.app.playback, playing=False)
        def update():
            if self.now[0] >= 100.8:
                self.service.close()
                self.controls.return_value.close.assert_called_once()
        self.cancel.update = update
        self.assertFalse(self.play())
        self.controls.return_value.close.assert_called_once()

    def test_cancellation_closes_targeted_control(self):
        self.app.playback = replace(self.app.playback, playing=False)
        def update():
            if self.now[0] >= 100.8:self.cancel.set()
        self.cancel.update = update
        self.assertFalse(self.play())
        self.controls.return_value.close.assert_called_once()

    def test_targeted_control_success_still_requires_media_confirmation(self):
        self.app.playback = replace(self.app.playback, playing=False)
        def update():
            self.app.playback = replace(self.app.playback, captured=self.now[0],
                                        playing=self.now[0] >= 100.8)
        self.cancel.update = update
        self.assertTrue(self.play())
        self.controls.return_value.close.assert_called_once()


if __name__ == '__main__':unittest.main()
