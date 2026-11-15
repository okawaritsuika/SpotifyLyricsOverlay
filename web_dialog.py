"""Preview a wiki lyric table before saving or confirming replacement."""
import tkinter as tk
from tkinter import ttk
from web_lyrics import namu_url,parse_table_text,make_song,validate_url,source_id


class WebLyricsDialog:
    def __init__(self,app):
        self.app=app
        self.playback=app.playback
        self.lines=self.source=None
        self.fetch_identity=None
        self.window=tk.Toplevel(app.root)
        w=self.window
        w.title('위키 · 아이마스 가사 가져오기')
        w.geometry('760x650');w.minsize(560,530);w.attributes('-topmost',False)
        w.columnconfigure(0,weight=1);w.rowconfigure(2,weight=1)
        fields=ttk.Frame(w,padding=12);fields.grid(row=0,column=0,sticky='ew');fields.columnconfigure(1,weight=1)
        self.title=tk.StringVar(value=app.playback.title)
        self.artist=tk.StringVar(value=app.playback.artist)
        self.album=tk.StringVar(value=app.playback.album)
        self.url=tk.StringVar(value=namu_url(app.playback.title) if app.playback.title else '')
        for row,(label,var) in enumerate([('곡 제목',self.title),('아티스트',self.artist),('앨범',self.album),('문서 URL',self.url)]):
            ttk.Label(fields,text=label).grid(row=row,column=0,sticky='w',padx=(0,10),pady=3)
            ttk.Entry(fields,textvariable=var).grid(row=row,column=1,sticky='ew',pady=3)
        self.status=tk.StringVar(value='위키 URL을 가져오거나 SideM·학원마스·샤이니 컬러즈 출처에서 일본어 원문을 찾을 수 있습니다.')
        self.status_label=ttk.Label(w,textvariable=self.status,wraplength=680,justify='left')
        self.status_label.grid(row=1,column=0,sticky='ew',padx=12,pady=4)
        self.notebook=ttk.Notebook(w);self.notebook.grid(row=2,column=0,sticky='nsew',padx=12,pady=8)
        paste=ttk.Frame(self.notebook);self.preview_frame=ttk.Frame(self.notebook)
        self.notebook.add(paste,text='가사 표 붙여넣기');self.notebook.add(self.preview_frame,text='구절 미리보기')
        for frame in (paste,self.preview_frame):frame.columnconfigure(0,weight=1);frame.rowconfigure(0,weight=1)
        self.paste=tk.Text(paste,wrap='word',font=('Malgun Gothic',11),undo=True)
        self.paste.grid(row=0,column=0,sticky='nsew')
        scroll=ttk.Scrollbar(paste,orient='vertical',command=self.paste.yview)
        scroll.grid(row=0,column=1,sticky='ns');self.paste.configure(yscrollcommand=scroll.set)
        self.table=ttk.Treeview(self.preview_frame,columns=('original','reading','korean'),show='headings')
        for key,title in [('original','일본어 원문'),('reading','후리가나'),('korean','한국어 번역')]:
            self.table.heading(key,text=title);self.table.column(key,width=160,minwidth=80)
        self.table.grid(row=0,column=0,sticky='nsew')
        scroll=ttk.Scrollbar(self.preview_frame,orient='vertical',command=self.table.yview)
        scroll.grid(row=0,column=1,sticky='ns');self.table.configure(yscrollcommand=scroll.set)
        footer=ttk.Frame(w,padding=12);footer.grid(row=3,column=0,sticky='ew')
        footer.columnconfigure(1,weight=1)
        self.fetch_button=app.button(footer,'URL 가져오기',self.fetch);self.fetch_button.grid(row=0,column=0,sticky='w',padx=3)
        app.button(footer,'붙여넣기 분석',self.parse).grid(row=0,column=1,sticky='w',padx=3)
        self.imas_button=app.button(footer,'아이마스 원문 찾기',lambda:self.fetch(imas=True))
        self.imas_button.grid(row=0,column=2,sticky='e',padx=3)
        app.button(footer,'확장 수신 폴더',app.choose_extension_inbox).grid(row=1,column=0,sticky='w',padx=3,pady=(5,0))
        self.save_button=app.button(footer,'가사 저장 / 덮어쓰기',self.save,state='disabled')
        self.save_button.grid(row=1,column=1,sticky='e',padx=3,pady=(5,0))
        w.bind('<Configure>',lambda e:self.status_label.configure(wraplength=max(300,e.width-30)) if e.widget is w else None)
        w.protocol('WM_DELETE_WINDOW',self.close)

    def fetch(self,*,imas=False):
        if self.fetch_identity is not None:return
        title=self.title.get().strip()
        if not title:raise ValueError('곡 제목을 입력하세요.')
        self.fetch_identity=self.identity()
        self.fetch_button.configure(state='disabled')
        self.imas_button.configure(state='disabled')
        self.app.fetch_web_lyrics(title,self.artist.get(),self.album.get(),self.url.get(),self,imas=imas)
        self.status.set('아이마스 전용 출처에서 원문을 찾고 있습니다…' if imas else '문서에서 가사 표를 확인하고 있습니다…')

    def identity(self):
        return tuple(v.get().strip() for v in (self.title,self.artist,self.album,self.url))

    def parse(self):
        if self.fetch_identity is not None:raise ValueError('문서 조회가 끝난 뒤 붙여넣기를 분석하세요.')
        lines=parse_table_text(self.paste.get('1.0','end-1c'),source=source_id(self.url.get()) if self.url.get().strip() else 'namu')
        source={'url':validate_url(self.url.get()) if self.url.get().strip() else '',
                     'title':self.title.get(),'translation':'붙여넣은 한국어 번역','reading':'로컬 자동 히라가나 독음'}
        self.lines,self.source=lines,source
        self.preview()

    def receive(self,lines,source,error):
        identity=self.fetch_identity
        self.fetch_identity=None
        self.fetch_button.configure(state='normal')
        self.imas_button.configure(state='normal')
        if identity!=self.identity():
            self.status.set('조회 중 곡 정보가 바뀌었습니다. 바뀐 정보로 다시 가져오세요.')
            return
        if error:self.status.set(error);return
        self.lines,self.source=lines,source
        self.preview()

    def preview(self):
        self.table.delete(*self.table.get_children())
        for line in self.lines:self.table.insert('','end',values=(line.original,line.reading,line.korean))
        self.status.set(f'{len(self.lines)}구절을 찾았습니다. 기존 가사가 있으면 경고를 확인한 뒤 덮어쓸 수 있습니다.')
        self.save_button.configure(state='normal');self.notebook.select(self.preview_frame)

    def save(self):
        if not self.lines:return
        title=self.title.get().strip()
        if not title:raise ValueError('곡 제목을 입력하세요.')
        song=make_song(title,self.artist.get().strip(),self.album.get().strip(),self.lines)
        same=tuple(v.get().strip() for v in (self.title,self.artist,self.album))==tuple(
            v.strip() for v in (self.playback.title,self.playback.artist,self.playback.album))
        if not self.app.add_web_song(song,self.source,interactive=True,
                playback_key=self.playback.key if same and self.playback.title else None,parent=self.window):
            self.status.set('덮어쓰기를 취소했습니다. 기존 가사는 그대로입니다.')
            return
        self.status.set('저장했습니다. 같은 곡이 재생되면 자동으로 연결합니다.')
        self.save_button.configure(state='disabled')

    def close(self):
        self.window.destroy();self.app.web_dialog=None
