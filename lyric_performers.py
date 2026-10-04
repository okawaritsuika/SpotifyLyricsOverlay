"""Short singer names in colored lyric-gutter chips, with complete hover credits."""
import re
import tkinter as tk

from PIL import Image,ImageDraw

from artist_colors import character_color_spans,catalog as color_catalog
from artist_header import ArtistHeader,_credit_font,_credit_mask
from artist_names import artist_label,name_key
from group_credits import with_character_voices
from ui_theme import ControlHint


PREFIX = '현재 가창 · '
MARKER_WIDTH,MARKER_HEIGHT = 64,26
MARKER_GAP = 3
LYRIC_GUTTER = 52


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


def _chip_rows(names,colors,height):
    capacity=max(1,(height+MARKER_GAP)//(MARKER_HEIGHT+MARKER_GAP))
    if len(names)<=capacity:return tuple((name,(color,)) for name,color in zip(names,colors))
    if capacity==1:return ((names[0],colors),)
    count=capacity-1
    return tuple((name,(color,)) for name,color in zip(names[:count],colors[:count]))+(
        ('+'+str(len(names)-count)+'명',colors[count:]),)


def marker_image(colors,names=(),height=MARKER_HEIGHT):
    """Readable white names and distinct source colors survive a light desktop."""
    scale=2
    image=Image.new('RGBA',(MARKER_WIDTH*scale,height*scale))
    if not colors:return image.resize((MARKER_WIDTH,height))
    image.paste((0,0,0,1),(0,0,image.width,image.height))
    draw=ImageDraw.Draw(image)
    rows=_chip_rows(names or tuple('' for _ in colors),colors,height)
    cell_height=min(MARKER_HEIGHT,(height-MARKER_GAP*(len(rows)-1))/len(rows))
    pixels=min(15,max(10,int(cell_height)-9))*scale
    font=_credit_font(pixels,True);ascent,descent=font.getmetrics()
    for index,(label,palette) in enumerate(rows):
        top=round(index*(cell_height+MARKER_GAP)*scale)
        bottom=round((index*(cell_height+MARKER_GAP)+cell_height)*scale)-1
        draw.rounded_rectangle((1,top,MARKER_WIDTH*scale-2,bottom),radius=6*scale,
                               fill=(15,23,38,245),outline=palette[0],width=scale)
        draw.rounded_rectangle((4*scale,top+5*scale,7*scale,bottom-5*scale),radius=scale,fill=palette[0])
        if len(palette)>1:
            for position,color in enumerate(palette):
                left=round((10+position*50/len(palette))*scale)
                right=round((10+(position+1)*50/len(palette))*scale)-1
                draw.rectangle((left,bottom-3*scale,right,bottom-scale),fill=color)
        available=(MARKER_WIDTH-14)*scale
        while label and font.getlength(label)>available:
            label=label[:-2]+'…' if label.endswith('…') else label[:-1]+'…'
        if label:
            mask=_credit_mask(label,pixels,True,ascent,ascent+descent)
            ink=Image.new('RGBA',mask.size,'#f8faff');ink.putalpha(mask)
            image.alpha_composite(ink,(round((MARKER_WIDTH*scale-mask.width)/2)+3*scale,
                                      round((top+bottom-mask.height)/2)))
        if len(palette)>1 and not label.startswith('+'):
            draw.text((MARKER_WIDTH*scale-4*scale,top+scale),'+'+str(len(palette)-1),
                      font=_credit_font(8*scale,True),fill='#f8faff',anchor='rt')
    image=image.resize((MARKER_WIDTH,height),Image.Resampling.LANCZOS)
    image.putalpha(image.getchannel('A').point(lambda value:max(1,value)))
    return image


class LyricPerformers(ArtistHeader):
    """Use attach_to() to place outside the lyric text without adding a row.

    ArtistHeader supplies the existing per-pixel alpha layer and input routing.
    Placement does not participate in the lyric grid or add a text row.
    """
    def __init__(self,parent,*,update_path=None,max_lines=2,**options):
        self.update_path=update_path
        self._marker_colors=()
        self._marker_names=()
        self._marker_height=MARKER_HEIGHT
        self._partial=False
        self._compact=False
        self._marker_scale=1.0
        self._placement=None
        self._anchor_widget=None
        options.pop('wraplength',None)
        options.setdefault('justify','center')
        options.setdefault('font',('Malgun Gothic',11,'bold'))
        super().__init__(parent,max_lines=max_lines,**options)
        self.hint=ControlHint(self,'')

    def attach_to(self,widget):
        self._anchor_widget=widget
        self._placement={'in_':widget,'x':-8,'rely':.5,'anchor':'e',
                         'width':MARKER_WIDTH,'height':self._marker_height}
        widget.bind('<Configure>',self._schedule_layer,add='+')

    def _position(self):
        if self._compact or not self._marker_colors or self._anchor_widget is None:return
        available=max(MARKER_HEIGHT,round(self._anchor_widget.winfo_height()/self._marker_scale))
        rows=min(len(self._marker_names),(available+MARKER_GAP)//(MARKER_HEIGHT+MARKER_GAP))
        height=rows*(MARKER_HEIGHT+MARKER_GAP)-MARKER_GAP
        if height!=self._marker_height:
            self._marker_height=height;self._draw()
        self._placement.update(height=max(1,round(height*self._marker_scale)),
                               width=max(1,round(MARKER_WIDTH*self._marker_scale)),
                               x=-max(1,round(8*self._marker_scale)))
        if (not self.place_info() or self.winfo_height()!=self._placement['height']
                or self.winfo_width()!=self._placement['width']):self.place(**self._placement)

    def _sync_layer(self):
        self._position()
        super()._sync_layer()

    def set_compact(self,compact):
        if self._compact==compact:return
        self._compact=compact
        if compact:
            self.hint.hide();self.place_forget();self._hide_layer()
        else:
            self._position();self._schedule_layer()

    def set_scale(self,scale):
        if self._marker_scale==scale:return
        self._marker_scale=scale
        self._draw();self._position();self._schedule_layer()

    def _draw(self):
        self.delete('lyric')
        self._artist_runs=()
        self._artist_image=marker_image(self._marker_colors,self._marker_names,self._marker_height) if self._marker_colors else None
        width=max(1,round(MARKER_WIDTH*self._marker_scale))
        height=max(1,round(self._marker_height*self._marker_scale))
        if self._artist_image is not None and self._marker_scale!=1:
            self._artist_image=self._artist_image.resize((width,height),Image.Resampling.LANCZOS)
        self.overflow=False
        tk.Canvas.configure(self,height=height,scrollregion=(0,0,width,height))
        if not self._marker_colors:self._hide_layer()

    def set_singers(self,names,album='',partial=False):
        """Return whether metadata or visibility changed; lyric-grid size stays fixed."""
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
            self.hint.hide()
            self.hint.text=('확인된 가창 · '+text[len(PREFIX):]+'\n일부 파트의 가창자는 미확인') if partial else text
            self._partial=partial
            self._marker_colors=colors
            self._marker_names=labels
            self.configure(text=text,outline_spans=spans)
        if text:
            self._position();self._schedule_layer()
        elif visible:self.place_forget()
        return changed
