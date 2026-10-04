"""Actual Tk focus transitions, without starting services or a global hook."""
import queue
import sys
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch

from windows import GlobalHotkeys


@unittest.skipUnless(sys.platform=='win32','Windows native foreground handles')
class HotkeyInputUITests(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk();self.root.geometry('360x260')
        self.hotkeys=GlobalHotkeys(queue.Queue())
        self.events=[];self.errors=[]
        self.root.report_callback_exception=lambda *error:self.errors.append(error)
        self.root.bind('<FocusIn>',lambda event:self.events.append(event.widget),add='+')
        self.hotkeys.register_window(self.root)

    def tearDown(self):
        self.root.destroy()
        self.assertEqual(self.errors,[])

    def focused(self,widget):
        widget.pack();self.root.update();widget.focus_force();self.root.update()
        self.assertIs(self.hotkeys.input_focus[1],widget)
        self.assertIn(widget,self.events)
        with patch('windows.ctypes.windll.user32.GetForegroundWindow',return_value=self.hotkeys.windows[self.root]):
            self.assertFalse(self.hotkeys.focused(plain_arrow=True))
            self.assertTrue(self.hotkeys.focused())

    def test_entry_text_and_readonly_spinbox_real_focus_and_destroy(self):
        for widget in (tk.Entry(self.root),ttk.Entry(self.root),tk.Text(self.root,height=2),
                       ttk.Spinbox(self.root,state='readonly',from_=1,to=5)):
            self.focused(widget)
            widget.destroy();self.root.update()
            self.assertIsNone(self.hotkeys.input_focus)

    def test_readonly_combo_popup_retains_guard_and_direction_selection(self):
        combo=ttk.Combobox(self.root,state='readonly',values=('first','second','third'))
        combo.current(0);self.focused(combo)
        self.root.tk.call('ttk::combobox::Post',str(combo));self.root.update()
        focus=str(self.root.tk.call('focus'))
        self.assertEqual(str(self.root.tk.call('winfo','class',focus)),'Listbox')
        self.assertIs(self.hotkeys.input_focus[1],combo)
        with patch('windows.ctypes.windll.user32.GetForegroundWindow',return_value=self.hotkeys.windows[self.root]):
            self.assertFalse(self.hotkeys.focused(plain_arrow=True))
        self.root.tk.call('event','generate',focus,'<Down>');self.root.update()
        self.root.tk.call('event','generate',focus,'<Return>');self.root.update()
        self.assertEqual(combo.current(),1)
        self.assertIs(self.hotkeys.input_focus[1],combo)
        tree=ttk.Treeview(self.root,height=1);tree.pack();self.root.update()
        tree.focus_force();self.root.update()
        self.assertIsNone(self.hotkeys.input_focus)
        with patch('windows.ctypes.windll.user32.GetForegroundWindow',return_value=self.hotkeys.windows[self.root]):
            self.assertTrue(self.hotkeys.focused(plain_arrow=True))

    def test_closing_editor_cancels_deferred_combo_callback_before_widget_commands_disappear(self):
        editor=tk.Toplevel(self.root);self.hotkeys.register_window(editor)
        combo=ttk.Combobox(editor,state='readonly',values=('one','two'));combo.pack()
        self.root.update();combo.focus_force();self.root.update()
        before=set(self.root.tk.call('after','info'))
        combo.event_generate('<FocusOut>')
        pending=set(self.root.tk.call('after','info'))-before
        self.assertEqual(len(pending),1)
        editor.destroy()
        self.assertTrue(pending.isdisjoint(self.root.tk.call('after','info')))
        self.root.update()
        self.assertIsNone(self.hotkeys.input_focus)


if __name__=='__main__':unittest.main()
