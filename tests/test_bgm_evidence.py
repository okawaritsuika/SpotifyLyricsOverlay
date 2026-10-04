from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import catalog_sources as sources
from song_catalog import SongCatalog, read_catalog, refresh_catalog
from tests.test_catalog_sources import recording
from tests.test_song_catalog import song, write_catalog

ROOT = Path(__file__).resolve().parents[1]
U149_CD = 'dc601d3a-e407-4c03-80d1-0ff2da42416e'
U149_DIGITAL = '35319e59-81da-4c92-b6a2-cb8cd36e615a'


class BgmEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reviews = json.loads(sources.RECORDING_KIND_CORRECTIONS_PATH.read_text(encoding='utf-8'))
        cls.entry = next(entry for entry in cls.reviews['entries'] if entry['release_id'] == U149_CD)
        cls.raw = {item['id']: item for item in json.loads((ROOT/'resources/song_catalog.json').read_text(encoding='utf-8'))['songs']}

    def test_u149_all_49_records_classify_from_either_verified_edition(self):
        self.assertEqual(len(self.entry['recordings']),49)
        for release_id in (U149_CD,U149_DIGITAL):
            for recording in self.entry['recordings']:
                item = deepcopy(self.raw['mb:'+recording['id']])
                item['releases'] = [{'id':release_id,'title':self.entry['release_title']}]
                self.assertEqual(sources.reclassify_reviewed_kinds([item]),1)
                self.assertEqual(item['kind'],'instrumental')
                self.assertEqual(item['kind_source_urls'],self.entry['source_urls'])

    def test_unreviewed_edition_or_recording_title_never_changes_kind(self):
        recording = self.entry['recordings'][0]
        base = deepcopy(self.raw['mb:'+recording['id']])
        for release, title in [({'id':U149_DIGITAL,'title':'Another title'},recording['title']),
                               ({'id':'11111111-2222-3333-4444-555555555555','title':self.entry['release_title']},recording['title']),
                               ({'id':U149_DIGITAL,'title':self.entry['release_title']},recording['title']+' (Vocal)')]:
            item = deepcopy(base)
            item.update(title=title,releases=[release])
            prior = deepcopy(item)
            self.assertEqual(sources.reclassify_reviewed_kinds([item]),0)
            self.assertEqual(item,prior)

    def test_malformed_alternate_release_review_fails_closed(self):
        invalid = [None,{},[{'id':U149_DIGITAL,'title':None}],
                   [{'id':U149_CD,'title':self.entry['release_title']}],
                   [{'id':U149_DIGITAL,'title':self.entry['release_title']}]*2,
                   [{'id':self.reviews['entries'][0]['release_id'],'title':'Duplicated existing release'}]]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'review.json'
            for aliases in invalid:
                data = deepcopy(self.reviews)
                next(entry for entry in data['entries'] if entry['release_id']==U149_CD)['alternate_releases'] = aliases
                path.write_text(json.dumps(data),encoding='utf-8')
                with patch.object(sources,'RECORDING_KIND_CORRECTIONS_PATH',path):
                    self.assertEqual(sources._recording_kind_reviews(),{})

    def test_vocal_bonus_partitions_remain_songs(self):
        loaded = {item['id']:item for item in SongCatalog().songs}
        selected = {'mb:'+row['id'] for row in self.entry['recordings']}
        vocals = [item for item in self.raw.values() if item['kind']=='song' and item['id'] not in selected
                  and any(release['id']==U149_CD for release in item.get('releases',[]))]
        self.assertEqual(len(vocals),33)
        self.assertTrue(all(loaded[item['id']]['kind']=='song' for item in vocals))


class PublicResponseShapeTests(unittest.TestCase):
    def test_malformed_nested_credit_data_raises_valueerror(self):
        changes = [{'artist-credit':None},{'artist-credit':[None]},
                   {'artist-credit':[{'artist':'wrong'}]}, {'artist-credit':[{'artist':None}]},
                   {'artist-credit':[{'name':[], 'artist':{}}]},
                   {'artist-credit':[{'artist':{'disambiguation':[]}}]},
                   {'releases':None},{'releases':[None]},{'releases':[{'title':[]} ]},
                   {'first-release-date':[]},{'isrcs':[None]},{'id':[]},{'title':{}}]
        for change in changes:
            with self.subTest(change=change),self.assertRaises(ValueError):
                sources.compact_mb_response({'count':1,'offset':0,'recordings':[recording(**change)]})

    def test_optional_unknown_fields_do_not_reject_a_valid_public_record(self):
        item = recording(**{'artist-credit':[{'name':None,'joinphrase':None,
            'artist':{'name':'初星学園','type':None,'disambiguation':None}}], 'first-release-date':None})
        item['releases'][0]['date'] = None
        result = sources.compact_mb_response({'count':1,'offset':0,'recordings':[item]})['recordings'][0]
        self.assertEqual(result['first-release-date'],'')
        self.assertEqual(result['artist-credit'][0]['name'],'初星学園')
        self.assertNotIn('type',result['artist-credit'][0]['artist'])
        self.assertNotIn('date',result['releases'][0])
        self.assertEqual(len(sources.normalize_musicbrainz([result])),1)

    def test_bad_musicbrainz_credit_retains_prior_source_and_updates_healthy_source(self):
        response = {'count':1,'offset':0,'recordings':[recording(**{'artist-credit':[None]})]}
        person = {'id':'idol','name':'Idol','aliases':[],'members':[],'kind':'idol','series':['765']}
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/'catalog.json'
            prior = song('prior')
            write_catalog(target,[prior])
            def collect(**kwargs):
                return sources.fetch_musicbrainz(queries=['release:test'],interval=0,
                    fetcher=lambda *a,**k:json.dumps(response).encode())
            with patch('song_catalog.fetch_musicbrainz',side_effect=collect), \
                    patch('song_catalog.parse_imasparql',return_value=([],[person])),patch.object(sources,'_pause'):
                result = refresh_catalog(target,fetcher=lambda *a,**k:b'public fixture')
            self.assertTrue(result['updated'])
            self.assertEqual(result['sources_updated'],['imasparql'])
            self.assertEqual(len(result['errors']),1)
            snapshot = read_catalog(target)
            self.assertEqual([item['id'] for item in snapshot['songs']],['prior'])
            self.assertEqual(snapshot['songs'][0]['title'],prior['title'])
            self.assertEqual(snapshot['performers'],[person])


if __name__=='__main__':
    unittest.main()
