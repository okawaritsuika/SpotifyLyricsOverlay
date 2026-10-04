import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup

from imas_live_sources import (
    CachedClient, Cancelled, INDEX_URL, ParseError, collect, parse_dates,
    parse_event, parse_index, parse_songs,
)


def entry(dates='2026/09/12(土)', brands=('6',)):
    return {'url': INDEX_URL + 'sample.html', 'brand_ids': list(brands),
            'index_entries': [{'title': 'Sample live', 'date_text': dates}]}


def table(rows, heading='セットリスト', brand='6'):
    return f'<div class="section" data-brand-id="{brand}"><h2>{heading}</h2><table class="tracklist"><thead><tr><th>No.</th><th>楽曲</th><th>演者</th></tr></thead><tbody>{rows}</tbody></table></div>'


ROW = '<tr><td>1</td><td><a href="/song/detail/1.html">Sample Song</a></td><td>Published Cast</td></tr>'


class IndexTests(unittest.TestCase):
    def test_only_indexed_event_html_and_preserve_shared_url_days(self):
        html = '''<div class="section"><ul><li data-brand-ids="1,6">
          Concert<ul><li><a href="./sample.html">DAY1</a><small class="date">2026/9/12</small></li>
          <li><a href="./sample.html#day2">DAY2</a><small class="date">2026/9/13</small></li>
          <li><span href="./pending.html">Future</span></li></ul></li>
          <li><a href="https://example.com/song/event/x.html">Ad</a></li>
          <li><a href="/song/detail/1.html">Song</a></li></ul></div>'''
        result = parse_index(html)
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0]['index_entries']), 2)
        self.assertEqual(result[0]['brand_ids'], ['1', '6'])

    def test_empty_index_is_error_not_success(self):
        with self.assertRaises(ParseError):
            parse_index('<html>temporarily unavailable</html>')


class DateTests(unittest.TestCase):
    def test_lists_ranges_and_full_dates(self):
        self.assertEqual(parse_dates('2026/08/06(木)・13(木)・27(木)'),
                         ['2026-08-06', '2026-08-13', '2026-08-27'])
        self.assertEqual(parse_dates('2026/8/13(木)〜16(日)'),
                         ['2026-08-13', '2026-08-14', '2026-08-15', '2026-08-16'])
        self.assertEqual(parse_dates('2026/8/20 18:00, 2026/8/21 16:00'),
                         ['2026-08-20', '2026-08-21'])

    def test_no_year_inference_or_invalid_date(self):
        self.assertEqual(parse_dates('2026年公演 DAY1 9/12'), [])
        with self.assertRaises(ParseError):
            parse_dates('2026/02/30')

    def test_two_days_have_independent_setlists(self):
        html = '<h1>Live</h1><p>2026/09/12(土)・13(日) Hall</p>' + table(ROW, 'DAY1 セットリスト') + table(ROW.replace('Sample Song', 'Other Song'), 'DAY2 セットリスト')
        result = parse_event(html, entry('2026/09/12(土)・13(日)'))
        self.assertEqual([e['date'] for e in result], ['2026-09-12', '2026-09-13'])
        self.assertEqual([e['songs'][0]['title'] for e in result], ['Sample Song', 'Other Song'])
        self.assertNotEqual(result[0]['id'], result[1]['id'])

    def test_conflict_is_reviewed_not_guessed(self):
        with self.assertRaisesRegex(ParseError, 'date_conflict'):
            parse_event('<h1>Live</h1><p>2025/04/11</p>' + table(ROW), entry('2024/04/11'))

    def test_ambiguous_shared_page_does_not_copy_setlist_to_every_date(self):
        with self.assertRaisesRegex(ParseError, 'ambiguous_table_date'):
            parse_event('<h1>Live</h1><p>2026/9/12・13</p>' + table(ROW), entry('2026/9/12・13'))

    def test_named_schedule_and_repeated_dates(self):
        html = '<h1>Live</h1><div class="section"><h3>夜公演</h3><div>2026/9/12・13 開演18:00</div></div>' + table(ROW, '夜公演セットリスト')
        self.assertEqual([e['date'] for e in parse_event(html, entry('2026/9/12・13'))],
                         ['2026-09-12', '2026-09-13'])

    def test_timezone_is_not_a_second_performance(self):
        html = '<h1>Live</h1><p>中部夏時間 2026/05/15 19:20 (日本時間5/16 9:20)</p>' + table(ROW)
        result = parse_event(html, entry('中部夏時間2026/05/15 (日本時間2026/05/16)'))
        self.assertEqual([e['date'] for e in result], ['2026-05-15'])

    def test_empty_setlist_is_failure(self):
        with self.assertRaisesRegex(ParseError, 'setlist_not_published'):
            parse_event('<h1>Live</h1><p>2026/9/12</p>', entry())

    def test_tour_range_does_not_invent_interior_performance_dates(self):
        html = '<h1>Tour</h1><div class="section"><h2>Tokyo</h2><p>2026/9/15 Hall</p>' + table(ROW, 'Tokyo') + '</div>'
        result = parse_event(html, entry('2026/9/12〜2026/9/20'))
        self.assertEqual([e['date'] for e in result], ['2026-09-15'])

    def test_index_explicit_day_sessions_preserve_night_and_noon(self):
        html = '<h1>Live</h1><p>2026/9/12・13 Hall</p>' + table(ROW)
        result = parse_event(html, entry('(DAY1夜・DAY2昼) 2026/9/12・13'))
        self.assertEqual([e['date'] for e in result], ['2026-09-12', '2026-09-13'])
        self.assertTrue(result[0]['title'].endswith('DAY1夜'))
        self.assertTrue(result[1]['title'].endswith('DAY2昼'))

    def test_shared_setlist_preserves_three_named_performances(self):
        schedule = '<table><tr><td>DAY1</td><td>2026/9/12</td></tr><tr><td>DAY2 昼公演</td><td>2026/9/13</td></tr><tr><td>DAY2 夜公演</td><td>2026/9/13</td></tr></table>'
        result = parse_event('<h1>Live</h1>' + schedule + table(ROW, 'セットリスト (全公演共通)'), entry('2026/9/12・13'))
        self.assertEqual(len(result), 3)
        self.assertEqual(len({e['id'] for e in result}), 3)

    def test_numbered_sessions_select_only_their_explicit_alternative(self):
        schedule = '<li>公演(1) : 2026/9/12</li><li>公演(2) : 2026/9/13</li><li>公演(3) : 2026/9/13</li>'
        alternatives = ''.join(f'<tr><td title="公演({n}) 2曲目">({n}) 2</td><td>Choice {n}</td><td>Cast</td></tr>' for n in (1, 2, 3))
        html = '<h1>Live</h1>' + schedule + table(ROW + alternatives).replace('</table>', '</table><p class="notes">2曲目は公演ごとに異なる楽曲</p>')
        result = parse_event(html, entry('2026/9/12・13'))
        self.assertEqual([e['songs'][1]['title'] for e in result], ['Choice 1', 'Choice 2', 'Choice 3'])
        self.assertEqual([len(e['songs']) for e in result], [2, 2, 2])
        with self.assertRaisesRegex(ParseError, 'schedule_mismatch'):
            parse_event(html.replace('<li>公演(3) : 2026/9/13</li>', ''), entry('2026/9/12・13'))

    def test_reviewed_date_correction_requires_unchanged_evidence(self):
        correction = {'database_url': INDEX_URL + 'sample.html', 'event_title': 'Live',
                      'observed_index_dates': ['2026-09-12'], 'observed_page_dates': ['2026-09-11'],
                      'official_url': 'https://idolmaster-official.jp/live_event/example/',
                      'official_source_name': 'Official schedule', 'reason': 'Individually reviewed',
                      'sessions': [{'source_table_label': 'セットリスト', 'date': '2026-09-12', 'session': ''}]}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'corrections.json'
            path.write_text(json.dumps({'corrections': [correction]}), encoding='utf-8')
            with patch('imas_live_sources.DATE_CORRECTIONS_PATH', path):
                html = '<h1>Live</h1><p>2026/9/11</p>' + table(ROW)
                event = parse_event(html, entry())[0]
                self.assertEqual(event['date'], '2026-09-12')
                self.assertEqual(event['official_date_source_url'], correction['official_url'])
                self.assertEqual(event['source_kind'], 'fan_database')
                for changed in (html.replace('2026/9/11', '2026/9/10'), html.replace('<h1>Live</h1>', '<h1>Another live</h1>'), html.replace('セットリスト', 'DAY2セットリスト')):
                    with self.assertRaisesRegex(ParseError, 'evidence_changed'):
                        parse_event(changed, entry())


class SongTests(unittest.TestCase):
    def songs(self, rows):
        return parse_songs(BeautifulSoup(table(rows), 'html.parser').find('table'), ['6'])[0]

    def test_brand_badge_and_short_note_are_separate_from_work_title(self):
        rows = '''<tr><td>1</td><td><a href="/song/detail/9.html">Song</a>
          <small class="notes">(short ver.)</small><span class="visually-hidden">(</span>
          <small class="badge bg-imas-brand-shinycolors">シャニ</small><span class="visually-hidden">)</span>
          </td><td>Cast (バックダンス: Guest)</td></tr>'''
        song = self.songs(rows)[0]
        self.assertEqual(song['title'], 'Song')
        self.assertEqual(song['song_title'], 'Song')
        self.assertEqual(song['performance_title'], 'Song (short ver.)')
        self.assertEqual(song['series'], ['shiny'])
        self.assertEqual(song['artists'], ['Cast (バックダンス: Guest)'])
        self.assertEqual(song['artist_basis'], 'fan_database_performance_credit')
        self.assertTrue(song['recording_version_unverified'])

    def test_rowspan_number_and_credit_keep_medley_order(self):
        rows = '''<tr><th colspan="3">Medley</th></tr>
          <tr><td rowspan="2">1</td><td>First</td><td rowspan="2">Cast</td></tr>
          <tr><td>Second</td></tr><tr><td>2</td><td>Third</td><td>Other</td></tr>'''
        songs = self.songs(rows)
        self.assertEqual([s['title'] for s in songs], ['First', 'Second', 'Third'])
        self.assertEqual([s['artists'] for s in songs], [['Cast'], ['Cast'], ['Other']])
        self.assertEqual([s['performance_number'] for s in songs], ['1', '1', '2'])

    def test_multiple_work_links_preserve_medley_components(self):
        songs = self.songs('<tr><td>1</td><td><a href="/song/detail/1.html">A</a> / <a href="/song/detail/2.html">B</a></td><td>Cast</td></tr>')
        self.assertEqual([s['title'] for s in songs], ['A', 'B'])
        self.assertEqual([s['medley_component'] for s in songs], [1, 2])

    def test_ordered_medley_keeps_unlinked_components_and_credit_note(self):
        rows = '<tr><td>10〜12</td><td colspan="2"><span>Audience medley</span><ol><li>Unlinked A</li><li><a href="/song/detail/2.html">Linked B</a></li><li>Unlinked C</li></ol></td></tr>'
        songs = self.songs(rows)
        self.assertEqual([s['title'] for s in songs], ['Unlinked A', 'Linked B', 'Unlinked C'])
        self.assertEqual(songs[0]['note'], 'Audience medley')
        self.assertNotIn('song_source_url', songs[0])
        self.assertEqual(songs[1]['song_source_url'], 'https://imas-db.jp/song/detail/2.html')

    def test_medley_inconsistent_count_is_not_silently_accepted(self):
        with self.assertRaisesRegex(ParseError, 'medley_number_range'):
            self.songs('<tr><td>10〜12</td><td><ol><li>A</li><li>B</li></ol></td><td>Cast</td></tr>')

    def test_each_medley_component_preserves_its_own_explicit_brand(self):
        songs = self.songs('<tr><td>1〜2</td><td><ol><li>A <span class="badge bg-imas-brand-gakuen">学園</span></li><li>B <span class="badge bg-imas-brand-sidem">SideM</span></li></ol></td><td>Cast</td></tr>')
        self.assertEqual([s['title'] for s in songs], ['A', 'B'])
        self.assertEqual([s['series'] for s in songs], [['gakuen'], ['sidem']])

    def test_dance_only_source_note_is_retained(self):
        html = table(ROW).replace('</table>', '</table><div class="notes">※ 1-1曲目はメドレー(歌唱なしダンスのみ)</div>')
        songs, info = parse_songs(BeautifulSoup(html, 'html.parser').find('table'), ['6'])
        self.assertEqual(songs[0]['performance_kind'], 'dance_without_vocals')
        self.assertIn('歌唱なし', info['setlist_notes'][0])

    def test_musical_unnumbered_song_but_not_stage_direction(self):
        songs = self.songs('<tr><td></td><td><span class="notes">(ウォーク)</span></td><td>Cast</td></tr><tr><td></td><td>Musical song</td><td>Cast</td></tr>')
        self.assertEqual([s['title'] for s in songs], ['Musical song'])

    def test_instrumental_colspan_does_not_become_artist_credit(self):
        song = self.songs('<tr><td>1</td><td colspan="2">Orchestra Song</td></tr>')[0]
        self.assertNotIn('artists', song)

    def test_unknown_brand_identity_preserved(self):
        result = parse_event('<h1>Live</h1><p>2026/9/12</p>' + table(ROW, brand='10'), entry(brands=['10']))
        self.assertEqual(result[0]['series'], ['other'])
        self.assertEqual(result[0]['brand_ids'], ['10'])
        self.assertEqual(result[0]['series_names'], ['vα-liv'])
        self.assertEqual(result[0]['source_kind'], 'fan_database')


class CollectionTests(unittest.TestCase):
    def test_cache_resume_without_network(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / (hashlib.sha256(INDEX_URL.encode()).hexdigest() + '.html')
            path.write_text('<html>cached</html>', encoding='utf-8')
            client = CachedClient(temp)
            with patch.object(client.opener, 'open', side_effect=AssertionError('network')):
                self.assertEqual(client.get(INDEX_URL), '<html>cached</html>')
            self.assertEqual(client.cache_count, 1)

    def test_cancel_before_request(self):
        with tempfile.TemporaryDirectory() as temp:
            stop = threading.Event(); stop.set()
            result = collect(temp, cancel=stop)
            self.assertTrue(result['cancelled'])
            self.assertFalse(result['events'])

    def test_success_failure_and_retry_scope(self):
        index = '<div class="section"><li data-brand-ids="6"><a href="./a.html">A</a><small class="date">2026/9/12</small></li><li data-brand-ids="6"><a href="./b.html">B</a><small class="date">2026/9/12</small></li></div>'
        def get(_client, url, refresh=False):
            if url == INDEX_URL:
                return index
            if url.endswith('a.html'):
                return '<h1>A</h1><p>2026/9/12</p>' + table(ROW)
            raise OSError('network unavailable')
        with tempfile.TemporaryDirectory() as temp, patch.object(CachedClient, 'get', get):
            result = collect(temp)
            self.assertEqual(len(result['index_urls']), 2)
            self.assertEqual(result['successful_urls'], [INDEX_URL + 'a.html'])
            self.assertEqual(result['failures'][0]['url'], INDEX_URL + 'b.html')
            retried = collect(temp, retry_urls=[INDEX_URL + 'b.html', 'https://untrusted.invalid/a'])
            self.assertEqual(len(retried['failures']), 1)
            self.assertFalse(retried['successful_urls'])


if __name__ == '__main__':
    unittest.main()
