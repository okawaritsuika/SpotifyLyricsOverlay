from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup

from imas_live_sources import INDEX_URL, ParseError, parse_event
from tests.test_imas_live_sources import ROW, entry, table


class OfficialSharedSetlistTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'corrections.json'
        self.html = '<h1>Live DIMENSION ARROW</h1><p>2026/9/12・13</p>' + table(ROW)
        digest = hashlib.sha256(str(BeautifulSoup(self.html, 'html.parser').select_one('table.tracklist')).encode()).hexdigest()
        official = 'https://idolmaster-official.jp/live_event/example/'
        self.correction = {
            'database_url': INDEX_URL + 'sample.html', 'event_title': 'Live DIMENSION ARROW',
            'observed_index_dates': ['2026-09-12', '2026-09-13'],
            'observed_page_dates': ['2026-09-12', '2026-09-13'],
            'official_url': official, 'official_source_name': 'Official schedule',
            'reason': 'Officially stated identical programme; fan setlist credit retained.',
            'shared_setlist_explicit': True,
            'shared_setlist_official_evidence': {
                'url': official, 'statement': 'DIMENSION ARROW全2公演は同様の内容になります。',
                'source_table_sha256': digest,
            },
            'sessions': [
                {'source_table_label': 'セットリスト', 'date': '2026-09-12', 'session': '昼公演'},
                {'source_table_label': 'セットリスト', 'date': '2026-09-13', 'session': '夜公演'},
            ],
        }

    def parse(self, correction=None, html=None):
        self.path.write_text(json.dumps({'corrections': [correction or self.correction]}), encoding='utf-8')
        with patch('imas_live_sources.DATE_CORRECTIONS_PATH', self.path):
            return parse_event(html or self.html, entry('2026/9/12・13'))

    def test_reviewed_official_statement_preserves_two_named_sessions_and_fan_attribution(self):
        result = self.parse()
        self.assertEqual([event['date'] for event in result], ['2026-09-12', '2026-09-13'])
        self.assertTrue(result[0]['title'].endswith('昼公演'))
        self.assertTrue(result[1]['title'].endswith('夜公演'))
        self.assertEqual(len({event['id'] for event in result}), 2)
        for event in result:
            self.assertEqual(event['source_kind'], 'fan_database')
            self.assertEqual(event['setlist_source_url'], INDEX_URL + 'sample.html')
            self.assertEqual(event['date_source_url'], self.correction['official_url'])
            self.assertEqual(event['date_correction']['shared_setlist_official_evidence'],
                             self.correction['shared_setlist_official_evidence'])
            self.assertEqual(event['note'], self.correction['reason'])

    def test_changed_statement_program_count_source_or_table_requires_new_review(self):
        edits = [
            ('statement', ''), ('statement', 'Another programme全2公演は同様の内容になります。'),
            ('statement', 'DIMENSION ARROW全3公演は同様の内容になります。'),
            ('url', 'https://idolmaster-official.jp/live_event/unreviewed/'),
            ('source_table_sha256', '0' * 64),
        ]
        for field, value in edits:
            correction = deepcopy(self.correction)
            correction['shared_setlist_official_evidence'][field] = value
            with self.subTest(field=field, value=value), self.assertRaisesRegex(ParseError, 'shared_setlist_not_explicit'):
                self.parse(correction)
        with self.assertRaisesRegex(ParseError, 'shared_setlist_not_explicit'):
            self.parse(html=self.html.replace('Sample Song', 'Changed song'))

    def test_official_url_or_hash_alone_never_authorizes_shared_setlist(self):
        for replacement in (None, {}, 'reviewed', True):
            correction = deepcopy(self.correction)
            correction['shared_setlist_official_evidence'] = replacement
            with self.subTest(replacement=replacement), self.assertRaisesRegex(ParseError, 'shared_setlist_not_explicit'):
                self.parse(correction)
        correction = deepcopy(self.correction)
        correction['shared_setlist_explicit'] = 'true'
        with self.assertRaisesRegex(ParseError, 'shared_setlist_not_explicit'):
            self.parse(correction)


if __name__ == '__main__':
    unittest.main()
