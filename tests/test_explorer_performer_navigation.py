"""Performer links filter the clicked identity without changing playback or credits."""
from copy import deepcopy
from types import SimpleNamespace
import unittest

from explorer_dialog import SONG_KINDS
from explorer_library import SERIES
from tests import test_explorer_dialog as fixtures


class PerformerNavigationTests(unittest.TestCase):
    setUp = fixtures.ExplorerDialogTests.setUp
    tearDown = fixtures.ExplorerDialogTests.tearDown

    def prepare_catalog(self):
        self.catalog.songs = [
            {**fixtures.song('solo'), 'artists': ['Idol (CV. Actor)']},
            {**fixtures.song('unit'), 'artists': ['Group']},
            {**fixtures.song('joint'), 'artists': ['Idol, Group']},
            {**fixtures.song('unknown'), 'artists': ['Guest Singer']},
            {**fixtures.song('unrelated'), 'artists': ['Another Idol']},
        ]
        self.original = deepcopy(self.catalog.songs)
        self.dialog.filter_songs();self.root.update()

    def ids(self):
        return {song['id'] for song in self.dialog.filtered}

    def link(self, panel, label):
        for tag, callback in panel.navigation.items():
            ranges = panel.tag_ranges(tag)
            if ranges and label in panel.get(*ranges):return callback, ranges[0]
        self.fail('Missing performer link: '+label)

    def click_text(self, panel, label):
        _, start = self.link(panel, label)
        panel.see(start);self.root.update()
        x, y, width, height = panel.bbox(start)
        x += max(1, width//2);y += height//2
        panel.event_generate('<Motion>', x=x, y=y);self.root.update()
        panel.event_generate('<ButtonPress-1>', x=x, y=y)
        panel.event_generate('<ButtonRelease-1>', x=x, y=y)
        self.root.update()

    def assert_no_playback_or_credit_changes(self):
        self.assertEqual(self.catalog.songs, self.original)
        self.spotify.search.assert_not_called();self.spotify.play.assert_not_called()
        self.spotify.open.assert_not_called()

    def test_song_credit_click_uses_clicked_person_and_clears_stale_filters(self):
        self.prepare_catalog()
        self.dialog.selected_person = self.people[0]
        self.dialog.query.set('missing')
        self.dialog.series.set(SERIES['765'])
        self.dialog.song_kind.set(SONG_KINDS['spoken'])
        self.dialog.only_favorites.set(True);self.dialog.only_playable.set(True)
        self.dialog.live_song_filter = {'ids': set(), 'title': 'Other live'}
        self.click_text(self.dialog.song_detail, 'Idol')
        self.assertIs(self.dialog.performer_filter, self.people[1])
        self.assertEqual(self.ids(), {'solo', 'joint'})
        self.assertEqual(self.dialog.query.get(), '')
        self.assertEqual(self.dialog.series.get(), SERIES[''])
        self.assertEqual(self.dialog.song_kind.get(), SONG_KINDS[''])
        self.assertFalse(self.dialog.only_favorites.get());self.assertFalse(self.dialog.only_playable.get())
        self.assertIsNone(self.dialog.live_song_filter)
        self.assertIn('아이돌', self.dialog.results_label.cget('text'))
        self.dialog.reset_filters()
        self.assertEqual(len(self.dialog.filtered), 5)
        self.assert_no_playback_or_credit_changes()

    def test_group_header_and_member_links_filter_their_own_explicit_credits(self):
        self.prepare_catalog()
        self.dialog.choose_song(self.catalog.songs[1])
        self.click_text(self.dialog.song_detail, 'Group · 그룹 멤버')
        self.assertIs(self.dialog.performer_filter, self.people[0])
        self.assertEqual(self.ids(), {'unit', 'joint'})
        self.click_text(self.dialog.song_detail, '아이돌  · CV:')
        self.assertEqual(self.dialog.performer_filter, self.people[1])
        self.assertEqual(self.ids(), {'solo', 'joint'})
        self.assert_no_playback_or_credit_changes()

    def test_compound_credits_have_separate_links_and_support_keyboard(self):
        self.prepare_catalog()
        self.dialog.choose_song(self.catalog.songs[2])
        panel = self.dialog.song_detail
        self.link(panel, 'Idol')
        _, start = self.link(panel, 'Group')
        panel.mark_set('insert', start)
        self.assertEqual(self.dialog.activate_text_link(SimpleNamespace(widget=panel)), 'break')
        self.assertEqual(self.ids(), {'unit', 'joint'})
        self.assert_no_playback_or_credit_changes()

    def test_unknown_singer_name_remains_a_clickable_exact_credit(self):
        self.prepare_catalog()
        self.dialog.choose_song(self.catalog.songs[3])
        self.click_text(self.dialog.song_detail, 'Guest Singer')
        self.assertEqual(self.ids(), {'unknown'})
        self.assertEqual(self.dialog.performer_filter['name'], 'Guest Singer')
        self.assert_no_playback_or_credit_changes()

    def test_song_list_name_click_filters_without_activating_playback(self):
        self.prepare_catalog()
        canvas = self.dialog.song_tree.canvas
        item = canvas.find_withtag('performer_name')[0]
        x1, y1, x2, y2 = canvas.bbox(item)
        x = (x1+x2)//2;y = (y1+y2)//2-int(canvas.canvasy(0))
        canvas.event_generate('<Motion>', x=x, y=y);self.root.update()
        canvas.event_generate('<ButtonPress-1>', x=x, y=y)
        canvas.event_generate('<ButtonRelease-1>', x=x, y=y);self.root.update()
        self.assertEqual(self.ids(), {'solo', 'joint'})
        self.assert_no_playback_or_credit_changes()

    def test_unit_list_title_and_detail_title_both_open_that_units_songs(self):
        self.prepare_catalog()
        self.dialog.notebook.select(self.dialog.person_tab);self.root.update()
        canvas = self.dialog.person_tree.canvas
        title = next(item for item in canvas.find_all() if canvas.type(item)=='text'
                     and canvas.itemcget(item, 'text')=='Group')
        x1, y1, x2, y2 = canvas.bbox(title)
        x = (x1+x2)//2;y = (y1+y2)//2-int(canvas.canvasy(0))
        canvas.event_generate('<ButtonPress-1>', x=x, y=y)
        canvas.event_generate('<ButtonRelease-1>', x=x, y=y);self.root.update()
        self.assertEqual(self.ids(), {'unit', 'joint'})
        self.assertEqual(self.dialog.notebook.select(), str(self.dialog.song_tab))
        self.dialog.reset_filters()
        self.dialog.notebook.select(self.dialog.person_tab);self.root.update()
        self.click_text(self.dialog.person_detail, 'Group')
        self.assertEqual(self.ids(), {'unit', 'joint'})
        self.assert_no_playback_or_credit_changes()


if __name__ == '__main__':unittest.main()
