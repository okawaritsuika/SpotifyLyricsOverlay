"""Sorting applies to full filtered results and leaves playback and user data alone."""
from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

from explorer_dialog import ExplorerDialog, PAGE_SIZE
from explorer_library import ExplorerLibrary, SONG_SORTS
from tests import test_explorer_dialog as fixtures


class SongSortTests(unittest.TestCase):
    setUp = fixtures.ExplorerDialogTests.setUp
    tearDown = fixtures.ExplorerDialogTests.tearDown

    def prepare_catalog(self):
        self.catalog.songs = [
            {**fixtures.song('old'), 'title': 'Zulu', 'artists': ['Idol'],
             'first_release_date': '2010-07-01', 'duration': 220},
            {**fixtures.song('new'), 'title': 'Alpha', 'artists': ['Group'],
             'first_release_date': '2025', 'duration': 190},
            {**fixtures.song('unknown'), 'title': 'Beta', 'artists': ['Idol']},
        ]
        self.counts = {'old': 7, 'new': 2, 'unknown': 0}
        self.live_catalog.count_for_song = Mock(side_effect=lambda song:self.counts[song['id']])
        self.before = deepcopy(self.catalog.songs)
        self.dialog.filter_songs();self.root.update()

    def select_sort(self, key):
        self.dialog.song_sort.set(SONG_SORTS[key])
        self.dialog.sort_combo.event_generate('<<ComboboxSelected>>')
        self.root.update()

    def ids(self):
        return [song['id'] for song in self.dialog.filtered]

    def test_every_sort_is_selectable_and_shows_its_comparison_value(self):
        self.prepare_catalog()
        expected = {'title_asc': ['new', 'unknown', 'old'],
                    'title_desc': ['old', 'unknown', 'new'],
                    'release_desc': ['new', 'old', 'unknown'],
                    'release_asc': ['old', 'new', 'unknown'],
                    'live_desc': ['old', 'new', 'unknown'],
                    'live_asc': ['unknown', 'new', 'old'],
                    'artist_asc': ['new', 'unknown', 'old'],
                    'duration_desc': ['old', 'new', 'unknown'],
                    'duration_asc': ['new', 'old', 'unknown'],
                    'default': ['old', 'new', 'unknown']}
        self.assertEqual(tuple(self.dialog.sort_combo['values']), tuple(SONG_SORTS.values()))
        for key, identities in expected.items():
            with self.subTest(sort=key):
                self.select_sort(key)
                self.assertEqual(self.ids(), identities)
                self.assertEqual(list(self.dialog.song_tree.get_children()), identities)
                self.assertEqual(self.dialog.library.song_sort, key)
                self.assertEqual(ExplorerLibrary(self.directory).song_sort, key)
                badge = self.dialog.song_tree.item(identities[0], 'badge')
                if key=='release_desc':self.assertIn('발매 2025', badge)
                if key=='release_asc':self.assertIn('발매 2010-07-01', badge)
                if key=='live_desc':self.assertIn('라이브 7회', badge)
                if key=='live_asc':self.assertIn('가창 기록 없음', badge)
                if key=='duration_desc':self.assertIn('3:40', badge)
        self.assertEqual(self.catalog.songs, self.before)
        self.spotify.search.assert_not_called();self.spotify.play.assert_not_called()

    def test_sort_keeps_active_filters_and_reset_keeps_sort_preference(self):
        self.prepare_catalog()
        self.dialog.show_performer_songs(self.people[1])
        self.select_sort('title_asc')
        self.assertEqual(self.ids(), ['unknown', 'old'])
        self.assertEqual(self.dialog.performer_filter, self.people[1])
        self.dialog.query.set('Zulu');self.dialog.filter_songs()
        self.select_sort('release_desc')
        self.assertEqual(self.ids(), ['old'])
        self.assertEqual(self.dialog.query.get(), 'Zulu')
        self.dialog.query.set('');self.dialog.library.toggle_favorite('old')
        self.dialog.only_favorites.set(True);self.dialog.filter_songs()
        self.select_sort('live_desc')
        self.assertEqual(self.ids(), ['old']);self.assertTrue(self.dialog.only_favorites.get())
        self.dialog.reset_filters()
        self.assertEqual(self.dialog.song_sort.get(), SONG_SORTS['live_desc'])
        self.assertEqual(self.ids(), ['old', 'new', 'unknown'])

    def test_changing_sort_starts_at_first_page_and_sorts_beyond_visible_rows(self):
        self.catalog.songs = [{**fixtures.song(str(index)), 'title': f'Title {index:03}'}
                              for index in range(PAGE_SIZE*2+5)]
        self.dialog.filter_songs();self.dialog.change_page(2)
        self.assertEqual(self.dialog.page, 2)
        self.select_sort('title_desc')
        self.assertEqual(self.dialog.page, 0)
        self.assertEqual(self.ids(), [str(index) for index in range(PAGE_SIZE*2+4, -1, -1)])
        self.assertEqual(self.dialog.song_tree.get_children()[0], str(PAGE_SIZE*2+4))
        self.dialog.change_page(2)
        self.assertEqual(self.dialog.song_tree.get_children(), ('4', '3', '2', '1', '0'))

    def test_live_counts_are_reused_then_refreshed_when_catalogs_change(self):
        self.prepare_catalog();self.select_sort('live_desc')
        self.assertEqual(self.live_catalog.count_for_song.call_count, 3)
        self.select_sort('live_asc');self.dialog.filter_songs()
        self.assertEqual(self.live_catalog.count_for_song.call_count, 3)
        self.counts['unknown'] = 9
        self.live_catalog.events = list(self.live_catalog.events)
        self.select_sort('live_desc')
        self.assertEqual(self.ids(), ['unknown', 'old', 'new'])
        self.assertEqual(self.live_catalog.count_for_song.call_count, 6)
        self.counts['new'] = 10
        self.catalog.songs = list(self.catalog.songs)
        self.dialog.filter_songs()
        self.assertEqual(self.ids(), ['new', 'unknown', 'old'])
        self.assertEqual(self.live_catalog.count_for_song.call_count, 9)
        self.counts['old'] = 15
        self.dialog.reload_live_records()
        self.assertEqual(self.ids(), ['old', 'new', 'unknown'])
        self.assertEqual(self.live_catalog.count_for_song.call_count, 12)

    def test_duplicate_preference_and_links_survive_sort_changes(self):
        self.prepare_catalog()
        self.catalog.songs.append({**self.catalog.songs[0], 'id':'reissue', 'album':'Reissue'})
        self.dialog.library.link_spotify('old', fixtures.track())
        self.dialog.library.toggle_favorite('old')
        self.dialog.hide_duplicates.set(True);self.dialog.toggle_duplicates()
        self.select_sort('release_desc')
        self.assertEqual(self.ids(), ['new', 'old', 'unknown'])
        self.assertEqual(self.dialog.hidden_duplicate_count, 1)
        self.assertTrue(self.dialog.library.hide_duplicates)
        self.assertTrue(self.dialog.library.is_favorite('old'))
        self.assertEqual(self.dialog.library.linked_track('old')['id'], 'a'*22)

    def test_saved_sort_is_restored_when_dictionary_reopens(self):
        self.prepare_catalog();self.select_sort('release_desc')
        self.dialog.close()
        self.dialog = ExplorerDialog(self.app);self.app.explorer_dialog = self.dialog
        self.root.update()
        self.assertEqual(self.dialog.song_sort.get(), SONG_SORTS['release_desc'])
        self.assertEqual(self.ids(), ['new', 'old', 'unknown'])

    def test_failed_preference_save_restores_selector_and_previous_results(self):
        self.prepare_catalog();self.select_sort('title_desc')
        old = self.ids()
        with patch.object(self.dialog.library, 'set_song_sort', side_effect=OSError('Disk full')):
            self.select_sort('release_desc')
        self.assertEqual(self.dialog.song_sort.get(), SONG_SORTS['title_desc'])
        self.assertEqual(self.ids(), old)

    def test_sort_control_fits_minimum_window_without_overlapping_results(self):
        self.dialog.window.geometry('900x620');self.root.update()
        combo, label = self.dialog.sort_combo, self.dialog.results_label
        self.assertGreater(combo.winfo_width(), 150)
        self.assertLessEqual(combo.winfo_x()+combo.winfo_width(), combo.master.winfo_width())
        self.assertLessEqual(label.winfo_x()+label.winfo_width(), combo.winfo_x())


if __name__ == '__main__':unittest.main()
