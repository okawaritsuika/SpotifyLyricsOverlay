import tempfile,time,tkinter as tk,unittest
from unittest.mock import patch
from app import LyricsApp
from lyrics import Line,Song
from manual_lyrics import parse_manual,manual_song
from media import Playback
from storage import Store


class ManualTextTests(unittest.TestCase):
    def test_original_only_keeps_repetitions_and_has_no_invented_translation(self):
        lines=parse_manual('青い空\r\n\r\n風の音\r\n青い空',reader=lambda value:'よみ')
        self.assertEqual([line.original for line in lines],['青い空','風の音','青い空'])
        self.assertTrue(all(line.reading=='よみ' and not line.korean for line in lines))

    def test_two_and_three_line_formats_preserve_user_text(self):
        lines=parse_manual('青い空\n푸른 하늘\n\n風の音\n바람 소리',1,reader=lambda value:'よみ')
        self.assertEqual(lines[1],Line('風の音','よみ','바람 소리'))
        with patch('manual_lyrics.reading',side_effect=AssertionError('unused')):
            lines=parse_manual('青い空\nあおいそら\n푸른 하늘\n風の音\nかぜのおと\n바람 소리',2)
        self.assertEqual(lines[0],Line('青い空','あおいそら','푸른 하늘'))

    def test_incomplete_group_is_rejected_instead_of_dropping_a_line(self):
        for text,mode in (('',0),('空\n번역\nextra',1),('空\n독음',2),('空\n\n번역',1)):
            with self.subTest(text=text,mode=mode),self.assertRaises(ValueError):parse_manual(text,mode)
        with self.assertRaises(ValueError):parse_manual('\n'.join(['空']*601))

    def test_song_identity_and_title_validation(self):
        lines=(Line('空','',''),)
        self.assertEqual(manual_song(' Song ','Artist','Album',lines),manual_song('Song','Artist','Album',lines))
        with self.assertRaises(ValueError):manual_song(' ','','',lines)


class ManualDialogTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(self.temp.name)
        self.store.save_settings({'online':False,'auto_audio':False,'web_auto':False,'borderless':False,'transparent':False,'reader_mode':'chrome'})
        self.root=tk.Tk();self.root.withdraw();self.app=LyricsApp(self.root,self.store,start_services=False)
        self.playback=Playback('Unregistered','Artist','Album',10,180,False,time.monotonic())
        self.app.set_playback(self.playback)

    def tearDown(self):self.app.close();self.temp.cleanup()

    def open(self,text='青い空\n風の音'):
        self.app.open_manual_lyrics();dialog=self.app.manual_dialog
        dialog.paste.insert('1.0',text);return dialog

    def test_paste_to_new_song_saves_links_and_can_record_without_any_file_picker(self):
        dialog=self.open()
        with patch('tkinter.filedialog.askopenfilename',side_effect=AssertionError('file picker')), \
             patch('web_lyrics.urlopen',side_effect=AssertionError('network')):
            dialog.preview();dialog.save()
        song=self.store.library()[0]
        self.assertEqual(self.store.settings()['links'][self.playback.key],song.id)
        self.assertEqual(self.app.song,song);self.assertTrue(self.app.is_bound())
        self.assertEqual(self.app.original_label.cget('text'),'青い空')
        self.assertFalse(self.app.hotkeys.suspended)
        self.app.manual_index=0;self.app.follow.set(False);self.app.stamp()
        self.assertEqual(self.store.timing(song)['times'],[None,10])
        self.app.set_playback(Playback('Other'));self.app.set_playback(self.playback)
        self.assertEqual(self.app.song,song);self.assertEqual(self.app.timing['times'],[None,10])

    def test_save_reparses_edits_after_preview_and_does_not_bind_to_a_new_track(self):
        dialog=self.open();dialog.preview()
        dialog.paste.insert('end','\n遠い道')
        self.app.set_playback(Playback('Other','Other artist','Other album',20,200,False,time.monotonic()))
        dialog.save();song=self.store.library()[0]
        self.assertEqual(len(song.lines),3)
        self.assertEqual(self.store.settings()['links'],{self.playback.key:song.id})
        self.assertIsNone(self.app.song)

    def test_duplicate_input_does_not_touch_existing_lyrics_or_timing(self):
        old=Song('old','Unregistered','Artist','Album',(Line('既存','','번역'),))
        self.store.save_library([old]);self.app.songs=[old]
        self.store.save_timing(old,{'times':[12],'offset':.25})
        paths=[self.store.directory/'library.json',self.store.directory/'timings/old.json']
        before=[path.read_bytes() for path in paths]
        dialog=self.open()
        with patch('app.messagebox.askyesno',return_value=False) as confirm:dialog.save()
        confirm.assert_called_once()
        self.assertIn('덮어쓰기',confirm.call_args.args[0])
        self.assertEqual(before,[path.read_bytes() for path in paths])
        self.assertTrue(dialog.window.winfo_exists())

    def test_cancel_saves_nothing_and_restores_shortcuts_even_when_destroyed(self):
        dialog=self.open();self.assertTrue(self.app.hotkeys.suspended)
        with patch('windows.ctypes.windll',side_effect=AssertionError('UI query')):
            self.assertFalse(self.app.hotkeys.focused())
        dialog.window.destroy();self.assertFalse(self.app.hotkeys.suspended)
        self.assertIsNone(self.app.manual_dialog);self.assertEqual(self.store.library(),[])

    def test_plain_arrows_edit_text_without_changing_lyric_cursor(self):
        dialog=self.open();self.root.update();dialog.paste.focus_force();self.root.update()
        dialog.paste.mark_set('insert','1.0');dialog.paste.event_generate('<Right>');self.root.update()
        self.assertEqual(dialog.paste.index('insert'),'1.1')
        self.assertEqual(self.app.cursor,0);self.assertEqual(self.store.library(),[])
        self.assertTrue(self.app.hotkeys.suspended)

    def test_empty_save_shows_error_inside_dialog_and_offline_creation_works(self):
        self.app.set_playback(Playback());dialog=self.open('')
        dialog.invoke(dialog.save)
        self.assertIn('가사',dialog.status.get());self.assertEqual(self.store.library(),[])
        dialog.title.set('Offline');dialog.paste.insert('1.0','青い空');dialog.save()
        self.assertEqual(self.store.library()[0].title,'Offline')
        self.assertFalse(self.store.settings().get('links'))

    def test_small_dialog_and_editor_keep_input_and_save_controls_visible(self):
        self.app.open_editor();self.app.editor.geometry('560x660+20+20')
        dialog=self.open();dialog.window.geometry('560x530+40+40')
        self.root.update();self.root.update()
        for button in dialog.buttons:
            self.assertTrue(button.winfo_ismapped())
            self.assertLessEqual(button.winfo_rootx()+button.winfo_width(),dialog.window.winfo_rootx()+dialog.window.winfo_width())
            self.assertLessEqual(button.winfo_rooty()+button.winfo_height(),dialog.window.winfo_rooty()+dialog.window.winfo_height())
        self.assertGreater(dialog.paste.winfo_height(),70)
        dialog.preview();self.root.update();self.assertGreater(dialog.table.winfo_height(),70)
        dialog.close();self.root.update()
        self.assertGreater(self.app.tree.winfo_height(),30)
        for widget in self.app.editor.winfo_children()[0].winfo_children():
            if isinstance(widget,tk.Button):
                self.assertLessEqual(widget.winfo_rootx()+widget.winfo_width(),self.app.editor.winfo_rootx()+560)


if __name__=='__main__':unittest.main()
