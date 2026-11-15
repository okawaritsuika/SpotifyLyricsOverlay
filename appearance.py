"""Independent backdrop opacity and per-language typography controls."""
import ctypes
from ctypes import wintypes
import math
import re
import tkinter as tk
from tkinter import colorchooser, ttk, messagebox, simpledialog
from copy import deepcopy

from windows import window_position
from subtitle import (SubtitleLine, contrast_outline, place_side_preview, side_preview_width,
                      text_style_options)
from overlay_fonts import font_families

COLOR_KEY = '#010203'
DEFAULT_BACKGROUND = '#10151f'
DEFAULT_BACKGROUND_OPACITY = .55
DEFAULT_TRANSPARENT = True
DEFAULT_STYLES = {
    'original': {'family':'Yu Gothic UI','size':24,'color':'#f4f7fc','bold':True},
    'reading': {'family':'Yu Gothic UI','size':14,'color':'#99e3c8','bold':False},
    'korean': {'family':'Malgun Gothic','size':20,'color':'#f1d6a5','bold':False},
    'title': {'family':'Malgun Gothic','size':11,'color':'#f4f7fc','bold':True},
    'artists': {'family':'Malgun Gothic','size':11,'color':'#e5eaf2','bold':True},
    'singers': {'family':'Malgun Gothic','size':14,'color':'#f8faff','bold':True},
    'previous': {'family':'Yu Gothic UI','size':12,'color':'#afbbcf','bold':False},
    'next': {'family':'Yu Gothic UI','size':12,'color':'#afbbcf','bold':False},
}
OUTLINE_WIDTH_MAX = 6
OUTLINE_BLUR_MAX = 4
OUTLINE_STEP = .1
for target, style in DEFAULT_STYLES.items():
    style.update(outline_width=0 if target=='singers' else 2, outline_blur=0, outline_color=None)
COMPACT_TARGETS = (
    ('title','제목'),('artists','곡 가창자'),('singers','현재 구절 가창자'),
    ('reading','후리가나'),('original','일본어 원문'),('korean','한국어 번역'),
)
DEFAULT_COMPACT_LAYOUT = {
    'mode':'auto_hide',
    'priorities':{'title':1,'artists':1,'singers':1,'reading':2,'original':0,'korean':0},
}
COMPACT_PRIORITIES = ('숨기지 않음',)+(tuple(f'{number}번째' for number in range(1,7)))


def compact_from_settings(settings):
    stored=settings.get('compact_layout')
    stored=stored if isinstance(stored,dict) else {}
    mode=stored.get('mode')
    if mode not in ('auto_hide','shrink_only'):mode=DEFAULT_COMPACT_LAYOUT['mode']
    priorities=stored.get('priorities')
    priorities=priorities if isinstance(priorities,dict) else {}
    result={}
    for key,default in DEFAULT_COMPACT_LAYOUT['priorities'].items():
        value=priorities.get(key)
        result[key]=value if isinstance(value,int) and not isinstance(value,bool) and 0<=value<=6 else default
    return {'mode':mode,'priorities':result}


def valid_color(value, fallback, *, allow_color_key=False):
    if (isinstance(value,str) and re.fullmatch(r'#[0-9a-fA-F]{6}',value)
            and (allow_color_key or value.lower()!=COLOR_KEY)):
        return value
    return fallback


def styles_from_settings(settings):
    result={}
    stored=settings.get('text_styles')
    stored=stored if isinstance(stored,dict) else {}
    for key,default in DEFAULT_STYLES.items():
        saved=stored.get(key)
        value={**default,**(saved if isinstance(saved,dict) else {})}
        value['color']=valid_color(value['color'],default['color'])
        value['size']=font_size(value['size'],default['size'])
        if not isinstance(value['family'],str) or not value['family'].strip():
            value['family']=default['family']
        if not isinstance(value['bold'],bool):
            value['bold']=default['bold']
        value['outline_width']=outline_value(value['outline_width'],OUTLINE_WIDTH_MAX,default['outline_width'])
        value['outline_blur']=outline_value(value['outline_blur'],OUTLINE_BLUR_MAX,default['outline_blur'])
        value['outline_color']=valid_color(value['outline_color'],None,allow_color_key=True)
        if 'visible' in value and not isinstance(value['visible'],bool):
            value.pop('visible')
        result[key]=value
    if not stored:
        size=max(16,font_size(settings.get('font_size',24),24))
        result['original']['size']=size
        result['reading']['size']=max(10,int(size*.62))
        result['korean']['size']=max(12,int(size*.85))
    return result


def font_size(value,fallback):
    try:return max(8,min(72,int(value)))
    except (TypeError,ValueError,OverflowError):return fallback


def outline_value(value,maximum,fallback):
    if isinstance(value,bool):return fallback
    try:number=float(value)
    except (TypeError,ValueError,OverflowError):return fallback
    return round(min(maximum,max(0,number)),1) if math.isfinite(number) else fallback


class Backdrop:
    def __init__(self,root):
        self.root=root
        self.window=tk.Toplevel(root)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes('-topmost',True)
        self.color='#10151f'
        self.opacity=0
        self.click_through=False
        root.bind('<Configure>',self._event,add='+')
        root.bind('<Map>',self._event,add='+')
        root.bind('<Unmap>',self._event,add='+')

    def _event(self,event):
        if event.widget is self.root:
            self.sync()

    def sync(self):
        if not self.root.winfo_exists() or not self.window.winfo_exists():
            return
        if self.opacity<=0 or not self.root.winfo_viewable():
            self.window.withdraw()
            return
        self.window.configure(bg=self.color)
        self.window.attributes('-alpha',self.opacity)
        self.window.geometry(f'{self.root.winfo_width()}x{self.root.winfo_height()}'
                             f'{window_position(self.root.winfo_rootx(), self.root.winfo_rooty())}')
        user32=ctypes.windll.user32
        user32.GetAncestor.argtypes=(wintypes.HWND,wintypes.UINT)
        user32.GetAncestor.restype=wintypes.HWND
        back=user32.GetAncestor(self.window.winfo_id(),2)
        front=user32.GetAncestor(self.root.winfo_id(),2)
        get=user32.GetWindowLongPtrW
        get.argtypes=(wintypes.HWND,ctypes.c_int)
        get.restype=ctypes.c_ssize_t
        set_style=user32.SetWindowLongPtrW
        set_style.argtypes=(wintypes.HWND,ctypes.c_int,ctypes.c_ssize_t)
        set_style.restype=ctypes.c_ssize_t
        style=(get(back,-20)|0x80080)&~0x08000020  # layered tool window
        set_style(back,-20,style|0x08000020 if self.click_through else style)
        self.window.deiconify()
        user32.SetWindowPos.argtypes=(wintypes.HWND,wintypes.HWND,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,wintypes.UINT)
        user32.SetWindowPos(back,front,0,0,0,0,0x0013)


class AppearanceDialog:
    """Edit an isolated draft and commit it only with Apply."""
    TARGETS=(('title','곡 제목'),('artists','곡 가창자'),('singers','구절 가창자'),
             ('original','일본어 원문'),('reading','읽는 법'),('korean','한국어 번역'),
             ('previous','이전 구절 · 왼쪽'),('next','다음 구절 · 오른쪽'))

    def __init__(self,app):
        self.app=app; self.styles=styles_from_settings({'text_styles':app.text_styles}); self.values={}; self.color_buttons={}
        self.window=tk.Toplevel(app.root); w=self.window
        w.title('가사 표시 설정'); w.geometry('800x660'); w.minsize(650,500)
        w.configure(bg='#f2f3f7'); w.columnconfigure(0,weight=1); w.rowconfigure(1,weight=1)
        header=tk.Frame(w,bg='white',padx=20,pady=14); header.grid(row=0,column=0,sticky='ew')
        tk.Label(header,text='가사 표시 설정',font=('Malgun Gothic',15,'bold'),bg='white',fg='#293044').pack(anchor='w')
        tk.Label(header,text='미리보기를 확인한 뒤 적용하세요',bg='white',fg='#5c6478').pack(anchor='w')
        tabs=ttk.Notebook(w); tabs.grid(row=1,column=0,sticky='nsew',padx=14,pady=12)
        body=ttk.Frame(tabs,padding=12); tabs.add(body,text='표시 항목'); body.columnconfigure(1,weight=1)
        families=font_families(app.root)
        for row,(key,title) in enumerate(self.TARGETS):
            style=self.styles[key]
            show=style.get('visible',key not in ('previous','next'))
            if key=='original':show=app.show_original.get()
            if key=='reading':show=app.show_reading.get()
            visible=tk.BooleanVar(value=show); family=tk.StringVar(value=style['family']); size=tk.IntVar(value=style['size']); bold=tk.BooleanVar(value=style['bold'])
            self.values[key]=(visible,family,size,bold)
            ttk.Checkbutton(body,text=title,variable=visible,command=self.update).grid(row=row,column=0,sticky='w',pady=7,padx=(0,8))
            combo=ttk.Combobox(body,values=families,textvariable=family); combo.grid(row=row,column=1,sticky='ew',padx=4); combo.bind('<<ComboboxSelected>>',self.update); combo.bind('<KeyRelease>',self.update)
            spin=ttk.Spinbox(body,from_=8,to=72,textvariable=size,width=5,command=self.update); spin.grid(row=row,column=2,padx=4); spin.bind('<KeyRelease>',self.update); spin.bind('<FocusOut>',self.update)
            ttk.Checkbutton(body,text='굵게',variable=bold,command=self.update).grid(row=row,column=3,padx=4)
            button=ttk.Button(body,command=lambda k=key:self.text_color(k)); button.grid(row=row,column=4,padx=4); self.color_buttons[key]=button
        self._build_outline_controls(tabs)
        self.opacity=tk.DoubleVar(value=app.opacity.get()); self.background_opacity=tk.DoubleVar(value=app.background_opacity.get()); self.transparent=tk.BooleanVar(value=app.transparent.get()); self.bg_color=app.background_color
        backdrop=ttk.Frame(tabs,padding=16); tabs.add(backdrop,text='배경 · 불투명도'); backdrop.columnconfigure(0,weight=1)
        ttk.Label(backdrop,text='글자 불투명도').grid(row=0,column=0,sticky='w'); self.text_percent=ttk.Label(backdrop); self.text_percent.grid(row=0,column=1)
        ttk.Scale(backdrop,from_=.15,to=1,variable=self.opacity,command=self.update).grid(row=1,column=0,columnspan=2,sticky='ew',pady=(4,18))
        ttk.Checkbutton(backdrop,text='배경 표시',variable=self.transparent,onvalue=False,offvalue=True,command=self.update).grid(row=2,column=0,sticky='w'); self.bg_percent=ttk.Label(backdrop); self.bg_percent.grid(row=2,column=1)
        ttk.Scale(backdrop,from_=0,to=1,variable=self.background_opacity,command=self.change_background_opacity).grid(row=3,column=0,columnspan=2,sticky='ew',pady=(4,18))
        self.bg_button=ttk.Button(backdrop,command=self.background_color); self.bg_button.grid(row=4,column=0,sticky='w')
        compact=compact_from_settings({'compact_layout':app.compact_layout}); self.compact_mode=tk.StringVar(value=compact['mode']); self.priorities={}; self.combos={}
        tab=ttk.Frame(tabs,padding=16); tabs.add(tab,text='창 축소')
        for row,(mode,label) in enumerate((('auto_hide','선택한 항목을 순서대로 숨긴 뒤 축소'),('shrink_only','숨기지 않고 모두 축소'))):ttk.Radiobutton(tab,text=label,variable=self.compact_mode,value=mode,command=self.update).grid(row=row,column=0,columnspan=2,sticky='w',pady=3)
        for row,(key,label) in enumerate(COMPACT_TARGETS,start=2):
            ttk.Label(tab,text=label).grid(row=row,column=0,sticky='w',padx=(0,28),pady=5)
            var=tk.StringVar(value=COMPACT_PRIORITIES[compact['priorities'][key]]); self.priorities[key]=var
            combo=ttk.Combobox(tab,values=COMPACT_PRIORITIES,textvariable=var,state='readonly',width=14); combo.grid(row=row,column=1,pady=5); combo.bind('<<ComboboxSelected>>',self.update); self.combos[key]=combo
        bar=ttk.Frame(w,padding=(14,0)); bar.grid(row=2,column=0,sticky='ew'); bar.columnconfigure(1,weight=1)
        ttk.Label(bar,text='프리셋').grid(row=0,column=0,padx=(0,8)); self.preset_name=tk.StringVar(); self.preset_combo=ttk.Combobox(bar,textvariable=self.preset_name,state='readonly'); self.preset_combo.grid(row=0,column=1,sticky='ew')
        ttk.Button(bar,text='불러오기',command=self.load_preset).grid(row=0,column=2,padx=5); ttk.Button(bar,text='저장',command=self.save_preset).grid(row=0,column=3); ttk.Button(bar,text='미리보기 표시',command=self.show_preview).grid(row=0,column=4,padx=(5,0))
        footer=ttk.Frame(w,padding=14); footer.grid(row=3,column=0,sticky='ew'); ttk.Button(footer,text='기본값 복원',command=self.reset).pack(side='left'); ttk.Button(footer,text='적용',command=self.apply).pack(side='right'); ttk.Button(footer,text='취소',command=self.close).pack(side='right',padx=7)
        self.preview=tk.Toplevel(w); self.preview.title('가사 미리보기'); self.preview.minsize(240,150)
        self.preview.attributes('-topmost',True)
        self.preview.configure(bg=COLOR_KEY); self.preview.attributes('-transparentcolor',COLOR_KEY)
        w.update_idletasks()
        screen_w,screen_h=w.winfo_screenwidth(),w.winfo_screenheight()
        preview_width=max(240,min(1000,screen_w-40)); preview_height=min(280,max(150,screen_h-80))
        x,y=w.winfo_rootx(),w.winfo_rooty(); right=x+w.winfo_width()+16
        right_space=screen_w-right-12; below=y+w.winfo_height()+38
        if right_space>=760:
            preview_width=min(preview_width,right_space)
            px,py=right,max(0,min(y,screen_h-preview_height-45))
        elif below+preview_height+35<=screen_h:
            px=max(0,min(x,screen_w-preview_width-20)); py=below
        else:
            px=max(0,min(x,screen_w-preview_width-20)); py=max(0,screen_h-preview_height-50)
        self.preview.geometry(f'{preview_width}x{preview_height}{window_position(px,py)}')
        self.preview_backdrop=Backdrop(self.preview)
        self.preview_backdrop.click_through=True
        self._build_preview()
        self.preview.protocol('WM_DELETE_WINDOW',self.preview.withdraw)
        self.preview.bind('<Configure>',lambda event:self.draw_preview() if event.widget is self.preview else None)
        w.protocol('WM_DELETE_WINDOW',self.close); w.bind('<Escape>',lambda _:self.close()); self.refresh_presets(); self.update()

    def draft(self):
        styles=deepcopy(self.styles)
        for key,(visible,family,size,bold) in self.values.items():
            try:number=font_size(size.get(),styles[key]['size'])
            except tk.TclError:number=styles[key]['size']
            styles[key].update(visible=visible.get(),family=family.get().strip() or DEFAULT_STYLES[key]['family'],size=number,bold=bold.get())
        return {'text_styles':styles,'opacity':self.opacity.get(),'background_opacity':self.background_opacity.get(),'transparent':self.transparent.get(),'background_color':self.bg_color,'compact_layout':{'mode':self.compact_mode.get(),'priorities':{k:COMPACT_PRIORITIES.index(v.get()) for k,v in self.priorities.items()}}}

    def update(self,*_):
        self.text_percent.configure(text=f'{round(self.opacity.get()*100)}%'); self.bg_percent.configure(text=f'{round(self.background_opacity.get()*100)}%'); self.bg_button.configure(text=f'배경색  {self.bg_color.upper()}')
        for key,button in self.color_buttons.items():button.configure(text=self.styles[key]['color'].upper())
        self._show_outline()
        for combo in self.combos.values():combo.configure(state='disabled' if self.compact_mode.get()=='shrink_only' else 'readonly')
        self.draw_preview()

    def change_background_opacity(self,_):
        self.transparent.set(False)
        self.update()

    def _build_outline_controls(self,tabs):
        panel=ttk.Frame(tabs,padding=16)
        tabs.add(panel,text='글자 외곽선')
        self.outline_target=ttk.Combobox(panel,values=[label for _,label in self.TARGETS],state='readonly')
        self.outline_target.current(3)
        ttk.Label(panel,text='표시 항목').grid(row=0,column=0,sticky='w',padx=(0,22))
        self.outline_target.grid(row=0,column=1,sticky='w')
        self.outline_target.bind('<<ComboboxSelected>>',lambda _:self._show_outline())
        self.outline_choices={}
        for row,(key,label,maximum) in enumerate((('outline_width','두께',OUTLINE_WIDTH_MAX),
                                                   ('outline_blur','외곽선 흐림',OUTLINE_BLUR_MAX)),1):
            ttk.Label(panel,text=label).grid(row=row,column=0,sticky='w',pady=6)
            field=ttk.Frame(panel)
            field.grid(row=row,column=1,sticky='w')
            spin=ttk.Spinbox(field,from_=0,to=maximum,increment=OUTLINE_STEP,format='%.1f',
                             width=6,state='readonly')
            spin.configure(command=lambda prop=key,control=spin:self._set_outline(prop,float(control.get())))
            spin.pack(side='left')
            ttk.Label(field,text='px').pack(side='left',padx=(5,0))
            self.outline_choices[key]=spin
        ttk.Label(panel,text='외곽선 색상').grid(row=3,column=0,sticky='w',pady=6)
        self.outline_color_button=ttk.Button(panel,command=self._pick_outline_color)
        self.outline_color_button.grid(row=3,column=1,sticky='w')
        self.outline_auto_button=ttk.Button(panel,text='자동 대비로 복원',command=lambda:self._set_outline('outline_color',None))
        self.outline_auto_button.grid(row=3,column=2,sticky='w',padx=6)

    def _outline_style(self):
        return self.styles[self.TARGETS[self.outline_target.current()][0]]

    def _show_outline(self):
        style=self._outline_style()
        for key,spin in self.outline_choices.items():spin.set(f'{style[key]:.1f}')
        color=style['outline_color']
        self.outline_color_button.configure(text=color.upper() if color else '색상 선택…')
        self.outline_auto_button.configure(state='normal' if color else 'disabled')

    def _set_outline(self,key,value):
        self._outline_style()[key]=value
        self.update()

    def _pick_outline_color(self):
        style=self._outline_style()
        auto=contrast_outline(*(value//257 for value in self.window.winfo_rgb(style['color'])))
        color=colorchooser.askcolor(style['outline_color'] or auto,parent=self.window,title='외곽선 색상')[1]
        if color:self._set_outline('outline_color',valid_color(color,style['outline_color'],allow_color_key=True))

    def _build_preview(self):
        from artist_header import ArtistHeader
        preview=self.preview
        preview.columnconfigure(0,weight=1); preview.rowconfigure(0,weight=1)
        self.preview_frame=tk.Frame(preview,bg=COLOR_KEY)
        self.preview_frame.grid(row=0,column=0,sticky='nsew',padx=22,pady=(6,4))
        self.preview_frame.columnconfigure(0,weight=1); self.preview_frame.rowconfigure(1,weight=1)
        self.preview_metadata=tk.Frame(self.preview_frame,bg=COLOR_KEY)
        self.preview_metadata.grid(row=0,column=0,sticky='ew',pady=(0,8)); self.preview_metadata.columnconfigure(0,weight=1)
        self.preview_headers={}
        for row,(key,text) in enumerate((('title','샘플 곡 제목'),('artists','가창자 A · 가창자 B'))):
            item=ArtistHeader(self.preview_metadata,bg=COLOR_KEY,fg='#293044',font=('Malgun Gothic',11,'bold'),justify='left',max_lines=1 if key=='title' else 2)
            item.grid(row=row,column=0,sticky='new',pady=(3,0) if row else 0); item.configure(text=text)
            self.preview_headers[key]=item
        self.preview_content=tk.Frame(self.preview_frame,bg=COLOR_KEY)
        self.preview_content.grid(row=1,column=0,sticky='ew'); self.preview_content.columnconfigure(0,weight=1,minsize=2)
        self.preview_content.bind('<Configure>',lambda event:self.draw_preview())
        self.preview_lines={}
        for row,(key,text,padding) in enumerate((('reading','かがやく みらいへ',(0,2)),('original','輝く未来へ',(2,2)),('korean','빛나는 미래로',(2,3))),1):
            item=SubtitleLine(self.preview_content,bg=COLOR_KEY,fg='#293044',font=('Malgun Gothic',14),justify='center')
            item.grid(row=row,column=0,sticky='nsew',pady=padding); item.configure(text=text); self.preview_lines[key]=item
        self.preview_sides={}
        for key,text in (('previous','지나온 길을 돌아보면 늘 함께했던 너와 보낸 시간이 떠올라'),
                         ('next','저 멀리 보이는 빛을 향해 우리 함께 노래하며 달려갈 거야')):
            item=SubtitleLine(self.preview_frame,bg=COLOR_KEY,fg='#5c6478',font=('Malgun Gothic',12),
                              justify='right' if key=='previous' else 'left',
                              ellipsis='start' if key=='previous' else 'end')
            item.configure(text=text); self.preview_sides[key]=item
        self.preview_singers=tk.Label(self.preview_frame,bg=COLOR_KEY,borderwidth=0,highlightthickness=0)
        self.preview_singer_image=None; self.preview_singer_key=None

    def draw_preview(self):
        if not hasattr(self,'preview_singers') or not self.preview.winfo_exists():return
        from lyric_performers import LYRIC_GUTTER, MARKER_HEIGHT, MARKER_GAP, MIN_CHIP_HEIGHT, marker_image
        from PIL import Image,ImageTk
        draft=self.draft(); styles=draft['text_styles']; width=self.preview.winfo_width()
        hidden=set(); compact=draft['compact_layout']
        if compact['mode']=='auto_hide':
            stages=sorted(set(v for v in compact['priorities'].values() if v))
            for threshold,priority in zip((760,540,440,360,300,260),stages):
                if width>=threshold:break
                hidden.update(k for k,v in compact['priorities'].items() if v==priority)
        shrink_only=compact['mode']=='shrink_only' or not any(compact['priorities'].values())
        scale=min(1.0,width/(760 if shrink_only else 440))
        singers=styles['singers']['visible'] and 'singers' not in hidden
        gutter=round(LYRIC_GUTTER*scale) if singers else 0
        self.preview_content.grid_configure(padx=gutter)
        for key,item in (*self.preview_headers.items(),*self.preview_lines.items()):
            style=styles[key]; item.configure(**text_style_options(style,scale))
            if key in self.preview_lines:
                padding={'reading':(0,2),'original':(2,2),'korean':(2,3)}[key]
                item.grid_configure(pady=tuple(round(v*scale) for v in padding))
            if style['visible'] and key not in hidden:item.grid()
            else:item.grid_remove()
        side_width=side_preview_width(width,self.preview_lines.values())
        for key,item in self.preview_sides.items():
            if side_width and styles[key]['visible']:
                item.configure(**text_style_options(styles[key],scale))
                place_side_preview(item,self.preview_frame,self.preview_content,key,side_width)
            else:item.place_forget()
        if singers:
            marker_width=102
            available=max(2*MIN_CHIP_HEIGHT+MARKER_GAP,round(self.preview_content.winfo_height()/scale))
            rows=min(4,(available+MARKER_GAP)//(MIN_CHIP_HEIGHT+MARKER_GAP))
            cell_height=min(MARKER_HEIGHT,(available+MARKER_GAP)//rows-MARKER_GAP)
            height=min(rows,2)*(cell_height+MARKER_GAP)-MARKER_GAP
            style=styles['singers']; key=(tuple(sorted(style.items())),height,rows,round(scale,3))
            size=(max(1,round(marker_width*scale)),max(1,round(height*scale)))
            if key!=self.preview_singer_key:
                marker=marker_image(('#f27b9f','#83b8d4'),('하루카','치하야'),height,capacity=rows,style=style,width=marker_width)
                if marker.size!=size:marker=marker.resize(size,Image.Resampling.LANCZOS)
                self.preview_singer_image=ImageTk.PhotoImage(marker,master=self.preview)
                self.preview_singers.configure(image=self.preview_singer_image)
                self.preview_singer_key=key
            self.preview_singers.place(in_=self.preview_content,rely=.5,x=-max(1,round(8*scale)),anchor='e',width=size[0],height=size[1])
        else:self.preview_singers.place_forget()
        self.preview.attributes('-alpha',max(.15,min(1,draft['opacity'])))
        self.preview_backdrop.color=draft['background_color']
        self.preview_backdrop.opacity=0 if draft['transparent'] else max(0,min(1,draft['background_opacity']))
        self.preview_backdrop.sync()

    def show_preview(self):
        self.preview.deiconify(); self.preview.lift(); self.draw_preview()

    def text_color(self,key):
        color=colorchooser.askcolor(self.styles[key]['color'],parent=self.window,title='글자색')[1]
        if color:self.styles[key]['color']=valid_color(color,DEFAULT_STYLES[key]['color']); self.update()

    def background_color(self):
        color=colorchooser.askcolor(self.bg_color,parent=self.window,title='배경색')[1]
        if color:
            self.bg_color=valid_color(color,DEFAULT_BACKGROUND)
            if not self.background_opacity.get():self.background_opacity.set(DEFAULT_BACKGROUND_OPACITY)
            self.transparent.set(False)
            self.update()

    def set_draft(self,draft):
        self.styles=styles_from_settings(draft)
        for key,(visible,family,size,bold) in self.values.items():
            style=self.styles[key]; visible.set(style.get('visible',key not in ('previous','next'))); family.set(style['family']); size.set(style['size']); bold.set(style['bold'])
        self.opacity.set(draft.get('opacity',.94)); self.background_opacity.set(draft.get('background_opacity',DEFAULT_BACKGROUND_OPACITY)); self.transparent.set(draft.get('transparent',DEFAULT_TRANSPARENT)); self.bg_color=valid_color(draft.get('background_color'),DEFAULT_BACKGROUND)
        compact=compact_from_settings(draft); self.compact_mode.set(compact['mode'])
        for key,var in self.priorities.items():var.set(COMPACT_PRIORITIES[compact['priorities'][key]])
        self.update()

    def reset(self):self.set_draft({'text_styles':deepcopy(DEFAULT_STYLES),'opacity':.94,'background_opacity':DEFAULT_BACKGROUND_OPACITY,'transparent':DEFAULT_TRANSPARENT,'background_color':DEFAULT_BACKGROUND})
    def refresh_presets(self):
        presets=self.app.settings.get('appearance_presets',{}); self.preset_combo.configure(values=sorted(presets) if isinstance(presets,dict) else [])
    def load_preset(self):
        presets=self.app.settings.get('appearance_presets',{})
        if isinstance(presets,dict) and isinstance(presets.get(self.preset_name.get()),dict):self.set_draft(deepcopy(presets[self.preset_name.get()]))
    def save_preset(self):
        choice=messagebox.askyesnocancel('프리셋 저장','새 프리셋을 만드시겠습니까?\n\n예: 새로 만들기   아니요: 선택한 프리셋 덮어쓰기',parent=self.window)
        if choice is None:return
        existing=self.app.settings.get('appearance_presets',{}); presets=deepcopy(existing) if isinstance(existing,dict) else {}
        if choice:
            name=simpledialog.askstring('새 프리셋','프리셋 이름',parent=self.window)
            if name is None or not name.strip():return
            name=name.strip()
            if any(name.casefold()==str(item).casefold() for item in presets):messagebox.showerror('프리셋 저장','같은 이름의 프리셋이 있습니다.',parent=self.window); return
        else:
            name=self.preset_name.get()
            if name not in presets:messagebox.showinfo('프리셋 저장','덮어쓸 프리셋을 먼저 선택하세요.',parent=self.window); return
            if not messagebox.askyesno('덮어쓰기',f'“{name}” 프리셋을 덮어쓰시겠습니까?',parent=self.window):return
        presets[name]=self.draft(); self.app.settings['appearance_presets']=presets; self.app.save_settings(); self.refresh_presets(); self.preset_name.set(name)
    def apply(self):
        draft=self.draft(); app=self.app; app.text_styles=deepcopy(draft['text_styles']); app.font_size.set(app.text_styles['original']['size']); app.show_original.set(app.text_styles['original']['visible']); app.show_reading.set(app.text_styles['reading']['visible']); app.opacity.set(draft['opacity']); app.background_opacity.set(draft['background_opacity']); app.transparent.set(draft['transparent']); app.background_color=draft['background_color']; app.compact_layout=deepcopy(draft['compact_layout']); app.apply_appearance(); app.save_settings(); self.close()
    def close(self):
        if self.preview.winfo_exists():self.preview.destroy()
        self.window.destroy(); self.app.appearance_dialog=None
