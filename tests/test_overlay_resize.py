"""Exercise resize handles and compact layout with an isolated, service-free Tk app."""
from copy import deepcopy
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from app import LyricsApp, visible_overlay_rect
from lyric_performers import MARKER_WIDTH
from lyrics import Line, Song
from media import Playback
from storage import Store
from windows import WindowActivation, window_position, work_area


class OverlayResizeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.song = Song('resize', 'Resize example', '黛冬優子',
                         'THE IDOLM@STER SHINY COLORS Song for Prism',
                         (Line('短い歌詞', 'みじかいかし', '짧은 가사'),
                          Line('次の歌詞を歌う', 'つぎのかしをうたう', '다음 가사를 부르네')))
        self.store.save_library([self.song])
        self.store.save_settings({'online': False, 'use_estimate': False,
                                  'transparent': True, 'borderless': False})
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = LyricsApp(self.root, self.store, start_services=False)
        self.app.set_playback(Playback(self.song.title, self.song.artist,
                                      self.song.album, 10, 180, False, time.monotonic()))
        self.app.follow.set(False)
        self.app.manual_index = 0
        self.app.chrome_available = True
        self.app.toggle_chrome()
        self.root.deiconify()
        self.root.update()

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def test_lyric_area_and_font_keep_their_size_when_singer_count_changes(self):
        names=('黛冬優子','園田智代子','鈴木羽那','七草にちか','大崎甜花')
        self.app.compact_layout['mode']='shrink_only'
        self.size(560,220)
        with patch.object(self.app,'current_line_singers') as singers:
            layouts=[]
            for current in (names,names[:1],()):
                singers.return_value=current
                self.app.render()
                self.root.update()
                content=self.app.lyric_content
                layouts.append((content.winfo_width(),content.winfo_height(),self.app.lyric_scale,
                                self.app.original_label.cget('font')))
            self.assertEqual(layouts,[layouts[0]]*3)

    def test_short_lyric_triplet_stays_together_with_a_longer_line_in_the_song(self):
        self.app.compact_layout['mode']='shrink_only'
        self.app.display_lines=(Line('短い歌詞','みじかいかし','짧은 가사'),
                                Line('長い歌詞を表示します。'*6,'ながいかしをひょうじします。'*6,'긴 가사입니다. '*6),
                                Line('English lyric','','영어 가사'))
        self.app.last_render=None
        self.app.render()
        for width,height in ((760,360),(900,440)):
            self.size(width,height)
            reading,original,korean=self.app.lyric_labels.values()
            self.assertTrue(all(label.winfo_ismapped() for label in (reading,original,korean)))
            for upper,lower in ((reading,original),(original,korean)):
                upper_bottom=upper.winfo_rooty()+upper.bbox('lyric')[3]
                lower_top=lower.winfo_rooty()+lower.bbox('lyric')[1]
                self.assertLessEqual(lower_top-upper_bottom,12)
                self.assertGreaterEqual(lower_top-upper_bottom,0)
        self.app.manual_index=2
        self.app.render()
        self.root.update()
        self.assertFalse(reading.winfo_ismapped())
        self.assertTrue(original.winfo_ismapped())
        self.assertFalse(korean.winfo_ismapped())

    def rect(self):
        return (self.root.winfo_x(), self.root.winfo_y(),
                self.root.winfo_width(), self.root.winfo_height())

    def size(self, width, height):
        self.app.manual_window_size = True
        self.root.minsize(240, 100)
        self.root.geometry(f'{width}x{height}+100+100')
        self.root.update()
        self.root.update()

    def drag(self, edge, dx, dy):
        handle = self.app.resize_handles[edge]
        x, y = max(1, handle.winfo_width() // 2), max(1, handle.winfo_height() // 2)
        rootx, rooty = handle.winfo_rootx() + x, handle.winfo_rooty() + y
        handle.event_generate('<ButtonPress-1>', x=x, y=y, rootx=rootx, rooty=rooty)
        self.root.update()
        handle.event_generate('<B1-Motion>', x=x + dx, y=y + dy,
                              rootx=rootx + dx, rooty=rooty + dy, state=256)
        self.root.update()
        handle.event_generate('<ButtonRelease-1>', x=x + dx, y=y + dy,
                              rootx=rootx + dx, rooty=rooty + dy)
        self.root.update()

    def test_all_eight_handles_change_only_the_selected_axes(self):
        self.assertEqual(set(self.app.resize_handles), {'n', 's', 'w', 'e', 'nw', 'ne', 'sw', 'se'})
        for edge in self.app.resize_handles:
            with self.subTest(edge=edge):
                self.size(900, 400)
                before = self.rect()
                self.drag(edge, 55, 35)
                x, y, width, height = self.rect()
                self.assertEqual(width, before[2] + (55 if 'e' in edge else -55 if 'w' in edge else 0))
                self.assertEqual(height, before[3] + (35 if 's' in edge else -35 if 'n' in edge else 0))
                self.assertEqual(x + width if 'w' in edge else x,
                                 before[0] + before[2] if 'w' in edge else before[0])
                self.assertEqual(y + height if 'n' in edge else y,
                                 before[1] + before[3] if 'n' in edge else before[1])
                self.assertIsNone(self.app.resize_origin)
                self.assertIsNone(self.root.grab_current())

    def test_volume_stays_accessible_without_overlapping_transport_controls(self):
        buttons=(self.app.previous_track_button,self.app.track_start_button,
                 self.app.play_pause_button,self.app.volume_button,
                 self.app.track_end_button,self.app.next_track_button)
        for width,height in ((1040,400),(500,200),(240,100)):
            with self.subTest(size=(width,height)):
                self.size(width,height)
                for button in buttons:
                    self.assertTrue(button.winfo_ismapped())
                    self.assertGreaterEqual(button.winfo_rootx(),self.root.winfo_rootx()+10)
                    self.assertLessEqual(button.winfo_rootx()+button.winfo_width(),
                                         self.root.winfo_rootx()+width-10)
                    self.assertLessEqual(button.winfo_rooty()+button.winfo_height(),
                                         self.root.winfo_rooty()+height-4)
                    self.assertTrue(button.cget('image'))
                for before,after in zip(buttons,buttons[1:]):
                    self.assertLessEqual(before.winfo_rootx()+before.winfo_width(),after.winfo_rootx())
                badge=self.app.playback_badge
                self.assertTrue(badge.winfo_ismapped())
                self.assertEqual(badge.cget('text'),'00:10 / 03:00')
                self.assertEqual(badge.winfo_rootx()+badge.winfo_width()/2,self.root.winfo_rootx()+width/2)
                self.assertLessEqual(self.app.play_pause_button.winfo_rootx()+self.app.play_pause_button.winfo_width(),
                                     badge.winfo_rootx())
                self.assertLessEqual(badge.winfo_rootx()+badge.winfo_width(),self.app.volume_button.winfo_rootx())
                self.assertLessEqual(badge.bbox('lyric')[2],badge.winfo_width())

    def test_minimum_size_keeps_the_opposite_corner_fixed(self):
        self.size(900, 400)
        x, y, width, height = self.rect()
        self.drag('nw', 2000, 2000)
        actual_x, actual_y, actual_width, actual_height = self.rect()
        self.assertEqual((actual_width, actual_height), self.root.minsize())
        self.assertEqual((actual_x + actual_width, actual_y + actual_height), (x + width, y + height))
        self.drag('se', -2000, -2000)
        self.assertEqual(self.rect(), (actual_x, actual_y, actual_width, actual_height))

    def test_first_drag_switches_to_saved_manual_size(self):
        self.assertFalse(self.app.manual_window_size)
        before = self.rect()
        self.drag('se', -90, 30)
        self.assertTrue(self.app.manual_window_size)
        self.assertEqual(self.rect()[2:], (before[2] - 90, before[3] + 30))
        settings = self.store.settings()
        self.assertTrue(settings['manual_window_size'])
        self.assertEqual(settings['window_rect'], [self.rect()[2], self.rect()[3], *self.rect()[:2]])

    def test_saved_overlay_keeps_a_connected_secondary_monitor(self):
        rect = [650, 240, -950, 50]
        with patch('app.monitor_work_area', return_value=(-1280, 0, 0, 720)):
            self.assertEqual(visible_overlay_rect(rect), tuple(rect))
        with patch('app.monitor_work_area', return_value=None), patch('app.work_area', return_value=(0, 0, 1920, 1032)):
            self.assertEqual(visible_overlay_rect(rect), (650, 240, 635, 762))

    def test_live_offscreen_overlay_recovers_and_saves_its_new_position(self):
        self.size(650, 240)
        _, _, right, _ = work_area()
        self.root.geometry('650x240' + window_position(right + 100, 60))
        self.root.update_idletasks()
        self.assertEqual(self.root.winfo_x(), right + 100)
        with patch('app.monitor_work_area', return_value=None):
            self.app.watch_overlay_position(None)
            self.root.update_idletasks()
        left, top, right, bottom = work_area()
        x, y, width, height = self.rect()
        self.assertGreaterEqual(x, left)
        self.assertGreaterEqual(y, top)
        self.assertLessEqual(x + width, right)
        self.assertLessEqual(y + height, bottom)
        self.assertEqual(self.store.settings()['window_rect'], [width, height, x, y])

    def test_manual_relaunch_shows_a_hidden_borderless_overlay(self):
        self.size(650, 240)
        left, top, right, bottom = work_area()
        self.root.geometry('650x240' + window_position(right - 100, 60))
        self.root.update_idletasks()
        self.root.withdraw()
        self.root.update_idletasks()
        with WindowActivation(self.store.directory, owner=True) as receiver:
            with WindowActivation(self.store.directory, owner=False) as launcher:
                self.assertTrue(launcher.request())
            with patch('app.monitor_work_area', return_value=(left, top, right, bottom)):
                self.app.watch_overlay_position(receiver)
                self.root.update_idletasks()
            self.assertTrue(self.root.winfo_viewable())
            self.assertTrue(self.root.overrideredirect())
            self.assertEqual(self.root.winfo_x() + self.root.winfo_width(), right)
            self.assertFalse(receiver.consume())

    def test_locked_overlay_ignores_resize_and_does_not_capture_input(self):
        self.size(900, 400)
        before = self.rect()
        self.app.locked = True
        self.drag('nw', 150, 100)
        self.assertEqual(self.rect(), before)
        self.assertIsNone(self.app.resize_origin)
        self.assertIsNone(self.root.grab_current())
        self.app.locked = False

    def test_compact_stages_hide_credits_then_reading_then_reduce_fonts_and_restore(self):
        original_styles = deepcopy(self.app.text_styles)
        original_preferences = (self.app.font_size.get(), self.app.show_reading.get(), self.app.show_original.get())
        with patch.object(self.app, 'current_line_singers', return_value=('黛冬優子',)):
            self.app.last_render = None
            self.app.render()
            self.size(1040, 600)
            self.assertTrue(self.app.track_metadata.winfo_ismapped())
            self.assertTrue(self.app.current_singers.winfo_ismapped())
            self.assertTrue(self.app.reading_label.winfo_ismapped())
            self.assertEqual(self.app.lyric_scale, 1)

            self.size(650, 500)
            self.assertFalse(self.app.track_metadata.winfo_ismapped())
            self.assertFalse(self.app.current_singers.winfo_ismapped())
            self.assertTrue(self.app.reading_label.winfo_ismapped())
            self.assertEqual(self.app.lyric_scale, 1)

            self.size(500, 500)
            self.assertFalse(self.app.reading_label.winfo_ismapped())
            self.assertEqual(self.app.lyric_scale, 1)

            self.size(300, 160)
            self.assertLess(self.app.lyric_scale, 1)
            self.assertLess(self.app.original_label.cget('font')[1], original_styles['original']['size'])
            self.assertLess(self.app.korean_label.cget('font')[1], original_styles['korean']['size'])
            self.assertTrue(self.app.original_label.winfo_ismapped())
            self.assertTrue(self.app.korean_label.winfo_ismapped())
            for index in (1, 0, 1):
                self.app.manual_index = index
                self.app.render()
                self.root.update()
                self.assertFalse(self.app.track_metadata.winfo_ismapped())
                self.assertFalse(self.app.current_singers.winfo_ismapped())
                self.assertFalse(self.app.current_singers._artist_layer._visible)
                self.assertFalse(self.app.reading_label.winfo_ismapped())

            self.size(1040, 600)
            self.assertTrue(self.app.track_metadata.winfo_ismapped())
            self.assertTrue(self.app.current_singers.winfo_ismapped())
            self.assertTrue(self.app.reading_label.winfo_ismapped())
            self.assertEqual(self.app.lyric_scale, 1)
            for key, label in (('original', self.app.original_label), ('reading', self.app.reading_label), ('korean', self.app.korean_label)):
                self.assertEqual(label.cget('font')[1], original_styles[key]['size'])
        self.assertEqual(self.app.text_styles, original_styles)
        self.assertEqual((self.app.font_size.get(), self.app.show_reading.get(), self.app.show_original.get()), original_preferences)
        self.app.save_settings()
        self.assertEqual(self.store.settings()['text_styles'], original_styles)

    def test_height_only_resize_compacts_without_changing_width(self):
        self.size(1040, 600)
        self.assertFalse(self.app.compact_metadata)
        self.drag('s', 0, -500)
        self.assertEqual(self.root.winfo_width(), 1040)
        self.assertTrue(self.app.compact_metadata)
        self.assertTrue(self.app.compact_reading)
        self.assertLess(self.app.lyric_scale, 1)

    def test_dragging_back_to_start_restores_the_initial_rectangle(self):
        self.size(900,400)
        before=self.rect()
        handle=self.app.resize_handles['se']
        x,y=handle.winfo_rootx()+5,handle.winfo_rooty()+5
        handle.event_generate('<ButtonPress-1>',x=5,y=5,rootx=x,rooty=y)
        handle.event_generate('<B1-Motion>',x=-95,y=-45,rootx=x-100,rooty=y-50,state=256)
        self.root.update()
        handle.event_generate('<B1-Motion>',x=5,y=5,rootx=x,rooty=y,state=256)
        handle.event_generate('<ButtonRelease-1>',x=5,y=5,rootx=x,rooty=y)
        self.root.update()
        self.assertEqual(self.rect(),before)

    def test_wrapped_lyrics_fit_inside_small_window_at_large_display_scaling(self):
        scaling=float(self.root.tk.call('tk','scaling'))
        try:
            self.root.tk.call('tk','scaling',2.0)
            line=Line('君と一緒に歩いて行こう 新しい世界へ','みじかいかし',
                      '너와 함께 걸어가자 새로운 세상으로')
            self.app.display_lines=(line,self.song.lines[1])
            self.app.last_render=None
            self.app.render()
            self.size(300,140)
            self.assertFalse(self.app.reading_label.winfo_ismapped())
            for label in (self.app.original_label,self.app.korean_label):
                box=label.bbox('lyric')
                self.assertGreaterEqual(box[0],0)
                self.assertLessEqual(box[2],label.winfo_width())
                self.assertGreaterEqual(label.winfo_rooty(),self.root.winfo_rooty())
                self.assertLessEqual(label.winfo_rooty()+box[3],self.app.previous_track_button.winfo_rooty())
        finally:
            self.root.tk.call('tk','scaling',scaling)

    def test_manual_geometry_survives_render_notifications_and_line_changes(self):
        self.size(900, 400)
        self.drag('se', -560, -250)
        before = self.rect()
        for index in (1, 0, 1):
            self.app.manual_index = index
            self.app.render()
            self.app.notify('상태가 바뀌어도 창 크기는 유지합니다.')
            self.root.update()
            self.root.update()
            self.assertEqual(self.rect(), before)

    def test_hidden_reading_preference_remains_hidden_after_expanding(self):
        self.app.show_reading.set(False)
        self.app.render()
        self.size(300, 150)
        self.size(1040, 600)
        self.assertFalse(self.app.show_reading.get())
        self.assertFalse(self.app.reading_label.winfo_ismapped())
        self.assertEqual(self.app.reading_label.cget('text'), '')

    def test_custom_order_can_hide_reading_before_the_track_metadata(self):
        self.app.compact_layout['priorities'].update(reading=1,title=2,artists=2,singers=2)
        self.size(650,500)
        self.assertEqual(self.app.compact_hidden,{'reading'})
        self.assertTrue(self.app.track_title_label.winfo_ismapped())
        self.assertTrue(self.app.track_artist_label.winfo_ismapped())
        self.assertFalse(self.app.reading_label.winfo_ismapped())
        self.size(500,500)
        self.assertFalse(self.app.track_metadata.winfo_ismapped())
        self.assertEqual(self.app.lyric_scale,1)

    def test_never_hidden_artist_and_reading_survive_title_hiding(self):
        self.app.compact_layout['priorities']={key:0 for key in self.app.compact_layout['priorities']}
        self.app.compact_layout['priorities']['title']=1
        for width,height in ((650,400),(300,160),(240,100)):
            with self.subTest(size=(width,height)):
                self.size(width,height)
                self.assertEqual(self.app.compact_hidden,{'title'})
                self.assertFalse(self.app.track_title_label.winfo_ismapped())
                self.assertTrue(self.app.track_artist_label.winfo_ismapped())
                self.assertTrue(self.app.reading_label.winfo_ismapped())
                self.assertTrue(self.app.original_label.winfo_ismapped())
                self.assertTrue(self.app.korean_label.winfo_ismapped())

    def test_lyric_targets_can_hide_individually_and_restore_without_losing_text(self):
        self.app.compact_layout['priorities']={key:0 for key in self.app.compact_layout['priorities']}
        self.app.compact_layout['priorities'].update(original=1,korean=2)
        self.app.render()
        before=(self.app.original_label.cget('text'),self.app.korean_label.cget('text'))
        self.size(650,500)
        self.assertFalse(self.app.original_label.winfo_ismapped())
        self.assertTrue(self.app.korean_label.winfo_ismapped())
        self.assertTrue(self.app.reading_label.winfo_ismapped())
        self.size(500,500)
        self.assertFalse(self.app.korean_label.winfo_ismapped())
        self.assertTrue(self.app.track_metadata.winfo_ismapped())
        self.size(1040,600)
        self.assertTrue(self.app.original_label.winfo_ismapped())
        self.assertTrue(self.app.korean_label.winfo_ismapped())
        self.assertEqual((self.app.original_label.cget('text'),self.app.korean_label.cget('text')),before)

    def test_shrink_only_keeps_all_six_targets_inside_a_small_window(self):
        self.app.compact_layout['mode']='shrink_only'
        styles=deepcopy(self.app.text_styles)
        with patch.object(self.app,'current_line_singers',return_value=('黛冬優子',)):
            self.app.last_render=None;self.app.render()
            for width,height in ((650,300),(300,160),(240,100)):
                with self.subTest(size=(width,height)):
                    self.size(width,height)
                    self.assertFalse(self.app.compact_hidden)
                    for widget in (self.app.track_title_label,self.app.track_artist_label,self.app.current_singers,
                                   self.app.reading_label,self.app.original_label,self.app.korean_label):
                        self.assertTrue(widget.winfo_ismapped())
                        self.assertGreaterEqual(widget.winfo_rootx(),self.root.winfo_rootx())
                        self.assertGreaterEqual(widget.winfo_rooty(),self.root.winfo_rooty())
                        self.assertLessEqual(widget.winfo_rootx()+widget.winfo_width(),self.root.winfo_rootx()+width)
                        self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(),self.root.winfo_rooty()+height-22)
                    self.assertLess(self.app.lyric_scale,1)
            self.size(1040,600)
            self.assertEqual(self.app.lyric_scale,1)
            self.assertEqual(self.app.track_title_label.cget('font')[1],11)
            self.assertEqual(self.app.current_singers.winfo_width(),MARKER_WIDTH)
        self.assertEqual(self.app.text_styles,styles)

    def test_switching_modes_in_a_compact_window_restores_content_immediately(self):
        self.size(320,200)
        self.assertFalse(self.app.track_title_label.winfo_ismapped())
        self.assertFalse(self.app.reading_label.winfo_ismapped())
        self.app.compact_layout['mode']='shrink_only'
        self.app.apply_appearance();self.root.update();self.root.update()
        self.assertTrue(self.app.track_title_label.winfo_ismapped())
        self.assertTrue(self.app.track_artist_label.winfo_ismapped())
        self.assertTrue(self.app.reading_label.winfo_ismapped())
        self.assertEqual(self.rect()[2:],(320,200))
        self.app.compact_layout['mode']='auto_hide'
        self.app.apply_appearance();self.root.update();self.root.update()
        self.assertFalse(self.app.track_title_label.winfo_ismapped())
        self.assertFalse(self.app.reading_label.winfo_ismapped())

    def test_all_targets_kept_in_auto_mode_behave_like_shrink_only(self):
        self.app.compact_layout['priorities']={key:0 for key in self.app.compact_layout['priorities']}
        self.size(300,160)
        self.assertFalse(self.app.compact_hidden)
        self.assertLess(self.app.lyric_scale,1)
        self.assertTrue(self.app.track_title_label.winfo_ismapped())
        self.assertTrue(self.app.reading_label.winfo_ismapped())

    def test_custom_policy_and_visible_content_restore_in_a_fresh_app(self):
        self.app.compact_layout['mode']='shrink_only'
        self.app.compact_layout['priorities']['reading']=1
        self.size(300,180)
        expected=deepcopy(self.app.compact_layout)
        self.app.close()
        self.root=tk.Tk();self.root.withdraw()
        self.app=LyricsApp(self.root,self.store,start_services=False)
        self.app.set_playback(Playback(self.song.title,self.song.artist,self.song.album,duration=180))
        self.app.follow.set(False);self.app.manual_index=0;self.app.render()
        self.root.deiconify();self.root.update();self.root.update()
        self.assertEqual(self.app.compact_layout,expected)
        self.assertFalse(self.app.compact_hidden)
        self.assertTrue(self.app.track_title_label.winfo_ismapped())
        self.assertTrue(self.app.reading_label.winfo_ismapped())

    def test_small_manual_rectangle_restores_in_a_fresh_app(self):
        self.size(900, 400)
        self.drag('se', -560, -240)
        before = self.rect()
        self.app.close()
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = LyricsApp(self.root, self.store, start_services=False)
        self.root.deiconify()
        self.root.update()
        self.root.update()
        self.assertTrue(self.app.manual_window_size)
        self.assertEqual(self.rect()[2:], before[2:])
        self.assertEqual(self.rect()[:2], before[:2])

    def test_handles_are_clear_at_rest_and_reveal_on_hover_over_transparent_background(self):
        self.size(340, 160)
        self.assertTrue(self.app.transparent.get())
        self.assertEqual(self.app.backdrop.opacity, 0)
        clear=self.root.attributes('-transparentcolor')
        with patch.object(self.root,'winfo_pointerxy',return_value=(0,0)):
            self.app.update_resize_handles()
        for edge, handle in self.app.resize_handles.items():
            with self.subTest(edge=edge):
                self.assertTrue(handle.winfo_ismapped())
                self.assertEqual(handle.cget('bg'),clear)
                self.assertGreater(handle.winfo_width(), 0)
                self.assertGreater(handle.winfo_height(), 0)
                point=(handle.winfo_rootx()+handle.winfo_width()//2,
                       handle.winfo_rooty()+handle.winfo_height()//2)
                with patch.object(self.root,'winfo_pointerxy',return_value=point):
                    self.app.update_resize_handles()
                    self.assertNotEqual(handle.cget('bg'),clear)
                    self.assertEqual(sum(h.cget('bg')!=clear for h in self.app.resize_handles.values()),1)
                    self.app.locked=True
                    self.app.update_resize_handles()
                    self.assertTrue(all(h.cget('bg')==clear for h in self.app.resize_handles.values()))
                    self.app.locked=False
        with patch.object(self.root,'winfo_pointerxy',return_value=(0,0)):
            self.app.update_resize_handles()
        self.assertTrue(all(h.cget('bg')==clear for h in self.app.resize_handles.values()))


if __name__ == '__main__':
    unittest.main()
