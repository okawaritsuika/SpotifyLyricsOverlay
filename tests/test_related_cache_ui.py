"""Related recording reuse through the real overlay, without network services."""
from dataclasses import replace
import hashlib
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from lyrics import Line, Song
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

    def test_reviewed_orchestra_vocals_bind_lyrics_and_start_their_own_capture(self):
        source=replace(self.playback,title='FairyTaleじゃいられない',
                       artist='最上静香 (CV.田所あずさ)',
                       album='THE IDOLM@STER MILLION THE@TER GENERATION 02',duration=222.36)
        lines=self.provider()['lines']
        self.assertTrue(self.app.lyric_cache.save(source,lines,self.provider()))
        target=replace(source,title='FairyTaleじゃいられない（Day 第2幕「BRILLIANT STAGE」より） - Live',
                       artist='堀江晶太',album='THE IDOLM@STER 20th anniversary ORCHESTRA CONCERT '
                       'SYMPHONY OF BRILLIANT STARS コンサートアルバム',duration=284.666,
                       playing=True,base=0,captured=time.monotonic())
        self.app.auto_analyzer.enabled=True;self.app.auto_analyzer.job_factory=FakeJob
        self.app.set_playback(target)
        self.assertTrue(self.app.cache_hit)
        self.assertTrue(self.app.is_bound())
        self.assertEqual(self.app.song.title,target.title)
        self.assertEqual(self.app.song.artist,target.artist)
        self.assertEqual(self.app.song.album,target.album)
        self.assertFalse(self.app.has_complete_sync())
        self.assertFalse(self.app.has_direct_sync())
        self.assertIsNone(self.app.cache_tracking)
        self.assertEqual(self.app.timing['times'],[None,None,None])
        job=next(iter(self.app.auto_analyzer.jobs))
        self.assertFalse(job.waiting_for_lyrics)
        self.assertEqual(job.song.id,self.app.song.id)
        self.assertEqual(job.analysis_key[0],target.key)

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
        self.assertEqual(self.app.playback_badge.cget('text'),'00:12 / 01:30')
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

    def test_linked_solo_remix_uses_manual_clock_only_for_compatible_edits(self):
        album = 'THE IDOLM@STER CINDERELLA MASTER NUMBER ONE!'
        title = 'Test Shared Solo Remix'
        lines = self.provider()['lines']
        group = 'https://open.spotify.com/track/' + 'g'*22
        store = self.app.playlist_lyrics.store

        def save(letter, name, suffix, duration, link=group):
            track = {'id': letter*22, 'title': title+suffix, 'artists': [name],
                     'album': album, 'duration': duration}
            self.assertTrue(store.save(track, lines, None, ('same_work_reference', link)))
            return replace(self.playback, title=track['title'], artist=name, album=album, duration=duration)

        source = save('a', '藤原肇 (CV: 鈴木みのり)', ' - 藤原肇ソロ・リミックス', 133.64)
        target = save('b', '遊佐こずえ (CV: 花谷麻妃)', ' - 遊佐こずえソロ・リミックス', 133.666)
        full = save('c', '安部菜々', '', 239.515)
        game = save('d', '安部菜々', ' - GAME VERSION', 134)
        unlinked = save('e', '望月聖', ' - 望月聖ソロ・リミックス', 133.813, group.replace('g'*22, 'h'*22))

        self.app.set_playback(source)
        self.app.save_current_timing({'times':[10,20,30], 'offset':0, 'origins':['manual']*3})
        self.app.set_playback(target)
        self.app.save_current_timing({'times':[None,22,None], 'offset':0, 'origins':[None,'manual',None]})
        self.app.timing = self.app.live_lyrics.timing
        self.assertEqual(self.app.effective_times()[0], [10,22,30])
        self.assertEqual(self.app.time_sources, ['참고 싱크','수동','참고 싱크'])
        self.assertFalse(self.app.has_complete_sync())

        for playback in (full, game, unlinked):
            with self.subTest(title=playback.title):
                self.app.set_playback(playback)
                self.assertIsNone(self.app.current_reference())

        self.app.set_playback(source)
        self.app.save_current_timing({'times':[10,None,None], 'offset':0, 'origins':['manual',None,None]})
        self.app.auto_analyzer.enabled = True
        self.app.auto_analyzer.job_factory = FakeJob
        self.app.set_playback(replace(target, base=0, playing=True, captured=time.monotonic()))
        self.assertEqual(self.app.effective_times()[0], [10,22,None])
        job, = self.app.auto_analyzer.jobs
        self.assertIsNone(getattr(job, 'bounded_window', None))

    def test_cinderella_no1_solos_keep_their_own_lyrics_and_manual_clock(self):
        store = self.app.playlist_lyrics.store
        album = 'THE IDOLM@STER CINDERELLA MASTER NUMBER ONE!'
        link = 'https://open.spotify.com/track/' + 'g'*22
        first, second, third = self.provider()['lines']
        solos = []
        for letter, name, middle in (('a', '藤原肇', second), ('b', '遊佐こずえ', Line('別の歌詞', '', ''))):
            title = f'シンデレラNo.1 - {name}ソロ・リミックス'
            lines = (first, middle, third)
            track = {'id': letter*22, 'title': title, 'artists': [name], 'album': album, 'duration': 134}
            self.assertTrue(store.save(track, lines, None, ('same_work_reference', link)))
            solos.append((replace(self.playback, title=title, artist=name, album=album, duration=134), lines))

        (source, _), (target, target_lines) = solos
        self.app.set_playback(source)
        self.app.save_current_timing({'times':[10,20,30], 'offset':0, 'origins':['manual']*3})
        saved_target = {**store.find(target), 'lines': (first, second, third)}
        self.assertIsNone(store.find_linked_reference(target, saved_target, self.app.live_lyrics.timing_store,
                                                      self.app.lyric_cache))
        self.app.set_playback(target)
        self.assertEqual(self.app.display_lines, target_lines)
        self.assertIsNone(self.app.current_reference())
        self.app.save_current_timing({'times':[None,22,None], 'offset':0, 'origins':[None,'manual',None]})
        self.app.set_playback(source)
        self.app.set_playback(target)
        self.assertEqual(self.app.effective_times()[0], [None,22,None])

    def test_anniversary_solo_uses_group_clock_despite_conflicting_cache_without_full_capture(self):
        title='Harmony 4 You'
        album='THE IDOLM@STER MILLION THE@TER SEASON'
        artist='765 MILLION ALLSTARS'
        source=replace(self.playback,title=title,album=album,artist=artist)
        target=replace(source,title=title+' (舞浜 歩 Ver.)',artist='舞浜 歩 (CV.戸田めぐみ)',
                       duration=91,base=0,playing=True,captured=time.monotonic())
        track={'id':'a'*22,'title':title,'artists':[artist],'album':album,'duration':90}
        provider={**self.provider(),'provider_id':track['id']}
        self.assertTrue(self.app.playlist_lyrics.store.save(track,provider['lines'],provider))
        self.assertTrue(self.app.lyric_cache.save(target,provider['lines']))
        for name, times in (('Release A',[5,15,25]),('Release B',[7,17,27])):
            self.assertTrue(self.app.lyric_cache.save(replace(source,album=name),provider['lines'],
                self.provider(source='LRCLIB',times=times)))
        self.app.auto_analyzer.enabled=True
        self.app.auto_analyzer.job_factory=FakeJob
        self.app.set_playback(target)
        self.assertEqual(self.app.anniversary_sync(),[10,20,30])
        self.assertEqual(self.app.effective_times()[0],[10,20,30])
        self.assertEqual(self.app.timing['times'],[None,None,None])
        self.assertFalse(self.app.has_direct_sync())
        self.assertFalse(self.app.auto_analyzer.jobs)

    def test_anniversary_missing_row_uses_only_a_bounded_capture(self):
        source=replace(self.playback,title='Harmony 4 You',artist='765 MILLION ALLSTARS')
        target=replace(source,title=source.title+' (舞浜 歩 Ver.)',artist='舞浜 歩 (CV.戸田めぐみ)',
                       duration=91,base=10)
        first, second, third=background.TEXTS
        provider=self.provider(texts=(first+second,third),times=[10,30])
        self.assertTrue(self.app.lyric_cache.save(source,provider['lines'],provider))
        self.assertTrue(self.app.lyric_cache.save(target,self.provider()['lines']))
        self.app.auto_analyzer.enabled=True
        self.app.auto_analyzer.job_factory=FakeJob
        self.app.set_playback(target)
        self.assertEqual(self.app.anniversary_sync(),[10,None,30])
        self.app.set_playback(replace(target,playing=True,captured=time.monotonic()))
        job,=self.app.auto_analyzer.jobs
        self.assertEqual(job.options['capture_window'],(10,30))
        self.assertEqual(job.bounded_rows,(0,2))


if __name__=='__main__':unittest.main()
