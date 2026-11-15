"""AI translation round trips must leave recording identity and clocks intact."""
import json
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from app import LyricsApp, display_translation
from batch_translation import BatchTranslationStore
from lyrics import Line, Song
from media import Playback
from storage import Store


class EnglishDisplayTests(unittest.TestCase):
    def test_english_only_prefers_exact_original_without_duplication(self):
        line = Line('True & False!', '트루 앤 폴스', '참과 거짓!')
        self.assertEqual(display_translation(line), line.original)
        self.assertEqual(display_translation(line, original_visible=True), '')

    def test_mixed_line_uses_original_until_english_is_preserved(self):
        original = '駆けあがれWonder Scale'
        invalid = Line(original, '', '달려 올라가, 경이로운 음계를')
        corrected = Line(original, '', '달려 올라가, Wonder Scale을')
        self.assertEqual(display_translation(invalid), original)
        self.assertEqual(display_translation(invalid, original_visible=True), '')
        self.assertEqual(display_translation(corrected, original_visible=True), corrected.korean)


class BatchTranslationOverlayTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(self.temp.name)
        self.store.save_settings({'online':False,'auto_audio':False,'web_auto':False,
                                  'borderless':False,'reader_mode':'chrome','use_estimate':False})
        self.lines=(Line('青い空を見る','あおいそらをみる','기존 번역'),Line('風の音を聞く','',''))
        self.song=Song('saved-song','Batch test','Singer','Album',self.lines)
        self.store.save_library([self.song])
        self.store.save_timing(self.song,{'times':[10,20],'offset':.25,'origins':['manual','manual']})
        self.root=tk.Tk();self.root.withdraw()
        self.app=LyricsApp(self.root,self.store,start_services=False)
        self.playback=Playback('Batch test','Singer','Album',12,90,False,time.monotonic())
        self.app.set_playback(self.playback)

    def tearDown(self):
        self.app.close();self.temp.cleanup()

    def request(self):
        return self.app.batch_translations.prepare(self.app.collect_translation_documents(),5)[0]

    def reply(self,batch):
        return json.dumps({'format':'spotify-lyrics-translation-v1','batch_id':batch['batch_id'],
            'songs':[{'id':doc['id'],'lines':[{'index':i,'original':row.original,'korean':'새 번역 '+str(i)}
                     for i,row in enumerate(doc['lines'],1) if row.original.strip() and not row.korean.strip()]}
                     for doc in batch['songs']]},ensure_ascii=False)

    def test_library_round_trip_updates_display_and_keeps_exact_song_and_manual_timing(self):
        batch=self.request();identity=self.app.song;timing=self.app.timing
        files=[self.store.directory/'library.json',self.store.directory/'timings/saved-song.json',
               self.store.directory/'settings.json']
        before=[path.read_bytes() for path in files]
        self.app.manual_index=1;self.app.follow.set(False)
        result=self.app.apply_translation_reply(self.reply(batch))
        self.assertEqual((result['songs'],result['lines']),(1,1))
        self.assertEqual([row.korean for row in self.app.display_lines],['기존 번역','새 번역 2'])
        self.assertIs(self.app.song,identity);self.assertIs(self.app.timing,timing)
        self.assertEqual(self.app.manual_index,1);self.assertFalse(self.app.follow.get())
        self.assertEqual([path.read_bytes() for path in files],before)
        self.app.batch_translations=BatchTranslationStore(self.store.directory)
        self.app.select_song(None);self.app.select_song(self.song)
        self.assertEqual(self.app.display_lines[1].korean,'새 번역 2')
        self.assertEqual(self.app.timing['times'],[10,20])

    def test_copied_reply_flows_from_dialog_poll_to_real_store_and_overlay(self):
        self.app.open_batch_translation();dialog=self.app.translation_dialog
        deadline=time.monotonic()+5
        while dialog.preparing and time.monotonic()<deadline:
            self.root.update();time.sleep(.01)
        self.assertFalse(dialog.preparing)
        batch=dialog.batches[0]
        if dialog._poll_id is not None:
            dialog.window.after_cancel(dialog._poll_id);dialog._poll_id=None
        with patch.object(dialog,'read_clipboard',return_value=self.reply(batch)):
            dialog.poll_clipboard()
        self.assertEqual(self.app.display_lines[1].korean,'새 번역 2')
        self.assertEqual(dialog.progress[batch['batch_id']],0)
        self.assertTrue(self.app.batch_translations.imports_path.is_file())
        self.assertEqual(self.store.timing(self.song)['times'],[10,20])
        self.assertIn('자동 적용 · 새 번역 저장 완료 · 1곡 · 1구절',dialog.result_status.get())
        self.assertIn('1곡 · 1구절',dialog.saved_status.get())
        before=self.app.batch_translations.imports_path.read_bytes()
        dialog.reply.insert('1.0',self.reply(batch));dialog.apply_manual()
        self.assertIn('이미 반영된 답변 · 1곡 · 1구절 확인',dialog.result_status.get())
        self.assertNotIn('0곡',dialog.result_status.get())
        self.assertIn('이미 반영된 답변',self.app.status.cget('text'))
        self.assertEqual(self.app.batch_translations.imports_path.read_bytes(),before)

    def test_saved_song_can_be_translated_without_playback(self):
        self.app.set_playback(Playback());self.app.select_song(self.song)
        batch=self.request();self.app.apply_translation_reply(self.reply(batch))
        self.assertEqual(self.app.display_lines[1].korean,'새 번역 2')
        self.assertEqual(self.app.song.fingerprint,self.song.fingerprint)

    def test_developer_imports_spotify_originals_and_manages_saved_translation(self):
        track={'id':'F'*22,'title':'Spotify test','artists':['Singer'],'album':'Spotify album','duration':90}
        source=self.app.spotify_translation_sources
        with patch('spotify_translation_sources._public_track', return_value=track), \
                patch.object(self.app.background_lyrics,'spotify_authenticated',True), \
                patch.object(self.app.background_lyrics,'job') as worker:
            source.start('https://open.spotify.com/track/'+track['id'])
            deadline=time.monotonic()+2
            while not worker.send.called and time.monotonic()<deadline:
                source.tick();time.sleep(.01)
            command=worker.send.call_args.args[0]
            self.app.receive_background_lyrics(worker,dict(kind='batch_lyrics_result',
                provider_id=track['id'],request_id=command['request_id'],
                lines=[{'text':'風の音を聞く','time':None}]))
        self.assertEqual(source.imported,1)
        workspace=self.app.workspace
        workspace.developer('open',{'tool':'translation'})
        dialog=self.app.translation_dialog
        dialog.poll_spotify_import()
        deadline=time.monotonic()+5
        while dialog.preparing and time.monotonic()<deadline:
            self.root.update();time.sleep(.01)
        self.assertFalse(dialog.preparing)
        self.assertEqual([doc['title'] for doc in dialog.batches[0]['songs']],['Spotify test'])
        self.assertIn('風の音を聞く',dialog.batches[0]['prompt'])
        dialog.apply_reply(self.reply(dialog.batches[0]))
        workspace.developer('open',{'tool':'translationResources'})
        self.assertIsNotNone(self.app.translation_resource_dialog)
        item=next(item for item in self.app.list_translation_resources() if item['title']=='Spotify test')
        self.assertEqual(item['translated'],1)
        self.app.set_translation_resource_included(item['id'],False)
        self.assertFalse(self.app.collect_translation_documents(spotify_only=True))
        self.assertNotIn('Spotify test',[doc['title'] for doc in self.app.collect_translation_documents()])
        self.assertEqual(self.app.translation_resource_details(item['id'])[0]['korean'],'새 번역 1')
        self.assertEqual(self.app.playlist_lyrics.store.load(track['id'])['direct']['times'],[None])
        self.app.set_translation_resource_included(item['id'],True)
        self.assertEqual(self.app.collect_translation_documents(spotify_only=True)[0]['title'],'Spotify test')

    def test_personal_resources_show_existing_manual_rows_and_ignore_provider_clocks(self):
        from user_annotations import SingerEdits, SingerRoster
        from lyric_annotations import LyricTextEdits
        from storage import atomic_json
        self.app.settings.setdefault('lyric_sources', {})[self.song.id]={'user_override':True}
        self.store.save_timing(self.song,{'times':[1,6],'origins':['manual','audio'],'offset':.25})
        key=(self.playback.key,tuple(line.original for line in self.lines))
        text=LyricTextEdits(self.store);text.select(key)
        text.save(0,'青い空を眺める')
        text.save(0,'직접 쓴 번역',field='korean')
        roster=SingerRoster(self.store.directory);roster.select(self.playback.key);roster.save(('가수 A','가수 B'))
        singers=SingerEdits(self.store.directory);singers.select(key);singers.save(0,('가수 B',))
        atomic_json(self.store.directory/'live_timings'/('f'*64+'.json'),
                    {'times':[3],'origins':['manual'],'offset':0})
        self.app.lyric_cache.save(Playback('자동 가사','가수','앨범',duration=90), self.lines)
        result=self.app.list_translation_resources()
        self.assertEqual([item['title'] for item in result],['Batch test','제목 미확인'])
        item=result[0]
        self.assertEqual((item['manual_sync'],item['translated'],item['original'],item['performers']),(1,1,2,3))
        self.assertEqual(item['roster'],('가수 A','가수 B'))
        first,second=self.app.translation_resource_details(item['id'])
        self.assertEqual((first['original'],first['korean'],first['time'],first['performers']),
                         ('青い空を眺める','직접 쓴 번역',1.25,('가수 B',)))
        self.assertIsNone(second['time'])
        self.assertEqual(self.app.translation_resource_details(result[1]['id'])[0]['time'],3)
        self.assertFalse(self.app.translation_resource_actions(result[1]['id']))
        self.assertIn('timing',self.app.translation_resource_actions(item['id']))
        self.app.update_translation_resource(item['id'],0,'timing','00:02.250')
        self.assertEqual(self.store.timing(self.song)['times'],[2,6])
        self.app.update_translation_resource(item['id'],0,'korean','수정한 번역')
        self.app.update_translation_resource(item['id'],0,'singers',['가수 A'])
        self.app.update_translation_resource(item['id'],0,'original',None)
        self.app.update_translation_resource(item['id'],None,'roster',None)
        self.app.list_translation_resources()
        changed=self.app.translation_resource_details(item['id'])[0]
        self.assertEqual((changed['original'],changed['korean'],changed['time'],changed['performers']),
                         (self.lines[0].original,'수정한 번역',2.25,('가수 A',)))
        self.assertFalse(roster.path.exists())
        self.app.update_translation_resource(item['id'],0,'timing',None)
        self.assertEqual(self.store.timing(self.song)['times'],[None,6])
        self.assertEqual(self.store.library()[0].lines,self.lines)

    def test_reviewed_translations_show_localized_calls_with_corrected_originals(self):
        self.app.settings.setdefault('lyric_sources', {})[self.song.id] = {'user_override': True}
        song = Song(self.song.id, self.song.title, self.song.artist, self.song.album, (
            Line('やるじゃない！ Yeah！', '', '제법이잖아! 예!'),
            Line('あたしもだよ！', '', '나도 좋아해! 예~!'),
            Line('Oh Yeah', '', '오 예!'),
        ))
        self.app.songs = [song]
        self.store.save_timing(song, {'times': [10, 20, 30], 'offset': 0})
        self.app.select_song(song)
        self.app.lyric_text_edits.save(1, 'あたしもだよ！ Yeah~!')
        self.app.open_manual_sync(search=False)
        dialog = self.app.manual_sync_dialog
        dialog.load()
        self.app.open_editor()
        self.assertEqual(self.app.korean_label.cget('text'), '제법이잖아! 예!')
        self.assertEqual(dialog.rows.set('1', 'lyric'), 'あたしもだよ！ Yeah~!')
        self.assertEqual(dialog.rows.set('1', 'translation'), '나도 좋아해! 예~!')
        self.assertEqual(dialog.rows.set('2', 'translation'), '오 예!')
        self.assertEqual(self.app.tree.set('1', 'original'), 'あたしもだよ！ Yeah~!')
        self.assertEqual(self.app.tree.set('1', 'korean'), '나도 좋아해! 예~!')
        self.assertEqual(self.app.timing['times'], [10, 20, 30])

    def test_all_sources_and_duplicate_recordings_are_collected_without_source_writes(self):
        self.app.lyric_cache.save(self.playback,self.lines)
        other=Playback('Cache only','Other singer','Other album',duration=120)
        self.app.lyric_cache.save(other,self.lines)
        track={'id':'A'*22,'title':'Playlist only','artists':['Playlist singer'],'album':'Playlist album','duration':100}
        self.assertTrue(self.app.playlist_lyrics.store.save(track,self.lines,None))
        paths=list((self.store.directory/'lyrics_cache').glob('*.json'))
        paths+=list((self.store.directory/'playlist_lyrics/tracks').glob('*.json'))
        before=[path.read_bytes() for path in paths]
        documents=self.app.collect_translation_documents()
        self.assertEqual(sorted(doc['title'] for doc in documents),['Batch test','Cache only','Playlist only'])
        self.app.apply_translation_reply(self.reply(self.app.batch_translations.prepare(documents,5)[0]))
        self.assertEqual([path.read_bytes() for path in paths],before)
        self.app.set_playback(other)
        self.assertEqual(self.app.display_lines[1].korean,'새 번역 2')
        self.assertEqual(self.app.display_lines[0].korean,'기존 번역')

    def test_live_capture_survives_bounded_cache_eviction_and_keeps_provider_clock(self):
        self.app.set_playback(Playback('Live only','Singer','Album',12,90,False,time.monotonic()))
        direct={'source':'Spotify','lines':self.lines,'times':[10,20],'provider_id':'live'}
        self.app.receive_direct_lyrics(self.app.playback.key,direct)
        song=self.app.song;timing=self.app.timing;clock=self.app.direct_result
        batch=self.request();self.app.apply_translation_reply(self.reply(batch))
        self.assertIs(self.app.song,song);self.assertIs(self.app.timing,timing)
        self.assertIs(self.app.direct_result,clock);self.assertEqual(self.app.direct_times(),[10,20])
        for path in (self.store.directory/'lyrics_cache').glob('*.json'):path.unlink()
        documents=BatchTranslationStore(self.store.directory).documents()
        self.assertIn('Live only',[doc['title'] for doc in documents])

    def test_verified_playlist_metadata_alias_keeps_translation_after_playback_and_restart(self):
        track={'id':'B'*22,'title':'Alias Song','artists':['Singer'],'album':'Album','duration':100}
        self.assertTrue(self.app.playlist_lyrics.store.save(track,self.lines,None))
        batch=self.request()
        self.app.set_playback(Playback('ALIAS SONG','Singer','Album',12,100,False,time.monotonic()))
        self.assertIsNotNone(self.app.song)
        self.app.apply_translation_reply(self.reply(batch))
        self.assertEqual(self.app.display_lines[1].korean,'새 번역 2')

        self.app.set_playback(Playback())
        self.app.batch_translations=BatchTranslationStore(self.store.directory)
        self.app.set_playback(Playback('ALIAS SONG','Singer','Album',12,100,False,time.monotonic()))
        self.assertEqual(self.app.display_lines[1].korean,'새 번역 2')

    def test_cache_only_replay_is_archived_before_eviction(self):
        playback=Playback('Cached replay','Singer','Album',12,90,False,time.monotonic())
        self.app.lyric_cache.save(playback,self.lines)
        self.app.set_playback(playback)
        for path in (self.store.directory/'lyrics_cache').glob('*.json'):path.unlink()
        documents=BatchTranslationStore(self.store.directory).documents()
        self.assertIn('Cached replay',[doc['title'] for doc in documents])

    def test_playlist_translation_matches_full_lyrics_with_other_artist_credits(self):
        title='なんどでも笑おう - ミリオンライブ！Ver.'
        playback=Playback(title,'MILLIONSTARS','なんどでも笑おう【ミリオンライブ！盤】',
                          12,335.68,False,time.monotonic())
        track={'id':'A'*22,'title':title,'artists':['春日未来','最上静香','伊吹翼'],
               'album':'なんどでも笑おう (ミリオンライブ！盤)','duration':335.68}
        originals=tuple(Line(line.original,'','') for line in self.lines)
        translated=(Line(originals[0].original,'','하늘을 봐'),Line(originals[1].original,'','바람 소리를 들어'))
        playlist=self.app.playlist_lyrics.store
        self.assertTrue(playlist.save(track,translated,{'source':'Spotify','lines':translated,
                                                       'times':[1,2],'provider_id':track['id']}))
        self.assertEqual(playlist.find_annotations('なんどでも笑おう - Another Ver.',playback.album,
                                                   tuple(row.original for row in originals)),
                         tuple(row.korean for row in translated))
        other={**track,'id':'B'*22,'album':'Other release'}
        self.assertTrue(playlist.save(other,tuple(Line(row.original,'','다른 번역') for row in translated),None))
        self.assertIsNone(playlist.find(playback))
        self.assertEqual(playlist.find_annotations(title,playback.album,('Unrelated lyrics',)),())
        own={'source':'Spotify','lines':originals,'times':[10,20],'provider_id':'C'*22}
        self.assertTrue(self.app.lyric_cache.save(playback,originals,own))
        self.app.set_playback(playback)
        self.assertEqual(self.app.display_lines,translated)
        self.assertEqual(self.app.direct_times(),own['times'])
        self.assertEqual(self.app.direct_result[2]['provider_id'],own['provider_id'])

    def test_broken_optional_history_does_not_stop_render_or_cache(self):
        before=self.app.display_lines
        with patch.object(self.app.batch_translations,'fill',side_effect=ValueError('broken history')):
            self.app.render()
        self.assertEqual(self.app.display_lines,before)
        self.assertEqual(self.app.batch_translation_error,'broken history')
        self.app.cache_current_lyrics()
        self.assertIsNotNone(self.app.lyric_cache.load(self.playback))
        with self.assertRaisesRegex(ValueError,'broken history'):self.app.open_batch_translation()

    def test_corrupt_import_does_not_block_existing_lyrics_on_startup(self):
        self.app.close()
        path=self.store.directory/'ai_translations/imports.json'
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text('{broken',encoding='utf-8')
        self.root=tk.Tk();self.root.withdraw();self.app=LyricsApp(self.root,self.store,start_services=False)
        self.app.set_playback(self.playback)
        self.assertEqual(self.app.display_lines,self.lines)
        self.assertTrue(self.app.batch_translation_error)
        self.assertEqual(path.read_text(encoding='utf-8'),'{broken')


if __name__=='__main__':unittest.main()
