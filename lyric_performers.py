"""Short singer names in colored lyric-gutter chips, with complete hover credits."""
import re
import tkinter as tk

from PIL import Image,ImageDraw,ImageColor

from artist_colors import character_color_spans,catalog as color_catalog
from artist_header import ArtistHeader
from artist_names import artist_label,name_key
from group_credits import with_character_voices
from overlay_fonts import _credit_font,_credit_mask
from subtitle import contrast_outline,paint_outline
from ui_theme import ControlHint


PREFIX = '현재 가창 · '
MARKER_WIDTH,MARKER_HEIGHT = 70,30
MARKER_GAP = 3
MAX_CHIP_ROWS = 4
MIN_CHIP_HEIGHT = 23
SECOND_COLUMN_OFFSET = MARKER_WIDTH+MARKER_GAP
LYRIC_GUTTER = MARKER_WIDTH-12+SECOND_COLUMN_OFFSET


def performer_credit(names, album='', update_path=None):
    """Use only supplied credits; a unit roster never identifies a line's singers."""
    if isinstance(names,str):names=(names,)
    if not isinstance(names,(list,tuple)):return '',()
    names=tuple(dict.fromkeys(name.strip() for name in names
                             if isinstance(name,str) and name.strip()))
    if not names:return '',()
    credits=with_character_voices(names,album,update_path=update_path)
    label=artist_label(credits,album)
    if not label:return '',()
    spans=tuple((start+len(PREFIX),end+len(PREFIX),color)
                for start,end,color in character_color_spans(label,album))
    return PREFIX+label,spans


def _marker_colors(names,album,update_path):
    if isinstance(names,str):names=(names,)
    if not isinstance(names,(list,tuple)):return ()
    colors=[]
    for name in dict.fromkeys(value.strip() for value in names if isinstance(value,str) and value.strip()):
        credit=with_character_voices((name,),album,update_path=update_path)
        spans=character_color_spans(artist_label(credit,album),album)
        # A roster gradient does not establish which group members sang a line.
        color=spans[0][2] if len(spans)==1 and isinstance(spans[0][2],str) else '#afbbcf'
        colors.append(color)
    return tuple(colors)


def short_performer_name(name,album=''):
    """Shorten only a known character's Korean display name, never a group roster."""
    label=re.sub(r'\s*[（(]\s*CV[.:：].*?[)）]\s*$','',artist_label((name,),album),flags=re.I).strip()
    colors=color_catalog();owner=colors.names.get(name_key(label))
    return label.rsplit(' ',1)[-1] if colors.kinds.get(owner)=='idol' else label


def _chip_rows(names,colors,height,capacity=None):
    if capacity is None:capacity=max(1,(height+MARKER_GAP)//(MARKER_HEIGHT+MARKER_GAP))
    if len(names)<=capacity:return tuple((name,(color,)) for name,color in zip(names,colors))
    if capacity==1:return ((names[0],colors),)
    count=capacity-1
    return tuple((name,(color,)) for name,color in zip(names[:count],colors[:count]))+(
        ('+'+str(len(names)-count)+'명',colors[count:]),)


def marker_image(colors,names=(),height=MARKER_HEIGHT,*,capacity=None,style=None,width=MARKER_WIDTH):
    """Readable white names and distinct source colors survive a light desktop."""
    scale=2
    image=Image.new('RGBA',(width*scale,height*scale))
    if not colors:return image.resize((width,height))
    image.paste((0,0,0,1),(0,0,image.width,image.height))
    draw=ImageDraw.Draw(image)
    rows=_chip_rows(names or tuple('' for _ in colors),colors,height,capacity)
    cell_height=min(MARKER_HEIGHT,(height-MARKER_GAP*(len(rows)-1))/len(rows))
    style=style or {}
    pixels=min(max(8,int(style.get('size',10))),max(8,int(cell_height)-6))*scale
    bold=style.get('bold',True)
    family=style.get('family','Malgun Gothic')
    font=_credit_font(pixels,bold,family)
    ascent,descent=font.getmetrics()
    try:red,green,blue=ImageColor.getrgb(style.get('color','#f8faff'))
    except ValueError:red,green,blue=248,250,255
    background=(246,247,250,245) if red*299+green*587+blue*114<128000 else (15,23,38,245)
    outline=(style.get('outline_width',0),style.get('outline_blur',0),style.get('outline_color'))
    for index,(label,palette) in enumerate(rows):
        top=round(index*(cell_height+MARKER_GAP)*scale)
        bottom=round((index*(cell_height+MARKER_GAP)+cell_height)*scale)-1
        draw.rounded_rectangle((1,top,width*scale-2,bottom),radius=6*scale,
                               fill=background,outline=palette[0],width=scale)
        draw.rounded_rectangle((4*scale,top+5*scale,7*scale,bottom-5*scale),radius=scale,fill=palette[0])
        if len(palette)>1:
            for position,color in enumerate(palette):
                left=round((10+position*(width-14)/len(palette))*scale)
                right=round((10+(position+1)*(width-14)/len(palette))*scale)-1
                draw.rectangle((left,bottom-3*scale,right,bottom-scale),fill=color)
        available=(width-22)*scale
        while label and font.getlength(label)>available:
            label=label[:-2]+'…' if label.endswith('…') else label[:-1]+'…'
        if label:
            mask=_credit_mask(label,pixels,bold,ascent,ascent+descent,family)
            position=(round((width*scale-mask.width)/2)+3*scale,round((top+bottom-mask.height)/2))
            ink=Image.new('RGBA',mask.size,(red,green,blue));ink.putalpha(mask)
            if outline[0]:
                chip=image.crop((0,top,width*scale,bottom+1))
                local=(position[0],position[1]-top)
                paint_outline(chip,mask,local,outline,contrast_outline(red,green,blue),scale)
                chip.alpha_composite(ink,local)
                image.paste(chip,(0,top))
            else:image.alpha_composite(ink,position)
        if len(palette)>1 and not label.startswith('+'):
            draw.text((width*scale-4*scale,top+scale),'+'+str(len(palette)-1),
                      font=_credit_font(8*scale,True),fill='#f8faff',anchor='rt')
    image=image.resize((width,height),Image.Resampling.LANCZOS)
    image.putalpha(image.getchannel('A').point(lambda value:max(1,value)))
    return image


class _PerformerColumn(ArtistHeader):
    def __init__(self,parent,**options):
        self._column_layout=None
        self._marker_style={}
        super().__init__(parent,**options)
        self.hint=ControlHint(self,'')

    def _draw(self):
        # The credits in text_options are for the tooltip. set_column draws the chips.
        pass

    def set_column(self,anchor,names,colors,rows,scale,offset=8,cell_height=MARKER_HEIGHT):
        layout=(anchor,names,colors,rows,scale,offset,cell_height,tuple(sorted(self._marker_style.items())))
        if layout==self._column_layout:return
        self._column_layout=layout
        if not names:
            self.hint.hide()
            self.place_forget()
            self._artist_image=None
            self._hide_layer()
            return
        height=min(rows,len(names))*(cell_height+MARKER_GAP)-MARKER_GAP
        size=(max(1,round(MARKER_WIDTH*scale)),max(1,round(height*scale)))
        image=marker_image(colors,names,height,capacity=rows,style=self._marker_style)
        self._artist_image=image.resize(size,Image.Resampling.LANCZOS) if size!=image.size else image
        self.place(in_=anchor,rely=.5,x=-max(1,round(offset*scale)),anchor='e',
                   width=size[0],height=size[1])
        self._schedule_layer()


class LyricPerformers(_PerformerColumn):
    """Use attach_to() to place outside the lyric text without adding a row.

    ArtistHeader supplies the existing per-pixel alpha layer and input routing.
    Placement does not participate in the lyric grid or add a text row.
    """
    def __init__(self,parent,*,update_path=None,max_lines=2,**options):
        self.update_path=update_path
        self._marker_colors=()
        self._marker_names=()
        self._partial=False
        self._compact=False
        self._marker_scale=1.0
        self._anchor_widget=None
        options.pop('wraplength',None)
        options.setdefault('justify','center')
        options.setdefault('font',('Malgun Gothic',11,'bold'))
        super().__init__(parent,max_lines=max_lines,**options)
        self.columns=(self,_PerformerColumn(parent,max_lines=max_lines,**options))

    def attach_to(self,widget):
        self._anchor_widget=widget
        for column in self.columns:
            widget.bind('<Configure>',column._schedule_layer,add='+')

    def _position(self):
        if self._anchor_widget is None:return
        left,right=self.columns
        if self._compact or not self._marker_names:
            for column in self.columns:
                column.set_column(self._anchor_widget,(),(),0,self._marker_scale)
            return
        available=max(MARKER_HEIGHT,round(self._anchor_widget.winfo_height()/self._marker_scale))
        rows=min(MAX_CHIP_ROWS,(available+MARKER_GAP)//(MIN_CHIP_HEIGHT+MARKER_GAP))
        cell_height=min(MARKER_HEIGHT,(available+MARKER_GAP)//rows-MARKER_GAP)
        left.set_column(self._anchor_widget,self._marker_names[:rows],self._marker_colors[:rows],
                        rows,self._marker_scale,8+SECOND_COLUMN_OFFSET,cell_height)
        end=2*rows if rows==MAX_CHIP_ROWS else len(self._marker_names)
        right.set_column(self._anchor_widget,self._marker_names[rows:end],self._marker_colors[rows:end],
                         rows,self._marker_scale,cell_height=cell_height)

    def _sync_layer(self):
        self._position()
        super()._sync_layer()

    def set_style(self,style):
        for column in self.columns:
            column._marker_style=dict(style)
            column._column_layout=None
        self._position()

    def set_compact(self,compact):
        if self._compact==compact:return
        self._compact=compact
        self._position()

    def set_scale(self,scale):
        if self._marker_scale==scale:return
        self._marker_scale=scale
        self._position()

    def set_singers(self,names,album='',partial=False):
        """Update the chip columns and their shared credits."""
        text,spans=performer_credit(names,album,self.update_path)
        colors=_marker_colors(names,album,self.update_path)
        supplied=(names,) if isinstance(names,str) else names if isinstance(names,(list,tuple)) else ()
        labels=tuple(short_performer_name(name,album) for name in dict.fromkeys(
            value.strip() for value in supplied if isinstance(value,str) and value.strip()))
        partial=bool(partial and text)
        visible=bool(self.place_info())
        changed=(text!=self.cget('text') or spans!=self.cget('outline_spans')
                 or colors!=self._marker_colors
                 or labels!=self._marker_names
                 or partial!=self._partial
                 or visible!=bool(text and not self._compact))
        if changed:
            hint=('확인된 가창 · '+text[len(PREFIX):]+'\n일부 파트의 가창자는 미확인') if partial else text
            for column in self.columns:
                column.hint.hide()
                column.hint.text=hint
            self._partial=partial
            self._marker_colors=colors
            self._marker_names=labels
            self.configure(text=text,outline_spans=spans)
        self._position()
        return changed
