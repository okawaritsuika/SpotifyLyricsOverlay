"""Release details keep edition evidence separate and never change playback."""
import copy
import tkinter as tk
import unittest
from unittest.mock import patch

import test_explorer_dialog as fixtures


class ExplorerReleaseTests(unittest.TestCase):
    setUp = fixtures.ExplorerDialogTests.setUp
    tearDown = fixtures.ExplorerDialogTests.tearDown

    def panel(self):
        return next(widget for widget in fixtures.descendants(self.dialog.release_window) if isinstance(widget, tk.Text))

    def test_editions_keep_distinct_dates_formats_and_exact_source_links(self):
        self.songs[0]['releases'] = [
            {'id': 'physical', 'title': 'Same album', 'date': '2023-01-18',
             'date_original': '2013-01-18', 'formats': ['CD', 'Blu-ray'],
             'url': 'https://musicbrainz.org/release/physical',
             'format_source_url': 'https://musicbrainz.org/release/physical',
             'date_source_url': 'https://label.example/album'},
            {'id': 'digital', 'title': 'Same album', 'date': '2023-01-19',
             'formats': ['Digital Media'], 'url': 'https://musicbrainz.org/release/digital'},
            {'id': 'unknown', 'title': 'Other album', 'url': 'https://example.test/unknown'}]
        before = copy.deepcopy(self.songs[0])
        self.dialog.show_releases(self.songs[0]);self.root.update()
        panel = self.panel();content = panel.get('1.0', 'end')
        self.assertEqual(content.count('Same album'), 2)
        for value in ('2023-01-18', '2023-01-19', '이전 자료의 표기 2013-01-18',
                      'CD + Blu-ray', '디지털', '발매일  미확인  ·  매체 미확인'):
            self.assertIn(value, content)
        with patch('explorer_dialog.open_url') as opened:
            for callback in panel.navigation.values():callback()
        self.assertEqual([call.args[0] for call in opened.call_args_list], [
            'https://musicbrainz.org/release/physical', 'https://label.example/album',
            'https://musicbrainz.org/release/digital', 'https://example.test/unknown'])
        self.assertEqual(self.songs[0], before)
        self.spotify.search.assert_not_called();self.spotify.play.assert_not_called()

    def test_detail_link_keeps_original_recording_and_reopening_replaces_window(self):
        self.songs[0]['releases'] = [{'title': 'First edition', 'date': '2020'}]
        self.dialog.choose_song(self.songs[0])
        text = self.dialog.song_detail
        position = text.search('발매 정보 · 1개 판본', '1.0')
        self.assertTrue(position)
        callback = next(text.navigation[tag] for tag in text.tag_names(position) if tag in text.navigation)
        self.dialog.choose_song(self.songs[1]);callback();self.root.update()
        self.assertIn('First edition', self.panel().get('1.0', 'end'))
        self.assertIs(self.dialog.selected_song, self.songs[1])
        old = self.dialog.release_window
        self.songs[1]['releases'] = [{'title': 'Second edition'}]
        self.dialog.show_releases(self.songs[1]);self.root.update()
        self.assertFalse(old.winfo_exists())
        self.assertNotIn('First edition', self.panel().get('1.0', 'end'))
        self.assertFalse(self.dialog.release_window.attributes('-topmost'))
        self.assertFalse(self.dialog.release_window.transient())
        child = self.dialog.release_window
        self.dialog.close()
        self.assertFalse(child.winfo_exists())

    def test_many_editions_scroll_and_invalid_sources_never_become_links(self):
        self.songs[0]['releases'] = [{'title': f'Edition {index}', 'date': '2020',
            'url': 'javascript:alert(1)', 'date_source_url': 'https://user:password@example.test/',
            'format_source_url': 'https://[broken'} for index in range(30)]
        self.dialog.show_releases(self.songs[0])
        self.dialog.release_window.geometry('480x340');self.root.update()
        panel = self.panel()
        self.assertIn('Edition 29', panel.get('1.0', 'end'))
        self.assertEqual(panel.navigation, {})
        panel.yview_moveto(1);self.root.update()
        self.assertGreater(panel.yview()[0], 0)
        self.assertGreater(panel.winfo_width(), 100)

    def test_only_fully_identical_release_rows_are_collapsed(self):
        release = {'id': 'same-id', 'title': 'Same edition', 'date': '2020', 'formats': ['CD']}
        releases = [release, copy.deepcopy(release), {**release, 'date': '2021'},
                    {**release, 'formats': ['Digital Media']},
                    {**release, 'date_source_url': 'https://label.example/date'}]
        self.songs[0]['releases'] = releases
        before = copy.deepcopy(releases)
        self.dialog.choose_song(self.songs[0])
        self.assertIn('발매 정보 · 4개 판본', self.dialog.song_detail.get('1.0', 'end'))
        self.dialog.show_releases(self.songs[0])
        self.assertEqual(self.panel().get('1.0', 'end').count('Same edition'), 4)
        self.assertEqual(releases, before)


if __name__ == '__main__':
    unittest.main()
