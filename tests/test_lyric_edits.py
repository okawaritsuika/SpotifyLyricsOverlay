import base64
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lyric_annotations import LyricAnnotations
from lyric_edits import save_lyric_edit
from lyrics import Line, Song
from live_timing import LiveTimingStore
from storage import Store, atomic_json


class LyricEditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(self.temporary.name)
        self.song = Song('saved', 'Song', 'Artist', 'Album',
                         (Line('青い空', 'old reading', 'old translation'), Line('風の音', '', '')))
        self.other = Song('other', 'Other', 'Singer', 'Release', (Line('Other lyric', '', ''),))
        self.songs = [self.song, self.other]
        self.key = '\0'.join((self.song.title, self.song.artist, self.song.album))
        self.link = 'Alternate title\0Artist\0Album'
        self.settings = {'links': {self.key: self.song.id, self.link: self.song.id, 'other-key': self.other.id},
                         'lyric_sources': {self.song.id: {'url': 'old'}, self.other.id: {'url': 'keep'}},
                         'deleted_lyrics': ['unrelated-key'], 'font_size': 24}
        self.store.save_library(self.songs)
        self.store.save_settings(self.settings)
        self.clock = {'times': [12, 25], 'offset': .5, 'source': 'manual', 'origins': ['manual', 'manual']}
        self.store.save_timing(self.song, self.clock)
        self.store.save_timing(self.other, {'times': [4], 'offset': 0})
        LyricAnnotations(self.store).save(self.song, self.song.lines, {'url': 'old'})
        atomic_json(self.store.directory / 'audio_drafts/saved.json', {'fingerprint': self.song.fingerprint})

    def path(self, name):return self.store.directory / name

    def snapshot(self):
        return {path.relative_to(self.store.directory): path.read_bytes()
                for path in self.store.directory.rglob('*.json')
                if 'lyric_edit_backups' not in path.parts}

    def incoming(self, lines):
        return Song('imported', 'Incoming title', 'Incoming artist', 'Incoming album', tuple(lines))

    def test_translation_replacement_keeps_identity_metadata_and_exact_clock(self):
        incoming = self.incoming([Line('青い空', '', 'new translation'), Line('風の音', '', '')])
        before = self.snapshot()
        original_settings = deepcopy(self.settings)
        updated, settings, saved, backup = save_lyric_edit(
            self.store, self.songs, self.settings, self.song, incoming, {'url': 'namu'})
        self.assertEqual(saved, replace(self.song, lines=incoming.lines))
        self.assertEqual(updated, [saved, self.other])
        self.assertEqual(self.store.library(), updated)
        self.assertEqual(self.store.timing(saved), {**self.clock, 'fingerprint': saved.fingerprint})
        self.assertEqual(settings['lyric_sources']['saved'], {'url': 'namu', 'user_override': True})
        self.assertEqual(settings['links'], self.settings['links'])
        self.assertEqual(self.settings, original_settings)
        self.assertEqual(self.songs, [self.song, self.other])
        self.assertFalse(self.path('lyric_annotations/saved.json').exists())
        self.assertEqual(LyricAnnotations(self.store).display_lines(saved), incoming.lines)
        for name in ('timings/other.json', 'audio_drafts/saved.json'):
            self.assertEqual(self.path(name).read_bytes(), before[Path(name)])
        data = json.loads(backup.read_text(encoding='utf-8'))
        self.assertEqual(data['operation'], 'replace')
        self.assertEqual(Song.from_dict(data['song']), self.song)
        for name in ('library.json', 'settings.json', 'timings/saved.json',
                     'lyric_annotations/saved.json', 'audio_drafts/saved.json'):
            self.assertEqual(base64.b64decode(data['files'][name]), before[Path(name)])

    def test_changed_originals_or_spacing_reset_clock_and_offset(self):
        for text in ('訂正した原文', '青い 空'):
            with self.subTest(text=text):
                incoming = self.incoming([Line(text, '', ''), Line('風の音', '', '')])
                _, _, saved, _ = save_lyric_edit(self.store, self.songs, self.settings, self.song, incoming)
                self.assertEqual(self.store.timing(saved), {'times': [None, None], 'offset': 0.,
                                                           'source': '', 'fingerprint': saved.fingerprint})

    def test_reordered_originals_do_not_reuse_clock(self):
        incoming = self.incoming(reversed(self.song.lines))
        _, _, saved, _ = save_lyric_edit(self.store, self.songs, self.settings, self.song, incoming)
        self.assertEqual(self.store.timing(saved)['times'], [None, None])

    def test_delete_suppresses_all_connected_recordings_and_preserves_other_song(self):
        before = self.snapshot()
        updated, settings, saved, backup = save_lyric_edit(
            self.store, self.songs, self.settings, self.song, playback_key='current-key')
        self.assertIsNone(saved)
        self.assertEqual(updated, [self.other])
        self.assertEqual(self.store.library(), updated)
        self.assertEqual(set(settings['deleted_lyrics']), {self.key, self.link, 'current-key', 'unrelated-key'})
        self.assertEqual(settings['links'], {'other-key': self.other.id})
        self.assertEqual(settings['lyric_sources'], {self.other.id: {'url': 'keep'}})
        self.assertEqual(self.store.settings(), settings)
        self.assertFalse(self.path('timings/saved.json').exists())
        self.assertFalse(self.path('lyric_annotations/saved.json').exists())
        self.assertEqual(self.path('timings/other.json').read_bytes(), before[Path('timings/other.json')])
        self.assertEqual(json.loads(backup.read_text(encoding='utf-8'))['operation'], 'delete')

    def test_new_lyrics_reenable_deleted_track_and_bind_requested_recording(self):
        self.settings['deleted_lyrics'] += [self.key, 'current-key']
        new = replace(self.song, id='new')
        updated, settings, saved, backup = save_lyric_edit(
            self.store, [self.other], self.settings, None, new, {'source': 'manual'}, 'current-key')
        self.assertEqual(updated, [self.other, new])
        self.assertEqual(saved, new)
        self.assertEqual(settings['deleted_lyrics'], ['unrelated-key'])
        self.assertEqual(settings['links']['current-key'], new.id)
        self.assertTrue(settings['lyric_sources'][new.id]['user_override'])
        self.assertTrue(backup.is_file())

    def test_replacement_reenables_all_existing_links(self):
        self.settings['deleted_lyrics'] += [self.key, self.link, 'current-key']
        _, settings, _, _ = save_lyric_edit(self.store, self.songs, self.settings, self.song,
                                          self.song, playback_key='current-key')
        self.assertEqual(settings['deleted_lyrics'], ['unrelated-key'])
        self.assertEqual(settings['links']['current-key'], self.song.id)

    def test_temporary_delete_clears_live_clock_and_draft_with_backup(self):
        temporary = replace(self.song, id='memory-synthetic')
        live = LiveTimingStore(self.store.directory)
        live.save(temporary, self.clock)
        timing = live.path(temporary)
        draft = self.store.directory / 'live_audio_drafts' / timing.name
        atomic_json(draft, {'fingerprint': live.signature(temporary), 'times': [12, 25]})
        before = self.snapshot()
        updated, settings, saved, backup = save_lyric_edit(
            self.store, self.songs, self.settings, temporary, playback_key='temporary-key')
        self.assertEqual(updated, self.songs)
        self.assertIsNone(saved)
        self.assertFalse(timing.exists())
        self.assertFalse(draft.exists())
        self.assertIn(self.key, settings['deleted_lyrics'])
        self.assertIn('temporary-key', settings['deleted_lyrics'])
        data = json.loads(backup.read_text(encoding='utf-8'))
        self.assertEqual(Song.from_dict(data['song']), temporary)
        for path in (timing, draft):
            relative = path.relative_to(self.store.directory)
            self.assertEqual(base64.b64decode(data['files'][relative.as_posix()]), before[relative])
        self.assertEqual(self.path('timings/saved.json').read_bytes(), before[Path('timings/saved.json')])

    def test_temporary_delete_failure_restores_live_files(self):
        temporary = replace(self.song, id='memory-synthetic')
        live = LiveTimingStore(self.store.directory)
        live.save(temporary, self.clock)
        draft = self.store.directory / 'live_audio_drafts' / live.path(temporary).name
        atomic_json(draft, {'times': [12, 25]})
        before = self.snapshot()
        with patch.object(self.store, 'save_settings', side_effect=OSError('failed')):
            with self.assertRaises(OSError):
                save_lyric_edit(self.store, self.songs, self.settings, temporary)
        self.assertEqual(self.snapshot(), before)

    def test_temporary_replacement_preserves_exact_original_clock_and_backup(self):
        temporary = replace(self.song, id='memory-synthetic')
        live = LiveTimingStore(self.store.directory)
        live.save(temporary, self.clock)
        before_clock = live.load(temporary)
        before = self.snapshot()
        incoming = self.incoming([Line('青い空', '', 'corrected'), Line('風の音', '', 'new')])
        updated, settings, saved, backup = save_lyric_edit(
            self.store, self.songs, self.settings, temporary, incoming, playback_key='temporary-key')
        self.assertEqual(saved, replace(temporary, lines=incoming.lines))
        self.assertEqual(updated, [*self.songs, saved])
        self.assertEqual(self.store.library(), updated)
        self.assertEqual(self.store.timing(saved), {**before_clock, 'fingerprint': saved.fingerprint})
        self.assertTrue(settings['lyric_sources'][saved.id]['user_override'])
        self.assertEqual(settings['links']['temporary-key'], saved.id)
        data = json.loads(backup.read_text(encoding='utf-8'))
        self.assertEqual(Song.from_dict(data['song']), temporary)
        relative = live.path(temporary).relative_to(self.store.directory)
        self.assertEqual(base64.b64decode(data['files'][relative.as_posix()]), before[relative])
        self.assertEqual(live.path(temporary).read_bytes(), before[relative])

    def test_temporary_replacement_with_changed_rows_resets_clock(self):
        temporary = replace(self.song, id='memory-synthetic')
        live = LiveTimingStore(self.store.directory)
        live.save(temporary, self.clock)
        old_clock = live.path(temporary).read_bytes()
        incoming = self.incoming([Line('changed original', '', 'corrected')])
        _, _, saved, _ = save_lyric_edit(self.store, self.songs, self.settings, temporary, incoming)
        self.assertEqual(self.store.timing(saved)['times'], [None])
        self.assertEqual(self.store.timing(saved)['offset'], 0.)
        self.assertEqual(live.path(temporary).read_bytes(), old_clock)

    def test_temporary_replacement_failure_restores_live_and_library_files(self):
        temporary = replace(self.song, id='memory-synthetic')
        LiveTimingStore(self.store.directory).save(temporary, self.clock)
        before = self.snapshot()
        with patch.object(self.store, 'save_settings', side_effect=OSError('failed')):
            with self.assertRaises(OSError):
                save_lyric_edit(self.store, self.songs, self.settings, temporary, self.song)
        self.assertEqual(self.snapshot(), before)

    def test_late_write_failure_rolls_back_replace_and_delete_exactly(self):
        before = self.snapshot()
        for incoming in (self.incoming([Line('new original', '', '')]), None):
            with self.subTest(delete=incoming is None):
                with patch.object(self.store, 'save_settings', side_effect=PermissionError('locked')):
                    with self.assertRaises(PermissionError):
                        save_lyric_edit(self.store, self.songs, self.settings, self.song, incoming)
                self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(list(self.path('lyric_edit_backups').glob('*.json'))), 2)

    def test_failed_new_save_removes_created_library_and_settings(self):
        store = Store(self.path('empty'))
        with patch.object(store, 'save_settings', side_effect=OSError('failed')):
            with self.assertRaises(OSError):save_lyric_edit(store, [], {}, None, self.song)
        self.assertFalse((store.directory / 'library.json').exists())
        self.assertFalse((store.directory / 'settings.json').exists())
        self.assertEqual(len(list((store.directory / 'lyric_edit_backups').glob('*.json'))), 1)

    def test_failed_backup_does_not_mutate_any_saved_file(self):
        before = self.snapshot()
        with patch('lyric_edits.atomic_json', side_effect=OSError('backup failed')):
            with self.assertRaises(OSError):save_lyric_edit(self.store, self.songs, self.settings, self.song)
        self.assertEqual(self.snapshot(), before)

    def test_missing_target_or_unsafe_identifier_cannot_delete_files(self):
        before = self.snapshot()
        with self.assertRaises(ValueError):save_lyric_edit(self.store, [self.other], self.settings, self.song)
        unsafe = replace(self.song, id='../outside')
        with self.assertRaises(ValueError):save_lyric_edit(self.store, [unsafe], self.settings, unsafe)
        self.assertEqual(self.snapshot(), before)


if __name__ == '__main__':unittest.main()
