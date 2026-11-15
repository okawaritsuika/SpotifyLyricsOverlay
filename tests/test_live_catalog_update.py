from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from live_catalog import LiveCatalog, read_catalog
from live_catalog_update import merge_collected, refresh_live_catalog, write_catalog
from tests.test_live_catalog import snapshot


class LiveCatalogUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.base = self.directory / 'base.json'
        self.update = self.directory / 'update.json'
        write_catalog(self.base, snapshot())

    def collected(self):
        event = deepcopy(snapshot()['events'][0])
        event.update(id='db-evening', source_url='https://imas-db.jp/song/event/example.html',
                     source_kind='fan_database', source_table_label='夜公演セットリスト')
        return {'events': [event], 'failures': [], 'stats': {},
                'index_urls': [event['source_url']], 'successful_urls': [event['source_url']]}

    def test_cancel_and_source_failure_keep_previous_snapshot_byte_for_byte(self):
        write_catalog(self.update, snapshot())
        before = self.update.read_bytes()
        cancelled = threading.Event();cancelled.set()
        result = refresh_live_catalog(self.update, cancel=cancelled, resource_path=self.base,
                                      collector=lambda *args, **kwargs: self.collected())
        self.assertFalse(result['updated']);self.assertTrue(result['cancelled'])
        result = refresh_live_catalog(self.update, resource_path=self.base,
                                      collector=lambda *args, **kwargs: {'events': [], 'failures': ['offline']})
        self.assertFalse(result['updated']);self.assertEqual(result['errors'], ['offline'])
        self.assertEqual(self.update.read_bytes(), before)

    def test_partial_refresh_preserves_previous_and_official_and_new_events(self):
        old = self.collected()['events'][0]
        old.update(id='older', date='2023-04-10', source_url='https://imas-db.jp/song/event/old.html')
        previous = snapshot();previous['events'] = [old]
        write_catalog(self.update, previous)
        collected = self.collected();collected['failures'] = [{'url': 'https://imas-db.jp/song/event/old.html'}]
        result = refresh_live_catalog(self.update, resource_path=self.base,
                                      collector=lambda *args, **kwargs: collected)
        self.assertTrue(result['updated']);self.assertEqual(result['events'], 3)
        self.assertEqual({event['id'] for event in read_catalog(self.update)['events']},
                         {'older', 'db-evening', 'million-day2'})
        catalog = LiveCatalog(self.base, self.update)
        self.assertEqual(len(catalog.events), 3)
        self.assertEqual(len(catalog.collection_report['failures']), 1)

    def test_invalid_update_falls_back_to_bundle(self):
        self.update.write_text('{bad', encoding='utf-8')
        self.assertEqual(LiveCatalog(self.base, self.update).events[0]['id'], 'million-day2')

    def test_new_bundle_additions_and_reviewed_local_performances_are_both_visible(self):
        previous, bundled = snapshot(), snapshot()
        previous['verified_at'] = '2026-10-02T16:00:00+09:00'
        bundled['verified_at'] = '2026-11-14T14:22:35Z'
        original = previous['events'][0]
        previous['events'].append({**original, 'id': 'local-reviewed', 'date': '2026-10-30'})
        bundled['events'][0].update(source_kind='fan_database', songs=[{'title': 'Unreviewed setlist'}])
        bundled['events'].append({**original, 'id': 'official-birthday', 'date': '2026-11-11'})
        write_catalog(self.base, bundled)
        write_catalog(self.update, previous)
        before = self.update.read_bytes()
        catalog = LiveCatalog(self.base, self.update)
        self.assertEqual([e['id'] for e in catalog.events], ['official-birthday', 'local-reviewed', 'million-day2'])
        self.assertEqual(catalog.events[-1]['songs'], original['songs'])
        self.assertEqual(catalog.collection_report['events'], 3)
        self.assertEqual(self.update.read_bytes(), before)

    def test_latest_successful_fan_import_replaces_the_whole_page(self):
        older, newer = snapshot(), snapshot()
        older['verified_at'] = '2026-10-02'
        newer['verified_at'] = '2026-11-14'
        fresh = self.collected()['events'][0]
        url = fresh['source_url']
        stale = {**fresh, 'id': 'stale-table-id'}
        older['events'].extend([stale, {**stale, 'id': 'older-independent', 'source_url': 'https://example.org/old'}])
        newer['events'].extend([fresh, {**fresh, 'id': 'newer-independent', 'source_url': 'https://example.org/new'}])
        older['collection_report'] = {'successful_urls': [url]}
        newer['collection_report'] = {'successful_urls': [url]}
        expected = {'million-day2', 'db-evening', 'older-independent', 'newer-independent'}
        for bundle, update in ((older, newer), (newer, older)):
            with self.subTest(bundle=bundle['verified_at']):
                write_catalog(self.base, bundle)
                write_catalog(self.update, update)
                self.assertEqual({e['id'] for e in LiveCatalog(self.base, self.update).events}, expected)
        newer['collection_report']['successful_urls'] = []
        write_catalog(self.base, older)
        write_catalog(self.update, newer)
        self.assertEqual({e['id'] for e in LiveCatalog(self.base, self.update).events},
                         (expected - {'db-evening'}) | {'stale-table-id'})

    def test_refresh_and_reload_retain_official_date_links_for_shared_id(self):
        original = self.collected()['events'][0]
        official_url = 'https://idolmaster-official.jp/live_event/example'
        reviewed = {**original, 'date_source_url': official_url, 'venue': 'Kアリーナ横浜',
                    'date_basis': 'official_schedule_and_source_table',
                    'official_sources': [{'name': '공연 공식 일정', 'url': official_url}]}
        base = snapshot();base['events'].append(reviewed)
        write_catalog(self.base, base)
        refresh_live_catalog(self.update, resource_path=self.base, collector=lambda *args, **kwargs: self.collected())
        for events in (read_catalog(self.update)['events'], LiveCatalog(self.base, self.update).events):
            event = next(item for item in events if item['id'] == original['id'])
            self.assertEqual(event['date_source_url'], official_url)
            self.assertEqual(event['official_sources'], reviewed['official_sources'])
            self.assertEqual(event['venue'], reviewed['venue'])

    def test_partial_refresh_keeps_newer_successful_page_over_bundled_tables_on_reload(self):
        base, collected = snapshot(), self.collected()
        fresh = collected['events'][0]
        url = fresh['source_url']
        base['events'].append({**fresh, 'id': 'stale-table-id'})
        base['collection_report'] = {'successful_urls': [url]}
        write_catalog(self.base, base)
        with patch('live_catalog_update.datetime') as clock:
            clock.now.return_value = datetime(2026, 11, 12, tzinfo=timezone.utc)
            refresh_live_catalog(self.update, resource_path=self.base, collector=lambda *args, **kwargs: collected)
            next_result = {'events': [{**fresh, 'id': 'other-fan-show', 'source_url': 'https://example.org/other'}],
                           'successful_urls': ['https://example.org/other'], 'failures': [{'url': url}]}
            clock.now.return_value = datetime(2026, 11, 14, tzinfo=timezone.utc)
            refresh_live_catalog(self.update, resource_path=self.base, collector=lambda *args, **kwargs: next_result)
        catalog = LiveCatalog(self.base, self.update)
        self.assertEqual({event['id'] for event in catalog.events}, {'db-evening', 'million-day2', 'other-fan-show'})
        self.assertEqual(catalog.collection_report['successful_url_checks'][url], '2026-11-12T00:00:00+00:00')

    def test_successful_page_replaces_old_table_ids_without_ghost_duplicates(self):
        collected=self.collected()
        old=deepcopy(collected['events'][0]);old['id']='old-table-id'
        value=merge_collected(snapshot(),{'events':[old]},collected,links=[])
        self.assertEqual({event['id'] for event in value['events']},{'million-day2','db-evening'})

    def test_only_reviewed_same_date_and_session_are_coalesced(self):
        collected = self.collected()
        day = deepcopy(collected['events'][0]);day.update(id='db-day', source_table_label='昼公演セットリスト')
        collected['events'].append(day)
        link = {'official_id': 'million-day2', 'date': day['date'],
                'database_url': day['source_url'], 'database_section': day['source_table_label']}
        value = merge_collected(snapshot(), None, collected, links=[link])
        self.assertEqual(len(value['events']), 2)
        merged = next(event for event in value['events'] if event['id'] == 'million-day2')
        self.assertEqual(merged['source_table_label'], '昼公演セットリスト')
        self.assertTrue(merged['official_sources'])
        again = merge_collected(snapshot(), value, collected, links=[link])
        self.assertEqual(len(again['events']), 2)
        self.assertEqual(next(event for event in again['events'] if event['id']=='million-day2')['official_sources'],
                         merged['official_sources'])
        link['date'] = '2025-01-01'
        self.assertEqual(len(merge_collected(snapshot(), None, collected, links=[link])['events']), 3)

    def test_explicit_song_brand_prevents_false_same_title_cross_brand_matches(self):
        value = snapshot();event = value['events'][0]
        event['series'] = ['million', 'sidem']
        event['songs'][0]['series'] = ['million']
        write_catalog(self.base, value)
        catalog = LiveCatalog(self.base)
        good = {'id':'good', 'title':'ESPADA', 'series':['million']}
        bad = {'id':'bad', 'title':'ESPADA', 'series':['sidem']}
        self.assertEqual(catalog.for_song(bad), [])
        self.assertEqual(len(catalog.for_song(good)), 1)
        self.assertEqual([song['id'] for song in catalog.songs_for_event(event, [good,bad])[0]['matches']], ['good'])

    def test_malformed_result_and_cancel_during_atomic_write_never_replace(self):
        write_catalog(self.update, snapshot());before = self.update.read_bytes()
        value = snapshot();value['events'][0]['songs'][0]['series'] = ['made-up']
        with self.assertRaises(ValueError):write_catalog(self.update, value)
        self.assertFalse(write_catalog(self.update, snapshot(), cancel=lambda: True))
        self.assertEqual(self.update.read_bytes(), before)
        self.assertEqual(sorted(path.name for path in self.directory.iterdir()), ['base.json','update.json'])

    def test_dance_only_song_stays_in_setlist_but_is_not_singing_history(self):
        value=snapshot();value['events'][0]['songs'][0]['performance_kind']='dance_without_vocals'
        write_catalog(self.base,value)
        catalog=LiveCatalog(self.base)
        song={'id':'espada','title':'ESPADA','series':['million']}
        self.assertEqual(catalog.for_song(song),[])
        self.assertEqual(catalog.songs_for_event(value['events'][0],[song])[0]['matches'],[song])


if __name__ == '__main__':unittest.main()
