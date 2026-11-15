from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import wave

from audio_capture_cache import recording_directory
from japanese_alignment import CTC, HYBRID
from live_timing import LiveTimingStore
from local_analysis import AnalysisStore
from lyric_cache import LyricCache
from lyrics import Line, Song
from media import Playback
from storage import atomic_json
from sync_reanalysis_sources import BATCH_CTC_PROFILE, analyze, discover


class ReanalysisSourceTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.lines = ['歌いましょう', 'また明日']
        self.audio = self.directory / 'song.wav'
        self.write_audio(self.audio)

    def write_audio(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), 'wb') as wav:
            wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            wav.writeframes((b'\0\x08\0\xf8') * 24000)

    def test_discover_keeps_existing_jobs_and_protects_supplied_rows(self):
        store = AnalysisStore(self.directory)
        spec = {'kind': 'group', 'participants': ['a'], 'lines': self.lines, 'times': [0.25, None],
                'title': 'Song', 'track_id': 'a' * 22}
        job_id = store.register(self.audio, spec)
        job = store.get(job_id)
        store.update(job, 'done', 'aligned', {'alignment': {'times': spec['times'], 'engine': 'supplied'}})
        original = store.get(job_id)
        item, = discover(self.directory)
        self.assertTrue(item['eligible'])
        self.assertTrue(item['manual'])
        self.assertFalse(item['auto'])
        self.assertEqual(item['protected'], [True, False])
        self.assertEqual(item['before']['times'], [0.25, None])
        self.assertEqual(store.get(job_id), original)

        self.audio.unlink()
        missing, = discover(self.directory)
        self.assertFalse(missing['eligible'])
        self.assertEqual(missing['path'], str(self.audio))

    def test_capture_uses_original_signature_and_recording_clock(self):
        playback = Playback('Title', 'Artist', 'Album', duration=3.)
        song = Song('memory-' + sha256(playback.key.encode()).hexdigest()[:24], playback.title,
                    playback.artist, playback.album, tuple(Line(text, '', '') for text in self.lines))
        LyricCache(self.directory / 'lyrics_cache').save(playback, song.lines)
        timing = LiveTimingStore(self.directory)
        timing.save(song, {'times': [0.2, 1.4], 'origins': ['manual', 'audio'], 'offset': .2})
        capture = recording_directory(self.directory / 'audio_captures', playback.key) / '3'
        self.write_audio(capture / 'capture.wav')
        atomic_json(capture / 'capture-info.json', {'duration': 3., 'start_position': .25, 'clip_tail': .25})
        item, = discover(self.directory)
        self.assertTrue(item['live'])
        self.assertEqual(item['fingerprint'], timing.signature(song))
        self.assertEqual(item['audio_kind'], 'capture')
        self.assertEqual(item['protected'], [True, False])
        self.assertEqual(item['base_times'], [0.2, 1.4])
        self.assertAlmostEqual(item['before']['times'][1], 1.6)
        result = {'engine': CTC, 'times': [0.35, 1.7], 'line_alignment': [{}, {}]}
        with patch('local_analysis_worker._align', return_value=result) as align:
            candidate = analyze(item, {}, self.directory)
        self.assertEqual(candidate['times'], result['times'])
        self.assertEqual(align.call_args.args[0].size, 44000)
        self.assertEqual(align.call_args.kwargs['start_position'], .25)

    def test_cache_changes_with_readings_settings_and_engine_and_force_runs_alignment(self):
        item = {'path': str(self.audio), 'eligible': True, 'lines': self.lines}
        result = {'engine': CTC, 'times': [0.25, 1.25], 'line_alignment': [{}, {}]}
        cache = {}
        with patch('local_analysis_worker._align', return_value=result) as align:
            first = analyze(item, {}, self.directory, lookup=cache.get)
            self.assertEqual(align.call_args.args[1]['alignment']['profile'], BATCH_CTC_PROFILE)
            cache[first['cache_key']] = first
            cached = analyze(item, {}, self.directory, lookup=cache.get)
            self.assertTrue(cached['cached'])
            self.assertEqual(align.call_count, 1)
            forced = analyze(item, {}, self.directory, force=True, lookup=cache.get)
            self.assertFalse(forced['cached'])
            self.assertEqual(forced['cache_key'], first['cache_key'])
            readings = analyze({**item, 'line_readings': ['うたいましょう', 'またあした']}, {}, self.directory)
            settings = analyze(item, {'audio_device': 'cpu'}, self.directory)
            align.return_value = {**result, 'engine': HYBRID}
            experimental = analyze(item, {}, self.directory, engine='experimental')
            self.assertEqual(align.call_args.args[1]['alignment']['engine'], HYBRID)
        self.assertEqual(len({first['cache_key'], readings['cache_key'], settings['cache_key'],
                              experimental['cache_key']}), 4)


if __name__ == '__main__':
    unittest.main()
