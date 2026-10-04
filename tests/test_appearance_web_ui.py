import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

from app import LyricsApp
from lyrics import Line,Song
from storage import Store
from appearance import DEFAULT_STYLES,styles_from_settings


class AppearanceWebTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(self.temp.name)
        self.store.save_settings({'online':False,'transparent':False,'borderless':False})
        self.root=tk.Tk()
        self.app=LyricsApp(self.root,self.store,start_services=False)
        self.root.update()

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def test_independent_opacity_and_backdrop_follows_window(self):
        self.app.opacity.set(.85)
        self.app.background_opacity.set(.25)
        self.app.background_color='#314159'
        self.app.apply_appearance()
        self.root.geometry('640x300+80+120')
        self.root.update();self.root.update()
        back=self.app.backdrop.window
        self.assertAlmostEqual(self.root.attributes('-alpha'),.85)
        self.assertAlmostEqual(back.attributes('-alpha'),.25)
        self.assertEqual(back.cget('bg'),'#314159')
        self.assertEqual((back.winfo_rootx(),back.winfo_rooty()),(self.root.winfo_rootx(),self.root.winfo_rooty()))
        self.assertEqual(back.winfo_width(),self.root.winfo_width())
        self.app.toggle_transparent();self.root.update()
        self.assertFalse(back.winfo_viewable())
        self.assertAlmostEqual(self.root.attributes('-alpha'),.85)

    def test_subtitle_updates_items_in_place_and_restores_after_empty_line(self):
        label=self.app.korean_label
        label.configure(text='첫 번째 번역');self.root.update()
        ids=label.find_withtag('lyric');self.assertEqual(len(ids),9)
        label.configure(text='두 번째 번역',fg='#ffaa00');self.root.update()
        self.assertEqual(label.find_withtag('lyric'),ids)
        self.assertEqual(label.itemcget(ids[-1],'text'),'두 번째 번역')
        self.assertEqual(label.itemcget(ids[-1],'fill'),'#ffaa00')
        label.configure(text='');self.root.update()
        self.assertTrue(all(label.itemcget(item,'state')=='hidden' for item in ids))
        label.configure(text='복원된 번역');self.root.update()
        self.assertEqual(label.find_withtag('lyric'),ids)
        self.assertTrue(all(label.itemcget(item,'state')=='normal' for item in ids))

    def test_appearance_dialog_fits_and_persists_individual_fonts(self):
        self.app.open_appearance()
        d=self.app.appearance_dialog
        d.window.geometry('560x510+20+20')
        self.root.update();self.root.update()
        for family,size,bold in d.values.values():size.set(28)
        d.values['reading'][1].set(12)
        self.app.text_styles['korean']['color']='#ffaa00'
        d.update()
        self.assertEqual(self.app.text_styles['original']['size'],28)
        self.assertEqual(self.app.text_styles['reading']['size'],12)
        self.assertEqual(self.app.korean_label.cget('fg'),'#ffaa00')
        self.assert_controls_fit(d.window)
        d.close()
        self.assertEqual(self.store.settings()['text_styles']['reading']['size'],12)

    def test_appearance_feedback_tracks_opacity_colors_and_hidden_background(self):
        self.app.open_appearance();dialog=self.app.appearance_dialog
        self.app.opacity.set(.72);self.app.background_opacity.set(.31)
        self.app.text_styles['reading']['color']='#abcdef'
        dialog.change_background()
        self.assertEqual(dialog.text_percent.cget('text'),'72%')
        self.assertEqual(dialog.background_percent.cget('text'),'31%')
        self.assertEqual(dialog.color_buttons['reading'].cget('fg'),'#abcdef')
        self.app.transparent.set(True);dialog.update()
        self.assertEqual(dialog.background_percent.cget('text'),'숨김')
        dialog.reset()
        self.assertEqual(dialog.text_percent.cget('text'),'94%')
        self.assertEqual(dialog.background_percent.cget('text'),'55%')

    def assert_controls_fit(self,window):
        def visit(widget):
            for child in widget.winfo_children():
                if not child.winfo_ismapped():continue
                if isinstance(child,(tk.Button,)) or child.winfo_class() in ('TCombobox','TSpinbox'):
                    self.assertGreater(child.winfo_width(),15)
                    self.assertLessEqual(child.winfo_rootx()+child.winfo_width(),window.winfo_rootx()+window.winfo_width())
                    self.assertLessEqual(child.winfo_rooty()+child.winfo_height(),window.winfo_rooty()+window.winfo_height())
                visit(child)
        visit(window)

    def test_paste_preview_has_three_languages_and_saves_without_network(self):
        self.app.open_web_lyrics();d=self.app.web_dialog
        d.window.geometry('560x530+20+20')
        d.title.set('Test Song')
        d.paste.insert('1.0','窓を開ける\n마도오 아케루\n창문을 열다')
        with patch('web_lyrics.urlopen',side_effect=AssertionError('unexpected network')):
            d.parse();d.save()
        self.root.update();self.root.update()
        self.assert_controls_fit(d.window)
        self.assertGreater(d.table.winfo_height(),50)
        saved=self.store.library()[0]
        self.assertEqual(saved.lines[0].korean,'창문을 열다')
        self.assertIn('あける',saved.lines[0].reading)
        self.assertEqual(len(d.table.item(d.table.get_children()[0])['values']),3)

    def test_changed_identity_rejects_inflight_result(self):
        self.app.open_web_lyrics();d=self.app.web_dialog
        d.title.set('First')
        with patch.object(self.app,'fetch_web_lyrics'):
            d.fetch()
        d.title.set('Second')
        d.receive((Line('窓','まど','창문'),),{},'')
        self.assertIsNone(d.lines)
        self.assertIn('바뀌',d.status.get())

    def test_extension_result_registers_new_song_and_preserves_existing_timing(self):
        result=('Example Song',(Line('窓を開ける','まど を あける','창문을 열다'),),{'url':'https://namu.wiki/w/Example%20Song'})
        self.app.receive_extension_lyrics('one',result,'')
        song=self.store.library()[0]
        self.store.save_timing(song,{'times':[12],'offset':.5})
        self.app.receive_extension_lyrics('two',result,'')
        self.assertEqual(len(self.store.library()),1)
        self.assertEqual(self.store.timing(song)['times'],[12])
        self.assertEqual(self.store.timing(song)['offset'],.5)

    def test_received_translation_fills_current_japanese_line_from_persistent_cache(self):
        song=Song('japanese-only','Example Song','','',(Line('窓を開ける','',''),))
        self.app.songs=[song];self.store.save_library([song])
        self.store.save_timing(song,{'times':[12],'offset':.5})
        self.app.select_song(song);self.app.manual_index=0;self.app.follow.set(False)
        before=(self.store.directory/'timings'/'japanese-only.json').read_bytes()
        result=('Example Song',(Line('窓を開ける','まどをあける','창문을 열다'),),{})
        self.app.receive_extension_lyrics('cached',result,'')
        self.assertEqual(self.app.korean_label.cget('text'),'창문을 열다')
        self.assertEqual(self.app.reading_label.cget('text'),'まどをあける')
        self.assertEqual(self.store.library()[0],song)
        self.assertEqual(before,(self.store.directory/'timings'/'japanese-only.json').read_bytes())
        # Switching away and back loads the saved annotations without any web request.
        self.app.select_song(None)
        with patch('web_lyrics.urlopen',side_effect=AssertionError('unexpected network')):
            self.app.select_song(song)
            self.app.manual_index=0;self.app.follow.set(False);self.app.render()
        self.assertEqual(self.app.korean_label.cget('text'),'창문을 열다')


class AppearanceSettingsTests(unittest.TestCase):
    def test_invalid_saved_styles_recover_without_discarding_valid_languages(self):
        styles=styles_from_settings({'text_styles':{
            'original':{'family':None,'size':'invalid','color':'#010203','bold':'false'},
            'reading':None,'korean':{'size':31,'color':'#aabbcc'}}})
        self.assertEqual(styles['original'],DEFAULT_STYLES['original'])
        self.assertEqual(styles['reading'],DEFAULT_STYLES['reading'])
        self.assertEqual(styles['korean']['size'],31)
        self.assertEqual(styles['korean']['color'],'#aabbcc')

    def test_invalid_style_container_and_legacy_size_recover(self):
        for value in (None,[],False,'invalid'):
            styles=styles_from_settings({'text_styles':value,'font_size':None})
            self.assertEqual(styles['original']['size'],24)
        self.assertEqual(styles_from_settings({'font_size':100})['original']['size'],72)


if __name__=='__main__':unittest.main()
