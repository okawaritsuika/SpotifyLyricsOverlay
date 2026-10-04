from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import unittest

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
