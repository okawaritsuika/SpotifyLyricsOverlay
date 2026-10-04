from copy import deepcopy
import json
from pathlib import Path
import unittest
import catalog_sources as sources

ROOT=Path(__file__).resolve().parents[1]
RELEASE='7dec0fed-5e25-495d-8331-39c08805af35'

class BestBgmEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw=json.loads((ROOT/'resources/song_catalog.json').read_text(encoding='utf-8'))['songs']
        cls.entry=next(entry for entry in json.loads(sources.RECORDING_KIND_CORRECTIONS_PATH.read_text(encoding='utf-8'))['entries']
                       if entry['release_id']==RELEASE)
        cls.ids={'mb:'+row['id'] for row in cls.entry['recordings']}

    def test_exact_43_bonus_bgm_records_keep_series_dates_and_artists(self):
        self.assertEqual(len(self.ids),43)
        for original in self.raw:
            if original['id'] not in self.ids:continue
            song=deepcopy(original);sources.reclassify_reviewed_kinds([song])
            self.assertEqual(song['kind'],'instrumental')
            self.assertEqual(song['kind_source_urls'],['https://columbia.jp/prod-info/COCX-36239-40/'])
            for field in ('series','releases','artists','first_release_date'):
                self.assertEqual(song.get(field),original.get(field),field)
            self.assertEqual(song['series'],['other'])

    def test_main_disc_sixteen_vocals_are_preserved(self):
        vocals=[song for song in self.raw if song['id'] not in self.ids
                and any(release['id']==RELEASE for release in song.get('releases',[]))]
        self.assertEqual(len(vocals),16)
        before=deepcopy(vocals);sources.reclassify_reviewed_kinds(vocals)
        self.assertEqual(vocals,before)
        self.assertTrue(all(song['kind']=='song' for song in vocals))

    def test_generic_bgm_title_on_another_recording_or_edition_stays_unchanged(self):
        original=next(song for song in self.raw if song['id'] in self.ids and song['title']=='Main BGM')
        for field in ('recording_id','recording_title','release_id','release_title'):
            song=deepcopy(original)
            if field=='recording_id':song['id']='mb:00000000-0000-4000-8000-000000000001'
            elif field=='recording_title':song['title']='Main BGM (Another Version)'
            else:
                for release in song['releases']:release[field.removeprefix('release_')]+='different'
            before=deepcopy(song);sources.reclassify_reviewed_kinds([song]);self.assertEqual(song,before)

if __name__=='__main__':unittest.main()
