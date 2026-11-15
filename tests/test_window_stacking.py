"""Only the floating lyric display stays above other applications."""
import ctypes
from ctypes import wintypes
import sys
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

from app import LyricsApp
from artist_layer import ArtistLayer
from PIL import Image
from storage import Store
from ui_theme import ControlHint


class WindowStackingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.store.save_settings({'online': False, 'auto_audio': False,
                                  'transparent': True, 'background_opacity': 0,
                                  'borderless': False, 'opacity': 0})
        original_toplevel = tk.Toplevel
        def invisible_toplevel(*args, **kwargs):
            window = original_toplevel(*args, **kwargs)
            window.attributes('-alpha', 0)
            return window
        self.windows_patch = patch('tkinter.Toplevel', side_effect=invisible_toplevel)
        self.windows_patch.start()
        self.root = tk.Tk();self.root.withdraw()
        self.app = LyricsApp(self.root, self.store, start_services=False)
        self.root.attributes('-alpha', 0)
        self.root.deiconify();self.root.update()

    def tearDown(self):
        self.app.close()
        self.windows_patch.stop()
        self.temp.cleanup()

    def native_topmost(self, window):
        user = ctypes.WinDLL('user32', use_last_error=True)
        user.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
        user.GetAncestor.restype = wintypes.HWND
        user.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
        user.GetWindowLongW.restype = wintypes.LONG
        handle = user.GetAncestor(window.winfo_id(), 2)
        return bool(user.GetWindowLongW(handle, -20) & 0x8)

    def assert_normal_window(self, window):
        self.root.update()
        self.assertFalse(window.attributes('-topmost'))
        self.assertFalse(window.transient())
        if sys.platform == 'win32':self.assertFalse(self.native_topmost(window))

    def test_settings_editing_and_connection_windows_remain_normal_after_reopening(self):
        self.app.update_controller.checked = True
        actions = [('connection', self.app.open_connection, 'connection_dialog'),
                   ('appearance', self.app.open_appearance, 'appearance_dialog'),
                   ('manual lyrics', self.app.open_manual_lyrics, 'manual_dialog'),
                   ('wiki lyrics', self.app.open_web_lyrics, 'web_dialog'),
                   ('updates', self.app.open_updates, 'update_dialog'),
                   ('AI install', self.app.open_audio_install, 'audio_install_dialog'),
                   ('sync editor', self.app.open_editor, 'editor'),
                   ('audio analysis', self.app.open_audio, 'audio_window'),
                   ('shortcuts', self.app.open_shortcuts, 'shortcut_dialog')]
        for name, action, attribute in actions:
            with self.subTest(window=name):
                action()
                value = getattr(self.app, attribute)
                window = getattr(value, 'window', value)
                self.assert_normal_window(window)
                action()  # Existing-window focus must not turn pinning back on.
                self.assert_normal_window(window)
        self.app.apply_appearance();self.root.update()
        self.assertTrue(self.root.attributes('-topmost'))
        for _, _, attribute in actions:
            value = getattr(self.app, attribute)
            self.assert_normal_window(getattr(value, 'window', value))

    def test_shortcuts_opened_from_floating_lyrics_are_not_transient_or_pinned(self):
        self.app.open_shortcuts()
        self.assert_normal_window(self.app.shortcut_dialog.window)
        self.assertTrue(self.root.attributes('-topmost'))

    def test_control_hints_follow_their_display_window(self):
        normal = tk.Toplevel(self.root)
        normal.attributes('-topmost', False)
        for owner, expected in ((normal, False), (self.root, True)):
            with self.subTest(topmost=expected):
                button = tk.Button(owner, text='Hover');button.place(x=0, y=0, width=70, height=25)
                self.root.update()
                hint = ControlHint(button, 'Control help')
                try:
                    hint.show();self.root.update()
                    self.assertEqual(bool(hint.window.attributes('-topmost')), expected)
                    if sys.platform == 'win32':
                        self.assertEqual(self.native_topmost(hint.window), expected)
                finally:hint.hide();button.destroy()
        normal.destroy()

    @unittest.skipUnless(sys.platform == 'win32', 'Native artist layer requires Windows')
    def test_lyrics_backdrop_and_artist_artwork_keep_topmost(self):
        self.assertTrue(self.root.attributes('-topmost'))
        self.assertTrue(self.native_topmost(self.root))
        self.assertTrue(self.app.backdrop.window.attributes('-topmost'))
        widget = tk.Canvas(self.root, width=1, height=1);widget.place(x=0, y=0)
        self.root.update()
        layer = ArtistLayer(widget)
        try:
            layer.show(Image.new('RGBA', (1, 1)), -10000, -10000)
            self.assertTrue(layer._api.GetWindowLongPtrW(layer._hwnd, -20) & 0x8)
        finally:layer.close();widget.destroy()
