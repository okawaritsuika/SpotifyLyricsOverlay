"""Search ALSong and preview one registration for the current Spotify track."""
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPException
import tkinter as tk
from tkinter import ttk

from alsong_provider import load_alsong_registration, search_alsong, validate_alsong_query
from lyric_annotations import readings_for_display
from ui_theme import PANEL


def time_label(seconds):
    if seconds is None:
        return '미제공'
    minutes, remainder = divmod(int(seconds), 60)
    return f'{minutes}:{remainder:02}'


class AlsongDialog:
    def __init__(self, developer, parent=None):
        self.developer = developer
        self.app = developer.app
        playback = self.playback = self.app.playback
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='alsong-manual')
        self.waiting = None
        self.preview = None
        self.closed = False
        owner = parent or developer.window
        window = self.window = tk.Toplevel(owner)
        window.title('알송 가사 찾기')
        window.geometry('680x520')
        window.minsize(500, 400)
        if owner.winfo_viewable():
            window.transient(owner)
        window.columnconfigure(0, weight=1)
        window.rowconfigure(1, weight=1)
        window.protocol('WM_DELETE_WINDOW', self.close)
        window.bind('<Escape>', lambda _: self.close())

        fields = ttk.Frame(window, padding=(12, 10))
        fields.grid(row=0, column=0, sticky='ew')
        fields.columnconfigure(1, weight=1)
        self.query_key = f'{playback.key}\0{playback.duration:.3f}'
        self.default_query = tuple(value.strip() for value in (playback.title, playback.artist, playback.album))
        saved_query = self.search_queries().get(self.query_key)
        if not (isinstance(saved_query, list) and len(saved_query) == 3
                and all(isinstance(value, str) for value in saved_query)):
            saved_query = None
        self.query = tuple(tk.StringVar(master=window, value=value)
                           for value in (saved_query or self.default_query))
        for row, (label, value) in enumerate(zip(('곡 제목', '가수', '앨범 우선'), self.query)):
            ttk.Label(fields, text=label).grid(row=row, column=0, sticky='w', padx=(0, 8), pady=2)
            entry = ttk.Entry(fields, textvariable=value)
            entry.grid(row=row, column=1, sticky='ew', pady=2)
            entry.bind('<Return>', self.search)
            if row == 0:
                self.title_entry = entry
        self.search_button = ttk.Button(fields, text='검색', command=self.search)
        self.search_button.grid(row=0, column=2, sticky='ew', padx=(8, 0))
        self.reset_button = ttk.Button(fields, text='초기화', command=self.reset_query)
        self.reset_button.grid(row=1, column=2, sticky='ew', padx=(8, 0), pady=2)

        panes = tk.PanedWindow(window, orient='vertical', background=PANEL,
                               sashwidth=9, sashrelief='raised', showhandle=True,
                               sashcursor='sb_v_double_arrow')
        panes.grid(row=1, column=0, sticky='nsew', padx=12)
        results = ttk.Frame(panes)
        results.columnconfigure(0, weight=1)
        results.rowconfigure(0, weight=1)
        self.results = ttk.Treeview(results, columns=('title', 'artist', 'album'),
                                    show='headings', selectmode='browse', height=3)
        for key, label, width in (('title', '곡 제목', 220), ('artist', '가수', 170), ('album', '앨범', 160)):
            self.results.heading(key, text=label)
            self.results.column(key, width=width, minwidth=75)
        self.results.grid(row=0, column=0, sticky='nsew')
        scroll = ttk.Scrollbar(results, command=self.results.yview)
        scroll.grid(row=0, column=1, sticky='ns')
        self.results.configure(yscrollcommand=scroll.set)
        self.results.bind('<<TreeviewSelect>>', self.selection_changed)
        self.results.bind('<Double-1>', self.load_selection)
        self.results.bind('<Return>', self.load_selection)
        panes.add(results, minsize=70, stretch='never')

        preview = ttk.Frame(panes)
        preview.columnconfigure(0, weight=1)
        preview.rowconfigure(0, weight=1)
        self.text = tk.Text(preview, height=6, wrap='word', state='disabled', font=('Malgun Gothic', 10))
        self.text.grid(row=0, column=0, sticky='nsew')
        preview_scroll = ttk.Scrollbar(preview, orient='vertical', command=self.text.yview)
        preview_scroll.grid(row=0, column=1, sticky='ns')
        self.text.configure(yscrollcommand=preview_scroll.set)
        panes.add(preview, minsize=72, stretch='always')
        self.timing_info = tk.StringVar(master=window, value=f'Spotify 길이 {time_label(playback.duration)}')
        ttk.Label(window, textvariable=self.timing_info).grid(row=2, column=0, sticky='w', padx=12, pady=(6, 0))
        self.status = tk.StringVar(master=window, value='제목과 가수는 알송에 전송됩니다. 앨범은 결과 정렬에 사용합니다.')
        status = ttk.Label(window, textvariable=self.status, wraplength=630)
        status.grid(row=3, column=0, sticky='ew', padx=12, pady=5)
        window.bind('<Configure>', lambda event: status.configure(wraplength=event.width-24)
                    if event.widget is window else None)
        actions = ttk.Frame(window, padding=(12, 0, 12, 10))
        actions.grid(row=4, column=0, sticky='ew')
        self.preview_button = ttk.Button(actions, text='미리보기', command=self.load_selection, state='disabled')
        self.preview_button.pack(side='left')
        self.link_button = ttk.Button(actions, text='현재 곡에 임시 연결', command=self.link, state='disabled')
        self.link_button.pack(side='right')
        ttk.Button(actions, text='닫기', command=self.close).pack(side='right', padx=6)
        window.bind('<Map>', self.activate)
        if saved_query is not None:
            self.search()

    def activate(self, event):
        if event.widget is self.window:
            self.window.grab_set()
            self.title_entry.focus_set()

    def current_track(self):
        current = self.app.playback
        return (current.has_timeline and current.key == self.playback.key
                and abs(current.duration-self.playback.duration) <= 2)

    def set_preview(self, value):
        self.preview = value
        self.text.configure(state='normal')
        self.text.delete('1.0', 'end')
        info = f'Spotify 길이 {time_label(self.app.playback.duration)}'
        if value:
            lines = value['lines']
            readings = readings_for_display(lines)
            blocks = ('\n'.join(text for text in (line.original, reading, line.korean) if text.strip())
                      for line, reading in zip(lines, readings))
            self.text.insert('1.0', '\n\n'.join(blocks))
            info += (f' · 알송 곡 길이 {time_label(value.get("duration"))}'
                     f' · 마지막 가사 시작 {time_label(value.get("last_onset"))}')
        self.timing_info.set(info)
        self.text.configure(state='disabled')
        self.link_button.configure(state='normal' if value else 'disabled')

    def selection_changed(self, _event=None):
        selected = self.results.selection()
        if self.preview and selected != (self.preview['provider_id'],):
            self.set_preview(None)
        self.preview_button.configure(state='normal' if selected and self.waiting is None else 'disabled')

    def search_queries(self):
        queries = self.app.settings.get('alsong_search_queries', {})
        return queries if isinstance(queries, dict) else {}

    def save_query(self, criteria):
        queries = self.search_queries()
        saved = None if criteria == self.default_query else list(criteria)
        if queries.get(self.query_key) == saved:
            return
        updated = dict(queries)
        if saved is None:
            updated.pop(self.query_key, None)
        else:
            updated[self.query_key] = saved
        settings = dict(self.app.settings, alsong_search_queries=updated)
        self.app.store.save_settings(settings)
        self.app.settings = settings

    def reset_query(self):
        try:
            self.save_query(self.default_query)
        except OSError as error:
            self.status.set(f'알송 검색어 초기화 실패 · {error}')
            return
        for variable, value in zip(self.query, self.default_query):
            variable.set(value)
        self.search()

    def search(self, _event=None):
        if self.waiting is not None:
            return 'break'
        self.set_preview(None)
        self.results.delete(*self.results.get_children())
        try:
            criteria = validate_alsong_query(*(value.get() for value in self.query))
            self.save_query(criteria)
        except (ValueError, OSError) as error:
            self.status.set(f'알송 검색 실패 · {error}')
            return 'break'
        self.start(search_alsong, criteria, self.show_results, '알송 검색 중…')
        return 'break'

    def show_results(self, records):
        for record in records:
            self.results.insert('', 'end', iid=record['lyricID'],
                                values=(record['title'], record['artist'], record['album']))
        self.status.set(f'{len(records)}건 · 미리 볼 등록본을 선택하세요.' if records else
                        '검색 결과가 없습니다. 제목을 다듬거나 가수를 비우고 다시 검색하세요.')
        if records:
            self.results.selection_set(records[0]['lyricID'])
            self.selection_changed()

    def load_selection(self, _event=None):
        selected = self.results.selection()
        if not selected or self.waiting is not None:
            return 'break'
        identifier = selected[0]
        self.set_preview(None)
        self.start(load_alsong_registration, (identifier, self.playback.duration),
                   self.show_registration, '선택한 가사 불러오는 중…')
        return 'break'

    def show_registration(self, result):
        if self.results.selection() != (result['provider_id'],):
            self.status.set('미리 볼 등록본을 다시 선택하세요.')
            return
        self.set_preview(result)
        count = sum(stamp is not None for stamp in result['times'])
        clock = f'싱크 {count}줄' if count else '가사만 연결'
        if result['clock_mismatch']:
            clock += ' · 재생 길이 차이'
        translated = sum(bool(line.korean.strip()) for line in result['lines'])
        self.status.set(f'{len(result["lines"])}줄 · {clock} · 번역 {translated}줄' if translated else
                        f'{len(result["lines"])}줄 · {clock} · 등록본 번역 없음')

    def start(self, operation, args, complete, message):
        self.status.set(message)
        self.search_button.configure(state='disabled')
        self.reset_button.configure(state='disabled')
        self.preview_button.configure(state='disabled')
        future = self.executor.submit(operation, *args)
        def poll():
            if not future.done():
                self.waiting = self.window.after(80, poll)
                return
            self.waiting = None
            self.search_button.configure(state='normal')
            self.reset_button.configure(state='normal')
            self.selection_changed()
            try:
                complete(future.result())
            except (OSError, ValueError, HTTPException) as error:
                self.status.set(f'알송 조회 실패 · {error}')
        self.waiting = self.window.after(80, poll)

    def link(self):
        if not self.preview:
            return
        if not self.current_track():
            self.status.set('재생 곡이 바뀌었습니다. 창을 닫고 현재 곡으로 다시 검색하세요.')
            return
        try:
            self.developer.link_alsong(self.preview)
        except (ValueError, OSError) as error:
            self.status.set(str(error))
            return
        self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.waiting is not None:
            self.window.after_cancel(self.waiting)
            self.waiting = None
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.developer.alsong_dialog = None
        self.window.destroy()
