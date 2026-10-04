"""A local paste/type dialog for songs that have no lyrics yet."""
import tkinter as tk
from tkinter import ttk
from manual_lyrics import FORMATS,parse_manual,manual_song


class ManualLyricsDialog:
    def __init__(self,app):
        self.app=app;self.closed=False
        self.playback=app.playback
        self.was_suspended=app.hotkeys.suspended
        app.hotkeys.suspend(True)
        self.window=tk.Toplevel(app.root);w=self.window
        w.title('가사 직접 입력');w.geometry('740x650');w.minsize(560,530)
        w.attributes('-topmost',False);w.columnconfigure(0,weight=1);w.rowconfigure(3,weight=1)
        fields=ttk.Frame(w,padding=(12,10));fields.grid(row=0,column=0,sticky='ew');fields.columnconfigure(1,weight=1)
        self.title=tk.StringVar(value=self.playback.title)
        self.artist=tk.StringVar(value=self.playback.artist)
        self.album=tk.StringVar(value=self.playback.album)
        for row,(label,var) in enumerate((('곡 제목',self.title),('아티스트',self.artist),('앨범',self.album))):
            ttk.Label(fields,text=label).grid(row=row,column=0,sticky='w',padx=(0,10),pady=3)
            ttk.Entry(fields,textvariable=var).grid(row=row,column=1,sticky='ew',pady=3)
        self.format=ttk.Combobox(fields,values=FORMATS,state='readonly');self.format.current(0)
        ttk.Label(fields,text='입력 형식').grid(row=3,column=0,sticky='w',pady=3)
        self.format.grid(row=3,column=1,sticky='ew',pady=3)
        self.help=ttk.Label(w,text='원문만 있어도 저장할 수 있습니다. 빈 줄은 구절 사이 구분에 사용합니다.\n독음은 로컬에서 생성하며, 번역은 입력한 내용만 표시합니다.',wraplength=500)
        self.help.grid(row=1,column=0,sticky='ew',padx=12,pady=4)
        self.status=tk.StringVar(value='여기에 직접 쓰거나 Ctrl+V로 가사를 붙여넣으세요.')
        self.status_label=ttk.Label(w,textvariable=self.status,wraplength=500)
        self.status_label.grid(row=2,column=0,sticky='ew',padx=12,pady=4)
        self.notebook=ttk.Notebook(w);self.notebook.grid(row=3,column=0,sticky='nsew',padx=12,pady=6)
        self.input_frame=ttk.Frame(self.notebook);self.preview_frame=ttk.Frame(self.notebook)
        for frame,label in ((self.input_frame,'직접 입력 / 붙여넣기'),(self.preview_frame,'구절 미리보기')):
            frame.columnconfigure(0,weight=1);frame.rowconfigure(0,weight=1);self.notebook.add(frame,text=label)
        self.paste=tk.Text(self.input_frame,wrap='word',font=('Malgun Gothic',11),undo=True)
        self.paste.grid(row=0,column=0,sticky='nsew')
        scroll=ttk.Scrollbar(self.input_frame,command=self.paste.yview);scroll.grid(row=0,column=1,sticky='ns')
        self.paste.configure(yscrollcommand=scroll.set)
        self.table=ttk.Treeview(self.preview_frame,columns=('original','reading','korean'),show='headings')
        for key,label in (('original','원문'),('reading','독음'),('korean','번역')):
            self.table.heading(key,text=label);self.table.column(key,width=150,minwidth=65)
        self.table.grid(row=0,column=0,sticky='nsew')
        scroll=ttk.Scrollbar(self.preview_frame,command=self.table.yview);scroll.grid(row=0,column=1,sticky='ns')
        self.table.configure(yscrollcommand=scroll.set)
        self.link=tk.BooleanVar(value=bool(self.playback.title))
        self.link_button=ttk.Checkbutton(w,text='입력창을 열 때 재생하던 곡에 연결',variable=self.link)
        self.link_button.grid(row=4,column=0,sticky='w',padx=12,pady=3)
        if not self.playback.title:self.link_button.configure(state='disabled')
        footer=ttk.Frame(w,padding=12);footer.grid(row=5,column=0,sticky='ew')
        self.buttons=[]
        for column,(label,action) in enumerate((('붙여넣기',self.paste_clipboard),('미리보기',self.preview),('저장',self.save),('닫기',self.close))):
            footer.columnconfigure(column,weight=1)
            button=app.button(footer,label,lambda action=action:self.invoke(action))
            button.grid(row=0,column=column,sticky='ew',padx=3);self.buttons.append(button)
        w.bind('<Configure>',self.resize);w.bind('<Destroy>',self.destroyed)
        w.protocol('WM_DELETE_WINDOW',self.close);w.grab_set();self.paste.focus_set()

    def resize(self,event):
        if event.widget is self.window:
            for label in (self.help,self.status_label):label.configure(wraplength=max(300,event.width-28))

    def invoke(self,action):
        try:action()
        except (ValueError,OSError,tk.TclError) as error:self.status.set(str(error))

    def paste_clipboard(self):
        self.notebook.select(self.input_frame);self.paste.focus_set();self.paste.event_generate('<<Paste>>')

    def lines(self):return parse_manual(self.paste.get('1.0','end-1c'),self.format.current())

    def preview(self):
        lines=self.lines();self.table.delete(*self.table.get_children())
        for line in lines:self.table.insert('','end',values=(line.original,line.reading,line.korean))
        self.status.set(f'{len(lines)}구절 · 번역 {sum(bool(line.korean) for line in lines)}구절. 저장한 뒤 싱크를 맞출 수 있습니다.')
        self.notebook.select(self.preview_frame)

    def save(self):
        # Reparse the current input so an edit after preview is never lost.
        song=manual_song(self.title.get(),self.artist.get(),self.album.get(),self.lines())
        if self.app.add_manual_song(song,self.playback.key if self.playback.title and self.link.get() else None,parent=self.window):
            self.close()

    def close(self):
        if self.closed:return
        self.closed=True;self.app.hotkeys.suspend(self.was_suspended);self.app.manual_dialog=None
        self.window.destroy()

    def destroyed(self,event):
        if event.widget is self.window and not self.closed:
            self.closed=True;self.app.hotkeys.suspend(self.was_suspended);self.app.manual_dialog=None
