"""A small connection screen for choosing how to receive lyrics."""
import tkinter as tk
from tkinter import ttk


BG, PANEL, TEXT, MUTED, ACCENT = '#10151f', '#1d2635', '#f4f7fc', '#afbbcf', '#99e3c8'


class ConnectionDialog:
    def __init__(self,app):
        self.app=app
        self.window=tk.Toplevel(app.root)
        self.window.title('가사 연결')
        self.window.configure(bg=BG)
        height=min(800,max(500,self.window.winfo_screenheight()-100))
        self.window.geometry(f'620x{height}');self.window.minsize(560,500)
        self.window.attributes('-topmost',False)
        self.window.columnconfigure(0,weight=1);self.window.rowconfigure(1,weight=1)
        self.window.protocol('WM_DELETE_WINDOW',self.close)
        self.wrapped=[]

        header=tk.Frame(self.window,bg=BG)
        header.grid(row=0,column=0,sticky='ew',padx=20,pady=(18,10))
        tk.Label(header,text='아이마스 음악에 가사를 연결하세요',bg=BG,fg=TEXT,
                 font=('맑은 고딕',16,'bold'),anchor='w').pack(fill='x')
        self.label(header,'Spotify 연결로 시작하고, 필요한 가사 제공자를 추가하세요.',BG).pack(fill='x',pady=(4,0))

        content=tk.Frame(self.window,bg=BG)
        content.grid(row=1,column=0,sticky='nsew',padx=(20,8))
        content.columnconfigure(0,weight=1);content.rowconfigure(0,weight=1)
        self.canvas=tk.Canvas(content,bg=BG,highlightthickness=0,bd=0)
        self.canvas.grid(row=0,column=0,sticky='nsew')
        scrollbar=ttk.Scrollbar(content,orient='vertical',command=self.canvas.yview)
        scrollbar.grid(row=0,column=1,sticky='ns',padx=(6,0))
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.body=tk.Frame(self.canvas,bg=BG)
        self.body.columnconfigure(0,weight=1)
        self.body_id=self.canvas.create_window(0,0,window=self.body,anchor='nw')
        self.body.bind('<Configure>',lambda _:self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>',self.resize)
        self.window.bind('<MouseWheel>',self.scroll)
        self.window.bind('<Escape>',lambda _:self.close())

        status=tk.Frame(self.body,bg=PANEL,padx=14,pady=10,highlightthickness=1,highlightbackground='#35455a')
        status.grid(row=0,column=0,sticky='ew',pady=(0,10))
        tk.Label(status,text='현재 연결 상태',bg=PANEL,fg=ACCENT,anchor='w',
                 font=('맑은 고딕',9,'bold')).pack(fill='x')
        self.status_headline=self.label(status,'',PANEL,TEXT,True)
        self.status_headline.pack(fill='x',pady=(3,0))
        self.status_detail=self.label(status,'',PANEL)
        self.status_detail.pack(fill='x',pady=(4,0))

        spotify=self.card(1)
        tk.Label(spotify,text='Spotify 가사 · 기본 연결',bg=PANEL,fg=TEXT,
                 font=('맑은 고딕',11,'bold'),anchor='w').grid(row=0,column=0,sticky='w')
        self.spotify_button=self.button(spotify,'Spotify 연결',self.connect_spotify,primary=True)
        self.spotify_button.grid(row=0,column=1,sticky='e',padx=(8,0))
        self.label(spotify,'처음 한 번 로그인하면 다음부터 저장된 연결을 사용합니다.\nSpotify를 최소화하거나 가사창을 닫아도 됩니다.',PANEL).grid(
            row=1,column=0,columnspan=2,sticky='ew',pady=(8,0))

        public=self.card(2)
        tk.Label(public,text='공개 싱크 · LRCLIB',bg=PANEL,fg=TEXT,
                 font=('맑은 고딕',11,'bold'),anchor='w').grid(row=0,column=0,sticky='w')
        self.public_button=self.button(public,'공개 싱크 켜기',self.start_public_lyrics)
        self.public_button.grid(row=0,column=1,sticky='e',padx=(8,0))
        self.public_status=self.label(public,'',PANEL)
        self.public_status.grid(
            row=1,column=0,columnspan=2,sticky='ew',pady=(8,0))
        self.public_disclosure=self.label(public,'곡명과 가수를 LRCLIB(lrclib.net)에 보내 가사를 찾습니다.\n「공개 싱크 켜기」를 누르면 이 전송에 동의합니다.',PANEL)
        self.public_disclosure.grid(row=2,column=0,columnspan=2,sticky='ew',pady=(5,0))

        community=self.card(3)
        tk.Label(community,text='LyricsPlus 싱크',bg=PANEL,fg=TEXT,
                 font=('맑은 고딕',11,'bold'),anchor='w').grid(row=0,column=0,sticky='w')
        self.lyricsplus_button=self.button(community,'LyricsPlus 켜기',self.toggle_lyricsplus)
        self.lyricsplus_button.grid(row=0,column=1,sticky='e',padx=(8,0))
        self.lyricsplus_disclosure=self.label(community,'곡명·가수·앨범·길이를 lyricsplus.prjktla.my.id에 보내고, 장애 때 lyricsplus.binimum.org를 사용합니다.\n「LyricsPlus 켜기」를 누르면 이 전송에 동의합니다.',PANEL)
        self.lyricsplus_disclosure.grid(row=2,column=0,columnspan=2,sticky='ew',pady=(5,0))
        self.lyricsplus_status=self.label(community,'',PANEL)
        self.lyricsplus_status.grid(row=3,column=0,columnspan=2,sticky='ew',pady=(5,0))
        tk.Label(community,text='알송 싱크·번역',bg=PANEL,fg=TEXT,
                 font=('맑은 고딕',11,'bold'),anchor='w').grid(row=4,column=0,sticky='w',pady=(14,0))
        self.alsong_button=self.button(community,'알송 켜기',self.toggle_alsong)
        self.alsong_button.grid(row=4,column=1,sticky='e',padx=(8,0),pady=(14,0))
        self.alsong_disclosure=self.label(community,'곡명·가수를 알송(lyrics.alsong.co.kr)에 HTTP로 보내 검색합니다.\n「알송 켜기」를 누르면 이 전송에 동의하며, 받은 가사·싱크는 저장합니다.',PANEL)
        self.alsong_disclosure.grid(row=5,column=0,columnspan=2,sticky='ew',pady=(5,0))
        self.alsong_status=self.label(community,'',PANEL)
        self.alsong_status.grid(row=6,column=0,columnspan=2,sticky='ew',pady=(5,0))

        footer=tk.Frame(self.window,bg=BG)
        footer.grid(row=2,column=0,sticky='ew',padx=20,pady=(10,14))
        self.advanced_button=self.button(footer,'고급 설정',lambda:app.safe(app.open_reader_settings))
        self.advanced_button.pack(side='left')
        self.close_button=self.button(footer,'완료',self.close)
        self.close_button.pack(side='right')
        self.refresh()

    def label(self,parent,text,bg,fg=MUTED,bold=False):
        label=tk.Label(parent,text=text,bg=bg,fg=fg,anchor='w',justify='left',
                       font=('맑은 고딕',10,'bold' if bold else 'normal'),wraplength=470)
        self.wrapped.append(label)
        return label

    def card(self,row):
        frame=tk.Frame(self.body,bg=PANEL,padx=14,pady=9)
        frame.grid(row=row,column=0,sticky='ew',pady=(0,8))
        frame.columnconfigure(0,weight=1)
        return frame

    def button(self,parent,text,command,primary=False):
        button=tk.Button(parent,text=text,command=command,bg=ACCENT if primary else '#29374b',
                         fg=BG if primary else TEXT,activebackground=ACCENT,activeforeground=BG,
                         relief='flat',bd=0,padx=12,pady=6,cursor='hand2',font=('맑은 고딕',10),
                         highlightthickness=1,highlightbackground=PANEL,highlightcolor=ACCENT)
        button.bind('<FocusIn>',self.reveal_focus)
        return button

    def resize(self,event):
        self.canvas.itemconfigure(self.body_id,width=event.width)
        for label in self.wrapped:label.configure(wraplength=max(260,event.width-32))

    def scroll(self,event):
        if self.body.winfo_height()>self.canvas.winfo_height():
            self.canvas.yview_scroll(-int(event.delta/120),'units')

    def reveal_focus(self,event):
        widget=event.widget
        if not str(widget).startswith(str(self.body)+'.'):
            return
        top=widget.winfo_rooty()-self.body.winfo_rooty()
        bottom=top+widget.winfo_height()
        viewport=self.canvas.canvasy(0)
        if top<viewport:
            self.canvas.yview_moveto(top/max(1,self.body.winfo_height()))
        elif bottom>viewport+self.canvas.winfo_height():
            self.canvas.yview_moveto((bottom-self.canvas.winfo_height())/max(1,self.body.winfo_height()))

    def refresh(self):
        headline,detail=self.app.connection_status()
        self.status_headline.configure(text=headline)
        self.status_detail.configure(text=detail)
        self.public_button.configure(text='공개 싱크 끄기' if self.app.public_background.get() else '공개 싱크 켜기')
        self.public_status.configure(text=self.app.background_lyrics.status if self.app.public_background.get()
                                     else 'Spotify와 함께 사용할 수 있습니다. 저장한 싱크는 재사용합니다.')
        self.lyricsplus_button.configure(text='LyricsPlus 끄기' if self.app.lyricsplus_background.get() else 'LyricsPlus 켜기')
        self.lyricsplus_status.configure(text=self.app.lyricsplus_job.status)
        self.alsong_button.configure(text='알송 끄기' if self.app.alsong_background.get() else '알송 켜기')
        self.alsong_status.configure(text=self.app.alsong_job.status)
        for button,enabled in ((self.public_button,self.app.public_background.get()),
                               (self.lyricsplus_button,self.app.lyricsplus_background.get()),
                               (self.alsong_button,self.app.alsong_background.get())):
            button.configure(bg='#24463e' if enabled else '#29374b',fg=ACCENT if enabled else TEXT)

    def connect_spotify(self):
        self.app.safe(self.app.connect_spotify);self.refresh()

    def start_public_lyrics(self):
        self.app.safe(self.app.stop_public_lyrics if self.app.public_background.get() else self.app.start_public_lyrics)
        self.refresh()

    def toggle_lyricsplus(self):
        self.app.safe(self.app.stop_lyricsplus if self.app.lyricsplus_background.get() else self.app.start_lyricsplus)
        self.refresh()

    def toggle_alsong(self):
        self.app.safe(self.app.stop_alsong if self.app.alsong_background.get() else self.app.start_alsong)
        self.refresh()

    def close(self):
        self.window.destroy();self.app.connection_dialog=None
