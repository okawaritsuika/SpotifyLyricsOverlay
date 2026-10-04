import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

from app import LyricsApp
from lyrics import Line
from media import Playback
from storage import Store


class PlaylistOverlayTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(self.temp.name)
        self.store.save_settings({'online':False,'auto_audio':False,'web_auto':False,
                                  'borderless':False,'use_estimate':False,'reader_mode':'chrome'})
        self.root=tk.Tk();self.root.withdraw()
        self.app=LyricsApp(self.root,self.store,start_services=False)

    def tearDown(self):
        self.app.close();self.temp.cleanup()

    def test_requested_batch_survives_spotify_exit(self):
        self.app.playlist_lyrics.value['shutdown_requested']=True
        self.app.spotify_lifecycle.set(True)
        self.app.messages.put(('spotify_exit',None))
        with patch.object(self.app,'close') as close:self.app.tick()
        close.assert_not_called()

    def test_normal_spotify_exit_still_closes_app(self):
        self.app.spotify_lifecycle.set(True)
        self.app.messages.put(('spotify_exit',None))
        with patch.object(self.app,'close') as close:self.app.tick()
        close.assert_called_once()

    def test_durable_translation_and_clock_fill_existing_unsynced_cache(self):
        playback=Playback(title='Fixture song',artist='Fixture artist',album='Album',duration=100.)
        track={'id':'a'*22,'title':playback.title,'artists':[playback.artist],'album':playback.album,'duration':100.}
        originals=(Line('青い空を見上げて','',''),Line('花が咲く季節に','',''))
        lines=(Line(originals[0].original,'','푸른 하늘을 올려다보며'),Line(originals[1].original,'','꽃이 피는 계절에'))
        provider={'source':'Spotify','provider_id':track['id'],'lines':originals,'times':[None,None]}
        self.assertTrue(self.app.lyric_cache.save(playback,originals,provider))
        self.assertTrue(self.app.playlist_lyrics.store.save(track,lines,{**provider,'times':[10.,20.]}))
        self.app.set_playback(playback)
        self.assertEqual(self.app.translated_count,2)
        self.assertEqual(self.app.direct_times(),[10.,20.])
