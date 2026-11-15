from dataclasses import replace
import unittest
import queue
import tempfile
import threading
from unittest.mock import patch
from background_lyrics import BackgroundLyrics
from catalog_display import CatalogDisplayCredits
from media import Playback
from song_catalog import SongCatalog


class CatalogDisplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        catalog=SongCatalog()
        cls.credits=CatalogDisplayCredits(catalog.songs,catalog.performers)

    def test_endless_original_album_corrects_wrong_and_incomplete_spotify_cast(self):
        for artist in ('春日未来 (CV.山崎はるか)','Wrong singer'):
            playback=Playback('ENDLESS TOUR',artist,'ENDLESS TOUR',duration=282.9)
            identity=playback.key
            self.assertEqual(self.credits.get(playback),('春日未来','最上静香','箱崎星梨花'))
            self.assertEqual(playback.key,identity)

    def test_rio_union_solo_stays_rio(self):
        playback=Playback('UNION!! (百瀬莉緒 Ver.)','百瀬莉緒 (CV.山口立花子)',
                          'THE IDOLM@STER MILLION LIVE! SPECIAL SOLO RECORDS 百瀬莉緒',duration=331.933)
        self.assertEqual(self.credits.get(playback),('百瀬莉緒',))
        self.assertEqual(self.credits.get(replace(playback,title='UNION!! (春日未来 Ver.)')),())

    def test_conflicting_album_version_and_explicit_cv_do_not_correct(self):
        playback=Playback('ENDLESS TOUR','春日未来 (CV.山崎はるか)','ENDLESS TOUR',duration=282.9)
        for changes in ({'album':'Other'},{'title':'ENDLESS TOUR (Live)'},
                        {'artist':'春日未来 (CV.Other actor)'},{'album':''}):
            self.assertEqual(self.credits.get(replace(playback,**changes)),())

    def test_unique_album_cast_is_not_lost_to_inaccurate_catalog_length(self):
        playback=Playback('ENDLESS TOUR','Wrong singer','ENDLESS TOUR',duration=300)
        self.assertEqual(self.credits.get(playback),('春日未来','最上静香','箱崎星梨花'))

    def test_only_known_solo_singer_overrides_spotify_without_album_or_length_evidence(self):
        songs=[dict(id=str(i),title='Song',album=album,duration=240,artists=['Singer A'])
               for i,album in enumerate(('Album','Reissue'))]
        credits=CatalogDisplayCredits(songs,[])
        for album in ('','Other'):
            playback=Playback('Song','Wrong singer',album,duration=0)
            self.assertEqual(credits.get(playback),('Singer A',))
            self.assertEqual(credits.get(replace(playback,title='Song (Live)')),())

    def test_ohayo_character_editions_use_catalog_cast_and_keep_versions_separate(self):
        playback=Playback('オハヨ○サンシャイン','我那覇響(CV:沼倉愛美)',
                          'PETIT IDOLM@STER Twelve Campaigns! (Vol.1 高槻やよい&やよ)',duration=199.24)
        self.assertEqual(self.credits.get(playback),('高槻やよい',))
        self.assertEqual(self.credits.get(replace(playback,album=
            'PETIT IDOLM@STER Twelve Campaigns! (Vol.2 我那覇響&ちびき)')),('我那覇響',))
        self.assertEqual(self.credits.get(replace(playback,album='Unknown')),())

    def test_same_album_ambiguous_cast_is_not_guessed(self):
        songs=[dict(id=str(i),title='Song',album='Album',duration=240,artists=[name])
               for i,name in enumerate(('Singer A','Singer B'))]
        credits=CatalogDisplayCredits(songs,[])
        self.assertEqual(credits.get(Playback('Song','Wrong singer','Album',duration=240)),())

    def test_confirmed_solo_display_precedes_cached_spotify_without_altering_its_identity(self):
        songs=[dict(id='solo',title='Song',album='Album',duration=240,artists=['Singer A'])]
        credits=CatalogDisplayCredits(songs,[])
        playback=Playback('Song','Wrong singer','Album',duration=240)
        with tempfile.TemporaryDirectory() as directory:
            service=BackgroundLyrics('.',directory,queue.Queue())
            track=dict(id='a'*22,title='Song',album='Album',duration=240,artists=['Wrong singer'])
            self.assertTrue(service.artist_metadata.put(playback,track))
            with patch.object(service,'_catalog_display_artists',side_effect=credits.get):
                self.assertEqual(service.display_artists(playback),('Singer A',))
            self.assertEqual(service.artist_metadata.get(playback),('Wrong singer',))
            self.assertEqual(playback.artist,'Wrong singer')

    def test_deferred_catalog_uses_verified_metadata_until_index_is_ready(self):
        playback=Playback('Song','Cached Singer','Album',duration=240)
        songs=[dict(id='solo',title='Song',album='Album',duration=240,artists=['Catalog Singer'])]
        credits=CatalogDisplayCredits(songs,[])
        started=threading.Event();release=threading.Event()

        def load():
            started.set()
            release.wait(5)
            return credits

        with tempfile.TemporaryDirectory() as directory:
            messages=queue.Queue()
            service=BackgroundLyrics('.',directory,messages,defer_catalog=True)
            track=dict(id='a'*22,title='Song',album='Album',duration=240,artists=['Cached Singer'])
            self.assertTrue(service.artist_metadata.put(playback,track))
            with patch.object(service,'_read_catalog_credits',side_effect=load) as reader:
                try:
                    self.assertEqual(service.display_artists(playback),('Cached Singer',))
                    self.assertTrue(started.wait(5))
                    self.assertEqual(service.display_artists(playback),('Cached Singer',))
                    self.assertTrue(messages.empty())
                finally:
                    release.set()
                kind,value=messages.get(timeout=5)
                self.assertEqual(kind,'artist_catalog_ready')
                self.assertTrue(service.accept_artist_catalog(value))
                self.assertEqual(service.display_artists(playback),('Catalog Singer',))
                reader.assert_called_once()

    def test_deferred_catalog_reloads_if_updated_before_adoption(self):
        playback=Playback('Song','Original Singer','Album',duration=240)
        with tempfile.TemporaryDirectory() as directory:
            messages=queue.Queue()
            service=BackgroundLyrics('.',directory,messages,defer_catalog=True)
            initial=CatalogDisplayCredits([],[])
            updated=CatalogDisplayCredits([dict(id='solo',title='Song',album='Album',duration=240,
                                                 artists=['Updated Singer'])],[])
            with patch.object(service,'_read_catalog_credits',side_effect=[initial,updated]) as reader:
                self.assertEqual(service.display_artists(playback),())
                _,value=messages.get(timeout=5)
                service.artist_catalog_paths[1].write_text('{}',encoding='utf-8')
                self.assertFalse(service.accept_artist_catalog(value))
                self.assertEqual(service.display_artists(playback),())
                _,value=messages.get(timeout=5)
                self.assertTrue(service.accept_artist_catalog(value))
                self.assertEqual(service.display_artists(playback),('Updated Singer',))
                self.assertEqual(reader.call_count,2)


if __name__=='__main__':unittest.main()
