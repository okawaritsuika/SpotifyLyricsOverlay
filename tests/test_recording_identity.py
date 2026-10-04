"""A local song's saved timing never follows another recording by title alone."""
from dataclasses import replace
import tempfile
import time
import tkinter as tk
import unittest

from app import LyricsApp
from lyrics import Line, Song, match_recording, match_song
from media import Playback
from storage import Store


LINES = tuple(Line(text, '', '') for text in ('朝の光', '花を描く', '最後の言葉'))
LOCAL = Song('studio', 'Shared Song', 'Idol A', 'Studio recording', LINES)


class RecordingMatchTests(unittest.TestCase):
    def test_unique_title_cannot_override_different_artist_or_album(self):
        self.assertIsNone(match_recording([LOCAL], LOCAL.title, 'Idol B', LOCAL.album))
        self.assertIsNone(match_recording([LOCAL], LOCAL.title, LOCAL.artist, 'Live recording'))
        self.assertIsNone(match_recording([LOCAL], LOCAL.title, LOCAL.artist, LOCAL.album+' Deluxe'))

    def test_missing_playback_or_library_metadata_is_not_an_identity(self):
        for field in ('title', 'artist', 'album'):
            with self.subTest(field=field):
                values = {'title': LOCAL.title, 'artist': LOCAL.artist, 'album': LOCAL.album}
                values[field] = ''
                self.assertIsNone(match_recording([LOCAL], **values))
                self.assertIsNone(match_recording([replace(LOCAL, **{field: ''})], LOCAL.title, LOCAL.artist, LOCAL.album))

    def test_complete_identity_normalizes_width_case_and_spacing(self):
        self.assertEqual(match_recording([LOCAL], 'ＳＨＡＲＥＤ ＳＯＮＧ', 'idol a', 'Studio  recording'), LOCAL)

    def test_complete_identity_disambiguates_artists_but_not_duplicate_documents(self):
        other = replace(LOCAL, id='other', artist='Idol B')
        self.assertEqual(match_recording([LOCAL, other], LOCAL.title, other.artist, LOCAL.album), other)
        self.assertIsNone(match_recording([LOCAL, replace(LOCAL, id='duplicate')], LOCAL.title, LOCAL.artist, LOCAL.album))

    def test_explicit_link_and_title_search_remain_available(self):
        self.assertEqual(match_recording([LOCAL], 'Other title', '', '', LOCAL.id), LOCAL)
        self.assertEqual(match_song([LOCAL], LOCAL.title), LOCAL)
        self.assertIsNone(match_recording([LOCAL], LOCAL.title, LOCAL.artist, LOCAL.album, 'removed-id'))


class RecordingBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.store = Store(self.temp.name)
        self.store.save_library([LOCAL])
        self.store.save_settings({'online': False, 'auto_audio': False, 'web_auto': False,
                                  'borderless': False, 'use_estimate': True, 'reader_mode': 'chrome'})
        self.store.save_timing(LOCAL, {'times': [10, 20, 30], 'offset': 0, 'source': '직접 기록'})
        self.root = tk.Tk(); self.root.withdraw()
        self.app = LyricsApp(self.root, self.store, start_services=False)
        self.playback = Playback(LOCAL.title, 'Idol B', 'Different recording', 45, 100, False, time.monotonic())

    def tearDown(self):
        self.app.close(); self.temp.cleanup()

    def provider(self):
        return {'source': 'Spotify', 'lines': LINES, 'times': [40, 50, 60], 'provider_id': 'other-recording'}

    def test_different_recording_does_not_reuse_local_manual_clock_or_block_provider(self):
        for artist, album in (('Idol B', 'Different recording'), (LOCAL.artist, 'Live recording')):
            with self.subTest(artist=artist, album=album):
                self.playback = replace(self.playback, artist=artist, album=album)
                self.app.set_playback(self.playback)
                self.assertIsNone(self.app.song)
                self.assertFalse(self.app.is_bound())
                self.assertFalse(self.app.reuse_complete_sync())
                self.app.receive_direct_lyrics(self.playback.key, self.provider())
                self.assertTrue(self.app.is_bound())
                self.assertEqual(self.app.effective_times()[0], [40, 50, 60])
                self.assertEqual(self.app.displayed_index(), 0)
                self.assertEqual(self.store.timing(LOCAL)['times'], [10, 20, 30])

    def test_matching_recording_keeps_saved_manual_timing(self):
        self.playback = replace(self.playback, artist=LOCAL.artist, album=LOCAL.album)
        self.app.set_playback(self.playback)
        self.assertEqual(self.app.song, LOCAL)
        self.assertTrue(self.app.reuse_complete_sync())
        self.app.receive_direct_lyrics(self.playback.key, self.provider())
        self.assertEqual(self.app.effective_times()[0], [10, 20, 30])

    def test_explicit_link_preserves_user_override_with_missing_metadata(self):
        self.playback = replace(self.playback, artist='', album='')
        self.app.settings.setdefault('links', {})[self.playback.key] = LOCAL.id
        self.app.set_playback(self.playback)
        self.assertEqual(self.app.song, LOCAL)
        self.assertTrue(self.app.is_bound())
        self.assertEqual(self.app.effective_times()[0], [10, 20, 30])

    def test_manually_selected_mismatched_song_is_a_preview_until_linked(self):
        self.app.set_playback(self.playback)
        self.app.select_song(LOCAL)
        self.assertEqual(self.app.song, LOCAL)
        self.assertFalse(self.app.is_bound())
        self.assertIsNone(self.app.displayed_index())
        self.app.settings.setdefault('links', {})[self.playback.key] = LOCAL.id
        self.assertTrue(self.app.is_bound())

    def test_browser_lyrics_do_not_import_other_recording_manual_times(self):
        self.app.set_playback(self.playback)
        self.app.receive_reader_lyrics(self.app.reader_bridge.snapshot()['track'], LINES)
        self.assertTrue(self.app.live_lyrics.is_temporary(self.app.song))
        self.assertEqual(self.app.timing['times'], [None]*3)
        self.assertEqual(self.store.timing(LOCAL)['times'], [10, 20, 30])

    def test_cached_recording_does_not_restore_other_recording_manual_times(self):
        self.app.lyric_cache.save(self.playback, LINES, self.provider())
        self.app.set_playback(self.playback)
        self.assertTrue(self.app.cache_hit)
        self.assertTrue(self.app.live_lyrics.is_temporary(self.app.song))
        self.assertEqual(self.app.effective_times()[0], [40, 50, 60])
        self.assertEqual(self.app.displayed_index(), 0)

    def test_explicit_link_survives_cache_reader_and_web_addition(self):
        self.app.settings.setdefault('links', {})[self.playback.key] = LOCAL.id
        self.app.lyric_cache.save(self.playback, LINES, self.provider())
        self.app.set_playback(self.playback)
        self.assertTrue(self.app.cache_hit)
        self.assertEqual(self.app.song.id, LOCAL.id)
        self.assertEqual(self.app.effective_times()[0], [10, 20, 30])
        translated = tuple(replace(line, korean='번역') for line in LINES)
        self.app.receive_reader_lyrics(self.app.reader_bridge.snapshot()['track'], translated)
        self.assertEqual(self.app.song.id, LOCAL.id)
        self.assertEqual(self.app.effective_times()[0], [10, 20, 30])
        self.app.add_web_song(replace(LOCAL, id='unrelated', title='Unrelated song'), {})
        self.assertEqual(self.app.song.id, LOCAL.id)
        self.assertTrue(self.app.is_bound())

    def test_adding_unrelated_web_song_does_not_select_same_title_local_recording(self):
        self.app.set_playback(self.playback)
        self.app.add_web_song(replace(LOCAL, id='unrelated', title='Unrelated song'), {})
        self.assertIsNone(self.app.song)


if __name__ == '__main__':
    unittest.main()
