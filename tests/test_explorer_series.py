"""Series symbols navigate to their recordings without changing playback or credits."""
from copy import deepcopy
from types import SimpleNamespace
import unittest

from explorer_dialog import SONG_KINDS
from explorer_library import SERIES
from tests import test_explorer_dialog as fixtures


class SeriesNavigationTests(unittest.TestCase):
    setUp = fixtures.ExplorerDialogTests.setUp
    tearDown = fixtures.ExplorerDialogTests.tearDown

    def catalog_by_series(self):
        self.catalog.songs = [
            {**fixtures.song('allstars'), 'series': ['765']},
            {**fixtures.song('million'), 'series': ['million']},
            {**fixtures.song('joint'), 'series': ['765', 'million']},
            {**fixtures.song('shiny'), 'series': ['shiny']},
            {**fixtures.song('backing', 'instrumental'), 'series': ['million']},
            {**fixtures.song('other'), 'series': ['other']},
        ]
        self.dialog.filter_songs()
        return deepcopy(self.catalog.songs)

    def selected_ids(self):
        return {song['id'] for song in self.dialog.filtered}

    def series_link(self, panel, key):
        for tag, callback in panel.navigation.items():
            ranges = panel.tag_ranges(tag)
            if ranges and panel.get(ranges[0], ranges[1]).strip() in (SERIES[key], SERIES[key] + ' 곡 보기'):
                return tag, callback, ranges[0]
        self.fail('Missing series link: ' + key)

    def assert_selected_button(self, key):
        self.assertEqual(self.dialog.series.get(), SERIES[key])
        for identity, button in self.dialog.series_buttons.items():
            self.assertEqual(button.cget('relief'), 'sunken' if identity == key else 'flat')

    def test_buttons_show_distinct_symbols_and_filter_joint_recordings(self):
        original = self.catalog_by_series()
        self.assertEqual(set(self.dialog.series_buttons), set(SERIES))
        images = []
        for key, button in self.dialog.series_buttons.items():
            if key:
                image = str(button.cget('image'))
                self.assertTrue(image, key)
                self.assertGreater(int(self.root.tk.call('image', 'width', image)), 0)
                self.assertGreater(int(self.root.tk.call('image', 'height', image)), 0)
                images.append(image)
        self.assertEqual(len(set(images)), len(images))
        for key, expected in [('765', {'allstars', 'joint'}),
                              ('million', {'million', 'joint', 'backing'}),
                              ('shiny', {'shiny'}), ('other', {'other'}),
                              ('sidem', set()), ('', {row['id'] for row in original})]:
            with self.subTest(key=key):
                self.dialog.series_buttons[key].invoke()
                self.root.update()
                self.assertEqual(self.selected_ids(), expected)
                self.assert_selected_button(key)
        self.assertEqual(self.catalog.songs, original)
        self.assertEqual(self.dialog.library.value['spotify'], {})
        self.spotify.search.assert_not_called()
        self.spotify.play.assert_not_called()

    def test_series_navigation_clears_previous_scope_and_preserves_duplicate_preference(self):
        self.catalog_by_series()
        duplicate = {**self.catalog.songs[1], 'id': 'million-reissue', 'duration': 181}
        self.catalog.songs[1]['duration'] = 180
        self.catalog.songs.append(duplicate)
        self.dialog.library.set_hide_duplicates(True)
        self.dialog.hide_duplicates.set(True)
        self.dialog.query.set('no such title')
        self.dialog.performer_filter = self.people[0]
        self.dialog.live_song_filter = {'ids': set(), 'title': 'Different live'}
        self.dialog.song_kind.set(SONG_KINDS['spoken'])
        self.dialog.only_favorites.set(True)
        self.dialog.only_playable.set(True)
        self.dialog.notebook.select(self.dialog.person_tab)
        self.dialog.show_series_songs('million')
        self.root.update()
        self.assertEqual(self.dialog.notebook.select(), str(self.dialog.song_tab))
        self.assertEqual(self.dialog.query.get(), '')
        self.assertIsNone(self.dialog.performer_filter)
        self.assertIsNone(self.dialog.live_song_filter)
        self.assertEqual(self.dialog.song_kind.get(), SONG_KINDS[''])
        self.assertFalse(self.dialog.only_favorites.get())
        self.assertFalse(self.dialog.only_playable.get())
        self.assertTrue(self.dialog.hide_duplicates.get())
        self.assertTrue(self.dialog.library.hide_duplicates)
        self.assertEqual(len(self.dialog.filtered), 3)
        self.assertEqual(self.dialog.hidden_duplicate_count, 1)
        self.assertTrue({'joint', 'backing'}.issubset(self.selected_ids()))
        self.assert_selected_button('million')
        self.spotify.search.assert_not_called()
        self.spotify.play.assert_not_called()

    def test_search_favorites_and_kind_can_narrow_the_selected_series(self):
        self.catalog_by_series()
        self.dialog.show_series_songs('million')
        self.dialog.query.set('Song joint')
        self.dialog.filter_songs()
        self.assertEqual(self.selected_ids(), {'joint'})
        self.dialog.query.set('')
        self.dialog.library.toggle_favorite('million')
        self.dialog.library.toggle_favorite('shiny')
        self.dialog.only_favorites.set(True)
        self.dialog.filter_songs()
        self.assertEqual(self.selected_ids(), {'million'})
        self.dialog.only_favorites.set(False)
        self.dialog.song_kind.set(SONG_KINDS['instrumental'])
        self.dialog.filter_songs()
        self.assertEqual(self.selected_ids(), {'backing'})
        self.assert_selected_button('million')

    def test_indirect_changes_and_reset_update_the_selected_symbol(self):
        self.catalog_by_series()
        self.dialog.series.set(SERIES['765'])
        self.dialog.filter_songs()
        self.assert_selected_button('765')
        self.assertEqual(self.selected_ids(), {'allstars', 'joint'})
        self.dialog.reset_filters()
        self.assert_selected_button('')
        self.assertEqual(len(self.dialog.filtered), len(self.catalog.songs))
        self.dialog.show_series_songs('million')
        self.dialog.selected_person = self.people[0]
        self.dialog.person_songs()
        self.assert_selected_button('')
        self.assertIs(self.dialog.performer_filter, self.people[0])

    def test_group_buttons_fit_the_minimum_window_without_overlapping(self):
        self.dialog.window.geometry('900x620')
        self.root.update()
        bounds = []
        for button in self.dialog.series_buttons.values():
            self.assertTrue(button.winfo_ismapped())
            self.assertGreater(button.winfo_width(), 1)
            self.assertGreater(button.winfo_height(), 1)
            parent = button.master
            self.assertGreaterEqual(button.winfo_x(), 0)
            self.assertGreaterEqual(button.winfo_y(), 0)
            self.assertLessEqual(button.winfo_x() + button.winfo_width(), parent.winfo_width())
            self.assertLessEqual(button.winfo_y() + button.winfo_height(), parent.winfo_height())
            bounds.append((button.winfo_rootx(), button.winfo_rooty(),
                           button.winfo_rootx() + button.winfo_width(),
                           button.winfo_rooty() + button.winfo_height()))
        for index, first in enumerate(bounds):
            for second in bounds[index + 1:]:
                self.assertFalse(first[0] < second[2] and second[0] < first[2]
                                 and first[1] < second[3] and second[1] < first[3])

    def test_joint_song_header_has_two_symbols_and_mouse_navigation(self):
        original = self.catalog_by_series()
        joint = next(song for song in self.catalog.songs if song['id'] == 'joint')
        self.dialog.choose_song(joint)
        self.root.update()
        panel = self.dialog.song_detail
        self.assertEqual(len(panel.image_names()), 2)
        self.series_link(panel, '765')
        _, _, start = self.series_link(panel, 'million')
        position = panel.search(SERIES['million'], start, stopindex='end')
        panel.see(position)
        self.root.update()
        box = panel.bbox(position)
        self.assertIsNotNone(box)
        x, y, width, height = box
        panel.event_generate('<Motion>', x=x + max(1, width // 2), y=y + height // 2)
        self.root.update()
        panel.event_generate('<ButtonPress-1>', x=x + max(1, width // 2), y=y + height // 2)
        panel.event_generate('<ButtonRelease-1>', x=x + max(1, width // 2), y=y + height // 2)
        self.root.update()
        self.assert_selected_button('million')
        self.assertEqual(self.selected_ids(), {'million', 'joint', 'backing'})
        self.assertEqual(self.catalog.songs, original)
        self.spotify.play.assert_not_called()

    def test_person_header_symbol_supports_keyboard_navigation(self):
        self.catalog_by_series()
        self.dialog.notebook.select(self.dialog.person_tab)
        self.dialog.person_tree.selection_set(self.people[0]['id'])
        self.dialog.select_person()
        self.root.update()
        panel = self.dialog.person_detail
        self.assertEqual(len(panel.image_names()), 1)
        _, _, start = self.series_link(panel, 'shiny')
        panel.mark_set('insert', start)
        self.assertEqual(self.dialog.activate_text_link(SimpleNamespace(widget=panel)), 'break')
        self.root.update()
        self.assertEqual(self.dialog.notebook.select(), str(self.dialog.song_tab))
        self.assertEqual(self.selected_ids(), {'shiny'})
        self.assert_selected_button('shiny')
        self.spotify.search.assert_not_called()
        self.spotify.play.assert_not_called()


if __name__ == '__main__':
    unittest.main()
