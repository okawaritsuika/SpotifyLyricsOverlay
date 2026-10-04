"""AI translation round trips must leave recording identity and clocks intact."""
import json
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from app import LyricsApp
from batch_translation import BatchTranslationStore
from lyrics import Line, Song
from media import Playback
from storage import Store


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
