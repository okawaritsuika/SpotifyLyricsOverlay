"""Independent backdrop opacity and per-language typography controls."""
import ctypes
from ctypes import wintypes
import re
import tkinter as tk
from tkinter import colorchooser, font, ttk

COLOR_KEY = '#010203'
DEFAULT_STYLES = {
    'original': {'family':'Yu Gothic UI','size':24,'color':'#f4f7fc','bold':True},
    'reading': {'family':'Yu Gothic UI','size':14,'color':'#99e3c8','bold':False},
    'korean': {'family':'Malgun Gothic','size':20,'color':'#f1d6a5','bold':False},
}
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


def valid_color(value, fallback):
    if isinstance(value,str) and re.fullmatch(r'#[0-9a-fA-F]{6}',value) and value.lower()!=COLOR_KEY:
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
                             f'{self.root.winfo_rootx():+d}{self.root.winfo_rooty():+d}')
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
    def __init__(self,app):
        self.app=app
        self.values={}
        self.color_buttons={}
        self.window=tk.Toplevel(app.root)
        window=self.window
        window.title('가사 표시 설정')
        window.geometry('650x570')
        window.minsize(560,510)
        window.attributes('-topmost',False)
        window.configure(bg='#10151f')
        window.columnconfigure(0,weight=1)
        window.rowconfigure(0,weight=1)
        self.notebook=ttk.Notebook(window)
        self.notebook.grid(row=0,column=0,sticky='nsew',padx=12,pady=(12,0))
        body=ttk.Frame(self.notebook,padding=10)
        self.typography_tab=body
        self.notebook.add(body,text='글자 · 배경')
        body.columnconfigure(0,weight=1)
        ttk.Label(body,text='언어별 글꼴과 글자 · 배경의 불투명도를 조절하세요.').grid(row=0,column=0,sticky='w',pady=(0,8))
        opacity=ttk.Frame(body)
        opacity.grid(row=2,column=0,sticky='ew',pady=(0,8));opacity.columnconfigure(0,weight=1)
        ttk.Label(opacity,text='글자 불투명도').grid(row=0,column=0,sticky='w')
        self.text_percent=ttk.Label(opacity,text='')
        self.text_percent.grid(row=0,column=1,sticky='e')
        self.text_scale=ttk.Scale(opacity,from_=.15,to=1,variable=app.opacity,command=self.update)
        self.text_scale.grid(row=1,column=0,columnspan=2,sticky='ew',pady=(3,8))
        ttk.Label(opacity,text='배경 불투명도 · 0%이면 배경만 투명').grid(row=2,column=0,sticky='w')
        self.background_percent=ttk.Label(opacity,text='')
        self.background_percent.grid(row=2,column=1,sticky='e')
        self.background_scale=ttk.Scale(opacity,from_=0,to=1,variable=app.background_opacity,command=self.change_background)
        self.background_scale.grid(row=3,column=0,columnspan=2,sticky='ew',pady=(3,0))
        background=ttk.Frame(body)
        background.grid(row=3,column=0,sticky='ew',pady=(0,6))
        self.background_button=app.button(background,'배경색 선택',self.background_color)
        self.background_button.pack(side='left')
        ttk.Checkbutton(background,text='배경 표시',variable=app.transparent,onvalue=False,offvalue=True,
                        command=self.update).pack(side='left',padx=(12,0))
        families=sorted(set(font.families(app.root)))
        self.values={}
        for row,(key,title) in enumerate([('original','일본어 원문'),('reading','후리가나 · 읽는 법'),('korean','한국어 번역')],start=4):
            frame=ttk.LabelFrame(body,text=title,padding=7)
            frame.grid(row=row,column=0,sticky='ew',pady=4)
            frame.columnconfigure(0,weight=1)
            style=app.text_styles[key]
            family=tk.StringVar(value=style['family'])
            size=tk.IntVar(value=style['size'])
            bold=tk.BooleanVar(value=style['bold'])
            self.values[key]=(family,size,bold)
            combo=ttk.Combobox(frame,values=families,textvariable=family,state='readonly',width=18)
            combo.grid(row=0,column=0,sticky='ew',padx=(0,6))
            combo.bind('<<ComboboxSelected>>',self.update)
            spin=ttk.Spinbox(frame,from_=8,to=72,textvariable=size,width=4,state='readonly',command=self.update)
            spin.grid(row=0,column=1,padx=4)
            ttk.Checkbutton(frame,text='굵게',variable=bold,command=self.update).grid(row=0,column=2,padx=4)
            color_button=app.button(frame,'Aa  글자색',lambda k=key:self.text_color(k))
            color_button.grid(row=0,column=3)
            self.color_buttons[key]=color_button
        compact=compact_from_settings({'compact_layout':app.compact_layout})
        self.compact_mode=tk.StringVar(value=compact['mode'])
        self.compact_priorities={}
        self.compact_combos={}
        self.compact_tab=ttk.Frame(self.notebook,padding=16)
        self.notebook.add(self.compact_tab,text='창 축소')
        self.compact_tab.columnconfigure(0,weight=1)
        ttk.Label(self.compact_tab,text='가사창이 작아질 때',font=('Malgun Gothic',12,'bold')).grid(row=0,column=0,sticky='w',pady=(0,10))
        self.compact_radios={}
        for row,(mode,label) in enumerate((('auto_hide','선택한 항목을 순서대로 숨긴 뒤 축소'),
                                          ('shrink_only','숨기지 않고 모두 축소')),start=1):
            radio=ttk.Radiobutton(self.compact_tab,text=label,variable=self.compact_mode,value=mode,command=self.update)
            radio.grid(row=row,column=0,sticky='w',pady=3)
            self.compact_radios[mode]=radio
        choices=ttk.Frame(self.compact_tab)
        choices.grid(row=3,column=0,sticky='w',pady=(14,8))
        for row,(key,label) in enumerate(COMPACT_TARGETS):
            ttk.Label(choices,text=label).grid(row=row,column=0,sticky='w',padx=(0,28),pady=5)
            priority=tk.StringVar(value=COMPACT_PRIORITIES[compact['priorities'][key]])
            self.compact_priorities[key]=priority
            combo=ttk.Combobox(choices,values=COMPACT_PRIORITIES,textvariable=priority,state='readonly',width=14)
            combo.grid(row=row,column=1,sticky='w',pady=5)
            combo.bind('<<ComboboxSelected>>',self.update)
            self.compact_combos[key]=combo
        ttk.Label(self.compact_tab,text='같은 순서의 항목은 함께 숨깁니다.\n숨기지 않는 항목은 공간에 맞춰 작아집니다.',justify='left').grid(row=4,column=0,sticky='w',pady=(4,0))
        footer=ttk.Frame(window,padding=12)
        footer.grid(row=1,column=0,sticky='ew')
        app.button(footer,'기본값 복원',self.reset).pack(side='left')
        save=app.button(footer,'저장하고 닫기',self.close)
        save.configure(bg='#99e3c8',fg='#10151f',activebackground='#b7f0dc',activeforeground='#10151f')
        save.pack(side='right')
        ttk.Label(body,text='변경 사항은 가사창에 바로 적용됩니다.').grid(row=7,column=0,sticky='w',pady=(8,0))
        window.protocol('WM_DELETE_WINDOW',lambda:app.safe(self.close))
        window.bind('<Escape>',lambda _:app.safe(self.close))
        self.refresh_preview()

    def refresh_preview(self):
        self.text_percent.configure(text=f'{round(self.app.opacity.get()*100)}%')
        self.background_percent.configure(text='숨김' if self.app.transparent.get() else f'{round(self.app.background_opacity.get()*100)}%')
        self.background_button.configure(text=f'배경색  {self.app.background_color.upper()}',
                                         highlightthickness=2,highlightbackground=self.app.background_color)
        for key,button in self.color_buttons.items():
            color=self.app.text_styles[key]['color']
            red,green,blue=(int(color[index:index+2],16) for index in (1,3,5))
            background='#1d2635' if (red*299+green*587+blue*114)>128000 else '#edf2f9'
            button.configure(fg=color,bg=background)
        state='disabled' if self.compact_mode.get()=='shrink_only' else 'readonly'
        for combo in self.compact_combos.values():combo.configure(state=state)

    def update(self,*_):
        for key,(family,size,bold) in self.values.items():
            self.app.text_styles[key].update(family=family.get(),size=size.get(),bold=bold.get())
        self.app.font_size.set(self.app.text_styles['original']['size'])
        self.app.compact_layout={'mode':self.compact_mode.get(),
                                 'priorities':{key:COMPACT_PRIORITIES.index(value.get())
                                               for key,value in self.compact_priorities.items()}}
        self.app.apply_appearance()
        self.refresh_preview()

    def change_background(self,*_):
        self.app.transparent.set(self.app.background_opacity.get()<=.001)
        self.update()

    def background_color(self):
        color=colorchooser.askcolor(self.app.background_color,parent=self.window,title='배경색')[1]
        if color:
            self.app.background_color=valid_color(color,'#10151f')
            self.update()

    def text_color(self,key):
        color=colorchooser.askcolor(self.app.text_styles[key]['color'],parent=self.window,title='글자색')[1]
        if color:
            self.app.text_styles[key]['color']=valid_color(color,DEFAULT_STYLES[key]['color'])
            self.update()

    def reset(self):
        self.app.text_styles={key:dict(value) for key,value in DEFAULT_STYLES.items()}
        self.app.opacity.set(.94)
        self.app.background_opacity.set(.55)
        self.app.background_color='#10151f'
        self.app.transparent.set(False)
        for key,(family,size,bold) in self.values.items():
            style=self.app.text_styles[key]
            family.set(style['family']);size.set(style['size']);bold.set(style['bold'])
        self.compact_mode.set(DEFAULT_COMPACT_LAYOUT['mode'])
        for key,priority in self.compact_priorities.items():
            priority.set(COMPACT_PRIORITIES[DEFAULT_COMPACT_LAYOUT['priorities'][key]])
        self.update()

    def close(self):
        self.update()
        self.app.save_settings()
        self.window.destroy()
        self.app.appearance_dialog=None
