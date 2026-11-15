"""Exercise the actual hook callback with synthetic native events, never real keys."""
import ctypes
from ctypes import wintypes
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import tests.test_hotkey_window_scope as fixtures
from windows import install_shortcut_hook


class KeyboardEvent(ctypes.Structure):
    _fields_=[('vkCode',wintypes.DWORD),('scanCode',wintypes.DWORD),('flags',wintypes.DWORD),
              ('time',wintypes.DWORD),('extra',ctypes.c_size_t)]


class HotkeyInputScopeTests(unittest.TestCase):
    def setUp(self):
        fixtures.HotkeyWindowScopeTests.setUp(self)
        self.window=fixtures.Window(10);self.hotkeys.register_window(self.window)
        self.user.GetAsyncKeyState.return_value=0
        self.user.CallNextHookEx.return_value=77
        self.user.SetWindowsHookExW.return_value=123

    def install(self,key=0x25,modifiers=0):
        binding={'next':{'key':key,'modifiers':modifiers,'shift':'any'}}
        _,self.callback=install_shortcut_hook(self.user,self.hotkeys.messages,binding,self.hotkeys.focused)

    def key(self,key=0x25,down=True):
        event=KeyboardEvent(vkCode=key)
        return self.callback(0,0x100 if down else 0x101,ctypes.addressof(event))

    def focus_input(self,widget_class='TEntry'):
        widget=fixtures.Window(20,widget_class);self.window.fire('<FocusIn>',widget)
        # The keyboard callback must use the GUI thread's cached result only.
        widget.winfo_class=Mock(side_effect=AssertionError('Tk call inside hook'))
        return widget

    def test_every_plain_arrow_passes_down_repeat_and_up_for_input_classes(self):
        for widget_class in ('Entry','TEntry','Text','TCombobox','Spinbox','TSpinbox'):
            self.focus_input(widget_class)
            self.assertTrue(self.hotkeys.focused())
            self.assertFalse(self.hotkeys.focused(plain_arrow=True))
            for key in range(0x25,0x29):
                self.install(key)
                self.assertEqual([self.key(key),self.key(key),self.key(key,False)],[77,77,77])
                self.assertTrue(self.hotkeys.messages.empty())

    def test_treeview_and_overlay_keep_plain_arrow_actions(self):
        for widget in (fixtures.Window(20,'Treeview'),self.window):
            self.window.fire('<FocusIn>',widget);self.install()
            self.assertEqual([self.key(),self.key(),self.key(down=False)],[1,1,1])
            self.assertEqual(self.hotkeys.messages.get_nowait(),('action','next'))
            self.assertTrue(self.hotkeys.messages.empty())

    def test_developer_arrows_reach_both_overlay_surfaces(self):
        backdrop=fixtures.Window(11)
        self.hotkeys.register_window(backdrop)
        self.hotkeys.developer_input_windows=(self.window,backdrop)
        for handle in (1010,1011):
            self.user.GetForegroundWindow.return_value=handle
            for key in range(0x25,0x29):
                self.install(key)
                self.assertEqual([self.key(key),self.key(key,False)],[77,77])
                self.assertTrue(self.hotkeys.messages.empty())
        self.hotkeys.developer_input_windows=()
        self.install()
        self.assertEqual(self.key(),1)

    def test_modified_arrows_keep_actions_and_keyup_after_modifier_release(self):
        self.focus_input()
        for modifier,mask in ((0xA2,2),(0xA1,4),(0xA4,1),(0x5B,8)):
            self.install(modifiers=mask)
            self.assertEqual(self.key(modifier),77)
            self.assertEqual(self.key(),1)
            self.assertEqual(self.key(modifier,False),77)
            self.assertEqual(self.key(down=False),1)
            self.assertEqual(self.hotkeys.messages.get_nowait(),('action','next'))
            self.assertTrue(self.hotkeys.messages.empty())

    def test_function_key_keeps_action_inside_input(self):
        self.focus_input();self.install(0x77)
        self.assertEqual(self.key(0x77),1)
        self.assertEqual(self.key(0x77,False),1)
        self.assertEqual(self.hotkeys.messages.get_nowait(),('action','next'))

    def test_passing_input_arrow_does_not_lose_another_captured_keyup(self):
        self.focus_input()
        bindings={'next':{'key':0x25,'modifiers':0,'shift':'any'},
                  'follow':{'key':0x77,'modifiers':0,'shift':'any'}}
        _,self.callback=install_shortcut_hook(self.user,self.hotkeys.messages,bindings,self.hotkeys.focused)
        self.assertEqual(self.key(0x77),1)
        self.assertEqual(self.key(),77);self.assertEqual(self.key(down=False),77)
        self.assertEqual(self.key(0x77,False),1)
        self.assertEqual(self.hotkeys.messages.get_nowait(),('action','follow'))
        self.assertTrue(self.hotkeys.messages.empty())

    def test_held_input_arrow_does_not_start_action_on_overlay_repeat(self):
        entry=self.focus_input();self.install()
        self.assertEqual(self.key(),77)
        self.window.fire('<FocusOut>',entry);self.window.fire('<FocusIn>',self.window)
        self.assertEqual(self.key(),77)
        self.assertEqual(self.key(down=False),77)
        self.assertTrue(self.hotkeys.messages.empty())
        self.assertEqual(self.key(),1)
        self.assertEqual(self.hotkeys.messages.get_nowait(),('action','next'))

    def test_already_captured_arrow_keeps_pair_when_focus_moves_into_entry(self):
        self.install();self.assertEqual(self.key(),1)
        self.focus_input()
        self.assertEqual(self.key(),1)
        self.assertEqual(self.key(down=False),1)
        self.assertEqual(self.hotkeys.messages.get_nowait(),('action','next'))
        self.assertTrue(self.hotkeys.messages.empty())
        self.assertEqual(self.key(),77)

    def test_late_old_focusout_does_not_clear_new_input(self):
        old=self.focus_input();current=self.focus_input('Text')
        self.window.fire('<FocusOut>',old)
        self.assertIs(self.hotkeys.input_focus[1],current)
        self.assertFalse(self.hotkeys.focused(plain_arrow=True))
        self.window.fire('<FocusOut>',current)
        self.assertIsNone(self.hotkeys.input_focus)

    def test_remap_destroy_and_other_windows_keep_scope(self):
        entry=self.focus_input()
        self.window.handle=30;self.window.fire('<Map>')
        self.user.GetForegroundWindow.return_value=1030
        self.assertFalse(self.hotkeys.focused(plain_arrow=True))
        self.assertTrue(self.hotkeys.focused())
        self.window.fire('<Destroy>',entry)
        self.assertTrue(self.hotkeys.focused(plain_arrow=True))
        self.focus_input();self.window.fire('<Destroy>')
        self.assertIsNone(self.hotkeys.input_focus)
        self.assertFalse(self.hotkeys.focused())
        self.assertFalse(self.hotkeys.changed.is_set())

    def test_input_cache_never_enables_foreign_pid_or_unregistered_window(self):
        self.focus_input();self.install(modifiers=4)
        self.user.GetForegroundWindow.return_value=9090
        self.assertEqual(self.key(0xA1),77);self.assertEqual(self.key(),77)
        self.user.GetForegroundWindow.return_value=1010;self.pid+=1
        self.assertFalse(self.hotkeys.focused())
        self.assertFalse(self.hotkeys.focused(plain_arrow=True))

    def test_combobox_popup_keeps_cache_but_late_idle_cannot_override_new_focus(self):
        callbacks=[];self.window.after_idle=callbacks.append
        self.window.tk=SimpleNamespace(call=Mock())
        combo=self.focus_input('TCombobox')
        self.window.fire('<FocusOut>',combo)
        self.window.tk.call.return_value=str(combo)+'.popdown.f.l'
        callbacks.pop()()
        self.assertIs(self.hotkeys.input_focus[1],combo)
        self.assertFalse(self.hotkeys.focused(plain_arrow=True))
        self.window.fire('<FocusOut>',combo)
        entry=self.focus_input()
        callbacks.pop()()
        self.assertIs(self.hotkeys.input_focus[1],entry)
        combo=self.focus_input('TCombobox');self.window.fire('<FocusOut>',combo)
        self.window.tk.call.return_value=''
        callbacks.pop()()
        self.assertIsNone(self.hotkeys.input_focus)

    def test_window_destroy_cancels_pending_combobox_focus_callback(self):
        self.window.after_idle=Mock(return_value='focus-check')
        self.window.after_cancel=Mock()
        combo=self.focus_input('TCombobox');self.window.fire('<FocusOut>',combo)
        self.window.fire('<Destroy>')
        self.window.after_cancel.assert_called_once_with('focus-check')
        self.assertIsNone(self.hotkeys.input_focus)


if __name__=='__main__':unittest.main()
