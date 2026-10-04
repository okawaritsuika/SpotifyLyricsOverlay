from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from bs4 import BeautifulSoup

from live_catalog import read_catalog
from live_catalog_update import refresh_live_catalog, write_catalog
from tools.build_live_gap_supplement import MAKUHARI_MILLION_TITLES, SHOWS, cinderella_event, makuhari_event, merge


def cinderella_html():
    tracks = ''.join(f'M{i}.Song {i}<br>Artist {i}（Role {i}役）<br>' for i in range(1, 25))
    return ('<div class="description">THE IDOLM@STER CINDERELLA GIRLS 5thLIVE TOUR '
            'Serendipity Parade!!! 宮城公演<br>■開催日時<br>2017年5月13日 開場16:00<br>'
            '■開催場所<br>宮城・セキスイハイムスーパーアリーナ<br>■セットリスト<br>'
            + tracks + '</div>')


def makuhari_html():
    rows = []
    for number in range(1, 31):
        title = ('READY!!(M@STER VERSION)' if number == 1 else
                 'THE IDOLM@STER(M@STER VERSION)' if number == 30 else
                 MAKUHARI_MILLION_TITLES.get(number, f'Song {number}'))
        rows.append(f'<li><h4>{title}</h4>歌：Artist A、Artist B</li>')
    return ('<h3>COXC-1071 Yokohama</h3><ol class="hopstep"><li><h4>Wrong show</h4>歌：A</li></ol>'
            '<h3>COXC-1072 Makuhari</h3><ol class="hopstep">' + ''.join(rows[:15]) + '</ol>'
            '<ol class="hopstep" start="16">' + ''.join(rows[15:]) + '</ol>')


class OfficialGapTests(unittest.TestCase):
    def test_each_cinderella_day_requires_its_own_date_and_keeps_credits(self):
        soup = BeautifulSoup(cinderella_html(), 'html.parser')
        value = cinderella_event(soup, SHOWS[0])
        self.assertEqual(value['date'], '2017-05-13')
        self.assertEqual(len(value['songs']), 24)
        self.assertEqual(value['songs'][0]['artists'], ['Artist 1（Role 1役）'])
        with self.assertRaisesRegex(ValueError, 'date changed'):
            cinderella_event(soup, SHOWS[1])

    def test_changed_cinderella_identity_sequence_or_missing_credit_is_rejected(self):
        for before, after in [('宮城公演', '大阪公演'), ('M2.Song 2', 'M3.Song 2'),
                              ('Artist 2（Role 2役）<br>', ''),
                              ('宮城・セキスイハイムスーパーアリーナ', 'Different venue')]:
            with self.subTest(before=before), self.assertRaises(ValueError):
                cinderella_event(BeautifulSoup(cinderella_html().replace(before, after), 'html.parser'), SHOWS[0])

    def test_makuhari_excludes_other_show_and_preserves_guest_song_brands(self):
        value = makuhari_event(BeautifulSoup(makuhari_html(), 'html.parser'),
                               BeautifulSoup('<p>2013年9月22日幕張イベントホール</p>', 'html.parser'))
        self.assertEqual(value['date'], '2013-09-22')
        self.assertEqual(len(value['songs']), 30)
        self.assertNotIn('Wrong show', [row['title'] for row in value['songs']])
        self.assertEqual(value['songs'][9]['series'], ['cinderella'])
        self.assertEqual(value['songs'][11]['series'], ['million'])
        self.assertEqual(value['songs'][25]['series'], ['other'])
        for number in MAKUHARI_MILLION_TITLES:
            self.assertEqual(value['songs'][number - 1]['series'], ['million'])
            self.assertIn('lantis.jp', value['songs'][number - 1]['series_source_url'])

    def test_makuhari_does_not_infer_the_other_day_or_accept_missing_tracks(self):
        good = BeautifulSoup(makuhari_html(), 'html.parser')
        with self.assertRaisesRegex(ValueError, 'date changed'):
            makuhari_event(good, BeautifulSoup('<p>2013年9月21日幕張イベントホール</p>', 'html.parser'))
        good.select('ol.hopstep')[-1]['start'] = '15'
        with self.assertRaises(ValueError):
            makuhari_event(good, BeautifulSoup('<p>2013年9月22日幕張イベントホール</p>', 'html.parser'))

    def test_candidate_merge_is_idempotent_and_preserves_prior_records_and_report(self):
        old = {'id': 'existing', 'date': '2026-09-27', 'songs': [{'title': 'A'}]}
        base = {'events': [old], 'collection_report': {'failed_pages': 6, 'successful_pages': 333}}
        supplement = {'verified_at': '2026-10-02', 'events': [
            cinderella_event(BeautifulSoup(cinderella_html(), 'html.parser'), SHOWS[0])]}
        before = deepcopy(base)
        value = merge(base, supplement)
        self.assertEqual(base, before)
        self.assertEqual(value['events'][0], old)
        self.assertEqual(len(value['events']), 2)
        self.assertEqual(value['collection_report']['failed_pages'], 6)
        self.assertEqual(merge(value, supplement), value)

    def test_automatic_refresh_preserves_official_performance_rows_and_source_evidence(self):
        official = makuhari_event(BeautifulSoup(makuhari_html(), 'html.parser'),
                                  BeautifulSoup('<p>2013年9月22日幕張イベントホール</p>', 'html.parser'))
        base = {'schema_version': 1, 'coverage': 'partial', 'verified_at': '2026-10-02',
                'events': [official]}
        fan = deepcopy(official)
        fan.update(id='new-fan-event', source_kind='fan_database',
                   source_url='https://imas-db.jp/song/event/new.html')
        collected = {'events': [fan], 'successful_urls': [fan['source_url']],
                     'failures': [{'url': 'https://imas-db.jp/song/event/8th_makuhari.html'}]}
        with tempfile.TemporaryDirectory() as directory:
            resource, update = Path(directory) / 'base.json', Path(directory) / 'update.json'
            write_catalog(resource, base)
            for attempt in range(2):
                result = refresh_live_catalog(update, resource_path=resource,
                    collector=lambda *args, **kwargs: collected)
                self.assertTrue(result['updated'])
                retained = {event['id']: event for event in read_catalog(update)['events']}
                self.assertEqual(retained[official['id']], official)
                self.assertEqual(len(retained), 2)


if __name__ == '__main__':
    unittest.main()
