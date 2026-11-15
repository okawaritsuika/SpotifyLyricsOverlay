from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from queue import Queue
import copy
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from background_lyrics import PlaybackLyricMerge, mapped_times
from lyrics import Line, Song
import playlist_lyrics_sources as sources
from storage import Store


class PlaylistSourcesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.track = {'id': 'A'*22, 'title': 'Test Song', 'artists': ['Test Singer'],
                      'album': 'Test Album', 'duration': 180,
                      'url': 'https://open.spotify.com/track/'+'A'*22,
                      'source_url': 'https://open.spotify.com/track/'+'A'*22, 'is_playable': None}
        self.lines = (Line('朝の光を見上げて', '', ''), Line('明日の花を描く', '', ''), Line('最後の言葉を歌う', '', ''))
        self.translated = tuple(Line(row.original, '', meaning) for row, meaning in zip(self.lines, ('아침의 빛을 올려다봐', '내일의 꽃을 그려', '마지막 말을 노래해')))
        self.spotify = {'source': 'Spotify', 'provider_id': self.track['id'], 'lines': self.lines, 'times': [1, 20, 40]}
        self.patches = {}
        for name in ('public_lyrics.fetch_public_lyrics', 'alsong_provider.fetch_alsong',
                     'lyricsplus_provider.fetch_lyricsplus', 'web_lyrics.fetch_lyrics'):
            manager = patch('playlist_lyrics_sources.'+name, return_value=None)
            self.patches[name] = manager.start()
            self.addCleanup(manager.stop)
        manager = patch.object(sources, 'REQUEST_INTERVAL', 0)
        manager.start()
        self.addCleanup(manager.stop)
        sources._next_request = 0
        sources._browser_cooldown = 0
        for value in (sources._cooldowns, sources._local_cache, sources._store_cache, sources._annotation_indexes):value.clear()

    def run_track(self, existing=None, track=None):
        return sources.complete_track(self.directory, self.directory, track or self.track, existing, threading.Event())

    def test_library_annotations_complete_without_network_and_preserve_spotify(self):
        Store(self.directory).save_library([Song('saved', self.track['title'], 'Test Singer', 'Short album label', self.translated)])
        result = self.run_track(self.spotify)
        self.assertTrue(result['complete'])
        self.assertEqual(result['lines'], self.translated)
        self.assertEqual(result['direct'], self.spotify)
        self.assertEqual(result['direct']['times'], [1, 20, 40])
        self.assertEqual(self.spotify['lines'], self.lines)
        for mock in self.patches.values():mock.assert_not_called()

    def test_complete_legacy_document_checks_alternate_originals_once(self):
        existing = {'lines': self.translated, 'direct': self.spotify}
        result = self.run_track(existing)
        self.assertTrue(result['complete'])
        self.assertEqual(result['times'], [1, 20, 40])
        self.patches['alsong_provider.fetch_alsong'].assert_called_once()
        self.patches['web_lyrics.fetch_lyrics'].assert_called_once()
        for mock in self.patches.values():mock.reset_mock()
        self.assertTrue(self.run_track(result)['complete'])
        for mock in self.patches.values():mock.assert_not_called()

    def test_same_title_partial_original_cannot_borrow_translation(self):
        Store(self.directory).save_library([Song('saved', self.track['title'], 'Test Singer', 'Test Album', self.translated[:-1])])
        result = self.run_track(self.spotify)
        self.assertFalse(result['complete'])
        self.assertEqual(result['counts']['translated'], 0)
        self.assertEqual(result['missing'], ['korean_translation'])

    def test_wiki_lookup_keeps_fullwidth_spelling_after_removing_version(self):
        title = 'ハロー＊ランチタイム'
        self.run_track(track={**self.track, 'title': title+' (Live Ver.)'})
        call = self.patches['web_lyrics.fetch_lyrics'].call_args
        self.assertEqual(call.args[:2], (title, sources.web_lyrics.namu_url(title)))

    def test_annotation_only_alsong_never_supplies_clock(self):
        self.patches['alsong_provider.fetch_alsong'].return_value = {
            'source': 'ALSong', 'provider_id': '123', 'annotation_only': True,
            'lines': self.translated, 'times': [10, 30, 50]}
        result = self.run_track({'lines': self.lines, 'direct': None})
        self.assertEqual(result['lines'], self.translated)
        self.assertIsNone(result['direct'])
        self.assertFalse(result['complete'])
        self.assertEqual(result['missing'], ['timing'])

    def test_alsong_translation_preserves_all_spotify_onsets(self):
        self.patches['alsong_provider.fetch_alsong'].return_value = {
            'source': 'ALSong', 'provider_id': '123', 'lines': self.translated, 'times': [10, 30, 50]}
        result = self.run_track(self.spotify)
        self.assertTrue(result['complete'])
        self.assertEqual(result['direct'], self.spotify)
        self.patches['public_lyrics.fetch_public_lyrics'].assert_not_called()
        self.patches['lyricsplus_provider.fetch_lyricsplus'].assert_not_called()

    def test_verified_fallback_only_fills_missing_spotify_boundary(self):
        spotify = {**self.spotify, 'times': [1, None, 40]}
        self.patches['alsong_provider.fetch_alsong'].return_value = {
            'source': 'ALSong', 'provider_id': '123', 'lines': self.translated, 'times': [1, 30, 40]}
        result = self.run_track(spotify)
        self.assertEqual(spotify['times'], [1, None, 40])
        self.assertEqual(mapped_times(result['direct'], result['lines']), [1, 30, 40])
        self.assertEqual([row['selected_source'] for row in result['rows']], ['Spotify', 'ALSong', 'Spotify'])
        self.assertTrue(result['complete'])

    def test_auxiliary_clock_conflicting_with_spotify_leaves_gap_unresolved(self):
        spotify = {**self.spotify, 'times': [1, None, 40]}
        self.patches['alsong_provider.fetch_alsong'].return_value = {
            'source': 'ALSong', 'provider_id': '123', 'lines': self.translated, 'times': [10, 30, 50]}
        result = self.run_track(spotify)
        self.assertEqual(result['times'], [1, None, 40])
        self.assertEqual(result['rows'][1]['status'], 'unresolved')
        self.assertEqual(result['rows'][1]['timings'][0]['status'], 'rejected')

    def test_merged_dialogue_and_provider_snapshots_survive_playlist_store_reload(self):
        from playlist_lyrics_store import PlaylistLyricsStore

        self.track['album'] = 'THE IDOLM@STER SideM Test Album'
        original = '【春香】「準備はいいですか？」'
        speech = '準備はいいですか？'
        self.patches['alsong_provider.fetch_alsong'].return_value = {
            'source': 'ALSong', 'provider_id': '123',
            'lines': (self.lines[0], Line(speech, '', ''), *self.lines[1:]),
            'times': [1, 10, 20, 40]}

        def wiki(title, url, *args, **kwargs):
            lines = (self.translated[0], Line(speech, '', '준비됐나요?'), *self.translated[1:])
            if sources.web_lyrics.source_id(url) == 'namu':
                lines = (self.lines[0], Line(original, '', ''), *self.lines[1:])
            return lines, {'url': url, 'title': title}

        self.patches['web_lyrics.fetch_lyrics'].side_effect = wiki
        result = self.run_track(self.spotify)
        self.assertEqual(result['lines'][1].original, original)
        self.assertEqual(result['lines'][1].korean, '준비됐나요?')
        self.assertEqual(result['times'], [1, 10, 20, 40])
        self.assertEqual(result['rows'][1]['translation_source'], 'sidem')
        self.assertEqual(result['rows'][1]['selected_source'], 'ALSong')
        store = PlaylistLyricsStore(self.directory)
        self.assertTrue(store.save(self.track, self.lines, self.spotify))
        self.assertTrue(store.save(self.track, result['lines'], result['direct'], result['sources'],
                                   snapshots=result['snapshots'], merge_rows=result['rows']))
        saved = PlaylistLyricsStore(self.directory).load(self.track['id'])
        self.assertEqual(saved['snapshots'], result['snapshots'])
        self.assertEqual(saved['merge_rows'], result['rows'])
        self.assertEqual(saved['direct'], result['direct'])
        self.assertEqual(self.run_track(saved)['rows'], result['rows'])

        messages = Queue()
        playback = sources.Playback(title=self.track['title'], artist=self.track['artists'][0],
                                    album=self.track['album'], duration=self.track['duration'])
        live = PlaybackLyricMerge(messages)
        for snapshot in reversed(result['snapshots']):
            live.remember(snapshot)
        live.submit(playback, (None, None, (), False, ()), ())
        kind, (_, _, _, live_result, error) = messages.get(timeout=5)
        self.assertEqual(kind, 'merged_lyrics')
        self.assertIsNone(error)
        self.assertEqual({field: live_result[field] for field in ('lines', 'rows', 'times')},
                         {field: result[field] for field in ('lines', 'rows', 'times')})

        self.patches['web_lyrics.fetch_lyrics'].side_effect = None
        initial = self.run_track(self.spotify)
        other_store = PlaylistLyricsStore(self.directory / 'late_wiki')
        self.assertTrue(other_store.save(self.track, initial['lines'], initial['direct'],
                                         snapshots=initial['snapshots'], merge_rows=initial['rows']))
        self.patches['web_lyrics.fetch_lyrics'].side_effect = wiki
        updated = self.run_track(other_store.load(self.track['id']))
        self.assertEqual(updated['lines'][1].original, original)
        self.assertTrue(other_store.save(self.track, updated['lines'], updated['direct'],
                                         snapshots=updated['snapshots'], merge_rows=updated['rows']))
        self.assertEqual(other_store.load(self.track['id'])['direct']['times'], [1, 10, 20, 40])

    def test_split_translation_keeps_spotify_source_rows(self):
        stored = (Line('朝の光を', '', '아침의 빛을 올려다봐'), Line('見上げて', '', '아침의 빛을 올려다봐'), *self.translated[1:])
        result = self.run_track({'lines': stored, 'direct': self.spotify})
        self.assertTrue(result['complete'])
        self.assertEqual([row.original for row in result['lines']], [row.original for row in self.lines])
        self.assertEqual(result['direct']['times'], [1, 20, 40])

    def test_unsupported_synthetic_provider_does_not_complete_timing(self):
        result = self.run_track({'lines': self.translated, 'direct': {**self.spotify, 'source': 'AI audio alignment'}})
        self.assertFalse(result['complete'])
        self.assertIsNone(result['direct'])
        self.assertEqual(result['missing'], ['timing'])

    def test_identical_original_with_music_marker_retains_actual_clock(self):
        lines = (Line('♪ ♪', '', ''), *self.translated)
        existing = {**self.spotify, 'lines': lines, 'times': [0, 1, 20, 40]}
        result = self.run_track(existing)
        self.assertEqual(result['counts']['timed'], 4)
        self.assertEqual(result['direct']['times'], [0, 1, 20, 40])
        self.assertEqual(result['missing'], ['korean_translation'])
        self.assertFalse(result['complete'])
        self.patches['public_lyrics.fetch_public_lyrics'].assert_not_called()

    def test_wrong_spotify_id_discards_reference_and_clock(self):
        result = self.run_track({**self.spotify, 'provider_id': 'B'*22})
        self.assertFalse(result['lines'])
        self.assertFalse(result['complete'])
        self.assertEqual(result['missing'], ['originals', 'timing', 'korean_translation'])

    def test_lrclib_synced_result_then_verified_wiki_translation(self):
        self.patches['public_lyrics.fetch_public_lyrics'].return_value = {**self.spotify, 'source': 'LRCLIB', 'provider_id': 7}
        url = 'https://namu.wiki/w/Test%20Song?tracking=secret#section'
        self.patches['web_lyrics.fetch_lyrics'].return_value = self.translated, {'url': url}
        result = self.run_track()
        self.assertTrue(result['complete'])
        self.assertIn('https://namu.wiki/w/Test%20Song', result['sources'])
        self.assertNotIn(url, result['sources'])
        self.assertEqual(result['direct']['source'], 'LRCLIB')
        self.assertEqual(self.patches['web_lyrics.fetch_lyrics'].call_args.args[3], self.lines)

    def test_wiki_selection_receives_current_spotify_originals_without_changing_clocks(self):
        self.patches['web_lyrics.fetch_lyrics'].return_value = self.translated, {'url': 'https://namu.wiki/w/Test%20Song'}
        result = self.run_track(self.spotify)
        self.assertEqual(self.patches['web_lyrics.fetch_lyrics'].call_args.args[3], self.lines)
        self.assertEqual(result['direct'], self.spotify)
        self.assertEqual(result['direct']['times'], [1, 20, 40])

    def test_wiki_originals_bootstrap_then_alsong_timing(self):
        self.patches['web_lyrics.fetch_lyrics'].return_value = self.translated, {'url': 'https://namu.wiki/w/Test%20Song'}
        self.patches['alsong_provider.fetch_alsong'].return_value = {**self.spotify, 'source': 'ALSong', 'provider_id': '7'}
        result = self.run_track()
        self.assertTrue(result['complete'])
        self.patches['alsong_provider.fetch_alsong'].assert_called_once()
        self.patches['lyricsplus_provider.fetch_lyricsplus'].assert_not_called()

    def test_other_wiki_document_whole_lyrics_rejected(self):
        wrong = (Line('別の物語の始まり', '', '다른 이야기'), *self.translated[1:])
        self.patches['web_lyrics.fetch_lyrics'].return_value = wrong, {'url': 'https://namu.wiki/w/Test%20Song'}
        result = self.run_track(self.spotify)
        self.assertEqual(result['counts']['translated'], 0)
        self.assertIn('whole_lyrics_mismatch', [entry['state'] for entry in result['attempts']])

    def test_permanent_store_duplicate_version_only_reuses_translation(self):
        from playlist_lyrics_store import PlaylistLyricsStore
        store = PlaylistLyricsStore(self.directory)
        other = {**self.track, 'id': 'B'*22, 'title': 'Test Song (2024 Version)', 'artists': ['Other Singer']}
        other['url'] = other['source_url'] = 'https://open.spotify.com/track/'+other['id']
        self.assertTrue(store.save(other, self.translated, {**self.spotify, 'provider_id': other['id'], 'times': [10, 30, 50]}))
        result = self.run_track(self.spotify)
        self.assertTrue(result['complete'])
        self.assertEqual(result['direct']['times'], [1, 20, 40])
        for mock in self.patches.values():mock.assert_not_called()

    def test_permanent_same_title_different_lyrics_cannot_transfer(self):
        from playlist_lyrics_store import PlaylistLyricsStore
        other = {**self.track, 'id': 'B'*22}
        other['url'] = other['source_url'] = 'https://open.spotify.com/track/'+other['id']
        self.assertTrue(PlaylistLyricsStore(self.directory).save(other, self.translated[:-1], None))
        result = self.run_track(self.spotify)
        self.assertEqual(result['counts']['translated'], 0)

    def test_429_chained_inside_wiki_error_propagates_retry_after(self):
        def blocked(*args, **kwargs):
            try:raise HTTPError('https://namu.wiki/w/Test', 429, 'slow', {'Retry-After': '123'}, None)
            except HTTPError as error:raise ValueError('blocked') from error
        self.patches['web_lyrics.fetch_lyrics'].side_effect = blocked
        with self.assertRaises(sources.SourceRateLimited) as context:self.run_track(self.spotify)
        self.assertEqual(context.exception.retry_after, 123)
        self.assertEqual(context.exception.source, 'namu')
        self.assertEqual(context.exception.code, 'rate_limited')

    def test_throttle_preserves_snapshots_obtained_before_retry(self):
        spotify = {**self.spotify, 'times': [1, None, 40]}
        lrclib = self.patches['public_lyrics.fetch_public_lyrics']
        lrclib.return_value = {**self.spotify, 'source': 'LRCLIB', 'provider_id': 42}
        wiki = self.patches['web_lyrics.fetch_lyrics']
        wiki.side_effect = HTTPError('https://namu.wiki/w/Test', 429, 'slow', {'Retry-After': '90'}, None)
        with self.assertRaises(sources.SourceRateLimited) as context:
            self.run_track(spotify)
        partial = context.exception.partial
        self.assertEqual(partial['times'], [1, 20, 40])
        self.assertEqual({snapshot['source'] for snapshot in partial['snapshots']}, {'Spotify', 'LRCLIB'})
        sources._cooldowns.clear()
        wiki.side_effect = None
        wiki.return_value = self.translated, {'url': 'https://namu.wiki/w/Test%20Song'}
        self.assertTrue(self.run_track(partial)['complete'])
        lrclib.assert_called_once()

    def test_http_date_retry_after_and_blocked_cooldown_are_honest(self):
        future = format_datetime(datetime.now(timezone.utc)+timedelta(seconds=180))
        error = HTTPError('https://namu.wiki/w/Test', 429, '', {'Retry-After': future}, None)
        self.assertTrue(177 <= sources._retry_after(error) <= 181)
        sources._cooldowns['namu'] = time.monotonic()+100
        with patch.object(sources, '_browser', side_effect=sources._Deferred('deferred_cooldown', 90)):
            result = self.run_track(self.spotify)
        self.patches['web_lyrics.fetch_lyrics'].assert_not_called()
        self.assertIn('deferred_cooldown', [entry['state'] for entry in result['attempts']])
        self.assertFalse(result['complete'])

    def test_http_cooldown_still_tries_healthy_browser_for_another_title(self):
        sources._cooldowns['namu'] = time.monotonic()+100
        track = {**self.track, 'id': 'B'*22, 'title': 'Another Song'}
        existing = {**self.spotify, 'provider_id': track['id']}
        with patch.object(sources, '_browser', return_value=(self.translated, ['https://namu.wiki/w/Another%20Song'])) as browser:
            result = self.run_track(existing, track)
        self.patches['web_lyrics.fetch_lyrics'].assert_not_called()
        browser.assert_called_once()
        self.assertEqual(browser.call_args.args[2].title, 'Another Song')
        self.assertTrue(result['complete'])
        self.assertEqual(result['direct']['times'], existing['times'])
        self.assertIn({'source': 'wiki_browser', 'state': 'verified'}, result['attempts'])

    def test_restore_cooldowns_preserves_elapsed_time_and_longest_provider_wait(self):
        sources._cooldowns['namu'] = 450
        state = {'enrichment_pending': {
            'first': {'retry_at': 1100, 'attempts': [
                {'source': source, 'state': 'deferred_cooldown', 'retry_after': 1800}
                for source in ('LRCLIB', 'ALSong', 'LyricsPlus', 'namu', 'sidem')]},
            'second': {'retry_at': 1200, 'attempts': [
                {'source': 'ALSong', 'state': 'request_failed', 'retry_after': 300},
                {'source': 'sidem', 'state': 'blocked', 'retry_after': 1800}]}}}
        original = copy.deepcopy(state)
        with patch.object(sources.time, 'time', return_value=1000), patch.object(sources.time, 'monotonic', return_value=100):
            sources.restore_cooldowns(state)
            sources.restore_cooldowns(state)
        self.assertEqual(sources._cooldowns, {'LRCLIB': 200, 'ALSong': 300, 'LyricsPlus': 200, 'namu': 450, 'sidem': 300})
        self.assertEqual(sources._browser_cooldown, 0)
        self.assertEqual(state, original)
        for mock in self.patches.values():mock.assert_not_called()

    def test_restored_http_wait_blocks_transport_until_original_deadline(self):
        state = {'enrichment_pending': {'track': {'retry_at': 1030, 'attempts': [
            {'source': 'namu', 'state': 'deferred_cooldown', 'retry_after': 1800}]}}}
        transport = Mock(return_value='received')
        with patch.object(sources.time, 'time', return_value=1000), patch.object(sources.time, 'monotonic', return_value=100):
            sources.restore_cooldowns(state)
            with self.assertRaises(sources._Deferred) as context:
                sources._request('namu', transport, threading.Event(), 1000)
        self.assertEqual(context.exception.state, 'deferred_cooldown')
        self.assertEqual(context.exception.retry_after, 30)
        transport.assert_not_called()
        with patch.object(sources.time, 'monotonic', return_value=130):
            self.assertEqual(sources._request('namu', transport, threading.Event(), 1000), 'received')
        transport.assert_called_once()

    def test_restore_browser_wait_is_separate_and_cannot_shorten_existing_wait(self):
        import internal_reader
        state = {'enrichment_pending': {
            str(index): {'retry_at': 1100+index*10, 'attempts': [
                {'source': 'wiki_browser', 'state': status, 'retry_after': 1800}]}
            for index, status in enumerate(('blocked', 'browser_error', 'browser_timeout', 'deferred_cooldown'))}}
        with patch.object(sources.time, 'time', return_value=1000), patch.object(sources.time, 'monotonic', return_value=100):
            sources.restore_cooldowns(state)
            self.assertEqual(sources._browser_cooldown, 230)
            sources._browser_cooldown = 500
            sources.restore_cooldowns(state)
            with patch.object(internal_reader, 'available', return_value=True), patch.object(internal_reader, 'BrowserJob') as browser:
                with self.assertRaises(sources._Deferred) as context:
                    sources._browser(self.directory, self.directory, None, self.lines, threading.Event(), 1000)
                browser.assert_not_called()
        self.assertEqual(context.exception.retry_after, 400)
        self.assertEqual(sources._cooldowns, {})

    def test_restore_ignores_malformed_expired_and_unevidenced_waits(self):
        good = {'source': 'namu', 'state': 'deferred_cooldown', 'retry_after': 100}
        entries = [None, [], {}, {'retry_at': 1100, 'attempts': None}]
        entries += [{'retry_at': value, 'attempts': [good]} for value in
                    (None, True, '1100', float('nan'), float('inf'), 10**1000, 999, 1000)]
        entries += [{'retry_at': 1100, 'attempts': [{**good, 'retry_after': value}]} for value in
                    (None, True, '100', float('nan'), float('inf'), 10**1000, 0, -1)]
        entries.append({'retry_at': 1100, 'attempts': [None, [],
            {**good, 'source': []}, {**good, 'source': 'saved_library'},
            {**good, 'state': []}, {**good, 'state': 'received'},
            {**good, 'state': 'deferred_deadline'}, {**good, 'state': 'no_verified_result'},
            {**good, 'source': 'wiki_browser', 'state': 'no_verified_document'},
            {**good, 'source': 'wiki_browser', 'state': 'browser_unavailable'},
            {'source': 'namu', 'state': 'blocked'}, {'source': 'namu', 'state': 'request_failed'}]})
        with patch.object(sources.time, 'time', return_value=1000), patch.object(sources.time, 'monotonic', return_value=100):
            for value in (None, [], {}, {'enrichment_pending': []}, {'enrichment_pending': dict(enumerate(entries))}):
                sources.restore_cooldowns(value)
        self.assertEqual(sources._cooldowns, {})
        self.assertEqual(sources._browser_cooldown, 0)
        for mock in self.patches.values():mock.assert_not_called()

    def test_cancellation_and_deadline_issue_no_network(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(sources._Cancelled):
            sources.complete_track(self.directory, self.directory, self.track, self.spotify, cancel)
        with patch.object(sources, 'TRACK_SECONDS', 0):
            with self.assertRaises(sources._Deferred):self.run_track(self.spotify)
        for mock in self.patches.values():mock.assert_not_called()

    def test_browser_uses_isolated_profile_and_full_reference_guard(self):
        import internal_reader
        jobs = []
        translated = '\n'.join(text for row in self.translated for text in (row.original, '발음', row.korean))
        class FakeBrowser:
            def __init__(job, base, state, events, **kwargs):
                jobs.append((job, kwargs, state))
                events.put(('internal_reader', (job, {'kind': 'lyrics', 'value': {
                    'url': 'https://namu.wiki/w/Test%20Song', 'text': translated, 'match_mode': 'title', 'user_selected': True}})))
            def close(job):job.closed = True
        with patch.object(internal_reader, 'available', return_value=True), patch.object(internal_reader, 'BrowserJob', FakeBrowser):
            rows, provenance = sources._browser(self.directory, self.directory,
                sources.Playback(title='Test Song', artist='Test Singer', album='Test Album', duration=180),
                self.lines, threading.Event(), time.monotonic()+10)
        self.assertEqual([row.korean for row in rows], [row.korean for row in self.translated])
        self.assertEqual(jobs[0][1]['profile'], self.directory/'playlist_lyrics'/'browser_profile')
        self.assertFalse(jobs[0][1]['visible'])
        self.assertEqual(jobs[0][2]['full_reference_lines'], tuple(row.original for row in self.lines))
        self.assertTrue(jobs[0][0].closed)
        self.assertEqual(provenance, ['https://namu.wiki/w/Test%20Song'])
        # Two shared lines satisfy the reader's discovery guard, but the bulk
        # worker still rejects a different last verse despite user_selected.
        translated = '\n'.join(text for row in (*self.translated[:2], Line('別の物語の始まり', '', '다른 이야기'))
                                for text in (row.original, '발음', row.korean))
        with patch.object(internal_reader, 'available', return_value=True), patch.object(internal_reader, 'BrowserJob', FakeBrowser):
            with self.assertRaisesRegex(ValueError, 'Different whole lyric'):
                sources._browser(self.directory, self.directory,
                    sources.Playback(title='Test Song', artist='Test Singer', album='Test Album', duration=180),
                    self.lines, threading.Event(), time.monotonic()+10)
        self.assertTrue(jobs[-1][0].closed)

    def test_missing_browser_document_is_not_a_global_browser_failure(self):
        import internal_reader
        class MissingDocument:
            def __init__(job, base, state, events, **kwargs):
                events.put(('internal_reader', (job, {'kind': 'error', 'error_code': 'no_table',
                    'text': '일치하는 가사 표를 찾지 못했습니다.'})))
            def close(job):pass
        with patch.object(internal_reader, 'available', return_value=True), patch.object(internal_reader, 'BrowserJob', MissingDocument):
            with self.assertRaises(sources._Deferred) as context:
                sources._browser(self.directory, self.directory,
                    sources.Playback(title='Test Song', artist='Test Singer', album='Test Album', duration=180),
                    self.lines, threading.Event(), time.monotonic()+10)
        self.assertEqual(context.exception.state, 'no_verified_document')
        self.assertEqual(context.exception.details['error_code'], 'no_table')
        self.assertEqual(context.exception.retry_after, 0)
        self.assertEqual(sources._browser_cooldown, 0)


if __name__ == '__main__':unittest.main()
