"""Select the page connection backend and inspect native-reader results."""
import tkinter as tk
from tkinter import ttk
from web_lyrics import sidem_url,document_name
from reader_bridge import _document_name


class ReaderDialog:
    def __init__(self,app):
        self.app=app
        self.window=tk.Toplevel(app.root)
        self.window.title('번역 연결 고급 설정')
        self.window.geometry('600x500');self.window.minsize(520,480)
        self.window.columnconfigure(0,weight=1);self.window.rowconfigure(3,weight=1)
        self.window.protocol('WM_DELETE_WINDOW',self.close)
        modes=tk.Frame(self.window);modes.grid(row=0,column=0,sticky='ew',padx=12,pady=8)
        for label,value in (('앱 내부 브라우저 · Chrome 확장 없이','internal'),('Chrome 확장 · 수신 후 탭 자동 닫기','chrome')):
            tk.Radiobutton(modes,text=label,value=value,variable=app.reader_mode,
                           command=lambda:app.safe(app.change_reader_mode)).pack(anchor='w')
        tk.Label(self.window,text='내부 브라우저는 연결할 때만 실행합니다.\n확인한 가사는 PC에 저장해 다음 재생 때 바로 표시합니다.',
                 justify='left',anchor='w',wraplength=490).grid(row=1,column=0,sticky='ew',padx=12)
        self.status=tk.Label(self.window,justify='left',anchor='nw',wraplength=490,height=3)
        self.status.grid(row=2,column=0,sticky='ew',padx=12,pady=6)
        self.choices=ttk.Treeview(self.window,columns=('title',),show='headings',height=3,selectmode='browse')
        self.choices.heading('title',text='동명 문서 후보 · 선택 후 이 문서 연결')
        self.choices.column('title',width=450,minwidth=100)
        self.choices.grid(row=3,column=0,sticky='nsew',padx=12)
        address=tk.Frame(self.window);address.grid(row=4,column=0,sticky='ew',padx=12,pady=(8,0))
        address.columnconfigure(0,weight=1)
        tk.Label(address,text='나무위키 · SideM KO 위키 문서 주소로 연결',anchor='w').grid(row=0,column=0,columnspan=2,sticky='w')
        self.document_url=tk.StringVar(value=app.reader_bridge.snapshot().get('document_url',''))
        self.url_entry=tk.Entry(address,textvariable=self.document_url)
        self.url_entry.grid(row=1,column=0,sticky='ew',pady=(3,0))
        self.url_connect=tk.Button(address,text='주소 연결',command=lambda:app.safe(self.connect_url))
        self.url_connect.grid(row=1,column=1,padx=(6,0),pady=(3,0))
        self.url_entry.bind('<Return>',lambda _:app.safe(self.connect_url))
        self.sidem_find=tk.Button(address,text='SideM 위키에서 현재 곡 찾기',command=lambda:app.safe(self.find_sidem))
        self.sidem_find.grid(row=2,column=0,columnspan=2,sticky='w',pady=(5,0))
        actions=tk.Frame(self.window);actions.grid(row=5,column=0,sticky='ew',padx=12,pady=10)
        self.retry=tk.Button(actions,text='현재 곡 다시 연결',command=lambda:app.safe(lambda:app.internal_reader.connect()))
        self.retry.pack(side='left')
        self.original=tk.Button(actions,text='원본 확인',command=lambda:app.safe(lambda:app.internal_reader.connect(visible=True)))
        self.original.pack(side='left',padx=6)
        self.choose=tk.Button(actions,text='이 문서 연결',command=lambda:app.safe(self.connect_selected))
        self.choose.pack(side='right')
        tk.Button(self.window,text='간단한 가사 연결 화면',command=app.open_connection).grid(row=6,column=0,sticky='w',padx=12,pady=(0,8))
        self.window.bind('<Configure>',lambda event:self.status.configure(wraplength=max(300,event.width-28)) if event.widget is self.window else None)
        self.refresh()

    def refresh(self):
        reader=self.app.internal_reader
        internal=self.app.reader_mode.get()=='internal'
        self.status.configure(text=reader.status if internal else 'Chrome 확장에서 자동 연동을 켜세요.\n가사창 표시가 확인되면 확장이 만든 비활성 탭만 닫습니다.')
        for button in (self.retry,self.original,self.choose,self.url_entry,self.url_connect,self.sidem_find):
            button.configure(state='normal' if internal else 'disabled')
        if tuple(self.choices.get_children())!=tuple(reader.choices):
            self.choices.delete(*self.choices.get_children())
            for url in reader.choices:self.choices.insert('','end',iid=url,values=(document_name(url),))

    def connect_selected(self):
        selected=self.choices.selection()
        if not selected:raise ValueError('연결할 문서를 목록에서 선택하세요.')
        self.app.internal_reader.connect(selected_url=selected[0])

    def connect_url(self):
        url=self.document_url.get().strip()
        if not url:raise ValueError('연결할 나무위키 또는 SideM KO 위키 문서 주소를 입력하세요.')
        self.app.internal_reader.connect(selected_url=url)

    def find_sidem(self):
        state=self.app.reader_bridge.snapshot()
        if not state.get('title'):raise ValueError('Spotify에서 곡을 먼저 선택하세요.')
        url=sidem_url(_document_name(state['title']))
        self.document_url.set(url)
        # Searching a suggested title is not manual approval of its lyric contents.
        self.app.internal_reader.connect({**state,'document_url':'','suggested_document_url':url})

    def close(self):
        self.window.destroy();self.app.reader_dialog=None
