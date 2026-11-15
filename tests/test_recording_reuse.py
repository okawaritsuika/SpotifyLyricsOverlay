from dataclasses import replace
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lyric_cache import LyricCache
from lyrics import Line, Song
from live_timing import LiveTimingStore
from media import Playback
from playlist_lyrics_store import PlaylistLyricsStore


class RecordingReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cache = LyricCache(self.root / 'lyrics_cache')
        self.timings = LiveTimingStore(self.root)
        self.playback = Playback('キラキラ - GAME VERSION', '歌手', 'Album A', duration=200)
        self.target = replace(self.playback, title='キラキラ (Game Ver.)', album='Album B', duration=200.3)
        self.lines = (Line('青い空を見る', '독음', '번역'), Line('花を描く', '', ''))
        self.direct = {'source': 'Spotify', 'lines': self.lines, 'times': [1, 15], 'provider_id': 'track123'}

    def tearDown(self):self.temp.cleanup()

    def save(self, playback=None, lines=None, direct=None):
        self.assertTrue(self.cache.save(playback or self.playback, lines or self.lines,
                                        direct if direct is not None else self.direct))

    def test_game_spelling_and_release_reuse_text_and_clock_without_spotify_claim(self):
        self.save()
        before = {path.name: path.read_bytes() for path in self.cache.directory.iterdir()}
        self.assertIsNone(self.cache.load(self.target))
        reused = self.cache.load_related(self.target, self.timings)
        self.assertEqual(reused['lines'], self.lines)
        self.assertEqual(reused['direct']['times'], [1, 15])
        self.assertEqual(reused['direct']['source'], '저장 캐시')
        self.assertIsNone(reused['direct']['provider_id'])
        self.assertTrue(reused['reuse']['timing_compatible'])
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.cache.directory.iterdir()})

    def test_current_recording_does_not_conflict_with_related_reference(self):
        self.save()
        target=replace(self.target,artist='Other singer')
        self.assertTrue(self.cache.save(target,self.lines,{**self.direct,'times':[7,22]}))
        with patch('recording_reuse._work_evidence',return_value=True):
            reused=self.cache.load_related(target)
        self.assertEqual(reused['tracking_times'],[1,15])
        self.assertFalse(reused['reuse']['tracking_conflict'])

    def test_conflicting_stores_cannot_supply_a_reference(self):
        from recording_reuse import related_reference
        candidates=[{'lines':self.lines,'tracking_times':times}
                    for times in ([1,15],[4,18])]
        self.assertIsNone(related_reference(self.lines,candidates))

    def test_an_internal_store_conflict_cannot_be_hidden_by_another_store(self):
        from recording_reuse import related_reference
        candidates=[{'lines':self.lines,'tracking_times':None,'reuse':{'tracking_conflict':True}},
                    {'lines':self.lines,'tracking_times':[1,15]}]
        self.assertIsNone(related_reference(self.lines,candidates))

    def test_collapsed_whole_song_zero_is_not_a_phrase_clock(self):
        from background_lyrics import mapped_times
        collapsed={'source':'LyricsPlus','lines':(Line(''.join(x.original for x in self.lines),'',''),),
                   'times':[0]}
        self.assertEqual(mapped_times(collapsed,self.lines),[None,None])
        self.assertEqual(mapped_times({**collapsed,'times':[11]},self.lines),[11,None])

    def test_missing_game_suffix_can_reuse_an_equal_length_recording(self):
        self.save()
        self.assertIsNotNone(self.cache.load_related(replace(self.target, title='キラキラ'))['direct'])

    def test_nearby_length_reuses_text_but_does_not_stretch_clock(self):
        self.save()
        reused = self.cache.load_related(replace(self.target, duration=207))
        self.assertEqual(reused['lines'], self.lines)
        self.assertIsNone(reused['direct'])
        self.assertFalse(reused['reuse']['timing_compatible'])

    def test_two_second_clock_boundary(self):
        self.save()
        self.assertIsNotNone(self.cache.load_related(replace(self.target, duration=202))['direct'])
        self.assertIsNone(self.cache.load_related(replace(self.target, duration=202.01))['direct'])

    def test_full_and_short_length_mismatch_cannot_reuse_even_text(self):
        self.save()
        self.assertIsNone(self.cache.load_related(replace(self.target, title='キラキラ', duration=300)))

    def test_other_artist_solo_live_and_remix_are_not_title_aliases(self):
        self.save()
        for changes in ({'artist': '別の歌手'}, {'title': 'キラキラ (Solo Ver.)'},
                        {'title': 'キラキラ (Live)'}, {'title': 'キラキラ (Remix)'}):
            with self.subTest(changes=changes):
                self.assertIsNone(self.cache.load_related(replace(self.target, **changes)))

    def test_different_concert_release_does_not_share_clock(self):
        self.save(replace(self.playback, title='キラキラ (Live)'))
        self.assertIsNone(self.cache.load_related(replace(self.target, title='キラキラ (Live)')))

    def test_live_word_in_composition_name_is_not_a_concert_qualifier(self):
        self.save(replace(self.playback, title='DRIVE A LIVE'))
        result = self.cache.load_related(replace(self.target, title='DRIVE A LIVE (GAME Ver.)'))
        self.assertIsNotNone(result['direct'])

    def test_tv_and_game_equal_lengths_share_text_only(self):
        self.save()
        result = self.cache.load_related(replace(self.target, title='キラキラ (TV Size)'))
        self.assertEqual(result['lines'], self.lines)
        self.assertIsNone(result['direct'])

    def test_ambiguous_originals_are_a_miss(self):
        self.save()
        different = (Line('別の最初の歌詞', '', ''), self.lines[1])
        self.save(replace(self.playback, album='Conflicting'), different,
                  {**self.direct, 'lines': different})
        self.assertIsNone(self.cache.load_related(self.target))

    def test_known_singers_with_spelling_variants_choose_complete_translation_without_clock(self):
        lines=tuple(Line(f'青い空を見る花を描く今日の物語その{i}番', '', '번역') for i in range(12))
        variant=tuple(Line(row.original.replace('空','そら'), '', '') for row in lines)
        self.save(lines=lines,direct={**self.direct,'lines':lines,'times':list(range(12))})
        self.save(replace(self.playback,artist='Third Singer',album='Other release',duration=201),variant,
                  {**self.direct,'lines':variant,'times':list(range(12))})
        with patch('recording_reuse._work_evidence',return_value=True):
            result=self.cache.load_related(replace(self.target,artist='別の歌手'))
        self.assertEqual(result['lines'],lines)
        self.assertIsNone(result['direct'])
        self.assertIsNone(result['timing'])
        self.assertIsNone(result['tracking_times'])
        self.assertTrue(result['reuse']['tracking_conflict'])

    def test_known_work_still_rejects_divergent_lyric_documents(self):
        lines=tuple(Line(f'青い空を見る花を描く今日の物語その{i}番','','') for i in range(12))
        different=tuple(Line(f'全然違う歌詞の内容を歌っているその{i}番','','') for i in range(12))
        self.save(lines=lines,direct={**self.direct,'lines':lines,'times':list(range(12))})
        self.save(replace(self.playback,artist='Third Singer'),different,
                  {**self.direct,'lines':different,'times':list(range(12))})
        with patch('recording_reuse._work_evidence',return_value=True):
            self.assertIsNone(self.cache.load_related(replace(self.target,artist='別の歌手')))

    def test_divergent_other_singer_documents_cannot_choose_different_length_or_cut(self):
        self.save()
        variant=(Line('青いそらを見る','','번역'),self.lines[1])
        self.save(replace(self.playback,artist='Third Singer',duration=201),variant,
                  {**self.direct,'lines':variant})
        with patch('recording_reuse._work_evidence',return_value=True):
            self.assertIsNone(self.cache.load_related(replace(self.target,artist='別の歌手',duration=210)))
            self.assertIsNone(self.cache.load_related(replace(self.target,artist='別の歌手',title='キラキラ')))

    def test_conflicting_provider_clocks_keep_text_and_discard_clocks(self):
        self.save()
        self.save(replace(self.playback, album='Conflicting'), direct={**self.direct, 'times': [3, 18]})
        result = self.cache.load_related(self.target)
        self.assertEqual(result['lines'], self.lines)
        self.assertIsNone(result['direct'])
        self.assertIsNone(result['timing'])
        self.assertTrue(result['reuse']['timing_conflict'])

    def saved_song(self, lines=None, prefix='memory-'):
        return Song(prefix + hashlib.sha256(self.playback.key.encode()).hexdigest()[:24],
                    self.playback.title, self.playback.artist, self.playback.album, lines or self.lines)

    def test_saved_final_alignment_is_available_with_original_offset(self):
        self.assertTrue(self.cache.save(self.playback, self.lines))
        self.timings.save(self.saved_song(), {'times': [2, 17], 'origins': ['audio', 'audio'],
                                           'audio_finalized': True, 'offset': .5})
        result = self.cache.load_related(self.target, self.timings)
        self.assertEqual(result['timing']['times'], [2, 17])
        self.assertEqual(result['timing']['offset'], .5)
        self.assertTrue(result['timing']['audio_finalized'])
        self.assertIsNone(result['direct'])

    def test_unfinished_machine_times_are_not_reused_but_manual_rows_survive(self):
        self.assertTrue(self.cache.save(self.playback, self.lines))
        self.timings.save(self.saved_song(), {'times': [2, 17], 'origins': ['manual', 'audio'], 'offset': 0})
        result = self.cache.load_related(self.target, self.timings)
        self.assertEqual(result['timing']['times'], [2, None])
        self.assertEqual(result['timing']['origins'], ['manual', None])

    def test_different_original_signature_cannot_borrow_saved_timing(self):
        self.assertTrue(self.cache.save(self.playback, self.lines))
        self.timings.save(self.saved_song(lines=(Line('違う歌詞', '', ''), self.lines[1])),
                          {'times': [2, 17], 'offset': 0})
        self.assertIsNone(self.cache.load_related(self.target, self.timings)['timing'])

    def test_valid_manual_clock_does_not_hide_provider_end_overflow(self):
        self.save(direct={**self.direct, 'times': [1, 202]})
        self.timings.save(self.saved_song(), {'times': [2, 17], 'offset': 0})
        result = self.cache.load_related(replace(self.target, duration=198), self.timings)
        self.assertEqual(result['timing']['times'], [2, 17])
        self.assertIsNone(result['direct'])

    def test_reuse_provenance_prevents_two_second_chains(self):
        self.save()
        first = replace(self.target, duration=202)
        borrowed = self.cache.load_related(first)
        self.assertTrue(self.cache.save(first, borrowed['lines'], borrowed['direct'], reuse=borrowed['reuse']))
        # Remove the original cache so only the already-reused release remains.
        original = self.cache.directory / (hashlib.sha256(self.playback.key.encode()).hexdigest() + '.json')
        original.unlink()
        later = replace(first, album='Album C', duration=204)
        second = self.cache.load_related(later)
        self.assertEqual(second['reuse']['source_duration'], 200)
        self.assertIsNone(second['direct'])
        exact = self.cache.load(replace(first, duration=204))
        self.assertIsNone(exact['direct'])
        self.assertFalse(exact['reuse']['timing_compatible'])

    def test_corrupt_candidate_does_not_block_a_valid_related_entry(self):
        self.save()
        (self.cache.directory / ('f' * 64 + '.json')).write_text('{', encoding='utf-8')
        self.assertIsNotNone(self.cache.load_related(self.target))

    def test_key_filename_mismatch_does_not_supply_related_lyrics(self):
        self.save()
        original = next(self.cache.directory.glob('*.json'))
        original.rename(self.cache.directory / ('f' * 64 + '.json'))
        self.assertIsNone(self.cache.load_related(self.target))

    def test_invalid_reuse_provenance_cannot_replace_cache(self):
        self.save()
        before = next(self.cache.directory.glob('*.json')).read_bytes()
        self.assertFalse(self.cache.save(self.playback, self.lines, reuse={'source_key': 'wrong', 'source_duration': 200}))
        self.assertEqual(next(self.cache.directory.glob('*.json')).read_bytes(), before)

    def test_playlist_related_release_uses_same_policy_and_known_first_artist(self):
        store = PlaylistLyricsStore(self.root)
        track = {'id': 'a' * 22, 'title': self.playback.title, 'artists': [self.playback.artist, 'Second Singer'],
                 'album': self.playback.album, 'duration': self.playback.duration}
        self.assertTrue(store.save(track, self.lines, {**self.direct, 'provider_id': track['id']}))
        self.assertIsNone(store.find(self.target))
        result = store.find_related(self.target, self.timings)
        self.assertEqual(result['direct']['times'], [1, 15])
        self.assertEqual(result['direct']['source'], '저장 캐시')
        self.assertIsNone(store.find_related(replace(self.target, artist='Second Singer')))
        self.assertIsNone(store.find_related(replace(self.target, duration=300)))

    def test_registered_artist_alias_keeps_reuse_provenance_reloadable(self):
        original = replace(self.playback, artist='天海春香 (CV.中村繪里子)')
        target = replace(self.target, artist='아마미 하루카 (CV.나카무라 에리코)')
        self.save(original)
        result = self.cache.load_related(target)
        self.assertIsNotNone(result)
        self.assertTrue(self.cache.save(target, result['lines'], result['direct'], reuse=result['reuse']))
        self.assertEqual(self.cache.load(target)['direct']['times'], [1, 15])

    def test_text_reuse_accepts_thirty_seconds_but_not_thirty_one(self):
        self.save()
        for duration in (170, 230):
            result = self.cache.load_related(replace(self.target, duration=duration))
            self.assertEqual(result['lines'], self.lines)
            self.assertIsNone(result['direct'])
            self.assertIsNone(result['timing'])
            self.assertIsNone(result['tracking_times'])
        self.assertIsNone(self.cache.load_related(replace(self.target, duration=231)))

    def test_known_other_singer_reuses_text_and_tracking_hint_without_direct_clock(self):
        self.save()
        other = replace(self.target, artist='別の歌手')
        with patch('recording_reuse._work_evidence', return_value=True):
            result = self.cache.load_related(other)
        self.assertEqual(result['lines'], self.lines)
        self.assertIsNone(result['direct'])
        self.assertIsNone(result['timing'])
        self.assertEqual(result['tracking_times'], [1, 15])
        self.assertFalse(result['reuse']['same_artist'])
        self.assertTrue(result['reuse']['work_evidence'])

    def test_other_singer_thirty_second_difference_cannot_seed_clock(self):
        self.save()
        with patch('recording_reuse._work_evidence', return_value=True):
            result = self.cache.load_related(replace(self.target, artist='別の歌手', duration=230))
        self.assertEqual(result['lines'], self.lines)
        self.assertIsNone(result['direct'])
        self.assertIsNone(result['tracking_times'])

    def test_singer_version_and_million_casting_suffix_use_work_evidence(self):
        source = replace(self.playback, title='キラキラ (如月千早 Ver.)')
        self.save(source)
        target = replace(self.target, title='キラキラ (MILLIONC@STING04ver.)', artist='別の歌手')
        with patch('recording_reuse._work_evidence', return_value=True):
            result = self.cache.load_related(target)
        self.assertEqual(result['lines'], self.lines)
        self.assertEqual(result['tracking_times'], [1, 15])
        self.assertIsNone(result['direct'])
        with patch('recording_reuse._work_evidence', return_value=None):
            self.assertIsNone(self.cache.load_related(target))

    def test_different_cut_cannot_seed_other_singers_clock(self):
        self.save()
        with patch('recording_reuse._work_evidence', return_value=True):
            result = self.cache.load_related(replace(self.target, title='キラキラ', artist='別の歌手'))
        self.assertEqual(result['lines'], self.lines)
        self.assertIsNone(result['tracking_times'])

    def test_conflicting_other_singer_clocks_keep_text_without_tracking_hint(self):
        self.save()
        self.save(replace(self.playback, artist='Third Singer'), direct={**self.direct, 'times': [8, 25]})
        with patch('recording_reuse._work_evidence', return_value=True):
            result = self.cache.load_related(replace(self.target, artist='別の歌手'))
        self.assertEqual(result['lines'], self.lines)
        self.assertIsNone(result['direct'])
        self.assertIsNone(result['tracking_times'])
        self.assertTrue(result['reuse']['tracking_conflict'])

    def test_saved_other_singer_timing_applies_offset_only_to_tracking_hint(self):
        self.assertTrue(self.cache.save(self.playback, self.lines))
        self.timings.save(self.saved_song(), {'times': [2, 17], 'offset': .5})
        with patch('recording_reuse._work_evidence', return_value=True):
            result = self.cache.load_related(replace(self.target, artist='別の歌手'), self.timings)
        self.assertIsNone(result['timing'])
        self.assertEqual(result['tracking_times'], [2.5, 17.5])

    def test_same_text_with_different_row_splits_remains_reusable(self):
        self.save()
        split = (Line('青い空を', '', ''), Line('見る', '', ''), self.lines[1])
        self.save(replace(self.playback, album='Other split'), split,
                  {**self.direct, 'lines': split, 'times': [1, None, 15]})
        result = self.cache.load_related(self.target)
        self.assertIsNotNone(result)
        self.assertEqual(''.join(line.original for line in result['lines']), ''.join(line.original for line in self.lines))
        self.assertFalse(result['reuse']['timing_conflict'])
        self.assertEqual([stamp for stamp in result['direct']['times'] if stamp is not None], [1, 15])

    def test_playlist_known_other_singer_can_supply_text_and_tracking_hint(self):
        store = PlaylistLyricsStore(self.root)
        track = {'id': 'a' * 22, 'title': self.playback.title, 'artists': [self.playback.artist],
                 'album': self.playback.album, 'duration': self.playback.duration}
        self.assertTrue(store.save(track, self.lines, {**self.direct, 'provider_id': track['id']}))
        with patch('recording_reuse._work_evidence', return_value=True):
            result = store.find_related(replace(self.target, artist='別の歌手'))
        self.assertIsNone(result['direct'])
        self.assertEqual(result['tracking_times'], [1, 15])


if __name__ == '__main__':unittest.main()
