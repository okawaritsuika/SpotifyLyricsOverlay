import tempfile
import threading
from time import monotonic, sleep
import tkinter as tk
import unittest
from unittest.mock import patch

from app import LyricsApp
from lyric_merge import merge_lyrics, snapshot_from_direct
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

    def assert_first_paint_releases_lookups(self, lines):
        playback=Playback('Initial track','Singer','Album',duration=100.)
        if lines:self.assertTrue(self.app.lyric_cache.save(playback,lines))
        self.app.services_started=True
        self.app.initial_playback_paint_pending=True
        self.app.background_lyrics.defer_catalog=True
        self.app.background_lyrics.artist_catalog_held=True
        self.app.workspace.overlay_requested=self.app.workspace.ready=True
        events=[]

        def publish(_position):
            events.append(('publish',len(self.app.display_lines)))

        start_thread=threading.Thread.start

        def start(worker):
            if worker.name in ('artist-catalog','saved-lyric-supplements'):
                events.append((worker.name,len(self.app.display_lines)))
            else:start_thread(worker)

        with patch.object(threading.Thread,'start',start), \
                patch.object(self.app.workspace,'publish_developer_playback',side_effect=publish), \
                patch.object(self.app.background_lyrics,'observe'), \
                patch.object(self.app,'observe_lyricsplus'):
            self.app.set_playback(playback)
            self.assertTrue(self.app.initial_playback_paint_pending)
            self.assertTrue(all(event[0]=='publish' for event in events))
            self.app.render()
            deadline=monotonic()+2
            while self.app.initial_playback_paint_pending and monotonic()<deadline:
                self.root.update()
                sleep(.01)

        self.assertFalse(self.app.initial_playback_paint_pending)
        self.assertIsNone(self.app.related_cache_pending)
        rows=len(lines)
        self.assertIn(('publish',rows),events)
        for worker in ('artist-catalog','saved-lyric-supplements'):
            self.assertIn((worker,rows),events)
            self.assertLess(events.index(('publish',rows)),events.index((worker,rows)))

    def test_initial_cached_lyrics_publish_before_background_lookups(self):
        self.assert_first_paint_releases_lookups((Line('First saved line','','번역'),))

    def test_initial_cache_miss_releases_background_lookups(self):
        self.assert_first_paint_releases_lookups(())

    def test_exact_lyrics_display_while_related_lookup_is_still_blocked(self):
        playback=Playback(title='Fixture song',artist='Fixture artist',album='Album',duration=100.)
        lines=(Line('青い空を見上げて','','푸른 하늘'),Line('花が咲く季節に','','꽃이 피는 계절'))
        self.app.lyric_cache.save(playback,lines)
        started=threading.Event();release=threading.Event();finished=threading.Event()
        def slow_lookup(*args):
            started.set()
            release.wait(5)
            finished.set()
            return [],None
        self.app.services_started=True
        try:
            with patch.object(self.app,'load_related_lyrics',side_effect=slow_lookup), \
                    patch.object(self.app.background_lyrics,'observe'), \
                    patch.object(self.app,'observe_lyricsplus'):
                self.app.set_playback(playback)
                self.assertTrue(started.wait(1))
                self.assertFalse(finished.is_set())
                self.assertEqual(self.app.display_lines,lines)
                self.assertEqual(self.app.original_label.cget('text'),lines[0].original)
                request=self.app.related_cache_request
                self.app.set_playback(Playback('Other','Singer','Album',duration=80))
                with patch.object(self.app,'restore_cached_lyrics') as restore:
                    self.app.receive_related_lyrics(request,playback,([{'lines':lines}],None))
                    restore.assert_not_called()
        finally:
            release.set()
            finished.wait(2)
            self.app.services_started=False

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

    def test_saved_playlist_restores_lines_from_collapsed_lyricsplus_cache(self):
        playback=Playback(title='Fixture song',artist='Fixture artist',album='Album',duration=100.)
        track={'id':'a'*22,'title':playback.title,'artists':[playback.artist],'album':playback.album,'duration':100.}
        lines=(Line('青い空を見上げて','','푸른 하늘을 올려다보며'),Line('花が咲く季節に','','꽃이 피는 계절에'))
        collapsed=(Line(' '.join(line.original for line in lines),'',''),)
        direct={'source':'LyricsPlus','provider_id':None,'lines':collapsed,'times':[0.]}
        merged=merge_lyrics(playback,[snapshot_from_direct(direct)])
        self.assertTrue(self.app.lyric_cache.save(playback,collapsed,direct,merge=merged))
        self.assertTrue(self.app.playlist_lyrics.store.save(track,lines,None))

        self.app.set_playback(playback)

        self.assertEqual(self.app.display_lines,lines)
        self.assertEqual(self.app.original_label.cget('text'),lines[0].original)
        self.assertFalse(self.app.has_direct_sync())
        self.assertFalse(any(source=='lyricsplus' for source,_ in self.app.lyric_merge_job.sources))
