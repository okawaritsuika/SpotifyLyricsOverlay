"""Related recording reuse through the real overlay, without network services."""
from dataclasses import replace
import hashlib
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from lyrics import Song
from test_auto_audio import FakeJob
import test_background_overlay as background


class RelatedCacheUITests(unittest.TestCase):
    setUp=background.BackgroundOverlayTests.setUp
    tearDown=background.BackgroundOverlayTests.tearDown
    provider=background.BackgroundOverlayTests.provider
    receive=background.BackgroundOverlayTests.receive

    def test_same_recording_on_another_album_restores_lyrics_and_clock(self):
        self.receive()
        target=replace(self.playback,album='Compilation',duration=91)
        self.app.set_playback(target)
        self.assertTrue(self.app.cache_hit)
        self.assertEqual(tuple(line.original for line in self.app.display_lines),background.TEXTS)
        self.assertEqual(self.app.direct_times(),[10,20,30])
        self.assertFalse(self.app.has_spotify_timing())
        self.assertIsNotNone(self.app.cache_reuse)

    def test_game_suffix_can_reuse_same_length_recording(self):
        self.receive()
        self.app.set_playback(replace(self.playback,title='Test Song - GAME VERSION',album='Game'))
        self.assertTrue(self.app.cache_hit)
        self.assertEqual(self.app.direct_times(),[10,20,30])

    def test_near_length_restores_text_without_borrowing_timestamps(self):
        self.receive()
        self.app.set_playback(replace(self.playback,album='Other release',duration=94))
        self.assertTrue(self.app.cache_hit)
        self.assertFalse(self.app.has_direct_sync())
        self.assertEqual(self.app.timing['times'],[None,None,None])

    def test_thirty_second_difference_reuses_text_but_reanalyzes_the_clock(self):
        self.receive()
        self.app.set_playback(replace(self.playback,album='Other release',duration=120))
        self.assertTrue(self.app.cache_hit)
        self.assertFalse(self.app.has_direct_sync())
        self.assertIsNone(self.app.cache_tracking)
        self.assertEqual(self.app.timing['times'],[None,None,None])

    def test_saved_audio_clock_is_reused_and_existing_target_correction_wins(self):
        self.receive(times=[None,None,None])
        saved={**self.app.timing,'times':[11,21,31],'origins':['audio']*3,'audio_finalized':True}
        self.app.save_current_timing(saved)
        target=replace(self.playback,album='Compilation',duration=91)
        identifier='memory-'+hashlib.sha256(target.key.encode()).hexdigest()[:24]
        song=Song(identifier,target.title,target.artist,target.album,self.app.song.lines)
        existing={**saved,'times':[None,24,None],'origins':[None,'manual',None]}
        self.app.live_lyrics.timing_store.save(song,existing)
        self.app.set_playback(target)
        self.assertEqual(self.app.timing['times'],existing['times'])

    def test_saved_audio_clock_is_borrowed_without_copying_manual_or_audio_ownership(self):
        self.receive(times=[None,None,None])
        saved={**self.app.timing,'times':[11,21,31],'origins':['audio']*3,'audio_finalized':True,'offset':.5}
        self.app.save_current_timing(saved)
        self.app.set_playback(replace(self.playback,album='Compilation',duration=91))
        self.assertEqual(self.app.direct_times(),[11.5,21.5,31.5])
        self.assertEqual(self.app.timing['times'],[None,None,None])
        self.app.cache_current_lyrics()
        cached=self.app.lyric_cache.load(self.app.playback)
        self.assertEqual(cached['reuse']['source_duration'],90)
        self.assertEqual(cached['direct']['times'],[11.5,21.5,31.5])

    def test_related_clock_cannot_drift_via_successive_cached_releases(self):
        self.receive()
        first=replace(self.playback,album='Compilation',duration=92)
        self.app.set_playback(first);self.app.cache_current_lyrics()
        self.app.set_playback(replace(first,album='Compilation Two',duration=94))
        self.assertTrue(self.app.cache_hit)
        self.assertFalse(self.app.has_direct_sync())
        self.assertEqual(self.app.timing['times'],[None,None,None])

    def test_game_cut_with_large_length_difference_does_not_reuse_full_clock(self):
        self.receive()
        self.app.set_playback(replace(self.playback,title='Test Song - GAME VERSION',duration=45))
        self.assertFalse(self.app.cache_hit)
        self.assertFalse(self.app.has_direct_sync())

    def test_lyricless_capture_status_never_uses_a_missing_song_signature(self):
        self.receive(times=[None,None,None])
        job=SimpleNamespace(song=None,cancelled=threading.Event(),playback_key=self.playback.key)
        self.app.receive_realtime(job,{'kind':'realtime_status','state':'loading'})
        self.assertIsNone(self.app.realtime_job)

    def test_other_singer_clock_is_a_provisional_reference_and_live_search_hint(self):
        lines=self.provider()['lines']
        target=replace(self.playback,artist='Other credited singer',album='Solo release',
                       base=24,playing=True,captured=time.monotonic())
        cached={'lines':lines,'direct':None,'timing':None,'tracking_times':[5,15,25],
                'reuse':{'source_key':self.playback.key,'source_duration':90,
                         'timing_compatible':False,'cross_artist':True},'saved_at':1}
        self.app.auto_analyzer.enabled=True;self.app.auto_analyzer.job_factory=FakeJob
        with patch.object(self.app.lyric_cache,'load_related',return_value=cached):
            self.app.set_playback(target)
        job=next(iter(self.app.auto_analyzer.jobs))
        self.assertFalse(job.waiting_for_lyrics)
        self.assertEqual(job.bound_start_index,1)
        self.assertEqual(self.app.timing['times'],[None,None,None])
        self.assertFalse(self.app.has_direct_sync())
        self.assertIsNone(self.app.realtime_index)
        self.assertIn('source_duration',self.app.cache_reuse)
        self.assertEqual(self.app.effective_times()[0],[5,15,25])
        self.assertEqual(self.app.time_sources,['참고 싱크']*3)
        self.assertFalse(self.app.has_complete_sync())

    def test_exact_text_cache_does_not_hide_same_recording_clock(self):
        self.receive()
        target=replace(self.playback,album='Compilation')
        self.assertTrue(self.app.lyric_cache.save(target,self.app.display_lines))
        self.app.set_playback(target)
        self.assertEqual(self.app.direct_times(),[10,20,30])
        self.assertTrue(self.app.has_direct_sync())
        self.assertEqual(self.app.timing['times'],[None,None,None])

    def test_exact_solo_cache_uses_reference_and_keeps_manual_times(self):
        self.receive()
        lines=self.app.display_lines
        target=replace(self.playback,artist='Other credited singer',album='Solo release')
        self.assertTrue(self.app.lyric_cache.save(target,lines))
        identifier='memory-'+hashlib.sha256(target.key.encode()).hexdigest()[:24]
        song=Song(identifier,target.title,target.artist,target.album,lines)
        self.app.live_lyrics.timing_store.save(song,{'times':[None,22,None],
            'origins':[None,'manual',None],'offset':0})
        with patch('recording_reuse._work_evidence',return_value=True):
            self.app.set_playback(target)
        self.assertEqual(self.app.effective_times()[0],[10,22,30])
        self.assertEqual(self.app.time_sources,['참고 싱크','수동','참고 싱크'])
        self.assertEqual(self.app.timing['times'],[None,22,None])
        self.assertFalse(self.app.has_complete_sync())
        self.app.render()
        self.assertIn('참고 싱크',self.app.playback_badge.cget('text'))
        self.app.receive_direct_lyrics(target.key,self.provider(times=[11,21,31]))
        self.assertEqual(self.app.effective_times()[0],[11,22,31])
        self.assertNotIn('참고 싱크',self.app.time_sources)

    def test_text_only_cache_does_not_hide_playlist_reference(self):
        self.receive(times=[None,None,None])
        lines=self.app.display_lines
        target=replace(self.playback,artist='Other credited singer',album='Solo release')
        track={'id':'a'*22,'title':self.playback.title,'artists':[self.playback.artist],
               'album':'Collected album','duration':self.playback.duration}
        self.assertTrue(self.app.playlist_lyrics.store.save(track,lines,
            {**self.provider(),'provider_id':track['id']}))
        self.assertTrue(self.app.lyric_cache.save(target,lines))
        with patch('recording_reuse._work_evidence',return_value=True):
            self.app.set_playback(target)
        self.assertEqual(self.app.effective_times()[0],[10,20,30])
        self.assertFalse(self.app.has_direct_sync())


if __name__=='__main__':unittest.main()
