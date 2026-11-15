from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from live_catalog import LiveCatalog, RESOURCE_PATH, read_catalog, title_key, work_key
from tools.build_live_catalog import expect, numbered, pylon_setlist, setlist_rows, song_row, write_candidate
from bs4 import BeautifulSoup


def snapshot():
    return {
        'schema_version': 1, 'coverage': 'partial', 'verified_at': '2026-10-02',
        'events': [{
            'id': 'million-day2', 'title': 'MILLION LIVE! DAY2', 'date': '2024-02-25',
            'series': ['million'], 'source_url': 'https://idolmaster-official.jp/news/source',
            'songs': [{'title': 'ESPADA', 'song_title': 'ESPADA',
                       'performance_title': 'ESPADA（Long Intro ver.）'},
                      {'title': 'Thank You!'}, {'title': 'Thank You!'}],
        }],
    }


class LiveCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'live.json'
        self.save(snapshot())

    def save(self, value):
        self.path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def test_missing_file_is_an_empty_unavailable_catalog(self):
        catalog = LiveCatalog(Path(self.temp.name) / 'missing.json')
        self.assertEqual(catalog.status, 'unavailable')
        self.assertEqual(catalog.events, [])
        self.assertEqual(catalog.for_song({'title': 'ESPADA', 'series': ['million']}), [])

    def test_espada_is_work_match_not_a_recording_match(self):
        result = LiveCatalog(self.path).for_song({'title': 'ＥＳＰＡＤＡ', 'series': ['million']})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['date'], '2024-02-25')
        self.assertEqual(result[0]['match_basis'], 'title_and_series')
        self.assertTrue(result[0]['recording_version_unverified'])
        self.assertEqual(result[0]['songs'][0]['performance_title'], 'ESPADA（Long Intro ver.）')

    def test_exact_performance_title_is_also_searchable(self):
        self.assertEqual(len(LiveCatalog(self.path).for_song(
            {'title': 'ESPADA (Long Intro ver.)', 'series': ['million']})), 1)

    def test_an_unknown_recording_suffix_is_not_stripped(self):
        self.assertEqual(LiveCatalog(self.path).for_song(
            {'title': 'ESPADA (Acoustic ver.)', 'series': ['million']}), [])

    def test_known_length_versions_link_to_work_without_claiming_recording(self):
        for suffix in ('(M@STER VERSION)', '（GAME VERSION）', '(GAME ver.)', '(Game Size)'):
            with self.subTest(suffix=suffix):
                result = LiveCatalog(self.path).for_song({'title': 'ESPADA ' + suffix, 'series': ['million']})
                self.assertEqual(len(result), 1)
                self.assertEqual(result[0]['match_basis'], 'known_work_title_and_series')
                self.assertTrue(result[0]['recording_version_unverified'])
        for title in ('ESPADA (2023 Version)', 'ESPADA (Remix)', 'ESPADA (Acoustic ver.)',
                      'ESPADA (M@STER VERSION) (Off Vocal)'):
            self.assertNotEqual(work_key(title), work_key('ESPADA'))

    def test_spoken_and_instrumental_records_do_not_receive_vocal_history(self):
        for kind in ('spoken', 'instrumental', 'unknown'):
            self.assertEqual(LiveCatalog(self.path).for_song(
                {'title': 'ESPADA', 'series': ['million'], 'kind': kind}), [])

    def test_solo_suffix_requires_an_exact_recording_artist_credit(self):
        for suffix in ('[田中琴葉 Solo Ver.]', '（田中琴葉 ソロVer.）', '(田中琴葉 ソロ・リミックス)'):
            song = {'title': 'ESPADA ' + suffix, 'series': ['million'], 'artists': ['田中琴葉']}
            self.assertEqual(len(LiveCatalog(self.path).for_song(song)), 1)
            song['artists'] = ['別の歌手']
            self.assertEqual(LiveCatalog(self.path).for_song(song), [])
        self.assertNotEqual(work_key('ESPADA (Acoustic Ver.)', ['Acoustic']), work_key('ESPADA'))

    def test_reverse_navigation_keeps_order_reprises_and_all_recording_candidates(self):
        songs = [{'id': 'full', 'title': 'ESPADA (M@STER VERSION)', 'series': ['million']},
                 {'id': 'game', 'title': 'ESPADA (GAME VERSION)', 'series': ['million']},
                 {'id': 'wrong-brand', 'title': 'ESPADA', 'series': ['sidem']},
                 {'id': 'instrumental', 'title': 'ESPADA', 'series': ['million'], 'kind': 'instrumental'}]
        catalog = LiveCatalog(self.path)
        rows = catalog.songs_for_event(catalog.events[0], songs)
        self.assertEqual([row['position'] for row in rows], [1, 2, 3])
        self.assertEqual([song['id'] for song in rows[0]['matches']], ['full', 'game'])
        self.assertEqual(rows[1]['matches'], [])
        self.assertTrue(rows[0]['recording_version_unverified'])
        self.assertEqual(rows[0]['performance']['performance_title'], 'ESPADA（Long Intro ver.）')
        rows[0]['matches'][0]['title'] = 'changed'
        rows[0]['performance']['title'] = 'changed'
        self.assertEqual(songs[0]['title'], 'ESPADA (M@STER VERSION)')
        self.assertEqual(catalog.events[0]['songs'][0]['title'], 'ESPADA')
        self.assertEqual(catalog.songs_for_event({'id': 'invented'}, songs), [])

    def test_same_title_in_another_brand_is_not_automatically_associated(self):
        catalog = LiveCatalog(self.path)
        for series in (['sidem'], ['other'], []):
            self.assertEqual(catalog.for_song({'title': 'ESPADA', 'series': series}), [])

    def test_indexed_live_cover_matches_independently_of_cast_and_filters(self):
        value=snapshot()
        event=value['events'][0]
        event['songs']=[
            {'title':'風花','song_title':'風花','series':['million'],'series_basis':'event_section_brand',
             'song_source_url':'https://imas-db.jp/song/detail/142.html','artists':['Another singer']},
            {'title':'Short','series_basis':'event_section_brand'},
            {'title':'長い名前が付いた舞台曲','series_basis':'event_section_brand','performance_kind':'dance_without_vocals'},
            {'title':'長い名前を持つ異作品','series_basis':'event_section_brand'}]
        self.save(value)
        catalog=LiveCatalog(self.path)
        vocal={'id':'cover','title':'風花','series':['765'],'kind':'song','artists':['四条貴音']}
        recordings=[vocal,{'id':'short','title':'Short','series':['shiny'],'kind':'song'},
                    {'id':'dance','title':'長い名前が付いた舞台曲','series':['shiny'],'kind':'song'},
                    {'id':'alternate1','title':'長い名前を持つ異作品','series':['shiny'],'kind':'song'},
                    {'id':'alternate2','title':'長い名前を持つ異作品','series':['sidem'],'kind':'song'}]
        catalog.index_recordings(recordings)
        self.assertEqual([row['matches'] for row in catalog.songs_for_event(event,recordings)],[[vocal],[],[],[]])
        self.assertEqual(catalog.songs_for_event(event,recordings[:1])[0]['performance']['artists'],['Another singer'])
        self.assertEqual(catalog.songs_for_event(event,recordings[-1:])[-1]['matches'],[])
        self.assertEqual(catalog.count_for_song(vocal),1)
        self.assertEqual(catalog.for_song(vocal)[0]['id'],event['id'])

    def test_no_substring_or_fuzzy_title_match(self):
        catalog = LiveCatalog(self.path)
        for title in ('ESPA', 'My ESPADA', 'ESPADA!', ''):
            self.assertEqual(catalog.for_song({'title': title, 'series': ['million']}), [])

    def test_reprises_do_not_repeat_the_event(self):
        matches = LiveCatalog(self.path).for_song({'title': 'Thank You!', 'series': ['million']})
        self.assertEqual(len(matches), 1)

    def test_count_uses_vocal_row_series_and_counts_reprises_and_medleys_once(self):
        value = snapshot()
        original = value['events'][0]
        value['events'] += [
            {**deepcopy(original), 'id':'different-brand', 'series':['million','shiny'], 'songs':[
                {'title':'ESPADA', 'series':['shiny']},
                {'title':'ESPADA', 'series':['million'], 'performance_kind':'dance_without_vocals'}]},
            {**deepcopy(original), 'id':'dance-only', 'songs':[
                {'title':'ESPADA', 'performance_kind':'dance_without_vocals'}]},
            {**deepcopy(original), 'id':'medley', 'series':['million','shiny'], 'songs':[
                {'title':'Reviewed medley', 'song_title':'ESPADA', 'series':['million']},
                {'title':'ESPADA', 'series':['million']}]}]
        self.save(value)
        catalog = LiveCatalog(self.path)
        for series, expected in [(['million'], 2), (['shiny'], 1), (['million','shiny'], 3),
                                 (['other'], 0), (['sidem'], 0)]:
            with self.subTest(series=series):
                song = {'title':'ESPADA', 'series':series}
                self.assertEqual(catalog.count_for_song(song), expected)
                self.assertEqual(catalog.count_for_song(song), len(catalog.for_song(song)))
        self.assertEqual(catalog.count_for_song({'title':'Thank You!', 'series':['million']}), 1)
        self.assertEqual(catalog.coverage, 'partial')

    def test_count_recognizes_known_versions_and_excludes_nonvocal_or_missing_records_without_copying(self):
        catalog = LiveCatalog(self.path)
        cases = [({'title':'ＥＳＰＡＤＡ', 'series':['million']}, 1),
                 ({'title':'ESPADA (M@STER VERSION)', 'series':['million']}, 1),
                 ({'title':'ESPADA (Long Intro ver.)', 'series':['million']}, 1),
                 ({'title':'ESPADA [田中琴葉 Solo Ver.]', 'series':['million'], 'artists':['田中琴葉']}, 1),
                 ({'title':'ESPADA [田中琴葉 Solo Ver.]', 'series':['million'], 'artists':['別の歌手']}, 0),
                 ({'title':'ESPADA (Unknown Remix)', 'series':['million']}, 0),
                 ({'title':'ESPADA', 'series':['million'], 'kind':'instrumental'}, 0),
                 ({'title':'ESPADA', 'series':['million'], 'kind':'spoken'}, 0),
                 ({'title':'ESPADA', 'series':['million'], 'kind':'unknown'}, 0),
                 ({'title':'ESPADA', 'series':[]}, 0), ({}, 0), (None, 0)]
        before = deepcopy(catalog.events)
        for song, expected in cases:
            with self.subTest(song=song):
                self.assertEqual(len(catalog.for_song(song)), expected)
                with patch('live_catalog.deepcopy', side_effect=AssertionError('count copied an event')):
                    self.assertEqual(catalog.count_for_song(song), expected)
        self.assertEqual(catalog.events, before)
        missing = LiveCatalog(Path(self.temp.name) / 'missing.json')
        self.assertEqual(missing.count_for_song({'title':'ESPADA', 'series':['million']}), 0)

    def test_count_uses_reviewed_work_identity_only_when_the_recording_evidence_matches(self):
        alias = {'recording_title':'ESPADA (Reviewed arrangement)', 'work_title':'ESPADA',
                 'artists':['Singer'], 'series':['million'], 'release_id':'release', 'release_title':'Album'}
        with patch('live_catalog._reviewed_work_aliases', return_value={'recording':alias}):
            catalog = LiveCatalog(self.path)
        song = {'id':'mb:recording', 'source_id':'musicbrainz', 'kind':'song',
                'title':alias['recording_title'], 'artists':alias['artists'], 'series':alias['series'],
                'releases':[{'id':'release','title':'Album'}]}
        self.assertEqual(catalog.count_for_song(song), 1)
        self.assertEqual(catalog.count_for_song(song), len(catalog.for_song(song)))
        song['releases'] = [{'id':'release','title':'Another album'}]
        self.assertEqual(catalog.count_for_song(song), 0)
        self.assertEqual(catalog.for_song(song), [])

    def test_count_reflects_successful_reload_and_invalid_reload_preserves_last_good_counts(self):
        catalog = LiveCatalog(self.path)
        song = {'title':'ESPADA', 'series':['million']}
        self.assertEqual(catalog.count_for_song(song), 1)
        value = snapshot()
        value['events'].append({**deepcopy(value['events'][0]), 'id':'new-show', 'date':'2025-01-01'})
        self.save(value)
        self.assertTrue(catalog.reload())
        self.assertEqual(catalog.count_for_song(song), 2)
        self.path.write_text('{broken', encoding='utf-8')
        self.assertFalse(catalog.reload())
        self.assertEqual(catalog.count_for_song(song), 2)
        value['events'][1]['songs'] = [{'title':'Different work'}]
        self.save(value)
        self.assertTrue(catalog.reload())
        self.assertEqual(catalog.count_for_song(song), 1)

    def test_match_returns_a_copy_and_preserves_source_snapshot(self):
        catalog = LiveCatalog(self.path)
        first = catalog.for_song({'title': 'ESPADA', 'series': ['million']})
        first[0]['songs'][0]['title'] = 'changed'
        self.assertEqual(catalog.events[0]['songs'][0]['title'], 'ESPADA')
        self.assertNotIn('match_basis', catalog.events[0])

    def test_events_sort_by_actual_performance_date(self):
        value = snapshot()
        older = deepcopy(value['events'][0])
        older.update(id='old', date='2022-07-09')
        value['events'].append(older)
        self.save(value)
        self.assertEqual([event['date'] for event in LiveCatalog(self.path).events], ['2024-02-25', '2022-07-09'])

    def test_invalid_reload_keeps_the_last_good_data(self):
        catalog = LiveCatalog(self.path)
        self.path.write_text('{broken', encoding='utf-8')
        self.assertFalse(catalog.reload())
        self.assertEqual(catalog.events[0]['id'], 'million-day2')
        self.assertEqual(catalog.status, 'ready')

    def test_snapshot_rejects_bad_dates_versions_and_unsafe_source_urls(self):
        for change in ({'date': '2025-02-30'}, {'date': '2024-2-5'},
                       {'source_url': 'javascript:alert(1)'}, {'series': ['invented']},
                       {'songs': []}, {'songs': [{'title': ''}]}):
            with self.subTest(change=change):
                value = snapshot()
                value['events'][0].update(change)
                self.save(value)
                self.assertIsNone(read_catalog(self.path))
        value = snapshot()
        value['schema_version'] = 2
        self.save(value)
        self.assertIsNone(read_catalog(self.path))

    def test_duplicate_event_ids_and_complete_coverage_claims_are_rejected(self):
        value = snapshot()
        value['events'] *= 2
        self.save(value)
        self.assertIsNone(read_catalog(self.path))
        value = snapshot()
        value['coverage'] = 'complete'
        self.save(value)
        self.assertIsNone(read_catalog(self.path))

    def test_atomic_writer_preserves_previous_data_on_invalid_candidate(self):
        before = self.path.read_bytes()
        bad = snapshot()
        bad['events'][0]['date'] = '2024-02-31'
        with self.assertRaises(ValueError):
            write_candidate(bad, self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_atomically_written_candidate_is_usable(self):
        value = snapshot()
        value['events'][0]['title'] = 'new official title'
        write_candidate(value, self.path)
        self.assertEqual(LiveCatalog(self.path).events[0]['title'], 'new official title')

    def test_numbered_parser_does_not_turn_prices_times_into_songs(self):
        page = BeautifulSoup('<p>1. Song A<br>2. Song B<br>16:30<br>4,900円<br>7月26日</p>', 'html.parser')
        self.assertEqual([row['title'] for row in expect(numbered(page), 2)], ['Song A', 'Song B'])

    def test_changed_source_count_fails_closed(self):
        with self.assertRaises(ValueError):
            expect([(1, {'title': 'A'}), (3, {'title': 'C'})], 2)

    def test_pylon_parser_separates_days_and_preserves_official_credits(self):
        soup = BeautifulSoup('<h3>DAY1 Set List</h3><p>01. Song A / Unit A<br>02. Song B／Singer B</p>'
                             '<h3>DAY2 Set List</h3><p>01. Another</p><h3>Information</h3>'
                             '<p>01. An album track, not performed</p>', 'html.parser')
        rows = pylon_setlist(soup, 1, 2)
        self.assertEqual([row['title'] for row in rows], ['Song A', 'Song B'])
        self.assertEqual(rows[0]['artists'], ['Unit A'])
        self.assertEqual(rows[0]['artist_basis'], 'official_performance_credit')
        self.assertEqual(pylon_setlist(soup, 2, 1)[0]['title'], 'Another')
        with self.assertRaises(ValueError):
            pylon_setlist(soup, 1, 3)

    def test_reviewed_number_formats_reject_dates_and_times(self):
        text = '2025.08.02\n16:00\n4,900円\nM01 Song A\n2 Song B\n3. Song C'
        self.assertEqual([row['title'] for row in expect(setlist_rows(text), 3)],
                         ['Song A', 'Song B', 'Song C'])

    def test_only_reviewed_stage_arrangements_receive_explicit_work_titles(self):
        known = song_row('ESPADA（Long Intro ver.）')
        self.assertEqual(known['song_title'], 'ESPADA')
        unknown = song_row('ESPADA（New Edition）')
        self.assertEqual(unknown['title'], 'ESPADA（New Edition）')
        self.assertNotIn('song_title', unknown)

    def test_typography_is_normalized_without_dropping_punctuation(self):
        self.assertEqual(title_key('Ｌｕｎａ　say maybe'), title_key('Luna say maybe'))
        self.assertEqual(title_key('I’m yours'), title_key("I'm yours"))
        self.assertEqual(title_key('組曲 –to All Ages Concerto–'), title_key('組曲 -to All Ages Concerto-'))
        self.assertNotEqual(title_key('組曲 -to All Ages Concerto-'), title_key('組曲 to All Ages Concerto'))
        self.assertNotEqual(work_key('組曲 –to All Ages Concerto– (Remix)'), work_key('組曲 -to All Ages Concerto-'))
        self.assertNotEqual(title_key('初'), title_key('初!'))

    def test_shipped_snapshot_has_all_six_brands_and_espada_official_event(self):
        catalog = LiveCatalog(RESOURCE_PATH)
        self.assertEqual(catalog.status, 'ready')
        self.assertEqual(catalog.coverage, 'partial')
        self.assertGreaterEqual(len(catalog.events), 400)
        self.assertGreaterEqual(sum(len(event['songs']) for event in catalog.events), 9500)
        self.assertTrue({'765', 'million', 'cinderella', 'sidem', 'shiny', 'gakuen'}.issubset(
            {s for event in catalog.events for s in event['series']}))
        self.assertTrue(any(event.get('official_sources') for event in catalog.events))
        self.assertTrue(any(event.get('source_kind') == 'fan_database' for event in catalog.events))
        for event in catalog.events:
            self.assertTrue(event.get('date_source_url', '').startswith('https://'))
            self.assertTrue(event.get('setlist_source_url', '').startswith('https://'))
            self.assertFalse(any('MC-' in song['title'] or 'ナレーション' in song['title'] for song in event['songs']))
        esp = catalog.for_song({'title': 'ESPADA', 'series': ['million']})
        self.assertTrue({'2026-05-06', '2024-02-25'}.issubset({event['date'] for event in esp}))
        self.assertGreaterEqual(len(catalog.for_song({'title': 'Luna say maybe', 'series': ['gakuen']})), 4)
        self.assertGreaterEqual(catalog.events[0]['date'], '2026-09-27')
        self.assertLessEqual(catalog.events[-1]['date'], '2013-02-10')
        self.assertTrue(catalog.for_song({'title': 'M@STERPIECE (M@STER VERSION)', 'series': ['765']}))

    def test_real_shipped_masterpiece_record_connects_in_both_directions(self):
        value = json.loads(RESOURCE_PATH.with_name('song_catalog.json').read_text(encoding='utf-8'))
        song = next(row for row in value['songs'] if row['id'] == 'mb:103e53b1-d910-4eee-914a-63e079876530')
        self.assertEqual(song['series'], ['765'])
        catalog = LiveCatalog(RESOURCE_PATH)
        events = catalog.for_song(song)
        self.assertTrue({'2025-08-02', '2022-07-10'}.issubset({event['date'] for event in events}))
        for event in events:
            candidates = [candidate['id'] for row in catalog.songs_for_event(event, [song])
                          for candidate in row['matches']]
            self.assertIn(song['id'], candidates)

    def test_sidem_typographic_title_connects_without_including_instrumental_version(self):
        value = json.loads(RESOURCE_PATH.with_name('song_catalog.json').read_text(encoding='utf-8'))
        identifiers = {'mb:c0ed4f45-3a16-4945-bb92-f076cb0df7b6', 'mb:e2b20d1e-5e76-4e7b-b99e-42af16e8edb6'}
        songs = [song for song in value['songs'] if song['id'] in identifiers]
        vocal = next(song for song in songs if song['kind'] == 'song')
        instrumental = next(song for song in songs if song['kind'] == 'instrumental')
        catalog = LiveCatalog(RESOURCE_PATH)
        events = catalog.for_song(vocal)
        self.assertEqual(len(events), 4)
        self.assertEqual(catalog.for_song(instrumental), [])
        for event in events:
            self.assertTrue(event['recording_version_unverified'])
            matches = [song['id'] for row in catalog.songs_for_event(event, songs) for song in row['matches']]
            self.assertEqual(matches, [vocal['id']])


if __name__ == '__main__':
    unittest.main()
