"""Actual-alpha pixel conversion and lifetime checks using an isolated Tk window."""
import ctypes
from ctypes import wintypes
import sys
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from PIL import Image

from artist_layer import ArtistLayer, premultiplied_bgra


class ArtistPixelsTests(unittest.TestCase):
    def test_premultiplied_bgra_keeps_alpha_and_clears_transparent_rgb(self):
        artwork=Image.new('RGBA',(3,1))
        artwork.putdata([(255,100,50,128),(201,20,255,0),(10,20,30,255)])
        before=artwork.tobytes()
        self.assertEqual(premultiplied_bgra(artwork),
                         bytes([25,50,128,128,0,0,0,0,30,20,10,255]))
        self.assertEqual(artwork.tobytes(),before)

    def test_conversion_preserves_top_to_bottom_scanline_order(self):
        artwork=Image.new('RGBA',(1,2))
        artwork.putdata([(255,0,0,255),(0,0,255,255)])
        self.assertEqual(premultiplied_bgra(artwork),bytes([0,0,255,255,255,0,0,255]))

    def test_non_rgba_image_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'RGBA'):
            premultiplied_bgra(Image.new('RGB',(1,1)))


@unittest.skipUnless(sys.platform=='win32','Native artwork requires Windows')
class ArtistLayerTests(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk();self.root.withdraw()
        self.root.geometry('220x80+-10000+-10000')
        self.widget=tk.Canvas(self.root,width=200,height=40)
        self.widget.pack()
        self.root.update_idletasks()
        self.layer=ArtistLayer(self.widget)
        self.artwork=Image.new('RGBA',(80,30),(200,100,50,128))
        self.user=ctypes.WinDLL('user32',use_last_error=True)
        for name,result,arguments in (
            ('GetWindowLongW',wintypes.LONG,[wintypes.HWND,ctypes.c_int]),
            ('GetWindowRect',wintypes.BOOL,[wintypes.HWND,ctypes.POINTER(wintypes.RECT)]),
            ('GetForegroundWindow',wintypes.HWND,[]),
            ('IsWindowVisible',wintypes.BOOL,[wintypes.HWND]),
            ('GetGuiResources',wintypes.DWORD,[wintypes.HANDLE,wintypes.DWORD])):
            function=getattr(self.user,name)
            function.restype=result;function.argtypes=arguments
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.GetCurrentProcess.restype=wintypes.HANDLE
        self.process=kernel.GetCurrentProcess()

    def tearDown(self):
        self.layer.close()
        if self.root is not None:self.root.destroy()

    def test_show_is_owned_and_does_not_activate(self):
        foreground=self.user.GetForegroundWindow()
        self.layer.show(self.artwork,-10000,-9990,.75)
        api=self.layer._api
        self.assertEqual(api.GetWindow(self.layer._hwnd,4),self.layer._owner)
        style=self.user.GetWindowLongW(self.layer._hwnd,-20)
        required=0x80000|0x08000000|0x80
        self.assertEqual(style&required,required)
        self.assertFalse(style&0x20)
        rectangle=wintypes.RECT()
        self.assertTrue(self.user.GetWindowRect(self.layer._hwnd,ctypes.byref(rectangle)))
        self.assertEqual((rectangle.left,rectangle.top,rectangle.right,rectangle.bottom),
                         (-10000,-9990,-9920,-9960))
        self.assertEqual(self.user.GetForegroundWindow(),foreground)
        self.assertTrue(self.user.IsWindowVisible(self.layer._hwnd))
        self.layer.hide()
        self.assertFalse(self.user.IsWindowVisible(self.layer._hwnd))
        self.assertIsNone(self.layer._poll_task)
        self.layer.show(self.artwork,-10000,-9990)
        self.assertIsNotNone(self.layer._poll_task)

    def test_repeated_updates_release_gdi_resources_even_after_failure(self):
        self.layer.show(self.artwork,-10000,-10000)
        baseline=self.user.GetGuiResources(self.process,0)
        for _ in range(30):self.layer.show(self.artwork,-10000,-10000)
        self.assertLessEqual(self.user.GetGuiResources(self.process,0),baseline)
        with patch.object(self.layer._api,'UpdateLayeredWindow',return_value=0):
            with self.assertRaises(OSError):
                self.layer.show(self.artwork,-10000,-10000)
        self.assertLessEqual(self.user.GetGuiResources(self.process,0),baseline)

    def test_layer_stays_under_an_existing_owned_popup(self):
        api=self.layer._api
        self.layer.show(self.artwork,-10000,-10000)
        popup=api.CreateWindowExW(0x08000080,'STATIC','Isolated artist test popup',
            0x80000000,-10000,-10000,30,30,self.layer._owner,None,None,None)
        self.assertTrue(popup)
        try:
            api.ShowWindow(popup,4)
            self.layer.show(self.artwork,-10000,-10000)
            self.assertEqual(api.GetWindow(self.layer._owner,3),self.layer._hwnd)
            self.assertEqual(api.GetWindow(self.layer._hwnd,3),popup)
        finally:api.DestroyWindow(popup)

    def test_close_is_idempotent_after_owner_destruction(self):
        hwnd=self.layer._hwnd
        self.root.destroy();self.root=None
        self.assertFalse(self.layer._api.IsWindow(hwnd))
        self.layer.close();self.layer.close();self.layer.hide()
        with self.assertRaisesRegex(RuntimeError,'closed'):
            self.layer.show(self.artwork,0,0)

    def test_bad_opacity_is_rejected_before_allocating(self):
        for opacity in (-.1,1.1,float('nan'),float('inf')):
            with self.subTest(opacity=opacity),self.assertRaises(ValueError):
                self.layer.show(self.artwork,0,0,opacity)

    def test_application_can_cancel_root_timers_before_destroying_widgets(self):
        self.layer.show(self.artwork,-10000,-10000)
        for pending in self.root.tk.splitlist(self.root.tk.call('after','info')):
            self.root.after_cancel(pending)
        self.layer.close()
        self.widget.destroy()
        self.root.destroy();self.root=None

    def test_owner_wrapper_replacement_recreates_the_layer(self):
        self.root.deiconify();self.root.update()
        self.layer.show(self.artwork,-10000,-10000)
        previous_owner=self.layer._owner
        for borderless in (True,False):
            self.root.overrideredirect(borderless);self.root.update()
            self.layer.show(self.artwork,-10000,-10000)
            api=self.layer._api
            self.assertNotEqual(self.layer._owner,previous_owner)
            self.assertTrue(api.IsWindow(self.layer._hwnd))
            self.assertEqual(api.GetWindow(self.layer._hwnd,4),self.layer._owner)
            previous_owner=self.layer._owner

    def test_click_through_follows_the_owner_lock(self):
        api=self.layer._api
        original=api.GetWindowLongPtrW(self.layer._owner,-20)
        for locked in (True,False):
            api.SetWindowLongPtrW(self.layer._owner,-20,original|0x20 if locked else original&~0x20)
            self.layer.show(self.artwork,-10000,-10000)
            self.assertEqual(bool(api.GetWindowLongPtrW(self.layer._hwnd,-20)&0x20),locked)

    def test_native_pointer_messages_reach_the_canvas(self):
        self.root.deiconify();self.root.update()
        self.layer.show(self.artwork,self.widget.winfo_rootx(),self.widget.winfo_rooty())
        events=[]
        for sequence in ('<Enter>','<Leave>','<ButtonPress-1>','<B1-Motion>',
                         '<ButtonRelease-1>','<ButtonPress-3>','<MouseWheel>'):
            self.widget.bind(sequence,lambda event,sequence=sequence:events.append(sequence),add='+')
        api=self.layer._api
        point=8|(8<<16)
        for message,wparam,lparam in ((0x200,0,point),(0x201,1,point),(0x200,1,point),
                                      (0x202,0,point),(0x204,2,point),(0x205,0,point),
                                      (0x20A,120<<16,point),(0x2A3,0,0)):
            api.SendMessageW(self.layer._hwnd,message,wparam,lparam)
            self.root.after(22,self.root.quit);self.root.mainloop()
            self.root.update()
        for sequence in ('<Enter>','<Leave>','<ButtonPress-1>','<B1-Motion>',
                         '<ButtonRelease-1>','<ButtonPress-3>','<MouseWheel>'):
            self.assertIn(sequence,events)

    def test_focused_owner_wheel_routes_only_the_visible_artist_viewport_once(self):
        self.root.deiconify();self.root.update()
        x,y=self.widget.winfo_rootx(),self.widget.winfo_rooty()
        self.layer.show(self.artwork,x,y)
        received=[]
        self.widget.bind('<MouseWheel>',lambda event:received.append(event.delta),add='+')
        self.root.event_generate('<MouseWheel>',delta=-120,rootx=x+8,rooty=y+8)
        self.root.update()
        self.assertEqual(received,[-120])
        self.widget.event_generate('<MouseWheel>',delta=120,rootx=x+8,rooty=y+8)
        self.root.update()
        self.assertEqual(received,[-120,120])

        self.root.event_generate('<MouseWheel>',delta=-120,rootx=x+200,rooty=y+8)
        self.root.update()
        self.assertEqual(received,[-120,120])
        self.layer.hide()
        self.root.event_generate('<MouseWheel>',delta=-120,rootx=x+8,rooty=y+8)
        self.root.update()
        self.assertEqual(received,[-120,120])
        self.layer.show(self.artwork,x,y)
        api=self.layer._api
        style=api.GetWindowLongPtrW(self.layer._owner,-20)
        api.SetWindowLongPtrW(self.layer._owner,-20,style|0x20)
        self.root.event_generate('<MouseWheel>',delta=-120,rootx=x+8,rooty=y+8)
        self.root.update()
        self.assertEqual(received,[-120,120])

    def test_native_wheel_on_either_owner_window_reaches_canvas(self):
        self.root.deiconify();self.root.update()
        x,y=self.widget.winfo_rootx(),self.widget.winfo_rooty()
        self.layer.show(self.artwork,x,y)
        received=[]
        self.widget.bind('<MouseWheel>',lambda event:received.append(event.delta),add='+')
        screen=((x+8)&0xffff)|(((y+8)&0xffff)<<16)
        for hwnd in (self.root.winfo_id(),self.layer._owner):
            self.layer._api.SendMessageW(hwnd,0x20A,(0xff88<<16),screen)
        deadline=time.monotonic()+1
        while len(received)<2 and time.monotonic()<deadline:
            self.root.after(10,self.root.quit);self.root.mainloop()
            self.root.update()
        self.assertEqual(received,[-120,-120])
        self.assertEqual(self.layer._wheel_counts['routed'],2)
        self.assertEqual(self.layer._wheel_counts['owner'],2)


if __name__=='__main__':unittest.main()
