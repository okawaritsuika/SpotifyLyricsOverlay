"""Search links preserve a complete title in one URL path component."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import unquote, urlsplit

from explorer_dialog import ExplorerDialog


class ExternalSpotifySearchTests(unittest.TestCase):
    def test_reserved_title_characters_stay_inside_the_search_component(self):
        for title in ('1/3', 'a/b\\c%?#', '空 / 星 & 夢'):
            with self.subTest(title=title):
                dialog = SimpleNamespace(selected_song={'id': 'song', 'title': title},
                    library=SimpleNamespace(linked_track=Mock(return_value=None)), spotify=Mock())
                with patch('explorer_dialog.open_url') as opened:
                    ExplorerDialog.open_spotify(dialog)
                parsed = urlsplit(opened.call_args.args[0])
                self.assertEqual(parsed.netloc, 'open.spotify.com')
                self.assertEqual(parsed.query, '')
                self.assertEqual(parsed.fragment, '')
                self.assertEqual(len(parsed.path.split('/')), 3)
                self.assertEqual(unquote(parsed.path.split('/')[-1]), title)
                dialog.spotify.open.assert_not_called()

    def test_linked_recording_uses_its_existing_track(self):
        track = {'id': 'a' * 22}
        dialog = SimpleNamespace(selected_song={'id': 'song', 'title': '1/3'},
            library=SimpleNamespace(linked_track=Mock(return_value=track)), spotify=Mock())
        with patch('explorer_dialog.open_url') as opened:
            ExplorerDialog.open_spotify(dialog)
        opened.assert_not_called()
        dialog.spotify.open.assert_called_once_with(track)


if __name__ == '__main__':
    unittest.main()
