"""Provider-independent regressions for ordered, recording-scoped composition."""
import itertools
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alsong_provider import fetch_alsong
from lyric_cache import LyricCache
from lyric_match import align_phrases
from lyric_merge import make_snapshot, merge_lyrics, restore_cached_translations, snapshot_from_direct
from lyrics import Line, parse_document
from lyricsplus_provider import parse_payload
from media import Playback
from public_lyrics import fetch_public_lyrics
from sync import active_line
from tests.test_alsong_provider import fixture_fetch
from web_lyrics import parse_html


PLAYBACK = Playback('Example song', 'Unit', 'Album', duration=90)
ORIGINALS = ('夜明けを待っている', '扉を開いてみよう', '星が遠くから見える', 'またここで会おうね')
DIALOGUE = 'さあ、行こう！'


def document(source, originals=ORIGINALS, times=None, **options):
    options.setdefault('recording_verified', source in ('Spotify', 'ALSong', 'LRCLIB', 'LyricsPlus'))
    return make_snapshot(source, (row if isinstance(row, Line) else Line(row, '', '') for row in originals),
                         times, **options)


class LyricMergeTests(unittest.TestCase):
    def test_independent_dialogue_original_reading_translation_and_timing(self):
        spotify = document('Spotify', times=[12, 19, 27, 36])
        alsong = document('ALSong', (*ORIGINALS[:1], DIALOGUE, *ORIGINALS[1:]), [12, 16, 19, 27, 36])
        namu = document('namu', (*ORIGINALS[:1], f'蓮：「{DIALOGUE}」', *ORIGINALS[1:]))
        sidem = document('sidem', (*ORIGINALS[:1], Line(DIALOGUE, '', '자, 가자!'), *ORIGINALS[1:]))
        with patch('web_lyrics.reading', return_value='さあ いこう') as reading:
            result = merge_lyrics(PLAYBACK, (sidem, alsong, spotify, namu))
        self.assertEqual(result['times'], [12, 16, 19, 27, 36])
        self.assertEqual(result['lines'][1], Line(f'蓮：「{DIALOGUE}」', 'さあ いこう', '자, 가자!'))
        self.assertEqual(result['snapshots'][0]['lines'], spotify['lines'])
        self.assertEqual(result['rows'][1]['kind'], 'dialogue')
        self.assertEqual(result['rows'][1]['original_source'], 'namu')
        self.assertEqual(result['rows'][1]['selected_source'], 'ALSong')
        self.assertEqual(result['rows'][1]['translation_source'], 'sidem')
        self.assertEqual(result['rows'][1]['reading_source'], 'generated')
        self.assertEqual(len(result['rows'][2]['timings']), 2)
        self.assertEqual(result['rows'][2]['selected_source'], 'Spotify')
        reading.assert_called_once()

    def test_inline_speaker_quote_is_confirmed_by_separate_timed_row(self):
        spotify = document('Spotify', times=[12, 19, 27, 36])
        alsong = document('ALSong', (ORIGINALS[0], DIALOGUE, *ORIGINALS[1:]), [12, 16, 19, 27, 36])
        namu = document('namu', (ORIGINALS[0] + f' 蓮：「{DIALOGUE}」', *ORIGINALS[1:]))
        result = merge_lyrics(PLAYBACK, [spotify, alsong, namu])
        self.assertEqual(result['lines'][1].original, f'蓮：「{DIALOGUE}」')
        self.assertEqual(result['rows'][1]['original_index'], 0)
        self.assertEqual(result['times'], [12, 16, 19, 27, 36])

    def test_wiki_only_and_conflicting_version_dialogue_stay_out(self):
        spotify = document('Spotify', times=[12, 19, 27, 36], track_id='original')
        originals = (ORIGINALS[0], DIALOGUE, *ORIGINALS[1:])
        namu = document('namu', originals)
        for metadata in ({'title': 'Example song (Game Size)'}, {'title': 'Example song (Live)'},
                         {'artist': 'Solo'}, {'album': 'Other recording'}, {'duration': 55},
                         {'track_id': 'alternate'}):
            with self.subTest(metadata=metadata):
                alternate = document('ALSong', originals, [12, 16, 19, 27, 36], **metadata)
                result = merge_lyrics(PLAYBACK, [spotify, namu, alternate], track_id='original')
                self.assertEqual(result['lines'], spotify['lines'])
                self.assertEqual(result['times'], list(spotify['times']))

    def test_timing_holes_repeat_context_and_order_independence(self):
        first, chorus, bridge, last = ORIGINALS
        texts = (first, chorus, bridge, chorus, last)
        spotify = document('Spotify', texts, [10, None, 30, None, 50])
        left = document('ALSong', texts[:3] + texts[-1:], [10, 21, 30, 50])
        right = document('LRCLIB', texts[:1] + texts[2:], [10, 30, 43, 50])
        merged = [merge_lyrics(PLAYBACK, snapshots) for snapshots in itertools.permutations((spotify, left, right))]
        self.assertEqual(merged[0]['times'], [10, 21, 30, 43, 50])
        self.assertTrue(all(value == merged[0] for value in merged))
        self.assertEqual(active_line(merged[0]['times'], 44, offset=1), 3)
        self.assertEqual(active_line(merged[0]['times'], 22, offset=1), 1)
        self.assertEqual(align_phrases([chorus, chorus], [chorus]), ())

    def test_long_ordered_documents_align_between_unique_phrases(self):
        left = tuple('Repeat the chorus' if i in (10, 11) else f'Original line {i}' for i in range(600))
        right = left[:11] + left[12:280] + ('An additional spoken phrase',) + left[280:]
        blocks = align_phrases(left, right)
        self.assertNotIn(10, {block.left for block in blocks})
        self.assertNotIn(11, {block.left for block in blocks})
        self.assertEqual(blocks[-1].left, len(left) - 1)
        self.assertEqual(blocks[-1].right, len(right) - 1)

    def test_splits_transfer_complete_translation_and_only_common_onset(self):
        originals = ('目を閉じて', 'ゆっくり深呼吸', '星の光が見える', 'またここで会おうね')
        spotify = document('Spotify', originals, [None, None, 27, 36])
        supplied = (Line(originals[0] + originals[1], 'めをとじて', '눈을 감고 천천히 숨을 쉬어'),
                    *originals[2:])
        other = document('ALSong', supplied, [12, 27, 36])
        result = merge_lyrics(PLAYBACK, [spotify, other])
        self.assertEqual(result['times'], [12, None, 27, 36])
        self.assertEqual([line.reading for line in result['lines'][:2]], ['', ''])
        self.assertEqual([line.korean for line in result['lines'][:2]], [supplied[0].korean] * 2)

    def test_common_offset_and_conflicting_clocks(self):
        spotify = document('Spotify', times=[10, None, 32, 42])
        alsong = document('ALSong', times=[15, 25, 37, 47])
        result = merge_lyrics(PLAYBACK, [spotify, alsong])
        self.assertEqual(result['times'], [10, 20, 32, 42])
        self.assertEqual(result['rows'][1]['timings'][0]['raw_seconds'], 25)
        lrclib = document('LRCLIB', times=[10, 25, 32, 42])
        self.assertEqual(merge_lyrics(PLAYBACK, [spotify, alsong, lrclib])['times'], [10, None, 32, 42])
        lyricsplus = document('LyricsPlus', times=[10, 20.1, 32, 42])
        self.assertEqual(merge_lyrics(PLAYBACK, [spotify, alsong, lrclib, lyricsplus])['times'], [10, 20, 32, 42])

    def test_manual_times_and_three_line_txt_annotations_survive(self):
        text = '[01] Example song - Unit\n' + '\n\n'.join(f'{line}\nよみ\n사용자 번역' for line in ORIGINALS)
        lines = parse_document(text, 'Album.txt')[0].lines
        manual = document('manual', lines, [None, 25, None, None])
        spotify = document('Spotify', times=[10, 20, 30, 40])
        other = document('ALSong', [Line(line, 'other', '다른 번역') for line in ORIGINALS], [10, 21, 30, 40])
        result = merge_lyrics(PLAYBACK, [other, manual, spotify])
        self.assertEqual(result['times'], [10, 25, 30, 40])
        self.assertEqual(result['lines'], lines)
        self.assertEqual(result['rows'][1]['status'], 'manual')

    def test_reviewed_translation_overrides_original_owner_only_for_matching_rows(self):
        supplied = (Line(ORIGINALS[0], 'よみ', '수동 번역'), *ORIGINALS[1:])
        alsong = document('ALSong', tuple(Line(line, 'other', '기존 번역') for line in ORIGINALS), [10, 20, 30, 40])
        manual = document('manual', supplied, annotation_only=True)
        result = merge_lyrics(PLAYBACK, [alsong, manual])
        self.assertEqual(result['lines'][0], supplied[0])
        self.assertEqual(result['lines'][1], alsong['lines'][1])
        self.assertEqual(result['rows'][0]['translation_source'], 'manual')
        self.assertTrue(result['rows'][0]['manual'])
        self.assertEqual(result['times'], [10, 20, 30, 40])

    def test_conflicting_providers_do_not_erase_saved_translations(self):
        spotify = document('Spotify', times=[12, 19, 27, 36])
        originals = (ORIGINALS[0], DIALOGUE, *ORIGINALS[1:])
        alsong = document('ALSong', [Line(text, '', '다른 번역') for text in originals],
                          [12, 16, 19, 27, 36])
        saved = document('cache', [Line(text, '', '저장한 번역') for text in ORIGINALS])
        snapshots = [spotify, alsong, saved]
        result = merge_lyrics(PLAYBACK, snapshots)
        self.assertEqual(tuple(line.original for line in result['lines']), originals)
        self.assertEqual(result['times'], [12, 16, 19, 27, 36])
        self.assertEqual([line.korean for line in result['lines']],
                         ['저장한 번역', '다른 번역', '저장한 번역', '저장한 번역', '저장한 번역'])
        self.assertEqual(result['rows'][0]['translation_source'], 'cache')

        stale_lines = (Line(originals[0], '', ''), *result['lines'][1:])
        stale_rows = [{**result['rows'][0], 'translation_source': None}, *result['rows'][1:]]
        self.assertEqual(restore_cached_translations(stale_lines, stale_rows, snapshots),
                         (result['lines'], result['rows']))

    def test_proven_missing_untimed_dialogue_stays_unresolved(self):
        spotify = document('Spotify', times=[12, 19, 27, 36])
        actual = (ORIGINALS[0], DIALOGUE, *ORIGINALS[1:])
        alsong = document('ALSong', actual, [12, None, 19, 27, 36], title=PLAYBACK.title,
                          artist=PLAYBACK.artist, album=PLAYBACK.album, duration=PLAYBACK.duration)
        result = merge_lyrics(PLAYBACK, [spotify, alsong])
        self.assertEqual(result['times'], [12, None, 19, 27, 36])
        self.assertEqual(result['rows'][1]['status'], 'unresolved')
        audio = document('audio', result['lines'], [None, 16, None, None, None], recording_verified=True)
        refined = merge_lyrics(PLAYBACK, [audio, spotify, alsong])
        self.assertEqual(refined['times'], [12, 16, 19, 27, 36])
        self.assertEqual(refined['rows'][1]['status'], 'audio_aligned')

    def test_legacy_cache_can_be_enriched_and_restarted(self):
        spotify = document('Spotify', times=[12, None, 27, 36])
        alsong = document('ALSong', times=[12, 19, 27, 36])
        with tempfile.TemporaryDirectory() as path:
            cache = LyricCache(Path(path))
            direct = {key: spotify[key] for key in ('source', 'provider_id', 'lines')}
            direct['times'] = list(spotify['times'])
            self.assertTrue(cache.save(PLAYBACK, spotify['lines'], direct))
            legacy = cache.load(PLAYBACK)
            result = merge_lyrics(PLAYBACK, [snapshot_from_direct(legacy['direct']), alsong])
            self.assertTrue(cache.save(PLAYBACK, result['lines'], result['direct'], merge=result))
            fresh = LyricCache(Path(path)).load(PLAYBACK)
        self.assertEqual(fresh['direct']['times'], [12, 19, 27, 36])
        self.assertEqual(fresh['merge']['rows'], result['rows'])
        self.assertEqual(merge_lyrics(PLAYBACK, fresh['merge']['snapshots']), result)

    def test_raw_alsong_and_lyricsplus_adapters_retain_partial_recording(self):
        spotify = document('Spotify', times=[12, 19, 27, 36])
        actual = (ORIGINALS[0], DIALOGUE, *ORIGINALS[1:])
        stamps = [12, 16, 19, 27, 36]
        alsong = {'strInfoID': '321', 'strTitle': PLAYBACK.title, 'strArtistName': PLAYBACK.artist,
                  'strAlbumName': PLAYBACK.album, 'playtime': '90',
                  'strLyric': '\n'.join(f'[00:{stamp:02}]{row}' for stamp, row in zip(stamps, actual))}
        plus = {'type': 'Line', 'metadata': {'title': PLAYBACK.title, 'artist': PLAYBACK.artist,
                                           'album': PLAYBACK.album},
                'lyrics': [{'text': row, 'time': stamp * 1000} for row, stamp in zip(actual, stamps)]}
        for value in (fetch_alsong(PLAYBACK, ORIGINALS, fixture_fetch(alsong), merge_partial=True),
                      parse_payload(plus, PLAYBACK, ORIGINALS, merge_partial=True)):
            with self.subTest(provider=value['source']):
                result = merge_lyrics(PLAYBACK, [spotify, snapshot_from_direct(value)])
                self.assertEqual(tuple(row.original for row in result['lines']), actual)
                self.assertEqual(result['times'], stamps)

    def test_lrclib_keeps_ordered_partial_lrc_and_unsynced_originals(self):
        actual = (ORIGINALS[0], DIALOGUE, *ORIGINALS[1:])
        stamps = [12, 19, 27, 36]
        item = {'trackName': PLAYBACK.title, 'artistName': PLAYBACK.artist,
                'albumName': PLAYBACK.album, 'duration': PLAYBACK.duration,
                'plainLyrics': '\n'.join(actual), 'id': 321,
                'syncedLyrics': '\n'.join(f'[00:{stamp:02}]{row}' for stamp, row in zip(stamps, ORIGINALS))}
        candidate = fetch_public_lyrics(PLAYBACK, lambda _: [item], merge_metadata=True)
        self.assertEqual(tuple(row.original for row in candidate['lines']), actual)
        self.assertEqual(candidate['times'], [12, None, 19, 27, 36])

    def test_wiki_table_without_dialogue_translation_still_preserves_dialogue(self):
        actual = (ORIGINALS[0], f'蓮：「{DIALOGUE}」', *ORIGINALS[1:])
        table = '<br>'.join(line if index == 1 else f'{line}<br>일본어 독음<br>한국어 번역'
                            for index, line in enumerate(actual))
        html = f'<h1>{PLAYBACK.title}</h1><h2>가사</h2><table><tr><td>{table}</td></tr></table>'
        lines, _ = parse_html(html, PLAYBACK.title, reader=lambda value: '', reference_lines=ORIGINALS,
                              merge_partial=True)
        self.assertEqual(tuple(row.original for row in lines), actual)
        self.assertEqual(lines[1].korean, '')
