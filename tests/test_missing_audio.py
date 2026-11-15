from collections import deque
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import time
import unittest
from unittest.mock import Mock, patch

from lyrics import Line
from media import Playback
from missing_audio import ANALYZED_TRACKS, CollectionPlaylist, MissingAudioCollector, missing_tracks
from playlist_lyrics_store import PlaylistLyricsStore
from spotify_catalog import parse_playlist_id
from storage import atomic_json


class MissingAudioTests(unittest.TestCase):
    def test_playlist_link_accepts_spotify_share_links_and_rejects_track_links(self):
        identifier = 'a' * 22
        self.assertEqual(parse_playlist_id(f'https://open.spotify.com/intl-ko/playlist/{identifier}?si=share'), identifier)
        self.assertEqual(parse_playlist_id(f'spotify:playlist:{identifier}'), identifier)
        self.assertIsNone(parse_playlist_id(f'https://open.spotify.com/track/{identifier}'))
        self.assertIsNone(parse_playlist_id(f'https://open.spotify.com.example/playlist/{identifier}'))

    def test_new_playlist_uses_its_order_and_checks_uncached_lyrics(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / 'output/mp3'
            audio.mkdir(parents=True)
            store = PlaylistLyricsStore(root)
            service = SimpleNamespace(job=Mock(), spotify_authenticated=True, spotify_pending=False,
                                      metadata_pending=False)
            app = SimpleNamespace(store=SimpleNamespace(directory=root), background_lyrics=service,
                                  playlist_lyrics=SimpleNamespace(store=store))
            collector = MissingAudioCollector(app, root)
            first = {'id': 'a' * 22, 'title': 'Same title', 'artists': ['First'], 'album': 'Album', 'duration': 90}
            second = {**first, 'id': 'b' * 22, 'artists': ['Second']}
            original = [{'track_id': first['id'], 'track': first}]
            atomic_json(root / 'playlist_lyrics/manifest.json', {'entries': original})
            (audio / '0001 Same title.mp3').write_bytes(b'audio')
            lines = (Line('Lyrics', '', ''),)
            self.assertTrue(store.save(first, lines, dict(source='Spotify', provider_id=first['id'],
                                                         lines=lines, times=[None])))
            request = CollectionPlaylist(collector, 'p' * 22)
            self.assertIsNone(request.poll())
            rows = [dict(index=i, status='track', track_id=track['id'], track=track)
                    for i, track in enumerate([second, first, second])]
            initial_id = request.request_id
            self.assertTrue(request.receive(service.job, dict(kind='catalog_playlist_progress', request_id=initial_id,
                                                             playlist_id='p' * 22, entries=rows[:1], start=0,
                                                             offset=1, total=3)))
            self.assertIsNone(request.poll())
            self.assertTrue(request.receive(service.job, dict(kind='catalog_playlist_result', request_id=initial_id,
                                                             playlist_id='p' * 22, entries=rows[1:], start=1,
                                                             offset=3, total=3, complete=True)))
            self.assertIsNone(request.poll())
            command = service.job.send.call_args.args[0]
            self.assertEqual((command['action'], command['track']['id']), ('batch_lyrics', second['id']))
            self.assertTrue(request.receive(service.job, dict(kind='batch_lyrics_result', request_id=command['request_id'],
                                                             provider_id=second['id'], lines=[{'text': 'New lyrics', 'time': None}])))
            request.next_at = 0
            result = collector.inventory(request.poll())
            self.assertEqual([(item['position'], item['track']['id']) for item in result], [(1, second['id'])])
            self.assertEqual(collector.inventory(), [])

    def test_incomplete_playlist_result_cannot_replace_the_selected_list(self):
        service = SimpleNamespace(job=Mock(), spotify_authenticated=True)
        collector = SimpleNamespace(app=SimpleNamespace(background_lyrics=service))
        request = CollectionPlaylist(collector, 'p' * 22)
        original_id = request.request_id
        self.assertFalse(request.receive(service.job, {'request_id': 'earlier'}))
        self.assertTrue(request.receive(service.job, dict(kind='catalog_playlist_result', request_id=original_id,
                                                         playlist_id='p' * 22, entries=[], start=0, offset=0,
                                                         total=2, complete=True)))
        with self.assertRaises(ValueError):
            request.poll()

    def test_inventory_requires_original_spotify_absence_and_no_local_audio(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / 'output/mp3'
            audio.mkdir(parents=True)
            lines = (Line('歌', '', ''),)
            records = {}
            entries = []
            for number, times in enumerate(([None], [None], [3], None), 1):
                track_id = str(number) * 22
                track = {'id': track_id, 'title': f'Track {number}', 'artists': ['Singer'],
                         'album': 'Album', 'duration': 90}
                entries.append({'track_id': track_id, 'track': track})
                direct = {'source': 'Spotify', 'provider_id': track_id, 'times': times} if times else None
                records[track_id] = {'track': track, 'lines': lines, 'direct': direct}
            atomic_json(root / 'playlist_lyrics/manifest.json', {'entries': entries})
            (audio / '0002 Track 2.mp3').write_bytes(b'audio')
            result = missing_tracks(root, audio, SimpleNamespace(load=records.get))
            self.assertEqual([item['position'] for item in result], [1])
            self.assertEqual(result[0]['reason'], '영상 링크 없음')

    def test_next_song_waits_for_analysis_and_completion_is_kept_without_audio(self):
        with TemporaryDirectory() as directory:
            app = SimpleNamespace(store=SimpleNamespace(directory=Path(directory)), playback=SimpleNamespace(playing=False),
                                  auto_analyzer=SimpleNamespace(capture_cache=Path(directory), progress={},jobs={}),
                                  background_lyrics=SimpleNamespace(artist_metadata=SimpleNamespace(entries={})))
            collector = MissingAudioCollector(app, directory)
            collector.active = True
            collector.quiet = Mock()
            song=SimpleNamespace(title='First',lines=(Line('Lyrics','',''),))
            job = Mock(capture_complete=True,analysis_committed='complete',song=song)
            collector.job = job
            collector.current = {'track': {'id': 'first', 'title': 'First'}}
            collector.jobs[job] = ('Track\0Artist\0Album', 90, 'first')
            collector.queue = deque([{'track': {'id': 'next', 'title': 'Next'}}])
            with patch.object(collector, '_play_next') as play_next:
                collector.tick()
                play_next.assert_not_called()
            self.assertEqual(collector.saved, 0)
            self.assertIn(job, collector.jobs)
            collector.receive(job, {'kind': 'finished'})
            self.assertEqual(collector.saved, 1)
            self.assertIsNone(collector.job)
            self.assertIn('first',(Path(directory)/ANALYZED_TRACKS).read_text(encoding='utf-8'))
            collector.next_tick = 0
            with patch.object(collector, '_play_next') as play_next:
                collector.tick()
                play_next.assert_called_once()
            self.assertFalse(collector.jobs)

    def test_stop_waits_for_spotify_pause_before_restoring_output(self):
        with TemporaryDirectory() as directory:
            app = SimpleNamespace(store=SimpleNamespace(directory=Path(directory)),
                                  playback=SimpleNamespace(has_timeline=True,playing=True,captured=time.monotonic()), monitor=Mock())
            collector = MissingAudioCollector(app, directory)
            collector.active = True
            collector.quiet = Mock()
            collector.stop()
            collector.tick()
            app.monitor.request_playback.assert_called_once_with('pause')
            collector.quiet.restore.assert_not_called()
            app.playback.playing = False
            collector.next_tick = 0
            collector.tick()
            collector.quiet.restore.assert_called_once()
            self.assertFalse(collector.stopping)

    def test_pauses_rewinds_and_waits_for_capture_worker_before_playing(self):
        with TemporaryDirectory() as directory:
            playback = Playback('Song', 'Singer', 'Album', 8, 90, True, time.monotonic())
            analyzer = Mock(jobs={}, progress={}, live_signatures={}, transport_generation=0,
                            capture_cache=Path(directory), signature=Mock(return_value='signature'))
            app = SimpleNamespace(store=SimpleNamespace(directory=Path(directory)), playback=playback,
                                  monitor=Mock(), is_bound=lambda: False, settings={}, auto_analyzer=analyzer)
            collector = MissingAudioCollector(app, directory)
            collector.active = True
            collector.quiet = Mock()
            collector.desktop.current_track = Mock(return_value=None)
            collector.current = {'track': {'id': 'a' * 22, 'title': 'Song', 'artists': ['Singer'],
                                            'album': 'Album', 'duration': 90}, 'lines': (Line('歌', '', ''),)}
            collector._start_capture()
            app.monitor.request_playback.assert_called_once_with('pause')
            app.playback = Playback('Song', 'Singer', 'Album', 8, 90, False, time.monotonic())
            collector._start_capture()
            app.monitor.request_seek.assert_called_once_with('first', playback.key)
            app.playback = Playback('Song', 'Singer', 'Album', 0, 90, False, time.monotonic() + .001)
            job = Mock(capture_complete=False, capture_started=False)
            with patch('missing_audio.AudioJob', return_value=job) as factory:
                collector._start_capture()
            self.assertTrue(factory.call_args.kwargs['playlist_analysis'])
            collector.tick()
            app.monitor.request_playback.assert_called_once()
            collector.receive(job, {'kind': 'progress', 'state': 'waiting'})
            collector.next_tick = 0
            collector.tick()
            app.monitor.request_playback.assert_called_with('play')
