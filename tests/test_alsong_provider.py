from dataclasses import replace
import io
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

from alsong_provider import (DETAIL_ACTION, ENDPOINT, MAX_RESPONSE_BYTES, REQUEST_TIMEOUT,
                             SEARCH_ACTION, _NoRedirect, _artist_matches, fetch_alsong,
                             load_alsong_registration, search_alsong)
from lyrics import Line
from media import Playback


def modern(record):
    names = {'strInfoID': 'lyricID', 'strTitle': 'title', 'strArtistName': 'artist',
             'strAlbumName': 'album', 'strLyric': 'lyric'}
    return {names.get(key, key): value for key, value in record.items()}


def envelope(action):
    root = ET.Element('{http://schemas.xmlsoap.org/soap/envelope/}Envelope')
    body = ET.SubElement(root, '{http://schemas.xmlsoap.org/soap/envelope/}Body')
    response = ET.SubElement(body, '{ALSongWebServer}' + action + 'Response')
    return root, response


def soap(*records):
    root, response = envelope(SEARCH_ACTION)
    result = ET.SubElement(response, '{ALSongWebServer}' + SEARCH_ACTION + 'Result')
    for record in records:
        entry = ET.SubElement(result, '{ALSongWebServer}ST_SEARCHLYRIC_LIST')
        for field, value in modern(record).items():
            if field in ('lyricID', 'title', 'artist', 'album'):
                ET.SubElement(entry, '{ALSongWebServer}' + field).text = value
    return ET.tostring(root, encoding='utf-8')


def detail(record):
    root, response = envelope(DETAIL_ACTION)
    if record is not None:
        output = ET.SubElement(response, '{ALSongWebServer}output')
        for field, value in modern(record).items():
            ET.SubElement(output, '{ALSongWebServer}' + field).text = value
    return ET.tostring(root, encoding='utf-8')


def fixture_fetch(*records):
    def fetch(action, params):
        if action == SEARCH_ACTION:
            return soap(*records)
        if action == DETAIL_ACTION:
            record = next(record for record in records if modern(record)['lyricID'] == params['lyricID'])
            return detail(record)
        raise AssertionError('unexpected action')
    return fetch


class AlsongProviderTests(unittest.TestCase):
    def test_english_phrase_in_japanese_three_row_document_can_have_translation(self):
        from alsong_provider import _parallel_annotations
        rows=['[00:10.00]Open the window','[00:10.00]오픈 더 윈도',
              '[00:10.00]창문을 열어','[00:12.00]朝だよ','[00:12.00]아사다요','[00:12.00]아침이야']
        values=_parallel_annotations(rows)
        self.assertEqual(len(values),2)
        self.assertIn(('오픈 더 윈도','창문을 열어'),values.values())
        # Empty provider translation placeholders are not invented locally.
        self.assertEqual(_parallel_annotations(rows[:1]+['[00:10.00]','[00:10.00]']+rows[3:]),{})

    def setUp(self):
        self.playback = Playback(title='Synthetic (Singer Ver.)', artist='Example Singer',
                                 album='Example Album', duration=207)
        self.reference = ('朝を探して', '窓に映る花', '同じ道を歩く', '朝を探して')
        self.item = {'strInfoID': '123',
                     'strTitle': self.playback.title, 'strArtistName': self.playback.artist,
                     'strAlbumName': self.playback.album,
                     'strLyric': '[00:05.10]朝を探して<br>[00:10.25]窓に映る花<br>'
                                 '[00:20.00]同じ道を歩く<br>[00:30.75]朝を探して<br>'}

    def get(self, *records, playback=None, reference=None, report=None):
        return fetch_alsong(playback or self.playback,
                            reference=self.reference if reference is None else reference,
                            fetch=fixture_fetch(*records),report=report)

    def test_manual_results_use_edited_query_and_load_only_selected_id(self):
        preferred = {**self.item, 'strInfoID': '456', 'strAlbumName': 'Edited Album'}
        fetch = Mock(side_effect=fixture_fetch(self.item, preferred, preferred))
        records = search_alsong(' Edited title ', ' Edited artist ', 'Edited Album', fetch=fetch)
        self.assertEqual([row['lyricID'] for row in records], ['456', '123'])
        fetch.assert_called_once_with(SEARCH_ACTION, {'title':'Edited title', 'artist':'Edited artist', 'pageNo':'1'})
        result = load_alsong_registration('456', 207, fetch=fetch)
        self.assertEqual(result['lines'][0].original, '朝を探して')
        self.assertEqual(result['times'], [5.1, 10.25, 20, 30.75])
        fetch.assert_called_with(DETAIL_ACTION, {'lyricID': '456'})

    def test_manual_registration_keeps_timing_when_onsets_exceed_track_length(self):
        result = load_alsong_registration('123', 20, fetch=fixture_fetch(self.item))
        self.assertEqual(len(result['lines']), 4)
        self.assertTrue(result['clock_mismatch'])
        self.assertEqual(result['times'], [5.1, 10.25, 20, 30.75])
        self.assertEqual(result['last_onset'], 30.75)
        self.assertIsNone(result['duration'])

    def test_manual_registration_keeps_timing_for_shorter_and_longer_versions(self):
        for length in (150, 207, 250):
            with self.subTest(length=length):
                registration = {**self.item, 'playtime':str(length)}
                result = load_alsong_registration('123', 207, fetch=fixture_fetch(registration))
                self.assertEqual(result['duration'], length)
                self.assertEqual(result['clock_mismatch'], length != 207)
                self.assertEqual(result['times'], [5.1, 10.25, 20, 30.75])

    def test_manual_registration_retains_lyrics_with_backwards_timing(self):
        registration = {**self.item, 'strLyric': '[00:20]First<br>[00:10]Second'}
        result = load_alsong_registration('123', 207, fetch=fixture_fetch(registration))
        self.assertEqual([line.original for line in result['lines']], ['First', 'Second'])
        self.assertEqual(result['times'], [None, None])

    def long_document(self):
        reference = tuple(f'The {ordinal} synthetic verse describes a different scene '
                          f'with {scene} and a quiet path toward the horizon'
                          for ordinal, scene in (('first', 'bright clouds'), ('second', 'open windows'),
                                                 ('third', 'falling leaves'), ('fourth', 'distant lights')))
        lyric = '\n'.join(f'[{stamp}]{text}' for stamp, text in zip(
            ('00:05.10', '00:10.25', '00:20', '00:30.75'), reference))
        return reference, {**self.item, 'strLyric': lyric}

    def three_slot_document(self, credits=True, copies=3):
        playback = replace(self.playback, title='An Open Window',
                           album='THE IDOLM@STER TEST ALBUM 09')
        reference = ('朝を探して', 'Open the window', '窓に映る花', 'Open the window')
        rows = (['[00:00.91]TEST ALBUM 09', '[00:00.91]' + playback.title,
                 '[00:00.91]가수 표시'] if credits else [])
        for stamp, original in zip(('00:05', '00:10', '00:20', '00:30'), reference):
            texts = [original] * copies if original == 'Open the window' else [original, '독음', '번역']
            rows.extend(f'[{stamp}]' + text for text in texts)
        return playback, reference, {**self.item, 'strTitle': playback.title,
            'strAlbumName': playback.album, 'strLyric': '\n'.join(rows)}

    def test_proven_three_slot_display_removes_leading_credits_and_duplicate_english_slots(self):
        playback, reference, item = self.three_slot_document()
        result = self.get(item, playback=playback, reference=reference)
        self.assertEqual(tuple(line.original for line in result['lines']), reference)
        self.assertEqual(result['times'], [5, 10, 20, 30])
        self.assertEqual([line.korean for line in result['lines']], ['번역', '', '번역', ''])

    def test_english_slot_cleanup_requires_two_distinct_japanese_three_row_groups(self):
        playback, reference, item = self.three_slot_document()
        item = {**item, 'strLyric': item['strLyric'].replace('窓に映る花', '朝を探して')}
        reference = (reference[0], reference[1], reference[0], reference[3])
        self.assertIsNone(self.get(item, playback=playback, reference=reference))
        for copies in (2, 4):
            with self.subTest(copies=copies):
                playback, reference, item = self.three_slot_document(copies=copies)
                self.assertIsNone(self.get(item, playback=playback, reference=reference))

    def test_slot_cleanup_cannot_hide_a_missing_verse_or_part_of_a_repeated_original(self):
        playback, reference, item = self.three_slot_document(credits=False)
        self.assertIsNone(self.get(item, playback=playback, reference=reference[:2] + reference[3:]))
        repeated = (reference[0], reference[1], reference[1], *reference[2:])
        self.assertIsNone(self.get(item, playback=playback, reference=repeated))
        complete = (reference[0], reference[1], reference[1], reference[1],
                    reference[2], reference[3], reference[3], reference[3])
        result = self.get(item, playback=playback, reference=complete)
        self.assertEqual(result['lines'][1].original, ' '.join([reference[1]] * 3))
        self.assertEqual(result['times'], [5, 10, 20, 30])

    def test_title_named_first_lyric_stays_when_no_album_header_is_present(self):
        playback, reference, item = self.three_slot_document(credits=False)
        item = {**item, 'strLyric': '[00:01]' + playback.title + '\n' + item['strLyric']}
        reference = (playback.title, *reference)
        result = self.get(item, playback=playback, reference=reference)
        self.assertEqual(tuple(line.original for line in result['lines']), reference)
        self.assertEqual(result['times'], [1, 5, 10, 20, 30])

    def test_album_and_title_rows_inside_song_are_not_removed_as_a_header(self):
        playback, reference, item = self.three_slot_document(credits=False)
        rows = item['strLyric'].splitlines()
        rows[3:3] = ['[00:06]TEST ALBUM 09', '[00:06]' + playback.title]
        item = {**item, 'strLyric': '\n'.join(rows)}
        self.assertIsNone(self.get(item, playback=playback, reference=reference))

    def test_cleanup_does_not_hide_unknown_header_text_or_invalid_clock(self):
        playback, reference, item = self.three_slot_document()
        for lyric in (item['strLyric'].replace('TEST ALBUM 09', 'Unrelated opening words'),
                      item['strLyric'] + '\n[00:00.91]TEST ALBUM 09\n[00:00.91]' + playback.title,
                      item['strLyric'].replace('[00:30]', '[00:10]'),
                      item['strLyric'].replace('[00:30]', '[04:00]')):
            with self.subTest(lyric_length=len(lyric)):
                self.assertIsNone(self.get({**item, 'strLyric': lyric}, playback=playback, reference=reference))

    def test_verified_originals_keep_explicit_fractional_seconds(self):
        result = self.get(self.item)
        self.assertEqual(result, {'source': 'ALSong', 'provider_id': '123',
                                 'lines': tuple(Line(text, '', '') for text in self.reference),
                                 'times': [5.1, 10.25, 20, 30.75]})

    def test_line_objects_use_only_originals_and_preserve_repeated_phrases(self):
        reference = [Line(text, '독음', '번역') for text in self.reference]
        calls = []
        result = fetch_alsong(self.playback, reference,
                              fetch=lambda action, params: calls.append((action, params)) or fixture_fetch(self.item)(action, params))
        self.assertEqual(result['times'], [5.1, 10.25, 20, 30.75])
        self.assertEqual(calls, [(SEARCH_ACTION, {'title': self.playback.title, 'artist': self.playback.artist, 'pageNo': '1'}),
                                 (DETAIL_ACTION, {'lyricID': '123'})])

    def test_reference_generator_is_consumed_once_and_preserves_row_boundaries(self):
        result = self.get(self.item, reference=(Line(text, '', '') for text in self.reference))
        self.assertEqual(result['times'], [5.1, 10.25, 20, 30.75])

    def test_empty_reference_rows_do_not_change_original_matching(self):
        reference = ('', *self.reference[:2], '   ', *self.reference[2:], '…')
        self.assertEqual(self.get(self.item, reference=reference)['times'], [5.1, 10.25, 20, 30.75])

    def test_two_character_differences_in_complete_lyrics_keep_provider_timestamps(self):
        reference, item = self.long_document()
        for lyric in (item['strLyric'].replace('first', 'firsx').replace('fourth', 'fourxh'),
                      item['strLyric'].replace('first', 'firt').replace('second', 'seccond')):
            with self.subTest(lyric_length=len(lyric)):
                result = self.get({**item, 'strLyric': lyric}, reference=reference)
                self.assertEqual(result['times'], [5.1, 10.25, 20, 30.75])
                self.assertNotEqual(tuple(line.original for line in result['lines']), reference)
                self.assertIn('first', reference[0])

    def test_missing_whole_short_row_is_rejected_even_within_character_budget(self):
        reference, item = self.long_document()
        reference = (*reference[:2], 'X', *reference[2:])
        self.assertIsNone(self.get(item, reference=reference))

    def test_typo_tolerance_does_not_accept_malformed_cues_in_a_long_document(self):
        reference, item = self.long_document()
        for stamp in ('[00]', '[00:99]', '[-00:10]', '[00:10][01]', '[offset:1]'):
            with self.subTest(stamp=stamp):
                lyric = item['strLyric'].replace('[00:10.25]', stamp)
                self.assertIsNone(self.get({**item, 'strLyric': lyric}, reference=reference))

    def test_no_trusted_full_originals_means_no_request(self):
        for reference in ((), None, 'not an iterable of lines', [None], [''], [Line('', '독음', '번역')],
                          ['x' * 4001], ['x'] * 1501):
            with self.subTest(reference_type=type(reference).__name__):
                self.assertIsNone(fetch_alsong(self.playback, reference,
                                               fetch=lambda action, params: self.fail('unexpected request')))

    def test_invalid_playback_metadata_means_no_request(self):
        for changes in ({'title': ''}, {'artist': ''}, {'duration': 0}, {'duration': True},
                        {'duration': float('inf')}, {'duration': float('nan')}):
            self.assertIsNone(fetch_alsong(replace(self.playback, **changes), self.reference,
                                           fetch=lambda action, params: self.fail('unexpected request')))

    def test_artist_and_entire_recording_title_are_required(self):
        for changes in ({'strTitle': 'Synthetic'}, {'strTitle': 'Synthetic (Other Singer Ver.)'},
                        {'strTitle': 'Synthetic (Singer Ver.) Live'}, {'strTitle': ''},
                        {'strArtistName': 'Other Singer'}, {'strArtistName': ''}):
            with self.subTest(changes=changes):
                self.assertIsNone(self.get({**self.item, **changes}))
        self.assertIsNotNone(self.get({**self.item, 'strTitle': 'ＳＹＮＴＨＥＴＩＣ - Singer Ver.'}))

    def test_separate_korean_reading_and_translation_rows_never_supply_timing(self):
        item = {**self.item, 'strLyric': '[00:04]아침을 찾으며<br>[00:05.10]朝を探して<BR />'
                '[00:06]아사오 사가시테&lt;br&gt;[00:10.25]<font color="red">窓に映る花</font><br>'
                '[00:09]창에 비친 꽃<br>[00:20]同じ道を歩く<br>[00:30.75]朝を探して'}
        self.assertEqual(self.get(item)['times'], [5.1, 10.25, 20, 30.75])

    def test_translation_timestamp_does_not_fill_untimed_original(self):
        item = {**self.item, 'strLyric': '朝を探して<br>[00:05]아침을 찾으며<br>'
                '[00:10.25]窓に映る花<br>[00:20]同じ道を歩く<br>[00:30.75]朝を探して'}
        self.assertEqual(self.get(item)['times'], [None, 10.25, 20, 30.75])

    def test_three_row_groups_keep_readings_and_translations_on_verified_originals(self):
        lyric = '\n'.join(f'[{stamp}]{original}\n[{stamp}]독음 {index}\n[{stamp}]번역 {index}'
                          for index, (stamp, original) in enumerate(zip(
                              ('00:05.10', '00:10.25', '00:20', '00:30.75'), self.reference)))
        result = self.get({**self.item, 'strLyric': lyric})
        self.assertEqual(result['lines'], tuple(Line(text, f'독음 {index}', f'번역 {index}')
                                               for index, text in enumerate(self.reference)))
        self.assertEqual(result['times'], [5.1, 10.25, 20, 30.75])

    def parenthetical_document(self,extra='（小さな声で（ハイ））'):
        reference=('朝の窓から青い庭を眺めながら今日の行き先をゆっくり考えた',
                   '川の向こうに見える広い公園では小鳥たちが静かに遊んでいた',
                   '昼の坂道を歩く少年は遠い町から届いた手紙を大切に持っていた',
                   '夕方の電車に乗った少女は初めて訪れる海辺の景色を楽しみにした',
                   '夜の広場に集まった友人たちは明日もまた会おうと笑って別れた')
        rows=(*reference[:2],extra,*reference[2:])
        lyric='\n'.join(f'[00:{5+index*10:02d}]{text}\n[00:{5+index*10:02d}]독음 {index}\n'
                        f'[00:{5+index*10:02d}]번역 {index}' for index,text in enumerate(rows))
        return reference,{**self.item,'strLyric':lyric}

    def test_parenthetical_difference_returns_annotations_without_any_timestamps(self):
        reference,item=self.parenthetical_document();statuses=[]
        result=self.get(item,reference=reference,report=statuses.append)
        self.assertTrue(result['annotation_only'])
        self.assertEqual(tuple(line.original for line in result['lines']),reference)
        self.assertEqual([line.korean for line in result['lines']],['번역 0','번역 1','번역 3','번역 4','번역 5'])
        self.assertEqual(result['times'],[None]*5)
        self.assertEqual(statuses,['알송 번역 5줄 보충 · 기존 싱크 유지'])

    def test_parenthetical_split_sentence_keeps_meaning_without_copying_whole_reading(self):
        reference,item=self.parenthetical_document()
        reference=(reference[0][:10],reference[0][10:20],reference[0][20:],*reference[1:])
        result=self.get(item,reference=reference)
        self.assertTrue(result['annotation_only'])
        self.assertEqual(tuple(line.original for line in result['lines']),reference)
        self.assertEqual([line.korean for line in result['lines'][:3]],['번역 0']*3)
        self.assertTrue(all(not line.reading for line in result['lines'][:3]))
        self.assertEqual(result['lines'][3].reading,'독음 1')
        self.assertEqual(result['times'],[None]*7)

    def test_parenthetical_merged_sentence_keeps_all_meanings_and_rejects_conflict(self):
        reference,item=self.parenthetical_document()
        reference=(reference[0]+reference[1],*reference[2:])
        result=self.get(item,reference=reference)
        self.assertEqual(result['lines'][0].korean,'번역 0 / 번역 1')
        self.assertEqual(result['lines'][0].reading,'')
        self.assertEqual(result['times'],[None]*4)
        other={**item,'strInfoID':'456','strLyric':item['strLyric'].replace('번역 1','다른 뜻')}
        result=self.get(item,other,reference=reference)
        self.assertEqual([line.korean for line in result['lines']],['','번역 3','번역 4','번역 5'])
        self.assertEqual(result['times'],[None]*4)

    def test_vocalise_repetition_returns_verified_translation_without_changing_clock(self):
        reference,item=self.parenthetical_document(extra='LaLaLaLaLaLaLaLa')
        reference=(*reference[:2],'LaLaLaLaLaLa',*reference[2:])
        statuses=[]
        result=self.get(item,reference=reference,report=statuses.append)
        self.assertTrue(result['annotation_only'])
        self.assertEqual(tuple(line.original for line in result['lines']),reference)
        self.assertEqual(result['times'],[None]*len(reference))
        self.assertEqual([line.korean for line in result['lines']],['번역 0','번역 1','','번역 3','번역 4','번역 5'])
        self.assertEqual(statuses,['알송 번역 5줄 보충 · 기존 싱크 유지'])

    def test_vocalise_translation_fallback_rejects_different_song_and_chant(self):
        reference,item=self.parenthetical_document(extra='LaLaLaLaLaLaLaLa')
        reference=(*reference[:2],'LaLaLaLaLaLa',*reference[2:])
        for changes in ({'strTitle':'Other Song'},{'strArtistName':'Other Singer'},
                        {'strLyric':item['strLyric'].replace('LaLaLaLaLaLaLaLa','OhOhOhOhOhOhOhOh')}):
            self.assertIsNone(self.get({**item,**changes},reference=reference))

    def aside_document(self):
        from tests.test_lyric_asides import documents
        source, reference = documents()
        for index in range(2, 11):
            source[index] = source[index][:10] + '異' + source[index][11:]
        lyric = '\n'.join(f'[02:{index:02d}.36]{row}' for index, row in enumerate(source))
        return reference, {**self.item, 'strLyric': lyric}

    def test_different_explicit_calls_use_only_verified_recording_onsets(self):
        reference, item = self.aside_document()
        result = self.get(item, reference=reference)
        self.assertEqual(tuple(line.original for line in result['lines']), tuple(reference))
        self.assertEqual(result['times'], [None if index in range(2, 11) else 120 + index + .36
                                          for index in range(30)])
        self.assertEqual(result['provider_id'], '123')

    def test_call_alignment_requires_exact_album_recording_and_valid_clock(self):
        reference, item = self.aside_document()
        for changes in ({'strAlbumName': ''}, {'strAlbumName': 'Other Album'},
                        {'strArtistName': 'Other Singer'}, {'strTitle': 'Synthetic'},
                        {'strTitle': 'Synthetic (Other Singer Ver.)'},
                        {'playtime': '300'}, {'strLyric': item['strLyric'].replace('[02:12.36]', '[04:12.36]')}):
            with self.subTest(fields=tuple(changes)):
                self.assertIsNone(self.get({**item, **changes}, reference=reference))
        self.assertIsNone(self.get(item, reference=reference, playback=replace(self.playback, album='')))

    def test_parenthetical_candidates_with_conflicting_clocks_are_held(self):
        reference, item = self.aside_document()
        other = {**item, 'strInfoID': '456',
                 'strLyric': item['strLyric'].replace('[02:', '[01:')}
        statuses = []
        self.assertIsNone(self.get(item, other, reference=reference, report=statuses.append))
        self.assertEqual(statuses, ['알송 시간표가 서로 달라 적용 보류'])

    def test_parenthetical_translation_fallback_keeps_metadata_and_clock_validation(self):
        reference,item=self.parenthetical_document()
        for changes in ({'strTitle':'Different Song'},{'strArtistName':'Different Artist'},
                        {'playtime':'400'},{'strLyric':item['strLyric'].replace('[00:25]','[00:99]')}):
            self.assertIsNone(self.get({**item,**changes},reference=reference))

    def test_annotation_fallback_never_uses_missing_nonparenthetical_lyric(self):
        reference,item=self.parenthetical_document(extra='小さな声で歌う')
        self.assertIsNone(self.get(item,reference=reference))

    def test_conflicting_annotation_only_candidates_keep_uncontested_rows(self):
        reference,item=self.parenthetical_document();statuses=[]
        other={**item,'strInfoID':'456','strLyric':item['strLyric'].replace('번역 0','다른 뜻')}
        result=self.get(item,other,reference=reference,report=statuses.append)
        self.assertEqual([line.korean for line in result['lines']],['','번역 1','번역 3','번역 4','번역 5'])
        self.assertEqual(statuses,['알송 번역 4줄 보충 · 기존 싱크 유지'])
        self.assertEqual(result['times'],[None]*5)

    def test_annotation_only_candidates_with_all_conflicting_translations_are_held(self):
        reference,item=self.parenthetical_document();statuses=[]
        other={**item,'strInfoID':'456','strLyric':item['strLyric'].replace('번역','다른 뜻')}
        self.assertIsNone(self.get(item,other,reference=reference,report=statuses.append))
        self.assertEqual(statuses,['알송 번역이 서로 달라 적용 보류'])

    def test_verified_timing_candidate_still_takes_precedence_over_annotation_only(self):
        reference,item=self.parenthetical_document()
        lyric='\n'.join(f'[00:{5+index*10:02d}]{text}' for index,text in enumerate(reference))
        other={**item,'strInfoID':'456','strLyric':lyric}
        result=self.get(item,other,reference=reference)
        self.assertNotIn('annotation_only',result)
        self.assertEqual(result['times'],[5,15,25,35,45])

    def test_single_or_differently_timed_korean_rows_are_not_guessed_as_translation(self):
        for extras in ('[00:05.10]독음인지 번역인지 불명', '[00:05.10]독음\n[00:06]번역',
                       '[00:05.10]독음\n[00:05.10]번역\n[00:05.10]추가 설명'):
            lyric = self.item['strLyric'].replace('<br>', '<br>' + extras + '<br>', 1)
            result = self.get({**self.item, 'strLyric': lyric})
            self.assertTrue(all(not line.reading and not line.korean for line in result['lines']))

    def test_korean_originals_are_retained_when_the_reference_is_korean(self):
        reference = ('첫 번째 문장', '다음 문장')
        result = self.get({**self.item, 'strLyric': '[00:05]첫 번째 문장<br>[00:10]다음 문장'},
                          reference=reference)
        self.assertEqual([line.original for line in result['lines']], list(reference))

    def test_complete_normalized_words_allow_different_line_grouping(self):
        item = {**self.item, 'strLyric': '[00:05.10]朝を探して 窓に映る花<br>'
                '[00:20]同じ道を歩く<br>[00:30.75]朝を探して'}
        self.assertEqual(self.get(item)['times'], [5.1, 20, 30.75])

    def test_duplicate_onsets_merge_adjacent_originals_without_inventing_times(self):
        item = {**self.item, 'strLyric': self.item['strLyric'].replace('[00:10.25]', '[00:05.10]')}
        self.assertEqual(self.get(item)['times'], [5.1, 20, 30.75])
        self.assertEqual(self.get(item)['lines'][0].original, '朝を探して 窓に映る花')

    def test_merged_originals_cannot_exceed_the_cache_row_limit(self):
        reference = ('朝' * 2500, '花' * 2500)
        item = {**self.item, 'strLyric': '\n'.join('[00:05]' + row for row in reference)}
        self.assertIsNone(self.get(item, reference=reference))

    def test_duplicate_onsets_separated_by_untimed_original_are_rejected(self):
        item = {**self.item, 'strLyric': '[00:05]朝を探して<br>窓に映る花<br>'
                '[00:05]同じ道を歩く<br>[00:30]朝を探して'}
        self.assertIsNone(self.get(item))

    def test_missing_extra_or_reordered_originals_are_rejected(self):
        for lyric in (self.item['strLyric'].replace('[00:10.25]窓に映る花<br>', ''),
                      self.item['strLyric'] + '[00:40]別の言葉',
                      self.item['strLyric'].replace('窓に映る花', '朝を探して'),
                      self.item['strLyric'].replace('窓に映る花', '窓に映る花 번역')):
            self.assertIsNone(self.get({**self.item, 'strLyric': lyric}))

    def test_out_of_range_backwards_and_invalid_cues_are_rejected(self):
        for stamp in ('[03:27.01]', '[00:04]', '[-00:10]', '[00:99]', '[00:10][01:10]'):
            with self.subTest(stamp=stamp):
                self.assertIsNone(self.get({**self.item, 'strLyric': self.item['strLyric'].replace('[00:10.25]', stamp)}))

    def test_zero_blank_gaps_and_explicit_metadata_do_not_create_lyric_rows(self):
        item = {**self.item, 'strLyric': '[ti:Synthetic]<br>[00:00]Synthetic (Singer Ver.)<br>'
                + self.item['strLyric'].replace('[00:20]', '[00:00]<br>[00:20]')}
        self.assertEqual(self.get(item)['times'], [5.1, 10.25, 20, 30.75])

    def test_unsynced_documents_are_not_misrepresented_as_sync(self):
        self.assertIsNone(self.get({**self.item, 'strLyric': '\n'.join(self.reference)}))
        self.assertIsNone(self.get({**self.item, 'strLyric': '[offset:500]<br>' + self.item['strLyric']}))
        self.assertIsNone(self.get({**self.item, 'strLyric': '<br>'.join('[00:00]' + text for text in self.reference)}))
        item = {**self.item, 'strLyric': self.item['strLyric'].replace('[00:05.10]', '[00:00]')}
        self.assertEqual(self.get(item)['times'], [0, 10.25, 20, 30.75])

    def test_empty_search_retries_title_only_once_without_relaxing_validation(self):
        fetch = Mock(side_effect=[soap(), soap(self.item), detail(self.item)])
        result = fetch_alsong(self.playback, self.reference, fetch)
        self.assertEqual(result['provider_id'], '123')
        self.assertEqual([call.args for call in fetch.call_args_list],
                         [(SEARCH_ACTION, {'title': self.playback.title, 'artist': self.playback.artist, 'pageNo': '1'}),
                          (SEARCH_ACTION, {'title': self.playback.title, 'artist': '', 'pageNo': '1'}),
                          (DETAIL_ACTION, {'lyricID': '123'})])
        fetch = Mock(return_value=soap())
        self.assertIsNone(fetch_alsong(self.playback, self.reference, fetch))
        self.assertEqual(fetch.call_count, 2)
        fetch = Mock(side_effect=[soap(), soap({**self.item, 'strArtistName': 'Other'})])
        self.assertIsNone(fetch_alsong(self.playback, self.reference, fetch))

    def test_live_empty_response_without_result_is_no_match_and_tries_title_once(self):
        root, response = envelope(SEARCH_ACTION)
        empty = ET.tostring(root, encoding='utf-8')
        fetch = Mock(side_effect=[empty, soap(self.item), detail(self.item)])
        self.assertEqual(fetch_alsong(self.playback, self.reference, fetch)['provider_id'], '123')
        self.assertEqual(fetch.call_args_list[1].args[1]['artist'], '')
        fetch = Mock(return_value=empty)
        self.assertIsNone(fetch_alsong(self.playback, self.reference, fetch))
        self.assertEqual(fetch.call_count, 2)

    def test_unexpected_search_child_is_not_silently_treated_as_empty(self):
        root, response = envelope(SEARCH_ACTION)
        ET.SubElement(response, 'unexpected').text = 'invalid response'
        with self.assertRaises(ValueError):
            fetch_alsong(self.playback, self.reference, Mock(return_value=ET.tostring(root)))

    def test_nonempty_metadata_mismatch_retries_title_once_and_finds_verified_recording(self):
        for changes in ({'strTitle': 'Other'}, {'strArtistName': 'Other Singer'}):
            with self.subTest(changes=changes):
                fetch = Mock(side_effect=[soap({**self.item, **changes}), soap(self.item), detail(self.item)])
                result = fetch_alsong(self.playback, self.reference, fetch)
                self.assertEqual(result['provider_id'], '123')
                self.assertEqual(result['times'], [5.1, 10.25, 20, 30.75])
                self.assertEqual([call.args for call in fetch.call_args_list],
                                 [(SEARCH_ACTION, {'title': self.playback.title, 'artist': self.playback.artist, 'pageNo': '1'}),
                                  (SEARCH_ACTION, {'title': self.playback.title, 'artist': '', 'pageNo': '1'}),
                                  (DETAIL_ACTION, {'lyricID': '123'})])

    def test_mismatching_results_after_title_retry_do_not_trigger_more_searches_or_details(self):
        fetch = Mock(return_value=soap({**self.item, 'strTitle': 'Other'}))
        statuses = []
        self.assertIsNone(fetch_alsong(self.playback, self.reference, fetch, report=statuses.append))
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(statuses, ['알송 곡명·가수 불일치'])
        self.assertTrue(all(call.args[0] == SEARCH_ACTION for call in fetch.call_args_list))

    def test_title_retry_keeps_album_and_original_checks(self):
        first = {**self.item, 'strTitle': 'Other'}
        wrong_album = {**self.item, 'strAlbumName': 'Other Album'}
        fetch = Mock(side_effect=[soap(first), soap(wrong_album)])
        statuses = []
        self.assertIsNone(fetch_alsong(self.playback, self.reference, fetch, report=statuses.append))
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(statuses, ['알송 앨범 불일치'])
        wrong_words = {**self.item, 'strLyric': self.item['strLyric'].replace('窓に映る花', '違う言葉')}
        fetch = Mock(side_effect=[soap(first), soap(self.item), detail(wrong_words)])
        self.assertIsNone(fetch_alsong(self.playback, self.reference, fetch))
        self.assertEqual(fetch.call_count, 3)

    def test_conflicting_clocks_are_rejected_identical_reuploads_are_accepted(self):
        other = {**self.item, 'strInfoID': '456'}
        self.assertEqual(self.get(self.item, other)['provider_id'], '123')
        other['strLyric'] = other['strLyric'].replace('[00:05.10]', '[00:07]')
        self.assertIsNone(self.get(self.item, other))
        self.assertIsNone(self.get(other, self.item))

    def test_small_clock_variation_selects_one_registration_by_numeric_id(self):
        other = {**self.item, 'strInfoID': '2',
                 'strLyric': self.item['strLyric'].replace('[00:05.10]', '[00:05.46]')}
        for records in ((self.item, other), (other, self.item)):
            result = self.get(*records)
            self.assertEqual(result['provider_id'], '2')
            self.assertEqual(result['times'], [5.46, 10.25, 20, 30.75])

    def test_clock_tolerance_includes_half_second_but_rejects_any_larger_gap(self):
        for stamp, accepted in (('[00:05.60]', True), ('[00:05.601]', False)):
            with self.subTest(stamp=stamp):
                other = {**self.item, 'strInfoID': '456',
                         'strLyric': self.item['strLyric'].replace('[00:05.10]', stamp)}
                self.assertEqual(self.get(self.item, other) is not None, accepted)

    def test_clock_tolerance_compares_every_pair_and_rejects_transitive_chain(self):
        middle = {**self.item, 'strInfoID': '456',
                  'strLyric': self.item['strLyric'].replace('[00:05.10]', '[00:05.40]')}
        last = {**self.item, 'strInfoID': '789',
                'strLyric': self.item['strLyric'].replace('[00:05.10]', '[00:05.70]')}
        self.assertIsNotNone(self.get(self.item, middle))
        self.assertIsNotNone(self.get(middle, last))
        self.assertIsNone(self.get(middle, self.item, last))

    def test_matching_clocks_prefer_fewer_text_edits_before_registration_id(self):
        reference, item = self.long_document()
        other = {**item, 'strInfoID': '2',
                 'strLyric': item['strLyric'].replace('first', 'firsx').replace('[00:05.10]', '[00:05.46]')}
        result = self.get(other, item, reference=reference)
        self.assertEqual(result['provider_id'], '123')
        self.assertEqual(result['times'], [5.1, 10.25, 20, 30.75])

    def test_matching_clocks_align_typo_insertions_and_deletions_at_original_boundaries(self):
        reference, item = self.long_document()
        other = {**item, 'strInfoID': '2', 'strLyric': item['strLyric']
                 .replace('first', 'firt').replace('second', 'seccond')
                 .replace('[00:05.10]', '[00:05.46]')}
        result = self.get(other, item, reference=reference)
        self.assertEqual(result['provider_id'], '123')

    def test_matching_clocks_prefer_more_observed_onsets_then_translations(self):
        untimed = {**self.item, 'strInfoID': '2',
                   'strLyric': self.item['strLyric'].replace('[00:10.25]', '')}
        self.assertEqual(self.get(untimed, self.item)['provider_id'], '123')
        translated = {**self.item, 'strInfoID': '456', 'strLyric': '\n'.join(
            f'[{stamp}]{text}\n[{stamp}]독음 {index}\n[{stamp}]번역 {index}'
            for index, (stamp, text) in enumerate(zip(
                ('00:05.10', '00:10.25', '00:20', '00:30.75'), self.reference)))}
        self.assertEqual(self.get(self.item, translated)['provider_id'], '456')

    def test_different_row_layouts_remain_held_without_invented_correspondence(self):
        other = {**self.item, 'strInfoID': '456',
                 'strLyric': self.item['strLyric'].replace('<br>[00:10.25]', ' ')}
        self.assertIsNotNone(self.get(other))
        self.assertIsNone(self.get(self.item, other))

    def test_matching_album_resolves_other_album_clock(self):
        other = {**self.item, 'strAlbumName': 'Other Album', 'strInfoID': '456',
                 'strLyric': self.item['strLyric'].replace('[00:05.10]', '[00:07]')}
        self.assertEqual(self.get(other, self.item)['provider_id'], '123')

    def test_explicit_other_album_search_never_requests_its_recording_clock(self):
        other = {**self.item, 'strAlbumName': 'Other Album'}
        fetch = Mock(return_value=soap(other))
        self.assertIsNone(fetch_alsong(self.playback, self.reference, fetch))
        self.assertEqual(fetch.call_count, 1)
        self.assertIsNotNone(self.get(other, playback=replace(self.playback, album='')))

    def test_detail_album_conflict_cannot_override_matching_or_missing_search_album(self):
        for album in (self.playback.album, ''):
            with self.subTest(album=album):
                search = {**self.item, 'strAlbumName': album}
                wrong = {**self.item, 'strAlbumName': 'Different Detail Album'}
                fetch = Mock(side_effect=[soap(search), detail(wrong)])
                self.assertIsNone(fetch_alsong(self.playback, self.reference, fetch))

    def test_missing_album_fields_do_not_disable_verified_alsong_timings(self):
        for album in ('', None):
            with self.subTest(album=album):
                item = {**self.item, 'strAlbumName': album}
                self.assertEqual(self.get(item)['times'], [5.1, 10.25, 20, 30.75])

    def test_matching_search_album_allows_detail_without_an_album_field(self):
        unknown = {**self.item, 'strAlbumName': ''}
        fetch = Mock(side_effect=[soap(self.item), detail(unknown)])
        result = fetch_alsong(self.playback, self.reference, fetch)
        self.assertEqual(result['times'], [5.1, 10.25, 20, 30.75])

    def test_xml_parse_and_transport_errors_stop_without_retries(self):
        for payload in (b'not XML', b'<html/>', b'<!DOCTYPE x><GetResembleLyric2Result/>',
                        b'<GetResembleLyric2Result><Fault/></GetResembleLyric2Result>',
                        b' ' * (MAX_RESPONSE_BYTES + 1)):
            fetch = Mock(return_value=payload)
            with self.assertRaises(ValueError):
                fetch_alsong(self.playback, self.reference, fetch)
            self.assertEqual(fetch.call_count, 1)
        fetch = Mock(side_effect=TimeoutError('test'))
        with self.assertRaises(TimeoutError):
            fetch_alsong(self.playback, self.reference, fetch)
        self.assertEqual(fetch.call_count, 1)

    def test_duplicate_xml_fields_are_rejected(self):
        payload = soap(self.item).replace(b'</ns1:title>', b'</ns1:title><ns1:title>Other</ns1:title>')
        with self.assertRaises(ValueError):
            fetch_alsong(self.playback, self.reference, lambda action, params: payload)

    def test_request_is_bounded_escaped_and_sends_only_search_metadata(self):
        playback = replace(self.playback, title='Synthetic <Singer> & Friends')
        item = {**self.item, 'strTitle': playback.title}
        opener = Mock()
        opener.open.side_effect = [io.BytesIO(soap(item)), io.BytesIO(detail(item))]
        with patch('alsong_provider.build_opener', return_value=opener):
            self.assertIsNotNone(fetch_alsong(playback, [Line(text, 'private reading', 'private translation')
                                                       for text in self.reference]))
        request = opener.open.call_args_list[0].args[0]
        self.assertEqual(request.full_url, ENDPOINT)
        self.assertEqual(opener.open.call_args.kwargs['timeout'], REQUEST_TIMEOUT)
        document = ET.fromstring(request.data)
        self.assertEqual(document.tag, '{http://schemas.xmlsoap.org/soap/envelope/}Envelope')
        fields = {e.tag.rsplit('}', 1)[-1]: e.text for e in document.iter() if not len(e)}
        self.assertEqual(set(fields), {'encData', 'title', 'artist', 'pageNo'})
        self.assertEqual({key: value for key, value in fields.items() if key != 'encData'},
                         {'title': playback.title, 'artist': playback.artist, 'pageNo': '1'})
        self.assertRegex(fields['encData'], r'^[0-9a-f]{256}$')
        self.assertNotIn(b'private', request.data)
        self.assertEqual(request.get_header('Soapaction'), 'ALSongWebServer/' + SEARCH_ACTION)
        self.assertEqual(request.get_header('Content-type'), 'text/xml; charset=utf-8')
        self.assertEqual(request.get_header('User-agent'), 'gSOAP/2.7')
        detail_request = opener.open.call_args_list[1].args[0]
        detail_fields = {e.tag.rsplit('}', 1)[-1]: e.text for e in ET.fromstring(detail_request.data).iter() if not len(e)}
        self.assertEqual(set(detail_fields), {'encData', 'lyricID'})
        self.assertEqual(detail_fields['lyricID'], '123')
        self.assertEqual(detail_request.get_header('Soapaction'), 'ALSongWebServer/' + DETAIL_ACTION)
        opener.open.side_effect = None
        opener.open.return_value = io.BytesIO(b' ' * (MAX_RESPONSE_BYTES + 1))
        with patch('alsong_provider.build_opener', return_value=opener), self.assertRaises(ValueError):
            fetch_alsong(playback, self.reference)

    def test_redirects_and_contributor_fields_are_not_forwarded(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.example'))
        result = self.get({**self.item, 'registerMail': 'private@example.com',
                           'registerComment': 'token=secret', 'registerName': 'private contributor'})
        self.assertEqual(set(result), {'source', 'provider_id', 'lines', 'times'})
        self.assertNotIn('private', repr(result))

    def test_artist_list_requires_exact_tokens_not_substrings_or_slash_fragments(self):
        for artist in ('Another、Example Singer、Third', 'Example Singer, Other', 'Other; Example Singer'):
            self.assertIsNotNone(self.get({**self.item, 'strArtistName': artist}))
        for artist in ('The Example Singer Band', 'Example Singer Jr.', 'Example Singer/Other'):
            self.assertIsNone(self.get({**self.item, 'strArtistName': artist}))
        playback = replace(self.playback, artist='AC/DC')
        self.assertIsNotNone(self.get({**self.item, 'strArtistName': 'AC/DC'}, playback=playback))
        self.assertIsNone(self.get({**self.item, 'strArtistName': 'AC'}, playback=playback))

    def test_collaboration_artist_delimiters_accept_sparkle_group_metadata(self):
        playback=replace(self.playback,title='Sparkle',artist='イルミネーションスターズ')
        for separator in (' x ', ' X ', '×', ' × ', ' & ', ' ＆ ', '\u3000x\u3000'):
            with self.subTest(separator=separator):
                artist=playback.artist+separator+'ノクチル'
                item={**self.item,'strTitle':'Sparkle','strArtistName':artist}
                self.assertIsNotNone(self.get(item,playback=playback))
                self.assertTrue(_artist_matches('ノクチル',artist))

    def test_spaced_collaboration_delimiters_do_not_split_inside_artist_names(self):
        for remote in ('Example Singerx Other', 'Example Singer xOther',
                       'Example Singer&Other', 'Example Singer＆Other'):
            with self.subTest(remote=remote):
                self.assertFalse(_artist_matches('Example Singer',remote))
        for remote in ('RADWIMPSX', 'The RADWIMPS Band', 'RADWIMPS Tribute'):
            self.assertFalse(_artist_matches('RADWIMPS',remote))

    def test_x_japan_name_is_preserved_after_list_and_collaboration_separators(self):
        for remote in ('X JAPAN', 'Other x X JAPAN', 'Other、 X JAPAN', 'Other× X JAPAN',
                       'Other × X JAPAN', 'X JAPAN x Other', 'First; X JAPAN & Last'):
            with self.subTest(remote=remote):
                self.assertTrue(_artist_matches('X JAPAN',remote))
                self.assertFalse(_artist_matches('JAPAN',remote))
                self.assertFalse(_artist_matches('X',remote))

    def test_band_name_punctuation_survives_collaboration_tokenization(self):
        for artist,fragments in (('AC/DC',('AC','DC')), ("B'z",('B','z')),
                                 ('Simon&Garfunkel',('Simon','Garfunkel'))):
            with self.subTest(artist=artist):
                self.assertTrue(_artist_matches(artist,'Other x '+artist))
                for fragment in fragments:
                    self.assertFalse(_artist_matches(fragment,'Other x '+artist))

    def test_local_collaboration_requires_every_exact_artist_token(self):
        self.assertTrue(_artist_matches('First x Second','Third、 First × Second'))
        self.assertFalse(_artist_matches('First x Second','First'))
        self.assertFalse(_artist_matches('First x Second','First x Second Tribute'))

    def test_collaboration_match_never_relaxes_full_title_or_full_originals(self):
        item={**self.item,'strArtistName':'Example Singer x Other'}
        self.assertIsNone(self.get({**item,'strTitle':'Synthetic'}))
        self.assertIsNone(self.get({**item,'strTitle':'Synthetic (Singer Ver.) Live'}))
        self.assertIsNone(self.get({**item,'strLyric':item['strLyric'].replace('窓に映る花','別の曲の言葉')}))

    def test_search_artist_match_cannot_override_wrong_detail_artist_or_title(self):
        for changes in ({'strArtistName': 'Other'}, {'strTitle': 'Synthetic Live'}):
            fetch = Mock(side_effect=[soap(self.item), detail({**self.item, **changes})])
            self.assertIsNone(fetch_alsong(self.playback, self.reference, fetch))
            self.assertEqual(fetch.call_count, 2)

    def test_detail_id_must_match_search_id_and_ids_must_be_digits(self):
        for identifier in ('124', '', '123.0', 'private-key', '１２３'):
            fetch = Mock(side_effect=[soap(self.item), detail({**self.item, 'strInfoID': identifier})])
            self.assertIsNone(fetch_alsong(self.playback, self.reference, fetch))
        for identifier in ('', 'private-key', '１２３', '1' * 33):
            fetch = Mock(return_value=soap({**self.item, 'strInfoID': identifier}))
            self.assertIsNone(fetch_alsong(self.playback, self.reference, fetch))
            self.assertEqual(fetch.call_count, 1)

    def test_missing_detail_is_a_miss(self):
        fetch = Mock(side_effect=[soap(self.item), detail(None)])
        self.assertIsNone(fetch_alsong(self.playback, self.reference, fetch))

    def test_available_playtime_is_seconds_and_checks_recording_length(self):
        for playtime in ('207', '205', '209', '0', ''):
            self.assertIsNotNone(self.get({**self.item, 'playtime': playtime}))
        for playtime in ('210', '207000', '3:27', 'NaN', '-1'):
            self.assertIsNone(self.get({**self.item, 'playtime': playtime}))

    def test_more_than_three_best_candidates_are_ambiguous_without_detail_requests(self):
        records = [{**self.item, 'strInfoID': str(number)} for number in range(1, 5)]
        fetch = Mock(return_value=soap(*records))
        self.assertIsNone(fetch_alsong(self.playback, self.reference, fetch))
        self.assertEqual(fetch.call_count, 1)
        fetch = Mock(side_effect=fixture_fetch(*records[:3]))
        self.assertIsNotNone(fetch_alsong(self.playback, self.reference, fetch))
        self.assertEqual(fetch.call_count, 4)

    def test_album_preference_applies_before_bounded_detail_requests(self):
        records = [self.item] + [{**self.item, 'strInfoID': str(number), 'strAlbumName': 'Other Album'}
                               for number in range(200, 205)]
        fetch = Mock(side_effect=fixture_fetch(*records))
        self.assertEqual(fetch_alsong(self.playback, self.reference, fetch)['provider_id'], '123')
        self.assertEqual(fetch.call_count, 2)

    def test_repeated_search_ids_only_request_detail_once(self):
        fetch = Mock(side_effect=fixture_fetch(self.item, self.item, self.item, self.item))
        self.assertIsNotNone(fetch_alsong(self.playback, self.reference, fetch))
        self.assertEqual(fetch.call_count, 2)

    def test_plain_newlines_in_modern_detail_preserve_original_times(self):
        item = {**self.item, 'strLyric': self.item['strLyric'].replace('<br>', '\r\n')}
        self.assertEqual(self.get(item)['times'], [5.1, 10.25, 20, 30.75])

    def test_report_distinguishes_empty_search_and_metadata_mismatch_once(self):
        for records,expected in (((), '알송 검색 결과 없음'),
                                  (({**self.item,'strTitle':'Other Song'},), '알송 곡명·가수 불일치'),
                                  (({**self.item,'strInfoID':'invalid'},), '알송 상세 정보 확인 실패')):
            statuses=[]
            self.assertIsNone(self.get(*records,report=statuses.append))
            self.assertEqual(statuses,[expected])

    def test_report_reference_mismatch_can_confirm_no_korean_only_after_detail_validation(self):
        item={**self.item,'strLyric':self.item['strLyric'].replace('窓に映る花','違う言葉')}
        statuses=[]
        self.assertIsNone(self.get(item,report=statuses.append))
        self.assertEqual(statuses,['알송 적용 조건 불일치 · 한글 번역 없음'])

    def test_report_does_not_claim_no_korean_for_failed_or_empty_details(self):
        for supplied in (None,{**self.item,'strInfoID':'999'},
                         {**self.item,'strArtistName':'Wrong Artist'},{**self.item,'strLyric':''}):
            statuses=[]
            fetch=Mock(side_effect=[soap(self.item),detail(supplied)])
            self.assertIsNone(fetch_alsong(self.playback,self.reference,fetch,report=statuses.append))
            self.assertEqual(len(statuses),1)
            self.assertNotIn('한글 번역 없음',statuses[0])
            expected='알송 적용 조건 불일치' if supplied and supplied.get('strLyric')=='' else '알송 상세 정보 확인 실패'
            self.assertEqual(statuses[0],expected)

    def test_report_does_not_claim_all_candidates_have_no_korean_when_one_detail_failed(self):
        first={**self.item,'strLyric':self.item['strLyric'].replace('窓に映る花','違う言葉')}
        second={**self.item,'strInfoID':'456'}
        fetch=Mock(side_effect=[soap(first,second),detail(first),detail(None)])
        statuses=[]
        self.assertIsNone(fetch_alsong(self.playback,self.reference,fetch,report=statuses.append))
        self.assertEqual(statuses,['알송 적용 조건 불일치'])

    def test_report_success_distinguishes_absent_and_unrecognized_korean_annotations(self):
        for lyric,expected in ((self.item['strLyric'],'알송 싱크 확인 · 한글 번역 없음'),
                               (self.item['strLyric']+'[00:30.75]한글 한 줄',
                                '알송 싱크 확인 · 번역 형식 확인 필요')):
            statuses=[]
            result=self.get({**self.item,'strLyric':lyric},report=statuses.append)
            self.assertEqual(result['times'],[5.1,10.25,20,30.75])
            self.assertEqual(statuses,[expected])

    def test_report_translation_count_never_includes_provider_text_or_contributor_details(self):
        lyric=('[00:05.10]朝を探して\n[00:05.10]첫 독음\n[00:05.10]첫 번역\n'
               '[00:10.25]窓に映る花\n[00:10.25]둘째 독음\n[00:10.25]둘째 번역')
        item={**self.item,'strLyric':lyric,'registerName':'private person',
              'registerMail':'private@example.com','registerComment':'account-secret'}
        statuses=[]
        result=self.get(item,reference=self.reference[:2],report=statuses.append)
        self.assertEqual(statuses,['알송 자료 확인 · 번역 2줄'])
        self.assertEqual([line.korean for line in result['lines']],['첫 번역','둘째 번역'])
        for supplied in (*self.reference,'첫 번역','private','account-secret',self.playback.title,self.playback.artist):
            self.assertNotIn(supplied,statuses[0])
        self.assertEqual(set(result),{'source','provider_id','lines','times'})

    def test_report_ambiguous_candidates_and_clocks_are_held_without_false_absence(self):
        statuses=[]
        records=[{**self.item,'strInfoID':str(number)} for number in range(1,5)]
        self.assertIsNone(self.get(*records,report=statuses.append))
        self.assertEqual(statuses,['알송 상세 조회 한도 초과 · 후보 미검증'])
        statuses=[]
        other={**self.item,'strInfoID':'456','strLyric':self.item['strLyric'].replace('[00:05.10]','[00:07]')}
        self.assertIsNone(self.get(self.item,other,report=statuses.append))
        self.assertEqual(statuses,['알송 시간표가 서로 달라 적용 보류'])

    def test_translation_detail_limit_reports_unverified_candidates_for_album_and_version(self):
        reference, item = self.long_document()
        for title, searches in ((self.playback.title, 1), ('Synthetic', 2)):
            with self.subTest(title=title):
                records = [{**item, 'strInfoID': str(number), 'strTitle': title,
                            'strAlbumName': 'Other Album'} for number in range(7)]
                fetch = Mock(return_value=soap(*records))
                statuses = []
                self.assertIsNone(fetch_alsong(self.playback, reference, fetch, report=statuses.append))
                self.assertEqual(fetch.call_count, searches)
                self.assertTrue(all(call.args[0] == SEARCH_ACTION for call in fetch.call_args_list))
                self.assertEqual(statuses, ['알송 번역 상세 조회 한도 초과 · 후보 미검증'])

    def test_report_is_local_only_and_does_not_change_requests_or_error_handling(self):
        statuses=[]
        fetch=Mock(side_effect=fixture_fetch(self.item))
        result=fetch_alsong(self.playback,self.reference,fetch,report=statuses.append)
        self.assertEqual(result,self.get(self.item))
        self.assertEqual([call.args for call in fetch.call_args_list],
                         [(SEARCH_ACTION,{'title':self.playback.title,'artist':self.playback.artist,'pageNo':'1'}),
                          (DETAIL_ACTION,{'lyricID':'123'})])
        self.assertEqual(len(statuses),1)
        statuses=[]
        with self.assertRaises(TimeoutError):
            fetch_alsong(self.playback,self.reference,Mock(side_effect=TimeoutError('remote detail')),report=statuses.append)
        self.assertEqual(statuses,[])

    def test_report_missing_input_is_a_wait_state_without_network_calls(self):
        for playback,reference,expected in ((replace(self.playback,title=''),self.reference,
                                            '알송 조회 대기 · 재생 정보 확인 필요'),
                                           (self.playback,(),'알송 조회 대기 · 기준 원문 필요')):
            statuses=[]
            self.assertIsNone(fetch_alsong(playback,reference,
                             fetch=lambda *args:self.fail('unexpected request'),report=statuses.append))
            self.assertEqual(statuses,[expected])


if __name__ == '__main__':
    unittest.main()
