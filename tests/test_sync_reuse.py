"""Provider reuse and automatic-analysis handoff without network or audio capture."""
from dataclasses import replace
import time
import unittest
from unittest.mock import patch

from lyrics import Line,Song
from test_auto_audio import FakeJob
from test_connection_flow import FakeSpotifyJob
import test_background_overlay as background


class SyncReuseTests(unittest.TestCase):
    setUp=background.BackgroundOverlayTests.setUp
    tearDown=background.BackgroundOverlayTests.tearDown
    provider=background.BackgroundOverlayTests.provider
    receive=background.BackgroundOverlayTests.receive
    translations=background.BackgroundOverlayTests.translations
    finish_merge=background.BackgroundOverlayTests.finish_merge

    def enable_fake_audio(self):
        self.app.auto_analyzer.enabled=True
        self.app.auto_analyzer.job_factory=FakeJob
        self.app.playback=replace(self.playback,playing=True,captured=time.monotonic())

    def alsong_annotations(self,korean=None):
        value=self.provider(source='ALSong',times=[15,25,35])
        value['lines']=tuple(Line(line.original,'알송 독음 '+str(index),
                                  '알송 번역 '+str(index) if korean is None else korean[index])
                             for index,line in enumerate(value['lines']))
        return value

    def test_cached_sync_skips_analysis_when_translation_and_media_events_arrive(self):
        self.receive();self.enable_fake_audio()
        self.translations()
        self.app.set_playback(replace(self.playback,title='Other Song'))
        self.app.set_playback(replace(self.playback,playing=True,captured=time.monotonic()))
        self.assertTrue(self.app.cache_hit)
        self.assertFalse(self.app.auto_analyzer.jobs)
        self.assertIn('자동 분석 생략',self.app.auto_analyzer.status)

    def test_lyricsplus_result_is_applied_saved_and_reused_without_new_lookup(self):
        self.receive(times=[None,None,None])
        value=self.provider(source='LyricsPlus',times=[11,21,31])
        with patch.object(self.app.lyricsplus_job,'receive',return_value=(self.playback.key,value)):
            self.app.receive_lyricsplus({'kind':'completed'})
        self.finish_merge()
        cached=self.app.lyric_cache.load(self.playback)
        self.assertEqual(cached['direct']['source'],'LyricsPlus')
        self.app.set_playback(replace(self.playback,title='Other Song'))
        self.app.set_playback(self.playback)
        self.assertTrue(self.app.cache_hit);self.assertEqual(self.app.direct_times(),[11,21,31])
        self.app.lyricsplus_background.set(True);self.app.online.set(True)
        with patch.object(self.app.lyricsplus_job,'observe') as observe:
            self.app.observe_lyricsplus()
        self.assertTrue(observe.call_args.kwargs['cached_sync'])

    def test_lyricsplus_uses_only_bound_originals_and_never_translations_as_reference(self):
        self.receive(times=[None,None,None]);self.translations()
        with patch.object(self.app.lyricsplus_job,'observe') as observe:
            self.app.observe_lyricsplus()
        self.assertEqual(observe.call_args.kwargs['reference'],tuple(line.original for line in self.app.display_lines))
        self.app.screen_cache_tainted=True
        with patch.object(self.app.lyricsplus_job,'observe') as observe:
            self.app.observe_lyricsplus()
        self.assertEqual(observe.call_args.kwargs['reference'],())

    def test_new_originals_schedule_lookup_while_collecting_audio_before_model_work(self):
        self.translations();self.enable_fake_audio()
        self.app.services_started=True;self.app.online.set(True)
        self.app.lyricsplus_background.set(True);self.app.sync_lookup_started=time.monotonic()
        def schedule(*args,**kwargs):self.app.lyricsplus_job.pending=True
        with patch.object(self.app.lyricsplus_job,'observe',side_effect=schedule):
            self.app.observe_auto_audio()
        job=next(iter(self.app.auto_analyzer.jobs))
        self.assertTrue(job.waiting_for_lyrics)
        self.assertIsNone(job.song)

    def test_alsong_completion_is_routed_to_its_own_provider_and_cached(self):
        self.receive(times=[None,None,None])
        value=self.provider(source='ALSong',times=[12,22,32])
        with patch.object(self.app.alsong_job,'receive',return_value=(self.playback.key,value)) as alsong, \
                patch.object(self.app.lyricsplus_job,'receive') as community:
            self.app.receive_lyricsplus({'provider':'알송'})
        self.finish_merge()
        alsong.assert_called_once();community.assert_not_called()
        self.assertEqual(self.app.lyric_cache.load(self.playback)['direct']['source'],'ALSong')

    def test_alsong_annotations_fill_raw_spotify_without_wiki_and_keep_spotify_clock(self):
        self.app.queue_provider_lyrics(self.playback.key,self.provider())
        self.finish_merge()
        originals=tuple(line.original for line in self.app.display_lines)
        spotify=self.app.direct_result
        self.assertEqual(self.app.translated_count,0)
        value=self.alsong_annotations()
        with patch.object(self.app.alsong_job,'receive',return_value=(self.playback.key,value)):
            self.app.receive_lyricsplus({'provider':'알송'})
        self.finish_merge()
        self.assertEqual(self.app.display_lines,value['lines'])
        self.assertEqual(tuple(line.original for line in self.app.song.lines),originals)
        self.assertEqual(self.app.translated_count,3)
        self.assertEqual(self.app.reading_label.cget('text'),'알송 독음 0')
        self.assertEqual(self.app.korean_label.cget('text'),'알송 번역 0')
        self.assertIs(self.app.direct_result,spotify)
        self.assertEqual(self.app.effective_times(),([10,20,30],False))
        self.assertFalse(self.app.screen_lyrics.thread.is_alive())
        self.assertEqual(self.store.library(),[])

    def test_alsong_fills_only_missing_fields_and_preserves_manual_timing_and_cursor(self):
        self.receive();texts=[line.original for line in self.app.display_lines]
        existing=(Line(texts[0],'수정한 독음',''),Line(texts[1],'','수정한 번역'),
                  Line(texts[2],'기존 독음','기존 번역'))
        self.app.receive_reader_lyrics(self.app.reader_bridge.snapshot()['track'],existing)
        timing={**self.app.timing,'times':[None,23,None],'offset':2.,
                'origins':[None,'manual',None],'manual_priority':True}
        self.app.save_current_timing(timing);self.app.timing=timing
        self.app.cursor=1;self.app.manual_index=1;self.app.follow.set(False)
        spotify=self.app.direct_result
        self.app.receive_direct_lyrics(self.playback.key,self.alsong_annotations())
        self.assertEqual(self.app.display_lines,(Line(texts[0],'수정한 독음','알송 번역 0'),
                         Line(texts[1],'알송 독음 1','수정한 번역'),existing[2]))
        self.assertIs(self.app.timing,timing)
        self.assertIs(self.app.live_lyrics.timing,timing)
        self.assertEqual((self.app.cursor,self.app.manual_index,self.app.follow.get()),(1,1,False))
        self.assertEqual(self.app.effective_times(),([10,23,30],False))
        self.assertIs(self.app.direct_result,spotify)

    def test_alsong_annotations_keep_saved_library_source_and_timing(self):
        base=Song('saved','Test Song','Singer','Album',self.provider()['lines'])
        self.app.songs=[base];self.app.select_song(base);self.receive()
        timing=self.app.timing
        self.app.receive_direct_lyrics(self.playback.key,self.alsong_annotations())
        self.assertIs(self.app.song,base)
        self.assertTrue(all(not line.korean for line in base.lines))
        self.assertTrue(all(line.korean for line in self.app.display_lines))
        self.assertIs(self.app.timing,timing)
        self.assertEqual(self.app.direct_times(),[10,20,30])
        self.app.select_song(base)
        self.assertTrue(all(line.korean for line in self.app.display_lines))

    def test_late_alsong_annotations_for_other_key_do_not_change_or_save_current_song(self):
        self.receive();before=self.app.display_lines
        with patch.object(self.app.lyric_cache,'save') as save:
            self.app.receive_direct_lyrics(replace(self.playback,title='Other Song').key,self.alsong_annotations())
        save.assert_not_called()
        self.assertEqual(self.app.display_lines,before)
        self.assertEqual(self.app.translated_count,0)

    def test_alsong_annotations_require_complete_matching_originals_even_for_same_key(self):
        self.receive();value=self.alsong_annotations();before=self.app.display_lines
        value['lines']=(value['lines'][0],Line('別の曲の言葉','잘못된 독음','잘못된 번역'),value['lines'][2])
        self.app.receive_direct_lyrics(self.playback.key,value)
        self.assertEqual(self.app.display_lines,before)
        self.assertEqual(self.app.direct_times(),[10,20,30])

    def test_alsong_annotations_cache_and_fresh_app_restore_keep_spotify_and_manual_times(self):
        self.receive()
        timing={**self.app.timing,'times':[None,23,None],'offset':1.5,
                'origins':[None,'manual',None],'manual_priority':True}
        self.app.save_current_timing(timing);self.app.timing=timing
        self.app.receive_direct_lyrics(self.playback.key,self.alsong_annotations())
        cached=self.app.lyric_cache.load(self.playback)
        self.assertEqual(cached['lines'],self.alsong_annotations()['lines'])
        self.assertEqual(cached['direct']['source'],'Spotify')
        self.assertEqual(cached['direct']['times'],[10,20,30])
        self.app.close()
        self.root=background.tk.Tk();self.root.withdraw()
        self.app=background.LyricsApp(self.root,self.store,start_services=False)
        self.app.set_playback(self.playback)
        self.assertTrue(self.app.cache_hit)
        self.assertEqual(self.app.display_lines,cached['lines'])
        self.assertEqual(self.app.direct_result[2]['source'],'Spotify')
        self.assertEqual(self.app.direct_times(),[10,20,30])
        self.assertEqual(self.app.effective_times(),([10,23,30],False))
        self.assertEqual(self.app.timing['offset'],1.5)

    def test_alsong_queries_until_all_translations_and_complete_sync_are_cached(self):
        self.receive();self.app.online.set(True);self.app.alsong_background.set(True)
        def observe():
            with patch.object(self.app.alsong_job,'observe') as alsong, \
                    patch.object(self.app.lyricsplus_job,'observe') as lyricsplus:
                self.app.observe_lyricsplus()
            return alsong.call_args.kwargs,lyricsplus.call_args.kwargs
        alsong,community=observe()
        self.assertTrue(alsong['enabled']);self.assertFalse(alsong['cached_sync'])
        self.assertTrue(community['cached_sync'])
        self.assertEqual(alsong['reference'],tuple(line.original for line in self.app.display_lines))
        self.app.receive_direct_lyrics(self.playback.key,self.alsong_annotations(korean=['알송 번역 0','','']))
        self.assertFalse(observe()[0]['cached_sync'])
        self.app.receive_direct_lyrics(self.playback.key,self.alsong_annotations())
        self.assertTrue(observe()[0]['cached_sync'])
        self.app.set_playback(replace(self.playback,title='Other Song'))
        self.app.set_playback(self.playback)
        self.assertTrue(self.app.cache_hit)
        self.assertTrue(observe()[0]['cached_sync'])
        self.app.direct_result=None
        self.assertFalse(observe()[0]['cached_sync'])

    def test_duplicate_alsong_annotation_delivery_does_not_rewrite_cache(self):
        self.receive();value=self.alsong_annotations()
        self.app.receive_direct_lyrics(self.playback.key,value)
        with patch.object(self.app.lyric_cache,'save') as save:
            self.app.receive_direct_lyrics(self.playback.key,value)
        save.assert_not_called()

    def test_direct_sync_cancels_current_automatic_job_and_rejects_late_tracking(self):
        self.translations();self.enable_fake_audio();self.app.observe_auto_audio()
        job=next(iter(self.app.auto_analyzer.jobs))
        job.playback_key=self.playback.key
        self.receive()
        self.assertTrue(job.cancelled.is_set())
        self.app.receive_auto_audio(job,{'kind':'realtime_status','state':'listening'})
        self.assertIsNone(self.app.realtime_job)
        self.assertEqual(self.app.direct_times(),[10,20,30])

    def test_public_complete_sync_cannot_fill_sparse_spotify(self):
        self.receive(times=[10,None,None])
        self.receive(source='LRCLIB',times=[11,21,31])
        self.assertEqual(self.app.direct_result[2]['source'],'Spotify')
        self.assertEqual(self.app.direct_times(),[10,None,None])
        self.receive(times=[10,None,None])
        self.assertEqual(self.app.direct_times(),[10,None,None])

    def test_complete_cache_prevents_lookup_but_incomplete_cache_keeps_lookup_available(self):
        self.receive();self.app.services_started=True;self.app.online.set(True)
        with patch.object(self.app.background_lyrics,'observe') as observe:
            self.app.set_playback(replace(self.playback,title='Other Song'))
            self.app.set_playback(self.playback)
        self.assertTrue(self.app.cache_hit)
        self.assertTrue(observe.call_args.kwargs['cached_sync'])
        self.app.direct_result=None;self.receive(times=[10,None,30])
        with patch.object(self.app.background_lyrics,'observe') as observe:
            self.app.set_playback(self.playback)
        self.assertFalse(observe.call_args.kwargs['cached_sync'])
        self.assertTrue(observe.call_args.kwargs['spotify_sync_present'])

    def test_complete_public_sync_cannot_fill_last_missing_spotify_row(self):
        texts=('青い空を見る','花を描く','明日を探す','声が響く','最後の言葉')
        self.receive(texts=texts,times=[10,20,30,40,None])
        self.assertTrue(self.app.has_direct_sync());self.assertFalse(self.app.has_complete_sync())
        self.receive(source='LRCLIB',texts=texts,times=[11,21,31,41,51])
        self.assertFalse(self.app.has_complete_sync())
        self.assertEqual(self.app.direct_result[2]['source'],'Spotify')
        self.assertEqual(self.app.direct_times(),[10,20,30,40,None])

    def test_sparse_spotify_skips_sync_only_lookup_but_keeps_translation_lookup(self):
        self.receive(times=[10,None,None])
        self.app.online.set(True);self.app.lyricsplus_background.set(True);self.app.alsong_background.set(True)
        with patch.object(self.app.lyricsplus_job,'observe') as community, patch.object(self.app.alsong_job,'observe') as alsong:
            self.app.observe_lyricsplus()
        self.assertTrue(community.call_args.kwargs['cached_sync'])
        self.assertFalse(alsong.call_args.kwargs['cached_sync'])
        self.assertTrue(alsong.call_args.kwargs['enabled'])
        self.app.receive_direct_lyrics(self.playback.key,self.alsong_annotations())
        self.assertEqual(self.app.direct_times(),[10,None,None])
        self.assertTrue(all(line.korean for line in self.app.display_lines))
        with patch.object(self.app.alsong_job,'observe') as alsong:
            self.app.observe_lyricsplus()
        self.assertTrue(alsong.call_args.kwargs['cached_sync'])

    def test_pending_spotify_holds_sync_only_lookup_until_terminal_response(self):
        self.translations();self.app.online.set(True);self.app.lyricsplus_background.set(True)
        service=self.app.background_lyrics;service.key=self.playback.key;service.spotify_pending=True
        with patch.object(self.app.lyricsplus_job,'observe') as observe:
            self.app.observe_lyricsplus()
        self.assertTrue(observe.call_args.kwargs['waiting_for_primary'])
        service.spotify_pending=False;service.spotify_state='not_found'
        with patch.object(self.app.lyricsplus_job,'observe') as observe:
            self.app.observe_lyricsplus()
        self.assertFalse(observe.call_args.kwargs['waiting_for_primary'])
        self.assertFalse(observe.call_args.kwargs['cached_sync'])

    def test_fast_public_response_does_not_cancel_same_tracks_pending_spotify_result(self):
        service=self.app.background_lyrics;service.job_factory=FakeSpotifyJob
        self.app.services_started=True;self.app.online.set(True)
        service.observe(self.playback,public_enabled=False);service.connect(visible=False)
        key=service.request_key;job=service.job
        self.receive(source='LRCLIB',times=[11,21,31])
        self.app.set_playback(self.playback)
        self.assertEqual(service.request_key,key);self.assertTrue(service.spotify_pending)
        self.app.receive_background_lyrics(job,{'kind':'lyrics','key':key,
            'lines':[{'text':line.original,'time':stamp} for line,stamp in
                     zip(self.provider()['lines'],[10,20,30])]})
        self.finish_merge()
        self.assertEqual(self.app.direct_result[2]['source'],'Spotify')
        self.assertEqual(self.app.direct_times(),[10,20,30])
        self.app.set_playback(self.playback)
        self.assertTrue(service.cached_sync);self.assertFalse(service.spotify_pending)

    def test_provider_wait_is_bounded_and_falls_back_to_audio(self):
        self.translations();self.enable_fake_audio()
        self.app.services_started=True;self.app.online.set(True)
        self.app.settings['spotify_connected']=True
        self.app.background_lyrics.spotify_pending=True
        self.app.sync_lookup_started=time.monotonic()
        self.app.observe_auto_audio()
        job=next(iter(self.app.auto_analyzer.jobs))
        self.assertTrue(job.waiting_for_lyrics)
        self.app.sync_lookup_started-=13
        self.app.observe_auto_audio()
        self.assertEqual(list(self.app.auto_analyzer.jobs),[job])
        self.assertFalse(job.waiting_for_lyrics)

    def test_saved_library_capture_does_not_miss_start_window_waiting_for_provider(self):
        song=Song('saved','Test Song','Singer','Album',self.provider()['lines'])
        self.app.songs=[song];self.app.select_song(song);self.enable_fake_audio()
        self.app.playback=replace(self.app.playback,base=1,captured=time.monotonic())
        self.app.services_started=True;self.app.online.set(True)
        self.app.settings['spotify_connected']=True
        self.app.background_lyrics.spotify_pending=True
        self.app.sync_lookup_started=time.monotonic()
        self.app.observe_auto_audio();job=next(iter(self.app.auto_analyzer.jobs))
        self.assertTrue(job.waiting_for_lyrics)
        self.app.playback=replace(self.app.playback,base=2.2,captured=time.monotonic())
        self.app.observe_auto_audio();self.assertEqual(list(self.app.auto_analyzer.jobs),[job])
        self.assertFalse(job.waiting_for_lyrics)

    def test_failed_provider_does_not_wait_for_full_grace_period(self):
        self.translations();self.enable_fake_audio()
        self.app.services_started=True;self.app.online.set(True)
        self.app.settings['spotify_connected']=True
        self.app.background_lyrics.spotify_pending=False
        self.app.background_lyrics.spotify_state='not_found'
        self.app.sync_lookup_started=time.monotonic()
        self.app.observe_auto_audio();self.assertEqual(len(self.app.auto_analyzer.jobs),1)


if __name__=='__main__':unittest.main()
