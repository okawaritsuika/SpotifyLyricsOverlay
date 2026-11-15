from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from lyrics import Line
from storage import atomic_json

with patch.object(sys, 'path', [str(Path(__file__).resolve().parents[1] / 'tools'), *sys.path]):
    import playlist_youtube_mp3 as playlist


class AnniversaryLinkTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.output = self.directory / 'links.json'
        self.manifest = self.directory / 'manifest.json'
        self.playlist_id = 'p' * 22
        self.group = self.track('a', 'Harmony 4 You', '765 MILLION ALLSTARS', duration=295.973)
        self.solo = self.track('b', 'Harmony 4 You (舞浜 歩 Ver.)', '舞浜 歩 (CV.戸田めぐみ)')
        self.saved = self.track('c', 'Harmony 4 You (木下ひなた Ver.)', '木下ひなた (CV.田村奈央)')
        self.other = self.track('d', 'Harmony 4 You (Game Ver.)', '765 MILLION ALLSTARS', duration=296.)
        self.lines = (Line('ようこそ', '', ''), Line('歌おう', '', ''))
        self.records = {self.group['id']: {'lines': self.lines, 'direct': {
            'source': 'Spotify', 'provider_id': self.group['id'], 'lines': self.lines, 'times': [4., 9.]}}}
        self.store = Mock()
        self.store.metadata.return_value = [self.group]
        self.store.load.side_effect = self.records.get

    def track(self, identifier, title, artist, duration=297.5):
        return {'id': identifier * 22, 'title': title, 'artists': [artist], 'duration': duration,
                'album': 'THE IDOLM@STER MILLION LIVE! SPECIAL SOLO RECORDS'}

    def write_links(self, *tracks, **options):
        atomic_json(self.manifest, {'playlist_id': self.playlist_id, 'entries': [
            {'track_id': track['id'], 'track': track} for track in tracks]})
        args = SimpleNamespace(**{'manifest': self.manifest, 'output': self.output, 'command': 'links',
                                  'resolve': True, 'retry_unmatched': False, 'start': 1, 'limit': None,
                                  'include_anniversary_reference': False, **options})
        summary = StringIO()
        with patch.dict(os.environ, {'YOUTUBE_API_KEY': 'test'}), redirect_stdout(summary), \
                patch.object(playlist, 'PlaylistLyricsStore', return_value=self.store), \
                patch.object(playlist, 'candidate_links', return_value=[]) as search:
            playlist.write_links(args)
        return json.loads(summary.getvalue()), [call.args[0]['id'] for call in search.call_args_list]

    def test_links_skip_reference_but_preserve_saved_links_and_exact_spotify_status(self):
        url = 'https://www.youtube.com/watch?v=abcdefghijk'
        atomic_json(self.output, {'playlist_id': self.playlist_id, 'entries': [
            {'track_id': self.saved['id'], 'youtube_url': url}]})
        summary, searched = self.write_links(self.group, self.solo, self.saved, self.other)
        rows = json.loads(self.output.read_text(encoding='utf-8'))['entries']
        self.assertEqual(searched, [self.other['id']])
        self.assertEqual(summary['skipped_spotify_synced'], 1)
        self.assertEqual(summary['skipped_anniversary_reference'], 1)
        self.assertEqual(summary['eligible_video_links'], 1)
        self.assertTrue(rows[1]['anniversary_reference'])
        self.assertFalse(rows[1]['spotify_synced'])
        self.assertEqual(rows[2]['youtube_url'], url)
        self.assertEqual(playlist.load_links(self.output), [rows[2]])

    def test_explicit_override_searches_reusable_versions(self):
        summary, searched = self.write_links(self.solo, include_anniversary_reference=True)
        self.assertEqual(searched, [self.solo['id']])
        self.assertEqual(summary['skipped_anniversary_reference'], 0)

    def test_character_enrollment_still_searches_for_distinct_solo_audio(self):
        summary, searched = self.write_links(self.solo, command='solos', one_per_character=True,
                                             reuse=self.directory / 'shared.json', mp3_dir=self.directory)
        self.assertEqual(searched, [self.solo['id']])
        self.assertEqual(summary['characters'], 1)
        self.assertEqual(summary['skipped_anniversary_reference'], 0)

    def test_conflicting_exact_text_keeps_lookup_enabled(self):
        self.records[self.solo['id']] = {'lines': (Line('違う曲です', '', ''),), 'direct': None}
        summary, searched = self.write_links(self.solo)
        self.assertEqual(searched, [self.solo['id']])
        self.assertEqual(summary['skipped_anniversary_reference'], 0)

    def download_links(self, *tracks, include_reference=False):
        atomic_json(self.output, {'entries': [
            {**track, 'track_id': track['id'], 'position': position,
             'youtube_url': playlist.WATCH + track['id'][:11]}
            for position, track in enumerate(tracks, 1)]})
        args = SimpleNamespace(links=self.output, output=self.directory / 'mp3', yt_dlp='yt-dlp',
                               node=None, include_spotify_synced=False, reuse_mp3_dir=None,
                               include_anniversary_reference=include_reference)
        summary = StringIO()
        with redirect_stdout(summary), patch.object(playlist.shutil, 'which', return_value='yt-dlp'), \
                patch.object(playlist, 'PlaylistLyricsStore', return_value=self.store), \
                patch.object(playlist, 'download_source', side_effect=FileExistsError) as download:
            self.assertFalse(playlist.download_mp3s(args))
        return json.loads(summary.getvalue()), [call.args[1] for call in download.call_args_list]

    def test_mp3_skips_a_linked_anniversary_solo_using_current_store(self):
        summary, downloaded = self.download_links(self.group, self.solo,
                                                  {**self.other, 'anniversary_reference': True})
        self.assertEqual(downloaded, [playlist.WATCH + self.other['id'][:11]])
        self.assertEqual(summary['skipped_spotify_synced'], 1)
        self.assertEqual(summary['skipped_anniversary_reference'], 1)

    def test_mp3_explicit_override_and_solo_enrollment_keep_the_recording(self):
        for track, override in ((self.solo, True), ({**self.solo, 'solo_credit': {'character_id': 'ayumu'}}, False)):
            with self.subTest(override=override):
                summary, downloaded = self.download_links(track, include_reference=override)
                self.assertEqual(downloaded, [playlist.WATCH + self.solo['id'][:11]])
                self.assertEqual(summary['skipped_anniversary_reference'], 0)
