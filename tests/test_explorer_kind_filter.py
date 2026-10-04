"""Dictionary type filters preserve navigation and explicit playback intent."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from explorer_dialog import SONG_KINDS
from tests import test_explorer_dialog as fixtures


class KindFilterTests(unittest.TestCase):
    setUp = fixtures.ExplorerDialogTests.setUp
    tearDown = fixtures.ExplorerDialogTests.tearDown

    def select_kind(self, key):
        self.dialog.song_kind.set(SONG_KINDS[key])
        self.dialog.kind_filter.event_generate('<<ComboboxSelected>>')
        self.root.update()

    def test_type_filter_keeps_each_recording_and_combines_with_search_and_favorites(self):
        self.catalog.songs.extend([fixtures.song('spoken', 'spoken'), fixtures.song('unknown', 'unknown')])
        for key, expected in [('song', {'a', 'b'}), ('instrumental', {'instrumental'}),
                              ('spoken', {'spoken'}), ('unknown', {'unknown'})]:
            self.select_kind(key)
            self.assertEqual({song['id'] for song in self.dialog.filtered}, expected)
        self.select_kind('song')
        self.dialog.library.toggle_favorite('b')
        self.dialog.only_favorites.set(True);self.dialog.filter_songs()
        self.assertEqual([song['id'] for song in self.dialog.filtered], ['b'])
        self.dialog.query.set('No such recording');self.dialog.filter_songs()
        self.assertEqual(self.dialog.filtered, [])
        self.dialog.reset_filters()
        self.assertEqual(self.dialog.song_kind.get(), SONG_KINDS[''])
        self.assertEqual(len(self.dialog.filtered), 5)
        self.assertTrue(self.dialog.library.is_favorite('b'))

    def test_legacy_type_field_and_missing_type_are_visible_in_correct_filter(self):
        legacy = fixtures.song('legacy');legacy.pop('kind');legacy['type'] = 'instrumental'
        unknown = fixtures.song('missing');unknown.pop('kind')
        self.catalog.songs.extend([legacy, unknown])
        self.select_kind('instrumental')
        self.assertEqual({song['id'] for song in self.dialog.filtered}, {'instrumental', 'legacy'})
        self.select_kind('unknown')
        self.assertEqual([song['id'] for song in self.dialog.filtered], ['missing'])

    def test_current_song_and_live_navigation_clear_unrelated_type_filter(self):
        self.select_kind('instrumental')
        self.app.playback = SimpleNamespace(title='Song b', artist='Artist', album='Album', duration=240)
        self.assertTrue(self.dialog.focus_current_song())
        self.assertEqual(self.dialog.song_kind.get(), SONG_KINDS[''])
        self.assertEqual(self.dialog.selected_song['id'], 'b')
        self.select_kind('instrumental')
        self.dialog.show_live_song(self.live_events[0], {'title': 'Song a'}, [self.songs[0]])
        self.assertEqual([song['id'] for song in self.dialog.filtered], ['a'])
        self.assertEqual(self.dialog.song_kind.get(), SONG_KINDS[''])
        self.spotify.search.assert_not_called();self.spotify.play.assert_not_called()

    def test_explicit_instrumental_discovery_uses_filter_without_playing(self):
        self.assertEqual({song['id'] for song in self.dialog.discovery_songs()}, {'a', 'b'})
        self.select_kind('instrumental')
        self.dialog.discover()
        self.assertEqual(self.dialog.selected_song['id'], 'instrumental')
        self.assertEqual(self.dialog.library.value['recent'], ['instrumental'])
        self.assertIn('연주 · 반주', self.dialog.song_detail.get('1.0', 'end'))
        self.spotify.search.assert_not_called();self.spotify.play.assert_not_called()

    def test_filter_controls_fit_minimum_window_width(self):
        self.dialog.window.geometry('900x620')
        self.root.update()
        parent = self.dialog.kind_filter.master
        for widget in parent.winfo_children():
            self.assertGreater(widget.winfo_width(), 1)
            self.assertLessEqual(widget.winfo_x() + widget.winfo_width(), parent.winfo_width())

    def test_reviewed_type_source_is_opened_from_recording_detail(self):
        url = 'https://columbia.jp/prod-info/COCX-37926-7/'
        self.songs[2]['kind_source_urls'] = [url]
        self.select_kind('instrumental')
        self.assertIn('유형 분류 근거 · columbia.jp', self.dialog.song_detail.get('1.0', 'end'))
        with patch('explorer_dialog.open_url') as opened:
            detail = self.dialog.song_detail
            next(command for tag, command in detail.navigation.items()
                 if '유형 분류 근거' in detail.get(*detail.tag_ranges(tag)))()
            opened.assert_called_once_with(url)


if __name__ == '__main__':
    unittest.main()
