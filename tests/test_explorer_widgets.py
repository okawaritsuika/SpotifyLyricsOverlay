"""Scrolling, navigation and clipping of the dictionary's native rich list."""
import tkinter as tk
from types import SimpleNamespace
import unittest

from explorer_widgets import CatalogList
from ui_theme import configure_theme


class CatalogListTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk();self.root.withdraw();configure_theme(self.root)
        self.window = tk.Toplevel(self.root);self.window.attributes('-alpha',0)
        self.window.geometry('480x330')
        self.list = CatalogList(self.window, ('title','artists','state'))
        self.list.pack(fill='both',expand=True)
        self.root.update()

    def tearDown(self):
        self.root.destroy()

    def populate(self, count):
        for index in range(count):
            self.list.insert('', 'end', iid=str(index), values=('곡 '+str(index),'가창자','미연결'),
                subtitle='유닛 · 앨범', preview=[{'name':'아이돌','voice':'성우',
                    'color':'#ff4411','text_color':'#ffbaa1'}])
        self.list.selection_set('0');self.root.update()

    def test_large_catalog_draws_visible_rows_and_keyboard_end_scrolls_to_last(self):
        self.populate(2500)
        self.assertLess(len(self.list.canvas.find_all()),150)
        self.list._key(SimpleNamespace(keysym='End'));self.root.update()
        self.assertEqual(self.list.selection(),('2499',))
        text = [self.list.canvas.itemcget(item,'text') for item in self.list.canvas.find_all()
                if self.list.canvas.type(item)=='text']
        self.assertIn('곡 2499',text)
        self.assertLessEqual(2500*self.list.row_height,
            self.list.canvas.canvasy(0)+self.list.canvas.winfo_height())
        self.list._key(SimpleNamespace(keysym='Prior'));self.root.update()
        self.assertLess(int(self.list.selection()[0]),2499)
        self.list._key(SimpleNamespace(keysym='Home'));self.root.update()
        self.assertEqual(self.list.selection(),('0',))
        self.assertEqual(self.list.yview()[0],0)

    def test_scrolled_click_selects_visible_target_and_enter_activates_once(self):
        self.populate(100)
        self.list._wheel(SimpleNamespace(delta=-120));self.root.update()
        expected = str(int(self.list.canvas.canvasy(10)//self.list.row_height))
        self.list._clicked(SimpleNamespace(x=10,y=10));self.root.update()
        self.assertEqual(self.list.selection(),(expected,))
        activated = []
        self.list.bind('<<ListActivate>>',lambda _:activated.append(self.list.selection()))
        self.list._activate(None);self.root.update()
        self.assertEqual(activated,[(expected,)])

    def test_long_title_and_cv_fit_two_distinct_previews_at_narrow_width(self):
        self.list.insert('', 'end', iid='long', values=('긴 곡명 '*70,'',''),
            subtitle='음반 이름 '*70, badge='Spotify', preview=[
                {'name':'아이돌 이름 '*12,'voice':'첫 번째 성우 이름 '*12,
                 'color':'#123456','text_color':'#aabbcc'},
                {'name':'다른 캐릭터 '*12,'voice':'두 번째 성우 이름 '*12,
                 'color':'#ed4123','text_color':'#f6bba1'}])
        self.list.selection_set('long');self.root.update()
        width = self.list.canvas.winfo_width()
        for item in self.list.canvas.find_all():
            if self.list.canvas.type(item)=='text':
                bounds = self.list.canvas.bbox(item)
                self.assertLessEqual(bounds[2],width)
        names = self.list.canvas.find_withtag('performer_name')
        voices = self.list.canvas.find_withtag('performer_voice')
        self.assertEqual(len(names),2);self.assertEqual(len(voices),2)
        self.assertEqual([self.list.canvas.itemcget(item,'fill') for item in names],['#aabbcc','#f6bba1'])
        self.assertTrue(all(self.list.canvas.itemcget(item,'text').startswith('CV. ') for item in voices))

    def test_cv_unconfirmed_label_is_only_for_a_confirmed_character(self):
        self.list.insert('','end',iid='roles',values=('크레딧','',''),preview=[
            {'name':'캐릭터','kind':'idol','voice':''},
            {'name':'일반 가수','kind':'unknown','voice':''}])
        self.root.update()
        names=[self.list.canvas.itemcget(item,'text') for item in self.list.canvas.find_withtag('performer_name')]
        voices=[self.list.canvas.itemcget(item,'text') for item in self.list.canvas.find_withtag('performer_voice')]
        self.assertEqual(names,['캐릭터','일반 가수'])
        self.assertEqual(voices,['CV. 미확인'])

    def test_replacing_scrolled_results_resets_scroll_and_removes_old_selection(self):
        self.populate(100)
        self.list._key(SimpleNamespace(keysym='End'));self.root.update()
        self.list.delete(*self.list.get_children())
        self.list.insert('', 'end', iid='new', values=('새 곡','',''),subtitle='새 결과')
        self.root.update()
        self.assertEqual(self.list.selection(),())
        self.assertEqual(self.list.get_children(),('new',))
        self.assertEqual(self.list.yview(),(0.0,1.0))

    def test_replacing_same_size_page_starts_at_top_and_preserved_selection_is_visible(self):
        self.populate(100)
        self.list._key(SimpleNamespace(keysym='End'));self.root.update()
        self.list.delete(*self.list.get_children())
        for index in range(100):self.list.insert('','end',iid=str(index),values=('다음 페이지','',''))
        self.root.update()
        self.assertEqual(self.list.yview()[0],0)
        self.list.delete(*self.list.get_children())
        for index in range(50):self.list.insert('','end',iid=str(index),values=('필터 결과','',''))
        self.list.selection_set('6');self.list.see('6');self.root.update()
        top = self.list.canvas.canvasy(0)
        self.assertGreaterEqual(6*self.list.row_height,top)
        self.assertLessEqual(7*self.list.row_height,top+self.list.canvas.winfo_height())

    def text_position(self, value):
        canvas = self.list.canvas
        item = next(item for item in canvas.find_all()
                    if canvas.type(item) == 'text' and canvas.itemcget(item, 'text') == value)
        left, top, right, bottom = canvas.bbox(item)
        return int((left + right)/2 - canvas.canvasx(0)), int((top + bottom)/2 - canvas.canvasy(0))

    def click(self, position, time):
        x, y = position
        self.list.canvas.event_generate('<ButtonPress-1>', x=x, y=y, time=time)
        self.list.canvas.event_generate('<ButtonRelease-1>', x=x, y=y, time=time+10)
        self.root.update()

    def test_clickable_title_and_performer_names_navigate_without_selecting_the_row(self):
        clicked, selected = [], []
        self.list.insert('', 'end', iid='row', values=('그룹 이름','',''),
            title_command=lambda:clicked.append('group'), preview=[
                {'name':'아이돌 이름','voice':'성우','command':lambda:clicked.append('idol')}])
        self.list.bind('<<TreeviewSelect>>', lambda _:selected.append(self.list.selection()))
        self.root.update()
        self.click(self.text_position('그룹 이름'), 1000)
        self.click(self.text_position('아이돌 이름'), 2000)
        self.assertEqual(clicked, ['group', 'idol'])
        self.assertEqual(self.list.selection(), ())
        self.assertEqual(selected, [])
        self.click((10, 40), 3000)
        self.assertEqual(self.list.selection(), ('row',))
        self.assertEqual(selected, [('row',)])
        self.assertEqual(clicked, ['group', 'idol'])

    def test_name_links_are_underlined_and_only_the_link_uses_a_hand_cursor(self):
        self.list.insert('', 'end', iid='row', values=('그룹 이름','',''),
            title_command=lambda:None, preview=[{'name':'아이돌','command':lambda:None}])
        self.root.update()
        for name in ('그룹 이름', '아이돌'):
            x, y = self.text_position(name)
            self.list.canvas.event_generate('<Motion>', x=x, y=y)
            self.root.update()
            self.assertEqual(self.list.canvas.cget('cursor'), 'hand2')
        for item in self.list.canvas.find_withtag('name_link'):
            face = self.list.canvas.itemcget(item, 'font')
            self.assertEqual(int(self.root.tk.call('font', 'actual', face, '-underline')), 1)
        self.list.canvas.event_generate('<Motion>', x=10, y=40)
        self.root.update()
        self.assertEqual(self.list.canvas.cget('cursor'), 'arrow')

    def test_scrolling_and_redrawing_keep_each_visible_name_callback_with_its_recording(self):
        clicked = []
        for index in range(100):
            self.list.insert('', 'end', iid=str(index), values=('곡 '+str(index),'',''),
                preview=[{'name':'아이돌 '+str(index), 'command':lambda index=index:clicked.append(index)}])
        self.root.update()
        self.list._key(SimpleNamespace(keysym='End'));self.root.update()
        self.list._schedule_draw();self.root.update()
        self.click(self.text_position('아이돌 99'), 1000)
        self.assertEqual(clicked, [99])
        self.list._key(SimpleNamespace(keysym='Home'));self.root.update()
        self.click(self.text_position('아이돌 0'), 2000)
        self.assertEqual(clicked, [99, 0])
        self.list.delete(*self.list.get_children())
        self.list.insert('', 'end', iid='new', values=('다른 그룹','',''),
            title_command=lambda:clicked.append('new'))
        self.root.update()
        self.click(self.text_position('다른 그룹'), 3000)
        self.assertEqual(clicked, [99, 0, 'new'])

    def test_double_click_on_name_never_propagates_to_playback_after_rows_are_replaced(self):
        clicked, played = [], []
        def navigate():
            clicked.append('idol')
            self.list.delete(*self.list.get_children())
            self.list.insert('', 'end', iid='replacement', values=('새 노래','',''))
        self.list.insert('', 'end', iid='original', values=('원래 노래','',''),
            preview=[{'name':'아이돌 이름','command':navigate}])
        self.list.bind('<Double-1>', lambda _:played.append(self.list.selection()))
        self.root.update()
        position = self.text_position('아이돌 이름')
        self.click(position, 1000)
        self.click(position, 1100)
        self.assertEqual(clicked, ['idol'])
        self.assertEqual(played, [])
        self.assertEqual(self.list.selection(), ())
        self.click((10, 40), 2000)
        self.click((10, 40), 2100)
        self.assertEqual(played, [('replacement',)])


if __name__ == '__main__':unittest.main()
