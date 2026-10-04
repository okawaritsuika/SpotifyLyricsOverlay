"""Complete display credits must stay separate from lyric identity and timing."""
from dataclasses import replace
import json
import os
from pathlib import Path
import queue
import tempfile
import unittest
from unittest.mock import Mock

from artist_metadata import ArtistMetadata, MAX_BYTES
from background_lyrics import BackgroundLyrics
from media import Playback


class FakeJob:
    def __init__(self, *args, **kwargs):self.commands = []
    def send(self, command):self.commands.append(command)
    def close(self):pass


class ArtistMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.playback = Playback('Sparkle', 'イルミネーションスターズ', 'Album', 0, 253.3, True)
        self.track = {'id': 'a' * 22, 'title': 'Sparkle', 'album': 'Album', 'duration': 253.31,
                      'artists': ['イルミネーションスターズ', 'ノクチル']}
        self.cache = ArtistMetadata(self.temp.name)

    def tearDown(self):self.temp.cleanup()

    def test_complete_credits_survive_restart_without_changing_original_identity(self):
        original_key = self.playback.key
        self.assertTrue(self.cache.put(self.playback, self.track))
        reopened = ArtistMetadata(self.temp.name)
        self.assertEqual(reopened.get(self.playback), tuple(self.track['artists']))
        self.assertEqual(self.playback.key, original_key)
        self.assertEqual(self.playback.artist, self.track['artists'][0])
        self.assertEqual(reopened.get(replace(self.playback, duration=253.31)), ())
        self.assertEqual(reopened.get(replace(self.playback, album='Other')), ())

    def test_all_recording_fields_must_match_before_credits_are_cached(self):
        for updates in ({'title': 'Sparkle (Live)'}, {'album': 'Other'}, {'duration': 260},
                        {'artists': ['RADWIMPS']}, {'id': 'bad-id'}):
            with self.subTest(updates=updates):
                self.assertFalse(self.cache.put(self.playback, {**self.track, **updates}))
        self.assertEqual(self.cache.get(self.playback), ())
        self.assertFalse(self.cache.path.exists())

    def test_malformed_or_control_character_names_and_lengths_are_rejected(self):
        for artists in ([], 'Singer', [None], [{}], [''], ['good', 'bad\nname'],
                        ['Singer\u202e'], ['a' * 513], ['Singer'] * 201):
            with self.subTest(artists=artists):
                self.assertFalse(self.cache.put(self.playback, {**self.track, 'artists': artists}))
        for duration in (True, '253.3', float('nan'), float('inf'), 10 ** 1000):
            self.assertFalse(self.cache.put(self.playback, {**self.track, 'duration': duration}))
        self.assertFalse(self.cache.put(replace(self.playback, title='Bad\0title'), self.track))

    def test_large_ensemble_retains_every_artist_without_truncation(self):
        artists = [self.playback.artist] + [f'Singer {number}' for number in range(199)]
        self.assertTrue(self.cache.put(self.playback, {**self.track, 'artists': artists}))
        self.assertEqual(self.cache.get(self.playback), tuple(artists))

    def test_only_display_metadata_is_persisted(self):
        self.cache.put(self.playback, {**self.track, 'authorization': 'synthetic-secret', 'lyrics': 'ignored'})
        value = json.loads(self.cache.path.read_text(encoding='utf-8'))
        self.assertEqual(set(value['entries'][0]['track']), {'id', 'title', 'album', 'duration', 'artists'})
        self.assertNotIn('synthetic-secret', self.cache.path.read_text(encoding='utf-8'))

    def test_invalid_and_oversized_disk_caches_do_not_break_startup(self):
        for value in ('invalid', '[]', '{"version":1,"entries":[null]}', ' ' * (MAX_BYTES + 1)):
            self.cache.path.write_text(value, encoding='utf-8')
            self.assertEqual(ArtistMetadata(self.temp.name).get(self.playback), ())

    def test_cached_track_is_revalidated_instead_of_trusting_saved_artist_list(self):
        self.cache.put(self.playback, self.track)
        value = json.loads(self.cache.path.read_text(encoding='utf-8'))
        value['entries'][0]['track']['artists'] = ['RADWIMPS']
        self.cache.path.write_text(json.dumps(value), encoding='utf-8')
        self.assertEqual(ArtistMetadata(self.temp.name).get(self.playback), ())


class BackgroundArtistTests(unittest.TestCase):
    def setUp(self):
        ArtistMetadataTests.setUp(self)
        self.service = BackgroundLyrics('.', self.temp.name, queue.Queue(), job_factory=FakeJob, fetch=Mock())

    def tearDown(self):
        self.service.close()
        ArtistMetadataTests.tearDown(self)

    def observe(self, playback=None, **kwargs):
        self.service.observe(playback or self.playback, public_enabled=False, cached_sync=True, **kwargs)

    def event(self, **kwargs):
        return {'kind': 'metadata', 'key': self.service.request_key,
                'request_duration': self.playback.duration, 'track': self.track, **kwargs}

    def start(self):
        self.service.connect(visible=False)
        self.observe()
        return self.service.job

    def test_cached_sync_requests_metadata_once_and_never_marks_lyrics_pending(self):
        job = self.start()
        for _ in range(6):self.observe()
        self.assertEqual(len(job.commands), 1)
        self.assertTrue(job.commands[0]['metadata_only'])
        self.assertFalse(self.service.spotify_pending)
        self.service.fetch.assert_not_called()
        self.assertIsNone(self.service.receive(job, self.event()))
        self.assertEqual(self.service.display_artists(self.playback), tuple(self.track['artists']))
        self.assertFalse(self.service.metadata_pending)

    def test_saved_credits_avoid_next_session_lookup_and_work_offline(self):
        self.cache.put(self.playback, self.track)
        restored = BackgroundLyrics('.', self.temp.name, queue.Queue(), job_factory=FakeJob)
        restored.connect(visible=False)
        restored.observe(self.playback, cached_sync=True, public_enabled=False)
        self.assertEqual(restored.job.commands, [])
        restored.observe(self.playback, enabled=False, cached_sync=True, public_enabled=False)
        self.assertEqual(restored.display_artists(self.playback), tuple(self.track['artists']))
        restored.close()

    def test_never_starts_spotify_or_queries_without_online_connection_intent(self):
        self.observe()
        self.assertIsNone(self.service.job)
        self.observe(enabled=False)
        self.service.connect(visible=False)
        self.observe(enabled=False)
        self.assertEqual(self.service.job.commands, [])

    def test_old_worker_old_track_disabled_and_changed_duration_results_are_rejected(self):
        job = self.start()
        old = self.event()
        self.service.receive(object(), old)
        self.assertEqual(self.service.display_artists(self.playback), ())
        self.observe(replace(self.playback, title='Other'))
        self.service.receive(job, old)
        self.assertEqual(self.service.display_artists(self.playback), ())
        self.observe()
        self.service.receive(job, old)
        self.assertEqual(self.service.display_artists(self.playback), ())
        changed = replace(self.playback, duration=253.4)
        old = self.event()
        self.observe(changed)
        self.service.receive(job, old)
        self.assertEqual(self.service.display_artists(changed), ())
        current = self.event(request_duration=changed.duration)
        self.observe(changed, enabled=False)
        self.service.receive(job, current)
        self.assertEqual(self.service.display_artists(changed), ())

    def test_metadata_result_requires_valid_track_and_numeric_request_duration(self):
        job = self.start()
        for event in (None, [], self.event(track={'artists': self.track['artists']}),
                      self.event(request_duration='253.3'), self.event(request_duration=True)):
            self.assertIsNone(self.service.receive(job, event))
            self.assertEqual(self.service.display_artists(self.playback), ())

    def test_metadata_progress_and_failure_do_not_replace_successful_lyrics_status(self):
        job = self.start()
        self.service.spotify_state = 'ready'
        self.service.spotify_status = 'Spotify 싱크 연결됨'
        for state in ('searching', 'error', 'not_found', 'ready'):
            self.service.receive(job, {'kind': 'metadata_status', 'key': self.service.request_key,
                                      'authenticated': True, 'state': state, 'text': 'metadata only'})
            self.assertEqual(self.service.spotify_state, 'ready')
            self.assertEqual(self.service.spotify_status, 'Spotify 싱크 연결됨')
            self.assertFalse(self.service.spotify_pending)

    def test_metadata_only_authentication_finishes_initial_connection_status(self):
        job = self.start()
        self.service.receive(job, {'kind': 'metadata_status', 'key': self.service.request_key,
                                  'authenticated': True, 'state': 'ready', 'text': 'metadata only'})
        self.assertEqual(self.service.spotify_state, 'ready')
        self.assertTrue(self.service.spotify_authenticated)

    def test_failed_lookup_is_not_repeated_every_poll_but_next_playback_can_retry(self):
        job = self.start()
        self.service.receive(job, {'kind': 'metadata_status', 'key': self.service.request_key,
                                  'authenticated': True, 'state': 'not_found'})
        for _ in range(5):self.observe()
        self.assertEqual(len(job.commands), 1)
        self.observe(replace(self.playback, title='Other'))
        self.observe()
        requests = [command for command in job.commands if command.get('title') == 'Sparkle']
        self.assertEqual(len(requests), 2)

    def test_online_resume_retries_cancelled_metadata_once_without_lyrics_lookup(self):
        job = self.start()
        old = self.event()
        self.observe(enabled=False)
        self.assertEqual(job.commands[-1]['key'], '')
        self.observe(enabled=True)
        requests = [command for command in job.commands if command.get('metadata_only')]
        self.assertEqual(len(requests), 2)
        self.assertNotEqual(requests[0]['key'], requests[1]['key'])
        self.assertTrue(self.service.metadata_pending)
        self.assertFalse(self.service.spotify_pending)
        for _ in range(5):self.observe()
        self.assertEqual(len(job.commands), 3)
        self.service.receive(job, old)
        self.assertEqual(self.service.display_artists(self.playback), ())
        self.service.receive(job, self.event())
        self.assertEqual(self.service.display_artists(self.playback), tuple(self.track['artists']))

    def test_online_resume_keeps_successful_metadata_cached(self):
        job = self.start()
        self.service.receive(job, self.event())
        self.observe(enabled=False)
        self.observe(enabled=True)
        self.assertEqual(sum(command.get('metadata_only', False) for command in job.commands), 1)
        self.assertEqual(self.service.display_artists(self.playback), tuple(self.track['artists']))

    def test_regular_lyric_search_also_accepts_credits_without_returning_fake_lyrics(self):
        self.service.connect(visible=False)
        self.service.observe(self.playback, public_enabled=False)
        self.assertTrue(self.service.spotify_pending)
        self.assertIsNone(self.service.receive(self.service.job, self.event()))
        self.assertTrue(self.service.spotify_pending)
        self.assertEqual(self.service.display_artists(self.playback), tuple(self.track['artists']))


class PublicArtistFallbackTests(unittest.TestCase):
    def setUp(self):
        BackgroundArtistTests.setUp(self)
        self.public_path = Path(self.temp.name) / 'spotify_public_tracks.json'

    def tearDown(self):BackgroundArtistTests.tearDown(self)

    def write_public(self, *tracks):
        previous = self.public_path.stat().st_mtime_ns if self.public_path.exists() else None
        rows = [{**track, 'source_url': 'https://open.spotify.com/track/' + track['id']}
                for track in tracks]
        self.public_path.write_text(json.dumps({'schema_version': 1, 'tracks': rows}), encoding='utf-8')
        if previous is not None:
            os.utime(self.public_path, ns=(previous + 1_000_000_000, previous + 1_000_000_000))

    def test_offline_session_uses_matching_public_credits_without_merging_localized_release(self):
        english = ['Haruka Amami (CV: Eriko Nakamura)', 'Iori Minase (CV: Rie Kugimiya)',
                   'Makoto Kikuchi (CV: Hiromi Hirata)', 'Hibiki Ganaha (CV: Manami Numakura)',
                   'Nana Abe (CV: Marie Miyake)', 'Anzu Futaba (CV: Hiromi Igarashi)',
                   'Mirai Kasuga (CV: Haruka Yamazaki)']
        japanese = ['天海春香 (CV.中村繪里子)', '水瀬伊織 (CV: 釘宮理恵)',
                    '菊地真 (CV: 平田宏美)', '我那覇 響 (CV.沼倉愛美)',
                    '安部菜々 (CV: 三宅麻理恵)', '双葉杏 (CV: 五十嵐裕美)',
                    '春日未来 (CV.山崎はるか)', '大崎甘奈 (CV.黒木ほの香)',
                    '大崎甜花 (CV.前川涼子)', '奥空心白 (CV.田中あいみ)']
        playback = Playback('SESSION!', english[0], 'THE IDOLM@STER STARLIT SEASON 01',
                            0, 252.12, True)
        track = {'id': '79f6QbSLICz3jkM2yVlTgg', 'title': playback.title,
                 'album': playback.album, 'duration': playback.duration, 'artists': english}
        self.write_public({**track, 'id': '7wrPBplgfbspTUVGprzGdP', 'artists': japanese}, track)
        original_key = playback.key
        self.service.observe(playback, enabled=False, public_enabled=False, cached_sync=True)
        self.assertFalse(self.service.spotify_authenticated)
        self.assertEqual(self.service.display_artists(playback), tuple(english))
        self.assertEqual(ArtistMetadata(self.temp.name).get(playback), tuple(english))
        self.assertEqual(playback.key, original_key)
        self.assertEqual(playback.artist, english[0])
        self.assertIsNone(self.service.job)
        self.service.fetch.assert_not_called()

    def test_existing_verified_artist_cache_takes_priority_over_public_fallback(self):
        self.service.artist_metadata.put(self.playback, self.track)
        self.write_public({**self.track, 'artists': [self.playback.artist, 'Other public credit']})
        self.assertEqual(self.service.display_artists(self.playback), tuple(self.track['artists']))
        self.assertEqual(ArtistMetadata(self.temp.name).get(self.playback), tuple(self.track['artists']))

    def test_public_fallback_requires_album_and_matching_complete_recording_identity(self):
        self.write_public(self.track)
        for changes in ({'title': 'Sparkle (Live)'}, {'album': 'Other'}, {'duration': 260},
                        {'artist': 'Other Singer'}, {'album': ''}, {'duration': 0},
                        {'title': ''}, {'artist': ''}):
            with self.subTest(changes=changes):
                self.assertEqual(self.service.display_artists(replace(self.playback, **changes)), ())
        self.assertEqual(self.service.artist_metadata.entries, {})

    def test_public_fallback_rejects_ambiguous_spotify_track_ids(self):
        self.write_public(self.track, {**self.track, 'id': 'b' * 22})
        self.assertEqual(self.service.display_artists(self.playback), ())
        self.assertEqual(self.service.artist_metadata.entries, {})

    def test_public_file_creation_and_same_size_update_retry_a_previous_miss(self):
        self.assertEqual(self.service.display_artists(self.playback), ())
        self.write_public({**self.track, 'title': 'Sporkle'})
        self.assertEqual(self.service.display_artists(self.playback), ())
        before = self.public_path.stat()
        self.write_public(self.track)
        after = self.public_path.stat()
        self.assertEqual(after.st_size, before.st_size)
        self.assertGreater(after.st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(self.service.display_artists(self.playback), tuple(self.track['artists']))

    def test_public_credits_skip_redundant_metadata_lookup_for_cached_lyrics(self):
        self.write_public(self.track)
        self.service.connect(visible=False)
        self.service.observe(self.playback, public_enabled=False, cached_sync=True)
        self.assertEqual(self.service.display_artists(self.playback), tuple(self.track['artists']))
        self.assertEqual(self.service.job.commands, [])
        self.assertFalse(self.service.spotify_pending)
        self.assertFalse(self.service.metadata_pending)
        self.service.fetch.assert_not_called()


if __name__ == '__main__':unittest.main()
