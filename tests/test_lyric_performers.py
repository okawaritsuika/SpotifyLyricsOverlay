"""Current-line singers retain explicit scope and clear when the line changes."""
import tkinter as tk
import unittest

from lyric_performers import LyricPerformers,PREFIX,MARKER_WIDTH,MARKER_HEIGHT,marker_image,performer_credit,short_performer_name,_chip_rows
from subtitle import SubtitleLine


ALBUM='THE IDOLM@STER SHINY COLORS Song for Prism'
SINGERS=('黛冬優子','園田智代子','鈴木羽那','七草にちか','大崎甜花')


class PerformerCreditTests(unittest.TestCase):
    def test_explicit_characters_show_their_own_voice_and_color(self):
        cases=(('黛冬優子','마유즈미 후유코','유키무라 에리','#5ce626'),
               ('園田智代子','소노다 치요코','시라이시 하루카','#f93b90'),
               ('鈴木羽那','스즈키 하나','미카와 하루나','#e0b5d3'),
               ('七草にちか','나나쿠사 니치카','시즈키 아즈사','#a5cfb6'),
               ('大崎甜花','오사키 텐카','마에카와 료코','#e75bec'))
        for name,character,voice,color in cases:
            with self.subTest(name=name):
                text,spans=performer_credit([name],ALBUM)
                self.assertEqual(text,PREFIX+character+' (CV. '+voice+')')
                self.assertEqual(spans,((len(PREFIX),len(text),color),))

    def test_multiple_singers_keep_distinct_colors_and_leave_prefix_neutral(self):
        names=[SINGERS[0],SINGERS[1],SINGERS[0]]
        original=list(names)
        text,spans=performer_credit(names,ALBUM)
        self.assertEqual(names,original)
        self.assertEqual(len(spans),2)
        self.assertEqual([color for _,_,color in spans],['#5ce626','#f93b90'])
        self.assertTrue(all(start>=len(PREFIX) for start,_,_ in spans))
        self.assertNotIn('스즈키 하나',text)

    def test_empty_or_invalid_credits_never_infer_the_previous_or_whole_group(self):
        for value in (None,[],(),{},[None,123,'  ']):
            with self.subTest(value=value):self.assertEqual(performer_credit(value,ALBUM),('',()))
        text,_=performer_credit(['イルミネーションスターズ'],ALBUM)
        self.assertNotIn('사쿠라기 마노',text)
        self.assertNotIn('카자노 히오리',text)

    def test_unknown_person_is_neutral_and_does_not_receive_a_guessed_voice(self):
        text,spans=performer_credit(['Explicit guest'],ALBUM)
        self.assertEqual(text,PREFIX+'Explicit guest')
        self.assertEqual(spans,())

    def test_short_names_are_character_given_names_and_do_not_expand_groups(self):
        self.assertEqual(tuple(short_performer_name(name,ALBUM) for name in SINGERS),
                         ('후유코','치요코','하나','니치카','텐카'))
        self.assertEqual(short_performer_name('Explicit guest',ALBUM),'Explicit guest')
        self.assertNotIn('사쿠라기 마노',short_performer_name('イルミネーションスターズ',ALBUM))

    def test_limited_height_reports_remaining_singers_with_each_color(self):
        names=('후유코','치요코','하나','니치카','텐카')
        colors=('#5ce626','#f93b90','#e0b5d3','#a5cfb6','#e75bec')
        self.assertEqual(_chip_rows(names,colors,55),
                         (('후유코',('#5ce626',)),('+4명',colors[1:])))
        self.assertEqual(len(_chip_rows(names,colors,142)),5)
        self.assertEqual(_chip_rows(names,colors,26),(('후유코',colors),))


class LyricPerformersTests(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk();self.root.withdraw()
        self.root.attributes('-alpha',0)
        self.root.geometry('620x150+20+20')
        self.root.columnconfigure(0,weight=1)
        self.lyrics=SubtitleLine(self.root,bg='#010203',fg='#f4f7fc',font=('Malgun Gothic',24,'bold'))
        self.lyrics.configure(text='가사 중앙과 높이를 그대로 유지합니다')
        self.lyrics.grid(row=0,column=0,sticky='ew',padx=74)
        self.header=LyricPerformers(self.root,bg='#010203',fg='#afbbcf')
        self.header.attach_to(self.lyrics)
        self.root.deiconify();self.root.update()

    def tearDown(self):self.root.destroy()

    def test_line_transition_and_empty_line_clear_stale_names_and_colors(self):
        self.assertTrue(self.header.set_singers([SINGERS[0]],ALBUM));self.root.update()
        self.assertIn('마유즈미 후유코',self.header.cget('text'))
        self.assertFalse(self.header.set_singers([SINGERS[0]],ALBUM))
        self.assertTrue(self.header.set_singers([SINGERS[1]],ALBUM));self.root.update()
        self.assertNotIn('마유즈미 후유코',self.header.cget('text'))
        self.assertEqual([color for _,_,color in self.header.cget('outline_spans')],['#f93b90'])
        self.assertTrue(self.header.set_singers([],ALBUM));self.root.update()
        self.assertEqual(self.header.cget('text'),'')
        self.assertEqual(self.header.cget('outline_spans'),())
        self.assertFalse(self.header.winfo_ismapped())
        self.assertIsNone(self.header._artist_image)
        self.assertFalse(self.header.set_singers([],ALBUM))
        self.assertTrue(self.header.set_singers([SINGERS[2]],ALBUM));self.root.update()
        self.assertTrue(self.header.winfo_ismapped())
        self.assertEqual(self.header.place_info()['in'],self.lyrics)

    def test_multiple_singers_have_named_chips_and_complete_hover_credits(self):
        geometry=(self.lyrics.winfo_rootx(),self.lyrics.winfo_rooty(),self.lyrics.winfo_width(),
                  self.lyrics.winfo_height(),self.root.winfo_height())
        self.header.set_singers(SINGERS,ALBUM);self.root.update();self.root.update()
        self.assertEqual(self.header._marker_colors,('#5ce626','#f93b90','#e0b5d3','#a5cfb6','#e75bec'))
        self.assertEqual(self.header._marker_names,('후유코','치요코','하나','니치카','텐카'))
        self.assertEqual(self.header._artist_image.width,MARKER_WIDTH)
        self.assertLessEqual(self.header._artist_image.height,self.lyrics.winfo_height())
        self.assertEqual(self.header.hint.text,self.header.cget('text'))
        self.assertIn('마에카와 료코',self.header.hint.text)
        self.assertFalse(self.header.grid_info())
        self.assertFalse(self.header.find_withtag('lyric'))
        self.assertLess(self.header.winfo_rootx()+self.header.winfo_width(),self.lyrics.winfo_rootx())
        self.assertEqual(geometry,(self.lyrics.winfo_rootx(),self.lyrics.winfo_rooty(),self.lyrics.winfo_width(),
                                   self.lyrics.winfo_height(),self.root.winfo_height()))
        self.assertEqual(self.header._artist_image.mode,'RGBA')
        self.assertTrue(all(start>=len(PREFIX) for start,_,_ in self.header.cget('outline_spans')))

    def test_marker_has_partial_alpha_edges_and_empty_image_is_transparent(self):
        image=marker_image(('#5ce626',),('후유코',))
        self.assertEqual(image.size,(MARKER_WIDTH,MARKER_HEIGHT))
        alpha=list(image.getchannel('A').get_flattened_data())
        self.assertEqual(min(alpha),1)
        self.assertTrue(any(0<value<255 for value in alpha))
        self.assertGreater(max(alpha),240)
        self.assertNotEqual(image.tobytes(),marker_image(('#5ce626',),('',)).tobytes())
        self.assertIsNone(marker_image(()).getbbox())

    def test_unknown_long_name_stays_inside_its_chip(self):
        image=marker_image(('#afbbcf',),('An exceptionally long guest performer',))
        self.assertEqual(image.size,(MARKER_WIDTH,MARKER_HEIGHT))
        self.assertEqual(image.getchannel('A').getbbox(),(0,0,MARKER_WIDTH,MARKER_HEIGHT))

    def test_partial_credit_keeps_known_color_and_limits_the_hover_claim(self):
        self.header.set_singers([SINGERS[0]],ALBUM);self.root.update()
        image=self.header._artist_image.tobytes()
        geometry=(self.header.winfo_x(),self.header.winfo_y(),self.header.winfo_height())
        self.assertTrue(self.header.set_singers([SINGERS[0]],ALBUM,partial=True))
        self.root.update()
        self.assertIn('확인된 가창 · 마유즈미 후유코',self.header.hint.text)
        self.assertIn('일부 파트의 가창자는 미확인',self.header.hint.text)
        self.assertNotIn('소노다 치요코',self.header.hint.text)
        self.assertEqual(self.header._artist_image.tobytes(),image)
        self.assertEqual(geometry,(self.header.winfo_x(),self.header.winfo_y(),self.header.winfo_height()))
        self.assertFalse(self.header.set_singers([SINGERS[0]],ALBUM,partial=True))
        self.header.set_singers([],ALBUM,partial=True)
        self.assertEqual(self.header.hint.text,'')
        self.assertFalse(self.header._partial)


if __name__=='__main__':unittest.main()
