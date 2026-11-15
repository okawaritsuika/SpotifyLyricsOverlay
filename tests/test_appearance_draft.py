from copy import deepcopy
import ctypes
from ctypes import wintypes
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch
from appearance import AppearanceDialog, styles_from_settings, compact_from_settings
from desktop_workspace import appearance_draft
from types import SimpleNamespace

class AppearanceDraftTests(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk(); self.root.withdraw()
        self.app=SimpleNamespace(root=self.root,text_styles=styles_from_settings({}),
            show_original=tk.BooleanVar(value=True),show_reading=tk.BooleanVar(value=True),
            opacity=tk.DoubleVar(value=.94),background_opacity=tk.DoubleVar(value=.55),
            transparent=tk.BooleanVar(value=False),background_color='#10151f',
            compact_layout=compact_from_settings({}),font_size=tk.IntVar(value=24),settings={},appearance_dialog=None)
        self.saved={}
        self.app.apply_appearance=lambda:None
        self.app.save_settings=lambda:self.saved.update(text_styles=deepcopy(self.app.text_styles),appearance_presets=deepcopy(self.app.settings.get('appearance_presets',{})))
        self.open_dialog()
    def open_dialog(self):
        self.d=AppearanceDialog(self.app); self.app.appearance_dialog=self.d; self.root.update()
    def tearDown(self):
        self.root.destroy()
    def test_cancel_and_apply(self):
        before=deepcopy(self.app.text_styles); self.d.values['original'][2].set(31); self.d.update(); self.root.update()
        self.assertEqual(self.app.text_styles,before)
        self.d.close(); self.assertEqual(self.app.text_styles,before)
        self.open_dialog(); self.d.values['original'][2].set(31); self.d.apply()
        self.assertEqual(self.app.text_styles['original']['size'],31)
        self.assertEqual(self.saved['text_styles']['original']['size'],31)
    def test_presets_duplicate_cancel_overwrite_and_load(self):
        self.d.values['korean'][2].set(25)
        with patch('appearance.messagebox.askyesnocancel',return_value=True),patch('appearance.simpledialog.askstring',return_value='My preset'):self.d.save_preset()
        saved=deepcopy(self.app.settings['appearance_presets']); self.assertIn('My preset',saved)
        self.d.values['korean'][2].set(35)
        with patch('appearance.messagebox.askyesnocancel',return_value=True),patch('appearance.simpledialog.askstring',return_value='my preset'),patch('appearance.messagebox.showerror'):self.d.save_preset()
        self.assertEqual(self.app.settings['appearance_presets'],saved)
        with patch('appearance.messagebox.askyesnocancel',return_value=None):self.d.save_preset()
        self.assertEqual(self.app.settings['appearance_presets'],saved)
        self.d.load_preset(); self.assertEqual(self.d.values['korean'][2].get(),25)
        self.d.values['korean'][2].set(35)
        with patch('appearance.messagebox.askyesnocancel',return_value=False),patch('appearance.messagebox.askyesno',return_value=True):self.d.save_preset()
        self.assertEqual(self.app.settings['appearance_presets']['My preset']['text_styles']['korean']['size'],35)
    def test_preview_updates_and_side_visibility(self):
        self.d.preview.geometry('1000x280'); self.root.update()
        before=self.d.preview_lines['original']
        self.d.values['original'][2].set(31)
        self.d.values['previous'][0].set(True); self.d.values['next'][0].set(False); self.d.update(); self.root.update()
        self.assertIs(self.d.preview_lines['original'],before)
        self.assertEqual(before.cget('font')[1],31)
        self.assertEqual(self.app.text_styles['original']['size'],24)
        self.assertEqual(self.d.preview_sides['previous'].winfo_manager(),'place')
        self.assertEqual(self.d.preview_sides['next'].winfo_manager(),'')
        previous=self.d.preview_sides['previous']
        self.assertEqual(previous.place_info()['anchor'],'w')
        self.assertTrue(previous.itemcget(previous.find_withtag('lyric')[-1],'text').startswith('…'))
        self.d.preview.geometry('300x260'); self.root.update()
        self.assertEqual(self.d.preview_sides['previous'].winfo_manager(),'')
        self.d.preview.geometry('1000x280'); self.d.values['next'][0].set(True); self.d.update(); self.root.update()
        self.assertEqual(self.d.preview_sides['next'].winfo_manager(),'place')
        upcoming=self.d.preview_sides['next']
        self.assertEqual(upcoming.place_info()['anchor'],'e')
        self.assertTrue(upcoming.itemcget(upcoming.find_withtag('lyric')[-1],'text').endswith('…'))

    def test_outline_blur_changes_the_edge_without_softening_the_foreground(self):
        self.d.preview.geometry('1000x350')
        self.root.update()
        original=self.d.preview_lines['original']
        self.d._set_outline('outline_width',4)
        self.d._set_outline('outline_color','#ff44bb')
        self.root.update()
        hard=original._artist_image
        self.d._set_outline('outline_blur',3)
        self.root.update()
        soft=original._artist_image
        self.assertTrue(original._artist_layer._visible)
        self.assertGreater(soft.height,hard.height)
        self.assertNotEqual(hard.tobytes(),soft.tobytes())
        foreground=(244,247,252,255)
        self.assertGreater(sum(pixel==foreground for pixel in hard.get_flattened_data()),0)
        self.assertEqual(sum(pixel==foreground for pixel in hard.get_flattened_data()),
                         sum(pixel==foreground for pixel in soft.get_flattened_data()))
        self.assertTrue(any(r>200 and g<100 and b>120 and 0<a<200
                            for r,g,b,a in soft.get_flattened_data()))
        self.assertEqual(self.app.text_styles['original']['outline_width'],2)
        self.d._set_outline('outline_width',0)
        self.root.update()
        self.assertFalse(original._artist_layer._visible)
        self.d._set_outline('outline_width',4)
        self.d.apply()
        style=self.saved['text_styles']['original']
        self.assertEqual((style['outline_width'],style['outline_blur'],style['outline_color']),
                         (4,3,'#ff44bb'))

    def test_tenths_of_a_pixel_produce_distinct_outline_effects(self):
        self.d.preview.geometry('1000x350')
        self.d._set_outline('outline_width',.1)
        self.root.update()
        original=self.d.preview_lines['original']
        thin=original._artist_image
        self.d._set_outline('outline_width',.2)
        self.root.update()
        thicker=original._artist_image
        self.assertEqual(thin.size,thicker.size)
        self.assertNotEqual(thin.tobytes(),thicker.tobytes())
        self.d._set_outline('outline_width',2.3)
        self.d._set_outline('outline_blur',.1)
        self.root.update()
        sharp=original._artist_image
        self.d._set_outline('outline_blur',.2)
        self.root.update()
        soft=original._artist_image
        self.assertEqual(sharp.size,soft.size)
        self.assertNotEqual(sharp.tobytes(),soft.tobytes())

    def test_preview_backdrop_and_singer_changes_are_draft_only(self):
        self.d.preview.geometry('1000x280'); self.root.update()
        before=self.d.preview_singer_image
        self.d.styles['singers']['color']='#b81761'; self.d.update(); self.root.update()
        self.assertIsNot(self.d.preview_singer_image,before)
        self.assertIsNone(self.d.preview_headers['title']._artist_layer)
        self.d.background_opacity.set(.3); self.d.opacity.set(.7); self.d.values['singers'][0].set(False); self.d.update(); self.root.update()
        self.assertAlmostEqual(self.d.preview_backdrop.opacity,.3)
        self.assertAlmostEqual(float(self.d.preview.attributes('-alpha')),.7,places=2)
        self.assertEqual(self.d.preview_singers.winfo_manager(),'')
        self.assertEqual(self.app.background_opacity.get(),.55)
        self.d.transparent.set(True); self.d.update(); self.root.update()
        self.assertEqual(self.d.preview_backdrop.opacity,0)
        self.assertTrue(self.d.preview_backdrop.window.state()=='withdrawn')
        self.d.preview.withdraw(); self.root.update()
        self.d.show_preview(); self.root.update()
        self.assertTrue(self.d.preview.winfo_viewable())

    def test_native_preview_updates_visible_draft_only(self):
        draft=appearance_draft(self.app)
        draft.update(opacity=.7, background_opacity=.3)
        self.d.set_draft(draft)
        self.d.show_preview()
        self.root.update()
        self.assertTrue(self.d.preview.winfo_viewable())
        self.assertTrue(bool(self.d.preview.attributes('-topmost')))
        self.assertTrue(bool(self.d.preview_backdrop.window.attributes('-topmost')))
        draft.update(opacity=.4, background_opacity=.8, background_color='#9b3275')
        self.d.set_draft(draft)
        self.root.update()
        self.assertAlmostEqual(float(self.d.preview.attributes('-alpha')),.4,places=2)
        self.assertAlmostEqual(float(self.d.preview_backdrop.window.attributes('-alpha')),.8,places=2)
        self.assertEqual(self.d.preview_backdrop.window.cget('bg'),'#9b3275')
        user32=ctypes.windll.user32
        user32.GetAncestor.argtypes=(wintypes.HWND,wintypes.UINT)
        user32.GetAncestor.restype=wintypes.HWND
        inspect=user32.GetLayeredWindowAttributes
        inspect.argtypes=(wintypes.HWND,ctypes.POINTER(wintypes.DWORD),ctypes.POINTER(ctypes.c_ubyte),ctypes.POINTER(wintypes.DWORD))
        for window,value in ((self.d.preview,.4),(self.d.preview_backdrop.window,.8)):
            key,alpha,flags=wintypes.DWORD(),ctypes.c_ubyte(),wintypes.DWORD()
            self.assertTrue(inspect(user32.GetAncestor(window.winfo_id(),2),ctypes.byref(key),ctypes.byref(alpha),ctypes.byref(flags)))
            self.assertTrue(flags.value & 2)
            self.assertAlmostEqual(alpha.value/255,value,places=2)
        self.assertEqual(self.app.background_opacity.get(),.55)
        self.assertEqual(self.app.opacity.get(),.94)
        self.d.preview.withdraw()
        self.d.set_draft(draft)
        self.assertEqual(self.d.preview.state(),'withdrawn')

    def test_background_picker_makes_a_hidden_background_visible_in_preview(self):
        self.d.transparent.set(True)
        self.d.background_opacity.set(0)
        with patch('appearance.colorchooser.askcolor',return_value=((200,40,60),'#c8283c')):
            self.d.background_color()
        self.assertFalse(self.d.transparent.get())
        self.assertGreater(self.d.preview_backdrop.opacity,0)
        self.assertEqual(self.d.preview_backdrop.color,'#c8283c')
        self.assertEqual(self.app.background_color,'#10151f')

    def test_separate_preview_stays_above_an_overlapping_window(self):
        self.d.preview.geometry('600x200+100+100')
        self.d.show_preview()
        self.root.update()
        cover=tk.Toplevel(self.root)
        cover.geometry('650x250+80+80')
        cover.lift()
        cover.focus_force()
        self.root.update()

        user32=ctypes.windll.user32
        user32.GetAncestor.argtypes=(wintypes.HWND,wintypes.UINT)
        user32.GetAncestor.restype=wintypes.HWND
        user32.GetWindow.argtypes=(wintypes.HWND,wintypes.UINT)
        user32.GetWindow.restype=wintypes.HWND
        hwnd=lambda window:user32.GetAncestor(window.winfo_id(),2)
        windows=[hwnd(self.d.preview),hwnd(self.d.preview_backdrop.window),hwnd(cover)]
        order=[]
        current=user32.GetWindow(windows[-1],0)  # GW_HWNDFIRST
        while current and current not in order:
            order.append(current)
            if current==windows[-1]:break
            current=user32.GetWindow(current,2)  # GW_HWNDNEXT
        self.assertEqual([handle for handle in order if handle in windows],windows)

if __name__=='__main__':unittest.main()
