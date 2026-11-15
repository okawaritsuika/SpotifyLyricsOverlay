"""A complete performer credit that cannot push the lyrics off the screen."""
import re
import tkinter as tk

from PIL import Image, ImageDraw, ImageFilter

from overlay_fonts import _credit_mask
from subtitle import DEFAULT_OUTLINE, SubtitleLine, outline_padding, paint_outline


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


def _rgb(color):
    return tuple(int(color[i:i+2],16) for i in (1,3,5))


class ArtistHeader(SubtitleLine):
    def __init__(self, parent, *, max_lines=3, **options):
        self.max_lines=max_lines
        self.overflow=False
        self._previous_text=None
        self.scrollbar=None
        self._span_render_key=None
        self._artist_runs=()
        spans=_outline_spans(options.pop('outline_spans',()))
        super().__init__(parent,**options)
        self.text_options['outline_spans']=spans
        self.scrollbar=tk.Scrollbar(self,orient='vertical',command=self.yview,width=10,
                                   borderwidth=0,highlightthickness=0)
        self.configure(yscrollcommand=self._scrolled)
        self.bind('<MouseWheel>',self._wheel,add='+')

    def configure(self,cnf=None,**options):
        if cnf:options={**cnf,**options}
        if 'outline_spans' in options:options['outline_spans']=_outline_spans(options['outline_spans'])
        return super().configure(**options)

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
                if self._span_render_key is not None:
                    self.delete('lyric')
                    self._span_render_key=None
                    self._artist_runs=()
                super()._draw()
        finally:self.text_options['wraplength']=wrap
        line_height=int(self.tk.call('font','metrics',self.text_options['font'],'-linespace'))
        box=self.bbox('lyric') if self.text_options['text'] else None
        padding=outline_padding(self.text_options['outline'])
        if box:total=max(total or 1,box[3]+(padding if self._text_render_key else 3))
        limit=line_height*self.max_lines+2*padding
        if self._span_render_key and self.text_options['outline']==DEFAULT_OUTLINE:limit-=2
        total=total or 1
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

    def _draw_spans(self):
        text=self.text_options['text'];font=self.text_options['font']
        outline=self.text_options['outline']; padding=outline_padding(outline)
        y_padding=3 if outline==DEFAULT_OUTLINE else padding
        width=max(80,min(self.text_options['wraplength'],self.winfo_width()-2*padding))
        if self.winfo_width()<80:width=self.text_options['wraplength']
        line_height=int(self.tk.call('font','metrics',font,'-linespace'))
        lines=self._wrapped_lines(text,font,width,line_height)
        total=2*y_padding+len(lines)*line_height
        key=(text,font,width,self.winfo_width(),self.text_options['fg'],
             self.text_options['justify'],self.text_options['outline_spans'],outline)
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
            x=padding
            if justify=='center':x=(max(width+2*padding,self.winfo_width())-line_width)/2
            elif justify=='right':x=max(width+2*padding,self.winfo_width())-padding-line_width
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
                runs.append((x+measure(text[start:current]),y_padding+line*line_height,
                             text[current:stop],style[2] if style else None,
                             measure(text[current:stop]),begin,finish))
                current=stop
        self.delete('lyric')
        self._text_render_key=None
        self._artist_runs=tuple(runs)
        self._artist_image=self._paint(runs,line_height,total,max(width+2*padding,self.winfo_width()))
        self._span_render_key=key
        return total

    def _paint(self,runs,line_height,height,width):
        scale=2
        image=Image.new('RGBA',(int(width)*scale,height*scale))
        font_size,bold,family,ascent=self._raster_font(self.text_options['font'],scale)
        outline=self.text_options['outline']
        ink_runs=[]
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
            mask=_credit_mask(value,font_size,bold,ascent,line_height*scale,family)
            mask=mask.resize((max(1,round(run_width*scale)+4*scale),line_height*scale),Image.Resampling.LANCZOS)
            pos=(round(x*scale)-2*scale,round(y*scale))
            fill=_blend_color(color,'#f4f6fb',.17) if isinstance(color,str) else (
                '#f4f6fb' if color else self.text_options['fg'])
            if outline==DEFAULT_OUTLINE:
                if color is None and any(char.isalnum() for char in value):
                    alpha=mask.filter(ImageFilter.MaxFilter(4*scale+1))
                    edge=Image.new('RGBA',mask.size,self._contrast_outline(fill))
                    edge.putalpha(alpha)
                    image.alpha_composite(edge,pos)
            else:
                paint_outline(image,mask,pos,outline,self._contrast_outline(fill),scale)
            ink_runs.append((mask,fill,pos))
        for mask,fill,pos in ink_runs:
            ink=Image.new('RGBA',mask.size,fill);ink.putalpha(mask)
            image.alpha_composite(ink,pos)
        return image.resize((int(width),height),Image.Resampling.LANCZOS)

    def _scrolled(self,first,last):
        if self.scrollbar is not None:self.scrollbar.set(first,last)
        self._schedule_layer()

    def _wheel(self,event):
        if self.overflow and event.delta:
            self.yview_scroll(-1 if event.delta>0 else 1,'units')
            return 'break'
