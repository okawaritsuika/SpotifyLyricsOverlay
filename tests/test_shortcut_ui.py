import tempfile
import sys
import tkinter as tk
import unittest
from types import SimpleNamespace
from app import LyricsApp
from shortcuts import validate_bindings
from storage import Store


class ShortcutUITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(self.temp.name)
        self.store.save_settings({'online':False,'auto_audio':False,'borderless':False})
        self.root=tk.Tk();self.app=LyricsApp(self.root,self.store,start_services=False)
        self.app.open_shortcuts();self.dialog=self.app.shortcut_dialog;self.root.update()
    def tearDown(self):
        self.app.close();self.temp.cleanup()
    def test_press_capture_save_and_reload(self):
        self.dialog.begin();self.assertTrue(self.app.hotkeys.suspended)
        self.dialog.key_down(SimpleNamespace(keysym='Shift_R',keycode=16,state=0))
        self.dialog.key_down(SimpleNamespace(keysym='F8',keycode=0x77,state=1))
        self.assertFalse(self.app.hotkeys.suspended)
        self.dialog.save()
        binding=self.store.settings()['hotkeys']['stamp']
        self.assertEqual(binding,{'key':0x77,'modifiers':4,'shift':'right'})
        self.assertEqual(self.app.hotkeys.bindings['stamp'],binding)
        self.assertEqual(validate_bindings(self.store.settings()['hotkeys'])['stamp'],binding)
    def test_cancel_and_parent_destroy_restore_shortcuts(self):
        self.dialog.begin();self.dialog.key_down(SimpleNamespace(keysym='Escape',keycode=27,state=0))
        self.assertFalse(self.app.hotkeys.suspended);self.assertNotIn('hotkeys',self.store.settings())
        self.dialog.begin();self.dialog.window.destroy()
        self.assertTrue(self.dialog.closed);self.assertFalse(self.app.hotkeys.suspended)
    def test_minimum_window_keeps_actions_visible_and_table_scrollable(self):
        self.dialog.window.geometry('510x400');self.root.update()
        width=self.dialog.window.winfo_width();height=self.dialog.window.winfo_height()
        for button in self.dialog.buttons:
            x=button.winfo_rootx()-self.dialog.window.winfo_rootx();y=button.winfo_rooty()-self.dialog.window.winfo_rooty()
            self.assertGreaterEqual(x,0);self.assertGreaterEqual(y,0)
            self.assertLessEqual(x+button.winfo_width(),width);self.assertLessEqual(y+button.winfo_height(),height)
            self.assertTrue(button.winfo_viewable())
        self.assertEqual(len(self.dialog.tree.get_children()),11)
    def test_duplicate_key_is_not_applied(self):
        self.dialog.begin();self.dialog.key_down(SimpleNamespace(keysym='Control_L',keycode=17,state=0))
        self.dialog.key_down(SimpleNamespace(keysym='Alt_L',keycode=18,state=4))
        self.dialog.key_down(SimpleNamespace(keysym='space',keycode=32,state=12))
        self.assertIn('같은 단축키',self.dialog.status.get());self.assertIsNotNone(self.dialog.capturing)
        self.assertNotIn('hotkeys',self.store.settings())

    @unittest.skipUnless(sys.platform=='win32','Windows native foreground handles')
    def test_application_registers_only_lyric_windows_and_removes_closed_editor(self):
        hotkeys=self.app.hotkeys
        self.assertEqual(set(hotkeys.windows),{self.root,self.app.backdrop.window})
        self.assertNotIn(self.dialog.window,hotkeys.windows)
        self.app.open_editor();self.root.update()
        editor=self.app.editor
        self.assertEqual(set(hotkeys.windows),{self.root,self.app.backdrop.window,editor})
        self.assertTrue(all(hotkeys.windows.values()))
        self.app.close_editor();self.root.update()
        self.assertNotIn(editor,hotkeys.windows)
        self.assertEqual(set(hotkeys.windows),{self.root,self.app.backdrop.window})

    def test_down_arrow_alone_can_be_captured_saved_and_matched(self):
        from shortcuts import ShortcutMatcher
        self.dialog.begin()
        self.dialog.key_down(SimpleNamespace(keysym='Down',keycode=0x28,state=0))
        self.assertIsNone(self.dialog.capturing)
        self.dialog.save()
        bindings=validate_bindings(self.store.settings()['hotkeys'])
        self.assertEqual(bindings['stamp'],{'key':0x28,'modifiers':0,'shift':'any'})
        matcher=ShortcutMatcher(bindings)
        self.assertEqual(matcher.event(0x28,True),(True,'stamp'))
        self.assertEqual(matcher.event(0x28,True),(True,None))
        self.assertEqual(matcher.event(0x28,False),(True,None))
