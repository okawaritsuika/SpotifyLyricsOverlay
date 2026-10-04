import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from batch_translation import BatchTranslationStore
from explorer_library import ExplorerLibrary
from lyric_cache import LyricCache
from lyric_edits import save_lyric_edit
from lyrics import Line, Song, match_recording
from media import Playback
from playlist_lyrics_store import PlaylistLyricsStore
from public_data import collect_public_data, export_public_data
from storage import Store


class PublicDataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'data'
        self.output = self.root / 'seed'
        self.rows = [{'original': '空を見て', 'reading': '소라오 미테', 'korean': '하늘을 봐'}]
        self.track = {'id': 'A' * 22, 'title': 'Song', 'album': 'Album', 'artists': ['Artist'], 'duration': 100.0}

    def write(self, name, value):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def fixture(self):
        song = {'id': 'a' * 24, 'title': 'Song', 'artist': 'Artist', 'album': 'Album', 'lines': self.rows}
        self.write('library.json', [{**song, 'source_path': 'C:\\Users\\Private\\lyrics.txt'}])
        key = 'Song\0Artist\0Album'
        cache_name = hashlib.sha256(key.encode()).hexdigest()
        self.write('lyrics_cache/' + cache_name + '.json', {
            'version': 1, 'key': key, 'duration': 100, 'saved_at': 123456789,
            'lines': self.rows, 'direct': {'source': 'Spotify', 'provider_id': self.track['id'],
                'lines': self.rows, 'times': [4.5], 'access_token': 'secret'}, 'cookie': 'secret'})
        self.write('playlist_lyrics/tracks/' + self.track['id'] + '.json', {
            'schema_version': 1, 'track': {**self.track, 'email': 'owner@example.test'},
            'lines': self.rows, 'direct': None, 'saved_at': 123456789,
            'sources': ['Spotify:private-session', 'https://open.spotify.com/track/' + self.track['id'] + '?si=secret',
                        'https://open.spotify.com/playlist/' + 'B' * 22, 'https://example.test/private',
                        'C:\\Users\\Private\\lyrics.txt']})
        self.write('explorer_library.json', {'schema_version': 1, 'favorites': [song['id']],
            'recent': [song['id']], 'spotify': {song['id']: {**self.track, 'market': 'KR',
                'is_playable': True, 'checked_at': 'personal-time', 'access_token': 'secret'}}})
        self.write('settings.json', {'email': 'owner@example.test', 'token': 'secret'})
        self.write('browser_profile/Cookies.json', {'secret': 'never exported'})
        self.write('ai_translations/requests/request.json', {'secret': 'never exported'})
        self.write('playlist_lyrics/manifest.json', {'playlist_id': 'private playlist'})
        self.write('playlist_lyrics/index.json', {'schema_version': 1, 'tracks': {'secret': {'owner': 'private'}}})
        translation = BatchTranslationStore(self.source)
        identifier = translation.capture('Song', 'Artist', 'Album', [Line('空を見て', '', '')])
        self.write('ai_translations/imports.json', {'version': 1, 'documents': {identifier: {'1': '하늘을 봐'}},
                                                  'email': 'owner@example.test'})
        self.write('timings/' + song['id'] + '.json', {'fingerprint': Store(self.source).library()[0].fingerprint,
            'times': [4.5], 'offset': .25, 'source': '직접 기록', 'origins': ['manual'],
            'audio_file': 'C:\\Users\\Private\\recording.wav', 'recorded_at': 'personal-time'})
        return song, cache_name

    def test_export_preserves_music_and_runtime_readers_without_private_state(self):
        song, cache_name = self.fixture()
        before = {path: path.read_bytes() for path in self.source.rglob('*') if path.is_file()}
        summary = export_public_data(self.source, self.output)
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.assertEqual(summary['playlist_tracks'], 1)
        self.assertEqual(summary['translated_rows'], 1)
        songs = Store(self.output).library()
        self.assertEqual(songs[0].lines[0].korean, '하늘을 봐')
        self.assertEqual(Store(self.output).timing(songs[0])['offset'], .25)
        cache = LyricCache(self.output / 'lyrics_cache').load(Playback('Song', 'Artist', 'Album', duration=100))
        self.assertEqual(cache['direct']['times'], [4.5])
        self.assertEqual(cache['saved_at'], 0)
        playlist = PlaylistLyricsStore(self.output)
        self.assertEqual(playlist.load(self.track['id'])['lines'][0].korean, '하늘을 봐')
        self.assertEqual(len(playlist.metadata()), 1)
        explorer = ExplorerLibrary(self.output)
        self.assertEqual(explorer.value['favorites'], [])
        self.assertEqual(explorer.value['recent'], [])
        self.assertEqual(explorer.linked_track(song['id'])['id'], self.track['id'])
        self.assertNotIn('is_playable', explorer.linked_track(song['id']))
        self.assertEqual(BatchTranslationStore(self.output).fill('Song', 'Artist', 'Album', [Line('空を見て', '', '')])[0].korean,
                         '하늘을 봐')
        payload = '\n'.join(path.read_text(encoding='utf-8') for path in self.output.rglob('*.json'))
        for private in ('secret', 'owner@example.test', 'C:\\\\Users', 'private-session', 'personal-time',
                        'playlist_id', 'access_token', 'recorded_at', 'source_path'):
            self.assertNotIn(private, payload)
        self.assertFalse((self.output / 'settings.json').exists())
        self.assertFalse((self.output / 'browser_profile').exists())
        self.assertFalse((self.output / 'ai_translations/requests').exists())

    def test_retained_lyric_fields_cannot_smuggle_private_values(self):
        for private in ('owner@example.test', 'C:\\Users\\Owner\\private.txt',
                        'ghp_' + 'A' * 36, 'Bearer ' + 'A' * 40, '/home/owner/private.txt'):
            with self.subTest(value=private[:10]):
                self.write('library.json', [{'id': 'a' * 24, 'title': 'Song', 'artist': 'Artist', 'album': 'Album',
                    'lines': [{'original': 'song', 'reading': '', 'korean': private}]}])
                with self.assertRaises(ValueError):export_public_data(self.source, self.output)
                self.assertFalse(self.output.exists())

    def test_official_album_name_is_not_mistaken_for_email(self):
        self.write('spotify_public_tracks.json', {'schema_version': 1, 'tracks': [
            {**self.track, 'album': 'THE IDOLM@STER.KR OST'}]})
        result = collect_public_data(self.source)
        self.assertEqual(result['spotify_public_tracks.json']['tracks'][0]['album'], 'THE IDOLM@STER.KR OST')

    def test_output_must_be_empty_and_separate_from_source(self):
        self.fixture()
        for output in (self.source, self.source / 'export', self.root,
                       self.root / 'unused' / '..' / 'data' / 'export'):
            with self.subTest(output=output.name):
                with self.assertRaises(ValueError):export_public_data(self.source, output)
        export_public_data(self.source, self.output)
        with self.assertRaises(ValueError):export_public_data(self.source, self.output)

    def test_singer_annotations_drop_personal_provenance_and_filename(self):
        self.write('user_lyrics/private-owner.json', {'schema_version': 1, 'title': 'Song', 'artists': ['Artist'],
            'source': {'kind': 'user_provided', 'url': 'https://namu.wiki/w/Song?token=private',
                       'received_at': 'private-time', 'description': 'private author'},
            'lines': [{**self.rows[0], 'singers': ['Singer'], 'original_variants': [
                {'source': 'local_spotify_original', 'text': '空をみて', 'owner': 'private author'}],
                'email': 'private@example.test'}]})
        documents = collect_public_data(self.source)
        name, value = next(iter(documents.items()))
        self.assertRegex(name, r'^user_lyrics/[0-9a-f]{64}\.json$')
        self.assertEqual(value['source'], {'kind': 'user_provided', 'url': 'https://namu.wiki/w/Song'})
        self.assertEqual(value['lines'][0]['singers'], ['Singer'])
        self.assertNotIn('private', json.dumps(value))

    def test_only_actual_manual_annotations_are_exported(self):
        self.write('manual_annotations/private-session-report.json', {'source_file': 'secret', 'applied': True})
        self.write('manual_annotations/private-session-lyrics.json', {'source_file': 'secret', 'lines': self.rows,
                                                                   'provenance': 'private author'})
        documents = collect_public_data(self.source)
        self.assertEqual(len(documents), 1)
        self.assertEqual(next(iter(documents.values())), {'lines': self.rows})

    def test_mismatched_translation_identity_is_rejected_before_writing(self):
        self.write('ai_translations/history/' + 'a' * 64 + '.json', {
            'id': 'a' * 64, 'title': 'Song', 'artist': 'Artist', 'album': 'Album', 'lines': self.rows})
        with self.assertRaises(ValueError):export_public_data(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_explicit_edit_survives_export_with_its_clock_and_override_binding(self):
        self.fixture()
        store = Store(self.source)
        original = store.library()[0]
        key = 'Alternate recording title\0Artist\0Album'
        settings = {**store.settings(), 'source_folder': 'C:\\Users\\Private\\lyrics',
                    'deleted_lyrics': ['private listening preference'], 'font_size': 99,
                    'links': {'unrelated private recording': original.id},
                    'lyric_sources': {'missing-song': {'user_override': True, 'email': 'owner@example.test'}}}
        incoming = Song('incoming', 'Title', 'Artist', 'Album', (Line('空を見て', 'new reading', '수정한 번역'),))
        _, edited_settings, saved, backup = save_lyric_edit(
            store, [original], settings, original, incoming,
            {'path': 'C:\\Users\\Private\\lyrics.txt', 'email': 'owner@example.test'}, key)
        # Unrelated malformed links are excluded when they point elsewhere.
        edited_settings['links']['unrelated private recording'] = 'missing-song'
        store.save_settings(edited_settings)
        before = {path: path.read_bytes() for path in self.source.rglob('*') if path.is_file()}
        summary = export_public_data(self.source, self.output)
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.assertEqual(summary['user_lyric_overrides'], 1)
        exported = Store(self.output)
        self.assertEqual(exported.settings(), {'lyric_sources': {saved.id: {'user_override': True}},
                                              'links': {key: saved.id}})
        selected = match_recording(exported.library(), 'Alternate recording title', 'Artist', 'Album',
                                   exported.settings()['links'][key])
        self.assertEqual(selected, saved)
        self.assertEqual(exported.timing(selected)['times'], [4.5])
        self.assertEqual(exported.timing(selected)['offset'], .25)
        self.assertFalse((self.output / 'lyric_edit_backups').exists())
        self.assertTrue(backup.exists())
        self.assertNotIn('private', (self.output / 'settings.json').read_text(encoding='utf-8').casefold())

    def test_private_recording_key_for_an_override_stops_export(self):
        self.fixture()
        self.write('settings.json', {'lyric_sources': {'a' * 24: {'user_override': True}},
                                    'links': {'owner@example.test\0Artist\0Album': 'a' * 24}})
        with self.assertRaises(ValueError):export_public_data(self.source, self.output)
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
