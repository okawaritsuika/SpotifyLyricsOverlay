"""Offline regressions for cache restoration across edited text and UIA input."""
from dataclasses import replace
import tkinter as tk
import unittest

from app import LyricsApp
from lyrics import Line,Song
import test_background_overlay as background

TEXTS=background.TEXTS


class CacheRegressionTests(unittest.TestCase):
    setUp=background.BackgroundOverlayTests.setUp
    tearDown=background.BackgroundOverlayTests.tearDown
    provider=background.BackgroundOverlayTests.provider
    receive=background.BackgroundOverlayTests.receive
    position=background.BackgroundOverlayTests.position
    translations=background.BackgroundOverlayTests.translations

    def restart(self):
        self.app.close()
        self.root=tk.Tk();self.root.withdraw()
        self.app=LyricsApp(self.root,self.store,start_services=False)
        self.app.set_playback(self.playback)

    def test_restart_preserves_manual_corrections_after_cached_lines_are_split(self):
        self.receive();self.app.offset(2);self.position(26)
        self.app.manual_index=1;self.app.follow.set(False);self.app.align_now()
        self.translations(('青い空','を見る',TEXTS[1],TEXTS[2]))
        before=self.app.timing
        self.assertEqual(before['times'],[None,None,24,None])
        self.assertEqual(before['offset'],2)
        self.restart()
        self.assertTrue(self.app.cache_hit)
        self.assertEqual(self.app.timing['times'],before['times'])
        self.assertEqual(self.app.timing['offset'],before['offset'])

    def test_edited_library_translation_takes_precedence_over_an_older_cache(self):
        original=Song('saved','Test Song','Singer','Album',tuple(Line(text,'','old translation') for text in TEXTS))
        self.store.save_library([original]);self.app.songs=[original];self.app.select_song(original)
        self.receive()
        self.assertEqual(self.app.lyric_cache.load(self.playback)['lines'][0].korean,'old translation')
        edited=replace(original,lines=tuple(Line(text,'','corrected translation') for text in TEXTS))
        self.store.save_library([edited])
        self.restart()
        self.assertTrue(self.app.cache_hit)
        self.assertEqual(self.app.display_lines[0].korean,'corrected translation')
        self.assertEqual(self.app.direct_times(),[10,20,30])

    def test_unverified_uia_completion_is_not_persisted_with_a_verified_api_result(self):
        saved=Song('saved','Test Song','Singer','Album',tuple(Line(TEXTS[index],'','saved translation') for index in (0,2)))
        self.store.save_library([saved]);self.app.songs=[saved];self.app.select_song(saved)
        stale='前の曲だけの誤った追加行'
        supplied=(Line(TEXTS[0],'',''),Line(stale,'',''),Line(TEXTS[2],'',''))
        self.app.messages.put(('spotify_lyrics',(self.playback.key,supplied)));self.app.tick()
        self.assertIn(stale,[line.original for line in self.app.song.lines])
        self.receive(texts=(TEXTS[0],TEXTS[2]),times=[10,30])
        cached=self.app.lyric_cache.load(self.playback)
        self.assertNotIn(stale,[] if cached is None else [line.original for line in cached['lines']])

    def test_restart_restores_translation_for_api_rows_missing_from_the_saved_library(self):
        saved=Song('saved','Test Song','Singer','Album',tuple(Line(TEXTS[index],'','saved translation') for index in (0,2)))
        self.store.save_library([saved]);self.app.songs=[saved];self.app.select_song(saved)
        self.receive();self.translations()
        self.assertEqual(self.app.display_lines[1].korean,'번역 1')
        self.assertEqual(self.app.lyric_cache.load(self.playback)['lines'][1].korean,'번역 1')
        self.restart()
        self.assertTrue(self.app.cache_hit)
        self.assertEqual(tuple(line.original for line in self.app.display_lines),TEXTS)
        self.assertEqual(self.app.display_lines[1].korean,'번역 1')
        self.assertEqual(self.app.direct_times(),[10,20,30])


if __name__=='__main__':unittest.main()
