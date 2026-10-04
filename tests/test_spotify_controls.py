from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from spotify_controls import (TrackPlayRequest, complete_track, find_requested_button,
                              row_play_button, _duration_matches)


TRACK = {'id': '2q14CYTDGnsNrJlTZ1cCHw', 'title': 'ESPADA', 'artists': ['SHADE OF SPADE'],
         'album': 'THE IDOLM@STER MILLION THE@TER SEASON SHADE OF SPADE', 'duration': 214.133}


def node(name='', kind='DataItemControl', children=(), **extra):
    return SimpleNamespace(Name=name, ControlTypeName=kind, GetChildren=lambda: list(children),
                           IsEnabled=True, IsOffscreen=False, **extra)


def row(title='ESPADA', artist='SHADE OF SPADE', duration='3:34', label=None):
    play = node(label or f'{artist}의 {title} 재생하기', 'ButtonControl')
    return node(children=[node(children=[play]), node(f'{title} {artist}'),
                          node(f'좋아요 표시한 곡에 추가 {duration} {artist}의 {title}에 대한 추가 옵션')]), play


class RequestedTrackTests(unittest.TestCase):
    def test_actual_spotify_album_row_matches(self):
        control, play = row()
        self.assertIs(row_play_button(control, TRACK), play)

    def test_chromium_presentation_groups_inside_first_cell_are_supported(self):
        control, play = row()
        group = node(kind='GroupControl', children=[node(kind='GroupControl', children=[play])])
        control.GetChildren()[0].GetChildren = lambda: [group]
        self.assertIs(row_play_button(control, TRACK), play)

    def test_play_button_in_another_cell_cannot_be_used(self):
        control, play = row()
        control.GetChildren()[0].GetChildren = lambda: []
        control.GetChildren()[-1].GetChildren = lambda: [play]
        self.assertIsNone(row_play_button(control, TRACK))

    def test_wrong_version_artist_and_duration_never_match(self):
        for changes in ({'title': 'ESPADA (Live)'}, {'title': 'ESPADA -Instrumental-'},
                        {'artist': 'Other Artist'}, {'duration': '3:58'}, {'title': 'ESPADA 2'}):
            with self.subTest(changes=changes):
                control, _ = row(**changes)
                self.assertIsNone(row_play_button(control, TRACK))

    def test_all_requested_artists_must_be_in_the_row(self):
        track = {**TRACK, 'artists': ['A', 'B']}
        control, play = row(artist='A, B')
        self.assertIs(row_play_button(control, track), play)
        control, _ = row(artist='A')
        self.assertIsNone(row_play_button(control, track))

    def test_generic_play_is_allowed_only_in_verified_row(self):
        control, play = row(label='재생하기')
        self.assertIs(row_play_button(control, TRACK), play)
        control, _ = row(title='Other', label='재생하기')
        self.assertIsNone(row_play_button(control, TRACK))

    def test_pause_options_and_shuffle_cannot_be_invoked(self):
        for label in ('일시 정지하기', 'Pause', 'SHADE OF SPADE의 ESPADA에 대한 추가 옵션',
                      'Play ESPADA (Live) by SHADE OF SPADE', 'Shuffle', '좋아요 표시한 곡에 추가'):
            with self.subTest(label=label):
                control, _ = row(label=label)
                self.assertIsNone(row_play_button(control, TRACK))

    def test_disabled_button_and_hidden_row_cannot_be_invoked(self):
        control, play = row()
        play.IsEnabled = False
        self.assertIsNone(row_play_button(control, TRACK))
        play.IsEnabled = True
        control.IsOffscreen = True
        self.assertIsNone(row_play_button(control, TRACK))

    def test_hover_only_play_button_in_visible_exact_row_is_supported(self):
        control, play = row()
        play.IsOffscreen = True
        self.assertIs(row_play_button(control, TRACK), play)

    def test_two_play_buttons_in_same_cell_is_ambiguous(self):
        control, play = row()
        control.GetChildren()[0].GetChildren = lambda: [play, play]
        self.assertIsNone(row_play_button(control, TRACK))

    def test_wrong_album_or_two_matching_rows_is_rejected(self):
        control, play = row()
        for album, rows in [('Other Album', [control]), (TRACK['album'], [control, control])]:
            grid = node(album, 'DataGridControl', rows)
            self.assertIsNone(find_requested_button(grid, TRACK, None, float('inf')))
        grid = node(TRACK['album'], 'DataGridControl', [control])
        self.assertIs(find_requested_button(grid, TRACK, None, float('inf'))[2], play)

    def test_hidden_paused_and_disabled_duplicates_still_block_play(self):
        first, _ = row()
        for attribute, value in [('IsOffscreen', True), ('IsEnabled', False), ('Name', 'Pause')]:
            other, button = row()
            setattr(button, attribute, value)
            grid = node(TRACK['album'], 'DataGridControl', [first, other])
            self.assertIsNone(find_requested_button(grid, TRACK, None, float('inf')))

    def test_expired_deadline_never_returns_button(self):
        control, _ = row()
        grid = node(TRACK['album'], 'DataGridControl', [control])
        self.assertIsNone(find_requested_button(grid, TRACK, None, 0))

    def test_changed_row_is_rechecked_before_invoke(self):
        control, play = row()
        grid = node(TRACK['album'], 'DataGridControl', [control])
        with patch('spotify_controls.row_play_button', return_value=None):
            self.assertIsNone(find_requested_button(grid, TRACK, None, float('inf')))

    def test_only_one_unambiguous_duration_is_accepted(self):
        self.assertTrue(_duration_matches('3:34', 214.133))
        self.assertTrue(_duration_matches('1:03:34', 3814.133))
        for value in ('3:99', '3:34 3:34', '34', '1:99:34', '103:34'):
            self.assertFalse(_duration_matches(value, 214.133))

    def test_incomplete_track_never_starts_worker(self):
        for changes in ({'album': ''}, {'artists': []}, {'title': None}, {'duration': True},
                        {'duration': float('nan')}, {'id': 'bad'}):
            with self.subTest(changes=changes), patch('spotify_controls.subprocess.Popen') as start:
                value = {**TRACK, **changes}
                self.assertFalse(complete_track(value))
                TrackPlayRequest(value).close()
                start.assert_not_called()

    @patch('spotify_controls.subprocess.Popen')
    def test_worker_receives_public_track_fields_only_and_terminates_on_close(self, start):
        process = start.return_value
        process.poll.return_value = None
        process.stdin.closed = False
        process.stdout.read.return_value = b'{"invoked":false}'
        request = TrackPlayRequest({**TRACK, 'url': 'https://untrusted.example', 'authorization': 'not-a-token'})
        payload = process.stdin.write.call_args.args[0].decode('utf-8')
        self.assertNotIn('authorization', payload)
        self.assertNotIn('untrusted', payload)
        self.assertIn('ESPADA', payload)
        request.close()
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=1)
        request.close()
        process.terminate.assert_called_once()

    @patch('spotify_controls.subprocess.Popen')
    def test_uses_actual_interpreter_so_cancellation_stops_the_worker(self, start):
        start.return_value.poll.return_value = 0
        start.return_value.stdout.read.return_value = b'{"invoked":false}'
        with patch('spotify_controls.sys._base_executable', 'C:/Python/pythonw.exe', create=True):
            request = TrackPlayRequest(TRACK)
        self.assertEqual(start.call_args.args[0][0].replace('\\', '/'), 'C:/Python/python.exe')
        self.assertIn('PYTHONPATH', start.call_args.kwargs['env'])
        request.close()


if __name__ == '__main__':unittest.main()
