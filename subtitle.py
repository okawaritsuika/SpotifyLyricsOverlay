"""Outlined, wrapping text that remains legible on a color-keyed background."""
from bisect import bisect_left
import math
import tkinter as tk
from tkinter import font as tkfont

from PIL import Image, ImageFilter

from artist_layer import ArtistLayer
from overlay_fonts import _credit_mask


DEFAULT_OUTLINE = (2, 0, None)
OUTLINE_OFFSETS = ((-2,0),(2,0),(0,-2),(0,2),(-1,-1),(1,-1),(-1,1),(1,1),(0,0))


def scaled_outline(style, scale=1):
    outline = (style['outline_width'], style['outline_blur'], style['outline_color'])
    if outline == DEFAULT_OUTLINE:
        return outline
    return (round(outline[0] * scale, 2), round(outline[1] * scale, 2), outline[2])


def outline_padding(outline):
    width, blur, _ = outline
    return max(4, 2 + math.ceil(width + 3 * blur)) if width else 4


def text_style_options(style, scale=1):
    return dict(fg=style['color'], outline=scaled_outline(style, scale),
                font=(style['family'], max(1, round(style['size'] * scale)),
                      'bold' if style['bold'] else 'normal'))


def contrast_outline(red, green, blue):
    return '#f7f7fa' if red*299 + green*587 + blue*114 < 128000 else '#090b10'


def paint_outline(image, mask, position, outline, color, scale):
    width, blur, chosen = outline
    if not width:
        return
    radius = width * scale
    whole = int(radius)
    fraction = radius - whole
    padding = math.ceil((width + 3 * blur) * scale)
    alpha = Image.new('L', (mask.width + 2*padding, mask.height + 2*padding))
    alpha.paste(mask, (padding, padding))
    if whole:
        alpha = alpha.filter(ImageFilter.MaxFilter(2*whole + 1))
    if fraction:
        # Blend the boundary between the two nearest raster widths.
        alpha = Image.blend(alpha, alpha.filter(ImageFilter.MaxFilter(3)), fraction)
    if blur:
        alpha = alpha.filter(ImageFilter.GaussianBlur(blur * scale))
    edge = Image.new('RGBA', alpha.size, chosen or color)
    edge.putalpha(alpha)
    image.alpha_composite(edge, (position[0] - padding, position[1] - padding))


def shorten_line(text, width, measure, *, keep_end=False):
    text = ' '.join(text.split())
    if measure(text) <= width:
        return text
    if measure('…') >= width:
        return ''
    low, high = 0, len(text)
    while low < high:
        count = (low + high + 1) // 2
        value = '…' + text[-count:] if keep_end else text[:count] + '…'
        if measure(value) <= width:
            low = count
        else:
            high = count - 1
    if not low:
        return '…'
    return '…' + text[-low:] if keep_end else text[:low] + '…'


def side_preview_width(width, lines):
    if width < 500:
        return 0
    available = width - 44
    for line in lines:
        if line.winfo_manager() != 'grid' or not line.cget('text'):
            continue
        box = line.bbox('lyric')
        if box:
            available = min(available, width-44-(box[2]-box[0])-24)
    space = min(172,max(0,available//2))
    return space if space >= 42 else 0


def place_side_preview(line, frame, center, side, width):
    previous = side == 'previous'
    line.configure(wraplength=width-8)
    line.place(in_=frame, relx=0 if previous else 1, rely=0, x=0,
               y=center.winfo_y()+center.winfo_height()//2,
               anchor='w' if previous else 'e', width=width)
    line.tk.call('raise', str(line), str(center))


class SubtitleLine(tk.Canvas):
    def __init__(self, parent, *, bg, fg, font, **options):
        self.text_options = {'text': '', 'fg': fg, 'font': font, 'wraplength': 900,
                             'justify':options.pop('justify','center'), 'ellipsis':options.pop('ellipsis',None),
                             'outline':options.pop('outline',DEFAULT_OUTLINE)}
        self.last_width = 0
        self._layout_probe = None
        self._layout_key = None
        self._layout_lines = ()
        self._text_render_key = None
        self._artist_image = None
        self._artist_layer = None
        self._layer_pending = None
        self._scheduler = parent._root()
        self._top_bindings = []
        super().__init__(parent, bg=bg, height=1, width=1, borderwidth=0,
                         highlightthickness=0, **options)
        self.bind('<Configure>', self._resize)
        self.bind('<Configure>', self._schedule_layer, add='+')
        self.bind('<Map>', self._schedule_layer, add='+')
        self.bind('<Unmap>', self._hide_layer, add='+')
        self.bind('<Destroy>', self._destroy_layer, add='+')

    def configure(self, cnf=None, **options):
        if cnf:
            options = {**cnf, **options}
        changed = False
        for name in tuple(options):
            if name in self.text_options:
                value=options.pop(name)
                if self.text_options[name]!=value:
                    self.text_options[name]=value
                    changed=True
        result = super().configure(**options) if options else None
        if changed or ('bg' in options and self._text_render_key is not None):
            self._draw()
        self._schedule_layer()
        return result

    config = configure

    def cget(self, key):
        return self.text_options[key] if key in self.text_options else super().cget(key)

    def _resize(self, event):
        if event.width != self.last_width:
            self.last_width = event.width
            self._draw()

    def _draw(self):
        text = self.text_options['text']
        ellipsis = self.text_options['ellipsis']
        wrap = self.text_options['wraplength']
        outline = self.text_options['outline']
        padding = outline_padding(outline)
        font = self.text_options['font']
        if text and ellipsis:
            face = tkfont.Font(root=self, font=font)
            text = shorten_line(text, max(1,wrap+8-2*padding), face.measure, keep_end=ellipsis=='start')
        raster = bool(outline[0]) and outline != DEFAULT_OUTLINE
        if not text or not raster:
            self._text_render_key = None
            self._artist_image = None
            self._hide_layer()
        if not text:
            self.itemconfigure('lyric',state='hidden')
            super().configure(height=1)
            return
        width = max(80, min(wrap, self.winfo_width()-2*padding)) if wrap else 0
        if self.winfo_width() < 80:
            width = wrap
        center = (self.winfo_width() if self.winfo_width() > 1 else width+8) / 2
        justify=self.text_options['justify']
        anchor='n'
        if justify=='left':center=padding;anchor='nw'
        elif justify=='right':center=max(wrap+8,self.winfo_width())-padding;anchor='ne'
        if ellipsis:width=0
        y = padding if raster else 3
        common = dict(text=text, font=font, width=width, anchor=anchor, justify=justify, tags='lyric')
        color = self.text_options['fg']
        edge = self._contrast_outline(color)
        foreground = self.cget('bg') if raster else color
        items=self.find_withtag('lyric')
        for index,(dx,dy) in enumerate(OUTLINE_OFFSETS):
            fill = foreground if index==8 else edge
            state = 'normal' if index==8 or outline==DEFAULT_OUTLINE else 'hidden'
            if index<len(items):
                self.coords(items[index],center+dx,y+dy)
                self.itemconfigure(items[index],fill=fill,state=state,**common)
            else:self.create_text(center+dx,y+dy,fill=fill,state=state,**common)
        box = self.bbox('lyric')
        height = max(1, box[3]+(padding if raster else 3)) if box else 1
        super().configure(height=height)
        if raster:
            self._draw_text_image(text,font,width,center,y,height,justify,outline,color,edge)

    def _contrast_outline(self,color):
        try:return contrast_outline(*(value//257 for value in self.winfo_rgb(color)))
        except tk.TclError:return '#090b10'

    def _wrapped_lines(self,text,font,width,line_height):
        key=(text,font,width,line_height)
        if key==self._layout_key:return self._layout_lines
        if self._layout_probe is None:
            self._layout_probe=self.create_text(4,4,anchor='nw',state='hidden')
        probe=self._layout_probe
        # Ask Tk for the same word and CJK breaks that the canvas text uses.
        self.itemconfigure(probe,text=text,font=font,width=width,justify='left',state='normal')
        try:
            box=self.bbox(probe)
            count=max(1,(box[3]-box[1])//line_height)
            starts=[self.index(probe,f'@-10000,{4+line*line_height+line_height//2}') for line in range(count)]
        finally:self.itemconfigure(probe,state='hidden')
        astral_size=int(self.tk.call('string','length',chr(0x1f600)))
        offsets=[0]
        for char in text:offsets.append(offsets[-1]+(astral_size if ord(char)>0xffff else 1))
        starts=[bisect_left(offsets,index) for index in starts]+[len(text)]
        lines=tuple((start,start+len(text[start:end].rstrip())) for start,end in zip(starts,starts[1:]))
        self._layout_key=key;self._layout_lines=lines
        return lines

    def _raster_font(self,font,scale):
        points = font[1] if isinstance(font,tuple) else int(self.tk.call('font','actual',font,'-size'))
        pixels = abs(points) if points<0 else points*float(self.tk.call('tk','scaling'))
        return (max(1,round(pixels*scale)), self.tk.call('font','actual',font,'-weight')=='bold',
                self.tk.call('font','actual',font,'-family'),
                int(self.tk.call('font','metrics',font,'-ascent'))*scale)

    def _draw_text_image(self,text,font,width,center,y,height,justify,outline,color,edge):
        image_width = max(1,self.winfo_width())
        key = (text,font,width,center,y,height,image_width,justify,outline,color)
        if key==self._text_render_key:return
        line_height = int(self.tk.call('font','metrics',font,'-linespace'))
        lines = self._wrapped_lines(text,font,width,line_height)
        measure = lambda value: int(self.tk.call('font','measure',font,value))
        scale = 2
        pixels,bold,family,ascent = self._raster_font(font,scale)
        image = Image.new('RGBA',(image_width*scale,height*scale))
        runs = []
        for row,(start,stop) in enumerate(lines):
            if start==stop:continue
            value = text[start:stop]
            measured = measure(value)
            x = center - measured*{'left':0,'center':.5,'right':1}[justify]
            mask = _credit_mask(value,pixels,bold,ascent,line_height*scale,family)
            mask = mask.resize((max(1,measured*scale)+4*scale,mask.height),Image.Resampling.LANCZOS)
            pos = (round(x*scale)-2*scale,(y+row*line_height)*scale)
            paint_outline(image,mask,pos,outline,edge,scale)
            runs.append((mask,pos))
        for mask,pos in runs:
            ink = Image.new('RGBA',mask.size,color)
            ink.putalpha(mask)
            image.alpha_composite(ink,pos)
        self._artist_image = image.resize((image_width,height),Image.Resampling.LANCZOS)
        self._text_render_key = key
        self._schedule_layer()

    def _owner_event(self,event):
        if event.widget!=self.winfo_toplevel():return
        if event.type==tk.EventType.Unmap:self._hide_layer()
        else:self._schedule_layer()

    def _schedule_layer(self,event=None):
        if self._artist_image is None and self._artist_layer is None:return
        if not self._top_bindings:
            top=self.winfo_toplevel()
            self._top_bindings=[(event,top.bind(event,self._owner_event,add='+'))
                                for event in ('<Configure>','<Map>','<Unmap>')]
        if self._layer_pending is None:
            self._layer_pending=self._scheduler.after_idle(self._sync_layer)

    def _sync_layer(self):
        self._layer_pending=None
        if (self._artist_image is None or not self.winfo_viewable()
                or getattr(self.winfo_toplevel(),'electron_overlay_active',False)):
            self._hide_layer();return
        if self._artist_layer is None:self._artist_layer=ArtistLayer(self)
        y=max(0,int(self.canvasy(0)))
        width=max(1,self.winfo_width()-(12 if getattr(self,'overflow',False) else 0))
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
