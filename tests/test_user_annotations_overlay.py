from dataclasses import replace
import json
import tkinter as tk
import unittest
from unittest.mock import patch

from app import LyricsApp
from lyrics import Line, Song
from user_annotations import UserAnnotations
import test_background_overlay as background


class UserAnnotationOverlayTests(unittest.TestCase):
    setUp=background.BackgroundOverlayTests.setUp
    tearDown=background.BackgroundOverlayTests.tearDown
    provider=background.BackgroundOverlayTests.provider
    receive=background.BackgroundOverlayTests.receive
    position=background.BackgroundOverlayTests.position

    def load_annotations(self):
        path=self.store.directory/'user_lyrics'/'song.json'
        path.parent.mkdir(exist_ok=True)
        value={'schema_version':1,'title':'Test Song','artists':['Singer'],
               'source':{'kind':'user_provided'},'lines':[
            {'original':text,'reading':'reading '+str(index),'korean':'user translation '+str(index),
             'singers':['如月千早'] if index==0 else ['天海春香'] if index==2 else []}
            for index,text in enumerate(background.TEXTS)]}
        path.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')
        self.app.user_annotations=UserAnnotations(self.store.directory)
        self.app.user_annotation_signature=None

    def test_annotations_arrive_with_originals_and_clear_singers_on_unlabelled_row(self):
        self.load_annotations();self.receive()
        self.assertEqual(self.app.translated_count,3)
        self.assertEqual(self.app.current_line_singers(),('如月千早',))
        self.assertIn('키사라기 치하야',self.app.current_singers.cget('text'))
        self.position(25)
        self.assertEqual(self.app.current_line_singers(),())
        self.assertEqual(self.app.current_singers.cget('text'),'')
        self.assertFalse(self.app.current_singers.place_info())
        self.position(35)
        self.assertEqual(self.app.current_line_singers(),('天海春香',))

    def test_annotation_only_refresh_preserves_identity_direct_clock_and_manual_timing(self):
        self.receive()
        saved=self.app.song
        fingerprint=saved.fingerprint
        self.app.timing={**self.app.timing,'times':[None,19.5,None],'offset':1.25,
                         'origins':[None,'manual',None],'manual_priority':True}
        self.app.save_current_timing(self.app.timing)
        before=self.app.timing;clock=self.app.direct_result;direct=self.app.direct_times()[:]
        path=self.app.live_lyrics.timing_store.path(saved);saved_bytes=path.read_bytes()
        self.app.manual_index=1;self.app.follow.set(False)
        self.load_annotations();self.app.render()
        self.assertIs(self.app.song,saved)
        self.assertEqual(self.app.song.fingerprint,fingerprint)
        self.assertIs(self.app.timing,before)
        self.assertIs(self.app.direct_result,clock)
        self.assertEqual(self.app.direct_times(),direct)
        self.assertEqual(path.read_bytes(),saved_bytes)
        self.assertEqual(self.app.manual_index,1);self.assertFalse(self.app.follow.get())
        self.assertEqual(tuple(line.original for line in self.app.display_lines),background.TEXTS)

    def test_existing_translation_and_reading_take_precedence(self):
        self.receive()
        self.app.display_lines=tuple(Line(line.original,'existing reading','existing translation')
                                     for line in self.app.display_lines)
        self.load_annotations();self.app.render()
        self.assertTrue(all(line.korean=='existing translation' and line.reading=='existing reading'
                            for line in self.app.display_lines))
        self.assertEqual(self.app.line_singers[0],('如月千早',))

    def test_song_switch_clears_labels_and_different_singer_cannot_receive_annotations(self):
        self.load_annotations();self.receive()
        self.playback=replace(self.playback,artist='Other Singer')
        self.app.set_playback(self.playback);self.receive()
        self.assertEqual(self.app.translated_count,0)
        self.assertEqual(self.app.current_line_singers(),())
        self.assertEqual(self.app.current_singers.cget('text'),'')

    def test_restart_restores_user_labels_without_changing_cache_schema_or_clock(self):
        self.load_annotations();self.receive();self.app.cache_current_lyrics()
        cached=self.app.lyric_cache.load(self.playback)
        self.assertEqual(cached['direct']['times'],[10,20,30])
        self.assertEqual(tuple(line.original for line in cached['lines']),background.TEXTS)
        self.app.close();self.root=tk.Tk();self.root.withdraw()
        self.app=LyricsApp(self.root,self.store,start_services=False)
        self.app.set_playback(self.playback)
        self.assertTrue(self.app.cache_hit)
        self.assertEqual(self.app.current_line_singers(),('如月千早',))
        self.assertEqual(self.app.direct_times(),[10,20,30])

    def test_saved_library_annotations_do_not_change_fingerprint_or_library(self):
        song=Song('saved','Test Song','Singer','Album',tuple(Line(text,'','') for text in background.TEXTS))
        self.store.save_library([song]);self.store.save_timing(song,{'times':[10,20,30],'offset':0})
        library=(self.store.directory/'library.json').read_bytes()
        timing=(self.store.directory/'timings'/'saved.json').read_bytes()
        self.app.songs=[song];self.app.select_song(song)
        self.load_annotations();self.app.render()
        self.assertIs(self.app.song,song)
        self.assertEqual(self.app.translated_count,3)
        self.assertEqual((self.store.directory/'library.json').read_bytes(),library)
        self.assertEqual((self.store.directory/'timings'/'saved.json').read_bytes(),timing)

    def test_singer_marker_preserves_lyric_center_and_height_in_narrow_transparent_overlay(self):
        self.load_annotations();self.receive()
        self.app.transparent.set(True);self.app.apply_appearance()
        self.root.attributes('-alpha',0);self.root.deiconify()
        self.root.geometry('560x320+50+50');self.root.update();self.root.update()
        baseline=(self.app.lyric_content.winfo_reqheight(),self.app.lyric_content.winfo_width(),
                  self.app.lyric_content.winfo_rootx(),self.root.winfo_height())
        foreground=self.app.original_label.cget('fg')
        self.assertIn(self.app.current_singers.hint,self.app.control_hints)
        for names in (('如月千早',),('天海春香','如月千早'),()):
            with self.subTest(names=names):
                self.app.current_singers.set_singers(names,self.playback.album)
                self.root.update();self.root.update()
                self.assertEqual(baseline,(self.app.lyric_content.winfo_reqheight(),self.app.lyric_content.winfo_width(),
                                            self.app.lyric_content.winfo_rootx(),self.root.winfo_height()))
                self.assertEqual(self.app.original_label.cget('fg'),foreground)
                self.assertFalse(self.app.current_singers.grid_info())
                self.assertEqual(bool(self.app.current_singers.place_info()),bool(names))
                if names:
                    self.assertGreaterEqual(self.app.current_singers.winfo_rootx(),self.root.winfo_rootx())
                    self.assertLess(self.app.current_singers.winfo_rootx()+self.app.current_singers.winfo_width(),
                                    self.app.lyric_content.winfo_rootx())
        self.assertEqual(self.app.current_singers.cget('text'),'')
        self.assertEqual(self.app.current_singers.hint.text,'')

    def test_part_reference_requires_same_recording_key_and_duration_within_two_seconds(self):
        self.load_annotations();self.receive()
        key,duration,result=self.app.direct_result
        for supplied_key,supplied_duration,valid in ((key,duration,True),(key,duration+2,True),
                                                    ('other-recording',duration,False),(key,duration+2.1,False)):
            with self.subTest(key=supplied_key,duration=supplied_duration):
                self.app.direct_result=(supplied_key,supplied_duration,result)
                self.app.user_annotation_signature=None
                with patch.object(self.app.user_annotations,'match',wraps=self.app.user_annotations.match) as match:
                    self.app.refresh_user_annotations()
                reference=match.call_args.kwargs['reference_lines']
                self.assertEqual(reference,tuple(result['lines']) if valid else ())
                self.assertEqual(tuple(line.original for line in self.app.display_lines),background.TEXTS)

    def test_name_chips_stay_on_screen_with_original_hidden_and_many_singers(self):
        self.load_annotations();self.receive()
        self.app.show_original.set(False);self.app.show_reading.set(False)
        self.app.render();self.root.attributes('-alpha',0);self.root.deiconify()
        self.root.geometry('560x320+0+50');self.root.update();self.root.update()
        names=('黛冬優子','園田智代子','鈴木羽那','七草にちか','大崎甜花','A very long explicit guest name')
        self.app.current_singers.set_singers(names,'THE IDOLM@STER SHINY COLORS')
        self.root.update();self.root.update()
        marker=self.app.current_singers;content=self.app.lyric_content
        self.assertEqual(self.app.original_label.cget('text'),'')
        self.assertGreaterEqual(marker.winfo_rootx(),self.root.winfo_rootx())
        self.assertLess(marker.winfo_rootx()+marker.winfo_width(),content.winfo_rootx())
        self.assertLessEqual(marker.winfo_height(),content.winfo_height())
        self.assertAlmostEqual(content.winfo_rootx()+content.winfo_width()/2,
                               self.root.winfo_rootx()+self.root.winfo_width()/2,delta=1)
        self.assertEqual(marker._marker_names[0],'후유코')
        self.assertIn('A very long explicit guest name',marker.hint.text)
        self.assertEqual(len(marker._marker_colors),6)

    def test_partial_singer_annotation_updates_hover_without_changing_lyric_or_clock(self):
        self.load_annotations();self.receive()
        text=self.app.original_label.cget('text');clock=self.app.direct_result;timing=self.app.timing
        self.app.user_annotation_rows=(replace(self.app.user_annotation_rows[0],singer_partial=True),
                                       *self.app.user_annotation_rows[1:])
        self.app.render()
        self.assertTrue(self.app.current_line_singer_partial())
        self.assertTrue(self.app.current_singers._partial)
        self.assertIn('일부 파트의 가창자는 미확인',self.app.current_singers.hint.text)
        self.assertEqual(self.app.original_label.cget('text'),text)
        self.assertIs(self.app.direct_result,clock);self.assertIs(self.app.timing,timing)
        self.position(25)
        self.assertFalse(self.app.current_line_singer_partial())
        self.assertEqual(self.app.current_singers.hint.text,'')


if __name__=='__main__':unittest.main()
