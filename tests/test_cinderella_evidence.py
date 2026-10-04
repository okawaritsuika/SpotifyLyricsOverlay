from copy import deepcopy
import json
from pathlib import Path
import unittest

import catalog_sources as sources
from song_catalog import SongCatalog

ROOT=Path(__file__).resolve().parents[1]
RELEASE='0ec05fad-f3fa-4e08-924a-b157e0ca332e'
TV={'d5dccf20-bac0-4bf4-b711-eac3c4ae7125':('2011-11-30','2011-12-28'),
    '4e05733f-6888-4598-9c72-77b35a7dd86a':('2011-09-07','2012-02-01')}

class CinderellaEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw=json.loads((ROOT/'resources/song_catalog.json').read_text(encoding='utf-8'))
        cls.raw_by_id={song['id']:song for song in cls.raw['songs']}
        cls.kind=next(entry for entry in json.loads(sources.RECORDING_KIND_CORRECTIONS_PATH.read_text(encoding='utf-8'))['entries']
                      if entry['release_id']==RELEASE)
        cls.dates=[entry for entry in json.loads(sources.REVIEWED_RECORDING_RELEASES_PATH.read_text(encoding='utf-8'))['entries']
                   if entry['release_id'] in TV]

    def test_exact_93_bgm_facts_preserve_twelve_vocals_and_one_karaoke(self):
        self.assertEqual(len(self.kind['recordings']),93)
        songs=deepcopy(self.raw['songs'])
        sources.reclassify_reviewed_kinds(songs)
        after={song['id']:song for song in songs}
        for row in self.kind['recordings']:
            self.assertEqual(after['mb:'+row['id']]['kind'],'instrumental')
        vocal=[song for song in self.raw['songs'] if any(r['id']==RELEASE for r in song.get('releases',[]))
               and song['id'] not in {'mb:'+r['id'] for r in self.kind['recordings']}]
        self.assertEqual(sum(song['kind']=='song' for song in vocal),12)
        self.assertEqual(sum(song['kind']=='instrumental' for song in vocal),2)
        for song in vocal:self.assertEqual(after[song['id']],song)

    def test_type_b_exact_recording_is_not_confused_with_type_a(self):
        rows={row['id']:row['title'] for row in self.kind['recordings']}
        self.assertEqual(rows['127730a6-34a1-42f0-a60f-72d286ab5ca6'],"RANKO's Grimoire (M13B)")
        self.assertEqual(rows['c44de470-025d-4e6f-a351-886009c5ffb6'],"RANKO's Grimoire (M13A)")
        target=deepcopy(self.raw_by_id['mb:127730a6-34a1-42f0-a60f-72d286ab5ca6'])
        target['title']=rows['c44de470-025d-4e6f-a351-886009c5ffb6']
        original=deepcopy(target)
        sources.reclassify_reviewed_kinds([target])
        self.assertEqual(target,original)

    def test_eleven_wrong_tv_dates_keep_original_and_official_sources(self):
        self.assertEqual(sum(len(entry['recordings']) for entry in self.dates),11)
        for entry in self.dates:
            old,new=TV[entry['release_id']]
            for row in entry['recordings']:
                target=deepcopy(self.raw_by_id['mb:'+row['id']])
                self.assertEqual(target['first_release_date'],old)
                sources.enrich_reviewed_releases([target])
                self.assertEqual(target['first_release_date'],new)
                self.assertEqual(target['first_release_date_original'],old)
                self.assertEqual(target['first_release_date_source_url'],entry['date_source_url'])
                self.assertTrue(all(release['date']==new for release in target['releases']))
                once=deepcopy(target)
                sources.enrich_reviewed_releases([target])
                self.assertEqual(target,once)

    def test_tv_dates_preserve_other_recordings_and_changed_upstream_evidence(self):
        entry=self.dates[0]
        original=self.raw_by_id['mb:'+entry['recordings'][0]['id']]
        for edit in ('id','title','release_id','release_title','date','first_date','other_release'):
            target=deepcopy(original)
            if edit in ('id','title'):target[edit]+='different'
            if edit=='release_id':target['releases'][0]['id']='another-release'
            if edit=='release_title':target['releases'][0]['title']+='different'
            if edit=='date':target['releases'][0]['date']='2024-01-01'
            if edit=='first_date':target['first_release_date']='2005-01-01'
            if edit=='other_release':target['releases'].append({'id':'other','title':'Earlier','date':'2005-01-01'})
            old_first=target['first_release_date']
            sources.enrich_reviewed_releases([target])
            self.assertEqual(target['first_release_date'],old_first,edit)
            if edit=='date':self.assertEqual(target['releases'][0]['date'],'2024-01-01')

    def test_raw_snapshot_is_unchanged_by_runtime_enrichment(self):
        path=ROOT/'resources/song_catalog.json';raw=path.read_bytes()
        catalog=SongCatalog()
        self.assertEqual(len(catalog.songs),13532)
        self.assertEqual(path.read_bytes(),raw)

if __name__=='__main__':unittest.main()
