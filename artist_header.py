"""A complete performer credit that cannot push the lyrics off the screen."""
from bisect import bisect_left
from functools import lru_cache
import os
from pathlib import Path
import re
import tkinter as tk

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from artist_layer import ArtistLayer
from subtitle import SubtitleLine


def _outline_spans(value):
    if not isinstance(value,(tuple,list)):return ()
    spans=[]
    for span in value:
        if not isinstance(span,(tuple,list)) or len(span)!=3:continue
        start,end,color=span
        palette=(color,) if isinstance(color,str) else color
        if (isinstance(start,int) and isinstance(end,int) and 0<=start<end
                and isinstance(palette,(tuple,list)) and palette
                and all(isinstance(value,str) and re.fullmatch(r'#[0-9a-fA-F]{6}',value)
                        and value.lower()!='#010203' for value in palette)):
            normalized=tuple(value.lower() for value in palette)
            spans.append((start,end,normalized[0] if isinstance(color,str) else normalized))
    return tuple(spans)


def _blend_color(color,base,amount):
    return '#'+''.join(f'{round(int(base[i:i+2],16)*(1-amount)+int(color[i:i+2],16)*amount):02x}'
                      for i in (1,3,5))


def _gradient_color(palette,position):
    location=max(0,min(1,position))*(len(palette)-1)
    first=min(int(location),len(palette)-1)
    return _blend_color(palette[min(first+1,len(palette)-1)],palette[first],location-first)


@lru_cache(maxsize=16)
def _credit_font(size,bold):
    path=Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'/('malgunbd.ttf' if bold else 'malgun.ttf')
    return ImageFont.truetype(str(path),size)


@lru_cache(maxsize=16)
def _credit_fonts(size,bold):
    """Pillow does not apply Windows' font linking to missing Japanese glyphs."""
    fonts=[_credit_font(size,bold)]
    directory=Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'
    for filename in (('meiryob.ttc' if bold else 'meiryo.ttc'),
                     ('YuGothB.ttc' if bold else 'YuGothM.ttc'),
                     'msgothic.ttc','seguisym.ttf','seguiemj.ttf'):
        try:fonts.append(ImageFont.truetype(str(directory/filename),size))
        except OSError:continue
    return tuple(fonts)


@lru_cache(maxsize=128)
def _missing_glyph(font):
    mask=font.getmask(chr(0x10ffff))
    return mask.size,bytes(mask)


@lru_cache(maxsize=8192)
def _has_glyph(font,char):
    mask=font.getmask(char)
    return (mask.size,bytes(mask))!=_missing_glyph(font)


def _credit_mask(value,size,bold,ascent,line_height):
    """Render one measured text run, substituting fonts only for absent glyphs."""
    primary=_credit_font(size,bold)
    fonts=None
    runs=[]
    for char in value:
        face=primary
        if not _has_glyph(primary,char):
            if fonts is None:fonts=_credit_fonts(size,bold)
            face=next((font for font in fonts if _has_glyph(font,char)),primary)
        if runs and runs[-1][1] is face:runs[-1]=(runs[-1][0]+char,face)
        else:runs.append((char,face))
    lengths=[face.getlength(text) for text,face in runs]
    mask=Image.new('L',(max(1,round(sum(lengths)))+8,line_height))
    draw=ImageDraw.Draw(mask);x=4
    for (text,face),width in zip(runs,lengths):
        draw.text((x,ascent),text,font=face,fill=255,anchor='ls')
        x+=width
    return mask


def _rgb(color):
    return tuple(int(color[i:i+2],16) for i in (1,3,5))


class ArtistHeader(SubtitleLine):
    def __init__(self, parent, *, max_lines=3, **options):
        self.max_lines=max_lines
        self.overflow=False
        self._previous_text=None
        self.scrollbar=None
        self._layout_probe=None
        self._layout_key=None
        self._layout_lines=()
        self._span_render_key=None
        self._artist_image=None
        self._artist_runs=()
        self._artist_layer=None
        self._layer_pending=None
        self._scheduler=parent._root()
        spans=_outline_spans(options.pop('outline_spans',()))
        super().__init__(parent,**options)
        self.text_options['outline_spans']=spans
        self.scrollbar=tk.Scrollbar(self,orient='vertical',command=self.yview,width=10,
                                   borderwidth=0,highlightthickness=0)
        self.configure(yscrollcommand=self._scrolled)
        self.bind('<MouseWheel>',self._wheel,add='+')
        self.bind('<Configure>',self._schedule_layer,add='+')
        self.bind('<Map>',self._schedule_layer,add='+')
        self.bind('<Unmap>',self._hide_layer,add='+')
        self.bind('<Destroy>',self._destroy_layer,add='+')
        top=self.winfo_toplevel()
        self._top_bindings=[(event,top.bind(event,self._owner_event,add='+'))
                            for event in ('<Configure>','<Map>','<Unmap>')]

    def configure(self,cnf=None,**options):
        if cnf:options={**cnf,**options}
        if 'outline_spans' in options:options['outline_spans']=_outline_spans(options['outline_spans'])
        result=super().configure(**options)
        self._schedule_layer()
        return result

    config=configure

    def _draw(self):
        wrap=self.text_options['wraplength']
        # Reserve a small gutter so an overflow scrollbar never covers a name.
        if self.winfo_width()>=80:
            self.text_options['wraplength']=min(wrap,max(60,self.winfo_width()-22))
        total=None
        try:
            if self.text_options.get('outline_spans') and self.text_options['text']:
                total=self._draw_spans()
            else:
                if self._span_render_key is not None:self.delete('lyric')
                self._span_render_key=None
                self._artist_image=None;self._artist_runs=()
                self._hide_layer()
                super()._draw()
        finally:self.text_options['wraplength']=wrap
        line_height=int(self.tk.call('font','metrics',self.text_options['font'],'-linespace'))
        box=self.bbox('lyric') if self.text_options['text'] else None
        total=max(total or 1,box[3]+3) if box else total or 1
        limit=line_height*self.max_lines+(6 if self._artist_image is not None else 8)
        self.overflow=total>limit
        tk.Canvas.configure(self,height=min(total,limit),
                            scrollregion=(0,0,max(1,self.winfo_width()),total),
                            yscrollincrement=line_height)
        if self.scrollbar is not None:
            if self.overflow:self.scrollbar.place(relx=1,rely=0,anchor='ne',relheight=1,width=10)
            else:self.scrollbar.place_forget()
        text=self.text_options['text']
        if text!=self._previous_text or not self.overflow:self.yview_moveto(0)
        self._previous_text=text
        self._schedule_layer()

    def _wrapped_lines(self,text,font,width,line_height):
        key=(text,font,width,line_height)
        if key==self._layout_key:return self._layout_lines
        if self._layout_probe is None:
            self._layout_probe=self.create_text(4,4,anchor='nw',state='hidden')
        probe=self._layout_probe
        # Let Tk choose exactly the same word/CJK breaks as the neutral header.
        # This item is hidden again before the event loop can paint it.
        self.itemconfigure(probe,text=text,font=font,width=width,justify='left',state='normal')
        try:
            box=self.bbox(probe)
            count=max(1,(box[3]-box[1])//line_height)
            starts=[self.index(probe,f'@-10000,{4+line*line_height+line_height//2}') for line in range(count)]
        finally:self.itemconfigure(probe,state='hidden')
        # Tcl 8.6 indices count an astral character as two code units.
        astral_size=int(self.tk.call('string','length',chr(0x1f600)))
        offsets=[0]
        for char in text:offsets.append(offsets[-1]+(astral_size if ord(char)>0xffff else 1))
        starts=[bisect_left(offsets,index) for index in starts]+[len(text)]
        lines=tuple((start,start+len(text[start:end].rstrip())) for start,end in zip(starts,starts[1:]))
        self._layout_key=key;self._layout_lines=lines
        return lines

    def _draw_spans(self):
        text=self.text_options['text'];font=self.text_options['font']
        width=max(80,min(self.text_options['wraplength'],self.winfo_width()-8))
        if self.winfo_width()<80:width=self.text_options['wraplength']
        line_height=int(self.tk.call('font','metrics',font,'-linespace'))
        lines=self._wrapped_lines(text,font,width,line_height)
        total=3+len(lines)*line_height+3
        key=(text,font,width,self.winfo_width(),self.text_options['fg'],
             self.text_options['justify'],self.text_options['outline_spans'])
        if key==self._span_render_key:return total
        measure=lambda value:int(self.tk.call('font','measure',font,value))
        colors=[None]*len(text)
        for start,end,color in self.text_options['outline_spans']:
            end=min(end,len(text))
            if start>=end:continue
            colors[start:end]=[(start,end,color)]*(end-start)
        runs=[]
        for line,(start,end) in enumerate(lines):
            line_width=measure(text[start:end]);justify=self.text_options['justify']
            x=4
            if justify=='center':x=(max(width+8,self.winfo_width())-line_width)/2
            elif justify=='right':x=max(width+8,self.winfo_width())-4-line_width
            current=start
            while current<end:
                stop=current+1
                while stop<end and colors[stop]==colors[current]:stop+=1
                style=colors[current]
                # A whole credit has one foreground. Only its background is a
                # continuous gradient; keep that gradient's position on wrapping.
                begin=finish=0
                if style:
                    span_start,span_end,color=style
                    span_width=max(1,measure(text[span_start:span_end]))
                    begin=measure(text[span_start:current])/span_width
                    finish=measure(text[span_start:stop])/span_width
                runs.append((x+measure(text[start:current]),3+line*line_height,
                             text[current:stop],style[2] if style else None,
                             measure(text[current:stop]),begin,finish))
                current=stop
        self.delete('lyric')
        self._artist_runs=tuple(runs)
        self._artist_image=self._paint(runs,line_height,total,max(width+8,self.winfo_width()))
        self._span_render_key=key
        return total

    def _paint(self,runs,line_height,height,width):
        scale=2
        image=Image.new('RGBA',(int(width)*scale,height*scale))
        font=self.text_options['font']
        points=int(self.tk.call('font','actual',font,'-size'))
        pixels=abs(points) if points<0 else points*float(self.tk.call('tk','scaling'))
        font_size=round(pixels*scale)
        bold=self.tk.call('font','actual',font,'-weight')=='bold'
        ascent=int(self.tk.call('font','metrics',font,'-ascent'))*scale
        for x,y,value,color,run_width,begin,finish in runs:
            if not color or not value.strip():continue
            palette=(color,) if isinstance(color,str) else color
            w=max(1,round(run_width*scale));h=line_height*scale
            pad=12*scale
            mask=Image.new('L',(w+pad*2,h+pad*2))
            ImageDraw.Draw(mask).rounded_rectangle((pad-2*scale,pad,pad+w+2*scale,pad+h),
                                                   radius=h//2,fill=210)
            mask=mask.filter(ImageFilter.GaussianBlur(3*scale))
            strip=Image.new('RGBA',(w+pad*2,1))
            # A smoky colored wash keeps bright letters readable over a light
            # desktop, even when the user's main backdrop is mostly transparent.
            # The mask follows the credit area, never the glyph edges.
            strip.putdata([_rgb(_blend_color(_gradient_color(palette,
                begin+(finish-begin)*max(0,min(1,(i-pad)/max(1,w-1)))),'#303849',.24))+(255,)
                for i in range(w+pad*2)])
            wash=strip.resize(mask.size);wash.putalpha(mask)
            image.alpha_composite(wash,(round(x*scale)-pad,round(y*scale)-pad))
        for x,y,value,color,run_width,begin,finish in runs:
            if not value:continue
            # Rasterize at twice the display resolution, retain alpha on every
            # edge, and match Tk's measured width so layout stays unchanged.
            mask=_credit_mask(value,font_size,bold,ascent,line_height*scale)
            mask=mask.resize((max(1,round(run_width*scale)+4*scale),line_height*scale),Image.Resampling.LANCZOS)
            pos=(round(x*scale)-2*scale,round(y*scale))
            if color is None and any(char.isalnum() for char in value):
                outline=mask.filter(ImageFilter.MaxFilter(4*scale+1))
                shadow=Image.new('RGBA',mask.size,'#090b10');shadow.putalpha(outline)
                image.alpha_composite(shadow,pos)
            fill=_blend_color(color,'#f4f6fb',.17) if isinstance(color,str) else (
                '#f4f6fb' if color else self.text_options['fg'])
            ink=Image.new('RGBA',mask.size,fill);ink.putalpha(mask)
            image.alpha_composite(ink,pos)
        return image.resize((int(width),height),Image.Resampling.LANCZOS)

    def _scrolled(self,first,last):
        if self.scrollbar is not None:self.scrollbar.set(first,last)
        self._schedule_layer()

    def _owner_event(self,event):
        if event.widget!=self.winfo_toplevel():return
        if event.type==tk.EventType.Unmap:self._hide_layer()
        else:self._schedule_layer()

    def _schedule_layer(self,event=None):
        if self._artist_image is None and self._artist_layer is None:return
        if self._layer_pending is None:
            self._layer_pending=self._scheduler.after_idle(self._sync_layer)

    def _sync_layer(self):
        self._layer_pending=None
        if self._artist_image is None or not self.winfo_viewable():
            self._hide_layer();return
        if self._artist_layer is None:self._artist_layer=ArtistLayer(self)
        y=max(0,int(self.canvasy(0)))
        width=max(1,self.winfo_width()-(12 if self.overflow else 0))
        viewport=self._artist_image.crop((0,y,width,y+self.winfo_height()))
        self._artist_layer.show(viewport,self.winfo_rootx(),self.winfo_rooty(),
                                opacity=float(self.winfo_toplevel().attributes('-alpha')))

    def _hide_layer(self,event=None):
        if self._artist_layer is not None:self._artist_layer.hide()

    def _destroy_layer(self,event):
        if event.widget!=self:return
        if self._layer_pending is not None:self._scheduler.after_cancel(self._layer_pending)
        if self._artist_layer is not None:self._artist_layer.close()
        top=self.winfo_toplevel()
        for sequence,binding in self._top_bindings:top.unbind(sequence,binding)

    def _wheel(self,event):
        if self.overflow and event.delta:
            self.yview_scroll(-1 if event.delta>0 else 1,'units')
            return 'break'
