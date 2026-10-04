from pathlib import Path
import tempfile
import unittest
from lyrics import Song,Line
from native_timing import NativeTiming
from dataclasses import replace
import hashlib
from storage import atomic_json
from unittest.mock import patch
import gc
import weakref
import json
from live_timing import original_signature


class NativeTimingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)
        self.song=Song('song','Test','','',tuple(Line(x,'','') for x in ('first','second','third','fourth')))
        self.native=NativeTiming(self.path)
    def tearDown(self):self.temp.cleanup()
    def observe(self,source,target,pos,now,eligible=True):
        return self.native.observe(self.song,100,source,target,pos,now,eligible)
    def test_witnessed_transition_is_saved_without_lyric_text(self):
        self.assertFalse(self.observe(0,0,10,1))
        self.assertTrue(self.observe(1,1,10.1,1.1))
        self.assertEqual(NativeTiming(self.path).select(self.song,100),[None,10.1,None,None])
        text=next((self.path/'native_timings').glob('*.json')).read_text()
        self.assertNotIn('second',text);self.assertNotIn('Test',text)
    def test_first_sample_seek_long_gap_and_skipped_rows_are_not_starts(self):
        for source,target,pos,now in [(2,2,10.1,1.1),(1,1,40,1.1),(1,1,13,4)]:
            self.native.reset_observation();self.observe(0,0,10,1)
            self.assertFalse(self.observe(source,target,pos,now))
        self.assertEqual(self.native.times,[None]*4)
    def test_ineligible_sample_and_explicit_seek_break_observation(self):
        self.observe(0,0,10,1);self.observe(0,0,10.1,1.1,False)
        self.assertFalse(self.observe(1,1,10.2,1.2))
        self.native.reset_observation();self.assertFalse(self.observe(2,2,10.3,1.3))
    def test_other_edit_or_duration_does_not_reuse_times(self):
        self.observe(0,0,10,1);self.observe(1,1,10.1,1.1)
        self.assertEqual(NativeTiming(self.path).select(self.song,120),[None]*4)

    def test_translation_and_reading_refresh_reuses_numeric_times_after_restart(self):
        self.observe(0,0,10,1);self.observe(1,1,10.1,1.1)
        translated=replace(self.song,lines=tuple(Line(l.original,'ふりがな','번역') for l in self.song.lines))
        self.assertEqual(NativeTiming(self.path).select(translated,100),[None,10.1,None,None])
        regrouped=replace(translated,lines=translated.lines[:3])
        self.assertEqual(NativeTiming(self.path).select(regrouped,100),[None]*3)

    def test_legacy_numeric_cache_is_still_read(self):
        legacy=self.path/'native_timings'/(hashlib.sha256((self.song.id+self.song.fingerprint).encode()).hexdigest()+'.json')
        atomic_json(legacy,{'times':[None,10.1,None,None],'duration':100})
        self.assertEqual(self.native.select(self.song,100),[None,10.1,None,None])

    def test_repeated_display_frames_do_not_rehash_unchanged_lyrics(self):
        self.native.select(self.song,100)
        with patch('native_timing.original_signature',side_effect=AssertionError('unchanged immutable song')):
            for _ in range(20):self.native.select(self.song,100)

    def test_cache_does_not_retain_transient_lyric_text_after_track_release(self):
        transient=replace(self.song,id='temporary')
        reference=weakref.ref(transient)
        self.native.select(transient,100)
        del transient
        gc.collect()
        self.assertIsNone(reference())

    def test_bad_files_are_preserved_and_repaired_file_reloads_in_same_object(self):
        path=self.path/'native_timings'/(hashlib.sha256((self.song.id+original_signature(self.song)).encode()).hexdigest()+'.json')
        path.parent.mkdir()
        valid={'duration':100,'times':[10,20,30,40]}
        invalid=[None,[],[1],'invalid',True,{'times':valid['times']},{'duration':100}]
        invalid.extend({**valid,**change} for change in (
            {'duration':'100'},{'duration':None},{'duration':True},{'duration':-1},
            {'duration':10**400},{'duration':float('nan')},{'duration':float('inf')},
            {'times':None},{'times':[10]},{'times':[20,10,30,40]},
            {'times':[False,20,30,40]},{'times':[10,20,30,10**400]}))
        for value in invalid:
            with self.subTest(value=value):
                native=NativeTiming(self.path)
                path.write_text(json.dumps(value),encoding='utf-8');before=path.read_bytes()
                for _ in range(2):
                    with self.assertRaises(ValueError):native.select(self.song,100)
                self.assertIsNone(native.key)
                self.assertIsNone(native.song_ref)
                self.assertEqual(path.read_bytes(),before)
                path.write_text(json.dumps(valid),encoding='utf-8')
                self.assertEqual(native.select(self.song,100),valid['times'])

    def test_corrupt_legacy_file_can_be_repaired_without_restart(self):
        legacy=self.path/'native_timings'/(hashlib.sha256((self.song.id+self.song.fingerprint).encode()).hexdigest()+'.json')
        legacy.parent.mkdir();legacy.write_text('null',encoding='utf-8')
        with self.assertRaises(ValueError):self.native.select(self.song,100)
        self.assertEqual(legacy.read_text(encoding='utf-8'),'null')
        atomic_json(legacy,{'duration':100,'times':[10,20,30,40]})
        self.assertEqual(self.native.select(self.song,100),[10,20,30,40])

    def test_failed_read_does_not_cache_empty_times(self):
        with patch('native_timing.read_json',side_effect=OSError('temporary read failure')):
            with self.assertRaises(OSError):self.native.select(self.song,100)
        with patch('native_timing.read_json',return_value={'duration':100,'times':[10,20,30,40]}):
            self.assertEqual(self.native.select(self.song,100),[10,20,30,40])

    def test_failed_new_selection_clears_previous_recording_and_observation(self):
        self.observe(0,0,10,1);self.observe(1,1,10.1,1.1)
        self.assertIsNotNone(self.native.previous)
        with patch('native_timing.read_json',return_value={'duration':'invalid'}):
            with self.assertRaises(ValueError):self.native.select(self.song,120)
        self.assertIsNone(self.native.key)
        self.assertIsNone(self.native.previous)
        self.assertEqual(self.native.times,[None]*4)
        self.assertEqual(self.native.select(self.song,100),[None,10.1,None,None])

    def test_invalid_requested_duration_raises_value_error_before_read(self):
        for duration in (None,'100',True,-1,10**400,float('nan'),float('inf')):
            with self.subTest(duration=duration),patch('native_timing.read_json') as read:
                with self.assertRaises(ValueError):self.native.select(self.song,duration)
                read.assert_not_called()

    def test_failed_write_preserves_memory_and_file_then_witnessed_retry_succeeds(self):
        self.native.select(self.song,100)
        atomic_json(self.native.path,{'duration':100,'times':[5,None,None,None]})
        self.native=NativeTiming(self.path)
        self.observe(0,0,10,1);before=self.native.path.read_bytes()
        with patch('native_timing.atomic_json',side_effect=OSError('temporary write failure')):
            with self.assertRaises(OSError):self.observe(1,1,10.1,1.1)
        self.assertEqual(self.native.times,[5,None,None,None])
        self.assertEqual(self.native.path.read_bytes(),before)
        self.native.reset_observation();self.observe(0,0,10,2)
        self.assertTrue(self.observe(1,1,10.1,2.1))
        self.assertEqual(NativeTiming(self.path).select(self.song,100),[5,10.1,None,None])
