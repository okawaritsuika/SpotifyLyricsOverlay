"""Provider anchors win over inferred clocks; only bounded missing cues progress."""
from dataclasses import replace
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest

import test_background_overlay as background
from lyrics import Line
from local_analysis import AnalysisStore


class SyncAuthorityTests(unittest.TestCase):
    setUp=background.BackgroundOverlayTests.setUp
    tearDown=background.BackgroundOverlayTests.tearDown
    provider=background.BackgroundOverlayTests.provider
    receive=background.BackgroundOverlayTests.receive
    position=background.BackgroundOverlayTests.position

    def test_finalized_audio_cannot_replace_spotify_even_with_manual_priority_flag(self):
        self.receive()
        self.app.timing={**self.app.timing,'times':[8,18,28],'origins':['audio']*3,
                         'audio_finalized':True,'manual_priority':True,'source':'음원 분석'}
        self.assertEqual(self.app.effective_times()[0],[10,20,30])
        self.position(19)
        self.assertEqual(self.app.displayed_index(),0)
        self.assertEqual(self.app.mode_label.cget('text'),'')
        self.assertEqual(self.app.timing['times'],[8,18,28])

    def test_finalized_audio_keeps_only_explicit_manual_correction_and_offset(self):
        self.receive()
        self.app.timing={**self.app.timing,'times':[8,23,28],'origins':['audio','manual','audio'],
                         'audio_finalized':True,'offset':2,'source':'직접 편집 / 자동 싱크'}
        self.assertEqual(self.app.effective_times()[0],[10,23,30])
        for seconds,index in ((11,None),(12,0),(24,0),(25,1),(32,2),(12,0)):
            self.position(seconds)
            self.assertEqual(self.app.displayed_index(),index)

    def test_missing_audio_candidate_outside_neighboring_spotify_onsets_is_rejected(self):
        self.receive(times=[10,None,30])
        for middle,expected in ((9,None),(31,None),(21,21)):
            self.app.timing={**self.app.timing,'times':[5,middle,40],
                             'origins':['audio']*3,'audio_finalized':True,'source':'음원 분석'}
            self.assertEqual(self.app.effective_times()[0],[10,expected,30])

    def test_playlist_mp3_candidate_fills_overlay_gap_and_leaves_saved_timing(self):
        track_id='A'*22
        track={'id':track_id,'title':self.playback.title,'artists':[self.playback.artist],
               'album':self.playback.album,'duration':self.playback.duration}
        direct={**self.provider(times=[10,None,30]),'provider_id':track_id}
        self.assertTrue(self.app.playlist_lyrics.store.save(track,direct['lines'],direct))
        source=Path(self.temp.name)/'0001 Test Song.mp3'
        source.write_bytes(b'fixture audio')
        database=AnalysisStore(self.temp.name)
        spec={'kind':'lyrics','track_id':track_id,'expected_duration':90,
              'lines':[row.original for row in direct['lines']]}
        job=database.get(database.register(source,spec))
        database.update(job,'done','aligned',{'duration':90,'alignment':{'times':[9,21,31],'matched':3}})
        self.app.restore_cached_lyrics()
        self.assertEqual(self.app.effective_times()[0],[10,21,30])
        self.position(22)
        self.assertEqual(self.app.displayed_index(),1)
        self.assertEqual(self.app.time_sources[1],'MP3 음원 싱크')
        self.assertEqual(self.app.timing['times'],[None,None,None])
        self.assertEqual(self.app.direct_times(),[10,None,30])

        other=Path(self.temp.name)/'newer review.mp3'
        other.write_bytes(b'unverified recording')
        unverified=database.get(database.register(other,spec))
        database.update(unverified,'done','aligned',{'duration':90,'alignment':{'times':[9,23,31],'matched':3},
                        'playlist_clock':{'usable':False,'reason':'recording_tags_unverified'}})
        self.app.local_analysis_cache=(None,0,None)
        self.app.local_playlist_sync_cache=(None,0,None)
        self.assertEqual(self.app.current_local_analysis()['id'],unverified['id'])
        self.assertEqual(self.app.effective_times()[0],[10,21,30])
        self.assertEqual(self.app.direct_times(),[10,None,30])

        database.update(job,'done','aligned',{'duration':90,'alignment':{'times':[None,21,None],'matched':1}})
        self.app.local_playlist_sync_cache=(None,0,None)
        self.assertEqual(self.app.effective_times()[0],[10,None,30])

    def test_missing_spotify_cue_is_estimated_only_inside_confirmed_neighbors(self):
        self.app.lyric_advance_ms=0
        texts=tuple(f'固有の歌詞{i}番' for i in range(5))
        self.receive(texts=texts,times=[10,None,30,40,50])
        self.app.use_estimate.set(True)
        times,estimated=self.app.effective_times()
        self.assertEqual(times,[10,20,30,40,50])
        self.assertTrue(estimated)
        for seconds,index in ((9,None),(10,0),(19,0),(20,1),(29.99,1),(30,2),(40,3),(50,4),(11,0)):
            self.position(seconds)
            self.assertEqual(self.app.displayed_index(),index)
        self.position(22)
        self.assertEqual(self.app.time_sources[1],'추정')
        self.assertEqual(self.app.direct_result[2]['times'],[10,None,30,40,50])

    def test_single_anchor_does_not_invent_before_and_after_cues(self):
        self.receive(times=[None,20,None])
        self.app.use_estimate.set(True)
        self.assertEqual(self.app.effective_times(),([None,20,None],False))
        self.position(19)
        self.assertIsNone(self.app.displayed_index())
        self.position(80)
        self.assertEqual(self.app.displayed_index(),1)

    def test_editor_labels_the_effective_provider_clock_instead_of_saved_audio_or_estimate(self):
        self.receive(times=[10,None,30])
        self.app.timing={**self.app.timing,'times':[5,None,25],
                         'origins':['audio',None,'audio'],'audio_finalized':True,'source':'음원 분석'}
        self.app.use_estimate.set(True)
        self.app.open_editor();self.app.editor_refresh()
        rows=[self.app.tree.item(str(i))['values'] for i in range(3)]
        self.assertEqual([row[3] for row in rows],['Spotify','추정','Spotify'])
        self.assertFalse(str(rows[0][0]).startswith('~'))
        self.assertTrue(str(rows[1][0]).startswith('~'))
        self.assertIn('10',str(rows[0][0]))

    def test_partial_spotify_survives_public_arrival_and_restart(self):
        self.receive(times=[10,None,30])
        self.receive(source='LRCLIB',times=[11,21,31])
        self.assertEqual(self.app.direct_result[2]['source'],'Spotify')
        self.assertEqual(self.app.direct_result[2]['times'],[10,None,30])
        self.assertEqual(self.app.direct_times(),[10,None,30])
        self.position(20)
        self.assertEqual(self.app.displayed_index(),0)
        self.app.set_playback(replace(self.playback,title='Other song'))
        self.app.set_playback(self.playback)
        self.assertEqual(self.app.direct_times(),[10,None,30])
        self.assertEqual(self.app.direct_result[2]['source'],'Spotify')

    def test_partial_spotify_arriving_last_discards_external_clock(self):
        self.receive(source='LRCLIB',times=[11,21,31])
        self.receive(times=[10,None,30])
        self.assertEqual(self.app.direct_times(),[10,None,30])
        self.assertEqual(self.app.direct_result[2]['source'],'Spotify')

    def test_legacy_cached_external_gap_is_removed_on_load(self):
        value=self.provider(times=[10,None,30])
        value['fallback']=self.provider(source='LRCLIB',times=[11,21,31])
        self.app.lyric_cache.save(self.playback,value['lines'],value)
        self.app.restore_cached_lyrics()
        self.assertTrue(self.app.cache_hit)
        self.assertNotIn('fallback',self.app.direct_result[2])
        self.assertEqual(self.app.direct_times(),[10,None,30])

    def test_playlist_cache_does_not_replace_partial_spotify_with_complete_auxiliary(self):
        track_id='A'*22
        track={'id':track_id,'title':self.playback.title,'artists':[self.playback.artist],
               'album':self.playback.album,'duration':self.playback.duration}
        spotify={**self.provider(times=[10,None,30]),'provider_id':track_id}
        auxiliary=self.provider(source='LRCLIB',times=[11,21,31])
        self.assertTrue(self.app.lyric_cache.save(self.playback,spotify['lines'],spotify))
        self.assertTrue(self.app.playlist_lyrics.store.save(track,auxiliary['lines'],auxiliary))
        self.app.restore_cached_lyrics()
        self.assertTrue(self.app.cache_hit)
        self.assertEqual(self.app.direct_result[2]['source'],'Spotify')
        self.assertEqual(self.app.direct_times(),[10,None,30])

    def test_open_editor_refreshes_when_provider_clock_arrives(self):
        self.receive(times=[None,None,None])
        self.app.open_editor()
        self.assertEqual(self.app.tree.item('0')['values'][3],'자동 대기')
        self.receive()
        self.assertEqual(self.app.tree.item('0')['values'][3],'Spotify')

    def partial_realtime(self,last=18):
        texts=tuple(f'固有の歌詞{i}番' for i in range(5))
        self.receive(texts=texts,times=[10,None,None,None,last])
        self.app.use_estimate.set(True)
        self.assertFalse(self.app.has_direct_sync())
        return SimpleNamespace(cancelled=threading.Event(),playback_key=self.playback.key,
                               song=self.app.song,memory_only=False)

    def realtime_event(self,job,index,onset,position):
        now=time.monotonic()
        # Advance the playback clock without treating synthetic input as a seek.
        self.app.playback=replace(self.playback,base=position,captured=now)
        self.app.receive_realtime(job,{'kind':'realtime','observed_at':now,'position':position,
            'match':{'index':index,'onset':onset,'confidence':.8}})

    def test_partial_spotify_does_not_advance_then_retract_an_audio_prediction(self):
        job=self.partial_realtime()
        for index,onset in enumerate((10,12,14)):
            self.realtime_event(job,index,onset,onset+.5)
        self.assertEqual(self.app.realtime_index,2)
        self.app.playback=replace(self.playback,base=16,captured=time.monotonic())
        self.assertEqual(self.app.displayed_index(),2)
        # Fresh recognition of the same phrase used to retract row 3 to row 2.
        self.realtime_event(job,2,14,17.2)
        self.assertEqual(self.app.displayed_index(),2)
        self.app.playback=replace(self.playback,base=18,captured=time.monotonic())
        self.assertEqual(self.app.displayed_index(),4)

    def test_partial_spotify_keeps_recognized_row_after_audio_expires_or_fails(self):
        job=self.partial_realtime(last=30)
        for index,onset in enumerate((10,12,14)):
            self.realtime_event(job,index,onset,onset+.5)
        self.app.realtime_matched_at=time.monotonic()-11
        self.app.playback=replace(self.playback,base=25.5,captured=time.monotonic())
        self.assertFalse(self.app.following_audio())
        self.assertEqual(self.app.displayed_index(),2)
        self.app.receive_realtime(job,{'kind':'realtime_status','state':'failed','text':'test failure'})
        self.assertEqual(self.app.displayed_index(),2)
        self.realtime_event(job,3,26,26.5)
        self.assertEqual(self.app.displayed_index(),3)
        self.app.playback=replace(self.playback,base=30,captured=time.monotonic())
        self.assertEqual(self.app.displayed_index(),4)
        self.position(9)
        self.assertIsNone(self.app.realtime_index)
        self.assertIsNone(self.app.displayed_index())

    def test_partial_spotify_recognition_does_not_survive_track_change(self):
        job=self.partial_realtime(last=30)
        self.realtime_event(job,2,14,14.5)
        self.app.set_playback(replace(self.playback,title='Other song'))
        self.app.set_playback(self.playback)
        self.assertIsNone(self.app.realtime_index)
        self.assertFalse(self.app.holding_audio())
        self.assertEqual(self.app.displayed_index(),0)

    def test_changed_spotify_onset_inside_merged_row_rechecks_cached_estimates(self):
        source=('start','ab','cd','ef','end')
        self.receive(texts=source,times=[10,20,21,None,40])
        display=('start','abcd','ef','end')
        self.app.receive_reader_lyrics(self.app.reader_bridge.snapshot()['track'],
            tuple(Line(text,'','translation') for text in display))
        self.app.use_estimate.set(True)
        self.assertEqual(self.app.effective_times()[0],[10,20,32,40])
        self.receive(texts=source,times=[10,20,35,None,40])
        # Visible onsets did not change, but row 2 must now wait past 35 s.
        self.assertEqual(self.app.direct_times(),[10,20,None,40])
        self.assertEqual(self.app.effective_times()[0],[10,20,None,40])


if __name__=='__main__':unittest.main()
