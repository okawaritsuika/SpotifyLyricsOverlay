"""Check native focus gating without installing a hook or sending real keys."""
import ctypes
from ctypes import wintypes
import os
import queue
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from shortcuts import ShortcutMatcher
from windows import GlobalHotkeys


class Window:
    def __init__(self,handle,widget_class='Toplevel'):self.handle=handle;self.bindings={};self.widget_class=widget_class
    def update_idletasks(self):raise AssertionError('window registration must not flush pending layout')
    def winfo_id(self):return self.handle
    def winfo_class(self):return self.widget_class
    def bind(self,event,callback,add=None):
        if add!='+':raise AssertionError('existing UI bindings must be retained')
        self.bindings.setdefault(event,[]).append(callback)
    def fire(self,name,widget=None):
        for callback in self.bindings.get(name,[]):callback(SimpleNamespace(widget=widget or self))


class HotkeyWindowScopeTests(unittest.TestCase):
    def setUp(self):
        self.user=Mock()
        self.user.GetAncestor.side_effect=lambda handle,kind:handle+1000
        self.user.GetForegroundWindow.return_value=1010
        self.pid=os.getpid()
        def process_id(handle,pointer):
            ctypes.cast(pointer,ctypes.POINTER(wintypes.DWORD)).contents.value=self.pid
        self.user.GetWindowThreadProcessId.side_effect=process_id
        self.native=patch('windows.ctypes.windll',SimpleNamespace(user32=self.user))
        self.native.start();self.addCleanup(self.native.stop)
        self.platform=patch('windows.sys.platform','win32')
        self.platform.start();self.addCleanup(self.platform.stop)
        self.hotkeys=GlobalHotkeys(queue.Queue(),{'stamp':{'key':40,'modifiers':0,'shift':'any'}})

    def test_unregistered_dictionary_down_passes_through_without_stamping(self):
        overlay=Window(10);self.hotkeys.register_window(overlay)
        self.user.GetForegroundWindow.return_value=1020
        matcher=ShortcutMatcher(self.hotkeys.bindings)
        self.assertFalse(self.hotkeys.focused())
        self.assertEqual(matcher.event(40,True,enabled=self.hotkeys.focused()),(False,None))
        self.assertEqual(matcher.event(40,False,enabled=self.hotkeys.focused()),(False,None))
        self.user.GetForegroundWindow.return_value=1010
        self.assertEqual(matcher.event(40,True,enabled=self.hotkeys.focused()),(True,'stamp'))

    def test_overlay_backdrop_and_editor_remain_available_only_in_this_process(self):
        for handle in (10,11,12):self.hotkeys.register_window(Window(handle))
        for handle in (1010,1011,1012):
            self.user.GetForegroundWindow.return_value=handle
            self.assertTrue(self.hotkeys.focused())
        self.pid+=1
        self.assertFalse(self.hotkeys.focused())
        self.pid=os.getpid();self.hotkeys.suspend(True)
        self.assertFalse(self.hotkeys.focused())

    def test_remap_replaces_native_frame_without_reinstalling_hook(self):
        window=Window(10);self.hotkeys.register_window(window)
        self.assertEqual(self.hotkeys.window_handles,frozenset({1010}))
        window.handle=20;window.fire('<Map>')
        self.assertEqual(self.hotkeys.window_handles,frozenset({1020}))
        self.assertFalse(self.hotkeys.focused())
        self.user.GetForegroundWindow.return_value=1020
        self.assertTrue(self.hotkeys.focused())
        self.assertFalse(self.hotkeys.changed.is_set())

    def test_child_events_preserve_scope_and_destroy_removes_reusable_handle(self):
        window=Window(10);self.hotkeys.register_window(window)
        window.fire('<Map>',Window(20));window.fire('<Destroy>',Window(20))
        self.assertTrue(self.hotkeys.focused())
        window.fire('<Destroy>')
        self.assertFalse(self.hotkeys.focused())
        self.assertEqual(self.hotkeys.windows,{})

    def test_repeated_registration_does_not_duplicate_window_callbacks(self):
        window=Window(10)
        self.hotkeys.register_window(window);self.hotkeys.register_window(window)
        self.assertEqual({key:len(value) for key,value in window.bindings.items()},
                         {'<Map>':1,'<Destroy>':1,'<FocusIn>':1,'<FocusOut>':1})

    def test_no_registered_window_never_reserves_input(self):
        self.assertFalse(self.hotkeys.focused())


if __name__=='__main__':unittest.main()
