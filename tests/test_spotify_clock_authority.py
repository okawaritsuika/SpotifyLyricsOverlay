"""A complete Spotify clock suppresses AI even when document wording differs."""
from dataclasses import replace
import time
import unittest

from lyrics import Line
from test_auto_audio import FakeJob
import test_background_overlay as background


class SpotifyClockAuthorityTests(unittest.TestCase):
    setUp=background.BackgroundOverlayTests.setUp
    tearDown=background.BackgroundOverlayTests.tearDown
    provider=background.BackgroundOverlayTests.provider

    def prepare(self):
        self.source=tuple(f'原文にある固有の物語{i}番' for i in range(12))
        self.lines=tuple(Line(text,'','保存訳') for text in
                         (*self.source[:4],*(f'文書だけにある別の文章{i}' for i in range(12))))
        self.app.select_song(self.app.live_lyrics.receive(self.playback,None,self.lines))
        self.app.playback=replace(self.playback,base=0,playing=True,captured=time.monotonic())
        self.app.auto_analyzer.enabled=True;self.app.auto_analyzer.job_factory=FakeJob
        self.app.observe_auto_audio()
        self.job=next(iter(self.app.auto_analyzer.jobs));self.job.playback_key=self.playback.key
        self.app.realtime_job=self.job;self.app.realtime_state='listening'
        self.app.realtime_index=9;self.app.realtime_matched_at=time.monotonic()

    def test_complete_spotify_clock_cancels_auto_and_preserves_document_and_manual_rows(self):
        self.prepare()
        self.app.timing['times'][10]=35
        self.app.timing.setdefault('origins',[None]*len(self.lines))[10]='manual'
        self.app.receive_direct_lyrics(self.playback.key,self.provider(texts=self.source,times=list(range(10,70,5))))
        self.assertFalse(self.app.has_direct_sync())
        self.assertTrue(self.app.has_spotify_clock())
        self.assertTrue(self.job.cancelled.is_set())
        self.assertFalse(self.app.following_audio());self.assertFalse(self.app.holding_audio())
        self.assertEqual(self.app.display_lines,self.lines)
        self.assertEqual(self.app.timing['times'][10],35)
        self.assertIn('Spotify 싱크 사용',self.app.analysis_badge.cget('text'))
        self.assertIn('가사 연결 4/16',self.app.analysis_badge.cget('text'))
        self.assertIsNone(self.app.displayed_index())

    def test_stale_realtime_delivery_cannot_take_over_complete_spotify_clock(self):
        self.prepare()
        self.app.direct_result=(self.playback.key,90,self.provider(texts=self.source,times=list(range(10,70,5))))
        self.app.clear_realtime()
        self.app.receive_realtime(self.job,{'kind':'realtime_status','state':'listening'})
        self.assertIsNone(self.app.realtime_job)

    def test_screen_tracking_cannot_take_over_complete_spotify_clock(self):
        self.prepare()
        self.app.direct_result=(self.playback.key,90,self.provider(texts=self.source,times=list(range(10,70,5))))
        self.app.receive_spotify_active(self.playback.key,self.source,1)
        self.assertIsNone(self.app.spotify_current)
        self.app.spotify_current=1;self.app.spotify_observed_at=time.monotonic()
        self.assertFalse(self.app.following_spotify())

    def test_sparse_spotify_source_retains_existing_gap_analysis_policy(self):
        self.prepare()
        times=[None]*12;times[0]=10;times[3]=25
        self.app.receive_direct_lyrics(self.playback.key,self.provider(texts=self.source,times=times))
        self.assertFalse(self.app.has_spotify_clock())
        self.assertFalse(self.job.cancelled.is_set())

    def test_complete_source_from_other_provider_or_length_has_no_spotify_authority(self):
        self.prepare()
        self.app.direct_result=(self.playback.key,90,self.provider(source='LRCLIB',texts=self.source,times=list(range(10,70,5))))
        self.assertFalse(self.app.has_spotify_clock())
        self.app.direct_result=(self.playback.key,94,self.provider(texts=self.source,times=list(range(10,70,5))))
        self.assertFalse(self.app.has_spotify_clock())


if __name__=='__main__':unittest.main()
