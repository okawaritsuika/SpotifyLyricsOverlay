import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import format_time, parse_time
from lyrics import Line, Song, import_folder, match_song, normalized, parse_document
from media import Playback, timeline_position
from storage import Store, validate_times
from sync import Cue, active_line, align_lines, estimate_times, get_public_timing, parse_lrc, stamp_line


DOCUMENT = '''제목: 예시 자료
HOPEFUL FE@THERS -Example-
목차
01. Example Song - 테스트

━━━━━━━━━━━━━━━━━━━━
[01] Example Song - 테스트
━━━━━━━━━━━━━━━━━━━━

유튜브 풀버전: https://example.invalid/song

窓を開ける
まどを あける
창문을 열다

花を描く
はなを かく
꽃을 그리다

이어서 보기: 다음 편
https://example.invalid/next

[예시 1편 끝]
'''


def sample_song(count=3):
    return Song('sample', 'Example Song', 'Example Artist', 'Example Album',
                tuple(Line(f'例文{i}', f'れいぶん{i}', f'예시{i}') for i in range(count)))


class ImportTests(unittest.TestCase):
    def test_import_keeps_triples_and_excludes_navigation(self):
        songs = parse_document(DOCUMENT)
        self.assertEqual(len(songs), 1)
        self.assertEqual(songs[0].title, 'Example Song')
        self.assertEqual(songs[0].album, 'Example')
        self.assertEqual(songs[0].lines, (Line('窓を開ける', 'まどを あける', '창문을 열다'),
                                        Line('花を描く', 'はなを かく', '꽃을 그리다')))

    def test_folder_import_does_not_modify_source_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / 'example.txt'
            file.write_text(DOCUMENT, encoding='utf-8-sig')
            original = file.read_bytes()
            (Path(tmp) / 'copy.txt').write_bytes(original)
            self.assertEqual(len(import_folder(tmp)), 1)
            self.assertEqual(file.read_bytes(), original)

    def test_incomplete_triple_is_rejected_instead_of_shifting_translation(self):
        with self.assertRaises(ValueError):
            parse_document(DOCUMENT.replace('창문을 열다\n', ''))

    def test_title_case_spacing_fullwidth_normalization(self):
        song = sample_song()
        self.assertEqual(match_song([song], 'ＥＸＡＭＰＬＥ　ＳＯＮＧ'), song)
        self.assertIsNone(match_song([song], 'Example Song Instrumental'))

    def test_duplicate_title_requires_album_or_explicit_link(self):
        a = sample_song()
        b = Song('other', a.title, a.artist, 'Other Album', a.lines)
        self.assertIsNone(match_song([a,b], a.title))
        self.assertEqual(match_song([a,b], a.title, 'Other Album'), b)
        self.assertEqual(match_song([a,b], '別表記', linked_id='sample'), a)

    def test_exact_album_precedes_a_shorter_album_prefix(self):
        a = sample_song()
        b = Song('other', a.title, a.artist, a.album + ' 2', a.lines)
        self.assertEqual(match_song([a, b], a.title, b.album), b)

    def test_missing_album_is_not_evidence_for_a_duplicate_title(self):
        a = sample_song()
        b = Song('other', a.title, a.artist, '', a.lines)
        self.assertIsNone(match_song([a, b], a.title, 'Unrelated Album'))
        self.assertIsNone(match_song([a, b], a.title))


class SyncTests(unittest.TestCase):
    def test_lrc_multiple_timestamps_and_offset(self):
        cues = parse_lrc('[offset:250]\n[00:05.50][01:02.125]花\n[00:99.00]bad\n[00:04]')
        self.assertEqual(cues, [Cue(3.75,''), Cue(5.25,'花'), Cue(61.875,'花')])

    def test_repeated_chorus_gets_distinct_timestamps(self):
        lines = [Line(x,'','') for x in ['朝','花','夜','花']]
        cues = [Cue(t,x) for t,x in [(3,'朝'),(7,'花'),(14,'夜'),(20,'花')]]
        self.assertEqual(align_lines(lines,cues), [3,7,14,20])

    def test_unmatched_lines_stay_untimed(self):
        lines = [Line(x,'','') for x in ['朝','missing','夜']]
        self.assertEqual(align_lines(lines, [Cue(2,'朝'),Cue(8,'夜')]), [2,None,8])

    def test_delay_sign_preroll_and_seek(self):
        times = [10,20,30]
        self.assertIsNone(active_line(times, 10, 1))
        self.assertEqual(active_line(times, 11, 1), 0)
        self.assertEqual(active_line(times, 19, -1), 1)
        self.assertEqual(active_line(times, 31), 2)
        self.assertEqual(active_line(times, 12), 0)

    def test_stamp_subtracts_offset(self):
        self.assertEqual(stamp_line([None,None], 0, 10, 1.5), [8.5,None])

    def test_out_of_order_stamp_does_not_modify_original(self):
        times = [10,None,20]
        with self.assertRaises(ValueError):
            stamp_line(times, 1, 25)
        self.assertEqual(times, [10,None,20])

    def test_invalid_numeric_timestamps(self):
        for times in [[float('nan')], [float('inf')], [-1], [True], [2,2], [2,1]]:
            with self.subTest(times=times), self.assertRaises(ValueError):
                validate_times(times, len(times))

    def test_public_lookup_checks_text_and_duration_not_title_alone(self):
        song = sample_song(5)
        lrc = '\n'.join(f'[00:{i+10:02}.00]{line.original}' for i,line in enumerate(song.lines))
        rows = [dict(trackName=song.title, artistName=song.artist, duration=100, syncedLyrics=lrc, id=1),
                dict(trackName=song.title, artistName=song.artist, duration=202, syncedLyrics=lrc, id=2)]
        result = get_public_timing(song, 200, fetch=lambda params: rows)
        self.assertEqual(result['lrclib_id'], 2)
        self.assertEqual(result['matched'], 5)
        self.assertIsNone(get_public_timing(song, 180, fetch=lambda params: rows))

    def test_public_lookup_rejects_different_lyrics(self):
        song = sample_song()
        self.assertIsNone(get_public_timing(song, fetch=lambda params: [dict(
            trackName=song.title, artistName=song.artist, duration=100, syncedLyrics='[00:05]違う歌')]))

    def test_network_failure_is_not_reported_as_success(self):
        def failed(params):
            raise TimeoutError('timeout')
        with self.assertRaises(TimeoutError):
            get_public_timing(sample_song(), fetch=failed)

    def test_time_entry_formats(self):
        self.assertEqual(parse_time('01:12.5'), 72.5)
        self.assertEqual(parse_time('3.25'), 3.25)
        self.assertIsNone(parse_time(''))
        self.assertEqual(format_time(72.5), '01:12.50')
        for value in ['hello', '1:99', '1:2:3']:
            with self.assertRaises(ValueError):
                parse_time(value)


class PublicTimingSafetyTests(unittest.TestCase):
    def setUp(self):
        self.song = sample_song(5)
        self.item = {'trackName': self.song.title, 'artistName': self.song.artist,
                     'albumName': self.song.album, 'duration': 200, 'id': 1,
                     'syncedLyrics': '\n'.join(f'[00:{i+10:02}]{line.original}'
                                               for i, line in enumerate(self.song.lines))}

    def get(self, *rows):
        return get_public_timing(self.song, 200, fetch=lambda params: list(rows))

    def test_other_solo_singer_cannot_supply_identical_lyrics_and_duration(self):
        other = {**self.item, 'id': 2, 'artistName': 'Other Idol'}
        self.assertIsNone(self.get(other))
        self.assertEqual(self.get(other, self.item)['lrclib_id'], 1)

    def test_distinct_clocks_are_ambiguous_even_with_identical_lyrics(self):
        other = {**self.item, 'id': 2, 'syncedLyrics': self.item['syncedLyrics'].replace('00:1', '00:2')}
        self.assertIsNone(self.get(self.item, other))
        self.assertIsNone(self.get(other, self.item))

    def test_album_selects_matching_recording_and_identical_reuploads_are_allowed(self):
        other = {**self.item, 'id': 2, 'albumName': 'Other Album',
                 'syncedLyrics': self.item['syncedLyrics'].replace('00:1', '00:2')}
        self.assertEqual(self.get(other, self.item)['lrclib_id'], 1)
        self.assertEqual(self.get(self.item, {**self.item, 'id': 3})['times'], [10, 11, 12, 13, 14])

    def test_known_other_album_is_rejected_while_missing_album_remains_eligible(self):
        self.assertIsNone(self.get({**self.item, 'albumName': 'Other Album'}))
        for album in ('', None):
            with self.subTest(album=album):
                self.assertEqual(self.get({**self.item, 'albumName': album})['times'], [10, 11, 12, 13, 14])

    def test_missing_local_album_preserves_unambiguous_lookup(self):
        from dataclasses import replace
        item = {**self.item, 'albumName': 'Other Album'}
        result = get_public_timing(replace(self.song, album=''), 200, fetch=lambda _: [item])
        self.assertEqual(result['lrclib_id'], 1)

    def test_invalid_rows_and_nonfinite_duration_do_not_hide_valid_result(self):
        for invalid in (None, 'bad', {'duration': []}, {**self.item, 'duration': float('nan')},
                        {**self.item, 'duration': True}, {**self.item, 'duration': 10**1000}):
            with self.subTest(invalid=type(invalid).__name__):
                self.assertIsNone(self.get(invalid))
                self.assertEqual(self.get(invalid, self.item)['lrclib_id'], 1)

    def test_timestamps_past_recording_end_and_empty_song_are_rejected(self):
        self.assertIsNone(self.get({**self.item, 'syncedLyrics': self.item['syncedLyrics'].replace('00:', '10:')}))
        self.assertIsNone(get_public_timing(sample_song(0), 200, fetch=lambda params: [self.item]))

    def test_partial_matching_still_keeps_missing_rows_untimed(self):
        partial = {**self.item, 'id': 2,
                   'syncedLyrics': '\n'.join(self.item['syncedLyrics'].splitlines()[:-1])}
        self.assertEqual(self.get(partial)['times'], [10, 11, 12, 13, None])
        self.assertEqual(self.get(partial, self.item)['lrclib_id'], 1)

    def test_unverified_artist_and_invalid_duration_are_not_accepted(self):
        for artist in ('', None, {}, 'Another Singer'):
            self.assertIsNone(self.get({**self.item, 'artistName': artist}))
        for duration in (True, -1, float('nan'), float('inf'), 10**1000):
            self.assertIsNone(get_public_timing(self.song, duration,
                                              fetch=lambda params: self.fail('unexpected request')))

    def test_request_is_bounded_and_does_not_send_local_lyrics_or_artist(self):
        from urllib.parse import parse_qs, urlparse
        body = json.dumps([self.item]).encode('utf-8')
        with patch('sync.urlopen', return_value=io.BytesIO(body)) as request:
            self.assertEqual(get_public_timing(self.song, 200)['lrclib_id'], 1)
        self.assertEqual(parse_qs(urlparse(request.call_args.args[0].full_url).query),
                         {'track_name': [self.song.title]})
        with patch('sync.urlopen', return_value=io.BytesIO(b' ' * 4_000_001)):
            with self.assertRaises(ValueError):
                get_public_timing(self.song, 200)


class EstimateTests(unittest.TestCase):
    def test_longer_reading_receives_more_time(self):
        lines = [Line('短い','あ',''),Line('長い','あいうえおかきくけこ',''),Line('末尾','あ','')]
        times = estimate_times(lines,100,intro=10,outro=5)
        self.assertEqual(times[0],10)
        self.assertGreater(times[2]-times[1],times[1]-times[0])
        self.assertLess(times[-1],95)

    def test_manual_anchors_are_preserved_and_only_gaps_are_filled(self):
        lines = sample_song(6).lines
        known = [None,20,None,None,90,None]
        times = estimate_times(lines,120,known)
        self.assertEqual(times[1],20)
        self.assertEqual(times[4],90)
        self.assertEqual(known,[None,20,None,None,90,None])
        self.assertTrue(all(a<b for a,b in zip(times,times[1:])))
        self.assertTrue(all(0<=t<120 for t in times))

    def test_full_measured_timing_is_never_redistributed(self):
        times=[1,12,18,30]
        self.assertEqual(estimate_times(sample_song(4).lines,60,times),times)

    def test_short_song_stays_inside_duration(self):
        times=estimate_times(sample_song(5).lines,2)
        self.assertTrue(all(0<=t<2 for t in times))
        self.assertTrue(all(a<b for a,b in zip(times,times[1:])))

    def test_zero_time_anchor_does_not_fabricate_negative_preroll(self):
        times=estimate_times(sample_song(4).lines,30,[None,None,0,None])
        self.assertEqual(times[:3],[None,None,0])
        self.assertGreater(times[3],0)

    def test_unknown_duration_and_different_cut_keep_manual_data(self):
        self.assertEqual(estimate_times(sample_song(3).lines,0),[None]*3)
        self.assertEqual(estimate_times([],100),[])
        self.assertEqual(estimate_times(sample_song(3).lines,20,[1,None,21]),[1,None,21])

    def test_invalid_intro_and_outro_are_rejected(self):
        for options in [dict(intro=-1),dict(outro=float('nan')),dict(intro=float('inf'))]:
            with self.subTest(options=options),self.assertRaises(ValueError):
                estimate_times(sample_song().lines,100,**options)


class StorageTests(unittest.TestCase):
    def test_library_and_timing_survive_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            song = sample_song()
            store.save_library([song])
            store.save_timing(song, dict(times=[1,None,8], offset=-.5, source='manual'))
            again = Store(tmp)
            self.assertEqual(again.library(), [song])
            self.assertEqual(again.timing(song)['times'], [1,None,8])
            self.assertEqual(again.timing(song)['offset'], -.5)

    def test_changed_text_does_not_inherit_old_timestamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            song = sample_song(2)
            store.save_timing(song, dict(times=[10,20], offset=0))
            changed = Song(song.id, song.title, song.artist, song.album, (song.lines[1],song.lines[0]))
            self.assertEqual(store.timing(changed)['times'], [None,None])
            self.assertEqual(store.timing(song)['times'], [10,20])

    def test_bad_json_is_reported_and_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'settings.json'
            path.write_text('{bad', encoding='utf-8')
            with self.assertRaises(ValueError):
                Store(tmp).settings()
            self.assertEqual(path.read_text(encoding='utf-8'), '{bad')


class PlaybackTests(unittest.TestCase):
    def test_playing_extrapolates_but_paused_does_not(self):
        p = Playback('song', base=12, duration=80, playing=True, captured=100)
        self.assertEqual(p.position(105), 17)
        paused = Playback('song', base=12, duration=80, playing=False, captured=100)
        self.assertEqual(paused.position(105), 12)
        self.assertEqual(p.position(1000), 80)

    def test_windows_last_update_age_is_applied_once(self):
        base = timeline_position(12, 3, True, 80)
        p = Playback('song',base=base,duration=80,playing=True,captured=100)
        self.assertEqual(p.position(102), 17)
        self.assertEqual(timeline_position(12,3,False,80), 12)

    def test_missing_timeline_is_not_usable(self):
        self.assertFalse(Playback('song').has_timeline)
        self.assertFalse(Playback('song',duration=80,error='unavailable').has_timeline)


if __name__ == '__main__':
    unittest.main()
