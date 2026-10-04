from copy import deepcopy
from dataclasses import replace
import io
import json
import socket
import ssl
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

from lyrics import Line
from lyricsplus_provider import (BACKUP_ENDPOINT, ENDPOINT, MAX_LYRIC_LINES, MAX_RESPONSE_BYTES,
                                 _NoRedirect, fetch_lyricsplus, parse_payload,
                                 parse_ttml_duration)
from media import Playback


class LyricsPlusProviderTests(unittest.TestCase):
    def setUp(self):
        self.playback = Playback(title='Crossing! (如月千早 Ver.)', artist='如月千早',
                                 album='Example Album', duration=207)
        self.reference = ('朝の光', '花が咲く', 'また朝の光')
        self.payload = {
            'type': 'Line',
            'metadata': {'title': self.playback.title, 'artist': self.playback.artist,
                         'album': self.playback.album, 'totalDuration': '00:03:27.000'},
            'lyrics': [{'time': 5000, 'duration': 4000, 'text': self.reference[0]},
                       {'time': 10000, 'duration': 4000, 'text': self.reference[1]},
                       {'time': 20000, 'duration': 4000, 'text': self.reference[2]}],
        }

    def parse(self, payload=None, playback=None, reference=None):
        return parse_payload(self.payload if payload is None else payload,
                             playback or self.playback,
                             self.reference if reference is None else reference)

    def test_returns_only_original_lines_and_provided_seconds(self):
        result = self.parse()
        self.assertEqual(result, {'source': 'LyricsPlus', 'provider_id': None,
                                 'lines': tuple(Line(s, '', '') for s in self.reference),
                                 'times': [5, 10, 20]})

    def test_reports_server_miss_separately_from_rejected_timed_candidates(self):
        cases=[(Mock(side_effect=HTTPError(ENDPOINT,404,'missing',{},None)),'서버 검색 결과 없음'),
               (Mock(return_value={**self.payload,'type':'None'}),'줄별 시각 없음'),
               (Mock(return_value={**self.payload,'metadata':{'title':'Other'}}),'곡 정보 불일치'),
               (Mock(return_value={**self.payload,'lyrics':[{'text':'wrong','time':1000}]}),'원문·시각 검증 불일치'),
               (Mock(return_value={}), '응답 형식 확인 실패')]
        for fetch,expected in cases:
            with self.subTest(expected=expected):
                statuses=[]
                self.assertIsNone(fetch_lyricsplus(self.playback,self.reference,fetch,report=statuses.append))
                self.assertEqual(len(statuses),1)
                self.assertIn(expected,statuses[0])

    def test_server_error_payload_propagates_instead_of_becoming_a_cached_miss(self):
        statuses=[]
        with self.assertRaises(ValueError):
            fetch_lyricsplus(self.playback,self.reference,lambda _: {'error':'upstream failed'},
                            report=statuses.append)
        self.assertEqual(statuses,[])

    def test_checked_result_reports_connection(self):
        statuses=[]
        result=fetch_lyricsplus(self.playback,self.reference,lambda _:self.payload,report=statuses.append)
        self.assertEqual(result['times'],[5,10,20])
        self.assertEqual(statuses,['LyricsPlus 싱크 연결됨'])

    def test_missing_or_invalid_reference_never_contacts_server(self):
        fetch = Mock(side_effect=AssertionError('unexpected request'))
        for reference in ((), None, 'not a row sequence', (None,), ('♪',),
                          ('row',) * (MAX_LYRIC_LINES + 1)):
            with self.subTest(reference_type=type(reference).__name__):
                self.assertIsNone(fetch_lyricsplus(self.playback, reference, fetch))
        fetch.assert_not_called()

    def test_missing_playback_identity_does_not_contact_server(self):
        fetch = Mock(side_effect=AssertionError('unexpected request'))
        for change in ({'title': ''}, {'artist': ''}, {'duration': 0},
                       {'duration': True}, {'duration': float('nan')}):
            playback = replace(self.playback, **change)
            self.assertIsNone(fetch_lyricsplus(playback, self.reference, fetch))
            self.assertIsNone(self.parse(playback=playback))
        fetch.assert_not_called()

    def test_reference_and_translation_are_never_sent(self):
        fetch = Mock(return_value=self.payload)
        reference = (Line(s, 'private reading', 'private translation') for s in self.reference)
        result = fetch_lyricsplus(self.playback, reference, fetch)
        self.assertEqual(result['times'], [5, 10, 20])
        fetch.assert_called_once_with({'title': self.playback.title,
                                       'artist': self.playback.artist,
                                       'album': self.playback.album, 'duration': 207.0})

    def test_complete_original_comparison_allows_line_break_and_punctuation_changes(self):
        self.assertIsNotNone(self.parse(reference=('朝の光、花が咲く', 'また 朝の光！')))
        for reference in (self.reference[:2], self.reference + ('extra verse',), ('別の曲',)):
            self.assertIsNone(self.parse(reference=reference))

    def test_full_song_with_two_minor_spelling_differences_keeps_supplied_onsets(self):
        reference=tuple(f'朝の光を見ながら遠い空の向こうへ歩いて行こう{i}' for i in range(20))
        texts=list(reference)
        texts[3]=texts[3].replace('光','灯')
        texts[17]=texts[17].replace('空','雲')
        payload={**self.payload,'lyrics':[{'time':1000+i*3000,'text':text}
                                         for i,text in enumerate(texts)]}
        result=self.parse(payload,reference=iter(reference))
        self.assertEqual(result['times'],[1+i*3 for i in range(20)])
        self.assertEqual([line.original for line in result['lines']],texts)
        # Approximate wording must never override conflicting recording identity.
        payload['metadata']={**payload['metadata'],'title':'Crossing! (Other Ver.)'}
        self.assertIsNone(self.parse(payload,reference=reference))

    def test_minor_typo_tolerance_does_not_accept_a_missing_verse(self):
        reference=tuple(f'朝の光を見ながら遠い空の向こうへ歩いて行こう{i}' for i in range(20))
        payload={**self.payload,'lyrics':[{'time':1000+i*3000,'text':text}
                                         for i,text in enumerate(reference) if i!=8]}
        self.assertIsNone(self.parse(payload,reference=reference))

    def test_simultaneous_rows_cannot_hide_an_omitted_repeated_row(self):
        reference=('z'+'a'*200,'a','a'*200+'z','uniqueendingline')
        payload={**self.payload,'lyrics':[{'time':1000,'text':reference[0]},
                                          {'time':1000,'text':reference[2]},
                                          {'time':2000,'text':reference[3]}]}
        self.assertIsNone(self.parse(payload,reference=reference))

    def test_actual_title_artist_and_album_mismatches_override_matching_reference(self):
        for name, value in (('title', 'Crossing!'),
                            ('title', 'Crossing! (天海春香 Ver.)'),
                            ('title', self.playback.title + ' - Live'),
                            ('artist', 'Other Artist'), ('album', 'Other Album')):
            payload = deepcopy(self.payload)
            payload['metadata'][name] = value
            with self.subTest(name=name, value=value):
                self.assertIsNone(self.parse(payload))

    def test_full_normalization_retains_japanese_vs_english_title(self):
        payload = deepcopy(self.payload)
        playback = replace(self.playback, title='I NEED YOU')
        payload['metadata']['title'] = 'アイ NEED YOU'
        self.assertIsNone(self.parse(payload, playback))
        payload['metadata']['title'] = 'Ｉ ＮＥＥＤ ＹＯＵ！'
        self.assertIsNotNone(self.parse(payload, playback))

    def test_missing_metadata_requires_full_reference(self):
        payload = {**self.payload, 'metadata': {}}
        self.assertIsNotNone(self.parse(payload))
        self.assertIsNone(self.parse(payload, reference=()))
        payload['processingTime'] = {'selectedSongMetadata': {
            'title': self.playback.title, 'artist': self.playback.artist,
            'album': self.playback.album, 'duration': self.playback.duration}}
        self.assertIsNone(self.parse(payload, reference=('Different text',)))
        payload['metadata']['title'] = 'Different version'
        self.assertIsNone(self.parse(payload))

    def test_missing_album_is_distinct_from_a_conflicting_recording_album(self):
        for album in ('', None):
            payload = deepcopy(self.payload)
            payload['metadata']['album'] = album
            with self.subTest(album=album):self.assertIsNotNone(self.parse(payload))
        payload = deepcopy(self.payload)
        payload['metadata']['album'] = 'Different Recording'
        self.assertIsNone(self.parse(payload))
        self.assertIsNotNone(self.parse(payload, replace(self.playback, album='')))

    def test_ttml_duration_explicit_units_and_clock_only(self):
        for text in ('00:03:27.000', '03:27', '207s', '207000ms', '3.45m'):
            self.assertAlmostEqual(parse_ttml_duration(text), 207)
        self.assertEqual(parse_ttml_duration('1h'), 3600)
        for value in (207, True, '207', 'nan', '03:60', '00:60:00', '-1s',
                      '1f', '1t', [], None, '00:' + '9' * 5000 + ':00'):
            self.assertIsNone(parse_ttml_duration(value))

    def test_shorter_ttml_document_is_not_recording_length_evidence(self):
        payload = deepcopy(self.payload)
        payload['metadata']['totalDuration'] = '24s'
        self.assertIsNotNone(self.parse(payload))
        self.assertIsNone(self.parse(payload, reference=()))
        payload['metadata']['totalDuration'] = '209.001s'
        self.assertIsNone(self.parse(payload))
        payload['metadata']['totalDuration'] = '207'
        self.assertIsNone(self.parse(payload))

    def test_simultaneous_distinct_rows_share_one_onset_without_interpolation(self):
        payload = deepcopy(self.payload)
        payload['lyrics'][1]['time'] = 5000
        result = self.parse(payload)
        self.assertEqual(result['times'], [5, 20])
        self.assertEqual([line.original for line in result['lines']],
                         ['朝の光 花が咲く', 'また朝の光'])

    def test_exact_duplicate_at_same_onset_collapses_but_later_repetition_stays(self):
        payload = deepcopy(self.payload)
        payload['lyrics'].insert(1, dict(payload['lyrics'][0]))
        self.assertEqual(self.parse(payload)['times'], [5, 10, 20])
        payload['lyrics'].append({'time': 30000, 'text': '朝の光'})
        self.assertIsNone(self.parse(payload))
        result = self.parse(payload, reference=self.reference + ('朝の光',))
        self.assertEqual(result['times'], [5, 10, 20, 30])

    def test_simultaneous_group_cannot_exceed_line_text_limit(self):
        originals = ('a' * 3000, 'b' * 3000)
        payload = {**self.payload, 'lyrics': [{'time': 5000, 'text': text}
                                            for text in originals]}
        self.assertIsNone(self.parse(payload, reference=originals))

    def test_no_timestamps_are_created_from_syllables_or_equal_distribution(self):
        payload = deepcopy(self.payload)
        payload['type'] = 'Word'
        del payload['lyrics'][0]['time']
        payload['lyrics'][0]['syllabus'] = [{'time': 5000, 'duration': 4000,
                                            'text': self.reference[0]}]
        self.assertIsNone(self.parse(payload))

    def test_line_text_precedes_syllables_and_joining_is_only_a_text_fallback(self):
        payload = deepcopy(self.payload)
        payload['lyrics'][0]['syllabus'] = [{'text': 'Different words', 'synthetic': True}]
        self.assertEqual(self.parse(payload)['lines'][0].original, self.reference[0])
        payload['lyrics'][0]['text'] = ''
        self.assertIsNone(self.parse(payload))
        payload['lyrics'][0]['syllabus'] = [{'text': '朝の'}, {'text': '光'}]
        self.assertEqual(self.parse(payload)['times'], [5, 10, 20])

    def test_invalid_or_decreasing_time_rejects_entire_payload(self):
        for timestamp in (-1, True, '5000', None, float('nan'), float('inf'), 209001):
            payload = deepcopy(self.payload)
            payload['lyrics'][0]['time'] = timestamp
            with self.subTest(timestamp=timestamp):
                self.assertIsNone(self.parse(payload))
        payload = deepcopy(self.payload)
        payload['lyrics'][1]['time'] = 4999
        self.assertIsNone(self.parse(payload))

    def test_timestamp_and_end_bounds_include_two_second_tolerance(self):
        payload = deepcopy(self.payload)
        payload['lyrics'][-1].update(time=209000, duration=0)
        self.assertEqual(self.parse(payload)['times'][-1], 209)
        payload['lyrics'][-1]['duration'] = 1
        self.assertIsNone(self.parse(payload))
        for span in (-1, True, None, float('nan'), '1000'):
            payload['lyrics'][-1]['duration'] = span
            self.assertIsNone(self.parse(payload))

    def test_blank_and_instrumental_rows_add_no_lyrics_or_times(self):
        payload = deepcopy(self.payload)
        payload['lyrics'].insert(0, {'time': 0, 'duration': 1000, 'text': '♪'})
        payload['lyrics'].insert(1, {'time': 2000, 'text': ' '})
        self.assertEqual(self.parse(payload)['times'], [5, 10, 20])

    def test_malformed_unbounded_and_unsynced_payloads_rejected(self):
        malformed = [None, [], {}, {'error': 'Unavailable'},
                     {**self.payload, 'type': 'None'},
                     {**self.payload, 'metadata': []},
                     {**self.payload, 'lyrics': [None]},
                     {**self.payload, 'lyrics': []},
                     {**self.payload, 'lyrics': self.payload['lyrics'] * 1000}]
        for payload in malformed:
            self.assertIsNone(parse_payload(payload, self.playback, self.reference))
        for text in (12, {}, 'x' * 4001):
            payload = deepcopy(self.payload)
            payload['lyrics'][0]['text'] = text
            self.assertIsNone(self.parse(payload))

    def test_transport_one_origin_metadata_only_and_timeout(self):
        opener = Mock()
        opener.open.return_value = io.BytesIO(json.dumps(self.payload).encode())
        with patch('lyricsplus_provider.build_opener', return_value=opener) as builder:
            result = fetch_lyricsplus(self.playback, self.reference)
        self.assertEqual(result['times'], [5, 10, 20])
        opener.open.assert_called_once()
        request = opener.open.call_args.args[0]
        parsed = urlparse(request.full_url)
        self.assertEqual(parsed.scheme + '://' + parsed.netloc + parsed.path, ENDPOINT)
        self.assertEqual(parse_qs(parsed.query), {'title': [self.playback.title],
                         'artist': [self.playback.artist], 'album': [self.playback.album],
                         'duration': ['207.0']})
        self.assertEqual(request.get_method(), 'GET')
        self.assertIsNone(request.data)
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 8)
        self.assertIsInstance(builder.call_args.args[0], _NoRedirect)
        self.assertIsNone(builder.call_args.args[0].redirect_request(
            request, None, 302, 'Found', {}, 'https://another.example/'))

    def test_transport_oversize_malformed_json_and_duplicate_keys_raise(self):
        for body in (b' ' * (MAX_RESPONSE_BYTES + 1), b'not json',
                     b'{"type":"Line","type":"None"}', b'{"time":NaN}'):
            opener = Mock()
            opener.open.return_value = io.BytesIO(body)
            with patch('lyricsplus_provider.build_opener', return_value=opener):
                with self.assertRaises(ValueError):
                    fetch_lyricsplus(self.playback, self.reference)
            opener.open.assert_called_once()

    def test_backup_only_after_primary_5xx_timeout_or_dns_error(self):
        failures = [HTTPError(ENDPOINT, status, 'test', {}, None)
                    for status in (500, 503, 530)]
        failures.extend((TimeoutError('timed out'), socket.gaierror(-2, 'DNS failure'),
                         URLError(TimeoutError('timed out')),
                         URLError(socket.gaierror(-2, 'DNS failure'))))
        for error in failures:
            with self.subTest(error=type(error).__name__, status=getattr(error, 'code', None)):
                opener = Mock()
                opener.open.side_effect = [error, io.BytesIO(json.dumps(self.payload).encode())]
                with patch('lyricsplus_provider.build_opener', return_value=opener):
                    result = fetch_lyricsplus(self.playback, self.reference)
                self.assertEqual(result['times'], [5, 10, 20])
                self.assertEqual(opener.open.call_count, 2)
                first, second = [urlparse(call.args[0].full_url)
                                 for call in opener.open.call_args_list]
                self.assertEqual(first.scheme + '://' + first.netloc + first.path, ENDPOINT)
                self.assertEqual(second.scheme + '://' + second.netloc + second.path,
                                 BACKUP_ENDPOINT)
                self.assertEqual(first.query, second.query)
                self.assertEqual(set(parse_qs(second.query)), {'title', 'artist', 'album', 'duration'})

    def test_primary_miss_access_rate_limit_redirect_and_other_network_errors_do_not_fallback(self):
        failures = [HTTPError(ENDPOINT, status, 'test', {}, None)
                    for status in (404, 401, 403, 429, 302)]
        failures.extend((URLError(ssl.SSLCertVerificationError('certificate failure')),
                         URLError(ConnectionRefusedError('connection refused')),
                         URLError('unclassified network failure')))
        for error in failures:
            opener = Mock()
            opener.open.side_effect = error
            with patch('lyricsplus_provider.build_opener', return_value=opener):
                if isinstance(error, HTTPError) and error.code == 404:
                    self.assertIsNone(fetch_lyricsplus(self.playback, self.reference))
                else:
                    with self.assertRaises(type(error)) as caught:
                        fetch_lyricsplus(self.playback, self.reference)
                    self.assertIs(caught.exception, error)
            opener.open.assert_called_once()

    def test_backup_failure_propagates_last_cause_and_never_retries(self):
        for final_error in (HTTPError(BACKUP_ENDPOINT, 530, 'test', {}, None),
                            URLError(socket.gaierror(-2, 'backup DNS failure')),
                            TimeoutError('backup timed out')):
            opener = Mock()
            opener.open.side_effect = [TimeoutError('primary timed out'), final_error]
            with patch('lyricsplus_provider.build_opener', return_value=opener):
                with self.assertRaises(type(final_error)) as caught:
                    fetch_lyricsplus(self.playback, self.reference)
            self.assertIs(caught.exception, final_error)
            self.assertEqual(opener.open.call_count, 2)

    def test_backup_404_is_a_miss_without_further_requests(self):
        opener = Mock()
        opener.open.side_effect = [HTTPError(ENDPOINT, 530, 'test', {}, None),
                                  HTTPError(BACKUP_ENDPOINT, 404, 'test', {}, None)]
        with patch('lyricsplus_provider.build_opener', return_value=opener):
            self.assertIsNone(fetch_lyricsplus(self.playback, self.reference))
        self.assertEqual(opener.open.call_count, 2)

    def test_well_formed_but_rejected_primary_response_does_not_fallback(self):
        for payload in ({}, {**self.payload, 'type': 'None'},
                        {**self.payload, 'metadata': {'title': 'Different recording'}}):
            opener = Mock()
            opener.open.return_value = io.BytesIO(json.dumps(payload).encode())
            with patch('lyricsplus_provider.build_opener', return_value=opener):
                self.assertIsNone(fetch_lyricsplus(self.playback, self.reference))
            opener.open.assert_called_once()

    def test_404_is_a_miss_other_http_and_transport_errors_propagate_without_retry(self):
        for status in (404, 429, 500, 503, 530, 302):
            error = HTTPError(ENDPOINT, status, 'test', {}, None)
            fetch = Mock(side_effect=error)
            if status == 404:
                self.assertIsNone(fetch_lyricsplus(self.playback, self.reference, fetch))
            else:
                with self.assertRaises(HTTPError):
                    fetch_lyricsplus(self.playback, self.reference, fetch)
            fetch.assert_called_once()
        fetch = Mock(side_effect=TimeoutError())
        with self.assertRaises(TimeoutError):
            fetch_lyricsplus(self.playback, self.reference, fetch)
        fetch.assert_called_once()


if __name__ == '__main__':
    unittest.main()
