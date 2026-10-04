from dataclasses import replace
import io
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from lyrics import Line
from media import Playback
from public_lyrics import MAX_RESPONSE_BYTES, fetch_public_lyrics


class PublicLyricsTests(unittest.TestCase):
    def setUp(self):
        self.playback = Playback(title='Crossing! (如月千早 Ver.)', artist='如月千早',
                                 album='Example Album', duration=200)
        self.item = {'id': 12, 'trackName': self.playback.title,
                     'artistName': self.playback.artist, 'albumName': self.playback.album,
                     'duration': 200, 'instrumental': False,
                     'plainLyrics': '朝\n花\n夜\n花',
                     'syncedLyrics': '[00:05]朝\n[00:10]花\n[00:15]夜\n[00:20]花'}

    def get(self, *items, playback=None):
        return fetch_public_lyrics(playback or self.playback, fetch=lambda params: list(items))

    def test_fetches_originals_and_repeated_timestamps_without_a_song_or_screen(self):
        result = self.get(self.item)
        self.assertEqual(result, {'source': 'LRCLIB', 'provider_id': 12,
                                 'lines': tuple(Line(text, '', '') for text in ('朝', '花', '夜', '花')),
                                 'times': [5, 10, 15, 20]})

    def test_query_contains_title_and_artist_only(self):
        calls = []
        fetch_public_lyrics(self.playback, fetch=lambda params: calls.append(params) or [self.item])
        self.assertEqual(calls, [{'track_name': self.playback.title,
                                 'artist_name': self.playback.artist}])

    def test_title_only_fallback_recovers_timing_after_an_empty_artist_search(self):
        calls=[];reports=[]
        def fetch(params):
            calls.append(params)
            return [] if 'artist_name' in params else [self.item]
        result=fetch_public_lyrics(self.playback,fetch=fetch,report=reports.append)
        self.assertEqual(result['times'],[5,10,15,20])
        self.assertEqual(calls,[{'track_name':self.playback.title,'artist_name':self.playback.artist},
                                {'track_name':self.playback.title}])
        self.assertEqual(reports,['LRCLIB 싱크 연결됨'])

    def test_plain_artist_result_still_searches_title_for_timing(self):
        plain={**self.item,'id':13,'syncedLyrics':None}
        calls=[]
        def fetch(params):
            calls.append(params)
            return [plain] if 'artist_name' in params else [self.item]
        result=fetch_public_lyrics(self.playback,fetch=fetch)
        self.assertEqual(result['provider_id'],12)
        self.assertEqual(len(calls),2)

    def test_fallback_results_are_checked_against_the_original_plain_candidate(self):
        plain={**self.item,'syncedLyrics':None}
        different={**self.item,'id':13,'plainLyrics':'違う歌詞','syncedLyrics':'[00:05]違う歌詞'}
        reports=[]
        result=fetch_public_lyrics(self.playback,report=reports.append,
            fetch=lambda params:[plain] if 'artist_name' in params else [different])
        self.assertIsNone(result)
        self.assertEqual(reports,['LRCLIB 후보 가사 충돌'])

    def test_fallback_keeps_metadata_and_timing_conflict_guards(self):
        shifted={**self.item,'id':13,'syncedLyrics':'[00:07]朝\n[00:12]花\n[00:17]夜\n[00:22]花'}
        for candidates,status in (([shifted,self.item],'LRCLIB 후보 시각 충돌'),
                                  ([{**self.item,'artistName':'天海春香'}],'LRCLIB 후보 검증 불일치'),
                                  ([{**self.item,'trackName':'Crossing!'}],'LRCLIB 후보 검증 불일치'),
                                  ([{**self.item,'albumName':'Other Album'}],'LRCLIB 후보 검증 불일치'),
                                  ([{**self.item,'duration':240}],'LRCLIB 후보 검증 불일치')):
            reports=[]
            with self.subTest(status=status,candidates=candidates):
                result=fetch_public_lyrics(self.playback,report=reports.append,
                    fetch=lambda params:[] if 'artist_name' in params else candidates)
                self.assertIsNone(result)
                self.assertEqual(reports,[status])

    def test_fallback_network_failure_propagates_even_after_plain_lyrics(self):
        calls=[];reports=[]
        def fetch(params):
            calls.append(params)
            if 'artist_name' in params:return [{**self.item,'syncedLyrics':None}]
            raise TimeoutError('fallback timed out')
        with self.assertRaisesRegex(TimeoutError,'fallback timed out'):
            fetch_public_lyrics(self.playback,fetch=fetch,report=reports.append)
        self.assertEqual(len(calls),2)
        self.assertEqual(reports,[])

    def test_reports_distinguish_search_scope_and_bad_response_from_plain_lyrics(self):
        for response,status in (([],'LRCLIB 검색 결과 없음'),
                                ([{**self.item,'syncedLyrics':None}],'LRCLIB 원문만 연결됨'),
                                ({'error':'unavailable'},'LRCLIB 응답 형식 확인 실패')):
            reports=[];calls=[]
            with self.subTest(status=status):
                fetch_public_lyrics(self.playback,report=reports.append,
                                    fetch=lambda params:calls.append(params) or response)
                self.assertEqual(reports,[status])
                self.assertLessEqual(len(calls),2)

    def test_empty_fallback_does_not_hide_rejected_artist_query_candidates(self):
        reports=[]
        result=fetch_public_lyrics(self.playback,report=reports.append,
            fetch=lambda params:[{**self.item,'duration':240}] if 'artist_name' in params else [])
        self.assertIsNone(result)
        self.assertEqual(reports,['LRCLIB 후보 검증 불일치'])

    def test_full_artist_search_page_still_allows_the_bounded_title_retry(self):
        calls=[]
        crowded=[{**self.item,'id':index,'duration':240} for index in range(20)]
        def fetch(params):
            calls.append(params)
            return crowded if 'artist_name' in params else [self.item]
        self.assertEqual(fetch_public_lyrics(self.playback,fetch=fetch)['provider_id'],12)
        self.assertEqual(len(calls),2)

    def test_malformed_fallback_is_reported_instead_of_returning_earlier_plain_lyrics(self):
        reports=[]
        result=fetch_public_lyrics(self.playback,report=reports.append,
            fetch=lambda params:[{**self.item,'syncedLyrics':None}]
                                if 'artist_name' in params else {'error':'unavailable'})
        self.assertIsNone(result)
        self.assertEqual(reports,['LRCLIB 응답 형식 확인 실패'])

    def test_missing_query_metadata_has_its_own_status(self):
        reports=[]
        result=fetch_public_lyrics(replace(self.playback,duration=0),report=reports.append,
                                  fetch=lambda params:self.fail('unexpected request'))
        self.assertIsNone(result)
        self.assertEqual(reports,['LRCLIB 조회에 필요한 곡 정보 부족'])

    def test_cv_suffix_is_optional_but_two_explicit_conflicting_actors_are_rejected(self):
        decorated=replace(self.playback,artist='如月千早（ＣＶ：今井麻美）')
        self.assertIsNotNone(self.get(self.item,playback=decorated))
        self.assertIsNotNone(self.get({**self.item,'artistName':'如月千早 (CV. 今井麻美)'}))
        self.assertIsNotNone(self.get({**self.item,'artistName':'如月 千早 (cv: 今井 麻美)'},
                                     playback=decorated))
        self.assertIsNone(self.get({**self.item,'artistName':'如月千早 (CV: 別の声優)'},
                                  playback=decorated))

    def test_cv_comparison_never_removes_other_performers_or_unmarked_parentheses(self):
        decorated=replace(self.playback,artist='如月千早 (CV: 今井麻美)')
        for artist in ('天海春香 (CV: 今井麻美)','如月千早 feat. Other',
                       '如月千早 (今井麻美)','如月千早 (Live)','如月千早 (CV:)'):
            with self.subTest(artist=artist):
                self.assertIsNone(self.get({**self.item,'artistName':artist},playback=decorated))

    def test_fullwidth_spacing_and_punctuation_equivalence_preserves_version(self):
        self.assertIsNotNone(self.get({**self.item, 'trackName': 'ＣＲＯＳＳＩＮＧ！ - 如月千早 Ver.'}))

    def test_base_recording_and_other_singer_versions_never_supply_timing(self):
        for title in ('Crossing!', 'Crossing! (天海春香 Ver.)',
                      'Crossing! (如月千早 Ver.) - Live', 'Crossing! (如月千早 Ver.) Instrumental'):
            with self.subTest(title=title):
                self.assertIsNone(self.get({**self.item, 'trackName': title}))

    def test_wrong_or_missing_artist_rejected(self):
        for artist in ('Other Artist', '', None):
            with self.subTest(artist=artist):
                self.assertIsNone(self.get({**self.item, 'artistName': artist}))

    def test_duration_tolerance_and_invalid_values(self):
        for duration in (198, 202, '200'):
            self.assertIsNotNone(self.get({**self.item, 'duration': duration}))
        for duration in (197.99, 202.01, 0, None, 'bad', float('nan'), float('inf'), True):
            with self.subTest(duration=duration):
                self.assertIsNone(self.get({**self.item, 'duration': duration}))

    def test_missing_playback_metadata_does_not_make_a_request(self):
        for changes in ({'title': ''}, {'artist': ''}, {'duration': 0}, {'duration': float('nan')}):
            playback = replace(self.playback, **changes)
            with self.subTest(changes=changes):
                self.assertIsNone(fetch_public_lyrics(playback, fetch=lambda params: self.fail('unexpected request')))

    def test_album_match_resolves_different_recording_candidates(self):
        other = {**self.item, 'id': 13, 'albumName': 'Other Album',
                 'syncedLyrics': '[00:07]朝\n[00:12]花\n[00:17]夜\n[00:22]花'}
        self.assertEqual(self.get(other, self.item)['provider_id'], 12)

    def test_explicit_album_conflict_cannot_supply_even_the_only_timing_candidate(self):
        other = {**self.item, 'albumName': 'Other Album'}
        self.assertIsNone(self.get(other))
        self.assertIsNotNone(self.get(other, playback=replace(self.playback, album='')))

    def test_missing_provider_album_keeps_other_recording_checks_and_ambiguity_guards(self):
        for album in ('', None):
            item = {**self.item, 'albumName': album}
            with self.subTest(album=album):
                self.assertIsNotNone(self.get(item))
                self.assertIsNone(self.get({**item, 'artistName': 'Other Singer'}))
                self.assertIsNone(self.get({**item, 'duration': 240}))
                shifted = {**item, 'id': 13, 'syncedLyrics': '[00:07]朝\n[00:12]花\n[00:17]夜\n[00:22]花'}
                self.assertIsNone(self.get(item, shifted))

    def test_ambiguous_timing_rejected_without_matching_album(self):
        other = {**self.item, 'id': 13, 'syncedLyrics': '[00:07]朝\n[00:12]花\n[00:17]夜\n[00:22]花'}
        self.assertIsNone(self.get(self.item, other))
        self.assertIsNone(self.get(other, self.item, playback=replace(self.playback, album='Unknown')))

    def test_ambiguous_lyrics_rejected(self):
        other = {**self.item, 'id': 13, 'plainLyrics': '別の歌詞', 'syncedLyrics': '[00:05]別の歌詞'}
        self.assertIsNone(self.get(self.item, other))

    def test_identical_reuploads_are_not_ambiguous(self):
        self.assertEqual(self.get(self.item, {**self.item, 'id': 13})['times'], [5, 10, 15, 20])

    def test_same_plain_lyrics_do_not_hide_available_timing(self):
        plain = {**self.item, 'id': 13, 'syncedLyrics': None}
        self.assertEqual(self.get(plain, self.item)['provider_id'], 12)

    def test_plain_lyrics_are_available_with_no_invented_timestamps(self):
        result = self.get({**self.item, 'syncedLyrics': None})
        self.assertEqual(result['times'], [None] * 4)
        self.assertEqual([line.original for line in result['lines']], ['朝', '花', '夜', '花'])

    def test_invalid_timing_uses_plain_lyrics_without_synthetic_sync(self):
        for synced in ('[00:05]朝\n[00:05]花', '[03:21]朝', 'not LRC'):
            with self.subTest(synced=synced):
                self.assertEqual(self.get({**self.item, 'syncedLyrics': synced})['times'], [None] * 4)

    def test_incomplete_synced_verses_do_not_drop_plain_lyrics(self):
        item = {**self.item, 'syncedLyrics': '[00:05]朝\n[00:10]花\nmissing timestamp\n[00:20]花'}
        result = self.get(item)
        self.assertEqual(result['times'], [None] * 4)
        self.assertEqual([line.original for line in result['lines']], ['朝', '花', '夜', '花'])

    def test_synced_line_break_changes_keep_timing_if_words_are_complete(self):
        item = {**self.item, 'plainLyrics': '朝花\n夜花'}
        self.assertEqual(self.get(item)['times'], [5, 10, 15, 20])

    def test_blank_instrumental_and_invalid_results_rejected(self):
        self.assertIsNone(self.get({**self.item, 'instrumental': True}))
        self.assertIsNone(self.get({**self.item, 'plainLyrics': None, 'syncedLyrics': None}))
        self.assertIsNone(self.get(None, 'invalid', {}, {'duration': []}))
        self.assertIsNone(fetch_public_lyrics(self.playback, fetch=lambda params: {'error': 'unavailable'}))

    def test_empty_lrc_instrumental_gap_does_not_create_lyric_rows(self):
        result = self.get({**self.item, 'plainLyrics': '朝\n花',
                           'syncedLyrics': '[00:00]\n[00:05]朝\n[00:09]\n[00:10]花'})
        self.assertEqual(result['times'], [5, 10])
        self.assertEqual([line.original for line in result['lines']], ['朝', '花'])

    def test_network_read_is_bounded_and_uses_metadata_query(self):
        body = json.dumps([self.item]).encode('utf-8')
        with patch('public_lyrics.urlopen', return_value=io.BytesIO(body)) as request:
            result = fetch_public_lyrics(self.playback)
        self.assertEqual(result['times'], [5, 10, 15, 20])
        url = request.call_args.args[0].full_url
        self.assertEqual(parse_qs(urlparse(url).query),
                         {'track_name': [self.playback.title], 'artist_name': [self.playback.artist]})
        self.assertEqual(request.call_args.kwargs['timeout'], 8)
        with patch('public_lyrics.urlopen', return_value=io.BytesIO(b' ' * (MAX_RESPONSE_BYTES + 1))):
            with self.assertRaises(ValueError):
                fetch_public_lyrics(self.playback)


if __name__ == '__main__':
    unittest.main()
