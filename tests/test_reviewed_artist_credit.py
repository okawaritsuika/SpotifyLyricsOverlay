from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import catalog_sources as sources
from song_catalog import SongCatalog,read_catalog
from tests.test_song_catalog import write_catalog

ROOT=Path(__file__).resolve().parents[1]
ID='mb:63dbb5f8-a6a1-4055-9493-82f67b8719b8'
RELEASE='0ec05fad-f3fa-4e08-924a-b157e0ca332e'
URLS=['https://columbia.jp/prod-info/COZX-1151-4/','https://columbia.jp/idolmaster/COZX-1151.html']

class ReviewedArtistCreditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw=json.loads((ROOT/'resources/song_catalog.json').read_text(encoding='utf-8'))
        cls.original=next(s for s in cls.raw['songs'] if s['id']==ID)
        cls.reviews=json.loads(sources.REVIEWED_RECORDING_RELEASES_PATH.read_text(encoding='utf-8'))

    def test_exact_known_wrong_credit_keeps_original_and_official_evidence(self):
        song=deepcopy(self.original)
        self.assertEqual(sources.enrich_reviewed_releases([song]),[song])
        self.assertEqual(song['artists'],['new generations'])
        self.assertEqual(song['artist_credit'],'new generations')
        self.assertEqual(song['artists_original'],['田中秀和'])
        self.assertEqual(song['artist_credit_original'],'田中秀和')
        self.assertEqual(song['artist_source_urls'],URLS)
        self.assertEqual(song['artist_source_kind'],'official_label')
        self.assertEqual(song['recordings'][0]['artists'],['new generations'])
        self.assertEqual(song['recordings'][0]['artists_original'],['田中秀和'])
        once=deepcopy(song)
        self.assertEqual(sources.enrich_reviewed_releases([song]),[])
        self.assertEqual(song,once)

    def test_all_identity_credit_and_nested_guards_preserve_other_evidence(self):
        for field,value in [('id','mb:00000000-0000-4000-8000-000000000001'),('title','Absolute NIne (new mix)'),
                            ('source_id','local'),('series',['other']),('series',['cinderella','765']),
                            ('artists',['Another singer']),('artist_credit','Another singer'),('recordings',[])]:
            song=deepcopy(self.original);song[field]=value;before=deepcopy(song)
            self.assertEqual(sources.enrich_reviewed_releases([song]),[])
            self.assertEqual(song,before,field)
        for field in ('id','title'):
            song=deepcopy(self.original)
            for release in song['releases']:release[field]+='different'
            before=deepcopy(song);sources.enrich_reviewed_releases([song]);self.assertEqual(song,before)
        for field,value in [('id','another'),('mbid','another'),('title','different'),('artists',['New singer'])]:
            song=deepcopy(self.original);song['recordings'][0][field]=value;before=deepcopy(song)
            sources.enrich_reviewed_releases([song]);self.assertEqual(song,before,field)

    def test_another_recording_of_same_title_is_unchanged(self):
        song=deepcopy(next(s for s in self.raw['songs'] if s['id']=='mb:7e36fa60-a31c-46a6-8d6b-e698fac579f2'))
        before=deepcopy(song);sources.enrich_reviewed_releases([song]);self.assertEqual(song,before)

    def test_normalize_corrects_main_and_nested_artist(self):
        song=self.original
        row={'id':ID[3:],'title':song['title'],'artist-credit':[{'name':'田中秀和','artist':{'name':'田中秀和'}}],
             'releases':[{'id':RELEASE,'title':song['releases'][0]['title'],'status':'Official'}]}
        normalized=sources.normalize_musicbrainz([row])[0]
        self.assertEqual(normalized['artists'],['new generations'])
        self.assertEqual(normalized['recordings'][0]['artists'],['new generations'])
        row['artist-credit']=[{'name':'Changed upstream singer','artist':{'name':'Changed upstream singer'}}]
        changed=sources.normalize_musicbrainz([row])[0]
        self.assertEqual(changed['artists'],['Changed upstream singer'])
        self.assertNotIn('artists_original',changed)

    def test_reload_updates_derived_aliases_only_for_the_corrected_song(self):
        path=ROOT/'resources/song_catalog.json';raw=path.read_bytes()
        with patch.object(sources,'_correct_reviewed_artists',return_value=False):
            before={song['id']:song for song in SongCatalog().songs}
        after={song['id']:song for song in SongCatalog().songs}
        changed=[identifier for identifier in after if before[identifier]!=after[identifier]]
        self.assertEqual(changed,[ID])
        self.assertIn('뉴 제너레이션즈',after[ID]['performer_aliases'])
        self.assertNotIn('田中秀和',after[ID]['performer_aliases'])
        self.assertEqual(path.read_bytes(),raw)

    def test_reviewed_artist_fact_malformed_shapes_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'reviews.json'
            for field,value in [('original_artists','田中秀和'),('artists',[None]),('artist_credit',[]),
                                ('original_artist_credit',None),('source_urls',['https://:secret@example.test']),
                                ('source_urls',['https://example.test/unreviewed'])]:
                reviews=deepcopy(self.reviews)
                row=next(e for e in reviews['entries'] if e['release_id']==RELEASE)['recordings'][0]
                row['artist_correction'][field]=value
                path.write_text(json.dumps(reviews),encoding='utf-8')
                with patch.object(sources,'REVIEWED_RECORDING_RELEASES_PATH',path):
                    self.assertEqual(sources._reviewed_recording_releases(),{},field)

    def test_snapshot_new_provenance_fields_validate_before_loading(self):
        song=deepcopy(self.original);sources.enrich_reviewed_releases([song])
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'catalog.json';write_catalog(path,[song])
            self.assertIsNotNone(read_catalog(path))
            for field,value in [('artists_original','田中秀和'),('artist_credit_original',{}),
                                ('artist_source_urls','https://example.test'),
                                ('artist_source_urls',['https://:secret@example.test']),('artist_source_kind',[])]:
                invalid=deepcopy(song);invalid[field]=value;write_catalog(path,[invalid])
                self.assertIsNone(read_catalog(path),field)
            for field,value in [('artists_original',{}),('artist_source_urls',['https://:secret@example.test']),
                                ('artist_source_kind','unverified')]:
                invalid=deepcopy(song);invalid['recordings'][0][field]=value;write_catalog(path,[invalid])
                self.assertIsNone(read_catalog(path),field)

if __name__=='__main__':unittest.main()
