from copy import deepcopy
import json
from pathlib import Path
import unittest
import catalog_sources as sources

ROOT=Path(__file__).resolve().parents[1]
RELEASES={'5daefbb3-642e-4277-a628-7fb38bce419e':11,'d3429a17-b46d-4cac-98f9-d9d2c84e028c':10,
          'ad633ad2-4b4c-49ef-a440-9512ff0bc9a7':10,'44ecd02b-995e-4a36-b206-d07b00662109':57}

class SideMostEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw=json.loads((ROOT/'resources/song_catalog.json').read_text(encoding='utf-8'))['songs']
        cls.by_id={song['id']:song for song in cls.raw}
        cls.entries=[entry for entry in json.loads(sources.RECORDING_KIND_CORRECTIONS_PATH.read_text(encoding='utf-8'))['entries']
                     if entry['release_id'] in RELEASES]

    def test_eighty_eight_exact_cd_and_digital_bgm_records(self):
        self.assertEqual(len(self.entries),4)
        for entry in self.entries:
            self.assertEqual(len(entry['recordings']),RELEASES[entry['release_id']])
            for row in entry['recordings']:
                for edition in [{'id':entry['release_id'],'title':entry['release_title']}]+entry['alternate_releases']:
                    song=deepcopy(self.by_id['mb:'+row['id']]);song['releases']=[edition]
                    sources.reclassify_reviewed_kinds([song])
                    self.assertEqual(song['kind'],'instrumental')
                    self.assertEqual(song['kind_source_urls'],entry['source_urls'])

    def test_exact_typographic_exception_has_no_general_title_conversion(self):
        original=self.by_id['mb:905b630c-477a-4188-b2a5-bf760698854f']
        self.assertEqual(original['title'],'あれれ...?')
        for field,value in [('title','あれれ・・・？'),('id','mb:11111111-2222-3333-4444-555555555555')]:
            song=deepcopy(original);song[field]=value;before=deepcopy(song)
            sources.reclassify_reviewed_kinds([song]);self.assertEqual(song,before)

    def test_seven_vocals_and_three_offvocals_are_not_reclassified(self):
        selected={'mb:'+row['id'] for entry in self.entries for row in entry['recordings']}
        remaining=[song for song in self.raw if song['id'] not in selected
                   and any(release['id'] in RELEASES for release in song.get('releases',[]))]
        self.assertEqual(len(remaining),10)
        self.assertEqual(sum(s['kind']=='song' for s in remaining),7)
        self.assertEqual(sum(s['kind']=='instrumental' for s in remaining),3)
        before=deepcopy(remaining);sources.reclassify_reviewed_kinds(remaining)
        self.assertEqual(remaining,before)

    def test_wrong_alternate_release_identity_does_not_grant_kind(self):
        for entry in self.entries:
            original=self.by_id['mb:'+entry['recordings'][0]['id']]
            for field in ('id','title'):
                song=deepcopy(original)
                release=deepcopy(entry['alternate_releases'][0]);release[field]+='wrong'
                song['releases']=[release];before=deepcopy(song)
                sources.reclassify_reviewed_kinds([song]);self.assertEqual(song,before)

if __name__=='__main__':unittest.main()
