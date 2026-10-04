from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lyric_cache import LyricCache, MAX_ENTRY_BYTES, MAX_ROWS, MAX_TEXT
from lyrics import Line
from media import Playback


class LyricCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / 'lyrics_cache'
        self.cache = LyricCache(self.directory)
        self.playback = Playback('Crossing! (如月千早 Ver.)', '如月千早', 'Album', duration=200)
        self.lines = (Line('青い空を見る', '아오이 소라오 미루', '푸른 하늘을 본다'),
                      Line('花を描く', '하나오 에가쿠', '꽃을 그린다'))
        self.direct = {'source': 'Spotify', 'lines': self.lines, 'times': [0, 10.5], 'provider_id': 'track123'}

    def tearDown(self):
        self.temp.cleanup()

    def path(self, playback=None):
        key = (playback or self.playback).key
        return self.directory / (hashlib.sha256(key.encode('utf-8')).hexdigest() + '.json')

    def read(self):
        return json.loads(self.path().read_text(encoding='utf-8'))

    def write(self, value):
        self.path().write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def test_fresh_instance_restores_original_reading_translation_and_direct_timing(self):
        with patch('lyric_cache.time.time', return_value=1234.5):
            self.assertTrue(self.cache.save(self.playback, self.lines, self.direct))
        cached = LyricCache(self.directory).load(self.playback)
        self.assertEqual(cached, {'lines': self.lines, 'direct': self.direct, 'saved_at': 1234.5})
        self.assertIsInstance(cached['lines'], tuple)
        self.assertIsInstance(cached['direct']['lines'], tuple)

    def test_entries_only_exposes_validated_cache_and_does_not_modify_files(self):
        self.assertTrue(self.cache.save(self.playback,self.lines,self.direct))
        before=self.path().read_bytes()
        invalid=replace(self.playback,title='Invalid')
        self.assertTrue(self.cache.save(invalid,self.lines))
        self.path(invalid).write_text('{broken',encoding='utf-8')
        (self.directory/'unrelated.json').write_text('{}',encoding='utf-8')
        entries=self.cache.entries()
        self.assertEqual(len(entries),1)
        playback,cached=entries[0]
        self.assertEqual(playback.key,self.playback.key)
        self.assertEqual(cached['lines'],self.lines)
        self.assertEqual(self.path().read_bytes(),before)

    def test_unsynced_translated_lyrics_can_be_cached_without_provider(self):
        self.assertTrue(self.cache.save(self.playback, self.lines))
        self.assertIsNone(self.cache.load(self.playback)['direct'])
        direct = {**self.direct, 'times': [None, None]}
        self.assertTrue(self.cache.save(self.playback, self.lines, direct))
        self.assertEqual(self.cache.load(self.playback)['direct']['times'], [None, None])

    def test_spotify_and_one_auxiliary_keep_separate_clocks_through_restart(self):
        fallback={'source':'LRCLIB','lines':self.lines,'times':[1,12],'provider_id':42}
        direct={**self.direct,'times':[0,None],'fallback':fallback}
        self.assertTrue(self.cache.save(self.playback,self.lines,direct))
        restored=LyricCache(self.directory).load(self.playback)['direct']
        self.assertEqual(restored,direct)
        self.assertIsInstance(restored['fallback']['lines'],tuple)
        from background_lyrics import mapped_times
        self.assertEqual(mapped_times(restored,self.lines),[0,12])
        self.assertEqual(restored['times'],[0,None])

    def test_auxiliary_only_whitelisted_fields_are_serialized(self):
        fallback={'source':'LyricsPlus','lines':self.lines,'times':[1,12],'provider_id':None,
                  'access_token':'PRIVATE_FALLBACK_TOKEN','raw_response':{'secret':'PRIVATE_RAW'}}
        self.assertTrue(self.cache.save(self.playback,self.lines,{**self.direct,'fallback':fallback}))
        self.assertNotIn('PRIVATE_',self.path().read_text(encoding='utf-8'))
        self.assertEqual(set(self.read()['direct']['fallback']),{'source','lines','times','provider_id'})

    def test_restored_auxiliary_with_only_one_matching_phrase_cannot_fill_the_clock(self):
        fallback={'source':'LRCLIB','lines':(Line('Unrelated opening','',''),self.lines[1]),
                  'times':[1,12],'provider_id':42}
        direct={**self.direct,'times':[0,None],'fallback':fallback}
        self.assertTrue(self.cache.save(self.playback,self.lines,direct))
        restored=LyricCache(self.directory).load(self.playback)['direct']
        from background_lyrics import mapped_times
        self.assertEqual(mapped_times(restored,self.lines),[0,None])
        self.assertEqual(restored['times'],[0,None])

    def test_invalid_nested_or_non_spotify_fallback_is_rejected_without_replacement(self):
        self.assertTrue(self.cache.save(self.playback,self.lines,self.direct))
        before=self.path().read_bytes()
        fallback={'source':'LRCLIB','lines':self.lines,'times':[1,12],'provider_id':None}
        cases=({**self.direct,'fallback':None},
               {**self.direct,'fallback':{**fallback,'fallback':fallback}},
               {**self.direct,'fallback':{**fallback,'source':'Spotify'}},
               {**self.direct,'fallback':{**fallback,'times':[None,None]}},
               {**self.direct,'fallback':{**fallback,'times':[12,1]}},
               {**self.direct,'fallback':{**fallback,'times':[1,203]}},
               {**self.direct,'times':[None,None],'fallback':fallback},
               {**self.direct,'source':'LRCLIB','fallback':fallback})
        for direct in cases:
            with self.subTest(direct=direct):
                self.assertFalse(self.cache.save(self.playback,self.lines,direct))
                self.assertEqual(self.path().read_bytes(),before)

    def test_malformed_persisted_auxiliary_fails_closed(self):
        fallback={'source':'LRCLIB','lines':self.lines,'times':[1,12],'provider_id':None}
        self.assertTrue(self.cache.save(self.playback,self.lines,{**self.direct,'fallback':fallback}))
        valid=self.read()
        for change in ({'times':[12,1]},{'lines':[]},{'fallback':None},{'source':'Spotify'}):
            value={**valid,'direct':{**valid['direct'],'fallback':{**valid['direct']['fallback'],**change}}}
            self.write(value)
            self.assertIsNone(self.cache.load(self.playback))

    def test_full_title_artist_album_identity_is_never_shortened_or_normalized(self):
        self.assertTrue(self.cache.save(self.playback, self.lines, self.direct))
        variants = ({'title': 'Crossing!'}, {'title': 'Crossing! (天海春香 Ver.)'},
                    {'title': self.playback.title.lower()}, {'title': self.playback.title + ' '},
                    {'artist': 'Other Singer'}, {'album': 'Other Album'})
        for changes in variants:
            with self.subTest(changes=changes):
                self.assertIsNone(self.cache.load(replace(self.playback, **changes)))

    def test_duration_tolerance_is_two_seconds_and_requires_valid_playback_metadata(self):
        self.assertTrue(self.cache.save(self.playback, self.lines))
        for duration in (198, 202):
            self.assertIsNotNone(self.cache.load(replace(self.playback, duration=duration)))
        for duration in (197.99, 202.01):
            self.assertIsNone(self.cache.load(replace(self.playback, duration=duration)))
        for duration in (0, -1, float('nan'), float('inf'), True):
            changed = replace(self.playback, duration=duration)
            self.assertIsNone(self.cache.load(changed))
            self.assertFalse(self.cache.save(changed, self.lines))
        for changes in ({'title': ''}, {'artist': ''}, {'artist': ' '}, {'album': 'bad\0album'}):
            changed = replace(self.playback, **changes)
            self.assertIsNone(self.cache.load(changed))
            self.assertFalse(self.cache.save(changed, self.lines))

    def test_different_recordings_save_separate_files(self):
        other = replace(self.playback, title='Crossing!')
        self.assertTrue(self.cache.save(self.playback, self.lines, self.direct))
        self.assertTrue(self.cache.save(other, self.lines[:1]))
        self.assertEqual(len(list(self.directory.glob('*.json'))), 2)
        self.assertEqual(self.cache.load(self.playback)['lines'], self.lines)
        self.assertEqual(self.cache.load(other)['lines'], self.lines[:1])

    def test_provider_secret_fields_are_never_serialized(self):
        secret = {**self.direct, 'access_token': 'SUPER_SECRET_TOKEN',
                  'headers': {'Authorization': 'Bearer SUPER_SECRET_HEADER'},
                  'cookie': 'SUPER_SECRET_COOKIE', 'raw_response': {'secret': 'SUPER_SECRET_RAW'}}
        self.assertTrue(self.cache.save(self.playback, self.lines, secret))
        text = self.path().read_text(encoding='utf-8')
        self.assertNotIn('SUPER_SECRET', text)
        self.assertEqual(set(self.read()['direct']), {'source', 'lines', 'times', 'provider_id'})
        self.assertEqual(set(self.read()['lines'][0]), {'original', 'reading', 'korean'})

    def test_corrupt_or_wrong_identity_cache_is_a_nonfatal_miss(self):
        self.assertIsNone(self.cache.load(self.playback))
        self.assertFalse(self.directory.exists())
        self.assertTrue(self.cache.save(self.playback, self.lines, self.direct))
        valid = self.read()
        for body in ('not json', '[]', '{"version":1}', '{"version":', '[[' * 2000):
            self.path().write_text(body, encoding='utf-8')
            self.assertIsNone(self.cache.load(self.playback))
        for update in ({'key': 'wrong'}, {'version': 99}, {'version': True}, {'saved_at': float('nan')},
                       {'duration': 0}, {'lines': []}, {'lines': [{'original': 'missing fields'}]},
                       {'direct': {**valid['direct'], 'times': [10, 5]}}):
            self.write({**valid, **update})
            self.assertIsNone(self.cache.load(self.playback))

    def test_entry_read_and_write_sizes_are_bounded(self):
        self.assertFalse(self.cache.save(self.playback, (Line('x' * (MAX_TEXT + 1), '', ''),)))
        self.assertFalse(self.cache.save(self.playback, self.lines[:1] * (MAX_ROWS + 1)))
        large = tuple(Line('字' * MAX_TEXT, '', '') for _ in range(100))
        self.assertFalse(self.cache.save(self.playback, large))
        self.assertTrue(self.cache.save(self.playback, self.lines))
        self.path().write_bytes(b' ' * (MAX_ENTRY_BYTES + 1))
        self.assertIsNone(self.cache.load(self.playback))

    def test_invalid_rows_and_provider_timings_are_rejected_without_overwriting(self):
        self.assertTrue(self.cache.save(self.playback, self.lines, self.direct))
        before = self.path().read_bytes()
        for lines in ((), [None], [Line('', '', '')], [Line('valid', None, '')]):
            self.assertFalse(self.cache.save(self.playback, lines))
        for times in ([0], [10, 10], [-1, 10], [0, float('inf')], [True, 10], [0, 203]):
            self.assertFalse(self.cache.save(self.playback, self.lines, {**self.direct, 'times': times}))
        for update in ({'source': None}, {'source': ''}, {'provider_id': {}}, {'provider_id': True}):
            self.assertFalse(self.cache.save(self.playback, self.lines, {**self.direct, **update}))
        self.assertEqual(self.path().read_bytes(), before)

    def test_count_eviction_removes_oldest_owned_entry_only(self):
        cache = LyricCache(self.directory, max_entries=2)
        self.assertTrue(cache.save(self.playback, self.lines))
        os.utime(self.path(), (1, 1))
        other = replace(self.playback, title='Other')
        newest = replace(self.playback, title='Newest')
        self.assertTrue(cache.save(other, self.lines))
        os.utime(self.path(other), (2, 2))
        unrelated = self.directory / 'user-notes.json'
        unrelated.write_text('keep', encoding='utf-8')
        nested = self.directory / 'nested'
        nested.mkdir()
        (nested / self.path().name).write_text('keep nested', encoding='utf-8')
        self.assertTrue(cache.save(newest, self.lines))
        self.assertIsNone(cache.load(self.playback))
        self.assertIsNotNone(cache.load(other))
        self.assertIsNotNone(cache.load(newest))
        self.assertEqual(unrelated.read_text(encoding='utf-8'), 'keep')
        self.assertTrue((nested / self.path().name).exists())

    def test_byte_budget_evicts_oldest_and_rejects_individually_oversized_entry(self):
        self.assertTrue(self.cache.save(self.playback, self.lines))
        entry_bytes = self.path().stat().st_size
        os.utime(self.path(), (1, 1))
        cache = LyricCache(self.directory, max_bytes=entry_bytes + 80)
        other = replace(self.playback, title='Other')
        self.assertTrue(cache.save(other, self.lines))
        self.assertIsNone(cache.load(self.playback))
        self.assertIsNotNone(cache.load(other))
        self.assertLessEqual(sum(path.stat().st_size for path in self.directory.glob('*.json')), cache.max_bytes)
        self.assertFalse(LyricCache(self.directory, max_bytes=10).save(other, self.lines))

    def test_atomic_replace_failure_preserves_previous_entry_and_removes_temporary_file(self):
        self.assertTrue(self.cache.save(self.playback, self.lines, self.direct))
        before = self.path().read_bytes()
        with patch('lyric_cache.os.replace', side_effect=PermissionError('read only')):
            self.assertFalse(self.cache.save(self.playback, self.lines[:1]))
        self.assertEqual(self.path().read_bytes(), before)
        self.assertEqual(list(self.directory.glob('*.tmp')), [])

    def test_filesystem_failures_and_disabled_budget_are_nonfatal(self):
        with patch('lyric_cache.Path.mkdir', side_effect=PermissionError('read only')):
            self.assertFalse(self.cache.save(self.playback, self.lines))
        self.assertIsNone(self.cache.load(self.playback))
        self.assertFalse(LyricCache(self.directory, max_entries=0).save(self.playback, self.lines))
        self.assertFalse(LyricCache(self.directory, max_bytes=0).save(self.playback, self.lines))

    def test_failed_eviction_removes_new_entry_and_preserves_existing_file(self):
        self.assertTrue(self.cache.save(self.playback, self.lines))
        old_path = self.path()
        before = old_path.read_bytes()
        unlink = Path.unlink
        def blocked(path, *args, **kwargs):
            if path == old_path:raise PermissionError('cache entry locked')
            return unlink(path, *args, **kwargs)
        other = replace(self.playback, title='Other')
        with patch('lyric_cache.Path.unlink', blocked):
            self.assertFalse(LyricCache(self.directory, max_entries=1).save(other, self.lines))
        self.assertEqual(old_path.read_bytes(), before)
        self.assertFalse(self.path(other).exists())

    def test_cache_named_directory_is_not_deleted_or_used_as_entry(self):
        self.directory.mkdir()
        self.path().mkdir()
        note = self.path() / 'user-note.txt'
        note.write_text('keep directory content', encoding='utf-8')
        self.assertIsNone(self.cache.load(self.playback))
        self.assertFalse(self.cache.save(self.playback, self.lines))
        self.assertEqual(note.read_text(encoding='utf-8'), 'keep directory content')

    def test_root_directory_reparse_point_is_not_followed(self):
        real_lstat = Path.lstat
        self.directory.mkdir()
        info = real_lstat(self.directory)
        class LinkedInfo:
            st_mode = info.st_mode
            st_file_attributes = 0x400
        def lstat(path, *args, **kwargs):
            return LinkedInfo() if path == self.directory else real_lstat(path, *args, **kwargs)
        with patch('lyric_cache.Path.lstat', lstat):
            self.assertFalse(self.cache.save(self.playback, self.lines))
            self.assertIsNone(self.cache.load(self.playback))
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_linked_cache_entry_is_neither_read_overwritten_nor_pruned(self):
        self.assertTrue(self.cache.save(self.playback, self.lines))
        original = self.path().read_bytes()
        real_lstat = Path.lstat
        info = real_lstat(self.path())
        class LinkedInfo:
            st_mode = info.st_mode
            st_file_attributes = 0x400
        def lstat(path, *args, **kwargs):
            return LinkedInfo() if path == self.path() else real_lstat(path, *args, **kwargs)
        with patch('lyric_cache.Path.lstat', lstat):
            self.assertIsNone(self.cache.load(self.playback))
            self.assertFalse(self.cache.save(self.playback, self.lines[:1]))
            other = replace(self.playback, title='Other')
            self.assertTrue(LyricCache(self.directory, max_entries=1).save(other, self.lines))
        self.assertEqual(self.path().read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
