"""Opening the dictionary follows playback without changing recording links."""
from types import SimpleNamespace
import unittest

from explorer_library import SERIES
from tests import test_explorer_dialog as fixtures
from tests.test_explorer_dialog import song, track


class CurrentSongTests(unittest.TestCase):
    setUp = fixtures.ExplorerDialogTests.setUp
    tearDown = fixtures.ExplorerDialogTests.tearDown

    def playback(self, **changes):
        values = dict(title='Song b', artist='Artist', album='Album', duration=240)
        values.update(changes)
        self.app.playback = SimpleNamespace(**values)

    def test_open_selects_current_song_and_clears_unrelated_filters(self):
        self.playback()
        self.dialog.series.set(SERIES['sidem'])
        self.dialog.only_favorites.set(True)
        self.dialog.only_playable.set(True)
        self.dialog.query.set('unrelated')
        self.dialog.performer_filter = self.people[0]
        self.dialog.live_song_filter = {'ids': set(), 'title': 'Other live'}
        self.dialog.notebook.select(self.dialog.live_tab)
        self.assertTrue(self.dialog.focus_current_song())
        self.root.update()
        self.assertEqual(self.dialog.selected_song['id'], 'b')
        self.assertEqual(self.dialog.song_tree.selection(), ('b',))
        self.assertIn('Song b', self.dialog.song_detail.get('1.0', 'end'))
        self.assertEqual(self.dialog.notebook.select(), str(self.dialog.song_tab))
        self.assertFalse(self.dialog.only_playable.get())
        self.assertIsNone(self.dialog.live_song_filter)
        self.spotify.search.assert_not_called()
        self.spotify.play.assert_not_called()

    def test_no_playback_keeps_existing_browsing(self):
        self.dialog.query.set('Song a')
        self.dialog.notebook.select(self.dialog.person_tab)
        self.assertFalse(self.dialog.focus_current_song())
        self.assertEqual(self.dialog.query.get(), 'Song a')
        self.assertEqual(self.dialog.notebook.select(), str(self.dialog.person_tab))

    def test_current_song_button_returns_after_browsing_without_playing_or_linking(self):
        self.playback()
        self.dialog.query.set('Song a');self.dialog.filter_songs()
        self.dialog.current_song_button.invoke()
        self.assertEqual(self.dialog.selected_song['id'], 'b')
        self.assertEqual(self.dialog.library.value['spotify'], {})
        self.spotify.search.assert_not_called();self.spotify.play.assert_not_called()

    def test_current_song_button_explains_missing_playback_without_resetting_search(self):
        self.dialog.query.set('Song a')
        self.dialog.current_song_button.invoke()
        self.assertEqual(self.dialog.query.get(), 'Song a')
        self.assertIn('현재 Spotify 곡을 확인할 수 없습니다', self.dialog.status.get())

    def test_new_window_starts_on_current_song(self):
        self.dialog.close()
        self.playback()
        self.dialog = fixtures.ExplorerDialog(self.app)
        self.app.explorer_dialog = self.dialog
        self.root.update()
        self.assertEqual(self.dialog.selected_song['id'], 'b')
        self.assertEqual(self.dialog.song_tree.selection(), ('b',))

    def test_album_disambiguates_same_title(self):
        duplicate = dict(song('c'), title='Song b', album='Other Album')
        self.songs.append(duplicate)
        self.playback(album='Other Album')
        self.assertTrue(self.dialog.focus_current_song())
        self.assertEqual(self.dialog.selected_song['id'], 'c')

    def test_ambiguous_metadata_only_searches_and_does_not_link(self):
        self.songs.append(dict(song('c'), title='Song b'))
        self.playback()
        self.assertFalse(self.dialog.focus_current_song())
        self.assertEqual(self.dialog.query.get(), 'Song b')
        self.assertIn('확인해 주세요', self.dialog.status.get())
        self.assertIsNone(self.dialog.library.linked_track('b'))
        self.assertIsNone(self.dialog.library.linked_track('c'))

    def test_existing_spotify_id_beats_ambiguous_titles(self):
        self.songs.append(dict(song('c'), title='Song b'))
        self.dialog.library.link_spotify('c', track('c'))
        self.spotify.current_track.return_value = track('c')
        self.playback()
        self.assertTrue(self.dialog.focus_current_song())
        self.assertEqual(self.dialog.selected_song['id'], 'c')

    def test_current_recording_is_visible_when_another_duplicate_is_favorite(self):
        self.songs.append(dict(song('c'), title='Song b'))
        self.dialog.library.toggle_favorite('b')
        self.dialog.library.set_hide_duplicates(True)
        self.dialog.hide_duplicates.set(True)
        self.dialog.library.link_spotify('c', track('c'))
        self.spotify.current_track.return_value = track('c')
        self.playback()
        self.assertTrue(self.dialog.focus_current_song())
        self.assertEqual(self.dialog.song_tree.selection(), ('c',))
        self.assertTrue(self.dialog.library.hide_duplicates)
        self.assertTrue(self.dialog.hide_duplicates.get())
        self.assertEqual(sum(item['title'] == 'Song b' for item in self.dialog.filtered), 1)
        self.assertEqual({item['id'] for item in self.dialog.duplicate_groups['c']}, {'b', 'c'})

    def test_title_without_artist_or_album_evidence_is_only_search(self):
        self.playback(artist='Different singer', album='Different album')
        self.assertFalse(self.dialog.focus_current_song())
        self.assertIn('확인해 주세요', self.dialog.status.get())

    def test_matching_artist_does_not_hide_a_known_album_conflict(self):
        self.playback(album='Other Album')
        self.assertFalse(self.dialog.focus_current_song())
        self.assertIn('확인해 주세요', self.dialog.status.get())

    def test_matching_album_does_not_hide_a_known_singer_conflict(self):
        self.playback(artist='Different singer')
        self.assertFalse(self.dialog.focus_current_song())
        self.assertIn('확인해 주세요', self.dialog.status.get())

    def test_explicit_same_recording_release_remains_valid_album_evidence(self):
        self.songs[1]['releases'] = [{'title': 'Other Album'}]
        self.playback(album='Other Album')
        self.assertTrue(self.dialog.focus_current_song())
        self.assertEqual(self.dialog.selected_song['id'], 'b')

    def test_instrumental_or_other_version_does_not_match(self):
        self.playback(title='Song b - Instrumental')
        self.assertFalse(self.dialog.focus_current_song())
        self.assertEqual(self.dialog.filtered, [])

    def test_duration_conflict_does_not_claim_current_recording(self):
        self.songs[1]['duration'] = 180
        self.playback()
        self.assertFalse(self.dialog.focus_current_song())

    def test_metadata_update_does_not_interrupt_user_browsing(self):
        self.playback()
        self.dialog.focus_current_song()
        self.dialog.reset_filters()
        self.dialog.choose_song(self.songs[0])
        self.playback(title='Song instrumental')
        self.root.update()
        self.assertEqual(self.dialog.selected_song['id'], 'a')


if __name__ == '__main__':unittest.main()
