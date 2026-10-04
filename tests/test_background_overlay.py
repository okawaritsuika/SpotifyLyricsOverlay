"""Screen-free lyrics through the real Tk overlay with every service stopped."""
from dataclasses import replace
import tempfile
import threading
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app import LyricsApp
from lyrics import Line
from media import Playback
from storage import Store


TEXTS = ('青い空を見る', '花を描く', '最後の言葉')


class BackgroundOverlayTests(unittest.TestCase):
    def test_shifted_display_rows_preserve_manual_time_and_original_cached_provider_clock(self):
        texts=('ab','cdef','sunrise')
        self.translations(texts)
        timing={**self.app.timing,'times':[None,18,None],
                'origins':[None,'manual',None],'offset':.5}
        self.app.timing=timing;self.app.live_lyrics.timing=timing
        self.app.save_current_timing(timing)
        self.receive(texts=('abcd','ef','sunrise'),times=[10,20,30])
        self.assertEqual(self.app.direct_times(),[10,None,30])
        self.assertEqual(self.app.timing['times'],[None,18,None])
        self.assertEqual([line.original for line in self.app.display_lines],list(texts))
        cached=self.app.lyric_cache.load(self.playback)
        self.assertEqual(cached['direct']['times'],[10,20,30])
        self.assertEqual([line.original for line in cached['lines']],list(texts))

    def test_short_edit_never_replaces_verified_spotify_with_a_longer_wiki(self):
        self.playback=replace(self.playback,title='Synthetic Song - Game Size')
        self.app.set_playback(self.playback)
        self.receive()
        before=self.app.display_lines;clock=self.app.direct_result;timing=self.app.timing
        self.translations((*TEXTS,'光の向こうへ進む','雨の足音を聞く'))
        self.assertEqual(self.app.display_lines,before)
        self.assertIs(self.app.direct_result,clock)
        self.assertIs(self.app.timing,timing)
        self.assertEqual(self.app.reader_bridge.snapshot()['receipt']['status'],'display_failed')
        self.translations()
        self.assertEqual(self.app.direct_times(),[10,20,30])
        self.assertEqual(self.app.translated_count,3)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.store.save_settings({'online': False, 'auto_audio': False, 'web_auto': False,
                                  'borderless': False, 'use_estimate': False, 'reader_mode': 'chrome'})
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = LyricsApp(self.root, self.store, start_services=False)
        self.playback = Playback('Test Song', 'Singer', 'Album', 12, 90, False, time.monotonic())
        self.app.set_playback(self.playback)

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def provider(self, source='Spotify', times=None, texts=TEXTS):
        return {'source': source, 'lines': tuple(Line(text, '', '') for text in texts),
                'times': [10, 20, 30] if times is None else times, 'provider_id': 'track123'}

    def receive(self, **kwargs):
        self.app.receive_direct_lyrics(self.playback.key, self.provider(**kwargs))

    def position(self, position, playing=False, captured=None):
        self.playback = replace(self.playback, base=position, playing=playing,
                                captured=time.monotonic() if captured is None else captured)
        self.app.set_playback(self.playback)
        self.app.render()

    def translations(self, texts=TEXTS):
        lines = tuple(Line(text, '', '번역 ' + str(index)) for index, text in enumerate(texts))
        self.app.receive_reader_lyrics(self.app.reader_bridge.snapshot()['track'], lines)

    def test_raw_api_lyrics_render_and_follow_clock_without_screen_monitor(self):
        self.receive()
        self.assertFalse(self.app.screen_lyrics.thread.is_alive())
        self.assertFalse(self.app.screen_lyrics.needed)
        self.assertEqual(self.app.original_label.cget('text'), TEXTS[0])
        self.assertEqual(self.app.direct_times(), [10, 20, 30])
        self.assertEqual(self.app.effective_times(), ([10, 20, 30], False))
        self.assertIn('Spotify 싱크', self.app.mode_label.cget('text'))
        self.position(25)
        self.assertEqual(self.app.displayed_index(), 1)
        self.assertEqual(self.app.original_label.cget('text'), TEXTS[1])

    def test_sentence_translation_stays_visible_across_spotify_line_splits(self):
        texts=('青い空を','見ながら歩こう','花を描く')
        self.receive(texts=texts)
        old_direct=self.app.direct_result
        old_times=self.app.direct_times()[:]
        before_timing=dict(self.app.timing)
        incoming=(Line('青い空を見ながら歩こう','あおいそらをみながらあるこう','파란 하늘을 보며 걸어가자'),
                  Line(texts[2],'はなをえがく','꽃을 그리자'))
        self.app.receive_direct_lyrics(self.playback.key,
            {'source':'ALSong','annotation_only':True,'lines':incoming,'times':[1,2]})
        self.assertEqual([line.original for line in self.app.display_lines],list(texts))
        self.assertEqual([line.reading for line in self.app.display_lines],['','','はなをえがく'])
        self.assertEqual(self.app.timing,before_timing)
        self.assertIs(self.app.direct_result,old_direct)
        self.assertEqual(self.app.direct_times(),old_times)
        for seconds,index,meaning in ((11,0,'파란 하늘을 보며 걸어가자'),
                                      (21,1,'파란 하늘을 보며 걸어가자'),(31,2,'꽃을 그리자')):
            self.position(seconds)
            self.assertEqual(self.app.displayed_index(),index)
            self.assertEqual(self.app.korean_label.cget('text'),meaning)
        cached=self.app.lyric_cache.load(self.playback)
        self.assertEqual(cached['lines'],self.app.display_lines)
        self.assertEqual(cached['direct']['times'],old_times)
        before=self.app.display_lines
        self.receive(texts=texts)  # A later raw Spotify response contains no meaning.
        self.assertEqual(self.app.display_lines,before)
        self.app.close();self.root=tk.Tk();self.root.withdraw()
        self.app=LyricsApp(self.root,self.store,start_services=False)
        self.app.set_playback(self.playback)
        self.assertEqual(self.app.display_lines,before)
        self.assertEqual(self.app.direct_times(),old_times)
        for seconds in (11,21):
            self.position(seconds)
            self.assertEqual(self.app.korean_label.cget('text'),'파란 하늘을 보며 걸어가자')

    def test_cached_originals_do_not_wait_past_the_audio_capture_start(self):
        self.receive(times=[None]*3)
        self.app.services_started=True
        self.app.online.set(True)
        self.app.settings['spotify_connected']=True
        self.app.background_lyrics.spotify_pending=True
        self.app.sync_lookup_started=time.monotonic()
        try:
            with patch.object(self.app,'observe_lyricsplus'),patch.object(self.app.auto_analyzer,'observe') as observe:
                for cached,position,waiting in ((True,.5,False),(False,.5,True),
                                                 (False,2.1,False),(False,3,False),
                                                 (False,12,True),(True,12,False)):
                    self.app.cache_hit=cached
                    self.app.playback=replace(self.playback,base=position,playing=False)
                    self.app.observe_auto_audio()
                    self.assertEqual(observe.call_args.kwargs['awaiting_sync'],waiting)
        finally:self.app.services_started=False

    def test_late_wiki_grouping_keeps_active_capture_and_accepts_its_result(self):
        from test_auto_audio import FakeJob
        texts=('青い空を','見ながら歩こう','花を描く')
        self.receive(texts=texts,times=[None]*3)
        self.app.auto_analyzer.enabled=True
        self.app.auto_analyzer.job_factory=FakeJob
        self.position(.5,playing=True)
        job=next(iter(self.app.auto_analyzer.jobs))
        signature=job.analysis_key[1]
        self.translations(('青い空を見ながら歩こう','花を描く'))
        self.assertEqual([line.original for line in self.app.display_lines],list(texts))
        self.assertEqual([line.korean for line in self.app.display_lines],['번역 0','번역 0','번역 1'])
        self.assertEqual(self.app.auto_analyzer.signature(self.app.song,True),signature)
        self.assertEqual(list(self.app.auto_analyzer.jobs),[job])
        self.app.receive_auto_audio(job,{'kind':'result','result':{'times':[10,20,30],'partial':False}})
        self.assertEqual(self.app.timing['times'],[10,20,30])
        self.assertTrue(self.app.timing['audio_finalized'])

    def test_late_wiki_grouping_keeps_saved_audio_and_manual_anchors(self):
        texts=('青い空を','見ながら歩こう','花を描く')
        self.receive(texts=texts,times=[None]*3)
        timing={**self.app.timing,'times':[10,21,None],'origins':['audio','manual',None],'offset':.5}
        self.app.save_current_timing(timing);self.app.timing=timing
        self.translations(('青い空を見ながら歩こう','花を描く'))
        self.assertEqual([line.original for line in self.app.display_lines],list(texts))
        self.assertIs(self.app.timing,timing)
        self.assertEqual(self.app.timing['times'],[10,21,None])
        self.assertEqual(self.app.timing['offset'],.5)

    def test_minor_provider_typos_keep_original_translation_manual_correction_and_cache(self):
        texts=tuple(f'朝の光を見ながら遠い空の向こうへ歩いて行こう{i}' for i in range(20))
        self.receive(texts=texts,times=[None]*len(texts))
        self.translations(texts)
        before=self.app.display_lines
        self.app.timing['times'][0]=9.5
        self.app.timing['offset']=.2
        remote=list(texts);remote[3]=remote[3].replace('光','灯');remote[17]=remote[17].replace('空','雲')
        times=[10+i*3 for i in range(20)]
        self.receive(source='LyricsPlus',texts=remote,times=times)
        self.assertEqual(self.app.display_lines,before)
        self.assertEqual(self.app.direct_times(),times)
        self.assertEqual(tuple(self.app.reader_bridge.snapshot()['reference_lines']),texts)
        self.assertEqual(self.app.timing['times'][0],9.5)
        self.assertEqual(self.app.timing['offset'],.2)
        self.assertTrue(self.app.has_complete_sync())
        cached=self.app.lyric_cache.load(self.playback)
        self.assertEqual(cached['lines'],before)
        self.assertEqual(cached['direct']['times'],times)
        self.assertEqual(cached['direct']['source'],'LyricsPlus')
        self.position(35)
        self.assertEqual(self.app.displayed_index(),8)
        self.assertEqual(self.app.original_label.cget('text'),texts[8])

    def test_pause_resume_and_backward_seek_follow_playback_clock(self):
        self.receive()
        self.position(22, playing=False, captured=100)
        with patch('media.time.monotonic', return_value=110):
            self.assertEqual(self.app.displayed_index(), 1)
        self.position(22, playing=True, captured=100)
        with patch('media.time.monotonic', return_value=110):
            self.assertEqual(self.app.displayed_index(), 2)
        self.position(11)
        self.assertEqual(self.app.displayed_index(), 0)
        self.position(1)
        self.assertIsNone(self.app.displayed_index())

    def test_tolerant_sync_does_not_copy_translation_from_a_different_repeated_row(self):
        repeated='朝の光を見ながら遠い空の向こうへ歩いて行こう'*3
        self.receive(texts=(repeated,repeated),times=[None,None])
        incoming=(Line(repeated.replace('光','灯',1),'','첫 번째 번역'),
                  Line(repeated,'','두 번째 번역'))
        self.app.receive_direct_lyrics(self.playback.key,
            {'source':'ALSong','provider_id':'123','lines':incoming,'times':[10,20]})
        self.assertEqual(self.app.direct_times(),[10,20])
        self.assertEqual(tuple(line.korean for line in self.app.display_lines),('','두 번째 번역'))

    def test_track_change_clears_direct_timing_and_rejects_late_old_result(self):
        self.receive()
        self.app.set_playback(replace(self.playback, title='Other Song'))
        self.receive()
        self.assertIsNone(self.app.direct_result)
        self.assertIsNone(self.app.song)
        self.assertEqual(self.app.direct_times(), [])

    def test_background_connection_rejects_stale_screen_lyrics_even_when_api_has_none(self):
        self.app.online.set(True)
        self.app.settings['spotify_connected']=True
        self.app.background_lyrics.spotify_authenticated=True
        self.app.background_lyrics.spotify_state='not_found'
        self.app.set_playback(self.playback)
        self.assertFalse(self.app.screen_lyrics.needed)
        self.assertFalse(self.app.screen_lyrics.allow_ocr)
        # The desktop window title has changed, but its lyric DOM still has the previous song.
        stale=tuple(Line(text,'','') for text in TEXTS)
        self.app.messages.put(('spotify_lyrics',(self.playback.key,stale)))
        self.app.messages.put(('screen_lyrics',(self.playback.key,TEXTS)))
        self.app.messages.put(('spotify_tracking',(self.playback.key,'background_clock')))
        self.app.tick()
        self.assertIsNone(self.app.song)
        state=self.app.reader_bridge.snapshot()
        self.assertEqual(state['reference_lines'],())
        self.assertEqual(state['tracking']['status'],'waiting')

    def test_background_connection_rejects_queued_highlight_without_direct_times(self):
        self.translations()
        self.app.online.set(True);self.app.settings['spotify_connected']=True
        self.app.receive_spotify_active(self.playback.key,TEXTS,1,
                                       observed_at=time.monotonic(),observed_position=12)
        self.assertIsNone(self.app.spotify_current)
        self.assertEqual(self.app.reader_bridge.snapshot()['tracking']['status'],'waiting')

    def test_spotify_connection_discards_only_unverified_screen_lyrics(self):
        self.app.messages.put(('spotify_lyrics',(self.playback.key,tuple(Line(text,'','') for text in TEXTS))))
        self.app.tick()
        self.assertIsNotNone(self.app.song)
        with patch.object(self.app.background_lyrics,'connect'):
            self.app.connect_spotify()
        self.assertIsNone(self.app.song)
        self.assertEqual(self.app.reader_bridge.snapshot()['reference_lines'],())
        self.assertFalse(self.app.screen_lyrics.needed)
        self.assertIsNone(self.app.spotify_anchor)
        self.translations()
        trusted=self.app.song
        with patch.object(self.app.background_lyrics,'connect'):
            self.app.connect_spotify()
        self.assertIs(self.app.song,trusted)

    def test_verified_api_replaces_unrelated_screen_originals_instead_of_merging(self):
        self.app.messages.put(('spotify_lyrics',(self.playback.key,(Line('前の曲だけにある言葉','',''),Line('前の曲だけの最後の行','','')))))
        self.app.tick()
        self.receive()
        self.assertEqual(tuple(line.original for line in self.app.song.lines),TEXTS)
        self.assertEqual(self.app.direct_times(),[10,20,30])
        self.assertNotIn('前の曲だけにある言葉',self.app.reader_bridge.snapshot()['reference_lines'])

    def test_translation_arriving_after_api_keeps_direct_times(self):
        self.receive()
        self.translations()
        self.assertEqual(self.app.direct_times(), [10, 20, 30])
        self.position(25)
        self.assertEqual(self.app.korean_label.cget('text'), '번역 1')

    def test_annotation_only_preserves_spotify_clock_selection_and_restart_cache(self):
        self.receive()
        self.app.manual_index=1;self.app.follow.set(False);self.app.render()
        direct=self.app.direct_result
        reference=self.app.reader_bridge.snapshot()['reference_lines']
        timing=dict(self.app.timing)
        lines=tuple(Line(text,'','검증된 번역' if index==1 else '') for index,text in enumerate(TEXTS))
        self.app.receive_direct_lyrics(self.playback.key,
            {'source':'ALSong','provider_id':'123','annotation_only':True,'lines':lines,'times':[1,2,3]})
        self.assertIs(self.app.direct_result,direct)
        self.assertEqual(self.app.direct_times(),[10,20,30])
        self.assertEqual(tuple(line.original for line in self.app.display_lines),TEXTS)
        self.assertEqual(self.app.reader_bridge.snapshot()['reference_lines'],reference)
        self.assertEqual(self.app.timing,timing)
        self.assertEqual(self.app.displayed_index(),1)
        self.assertFalse(self.app.follow.get())
        self.assertEqual(self.app.korean_label.cget('text'),'검증된 번역')
        self.assertIn('번역 1/3줄',self.app.playback_badge.cget('text'))
        cached=self.app.lyric_cache.load(self.playback)
        self.assertEqual(cached['direct']['source'],'Spotify')
        self.assertEqual(cached['direct']['times'],[10,20,30])
        self.assertEqual(cached['lines'][1].korean,'검증된 번역')
        self.app.close();self.root=tk.Tk();self.root.withdraw()
        self.app=LyricsApp(self.root,self.store,start_services=False)
        self.app.set_playback(self.playback)
        self.assertTrue(self.app.cache_hit)
        self.assertEqual(self.app.direct_times(),[10,20,30])
        self.assertEqual(self.app.display_lines[1].korean,'검증된 번역')

    def test_annotation_only_rejects_different_lyrics_without_installing_a_clock(self):
        self.receive(times=[None,None,None])
        original=self.app.display_lines;direct=self.app.direct_result
        self.app.receive_direct_lyrics(self.playback.key,
            {'source':'ALSong','annotation_only':True,'lines':(Line('別の歌','','다른 곡'),),'times':[5]})
        self.assertEqual(self.app.display_lines,original)
        self.assertIs(self.app.direct_result,direct)
        self.assertFalse(self.app.has_direct_sync())

    def test_translation_arriving_before_api_keeps_translations(self):
        self.translations()
        self.receive()
        self.assertEqual(self.app.direct_times(), [10, 20, 30])
        self.position(35)
        self.assertEqual(self.app.korean_label.cget('text'), '번역 2')

    def test_split_translation_after_api_gets_only_verified_phrase_onsets(self):
        self.receive()
        self.translations(('青い空', 'を見る', '花を描く', '最後の言葉'))
        self.assertEqual(self.app.direct_times(), [10, None, 20, 30])
        self.position(25)
        self.assertEqual(self.app.korean_label.cget('text'), '번역 2')

    def test_merged_translation_before_split_api_uses_phrase_start(self):
        self.translations()
        self.receive(texts=('青い空', 'を見る', '花を描く', '最後の言葉'), times=[10, 12, 20, 30])
        self.assertEqual(self.app.direct_times(), [10, 20, 30])
        self.assertEqual(self.app.korean_label.cget('text'), '번역 0')

    def test_manual_anchors_and_global_offset_survive_provider_and_translation_refresh(self):
        self.receive()
        self.app.offset(2)
        self.position(26)
        self.app.manual_index = 1
        self.app.follow.set(False)
        self.app.align_now()
        self.assertEqual(self.app.timing['times'], [None, 24, None])
        self.receive(times=[9, 19, 29])
        self.translations()
        self.assertEqual(self.app.timing['offset'], 2)
        self.assertEqual(self.app.timing['times'], [None, 24, None])
        self.assertEqual(self.app.effective_times()[0], [9, 24, 29])
        self.position(25)
        self.assertEqual(self.app.displayed_index(), 0)
        self.position(26)
        self.assertEqual(self.app.displayed_index(), 1)

    def test_manual_anchor_and_offset_survive_split_translation_regrouping(self):
        self.receive()
        self.app.offset(2)
        self.position(26)
        self.app.manual_index = 1
        self.app.follow.set(False)
        self.app.align_now()
        self.translations(('青い空', 'を見る', '花を描く', '最後の言葉'))
        self.assertEqual(self.app.timing['offset'], 2)
        self.assertEqual(self.app.timing['times'], [None, None, 24, None])
        self.assertEqual(self.app.effective_times()[0], [10, None, 24, 30])

    def test_more_complete_spotify_result_fills_gaps_in_earlier_public_originals(self):
        self.receive(source='LRCLIB', texts=(TEXTS[0], TEXTS[2]), times=[10, 30])
        self.receive()
        self.assertEqual(tuple(line.original for line in self.app.song.lines), TEXTS)
        self.position(25)
        self.assertEqual(self.app.original_label.cget('text'), TEXTS[1])

    def test_incomplete_wiki_arriving_after_api_keeps_complete_original_verses(self):
        self.receive()
        self.translations((TEXTS[0], TEXTS[2]))
        self.assertEqual(tuple(line.original for line in self.app.song.lines), TEXTS)
        self.assertEqual(self.app.direct_times(), [10, 20, 30])
        self.assertEqual(self.app.display_lines[0].korean, '번역 0')
        self.assertEqual(self.app.display_lines[2].korean, '번역 1')
        self.position(25)
        self.assertEqual(self.app.original_label.cget('text'), TEXTS[1])
        self.position(35)
        self.assertEqual(self.app.korean_label.cget('text'), '번역 1')

    def test_negative_offset_keeps_zero_second_cue_valid_and_advances_clock(self):
        self.receive(times=[0, 10, 20])
        self.app.offset(-2)
        self.assertEqual(self.app.effective_times(), ([0, 10, 20], False))
        self.position(0)
        self.assertEqual(self.app.displayed_index(), 0)
        self.position(8)
        self.assertEqual(self.app.displayed_index(), 1)
        self.assertEqual(self.app.timing['offset'], -2)

    def test_four_of_five_unrelated_provider_verses_do_not_enable_direct_sync(self):
        self.translations(('青い空を見る', '花を描く', '最後の言葉', '光の向こうへ進む', '雨の足音を聞く'))
        self.receive(texts=('青い空を見る', '完全に別の文', '時計は動かない', '電車が駅に着く', '地図を広げよう'),
                     times=[10, 20, 30, 40, 50])
        self.assertFalse(self.app.has_direct_sync())
        self.assertTrue(self.app.has_spotify_clock())
        self.assertFalse(self.app.screen_lyrics.needed)
        self.assertEqual(self.app.direct_times(),[10,None,None,None,None])
        self.assertNotIn('직접 싱크', self.app.mode_label.cget('text'))

    def test_sparse_direct_match_does_not_cancel_audio_fallback(self):
        self.translations(('青い空を見る', '花を描く', '最後の言葉', '光の向こうへ進む', '雨の足音を聞く'))
        job = SimpleNamespace(cancelled=threading.Event())
        self.app.realtime_job = job
        self.app.realtime_state = 'listening'
        self.app.realtime_index = 0
        self.app.realtime_matched_at = time.monotonic()
        self.receive(texts=('青い空を見る', '完全に別の文', '時計は動かない', '電車が駅に着く', '地図を広げよう'),
                     times=[10, None, None, None, 50])
        self.assertFalse(self.app.has_direct_sync())
        self.assertFalse(self.app.has_spotify_clock())
        self.assertIs(self.app.realtime_job, job)
        self.assertTrue(self.app.following_audio())

    def test_authorized_cache_stores_lyrics_separately_from_library_and_manual_timing(self):
        self.receive()
        self.translations()
        self.app.offset(.5)
        self.app.align_now()
        self.app.save_settings()
        self.assertEqual(self.store.library(), [])
        self.assertTrue(list((self.store.directory / 'live_timings').glob('*.json')))
        cached=self.app.lyric_cache.load(self.playback)
        self.assertEqual(tuple(line.original for line in cached['lines']),TEXTS)
        self.assertEqual(cached['lines'][1].korean,'번역 1')
        self.assertEqual(cached['direct']['times'],[10,20,30])
        for path in self.store.directory.rglob('*.json'):
            # Both explicit lyric archives are separate from library/manual data.
            if path.parent in (self.store.directory/'lyrics_cache',self.app.batch_translations.history_directory):continue
            text = path.read_text(encoding='utf-8')
            for original in (*TEXTS, '번역 0', '번역 1', '번역 2'):
                self.assertNotIn(original, text, str(path))

    def test_repeated_track_restores_originals_translation_and_sync_before_any_network(self):
        self.receive();self.translations()
        self.app.set_playback(replace(self.playback,title='Other Song'))
        with patch.object(self.app.background_lyrics,'observe') as lookup,patch.object(self.app.lyric_cache,'save') as save:
            self.app.set_playback(self.playback)
        self.assertTrue(self.app.cache_hit)
        self.assertEqual(self.app.original_label.cget('text'),TEXTS[0])
        self.assertEqual(self.app.korean_label.cget('text'),'번역 0')
        self.assertEqual(self.app.direct_times(),[10,20,30])
        lookup.assert_not_called();save.assert_not_called()

    def test_restart_restores_cache_and_preserves_latest_manual_time_and_offset(self):
        self.receive();self.translations()
        self.app.offset(2);self.position(26)
        self.app.manual_index=1;self.app.follow.set(False);self.app.align_now()
        self.app.close()
        self.root=tk.Tk();self.root.withdraw()
        self.app=LyricsApp(self.root,self.store,start_services=False)
        self.app.set_playback(self.playback)
        self.assertTrue(self.app.cache_hit)
        self.assertEqual(self.app.korean_label.cget('text'),'번역 1')
        self.assertEqual(self.app.timing['offset'],2)
        self.assertEqual(self.app.effective_times()[0],[10,24,30])

    def test_cache_never_reuses_unrelated_artist_arrangement_or_length(self):
        self.receive();self.translations()
        for changes in ({'artist':'Other Singer'},{'title':'Test Song (Piano Version)'},{'duration':121}):
            with self.subTest(changes=changes):
                self.app.set_playback(replace(self.playback,title='Other Song'))
                self.app.set_playback(replace(self.playback,**changes))
                self.assertFalse(self.app.cache_hit)
                self.assertIsNone(self.app.song)
                self.assertEqual(self.app.direct_times(),[])

    def test_cache_restore_retries_when_timeline_metadata_arrives_after_title(self):
        self.receive()
        self.app.set_playback(replace(self.playback,title='Other Song'))
        self.app.set_playback(replace(self.playback,duration=0))
        self.assertFalse(self.app.cache_hit)
        self.app.set_playback(self.playback)
        self.assertTrue(self.app.cache_hit)
        self.assertEqual(self.app.direct_times(),[10,20,30])

    def test_unverified_screen_originals_are_never_cached(self):
        self.app.messages.put(('spotify_lyrics',(self.playback.key,tuple(Line(text,'','') for text in TEXTS))))
        self.app.tick()
        self.app.cache_current_lyrics()
        self.assertIsNone(self.app.lyric_cache.load(self.playback))
        self.app.set_playback(replace(self.playback,title='Other Song'))
        self.app.set_playback(self.playback)
        self.assertIsNone(self.app.song)

    def test_spotify_synced_result_outranks_later_public_synced_result(self):
        self.receive()
        self.receive(source='LRCLIB', times=[11, 21, 31])
        self.assertEqual(self.app.direct_times(), [10, 20, 30])
        self.assertEqual(self.app.direct_result[2]['source'], 'Spotify')

    def test_spotify_synced_result_replaces_earlier_public_synced_result(self):
        self.receive(source='LRCLIB', times=[11, 21, 31])
        self.receive()
        self.assertEqual(self.app.direct_times(), [10, 20, 30])
        self.assertEqual(self.app.direct_result[2]['source'], 'Spotify')

    def test_synced_public_result_is_not_replaced_by_unsynced_spotify_result(self):
        self.receive(source='LRCLIB')
        self.receive(times=[None] * 3)
        self.assertEqual(self.app.direct_times(), [10, 20, 30])

    def test_unsynced_api_lyrics_never_claim_direct_sync(self):
        self.receive(times=[None] * 3)
        self.assertEqual(self.app.original_label.cget('text'), TEXTS[0])
        self.assertFalse(self.app.has_direct_sync())
        self.assertEqual(self.app.effective_times(), ([None] * 3, False))
        self.assertIsNone(self.app.displayed_index())
        self.assertNotIn('직접 싱크', self.app.mode_label.cget('text'))

    def test_screen_highlights_cannot_override_direct_clock(self):
        self.receive()
        self.app.receive_spotify_active(self.playback.key, TEXTS, 2,
                                        observed_at=time.monotonic(), observed_position=12)
        self.assertEqual(self.app.displayed_index(), 0)
        self.assertFalse(self.app.following_spotify())

    def test_changed_duration_invalidates_direct_timing(self):
        self.receive()
        self.app.set_playback(replace(self.playback, duration=150))
        self.assertFalse(self.app.has_direct_sync())


if __name__ == '__main__':
    unittest.main()
