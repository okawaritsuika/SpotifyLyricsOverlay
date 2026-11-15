"""Inspect and edit the lyric resources saved for each song."""
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk


RESOURCE_TYPES = (
    ('수동 싱크', 'manual_sync'), ('번역', 'translated'),
    ('원문', 'original'), ('가창자', 'performers'),
)
FIELD_LABELS = dict(original='원문', reading='독음', korean='한국어 번역',
                    timing='수동 싱크', singers='줄별 가창자', roster='참여 가창자')
ROW_FIELDS = ('original', 'reading', 'korean', 'timing', 'singers')
SOURCE_FIELDS = dict(timing='time', singers='performers')
PAGE, SURFACE, LINE = '#f6f6fa', '#ffffff', '#e4e2ec'
INK, MUTED, PINK, PINK_SOFT = '#2d3142', '#656776', '#a3235e', '#fbe3ee'
FONT = 'Segoe UI'


def clock(stamp):
    if stamp is None:
        return ''
    minutes, milliseconds = divmod(round(stamp * 1000), 60_000)
    return f'{minutes:02d}:{milliseconds / 1000:06.3f}'


class TranslationResourceDialog:
    def __init__(self, app):
        self.app = app
        self.closed = self.loading = self.refresh_pending = False
        self.poll_id = None
        self.resources = {}
        self.rows = {}
        self.actions = set()
        self.displayed_id = None
        self.editor = None
        self.window = window = tk.Toplevel(app.root, background=PAGE)
        window.title('내 자료 관리')
        window.geometry('1190x740')
        window.minsize(860, 520)
        window.columnconfigure(0, weight=1)
        window.rowconfigure(1, weight=1)
        self.styles()

        header = tk.Frame(window, background=SURFACE)
        header.grid(row=0, column=0, sticky='ew')
        self.label(header, '내 자료 관리', size=20, bold=True).pack(side='left', padx=23, pady=14)
        self.button(header, '닫기', self.close).pack(side='right', padx=(6, 22))
        self.refresh_button = self.button(header, '새로고침', self.refresh)
        self.refresh_button.pack(side='right')

        body = tk.Frame(window, background=PAGE)
        body.grid(row=1, column=0, sticky='nsew', padx=18, pady=16)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        sidebar = self.card(body)
        sidebar.grid(row=0, column=0, sticky='ns', padx=(0, 14))
        sidebar.columnconfigure(0, minsize=284)
        sidebar.rowconfigure(4, weight=1)
        self.label(sidebar, '곡 목록', size=13, bold=True).grid(row=0, column=0, sticky='w', padx=14, pady=(12, 4))
        self.query = tk.StringVar(window)
        self.label(sidebar, '곡명 · 아티스트 · 앨범', muted=True).grid(row=1, column=0, sticky='w', padx=14)
        search = self.entry(sidebar, self.query)
        search.grid(row=2, column=0, sticky='ew', padx=14, pady=(3, 8), ipady=5)
        self.category = ttk.Combobox(sidebar, values=('전체', *(label for label, _ in RESOURCE_TYPES)),
                                     state='readonly', style='Resource.TCombobox')
        self.category.current(0)
        self.category.grid(row=3, column=0, sticky='ew', padx=14, pady=(0, 9), ipady=3)
        self.songs = self.table(sidebar, 4, (
            ('title', '곡 제목', 147, True), ('artist', '아티스트', 104, True),
            ('album', '앨범', 125, True), ('summary', '저장 자료', 220, True),
            ('source', '출처', 90, True), ('included', '번역 요청', 76, False),
        ), visible=('title', 'artist'), variant='Song.Resource.Treeview')
        self.visible_count = tk.StringVar(window)
        self.label(sidebar, variable=self.visible_count, muted=True).grid(row=5, column=0, sticky='w', padx=14, pady=(7, 10))

        detail = self.card(body)
        detail.grid(row=0, column=1, sticky='nsew')
        detail.columnconfigure(0, weight=1)
        detail.rowconfigure(3, weight=1)
        detail_header = tk.Frame(detail, background=SURFACE)
        detail_header.grid(row=0, column=0, sticky='ew', padx=16, pady=(11, 2))
        detail_header.columnconfigure(0, weight=1)
        self.title = tk.StringVar(window, '곡을 선택하세요')
        self.selection_label = tk.StringVar(window)
        self.song_title = self.label(detail_header, variable=self.title, size=16, bold=True, wraplength=460)
        self.song_title.grid(row=0, column=0, sticky='w')
        self.roster_button = self.button(detail_header, '참여 가창자 편집', lambda: self.edit_field('roster'))
        self.roster_button.grid(row=0, column=1, sticky='ne', padx=(8, 0))
        self.selection_info = self.label(detail_header, variable=self.selection_label, muted=True, wraplength=540)
        self.selection_info.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(3, 0))
        detail_header.bind('<Configure>', self.resize_detail)
        self.badges = tk.Frame(detail, background=SURFACE)
        self.badges.grid(row=1, column=0, sticky='ew', padx=16, pady=(6, 11))
        table_heading = tk.Frame(detail, background=SURFACE)
        table_heading.grid(row=2, column=0, sticky='ew', padx=16, pady=(0, 5))
        self.label(table_heading, '가사와 싱크', bold=True, size=12).pack(side='left')
        self.inclusion_button = self.button(table_heading, '번역 요청에서 제외', self.toggle_inclusion)
        self.preview = self.table(detail, 3, (
            ('number', '줄', 43, False), ('original', '원문', 205, True), ('reading', '독음', 121, True),
            ('korean', '한국어 번역', 187, True), ('timing', '수동 싱크', 94, False),
            ('singers', '가창자', 108, True),
        ))
        toolbar = tk.Frame(detail, background=SURFACE)
        toolbar.grid(row=4, column=0, sticky='ew', padx=16, pady=(10, 12))
        self.row_label = tk.StringVar(window, '편집할 줄 선택')
        self.label(toolbar, variable=self.row_label, bold=True).pack(side='left', padx=(0, 9))
        self.field = ttk.Combobox(toolbar, state='disabled', style='Resource.TCombobox', width=11)
        self.field.pack(side='left', padx=(0, 7), ipady=3)
        self.edit_button = self.button(toolbar, '편집…', self.edit_field, primary=True)
        self.edit_button.pack(side='left', padx=(0, 6))
        self.clear_button = self.button(toolbar, '직접 저장값 지우기', self.clear_selected)
        self.clear_button.pack(side='left')
        self.status = tk.StringVar(window)
        self.label(window, variable=self.status, muted=True).grid(row=2, column=0, sticky='ew', padx=18, pady=(0, 11))

        self.query.trace_add('write', self.filter_songs)
        self.category.bind('<<ComboboxSelected>>', self.filter_songs)
        self.songs.bind('<<TreeviewSelect>>', self.preview_selected)
        self.preview.bind('<<TreeviewSelect>>', self.update_row_toolbar)
        self.preview.bind('<ButtonRelease-1>', self.select_field_at)
        self.preview.bind('<Double-Button-1>', self.edit_field_at)
        self.preview.bind('<Return>', lambda _event: self.edit_field())
        self.field.bind('<<ComboboxSelected>>', self.update_row_toolbar)
        window.bind('<Control-f>', lambda _event: search.focus_set())
        window.bind('<Destroy>', self.destroyed)
        window.protocol('WM_DELETE_WINDOW', self.close)
        search.focus_set()
        self.preview_selected()
        self.refresh()

    def styles(self):
        style = ttk.Style(self.window)
        for variant, height in (('Resource.Treeview', 31), ('Song.Resource.Treeview', 35)):
            style.configure(variant, background=SURFACE, fieldbackground=SURFACE, foreground=INK,
                            font=(FONT, 10), rowheight=height, borderwidth=0)
            style.map(variant, foreground=[('selected', PINK)], background=[('selected', PINK_SOFT)])
            style.configure(variant + '.Heading', font=(FONT, 9, 'bold'), foreground=INK,
                            background=PAGE, relief='flat', padding=(7, 6))
        style.configure('Resource.TCombobox', font=(FONT, 10), padding=3)

    @staticmethod
    def label(parent, text='', *, variable=None, bold=False, muted=False, size=10, **options):
        return tk.Label(parent, text=text, textvariable=variable,
                        font=(FONT, size, 'bold' if bold else 'normal'),
                        background=parent.cget('background'), foreground=MUTED if muted else INK,
                        anchor='w', justify='left', **options)

    @staticmethod
    def button(parent, text, action, *, primary=False):
        button = tk.Button(parent, text=text, command=action, font=(FONT, 10, 'bold'),
                           background=PINK if primary else PAGE, foreground=SURFACE if primary else INK,
                           activebackground='#842048' if primary else PINK_SOFT,
                           activeforeground=SURFACE if primary else PINK, disabledforeground='#878897',
                           relief='flat', borderwidth=0, highlightthickness=1,
                           highlightbackground=LINE, highlightcolor=PINK, padx=10, pady=6, takefocus=True)
        button.bind('<Return>', lambda _event: button.invoke())
        return button

    @staticmethod
    def entry(parent, variable=None):
        return tk.Entry(parent, textvariable=variable, font=(FONT, 10), background=SURFACE,
                        foreground=INK, relief='flat', highlightthickness=1,
                        highlightbackground=LINE, highlightcolor=PINK, insertbackground=INK)

    @staticmethod
    def card(parent):
        return tk.Frame(parent, background=SURFACE, highlightthickness=1, highlightbackground=LINE)

    @staticmethod
    def table(parent, row, columns, *, visible='#all', variant='Resource.Treeview'):
        frame = tk.Frame(parent, background=SURFACE, highlightthickness=1, highlightbackground=LINE)
        frame.grid(row=row, column=0, sticky='nsew', padx=13)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        tree = ttk.Treeview(frame, columns=tuple(item[0] for item in columns), displaycolumns=visible,
                            show='headings', selectmode='browse', height=1, style=variant)
        for key, title, width, stretch in columns:
            tree.heading(key, text=title, anchor='w')
            tree.column(key, width=width, minwidth=width if not stretch else 66, stretch=stretch,
                        anchor='center' if key in ('number', 'timing') else 'w')
        tree.grid(row=0, column=0, sticky='nsew')
        vertical = ttk.Scrollbar(frame, orient='vertical', command=tree.yview)
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal = ttk.Scrollbar(frame, orient='horizontal', command=tree.xview)
        horizontal.grid(row=1, column=0, sticky='ew')
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        tree.tag_configure('alternate', background='#faf9fc')
        return tree

    def resize_detail(self, event):
        self.song_title.configure(wraplength=max(190, event.width - 158))
        self.selection_info.configure(wraplength=event.width)

    def selected_id(self):
        selected = self.songs.selection()
        return selected[0] if selected else None

    def selected_row(self):
        selected = self.preview.selection()
        return self.rows.get(selected[0]) if selected else None

    def filter_songs(self, *_):
        selected = self.selected_id()
        words = self.query.get().casefold().split()
        category = self.category.current()
        field = RESOURCE_TYPES[category - 1][1] if category > 0 else None
        self.songs.delete(*self.songs.get_children())
        count = 0
        for ident, value in self.resources.items():
            if field and not value[field]:
                continue
            artist = ', '.join(value['artists'])
            searchable = ' '.join((value['title'], artist, value['album'])).casefold()
            if all(word in searchable for word in words):
                summary = ' · '.join(f'{label} {value[key]}' for label, key in RESOURCE_TYPES if value[key])
                request = {True: '포함', False: '제외'}.get(value['included'], '')
                self.songs.insert('', 'end', iid=ident, values=(
                    value['title'], artist, value['album'], summary, value['source'], request,
                ), tags=('alternate',) if count % 2 else ())
                count += 1
        self.visible_count.set(f'{count:,} / {len(self.resources):,}곡' if self.resources else '저장한 자료 없음')
        if selected and self.songs.exists(selected):
            self.songs.selection_set(selected)
            self.songs.see(selected)
        self.preview_selected()

    def preview_selected(self, _event=None):
        ident = self.selected_id()
        selected = self.preview.selection() if ident == self.displayed_id else ()
        if _event is not None and ident == self.displayed_id:
            return
        self.displayed_id = ident
        self.rows.clear()
        self.actions.clear()
        self.preview.delete(*self.preview.get_children())
        for badge in self.badges.winfo_children():
            badge.destroy()
        self.inclusion_button.pack_forget()
        if not ident:
            self.title.set('곡을 선택하세요')
            self.selection_label.set('')
            self.roster_button.configure(state='disabled')
            self.update_row_toolbar()
            return
        value = self.resources[ident]
        self.title.set(value['title'])
        metadata = ' · '.join(part for part in (', '.join(value['artists']), value['album'], value['source']) if part)
        offset = value.get('offset', 0)
        if offset:
            metadata += f' · 오프셋 {offset:+.3f}초'
        roster = ', '.join(value.get('roster', ()))
        self.selection_label.set(f'{metadata}\n참여 가창자  {roster}' if roster else metadata)
        for label, key in RESOURCE_TYPES:
            if value[key]:
                tk.Label(self.badges, text=f'{label} {value[key]:,}', background=PINK_SOFT,
                         foreground=PINK, font=(FONT, 9, 'bold'), padx=9, pady=3).pack(side='left', padx=(0, 6))
        request = value['included']
        if type(request) is bool:
            self.inclusion_button.configure(text='번역 요청에 다시 포함' if request is False else '번역 요청에서 제외',
                                            state='disabled' if self.loading else 'normal')
            self.inclusion_button.pack(side='right')
        try:
            lines = self.app.translation_resource_details(ident)
            self.actions.update(self.app.translation_resource_actions(ident))
            for number, line in enumerate(lines, 1):
                index = line.get('index', number)
                self.rows[str(index)] = line | {'index': index}
                self.preview.insert('', 'end', iid=str(index), values=(
                    index, line.get('original', ''), line.get('reading', ''), line.get('korean', ''),
                    clock(line.get('time')), ', '.join(line.get('performers', ())),
                ), tags=('alternate',) if number % 2 == 0 else ())
        except (OSError, ValueError) as error:
            self.status.set(f'자료를 열지 못했습니다. {error}')
        self.roster_button.configure(state='normal' if 'roster' in self.actions and not self.loading else 'disabled')
        if selected and self.preview.exists(selected[0]):
            self.preview.selection_set(selected[0])
            self.preview.see(selected[0])
        self.update_row_toolbar()

    def active_field(self):
        choice = self.field.get()
        return next((field for field in ROW_FIELDS if FIELD_LABELS[field] == choice), None)

    def update_row_toolbar(self, _event=None):
        row = self.selected_row()
        choices = tuple(FIELD_LABELS[field] for field in ROW_FIELDS if field in self.actions)
        can_edit = bool(row and choices and not self.loading)
        self.row_label.set(f"{row['index']}줄" if row else '편집할 줄 선택')
        self.field.configure(values=choices, state='readonly' if can_edit else 'disabled')
        if can_edit and self.field.get() not in choices:
            self.field.current(0)
        self.edit_button.configure(state='normal' if can_edit else 'disabled')
        field = self.active_field()
        removable = can_edit and field in row.get('removable', ())
        self.clear_button.configure(state='normal' if removable else 'disabled')

    def select_field_at(self, event):
        column = self.preview.identify_column(event.x)
        if self.preview.identify_region(event.x, event.y) != 'cell' or column in ('', '#1'):
            return
        name = ROW_FIELDS[int(column[1:]) - 2]
        if name in self.actions:
            self.field.set(FIELD_LABELS[name])
            self.update_row_toolbar()
            return name

    def edit_field_at(self, event):
        field = self.select_field_at(event)
        if field:
            self.edit_field(field)

    def edit_field(self, field=None):
        ident = self.selected_id()
        field = field or self.active_field()
        row = self.selected_row()
        if self.loading or not ident or field not in self.actions or (field != 'roster' and not row):
            return
        value = self.resources[ident]
        index = row['index'] - 1 if field != 'roster' else None
        names = field in ('roster', 'singers')
        seeded = value.get('roster', ()) if field == 'roster' else row.get(SOURCE_FIELDS.get(field, field), '')
        original = '\n'.join(seeded) if names else (str(seeded) if seeded not in (None, '') else '')
        title = FIELD_LABELS[field]
        editor = tk.Toplevel(self.window, background=PAGE)
        self.editor = editor
        editor.title(f'{title} 편집')
        editor.transient(self.window)
        editor.columnconfigure(0, weight=1)
        editor.rowconfigure(1, weight=1)
        editor.minsize(420, 200)
        width, height = 512, 238 if field == 'timing' else 320
        x = self.window.winfo_rootx() + (self.window.winfo_width() - width) // 2
        y = self.window.winfo_rooty() + (self.window.winfo_height() - height) // 2
        editor.geometry(f'{width}x{height}+{x}+{y}')
        header = tk.Frame(editor, background=PAGE)
        header.grid(row=0, column=0, sticky='ew', padx=17, pady=(13, 8))
        self.label(header, f'{title} 편집', size=15, bold=True).pack(anchor='w')
        context = f"{value['title']} · {index + 1}줄" if index is not None else value['title']
        self.label(header, context, muted=True, wraplength=455).pack(anchor='w', pady=(2, 0))
        instructions = ('이름을 한 줄에 한 명씩 입력하세요.' if names else
                        '시작 시간을 초 단위로 입력하세요. 예 12.345' if field == 'timing' else '')
        if instructions:
            self.label(header, instructions, muted=True).pack(anchor='w', pady=(5, 0))
        if field == 'timing':
            draft = tk.StringVar(editor, original)
            entry = self.entry(editor, draft)
            read_draft = draft.get
            entry.grid(row=1, column=0, sticky='new', padx=17, ipady=5)
            entry.select_range(0, 'end')
        else:
            entry = tk.Text(editor, font=(FONT, 11), wrap='word', undo=True, height=4,
                            relief='flat', padx=10, pady=8, foreground=INK, background=SURFACE,
                            highlightthickness=1, highlightbackground=LINE, highlightcolor=PINK)
            entry.insert('1.0', original)
            entry.grid(row=1, column=0, sticky='nsew', padx=17)
            read_draft = lambda: entry.get('1.0', 'end-1c')
        error_var = tk.StringVar(editor)
        error = self.label(editor, variable=error_var, muted=True, wraplength=455)
        error.grid(row=2, column=0, sticky='ew', padx=17, pady=(6, 0))
        controls = tk.Frame(editor, background=PAGE)
        controls.grid(row=3, column=0, sticky='ew', padx=17, pady=(7, 14))

        def close():
            self.editor = None
            editor.destroy()

        def save():
            text = read_draft().strip()
            draft = [line.strip() for line in text.splitlines() if line.strip()] if names else text
            try:
                self.app.update_translation_resource(ident, index, field, draft)
            except (OSError, ValueError) as error:
                error_var.set(str(error))
                return
            close()
            self.refresh(f'{title} 저장 완료')

        self.button(controls, '저장', save, primary=True).pack(side='right')
        self.button(controls, '취소', close).pack(side='right', padx=(0, 6))
        if field == 'roster' and value.get('roster_saved'):
            def remove_roster():
                if self.clear_field(ident, None, 'roster', editor):
                    close()
            self.button(controls, '직접 저장값 지우기', remove_roster).pack(side='left')
        editor.bind('<Escape>', lambda _event: close())
        editor.bind('<Control-Return>', lambda _event: save())
        editor.protocol('WM_DELETE_WINDOW', close)
        editor.grab_set()
        entry.focus_set()

    def clear_selected(self):
        row = self.selected_row()
        field = self.active_field()
        ident = self.selected_id()
        if ident and row and not self.loading and field in row.get('removable', ()) and field in self.actions:
            self.clear_field(ident, row['index'] - 1, field, self.window)

    def clear_field(self, ident, index, field, parent):
        label = FIELD_LABELS[field]
        target = f'{index + 1}줄 {label}' if index is not None else label
        if not messagebox.askyesno('직접 저장값 지우기',
                                   f'{target}의 직접 저장값을 지울까요?\n원래 값이 있으면 다시 표시됩니다.',
                                   parent=parent, icon='warning', default='no'):
            return False
        try:
            self.app.update_translation_resource(ident, index, field, None)
        except (OSError, ValueError) as error:
            self.status.set(f'저장값을 지우지 못했습니다. {error}')
            return False
        self.refresh(f'{label} 직접 저장값을 지웠습니다.')
        return True

    def refresh(self, feedback=''):
        if self.closed:
            return
        if self.loading:
            self.refresh_pending = True
            return
        self.loading = True
        self.refresh_button.configure(state='disabled')
        self.inclusion_button.configure(state='disabled')
        self.roster_button.configure(state='disabled')
        self.update_row_toolbar()
        self.status.set('저장한 자료를 확인하고 있습니다…')
        results = queue.Queue(maxsize=1)

        def load():
            try:
                results.put((True, self.app.list_translation_resources()))
            except (OSError, ValueError) as error:
                results.put((False, str(error)))

        threading.Thread(target=load, daemon=True, name='translation-resources').start()
        self.poll_id = self.window.after(60, self.loaded, results, feedback)

    def loaded(self, results, feedback):
        self.poll_id = None
        try:
            success, value = results.get_nowait()
        except queue.Empty:
            self.poll_id = self.window.after(60, self.loaded, results, feedback)
            return
        self.loading = False
        self.refresh_button.configure(state='normal')
        if success:
            self.resources = {item['id']: item for item in value}
            self.status.set(feedback)
            self.filter_songs()
        else:
            self.status.set(f'목록을 불러오지 못했습니다. {value}')
            self.preview_selected()
        if self.refresh_pending:
            self.refresh_pending = False
            self.refresh(feedback if success else '')

    def toggle_inclusion(self):
        ident = self.selected_id()
        if not ident or self.loading:
            return
        included = self.resources[ident]['included']
        if type(included) is not bool:
            return
        try:
            self.app.set_translation_resource_included(ident, not included)
        except (OSError, ValueError) as error:
            self.status.set(f'번역 요청 대상을 바꾸지 못했습니다. {error}')
            return
        self.resources[ident]['included'] = not included
        values = self.songs.item(ident, 'values')
        self.songs.item(ident, values=(*values[:-1], '제외' if included else '포함'))
        self.preview_selected()

    def destroyed(self, event):
        if event.widget is self.window:
            self.closed = True
            if self.poll_id is not None:
                self.window.after_cancel(self.poll_id)
                self.poll_id = None
            if self.app.translation_resource_dialog is self:
                self.app.translation_resource_dialog = None

    def close(self):
        if not self.closed:
            self.window.destroy()
