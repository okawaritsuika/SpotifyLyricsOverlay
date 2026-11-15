"""Colored credit artwork keeps its alpha, scope, and native text layout."""
import tkinter as tk
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont
from pathlib import Path
import os

from artist_header import ArtistHeader
from overlay_fonts import _credit_font, _credit_mask, _has_glyph


class ArtistHeaderTests(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk();self.root.withdraw()
        self.header=ArtistHeader(self.root,bg='#10151f',fg='#c5cedc',font=('Malgun Gothic',9),
                                 justify='left',max_lines=3)
        self.header.pack(fill='x')
        self.root.geometry('210x100+10+10');self.root.deiconify();self.root.update()

    def tearDown(self):
        self.root.destroy()

    def render(self,text,spans,width=210):
        self.root.geometry(f'{width}x100+10+10')
        self.header.configure(text=text,outline_spans=spans,wraplength=width-22)
        self.root.update();self.root.update()

    def test_only_character_names_receive_their_own_color(self):
        text='하루카 (CV. 성우), 치하야 (CV. 성우)'
        second=text.index('치하야')
        self.render(text,[(0,3,'#e22b30'),(second,second+3,'#2743d2')],420)
        colored={run[2]:run[3] for run in self.header._artist_runs if run[3]}
        self.assertEqual(colored,{'하루카':'#e22b30','치하야':'#2743d2'})
        self.assertEqual(''.join(run[2] for run in self.header._artist_runs),text)
        self.assertEqual(self.header._artist_image.mode,'RGBA')
        self.assertFalse(self.header.find_withtag('lyric'))
        self.assertFalse(self.header.find_withtag('artist-color'))
        self.assertEqual(self.header.cget('text'),text)

    def test_contrast_japanese_credits_use_real_glyphs_instead_of_missing_boxes(self):
        directory=Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'
        for bold in (False,True):
            primary=_credit_font(32,bold)
            japanese=ImageFont.truetype(str(directory/('meiryob.ttc' if bold else 'meiryo.ttc')),32)
            for char in '学々恵鵜広':
                with self.subTest(bold=bold,char=char):
                    self.assertFalse(_has_glyph(primary,char))
                    self.assertTrue(_has_glyph(japanese,char))
                    actual=_credit_mask(char,32,bold,40,48)
                    expected=Image.new('L',(round(japanese.getlength(char))+8,48))
                    ImageDraw.Draw(expected).text((4,40),char,font=japanese,fill=255,anchor='ls')
                    self.assertEqual(actual.tobytes(),expected.tobytes())

    def test_korean_credits_retain_existing_glyphs_without_loading_fallback_fonts(self):
        text='시노사와 히로 (CV. 카와무라 레이나)'
        primary=_credit_font(32,True)
        expected=Image.new('L',(round(primary.getlength(text))+8,48))
        ImageDraw.Draw(expected).text((4,40),text,font=primary,fill=255,anchor='ls')
        with patch('overlay_fonts._credit_fonts') as fallback:
            actual=_credit_mask(text,32,True,40,48)
        fallback.assert_not_called()
        self.assertEqual(actual.tobytes(),expected.tobytes())

    def test_japanese_font_uses_korean_fallback_for_hangul_readings(self):
        kana,hangul='かな ','한글'
        for bold in (False,True):
            with self.subTest(bold=bold):
                japanese=_credit_font(32,bold,'Yu Gothic UI')
                korean=_credit_font(32,bold,'Malgun Gothic')
                kana_width=japanese.getlength(kana)
                expected=Image.new('L',(round(kana_width+korean.getlength(hangul))+8,48))
                draw=ImageDraw.Draw(expected)
                draw.text((4,40),kana,font=japanese,fill=255,anchor='ls')
                draw.text((4+kana_width,40),hangul,font=korean,fill=255,anchor='ls')
                actual=_credit_mask(kana+hangul,32,bold,40,48,'Yu Gothic UI')
                self.assertEqual(actual.tobytes(),expected.tobytes())

    def test_character_voice_pair_has_no_black_matte_but_unknown_credit_keeps_outline(self):
        pair='하루카 (CV. 성우)'
        self.render(pair,[(0,len(pair),'#e22b30')],420)
        pixels=[p for p in self.header._artist_image.get_flattened_data() if p[3]>=16]
        self.assertTrue(pixels)
        self.assertFalse(any(max(p[:3])<40 for p in pixels))
        self.assertTrue(any(16<=p[3]<200 for p in pixels),'Edges must retain partial alpha')
        text='그룹명, '+pair
        self.render(text,[(text.index(pair),len(text),'#e22b30')],420)
        self.assertEqual([run[2] for run in self.header._artist_runs if run[3]],[pair])
        unknown=self.header._artist_runs[0]
        self.assertIsNone(unknown[3])
        region=self.header._artist_image.crop((0,0,int(unknown[0]+unknown[4])-3,
                                               self.header._artist_image.height))
        self.assertTrue(any(max(p[:3])<40 and p[3]>100 for p in region.get_flattened_data()))

    def test_dark_blue_and_yellow_keep_distinct_bright_character_fills(self):
        means=[]
        for name,color in (('치하야','#2743d2'),('미라이','#ffff00')):
            self.render(name,[(0,len(name),color)])
            opaque=[p for p in self.header._artist_image.get_flattened_data() if p[3]>=250]
            self.assertTrue(opaque)
            means.append(tuple(sum(p[channel] for p in opaque)/len(opaque) for channel in range(3)))
        blue,yellow=means
        self.assertGreater(min(blue),170)
        self.assertGreater(blue[2],blue[0]+20)
        self.assertGreater(min(yellow),170)
        self.assertGreater(yellow[0],yellow[2]+20)
        self.assertGreater(yellow[1],yellow[2]+20)

    def test_group_has_uniform_readable_letters_over_a_smooth_background_gradient(self):
        text='가나다라마바사아자차카타파하'
        self.render(text,[(0,len(text),('#ff0000','#00ff00','#0000ff'))],420)
        self.assertEqual(len(self.header._artist_runs),1,'A palette must not split glyph colors')
        artwork=self.header._artist_image
        opaque=[p for p in artwork.get_flattened_data() if p[3]>=250]
        self.assertTrue(opaque)
        self.assertTrue(all(min(p[:3])>220 and max(p[:3])-min(p[:3])<20 for p in opaque))
        x,_,_,_,run_width,_,_=self.header._artist_runs[0]
        # Above the glyphs this row contains only the soft backdrop.
        wash=[artwork.getpixel((i,0)) for i in range(round(x)+4,round(x+run_width)-4)]
        self.assertTrue(all(0<p[3]<130 for p in wash))
        self.assertGreater(len({p[:3] for p in wash}),20)
        self.assertGreater(wash[0][0],wash[0][2]+25)
        self.assertGreater(wash[len(wash)//2][1],wash[len(wash)//2][0]+25)
        self.assertGreater(wash[-1][2],wash[-1][0]+25)
        # Compare displayed light, since straight RGB at almost-zero alpha is
        # quantized and can vary even when the visible backdrop stays smooth.
        self.assertLess(max(abs(a[channel]*a[3]/255-b[channel]*b[3]/255)
                            for a,b in zip(wash,wash[1:]) for channel in range(3)),5)

    def test_wrapped_gradient_preserves_span_progress_and_reuses_artwork(self):
        text='가나다라마바사아자차카타파하'
        palette=('#ff0000','#0000ff')
        self.render(text,[(0,len(text),palette)],140)
        self.assertGreater(len(self.header._layout_lines),1)
        runs=self.header._artist_runs
        self.assertEqual(''.join(run[2] for run in runs),text)
        self.assertTrue(all(run[3]==palette for run in runs))
        self.assertEqual(runs[0][5],0)
        self.assertEqual(runs[-1][6],1)
        for before,after in zip(runs,runs[1:]):self.assertAlmostEqual(before[6],after[5])
        artwork=self.header._artist_image
        self.header._draw()
        self.assertIs(self.header._artist_image,artwork)
        self.assertIs(self.header._artist_runs,runs)
        self.render(text,[(0,len(text),'#2743d2')],140)
        self.assertIsNot(self.header._artist_image,artwork)
        self.assertTrue(all(run[3]=='#2743d2' for run in self.header._artist_runs))

    def test_styled_wrap_matches_native_canvas_for_words_cjk_and_newlines(self):
        text='Alpha beta gamma delta\n가나다라마바사아자차카타파하, 한국어 이름\nLast line'
        self.render(text,[(0,len(text),'#ff8800')],140)
        expected=self.header.create_text(4,4,text=text,font=self.header.cget('font'),
                                         width=118,anchor='nw',justify='left')
        line_height=int(self.root.tk.call('font','metrics',self.header.cget('font'),'-linespace'))
        box=self.header.bbox(expected);count=(box[3]-box[1])//line_height
        starts=[self.header.index(expected,f'@-10000,{4+i*line_height+line_height//2}') for i in range(count)]
        starts.append(len(text))
        expected_lines=[text[start:end].rstrip() for start,end in zip(starts,starts[1:])]
        self.assertEqual([run[2] for run in self.header._artist_runs],expected_lines)
        self.header.delete(expected)

    def test_astral_characters_do_not_shift_name_spans_after_wrapping(self):
        text='😀 Alpha beta gamma 😀 가나다라마 치하야'
        start=text.index('치하야')
        self.render(text,[(start,start+3,'#2743d2')],140)
        colored=''.join(run[2] for run in self.header._artist_runs if run[3])
        self.assertEqual(colored,'치하야')
        visible=''.join(run[2] for run in self.header._artist_runs)
        self.assertEqual(visible.replace(' ',''),text.replace(' ',''))

    def test_three_lines_fit_and_long_credits_remain_scrollable(self):
        self.render('하루카\n치하야\n미키',[(0,3,'#e22b30'),(4,7,'#2743d2'),(8,10,'#b4e04b')])
        self.assertFalse(self.header.overflow)
        text='\n'.join('하루카 '+str(index) for index in range(40))
        self.render(text,[(0,len(text),'#e22b30')])
        self.assertTrue(self.header.overflow)
        self.assertTrue(self.header.scrollbar.winfo_ismapped())
        self.assertLess(self.header.winfo_height(),70)
        layer=self.header._artist_layer
        with patch.object(layer,'show',wraps=layer.show) as show:
            self.header.yview_moveto(1);self.root.update()
            self.assertGreaterEqual(self.header.yview()[1],.99)
            viewport=show.call_args.args[0]
            self.assertEqual(viewport.size,(self.header.winfo_width()-12,self.header.winfo_height()))
            expected=self.header._artist_image.crop((0,int(self.header.canvasy(0)),viewport.width,
                                                     int(self.header.canvasy(0))+viewport.height))
            self.assertEqual(viewport.tobytes(),expected.tobytes())
        self.header.event_generate('<MouseWheel>',delta=120)
        self.assertLess(self.header.yview()[1],1)
        self.assertEqual(self.header.cget('text'),text)

    def test_repeated_render_reuses_artwork_and_resize_reflows(self):
        text='하루카 (CV. 성우), 치하야 (CV. 성우), 긴 이름의 다른 참여자'
        self.render(text,[(0,3,'#e22b30')],210)
        artwork=self.header._artist_image;lines=self.header._layout_lines
        self.header._draw();self.header._draw()
        self.assertIs(self.header._artist_image,artwork)
        self.assertIs(self.header._layout_lines,lines)
        self.root.geometry('140x100+10+10');self.root.update();self.root.update()
        self.assertGreater(len(self.header._layout_lines),len(lines))
        self.assertIsNot(self.header._artist_image,artwork)
        self.assertTrue(all(run[0]+run[4]<=self.header.winfo_width()-10
                            for run in self.header._artist_runs))

    def test_native_artwork_follows_owner_visibility_position_and_opacity(self):
        self.render('하루카',[(0,3,'#e22b30')])
        layer=self.header._artist_layer
        with patch.object(layer,'hide',wraps=layer.hide) as hide:
            self.root.withdraw();self.root.update()
            self.assertTrue(hide.called)
        with patch.object(layer,'show',wraps=layer.show) as show:
            self.root.attributes('-alpha',.65)
            self.root.geometry('210x100+40+40');self.root.deiconify();self.root.update()
            self.assertTrue(show.called)
            self.assertEqual(show.call_args.args[1:3],
                             (self.header.winfo_rootx(),self.header.winfo_rooty()))
            self.assertAlmostEqual(show.call_args.kwargs['opacity'],.65,places=2)

    def test_track_change_resets_scroll_and_clears_old_colors(self):
        text='\n'.join('하루카' for _ in range(12))
        self.render(text,[(0,len(text),'#e22b30')])
        self.header.yview_moveto(1)
        self.render('Unknown artist',[])
        self.assertFalse(self.header.overflow)
        self.assertEqual(self.header.yview()[0],0)
        self.assertIsNone(self.header._artist_image)
        self.assertEqual(self.header._artist_runs,())
        self.assertFalse(self.header.find_withtag('artist-color'))
        self.assertEqual(len(self.header.find_withtag('lyric')),9)
        self.render('',[])
        self.assertIsNone(self.header.bbox('lyric'))

    def test_invalid_spans_do_not_color_unrelated_text(self):
        self.render('Unknown artist',[(-1,4,'#ff0000'),(0,100,'invalid'),(0,4,'#010203')])
        self.assertEqual(self.header.cget('outline_spans'),())
        self.assertIsNone(self.header._artist_image)
        self.assertEqual(len(self.header.find_withtag('lyric')),9)


if __name__=='__main__':
    unittest.main()

