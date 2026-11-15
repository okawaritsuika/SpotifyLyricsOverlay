import gc
import http.client
import json
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from app import LyricsApp
from lyrics import Line,Song
from media import Playback
from storage import Store

TEXT='窓を開ける\n마도오 아케루\n창문을 열다'


class LiveOverlayTests(unittest.TestCase):
    def test_verified_aside_translation_keeps_live_originals_and_manual_timing(self):
        from dataclasses import replace
        from test_lyric_annotations import AnnotationTests
        originals,supplied=AnnotationTests().parenthetical_document()
        self.playing()
        track=self.app.reader_bridge.snapshot()['track']
        self.app.receive_reader_lyrics(track,originals)
        timing={'times':[10,20,30,40,50],'origins':['manual']*5,'offset':.5}
        self.app.save_current_timing(timing);self.app.timing=timing
        self.app.live_lyrics.timing=timing
        # More than the fuzzy budget; the verified main lyric still agrees.
        source=(replace(supplied[0],original=supplied[0].original+'(風の合図)',
                        reading='',korean=supplied[0].korean+'(코러스)'),*supplied[1:])
        self.assertTrue(self.app.merge_current_annotations(source))
        self.assertEqual([line.korean for line in self.app.display_lines],
                         [line.korean for line in supplied])
        self.assertEqual([line.original for line in self.app.display_lines],
                         [line.original for line in originals])
        self.assertEqual(self.app.timing,timing)
        self.assertFalse(self.app.display_lines[0].reading)

    def test_late_merged_translation_keeps_manual_rows_in_display_and_persistent_cache(self):
        playback=Playback(title='Test Song',artist='Singer',album='Album',base=20,duration=90,
                          playing=True,captured=time.monotonic())
        self.app.set_playback(playback)
        track=self.app.reader_bridge.snapshot()['track']
        original=(Line('窓を開ける','',''),Line('空を見る','',''),Line('道を歩く','',''))
        self.app.receive_reader_lyrics(track,original)
        timing={'times':[10,20,30],'origins':['manual']*3,'offset':.5}
        self.app.save_current_timing(timing);self.app.timing=timing
        self.app.live_lyrics.timing=timing
        self.app.receive_reader_lyrics(track,(Line('窓を開ける空を見る','','창문을 열고 하늘을 본다'),
                                              Line('道を歩く','','길을 걷는다')))
        self.assertEqual([line.original for line in self.app.display_lines],[line.original for line in original])
        self.assertEqual(self.app.timing['times'],[10,20,30])
        self.assertEqual(self.app.display_lines[0].korean,'창문을 열고 하늘을 본다')
        self.assertEqual(self.app.display_lines[1].korean,'창문을 열고 하늘을 본다')
        self.playing('Other');self.app.set_playback(playback)
        self.assertEqual([line.original for line in self.app.display_lines],[line.original for line in original])
        self.assertEqual(self.app.timing['times'],[10,20,30])
        self.assertEqual(self.app.timing['offset'],.5)

    def test_complete_spotify_originals_fill_saved_omissions_and_keep_manual_timing(self):
        saved=Song('saved-gap','Test Song','','',(Line('青い空を見る','あおい','푸른 하늘'),Line('最後の言葉','さいご','마지막')))
        self.store.save_library([saved]);self.app.songs=[saved]
        self.link_saved_song(saved)
        self.store.save_timing(saved,{'times':[10,50],'offset':.5,'source':'직접 기록'})
        paths=[self.store.directory/'library.json',self.store.directory/'timings/saved-gap.json']
        before=[path.read_bytes() for path in paths]
        self.playing();self.app.cursor=1;self.app.manual_index=1;self.app.follow.set(False)
        supplied=(Line('青い空','',''),Line('を見る','',''),Line('新しい言葉','あたらしい',''),Line('最後の言葉','',''))
        self.app.messages.put(('spotify_lyrics',(self.app.playback.key,supplied)));self.tick()
        self.assertEqual([line.original for line in self.app.display_lines],['青い空を見る','新しい言葉','最後の言葉'])
        self.assertEqual(self.app.timing['times'],[10,None,50]);self.assertEqual(self.app.timing['offset'],.5)
        persisted=self.app.auto_analyzer.timing(self.app.song,True)
        self.assertEqual(persisted['times'],[10,None,50]);self.assertTrue(persisted['completion_pending'])
        self.assertEqual((self.app.cursor,self.app.manual_index,self.app.follow.get()),(2,2,False))
        self.assertTrue(self.app.live_lyrics.is_temporary(self.app.song));self.assertTrue(self.app.is_bound())
        self.app.messages.put(('spotify_lyrics',(self.app.playback.key,supplied)));self.tick()
        self.assertEqual(len(self.app.song.lines),3)
        self.app.receive_reader_lyrics(self.app.reader_bridge.snapshot()['track'],(Line('新しい言葉','','새 말'),))
        self.assertEqual(self.app.display_lines[1].korean,'새 말')
        self.assertEqual(self.app.timing['times'],[10,None,50])
        self.app.follow.set(False);self.app.manual_index=0;self.app.cursor=0;self.app.stamp()
        corrected=list(self.app.timing['times'])
        self.playing('Other');self.playing()
        self.app.messages.put(('spotify_lyrics',(self.app.playback.key,supplied)));self.tick()
        self.assertEqual(self.app.timing['times'],corrected)
        self.assertEqual(before,[path.read_bytes() for path in paths])
        for path in (self.store.directory/'live_timings').glob('*.json'):
            self.assertNotIn('新しい言葉',path.read_text(encoding='utf-8'))

    def setUp(self):
        # Repeated Tk interpreters must be reclaimed on their creating thread,
        # before a request worker can trigger Python's cyclic garbage collector.
        gc.collect()
        self.temp=tempfile.TemporaryDirectory();self.store=Store(self.temp.name)
        self.store.save_settings({'online':False,'auto_audio':False,'web_auto':False,'borderless':False})
        self.root=tk.Tk();self.app=LyricsApp(self.root,self.store,start_services=False)
        # The HTTP cases exercise the bridge's compatibility backend directly.
        self.app.reader_bridge.set_backend('chrome')
        self.app.reader_bridge.port=0;self.app.reader_bridge.start()
    def tearDown(self):
        self.root.update_idletasks()
        self.app.close();self.temp.cleanup()
        self.app=self.root=None
        gc.collect()
    def playing(self,title='Test Song'):
        self.app.set_playback(Playback(title=title,base=20,duration=90,playing=True,captured=time.monotonic()))

    def tick(self):
        self.app.tick()
        deadline=time.monotonic()+3
        while self.app.merge_reader_receipts and time.monotonic()<deadline:
            time.sleep(.005)
            self.app.tick()

    def link_saved_song(self,song):
        # These legacy document fixtures omit artist/album metadata. Their
        # saved-timing tests require the user's explicit recording link.
        self.app.settings.setdefault('links',{})[Playback(title=song.title).key]=song.id
        self.app.save_settings()

    def test_extension_only_lyrics_start_auto_tracking_without_manual_library_import(self):
        from test_auto_audio import FakeJob
        self.app.auto_analyzer.enabled=True;self.app.auto_analyzer.job_factory=FakeJob
        self.playing()
        self.app.receive_reader_lyrics(self.app.reader_bridge.snapshot()['track'],(Line('窓を開ける','','창문을 열다'),))
        job=next(iter(self.app.auto_analyzer.jobs))
        self.assertTrue(job.memory_only)
        self.assertTrue(job.incremental)
        self.assertFalse(job.skip_final)
        self.assertEqual(self.store.library(),[])
        self.playing()
        self.assertEqual(len(self.app.auto_analyzer.jobs),1)

    def test_imported_manual_timeline_avoids_redundant_screen_work(self):
        song=Song('saved','Test Song','','',(Line('窓を開ける','','창문을 열다'),))
        self.store.save_library([song]);self.app.songs=[song]
        self.link_saved_song(song)
        self.store.save_timing(song,{'times':[10],'offset':0,'source':'직접 기록'})
        self.playing()
        self.assertFalse(self.app.screen_lyrics.needed)
        self.assertFalse(self.app.screen_lyrics.allow_ocr)
        self.assertEqual(self.app.displayed_index(),0)
        self.playing('Unknown')
        self.assertTrue(self.app.screen_lyrics.needed)
        self.assertTrue(self.app.screen_lyrics.allow_ocr)
    def payload(self,**updates):
        # This fixture has no media polling service to refresh the track.
        self.app.reader_bridge.observe(self.app.playback)
        state=self.app.reader_bridge.snapshot()
        return {'track':state['track'],'token':state['token'],'text':TEXT,'url':'https://namu.wiki/w/Test%20Song',**updates}
    def post(self,payload,headers=None):
        body=json.dumps(payload,ensure_ascii=False).encode()
        connection=http.client.HTTPConnection('127.0.0.1',self.app.reader_bridge.port,timeout=8)
        connection.request('POST','/lyrics',body,headers or {'X-Reader-Extension':'a'*32,'Content-Type':'application/json'})
        response=connection.getresponse();result=response.status,json.loads(response.read())
        connection.close();return result
    def test_http_translation_appears_in_existing_overlay_and_preserves_sync_files(self):
        song=Song('existing','Test Song','','',(Line('窓を開ける','',''),))
        self.store.save_library([song]);self.app.songs=[song]
        self.link_saved_song(song)
        self.store.save_timing(song,{'times':[12],'offset':.5})
        paths=[self.store.directory/'library.json',self.store.directory/'timings'/'existing.json']
        before=[path.read_bytes() for path in paths]
        self.playing()
        self.assertEqual(self.post(self.payload())[0],200)
        self.tick();self.root.update()
        self.assertEqual(self.app.korean_label.cget('text'),'창문을 열다')
        self.assertEqual(self.app.reader_bridge.snapshot()['receipt']['status'],'displayed')
        self.assertIn('あける',self.app.reading_label.cget('text'))
        self.assertEqual(self.app.timing['times'],[12]);self.assertEqual(self.app.timing['offset'],.5)
        self.assertEqual(before,[path.read_bytes() for path in paths])
        self.assertFalse((self.store.directory/'lyric_annotations').exists())
        self.playing('Next Song')
        self.assertEqual(self.app.live_lyrics.lines,())
        self.assertNotEqual(self.app.korean_label.cget('text'),'창문을 열다')
    def test_unknown_song_text_stays_in_memory_and_only_timing_is_saved(self):
        self.playing();self.assertEqual(self.post(self.payload())[0],200)
        self.tick();self.root.update()
        self.assertTrue(self.app.live_lyrics.is_temporary(self.app.song))
        self.assertEqual(self.app.korean_label.cget('text'),'창문을 열다')
        self.assertEqual(self.app.reader_bridge.snapshot()['receipt']['status'],'displayed')
        with patch.object(self.store,'save_timing',side_effect=AssertionError('must not persist live lyrics')):
            self.app.offset(.25);self.app.align_now()
        self.app.open_editor();self.root.update()
        self.assertEqual(self.app.tree.item('0')['values'][2],'창문을 열다')
        self.assertEqual(self.store.library(),[])
        self.assertFalse((self.store.directory/'timings').exists())
        saved=list((self.store.directory/'live_timings').glob('*.json'))
        self.assertEqual(len(saved),1)
        value=json.loads(saved[0].read_text(encoding='utf-8'))
        self.assertNotIn('창문을 열다',saved[0].read_text(encoding='utf-8'))
        self.assertNotIn('窓を開ける',saved[0].read_text(encoding='utf-8'))
        self.assertIsNotNone(value['times'][0])
        with self.assertRaises(ValueError):self.app.start_audio()
        self.app.save_settings()
        self.assertNotIn('창문을 열다',(self.store.directory/'settings.json').read_text(encoding='utf-8'))

    def test_exact_recording_title_displays_without_spotify_references(self):
        from urllib.parse import quote
        title='蒼い鳥 (OathOne Recording)';self.playing(title)
        self.assertEqual(self.post(self.payload(url='https://namu.wiki/w/'+quote(title),match_mode='title'))[0],200)
        self.tick();self.root.update()
        self.assertEqual(self.app.reader_bridge.snapshot()['receipt']['status'],'displayed')
        self.assertEqual(self.app.korean_label.cget('text'),'창문을 열다')
        self.assertEqual(self.store.library(),[])

    def test_singer_version_learns_displayed_document_and_reuses_it_after_reload(self):
        from reader_bridge import ReaderBridge
        title='Crossing! (如月千早 Ver.)';url='https://namu.wiki/w/Crossing!'
        self.playing(title);track=self.app.reader_bridge.snapshot()['track']
        self.assertEqual(self.post(self.payload(url=url,match_mode='unique_title'))[0],200)
        path=self.store.directory/'wiki_documents.json'
        self.assertFalse(path.exists())
        self.tick();self.root.update()
        self.assertEqual(self.app.reader_bridge.snapshot()['receipt']['status'],'displayed')
        self.assertEqual(self.app.korean_label.cget('text'),'창문을 열다')
        saved=json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(saved,{track:{'url':url,'learned':True}})
        reloaded=ReaderBridge(document_urls=saved);reloaded.observe(self.app.playback)
        self.assertEqual(reloaded.snapshot()['document_url'],url)
        self.assertTrue(reloaded.snapshot()['document_learned'])
        self.assertNotIn('窓を開ける',path.read_text(encoding='utf-8'))
        self.assertNotIn('창문을 열다',path.read_text(encoding='utf-8'))
        self.playing('Crossing!')
        self.assertEqual(self.app.reader_bridge.snapshot()['document_url'],'')

    def test_queued_learning_cannot_overwrite_a_manual_document_choice(self):
        self.playing();track=self.app.reader_bridge.snapshot()['track']
        chosen='https://namu.wiki/w/Chosen'
        self.app.messages.put(('reader_document',(track,chosen)))
        self.app.messages.put(('reader_document',(track,{'url':'https://namu.wiki/w/Test%20Song','learned':True})))
        self.tick()
        saved=json.loads((self.store.directory/'wiki_documents.json').read_text(encoding='utf-8'))
        self.assertEqual(saved,{track:chosen})

    def test_original_document_lyrics_keep_each_recordings_timing_separate(self):
        from urllib.parse import quote
        for name in ('蒼い鳥','眠り姫','M@STERPIECE','Synthetic Song'):
            with self.subTest(name=name):
                source='https://namu.wiki/w/'+quote(name)
                self.playing(name)
                self.assertEqual(self.post(self.payload(url=source,match_mode='title'))[0],200)
                self.tick();self.root.update()
                original=self.app.song
                self.app.live_lyrics.timing_store.save(original,{'times':[12.5],'offset':.3})
                self.playing(name+' (OathOne Recording)')
                # A shortened title alone is not sufficient evidence for automatic reuse.
                self.assertEqual(self.post(self.payload(url=source,match_mode='title'))[0],400)
                self.assertEqual(self.post(self.payload(url='https://namu.wiki/w/Unrelated',match_mode='metadata'))[0],400)
                for mode in ('metadata','unique_title'):
                    self.assertEqual(self.post(self.payload(url=source,match_mode=mode))[0],200)
                self.tick();self.root.update()
                recording=self.app.song
                self.assertNotEqual(recording.id,original.id)
                self.assertEqual(recording.title,name+' (OathOne Recording)')
                self.assertEqual(self.app.timing['times'],[None])
                self.assertEqual(self.app.korean_label.cget('text'),'창문을 열다')
                self.assertEqual(self.app.reader_bridge.snapshot()['receipt']['status'],'displayed')
                self.app.live_lyrics.timing_store.save(recording,{'times':[14.2],'offset':-.2})
                self.assertEqual(self.app.live_lyrics.timing_store.load(original)['times'],[12.5])
                self.assertEqual(self.app.live_lyrics.timing_store.load(recording)['times'],[14.2])

    def test_failed_display_reports_failure_after_http_queue_accepts(self):
        self.playing();self.assertEqual(self.post(self.payload())[0],200)
        self.assertEqual(self.app.reader_bridge.snapshot()['receipt']['status'],'queued')
        with patch.object(self.app.live_lyrics,'receive_merged',side_effect=ValueError('synthetic display failure')), \
             patch('app.messagebox.showerror') as modal:
            self.tick()
        modal.assert_not_called()
        self.assertEqual(self.app.reader_bridge.snapshot()['receipt']['status'],'display_failed')
        self.assertIn('synthetic display failure',self.app.mode_label.cget('text'))
        self.assertIsNone(self.app.song)
        self.assertEqual(self.store.library(),[])
        self.assertFalse((self.store.directory/'wiki_documents.json').exists())
        self.app.messages.put(('media',Playback(title='Next song',base=10,duration=90,playing=True,captured=time.monotonic())))
        self.tick()
        self.assertEqual(self.app.playback.title,'Next song')

    def test_english_refrain_does_not_block_http_translation_display(self):
        self.playing();text='Open the window\n'+TEXT+'\n花を描く\n하나오 카쿠\n꽃을 그리다'
        self.assertEqual(self.post(self.payload(text=text))[0],200)
        self.tick();self.root.update()
        self.assertEqual([line.korean for line in self.app.live_lyrics.lines],['','창문을 열다','꽃을 그리다'])
        self.app.follow.set(False);self.app.manual_index=1;self.app.render()
        self.assertEqual(self.app.korean_label.cget('text'),'창문을 열다')
        self.assertEqual(self.store.library(),[])
    def test_old_song_result_bad_token_and_web_origin_cannot_change_overlay(self):
        self.playing();old=self.payload();self.playing('Next Song')
        status,result=self.post(old)
        self.assertEqual(status,200);self.assertEqual(result['status'],'stale')
        self.assertEqual(self.post(self.payload(token='wrong'))[0],400)
        self.assertEqual(self.post(self.payload(),{'Origin':'https://example.com','Content-Type':'application/json','X-Reader-Extension':'a'*32})[0],403)
        self.assertIsNone(self.app.song)

    def test_spotify_originals_display_in_memory_and_translation_keeps_manual_sync(self):
        self.playing();lines=(Line('窓を開ける','まどをあける',''),)
        self.app.messages.put(('spotify_lyrics',(self.app.playback.key,lines)));self.tick()
        self.assertEqual(self.app.original_label.cget('text'),'窓を開ける')
        self.app.offset(.75);self.app.align_now();times=list(self.app.timing['times'])
        self.assertEqual(self.post(self.payload())[0],200);self.tick()
        self.assertEqual(self.app.timing['offset'],.75);self.assertEqual(self.app.timing['times'],times)
        self.assertEqual(self.app.korean_label.cget('text'),'창문을 열다')
        self.assertEqual(self.app.reader_bridge.snapshot()['receipt']['status'],'displayed')
        self.assertEqual(self.store.library(),[])

    def test_partial_refresh_keeps_translation_timing_and_developer_selection(self):
        self.playing()
        full=TEXT+'\n花を描く\n하나오 카쿠\n꽃을 그리다'
        self.assertEqual(self.post(self.payload(text=full))[0],200);self.tick()
        self.app.open_developer_mode()
        dialog = self.app.manual_sync_dialog
        dialog.select_for_edit(1)
        self.app.offset(.25)
        timing=self.app.timing
        # A temporarily missing meaning must not erase the same phrase's translation.
        partial='窓を開ける\n마도오 아케루\n花を描く\n하나오 카쿠\n새 번역'
        self.assertEqual(self.post(self.payload(text=partial))[0],200);self.tick();self.root.update()
        self.assertEqual([line.korean for line in self.app.display_lines],['창문을 열다','새 번역'])
        self.assertEqual(self.app.korean_label.cget('text'),'창문을 열다')
        self.assertEqual(dialog.selected_index(), 1)
        self.assertIs(self.app.timing, timing)
        self.assertTrue(self.app.follow.get())
        self.playing('Next Song')
        self.assertEqual(self.post(self.payload(text=partial,url='https://namu.wiki/w/Next%20Song'))[0],200);self.tick()
        self.assertEqual(self.app.display_lines[0].korean,'')

    def test_saved_song_partial_refresh_retains_current_translations(self):
        lines=(Line('窓を開ける','',''),Line('花を描く','',''))
        song=Song('saved-refresh','Test Song','','',lines)
        self.store.save_library([song]);self.app.songs=[song]
        self.link_saved_song(song);self.playing()
        self.app.receive_reader_lyrics(self.app.reader_bridge.snapshot()['track'],
            (Line('窓を開ける','','창문을 열다'),Line('花を描く','','꽃을 그리다')))
        self.app.receive_reader_lyrics(self.app.reader_bridge.snapshot()['track'],
            (Line('窓を開ける','',''),Line('花を描く','','새 번역')))
        self.assertEqual([line.korean for line in self.app.display_lines],['창문을 열다','새 번역'])
        self.assertEqual(self.store.library()[0],song)

    def test_same_row_status_updates_keep_existing_canvas_items(self):
        self.playing();self.assertEqual(self.post(self.payload())[0],200);self.tick();self.root.update()
        labels=(self.app.reading_label,self.app.original_label,self.app.korean_label)
        before=[label.find_withtag('lyric') for label in labels]
        for i in range(4):
            self.app.messages.put(('screen_status','상태 변경 '+str(i)));self.tick();self.root.update()
            self.assertEqual([label.find_withtag('lyric') for label in labels],before)
            self.assertEqual(self.app.korean_label.cget('text'),'창문을 열다')

    def test_spotify_highlight_overrides_estimate_and_clears_on_track_change(self):
        self.playing();lines=(Line('静かな空を見上げる','',''),Line('新しい道を歩いている','',''))
        self.app.messages.put(('spotify_lyrics',(self.app.playback.key,lines)));self.tick()
        self.app.receive_spotify_active(self.app.playback.key,tuple(x.original for x in lines),1)
        self.app.render();self.assertEqual(self.app.displayed_index(),1)
        self.assertTrue(self.app.following_spotify())
        self.assertEqual(self.app.mode_label.cget('text'),'')
        self.app.spotify_observed_at-=3;self.assertFalse(self.app.following_spotify())
        self.playing('Next Song');self.assertIsNone(self.app.spotify_current)

    def test_saved_song_timestamps_win_over_spotify_highlight_and_translation_refresh(self):
        lines=(Line('静かな空を見上げる','',''),Line('新しい道を歩いている','',''))
        song=Song('saved','Test Song','','',lines)
        self.store.save_library([song]);self.app.songs=[song]
        self.link_saved_song(song)
        self.store.save_timing(song,{'times':[10,60],'offset':0,'source':'직접 기록'})
        before=(self.store.directory/'timings'/'saved.json').read_bytes()
        self.playing();self.app.receive_spotify_active(self.app.playback.key,tuple(x.original for x in lines),1)
        self.app.render();self.assertEqual(self.app.displayed_index(),0)
        self.assertFalse(self.app.following_spotify())
        self.assertTrue(self.app.has_saved_sync())
        self.app.live_lyrics.receive(self.app.playback,song,tuple(Line(x.original,'','테스트 번역') for x in lines))
        self.app.select_song(song);self.app.receive_spotify_active(self.app.playback.key,tuple(x.original for x in lines),1)
        self.assertEqual(self.app.displayed_index(),0)
        self.assertEqual(before,(self.store.directory/'timings'/'saved.json').read_bytes())

    def test_translation_refresh_preserves_manual_row_and_recording_cursor(self):
        lines=tuple(Line(text,'','') for text in ('静かな空を見上げる','新しい道を歩いている','ここから先へ進もう'))
        song=Song('same','Test Song','','',lines)
        self.store.save_library([song]);self.app.songs=[song]
        self.link_saved_song(song);self.playing()
        self.app.move(1);selected=self.app.displayed_index();cursor=self.app.cursor
        self.app.open_editor();self.root.update()
        self.app.select_editor_row(selected);self.root.update()
        self.app.time_entry.delete(0,'end');self.app.time_entry.insert(0,'00:23.45')
        self.assertFalse(self.app.follow.get())
        self.app.receive_reader_lyrics(self.app.reader_bridge.snapshot()['track'],
                                      tuple(Line(x.original,'','새 번역') for x in lines))
        self.assertEqual(self.app.displayed_index(),selected)
        self.assertEqual(self.app.cursor,cursor)
        self.assertFalse(self.app.follow.get())
        self.assertEqual(self.app.korean_label.cget('text'),'새 번역')
        self.root.update()
        self.assertEqual(self.app.tree.selection(),(str(selected),))
        self.assertEqual(self.app.time_entry.get(),'00:23.45')

    def native_sample_song(self):
        lines=tuple(Line(text,'','') for text in ('静かな空を見上げる','新しい道を歩いている','ここから先へ進もう'))
        song=Song('sampled','Test Song','','',lines)
        self.store.save_library([song]);self.app.songs=[song]
        self.link_saved_song(song)
        with patch('app.time.monotonic',return_value=100):self.playing()
        self.assertEqual(self.app.song.id,song.id)
        self.assertTrue(self.app.is_bound())
        return tuple(x.original for x in lines)

    def test_queued_highlights_learn_observation_time_instead_of_ui_delivery_time(self):
        originals=self.native_sample_song();key=self.app.playback.key
        with patch('app.time.monotonic',return_value=100.3):
            self.app.receive_spotify_active(key,originals,0,100.1,20.1)
        with patch('app.time.monotonic',return_value=101.2):
            self.app.receive_spotify_active(key,originals,1,100.4,20.4)
        self.assertEqual(self.app.native_timing.times[1],20.4)
        self.assertEqual(self.app.spotify_anchor,(1,20.4))
        self.assertEqual(self.app.spotify_observed_at,100.4)

    def test_pre_seek_and_expired_highlights_cannot_restore_old_position(self):
        originals=self.native_sample_song();key=self.app.playback.key
        with patch('app.time.monotonic',return_value=101):
            self.app.set_playback(Playback(title='Test Song',base=60,duration=90,playing=True,captured=101))
            self.app.receive_spotify_active(key,originals,0,100.4,20.4)
        self.assertIsNone(self.app.spotify_source_index)
        with patch('app.time.monotonic',return_value=104):
            self.app.receive_spotify_active(key,originals,1,101.1,60.1)
        self.assertIsNone(self.app.spotify_source_index)

    def test_slow_scan_can_display_but_cannot_create_a_precise_start(self):
        originals=self.native_sample_song();key=self.app.playback.key
        with patch('app.time.monotonic',return_value=100.3):self.app.receive_spotify_active(key,originals,0,100.1,20.1)
        with patch('app.time.monotonic',return_value=100.6):self.app.receive_spotify_active(key,originals,1,100.5,20.5,False)
        self.assertEqual(self.app.spotify_current,1)
        self.assertEqual(self.app.native_timing.times,[None]*3)

    def test_delayed_record_key_uses_press_time_even_after_new_media_snapshot(self):
        self.native_sample_song()
        self.app.open_developer_mode()
        dialog = self.app.manual_sync_dialog
        with patch('app.time.monotonic',return_value=101):
            self.app.set_playback(Playback(title='Test Song',base=21,duration=90,playing=True,captured=101))
        with patch('app.time.monotonic',return_value=101.4):
            self.app.messages.put(('stamp_at',100.7));self.tick()
        self.assertIsNone(self.app.timing['times'][0])
        self.assertEqual(self.app.timing['times'][1],20.7)
        self.assertEqual(dialog.selected_index(), 1)
        self.assertTrue(self.app.follow.get())

    def test_stale_record_key_does_not_write_into_the_new_playback_position(self):
        self.native_sample_song()
        self.app.open_developer_mode()
        dialog = self.app.manual_sync_dialog
        with patch('app.time.monotonic',return_value=101):
            self.app.set_playback(Playback(title='Test Song',base=60,duration=90,playing=True,captured=101))
            self.app.messages.put(('stamp_at',100.7));self.tick()
        self.assertEqual(self.app.timing['times'],[None]*3)
        self.assertEqual(dialog.selected_index(), 0)
        self.assertIn('저장하지 못했습니다', dialog.status.cget('text'))

    def test_record_key_preserves_conflicting_manual_time_and_edit_selection(self):
        self.native_sample_song()
        timing={'times':[17.447,18.056,18.144],'origins':['manual']*3,'offset':0}
        self.app.save_current_timing(timing);self.app.timing=timing
        path=self.store.directory/'timings/sampled.json'
        before=path.read_bytes()
        self.app.playback=Playback(title='Test Song',base=18.3,duration=90,captured=100)
        with patch('app.time.monotonic',return_value=100.1):
            self.app.open_developer_mode()
            dialog = self.app.manual_sync_dialog
            self.app.messages.put(('stamp_at',100));self.tick()
        self.assertEqual(dialog.selected_index(), 0)
        self.assertEqual(self.app.displayed_index(), 2)
        self.assertTrue(self.app.follow.get())
        self.assertIn('저장 시각과 겹칩니다', dialog.status.cget('text'))
        self.assertEqual(self.app.timing,timing)
        self.assertEqual(path.read_bytes(),before)

    def test_remembered_native_time_does_not_shift_when_first_seen_mid_line(self):
        originals=self.native_sample_song()
        self.app.timing={'times':[10,20,30],'offset':0,'source':'음원 분석'}
        self.app.native_timing.select(self.app.song,90)
        self.app.native_timing.times=[None,20,30]
        with patch('app.time.monotonic',return_value=100):
            self.app.playback=Playback(title='Test Song',base=25,duration=90,playing=True,captured=100)
            self.app.receive_spotify_active(self.app.playback.key,originals,1)
            self.assertEqual(self.app.displayed_index(),1)
        with patch('app.time.monotonic',return_value=106):
            self.assertFalse(self.app.following_spotify())
            self.assertEqual(self.app.displayed_index(),2)

    def test_native_mapping_is_cached_and_invalidated_when_song_changes(self):
        self.playing();lines=(Line('静かな空を見上げる','',''),Line('新しい道を歩いている','',''))
        self.app.messages.put(('spotify_lyrics',(self.app.playback.key,lines)));self.tick()
        originals=tuple(x.original for x in lines)
        self.app.receive_spotify_active(self.app.playback.key,originals,0)
        with patch('app.native_line_mapping',side_effect=AssertionError('mapping must be reused')):
            self.app.receive_spotify_active(self.app.playback.key,originals,1)
        self.assertEqual(self.app.displayed_index(),1)
        self.app.select_song(self.app.song);self.assertIsNone(self.app.spotify_mapping)

    def test_unreviewed_audio_timing_does_not_override_actual_spotify_highlight(self):
        lines=(Line('静かな空を見上げる','',''),Line('新しい道を歩いている','',''))
        song=Song('auto','Test Song','','',lines);self.app.songs=[song]
        self.link_saved_song(song)
        self.store.save_timing(song,{'times':[10,60],'offset':0,'source':'음원 분석'})
        self.playing();self.app.receive_spotify_active(self.app.playback.key,tuple(x.original for x in lines),1)
        self.assertTrue(self.app.following_spotify())
        self.assertEqual(self.app.displayed_index(),1)
        self.assertEqual(self.store.timing(song)['times'],[10,60])
        self.app.offset(.25)
        self.assertTrue(self.app.has_saved_sync())
        self.assertFalse(self.app.following_spotify())

    def test_observed_transition_replaces_auto_time_after_highlight_disappears(self):
        from native_timing import NativeTiming
        lines=(Line('静かな空を見上げる','',''),Line('新しい道を歩いている','',''))
        song=Song('learn','Test Song','','',lines);self.app.songs=[song]
        self.link_saved_song(song)
        self.store.save_timing(song,{'times':[1,60],'offset':0,'source':'음원 분석'})
        originals=tuple(x.original for x in lines)
        with patch('app.time.monotonic',return_value=600):
            self.app.set_playback(Playback(title='Test Song',base=10,duration=90,playing=True,captured=600))
            self.app.receive_spotify_active(self.app.playback.key,originals,0)
        with patch('app.time.monotonic',return_value=600.1):
            self.app.receive_spotify_active(self.app.playback.key,originals,1)
        self.app.native_timing=NativeTiming(self.store.directory)
        self.app.spotify_current=None;self.app.spotify_anchor=None
        with patch('app.time.monotonic',return_value=620):
            self.assertEqual(self.app.displayed_index(),1)
        self.assertEqual(self.store.timing(song)['times'],[1,60])

    def test_no_spotify_lyrics_can_be_timed_and_reloaded_without_saving_text(self):
        self.playing('Example-2022ver')
        self.assertEqual(self.post(self.payload(url='https://namu.wiki/w/Example(Series)',match_mode='title'))[0],200)
        self.tick();self.app.align_now();self.app.offset(.5)
        expected=dict(self.app.timing);self.playing('Other');self.playing('Example-2022ver')
        self.assertEqual(self.post(self.payload(url='https://namu.wiki/w/Example(Series)',match_mode='title'))[0],200)
        self.tick()
        self.assertEqual(self.app.timing['times'],expected['times']);self.assertEqual(self.app.timing['offset'],.5)
        self.assertTrue(self.app.has_saved_sync());self.assertEqual(self.store.library(),[])

    def test_minimized_spotify_discards_frozen_highlight_and_uses_media_clock(self):
        self.playing();lines=(Line('静かな空を見上げる','',''),Line('新しい道を歩いている','',''))
        self.app.messages.put(('spotify_lyrics',(self.app.playback.key,lines)));self.tick()
        self.app.receive_spotify_active(self.app.playback.key,tuple(x.original for x in lines),1)
        self.assertTrue(self.app.following_spotify())
        self.app.messages.put(('spotify_tracking',(self.app.playback.key,'background_clock')));self.tick()
        self.assertFalse(self.app.following_spotify());self.assertEqual(self.app.displayed_index(),1)
        self.app.set_playback(Playback(title='Test Song',base=80,duration=90,playing=False,captured=time.monotonic()))
        self.assertEqual(self.app.displayed_index(),1)

    def test_native_matching_moves_to_nearby_line_without_warping_to_repeated_phrase(self):
        self.playing();lines=tuple(Line(text,'','') for text in ['静かな空を見上げる','新しい道を歩いている','ここから先へ進もう','静かな空を見上げる'])
        self.app.messages.put(('spotify_lyrics',(self.app.playback.key,lines)));self.tick()
        source=tuple(line.original for line in lines)
        self.app.receive_spotify_active(self.app.playback.key,source,0)
        # A later exact repeat must not take precedence over ordinary forward order.
        self.app.spotify_mapping[1][1]=3
        self.app.receive_spotify_active(self.app.playback.key,source,1)
        self.assertEqual(self.app.displayed_index(),1)
        self.app.receive_spotify_active(self.app.playback.key,source,0)
        self.assertEqual(self.app.displayed_index(),1)
        self.app.spotify_observed_at-=3
        self.assertEqual(self.app.displayed_index(),1)

    def test_manual_seek_resets_order_guard(self):
        self.playing();lines=(Line('静かな空を見上げる','',''),Line('新しい道を歩いている','',''))
        self.app.messages.put(('spotify_lyrics',(self.app.playback.key,lines)));self.tick()
        source=tuple(line.original for line in lines)
        self.app.receive_spotify_active(self.app.playback.key,source,1)
        self.app.set_playback(Playback(title='Test Song',base=0,duration=90,playing=False,captured=time.monotonic()))
        self.app.receive_spotify_active(self.app.playback.key,source,0)
        self.assertEqual(self.app.displayed_index(),0)

    def test_missed_highlight_samples_catch_up_to_actual_source_index(self):
        self.playing();lines=tuple(Line(text,'','') for text in ['静かな空を見上げる','新しい道を歩いている','ここから先へ進もう','遠い街へ向かう','星を数えて眠る'])
        self.app.messages.put(('spotify_lyrics',(self.app.playback.key,lines)));self.tick()
        source=tuple(line.original for line in lines)
        self.app.receive_spotify_active(self.app.playback.key,source,0)
        self.app.receive_spotify_active(self.app.playback.key,source,3)
        self.assertEqual(self.app.displayed_index(),3)

    def test_fresh_highlight_corrects_an_estimate_that_has_run_ahead(self):
        self.playing();lines=tuple(Line(text,'','') for text in ['静かな空を見上げる','新しい道を歩いている','ここから先へ進もう','遠い街へ向かう'])
        self.app.messages.put(('spotify_lyrics',(self.app.playback.key,lines)));self.tick()
        source=tuple(line.original for line in lines)
        self.app.receive_spotify_active(self.app.playback.key,source,1)
        self.app.spotify_observed_at-=3
        with patch.object(self.app,'displayed_index',return_value=3):
            self.app.receive_spotify_active(self.app.playback.key,source,1)
        self.assertEqual(self.app.displayed_index(),1)
        self.app.receive_spotify_active(self.app.playback.key,source,2)
        self.assertEqual(self.app.displayed_index(),2)

    def test_different_line_breaks_do_not_limit_native_progress_to_one_row(self):
        self.playing();lines=tuple(Line(text,'','') for text in ['静かな空を見上げる','新しい道を歩いている','ここから先へ進もう','遠い街へ向かう'])
        self.app.messages.put(('spotify_lyrics',(self.app.playback.key,lines)));self.tick()
        source=(lines[0].original,lines[1].original+' '+lines[2].original,lines[3].original)
        self.app.receive_spotify_active(self.app.playback.key,source,0)
        self.app.receive_spotify_active(self.app.playback.key,source,1)
        self.app.receive_spotify_active(self.app.playback.key,source,2)
        self.assertEqual(self.app.displayed_index(),3)

    def test_split_spotify_rows_hold_one_translation_and_continue_through_the_ending(self):
        self.playing()
        lines=tuple(Line(text,'','번역') for text in (
            '前奏が終わる','長い空を見渡す 小さな雲が流れていく','次の季節へ歩いてゆこう','最後の明日へ'))
        self.app.receive_reader_lyrics(self.app.reader_bridge.snapshot()['track'],lines)
        source=('前奏が終わる','長い空を見渡す','小さな雲が','流れていく','次の季節へ歩いてゆこう','最後の明日へ')
        actual=[]
        with patch.object(self.app.native_timing,'observe') as learn:
            for index in range(len(source)):
                self.app.receive_spotify_active(self.app.playback.key,source,index)
                actual.append(self.app.displayed_index())
                self.assertEqual(self.app.spotify_source_index,index)
        self.assertEqual(actual,[0,1,1,1,2,3])
        self.assertEqual([call.args[-1] for call in learn.call_args_list][1:4],[False]*3)
        self.assertEqual(self.app.display_lines,lines)
        self.assertEqual(self.store.library(),[])

    def test_first_highlight_in_middle_of_split_row_finds_containing_translation(self):
        self.playing()
        lines=tuple(Line(text,'','번역') for text in (
            '前奏が終わる','長い空を見渡す 小さな雲が流れていく','次の季節へ歩いてゆこう','最後の明日へ'))
        self.app.receive_reader_lyrics(self.app.reader_bridge.snapshot()['track'],lines)
        source=('前奏が終わる','長い空を見渡す','小さな雲が','流れていく','次の季節へ歩いてゆこう','最後の明日へ')
        self.app.receive_spotify_active(self.app.playback.key,source,3)
        self.assertEqual(self.app.spotify_current,1)

    def test_unmatched_backing_rows_do_not_consume_later_translations(self):
        self.playing()
        lines=tuple(Line(text,'','번역') for text in ('Red sunrise','Blue river','Green forest','Silver clouds'))
        self.app.receive_reader_lyrics(self.app.reader_bridge.snapshot()['track'],lines)
        source=('Red sunrise','zzzzzzzz','qqqqqqqq','xxxxxxxx','Blue river','Green forest','Silver clouds')
        actual=[]
        with patch.object(self.app.native_timing,'observe') as learn:
            for index in range(len(source)):
                self.app.receive_spotify_active(self.app.playback.key,source,index)
                actual.append(self.app.displayed_index())
                self.assertEqual(self.app.spotify_source_index,index)
                if index in (1,2,3):
                    self.app.receive_spotify_active(self.app.playback.key,source,index)
                    self.assertEqual(self.app.reader_bridge.snapshot()['tracking']['status'],'holding')
        self.assertEqual(actual,[0,0,0,0,1,2,3])
        self.assertFalse(any(call.args[-1] for call in learn.call_args_list if call.args[2] in (1,2,3)))

    def test_verified_next_phrase_can_recover_after_approximate_anchor_ran_ahead(self):
        self.playing()
        lines=tuple(Line(text,'','번역') for text in ('Red sunrise','Blue river','Green forest','Silver clouds'))
        self.app.receive_reader_lyrics(self.app.reader_bridge.snapshot()['track'],lines)
        source=tuple(line.original for line in lines)
        self.app.receive_spotify_active(self.app.playback.key,source,0)
        self.app.spotify_anchor=(3,self.app.playback.position());self.app.spotify_current=3
        self.app.receive_spotify_active(self.app.playback.key,source,1)
        self.assertEqual(self.app.spotify_current,1)

    def test_click_lyrics_and_plain_arrows_keep_the_overlay_following(self):
        self.playing();lines=tuple(Line(text,'','번역') for text in ['静かな空を見上げる','新しい道を歩いている','ここから先へ進もう'])
        self.app.messages.put(('spotify_lyrics',(self.app.playback.key,lines)));self.tick()
        source=tuple(line.original for line in lines)
        self.app.receive_spotify_active(self.app.playback.key,source,1)
        self.app.transparent.set(False);self.app.apply_appearance()
        self.app.chrome_available=True;self.app.toggle_chrome();self.root.update()
        timing = self.app.timing
        cursor = self.app.cursor
        with patch.object(self.app.monitor, 'request_seek', return_value=False):
            for widget in (self.app.original_label, self.app.korean_label, self.app.reading_label,
                           self.app.lyric_frame, self.app.backdrop.window):
                widget.event_generate('<ButtonPress-1>', x=5, y=5)
                widget.event_generate('<ButtonRelease-1>', x=5, y=5)
                self.root.update()
                self.assertIs(self.root.focus_get(), self.root)
                self.root.event_generate('<Right>')
                self.root.event_generate('<Left>')
                self.root.update()
                self.assertEqual(self.app.displayed_index(), 1)
                self.assertTrue(self.app.follow.get())
                self.assertIsNone(self.app.singer_input_index)
                self.assertIsNone(self.app.manual_sync_dialog)
                self.assertEqual(self.app.cursor, cursor)
                self.assertIs(self.app.timing, timing)
        self.app.messages.put(('stamp_at', time.monotonic()))
        self.tick()
        self.assertIs(self.app.timing, timing)
        self.app.receive_spotify_active(self.app.playback.key,source,2)
        self.assertEqual(self.app.displayed_index(), 2)
        self.assertTrue(self.app.follow.get())
        self.assertEqual(self.app.timing['times'], [None, None, None])

    def test_modified_arrows_and_editor_inputs_do_not_select_lyric_rows(self):
        from types import SimpleNamespace
        with patch.object(self.app,'move') as move:
            for state in (1,4,0x20000):
                self.app.lyric_arrow(SimpleNamespace(state=state,keysym='Right'))
            self.app.locked=True
            self.app.lyric_arrow(SimpleNamespace(state=0,keysym='Left'))
            self.app.locked=False
            move.assert_not_called()
        editor=tk.Toplevel(self.root);entry=tk.Entry(editor);entry.pack();self.root.update()
        with patch.object(self.app,'move') as move:
            entry.focus_force();entry.event_generate('<Right>');self.root.update()
            move.assert_not_called()
        editor.destroy()

    def test_click_through_applies_to_both_text_and_background(self):
        self.app.lock_available=True
        self.app.toggle_lock();self.assertTrue(self.app.backdrop.click_through)
        self.app.toggle_lock();self.assertFalse(self.app.backdrop.click_through)

    def test_user_assigned_plain_arrow_wins_over_local_row_navigation(self):
        from types import SimpleNamespace
        self.app.hotkeys.bindings['stamp']={'key':0x27,'modifiers':0,'shift':'any'}
        with patch.object(self.app,'move') as move:
            self.assertEqual(self.app.lyric_arrow(SimpleNamespace(state=0,keysym='Right')),'break')
            move.assert_not_called()

    def test_actual_windows_extended_arrow_state_is_not_mistaken_for_alt(self):
        from types import SimpleNamespace
        with patch.object(self.app,'move') as move:
            self.app.lyric_arrow(SimpleNamespace(state=0x40008,keysym='Right'))
            move.assert_called_once_with(1)
